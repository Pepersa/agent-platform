# -*- coding: utf-8 -*-
"""交互模式：Agent / Ask / Plan。

对应 Cursor 编辑器的三档交互模式（本项目实现等价语义）：

    · **Agent** —— 完整 ReAct 循环。可调用任意工具（含写操作，走 HITL 审批）。
    · **Ask**   —— 只读问答。只允许 ``READ_ONLY`` / ``COMPUTE`` 工具；
                    最多 1-2 轮工具调用后直接作答，不进入完整 ReAct 循环。
    · **Plan**  —— 只规划不执行。调用模型产出**结构化执行计划**
                    （步骤数组 / 涉及工具 / 风险点），**不调用任何工具**；
                    计划交给用户「采纳执行」或「放弃」。

设计取舍：
    · 模式**不硬编码模型路由**，而是复用 ``config/routing.yaml`` 的 ``routes:``
      机制 —— 新增 ``ask`` / ``plan`` 两个路由项即可（见 routing.yaml）。
    · Plan 模式的计划输出是**结构化 JSON**（步骤数组），前端才能渲染卡片。
    · Ask 模式的工具白名单是**权限维度**（READ_ONLY / COMPUTE）而非硬编码
      工具名清单 —— 新增只读工具无需改本文件即自动纳入。

模块只依赖标准库 + 本项目 core，无第三方依赖。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence

#: 三种交互模式
MODES = ("agent", "ask", "plan")

#: 模式中文名（前端 chip / 提示用）
MODE_LABELS: Dict[str, str] = {
    "agent": "Agent",
    "ask": "Ask",
    "plan": "Plan",
}

#: 模式描述
MODE_DESCRIPTIONS: Dict[str, str] = {
    "agent": "完整 ReAct 循环，可调用任意工具（写操作需人工确认）",
    "ask": "只读问答：仅允许 READ_ONLY / COMPUTE 工具，最多 1-2 轮调用",
    "plan": "只规划不执行：产出结构化执行计划，不调用任何工具",
}

#: Ask 模式允许的工具权限（**只读**维度）
ASK_ALLOWED_PERMISSIONS = ("READ_ONLY", "COMPUTE")

#: Ask 模式最大工具**调用轮数**（不是检索次数）
#:
#: 语义澄清（v1.1）：一次"轮"= 一次 agent→tools 的往返，即**一批** tool_call。
#:   · 计数在 tools 节点每次执行完一批工具后 +1；
#:   · 达到上限后**不再执行工具**，而是提示模型基于已有信息直接作答；
#:   · 这与"检索次数"是两个概念 —— 一轮里可以调用多个不同工具。
#:
#: 注意：判定为"本轮无需检索"时，模型**根本拿不到**检索工具 schema，
#: 因此通用问题不会消耗任何工具轮次（见 ``core/retrieval_policy.py``）。
ASK_MAX_TOOL_ROUNDS = 2

#: Plan 模式使用的路由项（复用 routing.yaml 的 routes 机制）
PLAN_ROUTE = "plan"
ASK_ROUTE = "ask"

#: Plan 模式系统提示词
PLAN_SYSTEM_PROMPT = """你是企业内部的规划助手。你的任务是**只制定执行计划，不执行任何工具调用**。

你必须输出**严格的 JSON**（不要包裹 markdown 围栏，不要有额外解释文字），格式如下：
{
  "goal": "一句话说明这次要达成的目标",
  "steps": [
    {
      "title": "步骤标题（不超过 30 字）",
      "detail": "这一步要做什么、怎么做（1-2 句）",
      "tools": ["预计调用的工具名"],
      "risk": "低 | 中 | 高"
    }
  ],
  "risks": ["整体风险点"],
  "questions": ["需要用户确认的问题"]
}

要求：
1. steps 至少 1 条、至多 8 条，按执行顺序排列；
2. tools 填**真实的工具名**（如 kb_search / tp_lookup / sql_query / spec_extract），
   拿不准就填最接近的真实工具名，不要臆造工具；
