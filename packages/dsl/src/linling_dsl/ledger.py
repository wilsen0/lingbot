"""DSL Action Ledger — write-side primitives.

This module hosts the DSL-side of the Action Ledger feature: the
``LedgerStore`` :class:`typing.Protocol` (a structural interface for the
optional persistence layer) and the ``LedgerWriter`` helper that
resolves ``expose_to_llm`` / ``summary_mode`` and appends a
:class:`linling_core.pipeline.DslEvent` to ``Session.dsl_events``.

Dependency direction is kept tight: this module sits inside
``linling_dsl`` and depends on ``linling_core.pipeline.DslEvent`` only.
It deliberately does **not** import ``linling_agent``; the persistent
backing store lives there (``KVDslLedgerStore``) but is injected from
the bootstrap layer as anything matching the structural protocol below,
so the ``linling_dsl → linling_agent`` reverse dependency is avoided.
The ``linling_agent`` package defines its own structurally identical
``LedgerStore`` for the read side; both surfaces are duck-typed and
``runtime_checkable`` so an ``isinstance(store, LedgerStore)`` works
in either context.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import structlog

from linling_core.pipeline import DslEvent, ledger_scope_keys

if TYPE_CHECKING:
    from linling_core.events import Event
    from linling_core.pipeline import Session

    from linling_dsl.ast_nodes import Handler


logger = structlog.get_logger(__name__)

_ELLIPSIS = "\u2026"  # U+2026 HORIZONTAL ELLIPSIS, single code point.

# Single_Char_Budget bounds (Requirement 4.2).
_BUDGET_MIN = 150
_BUDGET_MAX = 300

# Allowed ``summary_mode`` values (Requirement 5.1 / 5.6).
_VALID_MODES = frozenset({"trigger_only", "with_result"})


@runtime_checkable
class LedgerStore(Protocol):
    """Structural interface for the optional ledger persistence layer.

    Implementations persist ``DslEvent`` deques to a key-value backing
    store under a namespace fully separated from chat history (see
    Requirement 8.2 / 8.8). The ``(scope_id, file_id)`` pair is the
    ledger's scope key, derived by
    :func:`linling_core.pipeline.ledger_scope_keys` — group scope
    collapses to ``file_id == "_group"`` so every member of the room
    sees a unified ledger; dm scope keeps per-sender isolation.

    All three methods are coroutines because real implementations sit
    on top of an async :class:`linling_core.storage.kv.KVStore`. They
    are best-effort: failures are logged by the implementation and
    must never propagate up the DSL dispatch path (see Requirement
    8.7). The ``LedgerWriter`` schedules ``save`` via
    :func:`asyncio.create_task` so the main path stays under the 5 ms
    budget even when the underlying KV is slow.

    The ``runtime_checkable`` decorator lets bootstrap code do
    ``isinstance(store, LedgerStore)`` for opt-in wiring; the
    ``Router`` follows the same pattern for ``HistoryReset`` /
    ``LedgerReset``.
    """

    async def save(
        self,
        scope_id: str,
        file_id: str,
        events: list[DslEvent],
    ) -> None:
        """Persist the *current* ledger snapshot for ``(scope_id, file_id)``.

        Implementations replace any prior payload — the snapshot is
        the source of truth, not a delta. They are also responsible
        for trimming to ``Ledger_Maxlen`` (default 20, absolute cap
        200) before writing; the writer hands over the full deque
        contents but the store decides what fits its on-disk schema.
        """

    async def load(
        self,
        scope_id: str,
        file_id: str,
    ) -> list[DslEvent]:
        """Return events for ``(scope_id, file_id)`` sorted by ``occurred_at``.

        Implementations drop entries past TTL (default 3600 s) and
        skip rows that fail schema validation, logging
        ``kv_dsl_ledger_store.record_corrupt`` for each one. An empty
        list is the not-found / nothing-to-restore signal; callers
        must not raise on absent state.
        """

    async def clear(
        self,
        scope_id: str,
        file_id: str,
    ) -> None:
        """Drop the persisted ledger for ``(scope_id, file_id)``.

        Called by ``Router._do_reset`` so a ``/reset`` empties chat
        history and the ledger atomically (Requirement 7.1 / 7.2).
        Implementations must scope the delete strictly to the given
        key — never wildcard across scopes.
        """


class LedgerWriter:
    """Resolve handler metadata and append a :class:`DslEvent` to the session.

    The writer is the single chokepoint where DSL handler execution
    becomes a ledger entry. It owns three orthogonal decisions:

    1. **Visibility** (``_resolve_expose``) — should this handler's
       run surface to the LLM at all? Explicit ``expose_to_llm`` on
       the handler wins; ``[内部]`` handlers (``handler.is_internal``)
       fall to ``False`` next; otherwise the dispatcher-wide
       ``Global_Default_Expose`` decides. See Requirements 3.1–3.6.
    2. **Mode** (``_resolve_mode``) — does the LLM see the trigger
       only (``"trigger_only"``) or the truncated output as well
       (``"with_result"``)? Defaults to ``"with_result"`` on missing
       or invalid metadata so the more informative form wins by
       default. See Requirements 5.1 / 5.2 / 5.6.
    3. **Summary truncation** (``_truncate``) — output text is
       capped at ``Single_Char_Budget`` Unicode code points; oversize
       content is sliced to ``budget - 1`` chars and topped with a
       single ``…`` (U+2026), making the final length exactly
       ``budget``. See Requirements 4.1 / 4.3 / 4.4.

    The writer is intentionally **synchronous** on the main path: it
    only mutates an in-memory ``deque`` (which already enforces FIFO
    eviction via its ``maxlen``). When a ``LedgerStore`` is wired in,
    the persist call is fire-and-forget and lives behind a separate
    code path (see task 3.3) so the main dispatch budget stays
    well under 5 ms even with a slow KV.

    All knobs are set at construction time and never re-read; in
    particular, ``global_default_expose`` is captured eagerly so a
    later mutation of the same flag elsewhere can never change a
    long-lived dispatcher's behaviour mid-flight (Requirement 3.4).
    """

    __slots__ = ("_budget", "_default_expose", "_store")

    def __init__(
        self,
        *,
        store: LedgerStore | None = None,
        single_char_budget: int = 200,
        global_default_expose: bool = True,
    ) -> None:
        """Construct a writer with a fixed budget and default expose flag.

        Parameters:
            store: Optional persistence layer matching
                :class:`LedgerStore`. ``None`` keeps the writer
                purely in-memory; the bootstrap layer can pass a
                real ``KVDslLedgerStore`` later without changing any
                call sites.
            single_char_budget: Per-event ``summary`` length cap, in
                Unicode code points (see Requirement 4.1). Must lie
                in ``[150, 300]`` inclusive — values outside that
                range raise :class:`ValueError` rather than silently
                clamp, so misconfiguration surfaces at boot
                (Requirement 4.2).
            global_default_expose: Fallback ``expose_to_llm`` value
                applied when neither the handler metadata nor the
                ``[内部]`` prefix decides. Captured by value so the
                writer's behaviour is immutable post-construction
                (Requirement 3.4).
        """
        if not _BUDGET_MIN <= single_char_budget <= _BUDGET_MAX:
            raise ValueError(
                f"single_char_budget out of range "
                f"[{_BUDGET_MIN}, {_BUDGET_MAX}]: {single_char_budget!r}"
            )
        # Bind to private slots; ``__slots__`` keeps ``global_default_expose``
        # immutable in spirit — outside callers cannot rebind the public
        # attribute because none exists.
        self._budget = single_char_budget
        self._default_expose = bool(global_default_expose)
        self._store = store

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append(
        self,
        *,
        session: Session,
        handler: Handler,
        captures: list[str],
        raw_summary: str,
        outcome: str,
        event: Event,
    ) -> None:
        """Materialise a :class:`DslEvent` and append it to ``session.dsl_events``.

        The writer is a no-op when the handler resolves to
        ``expose_to_llm = False`` — neither the deque nor the backing
        store is touched (Requirement 1.7 / 3.1). Otherwise a single
        frozen :class:`DslEvent` is constructed with all field values
        derived from this method's arguments and the helper resolvers
        below; the deque's ``maxlen`` then enforces FIFO eviction
        (Requirement 1.8).

        Parameters:
            session: The session whose ``dsl_events`` deque will
                receive the new entry. The caller (the DSL command
                dispatcher) holds ``session.lock`` for the duration
                of this call.
            handler: The matched :class:`Handler` AST node. Used for
                its ``trigger`` text plus optional ``expose_to_llm``
                and ``summary_mode`` metadata fields.
            captures: ``HandlerMatch.captures`` — the regex capture
                groups, or an empty list for literal-match handlers.
                Stored as an immutable ``tuple`` snapshot.
            raw_summary: The full, untruncated joined output text
                (caller is responsible for concatenation order).
                Ignored when ``outcome == "error"`` or the resolved
                mode is ``"trigger_only"``.
            outcome: ``"ok"`` for successful runs, ``"error"`` for
                VM exceptions / ``VMResult.ok is False``. Anything
                else is treated like an error (the Renderer skips
                non-``"ok"`` events anyway).
            event: The originating :class:`Event`. Only its
                ``sender.id`` is read here; group-vs-DM scope routing
                lives in the persistence path (task 3.3).
        """
        if not self._resolve_expose(handler):
            return
        mode = self._resolve_mode(handler)
        summary = self._resolve_summary(raw_summary, outcome=outcome, mode=mode)
        # Requirement 6.4:fall back to "_unknown" on missing sender id so
        # the field is always non-empty and the renderer can decide whether
        # to surface ``by="..."`` purely on scope kind.
        actor_id = event.sender.id or "_unknown"
        ev = DslEvent(
            timestamp=time.strftime("%H:%M:%S", time.localtime()),
            trigger=handler.trigger,
            args=tuple(captures),
            summary=summary,
            outcome=outcome,
            mode=mode,
            actor_id=actor_id,
            occurred_at=time.time(),
        )
        # ``deque.append`` honours ``maxlen`` and evicts the oldest entry
        # in O(1) when full — this is the only mutation point.
        session.dsl_events.append(ev)

        # Requirement 8.1 / 8.7 / 10.5:if a persistence layer is wired
        # in, fire-and-forget the save so the main dispatch path stays
        # well under the 5 ms budget. Failures inside the save task are
        # logged inside ``_safe_save`` and never propagated.
        if self._store is not None:
            scope_id, file_id = ledger_scope_keys(event, logger=logger)
            # Snapshot under the caller's session lock — once the
            # task is scheduled, ``session.dsl_events`` may continue
            # to mutate from later dispatches.
            snapshot = list(session.dsl_events)
            asyncio.create_task(
                self._safe_save(scope_id, file_id, snapshot),
                name="dsl_ledger_save",
            )

    async def _safe_save(
        self,
        scope_id: str,
        file_id: str,
        events: list[DslEvent],
    ) -> None:
        """Forward ``save`` to the configured store, swallowing failures.

        Requirement 8.7 / 10.5:any exception raised by the store
        implementation is captured here so it never bubbles up the
        DSL dispatch path. Operators see the failure via the
        ``dsl_dispatcher.ledger_save_failed`` structured log line;
        users see no error.
        """
        # Defensive ``assert`` so type checkers narrow ``self._store``.
        # ``append`` only schedules this task when ``_store is not None``,
        # so the assertion is dead code at runtime.
        assert self._store is not None
        try:
            await self._store.save(scope_id, file_id, events)
        except Exception:
            logger.exception(
                "dsl_dispatcher.ledger_save_failed",
                scope_id=scope_id,
                file_id=file_id,
            )

    # ------------------------------------------------------------------
    # Internal resolvers
    # ------------------------------------------------------------------

    def _resolve_expose(self, handler: Handler) -> bool:
        """Resolve the effective ``expose_to_llm`` flag for ``handler``.

        Precedence (first match wins, per Requirement 3.1 / 3.2 / 3.3):

        1. Explicit ``expose_to_llm = True`` / ``False`` on the
           handler metadata.
        2. ``handler.is_internal`` truthy → ``False`` (the parser has
           already stripped the ``[内部]`` prefix and set this flag).
        3. ``Global_Default_Expose`` captured at construction.

        Non-bool values for the metadata field — including ``None``,
        strings, numbers, etc. — are treated as "not declared" and
        fall through to the next rule (Requirement 3.6). This makes
        the parser's job simpler: it can leave ``expose_to_llm``
        as ``None`` whenever the source value is missing or
        un-coercible without aborting handler load.
        """
        explicit = getattr(handler, "expose_to_llm", None)
        # ``is True`` / ``is False`` so truthy strings, 0/1 ints, etc. fall
        # through to the next rule rather than coerce silently.
        if explicit is True:
            return True
        if explicit is False:
            return False
        if handler.is_internal:
            return False
        return self._default_expose

    def _resolve_mode(self, handler: Handler) -> str:
        """Resolve ``DslEvent.mode`` from optional handler metadata.

        Returns the declared ``summary_mode`` only when it is one of
        the two valid string values; everything else (``None``,
        unknown strings, non-strings) defaults to ``"with_result"``
        (Requirement 5.2 / 5.6). The fallback is the more informative
        choice, matching the design rationale that handlers must
        opt *out* of carrying their result text rather than opt in.
        """
        mode = getattr(handler, "summary_mode", None)
        if mode in _VALID_MODES:
            # Mypy/Pyright can't see the membership check narrows ``mode``
            # to ``str``; the explicit cast keeps the return type honest
            # without runtime overhead.
            return str(mode)
        return "with_result"

    def _resolve_summary(
        self,
        raw_summary: str,
        *,
        outcome: str,
        mode: str,
    ) -> str:
        """Apply the empty-on-error / empty-on-trigger-only rule, then truncate.

        Requirement 1.6 / 2.1 / 5.3:both error events and
        ``trigger_only`` handlers carry an empty summary regardless
        of what the caller passed in. Every other event runs through
        :meth:`_truncate` to enforce ``Single_Char_Budget``.
        """
        if outcome == "error" or mode == "trigger_only":
            return ""
        return self._truncate(raw_summary)

    def _truncate(self, text: str) -> str:
        """Cap ``text`` at the configured ``Single_Char_Budget``.

        Returns ``text`` unchanged when its Unicode code-point length
        is at most ``budget``; otherwise slices the first
        ``budget - 1`` code points and appends a single ``…`` (U+2026)
        sentinel so the final length is exactly ``budget``
        (Requirement 4.3 / 4.4). The ellipsis is one code point
        regardless of the surrounding text's normalisation form, so
        the post-truncation length matches ``len(result) == budget``
        in every case.
        """
        if len(text) <= self._budget:
            return text
        return text[: self._budget - 1] + _ELLIPSIS


__all__ = ["LedgerStore", "LedgerWriter"]
