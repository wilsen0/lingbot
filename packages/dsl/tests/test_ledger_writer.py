"""Property-based + unit tests for ``LedgerWriter``.

Properties (per design.md "Testing Strategy"):

* **Property 1** — ``Session.dsl_events`` honours its ``maxlen`` FIFO
  invariant under arbitrary append batches (Req 1.8 / 2.4 / 2.5 /
  8.10).
* **Property 2** — every appended :class:`DslEvent` field is derived
  exclusively from the call arguments + handler metadata; nothing
  leaks across calls (Req 1.1–1.6 / 2.1 / 4.3 / 4.4 / 5.1 / 5.2 /
  6.4).
* **Property 3** — visibility decision table:explicit
  ``expose_to_llm`` wins, then ``[内部]`` (``is_internal``), then
  ``Global_Default_Expose`` (Req 1.7 / 3.1–3.6).
* **Property 4** — ``summary_mode`` resolution:valid value wins,
  otherwise ``"with_result"`` (Req 5.1 / 5.2 / 5.6).
* **Property 10** — a failing :class:`LedgerStore.save` does not
  affect the in-memory deque or raise into the dispatcher path
  (Req 8.7 / 9.1 / 9.5).

Plus a handful of boundary unit tests covering Single_Char_Budget
range guards and the truncation length invariant (Req 3.4 / 4.2 /
4.3).
"""

from __future__ import annotations

import asyncio
from collections import deque

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from linling_core.events import Event, Scope, User
from linling_core.pipeline import ConversationKey, DslEvent, Session
from linling_core.segments import TextSegment

from linling_dsl.ast_nodes import Handler
from linling_dsl.ledger import LedgerStore, LedgerWriter

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _event(sender_id: str = "u1", scope_kind: str = "dm") -> Event:
    return Event(
        id="e",
        platform="t",
        bot_id="b",
        scope=Scope(kind=scope_kind, id="s", platform="t"),
        sender=User(id=sender_id, platform="t"),
        segments=[TextSegment(text="x")],
    )


def _handler(
    *,
    trigger: str = "trig",
    is_internal: bool = False,
    expose_to_llm: bool | None = None,
    summary_mode: str | None = None,
) -> Handler:
    return Handler(
        trigger=trigger,
        is_internal=is_internal,
        body=[],
        line=1,
        expose_to_llm=expose_to_llm,
        summary_mode=summary_mode,
    )


def _session(maxlen: int) -> Session:
    """A minimal ``Session`` with a maxlen-bounded ``dsl_events`` deque."""
    return Session(
        key=ConversationKey("b", "s", "u"),
        lock=asyncio.Lock(),
        dsl_events=deque(maxlen=maxlen),
    )


# Hypothesis strategies. Kept local — the writer's surface is small
# enough that a per-test strategy is clearer than a shared module.

_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=0,
    max_size=400,
)


# ---------------------------------------------------------------------------
# Property 1 — Ledger maxlen FIFO invariant
# ---------------------------------------------------------------------------


@settings(max_examples=40, deadline=None)
@given(
    summaries=st.lists(_text, min_size=0, max_size=300),
    maxlen=st.integers(min_value=1, max_value=200),
)
def test_property_1_maxlen_fifo(summaries: list[str], maxlen: int) -> None:
    session = _session(maxlen=maxlen)
    writer = LedgerWriter(single_char_budget=200, global_default_expose=True)
    handler = _handler()
    event = _event()
    for s in summaries:
        writer.append(
            session=session,
            handler=handler,
            captures=[],
            raw_summary=s,
            outcome="ok",
            event=event,
        )
    # The deque never exceeds its declared maxlen.
    assert len(session.dsl_events) <= maxlen
    # When more events were appended than the cap, the most recent
    # ``maxlen`` survive (FIFO).
    if len(summaries) >= maxlen:
        assert len(session.dsl_events) == maxlen


# ---------------------------------------------------------------------------
# Property 2 — Append field consistency
# ---------------------------------------------------------------------------


