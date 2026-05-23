"""`linling run bot.yaml` — boot a bot (optionally with the WebUI attached).

Plain mode starts adapters and runs forever until Ctrl-C. With
``--webui`` we additionally serve :mod:`linling_webui` on the same
event loop, and all inbound events mirror into the WebUI's live
buffer — useful for interactive development.
"""

from __future__ import annotations

import asyncio
import signal
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
import typer
from linling_core.config import BotConfig

from linling_cli.bootstrap import RunningBot, bootstrap_bot

if TYPE_CHECKING:
    import uvicorn

logger = structlog.get_logger(__name__)

# Sentinel defaults so we can tell when an operator explicitly supplied
# a CLI flag versus relying on ``bot.yaml``'s ``webui:`` block. The
# values themselves match what a user probably types.
_DEFAULT_WEBUI_HOST = "127.0.0.1"
_DEFAULT_WEBUI_PORT = 8787


def run(
    config: Path = typer.Argument(  # noqa: B008
        ...,
        help="Path to bot.yaml",
        exists=True,
        dir_okay=False,
        resolve_path=True,
    ),
    webui: bool = typer.Option(
        False,
        "--webui/--no-webui",
        help="Also start the WebUI in this process.",
    ),
    webui_host: str = typer.Option(_DEFAULT_WEBUI_HOST, "--webui-host"),
    webui_port: int = typer.Option(_DEFAULT_WEBUI_PORT, "--webui-port"),
    only_adapters: str = typer.Option(
        "",
        "--only-adapters",
        help=(
            "Comma-separated allowlist of adapter kinds to load (e.g. 'cli' or "
            "'cli,onebot'). Empty (default) means 'use whatever bot.yaml says'. "
            "Use this to keep one bot.yaml across dev/prod and toggle QQ on or "
            "off from the command line."
        ),
    ),
) -> None:
    """Run a bot until Ctrl-C."""
    cfg = BotConfig.from_yaml(config)
    cfg = _apply_adapter_filter(cfg, only_adapters)
    base_dir = config.parent
    asyncio.run(
        _run_bot(cfg, base_dir=base_dir, webui=webui, webui_host=webui_host, webui_port=webui_port)
    )


def _apply_adapter_filter(cfg: BotConfig, only_adapters: str) -> BotConfig:
    """Restrict ``cfg.adapters`` to the whitelist, if one was supplied.

    Topology decision (which adapters this *deployment* exercises) is
    intentionally separate from bot identity (rules, agent, scopes) —
    the latter lives in ``bot.yaml`` and shouldn't be edited just to
    toggle the OneBot uplink between dev and prod. The filter is a no-
    op when ``only_adapters`` is empty so existing call sites are
    unaffected.

    Unknown kinds are reported with a non-zero exit so a typo
    (``--only-adapters clii``) doesn't silently start a bot with no
    adapters.
    """
    raw = only_adapters.strip()
    if not raw:
        return cfg
    requested = {kind.strip() for kind in raw.split(",") if kind.strip()}
    available = {a.kind for a in cfg.adapters}
    unknown = requested - available
    if unknown:
        typer.echo(
            f"linling run: unknown adapter kind(s) in --only-adapters: {sorted(unknown)} "
            f"(bot.yaml declares: {sorted(available)})",
            err=True,
        )
        raise typer.Exit(code=2)
    filtered = [a for a in cfg.adapters if a.kind in requested]
    return cfg.model_copy(update={"adapters": filtered})


