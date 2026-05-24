# 技术架构 / Technical Architecture

> 本文档描述 linling（铃）当前真实运行的技术架构。设计原则、未来路线图归
> [`.kiro/specs/linling/design.md`](../.kiro/specs/linling/design.md)；某个子系统
> 的细化设计（WebUI、Attention Probe、Action Ledger 等）归各自的 spec 目录。
> 本文是「索引 + 现实」：哪个目录做什么、组件怎么串、关键路径长什么样、最近
> 一轮改造改了什么。
>
> 阅读受众：第一次接触代码的工程师、想接入新平台 / 新工具的二次开发者、
> 排障时需要快速定位边界的运维。

最后更新：2026-05（commit `0b06045`，前端改版 + 像素图本地化 + 注意力探针）。

---

## 1. 总览：一张图看懂

```
┌────────────────────────────────────────────────────────────────────────┐
│                       Adapter Layer（Platform I/O）                     │
│   OneBot v11(QQ) · CLI(stdin) · WebUI Chat(WS)                          │
│          │ Event 入                          │ Action 出                │
│          ▼                                   ▲                         │
├──────────────────────────────────────────────┴─────────────────────────┤
│                          Kernel（linling_core）                         │
│   EventBus ──► Router ──► Classifier ─┬─► CommandDispatcher (DSL)       │
│        ▲                              ├─► ChatDispatcher  (Agent)       │
│        │                              └─► Builtin (help/reset/cancel)   │
│        │                                       │                         │
│   ConversationStore  ◄─ Session/History/Ledger │                         │
│   ToolRegistry       ◄─ python fn ↔ DSL ↔ LLM tool schemas               │
│   Audit / Metrics                              ▼                         │
│   ActionSink ◄────────────────── Action ◄──────┘                         │
├────────────────────────────────────────────────────────────────────────┤
│         DSL Engine                  │           Agent Engine             │
│   parser → AST → vm                 │   AgentRuntime (ReAct)             │
│   handler 体执行 + 工具调用         │   ContextManager (64k 预算 + 摘要)  │
│   $调用$/$jump$/$回调$              │   Group Batching + Attention Probe │
├──────────────────────────────────┬─────────────────────────────────────┤
│         LLM Providers            │             Storage                  │
│   OpenAI 兼容（OpenAI/Kimi/vLLM）│   SqliteKVStore  ScheduledTaskStore  │
│   Anthropic / Gemini（占位）      │   FileStore       AuditSink         │
└──────────────────────────────────┴─────────────────────────────────────┘
                                  │
                       ┌──────────┴──────────┐
                       │     linling-webui    │（独立包，贴上来）
                       │  FastAPI + Vue 3 SPA │
                       └─────────────────────┘
```

两条主路径共享 **ToolRegistry** 与 **Storage**：

* **指令路径**：Event → Router → Classifier 命中 DSL 触发器 → DSL VM → Action
* **聊天路径**：Event → Router → Classifier 走 fallback → Agent ReAct → Action

群聊兜底聊天再多一层「窗口聚合 + 关注规则 + 注意力探针」（见 §6）。

---

## 2. 目录与包关系

monorepo，pnpm + uv 双工作区。Python 包用 `uv sync --all-packages`，前端用
`pnpm install`。

```
linling/
├── packages/
│   ├── core/               ← linling_core         (kernel：events/router/storage/tools)
│   ├── dsl/                ← linling_dsl          (parser + ast + vm + linter + migrator)
│   ├── agent/              ← linling_agent        (runtime + memory + group_batch + probe)
│   ├── tools-stdlib/       ← linling_tools_stdlib (KV/HTTP/JSON/字符串/图文 等内置工具)
│   ├── adapters/
│   │   ├── onebot/         ← linling_adapter_onebot
│   │   └── cli/            ← linling_adapter_cli
│   ├── webui/              ← linling_webui        (FastAPI + Vue 3 SPA)
│   └── cli/                ← linling_cli          (linling 命令 + bootstrap)
├── bot/                    ← 真实部署的 bot 工程（涂山苏苏）
│   ├── bot.yaml
│   ├── agents/susu.yaml
│   ├── rules/*.ling
│   └── assets/picture/*    ← 本地像素图素材（@pic: 解析根）
├── docs/                   ← 公共开发文档（本目录）
├── scripts/                ← 一次性运维脚本（迁移、生成 SVG、审计图链等）
├── .kiro/specs/            ← 各子系统 spec（requirements/design/tasks）
└── tests/                  ← 跨包集成测试（gift_handlers 等场景级回归）
```

