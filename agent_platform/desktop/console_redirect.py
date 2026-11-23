# -*- coding: utf-8 -*-
"""无控制台（``--window``）模式下的 stdout / stderr 重定向。

为什么需要这个模块
------------------
PyInstaller 的 ``--window``（静默 / windowed）产物**不带控制台子系统**：
进程被创建时根本没有 ``CONOUT$``，此时 CPython 里的
``sys.stdout`` / ``sys.stderr`` 是 **None**。

后果非常致命：本项目在启动路径上大量使用 ``print()``（banner、
环境自检、后端解析、窗口位置……）。一旦 stdout 是 None，
``print()`` 内部会执行 ``sys.stdout.write(...)`` →
``AttributeError: 'NoneType' object has no attribute 'write'`` →
**应用在还没弹窗之前就直接崩掉**。

所以这里提供一个"兜底流"：把 stdout/stderr 换成**写日志文件**的流，
既保证 ``print()`` 不炸，又让用户事后还能看到启动诊断信息。

日志文件位置的优先级
--------------------
1. ``%LOCALAPPDATA%\\AgentPlatform\\logs\\desktop.log``
   —— 按用户隔离、无需管理员权限、一定是可写目录（首选）；
2. ``AGENT_DATA_DIR`` 环境变量指向目录下的 ``logs\\``；
3. 仓库根目录下的 ``data\\logs\\``（源码运行时的默认位置）；
4. 上面全都不行 → 退化成**空写入器**（``NullWriter``），
   应用照常启动，只是没有日志。

设计要点
--------
* **幂等**：重复调用不会叠加包装，也不会重复打开文件；
* **绝不抛异常**：所有失败都吞掉，最坏落到 ``NullWriter``；
* **行缓冲**：日志文件用 ``line_buffering=True``，
  这样用户随时 ``Get-Content -Wait`` 都能看到最新的启动进度；
* **UTF-8**：避免中文日志在记事本里乱码。
"""

from __future__ import annotations

import io
import os
import sys
from typing import Any, List, Optional, Tuple

__all__ = [
    "NullWriter",
    "desktop_log_path",
    "install_stream_redirect",
    "ensure_streams",
]

#: 已安装的重定向标记（避免重复包装 / 重复打开文件）
_INSTALLED = False

#: 实际使用的日志文件路径（``None`` 表示退化到 NullWriter）
_LOG_PATH: Optional[str] = None


class NullWriter(io.TextIOBase):
    """什么都不写的流。

    ★ 为什么要专门造一个：``sys.stdout`` 必须是**可写**的对象，
      否则任何 ``print()`` / ``sys.stderr.write()`` 都会抛
      ``AttributeError``。这个类把"写"变成彻底的无操作，
      保证即使日志文件一条都写不进去，应用也能正常起来。
    """

    def write(self, s: str) -> int:  # noqa: D102 - io 基类契约
        return len(s) if s else 0

    def writable(self) -> bool:  # noqa: D102
        return True

    def flush(self) -> None:  # noqa: D102
        return None

    def isatty(self) -> bool:  # noqa: D102
        return False

    def fileno(self) -> int:  # noqa: D102
        # 明确不支持 fd —— 有些库会试探 fileno()，给个错比给个假的好
        raise OSError("NullWriter has no file descriptor")

    @property
    def encoding(self) -> str:  # noqa: D102
        return "utf-8"


def _repo_root() -> str:
    """取仓库根目录（源码模式下的兜底日志位置）。"""
    # agent_platform/desktop/console_redirect.py -> 上溯三级
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _candidate_dirs() -> List[str]:
    """按优先级列出候选日志目录。"""
    candidates: List[str] = []

    # ① %LOCALAPPDATA%\AgentPlatform\logs —— 每用户可写，无需管理员
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(os.path.join(local_app_data, "AgentPlatform", "logs"))

    # ② AGENT_DATA_DIR 指向的目录下的 logs\
    data_dir = os.environ.get("AGENT_DATA_DIR")
    if data_dir:
        candidates.append(os.path.join(data_dir, "logs"))

    # ③ 仓库根目录下的 data\logs（源码运行）
    candidates.append(os.path.join(_repo_root(), "data", "logs"))

    # ④ 系统临时目录（最后的救命稻草）
    candidates.append(os.path.join(os.environ.get("TEMP", "."), "AgentPlatform", "logs"))

    return candidates


