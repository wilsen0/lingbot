"""End-to-end integration tests for the DSL Action Ledger feature.

Covers tasks:

* 5.3 — DSL writer-on-error-then-raise behaviour through the
  dispatcher.
* 8.6 — concurrent rehydrate, cancel-doesn't-pollute, history-stays-
  clean.
* 17.1 — DSL → chat → ledger full pipeline with both KV stores wired.
* 17.2 — ``/cancel`` does not write to the ledger (in-memory or KV).
* 17.3 — covered indirectly:these tests exercise ``Router._do_reset``
  through ``test_router.py::test_builtin_reset_*`` plus
  ``test_router_ledger_reset.py``.
"""

from __future__ import annotations

import asyncio
from collections import deque

import pytest
from linling_agent.agent_def import AgentDef
from linling_agent.dispatcher import AgentChatDispatcher
from linling_agent.history import KVHistoryStore
from linling_agent.ledger import LedgerRenderer
from linling_agent.ledger_store import KVDslLedgerStore
from linling_agent.llm import LLMResponse, Message, TokenUsage
from linling_agent.runtime import AgentRuntime
from linling_core.events import Event, Scope, User
from linling_core.pipeline import ConversationKey, ConversationStore, DslEvent
from linling_core.segments import TextSegment
from linling_core.storage.sqlite_kv import SqliteKVStore
from linling_core.tools import registry

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _CapturingProvider:
    """Records every ``messages`` array the dispatcher sends to the LLM."""

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    @property
    def name(self) -> str:
        return "capture"

    async def chat(self, messages, *, tools=None, temperature=0.7, max_tokens=None):
        # Snapshot the input so later calls can't mutate what we observed.
        self.calls.append(list(messages))
        return LLMResponse(
            message=Message(role="assistant", content="ok"),
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    async def chat_stream(self, messages, **kwargs):
        raise NotImplementedError


def _event(text: str, *, sender: str = "u1", scope: str = "s1", scope_kind: str = "group") -> Event:
    return Event(
        id=f"e-{text}-{sender}",
        platform="test",
        bot_id="bot1",
        scope=Scope(kind=scope_kind, id=scope, platform="test"),
        sender=User(id=sender, platform="test", display_name=sender),
        segments=[TextSegment(text=text)],
    )


def _dsl_event(occurred_at: float, summary: str = "did the thing") -> DslEvent:
    return DslEvent(
        timestamp="00:00:00",
        trigger="签到",
        args=(),
        summary=summary,
        outcome="ok",
        mode="with_result",
        actor_id="u1",
        occurred_at=occurred_at,
    )


def _build_runtime(provider: _CapturingProvider, kv: SqliteKVStore) -> AgentRuntime:
    agent_def = AgentDef(name="test", model="m", system="hi")
    return AgentRuntime(
        agent_def=agent_def,
        provider=provider,
        tool_registry=registry,
        kv=kv,
        bot_id="bot1",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def kv():
    store = SqliteKVStore(bot_id="bot1", db_path=":memory:")
    async with store:
        yield store


# ---------------------------------------------------------------------------
# 17.1 — DSL → chat → ledger full pipeline
# ---------------------------------------------------------------------------


async def test_full_pipeline_dsl_then_chat_sees_ledger(kv) -> None:
    """A DSL event accumulated in ``session.dsl_events`` lands in the LLM
    input as a transient ``<recent_user_actions>`` system message,
    *without* entering ``Session.history`` or ``KVHistoryStore``."""
    provider = _CapturingProvider()
    runtime = _build_runtime(provider, kv)
    history = KVHistoryStore(kv, max_turns=8)
    ledger_store = KVDslLedgerStore(kv)
    renderer = LedgerRenderer(total_char_budget=800)
    dispatcher = AgentChatDispatcher(
        agent=runtime,
        history_store=history,
        ledger_store=ledger_store,
        ledger_renderer=renderer,
    )

    store = ConversationStore(rate_per_second=100, burst=100, ledger_maxlen=20)
    session = await store.get_or_create(ConversationKey("bot1", "s1", "u1"))

    # Simulate a previous DSL turn:populate the deque directly,
    # bypassing the writer (the writer's behaviour is covered by
    # test_ledger_writer.py).
    session.dsl_events.append(_dsl_event(occurred_at=1.0, summary="灵玉+10"))

    event = _event("我刚才做了什么", scope_kind="group")
    result = await dispatcher.dispatch(event, session)

    assert result is not None
    # The LLM saw exactly one call.
    assert len(provider.calls) == 1
    sent = provider.calls[0]
    # Ledger system message is in the input. There may be other system
    # messages (the agent's own ``system`` prompt) — filter on content.
    ledger_msgs = [
        m
        for m in sent
        if m.role == "system" and m.content.startswith("<recent_user_actions>")
    ]
    assert len(ledger_msgs) == 1
    # In group scope, ``by="u1"`` should appear.
    assert 'by="u1"' in ledger_msgs[0].content

    # ``Session.history`` only contains user/assistant turns.
    assert all(m.role in ("user", "assistant") for m in session.history)
    assert len(session.history) == 2

    # ``KVHistoryStore`` likewise stores turns only.
    persisted = await history.load("s1", "u1")
    assert all(m.role in ("user", "assistant") for m in persisted)


async def test_dm_scope_omits_actor_attribute(kv) -> None:
    """DM scope keeps ``include_actor=False`` so no ``by="..."`` leaks."""
    provider = _CapturingProvider()
    runtime = _build_runtime(provider, kv)
    renderer = LedgerRenderer(total_char_budget=800)
    dispatcher = AgentChatDispatcher(agent=runtime, ledger_renderer=renderer)

    store = ConversationStore(rate_per_second=100, burst=100, ledger_maxlen=20)
    session = await store.get_or_create(ConversationKey("bot1", "s1", "u1"))
    session.dsl_events.append(_dsl_event(occurred_at=1.0))

    await dispatcher.dispatch(_event("hi", scope_kind="dm"), session)

    sent = provider.calls[0]
    ledger_msgs = [
        m
        for m in sent
        if m.role == "system" and m.content.startswith("<recent_user_actions>")
    ]
    assert len(ledger_msgs) == 1
    assert "by=" not in ledger_msgs[0].content


# ---------------------------------------------------------------------------
# 8.6 — Rehydrate concurrency, cancel doesn't pollute, history clean
# ---------------------------------------------------------------------------


async def test_rehydrate_concurrent_history_and_ledger(kv) -> None:
    """Both stores are loaded; ledger restoration appears in the LLM input."""
    import time

    history = KVHistoryStore(kv, max_turns=8)
    ledger_store = KVDslLedgerStore(kv)

    # Pre-seed both surfaces from a "previous process". ``occurred_at``
    # is anchored to ``time.time()`` so the load-time TTL filter
    # keeps the event live.
    await history.save(
        "s1",
        "u1",
        [Message(role="user", content="prev"), Message(role="assistant", content="ack")],
    )
    await ledger_store.save("s1", "_group", [_dsl_event(occurred_at=time.time())])

    provider = _CapturingProvider()
    runtime = _build_runtime(provider, kv)
    renderer = LedgerRenderer()
    dispatcher = AgentChatDispatcher(
        agent=runtime,
        history_store=history,
        ledger_store=ledger_store,
        ledger_renderer=renderer,
    )

    store = ConversationStore(rate_per_second=100, burst=100, ledger_maxlen=20)
    session = await store.get_or_create(ConversationKey("bot1", "s1", "u1"))

    await dispatcher.dispatch(_event("again", scope_kind="group"), session)

    # Both surfaces hydrated.
    assert getattr(session, "_linling_history_hydrated", False)
    assert getattr(session, "_linling_ledger_hydrated", False)

    # The LLM saw the prior history *and* the rendered ledger system message.
    sent = provider.calls[0]
    user_msgs = [m for m in sent if m.role == "user"]
    ledger_msgs = [
        m
        for m in sent
        if m.role == "system" and m.content.startswith("<recent_user_actions>")
    ]
    assert any(m.content == "prev" for m in user_msgs)
    assert ledger_msgs and ledger_msgs[0].content.startswith("<recent_user_actions>")


async def test_rehydrate_ledger_load_failure_falls_back_empty(kv) -> None:
    """A KV failure during ledger rehydrate must not block the chat path."""

    class _ExplodingStore:
        async def save(self, *args, **kw):
            return None

        async def load(self, *args, **kw):
            raise RuntimeError("kv on fire")

        async def clear(self, *args, **kw):
            return None

    provider = _CapturingProvider()
    runtime = _build_runtime(provider, kv)
    history = KVHistoryStore(kv, max_turns=8)
    dispatcher = AgentChatDispatcher(
        agent=runtime,
        history_store=history,
        ledger_store=_ExplodingStore(),
        ledger_renderer=LedgerRenderer(),
    )

    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "s1", "u1"))

    result = await dispatcher.dispatch(_event("hi"), session)

    # Dispatch completed normally even though ledger.load raised.
    assert result is not None
    # The hydrated flag is still set so we don't retry on every turn.
    assert getattr(session, "_linling_ledger_hydrated", False)
    # No ledger system message was injected (nothing in the deque).
    sent = provider.calls[0]
    assert all(
        not (m.role == "system" and m.content.startswith("<recent_user_actions>"))
        for m in sent
    )