依赖方向（严格单向）：

```
adapters/* ──┐
             ├──► core ◄──── tools-stdlib
   dsl ─────►│
             │
   agent ───►│      webui ───► core / agent / dsl / cli
             │
             └──► cli (bootstrap, 唯一允许"看见所有人"的地方)
```

* `core` 不依赖任何业务包。
* `dsl` 与 `agent` 互相不依赖；它们都把逻辑落到 `core` 的 ToolRegistry 上。
* `cli/bootstrap.py` 是唯一组装层：把 yaml + .env → KV/EventBus/Router/
  ChatDispatcher/Adapters 一次性拼起来。其他模块禁止反向依赖 cli。
* `webui` 通过 `cli/wire_webui.py` 接到 `RunningBot`，不重写任何 kernel
  组件，只读取 + 暴露 HTTP/WS。

---

## 3. 核心数据模型

定义在 [`packages/core/src/linling_core/events.py`](../packages/core/src/linling_core/events.py)
和 [`segments.py`](../packages/core/src/linling_core/segments.py)。所有
adapter 入站消息都被翻译成这个模型，所有出站动作都从这个模型回写。

```python
class Scope(BaseModel):
    kind: Literal["group", "dm", "system"]
    id: str                # 群号 / 用户号 / "system"
    platform: str          # "onebot" | "cli" | "webui"
    channel_id: str | None # Discord thread / Feishu topic, 暂未启用

class Event(BaseModel):
    id: str                # 平台消息 id，去重用
    platform: str
    bot_id: str
    scope: Scope
    sender: User
    time: datetime
    kind: Literal["message", "notice", "request", "system"]
    segments: list[Segment]
    raw: dict[str, object] # 适配器原始载荷；DSL/Agent 不要碰

class Action(BaseModel):
    kind: Literal["reply","send","recall","mute","unmute",
                  "poke","set_title","kick","noop"]
    target: Scope
    segments: list[Segment]
    options: dict[str, object]
```

**Segment** 类型：`text / image / at / reply / face / file / card / poke /
voice / video / xml`。平台不支持的 segment 在 adapter 层降级或忽略并打
warning，不在 kernel 抛错。

**Event 上的两个视图属性**：

* `event.text` — 仅 plain text segment 拼起来；LLM/历史/审计用这个。
* `event.match_text` — 把 `@user_id` mention 重新拼回去；DSL trigger
  的 `赠送大飞龙@.*` 这种字面 `@` 触发器需要它。

---

## 4. 启动链：bot.yaml → RunningBot

所有装配都在 `packages/cli/src/linling_cli/bootstrap.py` 的
`bootstrap_bot()`。流程：

1. 读 `bot.yaml`，做 `${VAR:-default}` 展开（[`config.expand_env_recursive`](../packages/core/src/linling_core/config.py)）。
2. 打开存储：`SqliteKVStore`、`SchedulerStore`（默认 sqlite，可降级 memory）、
   `AuditSink`（同上）、文件目录。
3. 编译规则：`rules/**/*.ling` glob → `linling_dsl.parser.parse` → 单一
   `Script` AST。同时跑 linter，记下 warning/error 但不阻断启动。
4. 构造 `MessageClassifier`（命中 DSL 触发器走 command 路径，否则走 chat
   fallback）。
5. **构造 ChatDispatcher** —— 这是变化最多的一段：
   * 加载 `agents/*.yaml` → `AgentDef` → `AgentRuntime`（带 LLM provider）
   * 包一层 `ConversationContext` / `KVHistoryStore`（持久化历史，[history.py](../packages/agent/src/linling_agent/history.py)）
   * **群聊场景再包一层 `GroupBatchChatDispatcher`**（见 §6）
   * **可选再注入 `AttentionProbe`**（见 §7）
   * 最外层包一层 `_ScopeGatedChatDispatcher` 实现 `allowed_scopes` 群聊白名单
