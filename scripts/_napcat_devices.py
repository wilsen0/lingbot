"""查 NapCat 当前登录态：登录信息 + 在线设备列表 + 设备型号自报。

用来判断「手机上看到的电脑登录」到底是不是 NapCat 自己。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import websockets

URL = os.environ.get("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
TOKEN = os.environ.get("ONEBOT_TOKEN", "linling-secret-2026")


async def call(ws, action, echo, **params):
    await ws.send(json.dumps({"action": action, "params": params, "echo": echo}))


async def main():
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    async with websockets.connect(URL, additional_headers=headers) as ws:
        wanted = {"login", "status", "clients", "model"}
        await call(ws, "get_login_info", "login")
        await call(ws, "get_status", "status")
        # 在线客户端列表（其他端登录会出现在这里）
        await call(ws, "get_online_clients", "clients", no_cache=True)
        # NapCat 自报的设备型号
        await call(ws, "_get_model_show", "model")
        async for raw in ws:
            try:
                m = json.loads(raw)
            except Exception:
                continue
            echo = m.get("echo")
            if echo in wanted:
                print(
                    json.dumps({"echo": echo, "retcode": m.get("retcode"),
                                "data": m.get("data")},
                               ensure_ascii=False, default=str)[:1500]
                )
                wanted.discard(echo)
                if not wanted:
                    return


asyncio.run(asyncio.wait_for(main(), timeout=12))
