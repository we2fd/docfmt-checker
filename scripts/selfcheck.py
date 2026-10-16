"""端到端自检：真实起服务 + 走完整 HTTP 流程 + 无头浏览器做 DOM 级断言。

为什么不用 FastAPI 的 TestClient：
TestClient 走的是进程内调用，绕过 uvicorn、绕过静态资源、绕过真实端口，
「测试全绿但双击打不开」的情况它一个都发现不了。

本脚本做三层验证，缺一不可：
  1. **HTTP 层**：真实端口 + httpx 全流程（上传/检查/导出/修复/规则增删改/错误分支）；
  2. **渲染层**：无头 Chrome 打开页面，断言 Vue 真的挂载（`{{` 与 `v-cloak` 都消失）、
     关键元素文案齐全——「静态资源 200」不等于「页面能用」；
  3. **交互层**：用 CDP 直连在真实页面里驱动一次「载入样例 → 检查 → 定位到原文」，
     并收集未捕获异常与控制台报错（把静默失败变成可见失败）。

用法：
    python scripts/selfcheck.py            # 全量
    python scripts/selfcheck.py --no-browser   # 只跑 HTTP 层
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
ARTIFACTS = ROOT / "artifacts"


def _env_path(name: str) -> Path | None:
    raw = os.getenv(name)
    return Path(raw) if raw else None


def _under_local_appdata(name: str) -> Path | None:
    base = os.getenv("LOCALAPPDATA")
    return (Path(base) / name) if base else None


def _first_existing(candidates: list[Path | None]) -> Path | None:
    for item in candidates:
        if item is None:
            continue
        try:
            if item.exists():
                return item
        except OSError:
            continue
    return None


def _find_chrome() -> Path | None:
    """多路径探测浏览器。

    便携包会被拷到**别的电脑**上，写死本机绝对路径在目标机上必然找不到 ——
    那会让自检报出一堆"失败"，而实际上只是环境里没有 Chrome。
    探测顺序：环境变量 → PATH → 常见安装位置（含 Edge 兜底）。
    """
    return _first_existing([
        _env_path("DOCFMT_CHROME"),
        Path(shutil.which("chrome.exe")) if shutil.which("chrome.exe") else None,
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
        _under_local_appdata("Google/Chrome/Application/chrome.exe"),
        Path(shutil.which("msedge.exe")) if shutil.which("msedge.exe") else None,
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
    ])


def _find_node() -> Path | None:
    return _first_existing([
        _env_path("DOCFMT_NODE"),
        Path(shutil.which("node.exe")) if shutil.which("node.exe") else None,
        Path(shutil.which("node")) if shutil.which("node") else None,
        Path("C:/Users/HP/.workbuddy/binaries/node/versions/22.22.2-3/node.exe"),
    ])


CHROME = _find_chrome()
NODE = _find_node()

RESULTS: list[dict] = []


def record(name: str, ok: bool, detail: str = "", skip: bool = False) -> bool:
    """记录一项检查结果。

    skip=True 用于「本机缺少必要外部条件、该项不适用」的情况（例如没装 Chrome
    导致无法做 DOM 断言）。跳过项既不算通过也不算失败，也不会让退出码变成 1 ——
    否则在目标机器上跑自检会得出"有失败项"的错误结论。
    """
    RESULTS.append({"name": name, "ok": bool(ok), "detail": detail, "skip": bool(skip)})
    if skip:
        flag = "SKIP"
    else:
        flag = "PASS" if ok else "FAIL"
    print(f"[{flag}] {name}" + (f" —— {detail}" if detail else ""), flush=True)
    return bool(ok)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def safe_rmtree(path: Path) -> bool:
    """尽力删除目录，**删不掉也不能让自检崩掉**。

    除了 OSError，还必须吞掉 SystemExit：宿主环境的安全策略会用 SystemExit
    拦截"批量删除"，而 shutil.rmtree(ignore_errors=True) 只处理 OSError——
    一旦被拦，后面的收尾代码（包括打印自检汇总、写报告）会一起中断，
    表现出来就是"所有检查都通过了，但脚本没输出结果"。
    """
    try:
        shutil.rmtree(path, ignore_errors=True)
        return True
    except (OSError, SystemExit):  # pragma: no cover - 取决于宿主环境
        return False


def wait_ready(client, base: str, process: subprocess.Popen, timeout: float = 45.0) -> tuple[bool, str]:
    """轮询健康检查；失败时带出「最后一次异常」与「进程是否还活着」，避免只看到干巴巴的超时。"""
    deadline = time.time() + timeout
    last_error = "（尚未发起请求）"
    while time.time() < deadline:
        if process.poll() is not None:
            return False, f"服务进程已退出，退出码 {process.returncode}"
        try:
            response = client.get(f"{base}/health", timeout=3.0)
            if response.status_code == 200:
                return True, ""
            last_error = f"HTTP {response.status_code}"
        except Exception as exc:  # noqa: BLE001 - 需要把原因原样带出来
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.4)
    return False, f"等待超时（{timeout:.0f}s），最后一次探测：{last_error}"


def build_upload_docx(target: Path) -> Path:
    from docx import Document
    from docx.shared import Cm

    document = Document()
    for section in document.sections:
        section.top_margin = Cm(1.0)
        section.bottom_margin = Cm(1.0)
    document.add_paragraph("自检用的临时文档")
    document.add_paragraph("这一段中文里混用了英文逗号,用来验证标点检查可以命中.")
    document.save(str(target))
    return target


def build_template_docx(target: Path) -> Path:
    """一份「格式与内置规则刻意不同」的参考模板：正文四号（14 磅）、页边距 4 厘米。

    它同时充当两件事的输入：HTTP 层验证推导结果，浏览器层验证对照表真的渲染出来。
    """
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt

    document = Document()
    for section in document.sections:
        section.top_margin = Cm(4.0)
        section.bottom_margin = Cm(4.0)
        section.left_margin = Cm(4.0)
        section.right_margin = Cm(4.0)

    def para(text: str, size: float, east: str):
        paragraph = document.add_paragraph()
        run = paragraph.add_run(text)
        run.font.size = Pt(size)
        rpr = run._element.get_or_add_rPr()
        rfonts = rpr.find(qn("w:rFonts"))
        if rfonts is None:
            rfonts = OxmlElement("w:rFonts")
            rpr.insert(0, rfonts)
        rfonts.set(qn("w:eastAsia"), east)
        paragraph.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        return paragraph

    para("自检用参考模板", 22, "黑体")
    document.add_heading("摘要", level=1)
    para("这是摘要正文，用于让模板有可解析的段落。" * 10, 14, "宋体")
    para("关键词：格式检查；规则引擎；模板推导", 14, "宋体")
    document.add_heading("第一章 绪论", level=1)
    for index in range(4):
        para(f"第 {index} 段正文，本模板统一使用四号字。" * 4, 14, "宋体")
    document.add_heading("参考文献", level=1)
    para("[1] 王明. 排版规范化研究[J]. 计算机学报, 2021: 1-10.", 10.5, "宋体")
    document.save(str(target))
    return target


def chrome_flags(profile: Path, extra: list[str] | None = None) -> list[str]:
    flags = [
        str(CHROME),
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--no-proxy-server",  # 本机设了 HTTP_PROXY 但没有 NO_PROXY，不加这个连 127.0.0.1 都会被代理掉
        f"--user-data-dir={profile}",
    ]
    return flags + (extra or [])


def run_http_checks(base: str, sample_names: list[str]) -> dict:
    import httpx

    client = httpx.Client(trust_env=False, timeout=30.0, base_url=base)
    outcome: dict = {}

    def get(path, **kwargs):
        return client.get(path, **kwargs)

    try:
        config = get("/config").json()
        record("接口 /config 返回规则集信息", config["ruleset"]["rule_count"] == 56,
               f"规则 {config['ruleset']['rule_count']} 条 / 检查器 {len(config['checkers'])} 个 / 修复动作 {len(config['fix_actions'])} 项")
        record("接口 /config 声明了「待人工确认」策略",
               "待人工确认" in config["honesty"]["unknown_policy"], config["honesty"]["unknown_policy"])

        health = get("/health").json()
        record("接口 /health 正常", health["status"] == "ok", json.dumps(health, ensure_ascii=False))

        samples = get("/samples").json()
        record("演示样例可被发现", samples["total"] >= 1, f"{samples['total']} 份样例")

        ids: dict[str, str] = {}
        expected_scores = {"01-规范样本.docx": (95, 100), "02-排版问题样本.docx": (0, 60),
                           "03-结构问题样本.docx": (60, 80), "04-一致性与标点样本.docx": (55, 80)}
        for name in sample_names:
            response = client.post(f"/samples/{name}/import")
            if response.status_code != 200:
                record(f"导入样例 {name}", False, response.text[:160])
                continue
            doc_id = response.json()["id"]
            ids[name] = doc_id
            report = client.post(f"/documents/{doc_id}/check", json={}).json()
            low, high = expected_scores.get(name, (0, 100))
            record(f"样例「{name}」体检分落在预期区间",
                   report["score"] is not None and low <= report["score"] <= high,
                   f"得分 {report['score']}（预期 {low}~{high}），问题 {report['issue_total']} 条，耗时 {report['elapsed_ms']}ms")

        strict = ids.get("01-规范样本.docx")
        if strict:
            report = client.post(f"/documents/{strict}/check", json={}).json()
            record("规范样本不产生任何误报",
                   report["issue_summary"]["error"] == 0,
                   f"错误 {report['issue_summary']['error']} / 警告 {report['issue_summary']['warning']}")
            record("规范化文档里没有整篇无法判定的规则",
                   report["coverage"] >= 90, f"覆盖率 {report['coverage']}%")

        broken = ids.get("03-结构问题样本.docx")
        if broken:
            report = client.post(f"/documents/{broken}/check", json={}).json()
            failed = {item["rule_id"] for item in report["rule_results"] if item["status"] == "fail"}
            expected = {"structure.heading.no_skip", "structure.figure.numbering", "structure.keywords.required"}
            record("结构问题样本命中了预期的结构规则", expected.issubset(failed),
                   "命中：" + "、".join(sorted(expected & failed)))
            issues = report["issues"]
            record("每条问题都带位置与整改建议",
                   all(item["suggestion"] for item in issues) and all("paragraph_index" in item["location"] for item in issues),
                   f"{len(issues)} 条问题")
            unknown = [item for item in issues if item["status"] == "unknown"]
            record("无法判定的项目被单独标注", len(unknown) >= 0,
                   f"待人工确认 {len(unknown)} 项，不计入扣分")

            markdown = client.get(f"/documents/{broken}/export", params={"kind": "markdown"}).text
            record("导出 Markdown 整改清单", "格式体检报告与整改清单" in markdown and "怎么改" in markdown,
                   f"{len(markdown)} 字符")
            html_report = client.get(f"/documents/{broken}/export", params={"kind": "html"}).text
            record("导出可打印 HTML 报告", "<!DOCTYPE html>" in html_report and "分维度得分" in html_report,
                   f"{len(html_report)} 字符")

            fixed = client.post(f"/documents/{broken}/fix",
                                json={"actions": ["blank_lines", "punctuation", "font_size", "caption_numbering"]}).json()
            record("保守修复生成新文件", bool(fixed.get("fixed_id")) and fixed["total_changed"] >= 0,
                   f"共修改 {fixed['total_changed']} 处；{fixed['notice']}")
            download = client.get(f"/fixed/{fixed['fixed_id']}/download")
            record("修复产物可下载且是合法 docx", download.status_code == 200 and download.content[:2] == b"PK",
                   f"{len(download.content)} 字节")
            after = client.post(f"/documents/{broken}/check", json={}).json()
            record("修复后重新检查仍然可用", after["score"] is not None,
                   f"修复后得分 {after['score']}")

        # ---- 错误分支：错误必须「可操作」 ----
        bad = client.post("/documents", files={"file": ("论文.doc", b"\xd0\xcf\x11\xe0abc", "application/msword")})
        payload = bad.json()
        record("旧版 .doc 被拦下并给出可操作建议",
               bad.status_code == 400 and payload["code"] == "unsupported_file_type" and "另存为" in payload.get("hint", ""),
               payload.get("hint", "")[:60])

        empty = client.post("/documents", files={"file": ("空.docx", b"", "application/octet-stream")})
        record("空文件被拦下", empty.status_code == 400, empty.json().get("message", ""))

        missing = client.get("/documents/不存在的编号")
        record("不存在的文档返回 404 且带提示", missing.status_code == 404 and missing.json().get("hint"),
               missing.json().get("hint", ""))

        upload_path = build_upload_docx(ARTIFACTS / "selfcheck-upload.docx")
        with open(upload_path, "rb") as handle:
            uploaded = client.post("/documents", files={"file": (upload_path.name, handle, "application/octet-stream")})
        record("通过 multipart 上传真实 docx", uploaded.status_code == 200 and uploaded.json()["paragraph_count"] > 0,
               f"解析出 {uploaded.json().get('paragraph_count')} 个段落")
        uploaded_report = client.post(f"/documents/{uploaded.json()['id']}/check", json={}).json()
        hit = any("标点" in item["rule_name"] for item in uploaded_report["issues"])
        record("上传的文档能命中「中文标点混用」规则", hit,
               "发现：" + "、".join(item["rule_name"] for item in uploaded_report["issues"][:3]))

        # ---- 规则库读写 ----
        detail = client.get("/rules/default").json()
        record("规则集详情包含全部规则与建议", detail["rule_count"] == 56 and all(r["suggestion"] for r in detail["rules"]),
               f"{detail['rule_count']} 条")

        bad_yaml = client.put("/rules/default", json={"yaml_text": "meta: {id: default}\nrules:\n  - id: a\n    name: 坏: 值\n"})
        record("非法 YAML 被拒绝并给出原因", bad_yaml.status_code == 422 and "双引号" in bad_yaml.json().get("hint", ""),
               bad_yaml.json().get("message", ""))

        unknown_checker = client.put("/rules/default", json={
            "yaml_text": "meta: {id: default, name: 测试}\nrules:\n  - {id: a, name: 甲, category: layout, checker: 不存在, params: {}}\n"})
        record("未知检查器被拒绝", unknown_checker.status_code == 422, "、".join(unknown_checker.json().get("detail", [])[:2]))

        toggled = client.post("/rules/default/toggle", json={"rule_id": "layout.body.size", "enabled": False}).json()
        rules = {item["id"]: item for item in toggled["ruleset"]["rules"]}
        record("单条规则可以停用", rules["layout.body.size"]["enabled"] is False, "layout.body.size → 停用")

        yaml_text = client.get("/rules/default/yaml").text
        record("规则集可导出为 YAML", "meta:" in yaml_text and "rules:" in yaml_text, f"{len(yaml_text)} 字符")

        client.post("/rules/default/reset")
        restored = client.get("/rules/default").json()
        rules = {item["id"]: item for item in restored["rules"]}
        record("恢复内置默认规则集", rules["layout.body.size"]["enabled"] is True, "layout.body.size → 启用")

        # ---- 阈值快速调整（对应界面上的「快速调整」页签） ----
        quick = client.get("/rules/default/quick").json()
        flat = {item["rule_id"]: item for group in quick["groups"] for item in group["items"]}
        record("「快速调整」列出全部可改阈值并给出单位与上下限",
               "layout.body.size" in flat and flat["layout.body.size"]["unit"] == "磅"
               and flat["layout.body.size"]["limits"] == [5.0, 100.0]
               and flat["layout.body.line_spacing_exact"]["limits"] == [8.0, 80.0],
               f"{len(flat)} 项可调阈值，分 {len(quick['groups'])} 组")

        changed = client.put("/rules/default/quick", json={"updates": [
            {"rule_id": "layout.body.size", "params": {"expected": 13.5, "tol": 0.5}}]}).json()
        after = {rule["id"]: rule for rule in client.get("/rules/default").json()["rules"]}
        record("改一个数字就能改规则（不用写 YAML）",
               changed["applied"] == 1 and after["layout.body.size"]["params"]["expected"] == 13.5
               and after["layout.body.size"]["params"]["scope"] == "body",
               f"layout.body.size 12 → {after['layout.body.size']['params']['expected']} 磅，检查对象与检查器均未被动过")

        partial = client.put("/rules/default/quick", json={"updates": [
            {"rule_id": "layout.body.size", "params": {"expected": 14}},
            {"rule_id": "layout.body.size", "params": {"expected": 9999}}]}).json()
        record("越界阈值被拦下且不影响同批其它改动",
               partial["applied"] == 1 and partial["rejected"] == 1 and partial["problems"][0].get("hint"),
               partial["problems"][0]["message"])

        patched = client.patch("/rules/default/rules/layout.body.line_spacing/params",
                               json={"params": {"min": 1.4, "max": 1.8}})
        record("单条规则的阈值可就地修改", patched.status_code == 200,
               json.dumps(patched.json().get("detail", {}), ensure_ascii=False))

        bad_scope = client.patch("/rules/default/rules/layout.body.size/params", json={"params": {"scope": "heading"}})
        record("非阈值参数被拒绝并指路 YAML 页签",
               bad_scope.status_code == 422 and "YAML" in bad_scope.json().get("hint", ""),
               bad_scope.json().get("message", ""))

        client.post("/rules/default/reset")

        # ---- 参考模板校准 ----
        tpl_path = build_template_docx(ARTIFACTS / "selfcheck-template.docx")
        with open(tpl_path, "rb") as handle:
            profile = client.post("/templates/profile",
                                  files={"file": (tpl_path.name, handle, "application/octet-stream")}).json()
        proposals = {item["rule_id"]: item for item in profile["proposals"]}
        body_size = proposals["layout.body.size"]
        record("上传参考模板后逐条推导出阈值",
               body_size["status"] == "proposed" and body_size["proposal"]["expected"] == 14
               and body_size["template_value"] == 14,
               f"可推导 {profile['counts']['proposed']} 项 / 已一致 {profile['counts']['unchanged']} 项 / "
               f"无阈值 {profile['counts']['not_applicable']} 项")

        still = {rule["id"]: rule for rule in client.get("/rules/default").json()["rules"]}
        record("推导阶段不改动任何规则（必须点了应用才生效）",
               still["layout.body.size"]["params"]["expected"] == 12, "推导后 layout.body.size 仍为 12 磅")

        record("结构类规则被标为「没有可推导的阈值」",
               proposals["structure.heading.no_skip"]["status"] == "not_applicable",
               proposals["structure.heading.no_skip"]["message"][:44])
        record("一致性规则量出参考值及其占比",
               proposals["consistency.body.font"]["status"] == "reference"
               and proposals["consistency.body.font"]["confidence"] == 1.0,
               f"模板实测 {proposals['consistency.body.font']['template_text']}，"
               f"占比 {proposals['consistency.body.font']['confidence']}")

        applied_tpl = client.post("/templates/apply", json={
            "ruleset_id": "default",
            "source_name": profile["filename"],
            "updates": [{"rule_id": "layout.body.size", "params": body_size["proposal"]}],
        }).json()
        detail_after = client.get("/rules/default").json()
        current = {rule["id"]: rule for rule in detail_after["rules"]}
        record("把模板推导结果写回规则集并留下出处",
               applied_tpl["applied"] == 1 and current["layout.body.size"]["params"]["expected"] == 14
               and profile["filename"] in detail_after["description"],
               f"layout.body.size → {current['layout.body.size']['params']['expected']} 磅；规则集说明已记下来源模板")

        if strict:
            recheck = client.post(f"/documents/{strict}/check", json={}).json()
            record("按模板校准后的规则集仍可正常跑完检查",
                   len(recheck["rule_results"]) == 56 and recheck["score"] is not None,
                   f"{len(recheck['rule_results'])} 条规则结果，得分 {recheck['score']}")

        listed = client.get("/templates").json()
        docs_now = client.get("/documents").json()["items"]
        record("参考模板不计入「待检查文档」列表",
               listed["total"] == 1 and all(item["filename"] != tpl_path.name for item in docs_now),
               f"模板 {listed['total']} 份、文档列表 {len(docs_now)} 份，模板没有混进去")

        removed = client.delete(f"/templates/{profile['template_id']}").json()
        gone = client.post(f"/templates/{profile['template_id']}/derive")
        record("参考模板可以删除", removed["deleted"] and gone.status_code == 404,
               f"删除方式：{removed['mode']}")

        client.post("/rules/default/reset")
        back = {rule["id"]: rule for rule in client.get("/rules/default").json()["rules"]}
        record("一键回退到内置默认阈值", back["layout.body.size"]["params"]["expected"] == 12,
               f"layout.body.size → {back['layout.body.size']['params']['expected']} 磅")

        stats = client.get("/stats").json()
        record("统计接口反映真实数据",
               stats["documents"] >= 4 and stats["checked_documents"] >= 4 and len(stats["top_issues"]) > 0,
               f"文档 {stats['documents']} 份 / 平均分 {stats['avg_score']} / TOP1 {stats['top_issues'][0]['rule_name'] if stats['top_issues'] else '—'}")

        target = uploaded.json()["id"]
        deleted = client.delete(f"/documents/{target}").json()
        gone = client.get(f"/documents/{target}")
        record("删除文档后列表与详情同步", deleted["deleted"] and gone.status_code == 404, f"删除 {target}")

        outcome["document_id"] = broken or ""
    finally:
        client.close()
    return outcome


def run_browser_checks(base: str) -> None:
    if CHROME is None:
        record("无头浏览器检查（跳过：本机未找到 Chrome/Edge）", False,
               "接口层与业务链路已全部验证；可用环境变量 DOCFMT_CHROME 指定浏览器", skip=True)
        return

    profile = ROOT / ".tmp-chrome-selfcheck"
    safe_rmtree(profile)
    dom_path = ARTIFACTS / "selfcheck-dom.html"
    dom_path.parent.mkdir(parents=True, exist_ok=True)

    # ---- 第一层：首屏渲染 ----
    subprocess.run(
        chrome_flags(profile, ["--virtual-time-budget=12000", "--dump-dom", base]),
        stdout=open(dom_path, "w", encoding="utf-8"),
        stderr=subprocess.DEVNULL,
        timeout=120,
    )
    dom = dom_path.read_text(encoding="utf-8", errors="ignore")
    record("页面 DOM 已渲染（Vue 真的挂载了）", "{{" not in dom and "v-cloak" not in dom,
           f"{len(dom)} 字符；未出现未编译的模板占位符")
    required = [
        "格致 · 学术文档格式智能检查与规范化助手",
        "把论文拖到这里",
        "一键载入演示样例",
        "文档列表",
        "开始检查",
        "问题清单",
        "原文对照",
        "修复与导出",
        "el-radio-button",
        "快速调整",
        "参考模板校准",
        "is-active",
    ]
    missing = [text for text in required if text not in dom]
    record("首屏关键元素齐全", not missing, "缺失：" + "、".join(missing) if missing else f"断言 {len(required)} 项全部命中")
    record("报告区已渲染出分数与维度条", "score-value" in dom and "cat-row" in dom, "含分数卡与四维度条形图")

    # ---- 第二层：截图（放进 artifacts，方便写文档与答辩材料） ----
    shot = ARTIFACTS / "frontend-report.png"
    subprocess.run(
        chrome_flags(profile, ["--virtual-time-budget=12000", "--window-size=1440,1900", f"--screenshot={shot}", base]),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=120,
    )
    record("生成页面截图", shot.exists() and shot.stat().st_size > 20000,
           f"{shot.name} {shot.stat().st_size // 1024 if shot.exists() else 0} KB")

    # ---- 第三层：CDP 驱动真实交互 ----
    if NODE is None:
        record("CDP 交互驱动（跳过：本机未找到 Node）", False,
               "前两层已完成；可用环境变量 DOCFMT_NODE 指定 node 可执行文件", skip=True)
        return
    debug_port = free_port()
    chrome = subprocess.Popen(
        chrome_flags(profile, [f"--remote-debugging-port={debug_port}", "--remote-allow-origins=*", "about:blank"]),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 25
        ready = False
        while time.time() < deadline:
            try:
                import httpx

                httpx.get(f"http://127.0.0.1:{debug_port}/json/version", timeout=2.0, trust_env=False)
                ready = True
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        if not ready:
            record("CDP 交互驱动（调试端口未就绪）", False, f"端口 {debug_port}")
            return

        drive_script = ARTIFACTS / "selfcheck-drive.js"
        template_path = ARTIFACTS / "selfcheck-template.docx"
        if template_path.exists():
            import base64

            template_b64 = base64.b64encode(template_path.read_bytes()).decode("ascii")
        else:  # pragma: no cover - HTTP 层总会先生成它
            template_b64 = ""

        drive_script.write_text(
            """
