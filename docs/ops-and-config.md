# linling 运维与配置指南 (Operations & Configuration Guide)

本文档是 linling（铃）对话智能体平台的**完整运维与配置手册**。涵盖系统配置模型、环境变量清单、CLI 命令工具箱、生产守护部署、掉线自愈看门狗、零停机热重载、数据备份及排障手册。

---

## 1. 核心概念与配置体系

linling 采用**高内聚单文件优先**的配置设计。一份标准的 `bot.yaml` 即可涵盖 DSL 规则路由、内联大模型（Agent）、轻量存储以及网络适配器。

### 1.1 配置加载层级与优先级

系统在加载配置时严格遵循以下自顶向下的覆盖优先级（高优先级覆盖低优先级）：

```text
1. Shell / 容器导出的环境变量 (os.environ)
2. .env 文件 (启动时自动加载，默认 override=False，不覆盖父进程现有变量)
3. bot.yaml 中的 ${VAR:-default} 动态占位符展开
4. bot.yaml 原文字段 / 代码 Pydantic 模型内建默认值
```

> [!TIP]
> 测试或特殊隔离场景下，若想阻断当前目录 `.env` 的自动载入，可在运行前指定环境变量：
> ```bash
> export LINLING_SKIP_DOTENV=1
> ```

---

## 2. 配置文件完全参考 (`bot.yaml`)

下面是一份包含了所有可用配置段及其默认值、生产推荐设置的完整配置模版：

