# Tasks — linling-webui（狐妖情缘主题 Web UI）

> 基于 `design.md`。每一条都落到具体文件路径与可执行输出，默认 **移动端优先**，桌面作为 progressive enhancement。
> 所有 WebUI 相关包/代码置于 `packages/webui/`（Python）与 `packages/webui/frontend/`（前端）。
>
> 图例：`🟣` 视觉/主题 · `🟡` 组件 · `🔵` 后端 · `🟢` 联调/测试 · `⚪` 工程/发布

---

## 阶段 W0 — 工程骨架

- [x] 1. 🔵 新建后端包 `packages/webui`
    - [x] 1.1 `pyproject.toml`：name `linling-webui`，依赖 `fastapi / uvicorn[standard] / pydantic-settings / python-jose[cryptography] / argon2-cffi / linling-core`
    - [x] 1.2 注册到根 `pyproject.toml` 的 `[tool.uv.workspace].members` 与 `[tool.uv.sources]`
    - [x] 1.3 `src/linling_webui/__init__.py` + `version.py`
    - [x] 1.4 `app.py`：`create_app(config)` 骨架（无路由，只挂静态目录存根）
    - [x] 1.5 `config.py`：`WebUIConfig`（bind/host/port/jwt_secret/cors/static_dir）
    - [x] 1.6 `tests/test_app_smoke.py`：`create_app` 返回 FastAPI，`/api/health` 200

- [x] 2. ⚪ 新建前端子项目 `packages/webui/frontend`
    - [x] 2.1 `pnpm-workspace.yaml`（根目录）+ `packages/webui/frontend/package.json`
    - [x] 2.2 Vite + Vue 3 + TS + Tailwind v4 + Pinia + vue-router 初始化
    - [x] 2.3 ESLint + Prettier + `eslint-plugin-vue` + `eslint-plugin-tailwindcss`
    - [x] 2.4 `vite.config.ts`：`proxy /api -> :8787`、`proxy /ws -> :8787`
    - [x] 2.5 `index.html` viewport：`width=device-width,initial-scale=1,viewport-fit=cover,interactive-widget=resizes-content`
    - [x] 2.6 CI：`pnpm lint && pnpm typecheck && pnpm build` + 产物同步到 `packages/webui/src/linling_webui/static/`

- [x] 3. ⚪ CLI 挂载
    - [x] 3.1 `packages/cli/src/linling_cli/commands/serve_webui.py`：`linling serve webui --host --port`
    - [x] 3.2 读 `bot.yaml` 的 `webui` 段；未配置走默认
    - [x] 3.3 smoke：`linling serve webui` 起 uvicorn 并在 2s 内 `/api/health` 200

---

## 阶段 W1 — 主题层 & 装饰层

- [x] 4. 🟣 主题 token 落地
    - [x] 4.1 `frontend/src/theme/tokens.css`：light/dark 两套 `--color-*`、`--radius-*`、`--space-*`、`--shadow-*`、`--motion-*`
    - [x] 4.2 `frontend/src/theme/tailwind.ts`：`theme.extend.colors = { sorrow, thread, bell, petal, jade, alert, ink, 'ink-soft', bg, 'bg-veil' }`
    - [x] 4.3 `frontend/src/theme/fonts.css` + `/static/fonts/` 放置子集化 woff2（`Noto Sans SC / Noto Serif SC / Ma Shan Zheng / JetBrains Mono`），`font-display: swap`
    - [x] 4.4 `App.vue` 根节点根据 `prefs.theme` 切 `data-theme="light|dark|auto"`，auto 跟随 `prefers-color-scheme`
    - [x] 4.5 单测：快照验证 light/dark 两套 CSS var 输出一致

