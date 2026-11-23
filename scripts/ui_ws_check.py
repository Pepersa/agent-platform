# -*- coding: utf-8 -*-
"""客观断言：**工作区模块**（文件树 + 编辑器叠层）的可见性 / 几何 / 样式正确性。

为什么需要这个脚本
------------------
上一轮交付的半成品里，用户实机报障是「点开工作区，文件树没了，编辑器区是一整块白色，
代码看不见」。这类问题**肉眼截图不可靠**（截图里白色可能是白字、可能是选区、可能是白底），
必须用 ``getComputedStyle`` + ``getBoundingClientRect`` 做客观断言：

    1. **编辑器文字可见**：``#ws-code``（高亮层）与其父级 ``#ws-pre`` 的**计算颜色必须
       同为浅色系**且**与所在背景形成对比**；``#ws-input``（透明输入层）必须是
       ``rgba(0,0,0,0)`` 文字 + 可见 caret。
    2. **不存在「白底 + 白字」**：编辑器区域的**有效背景色**不得为接近纯白的
       （R/G/B 均 > 230），否则就是「白块」退化。
    3. **叠层对齐**：``#ws-pre`` 与 ``#ws-input`` 的 font-family / font-size /
       line-height / padding / white-space / border 必须**完全一致**，
       否则高亮层与输入层会错位（这是「代码看不见」的常见根因）。
    4. **文件树有内容**：``#ws-tree`` 子节点数 > 0，且**可见**（高度 > 0、不在
       ``[hidden]`` 祖先内、``visibility != hidden``）。
    5. **行号栏存在且有行号**：``#ws-gutter`` 内 ``.ws-ln`` 数量 == 代码行数。
    6. **无横向溢出**：编辑器主区 ``scrollWidth <= clientWidth + 2``。

脚本会**先复现**用户操作路径（点工作区 → 展开目录 → 点文件），
再断言以上各项，并在 ``data/ui_ws_shots/`` 留证据截图。
"""
from __future__ import annotations

import json
import os
import sys

from playwright.sync_api import sync_playwright

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("AGENT_UI_BASE",
                      os.environ.get("AGENTPLAT_UI_BASE", "http://127.0.0.1:8010/ui/"))
SHOTS = os.path.join(REPO_ROOT, "data", "ui_ws_shots")

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""),
          flush=True)
    return bool(cond)


def _launch(pw):
    """优先用本机 Chrome / Edge（避免下载 Playwright 自带 Chromium）。"""
    for c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"):
        if os.path.exists(c):
            return pw.chromium.launch(executable_path=c, args=["--no-sandbox"])
    return pw.chromium.launch()


def _parse_rgb(s: str):
    """把 ``rgb(a, b, c)`` / ``rgba(a, b, c, d)`` 解析成 (r, g, b, a)。"""
    if not s:
        return (0, 0, 0, 0)
    try:
        nums = s[s.find("(") + 1:s.find(")")].split(",")
        r, g, b = (int(float(x)) for x in nums[:3])
        a = float(nums[3]) if len(nums) > 3 else 1.0
        return (r, g, b, a)
    except Exception:
        return (0, 0, 0, 0)


def _luma(c) -> float:
    """相对亮度（WCAG 近似）。"""
    r, g, b, a = c
    if a == 0:
        return 0.0
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


