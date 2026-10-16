"""模板反推 + 阈值快速调整的单元测试。

重点验证三件事：
1. 推导确实是「由规则自己声明的 scope/prop 驱动」的——换一条规则就自动覆盖；
2. 提取到的是**众数**而不是平均值，且样本不一致时如实降低置信度；
3. 写回参数时非法值会被拦下并给出可操作提示，而不是默默写坏规则集。
"""

from __future__ import annotations

import pytest

from app.core import template as tpl
from app.core.docmodel import parse_docx
from app.core.rules import dump_ruleset, parse_ruleset
from app.errors import RulesetInvalid


def _proposal_map(result: dict) -> dict[str, dict]:
    return {item["rule_id"]: item for item in result["proposals"]}


# ---------------------------------------------------------------- 推导


def test_derive_font_size_is_the_mode_not_the_average(factory):
    """9 段 12 磅 + 1 段 14 磅 → 应推导出 12（众数），而不是 12.2（平均值）。"""
    doc = factory.new()
    factory.paragraph(doc, "文档题目", east="黑体", size=22, indent=None, line=None)
    for index in range(9):
        factory.paragraph(doc, f"这是第 {index} 段正文，用来撑出足够的样本量。" * 3)
    factory.paragraph(doc, "被误粘贴成小四以外字号的这一段。" * 3, size=14)
    model = parse_docx(str(factory.save(doc, "mode.docx")))

    result = tpl.derive_proposals(parse_ruleset(_MINI_YAML), model)
    item = _proposal_map(result)["t.body.size"]

    assert item["template_value"] == 12
    assert item["measured"]["count"] == 10
    assert item["measured"]["dominant_count"] == 9
    assert item["confidence"] == pytest.approx(0.9)
    # 已经有 14 磅存在，所以 12 与「规则里写的 12」一致 → 无需调整
    assert item["status"] == "unchanged"


def test_derive_detects_noncompliant_template_and_proposes(factory):
    """模板本身用的是 14 磅 → 应提出把规则由 12 改成 14，并给出容差。"""
    doc = factory.new()
    factory.paragraph(doc, "文档题目", east="黑体", size=22, indent=None, line=None)
    for index in range(6):
        factory.paragraph(doc, f"第 {index} 段正文，本模板统一使用四号字。" * 3, size=14)
    model = parse_docx(str(factory.save(doc, "fourteen.docx")))

    item = _proposal_map(tpl.derive_proposals(parse_ruleset(_MINI_YAML), model))["t.body.size"]
    assert item["status"] == "proposed"
    assert item["applicable"] is True
    assert item["proposal"]["expected"] == 14
    # 自动容差 = 取值的 5%，但封顶 0.5（否则大数值会带出「±1 厘米」这种宽松到没意义的容差）
    assert item["proposal"]["tol"] == 0.5
    assert "12" in item["current_text"] and "14" in item["proposal_text"]


def test_derive_margin_from_section_settings(factory):
    """页面设置类规则（target: section）走的是另一个取数分支。"""
    doc = factory.new(margins=(3.5, 3.5, 4.0, 4.0))
    factory.paragraph(doc, "文档题目", east="黑体", size=22, indent=None, line=None)
    factory.paragraph(doc, "正文内容，用于让文档有可解析的段落。" * 3)
    model = parse_docx(str(factory.save(doc, "margin.docx")))

    item = _proposal_map(tpl.derive_proposals(parse_ruleset(_MARGIN_YAML), model))["t.margin.left"]
    assert item["scope_label"] == "页面设置（全部节）"
    assert item["template_value"] == 4.0
    assert item["proposal"]["expected"] == 4.0
    assert item["proposal"]["tol"] == 0.2  # 厘米类属性的容差下限


