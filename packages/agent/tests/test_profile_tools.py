"""Tests for read_user_profile / write_user_profile tools (Phase 2).

The tools live in ``linling_agent.profile`` (single source of truth shared by
the DM ReAct loop, the group-batch loop, and the pre-compaction updater).
Importing that module registers them into the global registry.
"""

from __future__ import annotations

import linling_agent.profile  # noqa: F401  (registers the tools)
from linling_core.storage.sqlite_kv import SqliteKVStore
from linling_core.tools import ToolCtx, registry


async def test_write_then_read_roundtrip() -> None:
    async with SqliteKVStore("bot1", ":memory:") as kv:
        ctx = ToolCtx(kv=kv, event=None, bot_id="bot1")
        write = registry.get("write_user_profile")
        read = registry.get("read_user_profile")
        assert write is not None and read is not None

        out = await write.fn(ctx, qq="123", profile="喜欢钓鱼", name="小红")
        assert "Updated" in out

        got = await read.fn(ctx, qq="123")
        assert "喜欢钓鱼" in got
        assert "小红" in got


async def test_read_missing_returns_placeholder() -> None:
    async with SqliteKVStore("bot1", ":memory:") as kv:
        ctx = ToolCtx(kv=kv, event=None, bot_id="bot1")
        read = registry.get("read_user_profile")
        assert read is not None
        got = await read.fn(ctx, qq="999")
        assert "No profile found" in got


async def test_write_is_full_rewrite() -> None:
    async with SqliteKVStore("bot1", ":memory:") as kv:
        ctx = ToolCtx(kv=kv, event=None, bot_id="bot1")
        write = registry.get("write_user_profile")
        read = registry.get("read_user_profile")
        assert write is not None and read is not None

        await write.fn(ctx, qq="123", profile="第一版")
        await write.fn(ctx, qq="123", profile="第二版完全不同")
        got = await read.fn(ctx, qq="123")
        assert "第二版完全不同" in got
        assert "第一版" not in got


async def test_write_clamps_long_profile() -> None:
    async with SqliteKVStore("bot1", ":memory:") as kv:
        ctx = ToolCtx(kv=kv, event=None, bot_id="bot1")
        write = registry.get("write_user_profile")
        assert write is not None
        long_text = "字" * 500
        await write.fn(ctx, qq="123", profile=long_text)
        stored = await kv.read("__profile__", "123", "profile")
        assert stored is not None
        assert len(stored) == 400


async def test_empty_qq_returns_error() -> None:
    async with SqliteKVStore("bot1", ":memory:") as kv:
        ctx = ToolCtx(kv=kv, event=None, bot_id="bot1")
        write = registry.get("write_user_profile")
        read = registry.get("read_user_profile")
        assert write is not None and read is not None
        assert "Error" in await write.fn(ctx, qq="", profile="x")
        assert "Error" in await read.fn(ctx, qq="")


async def test_tools_swallow_kv_exceptions() -> None:
    class _BrokenKV:
        async def read(self, *a, **k):
            raise RuntimeError("kv down")

        async def write(self, *a, **k):
            raise RuntimeError("kv down")

    ctx = ToolCtx(kv=_BrokenKV(), event=None, bot_id="bot1")  # type: ignore[arg-type]
    write = registry.get("write_user_profile")
    read = registry.get("read_user_profile")
    assert write is not None and read is not None
    # Neither should raise; both return an error string.
    assert "Error" in await write.fn(ctx, qq="123", profile="x")
    assert "Error" in await read.fn(ctx, qq="123")


async def test_llm_visible_and_not_dsl() -> None:
    write = registry.get("write_user_profile")
    read = registry.get("read_user_profile")
    assert write is not None and read is not None
    assert write.llm_visible is True
    assert read.llm_visible is True
    assert write.dsl_name == ""
    assert read.dsl_name == ""
    # Not registered under any DSL name.
    assert registry.get_by_dsl_name("read_user_profile") is None
