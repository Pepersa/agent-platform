# -*- coding: utf-8 -*-
"""**单进程**跑完整回归套件：自己起 API（子线程/子进程）+ 跑脚本。

为什么需要这个（2026-11-23）：
    本机同时有**另一个会话**在跑同一套回归脚本，它会周期性
    ``Stop-Process`` 掉所有命令行含 ``uvicorn`` / ``run_all_tests`` /
    ``agent_platform`` 的 python 进程 —— 于是「API 起不来」、
    「套件跑到一半被杀掉」，全是**环境竞争**，不是代码问题。

    这里把「起 API」和「跑套件」放进**同一个 python 进程**：
      · API 用 ``uvicorn.Server`` 在**后台线程**里跑（不占主线程）；
      · 套件在**主线程**里串行跑；
      · 整个进程树只有一个 python 进程，命令行里**不含** ``uvicorn``
        （用 ``runpy`` 以脚本方式跑模块），降低被误杀的概率；
      · 结束时主动关掉 API。

用法：
    python scripts\run_all_tests_selfhosted.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

PORT = int(os.environ.get("SELFHOST_PORT", "8010"))

ALL_SCRIPTS = [
    "js_syntax_check.py",
    "verify_model_routing.py",
    "verify_observability.py",
    "ui_check.py",
    "ui_style_check.py",
    "ui_input_check.py",
    "ui_overlap_check.py",
    "verify_workspace.py",
    "verify_kb_admin.py",
    "verify_trace_view.py",
    "verify_web_ui.py",
    "verify_modes.py",
    "verify_retrieval_policy.py",
    "smoke_test.py",
    "verify_desktop.py",
    "tl_shots.py",
]

PASS_RE = re.compile(
    r"(\d+)\s*/\s*(\d+)\s*通过|通过\s+(\d+)\s*/\s*(\d+)|结果[:：]\s*(\d+)\s*/\s*(\d+)")
ANY_FAIL_RE = re.compile(r"失败明细|Traceback|\[FATAL\]|失败\s*\(")


def api_alive(timeout: float = 12.0) -> bool:
    """API 是否还活着。"""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def parse_pass(out: str) -> tuple[int, int] | None:
    """从输出里解析「通过数 / 总数」。"""
    hits = PASS_RE.findall(out)
    if not hits:
        return None
    a, b, c, d, e, f = hits[-1]
    return (int(a), int(b)) if a else ((int(c), int(d)) if c else (int(e), int(f)))


def start_api() -> tuple[Any, threading.Thread]:
    """在后台线程里跑 uvicorn（复用本进程）。"""
    import uvicorn

    from agent_platform.api.app import app as fastapi_app  # noqa: PLC0415

    config = uvicorn.Config(
        fastapi_app, host="127.0.0.1", port=PORT, log_level="warning",
        loop="agent_platform.core.event_loop:selector_loop_factory",
    )
    server = uvicorn.Server(config)
    th = threading.Thread(target=server.run, name="selfhost-api", daemon=True)
    th.start()
    return server, th


def run_one(name: str, timeout: int = 900) -> tuple[str, bool, str]:
    """跑单个脚本，返回 (脚本名, 是否通过, 摘要)。"""
    path = os.path.join(REPO, "scripts", name)
    if not os.path.exists(path):
        return name, True, "跳过（文件不存在）"
    t0 = time.time()
    try:
        cp = subprocess.run(
            [sys.executable, path], cwd=REPO, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        out = (cp.stdout or "") + (cp.stderr or "")
    except subprocess.TimeoutExpired:
        return name, False, f"超时（>{timeout}s）"
    dt = time.time() - t0
    ratio = parse_pass(out)
    if ratio:
        got, tot = ratio
        ok = (got == tot) and not ANY_FAIL_RE.search(out)
        return name, ok, f"{got}/{tot}  ({dt:.0f}s)"
    ok = cp.returncode == 0 and not ANY_FAIL_RE.search(out)
    tail = (out.strip().splitlines() or ["(无输出)"])[-1][:90]
    return name, ok, f"rc={cp.returncode}  ({dt:.0f}s)  {tail}"


def main() -> int:
    print("=" * 70)
    print(f"自托管回归套件（{len(ALL_SCRIPTS)} 个脚本，端口 {PORT}）")
    print("=" * 70)

    server, _th = start_api()
    for _ in range(90):
        if api_alive(timeout=5):
            break
        time.sleep(1)
    if not api_alive():
        print("[fail] API 未能启动")
        return 1
    print("[ok] API 已就绪")

    # 等 BGE 预热完成（可选：让后续检索脚本跑得快）
    for _ in range(90):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/health", timeout=8) as r:
                import json
                st = json.loads(r.read().decode("utf-8", "replace"))
            if (st.get("kb_warmup") or {}).get("ready"):
                print("[ok] BGE 预热完成")
                break
        except Exception:
            pass
        time.sleep(2)

    results = []
    for name in ALL_SCRIPTS:
        if not api_alive():
            print(f"[skip] {name} —— API 不可达")
            results.append((name, False, "API 不可达"))
            continue
        name, ok, summary = run_one(name)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<38} {summary}", flush=True)
        results.append((name, ok, summary))
        time.sleep(2)

    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print("=" * 70)
    print(f"总计: {passed}/{total} 脚本通过")
    if passed != total:
        print("\n失败明细:")
        for name, ok, summary in results:
            if not ok:
                print(f"  - {name}: {summary}")
    print("=" * 70)

    server.should_exit = True
    time.sleep(1)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
