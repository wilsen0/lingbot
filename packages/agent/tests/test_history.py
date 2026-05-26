"""Tests for :class:`KVHistoryStore` and history-aware ``AgentChatDispatcher``."""

from __future__ import annotations

import asyncio
import json

import pytest
from linling_agent.agent_def import AgentDef
from linling_agent.context import (
    ContextBudget,
    estimate_messages_tokens,
    estimate_tokens,
    fit_messages_to_budget,
)
from linling_agent.dispatcher import AgentChatDispatcher
from linling_agent.history import KVHistoryStore
from linling_agent.llm import LLMResponse, Message, TokenUsage, ToolCall
from linling_agent.runtime import AgentRuntime
from linling_core.events import Event, Scope, User
from linling_core.pipeline import ConversationKey, ConversationStore
from linling_core.segments import TextSegment
from linling_core.storage.sqlite_kv import SqliteKVStore
from linling_core.tools import registry


class _EchoProvider:
    """Provider that echoes the last user message plus a turn counter."""

    def __init__(self) -> None:
        self._n = 0

    @property
    def name(self) -> str:
        return "echo"

    async def chat(self, messages, *, tools=None, temperature=0.7, max_tokens=None):
        self._n += 1
        user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        hist_user = sum(1 for m in messages if m.role == "user")
        return LLMResponse(
            message=Message(role="assistant", content=f"[turn {hist_user}] {user}"),
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    async def chat_stream(self, messages, **kwargs):
        raise NotImplementedError


class _RecordingProvider:
    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    @property
    def name(self) -> str:
        return "recording"

    async def chat(self, messages, *, tools=None, temperature=0.7, max_tokens=None):
        self.calls.append(list(messages))
        # Summary calls are plain one-message prompts from ContextManager.
        if len(messages) == 1 and messages[0].content.startswith("Summarize"):
            return LLMResponse(
                message=Message(role="assistant", content="compressed old facts"),
                usage=TokenUsage(total_tokens=3),
            )
        user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        return LLMResponse(
            message=Message(role="assistant", content=f"reply:{user[:20]}"),
            usage=TokenUsage(total_tokens=3),
        )

    async def chat_stream(self, messages, **kwargs):
        raise NotImplementedError


def _event(text: str, *, sender: str = "u1", scope: str = "s1") -> Event:
    return Event(
        id=f"e-{text}-{sender}",
        platform="test",
        bot_id="bot1",
        scope=Scope(kind="group", id=scope, platform="test"),
        sender=User(id=sender, platform="test", display_name=sender),
        segments=[TextSegment(text=text)],
    )


# ---------------------------------------------------------------------------
# KVHistoryStore
# ---------------------------------------------------------------------------


@pytest.fixture
async def kv():
    store = SqliteKVStore(bot_id="bot1", db_path=":memory:")
    async with store:
        yield store


@pytest.fixture
def history(kv):
    return KVHistoryStore(kv, max_turns=8)


async def test_load_empty_returns_empty_list(history):
    assert await history.load("s1", "u1") == []


async def test_save_then_load_roundtrip(history):
    msgs = [
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello"),
    ]
    await history.save("s1", "u1", msgs)
    loaded = await history.load("s1", "u1")
    assert [(m.role, m.content) for m in loaded] == [
        ("user", "hi"),
        ("assistant", "hello"),
    ]


async def test_save_strips_non_turn_roles(history, kv):
    msgs = [
        Message(role="system", content="persona"),
        Message(role="user", content="hi"),
        Message(role="tool", content="tool result", name="foo", tool_call_id="1"),
        Message(role="assistant", content="hello"),
    ]
    await history.save("s1", "u1", msgs)
    raw = await kv.read("__history__/s1", "u1", "messages")
    data = json.loads(raw)
    assert [item["role"] for item in data] == ["user", "assistant"]


async def test_save_load_preserves_tool_call_blocks(history, kv):
    msgs = [
        Message(role="user", content="群聊历史消息：m1"),
        Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(
                    id="tc1",
                    name="reply_to_message",
                    arguments='{"message_id":"m1","text":"可以"}',
                )
            ],
            reasoning_content="thought",
        ),
        Message(
            role="tool",
            content="回复完成",
            name="reply_to_message",
            tool_call_id="tc1",
        ),
    ]

    await history.save("s1", "u1", msgs)
    raw = await kv.read("__history__/s1", "u1", "messages")
    data = json.loads(raw)
    assert [item["role"] for item in data] == ["user", "assistant", "tool"]
    assert data[1]["tool_calls"][0]["name"] == "reply_to_message"

    loaded = await history.load("s1", "u1")
    assert [message.role for message in loaded] == ["user", "assistant", "tool"]
    assert loaded[1].tool_calls
    assert loaded[1].tool_calls[0].arguments == '{"message_id":"m1","text":"可以"}'
    assert loaded[1].reasoning_content == "thought"
    assert loaded[2].tool_call_id == "tc1"