6. 构造适配器：`OneBotAdapter` 和/或 `CliAdapter`，并注册到 EventBus。
7. 装 `Router`：把 classifier、command/chat dispatcher、metrics、audit、
   action sink 连到一起。
8. 返回 `RunningBot`，调用方 `await running.start()` + `await running.wait()`。

`linling run bot/bot.yaml [--webui]` 走的就是这条路；`linling serve webui`
另起一个不调 `adapter.run()` 的轻装版本（仍能编辑 KV、调起 agent，但不会
收到 IM 消息，详见 README）。

---

## 5. 路由与分类

`linling_core.router.Router` 承担所有跨切面职责：去重、限流、超时、
trace_id、审计、指标、错误降级。

```
Adapter ─publish─► EventBus ─► Router.handle(event)
                                  │
                                  ├─ duplicate check (LRU on event.id)
                                  ├─ classify(event) → Intent
                                  │     · command(handler, captures)
                                  │     · chat
                                  │     · help / reset / cancel  (内置)
                                  │     · unknown_command         (前缀但无 handler)
                                  │     · ignore                  (黑名单)
                                  ├─ rate-limit (token bucket / session)
                                  ├─ ConversationStore.session_for(...)
                                  ├─ dispatch（有 timeout 控住）
                                  ├─ ActionSink ← Action[]
                                  └─ AuditSink ← AuditEntry  (kind/outcome/latency_ms)
```

**Classifier**（[classifier.py](../packages/core/src/linling_core/classifier.py)）：

* 默认前缀 `("/", "!")`，可配置；前缀消息必须命中某条 DSL 触发器，否则
  走 `unknown_command_reply`，**不喂给 LLM**（防止 `/start` 这种命令被当
  闲聊回）。
* 无前缀消息：试着按 DSL 触发器 fullmatch；命中 → command，否则 →
  chat（落到 LLM fallback）。
* 黑名单（`block_scope_ids`、`block_sender_ids`）在分类器内直接返回
  `ignore`，不经 dispatch。

**ConversationStore**（[pipeline.py](../packages/core/src/linling_core/pipeline.py)）：

* 内存 LRU，按 `(bot_id, scope_id, sender_id, intent_kind)` 分桶。
* 每个 Session 持有一个 `asyncio.Lock`，路由层 `session_timeout_s=10s`
  内拿不到锁就回 `busy_session_reply`，避免一个慢 LLM 调用把同人后续
  消息全部排在后面。
* `history` 是一个 deque，长度 `history_turns`（默认 16）；
  `KVHistoryStore` 把这条 deque mirror 到 KV，让对话跨进程重启存活。
* 可选挂 `Ledger`（DSL Action Ledger spec），把最近若干次 DSL 输出注入
  Agent 上下文，让 LLM 知道刚刚 DSL 回了什么。

---

## 6. 群聊兜底聊天的「窗口聚合」

> 一句话：群聊里每条消息都喂 LLM 太贵，所以攒一小段再让它**选择性**回。

实现：[`packages/agent/src/linling_agent/group_batch.py`](../packages/agent/src/linling_agent/group_batch.py)。
配置在 `bot.yaml` 的 `agent.group_batch_*` 块。

### 6.1 关键时序

```
ingest(event_1) ──► state.messages.append, attention_seen 用规则更新
ingest(event_2) ──► …                                       (run() 几乎零延迟返回)
ingest(event_3) ──► …
       │
       └─ 后台 _flush_loop:
              ├─ 每隔 window_s 醒一次
              ├─ 触发条件 (任一)
              │     · attention_seen=true                   (有人 @机器人 / 回复机器人 / 提到机器人名 / 是问句)
              │     · max_messages 超限
              │     · max_chars 超限
              │     · max_hold_s 到 ── 仍无关注 → 丢弃整批 (fail-closed)
              ├─ window_s 到点但没关注: 跑 AttentionProbe (§7)
              └─ flush ─► _dispatch_batch_with_tools(batch)
                              │
                              └── LLM 看到所有 batch 消息 + 三个工具:
                                    · read_batch_messages   (扩展可见消息)
                                    · reply_to_message      (按 message_id 选择性回)
                                    · send_group            (主动发，不带 reply)
```

