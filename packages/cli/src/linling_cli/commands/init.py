"""`linling init` — scaffold a clean, out-of-the-box bot project."""

from __future__ import annotations

from pathlib import Path

import typer

_DEFAULT_BOT_YAML = """# linling 机器人主配置文件
bot_id: {bot_id}
name: {bot_name}

# 存储根目录：自动统一管理 ./data 下的 kv.sqlite, audit.sqlite, scheduler.sqlite
storage:
  data_dir: ./data

# 交互适配器（默认启用本地终端交互模式）
adapters:
  - kind: cli
  # 如需接入 QQ / OneBot v11，取消下方注释并配置环境变量 ONEBOT_WS_URL
  # - kind: onebot
  #   ws_url: ${ONEBOT_WS_URL}
  #   access_token: ${ONEBOT_TOKEN}

# 规则文件匹配路径
rules:
  - rules/**/*.ling

# AI 智能体配置（单文件内联模式，无需额外文件）
agent:
  model: ${LINLING_MODEL:-gpt-4o-mini}
  system: |
    你是一个友善、机智的 AI 助手 {bot_name}。
    用精炼、风趣的语言回答用户的问题。
"""

_DEFAULT_RULE_LING = """&&<配置>兼容模式:是

# 基础响应词条
ping
pong!

你好
你好呀！很高兴见到你～输入任意其他内容，我会交给 AI 助手回复你哦。

# 状态读写示例
签到
如果:$读 签到/%QQ% 0$==1
今天已经签到过啦，明天再来吧！
返回
如果尾
$写 签到/%QQ% 1$
签到成功！灵石 +10
"""

_DEFAULT_ENV_EXAMPLE = """# 大模型 API 密钥与基础地址 (支持 OpenAI、DeepSeek、Moonshot、通义千问等兼容接口)
LLM_API_KEY=
LLM_BASE_URL=https://api.openai.com/v1
LINLING_MODEL=gpt-4o-mini

# OneBot v11 适配器 (若仅在终端本地交互可留空)
ONEBOT_WS_URL=
ONEBOT_TOKEN=
"""


def init_cmd(
    directory: Path = typer.Argument(  # noqa: B008
        Path("."),
        help="Target directory to initialize bot in (default: current directory)",
    ),
    name: str = typer.Option("MiniBot", "--name", help="Name of your bot"),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing files if present"),
) -> None:
    """Initialize a clean, self-contained bot project."""
    target = directory.resolve()
    target.mkdir(parents=True, exist_ok=True)

    bot_yaml = target / "bot.yaml"
    rules_dir = target / "rules"
    rules_file = rules_dir / "main.ling"
    env_example = target / ".env.example"

    if bot_yaml.exists() and not force:
        typer.secho(
            f"❌ 目录 {target} 下已存在 bot.yaml。如需覆盖请添加 `--force` 参数。",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    # 1. 写入 bot.yaml
    bot_id = target.name if target.name != "." else "my_bot"
    content = _DEFAULT_BOT_YAML.replace("{bot_id}", bot_id).replace("{bot_name}", name)
    bot_yaml.write_text(content, encoding="utf-8")

    # 2. 写入 rules/main.ling
    rules_dir.mkdir(parents=True, exist_ok=True)
    rules_file.write_text(_DEFAULT_RULE_LING, encoding="utf-8")

    # 3. 写入 .env.example
    env_example.write_text(_DEFAULT_ENV_EXAMPLE, encoding="utf-8")

    typer.secho(f"\n🎉 机器人项目初始化成功: {target}\n", fg=typer.colors.GREEN, bold=True)
    typer.echo("生成的文件:")
    typer.echo("  ├── bot.yaml          (单文件大一统配置)")
    typer.echo("  ├── rules/main.ling   (Ling DSL 词条规则)")
    typer.echo("  └── .env.example      (环境变量配置模版)")

    typer.secho("\n快速启动方式:", fg=typer.colors.CYAN, bold=True)
    if directory != Path("."):
        typer.echo(f"  cd {directory}")
    typer.echo("  cp .env.example .env    # 填入大模型 API Key（可选，不配也能跑规则模式）")
    typer.echo("  uv run linling run      # 直接启动终端交互\n")
