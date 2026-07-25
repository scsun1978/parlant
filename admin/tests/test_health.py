"""健康检查：Parlant/RAG/Mongo 三向探测与上游挂掉时的降级显示。"""

from __future__ import annotations

import pytest

from admin.tests.conftest import Env

pytestmark = pytest.mark.asyncio


async def test_health_all_up(env: Env) -> None:
    resp = await env.client.get("/health")  # 无需鉴权
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "ok", "parlant": "up", "rag": "up", "mongo": "memory"}


async def test_health_degraded_when_parlant_down(env: Env) -> None:
    env.parlant.healthy_flag = False
    body = (await env.client.get("/health")).json()
    assert body["parlant"] == "down"
    assert body["rag"] == "up"
    assert body["status"] == "degraded"


async def test_health_degraded_when_rag_down(env: Env) -> None:
    env.rag.healthy_flag = False
    body = (await env.client.get("/health")).json()
    assert body["rag"] == "down"
    assert body["status"] == "degraded"
