"""/api/agents/:name/memory — short-term window view."""

from __future__ import annotations

from dataclasses import dataclass

from linling_webui.state import MemorySnapshot


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


def _login(client, username: str, password: str) -> str:  # type: ignore[no-untyped-def]
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200
    return str(r.json()["access"])


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
    assert body["summary"] == ""
    assert body["long_term"] == []


def test_memory_defaults_to_authenticated_user(app_client) -> None:
    client, _, _token = app_client
    client.app.state.runtime.auth.upsert_user(
        "viewer", "Viewer-Pwd-9", role="bot_admin", bots=["susu_main"]
    )
    token = _login(client, "viewer", "Viewer-Pwd-9")
    client.app.state.runtime.agent_registry = _Reg(_FakeAgent())
    seen: dict[str, str] = {}

    async def _provider(user_id: str, scope_id: str) -> MemorySnapshot:
        seen["user_id"] = user_id
        seen["scope_id"] = scope_id
        return MemorySnapshot(short_term=[{"role": "user", "content": user_id}])

    client.app.state.runtime.memory_providers["susu"] = _provider

    r = client.get(
        "/api/agents/susu/memory?scope_id=g1",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 200
    assert seen == {"user_id": "viewer", "scope_id": "g1"}
    assert r.json()["short_term"] == [{"role": "user", "content": "viewer"}]


def test_memory_prefers_wired_provider(app_client) -> None:
    client, _, token = app_client
    client.app.state.runtime.agent_registry = _Reg(_FakeAgent())

    async def _provider(user_id: str, scope_id: str) -> MemorySnapshot:
        assert user_id == "u1"
        assert scope_id == "g1"
        return MemorySnapshot(
            short_term=[{"role": "user", "content": "真实历史"}],
            summary="压缩摘要",
            long_term=[{"qq": "u1", "name": "小明", "profile": "喜欢钓鱼"}],
        )

    client.app.state.runtime.memory_providers["susu"] = _provider

    r = client.get(
        "/api/agents/susu/memory?user_id=u1&scope_id=g1",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["short_term"] == [{"role": "user", "content": "真实历史"}]
    assert body["summary"] == "压缩摘要"
    assert body["long_term"] == [{"qq": "u1", "name": "小明", "profile": "喜欢钓鱼"}]


def test_memory_rejects_cross_user_for_non_superadmin(app_client) -> None:
    client, _, _token = app_client
    client.app.state.runtime.auth.upsert_user(
        "viewer", "Viewer-Pwd-9", role="bot_admin", bots=["susu_main"]
    )
    token = _login(client, "viewer", "Viewer-Pwd-9")
    client.app.state.runtime.agent_registry = _Reg(_FakeAgent())
    called = False

    async def _provider(user_id: str, scope_id: str) -> MemorySnapshot:
        nonlocal called
        called = True
        return MemorySnapshot(short_term=[{"role": "user", "content": user_id}])

    client.app.state.runtime.memory_providers["susu"] = _provider

    r = client.get(
        "/api/agents/susu/memory?user_id=alice&scope_id=g1",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 403
    assert called is False


def test_memory_superadmin_can_query_explicit_user(app_client) -> None:
    client, _, token = app_client
    client.app.state.runtime.agent_registry = _Reg(_FakeAgent())
    seen: dict[str, str] = {}

    async def _provider(user_id: str, scope_id: str) -> MemorySnapshot:
        seen["user_id"] = user_id
        seen["scope_id"] = scope_id
        return MemorySnapshot(
            short_term=[{"role": "user", "content": "真实历史"}],
            summary="压缩摘要",
            long_term=[{"qq": user_id, "name": "小明", "profile": "喜欢钓鱼"}],
        )

    client.app.state.runtime.memory_providers["susu"] = _provider

    r = client.get(
        "/api/agents/susu/memory?user_id=u1&scope_id=g1",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 200
    assert seen == {"user_id": "u1", "scope_id": "g1"}
    assert r.json()["long_term"] == [{"qq": "u1", "name": "小明", "profile": "喜欢钓鱼"}]
