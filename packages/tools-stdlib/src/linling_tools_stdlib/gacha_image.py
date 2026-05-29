"""Gacha settlement image renderer for the 扭蛋 module.

Replaces the plain-text rendering that ``$图文 %录%$`` produced for
``扭蛋十次 / 扭蛋五十次`` with a stylised result panel:

- Black starfield background with a cross-shaped golden god-ray for the
  hero card.
- Hero card (the highest-rarity drop, centred) with a glowing border
  whose thickness/colour scale with rarity.
- Card grid below the hero: every drop gets its own cell tinted by
  rarity and stamped with the item sprite. The 50-pull layout is a
  10-column grid with as many rows as needed to show *every* drop.
- Header strip with a big gold gradient title and the cost in 灵玉.
- Footer strip with rarity tallies and the rule's "扭哇扭哇～" tag.

The DSL contract is the same shape the old ``$图文$`` had — the rule
file passes the cumulative ``%录%`` string and we tokenise its lines.
Rarity is detected from the line markers QRDic emits:

- ``〖呦呦〗`` / ``〖哒咩〗`` / ``〖思思〗`` — SR (珍品)
- ``〖郫忧〗``                                   — UR (藏品)
- ``〔小白猫〕`` / ``〔大飞龙〕`` / ``〔小豆芽〕`` etc. — R (普通收藏)
- ``🍬蛋壳+1``                                    — N (基础)

Glyph fallback:
NotoSansCJK is the bundled font and it does *not* cover several
decorative chars used in earlier drafts (✦ ✨ 🥚 ✓ ❄ etc. all render
as tofu). We restrict every literal that goes through ``draw.text``
to the codepoints that NotoSansCJK SC actually has — see ``_safe``
and ``_DECO_*`` for the chosen palette. The 蛋壳 (N) tile no longer
uses 🥚; it draws an oval-shaped egg shape directly with Pillow
primitives instead.

Sprites live under the bot's asset bundle, which the bootstrap exposes
through ``ctx.extras["asset_root"]`` — same dir the OneBot adapter and
WebUI use for ``@pic:`` resolution. Missing sprites degrade to a
drawn-shape fallback rather than a tofu rectangle.
"""

from __future__ import annotations

import base64
import io
import math
import random
import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from linling_core.tools import ToolCtx, tool
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ---------------------------------------------------------------------------
# Rarity model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rarity:
    """Visual recipe for one rarity tier."""

    name: str  # "UR" / "SR" / "R" / "N"
    label: str  # "藏品" / "珍品" / "普通" / "蛋壳"
    primary: tuple[int, int, int]  # main glow / border colour
    secondary: tuple[int, int, int]  # background tint
    tile_top: tuple[int, int, int]  # gradient start
    tile_bot: tuple[int, int, int]  # gradient end
    border_w: int  # tile border thickness (px @ base scale)
    glow: int  # glow halo radius (px @ base scale)
    weight: int  # sort order; higher = more prestigious


_UR = Rarity(
    "UR", "藏品",
    primary=(255, 64, 96),  # neon red
    secondary=(255, 200, 220),
    tile_top=(120, 20, 40),
    tile_bot=(60, 0, 18),
    border_w=6,
    glow=44,
    weight=4,
)
_SR = Rarity(
    "SR", "珍品",
    primary=(255, 200, 70),  # gold
    secondary=(255, 240, 180),
    tile_top=(120, 86, 14),
    tile_bot=(60, 38, 0),
    border_w=5,
    glow=36,
    weight=3,
)
_R = Rarity(
    "R", "普通",
    primary=(120, 200, 255),  # ice blue
    secondary=(190, 220, 255),
    tile_top=(20, 50, 100),
    tile_bot=(8, 18, 50),
    border_w=3,
    glow=14,
    weight=2,
)
_N = Rarity(
    "N", "蛋壳",
    primary=(200, 200, 210),  # silver
    secondary=(220, 220, 230),
    tile_top=(60, 60, 72),
    tile_bot=(30, 30, 40),
    border_w=2,
    glow=0,
    weight=1,
)

# Item name → rarity. The QRDic rule writes a finite set, so listing
# them by hand is clearer than parsing the ornament characters.
_ITEM_RARITY: dict[str, Rarity] = {
    "郫忧": _UR,
    "呦呦": _SR,
    "哒咩": _SR,
    "思思": _SR,
    "小白猫": _R,
    "大飞龙": _R,
    "小豆芽": _R,
    "五彩棒": _R,
    "蛋壳": _N,
}

