"""PVG 智能客服评测语料扩增脚本。

读取 evaluation/corpus_v1.jsonl 手工种子语料，用确定性规则（random.seed(42)）
扰动扩增至目标条数，输出 evaluation/corpus_full.jsonl。

扰动规则（除拼接外均只改动 query、不触碰标注，保证 must_include /
must_not_include 语义仍然成立，不会生成自相矛盾的标注）：

  1. 同义改写模板：query 内的保守同义替换（"怎么走"→"怎么去" 等）
  2. 口语化前缀/后缀：句首加"请问一下，"、句尾加"，谢谢" 等
  3. 常见错字映射注入："值机"→"直机"、"行李"→"行理" 等同音错字
  4. 中英术语替换：值机→check-in、登机牌→boarding pass 等
  5. 组合扰动：先做同义改写再叠加口语化前缀/后缀
  6. 多意图两两拼接：仅拼接 normal 类、且双方 must_not_include 均为空的
     条目，must_include / expected_tools 取并集——任一标注为空的并集仍为空，
     因此不会引入红线断言冲突

每条扩增记录带 "augmented_from"（溯源种子 id）与 "mutation"（扰动方式）字段。

用法：
  uv run python evaluation/generate_corpus.py --target 520
  uv run python evaluation/generate_corpus.py --input evaluation/corpus_v1.jsonl \
      --output evaluation/corpus_full.jsonl --target 500
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 扰动规则表
# ---------------------------------------------------------------------------

# 同义改写模板（query 内替换，保持意图不变；只替换首个命中位置）
SYNONYM_RULES: list[tuple[str, str]] = [
    ("怎么走", "怎么去"),
    ("怎么去", "怎么走"),
    ("在哪里", "在哪"),
    ("在哪", "在哪里"),
    ("多少钱", "怎么收费"),
    ("几点", "什么时间"),
    ("能带吗", "可以带吗"),
    ("可以带吗", "能带吗"),
    ("能不能带", "可以带吗"),
    ("怎么办理", "如何办理"),
    ("怎么办", "怎么处理"),
    ("帮我查一下", "麻烦查一下"),
    ("帮我查查", "麻烦查一下"),
    ("告诉我", "跟我说"),
]

# 口语化前缀 / 后缀
PREFIXES: list[str] = ["请问一下，", "你好，", "麻烦问下，", "你好我想问问，", "请问，", "喂，"]
SUFFIXES: list[str] = ["，谢谢", "，麻烦了", "，谢谢啦", "啊", "呢", "，请告知"]

# 常见错字映射（同音/近音错字，仅在 query 中出现原词时注入）
TYPO_MAP: dict[str, str] = {
    "值机": "直机",
    "行李": "行理",
    "托运": "拖运",
    "安检": "按检",
    "航站楼": "航战楼",
    "充电宝": "冲电宝",
    "登机牌": "登记牌",
    "赔偿": "陪偿",
}

# 中英术语替换
EN_TERM_MAP: dict[str, str] = {
    "值机": "check-in",
    "登机牌": "boarding pass",
    "安检": "security check",
    "航班": "flight",
}

MUTATION_KINDS: list[str] = ["synonym", "prefix_suffix", "typo", "en_term", "synonym+prefix", "concat"]


# ---------------------------------------------------------------------------
# 读写与扰动实现
# ---------------------------------------------------------------------------


def load_corpus(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL 语料文件，返回记录列表。"""

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


def _apply_synonym(query: str, rng: random.Random) -> str | None:
    """同义改写：随机选一条命中规则替换首个出现位置；无命中返回 None。"""

    candidates = [(old, new) for old, new in SYNONYM_RULES if old in query]
    if not candidates:
        return None
    old, new = rng.choice(candidates)
    return query.replace(old, new, 1)


def _apply_prefix_suffix(query: str, rng: random.Random) -> str | None:
    """口语化前缀/后缀：至少加一个，避免生成与原句相同的文本。"""

    prefix = rng.choice(PREFIXES + [""])
    suffix = rng.choice(SUFFIXES + [""])
    if not prefix and not suffix:
        return None
    return f"{prefix}{query}{suffix}"


def _apply_typo(query: str, rng: random.Random) -> str | None:
    """错字注入：随机选一个 query 中出现的词替换为常见错字。"""

    candidates = [(src, dst) for src, dst in TYPO_MAP.items() if src in query]
    if not candidates:
        return None
    src, dst = rng.choice(candidates)
    return query.replace(src, dst, 1)


def _apply_en_term(query: str, rng: random.Random) -> str | None:
    """中英术语替换：值机→check-in 等。"""

    candidates = [(src, dst) for src, dst in EN_TERM_MAP.items() if src in query]
    if not candidates:
        return None
    src, dst = rng.choice(candidates)
    return query.replace(src, dst, 1)


def _apply_combo(query: str, rng: random.Random) -> str | None:
    """组合扰动：先同义改写再叠加口语化前缀/后缀。"""

    stepped = _apply_synonym(query, rng)
    if stepped is None:
        return None
    return _apply_prefix_suffix(stepped, rng) or stepped


def _dedupe_preserving_order(items: list[str]) -> list[str]:
    """列表去重且保持原有顺序。"""

    return list(dict.fromkeys(items))


