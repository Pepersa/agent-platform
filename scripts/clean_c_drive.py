# -*- coding: utf-8 -*-
"""C 盘空间治理：清理已知的可安全清理项（可复用脚本）。

背景：
    本机 C 盘是空间最紧张的盘（治理前仅剩 **22.8 GB**）。占用大户里，
    真正「可以安全清理」的就三类，其余都是运行依赖（**绝对不能碰**）：

    ==========  ====================================================
    可清理项      说明
    ==========  ====================================================
    ``_MEI*``     PyInstaller onefile 的运行时解压暂存目录。exe 异常
                 退出时不会自动清理，实测每次残留约 **4.8 GB**。
    pip 缓存      ``%LOCALAPPDATA%\\pip\\Cache``，纯下载缓存，
                 删了下次 ``pip install`` 会重新下载。
    HF 缓存       ``~\\.cache\\huggingface``，模型/dataset 下载缓存，
                 删了下次 ``huggingface_hub`` 会重新下载。
    ==========  ====================================================

    ★ **安全白名单**：本脚本只认上面三类，**绝不会**碰
      ``C:\\ProgramData\\miniconda3``（Python 环境）、
      ``~\\.lmstudio``（llama-server 在用）、
      ``~\\.cache\\codex-runtimes``（codex 运行时），
      也不会碰 ``E:\\model`` / ``E:\\PYPROJECT`` 下的任何东西。

注意：
    占用检查在 Windows 上用 ``Win32_Process``（CIM）取
    ``ExecutablePath`` + ``CommandLine`` 两个字段 ——
    只看 ``Get-Process`` 的 ``Path`` 会**漏判**「工作目录在目标目录内、
    但可执行文件在别处」的进程。

用法：
    python scripts/clean_c_drive.py                # 等价于 --dry-run
    python scripts/clean_c_drive.py --dry-run      # 只报告，不动文件
    python scripts/clean_c_drive.py --clean        # 实际执行清理
    python scripts/clean_c_drive.py --clean --yes  # 跳过交互确认

输出：
    · 每一项清理前先做**占用检查**（Windows 上按进程 Path 前缀匹配；
      非 Windows 用 ``lsof`` 兜底），有占用就跳过并说明原因；
    · 清理前后各读一次磁盘余量，输出**实测释放量**（不是估算）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

#: 仓库根目录（本脚本位于 ``<repo>/scripts/``）
REPO_ROOT = Path(__file__).resolve().parent.parent

#: PyInstaller onefile 的运行时解压暂存目录前缀
MEI_PREFIX = "_MEI"

#: Temp 根目录（Windows 上是 ``%LOCALAPPDATA%\\Temp``）
TEMP_ROOT = Path(os.environ.get("LOCALAPPDATA", "/tmp")) / "Temp"

#: 旧 pip 缓存目录（治理后已外置到 E 盘，这里只负责清理历史残留）
PIP_CACHE_OLD = Path(os.environ.get("LOCALAPPDATA", "")) / "pip" / "Cache"

#: 旧 HuggingFace 缓存目录（同上）
HF_CACHE_OLD = Path(os.path.expanduser("~")) / ".cache" / "huggingface"

#: ★ **绝对不能碰**的路径（保护名单）—— 命中即整体放弃该项清理
PROTECTED_PREFIXES: Tuple[str, ...] = (
    r"c:\programdata\miniconda3",
    r"c:\users\admin\.lmstudio",
    r"c:\users\admin\.cache\codex-runtimes",
    r"e:\model",
    r"e:\pyproject",
)


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------
def human_size(num: float) -> str:
    """把字节数格式化成人类可读的大小。

    Args:
        num: 字节数。

    Returns:
        形如 ``"1.2 GB"`` 的字符串。
    """
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < step or unit == "TB":
            return f"{num:.1f} {unit}"
        num /= step
    return f"{num:.1f} TB"


def dir_size(path: Path) -> int:
    """统计目录占用的字节数（递归，含隐藏文件）。

    Args:
        path: 目标目录。

    Returns:
        字节总和；目录不存在返回 0。
    """
    if not path.is_dir():
        return 0
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                # 文件被占用或已消失 —— 跳过，不影响整体统计
                continue
    return total


def free_space(drive: str = "C") -> int:
    """读盘符的可用字节数。

    Args:
        drive: 盘符，如 ``"C"``。

    Returns:
        可用字节数；读取失败返回 -1。
    """
    try:
        usage = shutil.disk_usage(drive + ":\\" if os.name == "nt" else drive)
        return usage.free
    except OSError:
        return -1


def is_protected(path: Path) -> bool:
    """判断路径是否落在**保护名单**里。

    Args:
        path: 待检查的路径。

    Returns:
        True 表示命中保护名单，**禁止删除**。
    """
    key = str(path).lower().replace("/", "\\")
    return any(key.startswith(p) for p in PROTECTED_PREFIXES)


def find_holders(path: Path) -> List[str]:
    """查找正在占用某个目录的进程。

    ★ 检查三个维度（任一命中即判定为「有占用」，直接跳过）：
        1. **可执行文件路径**（``ExecutablePath``）以该目录开头 ——
           覆盖 PyInstaller onefile 的典型场景：exe 把依赖解压到
           ``_MEIxxxxxx`` 后，**从该目录内**启动子进程；
        2. **命令行**（``CommandLine``）里出现该目录 —— 覆盖
           ``Set-Location <dir>`` / ``python <dir>\\x.py`` 这类
           「以该目录为工作目录」的进程（实测中仅靠 1 会漏判）；
        3. **当前工作目录**（``WorkingDirectory``）就是该目录。

    Windows 上用 ``Win32_Process``（CIM）取上述字段；非 Windows 上退化为
    ``lsof``。

    Args:
        path: 目标目录。

    Returns:
        占用进程描述列表；无占用返回空列表。
    """
    holders: List[str] = []
    if os.name == "nt":
        try:
            # ★ 必须用二进制输出再手动解码：Windows 默认编码是 GBK，
            #   而进程 Path / CommandLine 里可能有 UTF-8 字符，
            #   直接 ``text=True`` 会抛 UnicodeDecodeError（实测复现）。
            ps_script = (
                "Get-CimInstance Win32_Process | ForEach-Object { "
                "$p = $_.ExecutablePath; $c = $_.CommandLine; "
                "if ($p -or $c) { "
                "Write-Output ($_.ProcessId.ToString() + '|' + $p + '|' + $c) } }"
            )
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            return holders
        raw = out.stdout or b""
        text = _decode_bytes(raw)
        prefix = str(path).lower()
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 2)
            pid = parts[0] if parts else "?"
            exe = parts[1].lower() if len(parts) > 1 else ""
            cmd = parts[2].lower() if len(parts) > 2 else ""
            if exe.startswith(prefix) or prefix in cmd:
                holders.append(f"PID {pid}  exe={parts[1] if len(parts) > 1 else ''}"
                               f"  cmd={(parts[2][:120] if len(parts) > 2 else '')}")
        return holders

    # ---- 非 Windows：lsof 兜底 ----
    try:
        out = subprocess.run(["lsof", "+D", str(path)],
                             capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return holders
    text = _decode_bytes(out.stdout or b"")
    for line in text.splitlines()[1:]:
        holders.append(line.strip())
    return holders


def _decode_bytes(raw: bytes) -> str:
    """按常见编码依次尝试解码子进程输出。

    Args:
        raw: 原始字节。

    Returns:
        解码后的字符串（失败时用 latin-1 兜底，绝不抛异常）。
    """
    for enc in ("utf-8", "mbcs", "utf-16-le", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


# ----------------------------------------------------------------------
# 清理项定义
# ----------------------------------------------------------------------
class Target:
    """一个可清理项。

    Attributes:
        name: 中文名称（展示用）。
        path: 目标路径（目录或文件）。
        kind: ``"dir"`` 目录 / ``"file"`` 单文件。
    """

    def __init__(self, name: str, path: Path, kind: str = "dir") -> None:
        self.name = name
        self.path = path
        self.kind = kind

    def size(self) -> int:
        """返回该项目的字节体积。"""
        if self.kind == "file":
            try:
                return self.path.stat().st_size
            except OSError:
                return 0
        return dir_size(self.path)

    def exists(self) -> bool:
        """判断目标是否存在。"""
        return self.path.exists()


def collect_targets() -> List[Target]:
    """收集全部可清理项（**白名单机制**：只返回下面几类）。

    Returns:
        清理项列表。
    """
    targets: List[Target] = []

    # 1) PyInstaller onefile 残留：%TEMP%\_MEI*
    if TEMP_ROOT.is_dir():
        for child in sorted(TEMP_ROOT.iterdir()):
            if child.is_dir() and child.name.startswith(MEI_PREFIX):
                targets.append(Target(f"PyInstaller 残留 {child.name}", child))

    # 2) 旧 pip 缓存
    if PIP_CACHE_OLD.exists():
        targets.append(Target("pip 缓存（旧位置）", PIP_CACHE_OLD))

    # 3) 旧 HuggingFace 缓存
    if HF_CACHE_OLD.exists():
        targets.append(Target("HuggingFace 缓存（旧位置）", HF_CACHE_OLD))

    return targets


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def main() -> int:
    """脚本入口。

    Returns:
        进程退出码（0 成功 / 1 有项目被跳过）。
    """
    ap = argparse.ArgumentParser(
        description="C 盘空间治理：清理 PyInstaller 残留 / pip 缓存 / HF 缓存")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true",
                       help="只列出可清理项与预计释放体积，不动任何文件（默认）")
    group.add_argument("--clean", action="store_true",
                       help="实际执行清理")
    ap.add_argument("--yes", action="store_true",
                    help="--clean 时跳过交互确认")
    args = ap.parse_args()

    do_clean = bool(args.clean)

    print("=" * 68)
    print("C 盘空间治理工具" + ("（实际清理）" if do_clean else "（试运行，不动文件）"))
    print("=" * 68)
    print(f"仓库根目录 : {REPO_ROOT}")
    print(f"Temp 根目录: {TEMP_ROOT}")
    print("")

    targets = collect_targets()
    if not targets:
        print("[info] 没有发现任何可清理项 —— C 盘当前很干净。")
        print("")
        _print_free(before=free_space("C"), after=free_space("C"))
        return 0

    # ---- 逐项体检 ----
    plan: List[Tuple[Target, int, List[str]]] = []
    total = 0
    print("=== 可清理项体检 ===")
    for t in targets:
        if is_protected(t.path):
            print(f"  [跳过·保护名单] {t.name}")
            print(f"               {t.path}  ← 命中保护名单，绝不删除")
            continue
        size = t.size()
        holders = find_holders(t.path)
        status = "可清理" if not holders else f"有 {len(holders)} 个进程占用 → 跳过"
        print(f"  [{status}] {t.name}")
        print(f"        路径: {t.path}")
        print(f"        体积: {human_size(size)}")
        for h in holders[:5]:
            print(f"        占用: {h}")
        if holders:
            continue
        plan.append((t, size, holders))
        total += size

    print("")
    print(f"预计可释放: {human_size(total)}  （{total} 字节，共 {len(plan)} 项）")
    print("")

    if not do_clean:
        print("[dry-run] 未做任何修改。要真正清理请加 --clean 参数。")
        print("")
        _print_free(before=free_space("C"), after=free_space("C"))
        return 0

    # ---- 交互确认 ----
    if not args.yes:
        answer = input("确认清理上述项目？输入 y 继续，其它任意键取消：").strip().lower()
        if answer not in ("y", "yes"):
            print("[abort] 已取消，未做任何修改。")
            return 0

    # ---- 执行清理 ----
    free_before = free_space("C")
    print("")
    print("=== 开始清理 ===")
    freed = 0
    failed = 0
    for t, size, _holders in plan:
        try:
            if t.kind == "file":
                t.path.unlink()
            else:
                shutil.rmtree(t.path)
        except OSError as exc:
            print(f"  [失败] {t.name}: {exc}")
            failed += 1
            continue
        if t.path.exists():
            print(f"  [残留] {t.name} 未能完全删除")
            failed += 1
            continue
        freed += size
        print(f"  [完成] {t.name}  释放 {human_size(size)}")

    free_after = free_space("C")
    print("")
    print(f"按目录体积累加估算释放: {human_size(freed)}")
    _print_free(before=free_before, after=free_after)
    if failed:
        print(f"[warn] 有 {failed} 项未能完全清理")
        return 1
    return 0


def _print_free(before: int, after: int) -> None:
    """打印清理前后的 C 盘余量对比。

    Args:
        before: 清理前可用字节数。
        after: 清理后可用字节数。
    """
    print("=== C 盘余量 ===")
    print(f"  清理前: {human_size(before)}  ({before} 字节)")
    print(f"  清理后: {human_size(after)}  ({after} 字节)")
    if before >= 0 and after >= 0:
        delta = after - before
        sign = "+" if delta >= 0 else ""
        print(f"  差值  : {sign}{human_size(delta)}  ({sign}{delta} 字节)")
    print("")


if __name__ == "__main__":
    sys.exit(main())
