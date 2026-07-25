"""RBAC：dev 级令牌认证与五角色权限矩阵。

五角色：operator(运营)、reviewer(话术审核)、auditor(合规审计)、
supervisor(坐席班长)、admin(系统管理员)。
令牌为进程内随机串（dev 级），重启即失效；生产形态由 Java BFF 网关实现。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Coroutine

from fastapi import Depends, Header, HTTPException, Query

VALID_ROLES = ("operator", "reviewer", "auditor", "supervisor", "admin")


@dataclass(frozen=True)
class SessionUser:
    """已认证会话主体。"""

    username: str
    role: str


_tokens: dict[str, SessionUser] = {}
_lock = threading.Lock()


def verify_user(users: list[dict[str, Any]], username: str, password: str) -> SessionUser | None:
    """校验用户名密码，命中则返回会话主体。"""
    for u in users:
        if u.get("username") == username and u.get("password") == password:
            return SessionUser(username=username, role=u.get("role", "operator"))
    return None


def issue_token(user: SessionUser) -> str:
    """签发进程内令牌。"""
    token = uuid.uuid4().hex
    with _lock:
        _tokens[token] = user
    return token


def _lookup(token: str) -> SessionUser | None:
    with _lock:
        return _tokens.get(token)


def _resolve_user(authorization: str | None, query_token: str | None) -> SessionUser:
    """从 Authorization 头（优先）或 query token（SSE EventSource 无法带请求头）解析用户。"""
    token: str | None = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
    elif query_token:
        token = query_token
    if not token:
        raise HTTPException(status_code=401, detail="缺少或非法的 Bearer 令牌")
    user = _lookup(token)
    if user is None:
        raise HTTPException(status_code=401, detail="令牌无效或已过期")
    return user


async def get_current_user(authorization: str | None = Header(default=None)) -> SessionUser:
    """依赖注入：解析 ``Authorization: Bearer <token>`` 得到当前用户。"""
    return _resolve_user(authorization, None)


def require_roles(
    *roles: str,
    allow_query_token: bool = False,
) -> Callable[..., Coroutine[Any, Any, SessionUser]]:
    """依赖工厂：校验当前用户角色是否在允许集合内，否则 403。

    ``allow_query_token=True`` 时额外接受 ``?token=`` 查询参数——
    原生 EventSource 无法设置请求头，SSE 端点需要此入口（仅限读类端点使用）。
    """

    async def _dep(
        authorization: str | None = Header(default=None),
        token: str | None = Query(default=None),
    ) -> SessionUser:
        user = _resolve_user(authorization, token if allow_query_token else None)
        if user.role not in roles:
            raise HTTPException(
                status_code=403,
                detail=f"角色 {user.role} 无权执行该操作（需要 {'/'.join(roles)}）",
            )
        return user

    return _dep