3. risk 只能取 低 / 中 / 高 三个值；
4. 涉及写数据的步骤必须标注 risk 为 高 或 中；
5. 只输出 JSON 对象本身。"""

#: Ask 模式系统提示词（**保留给独立使用 Ask 客户端的场景**）
#:
#: ⚠️ 当前 API 的 Ask 模式与 Agent 模式**共用** ``orchestration/prompts/system.md``
#: （见 ``api/app.py`` 的 ``system_prompt=get_system_prompt()``），
#: 本常量不参与主链路；保留它是为了让"Ask 提示词"的语义与主提示词一致，
#: 避免将来接入独立 Ask 客户端时又退回"什么都要检索"的旧行为。
ASK_SYSTEM_PROMPT = """你是企业内部的只读问答助手。

规则：
1. 只允许使用**只读**工具（READ_ONLY / COMPUTE），严禁调用任何写数据 / 对外副作用 / 管理员工具；
2. **有条件的检索**：先判断"答案是否取决于用户上传的文档"。
   A 类事实/参数类（限值、参数、定义、告警码、规程、统计数值）必须先调用只读检索工具查证；
   B 类通用/常识类（你是谁、你是什么模型、能做什么、写代码、翻译）**禁止**调用检索工具，直接回答；
   C 类闲聊简短回应。边界不确定时按 A 类处理；
3. 数字只能来自工具结果，禁止自行计算或改写；
4. 事实性断言后附 [来源:chunk_id]；
5. "未在知识库中找到"**只在本应走检索的问题**里说；
   对 B / C 类问题直接作答，**禁止**把"没有检索"说成"检索为空"；
6. 工具调用轮次有限（最多 1-2 轮），请**一次性**把需要的只读信息查齐，
   然后直接给出最终答案，不要反复试探；