- [x] 5. 🟣 装饰层组件
    - [x] 5.1 `DecoBreezeLayer.vue`：幻粉径向渐变 + `--motion-breeze-drift`，`aria-hidden`
    - [x] 5.2 `DecoPetalCanvas.vue`：canvas 花瓣发射器；props `density: 'full'|'subtle'|'off'`；最大 24 粒；`visibilitychange` 暂停
    - [x] 5.3 `DecoBellAccent.vue`：SVG 铃铛，tap 触发 `--motion-bell-swing`；可选 Web Audio 轻响（默认关）
    - [x] 5.4 `DecoBellLoader.vue`：加载器；`size: 'sm'|'md'|'lg'`
    - [x] 5.5 `DecoSorrowTree.vue`：Dashboard hero 插画；props `bells: 0..3`
    - [x] 5.6 `DecoThreadDivider.vue`：两段红线 + 花瓣粒分割线
    - [x] 5.7 所有装饰组件在 `prefers-reduced-motion: reduce` 下渲染为 `display:none`（单测覆盖 **WUI-C7**）

- [x] 6. 🟡 原子组件 `Ui-*`
    - [x] 6.1 `UiButton.vue`（primary/ghost/thread/danger/jade + loading 自动 `DecoBellLoader`）
    - [x] 6.2 `UiCard.vue`（glass/elevated/padding）
    - [x] 6.3 `UiInput.vue` / `UiTextarea.vue`（focus 红线绘出动画）
    - [x] 6.4 `UiChip.vue` / `UiPill.vue` / `UiTabs.vue`
    - [x] 6.5 `UiSheet.vue` bottom sheet：`<dialog>` + drag-down-to-close + safe-area-inset-bottom
    - [x] 6.6 `UiEmptyState.vue`：插画 slot + 默认 `DecoBellHang`
    - [x] 6.7 `UiSkeleton.vue`：shimmer，不引入新装饰
    - [x] 6.8 `UiToast.vue` + `useToast()`：幻粉底 + 红线边
    - [x] 6.9 `UiVirtualList.vue`（封装 `@tanstack/vue-virtual`）
    - [x] 6.10 `UiPullRefresh.vue`：阈值 72px，触发回调并 swing 顶部铃铛
    - [x] 6.11 所有可点击元素 bounding ≥ 44×44px（Playwright 扫一遍，**WUI-C11**）

---

## 阶段 W2 — 后端：鉴权 & 基础设施

- [x] 7. 🔵 鉴权
    - [x] 7.1 `auth.py`：argon2id 密码 hash（m=64MB,t=3,p=2）、JWT access(15m)/refresh(7d)
    - [x] 7.2 sqlite 用户表 + refresh 表（可撤销）；迁移 `scripts/webui_init_user.py`
    - [x] 7.3 `routers/auth.py`：`POST /api/auth/login` / `/refresh` / `/logout` / `GET /api/profile`
    - [x] 7.4 登录 rate-limit 5 次/分钟/IP
    - [x] 7.5 测试：登录/刷新/过期/撤销；redactor 验证错误信息不泄密码哈希

- [x] 8. 🔵 依赖与中间件
    - [x] 8.1 `deps.py`：`get_kv / get_bus / get_scheduler / get_audit / require_auth / require_role / require_bot_visibility`
    - [x] 8.2 CORS 中间件（默认同源）
    - [x] 8.3 CSP 响应头中间件（**WUI-C13** 单测校验）
    - [x] 8.4 `GET /api/health`：版本、bot 在线状态、最近事件时间

- [x] 9. 🔵 内存环形缓冲 + EventBus 订阅
    - [x] 9.1 `buffers.py`：per-bot ring buffer（默认 500），线程安全
    - [x] 9.2 启动时以 `priority=-10, name="webui:events"` 订阅 EventBus，推入 buffer
    - [x] 9.3 提供 `tail(since_id=None, limit=200) -> list[Event]` 与 `subscribe(cb) -> unsubscribe`

- [x] 10. 🔵 审计读模型
    - [x] 10.1 `audit_reader.py`：按 `linling` 主 spec 审计表结构做只读查询（若表未就绪则返回空 + 清晰 503 错误码）
    - [x] 10.2 搜索：time range / user / scope / bot / kind / outcome / 关键字
    - [x] 10.3 CSV 导出 ≤ 10000 行

---

## 阶段 W3 — 后端：业务 REST