async def test_chat_history_unchanged_when_ledger_msg_injected(kv) -> None:
    """Persisted history stays clean even with ledger injection enabled."""
    provider = _CapturingProvider()
    runtime = _build_runtime(provider, kv)
    history = KVHistoryStore(kv, max_turns=8)
    dispatcher = AgentChatDispatcher(
        agent=runtime,
        history_store=history,
        ledger_renderer=LedgerRenderer(),
    )
    store = ConversationStore(rate_per_second=100, burst=100, ledger_maxlen=20)
    session = await store.get_or_create(ConversationKey("bot1", "s1", "u1"))
    session.dsl_events.append(_dsl_event(occurred_at=1.0))

    await dispatcher.dispatch(_event("hi", scope_kind="group"), session)

    persisted = await history.load("s1", "u1")
    # The persisted view never includes role="system" entries.
    assert all(m.role in ("user", "assistant") for m in persisted)
    # Sanity:there really is a user/assistant pair from this turn.
    assert any(m.role == "user" and m.content == "hi" for m in persisted)


async def test_cancelled_turn_does_not_persist_ledger(kv) -> None:
    """A cancel mid-dispatch leaves both stores untouched on the ledger side."""
    import time

    class _SlowProvider:
        @property
        def name(self) -> str:
            return "slow"

        async def chat(self, messages, **kwargs):
            await asyncio.sleep(2.0)  # never expected to complete in test
            raise AssertionError("should have been cancelled")

        async def chat_stream(self, messages, **kwargs):
            raise NotImplementedError

    runtime = _build_runtime(_SlowProvider(), kv)  # type: ignore[arg-type]
    ledger_store = KVDslLedgerStore(kv)
    dispatcher = AgentChatDispatcher(
        agent=runtime,
        ledger_store=ledger_store,
        ledger_renderer=LedgerRenderer(),
    )

    store = ConversationStore(rate_per_second=100, burst=100, ledger_maxlen=20)
    session = await store.get_or_create(ConversationKey("bot1", "s1", "u1"))

    # Pre-populate; ledger writes from cancellation must not happen
    # but pre-existing state must survive. ``occurred_at`` is anchored
    # to ``time.time()`` so the TTL filter keeps the seed event live.
    now = time.time()
    seed = _dsl_event(occurred_at=now)
    session.dsl_events.append(seed)
    await ledger_store.save("s1", "_group", [seed])

    async def _dispatch() -> None:
        return await dispatcher.dispatch(_event("hi", scope_kind="group"), session)

    task = asyncio.create_task(_dispatch())
    # Give the dispatcher time to enter the LLM call.
    await asyncio.sleep(0.05)
    session.cancel_event.set()
    result = await asyncio.wait_for(task, timeout=2.0)
    assert result is None  # cancel path returns None

    # In-memory ledger unchanged.
    assert len(session.dsl_events) == 1
    # Persisted ledger unchanged.
    loaded = await ledger_store.load("s1", "_group")
    assert len(loaded) == 1


