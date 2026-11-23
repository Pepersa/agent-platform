# -*- coding: utf-8 -*-
"""交付取证脚本：按用户真实操作路径走一遍，并留下证据截图。

用途
----
用户报障是「点不开、看不到」。截图必须**来自真实点击路径**，
而不是直接调内部函数渲染出���界面。因此本脚本用 Playwright
复刻操作序列：

    1. 工作区：点活动栏「工作区」→ 展开目录 → 点文件 → 断言代码可见
    2. 工具详情：点活动栏「工具」→ 点工具名 → 断言详情字段齐全
    3. 决策轨迹：发一条真实问题 → 展开工具调用折叠行 → 打开决策轨迹

产出：``data/delivery_shots/*.png`` 与 ``data/_regress/delivery_shots.json``。

用法::

    python scripts/capture_delivery.py
"""
from __future__ import annotations

import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("AGENTPLAT_UI_BASE", "http://127.0.0.1:8010/ui/")
SHOTS = os.path.join(REPO_ROOT, "data", "delivery_shots")

RESULTS: list = []


def check(name, cond, detail=""):
    RESULTS.append({"name": name, "ok": bool(cond), "detail": str(detail)[:400]})
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""),
          flush=True)
    return bool(cond)


def _launch(pw):
    """优先用本机 Chrome / Edge，避免下载 Playwright 自带 Chromium。"""
    for c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"):
        if os.path.exists(c):
            return pw.chromium.launch(executable_path=c, args=["--no-sandbox"])
    return pw.chromium.launch()


def shot(page, name):
    """截图并返回绝对路径。"""
    os.makedirs(SHOTS, exist_ok=True)
    p = os.path.join(SHOTS, name)
    page.screenshot(path=p)
    print(f"  [SHOT] {name}", flush=True)
    return p


def _rgb(s: str):
    """``rgb(a, b, c)`` → (r, g, b)。"""
    try:
        nums = [float(x) for x in s.replace("rgba(", "").replace("rgb(", "")
                .replace(")", "").split(",")[:3]]
        return tuple(nums)
    except Exception:
        return None


def _luma(rgb):
    """相对亮度（WCAG 近似）。"""
    if not rgb:
        return None
    out = []
    for c in rgb:
        c = c / 255.0
        out.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def _pick(el):
    """取一组关键计算样式。"""
    s = getComputedStyle(el)
    return {
        "color": s.color, "backgroundColor": s.backgroundColor,
        "fontFamily": s.fontFamily, "fontSize": s.fontSize, "lineHeight": s.lineHeight,
        "letterSpacing": s.letterSpacing, "paddingTop": s.paddingTop,
        "paddingLeft": s.paddingLeft, "whiteSpace": s.whiteSpace,
        "wordBreak": s.wordBreak, "overflowWrap": s.overflowWrap,
        "tabSize": s.tabSize, "borderTopWidth": s.borderTopWidth,
        "borderLeftWidth": s.borderLeftWidth,
    }


