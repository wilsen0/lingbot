# linling-webui

狐妖情缘主题的 linling 管理面板。FastAPI 后端 + Vue 3 SPA，**移动优先**。

## 功能

- **缘起 / Login**：JWT access + refresh，argon2id 密码，5 次/分钟 登录限流
- **灵签 / Dashboard**：健康度、连接的 bot 列表、版本号
- **因缘簿 / Events**：WebSocket 实时事件流，支持离线补发（`since_seq`）、下拉刷新、按 kind 过滤
- **灵玉阁 / KV**：浏览 scope/file/key、乐观并发编辑（If-Match 412）、排行榜
- **签文 / Rules**：规则命中统计、最近 hits 抽屉
- **红娘司 / Agents**：Agent 列表、流式试聊（WS）、短期记忆
- **命格 / Audit**：搜索审计日志、CSV 导出（≤ 10k 行）
- **绳结 / Settings**：Bot 在线灯 + 热加载、主题（月白/暮紫/随系）、装饰层（尽兴/含蓄/静默）、铃响/震感开关、服务器配置（已脱敏）

## 主题 · 视觉

狐妖小红娘 · 情缘三锚点：

- **苦情树** · 顶部 hero 插画，缠红线、挂铃铛
- **铃铛** · 通知铃、加载器、挂角铃铛
- **幻粉微风** · 径向渐变雾层 + 落樱花瓣 canvas

在 Settings 里可切 `尽兴 / 含蓄 / 静默` 三档装饰密度；系统开启「减弱动画」时装饰层自动静默（WUI-C7）。

## 启动

```bash
# 初始化一个管理员
uv run python -m linling_webui.scripts.init_user --username admin --password your-pwd

# 起服务
uv run linling serve webui --host 0.0.0.0 --port 8787

# 或指定 bot.yaml（如果含 webui: 段）
uv run linling serve webui --bot bot/bot.yaml
```

默认账号：用 `--username admin --password linling` 初始化的就是默认账号。

打开 `http://127.0.0.1:8787/`，用上面的账号登入。

## 开发模式

```bash
# 终端 A：后端
uv run linling serve webui --port 8787

# 终端 B：前端 dev server + HMR
pnpm --filter @linling/webui-frontend dev
# → Vite 起在 :5173，并将 /api /ws 代理到 :8787
```

## 配置（bot.yaml）

```yaml
webui:
  host: 0.0.0.0
  port: 8787
  jwt_secret: ${LINLING_WEBUI_JWT_SECRET}
  cors_origins: []          # 空 = 同源
  auth_db_path: ./data/webui_auth.db
  event_buffer_size: 500
  login_rate_per_minute: 5
  write_rate_per_minute: 60
```

生产环境请务必：

1. 覆盖 `jwt_secret`（默认每次重启随机生成，所有 session 会失效）
2. 通过反向代理 TLS 终止（nginx/caddy）
3. CORS 白名单按需配置

## 接口

```
REST
  GET  /api/health                 无需鉴权
  POST /api/auth/login
  POST /api/auth/refresh
  POST /api/auth/logout
  GET  /api/profile
  GET  /api/bots
  POST /api/bots/:bot_id/hot-reload
  GET  /api/events                 ?since_seq= ?kind= ?limit=
  GET  /api/events/:bot/:id
  POST /api/events/:bot/:id/replay
  GET  /api/kv                     ?scope=
  GET  /api/kv/:scope/:file        ?prefix= ?cursor= ?limit=
  GET  /api/kv/:scope/:file/:key   返回 ETag
  PATCH /api/kv/:scope/:file/:key  If-Match: <etag>  → 412 on mismatch
  DELETE /api/kv/:scope/:file/:key
  GET  /api/kv/:scope/:file/rank   ?order=desc&top=10&sep=&fmt=
  GET  /api/rules
  GET  /api/rules/:name/hits
  GET  /api/agents
  GET  /api/agents/:name
  GET  /api/agents/:name/memory    ?user_id=&scope_id=
  POST /api/agents/:name/chat
  GET  /api/audit                  ?bot_id= ?user_id= ?kind= ?outcome= ?q=
  GET  /api/audit.csv              ?limit=10000
  GET  /api/settings               敏感字段返回 "***"

WebSocket  （?token=<access-jwt>）
  /ws/events                       hello / event / filter_ack / ping
  /ws/agents/:name/stream          hello / delta / tool_call / tool_result / done / error
  /ws/rules/hits                   hit
```

## 安全

- 所有非 `/api/auth/*` 路径要求 Bearer access JWT
- 写接口按用户限流 60/min，登录按 IP 限流 5/min
- Refresh token 存 sqlite 可撤销；每次 refresh 轮转 jti
- CSP `default-src 'self'; frame-ancestors 'none'`
- `/api/settings` 对含 `secret`/`token`/`password` 的字段做 `***` 脱敏 (WUI-C8)

## 测试

```bash
# 后端
uv run pytest packages/webui/tests

# 前端
pnpm --filter @linling/webui-frontend lint
pnpm --filter @linling/webui-frontend typecheck
pnpm --filter @linling/webui-frontend test:unit
pnpm --filter @linling/webui-frontend build
```

## 反代示例 (nginx)

```nginx
server {
    listen 443 ssl http2;
    server_name webui.example.com;

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
    location /ws/ {
        proxy_pass http://127.0.0.1:8787;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400s;
    }
}
```

## 目录

```
packages/webui/
├── pyproject.toml
├── README.md                        (this file)
├── Dockerfile
├── src/linling_webui/
│   ├── app.py                       FastAPI factory
│   ├── config.py
│   ├── auth.py                      argon2id + JWT + refresh store
│   ├── buffers.py                   per-bot event ring buffer
│   ├── audit_reader.py              audit log (in-memory fallback)
│   ├── deps.py                      FastAPI deps / role gate
│   ├── rate_limit.py
│   ├── middleware.py                CSP + security headers
│   ├── schemas.py
│   ├── state.py                     WebUIState container
│   ├── wire.py                      wire_bot / wire_agents / wire_hot_reload
│   ├── routers/                     health, auth, bots, events, kv, rules, agents, audit, settings
│   ├── ws/                          events, agents, rules
│   ├── scripts/init_user.py         `python -m linling_webui.scripts.init_user`
│   └── static/                      built SPA (committed as build artifact)
└── frontend/                        pnpm workspace · Vite + Vue 3 + TS + Tailwind v4
    ├── src/theme/                   tokens.css · fonts.css · tailwind.css
    ├── src/decor/                   DecoBreezeLayer · DecoPetalCanvas · DecoBellAccent
    │                                DecoBellLoader · DecoSorrowTree · DecoThreadDivider
    ├── src/components/              UiButton · UiCard · UiChip · UiPill · UiInput
    │                                UiSheet · UiEmptyState · UiSkeleton · UiToast
    │                                UiVirtualList · UiPullRefresh
    ├── src/composables/             useToast · useHaptics · useBellSound · useKeyboardShortcuts
    ├── src/api/                     client · ws · events · kv · bots · agents · audit · rules
    ├── src/layouts/AppShell.vue
    ├── src/pages/                   Login · Dashboard · Events · Kv · Rules
    │                                Agents · AgentDetail · Audit · Settings
    └── tests/                       theme · auth · prefs spec
```