async def test_ledger_msg_position_after_history_before_user(kv) -> None:
    """The ledger system message sits after history and is consumed by the
    LLM call alongside the user input."""
    provider = _CapturingProvider()
    runtime = _build_runtime(provider, kv)
    history = KVHistoryStore(kv, max_turns=8)
    await history.save(
        "s1",
        "u1",
        [Message(role="user", content="earlier"), Message(role="assistant", content="reply")],
    )
    dispatcher = AgentChatDispatcher(
        agent=runtime,
        history_store=history,
        ledger_renderer=LedgerRenderer(),
    )
    store = ConversationStore(rate_per_second=100, burst=100, ledger_maxlen=20)
    session = await store.get_or_create(ConversationKey("bot1", "s1", "u1"))
    session.dsl_events.append(_dsl_event(occurred_at=1.0))

    await dispatcher.dispatch(_event("now what", scope_kind="group"), session)

    sent = provider.calls[0]
    # Find the rendered ledger message and the existing history.
    system_idx = next(
        i
        for i, m in enumerate(sent)
        if m.role == "system" and m.content.startswith("<recent_user_actions>")
    )
    history_user_idx = next(i for i, m in enumerate(sent) if m.role == "user" and m.content == "earlier")
    # Ledger sits after the prior user/assistant turn.
    assert system_idx > history_user_idx


