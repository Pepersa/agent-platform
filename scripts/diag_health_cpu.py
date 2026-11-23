# -*- coding: utf-8 -*-
"""在冷加载窗口内**同时**采样系统 CPU 占用，验证 ``/health`` 的最坏延迟
究竟来自锁竞争还是 CPU 饥饿。

输出：
    · /health 延迟分位
    · 采样窗口内的系统 CPU 占用（%）
"""
from __future__ import annotations

import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

PORT = int(os.environ.get("COLD_PORT", "8018"))
BASE = f"http://127.0.0.1:{PORT}"


def _get(url: str, timeout: float) -> tuple[int, float]:
    t = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            r.read()
            return r.status, time.time() - t
    except Exception:
        return -1, time.time() - t


def _cpu_pct() -> float:
    """系统整体 CPU 占用（%）。"""
    try:
        import psutil
        return psutil.cpu_percent(interval=0.3)
    except Exception:
        return -1.0


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
    print(f"[ok] API 就绪 {BASE}（{time.time()-t0:.1f}s）", flush=True)

    samples: list[float] = []
    cpus: list[float] = []
    stop = threading.Event()

    def poller() -> None:
        while not stop.is_set():
            st, dt = _get(f"{BASE}/health", 60)
            samples.append(dt)
            stop.wait(0.15)

    def cpupoll() -> None:
        while not stop.is_set():
            cpus.append(_cpu_pct())
            stop.wait(0.3)

    threading.Thread(target=poller, daemon=True).start()
    threading.Thread(target=cpupoll, daemon=True).start()
    time.sleep(0.5)

    t1 = time.time()
    st, dt = _get(f"{BASE}/kb/search?query=T101_VDD_FUNCT&top_k=3", 900)
    cold = time.time() - t1
    stop.set()
    time.sleep(0.5)

    s = sorted(samples)
    print(f"冷 /kb/search: HTTP {st}  {cold:.1f}s", flush=True)
    print(f"/health 采样 {len(samples)} 次: "
          f"中位 {statistics.median(samples)*1000:.0f}ms  "
          f"p95 {s[int(len(s)*0.95)]*1000:.0f}ms  "
          f"p99 {s[int(len(s)*0.99)]*1000:.0f}ms  "
          f"最大 {max(samples)*1000:.0f}ms", flush=True)
    if cpus and cpus[0] >= 0:
        print(f"同期系统 CPU: 中位 {statistics.median(cpus):.0f}%  "
              f"最大 {max(cpus):.0f}%  （样本 {len(cpus)}）", flush=True)
    over = [x for x in samples if x > 1.0]
    print(f"超过 1s 的 /health 调用: {len(over)} 次", flush=True)
    if over:
        print("  最慢 3 次:", [f"{x:.1f}s" for x in sorted(over)[-3:]])
    server.should_exit = True
    return 0


if __name__ == "__main__":
    sys.exit(main())
