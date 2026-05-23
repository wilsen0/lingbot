# Requirements Document

DSL Action Ledger —— 让 LLM 兜底对话感知到用户刚才执行过的 DSL 指令操作（消息融合）。

## Introduction

当前架构下，DSL 命令路径与 LLM chat 路径完全分离：DSL 的输入与输出从不进入 `Session.history`，也不写入 `KVHistoryStore`。结果是用户在同一会话中穿插使用 DSL 指令与 LLM 兜底对话时，LLM 完全看不到 DSL 那几轮发生过什么，导致回复脱节、上下文失忆。

本特性引入一个独立的 **DSL Action Ledger**（DSL 操作分类账）：把 DSL 操作以受控、紧凑、provider-agnostic 的方式提供给 LLM 作为上下文。核心设计选择：

- 不直接 append 到 `Session.history`（避免 LLM 模仿 DSL 输出风格、token 爆炸、回声）。
- 不使用 `tool_call` 形式（避免幻觉、跨 provider schema 差异）。
- 在 `Session` 上新增独立结构 `dsl_events`，与 `history` 解耦。
- LLM 调用前，将 ledger 渲染成 `<recent_user_actions>` XML 块作为 `role="system"` 的临时消息注入到 history 末尾、user 输入之前；该渲染产物本身不写回 `Session.history`、不持久化。
- 提供 handler 级 `expose_to_llm` 白名单 / 黑名单开关。
- 单条与总渲染产物均有字符 budget。

最小可行实现遵循约定的分阶段开发顺序，但本 requirements 文档覆盖端到端的最终行为。

## Glossary

- **Ledger**：`Session.dsl_events` 中保存的 DSL 操作记录序列，亦即将被渲染成 `<recent_user_actions>` 注入 LLM 的"分类账"。
- **DslEvent**：Ledger 中的一条记录。包含字段 `timestamp`（HH:MM:SS 字符串）、`trigger`（命中的 DSL trigger 标签）、`args`（捕获参数列表）、`summary`（截断后的输出摘要）、`outcome`（`ok` / `error`）、`mode`（`trigger_only` / `with_result`）、`actor_id`（仅群聊场景，触发用户 id）、`occurred_at`（持久化排序用的单调时间戳）。
- **Ledger_Renderer**：负责把 `dsl_events` 序列化为 `<recent_user_actions>` XML 块的渲染器。输出一个临时 `Message`（`role="system"`），或在无可见事件时返回 `None`。
- **DslCommandDispatcher**：现有 DSL 命令分发器（`packages/dsl/src/linling_dsl/dispatcher.py`），DSL handler 执行后追加 `DslEvent`。
- **AgentChatDispatcher**：现有 LLM 聊天分发器（`packages/agent/src/linling_agent/dispatcher.py`），调用 LLM 前注入 ledger 渲染产物。
- **Session**：`packages/core/src/linling_core/pipeline.py` 中的 `Session` 类，本特性在其上新增 `dsl_events: deque[DslEvent]`。
- **Single_Char_Budget**：单条 `DslEvent.summary` 的字符上限，单位为 Python `len(str)` 计量的 Unicode code point 数；缺省值 200，允许配置区间 150–300（含端点）。
- **Total_Char_Budget**:单次渲染的 ledger 总字符上限（含起止 XML 标签），单位与 Single_Char_Budget 相同；缺省值 800，允许配置区间 200–8000（含端点）。
- **Ledger_Maxlen**：`dsl_events` deque 的最大长度；缺省值 20，至多 200。
- **expose_to_llm**：handler 级开关；可在 DSL handler 元数据上声明布尔字面量 `True` / `False`，控制该 handler 执行后是否进入 ledger。
- **Global_Default_Expose**：全局默认 `expose_to_llm` 取值，未在 handler 上声明时使用；类型布尔，缺省 `True`，构造后不可变更。
- **KVDslLedgerStore**：可选的持久化后端。使用 `__dsl_ledger__` 前缀写入 KV，与 `__history__` 完全独立；TTL 缺省 3600 秒（1 小时），构造时取值范围 60–86400 秒。
- **Audit_Sink**：现有审计层（`audit.sqlite`），与本特性所定义的 LLM-visible ledger 不复用、不互相影响。
- **Trigger_Only_Mode**：`DslEvent.mode == "trigger_only"`，`summary` 为空，仅记录 trigger 与参数。
- **With_Result_Mode**：`DslEvent.mode == "with_result"`，`summary` 包含截断后的输出文本。
- **Conversation_Scope**：ledger 的作用域键。DM 场景为 `(bot_id, scope_id, sender_id)`；group 场景为 `(bot_id, scope_id, "")`，群内成员共享。
- **Linling System**：本特性所属的整体运行时（涵盖 DslCommandDispatcher、AgentChatDispatcher、Router、Ledger_Renderer 等组件）。

