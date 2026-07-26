"""会话中心（设计方案 §4.7）：在线监控 + 人工接管 + trace 回放 + SSE 实时通道。

Parlant 契约（已核实）：
- GET /sessions → 数组（id/agent_id/customer_id/creation_utc/title/mode/...）
- GET /sessions/{id}/events?min_offset&wait_for_data → 长轮询，504 需重试；
  事件 kind=message|tool|status，source=customer|ai_agent|human_agent；
  message 在 data.message；status 的 data={"status":..., "data":{"stage":...}}，
  ready 且 stage=completed 为本轮完成
- PATCH /sessions/{id} {"mode":"manual"|"auto"}（接管=manual，恢复=auto）
- POST /sessions/{id}/events {kind, source, message, participant:{display_name}}
  （human_agent 必须带 display_name）

实时通道：单 BFF 直连模式——BFF 对该会话做 min_offset 长轮询循环，新事件转 SSE
推给前端；**多副本时需经 Redis pub/sub 转发至正确前端连接（遗留登记，见 README）**。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from admin.app import audit
from admin.app.auth import SessionUser, require_roles
from admin.app.deps import get_parlant, get_store
from admin.app.parlant_client import ParlantClient, UpstreamError
from admin.app.store import Store

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

# 监控/回放读权限：班长主战场、审计只读全量、admin 全部
_READ_ROLES = ("supervisor", "auditor", "admin")
# 接管/代发写权限：仅班长与 admin
_WRITE_ROLES = ("supervisor", "admin")

STREAM_WAIT_SECONDS = 60  # 上游长轮询时长（504 空返回后重试）
STREAM_IDLE_SLEEP = 0.5  # 空轮询间隔，避免对上游紧循环


def _message_preview(event: dict[str, Any]) -> dict[str, Any]:
    """提取消息事件的摘要视图。"""
    data = event.get("data") or {}
    message = data.get("message")
    text = message if isinstance(message, str) else json.dumps(message, ensure_ascii=False)
    return {
        "source": event.get("source"),
        "preview": text[:80],
        "creation_utc": event.get("creation_utc"),
    }


def _summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    """由事件流推导工单视图字段：最近消息摘要、最近活动时间、疑似已读不回。"""
    messages = [e for e in events if e.get("kind") == "message"]
    last_message = _message_preview(messages[-1]) if messages else None
    last_activity = events[-1].get("creation_utc") if events else None
    # 已读不回：最后一条 message 来自 customer（其后无 ai_agent message）
    unanswered = bool(messages) and messages[-1].get("source") == "customer"
    return {
        "last_message": last_message,
        "last_activity_utc": last_activity,
        "unanswered": unanswered,
    }


@router.get("")
def list_sessions(
    page: int = 1,
    page_size: int = 50,
    user: SessionUser = Depends(require_roles(*_READ_ROLES)),
    parlant: ParlantClient = Depends(get_parlant),
) -> Any:
    """会话列表（分页，新会话在前）：仅当前页附加工单视图字段（最近消息/已读不回）。

    注：逐会话取事件为 N+1 调用，全量会话下必须分页（934+ 实测会挂起）。
    """
    sessions = parlant.list_sessions() or []
    sessions.sort(key=lambda s: s.get("creation_utc", ""), reverse=True)
    total = len(sessions)
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    window = sessions[(page - 1) * page_size : page * page_size]
    items = []
    for s in window:
        try:
            events = parlant.get_events(s["id"], min_offset=0, wait_for_data=0) or []
            summary = _summarize(events)
        except UpstreamError:
            summary = {"last_message": None, "last_activity_utc": None, "unanswered": False}
        items.append({**s, **summary})
    return {"total": total, "page": page, "page_size": page_size, "items": items}


@router.get("/{sid}/events")
def get_events(
    sid: str,
    min_offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    user: SessionUser = Depends(require_roles(*_READ_ROLES)),
    parlant: ParlantClient = Depends(get_parlant),
) -> dict[str, Any]:
    """分页透传会话事件（trace 回放数据源），返回 next_offset 供翻页。"""
    events = parlant.get_events(sid, min_offset=min_offset, wait_for_data=0) or []
    page = events[:limit]
    next_offset = min_offset
    for ev in page:
        ev_offset = ev.get("offset")
        next_offset = max(next_offset, (ev_offset + 1) if ev_offset is not None else next_offset + 1)
    return {"items": page, "min_offset": min_offset, "next_offset": next_offset}


class TakeoverRequest(BaseModel):
    """接管/恢复请求：manual=人工接管，auto=恢复 AI。"""

    mode: Literal["manual", "auto"]


@router.patch("/{sid}/takeover")
def takeover(
    sid: str,
    body: TakeoverRequest,
    user: SessionUser = Depends(require_roles(*_WRITE_ROLES)),
    store: Store = Depends(get_store),
    parlant: ParlantClient = Depends(get_parlant),
) -> Any:
    """人工接管（manual）/ 恢复 AI（auto）：调上游 PATCH mode，写审计。"""
    result = parlant.patch_session(sid, {"mode": body.mode})
    audit.record(
        store,
        actor=user.username,
        role=user.role,
        action="session.takeover",
        target=sid,
        after={"mode": body.mode},
    )
    return result


class HumanMessageRequest(BaseModel):
    """坐席代发消息请求。"""

    message: str


@router.post("/{sid}/messages", status_code=201)
def send_human_message(
    sid: str,
    body: HumanMessageRequest,
    user: SessionUser = Depends(require_roles(*_WRITE_ROLES)),
    store: Store = Depends(get_store),
    parlant: ParlantClient = Depends(get_parlant),
) -> Any:
    """以 source=human_agent 代发消息；display_name 取登录用户名（上游强制要求），写审计。"""
    payload = {
        "kind": "message",
        "source": "human_agent",
        "message": body.message,
        "participant": {"display_name": user.username},
    }
    result = parlant.post_event(sid, payload)
    audit.record(
        store,
        actor=user.username,
        role=user.role,
        action="session.human_message",
        target=sid,
        after={"message": body.message[:200]},
    )
    return result


async def _sse_stream(parlant: ParlantClient, sid: str, start_offset: int) -> AsyncIterator[str]:
    """长轮询上游事件并转 SSE；客户端断开时由 Starlette 取消生成器。

    注意：单次上游长轮询最长 STREAM_WAIT_SECONDS，断开检测最多滞后一个轮询周期
    （dev 级可接受；上游 504 空响应即重试）。
    """
    offset = start_offset
    while True:
        try:
            events = await asyncio.to_thread(
                parlant.get_events, sid, offset, STREAM_WAIT_SECONDS
            )
        except UpstreamError as exc:
            if "504" in exc.detail:
                continue  # 上游长轮询超时，属正常空转
            yield f"event: error\ndata: {json.dumps({'detail': str(exc)}, ensure_ascii=False)}\n\n"
            return
        got_new = False
        for ev in events or []:
            ev_offset = ev.get("offset")
            offset = max(offset, (ev_offset + 1) if ev_offset is not None else offset + 1)
            got_new = True
            yield f"event: parlant-event\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
        if not got_new:
            await asyncio.sleep(STREAM_IDLE_SLEEP)


@router.get("/{sid}/stream")
async def stream_events(
    sid: str,
    min_offset: int = Query(default=0, ge=0),
    user: SessionUser = Depends(require_roles(*_READ_ROLES, allow_query_token=True)),
    parlant: ParlantClient = Depends(get_parlant),
) -> StreamingResponse:
    """SSE 实时事件流：event: parlant-event, data: 事件 JSON。

    原生 EventSource 无法带请求头，本端点额外接受 ``?token=`` 查询参数。
    """
    return StreamingResponse(
        _sse_stream(parlant, sid, min_offset),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
