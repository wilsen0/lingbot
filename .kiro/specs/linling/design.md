# Design — linling（对话智能体平台）

## 0. 总览

```
┌──────────────────────────────────────────────────────────────────────┐
│                          Platform Adapters                            │
│   OneBot(QQ)    WeChat    Feishu    Discord    HTTP    CLI            │
└───────────────▲──────────────────────────────────────▲────────────────┘
                │ 统一 Event/Action                       │
┌───────────────┴──────────────────────────────────────┴────────────────┐
│                           Kernel                                       │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────────────┐   │
│  │  Router     │→ │  Pipeline    │→ │   Tool Registry (共用)      │   │
│  │ (priority)  │  │ (middleware) │  │  python fn ↔ DSL ↔ LLM     │   │
│  └─────────────┘  └──────────────┘  └────────────────────────────┘   │
│         │                │                       ▲                    │
│         ▼                ▼                       │                    │
│  ┌─────────────┐  ┌──────────────┐      ┌────────┴─────────┐          │
│  │ DSL Engine  │  │ Agent Engine │──────│  LLM Providers   │          │
│  │ (parser+vm) │  │ (react/tools)│      │ OpenAI/Claude/…  │          │
│  └─────────────┘  └──────────────┘      └──────────────────┘          │
│         │                │                                             │
│         ▼                ▼                                             │
│  ┌──────────────────────────────────────────────────────────┐         │
│  │  Storage: KV / SQL / Vector / Files / Scheduler          │         │
│  └──────────────────────────────────────────────────────────┘         │
└────────────────────────────────────────────────────────────────────────┘
```

两条主路径共享 **Tool Registry** 和 **Storage**：
- 指令路径：Event → Router → DSL Engine → Actions
- Agent 路径：Event → Router → Agent Engine → (LLM ↔ Tools) → Actions

## 1. 语言与项目结构

- 语言：**Python 3.11+**（async、dataclasses、pattern matching）。
- 包管理：`uv`（或 `poetry`）。
- 分层（monorepo）：

```
linling/
├── pyproject.toml
├── packages/
│   ├── core/              # 内核：事件、工具注册表、路由、存储、调度
│   │   └── src/linling_core/
│   ├── dsl/               # DSL 解析器 + 虚拟机 + 迁移器
│   │   └── src/linling_dsl/
│   ├── agent/             # Agent 框架：LLM 抽象、记忆、工具调用
│   │   └── src/linling_agent/
│   ├── adapters/
│   │   ├── onebot/        # QQ 适配器（OneBot v11 优先）
│   │   ├── wechat/        # 预留
│   │   ├── feishu/        # 预留
│   │   ├── http/          # 通用 webhook
│   │   └── cli/           # 本地终端调试
│   ├── tools-stdlib/      # 官方工具集（KV、HTTP、图文、定时…）
│   └── cli/               # `linling` 命令行：run / migrate / lint / repl
├── scripts/
│   ├── migrate_qrdic.py   # QRDic → 新结构的一键迁移
│   └── seed.py
└── examples/
    ├── susu/              # 按原 QRDic 业务复刻的样例配置
    │   ├── bot.yaml
    │   ├── rules/*.ling   # 新 DSL 文件扩展名 .ling
    │   └── agents/*.yaml
    └── minimal/
```

扩展名选 `.ling`（与项目名一致，规则脚本 = linling rule）。

## 2. 统一消息模型（Event / Action）

```python
# packages/core/src/linling_core/events.py
class Event(BaseModel):
    id: str                        # 平台消息 ID
    platform: str                  # "onebot" | "wechat" | ...
    bot_id: str                    # 我方账号
    scope: Scope                   # (group/channel/dm)
    sender: User
    time: datetime
    kind: Literal["message","notice","request"]
    segments: list[Segment]        # 多段：text/image/at/reply/face/file/poke/card
    raw: dict                      # 保留原始载荷（仅适配器可读）

class Action(BaseModel):
    kind: Literal["reply","send","recall","mute","poke","set_title",
                  "http","call_adapter","log"]
    target: Scope
    segments: list[Segment] = []
    options: dict = {}             # 平台特定参数
```

