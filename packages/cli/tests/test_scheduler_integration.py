"""Bootstrap + Scheduler end-to-end integration.

Verifies the full path that a config-time recurring task or a delayed
DSL ``$调用$`` would travel:

    Scheduler.fire → RunningBot._on_scheduled_fire → Event onto the bus
        → Router.handle → DSL classifier matches the synthetic event's
        text against a handler → action emitted to the recording adapter

The integration matters because the scheduler has no opinion about *what*
``handler_name`` is — the bootstrap synthesises an inbound event and lets
the existing router do its job. If that wiring breaks, recurring tasks
silently drop on the floor.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from linling_cli.bootstrap import RunningBot, bootstrap_bot
from linling_core.config import BotConfig
from linling_core.events import Action
from linling_core.scheduler import SqliteSchedulerStore


class _Recorder:
    """Minimal adapter that captures outbound actions for assertions."""

    platform = "test"

    def __init__(self) -> None:
        self.sent: list[Action] = []

    async def run(self) -> None:
        return None

    async def send(self, action: Action) -> None:
        self.sent.append(action)

    async def stop(self) -> None:
        return None


def _write(tmp: Path, rel: str, content: str) -> Path:
    path = tmp / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


async def _boot(tmp_path: Path, yaml: str) -> RunningBot:
    cfg_path = _write(tmp_path, "bot.yaml", yaml)
    cfg = BotConfig.from_yaml(cfg_path)
    return await bootstrap_bot(cfg, base_dir=tmp_path)


@pytest.mark.asyncio
async def test_running_bot_creates_in_memory_scheduler_by_default(tmp_path: Path) -> None:
    yaml = """\
bot_id: bot1
name: tester
storage:
  kv: ":memory:"
"""
    bot = await _boot(tmp_path, yaml)
    try:
        assert bot.scheduler is not None
        # Memory store: no SQLite path involved.
        assert type(bot.scheduler._store).__name__ == "MemorySchedulerStore"
    finally:
        await bot.stop()


@pytest.mark.asyncio
async def test_running_bot_uses_sqlite_scheduler_when_configured(tmp_path: Path) -> None:
    yaml = f"""\
bot_id: bot1
name: tester
storage:
  kv: ":memory:"
  scheduler: "sqlite:///{tmp_path / "sched.db"}"
"""
    bot = await _boot(tmp_path, yaml)
    try:
        assert bot.scheduler is not None
        assert type(bot.scheduler._store).__name__ == "SqliteSchedulerStore"
        # Schedule something, persist, restart from the same db path.
        bot.scheduler.schedule(after_seconds=60.0, handler_name="x", bot_id="bot1", key="k1")
        store = SqliteSchedulerStore(tmp_path / "sched.db")
        assert len(store.load_all()) == 1
    finally:
        await bot.stop()


@pytest.mark.asyncio
async def test_scheduler_fire_routes_through_bus_to_dsl_handler(tmp_path: Path) -> None:
    """End-to-end: schedule a task, run the bot, watch the DSL handler reply."""
    _write(
        tmp_path,
        "rules/heartbeat.ling",
        "heartbeat\nOK\n",
    )
    yaml = """\
bot_id: bot1
name: tester
storage:
  kv: ":memory:"
rules:
  - "rules/**/*.ling"
"""
    bot = await _boot(tmp_path, yaml)
    rec = _Recorder()
    bot.attach_adapter(rec)
    try:
        await bot.start()
        assert bot.scheduler is not None
        # Fire immediately on the next tick.
        bot.scheduler.schedule(after_seconds=0.0, handler_name="heartbeat")

        # Wait for the scheduler tick + bus dispatch + adapter emit.
        for _ in range(40):
            if rec.sent:
                break
            await asyncio.sleep(0.05)

        assert len(rec.sent) == 1
        assert rec.sent[0].segments[0].text == "OK"
    finally:
        await bot.stop()


@pytest.mark.asyncio
async def test_scheduler_fire_invokes_internal_handler(tmp_path: Path) -> None:
    """Scheduler-fired tasks must reach ``[内部]`` handlers — they're
    the canonical target for ``$调用 ms handler args$``.

    Regression: events used to flow through the bus + classifier, but
    the classifier filters out ``[内部]`` handlers (they're not for
    user text). That meant scheduled internal calls either matched a
    catch-all regex by accident or went nowhere. The fix dispatches
    scheduled tasks directly to the named handler, bypassing the
    classifier.
    """
    _write(
        tmp_path,
        "rules/internal.ling",
        # A regular trigger that schedules an internal handler, plus
        # the internal handler whose body emits a sentinel.
        "kick\n$调用 0 secret_internal$\n\n[内部]secret_internal\nINTERNAL_FIRED\n",
    )
    yaml = """\
bot_id: bot1
name: tester
storage:
  kv: ":memory:"
rules:
  - "rules/**/*.ling"
