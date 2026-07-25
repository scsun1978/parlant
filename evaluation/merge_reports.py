"""合并多个分块评测报告为一份汇总。

用法：
  uv run python evaluation/merge_reports.py evaluation/reports/eval-report-A.json [B.json ...]
不带参数时自动选取 evaluation/reports/ 下最新的 4 份报告。
输出：控制台汇总 + evaluation/reports/eval-report-merged-<时间戳>.json
"""

from __future__ import annotations

import glob
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


def _details(rep: dict[str, Any]) -> list[dict[str, Any]]:
    return rep.get("details") or rep.get("results") or []


def _pctl(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    k = min(len(values) - 1, max(0, round(pct / 100 * (len(values) - 1))))
    return values[k]


def main() -> None:
    if len(sys.argv) > 1:
        paths = sys.argv[1:]
    else:
        paths = sorted(glob.glob("evaluation/reports/eval-report-*.json"))[-4:]
    print(f"合并 {len(paths)} 份报告：")
    for p in paths:
        print(f"  - {p}")

    details: list[dict[str, Any]] = []
    for p in paths:
        details.extend(_details(json.load(open(p, encoding="utf-8"))))

    by_cat: dict[str, list[bool]] = {}
    latencies: list[float] = []
    failures: list[dict[str, Any]] = []
    for d in details:
        passed = bool(d.get("passed"))
        by_cat.setdefault(d.get("category", "?"), []).append(passed)
        if d.get("latency_s") is not None:
            latencies.append(float(d["latency_s"]))
        if not passed:
            failures.append(d)

    total = len(details)
    passed_n = sum(1 for d in details if d.get("passed"))
    print("\n========== 合并摘要 ==========")
    print(f"{'category':<14}{'总数':>4}{'通过':>6}     通过率")
    for cat, flags in sorted(by_cat.items()):
        print(f"{cat:<14}{len(flags):>4}{sum(flags):>6}   {sum(flags)/len(flags)*100:5.1f}%")
    print(f"{'TOTAL':<14}{total:>4}{passed_n:>6}   {passed_n/total*100:5.1f}%")
    print(f"\n延迟（wall-clock）：p50={_pctl(latencies, 50):.2f}s  p95={_pctl(latencies, 95):.2f}s")
    print(f"\n失败样例（{len(failures)} 条）：")
    for d in failures[:40]:
        reasons = []
        if d.get("include_missed"):
            reasons.append(f"must_include 未命中: {d['include_missed']}")
        if d.get("exclude_hit"):
            reasons.append(f"must_not_include 命中: {d['exclude_hit']}")
        if d.get("tools_missed"):
            reasons.append(f"expected_tools 未调用: {d['tools_missed']}")
        if d.get("error"):
            reasons.append(str(d["error"]))
        print(f"  {d.get('id')}  {'; '.join(reasons)}")

    out = Path(f"evaluation/reports/eval-report-merged-{datetime.now():%Y%m%d-%H%M%S}.json")
    out.write_text(json.dumps({
        "sources": paths,
        "total": total,
        "passed": passed_n,
        "pass_rate": passed_n / total,
        "by_category": {c: {"total": len(f), "passed": sum(f)} for c, f in by_cat.items()},
        "latency_p50": _pctl(latencies, 50),
        "latency_p95": _pctl(latencies, 95),
        "details": details,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n合并报告已写入：{out}")


if __name__ == "__main__":
    main()
