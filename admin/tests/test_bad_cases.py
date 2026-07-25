"""质检工作台：intake 去重、状态机迁移校验、归因 prefill、挂接、沙盒重放、关闭硬条件、已读不回告警。"""

from __future__ import annotations

import pytest

from admin.tests.conftest import Env

pytestmark = pytest.mark.asyncio


async def _intake_one(env: Env, headers: dict) -> dict:
    resp = await env.client.post("/api/bad-cases/intake", headers=headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["created"] == 1
    return body["items"][0]


# --- intake ---


async def test_intake_manual_sessions_and_dedup(env: Env) -> None:
    op = await env.login("op")
    case = await _intake_one(env, op)
    # 仅 mode=manual 的 s2 被摄入；标题=首条 customer 消息前 60 字
    assert case["session_id"] == "s2"
    assert case["source"] == "handoff_review"
    assert case["title"] == "我要投诉，行李丢了没人管"
    assert case["status"] == "pending_review"
    assert case["created_by"] == "op"
    # 二次摄入不重复建单
    again = (await env.client.post("/api/bad-cases/intake", headers=op)).json()
    assert again["created"] == 0
    items, total = env.store.find("bad_cases")
    assert total == 1
    # 审计留痕
    _, n = env.store.find("audit_logs", {"action": "badcase.intake"})
    assert n == 1


async def test_intake_auditor_forbidden(env: Env) -> None:
    aud = await env.login("aud")
    resp = await env.client.post("/api/bad-cases/intake", headers=aud)
    assert resp.status_code == 403


async def test_list_queue_with_counts(env: Env) -> None:
    op = await env.login("op")
    await _intake_one(env, op)
    aud = await env.login("aud")
    resp = await env.client.get("/api/bad-cases", headers=aud)
    assert resp.status_code == 200
    body = resp.json()
    assert body["counts"]["pending_review"] == 1
    assert body["counts"]["closed"] == 0
    resp = await env.client.get(
        "/api/bad-cases", params={"status": "pending_fix"}, headers=aud
    )
    assert resp.json()["total"] == 0


async def test_detail_includes_session_events(env: Env) -> None:
    op = await env.login("op")
    case = await _intake_one(env, op)
    aud = await env.login("aud")
    resp = await env.client.get(f"/api/bad-cases/{case['id']}", headers=aud)
    assert resp.status_code == 200
    body = resp.json()
    assert body["case"]["id"] == case["id"]
    assert [e["offset"] for e in body["events"]] == [0, 1]  # s2 一问一答


# --- attribute ---


async def test_attribute_knowledge_prefill(env: Env) -> None:
    op = await env.login("op")
    case = await _intake_one(env, op)
    resp = await env.client.post(
        f"/api/bad-cases/{case['id']}/attribute",
        json={"type": "knowledge", "note": "答非所问"},
        headers=op,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending_fix"
    assert body["attribution"] == "knowledge"
    assert body["attribution_note"] == "答非所问"
    # prefill：首条用户问题 + 末条 AI 回答 + 建议答案位
    assert body["prefill"] == {
        "question": "我要投诉，行李丢了没人管",
        "wrong_answer": "建议您去柜台问问",
        "session_id": "s2",
        "suggested_answer": "",
    }


async def test_attribute_rule_prefill(env: Env) -> None:
    op = await env.login("op")
    case = await _intake_one(env, op)
    resp = await env.client.post(
        f"/api/bad-cases/{case['id']}/attribute", json={"type": "rule"}, headers=op
    )
    assert resp.json()["prefill"] == {
        "condition": "我要投诉，行李丢了没人管",
        "action": "",
        "session_id": "s2",
    }


async def test_attribute_requires_pending_review(env: Env) -> None:
    op = await env.login("op")
    case = await _intake_one(env, op)
    await env.client.post(
        f"/api/bad-cases/{case['id']}/attribute", json={"type": "tool"}, headers=op
    )
    # 已是 pending_fix，重复归因 → 409
    resp = await env.client.post(
        f"/api/bad-cases/{case['id']}/attribute", json={"type": "model"}, headers=op
    )
    assert resp.status_code == 409


async def test_attribute_invalid_type_422(env: Env) -> None:
    op = await env.login("op")
    case = await _intake_one(env, op)
    resp = await env.client.post(
        f"/api/bad-cases/{case['id']}/attribute", json={"type": "unknown"}, headers=op
    )
    assert resp.status_code == 422


# --- link-fix ---


async def _to_pending_fix(env: Env, headers: dict) -> dict:
    case = await _intake_one(env, headers)
    resp = await env.client.post(
        f"/api/bad-cases/{case['id']}/attribute", json={"type": "rule"}, headers=headers
    )
    return resp.json()


async def test_link_fix_transitions_and_validates(env: Env) -> None:
    op = await env.login("op")
    case = await _to_pending_fix(env, op)
    # 非法枚举 → 422
    resp = await env.client.post(
        f"/api/bad-cases/{case['id']}/link-fix",
        json={"ref_type": "magic", "ref_id": "x"},
        headers=op,
    )
    assert resp.status_code == 422
    # 正常挂接 → pending_verify
    resp = await env.client.post(
        f"/api/bad-cases/{case['id']}/link-fix",
        json={"ref_type": "guideline_draft", "ref_id": "ap-1"},
        headers=op,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending_verify"
    assert body["fix_ref"] == {"type": "guideline_draft", "id": "ap-1"}


async def test_link_fix_requires_pending_fix(env: Env) -> None:
    op = await env.login("op")
    case = await _intake_one(env, op)
    resp = await env.client.post(
        f"/api/bad-cases/{case['id']}/link-fix",
        json={"ref_type": "tool_ticket", "ref_id": "t-1"},
        headers=op,
    )
    assert resp.status_code == 409  # 还在 pending_review


# --- verify / close ---


async def _to_pending_verify(env: Env, headers: dict) -> dict:
    case = await _to_pending_fix(env, headers)
    resp = await env.client.post(
        f"/api/bad-cases/{case['id']}/link-fix",
        json={"ref_type": "guideline_draft", "ref_id": "ap-1"},
        headers=headers,
    )
    return resp.json()


async def test_verify_replays_in_sandbox(env: Env) -> None:
    op = await env.login("op")
    rev = await env.login("rev")
    case = await _to_pending_verify(env, op)
    agents_before = len(env.parlant.created_agents)
    resp = await env.client.post(f"/api/bad-cases/{case['id']}/verify", headers=rev)
    assert resp.status_code == 200
    body = resp.json()
    # 沙盒被调用：建了 test agent 且用完删除
    assert len(env.parlant.created_agents) == agents_before + 1
    sandbox_agent = env.parlant.created_agents[-1]["name"]
    assert sandbox_agent.startswith("sandbox-")
    assert env.parlant.deleted_agents  # stop 已清理
    # 重放的是该 bad case 的首条用户问题，回复与 tools 存 verify
    assert "我要投诉，行李丢了没人管" in body["verify"]["reply"]
    assert body["verify"]["tools"] == []
    assert body["verify"]["checked_by"] == "rev"
    assert body["verify"]["checked_at"]
    # verify 不自动判过：状态保持 pending_verify
    assert body["status"] == "pending_verify"


async def test_verify_requires_reviewer(env: Env) -> None:
    op = await env.login("op")
    case = await _to_pending_verify(env, op)
    resp = await env.client.post(f"/api/bad-cases/{case['id']}/verify", headers=op)
    assert resp.status_code == 403


async def test_verify_requires_pending_verify(env: Env) -> None:
    op = await env.login("op")
    rev = await env.login("rev")
    case = await _to_pending_fix(env, op)
    resp = await env.client.post(f"/api/bad-cases/{case['id']}/verify", headers=rev)
    assert resp.status_code == 409


async def test_close_missing_fix_ref_and_verify_409(env: Env) -> None:
    op = await env.login("op")
    rev = await env.login("rev")
    # 构造 pending_verify 但清掉 fix_ref（绕过 link-fix 校验，直改 store 模拟缺项）
    case = await _to_pending_verify(env, op)
    env.store.update("bad_cases", case["id"], {"fix_ref": None, "verify": None})
    resp = await env.client.post(f"/api/bad-cases/{case['id']}/close", headers=rev)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "fix_ref" in detail and "verify" in detail


async def test_close_missing_verify_409(env: Env) -> None:
    op = await env.login("op")
    rev = await env.login("rev")
    case = await _to_pending_verify(env, op)  # fix_ref 已挂，verify 未做
    resp = await env.client.post(f"/api/bad-cases/{case['id']}/close", headers=rev)
    assert resp.status_code == 409
    assert "verify" in resp.json()["detail"]


async def test_close_full_path_and_regression_item(env: Env) -> None:
    op = await env.login("op")
    rev = await env.login("rev")
    case = await _to_pending_verify(env, op)
    await env.client.post(f"/api/bad-cases/{case['id']}/verify", headers=rev)
    resp = await env.client.post(f"/api/bad-cases/{case['id']}/close", headers=rev)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "closed"
    assert body["closed_at"]
    # 回归集沉淀
    items, total = env.store.find("regression_items")
    assert total == 1
    assert items[0]["source_case_id"] == case["id"]
    assert items[0]["question"] == "我要投诉，行李丢了没人管"
    # 审计全链：intake/attribute/link_fix/verify/close
    logs, _ = env.store.find("audit_logs")
    actions = [l["action"] for l in logs]
    for a in ("badcase.intake", "badcase.attribute", "badcase.link_fix", "badcase.verify", "badcase.close"):
        assert a in actions


async def test_close_requires_reviewer(env: Env) -> None:
    op = await env.login("op")
    case = await _to_pending_verify(env, op)
    resp = await env.client.post(f"/api/bad-cases/{case['id']}/close", headers=op)
    assert resp.status_code == 403


# --- reopen（打回重修） ---


async def test_reopen_back_to_pending_fix(env: Env) -> None:
    op = await env.login("op")
    rev = await env.login("rev")
    case = await _to_pending_verify(env, op)
    await env.client.post(f"/api/bad-cases/{case['id']}/verify", headers=rev)
    resp = await env.client.post(f"/api/bad-cases/{case['id']}/reopen", headers=rev)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending_fix"
    assert body["verify"]["checked_by"] == "rev"  # verify 记录保留供对比
    assert body["fix_ref"]["id"] == "ap-1"  # 挂接保留，可换挂或重修
    _, n = env.store.find("audit_logs", {"action": "badcase.reopen"})
    assert n == 1
    # 打回后可重新 link-fix 再走验证
    resp = await env.client.post(
        f"/api/bad-cases/{case['id']}/link-fix",
        json={"ref_type": "knowledge_draft", "ref_id": "kd-2"},
        headers=op,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending_verify"


async def test_reopen_requires_pending_verify_and_reviewer(env: Env) -> None:
    op = await env.login("op")
    case = await _to_pending_verify(env, op)
    rev = await env.login("rev")
    resp = await env.client.post(f"/api/bad-cases/{case['id']}/reopen", headers=op)
    assert resp.status_code == 403  # operator 不能打回
    # 打回 pending_fix 后不能再打回（状态不符 409）
    await env.client.post(f"/api/bad-cases/{case['id']}/reopen", headers=rev)
    resp = await env.client.post(f"/api/bad-cases/{case['id']}/reopen", headers=rev)
    assert resp.status_code == 409


# --- no-reply-alerts ---


async def test_no_reply_alerts(env: Env) -> None:
    aud = await env.login("aud")
    resp = await env.client.get("/api/bad-cases/no-reply-alerts", headers=aud)
    assert resp.status_code == 200
    items = resp.json()["items"]
    by_sid = {i["session_id"]: i for i in items}
    # s1 末条 customer 无 AI 回复 → 告警；s3 customer 后 error 状态 → 告警且标 has_error
    assert by_sid["s1"]["source"] == "alert_no_reply"
    assert by_sid["s1"]["has_error_status"] is False
    assert by_sid["s3"]["has_error_status"] is True
    # s2 有完整一问一答 → 不告警
    assert "s2" not in by_sid