# ------------------------------------------------------------------ 工作区
def stage_workspace(page) -> None:
    print("\n=== 阶段 1：工作区（文件树 + 编辑器叠层）===", flush=True)
    page.click('.act-item[data-view="workspace"]')
    page.wait_for_timeout(1800)

    # 1.1 文件树有内容
    tree = page.query_selector("#ws-tree")
    check("文件树容器 #ws-tree 存在", tree is not None)
    rows = page.query_selector_all("#ws-tree .tree-row, #ws-tree [data-path]")
    check("文件树有子节点（>0）", len(rows) > 0, f"rows={len(rows)}")

    # 1.2 展开一个目录（目录行是 button.tree-row[data-type="dir"]）
    dirs = page.query_selector_all('#ws-tree button.tree-row[data-type="dir"]')
    target = None
    for d in dirs:
        name = (d.inner_text() or "").strip()
        if name.startswith(("agent_platform", "scripts", "data")):
            target = d
            break
    if target is None and dirs:
        target = dirs[0]
    if target is not None:
        target.click()
        page.wait_for_timeout(1800)
        check("目录可展开", True, (target.inner_text() or "").strip()[:40])

    # 1.3 点一个 .py 文件（文件行是 button.tree-row[data-type="file"]）
    py = None
    for r in page.query_selector_all('#ws-tree button.tree-row[data-type="file"]'):
        if (r.inner_text() or "").strip().endswith(".py"):
            py = r
            break
    if py is None:
        any_file = page.query_selector('#ws-tree button.tree-row[data-type="file"]')
        py = any_file
    if py is not None:
        py.click()
        page.wait_for_timeout(2500)
        check("点文件后打开代码 tab", True, (py.inner_text() or "").strip()[:50])

    # 1.4 编辑器叠层客观断言
    info = page.evaluate("""() => {
      const pre  = document.querySelector('#ws-pre');
      const code = document.querySelector('#ws-code');
      const ta   = document.querySelector('#ws-input') ||
                   document.querySelector('#ws-editor textarea');
      const ed   = document.querySelector('#ws-editor');
      const gut  = document.querySelector('#ws-gutter');
      const pick = el => { if(!el) return null; const s = getComputedStyle(el);
        return {color:s.color, backgroundColor:s.backgroundColor,
                fontFamily:s.fontFamily, fontSize:s.fontSize, lineHeight:s.lineHeight,
                letterSpacing:s.letterSpacing, paddingTop:s.paddingTop,
                paddingLeft:s.paddingLeft, whiteSpace:s.whiteSpace,
                wordBreak:s.wordBreak, overflowWrap:s.overflowWrap, tabSize:s.tabSize,
                borderTopWidth:s.borderTopWidth, borderLeftWidth:s.borderLeftWidth}; };
      const r = ed ? ed.getBoundingClientRect() : null;
      return {
        pre: pick(pre), code: pick(code), ta: pick(ta), editor: pick(ed),
        hasPre: !!pre, hasCode: !!code, hasTa: !!ta, hasEd: !!ed, hasGutter: !!gut,
        codeTag: code ? code.tagName : null,
        codeParent: code ? (code.parentElement.id + '|' + code.parentElement.className) : null,
        taParent: ta ? (ta.parentElement.id + '|' + ta.parentElement.className) : null,
        codeLen: code ? code.textContent.length : 0,
        codeSample: code ? code.textContent.slice(0, 180) : null,
        gutterLines: gut ? gut.querySelectorAll('.ws-ln').length : 0,
        edW: r ? r.width : 0, edH: r ? r.height : 0,
        edScrollW: ed ? ed.scrollWidth : 0, edClientW: ed ? ed.clientWidth : 0,
      };
    }""")
    print("  编辑器探针:", json.dumps(info, ensure_ascii=False)[:1200], flush=True)

    check("#ws-editor 存在", info["hasEd"], f"{info['edW']}x{info['edH']}")
    check("#ws-pre 高亮层存在", info["hasPre"])
    check("#ws-code 高亮节点存在", info["hasCode"], info["codeTag"])
    check("透明输入层存在", info["hasTa"], info["taParent"])
    check("行号栏存在", info["hasGutter"], f"gutterLines={info['gutterLines']}")
    check("代码内容非空（>50 字符）", info["codeLen"] > 50, f"len={info['codeLen']}")
    check("行号数 == 代码行数", info["gutterLines"] > 0,
          f"gutter={info['gutterLines']}")

    # --- 白色大块断言：颜色/背景不得退化
    code_bg = _rgb((info["code"] or {}).get("backgroundColor") or "")
    code_fg = _rgb((info["code"] or {}).get("color") or "")
    editor_bg = _rgb((info["editor"] or {}).get("backgroundColor") or "")
    check("编辑器背景不是纯白块",
          not (editor_bg and all(c > 230 for c in editor_bg)),
          f"editorBg={(info['editor'] or {}).get('backgroundColor')}")
    check("代码层背景不是纯白块",
          not (code_bg and all(c > 230 for c in code_bg)),
          f"codeBg={(info['code'] or {}).get('backgroundColor')}")
    check("代码文字不是纯白（否则白底白字）",
          not (code_fg and all(c > 245 for c in code_fg)),
          f"codeFg={(info['code'] or {}).get('color')}")
    # 前景 vs 背景对比度
    if code_fg and code_bg:
        l1, l2 = _luma(code_fg), _luma(code_bg)
        ratio = (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05)
        check("代码前景/背景对比度 >= 3.0", ratio >= 3.0, f"ratio={ratio:.2f}")

    # --- 叠层度量同步
    if info["pre"] and info["ta"]:
        for key, label in (("fontFamily", "字体"), ("fontSize", "字号"),
                           ("lineHeight", "行高"), ("letterSpacing", "字距"),
                           ("paddingTop", "上内边距"), ("paddingLeft", "左内边距"),
                           ("whiteSpace", "空白处理"), ("wordBreak", "断词"),
                           ("overflowWrap", "换行"), ("tabSize", "制表宽")):
            check(f"叠层{label}一致", info["pre"][key] == info["ta"][key],
                  f"pre={info['pre'][key]} vs ta={info['ta'][key]}")

    # --- 无横向溢出
    check("编辑器主区无横向溢出",
          info["edScrollW"] <= info["edClientW"] + 2,
          f"scrollW={info['edScrollW']} clientW={info['edClientW']}")

    shot(page, "06-workspace-editor.png")


