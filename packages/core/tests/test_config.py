"""Tests for the configuration system."""

from __future__ import annotations

import os
from unittest.mock import patch

from linling_core.config import BotConfig


class TestFromYamlStr:
    """Test loading from a YAML string."""

    def test_basic_load(self) -> None:
        yaml_str = """
bot_id: my_bot
name: MyBot
main_group: "12345"
admin_users:
  - "111"
  - "222"
adapters:
  - kind: onebot
    ws_url: ws://localhost:6700
    access_token: secret
storage:
  kv: sqlite:///./mydata/kv.db
  files: ./mydata/files
rules:
  - "rules/*.ling"
"""
        cfg = BotConfig.from_yaml_str(yaml_str)
        assert cfg.bot_id == "my_bot"
        assert cfg.name == "MyBot"
        assert cfg.main_group == "12345"
        assert cfg.admin_users == ["111", "222"]
        assert len(cfg.adapters) == 1
        assert cfg.adapters[0].kind == "onebot"
        assert cfg.adapters[0].ws_url == "ws://localhost:6700"
        assert cfg.adapters[0].access_token == "secret"
        assert cfg.storage.kv == "sqlite:///./mydata/kv.db"
        assert cfg.storage.files == "./mydata/files"
        assert cfg.rules == ["rules/*.ling"]


class TestEnvVarExpansion:
    """Test env var expansion (${VAR} syntax)."""

    def test_expand_env_var(self) -> None:
        with patch.dict(os.environ, {"MY_TOKEN": "abc123"}):
            yaml_str = """
bot_id: bot1
adapters:
  - kind: onebot
    access_token: "${MY_TOKEN}"
"""
            cfg = BotConfig.from_yaml_str(yaml_str)
            assert cfg.adapters[0].access_token == "abc123"

    def test_expand_with_default(self) -> None:
        # Ensure the var is NOT set
        env = os.environ.copy()
        env.pop("UNSET_VAR_XYZ", None)
        with patch.dict(os.environ, env, clear=True):
            yaml_str = """
bot_id: "${UNSET_VAR_XYZ:-fallback_id}"
"""
            cfg = BotConfig.from_yaml_str(yaml_str)
            assert cfg.bot_id == "fallback_id"

    def test_unexpanded_var_kept(self) -> None:
        env = os.environ.copy()
        env.pop("TOTALLY_MISSING_VAR", None)
        with patch.dict(os.environ, env, clear=True):
            yaml_str = """
bot_id: "${TOTALLY_MISSING_VAR}"
"""
            cfg = BotConfig.from_yaml_str(yaml_str)
            # If var is not set and no default, the literal is kept
            assert cfg.bot_id == "${TOTALLY_MISSING_VAR}"

    def test_expand_in_list(self) -> None:
        with patch.dict(os.environ, {"ADMIN1": "user_a", "ADMIN2": "user_b"}):
            yaml_str = """
admin_users:
  - "${ADMIN1}"
  - "${ADMIN2}"
"""
            cfg = BotConfig.from_yaml_str(yaml_str)
            assert cfg.admin_users == ["user_a", "user_b"]


class TestDefaults:
    """Test default values."""

    def test_empty_yaml(self) -> None:
        cfg = BotConfig.from_yaml_str("")
        assert cfg.bot_id == "linling"
        assert cfg.name == "linling"
        assert cfg.admin_users == []
        assert cfg.main_group == ""
        assert cfg.storage.kv == "sqlite:///./data/kv.db"
        assert cfg.storage.files == "./data/files"
        assert cfg.adapters == []
        assert cfg.rules == ["rules/**/*.ling"]

    def test_partial_yaml(self) -> None:
        yaml_str = """
bot_id: partial
"""
        cfg = BotConfig.from_yaml_str(yaml_str)
        assert cfg.bot_id == "partial"
        assert cfg.name == "linling"  # default


class TestAdminUsersParsing:
    """Test admin_users list parsing."""

    def test_single_admin(self) -> None:
        yaml_str = """
admin_users:
  - "12345"
"""
        cfg = BotConfig.from_yaml_str(yaml_str)
        assert cfg.admin_users == ["12345"]

    def test_multiple_admins(self) -> None:
        yaml_str = """
admin_users:
  - "111"
  - "222"
  - "333"
"""
        cfg = BotConfig.from_yaml_str(yaml_str)
        assert cfg.admin_users == ["111", "222", "333"]

    def test_empty_admin_list(self) -> None:
        yaml_str = """
admin_users: []
"""
        cfg = BotConfig.from_yaml_str(yaml_str)
        assert cfg.admin_users == []
