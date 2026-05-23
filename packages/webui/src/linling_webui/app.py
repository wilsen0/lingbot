"""FastAPI app factory for linling-webui.

`create_app(config)` wires:
- CORS (default same-origin)
- Security headers (CSP, XCTO, Frame-Options, Referrer-Policy)
- Auth store (sqlite) + in-memory rate limiter
- `app.state.runtime: WebUIState` — a container for bus / kv / scheduler /
  agents that the hosting process can fill in via `wire_*` helpers.
- REST routers: health, auth
- SPA static mount (if `static_dir/index.html` exists)
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from linling_webui.auth import AuthStore
from linling_webui.config import WebUIConfig
from linling_webui.middleware import SecurityHeadersMiddleware
from linling_webui.rate_limit import RateLimiter
from linling_webui.routers import agents as agents_router
from linling_webui.routers import audit as audit_router
from linling_webui.routers import auth as auth_router
from linling_webui.routers import bots as bots_router
from linling_webui.routers import events as events_router
from linling_webui.routers import files as files_router
from linling_webui.routers import health as health_router
from linling_webui.routers import kv as kv_router
from linling_webui.routers import rules as rules_router
from linling_webui.routers import settings as settings_router
from linling_webui.state import WebUIState
from linling_webui.version import __version__
from linling_webui.ws import agents as ws_agents
from linling_webui.ws import events as ws_events
from linling_webui.ws import rules as ws_rules


def create_app(config: WebUIConfig | None = None) -> FastAPI:
    """Build and return the FastAPI app."""
    config = config or WebUIConfig()

    app = FastAPI(
        title="linling-webui",
        version=__version__,
        root_path=config.root_path,
        docs_url="/api/_docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.config = config
    app.state.rate_limiter = RateLimiter()

    auth_store = AuthStore(config.auth_db_path)
    app.state.runtime = WebUIState(auth=auth_store)

    # Warn loud and early if the operator left the demo placeholder.
    # Continuing to boot is the right call (some integration tests want
    # to override the secret post-construction), but the warning makes
    # the misconfiguration impossible to miss in operator logs.
    if config.jwt_secret in ("change-me-in-prod", ""):
        import logging  # noqa: PLC0415

        logging.getLogger("linling_webui").warning(
            "jwt_secret is set to the demo placeholder; "
            "set LINLING_WEBUI_JWT_SECRET to a strong value in production."
        )

    # ---- Middleware ---------------------------------------------------
    app.add_middleware(SecurityHeadersMiddleware)
    if config.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # ---- API routers --------------------------------------------------
    app.include_router(health_router.router, prefix="/api")
    app.include_router(auth_router.router, prefix="/api/auth")
    app.include_router(auth_router.profile_router, prefix="/api")
    app.include_router(bots_router.router, prefix="/api/bots")
    app.include_router(events_router.router, prefix="/api/events")
    app.include_router(kv_router.router, prefix="/api/kv")
    app.include_router(rules_router.router, prefix="/api/rules")
    app.include_router(agents_router.router, prefix="/api/agents")
    app.include_router(audit_router.router, prefix="/api/audit")
    app.include_router(settings_router.router, prefix="/api/settings")
    app.include_router(files_router.router, prefix="/api/files")

    # ---- WebSocket routers -------------------------------------------
    app.include_router(ws_events.router)
    app.include_router(ws_agents.router)
    app.include_router(ws_rules.router)

    # ---- SPA (only if built) -----------------------------------------
    static_dir = Path(config.static_dir)
    if (static_dir / "index.html").exists():
        # Only pull these in when a SPA bundle is actually present; a
        # headless API-only deployment doesn't need the response helpers.
        from fastapi.responses import FileResponse, Response  # noqa: PLC0415

        favicon = static_dir / "favicon.svg"
        index_html = static_dir / "index.html"

        # Asset paths served straight from disk (long-term cacheable — hashed).
        app.mount(
            "/assets",
            StaticFiles(directory=static_dir / "assets"),
            name="spa_assets",
        )

        @app.get("/favicon.svg", include_in_schema=False)
        async def _favicon() -> Response:
            if favicon.exists():
                return FileResponse(favicon, media_type="image/svg+xml")
            return Response(status_code=404)

        @app.get("/{path:path}", include_in_schema=False)
        async def _spa_fallback(path: str) -> FileResponse:
            # /api/* and /ws/* are already handled by routers above; this
            # only catches SPA routes (/, /login, /kv, /agents/susu, …).
            target = static_dir / path
            if target.is_file():
                return FileResponse(target)
            # index.html must never be cached — it pins the asset hashes.
            return FileResponse(
                index_html,
                media_type="text/html; charset=utf-8",
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate",
                    "Pragma": "no-cache",
                },
            )

    return app
