# -*- coding: utf-8 -*-
r"""桌面版**单实例保护**（Win32 命名互斥量）。

为什么需要它
------------
2026-11-24 用户反馈「可能是我点了好几次桌面图标造成的」—— 确实如此：

    · PyInstaller ``--onefile`` 每次启动都把整个运行时（约 7.5 GB）
      解压到 ``%TEMP%\_MEIxxxxxx``；
    · 连点几下桌面图标 → 同时起了 N 个进程 → N 份 7.5 GB 同时解压，
      互相抢磁盘 I/O 与空间 → bootloader 解压失败后**静默退出**，
      **没有任何报错**（用户看到的「闪退」）。

换成 ``--onedir`` 之后「每次启动解压 7.5 GB」这个根因没了，但
「用户连点 5 次」这个动作本身还会多开 5 个 pywebview 窗口
（5 个 WebView2 渲染进程），既浪费内存也让用户不知道该看哪个窗口。

所以这里加一层**纵深防御**：用 Win32 命名互斥量保证**同一时刻
只有一个桌面实例**。

行为约定
--------
* 第一个启动的进程**拿到**互斥量，正常跑完整流程；
* 第二个及以后的启动发现互斥量已存在（``GetLastError() == 183``），
  **不**启动第二个应用，而是：
    ① 按标题找到已有的「Agent平台」窗口；
    ② 把那个窗口**恢复并置前**（``ShowWindow(SW_RESTORE)`` +
       ``SetForegroundWindow``），让用户看到它；
    ③ 打印一行提示后**干净退出**。

★ 关键约束：

  1. **互斥量句柄必须被进程持有到结束** —— 句柄一关闭，互斥量就失效，
     下一个实例会以为自己是第一个。所以这里把句柄存进模块级变量
     （``_MUTEX_HANDLE``），**绝不** ``CloseHandle``。
  2. **检查必须放在最前面** —— 必须在 ``resolve_backend()``
     （可能耗时 10~15 秒）**之前**做，否则第二次点击还是要等很久。
  3. **非 GUI 调用要能绕过** —— ``--check`` / ``--browser-only``
     这类不打开（或不独占）窗口的调用**不应该**被互斥量挡住，
     所以由调用方显式传入 ``enabled=False``。

★ 为什么用 ``Local\\`` 前缀：
  ``Local\\`` 命名空间只在**当前登录会话**内可见。桌面应用本来就属于
  当前用户当前会话，用 ``Local\\`` 可以避免多用户终端服务器上
  不同用户之间互相阻塞（那种场景应该用 ``Global\\`` + 权限）。
"""

from __future__ import annotations

import sys
from typing import Any, Dict, Optional

__all__ = [
    "MUTEX_NAME",
    "WINDOW_TITLE",
    "acquire_singleton",
    "focus_existing_window",
]
#: 命名互斥量名字。
#: ★ 名字必须**稳定**（跨版本不变），否则老实例的互斥量和新实例对不上，
#:   单实例保护就失效了。
MUTEX_NAME = r"Local\AgentPlatformDesktopSingleton"

#: 桌面窗口标题（与 ``window.open_desktop(title=...)`` 保持一致）
WINDOW_TITLE = "Agent平台"

#: ``GetLastError()`` 返回该值表示互斥量**已经存在**
_ERROR_ALREADY_EXISTS = 183

#: ★ 进程级持有的互斥量句柄 —— **绝不** CloseHandle。
#:   句柄一旦关闭，互斥量立即失效，单实例保护形同虚设。
_MUTEX_HANDLE: Optional[int] = None


