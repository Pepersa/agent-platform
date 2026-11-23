# -*- coding: utf-8 -*-
"""截取桌面应用窗口（不抢焦点、不发鼠标键盘事件）。

用 Win32 ``PrintWindow`` 把指定 PID 的顶层窗口画到内存 DC 再存 PNG。
不依赖前台窗口，因此不会打扰用户正在做的事。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "delivery_shots")

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
user32.SetProcessDPIAware()


class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG),
                ("right", wt.LONG), ("bottom", wt.LONG)]


def find_window(title_part: str, pid_filter: int | None = None):
    """按标题片段找顶层窗口，返回 (hwnd, pid, title)。"""
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _lparam):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid_filter and pid.value != pid_filter:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if title_part in buf.value:
            found.append((hwnd, pid.value, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    return found


def capture(hwnd, path: str) -> bool:
    """用 PrintWindow 把窗口内容存成 PNG。"""
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return False

    hdc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem_dc, bmp)
    # 2 = PW_RENDERFULLCONTENT（能抓到 WebView 这类合成内容）
    ok = user32.PrintWindow(hwnd, mem_dc, 2)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG),
                    ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                    ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]

    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h          # 负数 = 自上而下
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0

    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(bi), 0)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        from PIL import Image
        img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1)
        img.convert("RGB").save(path)
    except Exception:  # noqa: BLE001 —— 没有 PIL 就退化成 BMP 存盘
        path = os.path.splitext(path)[0] + ".bmp"
        with open(path, "wb") as fh:
            fh.write(buf.raw)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(hwnd, hdc)
    return ok


def main() -> int:
    title_part = sys.argv[1] if len(sys.argv) > 1 else "Agent平台"
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        OUT_DIR, "09-desktop-window.png")
    hits = find_window(title_part)
    if not hits:
        print(f"[FAIL] 没找到标题含 {title_part!r} 的窗口")
        return 1
    hwnd, pid, title = hits[0]
    print(f"[ok] 窗口 hwnd={hwnd} pid={pid} title={title!r}")
    ok = capture(hwnd, out)
    print(f"[{'ok' if ok else 'FAIL'}] 截图 -> {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
