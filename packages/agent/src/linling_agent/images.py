"""Image content resolution for multimodal (vision) messages.

Resolves image references (OneBot ``image``/``mface``/``bface`` segment
URLs) into ``data:`` URIs suitable for OpenAI-style multimodal content
parts. Results are cached in-memory so a sticker re-sent within a batch
window isn't re-downloaded.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections import OrderedDict
from pathlib import Path
from urllib.parse import unquote

import httpx
import structlog
from linling_core.segments import FaceSegment, ImageSegment, Segment

logger = structlog.get_logger(__name__)


def _detect_mime(data: bytes) -> str:
    """Best-effort MIME sniffing from magic bytes.

    Only the formats OneBot/QQ actually emit are recognised; anything
    else falls back to ``image/jpeg`` because every OpenAI-compatible
    vision endpoint we target accepts JPEG payloads inside ``data:``
    URIs, so a wrong guess never hard-fails the request.
    """
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


class ImageContentResolver:
    """Resolve image references to ``data:`` URIs with an in-memory cache."""

    MAX_BYTES = 4 * 1024 * 1024  # 单图上限
    TIMEOUT_S = 4.0
    MAX_IMAGE_PARTS = 4  # 每次请求最多附图数
    _CACHE_MAX = 64

    def __init__(self) -> None:
        # LRU 缓存:url -> data_uri (str) 或 None(失败)。OrderedDict 保序便于淘汰最旧。
        self._cache: OrderedDict[str, str | None] = OrderedDict()
        # 并发去重:同一 URL 的 in-flight 下载共享一个 Future,避免批量里重复 URL 重复下载。
        self._inflight: dict[str, asyncio.Future[str | None]] = {}
        self._http_client: httpx.AsyncClient | None = None

    async def resolve(self, raw_url: str) -> str | None:
        """Resolve a single image reference to a ``data:`` URI, or None.

        Every failure mode degrades to ``None`` so the caller can fall
        back to dropping the image part; this method never raises.
        ``asyncio.CancelledError`` is the one exception — it propagates
        so the batch pipeline can abort promptly.
        """
        url = (raw_url or "").strip()
        if not url:
            return None

        # LRU hit: ``move_to_end`` re-warms the entry so the eviction
        # boundary never touches frequently re-sent stickers. The value
        # may legitimately be ``None`` (a cached failure) — that is
        # still a hit, which is why we check membership rather than
        # ``.get()`` (whose ``None`` default would look like a miss).
        if url in self._cache:
            cached = self._cache[url]
            self._cache.move_to_end(url)
            return cached

        # Concurrent dedup: a sticker appearing in several messages of
        # one batch yields the same URL repeatedly. Join the in-flight
        # download instead of starting a second one.
        future = self._inflight.get(url)
        if future is not None:
            return await future

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._inflight[url] = future
        try:
            try:
                result = await self._resolve_uncached(url)
            except asyncio.CancelledError:
                # We own the shared future; settling it first keeps any
                # coroutine that deduped onto us from hanging forever,
                # then we let cancellation reach the caller.
                if not future.done():
                    future.set_result(None)
                raise
            except Exception as exc:
                # Safety net for anything unexpected (weird encodings,
                # httpx internals): degrade to None, never raise.
                logger.warning(
                    "image_resolve_failed",
                    url=url,
                    error=type(exc).__name__,
                    exc_info=True,
                )
                result = None
        finally:
            # Only the owning task reaches here; waiters that joined the
            # future never touch ``_inflight``.
            self._inflight.pop(url, None)
        if not future.done():
            future.set_result(result)
        self._cache[url] = result
        if len(self._cache) > self._CACHE_MAX:
            # OrderedDict iterates in insertion order and hits
            # (``move_to_end``) keep hot entries at the tail, so the
            # head is always the least-recently-used one.
            self._cache.popitem(last=False)
        return result

    async def resolve_batch(self, urls: list[str], *, limit: int | None = None) -> list[str]:
        """Resolve a list of URLs to data URIs, deduped and capped.

        Deduplicates on the input (each distinct URL is fetched at most
        once across the whole batch — the cache makes repeats free),
        keeps input order, and returns at most ``limit`` entries
        (default :attr:`MAX_IMAGE_PARTS`). Failed/oversized references
        are skipped rather than failing the batch, so a short batch
        never breaks the request; scanning continues past failures until
        the cap is filled.
        """
        cap = self.MAX_IMAGE_PARTS if limit is None else max(0, int(limit))
        results: list[str] = []
        seen: set[str] = set()
        for raw in urls:
            url = (raw or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            result = await self.resolve(url)
            if result is not None:
                results.append(result)
                if len(results) >= cap:
                    break
        return results

    async def aclose(self) -> None:
        """Close the lazily-created HTTP client (idempotent)."""
        client = self._http_client
        if client is not None:
            # Null the reference before closing so a concurrent/repeated
            # ``aclose`` never double-closes the same pool.
            self._http_client = None
            await client.aclose()

    async def _resolve_uncached(self, url: str) -> str | None:
        """Convert one reference to a ``data:`` URI, caches aside."""
        if url.startswith("base64://"):
            # OneBot's legacy ``base64://`` transport: everything after
            # the prefix is already base64. Keep the payload verbatim in
            # the data URI; only the MIME type needs sniffing.
            payload = url[len("base64://") :]
            try:
                data = base64.b64decode(payload, validate=True)
            except (binascii.Error, ValueError) as exc:
                logger.warning(
                    "image_base64_invalid",
                    url=url,
                    error=type(exc).__name__,
                )
                return None
            if not data:
                return None
            mime = _detect_mime(data)
            return f"data:{mime};base64,{payload}"
        if url.startswith("data:"):
            # Some adapters already forward content inline as a data
            # URI; nothing to do.
            return url
        if url.startswith("http://") or url.startswith("https://"):
            return await self._download_and_encode(url)
        if url.startswith("file://"):
            # ``file://`` URLs carry a filesystem path after the prefix;
            # strip it and any percent-encoding, then read from disk.
            path = unquote(url[len("file://") :])
            return await self._read_and_encode(path)
        if "://" in url:
            # Unknown scheme (``ftp://``, ``magnet:``, ...) — no
            # download strategy, so drop it rather than guess.
            logger.info("image_unsupported_scheme", url=url)
            return None
        # No scheme at all: treat the reference as a plain local
        # filesystem path. Path traversal is deliberately not defended —
        # references come from trusted internal segments and are already
        # operator-scoped.
        return await self._read_and_encode(url)

    async def _download_and_encode(self, url: str) -> str | None:
        """Stream-download ``url`` and return a ``data:`` URI, or None.

        Sized on the fly: the body is accumulated in chunks and aborted
        as soon as it exceeds :attr:`MAX_BYTES`, so an oversized remote
        image never gets buffered in full. All network failures degrade
        to ``None``.
        """
        client = self._ensure_http_client()
        try:
            async with client.stream("GET", url, timeout=self.TIMEOUT_S) as response:
                if response.status_code >= 400:
                    logger.warning(
                        "image_http_status",
                        url=url,
                        status=response.status_code,
                    )
                    return None
                buf = bytearray()
                async for chunk in response.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > self.MAX_BYTES:
                        logger.info(
                            "image_http_too_large",
                            url=url,
                            bytes_so_far=len(buf),
                            max_bytes=self.MAX_BYTES,
                        )
                        return None
        except (httpx.HTTPError, TimeoutError) as exc:
            logger.warning(
                "image_http_fetch_failed",
                url=url,
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )
            return None
        if not buf:
            return None
        mime = _detect_mime(bytes(buf))
        payload = base64.b64encode(bytes(buf)).decode("ascii")
        return f"data:{mime};base64,{payload}"

    def _ensure_http_client(self) -> httpx.AsyncClient:
        """Lazy-init the download client.

        The connection pool is created only on first remote download so
        pure ``base64://``/local-path resolution never pays for it.
        ``follow_redirects=True`` matches the adapter's behaviour —
        image hosts commonly 302 to a CDN.
        """
        client = self._http_client
        if client is None:
            client = httpx.AsyncClient(
                follow_redirects=True,
                timeout=self.TIMEOUT_S,
                # A small pool — a batch touches at most MAX_IMAGE_PARTS
                # images concurrently; the bot is not a scraper.
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            )
            self._http_client = client
        return client

    async def _read_and_encode(self, path: str) -> str | None:
        """Read a local file and return its ``data:`` URI, or None."""
        try:
            # ``Path.read_bytes`` is blocking; hop off the event loop so
            # a slow disk (or a network-mounted cache dir) never stalls
            # the whole batch pipeline.
            data = await asyncio.to_thread(Path(path).read_bytes)
        except (OSError, ValueError) as exc:
            logger.warning(
                "image_local_read_failed",
                path=path,
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )
            return None
        if not data or len(data) > self.MAX_BYTES:
            logger.info(
                "image_local_skipped",
                path=path,
                bytes_=len(data),
                max_bytes=self.MAX_BYTES,
            )
            return None
        mime = _detect_mime(data)
        payload = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{payload}"


def collect_image_refs(segments: list[Segment]) -> list[str]:
    """Collect image reference URLs from ``segments``, deduped, in order.

    Ordinary images come from ``ImageSegment.url``. Stickers (OneBot
    ``mface``/``bface``) surface their source through the ``url`` extra
    on ``FaceSegment``; plain QQ system emoji carry only a ``face_id``
    and no URL, so they naturally fall out of the result.
    """
    refs: list[str] = []
    seen: set[str] = set()
    for seg in segments:
        url: object = ""
        if isinstance(seg, ImageSegment) and seg.url is not None:
            url = seg.url
        elif isinstance(seg, FaceSegment):
            # ``extras`` is ``dict[str, object]``; accept only a str
            # value in case some platform stashes something else under
            # the "url" key.
            url = seg.extras.get("url", "")
        if isinstance(url, str) and url and url not in seen:
            seen.add(url)
            refs.append(url)
    return refs
