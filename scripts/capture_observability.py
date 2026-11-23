# -*- coding: utf-8 -*-
"""运行观测面板取证脚本：走**真实点击路径**逐个视图截图，并做客观断言。

与 ``capture_delivery.py`` 同风格：Playwright 复刻用户操作序列，
产出 ``data/obs_shots/*.png`` 与 ``data/_regress/observability_shots.json``。

覆盖 12 个观测视图：
    P0-1 时间瀑布 / P0-2 工具调用卡 / P0-3 时序图
    P1-1 成本瀑布 / P1-2 上下文占用 / P1-3 审批卡片 / P1-4 改动时间线
    P2-1 时间信号线 / P2-2 代码库图 / P2-3 自主性档位 / P2-4 推理摘要

关键客观断言：
    · Trace Waterfall 条形宽度比 ≈ 实际耗时比（容差 ±15%）
    · 工具卡展开体有「参数」「结果」两个分区 + 复制按钮
    · 时序图生命线数量 == 参与者数量
    · 空态：清空会话后每个视图都有设计过的空态元素

用法::

    python scripts/capture_observability.py
"""
from __future__ import annotations

import json
import os
import sys

from playwright.sync_api import sync_playwright

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("AGENTPLAT_UI_BASE", "http://127.0.0.1:8010/ui/")
SHOTS = os.path.join(REPO_ROOT, "data", "obs_shots")

RESULTS: list = []

#: 12 个视图：(分段控件文案, 视图 id, 截图文件名)
VIEWS = [
    ("时间瀑布", "waterfall", "obs-01-waterfall.png"),
    ("工具调用卡", "toolcards", "obs-02-toolcards.png"),
    ("时序图", "sequence", "obs-03-sequence.png"),
    ("成本瀑布", "cost", "obs-04-cost.png"),
    ("上下文占用", "context", "obs-05-context.png"),
    ("审批卡片", "approval", "obs-09-approval.png"),
    ("改动时间线", "difftimeline", "obs-10-difftimeline.png"),
    ("时间信号线", "timing", "obs-06-timing.png"),
    ("代码库图", "repomap", "obs-08-repomap.png"),
    ("自主性档位", "autonomy", "obs-07-autonomy.png"),
    ("推理摘要", "reasoning", "obs-11-reasoning.png"),
]


def check(name, cond, detail=""):
    RESULTS.append({"name": name, "ok": bool(cond), "detail": str(detail)[:400]})
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  -- {detail}" if detail else ""), flush=True)
    return bool(cond)


def _launch(pw):
    """优先用本机 Chrome / Edge，避免下载 Playwright 自带 Chromium。"""
    for c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if os.path.exists(c):
            return pw.chromium.launch(executable_path=c, args=["--no-sandbox"])
    return pw.chromium.launch()


def shot(page, name):
    os.makedirs(SHOTS, exist_ok=True)
    p = os.path.join(SHOTS, name)
    page.screenshot(path=p)
    print(f"  [SHOT] {name}", flush=True)
    return p


def open_obs(page) -> bool:
    """点开「运行观测」面板（对话 tab 顶部入口）。"""
    page.evaluate("""() => {
      const b = document.querySelector('button[title*="运行观测"], button[aria-label*="运行观测"]');
      if (b) b.click();
    }""")
    page.wait_for_timeout(1200)
    return bool(page.query_selector("#obs-root"))


def switch_view(page, label: str) -> bool:
    """切到指定观测视图。

    观测面板默认展示 **Live Timeline** 主视图；12 个组件视图藏在
    「全部视图」按钮后面的分段控件里（见 timeline.js ``tl-view-btn``）。
    所以这里先点「全部视图」进入画廊，再点对应的分段控件。
    """
    # 1) 进入「全部视图」画廊
    in_gallery = page.evaluate("""() => {
      const segs = document.querySelectorAll('#obs-root .obs-seg-btn');
      if (segs.length) return true;                      // 已在画廊
      const btn = Array.from(document.querySelectorAll('button'))
        .find(b => (b.textContent || '').includes('全部视图'));
      if (btn) { btn.click(); return true; }
      return false;
    }""")
    page.wait_for_timeout(700)
    if not in_gallery:
        return False
    # 2) 点分段控件
    ok = page.evaluate("""(label) => {
      const segs = document.querySelectorAll('#obs-root .obs-seg-btn');
      for (const s of segs) {
        if ((s.textContent || '').includes(label)) { s.click(); return true; }
      }
      return false;
    }""", label)
    page.wait_for_timeout(900)
    return bool(ok)


