"""给运行中的 API 进程 dump 所有线程的 Python 栈（定位阻塞点）。

用法:
    python scripts\\dump_api_stacks.py [--pid N]

需要 pystack / py-spy 之类工具；没有的话退回 ``faulthandler`` 的
``dump_traceback_later``（需要 API 侧配合）。这里优先用
``py-spy``（若已安装），否则用 ``psutil`` 列出线程与 CPU 时间做辅助判断。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

import psutil


def find_api_pid() -> int | None:
    """找运行中的 uvicorn API 进程。"""
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cl = " ".join(p.info["cmdline"] or [])
        except Exception:
            continue
        if "uvicorn" in cl and "agent_platform" in cl:
            return p.pid
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    args = ap.parse_args()

    pid = args.pid or find_api_pid()
    if not pid:
        print("[!] 找不到 API 进程")
        return 1
    print(f"API pid = {pid}")

    spy = shutil.which("py-spy")
    if spy:
        print(f"[dump] {spy} dump --pid {pid}")
        rc = subprocess.call([spy, "dump", "--pid", str(pid)])
        return rc

    print("[!] 未安装 py-spy，退回线程概览")
    try:
        proc = psutil.Process(pid)
    except Exception as exc:  # noqa: BLE001
        print(f"[!] {exc}")
        return 1
    print(f"    rss = {proc.memory_info().rss / 1024 / 1024:.0f} MB")
    for t in proc.threads():
        print(f"    tid={t.id}  user={t.user_time:.2f}s  sys={t.system_time:.2f}s")
    print("\n提示: pip install py-spy 后可拿到完整 Python 调用栈。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
