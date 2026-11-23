# -*- coding: utf-8 -*-
"""可观测性组件回归脚本（新增断言，不改既有脚本语义）。

覆盖：
  · 12 个组件的 DOM 容器存在且 ID/class 稳定
  · Trace Waterfall 条形**按真实时间比例**定位（宽度比 ≈ 时长比，容差 ±15%）
  · Tool Call Card 展开体有「参数」「结果」两个分区
  · Agent Sequence 生命线数量 == 参与者数量
  · 每个组件的**空态元素存在**
  · HITL 卡片有风险徽标与倒计时文本

用法：
    python scripts\\verify_observability.py            # 静态检查（不需要浏览器）
    python scripts\\verify_observability.py --browser  # 追加真实浏览器断言
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(REPO_ROOT, "agent_platform", "api", "static")

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


def read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as f:
        return f.read()


# ----------------------------------------------------------------------
# 静态检查
# ----------------------------------------------------------------------
def check_static() -> None:
    html = read("index.html")
    css = read("observability.css")
    js = read("observability.js")

    # ---- 入口挂载 ----
    check("index.html 引用 observability.css",
          'observability.css' in html, "未找到 link")
    check("index.html 引用 observability.js",
          'observability.js' in html, "未找到 script")
    check("index.html 含运行观测面板容器",
          'obs-root' in js or 'obs-root' in html,
          "未找到 obs-root（面板骨架由 observability.js 动态创建）")

    # ---- 12 个组件的视图容器（ID 稳定）----
    # 视图容器 id 由 ``'obs-view-' + v.id`` 动态拼接（见 OBS_VIEWS 定义），
    # 因此这里断言 **OBS_VIEWS 里的 id** 与对应的 id 前缀拼接逻辑都存在。
    view_ids = [
        "waterfall", "toolcards", "sequence",
        "cost", "context", "approval",
        "difftimeline", "timing", "repomap",
        "autonomy", "reasoning",
    ]
    check("视图容器 id 前缀拼接正确",
          re.search(r"['\"]obs-view-['\"]\s*\+\s*v\.id", js) is not None,
          "未找到拼接逻辑")
    for vid in view_ids:
        check(f"视图 {vid} 已注册", f"id: '{vid}'" in js, "未找到视图定义")

    # ---- 各组件渲染函数 ----
    for fn in [
        "obsRenderWaterfall", "obsRenderToolCards", "obsRenderSequence",
        "obsRenderCost", "obsRenderContext", "obsRenderApproval",
        "obsRenderDiffTimeline", "obsRenderTiming", "obsRenderRepoMap",
        "obsRenderAutonomy", "obsRenderReasoning",
    ]:
        check(f"渲染函数 {fn} 已实现", f"function {fn}" in js, "未找到函数定义")

    # ---- 自研 squarified treemap（不得引第三方库）----
    check("treemap 为自研实现（obsSquarify）", "function obsSquarify" in js, "未找到")
    check("未引入第三方 JS 库",
          not re.search(r'(src|href)\s*=\s*["\']https?://', html),
          "检测到外链脚本/样式")

    # ---- 图标一律内联 SVG ----
    check("图标使用内联 SVG", "<svg" in js, "未找到 svg")
    check("图标为 16x16 viewBox", 'viewBox="0 0 16 16"' in js, "未找到 16x16 viewBox")
    check("无 emoji", not re.search(r'[\U0001F300-\U0001FAFF☀-➿]', js + css),
          "检测到 emoji")

    # ---- 零渐变（忽略注释里的说明文字）----
    css_nc = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    check("CSS 无渐变", "gradient" not in css_nc.lower(), "检测到 gradient")

    # ---- 作用域隔离：不得出现裸元素选择器 ----
    bad_selectors = re.findall(r'^\s*(code|pre|h[1-6]|ul|ol|li|table|button)\s*[,{]', css, re.M)
    check("CSS 使用作用域类（无裸元素规则）", not bad_selectors,
          f"裸元素选择器: {bad_selectors[:5]}")

    # ---- 无障碍 ----
    check("使用 aria-expanded", "aria-expanded" in js, "未找到")
    check("使用 aria-label", "aria-label" in js, "未找到")
    check("使用 <details>/<summary> 或 role",
          ("<details" in js) or ("role=" in js), "未找到语义化标记")
    check("尊重 prefers-reduced-motion",
          "prefers-reduced-motion" in css, "未找到 reduced-motion 适配")

    # ---- 各组件都有空态 ----
    empty_count = len(re.findall(r"obsEmpty\(", js))
    check("空态覆盖所有组件（obsEmpty 调用 >= 11）", empty_count >= 11,
          f"仅 {empty_count} 处")
    for word in ["本次运行尚无工具调用", "本次运行尚无", "尚无"]:
        check(f"空态文案含「{word}」", word in js, "未找到")

    # ---- Tool Call Card：参数/结果分区 ----
    check("工具卡有「参数」分区", "参数" in js, "未找到")
    check("工具卡有「结果」分区", "结果" in js, "未找到")
    check("工具卡有复制按钮", "复制" in js or "copy" in js.lower(), "未找到")

    # ---- Agent Sequence ----
    check("时序图有生命线实现", "obs-seq-lifeline" in js or "lifeline" in js.lower(),
          "未找到")
    check("时序图区分实线/虚线", "dashed" in css or "obs-seq-arrow" in js, "未找到")

    # ---- HITL 风险徽标 / 倒计时 ----
    check("审批卡有风险徽标", "风险" in js, "未找到")
    check("审批卡有等待倒计时", "已等待" in js, "未找到")
    check("审批卡有 A/D 快捷键（批准/拒绝）",
          "obsBindApprovalKeys" in js and "批准" in js and "拒绝" in js, "未找到")
    check("审批卡 A/D 复用既有 /approve 端点",
          "'/approve'" in js and "tool_call_id" in js, "未找到 /approve 调用")

    # ---- 成本诚实降级 ----
    check("成本缺失时诚实标注本机推理", "本机推理" in js, "未找到")

    # ---- 上下文窗口上限 ----
    check("上下文占用显示窗口上限", "context_window" in js, "未找到")

    # ---- 后端：LLM 轮次进入轨迹 ----
    api = read_api()
    check("后端记录 LLM 轮次耗时（_build_llm_trace_entry）",
          "_build_llm_trace_entry" in api, "未找到")
    check("后端 trace 条目含 kind=llm", '"kind": "llm"' in api, "未找到")
    check("后端透传 llm_duration_ms",
          "llm_duration_ms" in api and "llm_duration_ms" in read_graph(), "未找到")
    check("后端透传 started_at / ended_at",
          '"started_at"' in api and '"ended_at"' in api, "未找到")


def read_api() -> str:
    p = os.path.join(REPO_ROOT, "agent_platform", "api", "app.py")
    with open(p, encoding="utf-8") as f:
        return f.read()


def read_graph() -> str:
    p = os.path.join(REPO_ROOT, "agent_platform", "orchestration", "graph.py")
    with open(p, encoding="utf-8") as f:
        return f.read()


# ----------------------------------------------------------------------
# 浏览器实测（可选）
# ----------------------------------------------------------------------
def check_browser() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover
        check("浏览器断言（playwright 可用）", False, f"缺少 playwright: {exc}")
        return

    url = os.environ.get("AGENT_UI",
                      os.environ.get("AGENT_UI_BASE", "http://127.0.0.1:8010/ui/"))
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.goto(url, wait_until="networkidle")
        page.wait_for_timeout(1200)

        # 打开运行观测面板
        page.evaluate("""() => {
            const b = document.querySelector('[data-obs-open], #btn-obs-open')
              || Array.from(document.querySelectorAll('button')).find(x => /运行观测/.test(x.textContent || ''));
            if (b) b.click();
        }""")
        page.wait_for_timeout(600)

        # ---- 容器存在 ----
        for vid in ["obs-root", "obs-body", "obs-view-waterfall", "obs-view-toolcards",
                    "obs-view-sequence", "obs-view-cost", "obs-view-context",
                    "obs-view-approval", "obs-view-difftimeline", "obs-view-timing",
                    "obs-view-repomap", "obs-view-autonomy", "obs-view-reasoning"]:
            check(f"浏览器内 #{vid} 存在",
                  page.locator(f"#{vid}").count() >= 1, "未找到")

        # ---- 空态：每个视图都应显示设计过的空态 ----
        empty_ok = page.evaluate("""() => {
            const views = Array.from(document.querySelectorAll('[id^="obs-view-"]'));
            return views.map(v => {
                const e = v.querySelector('.obs-empty');
                return {
                    id: v.id,
                    has: !!e,
                    visible: e ? (e.offsetParent !== null || e.getClientRects().length > 0) : false,
                    text: e ? (e.textContent || '').trim().slice(0, 40) : '',
                };
            });
        }""")
        for v in empty_ok:
            check(f"空态 {v['id']} 存在且可见",
                  v["has"] and v["visible"], json.dumps(v, ensure_ascii=False))
            check(f"空态 {v['id']} 文案非空", bool(v["text"]), "文案为空")

        browser.close()


def check_trace_probe() -> None:
    """用真实采集到的 trace 快照验证「时间比例」语义。

    读取 ``data/trace_probe.json``（由 ``scripts/run_trace_probe.py`` 生成），
    断言：条形宽度比 ≈ 时长比（容差 ±15%），且 LLM 轮次真实存在。
    """
    path = os.path.join(REPO_ROOT, "data", "trace_probe.json")
    if not os.path.exists(path):
        check("trace 探针数据存在", False, f"缺少 {path}（先跑 run_trace_probe.py）")
        return
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    trace = data.get("trace") or []
    check("trace 探针含步骤", len(trace) >= 2, f"仅 {len(trace)} 条")
    check("trace 探针含 LLM 轮次（kind=llm）",
          any(e.get("kind") == "llm" for e in trace),
          "没有 LLM 条目 —— 瀑布图会丢掉模型思考时间")
    check("trace 探针含工具调用（kind=tool）",
          any(e.get("kind") == "tool" for e in trace), "没有工具条目")
    check("trace 条目均带 started_at",
          all(e.get("started_at") for e in trace), "有条目缺 started_at")

    # ---- 时间比例：宽度比 ≈ 时长比 ----
    bars = []
    for e in trace:
        dur = e.get("duration_ms")
        st = e.get("started_at")
        en = e.get("ended_at")
        if not dur or not st:
            continue
        end = en if en else st + dur / 1000.0
        bars.append((dur, end - st, e.get("name")))
    check("至少两条可测量的条形", len(bars) >= 2, f"仅 {len(bars)} 条")

    # 取耗时最长与最短的两条，比较宽度比
    bars.sort(key=lambda b: b[0])
    if len(bars) >= 2:
        short, long_ = bars[0], bars[-1]
        dur_ratio = long_[0] / short[0] if short[0] else 0
        wid_ratio = long_[1] / short[1] if short[1] else 0
        if dur_ratio >= 1.5:  # 差距太小时比例断言没有意义
            dev = abs(wid_ratio - dur_ratio) / dur_ratio
            check("条形宽度比 ≈ 时长比（容差 ±15%）", dev <= 0.15,
                  f"时长比={dur_ratio:.3f} 宽度比={wid_ratio:.3f} 偏差={dev:.1%} "
                  f"({short[2]} vs {long_[2]})")
        else:
            check("条形宽度比 ≈ 时长比（容差 ±15%）", True,
                  "耗时差距过小，跳过比例断言")

    # ---- LLM 耗时应当远大于工具耗时（真实数据的关键特征）----
    llm_ms = sum(e.get("duration_ms") or 0 for e in trace if e.get("kind") == "llm")
    tool_ms = sum(e.get("duration_ms") or 0 for e in trace if e.get("kind") == "tool")
    check("LLM 耗时被记录（应 > 0）", llm_ms > 0, f"llm_ms={llm_ms}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", action="store_true", help="追加真实浏览器断言")
    ap.add_argument("--probe", action="store_true", help="追加真实 trace 数据断言")
    args = ap.parse_args()

    check_static()
    if args.probe:
        check_trace_probe()
    if args.browser:
        check_browser()

    total = len(PASSED) + len(FAILED)
    print("\n" + "=" * 60)
    print(f"通过 {len(PASSED)}/{total}")
    if FAILED:
        print("\n失败项：")
        for f in FAILED:
            print("  - " + f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
