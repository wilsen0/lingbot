"""FastAPI dependency helpers.

Centralised so routers stay slim:

- `get_config` / `get_state` — pull things off `app.state`.
- `require_auth` — parse & verify the Bearer JWT.
- `require_role` — ensure the caller has ≥ required role.
- `require_bot_visibility` — enforce jwt.bots for per-bot endpoints.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, Header, HTTPException, Request, status

from linling_webui.auth import Role, decode_token
from linling_webui.config import WebUIConfig
from linling_webui.state import WebUIState


@dataclass
class Caller:
    """The authenticated identity of the current request."""

    username: str
    role: Role
    bots: list[str] | None  # None = "all" (superadmin)

    @property
    def is_superadmin(self) -> bool:
        return self.role == "superadmin"


_ROLE_RANK: dict[str, int] = {"readonly": 0, "bot_admin": 1, "superadmin": 2}


def get_config(request: Request) -> WebUIConfig:
    return request.app.state.config  # type: ignore[no-any-return]


def get_state(request: Request) -> WebUIState:
    return request.app.state.runtime  # type: ignore[no-any-return]


def require_auth(
    authorization: str | None = Header(default=None),
    config: WebUIConfig = Depends(get_config),
) -> Caller:
    """Parse & verify the Authorization: Bearer <jwt> header."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    claims = decode_token(token, secret=config.jwt_secret, algorithm=config.jwt_algorithm)
    if claims is None or claims.get("typ") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token")
    role = claims.get("role", "readonly")
    bots = claims.get("bots")
    return Caller(
        username=str(claims["sub"]),
        role=role,
        bots=None if role == "superadmin" or bots is None else list(bots),
    )


def require_role(min_role: Role) -> Callable[[Caller], Caller]:
    """Require the caller to have ``min_role`` or higher."""

    required = _ROLE_RANK[min_role]

    def _dep(caller: Caller = Depends(require_auth)) -> Caller:
        if _ROLE_RANK.get(caller.role, -1) < required:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient role")
        return caller

    return _dep


def verify_bot_visibility(caller: Caller, bot_id: str) -> None:
    """Raise 404 if bot_id is not in caller.bots (treat as unknown for privacy)."""
    if caller.bots is None:
        return  # superadmin sees all
    if bot_id not in set(caller.bots):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "bot not found")


def require_bot_visibility(bot_id: str, caller: Caller = Depends(require_auth)) -> str:
    """Dependency for `/api/bots/{bot_id}/...`: checks visibility, returns bot_id."""
    verify_bot_visibility(caller, bot_id)
    return bot_id


def redact_settings(raw: dict[str, Any]) -> dict[str, Any]:
    """Recursively mask secret-looking fields in a config-shaped dict.

    Used by `GET /api/settings` to uphold WUI-C8.
    """
    SECRET_KEYS = {
        "api_key",
        "apikey",
        "access_token",
        "accesstoken",
        "token",
        "secret",
        "password",
        "jwt_secret",
    }
    if isinstance(raw, dict):
        out: dict[str, Any] = {}
        for k, v in raw.items():
            if isinstance(k, str) and k.lower() in SECRET_KEYS:
                out[k] = "***" if v else ""
            else:
                out[k] = redact_settings(v)
        return out
    if isinstance(raw, list):
        return [redact_settings(v) for v in raw]
    return raw
