"""End-to-end test using the *real* 单十次/单五十次扭蛋 handler chains.

Unlike ``_e2e_gacha_test.py`` which seeds a hand-written %录% string,
this script drives the actual roll → record-write → settlement chain so
the record is built up the way production does it — including the
literal ``\\n`` separators and the per-rule egg formatting (which the
two handlers historically wrote differently).

The key invariant: every single spin must show up as one card in the
settlement image, so ``parsed drops == number of spins``.
"""

from __future__ import annotations

import asyncio
import base64
import io
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "packages" / "core" / "src"))
sys.path.insert(0, str(REPO / "packages" / "dsl" / "src"))
sys.path.insert(0, str(REPO / "packages" / "tools-stdlib" / "src"))

import linling_tools_stdlib  # noqa: F401 — register tools
from linling_core import SqliteKVStore
from linling_core.events import Event, Scope, User
from linling_core.tools import registry
from linling_dsl.parser import parse
from linling_dsl.vm import VM
from linling_tools_stdlib.gacha_image import _parse_record
from PIL import Image


def _event() -> Event:
    return Event(
        id="msg-1",
        platform="onebot",
        bot_id="rt",
        scope=Scope(kind="group", id="group-1", platform="onebot"),
        sender=User(id="user-42", platform="onebot", display_name="Tester"),
    )


async def _run_case(
    spin_handler: str,
    settle_handler: str,
    spins: int,
    label: str,
) -> None:
    rules_path = REPO / "bot" / "rules" / "main.ling"
    cache_dir = REPO / "data" / "cache" / "image_text"
    cache_dir.mkdir(parents=True, exist_ok=True)
    asset_root = REPO / "bot" / "assets"

    script = parse(rules_path.read_text(encoding="utf-8"), strict=False)
    handlers = {h.trigger: h for h in script.handlers}
    db = cache_dir / f"_real_{label}.db"
    if db.exists():
        db.unlink()

    print(f"\n=== {label}: {spins}× {spin_handler} ===")

    async with SqliteKVStore("rt", db) as kv:
        await kv.write("啊/灵玉系", "灵玉", "user-42", "9999999")
        ev = _event()
        vm = VM(
            tool_registry=registry,
            kv=kv,
            bot_id="rt",
            extras={
                "image_text_cache_dir": cache_dir,
                "asset_root": asset_root,
                "handler_lookup": handlers.get,
            },
        )

        for _ in range(spins):
            await vm.execute_handler(handlers[spin_handler], ev)

        record = await kv.read("休闲系/珍品", "扭蛋记录", "user-42", "")
        drops = _parse_record(record)
        tally = Counter(d.rarity.name for d in drops)
        print(f"  parsed {len(drops)} drops: {dict(tally)}")

        # Hard invariant: one card per spin. The leading "0" sentinel
        # is not a drop, so the count must equal the spin count exactly.
        if len(drops) != spins:
            print(f"  raw %录% = {record!r}")
            raise SystemExit(f"FAIL [{label}]: expected {spins} drops, parsed {len(drops)}")

        result = await vm.execute_handler(handlers[settle_handler], ev)
        img_seg = next(
            (s for s in result.segments if getattr(s, "url", "").startswith("base64://")),
            None,
        )
        if img_seg is None:
            raise SystemExit(f"FAIL [{label}]: no image segment produced")

        png = base64.b64decode(img_seg.url[len("base64://") :])
        with Image.open(io.BytesIO(png)) as im:
            w, h = im.size
            print(f"  image: {w}×{h} ({len(png)} bytes)")
            _assert_all_cards_visible(im, len(drops), label)

        pngs = sorted(cache_dir.glob("gacha_*.png"))
        if pngs:
            print(f"  → {pngs[-1]}")


def _assert_all_cards_visible(img: Image.Image, n_drops: int, label: str) -> None:
    """Sanity-check that the grid actually has n_drops cells with content.

    We don't OCR; we just confirm the grid region isn't mostly empty
    background by counting cells whose centre differs from the page
    background (near-black). A blank cell (the old 🥚-tofu bug) reads
    as background.
    """
    rgb = img.convert("RGB")
    w, h = rgb.size
    cols = 5 if n_drops <= 12 else 10
    cell_w, cell_h = (180, 220) if n_drops <= 12 else (110, 134)
    gap = 12
    margin_x = (w - (cols * cell_w + (cols - 1) * gap)) // 2

    # Grid top: header(160) + hero(420 if a hero card is shown else 40) + 30.
    # We don't know hero presence here, so try both and pick the one
    # that lands the most non-background cells.
    best = 0
    for grid_top in (610, 230):
        filled = 0
        rows = (n_drops + cols - 1) // cols
        for i in range(n_drops):
            row, col = divmod(i, cols)
            cx = margin_x + col * (cell_w + gap) + cell_w // 2
            cy = grid_top + row * (cell_h + gap) + cell_h // 2
            if cy + 10 >= h:
                continue
            # Sample a few px; "filled" if any differs notably from the
            # (8,8,18) page background.
            hit = False
            for dy in (-20, 0, 20):
                for dx in (-20, 0, 20):
                    px = rgb.getpixel((cx + dx, cy + dy))
                    if abs(px[0] - 8) + abs(px[1] - 8) + abs(px[2] - 18) > 60:
                        hit = True
                        break
                if hit:
                    break
            if hit:
                filled += 1
        best = max(best, filled)

    print(f"  grid: {best}/{n_drops} cells render content")
    # Allow a tiny slack for cards whose sprite happens to sit just
    # outside our coarse sample points, but the old bug dropped ~90%.
    if best < n_drops - 1:
        raise SystemExit(
            f"FAIL [{label}]: only {best}/{n_drops} cells have content "
            f"(empty cells = lost drops)"
        )


async def main() -> None:
    await _run_case("单十次扭蛋", "十扭蛋记录", 10, "10-pull")
    await _run_case("单五十次扭蛋", "五十扭蛋记录", 50, "50-pull")
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
