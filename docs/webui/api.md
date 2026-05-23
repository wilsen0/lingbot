# linling-webui API

> 所有 JSON 响应遵循 `application/json; charset=utf-8`。
> 所有非 `/api/auth/*` 路径需要 `Authorization: Bearer <access-jwt>`。
> WebSocket 握手用 `?token=<access-jwt>` 传 JWT（浏览器对 WS header 支持有限）。

## REST

### Auth

- `POST /api/auth/login` body `{username, password}` → `{access, refresh, access_expires_at, refresh_expires_at}`
- `POST /api/auth/refresh` body `{refresh}` → 新 pair（旧 refresh 被 revoke）
- `POST /api/auth/logout` body `{refresh}` → 204，幂等
- `GET /api/profile` → `{username, role, bots}`

### Bots

- `GET /api/bots` → `BotStatus[]`
- `POST /api/bots/:bot_id/hot-reload` → `{reloaded, errors}`（依赖主 spec Task 22）

### Events

- `GET /api/events?bot_id=&since_seq=&kind=&limit=` → `{items: EventEnvelope[], next_cursor}`
- `GET /api/events/:bot/:id` → `EventEnvelope`
- `POST /api/events/:bot/:id/replay` body `{dry_run: true}` → `{ok, message, dry_run}`
  - 只接受 dry_run；非 dry_run 直接 400

### KV

- `GET /api/kv?scope=` → `KvNamespace[]`（若 backend 支持 `scopes()` 则不带 scope 也可列）
- `GET /api/kv/:scope/:file?prefix=&cursor=&limit=` → `{items: KvRow[], next_cursor}`
- `GET /api/kv/:scope/:file/:key` → `KvRow`，header `ETag`
- `PATCH /api/kv/:scope/:file/:key` header `If-Match: <etag>` → 200 或 412
- `DELETE /api/kv/:scope/:file/:key` → 204 / 404
- `GET /api/kv/:scope/:file/rank?order=&top=&sep=&fmt=` → `{rows, formatted}`

### Rules

- `GET /api/rules` → `RuleSummary[]`（从审计 `handler_dispatch` 聚合）
- `GET /api/rules/:name/hits?limit=` → 最近 N 次 hit

### Agents

- `GET /api/agents` / `GET /api/agents/:name`
- `GET /api/agents/:name/memory?user_id=&scope_id=` → `{short_term, long_term}`
- `POST /api/agents/:name/chat` body `{input}` → `{content, tool_calls_made, total_tokens, latency_ms}`

### Audit & Settings

- `GET /api/audit?bot_id=&user_id=&kind=&outcome=&q=&limit=` → `AuditEntry[]`
- `GET /api/audit.csv?limit=10000` → `text/csv`
- `GET /api/settings` → 脱敏配置

## WebSocket

### `/ws/events`

```
# client → server
{ "t": "filter", "data": { "since_seq": 42, "bots": ["susu_main"] } }
{ "t": "ping" }

# server → client
{ "t": "hello", "server_time": 169…, "capacity": 500 }
{ "t": "event", "bot_id": "susu_main", "data": EventEnvelope }
{ "t": "filter_ack", "replayed": 12 }
{ "t": "ping" }                     # 每 25s
```

Server 在认证失败时用 close code 1008 断开。

### `/ws/agents/:name/stream`

```
# client → server
{ "t": "input", "content": "你好" }
{ "t": "cancel" }

# server → client
{ "t": "hello", "agent": "susu" }
{ "t": "delta", "text": "你" }        # 0+ 条
{ "t": "tool_call", "id": "...", "name": "read_kv", "args": {...} }
{ "t": "tool_result", "id": "...", "result": "..." }
{ "t": "done", "tool_calls_made": 1, "total_tokens": 314 }
{ "t": "error", "msg": "..." }
```

### `/ws/rules/hits`

```
# server → client
{ "t": "hello" }
{ "t": "hit", "data": { id, time, bot_id, handler, outcome, latency_ms } }
```

## 错误形态

统一 `{"detail": "..."}` 与 HTTP 状态码：

| 状态 | 场景 |
|------|------|
| 400 | 请求体/参数不合法（e.g. dry_run=false） |
| 401 | 缺失 / 过期 / 非 access token |
| 403 | 角色不足（readonly 调写接口） |
| 404 | 资源不存在 / 该用户不可见的 bot |
| 409 / 412 | 乐观并发失败（If-Match mismatch） |
| 429 | 登录或写接口限流 |
| 503 | 上游未就绪（e.g. audit 表未建） |

## 正确性属性速查

| ID | 属性 | 覆盖测试 |
|----|------|----------|
| C1 | bearer 鉴权 | `test_auth.test_profile_requires_bearer` |
| C2 | 多租户隔离 | `test_bots.test_cross_tenant_access_is_404` |
| C3 | 事件 seq 有序 | `test_ws_events.test_ws_filter_replay_since` |
| C4 | KV 写后读 | `test_kv_router.test_write_then_read_roundtrip` |
| C5 | 乐观并发 | `test_kv_router.test_etag_optimistic_concurrency` |
| C7 | 动效降级 | `test_buffers`, `theme.spec` 静态 |
| C8 | 脱敏 | `test_settings_redact` |
| C9 | 审计完整 | `test_ws_agents.test_ws_agent_audit_written` |
| C13 | CSP | `test_health.test_csp_header_present` |