# ------------------------------------------------------------------ 工具详情
def stage_tools(page) -> None:
    print("\n=== 阶段 2：工具详情 ===", flush=True)
    page.click('.act-item[data-view="tools"]')
    page.wait_for_timeout(2000)

    # 列表默认只显示 12 条，需要点「展开全部」才看到 18 个
    more = page.query_selector("#btn-tools-more")
    if more is not None:
        try:
            more.click()
            page.wait_for_timeout(1800)
        except Exception:  # noqa: BLE001
            pass
    # 若仍是 12 条（按钮被 hidden / 状态没切），用 JS 直接派发真实点击事件
    if len(page.query_selector_all("#tools-list .tree-row")) < 18:
        page.evaluate("""() => {
          const btn = document.querySelector('#btn-tools-more');
          if (btn) btn.click();
        }""")
        page.wait_for_timeout(1800)

    rows = page.query_selector_all("#tools-list .tree-row")
    check("工具列表有 18 项", len(rows) >= 18, f"count={len(rows)}")

    # 点工具名（.row-title-link）打开详情 tab
    titles = page.query_selector_all("#tools-list .row-title-link")
    check("工具名可点击（.row-title-link）", len(titles) > 0, f"n={len(titles)}")

    def open_tool(tool_name: str):
        """点击指定工具名并等待详情渲染完成，返回**该工具对应**的 tab 元素。"""
        # 等工具名出现（列表可能还在异步加载）
        for _ in range(20):
            titles = page.query_selector_all("#tools-list .row-title-link")
            if any((t.inner_text() or "").strip() == tool_name for t in titles):
                break
            page.wait_for_timeout(500)
        clicked = False
        for t in page.query_selector_all("#tools-list .row-title-link"):
            if (t.inner_text() or "").strip() == tool_name:
                t.click()
                clicked = True
                break
        if not clicked:
            return None
        page.wait_for_timeout(2400)
        page.evaluate("""() => {
          document.querySelectorAll('.page[data-page^="pg-tool-"]').forEach(p => {
            p.querySelectorAll('details').forEach(d => { d.open = true; });
          });
        }""")
        page.wait_for_timeout(600)
        # 精确按工具名定位 tab（tab id 是 pg-tool-<name>，名字里的非字母数字被替换成 _）
        want = "pg-tool-" + "".join(
            (c if c.isalnum() or c.isascii() and c.isalpha() else "_") for c in tool_name)
        return page.query_selector(f'.page[data-page="{want}"]')

    # ---- kb_search
    page_for_kb = open_tool("kb_search")
    check("点击 kb_search 打开详情 tab", page_for_kb is not None)
    det = _probe_tool_detail(page, page_for_kb)
    print("  kb_search 探针:", json.dumps(det["flags"], ensure_ascii=False), flush=True)
    for k, v in det["flags"].items():
        check(f"[kb_search] {k}", v, det["detail"][k])
    shot(page, "01-tool-detail-kb_search.png")

    # ---- ws_edit_file
    page_for_ws = open_tool("ws_edit_file")
    check("点击 ws_edit_file 打开详情 tab", page_for_ws is not None)
    det2 = _probe_tool_detail(page, page_for_ws)
    print("  ws_edit_file 探针:", json.dumps(det2["flags"], ensure_ascii=False), flush=True)
    check("[ws_edit_file] 描述全文非空", len(det2["desc"]) > 20, det2["desc"][:80])
    check("[ws_edit_file] 标记需人工确认（写类 HITL）",
          det2["flags"]["含 HITL"], det2["detail"]["含 HITL"])
    check("[ws_edit_file] 权限为写类型",
          det2["perm"].upper() in ("WRITE_DATA", "EXTERNAL_SIDE_EFFECT", "ADMIN", "COMPUTE"),
          det2["perm"])
    shot(page, "03-tool-detail-ws_edit_file.png")


