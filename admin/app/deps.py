"""路由共享的依赖注入入口：从 app.state 取配置/存储/上游客户端（便于测试注入假实现）。"""

from __future__ import annotations

from fastapi import Request

from admin.app.config import Settings
from admin.app.parlant_client import ParlantClient, RagClient
from admin.app.store import Store


def get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_store(request: Request) -> Store:
    return request.app.state.store  # type: ignore[no-any-return]


def get_parlant(request: Request) -> ParlantClient:
    return request.app.state.parlant  # type: ignore[no-any-return]


def get_rag(request: Request) -> RagClient:
    return request.app.state.rag  # type: ignore[no-any-return]
