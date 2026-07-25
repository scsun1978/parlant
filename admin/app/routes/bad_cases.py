"""质检工作台（v3.0 §4.1 一期主干）：bad case 状态机与闭环。

状态机：
    待复盘(pending_review) ──四选一归因──> 待修复(pending_fix)
    待修复 ──挂接修复物──> 待验证(pending_verify)
    待验证 ──沙盒重放 + 人工确认关闭──> 已关闭(closed)（用例自动入回归集）

发现入口：转人工会话复盘（intake，主）；已读不回为可靠性特例（no-reply-alerts，
不进归因流）；人工抽检/自动预警为二期。

所有状态迁移均校验前置状态（不符 409），全部写操作进 audit_logs。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from admin.app import audit
from admin.app.audit import utc_now_iso
from admin.app.auth import SessionUser, require_roles
from admin.app.deps import get_parlant, get_store
from admin.app.parlant_client import ParlantClient, UpstreamError
from admin.app.routes.sandbox import chat_once, start_snapshot, stop_agent
from admin.app.store import Store

router = APIRouter(prefix="/api/bad-cases", tags=["bad-cases"])

COLLECTION = "bad_cases"
REGRESSION_COLLECTION = "regression_items"

_READ_ROLES = ("operator", "reviewer", "auditor", "admin")
_WRITE_ROLES = ("operator", "reviewer", "admin")  # 归因/挂接/摄入
_REVIEW_ROLES = ("reviewer", "admin")  # verify/close

STATUSES = ("pending_review", "pending_fix", "pending_verify", "closed")
ATTRIBUTIONS = ("knowledge", "rule", "tool", "model")
REF_TYPES = ("knowledge_draft", "guideline_draft", "tool_ticket", "corpus_item")

TITLE_MAX = 60


# ---------------------------------------------------------------------------
# 事件流辅助
# ---------------------------------------------------------------------------


def _message_text(event: dict[str, Any]) -> str:
    msg = (event.get("data") or {}).get("message")
    return msg if isinstance(msg, str) else ""


def _first_customer_message(events: list[dict[str, Any]]) -> str:
    for e in events:
        if e.get("kind") == "message" and e.get("source") == "customer":
            return _message_text(e)
    return ""


def _last_ai_message(events: list[dict[str, Any]]) -> str:
    for e in reversed(events):
        if e.get("kind") == "message" and e.get("source") == "ai_agent":
            return _message_text(e)
    return ""


def _session_events(parlant: ParlantClient, session_id: str) -> list[dict[str, Any]]:
    try:
        return parlant.get_events(session_id, min_offset=0, wait_for_data=0) or []
    except UpstreamError:
        return []


def _get_case(store: Store, case_id: str) -> dict[str, Any]:
    doc = store.get(COLLECTION, case_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="bad case 不存在")
    return doc


def _require_status(case: dict[str, Any], expected: str, action: str) -> None:
    if case["status"] != expected:
        raise HTTPException(
            status_code=409,
            detail=f"当前状态 {case['status']} 不能执行{action}（需要 {expected}）",
        )


# ---------------------------------------------------------------------------
# 发现入口
# ---------------------------------------------------------------------------


@router.post("/intake", status_code=201)
def intake(
    user: SessionUser = Depends(require_roles(*_WRITE_ROLES)),
    store: Store = Depends(get_store),
    parlant: ParlantClient = Depends(get_parlant),
) -> dict[str, Any]:
    """摄入转人工复盘：扫描 mode=manual 的会话，按 session_id 去重建待复盘条目。"""
    created = []
    for s in parlant.list_sessions() or []:
        if s.get("mode") != "manual":
            continue
        sid = s["id"]
        _, dup = store.find(COLLECTION, {"session_id": sid}, limit=1)
        if dup:
            continue
        events = _session_events(parlant, sid)
        title = _first_customer_message(events)[:TITLE_MAX] or f"会话 {sid}"
        doc = store.insert(
            COLLECTION,
            {
                "source": "handoff_review",
                "session_id": sid,
                "title": title,
                "status": "pending_review",
                "attribution": None,
                "attribution_note": None,
                "prefill": {},
                "fix_ref": None,
                "verify": None,
                "created_by": user.username,
                "created_at": utc_now_iso(),
                "closed_at": None,
            },
        )
        created.append(doc)
    if created:
        audit.record(
            store,
            actor=user.username,
            role=user.role,
            action="badcase.intake",
            after={"created": len(created), "ids": [d["id"] for d in created]},
        )
    return {"created": len(created), "items": created}


@router.get("/no-reply-alerts")
def no_reply_alerts(
    user: SessionUser = Depends(require_roles(*_READ_ROLES)),
    parlant: ParlantClient = Depends(get_parlant),
) -> dict[str, Any]:
    """已读不回告警（可靠性特例）：末条 customer 消息无后续 AI 回复，或其后出现 error 状态。

    只出告警列表（source 标 alert_no_reply），不进归因流（v3.0 §4.1 入口 3）。
    """
    alerts = []
    for s in parlant.list_sessions() or []:
        events = _session_events(parlant, s["id"])
        messages = [e for e in events if e.get("kind") == "message"]
        if not messages:
            continue
        last_customer_idx = max(
            (i for i, e in enumerate(events) if e.get("kind") == "message" and e.get("source") == "customer"),
            default=None,
        )
        if last_customer_idx is None:
            continue
        after = events[last_customer_idx + 1 :]
        has_ai_reply = any(
            e.get("kind") == "message" and e.get("source") == "ai_agent" for e in after
        )
        has_error = any(
            e.get("kind") == "status" and (e.get("data") or {}).get("status") == "error"
            for e in after
        )
        if has_ai_reply and not has_error:
            continue
        alerts.append(
            {
                "source": "alert_no_reply",
                "session_id": s["id"],
                "title": _message_text(events[last_customer_idx])[:TITLE_MAX] or f"会话 {s['id']}",
                "last_activity_utc": events[-1].get("creation_utc"),
                "has_error_status": has_error,
            }
        )
    return {"items": alerts, "total": len(alerts)}


# ---------------------------------------------------------------------------
# 队列与详情
# ---------------------------------------------------------------------------


@router.get("")
def list_bad_cases(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: SessionUser = Depends(require_roles(*_READ_ROLES)),
    store: Store = Depends(get_store),
) -> dict[str, Any]:
    """队列：按状态过滤分页；响应含四态计数（counts）。"""
    filters = {"status": status} if status else None
    items, total = store.find(COLLECTION, filters, offset=offset, limit=limit, sort="-created_at")
    counts = {s: 0 for s in STATUSES}
    for s in STATUSES:
        _, n = store.find(COLLECTION, {"status": s}, limit=1)
        counts[s] = n
    return {"items": items, "total": total, "counts": counts, "offset": offset, "limit": limit}


@router.get("/{case_id}")
def get_bad_case(
    case_id: str,
    user: SessionUser = Depends(require_roles(*_READ_ROLES)),
    store: Store = Depends(get_store),
    parlant: ParlantClient = Depends(get_parlant),
) -> dict[str, Any]:
    """详情 + 该会话事件流（供前端回放）。"""
    case = _get_case(store, case_id)
    events = _session_events(parlant, case["session_id"])
    return {"case": case, "events": events}


# ---------------------------------------------------------------------------
# 归因 / 挂接 / 验证 / 关闭
# ---------------------------------------------------------------------------


class AttributeRequest(BaseModel):
    """四选一归因。"""

    type: Literal["knowledge", "rule", "tool", "model"]
    note: str = ""


@router.post("/{case_id}/attribute")
def attribute(
    case_id: str,
    body: AttributeRequest,
    user: SessionUser = Depends(require_roles(*_WRITE_ROLES)),
    store: Store = Depends(get_store),
    parlant: ParlantClient = Depends(get_parlant),
) -> dict[str, Any]:
    """归因并生成修复预填（prefill）：pending_review → pending_fix。"""
    case = _get_case(store, case_id)
    _require_status(case, "pending_review", "归因")

    events = _session_events(parlant, case["session_id"])
    question = _first_customer_message(events)
    wrong_answer = _last_ai_message(events)
    sid = case["session_id"]
    if body.type == "knowledge":
        prefill = {
            "question": question,
            "wrong_answer": wrong_answer,
            "session_id": sid,
            "suggested_answer": "",
        }
    elif body.type == "rule":
        prefill = {"condition": question, "action": "", "session_id": sid}
    elif body.type == "tool":
        prefill = {"session_id": sid, "note": body.note}
    else:  # model
        prefill = {"question": question, "session_id": sid}

    updated = store.update(
        COLLECTION,
        case_id,
        {
            "attribution": body.type,
            "attribution_note": body.note,
            "prefill": prefill,
            "status": "pending_fix",
        },
    )
    audit.record(
        store,
        actor=user.username,
        role=user.role,
        action="badcase.attribute",
        target=case_id,
        before={"status": "pending_review"},
        after={"status": "pending_fix", "attribution": body.type, "note": body.note},
    )
    return updated  # type: ignore[return-value]


class LinkFixRequest(BaseModel):
    """挂接修复物。"""

    ref_type: Literal["knowledge_draft", "guideline_draft", "tool_ticket", "corpus_item"]
    ref_id: str


@router.post("/{case_id}/link-fix")
def link_fix(
    case_id: str,
    body: LinkFixRequest,
    user: SessionUser = Depends(require_roles(*_WRITE_ROLES)),
    store: Store = Depends(get_store),
) -> dict[str, Any]:
    """挂接修复物：pending_fix → pending_verify。"""
    case = _get_case(store, case_id)
    _require_status(case, "pending_fix", "挂接修复物")
    if not body.ref_id.strip():
        raise HTTPException(status_code=422, detail="ref_id 不能为空")

    updated = store.update(
        COLLECTION,
        case_id,
        {
            "fix_ref": {"type": body.ref_type, "id": body.ref_id},
            "status": "pending_verify",
        },
    )
    audit.record(
        store,
        actor=user.username,
        role=user.role,
        action="badcase.link_fix",
        target=case_id,
        before={"status": "pending_fix"},
        after={"status": "pending_verify", "fix_ref": {"type": body.ref_type, "id": body.ref_id}},
    )
    return updated  # type: ignore[return-value]


@router.post("/{case_id}/verify")
def verify(
    case_id: str,
    user: SessionUser = Depends(require_roles(*_REVIEW_ROLES)),
    store: Store = Depends(get_store),
    parlant: ParlantClient = Depends(get_parlant),
) -> dict[str, Any]:
    """沙盒重放原会话（复用 sandbox 核心：快照 → chat → stop），结果存 verify。

    不自动判过：由 POST /{id}/close 人工确认关闭。
    """
    case = _get_case(store, case_id)
    _require_status(case, "pending_verify", "沙盒重放验证")

    question = (case.get("prefill") or {}).get("question") or (case.get("prefill") or {}).get("condition")
    if not question:
        events = _session_events(parlant, case["session_id"])
        question = _first_customer_message(events)
    if not question:
        raise HTTPException(status_code=422, detail="无法确定重放问题（prefill 与会话均无用户消息）")

    sandbox = start_snapshot(parlant, user.username)
    agent_id = sandbox["sandbox_agent_id"]
    try:
        result = chat_once(parlant, agent_id, question)
    finally:
        stop_agent(parlant, agent_id)

    verify_info = {
        "reply": result["reply"],
        "tools": result["tools"],
        "checked_by": user.username,
        "checked_at": utc_now_iso(),
    }
    updated = store.update(COLLECTION, case_id, {"verify": verify_info})
    audit.record(
        store,
        actor=user.username,
        role=user.role,
        action="badcase.verify",
        target=case_id,
        after={"reply": result["reply"][:200], "tools_count": len(result["tools"])},
    )
    return updated  # type: ignore[return-value]


@router.post("/{case_id}/reopen")
def reopen(
    case_id: str,
    user: SessionUser = Depends(require_roles(*_REVIEW_ROLES)),
    store: Store = Depends(get_store),
) -> dict[str, Any]:
    """重放不满意打回重修：pending_verify → pending_fix（verify 记录保留供对比）。

    配合工作台"还不行，重新修"按钮；修复物挂接（fix_ref）保留，可换挂或重修后
    重新 link-fix 进入验证。
    """
    case = _get_case(store, case_id)
    _require_status(case, "pending_verify", "打回重修")
    updated = store.update(COLLECTION, case_id, {"status": "pending_fix"})
    audit.record(
        store,
        actor=user.username,
        role=user.role,
        action="badcase.reopen",
        target=case_id,
        before={"status": "pending_verify"},
        after={"status": "pending_fix"},
    )
    return updated  # type: ignore[return-value]


@router.post("/{case_id}/close")
def close(
    case_id: str,
    user: SessionUser = Depends(require_roles(*_REVIEW_ROLES)),
    store: Store = Depends(get_store),
    parlant: ParlantClient = Depends(get_parlant),
) -> dict[str, Any]:
    """人工确认关闭：硬条件=修复物已挂接 + 沙盒重放已执行；关闭即用例入回归集。"""
    case = _get_case(store, case_id)
    _require_status(case, "pending_verify", "关闭")

    missing = []
    if not (case.get("fix_ref") or {}).get("id"):
        missing.append("fix_ref（修复物未挂接）")
    if not (case.get("verify") or {}).get("checked_by"):
        missing.append("verify（沙盒重放未执行）")
    if missing:
        raise HTTPException(status_code=409, detail="关闭条件不满足，缺：" + "、".join(missing))

    closed_at = utc_now_iso()
    updated = store.update(COLLECTION, case_id, {"status": "closed", "closed_at": closed_at})

    # 回归集沉淀：关闭的 bad case 自动入库（防修复引入新问题）
    question = (case.get("prefill") or {}).get("question") or (case.get("prefill") or {}).get("condition")
    if not question:
        events = _session_events(parlant, case["session_id"])
        question = _first_customer_message(events)
    store.insert(
        REGRESSION_COLLECTION,
        {"question": question, "source_case_id": case_id, "created_at": closed_at},
    )
    audit.record(
        store,
        actor=user.username,
        role=user.role,
        action="badcase.close",
        target=case_id,
        before={"status": "pending_verify"},
        after={"status": "closed", "regression_question": (question or "")[:120]},
    )
    return updated  # type: ignore[return-value]
