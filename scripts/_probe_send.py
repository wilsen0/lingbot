"""Try to send a private message via NapCat to verify outbound path."""
import asyncio
import json
import sys
import websockets

URL = "ws://127.0.0.1:3001"
TOKEN = "linling-secret-2026"


async def main(user_id: int, text: str):
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
                        "message": [{"type": "text", "data": {"text": text}}],
                    },
                    "echo": "send-1",
                }
            )
        )
        async for raw in ws:
            try:
                m = json.loads(raw)
            except Exception:
                continue
            if m.get("echo") == "send-1":
                print(json.dumps(m, ensure_ascii=False, default=str)[:800])
                return


asyncio.run(asyncio.wait_for(main(int(sys.argv[1]), sys.argv[2]), timeout=10))
