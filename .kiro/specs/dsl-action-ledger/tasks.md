# Implementation Plan: DSL Action Ledger

## Overview

本实施计划严格按 design.md 中"Migration / 分阶段实施"的 6 个阶段推进:让 LLM 兜底对话感知用户最近执行的 DSL 操作,通过新增 `Session.dsl_events` 在 DSL 派发时追加 `DslEvent`,在 chat 派发前由 `LedgerRenderer` 渲染为 `<recent_user_actions>` 临时 system 消息注入,**绝不**污染 `Session.history` 与 `KVHistoryStore`。Phase 1 是其余阶段的前提;Phase 2 与 Phase 3 在 Phase 1 后可并行;Phase 4 是 Phase 2 的纯加性补充;Phase 5 是 Phase 6 的前提(`LedgerStore.clear` 协议)。

> 按 Kiro 约定,每个任务勾上复选框、含子任务、含引用的需求编号。
> 标记 `*` 的子任务为可选测试任务,可跳过以快速 MVP;但 PBT(Property 1–12)是验收"字段一致性 + 渲染语义 + 持久化往返"的关键,推荐执行。
> 执行前应先在 `requirements.md` / `design.md` 回看对应条目,Property 实现指引见 design.md "Testing Strategy" 表。

## Tasks

### 阶段 P1 — `Session.dsl_events` 内存骨架

- [x] 1. 在 linling_core 引入 `DslEvent` 与 `dsl_events` 字段
    - [x] 1.1 在 `linling_core/pipeline.py` 增加 frozen `DslEvent` dataclass
        - 字段:`timestamp` / `trigger` / `args: tuple[str, ...]` / `summary` / `outcome` / `mode` / `actor_id` / `occurred_at: float`
        - `frozen=True, slots=True`,不导入 `linling_dsl` / `linling_agent`
        - _Requirements: 1.3, 1.4, 1.5, 1.6, 6.4_
    - [x] 1.2 为 `Session` 增加 `dsl_events: deque[DslEvent]` 字段
        - 用 `field(default_factory=deque)` 默认值,不破坏现有 `Session(key=..., lock=...)` 构造
        - _Requirements: 1.8, 8.10_
    - [x] 1.3 `ConversationStore.__init__` 增加 `ledger_maxlen` 参数
        - 默认值 20,验证范围 [1, 200],越界抛 `ValueError`
        - 在 `get_or_create` 内以 `deque(maxlen=self._ledger_maxlen)` 初始化 `Session.dsl_events`
        - _Requirements: 1.8, 8.10_
    - [x] 1.4 实现 `ledger_scope_keys(event, *, logger=None) -> tuple[str, str]` 纯函数
        - group → `(scope.id, "_group")`;dm → `(scope.id, sender.id or "_unknown")`;其他 → 回退 `(scope.id, sender.id or "_unknown")` 并记 `pipeline.ledger_scope_unknown` 日志
        - 仅供 ledger 使用,**不**改变 chat history 的 scope 解析逻辑
        - _Requirements: 6.1, 6.2, 6.3, 6.7_
    - [x]* 1.5 单元测试:Session 默认空 deque、maxlen FIFO、scope_keys 三分支
        - `test_session_has_empty_dsl_events_by_default`
        - `test_ledger_maxlen_fifo_evicts_oldest_on_overflow`
        - `test_ledger_scope_keys_dispatches_group_dm_unknown`
        - _Requirements: 1.8, 6.1, 6.2, 6.3, 8.10_

- [x] 2. P1 检查点
    - 跑 `pytest packages/core/tests` 全过,无现有测试退化
    - 确认 `Session(key=..., lock=...)` 风格构造仍合法
    - Ensure all tests pass, ask the user if questions arise.

### 阶段 P2 — DslCommandDispatcher 写入 ledger

