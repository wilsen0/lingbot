#!/usr/bin/env bash
# NapCat 掉线自愈看门狗
# ----------------------------------------------------------------------------
# 检测到账号离线时，自动 docker restart napcat 走「快速登录」恢复，
# 不清缓存、不需要扫码（前提：上次是扫码/密码登录成功过，nt_qq 缓存还在）。
#
# 用法:
#   一次性检查并在离线时恢复:
#     ./scripts/napcat_watchdog.sh
#   常驻后台轮询（默认每 120s 检查一次）:
#     ./scripts/napcat_watchdog.sh --loop
#     WATCH_INTERVAL=60 ./scripts/napcat_watchdog.sh --loop
#   挂 cron（每 2 分钟）:
#     */2 * * * * /home/wilsen/apps/apps/linling/scripts/napcat_watchdog.sh >> /tmp/napcat_watchdog.log 2>&1
#
# 设计原则:
#   - 只做 docker restart（保留登录缓存），绝不 rm nt_qq*。清缓存是风控时
#     的人工操作，不该让看门狗自动跑，否则每次都被迫重新扫码。
#   - 重启后给 QQ 留足初始化时间，再复检；连续多次恢复失败才告警，避免
#     在 QQ 服务端抖动时疯狂重启。
set -euo pipefail
cd "$(dirname "$0")/.."

CONTAINER="${NAPCAT_CONTAINER:-napcat}"
INTERVAL="${WATCH_INTERVAL:-120}"     # --loop 模式轮询间隔（秒）
INIT_WAIT="${NAPCAT_INIT_WAIT:-25}"   # restart 后等待 QQ 初始化（秒）
MAX_RESTARTS="${NAPCAT_MAX_RESTARTS:-3}"  # 单次自愈最多重启几次

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*"; }

# .env 里有 ONEBOT_TOKEN / ONEBOT_WS_URL 就读进来，给 _napcat_online.py 用
if [[ -f .env ]]; then
  # 只导出这两个键，避免把整份 .env 灌进环境
  while IFS='=' read -r k v; do
    case "$k" in
      ONEBOT_WS_URL|ONEBOT_TOKEN) export "$k=${v}" ;;
    esac
  done < <(grep -E '^(ONEBOT_WS_URL|ONEBOT_TOKEN)=' .env || true)
fi

status() {
  # 0=在线 1=离线 2=连不上
  uv run python scripts/_napcat_online.py >/dev/null 2>&1
}

restart_napcat() {
  if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    log "✗ 容器 '$CONTAINER' 不存在，无法自愈"
    return 1
  fi
  log "⟳ 软重启 $CONTAINER（保留登录缓存，走快速登录）..."
  docker restart "$CONTAINER" >/dev/null
  log "  等待 QQ 初始化 ${INIT_WAIT}s ..."
  sleep "$INIT_WAIT"
}

# 检查一轮；离线就尝试恢复。返回 0 表示最终在线，非 0 表示仍未恢复。
heal_once() {
  if status; then
    return 0
  fi
  log "⚠ 检测到 NapCat 离线/不可达，开始自愈"
  local i
  for ((i = 1; i <= MAX_RESTARTS; i++)); do
    log "  第 $i/$MAX_RESTARTS 次重启尝试"
    restart_napcat || return 1
    if status; then
      log "✓ 已恢复在线"
      return 0
    fi
    log "  重启后仍未在线，稍候再试"
    sleep 5
  done
  log "✗ 连续 $MAX_RESTARTS 次重启仍未恢复——可能账号被风控，需要人工扫码:"
  log "  ./start.sh 选 4（清缓存重扫）"
  return 1
}

main() {
  if [[ "${1:-}" == "--loop" ]]; then
    log "看门狗启动（每 ${INTERVAL}s 检查一次，容器=$CONTAINER）"
    while true; do
      heal_once || true
      sleep "$INTERVAL"
    done
  else
    heal_once
  fi
}

main "$@"
