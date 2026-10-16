"""SQLite 存储层：文档台账、检查报告快照、规则集、修复产物。

刻意不引入 ORM：本项目表结构简单、查询固定，直接用标准库 sqlite3 更透明、更好讲。
每次操作使用独立连接（短连接），避免多线程共享连接带来的坑。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id              TEXT PRIMARY KEY,
    filename        TEXT NOT NULL,
    stored_path     TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    sha256          TEXT,
    uploaded_at     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'uploaded',
    paragraph_count INTEGER NOT NULL DEFAULT 0,
    char_count      INTEGER NOT NULL DEFAULT 0,
    heading_count   INTEGER NOT NULL DEFAULT 0,
    score           INTEGER,
    issue_summary   TEXT,
    report_json     TEXT,
    outline_json    TEXT,
    error_message   TEXT
);

CREATE TABLE IF NOT EXISTS fixed_files (
    id           TEXT PRIMARY KEY,
    document_id  TEXT NOT NULL,
    filename     TEXT NOT NULL,
    stored_path  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    actions      TEXT,
    diff_json    TEXT
);

CREATE TABLE IF NOT EXISTS rulesets (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    is_builtin INTEGER NOT NULL DEFAULT 0,
    yaml_text  TEXT NOT NULL,
    updated_at TEXT
);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    settings = get_settings()
    settings.ensure_dirs()
    conn = sqlite3.connect(str(settings.db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def _row_to_document(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for key in ("issue_summary", "report_json", "outline_json"):
        raw = data.pop(key, None)
        data[key.replace("_json", "") if key.endswith("_json") else key] = (
            json.loads(raw) if raw else None
        )
    return data


def insert_document(doc: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO documents
               (id, filename, stored_path, size_bytes, sha256, uploaded_at, status,
                paragraph_count, char_count, heading_count, error_message)
               VALUES (:id, :filename, :stored_path, :size_bytes, :sha256, :uploaded_at, :status,
                       :paragraph_count, :char_count, :heading_count, :error_message)""",
            {
                "id": doc["id"],
                "filename": doc["filename"],
                "stored_path": doc["stored_path"],
                "size_bytes": doc.get("size_bytes", 0),
                "sha256": doc.get("sha256"),
                "uploaded_at": doc.get("uploaded_at") or _now(),
                "status": doc.get("status", "uploaded"),
                "paragraph_count": doc.get("paragraph_count", 0),
                "char_count": doc.get("char_count", 0),
                "heading_count": doc.get("heading_count", 0),
                "error_message": doc.get("error_message"),
            },
        )


def save_check_result(
    doc_id: str,
    *,
    score: int | None,
    issue_summary: dict[str, int] | None,
    report: dict[str, Any] | None,
    outline: list[dict[str, Any]] | None,
    status: str = "checked",
) -> None:
    with connect() as conn:
        conn.execute(
            """UPDATE documents
               SET status = ?, score = ?, issue_summary = ?, report_json = ?, outline_json = ?
               WHERE id = ?""",
            (
                status,
                score,
                json.dumps(issue_summary, ensure_ascii=False) if issue_summary else None,
                json.dumps(report, ensure_ascii=False) if report else None,
                json.dumps(outline, ensure_ascii=False) if outline else None,
                doc_id,
            ),
        )


def find_document_by_sha(sha256: str) -> dict[str, Any] | None:
    """按内容哈希查重：同一份文件重复导入时复用已有记录，避免列表里堆满重复项。"""
    if not sha256:
        return None
    with connect() as conn:
        row = conn.execute("SELECT * FROM documents WHERE sha256 = ? LIMIT 1", (sha256,)).fetchone()
    return _row_to_document(row)


def list_documents(limit: int = 100) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM documents ORDER BY uploaded_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_document(row) for row in rows]  # type: ignore[misc]


