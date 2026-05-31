"""Tests for ProfileUpdater pre-compaction distillation loop (Phase 4)."""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from linling_agent.llm import LLMResponse, Message, TokenUsage, ToolCall
from linling_agent.profile import ProfileStore, ProfileUpdater
from linling_core.storage.sqlite_kv import SqliteKVStore
from linling_core.tools import registry


class _ScriptedProvider:
    """Emits a fixed sequence of assistant turns (tool_calls or final text)."""

    def __init__(self, script: list[Message]) -> None:
        self._script = script
        self.call_count = 0

    @property
    def name(self) -> str:
        return "scripted"

    async def chat(self, messages, *, tools=None, temperature=0.7, max_tokens=None):
        idx = min(self.call_count, len(self._script) - 1)
        msg = self._script[idx]
        self.call_count += 1
        return LLMResponse(message=msg, usage=TokenUsage(total_tokens=1))

    async def chat_stream(self, messages, **kwargs):
        raise NotImplementedError


class _AlwaysToolProvider:
    """Never stops calling a tool — exercises the round cap."""

    def __init__(self) -> None:
        self.call_count = 0

    @property
    def name(self) -> str:
        return "always"

    async def chat(self, messages, *, tools=None, temperature=0.7, max_tokens=None):
        self.call_count += 1
        return LLMResponse(
            message=Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(
                        id=f"c{self.call_count}",
                        name="read_user_profile",
                        arguments='{"qq": "123"}',
                    )
                ],
            ),
            usage=TokenUsage(total_tokens=1),
        )

    async def chat_stream(self, messages, **kwargs):
        raise NotImplementedError


def _older() -> list[Message]:
    return [
        Message(role="user", content="123: 我叫小明，喜欢钓鱼"),
        Message(role="assistant", content="记住啦"),
    ]


def _tc(cid: str, name: str, args: str) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=args)


def _updater(provider, kv):
    return ProfileUpdater(
        provider=provider,
        kv=kv,
        registry=registry,
    )


async def test_multi_user_distillation() -> None:
    async with SqliteKVStore("bot1", ":memory:") as kv:
        script = [
            Message(role="assistant", content="", tool_calls=[_tc("1", "read_user_profile", '{"qq":"123"}')]),
            Message(role="assistant", content="", tool_calls=[_tc("2", "write_user_profile", '{"qq":"123","profile":"小明，喜欢钓鱼"}')]),
            Message(role="assistant", content="", tool_calls=[_tc("3", "read_user_profile", '{"qq":"456"}')]),
            Message(role="assistant", content="", tool_calls=[_tc("4", "write_user_profile", '{"qq":"456","profile":"小红"}')]),
            Message(role="assistant", content="好了"),
        ]
        provider = _ScriptedProvider(script)
        updater = _updater(provider, kv)
        await updater.run("s1", "", _older())

        store = ProfileStore(kv)
        assert await store.load("123") == "小明，喜欢钓鱼"
        assert await store.load("456") == "小红"


async def test_no_toolcall_terminates_immediately() -> None:
    async with SqliteKVStore("bot1", ":memory:") as kv:
        provider = _ScriptedProvider([Message(role="assistant", content="好了")])
        updater = _updater(provider, kv)
        await updater.run("s1", "u1", _older())
        assert provider.call_count == 1


async def test_empty_older_skips_provider() -> None:
    async with SqliteKVStore("bot1", ":memory:") as kv:
        provider = _ScriptedProvider([Message(role="assistant", content="好了")])
        updater = _updater(provider, kv)
        await updater.run("s1", "u1", [])
        assert provider.call_count == 0


async def test_round_cap_terminates() -> None:
    async with SqliteKVStore("bot1", ":memory:") as kv:
        provider = _AlwaysToolProvider()
        updater = ProfileUpdater(
            provider=provider,
            kv=kv,
            registry=registry,
            max_tool_rounds=3,
        )
        await updater.run("s1", "u1", _older())
        # Bounded: exactly max_tool_rounds provider calls, then stop.
        assert provider.call_count == 3


async def test_provider_exception_is_failopen() -> None:
    class _Boom:
        name = "boom"

        async def chat(self, *a, **k):
            raise RuntimeError("llm down")

        async def chat_stream(self, *a, **k):
            raise NotImplementedError

    async with SqliteKVStore("bot1", ":memory:") as kv:
        updater = _updater(_Boom(), kv)
        # Must not raise.
        await updater.run("s1", "u1", _older())


async def test_timeout_is_failopen() -> None:
    class _Hang:
        name = "hang"

        async def chat(self, *a, **k):
            await asyncio.sleep(10)
            raise AssertionError("should have timed out")

        async def chat_stream(self, *a, **k):
            raise NotImplementedError

    async with SqliteKVStore("bot1", ":memory:") as kv:
        updater = ProfileUpdater(
            provider=_Hang(),
            kv=kv,
            registry=registry,
            timeout_s=0.05,
        )
        await updater.run("s1", "u1", _older())  # must not raise


async def test_cancelled_propagates() -> None:
    class _Cancel:
        name = "cancel"

        async def chat(self, *a, **k):
            raise asyncio.CancelledError

        async def chat_stream(self, *a, **k):
            raise NotImplementedError

    async with SqliteKVStore("bot1", ":memory:") as kv:
        updater = _updater(_Cancel(), kv)
        with pytest.raises(asyncio.CancelledError):
            await updater.run("s1", "u1", _older())


# ---------------------------------------------------------------------------
# Property 8: bounded termination
# Feature: user-profile-memory, Property 8
# ---------------------------------------------------------------------------


@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(rounds=st.integers(min_value=1, max_value=10))
async def test_property_bounded_by_round_cap(rounds: int) -> None:
    """An always-calling provider is capped at max_tool_rounds chat calls."""
    async with SqliteKVStore("bot1", ":memory:") as kv:
        provider = _AlwaysToolProvider()
        updater = ProfileUpdater(
            provider=provider,
            kv=kv,
            registry=registry,
            max_tool_rounds=rounds,
        )
        await updater.run("s1", "u1", _older())
        assert provider.call_count == rounds
