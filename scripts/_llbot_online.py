"""检查 OneBot v11 服务端当前账号是否在线（get_status）。

退出码:
  0  在线 (online=true)
  1  已连上 OneBot 服务端但账号离线 (online=false)  —— 需要重连/重登
  2  连不上 OneBot 服务端（容器没起 / 端口没开 / token 不对）

用法:
  uv run python scripts/_llbot_online.py
  ONEBOT_WS_URL=ws://127.0.0.1:3003 ONEBOT_TOKEN=xxx uv run python scripts/_llbot_online.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import websockets

URL = os.environ.get("ONEBOT_WS_URL", "ws://127.0.0.1:3003")
TOKEN = os.environ.get("ONEBOT_TOKEN", "linling-secret-2026")
TIMEOUT = float(os.environ.get("ONEBOT_STATUS_TIMEOUT", "10"))


async def _check() -> int:
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    async with websockets.connect(URL, additional_headers=headers) as ws:
        await ws.send(json.dumps({"action": "get_status", "params": {}, "echo": "s"}))
        async for raw in ws:
            try:
                m = json.loads(raw)
            except Exception:
                continue
            if m.get("echo") != "s":
                continue
            data = m.get("data") or {}
            online = bool(data.get("online"))
            print("online" if online else "offline")
            return 0 if online else 1
    return 2


def main() -> int:
    try:
        return asyncio.run(asyncio.wait_for(_check(), timeout=TIMEOUT))
    except Exception as exc:  # 连不上 / 超时 / 握手失败
        print(f"unreachable: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