async def test_trims_to_max_turns(kv):
    history = KVHistoryStore(kv, max_turns=2)  # keeps 4 messages
    msgs = [
        message
        for i in range(5)
        for message in (
            Message(role="user", content=f"u{i}"),
            Message(role="assistant", content=f"a{i}"),
        )
    ]
    await history.save("s1", "u1", msgs)
    loaded = await history.load("s1", "u1")
    # Trimmed to last 4 entries.
    assert len(loaded) == 4


async def test_load_handles_corrupted_json(history, kv):
    await kv.write("__history__/s1", "u1", "messages", "not json {{")
    assert await history.load("s1", "u1") == []


async def test_scope_wide_memory_when_sender_empty(history, kv):
    """Empty sender_id stores under the shared ``_group`` bucket."""
    msgs = [Message(role="user", content="hi"), Message(role="assistant", content="ho")]
    await history.save("s1", "", msgs)
    raw = await kv.read("__history__/s1", "_group", "messages")
    assert raw is not None
    loaded = await history.load("s1", "")
    assert len(loaded) == 2


async def test_clear(history):
    await history.save("s1", "u1", [Message(role="user", content="hi")])
    await history.save_summary("s1", "u1", "old summary")
    assert await history.load("s1", "u1") != []
    assert await history.load_summary("s1", "u1") == "old summary"
    await history.clear("s1", "u1")
    assert await history.load("s1", "u1") == []
    assert await history.load_summary("s1", "u1") == ""


async def test_summary_roundtrip(history):
    await history.save_summary("s1", "u1", "facts so far")
    assert await history.load_summary("s1", "u1") == "facts so far"
    await history.clear_summary("s1", "u1")
    assert await history.load_summary("s1", "u1") == ""


def test_token_estimator_is_byte_conservative() -> None:
    assert estimate_tokens("abc") == 3
    assert estimate_tokens("苏") == len("苏".encode())
    assert estimate_tokens("😀") == len("😀".encode())


def test_fit_messages_preserves_tool_blocks() -> None:
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="old"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c1", name="lookup", arguments='{"q":"x"}')],
        ),
        Message(role="tool", content="y" * 200, name="lookup", tool_call_id="c1"),
        Message(role="assistant", content="final"),
    ]

    fitted = fit_messages_to_budget(messages, 120)

    assert fitted[0].role == "system"
    assistant_tool = next(m for m in fitted if m.role == "assistant" and m.tool_calls)
    tool = next(m for m in fitted if m.role == "tool")
    assert assistant_tool.tool_calls
    assert tool.tool_call_id == assistant_tool.tool_calls[0].id
    assert len(tool.content) < 200
    assert estimate_messages_tokens(fitted) <= 120


def test_fit_messages_drops_orphan_tool_messages() -> None:
    messages = [
        Message(role="system", content="system"),
        Message(role="tool", content="orphan", name="lookup", tool_call_id="c1"),
        Message(role="assistant", content="final"),
    ]

    fitted = fit_messages_to_budget(messages, 80)

    assert [m.role for m in fitted] == ["system", "assistant"]


# ---------------------------------------------------------------------------
# Dispatcher + history integration
# ---------------------------------------------------------------------------


@pytest.fixture
def dispatcher(kv, history):
    agent_def = AgentDef(name="a", model="mock", system="")
    agent = AgentRuntime(
        agent_def=agent_def,
        provider=_EchoProvider(),
        tool_registry=registry,
        kv=kv,
        bot_id="bot1",
    )
    return AgentChatDispatcher(agent=agent, history_store=history)


