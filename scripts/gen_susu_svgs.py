"""
Generate the bundled Sūsū / linling pixel-art SVGs into
``bot/assets/picture/``.

Each emitted SVG replaces a (now-dead) ``s1.ax1x.com`` /
``wkphoto.cdn.bcebos.com`` reference that the previous QRSpeed-era
ruleset used. The generator keeps character anatomy consistent across
the set by sourcing primitives from ``_susu_lib``.

Run from the repo root:

    uv run python scripts/gen_susu_svgs.py

Re-run is idempotent: it overwrites every output file. If you want to
hand-tune one, copy it elsewhere first or delete it from the OUTPUTS
list below.

Most assets are static (pixel art doesn't need motion to read well);
a few — the drift-bottle scene, the sleeping/yawning sprites, and
banner sparkles — opt into SMIL animation where the animation
genuinely carries the meaning.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "bot" / "assets" / "picture"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _susu_lib import (  # noqa: E402
    PINK,
    PINK_DARK,
    PINK_DEEP,
    PINK_LIGHT,
    PIXEL_DEFS,
    WHITE,
    fox_tail_back,
    hanfu_top,
    head,
    lingyu_gem,
)

# ---------------------------------------------------------------------------
# SVG wrapper helpers
# ---------------------------------------------------------------------------


def svg_doc(viewbox: str, body: str, *, defs: str = "", label: str = "") -> str:
    """Wrap ``body`` in a self-contained SVG document with our standard
    pixel-art rendering hints and the shared ``cheek`` gradient.
    """
    aria = f'role="img" aria-label="{label}"' if label else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="{viewbox}"
     shape-rendering="crispEdges" image-rendering="pixelated"
     {aria}>
  <defs>{PIXEL_DEFS}
  {defs}
  </defs>
{body}
</svg>
"""


# ---------------------------------------------------------------------------
# 灵玉宝石 — used as both a stand-alone icon (Ozgi2d.png replacement) and
# embedded into排行榜 banners. The fluttering sparkle is a single
# subtle pulse.
# ---------------------------------------------------------------------------


def gen_lingyu_icon() -> str:
    """The 灵玉 gem stand-alone, ~80×80 with sparkle."""
    body = f"""
  <!-- soft halo -->
  <ellipse cx="40" cy="40" rx="34" ry="34" fill="#ffd0f0" opacity=".35"/>
  <g transform="translate(28 26)">
    {lingyu_gem(0, 0, scale=1)}
  </g>
  <!-- twinkle -->
  <g fill="#fff">
    <rect x="20" y="22" width="2" height="2">
      <animate attributeName="opacity" values="0;1;0" dur="2.4s" repeatCount="indefinite"/>
    </rect>
    <rect x="58" y="46" width="2" height="2">
      <animate attributeName="opacity" values="1;0;1" dur="2.4s" repeatCount="indefinite"/>
    </rect>
  </g>
"""
    return svg_doc("0 0 80 80", body, label="灵玉宝石")


# ---------------------------------------------------------------------------
# 御妖符库存徽章 — 7 variants (0/1/2/3/4/5/6+ talismans). The original
# QRDic art was a yellow rune card; we render that with a stack of
# golden cards and a pixel digit.
# ---------------------------------------------------------------------------


_DIGIT_STROKES = {
    "0": [(0, 0, 4, 1), (0, 0, 1, 6), (3, 0, 1, 6), (0, 5, 4, 1)],
    "1": [(2, 0, 1, 6), (1, 1, 1, 1), (1, 5, 3, 1)],
    "2": [(0, 0, 4, 1), (3, 1, 1, 2), (0, 2, 4, 1), (0, 3, 1, 2), (0, 5, 4, 1)],
    "3": [(0, 0, 4, 1), (3, 1, 1, 4), (1, 2, 2, 1), (0, 5, 4, 1)],
    "4": [(0, 0, 1, 3), (3, 0, 1, 6), (0, 2, 4, 1)],
    "5": [(0, 0, 4, 1), (0, 1, 1, 2), (0, 2, 4, 1), (3, 3, 1, 2), (0, 5, 4, 1)],
    "6": [(0, 0, 4, 1), (0, 1, 1, 4), (0, 2, 4, 1), (3, 3, 1, 2), (0, 5, 4, 1)],
    "+": [(1, 2, 3, 1), (2, 1, 1, 3)],
}


def pixel_digit(x: int, y: int, ch: str, color: str) -> str:
    """Render ch at (x,y) using a 4×6 pixel font (single digit)."""
    parts = []
    for dx, dy, dw, dh in _DIGIT_STROKES.get(ch, []):
        parts.append(f'<rect x="{x+dx}" y="{y+dy}" width="{dw}" height="{dh}" fill="{color}"/>')
    return "\n  ".join(parts)


