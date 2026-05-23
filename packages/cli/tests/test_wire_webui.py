"""Tests for attaching a :class:`RunningBot` to the WebUI app.

Proves the two behaviours operators care about:

* After ``attach_bot_to_webui`` the bot shows up in ``state.bots`` and
  its KV is queryable via ``state.kv_stores``.
* Every event published on the bot's :class:`EventBus` lands in the
  WebUI's ring buffer (so the live WS tail can stream it) **and** the
  bot's router still sees it.

The WebUI itself isn't HTTP-served here; we inspect the in-process
state directly, which is what the WS handlers and REST routers also do.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from linling_cli.bootstrap import RunningBot, bootstrap_bot
from linling_cli.wire_webui import attach_bot_to_webui
from linling_core.config import BotConfig
from linling_core.events import Event, Scope, User
from linling_core.segments import TextSegment
from linling_webui.app import create_app


def _write(tmp: Path, rel: str, content: str) -> Path:
    p = tmp / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


async def _boot(tmp_path: Path, yaml: str) -> RunningBot:
    cfg = BotConfig.from_yaml(_write(tmp_path, "bot.yaml", yaml))
    return await bootstrap_bot(cfg, base_dir=tmp_path)


def _event(text: str, *, bot_id: str = "bot1", sender: str = "u1") -> Event:
    return Event(
        id=f"e-{text}-{sender}",
        platform="test",
        bot_id=bot_id,
        scope=Scope(kind="group", id="g1", platform="test"),
        sender=User(id=sender, platform="test", display_name=sender),
        segments=[TextSegment(text=text)],
    )


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_registers_bot_in_state(tmp_path: Path):
    _write(tmp_path, "rules/main.ling", "ping\npong\n")
    bot = await _boot(
        tmp_path,
        """
bot_id: bot1
name: Test Bot
storage:
  kv: ":memory:"
rules:
  - "rules/*.ling"
""",
    )
    app = _make_app()
    try:
        attach_bot_to_webui(app, bot)

        state = app.state.runtime
        assert "bot1" in state.bots
        info = state.bots["bot1"]
        assert info.name == "Test Bot"
        assert info.online is True

        # KV is shared between router and WebUI.
        assert state.kv_stores["bot1"] is bot.kv

        # Bus wired so WebSocket subs on agents/rules can observe it.
        assert state.bus is bot.bus
    finally:
        await bot.stop()


@pytest.mark.asyncio
async def test_events_mirror_into_ring_buffer(tmp_path: Path):
    _write(tmp_path, "rules/main.ling", "ping\npong\n")
    bot = await _boot(
        tmp_path,
        """
bot_id: bot1
storage:
  kv: ":memory:"
rules:
  - "rules/*.ling"
""",
    )
    app = _make_app()
    try:
        attach_bot_to_webui(app, bot)

        # Publish a matching event (router runs too, but that's fine).
        await bot.bus.publish(_event("ping"))
        await bot.bus.publish(_event("hello there", sender="u2"))

        buf = app.state.runtime.event_buffers["bot1"]
        tailed = await buf.tail(limit=10)
        assert [be.event.id for be in tailed] == ["e-ping-u1", "e-hello there-u2"]
        # last_event_at populated.
        assert app.state.runtime.bots["bot1"].last_event_at is not None
    finally:
        await bot.stop()


@pytest.mark.asyncio
async def test_observer_isolation_does_not_short_circuit_bus(tmp_path: Path):
    """A raising observer must not prevent router processing."""
    _write(tmp_path, "rules/main.ling", "ping\npong\n")
    bot = await _boot(
        tmp_path,
        """
bot_id: bot1
storage:
  kv: ":memory:"
rules:
  - "rules/*.ling"
""",
    )
    try:

        async def _bad_observer(event: Event) -> None:
            raise RuntimeError("observer boom")

        bot.add_event_observer(_bad_observer)

        # Replace the router's sink with a recorder so we can verify the
        # router still processes events even though the observer crashed.
        from linling_core.events import Action

        replies: list[Action] = []

        class _Rec:
            platform = "test"

            async def send(self, action: Action) -> None:
                replies.append(action)

        bot.attach_adapter(_Rec())

        await bot.bus.publish(_event("ping"))
        assert len(replies) == 1
        assert replies[0].segments[0].text == "pong"
    finally:
        await bot.stop()


@pytest.mark.asyncio
async def test_foreign_bot_id_events_not_mirrored(tmp_path: Path):
    """If two bots share a bus, each buffer only sees its own bot's events."""
    _write(tmp_path, "rules/main.ling", "ping\npong\n")
    bot = await _boot(
        tmp_path,
        """
bot_id: bot1
storage:
  kv: ":memory:"
rules:
  - "rules/*.ling"
""",
    )
    app = _make_app()
    try:
        attach_bot_to_webui(app, bot)

        # Publish an event that claims to belong to *another* bot.
        await bot.bus.publish(_event("ping", bot_id="other"))

        buf = app.state.runtime.event_buffers["bot1"]
        assert await buf.tail(limit=10) == []
    finally:
        await bot.stop()


@pytest.mark.asyncio
async def test_metrics_endpoint_served_when_enabled(tmp_path: Path):
    """``metrics.enabled=True`` → ``/metrics`` returns Prometheus exposition."""
    from fastapi.testclient import TestClient

    _write(tmp_path, "rules/main.ling", "ping\npong\n")
    cfg_path = _write(
        tmp_path,
        "bot.yaml",
        """
bot_id: bot1
storage:
  kv: ":memory:"
rules:
  - "rules/*.ling"
metrics:
  enabled: true
""",
    )
    from linling_core.config import BotConfig

    cfg = BotConfig.from_yaml(cfg_path)
    bot = await bootstrap_bot(cfg, base_dir=tmp_path)
    app = _make_app()
    try:
        attach_bot_to_webui(app, bot)
        # Drive one event so the counter has something to report.
        await bot.bus.publish(_event("ping"))

        with TestClient(app) as client:
            r = client.get("/metrics")
            assert r.status_code == 200
            assert "text/plain" in r.headers["content-type"]
            body = r.text
            assert "linling_events_total" in body
            assert 'bot_id="bot1"' in body
    finally:
        await bot.stop()


@pytest.mark.asyncio
async def test_metrics_endpoint_absent_when_disabled(tmp_path: Path):
    """Default config → ``/metrics`` does not expose Prometheus exposition."""
    from fastapi.testclient import TestClient

    _write(tmp_path, "rules/main.ling", "ping\npong\n")
    bot = await _boot(
        tmp_path,
        """
bot_id: bot1
storage:
  kv: ":memory:"
rules:
  - "rules/*.ling"
""",
    )
    app = _make_app()
    try:
        attach_bot_to_webui(app, bot)
        with TestClient(app) as client:
            r = client.get("/metrics")
            # Whether the response is a 404 (no SPA bundle) or an HTML
            # shell (SPA bundle present) depends on whether the WebUI
            # frontend has been built. The invariant we care about:
            # we do **not** emit a Prometheus exposition.
            assert "linling_events_total" not in r.text
            assert "text/plain; version=0.0.4" not in r.headers.get("content-type", "")
    finally:
        await bot.stop()


# ---------------------------------------------------------------------------


def _make_app() -> FastAPI:
    """WebUI app factory wrapper — avoids global state between tests."""
    return create_app()
