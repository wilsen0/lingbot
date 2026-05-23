"""Catch backend schema drift against the committed TS snapshot.

The frontend depends on ``src/api/openapi.snapshot.json`` to generate
``src/api/openapi.types.ts``. Whenever the backend's schema changes, the
snapshot must be re-baselined and the TS types regenerated — otherwise
``pnpm typecheck`` keeps shipping with stale types until something
breaks at runtime.

This test enforces that on every commit by:

1. Building the live FastAPI app the same way the WebUI server does.
2. Pulling its ``/api/openapi.json`` document.
3. Comparing against the snapshot in the frontend tree, ignoring
   purely cosmetic fields (``title`` / ``description`` / ``examples``)
   so doc-only edits don't bounce CI.

When this test fails, the fix is one command:

    pnpm --filter @linling/webui-frontend api:update
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from linling_webui.app import create_app
from linling_webui.config import WebUIConfig

SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[1] / "frontend" / "src" / "api" / "openapi.snapshot.json"
)

# Fields ignored for drift detection — tweaking docstrings shouldn't
# require regenerating the TS bundle.
_DOC_FIELDS = {"title", "description", "examples"}


def _strip_docs(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_docs(v) for k, v in node.items() if k not in _DOC_FIELDS}
    if isinstance(node, list):
        return [_strip_docs(v) for v in node]
    return node


def _shape(doc: dict[str, Any]) -> dict[str, Any]:
    """Reduce an OpenAPI doc to the fields the TS generator depends on."""
    out: dict[str, Any] = {"paths": {}, "schemas": {}}
    for path, methods in doc.get("paths", {}).items():
        out["paths"][path] = {}
        for method, op in methods.items():
            if not isinstance(op, dict) or method.startswith("x-") or method == "parameters":
                continue
            out["paths"][path][method] = {
                "params": sorted(
                    f"{p['in']}:{p['name']}:{p.get('required', False)}"
                    for p in op.get("parameters", [])
                ),
                "body": _strip_docs(
                    op.get("requestBody", {})
                    .get("content", {})
                    .get("application/json", {})
                    .get("schema")
                ),
                "responses": {
                    code: _strip_docs(
                        r.get("content", {}).get("application/json", {}).get("schema")
                    )
                    for code, r in op.get("responses", {}).items()
                },
            }
    for name, schema in doc.get("components", {}).get("schemas", {}).items():
        out["schemas"][name] = _strip_docs(schema)
    return out


@pytest.fixture(scope="module")
def live_openapi() -> dict[str, Any]:
    config = WebUIConfig(jwt_secret="test", login_rate_per_minute=1000)
    app = create_app(config)
    schema = app.openapi()
    assert isinstance(schema, dict)
    return schema


def test_snapshot_exists() -> None:
    assert SNAPSHOT_PATH.exists(), (
        f"snapshot missing at {SNAPSHOT_PATH}. "
        f"Run `pnpm --filter @linling/webui-frontend api:update`."
    )


def test_openapi_matches_snapshot(live_openapi: dict[str, Any]) -> None:
    snap = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))

    live_shape = _shape(live_openapi)
    snap_shape = _shape(snap)

    if live_shape != snap_shape:
        diffs = _summarize_diff(snap_shape, live_shape)
        pytest.fail(
            "OpenAPI drifted from the committed snapshot. "
            "Either revert the schema change or run "
            "`pnpm --filter @linling/webui-frontend api:update` "
            "to refresh the snapshot + TS types.\n\nFirst few changes:\n" + "\n".join(diffs[:30])
        )


def _summarize_diff(snap: Any, live: Any, path: str = "") -> list[str]:
    """Tiny recursive differ matching ``check-api.mjs``."""
    out: list[str] = []
    snap_is_obj = isinstance(snap, dict)
    live_is_obj = isinstance(live, dict)
    if not snap_is_obj or not live_is_obj:
        if snap != live:
            out.append(f"{path or '(root)'}: {snap!r} → {live!r}")
        return out
    keys = set(snap) | set(live)
    for k in sorted(keys):
        child = f"{path}.{k}" if path else k
        if k not in snap:
            out.append(f"+ {child}")
        elif k not in live:
            out.append(f"- {child}")
        else:
            out.extend(_summarize_diff(snap[k], live[k], child))
    return out
