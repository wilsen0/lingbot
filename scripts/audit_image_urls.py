"""Audit every ``±img=...±`` URL referenced by dicpro.txt.

Reports which sources are still reachable and which ones rotted, so an
operator deciding what to migrate / re-host knows what's worth saving.

For each unique URL we record:

* ``http_status`` — HTTP status the upstream returns (or ``000`` when
  the connection itself failed; e.g. DNS / TLS).
* ``content_type`` — first response token (``image/png``, ``text/html``,
  empty when the connection failed).
* ``size`` — bytes downloaded (HEAD-emulated by GET-first-N for hosts
  that reject HEAD).

Local references — ``/storage/.../picture/X.jpg`` and ``@pic:X`` —
get a side-table noting whether the file actually exists on disk
under ``bot/assets/picture/`` (the canonical post-2026-05 location).
The side-table tolerates ``.jpg`` ↔ ``.svg`` promotion so the
audit doesn't false-positive on legacy refs whose replacement art
ships as SVG.

Run:

    uv run python scripts/audit_image_urls.py

Exits 0 unconditionally; this is informational, not a CI gate.
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections import Counter
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
DICPRO = REPO / "QRDic" / "dicpro.txt"
# Bundled bot assets — the canonical (post-2026-05) location. The
# old QRDic/data/picture path is no longer checked: assets live in
# the bot workspace now, full stop.
PICTURE_DIR = REPO / "bot" / "assets" / "picture"

_IMG_RE = re.compile(r"±img=([^±]+)±")
_QRDIC_LEGACY_PREFIX = "/storage/emulated/0/QR/QRDic/data/"

_TIMEOUT = httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)


def _classify(raw: str) -> tuple[str, str]:
    """Return (kind, normalised_url_or_path) for an ``±img=...±`` source."""
    raw = raw.strip()
    if not raw:
        return "empty", ""
    if raw.startswith(("http://", "https://", "//")):
        return "remote", raw if not raw.startswith("//") else "https:" + raw
    if raw.startswith(_QRDIC_LEGACY_PREFIX):
        return "local-legacy", raw[len(_QRDIC_LEGACY_PREFIX) :]
    if raw.startswith("@pic:"):
        return "local-pic", raw[len("@pic:") :]
    if "%" in raw or "$" in raw:
        return "templated", raw
    return "other", raw


async def _check_remote(client: httpx.AsyncClient, url: str) -> tuple[str, str, int]:
    try:
        resp = await client.get(url)
    except httpx.HTTPError as exc:  # pragma: no cover — runtime audit
        return "000", f"err:{type(exc).__name__}", 0
    ct = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    return str(resp.status_code), ct or "?", len(resp.content)


def _check_local(rel: str) -> str:
    """For ``picture/X.jpg`` style relpaths, check whether the file exists.

    The migrator's ``@pic:NAME`` shorthand can omit the extension; we
    try ``.jpg`` (the QRDic default) and ``.svg`` (the in-tree pixel-
    art replacements) as fallbacks before declaring it missing. The
    DSL's WebUI rewriter applies the same default-to-``.jpg`` fallback
    at runtime, so as long as one extension hits the disk the bot
    renders fine.
    """
    name = rel[len("picture/") :] if rel.startswith("picture/") else rel
    base = PICTURE_DIR / name
    if base.is_file():
        return "ok"
    # Try alternate extensions when the shorthand omitted one, or the
    # caller used the legacy .jpg suffix but we ship a .svg replacement.
    stem = base.stem if "." in base.name else base.name
    parent = base.parent
    for ext in (".svg", ".jpg", ".png", ".gif", ".webp", ".jpeg"):
        if (parent / f"{stem}{ext}").is_file():
            return "ok"
    return "MISSING"


async def main() -> int:
    if not DICPRO.exists():
        print(f"can't find {DICPRO}", file=sys.stderr)
        return 2
    src = DICPRO.read_text(encoding="utf-8")

    by_kind: dict[str, list[str]] = {}
    for match in _IMG_RE.finditer(src):
        kind, url = _classify(match.group(1))
        by_kind.setdefault(kind, []).append(url)

    print("=== usage by kind ===")
    for kind, urls in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        print(f"  {kind:14s}: {len(urls):4d} occurrences, {len(set(urls)):3d} unique")
    print()

    # Local references: cross-check disk availability.
    local_legacy = sorted(set(by_kind.get("local-legacy", [])))
    local_pic = sorted(set(by_kind.get("local-pic", [])))
    if local_legacy or local_pic:
        print("=== local references ===")
        for rel in local_legacy:
            status = _check_local(rel)
            print(f"  legacy   {status:7s}  {rel}")
        for name in local_pic:
            status = _check_local(name)
            print(f"  @pic:    {status:7s}  {name}")
        print()

    # Remote references: live HTTP probe (concurrency 8 to be polite).
    remote = sorted(set(by_kind.get("remote", [])))
    if not remote:
        return 0
    print(f"=== remote URL probe ({len(remote)} unique) ===")
    sem = asyncio.Semaphore(8)

    async def _probe(url: str) -> tuple[str, str, str, int]:
        async with (
            sem,
            httpx.AsyncClient(
                timeout=_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": "linling-image-audit/1.0"},
            ) as client,
        ):
            status, ct, size = await _check_remote(client, url)
            return url, status, ct, size

    results = await asyncio.gather(*(_probe(u) for u in remote))

    summary: Counter[str] = Counter()
    for _url, status, _ct, _size in results:
        bucket = (
            "ok"
            if status == "200"
            else "rotted"
            if status in ("404", "410")
            else "blocked"
            if status == "000"
            else "auth"
            if status in ("401", "403")
            else "other"
        )
        summary[bucket] += 1

    for status_bucket, count in summary.most_common():
        print(f"  {status_bucket:10s}: {count}")
    print()

    print("=== detail (sorted by status) ===")
    for url, status, ct, size in sorted(results, key=lambda r: (r[1], r[0])):
        print(f"  [{status}] {ct:18s} {size:>8d}  {url}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