- **Segment 类型**：`text / image(url|local|b64) / at(user) / reply(msgid) / face(id) / file / card / poke / voice / video / xml`。
- **平台不支持的 Segment 在适配器里**：要么降级（image→文字描述），要么忽略并打 warning 指标。

## 3. 路由与流水线

- **路由表**：由所有 rule 文件和 agent 文件编译而来。
  - Rule 触发器：正则 + 可选范围（群/用户/时间）+ 优先级。
  - Agent 触发器：`mention` / `keyword` / `fallback` / `always`。
- **匹配策略**：按 `priority` 降序首匹配；同 priority 下允许多处理器并行。
- **Pipeline 中间件**：`rate_limit → auth → input_sanitize → handler → output_filter → audit_log`，每个都能短路。

## 4. DSL 语法规范（正式定义）

### 4.1 设计原则

1. 向后兼容 QRDic 高频语法（中文关键字、`$func ...$`、`%var%`、`[expr]`、`如果/如果尾/返回`、`:label`、`[内部]`）。
2. 去掉 undocumented 行为，明确错误处理与沙箱。
3. 扩展：`导入`、函数定义 `函数 名(参数)`、作用域、类型提示（可选）、异步 `等待`。

### 4.2 词法

- 编码：UTF-8。
- 行结束：`\n`；连续空行结束上一个处理器体。
- 注释：`//` 行注释；`/* ... */` 块注释。
- 标识符：中文、英文、数字、下划线；不能以数字开头。
- 字符串：裸写（直到行尾或下一个 `$`/`%`）；也支持 `"..."` 引号字符串，内部可 `\n \t \\ \" \xNN \uNNNN`。
- 数字：十进制整数 / 小数；表达式里按需转换。
- 保留关键字：`如果 如果尾 正则 返回 完成 函数 返回值 等待 导入 当 否则 否则如 循环 中断 继续`。

### 4.3 EBNF（片段）

```ebnf
script        = { top_decl } ;
top_decl      = handler_decl | func_decl | import_decl | config_decl ;
import_decl   = "导入" STRING ;
config_decl   = "&&" TEXT_TO_EOL ;               (* 注释式配置 *)
handler_decl  = [ "[内部]" ] trigger NEWLINE block ;
trigger       = REGEX [ " " attr_list ]? ;       (* 顶格的正则即触发器 *)
attr_list     = "{" key "=" value { "," key "=" value } "}" ;
func_decl     = "函数" IDENT "(" [ param_list ] ")" NEWLINE block "函数尾" ;
block         = { stmt } ;
stmt          = assign | if_stmt | return_stmt | label | jump
              | call_stmt | output_stmt | loop_stmt ;
assign        = IDENT ":" expr ;                 (* 局部变量 *)
if_stmt       = ("如果" | "正则") ":" cond NEWLINE block
                { "否则如" ":" cond NEWLINE block }
                [ "否则" NEWLINE block ] "如果尾" ;
return_stmt   = "返回" [ expr ] | "完成" ;
label         = ":" IDENT ;
jump          = "$jump" ":" IDENT "$" ;
call_stmt     = "$" IDENT { " " expr } "$" ;     (* 内置或自定义函数 *)
output_stmt   = text_line | image_line ;
image_line    = "±img=" expr "±" ;
text_line     = ANY_OTHER_TEXT_WITH_INTERP ;
expr          = interp | arith | literal | ref ;
arith         = "[" arith_inner "]" ;
ref           = "%" IDENT "%" ;
cond          = expr cmp_op expr { ("&" | "|") expr cmp_op expr } ;
```

### 4.4 语义要点

