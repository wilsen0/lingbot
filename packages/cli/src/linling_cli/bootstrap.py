"""`bot.yaml` → running :class:`RunningBot`.

This module is the **only** place that knows how to turn a
:class:`BotConfig` into a live bot: storage opened, rules loaded,
classifier compiled, router wired, adapters constructed, sink routing
set up. Every other layer works with the assembled objects and does not
know about the YAML file.

Why it lives in ``linling-cli``: it's the one package that already
depends on every leaf (core / dsl / agent / both adapters), so placing
the bootstrap here keeps the core-facing packages free of circular
imports. Tests import from here too — that's fine, tests are not
"core".

The bootstrap is intentionally synchronous up to the point where it
returns; the returned :class:`RunningBot` is what you ``await`` on. The
split makes configuration errors ("rules glob matched zero files") show
up before any network handshake.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import linling_core.tools_builtin  # noqa: F401 — registers built-in tools on import
import linling_tools_stdlib  # noqa: F401 — registers stdlib tools (overrides 替换/正则/etc)
import structlog
from linling_core.adapters import Adapter as AdapterHandle
from linling_core.bus import EventBus
from linling_core.classifier import MessageClassifier
from linling_core.config import BotConfig
from linling_core.events import (
    ACTION_DELAY_BEFORE_OPTION,
    Action,
    Event,
    Scope,
    User,
)
from linling_core.metrics import MetricsSink, NullMetrics, set_metrics
from linling_core.pipeline import ConversationStore, Session
from linling_core.router import ActionSink, Router, RouterConfig
from linling_core.scheduler import (
    MemorySchedulerStore,
    ScheduledTask,
    Scheduler,
    SchedulerStore,
    SqliteSchedulerStore,
)
from linling_core.segments import TextSegment
from linling_core.storage.sqlite_kv import SqliteKVStore
from linling_core.tools import registry as global_registry
from linling_dsl.ast_nodes import Script
from linling_dsl.dispatcher import DslCommandDispatcher
from linling_dsl.parser import parse as parse_dsl

if TYPE_CHECKING:
    from linling_agent.agent_def import AgentDef
    from linling_agent.attention_probe import AttentionProbe
    from linling_agent.llm import LLMProvider
    from linling_core.config import AgentConfig
    from linling_core.router import ChatDispatcher

logger = structlog.get_logger(__name__)


# Adapters flow through this module by duck-type. We alias
# :class:`linling_core.adapters.Adapter` to ``AdapterHandle`` at import
# time so existing call sites keep working; use either name freely.
__all__ = [
    "AdapterHandle",
    "ReloadReport",
    "RunningBot",
    "bootstrap_bot",
    "build_sink",
]


# ---------------------------------------------------------------------------
# Public handle
# ---------------------------------------------------------------------------


@dataclass
class ReloadReport:
    """Result of :meth:`RunningBot.reload_rules`."""

    handlers: int
    files: int
    errors: list[str]
    applied: bool


@dataclass
class RunningBot:
    """Assembled runtime. Call :meth:`start` then ``await`` :meth:`wait`.

    Tests prefer :meth:`publish_and_wait` for request-response style
    verification rather than standing up the adapter's run loop.
    """

    config: BotConfig
    kv: SqliteKVStore
    bus: EventBus
    router: Router
    script: Script
    classifier: MessageClassifier
    conversations: ConversationStore
    metrics: MetricsSink
    adapters: list[AdapterHandle] = field(default_factory=list)
    # Map of agent-name → AgentRuntime. Populated when the bot has an
    # ``agent.default_agent`` config; empty otherwise. Exposed so the
    # WebUI can surface agents in its /api/agents list without reaching
    # into the router's private dispatcher.
    agents: dict[str, Any] = field(default_factory=dict)
    # The chat dispatcher the router uses for free-form (non-DSL)
    # messages. Same instance across IM and WebUI so short-term
    # history persistence (KVHistoryStore) and conversation-level
    # rehydration are guaranteed identical on both surfaces. ``None``
    # when no agent is configured (the router falls back to a static
    # reply); the WebUI wiring tolerates that by skipping the LLM
    # delegation in :func:`linling_cli.wire_webui._build_web_chat_dispatcher`.
    chat_dispatcher: Any = None
    # The :class:`Scheduler` driving delayed and recurring DSL handler
    # invocations. Always present (defaults to an in-memory store) so
    # that ``$调用 ms handler$`` works without operator opt-in. Persisted
    # tasks load on construction; ``RunningBot.start()`` is what
    # actually begins draining them.
    scheduler: Scheduler | None = None
    _base_dir: Path | None = None
    _adapter_tasks: list[asyncio.Task[None]] = field(default_factory=list)
    _scheduler_task: asyncio.Task[None] | None = None
    _unsubscribe_observers: list[Callable[[], None]] = field(default_factory=list)

    def add_event_observer(
        self, observer: Callable[[Event], Awaitable[None]]
    ) -> Callable[[], None]:
        """Attach a side-channel observer of every inbound event.

        Observers run **before** the router on the bus (higher
        ``priority``) so a router short-circuit — e.g. the router
        returning ``True`` after handling an event — never starves the
        observer. Because the wrapper always returns ``None``, observers
        cannot themselves stop event propagation.

        This is the seam the WebUI uses to mirror the event stream into
        its live buffer, and is kept generic here so it has no FastAPI
        dependency.

        Returns an unsubscribe callable; also recorded so :meth:`stop`
        cleans up automatically.
        """

        async def _wrapper(event: Event) -> None:
            try:
                await observer(event)
            except Exception:
                logger.exception("bootstrap.observer_failed")
            # Return ``None`` explicitly so we never short-circuit the bus.

        unsub = self.bus.subscribe(_wrapper, name="observer", priority=100)
        self._unsubscribe_observers.append(unsub)
        return unsub

    def _refresh_dispatcher_extras(self, *, sink: ActionSink) -> None:
        """Push runtime side-channels into the DSL dispatcher's extras.

        Centralised so :func:`bootstrap_bot`, :meth:`attach_adapter`,
        and :meth:`reload_rules` all wire the same set of keys
        (``action_sink`` / ``adapter`` / ``primary_platform`` /
        ``handler_lookup``) without drifting. A future addition needs
        to be made here once and benefits every entry point.
        """
        cmds = self.router.command_dispatcher
        if cmds is None:
            return
        update = getattr(cmds, "update_extras", None)
        if update is None:
            return
        update(action_sink=sink)
        # Bot-level identity: expose ``admin_users`` as the
        # ``%管理员%`` placeholder the migrator emits into rule files.
        # Resolved at runtime so a config swap changes behaviour
        # without recompiling rule files. ``%管理员%`` resolves to the
        # *first* admin user, matching QRSpeed's single-owner
        # convention; rules checking against multiple admins should
        # iterate ``admin_users`` themselves once we expose it as a
        # list var.
        update(
            admin_users=tuple(self.config.admin_users),
        )
        rpc_adapter = next((a for a in self.adapters if hasattr(a, "rpc")), None)
        if rpc_adapter is not None:
            update(adapter=rpc_adapter)
        primary = next((a for a in self.adapters if getattr(a, "platform", "")), None)
        if primary is not None:
            update(primary_platform=primary.platform)
        # Hand the dispatcher a *current* handler lookup so ``$回调$``
        # can resolve internal handlers. We close over ``self`` rather
        # than ``self.script`` so a hot-reload that swaps the script
        # is immediately visible to in-flight ``$回调$`` calls.
        update(handler_lookup=self._lookup_handler)

    def _lookup_handler(self, name: str) -> object | None:
        """Return the AST handler whose trigger matches ``name``, or None.

        Used by ``$回调$`` and ``$调用$`` (the latter via the scheduler
        bridge). Tries exact-string match first (~95% of the call
        sites — the QRDic ``$回调 X$`` / ``$调用 ms X$`` shape mostly
        hard-codes a literal trigger). Falls back to a regex
        fullmatch walk so dispatchers can target *regex-trigger*
        internal handlers like ``[内部]说话词语(.*)`` — the rule does
        ``$回调 说话词语%参数-1%$`` and expects the regex handler to
        receive ``%括号1%`` = the suffix.

        Return shape:
        - exact-match hit → ``Handler`` (legacy compatible)
        - regex-match hit → ``(Handler, captures)`` tuple so the
          caller can plumb regex groups through to ``%括号N%``
        - no match → ``None``

        Walks ``self.script.handlers`` linearly — there are only a
        few hundred handlers in the largest deployments, so a dict
        index is unnecessary overhead. If a future ruleset grows
        much larger we can index here.
        """
        for h in self.script.handlers:
            if h.trigger == name:
                return h
        # Regex fallback — only triggered when the literal lookup
        # missed. Compile each trigger lazily and skip uncompilable
        # ones (same liberal contract as the classifier's
        # ``_safe_compile``).
        import re as _re  # noqa: PLC0415

        for h in self.script.handlers:
            try:
                pattern = _re.compile(h.trigger)
            except _re.error:
                continue
            m = pattern.fullmatch(name)
            if m is not None:
                return h, list(m.groups())
        return None

    def attach_adapter(self, adapter: AdapterHandle) -> None:
        """Register an adapter after :func:`bootstrap_bot` has returned.

        Rebuilds the router's action sink so outbound messages can
        reach the new adapter. Useful in tests (which plug in recording
        adapters) and in future hot-reload flows where an operator
        edits the adapter list at runtime. The adapter's own ``run``
        method — if present — is **not** started automatically; callers
        are expected to manage its lifecycle.
        """
        self.adapters.append(adapter)
        new_sink = build_sink(self.adapters)
        self.router.set_sink(new_sink)
        _set_chat_action_sink(self.chat_dispatcher, build_sink(self.adapters, raise_on_error=True))
        self._refresh_dispatcher_extras(sink=new_sink)

    async def reload_rules(self) -> ReloadReport:
        """Reload ``.ling`` files and swap classifier + DSL dispatcher atomically.

        Call from a SIGHUP handler, a WebUI button, or a file-watcher.
        Active conversations keep running on the old handlers until
        they finish; every subsequent event sees the new ruleset.

        Reload policy: if **every** rule file failed to parse, we keep
        the old ruleset (``applied=False``) — apparently the operator
        broke the world wholesale and we shouldn't make it worse. If
        at least one file parsed cleanly, we apply the partial result
        and surface the failed files in ``report.errors`` so the
        operator can fix them. To recover from a complete parse
        disaster either fix the files and SIGHUP again, or restart.
        """
        # Deferred imports: same rationale as in ``bootstrap_bot`` —
        # keep the DSL / tool registry off the import path for commands
        # like ``linling lint`` that never load a bot.
        from linling_core.classifier import MessageClassifier  # noqa: PLC0415
        from linling_core.tools import registry as global_registry  # noqa: PLC0415
        from linling_dsl.dispatcher import DslCommandDispatcher  # noqa: PLC0415

        base = (self._base_dir or Path.cwd()).resolve()
        script, files_loaded, errors = _compile_rules(self.config, base)

        if files_loaded == 0 and errors:
            logger.warning(
                "bootstrap.rules_reload_rejected",
                bot_id=self.config.bot_id,
                errors=errors[:5],
            )
            return ReloadReport(
                handlers=len(self.script.handlers),
                files=0,
                errors=errors,
                applied=False,
            )

        new_classifier = MessageClassifier(
            script,
            command_prefixes=tuple(self.config.classifier.command_prefixes),
            block_scope_ids=frozenset(self.config.classifier.block_scope_ids),
            block_sender_ids=frozenset(self.config.classifier.block_sender_ids),
        )
        # Carry forward the scheduler (for ``$调用$``) into the new
        # dispatcher; the rest of the side-channels (action sink,
        # adapter, primary platform) are pushed via the shared
        # :meth:`_refresh_dispatcher_extras` helper *after* we install
        # the new dispatcher on the router.
        reload_extras: dict[str, Any] = {}
        if self.scheduler is not None:
            reload_extras["scheduler"] = self.scheduler
        new_commands = DslCommandDispatcher(
            registry=global_registry,
            kv=self.kv,
            bot_id=self.config.bot_id,
            extras=reload_extras,
            # Carry forward any DSL Action Ledger writer the existing
            # dispatcher was wired with so a hot-reloaded ruleset
            # keeps appending events to the same in-memory deques.
            ledger_writer=getattr(self.router.command_dispatcher, "ledger_writer", None),
        )
        self.router.set_classifier(new_classifier)
        self.router.set_command_dispatcher(new_commands)
        self._refresh_dispatcher_extras(sink=self.router.sink)
        self.script = script
        self.classifier = new_classifier
        logger.info(
            "bootstrap.rules_reloaded",
            bot_id=self.config.bot_id,
            handlers=len(script.handlers),
            files=files_loaded,
            errors=len(errors),
        )
        return ReloadReport(
            handlers=len(script.handlers),
            files=files_loaded,
            errors=errors,
            applied=True,
        )

    async def _on_scheduled_fire(self, task: ScheduledTask) -> None:
        """Dispatch a scheduler-fired task to its named handler.

        Scheduler tasks reference a handler by *name* (the trigger
        text typed in ``$调用 ms handler args$``), and that handler is
        very often an ``[内部]`` block — they're the canonical target
        for delayed callbacks. The classifier deliberately excludes
        ``[内部]`` handlers from public matching, so we cannot route
        these events through the bus / classifier path: we'd either
        miss internal targets (oldest bug) or accidentally match a
        catch-all regex like ``[\\s\\S]*`` (newer bug).

        Instead, we look the handler up by trigger and invoke the
        DSL VM directly. The synthesised :class:`Event` is still
        flagged ``platform="scheduler"`` / ``sender.id="system:scheduler"``
        so audit consumers can distinguish it from user events.

        Output actions still flow through the bot's sink — the same
        adapter that delivers user replies — so nothing in the
        downstream pipeline needs to know this is a scheduler-fired
        dispatch.

        We emit an explicit audit row + metrics observation here so
        scheduler activity remains visible in dashboards, exactly the
        way router-driven dispatches are. Skipping this would create a
        blind spot for cron / delayed-call flows.

        Cancellation of an in-flight DSL handler is intentionally
        not wired here: DSL commands are designed to complete; the
        scheduler will simply schedule the next tick.
        """
        bot_id = self.config.bot_id
        handler_name = task.handler_name

        # Same lookup contract as ``$回调$`` — exact-name first, then
        # regex fullmatch so a delayed ``$调用 ms 说话词语%参数-1%$``
        # can target an ``[内部]说话词语(.*)`` regex handler. Captures
        # from the regex match feed into ``%括号N%`` alongside any
        # explicit args queued by the caller.
        lookup_result = self._lookup_handler(handler_name)

        # Space-joined fallback for triggers with literal spaces like
        # ``[内部]游戏判断 ([0-9]+)`` — the rule writes
        # ``$调用 ms 游戏判断 %a%$`` and the parser stores
        # ``handler_name="游戏判断"`` + ``args=["12345"]``. Neither
        # the literal lookup nor the regex fullmatch on ``"游戏判断"``
        # alone hits; we have to reconstruct the call shape.
        consumed_args = False
        if lookup_result is None and task.args:
            joined = handler_name + " " + " ".join(str(a) for a in task.args)
            lookup_result = self._lookup_handler(joined)
            if lookup_result is not None:
                consumed_args = True

        if lookup_result is None:
            logger.warning(
                "bootstrap.scheduler_handler_not_found",
                bot_id=bot_id,
                task_id=task.id,
                handler=handler_name,
            )
            self._audit_scheduler_fire(task, outcome="not-found", latency_ms=0.0)
            return
        if isinstance(lookup_result, tuple):
            handler, regex_captures = lookup_result
        else:
            handler = lookup_result
            regex_captures = []

        # Reconstruct the original event scope so the reply Action
        # routes back to the IM adapter that delivered the inbound
        # message. ``$调用$`` persists ``platform``/``scope_kind`` in
        # ``task.scope`` (see ``scheduler_ops.schedule_handler``); for
        # legacy tasks (or tasks scheduled outside of a user event)
        # we fall back to the primary adapter's platform — anything
        # other than ``"scheduler"`` gives the multi-adapter sink a
        # routable hint instead of letting it drop the action with a
        # ``sink_no_adapter_for_platform`` warning.
        primary_platform = ""
        primary = next((a for a in self.adapters if getattr(a, "platform", "")), None)
        if primary is not None:
            primary_platform = primary.platform
        scope_platform = (
            task.scope.get("platform")
            or primary_platform
            or "scheduler"
        )
        # ``scope_kind`` distinguishes group from DM dispatches so the
        # OneBot adapter picks ``message_type=group`` vs ``private``.
        # Older persisted tasks lack this hint; default to ``"system"``
        # — matches the prior behaviour for those callers.
        raw_scope_kind = task.scope.get("scope_kind") or "system"
        scope = Scope(
            kind=raw_scope_kind,
            id=task.scope.get("scope_id", "scheduler") or "scheduler",
            platform=scope_platform,
        )
        sender_id = task.scope.get("sender_id") or "system:scheduler"
        sender = User(id=sender_id, platform="scheduler")
        event = Event(
            id=f"sched:{task.id}:{int(task.fire_at * 1000)}",
            platform="scheduler",
            bot_id=bot_id,
            scope=scope,
            sender=sender,
            kind="system",
            segments=[TextSegment(text=handler_name)],
        )

        # Build a one-shot HandlerMatch so we can call the dispatcher
        # the same way the router does for user events. Captures from
        # the scheduled args feed into ``%括号N%`` for handlers that
        # were originally written as regex triggers — matches the
        # ``$调用 ms handler arg1 arg2$`` ergonomics QRDic users expect.
        from linling_core.classifier import HandlerMatch  # noqa: PLC0415
        from linling_core.pipeline import ConversationKey, Session  # noqa: PLC0415

        # Regex captures (if any) come *first* — that mirrors ``$回调$``
        # semantics where ``%括号N%`` reflects the trigger fullmatch.
        # Explicit ``task.args`` (queued by the caller's ``$调用$``)
        # follow so position-based access still works — except when
        # the space-joined fallback already consumed those args to
        # form the trigger match, in which case the captures cover
        # them and re-passing would duplicate.
        if consumed_args:
            captures = regex_captures
        else:
            captures = regex_captures + list(task.args)
        match = HandlerMatch(handler=handler, captures=captures)

        # Use a dedicated, throwaway Session so scheduled jobs don't
        # contend with the user's live conversation lock. Per-task
        # isolation is correct here: scheduler events are admin-side,
        # not turn-side.
        session = Session(
            key=ConversationKey(bot_id=bot_id, scope_id=scope.id, sender_id=sender_id),
        )

        started = time.monotonic()
        outcome = "ok"
        try:
            actions = await self.router.command_dispatcher.run(event, match, session)
        except Exception:
            logger.exception(
                "bootstrap.scheduler_handler_failed",
                bot_id=bot_id,
                task_id=task.id,
                handler=handler_name,
            )
            self._audit_scheduler_fire(
                task, outcome="error", latency_ms=(time.monotonic() - started) * 1000.0
            )
            return

        sink = self.router.sink  # same sink as $发送$ and user replies
        for action in actions:
            try:
                await sink(action)
            except Exception:
                logger.exception(
                    "bootstrap.scheduler_sink_failed",
                    bot_id=bot_id,
                    task_id=task.id,
                    action_kind=action.kind,
                )
                outcome = "sink-error"

        self._audit_scheduler_fire(
            task, outcome=outcome, latency_ms=(time.monotonic() - started) * 1000.0
        )

    def _audit_scheduler_fire(
        self, task: ScheduledTask, *, outcome: str, latency_ms: float
    ) -> None:
        """Emit an audit entry for a scheduler-fired dispatch.

        The router writes audit rows for user events; scheduler events
        skip the router, so we mirror the schema here. Best-effort: we
        never let a slow audit sink stall the scheduler loop.
        """
        from linling_core.audit import AuditEntry  # noqa: PLC0415

        audit = getattr(self.router, "_audit", None)
        if audit is None:
            return
        try:
            audit.write(
                AuditEntry(
                    trace_id="",  # scheduler dispatches don't carry a trace id
                    bot_id=self.config.bot_id,
                    scope_id=task.scope.get("scope_id", "scheduler") or "scheduler",
                    user_id=task.scope.get("sender_id") or "system:scheduler",
                    kind="scheduler",
                    outcome=outcome,
                    verdict=f"scheduler:{task.handler_name}",
                    latency_ms=latency_ms,
                    payload={
                        "task_id": task.id,
                        "handler": task.handler_name,
                        "args": list(task.args),
                        "key": task.key,
                        "recurring": bool(task.recurring_seconds),
                    },
                )
            )
        except Exception:
            logger.exception(
                "bootstrap.scheduler_audit_failed",
                bot_id=self.config.bot_id,
                task_id=task.id,
            )

    async def start(self) -> None:
        """Start every adapter that exposes an async ``run`` method."""
        if self.scheduler is not None and self._scheduler_task is None:
            self._scheduler_task = asyncio.create_task(
                self.scheduler.run(self._on_scheduled_fire),
                name=f"scheduler:{self.config.bot_id}",
            )
        for adapter in self.adapters:
            runner = getattr(adapter, "run", None)
            if runner is None:
                continue
            task = asyncio.create_task(runner(), name=f"adapter:{type(adapter).__name__}")
            self._adapter_tasks.append(task)

    async def stop(self) -> None:
        """Politely drain adapters and release KV resources."""
        for unsub in self._unsubscribe_observers:
            with contextlib.suppress(Exception):
                unsub()
        self._unsubscribe_observers.clear()
        if self.scheduler is not None:
            await self.scheduler.stop()
        if self._scheduler_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._scheduler_task, timeout=2.0)
            self._scheduler_task = None
        for adapter in self.adapters:
            stopper = getattr(adapter, "stop", None)
            if stopper is not None:
                with contextlib.suppress(Exception):
                    res = stopper()
                    if asyncio.iscoroutine(res):
                        await res
        for task in self._adapter_tasks:
            task.cancel()
        for task in self._adapter_tasks:
            # Wait for cancellation to propagate; a misbehaving adapter
            # must not derail overall shutdown.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._adapter_tasks.clear()
        chat_dispatcher = self.chat_dispatcher
        if chat_dispatcher is not None:
            stopper = getattr(chat_dispatcher, "stop", None)
            if stopper is not None:
                with contextlib.suppress(Exception):
                    res = stopper()
                    if asyncio.iscoroutine(res):
                        await res
        # Close LLM providers so their httpx connection pools get
        # released. Each AgentRuntime owns one provider; iterate the
        # dict but close each underlying provider at most once.
        seen_providers: set[int] = set()
        for runtime in self.agents.values():
            provider = getattr(runtime, "_provider", None)
            if provider is None or id(provider) in seen_providers:
                continue
            seen_providers.add(id(provider))
            close = getattr(provider, "aclose", None)
            if close is None:
                continue
            with contextlib.suppress(Exception):
                await close()
        # Close the SQLite scheduler store if we opened one.
        store = getattr(self.scheduler, "_store", None) if self.scheduler else None
        if store is not None:
            close_store = getattr(store, "close", None)
            if close_store is not None:
                with contextlib.suppress(Exception):
                    close_store()
        await self.kv.close()

    async def wait(self) -> None:
        """Wait for every adapter task to finish (or be cancelled)."""
        if not self._adapter_tasks:
            return
        await asyncio.gather(*self._adapter_tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


async def bootstrap_bot(
    config: BotConfig,
    *,
    base_dir: Path | None = None,
    extra_adapters: list[AdapterHandle] | None = None,
) -> RunningBot:
    """Materialise a :class:`RunningBot` from a parsed configuration.

    ``base_dir`` is used to resolve storage / rules relative paths; the
    default is the current working directory. Tests typically pass
    ``tmp_path`` to sandbox filesystem writes.

    ``extra_adapters`` lets callers plug in test doubles or adapter
    types the bootstrap doesn't know natively. Anything placed here
    participates in sink routing as long as it exposes a ``platform``
    attribute whose value matches ``Scope.platform`` on outgoing
    actions.
    """
    base = (base_dir or Path.cwd()).resolve()

    # 1. Storage.
    kv = _open_kv(config, base)
    scheduler_store = _open_scheduler_store(config, base)
    # The scheduler itself is constructed up-front (still idle — its
    # ``run()`` task only starts when ``RunningBot.start()`` is called)
    # so the DSL dispatcher can hold a reference and ``$调用$`` works.
    scheduler = Scheduler(store=scheduler_store)

    # 1b. Metrics. An opt-in Prometheus backend; otherwise a no-op sink
    #     that still satisfies the MetricsSink protocol so call sites
    #     don't need conditionals. The process-default is updated so
    #     any code path that falls back to :func:`get_metrics` sees the
    #     same sink the router uses.
    metrics = _build_metrics(config)
    set_metrics(metrics)

    # 2. Rules → parsed Script.
    script = _load_rules(config, base)

    # 3. Classifier + conversations.
    classifier = MessageClassifier(
        script,
        command_prefixes=tuple(config.classifier.command_prefixes),
        block_scope_ids=frozenset(config.classifier.block_scope_ids),
        block_sender_ids=frozenset(config.classifier.block_sender_ids),
    )
    conversations = ConversationStore(
        max_sessions=config.conversation.max_sessions,
        ttl_seconds=config.conversation.ttl_seconds,
        history_turns=config.conversation.history_turns,
        rate_per_second=config.conversation.rate_per_second,
        burst=config.conversation.burst,
        ledger_maxlen=config.conversation.ledger_maxlen,
    )

    # 3b. Optional DSL Action Ledger backing store. When the feature
    # is disabled (default), all ledger references stay ``None`` and
    # the dispatchers run with their pre-feature semantics. When
    # enabled, a single ``KVDslLedgerStore`` instance backs both the
    # writer (DSL path) and the renderer's rehydrate (chat path).
    ledger_store: Any = None
    ledger_writer: Any = None
    ledger_renderer: Any = None
    if config.conversation.ledger_enabled:
        from linling_agent.ledger import LedgerRenderer  # noqa: PLC0415
        from linling_agent.ledger_store import KVDslLedgerStore  # noqa: PLC0415
        from linling_dsl.ledger import LedgerWriter  # noqa: PLC0415

        ledger_store = KVDslLedgerStore(
            kv,
            ttl_seconds=config.conversation.ledger_ttl_seconds,
            maxlen=config.conversation.ledger_maxlen,
        )
        ledger_writer = LedgerWriter(
            store=ledger_store,
            single_char_budget=config.conversation.ledger_single_char_budget,
            global_default_expose=config.conversation.ledger_global_default_expose,
        )
        ledger_renderer = LedgerRenderer(
            total_char_budget=config.conversation.ledger_total_char_budget,
        )

    # 4. Dispatchers.
    commands = DslCommandDispatcher(
        registry=global_registry,
        kv=kv,
        bot_id=config.bot_id,
        extras={"scheduler": scheduler},
        ledger_writer=ledger_writer,
    )

    # Image-rendering tools (``$图文$`` / ``$扭蛋图$`` / ``$钓鱼结算图$``
    # / ``$鱼篓图$`` / ``$鱼图鉴图$``) need an on-disk cache dir for their
    # output PNGs and the asset bundle root so sprite-backed renderers
    # can find their art. We resolve both relative to ``base`` (the
    # bot's project dir) so a single bot can host multiple asset bundles
    # without bleed.
    image_cache_dir = base / "data" / "cache" / "image_text"
    image_cache_dir.mkdir(parents=True, exist_ok=True)
    commands.update_extras(image_text_cache_dir=image_cache_dir)
    asset_root_for_tools = _resolve_asset_root(base)
    if asset_root_for_tools is not None:
        commands.update_extras(asset_root=asset_root_for_tools)
    chats, agents = _build_chat_dispatcher(
        config, kv, metrics, base, conversations,
        ledger_store=ledger_store,
        ledger_renderer=ledger_renderer,
    )

    # If we built any agents, expose them through an :class:`AgentRegistry`
    # so the DSL ``$agent <name> <input>$`` bridge can find them. The
    # registry is shared by-reference with the WebUI wiring (see
    # :func:`linling_cli.wire_webui.attach_bot_to_webui`) so a single
    # source of truth backs both surfaces.
    if agents:
        from linling_agent.bridge import AgentRegistry  # noqa: PLC0415

        agent_registry = AgentRegistry()
        for name, runtime in agents.items():
            agent_registry.register(name, runtime)
        commands.update_extras(agent_registry=agent_registry)

    # 5. Event bus + adapters. We build adapters *before* the router so
    #    the sink closure can look up adapter.send by platform without
    #    juggling late-binding references.
    bus = EventBus()
    adapters: list[AdapterHandle] = _build_adapters(config, bus, base_dir=base)
    if extra_adapters:
        adapters.extend(extra_adapters)

    sink = build_sink(adapters)
    _set_chat_action_sink(chats, build_sink(adapters, raise_on_error=True))
    # Initial action_sink wiring — :class:`RunningBot._refresh_dispatcher_extras`
    # will be called once the bot is constructed to fill in the rest
    # (``adapter``, ``primary_platform``, ``handler_lookup``).
    commands.update_extras(action_sink=sink)

    router = Router(
        classifier=classifier,
        commands=commands,
        chats=chats,
        sink=sink,
        conversations=conversations,
        config=RouterConfig(
            max_concurrent_events=config.router.max_concurrent_events,
            enqueue_timeout_s=config.router.enqueue_timeout_s,
            session_timeout_s=config.router.session_timeout_s,
            unknown_command_reply=config.router.unknown_command_reply,
            busy_reply=config.router.busy_reply,
            busy_session_reply=config.router.busy_session_reply,
        ),
        metrics=metrics,
    )

    # 6. Subscribe the router to the bus. Short-circuit: if the router
    #    accepts responsibility for an event it returns ``True`` so the
    #    bus stops propagation — there should be exactly one router per
    #    bus.
    bus.subscribe(router.handle, name="router", priority=0)

    bot = RunningBot(
        config=config,
        kv=kv,
        bus=bus,
        router=router,
        script=script,
        classifier=classifier,
        conversations=conversations,
        metrics=metrics,
        adapters=adapters,
        agents=agents,
        chat_dispatcher=chats,
        scheduler=scheduler,
        _base_dir=base,
    )
    # Now that the bot exists, its ``_refresh_dispatcher_extras`` can
    # finish wiring side-channels that depend on the bot itself
    # (``handler_lookup`` closes over ``bot.script`` so a hot-reload
    # picks up new handlers automatically).
    bot._refresh_dispatcher_extras(sink=sink)
    # Pin ``%RobotRunTime%`` to the moment the bot finished bootstrap
    # — matches QRSpeed semantics where the variable counts uptime
    # against the live process's start, not module import.
    import time as _time  # noqa: PLC0415

    from linling_dsl.vm import set_bot_start_time_ms  # noqa: PLC0415

    set_bot_start_time_ms(int(_time.time() * 1000))
    return bot


# ---------------------------------------------------------------------------
# Metrics wiring
# ---------------------------------------------------------------------------


def _build_metrics(config: BotConfig) -> MetricsSink:
    """Instantiate the metrics backend selected by ``bot.yaml``.

    Returns :class:`NullMetrics` when metrics are disabled (the default).
    Importing :mod:`prometheus_client` is deferred to this branch so
    that deployments without it never pay the dependency cost.
    """
    if not config.metrics.enabled:
        return NullMetrics()

    # Import locally so the absence of ``prometheus_client`` surfaces as
    # a helpful config-time error rather than a module-load error for
    # every bot, regardless of whether metrics are turned on.
    try:
        from linling_core.metrics_prometheus import PrometheusMetrics  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "metrics.enabled=True but `prometheus_client` is not installed. "
            "Install `linling-core[prometheus]` or add prometheus_client to your deps."
        ) from exc
    return PrometheusMetrics()


# ---------------------------------------------------------------------------
# Storage wiring
# ---------------------------------------------------------------------------


def _open_kv(config: BotConfig, base: Path) -> SqliteKVStore:
    url = config.storage.kv
    if url.startswith("sqlite:///"):
        raw = url[len("sqlite:///") :]
    elif url.startswith("sqlite://"):
        raw = url[len("sqlite://") :]
    elif url == ":memory:":
        return SqliteKVStore(bot_id=config.bot_id, db_path=":memory:")
    else:
        raise ValueError(f"unsupported storage.kv URL: {url!r}")

    if raw == ":memory:":
        return SqliteKVStore(bot_id=config.bot_id, db_path=":memory:")

    path = Path(raw)
    if not path.is_absolute():
        path = base / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteKVStore(bot_id=config.bot_id, db_path=str(path))


def _open_scheduler_store(config: BotConfig, base: Path) -> SchedulerStore:
    """Pick the scheduler persistence backend.

    * ``storage.scheduler`` unset → in-memory (tasks die on restart).
    * ``sqlite:///path`` → SQLite-backed; tasks survive restarts.

    Mirrors :func:`_open_kv` so operators have a single mental model
    for storage URLs across the system.
    """
    url = config.storage.scheduler
    if not url:
        return MemorySchedulerStore()
    if url == ":memory:":
        return SqliteSchedulerStore(":memory:")
    if url.startswith("sqlite:///"):
        raw = url[len("sqlite:///") :]
    elif url.startswith("sqlite://"):
        raw = url[len("sqlite://") :]
    else:
        raise ValueError(f"unsupported storage.scheduler URL: {url!r}")
    if raw == ":memory:":
        return SqliteSchedulerStore(":memory:")
    path = Path(raw)
    if not path.is_absolute():
        path = base / path
    return SqliteSchedulerStore(path)


# ---------------------------------------------------------------------------
# Rules loading
# ---------------------------------------------------------------------------


def _compile_rules(config: BotConfig, base: Path) -> tuple[Script, int, list[str]]:
    """Parse every ``.ling`` file under ``config.rules``.

    Shared between the initial bootstrap (which tolerates parse errors
    by logging and continuing) and :meth:`RunningBot.reload_rules`
    (which surfaces them to the caller for decision-making).

    Returns ``(script, files_loaded, errors)``. Handler order follows
    the sorted glob iteration, and duplicates are preserved — the
    classifier does first-match, so later files can intentionally
    shadow earlier ones.
    """
    handlers = []
    files_loaded = 0
    errors: list[str] = []

    for pattern in config.rules:
        for path in sorted(base.glob(pattern)):
            if not path.is_file():
                continue
            try:
                source = path.read_text(encoding="utf-8")
                parsed = parse_dsl(source, filename=str(path), strict=False)
            except Exception as exc:
                rel = str(path.relative_to(base)) if path.is_relative_to(base) else str(path)
                errors.append(f"{rel}: {exc}")
                continue
            handlers.extend(parsed.handlers)
            files_loaded += 1

    return Script(handlers=handlers), files_loaded, errors


def _load_rules(config: BotConfig, base: Path) -> Script:
    """Initial-bootstrap variant of :func:`_compile_rules`.

    Differs from the hot-reload path only in how it reports: it logs an
    aggregate ``rules_compiled`` line (plus a warning on parse errors
    or empty rulesets), rather than returning an error list.
    """
    script, files_loaded, errors = _compile_rules(config, base)
    if errors:
        logger.warning(
            "bootstrap.rules_parse_errors",
            count=len(errors),
            errors=errors[:5],
        )
    if files_loaded == 0:
        logger.warning("bootstrap.no_rules_loaded", globs=config.rules)
    else:
        logger.info(
            "bootstrap.rules_compiled",
            files=files_loaded,
            handlers=len(script.handlers),
        )
    return script


# ---------------------------------------------------------------------------
# Agent dispatcher
# ---------------------------------------------------------------------------


def _build_chat_dispatcher(
    config: BotConfig,
    kv: SqliteKVStore,
    metrics: MetricsSink,
    base: Path,
    conversations: ConversationStore,
    *,
    ledger_store: Any = None,
    ledger_renderer: Any = None,
) -> tuple[ChatDispatcher, dict[str, Any]]:
    """Build an agent-backed chat dispatcher, or a fallback if none configured.

    Separated from the main bootstrap so the "no LLM at all" deployment
    stays cheap — the fallback dispatcher uses zero external resources
    and just replies with the configured ``fallback_reply``.

    ``base`` is the directory ``bot.yaml`` lives in; the agent's YAML
    path is resolved relative to it when not absolute, matching the
    KV / rules / scheduler resolution rules.

    Returns ``(dispatcher, agents)`` where ``agents`` is a ``name → runtime``
    dict so callers (notably the WebUI) can look up AgentRuntimes without
    reaching into the dispatcher's private ``_agent`` field.
    """
    if config.agent.default_agent is None:
        return _FallbackChatDispatcher(text=config.agent.fallback_reply), {}

    # Deferred import: spinning up an LLM provider depends on env vars
    # and we don't want configuration errors (missing API key) to break
    # the simple "command-only" bootstrap.
    from linling_agent.agent_def import AgentDef  # noqa: PLC0415
    from linling_agent.context import ContextBudget  # noqa: PLC0415
    from linling_agent.dispatcher import AgentChatDispatcher  # noqa: PLC0415
    from linling_agent.group_batch import (  # noqa: PLC0415
        GroupBatchChatDispatcher,
        GroupBatchConfig,
    )
    from linling_agent.history import KVHistoryStore  # noqa: PLC0415
    from linling_agent.runtime import AgentRuntime  # noqa: PLC0415

    raw = config.agent.default_agent
    agent_path = Path(raw)
    if not agent_path.is_absolute():
        agent_path = base / agent_path
    agent_def = AgentDef.from_yaml(agent_path)
    provider = _provider_for(agent_def)
    agent = AgentRuntime(
        agent_def=agent_def,
        provider=provider,
        tool_registry=global_registry,
        kv=kv,
        bot_id=config.bot_id,
        metrics=metrics,
    )
    # Persistent short-term memory. The in-memory deque on Session
    # handles the live conversation; this store survives restarts.
    history = KVHistoryStore(kv, max_turns=config.conversation.history_turns)
    dispatcher: ChatDispatcher = AgentChatDispatcher(
        agent=agent,
        history_store=history,
        ledger_store=ledger_store,
        ledger_renderer=ledger_renderer,
        context_budget=ContextBudget(
            max_tokens=config.conversation.context_max_tokens,
            summary_trigger_tokens=config.conversation.summary_trigger_tokens,
            summary_keep_recent_turns=config.conversation.summary_keep_recent_turns,
            summary_max_tokens=config.conversation.summary_max_tokens,
        ),
        max_replies=config.agent.dm_max_replies,
        max_reply_chars=config.agent.dm_max_reply_chars,
        multi_reply_delay_min_s=config.agent.multi_reply_delay_min_s,
        multi_reply_delay_max_s=config.agent.multi_reply_delay_max_s,
    )
    if config.agent.group_batch_enabled:
        names = tuple(
            name
            for name in (
                *config.agent.group_batch_bot_names,
                config.name,
                agent_def.name,
            )
            if name
        )
        attention_probe = _build_attention_probe(
            agent_config=config.agent, agent_def=agent_def
        )
        dispatcher = GroupBatchChatDispatcher(
            inner=dispatcher,
            config=GroupBatchConfig(
                enabled=True,
                window_s=config.agent.group_batch_window_s,
                max_messages=config.agent.group_batch_max_messages,
                max_chars=config.agent.group_batch_max_chars,
                max_replies=config.agent.group_batch_max_replies,
                max_reply_chars=config.agent.group_batch_max_reply_chars,
                multi_reply_delay_min_s=config.agent.multi_reply_delay_min_s,
                multi_reply_delay_max_s=config.agent.multi_reply_delay_max_s,
                require_attention=config.agent.group_batch_require_attention,
                max_hold_s=config.agent.group_batch_max_hold_s,
                bot_names=names,
                # Flip this on only when we also have a real probe to
                # inject. The eligibility predicate in
                # :meth:`GroupBatchChatDispatcher._flush_loop` AND-checks
                # both flags, so an enabled flag without a probe — or
                # a probe without the enabled flag — are equivalent
                # to "no probe", but pairing them here makes the
                # bootstrap intent explicit.
                attention_probe_enabled=attention_probe is not None,
                attention_window_s=config.agent.group_batch_attention_window_s,
            ),
            conversations=conversations,
            bot_id=config.bot_id,
            probe=attention_probe,
            kv=kv,
        )

    # Scope allowlist for chat-mode (LLM fallback only). DSL handlers
    # always run regardless — this gate sits *after* the classifier
    # has decided "this is a chat", so commands are unaffected.
    # See :class:`AgentConfig.allowed_scopes` for the policy table.
    allowed = config.agent.allowed_scopes
    if allowed is not None:
        dispatcher = _ScopeGatedChatDispatcher(
            inner=dispatcher,
            allowed=frozenset(allowed),
            fallback_text=config.agent.fallback_reply,
        )
    return dispatcher, {agent_def.name: agent}


def _set_chat_action_sink(dispatcher: Any, sink: ActionSink) -> None:
    setter = getattr(dispatcher, "set_action_sink", None)
    if setter is None:
        return
    try:
        setter(sink)
    except Exception:
        logger.exception("bootstrap.set_chat_action_sink_failed")


class _ScopeGatedChatDispatcher:
    """Wraps a real chat dispatcher with a per-scope chat-mode allowlist.

    Policy:

    * **DM scopes** (``event.scope.kind == "dm"``) are always allowed.
      Private chats are inherently 1:1, so a deployment can comfortably
      let every operator have an LLM conversation with the bot without
      worrying about group-side noise.
    * **Group scopes** are checked against ``allowed`` — only the listed
      group ids reach the LLM. Other groups get the static
      ``fallback_text`` (or no reply at all, when ``fallback_text`` is
      empty).

    DSL command dispatches are unaffected — the gate sits *after*
    classification, so any matched ``.ling`` handler runs regardless
    of what platform / scope the inbound came from.

    The wrapper preserves the dispatcher Protocol (``run`` returns a
    list of Actions) and *also* the optional ``dispatch`` method that
    :class:`AgentChatDispatcher` uses to expose token/tool stats to the
    WebUI:

    * ``run`` — returns a single fallback reply Action (or no Actions
      if ``fallback_text`` is empty, which keeps the bot completely
      silent in unknown groups).
    * ``dispatch`` — returns a synthesised :class:`AgentResult` with
      the fallback text and zero tool/token usage so the WebUI's
      audit row stays uniform.

    The inner dispatcher's ``agent`` property (used by the WebUI to
    introspect provider/model) is forwarded so the gate is invisible
    to that surface.
    """

    def __init__(
        self,
        *,
        inner: Any,
        allowed: frozenset[str],
        fallback_text: str,
    ) -> None:
        self._inner = inner
        self._allowed = allowed
        self._fallback_text = fallback_text

    @property
    def agent(self) -> Any:
        # Mirror :class:`AgentChatDispatcher.agent` so WebUI introspection
        # (provider name / model / token counters) keeps working. ``None``
        # when the inner dispatcher doesn't expose one (e.g. tests with a
        # custom stub).
        return getattr(self._inner, "agent", None)

    def _is_allowed(self, event: Event) -> bool:
        # DMs are always allowed: private chats are 1:1 and the
        # operator explicitly opted into the conversation by
        # messaging the bot directly. Group chats stay behind the
        # explicit allowlist.
        if event.scope.kind == "dm":
            return True
        return event.scope.id in self._allowed

    def set_action_sink(self, sink: ActionSink) -> None:
        setter = getattr(self._inner, "set_action_sink", None)
        if setter is None:
            return
        setter(sink)

    async def stop(self) -> None:
        stopper = getattr(self._inner, "stop", None)
        if stopper is None:
            return
        res = stopper()
        if asyncio.iscoroutine(res):
            await res

    def _denied_actions(self, event: Event) -> list[Action]:
        if not self._fallback_text:
            return []
        return [
            Action(
                kind="reply",
                target=event.scope,
                segments=[TextSegment(text=self._fallback_text)],
            )
        ]

    async def run(self, event: Event, session: Session) -> list[Action]:
        if not self._is_allowed(event):
            return self._denied_actions(event)
        actions: list[Action] = await self._inner.run(event, session)
        return actions

    async def dispatch(self, event: Event, session: Session) -> Any:
        # Mirror :meth:`AgentChatDispatcher.dispatch` so the WebUI's
        # ``_build_web_chat_dispatcher`` keeps working. We synthesise an
        # :class:`AgentResult` with the fallback text on deny so the
        # WebUI can still render a reply bubble (and audit gets a
        # non-empty turn).
        from linling_agent.runtime import AgentResult  # noqa: PLC0415

        if not self._is_allowed(event):
            return AgentResult(
                content=self._fallback_text,
                tool_calls_made=0,
                total_tokens=0,
            )
        inner_dispatch = getattr(self._inner, "dispatch", None)
        if inner_dispatch is None:
            # Inner doesn't expose ``dispatch``; the WebUI path falls
            # back to ``run`` itself, so this branch is mainly for
            # forward compatibility with custom dispatchers.
            return None
        return await inner_dispatch(event, session)

    # ---- /reset forwarders ------------------------------------------
    #
    # The Router's built-in ``/reset`` uses ``isinstance`` checks
    # against :class:`HistoryReset` and :class:`LedgerReset` to decide
    # whether to clear persistent state. Without these forwarders the
    # gate would silently skip both clears whenever the inner
    # dispatcher implements them — losing chat history and DSL ledger
    # row consistency on a per-scope-allowlisted deployment. The
    # forwarders are unconditional:``/reset`` works regardless of
    # whether the calling scope is on the allowlist (a denied scope
    # cannot dispatch chats but should still be able to reset its
    # session). We use ``hasattr`` rather than ``isinstance(...,
    # HistoryReset)`` to keep the wrapper free of any cross-package
    # imports of those Protocols.

    async def clear_history(self, scope_id: str, sender_id: str) -> None:
        inner_clear = getattr(self._inner, "clear_history", None)
        if inner_clear is None:
            return
        await inner_clear(scope_id, sender_id)

    async def clear_ledger(self, scope_id: str, file_id: str) -> None:
        inner_clear = getattr(self._inner, "clear_ledger", None)
        if inner_clear is None:
            return
        await inner_clear(scope_id, file_id)


class _FallbackChatDispatcher:
    """Zero-dependency stand-in when no agent is configured."""

    def __init__(self, *, text: str) -> None:
        self._text = text

    async def run(self, event: Event, session: Session) -> list[Action]:
        return [
            Action(
                kind="reply",
                target=event.scope,
                segments=[TextSegment(text=self._text)],
            )
        ]


def _provider_for(agent_def: AgentDef) -> LLMProvider:
    """Tiny indirection to pick a concrete LLM provider.

    We only instantiate providers that actually ship with the repo. New
    provider kinds go here; the alternative — a plugin registry — is
    deferred until there's a second provider that warrants it.

    Configuration flows from ``agent_def.provider_config`` — populated
    by :meth:`AgentDef.from_yaml`, which performs ``${VAR}`` env
    interpolation up-front. Legacy ``OPENAI_API_KEY`` /
    ``OPENAI_BASE_URL`` env vars still fill in the gaps for YAMLs that
    omit ``provider_config``, so old deployments are unaffected. The
    provider's built-in ``User-Agent`` default removes any need for an
    ``OPENAI_USER_AGENT`` knob.
    """
    kind = agent_def.provider
    if kind == "openai":
        # Deferred: instantiating OpenAIProvider constructs an httpx
        # client. Keep it out of the import-time path so commandlines
        # like ``linling lint`` stay zero-cost on a missing API key.
        from linling_agent.providers.openai import OpenAIProvider  # noqa: PLC0415

        pc = agent_def.provider_config
        # ``OpenAIProvider`` treats ``extra_headers=None`` and
        # ``extra_headers={}`` interchangeably; pass ``None`` when
        # we have no overrides so the provider's default header set
        # is unmodified.
        extra_headers = pc.extra_headers or None
        # Proxy for the main LLM. ``OPENAI_HTTPS_PROXY`` is the
        # unified env var; most deployments leave it empty (direct).
        import os  # noqa: PLC0415

        proxy = os.environ.get("OPENAI_HTTPS_PROXY", "").strip() or None
        return OpenAIProvider(
            model=agent_def.model,
            api_key=pc.api_key,
            base_url=pc.base_url,
            extra_headers=extra_headers,
            proxy=proxy,
        )
    raise ValueError(f"unknown LLM provider: {kind!r}")


def _build_attention_probe(
    *,
    agent_config: AgentConfig,
    agent_def: AgentDef,
) -> AttentionProbe | None:
    """Resolve credentials and construct the second-stage attention probe.

    Returns ``None`` (and emits exactly one ``info``-level structlog
    record) when:

    * ``agent_config.group_batch_attention_probe_enabled`` is ``False``
      (operator opted out via ``bot.yaml``); or
    * neither ``ATTENTION_PROBE_API_KEY`` nor ``OPENAI_API_KEY`` is set
      (no usable credentials — auto-skip per Requirement 3).

    Otherwise returns a constructed :class:`AttentionProbe` and emits
    one ``info`` record describing the resolved model and base URL.

    Credential fallback chain (Requirement 2):

    * ``api_key``: ``ATTENTION_PROBE_API_KEY`` → ``OPENAI_API_KEY`` →
      probe disabled.
    * ``base_url``: ``ATTENTION_PROBE_BASE_URL`` → ``OPENAI_BASE_URL``
      → ``https://api.openai.com/v1``.
    * ``model``: ``ATTENTION_PROBE_MODEL`` → ``agent_def.model``.

    The function is the only place that translates env state into a
    probe instance; tests substitute ``os.environ`` and call this
    helper directly to verify Requirements 2 / 3 / 14.
    """
    # Deferred import — same rationale as :func:`_provider_for`. The
    # probe construction creates an httpx client; commandlines that
    # never reach this code path keep their zero-cost startup.
    import os  # noqa: PLC0415

    from linling_agent.attention_probe import AttentionProbe  # noqa: PLC0415

    if not agent_config.group_batch_attention_probe_enabled:
        logger.info(
            "group_batch.attention_probe.disabled",
            reason="config_off",
        )
        return None

    # ``or`` chains intentionally treat empty string and unset
    # identically — that matches how
    # :func:`linling_agent.agent_def._provider_config_from_dict`
    # already handles ``LLM_API_KEY`` / ``OPENAI_API_KEY``.
    api_key = (
        os.environ.get("ATTENTION_PROBE_API_KEY", "").strip()
        or os.environ.get("LLM_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )
    if not api_key:
        logger.info(
            "group_batch.attention_probe.disabled",
            reason="no_api_key",
        )
        return None

    base_url = (
        os.environ.get("ATTENTION_PROBE_BASE_URL", "").strip()
        or os.environ.get("LLM_BASE_URL", "").strip()
        or os.environ.get("OPENAI_BASE_URL", "").strip()
        or "https://api.openai.com/v1"
    )
    model = (
        os.environ.get("ATTENTION_PROBE_MODEL", "").strip() or agent_def.model
    )

    # Proxy resolution: ``ATTENTION_PROBE_HTTPS_PROXY`` is the
    # dedicated knob for routing probe traffic through a forward proxy
    # (e.g. when the probe endpoint is geo-blocked but the main LLM
    # is direct). Falls back to ``OPENAI_HTTPS_PROXY`` so a single
    # proxy env var covers both if desired. ``None`` = direct.
    proxy = (
        os.environ.get("ATTENTION_PROBE_HTTPS_PROXY", "").strip()
        or os.environ.get("OPENAI_HTTPS_PROXY", "").strip()
        or None
    )

    probe = AttentionProbe(
        api_key=api_key,
        base_url=base_url,
        model=model,
        proxy=proxy,
    )
    logger.info(
        "group_batch.attention_probe.configured",
        model=model,
        base_url=base_url,
        proxy=proxy or "(direct)",
    )
    return probe


# ---------------------------------------------------------------------------
# Adapters & sink
# ---------------------------------------------------------------------------


def _build_adapters(
    config: BotConfig,
    bus: EventBus,
    *,
    base_dir: Path | None = None,
) -> list[AdapterHandle]:
    adapters: list[AdapterHandle] = []
    asset_root = _resolve_asset_root(base_dir) if base_dir else None
    for spec in config.adapters:
        if spec.kind == "onebot":
            # Deferred: adapter packages may pull in protocol-specific
            # networking libraries; a CLI-only deployment should still
            # be able to boot without them installed.
            from linling_adapter_onebot.adapter import OneBotAdapter  # noqa: PLC0415

            adapters.append(
                OneBotAdapter(
                    bus,
                    ws_url=spec.ws_url,
                    access_token=spec.access_token,
                    bot_id=config.bot_id,
                    asset_root=asset_root,
                    remote_image_preflight=spec.remote_image_preflight,
                    remote_image_fallback_text=spec.remote_image_fallback_text,
                )
            )
        elif spec.kind == "cli":
            from linling_adapter_cli.adapter import CliAdapter  # noqa: PLC0415

            adapters.append(CliAdapter(bus, bot_id=config.bot_id))
        else:
            logger.warning("bootstrap.unknown_adapter_kind", kind=spec.kind)
    return adapters


def _resolve_asset_root(base: Path) -> Path | None:
    """Pick the on-disk root for ``@pic:`` resolution.

    The bundle lives at ``<base>/assets`` — that's the only location
    we look. The OneBot adapter and the WebUI both call this so they
    agree on where to find sprites; if it's missing both surfaces
    silently disable asset rewriting (broken images render as broken,
    which is the right failure mode for a missing bundle).
    """
    candidate = base / "assets"
    return candidate if candidate.is_dir() else None


def build_sink(
    adapters: list[AdapterHandle],
    *,
    raise_on_error: bool = False,
) -> Callable[[Action], Awaitable[None]]:
    """Return an :class:`ActionSink` that dispatches by platform.

    The sink picks the right adapter by matching ``action.target.platform``
    against the adapter's ``platform`` attribute. Adapters without a
    ``platform`` attribute are inferred from their class name (handy for
    tests). A single-adapter deployment bypasses matching entirely.
    """
    if not adapters:

        async def _noop(action: Action) -> None:
            logger.warning("bootstrap.sink_no_adapters", action_kind=action.kind)
            if raise_on_error:
                raise RuntimeError("no adapters configured")

        return _noop

    by_platform: dict[str, AdapterHandle] = {}
    for ad in adapters:
        plat = getattr(ad, "platform", "") or _infer_platform(ad)
        by_platform.setdefault(plat, ad)

    if len(adapters) == 1:
        adapter = adapters[0]

        async def _single(action: Action) -> None:
            await _sleep_before_action(action)
            await _invoke_send(adapter, action, raise_on_error=raise_on_error)

        return _single

    async def _multi(action: Action) -> None:
        plat = action.target.platform
        target_adapter = by_platform.get(plat)
        if target_adapter is None:
            logger.warning(
                "bootstrap.sink_no_adapter_for_platform",
                platform=plat,
                available=list(by_platform),
            )
            if raise_on_error:
                raise RuntimeError(f"no adapter for platform: {plat}")
            return
        await _sleep_before_action(action)
        await _invoke_send(target_adapter, action, raise_on_error=raise_on_error)

    return _multi


async def _sleep_before_action(action: Action) -> None:
    raw = action.options.get(ACTION_DELAY_BEFORE_OPTION)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return
    delay_s = max(0.0, float(raw))
    if delay_s > 0:
        await asyncio.sleep(delay_s)


def _infer_platform(adapter: AdapterHandle) -> str:
    """Fallback label derivation for adapter test doubles that omit ``platform``.

    Real adapter classes declare a ``platform`` class attribute (see
    :class:`linling_core.adapters.Adapter`); this helper exists only so
    legacy / ad-hoc test doubles don't need updating.
    """
    cls = type(adapter).__name__.lower()
    if "onebot" in cls:
        return "onebot"
    if "cli" in cls:
        return "cli"
    return "unknown"


async def _invoke_send(
    adapter: AdapterHandle,
    action: Action,
    *,
    raise_on_error: bool = False,
) -> None:
    """Call ``adapter.send``, accommodating both sync and async variants."""
    send = getattr(adapter, "send", None)
    if send is None:
        logger.warning("bootstrap.sink_adapter_has_no_send", adapter=type(adapter).__name__)
        if raise_on_error:
            raise RuntimeError(f"adapter has no send: {type(adapter).__name__}")
        return
    try:
        result = send(action)
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        logger.exception(
            "bootstrap.sink_adapter_failed",
            adapter=type(adapter).__name__,
            action_kind=action.kind,
        )
        if raise_on_error:
            raise
