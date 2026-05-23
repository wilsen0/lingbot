"""Auth endpoints: login / refresh / logout / profile."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from linling_webui.auth import decode_token, issue_tokens
from linling_webui.config import WebUIConfig
from linling_webui.deps import Caller, get_config, get_state, require_auth
from linling_webui.schemas import (
    LoginRequest,
    ProfileResponse,
    RefreshRequest,
    TokenResponse,
)
from linling_webui.state import WebUIState

router = APIRouter(tags=["auth"])


def _client_ip(request: Request) -> str:
    # Behind a trusted reverse proxy, X-Forwarded-For would be preferred.
    return request.client.host if request.client else "unknown"


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    config: WebUIConfig = Depends(get_config),
    state: WebUIState = Depends(get_state),
) -> TokenResponse:
    """Exchange username + password for an access/refresh pair."""
    limiter = request.app.state.rate_limiter
    ip = _client_ip(request)
    if not limiter.check("login", ip, config.login_rate_per_minute, 60.0):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many login attempts")

    user = state.auth.verify_password(body.username, body.password)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    tokens = issue_tokens(
        user,
        secret=config.jwt_secret,
        algorithm=config.jwt_algorithm,
        access_ttl_s=config.access_token_ttl_s,
        refresh_ttl_s=config.refresh_token_ttl_s,
        store=state.auth,
    )
    return TokenResponse(
        access=tokens.access,
        refresh=tokens.refresh,
        access_expires_at=tokens.access_expires_at,
        refresh_expires_at=tokens.refresh_expires_at,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    body: RefreshRequest,
    config: WebUIConfig = Depends(get_config),
    state: WebUIState = Depends(get_state),
) -> TokenResponse:
    """Exchange a valid refresh token for a new pair (rotates the refresh)."""
    claims = decode_token(body.refresh, secret=config.jwt_secret, algorithm=config.jwt_algorithm)
    if claims is None or claims.get("typ") != "refresh":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid refresh token")
    jti = claims.get("jti")
    if not jti or not state.auth.is_refresh_valid(str(jti)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "refresh revoked or expired")

    user = state.auth.get_user(str(claims["sub"]))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user removed")

    # Rotate refresh: revoke the old jti, mint a fresh pair.
    state.auth.revoke_refresh(str(jti))
    tokens = issue_tokens(
        user,
        secret=config.jwt_secret,
        algorithm=config.jwt_algorithm,
        access_ttl_s=config.access_token_ttl_s,
        refresh_ttl_s=config.refresh_token_ttl_s,
        store=state.auth,
    )
    return TokenResponse(
        access=tokens.access,
        refresh=tokens.refresh,
        access_expires_at=tokens.access_expires_at,
        refresh_expires_at=tokens.refresh_expires_at,
    )


@router.post(
    "/logout",
    status_code=204,
    responses={204: {"description": "Refresh token revoked"}},
)
async def logout(
    body: RefreshRequest,
    config: WebUIConfig = Depends(get_config),
    state: WebUIState = Depends(get_state),
) -> None:
    """Revoke the supplied refresh token. Idempotent."""
    claims = decode_token(body.refresh, secret=config.jwt_secret, algorithm=config.jwt_algorithm)
    if claims is None:
        return None
    jti = claims.get("jti")
    if jti:
        state.auth.revoke_refresh(str(jti))
    return None


profile_router = APIRouter(tags=["auth"])


@profile_router.get("/profile", response_model=ProfileResponse)
async def profile(caller: Caller = Depends(require_auth)) -> ProfileResponse:
    return ProfileResponse(
        username=caller.username,
        role=caller.role,
        bots=caller.bots if caller.bots is not None else [],
    )
