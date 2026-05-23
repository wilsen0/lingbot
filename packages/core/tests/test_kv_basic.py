"""Basic CRUD tests for the SQLite KV store."""

from __future__ import annotations

from pathlib import Path

import pytest
from linling_core import SqliteKVStore


async def _store(tmp_path: Path) -> SqliteKVStore:
    s = SqliteKVStore(bot_id="bot1", db_path=tmp_path / "kv.db")
    await s._ensure()
    return s


async def test_write_then_read_roundtrips(tmp_path: Path) -> None:
    async with SqliteKVStore("bot1", tmp_path / "kv.db") as s:
        await s.write("啊/灵玉系", "灵玉", "123", "456")
        assert await s.read("啊/灵玉系", "灵玉", "123") == "456"


async def test_read_missing_returns_default(tmp_path: Path) -> None:
    async with SqliteKVStore("bot1", tmp_path / "kv.db") as s:
        assert await s.read("a", "b", "c") is None
        assert await s.read("a", "b", "c", default="0") == "0"


async def test_write_is_upsert(tmp_path: Path) -> None:
    async with SqliteKVStore("bot1", tmp_path / "kv.db") as s:
        await s.write("a", "b", "k", "v1")
        await s.write("a", "b", "k", "v2")
        assert await s.read("a", "b", "k") == "v2"


async def test_empty_string_is_valid_value(tmp_path: Path) -> None:
    async with SqliteKVStore("bot1", tmp_path / "kv.db") as s:
        await s.write("a", "b", "k", "")
        assert await s.read("a", "b", "k") == ""


async def test_delete_single_key(tmp_path: Path) -> None:
    async with SqliteKVStore("bot1", tmp_path / "kv.db") as s:
        await s.write("a", "b", "k", "v")
        assert await s.delete("a", "b", "k") == 1
        assert await s.read("a", "b", "k") is None
        # deleting again is a no-op
        assert await s.delete("a", "b", "k") == 0


async def test_delete_whole_file(tmp_path: Path) -> None:
    async with SqliteKVStore("bot1", tmp_path / "kv.db") as s:
        await s.write("a", "b", "k1", "v")
        await s.write("a", "b", "k2", "v")
        await s.write("a", "c", "k1", "keep")
        assert await s.delete("a", "b") == 2
        assert await s.read("a", "b", "k1") is None
        assert await s.read("a", "c", "k1") == "keep"


async def test_delete_whole_scope(tmp_path: Path) -> None:
    async with SqliteKVStore("bot1", tmp_path / "kv.db") as s:
        await s.write("a", "b", "k", "v")
        await s.write("a", "c", "k", "v")
        await s.write("x", "y", "k", "keep")
        assert await s.delete("a") == 2
        assert await s.read("x", "y", "k") == "keep"


async def test_delete_key_without_file_is_rejected(tmp_path: Path) -> None:
    async with SqliteKVStore("bot1", tmp_path / "kv.db") as s:
        with pytest.raises(ValueError, match="without specifying file"):
            await s.delete("a", None, "k")


async def test_keys_and_files(tmp_path: Path) -> None:
    async with SqliteKVStore("bot1", tmp_path / "kv.db") as s:
        await s.write("a", "b", "k1", "v")
        await s.write("a", "b", "k2", "v")
        await s.write("a", "c", "k3", "v")
        assert sorted(await s.keys("a", "b")) == ["k1", "k2"]
        assert await s.files("a") == ["b", "c"]


async def test_bot_isolation(tmp_path: Path) -> None:
    db = tmp_path / "kv.db"
    async with SqliteKVStore("bot1", db) as s1, SqliteKVStore("bot2", db) as s2:
        await s1.write("a", "b", "k", "from-bot1")
        assert await s2.read("a", "b", "k") is None
        await s2.write("a", "b", "k", "from-bot2")
        assert await s1.read("a", "b", "k") == "from-bot1"


async def test_persists_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "kv.db"
    async with SqliteKVStore("bot1", db) as s:
        await s.write("a", "b", "k", "hello")
    async with SqliteKVStore("bot1", db) as s2:
        assert await s2.read("a", "b", "k") == "hello"


async def test_unicode_keys_and_values(tmp_path: Path) -> None:
    async with SqliteKVStore("bot1", tmp_path / "kv.db") as s:
        await s.write("啊/灵玉系", "灵玉", "涂山苏苏", "🦊🎐")
        assert await s.read("啊/灵玉系", "灵玉", "涂山苏苏") == "🦊🎐"
