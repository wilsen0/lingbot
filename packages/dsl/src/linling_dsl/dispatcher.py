"""DSL command dispatcher — adapter between :class:`Router` and :class:`VM`.

Implements :class:`linling_core.CommandDispatcher`. A single instance is
cheap and thread-safe (the VM itself is constructed per call).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from linling_core.events import Action, Event
from linling_core.segments import Segment, TextSegment

from linling_dsl.vm import VM

if TYPE_CHECKING:
    from linling_core.classifier import HandlerMatch
    from linling_core.pipeline import Session
    from linling_core.storage.kv import KVStore
    from linling_core.tools import ToolRegistry

    from linling_dsl.ledger import LedgerWriter


class DslCommandDispatcher:
    """Runs a DSL handler and turns the resulting segments into actions."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        kv: KVStore,
        bot_id: str = "linling",
        max_steps: int = 10_000,
        max_output_segments: int = 20,
        timeout_ms: int = 2_000,
        extras: dict[str, Any] | None = None,
        ledger_writer: LedgerWriter | None = None,
    ) -> None:
        self._registry = registry
        self._kv = kv
        self._bot_id = bot_id
        self._max_steps = max_steps
        self._max_output_segments = max_output_segments
        self._timeout_ms = timeout_ms
        # Forwarded into every per-call VM. Bootstrap stuffs the
        # scheduler / adapter list here so DSL tools (``$调用$``,
        # ``$发送$``) can reach the runtime without circular imports.
        self._extras: dict[str, Any] = dict(extras or {})
        # Optional DSL Action Ledger writer. ``None`` short-circuits
        # every ledger code path so existing call sites and tests run
        # unchanged (Requirement 1.7).
        self._ledger = ledger_writer

    def update_extras(self, **kwargs: Any) -> None:
        """Mutate the extras forwarded to every VM invocation.

        Used by the bootstrap to plug in side-channels that aren't yet
        constructed when the dispatcher is built (e.g. the adapter
        sink, which depends on the adapter list and routes outbound
        messages from ``$发送$``).
        """
        self._extras.update(kwargs)

    @property
    def ledger_writer(self) -> LedgerWriter | None:
        """Read-only handle to the configured DSL Action Ledger writer.

        Exposed so the bootstrap's hot-reload path can carry the
        same writer over when it constructs a fresh dispatcher
        bound to a newly compiled :class:`Script` — without forcing
        a full re-construction of the writer (whose budget /
        ``Global_Default_Expose`` knobs are immutable post-
        construction).
        """
        return self._ledger

    async def run(self, event: Event, match: HandlerMatch, session: Session) -> list[Action]:
        vm = VM(
            tool_registry=self._registry,
            kv=self._kv,
            bot_id=self._bot_id,
            max_steps=self._max_steps,
            max_output_segments=self._max_output_segments,
            timeout_ms=self._timeout_ms,
            extras=self._extras,
        )
        try:
            result = await vm.execute_handler(match.handler, event, captures=match.captures)
        except Exception:
            # Requirement 2.3:append the ``outcome="error"`` event
            # *before* re-raising so the LLM-visible debug surface
            # records the failed attempt; we deliberately do not
            # swallow or wrap the exception, the Router's ``_safe``
            # wrapper observes it and emits the friendly fallback.
            if self._ledger is not None:
                self._ledger.append(
                    session=session,
                    handler=match.handler,
                    captures=match.captures,
                    raw_summary="",
                    outcome="error",
                    event=event,
                )
            raise
        if self._ledger is not None:
            # Requirement 1.5:raw_summary is the in-order, no-separator
            # concatenation of every ``TextSegment.text`` in
            # ``result.segments`` — non-text segments are ignored at
            # this stage; the LedgerWriter handles truncation.
            raw_summary = "".join(s.text for s in result.segments if isinstance(s, TextSegment))
            self._ledger.append(
                session=session,
                handler=match.handler,
                captures=match.captures,
                raw_summary=raw_summary,
                outcome="ok",
                event=event,
            )
        if not result.segments:
            return []
        return [_segments_to_action(event, result.segments)]


def _segments_to_action(event: Event, segments: list[Segment]) -> Action:
    """Collapse a handler's output segments into a single reply action.

    Text fragments are merged (the VM emits one per line); image /
    voice / reply / card segments pass through. Adapters that can't
    render a given segment type fall back at their own layer.

    QQ-side OneBot expects ``ReplySegment`` to appear first in the
    message array — most forks tolerate it elsewhere but a few
    (Lagrange in particular) silently drop late replies. We hoist
    the *first* ReplySegment to the head of the list to be safe;
    multiple replies in one message are unusual but if they appear
    we keep declaration order for the rest.
    """
    from linling_core.segments import ReplySegment  # noqa: PLC0415

    merged: list[Segment] = []
    buffered_text: list[str] = []

    def flush() -> None:
        if buffered_text:
            merged.append(TextSegment(text="".join(buffered_text)))
            buffered_text.clear()

    for seg in segments:
        if isinstance(seg, TextSegment):
            buffered_text.append(seg.text)
        else:
            flush()
            merged.append(seg)
    flush()

    # Hoist a stray late ReplySegment to the head, OneBot convention.
    for i, s in enumerate(merged):
        if isinstance(s, ReplySegment):
            if i != 0:
                merged.insert(0, merged.pop(i))
            break

    return Action(kind="reply", target=event.scope, segments=merged)
