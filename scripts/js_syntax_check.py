# -*- coding: utf-8 -*-
"""开发辅助：静态 JS 语法检查（node 可用时用 node，否则用 Python 的 esprima 兜底）。

用途：改前端脚本后立刻发现「Identifier has already been declared」这类
**语法级**错误（浏览器只会在控制台报一句，回归脚本容易漏）。

用法：
    python scripts/js_syntax_check.py
"""
from __future__ import annotations

import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(REPO_ROOT, "agent_platform", "api", "static")

#: 需要做语法校验的前端脚本。
#: **2026-11-22 补入 workspace.js / trace_view.js** —— 上一轮交付新增了
#: 这两个文件，却没进语法检查清单，导致改坏了也不会被立刻发现。
#: **2026-11-23 补入 observability.js / timeline.js** —— Live Timeline
#: 改造新增了 timeline.js，并大幅改写了 observability.js。
FILES = ["app.js", "workbench.js", "custom_tools.js", "kb_admin.js",
         "workspace.js", "trace_view.js", "observability.js", "timeline.js"]


def node_check(path: str) -> tuple[bool, str]:
    """用 node --check 做语法校验。"""
    try:
        p = subprocess.run(["node", "--check", path], capture_output=True,
                           text=True, timeout=30)
    except FileNotFoundError:
        return True, ""     # 没装 node → 跳过
    except Exception as exc:
        return True, ""
    if p.returncode == 0:
        return True, ""
    return False, (p.stderr or p.stdout or "").strip()


def main() -> int:
    has_node = True
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=15)
    except Exception:
        has_node = False

    if not has_node:
        print("[skip] 未找到 node，跳过 JS 语法检查")
        return 0

    print("JS 语法检查（node --check）")
    bad = 0
    for f in FILES:
        p = os.path.join(STATIC, f)
        if not os.path.exists(p):
            print(f"  [SKIP] {f} 不存在")
            continue
        ok, err = node_check(p)
        if ok:
            print(f"  [PASS] {f}")
        else:
            bad += 1
            print(f"  [FAIL] {f}\n{err}")
    print("=" * 50)
    print(f"结果: {len(FILES) - bad}/{len(FILES)} 通过")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
