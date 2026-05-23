"""Per-bot in-memory event ring buffer.

The WebUI doesn't persist events — it observes the in-process `EventBus`
and keeps the last N per bot. Clients subscribe via WebSocket to get a
live tail plus optional `since` replay from the buffer.

Thread-safety: `deque.append` is atomic in CPython, but we guard `since`
lookups with an asyncio lock to keep iteration consistent with producers.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from linling_core.events import Event

Subscriber = Callable[["BufferedEvent"], Awaitable[None]]


# Per-subscriber delivery deadline. A slow WS client must not stall the
# event pipeline for everyone else. We pick a generous-but-bounded
# value so brief network jitter doesn't drop deliveries; clients that
# can't keep up get disconnected by their own send-side error path.
_SUB_DELIVER_TIMEOUT_S = 2.0


class BufferedEvent:
    """An envelope wrapping an Event with a monotonic sequence number.

    `seq` is assigned on insertion so clients can ask "give me everything
    after seq X" even if Event ids themselves aren't orderable.
    """

    __slots__ = ("event", "seq")

    def __init__(self, seq: int, event: Event) -> None:
        self.seq = seq
        self.event = event


class EventRingBuffer:
    """Fixed-capacity ring buffer for one bot's events.

    - Producers: `publish(event)` — typically called from an EventBus sub.
    - Consumers: `tail(since_seq=..., limit=...)` or `subscribe(cb)`.
    """

    def __init__(self, *, capacity: int, bot_id: str) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._bot_id = bot_id
        self._items: deque[BufferedEvent] = deque(maxlen=capacity)
        self._seq = itertools.count(1)
        self._subs: list[Subscriber] = []
        self._lock = asyncio.Lock()

    @property
    def bot_id(self) -> str:
        return self._bot_id

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        return len(self._items)

    @property
    def latest_seq(self) -> int:
        return self._items[-1].seq if self._items else 0

    async def publish(self, event: Event) -> None:
        """Append an event and fan out to subscribers (best-effort).

        Subscriber failures or stalls don't drop the event from the
        buffer or starve other subscribers: each callback is awaited
        with a short timeout so a slow WebSocket client cannot block
        the publish path. This is "fire-and-forget with a deadline".
        """
        item = BufferedEvent(seq=next(self._seq), event=event)
        self._items.append(item)
        subs = list(self._subs)
        # Dispatch after append so tail() correctness doesn't depend on sub latency.
        for cb in subs:
            try:
                await asyncio.wait_for(cb(item), timeout=_SUB_DELIVER_TIMEOUT_S)
            except (TimeoutError, Exception):
                # Subscriber failures should never drop events — and a
                # slow client must not freeze the bus. The subscriber
                # itself owns the cleanup of its connection.
                continue

    async def tail(self, *, since_seq: int | None = None, limit: int = 200) -> list[BufferedEvent]:
        """Return buffered events with seq > since_seq (or the last ``limit``)."""
        async with self._lock:
            items = list(self._items)
        if since_seq is not None:
            items = [it for it in items if it.seq > since_seq]
        if limit > 0 and len(items) > limit:
            items = items[-limit:]
        return items

    def subscribe(self, cb: Subscriber) -> Callable[[], None]:
        """Register a live subscriber; returns an unsubscribe callable."""
        self._subs.append(cb)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._subs.remove(cb)

        return unsubscribe