async def _run_bot(
    cfg: BotConfig,
    *,
    base_dir: Path,
    webui: bool,
    webui_host: str,
    webui_port: int,
) -> None:
    bot = await bootstrap_bot(cfg, base_dir=base_dir)

    webui_server: uvicorn.Server | None = None
    webui_task: asyncio.Task[None] | None = None
    if webui:
        webui_server, webui_task = await _start_webui(bot, host=webui_host, port=webui_port)

    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event, bot)

    await bot.start()
    typer.echo(
        f"linling run: bot '{cfg.bot_id}' online"
        f"{f' with WebUI at http://{webui_host}:{webui_port}' if webui else ''}"
    )

    # Exit conditions:
    #   * SIGINT / SIGTERM (always)
    #   * all adapter tasks have finished (common for interactive CLI
    #     mode once stdin hits EOF)
    #
    # In ``--webui`` mode the WebUI alone is enough to justify staying
    # up, so we ignore adapter exits — otherwise a non-interactive
    # deployment (``cli`` adapter reading a closed stdin) would quit
    # immediately.
    stop_gate: asyncio.Task[bool] = asyncio.create_task(stop_event.wait(), name="stop_gate")
    gates: set[asyncio.Task[bool] | asyncio.Task[None]] = {stop_gate}
    if bot.adapters and not webui:
        gates.add(asyncio.create_task(bot.wait(), name="adapter_gate"))
    try:
        _, pending = await asyncio.wait(gates, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
            with suppress(asyncio.CancelledError):
                await t
    finally:
        await bot.stop()
        if webui_task is not None and webui_server is not None:
            await _stop_webui(webui_server, webui_task)


# ---------------------------------------------------------------------------
# WebUI co-hosting (optional)
# ---------------------------------------------------------------------------


async def _start_webui(
    bot: RunningBot,
    *,
    host: str,
    port: int,
) -> tuple[uvicorn.Server, asyncio.Task[None]]:
    """Start the WebUI alongside the bot in the same event loop.

    Returns the ``(server, task)`` pair so the caller can drive a
    clean shutdown. We use uvicorn's programmatic server API instead of
    ``uvicorn.run`` so we don't take over the event loop.

    Config precedence: CLI flags (``--webui-host`` / ``--webui-port``)
    override the ``webui:`` section of ``bot.yaml``, which in turn
    overrides :class:`WebUIConfig` defaults. This matches the pattern
    we already use in ``linling serve webui``.
    """
    import uvicorn  # noqa: PLC0415 — optional dependency; deferred
    from linling_webui.app import create_app  # noqa: PLC0415
    from linling_webui.config import WebUIConfig  # noqa: PLC0415

    from linling_cli.wire_webui import attach_bot_to_webui  # noqa: PLC0415

    webui_cfg = WebUIConfig.from_bot_yaml_section(bot.config.webui or None)
    # CLI flags win over bot.yaml; we only override when the operator
    # supplied a non-default value via the command line.
    if host != _DEFAULT_WEBUI_HOST:
        webui_cfg.host = host
    if port != _DEFAULT_WEBUI_PORT:
        webui_cfg.port = port
    app = create_app(webui_cfg)
    attach_bot_to_webui(app, bot)

    uv_cfg = uvicorn.Config(
        app, host=webui_cfg.host, port=webui_cfg.port, log_level="info", access_log=False
    )
    server = uvicorn.Server(uv_cfg)
    task = asyncio.create_task(server.serve(), name="webui_server")
    # Give uvicorn a chance to bind before we return; otherwise the
    # announcement line would print before the listener is actually up.
    for _ in range(50):
        if getattr(server, "started", False):
            break
        await asyncio.sleep(0.01)
    return server, task


async def _stop_webui(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
    server.should_exit = True
    with suppress(asyncio.CancelledError, Exception):
        await asyncio.wait_for(task, timeout=5.0)


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------


def _install_signal_handlers(stop_event: asyncio.Event, bot: RunningBot | None = None) -> None:
    """Install SIGINT/SIGTERM handlers that flip ``stop_event``, and
    SIGHUP that triggers a hot rule reload.

    We don't cancel tasks directly from the signal handler because the
    shutdown sequence needs to run on the event loop (close KV, drain
    adapters, stop WebUI in order). Falling back to KeyboardInterrupt
    on Windows is fine — :meth:`RunningBot.stop` is idempotent and the
    ``finally`` block in :func:`_run_bot` will still run.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows: signal handlers aren't supported on the asyncio loop;
        # KeyboardInterrupt still propagates and the ``finally`` in
        # ``_run_bot`` still runs.
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    if bot is not None:
        with suppress(NotImplementedError, AttributeError):
            # SIGHUP is absent on Windows; ``signal.SIGHUP`` raises
            # AttributeError. That's fine — hot reload is POSIX-only.
            loop.add_signal_handler(
                signal.SIGHUP,
                lambda: asyncio.create_task(_do_reload(bot)),
            )


async def _do_reload(bot: RunningBot) -> None:
    """SIGHUP handler body — reload rules and log the outcome."""
    report = await bot.reload_rules()
    if report.applied:
        typer.echo(f"linling run: reloaded {report.handlers} handlers from {report.files} files")
    else:
        typer.echo(
            f"linling run: reload REJECTED — {len(report.errors)} parse error(s); previous ruleset kept"
        )
    for err in report.errors[:5]:
        typer.echo(f"  ! {err}")