```yaml
# ==============================================================================
# 1. 基础标识 (Identity)
# ==============================================================================
bot_id: linling                     # 机器人的唯一 ID（用于区分持久化存储空间）
name: 涂山苏苏                      # 机器人对外展示昵称
admin_users:                         # 管理员账号列表（匹配 %管理员% 宏）
  - "${BOT_ADMIN_QQ:-10001}"

# ==============================================================================
# 2. 存储配置 (Storage)
# ==============================================================================
storage:
  # 一键统一数据根目录（开箱即用推荐）：
  # 自动推导并创建：
  #   - KV 存储:        sqlite:///./data/kv.sqlite
  #   - 文件持久化:      ./data/files
  #   - 审计日志:        sqlite:///./data/audit.sqlite
  #   - 定时调度器:      sqlite:///./data/scheduler.sqlite
  #   - 表情/贴纸目录:   ./data/stickers
  data_dir: ./data

  # 若需高级定制或拆分不同存储引擎，可显式覆盖（非必须）：
  # kv: sqlite:///./data/kv.sqlite     # 亦支持 ":memory:"
  # files: ./data/files
  # audit: sqlite:///./data/audit.sqlite
  # scheduler: sqlite:///./data/scheduler.sqlite

# ==============================================================================
# 3. 通信适配器 (Adapters)
# ==============================================================================
adapters:
  # 本地终端交互适配器：无需网络，启动后直接在终端打字交互（开发联调极力推荐）
  - kind: cli

  # OneBot v11 协议适配器（对接 QQ 协议端，如 LLBot、NapCat、Lagrange 等）
  - kind: onebot
    # 反向 WebSocket 地址。若未配置或变量为空，linling 将安全跳过该适配器并降级为 CLI
    ws_url: ${ONEBOT_WS_URL:-ws://127.0.0.1:3003}
    access_token: ${ONEBOT_TOKEN:-}
    remote_image_preflight: true      # 远程图片下载预检（失败自动降级为文本，防止 QQ 拒发）
    remote_image_fallback_text: "[图片加载失败]"

# ==============================================================================
# 4. 规则引擎 (Rules)
# ==============================================================================
rules:
  # 支持 Glob 通配符，自动递归扫描并加载
  - rules/**/*.ling

# ==============================================================================
# 5. 智能体与大模型 (Agent)
# ==============================================================================
agent:
  # [单文件内联模式] 直接在此配置大模型（免去外挂 agents/*.yaml）：
  name: linling
  provider: openai                    # 目前支持 openai 兼容协议（DeepSeek, Kimi, vLLM 等均可）
  model: ${LINLING_MODEL:-deepseek-chat}
  system: |
    你是 linling，一个运行在 Linux 上的智能机器人助手。
    性格温和、机敏，回答精炼扼要。
  temperature: 0.7
  reasoning_effort: null              # 支持 "low" / "medium" / "high"（针对 o1/o3/r1 系列推理模型）
  vision_enabled: false               # 多模态视觉模式（开启后可理解图片并支持表情包管理工具）
  api_key: ${LLM_API_KEY:-${OPENAI_API_KEY:-}}
  base_url: ${LLM_BASE_URL:-${OPENAI_BASE_URL:-https://api.openai.com/v1}}
  tools: []                           # 启用的工具白名单（留空使用默认安全工具）

  # [外置引用模式] 如需复用多 Agent 设定，亦可直接指向文件：
  # default_agent: ./agents/susu.yaml

  # ---- 消息放行与白名单策略 ----
  # 兜底回复：当既不匹配任何 DSL 规则、群聊又未开放 LLM 时的回复。留空 "" 则彻底保持静默
  fallback_reply: ""

  # 群聊 LLM 放行白名单（注意：私聊 DM 永远 100% 开放，不受此项限制）：
  # - 不写或整段注释掉：开箱全开模式（所有加入的群聊均可与 LLM 对话）
  # - []：关闭所有群聊的 LLM 闲聊（仅响应明确的 DSL 指令）
  # - ["123456", "789012"]：仅放行指定群号
  # allowed_scopes:
  #   - "123456789"

  # ---- 私聊多段回复与拟人化延时 ----
  dm_max_replies: 3                   # 单轮私聊回复最大拆分段数
  dm_max_reply_chars: 500             # 单条消息最大字符截断
  multi_reply_delay_min_s: 2.0        # 多条分段消息之间的随机延时下限（秒）
  multi_reply_delay_max_s: 6.0        # 多条分段消息之间的随机延时上限（秒）

  # ---- 群聊聚合批处理 (Group Batch) ----
  group_batch_enabled: true           # 开启群聊消息时间窗口聚合，避免高频刷屏触发大模型
  group_batch_window_s: 8.0           # 聚合滑动窗口大小（秒）
  group_batch_max_messages: 50        # 窗口内最大缓冲消息数
  group_batch_max_chars: 6000         # 窗口内最大累计字符数
  group_batch_require_attention: true # 是否要求提到机器人/特定唤醒词才触发
  group_batch_max_hold_s: 60.0        # 批次在内存中最大保留时间
  group_batch_attention_window_s: 300 # 成功回复后，该用户在 5 分钟内的下次回话自动获得注意力
  group_batch_daily_summary_enabled: true  # 每天首批消息前自动触发历史摘要压缩
  group_batch_daily_summary_keep_recent_turns: 2
  group_batch_bot_names:              # 机器人在群聊中的唤醒别名列表
    - 苏苏
    - 涂山苏苏

  # 轻量注意力探针（二阶段判别）：
  group_batch_attention_probe_enabled: false

# ==============================================================================
# 6. 会话与上下文控制 (Conversation)
# ==============================================================================
conversation:
  max_sessions: 10000                 # 内存活跃会话最大跟踪数
  ttl_seconds: 3600.0                 # 会话超时时间（秒）
  history_turns: 16                   # 持久化短期记忆轮数
  context_max_tokens: 65536           # 上下文预算上限
  summary_trigger_tokens: 60000       # 接近此 token 阈值时触发摘要折叠
  summary_keep_recent_turns: 8        # 折叠时保留的最新对话轮数
  summary_max_tokens: 2000            # 摘要自身占用的最大 token 预算

  # DSL 行动台账 (Action Ledger)：
  ledger_enabled: false               # 开启后记录 DSL 规则执行台账供大模型上下文感知
  ledger_maxlen: 20
  ledger_ttl_seconds: 3600

# ==============================================================================
# 7. 路由器与消息分类 (Classifier & Router)
# ==============================================================================
classifier:
  command_prefixes:                   # 强制视为 DSL 指令的前缀（设为 [] 则全靠隐式触发词）
    - "/"
    - "!"
  block_scope_ids: []                 # 禁用的群号黑名单
  block_sender_ids: []                # 禁用的用户 QQ 黑名单

router:
  max_concurrent_events: 128          # 消息并发处理峰值通道
  enqueue_timeout_s: 1.0              # 队列满时的入队超时时间
  session_timeout_s: 10.0             # 同一会话写锁最大等待时间
  unknown_command_reply: "未知指令，请发送 /help 查看帮助。"
  busy_reply: "当前咨询人数较多，请稍后再试～"
  busy_session_reply: "发送频率过快，请稍慢一些哦。"

# ==============================================================================
# 8. WebUI 管理面板 (可选)
# ==============================================================================
webui:
  host: 0.0.0.0
  port: 8787
  jwt_secret: ${LINLING_WEBUI_JWT_SECRET:-change-me-in-production}
  auth_db_path: ./data/webui_auth.db
  event_buffer_size: 500              # 实时事件查看器最大缓冲行数
  login_rate_per_minute: 5            # 登录防爆破频控
  write_rate_per_minute: 60           # 敏感写操作频控
  cors_origins: []

# ==============================================================================
# 9. 可观测性指标 (Metrics)
# ==============================================================================
metrics:
  enabled: true                       # 开启 Prometheus 11 项核心指标暴露

---

## 3. 环境变量完整对照表

linling 支持通过 `.env` 或系统环境变量覆盖各项配置：

| 环境变量 | 默认值 | 作用说明 |
| :--- | :--- | :--- |
| **`LLM_API_KEY`** / **`OPENAI_API_KEY`** | *(空)* | 主大模型 API Key（自动填入内联 Agent 或 `${...}` 占位） |
| **`LLM_BASE_URL`** / **`OPENAI_BASE_URL`** | `https://api.openai.com/v1` | 主大模型 Base URL（支持 DeepSeek / Kimi / 自建 vLLM 等） |
| **`LINLING_MODEL`** | `gpt-4o-mini` | 覆盖默认大模型型号 |
| **`OPENAI_HTTPS_PROXY`** | *(空)* | 主大模型专用 HTTP/SOCKS5 代理（直连端点无需配置） |
| **`LINLING_VISION_ENABLED`** | `false` | 是否全局启用多模态视觉与表情包支持（需模型本身支持视觉） |
| **`ONEBOT_WS_URL`** | `ws://127.0.0.1:3003` | OneBot v11 反向 WebSocket 上行服务地址 |
| **`ONEBOT_TOKEN`** | *(空)* | OneBot 鉴权 Access Token |
| **`BOT_ADMIN_QQ`** | `10001` | 默认超级管理员 QQ 号（映射 `%管理员%` 宏） |
| **`LINLING_WEBUI_JWT_SECRET`** | *(随机生成)* | WebUI 面板 JWT 签名密钥（生产环境必须固定） |
| **`LOG_LEVEL`** | `INFO` | 日志输出级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` |
| **`LOG_FORMAT`** | `json` | 日志格式：`console`（本地彩色人类可读） / `json`（生产结构化采集） |
| **`ATTENTION_PROBE_API_KEY`** | 同 `LLM_API_KEY` | 二阶段注意力探针独立 Key（用于轻量模型降本） |
| **`ATTENTION_PROBE_BASE_URL`**| 同 `LLM_BASE_URL`| 二阶段注意力探针独立 Base URL |
| **`ATTENTION_PROBE_MODEL`**   | 同主模型 | 二阶段注意力探针模型名 |
| **`LINLING_SKIP_DOTENV`**     | `0` | 置为 `1` 时禁止读取本地 `.env` 文件 |

---

## 4. 日常运维 CLI 工具箱

linling 提供了一站式的命令行工具，通过 `uv run linling <command>` 即可调用：

### 4.1 环境与配置体检 (`linling doctor`)
在部署或调整配置后，运行体检命令排查一切潜在隐患：

```bash
# 自动探测当前目录的 bot.yaml / bot/bot.yaml
uv run linling doctor

