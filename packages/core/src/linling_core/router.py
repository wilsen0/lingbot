"""Router — the glue between events, DSL, and agents.

One :class:`Router` instance serves one bot. It owns:

* a :class:`MessageClassifier` (shared with bot-level rule reloading),
* a :class:`ConversationStore` (per-bot session state),
* two pluggable dispatchers: a ``CommandDispatcher`` (runs DSL handlers)
  and a ``ChatDispatcher`` (invokes an agent),
* a global :class:`asyncio.Semaphore` acting as backpressure.

Why core does **not** import :mod:`linling_dsl` or :mod:`linling_agent`:
those packages depend on core, not the other way around. Instead the
router accepts *protocols*. The wiring layer (e.g. ``linling`` CLI, bot
bootstrap in tests) constructs concrete dispatchers by closing over a
``VM`` and an ``AgentRuntime`` respectively. This is the same separation
that keeps :class:`EventBus` independent of adapters.

Failure policy: every dispatcher error is caught, logged structured,
turned into a friendly user-visible ``Action``, and does not mark the
event processed for the bus's purposes. Lock/limit exhaustion is also
handled here, not deep inside an agent.
"""

from __future__ import annotations

import asyncio
import contextvars
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import structlog

from linling_core.audit import AuditEntry, AuditSink, NullAuditSink
from linling_core.classifier import HandlerMatch, Intent, MessageClassifier
from linling_core.events import Action, Event
from linling_core.metrics import (
    ACTIVE_SESSIONS,
    DISPATCH_DURATION_SECONDS,
    ROUTER_DUPLICATES_TOTAL,
    ROUTER_EVENTS_TOTAL,
    SINK_FAILURES_TOTAL,
    MetricsSink,
    NullMetrics,
)
from linling_core.pipeline import ConversationKey, ConversationStore, SeenEvents, Session
from linling_core.segments import TextSegment

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Trace-id context
# ---------------------------------------------------------------------------

# Current request's trace id. Logs and audit entries share this value so
# an operator can follow a single message across every log line, retry
# note, handler invocation, and outbound action.
_trace_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("linling_trace_id", default="")


def current_trace_id() -> str:
    """Return the trace id of the in-flight dispatch (``""`` if none)."""
    return _trace_id_ctx.get()


def _new_trace_id() -> str:
    # 16-char hex keeps logs readable; full UUIDs end up truncated by
    # most log pipelines anyway.
    return uuid.uuid4().hex[:16]


# ---------------------------------------------------------------------------
# Trigger cleanup for /help
# ---------------------------------------------------------------------------

_GROUP_PATTERN = re.compile(r"\((?:\?:[^)]*)?[^)]*\)")


def clean_trigger_label(trigger: str) -> str:
    """Turn a raw regex trigger into a short user-facing label.

    QRDic triggers commonly include ``(.*)`` and ``([0-9]+)`` capture
    groups to receive arguments. For ``/help`` (and the WebUI's
    composer-suggest panel) we'd like to show the trigger without the
    regex noise but with enough structure that users can still guess
    the usage. Strategy:

    * Drop ``^`` / ``$`` anchors.
    * Replace each group with ``…`` as an argument hint.
    * Keep punctuation / Chinese characters verbatim.
    * Bail out (return empty) if the cleaned form is unusable — e.g.
      pure regex metacharacters with no literal text.

    Public: the WebUI's ``/api/agents/{name}/triggers`` endpoint
    delegates here so the inline-suggest panel renders the same labels
    the bot prints in ``/help``.
    """
    text = trigger.strip().strip("^").strip("$")
    cleaned = _GROUP_PATTERN.sub("…", text)
    # Drop trailing metacharacters that survive the group replacement.
    cleaned = cleaned.rstrip("\\")
    if not cleaned or cleaned == "…":
        return ""
    return cleaned


# Backwards-compat alias for the previous private name.
_cleanup_trigger_for_help = clean_trigger_label