def test_derive_narrows_allowed_set(factory):
    """prop_in_set 类规则（允许集合）应被收窄为模板实际使用的那一个值。"""
    doc = factory.new()
    factory.paragraph(doc, "文档题目", east="黑体", size=22, indent=None, line=None)
    for index in range(5):
        factory.paragraph(doc, f"第 {index} 段用楷体书写的正文。" * 3, east="楷体")
    model = parse_docx(str(factory.save(doc, "kaiti.docx")))

    item = _proposal_map(tpl.derive_proposals(parse_ruleset(_FONT_SET_YAML), model))["t.body.font"]
    assert item["template_value"] == "楷体"
    assert item["proposal"] == {"allowed": ["楷体"]}
    assert item["status"] == "proposed"


def test_derive_line_spacing_band_centered_on_template(factory):
    doc = factory.new()
    factory.paragraph(doc, "文档题目", east="黑体", size=22, indent=None, line=None)
    for index in range(5):
        factory.paragraph(doc, f"第 {index} 段正文，使用固定 1.5 倍行距。" * 3, line=1.5)
    model = parse_docx(str(factory.save(doc, "line.docx")))

    item = _proposal_map(tpl.derive_proposals(parse_ruleset(_LINE_YAML), model))["t.body.line"]
    assert item["template_value"] == 1.5
    assert item["proposal"]["min"] == 1.35
    assert item["proposal"]["max"] == 1.65


def test_derive_reports_reference_for_consistency_rules(factory):
    """一致性规则没有阈值可改，但模板实测值仍应被量出来当作参考。"""
    doc = factory.new()
    factory.paragraph(doc, "文档题目", east="黑体", size=22, indent=None, line=None)
    for index in range(6):
        factory.paragraph(doc, f"第 {index} 段正文。" * 4)
    model = parse_docx(str(factory.save(doc, "consistency.docx")))

    item = _proposal_map(tpl.derive_proposals(parse_ruleset(_CONSISTENCY_YAML), model))["t.body.font"]
    assert item["status"] == "reference"
    assert item["template_value"] == "宋体"
    assert item["applicable"] is False
    assert "参照" in item["message"]


def test_derive_marks_non_threshold_rules_not_applicable(factory):
    doc = factory.new()
    factory.paragraph(doc, "文档题目", east="黑体", size=22, indent=None, line=None)
    factory.paragraph(doc, "正文。" * 20)
    model = parse_docx(str(factory.save(doc, "na.docx")))

    result = tpl.derive_proposals(parse_ruleset(_STRUCTURE_YAML), model)
    item = _proposal_map(result)["t.heading.skip"]
    assert item["status"] == "not_applicable"
    assert "阈值" in item["message"]
    assert result["counts"]["not_applicable"] == 1


def test_derive_skips_when_scope_missing(factory):
    """规则指向「参考文献」，但模板里没有参考文献 → 应明确说没量到，而不是编一个值。"""
    doc = factory.new()
    factory.paragraph(doc, "文档题目", east="黑体", size=22, indent=None, line=None)
    factory.paragraph(doc, "正文，无参考文献章节。" * 3)
    model = parse_docx(str(factory.save(doc, "noref.docx")))

    item = _proposal_map(tpl.derive_proposals(parse_ruleset(_REF_YAML), model))["t.ref.size"]
    assert item["status"] == "skipped"
    assert item["template_value"] is None
    assert "参考文献" in item["message"]


def test_profile_template_lists_roles_and_sections(factory):
    doc = factory.new()
    factory.paragraph(doc, "文档题目", east="黑体", size=22, indent=None, line=None)
    factory.heading(doc, "第一章 绪论", level=1)
    factory.paragraph(doc, "正文段落内容。" * 5)
    model = parse_docx(str(factory.save(doc, "profile.docx")))

    profile = tpl.profile_template(model)
    labels = [role["label"] for role in profile["roles"]]
    assert "论文题目" in labels and "一级标题" in labels and "正文段落" in labels
    body = [role for role in profile["roles"] if role["label"] == "正文段落"][0]
    face = [cell for cell in body["cells"] if cell["prop"] == "font_eastasia"][0]
    assert face["value"] == "宋体"
    assert profile["sections"][0]["page"].startswith("21.0")


