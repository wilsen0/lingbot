"""Send an image via ``file://`` to confirm the legacy adapter path
(bind-mount-dependent) actually fails inside the LLBot container.

Reproduces what OneBotAdapter emits *before* the base64 rewrite lands.
If LLBot returns ``status=failed`` or returns ok but no image arrives
on the receiving QQ, the host filesystem isn't reachable from the
container — exactly the bug we're fixing.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import websockets

URL = "ws://127.0.0.1:3001"
TOKEN = "linling-secret-2026"
ASSET_ROOT = Path(__file__).resolve().parents[1] / "bot" / "assets" / "picture"


async def main(user_id: int, sprite: str) -> None:
    target = ASSET_ROOT / sprite
    if not target.is_file():
        raise SystemExit(f"sprite not found: {target}")
    file_url = f"file://{target}"
    print(f"file_url={file_url}")

    async with websockets.connect(
        URL, additional_headers={"Authorization": f"Bearer {TOKEN}"}
    ) as ws:
        await ws.send(
            json.dumps(
                {
                    "action": "send_msg",
                    "params": {
                        "message_type": "private",
                        "user_id": user_id,
                        "message": [
                            {"type": "text", "data": {"text": "[file:// test]"}},
                            {"type": "image", "data": {"file": file_url}},
                        ],
                    },
                    "echo": "img-2",
                }
            )
        )
        async for raw in ws:
            try:
                m = json.loads(raw)
            except Exception:
                continue
            if m.get("echo") == "img-2":
                print(json.dumps(m, ensure_ascii=False, default=str)[:1200])
                return


asyncio.run(asyncio.wait_for(main(int(sys.argv[1]), sys.argv[2]), timeout=15))
