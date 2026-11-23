# -*- coding: utf-8 -*-
"""桌面窗口：pywebview 优先，失败则降级到默认浏览器。

**优雅降级原则**（任务 1 硬要求）：
    pywebview 不可用（未安装 / WebView2 缺失 / import 失败 / 创建窗口抛错）
    → 打印**明确中文提示** → 用 ``webbrowser.open`` 打开默认浏览器 →
    **绝不让整个应用崩溃**。
"""

from __future__ import annotations

import os
import sys
import webbrowser
from typing import Any, Dict, Optional, Tuple

# ★ 静默模式（PyInstaller ``--window``）的兜底：没有控制台时 stdout 是 None，
#   直接 print() 会抛 AttributeError 把应用打崩。这里统一走 _safe_print。
from .console_redirect import ensure_streams

ensure_streams()

try:  # 仅在需要操作 WinForms 窗口位置时用得上
    from System.Drawing import Point  # type: ignore[import-not-found]
except Exception:  # noqa: BLE001 - 非 Windows / pywin32 缺失时降级
    Point = None  # type: ignore[assignment,misc]

__all__ = ["WEBVIEW_MISSING_HINT", "open_desktop", "open_in_browser"]


def _safe_print(*args: Any, **kwargs: Any) -> None:
    """在**可能没有 stdout** 的环境下安全地打印。

    ★ 为什么需要它：``--window`` 产物没有控制台子系统，
      ``sys.stdout`` 可能是 None（虽然正常情况下 entry_point 已经
      装好了日志重定向，但本模块也可能被单独使用）。
      这里再兜一层，任何异常都不允许冒泡 —— 打印失败绝不该让
      窗口开不出来。
    """
    try:
        stream = sys.stdout
        if stream is None:
            return
        print(*args, file=stream, **kwargs)
    except Exception:
        pass

#: pywebview 缺失 / 不可用时的中文提示
WEBVIEW_MISSING_HINT = """
────────────────────────────────────────────────────────────────
 未检测到可用的桌面窗口内核（pywebview / WebView2 Runtime）。

 原因可能：
   · 未安装 pywebview            → 请执行：pip install pywebview
   · 未安装 Microsoft Edge WebView2 Runtime
                                  → 请安装：https://developer.microsoft.com/microsoft-edge/webview2/
   · pywebview 导入失败 / 无图形界面环境

 已自动降级为「默认浏览器模式」：将用系统默认浏览器打开工作台。
 提示：浏览器模式下没有原生窗口标题栏与独立窗口生命周期，
       地址栏里直接访问 http://127.0.0.1:<port>/ui/ 即可。
────────────────────────────────────────────────────────────────
"""


def open_in_browser(url: str) -> bool:
    """用系统默认浏览器打开 url；返回是否成功调用。"""
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def _work_area() -> Tuple[int, int, int, int]:
    """取屏幕**工作区**（去掉任务栏）的 (x, y, width, height)。

    ★ 返回的是**逻辑像素**（与 pywebview 的坐标系一致）。
      本机 3840x2160 @200% 缩放，进程**不**声明 DPI 感知时
      ``GetSystemMetrics`` 返回 1920x1080 —— 那正是 pywebview
      用来算 ``scale = 物理/逻辑`` 的那个"逻辑"尺寸。
      这里必须用同一口径，否则居中位置会差一个 scale 倍数。

      ⚠️ **绝不能**在这里声明 DPI 感知（``SetProcessDpiAwareness``）：
         那会把 pywebview 的 scale 从 2.0 变成 1.0，两边口径就乱了。

    为什么需要工作区而不是整屏
    --------------------------
    任务栏通常贴在底部/侧边，用整屏算居中会让窗口被任务栏盖住一截。
    工作区才是用户实际能看到的区域。
    """
    try:
        import ctypes

        class _RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        rect = _RECT()
        # SPI_GETWORKAREA = 0x0030
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0,
                                                      ctypes.byref(rect), 0):
            return (int(rect.left), int(rect.top),
                    int(rect.right - rect.left), int(rect.bottom - rect.top))
    except Exception:
        pass

    try:
        import ctypes

        return (0, 0, int(ctypes.windll.user32.GetSystemMetrics(0)),
                int(ctypes.windll.user32.GetSystemMetrics(1)))
    except Exception:
        return (0, 0, 1920, 1080)


