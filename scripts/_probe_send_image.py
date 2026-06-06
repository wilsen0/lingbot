"""Send an inline base64 image to verify LLBot accepts the new payload.

Mirrors what the OneBot adapter will send after the ``@pic:`` →
``base64://`` rewrite lands. Reads a real sprite off disk, encodes it,
ships it through LLBot as a private message, and prints whatever
LLBot reports back.

Usage::

    uv run python scripts/_probe_send_image.py <user_id> <sprite_name>

``<sprite_name>`` is resolved against ``bot/assets/picture`` exactly
the way the adapter does (e.g. ``大飞龙.jpg`` or ``苏苏摸头.svg``).
"""

from __future__ import annotations

import asyncio
import base64
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
    encoded = "base64://" + base64.b64encode(target.read_bytes()).decode("ascii")
    print(f"sprite={target.name} size={target.stat().st_size} b64_len={len(encoded)}")

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
                            {"type": "text", "data": {"text": "[base64 inline test]"}},
                            {"type": "image", "data": {"file": encoded}},
                        ],
                    },
                    "echo": "img-1",
                }
            )
        )
        async for raw in ws:
            try:
                m = json.loads(raw)
            except Exception:
                continue
            if m.get("echo") == "img-1":
                print(json.dumps(m, ensure_ascii=False, default=str)[:1200])
                return


asyncio.run(asyncio.wait_for(main(int(sys.argv[1]), sys.argv[2]), timeout=15))
