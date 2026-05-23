"""/api/events/{bot}/{event}/replay — dry-run only."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from linling_core.events import Event, Scope, User
from linling_core.segments import TextSegment


def _mk(eid: str) -> Event:
    return Event(
        id=eid,
        platform="test",
        bot_id="susu_main",
        scope=Scope(kind="group", id="g1", platform="test"),
        sender=User(id="u1", platform="test"),
        time=datetime.now(UTC),
        kind="message",
        segments=[TextSegment(text="hi")],
    )


def test_dry_run_records_audit(app_client) -> None:
    client, _, token = app_client

    async def _push():
        buf = client.app.state.runtime.buffer_for("susu_main", capacity=20)
        await buf.publish(_mk("e1"))

    asyncio.run(_push())

    r = client.post(
        "/api/events/susu_main/e1/replay",
        json={"dry_run": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["dry_run"] is True

    audit = client.app.state.runtime.audit
    assert audit is not None
    rows = audit.search(kind="event_replay", limit=5)
    assert len(rows) == 1


def test_non_dry_run_is_rejected(app_client) -> None:
    client, _, token = app_client
    r = client.post(
        "/api/events/susu_main/whatever/replay",
        json={"dry_run": False},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400


def test_unknown_event_404(app_client) -> None:
    client, _, token = app_client
    r = client.post(
        "/api/events/susu_main/ghost/replay",
        json={"dry_run": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404
