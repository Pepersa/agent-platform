# -*- coding: utf-8 -*-
"""开发辅助：抓取交付所需的全部界面截图（Cursor 风格工作台）。

用法：python scripts/ui_shots.py
输出：data/shots/*.png
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = os.path.join(REPO_ROOT, "data", "shots")
BASE = "http://127.0.0.1:8010/ui/"


def _launch(pw):
    import shutil
    for c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if os.path.exists(c):
            return pw.chromium.launch(executable_path=c, args=["--no-sandbox"])
    return pw.chromium.launch()


def shot(page, name):
    path = os.path.join(SHOTS, name + ".png")
    page.screenshot(path=path)
    print("  saved", name + ".png")


def main() -> int:
    os.makedirs(SHOTS, exist_ok=True)
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1600, "height": 940},
                                device_scale_factor=1)
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(2500)

        print("截图中…")
        # 1. 整体布局（资源管理器视图）
        shot(page, "01-overview")

        # 2. 活动栏各视图
        for view, label in [("knowledge", "02-sidebar-knowledge"),
                            ("tools", "03-sidebar-tools"),
                            ("tasks", "04-sidebar-tasks"),
                            ("sessions", "05-sidebar-sessions"),
                            ("settings", "06-sidebar-settings")]:
            page.click(f'.act-item[data-view="{view}"]')
            page.wait_for_timeout(1400)
            shot(page, label)

        # 3. 自定义工具模态框
        page.click('.act-item[data-view="tools"]')
        page.wait_for_timeout(600)
        page.click("#btn-custom-tool-2")
        page.wait_for_timeout(1200)
        shot(page, "08-modal-custom-tool")
        page.click("#ct-close")
        page.wait_for_timeout(400)

        # 4. 知识库导入模态框
        page.click('.act-item[data-view="knowledge"]')
        page.wait_for_timeout(900)
        page.click("#btn-kb-import-2")
        page.wait_for_timeout(1000)
        shot(page, "09-modal-kb-import")
        page.click("#kb-close")
        page.wait_for_timeout(400)

        # 5. 任务详情 tab
        page.click('.act-item[data-view="tasks"]')
        page.wait_for_timeout(1800)
        if page.locator("#tasks-list .tree-row").count():
            page.locator("#tasks-list .tree-row").first.click()
            page.wait_for_timeout(2500)
            shot(page, "10-tab-task-detail")

        # 6. 知识库文档预览 tab
        page.click('.act-item[data-view="knowledge"]')
        page.wait_for_timeout(1600)
        if page.locator("#kb-doc-list .tree-row").count():
            page.locator("#kb-doc-list .tree-row").first.click()
            page.wait_for_timeout(2800)
            shot(page, "11-tab-kb-doc")

        # 7. 任务发布台抽屉
        page.click("#btn-tasks-toggle")
        page.wait_for_timeout(900)
        shot(page, "12-task-publisher-drawer")
        page.click("#btn-task-drawer-close")
        page.wait_for_timeout(400)

        # 8. 对话流（真实发送）
        page.locator(".tab", has_text="对话").first.click()
        page.wait_for_timeout(500)
        page.fill("#input", "T101_VDD_FUNCT 的上下限是多少？请引用来源。")
        page.press("#input", "Enter")
        page.wait_for_timeout(2500)
        shot(page, "13-chat-streaming")
        for _ in range(90):
            if page.locator(".msg.assistant .meta b").count():
                break
            page.wait_for_timeout(2000)
        page.wait_for_timeout(2500)
        shot(page, "14-chat-answer")

        browser.close()
    print("完成。截图目录:", SHOTS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
