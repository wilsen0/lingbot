"""DSL Action Ledger primitives in linling_core.pipeline.

Covers tasks 1.5 (Session default deque, ledger_maxlen FIFO,
ledger_scope_keys three branches) and 4.2 (Handler default metadata
+ legacy construction).

These are the read-only invariants every other ledger test relies on,
so we keep them lightweight and side-effect free.
"""

from __future__ import annotations

from collections import deque

import pytest
from linling_core.events import Event, Scope, User
from linling_core.pipeline import (
    ConversationKey,
    ConversationStore,
    DslEvent,
    Session,
    ledger_scope_keys,
)
from linling_core.segments import TextSegment


def _make_event(*, scope_kind: str = "dm", sender_id: str = "u1") -> Event:
    return Event(
        id="evt-1",
        platform="test",
        bot_id="bot1",
        scope=Scope(kind=scope_kind, id="scope-x", platform="test"),
        sender=User(id=sender_id, platform="test"),
        segments=[TextSegment(text="hi")],
    )


def _make_dsl_event(occurred_at: float = 0.0, summary: str = "s") -> DslEvent:
    return DslEvent(
        timestamp="00:00:00",
        trigger="t",
        args=(),
        summary=summary,
        outcome="ok",
        mode="with_result",
        actor_id="u1",
        occurred_at=occurred_at,
    )


# ---------------------------------------------------------------------------
# Session default deque + maxlen FIFO (Requirement 1.8 / 8.10)
# ---------------------------------------------------------------------------


async def test_session_has_empty_dsl_events_by_default() -> None:
    """Bare ``Session(key=..., lock=...)`` constructions still work."""
    import asyncio

    session = Session(key=ConversationKey("b", "s", "u"), lock=asyncio.Lock())
    assert isinstance(session.dsl_events, deque)
    assert len(session.dsl_events) == 0
    # ``maxlen`` is ``None`` for the bare constructor — only the
    # ConversationStore's factory sets it. That's fine: tests that
    # need bounded behaviour go through ``get_or_create``.
    assert session.dsl_events.maxlen is None


async def test_ledger_maxlen_fifo_evicts_oldest_on_overflow() -> None:
    store = ConversationStore(ledger_maxlen=3, rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("b", "s", "u"))

    for i in range(5):
        session.dsl_events.append(_make_dsl_event(occurred_at=float(i)))

    # FIFO eviction kept the *most recent* three.
    assert [e.occurred_at for e in session.dsl_events] == [2.0, 3.0, 4.0]
    assert session.dsl_events.maxlen == 3


def test_ledger_maxlen_out_of_range_raises() -> None:
    with pytest.raises(ValueError):
        ConversationStore(ledger_maxlen=0)
    with pytest.raises(ValueError):
        ConversationStore(ledger_maxlen=201)


# ---------------------------------------------------------------------------
# ledger_scope_keys three branches (Requirement 6.1 / 6.2 / 6.3 / 6.7)
# ---------------------------------------------------------------------------


def test_ledger_scope_keys_group_collapses_to_underscore_group() -> None:
    event = _make_event(scope_kind="group", sender_id="alice")
    scope_id, file_id = ledger_scope_keys(event)
    assert scope_id == "scope-x"
    assert file_id == "_group"


def test_ledger_scope_keys_dm_keeps_sender_id() -> None:
    event = _make_event(scope_kind="dm", sender_id="alice")
    assert ledger_scope_keys(event) == ("scope-x", "alice")


def test_ledger_scope_keys_dm_empty_sender_falls_back_to_unknown() -> None:
    event = _make_event(scope_kind="dm", sender_id="")
    assert ledger_scope_keys(event) == ("scope-x", "_unknown")


def test_ledger_scope_keys_unknown_kind_logs_and_falls_back() -> None:
    """system / future scope kinds use dm-style keys + a warn log."""
    captured: list[tuple[str, dict[str, object]]] = []

    class FakeLogger:
        def warning(self, name: str, **kw: object) -> None:
            captured.append((name, kw))

    event = _make_event(scope_kind="system", sender_id="alice")
    scope_id, file_id = ledger_scope_keys(event, logger=FakeLogger())  # type: ignore[arg-type]
    assert scope_id == "scope-x"
    assert file_id == "alice"
    assert captured and captured[0][0] == "pipeline.ledger_scope_unknown"
    assert captured[0][1]["scope_kind"] == "system"


def test_ledger_scope_keys_silent_without_logger() -> None:
    """No logger means no side effects — keeps the function pure-ish."""
    event = _make_event(scope_kind="system", sender_id="alice")
    # Just check it doesn't raise; a missing logger is the unit-test default.
    assert ledger_scope_keys(event) == ("scope-x", "alice")


# ---------------------------------------------------------------------------
# Handler default metadata + legacy construction (Requirement 3.6 / 5.6)
# ---------------------------------------------------------------------------


def test_handler_default_metadata_none() -> None:
    """Fresh handlers have ``None`` metadata by default."""
    from linling_dsl.ast_nodes import Handler

    h = Handler(trigger="ping", is_internal=False, body=[], line=1)
    assert h.expose_to_llm is None
    assert h.summary_mode is None


def test_legacy_handler_construction_still_valid() -> None:
    """Pre-feature keyword construction must not raise.

    The two new fields are appended at the end of the dataclass with
    defaults, so any old call site that named the four legacy
    parameters keeps working unchanged.
    """
    from linling_dsl.ast_nodes import Handler

    h = Handler(trigger="ping", is_internal=True, body=[], line=42)
    assert h.trigger == "ping"
    assert h.is_internal is True
    assert h.line == 42
    # ``getattr`` fallback path used by LedgerWriter._resolve_expose
    # / _resolve_mode: missing attributes resolve to ``None``.
    assert getattr(h, "expose_to_llm", None) is None
    assert getattr(h, "summary_mode", None) is None
