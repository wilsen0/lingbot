"""Fishing image renderers — Pillow panels for the 鱼塘 (fishing) module.

Three DSL-facing tools, all returning a ``base64://`` PNG data URL so
the OneBot adapter can ship them to LLBot without a shared filesystem
(same contract as ``$扭蛋图$`` — see :mod:`linling_tools_stdlib.gacha_image`):

* ``$钓鱼结算图 fish kind value buff$`` — single-catch settlement card.
  Shows the caught fish (large emoji), its name, rarity tier, value in
  灵玉, and the active rod-enchant buff. Legendary catches get a
  cross-shaped god-ray + glow, mirroring the gacha hero card.

* ``$鱼篓图 bucket title$`` — render the player's current bucket
  (``{fish: count}`` JSON) as a stylised grid: one tile per species
  with the emoji, count badge, unit price, and per-species subtotal,
  plus a footer with the grand total value.

* ``$鱼图鉴图 dex title$`` — render the collection dex: every known
  species in catalogue order, captured ones lit and stamped with the
  cumulative catch count, uncaptured ones shown as a dimmed "？" silhouette.

Fish are rendered as colour emoji (the bundled ``NotoColorEmoji``
covers them). NotoSansCJK provides the Chinese labels. Both fonts are
resolved defensively so the tools degrade to the bitmap default font
rather than crashing when a host lacks them.

The species catalogue (:data:`FISH_CATALOGUE`) is the single source of
truth shared with the DSL rules in ``bot/rules/main.ling``: rarity,
emoji, display name and base value all live here so the settlement
maths in the rule and the rendering here can never drift apart.
"""

from __future__ import annotations

import base64
import io
import json
import math
import time
import uuid
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
    """Visual + gameplay recipe for one fish rarity tier."""

    name: str  # "传说" / "稀有" / "普通" / "杂物"
    code: str  # "L" / "R" / "N" / "J" — short badge text
    primary: tuple[int, int, int]  # glow / border / accent colour
    tile_top: tuple[int, int, int]  # card gradient start
    tile_bot: tuple[int, int, int]  # card gradient end
    border_w: int
    glow: int
    weight: int  # higher = rarer; used to pick the "hero" of a bucket


_LEGEND = Rarity(
    "传说", "L",
    primary=(255, 80, 90),
    tile_top=(120, 20, 40), tile_bot=(50, 0, 16),
    border_w=6, glow=46, weight=4,
)
_RARE = Rarity(
    "稀有", "R",
    primary=(255, 200, 70),
    tile_top=(120, 86, 14), tile_bot=(54, 34, 0),
    border_w=5, glow=34, weight=3,
)
_COMMON = Rarity(
    "普通", "N",
    primary=(120, 200, 255),
    tile_top=(20, 50, 100), tile_bot=(8, 18, 50),
    border_w=3, glow=12, weight=2,
)
_JUNK = Rarity(
    "杂物", "J",
    primary=(150, 150, 160),
    tile_top=(58, 58, 66), tile_bot=(26, 26, 32),
    border_w=2, glow=0, weight=1,
)

_RARITY_BY_NAME: dict[str, Rarity] = {
    r.name: r for r in (_LEGEND, _RARE, _COMMON, _JUNK)
}


# ---------------------------------------------------------------------------
# Species catalogue — single source of truth, shared with the DSL rules.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Species:
    """One catchable thing in the pond."""

    emoji: str
    name: str
    rarity: Rarity
    value: int  # sell price in 灵玉 (0 for junk)


