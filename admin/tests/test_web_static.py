"""静态页托管：会话中心与三个嵌入组件页面均可访问（零构建、无外部依赖）。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from admin.tests.conftest import Env

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

PAGES = [
    "/app/",
    "/app/workbench.html",
    "/app/knowledge.html",
    "/app/rag.html",
    "/app/sandbox.html",
    "/app/canned-preview.html",
]


@pytest.mark.asyncio
async def test_static_pages_served(env: Env) -> None:
    for path in PAGES:
        resp = await env.client.get(path)
        assert resp.status_code == 200, path
        assert "text/html" in resp.headers["content-type"]


def test_pages_have_no_external_refs() -> None:
    """全部页面不得引用外部 CDN/协议链接（内网可开）。"""
    for f in WEB_DIR.iterdir():
        if f.suffix not in (".html", ".js", ".css"):
            continue
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"src=[\"']https?://", text), f
        assert not re.search(r"href=[\"']https?://", text), f
        assert not re.search(r"@import|url\(", text), f


def test_pages_interlinked_nav() -> None:
    """六页互链：每页 nav 都包含其余五页的链接（workbench 排首位，知识库第三）。"""
    links = [
        "/app/workbench.html",
        "/app/",
        "/app/knowledge.html",
        "/app/rag.html",
        "/app/sandbox.html",
        "/app/canned-preview.html",
    ]
    for page in ("index.html", "workbench.html", "knowledge.html", "rag.html", "sandbox.html", "canned-preview.html"):
        text = (WEB_DIR / page).read_text(encoding="utf-8")
        for link in links:
            assert f'href="{link}"' in text, f"{page} 缺少 {link}"
