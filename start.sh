#!/usr/bin/env bash
# 启动脚本 — 选择启动模式，自带重启检测
#
# 协议端：linling 当前接 LLBot(LLOneBot)，连 :3003。
# 配置见 docker/llbot/docker-compose.yml。
set -euo pipefail
cd "$(dirname "$0")"

LLBOT_COMPOSE="docker/llbot/docker-compose.yml"
LLBOT_WEBUI_TOKEN_FILE="${HOME}/.llbot/llbot_config/webui_token.txt"

# 不走代理：linling 连的是本机协议端和 LLM 端点，过代理只会出问题
unset all_proxy ALL_PROXY http_proxy HTTP_PROXY https_proxy HTTPS_PROXY
# 清掉 shell 里可能残留的旧 LLM / 连接配置,让 .env 做唯一配置来源。
unset OPENAI_API_KEY OPENAI_BASE_URL LLM_API_KEY LLM_BASE_URL LINLING_MODEL \
      ONEBOT_WS_URL ONEBOT_TOKEN 2>/dev/null || true

# ---- 重启检测：杀掉所有残留的 linling 实例（含多开） ----
# 用 pkill 全量匹配，循环确认，避免上次踩到的「多开抢账号」问题。
kill_stale_linling() {
  local pids
  pids=$(pgrep -f 'linling run' || true)
  if [[ -z "$pids" ]]; then
    return 0
  fi
  echo "⟳ 检测到 linling 正在运行 (PID: $(echo "$pids" | tr '\n' ' '))，正在全部停止..."
  pkill -f 'linling run' 2>/dev/null || true
  sleep 2
  pkill -9 -f 'linling run' 2>/dev/null || true
  sleep 1
  if pgrep -f 'linling run' >/dev/null 2>&1; then
    echo "  ⚠ 仍有残留进程，请手动 kill -9 后重试"
  else
    echo "  已全部停止"
  fi
}

# 确保 LLBot 两个容器(pmhq + llbot)起着
ensure_llbot_up() {
  if ! docker inspect llbot-pmhq >/dev/null 2>&1; then
    echo "  ✗ LLBot 容器不存在，先部署："
    echo "    docker compose -f $LLBOT_COMPOSE up -d"
    return 1
  fi
  local started=0
  for c in llbot-pmhq llbot; do
    if [[ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" != "true" ]]; then
      docker compose -f "$LLBOT_COMPOSE" up -d >/dev/null 2>&1 || docker start "$c" >/dev/null 2>&1 || true
      started=1
    fi
  done
  if [[ "$started" == "1" ]]; then
    echo "  LLBot 容器已拉起，等待 QQ 登录初始化..."
    sleep 8
  else
    echo "  LLBot 容器已在运行"
  fi
  # 打印 WebUI 直达（扫码登录 / 改配置）
  if [[ -f "$LLBOT_WEBUI_TOKEN_FILE" ]]; then
    local tok; tok=$(tr -d '\r\n' < "$LLBOT_WEBUI_TOKEN_FILE" 2>/dev/null || true)
    echo "  LLBot WebUI: http://127.0.0.1:3080  (token: ${tok:-见 $LLBOT_WEBUI_TOKEN_FILE})"
  else
    echo "  LLBot WebUI: http://127.0.0.1:3080"
  fi
  return 0
}

kill_stale_linling

# ---- 选模式 ----
echo ""
echo "  本地 (不连QQ)"
echo "    1) 只启动 linling (仅 CLI / WebUI 联调)"
echo ""
echo "  LLBot (当前协议端, :3003)"
echo "    2) 启动 linling + LLBot (完整服务)"
echo "    3) 重启 LLBot 协议端 (掉线了试这个——走快速登录恢复)"
echo "    4) 启动掉线自愈看门狗 (后台常驻，盯 LLBot，掉线自动重启)"
echo ""
read -rp "选择 [1/2/3/4]: " choice

case "$choice" in
  1)
    echo "→ 启动 linling (仅CLI模式)..."
    exec uv run linling run bot/bot.yaml --webui --only-adapters cli
    ;;
  2)
    echo "→ 检查 LLBot..."
    ensure_llbot_up || exit 1
    echo "→ 启动 linling (完整模式, 连 LLBot)..."
    exec uv run linling run bot/bot.yaml --webui
    ;;
  3)
    echo "→ 重启 LLBot 协议端（只重启 pmhq，走快速登录恢复）..."
    if ! docker inspect llbot-pmhq >/dev/null 2>&1; then
      echo "  ✗ LLBot 容器不存在，先 docker compose -f $LLBOT_COMPOSE up -d"; exit 1
    fi
    docker compose -f "$LLBOT_COMPOSE" restart pmhq >/dev/null 2>&1 \
      || docker restart llbot-pmhq >/dev/null
    echo "  已重启，等它登录..."
    sleep 30
    if uv run python scripts/_llbot_online.py >/dev/null 2>&1; then
      echo "  ✓ 账号已恢复在线，直接 ./start.sh 选 2 即可"
    else
      echo "  ⚠ 还没上线。快速登录缓存可能失效，开 WebUI http://127.0.0.1:3080 扫码，"
      echo "    或看 docker compose -f $LLBOT_COMPOSE logs -f llbot 里的二维码。"
    fi
    ;;
  4)
    echo "→ 启动掉线自愈看门狗（后台常驻，盯 LLBot）..."
    if ! docker inspect llbot-pmhq >/dev/null 2>&1; then
      echo "  ✗ LLBot 容器(llbot-pmhq)不存在，先 docker compose -f $LLBOT_COMPOSE up -d"
      exit 1
    fi
    exec ./scripts/llbot_watchdog.sh --loop
    ;;
  *)
    echo "无效选择"; exit 1
    ;;
esac
