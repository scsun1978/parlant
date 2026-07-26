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
    # --- W2：微信模式与令牌/兜底配置 ---
    wechat_app_id: str = ""  # 与 secret 都配置才启用微信模式，否则匿名模式
    wechat_app_secret: str = ""
    wechat_api_base_url: str = "https://api.weixin.qq.com"
    wechat_grant_type: str = "authorization_code"
    wechat_timeout_s: float = 10.0
    token_ttl_s: int = 7200  # 渠道 token 有效期（过期 401，login 重发）
    fallback_text: str = "抱歉，系统繁忙，请稍后再试；如需帮助请拨打 021-96990。"
    moderation_block_text: str = (
        "抱歉，您的提问方式我无法处理，请换个问题咨询机场出行相关服务。"
    )  # 审核拦截话术（CHANNEL_MODERATION_BLOCK_TEXT）

    @property
    def wechat_enabled(self) -> bool:
        """双模判定：appid/secret 都配置才启用微信模式。"""
        return bool(self.wechat_app_id and self.wechat_app_secret)


def load_settings() -> Settings:
    """从环境变量加载配置。

    - ``PARLANT_BASE_URL``：本地默认 http://127.0.0.1:8800（compose 注入 http://parlant-server:8800）
    - ``MONGO_URL``：未配置或连接失败时存储降级为进程内内存（复用 admin store 抽象）
    - ``CHANNEL_PREAMBLE_TIMEOUT_S`` / ``CHANNEL_DONE_TIMEOUT_S``：超时预算（默认 8 / 45）
    - ``WECHAT_APP_ID`` / ``WECHAT_APP_SECRET``：都配置才启用微信模式（否则匿名模式）
    - ``WECHAT_API_BASE_URL`` / ``WECHAT_GRANT_TYPE`` / ``WECHAT_TIMEOUT_S``：微信 API 参数
    - ``CHANNEL_TOKEN_TTL_S``：渠道 token 有效期（默认 7200，过期 401 重登）
    - ``CHANNEL_FALLBACK_TEXT``：兜底话术（W2 配置化；接管理后台审批版本为后续项）
    """
    return Settings(
        mongo_url=os.environ.get("MONGO_URL") or None,
        mongo_db=os.environ.get("MONGO_DB", "pvg_channel"),
        parlant_base_url=os.environ.get("PARLANT_BASE_URL", "http://127.0.0.1:8800"),
        agent_id=os.environ.get("CHANNEL_AGENT_ID", "xyVHBNLLPg"),
        preamble_timeout_s=float(os.environ.get("CHANNEL_PREAMBLE_TIMEOUT_S", "8")),
        done_timeout_s=float(os.environ.get("CHANNEL_DONE_TIMEOUT_S", "45")),
        wechat_app_id=os.environ.get("WECHAT_APP_ID", ""),
        wechat_app_secret=os.environ.get("WECHAT_APP_SECRET", ""),
        wechat_api_base_url=os.environ.get("WECHAT_API_BASE_URL", "https://api.weixin.qq.com"),
        wechat_grant_type=os.environ.get("WECHAT_GRANT_TYPE", "authorization_code"),
        wechat_timeout_s=float(os.environ.get("WECHAT_TIMEOUT_S", "10")),
        token_ttl_s=int(os.environ.get("CHANNEL_TOKEN_TTL_S", "7200")),
        fallback_text=os.environ.get(
            "CHANNEL_FALLBACK_TEXT", "抱歉，系统繁忙，请稍后再试；如需帮助请拨打 021-96990。"
        ),
        moderation_block_text=os.environ.get(
            "CHANNEL_MODERATION_BLOCK_TEXT",
            "抱歉，您的提问方式我无法处理，请换个问题咨询机场出行相关服务。",
        ),
    )