- [x] 11. 🔵 事件
    - [x] 11.1 `GET /api/events`：分页 + filter
    - [x] 11.2 `GET /api/events/:id`：详情 + handler trace + agent invocation
    - [x] 11.3 `POST /api/events/:id/replay`：dry-run 重放，禁止实际发送 Action（适配器注入 `dry_run=True`）

- [x] 12. 🔵 KV
    - [x] 12.1 `GET /api/kv`：列 namespace
    - [x] 12.2 `GET /api/kv/:scope/:file`：分页 + 搜索 key prefix
    - [x] 12.3 `GET /api/kv/:scope/:file/:key` / `PATCH`（支持 `If-Match: <updated_at>` 乐观并发，不匹配 409）/ `DELETE`
    - [x] 12.4 `GET /api/kv/:scope/:file/rank`：order/top/sep/fmt 与 `$排行榜$` 对齐
    - [x] 12.5 测试：并发写冲突 409（**WUI-C5**）；写后读一致性（**WUI-C4**）

- [x] 13. 🔵 规则命中
    - [x] 13.1 `GET /api/rules`：handler + 今日命中统计（从审计聚合）
    - [x] 13.2 `GET /api/rules/:name/hits`：最近 N 次 hit（分页）

- [x] 14. 🔵 Agents
    - [x] 14.1 `GET /api/agents` / `GET /api/agents/:name`
    - [x] 14.2 `GET /api/agents/:name/memory`：短期窗口 + 长期向量摘要（不返回原始 embedding）
    - [x] 14.3 `POST /api/agents/:name/chat`：非流式试聊；会话上下文可带 `context.scope`
    - [x] 14.4 写审计：所有 agent 试聊留痕（**WUI-C9**）

- [x] 15. 🔵 设置 / 多 bot
    - [x] 15.1 `GET /api/bots`：可见 bot 列表（按 jwt.bots 过滤，**WUI-C2**）
    - [x] 15.2 `POST /api/bots/:bot_id/hot-reload`：回调 `linling` 主 spec Task 22 热加载接口
    - [x] 15.3 `GET /api/settings`：脱敏配置（api_key/token 返回 `***`，**WUI-C8**）
    - [x] 15.4 `GET /api/audit` / `GET /api/audit.csv`

---

## 阶段 W4 — 后端：WebSocket

- [x] 16. 🔵 `/ws/events`
    - [x] 16.1 握手：`?token=<jwt>` 校验；按 `bots` 过滤可见
    - [x] 16.2 协议：`{t:"filter"|"ping"|"event"|"filter_ack"}`；`since` 补发从 ring buffer 读
    - [x] 16.3 心跳：server 25s ping，client 15s 无消息回 ping；断连自愈
    - [x] 16.4 测试：1000 事件注入 + 重连后 event.id 单调（**WUI-C3**）；断连 10s 自动补发（**WUI-C10**）

- [x] 17. 🔵 `/ws/agents/:name/stream`
    - [x] 17.1 协议：`{t:"input"|"cancel"}` → `{t:"delta"|"tool_call"|"tool_result"|"done"|"error"}`
    - [x] 17.2 对接 `linling_agent` 流式接口；超时/取消写审计
    - [x] 17.3 测试：流式拼接、工具调用序列、中途 cancel

- [x] 18. 🔵 `/ws/rules/hits`
    - [x] 18.1 实时签文命中推送（订阅 audit + router trace）
    - [x] 18.2 测试：伪造 hit 写审计后 1s 内抵达

---

## 阶段 W5 — 前端：布局 & 全局

- [x] 19. 🟡 全局布局
    - [x] 19.1 `App.vue`：`<DecoBreezeLayer/>` + `<DecoPetalCanvas :density="prefs.decor"/>` + `<router-view/>`
    - [x] 19.2 `layouts/MobileShell.vue`：顶部标题栏（铃铛通知）+ `<main>` + 底部 5 槽 tab
    - [x] 19.3 `layouts/DesktopShell.vue`：左侧 240px 侧栏 + 主区（≥ lg 断点启用）
    - [x] 19.4 `layouts/MoreDrawer.vue`：右上抽屉，放 红娘司/命格/绳结/深浅色/装饰开关/登出
    - [x] 19.5 底部 tab active 态：下方红线 `--motion-thread-draw` 绘出

