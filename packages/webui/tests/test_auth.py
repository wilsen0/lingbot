"""Auth endpoints — login / refresh / logout / profile / rate-limit."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from linling_webui.app import create_app
from linling_webui.config import WebUIConfig


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    config = WebUIConfig(
        auth_db_path=tmp_path / "auth.db",
        jwt_secret="test-secret-for-unit-tests",
        access_token_ttl_s=60,
        refresh_token_ttl_s=300,
    )
    app = create_app(config)
    # Seed one user
    app.state.runtime.auth.upsert_user("alice", "Sesame-Open-4u", role="superadmin")
    with TestClient(app) as c:
        yield c


def test_login_success_returns_tokens(client: TestClient) -> None:
    r = client.post("/api/auth/login", json={"username": "alice", "password": "Sesame-Open-4u"})
    assert r.status_code == 200
    body = r.json()
    assert body["access"]
    assert body["refresh"]
    assert body["access_expires_at"] > int(time.time())


def test_login_wrong_password_is_401(client: TestClient) -> None:
    r = client.post("/api/auth/login", json={"username": "alice", "password": "nope"})
    assert r.status_code == 401


def test_login_unknown_user_is_401(client: TestClient) -> None:
    r = client.post("/api/auth/login", json={"username": "mallory", "password": "x"})
    assert r.status_code == 401


def test_profile_requires_bearer(client: TestClient) -> None:
    r = client.get("/api/profile")
    assert r.status_code == 401


def test_profile_with_access_token(client: TestClient) -> None:
    login = client.post(
        "/api/auth/login", json={"username": "alice", "password": "Sesame-Open-4u"}
    ).json()
    r = client.get("/api/profile", headers={"Authorization": f"Bearer {login['access']}"})
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "alice"
    assert body["role"] == "superadmin"
    assert body["bots"] == []


def test_refresh_rotates_tokens(client: TestClient) -> None:
    login = client.post(
        "/api/auth/login", json={"username": "alice", "password": "Sesame-Open-4u"}
    ).json()
    r1 = client.post("/api/auth/refresh", json={"refresh": login["refresh"]})
    assert r1.status_code == 200
    new = r1.json()
    assert new["refresh"] != login["refresh"]

    # The old refresh must be revoked.
    r2 = client.post("/api/auth/refresh", json={"refresh": login["refresh"]})
    assert r2.status_code == 401


def test_logout_revokes_refresh(client: TestClient) -> None:
    login = client.post(
        "/api/auth/login", json={"username": "alice", "password": "Sesame-Open-4u"}
    ).json()
    r = client.post("/api/auth/logout", json={"refresh": login["refresh"]})
    assert r.status_code == 204
    r2 = client.post("/api/auth/refresh", json={"refresh": login["refresh"]})
    assert r2.status_code == 401


def test_profile_rejects_refresh_as_access(client: TestClient) -> None:
    """A refresh JWT must not be accepted on authenticated endpoints."""
    login = client.post(
        "/api/auth/login", json={"username": "alice", "password": "Sesame-Open-4u"}
    ).json()
    r = client.get("/api/profile", headers={"Authorization": f"Bearer {login['refresh']}"})
    assert r.status_code == 401


def test_login_rate_limit_kicks_in(tmp_path: Path) -> None:
    config = WebUIConfig(
        auth_db_path=tmp_path / "auth.db",
        jwt_secret="test-secret",
        login_rate_per_minute=3,
    )
    app = create_app(config)
    app.state.runtime.auth.upsert_user("alice", "Sesame-Open-4u")
    with TestClient(app) as c:
        for _ in range(3):
            c.post("/api/auth/login", json={"username": "alice", "password": "wrong"})
        r = c.post("/api/auth/login", json={"username": "alice", "password": "Sesame-Open-4u"})
        assert r.status_code == 429
