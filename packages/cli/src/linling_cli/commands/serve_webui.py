"""`linling serve webui` — boot the WebUI HTTP server.

用法::

    linling serve webui
    linling serve webui --host 0.0.0.0 --port 8787
    linling serve webui --bot bot/bot.yaml

与 ``linling run --webui`` 的区别：

- ``linling run --webui`` 既跑 bot 的 adapter，又跑 WebUI；适合生产。
- ``linling serve webui`` 专供前端调试：只启动 WebUI，不拉起 adapter 循环。
  传 ``--bot bot.yaml`` 时，它同样完整 bootstrap 一个 bot（KV、Agent、
  Router、审计），只是不调 ``bot.start()`` —— 因此前端的灵玉/因缘/红娘
  /命格四个 Tab 都有真数据，但 cli 不会从 stdin 吃消息。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path

import typer
import uvicorn
from linling_core.config import BotConfig
from linling_webui.app import create_app
from linling_webui.config import WebUIConfig


def serve_webui(
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址"),
    port: int = typer.Option(8787, "--port", help="监听端口"),
    bot: Path | None = typer.Option(  # noqa: B008
        None,
        "--bot",
        help="bot.yaml 路径；会完整 bootstrap 这个 bot 并把 KV / agent / 审计挂到 WebUI。",
    ),
    reload: bool = typer.Option(False, "--reload", help="开启热重载（开发用）"),
) -> None:
    """Start the linling WebUI HTTP server."""
    if bot is None:
        # No bot attached: serve a dashboard with no data sources.
        config = WebUIConfig()
        config.host = host
        config.port = port
        app = create_app(config)
        typer.echo(f"linling-webui listening on http://{config.host}:{config.port}")
        uvicorn.run(app, host=config.host, port=config.port, reload=reload, log_level="info")
        return

    # Bot attached: bootstrap + wire. The bootstrap is async so we drive
    # it on an explicit loop instead of uvicorn.run (which would create
    # its own loop after the bootstrap finished, stranding live objects).
    asyncio.run(_serve_with_bot(bot, host=host, port=port))


async def _serve_with_bot(bot_path: Path, *, host: str, port: int) -> None:
    from linling_cli.bootstrap import bootstrap_bot  # noqa: PLC0415
    from linling_cli.wire_webui import attach_bot_to_webui  # noqa: PLC0415

    cfg = BotConfig.from_yaml(bot_path)
    base_dir = bot_path.parent
    running_bot = await bootstrap_bot(cfg, base_dir=base_dir)

    webui_cfg = WebUIConfig.from_bot_yaml_section(cfg.webui or None)
    webui_cfg.host = host
    webui_cfg.port = port

    app = create_app(webui_cfg)
    attach_bot_to_webui(app, running_bot)

    agents = list(running_bot.agents.keys())
    adapter_kind = cfg.adapters[0].kind if cfg.adapters else "none"
    typer.echo(
        f"Wired bot '{cfg.bot_id}' ({adapter_kind}) — "
        f"KV attached, {len(agents)} agent(s): {agents or '-'}"
    )
    typer.echo(f"linling-webui listening on http://{webui_cfg.host}:{webui_cfg.port}")

    uv_cfg = uvicorn.Config(app, host=webui_cfg.host, port=webui_cfg.port, log_level="info")
    server = uvicorn.Server(uv_cfg)
    try:
        await server.serve()
    finally:
        # Use the bot's full ``stop()`` so KV, scheduler store, LLM
        # provider httpx clients all get closed. Adapters were never
        # ``start()``-ed so cancellation is a no-op there, but the
        # other resources still need cleanup.
        with suppress(Exception):
            await running_bot.stop()
