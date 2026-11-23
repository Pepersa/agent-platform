# -*- coding: utf-8 -*-
"""verify_modes.py —— 新功能回归：模型注册表 / 三种交互模式 / 会话消息持久化 / 会话摘要。

用法：
    python scripts/verify_modes.py            # 默认连 http://127.0.0.1:8010
    python scripts/verify_modes.py --base http://127.0.0.1:8010

覆盖四大块新功能：
    A. 模型注册表（GET/POST/PUT/DELETE /models，端点可达性探测）
    B. 交互模式（agent / ask / plan 的真实行为差异）
    C. 会话消息持久化（SQLite 落盘 + GET /sessions/{id}/messages）
    D. 会话摘要（POST /sessions/{id}/summarize 结构化摘要 + 持久化）
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

import requests

BASE = os.environ.get("AGENT_API_BASE", "http://127.0.0.1:8010").rstrip("/")
TIMEOUT = 300

PASS = 0
FAIL = 0
FAILURES: List[str] = []


def ok(name: str, detail: str = "") -> None:
    global PASS
    PASS += 1
    print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))


def bad(name: str, detail: str = "") -> None:
    global FAIL
    FAIL += 1
    FAILURES.append(f"{name}: {detail}")
    print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def check(cond: bool, name: str, detail: str = "") -> bool:
    if cond:
        ok(name, detail)
    else:
        bad(name, detail)
    return bool(cond)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------
def post(path: str, payload: Any, timeout: int = TIMEOUT) -> Any:
    r = requests.post(f"{BASE}{path}", json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def get(path: str, timeout: int = 60) -> Any:
    r = requests.get(f"{BASE}{path}", timeout=timeout)
    r.raise_for_status()
    return r.json()


def chat(message: str, *, mode: str = "agent", model: Optional[str] = None,
         thread_id: Optional[str] = None, stream: bool = False,
         timeout: int = TIMEOUT) -> Any:
    payload = {
        "message": message,
        "mode": mode,
        "stream": False,
    }
    if model:
        payload["model"] = model
    if thread_id:
        payload["thread_id"] = thread_id
    return post("/chat", payload, timeout=timeout)


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_path(*parts: str) -> str:
    """返回**数据目录**下的路径（可用 AGENT_DATA_DIR 整体外置）。

    不设 ``AGENT_DATA_DIR`` 时返回 ``<repo>/data/<parts>``（与改造前一致）。
    """
    env = os.environ.get("AGENT_DATA_DIR", "").strip()
    if env:
        base = os.path.abspath(os.path.expanduser(env))
    else:
        base = os.path.join(repo_root(), "data")
    return os.path.join(base, *parts)


# ======================================================================
# A. 模型注册表
# ======================================================================
def test_model_registry() -> None:
    section("A. 模型注册表（GET/POST/PUT/DELETE /models）")

    try:
        data = get("/models")
    except Exception as exc:  # pragma: no cover
        bad("GET /models 可用", str(exc))
        return

    check(data.get("count", 0) >= 2, "GET /models 返回至少 2 个内置模型",
          f"count={data.get('count')}")
    models = data.get("models") or []
    names = [m.get("name") for m in models]
    check("gpt-oss-20b" in names, "包含 gpt-oss-20b（:8080 GPU）", str(names))
    check("qwen2.5-14b" in names, "包含 qwen2.5-14b（:8081 CPU）", str(names))

    for m in models:
        check("reachable" in m, f"模型 {m.get('name')} 含可达性字段 reachable",
              f"reachable={m.get('reachable')}")
    # 至少一个真正探测成功
    reach = [m for m in models if m.get("reachable")]
    check(len(reach) >= 1, "至少一个端点探测可达（真实连通，非臆造）",
          f"reachable={[m['name'] for m in reach]}")

    # --- 新增自定义模型 ---
    cname = "verify-custom-" + uuid.uuid4().hex[:6]
    payload = {
        "name": cname,
        "base_url": "http://127.0.0.1:8080/v1",
        "model": "gpt-oss-20b",
        "description": "verify_modes 自动注册（指向本机 8080 的别名）",
        "reasoning_effort": "low",
        "temperature": 0.2,
        "max_tokens": 1024,
    }
    try:
        created = post("/models", payload)
        check(created.get("name") == cname, "POST /models 注册自定义模型",
              f"name={created.get('name')}")
    except Exception as exc:
        bad("POST /models 注册自定义模型", str(exc))
        return

    # 持久化到 data/models.json
    store_path = data.get("store")
    if store_path:
        p = store_path if os.path.isabs(store_path) else os.path.join(repo_root(), store_path)
        check(os.path.exists(p), "data/models.json 已落盘", p)
        try:
            with io.open(p, encoding="utf-8") as fh:
                raw = json.load(fh)
            custom = [m for m in raw.get("models", [])
                      if m.get("name") == cname]
            check(len(custom) == 1, "models.json 里确实持久化了该模型",
                  f"custom={len(custom)}")
        except Exception as exc:
            bad("models.json 解析", str(exc))

    # 重新 GET 应当出现
    data2 = get("/models")
    check(cname in [m.get("name") for m in (data2.get("models") or [])],
          "GET /models 能看到新增自定义模型")

    # --- PUT 更新 ---
    try:
        upd = requests.put(f"{BASE}/models/{cname}",
                           json={"description": "verify_modes 已更新",
                                 "reasoning_effort": "medium"},
                           timeout=60)
        upd.raise_for_status()
        rj = upd.json()
        check(rj.get("description") == "verify_modes 已更新",
              "PUT /models/{name} 更新描述", str(rj.get("description")))
    except Exception as exc:
        bad("PUT /models/{name}", str(exc))

    # --- PUT 内置模型应被拒（409） ---
    try:
        r = requests.put(f"{BASE}/models/gpt-oss-20b",
                         json={"description": "x"}, timeout=60)
        check(r.status_code in (400, 409), "PUT 内置模型被拒绝（保护内置定义）",
              f"status={r.status_code}")
    except Exception as exc:
        bad("PUT 内置模型被拒绝", str(exc))

    # --- DELETE ---
    try:
        r = requests.delete(f"{BASE}/models/{cname}", timeout=60)
        r.raise_for_status()
        check(r.json().get("deleted") == cname, "DELETE /models/{name} 删除成功")
    except Exception as exc:
        bad("DELETE /models/{name}", str(exc))

    data3 = get("/models")
    check(cname not in [m.get("name") for m in (data3.get("models") or [])],
          "删除后列表里不再出现")

    # --- DELETE 内置应被拒 ---
    try:
        r = requests.delete(f"{BASE}/models/gpt-oss-20b", timeout=60)
        check(r.status_code in (400, 409), "DELETE 内置模型被拒绝",
              f"status={r.status_code}")
    except Exception as exc:
        bad("DELETE 内置模型被拒绝", str(exc))

    # --- DELETE 不存在 ---
    try:
        r = requests.delete(f"{BASE}/models/no-such-model-xyz", timeout=60)
        check(r.status_code == 404, "DELETE 不存在的模型返回 404",
              f"status={r.status_code}")
    except Exception as exc:
        bad("DELETE 不存在的模型返回 404", str(exc))


# ======================================================================
# B. 交互模式
# ======================================================================
def test_modes() -> None:
    section("B. 交互模式（Agent / Ask / Plan）")

    # ---- Ask 模式：只读，不做写操作 ----
    try:
        r = chat("请用一句话回答：Python 的列表推导语法是什么？不要调用任何写操作工具。",
                 mode="ask", timeout=240)
        answer = (r.get("answer") or "")[:120]
        tool_calls = r.get("tool_calls") or r.get("tool_calls_used") or []
        if isinstance(tool_calls, int):
            n_tools = tool_calls
        else:
            n_tools = len(tool_calls)
        check(r.get("mode") == "ask", "Ask 模式响应回显 mode=ask",
              f"mode={r.get('mode')}")
        check(n_tools <= 2, "Ask 模式工具调用轮次 ≤ 2（不进入多轮 ReAct）",
              f"tools={n_tools}")
        check(bool(answer.strip()), "Ask 模式返回了答案", answer)
        check(r.get("status") in (None, "ok", "completed", "done"),
              "Ask 模式状态正常", f"status={r.get('status')}")
    except Exception as exc:
        bad("Ask 模式请求", str(exc))

    # ---- Ask 模式：请求写操作应被拒 ----
    try:
        r = chat("请把内容写入文件 /tmp/ask_write_probe.txt，内容为 hello",
                 mode="ask", timeout=240)
        used = r.get("tool_calls") or []
        names = []
        if isinstance(used, list):
            for tc in used:
                if isinstance(tc, dict):
                    names.append(tc.get("name") or tc.get("tool"))
        joined = " ".join(str(n) for n in names).lower()
        writeish = ("write" in joined or "create" in joined
                    or "edit" in joined or "delete" in joined)
        check(not writeish, "Ask 模式未调用任何写类工具",
              f"tools={names}")
    except Exception as exc:
        bad("Ask 模式写操作拦截", str(exc))

    # ---- Plan 模式：产出结构化计划，零工具调用 ----
    try:
        r = chat("请规划一个方案：用本地知识库检索一份关于向量数据库的文档并给出摘要。",
                 mode="plan", timeout=240)
        plan = r.get("plan")
        check(isinstance(plan, dict), "Plan 模式返回结构化 plan（对象）",
              f"type={type(plan).__name__}")
        if isinstance(plan, dict):
            steps = plan.get("steps") or []
            check(isinstance(steps, list) and len(steps) >= 1,
                  "plan.steps 是非空数组", f"n={len(steps) if isinstance(steps, list) else '?'}")
            if isinstance(steps, list) and steps:
                s0 = steps[0]
                check(isinstance(s0, dict), "每个 step 是对象")
                if isinstance(s0, dict):
                    check(bool(s0.get("title") or s0.get("description") or s0.get("name")),
                          "step 含 title/description 字段", str(list(s0.keys())))
                    check("tools" in s0 or "tool" in s0,
                          "step 含涉及工具字段（tools/tool）", str(list(s0.keys())))
                check(all(isinstance(s, dict) for s in steps), "所有 step 均为对象")
            check("risks" in plan or "risk_points" in plan or "open_questions" in plan,
                  "plan 含风险点 / 未解决问题字段", str(list(plan.keys())))
        used = r.get("tool_calls") or []
        n_tools = used if isinstance(used, int) else len(used)
        check(n_tools == 0, "Plan 模式零工具调用（只规划不执行）", f"tools={n_tools}")
    except Exception as exc:
        bad("Plan 模式请求", str(exc))

    # ---- Agent 模式（默认，向后兼容）----
    try:
        r = chat("请只回答：1+1 等于几？", mode="agent", timeout=240)
        check(bool((r.get("answer") or "").strip()), "Agent 模式返回答案",
              (r.get("answer") or "")[:80])
        check(r.get("mode") in (None, "agent"), "Agent 模式回显", f"mode={r.get('mode')}")
    except Exception as exc:
        bad("Agent 模式请求", str(exc))

    # ---- 向后兼容：不带 mode 仍是 agent ----
    try:
        r = requests.post(f"{BASE}/chat",
                          json={"message": "用一句话说明什么是 REST。",
                                "stream": False},
                          timeout=240)
        r.raise_for_status()
        j = r.json()
        check(bool((j.get("answer") or "").strip()),
              "老调用方（不带 mode）仍可用 —— 向后兼容",
              (j.get("answer") or "")[:60])
    except Exception as exc:
        bad("不带 mode 的向后兼容调用", str(exc))

    # ---- model 字段真正生效 ----
    try:
        r = chat("用一句话介绍你自己。", mode="agent", model="qwen2.5-14b",
                 timeout=240)
        used_model = r.get("model") or ""
        check(bool(used_model), "响应回显所选模型", f"model={used_model}")
        check("qwen" in used_model.lower(), "确实路由到 qwen2.5-14b",
              f"model={used_model}")
    except Exception as exc:
        bad("model 字段路由生效", str(exc))


# ======================================================================
# C. 会话消息持久化
# ======================================================================
def test_session_messages() -> None:
    section("C. 会话消息持久化（SQLite + GET /sessions/{id}/messages）")

    tid = "verify-sess-" + uuid.uuid4().hex[:8]
    try:
        r1 = chat("第一个问题：请记住数字 4242。", mode="agent", thread_id=tid,
                  timeout=240)
        check(bool(r1.get("answer")), "第 1 轮有回答")
        r2 = chat("第二个问题：请记住数字 7777。", mode="agent", thread_id=tid,
                  timeout=240)
        check(bool(r2.get("answer")), "第 2 轮有回答")
        r3 = chat("第三个问题：前面提到的数字有哪些？", mode="agent", thread_id=tid,
                  timeout=240)
        check(bool(r3.get("answer")), "第 3 轮有回答")
    except Exception as exc:
        bad("多轮对话", str(exc))
        return

    try:
        msgs = get(f"/sessions/{tid}/messages")
        items = msgs.get("messages") or []
        check(len(items) >= 6, "消息历史至少 6 条（3 问 3 答）",
              f"n={len(items)}")
        roles = [m.get("role") for m in items]
        check(roles.count("user") >= 3, "至少 3 条 user 消息",
              f"user={roles.count('user')}")
        check(roles.count("assistant") >= 3, "至少 3 条 assistant 消息",
              f"assistant={roles.count('assistant')}")
        seqs = [m.get("seq") for m in items]
        check(seqs == sorted(seqs), "消息按 seq 有序返回", str(seqs))
        check(all(m.get("ts") for m in items), "每条消息都有时间戳 ts")
        joined = " ".join(str(m.get("content") or "") for m in items)
        check("4242" in joined, "第 1 轮内容真正落盘（含 4242）")
        check("7777" in joined, "第 2 轮内容真正落盘（含 7777）")
    except Exception as exc:
        bad("GET /sessions/{id}/messages", str(exc))

    # SQLite 文件证据
    db = data_path("sessions.db")
    check(os.path.exists(db), "data/sessions.db 存在", db)
    if os.path.exists(db):
        try:
            con = sqlite3.connect(db)
            cur = con.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in cur.fetchall()]
            check("session_messages" in tables, "含 session_messages 表", str(tables))
            check("sessions" in tables, "含 sessions 表", str(tables))
            cur.execute(
                "SELECT COUNT(*) FROM session_messages WHERE thread_id=?", (tid,))
            n = cur.fetchone()[0]
            check(n >= 6, "session_messages 里确实有该会话的行", f"rows={n}")
            cur.execute("PRAGMA table_info(session_messages)")
            cols = [r[1] for r in cur.fetchall()]
            for c in ("thread_id", "seq", "role", "content", "ts"):
                check(c in cols, f"session_messages 含列 {c}", str(cols))
            con.close()
        except Exception as exc:
            bad("SQLite 直查", str(exc))

    # 列表里能看到
    try:
        lst = get("/sessions")
        sessions = lst.get("sessions") or []
        hit = [s for s in sessions if s.get("thread_id") == tid]
        check(len(hit) == 1, "GET /sessions 列表包含该会话", f"n={len(hit)}")
        if hit:
            check(int(hit[0].get("message_count") or 0) >= 6,
                  "会话 message_count ≥ 6",
                  f"count={hit[0].get('message_count')}")
            check("summary_status" in hit[0], "会话项含 summary_status 字段",
                  str(hit[0].get("summary_status")))
            check(hit[0].get("summary_status") in ("none", "pending", "ready"),
                  "summary_status 取值合法",
                  str(hit[0].get("summary_status")))
    except Exception as exc:
        bad("GET /sessions 列表", str(exc))

    # 单会话元信息
    try:
        s = get(f"/sessions/{tid}")
        check(s.get("thread_id") == tid, "GET /sessions/{id} 返回元信息")
        check("summary" in s or "summary_status" in s,
              "会话元信息含摘要字段")
    except Exception as exc:
        bad("GET /sessions/{id}", str(exc))

    return tid


# ======================================================================
# D. 会话摘要
# ======================================================================
def test_summarize(tid: Optional[str]) -> None:
    section("D. 会话摘要（POST /sessions/{id}/summarize）")

    if not tid:
        bad("摘要测试（缺少会话）")
        return

    try:
        s = get(f"/sessions/{tid}")
        check(s.get("summary_status") == "none" or not s.get("summary"),
              "摘要生成前 summary_status 非 ready",
              f"status={s.get('summary_status')}")
    except Exception as exc:
        bad("摘要前置状态", str(exc))

    try:
        r = requests.post(f"{BASE}/sessions/{tid}/summarize", timeout=300)
        r.raise_for_status()
        data = r.json()
        summary = data.get("summary") or data
        check(isinstance(summary, dict), "摘要返回结构化对象",
              f"keys={list(summary.keys()) if isinstance(summary, dict) else '?'}")
        if isinstance(summary, dict):
            check(bool(summary.get("conclusion")), "含一句话结论 conclusion",
                  str(summary.get("conclusion"))[:70])
            kp = summary.get("key_points") or summary.get("keypoints") or []
            check(isinstance(kp, list) and len(kp) >= 1, "含关键要点列表 key_points",
                  f"n={len(kp) if isinstance(kp, list) else '?'}")
            check("documents" in summary or "citations" in summary,
                  "含涉及文档/引用字段", str(list(summary.keys())))
            check("open_questions" in summary or "todos" in summary,
                  "含待办/未解决问题字段", str(list(summary.keys())))
    except Exception as exc:
        bad("POST /sessions/{id}/summarize", str(exc))
        return

    # 持久化
    try:
        s = get(f"/sessions/{tid}")
        check(s.get("summary_status") == "ready", "摘要状态变为 ready",
              f"status={s.get('summary_status')}")
        summ = s.get("summary")
        if isinstance(summ, dict):
            check(bool(summ.get("conclusion")), "持久化的摘要含结论")
        else:
            check(bool(summ), "持久化的摘要非空", str(summ)[:70])
    except Exception as exc:
        bad("摘要持久化读取", str(exc))

    # 列表里摘要状态
    try:
        lst = get("/sessions")
        hit = [s for s in (lst.get("sessions") or [])
               if s.get("thread_id") == tid]
        if hit:
            check(hit[0].get("summary_status") == "ready",
                  "列表里摘要状态为 ready",
                  str(hit[0].get("summary_status")))
    except Exception as exc:
        bad("列表摘要状态", str(exc))

    # SQLite 里确实有
    db = data_path("sessions.db")
    if os.path.exists(db):
        try:
            con = sqlite3.connect(db)
            cur = con.cursor()
            cur.execute(
                "SELECT summary, summary_status FROM sessions WHERE thread_id=?",
                (tid,))
            row = cur.fetchone()
            check(row is not None, "sessions 表里有该行")
            if row:
                check(bool(row[0]), "sessions.summary 已持久化", str(row[0])[:70])
                check(row[1] == "ready", "sessions.summary_status = ready",
                      str(row[1]))
            con.close()
        except Exception as exc:
            bad("SQLite 摘要直查", str(exc))


# ======================================================================
# E. 删除会话
# ======================================================================
def test_delete_session() -> None:
    section("E. 删除会话（DELETE /sessions/{id}）")
    tid = "verify-del-" + uuid.uuid4().hex[:8]
    try:
        chat("用于删除测试的一句话问题。", mode="agent", thread_id=tid, timeout=240)
    except Exception as exc:
        bad("删除测试的对话", str(exc))
        return
    try:
        r = requests.delete(f"{BASE}/sessions/{tid}", timeout=60)
        check(r.status_code in (200, 404), "DELETE /sessions/{id} 可调用",
              f"status={r.status_code}")
        m = get(f"/sessions/{tid}/messages")
        check(not (m.get("messages") or []), "删除后消息历史为空",
              f"n={len(m.get('messages') or [])}")
    except Exception as exc:
        bad("DELETE /sessions/{id}", str(exc))


# ======================================================================
def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("API_BASE", BASE))
    args = ap.parse_args()
    BASE = args.base.rstrip("/")

    print(f"verify_modes → {BASE}")
    t0 = time.time()

    try:
        get("/health", timeout=15)
    except Exception as exc:
        print(f"!! 后端不可用: {exc}")
        return 2

    test_model_registry()
    test_modes()
    tid = test_session_messages()
    test_summarize(tid)
    test_delete_session()

    print(f"\n{'=' * 60}")
    print(f"verify_modes: {PASS}/{PASS + FAIL} passed  ({time.time() - t0:.1f}s)")
    if FAILURES:
        print("\n失败明细：")
        for f in FAILURES:
            print(f"  - {f}")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
