"""试聊沙盒（设计方案 §4.2，v2.1 Q5 定稿）：生产快照 + 草稿叠加，打开现取、用完清理。

流程：
1. start：创建 test agent → 把生产 agent（PROD_AGENT_ID，默认 xyVHBNLLPg）的全部
   guideline 复制进去（逐条 POST 并带 tags:[agent:<newid>]）；有 guideline_draft 时
   叠加——同 condition 的规则被草稿覆盖，否则追加草稿；
2. chat：建/复用 session 发消息，轮询事件直到 ready 且 stage=completed 判定本轮完成，
   返回拼接回复 + 工具调用（preamble 标注）；
3. stop：删除 test agent；上游无删除接口时降级为停用其全部规则（README 已登记）。

沙盒全部写入独立 test agent，不影响线上生产 agent。
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from admin.app import audit
from admin.app.auth import SessionUser, require_roles
from admin.app.config import Settings
from admin.app.deps import get_parlant, get_settings, get_store
from admin.app.parlant_client import ParlantClient, UpstreamError
from admin.app.store import Store

router = APIRouter(prefix="/api/sandbox", tags=["sandbox"])

_ROLES = ("operator", "reviewer", "admin")

# 复制生产规则时保留的字段（剔除 id/tags/创建时间等实例属性）
_GUIDELINE_COPY_FIELDS = (
    "condition",
    "action",
    "description",
    "title",
    "criticality",
    "metadata",
    "enabled",
    "composition_mode",
    "priority",
)

CHAT_TIMEOUT_SECONDS = int(os.environ.get("SANDBOX_CHAT_TIMEOUT", "180"))  # 单轮回复等待上限（引擎完整答复可达 90s）
CHAT_POLL_SECONDS = 2  # 单次长轮询时长

# 沙盒 agent → session 复用表（进程内，dev 级；替代浏览器 cookie 方案）
_sessions: dict[str, str] = {}
_sessions_lock = threading.Lock()


class SandboxStartRequest(BaseModel):
    """开始沙盒：可携带一条草稿规则（当/则）叠加到生产快照上。"""

    guideline_draft: dict[str, Any] | None = None


class SandboxChatRequest(BaseModel):
    """沙盒对话请求。"""

    sandbox_agent_id: str
    message: str


class SandboxStopRequest(BaseModel):
    """结束沙盒请求。"""

    sandbox_agent_id: str


def _copy_payload(guideline: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """从生产规则提取可复制字段，并打上目标 agent 归属标签。"""
    payload = {k: v for k, v in guideline.items() if k in _GUIDELINE_COPY_FIELDS and v is not None}
    payload["tags"] = [f"agent:{agent_id}"]
    return payload


def _is_preamble(event: dict[str, Any]) -> bool:
    """preamble 启发式判定（与会话中心前端口径一致）。"""
    data = event.get("data") or {}
    return data.get("preamble") is True or bool((data.get("metadata") or {}).get("preamble"))


def start_snapshot(
    parlant: ParlantClient, username: str, draft: dict[str, Any] | None = None
) -> dict[str, Any]:
    """核心逻辑（路由与 bad case 重放复用）：生产快照复制 + 草稿叠加。

    返回 {sandbox_agent_id, copied_guidelines, draft_overlaid, draft_appended}。
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    agent = parlant.create_agent(
        {
            "name": f"sandbox-{ts}-{username}",
            "description": f"试聊沙盒（{username} 创建，生产快照+草稿叠加，用完清理）",
        }
    )
    agent_id = agent["id"] if isinstance(agent, dict) else None
    if not agent_id:
        raise UpstreamError("parlant", "创建 test agent 未返回 id")

    copied = 0
    overlaid = False
    for g in parlant.list_guidelines() or []:
        payload = _copy_payload(g, agent_id)
        if draft and g.get("condition") == draft.get("condition"):
            # 同 condition 覆盖：草稿字段盖在快照规则上（v2.1 Q5 的"叠加"语义）
            payload.update({k: v for k, v in draft.items() if v is not None and k != "tags"})
            overlaid = True
        parlant.create_guideline(payload)
        copied += 1
    draft_appended = False
    if draft and not overlaid:
        payload = {k: v for k, v in draft.items() if v is not None}
        payload["tags"] = [f"agent:{agent_id}"]
        parlant.create_guideline(payload)
        draft_appended = True
    return {
        "sandbox_agent_id": agent_id,
        "copied_guidelines": copied,
        "draft_overlaid": overlaid,
        "draft_appended": draft_appended,
    }


