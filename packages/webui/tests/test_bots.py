"""/api/bots listing enforces per-user visibility (WUI-C2)."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_alice_sees_only_her_bot(two_tenant_client: tuple[TestClient, str, str]) -> None:
    client, alice, _bob = two_tenant_client
    r = client.get("/api/bots", headers={"Authorization": f"Bearer {alice}"})
    assert r.status_code == 200
    items = r.json()
    assert [b["id"] for b in items] == ["susu_main"]


def test_bob_sees_only_his_bot(two_tenant_client: tuple[TestClient, str, str]) -> None:
    client, _alice, bob = two_tenant_client
    r = client.get("/api/bots", headers={"Authorization": f"Bearer {bob}"})
    assert r.status_code == 200
    items = r.json()
    assert [b["id"] for b in items] == ["other"]


def test_cross_tenant_access_is_404(two_tenant_client: tuple[TestClient, str, str]) -> None:
    client, alice, _bob = two_tenant_client
    # Hot-reload requires visibility on bot_id. Alice asking about 'other' should 404.
    r = client.post("/api/bots/other/hot-reload", headers={"Authorization": f"Bearer {alice}"})
    assert r.status_code == 404
