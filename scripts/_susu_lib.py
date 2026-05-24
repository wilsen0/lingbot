"""
Reusable pixel-art Sūsū primitives for the linling SVG asset set.

Used by ``scripts/gen_susu_svgs.py`` to emit consistent character art
across two-dozen replacement SVGs that go into ``bot/assets/picture/``.

Goals:
- Visual consistency across many sprites (same palette, same head)
- Each emitted SVG is self-contained (no cross-file refs)
- Character keys: bright golden hair, FOLDED ears, big emerald eyes,
  pink hanfu top with white V-collar, red waist ribbon, big white
  fox tail, golden bell. No ahoge.

All primitives draw onto an integer pixel grid and rely on
``shape-rendering="crispEdges"`` to keep edges sharp at any scale.
This file has no runtime cost in the bot — it's only invoked offline
by the generator script.
"""

# Palette (single source of truth)
HAIR        = "#ffd84a"
HAIR_DARK   = "#d8a020"
SKIN        = "#fff0d8"
SKIN_LINE   = "#e8c8a8"
EYE         = "#3aa05c"
EYE_DARK    = "#1f6a3c"
LINE        = "#5b3022"
PINK        = "#ff8aa6"
PINK_DARK   = "#d83a4d"
PINK_DEEP   = "#a02030"
PINK_LIGHT  = "#ff5577"
WHITE       = "#fff"
WHITE_FUR   = "#fff8e8"
FUR_SHADOW  = "#e8d8b8"
RED         = "#d83a4d"
GOLD        = "#fcd34d"
GOLD_DARK   = "#e0a92c"
GOLD_LIGHT  = "#fff7c4"
CHEEK       = "#ff8aa6"

PIXEL_DEFS = f'''
    <radialGradient id="cheek" cx="50%" cy="50%" r="50%">
      <stop offset="0%"  stop-color="{CHEEK}" stop-opacity=".9"/>
      <stop offset="100%" stop-color="{CHEEK}" stop-opacity="0"/>
    </radialGradient>
'''


def folded_ears(cx_left: int, cx_right: int, cy: int) -> str:
    """Folded fox ears drooping outward + downward.

    cx_left/cx_right are the centerline x of each ear's attachment to
    the head; cy is the top-of-ear at the attachment point.
    """
    parts = []
    L = cx_left
    parts.append(f'<rect x="{L+0}" y="{cy+0}" width="2"  height="2" fill="{HAIR_DARK}"/>')
    parts.append(f'<rect x="{L-2}" y="{cy+2}" width="6"  height="2" fill="{HAIR_DARK}"/>')
    parts.append(f'<rect x="{L-4}" y="{cy+4}" width="10" height="2" fill="{HAIR}"/>')
    parts.append(f'<rect x="{L-6}" y="{cy+6}" width="12" height="2" fill="{HAIR}"/>')
    parts.append(f'<rect x="{L-6}" y="{cy+8}" width="10" height="2" fill="{HAIR}"/>')
    parts.append(f'<rect x="{L-4}" y="{cy+10}" width="6"  height="2" fill="{HAIR}"/>')
    parts.append(f'<rect x="{L-2}" y="{cy+12}" width="2"  height="2" fill="{HAIR_DARK}"/>')
    parts.append(f'<rect x="{L-2}" y="{cy+8}" width="4" height="2" fill="{WHITE_FUR}"/>')
    parts.append(f'<rect x="{L+0}" y="{cy+10}" width="2" height="2" fill="{WHITE_FUR}"/>')
    parts.append(f'<rect x="{L-6}" y="{cy+6}" width="2" height="2" fill="{HAIR_DARK}"/>')
    parts.append(f'<rect x="{L-6}" y="{cy+8}" width="2" height="2" fill="{HAIR_DARK}"/>')
    R = cx_right
    parts.append(f'<rect x="{R+0}" y="{cy+0}" width="2"  height="2" fill="{HAIR_DARK}"/>')
    parts.append(f'<rect x="{R-2}" y="{cy+2}" width="6"  height="2" fill="{HAIR_DARK}"/>')
    parts.append(f'<rect x="{R-2}" y="{cy+4}" width="10" height="2" fill="{HAIR}"/>')
    parts.append(f'<rect x="{R-2}" y="{cy+6}" width="12" height="2" fill="{HAIR}"/>')
    parts.append(f'<rect x="{R+0}" y="{cy+8}" width="10" height="2" fill="{HAIR}"/>')
    parts.append(f'<rect x="{R+2}" y="{cy+10}" width="6"  height="2" fill="{HAIR}"/>')
    parts.append(f'<rect x="{R+4}" y="{cy+12}" width="2"  height="2" fill="{HAIR_DARK}"/>')
    parts.append(f'<rect x="{R+2}" y="{cy+8}" width="4" height="2" fill="{WHITE_FUR}"/>')
    parts.append(f'<rect x="{R+2}" y="{cy+10}" width="2" height="2" fill="{WHITE_FUR}"/>')
    parts.append(f'<rect x="{R+8}" y="{cy+6}" width="2" height="2" fill="{HAIR_DARK}"/>')
    parts.append(f'<rect x="{R+8}" y="{cy+8}" width="2" height="2" fill="{HAIR_DARK}"/>')
    return "\n      ".join(parts)