- [x] 3. 创建 `packages/dsl/src/linling_dsl/ledger.py`
    - [x] 3.1 定义 `LedgerStore` Protocol(`runtime_checkable`)
        - 接口:`save(scope_id, file_id, events)` / `load(scope_id, file_id)` / `clear(scope_id, file_id)`
        - 仅依赖 `linling_core.pipeline.DslEvent`,**不**导入 `linling_agent`
        - _Requirements: 8.2, 8.7_
    - [x] 3.2 实现 `LedgerWriter` 类核心逻辑
        - `__init__(*, store=None, single_char_budget=200, global_default_expose=True)`,budget 越界(范围 [150, 300])抛 `ValueError`,`global_default_expose` 构造后不可变
        - 公开 `append(*, session, handler, captures, raw_summary, outcome, event)`
        - `_resolve_expose`:`expose_to_llm is True/False` > `handler.is_internal == True → False` > `global_default_expose`
        - `_resolve_mode`:`summary_mode in {"trigger_only", "with_result"}` 否则 `"with_result"`
        - `_truncate`:`len > budget` 时取前 `(budget-1)` + `\u2026`
        - 在 `outcome=="error"` 或 `mode=="trigger_only"` 时强制 `summary=""`
        - `actor_id = event.sender.id or "_unknown"`
        - _Requirements: 1.1, 1.2, 1.5, 1.6, 1.7, 2.1, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 4.1, 4.2, 4.3, 4.4, 5.1, 5.2, 5.6, 6.4_
    - [x] 3.3 实现 fire-and-forget save 路径
        - `asyncio.create_task(self._safe_save(scope_id, file_id, list(session.dsl_events)))`,主路径不阻塞
        - `_safe_save` 用 `try/except` 包裹,失败仅 `logger.exception("dsl_dispatcher.ledger_save_failed", ...)`,异常**不**向上传播
        - 调用 `ledger_scope_keys(event, logger=...)` 计算 scope 键
        - _Requirements: 8.1, 8.7, 10.5_
    - [x]* 3.4 PBT — Property 1: Ledger maxlen 不变量
        - **Feature: dsl-action-ledger, Property 1: maxlen FIFO**
        - **Validates: Requirements 1.8, 2.4, 2.5, 8.10**
        - 文件:`packages/dsl/tests/test_ledger_writer.py`
        - Strategy:`lists(dsl_event_strategy())` × `integers(min_value=1, max_value=200)`
    - [x]* 3.5 PBT — Property 2: Append 字段一致性
        - **Feature: dsl-action-ledger, Property 2: field consistency**
        - **Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.1, 4.3, 4.4, 5.1, 5.2, 6.4**
        - 同 `test_ledger_writer.py`
    - [x]* 3.6 PBT — Property 3: Expose 决策表
        - **Feature: dsl-action-ledger, Property 3: expose decision table**
        - **Validates: Requirements 1.7, 3.1, 3.2, 3.3, 3.5, 3.6**
    - [x]* 3.7 PBT — Property 4: Mode 决策
        - **Feature: dsl-action-ledger, Property 4: mode decision**
        - **Validates: Requirements 5.1, 5.2, 5.6**
    - [x]* 3.8 PBT — Property 10: Save 失败隔离
        - **Feature: dsl-action-ledger, Property 10: save failure isolation**
        - **Validates: Requirements 8.7, 9.1, 9.5**
        - mocked `LedgerStore.save` 抛任意异常,断言 `session.dsl_events` 与 `store=None` 路径逐字段相等,且 `LedgerWriter.append` 正常返回
    - [x]* 3.9 单测:边界与不变性
        - `test_global_default_expose_immutable_after_construction`
        - `test_single_char_budget_out_of_range_raises_value_error`
        - `test_truncate_appends_ellipsis_at_exact_budget`
        - _Requirements: 3.4, 4.2, 4.3_

- [x] 4. 在 `Handler` AST 节点加可选元数据字段
    - [x] 4.1 修改 `packages/dsl/src/linling_dsl/ast_nodes.py`
        - `Handler` 增加 `expose_to_llm: bool | None = None`、`summary_mode: str | None = None`,字段位于末尾,默认 `None`
        - 旧 `Handler(trigger=..., is_internal=..., body=..., line=...)` 构造仍合法
        - _Requirements: 3.6, 5.6_
    - [x]* 4.2 单测:旧构造路径与 `getattr` 兼容
        - `test_handler_default_metadata_none`
        - `test_legacy_handler_construction_still_valid`
        - _Requirements: 3.6, 5.6_