def view_state(page, view_id: str) -> dict:
    """读某个观测视图容器的 DOM 概况。

    视图容器在「全部视图」画廊里是 ``#obs-view-<id>``；
    画廊切换后**只有当前子视图被渲染**，其余子视图容器为空。
    """
    return page.evaluate("""(id) => {
      const h = document.querySelector('#obs-view-' + id);
      if (!h) return {missing: true};
      const txt = (h.innerText || '').trim();
      const emptyEl = h.querySelector('[data-empty]');
      return {
        missing: false,
        empty: !!emptyEl,
        emptyText: emptyEl ? (emptyEl.innerText || '') : '',
        kids: h.querySelectorAll('*').length,
        txt: txt.slice(0, 200),
      };
    }""", view_id)


# ------------------------------------------------------------------ 瀑布比例
def probe_waterfall_ratio(page) -> dict:
    """**客观**测量 Trace Waterfall 的条形宽度 vs 实际耗时。

    对每个步骤：
        理论宽度% = (ended - started) / (t1 - t0) * 100
        实测宽度% = rect.width / 容器可用宽度 * 100
    再取「两条不同时长的条」的宽度比与耗时比比较，容差 ±15%。
    """
    return page.evaluate("""() => {
      const host = document.querySelector('#obs-view-waterfall');
      if (!host) return {missing: true};
      const bars = Array.from(host.querySelectorAll('.obs-wf-bar'));
      if (!bars.length) return {bars: 0};

      const track = host.querySelector('.obs-wf-track');
      const trackW = track ? track.getBoundingClientRect().width : 0;

      const steps = bars.map((bar) => {
        const r = bar.getBoundingClientRect();
        const s = parseFloat(bar.dataset.started || '0');
        const e = parseFloat(bar.dataset.ended || '0');
        const dur = parseFloat(bar.dataset.dur || '0');
        return {
          name: (bar.getAttribute('title') || '').trim(),
          w: r.width, left: r.left,
          s: s, e: e, dur: dur,
          realWidthPct: parseFloat(bar.dataset.realWidth || '0'),
        };
      });

      const t0 = Math.min(...steps.map(x => x.s).filter(x => x > 0));
      const t1 = Math.max(...steps.map(x => (x.e || x.s + x.dur)).filter(x => x > 0));
      const span = t1 - t0;

      steps.forEach((st) => {
        st.theoPct = span > 0 ? ((st.e || st.s + st.dur) - st.s) / span * 100 : 0;
        st.realPct = trackW > 0 ? st.w / trackW * 100 : 0;
        st.dev = st.theoPct > 0
          ? Math.abs(st.realPct - st.theoPct) / st.theoPct * 100 : null;
      });

      // 找耗时差异最明显的两条。
      // **过滤条件**：理论宽度 >= 1% 的条 —— 宽度 < 0.8% 的条会被
      // ``Math.max(width, 0.8)`` 的最小可见宽度抬高，比值会失真。
      const sorted = steps.filter(x => x.dur > 0 && x.theoPct >= 1)
        .sort((a, b) => b.dur - a.dur);
      const pair = sorted.length >= 2 ? [sorted[0], sorted[1]] : [];

      /* **兜底**：本轮只有 1 条宽度 >= 1% 的条时，无法做「两条比值」断言。
         改用「单条 vs 理论值」的偏差断言（更严格：直接比百分比）。
         两者都不可用时返回 ratioMode='none'。 */
      let ratio = null;
      let ratioMode = 'none';
      if (pair.length === 2) {
        ratioMode = 'pair';
        ratio = {
          a: {name: pair[0].name, dur: pair[0].dur, w: pair[0].w},
          b: {name: pair[1].name, dur: pair[1].dur, w: pair[1].w},
          durRatio: +(pair[0].dur / pair[1].dur).toFixed(4),
          widthRatio: +(pair[0].w / pair[1].w).toFixed(4),
          ratioDev: +(Math.abs(pair[0].w / pair[1].w - pair[0].dur / pair[1].dur)
                     / (pair[0].dur / pair[1].dur) * 100).toFixed(3),
        };
      } else if (sorted.length === 1) {
        ratioMode = 'single';
        const s = sorted[0];
        ratio = {
          a: {name: s.name, dur: s.dur, w: s.w},
          theoPct: s.theoPct, realPct: s.realPct,
          ratioDev: s.dev == null ? null : +s.dev.toFixed(3),
        };
      }

      return {
        bars: bars.length,
        trackW: trackW,
        span: span,
        steps: steps.map(x => ({
          name: x.name.slice(0, 40), dur: x.dur, w: +x.w.toFixed(2),
          theoPct: +x.theoPct.toFixed(4), realPct: +x.realPct.toFixed(4),
          dev: x.dev == null ? null : +x.dev.toFixed(3),
        })),
        pair: ratio,
        ratioMode: ratioMode,
      };
    }""")


