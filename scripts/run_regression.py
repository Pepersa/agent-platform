# -*- coding: utf-8 -*-
"""回归基线批量运行器（开发辅助）。

用法：
    python scripts/run_regression.py                # 跑全部回归脚本
    python scripts/run_regression.py verify_web_ui.py ui_check.py   # 只跑指定几个

特性：
    · 强制 UTF-8 解码子进程输出（Windows 默认 GBK 会导致中文断言输出乱码报错）；
    · 用 ``-u`` 让子进程无缓冲输出，避免 stdout 落盘为空；
    · 每个脚本的完整输出落到 ``data/_regress/<name>.log``；
    · 最后打印汇总（通过/失败）与失败清单。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 全量回归清单（13 个基线 + 2 个新增）
SCRIPTS = [
    "verify_web_ui.py",
    "verify_kb_admin.py",
    "verify_desktop.py",
    "smoke_test.py",
    "verify_e2e.py",
    "verify_kb_rebuild_persistence.py",
    "verify_modes.py",
    "ui_style_check.py",
    "ui_check.py",
    "ui_input_check.py",
    "ui_overlap_check.py",
    "verify_retrieval_policy.py",
    "verify_workspace.py",
    "verify_trace_view.py",
]


def main() -> int:
    only = sys.argv[1:] or SCRIPTS
    results = []
    out_dir = REPO_ROOT / "data" / "_regress"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in only:
        script = REPO_ROOT / "scripts" / name
        if not script.is_file():
            results.append((name, "MISSING", "", 0.0))
            print(f"[SKIP] {name} 不存在", flush=True)
            continue
        t0 = time.time()
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
        env["no_proxy"] = env["NO_PROXY"]
        try:
            proc = subprocess.run(
                [sys.executable, "-u", str(script)],
                cwd=str(REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1200,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            (out_dir / (name + ".log")).write_text(out, encoding="utf-8")
            summary = ""
            for line in reversed(out.splitlines()):
                if ("结果" in line or "通过" in line) and "/" in line:
                    summary = line.strip()
                    break
            dt = time.time() - t0
            status = "PASS" if proc.returncode == 0 else "FAIL"
            results.append((name, status, summary, dt))
            print(f"[{status}] {name}  {summary}  ({dt:.0f}s)", flush=True)
        except subprocess.TimeoutExpired:
            results.append((name, "TIMEOUT", "", 1200.0))
            print(f"[TIMEOUT] {name}", flush=True)

    print("\n" + "=" * 70, flush=True)
    ok = sum(1 for _, s, _, _ in results if s == "PASS")
    print(f"回归汇总: {ok}/{len(results)} 通过", flush=True)
    for name, status, summary, dt in results:
        if status != "PASS":
            print(f"  {status}: {name} {summary}", flush=True)
    print("=" * 70, flush=True)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