def get_document(doc_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    return _row_to_document(row)


def delete_document(doc_id: str) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        conn.execute("DELETE FROM fixed_files WHERE document_id = ?", (doc_id,))
    return cur.rowcount > 0


def insert_fixed_file(record: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO fixed_files (id, document_id, filename, stored_path, created_at, actions, diff_json)
               VALUES (:id, :document_id, :filename, :stored_path, :created_at, :actions, :diff_json)""",
            {
                "id": record["id"],
                "document_id": record["document_id"],
                "filename": record["filename"],
                "stored_path": record["stored_path"],
                "created_at": record.get("created_at") or _now(),
                "actions": json.dumps(record.get("actions", []), ensure_ascii=False),
                "diff_json": json.dumps(record.get("diff", []), ensure_ascii=False),
            },
        )


def get_fixed_file(fixed_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM fixed_files WHERE id = ?", (fixed_id,)).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["actions"] = json.loads(data["actions"] or "[]")
    data["diff"] = json.loads(data["diff_json"] or "[]")
    return data


def list_fixed_files(doc_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM fixed_files WHERE document_id = ? ORDER BY created_at DESC", (doc_id,)
        ).fetchall()
    result = []
    for row in rows:
        data = dict(row)
        data["actions"] = json.loads(data["actions"] or "[]")
        data["diff"] = json.loads(data["diff_json"] or "[]")
        result.append(data)
    return result


def upsert_ruleset(ruleset_id: str, name: str, yaml_text: str, is_builtin: bool = False) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO rulesets (id, name, is_builtin, yaml_text, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name = excluded.name,
                                             yaml_text = excluded.yaml_text,
                                             updated_at = excluded.updated_at""",
            (ruleset_id, name, 1 if is_builtin else 0, yaml_text, _now()),
        )


def get_ruleset(ruleset_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM rulesets WHERE id = ?", (ruleset_id,)).fetchone()
    return dict(row) if row else None


def list_rulesets() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM rulesets ORDER BY is_builtin DESC, id").fetchall()
    return [dict(row) for row in rows]


def stats() -> dict[str, Any]:
    """首页统计：全部基于本机真实数据，不造数。"""
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM documents").fetchone()["c"]
        checked = conn.execute("SELECT COUNT(*) AS c FROM documents WHERE score IS NOT NULL").fetchone()["c"]
        sums = conn.execute(
            """SELECT COALESCE(SUM(json_extract(issue_summary, '$.error')), 0)   AS errors,
                      COALESCE(SUM(json_extract(issue_summary, '$.warning')), 0) AS warnings,
                      COALESCE(SUM(json_extract(issue_summary, '$.info')), 0)    AS infos
               FROM documents WHERE issue_summary IS NOT NULL"""
        ).fetchone()
        avg_row = conn.execute("SELECT AVG(score) AS s FROM documents WHERE score IS NOT NULL").fetchone()
        top_rows = conn.execute(
            "SELECT report_json FROM documents WHERE report_json IS NOT NULL ORDER BY uploaded_at DESC LIMIT 200"
        ).fetchall()

    counter: dict[str, dict[str, Any]] = {}
    for row in top_rows:
        try:
            report = json.loads(row["report_json"])
        except (TypeError, ValueError):
            continue
        for issue in report.get("issues", []):
            key = issue.get("rule_id") or issue.get("rule_name") or "unknown"
            item = counter.setdefault(
                key,
                {"rule_id": key, "rule_name": issue.get("rule_name", key), "category": issue.get("category"), "count": 0},
            )
            item["count"] += 1
    top = sorted(counter.values(), key=lambda item: item["count"], reverse=True)[:5]

    return {
        "documents": total,
        "checked_documents": checked,
        "avg_score": round(avg_row["s"], 1) if avg_row and avg_row["s"] is not None else None,
        "total_errors": sums["errors"],
        "total_warnings": sums["warnings"],
        "total_infos": sums["infos"],
        "top_issues": top,
        "db_path": str(Path(get_settings().db_path)),
    }