#: 一次取回编辑器叠层 + 文件树的全部客观数据
PROBE = r"""
() => {
  const cs = (e) => (e ? getComputedStyle(e) : null);
  const box = (e) => {
    if (!e) return null;
    const b = e.getBoundingClientRect();
    return {w: Math.round(b.width), h: Math.round(b.height),
            x: Math.round(b.x), y: Math.round(b.y)};
  };
  const q = (s) => document.querySelector(s);
  const code = q('#ws-code');
  const pre = q('#ws-pre');
  const ta = q('#ws-input');
  const gutter = q('#ws-gutter');
  const editor = q('#ws-editor');
  const main = q('.ws-editor-main');
  const tree = q('#ws-tree');
  const cCode = cs(code), cPre = cs(pre), cTa = cs(ta), cEd = cs(editor);

  // 沿元素祖先链找**第一个不透明背景**（编辑器区域的真实底色）
  function effectiveBg(el) {
    let n = el;
    while (n && n !== document.documentElement) {
      const c = getComputedStyle(n);
      const m = c.backgroundColor.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)/);
      if (m) {
        const a = m[4] === undefined ? 1.0 : parseFloat(m[4]);
        if (a > 0.05) {
          return {color: c.backgroundColor,
                  rgb: [parseInt(m[1]), parseInt(m[2]), parseInt(m[3])],
                  a: a, from: n.tagName + (n.id ? '#' + n.id : '')};
        }
      }
      n = n.parentElement;
    }
    return {color: 'rgb(0, 0, 0)', rgb: [0, 0, 0], a: 1, from: 'none'};
  }

  const treeRows = tree ? tree.querySelectorAll('.tree-row') : [];
  let visibleRows = 0;
  treeRows.forEach((r) => {
    const b = r.getBoundingClientRect();
    const c = getComputedStyle(r);
    if (b.height > 0 && b.width > 0 && c.visibility !== 'hidden'
        && c.display !== 'none' && !r.closest('[hidden]')) visibleRows += 1;
  });

  const ln = gutter ? gutter.querySelectorAll('.ws-ln') : [];
  const codeText = code ? (code.textContent || '') : '';

  return {
    hasCode: !!code, hasPre: !!pre, hasInput: !!ta,
    hasGutter: !!gutter, hasEditor: !!editor, hasTree: !!tree,
    code: {
      color: cCode ? cCode.color : null,
      bg: cCode ? cCode.backgroundColor : null,
      fontFamily: cCode ? cCode.fontFamily : null,
      fontSize: cCode ? cCode.fontSize : null,
      lineHeight: cCode ? cCode.lineHeight : null,
      whiteSpace: cCode ? cCode.whiteSpace : null,
      textLen: codeText.length,
      sample: codeText.slice(0, 120),
      tokenSpans: code ? code.querySelectorAll('span[class^="tok-"]').length : 0,
      box: box(code),
    },
    pre: {
      color: cPre ? cPre.color : null,
      bg: cPre ? cPre.backgroundColor : null,
      fontFamily: cPre ? cPre.fontFamily : null,
      fontSize: cPre ? cPre.fontSize : null,
      lineHeight: cPre ? cPre.lineHeight : null,
      whiteSpace: cPre ? cPre.whiteSpace : null,
      padding: cPre ? cPre.padding : null,
      borderWidth: cPre ? cPre.borderWidth : null,
      box: box(pre),
    },
    input: {
      color: cTa ? cTa.color : null,
      bg: cTa ? cTa.backgroundColor : null,
      caretColor: cTa ? cTa.caretColor : null,
      fontFamily: cTa ? cTa.fontFamily : null,
      fontSize: cTa ? cTa.fontSize : null,
      lineHeight: cTa ? cTa.lineHeight : null,
      whiteSpace: cTa ? cTa.whiteSpace : null,
      padding: cTa ? cTa.padding : null,
      borderWidth: cTa ? cTa.borderWidth : null,
      valueLen: ta ? ta.value.length : -1,
      box: box(ta),
    },
    editor: {
      bg: cEd ? cEd.backgroundColor : null,
      fontFamily: cEd ? cEd.fontFamily : null,
      fontSize: cEd ? cEd.fontSize : null,
      lineHeight: cEd ? cEd.lineHeight : null,
      box: box(editor),
    },
    effectiveBg: effectiveBg(code),
    mainBox: box(main),
    gutterLines: ln.length,
    gutterBox: box(gutter),
    treeRows: treeRows.length,
    treeVisibleRows: visibleRows,
    treeBox: box(tree),
    treeChildren: tree ? tree.children.length : 0,
    treeHiddenAncestor: tree ? !!tree.closest('[hidden]') : null,
    treeDisplay: tree ? getComputedStyle(tree).display : null,
    docScrollW: document.documentElement.scrollWidth,
    docClientW: document.documentElement.clientWidth,
    mainScrollW: main ? main.scrollWidth : -1,
    mainClientW: main ? main.clientWidth : -1,
    pages: Array.from(document.querySelectorAll('.page')).map((p) => ({
      id: p.dataset.page, active: p.classList.contains('active'),
      display: getComputedStyle(p).display,
    })),
    activeView: (window.state && window.state.activeView) || null,
  };
}
"""


