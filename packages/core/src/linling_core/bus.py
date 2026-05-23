"""Asynchronous in-process event bus.

Adapters publish :class:`Event` instances; the kernel (router / pipeline)
subscribes. Subscribers have priorities: higher priority fires first. A
subscriber can short-circuit further delivery by returning ``True`` from
its callback; useful for authoritative handlers like rate-limiting.

The bus is intentionally minimal — persistence, retries, and fan-out
across processes are out of scope for P0. For the kernel's router we use
a single in-process bus per bot instance.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import itertools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeAlias

import structlog

from linling_core.events import Event

Subscriber: TypeAlias = Callable[[Event], Awaitable[bool | None]]
"""Subscriber callable. Return ``True`` to stop propagation."""


@dataclass(order=True)
class _Entry:
    # heapq is a min-heap; we want higher priority first, so negate.
    neg_priority: int
    seq: int  # tiebreaker preserves registration order for equal priorities
    cb: Subscriber = field(compare=False)
    name: str = field(compare=False, default="")


class EventBus:
    """In-process priority-aware async event bus."""

    def __init__(self) -> None:
        self._entries: list[_Entry] = []
        self._seq = itertools.count()
        self._lock = asyncio.Lock()

    def subscribe(self, cb: Subscriber, *, priority: int = 0, name: str = "") -> Callable[[], None]:
        """Register a subscriber.

        :param priority: higher fires first. Default 0.
        :param name: optional label for logging.
        :returns: an ``unsubscribe()`` callable.

        Subscribe / unsubscribe / publish-snapshot are all sync at the
        Python level; no ``await`` separates them, so the asyncio
        single-threaded guarantee makes them race-free without an
        explicit lock. Subscribers themselves run inside ``publish``
        under the lock to guarantee a consistent view.
        """
        entry = _Entry(neg_priority=-priority, seq=next(self._seq), cb=cb, name=name)
        heapq.heappush(self._entries, entry)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._entries.remove(entry)
                heapq.heapify(self._entries)

        return unsubscribe

    async def publish(self, event: Event) -> None:
        """Publish an event to all subscribers in priority order.

        Subscribers are called sequentially (not concurrently) so handlers
        can reliably short-circuit. If you need concurrent fan-out, wrap
        your subscriber with :func:`asyncio.create_task`.
        """
        # Snapshot under lock to avoid mutation during dispatch.
        async with self._lock:
            ordered = sorted(self._entries)  # stable: (neg_priority, seq)

        for entry in ordered:
            try:
                result = await entry.cb(event)
            except Exception:
                # The bus must not lose events because one handler raised.
                structlog.get_logger(__name__).exception(
                    "subscriber_failed",
                    subscriber=entry.name or repr(entry.cb),
                    event_id=event.id,
                )
                continue
            if result is True:
                break

    def __len__(self) -> int:
        return len(self._entries)