def gen_yufu_badge(n: int) -> str:
    """Render a stack of 御妖符 talisman cards labelled with `n` (0–6+).

    `n` is the inventory count. We draw min(n, 3) overlapping golden
    cards plus a number badge in the corner. n=0 fades the cards into
    grey to imply emptiness.
    """
    label = f"御妖符×{n}" if n < 6 else "御妖符×6+"
    is_zero = n == 0
    is_overflow = n >= 6

    base = "#fcd34d" if not is_zero else "#d8d8d8"
    base_dark = "#e0a92c" if not is_zero else "#a8a8a8"
    rune = "#a02030" if not is_zero else "#888"
    bg_grad_id = "yfbg"
    defs = f"""
    <linearGradient id="{bg_grad_id}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%"  stop-color="#fff5d8"/>
      <stop offset="100%" stop-color="#ffd6a8"/>
    </linearGradient>
"""

    body = [
        f'<rect width="120" height="120" fill="url(#{bg_grad_id})"/>',
    ]

    # Stack of cards (1 to 3 cards visible)
    visible = min(max(n, 1), 3)
    if is_zero:
        visible = 1
    for i in range(visible):
        ox = 24 + i * 6
        oy = 28 + (visible - 1 - i) * 6
        body.append(f"""
  <g>
    <rect x="{ox}" y="{oy}" width="48" height="64" fill="{base}"/>
    <rect x="{ox}" y="{oy}" width="48" height="3" fill="{base_dark}"/>
    <rect x="{ox}" y="{oy+61}" width="48" height="3" fill="{base_dark}"/>
    <rect x="{ox}" y="{oy}" width="3" height="64" fill="{base_dark}"/>
    <rect x="{ox+45}" y="{oy}" width="3" height="64" fill="{base_dark}"/>
    <!-- inner border -->
    <rect x="{ox+5}" y="{oy+5}" width="38" height="54" fill="none" stroke="{base_dark}" stroke-width="1"/>
    <!-- 妖 rune (center) -->
    <rect x="{ox+18}" y="{oy+14}" width="2" height="20" fill="{rune}"/>
    <rect x="{ox+24}" y="{oy+12}" width="2" height="22" fill="{rune}"/>
    <rect x="{ox+30}" y="{oy+14}" width="2" height="20" fill="{rune}"/>
    <rect x="{ox+18}" y="{oy+18}" width="14" height="2" fill="{rune}"/>
    <rect x="{ox+18}" y="{oy+24}" width="14" height="2" fill="{rune}"/>
    <rect x="{ox+18}" y="{oy+30}" width="14" height="2" fill="{rune}"/>
    <!-- bottom decoration line -->
    <rect x="{ox+12}" y="{oy+44}" width="24" height="1" fill="{rune}" opacity=".6"/>
    <rect x="{ox+14}" y="{oy+50}" width="20" height="1" fill="{rune}" opacity=".6"/>
  </g>
""")

    # Count badge (top-right corner of the canvas)
    if not is_zero:
        body.append(f"""
  <!-- count badge -->
  <circle cx="92" cy="28" r="14" fill="{PINK_LIGHT}"/>
  <circle cx="92" cy="28" r="14" fill="none" stroke="{PINK_DEEP}" stroke-width="2"/>
""")
        if is_overflow:
            # 6+
            body.append(pixel_digit(85, 25, "6", "#fff"))
            body.append(pixel_digit(91, 25, "+", "#fff"))
        else:
            body.append(pixel_digit(89, 25, str(n), "#fff"))
    else:
        # an X mark
        body.append("""
  <circle cx="92" cy="28" r="14" fill="#888"/>
  <circle cx="92" cy="28" r="14" fill="none" stroke="#555" stroke-width="2"/>
  <rect x="86" y="22" width="12" height="2" fill="#fff" transform="rotate(45 92 28)"/>
  <rect x="86" y="22" width="12" height="2" fill="#fff" transform="rotate(-45 92 28)"/>
""")

    return svg_doc("0 0 120 120", "\n".join(body), defs=defs, label=label)


# ---------------------------------------------------------------------------
# 排行榜 banner family — 财富/法力/妖力. Each is a horizontal scroll
# title plate with a themed icon (gem / talisman / aura) on the left.
# ---------------------------------------------------------------------------


def gen_rank_banner(kind: str) -> str:
    """`kind` ∈ 财富 (wealth) / 法力 (mana) / 妖力 (yokai)."""
    if kind == "财富":
        title = "财富排行榜"
        # gem icon left
        icon_block = f"""
  <g transform="translate(20 18)">
    {lingyu_gem(0, 0, scale=2)}
  </g>
"""
        accent = "#ff8aa6"
    elif kind == "法力":
        title = "法力排行榜"
        # talisman icon
        icon_block = """
  <g transform="translate(24 16)">
    <rect x="0" y="0" width="32" height="44" fill="#fcd34d"/>
    <rect x="0" y="0" width="32" height="2" fill="#e0a92c"/>
    <rect x="0" y="42" width="32" height="2" fill="#e0a92c"/>
    <rect x="0" y="0" width="2" height="44" fill="#e0a92c"/>
    <rect x="30" y="0" width="2" height="44" fill="#e0a92c"/>
    <rect x="14" y="6" width="2" height="14" fill="#a02030"/>
    <rect x="10" y="10" width="10" height="2" fill="#a02030"/>
    <rect x="10" y="16" width="10" height="2" fill="#a02030"/>
    <rect x="10" y="26" width="14" height="2" fill="#a02030" opacity=".7"/>
    <rect x="10" y="32" width="14" height="2" fill="#a02030" opacity=".7"/>
  </g>
"""
        accent = "#ffe28a"
    else:  # 妖力
        title = "妖力排行榜"
        icon_block = """
  <g transform="translate(20 14)">
    <!-- 妖力 aura swirl: orange with yellow center -->
    <circle cx="20" cy="24" r="18" fill="#ff8a3a" opacity=".5"/>
    <circle cx="20" cy="24" r="14" fill="#ffb84a" opacity=".7"/>
    <circle cx="20" cy="24" r="9"  fill="#ffd84a"/>
    <rect x="18" y="14" width="4" height="2" fill="#fff7c4"/>
    <rect x="14" y="20" width="2" height="4" fill="#fff7c4"/>
    <rect x="26" y="20" width="2" height="4" fill="#fff7c4"/>
    <rect x="18" y="30" width="4" height="2" fill="#fff7c4"/>
  </g>
"""
        accent = "#ffb84a"

    # Stylised title rendered as pixel glyphs would be heavy — use a
    # simple text element with a CJK-friendly fallback. Browsers render
    # this alongside the vector graphics fine.
    body = f"""
  <!-- backdrop -->
  <rect width="320" height="80" fill="#fff7e8"/>
  <rect width="320" height="80" fill="none" stroke="#a05a32" stroke-width="2"/>
  <!-- decorative side bars -->
  <rect x="0" y="0" width="6" height="80" fill="#a05a32"/>
  <rect x="314" y="0" width="6" height="80" fill="#a05a32"/>
  <rect x="6" y="0" width="2" height="80" fill="#7a4a25"/>
  <rect x="312" y="0" width="2" height="80" fill="#7a4a25"/>
  <!-- decorative top/bottom rules with dots -->
  <rect x="12" y="6" width="296" height="1" fill="{accent}"/>
  <rect x="12" y="73" width="296" height="1" fill="{accent}"/>
  {icon_block}
  <!-- title text -->
  <text x="170" y="50" font-family="serif" font-size="34"
        font-weight="bold" text-anchor="middle"
        fill="{PINK_DEEP}" stroke="{accent}" stroke-width="0.5">{title}</text>
  <!-- corner ornaments -->
  <g fill="{PINK_LIGHT}">
    <rect x="12" y="12" width="4" height="2"/>
    <rect x="14" y="10" width="2" height="2"/>
    <rect x="14" y="14" width="2" height="2"/>
    <rect x="304" y="12" width="4" height="2"/>
    <rect x="304" y="10" width="2" height="2"/>
    <rect x="304" y="14" width="2" height="2"/>
    <rect x="12" y="66" width="4" height="2"/>
    <rect x="14" y="64" width="2" height="2"/>
    <rect x="14" y="68" width="2" height="2"/>
    <rect x="304" y="66" width="4" height="2"/>
    <rect x="304" y="64" width="2" height="2"/>
    <rect x="304" y="68" width="2" height="2"/>
  </g>
"""
    return svg_doc("0 0 320 80", body, label=title)


