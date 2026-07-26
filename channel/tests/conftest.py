"""渠道 BFF 测试基建：内存存储 + 假 Parlant + TestClient（同步，WS 可测），全离线。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # 仓库根（admin/channel 包）

from admin.app.store import InMemoryStore
from channel.app.config import Settings
from channel.app.main import create_app


class FakeParlant:
    """假 Parlant 上游：会话/事件内存化；customer 消息触发脚本化 AI 应答。"""

    def __init__(self) -> None:
        self.healthy_flag = True
        self.sessions: list[dict[str, Any]] = []
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.created_sessions: list[dict[str, Any]] = []
        self.posted: list[tuple[str, dict[str, Any]]] = []
        self.auto_reply = True  # False 时引擎"沉默"（测 preamble 空返回与 45s 兜底）
        self._seq = 0

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    def healthy(self) -> bool:
        return self.healthy_flag

    def create_session(self, payload: dict[str, Any]) -> Any:
        self.created_sessions.append(payload)
        doc = {"id": self._next("ps"), **payload}
        self.sessions.append(doc)
        self.events[doc["id"]] = []
        return dict(doc)

    def _append(self, sid: str, kind: str, source: str, data: dict[str, Any]) -> None:
        evs = self.events[sid]
        evs.append(
            {
                "id": self._next("e"),
                "kind": kind,
                "source": source,
                "offset": (evs[-1]["offset"] + 1) if evs else 0,
                "creation_utc": "2026-07-26T10:00:00+00:00",
                "data": data,
            }
        )

    def get_events(self, session_id: str, min_offset: int = 0, wait_for_data: int = 0) -> Any:
        return [dict(e) for e in self.events.get(session_id, []) if e["offset"] >= min_offset]

    def post_event(self, session_id: str, payload: dict[str, Any]) -> Any:
        self.posted.append((session_id, payload))
        self._append(session_id, "message", payload.get("source", "customer"),
                     {"message": payload.get("message")})
        if self.auto_reply and payload.get("source") == "customer":
            self._append(session_id, "message", "ai_agent",
                         {"message": "好的，正在为您查询", "metadata": {"preamble": True}})
            self._append(session_id, "tool", "ai_agent",
                         {"tool_calls": [{"tool_name": "pvg_knowledge_search", "arguments": {}, "result": {}}]})
            self._append(session_id, "message", "ai_agent",
                         {"message": "捷运 24 小时运行，3 分钟一班。"})
            self._append(session_id, "status", "ai_agent",
                         {"status": "ready", "data": {"stage": "completed"}})
        return {"id": "e-ack"}


class Env:
    """一套测试环境：app + 内存存储 + 假上游 + TestClient（短超时便于快速断言）。"""

    def __init__(self, **overrides: Any) -> None:
        self.store = InMemoryStore()
        self.parlant = FakeParlant()
        settings = Settings(
            mongo_url=None,
            mongo_db="test",
            parlant_base_url="http://fake-parlant",
            preamble_timeout_s=overrides.pop("preamble_timeout_s", 0.5),
            done_timeout_s=overrides.pop("done_timeout_s", 0.5),
            **overrides,
        )
        self.app = create_app(settings=settings, store=self.store, parlant=self.parlant)  # type: ignore[arg-type]
        self.client = TestClient(self.app)

    def login(self, device_id: str = "dev-1") -> dict[str, Any]:
        resp = self.client.post("/channel/login", json={"device_id": device_id})
        assert resp.status_code == 200, resp.text
        return resp.json()

    def headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def post_message(self, token: str, text: str = "捷运末班车几点", msg_id: str = "m-1") -> dict[str, Any]:
        resp = self.client.post(
            "/channel/messages",
            json={"text": text, "client_msg_id": msg_id},
            headers=self.headers(token),
        )
        assert resp.status_code == 201, resp.text
        return resp.json()


@pytest.fixture()
def env() -> Env:
    return Env()
