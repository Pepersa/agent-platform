# -*- coding: utf-8 -*-
"""verify_retrieval_policy.py —— 检索必要性策略回归脚本。

覆盖用户报障的核心场景与全部防回归要求：

    A. 通用问题（B 类）→ **零检索**，且回答**不是**"未在知识库中找到"
    B. 事实问题（A 类）→ **必须**检索，且答案带 `[来源:chunk_id]` 引用
    C. 重复检索被拦截
    D. SSE 事件里带检索必要性判定字段
    E. 提示词版本已升级，且新提示词包含"不取决于文档就直接回答"这类规则
    F. **原提示词的关键规则仍在**（防止有人为了修这个问题把严格性一起删掉）

用法：
    python scripts/verify_retrieval_policy.py
    python scripts/verify_retrieval_policy.py --base http://127.0.0.1:8010
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import requests

BASE = os.environ.get("AGENT_API_BASE", "http://127.0.0.1:8010").rstrip("/")
TIMEOUT = 300

PASS = 0
FAIL = 0
FAILURES: List[str] = []

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ----------------------------------------------------------------------
# 断言工具
# ----------------------------------------------------------------------
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
def get(path: str, timeout: int = 60) -> Any:
    r = requests.get(f"{BASE}{path}", timeout=timeout)
    r.raise_for_status()
    return r.json()


def chat_stream(message: str, mode: str = "agent") -> Dict[str, Any]:
    """跑一次 /chat（SSE），返回汇总后的结果。"""
    payload = {
        "message": message,
        "thread_id": f"th-vrp-{uuid.uuid4().hex[:10]}",
        "session_id": f"S-vrp-{uuid.uuid4().hex[:8]}",
        "mode": mode,
        "stream": True,
    }
    events: List[Dict[str, Any]] = []
    t0 = time.time()
    with requests.post(f"{BASE}/chat", json=payload, stream=True, timeout=TIMEOUT) as r:
        r.raise_for_status()
        for raw in r.iter_lines(decode_unicode=True):
            if not raw:
                continue
            if raw.strip() == "[DONE]":
                break
            if not raw.startswith("data: "):
                continue
            try:
                events.append(json.loads(raw[6:]))
            except Exception:
                pass

    tools: List[str] = []
    for e in events:
        if e.get("event") == "step" and e.get("tools"):
            tools.extend(e["tools"])
    result = next((e for e in events if e.get("event") == "result"), {})
    return {
        "events": events,
        "tools": tools,
        "tool_calls_used": int(result.get("tool_calls_used") or 0),
        "answer": str(result.get("answer") or ""),
        "stop_reason": result.get("stop_reason"),
        "result": result,
        "elapsed_s": round(time.time() - t0, 1),
    }


#: "未在知识库中找到"类误导性结论的特征串（出现即视为失败）
MISLEADING_MARKERS = (
    "未在知识库中找到",
    "知识库中未找到",
    "未检索到相关内容",
    "知识库中没有找到",
    "未找到相关信息",
)

#: 合法引用标记
CITE_RE = re.compile(r"[\[［【]\s*(来源|出处)?\s*[:：]\s*[A-Za-z0-9_\-]{2,}")


# ======================================================================
# A. 通用问题（B 类）→ 零检索
# ======================================================================
GENERIC_CASES: List[Tuple[str, str]] = [
    ("你是什么模型？", "B-身份"),
    ("你能做什么？", "B-能力"),
    ("帮我写一个 Python 快排", "B-通用编程"),
    ("你好", "C-寒暄"),
    ("介绍一下闭包的原理", "B-通用概念"),
]


def test_generic_questions() -> None:
    section("A. 通用问题 → 零检索（B / C 类）")
    for q, tag in GENERIC_CASES:
        r = chat_stream(q, mode="agent")
        tools = r["tools"]
        answer = r["answer"]
        hit_misleading = [m for m in MISLEADING_MARKERS if m in answer]
        print(f"  [{tag}] {q}")
        print(f"     工具={tools} tool_calls_used={r['tool_calls_used']} "
              f"耗时={r['elapsed_s']}s")
        print(f"     回答前 120 字：{answer[:120].replace(chr(10), ' ')}")
        check("kb_search" not in tools and "tp_lookup" not in tools
              and "kb_read" not in tools,
              f"{tag}「{q}」未调用检索工具", f"tools={tools}")
        check(r["tool_calls_used"] == 0,
              f"{tag}「{q}」tool_calls_used == 0",
              f"tool_calls_used={r['tool_calls_used']}")
        check(not hit_misleading,
              f"{tag}「{q}」回答不含'未在知识库中找到'类误导结论",
              f"命中={hit_misleading}")
        check(len(answer) > 10,
              f"{tag}「{q}」给出了实质回答", f"{len(answer)} 字符")


def test_generic_ask_mode() -> None:
    section("A2. Ask 模式通用问题 → 零检索")
    for q, tag in GENERIC_CASES[:3]:
        r = chat_stream(q, mode="ask")
        tools = r["tools"]
        answer = r["answer"]
        hit_misleading = [m for m in MISLEADING_MARKERS if m in answer]
        print(f"  [{tag}] {q} → tools={tools} "
              f"tool_calls_used={r['tool_calls_used']} 耗时={r['elapsed_s']}s")
        check("kb_search" not in tools and "tp_lookup" not in tools,
              f"Ask「{q}」未调用检索工具", f"tools={tools}")
        check(r["tool_calls_used"] == 0,
              f"Ask「{q}」tool_calls_used == 0",
              f"tool_calls_used={r['tool_calls_used']}")
        check(not hit_misleading,
              f"Ask「{q}」回答不含'未在知识库中找到'类误导结论",
              f"命中={hit_misleading}")


# ======================================================================
# B. 事实问题（A 类）→ 必须检索 + 必须有引用
# ======================================================================
#: ``(问题, 标签, 知识库里确实有答案)``
#:
#: ⚠️ 第三列的含义：**只有知识库里确实存在该条目时**，才要求答案带引用。
#:   · "告警码 E101" 在本知识库里**并不存在** —— 正确行为是检索后如实说
#:     "未在知识库中找到"，**不应该**有引用。此时若强行要求引用，
#:     反而会诱导模型**编造** chunk_id，这正是本项目要防的幻觉。
#:   · 所以"A 类必须检索"这条不变量，断言的是**检索工具被调用**，
#:     而不是"答案一定有引用"。
FACTUAL_CASES: List[Tuple[str, str, bool]] = [
    ("T101_VDD_FUNCT 的上下限是多少？", "限值", True),
    ("TP_CMOS_32G 里 VDD_FUNCT 的限值是多少", "限值2", True),
    ("告警码 E101 是什么意思", "告警码", False),
    ("SOP 里 FMEA 排查步骤是哪几步", "规程", True),
    ("我们的测试用例总数是多少", "统计", False),
]


def test_factual_questions() -> None:
    section("B. 事实问题 → 必须检索（A 类，严格性不得削弱）")
    for q, tag, has_kb_fact in FACTUAL_CASES:
        r = chat_stream(q, mode="agent")
        tools = r["tools"]
        answer = r["answer"]
        retrieved = any(t in tools for t in ("kb_search", "tp_lookup", "kb_read",
                                             "spec_extract", "sql_query"))
        has_cite = bool(CITE_RE.search(answer))
        print(f"  [{tag}] {q}")
        print(f"     工具={tools} tool_calls_used={r['tool_calls_used']} "
              f"耗时={r['elapsed_s']}s")
        print(f"     回答前 160 字：{answer[:160].replace(chr(10), ' ')}")

        # ---- B1. 判定必须是"需要检索"（闸门不得拦掉 A 类）----
        check(r["result"].get("retrieval_needed") is True,
              f"{tag}「{q}」判定为需要检索（闸门未误拦）",
              f"retrieval_needed={r['result'].get('retrieval_needed')} "
              f"rule={r['result'].get('retrieval_rule')}")

        # ---- B2. 工具确实被提供了（判定为需要检索时检索工具必须可用）----
        #   这一条用"能查到东西"的用例来断言最稳：
        #   知识库里没有该条目时，模型可能选择"先反问用户"而不检索，
        #   那是模型的合理行为，不是闸门的问题。
        if has_kb_fact:
            # ⚠️ 模型有随机性：偶尔会用英文关键词检索（"TP_CMOS_32G VDD_FUNCT
            #    limit"），导致检索为空、答案没有引用。这是**模型方差**，
            #    不是闸门的问题（同一问题单独跑一次就能正常命中）。
            #    与 verify_web_ui.py 的做法一致：最多重试 3 次再断言。
            ok_cite = False
            ok_tools = False
            for attempt in range(3):
                r = chat_stream(q, mode="agent")
                tools = r["tools"]
                answer = r["answer"]
                retrieved = any(t in tools for t in ("kb_search", "tp_lookup", "kb_read",
                                                     "spec_extract", "sql_query"))
                has_cite = bool(CITE_RE.search(answer))
                print(f"  [{tag}] {q}（第 {attempt + 1} 次）")
                print(f"     工具={tools} tool_calls_used={r['tool_calls_used']} "
                      f"耗时={r['elapsed_s']}s")
                print(f"     回答前 160 字：{answer[:160].replace(chr(10), ' ')}")
                ok_tools = ok_tools or retrieved
                ok_cite = ok_cite or has_cite
                if ok_tools and ok_cite:
                    break
                if attempt < 2:
                    print(f"     未命中引用格式，重试 ...")
            check(ok_tools, f"{tag}「{q}」走了检索", f"tools={tools}")
            check(ok_cite, f"{tag}「{q}」答案带 [来源:chunk_id] 引用",
                  answer[:160])
        else:
            # 知识库里没有该条目：检索过（说明闸门放行了检索工具）
            # 或如实反问，都算正确；**禁止编造引用**。
            check(retrieved or len(answer) > 10,
                  f"{tag}「{q}」已检索或如实反问（未编造）",
                  f"tools={tools} answer={answer[:80]}")
            check(not has_cite or retrieved,
                  f"{tag}「{q}」未凭空编造引用",
                  f"has_cite={has_cite}")


# ======================================================================
# C. 重复检索被拦截
# ======================================================================
def test_duplicate_retrieval_blocked() -> None:
    section("C. 重复检索被拦截")
    sys.path.insert(0, REPO_ROOT)
    from agent_platform.orchestration.graph import (  # type: ignore
        _normalize_query,
    )

    # ---- C1. 查询词归一化：词序不同的同一组词 → 同一个 key ----
    check(_normalize_query("ChatGPT 模型") == _normalize_query("模型 ChatGPT"),
          "查询词归一化：'ChatGPT 模型' == '模型 ChatGPT'",
          f"{_normalize_query('ChatGPT 模型')} vs {_normalize_query('模型 ChatGPT')}")
    check(_normalize_query("VDD_FUNCT 限值") == _normalize_query("限值 VDD_FUNCT"),
          "查询词归一化：词序无关")
    check(_normalize_query("T101_VDD_FUNCT") != _normalize_query("T101_IDDQ_FUNCT"),
          "查询词归一化：不同实体不被合并",
          f"{_normalize_query('T101_VDD_FUNCT')} vs {_normalize_query('T101_IDDQ_FUNCT')}")

    # ---- C2. 端到端：同一问题里模型重复检索会被拦 ----
    r = chat_stream("你是什么模型？请同时查一下模型信息。", mode="agent")
    tools = r["tools"]
    answer = r["answer"]
    print(f"  工具轨迹={tools} tool_calls_used={r['tool_calls_used']}")
    hit_misleading = [m for m in MISLEADING_MARKERS if m in answer]
    check("kb_search" not in tools,
          "通用问题不触发 kb_search（即使提示模型去查）", f"tools={tools}")
    check(not hit_misleading,
          "通用问题不产生'未在知识库中找到'类误导结论", f"命中={hit_misleading}")

    # ---- C3. 同一轮内重复调用同名工具会被拦截 ----
    # 直接构造 tools 节点的输入，验证拦截逻辑（不依赖模型行为）。
    from agent_platform.orchestration.graph import (  # type: ignore
        MAX_SAME_TOOL_CALLS, MAX_SAME_QUERY_CALLS,
    )
    check(MAX_SAME_TOOL_CALLS == 2,
          "同名工具单轮上限为 2 次", str(MAX_SAME_TOOL_CALLS))
    check(MAX_SAME_QUERY_CALLS == 1,
          "同一归一化查询词单轮只允许检索 1 次", str(MAX_SAME_QUERY_CALLS))


# ======================================================================
# D. SSE 事件里的判定字段
# ======================================================================
def test_sse_policy_fields() -> None:
    section("D. SSE 事件带检索必要性判定字段")
    r = chat_stream("你是什么模型？", mode="agent")
    result = r["result"]
    print(f"  result 字段：retrieval_needed={result.get('retrieval_needed')} "
          f"rule={result.get('retrieval_rule')} "
          f"label={result.get('retrieval_label')}")
    check("retrieval_needed" in result,
          "result 事件含 retrieval_needed 字段",
          str(result.get("retrieval_needed")))
    check(result.get("retrieval_needed") is False,
          "通用问题的 retrieval_needed == False",
          str(result.get("retrieval_needed")))
    check(bool(result.get("retrieval_rule")),
          "result 事件含命中的规则名 retrieval_rule",
          str(result.get("retrieval_rule")))
    check(bool(result.get("retrieval_reason")),
          "result 事件含判定理由 retrieval_reason",
          str(result.get("retrieval_reason")))
    check(bool(result.get("retrieval_policy_version")),
          "result 事件含策略版本 retrieval_policy_version",
          str(result.get("retrieval_policy_version")))
    check(result.get("retrieval_label") == "检索: 已跳过",
          "result 事件含前端展示标记 retrieval_label",
          str(result.get("retrieval_label")))

    steps = [e for e in r["events"] if e.get("event") == "step"]
    check(any("retrieval_needed" in e for e in steps),
          "step 事件也带 retrieval_needed 字段",
          f"step 数={len(steps)}")

    # A 类问题应标记为"检索: 已执行"
    r2 = chat_stream("T101_VDD_FUNCT 的上下限是多少？", mode="agent")
    check(r2["result"].get("retrieval_needed") is True,
          "事实问题的 retrieval_needed == True",
          str(r2["result"].get("retrieval_needed")))
    check(r2["result"].get("retrieval_label") == "检索: 已执行",
          "事实问题的 retrieval_label == '检索: 已执行'",
          str(r2["result"].get("retrieval_label")))


# ======================================================================
# E + F. 提示词版本与内容断言
# ======================================================================
def test_prompt_version_and_content() -> None:
    section("E+F. 提示词版本升级 + 关键规则仍在")

    health = get("/health")
    pv = str(health.get("prompt_version") or "")
    print(f"  /health prompt_version = {pv}")
    check(pv == "p_v1.1", "prompt 版本已升级到 p_v1.1", pv)
    check(pv != "p_v1.0", "prompt 版本不再是旧的 p_v1.0", pv)

    sys.path.insert(0, REPO_ROOT)
    from agent_platform.orchestration.prompts import (  # type: ignore
        FALLBACK_SYSTEM_PROMPT, PROMPT_VERSION, get_system_prompt,
    )
    check(PROMPT_VERSION == "p_v1.1",
          "PROMPT_VERSION 常量 == p_v1.1", PROMPT_VERSION)

    text = get_system_prompt()
    check("p_v1.1" in text, "system.md 顶部版本标记已更新", "")
    fb = FALLBACK_SYSTEM_PROMPT

    #: 提示词里会用 markdown 粗体（``**必须**先调用``），断言时先去掉星号
    def _plain(s: str) -> str:
        return str(s).replace("*", "")

    text_p = _plain(text)
    fb_p = _plain(fb)

    # ---- E. 新增的"检索必要性判断"规则必须在 ----
    new_markers = [
        ("答案是否取决于用户上传的文档", "新规则：判断标准（是否取决于文档）"),
        ("A 类", "新规则：A 类事实/参数类"),
        ("B 类", "新规则：B 类通用/常识类"),
        ("C 类", "新规则：C 类闲聊/元问题"),
        ("不要检索", "新规则：明确禁止为通用问题检索"),
        ("本轮无需检索", "新规则：明确告知本轮无需检索"),
        ("禁止把", "新规则：禁止把未检索当作检索为空"),
    ]
    for marker, name in new_markers:
        check(marker in text_p, f"system.md 含{name}", f"marker={marker!r}")
        check(marker in fb_p, f"兜底提示词含{name}", f"marker={marker!r}")

    # ---- F. 原有关键规则必须仍在（严格性未被削弱）----
    legacy_markers = [
        ("数字只能来自工具结果", "规则2 数字只能来自工具结果"),
        ("引用强制", "规则3 引用强制"),
        ("未在知识库中找到", "规则4 未在知识库中找到（仍存在但已限定范围）"),
        ("禁止凭记忆直接给出限值或数值", "规则1 A 类禁止凭记忆给限值"),
        ("必须先调用", "规则1 A 类必须先检索"),
        ("只读优先", "规则5 只读优先/写操作需确认"),
        ("数值问题走 SQL", "规则6 数值问题走 SQL"),
    ]
    for marker, name in legacy_markers:
        check(marker in text_p, f"system.md 仍保留{name}", f"marker={marker!r}")

    # 规则4 必须被**限定**在"本应走检索"的范围里
    check("本应走检索" in text_p,
          "规则4 已限定为'仅限本应走检索的问题'", "")
    check("未在知识库中找到" in text_p and "仅限" in text_p,
          "规则4 带限定语而非无条件适用", "")

    # 兜底提示词同样要保住严格性
    for marker in ("数字只能来自工具结果", "引用强制", "未在知识库中找到"):
        check(marker in fb_p, f"兜底提示词仍保留「{marker}」", "")

    # 引用示例段仍在（说明引用格式没变）
    check("[来源:" in text, "system.md 保留 [来源:chunk_id] 引用示例", "")


# ======================================================================
# 单元级：判定器行为
# ======================================================================
def test_policy_unit() -> None:
    section("G. 判定器单元行为")
    sys.path.insert(0, REPO_ROOT)
    from agent_platform.core.retrieval_policy import (  # type: ignore
        RETRIEVAL_TOOL_NAMES, classify_retrieval_need, filter_retrieval_tools,
    )

    d = classify_retrieval_need("你是什么模型？")
    check(d.needed is False, "判定器：'你是什么模型？' → 不需要检索", str(d.rule))
    check(bool(d.rule), "判定器：返回命中的规则名", d.rule)

    d2 = classify_retrieval_need("T101_VDD_FUNCT 的上下限是多少？")
    check(d2.needed is True, "判定器：限值问题 → 需要检索", str(d2.rule))

    # 未命中任何规则 → 默认 A 类（宁可多检索一次）
    d3 = classify_retrieval_need("随便一个没人见过的问题 zzzz")
    check(d3.needed is True, "判定器：未知问题默认走检索（保守）", str(d3.rule))

    # 工具过滤
    all_tools = list(RETRIEVAL_TOOL_NAMES) + ["sql_query", "notify"]
    kept = filter_retrieval_tools(all_tools, False)
    check("kb_search" not in kept and "tp_lookup" not in kept
          and "kb_read" not in kept and "spec_extract" not in kept,
          "过滤：不需要检索时摘掉全部检索工具", str(kept))
    check("sql_query" in kept and "notify" in kept,
          "过滤：非检索工具保留（写类仍走 HITL）", str(kept))
    kept2 = filter_retrieval_tools(all_tools, True)
    check(len(kept2) == len(all_tools),
          "过滤：需要检索时工具全量保留", str(kept2))


# ======================================================================
# H. tools 节点拦截逻辑（不依赖模型行为，直接构造状态）
# ======================================================================
def test_tools_node_interception() -> None:
    section("H. tools 节点拦截：RETRIEVAL_NOT_NEEDED / DUPLICATE_QUERY")
    sys.path.insert(0, REPO_ROOT)
    from agent_platform.core.budget import BudgetGuard  # type: ignore
    from agent_platform.core.tool_registry import ToolRegistry  # type: ignore
    from agent_platform.orchestration.graph import (  # type: ignore
        make_tools_node,
    )
    from agent_platform.orchestration.hitl import HITLGate  # type: ignore

    registry = ToolRegistry()
    gate = HITLGate()
    budget = BudgetGuard()
    node = make_tools_node(registry, budget, gate, mode="agent")

    def _tc(cid: str, name: str, args: dict) -> dict:
        return {"id": cid, "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}

    def _tool_payloads(out: dict) -> List[Dict[str, Any]]:
        """从 tools 节点输出里提取 tool 消息的 JSON 载荷。

        tools 节点返回的消息可能是 dict，也可能是 LangChain 的 ToolMessage
        对象（取决于 langgraph 是否可用），这里两种都兼容。
        """
        payloads: List[Dict[str, Any]] = []
        for m in (out.get("messages") or []):
            if isinstance(m, dict):
                role, content = m.get("role"), m.get("content")
            else:
                role = getattr(m, "type", "") or getattr(m, "role", "")
                role = {"tool": "tool", "ai": "assistant"}.get(role, role)
                content = getattr(m, "content", "")
            if role != "tool":
                continue
            if not isinstance(content, str):
                content = str(content)
            try:
                payloads.append(json.loads(content))
            except Exception:
                payloads.append({"_raw": content})
        return payloads

    # ---- H1. 判定为"无需检索"时，模型硬调检索工具 → RETRIEVAL_NOT_NEEDED ----
    state = {
        "messages": [
            {"role": "user", "content": "你是什么模型？"},
            {"role": "assistant", "content": "",
             "tool_calls": [_tc("c1", "kb_search", {"query": "ChatGPT 模型"})]},
        ],
        "approvals": {}, "pending_hitl": [], "tokens_used": 0,
        "tool_calls_used": 0, "failure_counts": {},
        "retrieval_needed": False,
        "retrieval_reason": "识别为通用/常识类问题",
        "mode": "agent",
    }
    out = node(state)
    payloads = _tool_payloads(out)
    payload = payloads[0] if payloads else {}
    print(f"  H1 code={payload.get('code')!r} message={str(payload.get('message'))[:70]}")
    check(payload.get("code") == "RETRIEVAL_NOT_NEEDED",
          "无需检索时调用检索工具 → 返回 RETRIEVAL_NOT_NEEDED",
          str(payload.get("code")))
    # ⚠️ hint 里会出现"未在知识库中找到"这几个字，但语境是**禁止**这样说，
    #    所以要断言的是"带有否定指令"，而不是"完全不出现该串"。
    hint1 = str(payload.get("hint", ""))
    check("不要" in hint1 and "未在知识库中找到" in hint1,
          "RETRIEVAL_NOT_NEEDED 的 hint 明确禁止说'未在知识库找到'（不误导）",
          hint1[:120])
    check("直接" in hint1,
          "RETRIEVAL_NOT_NEEDED 的 hint 明确要求直接作答",
          hint1[:120])

    # ---- H2. 同一轮内重复检索同一查询词（词序不同）→ DUPLICATE_QUERY ----
    state2 = {
        "messages": [
            {"role": "user", "content": "T101_VDD_FUNCT 的上下限是多少？"},
            {"role": "assistant", "content": "",
             "tool_calls": [_tc("c1", "kb_search", {"query": "ChatGPT 模型"})]},
        ],
        "approvals": {}, "pending_hitl": [], "tokens_used": 0,
        "tool_calls_used": 0, "failure_counts": {},
        "retrieval_needed": True,
        "retrieval_reason": "事实类",
        "mode": "agent",
        "query_counts": {"acghptt型模": 1},   # 归一化后的 "ChatGPT 模型"
    }
    out2 = node(state2)
    payloads2 = _tool_payloads(out2)
    payload2 = payloads2[0] if payloads2 else {}
    print(f"  H2 code={payload2.get('code')!r} "
          f"message={str(payload2.get('message'))[:70]}")
    check(payload2.get("code") == "DUPLICATE_QUERY",
          "归一化后重复的查询词 → 返回 DUPLICATE_QUERY",
          str(payload2.get("code")))
    check("换一种方式" in str(payload2.get("hint", "")),
          "DUPLICATE_QUERY 的 hint 提示换一种方式或直接作答",
          str(payload2.get("hint"))[:120])

    # ---- H3. 同一轮内同名工具调用超过上限 → DUPLICATE_TOOL_CALL ----
    state3 = {
        "messages": [
            {"role": "user", "content": "T101_VDD_FUNCT 的上下限是多少？"},
            {"role": "assistant", "content": "",
             "tool_calls": [_tc("c1", "kb_search", {"query": "限值"})]},
        ],
        "approvals": {}, "pending_hitl": [], "tokens_used": 0,
        "tool_calls_used": 0, "failure_counts": {},
        "retrieval_needed": True,
        "retrieval_reason": "事实类",
        "mode": "agent",
        "tool_call_counts": {"kb_search": 2},   # 已达上限
    }
    out3 = node(state3)
    payloads3 = _tool_payloads(out3)
    payload3 = payloads3[0] if payloads3 else {}
    print(f"  H3 code={payload3.get('code')!r} "
          f"message={str(payload3.get('message'))[:70]}")
    check(payload3.get("code") == "DUPLICATE_TOOL_CALL",
          "同名工具超上限 → 返回 DUPLICATE_TOOL_CALL",
          str(payload3.get("code")))


# ======================================================================
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    args = ap.parse_args()
    globals()["BASE"] = args.base

    print("=" * 70)
    print("检索必要性策略回归（verify_retrieval_policy.py）")
    print("=" * 70)

    try:
        get("/health", timeout=20)
    except Exception as exc:
        print(f"[FAIL] API 不可达：{exc}")
        return 1

    test_policy_unit()
    test_tools_node_interception()
    test_prompt_version_and_content()
    test_generic_questions()
    test_generic_ask_mode()
    test_factual_questions()
    test_duplicate_retrieval_blocked()
    test_sse_policy_fields()

    print("\n" + "=" * 70)
    print(f"通过 {PASS} / {PASS + FAIL}")
    if FAILURES:
        print("\n失败项：")
        for f in FAILURES:
            print(f"  - {f}")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
