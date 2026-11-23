# -*- coding: utf-8 -*-
"""冷加载期间，拆解 ``/health`` 的耗时来源：
    · 总耗时
    · 响应体里 ``llm`` 段的内容（是否 reachable=false → 探测超时）
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

PORT = int(os.environ.get("COLD_PORT", "8019"))
BASE = f"http://127.0.0.1:{PORT}"


def _get(url: str, timeout: float):
    t = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, time.time() - t, body
    except urllib.error.HTTPError as e:
        return e.code, time.time() - t, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, time.time() - t, f"{type(e).__name__}: {e}"


def main() -> int:
    import uvicorn

    from agent_platform.api.app import app as fastapi_app

    cfg = uvicorn.Config(fastapi_app, host="127.0.0.1", port=PORT,
                         log_level="warning",
                         loop="agent_platform.core.event_loop:selector_loop_factory")
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()

    t0 = time.time()
    while time.time() - t0 < 90:
        if _get(f"{BASE}/health", 3)[0] == 200:
            break
        time.sleep(0.3)
    print(f"[ok] API 就绪（{time.time()-t0:.1f}s）", flush=True)

    rows: list[tuple[float, str]] = []
    stop = threading.Event()

    def poller() -> None:
        while not stop.is_set():
            st, dt, body = _get(f"{BASE}/health", 60)
            note = ""
            if st == 200:
                try:
                    j = json.loads(body)
                    llm = j.get("llm") or {}
                    note = f"llm.reachable={llm.get('reachable')} " \
                           f"warmup={(j.get('kb_warmup') or {}).get('state')}"
                except Exception as e:  # noqa: BLE001
                    note = f"parse-fail {e}"
            rows.append((dt, note))
            stop.wait(0.15)

    threading.Thread(target=poller, daemon=True).start()
    time.sleep(0.5)

    t1 = time.time()
    st, dt, _ = _get(f"{BASE}/kb/search?query=T101_VDD_FUNCT&top_k=3", 900)
    cold = time.time() - t1
    stop.set()
    time.sleep(0.5)

    print(f"冷 /kb/search HTTP {st} {cold:.1f}s", flush=True)
    print(f"/health 采样 {len(rows)} 次", flush=True)
    slow = sorted([r for r in rows if r[0] > 1.0])
    fast = [r for r in rows if r[0] <= 1.0]
    print(f"  快 (<1s): {len(fast)} 次，样例 note={fast[0][1] if fast else '-'}", flush=True)
    if slow:
        print(f"  慢 (>1s): {len(slow)} 次", flush=True)
        for dt, note in slow[-6:]:
            print(f"    {dt:6.2f}s  {note}")
    notes = {}
    for _, n in rows:
        notes[n] = notes.get(n, 0) + 1
    print("  note 分布：")
    for n, c in sorted(notes.items(), key=lambda x: -x[1]):
        print(f"    {c:4d}× {n}")
    server.should_exit = True
    return 0


if __name__ == "__main__":
    sys.exit(main())
