# -*- coding: utf-8 -*-
"""用 Win32 ``PrintWindow`` 直接抓**目标窗口自己的画面**。

为什么不用整屏截图：
    整屏截图只能证明"屏幕上那个位置有东西"，但如果别的窗口盖在上面，
    就分不清是应用内容还是遮挡物。而 ``PrintWindow(PW_RENDERFULLCONTENT)``
    直接让窗口把自己的内容重绘到 DC 里，**与遮挡无关**。

    ★ 这也是证明「窗口真的可见且已渲染内容」最客观的方式。

用法：
    python tools\\print_window.py 0x2D0542 输出.png
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = ctypes.c_bool
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = ctypes.c_bool
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = ctypes.c_bool
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

# PW_RENDERFULLCONTENT = 2 —— 让 WebView2 这类 DWM 合成窗口也能被抓出来
PW_RENDERFULLCONTENT = 0x00000002

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def main() -> int:
    hwnd = int(sys.argv[1], 0)
    out = sys.argv[2]

    h = wintypes.HWND(hwnd)
    title_len = user32.GetWindowTextLengthW(h)
    buf = ctypes.create_unicode_buffer(title_len + 1)
    user32.GetWindowTextW(h, buf, title_len + 1)
    print(f"hwnd=0x{hwnd:X} title={buf.value!r} visible={bool(user32.IsWindowVisible(h))}")

    rect = wintypes.RECT()
    user32.GetWindowRect(h, ctypes.byref(rect))
    w = int(rect.right) - int(rect.left)
    hgt = int(rect.bottom) - int(rect.top)
    print(f"rect=[{rect.left},{rect.top}] size={w}x{hgt}")

    hdc = user32.GetWindowDC(h)
    memdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, hgt)
    gdi32.SelectObject(memdc, bmp)

    ok = user32.PrintWindow(h, memdc, PW_RENDERFULLCONTENT)
    print(f"PrintWindow -> {bool(ok)}")

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -hgt          # 负高度 = 自上而下
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0

    buf_size = w * hgt * 4
    raw = ctypes.create_string_buffer(buf_size)
    lines = gdi32.GetDIBits(memdc, bmp, 0, hgt, raw, ctypes.byref(bmi), DIB_RGB_COLORS)
    print(f"GetDIBits lines={lines}")

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(h, hdc)

    from PIL import Image

    img = Image.frombuffer("RGB", (w, hgt), raw, "raw", "BGR", 0, 1)
    img.save(out)
    print(f"saved {out} size={img.size}")

    colors = img.getcolors(maxcolors=1 << 24) or []
    lum = [0.299 * r + 0.587 * g + 0.114 * b for r, g, b in img.getdata()]
    bright = sum(1 for v in lum if v > 140)
    print(f"唯一色数={len(colors)} 亮色像素={bright} ({bright/len(lum)*100:.1f}%)")
    print("判定:", "已渲染出内容（存在大量亮色文字/控件）" if bright > 200 else "内容偏暗")
    return 0


if __name__ == "__main__":
    sys.exit(main())