# ---------------------------------------------------------------------------
# 御妖符 trigger banner — header art for the 御妖符 command listing.
# ---------------------------------------------------------------------------


def gen_yufu_banner() -> str:
    body = f"""
  <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%"  stop-color="#fff8d8"/>
    <stop offset="100%" stop-color="#ffd6a8"/>
  </linearGradient>
  <rect width="320" height="80" fill="url(#bg)"/>
  <rect width="320" height="80" fill="none" stroke="#a05a32" stroke-width="2"/>
  <!-- talisman -->
  <g transform="translate(28 14)">
    <rect x="0" y="0" width="36" height="52" fill="#fcd34d"/>
    <rect x="0" y="0" width="36" height="3" fill="#e0a92c"/>
    <rect x="0" y="49" width="36" height="3" fill="#e0a92c"/>
    <rect x="0" y="0" width="3" height="52" fill="#e0a92c"/>
    <rect x="33" y="0" width="3" height="52" fill="#e0a92c"/>
    <rect x="6" y="6" width="24" height="40" fill="none" stroke="#e0a92c"/>
    <rect x="16" y="10" width="2" height="22" fill="#a02030"/>
    <rect x="22" y="8"  width="2" height="24" fill="#a02030"/>
    <rect x="28" y="10" width="2" height="22" fill="#a02030"/>
    <rect x="16" y="16" width="14" height="2" fill="#a02030"/>
    <rect x="16" y="24" width="14" height="2" fill="#a02030"/>
  </g>
  <text x="200" y="50" font-family="serif" font-size="32"
        font-weight="bold" text-anchor="middle"
        fill="{PINK_DEEP}">御妖符指南</text>
"""
    return svg_doc("0 0 320 80", body, label="御妖符指南")


# ---------------------------------------------------------------------------
# 我的灵玉系列 — wallet view header. Three variants (1-balance, in-debt,
# special amounts 520/1314).
# ---------------------------------------------------------------------------


def gen_wallet_card(variant: str) -> str:
    """variant ∈ 普通 / 富 / 浪漫 (520/1314)"""
    if variant == "富":
        accent = "#fcd34d"
        title = "我的灵玉"
        sub = "富甲天下"
    elif variant == "浪漫":
        accent = "#ff8aa6"
        title = "我的灵玉"
        sub = "❤"
    else:
        accent = "#88c0e0"
        title = "我的灵玉"
        sub = ""
    body = f"""
  <linearGradient id="wbg" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%"  stop-color="#fff5fa"/>
    <stop offset="100%" stop-color="#ffe0ed"/>
  </linearGradient>
  <rect width="220" height="80" fill="url(#wbg)"/>
  <rect width="220" height="80" fill="none" stroke="{accent}" stroke-width="3"/>
  <!-- corner gem -->
  <g transform="translate(14 26)">
    {lingyu_gem(0, 0, scale=1)}
  </g>
  <text x="138" y="38" font-family="serif" font-size="20" font-weight="bold"
        text-anchor="middle" fill="{PINK_DEEP}">{title}</text>
  <text x="138" y="60" font-family="serif" font-size="16"
        text-anchor="middle" fill="{accent}">{sub}</text>
"""
    return svg_doc("0 0 220 80", body, label=title)


# ---------------------------------------------------------------------------
# 半身像 — body-and-head sprite used as the base for many emotion variants.
# Returns a fragment that draws Sūsū's torso + head at (x,y) in canvas
# coords. The caller decides eye/mouth via head() params and adds
# scenery around it.
# ---------------------------------------------------------------------------


def torso_with_head(x: int, y: int, *, eye: str, mouth: str, blush: bool = True) -> str:
    """Half-body Sūsū at (x,y). Bounding box ≈ 80×96.

    Layout: tail behind body → torso → head on top.
    """
    out = []
    # Big white tail behind body, root toward right
    out.append(f'<g transform="translate({x-12} {y+30})">{fox_tail_back(0, 0)}</g>')
    # Torso (hanfu top)
    out.append(f'<g transform="translate({x+12} {y+44})">{hanfu_top(0, 0, w=60, h=32)}</g>')
    # Sleeves (just darker pink rectangles each side)
    out.append(f"""
  <rect x="{x+0}" y="{y+50}" width="14" height="22" fill="{PINK}"/>
  <rect x="{x+0}" y="{y+50}" width="14" height="2" fill="{PINK_DARK}"/>
  <rect x="{x+0}" y="{y+70}" width="14" height="2" fill="{PINK_DARK}"/>
  <rect x="{x+72}" y="{y+50}" width="14" height="22" fill="{PINK}"/>
  <rect x="{x+72}" y="{y+50}" width="14" height="2" fill="{PINK_DARK}"/>
  <rect x="{x+72}" y="{y+70}" width="14" height="2" fill="{PINK_DARK}"/>
  <!-- white sleeve cuffs -->
  <rect x="{x+0}" y="{y+72}" width="14" height="6" fill="{WHITE}"/>
  <rect x="{x+72}" y="{y+72}" width="14" height="6" fill="{WHITE}"/>
""")
    # Head — width 36, place centered above torso
    head_x = x + 18
    head_y = y + 4
    out.append(head(head_x, head_y, eye=eye, mouth=mouth, blush=blush))
    return "\n  ".join(out)


