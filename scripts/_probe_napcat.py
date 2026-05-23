"""Probe NapCat: get_status, get_login_info, get_friend_list size."""
import asyncio
import json
import websockets

URL = "ws://127.0.0.1:3001"
TOKEN = "linling-secret-2026"


async def call(ws, action, echo, **params):
    await ws.send(json.dumps({"action": action, "params": params, "echo": echo}))


async def main():
    async with websockets.connect(
        URL, additional_headers={"Authorization": f"Bearer {TOKEN}"}
    ) as ws:
        wanted = {"a", "b", "c"}
        await call(ws, "get_status", "a")
        await call(ws, "get_login_info", "b")
        await call(ws, "get_friend_list", "c")
        async for raw in ws:
            try:
                m = json.loads(raw)
            except Exception:
                continue
            echo = m.get("echo")
            if echo in wanted:
                if echo == "c":
                    data = m.get("data") or []
                    print(
                        json.dumps(
                            {
                                "echo": "c",
                                "friend_count": len(data),
                                "status": m.get("status"),
                                "retcode": m.get("retcode"),
                            },
                            ensure_ascii=False,
                        )
                    )
                else:
                    print(
                        json.dumps(
                            {"echo": echo, **m}, ensure_ascii=False, default=str
                        )[:800]
                    )
                wanted.discard(echo)
                if not wanted:
                    return


asyncio.run(asyncio.wait_for(main(), timeout=10))
