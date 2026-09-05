"""`/ws/rules/hits` — live rule-hit stream.

Observes the :class:`AuditReader` for ``kind='handler_dispatch'`` appends
and fans each one out to the browser. Uses the reader's public
``subscribe`` API — no monkey-patching.
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from linling_webui.audit_reader import AuditReader, AuditRow
from linling_webui.auth import decode_token

router = APIRouter()


@router.websocket("/ws/rules/hits")
async def rules_ws(ws: WebSocket, token: str = Query(default="")) -> None:
    await ws.accept()
    config = ws.app.state.config
    claims = decode_token(token or "", secret=config.jwt_secret, algorithm=config.jwt_algorithm)
    if claims is None or claims.get("typ") != "access":
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    role = claims.get("role", "readonly")
    raw_bots = claims.get("bots")
    visible: set[str] | None = None if role == "superadmin" or raw_bots is None else set(raw_bots)

    state = ws.app.state.runtime
    if state.audit is None:
        state.audit = AuditReader()
    audit: AuditReader = state.audit

    # Bridge sync subscribe-callback → async WS.send via a bounded queue.
    # Bound the queue so a slow client doesn't grow unbounded memory;
    # overflow drops the oldest event.
    queue: asyncio.Queue[AuditRow] = asyncio.Queue(maxsize=256)

    def _on_append(row: AuditRow) -> None:
        # Must be fast and non-blocking; the AuditReader iterates
        # subscribers inline on every .append() call.
        if row.kind != "handler_dispatch":
            return
        # Token-scoped filter: clients can never observe rule hits from
        # bots they aren't allowed to see, even if a future audit row
        # would otherwise leak the existence of those bots.
        if visible is not None and row.bot_id not in visible:
            return
        try:
            queue.put_nowait(row)
        except asyncio.QueueFull:
            # Drop oldest, enqueue new — favour recency over completeness.
            try:
                queue.get_nowait()
                queue.put_nowait(row)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    unsubscribe = audit.subscribe(_on_append)
    await ws.send_json({"t": "hello", "server_time": int(time.time())})

    try:
        while True:
            row = await queue.get()
            await ws.send_json(
                {
                    "t": "hit",
                    "data": {
                        "id": row.id,
                        "time": row.time,
                        "bot_id": row.bot_id,
                        "handler": row.payload.get("handler")
                        if isinstance(row.payload, dict)
                        else None,
                        "outcome": row.outcome,
                        "latency_ms": row.latency_ms,
                    },
                }
            )
    except WebSocketDisconnect:
        pass
    finally:
        unsubscribe()
