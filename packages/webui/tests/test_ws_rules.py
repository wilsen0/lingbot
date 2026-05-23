"""/ws/rules/hits — relays handler_dispatch audit rows."""

from __future__ import annotations


def test_handler_dispatch_reaches_client(app_client) -> None:
    client, _, token = app_client

    with client.websocket_connect(f"/ws/rules/hits?token={token}") as ws:
        hello = ws.receive_json()
        assert hello["t"] == "hello"

        audit = client.app.state.runtime.audit
        assert audit is not None
        audit.append(
            bot_id="susu_main",
            user_id="u1",
            scope_id="g1",
            kind="handler_dispatch",
            outcome="ok",
            latency_ms=12.0,
            payload={"handler": "打卡"},
        )
        hit = ws.receive_json()
        assert hit["t"] == "hit"
        assert hit["data"]["handler"] == "打卡"
        assert hit["data"]["outcome"] == "ok"


def test_non_handler_dispatch_is_filtered(app_client) -> None:
    client, _, token = app_client

    with client.websocket_connect(f"/ws/rules/hits?token={token}") as ws:
        ws.receive_json()  # hello
        audit = client.app.state.runtime.audit
        assert audit is not None
        audit.append(
            bot_id="susu_main",
            user_id="u1",
            scope_id="g1",
            kind="tool_call",
            outcome="ok",
            payload={"tool": "read_kv"},
        )
        audit.append(
            bot_id="susu_main",
            user_id="u1",
            scope_id="g1",
            kind="handler_dispatch",
            outcome="ok",
            payload={"handler": "灵玉"},
        )
        hit = ws.receive_json()
        # Only the handler_dispatch one should have arrived.
        assert hit["data"]["handler"] == "灵玉"
