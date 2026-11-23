# -*- coding: utf-8 -*-
"""桌面版入口：``python -m agent_platform.desktop``。

用法：
    python -m agent_platform.desktop                  # 自动探测后端（复用 8010 或内嵌启动）
    python -m agent_platform.desktop --api http://127.0.0.1:8010
    python -m agent_platform.desktop --no-embedded     # 不允许内嵌启动（只用既有 API）
    python -m agent_platform.desktop --browser-only    # 强制浏览器模式（不尝试 pywebview）
    python -m agent_platform.desktop --check           # 只做环境自检（不开窗口）

行为：
    1. 打印环境自检（python / pywebview / WebView2）；
    2. 解析后端地址（复用既有 API 或进程内启动 uvicorn）；
    3. 打开桌面窗口（pywebview），不可用时**降级**到默认浏览器；
    4. 窗口关闭后退出（内嵌后端随之停止）。

★ **「一键启动」怎么用**（2026-11-23 新增）：
  桌面窗口的**标题栏**有一个常驻的「一键启动」按钮：
  点开 → 勾选要跑的模型 → 点「启动」→ 平台把**勾选的**模型拉起来、
  **没勾的**停掉。背后的接口是 ``POST /platform/launch``。

  ★ **内嵌后端也能用**：桌面版在「没有既有 API」时会把 API 直接跑在
    **本进程**里（``launcher.start_embedded()``），此时
    ``/platform/launch`` 同样可用 —— 也就是说「一键启动」不依赖
    事先手工启动 API。
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any, Dict, List

# ★ 静默模式（PyInstaller ``--window``）的**前置兜底**：
#   没有控制台时 ``sys.stdout`` / ``sys.stderr`` 是 None，
#   任何 print() 都会抛 AttributeError 而让应用崩掉。
#   这个模块也可以被直接 ``python -m agent_platform.desktop`` 运行，
#   所以这里也要自己装一次重定向（install_stream_redirect 内部是幂等的，
#   跟 entry_point.py 里那次不会重复接管）。
from .console_redirect import ensure_streams, install_stream_redirect

install_stream_redirect()
ensure_streams()

from .launcher import DEFAULT_API_BASE, resolve_backend
from .single_instance import (WINDOW_TITLE, acquire_singleton,
                              focus_existing_window)
from .window import desktop_env_info, open_desktop, open_in_browser


def _safe_print(*args: Any, **kwargs: Any) -> None:
    """在**可能没有 stdout** 的环境下安全地打印。

    ★ 为什么需要它：静默模式（``--window``）下没有控制台子系统，
      ``sys.stdout`` 可能是 None。虽然模块导入时已经通过
      ``ensure_streams()`` 兜底，但这里的二次保护可以保证
      「打印失败」永远不会让桌面窗口开不出来。
    """
    try:
        stream = sys.stdout
        if stream is None:
            return
        print(*args, file=stream, **kwargs)
    except Exception:
        pass


def _print_banner() -> None:
    _safe_print("")
    _safe_print("===============================================================")
    _safe_print(" Agent平台 —— 桌面版工作台")
    _safe_print("===============================================================")


def _print_env() -> Dict[str, Any]:
    info = desktop_env_info()
    _safe_print("")
    _safe_print("[1/3] 运行环境自检")
    _safe_print(f"  Python            : {info['python']} ({info['platform']})")
    if info["pywebview_installed"]:
        _safe_print(f"  pywebview         : 已安装（版本 {info['pywebview_version'] or '未知'}）")
    else:
        _safe_print(f"  pywebview         : 不可用（{info['pywebview_import_error']}）")
        _safe_print("                      → 将自动降级为「默认浏览器模式」")
    if info.get("webview2_runtime"):
        _safe_print(f"  WebView2 Runtime  : 已安装（{info.get('webview2_version', '未知版本')}）")
    else:
        _safe_print("  WebView2 Runtime  : 未检测到（pywebview 在 Windows 上需要它）")
    return info


def _fix_console_encoding() -> None:
    """Windows 控制台默认 GBK 代码页，中文输出会乱码。

    把 stdout/stderr 切到 UTF-8（失败则退回 ``errors="replace"``），
    保证在 cmd / PowerShell / PyInstaller console 下都能正常打印中文。

    ★ 静默模式（``--window``）下 stdout 已被重定向到日志文件，
      这里同样适用（UTF-8 + 行缓冲），日志不会中文乱码。
    """
    for stream in (sys.stdout, sys.stderr):
        # ★ 静默模式下 stdout 可能是 None —— 直接跳过，
        #   否则 reconfigure 会在 None 上抛 AttributeError。
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            try:
                stream.reconfigure(errors="replace")  # type: ignore[union-attr]
            except Exception:
                pass


def _retry_embedded_backend(backend: Dict[str, Any]) -> Dict[str, Any]:
    """``resolve_backend()`` 失败时的**第二次机会**（只补内嵌路径）。

    ★ 为什么需要这一层兜底：
      ``launcher.resolve_backend()`` 内部靠 ``probe_api()`` 判定「既有 API
      是不是真的在跑」，而 ``probe_api()`` 打的是 ``GET /health``。
      ``/health`` 要探测两个本地模型端点 + 查知识库预热状态，
      在**模型刚启动 / BGE 还在预热**时可能要 **8~9 秒**才返回。
      如果那一刻的探测超时很短，桌面版就会误判「既有 API 不可达」。

      这里做两件**不改动** ``launcher.py`` 的事：
        1. 用**更宽松的超时**再探一次既有 API（``/ping`` 优先，
           它是纯 CPU 的轻量端点，没有模型探测）；
        2. 既有 API 确实没有时，**再试一次内嵌启动**（内嵌路径本身
           不依赖既有 API）。

    Args:
        backend: ``resolve_backend()`` 的返回值。

    Returns:
        补齐后的 backend 字典（成功时 ``base_url`` 非空）。
    """
    if backend.get("base_url"):
        return backend

    # ---- 1) 用更宽松的超时再探一次既有 API ----
    preferred = DEFAULT_API_BASE
    for base in (preferred, DEFAULT_API_BASE):
        if _probe_any(base, timeout=12.0):
            _safe_print(f"  [retry] 复检发现既有 API 可用：{base}")
            return {
                "base_url": base,
                "mode": "reuse",
                "detail": f"已连接到既有 API 服务：{base}",
                "server": None,
                "embedded": False,
            }

    # ---- 2) 再试一次内嵌启动 ----
    try:
        from .launcher import start_embedded  # noqa: PLC0415

        server, base_url, detail = start_embedded()
    except Exception as exc:  # noqa: BLE001 - 兜底路径不该让程序崩
        _safe_print(f"  [retry] 内嵌启动失败：{type(exc).__name__}: {exc}")
        server, base_url, detail = None, "", ""
    if server is not None and base_url:
        _safe_print(f"  [retry] {detail}")
        return {
            "base_url": base_url,
            "mode": "embedded",
            "detail": detail,
            "server": server,
            "embedded": True,
        }
    _safe_print(f"  [retry] 内嵌启动仍失败：{detail}")

    # ---- 3) 最后的兜底：**自己**在本进程起 uvicorn，用**轻量端点**探活 ----
    # ★ 为什么还要这一层：``launcher.start_embedded()`` 用 ``/health`` 探活，
    #   而 ``/health`` 会探测两个本地模型端点 + 查知识库预热状态，
    #   最坏要 8~9 秒（甚至在后端半坏时直接 500）。
    #   桌面版的「能不能用」其实只取决于「API 有没有在服务」，
    #   所以这里用 ``/platform/status``（纯内存、不探模型）来探活。
    local = _start_embedded_light()
    if local.get("base_url"):
        _safe_print(f"  [retry] {local.get('detail')}")
        return local
    return backend


def _start_embedded_light() -> Dict[str, Any]:
    """在**本进程**里起一个 uvicorn，并用**轻量端点**探活。

    ★ 与 ``launcher.start_embedded()`` 的区别只有一处：
      探活用的是 ``/platform/status`` 而不是 ``/health``。
      前者是纯内存计算（不探模型），毫秒级返回；后者最坏要 9 秒。
      桌面版要的是「窗口能打开」，不该被一个重端点卡住。

    ★ 这样起起来的后端**同样支持** ``POST /platform/launch`` ——
      「一键启动」在内嵌模式下也能用（这正是用户要的：
      打开桌面版 → 勾模型 → 一键拉起整个平台）。

    Returns:
        成功时 ``{"base_url", "mode", "detail", "server", "embedded"}``；
        失败时 ``base_url`` 为空串。
    """
    try:
        import threading  # noqa: PLC0415

        import uvicorn  # noqa: PLC0415

        from ..api.app import app as fastapi_app  # noqa: PLC0415
        from .launcher import find_free_port  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return {"base_url": "", "mode": "failed",
                "detail": f"无法内嵌启动后端：{type(exc).__name__}: {exc}",
                "server": None, "embedded": False}

    try:
        port = int(find_free_port())
        config = uvicorn.Config(
            fastapi_app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
            # Windows 上强制 Selector 事件循环（见 core/event_loop.py）：
            # 默认的 Proactor 在客户端中途断开 SSE 时会把服务搞崩。
            loop="agent_platform.core.event_loop:selector_loop_factory",
            timeout_keep_alive=120,
        )
        server = uvicorn.Server(config)
    except Exception as exc:  # noqa: BLE001
        return {"base_url": "", "mode": "failed",
                "detail": f"uvicorn 创建失败：{type(exc).__name__}: {exc}",
                "server": None, "embedded": False}

    thread = threading.Thread(target=server.run, name="desktop-embedded-uvicorn",
                               daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"

    deadline = time.time() + 45.0
    while time.time() < deadline:
        if _probe_any(base_url, timeout=2.0):
            return {
                "base_url": base_url,
                "mode": "embedded",
                "detail": f"已在桌面进程内启动后端（端口 {port}）",
                "server": server,
                "embedded": True,
            }
        if not thread.is_alive():
            break
        time.sleep(0.25)

    try:
        server.should_exit = True
    except Exception:
        pass
    return {"base_url": "", "mode": "failed",
            "detail": "内嵌后端启动超时（45s 内轻量端点未就绪）",
            "server": None, "embedded": False}


def _probe_any(base_url: str, timeout: float = 12.0) -> bool:
    """用**多个轻量端点**探一次 API 是否可达。

    探测顺序（任一返回 200 即视为可达）：

        1. ``GET /platform/status`` —— 本轮新增的「平台状态」端点，
           **纯内存计算、不探模型**，因此在模型冷启动 / BGE 预热期间
           也能毫秒级返回；
        2. ``GET /ping``        —— 纯 CPU 的轻量端点（后端提供时）；
        3. ``GET /health``      —— 最老、最重的端点，兼容没有前两者的旧后端。

    ★ 为什么要有多个候选：``/health`` 要探测两个本地模型端点 + 查知识库
      预热状态，最坏要 8~9 秒。桌面版在**冷启动**时如果只探 ``/health``，
      很容易误判「既有 API 不可达」而放弃复用、退化成内嵌启动。

    ★ 顺序也有讲究：把**最快最稳**的 ``/platform/status`` 放第一位，
      命中就立刻返回；只有它失败（老后端没有这个端点）才去试别的。
      一个端点都打不通时，最多花 ``timeout × 3`` 秒。

    Args:
        base_url: API 根地址。
        timeout: 单个端点的超时（秒）。

    Returns:
        任意一个端点返回 200 即视为可达。
    """
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    for path in ("/platform/status", "/ping", "/health"):
        url = base_url.rstrip("/") + path
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                if int(resp.status) == 200:
                    return True
        except urllib.error.HTTPError as exc:
            if int(exc.code) == 200:
                return True
        except Exception:
            continue
    return False


def main(argv: List[str] | None = None) -> int:
    """桌面版主流程。"""
    _fix_console_encoding()
    ap = argparse.ArgumentParser(
        prog="python -m agent_platform.desktop",
        description="Agent平台 桌面版工作台（pywebview + 复用现有 Web 前端）",
    )
    ap.add_argument("--api", default=DEFAULT_API_BASE,
                    help=f"既有 API 地址（默认 {DEFAULT_API_BASE}）")
    ap.add_argument("--no-embedded", action="store_true",
                    help="不允许在进程内启动后端（只用既有 API）")
    ap.add_argument("--browser-only", action="store_true",
                    help="强制使用默认浏览器打开（不尝试 pywebview 窗口）")
    ap.add_argument("--check", action="store_true",
                    help="只做环境自检并打印后端可达性，不打开窗口")
    ap.add_argument("--width", type=int, default=1440, help="窗口宽度（默认 1440）")
    ap.add_argument("--height", type=int, default=900, help="窗口高度（默认 900）")
    args = ap.parse_args(argv)

    # ★ ★ 单实例保护（2026-11-24）★★
    #   用户反馈「可能是我点了好几次桌面图标造成的」—— 确实如此。
    #   这里用 Win32 命名互斥量保证**同一时刻只有一个桌面实例**：
    #   第二次启动**不**开新窗口，而是把已有窗口置前然后自己退出。
    #
    #   ★ 位置极其重要：必须在 ``resolve_backend()`` **之前**。
    #     ``resolve_backend()`` 要探测 /health（含模型端点探活），
    #     最坏要 8~15 秒；如果把单实例检查放在它后面，
    #     用户连点两下时第二个进程还是得干等十几秒才退出 ——
    #     那就等于没做单实例保护。
    #
    #   ★ ``--check`` **不**走互斥量：它只是自检、不开窗口，
    #     属于「非 GUI 调用」，不应该被别的实例挡住（否则
    #     运维脚本就没法在实例运行时做健康检查了）。
    #   ★ ``--browser-only`` 也跳过：浏览器模式下本来就没有
    #     「Agent平台」原生窗口可供聚焦，强行单实例会挡住
    #     调试场景。
    singleton = {"acquired": True, "already_running": False, "focused": False}
    if not args.check and not args.browser_only:
        singleton = acquire_singleton()
        if singleton.get("already_running"):
            focused = focus_existing_window(WINDOW_TITLE)
            singleton["focused"] = focused
            if focused:
                _safe_print("")
                _safe_print("[单实例] 检测到已有「Agent平台」实例在运行，"
                            "已把它切到前台，本次启动结束。")
            else:
                _safe_print("")
                _safe_print("[单实例] 检测到已有实例在运行，但还没找到它的窗口"
                            "（可能仍在启动中），本次启动结束。")
            _safe_print("[单实例] 如需再开一个，请先关闭已有窗口。")
            return 0

    _print_banner()
    info = _print_env()

    _safe_print("")
    _safe_print("[2/3] 解析后端服务 ...")
    backend = resolve_backend(
        preferred=args.api,
        allow_embedded=not args.no_embedded,
    )
    _safe_print(f"  {backend['detail']}")
    # ★ 兜底：第一次没解析出后端时再给一次机会（更宽松的探测 + 重试内嵌）。
    #   这让「桌面版打开就能用」不依赖既有 API 恰好探测成功。
    if not backend.get("base_url") and not args.no_embedded:
        backend = _retry_embedded_backend(backend)
        if backend.get("base_url"):
            _safe_print(f"  {backend['detail']}")
    if not backend["base_url"]:
        _safe_print("")
        _safe_print("  !! 未能获取可用的后端地址，桌面版无法启动。")
        _safe_print("     排查建议：")
        _safe_print("       1) 确认 API 已启动：powershell -ExecutionPolicy Bypass -File deploy\\start_api.ps1 -Port 8010")
        _safe_print("       2) 或直接用源码方式运行本模块（会自动内嵌启动后端）")
        return 2

    base_url = backend["base_url"].rstrip("/")
    ui_url = f"{base_url}/ui/"

    _safe_print("")
    _safe_print("[3/3] 打开工作台 ...")
    _safe_print(f"  工作台地址 : {ui_url}")
    _safe_print(f"  后端模式   : {'复用既有 API' if backend['mode'] == 'reuse' else '本进程内嵌启动'}")
    _safe_print("")

    if args.check:
        _safe_print("  --check 模式：不打开窗口，已完成自检。")
        _safe_print(f"  pywebview 可用: {info['pywebview_installed']}")
        _safe_print(f"  前端可达    : {ui_url}")
        return 0

    if args.browser_only:
        ok = open_in_browser(ui_url)
        _safe_print("  已请求使用默认浏览器打开工作台。" if ok
              else "  自动打开浏览器失败，请手动访问上述地址。")
        # 浏览器模式下不阻塞；内嵌后端需要保持存活
        if backend.get("embedded"):
            _safe_print("  （内嵌后端保持运行，关闭本进程即停止）")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
        return 0

    result = open_desktop(
        ui_url,
        title="Agent平台",
        width=args.width,
        height=args.height,
    )
    _safe_print(f"  打开方式：{result['mode']} —— {result['detail']}")
    return 0 if result["mode"] == "webview" else 3


if __name__ == "__main__":
    sys.exit(main())
