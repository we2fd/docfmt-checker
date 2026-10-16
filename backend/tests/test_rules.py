"""规则集解析与校验的单元测试。"""

from __future__ import annotations

import pytest

from app.core.rules import CATEGORIES, CHECKERS, dump_ruleset, parse_ruleset
from app.errors import RulesetInvalid


def test_builtin_ruleset_is_valid(ruleset):
    assert len(ruleset.rules) == 56
    assert ruleset.id == "default"
    assert all(rule.checker in CHECKERS for rule in ruleset.rules)
    assert all(rule.category in CATEGORIES for rule in ruleset.rules)
    assert all(rule.suggestion for rule in ruleset.rules), "每条规则都应给出可操作的整改建议"


def test_rule_ids_are_unique(ruleset):
    ids = [rule.id for rule in ruleset.rules]
    assert len(ids) == len(set(ids))


def test_category_coverage(ruleset):
    counts = {key: 0 for key in CATEGORIES}
    for rule in ruleset.rules:
        counts[rule.category] += 1
    assert all(value >= 5 for value in counts.values()), counts


def test_parse_minimal_ruleset():
    ruleset = parse_ruleset(
        """
meta: {id: demo, name: 演示, version: "2.0", description: 说明}
rules:
  - id: a.b
    name: 名称
    category: layout
    severity: error
    checker: prop_equals
    params: {prop: font_size, expected: 12}
"""
    )
    assert ruleset.id == "demo"
    assert ruleset.version == "2.0"
    assert ruleset.rules[0].weight == 3


def test_reject_duplicate_ids():
    with pytest.raises(RulesetInvalid) as excinfo:
        parse_ruleset(
            """
meta: {id: demo, name: 演示}
rules:
  - {id: a, name: 甲, category: layout, checker: prop_equals, params: {prop: font_size, expected: 12}}
  - {id: a, name: 乙, category: layout, checker: prop_equals, params: {prop: font_size, expected: 12}}
"""
        )
    assert "重复" in str(excinfo.value.detail)


def test_reject_unknown_checker():
    with pytest.raises(RulesetInvalid) as excinfo:
        parse_ruleset(
            """
meta: {id: demo, name: 演示}
rules:
  - {id: a, name: 甲, category: layout, checker: not_exists, params: {}}
"""
        )
    assert "not_exists" in str(excinfo.value.detail)


def test_reject_missing_required_param():
    with pytest.raises(RulesetInvalid) as excinfo:
        parse_ruleset(
            """
meta: {id: demo, name: 演示}
rules:
  - {id: a, name: 甲, category: layout, checker: prop_equals, params: {prop: font_size}}
"""
        )
    assert "expected" in str(excinfo.value.detail)


def test_reject_bad_yaml_gives_actionable_hint():
    with pytest.raises(RulesetInvalid) as excinfo:
        parse_ruleset("meta: {id: demo}\nrules:\n  - id: a\n    name: 值里有冒号: 空格\n")
    assert "双引号" in (excinfo.value.hint or "")


def test_dump_and_parse_roundtrip(ruleset):
    text = dump_ruleset(ruleset)
    again = parse_ruleset(text, fallback_id="default")
    assert [rule.id for rule in again.rules] == [rule.id for rule in ruleset.rules]
    assert again.rules[0].params == ruleset.rules[0].params


def test_disabled_rule_is_excluded_from_enabled(ruleset):
    import copy

    clone = copy.deepcopy(ruleset)
    clone.rules[0].enabled = False
    assert len(clone.enabled_rules) == len(clone.rules) - 1
