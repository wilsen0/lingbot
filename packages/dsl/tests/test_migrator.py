"""Tests for the QRDic → linling migrator."""

from __future__ import annotations

from pathlib import Path

import pytest
from linling_cli.main import app
from linling_core.storage.sqlite_kv import SqliteKVStore
from linling_dsl.migrator import (
    MigrationConfig,
    MigrationReport,
    migrate,
    migrate_data_tree,
    migrate_properties_file,
    migrate_script,
)
from typer.testing import CliRunner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path, **kw: object) -> MigrationConfig:
    return MigrationConfig(
        src_dir=tmp_path / "src",
        out_dir=tmp_path / "out",
        **kw,  # type: ignore[arg-type]
    )


# ===========================================================================
# Properties file parsing
# ===========================================================================


class TestPropertiesParsing:
    def test_basic_key_value(self, tmp_path: Path) -> None:
        path = tmp_path / "p.properties"
        path.write_text("a=1\nb=2\n", encoding="utf-8")
        assert migrate_properties_file(path) == {"a": "1", "b": "2"}

    def test_comments_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "p.properties"
        path.write_text("# header\n! also header\na=1\n", encoding="utf-8")
        assert migrate_properties_file(path) == {"a": "1"}

    def test_unicode_escape_decoding(self, tmp_path: Path) -> None:
        path = tmp_path / "p.properties"
        # \u5466\u5466 = 呦呦
        path.write_text("user=\\u5466\\u5466\n", encoding="utf-8")
        assert migrate_properties_file(path) == {"user": "呦呦"}

    def test_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "p.properties"
        path.write_text("", encoding="utf-8")
        assert migrate_properties_file(path) == {}

    def test_qrspeed_header_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "p.properties"
        path.write_text(
            "#QRSpeed YYDS\n#Sun Sep 21 11:00:16 GMT+08:00 2025\n2475957524=0\n",
            encoding="utf-8",
        )
        assert migrate_properties_file(path) == {"2475957524": "0"}


# ===========================================================================
# Script migration
# ===========================================================================


class TestScriptMigration:
    def test_bsh_tutu_rewrite(self, tmp_path: Path) -> None:
        src = "±img=$BSH 图文.java imagettftext 灵玉不足28!$±"
        out, warnings = migrate_script(src, _cfg(tmp_path))
        assert out == "±img=$图文 灵玉不足28!$±"
        assert any("BSH 图文" in w for w in warnings)

    def test_picture_path_replacement(self, tmp_path: Path) -> None:
        src = "±img=/storage/emulated/0/QR/QRDic/data/picture/郫忧.jpg±"
        out, _ = migrate_script(src, _cfg(tmp_path))
        assert out == "±img=@pic:郫忧±"

    def test_admin_qq_replacement(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path, admin_qq="2078123478")
        src = "如果:%QQ%==2078123478\n返回\n如果尾"
        out, warnings = migrate_script(src, cfg)
        assert out == "如果:%QQ%==%管理员%\n返回\n如果尾"
        assert any("admin QQ" in w for w in warnings)

    def test_no_admin_no_main_leaves_numbers(self, tmp_path: Path) -> None:
        src = "如果:%QQ%==2078123478\n返回\n如果尾"
        out, _ = migrate_script(src, _cfg(tmp_path))
        assert "2078123478" in out

    def test_read_write_calls_unchanged(self, tmp_path: Path) -> None:
        src = "$读 小苏苏/自定义昵称/昵称 %括号1% 0$\n$写 啊/灵玉系/灵玉 %QQ% [%玉%+100]$"
        out, _ = migrate_script(src, _cfg(tmp_path))
        assert out == src

    def test_if_blocks_unchanged(self, tmp_path: Path) -> None:
        src = "如果:%Z%==呦呦\n$jump :形象标记$\n如果尾"
        out, _ = migrate_script(src, _cfg(tmp_path))
        assert out == src

    def test_empty_script(self, tmp_path: Path) -> None:
        out, warnings = migrate_script("", _cfg(tmp_path))
        assert out == ""
        assert warnings == []

    def test_admin_qq_not_replaced_inside_longer_digits(self, tmp_path: Path) -> None:
        """Admin QQ `1234` should not match inside `12345` or `01234`."""
        cfg = _cfg(tmp_path, admin_qq="1234")
        src = "a=12345\nb=01234\nc=1234"
        out, _ = migrate_script(src, cfg)
        assert "12345" in out
        assert "01234" in out
        assert "c=%管理员%" in out


# ===========================================================================
# Data tree migration
# ===========================================================================