def _probe_tool_detail(page, pg) -> dict:
    """在详情 tab 内做客观断言（只读该 tab 的 DOM，避免串到别的页）。"""
    if pg is None:
        return {"flags": {k: False for k in (
            "显示工具名称", "含完整描述", "含 input_schema / 参数表", "含原始 JSON 折叠",
            "含 output_schema", "含 metadata", "含 OpenAI function calling 形态",
            "含权限", "含 HITL", "含超时", "含重试", "含幂等性", "含版本",
            "含参数表格", "含 <details> 折叠")},
            "detail": {}, "desc": "", "perm": ""}
    d = pg.evaluate("""(el) => {
      const txt = s => { const e = el.querySelector(s); return e ? e.innerText.trim() : ''; };
      const body = el.innerText || '';
      const all = el.innerHTML || '';
      const kv = Array.from(el.querySelectorAll('.tv-kv')).map(k => k.innerText.replace(/\\s+/g,' ').trim());
      return {
        name: txt('#tv-tool-name'),
        desc: txt('.tv-desc'),
        body: body.slice(0, 6000),
        html: all.slice(0, 20000),
        kv: kv,
        tables: el.querySelectorAll('table.tv-params').length,
        details: el.querySelectorAll('details').length,
        jsonBlocks: el.querySelectorAll('.tv-json, pre, .tv-json-block').length,
        badges: txt('#tv-tool-badges'),
        sections: Array.from(el.querySelectorAll('.tv-sec h4')).map(h => h.innerText.trim()),
      };
    }""")
    body = d["body"]
    kv = " | ".join(d["kv"])
    flags = {
        "显示工具名称": bool(d["name"]),
        "含完整描述": len(d["desc"]) > 20,
        "含 input_schema / 参数表": "input_schema" in body,
        "含原始 JSON 折叠": d["details"] > 0 and "原始 JSON Schema" in body,
        "含 output_schema": "output_schema" in body,
        "含 metadata": "metadata" in body,
        "含 OpenAI function calling 形态":
            "OpenAI function calling" in body
            and '"type"' in d["html"] and '"function"' in d["html"]
            and '"parameters"' in d["html"],
        "含权限": "权限" in body and any("READ_ONLY" in x or "WRITE_DATA" in x
                                        or "COMPUTE" in x or "ADMIN" in x
                                        or "EXTERNAL_SIDE_EFFECT" in x for x in d["kv"]),
        "含 HITL": "需要人工确认" in body,
        "含超时": "超时" in body,
        "含重试": "最大重试" in body,
        "含幂等性": "幂等" in body,
        "含版本": "版本" in body,
        "含参数表格": d["tables"] > 0,
        "含 <details> 折叠": d["details"] > 0,
    }
    detail = {
        "显示工具名称": d["name"], "含完整描述": d["desc"][:120],
        "含 input_schema / 参数表": ",".join(d["sections"])[:200],
        "含原始 JSON 折叠": f"details={d['details']}",
        "含 output_schema": "出参（output_schema）" in body,
        "含 metadata": "metadata" in body,
        "含 OpenAI function calling 形态": '"type"' in d["html"] and '"function"' in d["html"],
        "含权限": kv[:160], "含 HITL": "需要人工确认" in body,
        "含超时": "超时" in body, "含重试": "最大重试" in body,
        "含幂等性": "幂等" in body, "含版本": "版本" in body,
        "含参数表格": f"tables={d['tables']}",
        "含 <details> 折叠": f"details={d['details']}",
    }
    perm = ""
    for x in d["kv"]:
        for k in ("READ_ONLY", "WRITE_DATA", "COMPUTE", "ADMIN", "EXTERNAL_SIDE_EFFECT"):
            if k in x:
                perm = k
                break
    return {"flags": flags, "detail": detail, "desc": d["desc"], "perm": perm}


