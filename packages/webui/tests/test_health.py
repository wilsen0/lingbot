"""Health endpoint returns version and CSP header."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from linling_webui.app import create_app
from linling_webui.config import WebUIConfig
from linling_webui.state import BotInfo


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    config = WebUIConfig(auth_db_path=tmp_path / "auth.db", jwt_secret="t")
    app = create_app(config)
    app.state.runtime.register_bot(BotInfo(id="susu_main", platform="onebot", name="涂山苏苏"))
    with TestClient(app) as c:
        yield c


def test_health_lists_known_bots(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert len(body["bots"]) == 1
    assert body["bots"][0]["id"] == "susu_main"


def test_csp_header_present(client: TestClient) -> None:
    r = client.get("/api/health")
    csp = r.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert r.headers.get("x-content-type-options") == "nosniff"
