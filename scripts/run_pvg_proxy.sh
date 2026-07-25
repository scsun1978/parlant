#!/usr/bin/env bash
# 启动 PVG MCP 代理（127.0.0.1:8900/mcp）。
# 凭据从 scripts/pvg_proxy.env 读取（gitignored，权限 0600）；日志写入 logs/pvg-proxy.log。
#
# 注意：Parlant server 会缓存与本代理的 MCP session，本代理重启后
# server 侧工具调用会报 "Session terminated"，必须随后重启 server
#（建议 scripts/run_parlant_server.sh --with-seed）。
set -euo pipefail

cd "$(dirname "$0")/.."

ENV_FILE="scripts/pvg_proxy.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "缺少 ${ENV_FILE}（参照 docs/real-mcp-call-guide 凭据创建，不得提交 Git）" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

mkdir -p logs
exec uv run --extra rag python scripts/pvg_mcp_proxy.py >> logs/pvg-proxy.log 2>&1
