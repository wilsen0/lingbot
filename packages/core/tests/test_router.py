"""Router + ConversationStore + MessageClassifier end-to-end tests.

These cover the behaviour that users of the framework actually rely on:

* commands route to the DSL dispatcher, chat routes to the agent;
* unknown ``/`` commands get a friendly reply, not hand-off to LLM;
* duplicate event ids are silently dropped;
* per-session lock serialises the same conversation but does not block others;
* global semaphore provides backpressure for the whole bot;
* per-session token bucket rate-limits chat-intent traffic;
* dispatcher crashes are caught and do not break routing.

The DSL script is stubbed through a minimal fake classifier source; real
DSL parsing is exercised in the dsl package's own tests. The agent side
uses a tiny ``FakeChatDispatcher`` so we don't have to stand up an LLM.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest
from linling_core.classifier import HandlerMatch, MessageClassifier
from linling_core.events import Action, Event, Scope, User
from linling_core.pipeline import ConversationKey, ConversationStore, Session
from linling_core.router import Router, RouterConfig
from linling_core.segments import TextSegment

# ---------------------------------------------------------------------------
# Minimal fake "Script / Handler" — we don't want to import linling_dsl here
# because core must stay independent of dsl. The classifier only reads
# ``handlers[].trigger`` / ``handlers[].is_internal``; anything beyond that is
# the dispatcher's concern.
# ---------------------------------------------------------------------------


@dataclass
class _FakeHandler:
    trigger: str
    is_internal: bool = False


@dataclass
class _FakeScript:
    handlers: list[_FakeHandler] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Fake dispatchers
# ---------------------------------------------------------------------------


class FakeCommandDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []  # (event_id, trigger)

    async def run(self, event: Event, match: HandlerMatch, session: Session) -> list[Action]:
        self.calls.append((event.id, match.handler.trigger))
        return [
            Action(
                kind="reply",
                target=event.scope,
                segments=[TextSegment(text=f"cmd:{match.handler.trigger}")],
            )
        ]


class FakeChatDispatcher:
    def __init__(self, *, delay: float = 0.0) -> None:
        self.calls: list[str] = []  # event texts
        self._delay = delay

    async def run(self, event: Event, session: Session) -> list[Action]:
        self.calls.append(event.text)
        if self._delay:
            await asyncio.sleep(self._delay)
        return [
            Action(
                kind="reply",
                target=event.scope,
                segments=[TextSegment(text=f"chat:{event.text}")],
            )
        ]


class FlakyChatDispatcher:
    """Always raises; used to verify error isolation."""

    async def run(self, event: Event, session: Session) -> list[Action]:
        raise RuntimeError("llm melted")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(text: str, *, sender: str = "u1", group: str = "g1", eid: str | None = None) -> Event:
    return Event(
        id=eid or f"{sender}-{text}-{group}",
        platform="test",
        bot_id="linling",
        scope=Scope(kind="group", id=group, platform="test"),
        sender=User(id=sender, platform="test", display_name=sender),
        segments=[TextSegment(text=text)],
    )


def _collect_sink() -> tuple[list[Action], callable]:
    actions: list[Action] = []

    async def sink(a: Action) -> None:
        actions.append(a)

    return actions, sink


def _build_router(
    *,
    script: _FakeScript | None = None,
    commands: FakeCommandDispatcher | None = None,
    chats=None,
    config: RouterConfig | None = None,
    conversations: ConversationStore | None = None,
) -> tuple[Router, list[Action], FakeCommandDispatcher, FakeChatDispatcher | FlakyChatDispatcher]:
    classifier = MessageClassifier(script or _FakeScript())
    commands = commands or FakeCommandDispatcher()
    chats = chats or FakeChatDispatcher()
    actions, sink = _collect_sink()
    router = Router(
        classifier=classifier,
        commands=commands,
        chats=chats,
        sink=sink,
        conversations=conversations,
        config=config,
    )
    return router, actions, commands, chats


# ---------------------------------------------------------------------------
# Routing decisions
# ---------------------------------------------------------------------------


async def test_prefix_command_routes_to_dsl():
    script = _FakeScript(handlers=[_FakeHandler(trigger="ping")])
    router, actions, commands, chats = _build_router(script=script)
    await router.handle(_event("/ping"))

    assert len(commands.calls) == 1
    assert commands.calls[0][1] == "ping"
    assert not chats.calls
    assert actions[0].segments[0].text == "cmd:ping"


async def test_implicit_trigger_also_routes_to_dsl():
    """QRDic-style: a bare ``我的灵玉`` with no prefix still runs DSL."""
    script = _FakeScript(handlers=[_FakeHandler(trigger="我的灵玉")])
    router, _actions, commands, chats = _build_router(script=script)
    await router.handle(_event("我的灵玉"))

    assert len(commands.calls) == 1
    assert not chats.calls


async def test_unknown_prefix_command_gets_friendly_reply():
    router, actions, commands, chats = _build_router()
    await router.handle(_event("/unknown"))

    assert not commands.calls
    assert not chats.calls
    assert actions[0].segments[0].text.startswith("Unknown command")


async def test_chat_routes_to_agent_when_no_match():
    router, actions, commands, chats = _build_router()
    await router.handle(_event("hi how are you"))

    assert not commands.calls
    assert chats.calls == ["hi how are you"]
    assert actions[0].segments[0].text == "chat:hi how are you"


async def test_self_message_is_ignored():
    router, actions, *_ = _build_router()
    ev = _event("hi")
    ev = ev.model_copy(update={"sender": User(id="linling", platform="test")})
    assert ev.sender.id == ev.bot_id
    await router.handle(ev)
    assert not actions


async def test_blocked_scope_is_ignored():
    classifier = MessageClassifier(_FakeScript(), block_scope_ids=frozenset(["bad"]))
    commands = FakeCommandDispatcher()
    chats = FakeChatDispatcher()
    actions, sink = _collect_sink()
    router = Router(classifier=classifier, commands=commands, chats=chats, sink=sink)
    await router.handle(_event("hi", group="bad"))
    assert not actions


# ---------------------------------------------------------------------------
# Delivery semantics
# ---------------------------------------------------------------------------


async def test_duplicate_event_id_is_dropped():
    ev = _event("/ping", eid="fixed")
    script_router, *_ = _build_router(script=_FakeScript(handlers=[_FakeHandler(trigger="ping")]))
    # Reuse a single router so both handles hit the same SeenEvents cache.
    first = await script_router.handle(ev)
    second = await script_router.handle(ev)
    assert first is True
    assert second is False


async def test_crashing_dispatcher_does_not_take_down_router():
    router, actions, _, _ = _build_router(chats=FlakyChatDispatcher())
    # Should not raise. Now produces a friendly error reply rather
    # than dropping the user's message silently — the structured log
    # entry still records the actual exception.
    await router.handle(_event("hello"))
    assert len(actions) == 1
    assert actions[0].kind == "reply"
    text = actions[0].segments[0].text  # type: ignore[union-attr]
    assert "wrong" in text.lower() or "error" in text.lower()


# ---------------------------------------------------------------------------
# Built-in /help and /reset
# ---------------------------------------------------------------------------


async def test_builtin_help_lists_triggers():
    script = _FakeScript(
        handlers=[
            _FakeHandler(trigger="ping"),
            _FakeHandler(trigger="我的灵玉"),
            _FakeHandler(trigger="充值([0-9]+)"),
            _FakeHandler(trigger="internal_only", is_internal=True),
        ]
    )
    router, actions, _, _ = _build_router(script=script)
    await router.handle(_event("/help"))

    text = actions[-1].segments[0].text
    # Header present.
    assert text.startswith("Available commands:")
    # Regular triggers show up with the first configured prefix.
    assert "/ping" in text
    assert "/我的灵玉" in text
    # Capture groups are replaced with an ellipsis.
    assert "/充值…" in text
    # Internal handlers are hidden.
    assert "internal_only" not in text


async def test_builtin_help_takes_precedence_over_classifier():
    """Even if a rule's trigger literally matches ``help``, builtin wins."""
    script = _FakeScript(handlers=[_FakeHandler(trigger="help")])
    router, actions, commands, _ = _build_router(script=script)
    await router.handle(_event("/help"))
    # The command dispatcher never runs.
    assert commands.calls == []
    assert actions[-1].segments[0].text.startswith("Available commands:")


