"""Tests for StickerStore: filesystem blob storage + KV index."""

from __future__ import annotations

import os
from pathlib import Path

import linling_agent.sticker_store as sticker_module
import pytest
from linling_agent.sticker_store import StickerStore
from linling_core.storage.sqlite_kv import SqliteKVStore


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


@pytest.fixture
async def kv():
    store = SqliteKVStore(bot_id="bot1", db_path=":memory:")
    async with store:
        yield store


@pytest.fixture
def store(kv, tmp_path: Path) -> StickerStore:
    return StickerStore(kv, tmp_path)


class _FakeTime:
    """Stub for the ``time`` module so created_at is controllable."""

    def __init__(self, values: list[int]) -> None:
        self._values = iter(values)

    def time(self) -> int:
        return next(self._values)


# ---------------------------------------------------------------------------
# save / load
# ---------------------------------------------------------------------------


async def test_save_and_load_roundtrip(kv, tmp_path: Path) -> None:
    store = StickerStore(kv, tmp_path)
    data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    sid = await store.save(data, name="猫")
    assert await store.count() == 1
    assert await store.load_bytes(sid) == data

    items = await store.list()
    assert len(items) == 1
    assert items[0]["id"] == sid
    assert items[0]["name"] == "猫"
    assert items[0]["created_at"] > 0


async def test_save_dedup(kv, tmp_path: Path) -> None:
    store = StickerStore(kv, tmp_path)
    data = b"identical-bytes"
    first = await store.save(data, name="猫", tags="可爱")
    second = await store.save(data, name="猫猫", tags="", source_qq="u1")
    # Same content -> same id, no second row.
    assert first == second
    assert await store.count() == 1

    items = await store.list()
    assert len(items) == 1
    # Metadata refreshed only for the fields the caller provided.
    assert items[0]["name"] == "猫猫"
    assert items[0]["source_qq"] == "u1"
    assert items[0]["tags"] == "可爱"


async def test_save_different_content(kv, tmp_path: Path) -> None:
    store = StickerStore(kv, tmp_path)
    sid1 = await store.save(b"content-1", name="a")
    sid2 = await store.save(b"content-2", name="b")
    assert sid1 != sid2
    assert await store.count() == 2


# ---------------------------------------------------------------------------
# find_by_name
# ---------------------------------------------------------------------------


async def test_find_by_name_exact(kv, tmp_path: Path) -> None:
    store = StickerStore(kv, tmp_path)
    await store.save(b"aaa", name="猫")
    item = await store.find_by_name("猫")
    assert item is not None
    assert item["name"] == "猫"


async def test_find_by_name_partial(kv, tmp_path: Path) -> None:
    store = StickerStore(kv, tmp_path)
    await store.save(b"aaa", name="猫猫收藏")
    item = await store.find_by_name("猫")
    assert item is not None
    assert item["name"] == "猫猫收藏"


async def test_find_by_name_exact_prefers_exact(kv, tmp_path: Path) -> None:
    store = StickerStore(kv, tmp_path)
    await store.save(b"one", name="猫咪")
    await store.save(b"two", name="猫")
    item = await store.find_by_name("猫")
    assert item is not None
    assert item["name"] == "猫"


async def test_find_by_name_missing(kv, tmp_path: Path) -> None:
    store = StickerStore(kv, tmp_path)
    await store.save(b"aaa", name="猫")
    assert await store.find_by_name("不存在的名字") is None
    assert await store.find_by_name("") is None


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


async def test_list_query_filter(kv, tmp_path: Path) -> None:
    store = StickerStore(kv, tmp_path)
    await store.save(b"1", name="猫", tags="可爱")
    await store.save(b"2", name="狗", tags="忠诚")
    await store.save(b"3", name="狐狸", tags="聪明 可爱")

    matches = await store.list(query="可爱")
    assert {m["name"] for m in matches} == {"猫", "狐狸"}

    matches = await store.list(query="狗")
    assert [m["name"] for m in matches] == ["狗"]

    assert await store.list(query="不存在") == []


async def test_list_order_newest_first(kv, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sticker_module, "time", _FakeTime([100, 200, 300]))
    store = StickerStore(kv, tmp_path)
    await store.save(b"1", name="oldest")
    await store.save(b"2", name="middle")
    await store.save(b"3", name="newest")
    assert [m["name"] for m in await store.list()] == ["newest", "middle", "oldest"]


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete(kv, tmp_path: Path) -> None:
    store = StickerStore(kv, tmp_path)
    sid = await store.save(b"deleteme", name="x")
    assert await store.delete(sid) is True
    assert await store.load_bytes(sid) is None
    assert await store.count() == 0
    # Second delete finds nothing.
    assert await store.delete(sid) is False


async def test_delete_unknown_id(kv, tmp_path: Path) -> None:
    store = StickerStore(kv, tmp_path)
    assert await store.delete("does-not-exist") is False


# ---------------------------------------------------------------------------
# capacity + degraded dir
# ---------------------------------------------------------------------------


async def test_capacity_limit(kv, tmp_path: Path) -> None:
    store = StickerStore(kv, tmp_path, max_stickers=2)
    await store.save(b"1", name="a")
    await store.save(b"2", name="b")
    with pytest.raises(ValueError):
        await store.save(b"3", name="c")
    assert await store.count() == 2


async def test_dir_none_degrades(kv) -> None:
    store = StickerStore(kv, None)
    with pytest.raises(ValueError):
        await store.save(b"data", name="x")
    assert await store.load_bytes("any-id") is None
    assert await store.list() == []
    assert await store.count() == 0
    assert await store.delete("any-id") is False


# ---------------------------------------------------------------------------
# disk layout
# ---------------------------------------------------------------------------


async def test_mime_extension(kv, tmp_path: Path) -> None:
    store = StickerStore(kv, tmp_path)
    png_id = await store.save(b"png-bytes", name="p", mime="image/png")
    gif_id = await store.save(b"gif-bytes", name="g", mime="image/gif")
    default_id = await store.save(b"other-bytes", name="o", mime="application/octet-stream")

    files = set(os.listdir(tmp_path))
    assert f"{png_id}.png" in files
    assert f"{gif_id}.gif" in files
    assert f"{default_id}.jpg" in files  # unknown mime falls back to .jpg
    assert _read_bytes(os.path.join(tmp_path, f"{png_id}.png")) == b"png-bytes"
