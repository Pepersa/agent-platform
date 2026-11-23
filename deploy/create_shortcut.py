# -*- coding: utf-8 -*-
"""在桌面创建「Agent平台」快捷方式。

指向 ``dist\\desktop\\启动Agent平台.vbs``（**不是** exe、也不是 .bat）
—— 原因：
    · 直接指向 exe 会缺少 BGE 模型路径 / HF 离线 / 线程数等环境变量；
    · 指向 .bat 会**先闪一个 cmd 黑框**再拉起 exe（用户抱怨的「一下出来俩」）；
    · 指向 .vbs 则由 ``WScript.Shell.Run(..., 0, False)`` 以
      **隐藏窗口**方式启动，**全程零控制台**，只出现应用窗口一个。

用法：
    python deploy\\create_shortcut.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "dist" / "desktop" / "启动Agent平台.vbs"
#: .bat 保留作为调试用（需要看控制台输出时双击它）
FALLBACK_BAT = REPO_ROOT / "dist" / "desktop" / "启动Agent平台.bat"
#: ★ onedir 产物（2026-11-24）：exe 在**产物目录**里面，不再是平铺文件。
#:   快捷方式的图标取自这个 exe；VBS 启动器内部也指向同一个路径。
EXE = REPO_ROOT / "dist" / "desktop" / "AgentPlatform" / "AgentPlatform.exe"
SHORTCUT_NAME = "Agent平台.lnk"


def _powershell_escape(path: Path) -> str:
    """把路径安全地嵌入 PowerShell 单引号字符串。"""
    return str(path).replace("'", "''")


def main() -> int:
    if not LAUNCHER.exists():
        print(f"!! 找不到启动器：{LAUNCHER}", flush=True)
        print("   请先执行 deploy\\build_desktop.ps1 生成 exe。", flush=True)
        return 1

    desktop = Path(os.path.join(os.path.expanduser("~"), "Desktop"))
    if not desktop.is_dir():
        # 某些系统上 Desktop 被 OneDrive 重定向
        desktop = Path(
            os.environ.get("OneDriveDesktop")
            or os.environ.get("OneDriveConsumerDesktop")
            or desktop
        )
    shortcut = desktop / SHORTCUT_NAME

    ps = (
        "$s = (New-Object -ComObject WScript.Shell)"
        f".CreateShortcut('{_powershell_escape(shortcut)}');"
        f"$s.TargetPath = '{_powershell_escape(LAUNCHER)}';"
        "$s.WorkingDirectory = '" + _powershell_escape(LAUNCHER.parent) + "';"
        "$s.Description = 'Agent平台 桌面版工作台（静默启动，无控制台窗口）';"
        # ★ 图标取自 onedir 产物目录里的 exe；产物不存在时退回系统默认图标，
        #   免得因为图标路径失效导致整个快捷方式创建失败。
        f"$s.IconLocation = '{_powershell_escape(EXE)},0';"
        "$s.Save()"
    )
    rc = os.system(f'powershell -NoProfile -ExecutionPolicy Bypass -Command "{ps}"')
    if rc != 0 or not shortcut.exists():
        print("!! 创建快捷方式失败。", flush=True)
        return 2
    print(f"已创建桌面快捷方式：{shortcut}", flush=True)
    print(f"  指向：{LAUNCHER}", flush=True)
    print(f"  图标：{EXE}", flush=True)
    print(f"  （调试备用：{FALLBACK_BAT}）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