# ---------------------------------------------------------------------------
# 5.3 — DSL writer-on-error-then-raise integration
# ---------------------------------------------------------------------------


async def test_dsl_dispatcher_propagates_vm_exception_to_router_safe(kv) -> None:
    """A VM exception is appended as ``outcome="error"`` and then re-raised."""
    from linling_core.classifier import HandlerMatch
    from linling_dsl.ast_nodes import (
        Assign,
        Handler,
        Jump,
        Label,
        Literal,
    )
    from linling_dsl.dispatcher import DslCommandDispatcher
    from linling_dsl.ledger import LedgerWriter
    from linling_dsl.vm import SandboxError

    # Hand-built handler that loops forever; ``max_steps=5`` triggers
    # SandboxError mid-execution. Mirrors ``test_sandbox_max_steps``
    # in the dsl test suite.
    handler = Handler(
        trigger="ping",
        is_internal=False,
        body=[
            Label(name="loop", line=2),
            Assign(name="x", value=Literal(value="1"), line=3),
            Jump(target="loop", line=4),
        ],
        line=1,
    )

    writer = LedgerWriter()
    dispatcher = DslCommandDispatcher(
        registry=registry,
        kv=kv,
        bot_id="bot1",
        max_steps=5,
        ledger_writer=writer,
    )

    session_store = ConversationStore(rate_per_second=100, burst=100, ledger_maxlen=20)
    session = await session_store.get_or_create(ConversationKey("bot1", "s1", "u1"))
    event = _event("ping", scope_kind="group")
    match = HandlerMatch(handler=handler, captures=[])

    with pytest.raises(SandboxError):
        await dispatcher.run(event, match, session)

    # The error event was appended *before* the raise, so it shows up
    # in the deque for debug surfaces (Requirement 2.5 / 2.6).
    assert len(session.dsl_events) == 1
    assert session.dsl_events[0].outcome == "error"
    assert session.dsl_events[0].summary == ""


async def test_internal_debug_can_read_error_events_from_dsl_events(kv) -> None:
    """Error events live alongside ok events for ops/audit surfaces."""
    from linling_dsl.ast_nodes import Handler
    from linling_dsl.ledger import LedgerWriter

    writer = LedgerWriter()
    session_store = ConversationStore(rate_per_second=100, burst=100, ledger_maxlen=20)
    session = await session_store.get_or_create(ConversationKey("bot1", "s1", "u1"))

    handler = Handler(trigger="t", is_internal=False, body=[], line=1)
    event = _event("ping")

    # Write ok then error; both must remain visible on the in-memory
    # deque (renderer filters errors out, but debug paths see them).
    writer.append(
        session=session,
        handler=handler,
        captures=[],
        raw_summary="ok",
        outcome="ok",
        event=event,
    )
    writer.append(
        session=session,
        handler=handler,
        captures=[],
        raw_summary="",
        outcome="error",
        event=event,
    )
    assert [e.outcome for e in session.dsl_events] == ["ok", "error"]


async def test_ledger_path_does_not_call_audit_sink(kv) -> None:
    """``LedgerWriter`` has no audit-sink dependency — no parameter, no import."""
    import inspect

    from linling_dsl.ledger import LedgerWriter

    src = inspect.getsource(LedgerWriter)
    # The writer's source must not reference any audit primitive.
    assert "AuditSink" not in src
    assert "audit_sink" not in src
    assert "AuditEntry" not in src
