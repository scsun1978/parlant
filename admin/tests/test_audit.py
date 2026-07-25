"""审计日志：写操作留痕、过滤分页、append-only（无更新/删除接口）。"""

from __future__ import annotations

import pytest

from admin.tests.conftest import Env

pytestmark = pytest.mark.asyncio


async def _make_some_writes(env: Env) -> None:
    await env.client.post("/api/auth/login", json={"username": "op", "password": "bad"})
    op = await env.login("op")
    draft = (
        await env.client.post("/api/guidelines/draft", json={"condition": "x"}, headers=op)
    ).json()
    rev = await env.login("rev")
    await env.client.post(f"/api/approvals/{draft['id']}/approve", headers=rev)


async def test_write_ops_are_audited_with_full_fields(env: Env) -> None:
    await _make_some_writes(env)
    aud = await env.login("aud")
    resp = await env.client.get("/api/audit-logs", headers=aud)
    assert resp.status_code == 200
    body = resp.json()
    actions = [i["action"] for i in body["items"]]
    assert "auth.login" in actions
    assert "guideline.draft" in actions
    assert "approval.approve" in actions
    for item in body["items"]:
        for field in ("actor", "role", "action", "target", "before", "after", "ts"):
            assert field in item


async def test_audit_filter_by_actor_and_action(env: Env) -> None:
    await _make_some_writes(env)
    aud = await env.login("aud")
    resp = await env.client.get(
        "/api/audit-logs", params={"actor": "rev", "action": "approval.approve"}, headers=aud
    )
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["actor"] == "rev"
    assert body["items"][0]["after"]["status"] == "approved"


async def test_audit_pagination(env: Env) -> None:
    await _make_some_writes(env)
    aud = await env.login("aud")
    page1 = (
        await env.client.get("/api/audit-logs", params={"limit": 2, "offset": 0}, headers=aud)
    ).json()
    page2 = (
        await env.client.get("/api/audit-logs", params={"limit": 2, "offset": 2}, headers=aud)
    ).json()
    assert page1["total"] == page2["total"] >= 3
    assert len(page1["items"]) == 2
    assert page1["items"][0]["id"] != page2["items"][0]["id"]


async def test_audit_time_range_filter(env: Env) -> None:
    await _make_some_writes(env)
    aud = await env.login("aud")
    resp = await env.client.get(
        "/api/audit-logs", params={"ts_from": "2999-01-01T00:00:00+00:00"}, headers=aud
    )
    assert resp.json()["total"] == 0
    resp = await env.client.get(
        "/api/audit-logs", params={"ts_from": "2000-01-01T00:00:00+00:00"}, headers=aud
    )
    assert resp.json()["total"] > 0


async def test_audit_append_only_no_mutation_routes(env: Env) -> None:
    await _make_some_writes(env)
    adm = await env.login("adm")
    log_id = env.store.find("audit_logs")[0][0]["id"]
    for method in ("put", "patch", "delete"):
        resp = await env.client.request(
            method, f"/api/audit-logs/{log_id}", headers=adm, json={}
        )
        # 无更新/删除接口：FastAPI 对未注册的路径动词返回 404 或 405
        assert resp.status_code in (404, 405), method
