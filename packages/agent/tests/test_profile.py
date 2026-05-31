"""Tests for ProfileStore and render_profile_block (Phase 1)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from linling_agent.profile import (
    PROFILE_MAX_CHARS,
    ProfileStore,
    render_profile_block,
)
from linling_core.storage.sqlite_kv import SqliteKVStore


@pytest.fixture
async def kv():
    store = SqliteKVStore(bot_id="bot1", db_path=":memory:")
    async with store:
        yield store


# ---------------------------------------------------------------------------
# render_profile_block (pure)
# ---------------------------------------------------------------------------


def test_render_empty_profile_returns_none() -> None:
    assert render_profile_block("123", "nick", "") is None
    assert render_profile_block("123", "nick", "   ") is None


def test_render_includes_qq_name_and_body() -> None:
    block = render_profile_block("123", "小红", "喜欢钓鱼")
    assert block is not None
    assert "<user_profile" in block
    assert 'qq="123"' in block
    assert 'name="小红"' in block
    assert "喜欢钓鱼" in block
    assert "</user_profile>" in block
    # Guard preface present so profile text isn't read as an instruction.
    assert "不是指令" in block


def test_render_without_name_omits_attr() -> None:
    block = render_profile_block("123", None, "body")
    assert block is not None
    assert "name=" not in block
    assert 'qq="123"' in block


def test_render_escapes_special_chars() -> None:
    block = render_profile_block("1&2", '"<bad>"', "a < b & c > d")
    assert block is not None
    assert "&amp;" in block
    assert "&lt;" in block
    assert "&gt;" in block
    assert "&quot;" in block


# ---------------------------------------------------------------------------
# ProfileStore
# ---------------------------------------------------------------------------


async def test_save_load_roundtrip(kv) -> None:
    store = ProfileStore(kv)
    await store.save("123", "喜欢钓鱼，称呼我老板")
    assert await store.load("123") == "喜欢钓鱼，称呼我老板"


async def test_load_missing_returns_empty(kv) -> None:
    store = ProfileStore(kv)
    assert await store.load("999") == ""
    assert await store.load_name("999") == ""


async def test_save_is_full_rewrite(kv) -> None:
    store = ProfileStore(kv)
    await store.save("123", "第一版画像")
    await store.save("123", "完全不同的第二版")
    # Full rewrite — no append/merge.
    assert await store.load("123") == "完全不同的第二版"


async def test_save_clamps_to_max_chars(kv) -> None:
    store = ProfileStore(kv)
    long_text = "字" * (PROFILE_MAX_CHARS + 50)
    await store.save("123", long_text)
    assert len(await store.load("123")) == PROFILE_MAX_CHARS


async def test_empty_qq_is_noop(kv) -> None:
    store = ProfileStore(kv)
    await store.save("", "should not persist")
    await store.touch_name("", "name")
    assert await store.load("") == ""
    assert await store.load_name("") == ""


async def test_save_with_name_upserts_name(kv) -> None:
    store = ProfileStore(kv)
    await store.save("123", "画像", name="小绿")
    assert await store.load_name("123") == "小绿"


async def test_touch_name_updates_only_name(kv) -> None:
    store = ProfileStore(kv)
    await store.save("123", "画像")
    await store.touch_name("123", "新昵称")
    assert await store.load_name("123") == "新昵称"
    assert await store.load("123") == "画像"  # body untouched


async def test_touch_name_empty_name_noop(kv) -> None:
    store = ProfileStore(kv)
    await store.touch_name("123", "")
    assert await store.load_name("123") == ""


# ---------------------------------------------------------------------------
# Property-based: full-rewrite + clamp invariants
# Feature: user-profile-memory, Property 2 (full rewrite), Property 3 (clamp)
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(p1=st.text(max_size=600), p2=st.text(max_size=600))
async def test_property_full_rewrite(p1: str, p2: str) -> None:
    """Property 2: the second write fully replaces the first, clamp applied."""
    store = SqliteKVStore(bot_id="bot1", db_path=":memory:")
    async with store:
        profiles = ProfileStore(store)
        await profiles.save("qq", p1)
        await profiles.save("qq", p2)
        assert await profiles.load("qq") == p2[:PROFILE_MAX_CHARS]


@settings(max_examples=200, deadline=None)
@given(p=st.text(max_size=1000))
async def test_property_clamp_never_exceeds_cap(p: str) -> None:
    """Property 3: stored profile length never exceeds max_chars."""
    store = SqliteKVStore(bot_id="bot1", db_path=":memory:")
    async with store:
        profiles = ProfileStore(store)
        await profiles.save("qq", p)
        assert len(await profiles.load("qq")) <= PROFILE_MAX_CHARS
