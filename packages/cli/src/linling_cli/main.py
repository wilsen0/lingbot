"""`linling` command-line entry point."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import typer
from dotenv import find_dotenv, load_dotenv
from linling_dsl.migrator import MigrationConfig, migrate

from linling_cli import __version__
from linling_cli.commands.doctor import doctor
from linling_cli.commands.init import init_cmd
from linling_cli.commands.run import run
from linling_cli.commands.serve_webui import serve_webui
from linling_cli.lint_cmd import lint

# Load ``.env`` into ``os.environ`` *before* anything imports
# :class:`BotConfig` or instantiates an LLM provider — both reach for
# ``os.environ`` directly (see ``bootstrap._provider_for`` and the
# ``${VAR}`` interpolation in YAML), so they need real env entries,
# not just the values pydantic-settings would inject into its own
# fields.
#
# Policy:
# * Walk up from CWD via :func:`find_dotenv` so ``linling run`` works
#   from any subdirectory of the repo.
# * ``override=False`` — env vars set by the operator's shell, CI, or
#   container runtime always win over the file. The file is the
#   "default for local dev" layer, not the source of truth.
# * Silent on missing — production deployments will set env vars
#   directly and don't ship a ``.env``.
#
# A ``LINLING_SKIP_DOTENV=1`` escape hatch lets tests or unusual
# embeddings opt out without monkey-patching.
if not os.environ.get("LINLING_SKIP_DOTENV"):
    _dotenv_path = find_dotenv(usecwd=True)
    if _dotenv_path:
        load_dotenv(_dotenv_path, override=False)


app = typer.Typer(
    help="linling command-line interface",
    no_args_is_help=True,
)


app.command("lint")(lint)
app.command("run")(run)
app.command("doctor")(doctor)
app.command("init")(init_cmd)


# ---------------------------------------------------------------------------
# `linling serve ...`
# ---------------------------------------------------------------------------

serve_app = typer.Typer(help="Servers (webui, …)", no_args_is_help=True)
app.add_typer(serve_app, name="serve")
serve_app.command("webui")(serve_webui)


@app.command()
def version() -> None:
    """Print the platform version."""
    typer.echo(__version__)


@app.command()
def info() -> None:
    """Show basic platform info. Placeholder; will list adapters/tools in P0."""
    typer.echo(f"linling {__version__}")


# ---------------------------------------------------------------------------
# `linling migrate ...`
# ---------------------------------------------------------------------------

migrate_app = typer.Typer(help="Migration tools", no_args_is_help=True)
app.add_typer(migrate_app, name="migrate")


@migrate_app.command("qrdic")
def migrate_qrdic(
    src: Path = typer.Option(..., "--src", help="Path to QRDic/ directory"),  # noqa: B008
    out: Path = typer.Option(..., "--out", help="Output directory"),  # noqa: B008
    bot_id: str = typer.Option("linling", "--bot-id"),
    admin_qq: str = typer.Option("", "--admin-qq"),
) -> None:
    """Migrate a QRDic project to linling format."""
    config = MigrationConfig(
        src_dir=src,
        out_dir=out,
        bot_id=bot_id,
        admin_qq=admin_qq,
    )
    report = asyncio.run(migrate(config))
    typer.echo(
        f"Migration complete: {report.rules_written} rules, {report.kv_entries_written} KV entries"
    )
    report_path = out / "migration_report.md"
    typer.echo(f"Report written to {report_path}")


if __name__ == "__main__":
    app()
