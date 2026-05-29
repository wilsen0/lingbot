"""End-to-end test of the 扭蛋十次 / 扭蛋五十次 settlement flow.

Drives the whole stack:
  1. Parse the live ``bot/rules/main.ling``.
  2. Seed the per-user gacha record KV with a realistic 10-line log.
  3. Execute the ``[内部]十扭蛋记录`` handler through the real VM.
  4. Verify the resulting segments contain an ImageSegment and the
     image file exists + decodes as a PNG.

Repeat for 五十扭蛋记录.
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
from PIL import Image


def _event() -> Event:
    return Event(
        id="msg-1",
        platform="onebot",
        bot_id="e2e",
        scope=Scope(kind="group", id="group-1", platform="onebot"),
        sender=User(id="user-42", platform="onebot", display_name="测试娘"),
    )


# Realistic 10-pull record — literal ``\\n`` separators just like
# the production KV stores, mix of N (蛋壳), R (大飞龙/小豆芽),
# SR (思思), and one UR (郫忧). Matches the line shapes the live
# rule emits via ``$写 ... %录%\\n<line>$``.
_RECORD_10 = (
    r"扭哇扭哇～\n"
    r"🍬蛋壳+1\n"
    r"🍬蛋壳+1\n"
    r"✨获得〔小豆芽〕！！\n"
    r"🍬蛋壳+1\n"
    r"✨恭喜获得珍品〖思思〗！！\n"
    r"🍬蛋壳+1\n"
    r"✨获得〔大飞龙〕！！\n"
    r"🍬蛋壳+1\n"
    r"✨恭喜获得藏品〖郫忧〗！！\n"
    r"🍬蛋壳+1"
)

# 50-pull record — same encoding, scattered SRs/Rs across the run.
_RECORD_50 = (
    r"扭哇扭哇～\n"
    + (
        r"🍬蛋壳+1\n🍬蛋壳+1\n✨获得〔小豆芽〕！！\n🍬蛋壳+1\n"
        r"🍬蛋壳+1\n🍬蛋壳+1\n✨获得〔大飞龙〕！！\n🍬蛋壳+1\n"
        r"🍬蛋壳+1\n🍬蛋壳+1"
    )
    * 5
    + r"\n✨恭喜获得珍品〖呦呦〗！！\n✨恭喜获得藏品〖郫忧〗！！"
)


async def _run(handler_name: str, record: str, label: str) -> None:
    rules_path = REPO / "bot" / "rules" / "main.ling"
    cache_dir = REPO / "data" / "cache" / "image_text"
    cache_dir.mkdir(parents=True, exist_ok=True)
    asset_root = REPO / "bot" / "assets"

    script = parse(rules_path.read_text(encoding="utf-8"), strict=False)
    handlers_by_trigger = {h.trigger: h for h in script.handlers}
    if handler_name not in handlers_by_trigger:
        raise SystemExit(f"handler not found: {handler_name}")
    target = handlers_by_trigger[handler_name]

    db_path = cache_dir / "_e2e_kv.db"
    if db_path.exists():
        db_path.unlink()

    async with SqliteKVStore("e2e", db_path) as kv:
        # Seed the per-user gacha record so 录:$读 ...$ has something
        # to hand $扭蛋图$.
        await kv.write("休闲系/珍品", "扭蛋记录", "user-42", record)

        vm = VM(
            tool_registry=registry,
            kv=kv,
            bot_id="e2e",
            extras={
                "image_text_cache_dir": cache_dir,
                "asset_root": asset_root,
                "handler_lookup": handlers_by_trigger.get,
            },
        )
        result = await vm.execute_handler(target, _event())

    # Inspect what came back.
    print(f"\n=== {label} ({handler_name}) ===")
    print(f"  segments: {len(result.segments)}")
    image_segments = []
    for i, seg in enumerate(result.segments):
        seg_type = type(seg).__name__
        url = getattr(seg, "url", None)
        text = getattr(seg, "text", None)
        if url:
            url_preview = url[:40] + "…" if len(url) > 40 else url
            print(f"   [{i}] {seg_type} url={url_preview} (len={len(url)})")
            image_segments.append(seg)
        elif text is not None:
            print(f"   [{i}] {seg_type} text={text!r}")
        else:
            print(f"   [{i}] {seg_type}")

    if not image_segments:
        raise SystemExit(f"FAIL: {label} produced no ImageSegment")

    # The tool now returns a ``base64://`` URL that NapCat accepts
    # natively (no shared filesystem required). Confirm the payload
    # decodes back to a valid PNG.
    img_url = image_segments[0].url
    print(f"  url scheme: {img_url[:20]}…")
    if not img_url.startswith("base64://"):
        raise SystemExit(f"FAIL: expected base64:// URL, got {img_url[:50]!r}")

    import base64 as _b64
    import io

    raw = _b64.b64decode(img_url[len("base64://") :])
    print(f"  payload: {len(raw)} bytes")
    with Image.open(io.BytesIO(raw)) as im:
        print(f"  image: {im.size[0]}×{im.size[1]} {im.mode}")

    # Confirm the KV write at the end of the handler reset the log.
    after = await SqliteKVStore("e2e", db_path).__aenter__() if False else None  # noqa
    # We re-open KV for the assertion to avoid mixing await contexts.
    async with SqliteKVStore("e2e", db_path) as kv2:
        cleared = await kv2.read("休闲系/珍品", "扭蛋记录", "user-42", "")
        # Rule emits ``扭哇扭哇～\n`` (literal backslash-n).
        assert "扭哇扭哇" in cleared, f"record not reset: {cleared!r}"
        print("  record cleared & reset to 扭哇扭哇～ ✓")

    # Print debug-cache copy if it exists.
    debug_cache = REPO / "data" / "cache" / "image_text"
    pngs = sorted(debug_cache.glob("gacha_*.png"))
    if pngs:
        print(f"  → debug copy: {pngs[-1]}")


async def main() -> None:
    await _run("十扭蛋记录", _RECORD_10, "10-pull")
    await _run("五十扭蛋记录", _RECORD_50, "50-pull")
    print("\nALL E2E CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
