"""Tests for the 鱼塘 (fishing) tools: draw logic + image renderers.

Every test goes through the global tool registry so we also confirm the
DSL names (``钓鱼抽签`` / ``附魔抽取`` / ``钓鱼结算图`` / ``鱼篓图`` /
``鱼图鉴图``) are wired up.
"""

from __future__ import annotations

import base64
import io
import json
import random
from collections import Counter
from pathlib import Path
from typing import Any

import linling_tools_stdlib  # noqa: F401 — registers core + stdlib tools
import pytest
from linling_core import SqliteKVStore
from linling_core.tools import ToolCtx, registry
from PIL import Image


@pytest.fixture
async def ctx(tmp_path: Path) -> Any:
    async with SqliteKVStore("test-bot", tmp_path / "kv.db") as kv:
        # Seed a deterministic RNG so weighted draws are reproducible.
        yield ToolCtx(
            kv=kv,
            event=None,
            bot_id="test-bot",
            extras={"random": random.Random(1234), "image_text_cache_dir": tmp_path},
        )


def _png_from_b64url(result: str) -> Image.Image:
    assert result.startswith("base64://")
    raw = base64.b64decode(result[len("base64://") :])
    return Image.open(io.BytesIO(raw))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    @pytest.mark.parametrize(
        ("py_name", "dsl_name"),
        [
            ("fishing_draw", "钓鱼抽签"),
            ("fishing_enchant_roll", "附魔抽取"),
            ("fishing_sell_bucket", "钓鱼卖鱼"),
            ("fishing_bucket_sync", "钓鱼背包同步"),
            ("fishing_settlement_image", "钓鱼结算图"),
            ("fishing_bucket_image", "鱼篓图"),
            ("fishing_dex_image", "鱼图鉴图"),
        ],
    )
    async def test_tool_registered(self, py_name: str, dsl_name: str) -> None:
        td = registry.get(py_name)
        assert td is not None
        assert td.dsl_name == dsl_name


# ---------------------------------------------------------------------------
# Draw logic
# ---------------------------------------------------------------------------


class TestFishingDraw:
    async def test_gone_when_elapsed_exceeds_ceiling(self, ctx: ToolCtx) -> None:
        td = registry.get("fishing_draw")
        assert td is not None
        out = json.loads(await td.fn(ctx, qq="u1", elapsed="1000", buff=""))
        assert out["result"] == "gone"
        # No KV mutation on a gone result.
        assert await ctx.kv.read("休闲系/钓鱼", "水桶", "u1", "{}") == "{}"

    async def test_negative_elapsed_treated_as_early(self, ctx: ToolCtx) -> None:
        """Clock skew (stale cast timestamp) must not crash — treated as 早."""
        td = registry.get("fishing_draw")
        assert td is not None
        out = json.loads(await td.fn(ctx, qq="u1", elapsed="-30", buff=""))
        assert out["result"] in {"empty", "junk", "catch"}

    async def test_clean_buff_suppresses_junk(self, ctx: ToolCtx) -> None:
        td = registry.get("fishing_draw")
        assert td is not None
        results = Counter()
        for _ in range(300):
            out = json.loads(await td.fn(ctx, qq="", elapsed="60", buff="驱垃圾"))
            results[out["result"]] += 1
        assert results["junk"] == 0

    async def test_golden_window_beats_early_on_catch_rate(self, ctx: ToolCtx) -> None:
        td = registry.get("fishing_draw")
        assert td is not None
        early = 0
        for _ in range(300):
            out = json.loads(await td.fn(ctx, qq="", elapsed="10", buff=""))
            if out["result"] == "catch":
                early += 1
        golden = 0
        for _ in range(300):
            out = json.loads(await td.fn(ctx, qq="", elapsed="150", buff=""))
            if out["result"] == "catch":
                golden += 1
        assert golden > early

    async def test_catch_updates_bucket_dex_and_value(self, ctx: ToolCtx) -> None:
        td = registry.get("fishing_draw")
        assert td is not None
        # Force a catch by hammering the golden window many times.
        for _ in range(40):
            await td.fn(ctx, qq="u1", elapsed="150", buff="")
        bucket = json.loads(await ctx.kv.read("休闲系/钓鱼", "水桶", "u1", "{}"))
        dex = json.loads(await ctx.kv.read("休闲系/钓鱼", "图鉴", "u1", "{}"))
        value = int(await ctx.kv.read("休闲系/钓鱼", "水桶价值", "u1", "0"))
        assert sum(bucket.values()) > 0
        assert value > 0
        # Every bucketed (sellable) species is in the dex too.
        for name in bucket:
            assert name in dex

    async def test_first_capture_flag_then_zero(self, ctx: ToolCtx) -> None:
        """``first`` is 1 on debut, 0 afterwards for the same species."""
        from linling_tools_stdlib.fishing_image import species_by_name

        td = registry.get("fishing_draw")
        assert td is not None
        # Pre-seed dex so we control the debut precisely.
        await ctx.kv.write("休闲系/钓鱼", "图鉴", "u2", "{}")
        firsts = []
        names = []
        for _ in range(60):
            out = json.loads(await td.fn(ctx, qq="u2", elapsed="150", buff=""))
            if out["result"] == "catch":
                firsts.append(out["first"])
                names.append(out["name"])
        # The first time each name appears, first==1; repeats are 0.
        seen: set[str] = set()
        for name, first in zip(names, firsts, strict=True):
            assert species_by_name(name) is not None
            if name in seen:
                assert first == 0
            else:
                assert first == 1
                seen.add(name)

    async def test_plump_buff_boosts_value(self, ctx: ToolCtx) -> None:
        """肥美 buff makes a caught fish worth +50% (rounded)."""
        from linling_tools_stdlib.fishing_image import species_by_name

        td = registry.get("fishing_draw")
        assert td is not None
        for _ in range(40):
            out = json.loads(await td.fn(ctx, qq="", elapsed="150", buff="肥美"))
            if out["result"] == "catch":
                base = species_by_name(out["name"])
                assert base is not None
                assert out["value"] == round(base.value * 1.5)
                return
        pytest.skip("no catch in sample; rerun")


