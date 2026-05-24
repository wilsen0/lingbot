"""/api/kv* — read/write/delete/rank with If-Match optimistic concurrency."""

from __future__ import annotations

import asyncio


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_read_returns_404_when_absent(app_client) -> None:
    client, _, token = app_client
    r = client.get("/api/kv/s1/f1/k1", headers=_auth(token))
    assert r.status_code == 404


def test_write_then_read_roundtrip(app_client) -> None:
    """WUI-C4: write-after-read consistency."""
    client, _, token = app_client
    w = client.patch("/api/kv/s1/f1/k1", json={"value": "3"}, headers=_auth(token))
    assert w.status_code == 200
    body = w.json()
    assert body["value"] == "3"
    assert body["key"] == "k1"

    r = client.get("/api/kv/s1/f1/k1", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["value"] == "3"


def test_etag_optimistic_concurrency(app_client) -> None:
    """WUI-C5: a stale If-Match must 412."""
    client, _, token = app_client
    # Seed a value.
    client.patch("/api/kv/s/f/k", json={"value": "1"}, headers=_auth(token))

    # Read once to pick up the ETag.
    r1 = client.get("/api/kv/s/f/k", headers=_auth(token))
    etag = r1.headers.get("etag")
    assert etag is not None

    # Write by a second caller (no If-Match).
    client.patch("/api/kv/s/f/k", json={"value": "2"}, headers=_auth(token))

    # Original caller tries to write with stale ETag → 412.
    r2 = client.patch(
        "/api/kv/s/f/k",
        json={"value": "3"},
        headers={**_auth(token), "If-Match": etag},
    )
    assert r2.status_code == 412


def test_delete_then_read_404(app_client) -> None:
    client, _, token = app_client
    client.patch("/api/kv/s/f/k", json={"value": "1"}, headers=_auth(token))
    d = client.delete("/api/kv/s/f/k", headers=_auth(token))
    assert d.status_code == 204
    r = client.get("/api/kv/s/f/k", headers=_auth(token))
    assert r.status_code == 404


def test_list_keys_paginates_and_filters(app_client) -> None:
    client, _, token = app_client
    for i in range(5):
        client.patch(f"/api/kv/s2/inv/key_{i}", json={"value": str(i)}, headers=_auth(token))
    client.patch("/api/kv/s2/inv/zz", json={"value": "9"}, headers=_auth(token))

    r = client.get("/api/kv/s2/inv?prefix=key_&limit=3", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert [it["key"] for it in body["items"]] == ["key_0", "key_1", "key_2"]
    assert body["next_cursor"] == "key_2"


def test_rank_desc_by_numeric_value(app_client) -> None:
    client, _, token = app_client
    for k, v in [("a", "3"), ("b", "10"), ("c", "7")]:
        client.patch(f"/api/kv/rank_s/rank_f/{k}", json={"value": v}, headers=_auth(token))
    r = client.get("/api/kv/rank_s/rank_f/rank?order=desc&top=3", headers=_auth(token))
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert [(row["key"], int(row["value"])) for row in rows] == [("b", 10), ("c", 7), ("a", 3)]


def test_read_key_query_handles_slash_scope(app_client) -> None:
    client, _, token = app_client
    store = client.app.state.runtime.kv_stores["susu_main"]
    asyncio.run(store.write("啊/灵玉系", "灵玉", "e2e", "128"))
    r = client.get(
        "/api/kv/row?scope=%E5%95%8A%2F%E7%81%B5%E7%8E%89%E7%B3%BB&file=%E7%81%B5%E7%8E%89&key=e2e",
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["value"] == "128"


def test_public_leaderboard_hides_keys(app_client) -> None:
    client, _, token = app_client
    store = client.app.state.runtime.kv_stores["susu_main"]
    asyncio.run(store.write("啊/灵玉系", "灵玉", "alice-id", "3"))
    asyncio.run(store.write("啊/灵玉系", "灵玉", "bob-id", "10"))
    asyncio.run(store.write("啊/灵玉系", "灵玉", "carol-id", "7"))

    r = client.get(
        "/api/kv/leaderboard?scope=%E5%95%8A%2F%E7%81%B5%E7%8E%89%E7%B3%BB&file=%E7%81%B5%E7%8E%89&order=desc&top=2",
        headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["formatted"] == ""
    assert [(row["rank"], row["key"], int(row["value"])) for row in body["rows"]] == [
        (1, "", 10),
        (2, "", 7),
    ]
    assert "bob-id" not in r.text
    assert "carol-id" not in r.text


def test_requires_auth(app_client) -> None:
    client, _, _token = app_client
    r = client.get("/api/kv/s/f/k")
    assert r.status_code == 401



def test_readonly_user_cannot_write_kv(tmp_path) -> None:
    """A readonly token must be rejected by ``PATCH /api/kv/...``.

    Regression: KV writes previously gated only on ``require_auth``,
    so a viewer token could mutate state. They are now gated on
    ``bot_admin`` role.
    """
    from fastapi.testclient import TestClient
    from linling_core.storage.sqlite_kv import SqliteKVStore
    from linling_webui.app import create_app
    from linling_webui.config import WebUIConfig
    from linling_webui.wire import wire_bot

    config = WebUIConfig(
        auth_db_path=tmp_path / "auth.db",
        jwt_secret="test-secret",
        access_token_ttl_s=60,
        refresh_token_ttl_s=300,
        login_rate_per_minute=1000,
    )
    app = create_app(config)
    app.state.runtime.auth.upsert_user(
        "viewer", "Viewer-Pwd-9!aB", role="readonly", bots=["b1"]
    )
    kv = SqliteKVStore(bot_id="b1", db_path=str(tmp_path / "kv.db"))
    wire_bot(app, bot_id="b1", platform="onebot", name="b1", kv=kv)

    with TestClient(app) as c:
        token = c.post(
            "/api/auth/login",
            json={"username": "viewer", "password": "Viewer-Pwd-9!aB"},
        ).json()["access"]
        # Reads succeed (or 404 — that's fine; the gate is the role).
        r = c.get("/api/kv?bot_id=b1", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        # Writes are forbidden.
        w = c.patch(
            "/api/kv/s/f/k?bot_id=b1",
            json={"value": "1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert w.status_code == 403
        d = c.delete(
            "/api/kv/s/f/k?bot_id=b1",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert d.status_code == 403
