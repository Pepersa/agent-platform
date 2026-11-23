# -*- coding: utf-8 -*-
"""Windows 安全事件循环工厂（供 uvicorn ``--loop`` 使用）。

背景
----
Windows 上 uvicorn（>= 0.36）不再使用「事件循环策略」，而是直接调用
``loop_factory``。默认的 ``uvicorn.loops.asyncio.asyncio_loop_factory`` 在
win32 且**不使用子进程**时返回 ``asyncio.ProactorEventLoop``。

问题
----
Proactor 事件循环在客户端**中途断开**时（典型场景：浏览器在 ``/chat`` 的
SSE 流式响应期间刷新页面 / 关页签）会：

1. socket 上收到 ``ConnectionResetError [WinError 995]``；
2. 在 ``IocpProactor._poll`` 里对**已经完成**的 future 调用 ``set_exception``；
3. 抛出 ``asyncio.exceptions.InvalidStateError: invalid state``。

该异常发生在 uvicorn 事件循环的顶层 → **整个 API 进程崩溃退出**。
实测现象：跑回归套件时服务静默死掉，后续脚本全部 ``ConnectError``；
浏览器侧表现为「保存失败：Failed to fetch」。

修复
----
改用 **Selector** 事件循环：

* Selector 的断连处理走标准的「读事件 → EOF」，不会有 Proactor 的
  双重 close 竞态；
* 本项目**不使用 asyncio 子进程**（已全仓检索确认没有 create_subprocess），
  而 Selector 唯一不支持的就是子进程 —— 因此没有功能损失。

⚠️ **uvicorn 自定义 ``--loop`` 的调用约定**（``uvicorn/config.py::get_loop_factory``）：
   当 ``--loop`` 的值**不在** uvicorn 内置表里时，uvicorn 会
   ``import_from_string(self.loop)`` 并**直接把它当作 loop_factory 使用**
   （不会传 ``use_subprocess`` 参数）。
   因此本模块导出的 ``selector_loop_factory`` 必须是
   **零参、返回事件循环实例** 的可调用对象。

用法
----
    python -m uvicorn agent_platform.api.app:app \\
        --loop agent_platform.core.event_loop:selector_loop_factory
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any


def selector_loop_factory() -> Any:
    """返回一个新的 **Selector** 事件循环实例。

    uvicorn 自定义 ``--loop`` 的契约：零参、返回 ``AbstractEventLoop``。
    """
    if sys.platform == "win32":
        # Windows：强制 Selector，规避 Proactor 断连崩溃（见模块 docstring）
        return asyncio.SelectorEventLoop()
    # 其它平台：优先 uvloop（更快），否则用标准 asyncio
    try:  # pragma: no cover - 取决于环境是否装了 uvloop
        import uvloop  # noqa: F401

        return uvloop.new_event_loop()
    except Exception:  # pragma: no cover
        return asyncio.new_event_loop()