def probe_sequence(page) -> dict:
    """时序图：生命线数量 == 参与者数量；箭头方向（调用向右 / 回复向左）。"""
    return page.evaluate("""() => {
      const host = document.querySelector('#obs-view-sequence');
      if (!host) return {missing: true};
      const parts = host.querySelectorAll('.obs-seq-part');
      const lifes = host.querySelectorAll('.obs-seq-lifeline');
      const msgs = Array.from(host.querySelectorAll('.obs-seq-msg'));
      const arrows = msgs.map(m => ({
        dir: m.dataset.dir || '',
        kind: m.dataset.kind || '',
        x1: parseFloat(m.dataset.x1 || '0'),
        x2: parseFloat(m.dataset.x2 || '0'),
        label: (m.getAttribute('aria-label') || '').slice(0, 40),
      }));
      return {
        participants: parts.length,
        lifelines: lifes.length,
        messages: msgs.length,
        right: arrows.filter(a => a.dir === 'right').length,
        left: arrows.filter(a => a.dir === 'left').length,
        calls: arrows.filter(a => a.kind === 'call').length,
        replies: arrows.filter(a => a.kind === 'reply').length,
        frames: host.querySelectorAll('.obs-seq-frame').length,
        actives: host.querySelectorAll('.obs-seq-active').length,
        arrows: arrows.slice(0, 12),
        hasEmpty: !!host.querySelector('[data-empty]'),
        body: (host.innerText || '').slice(0, 200),
      };
    }""")


def probe_toolcards(page) -> dict:
    """工具卡：展开体有「参数」「结果」两区 + 复制按钮。"""
    return page.evaluate("""() => {
      const host = document.querySelector('#obs-view-toolcards');
      if (!host) return {missing: true};
      const cards = host.querySelectorAll('details.obs-tc');
      const card = cards.length ? cards[0] : null;
      const zones = card
        ? Array.from(card.querySelectorAll('.obs-tc-zone')).map(z => ({
            zone: z.dataset.zone || '',
            title: (z.querySelector('.obs-tc-zone-title') || {}).textContent || '',
            chars: (z.innerText || '').length,
          }))
        : [];
      return {
        cards: cards.length,
        groups: host.querySelectorAll('.obs-tc-group').length,
        copyButtons: host.querySelectorAll('.obs-tc-copy').length,
        zones: zones,
        hasArgs: zones.some(z => z.zone === 'args' || z.title.includes('参数')),
        hasResult: zones.some(z => z.zone === 'result' || z.title.includes('结果')),
        statusDots: host.querySelectorAll('.obs-dot').length,
        jsonBlocks: host.querySelectorAll('.obs-tc-json').length,
        hasEmpty: !!host.querySelector('[data-empty]'),
        body: (host.innerText || '').slice(0, 200),
      };
    }""")


def probe_context(page) -> dict:
    """上下文占用：treemap 单元 + 总量 vs 窗口上限。"""
    return page.evaluate("""() => {
      const host = document.querySelector('#obs-view-context');
      if (!host) return {missing: true};
      const body = host.innerText || '';
      return {
        cells: host.querySelectorAll('.obs-tm-cell').length,
        hasLimit: /32768|8192|上限|窗口/.test(body),
        body: body.slice(0, 260),
        hasEmpty: !!host.querySelector('[data-empty]'),
      };
    }""")


