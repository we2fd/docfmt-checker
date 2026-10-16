"""检查引擎：16 类通用检查器 + 分类评分。

整套引擎不认识任何一条具体规则——它只认识 16 个「检查器」。
规则集（YAML）告诉引擎「用哪个检查器、检查哪个属性、期望值是多少」，
所以规则可以随意增删改，引擎一行都不用动。

判定遵循三条原则：
1. **取不到的属性不算错**，记 `unknown`（待人工确认），避免误报；
2. 每条问题都带**精确位置 + 实际值 + 期望值 + 出处 + 整改建议**，用户知道改哪儿；
3. 分数由**可判定规则**的通过率加权得出，并如实披露「本次覆盖率」。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .docmodel import DocumentModel, ParagraphInfo, _heading_level_from_style
from .rules import CATEGORIES, SEVERITIES, Rule, Ruleset

# 检查器名为以下值时，「没有检查对象」直接视为通过（这些规则描述的是「不该出现的问题」）
ZERO_IS_PASS = {"paragraph_run_uniform", "blank_line_run"}

# 四个维度在总分中的权重：排版与结构是硬要求，权重更高
CATEGORY_WEIGHTS = {"layout": 0.30, "structure": 0.30, "consistency": 0.20, "content": 0.20}

# 每个「错误」级问题在加权得分上再扣多少分，扣分上限
ERROR_PENALTY_PER_ITEM = 4.0
ERROR_PENALTY_CAP = 30.0

RUN_PROPS = {"font_eastasia", "font_ascii", "font_size", "bold", "italic", "color"}
PARA_PROPS = {
    "align",
    "line_spacing",
    "space_before",
    "space_after",
    "first_line_indent",
    "left_indent",
}
SECTION_PROPS = {"margin_top_cm", "margin_bottom_cm", "margin_left_cm", "margin_right_cm", "page_width_cm", "page_height_cm"}

FONT_ALIASES = {
    "simsun": "宋体",
    "宋体": "宋体",
    "nsimsun": "新宋体",
    "simhei": "黑体",
    "黑体": "黑体",
    "kaiti": "楷体",
    "楷体": "楷体",
    "楷体_gb2312": "楷体",
    "fangsong": "仿宋",
    "仿宋": "仿宋",
    "仿宋_gb2312": "仿宋",
    "microsoft yahei": "微软雅黑",
    "微软雅黑": "微软雅黑",
    "times new roman": "Times New Roman",
    "arial": "Arial",
    "calibri": "Calibri",
    "cambria": "Cambria",
}

UNIT_HINT = {
    "font_size": "磅",
    "line_spacing": "",
    "space_before": "磅",
    "space_after": "磅",
    "first_line_indent": "厘米",
    "left_indent": "厘米",
    "margin_top_cm": "厘米",
    "margin_bottom_cm": "厘米",
    "margin_left_cm": "厘米",
    "margin_right_cm": "厘米",
}

ALIGN_LABELS = {
    "left": "左对齐",
    "center": "居中",
    "right": "右对齐",
    "both": "两端对齐",
    "distribute": "分散对齐",
    "justify": "两端对齐",
    "start": "左对齐",
    "end": "右对齐",
}

SCOPE_SELECTOR: dict[str, Callable[[DocumentModel], list[ParagraphInfo]]] = {
    "body": lambda doc: doc.body_paragraphs,
    "all": lambda doc: [p for p in doc.paragraphs if p.char_count() > 0 and not p.in_table],
    "title": lambda doc: doc.paragraphs_of_kind("title"),
    "heading": lambda doc: doc.headings,
    "heading1": lambda doc: [p for p in doc.headings if p.heading_level == 1],
    "heading2": lambda doc: [p for p in doc.headings if p.heading_level == 2],
    "heading3": lambda doc: [p for p in doc.headings if p.heading_level == 3],
    "figure_caption": lambda doc: [p for p in doc.paragraphs if p.kind == "figure_caption"],
    "table_caption": lambda doc: [p for p in doc.paragraphs if p.kind == "table_caption"],
    "caption": lambda doc: doc.captions,
    "reference": lambda doc: doc.references,
    "keywords": lambda doc: doc.paragraphs_of_kind("keywords"),
    "abstract": lambda doc: [p for p in doc.paragraphs if p.kind == "abstract"],
    "toc": lambda doc: doc.paragraphs_of_kind("toc"),
}

SCOPE_LABELS = {
    "body": "正文段落",
    "all": "全部段落",
    "title": "论文题目",
    "heading": "全部标题",
    "heading1": "一级标题",
    "heading2": "二级标题",
    "heading3": "三级标题",
    "figure_caption": "图题注",
    "table_caption": "表题注",
    "caption": "全部图表题注",
    "reference": "参考文献条目",
    "keywords": "关键词行",
    "abstract": "摘要段落",
    "toc": "目录行",
}


# ---------------------------------------------------------------- 输出结构


@dataclass
class Issue:
    rule_id: str
    rule_name: str
    category: str
    severity: str
    status: str  # fail | unknown
    message: str
    actual: Any = None
    expected: Any = None
    location: dict[str, Any] = field(default_factory=dict)
    suggestion: str = ""
    reference: str = ""
    source_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "category": self.category,
            "category_label": CATEGORIES.get(self.category, self.category),
            "severity": self.severity,
            "severity_label": SEVERITIES.get(self.severity, {}).get("label", self.severity),
            "status": self.status,
            "message": self.message,
            "actual": self.actual,
            "expected": self.expected,
            "location": self.location,
            "suggestion": self.suggestion,
            "reference": self.reference,
            "source_text": self.source_text,
        }


@dataclass
class RuleResult:
    rule_id: str
    rule_name: str
    category: str
    severity: str
    status: str  # pass | fail | unknown | disabled
    checked_count: int = 0
    fail_count: int = 0
    unknown_count: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "category": self.category,
            "category_label": CATEGORIES.get(self.category, self.category),
            "severity": self.severity,
            "severity_label": SEVERITIES.get(self.severity, {}).get("label", self.severity),
            "status": self.status,
            "checked_count": self.checked_count,
            "fail_count": self.fail_count,
            "unknown_count": self.unknown_count,
            "note": self.note,
        }


# ---------------------------------------------------------------- 工具函数


def normalize_font(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return FONT_ALIASES.get(text.lower(), text)


_PUNCT_PATTERN = re.compile(
    r"[\u4e00-\u9fff]\s*([,;:?!])"          # 中文后面跟英文标点：中文,中文
    r"|([,;:?!])\s*[\u4e00-\u9fff]"          # 英文标点后面跟中文：,中文
    r"|[\u4e00-\u9fff]\s*\.(?![0-9])"        # 中文后面的句点（排除小数点）
    r"|\.(?![0-9])\s*[\u4e00-\u9fff]"        # 句点后面跟中文（排除小数点）
)


def scan_english_punctuation(text: str) -> int:
    """统计中文语境里混用的英文标点数量（跳过网址与小数点）。

    用单条正则一次匹配而不是三次替换式匹配，否则「中文,中文」里的同一个逗号会被数两次。
    """
    if not text:
        return 0
    cleaned = re.sub(r"https?://\S+", "", text)
    return len(_PUNCT_PATTERN.findall(cleaned))


def chinese_ratio(text: str) -> float:
    if not text:
        return 0.0
    han = len(re.findall(r"[\u4e00-\u9fff]", text))
    return han / max(len(re.sub(r"\s", "", text)), 1)


def format_value(prop: str, value: Any) -> str:
    if value is None:
        return "未识别"
    if prop == "align":
        return ALIGN_LABELS.get(str(value), str(value))
    if prop == "line_spacing":
        return f"{value} 倍" if isinstance(value, (int, float)) else str(value)
    if prop in UNIT_HINT:
        unit = UNIT_HINT[prop]
        if isinstance(value, float):
            value = round(value, 2)
        return f"{value} {unit}".strip()
    if prop == "bold":
        return "加粗" if value else "不加粗"
    if isinstance(value, float):
        return str(round(value, 2))
    return str(value)


def get_prop(para: ParagraphInfo, prop: str) -> Any:
    if prop in RUN_PROPS:
        return para.run_value(prop)
    if prop in PARA_PROPS:
        return para.value(prop)
    return para.value(prop)


def get_prop_source(para: ParagraphInfo, prop: str) -> str:
    if prop in RUN_PROPS:
        return para.run_source(prop)
    return para.source(prop)


def select(doc: DocumentModel, scope: str) -> list[ParagraphInfo]:
    selector = SCOPE_SELECTOR.get(scope)
    if selector is None:
        return doc.body_paragraphs
    return selector(doc)


def section_map(doc: DocumentModel) -> dict[int, str]:
    """段落序号 → 所属章节名，用于把问题定位到「第几章第几节」。"""
    result: dict[int, str] = {}
    current = "（文档开头）"
    for para in doc.paragraphs:
        if para.heading_level is not None and para.kind == "heading" and para.char_count() > 0:
            current = para.text.strip()[:40]
        result[para.index] = current
    return result


def make_location(para: ParagraphInfo, sections: dict[int, str]) -> dict[str, Any]:
    snippet = para.text.strip()
    if len(snippet) > 48:
        snippet = snippet[:48] + "…"
    return {
        "paragraph_index": para.index,
        "section": sections.get(para.index, ""),
        "snippet": snippet,
        "style_name": para.style_name,
        "in_table": para.in_table,
    }


def tolerance_ok(actual: float, expected: float, tol: float | None) -> bool:
    if tol is None:
        return abs(actual - expected) < 1e-6
    return abs(actual - expected) <= tol + 1e-9


# ---------------------------------------------------------------- 检查器实现


def _check_prop_equals(doc: DocumentModel, rule: Rule, sections: dict[int, str]) -> tuple[int, list[Issue]]:
    params = rule.params
    prop = params["prop"]
    expected = params["expected"]
    tol = params.get("tol")
    comparison = params.get("comparison", "eq")
    issues: list[Issue] = []

    if params.get("target") == "section" or prop in SECTION_PROPS:
        checked = 0
        for section in doc.sections:
            actual = getattr(section, prop, None)
            checked += 1
            if actual is None:
                issues.append(
                    _issue(rule, "unknown", f"未能从文档中识别{rule.name}，需人工确认。",
                           paragraph=None, actual=None, expected=expected)
                )
                continue
            if not _value_matches(actual, expected, tol, comparison, prop):
                issues.append(
                    _issue(rule, "fail", f"{rule.name}为 {format_value(prop, actual)}，应为 {format_value(prop, expected)}。",
                           paragraph=None, actual=format_value(prop, actual), expected=format_value(prop, expected),
                           section=f"第 {section.index + 1} 节")
                )
        return checked, issues

    scope = params.get("scope", "body")
    skip_empty = params.get("skip_empty", True)
    para_list = select(doc, scope)
    checked = 0
    for para in para_list:
        if skip_empty and para.char_count() == 0:
            continue
        checked += 1
        actual = get_prop(para, prop)
        if actual is None:
            issues.append(
                _issue(rule, "unknown",
                       f"该段落的{rule.name}继承了文档默认值或未设置，工具无法确定，请人工核对。",
                       paragraph=para, actual=None, expected=expected)
            )
            continue
        if not _value_matches(actual, expected, tol, comparison, prop):
            issues.append(
                _issue(rule, "fail",
                       f"{rule.name}为 {format_value(prop, actual)}，应为 {format_value(prop, expected)}。",
                       paragraph=para, actual=format_value(prop, actual),
                       expected=format_value(prop, expected),
                       source_text=para.source_text(prop) if prop not in RUN_PROPS else para.run_source(prop))
            )
    return checked, issues


def _value_matches(actual: Any, expected: Any, tol: float | None, comparison: str, prop: str) -> bool:
    if prop in ("font_eastasia", "font_ascii"):
        left, right = normalize_font(actual), normalize_font(expected)
        if comparison == "contains":
            return bool(left and right and (right in left or left in right))
        return left == right
    if comparison == "contains":
        return str(expected) in str(actual)
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return tolerance_ok(float(actual), float(expected), tol)
    if isinstance(expected, bool) or isinstance(actual, bool):
        return bool(actual) == bool(expected)
    return str(actual).strip() == str(expected).strip()


def _check_prop_range(doc: DocumentModel, rule: Rule, sections: dict[int, str]) -> tuple[int, list[Issue]]:
    params = rule.params
    prop = params["prop"]
    low, high = params.get("min"), params.get("max")
    kind_prop = params.get("kind_prop")
    kind_value = params.get("kind_value")
    para_list = select(doc, params.get("scope", "body"))
    issues: list[Issue] = []
    checked = 0
    for para in para_list:
        if para.char_count() == 0:
            continue
        # 行距这类属性有两种口径（倍数 / 固定磅值），只能对同一种口径做区间判定，
        # 否则固定 20 磅会被误判成「0.83 倍」。口径不匹配的直接跳过，不计入分母。
        if kind_prop and para.value(kind_prop) not in (None, kind_value):
            continue
        actual = get_prop(para, prop)
        if actual is None:
            continue
        checked += 1
        if isinstance(actual, (int, float)) and low is not None and actual < low - 1e-9:
            issues.append(_issue(rule, "fail", f"{rule.name}为 {format_value(prop, actual)}，小于下限 {format_value(prop, low)}。",
                                 paragraph=para, actual=format_value(prop, actual), expected=format_value(prop, low)))
        elif isinstance(actual, (int, float)) and high is not None and actual > high + 1e-9:
            issues.append(_issue(rule, "fail", f"{rule.name}为 {format_value(prop, actual)}，大于上限 {format_value(prop, high)}。",
                                 paragraph=para, actual=format_value(prop, actual), expected=format_value(prop, high)))
    return checked, issues


def _check_prop_in_set(doc: DocumentModel, rule: Rule, sections: dict[int, str]) -> tuple[int, list[Issue]]:
    params = rule.params
    prop = params["prop"]
    allowed = params["allowed"]
    normalized_allowed = {normalize_font(item) if prop.startswith("font") else str(item).strip() for item in allowed}
    para_list = select(doc, params.get("scope", "body"))
    issues: list[Issue] = []
    checked = 0
    for para in para_list:
        if para.char_count() == 0:
            continue
        actual = get_prop(para, prop)
        if actual is None:
            continue
        checked += 1
        candidate = normalize_font(actual) if prop.startswith("font") else str(actual).strip()
        if candidate not in normalized_allowed:
            issues.append(
                _issue(rule, "fail", f"{rule.name}为「{candidate}」，不在允许范围（{'、'.join(map(str, allowed))}）内。",
                       paragraph=para, actual=candidate, expected="、".join(map(str, allowed)))
            )
    return checked, issues


def _check_consistency(doc: DocumentModel, rule: Rule, sections: dict[int, str]) -> tuple[int, list[Issue]]:
    params = rule.params
    prop = params["prop"]
    scope = params.get("scope", "body")
    para_list = [p for p in select(doc, scope) if p.char_count() > 0]
    issues: list[Issue] = []
    buckets: dict[str, list[ParagraphInfo]] = {}
    for para in para_list:
        actual = get_prop(para, prop)
        if actual is None:
            continue
        key = normalize_font(actual) if prop.startswith("font") else str(actual)
        buckets.setdefault(key, []).append(para)
    checked = len(para_list)
    if not buckets:
        return checked, issues
    # 取「出现次数最多」的取值作为基准；并列时优先取首次出现的
    best_key = max(buckets, key=lambda key: (len(buckets[key]), -para_list.index(buckets[key][0])))
    if len(buckets) > 1:
        for key, paras in buckets.items():
            if key == best_key:
                continue
            sample = paras[0]
            label = format_value(prop, key)
            expected = format_value(prop, best_key)
            issues.append(
                _issue(rule, "fail",
                       f"{rule.name}不统一：{len(paras)} 处为 {label}，其余 {len(buckets[best_key])} 处为 {expected}。",
                       paragraph=sample, actual=label, expected=expected,
                       extra={"affected_count": len(paras),
                              "affected_indexes": [p.index for p in paras[:20]]})
            )
    return checked, issues


def _check_heading_no_skip(doc: DocumentModel, rule: Rule, sections: dict[int, str]) -> tuple[int, list[Issue]]:
    headings = [p for p in doc.headings if p.char_count() > 0]
    issues: list[Issue] = []
    previous = 0
    for para in headings:
        level = para.heading_level or 0
        if previous and level > previous + 1:
            issues.append(
                _issue(rule, "fail", f"标题层级跳级：从 {previous} 级直接跳到 {level} 级。",
                       paragraph=para, actual=f"{level} 级标题", expected=f"{previous + 1} 级或更高级")
            )
        previous = level
    return len(headings), issues


def _check_heading_style_used(doc: DocumentModel, rule: Rule, sections: dict[int, str]) -> tuple[int, list[Issue]]:
    headings = [p for p in doc.headings if p.char_count() > 0]
    issues: list[Issue] = []
    manual: list[ParagraphInfo] = []
    for para in headings:
        if para.value("outline_level") is None and _heading_level_from_style(para.style_name) is None:
            manual.append(para)
    if manual:
        issues.append(
            _issue(rule, "fail",
                   f"有 {len(manual)} 个标题没有使用 Word「标题 1/2/3」样式（靠手工调字体加粗实现），"
                   "这会导致自动目录无法生成。",
                   paragraph=manual[0], actual=f"{len(manual)} 个标题为手工排版",
                   expected="使用 Word 内置标题样式",
                   extra={"affected_count": len(manual), "affected_indexes": [p.index for p in manual[:20]]})
        )
    return len(headings), issues


_CN_NUM_RE = re.compile(r"(\d+)")


def _caption_numbers(doc: DocumentModel, prefix: str) -> list[tuple[ParagraphInfo, str, list[int]]]:
    wanted = "figure_caption" if prefix == "图" else "table_caption"
    result = []
    for para in doc.paragraphs:
        if para.kind != wanted:
            continue
        match = re.search(r"([\d]+(?:[-－.．][\d]+)*)", para.text)
        if not match:
            continue
        numbers = [int(part) for part in re.split(r"[-－.．]", match.group(1)) if part.isdigit()]
        result.append((para, match.group(1), numbers))
    return result


def _check_numbering_continuous(doc: DocumentModel, rule: Rule, sections: dict[int, str]) -> tuple[int, list[Issue]]:
    prefix = rule.params["prefix"]
    items = _caption_numbers(doc, prefix)
    issues: list[Issue] = []
    seen: dict[str, ParagraphInfo] = {}
    last_serial = 0
    for para, raw, numbers in items:
        if raw in seen:
            issues.append(
                _issue(rule, "fail", f"{prefix}编号重复：「{prefix}{raw}」出现了多次。",
                       paragraph=para, actual=f"{prefix}{raw}", expected="编号唯一")
            )
            continue
        seen[raw] = para
        serial = numbers[-1]
        if len(numbers) == 1:
            if serial != last_serial + 1:
                issues.append(
                    _issue(rule, "fail",
                           f"{prefix}编号不连续：上一处为「{prefix}{last_serial}」，此处为「{prefix}{raw}」。",
                           paragraph=para, actual=f"{prefix}{raw}", expected=f"{prefix}{last_serial + 1}")
                )
            last_serial = serial
    return len(items), issues


def _check_caption_referenced(doc: DocumentModel, rule: Rule, sections: dict[int, str]) -> tuple[int, list[Issue]]:
    issues: list[Issue] = []
    caption_indexes = {p.index for p in doc.captions}
    body_text = "\n".join(p.text for p in doc.paragraphs if p.index not in caption_indexes)
    total = 0
    for para in doc.captions:
        match = re.search(r"(图|表)\s*([\d]+(?:[-－.．][\d]+)*)", para.text)
        if not match:
            continue
        total += 1
        keyword = f"{match.group(1)}{match.group(2)}"
        flexible = re.sub(r"[-－.．]", "[-－.．]?", keyword)
        if not re.search(flexible, body_text):
            issues.append(
                _issue(rule, "fail", f"正文中没有发现对「{keyword}」的引用（正文应出现「如{keyword}所示」之类的表述）。",
                       paragraph=para, actual="正文未引用", expected=f"正文引用 {keyword}")
            )
    return total, issues


def _check_required_section(doc: DocumentModel, rule: Rule, sections: dict[int, str]) -> tuple[int, list[Issue]]:
    issues: list[Issue] = []
    positions = rule.params.get("positions", {})
    names = rule.params["names"]
    for name in names:
        found = doc.find_heading(name) is not None or bool(doc.paragraphs_of_kind("keywords") if "关键词" in name else False)
        if not found:
            issues.append(
                _issue(rule, "fail", f"未找到「{name}」章节。",
                       paragraph=None, actual="缺失", expected=f"包含「{name}」",
                       extra={"position_hint": positions.get(name, "")})
            )
    return len(names), issues


def _check_length_range(doc: DocumentModel, rule: Rule, sections: dict[int, str]) -> tuple[int, list[Issue]]:
    target = rule.params["target"]
    low, high = rule.params["min"], rule.params["max"]
    issues: list[Issue] = []
    if target == "abstract":
        text = doc.abstract_text()
        if not text:
            issues.append(_issue(rule, "unknown", "未能定位摘要正文（可能缺少「摘要」标题）。", paragraph=None,
                                 actual="未找到", expected=f"{low}~{high} 字"))
            return 0, issues
        count = len(re.sub(r"\s", "", text))
        if not (low <= count <= high):
            issues.append(
                _issue(rule, "fail", f"摘要 {count} 字，超出 {low}~{high} 字的要求。",
                       paragraph=None, actual=f"{count} 字", expected=f"{low}~{high} 字")
            )
        return 1, issues
    if target == "keywords":
        text = doc.keywords_text()
        if not text:
            issues.append(_issue(rule, "unknown", "未能定位关键词行。", paragraph=None, actual="未找到",
                                 expected=f"{low}~{high} 个"))
            return 0, issues
        body = re.sub(r"^(关键词|关键字|key\s*words?)\s*[:：]?", "", text.strip(), flags=re.IGNORECASE)
        items = [item for item in re.split(r"[，,;；、\s]+", body) if item.strip()]
        count = len(items)
        if not (low <= count <= high):
            issues.append(
                _issue(rule, "fail", f"关键词 {count} 个，应为 {low}~{high} 个。",
                       paragraph=doc.paragraphs_of_kind("keywords")[0] if doc.paragraphs_of_kind("keywords") else None,
                       actual=f"{count} 个", expected=f"{low}~{high} 个")
            )
        return 1, issues
    if target == "reference":
        count = len(doc.references)
        if count == 0:
            issues.append(_issue(rule, "fail", "参考文献列表为空。", paragraph=None, actual="0 条",
                                 expected=f"{low}~{high} 条"))
        elif not (low <= count <= high):
            issues.append(_issue(rule, "fail", f"参考文献 {count} 条，建议 {low}~{high} 条。",
                                 paragraph=None, actual=f"{count} 条", expected=f"{low}~{high} 条"))
        return 1, issues
    if target == "title":
        first = next((p for p in doc.paragraphs if p.char_count() > 0), None)
        if first is None:
            return 0, issues
        count = first.char_count()
        if not (low <= count <= high):
            issues.append(_issue(rule, "fail", f"标题 {count} 字，建议 {low}~{high} 字。",
                                 paragraph=first, actual=f"{count} 字", expected=f"{low}~{high} 字"))
        return 1, issues
    return 0, issues


def _check_punctuation_mix(doc: DocumentModel, rule: Rule, sections: dict[int, str]) -> tuple[int, list[Issue]]:
    issues: list[Issue] = []
    checked = 0
    for para in doc.paragraphs:
        if para.char_count() < 10 or para.in_table:
            continue
        if para.in_references or para.kind in ("figure_caption", "table_caption"):
            continue
        if chinese_ratio(para.text) < 0.4:
            continue
        checked += 1
        count = scan_english_punctuation(para.text)
        if count > 0:
            issues.append(
                _issue(rule, "fail", f"检测到 {count} 处中文语句中混用英文标点。",
                       paragraph=para, actual=f"{count} 处英文标点", expected="使用中文全角标点")
            )
    return checked, issues


def _check_reference_format(doc: DocumentModel, rule: Rule, sections: dict[int, str]) -> tuple[int, list[Issue]]:
    issues: list[Issue] = []
    refs = doc.references
    type_marker = re.compile(r"\[(J|M|C|D|N|P|S|R|Z|EB/OL|DB/OL|CP/DK)\]", re.IGNORECASE)
    year_re = re.compile(r"(19|20)\d{2}")
    for para in refs:
        missing: list[str] = []
        text = para.text.strip()
        if not year_re.search(text):
            missing.append("出版年份")
        if not type_marker.search(text):
            missing.append("文献类型标识（如 [J]/[M]/[D]）")
        if len(re.sub(r"\s", "", text)) < 20:
            missing.append("著录要素过少（作者、题名、出处）")
        if missing:
            issues.append(
                _issue(rule, "fail", "该条参考文献缺少：" + "、".join(missing) + "。",
                       paragraph=para, actual=text[:40] + ("…" if len(text) > 40 else ""),
                       expected="作者. 题名[文献类型]. 出处, 年份.")
            )
    return len(refs), issues


def _check_empty_heading(doc: DocumentModel, rule: Rule, sections: dict[int, str]) -> tuple[int, list[Issue]]:
    issues: list[Issue] = []
    headings = [p for p in doc.paragraphs if p.heading_level is not None and p.char_count() > 0]
    for position, para in enumerate(headings):
        level = para.heading_level or 1
        following = None
        for candidate in doc.paragraphs:
            if candidate.index <= para.index:
                continue
            if candidate.char_count() == 0:
                continue
            following = candidate
            break
        if following is None:
            issues.append(_issue(rule, "fail", "该标题之后文档即结束，没有正文内容。", paragraph=para,
                                 actual="无正文", expected="标题下有正文"))
        elif following.heading_level is not None and following.heading_level <= level:
            issues.append(_issue(rule, "fail", "该标题下没有正文（下一个非空段落是同级或更高级标题）。",
                                 paragraph=para, actual="无正文", expected="标题下有正文"))
    return len(headings), issues


def _check_paragraph_run_uniform(doc: DocumentModel, rule: Rule, sections: dict[int, str]) -> tuple[int, list[Issue]]:
    issues: list[Issue] = []
    checked = 0
    for para in doc.paragraphs:
        if para.char_count() < 20 or para.in_table:
            continue
        effective = [(run.props["font_eastasia"].value, run.props["font_size"].value)
                     for run in para.runs if run.text.strip()]
        if len(effective) < 2:
            continue
        checked += 1
        distinct = {item for item in effective if item[0] is not None or item[1] is not None}
        if len(distinct) > 1:
            counts = {item: effective.count(item) for item in distinct}
            dominant = max(counts, key=counts.get)
            minority = [item for item in distinct if item != dominant]
            desc = "；".join(
                f"{normalize_font(item[0]) or '未识别'} {item[1] or '未识别'} 磅 × {counts[item]} 处" for item in minority
            )
            issues.append(
                _issue(rule, "fail", f"同一段落内字体/字号不一致：{desc}，其余为 "
                                     f"{normalize_font(dominant[0]) or '未识别'} {dominant[1] or '未识别'} 磅。",
                       paragraph=para, actual=desc,
                       expected=f"{normalize_font(dominant[0]) or '未识别'} {dominant[1] or '未识别'} 磅（与本节多数一致）")
            )
    return checked, issues


def _check_blank_line_run(doc: DocumentModel, rule: Rule, sections: dict[int, str]) -> tuple[int, list[Issue]]:
    limit = int(rule.params["max_consecutive"])
    issues: list[Issue] = []
    streak: list[ParagraphInfo] = []
    checked = 0
    for para in doc.paragraphs:
        if para.in_table:
            continue
        if para.char_count() == 0:
            streak.append(para)
            continue
        if streak:
            checked += 1
            if len(streak) > limit:
                issues.append(
                    _issue(rule, "fail", f"出现 {len(streak)} 个连续空段落（建议最多 {limit} 个）。",
                           paragraph=streak[0], actual=f"{len(streak)} 个连续空段落", expected=f"≤ {limit} 个",
                           extra={"affected_indexes": [p.index for p in streak]})
                )
            streak = []
    return checked, issues


def _check_page_number_present(doc: DocumentModel, rule: Rule, sections: dict[int, str]) -> tuple[int, list[Issue]]:
    if not doc.sections:
        return 0, [_issue(rule, "unknown", "未能读取节信息。", paragraph=None, actual="未识别", expected="页脚有页码")]
    missing = [section.index + 1 for section in doc.sections if not section.has_page_number]
    if missing and len(missing) == len(doc.sections):
        return len(doc.sections), [
            _issue(rule, "fail", "未在页脚检测到页码域。", paragraph=None, actual="无页码", expected="页脚居中/右侧显示页码")
        ]
    return len(doc.sections), []


CHECKER_TABLE: dict[str, Callable[[DocumentModel, Rule, dict[int, str]], tuple[int, list[Issue]]]] = {
    "prop_equals": _check_prop_equals,
    "prop_range": _check_prop_range,
    "prop_in_set": _check_prop_in_set,
    "consistency": _check_consistency,
    "heading_no_skip": _check_heading_no_skip,
    "heading_style_used": _check_heading_style_used,
    "numbering_continuous": _check_numbering_continuous,
    "caption_referenced": _check_caption_referenced,
    "required_section": _check_required_section,
    "length_range": _check_length_range,
    "punctuation_mix": _check_punctuation_mix,
    "reference_format": _check_reference_format,
    "empty_heading": _check_empty_heading,
    "paragraph_run_uniform": _check_paragraph_run_uniform,
    "blank_line_run": _check_blank_line_run,
    "page_number_present": _check_page_number_present,
}


def _issue(
    rule: Rule,
    status: str,
    message: str,
    *,
    paragraph: ParagraphInfo | None,
    actual: Any,
    expected: Any,
    source_text: str = "",
    section: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Issue:
    # location 的键必须始终存在（哪怕值为 None），否则前端「定位到原文」会拿不到字段而报错
    location: dict[str, Any] = {
        "paragraph_index": None,
        "section": section or "",
        "snippet": "",
        "style_name": None,
        "in_table": False,
    }
    if paragraph is not None:
        snippet = paragraph.text.strip()
        if len(snippet) > 48:
            snippet = snippet[:48] + "…"
        location.update(
            {
                "paragraph_index": paragraph.index,
                "section": section or "",
                "snippet": snippet,
                "style_name": paragraph.style_name,
                "in_table": paragraph.in_table,
            }
        )
    if extra:
        location.update(extra)
    return Issue(
        rule_id=rule.id,
        rule_name=rule.name,
        category=rule.category,
        severity=rule.severity,
        status=status,
        message=message,
        actual=actual,
        expected=expected,
        location=location,
        suggestion=rule.suggestion,
        reference=rule.reference,
        source_text=source_text,
    )


# ---------------------------------------------------------------- 引擎主体


def run_checks(doc: DocumentModel, ruleset: Ruleset, *, include_disabled: bool = True) -> dict[str, Any]:
    started = time.perf_counter()
    sections = section_map(doc)
    rule_results: list[RuleResult] = []
    issues: list[Issue] = []

    for rule in ruleset.rules:
        if not rule.enabled:
            rule_results.append(
                RuleResult(rule.id, rule.name, rule.category, rule.severity, "disabled",
                           note="规则已被关闭" if include_disabled else "")
            )
            continue
        checker = CHECKER_TABLE.get(rule.checker)
        if checker is None:  # pragma: no cover - 加载阶段已校验
            rule_results.append(RuleResult(rule.id, rule.name, rule.category, rule.severity, "unknown",
                                           note="检查器不存在"))
            continue
        try:
            checked, rule_issues = checker(doc, rule, sections)
        except Exception as exc:  # 单条规则异常不应拖垮整轮检查
            rule_results.append(RuleResult(rule.id, rule.name, rule.category, rule.severity, "unknown",
                                           note=f"该规则执行出错：{exc}"))
            continue

        fail_count = len([item for item in rule_issues if item.status == "fail"])
        unknown_count = len([item for item in rule_issues if item.status == "unknown"])
        if checked == 0 and fail_count == 0:
            # 「没有检查对象」不等于「无法判定」：像「段落内字体是否一致」这种规则，
            # 文档里压根没有多字体段落，本身就说明这项没问题。
            if rule.checker in ZERO_IS_PASS:
                status = "pass"
                note = "本次文档中没有相关对象，视为通过"
            else:
                status = "unknown"
                note = "本次文档中没有可判定的对象（可能缺少对应章节）"
        elif fail_count:
            status = "fail"
            note = ""
        elif unknown_count:
            status = "unknown"
            note = "存在无法自动判定的项目，已列入待人工确认"
        else:
            status = "pass"
            note = ""
        rule_results.append(
            RuleResult(rule.id, rule.name, rule.category, rule.severity, status,
                       checked_count=checked, fail_count=fail_count, unknown_count=unknown_count, note=note)
        )
        issues.extend(rule_issues)

    severity_rank = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda item: (
        severity_rank.get(item.severity, 3),
        0 if item.status == "fail" else 1,
        item.location.get("paragraph_index") if item.location.get("paragraph_index") is not None else 10**6,
    ))

    summary = _score(rule_results, issues)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    summary["elapsed_ms"] = elapsed_ms
    summary["issue_total"] = len(issues)
    return {
        "document": doc.summary(),
        "ruleset": {"id": ruleset.id, "name": ruleset.name, "version": ruleset.version},
        **summary,
        "issues": [item.to_dict() for item in issues],
        "rule_results": [item.to_dict() for item in rule_results],
        "category_definitions": CATEGORIES,
        "severity_definitions": {key: {"label": value["label"], "weight": value["weight"]} for key, value in SEVERITIES.items()},
    }


def _score(rule_results: list[RuleResult], issues: list[Issue]) -> dict[str, Any]:
    categories: list[dict[str, Any]] = []
    weighted_sum = 0.0
    weight_total = 0.0

    for key, label in CATEGORIES.items():
        group = [item for item in rule_results if item.category == key and item.status != "disabled"]
        passed = len([item for item in group if item.status == "pass"])
        failed = len([item for item in group if item.status == "fail"])
        unknown = len([item for item in group if item.status == "unknown"])
        evaluated = passed + failed
        score = round(passed / evaluated * 100, 1) if evaluated else None
        weight = CATEGORY_WEIGHTS.get(key, 0.1)
        if score is not None:
            weighted_sum += score * weight
            weight_total += weight
        categories.append(
            {
                "key": key,
                "label": label,
                "score": score,
                "weight": weight,
                "total": len(group),
                "passed": passed,
                "failed": failed,
                "unknown": unknown,
                "issue_count": len([item for item in issues if item.category == key]),
            }
        )

    overall = weighted_sum / weight_total if weight_total else None
    error_count = len([item for item in issues if item.severity == "error" and item.status == "fail"])
    warning_count = len([item for item in issues if item.severity == "warning" and item.status == "fail"])
    info_count = len([item for item in issues if item.severity == "info" and item.status == "fail"])
    unknown_count = len([item for item in issues if item.status == "unknown"])

    penalty = min(ERROR_PENALTY_CAP, error_count * ERROR_PENALTY_PER_ITEM)
    final_score = None
    if overall is not None:
        final_score = int(max(0, min(100, round(overall - penalty))))
    if final_score is None:
        grade, grade_label, grade_color = None, "无法评定", "#888780"
    elif final_score >= 90:
        grade, grade_label, grade_color = "A", "格式规范，可直接提交", "#639922"
    elif final_score >= 75:
        grade, grade_label, grade_color = "B", "基本规范，建议修复后提交", "#378ADD"
    elif final_score >= 60:
        grade, grade_label, grade_color = "C", "存在较多格式问题，需逐项修改", "#EF9F27"
    else:
        grade, grade_label, grade_color = "D", "格式问题严重，建议按清单全面整改", "#E24B4A"

    enabled_total = len([item for item in rule_results if item.status != "disabled"])
    evaluated_total = len([item for item in rule_results if item.status in ("pass", "fail")])
    rule_unknown = len([item for item in rule_results if item.status == "unknown"])
    coverage = round(evaluated_total / enabled_total * 100) if enabled_total else 0
    notes: list[str] = []
    notes.append(
        f"总分算法：四个维度按权重加权（排版 {int(CATEGORY_WEIGHTS['layout'] * 100)}%、"
        f"结构 {int(CATEGORY_WEIGHTS['structure'] * 100)}%、一致性 {int(CATEGORY_WEIGHTS['consistency'] * 100)}%、"
        f"内容 {int(CATEGORY_WEIGHTS['content'] * 100)}%），再按错误数量扣分（每项错误 -{int(ERROR_PENALTY_PER_ITEM)} 分，"
        f"最多扣 {int(ERROR_PENALTY_CAP)} 分）。"
    )
    if rule_unknown:
        notes.append(f"有 {rule_unknown} 条规则无法自动判定（本文档缺少对应章节或对象），未计入分母。")
    if coverage < 60:
        notes.append(
            f"本次只有 {coverage}% 的规则得到有效判定（其余因缺少对应章节或格式未显式设置而无法判定），分数仅供参考。"
        )
    if unknown_count:
        notes.append(f"另有 {unknown_count} 项无法自动判定，已标注为「待人工确认」，不计入扣分。")

    return {
        "score": final_score,
        "grade": grade,
        "grade_label": grade_label,
        "grade_color": grade_color,
        "raw_score": round(overall, 1) if overall is not None else None,
        "penalty": penalty,
        "coverage": coverage,
        "category_scores": categories,
        "issue_summary": {
            "error": error_count,
            "warning": warning_count,
            "info": info_count,
            "unknown": unknown_count,
        },
        "notes": notes,
    }
