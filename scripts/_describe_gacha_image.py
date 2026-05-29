"""Describe a gacha image by region: header / hero / grid / footer.

Reports dominant colours per region and confirms the visual structure
matches the design (gold title, red god-ray for UR hero, etc.).
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from PIL import Image


def _bucket(rgb: tuple[int, int, int]) -> str:
    r, g, b = rgb
    if max(r, g, b) < 30:
        return "near-black"
    if r > 180 and g < 100 and b < 110:
        return "UR-red"
    if r > 200 and g > 150 and b < 120:
        return "SR-gold"
    if b > 150 and r < 150 and g < 200:
        return "R-blue"
    if abs(r - g) < 25 and abs(g - b) < 25 and r > 150:
        return "silver/white"
    if r < 100 and g < 100 and b < 100:
        return "dark"
    return f"#{r:02x}{g:02x}{b:02x}"


def _scan(img: Image.Image, box: tuple[int, int, int, int], step: int = 8) -> dict[str, int]:
    x0, y0, x1, y1 = box
    counter: Counter[str] = Counter()
    for y in range(y0, y1, step):
        for x in range(x0, x1, step):
            counter[_bucket(img.getpixel((x, y)))] += 1
    return counter


def describe(path: str) -> None:
    img = Image.open(path).convert("RGB")
    w, h = img.size
    print(f"\n{path}")
    print(f"  size: {w} × {h}")

    # Heuristic regions matching the layout: header (top 160), hero
    # (next ~420 if present), grid (rest minus footer 110), footer.
    regions = {
        "header": (0, 0, w, 160),
        "hero":   (0, 160, w, 580),
        "grid":   (0, 580, w, h - 110),
        "footer": (0, h - 110, w, h),
    }
    for name, box in regions.items():
        if box[3] <= box[1]:
            continue
        counts = _scan(img, box)
        top = counts.most_common(5)
        line = ", ".join(f"{k}({v})" for k, v in top)
        print(f"  {name:7s}: {line}")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        describe(p)
