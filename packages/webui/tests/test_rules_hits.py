"""/api/rules and /api/rules/:name/hits."""

from __future__ import annotations

from linling_webui.audit_reader import AuditReader


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_rules_aggregate_from_audit(app_client) -> None:
    client, _, token = app_client
    audit = AuditReader()
    client.app.state.runtime.audit = audit
    audit.append(
        bot_id="susu_main",
        user_id="u1",
        scope_id="g1",
        kind="handler_dispatch",
        outcome="ok",
        latency_ms=10.0,
        payload={"handler": "打卡", "trigger": "^打卡$"},
    )
    audit.append(
        bot_id="susu_main",
        user_id="u2",
        scope_id="g1",
        kind="handler_dispatch",
        outcome="err",
        latency_ms=20.0,
        payload={"handler": "打卡", "trigger": "^打卡$", "error": "boom"},
    )

    r = client.get("/api/rules", headers=_hdr(token))
    assert r.status_code == 200
    rules = r.json()
    assert rules[0]["name"] == "打卡"
    assert rules[0]["hits_today"] == 2
    assert abs(rules[0]["avg_latency_ms"] - 15.0) < 0.01
    assert rules[0]["last_error"] == "boom"


def test_rule_hits_filter(app_client) -> None:
    client, _, token = app_client
    audit = AuditReader()
    client.app.state.runtime.audit = audit
    for i in range(3):
        audit.append(
            bot_id="susu_main",
            user_id=f"u{i}",
            scope_id="g1",
            kind="handler_dispatch",
            outcome="ok",
            payload={"handler": "打卡", "matched": {"_": "打卡"}, "event_id": f"e{i}"},
        )
    audit.append(
        bot_id="susu_main",
        user_id="u9",
        scope_id="g1",
        kind="handler_dispatch",
        outcome="ok",
        payload={"handler": "灵玉"},
    )

    r = client.get("/api/rules/打卡/hits", headers=_hdr(token))
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 3
    assert all(it["event_id"].startswith("e") for it in items)
