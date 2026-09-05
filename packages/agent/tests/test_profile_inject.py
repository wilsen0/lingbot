"""Tests for DM-side <user_profile> injection in AgentChatDispatcher (Phase 3)."""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from linling_agent.agent_def import AgentDef
from linling_agent.dispatcher import AgentChatDispatcher
from linling_agent.llm import LLMResponse, Message, TokenUsage, ToolCall
from linling_agent.profile import ProfileStore
from linling_agent.runtime import AgentRuntime
from linling_core.events import Event, Scope, User
from linling_core.pipeline import ConversationKey, ConversationStore
from linling_core.segments import TextSegment
from linling_core.storage.sqlite_kv import SqliteKVStore
from linling_core.tools import registry


class _RecordingProvider:
    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    @property
    def name(self) -> str:
        return "recording"

    async def chat(self, messages, *, tools=None, temperature=0.7, max_tokens=None):
        self.calls.append(list(messages))
        return LLMResponse(
            message=Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(
                        id=f"ft_{len(self.calls)}",
                        name="finish_turn",
                        arguments='{"summary":"ok"}',
                    )
                ],
            ),
            usage=TokenUsage(total_tokens=3),
        )

    async def chat_stream(self, messages, **kwargs):
        raise NotImplementedError


@pytest.fixture
async def kv():
    store = SqliteKVStore(bot_id="bot1", db_path=":memory:")
    async with store:
        yield store


def _dm_event(text: str, *, sender: str = "u1", name: str = "小红") -> Event:
    return Event(
        id=f"e-{text}",
        platform="test",
        bot_id="bot1",
        scope=Scope(kind="dm", id=sender, platform="test"),
        sender=User(id=sender, platform="test", display_name=name),
        segments=[TextSegment(text=text)],
    )


def _group_event(text: str, *, sender: str = "u1", scope: str = "g1") -> Event:
    return Event(
        id=f"e-{text}",
        platform="test",
        bot_id="bot1",
        scope=Scope(kind="group", id=scope, platform="test"),
        sender=User(id=sender, platform="test", display_name="小红"),
        segments=[TextSegment(text=text)],
    )


def _build(kv, *, provider=None, profile_store_on=True):
    agent_def = AgentDef(name="a", model="mock", system="agent-sys")
    agent = AgentRuntime(
        agent_def=agent_def,
        provider=provider or _RecordingProvider(),
        tool_registry=registry,
        kv=kv,
        bot_id="bot1",
    )
    store = ProfileStore(kv) if profile_store_on else None
    return AgentChatDispatcher(agent=agent, profile_store=store)


def _profile_blocks(messages: list[Message]) -> list[Message]:
    return [m for m in messages if m.role == "system" and "<user_profile" in m.content]


async def _conv():
    return ConversationStore(rate_per_second=100, burst=100)


async def test_dm_injects_profile_when_present(kv) -> None:
    provider = _RecordingProvider()
    dispatcher = _build(kv, provider=provider)
    await ProfileStore(kv).save("u1", "喜欢钓鱼", name="小红")

    store = await _conv()
    session = await store.get_or_create(ConversationKey("bot1", "u1", "u1"))
    await dispatcher.run(_dm_event("hi"), session)

    blocks = _profile_blocks(provider.calls[-1])
    assert len(blocks) == 1
    assert "喜欢钓鱼" in blocks[0].content
    assert 'qq="u1"' in blocks[0].content


async def test_dm_empty_profile_not_injected(kv) -> None:
    provider = _RecordingProvider()
    dispatcher = _build(kv, provider=provider)

    store = await _conv()
    session = await store.get_or_create(ConversationKey("bot1", "u1", "u1"))
    await dispatcher.run(_dm_event("hi"), session)

    assert _profile_blocks(provider.calls[-1]) == []


async def test_group_scope_not_injected(kv) -> None:
    provider = _RecordingProvider()
    dispatcher = _build(kv, provider=provider)
    await ProfileStore(kv).save("u1", "喜欢钓鱼", name="小红")

    store = await _conv()
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))
    await dispatcher.run(_group_event("hi"), session)

    assert _profile_blocks(provider.calls[-1]) == []


