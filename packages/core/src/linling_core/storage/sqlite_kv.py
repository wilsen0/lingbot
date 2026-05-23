"""SQLite-backed KV store.

- WAL + synchronous=NORMAL for single-process async safety.
- CAST(value AS REAL) for numeric ranking; non-numeric sorts as 0.0.
- Single shared aiosqlite connection; serialised internally.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import aiosqlite

from linling_core.storage.kv import RankOrder, RankRow

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    bot_id      TEXT NOT NULL,
    scope       TEXT NOT NULL,
    file        TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (bot_id, scope, file, key)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS kv_scope_file ON kv(bot_id, scope, file);
"""


def _format_row(fmt: str, row: RankRow) -> str:
    return fmt.replace("[序号]", str(row.rank)).replace("[键]", row.key).replace("[值]", row.value)


class SqliteKVStore:
    """Async SQLite KV store bound to a single bot_id."""

    def __init__(self, bot_id: str, db_path: str | Path = ":memory:") -> None:
        self._bot_id = bot_id
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None
        self._init_lock = asyncio.Lock()
        # Serialises explicit transactions started via :meth:`transaction`
        # so two coroutines can't both ``BEGIN`` on our single shared
        # connection (SQLite raises ``OperationalError: cannot start a
        # transaction within a transaction`` otherwise). Implicit
        # auto-commits from :meth:`write` / :meth:`delete` etc. are
        # still allowed concurrently — aiosqlite serialises them on
        # its own queue.
        self._tx_lock = asyncio.Lock()

    # --- lifecycle ---

    async def _ensure(self) -> aiosqlite.Connection:
        if self._conn is not None:
            return self._conn
        async with self._init_lock:
            if self._conn is not None:
                return self._conn
            if self._db_path != ":memory:":
                Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = await aiosqlite.connect(self._db_path)
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA synchronous=NORMAL;")
            await conn.execute("PRAGMA foreign_keys=ON;")
            await conn.executescript(SCHEMA)
            await conn.commit()
            self._conn = conn
            return conn

    async def close(self) -> None:
        async with self._init_lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None

    async def __aenter__(self) -> SqliteKVStore:
        await self._ensure()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    # --- basic CRUD ---

    async def read(self, scope: str, file: str, key: str, default: str | None = None) -> str | None:
        conn = await self._ensure()
        cur = await conn.execute(
            "SELECT value FROM kv WHERE bot_id=? AND scope=? AND file=? AND key=?",
            (self._bot_id, scope, file, key),
        )
        row = await cur.fetchone()
        return default if row is None else str(row["value"])

    async def write(self, scope: str, file: str, key: str, value: str) -> None:
        conn = await self._ensure()
        await conn.execute(
            "INSERT INTO kv(bot_id, scope, file, key, value, updated_at) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(bot_id, scope, file, key) DO UPDATE SET "
            "  value=excluded.value, updated_at=excluded.updated_at",
            (self._bot_id, scope, file, key, value, int(time.time())),
        )
        await conn.commit()

    async def delete(self, scope: str, file: str | None = None, key: str | None = None) -> int:
        conn = await self._ensure()
        sql = "DELETE FROM kv WHERE bot_id=? AND scope=?"
        args: list[Any] = [self._bot_id, scope]
        if file is not None:
            sql += " AND file=?"
            args.append(file)
            if key is not None:
                sql += " AND key=?"
                args.append(key)
        elif key is not None:
            raise ValueError("cannot delete by key without specifying file")
        cur = await conn.execute(sql, args)
        removed = cur.rowcount
        await conn.commit()
        return int(removed or 0)

    # --- discovery ---

    async def keys(self, scope: str, file: str) -> list[str]:
        conn = await self._ensure()
        cur = await conn.execute(
            "SELECT key FROM kv WHERE bot_id=? AND scope=? AND file=?",
            (self._bot_id, scope, file),
        )
        return [str(r["key"]) async for r in cur]

    async def files(self, scope: str) -> list[str]:
        conn = await self._ensure()
        cur = await conn.execute(
            "SELECT DISTINCT file FROM kv WHERE bot_id=? AND scope=? ORDER BY file",
            (self._bot_id, scope),
        )
        return [str(r["file"]) async for r in cur]

    async def scopes(self) -> list[str]:
        conn = await self._ensure()
        cur = await conn.execute(
            "SELECT DISTINCT scope FROM kv WHERE bot_id=? ORDER BY scope",
            (self._bot_id,),
        )
        return [str(r["scope"]) async for r in cur]

    # --- ranking ---

    async def rank_rows(
        self,
        scope: str,
        file: str,
        *,
        order: RankOrder = RankOrder.DESC,
        top: int = 10,
    ) -> list[RankRow]:
        if top <= 0:
            return []
        direction = "DESC" if order is RankOrder.DESC else "ASC"
        conn = await self._ensure()
        cur = await conn.execute(
            "SELECT key, value, CAST(value AS REAL) AS n FROM kv "
            "WHERE bot_id=? AND scope=? AND file=? "
            f"ORDER BY n {direction}, key ASC LIMIT ?",
            (self._bot_id, scope, file, top),
        )
        rows: list[RankRow] = []
        rank = 0
        async for r in cur:
            rank += 1
            rows.append(
                RankRow(
                    rank=rank,
                    key=str(r["key"]),
                    value=str(r["value"]),
                    numeric=float(r["n"]),
                )
            )
        return rows

    async def rank(
        self,
        scope: str,
        file: str,
        *,
        order: RankOrder = RankOrder.DESC,
        top: int = 10,
        sep: str = "\n",
        fmt: str = "[序号]. [键] [值]",
    ) -> str:
        rows = await self.rank_rows(scope, file, order=order, top=top)
        return sep.join(_format_row(fmt, r) for r in rows)

    # --- transactions ---

    def transaction(self) -> _Transaction:
        """Group multiple writes into one commit.

        async with store.transaction() as tx:
            await tx.write(...)

        Commits on success, rolls back on exception.
        """
        return _Transaction(self)

    async def _begin(self) -> None:
        conn = await self._ensure()
        await conn.execute("BEGIN")

    async def _commit(self) -> None:
        assert self._conn is not None
        await self._conn.commit()

    async def _rollback(self) -> None:
        assert self._conn is not None
        await self._conn.rollback()