7. 同一个工具不要用不同查询词重复检索同一件事；
   系统会拦截重复调用并提示你"换一种方式或直接作答"。"""


def normalize_mode(raw: Any) -> str:
    """把任意输入归一成合法模式（默认 ``agent``）。"""
    text = str(raw or "").strip().lower()
    if text in MODES:
        return text
    # 常见别名
    alias = {
        "read": "ask", "readonly": "ask", "q": "ask", "question": "ask",
        "planning": "plan", "planner": "plan",
        "auto": "agent", "full": "agent", "act": "agent",
    }
    return alias.get(text, "agent")


def mode_label(mode: str) -> str:
    """模式的中文展示名。"""
    return MODE_LABELS.get(normalize_mode(mode), "Agent")


def mode_description(mode: str) -> str:
    """模式的说明文案。"""
    return MODE_DESCRIPTIONS.get(normalize_mode(mode), MODE_DESCRIPTIONS["agent"])


def is_write_permission(permission: Any) -> bool:
    """权限是否属于「写 / 对外 / 管理员」类（Ask 模式禁止）。"""
    p = str(permission or "").strip().upper()
    return p in ("WRITE_DATA", "EXTERNAL_SIDE_EFFECT", "ADMIN")


def allowed_permissions(mode: str) -> Sequence[str]:
    """某模式允许的工具权限集合。

    · Agent → 全部（写操作仍走 HITL 审批）
    · Ask   → 仅 READ_ONLY / COMPUTE
    · Plan  → 空（**不调用任何工具**）
    """
    m = normalize_mode(mode)
    if m == "ask":
        return ASK_ALLOWED_PERMISSIONS
    if m == "plan":
        return ()
    return ("READ_ONLY", "COMPUTE", "WRITE_DATA", "EXTERNAL_SIDE_EFFECT", "ADMIN")


# ----------------------------------------------------------------------
# Plan 结构化输出解析
# ----------------------------------------------------------------------
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)\s*```", re.DOTALL)


def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """从模型输出里抽取第一个 JSON 对象（容忍围栏 / 前后解释文字）。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    # 1) 直接解析
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    # 2) markdown 围栏
    m = _FENCE_RE.search(raw)
    if m:
        try:
            obj = json.loads(m.group(1).strip())
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    # 3) 平衡括号抽取
    start = raw.find("{")
    if start >= 0:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(raw[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except Exception:
                        break
    return None


def normalize_plan(raw: Any, *, goal_hint: str = "") -> Dict[str, Any]:
    """把模型输出归一成**结构化计划**（步骤数组）。

    Args:
        raw: 模型原始输出（字符串或已解析的 dict）。
        goal_hint: 用户问题（当模型没给 goal 时兜底）。

    Returns:
        ``{goal, steps:[{title,detail,tools,risk}], risks:[], questions:[], raw}``
    """
    obj: Optional[Dict[str, Any]] = None
    if isinstance(raw, dict):
        obj = raw
    elif isinstance(raw, str):
        obj = extract_json_object(raw)
    if obj is None:
        # 完全无法解析 → 降级为单步「人工确认」计划，保证前端始终有结构可渲染
        text = str(raw or "").strip()
        return {
            "goal": goal_hint or "（模型未返回结构化计划）",
            "steps": [{
                "title": "人工确认执行方式",
                "detail": (text[:600] or "模型未返回可解析的计划，请手动确认后续动作。"),
                "tools": [],
                "risk": "中",
            }],
            "risks": [],
            "questions": ["模型未返回结构化计划，是否仍要执行？"],
            "raw": text[:2000],
            "parsed": False,
        }

    steps: List[Dict[str, Any]] = []
    for item in (obj.get("steps") or [])[:8]:
        if isinstance(item, str):
            item = {"title": item[:40], "detail": item}
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("name") or "").strip() or "未命名步骤"
        detail = str(item.get("detail") or item.get("description") or "").strip()
        tools_raw = item.get("tools") or item.get("tool") or []
        if isinstance(tools_raw, str):
            tools_raw = [tools_raw]
        tools = [str(t).strip() for t in tools_raw if str(t).strip()][:8]
        risk = str(item.get("risk") or item.get("level") or "低").strip()
        if risk not in ("低", "中", "高"):
            risk = {"low": "低", "medium": "中", "high": "高"}.get(risk.lower(), "低")
        steps.append({
            "title": title[:60],
            "detail": detail[:600],
            "tools": tools,
            "risk": risk,
        })
    if not steps:
        steps = [{
            "title": "补充信息后再执行",
            "detail": "模型未给出可执行步骤，请补充需求。",
            "tools": [],
            "risk": "低",
        }]

    risks = obj.get("risks") or []
    if isinstance(risks, str):
        risks = [risks]
    questions = obj.get("questions") or obj.get("open_questions") or []
    if isinstance(questions, str):
        questions = [questions]
    return {
        "goal": str(obj.get("goal") or obj.get("objective") or goal_hint or "").strip()[:300],
        "steps": steps,
        "risks": [str(r).strip()[:200] for r in risks if str(r).strip()][:8],
        "questions": [str(q).strip()[:200] for q in questions if str(q).strip()][:8],
        "raw": "",
        "parsed": True,
    }


def plan_prompt_for(question: str, tool_names: Optional[Sequence[str]] = None) -> List[Dict[str, str]]:
    """构造 Plan 模式的对话消息（system + user）。"""
    msgs = [{"role": "system", "content": PLAN_SYSTEM_PROMPT}]
    if tool_names:
        joined = ", ".join(list(tool_names)[:40])
        msgs.append({
            "role": "system",
            "content": f"当前可用的真实工具名（请只使用这些名字）：{joined}",
        })
    msgs.append({"role": "user", "content": str(question or "")})
    return msgs


def ask_prompt_for(question: str) -> List[Dict[str, str]]:
    """构造 Ask 模式的对话消息（system + user）。"""
    return [
        {"role": "system", "content": ASK_SYSTEM_PROMPT},
        {"role": "user", "content": str(question or "")},
    ]


__all__ = [
    "MODES", "MODE_LABELS", "MODE_DESCRIPTIONS",
    "ASK_ALLOWED_PERMISSIONS", "ASK_MAX_TOOL_ROUNDS",
    "PLAN_ROUTE", "ASK_ROUTE",
    "PLAN_SYSTEM_PROMPT", "ASK_SYSTEM_PROMPT",
    "normalize_mode", "mode_label", "mode_description",
    "allowed_permissions", "is_write_permission",
    "normalize_plan", "plan_prompt_for", "ask_prompt_for",
    "extract_json_object",
]
