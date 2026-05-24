"""Send a remote ``http(s)://`` image to verify NapCat fetches it.

The 漂流瓶 / 接扔瓶子 DSL stores QQ-CDN URLs received from inbound
``%IMG0%`` and replays them later. The OneBot adapter does NOT rewrite
those (only ``@pic:`` shorthands are touched), so this is the
unmodified path. We confirm that:

* a generic ``https://`` image URL works,
* a malformed / unreachable ``https://`` produces a NapCat-side error
  (so the failure mode is explicit, not silent).
"""

from __future__ import annotations

import asyncio
import json
import sys

import websockets

URL = "ws://127.0.0.1:3001"
TOKEN = "linling-secret-2026"


async def main(user_id: int, image_url: str, label: str) -> None:
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
                            {"type": "text", "data": {"text": f"[{label}]"}},
                            {"type": "image", "data": {"file": image_url}},
                        ],
                    },
                    "echo": "url-1",
                }
            )
        )
        async for raw in ws:
            try:
                m = json.loads(raw)
            except Exception:
                continue
            if m.get("echo") == "url-1":
                print(json.dumps(m, ensure_ascii=False, default=str)[:1200])
                return


asyncio.run(asyncio.wait_for(main(int(sys.argv[1]), sys.argv[2], sys.argv[3]), timeout=20))