# 或显式指定配置文件路径
uv run linling doctor /path/to/custom_bot.yaml
```

**体检覆盖项**：
- [✓] **Python 运行环境**：校验版本是否满足 `>= 3.11`。
- [✓] **配置解析校验**：校验 YAML 语法、字段有效性与 `bot_id` 提取。
- [✓] **存储目录读写**：检查 `data_dir` 权限，自动测试读写创建。
- [✓] **规则编译检查**：预编译所有 `.ling` 文件，输出规则文件数与有效触发器总数。
- [✓] **大模型连通配置**：显示生效的 Provider、Model、Base URL 及脱敏展示的 Key。
- [✓] **网络与适配器就绪度**：检查 CLI / OneBot 配置与上行目标。

### 4.2 零参智能启动 (`linling run`)
`linling run` 会按以下优先级自动探测配置文件：
`bot.yaml` $\rightarrow$ `bot/bot.yaml` $\rightarrow$ `config.yaml` $\rightarrow$ `examples/minimal_bot/bot.yaml`。

```bash
# 1. 零参数开箱启动（自动推导配置文件）
uv run linling run

# 2. 启动并同时加载 WebUI 仪表盘
uv run linling run --webui --webui-port 8787

# 3. 仅启动 CLI 终端交互模式（跳过网络连接，用于本地单人交互联调）
uv run linling run --only-adapters cli

