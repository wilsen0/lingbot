"""Tests for image reference resolution and image-ref collection.

Covers the three transport paths in ``ImageContentResolver``
(``base64://`` payloads, ``data:`` passthrough, local files and HTTP
downloads), the MIME sniffing, batching/dedup, the LRU cache and the
``collect_image_refs`` segment scanner.
"""

from __future__ import annotations

import base64
import functools
import http.server
import socketserver
import threading
from pathlib import Path
from urllib.parse import quote

import pytest
from linling_agent.images import ImageContentResolver, _detect_mime, collect_image_refs
from linling_core.segments import FaceSegment, ImageSegment, TextSegment

# Magic bytes for the formats ``_detect_mime`` recognises.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 16
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def _b64url(data: bytes) -> str:
    """OneBot-style ``base64://`` reference for ``data``."""
    return "base64://" + base64.b64encode(data).decode("ascii")


def _payload(uri: str) -> bytes:
    return base64.b64decode(uri.split(",", 1)[1])


@pytest.fixture
async def resolver():
    r = ImageContentResolver()
    try:
        yield r
    finally:
        await r.aclose()


# ---------------------------------------------------------------------------
# Localhost HTTP server (real download path)
# ---------------------------------------------------------------------------


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass


class _LocalHTTPServer:
    """Serve the files under ``root`` from a localhost HTTP server thread."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def start(self) -> None:
        handler = functools.partial(_QuietHandler, directory=str(self._root))
        self._httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
        self._port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def http_server(tmp_path: Path) -> _LocalHTTPServer:
    server = _LocalHTTPServer(tmp_path)
    server.start()
    yield server
    server.stop()


# ---------------------------------------------------------------------------
# MIME sniffing
# ---------------------------------------------------------------------------


def test_detect_mime() -> None:
    assert _detect_mime(PNG) == "image/png"
    assert _detect_mime(GIF) == "image/gif"
    assert _detect_mime(WEBP) == "image/webp"
    assert _detect_mime(JPEG) == "image/jpeg"
    assert _detect_mime(b"\x00" * 64) == "image/jpeg"  # unknown -> jpeg fallback


# ---------------------------------------------------------------------------
# base64:// resolution
# ---------------------------------------------------------------------------


async def test_resolve_base64_png(resolver) -> None:
    uri = await resolver.resolve(_b64url(PNG))
    assert uri is not None
    assert uri.startswith("data:image/png;base64,")
    assert _payload(uri) == PNG


async def test_resolve_base64_gif(resolver) -> None:
    uri = await resolver.resolve(_b64url(GIF))
    assert uri is not None
    assert uri.startswith("data:image/gif;base64,")
    assert _payload(uri) == GIF


async def test_resolve_base64_webp(resolver) -> None:
    uri = await resolver.resolve(_b64url(WEBP))
    assert uri is not None
    assert uri.startswith("data:image/webp;base64,")
    assert _payload(uri) == WEBP


async def test_resolve_base64_default_jpeg(resolver) -> None:
    # Bytes with no recognisable magic fall back to image/jpeg.
    uri = await resolver.resolve(_b64url(b"\x00\x01\x02" * 32))
    assert uri is not None
    assert uri.startswith("data:image/jpeg;base64,")


async def test_resolve_invalid_base64(resolver) -> None:
    # Garbage after the prefix must degrade to None, not raise.
    assert await resolver.resolve("base64://!!!not-valid!!!") is None


async def test_resolve_empty_and_none(resolver) -> None:
    assert await resolver.resolve("") is None
    assert await resolver.resolve("   ") is None
    assert await resolver.resolve(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# data: passthrough
# ---------------------------------------------------------------------------


async def test_resolve_data_uri_passthrough(resolver) -> None:
    uri = "data:image/png;base64,aGVsbG8="
    assert await resolver.resolve(uri) == uri


# ---------------------------------------------------------------------------
# local files
# ---------------------------------------------------------------------------


async def test_resolve_local_file(resolver, tmp_path: Path) -> None:
    img = tmp_path / "cat.png"
    img.write_bytes(PNG)

    # Plain filesystem path.
    uri = await resolver.resolve(str(img))
    assert uri is not None
    assert uri.startswith("data:image/png;base64,")
    assert _payload(uri) == PNG

    # file:// URL with percent-encoding (exercises urllib.unquote).
    spaced = tmp_path / "my cat.png"
    spaced.write_bytes(GIF)
    uri2 = await resolver.resolve(f"file://{quote(str(spaced))}")
    assert uri2 is not None
    assert uri2.startswith("data:image/gif;base64,")
    assert _payload(uri2) == GIF


async def test_resolve_missing_local_file(resolver, tmp_path: Path) -> None:
    assert await resolver.resolve(str(tmp_path / "nope.png")) is None
    assert await resolver.resolve(f"file://{tmp_path / 'nope.png'}") is None


async def test_resolve_unsupported_scheme(resolver) -> None:
    # Unknown schemes are dropped rather than guessed.
    assert await resolver.resolve("ftp://example.com/sticker.png") is None
    assert await resolver.resolve("magnet:?xt=urn:btih:abcd") is None


# ---------------------------------------------------------------------------
# HTTP downloads (real localhost server)
# ---------------------------------------------------------------------------


async def test_resolve_http_download(resolver, http_server, tmp_path: Path) -> None:
    (tmp_path / "img.png").write_bytes(PNG)
    uri = await resolver.resolve(f"{http_server.base_url}/img.png")
    assert uri is not None
    assert uri.startswith("data:image/png;base64,")
    assert _payload(uri) == PNG


async def test_resolve_http_404(resolver, http_server) -> None:
    assert await resolver.resolve(f"{http_server.base_url}/missing.png") is None


async def test_resolve_http_oversize(resolver, http_server, tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "big.png").write_bytes(PNG + b"\x00" * 4096)
    monkeypatch.setattr(resolver, "MAX_BYTES", 1024)
    assert await resolver.resolve(f"{http_server.base_url}/big.png") is None


async def test_resolve_caches(resolver, monkeypatch) -> None:
    calls: list[str] = []

    async def fake_download(url: str) -> str:
        calls.append(url)
        return "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")

    monkeypatch.setattr(resolver, "_download_and_encode", fake_download)
    url = "http://example.invalid/cat.png"

    first = await resolver.resolve(url)
    second = await resolver.resolve(url)

    assert first == second
    assert first is not None
    assert calls == [url]  # second call served from cache


# ---------------------------------------------------------------------------
# batching
# ---------------------------------------------------------------------------


async def test_resolve_batch_dedup_and_limit(resolver) -> None:
    urls = [_b64url(PNG), _b64url(GIF), _b64url(WEBP), _b64url(PNG), _b64url(GIF)]
    result = await resolver.resolve_batch(urls, limit=2)
    assert len(result) == 2
    assert result[0].startswith("data:image/png;base64,")
    assert result[1].startswith("data:image/gif;base64,")


async def test_resolve_batch_skips_failures(resolver) -> None:
    good = _b64url(PNG)
    bad = "base64://!!!not-valid!!!"
    expected = await resolver.resolve(good)
    assert expected is not None
    result = await resolver.resolve_batch([bad, good, "", "   ", bad])
    assert result == [expected]


# ---------------------------------------------------------------------------
# collect_image_refs
# ---------------------------------------------------------------------------


def test_collect_image_refs() -> None:
    segments: list[object] = [
        ImageSegment(url="http://a/1.png"),
        FaceSegment(face_id="f1", extras={"url": "http://a/2.png"}),
        FaceSegment(face_id="f2"),  # plain emoji, no url
        FaceSegment(face_id="f3", extras={"url": 123}),  # non-str url ignored
        TextSegment(text="hi"),
        ImageSegment(url="http://a/1.png"),  # duplicate
    ]
    assert collect_image_refs(segments) == ["http://a/1.png", "http://a/2.png"]


def test_collect_image_refs_empty() -> None:
    assert collect_image_refs([]) == []
    # Note: the scanner does not strip whitespace, so a non-empty-but-blank
    # url (like "  ") IS collected; only empty strings are skipped.
    assert collect_image_refs([ImageSegment(url=""), FaceSegment(face_id="x")]) == []


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


async def test_aclose_idempotent() -> None:
    resolver = ImageContentResolver()
    await resolver.aclose()
    await resolver.aclose()  # must not raise
