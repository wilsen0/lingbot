# Tasks — linling（对话智能体平台）

> 按 Kiro 约定，每个任务勾上复选框、含子任务、含引用的需求编号。
> 建议按阶段推进：P0 → P1 → P2 → P3。执行前应先在 `requirements.md` / `design.md` 回看对应条目。

## 阶段 P0 — 项目骨架与最小闭环

- [x] 1. 初始化 monorepo 骨架
    - [x] 1.1 `pyproject.toml` + `uv` workspace + 预提交钩子（ruff/mypy/pytest）
    - [x] 1.2 新建 `packages/core packages/dsl packages/agent packages/adapters packages/tools-stdlib packages/cli`
    - [x] 1.3 基础日志（structlog）、配置加载（pydantic-settings）、.env 示例
    - _Requirements: NFR-1, NFR-6_

- [x] 2. 定义统一事件/动作模型
    - [x] 2.1 `linling_core.events.Event / Action / Segment / Scope / User` 的 pydantic 模型
    - [x] 2.2 Segment 枚举与编解码辅助（from_onebot_msg / to_onebot_msg 预留）
    - [x] 2.3 `EventBus`：异步发布订阅，支持优先级
    - _Requirements: US-P3, US-P4_

- [x] 3. KV 存储（SQLite 实现）
    - [x] 3.1 `KVStore` 接口 + SQLite 实现（含 `read/write/delete/rank`）
    - [x] 3.2 迁移工具：自动建表、wal 模式、事务封装
    - [x] 3.3 单测：读写、并发、排行榜语义与老 `$排行榜$` 对齐
    - _Requirements: US-S1, NFR-5_

- [x] 4. 工具注册表（Tool Registry）
    - [x] 4.1 `@tool` 装饰器：Python 名 / DSL 名 / JSON schema / safe 标志
    - [x] 4.2 运行时三视图（python call / dsl dispatch / llm schema）
    - [x] 4.3 标准工具：`read_kv / write_kv / delete_kv / rank_kv / http_get / http_post / random_int / regex_match / replace`
    - _Requirements: US-M3_

- [x] 5. CLI 适配器（最简）
    - [x] 5.1 `ap repl`：stdin 作消息，stdout 作回复
    - [x] 5.2 支持 `@用户` / `%群号%` 的手动注入，便于调试
    - _Requirements: S-4_

- [x] 6. DSL v0：词法与语法解析器
    - [x] 6.1 词法器：关键字、字符串、数字、标识符、注释、`$...$`、`%...%`、`[...]`、`±img=...±`
    - [x] 6.2 语法器：按 EBNF 生成 AST（采用 `lark` 或手写 PEG）
    - [x] 6.3 错误定位：行列号、上下文片段
    - [x] 6.4 单测：对 `dicpro.txt` 里 30+ 条代表性 handler 正确生成 AST
    - _Requirements: US-D2, US-D3, US-D4_

- [x] 7. DSL v0：解释器 / 虚拟机
    - [x] 7.1 handler 作用域 + 局部变量 + 内置变量 + KV 读穿透
    - [x] 7.2 `如果/如果尾/正则/返回/完成/:label/$jump$`
    - [x] 7.3 算术表达式求值（整型/浮点/字符串拼接歧义处理）
    - [x] 7.4 工具调用派发（经 Tool Registry）
    - [x] 7.5 沙箱：超时、步数、输出段数
    - [x] 7.6 单测：与 QRDic 黄金样例比对
    - _Requirements: US-D3, US-D5, NFR-4_

- [x] 8. 调度器
    - [x] 8.1 `scheduler.delay(ms, handler, args)` 内存实现
    - [x] 8.2 持久化：重启恢复未触发任务
    - [x] 8.3 `$调用 ms handler$` DSL 入口
    - _Requirements: US-T1, US-T3_

