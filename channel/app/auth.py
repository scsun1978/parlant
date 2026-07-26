"""渠道鉴权：匿名登录（device_id 脱敏）+ bearer token 校验。

数据脱敏（方案 §9 已决：PII 最小化）——只存 hash(device_id)，不存明文。
集合：channel_users（用户）、channel_tokens（令牌，dev 级随机串）。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from admin.app.store import Store

CST = timezone(timedelta(hours=8), "Asia/Shanghai")

USERS = "channel_users"
TOKENS = "channel_tokens"
SESSIONS = "channel_sessions"


def utc_now_iso() -> str:
    """UTC 当前时刻 ISO8601 字符串（字典序可比较）。"""
    return datetime.now(timezone.utc).isoformat()


def hash_device(device_id: str) -> str:
    """device_id 脱敏：sha256 全文（永不落明文）。"""
    return hashlib.sha256(device_id.encode("utf-8")).hexdigest()


def login_user(store: Store, device_id: str, resume_window_hours: int) -> dict[str, Any]:
    """匿名登录：首访建用户，返回 token 与 24h 内最近 open 会话（续聊）。"""
    device_hash = hash_device(device_id)
    users, _ = store.find(USERS, {"device_hash": device_hash}, limit=1)
    if users:
        user_id = users[0]["id"]
    else:
        user_id = uuid.uuid4().hex
        store.insert(
            USERS,
            {"id": user_id, "device_hash": device_hash, "created_at": utc_now_iso()},
        )

    token = uuid.uuid4().hex
    store.insert(TOKENS, {"token": token, "user_id": user_id, "created_at": utc_now_iso()})

    # 续聊：该用户最近一个 open 会话（status!=closed 且 last_active 在窗口内）
    resume_session_id = None
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=resume_window_hours)).isoformat()
    sessions, _ = store.find(SESSIONS, {"user_id": user_id}, sort="-last_active", limit=20)
    for s in sessions:
        if s.get("status") != "closed" and (s.get("last_active") or "") >= cutoff:
            resume_session_id = s["parlant_session_id"]
            break
    return {"channelToken": token, "user_id": user_id, "resume_session_id": resume_session_id}


def lookup_user(store: Store, token: str) -> str | None:
    """token → user_id；无效返回 None。"""
    docs, _ = store.find(TOKENS, {"token": token}, limit=1)
    return docs[0]["user_id"] if docs else None