- [x] 5. 修改 `DslCommandDispatcher`
    - [x] 5.1 `__init__` 接受 `ledger_writer: LedgerWriter | None = None`
        - 默认 `None` 时所有 ledger 路径短路,与现有行为完全一致
        - _Requirements: 1.7_
    - [x] 5.2 `run` 走 writer-on-error-then-raise 模式
        - VM 异常路径:**先**调 `ledger_writer.append(outcome="error", raw_summary="", ...)`,**再** `raise`(不 swallow,不 wrap)
        - 成功路径:由 `result.segments` 中 `TextSegment.text` 按声明顺序无分隔符拼接得 `raw_summary`,再调 `ledger_writer.append(outcome="ok", raw_summary=..., ...)`
        - 现有 `_segments_to_action` 行为不变
        - _Requirements: 1.1, 1.2, 1.5, 2.1, 2.3, 2.5_
    - [x]* 5.3 集成测试:writer 注入下的端到端行为
        - `test_dsl_dispatcher_propagates_vm_exception_to_router_safe`
        - `test_internal_debug_can_read_error_events_from_dsl_events`
        - `test_audit_failure_does_not_block_ledger_append`
        - `test_ledger_path_does_not_call_audit_sink`
        - _Requirements: 2.3, 2.6, 10.1, 10.5_

- [x] 6. P2 检查点
    - 确保 Property 1–4、Property 10 全过
    - 现有 DSL 派发测试无退化(`packages/dsl/tests` 全套)
    - Ensure all tests pass, ask the user if questions arise.

### 阶段 P3 — AgentChatDispatcher 注入 ledger

- [x] 7. 创建 `packages/agent/src/linling_agent/ledger.py`
    - [x] 7.1 实现 `LedgerRenderer` 类
        - `__init__(*, total_char_budget=800, include_actor=False)`,越界(范围 [200, 8000])抛 `ValueError`
        - 公开 `render(events: Iterable[DslEvent]) -> Message | None`
        - 实现:仅保留 `outcome=="ok"` 事件;由旧到新累加,首次 `len(content) > budget` 前停止追加;`<truncated count="N"/>` 行精确对账;全丢光时返回 `None`(**不**输出仅含 truncated 的空块)
        - 用 `xml.sax.saxutils.escape` + `quoteattr` 做属性转义,确保 `Message.content` 经 XML 1.0 解析无错
        - 起止标签 `<recent_user_actions>` / `</recent_user_actions>` 各恰好出现一次,前后无空白/换行
        - `mode == "with_result"` 且 `summary != ""` 才输出 `summary` 属性
        - _Requirements: 1.9, 1.11, 1.12, 4.5, 4.6, 4.7, 4.8, 4.9, 4.10, 5.3, 5.4, 5.5, 11.1, 11.2, 11.7_
    - [x] 7.2 实现 group 场景下的 `include_actor` 切换路径
        - `by="..."` 属性仅在 `include_actor==True` 且 `actor_id != "_unknown"` 时输出
        - 提供 `with_actor(flag: bool) -> LedgerRenderer` 工厂方法,避免在 hot path 重新构造
        - _Requirements: 6.5, 6.6_
    - [x]* 7.3 PBT — Property 5: Renderer 确定性
        - **Feature: dsl-action-ledger, Property 5: renderer determinism**
        - **Validates: Requirements 11.4, 10.2**
        - 文件:`packages/agent/tests/test_ledger_renderer.py`
        - 断言 `render(events) == render(events)` 在 UTF-8 字节序列上严格相等
    - [x]* 7.4 PBT — Property 6: 渲染产物字符 budget 上限
        - **Feature: dsl-action-ledger, Property 6: budget upper bound**
        - **Validates: Requirements 4.6, 4.8, 11.1, 11.2**
    - [x]* 7.5 PBT — Property 7: 截断计数对账
        - **Feature: dsl-action-ledger, Property 7: truncation count accounting**
        - **Validates: Requirements 4.7**
    - [x]* 7.6 PBT — Property 8: 渲染产物 XML 往返
        - **Feature: dsl-action-ledger, Property 8: XML round-trip**
        - **Validates: Requirements 5.3, 5.4, 5.5, 6.5, 6.6, 11.6**
        - Strategy:`dsl_event_strategy(with_xml_chars=True)`
    - [x]* 7.7 PBT — Property 9: 空 / 全过滤 → None
        - **Feature: dsl-action-ledger, Property 9: empty/all-filtered → None**
        - **Validates: Requirements 1.12, 4.9, 4.10, 11.7, 10.3**
    - [x]* 7.8 边界单测
        - `test_total_char_budget_out_of_range_raises_value_error`
        - `test_renderer_does_not_access_audit_sink`
        - _Requirements: 4.5, 10.2, 10.3_

