"""Canned response 与 guideline 同构审批流的冒烟测试。"""

from __future__ import annotations

import pytest

from admin.tests.conftest import Env

pytestmark = pytest.mark.asyncio

CANNED_PAYLOAD = {
    "value": "您好，航班 {{flight_no}} 当前状态为 {{flight_status}}。",
    "fields": [
        {"name": "flight_no", "description": "航班号", "examples": ["MU5117"]},
        {"name": "flight_status", "description": "航班状态", "examples": ["延误"]},
    ],
    "tags": ["航班"],
}


async def test_canned_draft_approve_smoke(env: Env) -> None:
    op = await env.login("op")
    resp = await env.client.post("/api/canned-responses/draft", json=CANNED_PAYLOAD, headers=op)
    assert resp.status_code == 201
    draft = resp.json()
    assert draft["kind"] == "canned_response"
    assert draft["status"] == "pending_review"
    assert env.parlant.created_canned == []

    rev = await env.login("rev")
    resp = await env.client.post(f"/api/approvals/{draft['id']}/approve", headers=rev)
    assert resp.status_code == 200
    assert resp.json()["upstream_id"]
    assert env.parlant.created_canned == [CANNED_PAYLOAD]


async def test_canned_list_passthrough(env: Env) -> None:
    op = await env.login("op")
    resp = await env.client.get("/api/canned-responses", headers=op)
    assert resp.status_code == 200
    assert resp.json() == []


# --- 话术渲染预览（jinja2 StrictUndefined） ---


async def test_preview_renders_template(env: Env) -> None:
    op = await env.login("op")
    resp = await env.client.post(
        "/api/canned/preview",
        json={
            "value": "您好，航班 {{flight_no}} 当前状态为 {{flight_status}}。",
            "sample_fields": {"flight_no": "MU5117", "flight_status": "延误"},
        },
        headers=op,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] is None
    assert body["rendered"] == "您好，航班 MU5117 当前状态为 延误。"


async def test_preview_strict_undefined_reports_error(env: Env) -> None:
    op = await env.login("op")
    resp = await env.client.post(
        "/api/canned/preview",
        json={"value": "{{knowledge_answer}}（来源：{{knowledge_source}}）", "sample_fields": {}},
        headers=op,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["rendered"] is None
    assert "knowledge_answer" in body["error"]  # 槽位悬空即报错，防线上模板不可选


async def test_preview_syntax_error_reported(env: Env) -> None:
    rev = await env.login("rev")
    resp = await env.client.post(
        "/api/canned/preview", json={"value": "{{ unclosed", "sample_fields": {}}, headers=rev
    )
    assert resp.status_code == 200
    assert resp.json()["error"]


async def test_preview_auditor_forbidden(env: Env) -> None:
    aud = await env.login("aud")
    resp = await env.client.post(
        "/api/canned/preview", json={"value": "x", "sample_fields": {}}, headers=aud
    )
    assert resp.status_code == 403
