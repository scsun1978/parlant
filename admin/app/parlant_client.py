"""上游客户端：Parlant REST 与 RAG 服务的薄封装（httpx 同步）。

字段契约按已核实的 Parlant REST 照抄（见任务书）：
- guideline：condition/action?/description?/title?/criticality/metadata?/enabled?/tags?/composition_mode?/priority?
- canned response：value/fields[]/signals?/tags?/metadata?/field_dependencies?
- term：name/description/synonyms?/tags?
- RAG：POST /stages {query,count}、POST /reload、GET /health
"""

from __future__ import annotations

from typing import Any

import httpx


class UpstreamError(Exception):
    """上游（Parlant/RAG）调用失败，路由层统一转 502。"""

    def __init__(self, service: str, detail: str) -> None:
        super().__init__(f"{service}: {detail}")
        self.service = service
        self.detail = detail


class ParlantClient:
    """Parlant REST 同步客户端。"""

    def __init__(
        self,
        base_url: str,
        timeout: float = 5.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout)

    @staticmethod
    def _handle(resp: httpx.Response) -> Any:
        if resp.status_code >= 400:
            raise UpstreamError("parlant", f"HTTP {resp.status_code}: {resp.text[:200]}")
        # 204 No Content（如 DELETE /agents/{id}）与空响应体不解析 JSON
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def _get(self, path: str) -> Any:
        try:
            return self._handle(self._client.get(path))
        except httpx.HTTPError as exc:
            raise UpstreamError("parlant", str(exc)) from exc

    def _send(self, method: str, path: str, payload: dict[str, Any]) -> Any:
        try:
            return self._handle(self._client.request(method, path, json=payload))
        except httpx.HTTPError as exc:
            raise UpstreamError("parlant", str(exc)) from exc

    def healthy(self) -> bool:
        """健康探测：拉一次 guidelines 列表判活。"""
        try:
            self._client.get("/guidelines")
            return True
        except httpx.HTTPError:
            return False

    # --- guidelines ---
    def list_guidelines(self) -> Any:
        return self._get("/guidelines")

    def get_guideline(self, guideline_id: str) -> Any:
        return self._get(f"/guidelines/{guideline_id}")

    def create_guideline(self, payload: dict[str, Any]) -> Any:
        return self._send("POST", "/guidelines", payload)

    def patch_guideline(self, guideline_id: str, payload: dict[str, Any]) -> Any:
        return self._send("PATCH", f"/guidelines/{guideline_id}", payload)

    # --- canned responses ---
    def list_canned_responses(self) -> Any:
        return self._get("/canned_responses")

    def create_canned_response(self, payload: dict[str, Any]) -> Any:
        return self._send("POST", "/canned_responses", payload)

    # --- terms ---
    def list_terms(self) -> Any:
        return self._get("/terms")

    def create_term(self, payload: dict[str, Any]) -> Any:
        return self._send("POST", "/terms", payload)

    # --- journeys ---
    def list_journeys(self) -> Any:
        return self._get("/journeys")

    # --- sessions ---
    def list_sessions(self) -> Any:
        return self._get("/sessions")

    def create_session(self, payload: dict[str, Any]) -> Any:
        return self._send("POST", "/sessions", payload)

    def get_events(
        self, session_id: str, min_offset: int = 0, wait_for_data: int = 0
    ) -> Any:
        """拉取会话事件；wait_for_data>0 为长轮询（上游超时返回 504，调用方需重试）。"""
        return self._get(
            f"/sessions/{session_id}/events?min_offset={min_offset}&wait_for_data={wait_for_data}"
        )

    def patch_session(self, session_id: str, payload: dict[str, Any]) -> Any:
        return self._send("PATCH", f"/sessions/{session_id}", payload)

    def post_event(self, session_id: str, payload: dict[str, Any]) -> Any:
        return self._send("POST", f"/sessions/{session_id}/events", payload)

    # --- agents（试聊沙盒：test agent 生命周期） ---
    def create_agent(self, payload: dict[str, Any]) -> Any:
        return self._send("POST", "/agents", payload)

    def delete_agent(self, agent_id: str) -> Any:
        """删除 agent；上游若无删除接口会抛 UpstreamError，由调用方降级处理。"""
        return self._send("DELETE", f"/agents/{agent_id}", {})


class RagClient:
    """RAG 检索服务同步客户端。"""

    def __init__(
        self,
        base_url: str,
        timeout: float = 5.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout)

    def stages(self, query: str, count: int) -> Any:
        try:
            resp = self._client.post("/stages", json={"query": query, "count": count})
            if resp.status_code >= 400:
                raise UpstreamError("rag", f"HTTP {resp.status_code}: {resp.text[:200]}")
            return resp.json()
        except httpx.HTTPError as exc:
            raise UpstreamError("rag", str(exc)) from exc

    def reload(self) -> Any:
        try:
            resp = self._client.post("/reload")
            if resp.status_code >= 400:
                raise UpstreamError("rag", f"HTTP {resp.status_code}: {resp.text[:200]}")
            return resp.json() if resp.content else {"status": "ok"}
        except httpx.HTTPError as exc:
            raise UpstreamError("rag", str(exc)) from exc

    def healthy(self) -> bool:
        try:
            resp = self._client.get("/health")
            return resp.status_code < 400
        except httpx.HTTPError:
            return False

    # --- 文档浏览 / 缺口看板代理 ---
    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            resp = self._client.get(path, params=params)
            if resp.status_code >= 400:
                raise UpstreamError("rag", f"HTTP {resp.status_code}: {resp.text[:200]}")
            return resp.json()
        except httpx.HTTPError as exc:
            raise UpstreamError("rag", str(exc)) from exc

    def list_docs(self, params: dict[str, Any]) -> Any:
        return self._get_json("/docs", params)

    def get_doc(self, doc_id: str) -> Any:
        return self._get_json(f"/docs/{doc_id}")

    def gaps(self, days: int, limit: int) -> Any:
        return self._get_json("/gaps", {"days": days, "limit": limit})

    def gaps_stats(self, days: int) -> Any:
        return self._get_json("/gaps/stats", {"days": days})

    def reindex(self, timeout_s: float = 300.0) -> Any:
        """重建索引并热加载（慢操作，默认 300s 超时）。"""
        try:
            resp = self._client.post("/reindex", timeout=timeout_s)
            if resp.status_code >= 400:
                raise UpstreamError("rag", f"HTTP {resp.status_code}: {resp.text[:500]}")
            return resp.json()
        except httpx.HTTPError as exc:
            raise UpstreamError("rag", str(exc)) from exc