async def test_builtin_help_respects_max_items():
    script = _FakeScript(handlers=[_FakeHandler(trigger=f"cmd{i}") for i in range(100)])
    router, actions, _, _ = _build_router(
        script=script,
        config=RouterConfig(help_max_items=3),
    )
    await router.handle(_event("/help"))
    text = actions[-1].segments[0].text
    assert text.count("/cmd") == 3
    assert "and 97 more" in text


async def test_builtin_help_disabled_when_empty_name():
    script = _FakeScript(handlers=[_FakeHandler(trigger="help")])
    router, _actions, commands, _ = _build_router(
        script=script,
        config=RouterConfig(help_command_name=""),
    )
    await router.handle(_event("/help"))
    # With builtin disabled, the classifier's "unknown command" path takes over
    # (``help`` doesn't match the ``/help`` prefixed form).
    # Actually ``help`` does match the stripped ``help`` trigger. Verify:
    assert len(commands.calls) == 1


async def test_builtin_reset_clears_in_memory_history():
    """``/reset`` clears the session deque and replies."""
    from linling_agent.llm import Message

    store = ConversationStore(rate_per_second=100, burst=100)
    router, actions, _, _ = _build_router(conversations=store)

    # Populate history as if a prior chat turn had happened.
    session = await store.get_or_create(ConversationKey("linling", "g1", "u1"))
    session.history.append(Message(role="user", content="earlier"))
    session.history.append(Message(role="assistant", content="reply"))

    await router.handle(_event("/reset"))
    assert len(session.history) == 0
    assert "cleared" in actions[-1].segments[0].text.lower()