def head(x: int, y: int, *, eye: str = "open", mouth: str = "smile",
         blush: bool = True) -> str:
    """A standard head at (x,y) — top-left of a 36×36 bounding box.

    Includes hair fringe, side bangs, ears, face features.

    eye options: open / closed / half / sparkle / wink_left / sad /
        cry / angry / shock
    mouth options: smile / o / yawn / grin / pout / line / heart
    """
    out = []
    out.append(f'<rect x="{x+2}" y="{y+0}" width="32" height="36" fill="{HAIR}"/>')
    out.append(f'<rect x="{x+0}" y="{y+4}" width="2" height="32" fill="{HAIR_DARK}"/>')
    out.append(f'<rect x="{x+34}" y="{y+4}" width="2" height="32" fill="{HAIR_DARK}"/>')
    out.append(f'<rect x="{x+2}" y="{y+0}" width="32" height="2" fill="{HAIR_DARK}"/>')

    out.append(folded_ears(x + 4, x + 30, y - 2))

    out.append(f'<rect x="{x+4}" y="{y+8}" width="28" height="22" fill="{SKIN}"/>')
    out.append(f'<rect x="{x+2}" y="{y+10}" width="2" height="18" fill="{SKIN_LINE}"/>')
    out.append(f'<rect x="{x+32}" y="{y+10}" width="2" height="18" fill="{SKIN_LINE}"/>')
    out.append(f'<rect x="{x+4}" y="{y+8}" width="28" height="2" fill="{SKIN_LINE}"/>')
    out.append(f'<rect x="{x+4}" y="{y+30}" width="28" height="2" fill="{SKIN_LINE}"/>')

    # fringe
    out.append(f'<rect x="{x+4}" y="{y+8}" width="28" height="6" fill="{HAIR}"/>')
    out.append(f'<rect x="{x+6}" y="{y+14}" width="6" height="2" fill="{HAIR}"/>')
    out.append(f'<rect x="{x+16}" y="{y+14}" width="4" height="2" fill="{HAIR}"/>')
    out.append(f'<rect x="{x+24}" y="{y+14}" width="6" height="2" fill="{HAIR}"/>')

    L_EYE = x + 8
    R_EYE = x + 22
    if eye == "closed":
        for cx in (L_EYE, R_EYE):
            out.append(f'<rect x="{cx-1}" y="{y+19}" width="2" height="2" fill="{LINE}"/>')
            out.append(f'<rect x="{cx+1}" y="{y+18}" width="2" height="2" fill="{LINE}"/>')
            out.append(f'<rect x="{cx+3}" y="{y+19}" width="2" height="2" fill="{LINE}"/>')
            out.append(f'<rect x="{cx+5}" y="{y+20}" width="2" height="2" fill="{LINE}"/>')
    elif eye == "half":
        for cx in (L_EYE, R_EYE):
            out.append(f'<rect x="{cx}" y="{y+18}" width="6" height="6" fill="{EYE}"/>')
            out.append(f'<rect x="{cx}" y="{y+18}" width="6" height="3" fill="{LINE}"/>')
            out.append(f'<rect x="{cx+2}" y="{y+22}" width="2" height="2" fill="{WHITE}"/>')
    elif eye == "sparkle":
        for cx in (L_EYE, R_EYE):
            out.append(f'<rect x="{cx}" y="{y+18}" width="6" height="6" fill="{EYE}"/>')
            out.append(f'<rect x="{cx}" y="{y+18}" width="6" height="2" fill="{EYE_DARK}"/>')
            out.append(f'<rect x="{cx+1}" y="{y+19}" width="2" height="2" fill="{WHITE}"/>')
            out.append(f'<rect x="{cx+3}" y="{y+22}" width="2" height="1" fill="{WHITE}"/>')
            out.append(f'<rect x="{cx+2}" y="{y+20}" width="1" height="1" fill="{WHITE}"/>')
    elif eye == "wink_left":
        cx = L_EYE
        out.append(f'<rect x="{cx-1}" y="{y+19}" width="2" height="2" fill="{LINE}"/>')
        out.append(f'<rect x="{cx+1}" y="{y+18}" width="2" height="2" fill="{LINE}"/>')
        out.append(f'<rect x="{cx+3}" y="{y+19}" width="2" height="2" fill="{LINE}"/>')
        cx = R_EYE
        out.append(f'<rect x="{cx}" y="{y+18}" width="6" height="6" fill="{EYE}"/>')
        out.append(f'<rect x="{cx}" y="{y+18}" width="6" height="2" fill="{EYE_DARK}"/>')
        out.append(f'<rect x="{cx+1}" y="{y+19}" width="2" height="2" fill="{WHITE}"/>')
    elif eye == "sad":
        for cx in (L_EYE, R_EYE):
            out.append(f'<rect x="{cx}" y="{y+19}" width="6" height="4" fill="{EYE}"/>')
            out.append(f'<rect x="{cx-1}" y="{y+18}" width="2" height="1" fill="{LINE}"/>')
            out.append(f'<rect x="{cx+5}" y="{y+18}" width="2" height="1" fill="{LINE}"/>')
            out.append(f'<rect x="{cx+1}" y="{y+20}" width="2" height="2" fill="{WHITE}"/>')
    elif eye == "cry":
        for cx in (L_EYE, R_EYE):
            out.append(f'<rect x="{cx}" y="{y+18}" width="6" height="6" fill="{EYE}"/>')
            out.append(f'<rect x="{cx+1}" y="{y+19}" width="2" height="2" fill="{WHITE}"/>')
            out.append(f'<rect x="{cx+2}" y="{y+24}" width="2" height="3" fill="#88ccff"/>')
            out.append(f'<rect x="{cx+1}" y="{y+25}" width="4" height="2" fill="#88ccff" opacity=".7"/>')
    elif eye == "angry":
        for cx in (L_EYE, R_EYE):
            out.append(f'<rect x="{cx}" y="{y+19}" width="6" height="4" fill="{EYE}"/>')
            out.append(f'<rect x="{cx}" y="{y+19}" width="6" height="2" fill="{LINE}"/>')
            out.append(f'<rect x="{cx+1}" y="{y+21}" width="2" height="1" fill="{WHITE}"/>')
        out.append(f'<rect x="{x+8}" y="{y+16}" width="6" height="1" fill="{LINE}"/>')
        out.append(f'<rect x="{x+9}" y="{y+15}" width="3" height="1" fill="{LINE}"/>')
        out.append(f'<rect x="{x+22}" y="{y+16}" width="6" height="1" fill="{LINE}"/>')
        out.append(f'<rect x="{x+24}" y="{y+15}" width="3" height="1" fill="{LINE}"/>')
    elif eye == "shock":
        for cx in (L_EYE, R_EYE):
            out.append(f'<rect x="{cx+1}" y="{y+19}" width="3" height="4" fill="{EYE}"/>')
            out.append(f'<rect x="{cx+1}" y="{y+19}" width="3" height="2" fill="{LINE}"/>')
            out.append(f'<rect x="{cx+1}" y="{y+21}" width="2" height="1" fill="{WHITE}"/>')
    else:  # open
        for cx in (L_EYE, R_EYE):
            out.append(f'<rect x="{cx}" y="{y+18}" width="6" height="6" fill="{EYE}"/>')
            out.append(f'<rect x="{cx}" y="{y+18}" width="6" height="2" fill="{EYE_DARK}"/>')
            out.append(f'<rect x="{cx+1}" y="{y+19}" width="2" height="2" fill="{WHITE}"/>')
            out.append(f'<rect x="{cx+3}" y="{y+22}" width="1" height="1" fill="{WHITE}"/>')

    if blush:
        out.append(f'<ellipse cx="{x+10}" cy="{y+27}" rx="3" ry="2" fill="url(#cheek)"/>')
        out.append(f'<ellipse cx="{x+26}" cy="{y+27}" rx="3" ry="2" fill="url(#cheek)"/>')

    cx = x + 18
    if mouth == "o":
        out.append(f'<rect x="{cx-1}" y="{y+25}" width="4" height="3" fill="#7a1622"/>')
        out.append(f'<rect x="{cx}" y="{y+26}" width="2" height="1" fill="{PINK}"/>')
    elif mouth == "yawn":
        out.append(f'<rect x="{cx-2}" y="{y+25}" width="6" height="4" fill="#7a1622"/>')
        out.append(f'<rect x="{cx-1}" y="{y+26}" width="4" height="2" fill="{PINK}"/>')
    elif mouth == "grin":
        out.append(f'<rect x="{cx-3}" y="{y+25}" width="8" height="2" fill="{PINK_DARK}"/>')
        out.append(f'<rect x="{cx-2}" y="{y+27}" width="6" height="1" fill="{PINK_DARK}"/>')
    elif mouth == "pout":
        out.append(f'<rect x="{cx-1}" y="{y+25}" width="3" height="2" fill="{PINK_DARK}"/>')
    elif mouth == "line":
        out.append(f'<rect x="{cx-2}" y="{y+26}" width="5" height="1" fill="{PINK_DARK}"/>')
    elif mouth == "heart":
        out.append(f'<rect x="{cx-2}" y="{y+25}" width="2" height="1" fill="{PINK_LIGHT}"/>')
        out.append(f'<rect x="{cx}" y="{y+25}" width="2" height="1" fill="{PINK_LIGHT}"/>')
        out.append(f'<rect x="{cx-3}" y="{y+26}" width="6" height="1" fill="{PINK_LIGHT}"/>')
        out.append(f'<rect x="{cx-2}" y="{y+27}" width="4" height="1" fill="{PINK_LIGHT}"/>')
        out.append(f'<rect x="{cx-1}" y="{y+28}" width="2" height="1" fill="{PINK_LIGHT}"/>')
    else:
        out.append(f'<rect x="{cx-1}" y="{y+26}" width="2" height="2" fill="{PINK_DARK}"/>')
        out.append(f'<rect x="{cx+1}" y="{y+26}" width="2" height="2" fill="{PINK_DARK}"/>')

    return "\n  ".join(out)