# 4. 生产指定配置启动
uv run linling run bot/bot.yaml
```

### 4.3 新机器人脚手架初始化 (`linling init`)
在空目录或新项目中一秒生成开箱即用的机器人模板：

```bash
# 在当前目录生成
uv run linling init

# 在指定子目录生成
uv run linling init my_new_bot
```

生成结构：
```text
my_new_bot/
├── bot.yaml          # 单文件大一统配置
├── rules/
│   └── main.ling     # 基础 DSL 词条与问候指令
└── .env.example      # 环境变量模版
```

### 4.4 语法静态检查 (`linling lint`)
用于 CI/CD 流水线或编写规则时的语法和合规校验：

```bash
uv run linling lint "rules/**/*.ling"
```

---

## 5. 生产环境部署方案

### 方案 A：Systemd 守护进程（Linux 主机推荐）

1. 创建服务定义文件 `/etc/systemd/system/linling.service`：

```ini
[Unit]
Description=linling Chatbot Service
After=network.target docker.service
Wants=docker.service

[Service]
Type=simple
User=wilsen
WorkingDirectory=/home/wilsen/apps/apps/linling
EnvironmentFile=/home/wilsen/apps/apps/linling/.env
ExecStart=/home/wilsen/.local/bin/uv run linling run bot/bot.yaml --webui
Restart=on-failure
RestartSec=5s
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=15s
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

2. 启用并启动服务：
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now linling
sudo systemctl status linling
```

3. 查看实时结构化日志：
```bash
journalctl -u linling -f -o cat
```

---

### 方案 B：结合 LLBot 的容器化联动编排

生产对接 QQ 时，建议使用官方推荐的 LLBot (LLOneBot) 容器。

#### 1. 协议端编排 (`docker/llbot/docker-compose.yml`)

```yaml
version: "3.8"