def _centered_position(width: int, height: int) -> Tuple[int, int]:
    """算一个**保证落在屏幕可见区域内**的窗口左上角坐标。

    返回的是 **pywebview 期望的逻辑像素**（它内部会再乘以 DPI scale）。

    ⚠️ 这里的 ``width``/``height`` 也必须是**逻辑像素**。
       本机 4K@200%，1440x900 的**逻辑**窗口 = 2880x1800 物理像素，
       正好是半屏 —— 这在 4K 屏上是合理的（不会糊满屏）。

    为什么必须显式给坐标（2026-11-23 修正）
    ------------------------------------------
    pywebview 在不传 ``x``/``y`` 时走 ``StartPosition = CenterScreen``，
    而在多显示器 / 高 DPI / 远程桌面场景下它常常算错 —— 实测直接落在
    ``(-32000, -32000)``（Windows 最小化窗口的哨兵坐标）或
    ``(-16000, -16000)``（WebView2 的哨兵坐标）。这两种情况下窗口
    **确实创建了**，但完全在屏幕外，用户双击 exe 只看到"没反应"。

    所以这里显式按工作区居中，并夹到工作区内，保证坐标非负。
    """
    wa_x, wa_y, wa_w, wa_h = _work_area()

    # 工作区与窗口都用逻辑像素，直接居中即可
    x = wa_x + max(0, (wa_w - width) // 2)
    y = wa_y + max(0, (wa_h - height) // 2)
    # 兜底：任何情况下都不允许负坐标
    return (max(0, x), max(0, y))


def _load_webview() -> Tuple[Optional[Any], str]:
    """尝试导入 pywebview。

    Returns:
        ``(module, error_message)``；成功时 error_message 为空串。
    """
    try:
        import webview  # type: ignore

        return webview, ""
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def open_desktop(url: str, *, title: str = "Agent平台",
                 width: int = 1440, height: int = 900,
                 background: str = "#141414") -> Dict[str, Any]:
    """打开桌面窗口（pywebview），不可用时降级到浏览器。

    Args:
        url: 前端地址（形如 ``http://127.0.0.1:8010/ui/``）。
        title: 窗口标题。
        width / height: 窗口初始尺寸。
        background: 窗口背景色（与前端一致，避免白屏闪一下）。

    Returns:
        ``{"mode": "webview"|"browser", "detail": str, "url": str}``
    """
    webview, err = _load_webview()
    if webview is None:
        _safe_print(WEBVIEW_MISSING_HINT, flush=True)
        _safe_print(f"[桌面] pywebview 不可用：{err}", flush=True)
        ok = open_in_browser(url)
        return {
            "mode": "browser",
            "detail": ("pywebview 不可用，已降级到默认浏览器模式"
                       if ok else "pywebview 不可用，且自动打开浏览器失败，请手动访问上述地址"),
            "url": url,
        }

    try:
        # ★ 必须**显式指定 x/y**（2026-11-23 修正）
        #   pywebview 在不传 x/y 时会交给 WebView2 自己算位置。在本机
        #   （单屏 3840x2160、无头会话 / 远程桌面断开）下它算出的是
        #   **-16000,-16000** —— 窗口确实创建了，但完全在屏幕外，
        #   用户看到的就是「双击了没反应」。
        #   显式给一个基于工作区居中的坐标，保证窗口一定落在可见区域。
        pos_x, pos_y = _centered_position(width, height)
        win = webview.create_window(
            title=title,
            url=url,
            width=width,
            height=height,
            x=pos_x,
            y=pos_y,
            background_color=background,
            text_select=True,
        )
        _safe_print(f"  窗口位置 : ({pos_x}, {pos_y})  尺寸 {width}x{height}", flush=True)
    except Exception as exc:
        _safe_print(WEBVIEW_MISSING_HINT, flush=True)
        _safe_print(f"[桌面] 创建 pywebview 窗口失败：{type(exc).__name__}: {exc}", flush=True)
        ok = open_in_browser(url)
        return {
            "mode": "browser",
            "detail": ("创建 pywebview 窗口失败，已降级到默认浏览器模式"
                       if ok else "创建 pywebview 窗口失败，且自动打开浏览器失败，请手动访问上述地址"),
            "url": url,
        }

    try:
        # ★ 窗口创建后**强制移回屏幕内**（2026-11-23 修正）
        #   ``create_window(x=, y=)`` 在 winforms+EdgeChromium 后端会被忽略，
        #   窗口仍落在 WebView2 的哨兵坐标 (-16000,-16000)。
        #
        #   注意时机：WebView2 的宿主布局是在页面 ``loaded`` **之后**才
        #   最终落定的 —— 此刻再 MoveWindow 会把位置**又覆盖回** -16000。
        #   所以这里做**定时重试**：每次都重新搬运，直到窗口真的落在
        #   屏幕内（矩形原点非负）为止，最多 ~3 秒。
        def _on_loaded() -> None:
            import threading
            import time as _time

            def _worker() -> None:
                for _ in range(15):
                    try:
                        _force_move_window(win, pos_x, pos_y, width, height)
                    except Exception:
                        pass
                    # 检查是否已经落在屏幕内
                    try:
                        ok = _window_on_screen(win)
                    except Exception:
                        ok = True  # 检查不了就当成功，别无限重试
                    if ok:
                        _safe_print(f"  窗口复位 : 已移回 ({pos_x}, {pos_y})", flush=True)
                        return
                    _time.sleep(0.2)
                _safe_print(f"  窗口复位 : 未能确认位置（目标 {pos_x}, {pos_y}）", flush=True)

            threading.Thread(target=_worker, name="window-reposition",
                             daemon=True).start()

        try:
            win.events.loaded += _on_loaded
        except Exception:
            # 事件挂不上不算致命：窗口至少创建成功了
            pass

        # ★ 显式 show()（2026-11-24 修正）
        #   在 PyInstaller ``--window``（Windows GUI 子系统）下，exe **没有
        #   控制台**，而 pywebview 的 winforms 后端在某些路径下会把窗口
        #   创建成**不可见**状态（``IsWindowVisible == False``）：
        #   窗口句柄存在、标题正确、坐标也正确，但屏幕上就是看不到东西。
        #   实测：源码模式 ``python -m agent_platform.desktop`` 的窗口
        #   vis=True，而同一个 exe 的窗口 vis=False —— 差异只来自
        #   「有无控制台」这一点。
        #
        #   ``win.show()`` 内部做 ``self.Show(); self.Activate()``，
        #   明确把窗口显示出来并抢焦点。放在 loaded 之后调用，
        #   此时 WebView2 已经就绪，显示出来就是有内容的。
        def _on_shown() -> None:
            try:
                win.show()
                _safe_print("  窗口显示 : 已显式 show()", flush=True)
            except Exception as exc:
                _safe_print(f"  窗口显示 : show() 失败（{exc}）", flush=True)

        try:
            win.events.loaded += _on_shown
        except Exception:
            pass

        # ★ 兜底：确保窗口**真的可见**（2026-11-24）
        #   不依赖 ``loaded`` 事件（页面加载失败时它可能不触发）。
        #   在窗口创建后起一个短生命周期的守护线程，反复确认：
        #     ① 窗口可见（IsWindowVisible）
        #     ② 窗口在屏幕内（矩形原点非负）
        #   两项都满足就退出。最多跑 ~8 秒，不会变成常驻后台负载。
        def _ensure_visible() -> None:
            import threading
            import time as _time

            def _worker() -> None:
                for _ in range(40):
                    try:
                        if not _window_visible(win):
                            win.show()
                        if _window_on_screen(win) and _window_visible(win):
                            _safe_print("  窗口显示 : 已确认可见且在屏幕内", flush=True)
                            return
                    except Exception:
                        pass
                    _time.sleep(0.2)

            threading.Thread(target=_worker, name="window-ensure-visible",
                             daemon=True).start()

        try:
            _ensure_visible()
        except Exception:
            pass

        webview.start()
        return {"mode": "webview", "detail": "桌面窗口已正常打开", "url": url}
    except Exception as exc:
        _safe_print(WEBVIEW_MISSING_HINT, flush=True)
        _safe_print(f"[桌面] pywebview.start() 失败：{type(exc).__name__}: {exc}", flush=True)
        ok = open_in_browser(url)
        return {
            "mode": "browser",
            "detail": ("pywebview 启动失败，已降级到默认浏览器模式"
                       if ok else "pywebview 启动失败，且自动打开浏览器失败，请手动访问上述地址"),
            "url": url,
        }


_DPI_SCALE_CACHE: Optional[float] = None


def _dpi_scale() -> float:
    """取 pywebview 使用的 DPI 缩放比（scale = 物理宽度 / 逻辑宽度）。

    为什么不能自己声明 DPI 感知（2026-11-23 实测踩坑）
    ------------------------------------------------------
    最初这里调用了 ``SetProcessDpiAwareness(2)`` 让进程感知 DPI，
    结果**反而把窗口位置算错了**。原因是 pywebview 的 scale 定义
    （``webview/platforms/win32.py::get_screen_scale``）是::

        scale = physical_width / logical_width

    其中 ``logical_width`` 来自 ``SystemInformation.VirtualScreen``，
    它取的是**进程是否 DPI 感知**下的"逻辑"宽度：

    · 进程**不**感知 DPI → logical = 1920 → scale = 3840/1920 = **2.0**
    · 进程**感知** DPI   → logical = 3840 → scale = 3840/3840 = **1.0**

    也就是说，**一旦我们自己声明了 DPI 感知，pywebview 的 scale 就从
    2.0 变成 1.0**，于是它不再对我们的逻辑坐标做 2 倍换算 —— 窗口
    落在 (-16000,-16000) 这种哨兵位置。

    实测对照：
      · 实验脚本（不声明 DPI 感知）传 x=120,y=33 → 窗口落在 **(240,66)** ✅
      · 应用（声明了 DPI 感知）传 x=600,y=291 → 窗口落在 **(-16000,-16000)** ❌

    所以这里**绝不**声明 DPI 感知，而是**复刻 pywebview 自己的算法**
    来算出它将要使用的 scale，保证两边口径一致。
    """
    global _DPI_SCALE_CACHE
    if _DPI_SCALE_CACHE is not None:
        return _DPI_SCALE_CACHE

    scale = 1.0
    try:
        import ctypes

        user32 = ctypes.windll.user32

        # 物理宽度：EnumDisplaySettingsW 的 dmPelsWidth
        physical = 0
        try:
            class _DEVMODE(ctypes.Structure):
                # 只声明到 dmPelsWidth 之前需要的字段
                _fields_ = [
                    ("dmDeviceName", ctypes.c_wchar * 32),
                    ("dmSpecVersion", ctypes.c_ushort),
                    ("dmDriverVersion", ctypes.c_ushort),
                    ("dmSize", ctypes.c_ushort),
                    ("dmDriverExtra", ctypes.c_ushort),
                    ("dmFields", ctypes.c_ulong),
                    ("dmPositionX", ctypes.c_long),
                    ("dmPositionY", ctypes.c_long),
                    ("dmDisplayOrientation", ctypes.c_ulong),
                    ("dmDisplayFixedOutput", ctypes.c_ulong),
                    ("dmColor", ctypes.c_short),
                    ("dmDuplex", ctypes.c_short),
                    ("dmYResolution", ctypes.c_short),
                    ("dmTTOption", ctypes.c_short),
                    ("dmCollate", ctypes.c_short),
                    ("dmFormName", ctypes.c_wchar * 32),
                    ("dmLogPixels", ctypes.c_ushort),
                    ("dmBitsPerPel", ctypes.c_ulong),
                    ("dmPelsWidth", ctypes.c_ulong),
                    ("dmPelsHeight", ctypes.c_ulong),
                    ("dmDisplayFlags", ctypes.c_ulong),
                    ("dmDisplayFrequency", ctypes.c_ulong),
                ]

            dm = _DEVMODE()
            dm.dmSize = ctypes.sizeof(_DEVMODE)
            if user32.EnumDisplaySettingsW(None, -1, ctypes.byref(dm)):
                physical = int(dm.dmPelsWidth)
        except Exception:
            physical = 0

        # 逻辑宽度：与 pywebview 同源（未声明 DPI 感知时的逻辑尺寸）
        logical = int(user32.GetSystemMetrics(0))
        if physical > 0 and logical > 0 and physical != logical:
            scale = physical / logical
        elif physical > 0:
            scale = 1.0
        else:
            scale = 1.0
    except Exception:
        scale = 1.0

    _DPI_SCALE_CACHE = scale
    return scale


def _native_hwnd(win: Any) -> int:
    """取 pywebview 窗口的 Win32 句柄（hwnd）。

    pywebview 的 ``win.native`` 在 winforms 下是一个 **Form 对象**，
    句柄在 ``Form.Handle`` 上（是一个 ``System.IntPtr`` 包装）。
    不同 pywebview 版本可能把它暴露成裸整数，所以两种都试。

    取不到时返回 0（调用方跳过 Win32 操作）。
    """
    native = getattr(win, "native", None)
    if native is None:
        return 0

    # ① Form.Handle（winforms）
    handle = getattr(native, "Handle", None)
    if handle is not None:
        try:
            # System.IntPtr 支持 int()；裸 int 直接返回
            return int(handle)
        except Exception:
            pass

    # ② 直接就是句柄
    try:
        value = int(native)
        return value if value > 0 else 0
    except Exception:
        return 0


def _window_visible(win: Any) -> bool:
    """检查 pywebview 窗口当前**是否可见**（不是最小化、也不是隐藏）。

    用 Win32 ``IsWindowVisible`` 判断 —— 它同时考虑窗口自身的
    ``WS_VISIBLE`` 样式和所有祖先窗口的可见性。

    实测：在 PyInstaller ``--window``（无控制台）构建下，窗口句柄
    存在、标题与坐标都正确，但 ``IsWindowVisible`` 返回 **False**，
    屏幕上什么也看不到。源码模式下同一个窗口返回 True。
    """
    hwnd = _native_hwnd(win)
    if not hwnd:
        return True
    try:
        import ctypes

        return bool(ctypes.windll.user32.IsWindowVisible(ctypes.c_void_p(hwnd)))
    except Exception:
        return True


def _window_on_screen(win: Any) -> bool:
    """检查 pywebview 窗口当前是否**真的落在屏幕可见区域内**。

    优先用 Win32 ``GetWindowRect`` 读真实矩形 —— 因为
    ``native.Left``/``native.Top`` 是**逻辑**坐标，和我们要判断的
    物理位置不是一个口径。

    判据：矩形左上角原点非负（WebView2 / Windows 的哨兵坐标都是
    -16000 / -32000 这种大负数，一眼可辨）。

    拿不到句柄时返回 True（无法判断时不阻塞重试逻辑）。
    """
    hwnd = _native_hwnd(win)
    if hwnd:
        try:
            import ctypes
            from ctypes import wintypes

            rect = wintypes.RECT()
            if ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(hwnd),
                                                   ctypes.byref(rect)):
                return int(rect.left) >= 0 and int(rect.top) >= 0
        except Exception:
            return True
    return True


def _force_move_window(win: Any, x: int, y: int,
                       width: int, height: int) -> bool:
    """把 pywebview 窗口**强制移动**到 (x, y) 并调整尺寸。

    为什么 ``create_window(x=..., y=...)`` 不够（2026-11-23）
    -----------------------------------------------------
    实测：即使显式传了 ``x=240, y=66``，pywebview 的 winforms 后端
    **仍然**把窗口放在 ``(-16000, -16000)`` —— 那个坐标是 WebView2
    在"没有可用显示器布局信息"时使用的哨兵值。``create_window`` 的
    x/y 参数只在某些后端生效，winforms + EdgeChromium 这一组合会忽略。

    实测到的窗口结构（便于理解为什么要多层兜底）::

        TOP    WindowsForms10.Window...            157x25    <- 只是外框
          CHILD Chrome_WidgetWin_1  'Agent平台'   0x0
          CHILD Chrome_RenderWidgetHostHWND        971x1032  <- 真正的内容窗口
          CHILD Intermediate D3D Window            971x1032

    直接对 TOP 调 Win32 ``MoveWindow`` **返回成功但矩形纹丝不动**
    （实测），因为 WinForms Form 的 Bounds 会被 WebView2 宿主接管。

    所以这里做**三层兜底**，任何一层生效即可：
      ① pywebview 自己的 ``win.move(x, y)``（内部做 DPI 换算，最贴合）；
      ② 直接设置 ``win.native.Location``（WinForms Form 属性）；
      ③ 遍历子窗口，对 ``Chrome_RenderWidgetHostHWND`` 等真实内容窗口调
         Win32 ``MoveWindow``。

    失败不致命 —— 只是窗口位置不理想，不影响功能。
    """
    moved = False

    # ⚠️ 坐标系换算
    #   ``x``/``y``/``width``/``height`` 是**逻辑像素**（pywebview 的口径，
    #   它内部会乘以 scale）；而 Win32 的 MoveWindow 用的是**物理像素**。
    #   这里统一换算成物理像素再调用 Win32 API。
    scale = _dpi_scale()
    phys_x = int(x * scale)
    phys_y = int(y * scale)
    phys_w = int(width * scale)
    phys_h = int(height * scale)

    # ⓪ 先解除「最小化」状态 —— 这是**最关键**的一步
    #   Windows 把最小化窗口放在 (-16000, -16000)，并且**忽略**对它的
    #   MoveWindow（实测返回 1 但矩形纹丝不动）。
    #   触发场景：用 ``Start-Process -WindowStyle Minimized`` 启动进程时，
    #   控制台窗口被最小化，而与它关联的 WinForms Form 会**一起**被最小化
    #   —— pywebview 的窗口就这样跑到了屏幕外，用户只看到"双击没反应"。
    #   必须先 ShowWindow(SW_RESTORE=9)，MoveWindow 才会真正生效。
    hwnd = _native_hwnd(win)
    if hwnd:
        try:
            import ctypes

            ctypes.windll.user32.ShowWindow(ctypes.c_void_p(hwnd), 9)
        except Exception:
            pass

    # ① Win32 MoveWindow（物理像素）—— 最直接、最可控
    #   ⚠️ 刻意**不**用 ``win.move()``：它在 winforms 里做的是
    #      ``self.Location = Point(x, y)``，用的是**逻辑**坐标，
    #      会把已经摆对的位置又挪偏（实测偏左上角）。
    if hwnd:
        try:
            import ctypes

            user32 = ctypes.windll.user32

            def _move_hwnd(target: int, w: int, h: int) -> bool:
                return bool(user32.MoveWindow(ctypes.c_void_p(target),
                                              phys_x, phys_y, w, h, True))

            _move_hwnd(hwnd, phys_w, phys_h)
            moved = True

            # 遍历子窗口，把真正的 WebView2 内容窗口一起搬过去
            EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)

            def _child_cb(child: Any, _l: Any) -> bool:
                try:
                    cls = ctypes.create_unicode_buffer(256)
                    ctypes.windll.user32.GetClassNameW(child, cls, 256)
                    name = cls.value
                    # Chrome_RenderWidgetHostHWND = 真正的渲染内容
                    if "RenderWidgetHost" in name or "Chrome_WidgetWin_1" in name:
                        _move_hwnd(int(child), phys_w, phys_h)
                except Exception:
                    pass
                return True

            user32.EnumChildWindows(ctypes.c_void_p(hwnd),
                                    EnumProc(_child_cb), 0)
        except Exception:
            pass

    # ② 兜底：WinForms Form 的 Location 属性（逻辑坐标）
    try:
        native = getattr(win, "native", None)
        if native is not None and hasattr(native, "Location") and Point is not None:
            native.Location = Point(int(x), int(y))
            moved = True
    except Exception:
        pass

    return moved


def desktop_env_info() -> Dict[str, Any]:
    """收集桌面运行环境信息（用于启动时打印与验证脚本）。"""
    info: Dict[str, Any] = {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "pywebview_installed": False,
        "pywebview_version": "",
        "pywebview_import_error": "",
        "webview2_runtime": False,
    }
    webview, err = _load_webview()
    if webview is None:
        info["pywebview_import_error"] = err
    else:
        info["pywebview_installed"] = True
        info["pywebview_version"] = str(getattr(webview, "__version__", "") or "")
    # WebView2 Runtime 是否安装（Windows 注册表）
    if os.name == "nt":
        try:
            import winreg  # type: ignore

            key_paths = [
                (winreg.HKEY_LOCAL_MACHINE,
                 r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
                 "pv"),
                (winreg.HKEY_LOCAL_MACHINE,
                 r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
                 "pv"),
            ]
            for root, path, value_name in key_paths:
                try:
                    with winreg.OpenKey(root, path) as key:
                        version, _ = winreg.QueryValueEx(key, value_name)
                        if version:
                            info["webview2_runtime"] = True
                            info["webview2_version"] = str(version)
                            break
                except OSError:
                    continue
        except Exception:
            pass
    return info