# Ordered catalogue. The dex renders in this order; the DSL 抽奖 weights
# live in the rule file (main.ling) but the names/emoji/values must match
# the keys here exactly. Every species uses a *distinct* emoji so bucket
# / dex tiles never collide.
FISH_CATALOGUE: tuple[Species, ...] = (
    # 传说 (legendary)
    Species("🐉", "锦鲤龙", _LEGEND, 1888),
    Species("🦈", "巨齿鲨", _LEGEND, 1288),
    Species("🐋", "蓝鲸", _LEGEND, 988),
    # 稀有 (rare)
    Species("🐬", "海豚", _RARE, 328),
    Species("🐠", "小丑鱼", _RARE, 288),
    Species("🐡", "河豚", _RARE, 198),
    Species("🐙", "章鱼", _RARE, 178),
    Species("🦑", "鱿鱼", _RARE, 168),
    # 普通 (common)
    Species("🦞", "龙虾", _COMMON, 78),
    Species("🦐", "对虾", _COMMON, 68),
    Species("🐟", "草鱼", _COMMON, 59),
    Species("🐚", "扇贝", _COMMON, 32),
    Species("🦀", "螃蟹", _COMMON, 18),
    # 杂物 (junk)
    Species("🥾", "破靴子", _JUNK, 0),
    Species("🧦", "湿袜子", _JUNK, 0),
    Species("🌿", "水草", _JUNK, 0),
    Species("🥫", "空罐头", _JUNK, 0),
    Species("🪣", "空水桶", _JUNK, 0),
)

# Build lookup by name. All emoji are distinct, so the render catalogue
# is simply the full ordered list.
_SPECIES_BY_NAME: dict[str, Species] = {s.name: s for s in FISH_CATALOGUE}
_RENDER_CATALOGUE: list[Species] = list(FISH_CATALOGUE)

# Reverse map emoji → species name, for parsing the *legacy* bucket
# format (``水桶里有`` stored a concatenated emoji run like ``🦀🐟🐠``).
# Distinct emoji make this unambiguous; the historical pond used a
# subset (🦀🦞🐟🦑🐙🐡🐠) that all live in the catalogue.
_EMOJI_TO_NAME: dict[str, str] = {s.emoji: s.name for s in FISH_CATALOGUE}


def species_by_name(name: str) -> Species | None:
    """Look up a species by its display name."""
    return _SPECIES_BY_NAME.get(name)


# ---------------------------------------------------------------------------
# Font loading
# ---------------------------------------------------------------------------


_CJK_FONT_CANDIDATES: tuple[str, ...] = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
)

_EMOJI_FONT_CANDIDATES: tuple[str, ...] = (
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/System/Library/Fonts/Apple Color Emoji.ttc",
    "C:/Windows/Fonts/seguiemj.ttf",
)

# NotoColorEmoji ships a single bitmap strike at 109px. Pillow requires
# you to request that exact size, then we scale the rendered glyph.
_EMOJI_NATIVE_PX = 109


def _resolve_cjk_font(ctx: ToolCtx) -> str | None:
    explicit = ctx.extras.get("image_text_font") or ctx.extras.get("fishing_image_font")
    if isinstance(explicit, str) and Path(explicit).is_file():
        return explicit
    for cand in _CJK_FONT_CANDIDATES:
        if Path(cand).is_file():
            return cand
    return None


def _resolve_emoji_font() -> str | None:
    for cand in _EMOJI_FONT_CANDIDATES:
        if Path(cand).is_file():
            return cand
    return None


def _load_font(path: str | None, size: int) -> Any:
    if path:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def _load_emoji_font(path: str | None) -> Any | None:
    if not path:
        return None
    try:
        return ImageFont.truetype(path, size=_EMOJI_NATIVE_PX)
    except OSError:
        return None


def _measure(draw: ImageDraw.ImageDraw, text: str, font: Any) -> tuple[int, int]:
    if hasattr(draw, "textbbox"):
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        return int(right - left), int(bottom - top)
    w, h = draw.textsize(text, font=font)  # type: ignore[attr-defined]
    return int(w), int(h)


# ---------------------------------------------------------------------------
# Emoji rendering — render at the native 109px strike, then scale.
# ---------------------------------------------------------------------------