- [x] 8. 修改 `AgentChatDispatcher`
    - [x] 8.1 `__init__` 接受 `ledger_store: LedgerStore | None = None` 与 `ledger_renderer: LedgerRenderer | None = None`
        - 现有 `history_store` 参数与现有调用方完全兼容,默认 `None` 等价于不启用本特性
        - _Requirements: 1.11, 1.12, 11.5_
    - [x] 8.2 引入 `_LEDGER_HYDRATED_FLAG = "_linling_ledger_hydrated"` 常量
        - 与 `_HYDRATED_FLAG` 同模式,**独立**标志位
        - _Requirements: 8.5_
    - [x] 8.3 重写 `_maybe_rehydrate` 走并发路径
        - 用 `asyncio.gather(*tasks, return_exceptions=True)` 并发触发 history + ledger rehydrate
        - 任一路径失败不阻塞另一路径;ledger 失败仅记 `chat_dispatcher.ledger_load_failed`,以空 ledger 继续
        - 仅在 `session.dsl_events` 为空时合并 restored,按 `occurred_at` 升序填回
        - _Requirements: 8.4, 8.5, 8.6_
    - [x] 8.4 在 `dispatch()` 内调用 LLM 之前 render ledger 并注入
        - 先 `history = list(session.history)`,再 `injected = list(history) + ([ledger_msg] if ledger_msg else [])`
        - 渲染产物**绝不** append 进 `session.history`,**绝不**经 `_persist`
        - group 场景下用 `renderer.with_actor(True)` 切换;DM 场景用默认 `include_actor=False`
        - 注入位置:索引大于所有现有 history 消息、小于本次 user 输入消息
        - _Requirements: 1.9, 1.10, 1.11, 11.3, 11.5_
    - [x] 8.5 cancel 路径不触发任何 ledger 写入
        - 现有 cancel race 逻辑保持兼容;`asyncio.create_task` 创建的 save task 已脱离 dispatch 协程取消树,自然完成或失败仅记日志
        - cancel 提前返回 `None` 时,`session.dsl_events` 与 `KVDslLedgerStore` 状态保持与 dispatch 开始前一致
        - _Requirements: 9.1, 9.2, 9.5_
    - [x]* 8.6 集成测试:rehydrate 并发、cancel 不污染、history 不含 ledger msg
        - `test_rehydrate_concurrent_history_and_ledger`
        - `test_rehydrate_ledger_load_failure_falls_back_empty`
        - `test_chat_history_unchanged_when_ledger_msg_injected`
        - `test_cancelled_turn_does_not_persist_ledger`
        - `test_ledger_msg_position_after_history_before_user`
        - _Requirements: 8.5, 8.6, 9.2, 11.3, 11.5_

- [x] 9. P3 检查点
    - 确保 Property 5–9 全过
    - `AgentChatDispatcher.run` 输出语义不变(history 不写 ledger msg,`_only_turn_messages` 不变量保持)
    - Ensure all tests pass, ask the user if questions arise.

### 阶段 P4 — Handler 级 `expose_to_llm` / `summary_mode` 解析