def probe_repomap(page) -> dict:
    """代码库图：treemap 单元数量（文件块）。"""
    return page.evaluate("""() => {
      const host = document.querySelector('#obs-view-repomap');
      if (!host) return {missing: true};
      const cells = host.querySelectorAll('.obs-repo-cell');
      return {
        cells: cells.length,
        fileCells: host.querySelectorAll('.obs-repo-cell.is-file').length,
        dirCells: host.querySelectorAll('.obs-repo-cell.is-dir').length,
        edited: host.querySelectorAll('.obs-repo-cell.is-edited').length,
        labels: Array.from(cells).slice(0, 8)
                    .map(c => (c.getAttribute('title') || c.textContent || '').trim().slice(0, 30)),
        hasEmpty: !!host.querySelector('[data-empty]'),
        body: (host.innerText || '').slice(0, 200),
      };
    }""")


def probe_approval(page) -> dict:
    """审批卡：风险徽标 + 等待倒计时 + A/D 快捷键提示。"""
    return page.evaluate("""() => {
      const host = document.querySelector('#obs-view-approval');
      if (!host) return {missing: true};
      const body = host.innerText || '';
      return {
        badges: host.querySelectorAll('.obs-ap-risk').length,
        hasRisk: /高风险|中风险|低风险/.test(body),
        hasWait: /已等待\\s*\\d+/.test(body),
        waitText: (body.match(/已等待\\s*\\d+s?/) || [''])[0],
        hasKeys: /A\\s*批准|D\\s*拒绝|快捷键/.test(body),
        hasEmpty: !!host.querySelector('[data-empty]'),
        body: body.slice(0, 220),
      };
    }""")


def probe_cost(page) -> dict:
    """成本瀑布：无价格时诚实标注本机推理。"""
    return page.evaluate("""() => {
      const host = document.querySelector('#obs-view-cost');
      if (!host) return {missing: true};
      const body = host.innerText || '';
      return {
        hasLocalNote: /本机推理|无 API 成本/.test(body),
        steps: host.querySelectorAll('.obs-cost-row').length,
        hasEmpty: !!host.querySelector('[data-empty]'),
        body: body.slice(0, 220),
      };
    }""")


def probe_difftimeline(page) -> dict:
    return page.evaluate("""() => {
      const host = document.querySelector('#obs-view-difftimeline');
      if (!host) return {missing: true};
      return {
        files: host.querySelectorAll('.obs-dtl-file').length,
        edits: host.querySelectorAll('.obs-dtl-edit').length,
        bars: host.querySelectorAll('.obs-dtl-file-bar').length,
        hasEmpty: !!host.querySelector('[data-empty]'),
        body: (host.innerText || '').slice(0, 200),
      };
    }""")


def probe_timing(page) -> dict:
    """时间信号线：三态高度编码（think=4px / tool=14px / wait=2px dashed）。"""
    return page.evaluate("""() => {
      const host = document.querySelector('#obs-view-timing');
      if (!host) return {missing: true};
      const segs = Array.from(host.querySelectorAll('.obs-rs-seg, .obs-sig-bar'));
      const hs = segs.map(s => s.getBoundingClientRect().height);
      return {
        segs: segs.length,
        heights: hs.slice(0, 14).map(h => +h.toFixed(1)),
        kinds: [...new Set(Array.from(segs).map(s => s.dataset.kind || s.className))].slice(0, 8),
        hasEmpty: !!host.querySelector('[data-empty]'),
        body: (host.innerText || '').slice(0, 200),
      };
    }""")


def probe_autonomy(page) -> dict:
    return page.evaluate("""() => {
      const host = document.querySelector('#obs-view-autonomy');
      if (!host) return {missing: true};
      return {
        stages: host.querySelectorAll('.obs-dial-seg').length,
        knob: host.querySelectorAll('.obs-dial-knob').length,
        hasEmpty: !!host.querySelector('[data-empty]'),
        body: (host.innerText || '').slice(0, 200),
      };
    }""")


def probe_reasoning(page) -> dict:
    """推理摘要：**不得**泄露原始思维链。"""
    return page.evaluate("""() => {
      const host = document.querySelector('#obs-view-reasoning');
      if (!host) return {missing: true};
      const body = host.innerText || '';
      const html = host.innerHTML || '';
      return {
        hasRaw: /reasoning_content|<think>/.test(html),
        hasSummary: /摘要/.test(body),
        hasEmpty: !!host.querySelector('[data-empty]'),
        body: body.slice(0, 220),
      };
    }""")


