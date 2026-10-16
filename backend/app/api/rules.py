"""规则库接口：查看、就地修改、单条启停、恢复内置默认、阈值快速调整。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from fastapi.responses import PlainTextResponse

from ..core.rules import dump_ruleset
from ..errors import NotFound, RulesetInvalid
from ..services import DocFmtService

router = APIRouter(tags=["rules"])


def get_service(request: Request) -> DocFmtService:
    return request.app.state.service


class RulesetText(BaseModel):
    yaml_text: str


class ToggleRequest(BaseModel):
    rule_id: str
    enabled: bool


class ParamsPatch(BaseModel):
    """只允许阈值类参数（expected / tol / min / max / allowed），服务层会再校验一次。"""

    params: dict


class QuickUpdateRequest(BaseModel):
    updates: list[dict] = Field(default_factory=list)


@router.get("/rules", summary="规则集列表")
def list_rulesets(request: Request) -> dict:
    items = get_service(request).list_rulesets()
    return {"total": len(items), "items": items}


@router.get("/rules/{ruleset_id}", summary="规则集详情（含全部规则）")
def ruleset_detail(request: Request, ruleset_id: str) -> dict:
    ruleset = get_service(request).load_ruleset(ruleset_id)
    return ruleset.to_dict()


@router.get("/rules/{ruleset_id}/yaml", summary="导出规则集 YAML 原文", response_class=PlainTextResponse)
def ruleset_yaml(request: Request, ruleset_id: str) -> PlainTextResponse:
    ruleset = get_service(request).load_ruleset(ruleset_id)
    return PlainTextResponse(dump_ruleset(ruleset), media_type="text/yaml; charset=utf-8")


@router.put("/rules/{ruleset_id}", summary="保存规则集（先校验后落库）")
def save_ruleset(request: Request, ruleset_id: str, payload: RulesetText) -> dict:
    ruleset = get_service(request).save_ruleset(ruleset_id, payload.yaml_text)
    return {"saved": True, "ruleset": ruleset.to_dict(include_rules=False)}


@router.post("/rules/{ruleset_id}/toggle", summary="启用/停用单条规则")
def toggle_rule(request: Request, ruleset_id: str, payload: ToggleRequest) -> dict:
    ruleset = get_service(request).toggle_rule(ruleset_id, payload.rule_id, payload.enabled)
    return {"saved": True, "ruleset": ruleset.to_dict()}


@router.post("/rules/{ruleset_id}/reset", summary="恢复内置默认规则集")
def reset_ruleset(request: Request, ruleset_id: str) -> dict:
    service = get_service(request)
    if ruleset_id != "default":
        raise NotFound("只有内置规则集（default）支持恢复默认。")
    ruleset = service.reset_builtin_ruleset()
    return {"reset": True, "ruleset": ruleset.to_dict(include_rules=False)}


# ---------------------------------------------------------------- 阈值快速调整


@router.get("/rules/{ruleset_id}/quick", summary="可快速调整的阈值（按用途分组）")
def quick_groups(request: Request, ruleset_id: str) -> dict:
    return get_service(request).quick_groups(ruleset_id)


@router.put("/rules/{ruleset_id}/quick", summary="批量保存阈值调整")
def quick_update(request: Request, ruleset_id: str, payload: QuickUpdateRequest) -> dict:
    ruleset, outcome = get_service(request).update_rule_params(ruleset_id, payload.updates)
    return {
        "saved": bool(outcome["applied"]),
        "applied": len(outcome["applied"]),
        "rejected": len(outcome["rejected"]),
        "details": outcome["applied"],
        "problems": outcome["rejected"],
        "ruleset": ruleset.to_dict(include_rules=False),
        "yaml_text": dump_ruleset(ruleset),
    }


@router.patch("/rules/{ruleset_id}/rules/{rule_id}/params", summary="修改单条规则的阈值")
def patch_rule_params(request: Request, ruleset_id: str, rule_id: str, payload: ParamsPatch) -> dict:
    service = get_service(request)
    # 先确认规则存在：不存在是「资源找不到」（404），不该混在「参数不合法」（422）里
    if not any(rule.id == rule_id for rule in service.load_ruleset(ruleset_id).rules):
        raise NotFound(f"规则「{rule_id}」不存在。", hint="规则集可能已被改动，请刷新页面后重试。")

    ruleset, outcome = service.update_rule_params(
        ruleset_id, [{"rule_id": rule_id, "params": payload.params}]
    )
    if not outcome["applied"]:
        # 参数没通过校验 → 直接把原因当错误抛出，前端能显示可操作的提示
        problem = outcome["rejected"][0] if outcome["rejected"] else {}
        raise RulesetInvalid(problem.get("message") or "参数修改未生效。", hint=problem.get("hint"))
    return {
        "saved": True,
        "rule_id": rule_id,
        "detail": outcome["applied"][0],
        "ruleset": ruleset.to_dict(include_rules=False),
    }
