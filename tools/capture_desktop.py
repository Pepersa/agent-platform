# -*- coding: utf-8 -*-
"""启动 exe → 等窗口出现 → 置前台 → 立刻截**整屏** → 存图。

★ 为什么必须"一气呵成"：
    这台机器上有**另一个 agent** 在同时跑/关同一个 exe，实例随时
    可能被别人收走。所以"启动 → 枚举 → 截图"必须在同一个脚本里
    连续完成，中间不能有几十秒的空档。

截图用的是 **整屏抓取**（GDI 的 BitBlt 整个屏幕），而不是
PrintWindow —— PrintWindow 对 WebView2/DWM 合成窗口只能抓到一个
纯色伪影，证明不了"内容真的画在屏幕上"。

用法：
    python tools\\capture_desktop.py 输出.png
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
user32.SetProcessDPIAware()

EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

VBS = r"E:\PYPROJECT\agent\dist\desktop\启动Agent平台.vbs"
REPO = r"E:\PYPROJECT\agent"

user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = ctypes.c_bool
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetDesktopWindow.restype = wintypes.HWND
user32.GetWindowDC.argtypes = [wintypes.HWND]
user32.GetWindowDC.restype = wintypes.HDC
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
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int


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


def _grab_screen(path):
    """整屏 BitBlt 抓图（物理像素）。"""
    desk = user32.GetDesktopWindow()
    hdc = user32.GetWindowDC(desk)
    memdc = gdi32.CreateCompatibleDC(hdc)
    w = user32.GetSystemMetrics(0)
    h = user32.GetSystemMetrics(1)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(memdc, bmp)
    gdi32.BitBlt(memdc, 0, 0, w, h, hdc, 0, 0, 0x00CC0020)  # SRCCOPY

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

    subprocess.run(["taskkill", "/F", "/IM", "AgentPlatform.exe"], capture_output=True)
    time.sleep(2)
    before = set(_exe_pids())
    print(f"[{time.strftime('%H:%M:%S')}] 启动 {VBS}")
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

    # 置前台：SW_RESTORE → SetForegroundWindow → BringWindowToTop
    user32.ShowWindow(wintypes.HWND(hwnd), 9)
    time.sleep(0.4)
    user32.SetForegroundWindow(wintypes.HWND(hwnd))
    user32.BringWindowToTop(wintypes.HWND(hwnd))
    time.sleep(0.4)
    user32.SetWindowPos(wintypes.HWND(hwnd), 0, 240, 66, 1440, 900, 0x0040)
    time.sleep(2.0)

    rect = wintypes.RECT()
    user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    print(f"rect=[{rect.left},{rect.top}] size={rect.right-rect.left}x{rect.bottom-rect.top}")
    print(f"visible={bool(user32.IsWindowVisible(wintypes.HWND(hwnd)))} "
          f"iconic={bool(user32.IsIconic(wintypes.HWND(hwnd)))} "
          f"foreground=0x{user32.GetForegroundWindow():X} "
          f"target=0x{hwnd:X}")

    img = _grab_screen(out)
    print(f"整屏截图已保存: {out} {img.size}")

    # 顺便把窗口区域裁出来
    crop = img.crop((int(rect.left), int(rect.top),
                     int(rect.right), int(rect.bottom)))
    crop.save(REPO + r"\build\desktop_capture_window.png")
    print("窗口区域已保存: build\\desktop_capture_window.png", crop.size)
    return 0


if __name__ == "__main__":
    sys.exit(main())
