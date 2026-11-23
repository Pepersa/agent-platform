# -*- coding: utf-8 -*-
"""桌面版启动器：后端探测 + 内嵌启动 + 前端地址计算。

职责划分：
    · ``probe_api``        —— 探测既有 API 是否可用（``GET /health``）
    · ``start_embedded``   —— 在**本进程内**用后台线程起 uvicorn（随机空闲端口）
    · ``resolve_backend``  —— 组合上述两步，返回 (base_url, mode, detail)
"""

from __future__ import annotations

import os
import socket
import threading
import time
from typing import Any, Dict, Optional, Tuple

__all__ = [
    "DEFAULT_API_BASE",
    "probe_api",
    "find_free_port",
    "start_embedded",
    "resolve_backend",
]

#: 默认探测的既有 API 地址
DEFAULT_API_BASE = "http://127.0.0.1:8010"

#: 探测超时（秒）
#:
#: ★ 2026-11-23 修正：``/health`` 最坏要 8~9 秒（三个模型端点串行探测，
#:   每个吃满读超时 + Windows TCP SYN 超时），此前这里写死 3.0 导致
#:   探测**必然超时** → 桌面版永远判定「API 不可达」并退出（窗口开不出来）。
#:   现在把两个超时分开：
#:     · ``PING_TIMEOUT_S``  —— 探 ``/ping``（纯存活，<5 ms），留 2 s 余量；
#:     · ``HEALTH_TIMEOUT_S`` —— 回退探 ``/health``（含子组件探测），给足 15 s。
PROBE_TIMEOUT_S = 3.0
PING_TIMEOUT_S = 2.0
HEALTH_TIMEOUT_S = 15.0
#: 内嵌服务启动等待上限（秒）
EMBED_START_TIMEOUT_S = 30.0


