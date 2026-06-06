# LLM 记忆机制改进实施方案

## 背景

当前 Agent 记忆主路径已经从早期内存滑窗演进为三层结构：

- 短期历史：`Session.history` + `KVHistoryStore` 持久化最近 turn。
- 会话摘要：`ContextManager` 在上下文接近预算时折叠旧 turn。
- 长期画像：`ProfileStore` 按 QQ 号保存稳定用户事实，私聊自动注入，群聊通过工具读写。

群聊 fallback 由 `GroupBatchChatDispatcher` 做窗口聚合，使用群级共享历史 `sender_id=""`，再用 profile 维持跨群、跨重启的个人连续性。

## 本轮目标

本轮只做低风险、可验证的闭环改进：

1. 让 WebUI `/api/agents/{name}/memory` 读取真实运行时记忆，而不是只兼容旧 `_memory`。
2. 统一 group batch 的工具契约，让代码、文档和模型可见 schema 一致。
3. 保留现有 API 形状向后兼容，避免前端旧字段失效。
4. 补充聚焦测试覆盖改动路径。

## 具体实施

### 1. WebUI 真实记忆视图

新增 WebUI state 中的 per-agent memory provider。`attach_bot_to_webui` 在注册 agent 时绑定 provider，provider 直接读取 bot 的 KV：

- `short_term`：从 `KVHistoryStore.load(scope_id, sender_id)` 读取。
- `summary`：从 `KVHistoryStore.load_summary(scope_id, sender_id)` 读取。
- `long_term`：从 `ProfileStore.load(user_id)` 和 `load_name(user_id)` 读取。

兼容策略：

- 响应保留 `short_term` 和 `long_term`。
- 新增 `summary` 字段，默认 `""`。
- 如果没有 provider，继续回退到旧 `runtime.memory/_memory` 逻辑。

访问控制：

- 未传 `user_id` 时，后端按当前 WebUI 登录用户名读取，避免默认落到共享的 `webui` subject。
- 非 `superadmin` 只能读取与自己登录用户名相同的 `user_id`。
- `superadmin` 可以显式指定任意 `user_id`，用于排障和运营查看。
- 当前 agent 路由还没有可靠的 agent -> bot -> 群成员授权模型，因此不开放 `bot_admin` 跨用户查看记忆。

### 2. Group Batch 工具契约

当前执行器支持 `send_group`，但 `_group_batch_tool_schemas()` 没有暴露它；文档也写了三种工具。实现上补齐 `send_group` schema，让模型可以用工具直接发群消息。

注意力探针仍只暴露 reply-oriented 工具：

- `read_batch_messages`
- `reply_to_message`
- `send_group`

profile 工具继续不提供给探针，避免“只是好奇读画像”被误判为回复意图。

### 3. 验证范围

运行聚焦测试：

- `packages/webui/tests/test_agent_memory.py`
- `packages/agent/tests/test_group_batch.py`
- `packages/agent/tests/test_group_batch_profile.py`
- `packages/agent/tests/test_group_batch_attention_probe.py`

如 OpenAPI schema 变化，同步更新 WebUI OpenAPI snapshot 和类型文件。

## 后续阶段

本轮不做以下较高风险变更，只在完成后作为下一步推进：

- 群聊成功 batch 后的低频 profile distill。
- `memory.py` legacy 标记或迁移删除。
- 前端记忆面板展示 summary/profile。
- 更完整的端到端重启恢复测试。
