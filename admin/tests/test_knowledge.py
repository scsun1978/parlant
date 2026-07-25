"""知识补录闭环：文档/缺口代理、草稿创建/权限、approve 生成 bundle + reindex 两分支、reject、link-fix 挂接。"""

from __future__ import annotations

from pathlib import Path

import pytest

from admin.tests.conftest import Env

pytestmark = pytest.mark.asyncio


async def _create_draft(env: Env, headers: dict) -> dict:
    resp = await env.client.post(
        "/api/knowledge/drafts",
        json={
            "question": "T2 到 S2 捷运末班车是几点？",
            "wrong_answer": "建议您去柜台问问",
            "suggested_answer": "捷运 24 小时运行，高峰 3 分钟一班。",
            "session_id": "s2",
            "bad_case_id": "bc-1",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- 只读代理 ---


async def test_docs_proxy_passthrough(env: Env) -> None:
    op = await env.login("op")
    resp = await env.client.get(
        "/api/knowledge/docs",
        params={"q": "行李", "knowledge_type": "policy", "valid_state": "valid", "page_size": 5},
        headers=op,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == "行李托运限额是多少"
    assert body["page_size"] == 5


async def test_doc_detail_and_gaps_proxy(env: Env) -> None:
    aud = await env.login("aud")
    assert (await env.client.get("/api/knowledge/docs/d1", headers=aud)).json()["text"] == "全文……"
    gaps = (await env.client.get("/api/knowledge/gaps", headers=aud)).json()
    assert gaps["items"][0]["query"] == "宠物托运"
    stats = (await env.client.get("/api/knowledge/gaps/stats", headers=aud)).json()
    assert stats["no_hit_ratio"] == 0.3


# --- 草稿创建与权限 ---


async def test_create_draft_and_list(env: Env) -> None:
    op = await env.login("op")
    doc = await _create_draft(env, op)
    assert doc["status"] == "pending_review"
    assert doc["created_by"] == "op"
    rev = await env.login("rev")
    body = (await env.client.get("/api/knowledge/drafts", params={"status": "pending_review"}, headers=rev)).json()
    assert body["total"] == 1
    _, n = env.store.find("audit_logs", {"action": "knowledge.draft"})
    assert n == 1


async def test_create_draft_reviewer_forbidden(env: Env) -> None:
    rev = await env.login("rev")
    resp = await env.client.post(
        "/api/knowledge/drafts",
        json={"question": "q", "suggested_answer": "a"},
        headers=rev,
    )
    assert resp.status_code == 403


# --- approve 全流程 ---


async def test_approve_publishes_bundle_and_reindexes(env: Env) -> None:
    op = await env.login("op")
    rev = await env.login("rev")
    draft = await _create_draft(env, op)
    resp = await env.client.post(f"/api/knowledge/drafts/{draft['id']}/approve", headers=rev)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "published"
    assert body["bundle_asset_id"].startswith("pvg.v2.kb")
    assert body["published_docs"] == 891
    assert body["reviewer"] == "rev"
    # reindex 被调用且超时放宽
    assert env.rag.reindex_calls == [{"timeout_s": 300.0}]
    # bundle 文件落盘且内容合规
    stem = body["bundle_asset_id"].removeprefix("pvg.v2.")
    files = list(Path(env.bundles_dir).glob("*.md"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert files[0].name == f"{stem}.md"
    assert f"asset_id: {body['bundle_asset_id']}" in content
    assert "reviewer: rev" in content
    assert "source_uri: admin://badcase/bc-1" in content
    assert "source_id: bc-1" in content
    assert "T2 到 S2 捷运末班车是几点？" in content
    assert "捷运 24 小时运行，高峰 3 分钟一班。" in content
    assert "status: published" in content
    assert "knowledge_type: faq" in content
    # 审计
    items, n = env.store.find("audit_logs", {"action": "knowledge.approve"})
    assert n == 1
    assert items[0]["after"]["status"] == "published"


async def test_approve_reindex_failure_keeps_approved(env: Env) -> None:
    env.rag.reindex_fails = True
    op = await env.login("op")
    rev = await env.login("rev")
    draft = await _create_draft(env, op)
    resp = await env.client.post(f"/api/knowledge/drafts/{draft['id']}/approve", headers=rev)
    assert resp.status_code == 200
    body = resp.json()
    # reindex 失败：保持 approved + 错误尾；bundle 文件不丢
    assert body["status"] == "approved"
    assert "boom" in body["reindex_error"]
    assert body["bundle_asset_id"]
    assert len(list(Path(env.bundles_dir).glob("*.md"))) == 1
    # approved 状态不可重复 approve
    again = await env.client.post(f"/api/knowledge/drafts/{draft['id']}/approve", headers=rev)
    assert again.status_code == 409


async def test_approve_double_review_constraint(env: Env) -> None:
    adm = await env.login("adm")
    draft = await _create_draft(env, adm)  # admin 起草
    resp = await env.client.post(f"/api/knowledge/drafts/{draft['id']}/approve", headers=adm)
    assert resp.status_code == 403  # 不得审批自己起草的草稿
    rev = await env.login("rev")
    assert (await env.client.post(f"/api/knowledge/drafts/{draft['id']}/approve", headers=rev)).status_code == 200


async def test_approve_operator_forbidden(env: Env) -> None:
    op = await env.login("op")
    draft = await _create_draft(env, op)
    resp = await env.client.post(f"/api/knowledge/drafts/{draft['id']}/approve", headers=op)
    assert resp.status_code == 403


# --- reject ---


async def test_reject_writes_no_bundle(env: Env) -> None:
    op = await env.login("op")
    rev = await env.login("rev")
    draft = await _create_draft(env, op)
    resp = await env.client.post(
        f"/api/knowledge/drafts/{draft['id']}/reject",
        json={"reason": "答案不准确"},
        headers=rev,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert resp.json()["reject_reason"] == "答案不准确"
    assert list(Path(env.bundles_dir).glob("*.md")) == []  # 不写文件
    assert env.rag.reindex_calls == []  # 不触发 reindex


# --- bad case 闭环挂接 ---


async def test_published_draft_linkable_in_bad_case(env: Env) -> None:
    op = await env.login("op")
    rev = await env.login("rev")
    # 补录草稿 → 发布
    draft = await _create_draft(env, op)
    published = (
        await env.client.post(f"/api/knowledge/drafts/{draft['id']}/approve", headers=rev)
    ).json()
    assert published["status"] == "published"
    # bad case 归因 knowledge → link-fix 引用 published draft → close 链路可用
    intake = (await env.client.post("/api/bad-cases/intake", headers=op)).json()
    case = intake["items"][0]
    await env.client.post(
        f"/api/bad-cases/{case['id']}/attribute", json={"type": "knowledge"}, headers=op
    )
    resp = await env.client.post(
        f"/api/bad-cases/{case['id']}/link-fix",
        json={"ref_type": "knowledge_draft", "ref_id": draft["id"]},
        headers=op,
    )
    assert resp.status_code == 200
    assert resp.json()["fix_ref"] == {"type": "knowledge_draft", "id": draft["id"]}
    assert resp.json()["status"] == "pending_verify"
