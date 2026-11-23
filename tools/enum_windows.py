# -*- coding: utf-8 -*-
"""枚举某个进程名拥有的全部顶层窗口，报告可见性与矩形。

用法：
    python tools/enum_windows.py AgentPlatform
    python tools/enum_windows.py --pid 1234

输出每行形如：
    hwnd=0x1234 pid=1234 cls=WindowsForms10.Window.8.1 ... vis=True rect=[240,66] size=1440x900 title=Agent平台

★ 为什么用 Toolhelp32 而不是 tasklist：
  tasklist 的 ``/FI "IMAGENAME eq X"`` 必须写**完整**的映像名
  （``X.exe``），写 ``X`` 会一条都匹配不到。用 Toolhelp32 直接
  按进程名枚举，不受这些细节影响。
"""
from __future__ import annotations

import argparse
import ctypes
import sys
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = ctypes.c_bool
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = ctypes.c_bool
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = ctypes.c_bool

# ---- Toolhelp32 进程枚举 ----
TH32CS_SNAPPROCESS = 0x00000002


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]


def _pids_for(name: str) -> list:
    """按进程名取 PID 列表（``name`` 可省略 ``.exe`` 后缀）。"""
    wanted = name.lower()
    if not wanted.endswith(".exe"):
        wanted += ".exe"
    out = []
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == wintypes.HANDLE(-1).value or snap == 0:
        return out
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if kernel32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                if pe.szExeFile.lower() == wanted:
                    out.append(int(pe.th32ProcessID))
                if not kernel32.Process32NextW(snap, ctypes.byref(pe)):
                    break
    finally:
        kernel32.CloseHandle(snap)
    return out


def _text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _pid_of(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _console_attached(pids: list) -> list:
    """返回 (pid, console_process_count, attached_ok)。

    ★ 判定「目标进程是否挂在某个控制台」的**正确**做法：
      先 ``FreeConsole()`` 把**自己**的控制台摘掉，否则
      ``GetConsoleProcessList`` 报的是**自己**控制台的进程数，
      跟目标进程毫无关系 —— 那样非控制台程序也会被误判成"有控制台"。

      之后 ``AttachConsole(pid)``：
        · 成功 → 目标进程确实有控制台，此时 GetConsoleProcessList
          给出的是**那个控制台**里挂着的进程数；
        · 失败 → 目标进程**没有**控制台，计数按 0 计。
    """
    kernel32.FreeConsole()
    rows = []
    for pid in pids:
        attached = bool(kernel32.AttachConsole(pid))
        count = 0
        if attached:
            arr = (wintypes.DWORD * 64)()
            n = kernel32.GetConsoleProcessList(arr, 64)
            count = int(n)
            kernel32.FreeConsole()
        rows.append((pid, count, attached))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("name", nargs="?", default="AgentPlatform")
    ap.add_argument("--pid", type=int, default=None)
    ap.add_argument("--pids", default="", help="逗号分隔的 PID 集合，只枚举这些 PID 的窗口")
    ap.add_argument("--console", action="store_true")
    args = ap.parse_args()

    if args.pids:
        pids = [int(x) for x in args.pids.split(",") if x.strip()]
    elif args.pid is not None:
        pids = [args.pid]
    else:
        pids = _pids_for(args.name)

    print(f"=== 目标 PID: {pids} ===")
    if not pids:
        print("!! 没有找到目标进程")
        return 1

    pidset = set(pids)
    rows = []

    def _cb(hwnd, _lparam):
        try:
            pid = _pid_of(hwnd)
        except Exception:
            return True
        if pid not in pidset:
            return True
        rect = wintypes.RECT()
        ok = user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = int(rect.right) - int(rect.left)
        h = int(rect.bottom) - int(rect.top)
        vis = bool(user32.IsWindowVisible(hwnd))
        iconic = bool(user32.IsIconic(hwnd))
        rows.append({
            "hwnd": hwnd,
            "pid": pid,
            "cls": _class(hwnd),
            "vis": vis,
            "iconic": iconic,
            "rect": (int(rect.left), int(rect.top), w, h),
            "title": _text(hwnd),
            "rect_ok": bool(ok),
        })
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)

    print(f"=== 顶层窗口枚举（{len(rows)} 个）===")
    visible_main = []
    console_windows = []
    for r in rows:
        flag = "VIS" if r["vis"] else "HID"
        print(
            f'hwnd=0x{r["hwnd"]:X} pid={r["pid"]} vis={r["vis"]} iconic={r["iconic"]} '
            f'rect={list(r["rect"][:2])} size={r["rect"][2]}x{r["rect"][3]} '
            f'cls={r["cls"]!r} title={r["title"]!r} [{flag}]'
        )
        if r["cls"] == "ConsoleWindowClass":
            console_windows.append(r)
        if r["vis"] and r["title"] == "Agent平台":
            visible_main.append(r)

    print("=== 汇总 ===")
    print(f"总窗口数           : {len(rows)}")
    print(f"可见「Agent平台」   : {len(visible_main)}")
    print(f"ConsoleWindowClass  : {len(console_windows)}")

    if args.console:
        print("=== 控制台附加检查 ===")
        for pid, n, attached in _console_attached(pids):
            print(f"pid={pid} AttachConsole={'OK' if attached else 'FAIL(无控制台)'} "
                  f"GetConsoleProcessList={n}")

    print("=== 子窗口（主窗口）===")
    for r in (visible_main or rows):
        kids = []

        def _kcb(ch, _l):
            rect = wintypes.RECT()
            user32.GetWindowRect(ch, ctypes.byref(rect))
            kids.append((ch, _class(ch), _text(ch),
                         bool(user32.IsWindowVisible(ch)),
                         int(rect.left), int(rect.top),
                         int(rect.right) - int(rect.left),
                         int(rect.bottom) - int(rect.top)))
            return True

        user32.EnumChildWindows(wintypes.HWND(r["hwnd"]), EnumWindowsProc(_kcb), 0)
        for k in kids:
            print(f'  child hwnd=0x{k[0]:X} vis={k[3]} cls={k[1]!r} title={k[2]!r} '
                  f'rect=[{k[4]},{k[5]}] size={k[6]}x{k[7]}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