### 6.2 关键不变式

* `state.lock` 只保护 *状态变量*；`probe.judge(...)` / `_dispatch_batch(...)`
  这些 I/O 调用都在 lock 外做，新消息可以照常 ingest。
* `_GroupState.generation` 每次 `clear_history` / 重置都自增；in-flight
  的 LLM 调用回来时若 generation 不匹配就静默丢弃（`_batch_is_current`），
  解决「我清空了对话，结果半秒前的 LLM 回到了」这类 race。
* 群聊白名单（`agent.allowed_scopes`）不影响 DM；DM 永远绕过白名单。

### 6.3 可调旋钮（生产实测）

```yaml
agent:
  group_batch_enabled: true
  group_batch_window_s: 8           # 第一次给关注规则的窗口
  group_batch_max_messages: 50      # 单批硬上限
  group_batch_max_chars: 6000
  group_batch_max_replies: 3        # 一次 batch 最多发 3 句，避免刷屏
  group_batch_max_reply_chars: 500
  group_batch_require_attention: true
  group_batch_max_hold_s: 300       # 探针 + 规则都没关注就丢
  group_batch_attention_probe_enabled: true
  group_batch_bot_names: [苏苏, 涂山苏苏]
```

---

## 7. 注意力探针（Lightweight Attention Probe）

> 第二阶段 yes/no LLM。规则没命中也不直接丢，再让一个轻量模型瞄一眼。

实现：[`packages/agent/src/linling_agent/attention_probe.py`](../packages/agent/src/linling_agent/attention_probe.py)。
完整设计：[`.kiro/specs/lightweight-attention-probe/`](../.kiro/specs/lightweight-attention-probe/)。

### 7.1 触发链

```
窗口到点 (elapsed >= window_s)
  ├─ require_attention=false      → 直接 flush（探针不参与）
  ├─ attention_seen=true（规则命中） → 直接 flush
  ├─ probe is None                  → 老行为：等到 max_hold_s 丢弃
  └─ probe wired:
        ├─ 取 batch 快照（lock 内）
        ├─ 标记 attention_probed=true（lock 内 — 限一次/lifecycle）
        ├─ 释放 lock，judge(snapshot)         ◄── HTTP 在锁外做
        ├─ True  → attention_seen=true → 下一轮 flush 走主 LLM
        └─ False / 异常 / 401 / malformed → 不动 attention_seen，继续走 max_hold_s 丢弃
```

### 7.2 配置链（env 优先）

| 字段 | 主 env | 回落 1 | 回落 2 | 默认 |
| --- | --- | --- | --- | --- |
| `api_key` | `ATTENTION_PROBE_API_KEY` | `OPENAI_API_KEY` | — | 空 → 静默关闭探针 |
| `base_url` | `ATTENTION_PROBE_BASE_URL` | `OPENAI_BASE_URL` | — | `https://api.openai.com/v1` |
| `model` | `ATTENTION_PROBE_MODEL` | 默认 agent 的 model | — | `gpt-4o-mini` |
| toggle | — | `agent.group_batch_attention_probe_enabled`（yaml） | — | `True` |

bootstrap 在 `_build_attention_probe()` 里做这一套解析。**没填 key 就静默
跳过，启动只打一条 info 日志**——这是 Requirement 3 的硬契约。

### 7.3 成本与失败预算

* `max_tokens=32`、`temperature=0.0`、`tools=None`、`timeout=8s`，所以单次
  探针调用是固定的 micro-cost。
* yes-token 集合：`{yes, y, true, 1, 是, 需要, 回复}`；其它一律视为 no。
* **fail-closed**：网络错 / 超时 / 401 / 解析失败 → 一律 verdict=False，
  打一条 warning，主 LLM 完全不被调用。一个 batch 内只允许探针调一次
  （`attention_probed` 在快照前置位）。

---

## 8. DSL 引擎

