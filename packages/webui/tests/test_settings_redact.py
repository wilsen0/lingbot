"""/api/settings redacts secret-looking keys (WUI-C8)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from linling_webui.app import create_app
from linling_webui.config import WebUIConfig


def test_jwt_secret_is_masked(tmp_path: Path) -> None:
    config = WebUIConfig(
        auth_db_path=tmp_path / "auth.db",
        jwt_secret="super-secret-do-not-leak",
        access_token_ttl_s=60,
        refresh_token_ttl_s=300,
        login_rate_per_minute=1000,
    )
    app = create_app(config)
    app.state.runtime.auth.upsert_user("alice", "Sesame-Open-4u")
    with TestClient(app) as c:
        token = c.post(
            "/api/auth/login", json={"username": "alice", "password": "Sesame-Open-4u"}
        ).json()["access"]
        r = c.get("/api/settings", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        body = r.json()
        assert body["jwt_secret"] == "***"
        assert "super-secret-do-not-leak" not in r.text
