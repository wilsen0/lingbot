#!/usr/bin/env bash
# LLBot(LLOneBot) 掉线自愈看门狗
# ----------------------------------------------------------------------------
# 当前 linling 接的是 LLBot（pmhq + llbot 两容器，见 docker/llbot/docker-compose.yml）。
# 本看门狗定时用 OneBot get_status 探活，离线就：
#   记录时间+出口IP（取证）-> docker compose restart pmhq -> 走快速登录 -> 复检
#
# 说明：
# - LLBot 没有「密码登录」环境变量。掉线后能否自动恢复，取决于 pmhq 的快速登录
#   缓存（qq_volume）是否还有效：有效则免扫码自动回来；失效则需要人工扫码
#   （docker compose ... logs 里会再出二维码，或开 WebUI :3080）。
# - 只 restart pmhq，不动 llbot（OneBot 服务端无状态，pmhq 才持有 QQ 登录态）。
# - 连续 MAX_RESTARTS 次仍上不来 -> 停手提示人工扫码，避免无谓重启风暴。
#
# 用法：
#   一次性检查：           ./scripts/llbot_watchdog.sh
#   后台常驻（默认120s）： ./scripts/llbot_watchdog.sh --loop
#   cron（每2分钟）：
#     */2 * * * * /home/wilsen/apps/apps/linling/scripts/llbot_watchdog.sh >> /tmp/llbot_watchdog.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE_FILE="${LLBOT_COMPOSE_FILE:-docker/llbot/docker-compose.yml}"
QQ_CONTAINER="${LLBOT_QQ_CONTAINER:-llbot-pmhq}"   # 持有 QQ 登录态的容器
INTERVAL="${WATCH_INTERVAL:-120}"
INIT_WAIT="${LLBOT_INIT_WAIT:-30}"
RECHECK_WAIT="${LLBOT_RECHECK_WAIT:-15}"
RECHECK_TRIES="${LLBOT_RECHECK_TRIES:-6}"
MAX_RESTARTS="${LLBOT_MAX_RESTARTS:-2}"
FORENSIC_LOG="${LLBOT_FORENSIC_LOG:-./data/qqbot_offline.log}"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*"; }

# .env 的 ONEBOT_WS_URL / ONEBOT_TOKEN 喂给探活脚本（默认就是 LLBot 的 :3003）
if [[ -f .env ]]; then
  while IFS='=' read -r k v; do
    case "$k" in
      ONEBOT_WS_URL|ONEBOT_TOKEN) export "$k=${v}" ;;
    esac
  done < <(grep -E '^(ONEBOT_WS_URL|ONEBOT_TOKEN)=' .env || true)
fi

egress_ip() {
  docker exec "$QQ_CONTAINER" sh -c \
    'curl -s --max-time 6 https://myip.ipip.net 2>/dev/null || curl -s --max-time 6 ifconfig.me 2>/dev/null' \
    2>/dev/null | tr -d '\n' | cut -c1-120
}

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

# 0=在线 1=离线 2=连不上
status() { uv run python scripts/_llbot_online.py >/dev/null 2>&1; }

record_offline() {
  local ip; ip="$(egress_ip)"
  mkdir -p "$(dirname "$FORENSIC_LOG")"
  echo "$(ts)	offline	egress_ip=${ip:-unknown}" >> "$FORENSIC_LOG"
  log "⚠ LLBot 离线/不可达（出口IP=${ip:-unknown}，已记入 $FORENSIC_LOG）"
}

restart_and_wait() {
  if ! docker inspect "$QQ_CONTAINER" >/dev/null 2>&1; then
    log "✗ 容器 '$QQ_CONTAINER' 不存在，无法自愈"; return 1
  fi
  log "⟳ 重启 pmhq（走快速登录恢复，缓存有效则免扫码）..."
  compose restart pmhq >/dev/null 2>&1 || docker restart "$QQ_CONTAINER" >/dev/null
  log "  等待 QQ 初始化与登录 ${INIT_WAIT}s ..."
  sleep "$INIT_WAIT"
  local i
  for ((i = 1; i <= RECHECK_TRIES; i++)); do
    if status; then return 0; fi
    log "  复检 $i/$RECHECK_TRIES：还没上线，再等 ${RECHECK_WAIT}s"
    sleep "$RECHECK_WAIT"
  done
  return 1
}

heal_once() {
  if status; then return 0; fi
  record_offline
  local i
  for ((i = 1; i <= MAX_RESTARTS; i++)); do
    log "  第 $i/$MAX_RESTARTS 次重启尝试"
    if restart_and_wait; then
      log "✓ 已恢复在线（快速登录成功）"
      return 0
    fi
  done
  log "✗ 连续 $MAX_RESTARTS 次重启仍未上线——快速登录缓存可能已失效，需要人工扫码："
  log "  开 WebUI http://127.0.0.1:3080 ，或看 docker compose -f $COMPOSE_FILE logs -f llbot 里的二维码。"
  return 1
}

main() {
  if [[ "${1:-}" == "--loop" ]]; then
    log "LLBot 看门狗启动（每 ${INTERVAL}s 检查，QQ容器=$QQ_CONTAINER）"
    while true; do
      heal_once || true
      sleep "$INTERVAL"
    done
  else
    heal_once
  fi
}

main "$@"