* 包：`linling_dsl`，纯 Python，无外部解析器依赖。
* 文件扩展名：`.ling`。一个文件 = 多个 handler，handler 之间用空行分隔。
* `parser` 切 handler 块 → AST → `vm` 执行体。`linter` 在 CI 里把
  `unused var / unreachable / 危险工具调用` 报出来。
* DSL 的 trigger 是一个 Python 正则；`(?i)`、字符类、捕获组、选择分支都
  原生支持。
* 工具调用 `$名 参数...$` 走 `ToolRegistry.get_by_dsl_name()`；同一个
  `@tool(...)` 装饰器的函数同时挂到 DSL 名（`读`、`写`、`图文` …）和
  LLM 工具 schema（[`tools.py`](../packages/core/src/linling_core/tools.py)）。
* DSL 沙箱：循环最大迭代、handler 输出段数上限、运行时异常捕获、
  HTTP/文件白名单（在工具实现里做）。
* 完整语法对照表（含与 QRSpeed 兼容性）：
  [`docs/dsl/grammar.md`](./dsl/grammar.md)。

`DslCommandDispatcher` 跑 DSL 时还会读两类外部上下文：

* **History/Ledger** — 让 DSL 可以把 LLM 历史插值到回复里（`%LLM上一句%` 之类）。
* **图片资产** — `±img=URL±` 经下游 dispatcher 重写为 `@pic:`、HTTP 链接
  或本地路径（见 §9）。

---

## 9. 图片素材本地化（2026-05 改造）

> 历史：`s1.ax1x.com` / `wkphoto.cdn.bcebos.com` 等图床的链接陆续失效，
> 一些规则掉到「文字回复但图缺失」。
>
> 现在：所有 `±img=...±` 引用通过 `@pic:NAME` 解析到仓库自带的本地素材。
> 涂山苏苏的角色形象用脚本生成的像素 SVG 兜底。

### 9.1 资产布局

```
bot/
├── bot.yaml
└── assets/
    └── picture/
        ├── 苏苏比心.svg        ← 程序生成的像素图
        ├── 思思.jpg            ← 角色立绘 / 道具图（jpg 原图）
        ├── 大飞龙.jpg
        └── ...
```

bootstrap 时 `_resolve_asset_root(base)` 把 `<base>/assets` 设为唯一可信
根目录；OneBot adapter 和 WebUI router 都用它做路径重写。

### 9.2 三层重写

```
DSL 输出:    ±img=@pic:思思±                                        (作者写的)
              │
              ▼
ImageSegment(url="@pic:思思")
              │
   ┌──────────┼─────────────────────────────────────────────┐
   │ OneBot 出口                                            │ WebUI 出口
   ▼                                                        ▼
file:///abs/path/to/bot/assets/picture/思思.jpg            /api/files/assets/picture/思思.jpg
   │                                                        │
   │ NapCat 直接读盘                                         │ 浏览器同源 fetch（CSP 友好）
```

* **扩展名 fallback**：`@pic:思思` 找不到时自动尝试 `思思.jpg` → `思思.svg`。
  `gen_susu_svgs.py` 替换的素材直接覆盖原 .jpg 路径而不破坏现有规则。
* **Path traversal 防护**：`@pic:../secret` 不解析、原样下传，OneBot 端
  自然忽略；router 端 404。
* 远程 `±img=https://...±` 走 `/api/files/proxy?url=...` 反代，避免在
  浏览器上需要放开 `img-src` 的 CSP 白名单。

### 9.3 工具脚本

| 脚本 | 作用 |
| --- | --- |
| [`scripts/gen_susu_svgs.py`](../scripts/gen_susu_svgs.py) + [`scripts/_susu_lib.py`](../scripts/_susu_lib.py) | 程序生成 38 张苏苏像素 SVG，统一发型 / 耳朵 / 比例。 |
| [`scripts/audit_image_urls.py`](../scripts/audit_image_urls.py) | 扫 `bot/rules/*.ling` 的所有图引用，分类为 remote-live / remote-dead / local-pic / templated。 |
| [`scripts/strip_dead_image_urls.py`](../scripts/strip_dead_image_urls.py) | 把 audit 标记 dead 的远程 URL 从规则里去掉，保留文字。 |

