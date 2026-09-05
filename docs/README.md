# linling 文档导航 / Documentation

欢迎阅读 linling（铃）对话智能体平台的技术文档。本文档索引汇总了仓库内的所有规范、技术架构、语法参考和运维指南。

---

## 目录索引

```text
docs/
├── README.md                  # 本导航文档
├── architecture.md            # 系统技术架构、数据流与组件关系
├── ops-and-config.md          # 系统运维与配置完整参考指南
├── marketplace.md             # 玩家摊位交易系统（规则、图卡、事务原子性）
├── susu_sticker_prompts.md    # 涂山苏苏表情包与道具图生图提示词规范
├── dsl/                       # DSL 规则引擎
│   ├── grammar.md             # DSL 语法完整参考（含实现状态对照）
│   ├── qrspeed-comparison.md  # QRSpeed / QRDicPro 语法兼容性实测
│   └── external-references/   # 外部 DSL 语料参考与测试用例
├── webui/                     # linling-webui 管理面板
│   ├── api.md                 # REST 与 WebSocket 接口规范
│   ├── api-types.md           # 前端 TypeScript 类型生成与 OpenAPI 漂移检测
│   ├── theme.md               # 狐妖小红娘情缘主题视觉设计规范
│   └── a11y.md                # 无障碍（A11y）支持与承诺
└── observability/             # 可观测性与告警
    ├── README.md              # 监控指标目录与接入指南
    ├── prometheus.yml         # Prometheus 采集配置
    ├── alerts.yml             # PromQL 告警规则定义
    └── grafana-dashboard.json # Grafana 监控大盘定义
```

---

## 1. 核心架构与设计

- [技术架构 (Architecture)](./architecture.md)
  系统运行时完整数据通路：从 OneBot / CLI 适配器进入 EventBus，经 Router 路由到 DSL VM 或 Agent Runtime，再将 Action 交付给出口与审计。
- [顶层设计方案 (.kiro/specs/linling/)](../.kiro/specs/linling/design.md)
  系统高层设计、包边界约定与演进路线图。
- [轻量注意力探针 (.kiro/specs/lightweight-attention-probe/)](../.kiro/specs/lightweight-attention-probe/design.md)
  群聊消息聚合与二阶段注意力判别机制设计。
- [用户画像记忆体系 (.kiro/specs/user-profile-memory/)](../.kiro/specs/user-profile-memory/design.md)
  短期会话历史、会话摘要与长期画像三层记忆结构设计。

---

## 2. DSL 规则引擎

- [DSL 语法参考 (Grammar)](./dsl/grammar.md)
  `.ling` 规则文件的完整语法规范，包括触发器、控制流分支、算术表达式、变量插值、内置工具调用及状态。
- [QRSpeed 语法对照 (Comparison)](./dsl/qrspeed-comparison.md)
  基于 `dicpro.txt` 生产词库与公开样例的比对分析，标注各项原版特性的兼容实现状态。
- [外部参考用例 (External References)](./dsl/external-references/ziyii01/)
  包含签到、留言板、商店背包、转盘等典型场景的原始规则样例，用于验证 DSL 解析器与 VM 的兼容性。

---

## 3. WebUI 管理面板

- [WebUI 概览与快速启动](../packages/webui/README.md)
  FastAPI + Vue 3 SPA 的架构概述、开发启动、鉴权与配置说明。
- [接口契约规范 (API)](./webui/api.md)
  REST 端点（鉴权、Bot 管理、事件流、KV 浏览、审计日志）与 WebSocket 通信协议。
- [前端类型与契约对齐 (API Types)](./webui/api-types.md)
  基于 OpenAPI snapshot 和 `openapi-typescript` 的端到端类型安全与双向漂移阻断机制。
- [主题设计规范 (Theme Tokens)](./webui/theme.md)
  以「狐妖小红娘 · 苦情树 / 铃铛 / 幻粉雾」为核心的色彩体系、字体层级、动效降级与装饰开关。
- [可访问性 (Accessibility)](./webui/a11y.md)
  WCAG 2.1 AA 规范对齐、Reduced-motion 动效压制、键盘导航与触达区域定义。

---

## 4. 业务特性与素材

- [玩家摊位交易系统 (Marketplace)](./marketplace.md)
  全服物品交易市场：DSL 触发指令、Pillow 动态 150px 摊位卡片生成、字体自动发现、SQLite KV 事务原子性与防刷风控。
- [苏苏表情包与道具生图规范 (Sticker Prompts)](./susu_sticker_prompts.md)
  统一 Q 版折耳金发狐耳少女画风的 AI 生图 Prompt、宫格切图对照表与素材布局。

---

## 5. 运维、部署与可观测性

- [运维与配置指南 (Operations & Configuration Guide)](./ops-and-config.md)
  系统配置模型、完整 `bot.yaml` 参数详解、环境变量清单、CLI 命令工具箱、Systemd/Docker 部署、规则零停机热重载与排障手册。
- [可观测性体系 (Observability)](./observability/README.md)
  Structlog 链路追踪（`trace_id`）、SQLite 审计留痕、Prometheus 11 项核心指标说明与 Grafana 导入指南。
- [LLBot 协议端部署](../docker/llbot/docker-compose.yml)
  LLBot (LLOneBot) 容器化部署、QQ 扫码登录与反向 WebSocket 对接配置。
- [一键启停脚本](../start.sh)
  自带残留进程探测清理、容器状态健康检查与一键前台/后台启动管理。
