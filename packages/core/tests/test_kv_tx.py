"""Transaction and concurrency tests for SqliteKVStore."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from linling_core import SqliteKVStore


async def test_transaction_commits_on_success(tmp_path: Path) -> None:
    async with SqliteKVStore("bot1", tmp_path / "kv.db") as s:
        async with s.transaction() as tx:
            await tx.write("a", "b", "k1", "v1")
            await tx.write("a", "b", "k2", "v2")
        assert await s.read("a", "b", "k1") == "v1"
        assert await s.read("a", "b", "k2") == "v2"


async def test_transaction_rolls_back_on_exception(tmp_path: Path) -> None:
    async with SqliteKVStore("bot1", tmp_path / "kv.db") as s:
        await s.write("a", "b", "k", "original")
        with pytest.raises(RuntimeError, match="boom"):
            async with s.transaction() as tx:
                await tx.write("a", "b", "k", "overwritten")
                await tx.write("a", "b", "new", "added")
                raise RuntimeError("boom")
        # Both changes rolled back.
        assert await s.read("a", "b", "k") == "original"
        assert await s.read("a", "b", "new") is None


async def test_concurrent_writes_are_all_persisted(tmp_path: Path) -> None:
    """Many tasks each writing distinct keys must all succeed.

    aiosqlite serialises internally; we want to confirm there is no
    lost-update or deadlock.
    """
    async with SqliteKVStore("bot1", tmp_path / "kv.db") as s:

        async def worker(i: int) -> None:
            await s.write("a", "b", f"k{i}", str(i))

        await asyncio.gather(*(worker(i) for i in range(50)))
        keys = await s.keys("a", "b")
        assert len(keys) == 50
        for i in range(50):
            assert await s.read("a", "b", f"k{i}") == str(i)


async def test_close_is_idempotent(tmp_path: Path) -> None:
    s = SqliteKVStore("bot1", tmp_path / "kv.db")
    await s._ensure()
    await s.close()
    await s.close()  # should not raise


async def test_double_enter_reuses_connection(tmp_path: Path) -> None:
    s = SqliteKVStore("bot1", tmp_path / "kv.db")
    c1 = await s._ensure()
    c2 = await s._ensure()
    assert c1 is c2
    await s.close()