# ---------------------------------------------------------------------------
# Emotion sprites
# ---------------------------------------------------------------------------


def gen_emotion(
    name: str,
    *,
    eye: str,
    mouth: str,
    accent: str = "#fff1e0",
    accent_dark: str = "#ffd0d6",
    ornaments: str = "",
) -> str:
    """A 200×220 emotion sprite of half-body Sūsū against a soft
    gradient circle. Optional `ornaments` SVG string is overlayed."""
    body = f"""
  <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%"  stop-color="{accent}"/>
    <stop offset="100%" stop-color="{accent_dark}"/>
  </linearGradient>
  <rect width="200" height="220" fill="url(#bg)"/>
  <ellipse cx="100" cy="180" rx="80" ry="10" fill="#000" opacity=".08"/>
  <g>
    {torso_with_head(60, 60, eye=eye, mouth=mouth)}
  </g>
  {ornaments}
"""
    return svg_doc("0 0 200 220", body, label=name)


# ---------------------------------------------------------------------------
# Scene sprites: 钓鱼 banner / 卧底游戏 / 捡瓶子 / 道具宝箱 / 唉你是 /
# 灵玉不足100 / 御妖符成功 / 禁言成功 / 运势.
# ---------------------------------------------------------------------------


def gen_diaoyu_banner() -> str:
    """Fishing rod against pond water."""
    body = """
  <linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%"  stop-color="#bce0ff"/>
    <stop offset="100%" stop-color="#7fc8ff"/>
  </linearGradient>
  <linearGradient id="water" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%"  stop-color="#5fa8c4"/>
    <stop offset="100%" stop-color="#2d6a8c"/>
  </linearGradient>
  <rect width="320" height="100" fill="url(#sky)"/>
  <rect y="100" width="320" height="60" fill="url(#water)"/>
  <!-- ripples -->
  <rect x="40" y="120" width="40" height="2" fill="#a8d8ee"/>
  <rect x="180" y="130" width="60" height="2" fill="#a8d8ee"/>
  <rect x="260" y="118" width="40" height="2" fill="#a8d8ee"/>
  <!-- fishing rod -->
  <rect x="40" y="40" width="180" height="2" fill="#7a4a25" transform="rotate(-12 40 40)"/>
  <rect x="40" y="38" width="180" height="2" fill="#a05a32" transform="rotate(-12 40 40)"/>
  <!-- line -->
  <rect x="200" y="20" width="1" height="100" fill="#888"/>
  <!-- float -->
  <rect x="196" y="118" width="10" height="6" fill="#ff5577"/>
  <rect x="196" y="124" width="10" height="3" fill="#fff"/>
  <rect x="196" y="118" width="10" height="2" fill="#a02030"/>
  <!-- fish silhouette under water -->
  <g fill="#ffd84a" opacity=".7">
    <rect x="240" y="138" width="14" height="6"/>
    <rect x="244" y="136" width="8" height="2"/>
    <rect x="244" y="144" width="8" height="2"/>
    <rect x="252" y="138" width="2" height="2"/>
    <rect x="232" y="138" width="4" height="6"/>
  </g>
  <text x="100" y="80" font-family="serif" font-size="32"
        font-weight="bold" text-anchor="middle" fill="#fff"
        stroke="#2d6a8c" stroke-width="2">🎣 鱼塘</text>
"""
    return svg_doc("0 0 320 160", body, label="钓鱼鱼塘")


def gen_woudi_banner() -> str:
    """Undercover game — masked silhouette + question mark."""
    body = """
  <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%"  stop-color="#3a3060"/>
    <stop offset="100%" stop-color="#5d4a8a"/>
  </linearGradient>
  <rect width="320" height="160" fill="url(#bg)"/>
  <!-- spotlight -->
  <ellipse cx="160" cy="80" rx="120" ry="60" fill="#fff" opacity=".15"/>
  <!-- silhouette head -->
  <g transform="translate(120 30)">
    <rect x="20" y="0" width="40" height="50" fill="#1a1530"/>
    <rect x="14" y="6" width="52" height="40" fill="#1a1530"/>
    <!-- mask band -->
    <rect x="14" y="14" width="52" height="10" fill="#0a0a18"/>
    <rect x="14" y="14" width="52" height="2" fill="#3a2050"/>
    <rect x="14" y="22" width="52" height="2" fill="#3a2050"/>
    <!-- mask eye holes -->
    <rect x="22" y="17" width="6" height="4" fill="#fff5d8"/>
    <rect x="52" y="17" width="6" height="4" fill="#fff5d8"/>
  </g>
  <!-- question marks -->
  <text x="60" y="80" font-family="serif" font-size="60" font-weight="bold"
        fill="#ffd84a" opacity=".8">?</text>
  <text x="240" y="120" font-family="serif" font-size="50" font-weight="bold"
        fill="#ff8aa6" opacity=".8">?</text>
  <text x="160" y="148" font-family="serif" font-size="24"
        font-weight="bold" text-anchor="middle" fill="#fff">谁是卧底</text>
"""
    return svg_doc("0 0 320 160", body, label="卧底游戏")