QRDic 的老 `data/picture` 现在仅作引用，不再下发，已加进 `.gitignore`。

---

## 10. 适配器

### 10.1 OneBot v11

[`packages/adapters/onebot/src/linling_adapter_onebot/adapter.py`](../packages/adapters/onebot/src/linling_adapter_onebot/adapter.py)

* 反向 / 正向 WS 都行，推荐反向（linling 主动连 NapCat）。
* 重连指数退避（5s → 60s 上限），401/403 立刻退出（不无限刷 token 错）。
* `call_api` 带 5s `echo` 超时，避免 NapCat 偶尔丢响应把整个 session 卡死
  （Router 的 `session_timeout_s` 是 10s，5 < 10 是有意的）。
* 把 `notice` / `request` 翻译成 QRSpeed 兼容的合成 message：`[系统]`
  `[退群]` `[上下管理]` `[戳一戳]`，让原 QRSpeed 规则原样工作。
* `@pic:` 解析见 §9。

### 10.2 CLI

[`packages/adapters/cli/`](../packages/adapters/cli/) — 把 stdin 行翻译成
DM event，stdout 渲染回复。开发联调和 spec 跑 dispatch 都用它。

### 10.3 WebUI Chat

不是传统适配器；通过 `wire_webui` 把 chat dispatcher 接到 WS 路由
`/ws/agents/:name/stream`，复用同一个 `ChatDispatcher` 实例。前端在
`ChatComposer` 里发的消息走的就是这条通路。

---

## 11. WebUI

独立包：`packages/webui`。FastAPI 后端 + Vue 3 SPA + Tailwind v4。

* 主题：狐妖小红娘 · 苦情树 / 铃铛 / 幻粉雾，[详见 spec](../.kiro/specs/linling-webui/design.md)。
* 鉴权：argon2id 密码 + JWT (access 15m / refresh 7d，refresh 落 sqlite)。
* 移动优先：iPhone 12 Mini → iPad Mini 都过设计；点击区 ≥ 44×44。
* 关键页：`Chat`（试聊 + 流式）、`Observatory`（事件流 / KV / Audit）、
  `Settings`（主题 / 装饰 / Bot 列表 / 热加载）、`Login`。
* `Observatory` 三个面板：`ObsEventsPane`（实时 WS）、`ObsKvPane`
  （KV 浏览 + 编辑 + ETag 乐观锁）、`ObsAuditPane`（审计搜索 + CSV）。
* 装饰层：`DecoBreezeLayer` / `DecoPetalCanvas` / `DecoBellAccent` 在
  `prefers-reduced-motion: reduce` 下硬退化为静态。

API 与契约对齐：
[`docs/api-types.md`](./api-types.md) — backend 改 schema 时跑 `pnpm
api:update` 同步 `openapi.snapshot.json` + `openapi.types.ts`，drift 在
pytest 与 `pnpm api:check` 双向阻断。

---

## 12. 配置体系

层级（高优先级覆盖低优先级）：

```
1. shell / 容器 export 的环境变量
2. .env （linling 启动时 load_dotenv override=False）
3. bot.yaml ${VAR:-default} 展开
4. yaml 字段本身 / 代码默认值
```

`expand_env_recursive` 是单一入口（[config.py](../packages/core/src/linling_core/config.py)），
不允许在其他模块再写一遍解析逻辑。常见 env：