## Requirements

### Requirement 1: DSL 操作进入 Ledger（基础融合行为）

**User Story:** 作为终端用户，我希望在同一会话中先用 DSL 指令完成几次操作（例如签到、查看灵玉），再用自然语言提问"我刚才做了什么？"时，LLM 能基于我刚才执行的 DSL 指令给出连贯回答，从而不再出现上下文断层。

#### Acceptance Criteria

1. WHEN 一条 DSL handler 在 `DslCommandDispatcher.run` 中返回且未抛出异常、且 `VMResult.ok` 为 `True`，THE DslCommandDispatcher SHALL 在其作用 Session 的 `dsl_events` 末尾追加一条 `DslEvent`，该 `DslEvent` 的 `outcome` 字段值为字符串 `"ok"`。
2. IF DSL handler 在 `DslCommandDispatcher.run` 中抛出异常或 `VMResult.ok` 为 `False`，THEN THE DslCommandDispatcher SHALL 在 `Session.dsl_events` 末尾追加一条 `DslEvent`，其 `outcome` 字段值为字符串 `"error"`、`summary` 字段为长度 0 的字符串 `""`。
3. WHEN 追加 `DslEvent` 时，THE DslCommandDispatcher SHALL 把 `timestamp` 字段设置为当前本地时间的 `HH:MM:SS` 字符串（24 小时制、零填充、长度恒为 8），且不包含日期、时区后缀或毫秒部分。
4. WHEN 追加 `DslEvent` 时，THE DslCommandDispatcher SHALL 把 `trigger` 字段设置为命中 handler 的 trigger 文本（来自 `HandlerMatch.handler`），把 `args` 字段设置为 `HandlerMatch.captures` 列表的浅拷贝（不同列表对象、元素引用相同）。
5. WHEN 追加 `DslEvent` 时且当前 handler 的 `expose_to_llm` 解析为 `True`、`mode` 为 `"with_result"`，THE DslCommandDispatcher SHALL 由 `VMResult.segments` 中所有 `TextSegment` 的 `text` 字段按声明顺序、无分隔符拼接得到 raw_summary，并按 Requirement 4 的截断规则写入 `summary`。
6. WHEN 追加 `DslEvent` 时且 `mode` 为 `"trigger_only"`，THE DslCommandDispatcher SHALL 把 `summary` 字段设置为长度 0 的字符串 `""`。
7. WHERE 当前 handler 的 `expose_to_llm` 解析为 `False`，THE DslCommandDispatcher SHALL NOT 向 `Session.dsl_events` 追加任何事件（与 `outcome` 取值无关）。
8. WHEN `dsl_events` 的当前长度等于 Ledger_Maxlen 且需要追加新事件，THE Session SHALL 通过 deque 的固定 `maxlen` 语义自动丢弃最旧的一条事件（FIFO），且追加完成后 `len(dsl_events)` 仍恰好等于 Ledger_Maxlen。
9. WHEN `AgentChatDispatcher.dispatch` 在调用 LLM 之前且 `Session.dsl_events` 中存在至少一条 `outcome == "ok"` 的事件，THE AgentChatDispatcher SHALL 调用 Ledger_Renderer 把当前 Session 的 `dsl_events` 渲染为单个 `Message`；该 `Message.role` 等于 `"system"`，`Message.content` 以 `<recent_user_actions>` 开头、以 `</recent_user_actions>` 结尾，且两标签之间至少为每条可见事件包含一行其 `timestamp`、`trigger`、`outcome`、`summary` 字段的序列化结果。
10. WHEN Ledger_Renderer 产生 `Message`，THE AgentChatDispatcher SHALL 把该 `Message` 注入到传给 LLM 的消息序列中本次 user 输入之前的位置，且该 `Message` 在所有现有 history 消息之后。
11. THE AgentChatDispatcher SHALL NOT 把 Ledger_Renderer 产生的 `Message` 追加到 `Session.history`，AND SHALL NOT 通过 `HistoryStore` 持久化该 `Message`。
12. WHEN `Session.dsl_events` 为空或不含任何 `outcome == "ok"` 事件，THE AgentChatDispatcher SHALL 跳过 ledger 注入；本次传给 LLM 的消息序列与不启用本特性时按字段逐条相等。

