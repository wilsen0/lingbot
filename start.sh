#!/usr/bin/env bash
# 启动脚本 — 选择启动模式，自带重启检测
set -euo pipefail
cd "$(dirname "$0")"

# 不走代理：linling 连的是本机 NapCat 和 LLM 端点，过代理只会出问题
unset all_proxy ALL_PROXY http_proxy HTTP_PROXY https_proxy HTTPS_PROXY
# 清掉 shell 里可能残留的旧 LLM 配置,让 .env 做唯一配置来源
unset OPENAI_API_KEY OPENAI_BASE_URL LLM_API_KEY LLM_BASE_URL LINLING_MODEL 2>/dev/null || true

# ---- 重启检测 ----
OLD_PID=$(pgrep -f 'linling run' || true)
if [[ -n "$OLD_PID" ]]; then
  echo "⟳ 检测到 linling 正在运行 (PID: $OLD_PID)，正在停止..."
  kill $OLD_PID 2>/dev/null || true
  sleep 2
  kill -9 $OLD_PID 2>/dev/null || true
  echo "  已停止"
fi

# ---- 选模式 ----
echo ""
echo "  1) 只启动 linling (不连QQ)"
echo "  2) 启动 linling + QQ (完整服务)"
echo "  3) 软重连 NapCat (掉线了日常用这个——保留缓存，免扫码)"
echo "  4) 清缓存重扫 (仅账号被风控时用——会清登录态，要重新扫码)"
echo "  5) 启动掉线自愈看门狗 (后台常驻，掉线自动软重连)"
echo ""
read -rp "选择 [1/2/3/4/5]: " choice

case "$choice" in
  1)
    echo "→ 启动 linling (仅CLI模式)..."
    exec uv run linling run bot/bot.yaml --webui --only-adapters cli
    ;;
  2)
    echo "→ 检查 NapCat..."
    if docker inspect napcat >/dev/null 2>&1; then
      if [[ "$(docker inspect -f '{{.State.Running}}' napcat)" != "true" ]]; then
        docker start napcat >/dev/null
        echo "  NapCat 已启动"
      else
        echo "  NapCat 已在运行"
      fi
      # 把 NapCat WebUI 的免登录直达 URL 打印出来，省得每次手动输 token
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
    echo "→ 启动 linling (完整模式)..."
    exec uv run linling run bot/bot.yaml --webui
    ;;
  3)
    echo "→ 软重连 NapCat: 保留登录缓存，走快速登录（不用扫码）..."
    if ! docker inspect napcat >/dev/null 2>&1; then
      echo "  ✗ NapCat 容器不存在"; exit 1
    fi
    docker restart napcat >/dev/null
    echo "  NapCat 已重启，等它走快速登录..."
    sleep 8
    # 复检在线状态
    if uv run python scripts/_napcat_online.py >/dev/null 2>&1; then
      echo "  ✓ 账号已恢复在线，直接 ./start.sh 选 2 即可"
    else
      echo "  ⚠ 还没上线，再等十几秒看 docker logs napcat；"
      echo "    若日志反复报「快速登录失败/历史登录记录」，才需要选 4 重扫。"
    fi
    ;;
  4)
    echo "→ 清登录缓存，强制重新扫码（仅风控时用）..."
    if ! docker inspect napcat >/dev/null 2>&1; then
      echo "  ✗ NapCat 容器不存在"; exit 1
    fi
    # 清掉 nt_qq* 这些登录态缓存（不动 webui/onebot 配置）
    docker exec napcat sh -c "rm -rf /app/.config/QQ/nt_qq*" >/dev/null 2>&1 || true
    docker restart napcat >/dev/null
    echo "  NapCat 已重启，等它初始化..."
    sleep 8
    napcat_token=$(docker exec napcat sh -c 'cat /app/napcat/config/webui.json' 2>/dev/null \
      | python3 -c "import json,sys; print(json.load(sys.stdin).get('token',''))" 2>/dev/null || true)
    echo ""
    echo "  打开下面这个链接，选「扫码登录」用手机 QQ 扫一下："
    echo "  http://127.0.0.1:6099/webui?token=$napcat_token"
    echo ""
    echo "  扫完之后再跑 ./start.sh 选 2 即可。"
    ;;
  5)
    echo "→ 启动掉线自愈看门狗（后台常驻）..."
    if ! docker inspect napcat >/dev/null 2>&1; then
      echo "  ✗ NapCat 容器不存在"; exit 1
    fi
    exec ./scripts/napcat_watchdog.sh --loop
    ;;
  *)
    echo "无效选择"; exit 1
    ;;
esac
