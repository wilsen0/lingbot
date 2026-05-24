"""`/api/events*` endpoints — browse and replay buffered events."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict

from linling_webui.audit_reader import AuditReader
from linling_webui.buffers import BufferedEvent
from linling_webui.config import WebUIConfig
from linling_webui.deps import (
    Caller,
    get_config,
    get_state,
    require_auth,
    verify_bot_visibility,
)
from linling_webui.schemas import EventEnvelope, EventPage
from linling_webui.state import WebUIState

router = APIRouter(tags=["events"])


def _to_envelope(be: BufferedEvent) -> EventEnvelope:
    ev = be.event
    return EventEnvelope(
        seq=be.seq,
        id=ev.id,
        platform=ev.platform,
        bot_id=ev.bot_id,
        scope=ev.scope.model_dump(),
        sender=ev.sender.model_dump(),
        time=ev.time.isoformat(),
        kind=ev.kind,
        segments=[s.model_dump() for s in ev.segments],
        text=ev.text,
    )


@router.get("", response_model=EventPage)
async def list_events(
    bot_id: str | None = Query(default=None),
    since_seq: int | None = Query(default=None, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    kind: str | None = Query(default=None),
    scope_kind: str | None = Query(default=None),
    mine: bool = Query(default=False),
    caller: Caller = Depends(require_auth),
    state: WebUIState = Depends(get_state),
    config: WebUIConfig = Depends(get_config),
) -> EventPage:
    """Return events from one or all visible bots' ring buffers."""
    if bot_id is not None:
        verify_bot_visibility(caller, bot_id)
        bot_ids = [bot_id]
    else:
        bot_ids = [b.id for b in state.visible_bots(caller.bots)]

    items: list[BufferedEvent] = []
    for bid in bot_ids:
        buf = state.event_buffers.get(bid)
        if buf is None:
            continue
        tail_limit = buf.capacity if (kind or scope_kind or mine) else limit
        tail = await buf.tail(since_seq=since_seq, limit=tail_limit)
        items.extend(tail)

    # Keep relative ordering: each buffer is already ordered by seq, merge by time.
    items.sort(key=lambda it: it.event.time)

    if kind:
        items = [it for it in items if it.event.kind == kind]
    if scope_kind:
        items = [it for it in items if it.event.scope.kind == scope_kind]
    if mine:
        items = [it for it in items if it.event.sender.id == caller.username]
    if len(items) > limit:
        items = items[-limit:]

    next_cursor = items[-1].seq if items else None
    # Defensive: don't expose buffered events beyond per-bot capacity.
    _ = config  # reserved for future per-query caps
    return EventPage(items=[_to_envelope(be) for be in items], next_cursor=next_cursor)


@router.get("/{bot_id}/{event_id}", response_model=EventEnvelope)
async def get_event(
    bot_id: str,
    event_id: str,
    caller: Caller = Depends(require_auth),
    state: WebUIState = Depends(get_state),
) -> EventEnvelope:
    verify_bot_visibility(caller, bot_id)
    buf = state.event_buffers.get(bot_id)
    if buf is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown bot buffer")
    tail = await buf.tail(limit=buf.capacity)
    for be in tail:
        if be.event.id == event_id:
            return _to_envelope(be)
    raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found in buffer")


class _ReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dry_run: bool = True


class _ReplayResponse(BaseModel):
    ok: bool
    message: str
    dry_run: bool = True


@router.post("/{bot_id}/{event_id}/replay", response_model=_ReplayResponse)
async def replay_event(
    bot_id: str,
    event_id: str,
    body: _ReplayRequest,
    caller: Caller = Depends(require_auth),
    state: WebUIState = Depends(get_state),
) -> _ReplayResponse:
    """Re-dispatch a buffered event through the router in dry-run mode.

    Only dry-run is supported (``dry_run=True``). Actual re-sending to an
    adapter would bypass rate-limits and idempotency — we refuse.
    """
    verify_bot_visibility(caller, bot_id)
    if not body.dry_run:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "only dry_run=true is supported")
    buf = state.event_buffers.get(bot_id)
    if buf is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown bot buffer")
    tail = await buf.tail(limit=buf.capacity)
    target = next((be for be in tail if be.event.id == event_id), None)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found in buffer")

    # Record an audit entry so the replay is visible in the trace.
    if state.audit is None:
        state.audit = AuditReader()
    state.audit.append(
        bot_id=bot_id,
        user_id=caller.username,
        scope_id=f"events/{event_id}",
        kind="event_replay",
        outcome="ok",
        payload={"dry_run": True, "text": target.event.text},
    )
    return _ReplayResponse(ok=True, message="replay recorded (dry-run)", dry_run=True)