### Requirement 2: 失败的 DSL 不污染 Ledger

**User Story:** 作为机器人作者，我希望 VM 抛错或超时的 DSL 调用不要把脏数据带进 LLM 上下文，避免 LLM 学到错误示例或基于失败结果回答用户。

#### Acceptance Criteria

1. IF DSL handler 在 VM 中抛出异常（包括但不限于 `VMError`、`SandboxError`、`UndefinedVarError`、`TimeoutError`），THEN THE DslCommandDispatcher SHALL 把对应 `DslEvent` 的 `outcome` 设置为字符串字面量 `"error"`、`summary` 设置为长度 0 的字符串 `""`。
2. WHEN Ledger_Renderer 渲染 `<recent_user_actions>` 块时，THE Ledger_Renderer SHALL 跳过所有 `outcome == "error"` 的事件，使其 `trigger` / `args` / `summary` 等字段不出现在最终 `Message.content` 中。
3. WHEN DSL handler 在 VM 中抛出异常，THE DslCommandDispatcher SHALL 在写入 `outcome="error"` 的 `DslEvent` 之后再把异常向上传播给 `Router._safe`，且不得 catch、swallow 或包装该异常；现有 friendly fallback reply 行为以"该异常恰好触发 `Router._safe` 的 `errored=True` 单一路径"为可观测判定。
4. WHILE `outcome == "error"` 的事件保留在 `dsl_events` 中，THE Session SHALL 把每条该类事件按 1 计入 Ledger_Maxlen 容量上限（FIFO 淘汰，与 `outcome="ok"` 事件等价）。
5. THE Session SHALL 在 `dsl_events` 中保留 `outcome == "error"` 的事件，使后续审计与统计目的可枚举到这些事件。
6. THE 内部 ledger 检视工具 / 调试接口 SHALL 能够读取并展示 `outcome == "error"` 的事件，使其在 LLM 不可见的同时仍可被运维侧观察。

### Requirement 3: Handler 级 `expose_to_llm` 开关

**User Story:** 作为机器人作者，我希望对敏感或噪声大的 DSL handler（例如 `[内部]` 私有处理器、高频轮询触发器）显式禁用 ledger 暴露，避免污染 LLM 上下文或泄露内部实现细节。

#### Acceptance Criteria

