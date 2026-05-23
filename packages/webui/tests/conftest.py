"""Shared fixtures: an app with one seeded user + wired KV."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from linling_core.storage.sqlite_kv import SqliteKVStore
from linling_webui.app import create_app
from linling_webui.config import WebUIConfig
from linling_webui.state import BotInfo
from linling_webui.wire import wire_bot


@pytest.fixture
def app_client(tmp_path: Path) -> Iterator[tuple[TestClient, WebUIConfig, str]]:
    """Yield (TestClient, config, access_token) with:
    - user 'alice' (superadmin)
    - bot 'susu_main' wired with a fresh in-memory SqliteKVStore
    """
    config = WebUIConfig(
        auth_db_path=tmp_path / "auth.db",
        jwt_secret="test-secret",
        access_token_ttl_s=60,
        refresh_token_ttl_s=300,
        login_rate_per_minute=1000,  # don't interfere with tests
    )
    app = create_app(config)
    app.state.runtime.auth.upsert_user("alice", "Sesame-Open-4u", role="superadmin")
    kv = SqliteKVStore(bot_id="susu_main", db_path=str(tmp_path / "kv.db"))
    wire_bot(app, bot_id="susu_main", platform="onebot", name="涂山苏苏", kv=kv)

    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": "alice", "password": "Sesame-Open-4u"})
        token = r.json()["access"]
        yield c, config, token


@pytest.fixture
def two_tenant_client(tmp_path: Path) -> Iterator[tuple[TestClient, str, str]]:
    """Client with two users: alice (bot_admin, [susu_main]) & bob (bot_admin, [other])."""
    config = WebUIConfig(
        auth_db_path=tmp_path / "auth.db",
        jwt_secret="test-secret",
        access_token_ttl_s=60,
        refresh_token_ttl_s=300,
        login_rate_per_minute=1000,
    )
    app = create_app(config)
    app.state.runtime.auth.upsert_user(
        "alice", "Sesame-Open-4u", role="bot_admin", bots=["susu_main"]
    )
    app.state.runtime.auth.upsert_user("bob", "Opensaysame-4u", role="bot_admin", bots=["other"])
    # Register two bots so we can verify isolation
    app.state.runtime.register_bot(BotInfo(id="susu_main", platform="onebot", name="涂山苏苏"))
    app.state.runtime.register_bot(BotInfo(id="other", platform="onebot", name="别家"))

    with TestClient(app) as c:
        a = c.post(
            "/api/auth/login", json={"username": "alice", "password": "Sesame-Open-4u"}
        ).json()
        b = c.post("/api/auth/login", json={"username": "bob", "password": "Opensaysame-4u"}).json()
        yield c, a["access"], b["access"]
