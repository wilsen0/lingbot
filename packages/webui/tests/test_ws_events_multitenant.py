"""WS event stream only delivers events for bots the caller can see (WUI-C2)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from linling_core.events import Event, Scope, User
from linling_core.segments import TextSegment
from linling_webui.app import create_app
from linling_webui.config import WebUIConfig
from linling_webui.state import BotInfo


def _mk(bot_id: str, eid: str) -> Event:
    return Event(
        id=eid,
        platform="test",
        bot_id=bot_id,
        scope=Scope(kind="group", id="g1", platform="test"),
        sender=User(id="u1", platform="test"),
        time=datetime.now(UTC),
        kind="message",
        segments=[TextSegment(text=eid)],
    )


@pytest.fixture
def two_bots(tmp_path: Path):
    config = WebUIConfig(
        auth_db_path=tmp_path / "auth.db",
        jwt_secret="t",
        login_rate_per_minute=1000,
    )
    app = create_app(config)
    app.state.runtime.auth.upsert_user("alice", "pw", role="bot_admin", bots=["susu_main"])
    app.state.runtime.register_bot(BotInfo(id="susu_main", platform="cli", name="苏苏"))
    app.state.runtime.register_bot(BotInfo(id="other", platform="cli", name="别家"))
    with TestClient(app) as c:
        token = c.post("/api/auth/login", json={"username": "alice", "password": "pw"}).json()[
            "access"
        ]
        yield c, token


def test_alice_does_not_see_other_bot_events(two_bots) -> None:
    client, token = two_bots

    # Seed events on both bots.
    async def _seed():
        for b in ["susu_main", "other"]:
            buf = client.app.state.runtime.buffer_for(b, capacity=20)
            await buf.publish(_mk(b, f"ev-{b}"))

    asyncio.run(_seed())

    with client.websocket_connect(f"/ws/events?token={token}") as ws:
        ws.receive_json()  # hello
        ws.send_json({"t": "filter", "data": {"since_seq": 0}})

        received: list[str] = []
        # Collect replays until filter_ack.
        while True:
            msg = ws.receive_json()
            if msg["t"] == "filter_ack":
                break
            if msg["t"] == "event":
                received.append(msg["bot_id"])

        assert "susu_main" in received
        assert "other" not in received, "cross-tenant leakage"
