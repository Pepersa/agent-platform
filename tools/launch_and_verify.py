# -*- coding: utf-8 -*-
"""启动 exe 并**快速轮询**窗口出现，一旦出现就立即做完整枚举 + 截图。

★ 为什么必须"快速"且"按 PID 归属"：
    这台机器上还有**另一个 agent** 在同时跑/关同一个
    ``AgentPlatform.exe``。所以：
      · 窗口一出现就必须马上抓证据，否则实例可能被收走；
      · 必须校验窗口的**属主 PID** 属于**本次启动的那一组** PID，
        否则会抓到别人的窗口（实测就踩到了）。

用法：
    python tools\\launch_and_verify.py
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

VBS = r"E:\PYPROJECT\agent\dist\desktop\启动Agent平台.vbs"
LOG = r"C:\Users\admin\AppData\Local\AgentPlatform\logs\desktop.log"
REPO = r"E:\PYPROJECT\agent"

user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = ctypes.c_bool
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]


def _title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    b = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, b, n + 1)
    return b.value


def _pid(hwnd):
    p = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
    return int(p.value)


def _exe_pids():
    """当前所有 AgentPlatform.exe 的 PID。

    ★ onedir 构建（2026-11-24）下 exe 路径变成
      ``dist\\desktop\\AgentPlatform\\AgentPlatform.exe``，
      但**进程名**不变，仍是 ``AgentPlatform.exe``，所以这里不用改。
    """
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq AgentPlatform.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    pids = []
    for line in out.stdout.splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[1].isdigit():
            pids.append(int(parts[1]))
    return pids


def _find_main(owner_pids):
    """找标题为「Agent平台」且属主属于 owner_pids 的顶层窗口。"""
    found = []

    def cb(h, _l):
        if _title(h) == "Agent平台" and _pid(h) in owner_pids:
            found.append((h, _pid(h)))
            return False
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return found[0] if found else None


def main():
    print(f"[{time.strftime('%H:%M:%S')}] 清理旧实例 ...")
    subprocess.run(["taskkill", "/F", "/IM", "AgentPlatform.exe"],
                   capture_output=True, text=True)
    time.sleep(2)

    before = set(_exe_pids())
    print(f"[{time.strftime('%H:%M:%S')}] 通过 VBS 启动：{VBS}")
    subprocess.run(["wscript.exe", VBS], check=False)

    deadline = time.time() + 300
    hwnd = None
    owner = None
    while time.time() < deadline:
        time.sleep(2)
        now = set(_exe_pids())
        # 本次启动新增的 PID（排除启动前就存在的）
        mine = now - before
        if mine:
            hit = _find_main(mine)
            if hit:
                hwnd, owner = hit
                print(f"[{time.strftime('%H:%M:%S')}] 窗口出现 hwnd=0x{hwnd:X} "
                      f"pid={owner} (本次启动 PID 集合 {sorted(mine)})")
                break
            print(f"[{time.strftime('%H:%M:%S')}] 本次 PID {sorted(mine)} 在跑，窗口未出现 ...")
        elif now:
            print(f"[{time.strftime('%H:%M:%S')}] 进程 {sorted(now)} 非本次启动，等待 ...")
        else:
            print(f"[{time.strftime('%H:%M:%S')}] 进程已退出，重新启动 ...")
            before = set()
            subprocess.run(["wscript.exe", VBS], check=False)

    if not hwnd:
        print("!! 超时：窗口没有出现")
        return 1

    # 窗口一出现**立刻**推到前台（避免被别的窗口盖住）
    user32.ShowWindow(wintypes.HWND(hwnd), 5)          # SW_SHOW
    user32.SetForegroundWindow(wintypes.HWND(hwnd))
    user32.BringWindowToTop(wintypes.HWND(hwnd))
    user32.SetWindowPos(wintypes.HWND(hwnd), 0, 0, 0, 0, 0,
                        0x0001 | 0x0002 | 0x0040)       # NOSIZE|NOMOVE|SHOWWINDOW
    time.sleep(2.0)

    print("=== 立刻枚举（限定本次启动的 PID）===")
    subprocess.run([sys.executable, REPO + r"\tools\enum_windows.py",
                    "AgentPlatform", "--console"], check=False)

    print("=== 日志尾部 ===")
    try:
        with open(LOG, "r", encoding="utf-8", errors="replace") as fh:
            print("".join(fh.readlines()[-22:]))
    except Exception as exc:
        print("读日志失败:", exc)
    print(f"DONE_HWND=0x{hwnd:X} OWNER_PID={owner}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