def desktop_log_path() -> Optional[str]:
    """返回当前可用的桌面日志文件路径；``None`` 表示连文件都开不了。

    会**实际创建**目录（以及父目录），并用 ``a``（追加）模式打开一次
    以确认可写 —— 只在第一次调用时真正建目录，之后直接返回缓存路径。
    """
    global _LOG_PATH
    if _LOG_PATH is not None:
        return _LOG_PATH

    for directory in _candidate_dirs():
        try:
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, "desktop.log")
            # 试开一次，确认这个目录真的可写（不要真的写内容进去）
            with open(path, "a", encoding="utf-8"):
                pass
            _LOG_PATH = path
            return _LOG_PATH
        except Exception:
            # 目录建不了 / 没权限 → 试下一个候选
            continue
    return None


def _open_log_stream(path: str) -> Optional[io.TextIOBase]:
    """以追加模式打开日志文件；失败返回 None。"""
    try:
        return open(path, "a", encoding="utf-8", errors="replace",
                    buffering=1, newline="\n")
    except Exception:
        return None


def ensure_streams() -> Tuple[Any, Any]:
    """确保 ``sys.stdout`` / ``sys.stderr`` **不是 None**。

    这是最轻量的一层保护：``--window`` 模式下这两个流可能是 None，
    任何 ``print()`` 都会炸。这里把它们替换成可写的替身
    （有日志文件就写文件，没有就用 ``NullWriter``）。

    Returns:
        ``(stdout, stderr)`` —— 当前生效的两个流对象。
    """
    if sys.stdout is None:
        sys.stdout = NullWriter()
    if sys.stderr is None:
        sys.stderr = NullWriter()
    return sys.stdout, sys.stderr


def install_stream_redirect() -> Optional[str]:
    """把 stdout / stderr 重定向到桌面日志文件（静默模式的**总开关**）。

    必须在**任何** ``print()`` 之前调用 —— 推荐在
    ``entry_point.py`` 里作为**第一件事**（甚至在 import 桌面包之前）。

    ★ 即使已经有控制台（例如源码模式 ``python -m agent_platform.desktop``），
      这里也**不**去动已有的 stdout —— 只在"没有控制台"时才接管，
      这样源码模式的命令行输出行为完全不变。

    Returns:
        实际使用的日志文件路径；退化成 ``NullWriter`` 时返回 ``None``。
    """
    global _INSTALLED, _LOG_PATH
    if _INSTALLED:
        return _LOG_PATH

    # ★ 先判断"有没有控制台"，**再**替换流 —— 顺序不能反：
    #   如果先用 ensure_streams() 把 None 换成 NullWriter，
    #   后面的"是否无控制台"判断就永远看不到 None 了，
    #   结果是日志文件**永远不会**被接管。
    console_absent = _is_console_absent()

    # 保证两个流都不是 None（哪怕后面日志文件开不了，print 也不会炸）
    ensure_streams()

    # 已经有真正的控制台 → 不接管，保持命令行下的原有行为
    if not console_absent:
        _INSTALLED = True
        return None

    path = desktop_log_path()
    if not path:
        # 连日志文件都开不了 → 保持 NullWriter，应用照常启动
        _INSTALLED = True
        return None

    stream = _open_log_stream(path)
    if stream is None:
        _INSTALLED = True
        return None

    # stdout 与 stderr 写同一个文件；stderr 用行缓冲保证异常栈实时可见
    sys.stdout = stream
    sys.stderr = stream
    _LOG_PATH = path
    _INSTALLED = True

    try:
        stream.write(
            "\n===== Agent平台 桌面版启动 "
            f"({__import__('datetime').datetime.now().isoformat(timespec='seconds')}) =====\n"
        )
        stream.flush()
    except Exception:
        pass
    return path


def _is_console_absent() -> bool:
    """判断当前是否**没有**可用的输出目标（需要接管重定向）。

    ★ 判定标准非常简单，也非常可靠：
      ``sys.stdout is None`` —— 这就是 PyInstaller ``--window`` 的**唯一**
      特征（进程没有控制台子系统，CPython 拿不到流对象）。

    ★ 为什么**不**用 ``os.isatty(fd)`` 之类的判断：
      - ``os.isatty()`` 为 False 的情况**远不止**"没有控制台"这一种
        （stdout 被重定向到文件 / 管道时也是 False）；
      - 源码模式下 ``python -m agent_platform.desktop`` 有真正的控制台，
        此时 ``isatty()`` 为 True，但我们要**保持控制台输出不变**；
      - 而"stdout 被重定向到文件"的场景**不需要**我们接管 ——
        用户本来就有输出目标，日志照样看得见。
      所以只有"压根没有流对象"才接管。

    Returns:
        True 表示"必须接管到日志文件"。
    """
    if sys.stdout is None:
        return True
    if isinstance(sys.stdout, NullWriter):
        # 之前已经被 ensure_streams() 兜底成 NullWriter —— 说明原本就没有
        return True
    return False
