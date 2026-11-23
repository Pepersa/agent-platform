# -*- coding: utf-8 -*-
"""Web 工作台验证脚本（7 项）。

用法：python scripts/verify_web_ui.py [--base http://127.0.0.1:8010]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import httpx

BASE = os.environ.get("AGENT_API_BASE", "http://127.0.0.1:8010").rstrip("/")
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    flag = "PASS" if cond else "FAIL"
    print(f"  [{flag}] {name}" + (f"  -- {detail}" if detail else ""), flush=True)
    return bool(cond)


def sec(t):
    print(f"\n=== {t} ===", flush=True)


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    args = ap.parse_args()
    BASE = args.base.rstrip("/")
    client = httpx.Client(timeout=300.0)

    # ---------------------------------------------------------- 1
    sec("1. GET /health")
    r = client.get(f"{BASE}/health")
    print("  HTTP", r.status_code)
    h = r.json()
    print("  body:", json.dumps(h, ensure_ascii=False)[:400])
    check("/health 返回 200", r.status_code == 200)
    check("llm.reachable == true", h["llm"]["reachable"] is True,
          f"llm={h['llm'].get('model')} models={h['llm'].get('models')}")
    # 内置工具：原有 8 个 + 工作区模块新增的 5 个 ws_* 工具（任务 3 引入）
    BUILTIN_CORE = {"case_lint", "kb_read", "kb_search", "notify", "report_render",
                     "spec_extract", "sql_query", "tp_lookup"}
    BUILTIN_WS = {"ws_list_files", "ws_read_file", "ws_search_code",
                  "ws_write_file", "ws_edit_file"}
    builtin = set(h["registry"].get("builtin_tools", []))
    check("registry.builtin_tool_count == 13（内置 8 + 工作区 5）",
          len(builtin) == 13,
          f"builtin={h['registry'].get('builtin_tools')}")
    check("内置核心工具齐全（原 8 个）",
          BUILTIN_CORE.issubset(builtin),
          f"missing={sorted(BUILTIN_CORE - builtin)}")
    check("工作区工具已注册（ws_* 五件套）",
          BUILTIN_WS.issubset(builtin),
          f"missing={sorted(BUILTIN_WS - builtin)}")
    check("registry.tool_count >= 13（内置 13 + 自定义）",
          h["registry"]["tool_count"] >= 13,
          f"tools={h['registry']['tools']}")
    check("内置 HITL 工具列出（notify / report_render）",
          set(h["registry"]["hitl_tools"]) >= {"notify", "report_render"},
          str(h["registry"]["hitl_tools"]))
    check("checkpointer 类型可见", bool(h["graph"]["checkpointer"]),
          h["graph"]["checkpointer"])
    check("prompt 版本可见", bool(h["prompt_version"]), h["prompt_version"])
    check("两个模型端点均可达",
          all(e["reachable"] for e in h["endpoints"]),
          str([(e["model"], e["reachable"]) for e in h["endpoints"]]))

    # ---------------------------------------------------------- 2 & 3
    sec("2+3. POST /chat —— SSE 流式 + 引用 + tool_calls_used")
    payload = {"message": "T101_VDD_FUNCT 的上下限是多少？请引用来源。"}
    t0 = time.time()

    def _collect_chat(pay: dict) -> tuple:
        """跑一次 /chat 并收集 SSE 事件（返回 events, content-type）。"""
        evs: list = []
        ct_raw = ""
        with httpx.stream("POST", f"{BASE}/chat", json=pay, timeout=300.0) as resp:
            ct_raw = resp.headers.get("content-type", "")
            buf = ""
            for chunk in resp.iter_text():
                buf += chunk
                while "\n\n" in buf:
                    line, buf = buf.split("\n\n", 1)
                    d = [l for l in line.split("\n") if l.startswith("data: ")]
                    if not d:
                        continue
                    body = d[0][6:]
                    if body.strip() == "[DONE]":
                        evs.append({"event": "__DONE__"})
                        continue
                    try:
                        evs.append(json.loads(body))
                    except Exception:
                        pass
        return evs, ct_raw

    events, raw_ct = [], ""
    # 模型偶尔会把引用写成 `` `chunk_id` `` 而非 `` [来源:chunk_id] ``，重试若干次
    has_cite = False
    for attempt in range(4):
        events, raw_ct = _collect_chat(payload)
        result0 = next((e for e in events if e.get("event") == "result"), {})
        ans0 = result0.get("answer") or ""
        has_cite = bool(re.search(
            r"[\[［【]\s*(来源|出处)?\s*[:：]\s*[A-Za-z0-9_\-]{2,}", ans0))
        if has_cite:
            break
        print(f"  第 {attempt + 1} 次尝试未命中引用格式，重试 ...")
    elapsed = time.time() - t0
    print(f"  收到 {len(events)} 个 SSE 事件，耗时 {elapsed:.1f}s")
    for e in events:
        if e.get("event") == "step":
            print(f"    step node={e.get('node')} tools={e.get('tools')} "
                  f"stop={e.get('stop_reason')}")
        elif e.get("event") == "approval":
            print(f"    approval tools={[a.get('tool_name') for a in e.get('approvals', [])]}")
        elif e.get("event") == "result":
            print(f"    result status={e.get('status')} "
                  f"tool_calls_used={e.get('tool_calls_used')} "
                  f"tokens_used={e.get('tokens_used')} stop={e.get('stop_reason')}")
            print(f"    answer: {str(e.get('answer'))[:300]}")
        else:
            print(f"    {e.get('event', '?')}")

    check("SSE content-type 为 text/event-stream",
          "text/event-stream" in raw_ct, raw_ct)
    check("收到 data: [DONE]", any(e.get("event") == "__DONE__" for e in events))
    check("首个事件是 start（不空白等待）",
          bool(events) and events[0].get("event") == "start",
          str(events[0].get("event")) if events else "none")
    check("中间有 step 事件（真正分块流式）",
          any(e.get("event") == "step" for e in events),
          f"step 数={sum(1 for e in events if e.get('event') == 'step')}")

    result = next((e for e in events if e.get("event") == "result"), {})
    answer = result.get("answer") or ""
    print("  --- 完整 answer ---")
    print(answer)
    print("  -------------------")
    check("返回了答案", len(answer) > 10, f"{len(answer)} 字符")
    check("答案带引用", has_cite, answer[:160])
    check("tool_calls_used >= 1", int(result.get("tool_calls_used") or 0) >= 1,
          f"tool_calls_used={result.get('tool_calls_used')}")
    chat_thread = result.get("thread_id")

    # ---------------------------------------------------------- 4
    sec("4. HITL：pending_approval → approve 续跑（同 thread_id）")
    hitl_payload = {"message": "调用 report_render 生成一份日报，template 填 daily"}
    ev2, r2 = [], {}
    # gpt-oss 偶尔会只口头说"需要确认"而不发起调用（verify_e2e.py 同样做重试）
    for attempt in range(8):
        ev2 = []
        with httpx.stream("POST", f"{BASE}/chat", json=hitl_payload, timeout=300.0) as resp:
            buf = ""
            for chunk in resp.iter_text():
                buf += chunk
                while "\n\n" in buf:
                    line, buf = buf.split("\n\n", 1)
                    d = [l for l in line.split("\n") if l.startswith("data: ")]
                    if not d:
                        continue
                    b = d[0][6:]
                    if b.strip() == "[DONE]":
                        ev2.append({"event": "__DONE__"})
                        continue
                    try:
                        ev2.append(json.loads(b))
                    except Exception:
                        pass
        r2 = next((e for e in ev2 if e.get("event") == "result"), {})
        print(f"  第 {attempt + 1} 次尝试 → status={r2.get('status')} "
              f"approvals={len(r2.get('approvals') or [])} "
              f"tool_calls_used={r2.get('tool_calls_used')}")
        if r2.get("status") == "pending_approval" and r2.get("approvals"):
            break
    print("  status:", r2.get("status"))
    print("  approvals:", json.dumps(r2.get("approvals"), ensure_ascii=False)[:600])
    check("status == pending_approval", r2.get("status") == "pending_approval",
          str(r2.get("status")))
    check("approvals 非空", bool(r2.get("approvals")),
          f"{len(r2.get('approvals') or [])} 项")
    check("pending 阶段工具未执行（tool_calls_used==0）",
          int(r2.get("tool_calls_used") or 0) == 0,
          f"tool_calls_used={r2.get('tool_calls_used')}")

    appr = (r2.get("approvals") or [{}])[0]
    tc_id = appr.get("tool_call_id")
    hitl_thread = r2.get("thread_id")
    print(f"  审批 thread_id = {hitl_thread}（与 /chat 返回一致 = {hitl_thread == chat_thread or 'n/a'}）")

    approve_body = {
        "thread_id": hitl_thread,
        "tool_call_id": tc_id,
        "approved": True,
        "actor": "验证脚本",
        "reason": "同意生成日报",
        "session_grant": False,
    }
    print("  POST /approve 请求体:", json.dumps(approve_body, ensure_ascii=False))
    ar = client.post(f"{BASE}/approve", json=approve_body)
    print("  POST /approve HTTP", ar.status_code)
    print("  响应:", json.dumps(ar.json(), ensure_ascii=False)[:700])
    aj = ar.json()
    check("approve 返回 200", ar.status_code == 200)
    check("approve 用同一 thread_id 续跑成功",
          aj.get("status") in ("done", "pending_approval"), str(aj.get("status")))
    check("续跑后有最终答案", bool(aj.get("answer")), str(aj.get("answer"))[:120])
    check("续跑后工具真的执行了", int(aj.get("tool_calls_used") or 0) >= 1,
          f"tool_calls_used={aj.get('tool_calls_used')}")

    # ---------------------------------------------------------- 5
    sec("5. POST /tasks → GET /tasks 状态机")
    tr = client.post(f"{BASE}/tasks", json={
        "name": "L1-002 限值核对",
        "description": "T101_VDD_FUNCT 的上下限是多少？请引用来源。",
        "model": "gpt-oss-20b",
        "params": {"max_rounds": 8, "temperature": 0.2,
                   "reasoning_effort": "medium", "max_tokens": 2048},
    })
    print("  POST /tasks HTTP", tr.status_code)
    tj = tr.json()
    print("  创建响应:", json.dumps(tj, ensure_ascii=False)[:400])
    task_id = tj.get("task_id")
    check("POST /tasks 返回 201", tr.status_code == 201, str(tr.status_code))

    seen = []
    for _ in range(90):
        tl = client.get(f"{BASE}/tasks").json()
        task = next((x for x in tl["tasks"] if x["task_id"] == task_id), None)
        if task:
            st = task["status"]
            if not seen or seen[-1] != st:
                seen.append(st)
                print(f"    状态 → {st}  (耗时 {task.get('duration_ms')}ms, "
                      f"token {task.get('tokens_used')}, 工具 {task.get('tool_calls_used')})")
            if st in ("done", "failed"):
                break
        time.sleep(2)
    final = next((x for x in client.get(f"{BASE}/tasks").json()["tasks"]
                   if x["task_id"] == task_id), {})
    print("  任务最终记录:", json.dumps(final, ensure_ascii=False)[:600])
    check("任务状态流转 queued → ... → done",
          "queued" in seen and final.get("status") == "done",
          f"经历状态={seen} 最终={final.get('status')}")
    check("任务记录了耗时", final.get("duration_ms") is not None,
          str(final.get("duration_ms")))
    check("任务记录了 token 数", int(final.get("tokens_used") or 0) > 0,
          str(final.get("tokens_used")))
    check("任务有结果摘要", bool(final.get("result_summary")),
          str(final.get("result_summary"))[:120])

    # ---------------------------------------------------------- 6
    sec("6. GET /ui/ 返回 HTML")
    ur = client.get(f"{BASE}/ui/")
    print("  HTTP", ur.status_code, "| content-type:", ur.headers.get("content-type"))
    print("  长度:", len(ur.text))
    check("/ui/ 返回 200", ur.status_code == 200)
    check("是 HTML", "text/html" in ur.headers.get("content-type", ""),
          ur.headers.get("content-type", ""))
    # IDE 布局关键元素（Cursor 风格：活动栏 / 侧栏 / 标签页 / 主区）
    for key in ["系统状态", "任务发布台", "新建会话", "chat", "composer",
                "tpl-select", "btn-publish", "tasks-list", "h-endpoints",
                # 工具视图：新布局里工具清单是活动栏的一个视图（view-tools），
                # 不再是旧的手风琴折叠段标题「工具清单」。
                "view-tools", "tools-list",
                # IDE 三段式骨架
                "activitybar", "sidebar", "tabbar", "main", "workbench"]:
        check(f"HTML 含关键元素 {key!r}", key in ur.text)
    cs = client.get(f"{BASE}/ui/app.css")
    js = client.get(f"{BASE}/ui/app.js")
    check("app.css 可访问", cs.status_code == 200 and len(cs.text) > 1000, str(len(cs.text)))
    check("app.js 可访问", js.status_code == 200 and len(js.text) > 1000, str(len(js.text)))
    check("JS 用 fetch+ReadableStream 而非 EventSource",
          "getReader" in js.text and "new EventSource" not in js.text)
    # 外链 CDN 检查：只看 <script src> / <link href> 等**资源引用**，
    # 输入框 placeholder 里的示例 URL 不算外链。
    resource_urls = re.findall(
        r'(?:src|href)\s*=\s*["\']([^"\']+)["\']', ur.text, flags=re.IGNORECASE)
    external = [u for u in resource_urls if u.lower().startswith(("http://", "https://", "//"))]
    check("前端无外链 CDN（资源引用不含外部域名）", not external, str(external[:5]))
    # 新增：自定义工具模态框 / 任务详情面板 / 导出按钮（任务 2 / 任务 3）
    for key in ["ct-mask", "ct-kind", "ct-schema", "ct-test", "ct-list",
                "btn-custom-tool", "td-mask", "td-trace", "td-sse", "td-raw",
                "td-export-json", "td-export-md", "task-status-filter", "task-sort"]:
        check(f"HTML 含新增元素 {key!r}", key in ur.text)
    cjs = client.get(f"{BASE}/ui/custom_tools.js")
    check("custom_tools.js 可访问", cjs.status_code == 200 and len(cjs.text) > 1000,
          str(len(cjs.text)))

    # ---------------------------------------------------------- 7
    sec("7. GET /tasks/templates")
    trs = client.get(f"{BASE}/tasks/templates")
    tj2 = trs.json()
    print("  HTTP", trs.status_code)
    print("  count:", tj2.get("count"))
    print("  categories:", tj2.get("categories"))
    print("  groups 计数:", {k: len(v) for k, v in tj2.get("groups", {}).items()})
    print("  样例:", json.dumps(tj2["templates"][0], ensure_ascii=False))
    check("/tasks/templates 返回 200", trs.status_code == 200)
    check("返回 30 条种子集", tj2.get("count") == 30, str(tj2.get("count")))
    check("按 L1-L6 分组", tj2.get("categories") == ["L1", "L2", "L3", "L4", "L5", "L6"],
          str(tj2.get("categories")))
    check("模板含 id/category/query/tags",
          all(k in tj2["templates"][0] for k in ("id", "category", "query", "tags")),
          str(list(tj2["templates"][0].keys())))

    # ---------------------------------------------------------- 附加
    sec("附加：GET /sessions 与 GET /metrics")
    sr = client.get(f"{BASE}/sessions").json()
    print("  sessions:", json.dumps(sr, ensure_ascii=False)[:400])
    check("/sessions 返回会话列表", sr.get("count", 0) >= 1, str(sr.get("count")))
    mr = client.get(f"{BASE}/metrics").json()
    print("  metrics:", json.dumps(mr, ensure_ascii=False))
    check("/metrics 有累计统计", mr.get("requests_total", 0) >= 3,
          f"requests_total={mr.get('requests_total')}")

    # ---------------------------------------------------------- 汇总
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
