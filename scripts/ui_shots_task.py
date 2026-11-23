# -*- coding: utf-8 -*-
"""开发辅助：仅抓「任务详情 tab」截图（需先有已完成的任务）。

用法：python scripts/ui_shots_task.py
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = os.path.join(REPO_ROOT, "data", "shots")
BASE = "http://127.0.0.1:8010/ui/"


def _launch(pw):
    for c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"):
        if os.path.exists(c):
            return pw.chromium.launch(executable_path=c, args=["--no-sandbox"])
    return pw.chromium.launch()


def main() -> int:
    os.makedirs(SHOTS, exist_ok=True)
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1600, "height": 940})
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(2000)
        page.click('.act-item[data-view="tasks"]')
        page.wait_for_timeout(2200)
        n = page.locator("#tasks-list .tree-row").count()
        print("任务行数:", n)
        if n:
            page.locator("#tasks-list .tree-row").first.click()
            page.wait_for_timeout(3000)
            page.screenshot(path=os.path.join(SHOTS, "10-tab-task-detail.png"))
            print("saved 10-tab-task-detail.png")
            # 滚到工具轨迹处再截一张
            page.locator(".page.active").evaluate(
                "el => { const h = el.querySelector('h3'); if (h) h.scrollIntoView(); }")
            page.wait_for_timeout(900)
            page.screenshot(path=os.path.join(SHOTS, "10b-tab-task-trace.png"))
            print("saved 10b-tab-task-trace.png")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
