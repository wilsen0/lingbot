"""End-to-end DSL flow for the rewritten 鱼塘 (fishing) module.

Exercises the real handler shapes from ``bot/rules/main.ling`` against
the full tool registry: the 起杆 draw → ``@结[...]`` JSON access, the
bucket sync/sell round-trip, and the legacy emoji-run migration. A
failure here means the DSL VM can't drive the new fishing tools the way
the rule file expects.
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator

import linling_tools_stdlib  # noqa: F401 — registers core + stdlib tools
import pytest
from linling_core import (
    Event,
    Scope,
    SqliteKVStore,
    TextSegment,
    User,
    registry,
)
from linling_dsl.parser import parse
from linling_dsl.vm import VM


@pytest.fixture
async def kv() -> AsyncIterator[SqliteKVStore]:
    store = SqliteKVStore(bot_id="fish_test", db_path=":memory:")
    try:
        yield store
    finally:
        await store.close()


def _event(text: str, *, sender: str = "12345", group: str = "67890") -> Event:
    return Event(
        id=f"e-{sender}-{text[:8]}",
        platform="cli",
        bot_id="fish_test",
        scope=Scope(kind="group", id=group, platform="cli"),
        sender=User(id=sender, platform="cli", display_name="渔夫"),
        segments=[TextSegment(text=text)],
    )


async def _run(source: str, kv: SqliteKVStore, ev: Event, **extras: object) -> str:
    full = "trigger\n" + source.strip("\n") + "\n"
    script = parse(full, strict=False)
    handler = script.handlers[0]
    vm = VM(tool_registry=registry, kv=kv, bot_id="fish_test", extras=dict(extras))
    res = await vm.execute_handler(handler, ev, captures=[])
    return "".join(s.text for s in res.segments if isinstance(s, TextSegment))


@pytest.mark.asyncio
async def test_draw_json_access_in_dsl(kv, tmp_path) -> None:
    """``结:$钓鱼抽签 ...$`` then ``@结[result]`` resolves a draw field.

    Uses the golden window (elapsed=150) so we reliably get a non-gone
    result; the exact result varies but the field must be one of the
    known kinds — proving the JSON access path works.
    """
    body = """现:9000000000
结:$钓鱼抽签 %QQ% 150 无$
类:@结[result]
%类%"""
    out = await _run(body, kv, _event("起杆"), random=random.Random(7))
    assert out in {"catch", "junk", "empty"}


@pytest.mark.asyncio
async def test_catch_persists_and_sells(kv, tmp_path) -> None:
    """A golden-window draw fills the bucket; 卖鱼 empties it for 灵玉."""
    rng = random.Random(3)
    # Hammer the draw enough that at least one real fish lands.
    draw_body = "结:$钓鱼抽签 %QQ% 150 无$\n@结[result]"
    landed = False
    for _ in range(40):
        out = await _run(draw_body, kv, _event("起杆"), random=rng)
        if out == "catch":
            landed = True
    assert landed

    # Sell: 钓鱼卖鱼 returns {count,value}; bucket clears.
    sell_body = """卖:$钓鱼卖鱼 %QQ%$
数:@卖[count]
价:@卖[value]
%数%-%价%"""
    out = await _run(sell_body, kv, _event("出售鱼虾"), random=rng)
    count_s, _, value_s = out.partition("-")
    assert int(count_s) > 0
    assert int(value_s) > 0
    # Bucket emptied.
    again = await _run(sell_body, kv, _event("出售鱼虾"), random=rng)
    assert again == "0-0"


@pytest.mark.asyncio
async def test_legacy_emoji_bucket_migrates_on_view(kv, tmp_path) -> None:
    """An old player's emoji-run bucket is folded in by 钓鱼背包同步."""
    # Seed the legacy field directly, as the old rule would have.
    await _run("$写 休闲系/钓鱼/水桶里有 %QQ% 🦀🐟🐟🦞$", kv, _event("seed"))
    body = """桶:$钓鱼背包同步 %QQ%$
%桶%"""
    out = await _run(body, kv, _event("查看水桶"), random=random.Random(1))
    # JSON now contains the migrated species.
    assert "草鱼" in out
    assert "螃蟹" in out
    assert "龙虾" in out


@pytest.mark.asyncio
async def test_gone_when_idle_too_long(kv, tmp_path) -> None:
    """elapsed beyond the ceiling → gone, no bucket write."""
    body = """结:$钓鱼抽签 %QQ% 99999 无$
@结[result]"""
    out = await _run(body, kv, _event("起杆"), random=random.Random(1))
    assert out == "gone"
    bucket = await kv.read("休闲系/钓鱼", "水桶", "12345", "{}")
    assert bucket == "{}"


@pytest.mark.asyncio
async def test_enchant_roll_in_dsl(kv, tmp_path) -> None:
    """附魔抽取 returns a buff the rule can store via @抽[buff]."""
    body = """抽:$附魔抽取 3$
新:@抽[buff]
次:@抽[charges]
%新%-%次%"""
    out = await _run(body, kv, _event("鱼竿附魔"), random=random.Random(5))
    buff, _, charges = out.partition("-")
    assert buff in {"幸运", "驱垃圾", "守时", "肥美"}
    assert charges == "3"
