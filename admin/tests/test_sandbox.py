"""试聊沙盒：生产快照复制 + 草稿叠加、chat 完成判定、stop 清理与降级。"""

from __future__ import annotations

import pytest

from admin.tests.conftest import Env

pytestmark = pytest.mark.asyncio


async def _start(env: Env, headers: dict, draft: dict | None = None) -> dict:
    resp = await env.client.post(
        "/api/sandbox/start", json={"guideline_draft": draft}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_start_creates_agent_and_copies_all_prod_guidelines(env: Env) -> None:
    op = await env.login("op")
    body = await _start(env, op)
    agent_id = body["sandbox_agent_id"]
    # 上游收到一次建 agent，名字带 sandbox- 与用户名
    assert len(env.parlant.created_agents) == 1
    assert env.parlant.created_agents[0]["name"].startswith("sandbox-")
    assert "op" in env.parlant.created_agents[0]["name"]
    # 复制规则数 = 生产规则数（假上游 1 条），且带新 agent 归属标签
    assert body["copied_guidelines"] == len(env.parlant.guidelines) - body["copied_guidelines"]
    assert len(env.parlant.created_guidelines) == 1
    assert env.parlant.created_guidelines[0]["tags"] == [f"agent:{agent_id}"]
    assert env.parlant.created_guidelines[0]["condition"] == "旅客询问航班动态"
    # 审计留痕
    _, total = env.store.find("audit_logs", {"action": "sandbox.start"})
    assert total == 1


async def test_start_draft_overlays_same_condition(env: Env) -> None:
    op = await env.login("op")
    draft = {"condition": "旅客询问航班动态", "action": "不得承诺赔偿金额"}
    body = await _start(env, op, draft)
    assert body["draft_overlaid"] is True
    assert body["draft_appended"] is False
    # 同 condition 被草稿覆盖，不额外新增
    assert len(env.parlant.created_guidelines) == 1
    copied = env.parlant.created_guidelines[0]
    assert copied["condition"] == "旅客询问航班动态"
    assert copied["action"] == "不得承诺赔偿金额"


async def test_start_draft_appended_when_new_condition(env: Env) -> None:
    op = await env.login("op")
    draft = {"condition": "旅客问失物招领", "action": "引导至失物招领处"}
    body = await _start(env, op, draft)
    assert body["draft_overlaid"] is False
    assert body["draft_appended"] is True
    # 生产 1 条复制 + 草稿追加 1 条
    assert len(env.parlant.created_guidelines) == 2
    assert env.parlant.created_guidelines[1]["condition"] == "旅客问失物招领"


async def test_start_auditor_forbidden(env: Env) -> None:
    aud = await env.login("aud")
    resp = await env.client.post("/api/sandbox/start", json={}, headers=aud)
    assert resp.status_code == 403
    assert env.parlant.created_agents == []


async def test_chat_returns_reply_on_ready_completed(env: Env) -> None:
    op = await env.login("op")
    agent_id = (await _start(env, op))["sandbox_agent_id"]
    resp = await env.client.post(
        "/api/sandbox/chat",
        json={"sandbox_agent_id": agent_id, "message": "我的航班延误了怎么办"},
        headers=op,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "沙盒回复：我的航班延误了怎么办" in body["reply"]
    assert body["messages"][0]["preamble"] is False
    assert body["tools"] == []
    # 会话复用：第二次 chat 不新建 session
    resp = await env.client.post(
        "/api/sandbox/chat",
        json={"sandbox_agent_id": agent_id, "message": "再说一次"},
        headers=op,
    )
    assert resp.status_code == 200
    assert len(env.parlant.created_sessions) == 1
    # 第二轮只拼接本轮新消息
    assert resp.json()["reply"] == "沙盒回复：再说一次"


async def test_chat_supervisor_forbidden(env: Env) -> None:
    sup = await env.login("sup")
    resp = await env.client.post(
        "/api/sandbox/chat",
        json={"sandbox_agent_id": "x", "message": "y"},
        headers=sup,
    )
    assert resp.status_code == 403


async def test_stop_deletes_agent(env: Env) -> None:
    op = await env.login("op")
    agent_id = (await _start(env, op))["sandbox_agent_id"]
    resp = await env.client.post(
        "/api/sandbox/stop", json={"sandbox_agent_id": agent_id}, headers=op
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True
    assert env.parlant.deleted_agents == [agent_id]
    items, total = env.store.find("audit_logs", {"action": "sandbox.stop"})
    assert total == 1
    assert items[0]["after"]["deleted"] is True


async def test_stop_fallback_disables_guidelines_when_delete_unsupported(env: Env) -> None:
    env.parlant.delete_agent_fails = True
    op = await env.login("op")
    agent_id = (await _start(env, op))["sandbox_agent_id"]
    resp = await env.client.post(
        "/api/sandbox/stop", json={"sandbox_agent_id": agent_id}, headers=op
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is False
    # 降级：停用该沙盒 agent 的全部规则（逐条 PATCH enabled=False）
    assert body["disabled_guidelines"] == 1
    sandbox_gids = [
        g["id"] for g in env.parlant.guidelines if f"agent:{agent_id}" in (g.get("tags") or [])
    ]
    assert len(sandbox_gids) == 1
    assert env.parlant.patched_guidelines == [(sandbox_gids[0], {"enabled": False})]


def test_handle_204_no_content() -> None:
    """DELETE /agents/{id} 上游返回 204 空体：_handle 不得解析 JSON（线上 500 回归）。"""
    import httpx

    from admin.app.parlant_client import ParlantClient

    resp = httpx.Response(204)
    assert ParlantClient._handle(resp) is None
