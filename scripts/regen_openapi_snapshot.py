"""Regenerate ``packages/webui/frontend/src/api/openapi.snapshot.json``
from the in-process FastAPI app.

Equivalent to ``pnpm --filter @linling/webui-frontend run check:api -- --update``
without needing a live bot. Useful when developing offline; CI still
runs the JS path against a started backend.

Usage:
    uv run python scripts/regen_openapi_snapshot.py
"""

from __future__ import annotations

import json
from pathlib import Path

from linling_webui.app import create_app
from linling_webui.config import WebUIConfig

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "packages" / "webui" / "frontend" / "src" / "api" / "openapi.snapshot.json"


def main() -> None:
    config = WebUIConfig(jwt_secret="snapshot", login_rate_per_minute=1000)
    app = create_app(config)
    schema = app.openapi()
    SNAPSHOT.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"✓ snapshot written: {SNAPSHOT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
