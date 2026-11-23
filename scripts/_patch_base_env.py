# -*- coding: utf-8 -*-
"""把各回归脚本里硬编码的 8010 端口改成可用环境变量覆盖。

背景（2026-11-23）：
    本工作区同时有**另一个会话**在跑同一套回归脚本，它会周期性
    ``kill_stale_tests.py`` + 重启 8010 上的 API —— 于是正在跑的这一轮
    会被 ``ReadError: [WinError 10054]`` / ``ConnectError 10061`` 打断。

    这些假失败跟被测代码**无关**，纯粹是端口抢占。
    把端口抽成环境变量后，两边可以各跑各的，互不干扰。

用法：
    python scripts/_patch_base_env.py          # 打补丁
    AGENT_API_BASE=http://127.0.0.1:8021 AGENT_UI_BASE=http://127.0.0.1:8021/ui/ \
        python scripts/_obs_suite_runner.py
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

#: (文件名, 旧行片段, 新行片段)
PATCHES = [
    ("verify_web_ui.py",
     'BASE = "http://127.0.0.1:8010"',
     'BASE = os.environ.get("AGENT_API_BASE", "http://127.0.0.1:8010").rstrip("/")'),
    ("verify_kb_admin.py",
     'BASE = "http://127.0.0.1:8010"',
     'BASE = os.environ.get("AGENT_API_BASE", "http://127.0.0.1:8010").rstrip("/")'),
    ("verify_desktop.py",
     'BASE = "http://127.0.0.1:8010"',
     'BASE = os.environ.get("AGENT_API_BASE", "http://127.0.0.1:8010").rstrip("/")'),
    ("verify_modes.py",
     'BASE = "http://127.0.0.1:8010"',
     'BASE = os.environ.get("AGENT_API_BASE", "http://127.0.0.1:8010").rstrip("/")'),
    ("verify_trace_view.py",
     'BASE = "http://127.0.0.1:8010"',
     'BASE = os.environ.get("AGENT_API_BASE", "http://127.0.0.1:8010").rstrip("/")'),
    ("verify_workspace.py",
     'BASE = "http://127.0.0.1:8010"',
     'BASE = os.environ.get("AGENT_API_BASE", "http://127.0.0.1:8010").rstrip("/")'),
    ("verify_retrieval_policy.py",
     'BASE = "http://127.0.0.1:8010"',
     'BASE = os.environ.get("AGENT_API_BASE", "http://127.0.0.1:8010").rstrip("/")'),
    ("verify_kb_rebuild_persistence.py",
     'BASE = "http://127.0.0.1:8010"',
     'BASE = os.environ.get("AGENT_API_BASE", "http://127.0.0.1:8010").rstrip("/")'),
    ("verify_observability.py",
     'url = os.environ.get("AGENT_UI", "http://127.0.0.1:8010/ui/")',
     'url = os.environ.get("AGENT_UI",\n                      os.environ.get("AGENT_UI_BASE", "http://127.0.0.1:8010/ui/"))'),
    ("ui_style_check.py",
     'BASE = "http://127.0.0.1:8010/ui/"',
     'BASE = os.environ.get("AGENT_UI_BASE", "http://127.0.0.1:8010/ui/")'),
    ("ui_overlap_check.py",
     'BASE = "http://127.0.0.1:8010/ui/"',
     'BASE = os.environ.get("AGENT_UI_BASE", "http://127.0.0.1:8010/ui/")'),
    ("ui_check.py",
     'BASE = "http://127.0.0.1:8010/ui/"',
     'BASE = os.environ.get("AGENT_UI_BASE", "http://127.0.0.1:8010/ui/")'),
    ("ui_input_check.py",
     'BASE = "http://127.0.0.1:8010/ui/"',
     'BASE = os.environ.get("AGENT_UI_BASE", "http://127.0.0.1:8010/ui/")'),
    ("ui_ws_check.py",
     'BASE = os.environ.get("AGENTPLAT_UI_BASE", "http://127.0.0.1:8010/ui/")',
     'BASE = os.environ.get("AGENT_UI_BASE",\n                      os.environ.get("AGENTPLAT_UI_BASE", "http://127.0.0.1:8010/ui/"))'),
]

#: verify_desktop.py 里还有一处写死的 health URL
DESKTOP_CFG_OLD = '"config": {"url": "http://127.0.0.1:8010/health"},'
DESKTOP_CFG_NEW = '"config": {"url": BASE + "/health"},'


def ensure_import_os(text: str) -> str:
    """确保文件顶部有 ``import os``。"""
    if re.search(r"^import os$", text, re.M):
        return text
    # 放在第一个 ``import <mod>`` 之前，保持字母序风格
    m = re.search(r"^import ", text, re.M)
    if not m:
        return text
    return text[:m.start()] + "import os\n" + text[m.start():]


def main() -> int:
    changed = []
    for fname, old, new in PATCHES:
        p = SCRIPTS / fname
        if not p.is_file():
            print(f"[skip] {fname} 不存在")
            continue
        text = p.read_text(encoding="utf-8")
        if old not in text:
            print(f"[skip] {fname}: 目标行未找到（可能已打过补丁）")
            continue
        text = text.replace(old, new, 1)
        text = ensure_import_os(text)
        p.write_text(text, encoding="utf-8")
        changed.append(fname)
        print(f"[ok] {fname}")

    # verify_desktop.py 的 health URL
    dp = SCRIPTS / "verify_desktop.py"
    if dp.is_file():
        t = dp.read_text(encoding="utf-8")
        if DESKTOP_CFG_OLD in t:
            dp.write_text(t.replace(DESKTOP_CFG_OLD, DESKTOP_CFG_NEW, 1), encoding="utf-8")
            print("[ok] verify_desktop.py health URL")
    print(f"\n共修改 {len(changed)} 个文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