# ---------------------------------------------------------------------------
# Dispatcher protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class CommandDispatcher(Protocol):
    """Executes a matched DSL handler against an event.

    Implementations should be CPU-cheap: DSL VMs are typically
    constructed per-call (they're stateless), so passing one in at
    dispatch time is fine.
    """

    async def run(self, event: Event, match: HandlerMatch, session: Session) -> list[Action]: ...


@runtime_checkable
class ChatDispatcher(Protocol):
    """Sends an event's text to an agent and turns the reply into actions."""

    async def run(self, event: Event, session: Session) -> list[Action]: ...


@runtime_checkable
class HistoryReset(Protocol):
    """Optional capability implemented by chat dispatchers with persistent history.

    The router's built-in ``/reset`` command uses this to clear a
    session's long-term memory. Dispatchers without persistence can
    omit it; the router degrades gracefully (clears the in-memory
    deque only).
    """

    async def clear_history(self, scope_id: str, sender_id: str) -> None: ...


@runtime_checkable
class LedgerReset(Protocol):
    """Optional capability for dispatchers with a persistent DSL action ledger.

    The router's ``/reset`` command pairs this with
    :class:`HistoryReset` so a single user-visible reset clears both
    surfaces atomically. The two protocols are independent: a
    dispatcher may implement either, both, or neither, and the router
    short-circuits each leg via ``isinstance`` (Requirement 7.2).

    The ``(scope_id, file_id)`` pair is the *ledger* scope key (group
    collapses to ``"_group"``, DM keeps the sender id) — caller is
    expected to derive it via
    :func:`linling_core.pipeline.ledger_scope_keys` so chat-history
    scope semantics remain unchanged (Requirement 6.7).
    """

    async def clear_ledger(self, scope_id: str, file_id: str) -> None: ...


ActionSink = Callable[[Action], Awaitable[None]]
"""Callable the router invokes for each produced action. Usually wraps
an adapter's ``send``. Multiple sinks can be chained if needed."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouterConfig:
    """Knobs for the router. All values are conservative, single-node defaults."""

    max_concurrent_events: int = 128
    """Global backpressure cap. New events block (or drop, see below)
    when this many are in flight. Sized for a single Python process
    talking to one or two LLM providers."""

    enqueue_timeout_s: float = 1.0
    """How long ``handle`` waits for backpressure before giving up and
    returning a "busy" action to the user. Keep short so the adapter
    can acknowledge quickly."""

    session_timeout_s: float = 30.0
    """Per-session lock hold timeout; a runaway handler cannot block
    further messages from the same user forever."""

    unknown_command_reply: str = "Unknown command. Try /help."
    """Reply text when a message starts with a command prefix but no
    handler matches."""

    busy_reply: str = "Bot is busy, please try again."
    busy_session_reply: str = "You're sending messages too fast, slow down."
    error_reply: str = "Something went wrong handling that. Please try again."
    """Reply text when a dispatcher (DSL command or chat agent) raises.
    Replaces the previous behaviour of dropping the message silently —
    users would otherwise have no idea their message wasn't ignored.
    Keep it generic; the structured log entry holds the real detail.
    """

    help_command_name: str = "help"
    """The sub-command name that triggers the built-in auto-generated
    help (served *before* the classifier). Prefix detection still
    applies — users type ``/help`` (or ``!help``). Set to empty string
    to disable the built-in and let operator-defined handlers take
    over.
    """

    help_header: str = "Available commands:"
    help_max_items: int = 60
    """Upper bound on entries listed in a single ``/help`` reply; large
    rulesets (the migrated QRDic script has 450+ triggers) would blow
    past IM platforms' per-message size otherwise."""

    reset_command_name: str = "reset"
    """Built-in ``<prefix>reset`` command that clears the calling
    session's chat history (both the in-memory deque and the
    persistent KV copy). Set to empty string to disable.
    """

    reset_reply: str = "Chat history cleared."

    cancel_command_name: str = "cancel"
    """Built-in ``<prefix>cancel`` command that interrupts an in-flight
    agent chat dispatch for the calling session. Handled *outside* the
    session lock (that's the point — the lock is held by the thing we
    want to stop), and only affects chat-path dispatches. DSL commands
    are not cancellable because they may be mid read-modify-write on
    the KV store. Set to empty string to disable the built-in.
    """

    cancel_reply: str = "OK, stopped."
    cancel_noop_reply: str = "Nothing in flight to cancel."


