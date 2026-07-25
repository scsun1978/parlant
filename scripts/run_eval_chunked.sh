#!/usr/bin/env bash
# 分块压测执行器：逐块运行评测，块前做健康检查，服务挂掉则自动拉起后继续。
# 用法：bash scripts/run_eval_chunked.sh [concurrency]
# 产出：evaluation/reports/eval-report-*.json（每块一份）+ logs/eval-chunked.log
set -uo pipefail

cd "$(dirname "$0")/.."
CONCURRENCY="${1:-4}"
LOG="logs/eval-chunked.log"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

health() { curl -sf -m 5 http://127.0.0.1:8800/agents > /dev/null 2>&1; }

restart_stack() {
  log "服务不可用，重启 server（--with-seed）..."
  bash scripts/run_parlant_server.sh --with-seed >> "$LOG" 2>&1 &
  for _ in $(seq 1 60); do
    health && { log "server 恢复"; return 0; }
    sleep 3
  done
  log "ERROR: server 重启超时"
  return 1
}

# 确保代理在线（不重启已在线的代理——重启会使 server 的 MCP session 失效）
# 注意：/mcp 对非法请求也返回 4xx，不能用 -f 判断存活；能建立连接即视为在线
if ! curl -s -o /dev/null -m 5 http://127.0.0.1:8900/mcp; then
  log "代理不在线，启动..."
  bash scripts/run_pvg_proxy.sh &
  sleep 8
fi
health || restart_stack || exit 1

for chunk in evaluation/chunks/chunk-*.jsonl; do
  health || restart_stack || exit 1
  log "开始 ${chunk}（并发 ${CONCURRENCY}）"
  .venv/bin/python evaluation/run_eval.py --corpus "$chunk" \
    --concurrency "$CONCURRENCY" --timeout 240 >> "$LOG" 2>&1
  rc=$?
  log "完成 ${chunk}（exit ${rc}）"
  sleep 5
done
log "全部分块完成"