"""
    bot = await _boot(tmp_path, yaml)
    rec = _Recorder()
    bot.attach_adapter(rec)
    try:
        await bot.start()
        # Trigger the user-facing rule which calls $调用 0 secret_internal$.
        # That schedules a task; the scheduler fires it; the internal
        # handler emits "INTERNAL_FIRED" through the same sink.
        from linling_core.events import Event, Scope, User
        from linling_core.segments import TextSegment

        ev = Event(
            id="trigger-1",
            platform="test",
            bot_id="bot1",
            scope=Scope(kind="group", id="g1", platform="test"),
            sender=User(id="u1", platform="test"),
            segments=[TextSegment(text="kick")],
        )
        await bot.bus.publish(ev)

        for _ in range(40):
            if any("INTERNAL_FIRED" in (a.segments[0].text if a.segments else "") for a in rec.sent):
                break
            await asyncio.sleep(0.05)

        emitted = [a.segments[0].text for a in rec.sent if a.segments]
        assert any("INTERNAL_FIRED" in t for t in emitted), (
            f"expected scheduler to invoke [内部]secret_internal, got {emitted!r}"
        )
    finally:
        await bot.stop()


@pytest.mark.asyncio
async def test_scheduler_fire_resolves_regex_internal_handler(tmp_path: Path) -> None:
    """Scheduler-fired ``$调用 0 prefix-suffix$`` must reach ``[内部]prefix-(.*)``
    via regex fullmatch fallback in ``_lookup_handler``.

    Regression: the original lookup did exact-string match only, so a
    rule that tries ``$调用 0 说话词语%参数-1%$`` against
    ``[内部]说话词语(.*)`` silently dropped on the floor — the literal
    name (e.g. ``"说话词语苹果"``) doesn't equal the regex trigger
    string. Now the lookup falls back to ``re.fullmatch`` and forwards
    the captured groups as ``%括号N%``.
    """
    _write(
        tmp_path,
        "rules/regex_internal.ling",
        # Schedule a handler whose name only matches a regex trigger
        # in the script. The internal handler echoes %括号1% so we can
        # assert the capture made it through.
        "kick\n$调用 0 echo-苹果$\n\n[内部]echo-(.*)\nGOT_%括号1%\n",
    )
    yaml = """\
bot_id: bot1
name: tester
storage:
  kv: ":memory:"
rules:
  - "rules/**/*.ling"
"""
    bot = await _boot(tmp_path, yaml)
    rec = _Recorder()
    bot.attach_adapter(rec)
    try:
        await bot.start()
        from linling_core.events import Event, Scope, User
        from linling_core.segments import TextSegment

        ev = Event(
            id="trigger-regex-1",
            platform="test",
            bot_id="bot1",
            scope=Scope(kind="group", id="g1", platform="test"),
            sender=User(id="u1", platform="test"),
            segments=[TextSegment(text="kick")],
        )
        await bot.bus.publish(ev)

        for _ in range(40):
            if any(
                "GOT_苹果" in (a.segments[0].text if a.segments else "")
                for a in rec.sent
            ):
                break
            await asyncio.sleep(0.05)

        emitted = [a.segments[0].text for a in rec.sent if a.segments]
        assert any("GOT_苹果" in t for t in emitted), (
            f"expected scheduler to invoke [内部]echo-(.*) with capture '苹果', "
            f"got {emitted!r}"
        )
    finally:
        await bot.stop()


@pytest.mark.asyncio
async def test_scheduler_fire_resolves_regex_internal_with_space_arg(
    tmp_path: Path,
) -> None:
    """Scheduler-fired ``$调用 0 游戏判断 12345$`` must reach
    ``[内部]游戏判断 ([0-9]+)`` via the space-joined fallback.

    Regression: ``$调用 ms X args$`` stores ``handler_name=X`` and
    ``args=[args...]`` in the ScheduledTask. The literal lookup of
    ``"X"`` alone misses regex triggers like ``"X ([0-9]+)"`` that
    embed a literal space. ``_on_scheduled_fire`` now reconstructs
    ``X + " " + args`` and tries the regex lookup on that joined
    string, forwarding the captured groups as ``%括号N%`` while
    suppressing duplicate args.
    """
    _write(
        tmp_path,
        "rules/regex_space.ling",
        "kick\n$调用 0 游戏判断 12345$\n\n"
        "[内部]游戏判断 ([0-9]+)\nGOT_%括号1%\n",
    )
    yaml = """\
bot_id: bot1
name: tester
storage:
  kv: ":memory:"
rules:
  - "rules/**/*.ling"
"""
    bot = await _boot(tmp_path, yaml)
    rec = _Recorder()
    bot.attach_adapter(rec)
    try:
        await bot.start()
        from linling_core.events import Event, Scope, User
        from linling_core.segments import TextSegment

        ev = Event(
            id="trigger-space-1",
            platform="test",
            bot_id="bot1",
            scope=Scope(kind="group", id="g1", platform="test"),
            sender=User(id="u1", platform="test"),
            segments=[TextSegment(text="kick")],
        )
        await bot.bus.publish(ev)

        for _ in range(40):
            if any(
                "GOT_12345" in (a.segments[0].text if a.segments else "")
                for a in rec.sent
            ):
                break
            await asyncio.sleep(0.05)

        emitted = [a.segments[0].text for a in rec.sent if a.segments]
        assert any("GOT_12345" in t for t in emitted), (
            f"expected scheduler to invoke [内部]游戏判断 ([0-9]+) with "
            f"capture '12345', got {emitted!r}"
        )
    finally:
        await bot.stop()


@pytest.mark.asyncio
async def test_scheduler_lifecycle_clean_shutdown(tmp_path: Path) -> None:
    yaml = """\
bot_id: bot1
name: tester
storage:
  kv: ":memory:"
"""
    bot = await _boot(tmp_path, yaml)
    await bot.start()
    # The scheduler task should be running.
    assert bot._scheduler_task is not None
    assert not bot._scheduler_task.done()
    await bot.stop()
    # And cleanly stopped.
    assert bot._scheduler_task is None