- [x] 20. 🟡 通用 composables
    - [x] 20.1 `api/client.ts`：axios + interceptor（401 自动 refresh 一次，再败跳 `/login?next=`）
    - [x] 20.2 `api/ws.ts`：`useEventStream / useAgentStream / useRuleHits`，自动重连 + 心跳 + since 补发
    - [x] 20.3 `store/auth.ts` / `store/prefs.ts`（localStorage persist：theme/decor/sound）
    - [x] 20.4 `router.ts` 守卫：未登录重定向登录

---

## 阶段 W6 — 前端：页面（移动优先）

- [x] 21. 🟡 缘起（Login）`pages/Login.vue`
    - [x] 21.1 Logo（铃铛+红线）+ 用户名 + 密码 + `UiButton kind="primary"`
    - [x] 21.2 错误：抖动 + 单次铃响（尊重 `prefers-reduced-motion`）
    - [x] 21.3 成功：跳 `next` 或 `/`

- [x] 22. 🟡 灵签（Dashboard）`pages/Dashboard.vue`
    - [x] 22.1 Hero `<DecoSorrowTree bells="2"/>`
    - [x] 22.2 2×2 卡片：今日事件 + sparkline、规则 Top3 条形、Agent token 金额、系统健康灯
    - [x] 22.3 三段列表：近期签文命中 / 灵玉阁热键 / 红娘近话（各自跳对应路由）
    - [x] 22.4 数字滚动缓动（`--motion-fade-in-up`）

- [x] 23. 🟡 因缘簿（Events）`pages/Events.vue`
    - [x] 23.1 `UiVirtualList` + `LingEventCard`
    - [x] 23.2 顶部 `UiChip` filter：kind/platform/bot/scope/命中与否
    - [x] 23.3 `UiPullRefresh` 触发顶部 swing
    - [x] 23.4 WS 断开 banner "红线松了 · 点此重连"
    - [x] 23.5 展开：segments / raw / 回溯按钮（调 `/replay`）
    - [x] 23.6 性能：10k 注入后内存增长 < 20MB（Playwright + CDP，**WUI-C6**）

- [x] 24. 🟡 灵玉阁（KV）`pages/Kv.vue`
    - [x] 24.1 namespace 面包屑 + scope/file 折叠树（`LingKvTree`）
    - [x] 24.2 主区 `LingKvRow` 虚拟列表
    - [x] 24.3 长按行 → bottom sheet 操作（编辑/删除/复制键/排行榜）
    - [x] 24.4 `LingKvEditor`：类型切换（string/number/json），JSON 走 Monaco lite 懒加载
    - [x] 24.5 乐观更新 + 失败回滚；409 冲突 sheet 顶部红色 banner + 重载按钮
    - [x] 24.6 排行榜视图：彩色条形 + 格式串输入

- [x] 25. 🟡 签文（Rules）`pages/Rules.vue`
    - [x] 25.1 `LingRuleCard` 列表：正则 / 今日命中 / 平均延迟 / 最近错误
    - [x] 25.2 点开详情抽屉：最近 50 次 hit + 回溯按钮
    - [x] 25.3 lint 警告：右上 `⚠` 铃铛金 pill，tap 展开详情

- [x] 26. 🟡 红娘司（Agents）`pages/Agents.vue` + `pages/AgentDetail.vue`
    - [x] 26.1 列表卡：头像 / name / model / 今日 token / 延迟
    - [x] 26.2 详情：三标签 试聊 / 记忆 / 日志
    - [x] 26.3 `LingAgentChat`：气泡对话（我方 thread 描边 + 幻粉填充），流式 delta 拼接；tool_call collapsible 卡
    - [x] 26.4 `LingAgentMemory`：短期窗口列表 + 长期条目（按 namespace 分组）
    - [x] 26.5 取消按钮：中途 `{t:"cancel"}` 并清理流式状态