1. WHERE 某个 DSL handler 在元数据中显式声明 `expose_to_llm = False`（布尔字面量 `False`），THE DslCommandDispatcher SHALL 在该 handler 执行后不向 `Session.dsl_events` 追加任何条目（无论 `outcome` 为何）。
2. WHERE 某个 DSL handler 在元数据中显式声明 `expose_to_llm = True`（布尔字面量 `True`），THE DslCommandDispatcher SHALL 在该 handler 执行后按 Requirement 1 与 Requirement 2 规则向 `Session.dsl_events` 追加一条 `DslEvent`。
3. WHERE 某个 DSL handler 未声明 `expose_to_llm`，THE DslCommandDispatcher SHALL 按以下顺序解析（首条匹配生效）：(a) 若 trigger 文本以字符串 `[内部]` 前缀开头则视为 `expose_to_llm = False`；(b) 否则使用 Global_Default_Expose 的取值。
4. THE DslCommandDispatcher SHALL 提供布尔配置项 Global_Default_Expose，缺省值为 `True`；该配置项在 dispatcher 构造完成后保持不可变更。
5. WHERE handler 的 trigger 文本以字符串 `[内部]`（区分大小写、无前导空白、从首字符起精确匹配 6 个字符）开头，THE Global_Default_Expose 解析逻辑 SHALL 将该 handler 视为 `expose_to_llm = False`，除非该 handler 显式声明了 `expose_to_llm = True`。
6. IF handler 元数据中的 `expose_to_llm` 字段存在但取值不是布尔字面量 `True` 或 `False`（含 `None`、字符串、数字等），THEN THE DslCommandDispatcher SHALL 将其视为未声明并按 Criterion 3 规则解析，且 SHALL NOT 阻断 handler 执行。

### Requirement 4: 字符 Budget 与摘要截断

**User Story:** 作为运维 Admin，我希望 ledger 注入的 system 消息长度可控，避免无界拼接造成 token 爆炸或超过 LLM 上下文窗口。

#### Acceptance Criteria

1. THE DslCommandDispatcher SHALL 提供配置项 Single_Char_Budget，缺省值 200；字符长度统一以 Python `len(str)` 返回的 Unicode code point 数计量。
2. IF 构造 DslCommandDispatcher 时传入的 Single_Char_Budget 超出 [150, 300]（含端点）范围，THEN THE DslCommandDispatcher SHALL 抛出 `ValueError`，不允许静默回退到默认值或裁剪到范围内。
3. WHEN raw_summary 的字符长度严格大于 Single_Char_Budget，THE DslCommandDispatcher SHALL 截断为前 (Single_Char_Budget − 1) 个字符并追加单字符省略号 `…`（U+2026），使最终 `summary` 字符长度恰好等于 Single_Char_Budget。
4. WHEN raw_summary 的字符长度小于等于 Single_Char_Budget，THE DslCommandDispatcher SHALL 把 `summary` 设置为 raw_summary 本身，不附加省略号。
5. THE Ledger_Renderer SHALL 提供配置项 Total_Char_Budget，缺省值 800；构造时取值超出 [200, 8000]（含端点）范围 SHALL 抛出 `ValueError`。
6. WHEN Ledger_Renderer 渲染 `<recent_user_actions>` 块时，THE Ledger_Renderer SHALL 按 `dsl_events` 由旧到新的顺序累加每条事件序列化后的字符长度，并在累加值首次严格大于 Total_Char_Budget 之前停止追加；事件之间的相对顺序保持不变。
7. WHEN Ledger_Renderer 在 budget 限制下省略了 N（N > 0）条事件，THE Ledger_Renderer SHALL 在 `<recent_user_actions>` 块中、被保留事件之前插入一行 `<truncated count="N"/>`；当 N == 0 时不插入该行。
8. WHEN 渲染产物含起止 XML 标签的最终字符长度严格大于 Total_Char_Budget，THE Ledger_Renderer SHALL 进一步丢弃最旧的可见事件直至总长度小于等于 Total_Char_Budget。
9. IF 上述丢弃过程导致全部可见事件被丢弃，THEN THE Ledger_Renderer SHALL 返回 `None`，使 AgentChatDispatcher 跳过注入；SHALL NOT 输出仅含 `<truncated/>` 但无可见事件的空块。
10. WHEN `dsl_events` 为空、或其中所有事件均被 `outcome == "error"` 过滤，THE Ledger_Renderer SHALL 返回 `None`，不输出任何 `<recent_user_actions>` 块。

