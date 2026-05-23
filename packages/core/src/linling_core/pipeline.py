"""Per-conversation concurrency primitives.

This module owns the *non-shared* state for every active conversation:

* A coroutine lock, so messages from the same ``(bot, scope, sender)``
  never race each other through the DSL VM or an agent's ReAct loop.
* A token bucket, so a single spammy user cannot starve others.
* A rolling chat history, for agent short-term memory.
* A cheap idempotency cache so retried deliveries don't produce dupes.

What it deliberately does **not** own:

* The KV store (global per-bot, see :class:`KVStore`).
* Any agent persona or tool catalogue (shared, immutable).
* The event bus (process-global).

Keeping this module stateless w.r.t. the outside world means we can
replace the single-process implementation with a Redis-backed one later
without changing the :class:`Router` or the adapters. That's the main
design constraint.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from linling_core.events import Event

if TYPE_CHECKING:
    import structlog
    from linling_agent.llm import Message


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConversationKey:
    """Uniquely identifies a running conversation.

    ``bot_id`` is always included so a multi-tenant process (several
    bots sharing one router) cannot cross-leak state.

    ``sender_id`` is included for DM and for private-reply semantics;
    group conversations that should be *shared* across members (think
    ``/游戏状态`` in a QRDic-style bot) should collapse ``sender_id`` to
    the empty string at construction time. That's a caller choice, kept
    out of this module.
    """

    bot_id: str
    scope_id: str
    sender_id: str

    @classmethod
    def from_event(cls, event: Event, *, per_sender: bool = True) -> ConversationKey:
        return cls(
            bot_id=event.bot_id,
            scope_id=event.scope.id,
            sender_id=event.sender.id if per_sender else "",
        )


# ---------------------------------------------------------------------------
# DSL action ledger event
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DslEvent:
    """One DSL operation surfaced to the LLM-visible ledger.

    A ``DslEvent`` is the smallest unit of "user did this DSL thing"
    that the agent's chat dispatcher can surface back to the LLM as a
    transient ``<recent_user_actions>`` block. It is *not* a chat
    message, *not* persisted via ``KVHistoryStore``, and *not* emitted
    on the audit sink — those are independent pipelines.

    The class is ``frozen=True`` so mutations only happen at the
    enclosing ``deque`` boundary (``append`` / ``popleft``) and never
    via in-place edit; ``slots=True`` keeps memory tight under the
    absolute ``Ledger_Maxlen=200`` upper bound.

    Fields:
        timestamp:    Local "HH:MM:SS" string (zero-padded, length 8;
                      no date, no timezone, no millis). Stable across
                      processes only modulo wall-clock — for sorting and
                      TTL comparisons, use ``occurred_at`` instead.
        trigger:      The handler trigger text that matched. The parser
                      strips any ``[内部]`` prefix before populating
                      ``Handler.trigger``, so this never carries that
                      marker.
                      .
        args:         Immutable snapshot of ``HandlerMatch.captures``.
                      Stored as a ``tuple`` (not ``list``) so the frozen
                      dataclass remains hashable and can be compared
                      cheaply in property tests.
        summary:      Truncated text summary of the handler's output
                      (``Single_Char_Budget`` characters max, with a
                      single ``…`` U+2026 sentinel when truncated).
                      Empty string ``""`` when ``outcome == "error"`` or
                      ``mode == "trigger_only"``.
        outcome:      ``"ok"`` for successful handler runs, ``"error"``
                      for VM exceptions or ``VMResult.ok is False``.
                      Renderer skips ``"error"`` events but the audit /
                      debug surfaces still see them.
        mode:         ``"trigger_only"`` (record only "user did X" with
                      no result text) or ``"with_result"`` (include
                      truncated summary). Defaults to ``"with_result"``
                      when handler metadata is missing.
        actor_id:     ``event.sender.id``, or ``"_unknown"`` when the
                      sender id is missing. Always populated; the
                      renderer decides whether to emit ``by="..."``
                      based on the conversation scope kind.
        occurred_at:  ``time.time()`` epoch seconds (float). Used for
                      cross-process TTL comparisons and stable sorting
                      after KV rehydrate; never use ``time.monotonic()``
                      here because rehydrate must compare against
                      absolute wall-clock TTL.
    """

    timestamp: str
    trigger: str
    args: tuple[str, ...]
    summary: str
    outcome: str
    mode: str
    actor_id: str
    occurred_at: float


def ledger_scope_keys(
    event: Event,
    *,
    logger: structlog.BoundLogger | None = None,
) -> tuple[str, str]:
    """Return ``(scope_id, file_id)`` for ledger KV writes / lookups.

    The ledger has its own scoping rules, distinct from chat history:

    * **group** scope → ``(scope.id, "_group")``. Every member of the
      group shares the same persisted ledger so the LLM sees a unified
      "what just happened in this room" view, regardless of which user
      asked the follow-up question.
    * **dm** scope → ``(scope.id, sender.id)``. Each user keeps a
      private ledger; sender ids are never crossed.
    * Any other scope kind (``"system"`` today, possible future
      additions) falls back to the dm-style key and emits a structured
      warning ``pipeline.ledger_scope_unknown`` so operators can spot
      adapters that produce events the ledger wasn't designed for.

    Empty / missing ``sender.id`` is mapped to the literal string
    ``"_unknown"`` (matching :class:`DslEvent.actor_id`'s convention)
    so KV keys stay non-empty and deterministic.

    This function is **only** consumed by the ledger code path. The
    chat history scope still uses ``(scope.id, sender.id)`` regardless
    of ``scope.kind`` — see Requirement 6.7. Callers must not route
    history reads/writes through here.

    Parameters:
        event: The inbound :class:`Event` whose scope determines the
               ledger key.
        logger: Optional structlog ``BoundLogger`` used to emit the
                ``pipeline.ledger_scope_unknown`` warning. When
                ``None``, the warning is silently dropped, which keeps
                the function side-effect-free for unit tests.

    Returns:
        A ``(scope_id, file_id)`` tuple ready to pass into
        :class:`KVDslLedgerStore` or any :class:`LedgerStore`-shaped
        object.
    """
    scope_id = event.scope.id
    sender_id = event.sender.id or "_unknown"
    if event.scope.kind == "group":
        return scope_id, "_group"
    if event.scope.kind == "dm":
        return scope_id, sender_id
    if logger is not None:
        logger.warning(
            "pipeline.ledger_scope_unknown",
            scope_kind=event.scope.kind,
            scope_id=scope_id,
            sender_id=event.sender.id,
        )
    return scope_id, sender_id


# ---------------------------------------------------------------------------
# Rate limiter: simple async token bucket
# ---------------------------------------------------------------------------


class TokenBucket:
    """Classic token bucket, refilled lazily on each ``acquire``.

    All math is done on ``time.monotonic()`` so clock jumps cannot
    disrupt ordering. Thread-unsafe but coroutine-safe (we never await
    inside the update block).
    """

    __slots__ = ("_capacity", "_last", "_rate", "_tokens")

    def __init__(self, *, rate: float, capacity: float) -> None:
        if rate <= 0 or capacity <= 0:
            raise ValueError("rate and capacity must be positive")
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity
        self._last = time.monotonic()

    def try_acquire(self, amount: float = 1.0) -> bool:
        now = time.monotonic()
        elapsed = now - self._last
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last = now
        if self._tokens >= amount:
            self._tokens -= amount
            return True
        return False

    @property
    def tokens(self) -> float:
        """Diagnostic accessor; does not refill."""
        return self._tokens


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@dataclass
class Session:
    """Per-conversation runtime state.

    The lock guarantees that at most one of ``(classifier → router →
    handler/agent)`` is running for this conversation at any time. The
    bucket caps the QPS at which we're *willing* to spend agent/tool
    resources; misses translate into a friendly "slow down" reply at the
    router level, not a hard drop.

    The ``cancel_event`` is how the router's built-in ``/cancel`` command
    signals an in-flight **chat** dispatch to bail early. The
    :class:`AgentChatDispatcher` races its LLM call against this event
    via :func:`asyncio.wait` — on fire, the LLM request is cancelled
    and no history is persisted for the truncated turn. DSL command
    dispatches are *not* cancellable (they may be mid read-modify-write
    on the KV store), which is a deliberate safety choice.
    """

    key: ConversationKey
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    rate_limiter: TokenBucket | None = None
    history: deque[Message] = field(default_factory=deque)
    dsl_events: deque[DslEvent] = field(default_factory=deque)
    last_active: float = field(default_factory=time.monotonic)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)


# ---------------------------------------------------------------------------
# Conversation store
# ---------------------------------------------------------------------------


class ConversationStore:
    """LRU-evicting map of active sessions.

    Eviction is lazy: ``get_or_create`` bumps the item to the MRU end,
    and when we exceed ``max_sessions`` the LRU entry is dropped. TTL is
    enforced by an optional periodic sweep — exposed as a coroutine the
    router can schedule if desired. For small deployments (<1k sessions)
    the LRU policy alone is enough.

    Concurrency: the top-level OrderedDict is protected by a short
    asyncio lock; per-session state is protected by the session's own
    lock, and they don't overlap.
    """

    def __init__(
        self,
        *,
        max_sessions: int = 10_000,
        ttl_seconds: float | None = 3_600.0,
        history_turns: int = 16,
        rate_per_second: float = 1.0,
        burst: float = 5.0,
        ledger_maxlen: int = 20,
    ) -> None:
        if max_sessions <= 0:
            raise ValueError("max_sessions must be positive")
        if not 1 <= ledger_maxlen <= 200:
            raise ValueError("ledger_maxlen must be in [1, 200]")
        self._max = max_sessions
        self._ttl = ttl_seconds
        self._turns = history_turns
        self._rate = rate_per_second
        self._burst = burst
        self._ledger_maxlen = ledger_maxlen
        self._sessions: OrderedDict[ConversationKey, Session] = OrderedDict()
        self._guard = asyncio.Lock()

    # ------------------------------------------------------------------ public

    async def get_or_create(self, key: ConversationKey) -> Session:
        async with self._guard:
            existing = self._sessions.get(key)
            if existing is not None:
                existing.last_active = time.monotonic()
                self._sessions.move_to_end(key)
                return existing

            session = Session(
                key=key,
                rate_limiter=TokenBucket(rate=self._rate, capacity=self._burst),
                history=deque(maxlen=self._turns),
                dsl_events=deque(maxlen=self._ledger_maxlen),
            )
            self._sessions[key] = session
            self._evict_if_needed()
            return session

    async def drop(self, key: ConversationKey) -> None:
        async with self._guard:
            self._sessions.pop(key, None)

    async def sweep(self) -> int:
        """Remove sessions that exceeded TTL. Returns number removed.

        Safe to call periodically from a background task. If
        ``ttl_seconds`` is ``None`` this is a no-op. Sessions whose
        lock is currently held are spared — they're mid-dispatch and
        will be cleaned up on the next sweep after they release.
        """
        if self._ttl is None:
            return 0
        now = time.monotonic()
        removed = 0
        async with self._guard:
            stale: list[ConversationKey] = [
                k
                for k, s in self._sessions.items()
                if now - s.last_active > self._ttl and not s.lock.locked()
            ]
            for k in stale:
                self._sessions.pop(k, None)
                removed += 1
        return removed

    def snapshot_size(self) -> int:
        """Diagnostic; not exact under heavy concurrency."""
        return len(self._sessions)

    # ------------------------------------------------------------------ helpers

    def _evict_if_needed(self) -> None:
        """LRU-evict surplus sessions, skipping those currently in use.

        A session whose lock is held is mid-dispatch — evicting it
        would let a fresh session for the same key receive a new
        message in parallel, defeating the per-user serialisation
        invariant. We walk from oldest to newest and pop the first
        free entry; if every session is busy we accept that we're
        temporarily over the cap (the next cycle will catch up).
        """
        # Must be called holding ``_guard``.
        while len(self._sessions) > self._max:
            evicted = False
            for key, session in list(self._sessions.items()):
                if session.lock.locked():
                    continue
                self._sessions.pop(key, None)
                evicted = True
                break
            if not evicted:
                # All sessions are in use; we're over cap until one finishes.
                return


# ---------------------------------------------------------------------------
# Idempotency cache
# ---------------------------------------------------------------------------


class SeenEvents:
    """Bounded FIFO set, for "have we processed this event id already?".

    Retry-at-least-once delivery is the norm for webhook adapters; this
    cache suppresses the second delivery silently. Not a cryptographic
    guarantee, just enough for a typical 1 minute retry window at a few
    hundred events per second.
    """

    __slots__ = ("_max", "_order", "_set")

    def __init__(self, *, maxlen: int = 2048) -> None:
        if maxlen <= 0:
            raise ValueError("maxlen must be positive")
        self._max = maxlen
        self._order: deque[str] = deque()
        self._set: set[str] = set()

    def add(self, event_id: str) -> bool:
        """Record an id; return ``True`` if it was unseen, ``False`` if duplicate."""
        if event_id in self._set:
            return False
        self._set.add(event_id)
        self._order.append(event_id)
        while len(self._order) > self._max:
            oldest = self._order.popleft()
            self._set.discard(oldest)
        return True
