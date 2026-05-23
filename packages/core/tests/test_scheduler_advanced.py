"""Tests for the persistent / recurring / idempotent scheduler features."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from linling_core.scheduler import (
    ScheduledTask,
    Scheduler,
    SqliteSchedulerStore,
)

# ---------------------------------------------------------------------------
# Idempotent keys
# ---------------------------------------------------------------------------


async def test_schedule_with_key_replaces_prior_task() -> None:
    sched = Scheduler()
    fired: list[str] = []

    async def cb(t: ScheduledTask) -> None:
        fired.append(t.handler_name)

    # First registration.
    sched.schedule(
        after_seconds=10.0, handler_name="old", bot_id="b1", key="cooldown:u1"
    )
    # Second with same key replaces it.
    sched.schedule(
        after_seconds=0.05, handler_name="new", bot_id="b1", key="cooldown:u1"
    )
    assert sched.pending_count == 1

    runner = asyncio.create_task(sched.run(cb))
    await asyncio.sleep(0.2)
    await sched.stop()
    await runner

    assert fired == ["new"]


async def test_schedule_with_different_keys_are_independent() -> None:
    sched = Scheduler()
    sched.schedule(after_seconds=1.0, handler_name="a", bot_id="b1", key="k1")
    sched.schedule(after_seconds=1.0, handler_name="b", bot_id="b1", key="k2")
    sched.schedule(after_seconds=1.0, handler_name="c", bot_id="b2", key="k1")
    assert sched.pending_count == 3


async def test_blank_key_does_not_dedupe() -> None:
    sched = Scheduler()
    sched.schedule(after_seconds=1.0, handler_name="a")
    sched.schedule(after_seconds=1.0, handler_name="b")
    assert sched.pending_count == 2


# ---------------------------------------------------------------------------
# Recurring tasks
# ---------------------------------------------------------------------------


async def test_recurring_task_fires_repeatedly() -> None:
    sched = Scheduler()
    fired: list[float] = []

    async def cb(t: ScheduledTask) -> None:
        fired.append(time.time())

    sched.schedule_recurring(
        every_seconds=0.05,
        handler_name="tick",
        first_fire_at=time.time(),  # fire immediately
    )
    runner = asyncio.create_task(sched.run(cb))
    await asyncio.sleep(0.30)
    await sched.stop()
    await runner

    # Expect at least 3 fires in 0.30s with 0.05s cadence and 0.1s tick.
    assert len(fired) >= 3


async def test_recurring_task_can_be_cancelled() -> None:
    sched = Scheduler()
    fired: list[str] = []

    async def cb(t: ScheduledTask) -> None:
        fired.append(t.handler_name)

    tid = sched.schedule_recurring(
        every_seconds=0.05,
        handler_name="tick",
        first_fire_at=time.time(),
    )
    runner = asyncio.create_task(sched.run(cb))
    await asyncio.sleep(0.12)
    assert sched.cancel(tid) is True
    n_at_cancel = len(fired)
    await asyncio.sleep(0.30)
    await sched.stop()
    await runner
    # After cancel, no further fires.
    assert len(fired) == n_at_cancel


async def test_recurring_rejects_non_positive_interval() -> None:
    sched = Scheduler()
    with pytest.raises(ValueError):
        sched.schedule_recurring(every_seconds=0, handler_name="x")
    with pytest.raises(ValueError):
        sched.schedule_recurring(every_seconds=-1, handler_name="x")


# ---------------------------------------------------------------------------
# Persistence — SqliteSchedulerStore
# ---------------------------------------------------------------------------


async def test_sqlite_store_persists_across_restart(tmp_path: Path) -> None:
    db = tmp_path / "sched.db"
    s1 = Scheduler(store=SqliteSchedulerStore(db))
    fire_at_iso = time.time() + 60.0
    tid = s1.schedule(
        after_seconds=60.0,
        handler_name="reminder",
        args=["wake up"],
        scope={"chat": "g1"},
        bot_id="bot1",
        key="reminder-1",
    )
    assert s1.pending_count == 1

    # Simulate a restart — fresh Scheduler reading the same store.
    store2 = SqliteSchedulerStore(db)
    s2 = Scheduler(store=store2)
    assert s2.pending_count == 1
    pending = store2.load_all()
    assert len(pending) == 1
    assert pending[0].id == tid
    assert pending[0].handler_name == "reminder"
    assert pending[0].args == ["wake up"]
    assert pending[0].scope == {"chat": "g1"}
    assert pending[0].bot_id == "bot1"
    assert pending[0].key == "reminder-1"
    assert abs(pending[0].fire_at - fire_at_iso) < 1.0


async def test_sqlite_store_overdue_task_fires_immediately(tmp_path: Path) -> None:
    """Tasks whose deadline passed during downtime should fire on next run()."""
    store = SqliteSchedulerStore(tmp_path / "sched.db")
    # Inject an already-overdue task by hand.
    overdue = ScheduledTask(
        fire_at=time.time() - 60.0,  # 1 minute ago
        id="manual-1",
        handler_name="late",
    )
    store.upsert(overdue)

    sched = Scheduler(store=store)
    fired: list[str] = []

    async def cb(t: ScheduledTask) -> None:
        fired.append(t.handler_name)

    runner = asyncio.create_task(sched.run(cb))
    await asyncio.sleep(0.2)
    await sched.stop()
    await runner

    assert fired == ["late"]


async def test_sqlite_idempotent_key_survives_restart(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "sched.db")
    s1 = Scheduler(store=store)
    s1.schedule(after_seconds=60.0, handler_name="old", bot_id="b", key="reset")
    assert s1.pending_count == 1

    # Restart and re-schedule with the same key — should replace.
    store2 = SqliteSchedulerStore(tmp_path / "sched.db")
    s2 = Scheduler(store=store2)
    assert s2.pending_count == 1
    s2.schedule(after_seconds=60.0, handler_name="new", bot_id="b", key="reset")
    assert s2.pending_count == 1

    rows = store2.load_all()
    assert len(rows) == 1
    assert rows[0].handler_name == "new"


async def test_sqlite_recurring_advances_fire_at(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "sched.db")
    sched = Scheduler(store=store)
    sched.schedule_recurring(
        every_seconds=0.05,
        handler_name="tick",
        first_fire_at=time.time(),
        bot_id="b1",
        key="hb",
    )
    # The task is persisted with its recurring interval.
    persisted = store.load_all()
    assert len(persisted) == 1
    assert persisted[0].recurring_seconds == 0.05

    fired: list[float] = []

    async def cb(t: ScheduledTask) -> None:
        fired.append(t.fire_at)

    runner = asyncio.create_task(sched.run(cb))
    await asyncio.sleep(0.25)
    await sched.stop()
    await runner

    # The persisted row's ``fire_at`` should have advanced past the
    # original first_fire_at — i.e. the recurring re-arm wrote back.
    persisted_after = store.load_all()
    assert len(persisted_after) == 1
    assert persisted_after[0].fire_at > persisted[0].fire_at + 0.04


async def test_cancel_removes_from_persistent_store(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "sched.db")
    sched = Scheduler(store=store)
    tid = sched.schedule(after_seconds=60.0, handler_name="x")
    assert len(store.load_all()) == 1
    assert sched.cancel(tid) is True
    assert store.load_all() == []


# ---------------------------------------------------------------------------
# Backwards-compat with the legacy ``delay()`` API
# ---------------------------------------------------------------------------


async def test_legacy_delay_is_thin_wrapper() -> None:
    sched = Scheduler()
    fired: list[str] = []

    async def cb(t: ScheduledTask) -> None:
        fired.append(t.handler_name)

    tid = sched.delay(50, "old_school")
    assert tid.startswith("sched-")

    runner = asyncio.create_task(sched.run(cb))
    await asyncio.sleep(0.2)
    await sched.stop()
    await runner

    assert fired == ["old_school"]
