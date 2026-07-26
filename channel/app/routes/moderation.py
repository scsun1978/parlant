"""审核规则治理与审计查询 API（staff 侧，CHANNEL_ADMIN_TOKEN 保护）。"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from admin.app.store import Store
from channel.app.deps import get_store
from channel.app.moderation import EVENTS, RULES

router = APIRouter(prefix="/channel/moderation", tags=["moderation"])


def _require_staff(x_admin_token: str | None = Header(default=None)) -> str:
    """staff 令牌校验（CHANNEL_ADMIN_TOKEN 环境变量；未配置则全部 403）。"""
    expected = os.environ.get("CHANNEL_ADMIN_TOKEN")
    if not expected or x_admin_token != expected:
        raise HTTPException(status_code=403, detail="staff token 缺失或错误（CHANNEL_ADMIN_TOKEN）")
    return "staff"


class RuleCreate(BaseModel):
    category: str
    pattern: str
    note: str = ""


class RulePatch(BaseModel):
    enabled: bool | None = None


@router.get("/rules")
def list_rules(store: Store = Depends(get_store), _: str = Depends(_require_staff)) -> Any:
    items, total = store.find(RULES, {}, limit=500)
    return {"items": items, "total": total}


@router.post("/rules", status_code=201)
def create_rule(body: RuleCreate, store: Store = Depends(get_store), _: str = Depends(_require_staff)) -> Any:
    if body.category not in ("injection", "content"):
        raise HTTPException(status_code=422, detail="category 须为 injection|content")
    return store.insert(
        RULES,
        {
            "category": body.category,
            "pattern": body.pattern,
            "action": "block",
            "enabled": True,
            "note": body.note,
            "hit_count": 0,
            "created_by": "staff",
        },
    )


@router.patch("/rules/{rule_id}")
def patch_rule(rule_id: str, body: RulePatch, store: Store = Depends(get_store), _: str = Depends(_require_staff)) -> Any:
    docs, _ = store.find(RULES, {"id": rule_id}, limit=1)
    if not docs:
        raise HTTPException(status_code=404, detail="规则不存在")
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if not patch:
        raise HTTPException(status_code=422, detail="无可更新字段")
    return store.update(RULES, rule_id, patch)


@router.get("/events")
def list_events(limit: int = 50, store: Store = Depends(get_store), _: str = Depends(_require_staff)) -> Any:
    items, total = store.find(EVENTS, {}, limit=min(limit, 500))
    return {"items": items, "total": total}


# --- 兜底文案治理（channel_fallback 单文档生效版本） ---


class FallbackTextUpdate(BaseModel):
    text: str


@router.get("/fallback-text")
def get_fallback(store: Store = Depends(get_store), _: str = Depends(_require_staff)) -> Any:
    from channel.app import fallback_text as fbtext

    docs, _ = store.find(fbtext.COLLECTION, {}, limit=1)
    if docs:
        return docs[0]
    return {"text": None, "version": 0, "note": "未设置，走环境常量兜底"}


@router.put("/fallback-text")
def put_fallback(
    body: FallbackTextUpdate,
    store: Store = Depends(get_store),
    staff: str = Depends(_require_staff),
) -> Any:
    from channel.app import fallback_text as fbtext

    if not body.text.strip():
        raise HTTPException(status_code=422, detail="文案不能为空")
    return fbtext.put_fallback_text(store, body.text.strip(), "", staff)
