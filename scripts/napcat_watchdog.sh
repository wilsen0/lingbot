#!/usr/bin/env bash
# NapCat 掉线自愈看门狗（密码自动登录版）
# ============================================================================
# ⚠️ 已退居二线：当前 linling 接的是 LLBot，不是 NapCat。日常请用
#    scripts/llbot_watchdog.sh。本脚本仅在回退到 NapCat 时才用。
# ============================================================================
#
# 背景：这台机器走移动蜂窝 CGNAT，出口 IP 频繁跳变，QQ 风控会周期性把
# 注入式登录判为「异地异常」强制下线（日志 KickedOffLine + ti.qq.com 短信验证）。
# 实测「快速登录」凭证要么写不进（Login/.<QQ> 为 0 字节），要么被服务端判
# 「登录态已失效」——所以单纯 docker restart 走快速登录对本机几乎不可能成功，
# 每次都回退到扫码。
#
# 因此本看门狗的恢复策略是：
#   离线 -> docker restart（不清缓存）-> 靠容器里配置的密码自动登录 -> 复检
# 只要容器配了 NAPCAT_QUICK_PASSWORD(_MD5)，整个过程无人值守、不用扫码。
# 没配密码时，本脚本只做检测 + 记录取证，不会去清缓存（清缓存=强制重扫，
# 那是 ./start.sh 选 4 的人工动作，不该自动跑）。
#
# 用法:
#   一次性检查并在离线时自愈:
#     ./scripts/napcat_watchdog.sh
#   常驻后台轮询（默认每 120s）:
#     ./scripts/napcat_watchdog.sh --loop
#     WATCH_INTERVAL=60 ./scripts/napcat_watchdog.sh --loop
#   挂 cron（每 2 分钟）:
#     */2 * * * * /home/wilsen/apps/apps/linling/scripts/napcat_watchdog.sh >> /tmp/napcat_watchdog.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."

CONTAINER="${NAPCAT_CONTAINER:-napcat}"
INTERVAL="${WATCH_INTERVAL:-120}"        # --loop 轮询间隔（秒）
INIT_WAIT="${NAPCAT_INIT_WAIT:-30}"      # restart 后等待登录完成（秒）
RECHECK_WAIT="${NAPCAT_RECHECK_WAIT:-15}" # 单次复检间隔（秒）
RECHECK_TRIES="${NAPCAT_RECHECK_TRIES:-6}" # restart 后最多复检几次
MAX_RESTARTS="${NAPCAT_MAX_RESTARTS:-2}"  # 单次自愈最多重启几次
FORENSIC_LOG="${NAPCAT_FORENSIC_LOG:-./data/napcat_offline.log}"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*"; }

# .env 里的 ONEBOT_WS_URL / ONEBOT_TOKEN 喂给在线探测脚本
if [[ -f .env ]]; then
  while IFS='=' read -r k v; do
    case "$k" in
      ONEBOT_WS_URL|ONEBOT_TOKEN) export "$k=${v}" ;;
    esac
  done < <(grep -E '^(ONEBOT_WS_URL|ONEBOT_TOKEN)=' .env || true)
fi

# 取当前公网出口 IP（取证用，判断是不是 IP 跳变触发风控）
egress_ip() {
  docker exec "$CONTAINER" sh -c \
    'curl -s --max-time 6 https://myip.ipip.net 2>/dev/null || curl -s --max-time 6 ifconfig.me 2>/dev/null' \
    2>/dev/null | tr -d '\n' | cut -c1-120
}

# 容器里是否配了登录密码（决定能不能无人值守恢复）
has_password() {
  docker inspect "$CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
    | grep -qE '^NAPCAT_QUICK_PASSWORD(_MD5)?='
}

# 0=在线 1=离线 2=连不上
status() { uv run python scripts/_napcat_online.py >/dev/null 2>&1; }

record_offline() {
  local ip; ip="$(egress_ip)"
  mkdir -p "$(dirname "$FORENSIC_LOG")"
  echo "$(ts)	offline	egress_ip=${ip:-unknown}" >> "$FORENSIC_LOG"
  log "⚠ NapCat 离线/不可达（出口IP=${ip:-unknown}，已记入 $FORENSIC_LOG）"
}

restart_and_wait() {
  if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    log "✗ 容器 '$CONTAINER' 不存在，无法自愈"; return 1
  fi
  log "⟳ 重启 $CONTAINER（保留缓存，靠密码自动登录恢复）..."
  docker restart "$CONTAINER" >/dev/null
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

  if ! has_password; then
    log "✗ 容器未配置 NAPCAT_QUICK_PASSWORD，无法自动登录。"
    log "  本机走移动 IP，快速登录基本无效；请配密码（见 docs/deployment/napcat.md），"
    log "  或人工执行 ./start.sh 选 4（清缓存重扫，会给出🔗+二维码）。"
    return 1
  fi

  local i
  for ((i = 1; i <= MAX_RESTARTS; i++)); do
    log "  第 $i/$MAX_RESTARTS 次重启 + 密码登录尝试"
    if restart_and_wait; then
      log "✓ 已恢复在线（密码自动登录成功）"
      return 0
    fi
  done
  log "✗ 连续 $MAX_RESTARTS 次重启仍未上线——可能密码错/被要求短信验证/风控加严。"
  log "  请人工 ./start.sh 选 4 处理（清缓存重扫，会给出🔗+二维码）。"
  return 1
}

main() {
  if [[ "${1:-}" == "--loop" ]]; then
    log "看门狗启动（每 ${INTERVAL}s 检查，容器=$CONTAINER，密码登录=$(has_password && echo on || echo off)）"
    while true; do
      heal_once || true
      sleep "$INTERVAL"
    done
  else
    heal_once
  fi
}

main "$@"
