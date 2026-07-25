"""知识库管理（v3.0 §5 知识编辑器 + §4.1 知识缺失闭环）：文档浏览代理 + 补录草稿流。

闭环：bad case"知识缺失"归因 → POST /drafts 补录草稿 → reviewer approve
（生成 bundle 文件 → RAG /reindex 重建索引热加载）→ published → bad case link-fix 挂接 → close。

- bundle 写入 BUNDLES_DIR（默认 ../knowledge/bundles/v2-miniprogram-20260719b，容器内 /data/...）
- approve 双人复核约束与 approvals 一致：起草人不得审批自己的草稿
- reindex 失败不丢 bundle 文件：status 保持 approved，reindex_error 记录错误尾，可重试 approve？——
  不，approved 状态不允许重复 approve（409）；如需重试 reindex 由运维直接调 RAG /reindex（README 登记）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from admin.app import audit
from admin.app.audit import utc_now_iso
from admin.app.auth import SessionUser, require_roles
from admin.app.bundles import new_asset_id, render_bundle
from admin.app.config import Settings
from admin.app.deps import get_rag, get_settings, get_store
from admin.app.parlant_client import RagClient, UpstreamError
from admin.app.store import Store

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

COLLECTION = "knowledge_drafts"

_READ_ROLES = ("operator", "reviewer", "auditor", "admin")
_DRAFT_ROLES = ("operator", "admin")
_REVIEW_ROLES = ("reviewer", "admin")

REINDEX_TIMEOUT_S = 300.0

# ---------------------------------------------------------------------------
# 只读代理：文档浏览与缺口看板（RAG /docs /gaps）
# ---------------------------------------------------------------------------


@router.get("/docs")
def list_docs(
    q: str | None = Query(default=None),
    knowledge_type: str | None = Query(default=None),
    valid_state: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: SessionUser = Depends(require_roles(*_READ_ROLES)),
    rag: RagClient = Depends(get_rag),
) -> Any:
    """透传 RAG /docs（全部过滤参数）。"""
    params: dict[str, Any] = {"page": page, "page_size": page_size}
    if q is not None:
        params["q"] = q
    if knowledge_type is not None:
        params["knowledge_type"] = knowledge_type
    if valid_state is not None:
        params["valid_state"] = valid_state
    return rag.list_docs(params)


@router.get("/docs/{doc_id}")
def get_doc(
    doc_id: str,
    user: SessionUser = Depends(require_roles(*_READ_ROLES)),
    rag: RagClient = Depends(get_rag),
) -> Any:
    """透传 RAG /docs/{doc_id}（text 全文）。"""
    return rag.get_doc(doc_id)


@router.get("/gaps")
def gaps(
    days: int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=20, ge=1, le=200),
    user: SessionUser = Depends(require_roles(*_READ_ROLES)),
    rag: RagClient = Depends(get_rag),
) -> Any:
    """透传 RAG /gaps（知识缺口看板）。"""
    return rag.gaps(days, limit)


@router.get("/gaps/stats")
def gaps_stats(
    days: int = Query(default=7, ge=1, le=90),
    user: SessionUser = Depends(require_roles(*_READ_ROLES)),
    rag: RagClient = Depends(get_rag),
) -> Any:
    """透传 RAG /gaps/stats。"""
    return rag.gaps_stats(days)


# ---------------------------------------------------------------------------
# 补录草稿流
# ---------------------------------------------------------------------------


class KnowledgeDraftCreate(BaseModel):
    """补录草稿创建（可从 bad case prefill 带值）。"""

    question: str
    suggested_answer: str
    wrong_answer: str | None = None
    session_id: str | None = None
    bad_case_id: str | None = None  # 来源 bad case（bundle 的 source_id/source_uri）


@router.post("/drafts", status_code=201)
def create_draft(
    body: KnowledgeDraftCreate,
    user: SessionUser = Depends(require_roles(*_DRAFT_ROLES)),
    store: Store = Depends(get_store),
) -> dict[str, Any]:
    """operator 起草知识补录（pending_review）。"""
    doc = store.insert(
        COLLECTION,
        {
            "question": body.question,
            "wrong_answer": body.wrong_answer,
            "suggested_answer": body.suggested_answer,
            "session_id": body.session_id,
            "bad_case_id": body.bad_case_id,
            "status": "pending_review",
            "bundle_asset_id": None,
            "published_docs": None,
            "reindex_error": None,
            "reject_reason": None,
            "created_by": user.username,
            "reviewer": None,
            "created_at": utc_now_iso(),
            "reviewed_at": None,
        },
    )
    audit.record(
        store,
        actor=user.username,
        role=user.role,
        action="knowledge.draft",
        target=doc["id"],
        after={"question": body.question[:120]},
    )
    return doc


@router.get("/drafts")
def list_drafts(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: SessionUser = Depends(require_roles(*_READ_ROLES)),
    store: Store = Depends(get_store),
) -> dict[str, Any]:
    """草稿队列（可按 status 过滤）。"""
    filters = {"status": status} if status else None
    items, total = store.find(COLLECTION, filters, offset=offset, limit=limit, sort="-created_at")
    return {"items": items, "total": total, "offset": offset, "limit": limit}


def _get_draft(store: Store, draft_id: str) -> dict[str, Any]:
    doc = store.get(COLLECTION, draft_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="知识草稿不存在")
    return doc


@router.post("/drafts/{draft_id}/approve")
def approve_draft(
    draft_id: str,
    user: SessionUser = Depends(require_roles(*_REVIEW_ROLES)),
    store: Store = Depends(get_store),
    settings: Settings = Depends(get_settings),
    rag: RagClient = Depends(get_rag),
) -> dict[str, Any]:
    """审批通过：生成 bundle 文件 → 调 RAG /reindex → published（失败保持 approved 记错误尾）。"""
    doc = _get_draft(store, draft_id)
    if doc["status"] != "pending_review":
        raise HTTPException(status_code=409, detail=f"草稿已处于 {doc['status']} 状态")
    if doc["created_by"] == user.username:
        raise HTTPException(status_code=403, detail="双人复核约束：不得审批自己起草的草稿")

    # 1) 生成 bundle 文件（asset_id=pvg.v2.kb<时间戳 hex>，文件名 <stem>.md）
    asset_id, stem = new_asset_id()
    source_id = doc.get("bad_case_id") or draft_id
    content = render_bundle(
        asset_id=asset_id,
        question=doc["question"],
        suggested_answer=doc["suggested_answer"],
        reviewer=user.username,
        source_id=source_id,
    )
    bundles_dir = Path(settings.bundles_dir)
    bundles_dir.mkdir(parents=True, exist_ok=True)
    (bundles_dir / f"{stem}.md").write_text(content, encoding="utf-8")

    # 2) 调 RAG /reindex 重建索引并热加载；失败不丢 bundle 文件
    patch: dict[str, Any] = {
        "bundle_asset_id": asset_id,
        "reviewer": user.username,
        "reviewed_at": utc_now_iso(),
    }
    try:
        result = rag.reindex(timeout_s=REINDEX_TIMEOUT_S)
        patch.update(
            {
                "status": "published",
                "published_docs": result.get("docs") if isinstance(result, dict) else None,
                "reindex_error": None,
            }
        )
    except UpstreamError as exc:
        patch.update({"status": "approved", "reindex_error": str(exc)[:500]})

    updated = store.update(COLLECTION, draft_id, patch)
    audit.record(
        store,
        actor=user.username,
        role=user.role,
        action="knowledge.approve",
        target=draft_id,
        before={"status": "pending_review"},
        after={
            "status": patch["status"],
            "bundle_asset_id": asset_id,
            "published_docs": patch.get("published_docs"),
            "reindex_error": patch.get("reindex_error"),
        },
    )
    return updated  # type: ignore[return-value]


class RejectRequest(BaseModel):
    """驳回请求：必须附理由。"""

    reason: str


@router.post("/drafts/{draft_id}/reject")
def reject_draft(
    draft_id: str,
    body: RejectRequest,
    user: SessionUser = Depends(require_roles(*_REVIEW_ROLES)),
    store: Store = Depends(get_store),
) -> dict[str, Any]:
    """审批驳回：附理由，不写 bundle 文件。"""
    doc = _get_draft(store, draft_id)
    if doc["status"] != "pending_review":
        raise HTTPException(status_code=409, detail=f"草稿已处于 {doc['status']} 状态")
    if doc["created_by"] == user.username:
        raise HTTPException(status_code=403, detail="双人复核约束：不得审批自己起草的草稿")

    updated = store.update(
        COLLECTION,
        draft_id,
        {
            "status": "rejected",
            "reject_reason": body.reason,
            "reviewer": user.username,
            "reviewed_at": utc_now_iso(),
        },
    )
    audit.record(
        store,
        actor=user.username,
        role=user.role,
        action="knowledge.reject",
        target=draft_id,
        after={"status": "rejected", "reason": body.reason},
    )
    return updated  # type: ignore[return-value]
