"""FastAPI 应用入口：组装配置、存储、上游客户端与各路由。

启动：``uvicorn admin.app.main:app --port 9000``
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from admin.app.config import Settings, load_settings
from admin.app.parlant_client import ParlantClient, RagClient, UpstreamError
from admin.app.routes import (
    approvals,
    audit_routes,
    auth_routes,
    bad_cases,
    canned,
    guidelines,
    journeys,
    knowledge,
    sandbox,
    sessions,
    terms,
)
from admin.app.routes import rag as rag_routes
from admin.app.store import Store, create_store

logger = logging.getLogger("admin.main")


def create_app(
    settings: Settings | None = None,
    store: Store | None = None,
    parlant: ParlantClient | None = None,
    rag: RagClient | None = None,
) -> FastAPI:
    """应用工厂：依赖均可注入，测试可传入内存存储与假上游。"""
    settings = settings or load_settings()
    store = store or create_store(settings)
    parlant = parlant or ParlantClient(settings.parlant_base_url, timeout=settings.upstream_timeout)
    rag = rag or RagClient(settings.rag_url, timeout=settings.upstream_timeout)

    app = FastAPI(title="PVG 机场智能客服运营后台 BFF", version="0.1.0")
    app.state.settings = settings
    app.state.store = store
    app.state.parlant = parlant
    app.state.rag = rag

    # Appsmith/定制前端从浏览器直连 BFF：CORS（生产经 ADMIN_CORS_ORIGINS 收敛）
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(UpstreamError)
    async def upstream_error_handler(request: Request, exc: UpstreamError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @app.get("/", include_in_schema=False)
    def root() -> Any:
        """根路径重定向到会话中心（避免误访 / 得到 404）。"""
        from fastapi.responses import RedirectResponse

        return RedirectResponse(url="/app/", status_code=302)

    @app.get("/health")
    def health() -> dict[str, Any]:
        """三向探测：Parlant / RAG / Mongo（内存降级时如实标注）。"""
        parlant_ok = parlant.healthy()
        rag_ok = rag.healthy()
        mongo = "up" if store.ping() else "down"
        if store.name == "memory":
            mongo = "memory"  # MONGO_URL 未配置或连接失败的降级形态
        ok = parlant_ok and rag_ok and mongo != "down"
        return {
            "status": "ok" if ok else "degraded",
            "parlant": "up" if parlant_ok else "down",
            "rag": "up" if rag_ok else "down",
            "mongo": mongo,
        }

    app.include_router(auth_routes.router)
    app.include_router(guidelines.router)
    app.include_router(canned.router)
    app.include_router(canned.preview_router)
    app.include_router(approvals.router)
    app.include_router(terms.router)
    app.include_router(journeys.router)
    app.include_router(rag_routes.router)
    app.include_router(audit_routes.router)
    app.include_router(sessions.router)
    app.include_router(sandbox.router)
    app.include_router(bad_cases.router)
    app.include_router(knowledge.router)

    # 会话中心前端：零构建静态页（vanilla JS + 原生 SSE），挂载 /app
    web_dir = Path(__file__).resolve().parent.parent / "web"
    if web_dir.is_dir():
        app.mount("/app", StaticFiles(directory=web_dir, html=True), name="web")
    return app


app = create_app()
