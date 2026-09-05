"""Mimic a 漂流瓶/捡瓶子 outbound: text + local sprite + remote URL.

Reproduces what the rules emit when a user picks up a bottle:

    捡到了一个瓶子
    ±img=@pic:捡到一个瓶子.svg±        (local sprite — base64 inline)
    ±img=<original sender's image URL>±  (remote QQ CDN — passthrough)

We craft both image segments by hand here (the local one as base64, the
remote one as a real https URL) and ship them together to confirm
LLBot handles a mixed-shape message.
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
LOCAL_SPRITE = ASSET_ROOT / "捡到一个瓶子.svg"
REMOTE_IMAGE = "https://www.python.org/static/community_logos/python-logo-master-v3-TM.png"


async def main(user_id: int) -> None:
    if not LOCAL_SPRITE.is_file():
        raise SystemExit(f"sprite missing: {LOCAL_SPRITE}")
    encoded = "base64://" + base64.b64encode(LOCAL_SPRITE.read_bytes()).decode("ascii")

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
                            {
                                "type": "text",
                                "data": {"text": "[漂流瓶 mixed test]\n捡到了一个瓶子"},
                            },
                            {"type": "image", "data": {"file": encoded}},
                            {"type": "image", "data": {"file": REMOTE_IMAGE}},
                        ],
                    },
                    "echo": "bottle-1",
                }
            )
        )
        async for raw in ws:
            try:
                m = json.loads(raw)
            except Exception:
                continue
            if m.get("echo") == "bottle-1":
                print(json.dumps(m, ensure_ascii=False, default=str)[:1200])
                return


asyncio.run(asyncio.wait_for(main(int(sys.argv[1])), timeout=20))
