"""清理残留的 verify_* 测试进程（避免多个测试套件并发抢同一个 API）。

只杀**明确属于旧一轮**的测试脚本，不碰 API / llama-server / 当前正在跑的那一个。

用法:
    python scripts\\kill_stale_tests.py [--keep <pid>] [--dry-run]
    python scripts\\kill_stale_tests.py --keep 12345    # 保留指定 PID
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import psutil

#: 这些脚本是「慢速 LLM 套件」，并发跑会互相拖死
TARGET_SCRIPTS = (
    "verify_modes.py",
    "verify_trace_view.py",
    "verify_kb_rebuild_persistence.py",
    "verify_retrieval_policy.py",
    "verify_desktop.py",
    "smoke_test.py",
    "verify_e2e.py",
    "verify_kb_admin.py",
    "verify_web_ui.py",
    "ui_check.py",
    "ui_style_check.py",
    "ui_input_check.py",
    "ui_overlap_check.py",
    "verify_observability.py",
    "verify_workspace.py",
    "verify_api_responsiveness.py",
    "verify_kb_search_latency.py",
    "tl_shots.py",
    "ui_ws_check.py",
    # 早期遗留的整套回归器（与 run_all_tests.py 功能重叠）
    "run_regression.py",
    # ★ 后台回归器 —— 它会**自己重启 API**，把正在进行的测量全部打断。
    #   是「一切并发/超时假失败」的根源，必须一并清掉。
    "run_regression_bg.py",
)

#: 绝不能杀的（运行中的套件本体）
NEVER_KILL = ("run_all_tests.py", "kill_stale_tests.py")


def _is_descendant_of_runner(pid: int) -> bool:
    """该进程是否是某个 ``run_all_tests.py`` 的后代。

    ★ 必须做这个判断（2026-11-23 修复）：
        ``run_all_tests.py`` 会**串行**拉起各 ``verify_*.py`` 子进程。
        如果无条件按脚本名 kill，就会把**当前这一轮正在跑的**脚本一起杀掉 ——
        表现为三个脚本 rc=15（SIGTERM）、日志尾部却是 ``[PASS]``。
        所以只杀「**不属于任何 run_all_tests.py 血统**」的残留进程。
    """
    try:
        proc = psutil.Process(pid)
    except Exception:
        return False
    try:
        ancestors = proc.parents()
    except Exception:
        return False
    for anc in ancestors:
        try:
            cl = " ".join(anc.cmdline() or [])
        except Exception:
            continue
        if "run_all_tests.py" in cl:
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=int, default=0, help="保留这个 PID 不杀")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--include-suite-children", action="store_true",
        help="连当前套件自己的子进程一起杀（默认不动，避免误伤正在跑的这一轮）",
    )
    args = ap.parse_args()

    me = os.getpid()
    killed = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cl = " ".join(p.info["cmdline"] or [])
        except Exception:
            continue
        pid = p.info["pid"]
        if pid in (me, args.keep):
            continue
        if any(n in cl for n in NEVER_KILL):
            continue
        # 必须匹配脚本路径形式，避免匹配到本脚本自身或父 shell
        if not any(("scripts\\" + s) in cl for s in TARGET_SCRIPTS):
            continue
        # ★ 不杀属于当前套件血统的进程（否则会打断正在跑的这一轮）
        if not args.include_suite_children and _is_descendant_of_runner(pid):
            if args.dry_run:
                print(f"[skip ] {pid}（属于当前 run_all_tests 套件）")
            continue
        if args.dry_run:
            print(f"[dry-run] would kill {pid}: {cl[:90]}")
            continue
        try:
            p.kill()
            killed.append(pid)
        except Exception as exc:  # pragma: no cover
            print(f"[fail] {pid}: {type(exc).__name__}")
    time.sleep(1.5)
    print(f"[ok] killed={killed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
