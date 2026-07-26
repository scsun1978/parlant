"""渠道事件转译：Parlant 事件流 → 小程序友好的消息模型（方案 §3.1）。

- tool 事件 → tool_start（"正在为您查询…"状态条）
- ai_agent message → message_append（preamble 启发式标注 display_as=placeholder：
  data.preamble / data.metadata.preamble 为真，或本轮首条 AI 消息）
- ready 且 stage=completed → message_done
- 引擎 error → fallback（渠道兜底话术，ADR 口径：不落引擎）
- customer/human_agent/其余 status → 不在事件流推送（history 另按角色映射）
"""

from __future__ import annotations

from typing import Any

# 渠道兜底话术（W1 常量；文案治理走管理后台审批流——方案 §9 已决，W2 接生效版本）
FALLBACK_TEXT = "抱歉，系统繁忙，请稍后再试；如需帮助请拨打 021-96990。"


def _flagged_preamble(event: dict[str, Any]) -> bool:
    data = event.get("data") or {}
    return data.get("preamble") is True or bool((data.get("metadata") or {}).get("preamble"))


def translate_events(
    events: list[dict[str, Any]], fallback_text: str = FALLBACK_TEXT
) -> list[dict[str, Any]]:
    """批量转译（WS 与 poll 共用）；每条带 offset 供增量拉取。"""
    out: list[dict[str, Any]] = []
    ai_seen_in_turn = 0
    for ev in events:
        kind = ev.get("kind")
        source = ev.get("source")
        offset = ev.get("offset")
        if kind == "message" and source == "customer":
            ai_seen_in_turn = 0  # 新一轮：首条 AI 消息视为 preamble
            continue
        if kind == "tool":
            out.append({"type": "tool_start", "text": "正在为您查询…", "offset": offset})
        elif kind == "message" and source == "ai_agent":
            placeholder = _flagged_preamble(ev) or ai_seen_in_turn == 0
            ai_seen_in_turn += 1
            out.append(
                {
                    "type": "message_append",
                    "text": (ev.get("data") or {}).get("message") or "",
                    "display_as": "placeholder" if placeholder else "content",
                    "offset": offset,
                }
            )
        elif kind == "status":
            data = ev.get("data") or {}
            inner = data.get("data") or {}
            if data.get("status") == "ready" and inner.get("stage") == "completed":
                out.append({"type": "message_done", "offset": offset})
            elif data.get("status") == "error":
                out.append({"type": "fallback", "text": fallback_text, "offset": offset})
    return out


def to_history(
    events: list[dict[str, Any]], limit: int, fallback_text: str = FALLBACK_TEXT
) -> list[dict[str, Any]]:
    """用户友好历史形态：[{role: user|assistant|system, text, ts, display_as}]。

    tool/status 事件不直接暴露（引擎 error 折成 system 行）。
    """
    items: list[dict[str, Any]] = []
    ai_seen_in_turn = 0
    for ev in events:
        kind = ev.get("kind")
        source = ev.get("source")
        ts = ev.get("creation_utc")
        if kind == "message" and source == "customer":
            ai_seen_in_turn = 0
            items.append(
                {"role": "user", "text": (ev.get("data") or {}).get("message") or "", "ts": ts, "display_as": "content"}
            )
        elif kind == "message" and source == "ai_agent":
            placeholder = _flagged_preamble(ev) or ai_seen_in_turn == 0
            ai_seen_in_turn += 1
            items.append(
                {
                    "role": "assistant",
                    "text": (ev.get("data") or {}).get("message") or "",
                    "ts": ts,
                    "display_as": "placeholder" if placeholder else "content",
                }
            )
        elif kind == "message" and source == "human_agent":
            items.append(
                {
                    "role": "assistant",
                    "text": (ev.get("data") or {}).get("message") or "",
                    "ts": ts,
                    "display_as": "human",  # 人工坐席代聊
                }
            )
        elif kind == "status" and (ev.get("data") or {}).get("status") == "error":
            items.append({"role": "system", "text": fallback_text, "ts": ts, "display_as": "fallback"})
    return items[-limit:]
