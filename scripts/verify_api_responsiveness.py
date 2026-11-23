"""验证 **API 在长时间阻塞操作（KB 全量重建）期间仍可响应**。

回归背景（2026-11-23）：
    ``POST /kb/reindex`` 之前在 ``async def`` 里**直接**调用同步的
    ``admin.reindex_all()``，而它会把每个 chunk 重新过一遍 bge-m3
    向量化（本机 bge-m3 走 CPU，实测单次重建要几分钟）。
    这段纯 CPU 同步调用把 **FastAPI 事件循环整个堵死**，
    表现为「重建期间所有其它接口全部超时」，
    连带把回归测试套件打挂（表现为一堆 Connection refused / ReadTimeout）。

    本脚本的判定方式：
      1. 后台线程发起 ``POST /kb/reindex``（长任务）
      2. 主线程**同时**轮询 ``GET /health`` 与 ``GET /kb/documents``
      3. 只要在重建期间健康检查能正常返回，就说明事件循环没被堵死

用法:
    python scripts\\verify_api_responsiveness.py
    python scripts\\verify_api_responsiveness.py --skip-reindex   # 只测健康检查基线
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8010"

_results: list[tuple[bool, str]] = []


def check(ok: bool, msg: str) -> None:
    _results.append((bool(ok), msg))
    print(f"  [{'PASS' if ok else 'FAIL'}] {msg}")


def get_json(path: str, timeout: float = 8.0):
    """GET 一个 JSON 端点，返回 (ok, payload_or_error, 耗时秒)。"""
    t0 = time.time()
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
            return True, data, time.time() - t0
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}", time.time() - t0
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}", time.time() - t0


def post_json(path: str, timeout: float = 900.0):
    """POST 一个 JSON 端点（body 为空）。"""
    req = urllib.request.Request(
        BASE + path, data=b"{}", headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, r.read().decode("utf-8", "replace"), time.time() - t0
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:200]}", time.time() - t0
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}", time.time() - t0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-reindex", action="store_true", help="跳过真实的全量重建")
    ap.add_argument("--probes", type=int, default=8, help="重建期间探测次数")
    args = ap.parse_args()

    print("=" * 66)
    print("API 响应性回归（阻塞操作不应拖死事件循环）")
    print("=" * 66)

    # ---- 基线：健康检查 ----
    ok, payload, dt = get_json("/health")
    check(ok, f"基线 /health 正常返回（{dt:.2f}s）")
    if not ok:
        print(f"    错误: {payload}")
        print("    → API 没起来？先跑 scripts\\restart_api.py")
        return 1

    # ---- 基线：知识库列表 ----
    ok, payload, dt = get_json("/kb/documents")
    check(ok, f"基线 /kb/documents 正常返回（{dt:.2f}s）")

    if args.skip_reindex:
        print("\n[skip] 按要求跳过全量重建")
        print(f"\n通过 {sum(1 for o, _ in _results if o)} / {len(_results)}")
        return 0

    # ---- 后台发起全量重建 ----
    rebuild_result: dict = {}

    def _do_rebuild() -> None:
        rebuild_result["ok"], rebuild_result["body"], rebuild_result["dt"] = post_json(
            "/kb/reindex", timeout=900.0
        )

    th = threading.Thread(target=_do_rebuild, daemon=True)
    print("\n--- 发起 POST /kb/reindex（后台线程）---")
    th.start()
    time.sleep(2.0)  # 给重建一点时间真正开始

    # ---- 重建期间持续探测 ----
    print("--- 重建期间轮询健康检查 ---")
    worst = 0.0
    alive = 0
    for i in range(args.probes):
        ok, payload, dt = get_json("/health", timeout=8.0)
        worst = max(worst, dt)
        if ok:
            alive += 1
            print(f"    probe {i + 1}/{args.probes}: OK  ({dt:.2f}s)")
        else:
            print(f"    probe {i + 1}/{args.probes}: FAIL ({payload})")
        time.sleep(0.7)

    check(alive >= max(1, args.probes - 1),
          f"重建期间 /health 仍可响应（{alive}/{args.probes} 次成功）")
    check(worst < 5.0,
          f"重建期间 /health 最坏延迟 {worst:.2f}s < 5s（事件循环未被堵死）")

    # ---- 等重建结束 ----
    print("\n--- 等待重建结束 ---")
    th.join(timeout=900)
    check("ok" in rebuild_result and rebuild_result.get("ok"),
          f"全量重建成功（{rebuild_result.get('dt', 0):.0f}s）"
          if rebuild_result.get("ok")
          else f"全量重建失败: {str(rebuild_result.get('body'))[:200]}")

    # ---- 重建后再测一次 ----
    ok, payload, dt = get_json("/kb/documents")
    check(ok, f"重建后 /kb/documents 正常返回（{dt:.2f}s）")

    passed = sum(1 for o, _ in _results if o)
    print("=" * 66)
    print(f"通过 {passed} / {len(_results)}")
    print("=" * 66)
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
