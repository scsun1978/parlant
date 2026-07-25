"""PVG 智能客服 PoC 评测运行器（对应 PRD 7.2 节验收评测）。

对运行中的 Parlant server（development auth，无需鉴权头）逐条回放评测语料：
每条语料创建独立 session → POST 用户消息 → 长轮询拉取 ai_agent 回复 →
按 must_include / must_not_include / expected_tools 三维评分 → 输出报告。

Parlant REST 契约（v3.3.1，已对照 src/parlant/api/sessions.py 核实）：
  - POST /sessions  body {"agent_id": "<id>", "title": "eval-<语料id>"}
      → 201 返回含 "id"
  - POST /sessions/{sid}/events
      body {"kind":"message","source":"customer","message":"<query>"}
      → 201 返回事件含 "offset"
  - GET  /sessions/{sid}/events?min_offset={n}&wait_for_data=60
      → 事件列表；AI 回复事件 source="ai_agent"、kind="message"、文本在
        data.message；工具事件 kind="tool"、data.tool_calls 为数组，元素
        含 "tool_id"（格式 "service_name:tool_name"，取冒号后段比对）。
        wait_for_data 超时返回 504，需重试。
        注意：canned 模式下引擎先发 preamble（"收到/Got it"）再发正式答复，
        且正式答复可能分多条（正文+免责尾注）；须持续轮询至静默窗口
        （QUIET_SECONDS）结束，拼接全部 ai_agent message 作为评测文本。

评分维度：
  - must_include：正则数组，全部命中 AI 最终回复文本（不区分大小写）
  - must_not_include：正则数组，全部不命中（红线检测）
  - expected_tools：工具名数组，全部在会话 tool 事件中出现；
    单个元素支持 "a|b" 写法，表示任一命中即算通过
  - 延迟：客户端侧 wall-clock（POST 消息发出 → 最后一个内容事件到达，
    不含完成判定的静默窗口等待）

用法：
  uv run python evaluation/run_eval.py --corpus evaluation/corpus_full.jsonl --limit 20
  uv run python evaluation/run_eval.py --category multi_hop --concurrency 4
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

# 长轮询单次等待秒数（与服务端 wait_for_data 默认值对齐）
POLL_WAIT_SECONDS = 60

# 答复完成判定的静默窗口：preamble 之后正式答复才到达，
# 连续 QUIET_SECONDS 秒无任何新事件即认为本轮答复完成，取最后一条 ai_agent 消息
QUIET_SECONDS = 15


@dataclass
class EvalResult:
    """单条语料的评测结果。"""

    id: str
    category: str
    query: str
    passed: bool = False
    include_pass: bool = False
    exclude_pass: bool = False
    tools_pass: bool = False
    latency_s: float | None = None
    response: str = ""
    tools_observed: list[str] = field(default_factory=list)
    include_missed: list[str] = field(default_factory=list)
    exclude_hit: list[str] = field(default_factory=list)
    tools_missed: list[str] = field(default_factory=list)
    error: str | None = None


def load_corpus(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL 语料文件。"""

    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"{path} 第 {lineno} 行 JSON 解析失败: {e}") from e
    return records


def _tool_name_from_call(tool_call: dict[str, Any]) -> str | None:
    """从 tool_calls 元素提取工具名。

    v3.3.1 的 ToolCallDTO 字段为 tool_id（格式 "service_name:tool_name"），
    取冒号后段；兼容兜底读取 name 字段。
    """

    tool_id = tool_call.get("tool_id")
    if isinstance(tool_id, str) and tool_id:
        return tool_id.split(":")[-1]
    name = tool_call.get("name")
    return name if isinstance(name, str) and name else None


def _collect_tool_names(events: list[dict[str, Any]]) -> set[str]:
    """从事件列表中收集全部已调用的工具名。"""

    names: set[str] = set()
    for event in events:
        if event.get("kind") != "tool":
            continue
        data = event.get("data") or {}
        for tool_call in data.get("tool_calls") or []:
            if isinstance(tool_call, dict):
                name = _tool_name_from_call(tool_call)
                if name:
                    names.add(name)
    return names


def _find_ai_messages(events: list[dict[str, Any]]) -> list[str]:
    """从事件列表中按序取出全部 ai_agent 文本消息。"""

    texts: list[str] = []
    for event in events:
        if event.get("kind") == "message" and event.get("source") == "ai_agent":
            data = event.get("data") or {}
            message = data.get("message")
            if isinstance(message, str) and message.strip():
                texts.append(message)
    return texts


