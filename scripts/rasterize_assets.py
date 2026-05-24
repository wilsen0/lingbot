"""Rasterize ``bot/assets/picture/*.svg`` to formats QQ accepts.

Why
---

QQ's rich-media protocol rejects raw SVG uploads with
``rich media transfer failed``. The OneBot adapter inlines local
sprites as ``base64://...`` after the file:// fix, but a base64-encoded
SVG body still fails server-side. This script renders each SVG to a
sibling raster file the adapter can ship instead.

The OneBot adapter's ``_resolve_asset_path`` already walks
``_ASSET_FALLBACK_EXTS = ('.svg', '.jpg', '.png', '.gif', ...)``: when
a rule emits ``±img=@pic:foo.svg±`` and ``foo.png`` exists alongside,
the resolver picks the PNG. So this script's only job is to *populate*
those siblings.

Static vs animated
------------------

* Static SVG  → render once with cairosvg → ``foo.png`` (and reorder
  the fallback so PNG wins over the SVG).
* Animated SVG (any ``<animate>`` / ``<animateTransform>``) → render
  N evenly-spaced frames, compose an animated GIF → ``foo.gif``.

The supported SMIL primitives are exactly what the bundled sprites use:

* ``<animate attributeName="opacity" values="..." dur="...s"
  repeatCount="indefinite">`` — interpolated numerically.
* ``<animate attributeName="(cy|y|x|cx|...)" ...>`` — same, applied to
  the parent element's attribute.
* ``<animateTransform type="translate" values="x1,y1; x2,y2; ..."
  dur="...s">`` — interpolated coordinate pair, written into the
  parent's ``transform`` attribute as ``translate(x, y)``.
* ``<animateTransform type="rotate" values="a1; a2; ..." dur="...s">``
  — same, ``rotate(angle)``.

Anything else is left as-is in the rendered frame; if a future SVG
introduces a new primitive the conversion still produces *something*,
just without the unsupported animation.

Usage
-----

::

    uv run --with cairosvg --with lxml --with pillow \
        python scripts/rasterize_assets.py
    uv run --with cairosvg --with lxml --with pillow \
        python scripts/rasterize_assets.py --force

Pass ``--force`` to re-render even when the sibling output already
exists and is newer than the SVG. The default behaviour is to skip
unchanged files so re-running the script after editing one sprite is
fast.
"""

from __future__ import annotations

import argparse
import io
import sys
from dataclasses import dataclass
from pathlib import Path

# Frame count for animated GIFs. 12 frames over the cycle gives a
# perceptibly smooth pulse for typical 2–3s animations without
# blowing up file size; QQ caps animated images at ~5 MB and most
# of these sprites land well under 100 KB at this density.
_ANIMATION_FRAMES = 12

# Output GIF dimensions. The bundled SVGs declare 200–360 px viewport
# widths; rendering at 1× respects the artists' intent and keeps
# the GIF small. Cairosvg uses the SVG's intrinsic size when neither
# ``output_width`` nor ``output_height`` is set, so we let it default.

# Token used by lxml to recognise the SVG namespace. cairosvg uses the
# SVG 1.1 namespace; we don't strip it because cairosvg expects
# qualified element names when re-rendering the modified tree.
_SVG_NS = "http://www.w3.org/2000/svg"


@dataclass(frozen=True)
class _AnimSpec:
    """A single SMIL animation we know how to interpolate."""

    parent: object  # lxml element, kept loose to avoid lxml import at type-check time
    anim_kind: str  # "attr" or "transform"
    attribute_name: (
        str  # opacity / cy / y / ... (only for attr); "translate" / "rotate" for transform
    )
    values: list[str]  # raw value tokens (semicolon-separated)
    duration_s: float


def _parse_dur(raw: str) -> float:
    """Parse SMIL ``dur`` into seconds. Supports ``s`` and ``ms`` suffixes."""
    raw = raw.strip()
    if raw.endswith("ms"):
        return float(raw[:-2]) / 1000.0
    if raw.endswith("s"):
        return float(raw[:-1])
    return float(raw)


