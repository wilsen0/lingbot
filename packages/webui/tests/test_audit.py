"""Audit search and CSV export."""

from __future__ import annotations

from linling_webui.audit_reader import AuditReader


def test_search_filters_by_bot_and_kind(app_client) -> None:
    client, _, token = app_client
    audit = AuditReader()
    client.app.state.runtime.audit = audit
    audit.append(bot_id="susu_main", user_id="u1", scope_id="g1", kind="handler_dispatch")
    audit.append(bot_id="susu_main", user_id="u2", scope_id="g1", kind="tool_call")
    audit.append(bot_id="other", user_id="u3", scope_id="g2", kind="handler_dispatch")

    r = client.get("/api/audit?kind=handler_dispatch", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    items = r.json()
    assert [it["kind"] for it in items] == ["handler_dispatch", "handler_dispatch"]


def test_csv_export(app_client) -> None:
    client, _, token = app_client
    audit = AuditReader()
    client.app.state.runtime.audit = audit
    audit.append(bot_id="susu_main", user_id="u1", scope_id="g1", kind="login")

    r = client.get("/api/audit.csv", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    text = r.text
    assert text.startswith("id,time,bot_id,user_id,scope_id,kind,outcome,latency_ms")
    assert "susu_main" in text


def test_audit_search_respects_jwt_bots_visibility(two_tenant_client) -> None:
    """A bot_admin scoped to one bot must not see other bots' audit rows.

    Regression: previously, ``GET /api/audit?bot_id=other`` ignored
    ``jwt.bots`` and let alice fetch audit rows from bots she shouldn't
    see. The fix routes the explicit ``bot_id`` filter through
    ``verify_bot_visibility`` before delegating to the audit reader.
    """
    from linling_webui.audit_reader import AuditReader

    client, alice_token, _bob_token = two_tenant_client
    audit = AuditReader()
    client.app.state.runtime.audit = audit
    audit.append(bot_id="susu_main", user_id="u1", scope_id="g1", kind="handler_dispatch")
    audit.append(bot_id="other", user_id="u2", scope_id="g2", kind="handler_dispatch")

    # Alice can see her own bot's audit fine.
    r = client.get(
        "/api/audit?bot_id=susu_main", headers={"Authorization": f"Bearer {alice_token}"}
    )
    assert r.status_code == 200
    assert all(row["bot_id"] == "susu_main" for row in r.json())

    # Alice cannot see ``other`` bot's audit; treated as nonexistent.
    r = client.get("/api/audit?bot_id=other", headers={"Authorization": f"Bearer {alice_token}"})
    assert r.status_code == 404

    # CSV export honours the same rule.
    r = client.get(
        "/api/audit.csv?bot_id=other", headers={"Authorization": f"Bearer {alice_token}"}
    )
    assert r.status_code == 404


def test_audit_search_default_filters_to_visible_bots(two_tenant_client) -> None:
    """Without ``?bot_id``, alice should only see her visible bots' rows."""
    from linling_webui.audit_reader import AuditReader

    client, alice_token, _ = two_tenant_client
    audit = AuditReader()
    client.app.state.runtime.audit = audit
    audit.append(bot_id="susu_main", user_id="u1", scope_id="g1", kind="login")
    audit.append(bot_id="other", user_id="u2", scope_id="g2", kind="login")

    r = client.get("/api/audit", headers={"Authorization": f"Bearer {alice_token}"})
    assert r.status_code == 200
    bot_ids = {row["bot_id"] for row in r.json()}
    assert bot_ids == {"susu_main"}
