"""`linling run` live bring-up test.

Starts the CLI subcommand in-process (via :func:`_run_bot`), publishes
one event from a test adapter, asserts the bot replied, then flips the
stop event. Exercises the real signal-driven shutdown path without
actually sending SIGINT (which pytest doesn't propagate cleanly).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from linling_cli.commands.run import _run_bot
from linling_core.events import Action, Event, Scope, User
from linling_core.segments import TextSegment


def _write(tmp: Path, rel: str, content: str) -> Path:
    p = tmp / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


class _FakeAdapter:
    """Adapter test double: immediately emits one event on `run`, records sends."""

    platform = "test"

    def __init__(self) -> None:
        self.sent: list[Action] = []
        self.bus = None  # late-bound
        self._done = asyncio.Event()

    async def run(self) -> None:
        assert self.bus is not None
        await self.bus.publish(
            Event(
                id="live-1",
                platform="test",
                bot_id="bot1",
                scope=Scope(kind="group", id="g1", platform="test"),
                sender=User(id="u1", platform="test", display_name="u1"),
                segments=[TextSegment(text="ping")],
            )
        )
        # Block so `RunningBot.wait()` does not complete on its own.
        await self._done.wait()

    async def send(self, action: Action) -> None:
        self.sent.append(action)

    async def stop(self) -> None:
        self._done.set()


@pytest.mark.asyncio
async def test_run_boots_and_shuts_down_cleanly(tmp_path: Path, monkeypatch):
    """`_run_bot` boots, routes an inbound event, then shuts down via stop_event."""
    _write(tmp_path, "rules/main.ling", "ping\npong\n")
    _write(
        tmp_path,
        "bot.yaml",
        """
bot_id: bot1
storage:
  kv: ":memory:"
rules:
  - "rules/*.ling"
""",
    )

    from linling_cli import bootstrap as bootstrap_mod
    from linling_core.config import BotConfig

    cfg = BotConfig.from_yaml(tmp_path / "bot.yaml")

    # Patch bootstrap so our fake adapter is installed and wired through
    # the real sink. The simplest seam is ``bootstrap_bot``'s
    # ``extra_adapters`` argument.
    fake = _FakeAdapter()
    orig_bootstrap = bootstrap_mod.bootstrap_bot

    async def _bootstrap_with_fake(cfg, *, base_dir, extra_adapters=None):
        bot = await orig_bootstrap(cfg, base_dir=base_dir, extra_adapters=[fake])
        fake.bus = bot.bus
        # Rebuild sink to include the fake adapter.
        bot.router.set_sink(bootstrap_mod.build_sink(bot.adapters))
        return bot

    monkeypatch.setattr(
        "linling_cli.commands.run.bootstrap_bot",
        _bootstrap_with_fake,
    )

    task = asyncio.create_task(
        _run_bot(
            cfg,
            base_dir=tmp_path,
            webui=False,
            webui_host="127.0.0.1",
            webui_port=0,
        )
    )

    # Wait for the ping to produce a pong.
    for _ in range(50):
        if fake.sent:
            break
        await asyncio.sleep(0.02)
    assert fake.sent, "no reply observed"
    assert fake.sent[0].segments[0].text == "pong"

    # Drive shutdown — the fake's `run` is parked on `_done`.
    fake._done.set()
    await asyncio.wait_for(task, timeout=5.0)