# ---------------------------------------------------------------- 写回参数


def test_apply_updates_merges_and_survives_roundtrip():
    ruleset = parse_ruleset(_MINI_YAML)
    updated, outcome = tpl.apply_updates(
        ruleset,
        [{"rule_id": "t.body.size", "params": {"expected": 14, "tol": 0.7}}],
        source_name="学校模板.docx",
    )
    assert len(outcome["applied"]) == 1
    assert outcome["applied"][0]["before"]["expected"] == 12
    rule = [item for item in updated.rules if item.id == "t.body.size"][0]
    assert rule.params["expected"] == 14

    # 序列化 → 重新解析之后仍然是 14，否则「保存成功但重启失效」会很难查
    again = parse_ruleset(dump_ruleset(updated))
    assert [item for item in again.rules if item.id == "t.body.size"][0].params["expected"] == 14
    # 说明里留下模板出处，便于追溯
    assert "学校模板.docx" in again.description


def test_apply_updates_rejects_one_without_breaking_the_batch():
    ruleset = parse_ruleset(_MINI_YAML)
    _, outcome = tpl.apply_updates(
        ruleset,
        [
            {"rule_id": "t.body.size", "params": {"expected": 13}},
            {"rule_id": "t.body.size", "params": {"expected": 9999}},  # 越界
            {"rule_id": "no.such.rule", "params": {"expected": 1}},
        ],
        source_name=None,
    )
    assert len(outcome["applied"]) == 1
    assert len(outcome["rejected"]) == 2
    assert "超出合理范围" in outcome["rejected"][0]["message"]
    assert outcome["rejected"][0]["hint"]


@pytest.mark.parametrize(
    "patch, keyword",
    [
        ({"allowed": []}, "不能为空"),
        ({"expected": "不是数字"}, "必须是数字"),
        ({"tol": -1}, "不能为负数"),
        ({"tol": 99}, "过大"),
        ({"prop": "font_size"}, "不支持"),
        ({"min": 1, "max": 0}, "最小"),  # 先撞上 min>max（这条规则是 prop_equals，故走 range 分支）
    ],
)
def test_validate_patch_rejects_bad_values(patch, keyword):
    ruleset = parse_ruleset(_MINI_YAML)
    rule = [item for item in ruleset.rules if item.id == "t.body.size"][0]
    with pytest.raises(RulesetInvalid) as excinfo:
        tpl.validate_patch(rule, patch)
    assert keyword in excinfo.value.message or keyword in (excinfo.value.hint or "")


def test_validate_patch_checks_range_order():
    ruleset = parse_ruleset(_LINE_YAML)
    rule = [item for item in ruleset.rules if item.id == "t.body.line"][0]
    with pytest.raises(RulesetInvalid) as excinfo:
        tpl.validate_patch(rule, {"min": 2.0, "max": 1.0})
    assert "不能大于" in excinfo.value.message


def test_validate_patch_rejects_enums_outside_known_values():
    ruleset = parse_ruleset(_MINI_YAML)
    ruleset.rules.append(
        parse_ruleset(
            """
meta: {id: x, name: x}
rules:
  - id: t.align
    name: 正文对齐
    category: layout
    severity: info
    checker: prop_equals
    params: {scope: body, prop: align, expected: both}
"""
        ).rules[0]
    )
    rule = [item for item in ruleset.rules if item.id == "t.align"][0]
    assert tpl.validate_patch(rule, {"expected": "center"}) == {"expected": "center"}
    with pytest.raises(RulesetInvalid):
        tpl.validate_patch(rule, {"expected": "居中"})


# ---------------------------------------------------------------- 快速调整分组


