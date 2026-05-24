"""Rewrite ``@pic:NAME.svg`` references in .ling rule files.

Looks at every ``@pic:<name>.svg`` token, checks for a sibling
rasterised file alongside the SVG on disk (``.gif`` for animated,
``.png`` for static, produced by ``scripts/rasterize_assets.py``),
and replaces the reference in-place. SVG references with no raster
sibling are left alone and reported.

The script is idempotent: running it twice is a no-op since
post-rewrite the ``@pic:`` references already point at PNG/GIF.

Usage::

    python scripts/_rewrite_pic_refs.py            # rewrite all .ling
    python scripts/_rewrite_pic_refs.py --dry-run  # report only
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ASSET_ROOT = REPO / "bot" / "assets" / "picture"
RULES_GLOB = "**/*.ling"

_PIC_REF = re.compile(r"@pic:([^\s±/]+?)\.svg(?=[\s±]|$)")


def pick_replacement(stem: str) -> str | None:
    """Return ``"<stem>.gif"`` or ``"<stem>.png"`` based on disk, or ``None``."""
    gif = ASSET_ROOT / f"{stem}.gif"
    png = ASSET_ROOT / f"{stem}.png"
    if gif.is_file():
        return f"{stem}.gif"
    if png.is_file():
        return f"{stem}.png"
    return None


def rewrite_text(text: str) -> tuple[str, dict[str, int], list[str]]:
    """Return (new_text, replacement_counts, missing_stems).

    ``replacement_counts`` keys on the replacement filename so callers
    can summarise (``漂流瓶.gif: 4``) rather than re-counting.
    ``missing_stems`` lists SVG references that had no raster sibling
    so the operator knows to either rasterize or pick a different ref.
    """
    counts: dict[str, int] = {}
    missing: list[str] = []

    def _sub(match: re.Match[str]) -> str:
        stem = match.group(1)
        replacement = pick_replacement(stem)
        if replacement is None:
            if stem not in missing:
                missing.append(stem)
            return match.group(0)
        token = f"@pic:{replacement}"
        counts[replacement] = counts.get(replacement, 0) + 1
        return token

    new_text = _PIC_REF.sub(_sub, text)
    return new_text, counts, missing


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dry-run", action="store_true", help="don't write, just report")
    p.add_argument(
        "--rules-root",
        type=Path,
        default=REPO / "bot",
        help="root to glob for .ling files (default: bot/)",
    )
    args = p.parse_args(argv)

    files = sorted(args.rules_root.glob(RULES_GLOB))
    if not files:
        print(f"no .ling files under {args.rules_root}", file=sys.stderr)
        return 1

    grand_counts: dict[str, int] = {}
    grand_missing: list[tuple[Path, str]] = []
    touched_files = 0

    for path in files:
        original = path.read_text(encoding="utf-8")
        new, counts, missing = rewrite_text(original)
        if counts:
            touched_files += 1
            for k, v in counts.items():
                grand_counts[k] = grand_counts.get(k, 0) + v
        for stem in missing:
            grand_missing.append((path, stem))

        if new != original and not args.dry_run:
            path.write_text(new, encoding="utf-8")

    print(f"files touched: {touched_files}")
    if grand_counts:
        print("replacements (replacement -> count):")
        for k in sorted(grand_counts):
            print(f"  {k}: {grand_counts[k]}")
    if grand_missing:
        print("\nWARNING: SVG references with no raster sibling on disk:")
        for path, stem in grand_missing:
            print(f"  {path.relative_to(REPO)}: @pic:{stem}.svg")
        print(
            "Run scripts/rasterize_assets.py first, or hand-edit the rule "
            "to point at an existing sprite."
        )

    return 0 if not grand_missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
