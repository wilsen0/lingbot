"""`/api/bots*` endpoints: list visible bots, hot-reload."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from linling_webui.deps import (
    Caller,
    get_state,
    require_auth,
    require_bot_visibility,
    require_role,
)
from linling_webui.schemas import BotStatus
from linling_webui.state import WebUIState

router = APIRouter(tags=["bots"])


class HotReloadResponse(BaseModel):
    """Result of ``POST /api/bots/{bot_id}/hot-reload``.

    Mirrors :class:`linling_cli.bootstrap.ReloadReport` plus the
    ``reloaded`` count for backward compat with the older callback
    interface that returned ``{"reloaded": int, "errors": [...]}``.
    """

    reloaded: int = 0
    files: int = 0
    applied: bool = True
    errors: list[str] = Field(default_factory=list)


@router.get("", response_model=list[BotStatus])
async def list_bots(
    caller: Caller = Depends(require_auth),
    state: WebUIState = Depends(get_state),
) -> list[BotStatus]:
    return [
        BotStatus(
            id=b.id,
            platform=b.platform,
            name=b.name,
            online=b.online,
            last_event_at=b.last_event_at,
        )
        for b in state.visible_bots(caller.bots)
    ]


@router.post("/{bot_id}/hot-reload", response_model=HotReloadResponse)
async def hot_reload(
    bot_id: str = Depends(require_bot_visibility),
    _admin: Caller = Depends(require_role("bot_admin")),
    state: WebUIState = Depends(get_state),
) -> HotReloadResponse:
    """Reload a bot's rule files. Requires ``bot_admin`` or higher.

    Hot-reload swaps the live classifier + DSL dispatcher, so it
    materially changes the bot's behaviour for every user. Read-only
    callers (``readonly`` role) can still see hits via ``/ws/rules/hits``
    but cannot trigger a reload.
    """
    cb = state.hot_reload_callback
    if cb is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "hot reload not configured")
    try:
        result = await cb(bot_id)
    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc
    if not isinstance(result, dict):
        return HotReloadResponse()
    payload: dict[str, Any] = result
    return HotReloadResponse(
        reloaded=int(payload.get("reloaded", 0) or 0),
        files=int(payload.get("files", 0) or 0),
        applied=bool(payload.get("applied", True)),
        errors=[str(e) for e in payload.get("errors", [])],
    )