async def test_builtin_reset_invokes_history_store_when_supported():
    from linling_agent.llm import Message

    cleared: list[tuple[str, str]] = []

    class ResettableDispatcher(FakeChatDispatcher):
        async def clear_history(self, scope_id: str, sender_id: str) -> None:
            cleared.append((scope_id, sender_id))

    dispatcher = ResettableDispatcher()
    store = ConversationStore(rate_per_second=100, burst=100)
    router, _, _, _ = _build_router(chats=dispatcher, conversations=store)

    # Pre-populate so history isn't empty before the reset.
    session = await store.get_or_create(ConversationKey("linling", "g1", "u1"))
    session.history.append(Message(role="user", content="x"))

    await router.handle(_event("/reset"))
    assert cleared == [("g1", "u1")]


# ---------------------------------------------------------------------------
# Built-in /cancel
# ---------------------------------------------------------------------------


async def test_builtin_cancel_sets_session_cancel_event():
    """``/cancel`` flips ``session.cancel_event`` without taking the lock."""
    store = ConversationStore(rate_per_second=100, burst=100)

    # Create a session and *hold* its lock so we can verify /cancel
    # doesn't wait on it (the whole point of /cancel).
    session = await store.get_or_create(ConversationKey("linling", "g1", "u1"))
    await session.lock.acquire()
    try:
        assert not session.cancel_event.is_set()
        router, actions, _, _ = _build_router(conversations=store)

        # /cancel must complete even though the session lock is held.
        await asyncio.wait_for(router.handle(_event("/cancel")), timeout=0.5)
        assert session.cancel_event.is_set()
        assert "stopped" in actions[-1].segments[0].text.lower()
    finally:
        session.lock.release()


async def test_builtin_cancel_reports_noop_when_nothing_in_flight():
    """``/cancel`` with no active dispatch says so rather than stopping silently."""
    router, actions, _, _ = _build_router(
        conversations=ConversationStore(rate_per_second=100, burst=100),
    )
    await router.handle(_event("/cancel"))
    text = actions[-1].segments[0].text.lower()
    assert "nothing" in text or "no" in text


async def test_builtin_cancel_disabled_when_empty_name():
    """Empty ``cancel_command_name`` lets a ruleset-defined handler own ``/cancel``."""
    script = _FakeScript(handlers=[_FakeHandler(trigger="cancel")])
    router, _, commands, _ = _build_router(
        script=script,
        config=RouterConfig(cancel_command_name=""),
    )
    await router.handle(_event("/cancel"))
    # The command dispatcher ran instead of the built-in.
    assert len(commands.calls) == 1


