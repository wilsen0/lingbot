"""Shrink large in-chat stickers in ``bot/assets/picture`` to 40 % size.

QQ renders the original ~360 px PNGs uncomfortably large in chat. The
40 % scale matches what other rasters in this set already use and
keeps them legible at sticker dimensions.

In-place rewrite. Run with ``uv run python scripts/_resize_susu_stickers.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ASSET_DIR = Path(__file__).resolve().parents[1] / "bot" / "assets" / "picture"
SCALE = 0.40
PATTERNS = ("苏苏*.png", "我的灵玉*.png")


def main() -> int:
    files = sorted({p for pat in PATTERNS for p in ASSET_DIR.glob(pat)})
    if not files:
        print(f"no matching PNG files under {ASSET_DIR}", file=sys.stderr)
        return 1

    for path in files:
        with Image.open(path) as im:
            im.load()
            old = im.size
            new = (max(1, round(old[0] * SCALE)), max(1, round(old[1] * SCALE)))
            resized = im.resize(new, Image.LANCZOS)
            resized.save(path, format="PNG", optimize=True)
        print(f"{path.name}: {old} -> {new}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
