"""End-to-end test using the *real* 单十次扭蛋 handler chain.

Unlike ``_e2e_gacha_test.py`` which seeds a hand-written %录% string,
this script drives the actual roll → record-write → settlement chain so
the record is built up the way production does it (with literal
``\\n`` separators in the KV value).
"""

from __future__ import annotations

import asyncio
import sys
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


def _event() -> Event:
    return Event(
        id="msg-1",
        platform="onebot",
        bot_id="rt",
        scope=Scope(kind="group", id="group-1", platform="onebot"),
        sender=User(id="user-42", platform="onebot", display_name="Tester"),
    )


async def main() -> None:
    rules_path = REPO / "bot" / "rules" / "main.ling"
    cache_dir = REPO / "data" / "cache" / "image_text"
    cache_dir.mkdir(parents=True, exist_ok=True)
    asset_root = REPO / "bot" / "assets"

    script = parse(rules_path.read_text(encoding="utf-8"), strict=False)
    handlers = {h.trigger: h for h in script.handlers}
    db = cache_dir / "_real_kv.db"
    if db.exists():
        db.unlink()

    async with SqliteKVStore("rt", db) as kv:
        await kv.write("啊/灵玉系", "灵玉", "user-42", "999999")
        # Pre-seed a near-pity counter so 单*次扭蛋 will roll a guaranteed
        # 珍品 in the run — that branch fires when 次数>=99.
        await kv.write("休闲系/珍品", "次数", "user-42", "99")
        await kv.write("休闲系/珍品", "机会", "user-42", "1")
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

        # Spin 10 single-pulls — this is what 扭蛋十次 invokes via $调用$
        # in production, building up %录% one literal-\\n-separated row
        # at a time.
        for _ in range(10):
            await vm.execute_handler(handlers["单十次扭蛋"], ev)

        record = await kv.read("休闲系/珍品", "扭蛋记录", "user-42", "")
        print(f"raw %录% (len={len(record)}):")
        print(f"  {record!r}")

        drops = _parse_record(record)
        print(f"\nparsed {len(drops)} drops:")
        for i, d in enumerate(drops):
            print(f"  [{i}] {d.rarity.name}  {d.name}")

        if len(drops) < 9:  # at least 9, the leading \"0\" line is
                            # ignored as not a drop
            raise SystemExit(
                f"FAIL: expected ~10 drops but only parsed {len(drops)}"
            )

        # Now trigger the settlement.
        result = await vm.execute_handler(handlers["十扭蛋记录"], ev)
        for seg in result.segments:
            url = getattr(seg, "url", "")
            if url.startswith("base64://"):
                import base64 as _b64
                from PIL import Image
                import io

                png = _b64.b64decode(url[len("base64://") :])
                with Image.open(io.BytesIO(png)) as im:
                    print(f"\nsettlement image: {im.size[0]}×{im.size[1]} ({len(png)} bytes)")
                # Persist for human inspection (uses our debug-cache writer).
                debug_dir = REPO / "data" / "cache" / "image_text"
                pngs = sorted(debug_dir.glob("gacha_*.png"))
                if pngs:
                    print(f"  → {pngs[-1]}")
                break

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
