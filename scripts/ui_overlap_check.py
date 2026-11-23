# -*- coding: utf-8 -*-
"""客观断言：侧栏四个列表视图（知识库 / 工具 / 任务 / 历史会话）
**不存在文字重叠**，且徽标**不压住标题**。

为什么需要这个脚本
------------------
用户的实机报障是「侧边栏怎么文字重叠了」。这类问题**靠肉眼看截图无法防退化**，
必须用 ``getBoundingClientRect`` 做几何断言。

根因（已修）：``.tree-row`` 是 ``display:flex`` 且**高度被锁死 22px**，
曾经被覆盖成 ``flex-wrap: wrap`` + ``align-items: flex-start``，
而 ``.row-main`` / ``.row-sub`` 都是 ``flex: 1 1 100%``（各占一整行）。
行盒高度不变 → 第二行（副标题）**溢出到行盒下方**，
与**下一行的标题**重叠（实测 15.75px）。

现在统一为 ``.tree-row2``：行盒高度自适应 + 内部两行网格，
第一行 = 标题（可截断）+ 右侧徽标，第二行 = 副标题（可截断）。

本脚本对每个列表视图断言：
    1. 每行的 ``.row-line1`` 与 ``.row-line2`` 矩形**不相交**；
    2. 每行**最后一行**的底部不超过行盒底部（不溢出）；
    3. 第一行的**徽标矩形**与**标题矩形**不相交；
    4. 行内元素不超出侧栏右边界（不横向撑破）。
"""
from __future__ import annotations

import json
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


#: 在页面里跑的探针：对一个容器，返回逐行的几何数据
PROBE = r"""
(sel) => {
  const box = document.querySelector(sel);
  if (!box) return {ok: false, reason: 'container missing', rows: []};
  const rows = Array.from(box.querySelectorAll('.tree-row2'));
  const out = [];
  rows.forEach((r, i) => {
    const lines = Array.from(r.querySelectorAll('.row-line1, .row-line2'));
    const title = r.querySelector('.row-line1 .row-title');
    const metas = Array.from(r.querySelectorAll('.row-line1 .row-meta'));
    const rect = (e) => {
      const b = e.getBoundingClientRect();
      return {top: b.top, bottom: b.bottom, left: b.left, right: b.right,
              width: b.width, height: b.height};
    };
    out.push({
      i,
      row: rect(r),
      lines: lines.map((l) => ({
        cls: l.className,
        ...rect(l),
        text: (l.textContent || '').trim().slice(0, 60),
      })),
      title: title ? {text: (title.textContent || '').trim().slice(0, 60), ...rect(title)} : null,
      metas: metas.map((m) => ({text: (m.textContent || '').trim().slice(0, 30), ...rect(m)})),
    });
  });
  return {ok: true, rows: out, sidebarRight: document.querySelector('#sidebar')
    ? document.querySelector('#sidebar').getBoundingClientRect().right : null};
}
"""


def overlaps(a, b, eps=0.5):
    """两个矩形是否相交（留 0.5px 容差，避免浮点抖动误报）。"""
    return (min(a["right"], b["right"]) - max(a["left"], b["left"]) > eps and
            min(a["bottom"], b["bottom"]) - max(a["top"], b["top"]) > eps)


def analyze(page, sel, label, require_rows=True):
    """对单个列表容器做几何断言，返回统计信息。"""
    d = page.evaluate(PROBE, sel)
    if not d["ok"]:
        check(f"{label}：容器存在", False, d.get("reason", ""))
        return {"rows": 0}

    rows = d["rows"]
    if not rows:
        # 列表本来就是空的（例如当前没有任务）——不判失败，但要有明确空态
        empty_txt = page.locator(sel).inner_text().strip()
        check(f"{label}：空列表有明确空态文案（非空白）",
              bool(empty_txt) and "暂无" in empty_txt or "没有" in empty_txt
              or "还是空的" in empty_txt or "加载中" in empty_txt,
              repr(empty_txt[:40]))
        return {"rows": 0}
    check(f"{label}：列表有内容", True, f"n={len(rows)}")
    if not require_rows:
        return {"rows": len(rows)}

    line_overlaps = []      # 同一行内 line1/line2 互相重叠
    overflows = []          # 行内容溢出行盒底部
    meta_hits = []          # 徽标压住标题
    h_overflows = []        # 横向撑破侧栏

    for r in rows:
        # 1) 同一行内：第一行 vs 第二行（以及多个第二行之间）
        for a in range(len(r["lines"])):
            for b in range(a + 1, len(r["lines"])):
                if overlaps(r["lines"][a], r["lines"][b]):
                    line_overlaps.append(
                        {"row": r["i"], "a": r["lines"][a]["cls"],
                         "b": r["lines"][b]["cls"],
                         "textA": r["lines"][a]["text"], "textB": r["lines"][b]["text"]})
        # 2) 溢出：最后一条线底部 > 行盒底部
        if r["lines"]:
            last = max(r["lines"], key=lambda x: x["bottom"])
            if last["bottom"] > r["row"]["bottom"] + 0.5:
                overflows.append({"row": r["i"],
                                  "lineBottom": last["bottom"], "rowBottom": r["row"]["bottom"]})
        # 3) 徽标压标题
        if r["title"]:
            for m in r["metas"]:
                if overlaps(r["title"], m):
                    meta_hits.append({"row": r["i"], "title": r["title"]["text"],
                                      "meta": m["text"]})
        # 4) 横向撑破
        for ln in r["lines"]:
            if d["sidebarRight"] and ln["right"] > d["sidebarRight"] + 1:
                h_overflows.append({"row": r["i"], "right": ln["right"],
                                    "sidebarRight": d["sidebarRight"]})

    check(f"{label}：行内 line1 / line2 矩形不相交",
          not line_overlaps,
          f"{len(line_overlaps)} 处重叠 " + json.dumps(line_overlaps[:2], ensure_ascii=False))
    check(f"{label}：行内容不溢出行盒底部",
          not overflows,
          f"{len(overflows)} 处溢出 " + json.dumps(overflows[:2], ensure_ascii=False))
    check(f"{label}：徽标矩形不与标题矩形相交",
          not meta_hits,
          f"{len(meta_hits)} 处压字 " + json.dumps(meta_hits[:2], ensure_ascii=False))
    check(f"{label}：行内容不横向撑破侧栏",
          not h_overflows,
          f"{len(h_overflows)} 处撑破 " + json.dumps(h_overflows[:2], ensure_ascii=False))
    return {"rows": len(rows)}