- **输出**：handler 体内所有 **非指令非控制流的文本行** 视为一次回复的累积文本；遇到 `返回/完成` 或 handler 结束时作为一条消息发出。多段可用空行分段，或显式 `$发送$`。
- **`±img=...±`** 作为一个 image segment 插入当前回复。
- **`%var%`**：先查当前 handler 局部变量 → 处理器参数 → 事件上下文 → 全局 → KV 查询（只读）。
- **算术 `[expr]`**：四则、比较、字符串拼接（用 `+`）、数组索引 `[arr,i]`。
- **`$name args...$`**：内置函数 / 注册工具 / 其他 handler（用 `$调用 0 处理器名 ...$` 或直接 `$handler名 ...$`）。
- **`$jump :label$`**：同一 handler 内跳转。
- **`$调用 ms 处理器名 ...$`**：异步延时调度，返回 task id。
- **`$回调 处理器名 ...$`**：同步调用另一个 handler，带返回值。
- **`返回 expr`**：返回值；不带值等同于空串。
- **错误处理**：运行时异常以 handler 为单位捕获，写审计日志，**默认不向终端用户暴露**（可配置调试模式）。
- **沙箱**：
  - 单次处理器 CPU 超时（默认 2s）
  - 单次处理器输出段数上限（默认 20）
  - 循环最大迭代次数（默认 10k）
  - 文件/HTTP 调用必须走注册函数，禁止任意路径 / URL。

### 4.5 标准内置函数（选摘；完整列表见 appendix）

| DSL 名 | 等价 Python | 说明 |
|---|---|---|
| `$读 scope/file key default$` | `kv.read(scope, file, key, default)` | 兼容老 `$读$` |
| `$写 scope/file key value$` | `kv.write(scope, file, key, value)` |  |
| `$删除 path$` | `kv.delete(path)` | path 必须在白名单 |
| `$排行榜 scope/file order topN sep fmt$` | `kv.rank(...)` |  |
| `$发送 好友/群 msg target text$` | `actions.send(...)` |  |
| `$撤回 scope msg_id$` | `actions.recall(...)` |  |
| `$访问 url$` / `$访问 POST url body$` | `http.request(...)` | 走白名单 / 超时 / 缓存 |
| `$JSON 长度 var$` / `$JSON 获取 var path$` / `$JSON 添加 var value$` | `json_ops.*` |  |
| `$随机数 a-b$` / `$概率随机 weights values$` | `random_ops.*` |  |
| `$URLEncoder$ / $URLDecoder$ / $Base64Decoder$ / $HexEncoder$ / $HexDecoder$ / $UnicodeDecoder$` | `codec.*` |  |
| `$替换 sep text @from@to$` | `str_ops.replace(...)` |  |
| `$正则 sep text pattern$` | `str_ops.regex(...)` |  |
| `$群昵称 group qq$` / `$群头衔 group qq title$` / `$获取群成员 group$` | adapter RPC |  |
| `$BSH file method args$` | `bsh.call(...)` | 兼容层；内部改调 Python 实现 |
| `$agent 调用 name input$` | `agent.invoke(...)` | **新增**：桥接 Agent |
| `$agent 流式 name input$` | `agent.stream(...)` | **新增** |
| `$全局变量 k v$` / `$取变量 k$` | `globals.*` | 进程内，可持久化 |
| `$调用 ms handler args...$` | `scheduler.delay(...)` |  |
| `$回调 handler args...$` | `call_handler(sync=True)` |  |

### 4.6 上下文变量（`%名%`）

由事件填充，只读：

| 变量 | 来源 |
|---|---|
| `%QQ%` `%用户%` | 发送者 uid（平台中立时用 `%用户%`） |
| `%群号%` `%群%` `%会话%` | 群号 / 会话 ID |
| `%昵称%` | 群昵称或平台昵称 |
| `%Robot%` `%自己%` | 机器人 id |
| `%Code%` | 被戳对象或事件主体 |
| `%AT0%..%ATn%` | 消息中的 @ 列表 |
| `%括号1%..%括号n%` | 触发正则的捕获组 |
| `%参数-1%` | 整条原文（不含 bot 指令前缀） |
| `%IMG0%..%IMGn%` | 附带图片 URL/ID |
| `%Json%` `%Msgbar%` `%Reqid%` `%Type%` | 平台原始字段，适配器映射 |
| `%时间yyyy/MM/dd/HH/mm/ss%` | 组合任意时间格式 |
| `%管理员%` `%主群%` | 配置派生 |

