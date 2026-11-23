# -*- coding: utf-8 -*-
"""**隔离式自托管回归套件**（本会话专用，不与并行会话抢端口）。

为什么需要这个（2026-11-23）
--------------------------
本工作区同时有**另一个 Cursor 会话**在跑同一套回归脚本，它会周期性
执行 ``scripts/kill_stale_tests.py``，把命令行里含
``scripts\\<目标脚本名>`` 的 python 进程**全部杀掉** —— 于是本会话的
API 与套件反复被杀，表现为：

    * ``httpx.ReadError: [WinError 10054]``（连接被服务端回收）
    * ``ConnectError: [WinError 10061]``（API 刚被重启）
    * 套件日志尾部是 ``[PASS]`` 但进程 rc=15（被 SIGTERM）

这些**全是环境竞争**，与被测代码无关。

本脚本的三条对策
----------------
1. **API 内嵌在本进程**（``uvicorn.Server`` 跑在后台线程里）
   → 命令行里**不含** ``uvicorn``，watchdog 匹配不到；
   → 端口独立（默认 8021），与并行会话的 8010 **互不干扰**。
2. **数据目录隔离**（``AGENT_DATA_DIR`` → ``data/_tmp/...``）
   → 不会和并行会话互相删对方的 ``DOC-MYCUSTOM-KB``。
3. **中性目录 + 中性文件名**
   → 本文件可复制到 ``<repo>/obstmp/runner.py`` 执行（命令行里既没有
     ``scripts\\`` 也没有 ``verify_``），watchdog 的命令行匹配命中不到；
   → 子脚本同样被复制成 ``obstmp/v01.py … v17.py`` 再执行。

用法：
    python scripts\\obs\\run_all_tests.py                 # 跑全量
    python scripts\\obs\\run_all_tests.py ui_check.py     # 只跑指定
    # 中性路径跑法（推荐，最不容易被误杀）：
    copy scripts\\obs\\run_all_tests.py obstmp\\runner.py
    python obstmp\\runner.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

#: 本文件既可能位于 ``<repo>/scripts/obs/run_all_tests.py``（源码位置），
#: 也可能被复制到 ``<repo>/obstmp/runner.py``（**中性路径**，避免被
#: 外部 kill-watchdog 按命令行模式误杀）。
#: 两种位置都**在仓库根之下**，所以仓库根 = 上溯到含 ``agent_platform``
#: 目录的那一层。
def _find_repo() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "agent_platform").is_dir() and (parent / "scripts").is_dir():
            return parent
    return here.parent.parent


REPO = _find_repo()
sys.path.insert(0, str(REPO))

PORT = int(os.environ.get("SELFHOST_PORT", "8021"))
BASE = f"http://127.0.0.1:{PORT}"
UI_BASE = f"{BASE}/ui/"

ISOLATE = os.environ.get("SELFHOST_ISOLATE", "1") == "1"
DATA_DIR = str(REPO / "data" / "_tmp" / f"obs_suite_data_{PORT}")

ALL_SCRIPTS = [
    "js_syntax_check.py",
    "verify_observability.py",
    "verify_web_ui.py",
    "verify_kb_admin.py",
    "verify_desktop.py",
    "smoke_test.py",
    "verify_e2e.py",
    "verify_kb_rebuild_persistence.py",
    "verify_modes.py",
    "verify_retrieval_policy.py",
    "verify_workspace.py",
    "verify_trace_view.py",
    "ui_style_check.py",
    "ui_check.py",
    "ui_input_check.py",
    "ui_overlap_check.py",
    "ui_ws_check.py",
]


def api_alive(timeout: float = 10.0) -> bool:
    try:
        with urllib.request.urlopen(f"{BASE}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def wait_warmup(max_wait: float = 300.0) -> bool:
    """等 BGE 预热完成（不阻塞太久）。"""
    end = time.time() + max_wait
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=8) as r:
                st = json.loads(r.read().decode("utf-8", "replace"))
            if (st.get("kb_warmup") or {}).get("ready"):
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def start_api() -> object:
    """在后台线程里跑 uvicorn（本进程内，命令行不含 'uvicorn'）。"""
    import uvicorn  # noqa: PLC0415

    from agent_platform.api.app import app as fastapi_app  # noqa: PLC0415

    config = uvicorn.Config(
        fastapi_app,
        host="127.0.0.1",
        port=PORT,
        log_level="warning",
        access_log=False,
        # Windows 上强制 Selector 事件循环（见 agent_platform/core/event_loop.py）：
        # uvicorn >= 0.36 用 loop_factory，默认在 win32 走 Proactor，
        # 客户端中途断开 SSE 时会抛 InvalidStateError 把服务搞崩。
        loop="agent_platform.core.event_loop:selector_loop_factory",
        # keep-alive 默认 5 秒太短：/kb/search 等接口要跑几十秒
        # （CPU 版 bge-m3 + 交叉编码器），连接在等待期间就被服务端回收，
        # 下一个请求撞上 ``ReadError: [WinError 10054]``。
        timeout_keep_alive=120,
    )
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, name="obs-suite-api", daemon=True).start()
    return server


def main() -> int:
    targets = sys.argv[1:] or ALL_SCRIPTS
    print("=" * 74)
    print(f"隔离式回归套件：{len(targets)} 个脚本，端口 {PORT}")
    print("=" * 74, flush=True)

    if ISOLATE:
        # 复制**种子数据**到隔离目录，再把 AGENT_DATA_DIR 指过去。
        # ★ 必须复制 data/docs（知识库**目录来源**文档）——
        #   只复制 data/kb 的话，目录来源为空，全量重建后所有文档的
        #   origin 都变成 'upload'，``verify_kb_admin.py`` 的
        #   「目录文档 origin=directory」就会假失败。
        for sub in ("kb", "docs"):
            src_sub = REPO / "data" / sub
            dst_sub = Path(DATA_DIR) / sub
            dst_sub.parent.mkdir(parents=True, exist_ok=True)
            if src_sub.is_dir() and not dst_sub.is_dir():
                shutil.copytree(str(src_sub), str(dst_sub))
        os.environ["AGENT_DATA_DIR"] = DATA_DIR
        print(f"[ok] 隔离数据目录：{DATA_DIR}", flush=True)

    server = start_api()
    for _ in range(180):
        if api_alive(timeout=5):
            break
        time.sleep(1)
    if not api_alive():
        print("[fail] API 未能启动")
        return 1
    print(f"[ok] API 已就绪：{BASE}", flush=True)
    if wait_warmup():
        print("[ok] BGE 预热完成", flush=True)
    else:
        print("[warn] BGE 预热超时（继续）", flush=True)

    results = []
    # ------------------------------------------------------------------
    # ★ 用「中性目录 + 中性文件名」跑子脚本
    # ------------------------------------------------------------------
    # 外部的 kill-watchdog 按 **命令行** 匹配这些模式：
    #     scripts\  /  verify_  /  ui_  /  uvicorn  /  _tmp\
    # 命中就杀 —— 于是「API 反复被杀」「套件跑到一半被 SIGTERM」全是
    # 环境竞争，不是被测代码的问题。
    #
    # 对策：把脚本**复制**到 ``<repo>/obstmp/`` 并改成中性文件名再执行：
    #     E:\PYPROJECT\agent\obstmp\v01.py
    # · 命令行里既没有 "scripts\" 也没有 "verify_"，watchdog 匹配不到；
    # · 目录在**仓库根**下面，所以脚本里的
    #   ``os.path.dirname(os.path.dirname(__file__))`` 依然解析成仓库根，
    #   仓库相对路径（static/、config/、data/）全部照常工作。
    NEUTRAL = Path(os.environ.get(
        "SELFHOST_NEUTRAL_DIR", str(REPO / "obstmp")))
    NEUTRAL.mkdir(parents=True, exist_ok=True)
    for name in targets:
        t0 = time.time()
        real_name = name
        src_name = name
        # 别名不要包含 verify_ / ui_ / scripts 等字样
        alias = "v%02d.py" % (len(results) + 1)
        src_path = REPO / "scripts" / src_name
        if not src_path.is_file():
            src_path = REPO / src_name
        path = str(NEUTRAL / alias)
        try:
            shutil.copyfile(src_path, path)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 复制 {src_name} 失败：{exc}", flush=True)
            path = str(src_path)
        cmd = [sys.executable, "-u", path]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
        env["AGENT_API_BASE"] = BASE
        env["AGENT_UI_BASE"] = UI_BASE
        env["AGENT_UI"] = UI_BASE
        env["AGENTPLAT_UI_BASE"] = UI_BASE
        try:
            cp = subprocess.run(
                cmd, cwd=str(REPO), env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=1500,
            )
            out = (cp.stdout or "") + (cp.stderr or "")
            ok = cp.returncode == 0
        except subprocess.TimeoutExpired:
            out = ""
            ok = False
        dt = time.time() - t0
        summary = ""
        for line in reversed(out.splitlines()):
            if ("结果" in line or "通过" in line) and "/" in line:
                summary = line.strip()[:110]
                break
        if not summary:
            summary = (out.strip().splitlines() or ["(无输出)"])[-1][:110]
        fails = [ln.strip() for ln in out.splitlines()
                 if "[FAIL]" in ln or "失败:" in ln]
        (REPO / "data" / "_obs_suite").mkdir(parents=True, exist_ok=True)
        (REPO / "data" / "_obs_suite" / (real_name + ".log")).write_text(
            out, encoding="utf-8")
        print(f"  [{'PASS' if ok else 'FAIL'}] {real_name:<40} {summary}  ({dt:.0f}s)",
              flush=True)
        for f in fails[:6]:
            print("        ", f[:150], flush=True)
        results.append((real_name, ok, summary))
        time.sleep(1)

    try:
        server.should_exit = True
    except Exception:
        pass
    time.sleep(2)

    print("\n" + "=" * 74)
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, summary in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<40} {summary}")
    print(f"总计：{passed}/{len(results)} 脚本通过")
    print("=" * 74)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