PROBES = {
    "waterfall": probe_waterfall_ratio,
    "toolcards": probe_toolcards,
    "sequence": probe_sequence,
    "context": probe_context,
    "repomap": probe_repomap,
    "approval": probe_approval,
    "cost": probe_cost,
    "difftimeline": probe_difftimeline,
    "timing": probe_timing,
    "autonomy": probe_autonomy,
    "reasoning": probe_reasoning,
}


def stage_run_with_data(page) -> None:
    """发一条**需要多次工具调用**的真实问题，让观测面板有足够数据。"""
    print("\n=== 阶段 1：真实运行（多轮工具调用）===", flush=True)
    page.evaluate("""() => {
      const pg = document.querySelector('.page[data-page="pg-chat"]');
      if (pg && typeof activateTab === 'function') activateTab(pg.dataset.page);
    }""")
    page.wait_for_timeout(800)
    ta = page.query_selector("#input") or page.query_selector("#composer textarea")
    if ta is None:
        check("找到输入框", False)
        return
    ta.click()
    ta.fill("请对比 TP_CMOS_32G 和 TP_NMOS_65G 两份清单里 T101_VDD_FUNCT 的上下限，"
            "并列出两份清单的差异点，引用来源。")
    # 用「发送」按钮而不是 Enter 键（IME / keydown 路径更不可控）
    page.evaluate("""() => {
      const b = document.querySelector('#btn-send');
      if (b) b.click();
    }""")
    print("  等待模型多轮响应 ...", flush=True)

    # 等工具调用行出现 + 结果回填
    for _ in range(120):
        page.wait_for_timeout(1000)
        st = page.evaluate("""() => {
          const rows = document.querySelectorAll('#chat details.tool-row.tv-trace-row');
          const last = rows[rows.length - 1];
          if (!last) return {rows: 0, filled: 0};
          const filled = Array.from(last.querySelectorAll('.tv-tb-label'))
            .filter(e => (e.textContent || '').includes('输出结果')).length;
          return {rows: rows.length, filled: filled};
        }""")
        if st.get("rows", 0) >= 2 and st.get("filled", 0) >= 1:
            break
    st = page.evaluate("""() => ({
      rows: document.querySelectorAll('#chat details.tool-row.tv-trace-row').length,
      obsTrace: (typeof OBS !== 'undefined' && OBS.trace) ? OBS.trace.length : -1,
    })""")
    check("对话流出现工具调用行", st.get("rows", 0) > 0, f"rows={st.get('rows')}")
    check("观测面板轨迹快照有数据", st.get("obsTrace", -1) > 0,
          f"OBS.trace={st.get('obsTrace')}")


