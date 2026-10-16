"""FastAPI 应用入口：装配路由、异常映射、静态前端与生命周期。

启动：
    uvicorn app.main:app --app-dir backend --port 8300
或：
    python -m app.main          # 在 backend 目录下执行
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, db
from .api import documents, rules, system, templates
from .config import Settings, get_settings
from .errors import DocFmtError
from .services import DocFmtService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_dirs()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init_db()
        app.state.settings = settings
        app.state.service = DocFmtService(settings)
        logger.info("格致服务已启动：%s%s", settings.host, settings.api_prefix)
        yield
        logger.info("格致服务已停止")

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "上传 .docx → 按 YAML 规则集检查排版/结构/一致性/规范性 → 输出带定位与整改建议的体检报告，"
            "并提供保守修复。"
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    api = APIRouter()
    api.include_router(system.router)
    api.include_router(documents.router)
    api.include_router(rules.router)
    api.include_router(templates.router)
    app.include_router(api, prefix=settings.api_prefix)

    @app.exception_handler(DocFmtError)
    async def _domain_error(request: Request, exc: DocFmtError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"loc": [str(part) for part in item.get("loc", ())], "msg": str(item.get("msg", "")), "type": str(item.get("type", ""))}
            for item in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={"code": "validation_error", "message": "请求参数不合法", "detail": details},
        )

    if settings.frontend_dir.exists():
        app.mount("/", StaticFiles(directory=str(settings.frontend_dir), html=True), name="frontend")
    else:  # pragma: no cover
        logger.warning("未找到前端目录：%s", settings.frontend_dir)

    return app


app = create_app()


def run() -> None:  # pragma: no cover - 便捷启动入口
    import uvicorn

    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":  # pragma: no cover
    run()
