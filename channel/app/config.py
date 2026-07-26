"""渠道 BFF 配置：全部经环境变量注入，本地开发有安全缺省。"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """渠道 BFF 运行配置。"""

    mongo_url: str | None
    mongo_db: str
    parlant_base_url: str
    agent_id: str = "xyVHBNLLPg"
    preamble_timeout_s: float = 8.0  # 同步等 preamble 上限（对齐引擎实测 4.5s）
    done_timeout_s: float = 45.0  # ready/completed 上限（答复 15–40s 预算）
    resume_window_hours: int = 24  # 匿名续聊窗口
    idempotency_window_s: int = 300  # client_msg_id 幂等去重窗口（5 分钟）
    upstream_timeout: float = 5.0


def load_settings() -> Settings:
    """从环境变量加载配置。

    - ``PARLANT_BASE_URL``：本地默认 http://127.0.0.1:8800（compose 注入 http://parlant-server:8800）
    - ``MONGO_URL``：未配置或连接失败时存储降级为进程内内存（复用 admin store 抽象）
    - ``CHANNEL_PREAMBLE_TIMEOUT_S`` / ``CHANNEL_DONE_TIMEOUT_S``：超时预算（默认 8 / 45）
    """
    return Settings(
        mongo_url=os.environ.get("MONGO_URL") or None,
        mongo_db=os.environ.get("MONGO_DB", "pvg_channel"),
        parlant_base_url=os.environ.get("PARLANT_BASE_URL", "http://127.0.0.1:8800"),
        agent_id=os.environ.get("CHANNEL_AGENT_ID", "xyVHBNLLPg"),
        preamble_timeout_s=float(os.environ.get("CHANNEL_PREAMBLE_TIMEOUT_S", "8")),
        done_timeout_s=float(os.environ.get("CHANNEL_DONE_TIMEOUT_S", "45")),
    )
