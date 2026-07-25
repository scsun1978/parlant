"""测试基建：内存存储 + 假上游（Parlant/RAG）+ ASGI 测试客户端，全部离线可跑。"""

from __future__ import annotations

from typing import Any

from collections.abc import AsyncIterator

import httpx
import pytest_asyncio

from admin.app.config import Settings
from admin.app.main import create_app
from admin.app.store import InMemoryStore

USERS = [
    {"username": "op", "password": "pw", "role": "operator"},
    {"username": "rev", "password": "pw", "role": "reviewer"},
    {"username": "aud", "password": "pw", "role": "auditor"},
    {"username": "sup", "password": "pw", "role": "supervisor"},
    {"username": "adm", "password": "pw", "role": "admin"},
]


class FakeParlant:
    """假 Parlant 上游：内存数据 + 调用记录，断言 BFF 透传字段正确。"""

    def __init__(self) -> None:
        self.healthy_flag = True
        self.guidelines: list[dict[str, Any]] = [
            {"id": "g1", "condition": "旅客询问航班动态", "action": "调用航班工具查询"}
        ]
        self.canned: list[dict[str, Any]] = []
        self.terms: list[dict[str, Any]] = []
        self.journeys: list[dict[str, Any]] = [
            {"id": "j1", "title": "退改签引导", "description": "", "triggers": ["退票"]}
        ]
        self.created_guidelines: list[dict[str, Any]] = []
        self.patched_guidelines: list[tuple[str, dict[str, Any]]] = []
        self.created_canned: list[dict[str, Any]] = []
        self.created_terms: list[dict[str, Any]] = []
        # --- sessions（会话中心 / 质检工作台） ---
        self.sessions: list[dict[str, Any]] = [
            {
                "id": "s1",
                "agent_id": "a1",
                "customer_id": "c1",
                "creation_utc": "2026-07-22T08:00:00+00:00",
                "title": "旅客 A",
                "mode": "auto",
                "consumption_offsets": {},
                "metadata": {},
                "labels": [],
            },
            {
                "id": "s2",
                "agent_id": "a1",
                "customer_id": "c2",
                "creation_utc": "2026-07-22T09:00:00+00:00",
                "title": "旅客 B（转人工）",
                "mode": "manual",  # 转人工复盘 intake 来源
                "consumption_offsets": {},
                "metadata": {},
                "labels": [],
            },
            {
                "id": "s3",
                "agent_id": "a1",
                "customer_id": "c3",
                "creation_utc": "2026-07-22T10:00:00+00:00",
                "title": "旅客 C（已读不回）",
                "mode": "auto",
                "consumption_offsets": {},
                "metadata": {},
                "labels": [],
            },
        ]
        # 每个会话的事件流（按 offset 升序）
        self.session_events: dict[str, list[dict[str, Any]]] = {
            # s1 末条为 customer 消息 → 疑似已读不回
            "s1": [
                {
                    "id": "e1",
                    "kind": "message",
                    "source": "customer",
                    "offset": 0,
                    "creation_utc": "2026-07-22T08:01:00+00:00",
                    "data": {"message": "你好，我的航班延误了"},
                },
                {
                    "id": "e2",
                    "kind": "status",
                    "source": "ai_agent",
                    "offset": 1,
                    "creation_utc": "2026-07-22T08:01:01+00:00",
                    "data": {"status": "acknowledged", "data": {}},
                },
                {
                    "id": "e3",
                    "kind": "message",
                    "source": "ai_agent",
                    "offset": 2,
                    "creation_utc": "2026-07-22T08:01:05+00:00",
                    "data": {"message": "请提供航班号，我帮您查"},
                },
                {
                    "id": "e4",
                    "kind": "message",
                    "source": "customer",
                    "offset": 3,
                    "creation_utc": "2026-07-22T08:02:00+00:00",
                    "data": {"message": "MU5117"},
                },
            ],
            # s2 转人工会话：有完整一问一答（bad case 归因 prefill 来源）
            "s2": [
                {
                    "id": "e5",
                    "kind": "message",
                    "source": "customer",
                    "offset": 0,
                    "creation_utc": "2026-07-22T09:01:00+00:00",
                    "data": {"message": "我要投诉，行李丢了没人管"},
                },
                {
                    "id": "e6",
                    "kind": "message",
                    "source": "ai_agent",
                    "offset": 1,
                    "creation_utc": "2026-07-22T09:01:05+00:00",
                    "data": {"message": "建议您去柜台问问"},
                },
            ],
            # s3 已读不回特例：customer 消息后只有 error 状态，无 AI 回复
            "s3": [
                {
                    "id": "e7",
                    "kind": "message",
                    "source": "customer",
                    "offset": 0,
                    "creation_utc": "2026-07-22T10:01:00+00:00",
                    "data": {"message": "为什么没人回复我"},
                },
                {
                    "id": "e8",
                    "kind": "status",
                    "source": "ai_agent",
                    "offset": 1,
                    "creation_utc": "2026-07-22T10:01:02+00:00",
                    "data": {"status": "error", "data": {}},
                },
            ],
        }
        self.patched_sessions: list[tuple[str, dict[str, Any]]] = []
        self.posted_events: list[tuple[str, dict[str, Any]]] = []
        # --- agents（试聊沙盒） ---
        self.agents: list[dict[str, Any]] = [{"id": "xyVHBNLLPg", "name": "pvg-prod"}]
        self.created_agents: list[dict[str, Any]] = []
        self.deleted_agents: list[str] = []
        self.created_sessions: list[dict[str, Any]] = []
        self.delete_agent_fails = False  # 置 True 模拟上游无删除接口（stop 走降级）
        self._seq = 0

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    def healthy(self) -> bool:
        return self.healthy_flag

    def list_guidelines(self) -> Any:
        return [dict(g) for g in self.guidelines]  # 返回拷贝，防遍历时追加死循环

    def get_guideline(self, guideline_id: str) -> Any:
        for g in self.guidelines:
            if g["id"] == guideline_id:
                return dict(g)
        from admin.app.parlant_client import UpstreamError

        raise UpstreamError("parlant", f"HTTP 404: guideline {guideline_id}")

    def create_guideline(self, payload: dict[str, Any]) -> Any:
        self.created_guidelines.append(payload)
        doc = {"id": self._next_id("g"), **payload}
        self.guidelines.append(doc)  # 建后可被 list/get 查到（沙盒 stop 降级依赖）
        return doc

    def patch_guideline(self, guideline_id: str, payload: dict[str, Any]) -> Any:
        self.patched_guidelines.append((guideline_id, payload))
        return {"id": guideline_id, **payload}

    def list_canned_responses(self) -> Any:
        return self.canned

    def create_canned_response(self, payload: dict[str, Any]) -> Any:
        self.created_canned.append(payload)
        return {"id": self._next_id("c"), **payload}

    def list_terms(self) -> Any:
        return self.terms

    def create_term(self, payload: dict[str, Any]) -> Any:
        self.created_terms.append(payload)
        return {"id": self._next_id("t"), **payload}

    def list_journeys(self) -> Any:
        return self.journeys

    # --- sessions ---
    def list_sessions(self) -> Any:
        return [dict(s) for s in self.sessions]

    def get_events(
        self, session_id: str, min_offset: int = 0, wait_for_data: int = 0
    ) -> Any:
        """返回 min_offset 之后的事件（长轮询语义：无新事件即空列表，由 BFF 侧重试）。"""
        return [
            dict(e)
            for e in self.session_events.get(session_id, [])
            if e.get("offset", 0) >= min_offset
        ]

    def patch_session(self, session_id: str, payload: dict[str, Any]) -> Any:
        self.patched_sessions.append((session_id, payload))
        for s in self.sessions:
            if s["id"] == session_id:
                s.update(payload)
                return dict(s)
        from admin.app.parlant_client import UpstreamError

        raise UpstreamError("parlant", f"HTTP 404: session {session_id}")

    def post_event(self, session_id: str, payload: dict[str, Any]) -> Any:
        self.posted_events.append((session_id, payload))
        events = self.session_events.setdefault(session_id, [])
        offset = (events[-1].get("offset", -1) + 1) if events else 0
        event = {
            "id": self._next_id("e"),
            "kind": payload.get("kind", "message"),
            "source": payload.get("source", "human_agent"),
            "offset": offset,
            "creation_utc": "2026-07-22T08:03:00+00:00",
            "data": {
                "message": payload.get("message"),
                "participant": payload.get("participant", {}),
            },
        }
        events.append(event)
        if payload.get("source") == "customer":
            # 模拟引擎应答：AI 消息 + ready/completed 状态（沙盒 chat 完成判定依赖）
            events.append(
                {
                    "id": self._next_id("e"),
                    "kind": "message",
                    "source": "ai_agent",
                    "offset": offset + 1,
                    "creation_utc": "2026-07-22T08:03:01+00:00",
                    "data": {"message": f"沙盒回复：{payload.get('message')}"},
                }
            )
            events.append(
                {
                    "id": self._next_id("e"),
                    "kind": "status",
                    "source": "ai_agent",
                    "offset": offset + 2,
                    "creation_utc": "2026-07-22T08:03:02+00:00",
                    "data": {"status": "ready", "data": {"stage": "completed"}},
                }
            )
        return dict(event)

    # --- agents / sessions 创建（试聊沙盒） ---
    def create_agent(self, payload: dict[str, Any]) -> Any:
        self.created_agents.append(payload)
        doc = {"id": self._next_id("a"), **payload}
        self.agents.append(doc)
        return dict(doc)

    def delete_agent(self, agent_id: str) -> Any:
        if self.delete_agent_fails:
            from admin.app.parlant_client import UpstreamError

            raise UpstreamError("parlant", "HTTP 405: delete agent unsupported")
        self.deleted_agents.append(agent_id)
        self.agents = [a for a in self.agents if a["id"] != agent_id]
        return None

    def create_session(self, payload: dict[str, Any]) -> Any:
        self.created_sessions.append(payload)
        sid = self._next_id("sess")
        self.session_events[sid] = []
        doc = {
            "id": sid,
            "agent_id": payload.get("agent_id"),
            "customer_id": None,
            "creation_utc": "2026-07-22T08:03:00+00:00",
            "title": payload.get("title"),
            "mode": "auto",
            "consumption_offsets": {},
            "metadata": {},
            "labels": [],
        }
        self.sessions.append(doc)
        return dict(doc)