@router.post("/start", status_code=201)
def start_sandbox(
    body: SandboxStartRequest,
    user: SessionUser = Depends(require_roles(*_ROLES)),
    store: Store = Depends(get_store),
    settings: Settings = Depends(get_settings),
    parlant: ParlantClient = Depends(get_parlant),
) -> dict[str, Any]:
    """开始沙盒：生产快照（复制全部规则）+ 草稿叠加（同 condition 覆盖）。"""
    result = start_snapshot(parlant, user.username, body.guideline_draft)
    audit.record(
        store,
        actor=user.username,
        role=user.role,
        action="sandbox.start",
        target=result["sandbox_agent_id"],
        after={
            "prod_agent_id": settings.prod_agent_id,
            "copied_guidelines": result["copied_guidelines"],
            "draft_overlaid": result["draft_overlaid"],
            "draft_appended": result["draft_appended"],
        },
    )
    return result


def _get_or_create_session(parlant: ParlantClient, agent_id: str) -> str:
    with _sessions_lock:
        sid = _sessions.get(agent_id)
    if sid:
        return sid
    session = parlant.create_session({"agent_id": agent_id, "title": f"sandbox:{agent_id}"})
    sid = session["id"]
    with _sessions_lock:
        _sessions[agent_id] = sid
    return sid


def chat_once(parlant: ParlantClient, agent_id: str, message: str) -> dict[str, Any]:
    """核心逻辑（路由与 bad case 重放复用）：单轮对话，ready 且 stage=completed 判定完成。"""
    sid = _get_or_create_session(parlant, agent_id)

    baseline_events = parlant.get_events(sid, min_offset=0, wait_for_data=0) or []
    baseline = max((e.get("offset", -1) for e in baseline_events), default=-1) + 1

    parlant.post_event(sid, {"kind": "message", "source": "customer", "message": message})

    deadline = time.monotonic() + CHAT_TIMEOUT_SECONDS
    messages: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    completed = False
    while time.monotonic() < deadline:
        try:
            events = parlant.get_events(sid, min_offset=baseline, wait_for_data=CHAT_POLL_SECONDS) or []
        except UpstreamError as exc:
            if "504" in exc.detail:
                continue  # 长轮询空超时，继续等
            raise
        for ev in events:
            if ev.get("kind") == "message" and ev.get("source") == "ai_agent":
                text = (ev.get("data") or {}).get("message") or ""
                messages.append({"text": text, "preamble": _is_preamble(ev)})
            elif ev.get("kind") == "tool":
                tools.append(ev.get("data") or {})
            elif ev.get("kind") == "status":
                data = ev.get("data") or {}
                inner = data.get("data") or {}
                if data.get("status") == "ready" and inner.get("stage") == "completed":
                    completed = True
        if completed:
            break

    if not completed:
        raise HTTPException(status_code=504, detail="沙盒回复超时（未收到 ready/completed）")
    reply = "\n".join(m["text"] for m in messages if m["text"])
    return {
        "session_id": sid,
        "reply": reply,
        "messages": messages,
        "tools": tools,
    }


@router.post("/chat")
def chat(
    body: SandboxChatRequest,
    user: SessionUser = Depends(require_roles(*_ROLES)),
    parlant: ParlantClient = Depends(get_parlant),
) -> dict[str, Any]:
    """沙盒单轮对话：发消息后轮询事件，ready 且 stage=completed 判定本轮完成。"""
    return chat_once(parlant, body.sandbox_agent_id, body.message)


def stop_agent(parlant: ParlantClient, agent_id: str) -> dict[str, Any]:
    """核心逻辑（路由与 bad case 重放复用）：删除 test agent，无删除接口时降级停用其规则。"""
    with _sessions_lock:
        _sessions.pop(agent_id, None)

    deleted = False
    disabled = 0
    try:
        parlant.delete_agent(agent_id)
        deleted = True
    except UpstreamError:
        # 降级方案：停用该沙盒 agent 的全部规则（tags 含 agent:<id>）
        for g in parlant.list_guidelines() or []:
            if f"agent:{agent_id}" in (g.get("tags") or []):
                parlant.patch_guideline(g["id"], {"enabled": False})
                disabled += 1
    return {"sandbox_agent_id": agent_id, "deleted": deleted, "disabled_guidelines": disabled}


@router.post("/stop")
def stop_sandbox(
    body: SandboxStopRequest,
    user: SessionUser = Depends(require_roles(*_ROLES)),
    store: Store = Depends(get_store),
    parlant: ParlantClient = Depends(get_parlant),
) -> dict[str, Any]:
    """结束沙盒：删除 test agent；上游无删除接口时降级为停用其全部规则。"""
    result = stop_agent(parlant, body.sandbox_agent_id)
    audit.record(
        store,
        actor=user.username,
        role=user.role,
        action="sandbox.stop",
        target=body.sandbox_agent_id,
        after={"deleted": result["deleted"], "disabled_guidelines": result["disabled_guidelines"]},
    )
    return result
