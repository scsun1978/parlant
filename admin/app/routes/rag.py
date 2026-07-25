"""知识库召回测试台（RAG）：/stages 透传、/reload 热加载（admin）、/health 透传。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from admin.app import audit
from admin.app.auth import SessionUser, get_current_user, require_roles
from admin.app.deps import get_rag, get_store
from admin.app.parlant_client import RagClient
from admin.app.store import Store

router = APIRouter(prefix="/api/rag", tags=["rag"])


class RagStagesRequest(BaseModel):
    """分阶段召回测试请求。"""

    query: str
    count: int = 5


@router.post("/stages")
def rag_stages(
    body: RagStagesRequest,
    user: SessionUser = Depends(get_current_user),
    rag: RagClient = Depends(get_rag),
) -> Any:
    """透传 RAG /stages：展示 BM25/向量/RRF 各阶段 top N 与得分（召回测试台）。"""
    return rag.stages(body.query, body.count)


@router.post("/reload")
def rag_reload(
    user: SessionUser = Depends(require_roles("admin")),
    store: Store = Depends(get_store),
    rag: RagClient = Depends(get_rag),
) -> Any:
    """透传 RAG /reload 热加载：索引重建后一键生效（仅 admin），写操作追加审计。"""
    result = rag.reload()
    audit.record(
        store,
        actor=user.username,
        role=user.role,
        action="rag.reload",
        after={"result": "triggered"},
    )
    return result


@router.get("/health")
def rag_health(
    user: SessionUser = Depends(get_current_user),
    rag: RagClient = Depends(get_rag),
) -> dict[str, Any]:
    """透传 RAG 健康状态。"""
    return {"status": "up" if rag.healthy() else "down"}