def stage_views(page) -> None:
    """逐个视图：切过去 → 探针 → 截图。"""
    print("\n=== 阶段 2：12 个观测视图 ===", flush=True)
    check("运行观测面板可打开", open_obs(page))

    for label, view_id, fname in VIEWS:
        ok = switch_view(page, label)
        check(f"分段控件可切到「{label}」", ok)
        st = view_state(page, view_id)
        check(f"视图 {view_id} 容器存在", not st.get("missing"))
        if view_id == "repomap":
            # Repo Map 的文件清单是**异步**拉的（/workspace/repomap），
            # 等它回填完再探针，否则测到的是加载中的空态。
            for _ in range(20):
                page.wait_for_timeout(600)
                n = page.evaluate(
                    "() => document.querySelectorAll('#obs-view-repomap .obs-repo-cell').length")
                if n:
                    break
        probe = PROBES.get(view_id)
        if probe:
            data = probe(page)
            print(f"  [{view_id}] 探针:", json.dumps(data, ensure_ascii=False)[:600], flush=True)
            yield_data = data
            if view_id == "waterfall":
                check("瀑布有条形", data.get("bars", 0) > 0, f"bars={data.get('bars')}")
                pair = data.get("pair")
                mode = data.get("ratioMode")
                if mode == "pair":
                    check("瀑布宽度比 ≈ 耗时比（±15%）",
                          pair["ratioDev"] <= 15.0,
                          f"宽比={pair['widthRatio']} 时长比={pair['durRatio']} "
                          f"偏差={pair['ratioDev']}%")
                elif mode == "single":
                    # 本轮只有 1 条够宽的条 → 改断言「单条 vs 理论百分比」
                    check("瀑布条宽 ≈ 理论百分比（±15%）",
                          pair["ratioDev"] is not None and pair["ratioDev"] <= 15.0,
                          f"理论={pair['theoPct']}% 实测={pair['realPct']}% "
                          f"偏差={pair['ratioDev']}%")
                else:
                    check("瀑布宽度比 ≈ 耗时比（±15%）", False,
                          "本轮无宽度 >= 1% 的步骤，无法比较")
            elif view_id == "toolcards":
                check("工具卡有「参数」分区", data.get("hasArgs", False))
                check("工具卡有「结果」分区", data.get("hasResult", False))
                check("工具卡有复制按钮", data.get("copyButtons", 0) > 0)
            elif view_id == "sequence":
                check("生命线数量 == 参与者数量",
                      data.get("lifelines", 0) == data.get("participants", 0)
                      and data.get("participants", 0) > 0,
                      f"life={data.get('lifelines')} part={data.get('participants')}")
                check("调用箭头向右", data.get("right", 0) > 0, f"right={data.get('right')}")
                check("回复箭头向左", data.get("left", 0) > 0, f"left={data.get('left')}")
            elif view_id == "context":
                check("上下文 treemap 有单元", data.get("cells", 0) > 0,
                      f"cells={data.get('cells')}")
                check("上下文显示窗口上限", data.get("hasLimit", False))
            elif view_id == "repomap":
                check("代码库图 treemap 有文件块", data.get("cells", 0) > 0,
                      f"cells={data.get('cells')}")
            elif view_id == "cost":
                check("成本缺失时诚实标注本机推理", data.get("hasLocalNote", False))
            elif view_id == "reasoning":
                check("推理摘要不泄露原始思维链", not data.get("hasRaw", False))
        shot(page, fname)


def stage_empty(page) -> None:
    """空态：清空会话（新建会话不发消息）后每个视图都有设计过的空态。"""
    print("\n=== 阶段 3：空态 ===", flush=True)
    page.evaluate("() => { const b = document.querySelector('#btn-new'); if (b) b.click(); }")
    page.wait_for_timeout(2200)

    open_obs(page)
    empty_report = {}
    for label, view_id, _ in VIEWS:
        switch_view(page, label)
        st = view_state(page, view_id)
        empty_report[view_id] = {
            "empty": st.get("empty", False),
            "text": (st.get("emptyText") or "").strip()[:120],
        }
        # 说明：repomap / autonomy 的数据源是**工作区文件树**与**交互模式配置**，
        # 与「本次运行的轨迹」无关 —— 新建会话后它们依然有内容，这是**正确行为**，
        # 不是缺空态。它们各自另有「数据没加载出来」时的降级空态。
        if view_id in ("repomap", "autonomy"):
            check(f"[空态] {view_id} 渲染正常（非运行态依赖）",
                  st.get("kids", 0) > 0 and not st.get("missing"),
                  f"kids={st.get('kids')} txt={(st.get('txt') or '')[:60]}")
            continue
        check(f"[空态] {view_id} 显示设计过的空态", st.get("empty", False),
              (st.get("emptyText") or "").strip()[:80])
    print("  空态报告:", json.dumps(empty_report, ensure_ascii=False)[:1200], flush=True)
    shot(page, "obs-12-empty-state.png")


def main() -> int:
    os.makedirs(SHOTS, exist_ok=True)
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1600, "height": 940})
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)

        stage_run_with_data(page)
        stage_views(page)
        stage_empty(page)

        browser.close()

    passed = sum(1 for r in RESULTS if r["ok"])
    print("\n" + "=" * 64)
    print(f"结果: {passed}/{len(RESULTS)} 通过")
    for r in RESULTS:
        if not r["ok"]:
            print(f"  失败: {r['name']}  {r['detail']}")
    print("=" * 64)

    out = os.path.join(REPO_ROOT, "data", "_regress", "observability_shots.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(RESULTS, f, ensure_ascii=False, indent=2)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
