"""/api/events — listing from ring buffer, per-bot visibility."""

from __future__ import annotations

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
        segments=[TextSegment(text=f"hi {eid}")],
    )


async def _seed(app, *ids: str, bot_id: str = "susu_main") -> None:
    buf = app.state.runtime.buffer_for(bot_id, capacity=50)
    for i in ids:
        await buf.publish(_mk(i, bot_id))


def test_empty_events(app_client) -> None:
    client, _, token = app_client
    r = client.get("/api/events", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json() == {"items": [], "next_cursor": None}


def test_events_roundtrip(app_client) -> None:
    client, _, token = app_client
    import asyncio

    asyncio.run(_seed(client.app, "a", "b", "c"))
    r = client.get("/api/events", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    ids = [it["id"] for it in body["items"]]
    assert ids == ["a", "b", "c"]
    assert all(it["bot_id"] == "susu_main" for it in body["items"])


def test_events_since_cursor_filter(app_client) -> None:
    client, _, token = app_client
    import asyncio

    asyncio.run(_seed(client.app, "a", "b", "c", "d"))
    r = client.get(
        "/api/events?since_seq=2",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = r.json()
    ids = [it["id"] for it in body["items"]]
    assert ids == ["c", "d"]


def test_events_mine_filters_to_authenticated_sender(app_client) -> None:
    client, _, token = app_client
    import asyncio

    async def _seed_mixed():
        buf = client.app.state.runtime.buffer_for("susu_main", capacity=50)
        await buf.publish(_mk("mine", sender_id="alice"))
        await buf.publish(_mk("other", sender_id="someone-else"))

    asyncio.run(_seed_mixed())
    r = client.get(
        "/api/events?mine=true",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    ids = [it["id"] for it in r.json()["items"]]
    assert ids == ["mine"]


def test_events_mine_limit_is_applied_after_filtering(app_client) -> None:
    client, _, token = app_client
    import asyncio

    async def _seed_mixed():
        buf = client.app.state.runtime.buffer_for("susu_main", capacity=50)
        await buf.publish(_mk("mine", sender_id="alice"))
        for idx in range(5):
            await buf.publish(_mk(f"other-{idx}", sender_id="someone-else"))

    asyncio.run(_seed_mixed())
    r = client.get(
        "/api/events?mine=true&limit=1",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    ids = [it["id"] for it in r.json()["items"]]
    assert ids == ["mine"]
