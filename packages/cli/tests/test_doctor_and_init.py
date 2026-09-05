"""Tests for `linling doctor` and `linling init` commands."""

from __future__ import annotations

from pathlib import Path

from linling_cli.main import app
from linling_core.config import BotConfig
from typer.testing import CliRunner

runner = CliRunner()


def test_doctor_command_registered() -> None:
    result = runner.invoke(app, ["doctor", "--help"])
    assert result.exit_code == 0
    assert "Run health and environment diagnostics" in result.output


def test_init_command_registered() -> None:
    result = runner.invoke(app, ["init", "--help"])
    assert result.exit_code == 0
    assert "Initialize a clean, self-contained bot project" in result.output


def test_init_creates_project(tmp_path: Path) -> None:
    target = tmp_path / "my_test_bot"
    result = runner.invoke(app, ["init", str(target), "--name", "测试小助手"])
    assert result.exit_code == 0
    assert "初始化成功" in result.output

    assert (target / "bot.yaml").is_file()
    assert (target / "rules" / "main.ling").is_file()
    assert (target / ".env.example").is_file()

    content = (target / "bot.yaml").read_text(encoding="utf-8")
    assert "测试小助手" in content
    assert "data_dir: ./data" in content


def test_init_refuses_overwrite_without_force(tmp_path: Path) -> None:
    target = tmp_path / "overwrite_test"
    runner.invoke(app, ["init", str(target), "--name", "A"])
    # 再次创建应拒绝
    r2 = runner.invoke(app, ["init", str(target), "--name", "B"])
    assert r2.exit_code != 0
    assert "已存在 bot.yaml" in r2.output

    # 带 --force 覆盖
    r3 = runner.invoke(app, ["init", str(target), "--name", "B", "--force"])
    assert r3.exit_code == 0
    content = (target / "bot.yaml").read_text(encoding="utf-8")
    assert "B" in content


def test_doctor_on_initialized_project(tmp_path: Path) -> None:
    target = tmp_path / "doctor_target"
    runner.invoke(app, ["init", str(target), "--name", "DoctorBot"])
    result = runner.invoke(app, ["doctor", str(target / "bot.yaml")])
    assert result.exit_code == 0
    assert "[✓] 配置文件" in result.output
    assert "[✓] 数据存储目录" in result.output
    assert "[✓] 规则系统" in result.output


def test_storage_data_dir_derives_defaults() -> None:
    cfg = BotConfig.from_yaml_str("storage:\n  data_dir: ./custom_data\n")
    assert cfg.storage.data_dir == "./custom_data"
    assert cfg.storage.kv == "sqlite:///./custom_data/kv.sqlite"
    assert cfg.storage.files == "./custom_data/files"
    assert cfg.storage.audit == "sqlite:///./custom_data/audit.sqlite"
    assert cfg.storage.scheduler == "sqlite:///./custom_data/scheduler.sqlite"


def test_storage_explicit_override_wins() -> None:
    cfg = BotConfig.from_yaml_str(
        "storage:\n  data_dir: ./custom_data\n  kv: sqlite:///./custom.db\n"
    )
    assert cfg.storage.data_dir == "./custom_data"
    assert cfg.storage.kv == "sqlite:///./custom.db"
    assert cfg.storage.audit == "sqlite:///./custom_data/audit.sqlite"


def test_inline_agent_configuration_loaded() -> None:
    cfg = BotConfig.from_yaml_str("agent:\n  model: deepseek-chat\n  system: 你好助手\n")
    assert cfg.agent.model == "deepseek-chat"
    assert cfg.agent.system == "你好助手"
    assert cfg.agent.default_agent is None
