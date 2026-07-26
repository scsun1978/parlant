"""W2：微信 code2session 接入 + 双模配置 + token TTL。

假微信上游两条路径：路由层 monkeypatch wechat.code2session；
函数层用 httpx MockTransport 断言真实请求参数。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from channel.app import wechat
from channel.app.config import Settings
from channel.tests.conftest import Env

WECHAT_KW = {"wechat_app_id": "wx-appid-1", "wechat_app_secret": "wx-secret-1"}


@pytest.fixture()
def wenv() -> Env:
    """微信模式环境。"""
    return Env(**WECHAT_KW)


@pytest.fixture()
def fake_code2session(monkeypatch: pytest.MonkeyPatch):
    def _set(body: dict) -> None:
        monkeypatch.setattr(
            "channel.app.wechat.code2session",
            lambda settings, code, client=None: body,
        )

    return _set


# --- /channel/config ---


def test_config_anonymous_mode(env: Env) -> None:
    resp = env.client.get("/channel/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "anonymous"
    assert body["features"] == {"ws": True, "poll": True, "fallback": True}
    assert body["preamble_timeout_s"] == 0.5  # 测试注入值
    assert "secret" not in json.dumps(body).lower()  # 绝不输出 secret


def test_config_wechat_mode(wenv: Env) -> None:
    body = wenv.client.get("/channel/config").json()
    assert body["mode"] == "wechat"
    assert WECHAT_KW["wechat_app_secret"] not in json.dumps(body)
    assert "wx-secret" not in json.dumps(body)


# --- /channel/wechat/login ---


def test_wechat_login_success_and_cross_device_resume(wenv: Env, fake_code2session) -> None:
    fake_code2session({"openid": "o-1", "unionid": "u-1", "session_key": "sk-1"})
    first = wenv.client.post("/channel/wechat/login", json={"code": "code-A"}).json()
    assert first["channelToken"]
    # 发一条消息建会话；换"设备"（另一个 code，同 unionid）再登录 → 跨设备续聊
    sid = wenv.post_message(first["channelToken"])["session_id"]
    second = wenv.client.post("/channel/wechat/login", json={"code": "code-B"}).json()
    assert second["user_id"] == first["user_id"]
    assert second["resume_session_id"] == sid
    # session_key 不落库
    users, _ = wenv.store.find("channel_users")
    assert "sk-1" not in json.dumps(users, ensure_ascii=False)


def test_unionid_preferred_over_openid(wenv: Env, fake_code2session) -> None:
    fake_code2session({"openid": "o-1", "unionid": "u-1"})
    wenv.client.post("/channel/wechat/login", json={"code": "c1"})
    users, _ = wenv.store.find("channel_users")
    assert users[0]["device_hash"] == hashlib.sha256(b"wechat:u-1").hexdigest()
    # 仅 openid 时用 openid
    fake_code2session({"openid": "o-2"})
    wenv.client.post("/channel/wechat/login", json={"code": "c2"})
    users, total = wenv.store.find("channel_users")
    assert total == 2
    hashes = {u["device_hash"] for u in users}
    assert hashlib.sha256(b"wechat:o-2").hexdigest() in hashes


def test_wechat_errcode_returns_502_summary(wenv: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(settings, code, client=None):  # noqa: ANN001
        raise wechat.WechatError("微信错误 errcode=40029: invalid code")

    monkeypatch.setattr("channel.app.wechat.code2session", _raise)
    resp = wenv.client.post("/channel/wechat/login", json={"code": "bad"})
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "40029" in detail
    assert WECHAT_KW["wechat_app_secret"] not in detail


def test_wechat_login_501_in_anonymous_mode(env: Env) -> None:
    resp = env.client.post("/channel/wechat/login", json={"code": "x"})
    assert resp.status_code == 501
    assert "匿名模式" in resp.json()["detail"]


# --- code2session 函数层（MockTransport 断言真实请求） ---


def _settings() -> Settings:
    return Settings(
        mongo_url=None, mongo_db="t", parlant_base_url="http://x", **WECHAT_KW
    )


def test_code2session_request_params() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"openid": "o-1", "unionid": "u-1", "session_key": "sk"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    body = wechat.code2session(_settings(), "CODE123", client=client)
    assert body["openid"] == "o-1"
    url = seen["url"]
    assert url.startswith("https://api.weixin.qq.com/sns/jscode2session?")
    assert "appid=wx-appid-1" in url
    assert "secret=wx-secret-1" in url
    assert "js_code=CODE123" in url
    assert "grant_type=authorization_code" in url


def test_code2session_errcode_raises() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"errcode": 40029, "errmsg": "invalid code"})
        )
    )
    with pytest.raises(wechat.WechatError, match="40029"):
        wechat.code2session(_settings(), "bad", client=client)


def test_code2session_http_error_raises() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(500, text="boom"))
    )
    with pytest.raises(wechat.WechatError, match="HTTP 500"):
        wechat.code2session(_settings(), "x", client=client)


# --- token TTL ---


def test_token_expired_401_and_reissue(env: Env) -> None:
    body = env.login()
    token = body["channelToken"]
    env.post_message(token)  # 先确认可用
    # 人工把 token 过期时间改到过去
    docs, _ = env.store.find("channel_tokens", {"token": token})
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    env.store.update("channel_tokens", docs[0]["id"], {"expires_at": past})
    sid = env.store.find("channel_sessions")[0][0]["parlant_session_id"]
    resp = env.client.get(
        "/channel/history", params={"session_id": sid}, headers=env.headers(token)
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "token 已过期，请重新登录"
    # 重新登录重发新 token，旧 token 依旧失效
    new_token = env.login()["channelToken"]
    assert new_token != token
    assert env.client.get(
        "/channel/history", params={"session_id": sid}, headers=env.headers(new_token)
    ).status_code == 200


def test_token_has_expiry(env: Env) -> None:
    token = env.login()["channelToken"]
    docs, _ = env.store.find("channel_tokens", {"token": token})
    expires = datetime.fromisoformat(docs[0]["expires_at"])
    delta = expires - datetime.now(timezone.utc)
    assert timedelta(seconds=7000) < delta < timedelta(seconds=7300)  # 默认 7200s
