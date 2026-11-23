"""串行跑完整回归套件（避免多个脚本并发抢同一个 API 导致假失败）。

用法:
    python scripts\run_all_tests.py                # 跑全部
    python scripts\run_all_tests.py --fast         # 只跑不依赖 LLM 的静态脚本
    python scripts\run_all_tests.py --only verify_observability verify_web_ui

为什么需要这个：
    多个 verify_*.py 同时跑会争抢 127.0.0.1:8010，uvicorn 事件循环被打满，
    表现为「Connection refused」/「ReadTimeout」的**假失败**。
    本脚本保证**一个接一个**跑，并在每个脚本之间确认 API 还活着。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ★ 2026-11-23：Windows 控制台默认编码是 GBK(cp936)，输出里出现
#   U+FFFD / emoji / 冷僻字时 print 会抛 UnicodeEncodeError，
#   导致整个回归套件在中途崩溃（实测已发生）。
#   统一把 stdout/stderr 切到 UTF-8，并对无法编码的字符用 'replace' 兜底。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 老版本 Python 没有 reconfigure
        pass

#: 完整套件（按「由快到慢」排序，先跑快的能尽早发现问题）
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

#: 不发起 LLM 请求的静态脚本（--fast 用）
FAST_SCRIPTS = {
    "js_syntax_check.py",
    "verify_model_routing.py",
    "verify_observability.py",
    "ui_check.py",
    "ui_style_check.py",
    "ui_input_check.py",
    "ui_overlap_check.py",
    "verify_workspace.py",
}

#: 判定「通过」的正则（取脚本输出的最后几行）
PASS_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s*通过|通过\s+(\d+)\s*/\s*(\d+)|结果[:：]\s*(\d+)\s*/\s*(\d+)")
ANY_FAIL_RE = re.compile(r"失败明细|Traceback|\[FATAL\]|失败\s*\(")


def api_alive(url: str = "http://127.0.0.1:8010/health", timeout: float = 12.0) -> bool:
    """API 是否还活着（用来在脚本之间探测是否被打挂）。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def parse_pass(out: str) -> tuple[int, int] | None:
    """从输出里解析「通过数 / 总数」。"""
    hits = PASS_RE.findall(out)
    if not hits:
        return None
    a, b, c, d, e, f = hits[-1]
    got, tot = (int(a), int(b)) if a else ((int(c), int(d)) if c else (int(e), int(f)))
    return got, tot


def run_one(name: str, timeout: int = 900) -> tuple[str, bool, str]:
    """跑单个脚本，返回 (脚本名, 是否通过, 摘要)。"""
    path = os.path.join(REPO, "scripts", name)
    if not os.path.exists(path):
        return name, True, "跳过（文件不存在）"
    t0 = time.time()
    # ★ 2026-11-23：给子进程设 PYTHONIOENCODING=utf-8。
    #   Windows 控制台默认编码是 GBK(cp936)，子脚本里只要 print 一个
    #   ⚠ / ‑ / ↔ / ✅ 之类字符就会抛 UnicodeEncodeError 而**整个脚本失败**
    #   （实测 16 个回归脚本里有 7 个因此挂掉 —— 全是假失败）。
    #   这里统一让子进程用 UTF-8 输出，从根上消掉这类环境性假失败。
    child_env = dict(os.environ)
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
    try:
        cp = subprocess.run(
            [sys.executable, path],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=child_env,
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
    # ★ 2026-11-23：Windows 控制台默认 GBK，脚本输出里若含「替换字符」(U+FFFD)
    #   或 emoji 等非 GBK 字符，print 会抛 UnicodeEncodeError
    #   （实测 run_all_tests.py 在打印某个脚本摘要时整个套件崩溃）。
    #   这里把摘要统一转成「安全可打印」形式：
    #   · 不可编码字符 → 丢掉
    #   · 替换字符 U+FFFD → 统一显示为 '?'
    safe = "".join(
        ch for ch in tail
        if ch.isprintable() and ch != "\ufffd"
    ).strip() or "(无输出)"
    return name, ok, f"rc={cp.returncode}  ({dt:.0f}s)  {safe}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true", help="只跑静态脚本")
    ap.add_argument("--only", nargs="*", help="只跑指定脚本")
    ap.add_argument("--timeout", type=int, default=900, help="单脚本超时秒数")
    args = ap.parse_args()

    if args.only:
        scripts = list(args.only)
    elif args.fast:
        scripts = [s for s in ALL_SCRIPTS if s in FAST_SCRIPTS]
    else:
        scripts = list(ALL_SCRIPTS)

    print("=" * 70)
    print(f"串行回归套件（{len(scripts)} 个脚本）")
    print("=" * 70)

    if not api_alive():
        print("[warn] API 不可达 —— 先跑 scripts\\restart_api.py")

    results = []
    for name in scripts:
        if not api_alive():
            print(f"[skip] {name} —— API 不可达")
            results.append((name, False, "API 不可达"))
            continue
        name, ok, summary = run_one(name, timeout=args.timeout)
        flag = "PASS" if ok else "FAIL"
        print(f"  [{flag}] {name:<38} {summary}")
        results.append((name, ok, summary))
        # 每个脚本之间留出缓冲，让 uvicorn 事件循环喘口气
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
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
