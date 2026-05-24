"""/ws/events — WS handshake, auth, filter ack, since replay, ordering.

Verifies WUI-C1 (auth on WS), WUI-C3 (since replay preserves seq order).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from linling_core.events import Event, Scope, User
from linling_core.segments import TextSegment


def _mk(eid: str, bot_id: str = "susu_main", sender_id: str = "u1") -> Event:
    return Event(
        id=eid,
        platform="test",
        bot_id=bot_id,
        scope=Scope(kind="group", id="g1", platform="test"),
        sender=User(id=sender_id, platform="test"),
        time=datetime.now(UTC),
        kind="message",
        segments=[TextSegment(text=eid)],
    )


def test_ws_rejects_missing_token(app_client) -> None:
    client, _, _ = app_client

    with pytest_raises_ws_close(client, "/ws/events"):
        pass


def pytest_raises_ws_close(client, path: str):  # type: ignore[no-untyped-def]
    class _Ctx:
        def __enter__(self_inner):
            self_inner._ws = client.websocket_connect(path)
            self_inner._ws.__enter__()
            return self_inner._ws

        def __exit__(self_inner, *args):
            try:
                self_inner._ws.__exit__(*args)
            except Exception:
                return True
            return False

    return _Ctx()


def test_ws_hello_and_live_event(app_client) -> None:
    client, _, token = app_client
    with client.websocket_connect(f"/ws/events?token={token}") as ws:
        hello = ws.receive_json()
        assert hello["t"] == "hello"
        assert hello["capacity"] > 0

        # Inject an event after WS has subscribed.
        async def _push():
            buf = client.app.state.runtime.buffer_for("susu_main", capacity=50)
            await buf.publish(_mk("e1"))

        asyncio.run(_push())

        msg = ws.receive_json()
        assert msg["t"] == "event"
        assert msg["data"]["id"] == "e1"


def test_ws_filter_replay_since(app_client) -> None:
    client, _, token = app_client

    # Pre-seed before the WS connects.
    async def _seed():
        buf = client.app.state.runtime.buffer_for("susu_main", capacity=50)
        for i in range(5):
            await buf.publish(_mk(f"s{i}"))

    asyncio.run(_seed())

    with client.websocket_connect(f"/ws/events?token={token}") as ws:
        ws.receive_json()  # hello
        ws.send_json({"t": "filter", "data": {"since_seq": 2}})
        received: list[str] = []
        # replay of seq > 2 → s2, s3, s4 (3 events) and then filter_ack
        for _ in range(3):
            msg = ws.receive_json()
            assert msg["t"] == "event"
            received.append(msg["data"]["id"])
        ack = ws.receive_json()
        assert ack["t"] == "filter_ack"
        assert ack["replayed"] == 3
        assert received == ["s2", "s3", "s4"]


def test_ws_invalid_token_closes(app_client) -> None:
    client, _, _ = app_client
    try:
        with client.websocket_connect("/ws/events?token=garbage") as ws:
            ws.receive_json()  # should raise (server closes)
    except Exception:
        return
    raise AssertionError("expected WS to close on invalid token")


def test_ws_mine_filters_live_and_replay(app_client) -> None:
    client, _, token = app_client

    async def _seed():
        buf = client.app.state.runtime.buffer_for("susu_main", capacity=50)
        await buf.publish(_mk("mine-old", sender_id="alice"))
        await buf.publish(_mk("other-old", sender_id="someone-else"))

    asyncio.run(_seed())

    with client.websocket_connect(f"/ws/events?token={token}&mine=1") as ws:
        ws.receive_json()  # hello
        ws.send_json({"t": "filter", "data": {"since_seq": 0}})
        replay = ws.receive_json()
        assert replay["t"] == "event"
        assert replay["data"]["id"] == "mine-old"
        ack = ws.receive_json()
        assert ack["t"] == "filter_ack"
        assert ack["replayed"] == 1

        async def _push():
            buf = client.app.state.runtime.buffer_for("susu_main", capacity=50)
            await buf.publish(_mk("other-live", sender_id="someone-else"))
            await buf.publish(_mk("mine-live", sender_id="alice"))

        asyncio.run(_push())
        msg = ws.receive_json()
        assert msg["t"] == "event"
        assert msg["data"]["id"] == "mine-live"
