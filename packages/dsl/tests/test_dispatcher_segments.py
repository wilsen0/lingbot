"""Unit tests for ``DslCommandDispatcher._segments_to_action``.

The dispatcher collapses VM output segments into a single Action.
This module exercises the merging / reordering rules in isolation
so any future regression (e.g. a refactor that drops Reply hoisting)
fails immediately.
"""

from __future__ import annotations

from linling_core.events import Event, Scope, User
from linling_core.segments import (
    ImageSegment,
    ReplySegment,
    TextSegment,
    VoiceSegment,
)
from linling_dsl.dispatcher import _segments_to_action


def _event() -> Event:
    return Event(
        id="test",
        platform="test",
        bot_id="b",
        scope=Scope(kind="group", id="g", platform="test"),
        sender=User(id="u", platform="test"),
        kind="message",
        segments=[],
    )


def test_text_only_collapses_to_single_segment() -> None:
    action = _segments_to_action(
        _event(),
        [TextSegment(text="hello "), TextSegment(text="world")],
    )
    assert len(action.segments) == 1
    assert isinstance(action.segments[0], TextSegment)
    assert action.segments[0].text == "hello world"


def test_text_and_image_preserve_order() -> None:
    action = _segments_to_action(
        _event(),
        [
            TextSegment(text="here is the picture:\n"),
            ImageSegment(url="https://example.com/x.png"),
            TextSegment(text=" — enjoy"),
        ],
    )
    assert len(action.segments) == 3
    assert isinstance(action.segments[0], TextSegment)
    assert action.segments[0].text == "here is the picture:\n"
    assert isinstance(action.segments[1], ImageSegment)
    assert isinstance(action.segments[2], TextSegment)
    assert action.segments[2].text == " — enjoy"


def test_voice_segment_passes_through() -> None:
    action = _segments_to_action(
        _event(),
        [VoiceSegment(url="https://example.com/v.mp3")],
    )
    assert len(action.segments) == 1
    assert isinstance(action.segments[0], VoiceSegment)


def test_late_reply_segment_is_hoisted_to_head() -> None:
    """``±rep msg±`` after ``hello\\n`` lands at index 0 in the action.

    OneBot convention puts ``reply`` at the start of the message
    array; some forks silently drop late replies. The dispatcher
    enforces the convention so handlers can declare ``±rep$`` in any
    order without surprise behaviour.
    """
    action = _segments_to_action(
        _event(),
        [
            TextSegment(text="ok\n"),
            ReplySegment(message_id="12345"),
        ],
    )
    assert len(action.segments) == 2
    assert isinstance(action.segments[0], ReplySegment)
    assert action.segments[0].message_id == "12345"
    assert isinstance(action.segments[1], TextSegment)


def test_reply_at_head_stays_at_head() -> None:
    action = _segments_to_action(
        _event(),
        [
            ReplySegment(message_id="42"),
            TextSegment(text="thanks"),
        ],
    )
    assert isinstance(action.segments[0], ReplySegment)
    assert action.segments[0].message_id == "42"
    assert isinstance(action.segments[1], TextSegment)


def test_only_first_reply_is_hoisted() -> None:
    """Multiple replies are unusual; we keep declaration order for the rest."""
    action = _segments_to_action(
        _event(),
        [
            TextSegment(text="abc"),
            ReplySegment(message_id="A"),
            TextSegment(text=" mid "),
            ReplySegment(message_id="B"),
        ],
    )
    # First reply (A) hoisted to head; second reply (B) stays where it was.
    assert isinstance(action.segments[0], ReplySegment)
    assert action.segments[0].message_id == "A"
    # Reply B shows up in declaration order among the rest.
    reply_b_indices = [
        i
        for i, s in enumerate(action.segments)
        if isinstance(s, ReplySegment) and s.message_id == "B"
    ]
    assert reply_b_indices, "reply B was lost"


def test_empty_segments_returns_action_with_empty_list() -> None:
    action = _segments_to_action(_event(), [])
    assert action.segments == []


def test_action_target_matches_event_scope() -> None:
    event = _event()
    action = _segments_to_action(event, [TextSegment(text="x")])
    assert action.target.id == event.scope.id
    assert action.kind == "reply"