- [x] 27. 🟡 命格（Audit）`pages/Audit.vue`
    - [x] 27.1 搜索条 + 虚拟列表
    - [x] 27.2 `vue-json-pretty` 详情（主题覆写）
    - [x] 27.3 CSV 导出按钮（≤ 1 万行同步下载）

- [x] 28. 🟡 绳结（Settings）`pages/Settings.vue`
    - [x] 28.1 Bot 列表 + 在线灯 + 热加载按钮
    - [x] 28.2 HTTP 白名单（只读）
    - [x] 28.3 装饰层 full/subtle/off 单选 + 主题（跟随/月白/暮紫）+ 铃铛音效开关
    - [x] 28.4 关于：版本号（来自 `/api/health.version`）

---

## 阶段 W7 — 移动适配细节

- [x] 29. 🟡 安全区 & 键盘
    - [x] 29.1 Tailwind 插件：`pt-safe / pb-safe / px-safe` 映射 `env(safe-area-inset-*)`
    - [x] 29.2 输入框 focus 自动 `scrollIntoView({block:"center"})`
    - [x] 29.3 底部 tab 背景延伸到 safe area 外，内容使用 `pb-safe`

- [x] 30. 🟡 手势
    - [x] 30.1 `UiSheet` drag-down 关闭（手势 > 80px）
    - [x] 30.2 `UiPullRefresh` 在 Events/Kv/Rules 接入
    - [x] 30.3 左滑删除（Audit 书签）二级确认

- [x] 31. 🟡 图像 & 资源
    - [x] 31.1 所有 `<img>` 加 `loading="lazy" decoding="async"` + `srcset`
    - [x] 31.2 SVG 插画统一放 `assets/`；tree/bell/petal 可用 symbol 复用

- [x] 32. 🟡 震感与铃音
    - [x] 32.1 `useHaptics()`：关键交互 15ms 震感，受 `prefs.haptics` 控制
    - [x] 32.2 `useBellSound()`：可选轻响，默认关

---

## 阶段 W8 — 性能 / 可访问性 / 安全

- [x] 33. ⚪ 性能预算
    - [x] 33.1 路由代码分割（每页 chunk）
    - [x] 33.2 装饰层延迟挂载（`requestIdleCallback`）
    - [x] 33.3 首屏 JS gzip ≤ 180KB（CI 失败若超）
    - [x] 33.4 Lighthouse mobile（Pixel 6 模拟）FCP < 1.5s / TTI < 3.0s

- [x] 34. ⚪ 可访问性
    - [x] 34.1 ARIA landmark、`aria-hidden` on decor、`aria-live="polite"` on 流式 bubble
    - [x] 34.2 axe-playwright 全页扫描 AA（**WUI-C12**）
    - [x] 34.3 键盘路径：Tab / Esc / Enter / `/` 聚焦搜索

- [x] 35. ⚪ 安全
    - [x] 35.1 CSP 头单测（**WUI-C13**）
    - [x] 35.2 设置脱敏（**WUI-C8**）
    - [x] 35.3 写接口 rate-limit 60 次/分钟/用户
    - [x] 35.4 Redactor：日志/错误响应不回显 JWT / 密码 / api_key

---

## 阶段 W9 — 测试 & 联调

- [x] 36. 🟢 后端单测 (pytest)
    - [x] 36.1 auth / kv / events / agents / ws_events / ws_agents 基本回归
    - [x] 36.2 多租户隔离（**WUI-C2**）
    - [x] 36.3 事件有序性 + 补发（**WUI-C3**）
    - [x] 36.4 KV 原子性/并发冲突（**WUI-C4/C5**）

- [x] 37. 🟢 前端单测 (vitest)
    - [x] 37.1 组件 snapshot（light/dark）
    - [x] 37.2 reduced-motion 下装饰组件不渲染（**WUI-C7**）
    - [x] 37.3 stores：auth refresh、events ring buffer、kv 乐观更新回滚