def _interpolate(values: list[str], t01: float) -> str:
    """Pick a value from ``values`` (a SMIL ``values=`` list) at ``t01 ∈ [0, 1).

    Linear interpolation between consecutive scalars or coordinate
    pairs. ``values`` is a list of tokens ``"a;b;c"`` already split.
    The returned string keeps the same shape as the input tokens —
    a scalar in, a scalar out; a ``"x,y"`` in, a ``"x,y"`` out.
    """
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    # Map t in [0,1) to a (segment_index, segment_t) pair.
    n_segments = len(values) - 1
    pos = t01 * n_segments
    idx = min(int(pos), n_segments - 1)
    frac = pos - idx
    a = values[idx].strip()
    b = values[idx + 1].strip()

    def _to_floats(token: str) -> list[float]:
        # Accepts "1.0", "0.2", "5,10", "10 20" — split on , or whitespace.
        token = token.replace(",", " ")
        return [float(x) for x in token.split() if x]

    fa = _to_floats(a)
    fb = _to_floats(b)
    if len(fa) != len(fb):
        # Mismatched shapes — return start as a defensive default.
        return a
    interp = [fa[i] + (fb[i] - fa[i]) * frac for i in range(len(fa))]
    if len(interp) == 1:
        return f"{interp[0]:.4f}"
    return ",".join(f"{v:.4f}" for v in interp)


def _strip_ns(tag: object) -> str:
    """Return the local part of an lxml element tag, robust to non-string tags.

    ``Element.tag`` is normally a ``str`` but lxml uses callable
    sentinels for ``Comment`` / ``ProcessingInstruction`` nodes, which
    raise ``TypeError: argument of type 'cython_function_or_method'
    is not iterable`` if you do ``"}" in tag``. We just bail to the
    empty string for those — they're never the SMIL elements we care
    about.
    """
    if not isinstance(tag, str):
        return ""
    return tag.split("}", 1)[1] if "}" in tag else tag


def _collect_animations(root: object) -> tuple[list[_AnimSpec], float]:
    """Walk the tree and pull out every ``<animate>`` / ``<animateTransform>``.

    Returns the spec list and the total cycle duration (LCM of the
    ``dur`` values, capped at the maximum so we don't generate a
    decade-long GIF if someone uses oddball durations).
    """
    from lxml import etree  # noqa: PLC0415

    specs: list[_AnimSpec] = []
    durations: list[float] = []

    # ``iter()`` walks descendants. We collect the parent up-front
    # because once we remove the animation node it'd be unrooted.
    for elem in list(root.iter()):
        tag = _strip_ns(elem.tag)
        if tag not in ("animate", "animateTransform"):
            continue
        parent = elem.getparent()
        if parent is None:
            continue
        dur = elem.get("dur") or "1s"
        try:
            dur_s = _parse_dur(dur)
        except ValueError:
            continue
        durations.append(dur_s)

        values_raw = elem.get("values") or ""
        if not values_raw:
            # Some SVGs use ``from``/``to`` instead of ``values``.
            from_v = elem.get("from")
            to_v = elem.get("to")
            if from_v and to_v:
                values_raw = f"{from_v};{to_v}"
        values = [v.strip() for v in values_raw.split(";") if v.strip()]
        if not values:
            continue

        if tag == "animate":
            attr = elem.get("attributeName") or ""
            specs.append(
                _AnimSpec(
                    parent=parent,
                    anim_kind="attr",
                    attribute_name=attr,
                    values=values,
                    duration_s=dur_s,
                )
            )
        else:  # animateTransform
            ttype = elem.get("type") or "translate"
            specs.append(
                _AnimSpec(
                    parent=parent,
                    anim_kind="transform",
                    attribute_name=ttype,
                    values=values,
                    duration_s=dur_s,
                )
            )

        # Remove the animation node — we'll bake values into the
        # parent for each frame.
        parent.remove(elem)
        _ = etree  # keep import alive for type checkers

    if not durations:
        return specs, 0.0
    # Cycle = max duration. Most sprites use a single dur; mixed
    # durations get slightly out-of-phase frames at the boundary,
    # which is unnoticeable for the tiny indefinite loops here.
    cycle = max(durations)
    return specs, cycle


def _apply_specs_at(specs: list[_AnimSpec], t01: float, master_cycle: float) -> None:
    """Set parent attributes to the interpolated values for time ``t01``.

    ``master_cycle`` is the longest individual ``dur`` in the SVG;
    each spec runs at its own period within that window so faster
    animations cycle multiple times per master cycle (matches SMIL
    ``repeatCount="indefinite"`` semantics).
    """
    # Group transform specs per parent so multiple animateTransforms
    # on the same element compose correctly (translate + rotate).
    transforms_by_parent: dict[int, list[str]] = {}
    for spec in specs:
        local = (t01 * master_cycle) / spec.duration_s
        local_t01 = local - int(local)  # wrap into [0, 1)
        v = _interpolate(spec.values, local_t01)
        if spec.anim_kind == "attr":
            spec.parent.set(spec.attribute_name, v)  # type: ignore[attr-defined]
        else:
            key = id(spec.parent)
            kind = spec.attribute_name
            transforms_by_parent.setdefault(key, []).append(f"{kind}({v})")
    for spec in specs:
        if spec.anim_kind != "transform":
            continue
        key = id(spec.parent)
        chunks = transforms_by_parent.pop(key, None)
        if chunks is None:
            continue
        spec.parent.set("transform", " ".join(chunks))  # type: ignore[attr-defined]


