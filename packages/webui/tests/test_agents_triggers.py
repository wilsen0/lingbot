"""``/api/agents/{name}/triggers`` — DSL trigger list for the inline-suggest panel."""

from __future__ import annotations

from linling_webui.state import TriggerInfo


class _FakeAgent:
    """Minimal stand-in for an :class:`AgentRuntime` — the endpoint
    only needs the registry to confirm the agent exists. Mirrors the
    fake used in :mod:`tests.test_ws_agents`."""

    def __init__(self, name: str = "susu") -> None:
        self.name = name
        self._agent_def = type("AD", (), {"name": name, "provider": "fake", "model": "fake-7b"})()

    async def invoke(self, user_input: str, **_kw):  # type: ignore[no-untyped-def]
        raise RuntimeError("not used by trigger endpoint")


class _FakeRegistry:
    def __init__(self, agents: dict[str, _FakeAgent]) -> None:
        self._agents = agents

    def get(self, name: str):  # type: ignore[no-untyped-def]
        return self._agents.get(name)

    def names(self) -> list[str]:
        return list(self._agents.keys())


def _seed(client, agent_name: str = "susu", *, provider=None) -> None:  # type: ignore[no-untyped-def]
    """Wire a registry + provider onto the test app's state."""
    runtime = client.app.state.runtime
    runtime.agent_registry = _FakeRegistry({agent_name: _FakeAgent(agent_name)})
    if provider is not None:
        runtime.trigger_providers[agent_name] = provider


def test_triggers_404_for_unknown_agent(app_client) -> None:
    client, _, token = app_client
    r = client.get(
        "/api/agents/mystery/triggers",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


def test_triggers_empty_when_no_provider(app_client) -> None:
    """Agent exists but no bot is wired — endpoint returns []
    so the frontend hides the panel cleanly."""
    client, _, token = app_client
    _seed(client)
    r = client.get(
        "/api/agents/susu/triggers",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json() == []


def test_triggers_returns_provider_payload(app_client) -> None:
    client, _, token = app_client
    _seed(
        client,
        provider=lambda: [
            TriggerInfo(
                raw="我的灵玉", label="我的灵玉", has_args=False, literal_prefix="我的灵玉"
            ),
            TriggerInfo(
                raw="反馈丢失(.*)",
                label="反馈丢失…",
                has_args=True,
                literal_prefix="反馈丢失",
            ),
        ],
    )
    r = client.get(
        "/api/agents/susu/triggers",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body == [
        {
            "raw": "我的灵玉",
            "label": "我的灵玉",
            "has_args": False,
            "literal_prefix": "我的灵玉",
        },
        {
            "raw": "反馈丢失(.*)",
            "label": "反馈丢失…",
            "has_args": True,
            "literal_prefix": "反馈丢失",
        },
    ]


def test_triggers_provider_exception_degrades_to_empty(app_client) -> None:
    """Hot-reload mid-flight could raise; endpoint must not 500
    the picker — the UI just hides the panel for this poll."""
    client, _, token = app_client

    def raises() -> list[TriggerInfo]:
        raise RuntimeError("classifier swap in progress")

    _seed(client, provider=raises)
    r = client.get(
        "/api/agents/susu/triggers",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json() == []


def test_triggers_requires_auth(app_client) -> None:
    client, _, _ = app_client
    _seed(client)
    r = client.get("/api/agents/susu/triggers")
    assert r.status_code == 401
