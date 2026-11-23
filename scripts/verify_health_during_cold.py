# -*- coding: utf-8 -*-
"""验证「冷启动 bge-m3 加载期间，API 事件循环仍然可响应」。

做法：
    1. 起一个**全新进程**的 API（空 KB → 首次检索会触发 bge-m3 权重加载）；
    2. 在**另一个线程**里反复打 ``GET /health``，记录每次延迟；
    3. 同时发起**冷**的 ``GET /kb/search``（触发权重加载）；
    4. 断言：整个冷加载期间 ``/health`` 每次都返回 200 且在阈值内。

用法：
    python scripts\\verify_health_during_cold.py [--base http://127.0.0.1:8016]
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request

#: /health 的可接受最大响应时间（秒）——冷加载期间也必须远小于此
HEALTH_BUDGET = 5.0


def _get(url: str, timeout: float) -> tuple[int, float, str]:
    t = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, time.time() - t, body
    except urllib.error.HTTPError as e:
        return e.code, time.time() - t, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, time.time() - t, "%s: %s" % (type(e).__name__, e)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("AGENT_API_BASE", "http://127.0.0.1:8016"))
    args = ap.parse_args()
    base = args.base.rstrip("/")
    search_url = f"{base}/kb/search?query=T101_VDD_FUNCT&top_k=3"
    health_url = f"{base}/health"

    samples: list[tuple[int, float]] = []
    stop = threading.Event()

    def poller() -> None:
        while not stop.is_set():
            st, dt, _ = _get(health_url, timeout=30.0)
            samples.append((st, dt))
            stop.wait(0.2)

    th = threading.Thread(target=poller, daemon=True)
    th.start()
    time.sleep(0.5)

    t0 = time.time()
    st, dt, body = _get(search_url, timeout=600.0)
    cold = time.time() - t0
    stop.set()
    th.join(timeout=5)

    ok_codes = [s for s, _ in samples if s == 200]
    lat = [d for _, d in samples]
    print(f"冷 /kb/search: HTTP {st}  耗时 {cold:.1f}s")
    print(f"/health 采样: {len(samples)} 次, 200 响应 {len(ok_codes)} 次")
    if lat:
        print(f"  延迟 中位数 {statistics.median(lat)*1000:.0f}ms  "
              f"p95 {sorted(lat)[int(len(lat)*0.95)]*1000:.0f}ms  "
              f"最大 {max(lat)*1000:.0f}ms")

    fails = []
    if st != 200:
        fails.append(f"冷 /kb/search 返回 {st}")
    if not ok_codes:
        fails.append("冷加载期间 /health 一次都没返回 200")
    elif len(ok_codes) != len(samples):
        fails.append(f"冷加载期间 /health 有 {len(samples)-len(ok_codes)} 次非 200")
    if lat and max(lat) > HEALTH_BUDGET:
        fails.append(f"/health 最坏延迟 {max(lat):.2f}s > {HEALTH_BUDGET}s")
    if len(samples) < 10:
        fails.append(f"/health 采样太少（{len(samples)}），不足以说明并发情况")

    for f in fails:
        print("[FAIL]", f)
    if not fails:
        print(f"[PASS] 冷加载 {cold:.0f}s 期间 /health 始终可响应（{len(samples)} 次全部 200，"
              f"最大延迟 {max(lat)*1000:.0f}ms < {HEALTH_BUDGET*1000:.0f}ms）")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
