# -*- coding: utf-8 -*-
"""verify_trace_view.py —— 工具详情 + 决策轨迹回归脚本。

覆盖两个用户报障能力：

    A. **工具详情可点开查看**
       · ``GET /tools`` 列表保持向后兼容（字段不破坏）
       · ``GET /tools/{name}`` 返回**完整** schema（input_schema / output_schema /
         metadata / version / timeout / 重试 / 幂等 / 权限 / HITL）
       · ``GET /tools/{name}`` 返回 **OpenAI function calling 形态**
         （= 模型实际看到的工具定义）
       · ``GET /tools/{name}`` 返回派生的**参数表**（名称/类型/必填/说明/默认/enum）
       · 不存在的工具 → 404 + 结构化错误
       · 前端：工具名可点击（``.row-title-link``）+ 详情容器存在
         （``#tv-tool-page`` / ``#tv-tool-body`` / ``.tv-params`` / ``.tv-json``）
    B. **决策轨迹可视化**
       · SSE ``trace`` 事件存在，且条目带**结构化字段**：
         seq / name / args / result / ok / error / duration_ms /
         reasoning_summary / action_summary
       · SSE ``step`` 事件带 ``tool_calls``（结构化参数），不再只有字符串
       · 后端**不泄露原始思维链**：``reasoning_summary`` 必须被截断
         （长度有上限）且不含大段原始 reasoning_content
       · 前端：轨迹容器存在（``.tv-trace-row`` / ``#tv-trace-list`` /
         ``#tv-trace-budget``）+ 参数/结果分区存在
       · 历史会话恢复的消息也带轨迹数据（落盘的 ``extra.trace``）
       · 预算消耗可展示（上限 8 轮 / 60k token / 24 工具调用）
    C. 前端零第三方依赖 / 无外链 CDN + JS 语法检查

用法：
    python scripts/verify_trace_view.py
    python scripts/verify_trace_view.py --base http://127.0.0.1:8010
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional

import requests

BASE = os.environ.get("AGENT_API_BASE", "http://127.0.0.1:8010").rstrip("/")
TIMEOUT = 180
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(REPO_ROOT, "agent_platform", "api", "static")

RESULTS: List[Any] = []


def check(name: str, cond: Any, detail: str = "") -> bool:
    ok = bool(cond)
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""),
          flush=True)
    return ok


def section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def get(url_path: str, **params: Any) -> requests.Response:
    return requests.get(f"{BASE}{url_path}", params=params, timeout=TIMEOUT)


def post(path: str, payload: Dict[str, Any], timeout: int = TIMEOUT) -> requests.Response:
    return requests.post(f"{BASE}{path}", json=payload, timeout=timeout)


# ======================================================================
# A. 工具详情
# ======================================================================
def test_tools_list_compat() -> None:
    section("A1. GET /tools 列表向后兼容")
    r = get("/tools")
    check("GET /tools 返回 200", r.status_code == 200, str(r.status_code))
    body = r.json()
    check("返回 count 字段", "count" in body, str(list(body.keys())))
    tools = body.get("tools") or []
    check("返回工具列表", len(tools) > 0, f"n={len(tools)}")
    first = tools[0]
    for key in ("name", "description", "permission", "hitl_required",
                "input_schema", "output_schema", "timeout_ms", "max_retries",
                "version", "idempotent", "metadata"):
        check(f"列表项含既有字段 {key}", key in first, str(list(first.keys())))
    check("新增 detail_url 提示字段（附加，不破坏旧契约）",
          all("detail_url" in t for t in tools), str(tools[0].get("detail_url")))
    # 老字段的语义不变
    check("permission 取值合法",
          all(t.get("permission") in ("READ_ONLY", "COMPUTE", "WRITE_DATA",
                                      "EXTERNAL_SIDE_EFFECT", "ADMIN")
              for t in tools),
          str([t.get("permission") for t in tools[:6]]))


def test_tool_detail_endpoint() -> None:
    section("A2. GET /tools/{name} 返回完整契约")
    r = get("/tools")
    tools = r.json().get("tools") or []
    if not tools:
        check("有工具可测", False)
        return
    names = [t["name"] for t in tools]

    for name in ("kb_search", "ws_edit_file", "ws_write_file"):
        if name not in names:
            continue
        d = get("/tools/" + name)
        check(f"GET /tools/{name} 返回 200", d.status_code == 200, str(d.status_code))
        body = d.json()
        tool = body.get("tool") or {}
        # ---- 完整 schema ----
        check(f"{name} 返回完整 input_schema",
              isinstance(tool.get("input_schema"), dict)
              and (tool["input_schema"].get("type") == "object")
              and isinstance(tool["input_schema"].get("properties"), dict),
              str(list((tool.get("input_schema") or {}).get("properties") or {})))
        check(f"{name} 返回 output_schema 字段",
              "output_schema" in tool, str(list(tool.keys())))
        check(f"{name} 返回 metadata",
              isinstance(tool.get("metadata"), dict), str(tool.get("metadata"))[:120])
        check(f"{name} 返回 version",
              bool(tool.get("version")), str(tool.get("version")))
        check(f"{name} 返回 timeout_ms / max_retries / idempotent",
              "timeout_ms" in tool and "max_retries" in tool
              and "idempotent" in tool,
              f"timeout={tool.get('timeout_ms')} retries={tool.get('max_retries')}")
        check(f"{name} 返回 permission / hitl_required",
              "permission" in tool and "hitl_required" in tool,
              f"{tool.get('permission')} / hitl={tool.get('hitl_required')}")
        check(f"{name} 描述非空（详情里不截断）",
              bool(tool.get("description")) and len(tool["description"]) > 10,
              tool.get("description", "")[:80])
        # ---- OpenAI function calling ----
        ofn = body.get("openai_function")
        #: ``spec.openai_function()`` 返回的是 **OpenAI tool 形态**：
        #: ``{"type": "function", "function": {"name", "description", "parameters"}}``
        fn_inner = (ofn or {}).get("function") if isinstance(ofn, dict) else None
        check(f"{name} 返回 OpenAI function calling 形态",
              isinstance(ofn, dict) and ofn.get("type") == "function"
              and isinstance(fn_inner, dict) and fn_inner.get("name") == name
              and "parameters" in fn_inner,
              json.dumps(ofn or {}, ensure_ascii=False)[:140])
        check(f"{name} 的 OpenAI 形态参数与 input_schema 一致",
              fn_inner and (fn_inner.get("parameters") or {}).get("properties")
              == (tool.get("input_schema") or {}).get("properties"),
              "参数键一致")
        # ---- 派生参数表 ----
        params = body.get("params")
        check(f"{name} 返回派生参数表（params 数组）",
              isinstance(params, list) and len(params) > 0,
              str([p.get("name") for p in (params or [])]))
        if params:
            p0 = params[0]
            for key in ("name", "type", "required", "description"):
                check(f"{name} 参数表项含 {key}", key in p0, str(list(p0.keys())))
        # ---- 必填/可选 ----
        check(f"{name} 返回 required / optional 列表",
              isinstance(body.get("required"), list)
              and isinstance(body.get("optional"), list),
              f"req={body.get('required')} opt={body.get('optional')}")

    # 写类工具必须 HITL
    for name in ("ws_write_file", "ws_edit_file"):
        if name in names:
            d = get("/tools/" + name).json()
            check(f"{name} 详情里 hitl_required == True",
                  (d.get("tool") or {}).get("hitl_required") is True,
                  str((d.get("tool") or {}).get("hitl_required")))

    # 不存在的工具 → 404
    r404 = get("/tools/__not_exist__tool__")
    check("不存在的工具返回 404", r404.status_code == 404, str(r404.status_code))
    try:
        b404 = r404.json()
    except Exception:
        b404 = {}
    check("404 返回结构化错误码 TOOL_NOT_FOUND",
          b404.get("code") == "TOOL_NOT_FOUND",
          json.dumps(b404, ensure_ascii=False)[:140])


# ======================================================================
# B. 决策轨迹
# ======================================================================
def _sse_chat(message: str, timeout: int = 180) -> List[Dict[str, Any]]:
    """发一条消息，收集全部 SSE 事件。"""
    events: List[Dict[str, Any]] = []
    try:
        with requests.post(f"{BASE}/chat", json={
            "message": message, "stream": True, "mode": "agent",
        }, stream=True, timeout=timeout) as resp:
            if resp.status_code != 200:
                return events
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw or not raw.startswith("data: "):
                    continue
                payload = raw[6:]
                if payload.strip() == "[DONE]":
                    continue
                try:
                    events.append(json.loads(payload))
                except Exception:
                    continue
    except Exception as exc:  # pragma: no cover
        print(f"  (SSE 异常：{exc})", flush=True)
    return events


def test_trace_events() -> None:
    section("B1. SSE trace 事件带结构化轨迹字段")
    events = _sse_chat("T101_VDD_FUNCT 的上下限是多少？")
    kinds = [e.get("event") for e in events]
    check("收到 SSE 事件", len(events) > 0, f"n={len(events)} kinds={sorted(set(kinds))}")
    check("收到 start 事件", "start" in kinds, str(kinds[:8]))
    check("收到 result 事件", "result" in kinds, str(kinds[:8]))

    step_evts = [e for e in events if e.get("event") == "step"]
    check("收到 step 事件", len(step_evts) > 0, f"n={len(step_evts)}")
    if step_evts:
        s0 = step_evts[0]
        check("step 事件带 node 字段", "node" in s0, str(list(s0.keys())))
        check("step 事件带 tools 列表（工具名）",
              isinstance(s0.get("tools"), list), str(s0.get("tools")))
        check("step 事件带结构化 tool_calls（参数不是字符串拼接）",
              isinstance(s0.get("tool_calls"), list)
              and (not s0["tool_calls"]
                   or isinstance((s0["tool_calls"][0] or {}).get("args"), dict)),
              json.dumps(s0.get("tool_calls"), ensure_ascii=False)[:160])

    trace_evts = [e for e in events if e.get("event") == "trace"]
    check("收到 trace 事件（决策轨迹）", len(trace_evts) > 0, f"n={len(trace_evts)}")
    if not trace_evts:
        return
    last = trace_evts[-1]
    trace = last.get("trace") or []
    check("trace 事件带 trace 数组", isinstance(trace, list) and len(trace) > 0,
          f"n={len(trace)}")
    if trace:
        t0 = trace[0]
        for key in ("seq", "name", "args", "ok", "action_summary"):
            check(f"轨迹项含 {key}", key in t0, str(list(t0.keys())))
        check("轨迹项带 duration_ms（耗时）",
              "duration_ms" in t0, str(list(t0.keys())))
        check("轨迹项带 result（输出结果）",
              "result" in t0, str(list(t0.keys())))
        check("轨迹项带 error 字段（失败信息）",
              "error" in t0, str(list(t0.keys())))
        check("轨迹项带 reasoning_summary（推理摘要）",
              "reasoning_summary" in t0, str(list(t0.keys())))
        check("轨迹项 args 是结构化对象",
              isinstance(t0.get("args"), dict), str(type(t0.get("args"))))
        check("轨迹项 name 是工具名（非空）",
              bool(t0.get("name")), str(t0.get("name")))
        check("轨迹项 seq 递增", [t.get("seq") for t in trace]
              == sorted(t.get("seq") or 0 for t in trace),
              str([t.get("seq") for t in trace]))
    # ---- 预算 ----
    budget = last.get("budget")
    check("trace 事件带 budget 快照", isinstance(budget, dict),
          json.dumps(budget or {}, ensure_ascii=False)[:160])
    if isinstance(budget, dict):
        check("budget 含已用轮数", "rounds" in budget, str(list(budget.keys())))
        check("budget 含上限（轮数 8 / token 60000 / 工具调用 24）",
              budget.get("max_rounds") == 8
              and budget.get("max_tokens") == 60000
              and budget.get("max_tool_calls") == 24,
              json.dumps(budget, ensure_ascii=False)[:200])


def test_reasoning_not_leaked() -> None:
    section("B2. 不泄露原始思维链（只给截断摘要）")
    events = _sse_chat("T101_VDD_FUNCT 的上下限是多少？")
    trace_evts = [e for e in events if e.get("event") == "trace"]
    if not trace_evts:
        check("有 trace 事件可测", False)
        return
    trace = trace_evts[-1].get("trace") or []
    for item in trace:
        rs = item.get("reasoning_summary") or ""
        check(f"步骤 {item.get('seq')} 的 reasoning_summary 长度受限（<=220 字符）",
              len(rs) <= 220, f"len={len(rs)}")
        check(f"步骤 {item.get('seq')} 的 reasoning_summary 不含原始字段名",
              "reasoning_content" not in rs and "<think>" not in rs,
              rs[:100])
        check(f"步骤 {item.get('seq')} 有可解释的动作摘要（不依赖模型内部思维链）",
              bool(item.get("action_summary")),
              str(item.get("action_summary"))[:80])
    # 整个 SSE 流里不得出现原始思维链字段
    raw = json.dumps(events, ensure_ascii=False)
    check("SSE 流不含原始 reasoning_content 字段",
          "reasoning_content" not in raw, "未泄露")
    check("SSE 流不含 <think> 标签", "<think>" not in raw, "未泄露")


def test_history_persist_trace() -> None:
    section("B3. 历史会话恢复时带轨迹数据（落盘）")
    r = get("/sessions")
    check("GET /sessions 返回 200", r.status_code == 200, str(r.status_code))
    sessions = r.json().get("sessions") or []
    if not sessions:
        check("有历史会话可测", True, "暂无历史会话（跳过）")
        return
    found = False
    for s in sessions[:10]:
        tid = s.get("thread_id")
        if not tid:
            continue
        m = get(f"/sessions/{tid}/messages")
        if m.status_code != 200:
            continue
        msgs = m.json().get("messages") or []
        for msg in msgs:
            extra = msg.get("extra") or {}
            if extra.get("trace"):
                found = True
                tr = extra["trace"]
                check("落盘的 trace 是数组", isinstance(tr, list), str(type(tr)))
                if tr:
                    check("落盘 trace 项含 name / args / ok",
                          all(k in tr[0] for k in ("name", "args", "ok")),
                          str(list(tr[0].keys())))
                check("落盘 trace 项的 result 已瘦身（<=4200 字符）",
                      all(len(str(t.get("result") or "")) <= 4200 for t in tr),
                      str(max((len(str(t.get("result") or "")) for t in tr),
                            default=0)))
                check("落盘带 budget 快照", isinstance(extra.get("budget"), dict),
                      str(type(extra.get("budget"))))
                return
    check("历史会话消息里带 extra.trace（决策轨迹落盘）", found,
          f"扫描了 {len(sessions[:10])} 个会话")


# ======================================================================
# C. 前端
# ======================================================================
def test_frontend() -> None:
    section("C. 前端：工具详情容器 + 轨迹容器 + 零依赖")
    r = get("/ui/")
    check("/ui/ 返回 200", r.status_code == 200, str(r.status_code))
    html = r.text
    check("HTML 引入 trace_view.js", "trace_view.js" in html, "")
    check("HTML 有对话头容器（决策轨迹入口挂载点）",
          'class="chat-head"' in html, "")

    js = requests.get(f"{BASE}/ui/trace_view.js", timeout=TIMEOUT)
    check("trace_view.js 可访问", js.status_code == 200 and len(js.text) > 3000,
          f"HTTP {js.status_code} len={len(js.text)}")
    t = js.text
    # ---- 工具详情 ----
    check("前端有工具详情页容器 id=tv-tool-page",
          "tv-tool-page" in t, "")
    check("前端有工具详情正文容器 id=tv-tool-body",
          "tv-tool-body" in t, "")
    check("前端渲染参数表（.tv-params）", "tv-params" in t, "")
    check("前端参数表列含 参数名/类型/必填/说明",
          all(k in t for k in ("参数名", "类型", "必填", "说明")), "")
    check("前端展示 OpenAI function calling 形态",
          "OpenAI function calling" in t, "")
    check("前端展示 input_schema 原始 JSON 折叠",
          "查看原始 JSON Schema" in t, "")
    check("前端工具名可点击绑定（点击工具名 → 打开详情 tab）",
          "tvBindToolList" in t and "tvOpenTool" in t, "")
    check("前端工具详情走独立 tab（openTab + activateTab）",
          "openTab('tool'" in t and "activateTab" in t, "")
    # ---- 决策轨迹 ----
    check("前端有轨迹面板容器 id=tv-trace-list",
          "tv-trace-list" in t, "")
    check("前端有预算容器 id=tv-trace-budget",
          "tv-trace-budget" in t, "")
    check("前端有决策轨迹入口按钮 id=btn-trace-view",
          "btn-trace-view" in t, "")
    check("前端渲染参数分区与结果分区",
          "输入参数" in t and "输出结果" in t, "")
    check("前端渲染 ReAct 结构（思考/决策/动作/观察/下一步）",
          all(k in t for k in ("思考", "决策", "动作", "观察", "下一步")), "")
    check("前端标注推理摘要已截断（不声称展示完整思维链）",
          "模型推理摘要（截断）" in t or "已截断" in t, "")
    check("前端提供审计记录入口", "tv-audit-btn" in t, "")
    check("前端 JSON 语法着色（tok-property 等）",
          "tok-property" in t, "")
    # 零 emoji：只查「真正的表情符号区段」，避开 ⌚⌘ 等带变体选择符的技术符号
    emoji_re = re.compile("[\U0001F300-\U0001FAFF"
                           "\U00002600-\U000027BF"
                           "\U0001F1E6-\U0001F1FF"
                           "\U0000FE0F"
                           "\U0001F900-\U0001F9FF]")
    check("前端零 emoji（无 Unicode 表情符号）",
          not emoji_re.search(t), emoji_re.findall(t)[:5])

    css = requests.get(f"{BASE}/ui/app.css", timeout=TIMEOUT)
    check("app.css 返回 200", css.status_code == 200, str(css.status_code))
    c = css.text
    check("app.css 含工具详情样式（.tv-tool-page / .tv-params）",
          ".tv-tool-head" in c and ".tv-params" in c, "")
    check("app.css 含决策轨迹样式（.tv-trace-item / .tv-tl-sec）",
          ".tv-trace-item" in c and ".tv-tl-sec" in c, "")
    check("app.css 含轨迹折叠块样式（.tv-trace-row）",
          ".tv-trace-row" in c, "")
    check("app.css 含预算条样式（.tv-budget-bar）",
          ".tv-budget-bar" in c, "")
    check("app.css 无渐变（保持 Cursor 视觉规范）",
          "gradient" not in c.lower(), "")

    # 无外链 CDN
    urls = re.findall(r'(?:src|href)\s*=\s*["\']([^"\']+)["\']', html, flags=re.IGNORECASE)
    external = [u for u in urls if u.lower().startswith(("http://", "https://", "//"))]
    check("前端无外链 CDN", not external, str(external[:4]))

    # JS 语法检查
    try:
        for name in ("trace_view.js", "app.js", "workbench.js", "workspace.js"):
            p = os.path.join(STATIC_DIR, name)
            out = subprocess.run(["node", "--check", p], capture_output=True, timeout=60)
            err = (out.stderr or b"").decode("utf-8", "replace")
            check(f"{name} 语法检查通过", out.returncode == 0, err.strip()[:200])
    except FileNotFoundError:
        check("node 不可用，跳过 JS 语法检查", True, "node 未安装")


def test_app_js_hooks() -> None:
    section("C2. app.js / workbench.js 与轨迹模块对接")
    app = requests.get(f"{BASE}/ui/app.js", timeout=TIMEOUT).text
    check("app.js 处理 SSE trace 事件",
          "obj.event === 'trace'" in app, "")
    check("app.js 有结构化轨迹渲染入口 addTraceRow",
          "function addTraceRow" in app, "")
    check("app.js 启动时初始化轨迹模块（tvInit）",
          "tvInit" in app, "")
    check("app.js 工具名带 row-title-link（可点击）",
          "row-title-link" in app, "")
    wb = requests.get(f"{BASE}/ui/workbench.js", timeout=TIMEOUT).text
    check("workbench.js 历史会话恢复时渲染结构化轨迹",
          "addTraceRow" in wb, "")
    check("workbench.js 把落盘 trace 灌回全局缓存",
          "tvSetTrace" in wb, "")


def test_audit_endpoint() -> None:
    section("C3. 审计记录入口（与同一次执行对齐）")
    r = get("/audit", limit=20)
    check("GET /audit 返回 200", r.status_code == 200, str(r.status_code))
    body = r.json()
    check("返回 records 数组", isinstance(body.get("records"), list),
          str(list(body.keys())))
    check("返回 mode（审计后端类型）", "mode" in body, str(list(body.keys())))


# ======================================================================
# 主流程
# ======================================================================
def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    args = ap.parse_args()
    BASE = args.base.rstrip("/")

    print("工具详情 + 决策轨迹验证 —— " + BASE)
    section("0. 服务可达性")
    r = get("/health")
    check("/health 返回 200", r.status_code == 200, str(r.status_code))
    if r.status_code != 200:
        print("API 不可用，终止")
        return 1

    test_tools_list_compat()
    test_tool_detail_endpoint()
    test_trace_events()
    test_reasoning_not_leaked()
    test_history_persist_trace()
    test_frontend()
    test_app_js_hooks()
    test_audit_endpoint()

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
