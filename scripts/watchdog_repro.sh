#!/usr/bin/env bash
# 静默死亡复现：watchdog 采样 + 分块压测（迭代 1-3，时间盒 1 天）
# 用法：bash scripts/watchdog_repro.sh [concurrency]
# 采样：server/proxy/rag 三进程 RSS/存活，5 秒间隔 → logs/watchdog-<ts>.log
# 压测：520 条语料（分块执行器），结束后保留采样日志供死亡签名分析
set -uo pipefail

cd "$(dirname "$0")/.."
CONC="${1:-6}"
TS=$(date +%Y%m%d-%H%M%S)
WLOG="logs/watchdog-$TS.log"

pids_of() { pgrep -f "parlant.bin.server|pvg_mcp_proxy|pvg_rag_service" | tr '\n' ' '; }

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$WLOG"; }

(
  while true; do
    line="$(date '+%H:%M:%S')"
    for pid in $(pids_of); do
      if ps -p "$pid" > /dev/null 2>&1; then
        rss=$(ps -o rss= -p "$pid" | awk '{printf "%.1f", $1/1048576}')
        comm=$(ps -o comm= -p "$pid" | xargs basename | cut -c1-28)
        line="$line  $comm($pid)=${rss}G"
      else
        line="$line  DEAD($pid)"
      fi
    done
    echo "$line" >> "$WLOG"
    sleep 5
  done
) &
WPID=$!
trap 'kill $WPID 2>/dev/null || true' EXIT

log "watchdog 启动（pid $WPID），压测并发 $CONC"
bash scripts/run_eval_chunked.sh "$CONC"
rc=$?
log "压测结束 rc=$rc，采样日志 $WLOG"
exit $rc