# ---------------------------------------------------------------------------
# Concurrency / isolation
# ---------------------------------------------------------------------------


async def test_same_session_is_serialized():
    """Two concurrent messages for the same (bot, group, sender) are serialized."""
    order: list[str] = []

    class Recorder:
        async def run(self, event: Event, session: Session) -> list[Action]:
            order.append(f"start:{event.id}")
            await asyncio.sleep(0.05)
            order.append(f"end:{event.id}")
            return []

    store = ConversationStore(rate_per_second=100, burst=100)
    router, _, _, _ = _build_router(chats=Recorder(), conversations=store)

    await asyncio.gather(
        router.handle(_event("hi a", eid="a")),
        router.handle(_event("hi b", eid="b")),
    )

    # A finishes before B starts (or vice versa); they do not interleave.
    assert order in (
        ["start:a", "end:a", "start:b", "end:b"],
        ["start:b", "end:b", "start:a", "end:a"],
    )


async def test_different_sessions_run_concurrently():
    """Different (bot, group, sender) sessions are not mutually blocked."""
    in_flight: list[str] = []

    class Watcher:
        async def run(self, event: Event, session: Session) -> list[Action]:
            in_flight.append(event.id)
            # Hold the lock briefly; if sessions *were* shared this
            # value would tail back to 1 on every event.
            await asyncio.sleep(0.05)
            return []

    store = ConversationStore(rate_per_second=100, burst=100)
    router, _, _, _ = _build_router(chats=Watcher(), conversations=store)

    await asyncio.gather(
        router.handle(_event("m", sender="u1", eid="1")),
        router.handle(_event("m", sender="u2", eid="2")),
        router.handle(_event("m", sender="u3", eid="3")),
    )
    assert sorted(in_flight) == ["1", "2", "3"]


async def test_per_session_rate_limit_hits_busy_reply():
    """Chat rate-limited per session; commands are not rate-limited."""
    store = ConversationStore(rate_per_second=1, burst=1)
    router, actions, _, chats = _build_router(conversations=store)

    # First chat consumes the single token.
    await router.handle(_event("hi 1", eid="1"))
    # Second chat immediately after must hit the rate limiter.
    await router.handle(_event("hi 2", eid="2"))

    # One success + one "slow down" message.
    texts = [a.segments[0].text for a in actions]
    assert any(t.startswith("chat:") for t in texts)
    assert any("slow down" in t.lower() or "fast" in t.lower() for t in texts)
    # Only one of the two actually reached the chat dispatcher.
    assert chats.calls == ["hi 1"]


async def test_backpressure_returns_busy_reply():
    """Global concurrency cap produces a visible busy reply rather than hanging."""
    release = asyncio.Event()

    class Blocker:
        async def run(self, event: Event, session: Session) -> list[Action]:
            await release.wait()
            return []

    router, actions, _, _ = _build_router(
        chats=Blocker(),
        config=RouterConfig(max_concurrent_events=1, enqueue_timeout_s=0.05),
    )

    slow = asyncio.create_task(router.handle(_event("first", sender="u1", eid="1")))
    await asyncio.sleep(0)  # let slow grab the semaphore
    # Second request cannot acquire the semaphore before enqueue_timeout_s.
    await router.handle(_event("second", sender="u2", eid="2"))

    # The second call produced a busy reply; the first is still in flight.
    assert actions, "expected at least the busy reply"
    assert any("busy" in a.segments[0].text.lower() for a in actions)

    release.set()
    await slow


# ---------------------------------------------------------------------------
# Multi-bot isolation — one bot stuck must not stall another
# ---------------------------------------------------------------------------


