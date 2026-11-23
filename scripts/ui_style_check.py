# -*- coding: utf-8 -*-
"""开发辅助：校验页面的**视觉规范**（配色 / 字号 / 圆角 / 层次）。

直接从运行中的页面读取 getComputedStyle，确保真正落地的样式符合
Cursor 深色主题的视觉语言（而非只看 CSS 源码里写了什么）。
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


JS = r"""
(() => {
  const cs = (sel, props) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const s = getComputedStyle(el);
    const o = {};
    props.forEach(p => { o[p] = s[p]; });
    return o;
  };
  const rgb = (c) => {
    if (!c) return null;
    const s = String(c).trim();
    if (s.startsWith('#')) {
      let h = s.slice(1);
      if (h.length === 3) h = h.split('').map(x => x + x).join('');
      if (h.length < 6) return null;
      return [parseInt(h.slice(0,2),16), parseInt(h.slice(2,4),16), parseInt(h.slice(4,6),16)];
    }
    const m = s.match(/\d+(\.\d+)?/g);
    if (!m) return null;
    return m.slice(0,3).map(Number);
  };
  const lum = (c) => { const v = rgb(c); if (!v) return -1;
    return 0.2126*v[0] + 0.7152*v[1] + 0.0722*v[2]; };
  const root = getComputedStyle(document.documentElement);
  const tok = (n) => root.getPropertyValue(n).trim();
  return {
    tokens: {
      bg0: tok('--bg-0'), bg1: tok('--bg-1'), bg2: tok('--bg-2'),
      bg3: tok('--bg-3'), bg4: tok('--bg-4'),
      line: tok('--line'), fg: tok('--fg'), fgDim: tok('--fg-dim'),
      accent: tok('--accent'),
    },
    lum: {
      bg0: lum(tok('--bg-0')), bg1: lum(tok('--bg-1')),
      bg2: lum(tok('--bg-2')), bg4: lum(tok('--bg-4')),
    },
    titlebar: cs('.titlebar', ['backgroundColor', 'borderBottomColor', 'height']),
    activitybar: cs('.activitybar', ['backgroundColor', 'borderRightColor', 'width']),
    sidebar: cs('#sidebar', ['backgroundColor', 'borderRightColor', 'width']),
    main: cs('#main', ['backgroundColor']),
    tabbar: cs('.tabbar', ['backgroundColor', 'borderBottomColor', 'height']),
    tabActive: cs('.tab.active', ['backgroundColor', 'color']),
    tabInactive: cs('.tab:not(.active)', ['backgroundColor', 'color']),
    composer: cs('.composer', ['backgroundColor', 'borderTopColor']),
    composerInner: cs('.composer-inner', ['backgroundColor', 'borderColor', 'borderRadius']),
    sendBtn: cs('#btn-send', ['borderRadius', 'width', 'height', 'backgroundColor']),
    activityActive: cs('.act-item.active', ['color']),
    activityIdle: cs('.act-item:not(.active)', ['color']),
    body: cs('body', ['fontFamily', 'fontSize']),
    mono: cs('.mono, .tool-name, .cite', ['fontFamily']),
    scrollbar: (() => {
      const s = getComputedStyle(document.querySelector('#chat'), '::-webkit-scrollbar-thumb');
      return {width: s.width, bg: s.backgroundColor};
    })(),
    emojiInDom: /[\u{1F300}-\u{1FAFF}]/u.test(document.body.innerText),
    shadows: (() => {
      const out = [];
      document.querySelectorAll('*').forEach(el => {
        const s = getComputedStyle(el);
        if (s.boxShadow && s.boxShadow !== 'none') out.push(el.className + ' :: ' + s.boxShadow);
      });
      return out.slice(0, 10);
    })(),
    gradients: (() => {
      const out = [];
      document.querySelectorAll('*').forEach(el => {
        const s = getComputedStyle(el);
        if (s.backgroundImage && s.backgroundImage.includes('gradient')) {
          out.push(el.className + ' :: ' + s.backgroundImage);
        }
      });
      return out.slice(0, 10);
    })(),
    /* ---- Task 6：Cursor 视觉对齐采样 ---- */
    baseFontSize: cs('body', ['fontSize', 'lineHeight', 'fontFamily']),
    viewTitle: cs('.view-title', ['fontSize', 'fontWeight', 'textTransform', 'letterSpacing']),
    treeRow: cs('.tree-row', ['height', 'fontSize', 'lineHeight']),
    /* 两行列表行（.tree-row2）：知识库 / 工具 / 任务 / 历史会话共用。
       **回归点**：.tree-row 曾被设成固定 22px 高 + flex-wrap: wrap，
       导致第二行（副标题）溢出到行盒下方、与下一行标题重叠 15.75px。
       现在行盒高度自适应（auto），内部两行网格：第一行 20px、第二行 16px。*/
    treeRow2: (() => {
      // 必须挑一个**可见容器**里的行：隐藏视图里的行 getBoundingClientRect 全是 0，
      // 那样断言会变成"空跑"，失去防退化意义。
      const sel = ['#sessions-list', '#kb-doc-list', '#tools-list', '#tasks-list'];
      let el = null;
      for (const s of sel) {
        const box = document.querySelector(s);
        if (!box) continue;
        const vis = Array.from(box.querySelectorAll('.tree-row2')).find((r) => {
          const b = r.getBoundingClientRect();
          return b.width > 0 && b.height > 0;
        });
        if (vis) { el = vis; break; }
      }
      if (!el) return null;
      const s = getComputedStyle(el);
      const l1 = el.querySelector('.row-line1');
      const l2 = el.querySelector('.row-line2');
      const r1 = l1 ? l1.getBoundingClientRect() : null;
      const r2 = l2 ? l2.getBoundingClientRect() : null;
      const title = el.querySelector('.row-line1 .row-title');
      const meta = el.querySelector('.row-line1 .row-meta');
      const rt = title ? title.getBoundingClientRect() : null;
      const rm = meta ? meta.getBoundingClientRect() : null;
      return {
        height: s.height,
        minHeight: s.minHeight,
        display: s.display,
        fontSize: s.fontSize,
        gridTemplateRows: s.gridTemplateRows,
        line1Height: l1 ? getComputedStyle(l1).height : null,
        line2Height: l2 ? getComputedStyle(l2).height : null,
        line2FontSize: l2 ? getComputedStyle(l2).fontSize : null,
        line2Color: l2 ? getComputedStyle(l2).color : null,
        titleFs: title ? getComputedStyle(title).fontSize : null,
        titleOverflow: title ? getComputedStyle(title).textOverflow : null,
        titleWhiteSpace: title ? getComputedStyle(title).whiteSpace : null,
        /* 徽标必须 flex:none（不参与伸缩），否则会压住标题 */
        metaFlex: meta ? getComputedStyle(meta).flex : null,
        titleMinWidth: title ? getComputedStyle(title).minWidth : null,
        /* line1 / line2 是否相交（几何真相） */
        line1Bottom: r1 ? r1.bottom : null,
        line2Top: r2 ? r2.top : null,
        linesOverlap: !!(r1 && r2 && r1.bottom > r2.top + 0.5),
        /* 徽标是否压住标题 */
        metaHitsTitle: !!(rt && rm && rt.right > rm.left + 0.5 &&
                          Math.min(rt.bottom, rm.bottom) - Math.max(rt.top, rm.top) > 0),
      };
    })(),
    tabbarH: cs('.tabbar', ['height']),
    /* 主区标签栏容器与单个 tab 的实际高度。
       **回归点**：模态框的 .tabs 规则曾经泄漏到主标签栏（margin-bottom 12px
       + 重新定义 .tab），导致 #tabs 只有 15px、.tab 只有 14px。
       现在模态框用 .modal-tabs 独立作用域，这里锁死高度防止回归。*/
    tabsContainerH: cs('#tabs', ['height', 'marginBottom', 'gap', 'borderBottomWidth']),
    tabItemH: (() => {
      const el = document.querySelector('#tabs .tab');
      if (!el) return null;
      return {rect: el.getBoundingClientRect().height, fs: getComputedStyle(el).fontSize};
    })(),
    modalTabFs: cs('.modal-tabs .tab', ['fontSize']),
    tabActiveColor: cs('.tab.active', ['color']),
    tabInactiveColor: cs('.tab:not(.active)', ['color']),
    tabActiveBefore: (() => {
      const el = document.querySelector('.tab.active');
      if (!el) return null;
      const s = getComputedStyle(el, '::before');
      return {content: s.content, height: s.height, background: s.backgroundColor};
    })(),
    actIcons: (() => {
      const out = [];
      document.querySelectorAll('.act-item svg').forEach(svg => {
        const s = getComputedStyle(svg);
        out.push({
          vb: svg.getAttribute('viewBox'),
          w: s.width, h: s.height,
          sw: s.strokeWidth,
          cap: s.strokeLinecap, join: s.strokeLinejoin,
        });
      });
      return out;
    })(),
    allSvgViewBoxes: (() => {
      const set = {};
      document.querySelectorAll('svg').forEach(svg => {
        const vb = svg.getAttribute('viewBox');
        if (!vb) return;
        set[vb] = (set[vb] || 0) + 1;
      });
      return set;
    })(),
    transitions: (() => {
      const out = [];
      document.querySelectorAll('.tree-row, .tab, .btn, .chip-btn, .composer-inner, .act-item, .tabbar, #sidebar').forEach(el => {
        const s = getComputedStyle(el);
        if (s.transitionDuration && s.transitionDuration !== '0s') {
          out.push(el.className + ' :: ' + s.transitionDuration + ' / ' + s.transitionProperty);
        }
      });
      return out;
    })(),
    focusVisibleRule: (() => {
      const out = [];
      for (const sheet of Array.from(document.styleSheets)) {
        let rules;
        try { rules = sheet.cssRules; } catch (e) { continue; }
        for (const r of Array.from(rules || [])) {
          if (r.selectorText && r.selectorText.includes(':focus-visible')) out.push(r.selectorText);
          if (r.cssRules) {
            for (const r2 of Array.from(r.cssRules)) {
              if (r2.selectorText && r2.selectorText.includes(':focus-visible')) out.push(r2.selectorText);
            }
          }
        }
      }
      return out;
    })(),
    reducedMotionRule: (() => {
      const out = [];
      for (const sheet of Array.from(document.styleSheets)) {
        let rules;
        try { rules = sheet.cssRules; } catch (e) { continue; }
        for (const r of Array.from(rules || [])) {
          const cond = r.conditionText || (r.media && r.media.mediaText) || '';
          if (String(cond).includes('prefers-reduced-motion')) out.push(String(cond));
        }
      }
      return out;
    })(),
    composerTextarea: cs('.composer textarea', ['fontSize', 'lineHeight', 'maxHeight', 'minHeight']),
    btnPrimary: cs('.btn-primary', ['backgroundColor', 'color', 'borderColor']),
    btnGhost: cs('.btn-ghost', ['backgroundColor', 'borderColor']),
    modeChip: cs('.mode-chip', ['color', 'height', 'borderRadius']),
    sidebarW: cs('#sidebar', ['width']),
    radiusLarge: (() => {
      const out = [];
      document.querySelectorAll('*').forEach(el => {
        const r = parseFloat(getComputedStyle(el).borderRadius) || 0;
        if (r > 10) out.push(el.className + ' :: ' + r + 'px');
      });
      return out.slice(0, 10);
    })(),
  };
})()
"""


def main() -> int:
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1600, "height": 940})
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(2200)
        # 切到「历史会话」视图：那里有 .tree-row2（两行列表行），
        # 否则默认的资源管理器视图里只有单行 .tree-row，采样不到。
        try:
            page.click('.act-item[data-view="sessions"]')
            page.wait_for_timeout(1500)
        except Exception:
            pass
        d = page.evaluate(JS)
        print(json.dumps(d, ensure_ascii=False, indent=2))

        print("\n=== 视觉规范校验 ===")
        t = d["tokens"]
        lum = d["lum"]
        check("背景不是纯黑（#000）", t["bg0"] not in ("#000", "#000000", "rgb(0, 0, 0)"), t["bg0"])
        check("文字不是纯白（#fff）", t["fg"] not in ("#fff", "#ffffff", "rgb(255, 255, 255)"), t["fg"])
        check("背景有 4 级层次且单调递增",
              lum["bg0"] <= lum["bg1"] <= lum["bg2"] < lum["bg4"],
              f"{lum['bg0']} {lum['bg1']} {lum['bg2']} {lum['bg4']}")
        check("编辑器区(#1f1f1f 附近)不是最外层", t["bg2"] != t["bg0"], f"{t['bg2']} vs {t['bg0']}")
        check("强调色唯一（青蓝）", t["accent"].lower() in ("#4d9fff", "#4d9fff"), t["accent"])

        tb = d["titlebar"]
        check("标题栏高度 ≤ 36px", float(tb["height"].replace("px", "")) <= 36, tb["height"])
        check("标题栏与侧栏同色系（暗）",
              float(tb["backgroundColor"].split(",")[1].strip() or 0) < 40, tb["backgroundColor"])

        ab = d["activitybar"]
        check("活动栏宽 48px", 44 <= float(ab["width"].replace("px", "")) <= 52, ab["width"])

        check("主区背景比侧栏亮",
              lum["bg2"] > lum["bg1"], f"{lum['bg2']} > {lum['bg1']}")

        check("活动栏选中项更亮于未选中",
              d["activityActive"]["color"] != d["activityIdle"]["color"],
              f"{d['activityActive']['color']} vs {d['activityIdle']['color']}")

        check("标签页：选中态与未选中态背景不同",
              d["tabActive"]["backgroundColor"] != d["tabInactive"]["backgroundColor"],
              f"{d['tabActive']['backgroundColor']} vs {d['tabInactive']['backgroundColor']}")

        check("输入框无边框（聚焦前 transparent）",
              "rgba(0, 0, 0, 0)" in d["composerInner"]["borderColor"]
              or d["composerInner"]["borderColor"] == "transparent",
              d["composerInner"]["borderColor"])
        check("输入框圆角 ≤ 8px",
              float(d["composerInner"]["borderRadius"].replace("px", "")) <= 8,
              d["composerInner"]["borderRadius"])
        check("发送按钮是圆形（border-radius: 50%）",
              d["sendBtn"]["borderRadius"] == "50%",
              f"{d['sendBtn']['borderRadius']} / {d['sendBtn']['width']}")

        check("UI 字体是系统字体栈",
              "system-ui" in d["body"]["fontFamily"] or "Segoe UI" in d["body"]["fontFamily"],
              d["body"]["fontFamily"][:70])
        check("等宽字体用于代码/引用",
              "Consolas" in (d["mono"]["fontFamily"] or "")
              or "Cascadia" in (d["mono"]["fontFamily"] or ""),
              (d["mono"]["fontFamily"] or "")[:70])

        check("无渐变", not d["gradients"], str(d["gradients"][:3]))
        # 胶囊徽标 / 圆点允许 999px / 50%，但**面板容器**不应有大圆角
        panel_radius = [r for r in d["radiusLarge"]
                        if "pill" not in r and "dot" not in r and "status" not in r
                        and "cite" not in r and "badge" not in r]
        check("无大圆角容器（>10px，排除胶囊/圆点徽标）", not panel_radius, str(panel_radius[:3]))
        # 阴影只允许出现在**浮层**（模态框 / 抽屉 / popover）上，
        # 普通容器（树行 / tab / 按钮 / 卡片）不应有阴影。
        float_layers = ("modal", "drawer", "popover", "menu-popover",
                        "toast", "tooltip")
        non_float = [s for s in d["shadows"]
                     if not any(k in s.split(" :: ")[0] for k in float_layers)]
        check("阴影仅用于浮层（模态/抽屉/popover）", not non_float, str(non_float[:3]))
        check("浮层阴影数量受控（≤8 处）", len(d["shadows"]) <= 8,
              f"n={len(d['shadows'])}")

        check("页面文本无 emoji", not d["emojiInDom"])

        # ================= Task 6：Cursor 视觉对齐断言 =================
        bf = d["baseFontSize"]
        check("正文字号 13px（Cursor 标准，非 14/16）",
              bf["fontSize"] == "13px", bf["fontSize"])
        check("正文行高 1.5",
              abs(float(bf["lineHeight"].replace("px", "")) / 13.0 - 1.5) < 0.05,
              f"{bf['lineHeight']} / {bf['fontSize']}")
        check("UI 字体栈含 system-ui 优先",
              bf["fontFamily"].strip().lower().startswith("system-ui")
              or "system-ui" in bf["fontFamily"],
              bf["fontFamily"][:80])

        vt = d["viewTitle"]
        check("视图标题 13px", vt["fontSize"] == "13px", vt["fontSize"])
        check("视图标题字重 600", vt["fontWeight"] in ("600", "bold"),
              vt["fontWeight"])
        check("视图标题不大写（text-transform: none）",
              vt["textTransform"] == "none", vt["textTransform"])
        check("视图标题无额外字距",
              vt["letterSpacing"] in ("normal", "0px"), vt["letterSpacing"])

        tr = d["treeRow"]
        check("树行高 22px（4px 栅格）", tr["height"] == "22px", tr["height"])
        check("树行字号 12px", tr["fontSize"] == "12px", tr["fontSize"])

        # ---- 两行列表行（.tree-row2）：回归点，锁死「不重叠」的几何前提 ----
        t2 = d["treeRow2"]
        check("两行列表行存在（知识库/工具/任务/会话共用 .tree-row2）",
              t2 is not None, str(t2))
        if t2:
            check("两行列表行高度自适应（不再是锁死的 22px）",
                  t2["height"] != "22px", t2["height"])
            check("两行列表行高度 ≈ 第一行 + 第二行 + 内边距（40px 上下）",
                  36 <= float(t2["height"].replace("px", "")) <= 52,
                  t2["height"])
            check("两行列表行用 grid 布局（不是 flex-wrap）",
                  t2["display"] == "grid", t2["display"])
            check("两行列表行第一行高 20px", t2["line1Height"] == "20px",
                  str(t2["line1Height"]))
            check("两行列表行第二行高 16px", t2["line2Height"] == "16px",
                  str(t2["line2Height"]))
            check("两行列表行副标题字号小于标题（11px < 12px）",
                  t2["line2FontSize"] == "11px" and t2["titleFs"] == "12px",
                  f"{t2['line2FontSize']} / {t2['titleFs']}")
            check("副标题用弱化灰（fg-faint 级）",
                  "110" in t2["line2Color"] or "139" in t2["line2Color"],
                  t2["line2Color"])
            check("标题过长时 ellipsis 截断",
                  t2["titleOverflow"] == "ellipsis"
                  and t2["titleWhiteSpace"] in ("nowrap", "pre"),
                  f"{t2['titleOverflow']} / {t2['titleWhiteSpace']}")
            check("标题 min-width:0（保证 ellipsis 生效，不撑破容器）",
                  t2["titleMinWidth"] == "0px", t2["titleMinWidth"])
            check("徽标 flex 不伸缩（永不吃掉标题空间）",
                  t2["metaFlex"] is not None and "0" in t2["metaFlex"].split()[0],
                  str(t2["metaFlex"]))
            check("**两行列表行 line1 与 line2 矩形不相交**（重叠回归点）",
                  not t2["linesOverlap"],
                  f"line1.bottom={t2['line1Bottom']} line2.top={t2['line2Top']}")
            check("**徽标矩形不与标题矩形相交**（压字回归点）",
                  not t2["metaHitsTitle"], str(t2["metaHitsTitle"]))

        check("标签栏高 28px（4px 栅格）", d["tabbarH"]["height"] == "28px",
              d["tabbarH"]["height"])
        tch = d["tabsContainerH"]
        check("主区 #tabs 容器未被模态框样式污染（无 margin-bottom）",
              tch and tch["marginBottom"] in ("0px", "normal"),
              str(tch))
        tih = d["tabItemH"]
        check("单个 tab 高度约 27-28px（未被压缩到 14px）",
              tih and 26 <= tih["rect"] <= 28,
              str(tih))
        check("模态框 tab 使用独立作用域 .modal-tabs",
              d["modalTabFs"] is not None
              and d["modalTabFs"].get("fontSize") == "12px",
              str(d["modalTabFs"]))
        check("选中 tab 文字为白色系",
              "255" in d["tabActiveColor"]["color"],
              d["tabActiveColor"]["color"])
        check("未选中 tab 文字为 #8b8b8b 级",
              "139" in d["tabInactiveColor"]["color"],
              d["tabInactiveColor"]["color"])
        tb2 = d["tabActiveBefore"]
        check("激活 tab 有 1px 底部强调线",
              tb2 and tb2["height"] in ("1px", "2px") and "77, 159, 255" in tb2["background"],
              str(tb2))

        ai = d["actIcons"]
        # 活动栏原有 6 个图标 + 工作区模块新增的「工作区」入口 = 7 个
        check("活动栏有 7 个图标（原 6 + 工作区 1）", len(ai) == 7, f"n={len(ai)}")
        check("活动栏图标 viewBox 统一 0 0 16 16",
              all(i["vb"] == "0 0 16 16" for i in ai),
              str([i["vb"] for i in ai]))
        check("活动栏图标渲染尺寸 20px",
              all(i["w"] == "20px" for i in ai),
              str([i["w"] for i in ai]))
        check("活动栏图标线宽 1.5px",
              all(i["sw"] in ("1.5px", "1.4px") for i in ai),
              str([i["sw"] for i in ai]))
        check("活动栏图标端点/拐角为 round",
              all(i["cap"] == "round" and i["join"] == "round" for i in ai),
              str([(i["cap"], i["join"]) for i in ai][:2]))

        vb = d["allSvgViewBoxes"]
        bad_vb = [k for k in vb if k != "0 0 16 16"]
        check("页面 SVG viewBox 统一 0 0 16 16（无手写混合）",
              not bad_vb, str(bad_vb))

        trs = d["transitions"]
        check("关键交互元素有 transition", len(trs) >= 5, f"n={len(trs)}")
        durs = set()
        for t in trs:
            for part in t.split(" :: ")[-1].split(", ")[0].split(", "):
                durs.add(part)
        fast = [x for x in durs if x in ("0.12s", "0.120s", "0.1s")]
        check("过渡时长 120ms 级别（Cursor 手感）", len(fast) >= 1, str(sorted(durs)))

        check("存在 :focus-visible 焦点环规则", len(d["focusVisibleRule"]) >= 1,
              str(d["focusVisibleRule"][:3]))
        check("存在 prefers-reduced-motion 降级规则",
              len(d["reducedMotionRule"]) >= 1, str(d["reducedMotionRule"][:2]))

        ct = d["composerTextarea"]
        check("composer 正文 13px", ct["fontSize"] == "13px", ct["fontSize"])
        check("composer 行高 1.6",
              abs(float(ct["lineHeight"].replace("px", "")) / 13.0 - 1.6) < 0.06,
              f"{ct['lineHeight']} / {ct['fontSize']}")
        check("composer 内容自适应高度（max-height 约 6 行 ≈125px）",
              "125px" in ct["maxHeight"], ct["maxHeight"])

        bp = d["btnPrimary"]
        check("primary 按钮是淡色底（非实心蓝块）",
              bp["backgroundColor"] != "rgb(77, 159, 255)",
              bp["backgroundColor"])
        check("primary 按钮文字是强调色",
              "77, 159, 255" in bp["color"], bp["color"])
        bg = d["btnGhost"]
        check("ghost 按钮默认无背景",
              bg["backgroundColor"] in ("rgba(0, 0, 0, 0)", "transparent"),
              bg["backgroundColor"])

        mc = d["modeChip"]
        check("模式 chip 高度 20px（工具条图标按钮规格）",
              mc["height"] == "20px", mc["height"])
        check("模式 chip 圆角 2px", mc["borderRadius"] == "2px", mc["borderRadius"])

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