def _free_port() -> int:
    """向系统申请一个空闲端口（bind 到 0 后立即释放）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def find_free_port() -> int:
    """返回当前空闲的本地端口号。"""
    return _free_port()


def probe_api(base_url: str = DEFAULT_API_BASE, timeout: float = PROBE_TIMEOUT_S) -> bool:
    """探测既有 API 是否可用。

    策略（2026-11-23 修正）
    ----------------------
    **优先探 ``/ping``**（纯存活探针，< 5 ms），失败再回退到 ``/health``
    （含模型端点 / 知识库 / 注册表探测，最坏 8~9 秒）。

    为什么不能直接探 ``/health``
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    ``/health`` 会探测每个本地模型端点。llama-server 以 ``--parallel 1``
    运行，``/v1/models`` 会**排在正在进行的生成请求后面** —— 只要有一个
    模型在跑长任务，``/health`` 就要等好几秒。此前 ``probe_api`` 的超时
    是 3 秒，于是探测**必然超时**，桌面版每次都判定"既有 API 不可达"、
    直接退出，窗口根本开不出来（哪怕 API 好好地在跑）。

    超时分开：``/ping`` 用 2 s（够宽裕），``/health`` 用 15 s（够跑完）。

    优先用 httpx（项目已有依赖）；不可用时退回 urllib。
    """
    base = base_url.rstrip("/")

    def _get(url: str, t: float) -> bool:
        try:
            import httpx  # type: ignore

            resp = httpx.get(url, timeout=t)
            return resp.status_code == 200
        except Exception:
            pass
        try:
            import urllib.request

            with urllib.request.urlopen(url, timeout=t) as resp:
                return int(resp.status) == 200
        except Exception:
            return False

    # ① 快路径：/ping —— 老版本 API 没有这个端点会返回 404，落到回退分支
    if _get(base + "/ping", PING_TIMEOUT_S):
        return True

    # ② 回退：/health —— 老 API（或 /ping 被中间层挡住）时用，给足超时
    return _get(base + "/health", HEALTH_TIMEOUT_S)


def start_embedded(host: str = "127.0.0.1",
                   port: Optional[int] = None) -> Tuple[Optional[Any], str, str]:
    """在本进程内用**后台线程**启动 uvicorn。

    Returns:
        ``(server, base_url, detail)``；启动失败时 server 为 None、detail 为中文原因。
    """
    try:
        import uvicorn  # type: ignore
    except Exception as exc:
        return None, "", f"未安装 uvicorn，无法内嵌启动后端：{exc}"

    try:
        from ..api.app import app as fastapi_app  # type: ignore
    except Exception as exc:
        return None, "", f"无法导入 API 应用：{type(exc).__name__}: {exc}"

    if fastapi_app is None:
        return None, "", "API 应用创建失败（agent_platform.api.app:app 为空）"

    use_port = int(port or _free_port())
    config = uvicorn.Config(
        fastapi_app,
        host=host,
        port=use_port,
        log_level="warning",
        access_log=False,
        # Windows 上强制 Selector 事件循环（见 agent_platform/core/event_loop.py）：
        # uvicorn >= 0.36 用 loop_factory，默认在 win32 走 Proactor，
        # 客户端中途断开 SSE 时会抛 InvalidStateError 把服务搞崩。
        loop="agent_platform.core.event_loop:selector_loop_factory",
        # keep-alive 默认 5 秒太短：/kb/search 等接口要跑几十秒
        # （CPU 版 bge-m3 + 交叉编码器），连接在等待期间就被服务端回收，
        # 下一个请求撞上 ``ReadError: [WinError 10054]``。
        timeout_keep_alive=120,
    )
    try:
        server = uvicorn.Server(config)
    except Exception as exc:
        return None, "", f"uvicorn Server 创建失败：{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=server.run, name="desktop-embedded-uvicorn",
                              daemon=True)
    thread.start()

    base_url = f"http://{host}:{use_port}"
    deadline = time.time() + EMBED_START_TIMEOUT_S
    while time.time() < deadline:
        # ★ 探测内嵌后端时用**独立的**短超时（2026-11-23 修正）：
        #   内嵌 uvicorn 是刚起的，第一版 /health 可能还在做 KB 预热，
        #   用默认的 15 s 会在启动窗口里干等太久。
        #   ``probe_api`` 现在会先探 ``/ping``（<5 ms），这里给 1 s 足够。
        if probe_api(base_url, timeout=1.0):
            return server, base_url, f"已在桌面进程内启动后端（端口 {use_port}）"
        if not thread.is_alive():
            break
        time.sleep(0.25)

    try:
        server.should_exit = True
    except Exception:
        pass
    return None, "", f"内嵌后端启动超时（{EMBED_START_TIMEOUT_S:.0f}s 内 /health 未就绪）"


def resolve_backend(preferred: Optional[str] = None,
                    allow_embedded: bool = True) -> Dict[str, Any]:
    """解析后端地址。

    优先复用既有 API；不可达时（且允许时）在本进程内起一个。

    Returns:
        ``{"base_url","mode","detail","server","embedded"}``
        mode ∈ {"reuse", "embedded"}；失败时 ``base_url`` 为空串。
    """
    candidates: list[str] = []
    if preferred:
        candidates.append(preferred.rstrip("/"))
    if DEFAULT_API_BASE not in candidates:
        candidates.append(DEFAULT_API_BASE)

    for base in candidates:
        if probe_api(base):
            return {
                "base_url": base,
                "mode": "reuse",
                "detail": f"已连接到既有 API 服务：{base}",
                "server": None,
                "embedded": False,
            }

    if not allow_embedded:
        return {
            "base_url": "",
            "mode": "failed",
            "detail": "既有 API 不可达，且未允许内嵌启动",
            "server": None,
            "embedded": False,
        }

    server, base_url, detail = start_embedded()
    if server is None:
        return {
            "base_url": "",
            "mode": "failed",
            "detail": detail,
            "server": None,
            "embedded": False,
        }
    return {
        "base_url": base_url,
        "mode": "embedded",
        "detail": detail,
        "server": server,
        "embedded": True,
    }


def static_dir() -> str:
    """返回静态前端目录绝对路径。"""
    from ..api.app import STATIC_DIR  # type: ignore

    return str(STATIC_DIR)


def repo_root() -> str:
    """仓库根目录绝对路径。"""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