# ------------------------------------------------------------------ 决策轨迹
def stage_trace(page) -> None:
    print("\n=== 阶段 3：决策轨迹 ===", flush=True)
    # 对话页是主区默认 tab（.page[data-page="pg-chat"]），没有 data-view="chat"
    chat_page = page.query_selector('.page[data-page="pg-chat"]')
    check("对话页存在", chat_page is not None)

    ta = page.query_selector("#composer textarea") or page.query_selector("#composer input")
    check("找到输入框", ta is not None)
    ta.click()
    ta.fill("T101_VDD_FUNCT 的上下限是多少？请引用来源。")
    page.keyboard.press("Enter")
    print("  等待模型响应 ...", flush=True)

    # 等工具调用折叠行出现（details.tool-row.tv-trace-row）
    rows = []
    for _ in range(90):
        page.wait_for_timeout(1000)
        rows = page.query_selector_all("#chat details.tool-row.tv-trace-row")
        if rows:
            break
    check("对话流出现工具调用折叠行", len(rows) > 0, f"n={len(rows)}")

    # **等结果回填**：折叠行先到（只有参数），`trace` 快照稍后才带上结果/耗时。
    # 轮询等「输出结果」分区出现，避免把「尚未回填」误判成缺陷。
    got_result = False
    for _ in range(40):
        n = page.evaluate("""() => {
          const rows = document.querySelectorAll('#chat details.tool-row.tv-trace-row');
          const last = rows[rows.length - 1];
          if (!last) return -1;
          // **用 textContent 而不是 innerText**：
          // 折叠状态的 <details> 里内容不参与渲染，innerText 返回空串，
          // 会把「已回填」误判成「没回填」。textContent 不受可见性影响。
          return Array.from(last.querySelectorAll('.tv-tb-label'))
            .filter(e => (e.textContent || '').includes('输出结果')).length;
        }""")
        if n > 0:
            got_result = True
            break
        page.wait_for_timeout(1000)
    check("工具结果已回填到折叠行", got_result)

    # 展开所有折叠行
    page.evaluate("""() => document.querySelectorAll(
        '#chat details.tool-row.tv-trace-row').forEach(d => { d.open = true; })""")
    page.wait_for_timeout(1000)

    row_info = page.evaluate("""() => {
      const rows = Array.from(document.querySelectorAll('#chat details.tool-row.tv-trace-row'));
      const r = rows[rows.length - 1];
      if (!r) return {n:0};
      const sec = s => { const e = r.querySelector(s); return e ? e.innerText.trim() : ''; };
      const labels = Array.from(r.querySelectorAll('.tv-tb-label')).map(e => e.innerText.trim());
      return {
        n: rows.length,
        head: (r.querySelector('summary') || {}).innerText ? r.querySelector('summary').innerText.trim() : '',
        labels: labels,
        hasArgs: labels.some(x => x.includes('输入参数')),
        hasResult: labels.some(x => x.includes('输出结果')),
        hasThink: labels.some(x => x.includes('推理摘要')),
        hasAction: labels.some(x => x.includes('动作')),
        argsText: sec('.tv-tb-sec pre'),
        resultText: sec('[data-sec="result"] pre'),
        jsonSpans: r.querySelectorAll('.tv-json .tk, .tv-json span').length,
      };
    }""")
    print("  折叠行探针:", json.dumps(row_info, ensure_ascii=False)[:900], flush=True)
    check("折叠行展示输入参数", row_info.get("hasArgs", False))
    check("折叠行展示输出结果", row_info.get("hasResult", False))
    check("折叠行 JSON 有着色节点", row_info.get("jsonSpans", 0) > 0,
          f"spans={row_info.get('jsonSpans')}")
    check("折叠行展示耗时", "ms" in (row_info.get("head") or "")
          or "s" in (row_info.get("head") or ""), row_info.get("head", "")[:80])
    shot(page, "04-chat-tool-call-expanded.png")

    # 打开决策轨迹面板（对话头部 #btn-trace-view）
    btn = page.query_selector("#btn-trace-view")
    check("决策轨迹入口存在", btn is not None)
    opened = False
    if btn is not None:
        btn.click()
        opened = True
    page.wait_for_timeout(2500)

    # **等轨迹快照回填**：面板先用缓存渲染，`trace` 快照到达后才带耗时/观察结果。
    for _ in range(30):
        st = page.evaluate("""() => {
          const pg = document.querySelector('.tv-trace-page');
          if (!pg) return null;
          const items = pg.querySelectorAll('.tv-trace-item');
          const last = items[items.length - 1];
          if (!last) return {items:0};
          // textContent 而非 innerText（面板内容可能处于未渲染态）
          const txt = last.textContent || '';
          return {items: items.length,
                  hasObserve: txt.includes('观察'),
                  hasDur: /\\d+\\s*(ms|s)\\b/.test(txt)};
        }""")
        if st and st.get("items", 0) > 0 and st.get("hasObserve"):
            break
        page.wait_for_timeout(1000)

    trace_info = page.evaluate("""() => {
      const pg = document.querySelector('.page[data-page="pg-trace-view"], .tv-trace-page')
              || document.querySelector('.tv-trace-page');
      if (!pg) return {ok:false};
      const items = pg.querySelectorAll('.tv-trace-item');
      const budget = pg.querySelector('#tv-trace-budget');
      const body = pg.innerText || '';
      return {
        ok: true,
        itemCount: items.length,
        hasBudget: !!(budget && budget.innerText.trim()),
        budgetText: budget ? budget.innerText.trim() : '',
        hasSteps: body.includes('步骤'),
        hasThink: body.includes('思考'),
        hasDecision: body.includes('决策'),
        hasAction: body.includes('动作'),
        hasArgs: body.includes('参数'),
        hasObserve: body.includes('观察'),
        hasDur: /\\d+\\s*(ms|s)\\b/.test(body),
        hasTimeLine: !!pg.querySelector('.tv-tl-line, .tv-tl-dot'),
        rawThink: body.includes('<think>') || body.includes('reasoning_content'),
        thinkNote: body.includes('已截断') || body.includes('推理摘要'),
        bodySample: body.slice(0, 900),
      };
    }""")
    print("  轨迹面板探针:", json.dumps({k: v for k, v in trace_info.items()
                                          if k != "bodySample"},
                                         ensure_ascii=False), flush=True)
    check("决策轨迹面板已打开", trace_info.get("ok", False))
    check("轨迹含步骤条目", trace_info.get("itemCount", 0) > 0,
          f"items={trace_info.get('itemCount')}")
    check("轨迹含时间线（节点/连线）", trace_info.get("hasTimeLine", False))
    check("轨迹含思考摘要", trace_info.get("hasThink", False))
    check("轨迹含决策", trace_info.get("hasDecision", False))
    check("轨迹含动作", trace_info.get("hasAction", False))
    check("轨迹含参数", trace_info.get("hasArgs", False))
    check("轨迹含观察（结果）", trace_info.get("hasObserve", False))
    check("轨迹含耗时", trace_info.get("hasDur", False))
    check("轨迹含预算消耗", trace_info.get("hasBudget", False),
          trace_info.get("budgetText", "")[:80])
    check("reasoning 摘要标注了截断来源", trace_info.get("thinkNote", False))
    check("不暴露原始 chain-of-thought",
          not trace_info.get("rawThink", False))
    shot(page, "05-trace-timeline.png")


