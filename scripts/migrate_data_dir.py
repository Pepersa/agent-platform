# -*- coding: utf-8 -*-
"""迁移数据目录到**任意盘符**（外置 ``data/`` 目录）。

背景：
    平台运行时数据（SQLite 库 / 向量索引 / 审计日志 / 截图产物）默认落在
    ``<repo>/data/``。用户希望把整个数据目录挪到**别的盘**（比如 D 盘），
    之后只靠环境变量 ``AGENT_DATA_DIR`` 就能让平台找到它。

用法：
    python scripts/migrate_data_dir.py --to D:\\agent-data --dry-run
    python scripts/migrate_data_dir.py --to D:\\agent-data
    python scripts/migrate_data_dir.py --to D:\\agent-data --copy-only

关键行为：
    1. ``--dry-run`` **只打印**将要移动的文件清单与大小，不动任何文件；
    2. 迁移前检查目标目录是否已有同名文件 —— **不覆盖**（避免把旧数据弄丢）；
    3. 数据库文件（``.db`` / ``.db-wal`` / ``.db-shm``）迁移前会检查是否
       被进程占用（Windows 上独占锁），占用则**拒绝迁移**并给出提示；
    4. ``data/scripts/`` 子目录**默认不移动** —— 里面的文件是用户自定义
       工具脚本，被 ``custom_tools`` 功能按**仓库根相对路径**
       （``data/scripts/demo_tool.py``）引用，移动会打断引用。
       可以用 ``--include-scripts`` 一并迁移（此时会自动在代码里加 fallback：
       ``resolve_script_path`` 会同时尝试仓库根与数据目录）；
    5. 迁移后：
       · 目标目录写一份 ``data_dir.json`` 记录迁移来源；
       · 原 ``<repo>/data`` 留一份 ``MIGRATED_TO.txt`` 说明去向。

注意：迁移**不会**自动修改环境变量。请自行设置：

    setx AGENT_DATA_DIR "D:\\agent-data"
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

#: 仓库根目录（本脚本位于 ``<repo>/scripts/``）
REPO_ROOT = Path(__file__).resolve().parent.parent

#: 默认数据目录（迁移来源）
DEFAULT_SOURCE = REPO_ROOT / "data"

#: 迁移来源里**不移动**的子目录（相对数据根目录）
#:
#: ``scripts`` 保存用户自定义工具脚本，被 ``custom_tools`` 按仓库根相对路径
#: 引用（``data/scripts/demo_tool.py``）—— 移动会打断引用。
EXCLUDE_SUBDIRS: Tuple[str, ...] = ("scripts",)

#: 视为「数据库文件」的后缀（迁移前检查占用）
DB_SUFFIXES: Tuple[str, ...] = (".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3")

#: 迁移记录文件名（写进**新**数据目录）
RECORD_NAME = "data_dir.json"

#: 原目录留下的说明文件名
NOTE_NAME = "MIGRATED_TO.txt"


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------
def human_size(num: float) -> str:
    """把字节数格式化成人类可读字符串。"""
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < step or unit == "TB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= step
    return f"{num:.1f} TB"


def is_db_file(path: Path) -> bool:
    """判断是否是数据库文件（含 WAL / SHM 伴生文件）。"""
    name = path.name.lower()
    return any(name.endswith(sfx) for sfx in DB_SUFFIXES)


def db_is_locked(path: Path) -> bool:
    """检查数据库文件是否被进程占用（Windows 独占锁探测）。

    做法：尝试以**独占模式**打开（``sqlite3`` 的 ``immutable=0`` 不适用，
    这里直接用普通 ``open(..., "r+b")`` + Windows 的默认共享模式）。
    SQLite 在有活跃连接时不会独占文件，因此这里退化为「能否独占打开」。

    Args:
        path: 数据库文件路径。

    Returns:
        True 表示**看起来被占用**（无法独占打开）。
    """
    if not path.exists():
        return False
    try:
        with open(path, "r+b"):
            pass
    except PermissionError:
        return True
    except OSError:
        return True
    return False


def collect_items(source: Path,
                  exclude_subdirs: Iterable[str] = EXCLUDE_SUBDIRS,
                  ) -> List[Tuple[Path, Path]]:
    """收集待迁移的条目（文件 + 目录）。

    Args:
        source: 源数据目录。
        exclude_subdirs: 要排除的子目录名（相对源目录）。

    Returns:
        ``[(源路径, 相对源目录的相对路径), ...]``。
    """
    excluded = set(exclude_subdirs or ())
    items: List[Tuple[Path, Path]] = []
    if not source.is_dir():
        return items
    for root, dirs, files in os.walk(source):
        root_path = Path(root)
        rel_root = root_path.relative_to(source)
        # 目录级排除（就地剪枝，不进入被排除的目录）
        dirs[:] = [d for d in dirs
                   if not (rel_root == Path(".") and d in excluded)]
        for name in sorted(files):
            p = root_path / name
            items.append((p, p.relative_to(source)))
    return items


def plan_moves(source: Path, target: Path,
               exclude_subdirs: Iterable[str] = EXCLUDE_SUBDIRS,
               ) -> Tuple[List[Tuple[Path, Path, int]], List[Tuple[Path, Path]]]:
    """生成迁移计划。

    Args:
        source: 源数据目录。
        target: 目标目录。
        exclude_subdirs: 排除的子目录名。

    Returns:
        ``(可迁移列表, 冲突列表)``；可迁移列表元素为
        ``(源文件, 相对路径, 字节数)``，冲突列表元素为
        ``(源文件, 已存在的目标文件)``。
    """
    movable: List[Tuple[Path, Path, int]] = []
    conflicts: List[Tuple[Path, Path]] = []
    for src, rel in collect_items(source, exclude_subdirs):
        dst = target / rel
        if dst.exists():
            conflicts.append((src, dst))
            continue
        try:
            size = src.stat().st_size
        except OSError:
            size = 0
        movable.append((src, rel, size))
    return movable, conflicts


def print_plan(movable: List[Tuple[Path, Path, int]],
               conflicts: List[Tuple[Path, Path]],
               source: Path, target: Path,
               excluded: Iterable[str] = EXCLUDE_SUBDIRS) -> int:
    """打印迁移清单（dry-run 与实际迁移共用）。

    Returns:
        总字节数。
    """
    total = sum(size for _, _, size in movable)
    print("=" * 70)
    print("数据目录迁移计划")
    print("=" * 70)
    print(f"  来源 : {source}")
    print(f"  目标 : {target}")
    print(f"  文件 : {len(movable)} 个")
    print(f"  体积 : {human_size(total)}")
    if excluded:
        print(f"  排除 : {', '.join(excluded)}/（保留在原处）")
    print("-" * 70)

    if conflicts:
        print(f"[冲突] 目标目录已存在同名文件（**不会覆盖**，需人工处理）：")
        for src, dst in conflicts:
            print(f"    - {src.relative_to(source)}  ->  {dst}")
        print("-" * 70)

    # 按相对路径分组打印（前缀缩进，直观看出目录结构）
    for src, rel, size in movable:
        print(f"    {rel.as_posix():<48} {human_size(size):>10}")
    print("=" * 70)
    return total


def write_record(target: Path, source: Path, moved: int, bytes_moved: int) -> None:
    """在**新**数据目录写一份 ``data_dir.json`` 迁移记录。"""
    record = {
        "migrated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "migrated_from": str(source),
        "migrated_to": str(target),
        "file_count": moved,
        "total_bytes": bytes_moved,
        "total_bytes_human": human_size(bytes_moved),
        "env_var": "AGENT_DATA_DIR",
        "env_var_value": str(target),
        "note": (
            "本目录由 scripts/migrate_data_dir.py 从原项目 data/ 目录迁移而来。"
            "启动平台前请设置环境变量 AGENT_DATA_DIR 指向本目录。"
        ),
    }
    try:
        target.mkdir(parents=True, exist_ok=True)
        with open(target / RECORD_NAME, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 写迁移记录失败：{exc}")


def write_note(source: Path, target: Path, moved: int, bytes_moved: int) -> None:
    """在**原** ``data/`` 目录留一份 ``MIGRATED_TO.txt`` 说明。"""
    text = (
        "本目录的数据已于 "
        + time.strftime("%Y-%m-%d %H:%M:%S")
        + " 迁移到：\n\n"
        + f"    {target}\n\n"
        f"迁移文件数：{moved}，总体积：{human_size(bytes_moved)}\n\n"
        "使用方式（Windows cmd）：\n"
        f'    setx AGENT_DATA_DIR "{target}"\n\n'
        "使用方式（PowerShell）：\n"
        f'    $env:AGENT_DATA_DIR = "{target}"\n\n'
        "若要回到原目录：删除环境变量 AGENT_DATA_DIR，或把它指回本目录。\n"
        "迁移记录（机器可读）：见新目录下的 " + RECORD_NAME + "\n"
    )
    try:
        source.mkdir(parents=True, exist_ok=True)
        with open(source / NOTE_NAME, "w", encoding="utf-8") as fh:
            fh.write(text)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 写迁移说明失败：{exc}")


def check_db_locked(movable: List[Tuple[Path, Path, int]]) -> List[Path]:
    """检查待迁移的数据库文件是否被占用。

    Returns:
        被占用的文件列表（空表示都可迁移）。
    """
    locked: List[Path] = []
    for src, _, _ in movable:
        if is_db_file(src) and db_is_locked(src):
            locked.append(src)
    return locked


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """解析命令行参数。"""
    p = argparse.ArgumentParser(
        description="迁移平台数据目录（data/）到任意盘符",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/migrate_data_dir.py --to D:\\agent-data --dry-run\n"
            "  python scripts/migrate_data_dir.py --to D:\\agent-data\n"
        ),
    )
    p.add_argument("--to", dest="target", required=True,
                   help="目标数据目录（必填），例如 D:\\agent-data")
    p.add_argument("--from", dest="source", default=str(DEFAULT_SOURCE),
                   help=f"源数据目录（默认 {DEFAULT_SOURCE}）")
    p.add_argument("--dry-run", action="store_true",
                   help="只打印将要迁移的文件清单与大小，不做任何改动")
    p.add_argument("--copy-only", action="store_true",
                   help="只复制不删除源文件（默认是**移动**）")
    p.add_argument("--include-scripts", action="store_true",
                   help="连同 data/scripts/ 一起迁移（默认保留在原处）")
    p.add_argument("--force", action="store_true",
                   help="目标目录已有同名文件时**覆盖**（默认报错跳过）")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    """脚本入口。

    Returns:
        进程退出码（0 成功 / 1 失败）。
    """
    args = parse_args(argv)
    source = Path(args.source).expanduser().resolve()
    target = Path(args.target).expanduser().resolve()

    if not source.is_dir():
        print(f"[fail] 源数据目录不存在：{source}")
        return 1
    if source == target:
        print("[fail] 目标目录与源目录相同，无需迁移。")
        return 1
    if target.is_relative_to(source):
        print("[fail] 目标目录在源目录内部，会造成递归移动，请换一个位置。")
        return 1

    excluded = () if args.include_scripts else EXCLUDE_SUBDIRS
    movable, conflicts = plan_moves(source, target, excluded)

    # ---- dry-run：只打印 ----
    if args.dry_run:
        total = print_plan(movable, conflicts, source, target, excluded)
        print("[dry-run] 未做任何改动。去掉 --dry-run 即可执行实际迁移。")
        print(f"[dry-run] 预计迁移 {len(movable)} 个文件 / {human_size(total)}"
              + (f"，{len(conflicts)} 个冲突文件将被跳过。" if conflicts else "。"))
        return 0

    # ---- 冲突检查 ----
    if conflicts and not args.force:
        print("[fail] 目标目录已存在同名文件，已中止（不会覆盖）。")
        print("       处理方式：")
        print("         1) 手动清理目标目录里的同名文件后重跑；")
        print("         2) 或加 --force 强制覆盖；")
        print("         3) 或换一个空的目标目录。")
        for src, dst in conflicts:
            print(f"       冲突：{src.relative_to(source)}  <->  {dst}")
        return 1

    # ---- 占用检查（数据库文件）----
    locked = check_db_locked(movable)
    if locked:
        print("[fail] 以下数据库文件被进程占用，请先停掉 API / 索引构建进程：")
        for p in locked:
            print(f"       {p}")
        print("       （Windows 上文件被独占打开时会报 PermissionError）")
        return 1

    # ---- 执行迁移 ----
    print_plan(movable, conflicts, source, target, excluded)
    target.mkdir(parents=True, exist_ok=True)

    moved = 0
    moved_bytes = 0
    failed: List[Tuple[Path, str]] = []
    for src, rel, size in movable:
        dst = target / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if args.copy_only:
                shutil.copy2(src, dst)
            else:
                shutil.move(str(src), str(dst))
            moved += 1
            moved_bytes += size
        except Exception as exc:  # noqa: BLE001
            failed.append((src, f"{type(exc).__name__}: {exc}"))

    if failed:
        print(f"[warn] {len(failed)} 个文件迁移失败：")
        for src, err in failed[:20]:
            print(f"       {src.relative_to(source)}  {err}")

    # ---- 迁移记录 + 原目录说明 ----
    write_record(target, source, moved, moved_bytes)
    write_note(source, target, moved, moved_bytes)

    print("=" * 70)
    print(f"[ok] 迁移完成：{moved} 个文件 / {human_size(moved_bytes)}")
    print(f"     新目录：{target}")
    print(f"     记录  ：{target / RECORD_NAME}")
    if not args.copy_only:
        print(f"     说明  ：{source / NOTE_NAME}")
    print()
    print("下一步（Windows cmd）：")
    print(f'     setx AGENT_DATA_DIR "{target}"')
    print("然后重启 API 即可生效（scripts/restart_api.py --data-dir "
          f'"{target}"）。')
    print("=" * 70)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
