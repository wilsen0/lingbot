"""Message classifier.

Decides whether an incoming :class:`Event` should be treated as a
**command** (served by a DSL handler) or a **chat** message (served by
the configured agent). Also handles an ``ignore`` verdict for filtered
scopes / senders.

Routing policy (KISS, configurable):

1. Messages from the bot itself are ignored.
2. ``block_scope_ids`` / ``block_sender_ids`` → ``ignore``.
3. If the text starts with one of ``command_prefixes`` (default ``/``,
   ``!``), try matching the stripped text against DSL handlers; miss
   → still classify as ``command`` but with ``match=None`` so the router
   can produce a friendly "unknown command" reply rather than handing
   the raw string to the LLM.
4. Otherwise, try DSL handlers against the full text (QRDic style
   ``我的灵玉`` etc.). A match classifies as ``command``.
5. Nothing matched → ``chat``.

Exactly one DSL handler is returned per classification. The first
handler that matches wins; within equal matches the declaration order
is preserved (parser order = QRDic source order).

Match performance: triggers are partitioned at compile time into

* a ``literal_index`` — exact-match lookup keyed by the trigger text,
  serving any trigger that contains no regex metacharacters (the bulk
  of QRDic triggers, e.g. ``背包`` / ``查看消息`` / ``[戳一戳]``).
* a ``regex_list`` — patterns that need ``fullmatch``.

Literal candidates are dispatched as a single dict access; regex
candidates are walked in declaration order. For the migrated 453
handler dicpro.txt this drops the per-event match cost from O(453)
``re.fullmatch`` calls to typically O(1) + O(small_regex_subset).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from re import Pattern
from typing import TYPE_CHECKING, Literal

from linling_core.events import Event

if TYPE_CHECKING:
    from linling_dsl.ast_nodes import Handler, Script


# The set of prefixes that unconditionally mark a message as a command
# attempt. Configured at classifier construction; defaults cover both
# Slack-style (``/``) and Unix-style (``!``) conventions.
DEFAULT_COMMAND_PREFIXES: tuple[str, ...] = ("/", "!")

# Regex meta-characters that *actually drive runtime behaviour*. A
# trigger that contains none of these is matched as a literal string
# equality (single dict lookup). We deliberately exclude ``[`` and
# ``]`` here because QRSpeed-era rule files use ``[X]``-bracketed
# strings (``[戳一戳]`` / ``[系统]`` / ``[内部]接扔瓶子``) as plain
# triggers — not character classes. Treating them as character
# classes was a long-standing bug that made every bracket trigger
# match a single arbitrary CJK char inside the brackets and miss the
# literal text.
_DYNAMIC_REGEX_META = frozenset(r".^$*+?{}()|\\")


def _is_literal_trigger(trigger: str) -> bool:
    """True iff ``trigger`` can be matched as a plain string equality.

    QRDic triggers like ``背包`` / ``[戳一戳]`` / ``查看消息`` are pure
    text — most of the ruleset, in fact. Only a small subset uses
    ``([0-9]+)`` style captures, and those go through the regex path.

    ``[戳一戳]``-shaped bracketed triggers are a QRSpeed convention
    *not* a regex character class. We detect them by looking for any
    "dynamic" meta char (``.^$*+?{}()|\\``) — if none are present,
    the ``[`` / ``]`` are decorative and the trigger is a literal.
    """
    return not any(ch in _DYNAMIC_REGEX_META for ch in trigger)


@dataclass(frozen=True)
class HandlerMatch:
    """A DSL handler that matched an event's text."""

    handler: Handler
    captures: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Intent:
    """Classification result for a single event."""

    kind: Literal["command", "chat", "ignore"]
    match: HandlerMatch | None = None
    reason: str = ""


@dataclass(frozen=True)
class _CompiledTrigger:
    """A handler with its compiled regex; ``None`` pattern marks an
    uncompilable trigger that will be skipped at match time."""

    handler: Handler
    pattern: Pattern[str] | None