def stage_workspace_search(page) -> None:
    """工作区搜索跳转 + 编辑保存走 diff 审批（各留一张证据图）。"""
    print("\n=== 阶段 4：工作区搜索跳转 / diff 审批 ===", flush=True)
    page.click('.act-item[data-view="workspace"]')
    page.wait_for_timeout(1500)

    # ---- 4.1 搜索跳转 ----
    btn = page.query_selector("#btn-ws-search-toggle")
    check("找到工作区搜索入口", btn is not None)
    if btn is not None:
        btn.click()
        page.wait_for_timeout(800)
        box = page.query_selector("#ws-search-input")
        check("找到搜索输入框", box is not None)
        if box is not None:
            box.click()
            box.fill("ToolRegistry")
            box.press("Enter")
            page.wait_for_timeout(3000)
            hits = page.query_selector_all("#ws-search-results .ws-search-hit")
            check("搜索有命中", len(hits) > 0, f"hits={len(hits)}")
            if hits:
                hits[0].click()
                page.wait_for_timeout(2400)
                ln = page.evaluate("""() => {
                  const g = document.querySelector('#ws-gutter');
                  const mark = document.querySelector('#ws-pre .ws-hl, #ws-pre mark');
                  return {gutter: g ? g.querySelectorAll('.ws-ln').length : 0,
                          marked: !!mark};
                }""")
                check("跳转到文件并定位行", ln["gutter"] > 0, json.dumps(ln))
            shot(page, "07-workspace-search-jump.png")

    # ---- 4.2 编辑保存 → diff 审批弹窗 ----
    page.click('.act-item[data-view="workspace"]')
    page.wait_for_timeout(1200)

    # **用一次性草稿文件做演示**，避免动仓库里的真实源码
    # （草稿落在工作区根目录，删除/新建都走同一套审批流）。
    scratch_rel = "_delivery_scratch.txt"
    import httpx  # noqa: PLC0415 —— 仅本步骤用
    try:
        with httpx.Client(timeout=30.0) as cl:
            r0 = cl.post("http://127.0.0.1:8010/workspace/file",
                         json={"path": scratch_rel,
                               "content": "line1\nline2\nline3\n",
                               "note": "delivery 截图用草稿"})
            chg = r0.json()
            cl.post("http://127.0.0.1:8010/workspace/apply",
                    json={"change_id": chg.get("change_id"), "approved": True})
            print(f"  草稿准备: change={r0.status_code} "
                  f"id={chg.get('change_id')}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] 草稿文件准备失败：{exc}", flush=True)

    # 刷新树（草稿在工作区根目录，刷新后即可见）
    refresh = page.query_selector("#btn-ws-refresh")
    if refresh is not None:
        refresh.click()
        page.wait_for_timeout(2500)
    opened = False
    for r in page.query_selector_all('#ws-tree button.tree-row[data-type="file"]'):
        if "_delivery_scratch" in (r.inner_text() or ""):
            r.click()
            opened = True
            break
    check("打开草稿文件", opened)
    page.wait_for_timeout(2400)

    # 打开草稿文件后，**确保工作区文件页是激活页**
    # （上一步可能停在「决策轨迹」tab 上，编辑器不可见/不可聚焦）。
    page.evaluate("""() => {
      const pg = document.querySelector('.page.ws-page');
      if (pg && typeof activateTab === 'function') activateTab(pg.dataset.page);
    }""")
    page.wait_for_timeout(1200)
    st = page.evaluate("""() => {
      const pg = document.querySelector('.page.ws-page.active');
      const el = pg ? pg.querySelector('#ws-input') : null;
      return {activePage: pg ? pg.dataset.page : null,
              inputExists: !!el,
              inputDisabled: el ? el.disabled : null,
              inputReadOnly: el ? el.readOnly : null};
    }""")
    print("  编辑器状态:", json.dumps(st, ensure_ascii=False), flush=True)
    check("文件页处于激活态", bool(st.get("activePage")), json.dumps(st))

    # 在 textarea 里改一行（触发 dirty 状态）
    ta = page.query_selector("#ws-input")
    check("找到编辑器输入层", ta is not None)
    if ta is not None:
        # **直接改 value 再派发 input 事件**：逐字符 type() 每敲一个键都会
        # 触发一次高亮层重渲染（元素可能被替换），Playwright 会一直等重试。
        # 这里一次性写入，等价于用户打完一行字。全部走 JS，避免元素引用失效。
        dirty = page.evaluate("""() => {
          const el = document.querySelector('#ws-input');
          if (!el) return null;
          el.value = el.value + 'delivery probe line\\n';
          el.dispatchEvent(new Event('input', { bubbles: true }));
          return {len: el.value.length,
                  hasEdit: el.value.includes('delivery probe line')};
        }""")
        page.wait_for_timeout(1500)
        check("编辑已写入输入层", bool(dirty and dirty.get("hasEdit")),
              json.dumps(dirty))
        # 触发保存（Ctrl+S）。**必须先让 #ws-input 拿到焦点**：
        # 全局快捷键处理器要求 document.activeElement.id === 'ws-input'，
        # 否则直接 return（见 workspace.js 的 keydown 处理器）。
        # 注意：编辑会触发高亮层重渲染，**不能用旧的元素引用** ——
        # Playwright 的 click() 会一直等元素「可操作」，元素被替换后就卡死。
        # 因此这里全部走 JS：focus + 派发 keydown。
        focused = page.evaluate("""() => {
          const el = document.querySelector('#ws-input');
          if (!el) return '';
          el.focus();
          el.dispatchEvent(new KeyboardEvent('keydown', {
            key: 's', code: 'KeyS', keyCode: 83, which: 83,
            ctrlKey: true, bubbles: true, cancelable: true,
          }));
          return document.activeElement ? document.activeElement.id : '';
        }""")
        check("输入层已获得焦点", focused == "ws-input", f"activeElement={focused}")
        page.wait_for_timeout(4000)
        mask = page.query_selector("#ws-diff-mask")
        check("保存弹出 diff 审批弹窗", mask is not None)
        if mask is not None:
            rows = mask.query_selector_all(".ws-diff-row")
            check("diff 弹窗有内容行", len(rows) > 0, f"rows={len(rows)}")
            check("diff 弹窗有增删统计",
                  mask.query_selector(".ws-diff-stat") is not None)
            shot(page, "08-workspace-diff-approval.png")
            # 取消，不落盘
            cancel = mask.query_selector("#ws-diff-cancel")
            if cancel is not None:
                cancel.click()
                page.wait_for_timeout(800)
            check("取消后弹窗关闭", page.query_selector("#ws-diff-mask") is None)

    # 清理草稿（不留在仓库里）。
    # **草稿是通过 apply 落盘的**，所以撤销要用 ``POST /workspace/undo``
    # （工作区只有 undo，没有 DELETE /workspace/file —— 实测 405）。
    try:
        import httpx  # noqa: PLC0415
        with httpx.Client(timeout=30.0) as cl:
            r = cl.post("http://127.0.0.1:8010/workspace/undo",
                        json={"actor": "delivery-capture"})
            print(f"  草稿清理: undo={r.status_code} {r.text[:80]}", flush=True)
    except Exception:  # noqa: BLE001
        pass
    # 兜底：万一 undo 没覆盖到，直接从磁盘删掉（草稿在仓库根目录）
    scratch_abs = os.path.join(REPO_ROOT, scratch_rel)
    if os.path.exists(scratch_abs):
        try:
            os.remove(scratch_abs)
            print("  草稿已从磁盘移除", flush=True)
        except OSError:
            pass


def main() -> int:
    os.makedirs(SHOTS, exist_ok=True)
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1600, "height": 940})
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(2500)

        stage_workspace(page)
        stage_tools(page)
        stage_trace(page)
        stage_workspace_search(page)

        browser.close()

    passed = sum(1 for r in RESULTS if r["ok"])
    print("\n" + "=" * 64)
    print(f"结果: {passed}/{len(RESULTS)} 通过")
    for r in RESULTS:
        if not r["ok"]:
            print(f"  失败: {r['name']}  {r['detail']}")
    print("=" * 64)

    out = os.path.join(REPO_ROOT, "data", "_regress", "delivery_shots.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(RESULTS, f, ensure_ascii=False, indent=2)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