- [x] 38. 🟢 e2e (Playwright, mobile viewport 375×812)
    - [x] 38.1 登录 → 灵签 → 因缘簿 tail → KV 编辑 → Agent 试聊全流程
    - [x] 38.2 断网 10s 后 `/ws/events` 自愈（**WUI-C10**）
    - [x] 38.3 tap target 扫描（**WUI-C11**）
    - [x] 38.4 axe 扫描（**WUI-C12**）

- [x] 39. 🟢 联调主仓
    - [x] 39.1 在 `bot/bot.yaml` 添加 `webui:` 段并连通
    - [x] 39.2 `linling serve webui --bot bot/bot.yaml` 本地端到端跑通
    - [x] 39.3 README：部署 / 首登 / 改密码 / 反代

---

## 阶段 W10 — 发布

- [x] 40. ⚪ 打包
    - [x] 40.1 前端 `pnpm build` 产物拷入 `packages/webui/src/linling_webui/static/`
    - [x] 40.2 Python wheel 打包包含 `static/`
    - [x] 40.3 Docker：基础镜像 + 前端构建产物（多阶段构建）

- [x] 41. ⚪ 文档
    - [x] 41.1 `packages/webui/README.md`：功能 / 截图（移动+桌面）/ 配置
    - [x] 41.2 `docs/webui/theme.md`：主题 token 表 + 装饰开关说明
    - [x] 41.3 `docs/webui/api.md`：REST + WS 契约（可从 FastAPI OpenAPI 生成）
    - [x] 41.4 `docs/webui/a11y.md`：可访问性承诺与已知限制

- [x] 42. ⚪ 主仓任务联动
    - [x] 42.1 把主 `linling/tasks.md` 的 `21.3 最小 Web UI` 替换为"见 `linling-webui` spec"
    - [x] 42.2 明确 21.1 Prometheus / 21.2 审计表为 WebUI 的上游依赖
    - [x] 42.3 PWA manifest（Q-1）列为 v0.1 尾声；SW 留待 v0.2

---

## 里程碑

- [x] **M-W1**（W0~W2 完成）：能登录、看到 `/api/health`、前端壳子跑起来、主题可切。
- [x] **M-W2**（W3~W4 完成）：REST + WS 全通，mock 数据下所有页面可点。
- [x] **M-W3**（W5~W7 完成）：真实 bot 对接，移动端可用。
- [x] **M-W4**（W8~W10 完成）：性能 / a11y / 安全达标，发版本 `linling-webui 0.1.0`。

---

## 正确性清单（Design §14 对照）

- [x] WUI-C1  鉴权完整性（后端）— 任务 7、8、16、17、18
- [x] WUI-C2  多租户隔离 — 任务 15.1、36.2
- [x] WUI-C3  事件有序性 — 任务 16、36.3
- [x] WUI-C4  KV 原子性 — 任务 12、36.4
- [x] WUI-C5  KV 乐观并发 — 任务 12.3、24.5
- [x] WUI-C6  事件流内存 — 任务 23.6
- [x] WUI-C7  动效降级 — 任务 5.7、37.2
- [x] WUI-C8  设置脱敏 — 任务 15.3、35.2
- [x] WUI-C9  审计完整 — 任务 14.4、18
- [x] WUI-C10 WS 自愈 — 任务 16.3、38.2
- [x] WUI-C11 触达尺寸 — 任务 6.11、38.3
- [x] WUI-C12 对比度 — 任务 34.2、38.4
- [x] WUI-C13 CSP — 任务 8.3、35.1

---

## 进入实现前的 tiny-check

- [x] 主仓 `linling_agent` 是否已暴露"流式试聊"可编程入口？若无，先补一小步（设计阶段未计）。
- [x] 主仓 `linling_core` 的 EventBus 是否支持"订阅时指定 `replay_since`"？本 spec 用 WebUI 内部 ring buffer 规避，不阻塞。
- [x] 审计表（主 spec Task 21.2）schema 是否已定？未定则本 spec 内先用占位表，后续对齐。