async def test_dispatcher_persists_turns(dispatcher, history):
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "s1", "u1"))

    await dispatcher.run(_event("first"), session)
    await dispatcher.run(_event("second"), session)

    loaded = await history.load("s1", "u1")
    # 2 turns * 2 messages (user + assistant) = 4 stored messages.
    assert [m.role for m in loaded] == ["user", "assistant", "user", "assistant"]
    assert loaded[0].content == "first"
    assert loaded[2].content == "second"


async def test_dispatcher_rehydrates_from_store_on_fresh_session(dispatcher, history):
    """A brand-new Session (post-restart) picks up saved history."""
    store = ConversationStore(rate_per_second=100, burst=100)

    # Seed history as if a previous process had written it.
    await history.save(
        "s1",
        "u1",
        [
            Message(role="user", content="remember me"),
            Message(role="assistant", content="ok"),
        ],
    )

    session = await store.get_or_create(ConversationKey("bot1", "s1", "u1"))
    # No in-memory history yet.
    assert len(session.history) == 0

    await dispatcher.run(_event("now what?"), session)

    # The provider counts messages with role=user; after rehydration +
    # this turn, there should be 2 user messages (hence ``[turn 2]``).
    loaded = await history.load("s1", "u1")
    assert loaded[-1].content.startswith("[turn 2]")


async def test_dispatcher_summarizes_when_history_exceeds_budget(kv, history):
    provider = _RecordingProvider()
    agent_def = AgentDef(name="ctx", model="mock", system="")
    agent = AgentRuntime(
        agent_def=agent_def,
        provider=provider,
        tool_registry=registry,
        kv=kv,
        bot_id="bot1",
    )
    dispatcher = AgentChatDispatcher(
        agent=agent,
        history_store=history,
        context_budget=ContextBudget(
            max_tokens=200,
            summary_trigger_tokens=80,
            summary_keep_recent_turns=1,
            summary_max_tokens=50,
        ),
    )
    store = ConversationStore(rate_per_second=100, burst=100, history_turns=20)
    session = await store.get_or_create(ConversationKey("bot1", "s1", "u1"))
    for i in range(6):
        session.history.append(Message(role="user", content=f"old user {i} " + "很长" * 20))
        session.history.append(Message(role="assistant", content=f"old assistant {i}"))

    await dispatcher.run(_event("now"), session)

    assert await history.load_summary("s1", "u1") == "compressed old facts"
    # One call for summary, one call for the actual reply.
    assert len(provider.calls) == 2
    actual_prompt = provider.calls[-1]
    assert any("<conversation_summary>" in m.content for m in actual_prompt)
    assert not any("old user 0" in m.content for m in actual_prompt)


async def test_dispatcher_clips_current_input_to_context_budget(kv, history):
    provider = _RecordingProvider()
    agent_def = AgentDef(name="ctx", model="mock", system="system prompt")
    agent = AgentRuntime(
        agent_def=agent_def,
        provider=provider,
        tool_registry=registry,
        kv=kv,
        bot_id="bot1",
    )
    dispatcher = AgentChatDispatcher(
        agent=agent,
        history_store=history,
        context_budget=ContextBudget(
            max_tokens=120,
            summary_trigger_tokens=1_000,
            summary_keep_recent_turns=1,
            summary_max_tokens=50,
        ),
    )
    store = ConversationStore(rate_per_second=100, burst=100, history_turns=20)
    session = await store.get_or_create(ConversationKey("bot1", "s1", "u1"))
    long_input = "x" * 1_000

    await dispatcher.run(_event(long_input), session)

    actual_prompt = provider.calls[-1]
    user_messages = [m.content for m in actual_prompt if m.role == "user"]
    assert user_messages
    assert len(user_messages[-1]) < len(long_input)
    assert "x" * len(user_messages[-1]) == user_messages[-1]


async def test_disabled_context_budget_does_not_clip_current_input(kv, history):
    provider = _RecordingProvider()
    agent_def = AgentDef(name="ctx", model="mock", system="")
    agent = AgentRuntime(
        agent_def=agent_def,
        provider=provider,
        tool_registry=registry,
        kv=kv,
        bot_id="bot1",
    )
    dispatcher = AgentChatDispatcher(
        agent=agent,
        history_store=history,
        context_budget=ContextBudget(
            max_tokens=0,
            summary_trigger_tokens=0,
            summary_keep_recent_turns=1,
            summary_max_tokens=50,
        ),
    )
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "s1", "u1"))
    long_input = "x" * 1_000

    await dispatcher.run(_event(long_input), session)

    actual_prompt = provider.calls[-1]
    user_messages = [m.content for m in actual_prompt if m.role == "user"]
    assert user_messages[-1] == long_input