def rasterize_static(svg_path: Path, png_path: Path) -> None:
    """Render a static SVG to a PNG. Errors propagate to the caller."""
    import cairosvg  # noqa: PLC0415

    cairosvg.svg2png(
        url=str(svg_path),
        write_to=str(png_path),
    )


def rasterize_animated(svg_path: Path, gif_path: Path, frames: int) -> int:
    """Render an animated SVG to a GIF. Returns the number of frames written."""
    import cairosvg  # noqa: PLC0415
    from lxml import etree  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    parser = etree.XMLParser(remove_blank_text=False)
    tree = etree.parse(str(svg_path), parser)
    root = tree.getroot()

    specs, cycle = _collect_animations(root)
    if not specs or cycle <= 0:
        # No usable animations after the strip — fall back to a
        # single still frame so the caller still gets a GIF (so the
        # ``.gif`` sibling is consistent for the resolver).
        rasterize_static(svg_path, gif_path)
        return 1

    pil_frames: list[Image.Image] = []
    for i in range(frames):
        t01 = i / frames
        # Apply the spec mutations to the original tree, then
        # serialise + render. We re-serialise rather than passing
        # the lxml tree directly because cairosvg's API expects a
        # bytestring or path.
        _apply_specs_at(specs, t01, cycle)
        svg_bytes = etree.tostring(tree, xml_declaration=True, encoding="utf-8")
        png_bytes = cairosvg.svg2png(bytestring=svg_bytes)
        pil_frames.append(Image.open(io.BytesIO(png_bytes)).convert("RGBA"))

    if not pil_frames:
        return 0

    # GIF doesn't support full alpha; convert to palette while keeping
    # transparent regions transparent. ``disposal=2`` clears between
    # frames so the pulse looks clean rather than smearing.
    duration_ms = round(1000 * cycle / frames)
    pil_frames[0].save(
        gif_path,
        save_all=True,
        append_images=pil_frames[1:],
        loop=0,  # infinite loop
        duration=duration_ms,
        disposal=2,
        optimize=True,
    )
    return len(pil_frames)


def is_animated(svg_path: Path) -> bool:
    """Cheap textual check; SMIL primitives are unique strings."""
    text = svg_path.read_text(encoding="utf-8", errors="ignore")
    return any(
        marker in text for marker in ("<animate ", "<animateTransform ", "<animateMotion ", "<set ")
    )


def output_path_for(svg: Path, animated: bool) -> Path:
    return svg.with_suffix(".gif" if animated else ".png")


def needs_render(svg: Path, out: Path, force: bool) -> bool:
    if force:
        return True
    if not out.exists():
        return True
    return svg.stat().st_mtime_ns > out.stat().st_mtime_ns


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--asset-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "bot" / "assets" / "picture",
        help="Directory holding the sprite SVGs.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-render even when the output is newer than the SVG.",
    )
    parser.add_argument(
        "--frames",
        type=int,
        default=_ANIMATION_FRAMES,
        help=f"Frame count for animated GIFs (default {_ANIMATION_FRAMES}).",
    )
    args = parser.parse_args(argv)

    root: Path = args.asset_root
    if not root.is_dir():
        print(f"asset root not found: {root}", file=sys.stderr)
        return 2

    svgs = sorted(root.glob("*.svg"))
    if not svgs:
        print(f"no SVGs under {root}")
        return 0

    n_static = n_animated = n_skipped = n_failed = 0
    for svg in svgs:
        animated = is_animated(svg)
        out = output_path_for(svg, animated)
        if not needs_render(svg, out, args.force):
            n_skipped += 1
            continue
        try:
            if animated:
                count = rasterize_animated(svg, out, args.frames)
                print(f"animated {svg.name} -> {out.name} ({count} frames)")
                n_animated += 1
            else:
                rasterize_static(svg, out)
                print(f"static   {svg.name} -> {out.name}")
                n_static += 1
        except Exception as exc:
            print(f"FAILED   {svg.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            n_failed += 1

    print(
        f"\ndone: {n_static} static, {n_animated} animated, {n_skipped} skipped, {n_failed} failed"
    )
    return 0 if n_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