# Sprite filename for each item (lives under ``<asset_root>/picture/``).
# Falls back to ``<name>.jpg`` if not listed.
_ITEM_SPRITE: dict[str, str] = {
    "郫忧": "郫忧.jpg",
    "呦呦": "呦呦.jpg",
    "哒咩": "哒咩.jpg",
    "思思": "思思.jpg",
    "小白猫": "小白猫.jpg",
    "大飞龙": "大飞龙.jpg",
    "小豆芽": "小豆芽.jpg",
    "五彩棒": "五彩棒.jpg",
    "蛋壳": "",  # no sprite — we draw an egg shape directly
}

# Decorative glyphs that NotoSansCJK SC actually covers (verified via
# the Unicode cmap). Earlier drafts used ✦ / ✨ / ❄ / 🥚 etc. but those
# render as tofu rectangles in NotoSansCJK because the face's font
# files don't ship them — and Pillow has no automatic font fallback.
# Stick to this palette when stamping decorations onto the canvas.
_DECO_STAR = "★"  # U+2605, present in NotoSansCJK SC
_DECO_DIAMOND = "◆"  # U+25C6
_DECO_DOT = "·"  # U+00B7
_DECO_BULLET = "•"  # U+2022
_DECO_FLOWER = "❀"  # U+2740
_DECO_TRI = "▲"  # U+25B2

# Map from any decorative char a future caller might pass through to
# its CJK-safe substitute. Used by ``_safe`` to scrub strings before
# they reach Pillow's text renderer.
_GLYPH_FALLBACKS: dict[str, str] = {
    "✦": _DECO_STAR,
    "✧": _DECO_STAR,
    "✶": _DECO_STAR,
    "✨": _DECO_STAR,
    "✩": _DECO_STAR,
    "✪": _DECO_STAR,
    "✫": _DECO_STAR,
    "✬": _DECO_STAR,
    "❄": _DECO_FLOWER,
    "❉": _DECO_FLOWER,
    "❋": _DECO_FLOWER,
    "✔": "v",  # NotoSansCJK lacks heavy check
    "✕": "x",
    "✗": "x",
    "✘": "x",
    "🥚": "",  # don't render literal egg emoji — handled by _draw_egg_shape
    "🍬": "",  # same; appears in the source text only, not in the image
}


def _safe(text: str) -> str:
    """Strip / substitute glyphs that NotoSansCJK can't render.

    Keeps the function pure-Python so it stays cheap to call inline.
    """
    if not any(ch in _GLYPH_FALLBACKS for ch in text):
        return text
    out = []
    for ch in text:
        out.append(_GLYPH_FALLBACKS.get(ch, ch))
    return "".join(out)


@dataclass(frozen=True)
class Drop:
    """One row of the 扭蛋 result log."""

    name: str
    rarity: Rarity


# Detect lines like ``恭喜获得珍品〖呦呦〗！！`` / ``获得〔小白猫〕！！`` /
# ``🍬蛋壳+1`` — same shapes the rule emits in main.ling.
_SR_RE = re.compile(r"〖([^〗]+)〗")
_R_RE = re.compile(r"〔([^〕]+)〕")


def _parse_record(record: str) -> list[Drop]:
    """Tokenise the cumulative ``%录%`` log into per-pull :class:`Drop` rows.

    The QRDic rule stores the log via repeated ``$写 ... %录%\\n<line>$``
    appends, which keeps the ``\\n`` as a *literal* backslash-n in the
    KV — not a real newline. We normalise both forms here so the
    function works regardless of whether the caller passed pre-decoded
    text (the unit/E2E tests do) or the raw KV value (production).
    """
    # Real newlines first, then literal "\n" sequences.
    normalised = record.replace("\r\n", "\n").replace("\\n", "\n")
    drops: list[Drop] = []
    for raw in normalised.split("\n"):
        line = raw.strip()
        if not line:
            continue
        m = _SR_RE.search(line)
        if m:
            name = m.group(1)
            r = _ITEM_RARITY.get(name, _SR)
            drops.append(Drop(name, r))
            continue
        m = _R_RE.search(line)
        if m:
            name = m.group(1)
            r = _ITEM_RARITY.get(name, _R)
            drops.append(Drop(name, r))
            continue
        if "蛋壳" in line:
            drops.append(Drop("蛋壳", _N))
            continue
    return drops


