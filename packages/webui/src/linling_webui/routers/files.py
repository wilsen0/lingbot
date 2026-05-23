"""`/api/files*` — serve bot-local image assets to the WebUI.

QRDic rule files reference images via the legacy filesystem path
``/storage/emulated/0/QR/QRDic/data/picture/...``. The DSL VM emits
those as :class:`ImageSegment` URLs verbatim, but a browser can't
fetch ``/storage/...``. This router maps the path tail (``picture/X.jpg``,
``image/Y.png``, etc.) to a real file under the bot's
``<base_dir>/QRDic/data/`` tree and streams it back.

The same router also exposes a tightly-scoped image proxy
(``/api/files/proxy?url=https://...``) so DSL-emitted *remote*
``±img=https://...±`` URLs render even when the WebUI's CSP keeps
``img-src`` to ``'self'``. The proxy:

* only fetches over ``http(s)``,
* refuses anything whose response ``Content-Type`` isn't an image,
* enforces a hard size + timeout cap,
* never sends cookies or auth headers, and
* doesn't follow redirects past a small chain.

Security model:

* The asset root is fixed at attach time to ``<bot_base_dir>/QRDic``
  (or whatever the bot config's ``storage.files`` points at when we
  expand that). Requests are confined to that directory.
* Path traversal (``..``) is rejected up-front — we resolve and
  re-check against the root after :meth:`Path.resolve`.
* Only the well-known asset extensions (``.jpg`` / ``.png`` /
  ``.gif`` / ``.webp`` / ``.jpeg``) are served. Anything else 404s.
* No write surface — read-only.

The endpoint is unauthenticated by design: in the deployments we
care about the WebUI is already behind the same auth gate as the
SPA, and embedding a token in ``<img src=>`` is awkward. If a
deployment puts the WebUI on the public internet they should fence
the asset path at the reverse proxy.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import FileResponse, Response

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["files"])


# Accepted image extensions. We render images inside chat bubbles, so
# anything fancy (svg, ico) gets 404'd to keep the surface small.
_ALLOWED_EXT = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp"})

# MIME map for the responses; FileResponse can guess too but we set
# it explicitly so caching and CSP behave deterministically.
_MIME_BY_EXT: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# Proxy hard limits. These are deliberately conservative — the proxy
# is for chat-bubble thumbnails, not arbitrary downloads.
_PROXY_TIMEOUT_S = 10.0
_PROXY_MAX_BYTES = 8 * 1024 * 1024  # 8 MiB; a large badge fits in well under this
_PROXY_ALLOWED_PREFIXES = ("image/",)
_PROXY_REDIRECT_LIMIT = 3


@router.get("/qrdic/{rel:path}")
async def get_qrdic_asset(rel: str) -> Response:
    """Stream an image from the bot's ``QRDic/data`` tree.

    Sample request: ``GET /api/files/qrdic/picture/思思.jpg`` →
    ``<bot_base_dir>/QRDic/data/picture/思思.jpg``.

    The asset root is plumbed via ``app.state.runtime.qrdic_asset_root``
    (set by :func:`attach_bot_to_webui`). Without a root configured we
    return 404 rather than guessing — same as a missing file.
    """

    # We can't easily get the request via dependency injection at the
    # top-level decorator without expanding the signature, so reach
    # into the active request through ``starlette.requests`` later.
    # Simpler: read the asset root from a module-level setter wired
    # at attach time.
    root = _ASSET_ROOT.get()
    if root is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "qrdic asset root not configured")

    target = (root / rel).resolve()
    if not _is_within(target, root.resolve()):
        # Path-traversal attempt or absolute path slipped past the
        # router. 404 — never reveal *why* it failed.
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if target.suffix.lower() not in _ALLOWED_EXT:
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    media_type = _MIME_BY_EXT.get(target.suffix.lower(), "application/octet-stream")
    # 1 hour cache: assets rarely change, but the bot operator may
    # refresh them out-of-band — we don't want to pin them forever.
    return FileResponse(
        target,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


@router.get("/proxy")
async def proxy_remote_image(
    url: str = Query(..., min_length=8, max_length=2048),
) -> Response:
    """Stream a remote image through the WebUI so strict CSP can serve it.

    DSL rules emit lots of ``±img=https://...±`` URLs (108 distinct
    hosts in the migrated ``dicpro.txt``). With ``img-src 'self'`` —
    the WebUI's default CSP — the browser refuses to render any of
    them. Rather than relax CSP we route them through here:

    * fetch over ``http(s)`` only (other schemes are rejected),
    * cap the response size and time,
    * verify the upstream ``Content-Type`` is an image, and
    * strip credentials / cookies, never follow more than a few redirects.

    On any failure we return 502 with no body — the client just sees
    a broken ``<img>`` placeholder, which is the same UX as a remote
    host being temporarily down.
    """

    if not url.startswith(("http://", "https://")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "scheme must be http(s)")

    # ``follow_redirects=True`` with a low cap — some image CDNs do
    # one or two hops. Disabling cookies isn't necessary because we
    # don't carry any, but using a fresh client per call keeps the
    # connection pool predictable.
    try:
        async with httpx.AsyncClient(
            timeout=_PROXY_TIMEOUT_S,
            follow_redirects=True,
            max_redirects=_PROXY_REDIRECT_LIMIT,
            headers={"User-Agent": "linling-webui/1.0 (+image proxy)"},
        ) as client:
            upstream = await client.get(url)
    except httpx.HTTPError as exc:
        logger.info("files.proxy_fetch_failed", url=url, error=str(exc))
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "upstream fetch failed") from exc

    if upstream.status_code >= 400:
        logger.info("files.proxy_upstream_status", url=url, status=upstream.status_code)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "upstream returned error")

    content_type = (upstream.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if not any(content_type.startswith(prefix) for prefix in _PROXY_ALLOWED_PREFIXES):
        logger.info("files.proxy_bad_content_type", url=url, content_type=content_type)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "upstream is not an image")

    body = upstream.content
    if len(body) > _PROXY_MAX_BYTES:
        logger.info("files.proxy_too_large", url=url, bytes=len(body))
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "upstream image too large")

    # 1 hour cache; remote assets rarely change but operators may
    # rotate them out-of-band, so don't pin them forever.
    return Response(
        content=body,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )


class _AssetRoot:
    """Process-singleton holder for the active QRDic asset root.

    Avoids the FastAPI ``app.state`` indirection in the route handler
    (which would force every request to look it up). The bootstrap
    sets this once at attach time; subsequent reloads or attach calls
    overwrite it.
    """

    def __init__(self) -> None:
        self._root: Path | None = None

    def set(self, root: Path | None) -> None:
        self._root = root.resolve() if root is not None else None

    def get(self) -> Path | None:
        return self._root


_ASSET_ROOT = _AssetRoot()


def set_qrdic_asset_root(root: Path | None) -> None:
    """Wire the on-disk root from which ``/api/files/qrdic/...`` serves.

    Called by :func:`attach_bot_to_webui` after the bot is bootstrapped.
    Pass ``None`` to disable serving (the endpoint will start 404'ing).
    """
    _ASSET_ROOT.set(root)
