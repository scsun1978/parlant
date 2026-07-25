"""认证路由：POST /api/auth/login（登录失败也追加审计）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from admin.app import audit
from admin.app.auth import issue_token, verify_user
from admin.app.config import Settings
from admin.app.deps import get_settings, get_store
from admin.app.store import Store

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """登录请求。"""

    username: str
    password: str


class LoginResponse(BaseModel):
    """登录响应：进程内令牌 + 角色。"""

    token: str
    username: str
    role: str


@router.post("/login", response_model=LoginResponse)
def login(
    body: LoginRequest,
    settings: Settings = Depends(get_settings),
    store: Store = Depends(get_store),
) -> LoginResponse:
    user = verify_user(settings.users, body.username, body.password)
    if user is None:
        audit.record(
            store,
            actor=body.username,
            role="unknown",
            action="auth.login",
            after={"result": "failed"},
        )
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = issue_token(user)
    audit.record(
        store,
        actor=user.username,
        role=user.role,
        action="auth.login",
        after={"result": "success"},
    )
    return LoginResponse(token=token, username=user.username, role=user.role)