services:
  pmhq:
    image: ghcr.io/linyuchen/pmhq:latest
    container_name: llbot-pmhq
    restart: unless-stopped
    volumes:
      - ~/.llbot/qq_volume:/data
    network_mode: "host"

  llbot:
    image: ghcr.io/linyuchen/llonebot:latest
    container_name: llbot
    restart: unless-stopped
    depends_on:
      - pmhq
    volumes:
      - ~/.llbot/llbot_config:/root/.config/QQ/Crashpad
    environment:
      - ONEBOT_WS_PORT=3003
    network_mode: "host"
```

#### 2. 一键启动与维护脚本 (`./start.sh`)

仓库根目录下提供了交互式启动与清理脚本 [`start.sh`](../start.sh)：
- **自动杀死残留进程**：避免 `linling run` 多开抢占端口或账号。
- **协议端状态检查**：自动探测 `llbot-pmhq` 是否在线，若离线自动唤醒。
- **模式切换**：支持 1) 仅CLI本地联调、2) 完整服务联动、3) 快速重启协议端、4) 开启掉线自愈看门狗。

---

### 方案 C：掉线自愈看门狗 (`scripts/llbot_watchdog.sh`)

由于 QQ 协议端可能因网络抖动、风控策略而离线，linling 提供了专用的探活与自愈守护脚本 [`scripts/llbot_watchdog.sh`](../scripts/llbot_watchdog.sh)。

**工作机制**：
1. 每隔 120 秒通过 OneBot `get_status` 发送轻量心跳探活。
2. 若探测离线：记录当前出口公网 IP 至 `./data/qqbot_offline.log` 进行取证。
3. 自动执行 `docker compose restart pmhq` 触发无感知快速免密登录恢复。
4. 连续超过最大自愈尝试（默认 2 次）仍离线则停手，报警提示人工介入扫码，杜绝频繁重启引发的风控风暴。

**启动方式**：

```bash
# 方式 1：常驻后台运行
nohup ./scripts/llbot_watchdog.sh --loop >/dev/null 2>&1 &

# 方式 2：写入 Crontab 定时轮询
*/2 * * * * cd /home/wilsen/apps/apps/linling && ./scripts/llbot_watchdog.sh >> ./data/watchdog.log 2>&1
```

---

## 6. 零停机规则热重载 (Hot Reload)

在生产环境中修改 `.ling` 词条规则后，**无需重启进程，亦不会丢失任何正在处理中的会话状态**。

### 6.1 SIGHUP 信号触发热重载
向运行中的 linling 进程发送 `SIGHUP` 信号：

```bash
pkill -HUP -f "linling run"
```

### 6.2 WebUI 管理接口触发
向 WebUI API 发送 POST 请求：

```bash
curl -X POST http://127.0.0.1:8787/api/rules/reload \
     -H "Authorization: Bearer <YOUR_ADMIN_JWT_TOKEN>"
```

### 6.3 热重载安全保护机制
- **正在处理中的会话保护**：旧请求继续在当前 VM 执行完毕，后续新消息无缝切到新规则。
- **解析灾难防护**：若新修改的文件存在严重语法错误导致**全部**无法解析，系统将拒绝应用并保持旧规则集，同时打出 `warning` 级别日志。
- **部分容错机制**：若修改了 10 个文件，仅 1 个报错，系统将应用其余 9 个文件的修改，并在日志/响应报表中明确列出错误行号与原因。

---

## 7. 数据持久化、备份与灾备

### 7.1 数据目录拓扑规划
在配置了 `storage.data_dir: ./data` 的情况下，系统运行数据全部沉淀在 `./data` 中：

```text
data/
├── kv.sqlite             # 核心 KV 存储（用户金币、签到数据、DSL 变量、苏苏好感度等）
├── audit.sqlite          # 审计追踪数据（每条消息的处理耗时、命中规则、LLM token 消耗）
├── scheduler.sqlite      # 延时与定时任务状态（掉线重启后可恢复）
├── files/                # 动态生成的富文本卡片、图片与持久化静态资源
├── stickers/             # 收藏的表情包持久化存储
└── webui_auth.db         # WebUI 运维管理员账户数据
```

### 7.2 SQLite 在线热备份 (Zero-Downtime Backup)

linling 采用 SQLite WAL 模式，读写互不阻塞，支持热备份：

```bash
#!/usr/bin/env bash
# 每日备份脚本示例：backup.sh
BACKUP_DIR="./backup/$(date +%F)"
mkdir -p "$BACKUP_DIR"

