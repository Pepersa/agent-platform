# -*- coding: utf-8 -*-
"""开发辅助：用 Playwright 驱动真实 UI，截图 + 校验 IDE 三段式布局。

用法：
    python scripts/ui_check.py            # 截图 + 布局断言
    python scripts/ui_check.py --chat     # 额外跑一次真实对话（较慢）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = os.path.join(REPO_ROOT, "data", "shots")
BASE = os.environ.get("AGENT_UI_BASE", "http://127.0.0.1:8010/ui/")

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""),
          flush=True)
    return bool(cond)


def box(page, sel):
    return page.eval_on_selector(
        sel,
        "el => { const b = el.getBoundingClientRect();"
        " return {x: Math.round(b.x), y: Math.round(b.y),"
        " w: Math.round(b.width), h: Math.round(b.height)}; }")


def _launch(pw):
    """优先用已安装的 Chrome / Edge（无需下载 Playwright 自带浏览器）。"""
    import glob

    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return pw.chromium.launch(executable_path=c, args=["--no-sandbox"])
    return pw.chromium.launch()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", action="store_true")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    args = ap.parse_args()

    os.makedirs(SHOTS, exist_ok=True)
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": args.width, "height": args.height},
                                device_scale_factor=1)
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append("PAGEERROR: " + str(e)))

        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(1200)

        print("\n=== 1. 整体布局（标题栏 / 活动栏 / 侧栏 / 主区）===")
        check("标题为「Agent平台」", page.title() == "Agent平台", page.title())
        tb = box(page, ".titlebar")
        ab = box(page, ".activitybar")
        sb = box(page, "#sidebar")
        mn = box(page, "#main")
        print("  titlebar:", tb, "activitybar:", ab, "sidebar:", sb, "main:", mn)
        check("标题栏在顶部（高 30~40px）", 28 <= tb["h"] <= 42, str(tb["h"]))
        check("活动栏在最左（宽约 48px）", ab["x"] == 0 and 40 <= ab["w"] <= 56, str(ab))
        check("侧栏紧邻活动栏", sb["x"] == ab["w"], f"{sb['x']} vs {ab['w']}")
        check("主区在侧栏右侧", mn["x"] >= sb["x"] + sb["w"] - 1, str(mn["x"]))
        check("侧栏宽度约 260px", 200 <= sb["w"] <= 340, str(sb["w"]))
        check("主区占据剩余宽度", mn["w"] > 700, str(mn["w"]))
        check("无横向溢出",
              page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"),
              str(page.evaluate("document.documentElement.scrollWidth")))

        print("\n=== 2. 标签页 / 输入区 ===")
        check("存在标签页栏", page.locator(".tabbar").count() == 1)
        check("有「对话」标签", page.locator(".tab", has_text="对话").count() >= 1)
        check("存在输入框 #input", page.locator("#input").count() == 1)
        check("存在发送按钮 #btn-send", page.locator("#btn-send").count() == 1)
        check("存在 composer 表单", page.locator("#composer").count() == 1)
        check("存在 #tools-list", page.locator("#tools-list").count() == 1)
        check("存在 #tasks-list", page.locator("#tasks-list").count() == 1)
        check("存在 #kb-doc-list", page.locator("#kb-doc-list").count() == 1)
        check("存在 #tpl-select", page.locator("#tpl-select").count() == 1)
        check("存在 #sessions-list", page.locator("#sessions-list").count() == 1)
        check("存在 #h-endpoints", page.locator("#h-endpoints").count() == 1)

        print("\n=== 3. 活动栏切换（一次只显示一个视图）===")
        views = ["knowledge", "tools", "tasks", "sessions", "settings", "explorer"]
        for v in views:
            page.click(f'.act-item[data-view="{v}"]')
            page.wait_for_timeout(700)
            visible = page.eval_on_selector_all(
                ".view", "els => els.filter(e => !e.hidden).map(e => e.id)")
            ok = visible == [f"view-{v}"]
            check(f"活动栏切换到 {v}：仅显示该视图", ok, str(visible))
            page.screenshot(path=os.path.join(SHOTS, f"view-{v}.png"))

        print("\n=== 4. 侧栏拖拽改宽度 + 持久化 ===")
        before = box(page, "#sidebar")["w"]
        handle = page.locator("#rs-left")
        hb = handle.bounding_box()
        page.mouse.move(hb["x"] + 2, hb["y"] + 200)
        page.mouse.down()
        page.mouse.move(hb["x"] + 92, hb["y"] + 200, steps=12)
        page.mouse.up()
        page.wait_for_timeout(400)
        after = box(page, "#sidebar")["w"]
        print(f"  宽度 {before} -> {after}")
        check("拖拽后侧栏变宽", after > before + 40, f"{before} -> {after}")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(900)
        after_reload = box(page, "#sidebar")["w"]
        check("刷新后宽度保持", abs(after_reload - after) <= 3,
              f"{after} -> {after_reload}")

        print("\n=== 5. 模态框 ===")
        page.click('.act-item[data-view="tools"]')
        page.wait_for_timeout(500)
        page.click("#btn-custom-tool-2")
        page.wait_for_timeout(700)
        check("自定义工具模态框打开", page.locator("#ct-mask").is_visible())
        check("模态框内含名称/Schema/试运行",
              page.locator("#ct-name").count() == 1
              and page.locator("#ct-schema").count() == 1
              and page.locator("#ct-test").count() == 1)
        page.screenshot(path=os.path.join(SHOTS, "modal-custom-tool.png"))
        page.click("#ct-close")
        page.wait_for_timeout(400)

        page.click('.act-item[data-view="knowledge"]')
        page.wait_for_timeout(600)
        page.click("#btn-kb-import-2")
        page.wait_for_timeout(700)
        check("知识库导入模态框打开", page.locator("#kb-mask").is_visible())
        check("导入模态框含上传/粘贴两个 tab",
              page.locator("#kb-tab-upload").count() == 1
              and page.locator("#kb-tab-text").count() == 1)
        page.screenshot(path=os.path.join(SHOTS, "modal-kb-import.png"))
        page.click("#kb-close")
        page.wait_for_timeout(400)

        print("\n=== 6. 任务详情 tab ===")
        page.click('.act-item[data-view="tasks"]')
        page.wait_for_timeout(1200)
        # 任务表是**进程内**的（重启即清空），所以这里先真实发布一个任务，
        # 保证「任务列表有内容」这条断言检查的是**真实数据**而不是空列表。
        print("  发布一个任务作为断言前提…")
        created = page.evaluate("""async () => {
          const r = await fetch('/tasks', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
              name: 'ui_check 自检任务',
              description: '用一句话说明什么是索引。',
              prompt: '用一句话说明什么是索引。',
              model: 'gpt-oss-20b',
              params: {max_rounds: 2, max_tokens: 256}
            })
          });
          return {status: r.status, body: await r.text()};
        }""")
        print("  POST /tasks →", created.get("status"))
        for _ in range(40):
            page.wait_for_timeout(500)
            n_tasks = page.locator("#tasks-list .tree-row").count()
            if n_tasks > 0:
                break
        n_tasks = page.locator("#tasks-list .tree-row").count()
        print("  任务行数:", n_tasks)
        check("任务列表有内容（真实发布的任务）", n_tasks > 0, str(n_tasks))
        if n_tasks:
            page.locator("#tasks-list .tree-row").first.click()
            page.wait_for_timeout(2000)
            tabs = page.locator(".tab").count()
            check("点任务打开新标签页", tabs >= 2, f"tab 数={tabs}")
            check("任务详情页含基本信息",
                  page.locator('.page[data-page] .td-kv-row').count() > 0)
            page.screenshot(path=os.path.join(SHOTS, "tab-task-detail.png"))
            # 关掉该 tab
            page.locator(".tab.active .tab-close").click()
            page.wait_for_timeout(500)
            check("关闭标签页生效", page.locator(".tab").count() == tabs - 1)

        print("\n=== 7. 知识库文档 tab ===")
        page.click('.act-item[data-view="knowledge"]')
        page.wait_for_timeout(1500)
        n_docs = page.locator("#kb-doc-list .tree-row").count()
        print("  文档数:", n_docs)
        if n_docs:
            page.locator("#kb-doc-list .tree-row").first.click()
            page.wait_for_timeout(2500)
            check("文档预览 tab 已打开", page.locator(".tab").count() >= 2)
            page.screenshot(path=os.path.join(SHOTS, "tab-kb-doc.png"))

        if args.chat:
            print("\n=== 8. 真实对话（Enter 发送 / SSE / 引用 chip / 工具行）===")
            page.click('.act-item[data-view="explorer"]')
            page.wait_for_timeout(400)
            page.locator(".tab", has_text="对话").first.click()
            page.wait_for_timeout(400)
            page.fill("#input", "T101_VDD_FUNCT 的上下限是多少？请引用来源。")
            page.press("#input", "Enter")
            page.wait_for_timeout(1500)
            check("输入框已清空（Enter 发送）",
                  page.eval_on_selector("#input", "el => el.value === ''"))
            check("出现用户消息", page.locator(".msg.user").count() >= 1)
            # 等待助手回答
            for _ in range(90):
                if page.locator(".msg.assistant .cites .cite").count() > 0:
                    break
                if page.locator(".msg.assistant .meta b").count() > 0:
                    break
                page.wait_for_timeout(2000)
            page.wait_for_timeout(2000)
            check("助手消息已渲染", page.locator(".msg.assistant").count() >= 1)
            check("有引用 chip", page.locator(".msg.assistant .cites .cite").count() > 0,
                  str(page.locator(".msg.assistant .cites .cite").count()))
            check("有工具调用折叠行", page.locator(".msg.assistant .tool-row").count() > 0,
                  str(page.locator(".msg.assistant .tool-row").count()))
            check("无气泡式消息", page.locator(".msg .bubble").count() == 0)
            page.screenshot(path=os.path.join(SHOTS, "chat-stream.png"))

        print("\n=== 9. 控制台错误 ===")
        real = [e for e in errors if "favicon" not in e.lower()]
        check("无 JS 运行时错误", not real, str(real[:4]))

        browser.close()

    print("\n" + "=" * 64)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"结果: {passed}/{len(RESULTS)} 通过")
    for n, ok, d in RESULTS:
        if not ok:
            print(f"  失败: {n}  {d}")
    print("=" * 64)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