@settings(max_examples=40, deadline=None)
@given(
    trigger=st.text(min_size=1, max_size=20),
    captures=st.lists(_text, max_size=5),
    summary=_text,
    sender_id=st.text(min_size=0, max_size=20),
    summary_mode=st.sampled_from([None, "trigger_only", "with_result"]),
    outcome=st.sampled_from(["ok", "error"]),
)
def test_property_2_append_field_consistency(
    trigger: str,
    captures: list[str],
    summary: str,
    sender_id: str,
    summary_mode: str | None,
    outcome: str,
) -> None:
    session = _session(maxlen=10)
    writer = LedgerWriter(single_char_budget=200, global_default_expose=True)
    handler = _handler(trigger=trigger, summary_mode=summary_mode)
    event = _event(sender_id=sender_id)
    writer.append(
        session=session,
        handler=handler,
        captures=captures,
        raw_summary=summary,
        outcome=outcome,
        event=event,
    )
    assert len(session.dsl_events) == 1
    ev = session.dsl_events[0]

    # Trigger and outcome echo the input exactly.
    assert ev.trigger == trigger
    assert ev.outcome == outcome

    # ``args`` is an immutable snapshot of captures.
    assert ev.args == tuple(captures)
    assert isinstance(ev.args, tuple)

    # actor_id falls back to ``"_unknown"`` for empty sender ids.
    assert ev.actor_id == (sender_id or "_unknown")

    # mode resolution mirrors the writer's policy.
    expected_mode = summary_mode if summary_mode in ("trigger_only", "with_result") else "with_result"
    assert ev.mode == expected_mode

    # Summary is empty on error or trigger_only; otherwise truncated.
    if outcome == "error" or expected_mode == "trigger_only":
        assert ev.summary == ""
    else:
        # Truncation property: never longer than the budget.
        assert len(ev.summary) <= 200
        # And lossless when input fits the budget.
        if len(summary) <= 200:
            assert ev.summary == summary


# ---------------------------------------------------------------------------
# Property 3 — Expose decision table
# ---------------------------------------------------------------------------


@settings(max_examples=40, deadline=None)
@given(
    explicit=st.sampled_from([None, True, False]),
    is_internal=st.booleans(),
    default=st.booleans(),
)
def test_property_3_expose_decision_table(
    explicit: bool | None,
    is_internal: bool,
    default: bool,
) -> None:
    """First-match precedence:explicit > is_internal → False > default."""
    session = _session(maxlen=10)
    writer = LedgerWriter(global_default_expose=default)
    handler = _handler(is_internal=is_internal, expose_to_llm=explicit)
    writer.append(
        session=session,
        handler=handler,
        captures=[],
        raw_summary="",
        outcome="ok",
        event=_event(),
    )

    if explicit is True:
        appended = True
    elif explicit is False:
        appended = False
    elif is_internal:
        appended = False
    else:
        appended = default

    assert (len(session.dsl_events) == 1) == appended


# ---------------------------------------------------------------------------
# Property 4 — Mode decision
# ---------------------------------------------------------------------------


@settings(max_examples=30, deadline=None)
@given(
    mode_input=st.one_of(
        st.none(),
        st.sampled_from(["trigger_only", "with_result"]),
        st.text(min_size=0, max_size=12),  # arbitrary garbage
        st.integers(),
    ),
)
def test_property_4_mode_decision(mode_input: object) -> None:
    session = _session(maxlen=10)
    writer = LedgerWriter()
    handler = _handler(summary_mode=mode_input)  # type: ignore[arg-type]
    writer.append(
        session=session,
        handler=handler,
        captures=[],
        raw_summary="x",
        outcome="ok",
        event=_event(),
    )
    assert len(session.dsl_events) == 1
    if mode_input in ("trigger_only", "with_result"):
        assert session.dsl_events[0].mode == mode_input
    else:
        assert session.dsl_events[0].mode == "with_result"


# ---------------------------------------------------------------------------
# Property 10 — Save failure isolation
# ---------------------------------------------------------------------------


