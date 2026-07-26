"""兜底文案提供者：治理库（Mongo channel_fallback）优先，缓存 60s，回退环境常量。

文案作为话术资产治理：channel_fallback 单文档 {text, version, updated_by, updated_at}；
启动时若空则以 CHANNEL_FALLBACK_TEXT 种子。审批流整合为后续项（README 登记）。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from admin.app.store import Store

COLLECTION = "channel_fallback"
CACHE_TTL_S = 60

_cache: dict[str, Any] = {"text": None, "fetched_at": 0.0}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_fallback_text(store: Store, env_default: str) -> str:
    """治理库生效版本 → 缓存 → 环境常量兜底。"""
    now = time.monotonic()
    if _cache["text"] is not None and now - _cache["fetched_at"] < CACHE_TTL_S:
        return _cache["text"]
    docs, _ = store.find(COLLECTION, {}, limit=1)
    text = docs[0]["text"] if docs else None
    if text:
        _cache.update(text=text, fetched_at=now)
        return text
    _cache.update(text=env_default, fetched_at=now)
    return env_default


def put_fallback_text(store: Store, text: str, env_default: str, updated_by: str) -> dict[str, Any]:
    """写入新文案（版本自增）；不存在则以环境常量为 v1 基础。"""
    docs, _ = store.find(COLLECTION, {}, limit=1)
    if docs:
        doc = docs[0]
        patch = {
            "text": text,
            "version": int(doc.get("version", 1)) + 1,
            "updated_by": updated_by,
            "updated_at": _utc_now_iso(),
        }
        result = store.update(COLLECTION, doc["id"], patch)
        _cache.update(text=text, fetched_at=time.monotonic())
        return result  # type: ignore[return-value]
    result = store.insert(
        COLLECTION,
        {"text": text, "version": 1, "updated_by": updated_by, "updated_at": _utc_now_iso()},
    )
    _cache.update(text=text, fetched_at=time.monotonic())
    return result
