#!/usr/bin/env bash
# 启动脚本 — 选择启动模式，自带重启检测
#
# 协议端现状：linling 已迁移到 LLBot(LLOneBot)，连 :3003。
# NapCat(:3001) 作为旧方案保留，仅在回退时用。
# ⚠ 同一个 QQ 号不能被 NapCat 和 LLBot 同时登录，否则互相顶号、群里收发错乱。
#   所以启动某一个协议端前，脚本会主动停掉另一个。
set -euo pipefail
cd "$(dirname "$0")"

LLBOT_COMPOSE="docker/llbot/docker-compose.yml"
LLBOT_WEBUI_TOKEN_FILE="${HOME}/.llbot/llbot_config/webui_token.txt"

# 不走代理：linling 连的是本机协议端和 LLM 端点，过代理只会出问题
unset all_proxy ALL_PROXY http_proxy HTTP_PROXY https_proxy HTTPS_PROXY
# 清掉 shell 里可能残留的旧 LLM / 连接配置,让 .env 做唯一配置来源。
# 注意 ONEBOT_WS_URL 也要清——否则 shell 里残留的旧值(如指向 NapCat 的
# :3001)会因为 load_dotenv(override=False) 盖过 .env 的 :3003，导致连错端。
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

# 停掉 NapCat（迁移到 LLBot 后，启动 LLBot 前必须先停 NapCat 防顶号）
stop_napcat_if_running() {
  if docker inspect napcat >/dev/null 2>&1 \
      && [[ "$(docker inspect -f '{{.State.Running}}' napcat 2>/dev/null)" == "true" ]]; then
    echo "→ 停止 NapCat（避免和 LLBot 抢同一个 QQ 号）..."
    docker stop napcat >/dev/null && echo "  NapCat 已停止"
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
echo "  NapCat (旧协议端, :3001, 仅回退用)"
echo "    5) 启动 linling + NapCat"
echo "    6) NapCat 清缓存重扫 (风控顶不住时用——给🔗+终端二维码)"
echo ""
read -rp "选择 [1/2/3/4/5/6]: " choice

case "$choice" in
  1)
    echo "→ 启动 linling (仅CLI模式)..."
    exec uv run linling run bot/bot.yaml --webui --only-adapters cli
    ;;
  2)
    echo "→ 检查 LLBot..."
    stop_napcat_if_running
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
    if uv run python scripts/_napcat_online.py >/dev/null 2>&1; then
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
  5)
    echo "→ [回退] 启动 linling + NapCat..."
    echo "  ⚠ 这是旧协议端。当前 .env 的 ONEBOT_WS_URL 指向 LLBot(:3003)，"
    echo "    用 NapCat 需要把 .env 的 ONEBOT_WS_URL 改回 ws://127.0.0.1:3001。"
    # 先停 LLBot 防顶号
    if docker inspect llbot-pmhq >/dev/null 2>&1 \
        && [[ "$(docker inspect -f '{{.State.Running}}' llbot-pmhq 2>/dev/null)" == "true" ]]; then
      echo "→ 停止 LLBot（避免和 NapCat 抢同一个 QQ 号）..."
      docker compose -f "$LLBOT_COMPOSE" stop >/dev/null 2>&1 || docker stop llbot-pmhq llbot >/dev/null 2>&1 || true
      echo "  LLBot 已停止"
    fi
    if docker inspect napcat >/dev/null 2>&1; then
      if [[ "$(docker inspect -f '{{.State.Running}}' napcat)" != "true" ]]; then
        docker start napcat >/dev/null
        echo "  NapCat 已启动"
      else
        echo "  NapCat 已在运行"
      fi
      napcat_token=$(docker exec napcat sh -c 'cat /app/napcat/config/webui.json' 2>/dev/null \
        | python3 -c "import json,sys; print(json.load(sys.stdin).get('token',''))" 2>/dev/null || true)
      if [[ -n "$napcat_token" ]]; then
        echo "  NapCat WebUI: http://127.0.0.1:6099/webui?token=$napcat_token"
      fi
    else
      echo "  ✗ NapCat 容器不存在，请先按 docs/deployment/napcat.md 部署"
      exit 1
    fi
    sleep 2
    echo "→ 启动 linling (完整模式, 连 NapCat)..."
    exec uv run linling run bot/bot.yaml --webui
    ;;
  6)
    echo "→ [回退] NapCat 清登录缓存，强制重新扫码..."
    if ! docker inspect napcat >/dev/null 2>&1; then
      echo "  ✗ NapCat 容器不存在"; exit 1
    fi
    # 清掉 nt_qq* 这些登录态缓存（不动 webui/onebot 配置）
    docker exec napcat sh -c "rm -rf /app/.config/QQ/nt_qq*" >/dev/null 2>&1 || true
    docker restart napcat >/dev/null
    echo "  NapCat 已重启，等它生成二维码..."
    sleep 12
    napcat_token=$(docker exec napcat sh -c 'cat /app/napcat/config/webui.json' 2>/dev/null \
      | python3 -c "import json,sys; print(json.load(sys.stdin).get('token',''))" 2>/dev/null || true)
    echo ""
    echo "  方式一 · WebUI 扫码（点链接，页面里选「扫码登录」）："
    echo "  🔗 http://127.0.0.1:6099/webui?token=$napcat_token"
    # 方式二 · 终端直接显示二维码 + 解码 URL
    uv run python scripts/_napcat_qrlogin.py || true
    echo "  手机 QQ 扫完上面任一二维码授权，再跑 ./start.sh 选 5 即可。"
    ;;
  *)
    echo "无效选择"; exit 1
    ;;
esac