### Requirement 5: 两档 Summary 模式

**User Story:** 作为机器人作者，我希望对纯触发型 handler（例如 `/help`、`/cancel`、`签到`）只记录"用户做过什么"，对包含语义结果的 handler（例如 `查看背包`、`抽奖`）才记录截断后的输出，从而既保留语义上下文又不浪费 token。

#### Acceptance Criteria

1. WHEN DslCommandDispatcher 追加 `DslEvent`，THE DslCommandDispatcher SHALL 根据 handler 元数据 `summary_mode` 字段决定 `DslEvent.mode` 字段取值；`mode` 的合法取值集合恰为 `{"trigger_only", "with_result"}`。
2. IF handler 元数据未声明 `summary_mode`，THEN THE DslCommandDispatcher SHALL 把 `DslEvent.mode` 设置为全局默认值 `"with_result"`。
3. WHEN Ledger_Renderer 渲染 `mode == "trigger_only"` 的事件，THE Ledger_Renderer SHALL 仅包含 `timestamp`、`trigger`、`args` 字段，不包含 `summary` 字段。
4. WHEN Ledger_Renderer 渲染 `mode == "with_result"` 的事件且 `summary` 是非空字符串，THE Ledger_Renderer SHALL 同时包含 `timestamp`、`trigger`、`args`、`summary` 字段。
5. WHEN Ledger_Renderer 渲染 `mode == "with_result"` 的事件且 `summary` 为空字符串、`None` 或字段缺失，THE Ledger_Renderer SHALL 省略 `summary` 字段（其余字段输出与 trigger_only 模式等价）。
6. IF handler 元数据中 `summary_mode` 字段存在但取值不属于 `{"trigger_only", "with_result"}`，THEN THE DslCommandDispatcher SHALL 视为未声明并按 Criterion 2 默认 `"with_result"` 处理；该回退 SHALL NOT 阻断事件追加流程。

### Requirement 6: Group 与 DM 的作用域选择

**User Story:** 作为机器人作者，我希望在群聊场景下，群内不同成员的 DSL 操作能够互相成为彼此 LLM 上下文的一部分（"群体共享视角"），而 DM 场景仍按用户隔离，避免混入他人数据。

#### Acceptance Criteria

1. IF `event.scope` 为 group 类型，THEN THE Session SHALL 使用 `(bot_id, scope_id, "")` 作为 ledger 的 Conversation_Scope 键，使群内所有成员共享同一 `dsl_events` 实例。
2. IF `event.scope` 为 DM（私聊）类型，THEN THE Session SHALL 使用 `(bot_id, scope_id, sender_id)` 作为 ledger 的 Conversation_Scope 键，使每个用户独立。
3. IF `event.scope` 既不是 group 类型也不是 DM 类型（未知或新增 scope 类型），THEN THE Session SHALL 回退到 `(bot_id, scope_id, sender_id)` 作为 ledger 的 Conversation_Scope 键，并记录结构化日志 `pipeline.ledger_scope_unknown`。
4. WHEN 群聊场景下追加 `DslEvent`，THE DslCommandDispatcher SHALL 在 `DslEvent` 中记录 `actor_id` 字段为 `event.sender.id`；IF `event.sender.id` 缺失或为空字符串，THEN `actor_id` 设为字符串 `"_unknown"` 且不阻断追加。
5. WHEN 群聊场景下渲染 `<recent_user_actions>`，THE Ledger_Renderer SHALL 把 `actor_id` 作为 XML 属性附在每条事件序列化结果上（如 `<action by="..."/>`），属性值按 XML 1.0 规则进行转义。
6. WHERE 一条 `DslEvent` 缺失 `actor_id` 字段（例如来自旧版本的持久化数据），THE Ledger_Renderer SHALL 在渲染时省略 `by` 属性而非输出 `by=""` 或 `by="None"`。
7. THE ledger 与 chat history 的 Conversation_Scope SHALL 各自独立解析；group 场景下 chat history 仍按 `(bot_id, scope_id, sender_id)` per-sender 作用域，不被本特性改变；ledger 的作用域选择 SHALL NOT 通过任何代码路径影响 chat history 的作用域选择。