def _safe_print(*args: Any, **kwargs: Any) -> None:
    """在**可能没有 stdout** 的环境下安全地打印。

    静默模式（``--window``）下 stdout 可能是 None；这里与
    ``console_redirect.ensure_streams()`` 配合，保证打印失败
    永远不会影响单实例逻辑本身。
    """
    try:
        stream = sys.stdout
        if stream is None:
            return
        print(*args, file=stream, **kwargs)
    except Exception:
        pass


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def acquire_singleton(mutex_name: str = MUTEX_NAME) -> Dict[str, Any]:
    """尝试获取命名互斥量，判断本进程是不是**唯一**实例。

    Returns:
        一个字典，键含义：

        * ``acquired`` (bool) —— True 表示本进程**拿到了**互斥量，
          应该继续正常启动；False 表示**已经有实例在跑**，
          调用方应走「激活已有窗口并退出」的分支。
        * ``already_running`` (bool) —— 等价于 ``not acquired``，
          语义更直白。
        * ``focused`` (bool) —— 仅在 ``already_running`` 时有意义：
          是否成功找到了并激活了已有窗口。
        * ``reason`` (str) —— 中文原因说明，写进日志便于排查。
        * ``handle`` (int) —— 拿到的互斥量句柄（0 表示没拿到）。

    ★ 非 Windows 平台**永远**返回 ``acquired=True``：
      这是纯 Windows 的优化（``Local\\`` 命名空间），不该在其他平台
      阻断应用启动。
    """
    global _MUTEX_HANDLE

    if not _is_windows():
        return {
            "acquired": True,
            "already_running": False,
            "focused": False,
            "reason": "非 Windows 平台，跳过单实例检查",
            "handle": 0,
        }

    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        # 单实例保护是「锦上添花」，绝不能因为它把应用搞挂
        return {
            "acquired": True,
            "already_running": False,
            "focused": False,
            "reason": f"ctypes 不可用，跳过单实例检查（{exc}）",
            "handle": 0,
        }

    try:
        kernel32 = ctypes.windll.kernel32
        # 明确声明签名，避免 64 位下句柄被截断成 32 位 int
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                          wintypes.LPCWSTR]
        # lpMutexAttributes = NULL, bInitialOwner = TRUE, name
        handle = kernel32.CreateMutexW(None, True, mutex_name)
        err = kernel32.GetLastError()

        if not handle:
            # 创建失败（极端情况）→ 不阻断启动
            return {
                "acquired": True,
                "already_running": False,
                "focused": False,
                "reason": f"CreateMutexW 失败（err={err}），跳过单实例检查",
                "handle": 0,
            }

        if err == _ERROR_ALREADY_EXISTS:
            # 已经有实例在跑：我们**不**拥有这个互斥量，
            # 必须立刻关掉刚拿到的句柄，否则会一直吊着它。
            # ★ 注意：关的是**我们刚创建/打开的那个句柄副本**，
            #   不影响原持有者（内核里按名字计引用）。
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass
            return {
                "acquired": False,
                "already_running": True,
                "focused": False,
                "reason": "检测到已有实例在运行（互斥量已存在）",
                "handle": 0,
            }

        # ★ 拿到互斥量：把句柄存进模块级变量，**持有到进程结束**。
        _MUTEX_HANDLE = int(handle)
        return {
            "acquired": True,
            "already_running": False,
            "focused": False,
            "reason": "已获取单实例互斥量",
            "handle": _MUTEX_HANDLE,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "acquired": True,
            "already_running": False,
            "focused": False,
            "reason": f"单实例检查异常，跳过（{type(exc).__name__}: {exc}）",
            "handle": 0,
        }


def _find_window_by_title(title: str) -> int:
    """按**精确标题**查找一个可见的顶层窗口，返回其 hwnd（找不到返回 0）。

    为什么用 ``EnumWindows`` 而不是 ``FindWindowW``：
      ``FindWindowW`` 只按标题找，**不区分可见性**，可能命中
      已隐藏的窗口或 WebView2 的子窗口；``EnumWindows`` 允许我们
      过滤 ``IsWindowVisible``，只认**真正显示在屏幕上**的窗口。

    ★ 只找**可见**窗口：如果第一个实例还在启动中（窗口还没 show），
      这里会找不到 —— 此时调用方会退化成「打印提示后退出」，
      用户仍然可以手动再点一次看到窗口。
    """
    if not _is_windows():
        return 0
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415
    except Exception:
        return 0

    try:
        user32 = ctypes.windll.user32
        found = {"hwnd": 0}

        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND,
                                        wintypes.LPARAM)

        def _cb(hwnd: int, _lparam: int) -> bool:
            try:
                length = user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(int(length) + 1)
                user32.GetWindowTextW(wintypes.HWND(hwnd), buf, int(length) + 1)
                if buf.value != title:
                    return True
                if not user32.IsWindowVisible(wintypes.HWND(hwnd)):
                    return True
                found["hwnd"] = int(hwnd)
                return False  # 找到即停止枚举
            except Exception:
                return True

        user32.EnumWindows(enum_proc(_cb), 0)
        return int(found["hwnd"])
    except Exception:
        return 0


def focus_existing_window(title: str = WINDOW_TITLE) -> bool:
    """把已有的「Agent平台」窗口恢复并**置于前台**。

    Returns:
        True 表示成功找到并激活了窗口；False 表示没找到
        （例如第一个实例还在启动、窗口尚未 show）。

    ★ 为什么需要 ``ShowWindow(SW_RESTORE)``：
      如果第一个实例的窗口被最小化（或启动时被最小化），
      光调 ``SetForegroundWindow`` 是没用的 —— 必须先恢复。
    """
    if not _is_windows():
        return False

    hwnd = _find_window_by_title(title)
    if not hwnd:
        return False

    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        user32 = ctypes.windll.user32
        h = wintypes.HWND(hwnd)
        # SW_RESTORE = 9：先从最小化/隐藏状态恢复
        user32.ShowWindow(h, 9)
        # 再置前（可能因为前台锁被系统拒绝，所以再补一次 BringWindowToTop）
        user32.SetForegroundWindow(h)
        user32.BringWindowToTop(h)
        user32.SetWindowPos(h, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
        return True
    except Exception:
        return False


def release_singleton() -> None:
    """主动释放互斥量（仅供测试 / 优雅退出时使用）。

    ⚠️ 正常流程**不应该**调用它：句柄持有到进程结束，
    由操作系统自动回收。这里的存在只是为了让测试脚本能干净地
    反复启动/停止。
    """
    global _MUTEX_HANDLE
    if not _MUTEX_HANDLE:
        return
    try:
        import ctypes  # noqa: PLC0415

        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(_MUTEX_HANDLE))
    except Exception:
        pass
    finally:
        _MUTEX_HANDLE = None
