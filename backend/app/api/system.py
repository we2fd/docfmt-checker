"""系统类接口：健康检查、运行配置、统计、修复动作清单。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..config import Settings
from ..core import fixer
from ..core.engine import CHECKER_TABLE, SCOPE_LABELS
from ..core.rules import CATEGORIES, CHECKERS, SEVERITIES
from ..services import DocFmtService

router = APIRouter(tags=["system"])


def get_service(request: Request) -> DocFmtService:
    return request.app.state.service


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


@router.get("/health", summary="健康检查")
def health(request: Request) -> dict:
    service = get_service(request)
    rows = service.list_documents()
    return {
        "status": "ok",
        "documents": len(rows),
        "checked": len([row for row in rows if row.get("score") is not None]),
        "rulesets": len(service.list_rulesets()),
    }


@router.get("/config", summary="运行配置（前端据此渲染文案与限制）")
def config(request: Request) -> dict:
    settings = get_settings(request)
    service = get_service(request)
    ruleset = service.load_ruleset()
    return {
        "app_name": settings.app_name,
        "app_code": settings.app_code,
        "version": settings.version,
        "max_upload_mb": settings.max_upload_mb,
        "allowed_extensions": list(settings.allowed_extensions),
        "known_unsupported": list(settings.known_unsupported),
        "data_dir": str(settings.data_dir),
        "default_ruleset": settings.default_ruleset,
        "ruleset": ruleset.to_dict(include_rules=False),
        "categories": [{"key": key, "label": label} for key, label in CATEGORIES.items()],
        "severities": [
            {"key": key, "label": value["label"], "weight": value["weight"]} for key, value in SEVERITIES.items()
        ],
        "scopes": [{"key": key, "label": label} for key, label in SCOPE_LABELS.items()],
        "checkers": [{"key": key, "summary": value["summary"]} for key, value in CHECKERS.items()],
        "checker_implemented": sorted(CHECKER_TABLE.keys()),
        "fix_actions": fixer.available_actions(),
        "honesty": {
            "scope": "只检查格式与结构规范，不判断学术内容质量",
            "unknown_policy": "无法自动判定的项目标注为「待人工确认」，不计入扣分",
        },
    }


@router.get("/stats", summary="首页统计（全部来自本机真实数据）")
def stats(request: Request) -> dict:
    return get_service(request).stats()
