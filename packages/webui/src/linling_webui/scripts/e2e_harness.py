"""E2E harness — spin up a self-contained WebUI for Playwright tests.

Seeds a known user (`e2e` / `Op3n-4u!`) and mounts an in-memory SQLite KV
store with a few rows so tests don't depend on examples data.
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from linling_core.events import Event, Scope, User
from linling_core.segments import TextSegment
from linling_core.storage.sqlite_kv import SqliteKVStore

from linling_webui.app import create_app
from linling_webui.audit_reader import AuditReader
from linling_webui.config import WebUIConfig
from linling_webui.wire import wire_bot


def _fake_event(eid: str, bot_id: str = "e2e_bot") -> Event:
    return Event(
        id=eid,
        platform="test",
        bot_id=bot_id,
        scope=Scope(kind="group", id="g1", platform="test"),
        sender=User(id="u1", platform="test"),
        time=datetime.now(UTC),
        kind="message",
        segments=[TextSegment(text=f"e2e 事件 {eid}")],
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="linling-webui-e2e-"))
    auth_db = tmp / "auth.db"
    kv_db = tmp / "kv.db"

    config = WebUIConfig(
        host=args.host,
        port=args.port,
        auth_db_path=auth_db,
        jwt_secret="e2e-secret",
        login_rate_per_minute=1000,
        write_rate_per_minute=1000,
    )
    app = create_app(config)

    # Seed user.
    app.state.runtime.auth.upsert_user("e2e", "Op3n-4u!", role="superadmin")

    # Seed a bot + KV.
    kv = SqliteKVStore(bot_id="e2e_bot", db_path=str(kv_db))
    wire_bot(app, bot_id="e2e_bot", platform="cli", name="e2e 机", kv=kv)

    async def _seed() -> None:
        for k, v in [("alice", "10"), ("bob", "7"), ("carol", "3"), ("dave", "21")]:
            await kv.write("榜", "分数", k, v)
        await kv.write("啊/灵玉系", "灵玉", "e2e", "128")
        await kv.write("啊/禁言系", "妖力", "e2e", "64")
        await kv.write("啊/节日系", "节日礼包", "e2e", "2")
        await kv.write("啊/活动系", "玫瑰花", "e2e", "3")
        await kv.write("啊/活动系", "锦囊", "e2e", "1")
        await kv.write("休闲系/珍品", "气球", "e2e", "2")
        await kv.write("休闲系/珍品", "小豆芽", "e2e", "1")

    asyncio.run(_seed())

    # Seed audit.
    audit = AuditReader()
    for i in range(3):
        audit.append(
            bot_id="e2e_bot",
            user_id=f"u{i}",
            scope_id="g1",
            kind="handler_dispatch",
            outcome="ok",
            latency_ms=float(12 + i),
            payload={"handler": "打卡", "trigger": "^打卡$", "event_id": f"e{i}"},
        )
    app.state.runtime.audit = audit

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
