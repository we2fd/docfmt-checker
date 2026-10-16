"""报告导出：Markdown 整改清单（可直接贴进群里发给同学）+ 可打印的 HTML 报告。

Markdown 里对每条问题都给出「位置 / 实际值 / 期望值 / 出处 / 怎么改」，
拿到清单的人不需要再看别的说明就能动手改——这是「整改清单」这个交付物的全部意义。
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any

SEVERITY_ORDER = ("error", "warning", "info")


def _group_issues(issues: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key in SEVERITY_ORDER}
    for issue in issues:
        grouped.setdefault(issue.get("severity", "info"), []).append(issue)
    return grouped


def _location_text(issue: dict[str, Any]) -> str:
    location = issue.get("location") or {}
    parts: list[str] = []
    if location.get("section"):
        parts.append(f"章节：{location['section']}")
    if location.get("paragraph_index") is not None:
        parts.append(f"第 {location['paragraph_index'] + 1} 段")
    if location.get("in_table"):
        parts.append("位于表格内")
    if location.get("affected_count"):
        parts.append(f"共 {location['affected_count']} 处")
    return " · ".join(parts) if parts else "全文"


def build_markdown(report: dict[str, Any], *, document_name: str = "", generated_at: str | None = None) -> str:
    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    document = report.get("document", {})
    summary = report.get("issue_summary", {})
    lines: list[str] = []

    lines.append("# 格式体检报告与整改清单")
    lines.append("")
    lines.append(f"- **受检文件**：{document_name or document.get('filename', '')}")
    lines.append(f"- **检查时间**：{generated_at}")
    lines.append(f"- **规则集**：{report.get('ruleset', {}).get('name', '')}（{report.get('ruleset', {}).get('version', '')}）")
    score = report.get("score")
    if score is not None:
        lines.append(f"- **体检分**：**{score} / 100** — {report.get('grade_label', '')}")
    lines.append(
        f"- **问题统计**：错误 {summary.get('error', 0)} · 警告 {summary.get('warning', 0)}"
        f" · 提示 {summary.get('info', 0)} · 待人工确认 {summary.get('unknown', 0)}"
    )
    lines.append(f"- **规则覆盖率**：{report.get('coverage', 0)}%（{report.get('elapsed_ms', 0)} 毫秒完成）")
    lines.append("")

    lines.append("## 一、分维度得分")
    lines.append("")
    lines.append("| 维度 | 得分 | 通过 | 未通过 | 待确认 |")
    lines.append("| --- | ---: | ---: | ---: | ---: |")
    for item in report.get("category_scores", []):
        score_text = "—" if item["score"] is None else f"{item['score']}"
        lines.append(
            f"| {item['label']} | {score_text} | {item['passed']} | {item['failed']} | {item['unknown']} |"
        )
    lines.append("")

    grouped = _group_issues(report.get("issues", []))
    titles = {"error": "二、必须修改（错误）", "warning": "三、建议修改（警告）", "info": "四、可选优化（提示）"}
    labels = {"error": "错误", "warning": "警告", "info": "提示"}
    for severity in SEVERITY_ORDER:
        items = [item for item in grouped.get(severity, []) if item.get("status") == "fail"]
        lines.append(f"## {titles[severity]}")
        lines.append("")
        if not items:
            lines.append(f"没有{labels[severity]}级别的问题。")
            lines.append("")
            continue
        for position, issue in enumerate(items, start=1):
            lines.append(f"{position}. **[{issue.get('category_label')}] {issue.get('rule_name')}** — {issue.get('message')}")
            lines.append(f"   - 位置：{_location_text(issue)}")
            if issue.get("actual") not in (None, ""):
                lines.append(f"   - 实际：{issue['actual']}")
            if issue.get("expected") not in (None, ""):
                lines.append(f"   - 期望：{issue['expected']}")
            if issue.get("source_text"):
                lines.append(f"   - 当前来源：{issue['source_text']}")
            if issue.get("suggestion"):
                lines.append(f"   - 怎么改：{issue['suggestion']}")
            if issue.get("reference"):
                lines.append(f"   - 依据：{issue['reference']}")
        lines.append("")

    unknowns = [item for item in report.get("issues", []) if item.get("status") == "unknown"]
    lines.append("## 五、待人工确认（工具无法确定，不计入扣分）")
    lines.append("")
    if not unknowns:
        lines.append("没有需要人工确认的项目。")
    else:
        for issue in unknowns:
            lines.append(f"- {issue.get('rule_name')} — {_location_text(issue)}：{issue.get('message')}")
    lines.append("")

    lines.append("## 六、说明")
    lines.append("")
    for note in report.get("notes", []):
        lines.append(f"- {note}")
    lines.append("- 本工具只检查**格式与结构规范**，不判断学术内容质量。")
    lines.append("- 「待人工确认」项目是因为格式由 Word 样式继承而来、工具无法百分之百确定，请对照学校模板核对该项。")
    lines.append("")
    return "\n".join(lines)


def build_html(report: dict[str, Any], *, document_name: str = "", generated_at: str | None = None) -> str:
    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    document = report.get("document", {})
    summary = report.get("issue_summary", {})
    score = report.get("score")
    score_text = "—" if score is None else str(score)

    rows: list[str] = []
    for item in report.get("category_scores", []):
        width = 0 if item["score"] is None else max(2, item["score"])
        value = "—" if item["score"] is None else f"{item['score']}"
        rows.append(
            f"""<tr><td>{html.escape(item['label'])}</td>
            <td><div class="bar"><span style="width:{width}%"></span></div></td>
            <td class="num">{value}</td>
            <td class="num">{item['passed']}</td><td class="num">{item['failed']}</td>
            <td class="num">{item['unknown']}</td></tr>"""
        )

    issue_blocks: list[str] = []
    for severity, title in (("error", "必须修改"), ("warning", "建议修改"), ("info", "可选优化")):
        items = [i for i in report.get("issues", []) if i.get("severity") == severity and i.get("status") == "fail"]
        issue_blocks.append(f'<h2>{title}（{len(items)}）</h2>')
        if not items:
            issue_blocks.append('<p class="muted">没有该级别的问题。</p>')
            continue
        issue_blocks.append('<table class="issues"><thead><tr><th style="width:22%">规则</th>'
                            '<th style="width:30%">问题</th><th style="width:20%">位置</th>'
                            '<th style="width:28%">怎么改</th></tr></thead><tbody>')
        for issue in items:
            issue_blocks.append(
                f"<tr><td><b>{html.escape(str(issue.get('rule_name')))}</b>"
                f"<div class=\"muted\">{html.escape(str(issue.get('category_label')))}</div></td>"
                f"<td>{html.escape(str(issue.get('message')))}"
                f"<div class=\"muted\">实际：{html.escape(str(issue.get('actual') or '—'))}"
                f" ／ 期望：{html.escape(str(issue.get('expected') or '—'))}</div></td>"
                f"<td>{html.escape(_location_text(issue))}</td>"
                f"<td>{html.escape(str(issue.get('suggestion') or '—'))}</td></tr>"
            )
        issue_blocks.append("</tbody></table>")

    unknowns = [i for i in report.get("issues", []) if i.get("status") == "unknown"]
    unknown_block = (
        "<ul>" + "".join(
            f"<li>{html.escape(str(i.get('rule_name')))} — {html.escape(_location_text(i))}：{html.escape(str(i.get('message')))}</li>"
            for i in unknowns
        ) + "</ul>"
    ) if unknowns else '<p class="muted">没有需要人工确认的项目。</p>'

    notes = "".join(f"<li>{html.escape(str(note))}</li>" for note in report.get("notes", []))

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>格式体检报告 · {html.escape(document_name or document.get('filename', ''))}</title>
<style>
  body {{ font-family: "Microsoft YaHei", "PingFang SC", system-ui, sans-serif; color: #2C2C2A; margin: 0; padding: 32px; background: #FFFFFF; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  h2 {{ font-size: 16px; margin: 28px 0 10px; padding-bottom: 6px; border-bottom: 1px solid #E5E3DC; }}
  .meta {{ color: #5F5E5A; font-size: 13px; line-height: 1.9; }}
  .score-card {{ display: flex; align-items: center; gap: 24px; border: 1px solid #E5E3DC; border-radius: 12px; padding: 18px 22px; margin: 18px 0 6px; }}
  .score-value {{ font-size: 40px; font-weight: 500; }}
  .score-label {{ font-size: 13px; color: #5F5E5A; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  th, td {{ border: 1px solid #E5E3DC; padding: 8px 10px; text-align: left; vertical-align: top; }}
  th {{ background: #F7F6F2; font-weight: 500; }}
  td.num {{ text-align: right; }}
  .bar {{ background: #F1EFE8; border-radius: 4px; height: 8px; overflow: hidden; }}
  .bar span {{ display: block; height: 8px; background: #378ADD; }}
  .muted {{ color: #888780; font-size: 12px; }}
  ul {{ font-size: 13px; line-height: 1.8; padding-left: 20px; }}
  @media print {{ body {{ padding: 0; }} .score-card {{ break-inside: avoid; }} table {{ break-inside: auto; }} tr {{ break-inside: avoid; }} }}
</style>
</head>
<body>
  <h1>格式体检报告</h1>
  <div class="meta">
    受检文件：{html.escape(document_name or document.get('filename', ''))}<br>
    检查时间：{html.escape(generated_at)}　规则集：{html.escape(str(report.get('ruleset', {}).get('name', '')))}
    　覆盖率：{report.get('coverage', 0)}%　耗时：{report.get('elapsed_ms', 0)} 毫秒
  </div>
  <div class="score-card">
    <div>
      <div class="score-value" style="color:{report.get('grade_color', '#2C2C2A')}">{score_text}</div>
      <div class="score-label">体检分 / 100</div>
    </div>
    <div style="flex:1">
      <div style="font-size:15px">{html.escape(str(report.get('grade_label', '')))}</div>
      <div class="muted">
        错误 {summary.get('error', 0)} · 警告 {summary.get('warning', 0)} · 提示 {summary.get('info', 0)}
        · 待人工确认 {summary.get('unknown', 0)}
      </div>
    </div>
  </div>

  <h2>分维度得分</h2>
  <table>
    <thead><tr><th style="width:22%">维度</th><th style="width:34%">得分</th><th>分数</th><th>通过</th><th>未通过</th><th>待确认</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>

  {''.join(issue_blocks)}

  <h2>待人工确认</h2>
  {unknown_block}

  <h2>说明</h2>
  <ul>{notes}<li>本工具只检查格式与结构规范，不判断学术内容质量。</li></ul>
</body>
</html>
"""