def _apply_concat(
    record: dict[str, Any],
    eligible: list[dict[str, Any]],
    rng: random.Random,
) -> dict[str, Any] | None:
    """多意图两两拼接（仅 normal 类且双方 must_not_include 均为空）。

    返回新的 (query, must_include, must_not_include, expected_tools, note)；
    抽到的搭档与自身相同则返回 None。
    """

    partner = rng.choice(eligible)
    if partner["id"] == record["id"]:
        return None
    query = f"{record['query']}，另外{partner['query']}"
    return {
        "query": query,
        "must_include": _dedupe_preserving_order(record["must_include"] + partner["must_include"]),
        "must_not_include": _dedupe_preserving_order(
            record["must_not_include"] + partner["must_not_include"]
        ),
        "expected_tools": _dedupe_preserving_order(
            record["expected_tools"] + partner["expected_tools"]
        ),
        "note": f"{record.get('note', '')} | 多意图拼接：{record['id']}+{partner['id']}".strip(" |"),
    }


def make_variant(
    record: dict[str, Any],
    kind: str,
    rng: random.Random,
    concat_eligible: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """对单条种子施加一种扰动，返回新记录（仅 query 等少数字段变化）。"""

    if kind == "concat":
        merged = _apply_concat(record, concat_eligible, rng)
        if merged is None:
            return None
        variant = dict(record)
        variant.update(merged)
        return variant

    appliers = {
        "synonym": _apply_synonym,
        "prefix_suffix": _apply_prefix_suffix,
        "typo": _apply_typo,
        "en_term": _apply_en_term,
        "synonym+prefix": _apply_combo,
    }
    new_query = appliers[kind](record["query"], rng)
    if new_query is None or new_query == record["query"]:
        return None
    variant = dict(record)
    variant["query"] = new_query
    return variant


def augment(
    seeds: list[dict[str, Any]],
    target: int,
    rng: random.Random,
) -> list[dict[str, Any]]:
    """以确定性规则把种子语料扩增至 target 条。

    种子原样保留在前（augmented_from 为 None），随后按种子轮转、扰动方式
    轮转的顺序生成变体；以 query 文本去重，避免重复样本。
    """

    results: list[dict[str, Any]] = []
    seen_queries: set[str] = set()
    for seed in seeds:
        row = dict(seed)
        row["augmented_from"] = None
        row["mutation"] = "seed"
        results.append(row)
        seen_queries.add(seed["query"])

    # 多意图拼接的候选池：normal 类且 must_not_include 为空（无红线断言），
    # 保证拼接后的并集标注不会自相矛盾
    concat_eligible = [
        r for r in seeds if r["category"] == "normal" and not r["must_not_include"]
    ]

    order = list(range(len(seeds)))
    rng.shuffle(order)

    max_rounds = 200  # 安全上限，防止扰动空间耗尽后死循环
    for _ in range(max_rounds):
        if len(results) >= target:
            break
        for idx in order:
            if len(results) >= target:
                break
            seed = seeds[idx]
            kinds = list(MUTATION_KINDS)
            rng.shuffle(kinds)
            for kind in kinds:
                # 拼接扰动的母本同样限 normal 类且无红线断言（与搭档池同口径），
                # 保证合并后的标注必为两条 normal 标注的并集、不含矛盾断言
                if kind == "concat" and seed not in concat_eligible:
                    continue
                variant = make_variant(seed, kind, rng, concat_eligible)
                if variant is None or variant["query"] in seen_queries:
                    continue
                variant["id"] = f"{seed['id']}-aug{sum(1 for r in results if r['augmented_from'] == seed['id']) + 1:03d}"
                variant["augmented_from"] = seed["id"]
                variant["mutation"] = kind
                results.append(variant)
                seen_queries.add(variant["query"])
                break  # 每轮每种子的扰动方式各试一次，成功后轮到下一条种子

    if len(results) < target:
        raise RuntimeError(
            f"扰动空间耗尽：只生成到 {len(results)} 条，未达到目标 {target} 条"
        )
    return results


def main() -> int:
    """命令行入口：读种子语料 → 确定性扩增 → 写 JSONL。"""

    parser = argparse.ArgumentParser(description="PVG 评测语料确定性扩增（seed=42）")
    parser.add_argument("--input", default="evaluation/corpus_v1.jsonl", help="种子语料 JSONL 路径")
    parser.add_argument("--output", default="evaluation/corpus_full.jsonl", help="输出 JSONL 路径")
    parser.add_argument("--target", type=int, default=500, help="目标总条数（含种子），默认 500")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    seeds = load_corpus(input_path)
    if args.target < len(seeds):
        print(f"目标条数 {args.target} 小于种子条数 {len(seeds)}，无需扩增", file=sys.stderr)
        return 1

    rng = random.Random(42)
    rows = augment(seeds, args.target, rng)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # 控制台摘要：总数与分类分布
    by_category: dict[str, int] = {}
    for row in rows:
        by_category[row["category"]] = by_category.get(row["category"], 0) + 1
    dist = ", ".join(f"{k}={v}" for k, v in sorted(by_category.items()))
    print(f"已生成 {len(rows)} 条语料（种子 {len(seeds)} 条）→ {output_path}")
    print(f"分类分布：{dist}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
