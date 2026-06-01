"""Tests for the configuration system."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from linling_core.config import BotConfig


class TestFromYamlStr:
    """Test loading from a YAML string."""

    def test_basic_load(self) -> None:
        yaml_str = """
bot_id: my_bot
name: MyBot
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

    def test_context_and_group_batch_defaults(self) -> None:
        cfg = BotConfig.from_yaml_str("")
        assert cfg.conversation.context_max_tokens == 65_536
        assert cfg.conversation.summary_trigger_tokens == 60_000
        assert cfg.conversation.summary_keep_recent_turns == 8
        assert cfg.conversation.summary_max_tokens == 2_000
        assert cfg.agent.group_batch_enabled is False
        assert cfg.agent.group_batch_require_attention is True
        assert cfg.agent.group_batch_max_hold_s == 30.0
        assert cfg.agent.group_batch_daily_summary_enabled is False
        assert cfg.agent.group_batch_daily_summary_keep_recent_turns == 2
        assert cfg.agent.multi_reply_delay_min_s == 0.0
        assert cfg.agent.multi_reply_delay_max_s == 0.0

    def test_multi_reply_delay_config(self) -> None:
        cfg = BotConfig.from_yaml_str(
            """\
agent:
  multi_reply_delay_min_s: 2
  multi_reply_delay_max_s: 8
"""
        )

        assert cfg.agent.multi_reply_delay_min_s == 2
        assert cfg.agent.multi_reply_delay_max_s == 8

    def test_invalid_context_budget_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="summary_max_tokens"):
            BotConfig.from_yaml_str(
                """\
conversation:
  summary_max_tokens: 0
"""
            )

    def test_invalid_group_batch_budget_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="group_batch_max_messages"):
            BotConfig.from_yaml_str(
                """\
agent:
  group_batch_max_messages: 0
"""
            )

        with pytest.raises(ValueError, match="group_batch_daily_summary_keep_recent_turns"):
            BotConfig.from_yaml_str(
                """\
agent:
  group_batch_daily_summary_keep_recent_turns: -1
"""
            )

    def test_invalid_multi_reply_delay_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="multi_reply_delay_max_s"):
            BotConfig.from_yaml_str(
                """\
agent:
  multi_reply_delay_min_s: 8
  multi_reply_delay_max_s: 2
"""
            )


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