class TestDataTreeMigration:
    @pytest.mark.asyncio
    async def test_single_file(self, tmp_path: Path) -> None:
        (tmp_path / "data" / "scope_a" / "file1").parent.mkdir(parents=True)
        (tmp_path / "data" / "scope_a" / "file1").write_text("k1=v1\nk2=v2\n", encoding="utf-8")
        kv = SqliteKVStore("bot", db_path=tmp_path / "kv.db")
        try:
            written = await migrate_data_tree(tmp_path, kv)
            assert written == 2
            assert await kv.read("scope_a", "file1", "k1") == "v1"
            assert await kv.read("scope_a", "file1", "k2") == "v2"
        finally:
            await kv.close()

    @pytest.mark.asyncio
    async def test_nested_scopes(self, tmp_path: Path) -> None:
        (tmp_path / "data" / "a" / "b" / "c").parent.mkdir(parents=True)
        (tmp_path / "data" / "a" / "b" / "c").write_text("x=y\n", encoding="utf-8")
        kv = SqliteKVStore("bot", db_path=tmp_path / "kv.db")
        try:
            written = await migrate_data_tree(tmp_path, kv)
            assert written == 1
            assert await kv.read("a/b", "c", "x") == "y"
        finally:
            await kv.close()

    @pytest.mark.asyncio
    async def test_bak_files_skipped(self, tmp_path: Path) -> None:
        d = tmp_path / "data" / "s"
        d.mkdir(parents=True)
        (d / "real").write_text("k=v\n", encoding="utf-8")
        (d / "real.bak").write_text("k=stale\n", encoding="utf-8")
        kv = SqliteKVStore("bot", db_path=tmp_path / "kv.db")
        try:
            written = await migrate_data_tree(tmp_path, kv)
            assert written == 1
            assert await kv.read("s", "real", "k") == "v"
            assert await kv.read("s", "real.bak", "k") is None
        finally:
            await kv.close()

    @pytest.mark.asyncio
    async def test_unicode_escaped_values_decoded(self, tmp_path: Path) -> None:
        d = tmp_path / "data" / "休闲系" / "珍品"
        d.mkdir(parents=True)
        (d / "个人守护").write_text("2992611516=\\u5C0F\\u8C46\\u82BD\n", encoding="utf-8")
        kv = SqliteKVStore("bot", db_path=tmp_path / "kv.db")
        try:
            await migrate_data_tree(tmp_path, kv)
            assert await kv.read("休闲系/珍品", "个人守护", "2992611516") == "小豆芽"
        finally:
            await kv.close()


# ===========================================================================
# End-to-end migration
# ===========================================================================


def _build_mini_qrdic(root: Path) -> None:
    """Create a miniature QRDic tree under ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "dicpro.txt").write_text(
        "好运赠送(.*)\n"
        "如果:%群号%==754800438\n"
        "返回\n"
        "如果尾\n"
        "$写 休闲系/珍品/机会 %括号1% 1$\n"
        "±img=/storage/emulated/0/QR/QRDic/data/picture/呦呦.jpg±\n"
        "±img=$BSH 图文.java imagettftext 完成!$±\n",
        encoding="utf-8",
    )
    # Data files
    d = root / "data" / "休闲系" / "珍品"
    d.mkdir(parents=True)
    (d / "机会").write_text("#QRSpeed YYDS\n2475957524=0\n2977195274=1\n", encoding="utf-8")
    (d / "机会.bak").write_text("2475957524=stale\n", encoding="utf-8")
    # Picture
    pic_dir = root / "data" / "picture"
    pic_dir.mkdir(parents=True)
    (pic_dir / "呦呦.jpg").write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_full_migration(self, tmp_path: Path) -> None:
        src = tmp_path / "QRDic"
        out = tmp_path / "out"
        _build_mini_qrdic(src)

        cfg = MigrationConfig(src_dir=src, out_dir=out)
        report = await migrate(cfg)

        assert report.rules_written == 1
        assert report.kv_entries_written == 2  # 2 entries, .bak skipped

        # .ling script rewritten
        ling = (out / "rules" / "main.ling").read_text(encoding="utf-8")
        assert "@pic:呦呦" in ling
        assert "$图文 完成!$" in ling

        # KV populated
        kv = SqliteKVStore("linling", db_path=out / "data" / "kv.db")
        try:
            assert await kv.read("休闲系/珍品", "机会", "2475957524") == "0"
        finally:
            await kv.close()

        # Picture copied
        assert (out / "files" / "picture" / "呦呦.jpg").is_file()

        # bot.yaml written
        assert (out / "bot.yaml").is_file()

    @pytest.mark.asyncio
    async def test_report_counts(self, tmp_path: Path) -> None:
        src = tmp_path / "QRDic"
        out = tmp_path / "out"
        _build_mini_qrdic(src)

        report = await migrate(MigrationConfig(src_dir=src, out_dir=out))
        assert report.rules_written > 0
        assert report.kv_entries_written > 0
        assert isinstance(report, MigrationReport)

    @pytest.mark.asyncio
    async def test_report_file_generated(self, tmp_path: Path) -> None:
        src = tmp_path / "QRDic"
        out = tmp_path / "out"
        _build_mini_qrdic(src)

        await migrate(MigrationConfig(src_dir=src, out_dir=out))

        md = (out / "migration_report.md").read_text(encoding="utf-8")
        assert "# Migration Report" in md
        assert "Handlers written:" in md
        assert "KV entries written:" in md


# ===========================================================================
# CLI
# ===========================================================================


class TestCli:
    def test_migrate_qrdic_command(self, tmp_path: Path) -> None:
        src = tmp_path / "QRDic"
        out = tmp_path / "out"
        _build_mini_qrdic(src)

        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "migrate",
                "qrdic",
                "--src",
                str(src),
                "--out",
                str(out),
                "--bot-id",
                "susu",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Migration complete" in result.output
        assert (out / "rules" / "main.ling").is_file()
        assert (out / "data" / "kv.db").is_file()
        assert (out / "migration_report.md").is_file()
        assert (out / "bot.yaml").is_file()