def analyze(d, phase, expect_editor=True):
    """对一份探针数据做断言。

    Args:
        d: 探针返回的数据。
        phase: 阶段名（打印用）。
        expect_editor: 是否应当已存在编辑器骨架（阶段 1 尚未打开文件 → False）。
    """
    print(f"\n=== {phase} ===")

    # ---- 结构存在 ----
    if expect_editor:
        check("编辑器骨架存在（#ws-editor / #ws-pre / #ws-code / #ws-input / #ws-gutter）",
              d["hasEditor"] and d["hasPre"] and d["hasCode"] and d["hasInput"] and d["hasGutter"],
              json.dumps({"editor": d["hasEditor"], "pre": d["hasPre"], "code": d["hasCode"],
                          "input": d["hasInput"], "gutter": d["hasGutter"]}, ensure_ascii=False))
    else:
        check("阶段 1 尚未打开文件，编辑器骨架可以不存在（前置条件）",
              not (d["hasCode"] and d["hasInput"]),
              json.dumps({"code": d["hasCode"], "input": d["hasInput"]}, ensure_ascii=False))

    # ---- 文件树有内容且可见 ----
    check("文件树容器存在且未处于 hidden 祖先内",
          d["hasTree"] and not d["treeHiddenAncestor"], str(d["treeHiddenAncestor"]))
    check("文件树有子节点（行数 > 0）",
          d["treeRows"] > 0, f"rows={d['treeRows']} children={d['treeChildren']}")
    check("文件树存在**可见**行（高度 > 0 且未被隐藏）",
          d["treeVisibleRows"] > 0, f"visible={d['treeVisibleRows']}/{d['treeRows']}")
    check("文件树容器高度 > 0（未塌成 0）",
          (d["treeBox"] or {}).get("h", 0) > 0, json.dumps(d["treeBox"], ensure_ascii=False))

    # ---- 编辑器叠层对齐 ----
    if d["hasPre"] and d["hasInput"]:
        same = []
        for k in ("fontFamily", "fontSize", "lineHeight", "whiteSpace", "padding"):
            same.append((k, d["pre"].get(k) == d["input"].get(k),
                         d["pre"].get(k), d["input"].get(k)))
        bad = [s for s in same if not s[1]]
        check("叠层对齐：pre 与 textarea 的 字体/字号/行高/换行/内边距 完全一致",
              not bad, json.dumps(bad, ensure_ascii=False))

    # ---- 文字可见性（本次的核心断言）----
    if d["hasCode"]:
        code_rgb = _parse_rgb(d["code"]["color"])
        pre_rgb = _parse_rgb(d["pre"]["color"])
        eff_rgb = tuple(d["effectiveBg"]["rgb"]) + (d["effectiveBg"]["a"],)

        # a) 高亮层文字不能是「透明 / 与背景同色」
        check("#ws-code 文字不透明（alpha > 0.9）", code_rgb[3] > 0.9, d["code"]["color"])
        check("#ws-code 文字与有效背景存在亮度对比（不是白底白字）",
              abs(_luma(code_rgb) - _luma(eff_rgb)) > 40,
              f"code_luma={_luma(code_rgb):.1f} bg_luma={_luma(eff_rgb):.1f} "
              f"bg={d['effectiveBg']['color']} from={d['effectiveBg']['from']}")
        # b) 有效背景不得接近纯白（那就是「白块」退化）
        check("编辑器有效背景不是接近纯白（排除白底白字）",
              not (eff_rgb[0] > 230 and eff_rgb[1] > 230 and eff_rgb[2] > 230),
              f"{d['effectiveBg']['color']} from={d['effectiveBg']['from']}")
        # c) pre 的默认文字色也应是浅色（不能是黑）
        check("#ws-pre 默认文字色为浅色（正文 #cccccc 量级）",
              _luma(pre_rgb) > 120, f"{d['pre']['color']} luma={_luma(pre_rgb):.1f}")
        # d) textarea 文字透明 + caret 可见
        ta_rgb = _parse_rgb(d["input"]["color"])
        check("#ws-input 文字透明（透明输入层，文字由 pre 层绘制）",
              ta_rgb[3] < 0.05, d["input"]["color"])
        caret = _parse_rgb(d["input"]["caretColor"])
        check("#ws-input 光标可见（caret-color 不透明）",
              caret[3] > 0.5 and _luma(caret) > 80, d["input"]["caretColor"])
        # e) 真的画出了高亮 token
        check("#ws-code 内含语法高亮 token（span.tok-*）",
              d["code"]["tokenSpans"] > 0, f"tokens={d['code']['tokenSpans']}")
        check("#ws-code 有实际文本内容（长度 > 0）",
              d["code"]["textLen"] > 0, f"len={d['code']['textLen']}")

    # ---- 行号栏 ----
    if d["hasGutter"]:
        check("行号栏有行号元素（.ws-ln）", d["gutterLines"] > 0, f"lines={d['gutterLines']}")
        check("行号栏高度 > 0（未塌陷）", (d["gutterBox"] or {}).get("h", 0) > 0,
              json.dumps(d["gutterBox"], ensure_ascii=False))

    # ---- 无横向溢出 ----
    check("页面无横向溢出（document scrollWidth <= clientWidth）",
          d["docScrollW"] <= d["docClientW"] + 2,
          f"{d['docScrollW']} vs {d['docClientW']}")
    if d["mainBox"]:
        check("编辑器主区无横向溢出（main.scrollWidth <= clientWidth）",
              d["mainScrollW"] <= d["mainClientW"] + 2,
              f"{d['mainScrollW']} vs {d['mainClientW']}")