- [x] 9. 最小 OneBot 适配器
    - [x] 9.1 反向 WS 客户端，`message / notice / request` 事件 → `Event`
    - [x] 9.2 `Action` → OneBot API 调用（`send_msg / delete_msg / set_group_ban / set_group_special_title`）
    - [x] 9.3 CQ / message array 双向映射（text/image/at/reply/face/poke）
    - [x] 9.4 能力矩阵实现 + 不支持能力的降级
    - _Requirements: US-P1, US-P3, US-P4_

- [x] 10. 配置系统
    - [x] 10.1 `bot.yaml` 加载 + ${ENV} 展开
    - [x] 10.2 `.env` + secrets 加载
    - [x] 10.3 HTTP 白名单、沙箱参数、主群/管理员从配置注入
    - _Requirements: US-X3, MIG-4_

- [x] 11. 端到端 smoke：P0 验收
    - [x] 11.1 CLI 适配器跑通"打卡 → 写灵玉 → 查看背包"三条规则
    - [x] 11.2 OneBot mock 跑通同样三条
    - _Requirements: S-1, S-2_

## 阶段 P1 — 迁移器 + 完整指令兼容

- [x] 12. 迁移器 `ap migrate qrdic`
    - [x] 12.1 分段切 handler；识别 `[内部]`；识别 `&&` 配置/注释
    - [x] 12.2 `$BSH 图文.java imagettftext$` → `$图文$` 重写
    - [x] 12.3 硬编码号码 → 占位 + 配置；`/storage/emulated/...` 路径 → 资源引用
    - [x] 12.4 Properties → KV（含 Unicode 转义解码，忽略 `.bak`）
    - [x] 12.5 输出 `migration_report.md`：无法迁移段、TODO、警告
    - _Requirements: MIG-1, MIG-2, MIG-3, MIG-4, S-2_

- [x] 13. 标准工具库补齐
    - [x] 13.1 `JSON 长度/获取/添加`、`URLEncoder/Decoder`、`Base64Decoder`、`HexEncoder/Decoder`、`UnicodeDecoder`
    - [x] 13.2 `替换 / 正则 / 概率随机`
    - [x] 13.3 `群昵称 / 群头衔 / 获取群成员 / 获取消息`（通过适配器 RPC）
    - [x] 13.4 `图文`（Pillow，中文 TTF，复刻 `图文.java` 行为）
    - [x] 13.5 `全局变量 / 取变量`（进程内 + 可选持久）
    - _Requirements: US-D3, MIG-3_

- [x] 14. `ap lint`
    - [x] 14.1 语法错误 / 未使用变量 / 不可达代码检测
    - [x] 14.2 危险工具使用（`$删除$`、`$访问$`、`$撤回$`）在缺少权限声明时报错
    - [x] 14.3 触发器正则冲突检测（多个 handler 会同时匹配）
    - _Requirements: US-D4_

- [x] 15. QRDic 黄金测试集
    - [-] 15.1 选 20 条主 handler（打卡、灵玉、扭蛋、钓鱼、戳一戳、背包、守护、偷玉、漂流瓶、羁绊、路线、对话兜底）
    - [ ] 15.2 固化输入 → 期望输出，接入 CI
    - _Requirements: NFR-5, S-2_

## 阶段 P2 — Agent 框架

- [x] 16. LLM Provider 抽象
    - [x] 16.1 `LLMProvider.chat(messages, tools, stream)`
    - [x] 16.2 实现：OpenAI / Anthropic / Gemini（openai 兼容代理）
    - [x] 16.3 token 计量 / 费用上报 / 失败重试
    - _Requirements: US-A2_

- [x] 17. Agent 定义与运行时
    - [x] 17.1 `agent.yaml` schema：provider/model/system/tools/memory/triggers/guardrails
    - [x] 17.2 ReAct 工具调用循环 + 守护（超时、最大工具链、token 上限）
    - [x] 17.3 触发器：`mention / dm / keyword / fallback / always`
    - [x] 17.4 流式回复到适配器
    - _Requirements: US-A1, US-A3, US-A5, US-A6_

- [x] 18. 记忆
    - [x] 18.1 短期滚动窗口
    - [x] 18.2 长期向量库（sqlite-vss 默认实现）
    - [x] 18.3 记忆 namespace：`bot × user × scope`
    - _Requirements: US-A4_

