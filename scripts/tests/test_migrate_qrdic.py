"""Tests for scripts/migrate_qrdic.py.

These tests exercise the migration tool against hand-rolled fixtures
(fast unit tests) and, via the ``test_golden_path_real_qrdic`` case, the
real ``QRDic/`` tree in the repo root (slow integration test).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from linling_core.storage.sqlite_kv import SqliteKVStore
from linling_dsl import parse as parse_dsl
from typer.testing import CliRunner

# Load the top-level script as a module so tests can import its private
# helpers directly. (The script lives under scripts/ which is not on
# sys.path by default.)
_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "migrate_qrdic.py"
_SPEC = importlib.util.spec_from_file_location("migrate_qrdic", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
migrate_qrdic = importlib.util.module_from_spec(_SPEC)
sys.modules["migrate_qrdic"] = migrate_qrdic
_SPEC.loader.exec_module(migrate_qrdic)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_props(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _parse_props(text: str) -> dict[str, str]:
    return migrate_qrdic._parse_properties_text(text)  # type: ignore[no-any-return]


# ===========================================================================
# Properties parsing (delegated, but worth a smoke test)
# ===========================================================================


class TestPropertiesParsing:
    def test_basic(self) -> None:
        assert _parse_props("a=1\nb=2\n") == {"a": "1", "b": "2"}

    def test_qrspeed_header_skipped(self) -> None:
        text = "#QRSpeed YYDS\n#Sun Sep 21 11:00:16 GMT+08:00 2025\n2475957524=0\n"
        assert _parse_props(text) == {"2475957524": "0"}

    def test_unicode_escape_in_value(self) -> None:
        # \u5466\u5466 = 呦呦
        assert _parse_props("user=\\u5466\\u5466\n") == {"user": "呦呦"}

    def test_unicode_escape_in_key(self) -> None:
        assert _parse_props("\\u5466\\u5466=1\n") == {"呦呦": "1"}

    def test_comments_and_bangs(self) -> None:
        assert _parse_props("# c\n! c\nx=y\n") == {"x": "y"}


# ===========================================================================
# Scope / file extraction
# ===========================================================================


class TestScopeFile:
    def test_nested_two_levels(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        f = data_root / "啊" / "灵玉系" / "灵玉"
        f.parent.mkdir(parents=True)
        f.write_text("x=1\n", encoding="utf-8")
        assert migrate_qrdic.scope_and_file_from_path(data_root, f) == (
            "啊/灵玉系",
            "灵玉",
        )

    def test_single_level(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        f = data_root / "偷玉游戏" / "偷玉数量"
        f.parent.mkdir(parents=True)
        f.write_text("x=1\n", encoding="utf-8")
        assert migrate_qrdic.scope_and_file_from_path(data_root, f) == (
            "偷玉游戏",
            "偷玉数量",
        )

    def test_top_level_file_returns_none(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        data_root.mkdir()
        f = data_root / "stray"
        f.write_text("x=1\n", encoding="utf-8")
        assert migrate_qrdic.scope_and_file_from_path(data_root, f) is None

    def test_picture_subtree_skipped(self, tmp_path: Path) -> None:
        data_root = tmp_path / "data"
        f = data_root / "picture" / "呦呦.jpg"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"fake")
        assert migrate_qrdic.scope_and_file_from_path(data_root, f) is None


# ===========================================================================
# DSL rewrites
# ===========================================================================


class TestRewrites:
    def _rewrite(self, block: str) -> tuple[str, migrate_qrdic.MigrationReport]:
        report = migrate_qrdic.MigrationReport()
        out = migrate_qrdic.rewrite_block(block, 1, "trigger", report)
        return out, report

    def test_bsh_rewrite(self) -> None:
        out, r = self._rewrite("±img=$BSH 图文.java imagettftext 灵玉不足!$±")
        assert out == "±img=$图文 灵玉不足!$±"
        assert r.substitution_counts["bsh"] == 1

    def test_admin_qq_substitution(self) -> None:
        out, r = self._rewrite("如果:%QQ%==2078123478\n返回\n如果尾")
        assert "%管理员%" in out
        assert "2078123478" not in out
        assert r.substitution_counts["admin"] == 1

    def test_main_group_substitution(self) -> None:
        out, r = self._rewrite("如果:%群号%==754800438\n返回\n如果尾")
        assert "%主群%" in out
        assert "754800438" not in out
        assert r.substitution_counts["main_group"] == 1

    def test_picture_path_preserves_extension(self) -> None:
        out, r = self._rewrite("±img=/storage/emulated/0/QR/QRDic/data/picture/郫忧.jpg±")
        assert out == "±img=@pic:郫忧.jpg±"
        assert r.substitution_counts["pic"] == 1

    def test_picture_path_png(self) -> None:
        out, _ = self._rewrite("±img=/storage/emulated/0/QR/QRDic/data/picture/foo.png±")
        assert out == "±img=@pic:foo.png±"

    def test_partial_digit_match_ignored(self) -> None:
        """The admin QQ shouldn't match when embedded in longer digit runs."""
        out, r = self._rewrite("x=20781234780\n")  # trailing 0, not a match
        assert "2078123478" in out
        assert r.substitution_counts["admin"] == 0

    def test_multiple_on_one_line(self) -> None:
        out, r = self._rewrite("a=754800438 b=754800438")
        assert out == "a=%主群% b=%主群%"
        assert r.substitution_counts["main_group"] == 2


