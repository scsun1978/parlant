"""CORS 中间件接线验证（Appsmith 浏览器直连前提）。"""

from __future__ import annotations

import pytest

from admin.tests.conftest import Env

pytestmark = pytest.mark.asyncio


async def test_cors_preflight_allows_appsmith_origin():
    env = Env()
    resp = await env.client.options(
        "/health",
        headers={
            "Origin": "http://172.16.100.102:8902",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") in ("*", "http://172.16.100.102:8902")


async def test_root_redirects_to_app():
    """根路径应 302 到 /app/（避免误访 / 得到 Not Found）。"""
    env = Env()
    resp = await env.client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/app/"
