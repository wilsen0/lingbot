"""Runtime state container shared across routers and WS handlers.

`WebUIState` is attached to `app.state.runtime`. It collects the pluggable
integrations (KV store, event bus, scheduler, agent registry, rule router,
audit reader). All fields are optional so that the WebUI can start with
only some subsystems wired — useful for the dev build and for piecewise
testing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from linling_webui.auth import AuthStore
from linling_webui.buffers import EventRingBuffer

if TYPE_CHECKING:
    from linling_agent.bridge import AgentRegistry
    from linling_core.bus import EventBus
    from linling_core.scheduler import Scheduler
    from linling_core.storage.kv import KVStore


@dataclass(frozen=True)
class TriggerInfo:
    """One DSL trigger surfaced to the UI's inline-suggest panel.

    ``raw`` is the trigger source (regex or literal) as the bot
    actually matches it. ``label`` is a cleaned-up display form
    (``([0-9]+)`` → ``…``) — see :func:`linling_core.router.clean_trigger_label`.
    ``has_args`` flags triggers that need the user to type something
    after the literal prefix; the UI uses it to leave the cursor
    parked at the placeholder rather than auto-sending.
    ``literal_prefix`` is the longest leading literal substring of
    ``label`` (everything before the first ``…``); the composer
    panel uses it to pre-fill the input on click without forcing
    the user into a still-not-matchable regex shape.
    """

    raw: str
    label: str
    has_args: bool = False
    literal_prefix: str = ""


# Resolves the current set of matchable triggers for one agent. Called
# at request time, *not* at registration, so hot-reload picks up the
# new ruleset on the very next ``/api/agents/{name}/triggers`` poll.
TriggerProvider = Callable[[], list[TriggerInfo]]


@dataclass(frozen=True)
class WebChatSegment:
    """One piece of a rich chat reply.

    ``kind`` is ``"text"`` or ``"image"``. Text segments carry their
    content in ``text``; image segments carry a (browser-fetchable)
    URL in ``url`` plus optional ``alt`` for accessibility. Mixing
    the two in a single reply is QQ's native shape — a chat bubble
    can interleave text lines and pictures, which the QRDic ruleset
    exercises constantly (e.g. ``我的灵玉`` emits "0\n" then a
    rank-tier badge image).
    """

    kind: str
    text: str = ""
    url: str = ""
    alt: str = ""


@dataclass(frozen=True)
class WebChatReply:
    """Result of a single WebUI chat dispatch.

    Aggregates everything the ``/ws/agents/*/stream`` endpoint needs
    to surface to the client. ``content`` is the plain-text fallback
    (concatenation of every text segment) — kept so callers that
    only care about text don't have to reduce ``segments`` themselves.
    ``source`` records who actually answered (``"dsl"`` for a matched
    ``.ling`` handler, ``"agent"`` for an LLM round-trip, ``"empty"``
    when neither produced output).
    """

    content: str
    tool_calls_made: int = 0
    total_tokens: int = 0
    source: str = "agent"
    segments: tuple[WebChatSegment, ...] = ()


# Dispatcher signature: the WebUI hands us (input_text, user_id, scope_override)
# and expects a fully-resolved :class:`WebChatReply`. The implementation
# (in ``linling_cli.wire_webui``) runs DSL classifier + handler first,
# falling back to the LLM agent only when no command matched.
# ``scope_override`` lets the caller pin a specific group id to test
# in (defaults to the bot's configured ``main_group``); pass ``None``
# to use the configured default.
WebChatDispatcher = Callable[[str, str, str | None], Awaitable[WebChatReply]]


@dataclass
class BotInfo:
    """Metadata about a connected bot (platform adapter)."""

    id: str
    platform: str = "unknown"
    name: str = ""
    online: bool = False
    last_event_at: float | None = None  # epoch seconds


@dataclass
class WebUIState:
    """Shared runtime state for the WebUI process."""

    auth: AuthStore
    event_buffers: dict[str, EventRingBuffer] = field(default_factory=dict)
    bots: dict[str, BotInfo] = field(default_factory=dict)
    kv_stores: dict[str, KVStore] = field(default_factory=dict)
    bus: EventBus | None = None
    scheduler: Scheduler | None = None
    agent_registry: AgentRegistry | None = None
    hot_reload_callback: Any = None  # async callable(bot_id) -> ReloadResult
    audit: Any = None  # AuditReader (set lazily)
    # Per-bot rule-file controller used by ``/api/rules/files*``. The
    # controller hides the bot's filesystem layout from the WebUI
    # router so we don't leak paths in and out of the HTTP layer.
    rule_files: dict[str, Any] = field(default_factory=dict)
    # Per-agent web chat dispatcher. Wired by ``attach_bot_to_webui``;
    # the ``/ws/agents/*/stream`` endpoint prefers this over a raw
    # ``runtime.invoke`` call so user input flows through the bot's
    # DSL classifier first and only falls back to the LLM when no
    # handler matched. Tests that don't go through ``attach_bot_to_webui``
    # will simply leave this empty and the WS endpoint degrades to the
    # legacy direct-invoke behaviour.
    chat_dispatchers: dict[str, WebChatDispatcher] = field(default_factory=dict)
    # Per-agent trigger provider. Populated by ``attach_bot_to_webui``
    # (closure over ``bot.classifier``) so the inline-suggest endpoint
    # can list a bot's matchable DSL triggers without the WebUI
    # depending on ``linling_cli`` directly. Resolved at request time
    # so hot-reload immediately reflects in the picker.
    trigger_providers: dict[str, TriggerProvider] = field(default_factory=dict)

    # Per-bot event buffer, lazy-created.
    def buffer_for(self, bot_id: str, *, capacity: int) -> EventRingBuffer:
        buf = self.event_buffers.get(bot_id)
        if buf is None:
            buf = EventRingBuffer(capacity=capacity, bot_id=bot_id)
            self.event_buffers[bot_id] = buf
        return buf

    def register_bot(self, info: BotInfo) -> None:
        self.bots[info.id] = info

    def visible_bots(self, allowed: list[str] | None) -> list[BotInfo]:
        """Filter the bot list by jwt.bots. ``None`` means "all" (superadmin)."""
        if allowed is None:
            return list(self.bots.values())
        return [b for b in self.bots.values() if b.id in set(allowed)]
