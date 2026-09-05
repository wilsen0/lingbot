"""`linling doctor` — diagnostic command for environment, configuration, and connectivity."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer
from linling_core.config import BotConfig
from linling_dsl import parse as parse_dsl

from linling_cli.commands.run import _find_default_config


def doctor(
    config: Path | None = typer.Argument(  # noqa: B008
        None,
        help="Path to bot.yaml (auto-detects if omitted)",
    ),
) -> None:
    """Run health and environment diagnostics."""
    typer.echo("\n🩺 linling doctor — 正在诊断环境与配置...\n")
    all_ok = True

    # 1. Python 环境检查
    py_ver = sys.version.split()[0]
    if sys.version_info >= (3, 11):  # noqa: UP036
        typer.secho(f"  [✓] Python 环境: {py_ver} (>= 3.11 满足)", fg=typer.colors.GREEN)
    else:
        typer.secho(f"  [✗] Python 环境: {py_ver} (要求 >= 3.11)", fg=typer.colors.RED)
        all_ok = False

    # 2. 配置文件检测
    target_config = config
    if target_config is None:
        target_config = _find_default_config()

    if target_config is None or not target_config.is_file():
        typer.secho(
            "  [✗] 配置文件: 未找到 bot.yaml（可执行 `linling init` 快速创建）", fg=typer.colors.RED
        )
        typer.echo("\n诊断结束，请根据提示修复后再试。")
        raise typer.Exit(code=1)

    target_config = target_config.resolve()
    base_dir = target_config.parent

    try:
        cfg = BotConfig.from_yaml(target_config)
        typer.secho(
            f"  [✓] 配置文件: {target_config.name} (解析成功, bot_id='{cfg.bot_id}')",
            fg=typer.colors.GREEN,
        )
    except Exception as exc:
        typer.secho(f"  [✗] 配置文件解析失败: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    # 3. 存储目录检查
    data_dir = Path(cfg.storage.data_dir)
    if not data_dir.is_absolute():
        data_dir = base_dir / data_dir

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        test_file = data_dir / ".doctor_probe"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
        typer.secho(f"  [✓] 数据存储目录: {data_dir} (可读写)", fg=typer.colors.GREEN)
    except Exception as exc:
        typer.secho(f"  [✗] 数据存储目录不可写: {data_dir} ({exc})", fg=typer.colors.RED)
        all_ok = False

    # 4. 规则文件检查
    rule_files = 0
    total_handlers = 0
    rule_errors: list[str] = []
    for pattern in cfg.rules:
        for p in sorted(base_dir.glob(pattern)):
            if p.is_file():
                rule_files += 1
                try:
                    script = parse_dsl(p.read_text(encoding="utf-8"), filename=str(p), strict=False)
                    total_handlers += len(script.handlers)
                except Exception as exc:
                    rule_errors.append(f"{p.name}: {exc}")

    if rule_errors:
        typer.secho(
            f"  [!] 规则文件: 发现 {len(rule_errors)} 处语法错误: {rule_errors[:3]}",
            fg=typer.colors.YELLOW,
        )
    elif rule_files > 0:
        typer.secho(
            f"  [✓] 规则系统: 加载 {rule_files} 个规则文件 (共 {total_handlers} 条词条/指令)",
            fg=typer.colors.GREEN,
        )
    else:
        typer.secho(
            "  [-] 规则系统: 未检测到任何 .ling 规则文件 (仅大模型对话)", fg=typer.colors.CYAN
        )

    # 5. LLM API Key 连通性预检
    llm_key = (
        cfg.agent.api_key
        or os.environ.get("LLM_API_KEY", "")
        or os.environ.get("OPENAI_API_KEY", "")
    ).strip()
    model_name = cfg.agent.model or os.environ.get("LINLING_MODEL", "gpt-4o-mini")

    if llm_key and not llm_key.startswith("${"):
        base_url = (
            cfg.agent.base_url
            or os.environ.get("LLM_BASE_URL", "")
            or os.environ.get("OPENAI_BASE_URL", "")
            or "https://api.openai.com/v1"
        )
        masked_key = llm_key[:4] + "..." + llm_key[-4:] if len(llm_key) > 8 else "***"
        typer.secho(
            f"  [✓] 大模型配置: {model_name} @ {base_url} (Key: {masked_key})",
            fg=typer.colors.GREEN,
        )
    else:
        typer.secho(
            "  [-] 大模型配置: 未检测到 LLM_API_KEY（将自动以纯 DSL 规则/回声模式运行）",
            fg=typer.colors.CYAN,
        )

    # 6. 适配器状态
    for adapter in cfg.adapters:
        if adapter.kind == "cli":
            typer.secho("  [✓] 适配器 [cli]: 终端交互模式就绪", fg=typer.colors.GREEN)
        elif adapter.kind == "onebot":
            ws = (adapter.ws_url or "").strip()
            if ws and not ws.startswith("${"):
                typer.secho(f"  [✓] 适配器 [onebot]: 上行目标 {ws}", fg=typer.colors.GREEN)
            else:
                typer.secho(
                    "  [-] 适配器 [onebot]: ws_url 未配置（启动时将自动静默跳过）",
                    fg=typer.colors.CYAN,
                )

    if all_ok:
        typer.secho(
            "\n✨ 状态正常！直接输入 `uv run linling run` 即可启动机器人。\n",
            fg=typer.colors.GREEN,
            bold=True,
        )
    else:
        typer.secho("\n⚠️ 存在部分配置项需要关注，请参考上方提示处理。\n", fg=typer.colors.YELLOW)
