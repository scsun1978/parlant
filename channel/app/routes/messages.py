"""渠道消息通道（方案 §3.1 + §5.1）：发消息（幂等+preamble 秒回）、WS 推送、降级轮询、历史。

协议决策（方案 §3.1）：同步请求只覆盖 preamble；正式答复经 WebSocket 推送，
WS 不可达时降级 GET /channel/messages/poll 增量轮询。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from admin.app.parlant_client import ParlantClient, UpstreamError
from admin.app.store import Store
from channel.app.auth import SESSIONS, login_user, lookup_user, utc_now_iso
from channel.app.config import Settings
from channel.app.deps import get_parlant, get_settings, get_store, get_user_id
from channel.app.translate import FALLBACK_TEXT, to_history, translate_events

router = APIRouter(prefix="/channel", tags=["channel"])

MESSAGES = "channel_messages"  # 幂等缓存集合
FALLBACK_EVENTS = "fallback_events"


class LoginRequest(BaseModel):
    """匿名登录请求：设备标识（仅存 hash，不落明文）。"""

    device_id: str


@router.post("/login")
def login(
    body: LoginRequest,
    settings: Settings = Depends(get_settings),
    store: Store = Depends(get_store),
) -> dict[str, Any]:
    """匿名续聊登录：首访建用户，返回 token 与 24h 内最近 open 会话。"""
    return login_user(store, body.device_id, settings.resume_window_hours)


def _open_session(store: Store, user_id: str) -> dict[str, Any] | None:
    """用户当前 open 会话映射。"""
    docs, _ = store.find(SESSIONS, {"user_id": user_id}, sort="-last_active", limit=20)
    for s in docs:
        if s.get("status") != "closed":
            return s
    return None


def _owned_session(store: Store, user_id: str, session_id: str) -> dict[str, Any]:
    """会话归属校验：所有会话级接口必须先过这关（越权 403，不泄露存在性）。"""
    docs, _ = store.find(SESSIONS, {"parlant_session_id": session_id}, limit=1)
    if not docs or docs[0]["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="无权访问该会话")
    return docs[0]


def _get_or_create_session(store: Store, parlant: ParlantClient, user_id: str, agent_id: str) -> str:
    mapping = _open_session(store, user_id)
    if mapping:
        return mapping["parlant_session_id"]
    session = parlant.create_session({"agent_id": agent_id, "title": f"channel:{user_id[:8]}"})
    sid = session["id"]
    store.insert(
        SESSIONS,
        {
            "id": uuid.uuid4().hex,
            "user_id": user_id,
            "parlant_session_id": sid,
            "status": "open",
            "created_at": utc_now_iso(),
            "last_active": utc_now_iso(),
        },
    )
    return sid


def _record_fallback(store: Store, session_id: str, trigger: str) -> None:
    """兜底观测：目标 <2%（方案 §4 fallback_events）。"""
    store.insert(
        FALLBACK_EVENTS,
        {"session_id": session_id, "trigger": trigger, "text": FALLBACK_TEXT, "ts": utc_now_iso()},
    )


class MessageRequest(BaseModel):
    """发消息请求：client_msg_id 为幂等键（小程序重发/双击防重）。"""

    text: str
    client_msg_id: str


@router.post("/messages", status_code=201)
def post_message(
    body: MessageRequest,
    user_id: str = Depends(get_user_id),
    settings: Settings = Depends(get_settings),
    store: Store = Depends(get_store),
    parlant: ParlantClient = Depends(get_parlant),
) -> dict[str, Any]:
    """发消息：幂等去重 → 查/建会话 → 转发 Parlant → 同步等 preamble ≤8s。"""
    # 幂等：unique(user_id, client_msg_id)，窗口内重发返回首次缓存结果
    dupes, _ = store.find(
        MESSAGES, {"user_id": user_id, "client_msg_id": body.client_msg_id}, limit=1
    )
    if dupes:
        cached = dupes[0]
        age = datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(cached["ts"]).timestamp()
        if age <= settings.idempotency_window_s:
            return {**cached["response"], "status": "duplicate"}

    sid = _get_or_create_session(store, parlant, user_id, settings.agent_id)
    baseline_events = parlant.get_events(sid, min_offset=0, wait_for_data=0) or []
    baseline = max((e.get("offset", -1) for e in baseline_events), default=-1) + 1
    parlant.post_event(sid, {"kind": "message", "source": "customer", "message": body.text})

    # 同步等 preamble：首条 ai_agent message 即返回；超时返回空 preamble（不阻塞）
    preamble = None
    deadline = time.monotonic() + settings.preamble_timeout_s
    while time.monotonic() < deadline:
        try:
            events = parlant.get_events(sid, min_offset=baseline, wait_for_data=1) or []
        except UpstreamError as exc:
            if "504" in exc.detail:
                continue
            raise
        for ev in events:
            if ev.get("kind") == "message" and ev.get("source") == "ai_agent":
                preamble = {
                    "text": (ev.get("data") or {}).get("message") or "",
                    "display_as": "placeholder",
                }
                break
        if preamble:
            break

    mapping = _open_session(store, user_id)
    store.update(SESSIONS, mapping["id"], {"last_active": utc_now_iso()})
    response = {
        "session_id": sid,
        "preamble": preamble,
        "ws_url": f"/channel/messages/stream?session_id={sid}",
    }
    store.insert(
        MESSAGES,
        {
            "user_id": user_id,
            "client_msg_id": body.client_msg_id,
            "response": response,
            "ts": utc_now_iso(),
        },
    )
    return {**response, "status": "ok"}


@router.websocket("/messages/stream")
async def message_stream(
    websocket: WebSocket,
    token: str = Query(...),
    session_id: str = Query(...),
) -> None:
    """WS 事件推送：tool_start / message_append / message_done / fallback。

    超时兜底：连接建立起 done_timeout_s 内无 ready/completed → 发 fallback 并记录。
    """
    store: Store = websocket.app.state.store
    parlant: ParlantClient = websocket.app.state.parlant
    settings: Settings = websocket.app.state.settings

    user_id = lookup_user(store, token)
    if user_id is None:
        await websocket.close(code=4401)
        return
    try:
        _owned_session(store, user_id, session_id)
    except HTTPException:
        await websocket.close(code=4403)
        return
    await websocket.accept()

    offset = 0
    deadline = time.monotonic() + settings.done_timeout_s
    completed = False
    fallback_sent = False
    try:
        while True:
            try:
                events = await asyncio.to_thread(
                    parlant.get_events, session_id, offset, 5
                )
            except UpstreamError as exc:
                if "504" in (exc.detail or ""):
                    events = []
                else:
                    if not fallback_sent:
                        _record_fallback(store, session_id, "upstream_down")
                        await websocket.send_json({"type": "fallback", "text": FALLBACK_TEXT})
                    return
            translated = translate_events(events or [])
            for msg in translated:
                offset = max(offset, (msg.get("offset") or offset - 1) + 1)
                if msg["type"] == "fallback":
                    _record_fallback(store, session_id, "engine_error")
                    fallback_sent = True
                if msg["type"] == "message_done":
                    completed = True
                msg.pop("offset", None)
                await websocket.send_json(msg)
            if completed:
                return  # 本轮答复完成，推送结束（前端可关或保持重连拉 history）
            if not fallback_sent and time.monotonic() > deadline:
                _record_fallback(store, session_id, "timeout")
                await websocket.send_json({"type": "fallback", "text": FALLBACK_TEXT})
                fallback_sent = True
            if not events:
                await asyncio.sleep(0.2)  # 空轮询间隔，防紧循环
    except WebSocketDisconnect:
        return


@router.get("/messages/poll")
def poll_messages(
    session_id: str = Query(...),
    after_offset: int = Query(default=0, ge=0),
    user_id: str = Depends(get_user_id),
    store: Store = Depends(get_store),
    parlant: ParlantClient = Depends(get_parlant),
) -> dict[str, Any]:
    """增量事件拉取（WS 降级）：同 WS 转译格式 + next_offset + done。"""
    _owned_session(store, user_id, session_id)
    events = parlant.get_events(session_id, min_offset=0, wait_for_data=0) or []
    translated = translate_events(events)
    fresh = [m for m in translated if (m.get("offset") or 0) >= after_offset]
    next_offset = max(((m.get("offset") or 0) + 1 for m in translated), default=after_offset)
    done = any(m["type"] == "message_done" for m in translated)
    for m in fresh:
        m.pop("offset", None)
    return {"events": fresh, "next_offset": next_offset, "done": done}


@router.get("/history")
def get_history(
    session_id: str = Query(...),
    limit: int = Query(default=50, ge=1, le=200),
    user_id: str = Depends(get_user_id),
    store: Store = Depends(get_store),
    parlant: ParlantClient = Depends(get_parlant),
) -> dict[str, Any]:
    """本会话历史（断线恢复/重进续聊）：用户友好形态，tool/status 不直接暴露。"""
    _owned_session(store, user_id, session_id)
    events = parlant.get_events(session_id, min_offset=0, wait_for_data=0) or []
    return {"items": to_history(events, limit)}
