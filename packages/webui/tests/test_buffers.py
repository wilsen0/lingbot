"""Event ring buffer behaviour."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from linling_core.events import Event, Scope, User
from linling_core.segments import TextSegment
from linling_webui.buffers import EventRingBuffer


def _mk_event(eid: str, text: str = "hi") -> Event:
    scope = Scope(kind="group", id="g1", platform="test")
    sender = User(id="u1", platform="test")
    return Event(
        id=eid,
        platform="test",
        bot_id="bot1",
        scope=scope,
        sender=sender,
        time=datetime.now(UTC),
        kind="message",
        segments=[TextSegment(text=text)],
    )


@pytest.mark.asyncio
async def test_ring_buffer_drops_oldest_on_overflow() -> None:
    buf = EventRingBuffer(capacity=3, bot_id="bot1")
    for i in range(5):
        await buf.publish(_mk_event(f"e{i}"))
    tail = await buf.tail()
    assert [be.event.id for be in tail] == ["e2", "e3", "e4"]
    assert [be.seq for be in tail] == [3, 4, 5]


@pytest.mark.asyncio
async def test_ring_buffer_since_replay() -> None:
    buf = EventRingBuffer(capacity=100, bot_id="bot1")
    for i in range(10):
        await buf.publish(_mk_event(f"e{i}"))
    tail = await buf.tail(since_seq=5)
    assert [be.seq for be in tail] == [6, 7, 8, 9, 10]


@pytest.mark.asyncio
async def test_ring_buffer_subscribe_and_unsubscribe() -> None:
    buf = EventRingBuffer(capacity=10, bot_id="bot1")
    received: list[str] = []

    async def cb(be):  # type: ignore[no-untyped-def]
        received.append(be.event.id)

    unsubscribe = buf.subscribe(cb)
    await buf.publish(_mk_event("a"))
    await buf.publish(_mk_event("b"))
    unsubscribe()
    await buf.publish(_mk_event("c"))

    assert received == ["a", "b"]
