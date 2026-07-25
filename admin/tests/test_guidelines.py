"""Guideline 审批流：起草 → 审批 → 落实体；驳回不触上游；双人复核约束。"""

from __future__ import annotations

import pytest

from admin.tests.conftest import Env

pytestmark = pytest.mark.asyncio

DRAFT_PAYLOAD = {
    "condition": "旅客询问延误赔偿金额",
    "action": "不得承诺具体赔偿金额，引导至柜台咨询",
    "criticality": "high",
    "composition_mode": "composited_canned",
    "priority": 10,
    "tags": ["红线"],
}


async def _draft(env: Env) -> dict:
    op = await env.login("op")
    resp = await env.client.post("/api/guidelines/draft", json=DRAFT_PAYLOAD, headers=op)
    assert resp.status_code == 201
    return resp.json()


async def test_draft_goes_to_pending_queue_not_upstream(env: Env) -> None:
    draft = await _draft(env)
    assert draft["status"] == "pending_review"
    assert draft["author"] == "op"
    assert env.parlant.created_guidelines == []  # 起草不直接写上游
    rev = await env.login("rev")
    resp = await env.client.get("/api/approvals", params={"status": "pending_review"}, headers=rev)
    assert resp.status_code == 200
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["id"] == draft["id"]


async def test_approve_calls_upstream_with_exact_payload(env: Env) -> None:
    draft = await _draft(env)
    rev = await env.login("rev")
    resp = await env.client.post(f"/api/approvals/{draft['id']}/approve", headers=rev)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["reviewed_by"] == "rev"
    assert body["upstream_id"]
    # 断言上游收到的 JSON 字段与起草载荷一致（按 Parlant 契约透传）
    assert env.parlant.created_guidelines == [DRAFT_PAYLOAD]


async def test_reject_does_not_touch_upstream(env: Env) -> None:
    draft = await _draft(env)
    rev = await env.login("rev")
    resp = await env.client.post(
        f"/api/approvals/{draft['id']}/reject",
        json={"reason": "措辞不合规"},
        headers=rev,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["reject_reason"] == "措辞不合规"
    assert env.parlant.created_guidelines == []  # 驳回不触上游


async def test_author_cannot_approve_own_draft(env: Env) -> None:
    # admin 有起草权也有审批权，但双人复核约束：不得审批自己起草的单
    adm = await env.login("adm")
    draft = (
        await env.client.post("/api/guidelines/draft", json=DRAFT_PAYLOAD, headers=adm)
    ).json()
    resp = await env.client.post(f"/api/approvals/{draft['id']}/approve", headers=adm)
    assert resp.status_code == 403
    # 换 reviewer 审批则放行
    rev = await env.login("rev")
    resp = await env.client.post(f"/api/approvals/{draft['id']}/approve", headers=rev)
    assert resp.status_code == 200


async def test_double_approve_conflict(env: Env) -> None:
    draft = await _draft(env)
    rev = await env.login("rev")
    resp = await env.client.post(f"/api/approvals/{draft['id']}/approve", headers=rev)
    assert resp.status_code == 200
    resp = await env.client.post(f"/api/approvals/{draft['id']}/approve", headers=rev)
    assert resp.status_code == 409


async def test_patch_guideline_goes_through_approval(env: Env) -> None:
    op = await env.login("op")
    patch = {"action": "改为引导至航司柜台", "enabled": True}
    resp = await env.client.patch("/api/guidelines/g1", json=patch, headers=op)
    assert resp.status_code == 201
    draft = resp.json()
    # diff 展示所需的原值/新值齐备
    assert draft["action"] == "update"
    assert draft["target_id"] == "g1"
    assert draft["before"]["id"] == "g1"
    assert draft["payload"] == patch
    assert env.parlant.patched_guidelines == []  # 未审批前不改上游

    rev = await env.login("rev")
    resp = await env.client.post(f"/api/approvals/{draft['id']}/approve", headers=rev)
    assert resp.status_code == 200
    assert env.parlant.patched_guidelines == [("g1", patch)]


async def test_get_approval_detail_contains_before_and_payload(env: Env) -> None:
    draft = await _draft(env)
    rev = await env.login("rev")
    resp = await env.client.get(f"/api/approvals/{draft['id']}", headers=rev)
    assert resp.status_code == 200
    body = resp.json()
    assert body["payload"] == DRAFT_PAYLOAD
    assert "before" in body
    assert body["created_at"]


async def test_approve_missing_approval_404(env: Env) -> None:
    rev = await env.login("rev")
    resp = await env.client.post("/api/approvals/nope/approve", headers=rev)
    assert resp.status_code == 404
