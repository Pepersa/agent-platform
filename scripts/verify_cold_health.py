# -*- coding: utf-8 -*-
"""**单进程**验证：冷启动 bge-m3 加载期间 ``/health`` 始终可响应。

为什么单进程：
    本机有并行会话会周期性杀掉命令行含 ``uvicorn`` / ``agent_platform`` /
    ``scripts\\`` 的进程，跨进程验证会被误杀。

做法：
    1. 在后台线程跑 API（``uvicorn.Server``）；
    2. **不**等待 BGE 预热完成 —— 立刻开始打 ``/health``；
    3. 并发起一次冷 ``GET /kb/search``（触发 bge-m3 权重加载）；
    4. 断言整个冷加载期间 ``/health`` 每次都 200 且延迟在阈值内。

用法：
    python scripts\\verify_cold_health.py
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

PORT = int(os.environ.get("COLD_PORT", "8017"))
BASE = f"http://127.0.0.1:{PORT}"
HEALTH_BUDGET = 5.0


def _get(url: str, timeout: float) -> tuple[int, float, str]:
    t = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, time.time() - t, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, time.time() - t, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, time.time() - t, "%s: %s" % (type(e).__name__, e)


def main() -> int:
    import uvicorn

    from agent_platform.api.app import app as fastapi_app

    cfg = uvicorn.Config(
        fastapi_app, host="127.0.0.1", port=PORT, log_level="warning",
        loop="agent_platform.core.event_loop:selector_loop_factory",
    )
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()

    # 等端口起来（**不等** BGE 预热完成 —— 那正是要观察的窗口）
    t_ready = time.time()
    while time.time() - t_ready < 90:
        st, _, _ = _get(f"{BASE}/health", timeout=3)
        if st == 200:
            break
        time.sleep(0.3)
    print(f"[ok] API 就绪 {BASE}（{time.time()-t_ready:.1f}s）", flush=True)

    samples: list[tuple[int, float]] = []
    stop = threading.Event()

    def poller() -> None:
        while not stop.is_set():
            st, dt, _ = _get(f"{BASE}/health", timeout=30.0)
            samples.append((st, dt))
            stop.wait(0.2)

    threading.Thread(target=poller, daemon=True).start()
    time.sleep(0.5)

    t0 = time.time()
    st, dt, body = _get(f"{BASE}/kb/search?query=T101_VDD_FUNCT&top_k=3", timeout=900.0)
    cold = time.time() - t0
    stop.set()
    time.sleep(0.5)

    ok = [s for s, _ in samples if s == 200]
    lat = [d for _, d in samples]
    print(f"冷 /kb/search: HTTP {st}  耗时 {cold:.1f}s", flush=True)
    print(f"/health 采样: {len(samples)} 次, 200 响应 {len(ok)} 次", flush=True)
    if lat:
        s = sorted(lat)
        print(f"  延迟 中位数 {statistics.median(lat)*1000:.0f}ms  "
              f"p95 {s[int(len(s)*0.95)]*1000:.0f}ms  最大 {max(lat)*1000:.0f}ms")

    fails = []
    if st != 200:
        fails.append(f"冷 /kb_search 返回 {st}: {body[:200]}")
    if not ok:
        fails.append("冷加载期间 /health 一次都没返回 200")
    elif len(ok) != len(samples):
        fails.append(f"冷加载期间 /health 有 {len(samples)-len(ok)} 次非 200")
    if lat and max(lat) > HEALTH_BUDGET:
        fails.append(f"/health 最坏延迟 {max(lat):.2f}s > {HEALTH_BUDGET}s")
    if len(samples) < 10:
        fails.append(f"/health 采样太少（{len(samples)}）")

    for f in fails:
        print("[FAIL]", f)
    if not fails:
        print(f"[PASS] 冷加载 {cold:.0f}s 期间 /health 始终可响应"
              f"（{len(samples)} 次全部 200，最大延迟 {max(lat)*1000:.0f}ms）")
    server.should_exit = True
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
