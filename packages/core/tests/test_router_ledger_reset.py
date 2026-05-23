"""Tests for ``Router._do_reset`` ledger integration (Task 15.3).

The reset flow now clears chat history *and* the DSL action ledger
atomically inside ``session.lock``. This file exercises:

* The in-memory ``session.dsl_events`` is cleared in the same lock-held
  block as ``session.history``.
* ``LedgerReset.clear_ledger`` is invoked with ledger-style scope keys
  (``"_group"`` for group scope, sender id for DM).
* A failing ``clear_ledger`` is logged but does not break the reply
  text or block ``clear_history``.
* Dispatchers that don't implement :class:`LedgerReset` keep their
  legacy behaviour.

The DSL ledger primitives themselves are tested in
``packages/dsl/tests/test_ledger_writer.py`` and
``packages/agent/tests/test_kv_dsl_ledger_store.py``.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field

from linling_agent.llm import Message
from linling_core.classifier import HandlerMatch, MessageClassifier
from linling_core.events import Action, Event, Scope, User
from linling_core.pipeline import (
    ConversationKey,
    ConversationStore,
    DslEvent,
    Session,
)
from linling_core.router import Router, RouterConfig
from linling_core.segments import TextSegment


# ---------------------------------------------------------------------------
# Fakes (mirroring test_router.py to keep test_router.py untouched)
# ---------------------------------------------------------------------------


@dataclass
class _FakeHandler:
    trigger: str
    is_internal: bool = False


@dataclass
class _FakeScript:
    handlers: list[_FakeHandler] = field(default_factory=list)


def _event(text: str, *, sender: str = "u1", scope: str = "g1", scope_kind: str = "group") -> Event:
    return Event(
        id=f"e-{text}-{sender}",
        platform="t",
        bot_id="linling",
        scope=Scope(kind=scope_kind, id=scope, platform="t"),
        sender=User(id=sender, platform="t"),
        segments=[TextSegment(text=text)],
    )


def _dsl_event(occurred_at: float = 1.0) -> DslEvent:
    return DslEvent(
        timestamp="00:00:00",
        trigger="t",
        args=(),
        summary="s",
        outcome="ok",
        mode="with_result",
        actor_id="u1",
        occurred_at=occurred_at,
    )


class _FakeChatDispatcher:
    """Records what the router did to the dispatcher's reset surfaces."""

    def __init__(self, *, raise_on_clear_ledger: bool = False) -> None:
        self.history_clears: list[tuple[str, str]] = []
        self.ledger_clears: list[tuple[str, str]] = []
        self._raise_on_clear_ledger = raise_on_clear_ledger

    async def run(self, event: Event, session: Session) -> list[Action]:
        return []

    async def clear_history(self, scope_id: str, sender_id: str) -> None:
        self.history_clears.append((scope_id, sender_id))

    async def clear_ledger(self, scope_id: str, file_id: str) -> None:
        self.ledger_clears.append((scope_id, file_id))
        if self._raise_on_clear_ledger:
            raise RuntimeError("ledger clear blew up")


class _LegacyChatDispatcher:
    """Older dispatcher without ``clear_ledger`` — backward-compat baseline."""

    def __init__(self) -> None:
        self.history_clears: list[tuple[str, str]] = []

    async def run(self, event: Event, session: Session) -> list[Action]:
        return []

    async def clear_history(self, scope_id: str, sender_id: str) -> None:
        self.history_clears.append((scope_id, sender_id))


class _FakeCommandDispatcher:
    async def run(self, event: Event, match: HandlerMatch, session: Session) -> list[Action]:
        return []


def _build_router(*, chats, conversations: ConversationStore | None = None):
    classifier = MessageClassifier(_FakeScript())
    actions: list[Action] = []

    async def sink(a: Action) -> None:
        actions.append(a)

    router = Router(
        classifier=classifier,
        commands=_FakeCommandDispatcher(),
        chats=chats,
        sink=sink,
        conversations=conversations,
        config=RouterConfig(),
    )
    return router, actions


# ---------------------------------------------------------------------------
# 15.3 unit tests
# ---------------------------------------------------------------------------


