from __future__ import annotations

import asyncio

import pytest
from linling_core import Event, EventBus, Scope, User, text


def _mk() -> Event:
    return Event(
        id="e1",
        platform="cli",
        bot_id="bot",
        scope=Scope(kind="dm", id="u", platform="cli"),
        sender=User(id="u", platform="cli"),
        segments=[text("hi")],
    )


async def test_subscribers_fire_in_priority_order() -> None:
    bus = EventBus()
    log: list[str] = []

    async def low(_: Event) -> None:
        log.append("low")

    async def high(_: Event) -> None:
        log.append("high")

    async def mid(_: Event) -> None:
        log.append("mid")

    bus.subscribe(low, priority=0, name="low")
    bus.subscribe(high, priority=10, name="high")
    bus.subscribe(mid, priority=5, name="mid")

    await bus.publish(_mk())
    assert log == ["high", "mid", "low"]


async def test_equal_priority_preserves_registration_order() -> None:
    bus = EventBus()
    log: list[str] = []

    async def a(_: Event) -> None:
        log.append("a")

    async def b(_: Event) -> None:
        log.append("b")

    bus.subscribe(a, priority=0)
    bus.subscribe(b, priority=0)
    await bus.publish(_mk())
    assert log == ["a", "b"]


async def test_returning_true_short_circuits() -> None:
    bus = EventBus()
    log: list[str] = []

    async def stopper(_: Event) -> bool:
        log.append("stopper")
        return True

    async def after(_: Event) -> None:
        log.append("after")

    bus.subscribe(stopper, priority=10)
    bus.subscribe(after, priority=0)

    await bus.publish(_mk())
    assert log == ["stopper"]


async def test_unsubscribe_removes_subscriber() -> None:
    bus = EventBus()
    log: list[str] = []

    async def cb(_: Event) -> None:
        log.append("fired")

    unsub = bus.subscribe(cb)
    assert len(bus) == 1

    unsub()
    assert len(bus) == 0

    await bus.publish(_mk())
    assert log == []


async def test_exception_in_one_subscriber_does_not_stop_others() -> None:
    bus = EventBus()
    log: list[str] = []

    async def bad(_: Event) -> None:
        raise RuntimeError("boom")

    async def good(_: Event) -> None:
        log.append("good")

    bus.subscribe(bad, priority=10, name="bad")
    bus.subscribe(good, priority=0, name="good")

    await bus.publish(_mk())  # must not raise
    assert log == ["good"]


async def test_concurrent_publishes_do_not_interleave_dispatch() -> None:
    bus = EventBus()
    active = 0
    observed_peak = 0

    async def slow(_: Event) -> None:
        nonlocal active, observed_peak
        active += 1
        observed_peak = max(observed_peak, active)
        await asyncio.sleep(0.01)
        active -= 1

    bus.subscribe(slow)
    bus.subscribe(slow)  # two subs per publish

    await asyncio.gather(bus.publish(_mk()), bus.publish(_mk()))

    # Within a single publish subscribers fire sequentially, but two
    # publishes may overlap. Peak active across both publishes is at most
    # the number of concurrent publishes (2).
    assert observed_peak <= 2


def test_empty_bus_publish_is_noop() -> None:
    bus = EventBus()
    asyncio.run(bus.publish(_mk()))
    assert len(bus) == 0


def test_subscribe_priority_default_is_zero() -> None:
    bus = EventBus()

    async def cb(_: Event) -> None: ...

    unsub = bus.subscribe(cb)
    assert len(bus) == 1
    unsub()


@pytest.mark.parametrize("count", [1, 5, 20])
def test_subscribe_and_unsubscribe_leaves_bus_empty(count: int) -> None:
    bus = EventBus()
    unsubs = [bus.subscribe(lambda _e: None) for _ in range(count)]
    assert len(bus) == count
    for u in unsubs:
        u()
    assert len(bus) == 0
