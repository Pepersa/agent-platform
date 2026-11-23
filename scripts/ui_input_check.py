# -*- coding: utf-8 -*-
"""开发辅助：验证输入框的 Enter/Shift+Enter/IME 行为（真实按键派发）。

覆盖：
  1. Enter                → 发送（清空输入框 + 追加用户消息）
  2. Shift+Enter          → 换行（**不**发送）
  3. Ctrl+Enter           → 发送
  4. IME 组字中（isComposing / keyCode 229）→ 不发送
  5. Ctrl+B               → 切换侧栏
  6. Ctrl+W               → 关闭当前 tab
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("AGENT_UI_BASE", "http://127.0.0.1:8010/ui/")

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""),
          flush=True)


def _launch(pw):
    for c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"):
        if os.path.exists(c):
            return pw.chromium.launch(executable_path=c, args=["--no-sandbox"])
    return pw.chromium.launch()


def main() -> int:
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(1800)

        print("=== 1. Enter 发送 ===")
        page.fill("#input", "测试回车发送")
        page.press("#input", "Enter")
        page.wait_for_timeout(1200)
        check("Enter 后输入框清空",
              page.eval_on_selector("#input", "el => el.value === ''"),
              repr(page.eval_on_selector("#input", "el => el.value")))
        check("Enter 后出现用户消息",
              page.locator(".msg.user", has_text="测试回车发送").count() >= 1)

        print("\n=== 2. Shift+Enter 换行 ===")
        page.fill("#input", "第一行")
        page.press("#input", "Shift+Enter")
        page.wait_for_timeout(400)
        val = page.eval_on_selector("#input", "el => el.value")
        check("Shift+Enter 后内容仍在输入框", val.startswith("第一行"), repr(val))
        check("Shift+Enter 未发送（无新用户消息）",
              page.locator(".msg.user", has_text="第一行").count() == 0)

        print("\n=== 3. Ctrl+Enter 发送 ===")
        # 等上一条消息跑完（state.busy 期间 send() 会直接返回，这是正确行为）
        for _ in range(90):
            if page.eval_on_selector("#btn-send", "el => !el.disabled"):
                break
            page.wait_for_timeout(1000)
        page.fill("#input", "测试Ctrl回车")
        page.press("#input", "Control+Enter")
        page.wait_for_timeout(1200)
        check("Ctrl+Enter 后输入框清空",
              page.eval_on_selector("#input", "el => el.value === ''"))
        check("Ctrl+Enter 后出现用户消息",
              page.locator(".msg.user", has_text="测试Ctrl回车").count() >= 1)

        print("\n=== 4. IME 保护（isComposing / keyCode 229）===")
        # 真实 IME 难以在无头环境模拟，这里直接派发带 isComposing=true 的合成
        # KeyboardEvent，验证处理函数确实会因 IME 状态而**不**发送。
        res = page.evaluate(r"""
        (() => {
          const inp = document.getElementById('input');
          const before = document.querySelectorAll('.msg.user').length;
          inp.value = '拼音组字中';
          const ev = new KeyboardEvent('keydown', {
            key: 'Enter', code: 'Enter', bubbles: true, cancelable: true,
            isComposing: true, keyCode: 229, which: 229,
          });
          // 旧构造器无法设置 isComposing，这里用 defineProperty 强制置位
          Object.defineProperty(ev, 'isComposing', {value: true});
          Object.defineProperty(ev, 'keyCode', {value: 229});
          inp.dispatchEvent(ev);
          const after = document.querySelectorAll('.msg.user').length;
          const stillThere = inp.value === '拼音组字中';
          return {before, after, stillThere};
        })()
        """)
        check("IME 组字中按 Enter 不发送", res["after"] == res["before"],
              f"{res['before']} -> {res['after']}")
        check("IME 组字中输入内容保留", res["stillThere"])

        # keyCode === 229 但 isComposing=false 的情况也要拦住
        res2 = page.evaluate(r"""
        (() => {
          const inp = document.getElementById('input');
          const before = document.querySelectorAll('.msg.user').length;
          inp.value = 'keycode229';
          const ev = new KeyboardEvent('keydown', {
            key: 'Enter', code: 'Enter', bubbles: true, cancelable: true,
          });
          Object.defineProperty(ev, 'isComposing', {value: false});
          Object.defineProperty(ev, 'keyCode', {value: 229});
          inp.dispatchEvent(ev);
          return {before, after: document.querySelectorAll('.msg.user').length};
        })()
        """)
        check("keyCode===229 时也不发送", res2["after"] == res2["before"],
              f"{res2['before']} -> {res2['after']}")

        print("\n=== 5. Ctrl+B 切换侧栏 ===")
        vis_before = page.locator("#sidebar").is_visible()
        page.keyboard.press("Control+b")
        page.wait_for_timeout(400)
        vis_after = page.locator("#sidebar").is_visible()
        check("Ctrl+B 切换侧栏可见性", vis_before != vis_after,
              f"{vis_before} -> {vis_after}")
        page.keyboard.press("Control+b")
        page.wait_for_timeout(300)

        print("\n=== 6. Ctrl+W 关闭 tab ===")
        page.click('.act-item[data-view="tasks"]')
        page.wait_for_timeout(1500)
        if page.locator("#tasks-list .tree-row").count():
            page.locator("#tasks-list .tree-row").first.click()
            page.wait_for_timeout(1500)
            n_before = page.locator(".tab").count()
            page.keyboard.press("Control+w")
            page.wait_for_timeout(600)
            n_after = page.locator(".tab").count()
            check("Ctrl+W 关闭当前 tab", n_after == n_before - 1, f"{n_before} -> {n_after}")
        else:
            check("Ctrl+W 关闭当前 tab（无任务可开，跳过）", True, "skipped")

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
