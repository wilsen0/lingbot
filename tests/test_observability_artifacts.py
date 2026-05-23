"""Catches drift between the observability artifacts and the real metrics.

Artifacts under ``docs/observability/`` reference metric names and
label combinations. If a future refactor renames a metric, the dashboard
silently paints a blank panel and the alerts silently never fire. This
test catches that by:

1. Booting a bot with ``metrics.enabled=True``, driving one event,
   scraping ``/metrics`` via the Prometheus text-format parser, and
   extracting the set of (metric_name, label_names) actually emitted.
2. Parsing every PromQL expression out of the alerts and dashboard.
3. Verifying every metric name in those expressions matches something
   the bot actually emits. Histograms are expanded to the ``_bucket``
   suffix used by ``histogram_quantile``.

It intentionally does not validate PromQL semantics (that would need
``promtool``) — it catches the common "I renamed a metric and forgot"
class of bug, which is by far the most frequent dashboard regression.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from linling_cli.bootstrap import bootstrap_bot
from linling_cli.wire_webui import attach_bot_to_webui
from linling_core.config import BotConfig
from linling_webui.app import create_app

DOCS = Path(__file__).resolve().parent.parent / "docs" / "observability"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_METRIC_NAME_RE = re.compile(r"\b(linling_[a-z_]+)\b")


def _metric_names(text: str) -> set[str]:
    """Extract every ``linling_*`` identifier."""
    return set(_METRIC_NAME_RE.findall(text))


def _walk_json(node: Any) -> list[str]:
    """Recursively pull every string value out of the dashboard JSON."""
    if isinstance(node, dict):
        out: list[str] = []
        for v in node.values():
            out.extend(_walk_json(v))
        return out
    if isinstance(node, list):
        out = []
        for item in node:
            out.extend(_walk_json(item))
        return out
    if isinstance(node, str):
        return [node]
    return []


def _scrape_emitted_metrics(bot_text_response: str) -> set[str]:
    """Parse Prometheus exposition text → set of metric names.

    Histogram families (``foo_seconds``) implicitly expose three time
    series: ``foo_seconds_bucket`` / ``_count`` / ``_sum``. Dashboard
    expressions that call ``histogram_quantile`` reference the
    ``_bucket`` name, so we emit all three variants per histogram so
    the cross-check matches.
    """
    out: set[str] = set()
    for line in bot_text_response.splitlines():
        if not line.startswith("# TYPE "):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        name, kind = parts[2], parts[3]
        out.add(name)
        if kind == "histogram":
            out.add(f"{name}_bucket")
            out.add(f"{name}_count")
            out.add(f"{name}_sum")
    return out


async def _boot(tmp_path: Path):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "main.ling").write_text("ping\npong\n", encoding="utf-8")
    bot_yaml = tmp_path / "bot.yaml"
    bot_yaml.write_text(
        """
bot_id: bot1
storage:
  kv: ":memory:"
rules:
  - "rules/*.ling"
metrics:
  enabled: true
""",
        encoding="utf-8",
    )
    cfg = BotConfig.from_yaml(bot_yaml)
    return await bootstrap_bot(cfg, base_dir=tmp_path)


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_and_alerts_reference_only_real_metrics(tmp_path: Path):
    dashboard_path = DOCS / "grafana-dashboard.json"
    alerts_path = DOCS / "alerts.yml"
    assert dashboard_path.is_file(), "dashboard file missing"
    assert alerts_path.is_file(), "alerts file missing"

    # --- 1. What does the bot actually emit? ---
    bot = await _boot(tmp_path)
    app = create_app()
    try:
        attach_bot_to_webui(app, bot)
        from linling_core.events import Event, Scope, User
        from linling_core.segments import TextSegment

        await bot.bus.publish(
            Event(
                id="e1",
                platform="test",
                bot_id="bot1",
                scope=Scope(kind="group", id="g1", platform="test"),
                sender=User(id="u1", platform="test", display_name="u"),
                segments=[TextSegment(text="ping")],
            )
        )
        with TestClient(app) as client:
            r = client.get("/metrics")
            assert r.status_code == 200
            emitted = _scrape_emitted_metrics(r.text)
    finally:
        await bot.stop()

    assert emitted, "no metrics exposed"

    # --- 2. What do the artifacts reference? ---
    alerts_yaml = yaml.safe_load(alerts_path.read_text(encoding="utf-8"))
    alert_expressions = []
    for group in alerts_yaml.get("groups", []):
        for rule in group.get("rules", []):
            expr = rule.get("expr", "")
            alert_expressions.append(expr)
    referenced_from_alerts = set().union(*(_metric_names(e) for e in alert_expressions))

    dashboard_json = json.loads(dashboard_path.read_text(encoding="utf-8"))
    referenced_from_dashboard: set[str] = set()
    for text in _walk_json(dashboard_json):
        referenced_from_dashboard |= _metric_names(text)

    # --- 3. Cross-check ---
    unknown_alerts = referenced_from_alerts - emitted
    unknown_dashboard = referenced_from_dashboard - emitted
    assert not unknown_alerts, (
        f"alerts.yml references metrics not emitted by the bot: {sorted(unknown_alerts)}"
    )
    assert not unknown_dashboard, (
        f"grafana-dashboard.json references metrics not emitted by the bot: "
        f"{sorted(unknown_dashboard)}"
    )


def test_alerts_yaml_valid():
    data = yaml.safe_load((DOCS / "alerts.yml").read_text(encoding="utf-8"))
    assert data.get("groups"), "no alert groups"
    for group in data["groups"]:
        assert "name" in group
        for rule in group.get("rules", []):
            assert "alert" in rule and "expr" in rule
            assert "labels" in rule and "severity" in rule["labels"]


def test_dashboard_json_valid_and_has_panels():
    data = json.loads((DOCS / "grafana-dashboard.json").read_text(encoding="utf-8"))
    panels = data.get("panels", [])
    assert len(panels) >= 10
    # Every non-row panel has at least one target with an expression.
    for p in panels:
        if p.get("type") == "row":
            continue
        targets = p.get("targets", [])
        assert targets, f"panel {p.get('title')!r} has no targets"
        for t in targets:
            assert t.get("expr"), f"panel {p.get('title')!r} has an empty expr"