async def test_group_batch_flag_not_injected(kv) -> None:
    provider = _RecordingProvider()
    dispatcher = _build(kv, provider=provider)
    await ProfileStore(kv).save("u1", "喜欢钓鱼", name="小红")

    ev = _dm_event("hi")
    ev = ev.model_copy(update={"raw": {"_linling_group_batch": True}})

    store = await _conv()
    session = await store.get_or_create(ConversationKey("bot1", "u1", "u1"))
    await dispatcher.run(ev, session)

    assert _profile_blocks(provider.calls[-1]) == []


async def test_no_profile_store_not_injected(kv) -> None:
    provider = _RecordingProvider()
    dispatcher = _build(kv, provider=provider, profile_store_on=False)
    await ProfileStore(kv).save("u1", "喜欢钓鱼", name="小红")

    store = await _conv()
    session = await store.get_or_create(ConversationKey("bot1", "u1", "u1"))
    await dispatcher.run(_dm_event("hi"), session)

    assert _profile_blocks(provider.calls[-1]) == []


async def test_dm_kv_failure_is_failopen(kv) -> None:
    """A KV read error must not block the reply; just skip injection."""

    class _PartlyBrokenStore(ProfileStore):
        async def load(self, qq: str) -> str:
            raise RuntimeError("kv down")

    provider = _RecordingProvider()
    agent_def = AgentDef(name="a", model="mock", system="agent-sys")
    agent = AgentRuntime(
        agent_def=agent_def, provider=provider, tool_registry=registry, kv=kv, bot_id="bot1"
    )
    dispatcher = AgentChatDispatcher(agent=agent, profile_store=_PartlyBrokenStore(kv))

    store = await _conv()
    session = await store.get_or_create(ConversationKey("bot1", "u1", "u1"))
    await dispatcher.run(_dm_event("hi"), session)

    assert _profile_blocks(provider.calls[-1]) == []
    assert provider.calls  # reply was still attempted


async def test_dm_touches_name(kv) -> None:
    provider = _RecordingProvider()
    dispatcher = _build(kv, provider=provider)

    store = await _conv()
    session = await store.get_or_create(ConversationKey("bot1", "u1", "u1"))
    await dispatcher.run(_dm_event("hi", name="新昵称"), session)

    assert await ProfileStore(kv).load_name("u1") == "新昵称"


# ---------------------------------------------------------------------------
# Property 4: injection iff (dm AND non-empty profile AND no group-batch flag)
# Feature: user-profile-memory, Property 4
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    kind=st.sampled_from(["dm", "group"]),
    has_profile=st.booleans(),
    batch_flag=st.booleans(),
)
async def test_property_injection_conditions(
    kind: str, has_profile: bool, batch_flag: bool
) -> None:
    store_kv = SqliteKVStore(bot_id="bot1", db_path=":memory:")
    async with store_kv:
        provider = _RecordingProvider()
        agent_def = AgentDef(name="a", model="mock", system="sys")
        agent = AgentRuntime(
            agent_def=agent_def,
            provider=provider,
            tool_registry=registry,
            kv=store_kv,
            bot_id="bot1",
        )
        dispatcher = AgentChatDispatcher(agent=agent, profile_store=ProfileStore(store_kv))
        if has_profile:
            await ProfileStore(store_kv).save("u1", "画像内容", name="小红")

        if kind == "dm":
            ev = _dm_event("hi")
        else:
            ev = _group_event("hi")
        if batch_flag:
            ev = ev.model_copy(update={"raw": {"_linling_group_batch": True}})

        conv = ConversationStore(rate_per_second=100, burst=100)
        session = await conv.get_or_create(ConversationKey("bot1", ev.scope.id, ev.sender.id))
        await dispatcher.run(ev, session)

        injected = bool(_profile_blocks(provider.calls[-1]))
        expected = kind == "dm" and has_profile and not batch_flag
        assert injected == expected