@dataclass(frozen=True)
class _DispatchErrorInfo:
    """Structured dispatcher exception metadata for audit payloads."""

    dispatcher: str
    type: str
    message: str


@dataclass(frozen=True)
class _SinkErrorInfo:
    """Structured sink-failure metadata for audit payloads.

    Action sinks (typically the OneBot adapter's ``send``) can fail
    after the dispatcher has already produced its actions — e.g.
    NapCat returning ``status=failed`` on a message containing a
    dead image URL. We collect these per-event so the audit row's
    payload reflects "DSL succeeded but delivery failed" instead of
    silently logging ``ok``.
    """

    platform: str
    type: str
    message: str


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class Router:
    """Orchestrates event → intent → dispatcher → actions."""

    def __init__(
        self,
        *,
        classifier: MessageClassifier,
        commands: CommandDispatcher,
        chats: ChatDispatcher,
        sink: ActionSink,
        conversations: ConversationStore | None = None,
        config: RouterConfig | None = None,
        audit: AuditSink | None = None,
        metrics: MetricsSink | None = None,
    ) -> None:
        self._classifier = classifier
        self._commands = commands
        self._chats = chats
        self._sink = sink
        self._conversations = conversations or ConversationStore()
        self._cfg = config or RouterConfig()
        self._audit: AuditSink = audit or NullAuditSink()
        self._metrics: MetricsSink = metrics or NullMetrics()
        self._semaphore = asyncio.Semaphore(self._cfg.max_concurrent_events)
        self._seen = SeenEvents()

    # ------------------------------------------------------------------ reconfiguration

    def set_sink(self, sink: ActionSink) -> None:
        """Replace the action sink at runtime.

        Used by the bootstrap when adapters are registered *after* the
        router (current tests), and by hot-reload when the adapter list
        is rewired. Thread/coroutine safety: sink replacement is a
        single pointer assignment, which is atomic in CPython; in-flight
        dispatches that already captured ``self._sink`` finish on the
        old sink and new dispatches use the new one.
        """
        self._sink = sink

    @property
    def sink(self) -> ActionSink:
        """Read-only handle to the current action sink.

        Exposed so the bootstrap's scheduler bridge can deliver
        actions through the same adapter as user-driven dispatches
        without reaching into a private attribute.
        """
        return self._sink

    @property
    def classifier(self) -> MessageClassifier:
        """Expose the current classifier so hot-reload can swap it in place."""
        return self._classifier

    def set_classifier(self, classifier: MessageClassifier) -> None:
        """Atomically replace the classifier (used by hot-reload).

        In-flight dispatches keep using the old classifier they already
        consulted; new events see the new one.
        """
        self._classifier = classifier

    def set_command_dispatcher(self, commands: CommandDispatcher) -> None:
        """Replace the DSL command dispatcher (used by hot-reload).

        When a new ``Script`` is compiled, its handlers live on a fresh
        VM-bound dispatcher. We swap this pointer and the classifier in
        a single update so the router sees a consistent pair.
        """
        self._commands = commands

    @property
    def command_dispatcher(self) -> CommandDispatcher:
        """Read-only handle to the current DSL command dispatcher.

        Exposed so the bootstrap can mutate dispatcher-side state
        (e.g. plug in a fresh action sink for ``$发送$``) without
        reaching into private attributes.
        """
        return self._commands

    def set_audit_sink(self, audit: AuditSink) -> None:
        """Replace the audit sink. Used when the WebUI attaches post-bootstrap."""
        self._audit = audit

    # ------------------------------------------------------------------ entrypoint

    async def handle(self, event: Event) -> bool:
        """Process one event. Returns ``True`` if the router produced a
        verdict (even 'ignored'), ``False`` if it declined to handle due
        to duplicate/backpressure/etc. Designed to double as a
        :class:`EventBus` subscriber (treats the return value as "stop
        propagation").
        """
        if not self._seen.add(event.id):
            logger.info("router.duplicate_drop", event_id=event.id, bot_id=event.bot_id)
            self._metrics.counter_inc(ROUTER_DUPLICATES_TOTAL, {"bot_id": event.bot_id})
            return False

        trace_id = _new_trace_id()
        token = _trace_id_ctx.set(trace_id)
        structlog.contextvars.bind_contextvars(
            trace_id=trace_id,
            bot_id=event.bot_id,
            event_id=event.id,
        )
        started = time.monotonic()
        verdict = "unknown"
        outcome = "ok"
        dispatch_error: _DispatchErrorInfo | None = None
        sink_errors: list[_SinkErrorInfo] = []
        try:
            try:
                await asyncio.wait_for(
                    self._semaphore.acquire(), timeout=self._cfg.enqueue_timeout_s
                )
            except TimeoutError:
                logger.warning(
                    "router.backpressure_reject",
                    in_flight=self._cfg.max_concurrent_events,
                )
                await self._emit_text(event, self._cfg.busy_reply)
                verdict = "backpressure"
                outcome = "rate-limited"
                return True

            try:
                verdict, dispatch_error, sink_errors = await self._dispatch(event)
                if dispatch_error is not None:
                    outcome = "error"
                elif sink_errors:
                    # Dispatcher produced actions but the sink rejected
                    # them — distinct from a dispatcher crash. We keep
                    # the verdict (so the rule trace is still visible)
                    # and flag outcome so dashboards can show
                    # delivery-fail rate separately from rule errors.
                    outcome = "sink-failed"
                elif verdict.endswith(":rate-limited") or verdict.endswith(":timeout"):
                    outcome = "rate-limited"
                elif verdict.startswith("ignore:"):
                    outcome = "ignored"
            finally:
                self._semaphore.release()
            duration_ms = (time.monotonic() - started) * 1000.0
            logger.info(
                "router.dispatched",
                verdict=verdict,
                duration_ms=int(duration_ms),
            )
            return True
        finally:
            self._write_audit(
                event=event,
                verdict=verdict,
                outcome=outcome,
                started=started,
                trace_id=trace_id,
                dispatch_error=dispatch_error,
                sink_errors=sink_errors,
            )
            structlog.contextvars.unbind_contextvars("trace_id", "bot_id", "event_id")
            _trace_id_ctx.reset(token)

    # ------------------------------------------------------------------ internals

    async def _dispatch(
        self, event: Event
    ) -> tuple[str, _DispatchErrorInfo | None, list[_SinkErrorInfo]]:
        # Built-in ``/help`` takes precedence over the classifier so
        # operators cannot accidentally shadow it with a rule. The
        # classifier-level prefix parsing still has to agree the
        # message looks like a command, though — "help" typed bare
        # shouldn't override whatever the ruleset wants to do.
        if self._cfg.help_command_name and self._maybe_builtin(event, self._cfg.help_command_name):
            await self._emit_help(event)
            return "help:builtin", None, []
        # ``/cancel`` is handled *before* acquiring the session lock —
        # the whole point is to unblock the lock-holder.
        if self._cfg.cancel_command_name and self._maybe_builtin(
            event, self._cfg.cancel_command_name
        ):
            await self._do_cancel(event)
            return "cancel:builtin", None, []
        if self._cfg.reset_command_name and self._maybe_builtin(
            event, self._cfg.reset_command_name
        ):
            await self._do_reset(event)
            return "reset:builtin", None, []

        intent: Intent = self._classifier.classify(event)

        if intent.kind == "ignore":
            return f"ignore:{intent.reason}", None, []

        if intent.kind == "command" and intent.match is None:
            # Prefix present but no handler matched.
            await self._emit_text(event, self._cfg.unknown_command_reply)
            return f"unknown-command:{intent.reason}", None, []

        # Both remaining branches need a session lock.
        key = self._conversation_key(event, intent)
        session = await self._conversations.get_or_create(
            ConversationKey(bot_id=key[0], scope_id=key[1], sender_id=key[2])
        )

        # Chat traffic is rate-limited per session; commands are not —
        # operators explicitly typed them and shouldn't fight a token bucket.
        if (
            intent.kind == "chat"
            and session.rate_limiter is not None
            and not session.rate_limiter.try_acquire()
        ):
            await self._emit_text(event, self._cfg.busy_session_reply)
            return "chat:rate-limited", None, []

        # Per-session serialization. We don't await inside the lock
        # longer than the dispatcher needs; on timeout we bail so a stuck
        # agent can't block further messages from this user.
        try:
            await asyncio.wait_for(session.lock.acquire(), timeout=self._cfg.session_timeout_s)
        except TimeoutError:
            logger.warning("router.session_lock_timeout", event_id=event.id, key=str(session.key))
            await self._emit_text(event, self._cfg.busy_session_reply)
            return "session-timeout", None, []

        dispatch_error: _DispatchErrorInfo | None = None
        try:
            actions: list[Action]
            if intent.kind == "command":
                assert intent.match is not None
                actions, dispatch_error = await self._safe(
                    self._commands.run(event, intent.match, session),
                    label="command",
                    event=event,
                )
                verdict = f"command:{intent.reason}"
            else:  # chat
                actions, dispatch_error = await self._safe(
                    self._chats.run(event, session),
                    label="chat",
                    event=event,
                )
                verdict = f"chat:{intent.reason}"
        finally:
            session.lock.release()

        if dispatch_error is not None and not actions:
            # Dispatcher raised and produced no actions of its own;
            # send a friendly fallback so the user knows we received
            # the message even though the handler crashed.
            await self._emit_text(event, self._cfg.error_reply)
            verdict = f"{verdict}:error"
            return verdict, dispatch_error, []

        sink_errors: list[_SinkErrorInfo] = []
        for a in actions:
            try:
                await self._sink(a)
            except Exception as exc:
                logger.exception(
                    "router.sink_failed",
                    event_id=event.id,
                    sink_error_type=type(exc).__name__,
                    sink_error_detail=str(exc)[:500],
                )
                self._metrics.counter_inc(
                    SINK_FAILURES_TOTAL,
                    {"bot_id": event.bot_id, "platform": a.target.platform},
                )
                sink_errors.append(
                    _SinkErrorInfo(
                        platform=a.target.platform,
                        type=type(exc).__name__,
                        message=str(exc)[:500],
                    )
                )

        return verdict, dispatch_error, sink_errors

    async def _safe(
        self,
        coro: Awaitable[list[Action]],
        *,
        label: str,
        event: Event,
    ) -> tuple[list[Action], _DispatchErrorInfo | None]:
        """Run a dispatcher coroutine, catching its exceptions.

        Returns ``(actions, error)`` so the caller can distinguish
        "the dispatcher returned no actions on purpose" from "the
        dispatcher crashed". The latter triggers a friendly fallback
        reply so the user is never silently ignored. The structured
        error is also copied into the audit payload so operator-visible
        rows do not require digging through stdout logs.
        """
        try:
            return await coro, None
        except Exception as exc:
            logger.exception(
                "router.dispatcher_failed",
                event_id=event.id,
                bot_id=event.bot_id,
                dispatcher=label,
            )
            return [], _DispatchErrorInfo(
                dispatcher=label,
                type=type(exc).__name__,
                message=str(exc),
            )

    def _conversation_key(self, event: Event, intent: Intent) -> tuple[str, str, str]:
        # Group messages that are chat-intent use (bot, group, sender) so
        # two users in the same group don't share history. Commands use
        # the same; they're short-lived and need the same isolation (and
        # per-session rate doesn't apply to them anyway).
        _ = intent  # reserved for future scope-level overrides
        return event.bot_id, event.scope.id, event.sender.id

    async def _emit_text(self, event: Event, text: str) -> None:
        action = Action(
            kind="reply",
            target=event.scope,
            segments=[TextSegment(text=text)],
        )
        try:
            await self._sink(action)
        except Exception:
            logger.exception("router.sink_failed_emit_text", event_id=event.id)

    # ---- audit -------------------------------------------------------

    def _write_audit(
        self,
        *,
        event: Event,
        verdict: str,
        outcome: str,
        started: float,
        trace_id: str,
        dispatch_error: _DispatchErrorInfo | None = None,
        sink_errors: list[_SinkErrorInfo] | None = None,
    ) -> None:
        """Emit a single audit entry for the dispatch outcome.

        Also records Prometheus counters and a latency histogram so that
        operators can build alerts and dashboards from the same data the
        audit log stores. Swallow any sink exceptions — observability
        must never take down the hot path. ``verdict`` may still be
        ``"unknown"`` if the dispatch raised before setting it, which
        is itself useful forensic data.
        """
        kind = verdict.split(":", 1)[0] if ":" in verdict else verdict
        latency_ms = (time.monotonic() - started) * 1000.0

        # Metrics first — the counter / histogram are O(1) and must run
        # even if the audit sink is slow or fails.
        try:
            self._metrics.counter_inc(
                ROUTER_EVENTS_TOTAL,
                {
                    "bot_id": event.bot_id,
                    "platform": event.platform,
                    "kind": kind,
                    "outcome": outcome,
                },
            )
            self._metrics.histogram_observe(
                DISPATCH_DURATION_SECONDS,
                {"bot_id": event.bot_id, "kind": kind},
                latency_ms / 1000.0,
            )
            self._metrics.gauge_set(
                ACTIVE_SESSIONS,
                {"bot_id": event.bot_id},
                float(self._conversations.snapshot_size()),
            )
        except Exception:
            logger.exception("router.metrics_failed")

        payload: dict[str, object] = {
            "event_id": event.id,
            "platform": event.platform,
            "message": event.text[:500],  # clip to avoid PII firehose
        }
        if dispatch_error is not None:
            payload.update(
                {
                    "error_dispatcher": dispatch_error.dispatcher,
                    "error_type": dispatch_error.type,
                    "error_message": dispatch_error.message[:500],
                }
            )
        if sink_errors:
            # Capped serialised form so the audit row stays small even
            # if a flapping platform produces many sub-action failures.
            payload["sink_errors"] = [
                {"platform": e.platform, "type": e.type, "message": e.message}
                for e in sink_errors[:5]
            ]
        try:
            self._audit.write(
                AuditEntry(
                    trace_id=trace_id,
                    bot_id=event.bot_id,
                    scope_id=event.scope.id,
                    user_id=event.sender.id,
                    kind=kind,
                    outcome=outcome,
                    verdict=verdict,
                    latency_ms=latency_ms,
                    payload=payload,
                )
            )
        except Exception:
            logger.exception("router.audit_write_failed")

    # ---- built-in /help ---------------------------------------------

    def _maybe_builtin(self, event: Event, command_name: str) -> bool:
        """Return True iff ``event`` is an explicit ``<prefix>command_name`` invocation."""
        text = event.text.strip()
        for prefix in self._classifier.command_prefixes():
            candidate = prefix + command_name
            if text == candidate:
                return True
        return False

    async def _emit_help(self, event: Event) -> str:
        """Send the auto-generated command listing."""
        prefix = (
            self._classifier.command_prefixes()[0] if self._classifier.command_prefixes() else ""
        )
        triggers = self._classifier.list_triggers()
        lines: list[str] = [self._cfg.help_header]
        # Only show a preview of each trigger — regex metacharacters
        # would confuse end users. Strip common trailing regex tails.
        shown = 0
        for trig in triggers:
            cleaned = _cleanup_trigger_for_help(trig)
            if not cleaned:
                continue
            lines.append(f"  {prefix}{cleaned}")
            shown += 1
            if shown >= self._cfg.help_max_items:
                lines.append(f"  … and {len(triggers) - shown} more")
                break
        text = "\n".join(lines)
        await self._emit_text(event, text)
        return text

    # ---- built-in /cancel --------------------------------------------

    async def _do_cancel(self, event: Event) -> None:
        """Interrupt an in-flight chat dispatch for the calling session.

        Deliberately **does not** acquire the session lock — that's held
        by the dispatch we're trying to cancel. We just flip the
        session's ``cancel_event`` and let the chat dispatcher race it
        against the LLM call. If no session exists yet (the user hits
        ``/cancel`` without ever chatting) or the session isn't busy,
        we reply with a friendly no-op line.

        Routing note: unlike ``/reset``, cancel never touches the
        conversation store's idempotency path — it's safe to call
        repeatedly in a row without leaking locks.
        """
        key = self._conversation_key(event, Intent(kind="chat"))
        ckey = ConversationKey(bot_id=key[0], scope_id=key[1], sender_id=key[2])
        session = await self._conversations.get_or_create(ckey)

        # ``locked()`` is True iff some other coroutine holds the lock —
        # i.e. a dispatch is in flight. Flipping the event unblocks the
        # chat dispatcher's ``asyncio.wait`` race.
        in_flight = session.lock.locked()
        session.cancel_event.set()
        reply = self._cfg.cancel_reply if in_flight else self._cfg.cancel_noop_reply
        await self._emit_text(event, reply)

    async def _do_reset(self, event: Event) -> None:
        """Clear chat history and the DSL action ledger for the current session.

        Acquires the session lock to wait for any in-flight dispatch to
        finish (keeps KV writes consistent), then atomically clears the
        in-memory deques (chat history *and* the DSL action ledger),
        the persistent chat-history KV (if the chat dispatcher
        supports :class:`HistoryReset`), and the persistent ledger KV
        (if the chat dispatcher supports :class:`LedgerReset`).

        Requirement 7.1:both clears happen inside the same lock-held
        ``try`` block so an external observer never sees the session
        with one surface cleared and the other intact.

        Requirement 7.4:if either persistent clear raises, the
        Router logs the failure (``router.reset_history_clear_failed``
        or ``router.reset_ledger_clear_failed``) and continues — the
        in-memory state has already been cleared and the user gets
        the standard ``reset_reply`` either way.
        """
        from linling_core.pipeline import ledger_scope_keys  # noqa: PLC0415

        key = self._conversation_key(event, Intent(kind="chat"))
        session = await self._conversations.get_or_create(
            ConversationKey(bot_id=key[0], scope_id=key[1], sender_id=key[2])
        )
        try:
            await asyncio.wait_for(session.lock.acquire(), timeout=self._cfg.session_timeout_s)
        except TimeoutError:
            logger.warning("router.reset_lock_timeout", event_id=event.id, key=str(session.key))
            await self._emit_text(event, self._cfg.busy_session_reply)
            return
        try:
            session.history.clear()
            # Requirement 7.1:in-memory ledger clears in the same
            # critical section so chat history and ledger never drift
            # between cleared / un-cleared mid-reset.
            session.dsl_events.clear()
            if isinstance(self._chats, HistoryReset):
                try:
                    await self._chats.clear_history(event.scope.id, event.sender.id)
                except Exception:
                    logger.exception("router.reset_history_clear_failed", event_id=event.id)
            if isinstance(self._chats, LedgerReset):
                try:
                    ledger_scope_id, ledger_file_id = ledger_scope_keys(event, logger=logger)
                    await self._chats.clear_ledger(ledger_scope_id, ledger_file_id)
                except Exception:
                    # Requirement 7.4:failure to clear persistence
                    # must not block the user-visible reply or leave
                    # the in-memory ledger in a half-state.
                    logger.exception(
                        "router.reset_ledger_clear_failed",
                        event_id=event.id,
                        scope_id=event.scope.id,
                        sender_id=event.sender.id,
                    )
            # Drop both rehydration markers so the next chat re-reads
            # fresh state from KV. Independent flags so a transient
            # failure on one surface can't pollute the other.
            object.__setattr__(session, "_linling_history_hydrated", False)
            object.__setattr__(session, "_linling_ledger_hydrated", False)
        finally:
            session.lock.release()
        await self._emit_text(event, self._cfg.reset_reply)