- [x] 19. DSL ↔ Agent 桥
    - [x] 19.1 `$agent 调用 name input$` / `$agent 流式 name input$`
    - [x] 19.2 DSL 处理器可通过 `expose_as_tool: true` 注册成 LLM 工具
    - [x] 19.3 工具调用权限传递（Agent 调 DSL 时继承 Agent 的用户身份）
    - _Requirements: US-M1, US-M2, US-M3_

- [x] 20. 内容安全与注入防护
    - [x] 20.1 外部内容不可信标记（user-content / http-result / file-content）
    - [x] 20.2 system / user 提示严格分离
    - [x] 20.3 Guardrail 钩子：输入/输出过滤（接入可插拔策略）
    - _Requirements: US-X4, US-A6_

## 阶段 P3 — 观测、多平台、多租户

- [-] 21. 观测
    - [ ] 21.1 Prometheus 指标
    - [ ] 21.2 审计日志表 + 检索 CLI
    - [x] 21.3 最小 Web UI（事件流 / KV 浏览器 / 规则命中）— 见独立 spec [`linling-webui`](../linling-webui/design.md)；P3 WebUI 已单独完成并发布，后端通过 `linling_webui.wire` 接入本仓的 EventBus / KVStore / AgentRegistry
    - _Requirements: US-O1, US-O2_

- [ ] 22. 热加载
    - [ ] 22.1 监听 `rules/**/*.ling` 与 `agents/*.yaml` 变更 → 平滑替换
    - _Requirements: US-O3_

- [ ] 23. Postgres 后端
    - [ ] 23.1 同 schema 的 PG 实现 + 迁移脚本（alembic）
    - [ ] 23.2 KV 排行榜用窗口函数实现
    - _Requirements: NFR-2_

- [ ] 24. 多租户
    - [ ] 24.1 存储层加入 `bot_id` 分区键
    - [ ] 24.2 配置中支持多 bot 同进程运行
    - [ ] 24.3 管理员/权限模型：全局管理员 / 群管 / 普通用户
    - _Requirements: US-X1, US-X2_

- [ ] 25. 新适配器
    - [ ] 25.1 HTTP Webhook 适配器（通用平台接入）
    - [ ] 25.2 Discord 适配器
    - [ ] 25.3 飞书适配器
    - [ ] 25.4 微信适配器（IPad 协议或第三方桥）
    - _Requirements: US-P2_

- [ ] 26. 文档与示例
    - [ ] 26.1 DSL 语法参考（自动从 EBNF + registry 生成）
    - [ ] 26.2 工具一览（Python 名 / DSL 名 / LLM schema）
    - [ ] 26.3 迁移指南（QRDic → linling）
    - [ ] 26.4 `bot/` 完整复刻示例
    - _Requirements: NFR-6, S-5_

## 里程碑验收

- [ ] M1 (P0 完成)：CLI + OneBot + DSL v0 + KV 能跑，手写三条规则成功响应。
- [ ] M2 (P1 完成)：`ap migrate qrdic` 成功迁移老项目，黄金测试集通过。
- [ ] M3 (P2 完成)：Agent YAML 可用；DSL 和 Agent 共享一个"查看背包"工具。
- [ ] M4 (P3 完成)：Postgres + 观测 + 热加载 + 至少一个新适配器上线。

## 开放决策（需要你拍板，开工前明确）

- [x] D-1 ~~项目正式名（`AgentDic` / `Kitsune` / `FoxFly` / 其他）~~ → **`linling`**
- [x] D-2 ~~规则文件扩展名（`.ap` / `.dic` / `.fox`）~~ → **`.ling`**
- [ ] D-3 主语言：Python 3.11（当前默认） vs Node/TS
- [ ] D-4 DSL 是否允许用户自定义函数 `函数/函数尾`
- [ ] D-5 默认 LLM 提供方（OpenAI 兼容端点 vs Gemini proxy）
- [ ] D-6 MVP 是否包含 Web UI，还是纯 CLI 先行
