"""Smoke test: create_app returns a FastAPI, /api/health 200."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from linling_webui.app import create_app
from linling_webui.config import WebUIConfig
from linling_webui.version import __version__


def test_create_app_returns_fastapi() -> None:
    app = create_app(WebUIConfig())
    assert isinstance(app, FastAPI)


def test_health_endpoint_returns_ok() -> None:
    app = create_app(WebUIConfig())
    with TestClient(app) as client:
        resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert "time" in body
    assert isinstance(body.get("bots"), list)


def test_config_from_empty_yaml_section() -> None:
    cfg = WebUIConfig.from_bot_yaml_section(None)
    assert cfg.port == 8787
    assert cfg.host == "127.0.0.1"


def test_config_from_yaml_section_with_overrides() -> None:
    cfg = WebUIConfig.from_bot_yaml_section({"port": 9090, "host": "0.0.0.0"})
    assert cfg.port == 9090
    assert cfg.host == "0.0.0.0"
