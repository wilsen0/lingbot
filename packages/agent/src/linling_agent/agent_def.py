"""Agent definition loaded from YAML.

An :class:`AgentDef` describes *what* an agent is — its model, system prompt,
allowed tools, trigger rules, and guardrails — without any runtime state.
Definitions are typically loaded from YAML files at startup.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from linling_core.config import expand_env_recursive


@dataclass
class AgentGuardrails:
    """Safety limits for a single agent invocation."""

    max_tool_calls: int = 6
    max_tokens: int = 1200
    timeout_s: float = 20.0


@dataclass
class AgentTrigger:
    """When this agent should be activated.

    ``kind`` is one of: ``"mention"``, ``"dm"``, ``"keyword"``, ``"fallback"``, ``"always"``.
    ``patterns`` is only meaningful for the ``"keyword"`` kind.
    """

    kind: str
    patterns: list[str] = field(default_factory=list)


@dataclass
class AgentProviderConfig:
    """Connection details for the LLM provider this agent uses.

    Lives on :class:`AgentDef` so configuration is co-located with the
    agent's other knobs and flows through the same YAML + ``${VAR}``
    interpolation pipeline ``BotConfig`` uses. That keeps the
    ``.env`` → ``YAML`` chain as the single source of truth and
    eliminates the historical "raw ``os.environ.get`` from
    bootstrap" path.

    ``api_key`` and ``base_url`` fall back to the legacy
    ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` env vars when the YAML
    leaves them empty, so existing deployments keep working without
    edits. ``extra_headers`` has no env back-compat — the provider
    ships a default ``User-Agent`` that already passes the common
    OpenAI-compatible gates (Kimi's ``/coding/v1`` included), and
    operators who genuinely need to override it can write::

        provider_config:
          extra_headers:
            User-Agent: my-bot/1.0
    """

    api_key: str = ""
    base_url: str = ""
    # Free-form extra headers. Caller-supplied values win over the
    # provider's defaults; see :class:`OpenAIProvider._build_headers`.
    extra_headers: dict[str, str] = field(default_factory=dict)


# Default endpoints / env-var fallbacks used when an :class:`AgentDef`
# YAML omits ``provider_config``. Centralised so the same defaults
# back both the dataclass coercion and the
# ``bootstrap._provider_for`` factory.
_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


def _provider_config_from_dict(data: dict[str, Any] | None) -> AgentProviderConfig:
    """Build an :class:`AgentProviderConfig` with env-var fallbacks.

    Empty strings / missing keys for ``api_key`` and ``base_url`` fall
    back through the chain ``LLM_*`` → legacy ``OPENAI_*`` → hardcoded
    default. The ``LLM_*`` variables are the preferred convention (they
    won't collide with third-party tools that export ``OPENAI_*``);
    ``OPENAI_*`` is kept as a second fallback so old deployments work
    unchanged. ``extra_headers`` has no env fallback — the provider's
    built-in ``User-Agent`` default covers every endpoint we test
    against, and operators who need a specific override can write it
    explicitly in YAML.
    """
    raw = data or {}

    api_key = (
        raw.get("api_key")
        or os.environ.get("LLM_API_KEY", "")
        or os.environ.get("OPENAI_API_KEY", "")
    )
    base_url = (
        raw.get("base_url")
        or os.environ.get("LLM_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or _DEFAULT_OPENAI_BASE_URL
    )

    extra_headers = dict(raw.get("extra_headers") or {})

    return AgentProviderConfig(
        api_key=api_key,
        base_url=base_url,
        extra_headers=extra_headers,
    )


@dataclass
class AgentDef:
    """Definition of an agent loaded from YAML."""

    name: str
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    system: str = ""
    tools: list[str] = field(default_factory=list)
    triggers: list[AgentTrigger] = field(default_factory=list)
    guardrails: AgentGuardrails = field(default_factory=AgentGuardrails)
    temperature: float = 0.7
    provider_config: AgentProviderConfig = field(default_factory=AgentProviderConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> AgentDef:
        """Load agent definition from a YAML file.

        Runs the same ``${VAR}``-style env-var interpolation as
        :meth:`BotConfig.from_yaml`, so YAMLs can reference
        ``${OPENAI_API_KEY}`` etc. without each call site having to
        re-implement the substitution.
        """
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls.from_dict(expand_env_recursive(data))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentDef:
        """Load agent definition from a dict (parsed YAML).

        ``provider_config`` is materialised through
        :func:`_provider_config_from_dict` so legacy YAMLs (no
        ``provider_config`` block) automatically inherit the
        ``OPENAI_*`` env-var fallbacks. Callers passing a dict
        directly (i.e. bypassing :meth:`from_yaml`) are expected to
        have already performed env-var expansion, since this method
        is also used by tests that want to control the input.
        """
        triggers: list[AgentTrigger] = []
        for t in data.get("triggers", []):
            if isinstance(t, str):
                triggers.append(AgentTrigger(kind=t))
            else:
                triggers.append(
                    AgentTrigger(
                        kind=t["kind"],
                        patterns=t.get("patterns", []),
                    )
                )

        guardrails_data = data.get("guardrails", {})
        guardrails = AgentGuardrails(
            max_tool_calls=guardrails_data.get("max_tool_calls", 6),
            max_tokens=guardrails_data.get("max_tokens", 1200),
            timeout_s=guardrails_data.get("timeout_s", 20.0),
        )

        provider_config = _provider_config_from_dict(data.get("provider_config"))

        return cls(
            name=data["name"],
            provider=data.get("provider", "openai"),
            model=data.get("model", "gpt-4o-mini"),
            system=data.get("system", ""),
            tools=data.get("tools", []),
            triggers=triggers,
            guardrails=guardrails,
            temperature=data.get("temperature", 0.7),
            provider_config=provider_config,
        )