def gen_pickbottle() -> str:
    """Picking up a bottle on the beach — happy Sūsū holding bottle."""
    bottle_inset = """
  <!-- bottle in hand at right side -->
  <g transform="translate(132 100)">
    <rect x="6" y="0" width="8" height="3" fill="#a06a3a"/>
    <rect x="6" y="3" width="8" height="2" fill="#7a4a25"/>
    <rect x="7" y="5" width="6" height="6" fill="#cdeaf0"/>
    <rect x="5" y="11" width="10" height="14" fill="#cdeaf0"/>
    <rect x="7" y="14" width="6" height="6" fill="#fff7d6"/>
    <rect x="9" y="16" width="2" height="2" fill="#d83a4d"/>
    <rect x="5" y="11" width="2" height="14" fill="#a3d3df"/>
    <rect x="13" y="11" width="2" height="14" fill="#5d8da0"/>
  </g>
"""
    return gen_emotion(
        "捡到一个瓶子",
        eye="sparkle",
        mouth="grin",
        accent="#ffe9d6",
        accent_dark="#ffb6c4",
        ornaments=bottle_inset,
    )


def gen_treasure_chest() -> str:
    """道具宝箱 — opened wooden chest with jewels overflowing."""
    body = f"""
  <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%"  stop-color="#3a2c1a"/>
    <stop offset="100%" stop-color="#a0703a"/>
  </linearGradient>
  <rect width="200" height="160" fill="url(#bg)"/>
  <!-- glow -->
  <ellipse cx="100" cy="80" rx="80" ry="40" fill="#ffd84a" opacity=".4"/>
  <!-- chest body -->
  <g transform="translate(40 70)">
    <rect x="0" y="20" width="120" height="60" fill="#7a4a25"/>
    <rect x="0" y="20" width="120" height="4"  fill="#a06a3a"/>
    <rect x="0" y="76" width="120" height="4"  fill="#3a1c0a"/>
    <rect x="0" y="20" width="4" height="60"  fill="#3a1c0a"/>
    <rect x="116" y="20" width="4" height="60" fill="#3a1c0a"/>
    <!-- iron bands -->
    <rect x="0" y="40" width="120" height="3"  fill="#444"/>
    <rect x="0" y="60" width="120" height="3"  fill="#444"/>
    <!-- lid open (rotated upward) -->
    <g transform="translate(0 -34) skewY(-10)">
      <rect x="0" y="0" width="120" height="34" fill="#a06a3a"/>
      <rect x="0" y="0" width="120" height="4"  fill="#cd9050"/>
      <rect x="0" y="30" width="120" height="4" fill="#3a1c0a"/>
      <rect x="0" y="0" width="4" height="34"   fill="#3a1c0a"/>
      <rect x="116" y="0" width="4" height="34" fill="#3a1c0a"/>
      <rect x="0" y="14" width="120" height="3" fill="#444"/>
      <!-- lock plate -->
      <rect x="56" y="20" width="10" height="14" fill="#fcd34d"/>
      <rect x="56" y="20" width="10" height="2" fill="#a02030"/>
      <rect x="60" y="26" width="2" height="4" fill="#3a1c0a"/>
    </g>
  </g>
  <!-- treasures spilling -->
  <g transform="translate(78 76)">
    {lingyu_gem(0, 0, scale=1)}
  </g>
  <g transform="translate(50 88)">
    {lingyu_gem(0, 0, scale=1)}
  </g>
  <g transform="translate(108 90)">
    {lingyu_gem(0, 0, scale=1)}
  </g>
  <!-- coins -->
  <g fill="#fcd34d">
    <circle cx="40" cy="120" r="6"/>
    <circle cx="160" cy="118" r="6"/>
    <circle cx="100" cy="124" r="5"/>
  </g>
  <g fill="#e0a92c">
    <circle cx="40" cy="120" r="6" fill="none" stroke="#e0a92c"/>
    <circle cx="160" cy="118" r="6" fill="none" stroke="#e0a92c"/>
  </g>
  <!-- sparkles -->
  <g fill="#fff">
    <rect x="44" y="58" width="2" height="2"/>
    <rect x="42" y="60" width="6" height="1"/>
    <rect x="45" y="56" width="1" height="6"/>
    <rect x="148" y="46" width="2" height="2"/>
    <rect x="146" y="48" width="6" height="1"/>
    <rect x="149" y="44" width="1" height="6"/>
  </g>
"""
    return svg_doc("0 0 200 160", body, label="道具宝箱")


def gen_who_are_you() -> str:
    """`唉，你是？` — confused Sūsū tilting head with question mark."""
    qmark = """
  <text x="160" y="60" font-family="serif" font-size="48" font-weight="bold"
        fill="#ff8aa6">?</text>
  <text x="40" y="80" font-family="serif" font-size="32" font-weight="bold"
        fill="#ffb84a">?</text>
"""
    return gen_emotion(
        "唉你是",
        eye="open",
        mouth="o",
        accent="#fff5e0",
        accent_dark="#ffd0d6",
        ornaments=qmark,
    )


def gen_lingyu_insufficient() -> str:
    """灵玉不足100 — gem with red X overlay."""
    body = f"""
  <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%"  stop-color="#fff0e8"/>
    <stop offset="100%" stop-color="#ffd0c8"/>
  </linearGradient>
  <rect width="200" height="120" fill="url(#bg)"/>
  <g transform="translate(70 30)">
    {lingyu_gem(0, 0, scale=2)}
  </g>
  <!-- red X overlay -->
  <g stroke="#d83a4d" stroke-width="6" stroke-linecap="round">
    <line x1="60" y1="20" x2="140" y2="100"/>
    <line x1="140" y1="20" x2="60" y2="100"/>
  </g>
  <text x="100" y="115" font-family="serif" font-size="14" font-weight="bold"
        text-anchor="middle" fill="{PINK_DEEP}">灵玉不足</text>
"""
    return svg_doc("0 0 200 120", body, label="灵玉不足")


