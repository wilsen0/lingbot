"""Tests for :func:`linling_cli.bootstrap._build_attention_probe`.

Covers the credential-resolution algorithm (Requirement 2), the
auto-skip-on-no-credentials behaviour (Requirement 3), and the
single ``info``-level log emission at startup (Requirement 14.1).

The probe construction creates an ``httpx.AsyncClient``; we close it
in each test via ``await probe.aclose()`` to keep the test suite
free of resource leaks.
"""

from __future__ import annotations

import asyncio
from typing import Iterable

import pytest
import structlog

from linling_agent.agent_def import AgentDef
from linling_agent.attention_probe import AttentionProbe
from linling_cli.bootstrap import _build_attention_probe
from linling_core.config import AgentConfig


_PROBE_ENV_KEYS = (
    "ATTENTION_PROBE_API_KEY",
    "ATTENTION_PROBE_BASE_URL",
    "ATTENTION_PROBE_MODEL",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
)


def _clear_probe_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _PROBE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _agent_def(model: str = "gpt-4o-mini") -> AgentDef:
    return AgentDef(name="test-agent", model=model, system="")


def _filter_events(records: Iterable[dict[str, object]], event: str) -> list[dict[str, object]]:
    return [r for r in records if r.get("event") == event]


# ---------------------------------------------------------------------------
# Toggle off path
# ---------------------------------------------------------------------------


def test_disabled_when_toggle_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_probe_env(monkeypatch)
    monkeypatch.setenv("ATTENTION_PROBE_API_KEY", "sk-irrelevant")
    config = AgentConfig(group_batch_attention_probe_enabled=False)

    with structlog.testing.capture_logs() as records:
        probe = _build_attention_probe(agent_config=config, agent_def=_agent_def())

    assert probe is None
    disabled = _filter_events(records, "group_batch.attention_probe.disabled")
    assert len(disabled) == 1
    assert disabled[0]["reason"] == "config_off"


# ---------------------------------------------------------------------------
# Auto-skip path (R3)
# ---------------------------------------------------------------------------


def test_auto_skip_when_no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_probe_env(monkeypatch)
    config = AgentConfig()  # default: enabled=True

    with structlog.testing.capture_logs() as records:
        probe = _build_attention_probe(agent_config=config, agent_def=_agent_def())

    assert probe is None
    disabled = _filter_events(records, "group_batch.attention_probe.disabled")
    assert len(disabled) == 1
    assert disabled[0]["reason"] == "no_api_key"


def test_auto_skip_treats_whitespace_only_keys_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_probe_env(monkeypatch)
    monkeypatch.setenv("ATTENTION_PROBE_API_KEY", "   ")
    monkeypatch.setenv("OPENAI_API_KEY", "\t\n")
    config = AgentConfig()

    with structlog.testing.capture_logs() as records:
        probe = _build_attention_probe(agent_config=config, agent_def=_agent_def())

    assert probe is None
    assert _filter_events(records, "group_batch.attention_probe.disabled")[0][
        "reason"
    ] == "no_api_key"


# ---------------------------------------------------------------------------
# Configured path (R2 + R14)
# ---------------------------------------------------------------------------


def test_configured_with_explicit_probe_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_probe_env(monkeypatch)
    monkeypatch.setenv("ATTENTION_PROBE_API_KEY", "sk-probe")
    monkeypatch.setenv("ATTENTION_PROBE_BASE_URL", "https://probe.example.com/v1")
    monkeypatch.setenv("ATTENTION_PROBE_MODEL", "probe-mini")
    config = AgentConfig()

    with structlog.testing.capture_logs() as records:
        probe = _build_attention_probe(agent_config=config, agent_def=_agent_def())

    assert isinstance(probe, AttentionProbe)
    assert probe.model == "probe-mini"
    assert probe.base_url == "https://probe.example.com/v1"

    configured = _filter_events(records, "group_batch.attention_probe.configured")
    assert len(configured) == 1
    assert configured[0]["model"] == "probe-mini"
    assert configured[0]["base_url"] == "https://probe.example.com/v1"

    asyncio.run(probe.aclose())


def test_falls_back_to_openai_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_probe_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    config = AgentConfig()

    probe = _build_attention_probe(agent_config=config, agent_def=_agent_def())
    assert isinstance(probe, AttentionProbe)
    asyncio.run(probe.aclose())


def test_falls_back_to_default_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_probe_env(monkeypatch)
    monkeypatch.setenv("ATTENTION_PROBE_API_KEY", "sk-probe")
    config = AgentConfig()

    probe = _build_attention_probe(agent_config=config, agent_def=_agent_def())
    assert isinstance(probe, AttentionProbe)
    assert probe.base_url == "https://api.openai.com/v1"
    asyncio.run(probe.aclose())


def test_falls_back_to_openai_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_probe_env(monkeypatch)
    monkeypatch.setenv("ATTENTION_PROBE_API_KEY", "sk-probe")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openai.example.com/v1")
    config = AgentConfig()

    probe = _build_attention_probe(agent_config=config, agent_def=_agent_def())
    assert isinstance(probe, AttentionProbe)
    assert probe.base_url == "https://openai.example.com/v1"
    asyncio.run(probe.aclose())


def test_falls_back_to_agent_def_model(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_probe_env(monkeypatch)
    monkeypatch.setenv("ATTENTION_PROBE_API_KEY", "sk-probe")
    config = AgentConfig()

    probe = _build_attention_probe(
        agent_config=config, agent_def=_agent_def(model="agent-default-model")
    )
    assert isinstance(probe, AttentionProbe)
    assert probe.model == "agent-default-model"
    asyncio.run(probe.aclose())


def test_attention_probe_model_overrides_agent_def(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_probe_env(monkeypatch)
    monkeypatch.setenv("ATTENTION_PROBE_API_KEY", "sk-probe")
    monkeypatch.setenv("ATTENTION_PROBE_MODEL", "explicit-probe-model")
    config = AgentConfig()

    probe = _build_attention_probe(
        agent_config=config, agent_def=_agent_def(model="agent-default-model")
    )
    assert isinstance(probe, AttentionProbe)
    assert probe.model == "explicit-probe-model"
    asyncio.run(probe.aclose())


def test_does_not_raise_when_credentials_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-skip MUST NOT propagate any exception."""
    _clear_probe_env(monkeypatch)
    config = AgentConfig()
    # Should not raise — that is the contract from R3.3.
    assert _build_attention_probe(agent_config=config, agent_def=_agent_def()) is None
