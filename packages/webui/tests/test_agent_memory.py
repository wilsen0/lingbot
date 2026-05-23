"""/api/agents/:name/memory — short-term window view."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class _Msg:
    role: str
    content: str


class _Mem:
    def __init__(self, msgs: list[_Msg]) -> None:
        self._msgs = msgs

    def get(self, _u: str, _s: str) -> list[_Msg]:
        return self._msgs


class _FakeAgent:
    def __init__(self) -> None:
        self._memory = _Mem([_Msg("user", "你好"), _Msg("assistant", "你也好")])
        self._agent_def = type("AD", (), {"provider": "fake", "model": "fake-7b"})()


class _Reg:
    def __init__(self, a: _FakeAgent) -> None:
        self._a = a

    def get(self, name: str):  # type: ignore[no-untyped-def]
        return self._a if name == "susu" else None

    def names(self) -> list[str]:
        return ["susu"]


def test_memory_short_term(app_client) -> None:
    client, _, token = app_client
    client.app.state.runtime.agent_registry = _Reg(_FakeAgent())
    r = client.get(
        "/api/agents/susu/memory?user_id=u1&scope_id=g1",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["short_term"]) == 2
    assert body["short_term"][0]["role"] == "user"
    assert body["long_term"] == []