def hanfu_top(x: int, y: int, w: int = 60, h: int = 26) -> str:
    """Pink hanfu top with V-collar and red waist sash."""
    return f'''
  <rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{PINK}"/>
  <rect x="{x}" y="{y}" width="{w}" height="2" fill="{PINK_DARK}"/>
  <rect x="{x}" y="{y+h-2}" width="{w}" height="2" fill="{PINK_DARK}"/>
  <rect x="{x}" y="{y}" width="2" height="{h}" fill="{PINK_DARK}"/>
  <rect x="{x+w-2}" y="{y}" width="2" height="{h}" fill="{PINK_DARK}"/>
  <rect x="{x + w//2 - 6}" y="{y}" width="12" height="8" fill="{WHITE}"/>
  <rect x="{x + w//2 - 3}" y="{y+8}" width="6" height="3" fill="{WHITE}"/>
  <rect x="{x + w//2 - 6}" y="{y}" width="2" height="8" fill="{SKIN_LINE}"/>
  <rect x="{x + w//2 + 4}" y="{y}" width="2" height="8" fill="{SKIN_LINE}"/>
  <rect x="{x}" y="{y + h - 6}" width="{w}" height="4" fill="{RED}"/>
  <rect x="{x + w//2 - 4}" y="{y + h - 8}" width="8" height="6" fill="{PINK_LIGHT}"/>
  <rect x="{x + w//2 - 4}" y="{y + h - 8}" width="2" height="6" fill="{PINK_DEEP}"/>
  <rect x="{x + w//2 + 2}" y="{y + h - 8}" width="2" height="6" fill="{PINK_DEEP}"/>
  <rect x="{x + w//2 - 2}" y="{y + h}" width="2" height="6" fill="{RED}"/>
  <rect x="{x + w//2 + 0}" y="{y + h}" width="2" height="8" fill="{RED}"/>
'''


