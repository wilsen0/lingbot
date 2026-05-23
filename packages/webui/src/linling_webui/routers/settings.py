"""`/api/settings` — redacted config snapshot."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from linling_webui.config import WebUIConfig
from linling_webui.deps import Caller, get_config, redact_settings, require_auth

router = APIRouter(tags=["settings"])


class SettingsResponse(BaseModel):
    """Redacted config snapshot.

    The structure mirrors :class:`WebUIConfig` but secret-looking
    fields are replaced with ``"***"``. The shape is intentionally open
    (``extra="allow"``) so config additions ship without breaking the
    OpenAPI contract; the UI dereferences known keys defensively.
    """

    model_config = ConfigDict(extra="allow")
    role: str = Field(description="Authenticated caller's role.")


@router.get("", response_model=SettingsResponse)
async def get_settings(
    caller: Caller = Depends(require_auth),
    config: WebUIConfig = Depends(get_config),
) -> SettingsResponse:
    """Return a redacted view of the webui config.

    Secret-looking keys (api_key, token, jwt_secret, password, …) are
    replaced with ``***`` so the UI can render settings without leaking
    secrets to the browser console.
    """
    raw = config.model_dump(mode="json")
    redacted: dict[str, Any] = redact_settings(raw)
    redacted["role"] = caller.role
    return SettingsResponse.model_validate(redacted)
