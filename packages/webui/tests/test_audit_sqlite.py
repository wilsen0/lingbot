"""Tests for :class:`SqliteAuditReader` + bootstrap selection."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from linling_webui.audit_reader import (
    AuditReader,
    AuditReaderProtocol,
    SqliteAuditReader,
)

# ---------------------------------------------------------------------------
# Drop-in compatibility — the SQLite backend must answer the same
# questions as the in-memory one, so we ship the same suite against
# both via a parametrized fixture.
# ---------------------------------------------------------------------------


@pytest.fixture(params=["memory", "sqlite"])
def reader(request, tmp_path: Path) -> AuditReaderProtocol:
    if request.param == "memory":
        return AuditReader()
    return SqliteAuditReader(tmp_path / "audit.db")


def _seed(reader: AuditReaderProtocol) -> None:
    reader.append(bot_id="bot1", user_id="u1", scope_id="g1", kind="cmd", outcome="ok")
    reader.append(bot_id="bot1", user_id="u2", scope_id="g1", kind="chat", outcome="ok")
    reader.append(bot_id="bot2", user_id="u3", scope_id="g2", kind="cmd", outcome="error")
    reader.append(
        bot_id="bot1",
        user_id="u1",
        scope_id="g1",
        kind="cmd",
        outcome="ok",
        latency_ms=42.0,
        payload={"trigger": "ping", "trace_id": "abc"},
    )


def test_append_returns_row_with_metadata(reader: AuditReaderProtocol) -> None:
    row = reader.append(bot_id="b", user_id="u", scope_id="s", kind="k")
    assert row.id and len(row.id) >= 8
    assert row.bot_id == "b"
    assert row.outcome == "ok"
    assert row.payload == {}
    assert row.time > 0


def test_search_returns_newest_first(reader: AuditReaderProtocol) -> None:
    from itertools import pairwise

    _seed(reader)
    rows = reader.search(limit=10)
    assert len(rows) == 4
    # Strictly non-increasing time.
    for prev, nxt in pairwise(rows):
        assert prev.time >= nxt.time


def test_search_filters_by_bot_id(reader: AuditReaderProtocol) -> None:
    _seed(reader)
    rows = reader.search(bot_ids=["bot1"])
    assert all(r.bot_id == "bot1" for r in rows)
    assert len(rows) == 3


def test_search_filters_by_kind(reader: AuditReaderProtocol) -> None:
    _seed(reader)
    rows = reader.search(kind="cmd")
    assert all(r.kind == "cmd" for r in rows)
    assert len(rows) == 3


def test_search_filters_by_outcome(reader: AuditReaderProtocol) -> None:
    _seed(reader)
    rows = reader.search(outcome="error")
    assert all(r.outcome == "error" for r in rows)
    assert len(rows) == 1


def test_search_filters_by_user(reader: AuditReaderProtocol) -> None:
    _seed(reader)
    rows = reader.search(user_id="u1")
    assert all(r.user_id == "u1" for r in rows)
    assert len(rows) == 2


def test_search_filters_by_time_window(reader: AuditReaderProtocol) -> None:
    _seed(reader)
    now = time.time()
    rows = reader.search(since=now - 60)
    assert len(rows) == 4
    rows = reader.search(until=0)
    assert rows == []


def test_search_q_substring_match(reader: AuditReaderProtocol) -> None:
    _seed(reader)
    rows = reader.search(q="ping")
    assert len(rows) == 1
    assert rows[0].payload.get("trigger") == "ping"


def test_search_empty_bot_ids_returns_empty(reader: AuditReaderProtocol) -> None:
    """``bot_ids=[]`` means the caller has zero allowed bots — shouldn't see anything."""
    _seed(reader)
    assert reader.search(bot_ids=[]) == []


def test_subscribe_fires_on_append(reader: AuditReaderProtocol) -> None:
    seen = []
    unsub = reader.subscribe(lambda r: seen.append(r.id))
    r1 = reader.append(bot_id="b", user_id="u", scope_id="s", kind="k")
    r2 = reader.append(bot_id="b", user_id="u", scope_id="s", kind="k")
    assert seen == [r1.id, r2.id]
    unsub()
    reader.append(bot_id="b", user_id="u", scope_id="s", kind="k")
    assert len(seen) == 2  # didn't grow after unsubscribe


def test_subscriber_exception_does_not_break_append(reader: AuditReaderProtocol) -> None:
    def boom(_r):
        raise RuntimeError("nope")

    reader.subscribe(boom)
    # Must not raise.
    row = reader.append(bot_id="b", user_id="u", scope_id="s", kind="k")
    assert row.id


# ---------------------------------------------------------------------------
# SQLite-specific behaviours
# ---------------------------------------------------------------------------


