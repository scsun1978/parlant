"""PVG mini-program backend proxy MCP server.

Exposes Parlant-friendly MCP tools that wrap:
  1. Flight Streamable MCP (HMAC-SHA256 signed headers, per-request)
  2. POI search REST API (OAuth client-credentials bearer token)
  3. Lost & found brief REST API (same token)
  4. Knowledge search — 转发独立 RAG 检索服务（scripts/pvg_rag_service.py，
     环境变量 PVG_RAG_URL，默认 http://127.0.0.1:8901）

Credentials are loaded from environment variables (see scripts/run_pvg_proxy.sh).
"""

import hashlib
import hmac
import json
import os
import time
from typing import Any, Optional

import httpx
from fastmcp import FastMCP

FLIGHT_ENDPOINT = os.environ["PVG_FLIGHT_ENDPOINT"]
FLIGHT_APP_ID = os.environ["PVG_FLIGHT_APP_ID"]
FLIGHT_APP_KEY = os.environ["PVG_FLIGHT_APP_KEY"]
BASE_URL = os.environ["PVG_API_BASE_URL"]
CLIENT_ID = os.environ["PVG_CLIENT_ID"]
CLIENT_SECRET = os.environ["PVG_CLIENT_SECRET"]

MCP_PROTOCOL_VERSION = "2025-06-18"

mcp = FastMCP("pvg-airport-services")

# ---------------------------------------------------------------------------
# RAG 检索经独立微服务（scripts/pvg_rag_service.py）提供，本代理只做转发（PRD 3.2）
# ---------------------------------------------------------------------------

RAG_URL = os.environ.get("PVG_RAG_URL", "http://127.0.0.1:8901")


async def _rag_search(query: str, count: int) -> dict[str, Any]:
    """调用独立 RAG 检索服务（生产拓扑：检索服务化，本进程不再加载模型）。"""
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(f"{RAG_URL}/search", json={"query": query, "count": count})
        resp.raise_for_status()
        return resp.json()


_token_cache: dict[str, Any] = {"token": None, "fetched_at": 0.0}


def _flight_headers() -> dict[str, str]:
    timestamp = str(int(time.time() * 1000))
    payload = f"{FLIGHT_APP_ID}\n{timestamp}".encode()
    signature = hmac.new(FLIGHT_APP_KEY.encode(), payload, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        "X-App-Id": FLIGHT_APP_ID,
        "X-Timestamp": timestamp,
        "X-Signature": signature,
    }


async def _call_flight_tool(name: str, arguments: dict[str, Any]) -> Any:
    body = {
        "jsonrpc": "2.0",
        "id": f"proxy-{int(time.time() * 1000)}",
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    data: dict[str, Any] = {}
    last_exc: Exception | None = None
    # Retry up to 3 times: upstream flight MCP occasionally times out
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=45) as client:
                resp = await client.post(FLIGHT_ENDPOINT, headers=_flight_headers(), json=body)
                resp.raise_for_status()
                data = resp.json()
            result = data.get("result", {})
            # Upstream-level errors (e.g. "Upstream request timed out") also retry
            if "error" not in data and not result.get("isError"):
                break
            last_exc = RuntimeError(str(data.get("error") or result.get("content")))
        except (httpx.HTTPError, ValueError) as exc:
            last_exc = exc
    else:
        raise last_exc if last_exc else RuntimeError("flight tool call failed")
    if "error" in data:
        return {"error": data["error"]}
    result = data.get("result", {})
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    for block in result.get("content", []):
        if block.get("type") == "text":
            try:
                return json.loads(block["text"])
            except (ValueError, TypeError):
                return block["text"]
    return result


async def _get_access_token(client: httpx.AsyncClient) -> str:
    # Short-lived token; cache for 10 minutes
    if _token_cache["token"] and time.time() - _token_cache["fetched_at"] < 600:
        return _token_cache["token"]
    resp = await client.post(
        f"{BASE_URL}/lf-aaa/oauth/client_token/v1",
        json={"clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET},
    )
    resp.raise_for_status()
    token = resp.json()["data"]["accessToken"]
    _token_cache.update(token=token, fetched_at=time.time())
    return token


def _today_cst() -> str:
    """Today's date in Asia/Shanghai. Injected when the model omits flightDate,
    so the upstream never silently defaults to stale test data."""
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600))


