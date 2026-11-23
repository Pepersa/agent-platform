# -*- coding: utf-8 -*-
"""用 Chrome DevTools Protocol 截图并直接落盘到指定路径。

用法：
    python scripts/cdp_shot.py <out.png> [width] [height]
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request

CDP_HTTP = "http://127.0.0.1:9222"


def cdp(method: str, params: dict | None = None, session: str | None = None) -> dict:
    """走 /json 找到 target，再用 websocket 发命令。"""
    import socket
    import struct

    # 简易 websocket 客户端（避免额外依赖）
    targets = json.load(urllib.request.urlopen(CDP_HTTP + "/json", timeout=5))
    target = None
    for t in targets:
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            target = t
            break
    if target is None:
        raise RuntimeError("没有找到可调试的 page target")
    ws_url = target["webSocketDebuggerUrl"]
    # 解析 ws://host:port/path
    rest = ws_url.split("://", 1)[1]
    hostport, _, path = rest.partition("/")
    host, _, port = hostport.partition(":")
    port = int(port or 80)

    s = socket.create_connection((host, port), timeout=30)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET /{path} HTTP/1.1\r\n"
        f"Host: {hostport}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    s.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(4096)

    def send(payload: bytes) -> None:
        mask = os.urandom(4)
        n = len(payload)
        hdr = bytearray([0x81])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < (1 << 16):
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        hdr += mask
        hdr += bytes(payload[i] ^ mask[i % 4] for i in range(n))
        s.sendall(bytes(hdr))

    def recv() -> dict:
        def rd(n: int) -> bytes:
            out = b""
            while len(out) < n:
                c = s.recv(n - len(out))
                if not c:
                    raise RuntimeError("socket closed")
                out += c
            return out

        h = rd(2)
        ln = h[1] & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", rd(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", rd(8))[0]
        data = rd(ln)
        return json.loads(data.decode("utf-8", "replace"))

    msg = json.dumps({"id": 1, "method": method, "params": params or {}})
    send(msg.encode())
    while True:
        r = recv()
        if r.get("id") == 1:
            s.close()
            return r

    s.close()


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "shot.png"
    w = int(sys.argv[2]) if len(sys.argv) > 2 else 1600
    h = int(sys.argv[3]) if len(sys.argv) > 3 else 1000
    r = cdp("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
    data = r.get("result", {}).get("data")
    if not data:
        print("FAIL: no data", json.dumps(r)[:300])
        return 1
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "wb") as f:
        f.write(base64.b64decode(data))
    print(f"saved {out} ({os.path.getsize(out)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
