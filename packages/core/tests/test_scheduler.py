"""Tests for the in-memory async scheduler."""

from __future__ import annotations

import asyncio

from linling_core.scheduler import ScheduledTask, Scheduler


async def test_delay_returns_task_id() -> None:
    sched = Scheduler()
    tid = sched.delay(100, "handler_a")
    assert tid == "sched-1"


async def test_delay_returns_incrementing_ids() -> None:
    sched = Scheduler()
    t1 = sched.delay(100, "h1")
    t2 = sched.delay(200, "h2")
    t3 = sched.delay(300, "h3")
    assert t1 == "sched-1"
    assert t2 == "sched-2"
    assert t3 == "sched-3"


async def test_pending_count_reflects_queued_tasks() -> None:
    sched = Scheduler()
    assert sched.pending_count == 0
    sched.delay(1000, "h1")
    assert sched.pending_count == 1
    sched.delay(2000, "h2")
    assert sched.pending_count == 2


async def test_cancel_removes_task_returns_true() -> None:
    sched = Scheduler()
    tid = sched.delay(1000, "h1")
    assert sched.cancel(tid) is True
    assert sched.pending_count == 0


async def test_cancel_returns_false_for_unknown_id() -> None:
    sched = Scheduler()
    assert sched.cancel("sched-999") is False


async def test_task_fires_after_delay() -> None:
    sched = Scheduler()
    fired: list[ScheduledTask] = []

    async def cb(task: ScheduledTask) -> None:
        fired.append(task)

    sched.delay(80, "my_handler", args=["a", "b"], scope={"group_id": "g1"})

    loop_task = asyncio.create_task(sched.start(cb))
    await asyncio.sleep(0.2)
    await sched.stop()
    await loop_task

    assert len(fired) == 1
    assert fired[0].handler_name == "my_handler"
    assert fired[0].args == ["a", "b"]
    assert fired[0].scope == {"group_id": "g1"}


async def test_multiple_tasks_fire_in_correct_order() -> None:
    sched = Scheduler()
    fired: list[str] = []

    async def cb(task: ScheduledTask) -> None:
        fired.append(task.handler_name)

    sched.delay(150, "second")
    sched.delay(50, "first")
    sched.delay(250, "third")

    loop_task = asyncio.create_task(sched.start(cb))
    await asyncio.sleep(0.4)
    await sched.stop()
    await loop_task

    assert fired == ["first", "second", "third"]


async def test_cancelled_task_does_not_fire() -> None:
    sched = Scheduler()
    fired: list[str] = []

    async def cb(task: ScheduledTask) -> None:
        fired.append(task.handler_name)

    sched.delay(50, "keep")
    tid = sched.delay(100, "cancel_me")
    sched.cancel(tid)

    loop_task = asyncio.create_task(sched.start(cb))
    await asyncio.sleep(0.25)
    await sched.stop()
    await loop_task

    assert fired == ["keep"]


async def test_stop_causes_loop_to_exit() -> None:
    sched = Scheduler()

    async def cb(task: ScheduledTask) -> None:
        pass

    loop_task = asyncio.create_task(sched.start(cb))
    await asyncio.sleep(0.1)
    await sched.stop()
    # The loop task should complete shortly after stop
    await asyncio.wait_for(loop_task, timeout=1.0)
    assert loop_task.done()


async def test_zero_delay_fires_on_next_tick() -> None:
    sched = Scheduler()
    fired: list[str] = []

    async def cb(task: ScheduledTask) -> None:
        fired.append(task.handler_name)

    sched.delay(0, "immediate")

    loop_task = asyncio.create_task(sched.start(cb))
    await asyncio.sleep(0.1)
    await sched.stop()
    await loop_task

    assert fired == ["immediate"]


async def test_scheduler_can_be_restarted_after_stop() -> None:
    sched = Scheduler()
    fired: list[str] = []

    async def cb(task: ScheduledTask) -> None:
        fired.append(task.handler_name)

    # First run
    sched.delay(50, "run1")
    loop_task = asyncio.create_task(sched.start(cb))
    await asyncio.sleep(0.15)
    await sched.stop()
    await loop_task

    assert fired == ["run1"]

    # Second run after restart
    sched.delay(50, "run2")
    loop_task = asyncio.create_task(sched.start(cb))
    await asyncio.sleep(0.15)
    await sched.stop()
    await loop_task

    assert fired == ["run1", "run2"]


async def test_delay_while_loop_is_running() -> None:
    """delay() can be called while the scheduler loop is active."""
    sched = Scheduler()
    fired: list[str] = []

    async def cb(task: ScheduledTask) -> None:
        fired.append(task.handler_name)

    loop_task = asyncio.create_task(sched.start(cb))
    await asyncio.sleep(0.05)

    # Add task while loop is running
    sched.delay(50, "dynamic")
    await asyncio.sleep(0.15)
    await sched.stop()
    await loop_task

    assert "dynamic" in fired


async def test_pending_count_decreases_after_fire() -> None:
    sched = Scheduler()
    fired: list[str] = []

    async def cb(task: ScheduledTask) -> None:
        fired.append(task.id)

    sched.delay(50, "h1")
    sched.delay(500, "h2")
    assert sched.pending_count == 2

    loop_task = asyncio.create_task(sched.start(cb))
    await asyncio.sleep(0.15)

    # h1 should have fired, h2 still pending
    assert sched.pending_count == 1

    await sched.stop()
    await loop_task