## 5. Tool Registry（DSL ↔ Python ↔ LLM 同源）

```python
# packages/core/src/linling_core/tools.py
@tool(
    name="read_kv",                     # LLM 名
    dsl_name="读",                      # DSL 名（带 $$）
    description="读取一个键值；默认值在未命中时返回",
    schema={
        "scope": "string", "file": "string",
        "key": "string", "default": "any?"
    },
    safe=True,
)
async def read_kv(ctx: ToolCtx, scope: str, file: str, key: str, default=None):
    return await ctx.kv.read(scope, file, key, default)
```

- 一次注册，三种视图：
  - **Python**：直接调用或在其他插件里 `ctx.tools.read_kv(...)`。
  - **DSL**：`$读 scope/file key default$` 被解析成对 `read_kv` 的调用（参数映射表由 registry 提供）。
  - **LLM tool**：自动生成 JSON schema，Agent 初始化时挂到 LLM。
- `safe=True` 的工具允许在 DSL 沙箱里直接用；`safe=False`（例如 `send`、`recall`、`mute`）需要 handler 权限声明。

## 6. Agent 框架

### 6.1 定义

```yaml
# bot/agents/susu.yaml
name: susu
provider: openai          # openai | anthropic | gemini | openai_compatible
model: gpt-4o-mini
system: |
  你叫涂山苏苏…（此处接原 prompt）
tools:
  - read_kv
  - write_kv
  - show_bag           # 直接暴露某个 DSL 处理器
memory:
  kind: sliding_window
  turns: 8
  long_term:
    kind: vector
    store: qdrant
    collection: susu_${bot_id}
triggers:
  - kind: mention        # @机器人
  - kind: dm             # 私聊
  - kind: keyword
    patterns: ["问(.+)", "(.*)怎么说"]
guardrails:
  max_tool_calls: 6
  max_tokens: 1200
  timeout_s: 20
```

### 6.2 执行循环

- 标准 ReAct / tool-calling 循环：
  1. 构建 context = system + persona + memory + user input + (可选) recent dsl state summary
  2. 调 LLM
  3. 若返回 tool_call：
     - 校验工具白名单
     - 沙箱执行（复用 DSL 沙箱策略）
     - 结果回注，继续循环
  4. 终止条件：文本输出 / 达到 `max_tool_calls` / 超时

### 6.3 记忆

- **短期**：`sliding_window(turns)` 直接进 messages。
- **长期**：向量库插件化（默认 `sqlite-vss` 本地，生产 `qdrant/pgvector`）；按 `user × scope` 分 namespace。
- **KV 记忆**：Agent 可以读 DSL 的 KV（例如"灵玉数量"），通过 `read_kv` 工具。

### 6.4 多提供方

- 抽象 `LLMProvider`：`chat(messages, tools, stream) → AsyncIterator[Delta]`
- 统一 token 计量接口，便于计费和限流。
- 允许 per-Agent 选 provider/model。

## 7. 平台适配器

### 7.1 接口

```python
class PlatformAdapter(Protocol):
    name: str
    capabilities: Capabilities           # 支持哪些 Segment/Action

    async def run(self, bus: EventBus) -> None: ...
    async def send(self, action: Action) -> SendResult: ...
    async def rpc(self, name: str, **kw) -> Any: ...   # 平台特定 RPC
```

### 7.2 能力矩阵（初版）

| 能力 | OneBot(QQ) | WeChat(iPad协议) | Feishu | Discord | CLI |
|---|---|---|---|---|---|
| text | ✅ | ✅ | ✅ | ✅ | ✅ |
| image | ✅ | ✅ | ✅ | ✅ | 本地预览 |
| at | ✅ | ✅ | ✅ | ✅ | 模拟 |
| reply | ✅ | ⚠️ | ✅ | ✅ | — |
| recall | ✅ | ⚠️ | ✅ | ✅ | — |
| mute(set_mute) | ✅(群管权限) | ❌ | ❌ | ✅ | — |
| poke | ✅ | ❌ | ❌ | ❌ | — |
| card/xml | ✅ | ❌ | ❌ | ⚠️ | — |
| set_title | ✅ | ❌ | ❌ | ❌ | — |
| file upload | ✅ | ✅ | ✅ | ✅ | 写磁盘 |

