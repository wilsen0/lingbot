"""Leaderboard semantics — aligned with QRDic's ``$排行榜$``."""

from __future__ import annotations

from pathlib import Path

from linling_core import RankOrder, SqliteKVStore


async def _seeded(tmp_path: Path) -> SqliteKVStore:
    s = SqliteKVStore("bot1", tmp_path / "kv.db")
    await s._ensure()
    for k, v in [
        ("alice", "100"),
        ("bob", "300"),
        ("carol", "200"),
        ("dave", "50"),
    ]:
        await s.write("啊/灵玉系", "灵玉", k, v)
    return s


async def test_rank_rows_desc_by_numeric_value(tmp_path: Path) -> None:
    s = await _seeded(tmp_path)
    rows = await s.rank_rows("啊/灵玉系", "灵玉", order=RankOrder.DESC, top=10)
    assert [r.key for r in rows] == ["bob", "carol", "alice", "dave"]
    assert [r.rank for r in rows] == [1, 2, 3, 4]
    assert rows[0].numeric == 300.0


async def test_rank_rows_asc(tmp_path: Path) -> None:
    s = await _seeded(tmp_path)
    rows = await s.rank_rows("啊/灵玉系", "灵玉", order=RankOrder.ASC, top=2)
    assert [r.key for r in rows] == ["dave", "alice"]


async def test_rank_respects_top_n(tmp_path: Path) -> None:
    s = await _seeded(tmp_path)
    rows = await s.rank_rows("啊/灵玉系", "灵玉", top=2)
    assert len(rows) == 2
    assert rows[0].key == "bob"


async def test_rank_top_zero_returns_empty(tmp_path: Path) -> None:
    s = await _seeded(tmp_path)
    assert await s.rank_rows("啊/灵玉系", "灵玉", top=0) == []


async def test_rank_with_non_numeric_values_sorts_them_as_zero(tmp_path: Path) -> None:
    async with SqliteKVStore("bot1", tmp_path / "kv.db") as s:
        await s.write("a", "b", "x", "not a number")
        await s.write("a", "b", "y", "42")
        await s.write("a", "b", "z", "")
        rows = await s.rank_rows("a", "b", order=RankOrder.DESC, top=10)
        # numeric -> 42 > 0 (both the empty and the non-numeric cast to 0.0)
        assert rows[0].key == "y"
        # tie break on key ascending
        assert [r.key for r in rows[1:]] == ["x", "z"]


async def test_rank_format_default(tmp_path: Path) -> None:
    s = await _seeded(tmp_path)
    out = await s.rank("啊/灵玉系", "灵玉", top=3)
    assert out == "1. bob 300\n2. carol 200\n3. alice 100"


async def test_rank_format_custom_tokens(tmp_path: Path) -> None:
    s = await _seeded(tmp_path)
    out = await s.rank(
        "啊/灵玉系",
        "灵玉",
        top=2,
        sep=" | ",
        fmt="榜[序号]：[键]=[值]",
    )
    assert out == "榜1：bob=300 | 榜2：carol=200"


async def test_rank_order_parse_accepts_chinese_aliases() -> None:
    assert RankOrder.parse("反序") is RankOrder.DESC
    assert RankOrder.parse("正序") is RankOrder.ASC
    assert RankOrder.parse("desc") is RankOrder.DESC


async def test_rank_empty_file_returns_empty_string(tmp_path: Path) -> None:
    async with SqliteKVStore("bot1", tmp_path / "kv.db") as s:
        assert await s.rank("empty", "empty") == ""
