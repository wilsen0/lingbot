"""Image-text rendering — Pillow replacement for QRDic's ``图文.java``.

Renders a block of text (newline-delimited) to a PNG file and returns the
file path. Configuration mirrors the original:

- ``font_size``: text size in pixels
- ``padding``: per-side margin around the text block (default ``540``)
- ``background`` / ``text_color``: colours accepted by Pillow
- ``bold`` / ``underline`` / ``strikethru``: text decorations

If ``ctx.extras["image_text_font"]`` is provided, it's used as the TTF
path; otherwise Pillow's bitmap default font is used (small, ASCII only —
but keeps tests free of external font dependencies).
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from linling_core.tools import ToolCtx, tool
from PIL import Image, ImageDraw, ImageFont


def _load_font(path: str | None, size: int) -> Any:
    if path:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            # Fall through to the default font on load failure so the
            # caller still gets a usable image rather than an exception.
            pass
    return ImageFont.load_default()


def _measure(draw: Any, text: str, font: Any) -> tuple[int, int]:
    """Return (width, height) of a single line of *text* rendered with *font*."""
    # Pillow >=9 uses ``textbbox``; older uses ``textsize``.
    if hasattr(draw, "textbbox"):
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        return right - left, bottom - top
    width, height = draw.textsize(text, font=font)
    return int(width), int(height)


@tool(
    name="image_text",
    dsl_name="图文",
    description="Render text to a PNG image and return the file path",
    schema={
        "content": "string",
    },
    safe=False,
)
async def image_text(
    ctx: ToolCtx,
    *content_parts: str,
    content: str | None = None,
    font_size: int | None = None,
    padding: int | None = None,
    background: str | None = None,
    text_color: str | None = None,
    bold: bool | None = None,
    underline: bool | None = None,
    strikethru: bool | None = None,
) -> str:
    """Render *content* to a PNG image and return the file path.

    The DSL tokenises a call like ``$图文 加入成功 请等待另一名玩家$``
    on whitespace; each token reaches us as one positional ``str``.
    We join them back with single spaces — matches QRDic's "everything
    after the tool name is content" convention. Newlines authored as
    literal ``\\n`` in the DSL source were already decoded to real
    newlines by the parser-side escape pass, so split-on-``\\n`` here
    sees the right boundaries.

    Direct Python callers (the unit tests, agent integrations) can
    still pass ``content=...`` and the style flags as keyword args —
    the DSL never goes through that path. The keyword form takes
    precedence over the positional join when both are present.

    The font is taken from ``ctx.extras["image_text_font"]`` when set,
    otherwise Pillow's default bitmap font is used. Output goes to
    ``ctx.extras["image_text_cache_dir"]`` or ``./data/cache/``.

    Style flags from the DSL come from ``ctx.extras["image_text_options"]``
    (a dict) when present; the DSL has no shape for keyword args.
    Defaults match the original ``BSH 图文.java`` settings.
    """
    if content is None:
        content = " ".join(str(p) for p in content_parts)

    options: dict[str, Any] = {}
    raw_opts = ctx.extras.get("image_text_options")
    if isinstance(raw_opts, dict):
        options = raw_opts

    def _pick_int(kw: int | None, key: str, default: int) -> int:
        if kw is not None:
            return int(kw)
        return int(options.get(key, default))

    def _pick_str(kw: str | None, key: str, default: str) -> str:
        if kw is not None:
            return str(kw)
        return str(options.get(key, default))

    def _pick_bool(kw: bool | None, key: str, default: bool) -> bool:
        if kw is not None:
            return bool(kw)
        return bool(options.get(key, default))

    font_size = _pick_int(font_size, "font_size", 50)
    padding = _pick_int(padding, "padding", 540)
    background = _pick_str(background, "background", "#FFFFFF")
    text_color = _pick_str(text_color, "text_color", "#000000")
    bold = _pick_bool(bold, "bold", False)
    underline = _pick_bool(underline, "underline", False)
    strikethru = _pick_bool(strikethru, "strikethru", False)

    font_path = ctx.extras.get("image_text_font")
    cache_dir = Path(ctx.extras.get("image_text_cache_dir") or "./data/cache")
    cache_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 — filesystem setup is cheap

    lines = content.split("\n")
    font = _load_font(font_path if isinstance(font_path, str) else None, font_size)

    # Measure using a throwaway 1x1 canvas so we can size the real canvas.
    probe = Image.new("RGB", (1, 1), background)
    probe_draw = ImageDraw.Draw(probe)

    line_heights: list[int] = []
    max_width = 1
    for line in lines:
        w, h = _measure(probe_draw, line or " ", font)
        max_width = max(max_width, w)
        # Give each line at least the nominal font size of vertical space;
        # this keeps blank lines visible.
        line_heights.append(max(h, font_size))

    width = 2 * padding + max_width
    height = 2 * padding + sum(line_heights)
    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)

    y = padding
    for line, h in zip(lines, line_heights, strict=True):
        draw.text((padding, y), line, fill=text_color, font=font)
        line_w, _ = _measure(draw, line or "", font)
        if bold:
            # Cheap bold: re-draw one pixel to the right.
            draw.text((padding + 1, y), line, fill=text_color, font=font)
        if underline and line:
            underline_y = y + h + 2
            draw.line(
                [(padding, underline_y), (padding + line_w, underline_y)],
                fill=text_color,
                width=max(1, font_size // 20),
            )
        if strikethru and line:
            strike_y = y + h // 2
            draw.line(
                [(padding, strike_y), (padding + line_w, strike_y)],
                fill=text_color,
                width=max(1, font_size // 20),
            )
        y += h

    unique = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    out_path = cache_dir / f"{unique}.png"
    image.save(out_path, format="PNG")
    return str(out_path)
