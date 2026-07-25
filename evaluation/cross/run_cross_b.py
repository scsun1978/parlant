"""交叉评测（方向 B）：A 的 66 条语料打系统 B（pvg-agent-demo Robot API）。

用途：与 A（本方 Parlant 客服）做 bake-off。逐条回放 evaluation/corpus_v1.jsonl，
调用 B 的 Robot API（POST {base-url}/robot/chat，Bearer 鉴权，契约见 B 侧
docs/next-generation-agent-robot-digital-human-integration.md §4-§6），
把 B 的响应映射回 A 的词汇表后用 A 的评分口径（must_include /
must_not_include 正则 + expected_tools 比对，与 evaluation/run_eval.py 一致）
输出同结构报告。

响应映射：
  - 回复文本：顶层 reply 优先（实质内容），依次回退 data.robot.screen_text
    (title+summary)、speech_text（2026-07-21 实测：部分 skill 的口播为错配模板）；
  - 工具：data.used_tools + presentation.intelligence.tool_chain 中的 B 工具名
    经 TOOL_MAP_B_TO_A 映射为 A 工具名后与 expected_tools 比对（"a|b" 任一
    语义与 run_eval 一致）；
  - 多轮：语料含 turns 字段时按同一 session_id 顺序发送（§5 会话隔离要求），
    各轮回复拼接、工具取并集后评分。

token 从环境变量 PARTNER_ROBOT_API_TOKEN 读取，缺失时清晰报错退出。

用法：
  export PARTNER_ROBOT_API_TOKEN='<由项目方发放>'
  uv run python evaluation/cross/run_cross_b.py --limit 10
  uv run python evaluation/cross/run_cross_b.py --dry-run   # 只打印将发送的请求
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

# 复用 A 侧评测运行器的语料加载、评分与报告逻辑；run_eval.py 无包结构，
# 直接把 evaluation/ 加入 sys.path。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run_eval  # noqa: E402

# B 侧默认上下文（与 B 评测脚本 eval_complete_robot_corpus.DEFAULT_CONTEXT 一致；
# 缺 location_id 时路线/最近设施问题会被 B 追问 401，见对接文档 §4）
DEFAULT_CONTEXT: dict[str, Any] = {
    "airport": "PVG",
    "terminal": "T2",
    "floor": "3F",
    "location_id": "T2_DEP_3F_NEAR_GATE_70",
}
TERMINAL_ID = "cross-eval-terminal-001"
CHANNEL = "robot_kiosk"

# 瞬态错误（可重试一次）：与 B 评测脚本 robot_call 的 transient 集合对齐
_TRANSIENT_STATUS = {403, 408, 425, 429, 500, 502, 503, 504}

# B 工具名 → A 工具名。覆盖 B fixtures expected_tool 与 B 源码
# (app/ services/) 中 used_tools/tool_chain 实际产出的全部工具名；
# 近似项已逐行注明（更完整说明见 evaluation/cross/MAPPING.md）。
TOOL_MAP_B_TO_A: dict[str, str] = {
    "pvg.flight.search": "pvg_flight_search",          # 航班列表搜索，两侧同义
    "pvg.flight.batch_search": "pvg_flight_search",    # 批量/多段搜索：A 以多次 flight_search 实现
    "pvg.flight.query": "pvg_flight_detail",           # 单航班实时状态/详情
    "pvg.flight.status": "pvg_flight_detail",          # 状态查询：A 侧由 detail 工具兜底
    "pvg.navigation.search_poi": "pvg_poi_search",     # POI/设施检索，两侧同义
    "pvg.facility.search": "pvg_poi_search",           # B 等价表中 facility≡search_poi
    "pvg.navigation.plan_route": "pvg_poi_search",     # 近似：A 无路线规划工具，最近词汇为 POI 检索
    "pvg.navigation.create_miniprogram_link": "pvg_poi_search",  # 近似：路线小程序卡片，归 POI/导航域
    "pvg.lost_found.search": "pvg_lost_and_found_search",        # 失物检索，两侧同义
    "pvg.lost_found.query_or_create": "pvg_lost_and_found_search",  # 近似：A 只查拾获、不含登记
    "pvg.airport.search": "pvg_airport_search",        # 机场字典检索，两侧同义
    "pvg.knowledge.search": "pvg_knowledge_search",    # 政策/FAQ 知识库
    "pvg.current_datetime": "pvg_current_datetime",    # 防御性条目：B 源码未见，防后续新增
}

# B 的 policy 标签（非真实工具，B 评测脚本 _POLICY_TOOL_LABELS 同样过滤），
# 以及 skill 标签；不计入 A 的工具比对
_B_POLICY_OR_SKILL_LABELS = frozenset({
    "pvg.baggage_or_handoff",
    "pvg.handoff",
    "pvg.special_assistance",
    "resource_qa",
    "context.memory",
    "pvg-intent-clarify",
})


def map_b_tools(names: list[str]) -> list[str]:
    """B 工具名列表 → A 工具名集合（policy/skill 标签丢弃，未知名保留原名）。"""

    mapped: set[str] = set()
    for name in names:
        if name in _B_POLICY_OR_SKILL_LABELS:
            continue
        mapped.add(TOOL_MAP_B_TO_A.get(name, name))
    return sorted(mapped)


def _extract_b_tool_names(body: dict[str, Any]) -> list[str]:
    """从 B 响应提取工具名：data.used_tools + presentation.intelligence.tool_chain。

    used_tools 元素为 {"name": ..., "status": ...}（B 源码
    agent_service._used_tools_from_result），tool_chain 元素为 {"tool": ...}；
    兼容字符串元素。

    另：B 的知识库检索发生在 skill 内部、不出现在 used_tools（实测：
    restricted_item_guidance 命中 battery-rules.md 时 used_tools 为空），
    因此当 data.references 非空或 skill 属知识域时，合成 pvg.knowledge.search
    标记，使 A 侧 expected_tools(pvg_knowledge_search) 的语义（"是否依据知识库"）
    对 B 可判定。
    """

    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    robot = data.get("robot") if isinstance(data.get("robot"), dict) else {}
    presentation = robot.get("presentation") if isinstance(robot.get("presentation"), dict) else {}
    intelligence = presentation.get("intelligence") if isinstance(presentation.get("intelligence"), dict) else {}
    names: list[str] = []
    items: list[Any] = []
    used_tools = data.get("used_tools")
    tool_chain = intelligence.get("tool_chain")
    if isinstance(used_tools, list):
        items.extend(used_tools)
    if isinstance(tool_chain, list):
        items.extend(tool_chain)
    for item in items:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            for key in ("tool", "tool_name", "name"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    names.append(value)
                    break

    # 知识活动信号（见 docstring）
    references = data.get("references")
    skill = str(data.get("skill") or "")
    if (isinstance(references, list) and references) or any(
        k in skill for k in ("restricted-item", "knowledge", "resource_qa", "policy")
    ):
        names.append("pvg.knowledge.search")
    return names


def _extract_b_reply(body: dict[str, Any]) -> str:
    """提取 B 的答复文本：reply 优先，回退 screen_text / speech_text。

    顺序依据（2026-07-21 实测修正）：B 的口播 speech_text 在部分 skill
    （如 restricted_item_guidance）下是错配的设施模板（"已为您找到相关设施"），
    而实质内容在顶层 reply / screen_text.summary（kiosk 屏显设计）。
    评分以实质内容为准；speech_text 仅作最后回退。
    """

    reply = body.get("reply")
    if isinstance(reply, str) and reply.strip():
        return reply
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    robot = data.get("robot") if isinstance(data.get("robot"), dict) else {}
    screen = robot.get("screen_text")
    if isinstance(screen, dict):
        parts = [
            str(screen.get(key) or "").strip()
            for key in ("title", "summary")
        ]
        text = "\n".join(part for part in parts if part)
        if text:
            return text
    if isinstance(screen, str) and screen.strip():
        return screen
    speech = robot.get("speech_text")
    if isinstance(speech, str) and speech.strip():
        return speech
    return ""


def _build_payloads(record: dict[str, Any]) -> list[dict[str, Any]]:
    """构造一条语料各轮将发送的请求体（--dry-run 与实际发送共用同一构造函数）。"""

    turns = record.get("turns")
    if not isinstance(turns, list) or not turns:
        turns = [record["query"]]
    session_id = f"cross-b-{record['id']}-{int(time.time())}"
    payloads: list[dict[str, Any]] = []
    for turn_index, text in enumerate(turns, start=1):
        payloads.append(
            {
                "request_id": f"cross-b-{record['id']}-{turn_index}-{int(time.time() * 1000)}",
                "session_id": session_id,  # 同一语料多轮共用（§5 会话隔离要求）
                "terminal_id": TERMINAL_ID,
                "input_text": str(text),
                "channel": CHANNEL,
                "context": dict(DEFAULT_CONTEXT),
            }
        )
    return payloads


async def _post_chat(
    client: httpx.AsyncClient,
    token: str,
    payload: dict[str, Any],
    timeout_s: float,
) -> tuple[int, float, dict[str, Any], str | None]:
    """发送一轮 robot/chat，瞬态错误重试一次（与 B 评测脚本 robot_call 对齐）。

    返回 (http_status, elapsed_s, body, transport_error)。
    """

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    last_error: str | None = None
    for attempt in range(2):
        started = time.monotonic()
        try:
            resp = await client.post(
                "/robot/chat",
                json=payload,
                headers=headers,
                timeout=timeout_s,
            )
            elapsed = time.monotonic() - started
            try:
                body = resp.json()
                if not isinstance(body, dict):
                    body = {"raw": body}
            except json.JSONDecodeError:
                body = {"raw": resp.text[:1000]}
            if resp.status_code in _TRANSIENT_STATUS and attempt == 0:
                await asyncio.sleep(0.8)
                continue
            return resp.status_code, elapsed, body, None
        except httpx.HTTPError as e:
            last_error = f"{type(e).__name__}: {e}"
            if attempt == 0:
                await asyncio.sleep(0.8)
                continue
            return 0, time.monotonic() - started, {"error": last_error}, last_error
    return 0, 0.0, {"error": last_error}, last_error


async def _run_one(
    client: httpx.AsyncClient,
    record: dict[str, Any],
    token: str,
    timeout_s: float,
) -> run_eval.EvalResult:
    """回放单条语料（支持多轮）并按 A 的评分口径评分。"""

    result = run_eval.EvalResult(
        id=record["id"], category=record["category"], query=record["query"]
    )
    started = time.monotonic()

    texts: list[str] = []
    tools_seen: set[str] = set()
    latency = 0.0
    for payload in _build_payloads(record):
        status, elapsed, body, transport_error = await _post_chat(
            client, token, payload, timeout_s
        )
        latency += elapsed
        if transport_error is not None:
            result.error = f"transport: {transport_error}"
            result.latency_s = time.monotonic() - started
            return result
        if status != 200:
            message = str(body.get("message") or body.get("raw") or "")[:240]
            result.error = f"HTTP {status}: {message}"
            if status == 401:
                result.error += "（检查 PARTNER_ROBOT_API_TOKEN）"
            result.latency_s = time.monotonic() - started
            return result
        if body.get("code") != 0:
            result.error = f"code={body.get('code')}: {str(body.get('message') or '')[:240]}"
            result.latency_s = time.monotonic() - started
            return result
        tools_seen.update(map_b_tools(_extract_b_tool_names(body)))
        reply = _extract_b_reply(body)
        if reply:
            texts.append(reply)

    result.latency_s = latency
    result.tools_observed = sorted(tools_seen)
    if not texts:
        result.error = "响应中无 speech_text/reply/screen_text 文本"
        return result
    ai_text = "\n".join(texts)
    result.response = ai_text

    # A 的评分口径（与 run_eval._run_one 的评分段一致）
    result.include_missed = [
        p
        for p in record.get("must_include", [])
        if not re.search(p, ai_text, flags=re.IGNORECASE)
    ]
    result.exclude_hit = [
        p
        for p in record.get("must_not_include", [])
        if re.search(p, ai_text, flags=re.IGNORECASE)
    ]
    result.tools_missed = [
        spec
        for spec in record.get("expected_tools", [])
        if not any(alt in tools_seen for alt in spec.split("|"))
    ]
    result.include_pass = not result.include_missed
    result.exclude_pass = not result.exclude_hit
    result.tools_pass = not result.tools_missed
    result.passed = result.include_pass and result.exclude_pass and result.tools_pass
    return result


async def run(args: argparse.Namespace) -> int:
    """主流程：加载语料 → 过滤 → dry-run 或并发回放 → 写报告。"""

    records = run_eval.load_corpus(Path(args.corpus))
    if args.category:
        records = [r for r in records if r["category"] == args.category]
    if args.limit:
        records = records[: args.limit]
    if not records:
        print("过滤后语料为空，请检查 --corpus/--category/--limit 参数", file=sys.stderr)
        return 1

    if args.dry_run:
        # 只打印将发送的请求（不依赖 token，用于链路自检）
        print(f"dry-run：{len(records)} 条语料将发送至 {args.base_url.rstrip('/')}/robot/chat\n")
        for record in records:
            for payload in _build_payloads(record):
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                print("  Authorization: Bearer $PARTNER_ROBOT_API_TOKEN\n")
        return 0

    token = os.environ.get("PARTNER_ROBOT_API_TOKEN", "").strip()
    if not token:
        print(
            "缺少环境变量 PARTNER_ROBOT_API_TOKEN（B 侧 Robot API Bearer Token，"
            "由项目方单独发放）。设置后重试；或用 --dry-run 仅打印请求。",
            file=sys.stderr,
        )
        return 2

    semaphore = asyncio.Semaphore(args.concurrency)
    limits = httpx.Limits(
        max_connections=max(4, args.concurrency * 2),
        max_keepalive_connections=max(4, args.concurrency * 2),
    )
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"), timeout=args.timeout + 15, limits=limits
    ) as client:

        async def guarded(record: dict[str, Any]) -> run_eval.EvalResult:
            async with semaphore:
                result = await _run_one(client, record, token, args.timeout)
                print(f"{result.id} {'PASS' if result.passed else 'FAIL'}", flush=True)
                return result

        print(
            f"开始交叉评测：{len(records)} 条语料（A 语料 → B 系统），"
            f"并发 {args.concurrency}，单轮超时 {args.timeout}s"
        )
        results = list(await asyncio.gather(*(guarded(r) for r in records)))

    meta = {
        "direction": "A-corpus -> B-system (pvg-agent-demo Robot API)",
        "base_url": args.base_url,
        "corpus": args.corpus,
        "category_filter": args.category,
        "limit": args.limit,
        "concurrency": args.concurrency,
        "timeout_s": args.timeout,
        "scoring": "A 口径（must_include/must_not_include/expected_tools，同 evaluation/run_eval.py）；"
        "B 响应经 TOOL_MAP_B_TO_A 映射回 A 词汇（见 evaluation/cross/MAPPING.md）",
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    report = run_eval.build_report(results, meta)

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = report_dir / f"cross-b-{stamp}.json"
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    report_path.write_text(payload, encoding="utf-8")
    # 固定文件名副本，便于脚本取最新报告
    (report_dir / "cross-b-latest.json").write_text(payload, encoding="utf-8")

    run_eval.print_summary(report)
    print(f"\n报告已写入：{report_path}")
    return 0


def main() -> int:
    """命令行入口。"""

    parser = argparse.ArgumentParser(description="交叉评测：A 的 66 条语料打 B（pvg-agent-demo Robot API），按 A 口径评分")
    parser.add_argument("--corpus", default="evaluation/corpus_v1.jsonl", help="A 侧评测语料 JSONL 路径")
    parser.add_argument("--base-url", default="https://guest.artfox.ltd/v2-api", help="B 侧 Robot API base URL")
    parser.add_argument("--concurrency", type=int, default=2, help="并发语料数，默认 2")
    parser.add_argument("--timeout", type=float, default=120.0, help="单轮请求超时秒数，默认 120")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条")
    parser.add_argument("--category", default=None, help="只跑指定 category（normal/long_tail/adversarial/multi_hop）")
    parser.add_argument("--report-dir", default="evaluation/reports", help="报告输出目录")
    parser.add_argument("--dry-run", action="store_true", help="只打印将发送的请求，不实际调用（无需 token）")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