# ---------------------------------------------------------------------------
# Font loading — prefer a CJK-capable TTF; degrade gracefully without one
# ---------------------------------------------------------------------------


_DEFAULT_FONT_CANDIDATES: tuple[str, ...] = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
)


def _resolve_font_path(ctx: ToolCtx) -> str | None:
    explicit = ctx.extras.get("image_text_font") or ctx.extras.get("gacha_image_font")
    if isinstance(explicit, str) and Path(explicit).is_file():
        return explicit
    for candidate in _DEFAULT_FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def _load_font(path: str | None, size: int) -> Any:
    if path:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def _measure(draw: ImageDraw.ImageDraw, text: str, font: Any) -> tuple[int, int]:
    if hasattr(draw, "textbbox"):
        l, t, r, b = draw.textbbox((0, 0), text, font=font)
        return r - l, b - t
    return draw.textsize(text, font=font)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Sprite cache — small, asset-root keyed
# ---------------------------------------------------------------------------


_SPRITE_CACHE: dict[tuple[str, str], Image.Image | None] = {}


def _load_sprite(asset_root: Path | None, name: str) -> Image.Image | None:
    if asset_root is None:
        return None
    fname = _ITEM_SPRITE.get(name)
    if fname is None:
        # Unknown item — try its own jpg.
        fname = f"{name}.jpg"
    if not fname:
        return None
    key = (str(asset_root), fname)
    if key in _SPRITE_CACHE:
        return _SPRITE_CACHE[key]
    path = asset_root / "picture" / fname
    img: Image.Image | None
    try:
        with Image.open(path) as raw:
            img = raw.convert("RGBA").copy()
    except (OSError, FileNotFoundError):
        img = None
    _SPRITE_CACHE[key] = img
    return img


def _resolve_asset_root(ctx: ToolCtx) -> Path | None:
    raw = ctx.extras.get("asset_root") or ctx.extras.get("gacha_asset_root")
    if isinstance(raw, Path):
        return raw if raw.is_dir() else None
    if isinstance(raw, str) and raw:
        p = Path(raw)
        return p if p.is_dir() else None
    # Last-resort guess: walk up from the cache dir to find ``assets/``.
    cache = ctx.extras.get("image_text_cache_dir")
    if cache:
        cur = Path(cache).resolve()
        for _ in range(4):
            cand = cur / "assets"
            if cand.is_dir():
                return cand
            cur = cur.parent
    return None


# ---------------------------------------------------------------------------
# Drawing primitives
# ---------------------------------------------------------------------------


def _vertical_gradient(
    size: tuple[int, int],
    top: tuple[int, int, int],
    bottom: tuple[int, int, int],
) -> Image.Image:
    w, h = size
    img = Image.new("RGB", size, top)
    px = img.load()
    if px is None:
        return img
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(top[0] * (1 - t) + bottom[0] * t)
        g = int(top[1] * (1 - t) + bottom[1] * t)
        b = int(top[2] * (1 - t) + bottom[2] * t)
        for x in range(w):
            px[x, y] = (r, g, b)
    return img


def _starfield(size: tuple[int, int], seed: int) -> Image.Image:
    w, h = size
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    rng = random.Random(seed)
    n = (w * h) // 1500
    for _ in range(n):
        x = rng.randint(0, w - 1)
        y = rng.randint(0, h - 1)
        a = rng.randint(40, 200)
        size_px = rng.choice((1, 1, 1, 2))
        draw.ellipse((x, y, x + size_px, y + size_px), fill=(255, 255, 255, a))
    return img


