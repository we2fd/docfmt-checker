"""业务编排层：把「上传 → 解析 → 检查 → 修复 → 导出」串起来。

刻意保持这一层很薄：真正的能力都在 core/ 里，这里只负责取文件、落库、装配。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from . import db
from .config import Settings, get_settings
from .core import engine, fixer, report as report_builder
from .core import template as template_core
from .core.docmodel import DocumentModel, parse_docx
from .core.rules import Ruleset, dump_ruleset, load_builtin_ruleset, parse_ruleset
from .errors import CorruptedDocument, FileTooLarge, NotFound, RulesetInvalid, UnsupportedFileType

BUILTIN_RULESET_ID = "default"

TEMPLATE_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def _safe_name(filename: str) -> str:
    name = Path(filename).name
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    return name or "document.docx"


class DocFmtService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._models: dict[str, DocumentModel] = {}
        self.bootstrap_builtin_ruleset()

    # ------------------------------------------------------------ 规则集

    def bootstrap_builtin_ruleset(self) -> None:
        """首次启动把内置规则写进库；之后一律以库里的为准（用户可能改过）。"""
        if db.get_ruleset(BUILTIN_RULESET_ID) is not None:
            return
        path = self.settings.rules_dir / f"{BUILTIN_RULESET_ID}.yaml"
        if not path.exists():  # pragma: no cover
            return
        ruleset = load_builtin_ruleset(path)
        db.upsert_ruleset(ruleset.id, ruleset.name, dump_ruleset(ruleset), is_builtin=True)

    def reset_builtin_ruleset(self) -> Ruleset:
        path = self.settings.rules_dir / f"{BUILTIN_RULESET_ID}.yaml"
        ruleset = load_builtin_ruleset(path)
        db.upsert_ruleset(ruleset.id, ruleset.name, dump_ruleset(ruleset), is_builtin=True)
        return ruleset

    def load_ruleset(self, ruleset_id: str | None = None) -> Ruleset:
        ruleset_id = ruleset_id or self.settings.default_ruleset
        record = db.get_ruleset(ruleset_id)
        if record is None:
            if ruleset_id == BUILTIN_RULESET_ID:
                self.bootstrap_builtin_ruleset()
                record = db.get_ruleset(ruleset_id)
            if record is None:
                raise NotFound(f"规则集「{ruleset_id}」不存在。", hint="可在「规则库」页签查看可用规则集。")
        return parse_ruleset(record["yaml_text"], fallback_id=ruleset_id)

    def list_rulesets(self) -> list[dict[str, Any]]:
        result = []
        for record in db.list_rulesets():
            try:
                ruleset = parse_ruleset(record["yaml_text"], fallback_id=record["id"])
                meta = ruleset.to_dict(include_rules=False)
            except RulesetInvalid as exc:
                meta = {"id": record["id"], "name": record["name"], "rule_count": 0, "error": exc.message}
            meta["is_builtin"] = bool(record.get("is_builtin"))
            meta["updated_at"] = record.get("updated_at")
            result.append(meta)
        return result

    def save_ruleset(self, ruleset_id: str, yaml_text: str) -> Ruleset:
        ruleset = parse_ruleset(yaml_text, fallback_id=ruleset_id)  # 校验失败会抛 RulesetInvalid
        record = db.get_ruleset(ruleset_id)
        db.upsert_ruleset(
            ruleset_id,
            ruleset.name,
            dump_ruleset(ruleset),
            is_builtin=bool(record and record.get("is_builtin")),
        )
        return ruleset

    def toggle_rule(self, ruleset_id: str, rule_id: str, enabled: bool) -> Ruleset:
        ruleset = self.load_ruleset(ruleset_id)
        found = False
        for rule in ruleset.rules:
            if rule.id == rule_id:
                rule.enabled = enabled
                found = True
        if not found:
            raise NotFound(f"规则「{rule_id}」不存在。")
        self.save_ruleset(ruleset_id, dump_ruleset(ruleset))
        return ruleset

    # ------------------------------------------------------------ 阈值快速调整

    def quick_groups(self, ruleset_id: str | None = None) -> dict[str, Any]:
        """把规则里「本来就存在的阈值」按用途分组返回，供界面直接改。

        完全由规则自己的声明推导（checker + params.prop），新增规则会自动出现在这里。
        """
        ruleset = self.load_ruleset(ruleset_id)
        return {"ruleset": ruleset.to_dict(include_rules=False), "groups": template_core.quick_groups(ruleset)}

    def update_rule_params(
        self,
        ruleset_id: str | None,
        updates: list[dict[str, Any]],
        *,
        source_name: str | None = None,
    ) -> tuple[Ruleset, dict[str, Any]]:
        """批量写回阈值参数。单条非法只记入 rejected，不拖垮整批。"""
        target_id = ruleset_id or self.settings.default_ruleset
        ruleset = self.load_ruleset(target_id)
        ruleset, outcome = template_core.apply_updates(ruleset, updates, source_name=source_name)
        if outcome["applied"]:
            self.save_ruleset(target_id, dump_ruleset(ruleset))
        return ruleset, outcome

    # ------------------------------------------------------------ 参考模板

    def _resolve_template(self, template_id: str) -> tuple[Path, str]:
        if not TEMPLATE_ID_RE.match(template_id or ""):
            raise NotFound("参考模板编号不合法。", hint="请重新上传参考模板。")
        matches = sorted(self.settings.templates_dir.glob(f"{template_id}__*"))
        if not matches:
            raise NotFound(
                f"参考模板 {template_id} 不存在。",
                hint="模板文件可能已被清理，请重新上传。",
            )
        path = matches[0]
        name = path.name.split("__", 1)[1] or "参考模板.docx"
        return path, name

    def list_templates(self) -> list[dict[str, Any]]:
        directory = self.settings.templates_dir
        if not directory.exists():
            return []
        items = []
        for path in sorted(directory.glob("*.docx"), key=lambda item: item.stat().st_mtime, reverse=True):
            template_id, _, name = path.name.partition("__")
            items.append(
                {
                    "template_id": template_id,
                    "filename": name or path.name,
                    "size_bytes": path.stat().st_size,
                }
            )
        return items

    def profile_template(
        self,
        filename: str,
        content: bytes,
        ruleset_id: str | None = None,
    ) -> dict[str, Any]:
        """解析上传的参考模板并推导规则提案。

        模板刻意**不进入 documents 台账**：它是「标准」，不是「待检查的论文」，
        混进文档列表只会让人分不清哪份是自己要改的稿子。
        """
        safe = _safe_name(filename)
        suffix = Path(safe).suffix.lower()
        if suffix in self.settings.known_unsupported or suffix not in self.settings.allowed_extensions:
            raise UnsupportedFileType(
                f"参考模板只支持 .docx 格式，收到的是 {suffix or '（无扩展名）'}。",
                hint="请用 Word / WPS 打开模板，选择「另存为」→「Word 文档 (*.docx)」后再上传。",
            )
        if not content:
            raise CorruptedDocument("上传的模板是空文件。", hint="请确认选择的是完整的 .docx 文件。")
        if len(content) > self.settings.max_upload_bytes:
            raise FileTooLarge(
                f"模板 {len(content) / 1024 / 1024:.1f} MB，超过 {self.settings.max_upload_mb} MB 上限。",
                hint="模板一般只有几十 KB；请确认没有把整本论文当成模板上传。",
            )

        self.settings.ensure_dirs()
        template_id = uuid.uuid4().hex[:12]
        target = self.settings.templates_dir / f"{template_id}__{safe}"
        target.write_bytes(content)
        try:
            model = parse_docx(str(target), safe)
        except Exception:
            try:
                target.unlink(missing_ok=True)
            except (OSError, SystemExit):  # pragma: no cover - 清理失败不影响报错
                pass
            raise
        return self._derive_from_model(template_id, safe, model, ruleset_id)

    def derive_from_template(self, template_id: str, ruleset_id: str | None = None) -> dict[str, Any]:
        """对已上传的模板重新推导（用户改过规则之后不必重新上传）。"""
        path, name = self._resolve_template(template_id)
        model = parse_docx(str(path), name)
        return self._derive_from_model(template_id, name, model, ruleset_id)

    def _derive_from_model(
        self,
        template_id: str,
        filename: str,
        model: DocumentModel,
        ruleset_id: str | None,
    ) -> dict[str, Any]:
        ruleset = self.load_ruleset(ruleset_id)
        result = template_core.derive_proposals(ruleset, model)
        result["template_id"] = template_id
        result["filename"] = filename
        result["ruleset_id"] = ruleset.id
        return result

    def delete_template(self, template_id: str) -> dict[str, Any]:
        """删除参考模板。

        用户点了删除，就必须从列表里消失。宿主机可能因为文件被占用、权限不足、
        或安全策略而拒绝删除，所以这里和 delete_document 用同一套思路：
        **先保证「从列表里消失」，再尽力清理磁盘**——卸载失败就归档到 .trash 子目录。
        """
        path, _ = self._resolve_template(template_id)
        try:
            path.unlink()
            return {"deleted": True, "mode": "unlink"}
        except (OSError, SystemExit):
            trash = self.settings.templates_dir / ".trash"
            try:
                trash.mkdir(parents=True, exist_ok=True)
                path.rename(trash / path.name)
                return {"deleted": True, "mode": "archived"}
            except (OSError, SystemExit):  # pragma: no cover - 连重命名都不行时只能如实报告
                return {"deleted": False, "mode": "failed"}

    # ------------------------------------------------------------ 文档

    def upload(self, filename: str, content: bytes) -> dict[str, Any]:
        safe = _safe_name(filename)
        digest = hashlib.sha256(content).hexdigest()

        existing = db.find_document_by_sha(digest)
        if existing and Path(existing["stored_path"]).exists():
            existing.pop("stored_path", None)
            existing["reused"] = True
            existing["summary"] = self.get_model(existing["id"]).summary()
            return existing

        doc_id = uuid.uuid4().hex[:12]
        stored = self.settings.uploads_dir / f"{doc_id}_{safe}"
        stored.write_bytes(content)

        record: dict[str, Any] = {
            "id": doc_id,
            "filename": safe,
            "stored_path": str(stored),
            "size_bytes": len(content),
            "sha256": digest,
            "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "uploaded",
        }
        try:
            model = parse_docx(str(stored), safe)
            summary = model.summary()
            record.update(
                {
                    "status": "parsed",
                    "paragraph_count": summary["paragraph_count"],
                    "char_count": summary["char_count"],
                    "heading_count": summary["heading_count"],
                }
            )
            self._models[doc_id] = model
        except Exception as exc:
            record["status"] = "failed"
            record["error_message"] = str(exc)
            db.insert_document(record)
            raise
        db.insert_document(record)
        record["summary"] = model.summary()
        record["outline"] = model.outline()
        record["reused"] = False
        return record

    # ------------------------------------------------------------ 演示样例

    def list_samples(self) -> list[dict[str, Any]]:
        directory = self.settings.samples_dir
        if not directory.exists():
            return []
        items = []
        for path in sorted(directory.glob("*.docx")):
            items.append({"name": path.name, "size_bytes": path.stat().st_size})
        return items

    def import_sample(self, name: str) -> dict[str, Any]:
        target = self.settings.samples_dir / Path(name).name
        if not target.exists() or target.suffix.lower() != ".docx":
            raise NotFound(
                f"样例 {name} 不存在。",
                hint="先运行 python scripts/make_samples.py 生成样例文档。",
            )
        return self.upload(target.name, target.read_bytes())

    def get_model(self, doc_id: str) -> DocumentModel:
        if doc_id in self._models:
            return self._models[doc_id]
        row = db.get_document(doc_id)
        if row is None:
            raise NotFound(f"文档 {doc_id} 不存在。", hint="可能已被删除，请在左侧重新上传。")
        model = parse_docx(row["stored_path"], row["filename"])
        self._models[doc_id] = model
        return model

    def get_document(self, doc_id: str) -> dict[str, Any]:
        row = db.get_document(doc_id)
        if row is None:
            raise NotFound(f"文档 {doc_id} 不存在。", hint="该文档可能已被删除，请在左侧文档列表中重新上传。")
        return row

    def list_documents(self) -> list[dict[str, Any]]:
        return db.list_documents()

    def delete_document(self, doc_id: str) -> bool:
        row = db.get_document(doc_id)
        if row is None:
            raise NotFound(f"文档 {doc_id} 不存在。", hint="列表已刷新，请重新选择要删除的文档。")

        self._models.pop(doc_id, None)

        # 先把要清理的磁盘文件收集起来，再删元数据。
        # 顺序很重要：删除是用户可见的动作，必须"说删就删"；
        # 磁盘文件清理属于尽力而为，任何失败都不应让整次删除报错
        # （例如文件被其它程序占用、或宿主环境的安全策略拦截了删除）。
        paths = [row["stored_path"]]
        paths.extend(item["stored_path"] for item in db.list_fixed_files(doc_id))

        removed = db.delete_document(doc_id)

        for path in paths:
            try:
                Path(path).unlink(missing_ok=True)
            except (OSError, SystemExit):
                # 清理失败不影响已完成的元数据删除；残留文件后续可手工清理。
                continue

        return removed

    # ------------------------------------------------------------ 检查

    def check(self, doc_id: str, ruleset_id: str | None = None) -> dict[str, Any]:
        row = self.get_document(doc_id)
        model = self.get_model(doc_id)
        ruleset = self.load_ruleset(ruleset_id)
        result = engine.run_checks(model, ruleset)
        result["document_id"] = doc_id
        result["filename"] = row["filename"]
        result["checked_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.save_check_result(
            doc_id,
            score=result["score"],
            issue_summary=result["issue_summary"],
            report=result,
            outline=model.outline(),
        )
        return result

    def paragraphs(self, doc_id: str, offset: int = 0, limit: int = 400) -> dict[str, Any]:
        model = self.get_model(doc_id)
        items = model.paragraphs[offset : offset + limit]
        return {
            "total": len(model.paragraphs),
            "offset": offset,
            "limit": limit,
            "items": [item.to_dict() for item in items],
        }

    def outline(self, doc_id: str) -> list[dict[str, Any]]:
        return self.get_model(doc_id).outline()

    # ------------------------------------------------------------ 修复

    def fix(self, doc_id: str, actions: list[str]) -> dict[str, Any]:
        row = self.get_document(doc_id)
        model = self.get_model(doc_id)
        expected_pt = None
        try:
            ruleset = self.load_ruleset()
            for rule in ruleset.rules:
                if rule.params.get("prop") == "font_size" and rule.params.get("scope") == "body":
                    expected_pt = float(rule.params["expected"])
                    break
        except (NotFound, RulesetInvalid, KeyError, TypeError, ValueError):  # pragma: no cover
            expected_pt = None

        result = fixer.apply_fixes(model, actions, expected_font_size=expected_pt)
        fixed_id = uuid.uuid4().hex[:12]
        target = self.settings.fixed_dir / f"{fixed_id}_{Path(row['filename']).stem}_已规范化.docx"
        fixer.save_fixed(model, target)
        db.insert_fixed_file(
            {
                "id": fixed_id,
                "document_id": doc_id,
                "filename": target.name,
                "stored_path": str(target),
                "actions": result["actions"],
                "diff": result["results"],
            }
        )
        # 修复后原文已变，缓存里的模型要作废，重新解析
        self._models.pop(doc_id, None)
        result["fixed_id"] = fixed_id
        result["filename"] = target.name
        result["original_filename"] = row["filename"]
        result["size_bytes"] = target.stat().st_size if target.exists() else 0
        return result

    def fixed_file(self, fixed_id: str) -> dict[str, Any]:
        record = db.get_fixed_file(fixed_id)
        if record is None:
            raise NotFound(f"修复产物 {fixed_id} 不存在。")
        return record

    # ------------------------------------------------------------ 导出

    def export(self, doc_id: str, kind: str = "markdown") -> dict[str, Any]:
        row = self.get_document(doc_id)
        payload = row.get("report")
        if not payload:
            payload = self.check(doc_id)
        generated_at = payload.get("checked_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if kind == "html":
            text = report_builder.build_html(payload, document_name=row["filename"], generated_at=generated_at)
            media, suffix = "text/html", "html"
        else:
            text = report_builder.build_markdown(payload, document_name=row["filename"], generated_at=generated_at)
            media, suffix = "text/markdown", "md"
        stem = Path(row["filename"]).stem
        return {
            "content": text,
            "filename": f"{stem}_格式整改清单.{suffix}",
            "media_type": f"{media}; charset=utf-8",
        }

    def stats(self) -> dict[str, Any]:
        return db.stats()