class _Transaction:
    """Async context manager grouping multiple writes into one commit."""

    def __init__(self, store: SqliteKVStore) -> None:
        self._store = store
        self._entered = False
        self._lock_held = False

    async def __aenter__(self) -> _Transaction:
        # Hold the transaction lock for the duration of the with block
        # so a concurrent ``async with store.transaction()`` waits
        # rather than races into a nested ``BEGIN`` (illegal in SQLite).
        await self._store._tx_lock.acquire()
        self._lock_held = True
        try:
            await self._store._begin()
        except Exception:
            self._store._tx_lock.release()
            self._lock_held = False
            raise
        self._entered = True
        return self

    async def __aexit__(self, exc_type: object, *_: object) -> None:
        try:
            if not self._entered:
                return
            if exc_type is None:
                await self._store._commit()
            else:
                await self._store._rollback()
        finally:
            if self._lock_held:
                self._store._tx_lock.release()
                self._lock_held = False

    async def write(self, scope: str, file: str, key: str, value: str) -> None:
        conn = await self._store._ensure()
        await conn.execute(
            "INSERT INTO kv(bot_id, scope, file, key, value, updated_at) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(bot_id, scope, file, key) DO UPDATE SET "
            "  value=excluded.value, updated_at=excluded.updated_at",
            (self._store._bot_id, scope, file, key, value, int(time.time())),
        )

    async def read(self, scope: str, file: str, key: str, default: str | None = None) -> str | None:
        return await self._store.read(scope, file, key, default)