async def test_reset_clears_ledger_under_session_lock_atomically() -> None:
    """Both ``history`` and ``dsl_events`` are cleared in the same lock."""
    chats = _FakeChatDispatcher()
    store = ConversationStore(rate_per_second=100, burst=100, ledger_maxlen=10)
    session = await store.get_or_create(ConversationKey("linling", "g1", "u1"))
    session.history.append(Message(role="user", content="x"))
    session.dsl_events.append(_dsl_event())

    router, actions = _build_router(chats=chats, conversations=store)
    await router.handle(_event("/reset"))

    assert len(session.history) == 0
    assert len(session.dsl_events) == 0
    assert "cleared" in actions[-1].segments[0].text.lower()


async def test_reset_calls_clear_ledger_with_correct_scope_key_for_group() -> None:
    """Group scope collapses ``file_id`` to ``"_group"``."""
    chats = _FakeChatDispatcher()
    store = ConversationStore(rate_per_second=100, burst=100, ledger_maxlen=10)
    session = await store.get_or_create(ConversationKey("linling", "g1", "u1"))
    session.dsl_events.append(_dsl_event())

    router, _ = _build_router(chats=chats, conversations=store)
    await router.handle(_event("/reset", scope_kind="group"))

    assert chats.ledger_clears == [("g1", "_group")]
    assert chats.history_clears == [("g1", "u1")]


async def test_reset_calls_clear_ledger_with_correct_scope_key_for_dm() -> None:
    """DM scope keeps the sender id as ``file_id``."""
    chats = _FakeChatDispatcher()
    store = ConversationStore(rate_per_second=100, burst=100, ledger_maxlen=10)
    session = await store.get_or_create(ConversationKey("linling", "s1", "alice"))
    session.dsl_events.append(_dsl_event())

    router, _ = _build_router(chats=chats, conversations=store)
    await router.handle(_event("/reset", sender="alice", scope="s1", scope_kind="dm"))

    assert chats.ledger_clears == [("s1", "alice")]


async def test_reset_ledger_clear_failure_logs_and_still_replies() -> None:
    """A failing persistence-side clear must not break the reply text."""
    chats = _FakeChatDispatcher(raise_on_clear_ledger=True)
    store = ConversationStore(rate_per_second=100, burst=100, ledger_maxlen=10)
    session = await store.get_or_create(ConversationKey("linling", "g1", "u1"))
    session.dsl_events.append(_dsl_event())

    router, actions = _build_router(chats=chats, conversations=store)
    await router.handle(_event("/reset"))

    # In-memory ledger still cleared atomically, regardless of KV error.
    assert len(session.dsl_events) == 0
    # Persistence call was attempted.
    assert chats.ledger_clears == [("g1", "_group")]
    # The user-visible reply is unchanged.
    assert "cleared" in actions[-1].segments[0].text.lower()


async def test_reset_does_not_call_clear_ledger_when_dispatcher_not_protocol() -> None:
    """Backward compatibility:older dispatchers without ``clear_ledger``
    keep working and only chat history is touched."""
    chats = _LegacyChatDispatcher()
    store = ConversationStore(rate_per_second=100, burst=100, ledger_maxlen=10)
    session = await store.get_or_create(ConversationKey("linling", "g1", "u1"))
    session.history.append(Message(role="user", content="x"))
    session.dsl_events.append(_dsl_event())

    router, actions = _build_router(chats=chats, conversations=store)
    await router.handle(_event("/reset"))

    # Chat history cleared via the legacy ``HistoryReset`` path.
    assert chats.history_clears == [("g1", "u1")]
    # In-memory ledger still cleared (Router does that regardless of dispatcher).
    assert len(session.dsl_events) == 0
    assert len(session.history) == 0
    assert "cleared" in actions[-1].segments[0].text.lower()


async def test_reset_resets_both_hydrated_flags() -> None:
    """Both rehydrate flags are dropped so the next turn re-reads KV."""
    chats = _FakeChatDispatcher()
    store = ConversationStore(rate_per_second=100, burst=100, ledger_maxlen=10)
    session = await store.get_or_create(ConversationKey("linling", "g1", "u1"))
    object.__setattr__(session, "_linling_history_hydrated", True)
    object.__setattr__(session, "_linling_ledger_hydrated", True)

    router, _ = _build_router(chats=chats, conversations=store)
    await router.handle(_event("/reset"))

    assert getattr(session, "_linling_history_hydrated") is False
    assert getattr(session, "_linling_ledger_hydrated") is False
