"""WUI-C8 / log hygiene — error bodies must not echo secrets."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from linling_webui.app import create_app
from linling_webui.config import WebUIConfig


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    config = WebUIConfig(
        auth_db_path=tmp_path / "auth.db",
        jwt_secret="super-secret-do-not-leak",
        login_rate_per_minute=10_000,
    )
    app = create_app(config)
    app.state.runtime.auth.upsert_user("alice", "Sesame-Open-4u")
    with TestClient(app) as c:
        yield c


def test_login_error_does_not_echo_password(client: TestClient) -> None:
    r = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "NotThePassword-Secret-XYZ"},
    )
    assert r.status_code == 401
    assert "NotThePassword-Secret-XYZ" not in r.text
    assert "Sesame-Open-4u" not in r.text


def test_profile_401_does_not_echo_token(client: TestClient) -> None:
    fake_jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.AAAA.SECRET-PAYLOAD-XYZ"
    r = client.get("/api/profile", headers={"Authorization": f"Bearer {fake_jwt}"})
    assert r.status_code == 401
    assert fake_jwt not in r.text
    assert "SECRET-PAYLOAD-XYZ" not in r.text


def test_settings_does_not_echo_jwt_secret(client: TestClient) -> None:
    login = client.post(
        "/api/auth/login", json={"username": "alice", "password": "Sesame-Open-4u"}
    ).json()
    r = client.get("/api/settings", headers={"Authorization": f"Bearer {login['access']}"})
    assert r.status_code == 200
    assert "super-secret-do-not-leak" not in r.text