# 在线安全拷贝，杜绝文件损坏
sqlite3 ./data/kv.sqlite ".backup '$BACKUP_DIR/kv.sqlite'"
sqlite3 ./data/scheduler.sqlite ".backup '$BACKUP_DIR/scheduler.sqlite'"

# 打包归档
tar -czf "$BACKUP_DIR.tar.gz" -C "$BACKUP_DIR" .
rm -rf "$BACKUP_DIR"
echo "Backup completed: $BACKUP_DIR.tar.gz"
```

---

## 8. 常见故障排查手册 (Troubleshooting)

### Q1: 运行 `linling run` 提示找不到配置文件？
**现象**：`linling run: no config file found.`
**原因**：当前工作目录下未找到 `bot.yaml` 或 `bot/bot.yaml`。
**解决**：
1. 检查执行命令所在的目录是否正确。
2. 或运行 `uv run linling init` 生成新配置。
3. 或显式指定绝对/相对路径：`uv run linling run /path/to/bot.yaml`。

### Q2: 机器人对群消息没有任何响应？
**排查排障步骤**：
1. **检查群聊白名单**：检查 `bot.yaml` 中的 `agent.allowed_scopes`。如果设置了 `[]`，所有群聊的 LLM 兜底均被禁用；若设置了具体群号列表，非列表群不会触发 LLM。注释掉该项可对所有群开放。
2. **检查指令前缀**：如果输入的是指令（如 `/help`），检查 `classifier.command_prefixes` 是否包含了对应的引导符号。
3. **检查 OneBot 适配器连接**：运行 `uv run linling doctor` 检查是否成功连入 `ws://127.0.0.1:3003`。
4. **日志追踪**：检索日志中对应消息的 `trace_id`：
   ```bash
   # 查看最后一条分发的判决记录
   grep "router.dispatched" ./data/audit.sqlite
   ```

### Q3: LLM 请求频繁出现 ReadTimeout 超时？
**原因**：部分推理大模型（如 R1 或大参数视觉模型）首字思考时间长，或者二阶段探针超时时间较短。
**解决**：
1. 若开启了 `group_batch_attention_probe_enabled: true`，探针内置硬超时为 8 秒。建议在 `bot.yaml` 中关闭探针 (`false`)，由主模型统一处理。
2. 检查 `.env` 中的 `OPENAI_HTTPS_PROXY`，确保代理节点网络低延迟通畅。

### Q4: 提示 `database is locked` 锁库报错？
**原因**：并发读写过高或存储文件位于不支持 POSIX 锁的网络共享文件系统（如 NFS/SMB）上。
**解决**：
- 确保 `data/` 目录位于本机快速存储（SSD/NVMe）上。
- SQLite 已自动开启 WAL 模式，如需分流，可在 `bot.yaml` 的 `storage:` 段中将写密集的 `audit` 拆分至独立数据库或关闭非核心组件的冗余事务。

### Q5: 规则修改后使用 `pkill -HUP` 无效？
**原因**：新规则存在语法错误被安全熔断回滚，或者修改的文件不在 `rules` 通配路径匹配范围内。
**解决**：
- 运行 `uv run linling lint "rules/**/*.ling"` 检查是否有语法报错。
- 查看进程 stdout 日志中的 `bootstrap.rules_reload_failed` 或 `bootstrap.rules_partial_reload` 警告信息。