def _god_ray(
    size: tuple[int, int], centre: tuple[int, int], colour: tuple[int, int, int]
) -> Image.Image:
    """Cross-shaped god-ray — bright centre, fading horizontal & vertical bands."""
    w, h = size
    cx, cy = centre
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    # Horizontal band: 36 strips, alpha falling off from centre.
    band_h = max(60, h // 5)
    band_w = max(60, w // 5)
    for i in range(40):
        t = i / 40
        a = int(220 * (1 - t) ** 2)
        if a <= 0:
            continue
        dh = int(band_h * (0.2 + t))
        dw = int(band_w * (0.2 + t))
        draw.ellipse(
            (cx - w * (0.5 + t * 0.2), cy - dh, cx + w * (0.5 + t * 0.2), cy + dh),
            fill=(*colour, a // 6),
        )
        draw.ellipse(
            (cx - dw, cy - h * (0.5 + t * 0.2), cx + dw, cy + h * (0.5 + t * 0.2)),
            fill=(*colour, a // 6),
        )
    # Sharper inner cross.
    for w_px in range(8, 0, -1):
        a = 240 - w_px * 24
        draw.line([(0, cy), (w, cy)], fill=(*colour, a), width=w_px)
        draw.line([(cx, 0), (cx, h)], fill=(*colour, a), width=w_px)
    return layer.filter(ImageFilter.GaussianBlur(radius=8))


def _draw_glow_box(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    colour: tuple[int, int, int],
    radius: int,
    layers: int = 6,
) -> None:
    """Paint a soft outer glow around *box* on *canvas* (RGBA)."""
    if radius <= 0:
        return
    x0, y0, x1, y1 = box
    glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for i in range(layers, 0, -1):
        pad = int(radius * (i / layers))
        a = int(180 * (i / layers) ** 2)
        gd.rectangle(
            (x0 - pad, y0 - pad, x1 + pad, y1 + pad),
            outline=(*colour, a),
            width=max(2, radius // layers),
        )
    blurred = glow.filter(ImageFilter.GaussianBlur(radius=radius / 2))
    canvas.alpha_composite(blurred)


def _gold_text(
    base: Image.Image,
    xy: tuple[int, int],
    text: str,
    font: Any,
    *,
    fill_top: tuple[int, int, int] = (255, 240, 170),
    fill_bot: tuple[int, int, int] = (190, 130, 30),
    stroke_colour: tuple[int, int, int] = (60, 30, 0),
    stroke_w: int = 4,
) -> None:
    """Stamp *text* onto *base* with a gold gradient fill + dark outline."""
    draw = ImageDraw.Draw(base)
    w, h = _measure(draw, text, font)
    if w <= 0 or h <= 0:
        return
    # Build a vertical gradient bar then mask it with the rasterised text.
    bar = _vertical_gradient((w, h), fill_top, fill_bot).convert("RGBA")
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    md.text((0, 0), text, fill=255, font=font)
    bar.putalpha(mask)
    # Outline first (drawn directly so it blends with whatever is under).
    if stroke_w > 0:
        for dx in range(-stroke_w, stroke_w + 1):
            for dy in range(-stroke_w, stroke_w + 1):
                if dx == 0 and dy == 0:
                    continue
                draw.text(
                    (xy[0] + dx, xy[1] + dy),
                    text,
                    fill=(*stroke_colour, 255),
                    font=font,
                )
    base.alpha_composite(bar, dest=xy)


# ---------------------------------------------------------------------------
# Card drawing
# ---------------------------------------------------------------------------


def _draw_egg_shape(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
) -> None:
    """Draw an egg silhouette inside *box* — used for the 蛋壳 (N) tile.

    NotoSansCJK has no 🥚 codepoint, and Pillow won't auto-fall-back
    to a colour-emoji font, so we render the shape ourselves: a soft
    cream-coloured oval with a touch of gradient, positioned in the
    upper portion of the cell where the sprite would go.
    """
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    egg_w = int(w * 0.55)
    egg_h = int(h * 0.55)
    cx = x0 + w // 2
    cy = y0 + int(h * 0.40)

    # Soft drop shadow under the egg.
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.ellipse(
        (cx - egg_w // 2 + 4, cy - egg_h // 2 + 6,
         cx + egg_w // 2 + 4, cy + egg_h // 2 + 6),
        fill=(0, 0, 0, 120),
    )
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(radius=4)))

    # Egg body — cream-yellow ellipse, slightly taller than wide.
    body = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    bd = ImageDraw.Draw(body)
    bd.ellipse(
        (cx - egg_w // 2, cy - egg_h // 2,
         cx + egg_w // 2, cy + egg_h // 2),
        fill=(245, 230, 200, 255),
        outline=(170, 150, 110, 255),
        width=2,
    )
    # Highlight stroke (top-left) for a 3D feel.
    bd.arc(
        (cx - egg_w // 2 + 6, cy - egg_h // 2 + 6,
         cx + egg_w // 2 - 6, cy + egg_h // 2 - 6),
        start=200, end=290,
        fill=(255, 250, 230, 255),
        width=3,
    )
    canvas.alpha_composite(body)


def _draw_card(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    drop: Drop,
    sprite: Image.Image | None,
    font_label: Any,
    *,
    hero: bool = False,
) -> None:
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    rarity = drop.rarity

    # Card body — vertical gradient + rounded look (we approximate
    # rounded corners with small chamfer via a mask).
    body = _vertical_gradient((w, h), rarity.tile_top, rarity.tile_bot).convert("RGBA")
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle((0, 0, w - 1, h - 1), radius=max(8, min(w, h) // 12), fill=255)
    body.putalpha(mask)
    canvas.alpha_composite(body, dest=(x0, y0))

    # Outer glow (only SR/UR carry one; hero gets a beefed-up version).
    if rarity.glow > 0:
        radius = rarity.glow * (2 if hero else 1)
        _draw_glow_box(canvas, box, rarity.primary, radius)

    # Border.
    bd = ImageDraw.Draw(canvas)
    bd.rounded_rectangle(
        (x0, y0, x1 - 1, y1 - 1),
        radius=max(8, min(w, h) // 12),
        outline=(*rarity.primary, 255),
        width=rarity.border_w * (2 if hero else 1),
    )

    # Sprite — fits to ~70% of card width, centred, slightly above middle.
    if sprite is not None:
        target_w = int(w * (0.78 if hero else 0.72))
        scale = target_w / sprite.width
        target_h = int(sprite.height * scale)
        sized = sprite.resize((target_w, target_h), Image.Resampling.LANCZOS)
        sx = x0 + (w - target_w) // 2
        sy = y0 + int(h * 0.16)
        canvas.alpha_composite(sized, dest=(sx, sy))
    else:
        # No sprite — draw an egg shape directly (used for 蛋壳 N tier).
        # Pillow primitives sidestep the missing 🥚 emoji glyph in
        # NotoSansCJK; the result is also crisper at small sizes.
        _draw_egg_shape(canvas, box)

    # Name label at the bottom band.
    name = _safe(drop.name)
    nw, nh = _measure(bd, name, font_label)
    band_top = y1 - nh - 16
    bd.rectangle((x0, band_top - 4, x1, y1), fill=(0, 0, 0, 140))
    bd.text(
        (x0 + (w - nw) // 2, band_top),
        name,
        fill=(*rarity.primary, 255),
        font=font_label,
    )

    # Rarity badge (top-left).
    badge_pad = 4
    badge_font = font_label
    bw, bh = _measure(bd, rarity.name, badge_font)
    bd.rectangle(
        (x0 + 6, y0 + 6, x0 + 6 + bw + badge_pad * 2, y0 + 6 + bh + badge_pad * 2),
        fill=(*rarity.primary, 230),
    )
    bd.text(
        (x0 + 6 + badge_pad, y0 + 6 + badge_pad),
        rarity.name,
        fill=(0, 0, 0, 255),
        font=badge_font,
    )


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    width: int
    height: int
    header_h: int
    hero_h: int  # 0 means no hero strip
    grid_cols: int
    grid_rows: int
    cell_w: int
    cell_h: int
    cell_gap: int
    grid_top: int
    footer_h: int
    title: str


def _pick_layout(num_drops: int, has_hero: bool) -> Layout:
    """Choose canvas dims and grid layout for the given pull count.

    The grid always shows *every* drop — rows scale with the count.
    Cell size shrinks when there are more than ~12 drops so the
    canvas width stays sane on QQ's image preview, but the grid is
    never truncated.
    """
    title = "扭蛋结果"
    if num_drops <= 12:
        cols = min(5, num_drops) if num_drops > 0 else 5
        cell_w, cell_h = 180, 220
    else:
        cols = 10
        cell_w, cell_h = 110, 134

    rows = max(1, math.ceil(num_drops / cols))

    cell_gap = 12
    margin = 40

    grid_w = cols * cell_w + (cols - 1) * cell_gap
    width = grid_w + margin * 2

    header_h = 160
    hero_h = 420 if has_hero else 40
    footer_h = 110

    grid_top = header_h + hero_h + 30
    grid_h = rows * cell_h + (rows - 1) * cell_gap
    height = grid_top + grid_h + footer_h + margin

    return Layout(
        width=width,
        height=height,
        header_h=header_h,
        hero_h=hero_h,
        grid_cols=cols,
        grid_rows=rows,
        cell_w=cell_w,
        cell_h=cell_h,
        cell_gap=cell_gap,
        grid_top=grid_top,
        footer_h=footer_h,
        title=title,
    )


def _hero_drop(drops: list[Drop]) -> Drop | None:
    """Pick the highest-rarity drop, ties broken by first occurrence."""
    if not drops:
        return None
    best_idx = 0
    best = drops[0]
    for i, d in enumerate(drops[1:], 1):
        if d.rarity.weight > best.rarity.weight:
            best = d
            best_idx = i
    return best if best.rarity.weight >= _SR.weight else None


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------


def _render_panel(
    drops: list[Drop],
    *,
    cost: str,
    asset_root: Path | None,
    font_path: str | None,
) -> Image.Image:
    hero = _hero_drop(drops)
    layout = _pick_layout(len(drops), has_hero=hero is not None)

    # 1. Background — black + starfield.
    canvas = Image.new("RGBA", (layout.width, layout.height), (8, 8, 18, 255))
    canvas.alpha_composite(_starfield((layout.width, layout.height), seed=len(drops)))

    # 2. God-ray (only when there's a hero card to brag about).
    if hero is not None:
        ray_centre = (
            layout.width // 2,
            layout.header_h + layout.hero_h // 2,
        )
        canvas.alpha_composite(
            _god_ray((layout.width, layout.height), ray_centre, hero.rarity.primary)
        )

    # 3. Header — gold title + cost band.
    f_title = _load_font(font_path, 64)
    f_sub = _load_font(font_path, 28)
    f_card = _load_font(font_path, 22)
    f_card_sm = _load_font(font_path, 16)
    f_footer = _load_font(font_path, 30)

    draw = ImageDraw.Draw(canvas)
    title_text = _safe(layout.title)
    title_w, title_h = _measure(draw, title_text, f_title)
    _gold_text(
        canvas,
        ((layout.width - title_w) // 2, 30),
        title_text,
        f_title,
    )
    cost_text = _safe(f"消耗  灵玉 ×{cost}")
    cw, _ch = _measure(draw, cost_text, f_sub)
    draw.text(
        ((layout.width - cw) // 2, 30 + title_h + 12),
        cost_text,
        fill=(220, 220, 240, 255),
        font=f_sub,
    )

    # 4. Hero card.
    if hero is not None:
        hero_w = 280
        hero_h = layout.hero_h - 40
        hx = (layout.width - hero_w) // 2
        hy = layout.header_h + 20
        sprite = _load_sprite(asset_root, hero.name)
        _draw_card(
            canvas,
            (hx, hy, hx + hero_w, hy + hero_h),
            hero,
            sprite,
            f_card,
            hero=True,
        )
        # Tagline over hero. Use only NotoSansCJK-covered glyphs;
        # earlier drafts had ✦ and a "传奇" label, both removed:
        # ✦ renders as tofu, and "传奇" is the wrong vibe for a
        # benign collection bot.
        tag_map = {
            "UR": f"{_DECO_STAR} {_DECO_STAR} {_DECO_STAR}  藏  品  {_DECO_STAR} {_DECO_STAR} {_DECO_STAR}",
            "SR": f"{_DECO_STAR} {_DECO_STAR}  珍  品  {_DECO_STAR} {_DECO_STAR}",
        }
        tag = _safe(tag_map.get(hero.rarity.name, f"{_DECO_STAR}  收  藏  {_DECO_STAR}"))
        tw, th = _measure(draw, tag, f_sub)
        _gold_text(
            canvas,
            ((layout.width - tw) // 2, hy + hero_h + 4),
            tag,
            f_sub,
            fill_top=hero.rarity.primary,
            fill_bot=tuple(max(c // 2, 0) for c in hero.rarity.primary),  # type: ignore[arg-type]
            stroke_w=3,
        )

    # 5. Card grid.
    margin = (layout.width - (layout.grid_cols * layout.cell_w + (layout.grid_cols - 1) * layout.cell_gap)) // 2
    for i, drop in enumerate(drops):
        col = i % layout.grid_cols
        row = i // layout.grid_cols
        if row >= layout.grid_rows:
            break  # paranoid bound
        x0 = margin + col * (layout.cell_w + layout.cell_gap)
        y0 = layout.grid_top + row * (layout.cell_h + layout.cell_gap)
        sprite = _load_sprite(asset_root, drop.name)
        font = f_card if layout.cell_w >= 160 else f_card_sm
        _draw_card(canvas, (x0, y0, x0 + layout.cell_w, y0 + layout.cell_h), drop, sprite, font)

    # 6. Footer — rarity tallies.
    counts = Counter(d.rarity.name for d in drops)
    pieces = []
    for r in (_UR, _SR, _R, _N):
        n = counts.get(r.name, 0)
        if n == 0 and r.name in ("UR", "SR"):
            continue
        pieces.append((f"{r.name} × {n}", r.primary))
    if not pieces:
        pieces = [("扭哇扭哇～", (220, 220, 240))]
    footer_y = layout.height - layout.footer_h + 20

    # Layout footer pieces as a centred row with " · " separators.
    sep = f"  {_DECO_DOT}  "
    sep_w, _ = _measure(draw, sep, f_footer)
    total_w = 0
    measured: list[tuple[str, tuple[int, int, int], int]] = []
    for txt, col in pieces:
        safe_txt = _safe(txt)
        tw, _ = _measure(draw, safe_txt, f_footer)
        measured.append((safe_txt, col, tw))
        total_w += tw
    total_w += sep_w * (len(measured) - 1)
    cursor_x = (layout.width - total_w) // 2
    for i, (txt, col, tw) in enumerate(measured):
        if i > 0:
            draw.text((cursor_x, footer_y), sep, fill=(120, 120, 140, 255), font=f_footer)
            cursor_x += sep_w
        draw.text((cursor_x, footer_y), txt, fill=(*col, 255), font=f_footer)
        cursor_x += tw

    return canvas.convert("RGB")


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


@tool(
    name="gacha_image",
    dsl_name="扭蛋图",
    description="Render a 'legendary out-gold' style 扭蛋 settlement panel from %录% text",
    schema={
        "record": "string",
        "kind": "string?",  # "10" / "50" — determines layout if record is empty
        "cost": "string?",  # cost in 灵玉 to print in the header
    },
    safe=False,
)
async def gacha_image(
    ctx: ToolCtx,
    record: str = "",
    kind: str = "",
    cost: str = "",
) -> str:
    """Render the gacha settlement image and return its file path.

    DSL usage::

        ±img=$扭蛋图 %录% 十连 488$±
        ±img=$扭蛋图 %录% 五十连 2388$±

    *record* is the cumulative ``%录%`` log (lines like
    ``恭喜获得珍品〖思思〗！！`` and ``🍬蛋壳+1``). *kind* is informational
    — the actual layout switches on the parsed drop count. *cost*
    appears in the header strip.
    """
    drops = _parse_record(record or "")
    if not drops:
        # Defensive — caller passed an empty %录% (first-of-day reset).
        # Render at least a 10-cell N-rarity placeholder so the bot
        # never sends a 1×1 broken image.
        n_cells = 50 if "五" in kind else 10
        drops = [Drop("蛋壳", _N) for _ in range(n_cells)]

    asset_root = _resolve_asset_root(ctx)
    font_path = _resolve_font_path(ctx)
    cost_text = cost.strip() or ("2388" if len(drops) > 12 else "488")

    img = _render_panel(drops, cost=cost_text, asset_root=asset_root, font_path=font_path)

    # Encode the image to PNG bytes once. We return a ``base64://``
    # data URL rather than a filesystem path so the OneBot adapter
    # can ship it to NapCat regardless of host vs container split:
    # the ``$扭蛋图$`` tool runs in our process, but NapCat (which
    # actually does the QQ-side ``send_msg``) typically lives in a
    # Docker container that doesn't see ``/home/.../data/cache/``.
    # The returned URL is consumed verbatim by ``to_onebot_msg`` —
    # see :mod:`linling_core.onebot_codec`.
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    png_bytes = buf.getvalue()

    # Also persist a copy to disk for ops/debug. Best-effort — a
    # write failure (e.g. read-only fs in some test envs) must not
    # block the actual send.
    cache_dir = Path(ctx.extras.get("image_text_cache_dir") or "./data/cache")
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 — fs setup
        unique = f"gacha_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        (cache_dir / f"{unique}.png").write_bytes(png_bytes)
    except OSError:
        pass

    return "base64://" + base64.b64encode(png_bytes).decode("ascii")
