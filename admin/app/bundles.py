"""知识 bundle 文件生成器：bad case"知识缺失"补录草稿审批后落地为 bundle .md。

格式与 knowledge/bundles/v2-miniprogram-20260719b/ 现有 890 篇一致：
外层 front-matter（发布元数据）+ 内层 front-matter（来源元数据）+ markdown 正文
（## 分类 / ## 标准答案 / ## 意图标签 / ## 风险与来源 四节）。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

CST = timezone(timedelta(hours=8), "Asia/Shanghai")
VALID_MONTHS_DAYS = 90  # 默认有效期 3 个月（按 90 天计）


def new_asset_id() -> tuple[str, str]:
    """生成 asset_id 与文件 stem：pvg.v2.kb<时间戳 hex>（kb 前缀标识知识补录来源）。"""
    stem = f"kb{time.time_ns():x}"
    return f"pvg.v2.{stem}", stem


def _one_line(text: str) -> str:
    """front-matter 字段值压成单行（防注入换行破坏 YAML 结构）。"""
    return " ".join((text or "").split())


def render_bundle(
    *,
    asset_id: str,
    question: str,
    suggested_answer: str,
    reviewer: str,
    source_id: str,
    now: datetime | None = None,
) -> str:
    """渲染 bundle 文件内容（双 front-matter + 四节正文）。"""
    now = now or datetime.now(CST)
    now = now.astimezone(CST)
    published = now.isoformat()
    valid_from = now.isoformat()
    valid_until = (now + timedelta(days=VALID_MONTHS_DAYS)).isoformat()
    day = now.strftime("%Y-%m-%d")
    title = _one_line(question)
    return f"""---
asset_id: {asset_id}
version: v2-{asset_id.removeprefix('pvg.v2.')}
tenant_id: pvg
title: {title}
section: manual_supplement
reviewer: {reviewer}
publisher: admin-bff
published_at: '{published}'
source_uri: admin://badcase/{source_id}
status: published
knowledge_type: faq
valid_from: '{valid_from}'
valid_until: '{valid_until}'
channel_scope:
- miniprogram
terminal_scope:
- T1
- T2
- S1
- S2
revoked_at: ''
---
---
title: {title}
source_owner: PVG passenger service
source_type: manual_supplement
source_id: {source_id}
version: 1.0
updated_at: {day}
effective_from: {day}
scope: demo_only
confidentiality: internal
review_status: approved_for_demo
---

# {title}

## 分类

- 一级分类：运营补录
- 二级分类：bad case 修复

## 标准答案

{suggested_answer.strip()}

## 意图标签

- 主意图：manual_supplement

## 风险与来源

- 来源：bad case 闭环补录（admin-bff），审批人 {reviewer}，source_id {source_id}
"""
