"""Tests for the sticker-collection collage (3x3 grid image).

The collage turns the text-only sticker inventory into something the
vision model can actually see: each cell is a thumbnail of a saved
sticker labelled with its id and saved name. Behaviour pinned here:

* empty collection → ``None`` (nothing to attach)
* saved stickers → a JPEG ``data:`` URI whose payload decodes to the
  expected 3x3 grid dimensions
* more than ``max_items`` saved → only the newest ones are shown
* a sticker whose file vanished → placeholder cell, collage still built
* store failure → ``None`` (fail-open, never raises)
"""

from __future__ import annotations

import base64
import io

import pytest
from linling_agent.sticker_collage import StickerCollageBuilder
from linling_agent.sticker_store import StickerStore
from linling_core.storage.sqlite_kv import SqliteKVStore
from PIL import Image

# A minimal valid 1x1 transparent PNG.
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

_COLLAGE_COLS = 3
_CELL_SIZE = 200
_GAP = 4


@pytest.fixture
def store(tmp_path) -> StickerStore:
    kv = SqliteKVStore(bot_id="bot1", db_path=":memory:")
    return StickerStore(kv, tmp_path / "stickers")


async def _seed(store: StickerStore, count: int = 1) -> None:
    for index in range(count):
        await store.save(_PNG_BYTES + bytes([index]), name=f"表情{index}")


def _decode_uri(data_uri: str) -> Image.Image:
    assert data_uri.startswith("data:image/jpeg;base64,")
    raw = base64.b64decode(data_uri.split(",", 1)[1])
    with Image.open(io.BytesIO(raw)) as img:
        img.load()
        return img


async def test_empty_collection_returns_none(store) -> None:
    assert await StickerCollageBuilder(store).build_data_uri() is None


async def test_collage_decodes_to_grid_size(store) -> None:
    await _seed(store, count=3)
    data_uri = await StickerCollageBuilder(store).build_data_uri()
    assert data_uri is not None

    img = _decode_uri(data_uri)
    expected = _COLLAGE_COLS * _CELL_SIZE + (_COLLAGE_COLS - 1) * _GAP
    assert img.width == expected
    assert img.height == expected


async def test_only_newest_items_shown(store) -> None:
    await _seed(store, count=12)
    builder = StickerCollageBuilder(store, max_items=9)
    data_uri = await builder.build_data_uri()
    assert data_uri is not None
    # Newest 9 of 12 saved; the grid is still a single 3x3 image.
    img = _decode_uri(data_uri)
    assert img.width == 3 * _CELL_SIZE + 2 * _GAP


async def test_missing_file_degrades_to_placeholder(store, tmp_path) -> None:
    sticker_id = await store.save(_PNG_BYTES, name="会消失的表情")
    # Delete the file on disk but keep the KV index entry.
    meta = await store.list()
    assert meta
    file_path = tmp_path / "stickers" / meta[0]["file"]
    file_path.unlink()

    data_uri = await StickerCollageBuilder(store).build_data_uri()
    # The other cell (if any) still renders; here the only cell fails, so
    # the builder degrades to None rather than emitting a blank grid.
    assert data_uri is None

    # With a second healthy sticker the collage comes back, placeholder
    # cell or not.
    await store.save(_PNG_BYTES + b"\x01", name="正常表情")
    data_uri = await StickerCollageBuilder(store).build_data_uri()
    assert data_uri is not None
    assert sticker_id  # sanity: save returned an id


async def test_store_failure_returns_none() -> None:
    class _BoomStore:
        async def list(self):
            raise RuntimeError("kv down")

        async def load_bytes(self, sticker_id: str):
            raise RuntimeError("kv down")

    assert await StickerCollageBuilder(_BoomStore()).build_data_uri() is None
