# -*- coding: utf-8 -*-
"""后台回归运行器（独立进程，输出落盘，不受外层 shell 超时影响）。

用法：
    python scripts/run_regression_bg.py            # 跑全量
    python scripts/run_regression_bg.py a.py b.py  # 跑指定

结果写到 ``data/_regress_bg.json``。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

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
    "ui_ws_check.py",
    "verify_observability.py",
]


def main() -> int:
    only = sys.argv[1:] or SCRIPTS
    out_dir = REPO_ROOT / "data" / "_regress"
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for name in only:
        script = REPO_ROOT / "scripts" / name
        if not script.is_file():
            results.append({"name": name, "status": "MISSING", "summary": "", "sec": 0.0})
            continue
        t0 = time.time()
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
        env["no_proxy"] = env["NO_PROXY"]
        env.setdefault("BGE_M3_PATH", r"E:\model\models\bge-m3")
        env.setdefault("BGE_RERANKER_PATH", r"E:\model\models\bge-reranker-v2-m3")
        try:
            proc = subprocess.run(
                [sys.executable, "-u", str(script)],
                cwd=str(REPO_ROOT), env=env,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=2400,
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
            results.append({"name": name, "status": status,
                            "summary": summary, "sec": round(dt, 1)})
            print(f"[{status}] {name}  {summary}  ({dt:.0f}s)", flush=True)
        except subprocess.TimeoutExpired:
            results.append({"name": name, "status": "TIMEOUT", "summary": "", "sec": 2400.0})
            print(f"[TIMEOUT] {name}", flush=True)
        # 每次都落盘，中途被 kill 也能看到已完成的部分
        (REPO_ROOT / "data" / "_regress_bg.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    ok = sum(1 for r in results if r["status"] == "PASS")
    print("=" * 70, flush=True)
    print(f"回归汇总: {ok}/{len(results)} 通过", flush=True)
    for r in results:
        if r["status"] != "PASS":
            print(f"  {r['status']}: {r['name']} {r['summary']}", flush=True)
    print("=" * 70, flush=True)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