# ===========================================================================
# Block splitting
# ===========================================================================


class TestSplit:
    def test_two_blocks(self) -> None:
        blocks = migrate_qrdic.split_into_blocks("a\nb\n\nc\nd")
        assert blocks == [("a\nb", 1), ("c\nd", 4)]

    def test_multiple_blank_lines(self) -> None:
        blocks = migrate_qrdic.split_into_blocks("a\n\n\n\nb")
        assert [b[0] for b in blocks] == ["a", "b"]

    def test_trailing_blank(self) -> None:
        blocks = migrate_qrdic.split_into_blocks("a\nb\n")
        assert blocks == [("a\nb", 1)]


# ===========================================================================
# migrate_dsl end-to-end
# ===========================================================================


class TestMigrateDsl:
    def test_valid_handlers_all_migrate(self) -> None:
        source = "查看昵称(.*)\n$读 小苏苏/自定义昵称/昵称 %括号1% 0$\n\n上一页\n返回\n"
        report = migrate_qrdic.MigrationReport()
        out = migrate_qrdic.migrate_dsl(source, report)
        assert report.handlers_total == 2
        assert report.handlers_migrated == 2
        assert report.parse_failures == []
        # Output must reparse cleanly.
        parse_dsl(out)

    def test_lenient_unmatched_endif_now_migrates(self) -> None:
        """A single handler containing a stray 如果尾 used to fail; now it migrates."""
        source = "触发\n如果尾\n"
        report = migrate_qrdic.MigrationReport()
        migrate_qrdic.migrate_dsl(source, report)
        assert report.handlers_total == 1
        assert report.handlers_migrated == 1
        assert report.parse_failures == []

    def test_amp_amp_config_not_counted(self) -> None:
        source = "&&<配置>兼容模式:是\n\n触发\n返回\n"
        report = migrate_qrdic.MigrationReport()
        migrate_qrdic.migrate_dsl(source, report)
        # Only "触发" is a real handler.
        assert report.handlers_total == 1
        assert report.handlers_migrated == 1

    def test_orphan_if_block_merged_into_predecessor(self) -> None:
        """A block starting with 如果: is glued to the preceding handler."""
        source = "a\nb\n\n如果:%x%==1\n返回\n如果尾\n"
        report = migrate_qrdic.MigrationReport()
        out = migrate_qrdic.migrate_dsl(source, report)
        assert report.orphan_blocks_merged == 1
        assert report.handlers_total == 1
        assert report.handlers_migrated == 1
        assert report.parse_failures == []
        # Sanity: the merged output still reparses.
        parse_dsl(out)


# ===========================================================================
# Integration — tiny fake QRDic
# ===========================================================================


