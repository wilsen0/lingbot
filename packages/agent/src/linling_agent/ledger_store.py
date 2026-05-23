"""Persistent DSL Action Ledger storage backed by :class:`KVStore`.

The store is the agent-side mirror of :class:`Session.dsl_events`. It
sits beside :class:`KVHistoryStore` but uses an entirely separate KV
prefix (``__dsl_ledger__``) so the chat history's
``_only_turn_messages`` invariant cannot accidentally see ledger
records and vice-versa (Requirement 8.2 / 8.8).

Schema (under ``kv`` table):

* scope = ``__dsl_ledger__/<scope_id>``
* file  = ``"_group"`` (group scope) | ``<sender_id>`` (DM)
* key   = ``"events"``
* value = JSON ``{"saved_at": float, "ttl": int, "events": [DslEvent dicts]}``

Bumping to a future schema means writing under ``__dsl_ledger_v2__``
and leaving old rows alone — the ``__dsl_ledger__`` prefix is stable.

The class structurally satisfies both the
:class:`linling_dsl.ledger.LedgerStore` protocol (write side, used by
:class:`LedgerWriter`) and the equivalent agent-side read protocol
exposed below. Both protocols are duck-typed and ``runtime_checkable``
so an ``isinstance(store, LedgerStore)`` succeeds in either context.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import structlog

from linling_core.pipeline import DslEvent

if TYPE_CHECKING:
    from linling_core.storage.kv import KVStore

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# KV layout constants
# ---------------------------------------------------------------------------

_LEDGER_SCOPE_PREFIX = "__dsl_ledger__"
"""Stable prefix that isolates ledger rows from chat history. Operators
can blanket-filter the KV browser on this prefix to inspect or scrub
ledger state without touching any other column."""

_EVENTS_KEY = "events"
"""Single KV key per ``(scope, file)``; the entire deque is stored as
one JSON blob so a load is one read and a save is one write — the
hot path stays O(1) regardless of ``Ledger_Maxlen``."""

# Configuration bounds, mirrored from Requirement 8.3 / 8.10.
_DEFAULT_TTL = 3600  # 1 hour, per spec
_TTL_MIN = 60
_TTL_MAX = 86400
_DEFAULT_MAXLEN = 20
_ABSOLUTE_MAXLEN = 200


# ---------------------------------------------------------------------------
# Read-side protocol (agent twin of linling_dsl.ledger.LedgerStore)
# ---------------------------------------------------------------------------


@runtime_checkable
class LedgerStore(Protocol):
    """Structural read-side interface for the DSL Action Ledger.

    Defined here as a duck-typed twin of
    :class:`linling_dsl.ledger.LedgerStore` so the agent package does
    not need to import from ``linling_dsl`` (preserves the dependency
    direction ``linling_dsl → linling_core`` ∥
    ``linling_agent → linling_core``). Any object satisfying both
    protocols can be wired into the DSL writer *and* the agent
    dispatcher from the same bootstrap construction.
    """

    async def save(
        self,
        scope_id: str,
        file_id: str,
        events: list[DslEvent],
    ) -> None: ...

    async def load(
        self,
        scope_id: str,
        file_id: str,
    ) -> list[DslEvent]: ...

    async def clear(
        self,
        scope_id: str,
        file_id: str,
    ) -> None: ...


# ---------------------------------------------------------------------------
# KVDslLedgerStore
# ---------------------------------------------------------------------------


class KVDslLedgerStore:
    """Persist :class:`DslEvent` deques into a backing :class:`KVStore`.

    Each ``(scope_id, file_id)`` pair maps to one KV row whose value is
    a JSON envelope ``{"saved_at", "ttl", "events": [...]}``. The TTL
    is stamped *into the envelope*, not enforced by the KV layer — this
    keeps the store backend-agnostic (SQLite has no native TTL) and
    lets ``load`` filter expired entries deterministically without
    needing a background sweeper.

    Construction-time validation:

    * ``ttl_seconds`` outside ``[60, 86400]`` falls back to the default
      and emits ``kv_dsl_ledger_store.ttl_invalid`` so misconfiguration
      surfaces in logs without aborting bootstrap (Requirement 8.3).
    * ``maxlen`` outside ``[1, 200]`` raises :class:`ValueError`
      eagerly — this knob bounds in-memory and on-disk size and a
      silent fallback would be a foot-gun (Requirement 8.10).

    Runtime invariants:

    * ``save`` trims to the most recent ``maxlen`` events before
      writing — over-budget callers (e.g. a temporarily oversize
      in-memory deque from a future migration) cannot bloat the row.
    * ``load`` skips rows / entries that fail JSON or schema
      validation, logging ``kv_dsl_ledger_store.record_corrupt`` per
      bad item. Returns events sorted oldest-first by ``occurred_at``
      so the dispatcher's rehydrate path can append directly to the
      session deque.
    * ``clear`` deletes exactly one ``(scope_id, file_id, key)`` triple
      — never wildcard across scopes.
    """

    def __init__(
        self,
        kv: KVStore,
        *,
        ttl_seconds: int = _DEFAULT_TTL,
        maxlen: int = _DEFAULT_MAXLEN,
    ) -> None:
        if not _TTL_MIN <= ttl_seconds <= _TTL_MAX:
            logger.warning(
                "kv_dsl_ledger_store.ttl_invalid",
                given=ttl_seconds,
                fallback=_DEFAULT_TTL,
                allowed_min=_TTL_MIN,
                allowed_max=_TTL_MAX,
            )
            ttl_seconds = _DEFAULT_TTL
        if not 1 <= maxlen <= _ABSOLUTE_MAXLEN:
            raise ValueError(
                f"maxlen out of range [1, {_ABSOLUTE_MAXLEN}]: {maxlen!r}"
            )
        self._kv = kv
        self._ttl = ttl_seconds
        self._maxlen = maxlen

    # ------------------------------------------------------------------ public

    async def save(
        self,
        scope_id: str,
        file_id: str,
        events: list[DslEvent],
    ) -> None:
        """Persist ``events`` for ``(scope_id, file_id)``, trimmed to ``maxlen``.

        The blob carries ``saved_at`` (epoch seconds at write time) and
        ``ttl`` (the configured TTL captured at construction) so a
        future ``load`` can age out events without consulting the
        clock at write time. ``events`` is taken as a snapshot — the
        caller is responsible for not mutating the list during this
        call (the dispatcher passes ``list(session.dsl_events)`` for
        exactly this reason).

        Requirement 8.10:writes are trimmed to the trailing
        ``maxlen`` entries (most recent wins) so a bloated in-memory
        deque cannot exceed the on-disk cap.
        """
        trimmed = list(events)[-self._maxlen :]
        payload = json.dumps(
            {
                "saved_at": time.time(),
                "ttl": self._ttl,
                "events": [self._to_dict(e) for e in trimmed],
            },
            ensure_ascii=False,
        )
        await self._kv.write(
            _LEDGER_SCOPE_PREFIX + "/" + scope_id,
            file_id or "_group",
            _EVENTS_KEY,
            payload,
        )

    async def load(
        self,
        scope_id: str,
        file_id: str,
    ) -> list[DslEvent]:
        """Return un-expired events for ``(scope_id, file_id)``, oldest first.

        Best-effort:any decoding / schema failure is logged via
        ``kv_dsl_ledger_store.record_corrupt`` and the offending entry
        skipped. An empty list is returned for "no row at all", which
        is the not-found / nothing-to-restore signal the dispatcher
        treats as "fresh ledger".

        Requirement 8.4 / 8.9:expired events (``occurred_at + ttl <
        now``) are dropped during decode so the dispatcher always
        sees a TTL-respecting view, even if the row hasn't been
        rewritten since the cutoff. Returned list is sorted by
        ``occurred_at`` ascending so callers can append directly to
        the session deque without re-sorting.
        """
        raw = await self._kv.read(
            _LEDGER_SCOPE_PREFIX + "/" + scope_id,
            file_id or "_group",
            _EVENTS_KEY,
            default=None,
        )
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "kv_dsl_ledger_store.record_corrupt",
                scope_id=scope_id,
                file_id=file_id,
                reason="json_decode",
            )
            return []
        if not isinstance(payload, dict):
            logger.warning(
                "kv_dsl_ledger_store.record_corrupt",
                scope_id=scope_id,
                file_id=file_id,
                reason="payload_not_dict",
            )
            return []
        ttl_raw = payload.get("ttl", self._ttl)
        ttl = ttl_raw if isinstance(ttl_raw, int | float) and ttl_raw > 0 else None
        items = payload.get("events", [])
        if not isinstance(items, list):
            logger.warning(
                "kv_dsl_ledger_store.record_corrupt",
                scope_id=scope_id,
                file_id=file_id,
                reason="events_not_list",
            )
            return []
        out: list[DslEvent] = []
        now = time.time()
        for item in items:
            if not isinstance(item, dict):
                logger.warning(
                    "kv_dsl_ledger_store.record_corrupt",
                    scope_id=scope_id,
                    file_id=file_id,
                    reason="item_not_dict",
                )
                continue
            try:
                ev = self._from_dict(item)
            except (KeyError, TypeError, ValueError):
                logger.warning(
                    "kv_dsl_ledger_store.record_corrupt",
                    scope_id=scope_id,
                    file_id=file_id,
                    reason="schema_mismatch",
                )
                continue
            if ttl is not None and ev.occurred_at + ttl < now:
                continue
            out.append(ev)
        # Requirement 8.4:return oldest-first so callers can append to
        # the deque without re-sorting; trim to ``maxlen`` keeps the
        # rehydrated deque honest in the unlikely event the on-disk
        # row predates a tighter ``maxlen`` config.
        out.sort(key=lambda e: e.occurred_at)
        return out[-self._maxlen :]

    async def clear(
        self,
        scope_id: str,
        file_id: str,
    ) -> None:
        """Delete the ledger row for exactly one ``(scope_id, file_id)`` pair.

        Implements the third leg of :class:`LedgerStore`. The Router's
        ``/reset`` calls this through
        :meth:`AgentChatDispatcher.clear_ledger`; the scope key is
        derived from :func:`linling_core.pipeline.ledger_scope_keys`
        so a group reset always targets ``"_group"`` and a DM reset
        always targets the sender id (Requirement 7.2 — never
        wildcard across scopes).
        """
        await self._kv.delete(
            _LEDGER_SCOPE_PREFIX + "/" + scope_id,
            file_id or "_group",
            _EVENTS_KEY,
        )

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _to_dict(e: DslEvent) -> dict[str, object]:
        """Serialise one :class:`DslEvent` to a JSON-safe dict.

        ``args`` is materialised as ``list`` because tuples don't
        round-trip through :func:`json.dumps`; everything else is
        already a primitive.
        """
        return {
            "timestamp": e.timestamp,
            "trigger": e.trigger,
            "args": list(e.args),
            "summary": e.summary,
            "outcome": e.outcome,
            "mode": e.mode,
            "actor_id": e.actor_id,
            "occurred_at": e.occurred_at,
        }

    @staticmethod
    def _from_dict(d: dict[str, object]) -> DslEvent:
        """Reconstruct a :class:`DslEvent` from a decoded dict.

        Tolerates older schemas:``mode`` and ``actor_id`` default
        to safe values when missing so a record written by an older
        process can still be loaded after a rolling upgrade. Strict
        type coercion (``str(...)`` / ``float(...)``) ensures the
        frozen dataclass receives the right types even if the JSON
        decoder produced a borderline form (e.g. an int where a
        float is expected).
        """
        # ``KeyError`` / ``TypeError`` / ``ValueError`` are caught at the
        # call site so we don't bother validating each field individually
        # here — fast happy-path coercion is what matters.
        args_raw = d.get("args") or []
        if not isinstance(args_raw, list):
            raise TypeError("args must be a list")
        return DslEvent(
            timestamp=str(d["timestamp"]),
            trigger=str(d["trigger"]),
            args=tuple(str(x) for x in args_raw),
            summary=str(d.get("summary", "")),
            outcome=str(d["outcome"]),
            mode=str(d.get("mode", "with_result")),
            actor_id=str(d.get("actor_id", "_unknown")),
            occurred_at=float(d["occurred_at"]),  # type: ignore[arg-type]
        )


__all__ = ["KVDslLedgerStore", "LedgerStore"]
