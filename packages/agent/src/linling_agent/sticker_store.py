"""Sticker (表情包) collection store: filesystem blobs + KV index.

The LLM collects stickers it likes (``save_sticker`` tool) and can later
recall (``list_stickers``) or re-send them (``send_sticker``). Image bytes
live on disk under ``sticker_dir``; a small KV index maps sticker id →
metadata (name / tags / source) so listing and lookup stay cheap without
scanning the filesystem.

Design notes:
- id = first 16 hex chars of md5(image bytes) → identical images dedup
  automatically (saving the same sticker twice returns the existing id).
- ``sticker_dir`` may be ``None`` (e.g. read-only test env): every method
  degrades to an error/empty result instead of raising, so a missing
  directory never breaks the chat turn.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import structlog
from linling_core.storage.kv import KVStore

logger = structlog.get_logger(__name__)

_SCOPE = "__sticker_fav__"
_INDEX_FILE = "index"
_MIME_EXT = {
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/jpeg": ".jpg",
}


class StickerStore:
    """Filesystem + KV backed sticker collection."""

    def __init__(self, kv: KVStore, sticker_dir: Path | None, *, max_stickers: int = 200) -> None:
        self._kv = kv
        self._dir = sticker_dir
        self._max = max(1, max_stickers)

    async def save(
        self,
        image_bytes: bytes,
        *,
        name: str,
        tags: str = "",
        source_qq: str = "",
        mime: str = "image/jpeg",
    ) -> str:
        """Persist a sticker, returning its id. Dedups by content md5.

        Returns the id on success. Raises ValueError when the collection
        is full or sticker_dir is unavailable (callers turn this into a
        tool error string).
        """
        if self._dir is None:
            raise ValueError("sticker storage unavailable")

        sticker_id = hashlib.md5(image_bytes).hexdigest()[:16]

        # Dedup: identical bytes hash to the same id. Refresh the friendly
        # metadata (name / tags / source) when the caller provided any.
        existing = await self._read_meta(sticker_id)
        if existing is not None:
            if name or tags or source_qq:
                if name:
                    existing["name"] = name
                if tags:
                    existing["tags"] = tags
                if source_qq:
                    existing["source_qq"] = source_qq
                await self._write_meta(sticker_id, existing)
            return sticker_id

        if await self.count() >= self._max:
            raise ValueError(f"sticker collection full (max {self._max})")

        filename = f"{sticker_id}{_MIME_EXT.get(mime, '.jpg')}"
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            (self._dir / filename).write_bytes(image_bytes)
        except OSError as exc:
            logger.warning("sticker.write_file_failed", filename=filename, exc_info=True)
            raise ValueError(f"failed to save sticker: {exc}") from exc

        meta = {
            "name": name,
            "tags": tags,
            "file": filename,
            "created_at": int(time.time()),
            "source_qq": source_qq,
            "mime": mime,
        }
        try:
            await self._write_meta(sticker_id, meta)
        except ValueError:
            # Index write failed → roll back the orphaned file so a failed
            # save leaves no litter behind.
            with contextlib.suppress(OSError):
                (self._dir / filename).unlink(missing_ok=True)
            raise
        return sticker_id

    async def list(self, query: str = "") -> list[dict[str, Any]]:
        """Return metadata dicts [{id, name, tags, created_at, source_qq}], newest first.

        ``query`` (optional) filters by case-insensitive substring match
        on name or tags.
        """
        try:
            keys = await self._kv.keys(_SCOPE, _INDEX_FILE)
        except Exception:
            logger.warning("sticker.list_keys_failed", exc_info=True)
            return []

        items: list[dict[str, Any]] = []
        for key in keys:
            meta = await self._read_meta(key)
            if meta is None:
                continue  # skip bad/unparseable rows
            meta["id"] = key
            items.append(meta)
        items.sort(key=lambda m: m.get("created_at", 0), reverse=True)

        if query:
            folded = query.casefold()
            items = [
                m
                for m in items
                if folded in m.get("name", "").casefold() or folded in m.get("tags", "").casefold()
            ]
        return items

    async def find_by_name(self, name: str) -> dict[str, Any] | None:
        """Return the metadata dict for the sticker whose name equals/matches ``name``, else None.

        Exact match wins; otherwise the first entry whose name contains
        ``name`` (case-insensitive) is returned.
        """
        if not name:
            return None
        items = await self.list()
        for item in items:
            if item.get("name") == name:
                return item
        folded = name.casefold()
        for item in items:
            if folded in item.get("name", "").casefold():
                return item
        return None

    async def load_bytes(self, sticker_id: str) -> bytes | None:
        """Read the sticker's raw bytes from disk, or None if missing/unavailable."""
        meta = await self._read_meta(sticker_id)
        if meta is None or self._dir is None:
            return None
        filename = meta.get("file")
        if not isinstance(filename, str) or not filename:
            return None
        try:
            return (self._dir / filename).read_bytes()
        except OSError:
            logger.warning("sticker.read_file_failed", sticker_id=sticker_id, exc_info=True)
            return None

    async def delete(self, sticker_id: str) -> bool:
        """Remove a sticker (index entry + file). Returns True if it existed."""
        meta = await self._read_meta(sticker_id)
        if meta is None:
            return False
        # Best-effort file removal — an orphan file is harmless, an orphan
        # index row isn't (it would resurrect the sticker on listing).
        if self._dir is not None:
            filename = meta.get("file")
            if filename:
                try:
                    (self._dir / filename).unlink(missing_ok=True)
                except OSError:
                    logger.warning(
                        "sticker.delete_file_failed", sticker_id=sticker_id, exc_info=True
                    )
        try:
            await self._kv.delete(_SCOPE, _INDEX_FILE, sticker_id)
        except Exception:
            logger.warning("sticker.index_delete_failed", sticker_id=sticker_id, exc_info=True)
            return False
        return True

    async def count(self) -> int:
        """Number of stickers in the index."""
        try:
            return len(await self._kv.keys(_SCOPE, _INDEX_FILE))
        except Exception:
            logger.warning("sticker.count_failed", exc_info=True)
            return 0

    # -----------------------------------------------------------------------
    # KV index helpers (single place where the scope/file layout is used)
    # -----------------------------------------------------------------------

    async def _read_meta(self, sticker_id: str) -> dict[str, Any] | None:
        """Load + parse one index row; None on any failure (fail-open)."""
        try:
            raw = await self._kv.read(_SCOPE, _INDEX_FILE, sticker_id, default=None)
        except Exception:
            logger.warning("sticker.index_read_failed", sticker_id=sticker_id, exc_info=True)
            return None
        if not raw:
            return None
        try:
            meta = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("sticker.index_parse_failed", sticker_id=sticker_id, exc_info=True)
            return None
        if not isinstance(meta, dict):
            logger.warning("sticker.index_bad_row", sticker_id=sticker_id)
            return None
        return meta

    async def _write_meta(self, sticker_id: str, meta: dict[str, Any]) -> None:
        """Serialize + upsert one index row; raises ValueError on KV failure."""
        try:
            await self._kv.write(
                _SCOPE,
                _INDEX_FILE,
                sticker_id,
                json.dumps(meta, ensure_ascii=False),
            )
        except Exception as exc:
            logger.warning("sticker.index_write_failed", sticker_id=sticker_id, exc_info=True)
            raise ValueError(f"failed to save sticker: {exc}") from exc
