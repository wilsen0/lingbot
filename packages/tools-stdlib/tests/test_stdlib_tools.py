"""Tests for the linling_tools_stdlib standard tool library.

Every test goes through the global tool registry so we also verify each
tool is registered with both the correct Python name and DSL name.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import linling_tools_stdlib  # noqa: F401 — imports core + stdlib in the right order
import pytest
from linling_core import SqliteKVStore
from linling_core.tools import ToolCtx, registry
from linling_tools_stdlib.globals_ops import _reset_globals_for_tests

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def ctx(tmp_path: Path) -> Any:
    async with SqliteKVStore("test-bot", tmp_path / "kv.db") as kv:
        yield ToolCtx(kv=kv, event=None, bot_id="test-bot")


@pytest.fixture(autouse=True)
def _clean_globals() -> None:
    _reset_globals_for_tests()


# ---------------------------------------------------------------------------
# JSON operations (15 tests)
# ---------------------------------------------------------------------------


class TestJsonOp:
    """Tests for the QRDic-style ``$JSON 子命令 ...$`` dispatcher."""

    async def test_registered_with_dsl_name(self) -> None:
        td = registry.get("json_op")
        assert td is not None
        assert td.dsl_name == "JSON"
        assert td.safe is True

    # --- 长度 -----------------------------------------------------------

    async def test_length_array(self, ctx: ToolCtx) -> None:
        td = registry.get("json_op")
        assert td is not None
        assert await td.fn(ctx, subcommand="长度", text="[1, 2, 3]") == "3"

    async def test_length_object(self, ctx: ToolCtx) -> None:
        td = registry.get("json_op")
        assert td is not None
        assert await td.fn(ctx, subcommand="长度", text='{"a": 1, "b": 2}') == "2"

    async def test_length_string(self, ctx: ToolCtx) -> None:
        td = registry.get("json_op")
        assert td is not None
        assert await td.fn(ctx, subcommand="长度", text='"hello"') == "5"

    async def test_length_scalar_returns_zero(self, ctx: ToolCtx) -> None:
        td = registry.get("json_op")
        assert td is not None
        assert await td.fn(ctx, subcommand="长度", text="42") == "0"

    async def test_length_invalid_json_falls_back_to_string(self, ctx: ToolCtx) -> None:
        td = registry.get("json_op")
        assert td is not None
        assert await td.fn(ctx, subcommand="长度", text="not json") == str(len("not json"))

    # --- 获取 -----------------------------------------------------------

    async def test_get_array_index(self, ctx: ToolCtx) -> None:
        td = registry.get("json_op")
        assert td is not None
        assert await td.fn(ctx, subcommand="获取", text='["a", "b", "c"]', arg="0") == "a"
        assert await td.fn(ctx, subcommand="获取", text='["a", "b", "c"]', arg="2") == "c"

    async def test_get_object_field(self, ctx: ToolCtx) -> None:
        td = registry.get("json_op")
        assert td is not None
        assert await td.fn(ctx, subcommand="获取", text='{"data": "hello"}', arg="data") == "hello"

    async def test_get_nested_path(self, ctx: ToolCtx) -> None:
        td = registry.get("json_op")
        assert td is not None
        text = '[{"name": "alice"}, {"name": "bob"}]'
        assert await td.fn(ctx, subcommand="获取", text=text, arg="0.name") == "alice"
        assert await td.fn(ctx, subcommand="获取", text=text, arg="1.name") == "bob"

    async def test_get_missing_returns_empty(self, ctx: ToolCtx) -> None:
        td = registry.get("json_op")
        assert td is not None
        assert await td.fn(ctx, subcommand="获取", text='{"a": 1}', arg="missing") == ""
        assert await td.fn(ctx, subcommand="获取", text="[1, 2]", arg="99") == ""

    async def test_get_nested_object_roundtrips(self, ctx: ToolCtx) -> None:
        td = registry.get("json_op")
        assert td is not None
        result = await td.fn(ctx, subcommand="获取", text='{"obj": {"x": 1}}', arg="obj")
        assert json.loads(result) == {"x": 1}

    # --- 添加 -----------------------------------------------------------

    async def test_add_to_array(self, ctx: ToolCtx) -> None:
        td = registry.get("json_op")
        assert td is not None
        result = await td.fn(ctx, subcommand="添加", text="[1, 2]", arg="3")
        assert json.loads(result) == [1, 2, 3]

    async def test_add_string_value(self, ctx: ToolCtx) -> None:
        td = registry.get("json_op")
        assert td is not None
        result = await td.fn(ctx, subcommand="添加", text='["a"]', arg="b")
        assert json.loads(result) == ["a", "b"]

    async def test_add_to_invalid_creates_new(self, ctx: ToolCtx) -> None:
        td = registry.get("json_op")
        assert td is not None
        result = await td.fn(ctx, subcommand="添加", text="not an array", arg="x")
        assert json.loads(result) == ["x"]

    async def test_add_object(self, ctx: ToolCtx) -> None:
        td = registry.get("json_op")
        assert td is not None
        result = await td.fn(ctx, subcommand="添加", text="[]", arg='{"k": 1}')
        assert json.loads(result) == [{"k": 1}]

    # --- 删除 -----------------------------------------------------------

    async def test_delete_at_index(self, ctx: ToolCtx) -> None:
        td = registry.get("json_op")
        assert td is not None
        result = await td.fn(ctx, subcommand="删除", text="[1, 2, 3]", arg="1")
        assert json.loads(result) == [1, 3]

    async def test_delete_out_of_range_is_noop(self, ctx: ToolCtx) -> None:
        td = registry.get("json_op")
        assert td is not None
        result = await td.fn(ctx, subcommand="删除", text="[1, 2, 3]", arg="99")
        assert json.loads(result) == [1, 2, 3]

    async def test_delete_with_non_int_arg_is_noop(self, ctx: ToolCtx) -> None:
        td = registry.get("json_op")
        assert td is not None
        result = await td.fn(ctx, subcommand="删除", text="[1, 2]", arg="abc")
        assert json.loads(result) == [1, 2]

    # --- unknown subcommand --------------------------------------------

    async def test_unknown_subcommand_returns_empty(self, ctx: ToolCtx) -> None:
        td = registry.get("json_op")
        assert td is not None
        assert await td.fn(ctx, subcommand="???", text="[1]", arg="0") == ""


# ---------------------------------------------------------------------------
# Codec operations (10 tests)
# ---------------------------------------------------------------------------


class TestCodec:
    async def test_url_encode_decode_roundtrip(self, ctx: ToolCtx) -> None:
        enc = registry.get("url_encode")
        dec = registry.get("url_decode")
        assert enc is not None and dec is not None
        text = "hello world & co/ä"
        encoded = await enc.fn(ctx, text=text)
        assert encoded == "hello%20world%20%26%20co%2F%C3%A4"
        assert await dec.fn(ctx, text=encoded) == text

    async def test_url_encode_registers_dsl_name(self) -> None:
        enc = registry.get("url_encode")
        dec = registry.get("url_decode")
        assert enc is not None
        assert dec is not None
        assert enc.dsl_name == "URLEncoder"
        assert dec.dsl_name == "URLDecoder"

    async def test_base64_encode_decode_roundtrip(self, ctx: ToolCtx) -> None:
        enc = registry.get("base64_encode")
        dec = registry.get("base64_decode")
        assert enc is not None and dec is not None
        text = "linling 岭灵"
        encoded = await enc.fn(ctx, text=text)
        assert await dec.fn(ctx, text=encoded) == text

    async def test_base64_decode_invalid_returns_empty(self, ctx: ToolCtx) -> None:
        dec = registry.get("base64_decode")
        assert dec is not None
        assert await dec.fn(ctx, text="!!!not-base64!!!") == ""

    async def test_hex_encode_ascii(self, ctx: ToolCtx) -> None:
        enc = registry.get("hex_encode")
        assert enc is not None
        assert await enc.fn(ctx, text="hi") == "6869"

    async def test_hex_encode_decode_utf8(self, ctx: ToolCtx) -> None:
        enc = registry.get("hex_encode")
        dec = registry.get("hex_decode")
        assert enc is not None and dec is not None
        text = "你好"
        encoded = await enc.fn(ctx, text=text)
        assert await dec.fn(ctx, text=encoded) == text

    async def test_hex_decode_invalid_returns_empty(self, ctx: ToolCtx) -> None:
        dec = registry.get("hex_decode")
        assert dec is not None
        assert await dec.fn(ctx, text="zzz") == ""

    async def test_hex_decode_strips_whitespace(self, ctx: ToolCtx) -> None:
        dec = registry.get("hex_decode")
        assert dec is not None
        assert await dec.fn(ctx, text="68 69") == "hi"

    async def test_unicode_decode_basic(self, ctx: ToolCtx) -> None:
        dec = registry.get("unicode_decode")
        assert dec is not None
        # U+4F60 U+597D = 你好
        assert await dec.fn(ctx, text="\\u4f60\\u597d") == "你好"
        assert await dec.fn(ctx, text="ascii only") == "ascii only"

    async def test_unicode_decode_registers_dsl_name(self) -> None:
        dec = registry.get("unicode_decode")
        assert dec is not None
        assert dec.dsl_name == "UnicodeDecoder"


# ---------------------------------------------------------------------------
# String ops (6 tests)
# ---------------------------------------------------------------------------


class TestStrOps:
    async def test_replace_sep_qrdic_style(self, ctx: ToolCtx) -> None:
        td = registry.get("replace_sep")
        assert td is not None
        # $替换 @ text @from@to$ → replace 'from' with 'to'.
        result = await td.fn(ctx, sep="@", text="hello foo world", pattern="@foo@bar")
        assert result == "hello bar world"

    async def test_replace_sep_multiple_occurrences(self, ctx: ToolCtx) -> None:
        td = registry.get("replace_sep")
        assert td is not None
        result = await td.fn(ctx, sep="@", text="aaa", pattern="@a@b")
        assert result == "bbb"

    async def test_replace_sep_empty_from_is_noop(self, ctx: ToolCtx) -> None:
        td = registry.get("replace_sep")
        assert td is not None
        # Pattern with no from-part (e.g. '@@x') → return text unchanged.
        result = await td.fn(ctx, sep="@", text="hello", pattern="@@x")
        assert result == "hello"

    async def test_replace_sep_overrides_core_dsl_name(self) -> None:
        td = registry.get_by_dsl_name("替换")
        assert td is not None
        # The stdlib tool wins — it's imported after core.
        assert td.name == "replace_sep"

    async def test_regex_match_hit(self, ctx: ToolCtx) -> None:
        td = registry.get("regex_match")
        assert td is not None
        assert await td.fn(ctx, sep="", text="abc123", pattern=r"\d+") == "1"
        assert await td.fn(ctx, sep="", text="abc", pattern=r"\d+") == "0"

    async def test_regex_match_invalid_pattern_returns_zero(self, ctx: ToolCtx) -> None:
        td = registry.get("regex_match")
        assert td is not None
        assert await td.fn(ctx, sep="", text="abc", pattern="[") == "0"


# ---------------------------------------------------------------------------
# Weighted random (4 tests)
# ---------------------------------------------------------------------------


class TestWeightedRandom:
    async def test_registered_with_dsl_name(self) -> None:
        td = registry.get("weighted_random")
        assert td is not None
        assert td.dsl_name == "概率随机"

    async def test_returns_one_of_values_seeded(self, ctx: ToolCtx) -> None:
        ctx.extras["random"] = random.Random(42)
        td = registry.get("weighted_random")
        assert td is not None
        result = await td.fn(ctx, weights="[1, 1, 1]", values='["a", "b", "c"]')
        assert result in {"a", "b", "c"}

    async def test_heavy_weight_dominates(self, ctx: ToolCtx) -> None:
        td = registry.get("weighted_random")
        assert td is not None
        hits = {"a": 0, "b": 0}
        ctx.extras["random"] = random.Random(123)
        for _ in range(1000):
            result = await td.fn(ctx, weights="[1, 99]", values='["a", "b"]')
            hits[result] += 1
        assert hits["b"] > hits["a"] * 5  # overwhelmingly b

    async def test_length_mismatch_truncates_to_shorter(self, ctx: ToolCtx) -> None:
        """Mismatched weights/values lengths align by truncating to the
        shorter list rather than raising. Keeps QRDic-style typo'd
        rules running rather than crashing the dispatcher.
        """
        td = registry.get("weighted_random")
        assert td is not None
        # Two weights but only one value → truncates to one slot, picks "only".
        result = await td.fn(ctx, weights="[1, 2]", values='["only"]')
        assert result == "only"


# ---------------------------------------------------------------------------
# Globals (4 tests)
# ---------------------------------------------------------------------------


class TestGlobals:
    async def test_set_and_get(self, ctx: ToolCtx) -> None:
        setter = registry.get("set_global")
        getter = registry.get("get_global")
        assert setter is not None and getter is not None
        await setter.fn(ctx, key="greeting", value="hi")
        assert await getter.fn(ctx, key="greeting") == "hi"

    async def test_get_missing_returns_default(self, ctx: ToolCtx) -> None:
        getter = registry.get("get_global")
        assert getter is not None
        assert await getter.fn(ctx, key="absent") == ""
        assert await getter.fn(ctx, key="absent", default="fallback") == "fallback"

    async def test_set_returns_stored_value(self, ctx: ToolCtx) -> None:
        setter = registry.get("set_global")
        assert setter is not None
        assert await setter.fn(ctx, key="x", value="1") == "1"

    async def test_autouse_fixture_clears_globals(self, ctx: ToolCtx) -> None:
        # Previous tests should not leak into this one.
        getter = registry.get("get_global")
        assert getter is not None
        assert await getter.fn(ctx, key="greeting") == ""


# ---------------------------------------------------------------------------
# Image-text (2 tests)
# ---------------------------------------------------------------------------


class TestImageText:
    async def test_produces_valid_png(self, tmp_path: Path, ctx: ToolCtx) -> None:
        from PIL import Image

        ctx.extras["image_text_cache_dir"] = tmp_path
        td = registry.get("image_text")
        assert td is not None
        path = await td.fn(ctx, content="hello\nworld", font_size=16, padding=10)
        p = Path(path)
        assert p.exists()  # noqa: ASYNC240 — cheap local FS check
        assert p.suffix == ".png"
        with Image.open(p) as img:
            assert img.format == "PNG"
            assert img.size[0] > 20 and img.size[1] > 20

    async def test_respects_decorations_without_crashing(
        self, tmp_path: Path, ctx: ToolCtx
    ) -> None:
        from PIL import Image

        ctx.extras["image_text_cache_dir"] = tmp_path
        td = registry.get("image_text")
        assert td is not None
        path = await td.fn(
            ctx,
            content="decorated",
            font_size=20,
            padding=8,
            bold=True,
            underline=True,
            strikethru=True,
            background="#FFFFFF",
            text_color="#FF0000",
        )
        with Image.open(path) as img:
            assert img.mode == "RGB"


# ---------------------------------------------------------------------------
# Gacha image (3 tests)
# ---------------------------------------------------------------------------


class TestGachaImage:
    """Tests for the ``$扭蛋图$`` settlement panel renderer."""

    async def test_registered_with_dsl_name(self) -> None:
        td = registry.get("gacha_image")
        assert td is not None
        assert td.dsl_name == "扭蛋图"

    async def test_ten_pull_with_ur_renders_png(
        self, tmp_path: Path, ctx: ToolCtx
    ) -> None:
        from PIL import Image

        ctx.extras["image_text_cache_dir"] = tmp_path
        # The KV-stored %录% uses *literal* ``\n`` separators (the rule
        # writes ``$写 ... %录%\n<line>$`` and the DSL escape pass keeps
        # the backslash-n verbatim) — production data looks like this.
        record = (
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
        td = registry.get("gacha_image")
        assert td is not None
        result = await td.fn(ctx, record=record, kind="十连", cost="488")
        # Tool returns a ``base64://`` URL so the OneBot adapter can
        # ship the image to LLBot without a shared filesystem.
        assert result.startswith("base64://")
        import base64 as _b64
        import io
        png = _b64.b64decode(result[len("base64://") :])
        with Image.open(io.BytesIO(png)) as img:
            assert img.format == "PNG"
            # 10-pull layout is wider than tall in 10-pull mode but still
            # comfortably > 800px on each side. Hero strip is present
            # because the record contains a UR (郫忧) — so height ≥ 1100.
            assert img.size[0] >= 800
            assert img.size[1] >= 1100  # hero strip present

    async def test_parses_literal_backslash_n_record(
        self, tmp_path: Path, ctx: ToolCtx
    ) -> None:
        """``%录%`` round-trips through KV with literal ``\\n``; parser must accept."""
        from linling_tools_stdlib.gacha_image import _parse_record

        # 5 N + 1 R + 1 SR + 3 N — 10 drops total (leading sentinel ignored).
        record = (
            r"扭哇扭哇～\n"
            r"🍬蛋壳+1\n🍬蛋壳+1\n🍬蛋壳+1\n🍬蛋壳+1\n🍬蛋壳+1\n"
            r"✨获得〔大飞龙〕！！\n"
            r"✨恭喜获得珍品〖思思〗！！\n"
            r"🍬蛋壳+1\n🍬蛋壳+1\n🍬蛋壳+1"
        )
        drops = _parse_record(record)
        # The leading "扭哇扭哇～" sentinel doesn't match any drop pattern.
        assert len(drops) == 10
        rarities = [d.rarity.name for d in drops]
        assert rarities.count("N") == 8
        assert rarities.count("R") == 1
        assert rarities.count("SR") == 1

    async def test_parses_full_fifty_pull_egg_run(self) -> None:
        """Every 蛋壳 in a 50-pull must parse as its own N drop.

        Regression guard for the bug where 单五十次扭蛋 wrote eggs as a
        bare ``%蛋%🍬`` (no ``\\n``, no "蛋壳" word) — the parser saw a
        single run-on line and dropped ~44 of 50 cards. The rule was
        aligned to the 10-pull format (``%蛋%\\n🍬蛋壳+1``); this test
        pins that contract from the parser side so a future rule edit
        that reintroduces the bare-🍬 form fails here.
        """
        from linling_tools_stdlib.gacha_image import _parse_record

        # 46 eggs + 1 R + 1 SR + 2 eggs, all separated like the fixed
        # rule writes them. 50 drops, leading sentinel ignored.
        lines = [r"扭哇扭哇～"]
        lines += [r"🍬蛋壳+1"] * 46
        lines += [r"✨获得〔大飞龙〕！！", r"✨恭喜获得珍品〖思思〗！！"]
        lines += [r"🍬蛋壳+1"] * 2
        record = r"\n".join(lines)

        drops = _parse_record(record)
        assert len(drops) == 50
        rarities = [d.rarity.name for d in drops]
        assert rarities.count("N") == 48
        assert rarities.count("R") == 1
        assert rarities.count("SR") == 1

    async def test_missing_rare_sprite_does_not_render_as_egg(self) -> None:
        """A missing collectible sprite must not use the 蛋壳 visual fallback."""
        from linling_tools_stdlib.gacha_image import _N, _SR, Drop, _draw_card
        from PIL import Image, ImageFont

        def centre_colour(drop: Drop) -> tuple[int, int, int]:
            canvas = Image.new("RGBA", (240, 300), (8, 8, 18, 255))
            _draw_card(
                canvas,
                (20, 20, 220, 280),
                drop,
                None,
                ImageFont.load_default(),
            )
            return canvas.convert("RGB").getpixel((120, 124))

        egg = centre_colour(Drop("蛋壳", _N))
        rare_missing_sprite = centre_colour(Drop("思思", _SR))

        def distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
            return sum(abs(x - y) for x, y in zip(a, b, strict=True))

        assert distance(egg, (245, 230, 200)) < 80
        assert distance(rare_missing_sprite, (245, 230, 200)) > 120

    async def test_empty_record_falls_back_to_placeholder(
        self, tmp_path: Path, ctx: ToolCtx
    ) -> None:
        from PIL import Image

        ctx.extras["image_text_cache_dir"] = tmp_path
        td = registry.get("gacha_image")
        assert td is not None
        # Empty record should still yield a sensible image (not crash).
        result = await td.fn(ctx, record="", kind="五十连", cost="2388")
        assert result.startswith("base64://")
        import base64 as _b64
        import io
        png = _b64.b64decode(result[len("base64://") :])
        with Image.open(io.BytesIO(png)) as img:
            # 50-cell layout is wider than the 10-cell one (10 cols vs 5).
            assert img.size[0] >= 1200


# ---------------------------------------------------------------------------
# Adapter RPC stubs (4 tests)
# ---------------------------------------------------------------------------


class TestAdapterRpc:
    async def test_group_nickname_falls_back_without_adapter(self, ctx: ToolCtx) -> None:
        td = registry.get("group_nickname")
        assert td is not None
        result = await td.fn(ctx, group_id="g1", user_id="u42")
        assert result == "u42"

    async def test_group_nickname_uses_adapter_card(self, ctx: ToolCtx) -> None:
        adapter = MagicMock()
        adapter.rpc = AsyncMock(return_value={"card": "Alice", "nickname": "alice"})
        ctx.extras["adapter"] = adapter

        td = registry.get("group_nickname")
        assert td is not None
        assert await td.fn(ctx, group_id="g1", user_id="u42") == "Alice"
        adapter.rpc.assert_awaited_once_with("get_group_member_info", group_id="g1", user_id="u42")

    async def test_group_members_returns_json_array(self, ctx: ToolCtx) -> None:
        adapter = MagicMock()
        adapter.rpc = AsyncMock(
            return_value=[
                {"user_id": 1, "nickname": "alice"},
                {"user_id": 2, "nickname": "bob"},
            ]
        )
        ctx.extras["adapter"] = adapter
        td = registry.get("group_members")
        assert td is not None
        result = await td.fn(ctx, group_id="g1")
        assert json.loads(result) == ["1", "2"]

    async def test_get_message_field_reads_event_raw(self, ctx: ToolCtx) -> None:
        # Build an Event-like object with a `raw` attribute; the tool only
        # needs `.raw` to be a dict, so a plain MagicMock is enough.
        event = MagicMock()
        event.raw = {"message_id": 12345, "font": "default"}
        ctx.event = event

        td = registry.get("get_message_field")
        assert td is not None
        assert await td.fn(ctx, field="message_id") == "12345"
        assert await td.fn(ctx, field="font") == "default"
        assert await td.fn(ctx, field="missing", default="fallback") == "fallback"

    async def test_group_list_returns_empty_array_without_adapter(
        self, ctx: ToolCtx
    ) -> None:
        td = registry.get("group_list")
        assert td is not None
        assert await td.fn(ctx) == "[]"

    async def test_group_list_returns_json_array_via_adapter(
        self, ctx: ToolCtx
    ) -> None:
        adapter = MagicMock()
        adapter.rpc = AsyncMock(
            return_value=[
                {"group_id": 754800438, "group_name": "main"},
                {"group_id": 11111, "group_name": "test"},
            ]
        )
        ctx.extras["adapter"] = adapter

        td = registry.get("group_list")
        assert td is not None
        result = await td.fn(ctx)
        assert json.loads(result) == ["754800438", "11111"]
        adapter.rpc.assert_awaited_once_with("get_group_list")


    async def test_group_add_request_approves_via_flag(self, ctx: ToolCtx) -> None:
        """``$进群审核 group user 2001 11 reason$`` resolves the request via flag."""
        from linling_core.events import Event, Scope, User
        from linling_core.segments import TextSegment

        adapter = MagicMock()
        adapter.rpc = AsyncMock(return_value={"status": "ok"})
        ctx.extras["adapter"] = adapter
        # The synthetic [系统] event always carries ``flag`` in raw —
        # the QRSpeed handler resolves the request via that flag, not
        # group_id / user_id (those are informational).
        ctx.event = Event(
            id="x",
            platform="onebot",
            bot_id="b",
            scope=Scope(kind="group", id="g", platform="onebot"),
            sender=User(id="u", platform="onebot"),
            kind="message",
            segments=[TextSegment(text="[系统]")],
            raw={"flag": "abc-123"},
        )

        td = registry.get("group_add_request")
        assert td is not None
        result = await td.fn(ctx, "g", "u", "2001", "11", "welcome")
        assert result == "ok"
        adapter.rpc.assert_awaited_once_with(
            "set_group_add_request",
            flag="abc-123",
            sub_type="add",
            approve=True,
            reason="welcome",
        )

    async def test_group_add_request_rejects(self, ctx: ToolCtx) -> None:
        from linling_core.events import Event, Scope, User
        from linling_core.segments import TextSegment

        adapter = MagicMock()
        adapter.rpc = AsyncMock(return_value={"status": "ok"})
        ctx.extras["adapter"] = adapter
        ctx.event = Event(
            id="x",
            platform="onebot",
            bot_id="b",
            scope=Scope(kind="group", id="g", platform="onebot"),
            sender=User(id="u", platform="onebot"),
            kind="message",
            segments=[TextSegment(text="[系统]")],
            raw={"flag": "rej-1"},
        )

        td = registry.get("group_add_request")
        assert td is not None
        # 31=拒绝 / 12=拒绝 → approve=False
        result = await td.fn(ctx, "g", "u", "31", "12", "you", "are", "in", "blacklist")
        assert result == "ok"
        adapter.rpc.assert_awaited_once_with(
            "set_group_add_request",
            flag="rej-1",
            sub_type="add",
            approve=False,
            reason="you are in blacklist",
        )

    async def test_group_add_request_no_flag_in_raw_drops_call(self, ctx: ToolCtx) -> None:
        from linling_core.events import Event, Scope, User
        from linling_core.segments import TextSegment

        adapter = MagicMock()
        adapter.rpc = AsyncMock(return_value={"status": "ok"})
        ctx.extras["adapter"] = adapter
        # Event without ``flag`` (e.g. someone called the tool from a
        # plain message handler, not the synthetic [系统] event).
        ctx.event = Event(
            id="x",
            platform="onebot",
            bot_id="b",
            scope=Scope(kind="group", id="g", platform="onebot"),
            sender=User(id="u", platform="onebot"),
            kind="message",
            segments=[TextSegment(text="msg")],
            raw={},
        )

        td = registry.get("group_add_request")
        assert td is not None
        result = await td.fn(ctx, "g", "u", "2001", "11")
        # No flag → no API call → empty result.
        assert result == ""
        adapter.rpc.assert_not_awaited()
