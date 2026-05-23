"""Tests for the ``linling lint`` command."""

from __future__ import annotations

from pathlib import Path

import pytest
from linling_cli.main import app
from typer.testing import CliRunner


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _write(tmp_path: Path, name: str, source: str) -> Path:
    p = tmp_path / name
    p.write_text(source, encoding="utf-8")
    return p


class TestLintCommand:
    def test_clean_file_exits_zero(self, runner: CliRunner, tmp_path: Path) -> None:
        f = _write(tmp_path, "clean.ling", "触发\n返回\n")
        result = runner.invoke(app, ["lint", str(f)])
        assert result.exit_code == 0, result.output

    def test_file_with_only_warnings_exits_zero(self, runner: CliRunner, tmp_path: Path) -> None:
        f = _write(tmp_path, "warn.ling", "触发\n未用:1\n返回\n")
        result = runner.invoke(app, ["lint", str(f)])
        assert result.exit_code == 0
        assert "L100" in result.output

    def test_strict_flag_fails_on_warnings(self, runner: CliRunner, tmp_path: Path) -> None:
        f = _write(tmp_path, "warn.ling", "触发\n未用:1\n返回\n")
        result = runner.invoke(app, ["lint", "--strict", str(f)])
        assert result.exit_code == 1
        assert "L100" in result.output

    def test_directory_is_walked_recursively(self, runner: CliRunner, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        _write(tmp_path, "a.ling", "触发A\n返回\n")
        _write(tmp_path / "sub", "b.ling", "触发B\n未用:1\n返回\n")
        result = runner.invoke(app, ["lint", str(tmp_path)])
        assert result.exit_code == 0
        # Clean file produces no per-line output; b.ling's L100 must appear.
        assert "b.ling" in result.output
        assert "L100" in result.output
        assert "共检查 2 个文件" in result.output

    def test_no_files_found_exits_zero(self, runner: CliRunner, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        result = runner.invoke(app, ["lint", str(empty)])
        assert result.exit_code == 0
        assert "未找到" in result.output

    def test_syntax_error_file_is_handled(self, runner: CliRunner, tmp_path: Path) -> None:
        """A file the lenient parser accepts produces warnings (L002) and exits 0."""
        f = _write(tmp_path, "stray.ling", "触发\n如果尾\n返回\n")
        result = runner.invoke(app, ["lint", str(f)])
        # Lenient parser accepts stray 如果尾 → L002 warning, exit 0.
        assert result.exit_code == 0
        assert "L002" in result.output
