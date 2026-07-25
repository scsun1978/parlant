"""会话中心：列表增强、事件分页、接管/代发权限与审计、SSE 实时流。"""

from __future__ import annotations

import asyncio
import json

import pytest

from admin.tests.conftest import Env

pytestmark = pytest.mark.asyncio


# --- 列表与权限 ---


async def test_list_sessions_enriched_supervisor(env: Env) -> None:
    sup = await env.login("sup")
    resp = await env.client.get("/api/sessions", headers=sup)
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 3  # s1(auto)/s2(manual)/s3(error)
    s = [x for x in items if x["id"] == "s1"][0]
    assert s["id"] == "s1"
    assert s["mode"] == "auto"
    # 工单视图增强字段：末条为 customer 消息 → 疑似已读不回
    assert s["unanswered"] is True
    assert s["last_message"]["source"] == "customer"
    assert "MU5117" in s["last_message"]["preview"]
    assert s["last_activity_utc"] == "2026-07-22T08:02:00+00:00"


async def test_list_sessions_operator_forbidden(env: Env) -> None:
    op = await env.login("op")
    resp = await env.client.get("/api/sessions", headers=op)
    assert resp.status_code == 403


async def test_list_sessions_auditor_readonly_ok(env: Env) -> None:
    aud = await env.login("aud")
    resp = await env.client.get("/api/sessions", headers=aud)
    assert resp.status_code == 200


# --- 事件分页（trace 回放数据源） ---


async def test_events_pagination(env: Env) -> None:
    sup = await env.login("sup")
    page1 = (
        await env.client.get("/api/sessions/s1/events", params={"limit": 2}, headers=sup)
    ).json()
    assert [e["offset"] for e in page1["items"]] == [0, 1]
    assert page1["next_offset"] == 2
    page2 = (
        await env.client.get(
            "/api/sessions/s1/events",
            params={"min_offset": page1["next_offset"], "limit": 2},
            headers=sup,
        )
    ).json()
    assert [e["offset"] for e in page2["items"]] == [2, 3]


# --- 接管 / 恢复 ---


async def test_takeover_calls_upstream_and_audited(env: Env) -> None:
    sup = await env.login("sup")
    resp = await env.client.patch(
        "/api/sessions/s1/takeover", json={"mode": "manual"}, headers=sup
    )
    assert resp.status_code == 200
    assert env.parlant.patched_sessions == [("s1", {"mode": "manual"})]
    items, total = env.store.find("audit_logs", {"action": "session.takeover"})
    assert total == 1
    assert items[0]["actor"] == "sup"
    assert items[0]["target"] == "s1"
    assert items[0]["after"] == {"mode": "manual"}
    # 恢复
    resp = await env.client.patch(
        "/api/sessions/s1/takeover", json={"mode": "auto"}, headers=sup
    )
    assert resp.status_code == 200
    assert env.parlant.patched_sessions[-1] == ("s1", {"mode": "auto"})


async def test_takeover_invalid_mode_422(env: Env) -> None:
    sup = await env.login("sup")
    resp = await env.client.patch(
        "/api/sessions/s1/takeover", json={"mode": "pause"}, headers=sup
    )
    assert resp.status_code == 422
    assert env.parlant.patched_sessions == []


async def test_takeover_operator_forbidden(env: Env) -> None:
    op = await env.login("op")
    resp = await env.client.patch(
        "/api/sessions/s1/takeover", json={"mode": "manual"}, headers=op
    )
    assert resp.status_code == 403
    assert env.parlant.patched_sessions == []


# --- 人工代发 ---


async def test_human_message_carries_display_name_and_audited(env: Env) -> None:
    sup = await env.login("sup")
    resp = await env.client.post(
        "/api/sessions/s1/messages", json={"message": "您好，我是人工坐席"}, headers=sup
    )
    assert resp.status_code == 201
    sid, payload = env.parlant.posted_events[0]
    assert sid == "s1"
    assert payload["kind"] == "message"
    assert payload["source"] == "human_agent"
    assert payload["message"] == "您好，我是人工坐席"
    # human_agent 必须带 display_name：取登录用户名
    assert payload["participant"]["display_name"] == "sup"
    items, total = env.store.find("audit_logs", {"action": "session.human_message"})
    assert total == 1
    assert items[0]["actor"] == "sup"
    # 代发后末条为 human_agent 消息 → 不再判定已读不回
    listing = (await env.client.get("/api/sessions", headers=sup)).json()
    assert listing[0]["unanswered"] is False
    assert listing[0]["last_message"]["source"] == "human_agent"