### Requirement 7: `/reset` 命令与 Ledger 联动

**User Story:** 作为终端用户，我希望执行 `/reset` 时同时清空 chat history 与 DSL ledger，避免"清完 history 但 ledger 残留"导致的语义断层。

#### Acceptance Criteria

1. WHEN Router 执行内置 `/reset` 命令的 `_do_reset` 流程，THE Router SHALL 在持有 `session.lock` 的同一临界区内、与 `session.history.clear()` 同一原子流程中调用 `session.dsl_events.clear()`；外部观察者在两次清空之间 SHALL NOT 能够观察到 history 与 ledger 处于不一致中间状态。
2. WHERE `KVDslLedgerStore` 已配置（持久化阶段启用），WHEN Router 执行 `/reset`，THE Router SHALL 调用 `KVDslLedgerStore.clear`，且其作用域键 SHALL 严格等于该 Session 的 Conversation_Scope；SHALL NOT 跨 scope 删除其他 Session 的记录。
3. WHEN `/reset` 完成 ledger 清空后，THE Router SHALL 复用 `RouterConfig.reset_reply` 字符串作为回复文本，不引入新的回复文本字段、不修改条目数；该回复结构 SHALL 与不启用本特性时的 `/reset` 回复字段、值完全相同。
4. IF `KVDslLedgerStore.clear` 在 `/reset` 中抛出异常，THEN THE Router SHALL 记录结构化日志 `router.reset_ledger_clear_failed`（含 `scope_id`、`sender_id`、异常类型），随后仍向用户返回 `reset_reply`（与正常路径文本与字段完全相同），不向用户暴露错误，且 `session.dsl_events` 仍保持已被清空状态。

### Requirement 8: 跨进程重启的持久化

**User Story:** 作为终端用户，我希望机器人进程重启或被其他进程接管时，最近 1 小时内执行过的 DSL 操作仍能进入 LLM 上下文，避免"重启即失忆"。

#### Acceptance Criteria

