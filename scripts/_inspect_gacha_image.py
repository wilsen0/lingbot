"""Sample pixel colours across a gacha image to verify visual content."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image


def main(path: str) -> None:
    img = Image.open(path).convert("RGB")
    w, h = img.size
    print(f"{path}: {w}x{h}")

    # 5x5 grid of samples
    rows = 5
    cols = 5
    for ry in range(rows):
        line = []
        for rx in range(cols):
            x = int((rx + 0.5) / cols * w)
            y = int((ry + 0.5) / rows * h)
            r, g, b = img.getpixel((x, y))
            line.append(f"#{r:02x}{g:02x}{b:02x}")
        print("  ", " ".join(line))


if __name__ == "__main__":
    for p in sys.argv[1:]:
        main(p)