async def _run_one(
    client: httpx.AsyncClient,
    record: dict[str, Any],
    agent_id: str,
    timeout_s: float,
) -> EvalResult:
    """回放单条语料并评分（建 session → 发消息 → 长轮询 → 评分）。"""

    result = EvalResult(
        id=record["id"], category=record["category"], query=record["query"]
    )
    started = time.monotonic()
    deadline = started + timeout_s

    try:
        # 1. 创建独立 session
        resp = await client.post(
            "/sessions",
            json={"agent_id": agent_id, "title": f"eval-{record['id']}"},
        )
        resp.raise_for_status()
        session_id = resp.json()["id"]

        # 2. POST 用户消息，记录 offset 作为长轮询起点
        resp = await client.post(
            f"/sessions/{session_id}/events",
            json={"kind": "message", "source": "customer", "message": record["query"]},
        )
        resp.raise_for_status()
        min_offset = int(resp.json()["offset"]) + 1

        # 3. 长轮询直至答复完成或总超时；504 属正常超时，重试。
        # canned 模式下引擎会先发 preamble（"收到/Got it"）再发正式答复，
        # 且正式答复可能分多条消息（正文+免责尾注），因此不能取到第一条
        # ai_agent message 就停：持续轮询，直到 QUIET_SECONDS 内没有任何
        # 新事件（答复完成），拼接全部 ai_agent 消息作为评测文本。
        ai_msgs: list[str] = []
        tools_seen: set[str] = set()
        last_progress = time.monotonic()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if ai_msgs and time.monotonic() - last_progress >= QUIET_SECONDS:
                break
            wait = max(1, min(POLL_WAIT_SECONDS, math.ceil(remaining)))
            resp = await client.get(
                f"/sessions/{session_id}/events",
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
            tools_seen |= _collect_tool_names(events)
            ai_msgs.extend(_find_ai_messages(events))

        ai_text = "\n".join(ai_msgs) if ai_msgs else None

        # 延迟以"最后一个内容事件到达"为准，不含静默窗口的尾部等待
        result.latency_s = (last_progress if ai_text is not None else time.monotonic()) - started
        result.tools_observed = sorted(tools_seen)

        if ai_text is None:
            result.error = f"timeout: {timeout_s:.0f}s 内未收到 ai_agent 回复"
            return result
        result.response = ai_text

        # 4. 三维评分
        include_missed = [
            p
            for p in record.get("must_include", [])
            if not re.search(p, ai_text, flags=re.IGNORECASE)
        ]
        exclude_hit = [
            p
            for p in record.get("must_not_include", [])
            if re.search(p, ai_text, flags=re.IGNORECASE)
        ]
        # expected_tools 元素支持 "a|b"：任一备选工具出现即算该元素命中
        tools_missed = [
            spec
            for spec in record.get("expected_tools", [])
            if not any(alt in tools_seen for alt in spec.split("|"))
        ]

        result.include_missed = include_missed
        result.exclude_hit = exclude_hit
        result.tools_missed = tools_missed
        result.include_pass = not include_missed
        result.exclude_pass = not exclude_hit
        result.tools_pass = not tools_missed
        result.passed = result.include_pass and result.exclude_pass and result.tools_pass
        return result

    except (httpx.HTTPError, KeyError, ValueError) as e:
        result.error = f"{type(e).__name__}: {e}"
        result.latency_s = time.monotonic() - started
        return result


def _percentile(values: list[float], q: float) -> float | None:
    """近邻法百分位：q ∈ [0,1]，空列表返回 None。"""

    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[idx]


def build_report(
    results: list[EvalResult],
    meta: dict[str, Any],
) -> dict[str, Any]:
    """汇总明细结果为报告结构（按 category 聚合 + 总通过率 + 延迟分位）。"""

    by_category: dict[str, dict[str, Any]] = {}
    for r in results:
        bucket = by_category.setdefault(r.category, {"total": 0, "passed": 0})
        bucket["total"] += 1
        bucket["passed"] += int(r.passed)
    for bucket in by_category.values():
        bucket["pass_rate"] = round(bucket["passed"] / bucket["total"], 4)

    latencies = [r.latency_s for r in results if r.latency_s is not None]
    total = len(results)
    passed = sum(int(r.passed) for r in results)
    return {
        "meta": meta,
        "summary": {
            "total": total,
            "passed": passed,
            "pass_rate": round(passed / total, 4) if total else 0.0,
            "by_category": by_category,
            "latency_s": {
                "p50": _percentile(latencies, 0.50),
                "p95": _percentile(latencies, 0.95),
            },
        },
        "results": [
            {
                "id": r.id,
                "category": r.category,
                "query": r.query,
                "passed": r.passed,
                "include_pass": r.include_pass,
                "exclude_pass": r.exclude_pass,
                "tools_pass": r.tools_pass,
                "latency_s": round(r.latency_s, 3) if r.latency_s is not None else None,
                "tools_observed": r.tools_observed,
                "include_missed": r.include_missed,
                "exclude_hit": r.exclude_hit,
                "tools_missed": r.tools_missed,
                "error": r.error,
                "response": r.response,
            }
            for r in results
        ],
    }


def print_summary(report: dict[str, Any]) -> None:
    """控制台打印摘要表格与失败样例 id 列表。"""

    summary = report["summary"]
    print("\n========== 评测摘要 ==========")
    print(f"{'category':<14}{'总数':>6}{'通过':>6}{'通过率':>10}")
    for category, bucket in sorted(summary["by_category"].items()):
        rate = f"{bucket['pass_rate'] * 100:.1f}%"
        print(f"{category:<14}{bucket['total']:>6}{bucket['passed']:>6}{rate:>10}")
    overall = f"{summary['pass_rate'] * 100:.1f}%"
    print(f"{'TOTAL':<14}{summary['total']:>6}{summary['passed']:>6}{overall:>10}")

    latency = summary["latency_s"]
    if latency["p50"] is not None:
        print(f"\n延迟（wall-clock）：p50={latency['p50']:.2f}s  p95={latency['p95']:.2f}s")

    failed = [r for r in report["results"] if not r["passed"]]
    if failed:
        print(f"\n失败样例（{len(failed)} 条）：")
        for r in failed:
            reasons: list[str] = []
            if r["error"]:
                reasons.append(r["error"])
            if r["include_missed"]:
                reasons.append(f"must_include 未命中: {r['include_missed']}")
            if r["exclude_hit"]:
                reasons.append(f"must_not_include 命中红线: {r['exclude_hit']}")
            if r["tools_missed"]:
                reasons.append(f"expected_tools 未调用: {r['tools_missed']}")
            print(f"  {r['id']}  {'; '.join(reasons)}")
    else:
        print("\n全部通过，无失败样例。")


async def run(args: argparse.Namespace) -> int:
    """主流程：加载语料 → 并发回放 → 写报告。"""

    records = load_corpus(Path(args.corpus))
    if args.category:
        records = [r for r in records if r["category"] == args.category]
    if args.limit:
        records = records[: args.limit]
    if not records:
        print("过滤后语料为空，请检查 --corpus/--category/--limit 参数", file=sys.stderr)
        return 1

    semaphore = asyncio.Semaphore(args.concurrency)
    limits = httpx.Limits(
        max_connections=max(4, args.concurrency * 2),
        max_keepalive_connections=max(4, args.concurrency * 2),
    )
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"), timeout=30.0, limits=limits
    ) as client:

        async def guarded(record: dict[str, Any]) -> EvalResult:
            async with semaphore:
                return await _run_one(client, record, args.agent_id, args.timeout)

        print(f"开始评测：{len(records)} 条语料，并发 {args.concurrency}，单条超时 {args.timeout}s")
        results = await asyncio.gather(*(guarded(r) for r in records))

    meta = {
        "base_url": args.base_url,
        "agent_id": args.agent_id,
        "corpus": args.corpus,
        "category_filter": args.category,
        "limit": args.limit,
        "concurrency": args.concurrency,
        "timeout_s": args.timeout,
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    report = build_report(list(results), meta)

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = report_dir / f"eval-report-{stamp}.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 固定文件名副本，便于 CI/脚本取最新报告
    (report_dir / "eval-report-latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print_summary(report)
    print(f"\n报告已写入：{report_path}")
    return 0


def main() -> int:
    """命令行入口。"""

    parser = argparse.ArgumentParser(description="PVG 智能客服 PoC 评测运行器（PRD 7.2）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8800", help="Parlant server 地址")
    parser.add_argument("--agent-id", default="xyVHBNLLPg", help="被测 agent id")
    parser.add_argument("--corpus", default="evaluation/corpus_full.jsonl", help="评测语料 JSONL 路径")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条")
    parser.add_argument("--category", default=None, help="只跑指定 category（normal/long_tail/adversarial/multi_hop）")
    parser.add_argument("--concurrency", type=int, default=2, help="并发 session 数，默认 2")
    parser.add_argument("--timeout", type=float, default=90.0, help="单条语料总超时秒数，默认 90")
    parser.add_argument("--report-dir", default="evaluation/reports", help="报告输出目录")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
