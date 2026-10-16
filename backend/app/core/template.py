"""参考模板反推规则 + 规则阈值快速调整。

两个能力的共同点：都是「把外部的数字，写回规则的 params」。

设计要点（答辩可讲）：这一步同样**没有任何硬编码的规则映射表**。
每条规则自己声明了「用哪个 checker、量哪个属性（params.prop）、量哪些段落
（params.scope / params.target）」，所以「读模板 → 改规则」就是一个通用流程：

    遍历规则 → 用规则自己的 scope/prop 去文档里量一遍 → 取众数 → 写回 params

换句话说，**新增一条规则，模板推导能力自动跟着生效**，本模块一行都不用改。
同理，界面上的「快速调整」也只是把规则里本来就有的数字读出来给人改。

三条贯穿始终的原则：
1. **只提议、不擅自生效**：推导结果一律是「提案」，由用户逐条勾选后才写回；
2. **诚实披露把握**：每条提案都带「实测占比（置信度）」与样本数，
   占比低的时候用户一眼能看出「这个模板本身就不统一，别拿它当标准」；
3. **量不到就说量不到**：读不到值的规则标记为 skipped 并说明原因，
   绝不拿默认值凑数。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from ..errors import RulesetInvalid
from .docmodel import DocumentModel
from .engine import (
    ALIGN_LABELS,
    SCOPE_LABELS,
    SECTION_PROPS,
    UNIT_HINT,
    format_value,
    get_prop,
    normalize_font,
    select,
)
from .rules import SEVERITIES, Rule, Ruleset

# ---------------------------------------------------------------- 属性元数据

PROP_LABELS = {
    "font_eastasia": "中文字体",
    "font_ascii": "西文字体",
    "font_size": "字号",
    "bold": "加粗",
    "italic": "倾斜",
    "color": "字体颜色",
    "align": "对齐方式",
    "line_spacing": "行距",
    "space_before": "段前间距",
    "space_after": "段后间距",
    "first_line_indent": "首行缩进",
    "left_indent": "左缩进",
    "page_width_cm": "纸张宽度",
    "page_height_cm": "纸张高度",
    "margin_top_cm": "上页边距",
    "margin_bottom_cm": "下页边距",
    "margin_left_cm": "左页边距",
    "margin_right_cm": "右页边距",
}

# (下限, 上限, 步长, 小数位) —— 界面上数字输入的边界，同时用于参数合法性校验
NUMERIC_LIMITS: dict[str, tuple[float, float, float, int]] = {
    "font_size": (5.0, 100.0, 0.5, 1),
    "space_before": (0.0, 200.0, 1.0, 0),
    "space_after": (0.0, 200.0, 1.0, 0),
    "first_line_indent": (0.0, 5.0, 0.05, 2),
    "left_indent": (0.0, 5.0, 0.05, 2),
    "page_width_cm": (5.0, 60.0, 0.1, 1),
    "page_height_cm": (5.0, 60.0, 0.1, 1),
    "margin_top_cm": (0.0, 10.0, 0.1, 2),
    "margin_bottom_cm": (0.0, 10.0, 0.1, 2),
    "margin_left_cm": (0.0, 10.0, 0.1, 2),
    "margin_right_cm": (0.0, 10.0, 0.1, 2),
}

# 行距有两种口径，允许区间完全不同，必须分开给边界
LINE_SPACING_LIMITS: dict[str, tuple[float, float, float, int]] = {
    "multiple": (0.5, 5.0, 0.05, 2),
    "points": (8.0, 80.0, 1.0, 0),
}

NUMERIC_PROPS = set(NUMERIC_LIMITS) | {"line_spacing"}

# 注意：这里必须精确列举，不能用 startswith("font")——font_size 也是 font 开头，
# 但它是数值而不是字体名，误判会把 12 变成字符串 "12"，检查时再也比不中。
FONT_PROPS = ("font_eastasia", "font_ascii")


def is_font_prop(prop: str | None) -> bool:
    return (prop or "") in FONT_PROPS

LENGTH_LIMITS: dict[str, tuple[float, float, float, int]] = {
    "abstract": (50.0, 5000.0, 10.0, 0),
    "keywords": (1.0, 20.0, 1.0, 0),
    "reference": (1.0, 300.0, 1.0, 0),
    "title": (2.0, 100.0, 1.0, 0),
}
LENGTH_UNITS = {"abstract": "字", "keywords": "个", "reference": "条", "title": "字"}
LENGTH_TARGET_LABELS = {
    "abstract": "摘要字数",
    "keywords": "关键词个数",
    "reference": "参考文献条数",
    "title": "题目字数",
}

FONT_OPTIONS = ["宋体", "黑体", "楷体", "仿宋", "新宋体", "微软雅黑", "Times New Roman"]
ALIGN_OPTIONS = [("left", "左对齐"), ("center", "居中"), ("right", "右对齐"), ("both", "两端对齐")]

# 可以「从模板推导阈值」的检查器：只有靠具体取值判定的才能量
MEASURABLE_CHECKERS = ("prop_equals", "prop_range", "prop_in_set")

# 没有阈值、但同样能测的检查器：量出来的值不能写回规则，却是很好的自查参照
REFERENCE_CHECKERS = ("consistency",)

# 允许通过「快速调整 / 模板应用」写回的参数键（其余参数请走 YAML 页签）
ALLOWED_PATCH_KEYS = {"expected", "tol", "min", "max", "allowed"}

NO_THRESHOLD_REASON = (
    "该规则判的是「有没有 / 结构对不对」（如标题不跳级、图表编号连续、必须包含摘要），"
    "本身没有可调的数值阈值。"
)


def numeric_config(prop: str, params: dict[str, Any] | None = None) -> tuple[float, float, float, int]:
    if prop == "line_spacing":
        kind = (params or {}).get("kind_value")
        return LINE_SPACING_LIMITS.get(kind) or (0.5, 80.0, 0.05, 2)
    return NUMERIC_LIMITS.get(prop) or (0.0, 1000.0, 0.1, 2)


# ---------------------------------------------------------------- 取值统计


def _key_of(prop: str, value: Any) -> tuple[str, Any]:
    if isinstance(value, bool):
        return ("b", value)
    if isinstance(value, (int, float)):
        return ("n", round(float(value), 3))
    if is_font_prop(prop):
        return ("s", normalize_font(value) or "")
    return ("s", str(value).strip())


def _dominant(prop: str, values: list[Any]) -> tuple[Any, int, list[dict[str, Any]]]:
    """返回（众数取值, 众数出现次数, 取值分布）。并列时取首次出现的那个。"""
    counts: dict[tuple[str, Any], int] = {}
    rep: dict[tuple[str, Any], Any] = {}
    order: dict[tuple[str, Any], int] = {}
    for value in values:
        key = _key_of(prop, value)
        counts[key] = counts.get(key, 0) + 1
        rep.setdefault(key, value)
        order.setdefault(key, len(order))
    best = max(counts, key=lambda key: (counts[key], -order[key]))
    distinct = [
        {"value": rep[key], "count": counts[key], "display": format_value(prop, rep[key]), "_order": order[key]}
        for key in counts
    ]
    distinct.sort(key=lambda item: (-item["count"], item["_order"]))
    for item in distinct:
        item.pop("_order")
    return rep[best], counts[best], distinct


def _within(value: Any, low: float, high: float) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return True
    return low - 1e-9 <= number <= high + 1e-9


def _value_same(prop: str, left: Any, right: Any) -> bool:
    if isinstance(left, list) or isinstance(right, list):
        if not (isinstance(left, list) and isinstance(right, list)):
            return False
        return sorted(str(item) for item in left) == sorted(str(item) for item in right)
    if isinstance(left, bool) or isinstance(right, bool):
        return bool(left) == bool(right)
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) < 1e-6
    if is_font_prop(prop):
        return normalize_font(left) == normalize_font(right)
    return str(left).strip() == str(right).strip()


# ---------------------------------------------------------------- 从文档测量


def measure(doc: DocumentModel, rule: Rule) -> dict[str, Any]:
    """按规则自己的声明，从文档里量出该属性的全部取值。

    这里刻意复刻了检查器的取数口径（例如行距的 kind_prop 过滤），
    否则「模板推导出来的值」和「检查时实际比对的子集」会不一致。
    """
    params = rule.params or {}
    if rule.checker not in MEASURABLE_CHECKERS and rule.checker not in REFERENCE_CHECKERS:
        return {"values": [], "reason": NO_THRESHOLD_REASON, "derivable": False}
    prop = params.get("prop")
    if not prop:
        return {"values": [], "reason": "该规则没有声明要测量的属性（params.prop）。", "derivable": True}

    if params.get("target") == "section" or prop in SECTION_PROPS:
        values = [getattr(section, prop, None) for section in doc.sections]
        values = [value for value in values if value is not None]
        return {
            "values": values,
            "reason": "" if values else "模板没有读到页面设置信息（可能不含任何节）。",
            "derivable": True,
        }

    scope = params.get("scope", "body")
    skip_empty = params.get("skip_empty", True)
    kind_prop = params.get("kind_prop")
    kind_value = params.get("kind_value")
    values: list[Any] = []
    skipped_by_kind = 0
    for para in select(doc, scope):
        if skip_empty and para.char_count() == 0:
            continue
        if kind_prop and para.value(kind_prop) not in (None, kind_value):
            skipped_by_kind += 1
            continue
        value = get_prop(para, prop)
        if value is None:
            continue
        values.append(value)

    reason = ""
    if not values:
        prop_label = PROP_LABELS.get(prop, prop)
        if skipped_by_kind:
            reason = (
                f"模板里有 {skipped_by_kind} 个段落使用了另一种口径的{prop_label}，"
                "本条规则对这份模板不适用。"
            )
        else:
            reason = (
                f"模板的「{SCOPE_LABELS.get(scope, scope)}」中没有读到{prop_label}"
                "（可能这些段落没有显式设置该格式，全部继承自样式默认值）。"
            )
    return {"values": values, "reason": reason, "derivable": True}


# ---------------------------------------------------------------- 提案生成


def _auto_tol(prop: str, value: float) -> float:
    """自动容差：取「5% 相对量」与「单位相关的下限」中的较大者，并封顶。

    封顶很重要——A4 纸宽 21 厘米若按 5% 算就是 ±1.05 厘米，
    那等于允许整页跑偏，规则会失去意义。
    """
    base = 0.2 if (prop or "").endswith("_cm") else 0.1
    return round(min(max(abs(value) * 0.05, base), 0.5), 2)


def _propose_equals(prop: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"expected": bool(value)}
    if isinstance(value, (int, float)):
        return {"expected": round(float(value), 2), "tol": _auto_tol(prop, float(value))}
    if is_font_prop(prop):
        return {"expected": normalize_font(value) or str(value)}
    return {"expected": str(value).strip()}


def _propose_range(prop: str, value: Any, params: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    low, high, _, _ = numeric_config(prop, params)
    band = max(round(abs(float(value)) * 0.1, 2), 0.15)
    return {
        "min": round(max(float(value) - band, low), 2),
        "max": round(min(float(value) + band, high), 2),
    }


def _propose_in_set(prop: str, value: Any) -> dict[str, Any]:
    item = normalize_font(value) if is_font_prop(prop) else str(value).strip()
    return {"allowed": [item or str(value).strip()]}


def describe_params(prop: str, params: dict[str, Any]) -> str:
    if not params:
        return "—"
    if "allowed" in params and params["allowed"]:
        return "允许：" + "、".join(str(item) for item in params["allowed"])
    if "expected" in params:
        text = format_value(prop, params["expected"])
        if params.get("tol") is not None:
            text += f"（容差 ±{params['tol']}）"
        return text
    if "min" in params or "max" in params:
        return f"{format_value(prop, params.get('min'))} ~ {format_value(prop, params.get('max'))}"
    return "—"


def _kind_note(params: dict[str, Any]) -> str:
    if params.get("kind_prop") and params.get("kind_value"):
        return f"只统计口径为「{params['kind_value']}」的段落"
    return ""


def derive_one(doc: DocumentModel, rule: Rule) -> dict[str, Any]:
    """对单条规则生成一条提案。"""
    params = rule.params or {}
    prop = params.get("prop") or ""
    scope = params.get("scope", "body")
    is_section = params.get("target") == "section" or prop in SECTION_PROPS
    current = {key: params[key] for key in ("expected", "tol", "min", "max", "allowed") if key in params}

    proposal: dict[str, Any] = {
        "rule_id": rule.id,
        "rule_name": rule.name,
        "category": rule.category,
        "category_label": rule.category_label,
        "severity": rule.severity,
        "severity_label": SEVERITIES.get(rule.severity, {}).get("label", rule.severity),
        "checker": rule.checker,
        "enabled": rule.enabled,
        "prop": prop,
        "prop_label": PROP_LABELS.get(prop) or LENGTH_TARGET_LABELS.get(params.get("target")) or (prop or "—"),
        "scope": scope,
        "scope_label": "页面设置（全部节）" if is_section else SCOPE_LABELS.get(scope, scope),
        "current": current,
        "current_text": describe_params(prop, current),
        "template_value": None,
        "template_text": "",
        "proposal": None,
        "proposal_text": "",
        "confidence": None,
        "measured": {"count": 0, "dominant_count": 0, "distinct": []},
        "status": "not_applicable",
        "applicable": False,
        "message": "",
    }

    measured = measure(doc, rule)
    values = measured["values"]
    if not values:
        proposal["status"] = "skipped" if measured.get("derivable") else "not_applicable"
        proposal["message"] = measured["reason"] or "模板中没有可测量的对象。"
        return proposal

    dominant, dominant_count, distinct = _dominant(prop, values)
    total = len(values)
    proposal["measured"] = {"count": total, "dominant_count": dominant_count, "distinct": distinct}
    proposal["template_value"] = dominant
    proposal["template_text"] = format_value(prop, dominant)
    proposal["confidence"] = round(dominant_count / total, 3)
    share = proposal["confidence"] * 100

    # 「一致性」类规则只要求全篇统一、不设阈值，量出来的值没法写回规则，
    # 但它恰恰能回答「我们学校到底用几号字」——所以照样量、照样报，只是标为参考。
    if rule.checker in REFERENCE_CHECKERS:
        proposal["status"] = "reference"
        proposal["message"] = (
            f"该规则只要求全篇统一、没有可调的阈值。模板实测 {proposal['template_text']}"
            f"（{dominant_count}/{total} 处，占比 {share:.0f}%），可作为你自查时的参照。"
        )
        return proposal

    if rule.checker == "prop_equals":
        derived = _propose_equals(prop, dominant)
    elif rule.checker == "prop_range":
        derived = _propose_range(prop, dominant, params)
    else:
        derived = _propose_in_set(prop, dominant)

    if derived is None:
        proposal["status"] = "skipped"
        proposal["message"] = f"模板实测「{proposal['template_text']}」不是可比较的数值，无法推导区间。"
        return proposal

    # 只有「主参数」（expected / min / max / allowed）真的变了才算需要应用；
    # 否则会出现「模板和规则完全一致、却提示把容差 0.5 改成 0.6」这种噪音提案。
    primary_keys = [key for key in derived if key != "tol"]
    same = all(key in current and _value_same(prop, derived[key], current[key]) for key in primary_keys)
    if same:
        proposal["status"] = "unchanged"
        proposal["proposal_text"] = "保持不变"
        proposal["message"] = (
            f"模板实测 {proposal['template_text']}（{dominant_count}/{total} 处，占比 {share:.0f}%），"
            "与当前规则一致，无需调整。"
        )
    else:
        proposal["proposal"] = derived
        proposal["proposal_text"] = describe_params(prop, derived)
        proposal["status"] = "proposed"
        proposal["applicable"] = True
        proposal["message"] = (
            f"模板实测 {proposal['template_text']}（{dominant_count}/{total} 处，占比 {share:.0f}%），"
            f"建议由「{proposal['current_text']}」改为「{proposal['proposal_text']}」。"
        )
    return proposal


def profile_template(doc: DocumentModel) -> dict[str, Any]:
    """模板的「格式画像」：与规则无关，纯展示用，帮用户核对模板是否符合预期。"""
    roles: list[dict[str, Any]] = []
    for scope, label in (
        ("title", "论文题目"),
        ("heading1", "一级标题"),
        ("heading2", "二级标题"),
        ("heading3", "三级标题"),
        ("body", "正文段落"),
        ("abstract", "摘要正文"),
        ("keywords", "关键词行"),
        ("caption", "图表题注"),
        ("reference", "参考文献条目"),
    ):
        paragraphs = [item for item in select(doc, scope) if item.char_count() > 0]
        if not paragraphs:
            continue
        cells: list[dict[str, Any]] = []
        for prop, prop_label in (
            ("font_eastasia", "中文字体"),
            ("font_ascii", "西文字体"),
            ("font_size", "字号"),
            ("line_spacing", "行距"),
            ("align", "对齐"),
            ("first_line_indent", "首行缩进"),
        ):
            values = [value for value in (get_prop(item, prop) for item in paragraphs) if value is not None]
            if not values:
                continue
            dominant, count, _ = _dominant(prop, values)
            cells.append(
                {
                    "prop": prop,
                    "label": prop_label,
                    "value": dominant,
                    "display": format_value(prop, dominant),
                    "measured": len(values),
                    "consistency": round(count / len(values), 3),
                }
            )
        roles.append({"scope": scope, "label": label, "paragraphs": len(paragraphs), "cells": cells})

    sections = [
        {
            "index": section.index,
            "page": f"{section.page_width_cm} × {section.page_height_cm} 厘米",
            "margins": (
                f"上 {section.margin_top_cm} / 下 {section.margin_bottom_cm} / "
                f"左 {section.margin_left_cm} / 右 {section.margin_right_cm} 厘米"
            ),
            "has_page_number": section.has_page_number,
        }
        for section in doc.sections
    ]
    return {"summary": doc.summary(), "roles": roles, "sections": sections}


def derive_proposals(ruleset: Ruleset, doc: DocumentModel) -> dict[str, Any]:
    proposals = [derive_one(doc, rule) for rule in ruleset.rules]
    counts = {
        "total": len(proposals),
        "proposed": len([item for item in proposals if item["status"] == "proposed"]),
        "unchanged": len([item for item in proposals if item["status"] == "unchanged"]),
        "reference": len([item for item in proposals if item["status"] == "reference"]),
        "skipped": len([item for item in proposals if item["status"] == "skipped"]),
        "not_applicable": len([item for item in proposals if item["status"] == "not_applicable"]),
    }
    return {
        "ruleset": ruleset.to_dict(include_rules=False),
        "profile": profile_template(doc),
        "proposals": proposals,
        "counts": counts,
    }


# ---------------------------------------------------------------- 写回规则


def _to_float(rule: Rule, key: str, raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise RulesetInvalid(
            f"规则「{rule.name}」的 {key} 必须是数字。",
            hint="请输入数字，例如字号 12、行距 1.5、页边距 2.5。",
        ) from None
    if key == "tol" and value < 0:
        raise RulesetInvalid(f"规则「{rule.name}」的容差不能为负数。", hint="容差表示允许的浮动范围，应大于等于 0。")
    if key == "tol" and value > 5:
        raise RulesetInvalid(
            f"规则「{rule.name}」的容差 {value} 过大。",
            hint="容差超过 5 之后这条规则基本失去判定意义，请改小一些。",
        )
    return round(value, 3)


def _coerce_expected(rule: Rule, prop: str, raw: Any) -> Any:
    if prop == "bold":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on", "是", "加粗")
    if prop == "align":
        value = str(raw).strip()
        if value not in ALIGN_LABELS:
            raise RulesetInvalid(
                f"规则「{rule.name}」不认识对齐方式「{value}」。",
                hint="可用值：" + "、".join(sorted(set(ALIGN_LABELS))),
            )
        return value
    if is_font_prop(prop):
        value = str(raw).strip()
        if not value:
            raise RulesetInvalid(f"规则「{rule.name}」的字体不能为空。", hint="请填写字体名称，例如「宋体」。")
        return value
    if prop in NUMERIC_PROPS:
        return _to_float(rule, "expected", raw)
    return raw


def validate_patch(rule: Rule, patch: dict[str, Any]) -> dict[str, Any]:
    """校验并清洗一处参数修改，非法时抛 RulesetInvalid（带可操作提示）。"""
    params = rule.params or {}
    prop = params.get("prop") or ""
    if not isinstance(patch, dict) or not patch:
        raise RulesetInvalid(f"规则「{rule.name}」的修改内容为空。", hint="至少要改一个参数。")

    unknown = [key for key in patch if key not in ALLOWED_PATCH_KEYS]
    if unknown:
        raise RulesetInvalid(
            f"规则「{rule.name}」不支持在这里修改参数：{'、'.join(sorted(unknown))}。",
            hint="这里只能改阈值类参数（expected / tol / min / max / allowed）；要改检查对象请用「YAML 原文」页签。",
        )

    cleaned: dict[str, Any] = {}
    for key, raw in patch.items():
        if key == "allowed":
            if not isinstance(raw, list):
                raise RulesetInvalid(f"规则「{rule.name}」的允许取值必须是列表。", hint="在界面上勾选即可，无需手写。")
            items = [str(item).strip() for item in raw if str(item).strip()]
            if not items:
                raise RulesetInvalid(
                    f"规则「{rule.name}」的允许取值不能为空。",
                    hint="至少要保留一个允许的取值，否则任何写法都会被判为不合规。",
                )
            cleaned[key] = items
        elif key in ("tol", "min", "max"):
            cleaned[key] = _to_float(rule, key, raw)
        else:
            cleaned[key] = _coerce_expected(rule, prop, raw)

    if "min" in cleaned and "max" in cleaned and cleaned["min"] > cleaned["max"]:
        raise RulesetInvalid(
            f"规则「{rule.name}」的最小值（{cleaned['min']}）不能大于最大值（{cleaned['max']}）。",
            hint="请调整区间后重试。",
        )

    low, high, _, _ = numeric_config(prop, params)
    if rule.checker == "prop_range":
        for key in ("min", "max"):
            if key in cleaned and not _within(cleaned[key], low, high):
                raise RulesetInvalid(
                    f"规则「{rule.name}」的 {key} 取值 {cleaned[key]} 超出合理范围（{low} ~ {high}）。",
                    hint=f"{PROP_LABELS.get(prop, prop)}的单位是{UNIT_HINT.get(prop) or '对应单位'}，请确认是否填错。",
                )
    if rule.checker == "prop_equals" and "expected" in cleaned:
        value = cleaned["expected"]
        if isinstance(value, (int, float)) and not isinstance(value, bool) and not _within(value, low, high):
            raise RulesetInvalid(
                f"规则「{rule.name}」的取值 {value} 超出合理范围（{low} ~ {high}）。",
                hint=f"{PROP_LABELS.get(prop, prop)}的单位是{UNIT_HINT.get(prop) or '对应单位'}，请确认是否填错。",
            )
    return cleaned


_STAMP_RE = re.compile(r"；?（阈值已按参考模板「[^」]*」校准[^）]*）")


def stamp_description(description: str, source_name: str) -> str:
    """在规则集说明里留下「这套阈值来自哪份模板」的痕迹（同一份模板只留一条）。"""
    cleaned = _STAMP_RE.sub("", description or "").strip()
    stamp = f"（阈值已按参考模板「{source_name}」校准 · {date.today().isoformat()}）"
    return f"{cleaned}{stamp}"


def apply_updates(
    ruleset: Ruleset,
    updates: list[dict[str, Any]],
    *,
    source_name: str | None = None,
) -> tuple[Ruleset, dict[str, Any]]:
    """把一批参数修改合并进规则集（纯计算，不落库）。

    单条失败只记录到 rejected，不中断整批——用户勾了 20 条，
    不该因为其中一条越界就全部白改。
    """
    by_id = {rule.id: rule for rule in ruleset.rules}
    applied: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for item in updates or []:
        rule_id = str((item or {}).get("rule_id") or "").strip()
        if not rule_id:
            rejected.append({"rule_id": "", "rule_name": "", "message": "缺少 rule_id，已跳过。"})
            continue
        rule = by_id.get(rule_id)
        if rule is None:
            rejected.append(
                {
                    "rule_id": rule_id,
                    "rule_name": "",
                    "message": "规则不存在。",
                    "hint": "规则集可能已被改动，请刷新后重试。",
                }
            )
            continue
        try:
            cleaned = validate_patch(rule, (item or {}).get("params") or {})
        except RulesetInvalid as exc:
            rejected.append(
                {"rule_id": rule_id, "rule_name": rule.name, "message": exc.message, "hint": exc.hint}
            )
            continue
        before = {key: rule.params.get(key) for key in cleaned}
        rule.params.update(cleaned)
        applied.append({"rule_id": rule_id, "rule_name": rule.name, "before": before, "after": cleaned})

    if applied and source_name:
        ruleset.description = stamp_description(ruleset.description, source_name)
    return ruleset, {"applied": applied, "rejected": rejected}


# ---------------------------------------------------------------- 快速调整分组

QUICK_GROUP_PREFIXES = [
    ("layout.page.", "纸张与分节"),
    ("layout.margin.", "页边距"),
    ("layout.title.", "论文题目"),
    ("layout.body.", "正文"),
    ("layout.heading", "各级标题"),
    ("layout.caption.", "图表题注"),
    ("layout.abstract.", "摘要"),
    ("layout.keywords.", "关键词"),
    ("layout.reference.", "参考文献"),
]


def _group_label(rule: Rule) -> str:
    if rule.category == "layout":
        for prefix, label in QUICK_GROUP_PREFIXES:
            if rule.id.startswith(prefix):
                return label
        return "其他排版项"
    return rule.category_label


def _base_item(rule: Rule) -> dict[str, Any]:
    return {
        "rule_id": rule.id,
        "name": rule.name,
        "category": rule.category,
        "category_label": rule.category_label,
        "severity": rule.severity,
        "severity_label": SEVERITIES.get(rule.severity, {}).get("label", rule.severity),
        "enabled": rule.enabled,
        "scope_label": rule.scope_label or SCOPE_LABELS.get(rule.params.get("scope", ""), ""),
        "suggestion": rule.suggestion,
        "group": _group_label(rule),
    }


def quick_item(rule: Rule) -> dict[str, Any] | None:
    """把一条规则翻译成「界面上可以直接改的一项」，不依赖阈值判定的返回 None。"""
    params = rule.params or {}
    prop = params.get("prop")
    item = _base_item(rule)

    if rule.checker == "prop_equals":
        if prop in NUMERIC_PROPS:
            low, high, step, precision = numeric_config(prop, params)
            item.update(
                {
                    "kind": "number",
                    "prop": prop,
                    "prop_label": PROP_LABELS.get(prop, prop),
                    "unit": UNIT_HINT.get(prop, ""),
                    "value": params.get("expected"),
                    "tol": params.get("tol"),
                    "limits": [low, high],
                    "step": step,
                    "precision": precision,
                    "note": _kind_note(params),
                }
            )
            return item
        if prop == "bold":
            item.update(
                {
                    "kind": "enum",
                    "prop": prop,
                    "prop_label": PROP_LABELS.get(prop, prop),
                    "unit": "",
                    "value": bool(params.get("expected")),
                    "options": [{"value": True, "label": "加粗"}, {"value": False, "label": "不加粗"}],
                    "note": "",
                }
            )
            return item
        if prop == "align":
            item.update(
                {
                    "kind": "enum",
                    "prop": prop,
                    "prop_label": PROP_LABELS.get(prop, prop),
                    "unit": "",
                    "value": params.get("expected"),
                    "options": [{"value": value, "label": label} for value, label in ALIGN_OPTIONS],
                    "note": "",
                }
            )
            return item
        if is_font_prop(prop):
            item.update(
                {
                    "kind": "enum",
                    "prop": prop,
                    "prop_label": PROP_LABELS.get(prop, prop),
                    "unit": "",
                    "value": params.get("expected"),
                    "options": [{"value": name, "label": name} for name in FONT_OPTIONS],
                    "note": "",
                }
            )
            return item
        return None

    if rule.checker == "prop_range":
        if prop not in NUMERIC_PROPS:
            return None
        low, high, step, precision = numeric_config(prop, params)
        item.update(
            {
                "kind": "range",
                "prop": prop,
                "prop_label": PROP_LABELS.get(prop, prop),
                "unit": UNIT_HINT.get(prop, ""),
                "min": params.get("min"),
                "max": params.get("max"),
                "limits": [low, high],
                "step": step,
                "precision": precision,
                "note": _kind_note(params),
            }
        )
        return item

    if rule.checker == "prop_in_set":
        allowed = params.get("allowed") or []
        if not isinstance(allowed, list) or not allowed:
            return None
        names = [str(value) for value in allowed]
        options = FONT_OPTIONS if is_font_prop(prop) else []
        merged = list(dict.fromkeys(names + options))
        item.update(
            {
                "kind": "multi",
                "prop": prop,
                "prop_label": PROP_LABELS.get(prop, prop),
                "unit": "",
                "value": names,
                "options": [{"value": name, "label": name} for name in merged],
                "note": "勾选表示该取值被允许（至少保留一项）",
            }
        )
        return item

    if rule.checker == "length_range":
        target = params.get("target")
        low, high, step, precision = LENGTH_LIMITS.get(target, (0.0, 5000.0, 1.0, 0))
        item.update(
            {
                "kind": "range",
                "prop": target,
                "prop_label": LENGTH_TARGET_LABELS.get(target, target or "—"),
                "unit": LENGTH_UNITS.get(target, ""),
                "min": params.get("min"),
                "max": params.get("max"),
                "limits": [low, high],
                "step": step,
                "precision": precision,
                "note": "",
            }
        )
        return item

    return None


def quick_groups(ruleset: Ruleset) -> list[dict[str, Any]]:
    """把「靠阈值判定」的规则按用途分组，供界面直接渲染成表单。"""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for rule in ruleset.rules:
        item = quick_item(rule)
        if item is None:
            continue
        buckets.setdefault(_group_label(rule), []).append(item)
    return [{"group": name, "items": items} for name, items in buckets.items()]
