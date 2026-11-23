# -*- coding: utf-8 -*-
"""开发辅助：为本轮新功能拍摄交付用截图（菜单 / 模型 / 模式 / 计划 / 历史 / 摘要 / 视觉）。

用法：
    python scripts/ui_newfeatures_shots.py
输出：data/shots/ 下 nf-*.png
"""
from __future__ import annotations

import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = os.path.join(REPO_ROOT, "data", "shots")
BASE = "http://127.0.0.1:8010/ui/"

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""),
          flush=True)
    return bool(cond)


def wait_idle(page, timeout_ms=180000):
    """等待前端不再 busy（state.busy === false）。

    **为什么需要**：``send()`` 在 ``state.busy`` 为 true 时会**静默丢弃**消息，
    如果测试脚本不等待上一轮结束，后续请求根本不会发出（表现为"卡片不出现"）。
    """
    page.wait_for_function("() => typeof state === 'undefined' || !state.busy",
                           timeout=timeout_ms)
    page.wait_for_timeout(400)


def wait_delta(page, selector, before, timeout_ms=180000, step=500):
    """等待选择器数量相对 before **增加**（而不是"总数 >= N"）。"""
    page.wait_for_function(
        "([sel, n]) => document.querySelectorAll(sel).length > n",
        arg=[selector, before], timeout=timeout_ms)
    page.wait_for_timeout(step)


def send_and_wait(page, text, sel=".msg.assistant", timeout_ms=180000):
    """发送一条消息并等待回答出现（返回新增回答数）。"""
    wait_idle(page)
    before = page.locator(sel).count()
    page.fill("#input", text)
    page.press("#input", "Enter")
    wait_delta(page, sel, before, timeout_ms=timeout_ms)
    wait_idle(page)
    return page.locator(sel).count() - before