- [x] 10. DSL parser 识别 metadata 指令
    - [x] 10.1 扩展 DSL parser 识别 handler 元数据
        - 支持形如 `^expose_to_llm: false` / `^summary_mode: trigger_only` 等元数据行(具体语法依实现选择)
        - 布尔字面量 `True` / `False` 与字符串字面量 `trigger_only` / `with_result` 解析需一致
        - 非法值(如 `expose_to_llm: garbage`)不阻断 handler 加载,以 `None` 填充
        - _Requirements: 3.1, 3.2, 5.1, 3.6, 5.6_
    - [x] 10.2 把解析得到的字段填入 `Handler` dataclass
        - 直接复用 Phase 2 已定义字段,`LedgerWriter` 路径无需改动
        - _Requirements: 3.6, 5.6_
    - [x]* 10.3 单测:parser 解析合法 / 缺失 / 非法元数据值
        - `test_parser_recognizes_expose_to_llm_true`
        - `test_parser_recognizes_summary_mode_trigger_only`
        - `test_parser_falls_back_on_invalid_metadata_value`
        - `test_parser_handler_without_metadata_keeps_none_defaults`
        - _Requirements: 3.6, 5.6_
    - [x]* 10.4 PBT 重跑 — Property 3 + Property 4 在 parser 填充的 Handler 上仍通过
        - **Feature: dsl-action-ledger, Property 3 + 4 retest on parser-populated handlers**
        - **Validates: Requirements 1.7, 3.1, 3.2, 3.3, 3.5, 3.6, 5.1, 5.2, 5.6**

- [x] 11. P4 检查点
    - 确保 Property 3、4 在真正声明 metadata 的 handler 上仍通过
    - 现有 parser 测试无退化
    - Ensure all tests pass, ask the user if questions arise.

### 阶段 P5 — `KVDslLedgerStore` 持久化

- [x] 12. 创建 `packages/agent/src/linling_agent/ledger_store.py`
    - [x] 12.1 实现 `KVDslLedgerStore` 构造与字段
        - `__init__(kv, *, ttl_seconds=3600, maxlen=20)`,TTL 越界(范围 [60, 86400])fallback 到默认 + 记 `kv_dsl_ledger_store.ttl_invalid`
        - `maxlen` 越界(范围 [1, 200])抛 `ValueError`
        - 键前缀常量 `_LEDGER_SCOPE_PREFIX = "__dsl_ledger__"`,与 `__history__` 完全分离
        - _Requirements: 8.2, 8.3, 8.8, 8.10_
    - [x] 12.2 实现 `save / load / clear` 方法
        - `save`:整 deque 一行 JSON blob,`{"saved_at", "ttl", "events": [...]}`,写入前按 `maxlen` 截断尾部
        - `load`:JSON 解析失败 / 字段缺失 / 类型不符 → 跳过该条 + 记 `kv_dsl_ledger_store.record_corrupt`,过期事件按 `occurred_at + ttl < now` 丢弃,返回按 `occurred_at` 升序的列表
        - `clear`:`kv.delete(_LEDGER_SCOPE_PREFIX + "/" + scope_id, file_id, "events")`
        - _Requirements: 8.2, 8.4, 8.8, 8.9, 8.10_
    - [x]* 12.3 PBT — Property 11: Scope 隔离
        - **Feature: dsl-action-ledger, Property 11: scope isolation**
        - **Validates: Requirements 6.1, 6.2, 6.7**
        - 文件:`packages/agent/tests/test_kv_dsl_ledger_store.py`
        - Strategy:`event_strategy() × event_strategy()` filter 不同 keys,断言 (group, group)、(dm, dm)、(group, dm) 三类组合均不交叉污染
    - [x]* 12.4 PBT — Property 12: Persist 往返
        - **Feature: dsl-action-ledger, Property 12: persist round-trip**
        - **Validates: Requirements 8.4, 8.9**
    - [x]* 12.5 边界单测
        - `test_ttl_out_of_range_falls_back_to_default_and_logs`
        - `test_kv_dsl_ledger_store_uses_separate_prefix_from_history`
        - `test_corrupt_record_skipped_with_log`
        - `test_chat_history_scope_logic_unchanged`
        - `test_unknown_scope_kind_falls_back_with_log`
        - _Requirements: 6.3, 6.7, 8.2, 8.3, 8.8, 8.9_

