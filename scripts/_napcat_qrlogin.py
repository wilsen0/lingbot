"""从 NapCat 日志里抓出最新的二维码登录信息，并在终端渲染二维码。

仅给 ./start.sh 选 4（清缓存重扫）用——这是唯一需要人工扫码的场景。
优先用 NapCat 自己画好的 ASCII 二维码；抓不到就用解码 URL 现画一个。

输出:
  - 二维码解码 URL（手机 QQ 扫码登录授权用）
  - 终端二维码（ASCII）
依赖全部可选：有 qrcode 库就现画，没有就回退用日志里 NapCat 画的那张。
"""

from __future__ import annotations

import re
import subprocess
import sys

CONTAINER = "napcat"


def _logs(tail: int = 400) -> str:
    try:
        out = subprocess.run(
            ["docker", "logs", "--tail", str(tail), CONTAINER],
            capture_output=True, timeout=15,
        )
        return (out.stdout + out.stderr).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        print(f"读取 napcat 日志失败: {exc}", file=sys.stderr)
        return ""


def _latest_decode_url(text: str) -> str | None:
    urls = re.findall(r"二维码解码URL:\s*(\S+)", text)
    return urls[-1].strip() if urls else None


def _ascii_qr_from_logs(text: str) -> str | None:
    """抓 NapCat 自己打印的最后一块 ASCII 二维码（由 ▄▀█ 等块字符组成）。"""
    lines = text.splitlines()
    blocks: list[list[str]] = []
    cur: list[str] = []
    blockchars = set("▄▀█ ")
    for ln in lines:
        stripped = ln.strip()
        body = re.sub(r"^\d\d-\d\d \d\d:\d\d:\d\d.*?\]\s*", "", stripped)
        if body and set(body) <= blockchars and len(body) >= 10:
            cur.append(body)
        else:
            if len(cur) >= 10:
                blocks.append(cur)
            cur = []
    if len(cur) >= 10:
        blocks.append(cur)
    if not blocks:
        return None
    return "\n".join(blocks[-1])


def _render_qr(url: str) -> str | None:
    try:
        import qrcode  # type: ignore

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        import io

        buf = io.StringIO()
        qr.print_ascii(out=buf, invert=True)
        return buf.getvalue()
    except Exception:
        return None


def main() -> int:
    text = _logs()
    if not text:
        return 1
    url = _latest_decode_url(text)

    print()
    if url:
        print("  二维码解码 URL（用手机 QQ「扫一扫」扫下面的码授权登录）:")
        print(f"  {url}")
    print()

    qr = (_render_qr(url) if url else None) or _ascii_qr_from_logs(text)
    if qr:
        print(qr)
    else:
        print("  ⚠ 没能在终端画出二维码。")
        if url:
            print("  把上面的解码 URL 贴到任意「在线二维码生成」网站生成图片再扫，")
        print("  或打开 NapCat WebUI 扫码（链接见上方）。")
        print("  容器内二维码图片也存在: /app/napcat/cache/qrcode.png")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
