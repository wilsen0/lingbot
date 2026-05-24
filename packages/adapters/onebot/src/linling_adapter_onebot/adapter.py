"""OneBot v11 WebSocket adapter.

Connects to a OneBot implementation (NapCat/Lagrange/etc.) via
reverse WebSocket, translates events to linling Events, and sends
Actions back as OneBot API calls.

In addition to message events, the adapter also synthesises QRSpeed
compatibility events: ``[系统]``, ``[退群]``, ``[上下管理]`` text-keyed
"messages" emitted alongside the notice / request payloads so DSL
handlers using QRSpeed's classic patterns can match them. The
underlying notice/request event is **also** published unchanged for
downstream consumers that prefer the structured form.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from pathlib import Path
from typing import Any

import structlog
import websockets
from linling_core.bus import EventBus
from linling_core.events import Action, Event, Scope, User
from linling_core.onebot_codec import from_onebot_msg, to_onebot_msg
from linling_core.segments import ImageSegment, PokeSegment, Segment, TextSegment

logger = structlog.get_logger(__name__)

# Reconnect backoff (seconds). Initial value is what we wait after the
# first unexpected disconnect; subsequent failures double it up to
# ``_RECONNECT_BACKOFF_MAX`` so a misconfigured endpoint or a NapCat
# restart loop doesn't burn CPU and log volume. A clean reconnect
# (``_running`` still true and we made it past the WS handshake) resets
# the counter back to ``_RECONNECT_BACKOFF_INITIAL``.
_RECONNECT_BACKOFF_INITIAL = 5.0
_RECONNECT_BACKOFF_MAX = 60.0

# WebSocket keepalive / framing knobs. Values picked for NapCat-style
# OneBot endpoints on localhost or LAN:
#
# * ``ping_interval`` / ``ping_timeout`` — keep the link warm and notice
#   half-open TCP within ~40s. The adapter does **not** rely on
#   application-level OneBot heartbeats (``meta_event``) for liveness;
#   those are merely logged.
# * ``max_size`` — NapCat occasionally forwards merge-forwarded /
#   image-laden frames over 1MiB. The default cap of 1MiB closes the
#   connection with code 1009 mid-stream; bumping to 8MiB covers the
#   typical worst case while still rejecting clearly pathological
#   payloads.
# * ``max_queue`` — buffer between the WS reader and our dispatch
#   pump. We deliberately oversize this (256 frames) because the
#   reader hands every frame off via ``asyncio.create_task`` so the
#   websockets library should never need to backpressure us. The
#   bump just protects against transient task-scheduling spikes.
# * ``close_timeout`` — bound how long ``async with`` blocks waiting
#   for the close handshake when we're tearing the connection down.
_WS_PING_INTERVAL = 20.0
_WS_PING_TIMEOUT = 20.0
_WS_MAX_SIZE = 8 * 1024 * 1024
_WS_MAX_QUEUE = 256
_WS_CLOSE_TIMEOUT = 5.0


# Per-asset upper bound for inlining as ``base64://``. Anything larger
# is left as ``file://...`` and will only render correctly if NapCat
# happens to share the host filesystem (e.g. native install). Picked
# below ``_WS_MAX_SIZE`` so the JSON envelope, base64 expansion (4/3),
# and any sibling segments still fit in one WS frame: 4 MiB of source
# bytes encodes to ~5.5 MiB which leaves ~2 MiB of headroom — enough
# for a typical reply / text segment alongside the image.
_ASSET_INLINE_MAX_BYTES = 4 * 1024 * 1024

# Bounded in-memory cache for base64-encoded assets. Sprite catalogue
# is ~50 files and tends to be re-sent across messages within seconds,
# so caching avoids repeated read+encode for every group_admin /
# 背包 / 鱼塘商店 reply. Keyed by ``(abs_path, st_mtime_ns, st_size)``
# so an asset edited on disk auto-invalidates without an explicit
# bust. The cap keeps memory bounded if someone drops a huge bundle
# in; eviction is FIFO (insertion order).
_ASSET_CACHE_MAX_ENTRIES = 64

# On-disk extensions we'll try when the migrator-emitted name doesn't
# resolve directly. Order biases toward the SVG → raster output of
# ``scripts/rasterize_assets.py`` (PNG / GIF) so a hand-written rule
# referencing ``@pic:foo.jpg`` after the SVG migration still finds the
# rasterised replacement first; ``.svg`` is last so we only fall back
# to it when no raster exists. Mirrors the WebUI rewriter's default
# of ``.jpg`` (set in ``_ASSET_DEFAULT_EXT``).
_ASSET_FALLBACK_EXTS: tuple[str, ...] = (
    ".png",
    ".gif",
    ".jpg",
    ".jpeg",
    ".webp",
    ".svg",
)
# Default extension when the migrator shorthand omits one (``@pic:郫忧``
# instead of ``@pic:郫忧.jpg``). Matches the migrator's own convention.
_ASSET_DEFAULT_EXT = ".jpg"


# QRSpeed compatibility: notice / request payloads are translated into
# synthetic message events with these triggers so legacy ``[系统]`` /
# ``[退群]`` / ``[上下管理]`` handlers fire. The trigger is the message
# text; the original payload fields are stuffed into ``event.raw`` so
# the same context vars (``%Status%``, ``%Value%``, ``%UinName%``,
# ``%Inviteename%``) resolve as they did under QRSpeed.
_QR_SYSTEM_TRIGGER = "[系统]"
_QR_LEAVE_TRIGGER = "[退群]"
_QR_ADMIN_TRIGGER = "[上下管理]"
# ``[戳一戳]`` is the QRSpeed convention for the OneBot notify/poke
# notice. dicpro.txt's flagship ``[戳一戳]`` rule (cooldowns, image
# cascade, owner守护) keys off this exact bracket text. We synthesise
# a message-shaped event alongside the structured PokeSegment notice
# so both consumers (QRSpeed-era rules + structured-segment readers)
# see it.
_QR_POKE_TRIGGER = "[戳一戳]"

# Mapping QRSpeed-historic ``Status`` codes onto OneBot notice/request
# subtypes. Documented values come from the QRSpeed community wiki and
# the dicpro.txt corpus (which only checks 33, 84, 87 explicitly).
_QR_STATUS = {
    ("notice", "group_increase", "approve"): 33,
    ("notice", "group_increase", "invite"): 33,
    ("notice", "group_decrease", "leave"): 1,
    ("notice", "group_decrease", "kick"): 2,
    ("notice", "group_decrease", "kick_me"): 3,
    ("notice", "group_admin", "set"): 1,
    ("notice", "group_admin", "unset"): 0,
    ("request", "friend", ""): 84,
    ("request", "group", "add"): 87,
    ("request", "group", "invite"): 88,
}


def _first_present(data: dict[str, Any], *keys: str) -> str:
    """Return ``str(data[k])`` for the first non-empty key, else ``""``.

    Convenience for the OneBot fork-spelling fallback chain — NapCat
    / Lagrange / go-cqhttp each name the nickname fields differently.
    """
    for key in keys:
        value = data.get(key)
        if value:
            return str(value)
    return ""


class OneBotAdapter:
    """OneBot v11 WebSocket adapter.

    Connects to a OneBot implementation (NapCat/Lagrange/etc.) via
    reverse WebSocket, translates events to linling Events, and sends
    Actions back as OneBot API calls.
    """

    platform = "onebot"

    def __init__(
        self,
        bus: EventBus,
        *,
        ws_url: str = "ws://127.0.0.1:8080",
        access_token: str = "",
        bot_id: str = "",
        asset_root: Path | None = None,
    ) -> None:
        self._bus = bus
        self._ws_url = ws_url
        self._access_token = access_token
        self._bot_id = bot_id
        # Filesystem root for resolving DSL-emitted ``@pic:NAME``
        # asset shorthands. Set at bootstrap time from ``<base_dir>/
        # assets``. When ``None`` we leave the URLs untouched and let
        # the OneBot endpoint try to fetch them itself, which produces
        # broken images for the kinds of refs that need a local file
        # (NapCat won't read ``@pic:`` either).
        self._asset_root: Path | None = asset_root.resolve() if asset_root else None
        self._ws: Any = None  # websockets connection
        self._running = False
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # Strong references to in-flight dispatch tasks. The WS reader
        # fire-and-forgets every inbound frame so a slow LLM round-trip
        # (5–30s) can't stall the read loop and starve websockets'
        # internal queue / heartbeat. Without holding a reference,
        # ``asyncio.create_task`` results would be eligible for GC and
        # the tasks could be cancelled prematurely. ``discard`` is the
        # done-callback so successful tasks free up.
        self._dispatch_tasks: set[asyncio.Task[None]] = set()
        # ``@pic:`` → ``base64://...`` cache. Keyed by
        # ``(abs_path, mtime_ns, size)`` so a swap-on-disk transparently
        # invalidates without explicit busting. ``dict`` preserves
        # insertion order so we can FIFO-evict when ``len`` exceeds
        # ``_ASSET_CACHE_MAX_ENTRIES``.
        self._asset_b64_cache: dict[tuple[str, int, int], str] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Connect to WebSocket and start receiving events.

        On unexpected disconnect we sleep for an exponentially-growing
        delay (capped at ``_RECONNECT_BACKOFF_MAX``) so a misconfigured
        endpoint or a NapCat restart loop doesn't burn CPU and log
        volume. A connection that survives long enough to hand control
        back from the reader cleanly resets the backoff.
        """
        self._running = True
        backoff = _RECONNECT_BACKOFF_INITIAL
        while self._running:
            try:
                await self._connect_and_listen()
                # Clean exit from the reader (running=False or peer
                # closed gracefully). Reset backoff so the next cycle,
                # if any, starts from the floor.
                backoff = _RECONNECT_BACKOFF_INITIAL
            except websockets.InvalidStatus as exc:
                # 401 / 403 from the server — almost always a wrong
                # access_token. Reconnecting on a fixed schedule will
                # just spam the log and the upstream. Bail.
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                logger.error(
                    "onebot_ws_auth_failed",
                    ws_url=self._ws_url,
                    status_code=status_code,
                )
                self._running = False
                raise
            except Exception as exc:
                if not self._running:
                    break
                logger.warning(
                    "onebot_ws_disconnected",
                    ws_url=self._ws_url,
                    retry_in=backoff,
                    error=type(exc).__name__,
                    detail=str(exc)[:200],
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)

    async def stop(self) -> None:
        """Disconnect gracefully."""
        self._running = False
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        # Resolve any in-flight waiters so callers stop blocking.
        self._fail_pending(ConnectionError("OneBot adapter stopped"))
        # Cancel any dispatch tasks still in flight; we're going down.
        await self._cancel_dispatch_tasks()

    async def send(self, action: Action) -> dict[str, Any]:
        """Send an Action to the OneBot endpoint. Returns the API response."""
        payload = self._build_action_payload(action)
        api_action = payload.pop("action")
        params = payload.pop("params", {})
        return await self.call_api(api_action, **params)

    async def call_api(self, action: str, **params: Any) -> dict[str, Any]:
        """Call a raw OneBot API method.

        Times out at 5s per call: NapCat / Lagrange occasionally drop
        the ``echo`` response when an outbound message fails on QQ's
        side (kicked-from-group, image URL fetch failed, rate limit
        hit, …) and we'd otherwise wedge the calling session for the
        full router ``session_timeout_s`` (default 30s) waiting for a
        reply that never comes. 5s is comfortably above QQ's normal
        round-trip and far below the human-noticeable threshold for
        per-message delay in a busy group.
        """
        echo = uuid.uuid4().hex
        payload = {"action": action, "params": params, "echo": echo}

        if self._ws is None:
            raise RuntimeError("WebSocket not connected")

        await self._ws.send(json.dumps(payload))

        # Wait for the response matched by echo. ``get_running_loop``
        # is preferred over ``get_event_loop`` (deprecated since 3.10).
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        try:
            return await asyncio.wait_for(future, timeout=5.0)
        except TimeoutError:
            logger.warning(
                "onebot_call_api_timeout",
                action=action,
                echo=echo,
            )
            # Surface as an empty success-shaped dict so the DSL stub
            # caller doesn't raise — losing one ``$发送$`` is far
            # better than crashing the whole handler. Real errors
            # (auth, websocket dead) still raise above.
            return {"status": "failed", "retcode": -1, "data": None, "echo": echo}
        finally:
            self._pending.pop(echo, None)

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _build_ws_headers(self) -> dict[str, str]:
        """Build WebSocket connection headers."""
        headers: dict[str, str] = {}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return headers

    async def _connect_and_listen(self) -> None:
        """Establish WS connection and dispatch incoming messages.

        The reader loop is intentionally minimal: it only parses the
        frame and *schedules* dispatch as a separate task. Doing the
        downstream work (router → DSL VM or LLM round-trip) inline
        would block the reader for seconds at a time, which causes
        websockets to stop draining the receive queue, fills the TCP
        receive window, and ultimately makes NapCat declare the link
        dead. Async fan-out keeps the read path latency-bounded by the
        JSON parser alone.
        """
        headers = self._build_ws_headers()
        async with websockets.connect(
            self._ws_url,
            additional_headers=headers,
            ping_interval=_WS_PING_INTERVAL,
            ping_timeout=_WS_PING_TIMEOUT,
            max_size=_WS_MAX_SIZE,
            max_queue=_WS_MAX_QUEUE,
            close_timeout=_WS_CLOSE_TIMEOUT,
        ) as ws:
            self._ws = ws
            logger.info("onebot_ws_connected", ws_url=self._ws_url)
            try:
                async for raw_msg in ws:
                    if not self._running:
                        break
                    try:
                        data = json.loads(raw_msg)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    self._spawn_dispatch(data)
            finally:
                # Cancel any in-flight ``call_api`` waiters so they
                # don't hang for the full 30s timeout when the WS dies.
                # The next ``run()`` iteration will reconnect; callers
                # see a ConnectionError and can retry above us.
                self._fail_pending(ConnectionError("OneBot WebSocket disconnected"))
                self._ws = None
        self._ws = None

    def _spawn_dispatch(self, data: dict[str, Any]) -> None:
        """Schedule ``_dispatch`` as a background task.

        Read-loop fan-out point. We keep a strong reference in
        ``_dispatch_tasks`` because ``asyncio.create_task`` only
        guarantees the task survives until the loop next runs it; a
        long-running dispatch (LLM call) could otherwise be GC'd if
        nothing else held it. ``add_done_callback`` removes the entry
        on completion and surfaces unexpected exceptions to the
        structured log so they never get swallowed silently.
        """
        task = asyncio.create_task(self._dispatch(data), name="onebot_dispatch")
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._on_dispatch_done)

    def _on_dispatch_done(self, task: asyncio.Task[None]) -> None:
        self._dispatch_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.exception(
                "onebot_dispatch_failed",
                error=type(exc).__name__,
                detail=str(exc)[:200],
                exc_info=exc,
            )

    async def _cancel_dispatch_tasks(self) -> None:
        """Cancel and drain any background dispatch tasks (used by ``stop``)."""
        if not self._dispatch_tasks:
            return
        for task in list(self._dispatch_tasks):
            task.cancel()
        # Wait for cancellation to propagate; suppress everything since
        # we're tearing down anyway.
        await asyncio.gather(*self._dispatch_tasks, return_exceptions=True)
        self._dispatch_tasks.clear()

    def _fail_pending(self, exc: BaseException) -> None:
        """Resolve every outstanding ``call_api`` waiter with ``exc``.

        Called on connection drop. Idempotent: futures already resolved
        are skipped. The ``_pending`` map is drained so the new
        connection starts clean.
        """
        for echo, future in list(self._pending.items()):
            if not future.done():
                future.set_exception(exc)
            self._pending.pop(echo, None)

    async def _dispatch(self, data: dict[str, Any]) -> None:
        """Route an incoming WS message to the appropriate handler."""
        # API response (has echo field)
        if "echo" in data:
            echo = data["echo"]
            future = self._pending.get(echo)
            if future and not future.done():
                future.set_result(data)
            return

        # Event dispatch
        post_type = data.get("post_type")
        if post_type == "message":
            event = self._build_event_from_message(data)
            if event:
                await self._bus.publish(event)
        elif post_type == "notice":
            event = self._build_event_from_notice(data)
            if event:
                await self._bus.publish(event)
            # Plus a QRSpeed-compatibility synthetic message event
            # so legacy handlers ([系统]/[退群]/[上下管理]) fire.
            synthetic = self._build_qrspeed_synthetic(data)
            if synthetic is not None:
                await self._bus.publish(synthetic)
        elif post_type == "request":
            # OneBot request events (friend / group join). QRSpeed
            # mapped these to ``[系统]`` triggers with a Status code.
            synthetic = self._build_qrspeed_synthetic(data)
            if synthetic is not None:
                await self._bus.publish(synthetic)
        elif post_type == "meta_event":
            # OneBot heartbeat / lifecycle. Not routed anywhere — but
            # we record it at debug level so an operator who suspects
            # a silent NapCat (no heartbeats arriving) can confirm it
            # from logs without attaching a debugger.
            meta_type = data.get("meta_event_type", "")
            if meta_type == "heartbeat":
                logger.debug("onebot_heartbeat", interval=data.get("interval"))
            elif meta_type == "lifecycle":
                logger.info("onebot_lifecycle", sub_type=data.get("sub_type"))

    # ------------------------------------------------------------------
    # Event builders
    # ------------------------------------------------------------------

    def _build_event_from_message(self, data: dict[str, Any]) -> Event | None:
        """Translate a OneBot message payload into a linling Event."""
        message_type = data.get("message_type", "private")
        message = data.get("message", [])
        segments = from_onebot_msg(message)

        sender_info = data.get("sender", {})
        user_id = str(data.get("user_id", sender_info.get("user_id", "")))
        group_id = str(data.get("group_id", ""))

        # Determine scope
        if message_type == "group":
            scope = Scope(kind="group", id=group_id, platform="onebot")
        else:
            scope = Scope(kind="dm", id=user_id, platform="onebot")

        # Map sender role
        role_map: dict[str, str] = {"owner": "owner", "admin": "admin", "member": "member"}
        raw_role = sender_info.get("role", "member")
        role = role_map.get(str(raw_role), "unknown")

        sender = User(
            id=user_id,
            platform="onebot",
            display_name=sender_info.get("nickname") or sender_info.get("card"),
            role=role,
        )

        return Event(
            id=str(data.get("message_id", uuid.uuid4().hex)),
            platform="onebot",
            bot_id=self._bot_id,
            scope=scope,
            sender=sender,
            kind="message",
            segments=segments,
            raw=data,
        )

    def _build_event_from_notice(self, data: dict[str, Any]) -> Event | None:
        """Translate a OneBot notice payload into a linling Event."""
        notice_type = data.get("notice_type", "")
        sub_type = data.get("sub_type", "")
        user_id = str(data.get("user_id", ""))
        group_id = str(data.get("group_id", ""))

        if notice_type == "notify" and sub_type == "poke":
            target_id = str(data.get("target_id", ""))
            # group_id of 0 or empty means it's not a group context
            scope = (
                Scope(kind="group", id=group_id, platform="onebot")
                if group_id and group_id != "0"
                else Scope(kind="dm", id=user_id, platform="onebot")
            )
            segments = [PokeSegment(target_user_id=target_id)]
            sender = User(id=user_id, platform="onebot")
            return Event(
                id=uuid.uuid4().hex,
                platform="onebot",
                bot_id=self._bot_id,
                scope=scope,
                sender=sender,
                kind="notice",
                segments=segments,
                raw=data,
            )

        # Generic notice fallback
        scope = (
            Scope(kind="group", id=group_id, platform="onebot")
            if group_id and group_id != "0"
            else Scope(kind="system", id="system", platform="onebot")
        )
        sender = User(id=user_id, platform="onebot")
        return Event(
            id=uuid.uuid4().hex,
            platform="onebot",
            bot_id=self._bot_id,
            scope=scope,
            sender=sender,
            kind="notice",
            segments=[],
            raw=data,
        )

    # ------------------------------------------------------------------
    # QRSpeed-compatibility synthetic events
    # ------------------------------------------------------------------

    def _build_qrspeed_synthetic(self, data: dict[str, Any]) -> Event | None:
        """Build a ``message``-shaped event whose text matches a QRSpeed trigger.

        QRSpeed-era rules use bracketed pseudo-triggers like ``[系统]``,
        ``[退群]`` and ``[上下管理]`` to handle non-message events.
        We translate OneBot's ``notice`` and ``request`` payloads into
        synthetic message events with that exact text so DSL handlers
        match them. The original notice/request payload is also
        published unchanged for callers that prefer the structured
        ``Event(kind='notice')`` form.

        ``event.raw`` is populated with the QRSpeed-historic field
        names (``user_id``, ``operator_id``, ``operator_name``,
        ``user_name``, ``status``, ``value``, ``request_id``,
        ``message_id``, ``time``, ``sub_type``) so the corresponding
        ``%QQ%`` / ``%Code%`` / ``%Inviteename%`` / ``%UinName%`` /
        ``%Status%`` / ``%Value%`` / ``%Reqid%`` / ``%Msgbar%`` /
        ``%Time%`` / ``%Type%`` context variables resolve.
        """
        post_type = str(data.get("post_type", ""))
        trigger = self._pick_qr_trigger(post_type, data)
        if trigger is None:
            return None

        group_id = str(data.get("group_id", "")) or "0"
        # ``user_id`` for notice events is the affected user, not the
        # operator. QRSpeed maps that to ``%QQ%`` and the operator
        # (inviter / kicker) to ``%Code%`` — so we do the same here.
        user_id = str(data.get("user_id", "")) or self._bot_id

        scope = (
            Scope(kind="group", id=group_id, platform="onebot")
            if group_id and group_id != "0"
            else Scope(kind="dm", id=user_id, platform="onebot")
        )
        sender = User(id=user_id, platform="onebot")

        sub_type = str(data.get("sub_type", ""))
        notice_or_request = (
            str(data.get("notice_type", ""))
            if post_type == "notice"
            else str(data.get("request_type", ""))
        )
        status = self._compute_qr_status(post_type, notice_or_request, sub_type, data)

        return Event(
            id=uuid.uuid4().hex,
            platform="onebot",
            bot_id=self._bot_id,
            scope=scope,
            sender=sender,
            kind="message",
            segments=[TextSegment(text=trigger)],
            raw=self._build_qrspeed_raw(data, status, sub_type),
        )

    @staticmethod
    def _build_qrspeed_raw(data: dict[str, Any], status: Any, sub_type: str) -> dict[str, Any]:
        """Stuff QRSpeed-historic field names onto the synthetic event's ``raw``.

        The VM's context-variable resolver pulls from here for
        ``%Code%`` / ``%UinName%`` / ``%Inviteename%`` / etc.

        OneBot v11 doesn't standardise nickname fields on notice
        payloads — NapCat / Lagrange / go-cqhttp each spell them
        differently. We probe the common variants in priority order
        so legacy QRSpeed handlers see *some* name regardless of
        which fork is upstream.
        """
        return {
            **data,
            "operator_id": data.get("operator_id", ""),
            "operator_name": _first_present(
                data,
                "operator_nickname",
                "operator_name",
                "invitor_nickname",
                "invitor_name",
            ),
            "user_name": _first_present(data, "user_nickname", "user_name", "nickname"),
            "status": status,
            "value": data.get("value", ""),
            "request_id": data.get("flag", ""),
            "message_id": data.get("message_id", ""),
            "time": data.get("time", ""),
            "sub_type": sub_type,
            # Marker the classifier looks at: synthetic events fall
            # through to ``ignore`` when no DSL handler matches their
            # bracket trigger, instead of being routed to the chat
            # agent. Otherwise an unhandled ``[系统]`` event would be
            # sent to the LLM as the literal string "[系统]".
            "_synthetic_qrspeed": True,
        }

    @staticmethod
    def _pick_qr_trigger(post_type: str, data: dict[str, Any]) -> str | None:
        """Decide which QRSpeed bracket trigger this payload maps to.

        Returns ``None`` for shapes we don't synthesise.
        """
        if post_type == "notice":
            notice_type = str(data.get("notice_type", ""))
            if notice_type == "group_increase":
                return _QR_SYSTEM_TRIGGER
            if notice_type == "group_decrease":
                return _QR_LEAVE_TRIGGER
            if notice_type == "group_admin":
                return _QR_ADMIN_TRIGGER
            # ``notify/poke`` rides the same synthesis path as the
            # other notice subtypes — emits a synthetic ``[戳一戳]``
            # message event alongside the structured PokeSegment
            # notice. dicpro.txt's [戳一戳] handler matches the bracket
            # text directly; without this synthesis the rule never
            # fires from a real OneBot poke.
            if notice_type == "notify" and str(data.get("sub_type", "")) == "poke":
                return _QR_POKE_TRIGGER
        elif post_type == "request":
            # Friend / group request — ``[系统]`` with %Status%
            # discriminating which subtype.
            return _QR_SYSTEM_TRIGGER
        return None

    @staticmethod
    def _compute_qr_status(
        post_type: str,
        notice_or_request: str,
        sub_type: str,
        data: dict[str, Any],
    ) -> Any:
        """Pick the ``%Status%`` value for a synthesised event.

        Falls back to whatever ``data['status']`` already carries (some
        OneBot implementations stamp it on group_admin notices), then
        to the QRSpeed historic table, then to an empty string.
        """
        if "status" in data and data["status"] not in (None, ""):
            return data["status"]
        return _QR_STATUS.get((post_type, notice_or_request, sub_type), "")

    # ------------------------------------------------------------------
    # Action builder
    # ------------------------------------------------------------------

    # DSL rules emit image URLs as ``@pic:NAME`` (the migrator's
    # shorthand). NapCat can't fetch that — we read the bytes off
    # disk and inline them as ``base64://...`` so the image renders
    # regardless of whether NapCat shares the host filesystem (e.g.
    # the supported Docker deployment doesn't mount ``bot/assets``).
    # Files larger than ``_ASSET_INLINE_MAX_BYTES`` fall back to
    # ``file://...`` — those are oversized for one WS frame anyway,
    # and the file:// path at least works on native NapCat installs.
    _ASSET_SCHEME = "@pic:"

    def _resolve_asset_url(self, raw: str) -> str:
        """Map ``@pic:NAME`` to a ``base64://...`` (or ``file://...``) URL.

        Returns the URL unchanged when:
        * the string isn't a known asset shorthand,
        * no asset root is configured, or
        * resolution would escape the asset root (path traversal).

        Encoding falls back to ``file://`` for files larger than
        ``_ASSET_INLINE_MAX_BYTES`` so we don't blow the 8 MiB WS
        frame cap. Read / encode failures also fall back to
        ``file://`` so the bot stays "broken-image instead of broken
        send" if a sprite is unreadable.
        """
        if not raw or self._asset_root is None or not raw.startswith(self._ASSET_SCHEME):
            return raw

        target = self._resolve_asset_path(raw[len(self._ASSET_SCHEME) :])
        if target is None:
            return raw

        encoded = self._encode_asset_b64(target)
        if encoded is not None:
            return encoded
        # Oversized or read failure — fall back to ``file://``. NapCat
        # in a Docker deployment will still 404 on it, but on a native
        # install (or if the operator mounted the asset directory in)
        # it will work, and the WS frame is guaranteed to fit.
        return f"file://{target}"

    def _resolve_asset_path(self, name: str) -> Path | None:
        """Resolve a bare ``@pic:`` name to an on-disk path, or ``None``.

        Walks the requested extension first, then a small set of
        fallback extensions so legacy ``@pic:道具宝箱.jpg`` references
        still find ``道具宝箱.svg`` after the SVG migration. A name
        without an extension is treated as a request for the default
        (``.jpg``).
        """
        if not name:
            return None
        relpath = Path("picture") / name
        # Default to .jpg when the shorthand omits an extension —
        # mirrors the WebUI rewriter and matches what the migrator
        # emits.
        if "." not in relpath.name:
            relpath = relpath.with_suffix(_ASSET_DEFAULT_EXT)

        target = self._safe_join(relpath)
        if target is not None and target.is_file():
            return target

        # Fallback walk over sibling extensions. Skip the one we just
        # tried so we don't re-stat the same path.
        original_ext = relpath.suffix.lower()
        stem = relpath.stem
        for ext in _ASSET_FALLBACK_EXTS:
            if ext == original_ext:
                continue
            alt = self._safe_join(relpath.with_name(f"{stem}{ext}"))
            if alt is not None and alt.is_file():
                return alt
        return None

    def _encode_asset_b64(self, target: Path) -> str | None:
        """Read ``target`` and return its ``base64://...`` URL, or ``None``.

        ``None`` is returned for over-budget files (so the caller can
        fall back to ``file://``) and for IO errors. Cache key uses
        ``(abs_path, st_mtime_ns, st_size)`` so an in-place edit
        invalidates the cached encoding without the operator
        restarting the bot. Cache size is capped; FIFO eviction.
        """
        try:
            stat = target.stat()
        except OSError as exc:
            logger.warning(
                "onebot_asset_stat_failed",
                path=str(target),
                error=type(exc).__name__,
            )
            return None
        if stat.st_size > _ASSET_INLINE_MAX_BYTES:
            logger.info(
                "onebot_asset_too_large_for_inline",
                path=str(target),
                size=stat.st_size,
                max_bytes=_ASSET_INLINE_MAX_BYTES,
            )
            return None

        key = (str(target), stat.st_mtime_ns, stat.st_size)
        cached = self._asset_b64_cache.get(key)
        if cached is not None:
            return cached

        try:
            payload = target.read_bytes()
        except OSError as exc:
            logger.warning(
                "onebot_asset_read_failed",
                path=str(target),
                error=type(exc).__name__,
            )
            return None
        encoded = "base64://" + base64.b64encode(payload).decode("ascii")
        # FIFO evict before insert so the dict never exceeds the cap.
        # ``next(iter(d))`` returns the oldest insertion key on a
        # standard dict (insertion-ordered since 3.7).
        if len(self._asset_b64_cache) >= _ASSET_CACHE_MAX_ENTRIES:
            self._asset_b64_cache.pop(next(iter(self._asset_b64_cache)), None)
        self._asset_b64_cache[key] = encoded
        return encoded

    def _safe_join(self, rel: Path) -> Path | None:
        """Resolve ``self._asset_root / rel`` and reject path traversal.

        Returns ``None`` if ``rel`` is absolute or escapes the root.
        """
        root = self._asset_root
        if root is None or rel.is_absolute():
            return None
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return None
        return target

    def _resolve_asset_segments(self, segments: list[Segment]) -> list[Segment]:
        """Return ``segments`` with image URLs rewritten in place.

        Non-image segments pass through unchanged; image segments with
        a URL we don't recognize (remote ``http(s)``, raw ``file://``,
        ``base64://``) also pass through. Mutates a shallow copy so
        the caller's list isn't aliased into the OneBot payload.
        """
        out: list[Segment] = []
        for seg in segments:
            if isinstance(seg, ImageSegment) and seg.url:
                resolved = self._resolve_asset_url(seg.url)
                if resolved != seg.url:
                    out.append(
                        ImageSegment(
                            url=resolved,
                            path=seg.path,
                            b64=seg.b64,
                            alt=seg.alt,
                            extras=seg.extras,
                        )
                    )
                    continue
            out.append(seg)
        return out

    def _build_action_payload(self, action: Action) -> dict[str, Any]:
        """Translate a linling Action into a OneBot API call payload."""
        kind = action.kind
        target = action.target

        if kind in ("reply", "send"):
            ob_msg = to_onebot_msg(self._resolve_asset_segments(action.segments))
            params: dict[str, Any] = {"message": ob_msg}
            if target.kind == "group":
                params["message_type"] = "group"
                params["group_id"] = int(target.id) if target.id.isdigit() else target.id
            else:
                params["message_type"] = "private"
                params["user_id"] = int(target.id) if target.id.isdigit() else target.id
            return {"action": "send_msg", "params": params}

        if kind == "recall":
            msg_id = action.options.get("message_id", "")
            return {"action": "delete_msg", "params": {"message_id": msg_id}}

        if kind == "mute":
            duration = action.options.get("duration", 60)
            user_id = action.options.get("user_id", "")
            return {
                "action": "set_group_ban",
                "params": {
                    "group_id": int(target.id) if target.id.isdigit() else target.id,
                    "user_id": int(str(user_id)) if str(user_id).isdigit() else user_id,
                    "duration": duration,
                },
            }

        if kind == "unmute":
            user_id = action.options.get("user_id", "")
            return {
                "action": "set_group_ban",
                "params": {
                    "group_id": int(target.id) if target.id.isdigit() else target.id,
                    "user_id": int(str(user_id)) if str(user_id).isdigit() else user_id,
                    "duration": 0,
                },
            }

        if kind == "kick":
            user_id = action.options.get("user_id", "")
            return {
                "action": "set_group_kick",
                "params": {
                    "group_id": int(target.id) if target.id.isdigit() else target.id,
                    "user_id": int(str(user_id)) if str(user_id).isdigit() else user_id,
                },
            }

        if kind == "poke":
            user_id = action.options.get("user_id", "")
            return {
                "action": "send_msg",
                "params": {
                    "message_type": "group" if target.kind == "group" else "private",
                    "group_id": int(target.id)
                    if target.kind == "group" and target.id.isdigit()
                    else target.id,
                    "message": [{"type": "poke", "data": {"qq": str(user_id)}}],
                },
            }

        # Fallback: noop or unknown
        return {"action": "send_msg", "params": {}}