class FakeRag:
    """假 RAG 上游。"""

    def __init__(self) -> None:
        self.healthy_flag = True
        self.stages_calls: list[tuple[str, int]] = []
        self.reload_calls = 0
        # --- 文档浏览 / 缺口看板 ---
        self.docs = [
            {
                "id": "d1",
                "title": "行李托运限额是多少",
                "knowledge_type": "policy",
                "valid_from": None,
                "valid_until": "2026-10-17T00:00:00+08:00",
                "valid_state": "valid",
                "snippet": "国内航班经济舱免费托运行李额为 20 公斤。",
            }
        ]
        self.doc_detail = {"id": "d1", "title": "行李托运限额是多少", "text": "全文……", "valid_state": "valid"}
        self.gaps_result = {"days": 7, "items": [{"query": "宠物托运", "count": 3}], "total": 1}
        self.gaps_stats_result = {"window_days": 7, "total_queries": 10, "no_hit_count": 3, "no_hit_ratio": 0.3}
        # --- reindex ---
        self.reindex_calls: list[dict[str, Any]] = []
        self.reindex_fails = False

    def stages(self, query: str, count: int) -> Any:
        self.stages_calls.append((query, count))
        return {"bm25": [], "vector": [], "rrf": []}

    def reload(self) -> Any:
        self.reload_calls += 1
        return {"status": "reloaded"}

    def healthy(self) -> bool:
        return self.healthy_flag

    def list_docs(self, params: dict[str, Any]) -> Any:
        return {
            "total": len(self.docs),
            "page": params.get("page", 1),
            "page_size": params.get("page_size", 20),
            "items": self.docs,
        }

    def get_doc(self, doc_id: str) -> Any:
        return self.doc_detail

    def gaps(self, days: int, limit: int) -> Any:
        return self.gaps_result

    def gaps_stats(self, days: int) -> Any:
        return self.gaps_stats_result

    def reindex(self, timeout_s: float = 300.0) -> Any:
        from admin.app.parlant_client import UpstreamError

        self.reindex_calls.append({"timeout_s": timeout_s})
        if self.reindex_fails:
            raise UpstreamError("rag", "HTTP 500: {'status': 'failed', 'log_tail': ['boom']}")
        return {"status": "completed", "docs": 891, "duration_s": 95.2, "log_tail": ["index written"]}


class Env:
    """一套测试环境：app + 内存存储 + 假上游 + ASGI 客户端。"""

    def __init__(self) -> None:
        import tempfile

        self.store = InMemoryStore()
        self.parlant = FakeParlant()
        self.rag = FakeRag()
        self.bundles_dir = tempfile.mkdtemp(prefix="bundles-")  # 假 BUNDLES_DIR
        settings = Settings(
            mongo_url=None,
            mongo_db="test",
            parlant_base_url="http://fake-parlant",
            rag_url="http://fake-rag",
            bundles_dir=self.bundles_dir,
            users=USERS,
        )
        self.app = create_app(
            settings=settings, store=self.store, parlant=self.parlant, rag=self.rag  # type: ignore[arg-type]
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )

    async def login(self, username: str, password: str = "pw") -> dict[str, str]:
        """登录并返回 Authorization 头。"""
        resp = await self.client.post(
            "/api/auth/login", json={"username": username, "password": password}
        )
        assert resp.status_code == 200, resp.text
        return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest_asyncio.fixture()
async def env() -> AsyncIterator[Env]:
    e = Env()
    yield e
    await e.client.aclose()
