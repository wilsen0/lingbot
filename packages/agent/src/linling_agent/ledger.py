"""LLM-visible DSL action ledger renderer.

This module owns the *agent-side* half of the DSL Action Ledger
feature: turning a session's :class:`~linling_core.pipeline.DslEvent`
deque into a single, transient ``role="system"`` :class:`Message`
shaped like ``<recent_user_actions>...</recent_user_actions>`` and
injected immediately before the user input on each LLM call.

The rendered :class:`Message` is **never** appended to
``Session.history`` and **never** persisted via ``KVHistoryStore`` —
those invariants are protected by the dispatcher, not by this module.
This module only owns: filtering, oldest-first truncation accounting,
XML 1.0 well-formedness, and deterministic output.

Cross-cutting invariants enforced here:

* Only ``outcome == "ok"`` events are visible to the LLM
  (Requirement 1.9 / 2.2).
* Empty / fully-filtered ledger → ``None`` (Requirement 1.12 / 4.10 /
  11.7), so the dispatcher knows to skip injection rather than emit a
  vacuous ``<recent_user_actions/>`` block.
* When the rendered frame would exceed ``Total_Char_Budget``, the
  oldest events are dropped one-by-one until it fits, with a
  ``<truncated count="N"/>`` line accounting for what was omitted
  (Requirement 4.6 / 4.7 / 4.8). If everything is dropped, the
  renderer returns ``None`` rather than an empty truncation marker
  (Requirement 4.9).
* All string fields are XML-escaped via ``xml.sax.saxutils`` *and*
  scrubbed of XML-1.0-illegal control characters (which cannot be
  escaped — only stripped or replaced) so the final
  ``Message.content`` parses under an XML 1.0 parser without errors
  (Requirement 11.6).
* The output is deterministic: identical input deque ⇒ byte-identical
  output (Requirement 11.4).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from xml.sax.saxutils import escape, quoteattr

from linling_core.pipeline import DslEvent

from linling_agent.llm import Message

__all__ = ["LedgerRenderer"]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The wrapping tags. Defined as module constants so tests can assert
# against them without re-typing the string and risking drift.
_OPEN = "<recent_user_actions>"
_CLOSE = "</recent_user_actions>"

# Range guards for ``total_char_budget``. Mirrors Requirement 4.5.
_BUDGET_MIN = 200
_BUDGET_MAX = 8000

# Sentinel sender id; produced by ``LedgerWriter`` when ``event.sender.id``
# is missing. Renderer must never emit this as a ``by="..."`` value.
_UNKNOWN_ACTOR = "_unknown"

# XML 1.0 forbids most C0 control characters outright — they cannot be
# expressed even as numeric character references and an XML parser will
# reject them. The legal set is:
#   #x09 (tab) | #xA (LF) | #xD (CR) | #x20-#xD7FF | #xE000-#xFFFD
#   | #x10000-#x10FFFF
# Everything else (including #x00-#x08, #x0B-#x0C, #x0E-#x1F, surrogates,
# and #xFFFE/#xFFFF non-characters) is replaced with U+FFFD before the
# saxutils escapers see the string. This guarantees Requirement 11.6's
# "parses under XML 1.0" invariant even when handler output, KV-restored
# data, or trigger metadata accidentally carries a control character.
_XML_INVALID_RE = re.compile(
    "[^\u0009\u000a\u000d\u0020-\ud7ff\ue000-\ufffd"
    "\U00010000-\U0010ffff]",
)


def _xml_safe(text: str) -> str:
    """Replace XML-1.0-illegal characters with U+FFFD.

    The standard saxutils ``escape`` / ``quoteattr`` helpers handle the
    five canonical metacharacters but leave control characters alone —
    those are illegal in XML 1.0 even as numeric character references,
    so we substitute the Unicode replacement character. Legal text is
    returned unchanged in the common case (the regex matches no chars
    and ``re.sub`` short-circuits).
    """
    return _XML_INVALID_RE.sub("\ufffd", text)


# ---------------------------------------------------------------------------
# LedgerRenderer
# ---------------------------------------------------------------------------


class LedgerRenderer:
    """Render a session's DSL action ledger as a transient system :class:`Message`.

    The renderer is a pure function of its inputs: ``__init__``
    parameters fix the budget and whether the group-style ``by="..."``
    actor attribute is emitted, and ``render`` reads the supplied
    events without mutation. No IO, no audit-sink access, no chat
    history access.

    Parameters:
        total_char_budget: Maximum number of Unicode code points the
            final ``Message.content`` (including the wrapping
            ``<recent_user_actions>`` tags and the optional
            ``<truncated count="N"/>`` line) may occupy. Must be in
            the inclusive range ``[200, 8000]``; values outside this
            range raise :class:`ValueError` instead of silently
            clamping. Defaults to 800.
        include_actor: When ``True``, every rendered ``<action ...>``
            line carries a ``by="<actor_id>"`` attribute (used in group
            scope so the LLM can attribute actions to specific
            members). When ``False`` (DM scope default), the attribute
            is suppressed entirely — even if the underlying
            :class:`DslEvent` carries an ``actor_id``. The toggle is
            captured on the renderer instance; flipping it requires
            constructing a new renderer (see Task 7.2 for the
            ``with_actor`` factory helper).

    Raises:
        ValueError: If ``total_char_budget`` is outside ``[200, 8000]``.
    """

    __slots__ = ("_budget", "_include_actor")

    def __init__(
        self,
        *,
        total_char_budget: int = 800,
        include_actor: bool = False,
    ) -> None:
        if not _BUDGET_MIN <= total_char_budget <= _BUDGET_MAX:
            raise ValueError(
                f"total_char_budget out of range [{_BUDGET_MIN}, {_BUDGET_MAX}]"
            )
        self._budget = total_char_budget
        self._include_actor = bool(include_actor)

    # ------------------------------------------------------------------ public

    def render(self, events: Iterable[DslEvent]) -> Message | None:
        """Render the ledger as a system :class:`Message`, or ``None``.

        Behaviour matrix:

        * Empty input or every event has ``outcome == "error"`` → return
          ``None`` (Requirement 1.12 / 4.10 / 11.7).
        * All visible events fit within ``total_char_budget`` → return a
          ``Message`` whose ``content`` is the wrapped block with one
          ``<action ...>`` line per visible event in original order.
        * Some visible events would push the frame over budget → drop
          the oldest events one-by-one and emit a single ``<truncated
          count="N"/>`` line tallying how many were dropped, before any
          retained events.
        * Every visible event would have to be dropped to satisfy the
          budget → return ``None`` (Requirement 4.9). The renderer
          never emits a frame that contains only the truncation marker
          and no actions.

        The returned :class:`Message` always has ``role == "system"``
        (Requirement 11.1) and ``content`` that starts with
        ``<recent_user_actions>`` and ends with
        ``</recent_user_actions>`` with each tag occurring exactly once
        (Requirement 11.2). No leading or trailing whitespace surrounds
        either tag.

        Two calls with identical inputs produce byte-identical
        ``Message.content`` under UTF-8 (Requirement 11.4) — the
        renderer relies on Python's ``str`` ordering of the supplied
        iterable and the deterministic ``xml.sax.saxutils`` escapers.

        Parameters:
            events: Any iterable of :class:`DslEvent`. Typically this
                is the live ``session.dsl_events`` deque ordered
                oldest-first, but any iterable is accepted; the
                renderer never mutates it. Iteration happens once.

        Returns:
            A new :class:`Message` instance, or ``None`` when there is
            nothing visible to inject.
        """
        # Requirement 1.9 / 2.2: only outcome == "ok" events are LLM-visible.
        # Materialise into a list because we need to walk it multiple
        # times (frame size loop) and the caller may have handed us a
        # generator.
        visible = [e for e in events if e.outcome == "ok"]
        if not visible:
            # Requirement 1.12 / 4.10 / 11.7: nothing to inject.
            return None

        # Requirement 4.6 / 4.7 / 4.8: drop oldest events until the
        # framed content (including wrapping tags and the optional
        # ``<truncated count="N"/>`` line) fits the budget. We keep the
        # most-recent events because "recent_user_actions" is meant to
        # surface the latest context to the LLM; older events are
        # accounted for via the ``count`` attribute.
        kept = list(visible)
        omitted = 0
        while True:
            content = self._frame(kept, omitted)
            if len(content) <= self._budget:
                break
            if not kept:
                # Defensive: even the truncation-only frame doesn't
                # fit. Never happens for any in-range budget but
                # guards against future budget shrinks.
                return None
            kept.pop(0)
            omitted += 1

        # Requirement 4.9: if every visible event was dropped to fit
        # the budget, refuse to emit a frame that contains only the
        # ``<truncated count="N"/>`` marker. The dispatcher treats
        # ``None`` as "skip injection entirely". This check sits
        # *after* the loop because the truncation-only frame is small
        # enough to satisfy the budget on its own — without this guard
        # we'd happily emit a vacuous block.
        if not kept:
            return None

        return Message(role="system", content=content)

    def with_actor(self, flag: bool) -> LedgerRenderer:
        """Return a renderer whose ``include_actor`` matches ``flag``.

        The chat dispatcher needs to flip the actor-emission policy
        between DM (``include_actor=False``) and group
        (``include_actor=True``) scopes on a per-call basis. Allocating
        a fresh :class:`LedgerRenderer` on every dispatch in the hot
        path is wasteful; on the other hand, mutating ``_include_actor``
        in place would break the "renderer is a pure function of its
        ``__init__`` parameters" invariant other call-sites rely on.

        This factory threads the needle: when the requested ``flag``
        already matches the current setting, ``self`` is returned
        unchanged (zero allocation, the common case for any given
        scope kind once the dispatcher has cached the right renderer);
        otherwise a *new* renderer is constructed with the same
        budget but the flipped actor flag. The originally configured
        renderer is never mutated, so callers may safely cache and
        share renderers across dispatches.

        Parameters:
            flag: Desired ``include_actor`` value. ``True`` for group
                scope (emit ``by="<actor_id>"`` when actor is known),
                ``False`` for DM scope (suppress the attribute
                entirely).

        Returns:
            ``self`` if ``bool(flag) == self._include_actor``;
            otherwise a fresh :class:`LedgerRenderer` carrying the
            same ``total_char_budget`` and the requested
            ``include_actor`` value.
        """
        wanted = bool(flag)
        if wanted == self._include_actor:
            return self
        return LedgerRenderer(
            total_char_budget=self._budget,
            include_actor=wanted,
        )

    # ------------------------------------------------------------------ helpers

    def _frame(self, kept: list[DslEvent], omitted: int) -> str:
        """Compose the full ``<recent_user_actions>`` block as a string.

        The frame layout is::

            <recent_user_actions>
              <truncated count="N"/>          # only when omitted > 0
              <action time=... trigger=... .../>
              <action time=... trigger=... .../>
              ...
            </recent_user_actions>

        Requirement 11.2 mandates that the open tag is the very first
        thing in ``content`` and the close tag the very last, with no
        surrounding whitespace. Requirement 4.7 mandates the truncation
        marker — when present — appears *before* any retained
        ``<action>`` lines. Indentation between lines is purely cosmetic
        and is part of the rendered output (parsers ignore it).
        """
        lines: list[str] = [_OPEN]
        if omitted > 0:
            # ``omitted`` is always a non-negative int from our own
            # control flow, so a numeric formatter cannot inject XML
            # special chars; no escape needed.
            lines.append(f'  <truncated count="{omitted}"/>')
        for ev in kept:
            lines.append("  " + self._render_event(ev))
        lines.append(_CLOSE)
        return "\n".join(lines)

    def _render_event(self, ev: DslEvent) -> str:
        """Serialise a single :class:`DslEvent` as one ``<action .../>`` line.

        Attribute order is fixed (``time``, ``trigger``, ``args``,
        ``by``, ``summary``) so render output is deterministic given
        equal inputs (Requirement 11.4). All attribute values are
        escaped via :mod:`xml.sax.saxutils` so the resulting line is
        well-formed XML 1.0 even when the underlying string fields
        contain special characters (Requirement 11.6).

        Field-by-field rules:

        * ``time``: ``ev.timestamp`` is always an "HH:MM:SS" literal so
          escaping is harmless but defensively applied.
        * ``trigger``: ``quoteattr`` picks the safer of single/double
          quotes and emits ``&quot;``/``&apos;`` as needed.
        * ``args``: omitted entirely when the tuple is empty
          (deterministic — empty args ⇒ no attribute, never
          ``args=""``). When present, individual elements are
          space-joined; ``"`` inside any element is escaped to
          ``&quot;`` so the surrounding double quotes still form a
          valid attribute value (Requirement 11.6). Note that
          space-joining is intentionally lossy w.r.t. argument
          boundaries — the spec does not mandate per-arg round-trip
          fidelity, only well-formed XML.
        * ``by``: emitted only when ``include_actor`` is ``True`` *and*
          the actor is neither empty nor the ``_unknown`` sentinel
          (Requirement 6.6 — never emit ``by=""`` or ``by="None"``).
        * ``summary``: emitted only for ``mode == "with_result"`` events
          whose summary is non-empty (Requirement 5.3 / 5.4 / 5.5).
          Trigger-only events therefore never carry a ``summary``
          attribute, regardless of any stale value the writer might
          have left in the field.
        """
        attrs: list[str] = [
            # ``time`` carries no special chars in practice but we still
            # escape so a malformed (manually constructed) event can't
            # poison the rendered XML. ``_xml_safe`` strips control
            # characters, ``escape`` covers ``< > &``.
            f'time="{escape(_xml_safe(ev.timestamp))}"',
            # ``quoteattr`` returns a fully quoted string (its own
            # quotes), so we *do not* wrap it in extra ``"`` — that
            # would double-quote the value and produce invalid XML.
            f"trigger={quoteattr(_xml_safe(ev.trigger))}",
        ]

        if ev.args:
            # Escape ``<``, ``>``, ``&`` AND ``"`` (the surrounding
            # quote character of the attribute value) per Requirement
            # 11.6. ``'`` inside a ``"``-quoted attribute is legal as
            # a literal, so we don't escape it. ``_xml_safe`` strips
            # any control characters before escaping.
            joined = " ".join(escape(_xml_safe(a), {'"': "&quot;"}) for a in ev.args)
            attrs.append(f'args="{joined}"')

        # Requirement 6.6: actor attribute is only emitted in group
        # scope (caller toggles ``include_actor``), and never as
        # ``by=""`` or ``by="_unknown"`` for events whose actor was
        # missing.
        if (
            self._include_actor
            and ev.actor_id
            and ev.actor_id != _UNKNOWN_ACTOR
        ):
            attrs.append(f"by={quoteattr(_xml_safe(ev.actor_id))}")

        # Requirement 5.3: trigger_only events never expose a summary.
        # Requirement 5.4 / 5.5: with_result events expose summary only
        # when it is a non-empty string. ``ev.summary`` may legally be
        # ``""`` for either mode (writer scrubs it on error / trigger-
        # only paths) so the empty-string check is the load-bearing
        # filter here.
        if ev.mode == "with_result" and ev.summary:
            attrs.append(f"summary={quoteattr(_xml_safe(ev.summary))}")

        return f"<action {' '.join(attrs)}/>"
