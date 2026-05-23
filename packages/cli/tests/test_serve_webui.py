"""`linling serve webui` 子命令的冒烟测试。

在后台线程里启动 uvicorn，随后 HTTP GET /api/health，期望 2 秒内 200。
"""

from __future__ import annotations

import socket
import threading
import time
from urllib.request import Request, urlopen

import uvicorn
from linling_webui.app import create_app
from linling_webui.config import WebUIConfig


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_serve_webui_health_endpoint() -> None:
    port = _free_port()
    config = WebUIConfig(host="127.0.0.1", port=port)
    app = create_app(config)
    server = uvicorn.Server(
        uvicorn.Config(app, host=config.host, port=port, log_level="error"),
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 3.0
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(
                Request(f"http://127.0.0.1:{port}/api/health", method="GET"),
                timeout=1.0,
            ) as resp:
                assert resp.status == 200
                body = resp.read().decode("utf-8")
                assert '"status"' in body and "ok" in body
                break
        except Exception as exc:
            last_error = exc
            time.sleep(0.1)
    else:
        raise AssertionError(f"server did not come up in time: {last_error}")

    server.should_exit = True
    thread.join(timeout=3.0)