def gen_jinyan_success() -> str:
    """禁言成功 — chain icon + sparkle."""
    body = f"""
  <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%"  stop-color="#fff5e8"/>
    <stop offset="100%" stop-color="#ffd6a8"/>
  </linearGradient>
  <rect width="200" height="160" fill="url(#bg)"/>
  <!-- talisman strip overlapping a closed mouth -->
  <g transform="translate(50 30)">
    <rect x="0" y="0" width="100" height="50" fill="#fcd34d"/>
    <rect x="0" y="0" width="100" height="3" fill="#e0a92c"/>
    <rect x="0" y="47" width="100" height="3" fill="#e0a92c"/>
    <rect x="0" y="0" width="3" height="50" fill="#e0a92c"/>
    <rect x="97" y="0" width="3" height="50" fill="#e0a92c"/>
    <!-- 禁 stylised -->
    <rect x="38" y="8" width="2" height="34" fill="#a02030"/>
    <rect x="44" y="6" width="2" height="36" fill="#a02030"/>
    <rect x="50" y="6" width="2" height="36" fill="#a02030"/>
    <rect x="56" y="8" width="2" height="34" fill="#a02030"/>
    <rect x="34" y="14" width="28" height="2" fill="#a02030"/>
    <rect x="34" y="22" width="28" height="2" fill="#a02030"/>
    <rect x="34" y="30" width="28" height="2" fill="#a02030"/>
    <!-- ribbons -->
    <rect x="46" y="50" width="8" height="14" fill="#a02030"/>
    <rect x="44" y="64" width="12" height="3" fill="#7a1622"/>
  </g>
  <!-- sparkles -->
  <g fill="#fff">
    <rect x="40" y="20" width="2" height="2"/>
    <rect x="38" y="22" width="6" height="1"/>
    <rect x="170" y="100" width="2" height="2"/>
    <rect x="168" y="102" width="6" height="1"/>
  </g>
  <text x="100" y="130" font-family="serif" font-size="20" font-weight="bold"
        text-anchor="middle" fill="{PINK_DEEP}">禁言成功</text>
"""
    return svg_doc("0 0 200 160", body, label="禁言成功")


def gen_yunshi_card() -> str:
    """运势 sign — fortune slip with 卦 character."""
    body = """
  <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%"  stop-color="#fff8e8"/>
    <stop offset="100%" stop-color="#ffd6a8"/>
  </linearGradient>
  <rect width="180" height="220" fill="url(#bg)"/>
  <!-- slip paper -->
  <g transform="translate(40 30)">
    <rect x="0" y="0" width="100" height="160" fill="#fffaf0"/>
    <rect x="0" y="0" width="100" height="160" fill="none" stroke="#a05a32" stroke-width="2"/>
    <!-- top + bottom decoration -->
    <rect x="0" y="0" width="100" height="6" fill="#d83a4d"/>
    <rect x="0" y="154" width="100" height="6" fill="#d83a4d"/>
    <rect x="6" y="6" width="88" height="2" fill="#fcd34d"/>
    <rect x="6" y="152" width="88" height="2" fill="#fcd34d"/>
    <!-- 上签 -->
    <text x="50" y="40" font-family="serif" font-size="22" font-weight="bold"
          text-anchor="middle" fill="#a02030">上 签</text>
    <!-- 卦象 (eight trigrams: ䷀ heaven) -->
    <g fill="#3a2c1a">
      <rect x="20" y="64"  width="60" height="3"/>
      <rect x="20" y="76"  width="60" height="3"/>
      <rect x="20" y="88"  width="60" height="3"/>
      <rect x="20" y="100" width="60" height="3"/>
      <rect x="20" y="112" width="60" height="3"/>
      <rect x="20" y="124" width="60" height="3"/>
    </g>
    <!-- subtle 福 stamp -->
    <circle cx="78" cy="138" r="10" fill="#d83a4d" opacity=".7"/>
    <text x="78" y="142" font-family="serif" font-size="11" font-weight="bold"
          text-anchor="middle" fill="#fff">福</text>
  </g>
  <!-- tassel -->
  <rect x="86" y="190" width="6" height="20" fill="#d83a4d"/>
  <rect x="80" y="206" width="18" height="6" fill="#fcd34d"/>
"""
    return svg_doc("0 0 180 220", body, label="运势")


def gen_jibandash() -> str:
    """🦌羁绊+1 — soft sparkly heart with antlers ornament."""
    body = f"""
  <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%"  stop-color="#fff5fa"/>
    <stop offset="100%" stop-color="#ffd0e0"/>
  </linearGradient>
  <rect width="200" height="160" fill="url(#bg)"/>
  <!-- heart -->
  <g fill="{PINK_LIGHT}" transform="translate(60 30)">
    <rect x="6"  y="6"  width="16" height="6"/>
    <rect x="34" y="6"  width="16" height="6"/>
    <rect x="0"  y="12" width="56" height="20"/>
    <rect x="6"  y="32" width="44" height="6"/>
    <rect x="14" y="38" width="28" height="6"/>
    <rect x="22" y="44" width="12" height="6"/>
    <rect x="26" y="50" width="4" height="4"/>
  </g>
  <!-- highlight -->
  <rect x="68"  y="20" width="6" height="6" fill="#fff" opacity=".8"/>
  <rect x="74"  y="14" width="6" height="2" fill="#fff" opacity=".6"/>
  <!-- antlers (the 🦌 hint) -->
  <g fill="#a05a32">
    <rect x="58"  y="18" width="2" height="14"/>
    <rect x="56"  y="22" width="6" height="2"/>
    <rect x="50"  y="14" width="2" height="6"/>
    <rect x="50"  y="14" width="6" height="2"/>
    <rect x="120" y="18" width="2" height="14"/>
    <rect x="120" y="22" width="6" height="2"/>
    <rect x="128" y="14" width="2" height="6"/>
    <rect x="124" y="14" width="6" height="2"/>
  </g>
  <!-- +1 -->
  <text x="160" y="60" font-family="serif" font-size="36" font-weight="bold"
        fill="{PINK_DEEP}">+1</text>
  <text x="100" y="140" font-family="serif" font-size="20" font-weight="bold"
        text-anchor="middle" fill="{PINK_DEEP}">🦌 羁绊</text>
"""
    return svg_doc("0 0 200 160", body, label="羁绊+1")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


