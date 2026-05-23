"""`/ws/events` — live tail of per-bot ring buffers.

Wire protocol (JSON):

client → server:
    {"t": "filter", "data": {"bots": [...], "since_seq": 12, "kind": "message"}}
    {"t": "ping"}

server → client:
    {"t": "hello", "server_time": 169..., "capacity": 500}
    {"t": "event", "bot_id": "susu_main", "data": {...envelope...}}
    {"t": "ping"}           # server-initiated heartbeat (every 25s)
    {"t": "filter_ack", "replayed": N}

Errors close the socket with code 1008.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from linling_webui.auth import decode_token
from linling_webui.buffers import BufferedEvent
from linling_webui.routers.events import _to_envelope

router = APIRouter()

# Heartbeat cadence — server sends every 25s; client gets no-op every 15s.
SERVER_PING_INTERVAL_S = 25.0


async def _authorize(ws: WebSocket, token: str | None) -> tuple[dict[str, Any], list[str] | None]:
    """Return (claims, visible_bot_ids) or close the socket."""
    config = ws.app.state.config
    claims = decode_token(token or "", secret=config.jwt_secret, algorithm=config.jwt_algorithm)
    if claims is None or claims.get("typ") != "access":
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return {}, None
    role = claims.get("role", "readonly")
    bots = claims.get("bots")
    visible = None if role == "superadmin" or bots is None else list(bots)
    return claims, visible


@router.websocket("/ws/events")
async def events_ws(ws: WebSocket, token: str = Query(default="")) -> None:
    await ws.accept()
    claims, visible = await _authorize(ws, token)
    if not claims:
        return

    state = ws.app.state.runtime
    config = ws.app.state.config

    # Determine initial target bot set; auto-create buffers for registered bots
    # so that later publishes also flow to this client.
    all_known = list(state.bots.keys()) or list(state.event_buffers.keys())
    if visible is None:
        target_bots = all_known
    else:
        target_bots = [b for b in visible if b in all_known or b in state.event_buffers]
    for bid in target_bots:
        state.buffer_for(bid, capacity=config.event_buffer_size)

    await ws.send_json(
        {"t": "hello", "server_time": int(time.time()), "capacity": config.event_buffer_size}
    )

    send_lock = asyncio.Lock()

    async def deliver(buffered: BufferedEvent, bot_id: str) -> None:
        envelope = _to_envelope(buffered).model_dump()
        async with send_lock:
            with contextlib.suppress(Exception):
                await ws.send_json({"t": "event", "bot_id": bot_id, "data": envelope})

    # Subscribe to live updates on all target buffers.
    unsubscribes: list[Callable[[], None]] = []

    def _subscribe(bid: str) -> None:
        buf = state.event_buffers.get(bid)
        if buf is None:
            return

        async def _cb(be: BufferedEvent) -> None:
            await deliver(be, bid)

        unsubscribes.append(buf.subscribe(_cb))

    for bid in target_bots:
        _subscribe(bid)

    async def _heartbeat() -> None:
        try:
            while True:
                await asyncio.sleep(SERVER_PING_INTERVAL_S)
                async with send_lock:
                    await ws.send_json({"t": "ping"})
        except Exception:
            return

    hb_task = asyncio.create_task(_heartbeat())

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = msg.get("t")
            if kind == "ping":
                async with send_lock:
                    await ws.send_json({"t": "ping"})
                continue
            if kind == "filter":
                data = msg.get("data", {}) or {}
                since_seq = data.get("since_seq")
                want_bots = data.get("bots")
                target = target_bots
                if isinstance(want_bots, list):
                    if visible is not None:
                        vset = set(visible)
                        target = [b for b in want_bots if b in vset and b in state.event_buffers]
                    else:
                        target = [b for b in want_bots if b in state.event_buffers]
                replayed = 0
                if since_seq is not None:
                    for bid in target:
                        buf = state.event_buffers.get(bid)
                        if buf is None:
                            continue
                        items = await buf.tail(since_seq=int(since_seq), limit=buf.capacity)
                        for it in items:
                            await deliver(it, bid)
                            replayed += 1
                async with send_lock:
                    await ws.send_json({"t": "filter_ack", "replayed": replayed})
                continue
    except WebSocketDisconnect:
        pass
    finally:
        hb_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await hb_task
        for u in unsubscribes:
            with contextlib.suppress(Exception):
                u()
