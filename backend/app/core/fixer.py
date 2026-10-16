"""保守修复（M5）：只做「绝不会改坏论文」的四件事。

**为什么不全自动修复？**（答辩必讲）
Word 的格式是继承出来的，一个段落看起来是「宋体小四」，可能是直接格式、可能是样式、
也可能是文档默认值带来的。全自动改写会破坏用户的样式体系，甚至改乱版式。
所以本项目选择：只做四种低风险操作，且**一律生成新文件、绝不覆盖原文件**。

四种操作：
1. blank_lines      连续空段落压缩为最多 1 个
2. punctuation      中文语句里混用的英文标点改全角
3. font_size        把「已显式设置但偏离多数派」的正文字号统一回多数派
4. caption_numbering 图表题注编号重排为连续序号
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from docx.shared import Pt

from .docmodel import DocumentModel, ParagraphInfo

ACTIONS: dict[str, dict[str, Any]] = {
    "blank_lines": {
        "id": "blank_lines",
        "label": "压缩连续空行",
        "description": "把 2 个以上的连续空段落压缩为 1 个。空行既不承载内容，也常导致分页错乱。",
        "risk": "低",
        "default_on": True,
    },
    "punctuation": {
        "id": "punctuation",
        "label": "统一中英文标点",
        "description": "把中文语句中混用的英文半角标点（, . ; : ? !）改为全角。只处理中文占比高的段落，跳过参考文献与表格。",
        "risk": "低",
        "default_on": True,
    },
    "font_size": {
        "id": "font_size",
        "label": "统一正文字号",
        "description": "只调整「已经显式设置过、且与全文多数派不一致」的正文字号，不会给原本继承样式的段落新增直接格式。",
        "risk": "中",
        "default_on": True,
    },
    "caption_numbering": {
        "id": "caption_numbering",
        "label": "重排图表编号",
        "description": "把图题注、表题注的编号重排为连续序号（保留「3-1」这类章节前缀形式），只改编号数字，不动标题文字。",
        "risk": "中",
        "default_on": True,
    },
}

_PUNCT_MAP = {",": "，", ".": "。", ";": "；", ":": "：", "?": "？", "!": "！"}


def available_actions() -> list[dict[str, Any]]:
    return list(ACTIONS.values())


# ---------------------------------------------------------------- 工具


def _is_blank_paragraph(paragraph) -> bool:
    """真空段落：既没有可见文字，也不包含图片/图形。"""
    element = paragraph._element
    for tag in ("drawing", "pict", "object"):
        if element.findall(f".//{{http://schemas.openxmlformats.org/wordprocessingml/2006/main}}{tag}"):
            return False
    return not paragraph.text.strip()


def _fix_punctuation(text: str) -> tuple[str, int]:
    count = 0

    def repl_after_cjk(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return match.group(1) + _PUNCT_MAP[match.group(2)]

    def repl_before_cjk(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return _PUNCT_MAP[match.group(1)] + match.group(2)

    def repl_dot(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return match.group(1) + "。"

    fixed = re.sub(r"([\u4e00-\u9fff])\s*([,;:?!])", repl_after_cjk, text)
    fixed = re.sub(r"([\u4e00-\u9fff])\s*\.(?![0-9])", repl_dot, fixed)
    fixed = re.sub(r"([,;:?!])([\u4e00-\u9fff])", repl_before_cjk, fixed)
    return fixed, count


# ---------------------------------------------------------------- 各项修复


def fix_blank_lines(model: DocumentModel) -> dict[str, Any]:
    removed = 0
    samples: list[str] = []
    streak: list[ParagraphInfo] = []

    def flush() -> None:
        nonlocal removed, streak
        while len(streak) > 1:
            victim = streak.pop()
            element = victim.docx_paragraph._element
            if element.getparent() is not None:
                element.getparent().remove(element)
                removed += 1
        streak = []

    for para in model.paragraphs:
        if para.in_table:
            continue
        if para.char_count() == 0 and _is_blank_paragraph(para.docx_paragraph):
            streak.append(para)
            continue
        if len(streak) > 1:
            samples.append(f"第 {streak[0].index + 1} 段起连续 {len(streak)} 个空段落")
        flush()
    if len(streak) > 1:
        samples.append(f"第 {streak[0].index + 1} 段起连续 {len(streak)} 个空段落")
    flush()
    return {"action": "blank_lines", "changed": removed, "samples": samples[:5]}


def fix_punctuation(model: DocumentModel) -> dict[str, Any]:
    changed = 0
    samples: list[str] = []
    for para in model.paragraphs:
        if para.in_table or para.in_references:
            continue
        if para.kind in ("figure_caption", "table_caption", "toc", "empty"):
            continue
        if len(re.findall(r"[\u4e00-\u9fff]", para.text)) / max(len(re.sub(r"\s", "", para.text)), 1) < 0.4:
            continue
        before = para.text
        total = 0
        for run in para.docx_paragraph.runs:
            fixed, count = _fix_punctuation(run.text)
            if count:
                run.text = fixed
                total += count
        if total:
            changed += total
            if len(samples) < 5:
                samples.append(f"第 {para.index + 1} 段修正 {total} 处：{before.strip()[:24]}…")
    return {"action": "punctuation", "changed": changed, "samples": samples}


def fix_font_size(model: DocumentModel, expected_pt: float | None = None) -> dict[str, Any]:
    """目标字号：优先用规则期望值，否则取正文段落的多数派字号。"""
    if expected_pt is None:
        counter: dict[float, int] = {}
        for para in model.body_paragraphs:
            for run in para.runs:
                if not run.text.strip():
                    continue
                item = run.props.get("font_size")
                if item and item.known and item.value:
                    counter[float(item.value)] = counter.get(float(item.value), 0) + 1
        if not counter:
            return {"action": "font_size", "changed": 0, "samples": [], "note": "未能识别正文字号，已跳过。"}
        expected_pt = max(counter, key=counter.get)

    changed = 0
    samples: list[str] = []
    for para in model.body_paragraphs:
        # 按序号对齐：model 里的 RunInfo 与 python-docx 的 Run 是同一顺序，
        # 用序号取回真正的 Run 对象，才能同时读到「直接格式」和修改 XML。
        docx_runs = para.docx_paragraph.runs
        for position, run_info in enumerate(para.runs):
            if position >= len(docx_runs):
                break
            direct = run_info.direct_props.get("font_size")
            if direct is None:
                continue  # 只改「显式设置过」的部分，不动样式继承
            if abs(float(direct) - float(expected_pt)) < 0.01:
                continue
            docx_runs[position].font.size = Pt(float(expected_pt))
            changed += 1
            if len(samples) < 5:
                samples.append(f"第 {para.index + 1} 段：{direct} 磅 → {expected_pt} 磅")
    return {"action": "font_size", "changed": changed, "samples": samples, "target_pt": expected_pt}


def fix_caption_numbering(model: DocumentModel) -> dict[str, Any]:
    counters = {"图": 0, "表": 0}
    changed = 0
    samples: list[str] = []
    for para in model.paragraphs:
        if para.kind not in ("figure_caption", "table_caption"):
            continue
        match = re.match(r"^(\s*)(图|表)\s*([\d]+(?:[-－.．][\d]+)*)", para.text)
        if not match:
            continue
        prefix = match.group(2)
        original = match.group(3)
        counters[prefix] += 1
        parts = re.split(r"([-－.．])", original)
        if len(parts) >= 3:  # 形如 3-1，保留章节号，只重排后段
            new_number = f"{parts[0]}{parts[1]}{counters[prefix]}"
        else:
            new_number = str(counters[prefix])
        if new_number == original:
            continue
        for run in para.docx_paragraph.runs:
            if original in run.text:
                run.text = run.text.replace(original, new_number, 1)
                changed += 1
                if len(samples) < 5:
                    samples.append(f"{prefix}{original} → {prefix}{new_number}")
                break
    return {"action": "caption_numbering", "changed": changed, "samples": samples}


# ---------------------------------------------------------------- 入口


def apply_fixes(model: DocumentModel, actions: list[str], *, expected_font_size: float | None = None) -> dict[str, Any]:
    """按顺序执行修复，返回逐项结果与整体 diff 摘要。"""
    results: list[dict[str, Any]] = []
    order = ["blank_lines", "punctuation", "font_size", "caption_numbering"]
    executed = [name for name in order if name in actions]
    for name in actions:
        if name not in order:
            results.append({"action": name, "changed": 0, "samples": [], "note": "未知的修复动作，已忽略。"})

    for name in executed:
        if name == "blank_lines":
            results.append(fix_blank_lines(model))
        elif name == "punctuation":
            results.append(fix_punctuation(model))
        elif name == "font_size":
            results.append(fix_font_size(model, expected_font_size))
        elif name == "caption_numbering":
            results.append(fix_caption_numbering(model))

    total_changed = sum(int(item.get("changed", 0)) for item in results)
    return {
        "results": results,
        "total_changed": total_changed,
        "actions": executed,
        "notice": "修复结果已生成为新文件，原文件未被修改。",
    }


def save_fixed(model: DocumentModel, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    model.document.save(str(target))
    return target