- [x] 13. Bootstrap 接入
    - [x] 13.1 在 bootstrap config 处构造 `KVDslLedgerStore` 实例
        - 接受 `ttl_seconds` / `maxlen` / `single_char_budget` / `total_char_budget` / `global_default_expose` 配置项
        - _Requirements: 8.2, 8.3, 4.1, 4.5_
    - [x] 13.2 把 store 注入 `LedgerWriter` 与 `AgentChatDispatcher`
        - DSL 路径:`DslCommandDispatcher(ledger_writer=LedgerWriter(store=store, ...))`
        - Chat 路径:`AgentChatDispatcher(ledger_store=store, ledger_renderer=LedgerRenderer(...), ...)`
        - _Requirements: 8.1, 8.4_
    - [x]* 13.3 性能测试:save 调用主路径 ≤ 5ms
        - **Feature: dsl-action-ledger, Performance: dispatch.save_under_5ms_main_path**
        - **Validates: Requirements 8.1**
        - 模拟 100 次 `LedgerWriter.append`,断言 P99 主路径耗时 < 5ms(`asyncio.create_task` 立即返回,save 在 event loop 下一 tick 才执行)
        - 文件:`packages/agent/tests/test_ledger_perf.py`

- [x] 14. P5 检查点
    - 确保 Property 11、12 + 性能测试全过
    - 现有 `KVHistoryStore` 行为不退化(键前缀互不影响,`_only_turn_messages` 不变量保持)
    - Ensure all tests pass, ask the user if questions arise.

### 阶段 P6 — `/reset` 集成

- [x] 15. 在 `linling_core/router.py` 增加 `LedgerReset` 协议与 `_do_reset` 联动
    - [x] 15.1 定义 `LedgerReset` Protocol(`runtime_checkable`)
        - 接口:`async def clear_ledger(self, scope_id: str, file_id: str) -> None`
        - 与 `HistoryReset` 同层、独立判定;同一 chat dispatcher 可同时实现两者
        - _Requirements: 7.2_
    - [x] 15.2 修改 `Router._do_reset`
        - 在持有 `session.lock` 的同一 try 块内同步执行:`session.history.clear()` → `session.dsl_events.clear()` → `clear_history`(已有) → `clear_ledger`(新)
        - `clear_ledger` 抛错 → 仅 `logger.exception("router.reset_ledger_clear_failed", scope_id=..., sender_id=...)`,**不**阻断,仍向用户回 `RouterConfig.reset_reply`(字段、值不变)
        - 用 `ledger_scope_keys(event)` 计算 scope 键,**绝不**跨 scope 删除
        - 复位 `_linling_history_hydrated` 与 `_linling_ledger_hydrated` 标志位
        - _Requirements: 7.1, 7.2, 7.3, 7.4_
    - [x]* 15.3 单测:reset 在 lock 内、按 scope 调用、抛错降级、reply 不变
        - `test_reset_clears_ledger_under_session_lock_atomically`
        - `test_reset_calls_clear_ledger_with_correct_scope_key_for_group`
        - `test_reset_calls_clear_ledger_with_correct_scope_key_for_dm`
        - `test_reset_ledger_clear_failure_logs_and_still_replies`
        - `test_reset_does_not_call_clear_ledger_when_dispatcher_not_protocol`(向后兼容)
        - _Requirements: 7.1, 7.2, 7.3, 7.4_

- [x] 16. `AgentChatDispatcher` 实现 `LedgerReset` 协议
    - [x] 16.1 添加 `clear_ledger(scope_id, file_id)` 方法
        - 与 `clear_history` 同模式,2 秒 timeout 包裹
        - `ledger_store is None` 时静默返回
        - _Requirements: 7.2_

- [x] 17. 端到端集成测试
    - [x]* 17.1 集成测试:DSL → chat → ledger 全流程
        - 注入 `KVDslLedgerStore` + `LedgerWriter` + `LedgerRenderer` 全套
        - 模拟用户先发 DSL 指令(如"签到")再发 chat("我刚才做了什么"),断言 LLM 看到 `<recent_user_actions>` 注入
        - 验证 `Session.history` 不含 system 消息,`KVHistoryStore` 不持久化 ledger msg
        - _Requirements: 1.9, 1.10, 1.11, 8.4, 11.5_
    - [x]* 17.2 集成测试:`/cancel` 不污染 ledger
        - `test_cancel_does_not_touch_dsl_events`
        - `test_cancelled_dispatch_does_not_save_to_kv_dsl_ledger_store`
        - `test_cancel_during_dsl_handler_returns_noop_reply`
        - _Requirements: 9.1, 9.2, 9.3, 9.5_
    - [x]* 17.3 现有 `test_router.py` 无退化
        - 跑 `pytest packages/core/tests/test_router.py` 全过
        - 重点:`test_builtin_reset_clears_in_memory_history`、`test_builtin_reset_invokes_history_store_when_supported` 仍通过
        - _Requirements: 7.3_