def main() -> int:
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1600, "height": 940})
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(2500)

        views = [
            ("knowledge", "#kb-doc-list", "知识库文档列表"),
            ("tools", "#tools-list", "工具列表"),
            ("tasks", "#tasks-list", "任务列表"),
            ("sessions", "#sessions-list", "历史会话列表"),
        ]

        stats = {}
        for view, sel, label in views:
            page.click(f'.act-item[data-view="{view}"]')
            page.wait_for_timeout(1600)
            stats[label] = analyze(page, sel, label)

        # ---- 任务列表：当前可能确实没有任务（count=0）。
        #      为了仍然验证「任务行不重叠」，注入一行**真实结构**的合成行，
        #      用同样的 .tree-row2 / .row-line1 / .row-line2 标记做几何断言。
        print("\n=== 任务列表（注入合成行验证行结构）===")
        page.click('.act-item[data-view="tasks"]')
        page.wait_for_timeout(1500)
        # 先等列表刷新稳定，再注入（refreshTasks 会清空容器）
        page.wait_for_timeout(800)
        injected = page.evaluate(r"""
          () => {
            const box = document.querySelector('#tasks-list');
            if (!box) return false;
            const el = document.createElement('div');
            el.setAttribute('data-synthetic', '1');
            el.className = 'tree-row tree-row2 leaf act';
            el.innerHTML = '<span class="row-line1">'
              + '<span class="row-title">合成任务标题-用于验证两行结构不会重叠</span>'
              + '<span class="row-meta"><span class="st done">done</span></span></span>'
              + '<span class="row-line2">合成副标题 · 模型 · token 统计</span>';
            box.appendChild(el);
            return !!box.querySelector('[data-synthetic]');
          }
        """)
        check("合成任务行注入成功", injected, str(injected))
        page.wait_for_timeout(400)
        analyze(page, "#tasks-list", "任务列表（合成行）")

        # ---- 历史会话专项：分页加载到全部 ----
        print("\n=== 历史会话分页加载 ===")
        page.click('.act-item[data-view="sessions"]')
        page.wait_for_timeout(1200)

        def n_rows():
            return page.locator("#sessions-list .tree-row2").count()

        # **总数是动态的**（跑测试会不断新增会话），所以从后端取真实 count，
        # 不能写死 109 —— 否则会话一多，断言就变成"测了个错的数"。
        api_total = page.evaluate(
            "async () => (await (await fetch('/sessions')).json()).count")
        print(f"  后端会话总数 = {api_total}")

        base = n_rows()
        check("首屏渲染 30 条会话", base == 30, f"n={base}")

        # 连点「加载更多」直到全部加载完
        clicks = 0
        while clicks < 30:
            btn = page.locator("#btn-sessions-more")
            if btn.count() == 0 or btn.is_hidden():
                break
            btn.click()
            page.wait_for_timeout(900)
            clicks += 1
            if n_rows() >= api_total:
                break
        total = n_rows()
        check("「加载更多」可加载到全部会话",
              total >= api_total, f"n={total} / API={api_total}（点了 {clicks} 次）")

        summary = page.locator("#sess-summary").inner_text()
        check("视图头部显示总数", str(api_total) in summary, summary)

        more_txt = page.locator("#btn-sessions-more").inner_text()
        check("全部加载后「加载更多」按钮隐藏",
              page.locator("#btn-sessions-more").is_hidden(), more_txt)

        # 加载更多后仍然不能重叠
        analyze(page, "#sessions-list", "历史会话列表（全部加载后）")

        # ---- 点击会话 → 主区渲染完整历史 ----
        print("\n=== 点击会话渲染完整历史 ===")
        # ⚠️ 不能盲取第一行：会话列表按 updated_at 倒序，**最上面那条可能是
        #    别的会话跑测试时留下的「只有 1 条消息」的半截会话**
        #    （实测 th-vrp-2f5b21eb76 只有 1 条消息）。
        #    那样断言 msgs>=2 就会因**数据**而失败，而不是 UI 有问题。
        #    这里逐行试，点到第一个真正有 >=2 条消息的会话为止。
        rows = page.locator("#sessions-list .tree-row2")
        n_rows_tried = min(rows.count(), 6)
        n_msg = 0
        picked = -1
        for i in range(n_rows_tried):
            rows.nth(i).click()
            page.wait_for_timeout(2200)
            n_msg = page.locator(".sess-history .msg").count()
            if n_msg >= 2:
                picked = i
                break
        check("点会话在主区渲染出历史消息", n_msg >= 2,
              f"msgs={n_msg}（试了 {n_rows_tried} 行，命中第 {picked} 行）")
        check("历史视图有「在此会话继续提问」入口",
              page.locator(".sess-continue").count() >= 1)

        browser.close()

    print("\n" + "=" * 64)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"结果: {passed}/{len(RESULTS)} 通过")
    for n, ok, dd in RESULTS:
        if not ok:
            print(f"  失败: {n}  {dd}")
    print("=" * 64)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
