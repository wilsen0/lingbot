from __future__ import annotations

from datetime import UTC, datetime

from linling_core import Event, Scope, User, at, text


def _mk_event(segments: list | None = None, kind: str = "group") -> Event:
    segments = segments if segments is not None else [text("hello")]
    return Event(
        id="msg-1",
        platform="onebot",
        bot_id="10000",
        scope=Scope(kind=kind, id="20000", platform="onebot"),
        sender=User(id="30000", platform="onebot", display_name="alice"),
        time=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
        segments=segments,
    )


def test_event_text_property_joins_text_segments() -> None:
    e = _mk_event([text("foo "), at("99"), text("bar")])
    assert e.text == "foo bar"


def test_event_is_group_vs_is_dm() -> None:
    assert _mk_event(kind="group").is_group is True
    assert _mk_event(kind="group").is_dm is False
    assert _mk_event(kind="dm").is_dm is True


def test_event_roundtrip_preserves_segments() -> None:
    original = _mk_event([text("hi"), at("1")])
    data = original.model_dump()
    restored = Event.model_validate(data)
    assert restored == original


def test_default_time_is_utc_aware() -> None:
    e = Event(
        id="x",
        platform="cli",
        bot_id="bot",
        scope=Scope(kind="system", id="main", platform="cli"),
        sender=User(id="sys", platform="cli"),
    )
    assert e.time.tzinfo is not None