def test_quick_groups_covers_existing_thresholds(factory):
    from app.core.rules import load_builtin_ruleset
    from pathlib import Path

    ruleset = load_builtin_ruleset(Path(__file__).resolve().parents[1] / "rules" / "default.yaml")
    groups = tpl.quick_groups(ruleset)
    names = [group["group"] for group in groups]
    assert "正文" in names and "页边距" in names

    flat = {item["rule_id"]: item for group in groups for item in group["items"]}
    assert "layout.body.size" in flat and "layout.body.line_spacing" in flat

    size = flat["layout.body.size"]
    assert size["kind"] == "number"
    assert size["unit"] == "磅"
    assert size["value"] == 12
    assert size["tol"] == 0.5
    assert size["limits"] == [5.0, 100.0]

    # 行距的两种口径必须各自给出合适的边界，否则固定 22 磅会被界面上限截断
    assert flat["layout.body.line_spacing"]["limits"] == [0.5, 5.0]
    assert flat["layout.body.line_spacing_exact"]["limits"] == [8.0, 80.0]

    # 结构类规则不应出现在「快速调整」里（它们没有阈值）
    assert "structure.heading.no_skip" not in flat


def test_quick_groups_never_exposes_params_outside_whitelist():
    from app.core.rules import load_builtin_ruleset
    from pathlib import Path

    ruleset = load_builtin_ruleset(Path(__file__).resolve().parents[1] / "rules" / "default.yaml")
    for group in tpl.quick_groups(ruleset):
        for item in group["items"]:
            assert item["kind"] in ("number", "range", "enum", "multi")
            if item["kind"] == "multi":
                assert item["value"], "允许集合不能为空"


# ---------------------------------------------------------------- 测试用规则集

_MINI_YAML = """
meta: {id: mini, name: 最小规则集, version: "1.0"}
rules:
  - id: t.body.size
    name: 正文字号
    category: layout
    severity: error
    checker: prop_equals
    params: {scope: body, prop: font_size, expected: 12, tol: 0.5}
"""

_MARGIN_YAML = """
meta: {id: margin, name: 页边距规则集, version: "1.0"}
rules:
  - id: t.margin.left
    name: 左页边距
    category: layout
    severity: error
    checker: prop_equals
    params: {target: section, prop: margin_left_cm, expected: 3.0, tol: 0.2}
"""

_FONT_SET_YAML = """
meta: {id: fontset, name: 字体规则集, version: "1.0"}
rules:
  - id: t.body.font
    name: 正文字体
    category: layout
    severity: error
    checker: prop_in_set
    params: {scope: body, prop: font_eastasia, allowed: [宋体, 仿宋, 楷体]}
"""

_LINE_YAML = """
meta: {id: line, name: 行距规则集, version: "1.0"}
rules:
  - id: t.body.line
    name: 正文行距
    category: layout
    severity: warning
    checker: prop_range
    params: {scope: body, prop: line_spacing, min: 1.2, max: 2.0, kind_prop: line_spacing_kind, kind_value: multiple}
"""

_CONSISTENCY_YAML = """
meta: {id: cons, name: 一致性规则集, version: "1.0"}
rules:
  - id: t.body.font
    name: 正文字体一致
    category: consistency
    severity: warning
    checker: consistency
    params: {scope: body, prop: font_eastasia}
"""

_STRUCTURE_YAML = """
meta: {id: struct, name: 结构规则集, version: "1.0"}
rules:
  - id: t.heading.skip
    name: 标题不跳级
    category: structure
    severity: error
    checker: heading_no_skip
    params: {}
"""

_REF_YAML = """
meta: {id: ref, name: 参考文献规则集, version: "1.0"}
rules:
  - id: t.ref.size
    name: 参考文献字号
    category: layout
    severity: info
    checker: prop_equals
    params: {scope: reference, prop: font_size, expected: 10.5, tol: 0.5}
"""
