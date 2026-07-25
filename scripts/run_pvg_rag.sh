#!/usr/bin/env bash
# 本地启动 PVG RAG 检索服务（127.0.0.1:8901，供 pvg_mcp_proxy 转发调用）。
# 日志写入 logs/pvg-rag.log。
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p logs
exec uv run --extra rag python scripts/pvg_rag_service.py >> logs/pvg-rag.log 2>&1
