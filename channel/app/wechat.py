"""微信 code2session 接入（方案 §2.1 / W2）：wx.login code → openid/unionid。

- 仅服务端调用（secret 不出 BFF）；session_key 不落库（README 登记后续用途：解密用户信息）
- 用户键：hash(wechat:<unionid|openid>)，unionid 优先（同一主体跨设备/跨小程序续聊）
"""

from __future__ import annotations

from typing import Any

import httpx

from channel.app.config import Settings


class WechatError(Exception):
    """微信 API 调用失败：路由层转 502（摘要不含 secret）。"""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def code2session(
    settings: Settings, code: str, client: httpx.Client | None = None
) -> dict[str, Any]:
    """调微信 jscode2session，返回含 openid（可选 unionid/session_key）的响应 dict。

    网络失败或非 200 或 errcode≠0 一律抛 WechatError（摘要不含 secret）。
    """
    url = f"{settings.wechat_api_base_url}/sns/jscode2session"
    params = {
        "appid": settings.wechat_app_id,
        "secret": settings.wechat_app_secret,
        "js_code": code,
        "grant_type": settings.wechat_grant_type,
    }
    http = client or httpx.Client(timeout=settings.wechat_timeout_s)
    try:
        resp = http.get(url, params=params)
    except httpx.HTTPError as exc:
        raise WechatError(f"微信 API 不可达：{exc}") from exc
    if resp.status_code != 200:
        raise WechatError(f"微信 API HTTP {resp.status_code}")
    body = resp.json()
    errcode = body.get("errcode", 0)
    if errcode:
        raise WechatError(f"微信错误 errcode={errcode}: {body.get('errmsg', '')}")
    if not body.get("openid"):
        raise WechatError("微信响应缺少 openid")
    return body


def user_key_for(body: dict[str, Any]) -> str:
    """微信用户键：unionid 优先于 openid（跨设备续聊），带 wechat: 前缀与匿名键隔离。"""
    return f"wechat:{body.get('unionid') or body['openid']}"
