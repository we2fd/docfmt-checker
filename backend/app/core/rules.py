"""规则集加载与校验。

设计要点（答辩可讲）：**规则是数据，不是代码**。
新增一条「页眉字号必须是 9 磅」这种规则，只需要在 YAML 里加 8 行，
不需要改引擎、不需要重新测试引擎逻辑——这是本项目「可配置化」的核心。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..errors import RulesetInvalid

CATEGORIES: dict[str, str] = {
    "layout": "排版规范",
    "structure": "结构完整性",
    "consistency": "全文一致性",
    "content": "内容规范",
}

SEVERITIES: dict[str, dict[str, Any]] = {
    "error": {"label": "错误", "weight": 3, "color": "#E24B4A"},
    "warning": {"label": "警告", "weight": 2, "color": "#EF9F27"},
    "info": {"label": "提示", "weight": 1, "color": "#378ADD"},
}

CHECKERS: dict[str, dict[str, Any]] = {
    "prop_equals": {"summary": "属性必须等于给定值", "required": ["prop", "expected"]},
    "prop_range": {"summary": "属性必须落在区间内", "required": ["prop"]},
    "prop_in_set": {"summary": "属性必须属于允许集合", "required": ["prop", "allowed"]},
    "consistency": {"summary": "同类对象该属性必须全篇一致", "required": ["prop"]},
    "heading_no_skip": {"summary": "标题层级不得跳级", "required": []},
    "heading_style_used": {"summary": "标题应使用 Word 标题样式", "required": []},
    "numbering_continuous": {"summary": "图表编号应连续且唯一", "required": ["prefix"]},
    "caption_referenced": {"summary": "图表编号应在正文被引用", "required": []},
    "required_section": {"summary": "必须包含指定章节", "required": ["names"]},
    "length_range": {"summary": "字数/条目数应落在区间内", "required": ["target", "min", "max"]},
    "punctuation_mix": {"summary": "中文语境不得混用英文标点", "required": []},
    "reference_format": {"summary": "参考文献条目应含基本著录要素", "required": []},
    "empty_heading": {"summary": "标题下不应缺正文", "required": []},
    "paragraph_run_uniform": {"summary": "同一段落内字号/字体应一致", "required": []},
    "blank_line_run": {"summary": "不应出现连续空段落", "required": ["max_consecutive"]},
    "page_number_present": {"summary": "页脚应有页码", "required": []},
}


@dataclass
class Rule:
    id: str
    name: str
    category: str
    severity: str
    checker: str
    params: dict[str, Any] = field(default_factory=dict)
    suggestion: str = ""
    reference: str = ""
    enabled: bool = True
    scope_label: str = ""

    @property
    def weight(self) -> int:
        return SEVERITIES.get(self.severity, SEVERITIES["info"])["weight"]

    @property
    def category_label(self) -> str:
        return CATEGORIES.get(self.category, self.category)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "category_label": self.category_label,
            "severity": self.severity,
            "severity_label": SEVERITIES.get(self.severity, {}).get("label", self.severity),
            "checker": self.checker,
            "params": self.params,
            "suggestion": self.suggestion,
            "reference": self.reference,
            "enabled": self.enabled,
            "scope_label": self.scope_label,
        }


@dataclass
class Ruleset:
    id: str
    name: str
    version: str = "1.0"
    description: str = ""
    rules: list[Rule] = field(default_factory=list)

    @property
    def enabled_rules(self) -> list[Rule]:
        return [rule for rule in self.rules if rule.enabled]

    def by_category(self) -> dict[str, list[Rule]]:
        result: dict[str, list[Rule]] = {key: [] for key in CATEGORIES}
        for rule in self.rules:
            result.setdefault(rule.category, []).append(rule)
        return result

    def to_dict(self, include_rules: bool = True) -> dict[str, Any]:
        data = {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "rule_count": len(self.rules),
            "enabled_count": len(self.enabled_rules),
            "categories": [
                {"key": key, "label": label, "count": len([r for r in self.rules if r.category == key])}
                for key, label in CATEGORIES.items()
            ],
        }
        if include_rules:
            data["rules"] = [rule.to_dict() for rule in self.rules]
        return data


def parse_ruleset(yaml_text: str, fallback_id: str = "default") -> Ruleset:
    """把 YAML 文本解析成 Ruleset，并对每条规则做结构校验。"""
    try:
        raw = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise RulesetInvalid(
            f"YAML 语法错误：{exc}",
            hint=(
                "常见原因有三个：① 值里出现半角冒号 + 空格（如「年份: 页码」），"
                "请给这类值整体加上双引号；② 缩进用了 Tab，请改成空格；"
                "③ 同一层级缩进不一致。"
            ),
        ) from exc

    if not isinstance(raw, dict):
        raise RulesetInvalid("规则集根节点必须是对象（包含 meta 与 rules 两个键）。")

    meta = raw.get("meta") or {}
    items = raw.get("rules")
    if not isinstance(items, list) or not items:
        raise RulesetInvalid("rules 必须是非空列表。")

    errors: list[str] = []
    rules: list[Rule] = []
    seen: set[str] = set()

    for position, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            errors.append(f"第 {position} 条规则不是对象。")
            continue
        rule_id = str(item.get("id") or "").strip()
        if not rule_id:
            errors.append(f"第 {position} 条规则缺少 id。")
            continue
        if rule_id in seen:
            errors.append(f"规则 id 重复：{rule_id}")
            continue
        seen.add(rule_id)

        checker = str(item.get("checker") or "").strip()
        if checker not in CHECKERS:
            errors.append(f"{rule_id}：未知的 checker「{checker}」，可用值见规则设计文档。")
            continue
        category = str(item.get("category") or "").strip()
        if category not in CATEGORIES:
            errors.append(f"{rule_id}：未知的 category「{category}」。")
            continue
        severity = str(item.get("severity") or "warning").strip()
        if severity not in SEVERITIES:
            errors.append(f"{rule_id}：未知的 severity「{severity}」。")
            continue

        params = item.get("params") or {}
        if not isinstance(params, dict):
            errors.append(f"{rule_id}：params 必须是对象。")
            continue
        missing = [key for key in CHECKERS[checker]["required"] if key not in params]
        if missing:
            errors.append(f"{rule_id}：checker「{checker}」缺少必要参数 {'、'.join(missing)}。")
            continue

        rules.append(
            Rule(
                id=rule_id,
                name=str(item.get("name") or rule_id),
                category=category,
                severity=severity,
                checker=checker,
                params=params,
                suggestion=str(item.get("suggestion") or ""),
                reference=str(item.get("reference") or ""),
                enabled=bool(item.get("enabled", True)),
                scope_label=str(item.get("scope_label") or params.get("scope") or ""),
            )
        )

    if errors:
        raise RulesetInvalid("规则集校验未通过", hint="按下面的清单逐条修正后重新保存。", detail=errors)

    return Ruleset(
        id=str(meta.get("id") or fallback_id),
        name=str(meta.get("name") or "未命名规则集"),
        version=str(meta.get("version") or "1.0"),
        description=str(meta.get("description") or ""),
        rules=rules,
    )


def dump_ruleset(ruleset: Ruleset) -> str:
    """把 Ruleset 序列化回 YAML（保存配置时使用，保持字段顺序可读）。"""
    payload: dict[str, Any] = {
        "meta": {
            "id": ruleset.id,
            "name": ruleset.name,
            "version": ruleset.version,
            "description": ruleset.description,
        },
        "rules": [],
    }
    for rule in ruleset.rules:
        payload["rules"].append(
            {
                "id": rule.id,
                "name": rule.name,
                "category": rule.category,
                "severity": rule.severity,
                "checker": rule.checker,
                "params": rule.params,
                "suggestion": rule.suggestion,
                "reference": rule.reference,
                "enabled": rule.enabled,
                "scope_label": rule.scope_label,
            }
        )
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=100)


def load_builtin_ruleset(path: Path) -> Ruleset:
    return parse_ruleset(path.read_text(encoding="utf-8"), fallback_id=path.stem)
