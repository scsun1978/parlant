#!/usr/bin/env bash
# 启动 Parlant server（0.0.0.0:8800，home=parlant-data，DeepSeek 模型，本地 Jina 嵌入）。
# DEEPSEEK_API_KEY 从 scripts/pvg_proxy.env（gitignored，权限 0600）或当前环境读取；
# 日志写入 logs/parlant-server.log。
#
# 注意：Parlant 默认向量库为内存瞬态实现（TransientVectorDatabase），
# glossary 术语与 canned response 的 embedding 索引在重启后丢失
#（embedding 有 cache_embeddings.json 缓存，重建很快）。
# 使用 --with-seed 可在服务就绪后自动重跑种子脚本补回数据；
# 种子脚本带 --recreate-canned，会删除并重建话术模板以刷新向量索引，
# 否则 composited_canned 选择失败、回复退化为兜底话术。
set -euo pipefail

cd "$(dirname "$0")/.."

ENV_FILE="scripts/pvg_proxy.env"
if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
  if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
  fi
fi
if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
  echo "缺少 DEEPSEEK_API_KEY（写入 $ENV_FILE 或导出到环境）" >&2
  exit 1
fi

mkdir -p logs

if [[ "${1:-}" == "--with-seed" ]]; then
  uv run --extra deepseek --extra chroma python -m parlant.bin.server run --deepseek >> logs/parlant-server.log 2>&1 &
  SERVER_PID=$!
  trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT
  echo "等待 server 就绪（pid ${SERVER_PID}）..."
  for _ in $(seq 1 60); do
    if curl -sf http://127.0.0.1:8800/agents > /dev/null 2>&1; then
      break
    fi
    sleep 2
  done
  uv run python scripts/seed_pvg_poc_data.py --recreate-canned
  wait "$SERVER_PID"
else
  echo "提示：glossary 为内存瞬态存储，重启后需执行 scripts/seed_pvg_poc_data.py 补回（或改用 $0 --with-seed）" >&2
  exec uv run --extra deepseek --extra chroma python -m parlant.bin.server run --deepseek >> logs/parlant-server.log 2>&1
fi