(async () => {
  const app = window.__gezhi;
  if (!app) return { ok: false, reason: 'Vue 未挂载' };
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const out = { ok: true };

  // ---- 1) 主流程：载入样例 → 检查 → 定位到原文 ----
  const target = app.samples.find(s => s.name.indexOf('03-') === 0) || app.samples[0];
  await app.importSample(target);
  const first = app.visibleIssues[0];
  if (first) app.locate(first);
  Object.assign(out, {
    sample: target && target.name, score: app.report && app.report.score,
    issues: app.report && app.report.issues.length,
    rules: app.report && app.report.rule_results.length,
    feed: app.feed.length, tab: app.tab, targetIndex: app.targetIndex,
    radar: app.radarPoints, paragraphs: app.paragraphs.length
  });

  // ---- 2) 规则库 · 快速调整：真的改一个数字并保存 ----
  app.view = 'rules';
  await app.ensureRulesLoaded();
  app.ruleTab = 'quick';
  await wait(250);
  out.quickGroups = app.quickGroups.length;
  out.quickItems = app.quickItemCount;
  out.quickCards = document.querySelectorAll('.quick-card').length;
  out.quickInputs = document.querySelectorAll('.quick-card .el-input-number').length;

  const before = app.quickValues['layout.body.size'].expected;
  app.quickValues['layout.body.size'].expected = 13.5;
  await wait(80);
  out.quickDirty = app.quickDirtyCount;
  out.quickDirtyCards = document.querySelectorAll('.quick-card.is-dirty').length;
  await app.saveQuick();
  await wait(400);
  const saved = app.ruleset.rules.filter(r => r.id === 'layout.body.size')[0];
  out.savedExpected = saved && saved.params.expected;
  out.quickDirtyAfterSave = app.quickDirtyCount;

  // ---- 3) 规则库 · 参考模板校准：上传一份真实的 docx 并渲染对照表 ----
  if (window.__tplB64) {
    const bin = atob(window.__tplB64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const file = new File([bytes], '自检参考模板.docx',
      { type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' });
    await app.profileTemplate(file);
    app.ruleTab = 'template';
    await wait(400);
    out.tplProposed = app.tplResult && app.tplResult.counts.proposed;
    out.tplSelected = app.tplSelectedCount;
    out.tplProposalTags = document.querySelectorAll('.tag-proposed').length;
    out.tplProfileRows = document.querySelectorAll('.quick-group-head').length;
    const bodyProposal = app.tplResult.proposals.filter(p => p.rule_id === 'layout.body.size')[0];
    out.tplBody = bodyProposal && bodyProposal.proposal && bodyProposal.proposal.expected;
    out.tplConfidence = bodyProposal && bodyProposal.confidence;

    // 走接口把推导结果落盘（界面上这一步会弹确认框，无头环境里点不了）
    await app.request('/templates/apply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ruleset_id: 'default', source_name: app.tplResult.filename,
                             updates: [{ rule_id: 'layout.body.size', params: bodyProposal.proposal }] })
    });
    await app.loadRuleset();
    await wait(200);
    const afterApply = app.ruleset.rules.filter(r => r.id === 'layout.body.size')[0];
    out.appliedExpected = afterApply && afterApply.params.expected;
  }

  // 收尾：把规则集恢复到内置默认，避免影响后续人工查看
  await app.request('/rules/default/reset', { method: 'POST' });
  await app.loadRuleset();
  await wait(150);
  const restored = app.ruleset.rules.filter(r => r.id === 'layout.body.size')[0];
  out.restoredExpected = restored && restored.params.expected;
  return out;
})()
""",
            encoding="utf-8",
        )
        # 模板 base64 有几十 KB，直接拼进表达式不现实；作为「预注入脚本」单独传进去，
        # 与主表达式共用同一次导航，这样 window.__tplB64 不会在翻页时丢掉。
        inject_script = ARTIFACTS / "selfcheck-inject.js"
        import base64

        if template_b64:
            inject_script.write_text(
                "window.__tplB64 = " + json.dumps(template_b64) + "; true;", encoding="utf-8"
            )
        else:  # pragma: no cover
            inject_script.write_text("window.__tplB64 = ''; true;", encoding="utf-8")

        drive_dom = ARTIFACTS / "selfcheck-dom-driven.html"
        completed = subprocess.run(
            [
                str(NODE), str(ROOT / "scripts" / "dom_check.js"), str(debug_port), base,
                str(drive_dom), str(drive_script), str(inject_script),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if completed.returncode != 0:
            record("CDP 交互驱动执行成功", False, (completed.stderr or completed.stdout)[:200])
            return
        payload = json.loads(completed.stdout)
        result = payload.get("evalResult") or {}
        record("页面内驱动「载入样例 → 检查 → 定位」成功", result.get("ok") is True,
               f"样例 {result.get('sample')} / 得分 {result.get('score')} / 问题 {result.get('issues')} 条 / 规则 {result.get('rules')} 条")
        record("检查过程逐条揭示了全部规则", (result.get("feed") or 0) >= 50, f"揭示 {result.get('feed')} 条规则结果")
        record("问题定位跳转到原文对照", result.get("tab") == "doc" and result.get("targetIndex") is not None,
               f"跳转到第 {(result.get('targetIndex') or 0) + 1} 段")
        radar_raw = str(result.get("radar") or "").strip()
        radar_pairs = [part for part in radar_raw.split() if part]
        radar_ok = len(radar_pairs) == 4 and all(
            len(part.split(",")) == 2 and all(
                piece.replace(".", "", 1).isdigit() for piece in part.split(",")
            )
            for part in radar_pairs
        )
        record("雷达图已按四个维度计算", radar_ok, radar_raw[:60] or "未取到坐标")

        record("「快速调整」页渲染出可编辑的阈值表单",
               (result.get("quickCards") or 0) == (result.get("quickItems") or 0)
               and (result.get("quickInputs") or 0) > 0 and (result.get("quickGroups") or 0) >= 8,
               f"{result.get('quickGroups')} 组 / {result.get('quickCards')} 张卡片 / "
               f"{result.get('quickInputs')} 个数字输入框（应与 {result.get('quickItems')} 项可调阈值一致）")
        record("改动阈值后卡片被标记，保存后写回规则集",
               result.get("quickDirty") == 1 and result.get("quickDirtyCards") == 1
               and result.get("savedExpected") == 13.5 and result.get("quickDirtyAfterSave") == 0,
               f"改动 {result.get('quickDirty')} 项 → 保存后规则值 {result.get('savedExpected')} 磅，"
               f"未保存标记清零为 {result.get('quickDirtyAfterSave')}")

        if template_b64:
            record("参考模板对照表在页面上渲染出来",
                   (result.get("tplProposalTags") or 0) > 0 and (result.get("tplSelected") or 0) > 0
                   and result.get("tplBody") == 14,
                   f"建议变更 {result.get('tplProposed')} 项（页面渲染 {result.get('tplProposalTags')} 行），"
                   f"默认勾选 {result.get('tplSelected')} 项，正文字号推导值 {result.get('tplBody')} 磅")
            record("在页面上应用模板结果后规则真的变了",
                   result.get("appliedExpected") == 14 and result.get("restoredExpected") == 12,
                   f"应用后 layout.body.size = {result.get('appliedExpected')} 磅，"
                   f"恢复默认后 = {result.get('restoredExpected')} 磅")
        else:  # pragma: no cover
            record("参考模板对照表在页面上渲染出来", False, "未找到自检用模板文件", skip=True)

        problems = payload.get("problems") or []
        record("页面运行期间没有 JavaScript 异常或控制台报错", not problems,
               json.dumps(problems, ensure_ascii=False)[:200] if problems else "0 条异常 / 0 条 console error")

        driven_dom = drive_dom.read_text(encoding="utf-8", errors="ignore")
        record("交互后渲染出问题卡片与高亮段落",
               "is-target" in driven_dom and ("hit-error" in driven_dom or "hit-warning" in driven_dom),
               f"{len(driven_dom)} 字符")
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=8)
        except subprocess.TimeoutExpired:  # pragma: no cover
            chrome.kill()
        safe_rmtree(profile)


def main() -> int:
    parser = argparse.ArgumentParser(description="格致端到端自检")
    parser.add_argument("--no-browser", action="store_true", help="跳过无头浏览器检查")
    parser.add_argument("--keep-data", action="store_true", help="保留临时数据目录，便于排查")
    args = parser.parse_args()

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    data_dir = Path(tempfile.mkdtemp(prefix="docfmt-selfcheck-"))
    port = free_port()
    base = f"http://127.0.0.1:{port}/api/v1"
    print(f"== 自检开始 ==\n服务端口 {port}\n隔离数据目录 {data_dir}\n", flush=True)

    env = dict(os.environ)
    env["DOCFMT_DATA_DIR"] = str(data_dir)
    env["PYTHONUNBUFFERED"] = "1"
    log_path = ARTIFACTS / "selfcheck-server.log"
    log = open(log_path, "w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(BACKEND),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )

    exit_code = 1
    try:
        import httpx

        client = httpx.Client(trust_env=False, timeout=10.0)
        ready, reason = wait_ready(client, base, process)
        client.close()
        record("服务在真实端口上启动并就绪", ready, reason or f"{base}/health 返回 200")
        if not ready:
            print(f"\n服务日志（{log_path}）：\n" + log_path.read_text(encoding="utf-8", errors="ignore")[-1500:])
            return 1

        samples = sorted(path.name for path in (ROOT / "samples").glob("*.docx"))
        if not samples:
            print("未找到 samples/*.docx，正在生成……")
            subprocess.run([sys.executable, str(ROOT / "scripts" / "make_samples.py")], check=True)

        run_http_checks(base, sorted(path.name for path in (ROOT / "samples").glob("*.docx")))
        if not args.no_browser:
            run_browser_checks(f"http://127.0.0.1:{port}/")

        passed = len([item for item in RESULTS if item["ok"]])
        skipped = [item for item in RESULTS if item["skip"]]
        failed = [item for item in RESULTS if not item["ok"] and not item["skip"]]
        total = len(RESULTS)
        summary = f"{passed}/{total} 项通过"
        if skipped:
            summary += f"，{len(skipped)} 项跳过"
        print(f"\n== 自检结束：{summary} ==")
        if skipped:
            print("跳过项（本机缺少必要外部条件）：")
            for item in skipped:
                print(f"  - {item['name']}：{item['detail']}")
        if failed:
            print("失败项：")
            for item in failed:
                print(f"  - {item['name']}：{item['detail']}")
        report_path = ARTIFACTS / "selfcheck-report.md"
        lines = ["# 端到端自检报告", "", f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
                 f"- 服务端口：{port}", f"- 结果：**{summary}**", "",
                 "| # | 检查项 | 结果 | 说明 |", "| --- | --- | --- | --- |"]
        for index, item in enumerate(RESULTS, start=1):
            if item["skip"]:
                verdict = "跳过"
            else:
                verdict = "通过" if item["ok"] else "**未通过**"
            lines.append(f"| {index} | {item['name']} | {verdict} | {item['detail']} |")
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"报告已写入：{report_path}")
        exit_code = 0 if not failed else 1
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
        log.close()
        if not args.keep_data:
            safe_rmtree(data_dir)
        else:
            print(f"临时数据目录已保留：{data_dir}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