不支持的能力：适配器在 `send` 时报 `UnsupportedActionError`，Pipeline 有降级策略。

### 7.3 OneBot 适配器（MVP）

- WebSocket 连接 OneBot v11 impl（LLBot/Lagrange）。
- 正向 or 反向 WS 都支持；推荐反向（机器人连 OneBot）。
- 消息解析：把 `message[]` 映射为 `Segment[]`；CQ 码反解同 segments 结构。
- 发送：`Segment[]` 组装为 OneBot `message`；`reply` 追加 `reply` segment。

## 8. 存储

### 8.1 KV 抽象

```python
class KVStore(Protocol):
    async def read(self, scope: str, file: str, key: str, default=None) -> str | None
    async def write(self, scope: str, file: str, key: str, value) -> None
    async def delete(self, scope: str, file: str | None, key: str | None) -> None
    async def rank(self, scope: str, file: str, order: Literal["asc","desc"],
                   top: int, sep: str, fmt: str) -> str
```

- 默认 SQLite 后端：表结构

```sql
CREATE TABLE kv (
  bot_id      TEXT NOT NULL,
  scope       TEXT NOT NULL,   -- 例如 "啊/灵玉系"
  file        TEXT NOT NULL,   -- 例如 "灵玉"
  key         TEXT NOT NULL,   -- 例如 QQ 号
  value       TEXT NOT NULL,
  updated_at  INTEGER NOT NULL,
  PRIMARY KEY (bot_id, scope, file, key)
);
CREATE INDEX kv_rank ON kv(bot_id, scope, file, CAST(value AS REAL));
```

- Postgres 后端用相同 schema，`value` 用 `text`；`$排行榜$` 用窗口函数。

### 8.2 调度器

- 任务表：
```sql
CREATE TABLE scheduled_task (
  id TEXT PRIMARY KEY,
  bot_id TEXT, scope TEXT, handler TEXT,
  args_json TEXT, fire_at INTEGER, state TEXT
);
```
- worker 轮询 + apscheduler 可选。

### 8.3 文件存储

- 抽象 `FileStore`：本地目录 / S3 兼容。

### 8.4 向量存储

- 抽象 `VectorStore`：sqlite-vss / qdrant / pgvector。

## 9. 迁移器（QRDic → linling）

脚本 `scripts/migrate_qrdic.py`：

1. 读 `QRDic/dicpro.txt` → 行级扫描切 handler：空行作为分隔，顶格的非保留字文本即 trigger。
2. 对每个 handler：
   - 尝试用新 DSL 的 parser 解析；
   - 兼容 shim：`$BSH 图文.java imagettftext …$` → `$图文 …$`（新工具）。
   - 路径改写：`/storage/emulated/0/QR/QRDic/data/picture/…` → 资源键。
   - 硬编码 QQ/群号 → `%管理员%`/`%主群%` 占位 + 配置。
3. 读 `QRDic/data/**` Properties：
   - 目录层转 `scope`，文件名转 `file`，每行 `key=value` 入 `kv` 表。
   - `.bak` 文件忽略（原版的双写备份）。
4. 输出：
   - `bot/rules/*.ling`
   - `bot/data.sqlite`
   - `migration_report.md`：无法解析 / 需要人工确认的片段。

## 10. CLI

- `linling run`：启动平台；加载 `bot.yaml`。
- `linling migrate qrdic --src QRDic/ --out bot/`：执行迁移。
- `linling lint rules/`：语法检查 + 静态告警（未使用变量、不可达代码、危险工具调用）。
- `linling repl`：启动 CLI 适配器，交互式调试。
- `linling play rules/some.ling --input "..."`：单规则试跑。
- `linling tools list` / `linling tools schema <name>`：查看工具注册表。

## 11. 配置

`bot.yaml`（示例）：

