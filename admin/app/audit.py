"""审计日志：append-only，仅提供追加与查询，不提供任何更新/删除入口。

每条记录：actor/role/action/target/before/after/ts（UTC ISO8601）。
所有写操作（含 approve/reject/login 失败）都必须追加审计。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from admin.app.store import Store

AUDIT_COLLECTION = "audit_logs"


def utc_now_iso() -> str:
    """UTC 当前时刻 ISO8601 字符串（字典序可比较）。"""
    return datetime.now(timezone.utc).isoformat()


def record(
    store: Store,
    *,
    actor: str,
    role: str,
    action: str,
    target: str = "",
    before: Any = None,
    after: Any = None,
) -> dict[str, Any]:
    """追加一条审计日志。"""
    return store.insert(
        AUDIT_COLLECTION,
        {
            "actor": actor,
            "role": role,
            "action": action,
            "target": target,
            "before": before,
            "after": after,
            "ts": utc_now_iso(),
        },
    )


def query(
    store: Store,
    *,
    actor: str | None = None,
    action: str | None = None,
    ts_from: str | None = None,
    ts_to: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], int]:
    """按 actor/action/时间区间过滤分页查询（按 ts 倒序，新的在前）。"""
    filters: dict[str, Any] = {}
    if actor:
        filters["actor"] = actor
    if action:
        filters["action"] = action
    ts_range: dict[str, str] = {}
    if ts_from:
        ts_range["$gte"] = ts_from
    if ts_to:
        ts_range["$lte"] = ts_to
    if ts_range:
        filters["ts"] = ts_range
    return store.find(AUDIT_COLLECTION, filters, offset=offset, limit=limit, sort="-ts")