def test_sqlite_persists_across_reopens(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    r1 = SqliteAuditReader(db)
    r1.append(bot_id="b", user_id="u", scope_id="s", kind="k", payload={"x": 1})
    r1.close()

    r2 = SqliteAuditReader(db)
    rows = r2.search(limit=10)
    assert len(rows) == 1
    assert rows[0].payload == {"x": 1}


def test_sqlite_sweep_drops_old_rows(tmp_path: Path) -> None:
    r = SqliteAuditReader(tmp_path / "a.db", ttl_seconds=60)
    # Inject an old row by writing past ttl.
    old_row = r.append(bot_id="b", user_id="u", scope_id="s", kind="k")
    # Pretend ``now`` is far in the future so the seeded row is stale.
    future = old_row.time + 3600
    removed = r.sweep(now=future)
    assert removed == 1
    assert r.count() == 0


def test_sqlite_sweep_with_no_ttl_is_noop(tmp_path: Path) -> None:
    r = SqliteAuditReader(tmp_path / "a.db", ttl_seconds=None)
    r.append(bot_id="b", user_id="u", scope_id="s", kind="k")
    assert r.sweep() == 0
    assert r.count() == 1


def test_sqlite_corrupted_payload_doesnt_crash_search(tmp_path: Path) -> None:
    """Hand-mangled ``payload`` text should yield ``{}`` rather than raise."""
    db = tmp_path / "a.db"
    r = SqliteAuditReader(db)
    r.append(bot_id="b", user_id="u", scope_id="s", kind="k", payload={"ok": True})
    # Corrupt the payload column directly.
    with r._lock:
        r._conn.execute("UPDATE audit SET payload='not json{{'")
        r._conn.commit()
    rows = r.search(limit=10)
    assert len(rows) == 1
    assert rows[0].payload == {}


def test_sqlite_in_memory_url(tmp_path: Path) -> None:
    """``:memory:`` is supported and behaves like an ephemeral DB."""
    r = SqliteAuditReader(":memory:")
    r.append(bot_id="b", user_id="u", scope_id="s", kind="k")
    assert r.count() == 1


# ---------------------------------------------------------------------------
# Bootstrap → wire integration: the right backend is picked.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_picks_sqlite_when_configured(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient
    from linling_cli.bootstrap import bootstrap_bot
    from linling_cli.wire_webui import attach_bot_to_webui
    from linling_core.config import BotConfig
    from linling_webui.app import create_app
    from linling_webui.config import WebUIConfig

    # Bot.yaml with audit configured to a sibling sqlite file.
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "main.ling").write_text("ping\npong\n", encoding="utf-8")
    (tmp_path / "bot.yaml").write_text(
        """
bot_id: bot1
storage:
  kv: ":memory:"
  audit: sqlite:///audit.db
rules:
  - "rules/*.ling"
""",
        encoding="utf-8",
    )
    cfg = BotConfig.from_yaml(tmp_path / "bot.yaml")
    bot = await bootstrap_bot(cfg, base_dir=tmp_path)

    # WebUI app + auth seed so the api endpoint accepts requests.
    webui_cfg = WebUIConfig(
        auth_db_path=tmp_path / "auth.db",
        jwt_secret="t",
        login_rate_per_minute=1000,
    )
    app = create_app(webui_cfg)
    app.state.runtime.auth.upsert_user("alice", "Sesame-Open-4u", role="superadmin")
    attach_bot_to_webui(app, bot)
    try:
        # Backend selected by bootstrap should be the sqlite reader.
        assert isinstance(app.state.runtime.audit, SqliteAuditReader)

        # Drive one event so an audit row lands.
        from linling_core.events import Event, Scope, User
        from linling_core.segments import TextSegment

        await bot.bus.publish(
            Event(
                id="e1",
                platform="test",
                bot_id="bot1",
                scope=Scope(kind="group", id="g1", platform="test"),
                sender=User(id="u1", platform="test", display_name="u"),
                segments=[TextSegment(text="ping")],
            )
        )

        # File on disk and HTTP query both see the row.
        assert (tmp_path / "audit.db").is_file()
        with TestClient(app) as c:
            r = c.post(
                "/api/auth/login",
                json={"username": "alice", "password": "Sesame-Open-4u"},
            )
            token = r.json()["access"]
            r = c.get("/api/audit?limit=10", headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 200
            entries = r.json()
            assert len(entries) >= 1
            assert any(e["kind"] == "command" for e in entries)
    finally:
        await bot.stop()


@pytest.mark.asyncio
async def test_bootstrap_falls_back_to_memory_without_audit_url(tmp_path: Path) -> None:
    from linling_cli.bootstrap import bootstrap_bot
    from linling_cli.wire_webui import attach_bot_to_webui
    from linling_core.config import BotConfig
    from linling_webui.app import create_app

    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "main.ling").write_text("ping\npong\n", encoding="utf-8")
    (tmp_path / "bot.yaml").write_text(
        """
bot_id: bot1
storage:
  kv: ":memory:"
rules:
  - "rules/*.ling"
""",
        encoding="utf-8",
    )
    cfg = BotConfig.from_yaml(tmp_path / "bot.yaml")
    bot = await bootstrap_bot(cfg, base_dir=tmp_path)
    app = create_app()
    attach_bot_to_webui(app, bot)
    try:
        assert isinstance(app.state.runtime.audit, AuditReader)
        assert not isinstance(app.state.runtime.audit, SqliteAuditReader)
    finally:
        await bot.stop()
