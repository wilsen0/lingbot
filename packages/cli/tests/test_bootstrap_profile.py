"""Bootstrap wiring smoke test for per-user profile memory (Phase 6)."""

from __future__ import annotations

from pathlib import Path

import pytest
from linling_cli.bootstrap import _build_chat_dispatcher
from linling_core.config import BotConfig
from linling_core.metrics import NullMetrics
from linling_core.pipeline import ConversationStore
from linling_core.storage.sqlite_kv import SqliteKVStore


def _write(tmp: Path, rel: str, content: str) -> Path:
    path = tmp / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


_AGENT_YAML = """\
name: susu
provider: openai
model: mock-model
temperature: 1
provider_config:
  api_key: test-key
  base_url: https://example.invalid/v1
system: "hi"
tools:
  - read_user_profile
  - write_user_profile
"""

_BOT_YAML = """\
bot_id: bot1
name: tester
storage:
  kv: ":memory:"
agent:
  default_agent: ./agents/susu.yaml
  group_batch_enabled: false
"""


@pytest.fixture
async def kv():
    store = SqliteKVStore(bot_id="bot1", db_path=":memory:")
    async with store:
        yield store


async def test_bootstrap_wires_profile_store_and_hook(tmp_path: Path, kv) -> None:
    _write(tmp_path, "agents/susu.yaml", _AGENT_YAML)
    cfg = BotConfig.from_yaml(_write(tmp_path, "bot.yaml", _BOT_YAML))
    conversations = ConversationStore(rate_per_second=100, burst=100)

    dispatcher, agents = _build_chat_dispatcher(cfg, kv, NullMetrics(), tmp_path, conversations)

    # Profile store injected into the DM dispatcher.
    assert dispatcher._profile_store is not None
    # ContextManager has the pre-compaction distillation hook bound.
    assert dispatcher._context is not None
    assert dispatcher._context._on_before_compact is not None
    # Agent discovered.
    assert "susu" in agents