class TestLegacyMigration:
    async def test_sell_bucket_folds_legacy_emoji_run(self, ctx: ToolCtx) -> None:
        """卖鱼 must count old emoji-run data and clear everything."""
        # Old player: bucket stored as a concatenated emoji string.
        await ctx.kv.write("休闲系/钓鱼", "水桶里有", "old1", "🦀🐟🐟🦞")
        td = registry.get("fishing_sell_bucket")
        assert td is not None
        out = json.loads(await td.fn(ctx, qq="old1"))
        # 螃蟹18 + 草鱼59*2 + 龙虾78 = 214, count 4.
        assert out["count"] == 4
        assert out["value"] == 18 + 59 * 2 + 78
        # Everything cleared.
        assert await ctx.kv.read("休闲系/钓鱼", "水桶", "old1", "{}") == "{}"
        assert await ctx.kv.read("休闲系/钓鱼", "水桶里有", "old1", "") == ""
        assert await ctx.kv.read("休闲系/钓鱼", "水桶价值", "old1", "0") == "0"

    async def test_bucket_sync_merges_legacy_once(self, ctx: ToolCtx) -> None:
        await ctx.kv.write("休闲系/钓鱼", "水桶里有", "old2", "🐟🐟🦀")
        # Also a new-format catch already present.
        await ctx.kv.write("休闲系/钓鱼", "水桶", "old2", json.dumps({"草鱼": 1}))
        td = registry.get("fishing_bucket_sync")
        assert td is not None
        merged = json.loads(await td.fn(ctx, qq="old2"))
        assert merged["草鱼"] == 3  # 1 new + 2 legacy
        assert merged["螃蟹"] == 1
        # Legacy field consumed → a second sync is a no-op.
        assert await ctx.kv.read("休闲系/钓鱼", "水桶里有", "old2", "") == ""
        again = json.loads(await td.fn(ctx, qq="old2"))
        assert again == merged

    async def test_sell_empty_bucket(self, ctx: ToolCtx) -> None:
        td = registry.get("fishing_sell_bucket")
        assert td is not None
        out = json.loads(await td.fn(ctx, qq="nobody"))
        assert out == {"count": 0, "value": 0}


class TestEnchantRoll:
    async def test_returns_known_buff(self, ctx: ToolCtx) -> None:
        from linling_tools_stdlib.fishing_game import KNOWN_BUFFS

        td = registry.get("fishing_enchant_roll")
        assert td is not None
        out = json.loads(await td.fn(ctx, charges="3"))
        assert out["buff"] in KNOWN_BUFFS
        assert out["charges"] == 3
        assert out["desc"]

    async def test_blank_charges_defaults_to_three(self, ctx: ToolCtx) -> None:
        td = registry.get("fishing_enchant_roll")
        assert td is not None
        out = json.loads(await td.fn(ctx, charges=""))
        assert out["charges"] == 3


# ---------------------------------------------------------------------------
# Image renderers
# ---------------------------------------------------------------------------


class TestFishingImages:
    async def test_settlement_legendary_renders_png(self, ctx: ToolCtx) -> None:
        td = registry.get("fishing_settlement_image")
        assert td is not None
        result = await td.fn(ctx, name="锦鲤龙", value="1888", buff="幸运")
        with _png_from_b64url(result) as img:
            assert img.format == "PNG"
            assert img.size[0] >= 600 and img.size[1] >= 800

    async def test_settlement_unknown_name_falls_back(self, ctx: ToolCtx) -> None:
        """An off-catalogue name still renders (common-tier fallback)."""
        td = registry.get("fishing_settlement_image")
        assert td is not None
        result = await td.fn(ctx, name="史前巨兽", value="999", buff="")
        with _png_from_b64url(result) as img:
            assert img.format == "PNG"

    async def test_bucket_grid_renders_png(self, ctx: ToolCtx) -> None:
        td = registry.get("fishing_bucket_image")
        assert td is not None
        bucket = json.dumps({"草鱼": 3, "螃蟹": 2, "河豚": 1, "锦鲤龙": 1})
        result = await td.fn(ctx, bucket=bucket, title="我的鱼篓")
        with _png_from_b64url(result) as img:
            assert img.format == "PNG"

    async def test_empty_bucket_still_renders(self, ctx: ToolCtx) -> None:
        td = registry.get("fishing_bucket_image")
        assert td is not None
        result = await td.fn(ctx, bucket="{}", title="我的鱼篓")
        with _png_from_b64url(result) as img:
            assert img.format == "PNG"

    async def test_malformed_bucket_does_not_crash(self, ctx: ToolCtx) -> None:
        td = registry.get("fishing_bucket_image")
        assert td is not None
        result = await td.fn(ctx, bucket="not-json", title="")
        with _png_from_b64url(result) as img:
            assert img.format == "PNG"

    async def test_dex_renders_with_silhouettes(self, ctx: ToolCtx) -> None:
        td = registry.get("fishing_dex_image")
        assert td is not None
        # Only a couple captured → most tiles are dimmed silhouettes.
        result = await td.fn(ctx, dex=json.dumps({"草鱼": 12, "河豚": 2}), title="钓鱼图鉴")
        with _png_from_b64url(result) as img:
            assert img.format == "PNG"
            # Dex shows the full catalogue → tall image.
            assert img.size[1] >= 600
