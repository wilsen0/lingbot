"""Wire a :class:`RunningBot` into a :class:`FastAPI` WebUI app.

This keeps the ``bootstrap`` module free of any FastAPI dependency and
concentrates the "translate events onto the WebUI's ring buffer" policy
here. Separate function means a bot can boot with or without a UI
attached without conditionals polluting the main flow.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

import structlog
from linling_core.audit import AuditEntry
from linling_core.events import Action
from linling_core.router import clean_trigger_label
from linling_core.segments import ImageSegment, TextSegment
from linling_webui.state import BotInfo, TriggerInfo, WebChatReply, WebChatSegment
from linling_webui.wire import wire_bot as _wire_bot_state

if TYPE_CHECKING:
    from fastapi import FastAPI
    from linling_agent.runtime import AgentResult
    from linling_core.events import Event

    from linling_cli.bootstrap import RunningBot

logger = structlog.get_logger(__name__)

# Ring-buffer capacity for the in-memory event mirror. This is per-bot;
# the WebUI client reads at most a few hundred events back for replay,
# so a few thousand is a comfortable operating point.
_DEFAULT_BUFFER_CAPACITY = 2_000


class _WebUIAuditSink:
    """Forwards :class:`AuditEntry` rows into the WebUI's :class:`AuditReader`.

    Kept as a simple wrapper because the WebUI's ``AuditReader`` uses a
    slightly different field set (no ``trace_id`` or ``verdict`` fields
    directly, but allows them via ``payload``). This adapter maps the
    router-facing schema onto the reader's schema without either side
    knowing about the other.
    """

    def __init__(self, state: object) -> None:
        self._state = state

    def write(self, entry: AuditEntry) -> None:
        state = self._state
        if getattr(state, "audit", None) is None:
            # Late fallback — should normally have been set by
            # ``_install_audit_reader`` at attach time. We hit this
            # branch in unit tests that build a bare WebUI app without
            # a bot.
            from linling_webui.audit_reader import AuditReader  # noqa: PLC0415

            state.audit = AuditReader()  # type: ignore[attr-defined]
        payload = dict(entry.payload)
        payload.setdefault("trace_id", entry.trace_id)
        payload.setdefault("verdict", entry.verdict)
        state.audit.append(  # type: ignore[attr-defined]
            bot_id=entry.bot_id,
            user_id=entry.user_id,
            scope_id=entry.scope_id,
            kind=entry.kind,
            outcome=entry.outcome,
            latency_ms=entry.latency_ms,
            payload=payload,
        )


def _install_asset_root(bot: RunningBot) -> None:
    """Set the ``/api/files/assets/...`` root from the bot's base dir.

    DSL rules emit image URLs as the migrator shorthand ``@pic:NAME``.
    The chat dispatcher (see :func:`_rewrite_image_url`) rewrites
    those to ``/api/files/assets/picture/NAME``; the assets live on
    disk under ``<base_dir>/assets/``.

    If the directory is missing (asset-less example, dev sandbox) we
    leave the endpoint disabled. A 404 from the asset endpoint just
    means a broken ``<img>`` tag, not a broken bot.
    """
    from linling_webui.routers.files import set_asset_root  # noqa: PLC0415

    from linling_cli.bootstrap import _resolve_asset_root  # noqa: PLC0415

    base = Path(bot._base_dir) if bot._base_dir else Path.cwd()
    chosen = _resolve_asset_root(base)

    if chosen is None:
        logger.info(
            "bot_assets.no_root",
            bot_id=bot.config.bot_id,
            searched=str(base / "assets"),
        )
        set_asset_root(None)
        return

    logger.info(
        "bot_assets.root_set",
        bot_id=bot.config.bot_id,
        path=str(chosen),
    )
    set_asset_root(chosen)


def _install_audit_reader(state: object, bot: RunningBot) -> None:
    """Pick the audit backend based on ``bot.config.storage.audit``.

    Idempotent — if a previous bot already wired a reader (e.g. an
    in-memory ``AuditReader`` from a test), we leave it alone. The
    intent is "first wire wins"; tests that need a different backend
    set ``state.audit`` explicitly before calling :func:`attach_bot_to_webui`.
    """
    if getattr(state, "audit", None) is not None:
        return

    from linling_webui.audit_reader import AuditReader, SqliteAuditReader  # noqa: PLC0415

    url = bot.config.storage.audit
    if not url:
        state.audit = AuditReader()  # type: ignore[attr-defined]
        logger.info("audit.backend_in_memory", bot_id=bot.config.bot_id)
        return

    if url.startswith("sqlite:///") or url.startswith("sqlite://") or url == ":memory:":
        path = _sqlite_path(url, bot)
        state.audit = SqliteAuditReader(path)  # type: ignore[attr-defined]
        logger.info(
            "audit.backend_sqlite",
            bot_id=bot.config.bot_id,
            path=str(path),
        )
        return

    logger.warning(
        "audit.unsupported_url_falling_back_to_memory",
        bot_id=bot.config.bot_id,
        url=url,
    )
    state.audit = AuditReader()  # type: ignore[attr-defined]


def _sqlite_path(url: str, bot: RunningBot) -> str:
    if url == ":memory:":
        return ":memory:"
    if url.startswith("sqlite:///"):
        raw = url[len("sqlite:///") :]
    elif url.startswith("sqlite://"):
        raw = url[len("sqlite://") :]
    else:
        raw = url
    if raw == ":memory:":
        return ":memory:"
    p = Path(raw)
    if not p.is_absolute():
        base = Path(bot._base_dir) if bot._base_dir else Path.cwd()
        p = base / p
    return str(p)


def attach_bot_to_webui(
    app: FastAPI,
    bot: RunningBot,
    *,
    platform_hint: str = "",
    buffer_capacity: int = _DEFAULT_BUFFER_CAPACITY,
) -> None:
    """Mirror a running bot's event stream into the WebUI's live state.

    * Registers the bot in :class:`WebUIState.bots` so the bot picker
      lists it.
    * Exposes the KV store to the read-only ``/api/kv`` browser.
    * Attaches the bus to the WebUI so WS clients can observe actions.
    * Subscribes an :class:`EventRingBuffer` to the bot's bus, so the
      live event WebSocket has data to stream.

    The platform label defaults to the first adapter kind in the
    config, so operators reading the bot list see ``onebot`` / ``cli``
    rather than ``unknown``. Override with ``platform_hint`` if the bot
    runs behind multiple adapters.
    """
    platform = platform_hint or _first_adapter_kind(bot)

    _wire_bot_state(
        app,
        bot_id=bot.config.bot_id,
        platform=platform,
        name=bot.config.name,
        kv=bot.kv,
        bus=bot.bus,
    )

    # Build an AgentRegistry from the bot's agents dict so the
    # /api/agents endpoints and /ws/agents/:name/stream can look up
    # runtimes. One registry per bot is fine — the WebUI state holds
    # a single global pointer, which we only overwrite when it's empty
    # or belongs to the same bot (avoids clobbering cross-bot wiring).
    if bot.agents:
        from linling_agent.bridge import AgentRegistry  # noqa: PLC0415

        state = app.state.runtime
        registry: AgentRegistry
        existing = state.agent_registry
        if existing is None:
            registry = AgentRegistry()
            state.agent_registry = registry
        else:
            registry = existing
        for name, runtime in bot.agents.items():
            registry.register(name, runtime)

    state = app.state.runtime
    buf = state.buffer_for(bot.config.bot_id, capacity=buffer_capacity)

    # Register web chat dispatchers — one per agent the bot owns. The
    # WS endpoint resolves a user's message through this dispatcher,
    # which in turn runs the DSL classifier first (so triggers like
    # ``我的灵玉`` reach the rule file) and only invokes the LLM when
    # nothing matched. We always register, even when the bot has no
    # agents, so DSL-only bots still get the same behaviour through
    # ``/ws/agents/<dummy>/stream``-shaped tests; the dispatcher
    # tolerates a missing agent.
    for agent_name in bot.agents:
        state.chat_dispatchers[agent_name] = _build_web_chat_dispatcher(bot, agent_name)
        # Trigger provider for the inline-suggest panel. We close over
        # ``bot`` (not ``bot.classifier``) so a hot-reload that swaps
        # the classifier in place still gets reflected on the next
        # poll — the WebUI fetches this on-demand, never cached.
        state.trigger_providers[agent_name] = _build_trigger_provider(bot)

    # Expose the scheduler to the WebUI's state container so admin
    # endpoints can introspect pending tasks. ``state.scheduler`` is
    # singular by design (one scheduler per process); first wire wins.
    if state.scheduler is None and bot.scheduler is not None:
        state.scheduler = bot.scheduler

    async def _mirror(event: Event) -> None:
        # Only ever mirror events belonging to this bot. A shared bus
        # across multiple bots is unlikely in practice, but guarding
        # costs nothing and makes this safe to reuse in test harnesses.
        if event.bot_id != bot.config.bot_id:
            return
        await buf.publish(event)
        info = state.bots.get(bot.config.bot_id)
        if info is not None:
            info.last_event_at = event.time.timestamp()

    bot.add_event_observer(_mirror)

    # Pick the right audit backend (in-memory vs sqlite) before any
    # writes can happen. Idempotent — first wire wins, so tests that
    # pre-set ``state.audit`` keep their reader.
    _install_audit_reader(state, bot)

    # Wire the bot asset root so ``/api/files/assets/...`` can serve
    # the bundled picture sprites referenced by DSL rules via
    # ``@pic:NAME``. Canonical location is ``<base_dir>/assets``;
    # asset-less deployments still work because remote
    # ``±img=https://...±`` URLs never touched this code path.
    _install_asset_root(bot)

    # Route router audit emissions into the WebUI's AuditReader so the
    # ``/api/audit`` endpoint and ``/ws/rules/hits`` feed light up. We
    # install this unconditionally; callers that want to avoid the
    # forwarding can detach it by constructing the Router with a
    # :class:`NullAuditSink` and not calling this function.
    bot.router.set_audit_sink(_WebUIAuditSink(state))

    # Expose ``POST /api/bots/<id>/hot-reload`` → ``RunningBot.reload_rules``.
    # The WebUI endpoint is bot-id aware so multiple bots registered on
    # one app can each be reloaded independently; we close over ``bot``
    # here so only matching IDs actually reload.
    async def _reload(reload_bot_id: str) -> dict[str, object]:
        if reload_bot_id != bot.config.bot_id:
            return {"reloaded": 0, "errors": [f"bot '{reload_bot_id}' not attached here"]}
        report = await bot.reload_rules()
        return {
            "reloaded": report.handlers,
            "files": report.files,
            "applied": report.applied,
            "errors": report.errors,
        }

    state.hot_reload_callback = _reload

    # Register a file controller so ``/api/rules/files*`` can edit the
    # ``.ling`` tree. We close over ``_reload`` so a save → auto-reload
    # cycle hits the same in-process router.
    from linling_cli.rule_files import RuleFileController  # noqa: PLC0415

    async def _reload_for_save() -> dict[str, object]:
        return await _reload(bot.config.bot_id)

    state.rule_files[bot.config.bot_id] = RuleFileController(
        base_dir=Path(bot._base_dir) if bot._base_dir else Path.cwd(),
        globs=list(bot.config.rules),
        reload_fn=_reload_for_save,
    )

    # Attach the Prometheus scrape endpoint. Only reachable when the
    # bot was bootstrapped with ``metrics.enabled=True`` — otherwise
    # ``bot.metrics`` is a :class:`NullMetrics` that lacks ``render``.
    _attach_metrics_endpoint(app, bot)

    _mark_online(state, bot.config.bot_id)


def _attach_metrics_endpoint(app: FastAPI, bot: RunningBot) -> None:
    """Mount ``GET /metrics`` if the bot has a Prometheus backend.

    Idempotent per process — if multiple bots share one app (unusual,
    but possible) only the first installs the route; subsequent calls
    are no-ops because the registry is bot-bound and attaching the
    same path twice would 500 on startup.
    """
    render = getattr(bot.metrics, "render", None)
    if render is None:
        return
    if getattr(app.state, "_linling_metrics_attached", False):
        logger.info("wire_webui.metrics_endpoint_already_attached")
        return

    from fastapi import APIRouter, Depends  # noqa: PLC0415
    from fastapi.responses import Response  # noqa: PLC0415

    metrics_router = APIRouter()

    if bot.config.metrics.auth_required:
        # Defer importing WebUI auth so the branch without auth
        # doesn't pay for JWT machinery.
        from linling_webui.deps import Caller, require_auth  # noqa: PLC0415

        @metrics_router.get("/metrics", include_in_schema=False)
        async def _metrics_protected(
            _caller: Caller = Depends(require_auth),  # noqa: B008
        ) -> Response:
            body, content_type = render()
            return Response(content=body, media_type=content_type)
    else:

        @metrics_router.get("/metrics", include_in_schema=False)
        async def _metrics() -> Response:
            body, content_type = render()
            return Response(content=body, media_type=content_type)

    # Insert at the start of the router table so the SPA fallback's
    # catch-all ``/{path:path}`` mount doesn't shadow us.
    app.include_router(metrics_router)
    new_route = app.router.routes.pop()
    app.router.routes.insert(0, new_route)
    app.state._linling_metrics_attached = True


def _first_adapter_kind(bot: RunningBot) -> str:
    for spec in bot.config.adapters:
        return spec.kind
    return "in-process"


def _build_trigger_provider(
    bot: RunningBot,
) -> Callable[[], list[TriggerInfo]]:
    """Build a closure that snapshots the bot's matchable DSL triggers.

    Used by ``GET /api/agents/{name}/triggers`` to drive the chat
    composer's inline-suggest panel. We deliberately do *not* cache the
    list — ``bot.classifier`` is swapped in place on hot-reload, and a
    fresh poll is cheap (a few hundred entries even for the
    QRDic-migrated 453-trigger ruleset).

    Each :class:`TriggerInfo`:

    * ``raw`` — the trigger source as the classifier matches it
      (regex or literal). The frontend never displays this directly.
    * ``label`` — :func:`clean_trigger_label` output (``([0-9]+)`` →
      ``…``). ``""`` is silently dropped — those are pure-regex
      triggers with no human-readable form.
    * ``has_args`` — true iff cleanup added any ``…`` placeholder.
      The composer uses this to decide between auto-send (no args)
      and "park cursor at the end for the user to type" (has args).
    * ``literal_prefix`` — the chunk before the first ``…``. The
      composer pre-fills this when the user picks a parametric
      trigger so they only have to type the variable bit.

    Triggers without a usable label (e.g. ``[\\s\\S]*`` catch-alls)
    are filtered out entirely — surfacing them just adds noise.

    Triggers whose label or raw form matches one of
    :data:`_SUPPRESSED_TRIGGER_FRAGMENTS` are also dropped — those
    are work-in-progress rules ("备用" / 备用 / spare) that authors
    keep on disk for future use but don't want surfaced as live
    suggestions yet. The classifier still matches them if a user
    types the exact text (the rule file remains the source of
    truth); the picker just doesn't advertise them.
    """

    def provide() -> list[TriggerInfo]:
        triggers = bot.classifier.list_triggers()
        out: list[TriggerInfo] = []
        seen: set[str] = set()
        for raw in triggers:
            if _is_suppressed_trigger(raw):
                continue
            label = clean_trigger_label(raw)
            if not label:
                continue
            if _is_suppressed_trigger(label):
                continue
            # Dedupe — different rule files can register the same
            # trigger; only the first one is reachable, so showing
            # both in the picker is misleading.
            if label in seen:
                continue
            seen.add(label)
            placeholder_idx = label.find("…")
            has_args = placeholder_idx >= 0
            literal_prefix = label[:placeholder_idx] if has_args else label
            out.append(
                TriggerInfo(
                    raw=raw,
                    label=label,
                    has_args=has_args,
                    literal_prefix=literal_prefix,
                )
            )
        return out

    return provide


# Substring matches that hide a trigger from the inline-suggest panel.
# These are *display* filters only — the underlying handler still runs
# if a user types its full text. Use cases so far:
#
# * "备用" — author keeps a backup / spare rule on disk so it can be
#   swapped in later, but doesn't want it surfaced as a live
#   suggestion. The QRDic ruleset has several of these (e.g.
#   ``狐妖更新微博备用``).
#
# Extend conservatively: anything added here is invisible to users
# who don't already know it exists.
_SUPPRESSED_TRIGGER_FRAGMENTS: tuple[str, ...] = ("备用",)


def _is_suppressed_trigger(text: str) -> bool:
    return any(frag in text for frag in _SUPPRESSED_TRIGGER_FRAGMENTS)


def _build_web_chat_dispatcher(
    bot: RunningBot, agent_name: str
) -> Callable[[str, str, str | None], Awaitable[WebChatReply]]:
    """Closure that runs DSL classify+dispatch first, falling back to the LLM.

    The WebUI's chat endpoint hands an already-validated user message
    to this callable. We synthesise a webui-platform :class:`Event`
    and feed it to the bot's classifier:

    * **Command match** — the DSL handler runs through the bot's
      command dispatcher; we collect every text segment and return it.
      The LLM is *not* invoked. This is the path that fixes the bug
      the user hit on ``我的灵玉`` / ``我的好感``: those are real DSL
      triggers and must short-circuit the LLM.
    * **Chat fallback** — no command matched; we delegate to the
      same :class:`AgentChatDispatcher` the IM router uses, threaded
      through the bot's :class:`ConversationStore`. That gives WebUI
      conversations:

      * short-term history that survives across multiple turns, so
        the model can answer "what did I just ask?";
      * persistent history rehydrated from the KV store on restart;
      * per-(scope, sender) session locking, so concurrent WebUI
        clicks for the same account serialise instead of racing on
        ``session.history`` mutation;
      * the same ``/cancel`` semantics IM users get.

      Tool-call counts and token usage are surfaced through the raw
      :class:`AgentResult` returned by ``dispatch`` so the WebUI's
      audit row stays meaningful.
    * **Ignored** — classifier said "ignore" (e.g. blocked sender).
      We return an empty reply with ``source="empty"`` so the WS
      handler can still send a clean ``done`` frame.

    Identity bridging (WebUI ⇄ QQ adapter): we set the synthesised
    event's ``sender.id`` to the WebUI account name (== QQ number by
    convention; users register with their QQ as username). The
    ``scope.id`` defaults to a per-account synthetic scope so most
    rules behave as if the operator were chatting outside their
    main_group; callers can override per-request via ``scope_id`` to
    drive other test groups.

    Output actions are *not* pushed to the bot's adapter sink: this
    is the WebUI's chat surface, not a group reply path. We only
    return the text the rule emits inline; ``$发送$`` calls the rule
    makes still flow through the adapter as side effects (their
    side-effecty design is intentional).
    """
    from linling_core.events import Event, Scope, User  # noqa: PLC0415
    from linling_core.pipeline import ConversationKey, Session  # noqa: PLC0415

    bot_id = bot.config.bot_id
    # WebUI scope routing — DSL semantics:
    #
    # QRSpeed convention treats ``%群号%==0`` as "private chat".
    # Most dicpro.txt-style rules follow this:
    #
    #   * group-only rules early-return on non-group scopes (most
    #     common — they gate ``如果:%群号%==0 返回``);
    #   * private-friendly rules either run regardless or do the
    #     inverse gate (``如果:%群号%!=0 返回``);
    #   * a small minority short-circuit *inside* the configured
    #     ``main_group`` because a sibling bot owns that room.
    #
    # Routing the WebUI to a synthetic ``dm`` scope with id ``"0"``
    # therefore matches a real "operator chats with the bot one-on-one"
    # session — both QQ-side DM and WebUI converge on the same code
    # paths, ledger uses dm semantics, and rules don't have to know
    # the request came from a browser.
    #
    # Operators who want to test rules that *only* run inside a real
    # group can override per-request via ``scope_id`` (REST body or
    # WS frame) — passing the group id flips the synthesised scope
    # to ``kind="group", id=<override>``.

    def _build_scope(user_id: str, override: str | None) -> Scope:
        """Pick the synthesised scope for a WebUI dispatch.

        * No override → DM scope with id ``"0"``. ``%群号%`` resolves
          to ``0`` and ``event.is_dm`` is true, matching the QRSpeed
          private-chat convention.
        * Override matches ``main_group`` (or any non-empty string)
          → ``kind="group"`` and ``id=<override>`` so rules gating
          on the group id reach the in-group branch.
        """
        if override:
            return Scope(kind="group", id=override, platform="webui")
        return Scope(kind="dm", id=_WEBUI_DM_SCOPE_ID, platform="webui")

    async def _dispatch(
        content: str, user_id: str, scope_override: str | None = None
    ) -> WebChatReply:
        scope = _build_scope(user_id, scope_override)
        # ``sender.id`` mirrors the WebUI account name. Per the
        # deployment contract (account == QQ), this means
        # ``%QQ%`` resolves identically on QQ and WebUI for the same
        # operator.
        sender = User(id=user_id, platform="webui")
        event = Event(
            id=f"webui:{user_id}:{int(time.time() * 1000)}",
            platform="webui",
            bot_id=bot_id,
            scope=scope,
            sender=sender,
            kind="message",
            segments=[TextSegment(text=content)],
        )

        intent = bot.classifier.classify(event)

        # Command path: a DSL trigger matched. Run the handler and
        # surface every text/image segment it emits. QRDic rules
        # routinely interleave text and images in one bubble (e.g.
        # ``我的灵玉`` emits "0\n" plus a tier-badge picture); we
        # preserve that ordering so the UI can render a QQ-style
        # mixed bubble.
        if intent.kind == "command" and intent.match is not None:
            session = Session(
                key=ConversationKey(bot_id=bot_id, scope_id=scope.id, sender_id=user_id),
            )
            actions = await bot.router.command_dispatcher.run(
                event, intent.match, session
            )
            segments = _collect_web_segments(actions)
            text = "".join(s.text for s in segments if s.kind == "text")
            return WebChatReply(
                content=text,
                source="dsl",
                segments=tuple(segments),
            )

        # Unknown command (prefix typed but no match): mirror the
        # router's reply rather than calling the LLM. Same rationale
        # as above — operator intent is "this is a command attempt".
        if intent.kind == "command" and intent.match is None:
            return WebChatReply(
                content=bot.router._cfg.unknown_command_reply,
                source="empty",
                segments=(
                    WebChatSegment(kind="text", text=bot.router._cfg.unknown_command_reply),
                ),
            )

        # Ignore verdict: blocked / non-message / self-loop. Surface
        # nothing.
        if intent.kind == "ignore":
            return WebChatReply(content="", source="empty")

        # Chat fallback. We prefer the bot's :class:`AgentChatDispatcher`
        # because it threads short-term history through
        # :class:`Session` *and* persists it via :class:`KVHistoryStore`
        # — that's what fixes "WebUI forgets the previous turn".
        # Only :class:`AgentChatDispatcher` exposes ``dispatch``; other
        # implementations (the static ``_FallbackChatDispatcher``, or
        # the test fixture that injects an agent post-bootstrap) fall
        # through to the legacy direct-runtime path.
        chat = bot.chat_dispatcher
        dispatch = getattr(chat, "dispatch", None) if chat is not None else None

        if dispatch is None:
            # Legacy fallback: no history-aware dispatcher available,
            # so we invoke the named runtime directly. This is also
            # the only branch reachable by tests that bootstrap a
            # bot without ``agent.default_agent`` and inject a fake
            # runtime afterwards.
            runtime = bot.agents.get(agent_name)
            if runtime is None:
                return WebChatReply(content="", source="empty")
            result = await runtime.invoke(content, event=event)
            return _agent_result_to_reply(result)

        # ``dispatch`` mutates ``session.history`` and the persistent
        # KV row, so it must run under the per-session lock — same
        # invariant the router upholds for IM messages. We use
        # :class:`ConversationStore` so the deque is shared across
        # turns (LRU-evicted, TTL-swept) instead of being a
        # one-shot.
        key = ConversationKey(bot_id=bot_id, scope_id=scope.id, sender_id=user_id)
        session = await bot.conversations.get_or_create(key)

        # Two failure modes to tell apart:
        # * ``TimeoutError`` from ``wait_for`` — a previous turn for
        #   the same account is still running (operator double-clicked
        #   or browser reconnected mid-flight). Surface a friendly
        #   ``busy`` message instead of hanging the UI.
        # * ``CancelledError`` from the WS layer — the connection
        #   dropped or the user hit cancel. We re-raise so the WS
        #   handler can clean up; on this path we never acquired the
        #   lock so there's nothing to release.
        # CPython 3.11+ guarantees ``Lock.acquire`` releases the lock
        # if the awaiting coroutine is cancelled after the lock was
        # taken, so we don't need to special-case that here.
        try:
            await asyncio.wait_for(
                session.lock.acquire(), timeout=_WEBUI_SESSION_LOCK_TIMEOUT_S
            )
        except TimeoutError:
            logger.warning(
                "webui.chat.session_lock_timeout",
                bot_id=bot_id,
                scope_id=scope.id,
                sender_id=user_id,
            )
            return WebChatReply(
                content=bot.router._cfg.busy_session_reply,
                source="empty",
                segments=(
                    WebChatSegment(kind="text", text=bot.router._cfg.busy_session_reply),
                ),
            )

        try:
            result_or_none = await dispatch(event, session)
        finally:
            session.lock.release()

        if result_or_none is None:
            # Cancelled (e.g. ``/cancel`` reached the dispatcher first).
            # Surface an empty reply so the WS handler still emits a
            # clean ``done`` frame.
            return WebChatReply(content="", source="empty")
        return _agent_result_to_reply(result_or_none)

    return _dispatch


def _agent_result_to_reply(result: AgentResult) -> WebChatReply:
    """Wrap an :class:`AgentResult` in a :class:`WebChatReply`.

    Centralised so both the dispatcher-backed path and the legacy
    direct-runtime fallback shape their return value identically.
    """
    return WebChatReply(
        content=result.content,
        tool_calls_made=result.tool_calls_made,
        total_tokens=result.total_tokens,
        source="agent",
        segments=(
            (WebChatSegment(kind="text", text=result.content),)
            if result.content
            else ()
        ),
    )


def _collect_web_segments(actions: list[Action]) -> list[WebChatSegment]:
    """Flatten DSL-emitted Actions into ordered :class:`WebChatSegment` items.

    Each ``Action.segments`` already follows the order the DSL emit
    statements ran in. We copy that order through, mapping
    :class:`TextSegment` and :class:`ImageSegment` to web-shaped
    segments and rewriting ``@pic:NAME`` shorthands to
    ``/api/files/assets/...`` URLs the browser can fetch.

    Other segment kinds (``AtSegment``, ``CardSegment`` …) aren't
    representable in the web chat bubble; we drop them. The original
    Action still flowed to whatever IM adapter handled it.
    """
    out: list[WebChatSegment] = []
    for action in actions:
        for seg in action.segments:
            if isinstance(seg, TextSegment):
                if seg.text:
                    out.append(WebChatSegment(kind="text", text=seg.text))
            elif isinstance(seg, ImageSegment):
                url = _rewrite_image_url(seg.url or seg.path or "")
                if url:
                    out.append(
                        WebChatSegment(kind="image", url=url, alt=seg.alt or "")
                    )
    return out


# DSL ``±img=...±`` URL rewrite targets. The migrator emits
# ``@pic:NAME`` for asset references; the chat dispatcher rewrites
# that to ``/api/files/assets/picture/NAME`` so the browser fetches
# from same-origin (CSP friendly). Remote ``http(s)://`` URLs go
# through ``/api/files/proxy?url=...`` for the same reason.
_ASSET_SCHEME = "@pic:"
_WEB_ASSET_PREFIX = "/api/files/assets/"
_PROXY_PREFIX = "/api/files/proxy?url="

# WebUI synthetic DM scope id. ``"0"`` is the QRSpeed convention for
# private chats — DSL rules gating on ``%群号%==0`` (private branch)
# pass on WebUI, and rules gating on ``%群号%==<group>`` (group-only
# branch) skip cleanly. Operators who need to test a rule that only
# fires inside a specific group must pass ``scope_id=<group>`` per
# request, which flips the synthesised scope to ``kind="group"``.
_WEBUI_DM_SCOPE_ID = "0"

# Same-process budget for waiting on the per-session lock during a
# WebUI chat fallback. The WebUI is interactive — a 30s router-style
# timeout would feel like a hang — so we cap shorter and surface a
# friendly ``busy`` reply if a previous turn for the same account is
# still running. The router has its own (separate) timeout for IM.
_WEBUI_SESSION_LOCK_TIMEOUT_S = 20.0


def _rewrite_image_url(raw: str) -> str:
    """Map a DSL-emitted image source to a browser-reachable URL.

    * ``http://...`` / ``https://...`` / ``//host/path`` — proxy through
      ``/api/files/proxy`` so strict CSP (``img-src 'self'``) can still
      render them; the client never talks to third-party hosts directly.
    * ``@pic:<name>`` — rewrite to ``/api/files/assets/picture/<name>``,
      defaulting the extension to ``.jpg`` when the shorthand omits it.
    * Anything else (absolute filesystem paths, ``base64://``, empty) —
      drop (return empty) so the bubble doesn't render a broken
      ``<img>`` for sources the browser can't reach.
    """
    if not raw:
        return ""
    if raw.startswith("//"):
        return _PROXY_PREFIX + quote("https:" + raw, safe="")
    if raw.startswith(("http://", "https://")):
        return _PROXY_PREFIX + quote(raw, safe="")
    if raw.startswith(_ASSET_SCHEME):
        name = raw[len(_ASSET_SCHEME) :]
        if not name:
            return ""
        # The shorthand is ambiguous about the suffix; default to .jpg
        # since the QRDic corpus uses it almost exclusively, and the
        # files router will 404 cleanly if a different extension was
        # actually shipped.
        if "." not in name.rsplit("/", 1)[-1]:
            name = name + ".jpg"
        return _WEB_ASSET_PREFIX + "picture/" + name
    return ""


def _mark_online(state: object, bot_id: str) -> None:
    # BotInfo registered above defaults online=True; this is a guard
    # for the case where a bot was registered elsewhere and we're just
    # refreshing its status.
    bots = getattr(state, "bots", None)
    if not bots or bot_id not in bots:
        return
    info = bots[bot_id]
    if isinstance(info, BotInfo):
        info.online = True