def _build_fake_qrdic(root: Path) -> None:
    """Mini QRDic layout with a parseable dicpro.txt + two Properties files."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "dicpro.txt").write_text(
        "&&<配置>兼容模式:是\n"
        "\n"
        "好运赠送(.*)\n"
        "如果:%群号%==754800438\n"
        "返回\n"
        "如果尾\n"
        "$写 休闲系/珍品/机会 %括号1% 1$\n"
        "完成\n"
        "\n"
        "查看郫忧\n"
        "±img=/storage/emulated/0/QR/QRDic/data/picture/郫忧.jpg±\n"
        "±img=$BSH 图文.java imagettftext 完成!$±\n"
        "\n"
        "补偿管理\n"
        "如果:%QQ%!=2078123478\n"
        "返回\n"
        "如果尾\n"
        "OK\n",
        encoding="utf-8",
    )
    # Properties under a nested scope.
    _write_props(
        root / "data" / "啊" / "灵玉系" / "灵玉",
        "#QRSpeed YYDS\n2475957524=1280\n2963327345=299\n",
    )
    _write_props(
        root / "data" / "休闲系" / "珍品" / "机会",
        "2475957524=0\n2977195274=1\n",
    )
    # .bak that must be ignored.
    _write_props(
        root / "data" / "休闲系" / "珍品" / "机会.bak",
        "2475957524=STALE\n",
    )
    # picture dir — binary; must be skipped.
    pic = root / "data" / "picture"
    pic.mkdir(parents=True)
    (pic / "郫忧.jpg").write_bytes(b"\xff\xd8\xff\xe0fake")


class TestIntegrationFake:
    @pytest.mark.asyncio
    async def test_full_run_on_fake_tree(self, tmp_path: Path) -> None:
        src = tmp_path / "QRDic"
        out = tmp_path / "out"
        _build_fake_qrdic(src)

        report = await migrate_qrdic.run_migration(src, out, "susu")

        # Handlers: 3 real + 1 && config block.
        assert report.handlers_total == 3
        assert report.handlers_migrated == 3
        assert report.parse_failures == []

        # KV counts: 2 files x rows (2 + 2 = 4), .bak skipped.
        assert report.kv_files_migrated == 2
        assert report.kv_rows_inserted == 4

        # Substitutions: 1 admin, 1 main_group, 1 pic, 1 bsh.
        counts = report.substitution_counts
        assert counts == {"admin": 1, "main_group": 1, "pic": 1, "bsh": 1}

        # main.ling written and re-parses.
        ling_path = out / "rules" / "main.ling"
        assert ling_path.is_file()
        ling = ling_path.read_text(encoding="utf-8")
        assert "@pic:郫忧.jpg" in ling
        assert "$图文 完成!$" in ling
        assert "%主群%" in ling
        assert "%管理员%" in ling
        parse_dsl(ling)  # must not raise

        # KV sqlite populated with expected rows.
        kv = SqliteKVStore("susu", db_path=out / "data.sqlite")
        try:
            assert await kv.read("啊/灵玉系", "灵玉", "2475957524") == "1280"
            assert await kv.read("休闲系/珍品", "机会", "2977195274") == "1"
            # .bak must not have overwritten.
            assert await kv.read("休闲系/珍品", "机会.bak", "2475957524") is None
        finally:
            await kv.close()

        # Report rendered.
        md = (out / "migration_report.md").read_text(encoding="utf-8")
        assert "# QRDic → linling migration report" in md
        assert "KV files migrated: 2 / 2" in md
        assert "Handlers migrated: 3 / 3" in md

    @pytest.mark.asyncio
    async def test_idempotent_rerun(self, tmp_path: Path) -> None:
        src = tmp_path / "QRDic"
        out = tmp_path / "out"
        _build_fake_qrdic(src)

        first = await migrate_qrdic.run_migration(src, out, "susu")
        second = await migrate_qrdic.run_migration(src, out, "susu")

        # Counts must be identical; no duplication.
        assert first.kv_rows_inserted == second.kv_rows_inserted
        assert first.handlers_migrated == second.handlers_migrated

    def test_cli(self, tmp_path: Path) -> None:
        src = tmp_path / "QRDic"
        out = tmp_path / "out"
        _build_fake_qrdic(src)

        runner = CliRunner()
        result = runner.invoke(
            migrate_qrdic.app,
            [
                "--src",
                str(src),
                "--out",
                str(out),
                "--bot-id",
                "susu",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Migrated" in result.output
        assert (out / "rules" / "main.ling").is_file()
        assert (out / "data.sqlite").is_file()
        assert (out / "migration_report.md").is_file()


# ===========================================================================
# Integration — real QRDic tree
# ===========================================================================


def _real_qrdic_root() -> Path | None:
    root = Path(__file__).resolve().parents[2] / "QRDic"
    return root if (root / "dicpro.txt").is_file() else None


@pytest.mark.slow
class TestIntegrationReal:
    """Golden-path test on the real QRDic/ tree."""

    @pytest.mark.asyncio
    async def test_real_qrdic_migration(self, tmp_path: Path) -> None:
        src = _real_qrdic_root()
        if src is None:
            pytest.skip("QRDic/ not present in workspace root")

        out = tmp_path / "susu"
        report = await migrate_qrdic.run_migration(src, out, "linling")

        # Baseline assertions — intentionally loose so parser improvements
        # raise the numbers rather than break the test.
        assert report.handlers_total > 100, "expected many handler blocks"
        assert report.handlers_migrated >= 440, (
            f"only {report.handlers_migrated} handlers migrated; "
            f"{len(report.parse_failures)} parse errors"
        )
        assert report.kv_files_migrated > 1000
        assert report.kv_rows_inserted > 1000

        # Output files exist.
        assert (out / "rules" / "main.ling").is_file()
        assert (out / "data.sqlite").is_file()
        assert (out / "migration_report.md").is_file()


# ---------------------------------------------------------------------------
# Type-check noop to keep mypy happy on the ``Any`` import
# ---------------------------------------------------------------------------

_ = Any
