# -*- coding: utf-8 -*-
"""统一的数据目录解析（**可外置到任意盘符**）。

背景：
    平台运行期会产生大量落盘数据（SQLite 库 / 向量索引 / 审计日志 /
    截图产物 / 自定义模型注册表）。这些文件原本**硬编码**在
    ``<repo>/data/`` 下，导致：

        · 换盘（C 盘 → D 盘）必须搬整个项目目录；
        · 项目目录可能被放进只读 / 网络盘 / 版本控制目录；
        · 大文件（会话库 / 知识库索引）污染代码仓库。

本模块提供**单一数据目录解析入口**，让所有运行时数据文件都能通过
一个环境变量整体外置：

    ``AGENT_DATA_DIR``  —— 指向数据根目录（任意盘符均可）

优先级（从高到低）：
    1. **显式参数**（函数入参 ``override``）—— 供测试 / 脚本临时覆盖；
    2. **环境变量** ``AGENT_DATA_DIR`` —— 用户配置的主入口；
    3. **项目默认** ``<repo_root>/data`` —— **与改造前完全一致**（向后兼容）。

设计约束（**重要**）：
    1. **纯标准库**（不引入第三方依赖）；
    2. **默认值不变**：不设 ``AGENT_DATA_DIR`` 时，路径与改造前逐字节一致，
       现有测试与已有数据（如 ``verify-custom-48f74a`` 模型条目）不丢；
    3. **不自动建目录**于路径解析阶段（``data_dir()`` 只做解析），
       需要写文件时由调用方或 :func:`ensure_data_dirs` 负责；
    4. 目录不存在时**自动创建**（幂等），创建失败**降级为不报错**，
       由后续写文件操作抛出真实错误（避免在只读环境下直接崩在启动阶段）。

子目录约定（与改造前 ``data/`` 下的一级子目录一一对应）：
    ``kb/`` 知识库（chunks.db / chunk_manifest.json / uploads/）
    ``audit/`` 审计（audit.db / audit.jsonl）
    ``docs/`` 知识库原始文档
    ``reports/`` 报告产物
    ``outbox/`` 通知发件箱
    ``shots/`` / ``obs_shots/`` / ``ui_ws_shots/`` / ``eval_reports/`` 产物
    ``_regress/`` 回归测试输出
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional, Tuple

__all__ = [
    "ENV_DATA_DIR",
    "DEFAULT_DATA_DIR_NAME",
    "repo_root",
    "default_data_dir",
    "data_dir",
    "data_path",
    "kb_dir",
    "audit_dir",
    "ensure_data_dirs",
    "data_dir_info",
    "is_external_data_dir",
    "reset_cache",
]

#: 数据目录环境变量名（用户配置主入口）
ENV_DATA_DIR = "AGENT_DATA_DIR"

#: 数据目录在项目下的默认目录名
DEFAULT_DATA_DIR_NAME = "data"

#: 需要确保存在的**子目录**（相对数据根目录）
#:
#: 说明：``scripts/`` **不在此列** —— 它保存用户自定义工具脚本，
#: 历史上以仓库根相对路径（``data/scripts/demo_tool.py``）被引用，
#: 迁移时默认**保留在原处**（见 ``scripts/migrate_data_dir.py``）。
SUBDIRS: Tuple[str, ...] = (
    "kb",
    "kb/uploads",
    "audit",
    "docs",
    "reports",
    "outbox",
    "shots",
    "obs_shots",
    "ui_ws_shots",
    "eval_reports",
    "_regress",
)

#: 目录解析缓存（进程内）：``(原始值) -> 解析后的 Path``
#:
#: ⚠️ **缓存只是为了避免重复 IO**（``resolve()`` 在 Windows 上不便宜）。
#: 环境变量在进程生命周期内通常不变；测试里若需要动态切换，
#: 调用 :func:`reset_cache` 清空即可。
_CACHE: dict = {}


# ----------------------------------------------------------------------
# 仓库根目录
# ----------------------------------------------------------------------
def repo_root() -> Path:
    """返回仓库根目录（``agent_platform/core`` 的上两级）。

    与改造前各模块里的 ``Path(__file__).resolve().parent.parent.parent``
    完全等价（同一份推导逻辑，只是收拢到一处）。
    """
    return Path(__file__).resolve().parent.parent.parent


def default_data_dir() -> Path:
    """返回**项目默认**数据目录：``<repo_root>/data``。

    这是**向后兼容的锚点** —— 不设 ``AGENT_DATA_DIR`` 时，
    所有路径与改造前完全一致。
    """
    return repo_root() / DEFAULT_DATA_DIR_NAME


# ----------------------------------------------------------------------
# 核心解析
# ----------------------------------------------------------------------
def data_dir(override: Optional[str | os.PathLike] = None,
             *, create: bool = False) -> Path:
    """返回数据根目录。

    优先级（从高到低）：
        1. ``override`` 显式参数（测试 / 脚本临时覆盖）；
        2. 环境变量 ``AGENT_DATA_DIR``；
        3. 项目默认 ``<repo_root>/data``。

    路径会被 ``expanduser()`` + ``resolve()`` 绝对化（Windows 上会展开
    ``~`` 与相对路径）。

    Args:
        override: 显式覆盖（优先级最高）。
        create: 是否在目录不存在时**自动创建**（幂等）。

    Returns:
        数据根目录（绝对路径）。
    """
    raw: Optional[str] = None
    if override is not None and str(override).strip():
        raw = str(override).strip()
    else:
        env = os.environ.get(ENV_DATA_DIR, "").strip()
        if env:
            raw = env

    if not raw:
        base = default_data_dir()
    else:
        cached = _CACHE.get(raw)
        if cached is not None:
            base = cached
        else:
            base = Path(raw).expanduser()
            try:
                base = base.resolve()
            except Exception:  # pragma: no cover - 极端路径（非法字符等）
                base = base.absolute()
            _CACHE[raw] = base

    if create:
        try:
            base.mkdir(parents=True, exist_ok=True)
        except Exception:  # pragma: no cover - 只读盘等；后续写操作会报错
            pass
    return base


def data_path(*parts: str, override: Optional[str | os.PathLike] = None) -> Path:
    """返回数据目录下的子路径。

    用法::

        paths.data_path("sessions.db")            # -> <data>/sessions.db
        paths.data_path("kb", "chunks.db")        # -> <data>/kb/chunks.db

    Args:
        parts: 相对数据根目录的路径片段（可多段）。
        override: 显式覆盖数据根目录。

    Returns:
        拼好的绝对路径（**不创建**目录）。
    """
    base = data_dir(override)
    if not parts:
        return base
    return base.joinpath(*[str(p) for p in parts])


def kb_dir(override: Optional[str | os.PathLike] = None) -> Path:
    """知识库子目录（默认 ``<data>/kb``）。

    存放 ``chunks.db`` / ``chunk_manifest.json`` / ``uploads/`` 等。
    """
    return data_path("kb", override=override)


def audit_dir(override: Optional[str | os.PathLike] = None) -> Path:
    """审计子目录（默认 ``<data>/audit``）。

    存放 ``audit.db`` / ``audit.jsonl``。
    """
    return data_path("audit", override=override)


# ----------------------------------------------------------------------
# 目录创建
# ----------------------------------------------------------------------
def ensure_data_dirs(override: Optional[str | os.PathLike] = None,
                     subdirs: Optional[Iterable[str]] = None) -> Path:
    """确保数据根目录与各子目录存在（**幂等**）。

    单独抽出来的原因：路径解析阶段**不应该**有副作用（否则
    「只想算个路径」也会在磁盘上建目录），真正要写文件时再调用。

    Args:
        override: 显式覆盖数据根目录。
        subdirs: 要额外确保存在的子目录（缺省用 :data:`SUBDIRS`）。

    Returns:
        数据根目录（绝对路径）。
    """
    base = data_dir(override, create=True)
    targets = list(subdirs) if subdirs is not None else list(SUBDIRS)
    for sub in targets:
        try:
            (base / sub).mkdir(parents=True, exist_ok=True)
        except Exception:  # pragma: no cover - 只读盘等
            pass
    return base


# ----------------------------------------------------------------------
# 诊断信息
# ----------------------------------------------------------------------
def is_external_data_dir(override: Optional[str | os.PathLike] = None) -> bool:
    """当前数据目录是否**已被外置**（即不是项目默认目录）。

    用于前端 / 健康检查展示「数据目录现在在哪」。
    """
    try:
        return data_dir(override) != default_data_dir()
    except Exception:  # pragma: no cover
        return False


def data_dir_info(override: Optional[str | os.PathLike] = None) -> dict:
    """返回数据目录诊断信息（``/health`` / 前端展示用）。

    Returns:
        ``{dir, exists, external, source, subdirs}``；``source`` 取值
        ``env`` / ``override`` / ``default``。
    """
    env = os.environ.get(ENV_DATA_DIR, "").strip()
    has_override = override is not None and str(override).strip()
    source = "override" if has_override else ("env" if env else "default")
    base = data_dir(override)
    try:
        exists = base.is_dir()
    except Exception:  # pragma: no cover
        exists = False
    return {
        "dir": str(base),
        "exists": exists,
        "external": is_external_data_dir(override),
        "source": source,
        "subdirs": list(SUBDIRS),
    }


def reset_cache() -> None:
    """清空路径解析缓存（测试里动态改环境变量时用）。"""
    _CACHE.clear()
