"""非线性多轮对照评测：同一组场景分别打 A（Parlant）与 B（hermes robot API）。

用法：
  # A 侧（本地 Parlant，保持同一 session 顺序发多轮）
  uv run python evaluation/cross/run_multiturn.py --target a
  # B 侧（robot API，经 SSH 隧道 127.0.0.1:8281，session_id 保持连续）
  uv run python evaluation/cross/run_multiturn.py --target b

判定：每轮按 expect 正则数组命中计分（turn pass=全部命中）；场景 pass=全部轮通过。
输出：evaluation/reports/multiturn-<target>-<ts>.json（含完整逐轮文本供人工复核）。

环境变量：PARTNER_ROBOT_API_TOKEN（target=b 时必需）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

A_BASE = "http://127.0.0.1:8800"
A_AGENT = "xyVHBNLLPg"
B_BASE = "http://127.0.0.1:8281"
QUIET_SECONDS = 15
POLL_WAIT = 20

SCENARIOS = Path(__file__).parent / "multiturn_scenarios.json"


# ---------------------------------------------------------------- A 侧（复用 run_eval 轮询逻辑）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_eval as base  # noqa: E402


async def ask_a(client: httpx.AsyncClient, session_id: str, query: str, timeout: float) -> tuple[str, list[str], float]:
    """A 侧单轮：发消息 → 等待 status=ready（本轮完成）→ 拼接全部 ai 消息。

    完成判定用引擎的 ready 状态事件（acknowledged→processing→ready 序列），
    比静默窗口可靠：stream 模式下生成间隔可超过静默窗口导致只拿到 preamble。
    """
    resp = await client.post(
        f"{A_BASE}/sessions/{session_id}/events",
        json={"kind": "message", "source": "customer", "message": query},
    )
    resp.raise_for_status()
    min_offset = int(resp.json()["offset"]) + 1
    t0 = time.monotonic()
    deadline = t0 + timeout
    msgs: list[str] = []
    tools: set[str] = set()
    saw_ready = False
    last_progress = t0
    while time.monotonic() < deadline and not saw_ready:
        if msgs and time.monotonic() - last_progress >= 45:  # 兜底静默（completed 丢失时）
            break
        wait = max(1, min(POLL_WAIT, int(deadline - time.monotonic())))
        resp = await client.get(
            f"{A_BASE}/sessions/{session_id}/events",
            params={"min_offset": min_offset, "wait_for_data": wait},
            timeout=wait + 15,
        )
        if resp.status_code == 504:
            continue
        resp.raise_for_status()
        events = resp.json()
        if events:
            last_progress = time.monotonic()
            min_offset = max(int(e["offset"]) for e in events) + 1
        tools |= base._collect_tool_names(events)
        msgs.extend(base._find_ai_messages(events))
        for e in events:
            if e["kind"] == "status" and e["source"] == "ai_agent":
                data = e.get("data") or {}
                stage = (data.get("data") or {}).get("stage")
                if data.get("status") == "ready" and stage == "completed":
                    saw_ready = True
    return "\n".join(msgs), sorted(tools), time.monotonic() - t0


async def run_a(scenarios: list[dict[str, Any]], timeout: float) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=60) as client:
        for sc in scenarios:
            resp = await client.post(
                f"{A_BASE}/sessions",
                json={"agent_id": A_AGENT, "title": f"mt-{sc['id']}"},
            )
            resp.raise_for_status()
            sid = resp.json()["id"]
            turns = []
            for t in sc["turns"]:
                text, tools, lat = await ask_a(client, sid, t["q"], timeout)
                turns.append({"q": t["q"], "expect": t["expect"], "reply": text, "tools": tools, "latency_s": round(lat, 1)})
            results.append({"id": sc["id"], "theme": sc["theme"], "turns": turns})
            print(f"  {sc['id']} done", flush=True)
    return results


# ---------------------------------------------------------------- B 侧
async def ask_b(client: httpx.AsyncClient, token: str, session_id: str, query: str, timeout: float) -> tuple[str, list[str], float]:
    body = {
        "request_id": f"mt-{int(time.time() * 1000)}",
        "session_id": session_id,
        "terminal_id": "cross-eval",
        "input_text": query,
        "channel": "robot_kiosk",
        "context": {"airport": "PVG", "terminal": "T2", "floor": "3F", "location_id": "T2_DEP_3F_NEAR_GATE_70"},
    }
    t0 = time.monotonic()
    resp = await client.post(
        f"{B_BASE}/robot/chat",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    robot = data.get("robot") if isinstance(data.get("robot"), dict) else {}
    # 与 run_cross_b 一致：reply 优先（实质内容），回退 screen_text / speech_text
    reply = payload.get("reply")
    if not (isinstance(reply, str) and reply.strip()):
        screen = robot.get("screen_text")
        if isinstance(screen, dict):
            reply = "\n".join(p for p in [str(screen.get("title") or "").strip(), str(screen.get("summary") or "").strip()] if p)
        else:
            reply = screen if isinstance(screen, str) else ""
    if not reply:
        reply = robot.get("speech_text") or ""
    tools = []
    for item in data.get("used_tools") or []:
        if isinstance(item, dict) and item.get("name"):
            tools.append(item["name"])
    if data.get("references"):
        tools.append("pvg.knowledge.search")
    return reply, tools, time.monotonic() - t0


async def run_b(scenarios: list[dict[str, Any]], timeout: float) -> list[dict[str, Any]]:
    token = os.environ.get("PARTNER_ROBOT_API_TOKEN")
    if not token:
        print("缺少 PARTNER_ROBOT_API_TOKEN", file=sys.stderr)
        sys.exit(2)
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=timeout + 30) as client:
        for sc in scenarios:
            sid = f"cross-mt-{sc['id']}"
            turns = []
            for t in sc["turns"]:
                text, tools, lat = await ask_b(client, token, sid, t["q"], timeout)
                turns.append({"q": t["q"], "expect": t["expect"], "reply": text, "tools": tools, "latency_s": round(lat, 1)})
            results.append({"id": sc["id"], "theme": sc["theme"], "turns": turns})
            print(f"  {sc['id']} done", flush=True)
    return results


def score(results: list[dict[str, Any]]) -> dict[str, Any]:
    total_turns = passed_turns = passed_scenarios = 0
    for sc in results:
        sc_pass = True
        for t in sc["turns"]:
            text = t["reply"] or ""
            missed = [p for p in t["expect"] if not re.search(p, text, flags=re.IGNORECASE)]
            t["pass"] = not missed
            t["missed"] = missed
            total_turns += 1
            passed_turns += 0 if missed else 1
            sc_pass = sc_pass and not missed
        sc["pass"] = sc_pass
        passed_scenarios += 1 if sc_pass else 0
    return {
        "scenarios_total": len(results),
        "scenarios_pass": passed_scenarios,
        "turns_total": total_turns,
        "turns_pass": passed_turns,
        "scenario_rate": round(passed_scenarios / len(results) * 100, 1),
        "turn_rate": round(passed_turns / total_turns * 100, 1),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=["a", "b"], required=True)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--report-dir", default="evaluation/reports")
    args = parser.parse_args()

    scenarios = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    print(f"非线性多轮对照：target={args.target}，场景 {len(scenarios)} 个")
    results = await (run_a(scenarios, args.timeout) if args.target == "a" else run_b(scenarios, args.timeout))
    summary = score(results)
    print(json.dumps(summary, ensure_ascii=False))

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.report_dir) / f"multiturn-{args.target}-{ts}.json"
    out.write_text(json.dumps({"target": args.target, "summary": summary, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已写入：{out}")


if __name__ == "__main__":
    asyncio.run(main())
