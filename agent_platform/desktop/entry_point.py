# -*- coding: utf-8 -*-
"""PyInstaller 专用入口脚本。

为什么需要它：
    PyInstaller 以「脚本」方式运行时，``__main__.py`` 里的相对导入
    （``from .launcher import ...``）会因为没有父包而报
    ``ImportError: attempted relative import with no known parent package``。

    本文件用**绝对导入**把入口暴露出来，供
    ``deploy/build_desktop.ps1`` 作为 PyInstaller 的入口脚本使用；
    源码运行仍然推荐 ``python -m agent_platform.desktop``。

★ **静默模式（``--window``）下的第一条铁律**（2026-11-24）
  ``--window`` 产物**没有控制台子系统**，此时 CPython 的
  ``sys.stdout`` / ``sys.stderr`` 是 **None**。此时任何 ``print()``
  都会抛 ``AttributeError: 'NoneType' object has no attribute 'write'``，
  应用会在**弹窗之前**直接崩掉。

  所以本文件的第一件事（**在 import 桌面包之前**）就是调用
  ``console_redirect.install_stream_redirect()``：把 stdout/stderr
  换成写日志文件的流，让启动诊断信息仍然留得住。
"""

from __future__ import annotations

import sys


def _install_stream_redirect() -> None:
    """把 stdout / stderr 重定向到桌面日志（静默模式下的**必做前置**）。

    ★ 必须在**任何** ``print()`` 之前执行 —— 包括 ``__main__.py``
      导入链里模块级的那句 print。
    """
    try:
        from agent_platform.desktop.console_redirect import (
            install_stream_redirect,
        )
    except Exception:  # noqa: BLE001 - 兜底路径不该让程序崩
        # 连重定向模块都导不进来（极少见）时，至少保证 stdout/stderr
        # 不是 None，避免 print() 直接 AttributeError。
        try:
            from agent_platform.desktop.console_redirect import ensure_streams

            ensure_streams()
        except Exception:
            pass
        return

    try:
        install_stream_redirect()
    except Exception:
        pass


def _fix_console_encoding() -> None:
    """Windows 控制台默认 GBK，中文输出会乱码。

    优先把 stdout/stderr 切到 UTF-8；失败则退回 ``errors="replace"``，
    保证 exe 在 cmd / PowerShell 下都能正常打印中文。

    ★ **同时强制行缓冲（``line_buffering=True`` / ``write_through=True``）**。

    为什么必须加这个（2026-11-23 实测）：
      PyInstaller 的 Windows console bootloader 在 **stdout 被重定向**
      （``> file``、``Start-Process -RedirectStandardOutput``）时，子进程拿到的
      不是控制台而是一个**管道**。此时 Python 的 ``sys.stdout`` 默认退化成
      **块缓冲**（4~8 KB 才刷一次），而本应用的启动日志总共也就几百字节，
      于是**整个启动过程零输出** —— 用户双击 exe 只看到一个黑窗口，
      连「[3/3] 打开工作台」都看不到，出现"卡死"假象。

      强制 ``line_buffering=True`` 后，每行 ``print`` 立即落到管道，
      重定向与真实控制台下行为一致。

    ★ 静默模式（``--window``）下 stdout 已经被重定向到日志文件，
      这里的 ``reconfigure`` 对文件流同样适用（UTF-8 + 行缓冲），
      日志因此不会出现中文乱码或"写了一半还没落盘"。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            # 静默模式下极少见（重定向没装上），跳过即可
            continue
        try:
            stream.reconfigure(
                encoding="utf-8",
                errors="replace",
                line_buffering=True,
                write_through=True,
            )  # type: ignore[union-attr]
        except Exception:
            try:
                stream.reconfigure(errors="replace", line_buffering=True)  # type: ignore[union-attr]
            except Exception:
                pass


if __name__ == "__main__":
    # ★ 顺序很重要：先重定向，再修编码，最后才 import 会 print 的模块。
    _install_stream_redirect()
    _fix_console_encoding()

    from agent_platform.desktop.__main__ import main

    sys.exit(main())