async def test_human_message_operator_forbidden(env: Env) -> None:
    op = await env.login("op")
    resp = await env.client.post(
        "/api/sessions/s1/messages", json={"message": "x"}, headers=op
    )
    assert resp.status_code == 403
    assert env.parlant.posted_events == []


# --- SSE 实时流 ---
#
# 注：httpx ASGITransport / starlette TestClient 都会缓冲完整响应体（见
# httpx/_transports/asgi.py 的 ASGIResponseStream），无法测无限 SSE 流——
# 故拆为两层：路由接线用打桩的有限生成器验证；真实 _sse_stream 生成器直接单测。


async def test_stream_route_wiring_via_query_token(env: Env, monkeypatch) -> None:
    async def fake_stream(parlant, sid, start_offset):  # noqa: ANN001
        assert sid == "s1"
        assert start_offset == 2
        yield 'event: parlant-event\ndata: {"offset": 2, "kind": "message"}\n\n'

    monkeypatch.setattr("admin.app.routes.sessions._sse_stream", fake_stream)
    sup = await env.login("sup")
    token = sup["Authorization"].split(" ", 1)[1]
    # EventSource 无法带请求头，走 ?token= 查询参数
    resp = await env.client.get(f"/api/sessions/s1/stream?min_offset=2&token={token}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "event: parlant-event" in resp.text
    assert '"offset": 2' in resp.text


async def test_stream_requires_auth(env: Env) -> None:
    resp = await env.client.get("/api/sessions/s1/stream")
    assert resp.status_code == 401


async def test_stream_operator_forbidden(env: Env) -> None:
    op = await env.login("op")
    token = op["Authorization"].split(" ", 1)[1]
    resp = await env.client.get(f"/api/sessions/s1/stream?token={token}")
    assert resp.status_code == 403


async def test_sse_stream_generator_forwards_events(env: Env) -> None:
    from admin.app.routes.sessions import _sse_stream

    gen = _sse_stream(env.parlant, "s1", 0)
    chunks = []
    for _ in range(4):
        chunks.append(await asyncio.wait_for(gen.__anext__(), timeout=5))
    await gen.aclose()
    assert all(c.startswith("event: parlant-event\ndata: ") for c in chunks)
    offsets = [json.loads(c.split("data: ", 1)[1])["offset"] for c in chunks]
    assert offsets == [0, 1, 2, 3]


async def test_sse_stream_generator_retries_on_504() -> None:
    from admin.app.parlant_client import UpstreamError
    from admin.app.routes.sessions import _sse_stream
    from admin.tests.conftest import FakeParlant

    class FlakyParlant(FakeParlant):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def get_events(self, session_id, min_offset=0, wait_for_data=0):  # noqa: ANN001
            self.calls += 1
            if self.calls == 1:
                raise UpstreamError("parlant", "HTTP 504: ")  # 长轮询空超时
            return super().get_events(session_id, min_offset, wait_for_data)

    fake = FlakyParlant()
    gen = _sse_stream(fake, "s1", 0)
    first = await asyncio.wait_for(gen.__anext__(), timeout=5)
    await gen.aclose()
    assert fake.calls == 2  # 504 后重试成功
    assert '"offset": 0' in first


async def test_sse_stream_generator_reports_upstream_error() -> None:
    from admin.app.parlant_client import UpstreamError
    from admin.app.routes.sessions import _sse_stream
    from admin.tests.conftest import FakeParlant

    class DownParlant(FakeParlant):
        def get_events(self, session_id, min_offset=0, wait_for_data=0):  # noqa: ANN001
            raise UpstreamError("parlant", "HTTP 500: boom")

    gen = _sse_stream(DownParlant(), "s1", 0)
    first = await asyncio.wait_for(gen.__anext__(), timeout=5)
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(gen.__anext__(), timeout=5)
    assert first.startswith("event: error\ndata: ")