_EMOJI_CACHE: dict[tuple[str, int], Image.Image | None] = {}


def _render_emoji(emoji_font: Any | None, emoji: str, target_px: int) -> Image.Image | None:
    """Render *emoji* to an RGBA image of roughly ``target_px`` height.

    NotoColorEmoji only has a 109px bitmap strike, so we draw at native
    size onto a transparent canvas, crop to the glyph, then resize to
    the requested target. Returns ``None`` when no emoji font is
    available (callers fall back to a drawn placeholder).
    """
    if emoji_font is None:
        return None
    key = (emoji, target_px)
    cached = _EMOJI_CACHE.get(key)
    if cached is not None or key in _EMOJI_CACHE:
        return cached
    canvas = Image.new("RGBA", (_EMOJI_NATIVE_PX * 2, _EMOJI_NATIVE_PX * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    try:
        draw.text((0, 0), emoji, font=emoji_font, embedded_color=True)
    except (ValueError, OSError):
        _EMOJI_CACHE[key] = None
        return None
    bbox = canvas.getbbox()
    if bbox is None:
        _EMOJI_CACHE[key] = None
        return None
    glyph = canvas.crop(bbox)
    scale = target_px / max(glyph.width, glyph.height)
    new_size = (max(1, int(glyph.width * scale)), max(1, int(glyph.height * scale)))
    sized = glyph.resize(new_size, Image.Resampling.LANCZOS)
    _EMOJI_CACHE[key] = sized
    return sized


def _paste_emoji_centred(
    canvas: Image.Image,
    emoji_font: Any | None,
    emoji: str,
    box: tuple[int, int, int, int],
    target_px: int,
    primary: tuple[int, int, int],
) -> None:
    """Render *emoji* centred inside *box*; fall back to a coloured disc."""
    x0, y0, x1, y1 = box
    cx = (x0 + x1) // 2
    cy = (y0 + y1) // 2
    sprite = _render_emoji(emoji_font, emoji, target_px)
    if sprite is not None:
        canvas.alpha_composite(sprite, dest=(cx - sprite.width // 2, cy - sprite.height // 2))
        return
    # Fallback: a coloured disc with a "?" — keeps layout intact when
    # the emoji font is missing (e.g. a minimal CI image).
    r = target_px // 2
    disc = ImageDraw.Draw(canvas)
    disc.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*primary, 200))


# ---------------------------------------------------------------------------
# Drawing primitives (shared shapes; kept independent of gacha_image so
# the two renderers can evolve separately).
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


def _water_background(size: tuple[int, int]) -> Image.Image:
    """A calm teal→deep-blue vertical gradient — the pond water look."""
    return _vertical_gradient(size, (38, 96, 120), (8, 24, 56)).convert("RGBA")


def _god_ray(
    size: tuple[int, int], centre: tuple[int, int], colour: tuple[int, int, int]
) -> Image.Image:
    """Cross-shaped god-ray for legendary catches (ported from gacha_image)."""
    w, h = size
    cx, cy = centre
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
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
    canvas.alpha_composite(glow.filter(ImageFilter.GaussianBlur(radius=radius / 2)))


def _rounded_card(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    rarity: Rarity,
    *,
    hero: bool = False,
) -> None:
    """Paint a rounded gradient card with rarity border + optional glow."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    body = _vertical_gradient((w, h), rarity.tile_top, rarity.tile_bot).convert("RGBA")
    radius = max(8, min(w, h) // 12)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    body.putalpha(mask)
    canvas.alpha_composite(body, dest=(x0, y0))
    if rarity.glow > 0:
        _draw_glow_box(canvas, box, rarity.primary, rarity.glow * (2 if hero else 1))
    ImageDraw.Draw(canvas).rounded_rectangle(
        (x0, y0, x1 - 1, y1 - 1),
        radius=radius,
        outline=(*rarity.primary, 255),
        width=rarity.border_w * (2 if hero else 1),
    )


def _gold_text(
    base: Image.Image,
    xy: tuple[int, int],
    text: str,
    font: Any,
    *,
    fill_top: tuple[int, int, int] = (255, 240, 170),
    fill_bot: tuple[int, int, int] = (190, 130, 30),
    stroke_colour: tuple[int, int, int] = (40, 24, 0),
    stroke_w: int = 4,
) -> None:
    """Stamp *text* with a vertical gradient fill + dark outline."""
    draw = ImageDraw.Draw(base)
    w, h = _measure(draw, text, font)
    if w <= 0 or h <= 0:
        return
    bar = _vertical_gradient((w, h), fill_top, fill_bot).convert("RGBA")
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).text((0, 0), text, fill=255, font=font)
    bar.putalpha(mask)
    if stroke_w > 0:
        for dx in range(-stroke_w, stroke_w + 1):
            for dy in range(-stroke_w, stroke_w + 1):
                if dx == 0 and dy == 0:
                    continue
                draw.text((xy[0] + dx, xy[1] + dy), text, fill=(*stroke_colour, 255), font=font)
    base.alpha_composite(bar, dest=xy)


def _centre_text(
    canvas: Image.Image,
    text: str,
    font: Any,
    cx: int,
    y: int,
    fill: tuple[int, int, int],
) -> int:
    """Draw *text* horizontally centred at *cx*; return its height."""
    draw = ImageDraw.Draw(canvas)
    w, h = _measure(draw, text, font)
    draw.text((cx - w // 2, y), text, fill=(*fill, 255), font=font)
    return h


# ---------------------------------------------------------------------------
# 1. Single-catch settlement card — $钓鱼结算图$
# ---------------------------------------------------------------------------


def _render_settlement(
    species: Species | None,
    *,
    raw_name: str,
    raw_emoji: str,
    value: str,
    buff: str,
    cjk_font: str | None,
    emoji_font_path: str | None,
) -> Image.Image:
    """Render a single-catch card.

    Falls back to ``raw_name`` / ``raw_emoji`` / ``_COMMON`` when the
    species isn't in the catalogue (so a future rule can pass a one-off
    catch without us crashing).
    """
    name = species.name if species else (raw_name or "神秘收获")
    emoji = species.emoji if species else (raw_emoji or "🎣")
    rarity = species.rarity if species else _COMMON

    width, height = 720, 900
    emoji_loaded = _load_emoji_font(emoji_font_path)
    canvas = _water_background((width, height))

    hero = rarity.weight >= _RARE.weight
    if hero:
        canvas.alpha_composite(_god_ray((width, height), (width // 2, 430), rarity.primary))

    f_title = _load_font(cjk_font, 56)
    f_name = _load_font(cjk_font, 64)
    f_sub = _load_font(cjk_font, 34)
    f_badge = _load_font(cjk_font, 30)

    # Header title.
    _gold_text(canvas, (width // 2 - _measure(ImageDraw.Draw(canvas), "起杆收获", f_title)[0] // 2, 48),
               "起杆收获", f_title)

    # Centre card holding the fish emoji.
    card_w, card_h = 380, 380
    cx0 = (width - card_w) // 2
    cy0 = 170
    _rounded_card(canvas, (cx0, cy0, cx0 + card_w, cy0 + card_h), rarity, hero=hero)
    _paste_emoji_centred(
        canvas, emoji_loaded, emoji,
        (cx0, cy0, cx0 + card_w, cy0 + card_h),
        target_px=240, primary=rarity.primary,
    )

    # Rarity badge on the card.
    bd = ImageDraw.Draw(canvas)
    badge = f"{rarity.name}"
    bw, bh = _measure(bd, badge, f_badge)
    bd.rectangle((cx0 + 14, cy0 + 14, cx0 + 14 + bw + 20, cy0 + 14 + bh + 12),
                 fill=(*rarity.primary, 235))
    bd.text((cx0 + 24, cy0 + 20), badge, fill=(20, 20, 24, 255), font=f_badge)

    # Name (gold for rare+, plain white for common/junk).
    y = cy0 + card_h + 34
    nw, nh = _measure(bd, name, f_name)
    if hero:
        _gold_text(canvas, (width // 2 - nw // 2, y), name, f_name,
                   fill_top=tuple(min(255, c + 40) for c in rarity.primary),  # type: ignore[arg-type]
                   fill_bot=rarity.primary)
    else:
        bd.text((width // 2 - nw // 2, y), name, fill=(240, 246, 255, 255), font=f_name)
    y += nh + 30

    # Value line.
    if rarity is _JUNK:
        value_line = "卖不出钱，但也算个念想"
    else:
        value_line = f"估价  灵玉 ×{value or species.value if species else value}"
    y += _centre_text(canvas, value_line, f_sub, width // 2, y, (235, 235, 245)) + 16

    # Buff line (only when a buff is active).
    if buff and buff not in ("0", "无", ""):
        _centre_text(canvas, f"附魔加成 · {buff}", f_sub, width // 2, y, rarity.primary)

    return canvas.convert("RGB")


# ---------------------------------------------------------------------------
# 2. Bucket grid — $鱼篓图$
# ---------------------------------------------------------------------------


def _parse_counts(blob: str) -> dict[str, int]:
    """Parse a ``{name: count}`` JSON object into an int-valued dict.

    Tolerates malformed input (returns empty) and coerces stringy
    counts. As a safety net it also accepts the *legacy* bucket form —
    a bare concatenated emoji run like ``🦀🐟🐠`` — by tallying known
    emoji into species counts. Unknown keys survive; the renderer skips
    ones it can't map to a species.
    """
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        data = None
    if isinstance(data, dict):
        out: dict[str, int] = {}
        for k, v in data.items():
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            if n > 0:
                out[str(k)] = n
        return out
    # Legacy emoji-run fallback.
    return _counts_from_legacy_emoji(blob or "")


def _counts_from_legacy_emoji(text: str) -> dict[str, int]:
    """Tally a concatenated emoji string into ``{species_name: count}``.

    Iterates code points, matching each against the catalogue's emoji
    set. Non-fish characters are ignored. Returns an empty dict for an
    empty / fish-free string.
    """
    counts: dict[str, int] = {}
    for ch in text:
        name = _EMOJI_TO_NAME.get(ch)
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


def _grid_layout(n: int) -> tuple[int, int, int, int]:
    """Return (cols, cell_w, cell_h, gap) for *n* tiles."""
    if n <= 4:
        return max(1, n), 200, 240, 18
    if n <= 9:
        return 3, 200, 240, 18
    return 4, 168, 206, 14


def _render_bucket(
    counts: dict[str, int],
    *,
    title: str,
    cjk_font: str | None,
    emoji_font_path: str | None,
    dex_mode: bool,
) -> Image.Image:
    """Render the bucket grid (or, when *dex_mode*, the full dex)."""
    emoji_loaded = _load_emoji_font(emoji_font_path)

    if dex_mode:
        tiles = list(_RENDER_CATALOGUE)
    else:
        tiles = [s for s in _RENDER_CATALOGUE if counts.get(s.name, 0) > 0]

    f_title = _load_font(cjk_font, 52)
    f_name = _load_font(cjk_font, 26)
    f_sub = _load_font(cjk_font, 22)
    f_badge = _load_font(cjk_font, 22)
    f_footer = _load_font(cjk_font, 32)

    n = max(1, len(tiles))
    cols, cell_w, cell_h, gap = _grid_layout(n)
    rows = math.ceil(n / cols)
    margin = 40
    header_h = 130
    footer_h = 96

    grid_w = cols * cell_w + (cols - 1) * gap
    width = grid_w + margin * 2
    grid_top = header_h
    height = grid_top + rows * cell_h + (rows - 1) * gap + footer_h + margin

    canvas = _water_background((width, height))
    bd = ImageDraw.Draw(canvas)

    # Title.
    title_text = title or ("钓鱼图鉴" if dex_mode else "我的鱼篓")
    tw, _th = _measure(bd, title_text, f_title)
    _gold_text(canvas, ((width - tw) // 2, 36), title_text, f_title)

    total_value = 0
    grand_count = 0
    grid_left = (width - grid_w) // 2

    for i, sp in enumerate(tiles):
        col = i % cols
        row = i // cols
        x0 = grid_left + col * (cell_w + gap)
        y0 = grid_top + row * (cell_h + gap)
        box = (x0, y0, x0 + cell_w, y0 + cell_h)
        have = counts.get(sp.name, 0)
        captured = have > 0 or (not dex_mode)

        if dex_mode and not captured:
            # Dimmed silhouette card for an undiscovered species.
            _rounded_card(canvas, box, _JUNK)
            _centre = ((x0 + x0 + cell_w) // 2, (y0 + y0 + cell_h) // 2)
            qw, qh = _measure(bd, "？", f_title)
            bd.text((_centre[0] - qw // 2, _centre[1] - qh // 2), "？",
                    fill=(120, 130, 150, 255), font=f_title)
            nw, _nh = _measure(bd, "未发现", f_sub)
            bd.text((x0 + (cell_w - nw) // 2, y0 + cell_h - 40), "未发现",
                    fill=(120, 130, 150, 255), font=f_sub)
            continue

        _rounded_card(canvas, box, sp.rarity)
        _paste_emoji_centred(
            canvas, emoji_loaded, sp.emoji,
            (x0, y0 + 8, x0 + cell_w, y0 + cell_h - 70),
            target_px=int(cell_w * 0.5), primary=sp.rarity.primary,
        )
        # Count badge (top-right).
        badge = f"×{have}" if have > 0 else ""
        if badge:
            bw, bh = _measure(bd, badge, f_badge)
            bx1 = x0 + cell_w - 12
            bd.rectangle((bx1 - bw - 16, y0 + 12, bx1, y0 + 12 + bh + 10),
                         fill=(*sp.rarity.primary, 235))
            bd.text((bx1 - bw - 8, y0 + 16), badge, fill=(20, 20, 24, 255), font=f_badge)

        # Name + per-tile value band.
        name_w, name_h = _measure(bd, sp.name, f_name)
        band_top = y0 + cell_h - name_h - 40
        bd.rectangle((x0, band_top - 4, x0 + cell_w, y0 + cell_h), fill=(0, 0, 0, 150))
        bd.text((x0 + (cell_w - name_w) // 2, band_top), sp.name,
                fill=(*sp.rarity.primary, 255), font=f_name)
        if dex_mode:
            sub = f"累计 {have}"
        else:
            sub = f"{sp.value}/个" if sp.value > 0 else "无价值"
        sub_w, _sub_h = _measure(bd, sub, f_sub)
        bd.text((x0 + (cell_w - sub_w) // 2, y0 + cell_h - 30), sub,
                fill=(220, 224, 235, 255), font=f_sub)

        total_value += sp.value * have
        grand_count += have

    # Footer.
    footer_y = height - footer_h + 18
    if dex_mode:
        captured_species = sum(1 for s in _RENDER_CATALOGUE if counts.get(s.name, 0) > 0)
        footer = f"已收集  {captured_species} / {len(_RENDER_CATALOGUE)}  种"
    elif grand_count == 0:
        footer = "鱼篓空空，快去甩杆吧"
    else:
        footer = f"共 {grand_count} 只 · 总价值  灵玉 ×{total_value}"
    fw, _fh = _measure(bd, footer, f_footer)
    _gold_text(canvas, ((width - fw) // 2, footer_y), footer, f_footer,
               fill_top=(255, 240, 180), fill_bot=(200, 150, 50), stroke_w=3)

    return canvas.convert("RGB")


# ---------------------------------------------------------------------------
# Shared output encoder
# ---------------------------------------------------------------------------


def _encode(ctx: ToolCtx, img: Image.Image, tag: str) -> str:
    """PNG-encode *img*, persist a debug copy, return a ``base64://`` URL."""
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    png_bytes = buf.getvalue()
    cache_dir = Path(ctx.extras.get("image_text_cache_dir") or "./data/cache")
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        unique = f"{tag}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        (cache_dir / f"{unique}.png").write_bytes(png_bytes)
    except OSError:
        pass
    return "base64://" + base64.b64encode(png_bytes).decode("ascii")


# ---------------------------------------------------------------------------
# Tool registrations
# ---------------------------------------------------------------------------


@tool(
    name="fishing_settlement_image",
    dsl_name="钓鱼结算图",
    description="Render a single-catch settlement card from (name, value, buff)",
    schema={
        "name": "string",
        "value": "string?",
        "buff": "string?",
        "emoji": "string?",
    },
    safe=False,
)
async def fishing_settlement_image(
    ctx: ToolCtx,
    name: str = "",
    value: str = "",
    buff: str = "",
    emoji: str = "",
) -> str:
    """``$钓鱼结算图 鱼名 价值 附魔$`` → settlement card PNG (base64 URL).

    *name* is the species display name (e.g. ``草鱼``); we look it up in
    :data:`FISH_CATALOGUE` for rarity/emoji/value. *value* overrides the
    printed price (the rule already knows the final number); *buff* is
    the active rod-enchant name (blank/``无`` hides the line).
    """
    species = species_by_name(name.strip())
    img = _render_settlement(
        species,
        raw_name=name.strip(),
        raw_emoji=emoji.strip(),
        value=value.strip(),
        buff=buff.strip(),
        cjk_font=_resolve_cjk_font(ctx),
        emoji_font_path=_resolve_emoji_font(),
    )
    return _encode(ctx, img, "fish_catch")


@tool(
    name="fishing_bucket_image",
    dsl_name="鱼篓图",
    description="Render the player's bucket ({name:count} JSON) as a value grid",
    schema={
        "bucket": "string",
        "title": "string?",
    },
    safe=False,
)
async def fishing_bucket_image(
    ctx: ToolCtx,
    bucket: str = "",
    title: str = "",
) -> str:
    """``$鱼篓图 %水桶% 标题$`` → bucket grid PNG (base64 URL).

    *bucket* is a JSON object mapping species name → count. Empty or
    malformed input still renders (an "empty bucket" footer).
    """
    counts = _parse_counts(bucket or "")
    img = _render_bucket(
        counts,
        title=title.strip(),
        cjk_font=_resolve_cjk_font(ctx),
        emoji_font_path=_resolve_emoji_font(),
        dex_mode=False,
    )
    return _encode(ctx, img, "fish_bucket")


@tool(
    name="fishing_dex_image",
    dsl_name="鱼图鉴图",
    description="Render the fish collection dex (captured lit, others dimmed)",
    schema={
        "dex": "string",
        "title": "string?",
    },
    safe=False,
)
async def fishing_dex_image(
    ctx: ToolCtx,
    dex: str = "",
    title: str = "",
) -> str:
    """``$鱼图鉴图 %图鉴% 标题$`` → dex PNG (base64 URL).

    *dex* is a JSON object mapping species name → cumulative catch
    count. Species absent (or count 0) render as a dimmed silhouette.
    """
    counts = _parse_counts(dex or "")
    img = _render_bucket(
        counts,
        title=title.strip() or "钓鱼图鉴",
        cjk_font=_resolve_cjk_font(ctx),
        emoji_font_path=_resolve_emoji_font(),
        dex_mode=True,
    )
    return _encode(ctx, img, "fish_dex")