def _launch(pw):
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
    os.makedirs(SHOTS, exist_ok=True)
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1600, "height": 950},
                                device_scale_factor=1)
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append("PAGEERROR: " + str(e)))

        # 加随机 query 串避免命中浏览器缓存（旧 app.js/workbench.js 会导致假失败）
        page.goto(BASE + "?t=" + str(int(time.time())), wait_until="networkidle")
        page.wait_for_timeout(1800)

        # ---------------------------------------------------------- 1. 三个菜单
        print("\n=== 1. 标题栏菜单（每个 item 都真实执行）===")
        for key, label in (("file", "文件"), ("edit", "编辑"), ("view", "视图")):
            page.click(f'.tb-menu-item[data-menu="{key}"]')
            page.wait_for_timeout(450)
            n = page.locator("#menu-popover .menu-item").count()
            visible = not page.locator("#menu-popover").is_hidden()
            check(f"{label}菜单展开且有菜单项", visible and n > 0,
                  f"items={n} visible={visible}")
            page.screenshot(path=os.path.join(SHOTS, f"nf-menu-{key}.png"))
            # 记录菜单项文字（证明不是空壳）
            labels = page.eval_on_selector_all(
                "#menu-popover .menu-item .mi-label",
                "els => els.map(e => e.textContent.trim())")
            print(f"    {label}菜单项: {labels}")
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            check(f"{label}菜单 Esc 可关闭",
                  page.locator("#menu-popover").is_hidden())

        # Alt+F 快捷键
        page.keyboard.press("Alt+f")
        page.wait_for_timeout(400)
        check("Alt+F 唤起「文件」菜单",
              not page.locator("#menu-popover").is_hidden())
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)

        # 「视图 → 切换侧栏显隐」真实生效
        w0 = page.eval_on_selector("#sidebar", "el => el.getBoundingClientRect().width")
        page.click('.tb-menu-item[data-menu="view"]')
        page.wait_for_timeout(350)
        page.locator("#menu-popover .menu-item", has_text="切换侧栏显隐").click()
        page.wait_for_timeout(600)
        w1 = page.eval_on_selector("#sidebar", "el => el.getBoundingClientRect().width")
        check("视图→切换侧栏显隐 真的生效", w0 != w1, f"{w0} -> {w1}")
        page.screenshot(path=os.path.join(SHOTS, "nf-menu-sidebar-toggled.png"))
        # 恢复
        page.click('.tb-menu-item[data-menu="view"]')
        page.wait_for_timeout(300)
        page.locator("#menu-popover .menu-item", has_text="切换侧栏显隐").click()
        page.wait_for_timeout(500)

        # 「文件 → 新建会话」真实生效（清空当前 tab）
        page.fill("#input", "这是一条将被清空的临时消息")
        page.wait_for_timeout(200)
        page.click('.tb-menu-item[data-menu="file"]')
        page.wait_for_timeout(300)
        page.locator("#menu-popover .menu-item", has_text="新建会话").click()
        page.wait_for_timeout(600)
        check("文件→新建会话 清空了输入框",
              page.eval_on_selector("#input", "el => el.value === ''"))

        # ---------------------------------------------------------- 2. 模型选择器
        print("\n=== 2. 模型选择器 + 自定义模型 CRUD ===")
        # 幂等：先删掉可能残留的同名自定义模型
        page.evaluate("""async () => {
          try {
            const r = await fetch('/models');
            const j = await r.json();
            for (const m of (j.models||[])) {
              if (m.name === 'gpt-oss-20b-alias' && !m.builtin) {
                await fetch('/models/' + encodeURIComponent(m.name), {method:'DELETE'});
              }
            }
          } catch (e) {}
        }""")
        page.wait_for_timeout(600)
        page.click("#btn-model")
        page.wait_for_timeout(700)
        check("模型选择器 popover 打开",
              not page.locator("#model-popover").is_hidden())
        n_models = page.locator("#model-list .model-item").count()
        check("模型列表有内容", n_models >= 2, f"n={n_models}")
        page.screenshot(path=os.path.join(SHOTS, "nf-model-popover.png"))

        # 新增自定义模型
        page.click("#btn-add-model")
        page.wait_for_timeout(500)
        check("添加自定义模型模态框打开",
              page.locator("#mm-mask").is_visible())
        page.fill("#mm-name", "gpt-oss-20b-alias")
        page.fill("#mm-base-url", "http://127.0.0.1:8080/v1")
        page.fill("#mm-model", "gpt-oss-20b")
        page.fill("#mm-desc", "UI 手工新增：本机 8080 的别名端点")
        page.screenshot(path=os.path.join(SHOTS, "nf-model-modal.png"))
        page.click("#mm-save")
        page.wait_for_function(
            "() => /可达|不可达|失败|错误|已保存/.test("
            "(document.querySelector('#mm-msg')||{}).textContent||'')",
            timeout=90000)
        msg = page.eval_on_selector("#mm-msg", "el => el.textContent.trim()")
        check("自定义模型保存并探测端点", "可达" in msg, msg)
        page.click("#mm-cancel")
        page.wait_for_timeout(300)

        page.click("#btn-model")
        page.wait_for_timeout(900)
        names = page.eval_on_selector_all(
            "#model-list .model-item .m-name",
            "els => els.map(e => e.textContent.trim())")
        check("新模型出现在列表里", any("alias" in n for n in names), str(names))
        page.screenshot(path=os.path.join(SHOTS, "nf-model-popover-custom.png"))

        # 选中它（用 data-model 精确匹配，避免同名歧义）
        page.locator("#model-list .model-item[data-model='gpt-oss-20b-alias']").click()
        page.wait_for_timeout(600)
        chip = page.eval_on_selector("#model-label", "el => el.textContent.trim()")
        check("选中后 chip 显示新模型", "alias" in chip, chip)

        # 发一条消息，确认请求带上 model 且后端真的按它路由
        print("  用新模型发一条消息…")
        n_new = send_and_wait(page, "用一句话说明什么是倒排索引。")
        used = page.eval_on_selector_all(
            ".msg.assistant .meta b",
            "els => els.map(e => e.textContent.trim())")
        check("响应显示实际使用的模型是 alias",
              any("alias" in u for u in used), str(used))
        page.screenshot(path=os.path.join(SHOTS, "nf-model-used.png"))

        # 删除自定义模型
        page.click("#btn-model")
        page.wait_for_timeout(800)
        page.once("dialog", lambda d: d.accept())
        page.locator(
            "#model-list .model-item[data-model='gpt-oss-20b-alias']").locator(
            ".m-del").click()
        page.wait_for_timeout(1800)
        page.click("#btn-model")
        page.wait_for_timeout(900)
        names2 = page.eval_on_selector_all(
            "#model-list .model-item .m-name",
            "els => els.map(e => e.textContent.trim())")
        check("自定义模型已删除", not any("alias" in n for n in names2), str(names2))
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)

        # ---------------------------------------------------------- 3. 交互模式
        print("\n=== 3. 交互模式（Agent / Ask / Plan）===")
        for mode, label in (("agent", "Agent"), ("ask", "Ask"), ("plan", "Plan")):
            page.click("#btn-mode")
            page.wait_for_timeout(500)
            check(f"{label} 模式选择器打开",
                  not page.locator("#mode-popover").is_hidden())
            if mode == "agent":
                page.screenshot(path=os.path.join(SHOTS, "nf-mode-picker.png"))
            page.locator(f"#mode-list .mode-item[data-mode='{mode}']").click()
            page.wait_for_timeout(500)
            cur = page.eval_on_selector("#mode-label", "el => el.textContent.trim()")
            check(f"模式切到 {label}", cur == label, cur)
            color = page.eval_on_selector(
                "#btn-mode", "el => getComputedStyle(el).color")
            print(f"    {label} chip 颜色: {color}")

        # Ask 模式发一条（要求写操作）
        page.click("#btn-mode")
        page.wait_for_timeout(400)
        page.locator("#mode-list .mode-item[data-mode='ask']").click()
        page.wait_for_timeout(400)
        n_ask = send_and_wait(page,
                              "请把内容写入 /tmp/ask_probe.txt，内容为 hello，然后告诉我写入成功。")
        ask_answer = page.eval_on_selector_all(
            ".msg.assistant .body", "els => els.map(e => e.textContent.trim())")
        last = ask_answer[-1] if ask_answer else ""
        check("Ask 模式未执行写操作", "写入成功" not in last
              or "不能" in last or "无法" in last or "只读" in last,
              last[:140])
        page.screenshot(path=os.path.join(SHOTS, "nf-mode-ask.png"))

        # Plan 模式
        page.click("#btn-mode")
        page.wait_for_timeout(400)
        page.locator("#mode-list .mode-item[data-mode='plan']").click()
        page.wait_for_timeout(400)
        wait_idle(page)
        before_plan = page.locator(".plan-card").count()
        page.fill("#input", "请规划一个方案：检索知识库里关于向量数据库的文档并给出摘要。")
        page.press("#input", "Enter")
        wait_delta(page, ".plan-card", before_plan)
        wait_idle(page)
        check("Plan 模式渲染出计划卡片",
              page.locator(".plan-card").count() >= 1,
              f"cards={page.locator('.plan-card').count()}")
        n_steps = page.locator(".plan-card .plan-step").count()
        check("计划卡片含步骤列表", n_steps >= 1, f"steps={n_steps}")
        has_exec = page.locator(".plan-card button", has_text="执行").count() > 0
        has_drop = page.locator(".plan-card button", has_text="放弃").count() > 0
        check("计划卡片有「执行」/「放弃」按钮", has_exec and has_drop,
              f"exec={has_exec} drop={has_drop}")
        page.screenshot(path=os.path.join(SHOTS, "nf-mode-plan-card.png"))

        # 点「执行此计划」→ 以 agent 模式真跑
        print("  点「执行此计划」…")
        before_exec = page.locator(".msg.assistant").count()
        page.locator(".plan-card button", has_text="执行").first.click()
        wait_delta(page, ".msg.assistant", before_exec)
        wait_idle(page)
        mode_now = page.eval_on_selector("#mode-label", "el => el.textContent.trim()")
        check("「执行此计划」切回 Agent 模式并真的跑起来",
              mode_now == "Agent" and page.locator(".msg.assistant").count() > before_exec,
              f"mode={mode_now} answers={page.locator('.msg.assistant').count()}")
        page.screenshot(path=os.path.join(SHOTS, "nf-plan-executed.png"))

        # ---------------------------------------------------------- 4. 历史会话
        print("\n=== 4. 历史会话持久化 + 查看 ===")
        # 再来两轮，确保有 3 轮
        for q in ("请记住数字 2024。", "再记住数字 2025。"):
            send_and_wait(page, q)
        tid = page.evaluate("state.threadId")
        print("  thread_id =", tid)

        page.reload(wait_until="networkidle")
        page.wait_for_timeout(2200)
        # 强制重新拉取静态资源（避免旧 app.js 缓存）
        page.evaluate("""async () => {
          for (const p of ['/ui/app.js', '/ui/workbench.js', '/ui/app.css']) {
            await fetch(p + '?t=' + Date.now(), {cache: 'reload'});
          }
        }""")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(2200)
        page.click('.act-item[data-view="sessions"]')
        page.wait_for_timeout(1200)
        n_sess = page.locator("#sessions-list .tree-row").count()
        check("历史会话列表有内容", n_sess > 0, f"n={n_sess}")
        page.screenshot(path=os.path.join(SHOTS, "nf-sessions-list.png"))

        # 点刚才那个会话
        clicked = page.evaluate("""() => {
          const el = document.querySelector('#sessions-list .tree-row');
          if (!el) return false;
          el.click();
          return true;
        }""")
        page.wait_for_timeout(2500)
        n_hist = page.locator(".sess-history .msg").count()
        check("点历史会话在主区打开并渲染完整历史消息", clicked and n_hist >= 4,
              f"msgs={n_hist}")
        check("历史视图有「继续对话」入口",
              page.locator(".sess-continue").count() >= 1)
        check("历史视图有诚实提示（重启后不能从中间续跑）",
              page.locator(".sess-notice").count() >= 1)
        page.screenshot(path=os.path.join(SHOTS, "nf-session-history.png"))

        # 摘要（用历史会话 tab 里的「生成摘要」按钮 —— 作用于该会话）
        print("\n=== 5. 会话摘要 ===")
        check("历史会话 tab 有「生成摘要」按钮",
              page.locator(".act-sess-summary").count() >= 1)
        page.locator(".act-sess-summary").first.click()
        page.wait_for_function(
            "() => document.querySelectorAll('.summary-card').length >= 1",
            timeout=240000)
        check("摘要卡片渲染出来",
              page.locator(".summary-card").count() >= 1)
        concl = page.eval_on_selector_all(
            ".summary-card .sum-conclusion", "els => els.map(e => e.textContent.trim())")
        kps = page.locator(".summary-card .sum-list li").count()
        check("摘要含一句话结论", bool(concl and concl[0]), str(concl)[:120])
        check("摘要含关键要点列表", kps >= 1, f"kp={kps}")
        page.screenshot(path=os.path.join(SHOTS, "nf-summary-card.png"))

        # 摘要持久化验证：GET /sessions/{id} 里 has_summary 为 true
        sess_meta = page.evaluate("""async (tid) => {
          const r = await fetch('/sessions/' + encodeURIComponent(tid));
          return await r.json();
        }""", tid)
        check("摘要已持久化到会话元信息",
              bool(sess_meta.get("has_summary")),
              f"has_summary={sess_meta.get('has_summary')}")
        print("    summary_json =",
              json.dumps(sess_meta.get("summary_json"), ensure_ascii=False)[:300])

        # ---------------------------------------------------------- 6. 视觉精修
        print("\n=== 6. 视觉精修特写 ===")
        page.click('.act-item[data-view="explorer"]')
        page.wait_for_timeout(600)
        page.locator(".tab", has_text="对话").first.click()
        page.wait_for_timeout(600)
        page.screenshot(path=os.path.join(SHOTS, "nf-visual-full.png"))
        page.screenshot(path=os.path.join(SHOTS, "nf-visual-activitybar.png"),
                        clip={"x": 0, "y": 0, "width": 320, "height": 520})
        page.screenshot(path=os.path.join(SHOTS, "nf-visual-tabbar.png"),
                        clip={"x": 300, "y": 30, "width": 900, "height": 90})
        page.screenshot(path=os.path.join(SHOTS, "nf-visual-composer.png"),
                        clip={"x": 300, "y": 700, "width": 900, "height": 250})

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
