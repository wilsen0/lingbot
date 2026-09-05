"""Sticker collection collage — a 3x3 grid image of the saved stickers.

``list_stickers`` gives the model a text-only inventory, which does not
say what each sticker *looks* like, so the model never knows which of
its saved stickers is the right one to re-send. This module builds a
single 3x3 collage of the newest saved stickers — each cell labelled
with the sticker id and its saved name — that the vision paths attach
as one more multimodal content part on every request, so the model can
see its collection and pick a sticker by name.

Every failure mode degrades to ``None`` ("no collage"), never raising:
an empty collection, unreadable sticker files, or missing imaging
support simply mean the request goes out without the collage.
"""

from __future__ import annotations

import asyncio
import base64
import io
from pathlib import Path
from typing import Any

import structlog
from PIL import Image, ImageDraw, ImageFont, ImageOps

from linling_agent.sticker_store import StickerStore

logger = structlog.get_logger(__name__)

_COLLAGE_COLS = 3
_COLLAGE_ROWS = 3
_CELL_SIZE = 200  # 每格画布边长 (px)
_THUMB_SIZE = 160  # 缩略图 contain 边长 (px)
_LABEL_HEIGHT = 36  # 底部名字条高度 (px)
_GAP = 4  # 格间距 (px)
_MAX_NAME_CHARS = 8
_JPEG_QUALITY = 80

# CJK-capable system fonts, probed once at first use. Mirrors the
# candidate table in tools-stdlib's gacha_image (which agent cannot
# import without a circular dependency).
_FONT_CANDIDATES: tuple[str, ...] = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
)

_font_cache: dict[int, Any] = {}


def _load_font(size: int) -> Any:
    """Cached CJK font at ``size``, degrading to PIL's default font."""
    cached = _font_cache.get(size)
    if cached is not None:
        return cached
    font: Any = None
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).is_file():
            try:
                font = ImageFont.truetype(candidate, size=size)
                break
            except OSError:
                continue
    if font is None:
        font = ImageFont.load_default()
    _font_cache[size] = font
    return font


def _clip_name(name: str, max_chars: int = _MAX_NAME_CHARS) -> str:
    if len(name) <= max_chars:
        return name
    return name[:max_chars] + "…"


def _load_thumbnail(raw: bytes) -> Image.Image | None:
    """Decode ``raw`` into a square white-backed thumbnail, or None.

    Corrupt/unsupported payloads (or any Pillow failure) degrade to
    ``None`` so the caller can draw a placeholder cell instead.
    """
    try:
        with Image.open(io.BytesIO(raw)) as img:
            img.load()
            thumb = ImageOps.contain(img.convert("RGB"), (_THUMB_SIZE, _THUMB_SIZE))
            canvas = Image.new("RGB", (_THUMB_SIZE, _THUMB_SIZE), "white")
            canvas.paste(
                thumb,
                ((_THUMB_SIZE - thumb.width) // 2, (_THUMB_SIZE - thumb.height) // 2),
            )
            return canvas
    except Exception:
        logger.warning("sticker_collage.thumbnail_failed", exc_info=True)
        return None


def _draw_cell(
    canvas: Image.Image,
    col: int,
    row: int,
    thumb: Image.Image | None,
    sticker_id: str,
    name: str,
) -> None:
    """Paint one 200x200 cell: thumbnail, id badge, and name label."""
    x = col * (_CELL_SIZE + _GAP)
    y = row * (_CELL_SIZE + _GAP)
    img_area = _CELL_SIZE - _LABEL_HEIGHT
    draw = ImageDraw.Draw(canvas)

    if thumb is not None:
        canvas.paste(
            thumb,
            (x + (img_area - _THUMB_SIZE) // 2, y + (img_area - _THUMB_SIZE) // 2),
        )
    else:
        draw.rectangle((x, y, x + img_area, y + img_area), fill=(230, 230, 230))
        draw.text(
            (x + 8, y + img_area // 2 - 10),
            "[图片缺失]",
            fill=(120, 120, 120),
            font=_load_font(16),
        )

    # id badge: top-left, full hex id so it cross-references list_stickers.
    draw.text((x + 6, y + 6), sticker_id, fill=(90, 90, 90), font=_load_font(11))

    # name label strip along the bottom.
    label_y = y + img_area
    draw.rectangle(
        (x, label_y, x + _CELL_SIZE, y + _CELL_SIZE),
        fill=(245, 245, 245),
        outline=(200, 200, 200),
    )
    draw.text((x + 6, label_y + 8), _clip_name(name), fill=(40, 40, 40), font=_load_font(16))


class StickerCollageBuilder:
    """Build a 3x3 collage image of the newest saved stickers."""

    def __init__(
        self,
        store: StickerStore,
        *,
        max_items: int = _COLLAGE_ROWS * _COLLAGE_COLS,
    ) -> None:
        self._store = store
        self._max_items = max(1, int(max_items))

    async def build_data_uri(self) -> str | None:
        """Return a ``data:image/jpeg;base64,...`` collage URI, or None.

        ``None`` when the collection is empty, every cell failed to
        load, or imaging is unavailable — callers treat that as "no
        collage", never as an error.
        """
        try:
            items = await self._store.list()
        except Exception:
            logger.warning("sticker_collage.list_failed", exc_info=True)
            return None
        items = items[: self._max_items]
        if not items:
            return None

        cells: list[tuple[Image.Image | None, str, str]] = []
        for meta in items:
            sticker_id = str(meta.get("id", ""))
            raw = await self._store.load_bytes(sticker_id)
            thumb = _load_thumbnail(raw) if raw else None
            cells.append((thumb, sticker_id, str(meta.get("name", ""))))
        if not any(cell[0] is not None for cell in cells):
            return None

        try:
            jpeg = await asyncio.to_thread(self._compose_jpeg, cells)
        except Exception:
            logger.warning("sticker_collage.compose_failed", exc_info=True)
            return None
        if jpeg is None:
            return None
        return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")

    @staticmethod
    def _compose_jpeg(
        cells: list[tuple[Image.Image | None, str, str]],
    ) -> bytes | None:
        """Render the grid to JPEG bytes (runs off the event loop)."""
        width = _COLLAGE_COLS * _CELL_SIZE + (_COLLAGE_COLS - 1) * _GAP
        height = _COLLAGE_ROWS * _CELL_SIZE + (_COLLAGE_ROWS - 1) * _GAP
        canvas = Image.new("RGB", (width, height), "white")
        for index, (thumb, sticker_id, name) in enumerate(cells):
            row, col = divmod(index, _COLLAGE_COLS)
            _draw_cell(canvas, col, row, thumb, sticker_id, name)
        buf = io.BytesIO()
        canvas.save(buf, format="JPEG", quality=_JPEG_QUALITY)
        return buf.getvalue()
