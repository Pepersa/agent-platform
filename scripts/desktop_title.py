# -*- coding: utf-8 -*-
"""开发辅助：读取 pywebview 桌面窗口的真实标题（Win32 枚举可见顶层窗口）。

用法：python scripts/desktop_title.py
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

user32 = ctypes.windll.user32

EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

results = []


def _text(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _pid(hwnd):
    p = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
    return p.value


def _cb(hwnd, _):
    title = _text(hwnd)
    if title:
        results.append((hwnd, _pid(hwnd), title, bool(user32.IsWindowVisible(hwnd))))
    return True


def main() -> int:
    user32.EnumWindows(EnumWindowsProc(_cb), 0)
    print("=== 所有顶层窗口（PID 匹配 agent_platform.desktop 的会标 *）===")
    hits = []
    for hwnd, pid, title, visible in results:
        try:
            import psutil  # type: ignore
            name = psutil.Process(pid).name()
        except Exception:
            name = ""
        marker = ""
        try:
            import psutil  # type: ignore
            cmd = " ".join(psutil.Process(pid).cmdline())
            if "agent_platform.desktop" in cmd:
                marker = " *"
                hits.append(title)
        except Exception:
            pass
        # 注：pywebview / WebView2 的顶层窗口在部分环境下 IsWindowVisible
        # 会返回 0，但窗口确实存在（有真实标题）。所以这里**不**按可见性过滤，
        # 只按标题判定，避免误报「找不到窗口」。
        vis = "" if visible else " (hidden)"
        print(f"  hwnd={hwnd} pid={pid} name={name} title={title!r}{vis}{marker}")
    print()
    print("=== agent_platform.desktop 窗口标题 ===")
    for t in hits:
        print("  ", repr(t))
    ok = any(t == "Agent平台" for t in hits)
    print()
    print("结果:", "PASS 标题为「Agent平台」" if ok else "FAIL 未找到标题为「Agent平台」的窗口")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
