"""Property-based checks for the event ring buffer.

Uses Hypothesis to verify:
- `since_seq` replays are strictly > given seq
- seq sequence is strictly monotonic
- capacity bound is respected
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from linling_core.events import Event, Scope, User
from linling_core.segments import TextSegment
from linling_webui.buffers import EventRingBuffer


def _mk(eid: str) -> Event:
    return Event(
        id=eid,
        platform="t",
        bot_id="b",
        scope=Scope(kind="group", id="g", platform="t"),
        sender=User(id="u", platform="t"),
        time=datetime.now(UTC),
        kind="message",
        segments=[TextSegment(text=eid)],
    )


@settings(max_examples=40, deadline=None)
@given(st.integers(min_value=1, max_value=50), st.integers(min_value=0, max_value=200))
@pytest.mark.asyncio
async def test_seq_monotonic_and_capacity_bounded(cap: int, n: int) -> None:
    buf = EventRingBuffer(capacity=cap, bot_id="b")
    for i in range(n):
        await buf.publish(_mk(f"e{i}"))
    tail = await buf.tail()
    assert len(tail) <= cap
    seqs = [it.seq for it in tail]
    assert seqs == sorted(seqs), "seqs must be non-decreasing"
    assert len(set(seqs)) == len(seqs), "seqs must be strictly unique"


@settings(max_examples=20, deadline=None)
@given(st.integers(min_value=0, max_value=100))
@pytest.mark.asyncio
async def test_since_seq_filter(since: int) -> None:
    buf = EventRingBuffer(capacity=200, bot_id="b")
    for i in range(50):
        await buf.publish(_mk(f"e{i}"))
    tail = await buf.tail(since_seq=since)
    for it in tail:
        assert it.seq > since
