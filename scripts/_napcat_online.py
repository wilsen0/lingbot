"""检查 NapCat 当前是否在线（OneBot get_status）。

退出码:
  0  在线 (online=true)
  1  已连上 NapCat 但账号离线 (online=false)  —— 需要重连/重登
  2  连不上 NapCat（容器没起 / 端口没开 / token 不对）

用法:
  uv run python scripts/_napcat_online.py
  ONEBOT_WS_URL=ws://127.0.0.1:3001 ONEBOT_TOKEN=xxx uv run python scripts/_napcat_online.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import websockets

URL = os.environ.get("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
TOKEN = os.environ.get("ONEBOT_TOKEN", "linling-secret-2026")
TIMEOUT = float(os.environ.get("NAPCAT_STATUS_TIMEOUT", "10"))


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
