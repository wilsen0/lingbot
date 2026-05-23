"""Audit log reader.

Two backends, same interface:

* :class:`AuditReader` — in-memory ring buffer. Default. Fast, zero
  config, but rows die with the process. Used in tests and in
  deployments that prefer to ship logs/metrics elsewhere.

* :class:`SqliteAuditReader` — durable on-disk store with indexed
  ``search``. Wired by the bootstrap when ``audit.kv`` (or a future
  ``audit.url``) is configured in ``bot.yaml``.

Both expose the same :class:`AuditReaderProtocol` so every consumer
(``/api/audit``, ``/ws/rules/hits``, the router's audit sink) works
with either backend without conditionals.

The schema is conservative — :class:`AuditRow` plus a JSON ``payload``
column. The indices target the common filter axes; full-text search
on ``payload`` is intentionally absent (we paginate via ``time DESC``
and let operators export to a real OLAP system if they need it).
"""

from __future__ import annotations

import itertools
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class AuditRow:
    id: str
    time: float
    bot_id: str
    user_id: str
    scope_id: str
    kind: str
    outcome: str
    latency_ms: float | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class AuditReaderProtocol(Protocol):
    """Common surface for both audit backends."""

    def append(
        self,
        *,
        bot_id: str,
        user_id: str,
        scope_id: str,
        kind: str,
        outcome: str = "ok",
        latency_ms: float | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AuditRow: ...

    def search(
        self,
        *,
        bot_ids: list[str] | None = None,
        user_id: str | None = None,
        kind: str | None = None,
        outcome: str | None = None,
        since: float | None = None,
        until: float | None = None,
        q: str | None = None,
        limit: int = 100,
    ) -> list[AuditRow]: ...

    def subscribe(self, callback: Callable[[AuditRow], None]) -> Callable[[], None]: ...


# ---------------------------------------------------------------------------
# Helpers shared by both backends.
# ---------------------------------------------------------------------------


def _new_row(
    *,
    bot_id: str,
    user_id: str,
    scope_id: str,
    kind: str,
    outcome: str,
    latency_ms: float | None,
    payload: dict[str, Any] | None,
) -> AuditRow:
    return AuditRow(
        id=uuid.uuid4().hex,
        time=time.time(),
        bot_id=bot_id,
        user_id=user_id,
        scope_id=scope_id,
        kind=kind,
        outcome=outcome,
        latency_ms=latency_ms,
        payload=payload or {},
    )


def _matches(
    r: AuditRow,
    *,
    bot_ids: set[str] | None,
    user_id: str | None,
    kind: str | None,
    outcome: str | None,
    since: float | None,
    until: float | None,
    q: str | None,
) -> bool:
    if bot_ids is not None and r.bot_id not in bot_ids:
        return False
    if user_id and r.user_id != user_id:
        return False
    if kind and r.kind != kind:
        return False
    if outcome and r.outcome != outcome:
        return False
    if since is not None and r.time < since:
        return False
    if until is not None and r.time > until:
        return False
    if q:
        hay = r.kind + " " + r.user_id + " " + r.scope_id + " " + str(r.payload)
        if q not in hay:
            return False
    return True


# ---------------------------------------------------------------------------
# In-memory backend (default).
# ---------------------------------------------------------------------------


class AuditReader:
    """Bounded in-memory audit store with subscribe-style fan-out.

    Newest entry last; ``search`` walks in reverse so callers see the
    most recent rows first up to ``limit``. Capacity is ``5000`` by
    default — enough for an interactive dashboard, not enough for a
    long-tail forensic search. Use :class:`SqliteAuditReader` for that.
    """

    def __init__(self, *, capacity: int = 5000) -> None:
        self._capacity = capacity
        self._rows: list[AuditRow] = []
        self._seq = itertools.count(1)
        self._subscribers: set[Callable[[AuditRow], None]] = set()

    def subscribe(self, callback: Callable[[AuditRow], None]) -> Callable[[], None]:
        """Register a sync callback to receive every future :class:`AuditRow`.

        Returns an unsubscribe function. The callback runs inline with
        :meth:`append`, so it must not block — subscribers typically
        push into an :class:`asyncio.Queue` and return.
        """
        self._subscribers.add(callback)
        return lambda: self._subscribers.discard(callback)

    def append(
        self,
        *,
        bot_id: str,
        user_id: str,
        scope_id: str,
        kind: str,
        outcome: str = "ok",
        latency_ms: float | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AuditRow:
        row = _new_row(
            bot_id=bot_id,
            user_id=user_id,
            scope_id=scope_id,
            kind=kind,
            outcome=outcome,
            latency_ms=latency_ms,
            payload=payload,
        )
        self._rows.append(row)
        if len(self._rows) > self._capacity:
            self._rows = self._rows[-self._capacity :]
        _broadcast(self._subscribers, row)
        return row

    def search(
        self,
        *,
        bot_ids: list[str] | None = None,
        user_id: str | None = None,
        kind: str | None = None,
        outcome: str | None = None,
        since: float | None = None,
        until: float | None = None,
        q: str | None = None,
        limit: int = 100,
    ) -> list[AuditRow]:
        allowed = set(bot_ids) if bot_ids is not None else None
        out: list[AuditRow] = []
        for r in reversed(self._rows):
            if not _matches(
                r,
                bot_ids=allowed,
                user_id=user_id,
                kind=kind,
                outcome=outcome,
                since=since,
                until=until,
                q=q,
            ):
                continue
            out.append(r)
            if len(out) >= limit:
                break
        return out


# ---------------------------------------------------------------------------
# SQLite-backed backend.
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id          TEXT PRIMARY KEY,
    time        REAL NOT NULL,
    bot_id      TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    scope_id    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    latency_ms  REAL,
    payload     TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS audit_bot_time ON audit(bot_id, time DESC);
CREATE INDEX IF NOT EXISTS audit_kind_time ON audit(kind, time DESC);
CREATE INDEX IF NOT EXISTS audit_user_time ON audit(user_id, time DESC);
"""


class SqliteAuditReader:
    """SQLite-backed audit store.

    Drop-in replacement for :class:`AuditReader`. Configure via
    ``audit.kv: sqlite:///path/to/audit.db`` in ``bot.yaml``.

    Concurrency: uses one shared :class:`sqlite3.Connection` guarded by
    a :class:`threading.Lock`. The router and adapters run on the
    asyncio loop and call us via the sync :meth:`append` — locking is
    fine because individual inserts complete in tens of microseconds
    and we never hold the lock across an ``await``.

    Retention: :meth:`sweep` deletes rows older than ``ttl_seconds``.
    Run from a periodic task or a cron — not on every append.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        ttl_seconds: float | None = 30 * 24 * 3600,
    ) -> None:
        self._path = str(path)
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._subscribers: set[Callable[[AuditRow], None]] = set()
        self._conn = self._open()

    def _open(self) -> sqlite3.Connection:
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        # ``check_same_thread=False`` is safe because we serialise via
        # ``self._lock``. Without it the connection would refuse calls
        # from any thread other than the creator.
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.executescript(_SCHEMA)
        conn.commit()
        return conn

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- subscribers ----------------------------------------------------

    def subscribe(self, callback: Callable[[AuditRow], None]) -> Callable[[], None]:
        self._subscribers.add(callback)
        return lambda: self._subscribers.discard(callback)

    # -- write ----------------------------------------------------------

    def append(
        self,
        *,
        bot_id: str,
        user_id: str,
        scope_id: str,
        kind: str,
        outcome: str = "ok",
        latency_ms: float | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AuditRow:
        row = _new_row(
            bot_id=bot_id,
            user_id=user_id,
            scope_id=scope_id,
            kind=kind,
            outcome=outcome,
            latency_ms=latency_ms,
            payload=payload,
        )
        # Serialise payload outside the lock to keep the critical section
        # small. JSON encoding of small dicts is microseconds.
        payload_text = json.dumps(row.payload, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO audit
                    (id, time, bot_id, user_id, scope_id, kind, outcome, latency_ms, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.id,
                    row.time,
                    row.bot_id,
                    row.user_id,
                    row.scope_id,
                    row.kind,
                    row.outcome,
                    row.latency_ms,
                    payload_text,
                ),
            )
            self._conn.commit()
        _broadcast(self._subscribers, row)
        return row

    # -- read -----------------------------------------------------------

    def search(
        self,
        *,
        bot_ids: list[str] | None = None,
        user_id: str | None = None,
        kind: str | None = None,
        outcome: str | None = None,
        since: float | None = None,
        until: float | None = None,
        q: str | None = None,
        limit: int = 100,
    ) -> list[AuditRow]:
        clauses: list[str] = []
        params: list[Any] = []
        if bot_ids is not None:
            if not bot_ids:
                # Empty allow-list → empty result, as for memory backend.
                return []
            placeholders = ",".join("?" * len(bot_ids))
            clauses.append(f"bot_id IN ({placeholders})")
            params.extend(bot_ids)
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if outcome:
            clauses.append("outcome = ?")
            params.append(outcome)
        if since is not None:
            clauses.append("time >= ?")
            params.append(since)
        if until is not None:
            clauses.append("time <= ?")
            params.append(until)
        # We deliberately do NOT push ``q`` into SQL: payload is JSON
        # text and matching needs the same forgiving substring rules
        # the memory backend uses. Filter post-query — fine because
        # ``limit`` typically << total.

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM audit {where} ORDER BY time DESC LIMIT ?"
        # Pull more than ``limit`` if ``q`` is set so we can drop
        # non-matching rows without short-changing the response.
        sql_limit = limit * 5 if q else limit
        params.append(sql_limit)

        with self._lock:
            cursor = self._conn.execute(sql, params)
            rows = cursor.fetchall()

        out: list[AuditRow] = []
        for db_row in rows:
            r = _row_from_db(db_row)
            if q:
                hay = r.kind + " " + r.user_id + " " + r.scope_id + " " + str(r.payload)
                if q not in hay:
                    continue
            out.append(r)
            if len(out) >= limit:
                break
        return out

    # -- maintenance ----------------------------------------------------

    def sweep(self, *, now: float | None = None) -> int:
        """Delete rows older than ``ttl_seconds``. Returns rows removed."""
        if self._ttl is None:
            return 0
        cutoff = (now or time.time()) - self._ttl
        with self._lock:
            cur = self._conn.execute("DELETE FROM audit WHERE time < ?", (cutoff,))
            self._conn.commit()
            return int(cur.rowcount or 0)

    def count(self) -> int:
        """Diagnostic — total rows in the table."""
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM audit")
            return int(cur.fetchone()[0])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _broadcast(
    subscribers: set[Callable[[AuditRow], None]], row: AuditRow
) -> None:
    """Fire every subscriber with ``row``; never raise."""
    # Snapshot in case a callback unsubscribes itself.
    for cb in list(subscribers):
        try:
            cb(row)
        except Exception:
            logger.exception("audit.subscriber_failed")


def _row_from_db(db_row: sqlite3.Row) -> AuditRow:
    try:
        payload = json.loads(db_row["payload"])
    except (json.JSONDecodeError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return AuditRow(
        id=str(db_row["id"]),
        time=float(db_row["time"]),
        bot_id=str(db_row["bot_id"]),
        user_id=str(db_row["user_id"]),
        scope_id=str(db_row["scope_id"]),
        kind=str(db_row["kind"]),
        outcome=str(db_row["outcome"]),
        latency_ms=db_row["latency_ms"],
        payload=payload,
    )