class MessageClassifier:
    """Stateless classifier; compiles DSL triggers once at construction.

    Stateless means *thread- and coroutine-safe* — multiple routers can
    share one classifier. When the ruleset is reloaded, construct a new
    classifier; cheap (~a few hundred regexes).
    """

    def __init__(
        self,
        script: Script | None,
        *,
        command_prefixes: tuple[str, ...] = DEFAULT_COMMAND_PREFIXES,
        block_scope_ids: frozenset[str] | None = None,
        block_sender_ids: frozenset[str] | None = None,
    ) -> None:
        self._prefixes = tuple(sorted(command_prefixes, key=len, reverse=True))
        self._block_scopes = block_scope_ids or frozenset()
        self._block_senders = block_sender_ids or frozenset()

        # Triggers are split into literal-keyed and regex-keyed lookups
        # to keep the per-event match path off a 400-entry regex walk.
        # Declaration order is preserved within each bucket so legacy
        # rules with overlapping triggers still resolve as in QRDic.
        self._all_triggers: list[str] = []
        self._literal_index: dict[str, Handler] = {}
        self._regex_list: list[_CompiledTrigger] = []
        if script is not None:
            for h in script.handlers:
                if h.is_internal:
                    # Internal handlers are jump/call targets, never
                    # triggered by user text.
                    continue
                trigger = h.trigger.strip()
                if not trigger:
                    continue
                self._all_triggers.append(h.trigger)
                if _is_literal_trigger(trigger):
                    # First-declaration-wins: a later duplicate in the
                    # source wouldn't be reachable in the regex path
                    # either (linear scan would stop at the first hit),
                    # so we mirror that behaviour here.
                    self._literal_index.setdefault(trigger, h)
                    continue
                pattern = _safe_compile(trigger)
                if pattern is not None:
                    self._regex_list.append(_CompiledTrigger(h, pattern))

    # ------------------------------------------------------------------ public

    def list_triggers(self) -> list[str]:
        """Return the user-visible trigger text for every matchable handler.

        Useful for generating ``/help``. Triggers that failed to compile
        as regex (``_safe_compile`` returned ``None``) are still
        included — they survive in the ruleset because they may match
        under the lenient parser even if strict regex compilation would
        reject them. The caller decides whether to format them.
        """
        return list(self._all_triggers)

    def command_prefixes(self) -> tuple[str, ...]:
        """Prefix strings that mark a message as an explicit command."""
        return self._prefixes

    def classify(self, event: Event) -> Intent:
        """Decide what to do with an event."""
        # Only message events participate in command/chat routing; notices,
        # requests, and system events are ignored by default. Adapters that
        # want to translate e.g. pokes into commands should do so upstream
        # by emitting a synthetic ``message`` event.
        if event.kind != "message":
            return Intent(kind="ignore", reason="non-message-event")

        # The bot's own outbound echoes must never loop back in.
        if event.sender.id == event.bot_id:
            return Intent(kind="ignore", reason="self-message")

        if event.scope.id in self._block_scopes:
            return Intent(kind="ignore", reason="blocked-scope")
        if event.sender.id in self._block_senders:
            return Intent(kind="ignore", reason="blocked-sender")

        text = event.match_text

        # Commands marked with a prefix always resolve to a command
        # verdict, even on miss — that's how users discover typos.
        prefix = self._strip_prefix(text)
        if prefix is not None:
            stripped, used = prefix
            match = self._match_dsl(stripped)
            if match is not None:
                return Intent(kind="command", match=match, reason=f"prefix:{used}")
            return Intent(kind="command", match=None, reason=f"prefix-unknown:{used}")

        # No prefix — try a DSL match anyway for QRDic compatibility.
        match = self._match_dsl(text)
        if match is not None:
            return Intent(kind="command", match=match, reason="implicit-trigger")

        # Synthetic adapter events (e.g. OneBot's ``[系统]`` /
        # ``[退群]`` translations) carry an ``_synthetic_qrspeed``
        # marker on ``event.raw``. If we got here without a match,
        # the rule set doesn't define the handler — silently ignore
        # rather than ship the literal bracket string to the chat
        # agent. Operators who *want* their LLM to see "[系统]" can
        # always emit a regular text message themselves.
        if event.raw.get("_synthetic_qrspeed"):
            return Intent(kind="ignore", reason="synthetic-no-handler")

        # Everything else is chat.
        return Intent(kind="chat", reason="fallback")

    # ------------------------------------------------------------------ helpers

    def _strip_prefix(self, text: str) -> tuple[str, str] | None:
        for p in self._prefixes:
            if text.startswith(p):
                return text[len(p) :].lstrip(), p
        return None

    def _match_dsl(self, text: str) -> HandlerMatch | None:
        if not text:
            return None
        # Fast path: literal lookup. ~95% of QRDic triggers are
        # plain text, so this dict access usually wins.
        literal_match = self._literal_index.get(text)
        if literal_match is not None:
            return HandlerMatch(handler=literal_match, captures=[])
        # Fallback: regex walk over the small minority of triggers
        # with capture groups or alternation.
        for t in self._regex_list:
            assert t.pattern is not None  # guaranteed by constructor
            m = t.pattern.fullmatch(text)
            if m is not None:
                return HandlerMatch(handler=t.handler, captures=list(m.groups()))
        return None


def _safe_compile(trigger: str) -> Pattern[str] | None:
    """Compile a DSL trigger; return ``None`` if the regex is malformed.

    QRDic triggers are the lines users literally typed, so they can
    contain stray ``\\n`` / unescaped metacharacters. We strip trailing
    whitespace and try; if it still fails, we skip that handler instead
    of crashing the whole classifier.
    """
    trigger = trigger.strip()
    if not trigger:
        return None
    try:
        return re.compile(trigger)
    except re.error:
        return None
