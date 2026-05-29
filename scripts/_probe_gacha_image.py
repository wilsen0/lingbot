"""Local smoke test for the gacha_image renderer.

Builds a 10-pull and a 50-pull sample with mixed rarities and writes
PNGs under ``data/cache/`` so we can eyeball the result. Records use
the literal ``\\n`` separators that production KV stores.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "packages" / "core" / "src"))
sys.path.insert(0, str(REPO / "packages" / "tools-stdlib" / "src"))

import linling_tools_stdlib  # noqa: F401 — register tools
from linling_core import SqliteKVStore
from linling_core.tools import ToolCtx, registry


def make_record_10_with_ur() -> str:
    return (
        r"扭哇扭哇～\n"
        r"🍬蛋壳+1\n"
        r"✨获得〔小豆芽〕！！\n"
        r"🍬蛋壳+1\n"
        r"🍬蛋壳+1\n"
        r"✨恭喜获得珍品〖思思〗！！\n"
        r"🍬蛋壳+1\n"
        r"✨获得〔大飞龙〕！！\n"
        r"🍬蛋壳+1\n"
        r"✨恭喜获得藏品〖郫忧〗！！\n"
        r"🍬蛋壳+1"
    )


def make_record_50_mostly_egg() -> str:
    """Realistic 50-pull: 47 蛋壳 + 1 大飞龙 + 1 思思 + 1 蛋壳 (worst case for grid coverage)."""
    parts = [r"扭哇扭哇～\n"]
    for _ in range(47):
        parts.append(r"🍬蛋壳+1\n")
    parts.append(r"✨获得〔大飞龙〕！！\n")
    parts.append(r"✨恭喜获得珍品〖思思〗！！\n")
    parts.append(r"🍬蛋壳+1")
    return "".join(parts)


def make_record_50_mixed() -> str:
    parts = [r"扭哇扭哇～\n"]
    pattern = [
        r"🍬蛋壳+1",
        r"🍬蛋壳+1",
        r"🍬蛋壳+1",
        r"✨获得〔小豆芽〕！！",
        r"🍬蛋壳+1",
        r"🍬蛋壳+1",
        r"🍬蛋壳+1",
        r"✨获得〔大飞龙〕！！",
        r"🍬蛋壳+1",
        r"🍬蛋壳+1",
    ]
    for i in range(50):
        parts.append(pattern[i % len(pattern)] + r"\n")
    parts.append(r"✨恭喜获得珍品〖呦呦〗！！")
    return "".join(parts)


async def main() -> None:
    cache_dir = REPO / "data" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    asset_root = REPO / "bot" / "assets"

    async with SqliteKVStore("probe", cache_dir / "_probe_kv.db") as kv:
        ctx = ToolCtx(
            kv=kv,
            event=None,
            bot_id="probe",
            extras={
                "image_text_cache_dir": cache_dir,
                "asset_root": asset_root,
            },
        )
        td = registry.get_by_dsl_name("扭蛋图") or registry.get("gacha_image")
        assert td is not None

        url = await td.fn(ctx, record=make_record_10_with_ur(), kind="十连", cost="488")
        print(f"10-pull (with UR): {url[:40]}… (len={len(url)})")

        url = await td.fn(ctx, record=make_record_50_mostly_egg(), kind="五十连", cost="2388")
        print(f"50-pull (47 eggs): {url[:40]}… (len={len(url)})")

        url = await td.fn(ctx, record=make_record_50_mixed(), kind="五十连", cost="2388")
        print(f"50-pull (mixed):   {url[:40]}… (len={len(url)})")

        # List the files we produced.
        for p in sorted((REPO / "data" / "cache").glob("gacha_*.png"))[-3:]:
            print(f"  → {p}")


if __name__ == "__main__":
    asyncio.run(main())
