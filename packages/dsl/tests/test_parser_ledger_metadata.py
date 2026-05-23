"""Parser-side coverage for DSL Action Ledger handler metadata.

Tasks 10.3 (unit:legal / missing / illegal metadata values) and
10.4 (re-run Properties 3 & 4 against parser-populated handlers).

The parser recognises ``^expose_to_llm: <bool>`` and
``^summary_mode: <mode>`` directive lines that immediately follow a
handler trigger. Invalid values fall back to ``None`` rather than
aborting handler load (Requirement 3.6 / 5.6).
"""

from __future__ import annotations

import asyncio
from collections import deque

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from linling_core.events import Event, Scope, User
from linling_core.pipeline import ConversationKey, Session
from linling_core.segments import TextSegment

from linling_dsl.ledger import LedgerWriter
from linling_dsl.parser import parse


def _event() -> Event:
    return Event(
        id="e",
        platform="t",
        bot_id="b",
        scope=Scope(kind="dm", id="s", platform="t"),
        sender=User(id="u1", platform="t"),
        segments=[TextSegment(text="ping")],
    )


def _session() -> Session:
    return Session(
        key=ConversationKey("b", "s", "u"),
        lock=asyncio.Lock(),
        dsl_events=deque(maxlen=10),
    )


# ---------------------------------------------------------------------------
# Task 10.3 — parser parses legal / missing / illegal metadata values
# ---------------------------------------------------------------------------


def test_parser_recognizes_expose_to_llm_true() -> None:
    script = parse("ping\n^expose_to_llm: true\nPONG\n", strict=False)
    assert len(script.handlers) == 1
    assert script.handlers[0].expose_to_llm is True


def test_parser_recognizes_expose_to_llm_false() -> None:
    script = parse("ping\n^expose_to_llm: false\nPONG\n", strict=False)
    assert script.handlers[0].expose_to_llm is False


def test_parser_recognizes_summary_mode_trigger_only() -> None:
    script = parse("ping\n^summary_mode: trigger_only\nPONG\n", strict=False)
    assert script.handlers[0].summary_mode == "trigger_only"


def test_parser_recognizes_summary_mode_with_result() -> None:
    script = parse("ping\n^summary_mode: with_result\nPONG\n", strict=False)
    assert script.handlers[0].summary_mode == "with_result"


def test_parser_handler_without_metadata_keeps_none_defaults() -> None:
    """A handler without ``^...`` directives leaves both fields ``None``."""
    script = parse("ping\nPONG\n", strict=False)
    h = script.handlers[0]
    assert h.expose_to_llm is None
    assert h.summary_mode is None


def test_parser_falls_back_on_invalid_metadata_value() -> None:
    """Garbage values land as ``None`` and the handler still loads."""
    script = parse(
        "ping\n^expose_to_llm: garbage\n^summary_mode: nope\nPONG\n",
        strict=False,
    )
    h = script.handlers[0]
    assert h.expose_to_llm is None
    assert h.summary_mode is None


def test_parser_recognizes_multiple_metadata_directives() -> None:
    script = parse(
        "ping\n^expose_to_llm: false\n^summary_mode: trigger_only\nPONG\n",
        strict=False,
    )
    h = script.handlers[0]
    assert h.expose_to_llm is False
    assert h.summary_mode == "trigger_only"


def test_parser_unknown_directive_falls_through_to_body() -> None:
    """Future-proofing the other way: unknown ``^foo: bar`` directives
    are *not* consumed; they fall through to body parsing as plain
    output text. This protects users from accidentally losing content
    that happens to start with ``^`` while still letting future ledger
    keys be added without a parser change at every consumer site."""
    script = parse("ping\n^future_knob: yes\nPONG\n", strict=False)
    h = script.handlers[0]
    # Unknown directive doesn't touch the metadata fields.
    assert h.expose_to_llm is None
    assert h.summary_mode is None
    # And the body still has both lines (the unknown directive is
    # treated as output text, ``PONG`` is the second statement).
    assert len(h.body) >= 2


# ---------------------------------------------------------------------------
# Task 10.4 — Property 3 (expose) + Property 4 (mode) on parser-built Handlers
# ---------------------------------------------------------------------------


@settings(max_examples=20, deadline=None)
@given(
    expose_value=st.sampled_from(["true", "false", "garbage"]),
    is_internal=st.booleans(),
    default=st.booleans(),
)
def test_property_3_expose_decision_on_parsed_handler(
    expose_value: str,
    is_internal: bool,
    default: bool,
) -> None:
    """The expose decision table holds for parser-populated handlers
    just as it does for hand-built ones."""
    prefix = "[内部]" if is_internal else ""
    source = f"{prefix}ping\n^expose_to_llm: {expose_value}\nPONG\n"
    script = parse(source, strict=False)
    handler = script.handlers[0]

    session = _session()
    writer = LedgerWriter(global_default_expose=default)
    writer.append(
        session=session,
        handler=handler,
        captures=[],
        raw_summary="x",
        outcome="ok",
        event=_event(),
    )

    if expose_value == "true":
        appended = True
    elif expose_value == "false":
        appended = False
    elif is_internal:
        appended = False
    else:
        appended = default

    assert (len(session.dsl_events) == 1) == appended


@settings(max_examples=20, deadline=None)
@given(
    mode_value=st.sampled_from(["trigger_only", "with_result", "garbage"]),
)
def test_property_4_mode_decision_on_parsed_handler(mode_value: str) -> None:
    """Mode resolution defaults to ``with_result`` for unrecognised values."""
    source = f"ping\n^summary_mode: {mode_value}\nPONG\n"
    script = parse(source, strict=False)
    handler = script.handlers[0]

    session = _session()
    writer = LedgerWriter()
    writer.append(
        session=session,
        handler=handler,
        captures=[],
        raw_summary="x",
        outcome="ok",
        event=_event(),
    )
    assert len(session.dsl_events) == 1
    expected_mode = mode_value if mode_value in ("trigger_only", "with_result") else "with_result"
    assert session.dsl_events[0].mode == expected_mode