- [x] 18. 最终检查点
    - 全量 PBT(Property 1–12)+ Unit + Integration + 性能测试通过
    - 现有 `KVHistoryStore` / `Router` / `DslCommandDispatcher` / `AgentChatDispatcher` 测试无退化
    - 跨模块依赖方向校验:`linling_core` 不依赖 `linling_dsl` / `linling_agent`;`linling_dsl` 与 `linling_agent` 各自仅依赖 `linling_core`
    - Ensure all tests pass, ask the user if questions arise.

## Notes

- 标记 `*` 的子任务为可选测试任务,可跳过以快速 MVP;但 PBT 任务覆盖 Property 1–12 是验收"字段一致性 + 渲染 + 持久化"语义的关键,推荐全部执行。
- 每个任务引用具体 requirement 子条目(如 `Req 1.5, Req 4.3`),便于追溯;每个 PBT 任务显式标注 Property 编号与 Validates 的 requirements 列表。
- 阶段间依赖:Phase 1 → (Phase 2 ∥ Phase 3) → (Phase 4 ∥ Phase 5) → Phase 6;Phase 5 是 Phase 6 的前提(`LedgerStore.clear` 协议)。
- 检查点(Task 2 / 6 / 9 / 11 / 14 / 18)用于阶段间同步,确保不退化、提供回滚锚点。
- 所有新增模块严格遵守 design.md 的依赖方向:`linling_core` 不导入 `linling_dsl` / `linling_agent`;`LedgerStore` Protocol 在 `linling_dsl` 与 `linling_agent` 各自定义同构副本(避免 dsl 反向依赖 agent)。
- 渲染产物(`Message(role="system", content="<recent_user_actions>...")`)是**临时**对象,绝不写回 `Session.history`、绝不经 `KVHistoryStore.save`,这是 Property 5 与 Requirement 11.5 共同保护的不变量。

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2"] },
    { "id": 2, "tasks": ["1.3"] },
    { "id": 3, "tasks": ["1.4", "4.1"] },
    { "id": 4, "tasks": ["1.5", "4.2", "3.1", "7.1"] },
    { "id": 5, "tasks": ["3.2", "7.2"] },
    { "id": 6, "tasks": ["3.3"] },
    { "id": 7, "tasks": ["5.1", "8.1"] },
    { "id": 8, "tasks": ["5.2", "8.2"] },
    { "id": 9, "tasks": ["8.3"] },
    { "id": 10, "tasks": ["8.4"] },
    { "id": 11, "tasks": ["8.5"] },
    { "id": 12, "tasks": ["3.4", "7.3"] },
    { "id": 13, "tasks": ["3.5", "7.4"] },
    { "id": 14, "tasks": ["3.6", "7.5"] },
    { "id": 15, "tasks": ["3.7", "7.6"] },
    { "id": 16, "tasks": ["3.8", "7.7"] },
    { "id": 17, "tasks": ["3.9", "7.8"] },
    { "id": 18, "tasks": ["5.3", "8.6"] },
    { "id": 19, "tasks": ["10.1"] },
    { "id": 20, "tasks": ["10.2"] },
    { "id": 21, "tasks": ["10.3", "10.4"] },
    { "id": 22, "tasks": ["12.1"] },
    { "id": 23, "tasks": ["12.2"] },
    { "id": 24, "tasks": ["12.3"] },
    { "id": 25, "tasks": ["12.4"] },
    { "id": 26, "tasks": ["12.5"] },
    { "id": 27, "tasks": ["13.1"] },
    { "id": 28, "tasks": ["13.2"] },
    { "id": 29, "tasks": ["13.3"] },
    { "id": 30, "tasks": ["15.1", "16.1"] },
    { "id": 31, "tasks": ["15.2"] },
    { "id": 32, "tasks": ["15.3", "17.1", "17.2", "17.3"] }
  ]
}
```
