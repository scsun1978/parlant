"""内容审核前置层（ADR-0005）：频控 → 注入模式库 → 不当内容词表。

命中即拦截（不转发引擎），安全话术返回并写审计（moderation_events，不存原文）。
规则存 `moderation_rules` 集合（可治理：新增/停用/hit_count 递增），启动时种子默认规则。
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from admin.app.store import Store

RULES = "moderation_rules"
EVENTS = "moderation_events"
RATE = "moderation_rate"

# 默认规则种子（category: injection / content）
SEED_RULES: list[dict[str, Any]] = [
    # —— 注入：角色覆盖 ——
    {"category": "injection", "pattern": r"从现在开始你(是|不是)|你不再是.{0,8}(客服|助手)|ignore (all )?previous instructions|you are now", "note": "角色覆盖"},
    # —— 注入：指令忽略 ——
    {"category": "injection", "pattern": r"忽略(之前|以上|先前的?)?(的)?(所有|全部)?(指令|规则|提示)", "note": "指令忽略"},
    # —— 注入：系统提示窃取 ——
    {"category": "injection", "pattern": r"重复你的(系统)?提示|把你的 ?prompt 发我|show me your (system )?prompt|你的(系统|原始)提示词是什么", "note": "系统提示窃取"},
    # —— 注入：越权诱导 ——
    {"category": "injection", "pattern": r"以(管理员|root|开发者|开发人员)身份|我是(机场|系统)管理员|绕过(安全|审核)限制", "note": "越权诱导"},
    # —— 注入：红线诱导（客服场景特有：诱导承诺赔偿/免费）——
    {"category": "injection", "pattern": r"你就说(一定|肯定)赔|直接告诉我赔(多少钱|\d+)|承诺.{0,6}(免费|赔偿)", "note": "红线诱导"},
    # —— 不当内容词表（占位，运营扩充，见 README）——
    {"category": "content", "pattern": r"赌博|赌场|博彩", "note": "违法类"},
    {"category": "content", "pattern": r"毒品|冰毒|大麻", "note": "违法类"},
    {"category": "content", "pattern": r"枪支|弹药|炸药", "note": "违法类"},
]

# 频控：每用户每分钟 20 条（滑动窗口；多副本需 Redis，见 README 遗留）
RATE_LIMIT_PER_MINUTE = 20
_rate_windows: dict[str, list[float]] = {}


@dataclass
class ModerationResult:
    blocked: bool
    category: str | None = None
    rule_id: str | None = None
    note: str | None = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def seed_rules(store: Store) -> None:
    """启动时种子默认规则（按 pattern 去重，不重复插）。"""
    existing, _ = store.find(RULES, {}, limit=500)
    existing_patterns = {r["pattern"] for r in existing}
    for r in SEED_RULES:
        if r["pattern"] not in existing_patterns:
            store.insert(
                RULES,
                {
                    "category": r["category"],
                    "pattern": r["pattern"],
                    "action": "block",
                    "enabled": True,
                    "note": r["note"],
                    "hit_count": 0,
                    "created_by": "seed",
                    "created_at": _utc_now_iso(),
                },
            )


def _check_rate(user_id: str) -> bool:
    """滑动窗口频控；返回 True=超限。"""
    now = time.monotonic()
    window = [t for t in _rate_windows.get(user_id, []) if now - t < 60]
    window.append(now)
    _rate_windows[user_id] = window
    return len(window) > RATE_LIMIT_PER_MINUTE


def check_message(store: Store, user_id: str, session_id: str, text: str) -> ModerationResult:
    """审核管线：频控 → 启用规则逐条正则匹配。命中则审计并返回拦截。"""
    if _check_rate(user_id):
        _record_event(store, user_id, session_id, None, "rate", text)
        return ModerationResult(blocked=True, category="rate", note="频控超限")

    rules, _ = store.find(RULES, {"enabled": True}, limit=500)
    for rule in rules:
        try:
            if re.search(rule["pattern"], text, flags=re.IGNORECASE):
                store.update(RULES, rule["id"], {"hit_count": int(rule.get("hit_count", 0)) + 1})
                _record_event(store, user_id, session_id, rule["id"], rule["category"], text)
                return ModerationResult(
                    blocked=True,
                    category=rule["category"],
                    rule_id=rule["id"],
                    note=rule.get("note"),
                )
        except re.error:
            continue
    return ModerationResult(blocked=False)


def _record_event(store: Store, user_id: str, session_id: str, rule_id: str | None, category: str, text: str) -> None:
    store.insert(
        EVENTS,
        {
            "ts": _utc_now_iso(),
            "user_id": user_id,
            "session_id": session_id,
            "rule_id": rule_id,
            "category": category,
            "text_hash": hashlib.sha256(text.encode()).hexdigest()[:16],  # 不留原文
            "action": "blocked",
        },
    )