def fox_tail_back(x: int, y: int) -> str:
    """Big white fluffy fox tail behind body. ~60×32."""
    return f'''
  <rect x="{x+10}" y="{y+0}"  width="36" height="4" fill="{WHITE_FUR}"/>
  <rect x="{x+6}"  y="{y+4}"  width="44" height="8" fill="{WHITE_FUR}"/>
  <rect x="{x+2}"  y="{y+12}" width="52" height="10" fill="{WHITE_FUR}"/>
  <rect x="{x+0}"  y="{y+22}" width="56" height="8" fill="{WHITE_FUR}"/>
  <rect x="{x+8}"  y="{y+30}" width="40" height="2" fill="{WHITE_FUR}"/>
  <rect x="{x+0}"  y="{y+12}" width="2" height="18" fill="{FUR_SHADOW}"/>
  <rect x="{x+54}" y="{y+12}" width="2" height="18" fill="{FUR_SHADOW}"/>
  <rect x="{x+44}" y="{y+18}" width="14" height="6" fill="{HAIR}"/>
  <rect x="{x+48}" y="{y+24}" width="10" height="4" fill="{HAIR}"/>
  <rect x="{x+44}" y="{y+18}" width="2" height="6" fill="{HAIR_DARK}"/>
'''


def lingyu_gem(x: int, y: int, scale: int = 1) -> str:
    """The 灵玉 (spirit jade) gem icon — a faceted pink-purple gem.

    Used as the project's currency symbol; appears all over排行榜 etc.
    Pixel diamond shape ~24x28 at scale=1.
    """
    s = scale
    out = []
    # outer outline
    coords = [
        (10, 0, 4, 2, "#7a1d6a"),
        (8, 2, 8, 2, "#a83b9a"),
        (6, 4, 12, 2, "#a83b9a"),
        (4, 6, 16, 2, "#a83b9a"),
        (2, 8, 20, 2, "#a83b9a"),
        (0, 10, 24, 2, "#a83b9a"),
        (2, 12, 20, 6, "#d672c4"),
        (4, 18, 16, 2, "#a83b9a"),
        (6, 20, 12, 2, "#a83b9a"),
        (8, 22, 8, 2, "#a83b9a"),
        (10, 24, 4, 2, "#7a1d6a"),
        # facets
        (8, 4, 2, 4, "#fff8ff"),
        (10, 4, 4, 2, "#ffd0f0"),
        (10, 8, 2, 6, "#ffd0f0"),
    ]
    for cx, cy, cw, ch, col in coords:
        out.append(f'<rect x="{x + cx*s}" y="{y + cy*s}" width="{cw*s}" height="{ch*s}" fill="{col}"/>')
    return "\n  ".join(out)
