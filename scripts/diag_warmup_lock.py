# -*- coding: utf-8 -*-
"""诊断：冷加载期间 ``warmup_status()`` 的耗时到底卡在哪。

分别测量：
    · ``_lock`` 的**获取**耗时（锁竞争）
    · 拿到锁之后的**函数体**耗时（基本为 0）

如果获取锁很慢 → 是锁竞争（需改代码）；
如果获取锁很快但整体慢 → 是 GIL / CPU 争抢（不是锁的问题）。
"""
from __future__ import annotations

import os
import statistics
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from agent_platform.knowledge import tools  # noqa: E402


def main() -> int:
    samples: list[tuple[float, float, float]] = []
    stop = threading.Event()

    def poller() -> None:
        while not stop.is_set():
            t0 = time.perf_counter()
            with tools._lock:                 # ← 只测「拿锁」耗时
                t_lock = time.perf_counter() - t0
                snap = dict(tools._warmup_state)
            t_body = time.perf_counter() - t0 - t_lock
            t_total = time.perf_counter() - t0
            samples.append((t_lock, t_body, t_total))
            stop.wait(0.1)

    th = threading.Thread(target=poller, daemon=True)
    th.start()
    time.sleep(0.3)

    print("[*] 触发冷加载（构造 Embedder）…", flush=True)
    t0 = time.time()
    tools._get_cached_embedder()
    cold = time.time() - t0
    print(f"[*] 冷加载完成 {cold:.1f}s", flush=True)
    stop.set()
    th.join(timeout=3)

    lock_t = [s[0] for s in samples]
    body_t = [s[1] for s in samples]
    total_t = [s[2] for s in samples]

    def stat(name: str, xs: list[float]) -> None:
        s = sorted(xs)
        print(f"  {name}: 中位 {statistics.median(xs)*1000:7.1f}ms  "
              f"p95 {s[int(len(s)*0.95)]*1000:8.1f}ms  最大 {max(xs)*1000:9.1f}ms")

    print(f"采样 {len(samples)} 次，冷加载 {cold:.1f}s：")
    stat("拿锁耗时 ", lock_t)
    stat("函数体耗时", body_t)
    stat("总耗时    ", total_t)

    slow = [s for s in samples if s[2] > 1.0]
    if slow:
        print(f"\n有 {len(slow)} 次慢调用（>1s）：")
        for lk, bd, tt in slow[:8]:
            print(f"  拿锁 {lk*1000:9.1f}ms  函数体 {bd*1000:7.1f}ms  总 {tt*1000:9.1f}ms")
        if all(lk > 1.0 for lk, _, _ in slow):
            print("→ 结论：**锁竞争**（拿锁本身就很慢）")
        else:
            print("→ 结论：**不是锁竞争**，是 GIL / CPU 争抢（拿锁很快但整体慢）")
    else:
        print("\n→ 结论：没有出现 >1s 的调用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
