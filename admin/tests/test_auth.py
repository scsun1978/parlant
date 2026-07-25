"""登录与五角色权限矩阵测试。"""

from __future__ import annotations

import pytest

from admin.tests.conftest import Env

pytestmark = pytest.mark.asyncio


async def test_login_success_returns_token_and_role(env: Env) -> None:
    resp = await env.client.post("/api/auth/login", json={"username": "op", "password": "pw"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "operator"
    assert body["token"]


async def test_login_failure_401_and_audited(env: Env) -> None:
    resp = await env.client.post("/api/auth/login", json={"username": "op", "password": "bad"})
    assert resp.status_code == 401
    # 登录失败也追加审计
    items, total = env.store.find("audit_logs", {"action": "auth.login"})
    assert total == 1
    assert items[0]["actor"] == "op"
    assert items[0]["after"] == {"result": "failed"}


async def test_missing_token_401(env: Env) -> None:
    resp = await env.client.get("/api/guidelines")
    assert resp.status_code == 401


async def test_any_role_can_list_guidelines(env: Env) -> None:
    headers = await env.login("sup")
    resp = await env.client.get("/api/guidelines", headers=headers)
    assert resp.status_code == 200
    assert resp.json()[0]["id"] == "g1"


async def test_operator_can_draft_guideline(env: Env) -> None:
    headers = await env.login("op")
    resp = await env.client.post(
        "/api/guidelines/draft", json={"condition": "旅客问赔偿"}, headers=headers
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "pending_review"


async def test_reviewer_cannot_draft_guideline(env: Env) -> None:
    headers = await env.login("rev")
    resp = await env.client.post(
        "/api/guidelines/draft", json={"condition": "x"}, headers=headers
    )
    assert resp.status_code == 403


async def test_auditor_cannot_draft_guideline(env: Env) -> None:
    headers = await env.login("aud")
    resp = await env.client.post(
        "/api/guidelines/draft", json={"condition": "x"}, headers=headers
    )
    assert resp.status_code == 403


async def test_supervisor_cannot_draft_guideline(env: Env) -> None:
    headers = await env.login("sup")
    resp = await env.client.post(
        "/api/guidelines/draft", json={"condition": "x"}, headers=headers
    )
    assert resp.status_code == 403


async def test_operator_cannot_approve(env: Env) -> None:
    op = await env.login("op")
    draft = (
        await env.client.post("/api/guidelines/draft", json={"condition": "x"}, headers=op)
    ).json()
    resp = await env.client.post(f"/api/approvals/{draft['id']}/approve", headers=op)
    assert resp.status_code == 403


async def test_operator_cannot_read_approval_queue(env: Env) -> None:
    op = await env.login("op")
    resp = await env.client.get("/api/approvals", headers=op)
    assert resp.status_code == 403


async def test_auditor_reads_audit_logs_operator_forbidden(env: Env) -> None:
    aud = await env.login("aud")
    resp = await env.client.get("/api/audit-logs", headers=aud)
    assert resp.status_code == 200
    op = await env.login("op")
    resp = await env.client.get("/api/audit-logs", headers=op)
    assert resp.status_code == 403


async def test_admin_can_reload_rag_operator_cannot(env: Env) -> None:
    adm = await env.login("adm")
    resp = await env.client.post("/api/rag/reload", headers=adm)
    assert resp.status_code == 200
    assert env.rag.reload_calls == 1
    op = await env.login("op")
    resp = await env.client.post("/api/rag/reload", headers=op)
    assert resp.status_code == 403
    assert env.rag.reload_calls == 1


async def test_operator_can_create_term_reviewer_cannot(env: Env) -> None:
    op = await env.login("op")
    resp = await env.client.post(
        "/api/terms",
        json={"name": "捷运", "description": "T2 至 S2 接驳列车", "synonyms": ["小火车"]},
        headers=op,
    )
    assert resp.status_code == 201
    assert env.parlant.created_terms[0]["name"] == "捷运"
    rev = await env.login("rev")
    resp = await env.client.post(
        "/api/terms", json={"name": "x", "description": "y"}, headers=rev
    )
    assert resp.status_code == 403


async def test_rag_stages_authenticated_passthrough(env: Env) -> None:
    op = await env.login("op")
    resp = await env.client.post(
        "/api/rag/stages", json={"query": "行李托运", "count": 3}, headers=op
    )
    assert resp.status_code == 200
    assert env.rag.stages_calls == [("行李托运", 3)]


async def test_journeys_passthrough(env: Env) -> None:
    aud = await env.login("aud")
    resp = await env.client.get("/api/journeys", headers=aud)
    assert resp.status_code == 200
    assert resp.json()[0]["title"] == "退改签引导"
