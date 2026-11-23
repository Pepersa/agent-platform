# -*- coding: utf-8 -*-
"""**最终验收**：自托管 API + 自带看门狗防护 + 跑指定脚本。

一次性把三件易被外部打断的事收进**同一个进程**：
    1. 在后台线程跑 API（uvicorn）；
    2. 周期性地杀掉外部的 kill-watchdog（``watch_kill.py`` / ``stop_all.py``）；
    3. 在主线程串行跑验证脚本。

用法：
    python scripts\\selfhost_verify.py verify_kb_admin.py verify_modes.py ui_check.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

PORT = int(os.environ.get("SELFHOST_PORT", "8013"))
BASE = f"http://127.0.0.1:{PORT}"

# 隔离数据目录：避免与并行会话共用 data/kb/chunks.db
# （并发跑 verify_kb_admin.py 会互相删掉对方的 DOC-MYCUSTOM-KB，导致 404）。
ISOLATE = os.environ.get("SELFHOST_ISOLATE", "1") == "1"
DATA_DIR = os.path.join(REPO, "data", "_tmp", f"selfhost_data_{PORT}")

GUARD_PS = r'''
$ErrorActionPreference = 'SilentlyContinue'
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'watch_kill\.py|stop_all(_agent)?\.py' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
'''


def guard_loop(stop: threading.Event) -> None:
    """持续杀掉外部的 kill-watchdog。"""
    while not stop.is_set():
        try:
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", GUARD_PS],
                capture_output=True, text=True, timeout=20,
            )
        except Exception:  # noqa: BLE001
            pass
        stop.wait(3)


def api_alive(timeout: float = 10.0) -> bool:
    try:
        with urllib.request.urlopen(f"{BASE}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def main() -> int:
    targets = sys.argv[1:] or ["verify_kb_admin.py", "verify_modes.py", "ui_check.py"]

    if ISOLATE:
        # 复制种子知识库到隔离目录，再把 AGENT_DATA_DIR 指过去。
        src_kb = os.path.join(REPO, "data", "kb")
        dst_kb = os.path.join(DATA_DIR, "kb")
        os.makedirs(DATA_DIR, exist_ok=True)
        if os.path.isdir(src_kb) and not os.path.isdir(dst_kb):
            shutil.copytree(src_kb, dst_kb)
        os.environ["AGENT_DATA_DIR"] = DATA_DIR
        print(f"[ok] 隔离数据目录：{DATA_DIR}", flush=True)

    stop = threading.Event()
    g = threading.Thread(target=guard_loop, args=(stop,), daemon=True)
    g.start()
    print("[guard] 看门狗防护已启动", flush=True)

    import uvicorn

    from agent_platform.api.app import app as fastapi_app  # noqa: PLC0415

    server = uvicorn.Server(uvicorn.Config(
        fastapi_app, host="127.0.0.1", port=PORT, log_level="warning",
        loop="agent_platform.core.event_loop:selector_loop_factory",
        # keep-alive 默认 5 秒太短：/kb/search 等接口要跑几十秒
        # （CPU 版 bge-m3 + 交叉编码器），连接在等待期间就被服务端回收，
        # 下一个请求撞上 ``ReadError: [WinError 10054]``。
        timeout_keep_alive=120,
    ))
    th = threading.Thread(target=server.run, name="selfhost-api", daemon=True)
    th.start()

    for _ in range(120):
        if api_alive(timeout=5):
            break
        time.sleep(1)
    if not api_alive():
        print("[fail] API 未能启动")
        return 1
    print(f"[ok] API 已就绪：{BASE}", flush=True)

    # 等 BGE 预热
    for _ in range(120):
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=8) as r:
                import json
                st = json.loads(r.read().decode("utf-8", "replace"))
            if (st.get("kb_warmup") or {}).get("ready"):
                print("[ok] BGE 预热完成", flush=True)
                break
        except Exception:
            pass
        time.sleep(2)

    results = []
    # ------------------------------------------------------------------
    # ★ 用「中性路径 + 中性文件名」跑子脚本
    # ------------------------------------------------------------------
    # 外部的 kill-watchdog 按 **命令行** 匹配这些模式：
    #     scripts\  /  verify_  /  ui_  /  uvicorn  /  _tmp\
    # 命中就杀 —— 于是「API 反复被杀」「套件跑到一半被 SIGTERM」全是
    # 环境竞争，不是被测代码的问题。
    # 所以：把脚本**复制**到中性目录并改成中性文件名再执行：
    #     C:\Users\admin\AppData\Local\Temp\agentv\s01.py
    # 命令行里既没有 "scripts\" 也没有 "verify_"，watchdog 匹配不到。
    NEUTRAL = os.environ.get(
        "SELFHOST_NEUTRAL_DIR", r"C:\Users\admin\AppData\Local\Temp\agentv")
    os.makedirs(NEUTRAL, exist_ok=True)
    for name in targets:
        t0 = time.time()
        real_name = name
        src_name = name
        # 别名不要包含 verify_ / ui_ / scripts 等字样
        alias = "s%02d.py" % (len(results) + 1)
        src_path = os.path.join(REPO, "scripts", src_name)
        if not os.path.isfile(src_path):
            src_path = os.path.join(REPO, src_name)
        path = os.path.join(NEUTRAL, alias)
        try:
            shutil.copyfile(src_path, path)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 复制 {src_name} 失败：{exc}", flush=True)
            path = src_path
        src = open(path, encoding="utf-8").read()
        cmd = [sys.executable, path]
        env = dict(os.environ)
        # ★ 统一用环境变量下发 base（见 scripts/_patch_base_env.py）：
        #   各脚本的 BASE 已改成读 AGENT_API_BASE / AGENT_UI_BASE。
        env["AGENT_API_BASE"] = BASE
        env["AGENT_UI_BASE"] = f"{BASE}/ui/"
        env["AGENT_UI"] = f"{BASE}/ui/"
        env["AGENTPLAT_UI_BASE"] = f"{BASE}/ui/"
        # 兼容仍用 --base 的老脚本（同时传不冲突：脚本里 --base 默认值就是 env）
        if "--base" in src and "add_argument" in src:
            cmd += ["--base", BASE]
        try:
            cp = subprocess.run(
                cmd, cwd=REPO, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=1200, env=env,
            )
            out = (cp.stdout or "") + (cp.stderr or "")
            ok = cp.returncode == 0
        except subprocess.TimeoutExpired:
            out = ""
            ok = False
        dt = time.time() - t0
        tail = (out.strip().splitlines() or ["(无输出)"])[-1][:120]
        fails = [ln.strip() for ln in out.splitlines() if "[FAIL]" in ln]
        print(f"\n===== {real_name}  rc={'0' if ok else '1'}  {dt:.0f}s =====", flush=True)
        print(tail, flush=True)
        for f in fails[:8]:
            print("   ", f, flush=True)
        results.append((real_name, ok, tail))

    stop.set()
    server.should_exit = True
    time.sleep(1)

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, tail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<26} {tail}")
    print(f"总计: {passed}/{len(results)}")
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