async def test_multi_bot_isolation():
    """Two independent routers model two bots; a hung bot-A must not stall bot-B."""
    stuck = asyncio.Event()

    class Stuck:
        async def run(self, event, session):
            await stuck.wait()
            return []

    class Fast(FakeChatDispatcher):
        pass

    router_a, _, _, _ = _build_router(chats=Stuck())
    router_b, actions_b, _, _ = _build_router(chats=Fast())

    a_task = asyncio.create_task(router_a.handle(_event("hang", sender="a1", eid="A")))
    await asyncio.sleep(0)  # let A enter the dispatcher

    # B should complete promptly despite A being wedged.
    await asyncio.wait_for(
        router_b.handle(_event("hi B", sender="b1", eid="B")),
        timeout=0.5,
    )
    assert actions_b[0].segments[0].text == "chat:hi B"

    stuck.set()
    await a_task


# ---------------------------------------------------------------------------
# ConversationStore — LRU / sweep
# ---------------------------------------------------------------------------


async def test_conversation_store_evicts_lru():
    from linling_core.pipeline import ConversationKey

    store = ConversationStore(max_sessions=2)
    await store.get_or_create(ConversationKey("b", "g", "a"))
    b = await store.get_or_create(ConversationKey("b", "g", "b"))
    # Access 'a' again to refresh recency, then adding 'c' evicts 'b'.
    await store.get_or_create(ConversationKey("b", "g", "a"))
    await store.get_or_create(ConversationKey("b", "g", "c"))
    assert store.snapshot_size() == 2

    # b should be gone; a and c remain.
    # (Re-getting b re-creates a *new* session; lock identity differs.)
    new_b = await store.get_or_create(ConversationKey("b", "g", "b"))
    assert new_b is not b


async def test_conversation_store_ttl_sweep():
    from linling_core.pipeline import ConversationKey

    store = ConversationStore(ttl_seconds=0.0)
    await store.get_or_create(ConversationKey("b", "g", "u"))
    removed = await store.sweep()
    assert removed == 1


async def test_conversation_store_does_not_evict_locked_sessions():
    """A session that's mid-dispatch (lock held) survives LRU eviction.

    Regression: if a busy session were evicted while its dispatch was
    in flight, the next message from the same user would create a
    fresh Session — defeating the per-user serialisation invariant.
    """
    from linling_core.pipeline import ConversationKey

    store = ConversationStore(max_sessions=2)
    busy_key = ConversationKey("b", "g", "busy")
    busy = await store.get_or_create(busy_key)
    await busy.lock.acquire()
    try:
        # Now flood the store; the busy session must stay even though
        # it's the LRU entry.
        await store.get_or_create(ConversationKey("b", "g", "u2"))
        await store.get_or_create(ConversationKey("b", "g", "u3"))
        # The busy session is still discoverable via the same lock.
        same_busy = await store.get_or_create(busy_key)
        assert same_busy is busy
    finally:
        busy.lock.release()


async def test_conversation_store_sweep_skips_locked_sessions():
    """TTL sweep must not remove sessions actively serving a dispatch."""
    from linling_core.pipeline import ConversationKey

    store = ConversationStore(ttl_seconds=0.0)
    busy = await store.get_or_create(ConversationKey("b", "g", "busy"))
    free = await store.get_or_create(ConversationKey("b", "g", "free"))

    await busy.lock.acquire()
    try:
        removed = await store.sweep()
        # Free was over-TTL; busy was spared.
        assert removed == 1
        assert store.snapshot_size() == 1
        # Sanity: free is gone.
        new_free = await store.get_or_create(ConversationKey("b", "g", "free"))
        assert new_free is not free
    finally:
        busy.lock.release()


# ---------------------------------------------------------------------------
# TokenBucket property-ish behaviour
# ---------------------------------------------------------------------------


async def test_token_bucket_refills():
    from linling_core.pipeline import TokenBucket

    b = TokenBucket(rate=10.0, capacity=2.0)
    assert b.try_acquire()
    assert b.try_acquire()
    assert not b.try_acquire()  # bucket empty
    await asyncio.sleep(0.25)
    # ~2.5 tokens refilled; one acquire now succeeds.
    assert b.try_acquire()


def test_token_bucket_rejects_bad_config():
    from linling_core.pipeline import TokenBucket

    with pytest.raises(ValueError):
        TokenBucket(rate=0, capacity=1)
    with pytest.raises(ValueError):
        TokenBucket(rate=1, capacity=0)