OUTPUTS: list[tuple[str, callable]] = [
    # icon
    ("灵玉宝石.svg", gen_lingyu_icon),
    # 御妖符 inventory badges
    ("御妖符0.svg", lambda: gen_yufu_badge(0)),
    ("御妖符1.svg", lambda: gen_yufu_badge(1)),
    ("御妖符2.svg", lambda: gen_yufu_badge(2)),
    ("御妖符3.svg", lambda: gen_yufu_badge(3)),
    ("御妖符4.svg", lambda: gen_yufu_badge(4)),
    ("御妖符5.svg", lambda: gen_yufu_badge(5)),
    ("御妖符6.svg", lambda: gen_yufu_badge(6)),
    # 排行榜 banners
    ("财富排行榜.svg", lambda: gen_rank_banner("财富")),
    ("法力排行榜.svg", lambda: gen_rank_banner("法力")),
    ("妖力排行榜.svg", lambda: gen_rank_banner("妖力")),
    # other banners
    ("御妖符指南.svg", gen_yufu_banner),
    ("我的灵玉.svg", lambda: gen_wallet_card("普通")),
    ("我的灵玉富.svg", lambda: gen_wallet_card("富")),
    ("我的灵玉浪漫.svg", lambda: gen_wallet_card("浪漫")),
    # scenes
    ("钓鱼鱼塘.svg", gen_diaoyu_banner),
    ("卧底游戏.svg", gen_woudi_banner),
    ("捡到一个瓶子.svg", gen_pickbottle),
    ("道具宝箱.svg", gen_treasure_chest),
    ("唉你是.svg", gen_who_are_you),
    ("灵玉不足.svg", gen_lingyu_insufficient),
    ("禁言成功.svg", gen_jinyan_success),
    ("运势签.svg", gen_yunshi_card),
    ("羁绊加一.svg", gen_jibandash),
    # emotion sprites
    (
        "苏苏跳舞卖萌.svg",
        lambda: gen_emotion(
            "跳舞卖萌",
            eye="closed",
            mouth="grin",
            accent="#fff1d8",
            accent_dark="#ffc0d6",
            ornaments="""
  <g fill="#ffb6d4">
    <rect x="40"  y="36" width="3" height="3"/>
    <rect x="160" y="48" width="3" height="3"/>
    <rect x="36"  y="120" width="3" height="3"/>
    <rect x="164" y="130" width="3" height="3"/>
  </g>
  <text x="40" y="150" font-family="serif" font-size="22"
        font-weight="bold" fill="{PINK_DEEP}">♪</text>
  <text x="160" y="80" font-family="serif" font-size="22"
        font-weight="bold" fill="{PINK_DEEP}">♬</text>
""",
        ),
    ),
    (
        "苏苏比心.svg",
        lambda: gen_emotion(
            "比心心",
            eye="wink_left",
            mouth="heart",
            accent="#ffe5ed",
            accent_dark="#ffb6d4",
            ornaments="""
  <g fill="#ff5577">
    <rect x="22" y="60" width="4" height="4"/>
    <rect x="20" y="62" width="2" height="2"/>
    <rect x="174" y="100" width="4" height="4"/>
    <rect x="178" y="98" width="2" height="2"/>
  </g>
""",
        ),
    ),
    (
        "苏苏吃糖.svg",
        lambda: gen_emotion(
            "吃糖卖萌",
            eye="sparkle",
            mouth="o",
            accent="#fff8d8",
            accent_dark="#ffd6a8",
            ornaments="""
  <!-- candy (lollipop) at bottom-left -->
  <g transform="translate(30 130)">
    <rect x="6" y="0" width="2" height="22" fill="#fff"/>
    <rect x="0" y="-14" width="14" height="14" fill="#ff5577"/>
    <rect x="2" y="-12" width="10" height="10" fill="#ffb6d4"/>
    <rect x="4" y="-10" width="6" height="6" fill="#fff"/>
  </g>
""",
        ),
    ),
    (
        "苏苏直接绑走.svg",
        lambda: gen_emotion(
            "直接绑走",
            eye="sparkle",
            mouth="grin",
            accent="#ffe0c8",
            accent_dark="#ff9aa8",
            ornaments="""
  <!-- pink ribbon stretched diagonally -->
  <rect x="20" y="60" width="160" height="6" fill="#ff5577" transform="rotate(-15 100 60)"/>
  <rect x="20" y="60" width="160" height="2" fill="#a02030" transform="rotate(-15 100 60)"/>
""",
        ),
    ),
    (
        "苏苏思考.svg",
        lambda: gen_emotion(
            "萌苏思考",
            eye="open",
            mouth="line",
            accent="#fff5d8",
            accent_dark="#ffd6a8",
            ornaments="""
  <text x="160" y="50" font-family="serif" font-size="32" font-weight="bold"
        fill="#a05a32">?</text>
  <g fill="#a05a32" opacity=".7">
    <circle cx="146" cy="80" r="3"/>
    <circle cx="156" cy="68" r="2"/>
  </g>
""",
        ),
    ),
    (
        "苏苏推人.svg",
        lambda: gen_emotion(
            "萌苏推人",
            eye="angry",
            mouth="pout",
            accent="#fff0d8",
            accent_dark="#ff9aa8",
            ornaments="""
  <!-- swoosh lines -->
  <g fill="#a05a32" opacity=".5">
    <rect x="20" y="100" width="20" height="2"/>
    <rect x="22" y="106" width="14" height="2"/>
    <rect x="24" y="112" width="10" height="2"/>
  </g>
""",
        ),
    ),
    (
        "苏苏喝汤.svg",
        lambda: gen_emotion(
            "沉稳喝汤",
            eye="closed",
            mouth="o",
            accent="#fff5d8",
            accent_dark="#e8c8a8",
            ornaments="""
  <!-- soup bowl steam -->
  <g fill="#fff" opacity=".7">
    <rect x="98" y="36" width="2" height="6"/>
    <rect x="106" y="30" width="2" height="6"/>
    <rect x="92" y="30" width="2" height="6"/>
  </g>
""",
        ),
    ),
    (
        "苏苏吃瓜.svg",
        lambda: gen_emotion(
            "可爱吃瓜",
            eye="open",
            mouth="o",
            accent="#fff5e0",
            accent_dark="#c8e8a8",
            ornaments="""
  <!-- watermelon slice -->
  <g transform="translate(20 130)">
    <rect x="0" y="14" width="40" height="12" fill="#3aa05c"/>
    <rect x="0" y="14" width="40" height="3"  fill="#1f6a3c"/>
    <rect x="0" y="23" width="40" height="3"  fill="#1f6a3c"/>
    <rect x="2" y="0"  width="36" height="14" fill="#ff5577"/>
    <rect x="2" y="0"  width="36" height="2"  fill="#fff" opacity=".7"/>
    <rect x="8" y="6"  width="2" height="2" fill="#3a1c0a"/>
    <rect x="18" y="4" width="2" height="2" fill="#3a1c0a"/>
    <rect x="28" y="6" width="2" height="2" fill="#3a1c0a"/>
  </g>
""",
        ),
    ),
    (
        "苏苏摸头.svg",
        lambda: gen_emotion(
            "摸头",
            eye="closed",
            mouth="smile",
            accent="#ffe5ed",
            accent_dark="#ffb6d4",
            ornaments="""
  <!-- gentle hand from above -->
  <g fill="#ffe2c4">
    <rect x="80" y="18" width="40" height="20"/>
    <rect x="78" y="20" width="2" height="14" fill="#e8c8a8"/>
    <rect x="120" y="20" width="2" height="14" fill="#e8c8a8"/>
    <rect x="80" y="18" width="40" height="2" fill="#e8c8a8"/>
    <rect x="80" y="38" width="40" height="2" fill="#e8c8a8"/>
    <!-- fingers -->
    <rect x="86" y="38" width="3" height="6"/>
    <rect x="94" y="38" width="3" height="6"/>
    <rect x="102" y="38" width="3" height="6"/>
    <rect x="110" y="38" width="3" height="6"/>
  </g>
  <!-- sparkle -->
  <g fill="#fff">
    <rect x="100" y="50" width="2" height="2"/>
    <rect x="98"  y="52" width="6" height="1"/>
  </g>
""",
        ),
    ),
    (
        "苏苏盗图可耻.svg",
        lambda: gen_emotion(
            "盗图可耻",
            eye="angry",
            mouth="grin",
            accent="#ffe0d8",
            accent_dark="#ff8a8a",
            ornaments="""
  <text x="100" y="200" font-family="serif" font-size="18"
        font-weight="bold" text-anchor="middle" fill="#a02030">⚠ 盗图可耻 ⚠</text>
""",
        ),
    ),
    (
        "苏苏没爱了.svg",
        lambda: gen_emotion(
            "没爱了",
            eye="sad",
            mouth="pout",
            accent="#e8e8f0",
            accent_dark="#a8a8c0",
            ornaments="""
  <!-- broken heart -->
  <g fill="#888">
    <rect x="160" y="40" width="3" height="3"/>
    <rect x="166" y="40" width="3" height="3"/>
    <rect x="158" y="43" width="13" height="6"/>
    <rect x="160" y="49" width="9" height="3"/>
    <rect x="162" y="52" width="5" height="3"/>
    <rect x="164" y="55" width="2" height="2"/>
  </g>
  <!-- crack -->
  <g stroke="#fff" stroke-width="2" fill="none">
    <polyline points="164,40 160,46 168,52 162,58"/>
  </g>
""",
        ),
    ),
    (
        "苏苏不理你.svg",
        lambda: gen_emotion(
            "不理你",
            eye="closed",
            mouth="line",
            accent="#fff5d8",
            accent_dark="#ffc8a0",
            ornaments="""
  <!-- ⤵ humpf cloud -->
  <g fill="#fff" opacity=".7">
    <rect x="40" y="60" width="14" height="6"/>
    <rect x="36" y="64" width="22" height="6"/>
  </g>
  <text x="40" y="80" font-family="serif" font-size="14" fill="#a05a32">哼!</text>
""",
        ),
    ),
    (
        "苏苏听到了.svg",
        lambda: gen_emotion(
            "听到了听到了",
            eye="half",
            mouth="line",
            accent="#fff5d8",
            accent_dark="#ffd6a8",
            ornaments="""
  <text x="160" y="60" font-family="serif" font-size="28"
        font-weight="bold" fill="#a05a32">…</text>
""",
        ),
    ),
    (
        "苏苏耐心.svg",
        lambda: gen_emotion(
            "耐心耗尽",
            eye="half",
            mouth="pout",
            accent="#fff0d8",
            accent_dark="#ffb88a",
            ornaments="""
  <text x="160" y="80" font-family="serif" font-size="20"
        font-weight="bold" fill="{PINK_DEEP}">…?</text>
""",
        ),
    ),
]


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for name, fn in OUTPUTS:
        target = OUT_DIR / name
        try:
            content = fn()
        except Exception as exc:  # pragma: no cover — manual run
            print(f"!! {name}: {exc}")
            raise
        target.write_text(content, encoding="utf-8")
        written += 1
        print(f"  wrote {name} ({len(content):>5d} bytes)")
    print(f"\nGenerated {written} SVGs into {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