@mcp.tool()
async def pvg_flight_detail(
    flightNo: str,
    flightDate: Optional[str] = None,
    departureAirport: Optional[str] = None,
    arrivalAirport: Optional[str] = None,
) -> Any:
    """Query detailed real-time status of a single flight by flight number
    (e.g. MU5101). flightDate format: YYYY-MM-DD. Returns schedule, terminal,
    gate, check-in counters, times and status. NOTE: implemented via the
    flight search backend because the native detail endpoint is currently
    failing upstream; results are equivalent."""
    args: dict[str, Any] = {"flightNo": flightNo, "page": 1, "pageSize": 5}
    args["flightDate"] = flightDate or _today_cst()
    if departureAirport:
        args["departureAirport"] = departureAirport
    if arrivalAirport:
        args["arrivalAirport"] = arrivalAirport
    result = await _call_flight_tool("pvg_flight_search", args)
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return result
    if isinstance(result, dict):
        result["note"] = (
            "served by pvg_flight_search fallback (native pvg_flight_detail "
            "is broken upstream)"
        )
        result["canned_response_fields"] = ["flight_info"]
    return result


@mcp.tool()
async def pvg_flight_search(
    flightDate: Optional[str] = None,
    direction: Optional[str] = None,
    departureCity: Optional[str] = None,
    arrivalCity: Optional[str] = None,
    airlineName: Optional[str] = None,
    terminal: Optional[str] = None,
    page: Optional[int] = None,
    pageSize: Optional[int] = None,
) -> Any:
    """Search flights at Shanghai Pudong Airport (PVG) by date, direction
    (DEPARTURE/ARRIVAL), city, airline or terminal. flightDate format:
    YYYY-MM-DD. Use this to answer questions like 'which flights go to
    Beijing today'."""
    args: dict[str, Any] = {
        "page": page or 1,
        "pageSize": min(pageSize or 10, 20),
        "flightDate": flightDate or _today_cst(),
    }
    for key, value in {
        "direction": direction,
        "departureCity": departureCity,
        "arrivalCity": arrivalCity,
        "airlineName": airlineName,
        "terminal": terminal,
    }.items():
        if value:
            args[key] = value
    result = await _call_flight_tool("pvg_flight_search", args)
    if isinstance(result, dict):
        result["canned_response_fields"] = ["flight_info"]
    return result


@mcp.tool()
async def pvg_current_datetime() -> Any:
    """Get the current local date and time (Asia/Shanghai). ALWAYS call this
    first before any flight query to determine what 'today', 'tomorrow' or
    'tonight' means, and use the returned date as flightDate."""
    now = time.strftime("%Y-%m-%d %H:%M:%S %A", time.localtime())
    return {"datetime": now, "date": time.strftime("%Y-%m-%d", time.localtime())}


@mcp.tool()
async def pvg_airport_search(
    city: Optional[str] = None,
    airportName: Optional[str] = None,
    iataCode: Optional[str] = None,
    country: Optional[str] = None,
) -> Any:
    """Look up airport dictionary records by country, city, Chinese airport
    name or IATA code (e.g. PVG). Useful to resolve city/airport names
    before flight queries."""
    args = {k: v for k, v in {
        "country": country, "city": city,
        "airportName": airportName, "iataCode": iataCode,
    }.items() if v}
    return await _call_flight_tool("pvg_airport_search", args)


@mcp.tool()
async def pvg_poi_search(search: str, count: Optional[int] = None) -> Any:
    """Search points of interest inside Pudong Airport terminals, such as
    restrooms, restaurants, shops, lounges, nursing rooms, charging spots.
    Returns name, category, floor and location of each POI."""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _get_access_token(client)
        resp = await client.get(
            f"{BASE_URL}/lf-api/system/poi_search/v1",
            params={"search": search, "anchor": 0, "count": min(count or 10, 20)},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def pvg_lost_and_found_search(search: str, count: Optional[int] = None) -> Any:
    """Search found-item records in the Pudong Airport lost & found system,
    e.g. phone, wallet, passport, luggage. Returns item name, category,
    found time, found location and terminal. Note: filing a new lost report
    must be done at the counter or via staff; this tool only searches
    already-found items."""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _get_access_token(client)
        resp = await client.get(
            f"{BASE_URL}/lf-laf/mp/laf_brief/v1",
            params={"search": search, "anchor": 0, "count": min(count or 10, 20)},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def pvg_knowledge_search(query: str, count: Optional[int] = None) -> Any:
    """Search the Pudong Airport official knowledge base (policies, rules,
    FAQ, service information: baggage rules, security restrictions, check-in
    policies, special passengers, facilities, announcements). Use this FIRST
    for any policy/rule/service question. Answers MUST be based on the
    returned knowledge content; cite the source title. If nothing relevant
    is found, say so honestly instead of improvising."""
    results = await _rag_search(query, count or 5)
    # RAG 服务返回结构与历史一致（含 results/total/canned_response_fields），直接透传
    return results


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host=os.environ.get("PVG_PROXY_HOST", "127.0.0.1"),
        port=int(os.environ.get("PVG_PROXY_PORT", "8900")),
        path="/mcp",
    )