async def test_context_budget_clips_even_when_summary_disabled(kv, history):
    provider = _RecordingProvider()
    agent_def = AgentDef(name="ctx", model="mock", system="")
    agent = AgentRuntime(
        agent_def=agent_def,
        provider=provider,
        tool_registry=registry,
        kv=kv,
        bot_id="bot1",
    )
    dispatcher = AgentChatDispatcher(
        agent=agent,
        history_store=history,
        context_budget=ContextBudget(
            max_tokens=120,
            summary_trigger_tokens=0,
            summary_keep_recent_turns=1,
            summary_max_tokens=50,
        ),
    )
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "s1", "u1"))
    long_input = "x" * 1_000

    await dispatcher.run(_event(long_input), session)

    actual_prompt = provider.calls[-1]
    user_messages = [m.content for m in actual_prompt if m.role == "user"]
    assert len(user_messages[-1]) < len(long_input)


async def test_clear_history_drops_store_rows(dispatcher, history):
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "s1", "u1"))
    await dispatcher.run(_event("first"), session)

    assert await history.load("s1", "u1") != []
    await dispatcher.clear_history("s1", "u1")
    assert await history.load("s1", "u1") == []


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class _SlowProvider:
    """Provider that blocks until an external event is set.

    Useful for simulating a long-running LLM call so we can prove the
    dispatcher honours ``session.cancel_event``.
    """

    def __init__(self, release: asyncio.Event) -> None:
        self._release = release
        self.was_cancelled = False

    @property
    def name(self) -> str:
        return "slow"

    async def chat(self, messages, *, tools=None, temperature=0.7, max_tokens=None):
        try:
            await self._release.wait()
        except asyncio.CancelledError:
            self.was_cancelled = True
            raise
        return LLMResponse(
            message=Message(role="assistant", content="(slow reply)"),
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    async def chat_stream(self, messages, **kwargs):
        raise NotImplementedError


async def test_cancel_event_interrupts_in_flight_chat(kv, history):
    """Setting ``session.cancel_event`` mid-flight aborts the LLM call."""
    release = asyncio.Event()
    provider = _SlowProvider(release)
    agent_def = AgentDef(name="slow", model="mock", system="")
    agent = AgentRuntime(
        agent_def=agent_def,
        provider=provider,
        tool_registry=registry,
        kv=kv,
        bot_id="bot1",
    )
    dispatcher = AgentChatDispatcher(agent=agent, history_store=history)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "s1", "u1"))

    run_task = asyncio.create_task(dispatcher.run(_event("hi"), session))
    # Let the dispatcher reach ``asyncio.wait`` on the provider.
    await asyncio.sleep(0.05)

    # Fire the cancel signal (this is what ``/cancel`` does).
    session.cancel_event.set()

    result = await asyncio.wait_for(run_task, timeout=1.0)
    # Empty action list — nothing was sent to the user.
    assert result == []
    # The LLM call itself was cancelled.
    assert provider.was_cancelled

    # History is untouched — we don't remember half-turns.
    assert await history.load("s1", "u1") == []


async def test_cancel_event_is_cleared_before_next_turn(kv, history):
    """A cancel flag from a previous turn must not leak into the next one."""
    release = asyncio.Event()
    release.set()  # provider returns immediately
    provider = _SlowProvider(release)
    agent_def = AgentDef(name="fast", model="mock", system="")
    agent = AgentRuntime(
        agent_def=agent_def,
        provider=provider,
        tool_registry=registry,
        kv=kv,
        bot_id="bot1",
    )
    dispatcher = AgentChatDispatcher(agent=agent, history_store=history)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "s1", "u1"))
    # Pre-set the cancel flag as if a prior ``/cancel`` had fired.
    session.cancel_event.set()

    # The dispatcher should clear the flag at the top of ``run`` and
    # deliver the reply as usual.
    result = await dispatcher.run(_event("hello"), session)
    assert result
    assert "slow reply" in result[0].segments[0].text