```yaml
bot_id: susu_main
name: 涂山苏苏
admin_users: ["2078123478"]
main_group: "754800438"

storage:
  kv: sqlite:///./data/kv.db
  files: ./data/files
  vector: sqlite:///./data/vector.db
  scheduler: sqlite:///./data/sched.db

adapters:
  - kind: onebot
    ws_url: ws://127.0.0.1:8080
    access_token: ${ONEBOT_TOKEN}
  - kind: cli

rules:
  - rules/**/*.ling

agents:
  - agents/susu.yaml

llm:
  default: openai
  providers:
    openai:
      api_key: ${OPENAI_API_KEY}
      base_url: https://api.openai.com/v1
    gemini_proxy:
      kind: openai_compatible
      api_key: ${GEMINI_PROXY_KEY}
      base_url: https://proxy.example.com/v1

guardrails:
  http_allowlist:
    - "https://api.xingzhige.com/**"
    - "https://q1.qlogo.cn/**"
  dsl_timeout_ms: 2000
  max_output_segments: 20
```

## 12. 可观测

- 日志：结构化 JSON（`event_id, handler, latency_ms, outcome`）。
- 指标：Prometheus（`events_total`, `handler_latency`, `llm_tokens_total`, `tool_calls_total`）。
- 审计：每一次 LLM 调用与工具调用写表；Admin 可回溯。
- Web UI（后续）：事件流、规则命中率、LLM 开销、KV 浏览器。

## 13. 安全与沙箱

- DSL 运行在进程内协程里，但：
  - 通过 `resource` + 计数器实现 CPU/步数限制；
  - HTTP/文件走白名单；
  - 敏感函数（`send` / `recall` / `mute`）需要 handler 前置权限声明 `&&权限: 群管`。
- LLM 输入白名单（不可信内容标记），`system` 和 `user` 分离，禁止把用户内容拼到 system。
- 秘钥：`.env` + KMS/SOPS；落库字段加密。

## 14. 测试策略

- 单测：DSL 词法 / 语法 / 求值；工具映射；KV CRUD；调度器。
- 迁移回归：把 QRDic 里 20 条主处理器做成黄金测试（输入 → 期望输出）。
- 适配器集成：OneBot 用 mock WS server；CLI 用 pty。
- E2E：docker-compose 起 LLM mock + OneBot mock + 平台，跑脚本化对话。

## 15. 发布与路线图

- v0.1：Core + DSL + OneBot + CLI + KV(SQLite)（可迁移并运行 QRDic 主路径）。
- v0.2：Agent + 工具 registry 打通 + 记忆。
- v0.3：Web UI + 指标 + 审计。
- v0.4：WeChat / Feishu 适配器；Postgres 后端。
- v0.5：多租户 SaaS。

## 16. Appendix A — 迁移映射表（高频）

| 老 QRDic | 新平台 |
|---|---|
| `QRDic/dicpro.txt` | `bot/rules/*.ling` |
| `QRDic/data/**` Properties | `kv` 表行 |
| `QRDic/BSH/图文.java` | `tools-stdlib/image_text.py`（Pillow） |
| `/storage/emulated/0/QR/QRDic/data/picture/*.jpg` | `files/picture/*.jpg` + 资源引用 `@pic:xxx` |
| `$BSH 图文.java imagettftext …$` | `$图文 …$` 或直接新工具名 |
| `$访问 url$` | `$访问 url$`（保留但走白名单） |
| `%时间HHmm%` | 保留；时间引擎改为平台时区配置 |

## 17. Appendix B — 开放问题（待定稿）

1. ~~Rule 扩展名定 `.ap` 还是 `.dic` 或 `.fox`？~~ 定为 `.ling`。
2. 对 `正则:` 这个关键字是否还单独保留？或统一到 `如果:` + `匹配(...)` 函数？
3. 算术 `[…]` 支不支持字符串拼接？老代码里 `[%玉%+100]` 明显是数字，但 `[%有%+%量%*0.8]` 可能混用。
4. DSL 是否允许定义用户自定义函数（`函数 ... 函数尾`）？如果允许，怎么防死循环？
5. Agent 与 DSL 共享 KV 时，是否需要引入字段级 ACL（DSL 写的字段，Agent 默认只读）？
