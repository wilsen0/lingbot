# Minimal Bot Example

这是一个彻底开箱即用的最小 linling 机器人示例项目，演示了 Ling DSL 规则与单文件内联大模型协同工作的极简配置。

---

## 目录结构

```
examples/minimal_bot/
├── bot.yaml            # 单文件核心配置（内联 AI 助手、统一数据目录、CLI 终端适配器）
├── rules/
│   └── main.ling       # 基础 Ling DSL 规则（关键词匹配与持久化读写示例）
└── README.md           # 说明文档
```

---

## 快速开始

### 1. 一键健康体检（诊断配置与依赖）

```bash
uv run linling doctor examples/minimal_bot/bot.yaml
```

### 2. 检查规则语法

```bash
uv run linling lint examples/minimal_bot/rules/main.ling
```

### 3. 运行机器人（终端交互模式）

```bash
uv run linling run examples/minimal_bot/bot.yaml
```

运行后直接在终端输入文本测试：
- 输入 `ping`：触发 DSL 规则回复 `pong!`
- 输入 `你好`：触发 DSL 规则回复打招呼
- 输入 `签到`：触发 DSL 读写持久化状态
- 输入其他任意内容：兜底进入 AI 助手大模型对话（若未配置 `LLM_API_KEY`，则自动以安全回声模式运行）
