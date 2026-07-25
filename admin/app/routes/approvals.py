"""审批流路由与核心逻辑：guideline / canned response 共用同一审批结构。

状态机：pending_review → approved（BFF 调用 Parlant 落实体）/ rejected（附理由，不触上游）。
硬规则（设计方案 §2）：双人复核——起草人不得审批自己的单。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from admin.app import audit
from admin.app.audit import utc_now_iso
from admin.app.auth import SessionUser, require_roles
from admin.app.deps import get_parlant, get_store
from admin.app.parlant_client import ParlantClient
from admin.app.store import Store

router = APIRouter(prefix="/api/approvals", tags=["approvals"])

APPROVALS_COLLECTION = "approvals"

STATUS_PENDING = "pending_review"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"


def create_draft(
    store: Store,
    *,
    kind: str,
    action: str,
    payload: dict[str, Any],
    author: SessionUser,
    target_id: str | None = None,
    before: Any = None,
) -> dict[str, Any]:
    """登记一条待审变更（起草不进上游，审批通过才写入 Parlant）。"""
    return store.insert(
        APPROVALS_COLLECTION,
        {
            "kind": kind,
            "action": action,
            "target_id": target_id,
            "payload": payload,
            "before": before,
            "status": STATUS_PENDING,
            "author": author.username,
            "author_role": author.role,
            "created_at": utc_now_iso(),
            "reviewed_by": None,
            "reviewed_at": None,
            "reject_reason": None,
            "upstream_id": None,
        },
    )


def _apply_to_upstream(parlant: ParlantClient, approval: dict[str, Any]) -> Any:
    """审批通过后将变更写入 Parlant，返回上游实体（含 id）。"""
    kind = approval["kind"]
    action = approval["action"]
    payload = approval["payload"]
    if kind == "guideline":
        if action == "create":
            return parlant.create_guideline(payload)
        return parlant.patch_guideline(approval["target_id"], payload)
    if kind == "canned_response":
        return parlant.create_canned_response(payload)
    raise UpstreamKindError(kind)


class UpstreamKindError(Exception):
    """未知的审批单类型。"""


class RejectRequest(BaseModel):
    """驳回请求：必须附理由。"""

    reason: str


@router.get("")
def list_approvals(
    status: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    user: SessionUser = Depends(require_roles("reviewer", "auditor", "admin")),
    store: Store = Depends(get_store),
) -> dict[str, Any]:
    """审批队列（审核员主视图），可按 status 过滤（如 pending_review）。"""
    filters = {"status": status} if status else None
    items, total = store.find(
        APPROVALS_COLLECTION, filters, offset=offset, limit=limit, sort="-created_at"
    )
    return {"items": items, "total": total, "offset": offset, "limit": limit}


@router.get("/{approval_id}")
def get_approval(
    approval_id: str,
    user: SessionUser = Depends(require_roles("reviewer", "auditor", "admin")),
    store: Store = Depends(get_store),
) -> dict[str, Any]:
    """审批单详情：含 diff 展示所需的 before（原值）与 payload（新值）。"""
    doc = store.get(APPROVALS_COLLECTION, approval_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="审批单不存在")
    return doc


@router.post("/{approval_id}/approve")
def approve(
    approval_id: str,
    user: SessionUser = Depends(require_roles("reviewer", "admin")),
    store: Store = Depends(get_store),
    parlant: ParlantClient = Depends(get_parlant),
) -> dict[str, Any]:
    """审批通过：BFF 调用 Parlant 创建/更新实体，记录上游 id。"""
    doc = store.get(APPROVALS_COLLECTION, approval_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="审批单不存在")
    if doc["status"] != STATUS_PENDING:
        raise HTTPException(status_code=409, detail=f"审批单已处于 {doc['status']} 状态")
    if doc["author"] == user.username:
        raise HTTPException(status_code=403, detail="双人复核约束：不得审批自己起草的单")

    result = _apply_to_upstream(parlant, doc)
    upstream_id = result.get("id") if isinstance(result, dict) else None
    updated = store.update(
        APPROVALS_COLLECTION,
        approval_id,
        {
            "status": STATUS_APPROVED,
            "upstream_id": upstream_id,
            "reviewed_by": user.username,
            "reviewed_at": utc_now_iso(),
        },
    )
    audit.record(
        store,
        actor=user.username,
        role=user.role,
        action="approval.approve",
        target=approval_id,
        before={"status": STATUS_PENDING},
        after={"status": STATUS_APPROVED, "upstream_id": upstream_id},
    )
    return updated  # type: ignore[return-value]


@router.post("/{approval_id}/reject")
def reject(
    approval_id: str,
    body: RejectRequest,
    user: SessionUser = Depends(require_roles("reviewer", "admin")),
    store: Store = Depends(get_store),
) -> dict[str, Any]:
    """审批驳回：附理由，不触上游。"""
    doc = store.get(APPROVALS_COLLECTION, approval_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="审批单不存在")
    if doc["status"] != STATUS_PENDING:
        raise HTTPException(status_code=409, detail=f"审批单已处于 {doc['status']} 状态")
    if doc["author"] == user.username:
        raise HTTPException(status_code=403, detail="双人复核约束：不得审批自己起草的单")

    updated = store.update(
        APPROVALS_COLLECTION,
        approval_id,
        {
            "status": STATUS_REJECTED,
            "reject_reason": body.reason,
            "reviewed_by": user.username,
            "reviewed_at": utc_now_iso(),
        },
    )
    audit.record(
        store,
        actor=user.username,
        role=user.role,
        action="approval.reject",
        target=approval_id,
        before={"status": STATUS_PENDING},
        after={"status": STATUS_REJECTED, "reason": body.reason},
    )
    return updated  # type: ignore[return-value]