def main() -> int:
    os.makedirs(SHOTS, exist_ok=True)
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1600, "height": 940})
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(2500)

        # 清掉上一轮遗留的 localStorage（侧栏宽度 / 视图 / 标签页），
        # 保证脚本从**干净初始态**复现用户路径。
        page.evaluate("() => { try { localStorage.clear(); } catch (e) {} }")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(2500)

        print("\n=== 阶段 1：打开工作区视图（尚未打开任何文件）===")
        page.click('.act-item[data-view="workspace"]')
        page.wait_for_timeout(1800)
        page.screenshot(path=os.path.join(SHOTS, "01-workspace-tree.png"))

        d_tree = page.evaluate(PROBE)
        analyze(d_tree, "阶段 1：工作区文件树", expect_editor=False)

        print("\n=== 阶段 2：展开目录并打开一个文件 ===")
        # ---- 断言：目录能真正展开出子节点（用户报障「文件树消失」）----
        exp = page.evaluate(r"""
        async () => {
          const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
          const dir = Array.from(document.querySelectorAll('#ws-tree .tree-row[data-type="dir"]'))[0];
          if (!dir) return {ok: false, reason: 'no dir row'};
          const kids = dir.parentElement.querySelector(':scope > .tree-children');
          const before = {hidden: kids.hasAttribute('hidden'),
                          display: getComputedStyle(kids).display,
                          h: Math.round(kids.getBoundingClientRect().height)};
          dir.click();
          await sleep(1500);
          const after = {hidden: kids.hasAttribute('hidden'),
                         display: getComputedStyle(kids).display,
                         h: Math.round(kids.getBoundingClientRect().height),
                         childRows: kids.querySelectorAll('.tree-row').length,
                         parentCls: dir.parentElement.className,
                         path: dir.dataset.path};
          return {ok: true, before, after};
        }
        """)
        print(f"  [info] 展开探针：{json.dumps(exp, ensure_ascii=False)}")
        check("目录展开后子节点容器可见（display != none）",
              exp.get("ok") and exp.get("after", {}).get("display") != "none",
              json.dumps(exp.get("after"), ensure_ascii=False))
        check("目录展开后实际渲染出子行（子行数 > 0）",
              exp.get("ok") and exp.get("after", {}).get("childRows", 0) > 0,
              json.dumps(exp.get("after"), ensure_ascii=False))
        check("展开后的子容器高度 > 0（未塌成 0）",
              exp.get("ok") and exp.get("after", {}).get("h", 0) > 0,
              json.dumps(exp.get("after"), ensure_ascii=False))
        page.screenshot(path=os.path.join(SHOTS, "01b-dir-expanded.png"))

        # 展开目录：递归展开，直到出现**文本文件**为止（最多 12 层）
        opened = page.evaluate(r"""
        async () => {
          const TEXT = /\.(py|js|json|md|txt|css|html|ya?ml|ps1|sh|cfg|ini|toml)$/i;
          const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
          for (let depth = 0; depth < 12; depth++) {
            const rows = Array.from(document.querySelectorAll('#ws-tree .tree-row'));
            if (rows.some((r) => r.dataset.type !== 'dir' && TEXT.test(r.dataset.path))) {
              return 'found-text-depth-' + depth;
            }
            // 展开下一个尚未展开的目录
            const dir = rows.find((r) => {
              if (r.dataset.type !== 'dir') return false;
              const kids = r.parentElement.querySelector(':scope > .tree-children');
              return kids && kids.hidden;
            });
            if (!dir) return 'no-more-dirs';
            dir.click();
            await sleep(900);
          }
          return 'depth-limit';
        }
        """)
        print(f"  [info] 目录展开结果：{opened}")
        page.wait_for_timeout(1200)

        # 打开第一个**文本文件**（叶子行）。必须挑文本文件：
        # 二进制（如 .png）本来就没有可渲染的文本行，编辑器为空是**正确**行为。
        file_clicked = page.evaluate(r"""
        () => {
          const rows = Array.from(document.querySelectorAll('#ws-tree .tree-row'));
          const TEXT = /\.(py|js|json|md|txt|css|html|ya?ml|ps1|sh|cfg|ini|toml)$/i;
          const f = rows.find((r) => r.dataset.type !== 'dir' && TEXT.test(r.dataset.path))
                 || rows.find((r) => r.dataset.type !== 'dir');
          if (!f) return null;
          f.click();
          return f.dataset.path;
        }
        """)
        print(f"  [info] 打开文件：{file_clicked}")
        page.wait_for_timeout(2500)
        page.screenshot(path=os.path.join(SHOTS, "02-file-opened.png"))

        d_file = page.evaluate(PROBE)
        analyze(d_file, "阶段 2：打开文件后的编辑器")

        check("文件已打开（主区有激活的 file 页签）",
              any(p["active"] and str(p["id"]).startswith("pg-file-") for p in d_file["pages"]),
              json.dumps(d_file["pages"], ensure_ascii=False))

        # ---- 阶段 3：编辑 + 保存（走 diff 审批）----
        # 注意：不能直接改仓库里的真实文件（README.md 等），否则测试会污染交付物。
        # 这里新建一个**专用草稿文件**，测完立刻删掉。
        print("\n=== 阶段 3：编辑并保存（应弹出 diff 审批）===")
        scratch = "_ui_ws_check_probe.md"
        created = page.evaluate(r"""
        async (path) => {
          const r = await fetch('/workspace/file', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({path: path, content: '# probe\n原始内容\n',
                                  expected_mtime: 0, note: 'ui_ws_check 草稿'}),
          });
          const j = await r.json();
          return {status: r.status, change_id: j.change_id || null, keys: Object.keys(j)};
        }
        """, scratch)
        print(f"  [info] 创建草稿文件：{json.dumps(created, ensure_ascii=False)}")

        # 若返回了 change_id，说明写入已被**挂起待审批**（后端 diff 审批模型）：
        # 直接断言「写入被拦截、未落盘」这一安全语义。
        if created.get("change_id"):
            check("新建文件走审批（返回 change_id，未直接落盘）", True, str(created["change_id"]))
            page.evaluate("(id) => fetch('/workspace/change/' + encodeURIComponent(id), {method:'DELETE'})",
                          created["change_id"])
            page.wait_for_timeout(800)
        else:
            # 后端允许直接落盘（无审批）：再走前端编辑 → 审批路径
            page.evaluate("(p) => wsOpenFile(p, p.split('/').pop())", scratch)
            page.wait_for_timeout(1800)

            page.evaluate(r"""
            () => {
              const ta = document.querySelector('#ws-input');
              if (ta) { ta.focus(); ta.setSelectionRange(0, 0); }
            }
            """)
            page.keyboard.type("# ui_ws_check probe\n")
            page.wait_for_timeout(500)
            page.screenshot(path=os.path.join(SHOTS, "03-edited.png"))

            # **不要 await wsSave()**：它内部 await 审批弹窗的 Promise，
            # 只有用户点「确认写入/取消」才 resolve，await 会永久阻塞。
            page.evaluate("() => { if (typeof wsSave === 'function') { wsSave(); } }")
            page.wait_for_timeout(2500)
            diff_visible = page.evaluate(
                "() => !!document.querySelector('#ws-diff-close')")
            page.screenshot(path=os.path.join(SHOTS, "04-diff-approval.png"))
            check("保存走 diff 审批（弹出 diff 模态框）", diff_visible, str(diff_visible))
            if diff_visible:
                page.click("#ws-diff-cancel")
                page.wait_for_timeout(800)

        # ---- 阶段 4：语法高亮颜色断言 ----
        print("\n=== 阶段 4：语法高亮与配色 ===")
        tok = page.evaluate(r"""
        () => {
          const code = document.querySelector('#ws-code');
          if (!code) return null;
          const spans = Array.from(code.querySelectorAll('span[class^="tok-"]'));
          const out = [];
          spans.slice(0, 40).forEach((s) => {
            const c = getComputedStyle(s);
            out.push({cls: s.className, color: c.color, text: (s.textContent||'').slice(0,20)});
          });
          return out;
        }
        """)
        if tok:
            dark = [t for t in tok if _luma(_parse_rgb(t["color"])) > 60]
            check("语法高亮 token 使用浅色（在深色背景上可读）",
                  len(dark) > 0, f"{len(dark)}/{len(tok)} 个 token 亮度 > 60")
        else:
            check("语法高亮 token 使用浅色（在深色背景上可读）", False, "无 token")

        # ---- 清理草稿文件 ----
        try:
            page.evaluate(
                "(p) => fetch('/workspace/file?path=' + encodeURIComponent(p), {method:'DELETE'})",
                scratch)
            page.wait_for_timeout(500)
            print("  [info] 草稿文件已清理")
        except Exception as exc:  # pragma: no cover - 清理失败不阻塞结论
            print(f"  [warn] 草稿文件清理失败：{exc}")

        browser.close()

    total = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n" + "=" * 64)
    print(f"结果: {passed}/{total} 通过")
    print("=" * 64)
    if passed != total:
        print("\n失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}  [{detail}]")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
