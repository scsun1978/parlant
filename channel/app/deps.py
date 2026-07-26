"""路由共享的依赖注入入口：从 app.state 取配置/存储/上游客户端（便于测试注入假实现）。"""

from __future__ import annotations

from fastapi import Header, HTTPException, Request

from admin.app.parlant_client import ParlantClient
from admin.app.store import Store
from channel.app.auth import lookup_user
from channel.app.config import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_store(request: Request) -> Store:
    return request.app.state.store  # type: ignore[no-any-return]


def get_parlant(request: Request) -> ParlantClient:
    return request.app.state.parlant  # type: ignore[no-any-return]


async def get_user_id(request: Request, authorization: str | None = Header(default=None)) -> str:
    """bearer 校验依赖：token → user_id（无效 401）。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少或非法的 Bearer 令牌")
    user_id = lookup_user(request.app.state.store, authorization.removeprefix("Bearer ").strip())
    if user_id is None:
        raise HTTPException(status_code=401, detail="令牌无效或已过期")
    return user_id