1. WHERE `KVDslLedgerStore` 被注入到 `DslCommandDispatcher`，WHEN 一条新的 `DslEvent` 被追加到 `Session.dsl_events`，THE DslCommandDispatcher SHALL 调用 `KVDslLedgerStore.save` 写入键前缀为 `__dsl_ledger__` 的 KV 记录；该调用 SHALL NOT 阻塞主分发路径超过 5 毫秒（fire-and-forget 或异步任务）。
2. THE KVDslLedgerStore SHALL 使用与 `__history__` 完全分离的 KV 前缀 `__dsl_ledger__`，不共用 schema、不共用 TTL。
3. THE KVDslLedgerStore SHALL 对每条持久化记录设置 TTL，缺省值 3600 秒（1 小时），允许通过构造参数覆盖；构造参数取值范围为 60–86400 秒（含端点），超出范围时回退到缺省值并记录结构化日志 `kv_dsl_ledger_store.ttl_invalid`。
4. WHEN AgentChatDispatcher 首次为某个 Session 处理消息且该 Session 的 `dsl_events` 为空，THE AgentChatDispatcher SHALL 调用 `KVDslLedgerStore.load` 进行 rehydrate，仅装载 TTL 内未过期的事件，并按事件 `occurred_at` 升序填回 `Session.dsl_events`。
5. THE ledger rehydrate 与 chat history rehydrate SHALL 并发触发（通过 `asyncio.gather` 或等价机制）、不互相串行依赖；任一路径失败 SHALL NOT 阻塞另一路径完成；两者各自维护独立的 hydrated 标志位。
6. IF `KVDslLedgerStore.load` 抛出异常，THEN THE AgentChatDispatcher SHALL 记录结构化日志 `chat_dispatcher.ledger_load_failed` 并以空 ledger 继续 LLM 调用，不阻断本次会话。
7. IF `KVDslLedgerStore.save` 抛出异常，THEN THE DslCommandDispatcher SHALL 记录结构化日志 `dsl_dispatcher.ledger_save_failed` 并继续返回原始 actions，不向用户暴露错误。
8. THE KVDslLedgerStore SHALL NOT 与 `KVHistoryStore` 共用任何键空间或表，使现有 `_only_turn_messages`（仅接受 user/assistant role）逻辑保持不变。
9. WHEN `KVDslLedgerStore.load` 在解析单条已存事件时遇到字段缺失或 schema 不兼容，THE KVDslLedgerStore SHALL 跳过该条目、继续解析后续条目，并对每个被跳过的条目记录结构化日志 `kv_dsl_ledger_store.record_corrupt`。
10. THE `Session.dsl_events` 在持久化加载或运行期累积过程中 SHALL 受 Ledger_Maxlen（缺省 20，至多 200）上限约束；超出时按 `occurred_at` 升序丢弃最旧条目（FIFO）。

### Requirement 9: 与 `/cancel` 语义的一致性

**User Story:** 作为终端用户，我希望 `/cancel` 中断一次正在运行的 LLM chat 时，不会把那一轮"未完成"的状态错误地写入 ledger，保持现有"被取消的轮次不留痕"的语义。

#### Acceptance Criteria

1. WHEN Router 执行内置 `/cancel` 命令，THE Router SHALL NOT 修改 `Session.dsl_events` 的内容（不追加、不修改、不清空），AND SHALL NOT 调用 `KVDslLedgerStore.save`。
2. IF `AgentChatDispatcher.dispatch` 因 `cancel_event` 触发提前返回 `None`，THEN THE AgentChatDispatcher SHALL NOT 触发任何 ledger 写入操作，包括但不限于：不向 `Session.dsl_events` 追加事件、不调用 `KVDslLedgerStore.save`；ledger rehydrate 阶段已发起的读取允许完成，但其结果 SHALL NOT 被写入任何持久化存储。
3. WHILE DSL handler 正在执行（持有 `session.lock`），IF 用户发送 `/cancel`，THEN THE Router SHALL NOT 通过 `cancel_event` 中断 DSL 命令路径；该路径按现有 `cancel_noop_reply` 行为返回 no-op 回复；该约束与现有"DSL commands are not cancellable"行为一致。
4. WHEN `cancel_event` 已 set 但 LLM 调用先于 cancel 检测完成（race-condition 胜出方为 LLM），THE AgentChatDispatcher SHALL 按正常路径（非取消路径）记录 chat history 并返回结果；Ledger_Renderer 注入的 system 消息已随该次 LLM 调用被消费但本身不参与 chat history 持久化。
5. IF `AgentChatDispatcher.dispatch` 因 `cancel_event` 提前返回 `None`，THEN `Session.dsl_events` 与 `KVDslLedgerStore` 中的现有内容 SHALL 保持与本次 dispatch 开始前完全相同（无回滚也无累加）。

### Requirement 10: 审计层与 LLM-visible Ledger 的隔离

**User Story:** 作为运维 Admin，我希望审计层（`audit.sqlite`）与 LLM-visible ledger 各自独立演进，互不污染，使审计粒度可保持完整、ledger 粒度可保持紧凑。

