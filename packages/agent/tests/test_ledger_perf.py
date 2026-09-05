"""Performance smoke test for ``LedgerWriter.append``.

Task 13.3:Requirement 8.1 says the main dispatch path must not pay
more than 5 ms per ``append`` even when a slow store is wired in.
``append`` is synchronous and the persistence call is fire-and-forget
(``asyncio.create_task``), so the budget is essentially "as long as
deque.append + tuple()". This test asserts that envelope holds:
P99 ``append`` time over 100 calls stays well under 5 ms even if the
store deliberately blocks for 50 ms inside ``save``.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from statistics import quantiles

from linling_core.events import Event, Scope, User
from linling_core.pipeline import ConversationKey, DslEvent, Session
from linling_core.segments import TextSegment
from linling_dsl.ast_nodes import Handler
from linling_dsl.ledger import LedgerWriter


class _SlowStore:
    """A store whose ``save`` deliberately blocks; isolates main-path budget."""

    async def save(self, scope_id: str, file_id: str, events: list[DslEvent]) -> None:
        # 50 ms — an order of magnitude over the 5 ms budget.
        await asyncio.sleep(0.05)

    async def load(self, scope_id: str, file_id: str) -> list[DslEvent]:
        return []

    async def clear(self, scope_id: str, file_id: str) -> None:
        pass


def _event() -> Event:
    return Event(
        id="e",
        platform="t",
        bot_id="b",
        scope=Scope(kind="dm", id="s", platform="t"),
        sender=User(id="u1", platform="t"),
        segments=[TextSegment(text="x")],
    )


async def test_append_under_5ms_main_path_with_slow_store() -> None:
    """100 ``append`` calls; 99th percentile main-path time < 5 ms."""
    session = Session(
        key=ConversationKey("b", "s", "u"),
        lock=asyncio.Lock(),
        dsl_events=deque(maxlen=200),
    )
    writer = LedgerWriter(store=_SlowStore())
    handler = Handler(trigger="t", is_internal=False, body=[], line=1)
    event = _event()

    durations: list[float] = []
    for _ in range(100):
        t0 = time.perf_counter()
        writer.append(
            session=session,
            handler=handler,
            captures=[],
            raw_summary="x",
            outcome="ok",
            event=event,
        )
        durations.append((time.perf_counter() - t0) * 1000.0)

    # ``quantiles`` with n=100 and method='inclusive' gives us the
    # 99th percentile at index 98.
    p99 = quantiles(durations, n=100, method="inclusive")[98]
    assert p99 < 5.0, f"P99 append took {p99:.3f}ms (>5ms budget); samples: {durations[-5:]}"

    # Drain the fire-and-forget save tasks so pytest's asyncio integration
    # can clean up cleanly; we don't await individual results because
    # _safe_save swallows the underlying exception.
    pending = [t for t in asyncio.all_tasks() if t.get_name() == "dsl_ledger_save"]
    for task in pending:
        await task
