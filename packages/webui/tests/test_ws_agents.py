"""/ws/agents/:name/stream — input, delta, done, error, cancel."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class _FakeResult:
    content: str
    tool_calls_made: int = 0
    total_tokens: int = 0


class _FakeAgent:
    def __init__(self, name: str = "susu", *, raise_on: str | None = None) -> None:
        self.name = name
        self._raise_on = raise_on
        self._agent_def = type("AD", (), {"name": name, "provider": "fake", "model": "fake-7b"})()

    async def invoke(self, user_input: str, **_kw) -> _FakeResult:
        if self._raise_on is not None and self._raise_on in user_input:
            raise RuntimeError("boom")
        return _FakeResult(content=f"你说「{user_input}」。")


class _FakeRegistry:
    def __init__(self, agents: dict[str, _FakeAgent]) -> None:
        self._agents = agents

    def get(self, name: str) -> _FakeAgent | None:
        return self._agents.get(name)

    def names(self) -> list[str]:
        return list(self._agents.keys())


def test_ws_agent_unknown_is_closed(app_client) -> None:
    client, _, token = app_client
    try:
        with client.websocket_connect(f"/ws/agents/mystery/stream?token={token}") as ws:
            # server should send error + close
            msg = ws.receive_json()
            assert msg["t"] == "error"
            ws.receive_json()  # should raise on close
    except Exception:
        return


def test_ws_agent_input_yields_delta_and_done(app_client) -> None:
    client, _, token = app_client
    client.app.state.runtime.agent_registry = _FakeRegistry({"susu": _FakeAgent()})

    with client.websocket_connect(f"/ws/agents/susu/stream?token={token}") as ws:
        hello = ws.receive_json()
        assert hello["t"] == "hello"
        assert hello["agent"] == "susu"

        ws.send_json({"t": "input", "content": "你好"})
        delta = ws.receive_json()
        assert delta["t"] == "delta"
        assert "你好" in delta["text"]
        done = ws.receive_json()
        assert done["t"] == "done"


def test_ws_agent_error_is_surfaced(app_client) -> None:
    client, _, token = app_client
    client.app.state.runtime.agent_registry = _FakeRegistry({"susu": _FakeAgent(raise_on="炸")})

    with client.websocket_connect(f"/ws/agents/susu/stream?token={token}") as ws:
        ws.receive_json()  # hello
        ws.send_json({"t": "input", "content": "炸一下"})
        err = ws.receive_json()
        assert err["t"] == "error"
        assert "boom" in err["msg"]


def test_ws_agent_audit_written(app_client) -> None:
    """WUI-C9: chat streaming leaves an audit trail."""
    client, _, token = app_client
    client.app.state.runtime.agent_registry = _FakeRegistry({"susu": _FakeAgent()})

    with client.websocket_connect(f"/ws/agents/susu/stream?token={token}") as ws:
        ws.receive_json()
        ws.send_json({"t": "input", "content": "留痕测试"})
        ws.receive_json()  # delta
        ws.receive_json()  # done

    audit = client.app.state.runtime.audit
    assert audit is not None
    rows = audit.search(kind="agent_chat_stream", limit=5)
    assert any("agents/susu" in r.scope_id for r in rows)
