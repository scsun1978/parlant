"""审计中心：GET /api/audit-logs（auditor/admin）。

append-only 设计：本模块只有查询接口，不提供任何更新/删除路由。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from admin.app import audit
from admin.app.auth import SessionUser, require_roles
from admin.app.deps import get_store
from admin.app.store import Store

router = APIRouter(prefix="/api/audit-logs", tags=["audit"])


@router.get("")
def list_audit_logs(
    actor: str | None = Query(default=None),
    action: str | None = Query(default=None),
    ts_from: str | None = Query(default=None),
    ts_to: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    user: SessionUser = Depends(require_roles("auditor", "admin")),
    store: Store = Depends(get_store),
) -> dict[str, Any]:
    """按 actor/action/时间区间过滤的分页查询（按 ts 倒序）。"""
    items, total = audit.query(
        store,
        actor=actor,
        action=action,
        ts_from=ts_from,
        ts_to=ts_to,
        offset=offset,
        limit=limit,
    )
    return {"items": items, "total": total, "offset": offset, "limit": limit}