class _ExplodingStore:
    """:class:`LedgerStore` whose ``save`` always raises."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def save(self, scope_id: str, file_id: str, events: list[DslEvent]) -> None:
        self.calls.append((scope_id, file_id, len(events)))
        raise RuntimeError("kv on fire")

    async def load(self, scope_id: str, file_id: str) -> list[DslEvent]:
        return []

    async def clear(self, scope_id: str, file_id: str) -> None:
        pass


async def test_property_10_save_failure_does_not_raise_into_main_path() -> None:
    """An exploding ``save`` is logged and swallowed; deque is unaffected."""
    session = _session(maxlen=10)
    store = _ExplodingStore()
    writer = LedgerWriter(store=store)

    # Append must return cleanly even though the fire-and-forget save
    # task will fail.
    writer.append(
        session=session,
        handler=_handler(),
        captures=[],
        raw_summary="x",
        outcome="ok",
        event=_event(),
    )
    # The deque already reflects the new event before the save runs.
    assert len(session.dsl_events) == 1

    # Drain the fire-and-forget save task. We grab the latest pending
    # task to ensure its exception is observed (otherwise pytest's
    # asyncio integration warns about an unretrieved exception). The
    # writer guarantees ``_safe_save`` swallows the underlying error,
    # so awaiting the task itself returns cleanly.
    pending = [
        t
        for t in asyncio.all_tasks()
        if t.get_name() == "dsl_ledger_save"
    ]
    for task in pending:
        await task

    assert store.calls == [("s", "u1", 1)]
    # Deque still has the event after the save's exception was logged.
    assert len(session.dsl_events) == 1


async def test_save_failure_field_parity_with_no_store() -> None:
    """An exploding store yields the same in-memory event as ``store=None``."""
    session_no_store = _session(maxlen=10)
    session_bad_store = _session(maxlen=10)
    writer_a = LedgerWriter()
    writer_b = LedgerWriter(store=_ExplodingStore())

    args = dict(
        captures=["a", "b"],
        raw_summary="hello",
        outcome="ok",
        event=_event(sender_id="alice"),
    )
    writer_a.append(session=session_no_store, handler=_handler(), **args)  # type: ignore[arg-type]
    writer_b.append(session=session_bad_store, handler=_handler(), **args)  # type: ignore[arg-type]

    pending = [
        t
        for t in asyncio.all_tasks()
        if t.get_name() == "dsl_ledger_save"
    ]
    for task in pending:
        await task

    a = session_no_store.dsl_events[0]
    b = session_bad_store.dsl_events[0]
    # Field-by-field parity except for the wall-clock timestamps.
    assert a.trigger == b.trigger
    assert a.args == b.args
    assert a.summary == b.summary
    assert a.outcome == b.outcome
    assert a.mode == b.mode
    assert a.actor_id == b.actor_id


# ---------------------------------------------------------------------------
# Boundary unit tests
# ---------------------------------------------------------------------------


def test_single_char_budget_out_of_range_raises_value_error() -> None:
    with pytest.raises(ValueError):
        LedgerWriter(single_char_budget=149)
    with pytest.raises(ValueError):
        LedgerWriter(single_char_budget=301)


def test_truncate_appends_ellipsis_at_exact_budget() -> None:
    """Oversize text produces a result of exactly ``budget`` code points."""
    session = _session(maxlen=2)
    writer = LedgerWriter(single_char_budget=200)
    long_text = "a" * 500
    writer.append(
        session=session,
        handler=_handler(),
        captures=[],
        raw_summary=long_text,
        outcome="ok",
        event=_event(),
    )
    summary = session.dsl_events[0].summary
    assert len(summary) == 200
    assert summary.endswith("\u2026")
    assert summary[:-1] == "a" * 199


def test_global_default_expose_immutable_after_construction() -> None:
    """``global_default_expose`` has no public setter; ``__slots__`` blocks attribute injection."""
    writer = LedgerWriter(global_default_expose=True)
    # Public attribute should not exist by name.
    assert not hasattr(writer, "global_default_expose")
    # ``__slots__`` rejects new attributes outright.
    with pytest.raises(AttributeError):
        writer.global_default_expose = False  # type: ignore[attr-defined]


async def test_audit_failure_does_not_block_ledger_append() -> None:
    """LedgerWriter never invokes the audit sink; an audit failure cannot
    take down ``append`` because there's no shared code path."""
    # The writer takes no audit sink in its constructor — this is the
    # decoupling guarantee from Requirement 10.1 / 10.5. The test
    # simply asserts that property by inspecting the public surface.
    import inspect

    sig = inspect.signature(LedgerWriter.__init__)
    assert "audit" not in sig.parameters
    assert "audit_sink" not in sig.parameters


def test_ledger_store_protocol_is_runtime_checkable() -> None:
    """``LedgerStore`` must be ``runtime_checkable`` for bootstrap wiring."""
    assert isinstance(_ExplodingStore(), LedgerStore)
