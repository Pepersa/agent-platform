# -*- coding: utf-8 -*-
"""桌面版应用（pywebview + 复用现有 Web 前端）。

对应需求：任务 1「桌面版应用」。

整体流程：
    1. **确保后端可用**
        · 先探测 ``GET /health``（默认 http://127.0.0.1:8010）；
        · 可达 → 直接复用现有 API 进程；
        · 不可达 → **在本进程内**用 uvicorn 起一个后台线程，监听随机空闲端口。
    2. **打开窗口**
        · 首选 pywebview（WebView2 内核，Windows 11 已装 Runtime）；
        · pywebview 不可用 → **优雅降级**：打印明确中文提示，用默认浏览器打开。
    3. **前端适配**
        静态前端通过 ``window.pywebview`` 存在性判断是否处于桌面模式，
        桌面模式隐藏「浏览器地址栏提示」等浏览器专属元素（见 static/app.js）。

模块一览：
    __main__    —— 命令行入口（``python -m agent_platform.desktop``）
    launcher    —— 后端探测 / 内嵌启动 / 前端地址计算
    window      —— pywebview 窗口创建 + 降级到浏览器
"""

from __future__ import annotations

__all__ = ["launcher", "window"]

__version__ = "0.1.0"
