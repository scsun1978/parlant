"""渠道 BFF 应用入口：组装配置、存储、上游客户端与消息通道路由。

启动：``uvicorn channel.app.main:app --port 9100``
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from admin.app.parlant_client import ParlantClient, UpstreamError
from admin.app.store import Store, create_store
from channel.app.config import Settings, load_settings
from channel.app.routes import messages

logger = logging.getLogger("channel.main")


def create_app(
    settings: Settings | None = None,
    store: Store | None = None,
    parlant: ParlantClient | None = None,
) -> FastAPI:
    """应用工厂：依赖均可注入，测试可传入内存存储与假上游。"""
    settings = settings or load_settings()
    store = store or create_store(settings)  # type: ignore[arg-type]  # mongo_url/mongo_db 鸭子类型兼容
    parlant = parlant or ParlantClient(settings.parlant_base_url, timeout=settings.upstream_timeout)

    app = FastAPI(title="PVG 渠道 BFF（旅客侧）", version="0.1.0")
    app.state.settings = settings
    app.state.store = store
    app.state.parlant = parlant

    @app.exception_handler(UpstreamError)
    async def upstream_error_handler(request: Request, exc: UpstreamError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @app.get("/health")
    def health() -> dict[str, Any]:
        """双向探测：Parlant / Mongo（内存降级时如实标注）。"""
        parlant_ok = parlant.healthy()
        mongo = "up" if store.ping() else "down"
        if store.name == "memory":
            mongo = "memory"
        return {
            "status": "ok" if parlant_ok and mongo != "down" else "degraded",
            "parlant": "up" if parlant_ok else "down",
            "mongo": mongo,
        }

    app.include_router(messages.router)

    # 内容审核前置层（ADR-0005）：规则种子 + 治理路由
    from channel.app import moderation as mod
    from channel.app.routes import moderation as moderation_routes

    mod.seed_rules(store)
    app.include_router(moderation_routes.router)
    return app


app = create_app()
