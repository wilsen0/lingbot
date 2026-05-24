# linling

> 对话智能体平台：**LLM Agent 框架 + 中文 DSL 解释器 + 多平台适配**。
> 旨在覆盖两类使用者：
> - 不写代码的 DIY 玩家（中文规则脚本配机器人）
> - Agent 开发者（工具调用 / 记忆 / 多提供方）

项目名 `linling`（铃），由 [QRDic](./QRDic) 项目重写演化而来。目标是把原
Android + QRSpeed 中文 DSL 机器人迁移成一个标准化、可跨 IM 平台
（QQ / 微信 / 飞书 / Discord / HTTP）运行的 Python 平台。

规则文件使用 `.ling` 扩展名。

## 状态

处于 P0 骨架建设阶段。Spec 在 [`.kiro/specs/linling/`](./.kiro/specs/linling/)。

## 包结构

```
packages/
├── core/             # 内核：事件、工具注册表、路由、存储、调度
├── dsl/              # DSL 解析器 + 虚拟机 + 迁移器
├── agent/            # Agent 框架：LLM 抽象、记忆、工具调用
├── adapters/
│   ├── onebot/       # QQ 适配器（OneBot v11）
│   └── cli/          # 本地终端调试适配器
├── tools-stdlib/     # 官方工具集
└── cli/              # `linling` 命令行
```

## 开发

```bash
# 安装全部 workspace + dev deps
uv sync --all-packages

# 运行所有测试
uv run pytest

# 代码检查
uv run ruff check .
uv run ruff format --check .
uv run mypy

# 命令行
uv run linling version
```

## 启动服务

下面所有命令都基于仓库自带的 [`bot/bot.yaml`](./bot/bot.yaml)（涂山苏苏：完整规则集 + OneBot/CLI 适配器）。
本地开发和线上部署用同一份配置——dev/prod 同源是底线，避免「本地能跑，上线就崩」。

### 0. 一次性准备

```bash
# 装依赖（首次或拉了新代码后跑一次就够）
uv sync --all-packages

# 复制一份 .env，填上你的 LLM key —— CLI 启动时会自动加载到环境变量
cp .env.example .env
$EDITOR .env
```

`.env.example` 里给了几条占位:

```dotenv
# OpenAI 兼容端点；任何 provider（OpenAI / Azure / Kimi / vLLM）都通过这两项切换
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.openai.com/v1

# 想覆盖默认模型（Kimi 端点请用 kimi-for-coding）
LINLING_MODEL=
```

每个 agent 在 `agents/*.yaml` 的 `provider_config` 里通过 `${VAR}` 引用这些值，所以同一个 bot
可以让不同 agent 走不同 provider（在它自己的 YAML 里把 `api_key` / `base_url` 写死即可）。
Shell 里已经 `export` 的同名变量优先生效，`.env` 是 fallback。

> 想接 Kimi `/coding/v1`：把 `OPENAI_BASE_URL` 换成 `https://api.kimi.com/coding/v1`、
> `OPENAI_API_KEY` 填 Kimi key、`LINLING_MODEL=kimi-for-coding`，其余不动。
> 想接 OpenAI 官方：填 `OPENAI_API_KEY` 一项就够了。

### 1. 终端里直接聊（CLI REPL）

```bash
uv run linling run bot/bot.yaml
```

启动后直接在终端输入消息，回车发送，`Ctrl-C` 退出。

### 2. CLI REPL + WebUI 同一个进程（**推荐**，日常都用这个）

```bash
uv run linling run bot/bot.yaml --webui --webui-port 8787
```

浏览器打开 <http://127.0.0.1:8787> 看灵玉 / 因缘 / 红娘 / 命格四个面板，
终端照样可以打字聊。**想看到「聊天数据 / 事件流」就用这条命令**——必须走
adapter 循环，事件才会被采集进 WebUI。

### 3. 让手机或同事的电脑也能访问

把监听地址改成 `0.0.0.0`：

```bash
uv run linling run bot/bot.yaml --webui --webui-host 0.0.0.0 --webui-port 8787
```

然后手机浏览器访问 `http://<本机局域网IP>:8787`（用 `ip addr` / `ifconfig` 查 IP）。

### 4. 接 QQ（OneBot v11）

在 `bot.yaml` 的 `adapters:` 段追加：

```yaml
adapters:
  - kind: cli
  - kind: onebot
    ws_url: ${ONEBOT_WS_URL}     # 例如 ws://127.0.0.1:3001
    access_token: ${ONEBOT_TOKEN}
```

启动方式跟前面完全一样：

```bash
export ONEBOT_WS_URL='ws://127.0.0.1:3001'
export ONEBOT_TOKEN=''            # 没设 token 就留空
uv run linling run bot/bot.yaml
```

> linling 自己**不实现 QQ 协议**，需要外面有一个 OneBot v11 实现
> （NapCat / Lagrange / gocq）翻译流量。NapCat 的 Docker 部署
> （含 onebot11 配置 / 扫码登录 / 升级回滚）见
> [`docs/deployment/napcat.md`](./docs/deployment/napcat.md)。

### 常用小技巧

```bash
# 改了 .ling 规则不想重启？发 SIGHUP 热重载（WebUI 面板也有按钮）
kill -HUP $(pgrep -f 'linling run')

# 调日志级别
LOG_LEVEL=DEBUG   uv run linling run bot/bot.yaml   # 调试
LOG_LEVEL=WARNING uv run linling run bot/bot.yaml   # 安静
```

bot 工程目录：[`bot/`](./bot/)。

<details>
<summary>仅前端开发者：只起 WebUI 不跑 adapter</summary>

```bash
uv run linling serve webui --bot bot/bot.yaml --host 0.0.0.0 --port 8787
```

这条命令**不会**调 `bot.start()`，只是把 KV / agent / 审计读取层挂到 WebUI 上。
也就是说：

- ❌ 「因缘」事件流是空的（没人往 bus 里 publish 事件）
- ❌ 「命格」审计基本空（router 不会跑）
- ✅ 「灵玉」KV 浏览正常
- ✅ 「红娘」对话页可以发消息（走 WebUI 自己的 dispatcher）

它是为「改前端样式 / 调试 API、不想被 stdin 卡住」准备的。**普通用户和想看
聊天数据的运维都应该用上面的 `linling run --webui`。**
</details>

## 文档

- [技术架构 / Architecture](docs/architecture.md) — 当前真实运行的组件与数据流
- [Requirements](.kiro/specs/linling/requirements.md)
- [Design](.kiro/specs/linling/design.md)
- [Tasks](.kiro/specs/linling/tasks.md)
