"""Tests for :class:`AgentDef` YAML loading + provider_config wiring.

Most behaviour tests live alongside the runtime; these focus on the
``provider_config`` path that ties YAML, ``${VAR}`` interpolation, and
the legacy ``OPENAI_*`` env-var fallback together.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from linling_agent.agent_def import AgentDef, AgentProviderConfig


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every ``OPENAI_*`` and ``LLM_*`` env var so each test starts from zero."""
    for key in list(os.environ):
        if key.startswith("OPENAI_") or key.startswith("LLM_"):
            monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# from_dict — direct construction (no YAML, no env interpolation)
# ---------------------------------------------------------------------------


def test_from_dict_defaults_provider_config_empty(clean_env: None) -> None:
    """No ``provider_config`` block + no env vars → empty fields, default base."""
    a = AgentDef.from_dict({"name": "test"})

    assert isinstance(a.provider_config, AgentProviderConfig)
    assert a.provider_config.api_key == ""
    # Even with nothing configured, the OpenAI default URL is used
    # so a misconfigured deployment fails with a clear 401 from the
    # real OpenAI host instead of an empty-URL httpx error.
    assert a.provider_config.base_url == "https://api.openai.com/v1"
    assert a.provider_config.extra_headers == {}


def test_from_dict_falls_back_to_legacy_env_vars(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy ``OPENAI_*`` env vars fill empty ``provider_config`` fields.

    ``OPENAI_USER_AGENT`` was deliberately removed: the provider ships
    a ``User-Agent`` default that admits us at every endpoint we test
    against, so there is no env-level UA knob to fall back to.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("OPENAI_USER_AGENT", "from-env/9.9")  # ignored

    a = AgentDef.from_dict({"name": "test"})

    assert a.provider_config.api_key == "sk-from-env"
    assert a.provider_config.base_url == "https://example.test/v1"
    # ``OPENAI_USER_AGENT`` no longer leaks into ``extra_headers``;
    # the provider sets its own UA at request time.
    assert a.provider_config.extra_headers == {}


def test_from_dict_yaml_block_wins_over_env(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit ``provider_config`` block beats ``OPENAI_*`` env vars.

    YAML is the more specific layer (per-agent), env vars are the
    bot-wide knob; the more specific wins.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://env.test/v1")

    a = AgentDef.from_dict(
        {
            "name": "test",
            "provider_config": {
                "api_key": "sk-from-yaml",
                "base_url": "https://yaml.test/v1",
            },
        }
    )

    assert a.provider_config.api_key == "sk-from-yaml"
    assert a.provider_config.base_url == "https://yaml.test/v1"


def test_from_dict_explicit_user_agent_header_in_yaml(
    clean_env: None,
) -> None:
    """``extra_headers.User-Agent`` in YAML is preserved verbatim.

    The provider's built-in default UA is overridden at request time
    by anything in ``extra_headers``, so an explicit override here is
    the supported way to swap UAs per agent.
    """
    a = AgentDef.from_dict(
        {
            "name": "test",
            "provider_config": {
                "extra_headers": {"User-Agent": "from-yaml/2.0"},
            },
        }
    )

    assert a.provider_config.extra_headers == {"User-Agent": "from-yaml/2.0"}


def test_from_dict_partial_yaml_block_falls_back_per_field(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A YAML block that sets only ``base_url`` still picks up env API key.

    Lets operators pin the endpoint in YAML while keeping the secret
    in ``.env``.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")

    a = AgentDef.from_dict(
        {
            "name": "test",
            "provider_config": {"base_url": "https://yaml.test/v1"},
        }
    )

    assert a.provider_config.api_key == "sk-from-env"
    assert a.provider_config.base_url == "https://yaml.test/v1"


# ---------------------------------------------------------------------------
# from_yaml — full pipeline including ${VAR} interpolation
# ---------------------------------------------------------------------------


def test_from_yaml_expands_dollar_brace_vars(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``${MY_KEY}`` in YAML is substituted at load time."""
    monkeypatch.setenv("MY_KEY", "expanded-value")

    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text(
        """
name: test
provider_config:
  api_key: ${MY_KEY}
  base_url: https://example.test/v1
""".strip(),
        encoding="utf-8",
    )

    a = AgentDef.from_yaml(yaml_path)

    assert a.provider_config.api_key == "expanded-value"
    assert a.provider_config.base_url == "https://example.test/v1"


def test_from_yaml_supports_default_in_dollar_brace(
    clean_env: None, tmp_path: Path
) -> None:
    """``${VAR:-default}`` falls back to the default when VAR is unset."""
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text(
        """
name: test
provider_config:
  base_url: ${MISSING_VAR:-https://default.test/v1}
""".strip(),
        encoding="utf-8",
    )

    a = AgentDef.from_yaml(yaml_path)

    assert a.provider_config.base_url == "https://default.test/v1"


def test_from_yaml_unset_var_without_default_leaves_token(
    clean_env: None, tmp_path: Path
) -> None:
    """``${UNSET_VAR}`` (no default, no value) leaves the token in place.

    The agent layer doesn't gate on it — the provider will attempt
    the request and produce a clear runtime error rather than an
    opaque empty-string substitution.
    """
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text(
        """
name: test
provider_config:
  api_key: ${TRULY_NOT_SET_ANYWHERE}
""".strip(),
        encoding="utf-8",
    )

    a = AgentDef.from_yaml(yaml_path)

    # The literal token survives so misconfiguration is detectable.
    assert a.provider_config.api_key == "${TRULY_NOT_SET_ANYWHERE}"


def test_from_yaml_extra_headers_round_trip(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``extra_headers`` survives YAML → dict → :class:`AgentProviderConfig`."""
    monkeypatch.setenv("MY_UA", "linling-agent/1.0")

    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text(
        """
name: test
provider_config:
  api_key: dummy
  extra_headers:
    User-Agent: ${MY_UA}
    X-Org-Id: tu-shan
""".strip(),
        encoding="utf-8",
    )

    a = AgentDef.from_yaml(yaml_path)

    assert a.provider_config.extra_headers == {
        "User-Agent": "linling-agent/1.0",
        "X-Org-Id": "tu-shan",
    }
