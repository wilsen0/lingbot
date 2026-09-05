"""Async scheduler for delayed and recurring handler calls.

Two task shapes are supported:

* **Delayed** — fire once after ``ms`` milliseconds. The DSL exposes
  this as ``$调用 ms handler args$``.
* **Recurring** — fire on a fixed interval (``every`` minutes/seconds)
  starting at the next aligned tick. Use case: "每天 0 点 reset cooldowns",
  "每 5 分钟刷新缓存". Specified at config time, not from inside DSL.

Persistence is pluggable via :class:`SchedulerStore`. The default store
(:class:`MemorySchedulerStore`) keeps tasks in process memory; the
production path uses :class:`SqliteSchedulerStore` so tasks survive
restarts.

Concurrency model:

* One ``run()`` coroutine drains tasks. Two callers must not call
  ``run()`` simultaneously on the same scheduler.
* ``schedule()`` / ``cancel()`` are safe to call from any coroutine
  on the same loop.
* Tasks fire by invoking a user-supplied callback. The callback runs
  in the scheduler's event loop; it must not block. Long work belongs
  in a downstream queue / worker.

The scheduler's notion of time is *wall-clock* (``time.time()``) so
that recurring tasks survive a sleep / suspend correctly. We persist
``fire_at`` as a Unix timestamp; if the process is offline when a
delayed task was supposed to fire, it fires immediately on the next
``run()``. Recurring tasks compute the next aligned interval after
the current wall clock.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import json
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

import structlog

logger = structlog.get_logger(__name__)


# Wake-up cadence inside ``run()``. Lower = more responsive; higher =
# less CPU. Tasks fire on the first iteration after their ``fire_at``,
# so the practical worst-case latency is one tick.
_TICK_SECONDS = 0.1


@dataclass(order=True)
class ScheduledTask:
    """One pending task in the scheduler queue.

    ``fire_at`` is a wall-clock Unix timestamp (seconds since epoch).
    ``key`` lets callers schedule idempotently — two ``schedule()``
    calls with the same key replace each other rather than producing
    two firings. ``recurring_seconds`` non-zero turns this into a
    cron-like recurring task; on every fire, ``fire_at`` advances by
    ``recurring_seconds`` and the task goes back into the queue.
    """

    fire_at: float
    id: str = field(compare=False)
    handler_name: str = field(compare=False)
    args: list[str] = field(compare=False, default_factory=list)
    scope: dict[str, str] = field(compare=False, default_factory=dict)
    bot_id: str = field(compare=False, default="")
    key: str = field(compare=False, default="")
    recurring_seconds: float = field(compare=False, default=0.0)


SchedulerCallback = Callable[[ScheduledTask], Awaitable[None]]


# ---------------------------------------------------------------------------
# Store protocol + memory implementation
# ---------------------------------------------------------------------------


@runtime_checkable
class SchedulerStore(Protocol):
    """Pluggable persistence for the scheduler."""

    def load_all(self) -> list[ScheduledTask]: ...
    def upsert(self, task: ScheduledTask) -> None: ...
    def delete(self, task_id: str) -> bool: ...
    def delete_by_key(self, bot_id: str, key: str) -> str | None: ...


class MemorySchedulerStore:
    """In-process map keyed by task id. Stable across ``run()`` cycles
    in one process; lost on restart.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, ScheduledTask] = {}

    def load_all(self) -> list[ScheduledTask]:
        return list(self._by_id.values())

    def upsert(self, task: ScheduledTask) -> None:
        self._by_id[task.id] = task

    def delete(self, task_id: str) -> bool:
        return self._by_id.pop(task_id, None) is not None

    def delete_by_key(self, bot_id: str, key: str) -> str | None:
        match = next(
            (t for t in self._by_id.values() if t.bot_id == bot_id and t.key == key),
            None,
        )
        if match is None:
            return None
        self._by_id.pop(match.id, None)
        return match.id


# ---------------------------------------------------------------------------
# SQLite store
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_task (
    id                  TEXT PRIMARY KEY,
    bot_id              TEXT NOT NULL DEFAULT '',
    key                 TEXT NOT NULL DEFAULT '',
    fire_at             REAL NOT NULL,
    handler_name        TEXT NOT NULL,
    args_json           TEXT NOT NULL DEFAULT '[]',
    scope_json          TEXT NOT NULL DEFAULT '{}',
    recurring_seconds   REAL NOT NULL DEFAULT 0
);

-- Lookup by ``(bot_id, key)`` is how :meth:`Scheduler.schedule`
-- enforces idempotency.
CREATE UNIQUE INDEX IF NOT EXISTS scheduled_task_bot_key
    ON scheduled_task(bot_id, key)
    WHERE key != '';

CREATE INDEX IF NOT EXISTS scheduled_task_fire_at
    ON scheduled_task(fire_at);
