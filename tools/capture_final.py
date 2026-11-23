# -*- coding: utf-8 -*-
"""启动 exe → 等窗口出现 → **强制置前台/置顶** → 整屏抓图 → 存图。

★ 为什么要"强制置前台"：
    ``SetForegroundWindow`` 只对**前台进程**有效，脚本进程不是前台
    时调用会被 Windows 直接忽略（返回 NULL 但不报错）。所以这里用
    经典三板斧：
      ① ``AttachThreadInput``  把本线程的输入队列与目标窗口所在
         线程接上，之后 SetForegroundWindow 才被允许；
      ② ``SetWindowPos(HWND_TOPMOST)`` 把窗口临时提到最上层；
      ③ 抓完图后再 ``HWND_NOTOPMOST`` 还原，避免留下"总在最前"的
         副作用。

用法：
    python tools\\capture_final.py 输出.png
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

VBS = r"E:\PYPROJECT\agent\dist\desktop\启动Agent平台.vbs"
REPO = r"E:\PYPROJECT\agent"
LOG = r"C:\Users\admin\AppData\Local\AgentPlatform\logs\desktop.log"

user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = ctypes.c_bool
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = ctypes.c_bool
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetForegroundWindow.restype = wintypes.HWND
# GetCurrentThreadId 在 kernel32 里，不在 user32
kernel32 = ctypes.windll.kernel32
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
user32.GetDesktopWindow.restype = wintypes.HWND
user32.GetWindowDC.argtypes = [wintypes.HWND]
user32.GetWindowDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, ctypes.c_bool]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = ctypes.c_bool
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HBITMAP]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                            wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]

HWND_TOPMOST = wintypes.HWND(-1)
HWND_NOTOPMOST = wintypes.HWND(-2)
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
SWP_NOACTIVATE = 0x0010


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


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
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq AgentPlatform.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    pids = []
    for line in out.stdout.splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[1].isdigit():
            pids.append(int(parts[1]))
    return pids


def _find_main(pids):
    found = []

    def cb(h, _l):
        if _title(h) == "Agent平台" and _pid(h) in pids and user32.IsWindowVisible(h):
            found.append((h, _pid(h)))
            return False
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return found[0] if found else None


def _force_foreground(hwnd):
    """把窗口强制拉到前台（AttachThreadInput + TOPMOST）。"""
    target_tid = user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), None)
    my_tid = kernel32.GetCurrentThreadId()
    fg = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = False
    try:
        if fg_tid:
            attached = bool(user32.AttachThreadInput(my_tid, fg_tid, True))
        user32.ShowWindow(wintypes.HWND(hwnd), 9)      # SW_RESTORE
        user32.BringWindowToTop(wintypes.HWND(hwnd))
        user32.SetForegroundWindow(wintypes.HWND(hwnd))
        user32.SetWindowPos(wintypes.HWND(hwnd), HWND_TOPMOST, 240, 66, 1440, 900,
                            SWP_SHOWWINDOW)
    finally:
        if attached:
            user32.AttachThreadInput(my_tid, fg_tid, False)
    return target_tid


def _grab_screen(path):
    desk = user32.GetDesktopWindow()
    hdc = user32.GetWindowDC(desk)
    memdc = gdi32.CreateCompatibleDC(hdc)
    w = user32.GetSystemMetrics(0)
    h = user32.GetSystemMetrics(1)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(memdc, bmp)
    gdi32.BitBlt(memdc, 0, 0, w, h, hdc, 0, 0, 0x00CC0020)

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0
    raw = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(memdc, bmp, 0, h, raw, ctypes.byref(bmi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(desk, hdc)

    from PIL import Image
    img = Image.frombuffer("RGB", (w, h), raw, "raw", "BGR", 0, 1)
    img.save(path)
    return img


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else REPO + r"\build\desktop_capture.png"

    print(f"[{time.strftime('%H:%M:%S')}] 清理所有旧实例（避免单实例互斥量被别人占着）")
    subprocess.run(["taskkill", "/F", "/IM", "AgentPlatform.exe"], capture_output=True)
    time.sleep(3)
    try:
        import os
        os.remove(LOG)
    except Exception:
        pass

    before = set(_exe_pids())
    print(f"[{time.strftime('%H:%M:%S')}] 通过 VBS 启动")
    subprocess.run(["wscript.exe", VBS], check=False)

    hwnd = None
    deadline = time.time() + 180
    while time.time() < deadline:
        time.sleep(1.5)
        mine = set(_exe_pids()) - before
        if not mine:
            continue
        hit = _find_main(mine)
        if hit:
            hwnd, pid = hit
            print(f"[{time.strftime('%H:%M:%S')}] 窗口出现 hwnd=0x{hwnd:X} pid={pid}")
            break

    if not hwnd:
        print("!! 窗口未出现")
        return 1

    _force_foreground(hwnd)
    time.sleep(2.5)

    rect = wintypes.RECT()
    user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    fg = user32.GetForegroundWindow()
    print(f"rect=[{rect.left},{rect.top}] size={rect.right-rect.left}x{rect.bottom-rect.top}")
    print(f"visible={bool(user32.IsWindowVisible(wintypes.HWND(hwnd)))} "
          f"iconic={bool(user32.IsIconic(wintypes.HWND(hwnd)))} "
          f"foreground=0x{fg:X} target=0x{hwnd:X} "
          f"{'是前台' if fg == hwnd else '不是前台'}")

    img = _grab_screen(out)
    print(f"整屏截图: {out} {img.size}")

    crop = img.crop((int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)))
    crop.save(REPO + r"\build\desktop_capture_window.png")
    print("窗口区域: build\\desktop_capture_window.png", crop.size)

    user32.SetWindowPos(wintypes.HWND(hwnd), HWND_NOTOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW | SWP_NOACTIVATE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
