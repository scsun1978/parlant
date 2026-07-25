"""管理后台 BFF 配置：全部经环境变量注入，本地开发有安全缺省。"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("admin.config")

# ADMIN_USERS_JSON 未设置时的缺省账号（仅开发期兜底，生产必须显式配置）
DEFAULT_USERS: list[dict[str, Any]] = [
    {"username": "admin", "password": "admin123", "role": "admin"},
]


@dataclass(frozen=True)
class Settings:
    """BFF 运行配置。"""

    mongo_url: str | None
    mongo_db: str
    parlant_base_url: str
    rag_url: str
    prod_agent_id: str = "xyVHBNLLPg"
    bundles_dir: str = "../knowledge/bundles/v2-miniprogram-20260719b"
    users: list[dict[str, Any]] = field(default_factory=list)
    upstream_timeout: float = 5.0
    cors_origins: list[str] = field(default_factory=list)


def load_settings() -> Settings:
    """从环境变量加载配置。

    - ``MONGO_URL``：未设置或连接失败时存储层降级为进程内内存实现
    - ``PARLANT_BASE_URL``：Parlant REST 地址，默认 http://127.0.0.1:8800
    - ``RAG_URL``：RAG 检索服务地址，默认 http://127.0.0.1:8901
    - ``ADMIN_USERS_JSON``：JSON 数组 [{"username","password","role"}]，
      缺省使用内置 admin/admin123=admin 并打警告日志
    - ``ADMIN_CORS_ORIGINS``：逗号分隔的允许跨域来源（Appsmith/定制前端从
      浏览器直连 BFF 所需），默认 "*"（开发期），生产应显式收敛
    """
    users_json = os.environ.get("ADMIN_USERS_JSON")
    if users_json:
        users = json.loads(users_json)
    else:
        logger.warning("ADMIN_USERS_JSON 未设置，使用内置缺省账号 admin/admin123（仅限开发）")
        users = list(DEFAULT_USERS)

    return Settings(
        mongo_url=os.environ.get("MONGO_URL") or None,
        mongo_db=os.environ.get("MONGO_DB", "pvg_admin"),
        parlant_base_url=os.environ.get("PARLANT_BASE_URL", "http://127.0.0.1:8800"),
        rag_url=os.environ.get("RAG_URL", "http://127.0.0.1:8901"),
        prod_agent_id=os.environ.get("PROD_AGENT_ID", "xyVHBNLLPg"),
        bundles_dir=os.environ.get("BUNDLES_DIR", "../knowledge/bundles/v2-miniprogram-20260719b"),
        users=users,
        cors_origins=[o.strip() for o in os.environ.get("ADMIN_CORS_ORIGINS", "*").split(",") if o.strip()],
    )