| 环境变量 | 作用 |
| --- | --- |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | 主 LLM。被 agent yaml 用 `${...}` 引用 |
| `LINLING_MODEL` | 覆盖默认 model（agents/*.yaml 里 `${LINLING_MODEL:-gpt-4o-mini}`） |
| `ATTENTION_PROBE_*` | 探针单独走另一家 / 另一档模型，全空就跟主 LLM 同源 |
| `ONEBOT_WS_URL` / `ONEBOT_TOKEN` | OneBot 连接 |
| `LINLING_WEBUI_JWT_SECRET` | WebUI JWT 签名密钥（不填每次重启随机） |
| `LOG_LEVEL` / `LOG_FORMAT` | structlog 行为；prod 走 json，dev 可 console |
| `LINLING_SKIP_DOTENV=1` | 测试隔离：禁止 `.env` 注入 `os.environ` |

---

## 13. 可观测性

完整文档：[`docs/observability/`](./observability/)。

* **结构化日志（structlog）**：每次 dispatch 一个 `trace_id`，单条用户消息
  全链路同 id；把这个 id 喂进日志库直接复盘。
* **Audit**：`AuditSink` 写 sqlite，每行 `{kind, outcome, latency_ms,
  bot_id, user_id, scope_id, payload}`。WebUI `/api/audit` 直接读。
* **Prometheus**：`/metrics` 端点，11 项指标（events / dispatch_duration /
  llm_calls / llm_tokens / sink_failures / active_sessions ...）。标签
  低基数（不带 user/scope id），可放心 scrape。
* **关键 trace 锚点**：
  * `Router._dispatch` — kind / outcome / latency
  * `AgentRuntime.invoke` — provider / model / token / wall_clock
  * `GroupBatchChatDispatcher._flush_loop` — `attention_seen`、
    `attention_probed`、batch 大小、verdict 来源
  * `AttentionProbe.judge` — verdict / failure_category

---

## 14. 安全 / 沙箱

* DSL 跑在 asyncio task 里，不开 OS 沙箱；防御靠：
  * 工具白名单（`@tool(safe=True/False)`，写类工具要 handler 显式权限声明）
  * HTTP 白名单（`bot.yaml.guardrails.http_allowlist`）
  * 输出段数 / 循环步数上限
  * 运行时异常被 dispatcher 捕获，不向用户暴露 stacktrace
* LLM provider：所有 OpenAI 兼容端点走 `OpenAIProvider`，httpx + 8s
  timeout（探针）/ 30s timeout（主 agent）。鉴权失败一律 fail-closed。
* WebUI：`Content-Security-Policy default-src 'self'; frame-ancestors
  'none'`；`/api/settings` 含 `secret/token/password` 的字段输出 `***`；
  写接口 60/min，登录 5/min/IP。
* `bot/assets/picture` 之外的路径不响应，path traversal 在 OneBot adapter
  和 WebUI router 都 reject。

---

## 15. 已知边界 / 待办

* **多租户**：当前单 bot 实例就是一个进程；`bot_id` 已经在 KV/Audit 里
  分了区，可平滑扩到多 bot，但调度/适配器仍是单实例视角，参见
  `agent-platform` spec。
* **非 OpenAI 兼容 provider**：Anthropic / Gemini 占位在 `.env.example`
  里，但 `linling_agent.providers/` 暂未实装；要接得新增 provider class。
* **WeChat / Feishu / Discord** 适配器：spec 定了能力矩阵（[`linling/design.md` §7.2](../.kiro/specs/linling/design.md)），
  仓库里没有实装。
* **DSL `否则 / elseif`**：明确放弃，遵循 QRSpeed 原版习惯（用并列
  `如果:` 模拟）。
* **PWA / Service Worker**：WebUI v0 只装 manifest，SW 留 v0.2。

---

## 16. 索引：去哪找什么

* 顶层使用文档 → [`README.md`](../README.md)
* 高层设计 / 路线图 → [`.kiro/specs/linling/design.md`](../.kiro/specs/linling/design.md)
* 子系统设计：
  * WebUI → [`.kiro/specs/linling-webui/design.md`](../.kiro/specs/linling-webui/design.md)
  * Attention Probe → [`.kiro/specs/lightweight-attention-probe/design.md`](../.kiro/specs/lightweight-attention-probe/design.md)
  * DSL Action Ledger → [`.kiro/specs/dsl-action-ledger/`](../.kiro/specs/dsl-action-ledger/)
* DSL 语法 → [`docs/dsl/grammar.md`](./dsl/grammar.md)
* WebUI 主题 / API / a11y → [`docs/webui/`](./webui/)
* 部署 → [`docs/deployment/napcat.md`](./deployment/napcat.md)
* 可观测 → [`docs/observability/`](./observability/)
* OpenAPI 同步 → [`docs/api-types.md`](./api-types.md)
