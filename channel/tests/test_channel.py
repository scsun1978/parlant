"""渠道 BFF W1：登录续聊、幂等、preamble、poll/history、归属校验、WS 转译与兜底。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from starlette.websockets import WebSocketDisconnect

from channel.app.translate import FALLBACK_TEXT
from channel.tests.conftest import Env


# --- login 与续聊 ---


def test_login_creates_user_hashed(env: Env) -> None:
    body = env.login("dev-1")
    assert body["channelToken"]
    assert body["user_id"]
    assert body["resume_session_id"] is None
    users, total = env.store.find("channel_users")
    assert total == 1
    # 脱敏：只存 hash(device_id)，不落明文
    assert users[0]["device_hash"] == hashlib.sha256(b"dev-1").hexdigest()
    assert "dev-1" not in str(users[0])
    # 同设备再登录复用同一用户
    again = env.login("dev-1")
    assert again["user_id"] == body["user_id"]


def test_login_resume_open_session(env: Env) -> None:
    token = env.login()["channelToken"]
    sid = env.post_message(token)["session_id"]
    again = env.login()
    assert again["resume_session_id"] == sid


def test_login_resume_window_expired(env: Env) -> None:
    token = env.login()["channelToken"]
    env.post_message(token)
    # 把会话活跃时间改到 25h 前 → 不再续聊
    sessions, _ = env.store.find("channel_sessions")
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    env.store.update("channel_sessions", sessions[0]["id"], {"last_active": old})
    assert env.login()["resume_session_id"] is None


# --- 发消息：幂等与 preamble ---


def test_message_creates_session_and_returns_preamble(env: Env) -> None:
    token = env.login()["channelToken"]
    body = env.post_message(token)
    assert body["status"] == "ok"
    assert body["session_id"]
    assert body["preamble"]["text"] == "好的，正在为您查询"
    assert body["preamble"]["display_as"] == "placeholder"
    assert body["ws_url"].startswith("/channel/messages/stream")
    # 无 open 会话才创建：Parlant 侧只建一次
    assert len(env.parlant.created_sessions) == 1
    assert env.parlant.created_sessions[0]["agent_id"] == "xyVHBNLLPg"
    # 再发一条复用同一会话
    body2 = env.post_message(token, "再问问", "m-2")
    assert body2["session_id"] == body["session_id"]
    assert len(env.parlant.created_sessions) == 1


def test_message_idempotent_duplicate(env: Env) -> None:
    token = env.login()["channelToken"]
    first = env.post_message(token, msg_id="m-dup")
    second = env.post_message(token, msg_id="m-dup")
    assert second["status"] == "duplicate"
    assert second["session_id"] == first["session_id"]
    assert second["preamble"] == first["preamble"]
    # 只调了一次上游（customer 消息只发了一次）
    customer_posts = [p for p in env.parlant.posted if p[1].get("source") == "customer"]
    assert len(customer_posts) == 1


def test_message_preamble_timeout_returns_empty(env: Env) -> None:
    env.parlant.auto_reply = False  # 引擎沉默
    token = env.login()["channelToken"]
    body = env.post_message(token)
    assert body["preamble"] is None  # 8s（测试 0.5s）内无消息不阻塞
    assert body["status"] == "ok"


def test_message_requires_auth(env: Env) -> None:
    resp = env.client.post("/channel/messages", json={"text": "x", "client_msg_id": "m-1"})
    assert resp.status_code == 401


# --- poll / history / 归属 ---


def test_poll_incremental_and_done(env: Env) -> None:
    token = env.login()["channelToken"]
    sid = env.post_message(token)["session_id"]
    body = env.client.get(
        "/channel/messages/poll", params={"session_id": sid, "after_offset": 0}, headers=env.headers(token)
    ).json()
    types = [e["type"] for e in body["events"]]
    # 转译形态：preamble(placeholder) → tool_start → content → done
    assert types == ["message_append", "tool_start", "message_append", "message_done"]
    assert body["events"][0]["display_as"] == "placeholder"
    assert body["events"][2]["display_as"] == "content"
    assert body["events"][2]["text"] == "捷运 24 小时运行，3 分钟一班。"
    assert body["done"] is True
    assert body["next_offset"] == 5
    # 增量：after_offset=3 只剩 content+done
    body2 = env.client.get(
        "/channel/messages/poll", params={"session_id": sid, "after_offset": 3}, headers=env.headers(token)
    ).json()
    assert [e["type"] for e in body2["events"]] == ["message_append", "message_done"]


def test_history_user_friendly_shape(env: Env) -> None:
    token = env.login()["channelToken"]
    sid = env.post_message(token)["session_id"]
    items = env.client.get(
        "/channel/history", params={"session_id": sid}, headers=env.headers(token)
    ).json()["items"]
    roles = [i["role"] for i in items]
    assert roles == ["user", "assistant", "assistant"]  # tool/status 不直接暴露
    assert items[0]["text"] == "捷运末班车几点"
    assert items[1]["display_as"] == "placeholder"
    assert items[2]["display_as"] == "content"
    assert all("ts" in i for i in items)


def test_history_error_as_system_row(env: Env) -> None:
    token = env.login()["channelToken"]
    sid = env.post_message(token)["session_id"]
    env.parlant._append(sid, "status", "ai_agent", {"status": "error", "data": {}})
    items = env.client.get(
        "/channel/history", params={"session_id": sid}, headers=env.headers(token)
    ).json()["items"]
    assert items[-1]["role"] == "system"
    assert items[-1]["text"] == FALLBACK_TEXT


def test_cross_user_forbidden(env: Env) -> None:
    sid = env.post_message(env.login("dev-A")["channelToken"])["session_id"]
    token_b = env.login("dev-B")["channelToken"]
    for path in ("/channel/messages/poll", "/channel/history"):
        resp = env.client.get(path, params={"session_id": sid}, headers=env.headers(token_b))
        assert resp.status_code == 403, path


# --- WS 转译与兜底 ---


def test_ws_translation_sequence(env: Env) -> None:
    token = env.login()["channelToken"]
    sid = env.post_message(token)["session_id"]
    with env.client.websocket_connect(
        f"/channel/messages/stream?token={token}&session_id={sid}"
    ) as ws:
        got = [ws.receive_json() for _ in range(4)]
    assert [m["type"] for m in got] == ["message_append", "tool_start", "message_append", "message_done"]
    assert got[1]["text"] == "正在为您查询…"
    assert got[0]["display_as"] == "placeholder"


def test_ws_timeout_fallback(env: Env) -> None:
    env.parlant.auto_reply = False
    token = env.login()["channelToken"]
    sid = env.post_message(token)["session_id"]
    with env.client.websocket_connect(
        f"/channel/messages/stream?token={token}&session_id={sid}"
    ) as ws:
        msg = ws.receive_json()  # 0.5s 无 ready/completed → 兜底
    assert msg == {"type": "fallback", "text": FALLBACK_TEXT}
    events, total = env.store.find("fallback_events")
    assert total == 1
    assert events[0]["trigger"] == "timeout"
    assert events[0]["session_id"] == sid


def test_ws_engine_error_fallback(env: Env) -> None:
    token = env.login()["channelToken"]
    sid = env.post_message(token)["session_id"]
    env.parlant._append(sid, "status", "ai_agent", {"status": "error", "data": {}})
    with env.client.websocket_connect(
        f"/channel/messages/stream?token={token}&session_id={sid}"
    ) as ws:
        msgs = [ws.receive_json() for _ in range(5)]
    assert [m["type"] for m in msgs] == [
        "message_append", "tool_start", "message_append", "message_done", "fallback",
    ]
    assert msgs[-1]["text"] == FALLBACK_TEXT
    events, _ = env.store.find("fallback_events")
    assert events[0]["trigger"] == "engine_error"


def test_ws_auth_and_ownership(env: Env) -> None:
    sid = env.post_message(env.login("dev-A")["channelToken"])["session_id"]
    # 无效 token → 4401
    with pytest.raises(WebSocketDisconnect) as exc1:
        with env.client.websocket_connect(f"/channel/messages/stream?token=bad&session_id={sid}"):
            pass
    assert exc1.value.code == 4401
    # 他人会话 → 4403
    token_b = env.login("dev-B")["channelToken"]
    with pytest.raises(WebSocketDisconnect) as exc2:
        with env.client.websocket_connect(f"/channel/messages/stream?token={token_b}&session_id={sid}"):
            pass
    assert exc2.value.code == 4403


def test_health(env: Env) -> None:
    body = env.client.get("/health").json()
    assert body == {"status": "ok", "parlant": "up", "mongo": "memory"}