#### Acceptance Criteria

1. THE Audit_Sink SHALL 不读取、不写入 `Session.dsl_events`；任何针对 `Session.dsl_events` 的追加、删除或修剪操作 SHALL NOT 触发 `AuditEntry.write` 调用。
2. THE Ledger_Renderer SHALL 不读取 Audit_Sink 的任何记录；ledger 渲染产物 SHALL 仅依赖 `Session.dsl_events`、Single_Char_Budget、Total_Char_Budget 以及与 ledger 渲染相关的配置项。
3. WHERE `Session.dsl_events` 为空列表或未初始化、或与 ledger 渲染相关的配置项使用默认值，THE Ledger_Renderer SHALL 返回 `None` 或仅含起止标签的空 `<recent_user_actions/>` 块（按 Requirement 4 / 11 决定），且 SHALL NOT 抛出异常、SHALL NOT 访问 Audit_Sink。
4. WHERE 同一次 DSL 派发同时产生 `DslEvent` 与 `AuditEntry`，THE Linling System SHALL 在两条彼此不互相调用的代码路径中分别产生这两条记录，并允许在各自的 schema 上独立增删字段而不要求另一方的代码或 schema 同步变更。
5. IF ledger 写入路径或 audit 写入路径中的任一路径抛出异常，THEN THE Linling System SHALL 保证另一路径不被该异常中断并按其原有逻辑完成写入。

### Requirement 11: 渲染产物的可测试不变量

**User Story:** 作为机器人作者，我希望 ledger 注入到 LLM 的内容是确定可断言的，便于编写回归测试，确保 prompt 不会因实现重构而漂移。

#### Acceptance Criteria

1. THE Ledger_Renderer 产生的 `Message.role` SHALL 严格等于 ASCII 小写字符串 `"system"`（区分大小写、长度恰为 6、无前后空白）。
2. THE Ledger_Renderer 产生的 `Message.content` 字符串 SHALL 以 `<recent_user_actions>` 起始、以 `</recent_user_actions>` 结尾，两标签在 `content` 中各恰好出现 1 次；起始标签前与结束标签后 SHALL NOT 含有前导/尾随空白或换行。
3. WHEN AgentChatDispatcher 注入 ledger 消息，THE 注入位置 SHALL 同时满足：(a) ledger 消息索引严格大于所有 `role` 为 `"assistant"` 或 `"user"` 的现有 history 消息索引；(b) ledger 消息索引严格小于本次 user 输入消息索引；(c) 现有 history 为空时 ledger 消息仍被注入并放置在系统提示之后、本次 user 输入之前。
4. WHEN 同一 Session 在两次 `AgentChatDispatcher.dispatch` 之间未新增、未移除、未修改任何 `DslEvent`，THE Ledger_Renderer 在两次调用产生的 `Message.content` SHALL 在 UTF-8 编码下逐字节相等（确定性渲染）。
5. WHEN AgentChatDispatcher 完成一次 LLM 调用（结束路径含成功、错误响应、异常），THE 调用结束后 `Session.history` 的内容 SHALL NOT 包含 Ledger_Renderer 产生过的 `Message`（即 `system` 消息从未被持久化或追加）。
6. WHEN 任一 `DslEvent` 的字符串字段（`trigger`、`args` 元素、`summary`、`actor_id`）包含 XML 特殊字符（`<`、`>`、`&`、`"`、`'`），THE Ledger_Renderer SHALL 对其进行 XML 1.0 规则转义；最终 `Message.content` 经 XML 1.0 解析器解析 SHALL 无语法错误，且字段往返解析后字符相等。
7. WHEN `Session.dsl_events` 为空或仅含被过滤的 `outcome == "error"` 事件，THE Ledger_Renderer SHALL 返回 `None`；本不变量优先于 Criterion 2 关于起止标签的约束（即 Renderer 此时不输出任何 `Message`）。
