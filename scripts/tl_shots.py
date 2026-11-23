# -*- coding: utf-8 -*-
"""Live Timeline 浏览器实测 + 截图脚本。

用途：
  1. 打开 http://127.0.0.1:8010/ui/ ，切到「运行观测」面板；
  2. 截「空状态」；
  3. 发一条真实消息触发 Agent 执行，等 SSE 跑完；
  4. 截「完整时间线」「折叠展开」「耗时视图」「全部视图」；
  5. 检查控制台无 JS 报错、检查时间轴比例正比于耗时。

用法：
    python scripts/tl_shots.py
    python scripts/tl_shots.py --no-run     # 只用已有轨迹，不发新消息
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

from playwright.sync_api import sync_playwright

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = os.path.join(REPO_ROOT, "data", "obs_shots")
URL = os.environ.get("AGENT_UI", "http://127.0.0.1:8010/ui/")

PASSED: list = []
FAILED: list = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    if cond:
        PASSED.append(name)
        print(f"[ok]   {name}")
    else:
        FAILED.append(f"{name} :: {detail}")
        print(f"[FAIL] {name} :: {detail}")
    return bool(cond)


def shot(page, name: str) -> str:
    os.makedirs(SHOTS, exist_ok=True)
    path = os.path.join(SHOTS, name)
    page.screenshot(path=path, full_page=False)
    print(f"  [shot] {path}")
    return path


def open_obs(page) -> None:
    page.evaluate("""() => {
        const b = document.querySelector('[data-obs-open], #btn-obs-view')
          || Array.from(document.querySelectorAll('button')).find(x => /运行观测/.test(x.textContent || ''));
        if (b) b.click();
    }""")
    page.wait_for_timeout(900)


def ensure_timeline(page) -> None:
    page.evaluate("""() => {
        const b = document.querySelector('.tl-view-btn[data-tl-view="timeline"]');
        if (b) b.click();
    }""")
    page.wait_for_timeout(500)


def collapse_all(page) -> None:
    page.evaluate("""() => {
        const host = document.querySelector('#obs-view-timeline');
        if (!host) return;
        host.querySelectorAll('.tl-node[data-open="true"]').forEach(n => {
            n.setAttribute('data-open', 'false');
            const h = n.querySelector('.tl-node-head');
            if (h) h.setAttribute('aria-expanded', 'false');
        });
    }""")


def expand_first_tool(page) -> None:
    page.evaluate("""() => {
        const host = document.querySelector('#obs-view-timeline');
        if (!host) return;
        const n = host.querySelector('.tl-node[data-kind="tool"]');
        if (!n) return;
        n.setAttribute('data-open', 'true');
        const h = n.querySelector('.tl-node-head');
        if (h) h.setAttribute('aria-expanded', 'true');
        n.scrollIntoView({block: 'center'});
    }""")
    page.wait_for_timeout(600)


def send_message(page, msg: str) -> None:
    page.evaluate("""(m) => {
        const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
        const chat = tabs.find(t => (t.textContent || '').trim() === '对话');
        if (chat) chat.click();
    }""", msg)
    page.wait_for_timeout(400)
    page.evaluate("""(m) => {
        const t = Array.from(document.querySelectorAll('textarea'))
            .find(x => (x.placeholder || '').includes('输入问题'));
        if (!t) return;
        t.value = m;
        t.dispatchEvent(new Event('input', {bubbles: true}));
    }""", msg)
    page.wait_for_timeout(200)
    page.evaluate("""() => {
        const t = Array.from(document.querySelectorAll('textarea'))
            .find(x => (x.placeholder || '').includes('输入问题'));
        if (t) t.dispatchEvent(new KeyboardEvent('keydown',
            {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
    }""")
    print("  [send] " + msg)


def wait_done(page, timeout_s: int = 300) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        st = page.evaluate("""() => {
            return {
                finished: (typeof OBS !== 'undefined') ? OBS.finished : false,
                traceLen: (typeof OBS !== 'undefined') ? OBS.trace.length : 0,
                approvals: (typeof OBS !== 'undefined') ? OBS.approvals.length : 0,
            };
        }""")
        if st.get("finished") and st.get("traceLen", 0) > 0:
            print(f"  [done] {int(time.time() - t0)}s trace={st['traceLen']}")
            return True
        time.sleep(2)
    print("  [timeout] 等 SSE 结束超时")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-run", action="store_true", help="不发新消息，用已有轨迹")
    ap.add_argument("--msg", default="读一下 agent_platform/core/paths.py 的前 50 行并总结它的作用")
    args = ap.parse_args()

    os.makedirs(SHOTS, exist_ok=True)
    console_errors: list = []

    #: **用系统已装的 Chrome**（Playwright 自带的 chromium 未下载，
    #: 且本项目禁止联网装依赖 —— 硬约束「零 npm / 零 CDN」）。
    CHROME_CANDIDATES = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    launch_kw: dict = {}
    for c in CHROME_CANDIDATES:
        if os.path.exists(c):
            launch_kw["executable_path"] = c
            print(f"[browser] {c}")
            break

    #: 独立 user-data-dir：避免与用户日常 Chrome / 其它 Playwright 实例
    #: 抢同一个 profile 单例锁（共享 profile 时 goto 会静默挂住）。
    _ud = os.path.join(tempfile.gettempdir(), "tl_shots_chrome_profile")
    os.makedirs(_ud, exist_ok=True)
    launch_kw["args"] = [
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-extensions",
    ]

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            _ud, viewport={"width": 1600, "height": 1000}, **launch_kw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("console", lambda m: console_errors.append(m.text)
                if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append("pageerror: " + str(e)))

        # 注意：UI 会常驻一条 SSE / WS 长连接，networkidle 永远不会触发，
        # 因此这里用 domcontentloaded + 显式等待，而不是 networkidle。
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_load_state("load")
        page.wait_for_timeout(1500)

        # ---- 0. 新建会话（确保空状态是真实的空，不是上一轮的残留）----
        page.evaluate("""() => {
            const b = document.querySelector('#btn-new')
              || Array.from(document.querySelectorAll('button')).find(x =>
                   /新建会话/.test((x.getAttribute('aria-label') || '') + (x.title || '')));
            if (b) b.click();
        }""")
        page.wait_for_timeout(900)

        # ---- 1. 空状态 ----
        open_obs(page)
        ensure_timeline(page)
        empty_info = page.evaluate("""() => {
            const h = document.querySelector('#obs-view-timeline');
            const e = h ? h.querySelector('.tl-empty') : null;
            return {
                has: !!e,
                visible: e ? (e.offsetParent !== null || e.getClientRects().length > 0) : false,
                text: e ? (e.textContent || '').trim() : '',
                nodes: h ? h.querySelectorAll('.tl-node').length : -1,
            };
        }""")
        check("空态元素存在", empty_info["has"], "未找到 .tl-empty")
        check("空态可见", empty_info["visible"], str(empty_info))
        check("空态文案非空", bool(empty_info["text"]), empty_info["text"][:60])
        check("空态时没有节点", empty_info["nodes"] == 0, str(empty_info["nodes"]))
        shot(page, "tl-01-empty.png")

        # ---- 2. 真实执行 ----
        if not args.no_run:
            send_message(page, args.msg)
            wait_done(page)

        open_obs(page)
        ensure_timeline(page)

        # ---- 3. 完整时间线（全部折叠）----
        collapse_all(page)
        page.wait_for_timeout(500)
        node_info = page.evaluate("""() => {
            const h = document.querySelector('#obs-view-timeline');
            const nodes = Array.from(h ? h.querySelectorAll('.tl-node') : []);
            return {
                count: nodes.length,
                kinds: nodes.map(n => n.getAttribute('data-kind')),
                statuses: nodes.map(n => n.getAttribute('data-status')),
                heads: nodes.map(n => {
                    const x = n.querySelector('.tl-node-head');
                    return x ? x.textContent.replace(/\\s+/g, ' ').trim() : '';
                }),
                ariaOk: nodes.every(n => {
                    const x = n.querySelector('.tl-node-head');
                    return x && x.hasAttribute('aria-expanded')
                        && x.hasAttribute('aria-controls')
                        && x.getAttribute('tabindex') === '0';
                }),
                header: (h && h.querySelector('.tl-header'))
                    ? h.querySelector('.tl-header').textContent.replace(/\\s+/g, ' ').trim() : '',
            };
        }""")
        check("时间线有节点", node_info["count"] > 0, str(node_info["count"]))
        check("节点头部有 aria-expanded/aria-controls/tabindex", node_info["ariaOk"], "")
        check("sticky header 含上下文占用 %", "ctx" in node_info["header"], node_info["header"][:120])
        shot(page, "tl-02-timeline-full.png")

        # ---- 4. 时间轴比例：宽度正比于耗时 ----
        ratio = page.evaluate("""() => {
            const h = document.querySelector('#obs-view-timeline');
            const rows = Array.from(h ? h.querySelectorAll('.tl-node') : []).map(n => {
                const f = n.querySelector('.tl-durbar-fill');
                const d = n.querySelector('.tl-dur');
                if (!f || !d) return null;
                return { w: parseFloat(f.style.width || '0'),
                         d: d.textContent.trim() };
            }).filter(Boolean);
            return rows;
        }""")
        # 把 "1 分 13 s" / "40.02 s" / "2 ms" 解析成毫秒
        def to_ms(txt: str) -> float:
            t = txt.strip()
            if t.endswith("ms"):
                return float(t[:-2])
            if "分" in t:
                mm = t.split("分")[0].strip()
                ss = t.split("分")[1].replace("s", "").strip()
                return float(mm) * 60000 + float(ss) * 1000
            if t.endswith("s"):
                return float(t[:-1]) * 1000
            return 0.0
        # 只比较**宽度可见**的条（宽度 0% 的极短步骤无法做比例断言）
        measured = [(r["w"], to_ms(r["d"])) for r in ratio
                    if to_ms(r["d"]) > 0 and r["w"] >= 1.0]
        check("至少两条可测量的耗时条", len(measured) >= 2, str(measured))
        if len(measured) >= 2:
            measured.sort(key=lambda x: x[1])
            short_w, short_d = measured[0]
            long_w, long_d = measured[-1]
            dur_ratio = long_d / short_d
            wid_ratio = (long_w / short_w) if short_w > 0 else float("inf")
            dev = abs(wid_ratio - dur_ratio) / dur_ratio
            check("耗时条宽度比 ≈ 耗时比（容差 ±15%）", dev <= 0.15,
                  f"耗时比={dur_ratio:.3f} 宽度比={wid_ratio:.3f} 偏差={dev:.1%}")
        shot(page, "tl-03-duration-proportion.png")

        # ---- 5. 折叠展开（工具节点）----
        expand_first_tool(page)
        expanded = page.evaluate("""() => {
            const h = document.querySelector('#obs-view-timeline');
            const n = h ? h.querySelector('.tl-node[data-kind="tool"]') : null;
            if (!n) return { ok: false };
            const panel = n.querySelector('.tl-panel');
            const inner = n.querySelector('.tl-panel-inner');
            const cs = panel ? getComputedStyle(panel) : null;
            /* **声明值**（不是 computed used value）—— computed 会把
               1fr 解析成像素值，声明值才看得出「1fr ↔ 0fr」的实现方式。
               逐条 CSS 规则里找 .tl-panel 的 grid-template-rows。 */
            let declared = '';
            let declaredOpen = '';
            let transition = '';
            for (const sheet of document.styleSheets) {
                let rules;
                try { rules = sheet.cssRules; } catch (e) { continue; }
                for (const rule of rules) {
                    if (!rule.selectorText) continue;
                    /* 只取**精确**的 .tl-panel 规则（不能被
                       .tl-node[data-collapsible="false"] .tl-panel 覆盖）*/
                    if (rule.selectorText.trim() === '.tl-panel') {
                        declared = rule.style.getPropertyValue('grid-template-rows');
                        transition = rule.style.getPropertyValue('transition');
                    }
                    if (rule.selectorText.trim()
                        === '.tl-node[data-open="true"] .tl-panel') {
                        declaredOpen = rule.style.getPropertyValue('grid-template-rows');
                    }
                }
            }
            return {
                ok: true,
                open: n.getAttribute('data-open'),
                hasArgs: !!n.querySelector('[data-zone="args"]'),
                hasResult: !!n.querySelector('[data-zone="result"]'),
                hasCopy: !!n.querySelector('[data-tl-copy]'),
                gridRows: cs ? cs.gridTemplateRows : '',
                declared: declared,
                declaredOpen: declaredOpen,
                transition: transition,
                innerH: inner ? inner.getBoundingClientRect().height : 0,
            };
        }""")
        check("工具节点可展开", expanded.get("ok"), "")
        check("展开区含「参数」分区", expanded.get("hasArgs", False), "")
        check("展开区含「结果」分区", expanded.get("hasResult", False), "")
        check("展开区有复制按钮", expanded.get("hasCopy", False), "")
        check("折叠用 CSS Grid 1fr↔0fr（声明值）",
              "0fr" in (expanded.get("declared") or "")
              and "1fr" in (expanded.get("declaredOpen") or ""),
              f"collapsed={expanded.get('declared')!r} open={expanded.get('declaredOpen')!r}")
        check("折叠动效 180ms cubic-bezier",
              "180ms" in (expanded.get("transition") or "")
              and "0.2" in (expanded.get("transition") or "")
              and "0.38" in (expanded.get("transition") or ""),
              expanded.get("transition", ""))
        check("展开后面板高度 > 0", (expanded.get("innerH") or 0) > 0,
              str(expanded.get("innerH")))
        shot(page, "tl-04-expanded.png")

        # ---- 6. 结果截断（max-height + mask-image）----
        clip = page.evaluate("""() => {
            const h = document.querySelector('#obs-view-timeline');
            const c = h ? h.querySelector('.tl-code.is-clipped') : null;
            if (!c) return { present: false };
            const cs = getComputedStyle(c);
            return {
                present: true,
                maxHeight: cs.maxHeight,
                overflow: cs.overflow,
                mask: cs.maskImage || cs.webkitMaskImage || '',
            };
        }""")
        print(f"  [clip] {json.dumps(clip, ensure_ascii=False)}")

        # ---- 7. 审批节点（若本轮有审批）----
        appr = page.evaluate("""() => {
            const h = document.querySelector('#obs-view-timeline');
            const n = h ? h.querySelector('.tl-node[data-kind="approval"]') : null;
            if (!n) return { present: false };
            const dot = n.querySelector('.tl-dot');
            const cs = dot ? getComputedStyle(dot) : null;
            return {
                present: true,
                hasHeadBtns: !!n.querySelector('[data-tl-approve]') && !!n.querySelector('[data-tl-deny]'),
                ariaExpanded: n.querySelector('.tl-node-head').getAttribute('aria-expanded'),
                dotRadius: cs ? cs.borderRadius : '',
                dotTransform: cs ? cs.transform : '',
                status: n.getAttribute('data-status'),
            };
        }""")
        print(f"  [appr] {json.dumps(appr, ensure_ascii=False)}")
        if appr.get("present"):
            check("审批节点在折叠头直接挂批准/拒绝按钮", appr.get("hasHeadBtns", False), "")
            check("审批节点是菱形（零时长事件）",
                  appr.get("dotRadius", "").startswith("2px"), appr.get("dotRadius", ""))
            shot(page, "tl-05-approval.png")
        else:
            # ---- 7b. 本轮无审批 → 注入一条合成审批，验证审批 UI ----
            print("  [appr] 本轮无真实审批 → 注入合成审批项验证 UI")
            page.evaluate("""() => {
                if (typeof OBS === 'undefined') return;
                OBS.approvals.push({
                    tool_call_id: 'tl-synthetic-approval-1',
                    tool_name: 'ws_edit_file',
                    permission: 'WRITE_DATA',
                    impact: '覆盖 agent_platform/core/paths.py',
                    reversible: true,
                    args: {path: 'agent_platform/core/paths.py', diff: {path: 'agent_platform/core/paths.py',
                        added: 3, removed: 1,
                        lines: [{op:'ctx', old:10, new:10, text:'def resolve(*parts):'},
                                {op:'del', old:11, text:'    return None'},
                                {op:'add', new:11, text:'    return Path(*parts)'},
                                {op:'add', new:12, text:''},
                                {op:'add', new:13, text:'# resolved'}]}},
                    started_at: Date.now() / 1000,
                });
                OBS.trace.push({
                    seq: 900, round: 1, id: 'tl-synthetic-approval-1',
                    name: 'ws_edit_file', args: OBS.approvals[OBS.approvals.length-1].args,
                    result: null, ok: true, error: {},
                    reasoning_summary: '', action_summary: '等待人工确认：ws_edit_file',
                    duration_ms: null, ts: Date.now()/1000, kind: 'approval',
                    started_at: Date.now()/1000, ended_at: null, tokens: null,
                });
                if (typeof tlRender === 'function') tlRender();
            }""")
            page.wait_for_timeout(900)
            appr2 = page.evaluate("""() => {
                const h = document.querySelector('#obs-view-timeline');
                const n = h ? h.querySelector('.tl-node[data-kind="approval"]') : null;
                if (!n) return { present: false };
                const dot = n.querySelector('.tl-dot');
                const cs = dot ? getComputedStyle(dot) : null;
                return {
                    present: true,
                    hasHeadBtns: !!n.querySelector('[data-tl-approve]') && !!n.querySelector('[data-tl-deny]'),
                    dotRadius: cs ? cs.borderRadius : '',
                    dotTransform: cs ? cs.transform : '',
                    status: n.getAttribute('data-status'),
                };
            }""")
            print(f"  [appr2] {json.dumps(appr2, ensure_ascii=False)}")
            if appr2.get("present"):
                check("审批节点在折叠头直接挂批准/拒绝按钮",
                      appr2.get("hasHeadBtns", False), "")
                check("审批节点是菱形（零时长事件）",
                      appr2.get("dotRadius", "").startswith("2px"),
                      appr2.get("dotRadius", ""))
                check("审批节点状态为「等待批准」",
                      appr2.get("status") == "wait", str(appr2.get("status")))
                # 展开审批节点 → 应有 diff 预览 + 拒绝理由输入框
                page.evaluate("""() => {
                    const h = document.querySelector('#obs-view-timeline');
                    const n = h.querySelector('.tl-node[data-kind="approval"]');
                    n.setAttribute('data-open', 'true');
                    const hd = n.querySelector('.tl-node-head');
                    if (hd) hd.setAttribute('aria-expanded', 'true');
                    n.scrollIntoView({block: 'center'});
                }""")
                page.wait_for_timeout(800)
                appr3 = page.evaluate("""() => {
                    const n = document.querySelector('.tl-node[data-kind="approval"]');
                    if (!n) return {};
                    return {
                        hasDiff: !!n.querySelector('[data-zone="diff"]'),
                        hasReason: !!n.querySelector('[data-tl-reason]'),
                        hasApprove: !!n.querySelector('.appr-btn.primary'),
                        hasDeny: !!n.querySelector('.appr-btn.danger'),
                        risk: (n.querySelector('.tl-risk')||{}).textContent || '',
                    };
                }""")
                print(f"  [appr3] {json.dumps(appr3, ensure_ascii=False)}")
                check("审批展开区有 diff 预览", appr3.get("hasDiff", False), "")
                check("审批展开区有拒绝理由输入框", appr3.get("hasReason", False), "")
                check("审批展开区有批准/拒绝按钮",
                      appr3.get("hasApprove", False) and appr3.get("hasDeny", False), "")
                check("审批卡有风险徽标", bool(appr3.get("risk")), appr3.get("risk", ""))
                shot(page, "tl-05-approval.png")
                # 折叠态截图（菱形 + 头部按钮）
                page.evaluate("""() => {
                    const n = document.querySelector('.tl-node[data-kind="approval"]');
                    n.setAttribute('data-open', 'false');
                    const hd = n.querySelector('.tl-node-head');
                    if (hd) hd.setAttribute('aria-expanded', 'false');
                    n.scrollIntoView({block: 'center'});
                }""")
                page.wait_for_timeout(600)
                shot(page, "tl-05b-approval-collapsed.png")

        # ---- 8. 耗时视图（原 Trace Waterfall 仍可达）----
        page.evaluate("""() => {
            const b = document.querySelector('.tl-view-btn[data-tl-view="waterfall"]');
            if (b) b.click();
        }""")
        page.wait_for_timeout(800)
        wf = page.evaluate("""() => {
            const bars = Array.from(document.querySelectorAll('.obs-wf-bar'));
            return {
                count: bars.length,
                widths: bars.map(b => parseFloat(b.getAttribute('data-real-width') || '0')),
            };
        }""")
        check("耗时视图（Trace Waterfall）仍可达", wf["count"] > 0, str(wf["count"]))
        shot(page, "tl-06-waterfall.png")

        # ---- 9. 全部视图（12 个原组件仍可达）----
        page.evaluate("""() => {
            const b = document.querySelector('.tl-view-btn[data-tl-view="gallery"]');
            if (b) b.click();
        }""")
        page.wait_for_timeout(900)
        gal = page.evaluate("""() => {
            const segs = Array.from(document.querySelectorAll('#obs-gallery .obs-seg-btn'));
            const views = Array.from(document.querySelectorAll('#obs-gallery-body .obs-view'));
            return {
                segCount: segs.length,
                labels: segs.map(b => b.textContent.trim()),
                viewCount: views.length,
            };
        }""")
        check("全部视图保留 11 个组件", gal["segCount"] == 11, str(gal["segCount"]))
        check("全部视图保留 11 个视图容器", gal["viewCount"] == 11, str(gal["viewCount"]))
        shot(page, "tl-07-gallery.png")

        # ---- 10. 零渐变 / 零 emoji ----
        style = page.evaluate("""() => {
            const grads = [];
            document.querySelectorAll('*').forEach(el => {
                const s = getComputedStyle(el);
                if (s.backgroundImage && s.backgroundImage.includes('gradient')) {
                    grads.push((el.className || el.tagName) + ' :: ' + s.backgroundImage);
                }
            });
            return {
                grads: grads.slice(0, 6),
                emoji: /[\\u{1F300}-\\u{1FAFF}]/u.test(document.body.innerText),
            };
        }""")
        check("页面无 CSS 渐变背景", not style["grads"], str(style["grads"][:2]))
        check("页面文本无 emoji", not style["emoji"], "")

        ctx.close()

    print("\n" + "=" * 60)
    print(f"通过 {len(PASSED)}/{len(PASSED) + len(FAILED)}")
    if console_errors:
        print(f"控制台错误 {len(console_errors)} 条：")
        for e in console_errors[:8]:
            print("  - " + e[:200])
    else:
        print("控制台无 JS 报错")
    if FAILED:
        print("\n失败项：")
        for f in FAILED:
            print("  - " + f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
