"""Health & version endpoint.

Non-sensitive, unauthenticated. Returns per-bot online status so the
SPA can show the running state without a login.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends

from linling_webui.deps import get_state
from linling_webui.schemas import BotStatus, HealthResponse
from linling_webui.state import WebUIState
from linling_webui.version import __version__

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(state: WebUIState = Depends(get_state)) -> HealthResponse:
    bots = [
        BotStatus(
            id=b.id,
            platform=b.platform,
            name=b.name,
            online=b.online,
            last_event_at=b.last_event_at,
        )
        for b in state.bots.values()
    ]
    return HealthResponse(
        status="ok",
        version=__version__,
        time=datetime.now(UTC).isoformat(),
        bots=bots,
    )
