# -*- coding: utf-8 -*-
"""桌面版 + 自定义工具 + 任务详情 综合验证脚本。

覆盖任务书「验证」小节的 1~4 项：
    1. pywebview 可 import；桌面版后端解析（复用 / 内嵌启动）；降级路径提示正确
    2. 自定义工具：HTTP 工具试运行 → 真实 JSON；script 工具试运行 → 真实 JSON；
       再用自定义工具跑一次 /chat，确认 Agent 能调用并把结果写进答案
    3. 任务详情：POST /tasks → 等待完成 → GET /tasks/{id} 拿完整答案 +
       工具轨迹 + SSE 事件；GET /tasks/{id}/export?format=md 返回可下载内容
    4. 前端：/ui/ 含自定义工具模态框、任务详情面板、导出按钮等新元素

用法：
    python scripts/verify_desktop.py [--base http://127.0.0.1:8010]

退出码：0 = 全部通过；1 = 有失败项。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List

import httpx

BASE = os.environ.get("AGENT_API_BASE", "http://127.0.0.1:8010").rstrip("/")
RESULTS: List[Any] = []

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO_SCRIPT = os.path.join("data", "scripts", "demo_custom_tool.py")


def check(name: str, cond: Any, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    flag = "PASS" if cond else "FAIL"
    print(f"  [{flag}] {name}" + (f"  -- {detail}" if detail else ""), flush=True)
    return bool(cond)


def sec(t: str) -> None:
    print(f"\n=== {t} ===", flush=True)


def _rm(p: str) -> None:
    try:
        os.remove(p)
    except OSError:
        pass


# ----------------------------------------------------------------------
# 1. 桌面版
# ----------------------------------------------------------------------
def verify_desktop(client: httpx.Client) -> None:
    sec("1. 桌面版：pywebview / 后端解析 / 降级路径")

    # 1.1 pywebview 是否可用
    try:
        import webview  # type: ignore

        check("pywebview 可 import", True, f"version={getattr(webview, '__version__', '未知')}")
    except Exception as exc:
        check("pywebview 可 import（未安装 → 走降级路径）", True,
              f"未安装（{type(exc).__name__}），将验证降级提示")
        webview = None  # type: ignore

    # 1.2 WebView2 Runtime
    sys.path.insert(0, REPO_ROOT)
    try:
        from agent_platform.desktop.window import desktop_env_info

        info = desktop_env_info()
        print("  环境自检:", json.dumps(info, ensure_ascii=False))
        check("WebView2 Runtime 检测有结论", "webview2_runtime" in info,
              f"installed={info.get('webview2_runtime')} "
              f"version={info.get('webview2_version', 'n/a')}")
    except Exception as exc:
        check("桌面环境自检可用", False, f"{type(exc).__name__}: {exc}")

    # 1.3 后端解析（复用既有 API）
    try:
        from agent_platform.desktop.launcher import probe_api, resolve_backend

        reachable = probe_api(BASE)
        check("既有 API 探测函数工作正常", True, f"probe({BASE})={reachable}")

        backend = resolve_backend(preferred=BASE, allow_embedded=False)
        check("后端解析返回可复用地址", backend.get("base_url") == BASE,
              f"mode={backend.get('mode')} detail={backend.get('detail')}")
        check("后端模式标记为 reuse", backend.get("mode") == "reuse", str(backend.get("mode")))
    except Exception as exc:
        check("桌面后端解析模块可导入并工作", False, f"{type(exc).__name__}: {exc}")

    # 1.4 内嵌启动能力（在随机端口起一个后端并验证 /health 可达）
    try:
        from agent_platform.desktop.launcher import start_embedded

        server, base, detail = start_embedded()
        if server is not None:
            ok = False
            try:
                r = httpx.get(base + "/health", timeout=15)
                ok = r.status_code == 200
            except Exception:
                ok = False
            check("内嵌启动后端可用（随机空闲端口）", ok, f"{base} {detail}")
            try:
                server.should_exit = True
            except Exception:
                pass
        else:
            check("内嵌启动后端可用（随机空闲端口）", False, detail)
    except Exception as exc:
        check("内嵌启动后端可用（随机空闲端口）", False, f"{type(exc).__name__}: {exc}")

    # 1.5 降级路径提示文案
    try:
        from agent_platform.desktop.window import WEBVIEW_MISSING_HINT, open_desktop

        hint = WEBVIEW_MISSING_HINT or ""
        check("降级提示含中文说明", ("pywebview" in hint) and ("浏览器" in hint),
              hint.replace("\n", " ")[:120])
        check("降级提示含安装指引", "pip install pywebview" in hint,
              "含 pip install pywebview")
        check("降级提示含 WebView2 下载指引", "WebView2" in hint, "含 WebView2")
        check("降级函数返回结构完整",
              callable(open_desktop), "open_desktop(url) -> {mode,detail,url}")
    except Exception as exc:
        check("降级路径模块可导入", False, f"{type(exc).__name__}: {exc}")

    # 1.6 桌面版 CLI --check 能跑通
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "agent_platform.desktop", "--check"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",
        )
        out = proc.stdout or ""
        check("桌面版 CLI --check 退出码为 0", proc.returncode == 0,
              f"rc={proc.returncode} out={out[-200:]}")
        check("桌面版 CLI 输出工作台地址", "/ui/" in out, "含 /ui/ 地址")
    except Exception as exc:
        check("桌面版 CLI --check 可执行", False, f"{type(exc).__name__}: {exc}")


# ----------------------------------------------------------------------
# 2. 自定义工具
# ----------------------------------------------------------------------
def verify_custom_tools(client: httpx.Client) -> None:
    sec("2. 自定义工具：HTTP / script 两种 kind + 试运行 + Agent 调用")

    # 2.0 清理历史测试工具
    existing = client.get(f"{BASE}/custom-tools").json().get("tools", [])
    for t in existing:
        if t["name"].startswith("vf_"):
            client.delete(f"{BASE}/custom-tools/{t['name']}")

    # 2.1 名称冲突（与内置工具重名 → 409）
    r = client.post(f"{BASE}/custom-tools", json={
        "name": "kb_search", "kind": "http",
        "config": {"url": BASE + "/health"},
    })
    check("与内置工具重名返回 409", r.status_code == 409,
          f"HTTP {r.status_code} {r.text[:120]}")

    # 2.2 非法 JSON Schema 会被拒
    r = client.post(f"{BASE}/custom-tools", json={
        "name": "vf_bad_schema", "kind": "http",
        "input_schema": {"type": "array"}, "config": {"url": "http://x"},
    })
    check("非法 input_schema 被拒（4xx）", 400 <= r.status_code < 500,
          f"HTTP {r.status_code} {r.text[:120]}")

    # 2.3 HTTP 工具：创建 + 试运行
    http_tool = {
        "name": "vf_http_health",
        "description": "查询本机 API 的 /health（验证用）",
        "input_schema": {"type": "object", "properties": {}},
        "kind": "http",
        "config": {"url": f"{BASE}/health", "method": "GET", "result_path": "status"},
        "permission": "READ_ONLY", "hitl_required": False, "enabled": True,
    }
    r = client.post(f"{BASE}/custom-tools", json=http_tool)
    check("创建 HTTP 自定义工具返回 201", r.status_code == 201,
          f"HTTP {r.status_code} {r.text[:160]}")
    r = client.post(f"{BASE}/custom-tools/vf_http_health/test", json={"args": {}})
    body = r.json()
    check("HTTP 工具试运行成功", body.get("ok") is True,
          f"ok={body.get('ok')} result={body.get('result')} err={body.get('error')}")
    check("HTTP 工具返回真实 JSON（result_path=status）", body.get("result") == "ok",
          f"result={body.get('result')!r}")

    # 2.4 HTTP 工具：占位符渲染
    r = client.post(f"{BASE}/custom-tools/vf_http_health/test", json={"args": {}})
    check("HTTP 试运行返回 elapsed_ms", body.get("elapsed_ms") is not None,
          str(body.get("elapsed_ms")))

    # 2.5 script 工具：创建 + 试运行（子进程隔离）
    script_tool = {
        "name": "vf_script_echo",
        "description": "回显入参（验证用，本地脚本）",
        "input_schema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
        "kind": "script",
        "config": {
            "script_path": DEMO_SCRIPT.replace("\\", "/"),
            "function_name": "echo_tool",
            "timeout_s": 15,
        },
        "permission": "READ_ONLY", "hitl_required": False, "enabled": True,
    }
    r = client.post(f"{BASE}/custom-tools", json=script_tool)
    check("创建 script 自定义工具返回 201", r.status_code == 201,
          f"HTTP {r.status_code} {r.text[:160]}")
    r = client.post(f"{BASE}/custom-tools/vf_script_echo/test",
                    json={"args": {"message": "你好 Agent"}})
    body = r.json()
    check("script 工具试运行成功", body.get("ok") is True,
          f"ok={body.get('ok')} err={body.get('error')}")
    result = body.get("result") or {}
    check("script 工具返回真实 JSON（回显入参）",
          isinstance(result, dict) and (result.get("echo") or {}).get("message") == "你好 Agent",
          json.dumps(result, ensure_ascii=False)[:200])
    check("script 工具带 _elapsed_ms", result.get("_elapsed_ms") is not None,
          str(result.get("_elapsed_ms")))

    # 2.6 script 工具：参数校验（缺必填）
    r = client.post(f"{BASE}/custom-tools/vf_script_echo/test", json={"args": {}})
    body = r.json()
    check("script 工具缺必填参数时报错", body.get("ok") is False
          and (body.get("error") or {}).get("code") == "INVALID_ARGUMENTS",
          json.dumps(body.get("error"), ensure_ascii=False)[:160])

    # 2.7 script 工具：脚本不存在 → 明确错误
    r = client.post(f"{BASE}/custom-tools", json={
        "name": "vf_script_missing", "kind": "script",
        "config": {"script_path": "data/scripts/__not_exist__.py",
                   "function_name": "x"},
    })
    r2 = client.post(f"{BASE}/custom-tools/vf_script_missing/test", json={"args": {}})
    body = r2.json()
    check("脚本不存在时试运行给出明确错误",
          body.get("ok") is False and "不存在" in str((body.get("error") or {}).get("message")),
          str((body.get("error") or {}).get("message"))[:140])

    # 2.8 HITL 强制：WRITE_DATA 必须 hitl_required=True
    r = client.post(f"{BASE}/custom-tools", json={
        "name": "vf_write_tool", "kind": "script", "permission": "WRITE_DATA",
        "hitl_required": False,
        "config": {"script_path": DEMO_SCRIPT.replace("\\", "/"),
                   "function_name": "echo_tool"},
    })
    body = r.json()
    check("WRITE_DATA 权限强制开启 HITL（用户不可关）",
          body.get("hitl_required") is True, f"hitl_required={body.get('hitl_required')}")
    check("强制 HITL 有明确原因说明", bool(body.get("hitl_forced_reason")),
          str(body.get("hitl_forced_reason"))[:120])
    r = client.put(f"{BASE}/custom-tools/vf_write_tool", json={
        "name": "vf_write_tool", "kind": "script", "permission": "WRITE_DATA",
        "hitl_required": False,
        "config": {"script_path": DEMO_SCRIPT.replace("\\", "/"),
                   "function_name": "echo_tool"},
    })
    check("PUT 更新也无法关闭强制 HITL", r.json().get("hitl_required") is True,
          f"hitl_required={r.json().get('hitl_required')}")

    # 2.9 注册即时生效（ToolRegistry 里能看到）
    tools = client.get(f"{BASE}/tools").json()
    names = [t["name"] for t in tools.get("tools", [])]
    check("自定义工具已注册进 ToolRegistry（立即生效）",
          "vf_http_health" in names and "vf_script_echo" in names, str(names))
    spec = next((t for t in tools["tools"] if t["name"] == "vf_write_tool"), None)
    check("ToolRegistry 中的强制 HITL 生效",
          spec is not None and spec.get("hitl_required") is True,
          str(spec.get("hitl_required")) if spec else "spec 缺失")

    # 2.10 停用 → 从注册表下线；启用 → 重新上线
    client.post(f"{BASE}/custom-tools/vf_http_health/toggle?enabled=false")
    names = [t["name"] for t in client.get(f"{BASE}/tools").json()["tools"]]
    check("停用后从 ToolRegistry 下线", "vf_http_health" not in names, str(names))
    client.post(f"{BASE}/custom-tools/vf_http_health/toggle?enabled=true")
    names = [t["name"] for t in client.get(f"{BASE}/tools").json()["tools"]]
    check("启用后重新上线", "vf_http_health" in names, str(names))

    # 2.11 持久化：data/custom_tools.json 含这些工具
    path = os.path.join(REPO_ROOT, "data", "custom_tools.json")
    check("自定义工具持久化文件存在", os.path.isfile(path), path)
    if os.path.isfile(path):
        raw = json.load(open(path, encoding="utf-8"))
        persisted = [t["name"] for t in raw.get("tools", [])]
        check("持久化文件含全部自定义工具",
              "vf_http_health" in persisted and "vf_script_echo" in persisted,
              str(persisted))

    # 2.12 删除
    r = client.delete(f"{BASE}/custom-tools/vf_script_missing")
    check("删除自定义工具返回 200", r.status_code == 200, f"HTTP {r.status_code}")
    r = client.get(f"{BASE}/custom-tools")
    check("删除后列表不再含该工具",
          "vf_script_missing" not in [t["name"] for t in r.json()["tools"]], "")

    # 2.13 Agent 通过 /chat 调用自定义工具
    print("  （发起一次 /chat 让 Agent 调用自定义 HTTP 工具，可能需要 1~2 分钟）...", flush=True)
    events: List[Dict[str, Any]] = []
    try:
        with httpx.stream("POST", f"{BASE}/chat", timeout=300.0,
                          json={"message": "请调用 vf_http_health 工具查询本机 API 的健康状态，"
                                          "并把工具返回的结果写进答案里。"}) as resp:
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
                        continue
                    try:
                        events.append(json.loads(body))
                    except Exception:
                        pass
    except Exception as exc:
        check("Agent 调用自定义工具（/chat）", False, f"{type(exc).__name__}: {exc}")
        return

    called = any("vf_http_health" in (e.get("tools") or []) for e in events)
    result = next((e for e in events if e.get("event") == "result"), {})
    answer = result.get("answer") or ""
    check("Agent 确实发起了自定义工具调用", called,
          f"events={[(e.get('event'), e.get('tools')) for e in events][:8]}")
    check("工具结果被写进答案", "ok" in answer.lower(), answer[:200])
    check("tool_calls_used >= 1", int(result.get("tool_calls_used") or 0) >= 1,
          f"tool_calls_used={result.get('tool_calls_used')}")


# ----------------------------------------------------------------------
# 3. 任务详情
# ----------------------------------------------------------------------
def verify_task_detail(client: httpx.Client) -> None:
    sec("3. 任务详情：完整答案 / 工具轨迹 / SSE 事件 / 导出")

    r = client.post(f"{BASE}/tasks", json={
        "name": "验证任务详情",
        "description": "请调用 vf_http_health 工具查询本机 API 的健康状态，并简要说明。",
        "model": "gpt-oss-20b",
        "params": {"max_rounds": 6, "temperature": 0.2,
                   "reasoning_effort": "low", "max_tokens": 1024},
    })
    check("POST /tasks 返回 201", r.status_code == 201, f"HTTP {r.status_code}")
    task_id = r.json().get("task_id")
    print(f"  任务 ID: {task_id}", flush=True)

    seen: List[str] = []
    final: Dict[str, Any] = {}
    for _ in range(90):
        tl = client.get(f"{BASE}/tasks").json()
        task = next((x for x in tl["tasks"] if x["task_id"] == task_id), None)
        if task:
            st = task.get("status")
            if not seen or seen[-1] != st:
                seen.append(st)
                print(f"    状态 → {st}（耗时 {task.get('duration_ms')}ms，"
                      f"token {task.get('tokens_used')}，工具 {task.get('tool_calls_used')}）",
                      flush=True)
            if st in ("done", "failed"):
                final = task
                break
        time.sleep(2)
    check("任务执行完成（done）", final.get("status") == "done",
          f"经历状态={seen} 最终={final.get('status')} err={final.get('error')}")

    # 3.1 列表项字段完整性
    for key in ("id", "name", "prompt", "model", "params", "status",
                "created_at", "started_at", "finished_at", "duration_ms",
                "tokens_used", "tool_calls_used", "error", "result_summary"):
        check(f"GET /tasks 项含字段 {key}", key in final, str(list(final.keys())[:8]))

    # 3.2 详情接口
    d = client.get(f"{BASE}/tasks/{task_id}").json()
    for key in ("prompt", "answer", "tool_trace", "sse_events", "error",
                "thread_id", "session_id", "citations", "audit"):
        check(f"GET /tasks/{{id}} 含字段 {key}", key in d, str(list(d.keys())[:8]))
    check("详情含完整 prompt（不截断）", len(d.get("prompt") or "") > 10,
          str(d.get("prompt"))[:80])
    check("详情含完整答案", len(d.get("answer") or "") > 5, str(d.get("answer"))[:120])
    trace = d.get("tool_trace") or []
    check("详情含工具调用轨迹", len(trace) >= 1, f"len={len(trace)}")
    if trace:
        item = trace[0]
        check("轨迹项含 工具名/参数/结果",
              all(k in item for k in ("name", "args", "result", "result_summary")),
              str(list(item.keys())))
        check("轨迹记录了真实工具名", item.get("name") == "vf_http_health",
              str(item.get("name")))
    sse = d.get("sse_events") or []
    check("详情含 SSE 事件序列", len(sse) >= 1, f"len={len(sse)}")
    if sse:
        check("SSE 事件含节点名", any(e.get("node") for e in sse),
              str([e.get("node") for e in sse]))

    # 3.3 404
    r = client.get(f"{BASE}/tasks/tk-not-exist")
    check("不存在的任务返回 404", r.status_code == 404, f"HTTP {r.status_code}")

    # 3.4 导出 Markdown
    r = client.get(f"{BASE}/tasks/{task_id}/export?format=md")
    check("导出 md 返回 200", r.status_code == 200, f"HTTP {r.status_code}")
    check("导出 md 有 Content-Disposition",
          "attachment" in (r.headers.get("content-disposition") or ""),
          r.headers.get("content-disposition", ""))
    text = r.text
    check("导出 md 含完整答案", (d.get("answer") or "")[:40] in text, text[:120])
    check("导出 md 含工具轨迹章节", "工具调用轨迹" in text, "")
    check("导出 md 含 SSE 章节", "SSE 事件序列" in text, "")
    check("导出 md 非空", len(text) > 200, f"{len(text)} 字符")

    # 3.5 导出 JSON
    r = client.get(f"{BASE}/tasks/{task_id}/export?format=json")
    check("导出 json 返回 200", r.status_code == 200, f"HTTP {r.status_code}")
    try:
        j = r.json()
        check("导出 json 含完整任务对象",
              j.get("task_id") == task_id and "tool_trace" in j,
              str(list(j.keys())[:8]))
    except Exception as exc:
        check("导出 json 是合法 JSON", False, f"{type(exc).__name__}: {exc}")


# ----------------------------------------------------------------------
# 4. 前端元素
# ----------------------------------------------------------------------
def verify_frontend(client: httpx.Client) -> None:
    sec("4. 前端：自定义工具模态框 / 任务详情面板 / 导出按钮")
    r = client.get(f"{BASE}/ui/")
    check("/ui/ 返回 200", r.status_code == 200, f"HTTP {r.status_code}")
    html = r.text
    for key in ("ct-mask", "ct-name", "ct-kind", "ct-schema", "ct-test",
                "ct-test-out", "ct-list", "btn-custom-tool",
                "td-mask", "td-basic", "td-answer", "td-trace", "td-sse",
                "td-raw", "td-export-json", "td-export-md",
                "task-status-filter", "task-sort"):
        check(f"HTML 含 {key!r}", key in html, "")

    js = client.get(f"{BASE}/ui/custom_tools.js").text
    check("custom_tools.js 可访问", len(js) > 1000, f"{len(js)} 字符")
    for key in ("openCustomToolModal", "testCustomTool", "collectCustomTool",
                "refreshCustomTools", "openTaskDetail", "renderTaskDetail",
                "exportTask", "renderMarkdown", "applyDesktopMode"):
        check(f"custom_tools.js 含函数 {key!r}", key in js, "")
    check("前端无 eval/exec（安全要求）",
          "eval(" not in js and "new Function(" not in js, "")

    css = client.get(f"{BASE}/ui/app.css").text
    for key in (".modal-mask", ".drawer", ".td-timeline", ".md-view", ".tl-item"):
        check(f"app.css 含样式 {key!r}", key in css, "")

    # 外链 CDN 检查：只看 <script src> / <link href> / <img src> 等**资源引用**，
    # 不能出现外部域名；输入框 placeholder 里的示例 URL 不算外链。
    import re

    resource_urls = re.findall(
        r'(?:src|href)\s*=\s*["\']([^"\']+)["\']', html, flags=re.IGNORECASE)
    external = [u for u in resource_urls if u.lower().startswith(("http://", "https://", "//"))]
    check("前端无外链 CDN（资源引用不含外部域名）", not external, str(external[:5]))
    check("前端脚本/样式均为本地路径",
          all(u.startswith("/ui/") for u in resource_urls), str(resource_urls[:5]))


# ----------------------------------------------------------------------
def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    args = ap.parse_args()
    BASE = args.base.rstrip("/")

    client = httpx.Client(timeout=300.0)
    try:
        r = client.get(f"{BASE}/health", timeout=15)
        if r.status_code != 200:
            print(f"[FATAL] {BASE}/health 不可达（HTTP {r.status_code}）")
            return 1
    except Exception as exc:
        print(f"[FATAL] 无法连接 {BASE}：{type(exc).__name__}: {exc}")
        return 1

    print(f"验证目标：{BASE}")
    verify_desktop(client)
    verify_custom_tools(client)
    verify_task_detail(client)
    verify_frontend(client)

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