"""


class SqliteSchedulerStore:
    """Durable scheduler store backed by SQLite.

    One SQLite connection guarded by a thread lock — the scheduler
    runs on the event loop, but ``upsert``/``delete`` are sync calls
    serialised through ``threading.Lock`` so the connection works
    correctly even if a future operator drives it from background
    threads. WAL mode keeps reads non-blocking.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        import threading  # noqa: PLC0415 — only used by this backend

        self._lock = threading.Lock()
        self._conn = self._open()

    def _open(self) -> sqlite3.Connection:
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.executescript(_SCHEMA)
        conn.commit()
        return conn

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def load_all(self) -> list[ScheduledTask]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM scheduled_task ORDER BY fire_at")
            rows = cur.fetchall()
        return [_row_to_task(r) for r in rows]

    def upsert(self, task: ScheduledTask) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO scheduled_task
                  (id, bot_id, key, fire_at, handler_name, args_json,
                   scope_json, recurring_seconds)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  bot_id=excluded.bot_id,
                  key=excluded.key,
                  fire_at=excluded.fire_at,
                  handler_name=excluded.handler_name,
                  args_json=excluded.args_json,
                  scope_json=excluded.scope_json,
                  recurring_seconds=excluded.recurring_seconds
                """,
                (
                    task.id,
                    task.bot_id,
                    task.key,
                    task.fire_at,
                    task.handler_name,
                    json.dumps(task.args, ensure_ascii=False),
                    json.dumps(task.scope, ensure_ascii=False),
                    task.recurring_seconds,
                ),
            )
            self._conn.commit()

    def delete(self, task_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM scheduled_task WHERE id = ?", (task_id,))
            self._conn.commit()
            return (cur.rowcount or 0) > 0

    def delete_by_key(self, bot_id: str, key: str) -> str | None:
        if not key:
            return None
        with self._lock:
            cur = self._conn.execute(
                "SELECT id FROM scheduled_task WHERE bot_id=? AND key=?",
                (bot_id, key),
            )
            row = cur.fetchone()
            if row is None:
                return None
            tid = str(row["id"])
            self._conn.execute("DELETE FROM scheduled_task WHERE id=?", (tid,))
            self._conn.commit()
            return tid


def _row_to_task(row: sqlite3.Row) -> ScheduledTask:
    try:
        args = json.loads(row["args_json"])
    except (json.JSONDecodeError, TypeError):
        args = []
    if not isinstance(args, list):
        args = []
    try:
        scope = json.loads(row["scope_json"])
    except (json.JSONDecodeError, TypeError):
        scope = {}
    if not isinstance(scope, dict):
        scope = {}
    return ScheduledTask(
        fire_at=float(row["fire_at"]),
        id=str(row["id"]),
        handler_name=str(row["handler_name"]),
        args=[str(a) for a in args],
        scope={str(k): str(v) for k, v in scope.items()},
        bot_id=str(row["bot_id"]),
        key=str(row["key"]),
        recurring_seconds=float(row["recurring_seconds"]),
    )


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class Scheduler:
    """Event-loop scheduler with optional persistence.

    Construct, optionally pass a store, then ``run(callback)`` from
    the bot's main task. ``schedule()`` / ``schedule_recurring()`` /
    ``cancel()`` are safe to call before or while ``run()`` is active.

    Compatibility:
    * The legacy P0 ``delay(ms, handler_name, args, scope)`` API still
      exists as a thin wrapper around :meth:`schedule`.
    * The ``start()`` ↔ ``stop()`` lifecycle remains; ``run()`` is the
      new spelling that lets the caller name the task.
    """

    def __init__(self, store: SchedulerStore | None = None) -> None:
        self._store: SchedulerStore = store or MemorySchedulerStore()
        self._queue: list[ScheduledTask] = []
        self._cancelled: set[str] = set()
        self._task_index: dict[str, ScheduledTask] = {}
        self._counter = 0
        self._running = False

        # Hydrate any persisted tasks. ``fire_at`` is wall-clock so a
        # task whose deadline passed during downtime fires immediately
        # on the next tick.
        max_seq = 0
        for t in self._store.load_all():
            heapq.heappush(self._queue, t)
            self._task_index[t.id] = t
            # Resume id counter past whatever the store knows about,
            # otherwise a fresh process restarts at ``sched-1`` and
            # collides with surviving tasks.
            if t.id.startswith("sched-"):
                with contextlib.suppress(ValueError):
                    max_seq = max(max_seq, int(t.id[len("sched-") :]))
        self._counter = max_seq

    # -- introspection ------------------------------------------------

    @property
    def pending_count(self) -> int:
        """Number of tasks waiting to fire (excludes cancelled)."""
        return sum(
            1 for t in self._queue if t.id not in self._cancelled and t.id in self._task_index
        )

    # -- legacy P0 surface -------------------------------------------

    def delay(
        self,
        ms: int,
        handler_name: str,
        args: list[str] | None = None,
        scope: dict[str, str] | None = None,
    ) -> str:
        """Schedule a one-shot task to fire after ``ms`` milliseconds."""
        return self.schedule(
            after_seconds=ms / 1000.0,
            handler_name=handler_name,
            args=args,
            scope=scope,
        )

    # -- new surface -------------------------------------------------

    def schedule(
        self,
        *,
        after_seconds: float,
        handler_name: str,
        args: list[str] | None = None,
        scope: dict[str, str] | None = None,
        bot_id: str = "",
        key: str = "",
    ) -> str:
        """Schedule a one-shot task to fire after ``after_seconds``.

        ``key`` makes the call idempotent: scheduling another task
        with the same ``(bot_id, key)`` pair replaces the prior one.
        Useful for "set a single 'cooldown' timer per user" patterns.

        Returns the new task id.
        """
        if key:
            existing = self._store.delete_by_key(bot_id, key)
            if existing is not None:
                self._cancel_in_memory(existing)

        self._counter += 1
        task = ScheduledTask(
            fire_at=time.time() + max(after_seconds, 0.0),
            id=f"sched-{self._counter}",
            handler_name=handler_name,
            args=list(args or []),
            scope=dict(scope or {}),
            bot_id=bot_id,
            key=key,
        )
        heapq.heappush(self._queue, task)
        self._task_index[task.id] = task
        self._store.upsert(task)
        return task.id

    def schedule_recurring(
        self,
        *,
        every_seconds: float,
        handler_name: str,
        args: list[str] | None = None,
        scope: dict[str, str] | None = None,
        bot_id: str = "",
        key: str = "",
        first_fire_at: float | None = None,
    ) -> str:
        """Schedule a recurring task firing every ``every_seconds`` seconds.

        ``first_fire_at`` is a wall-clock timestamp for the first
        invocation; defaults to ``now + every_seconds`` so a freshly
        scheduled "every minute" job doesn't fire instantly. Pass an
        explicit value to align to wall clock (e.g. midnight UTC):

            scheduler.schedule_recurring(
                every_seconds=86_400,
                first_fire_at=next_midnight_utc(),
                handler_name="reset_cooldowns",
                key="daily-reset",
                bot_id="susu",
            )

        ``key`` is recommended for recurring tasks so a redeploy
        doesn't double up.
        """
        if every_seconds <= 0:
            raise ValueError("every_seconds must be positive")
        if key:
            existing = self._store.delete_by_key(bot_id, key)
            if existing is not None:
                self._cancel_in_memory(existing)

        self._counter += 1
        fire_at = first_fire_at if first_fire_at is not None else time.time() + every_seconds
        task = ScheduledTask(
            fire_at=fire_at,
            id=f"sched-{self._counter}",
            handler_name=handler_name,
            args=list(args or []),
            scope=dict(scope or {}),
            bot_id=bot_id,
            key=key,
            recurring_seconds=every_seconds,
        )
        heapq.heappush(self._queue, task)
        self._task_index[task.id] = task
        self._store.upsert(task)
        return task.id

    def cancel(self, task_id: str) -> bool:
        """Cancel a pending task. Returns ``True`` if it was queued."""
        if task_id in self._cancelled:
            return False
        if task_id not in self._task_index:
            return False
        self._cancelled.add(task_id)
        self._task_index.pop(task_id, None)
        self._store.delete(task_id)
        return True

    def _cancel_in_memory(self, task_id: str) -> None:
        """Drop a task from the live indexes without touching the store.

        Used when the store was already updated (e.g. by
        :meth:`SchedulerStore.delete_by_key`).
        """
        self._cancelled.add(task_id)
        self._task_index.pop(task_id, None)

    # -- lifecycle ----------------------------------------------------

    async def run(self, callback: SchedulerCallback) -> None:
        """Drain due tasks. Returns when :meth:`stop` is called.

        Per-tick contract: every task whose ``fire_at`` has passed
        gets its callback awaited *sequentially* before the next tick.
        Slow callbacks therefore delay subsequent firings — by design;
        operators who need parallelism wrap the callback in
        ``asyncio.create_task``.
        """
        self._running = True
        while self._running:
            now = time.time()
            while self._queue and self._queue[0].fire_at <= now:
                task = heapq.heappop(self._queue)
                if task.id in self._cancelled:
                    self._cancelled.discard(task.id)
                    continue
                if task.id not in self._task_index:
                    # Replaced by an idempotent re-schedule before fire.
                    continue
                try:
                    await callback(task)
                except Exception:
                    logger.exception(
                        "scheduler.callback_failed",
                        task_id=task.id,
                        handler=task.handler_name,
                    )
                if task.recurring_seconds > 0 and self._running:
                    # Re-arm. ``time.time() + interval`` keeps drift
                    # bounded even if the callback was slow; if exact
                    # cadence matters the operator can pre-compute the
                    # next aligned timestamp and ``schedule_recurring``
                    # again.
                    task.fire_at = time.time() + task.recurring_seconds
                    heapq.heappush(self._queue, task)
                    self._store.upsert(task)
                else:
                    self._task_index.pop(task.id, None)
                    self._store.delete(task.id)
            await asyncio.sleep(_TICK_SECONDS)

    async def start(self, callback: SchedulerCallback) -> None:
        """Compatibility alias for :meth:`run`."""
        await self.run(callback)

    async def stop(self) -> None:
        """Stop the run loop on the next tick."""
        self._running = False
