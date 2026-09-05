"""`linling run` command surface tests.

Starting the real command runs forever; we only verify the typer wiring
(``linling run --help`` succeeds and the subcommand is registered).
Full bring-up of a bot in-process is exercised by
``test_wire_webui.py`` without shelling out.
"""

from __future__ import annotations

from linling_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


def test_run_is_registered() -> None:
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "Run a bot until Ctrl-C" in result.output


def test_run_rejects_missing_config_file() -> None:
    result = runner.invoke(app, ["run", "/nonexistent/bot.yaml"])
    assert result.exit_code != 0
    # typer's ``exists=True`` emits a message on stderr.
    out = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "exist" in out.lower() or "not found" in out.lower()


# ---------------------------------------------------------------------------
# --only-adapters filter
# ---------------------------------------------------------------------------


def test_apply_adapter_filter_noop_when_empty() -> None:
    """No flag → config passes through verbatim."""
    from linling_cli.commands.run import _apply_adapter_filter
    from linling_core.config import AdapterConfig, BotConfig

    cfg = BotConfig(
        bot_id="b",
        adapters=[
            AdapterConfig(kind="cli"),
            AdapterConfig(kind="onebot", ws_url="ws://x"),
        ],
    )
    out = _apply_adapter_filter(cfg, "")
    assert [a.kind for a in out.adapters] == ["cli", "onebot"]


def test_apply_adapter_filter_keeps_only_listed_kinds() -> None:
    from linling_cli.commands.run import _apply_adapter_filter
    from linling_core.config import AdapterConfig, BotConfig

    cfg = BotConfig(
        bot_id="b",
        adapters=[
            AdapterConfig(kind="cli"),
            AdapterConfig(kind="onebot", ws_url="ws://x"),
        ],
    )
    out = _apply_adapter_filter(cfg, "cli")
    assert [a.kind for a in out.adapters] == ["cli"]


def test_apply_adapter_filter_supports_multiple() -> None:
    from linling_cli.commands.run import _apply_adapter_filter
    from linling_core.config import AdapterConfig, BotConfig

    cfg = BotConfig(
        bot_id="b",
        adapters=[
            AdapterConfig(kind="cli"),
            AdapterConfig(kind="onebot", ws_url="ws://x"),
        ],
    )
    out = _apply_adapter_filter(cfg, "cli,onebot")
    assert [a.kind for a in out.adapters] == ["cli", "onebot"]


def test_apply_adapter_filter_rejects_unknown_kind() -> None:
    """Typo in --only-adapters must abort, not silently start with 0 adapters."""
    import typer
    from linling_cli.commands.run import _apply_adapter_filter
    from linling_core.config import AdapterConfig, BotConfig

    cfg = BotConfig(bot_id="b", adapters=[AdapterConfig(kind="cli")])
    try:
        _apply_adapter_filter(cfg, "clii")
    except typer.Exit as exc:
        assert exc.exit_code == 2
    else:
        raise AssertionError("expected typer.Exit on unknown adapter kind")


def test_run_help_documents_only_adapters() -> None:
    """The new option must show up in ``--help`` so users can discover it."""
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "--only-adapters" in result.output
