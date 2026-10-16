"""参考模板接口：上传模板 → 逐条推导规则阈值 → 用户勾选后写回规则集。

三个动作严格分开，避免「上传即改规则」这种不可控行为：
    1. profile  上传 + 推导（只读，不落任何规则）
    2. derive   对已上传的模板重新推导（用户改过规则后可再来一次）
    3. apply    把用户勾选的提案写回规则集
"""

from __future__ import annotations

from fastapi import APIRouter, File, Request, UploadFile
from pydantic import BaseModel, Field

from ..core.rules import dump_ruleset
from ..services import DocFmtService

router = APIRouter(tags=["templates"])


def get_service(request: Request) -> DocFmtService:
    return request.app.state.service


class TemplateUpdate(BaseModel):
    rule_id: str
    params: dict


class ApplyRequest(BaseModel):
    ruleset_id: str | None = None
    updates: list[TemplateUpdate] = Field(default_factory=list)
    # 记下「这套阈值来自哪份模板」，会写进规则集说明里，便于日后追溯
    source_name: str | None = None


def _apply(service: DocFmtService, payload: ApplyRequest) -> dict:
    ruleset, outcome = service.update_rule_params(
        payload.ruleset_id,
        [item.model_dump() for item in payload.updates],
        source_name=payload.source_name,
    )
    return {
        "saved": bool(outcome["applied"]),
        "applied": len(outcome["applied"]),
        "rejected": len(outcome["rejected"]),
        "details": outcome["applied"],
        "problems": outcome["rejected"],
        "ruleset": ruleset.to_dict(include_rules=False),
        "yaml_text": dump_ruleset(ruleset),
    }


@router.get("/templates", summary="已上传的参考模板列表")
def list_templates(request: Request) -> dict:
    items = get_service(request).list_templates()
    return {"total": len(items), "items": items}


@router.post("/templates/profile", summary="上传参考模板并推导规则阈值（不修改规则）")
async def profile_template(request: Request, file: UploadFile = File(...), ruleset_id: str | None = None) -> dict:
    service = get_service(request)
    content = await file.read()
    return service.profile_template(file.filename or "参考模板.docx", content, ruleset_id)


@router.post("/templates/{template_id}/derive", summary="对已上传的模板重新推导")
def derive_template(request: Request, template_id: str, ruleset_id: str | None = None) -> dict:
    return get_service(request).derive_from_template(template_id, ruleset_id)


@router.post("/templates/apply", summary="把推导结果写回规则集")
def apply_template(request: Request, payload: ApplyRequest) -> dict:
    return _apply(get_service(request), payload)


@router.delete("/templates/{template_id}", summary="删除参考模板")
def delete_template(request: Request, template_id: str) -> dict:
    outcome = get_service(request).delete_template(template_id)
    return {**outcome, "template_id": template_id}
