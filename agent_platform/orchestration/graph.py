# -*- coding: utf-8 -*-
"""LangGraph 状态机 —— Agent 编排层核心。

对应设计文档：
    《01_架构方案_RAG_Agent.md》§2.4 Agent 编排层
        ReAct 循环 / HITL 人工确认闸门 / Checkpoint 断点续跑 / 预算与超时控制
        「失败重规划：工具连续失败时改计划而非死循环（≥2 次同类失败）」
    《01_架构方案_RAG_Agent.md》§3.2 Agent 工具调用可靠性设计
        「最多 8 轮 ReAct、同一工具同一参数连续 2 次失败即终止」
    《01_架构方案_RAG_Agent.md》§3.1 步骤 8/10（引用强制 + 未找到即拒答）

节点划分（硬性要求）：
        agent → tools(检查) → hitl → tools(执行) → agent
    实现为三个节点：``agent`` / ``tools`` / ``hitl``，其中 tools 节点分两段：

        第一段（gate 段）：只检查哪些 tool_call 需要 HITL 审批。
            未审批的放入 ``pending_hitl`` 并跳转到 hitl 节点，
            **不产生任何 tool 消息**（不执行、不写副作用）。
        第二段（execute 段）：所有需要的审批都已通过，才真正执行工具。

    这样设计的原因：LangGraph resume 时会从 interrupt 所在节点从头重跑，
    若在审批之前就执行工具，写操作会被执行两次。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Annotated, Any, Dict, List, Optional, Sequence, TypedDict

from ..core.budget import (
    BudgetExceeded,
    BudgetGuard,
    estimate_messages_tokens,
    estimate_tokens,
)
from ..core.citations import collect_chunk_ids, extract_citations, validate_citations
from ..core.tool_registry import Permission, ToolError
from .hitl import ApprovalDecision, HITLGate, PendingApproval
from .prompts import PROMPT_VERSION, get_system_prompt
try:  # pragma: no cover - 环境缺依赖时降级
    from langgraph.graph import END, START, StateGraph  # type: ignore
    from langgraph.graph.message import add_messages  # type: ignore
    LANGGRAPH_AVAILABLE = True
except Exception:  # pragma: no cover
    END = "end"
    START = "__start__"
    StateGraph = None  # type: ignore
    add_messages = None  # type: ignore
    LANGGRAPH_AVAILABLE = False


# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------
#: 同一轮内同名工具最多允许的调用次数（超过则拦截，提示"换一种方式或直接作答"）
#:
#: 为什么不用 ``failure_counts`` 里的"同参数连续失败 2 次"？
#:   那个机制只统计**失败**且**参数完全相同**的调用。用户实测里模型是
#:   ``kb_search("ChatGPT 模型")`` → ``kb_search("模型 ChatGPT")``：
#:   参数不同 → 旧机制拦不住；而且两次都是"成功"的（只是检索为空）。
#:   这里按**工具名**累计，与参数无关，直接堵住"换个词再查一遍"的空转。
#:
#: **写类工具例外**（2026-11-23）：写文件按**路径**计数，且允许同一路径
#: 写 3 次（见 ``MAX_SAME_WRITE_PATH_CALLS``）——"先建文件再改内容"
#: 是正常流程，不该被当空转拦下。
MAX_SAME_TOOL_CALLS = 2

#: 写类工具**同一路径**在同一轮内最多允许的调用次数
#:
#: 取 3 覆盖"创建 → 追加/改写 → 再修正"的正常流程，
#: 同时仍能拦住"反复覆写同一文件"的空转。
MAX_SAME_WRITE_PATH_CALLS = 3

#: ``kb_search`` 查询词归一化后，同一轮最多允许的**执行**次数
#:
#: 取 1 意味着"同一个归一化查询词在本轮只允许真正检索一次"。
#: 用户实测的重复检索正是这种形态：``"ChatGPT 模型"`` → ``"模型 ChatGPT"``，
#: 归一化后是同一个 key，属于同一件事检索了两次，第二次应当被拦掉。
#: 真正不同的检索词（不同字符集合）归一化后 key 不同，不受影响。
MAX_SAME_QUERY_CALLS = 1

#: 单轮**并行**工具调用数的硬上限（2026-11-22 新增）
#:
#: 为什么需要：gpt-oss 在检索类问题上偶发一次性返回**数千个** tool_calls。
#: 旧实现全部遍历，``tool_calls_used`` 会累加到 5003，进而触发
#: agent↔tools 死循环（LangGraph recursion_limit=10007 被耗尽）。
#: 这里给一个绝对上限，超出的调用**不执行**，只回一条错误 tool 消息。
#: 正常模型的并行调用数是个位数，32 足够宽松。
MAX_PARALLEL_TOOL_CALLS = 32

#: **单个工具在单轮内的最大执行次数**（2026-11-24 新增）
#:
#: 为什么需要（真实故障）：
#:   ``MAX_PARALLEL_TOOL_CALLS = 32`` 只限制「一轮里一共多少个并行调用」，
#:   但**不限制「同一个工具能占几个」**。实测 gpt-oss 在一轮里连发
#:   **7 个并行的 ``kb_search``**（不同查询词），每个都要跑一遍向量化检索
#:   （每次 1~3 秒），于是：
#:     · 一轮就吃掉 7 次工具调用（预算 24 次里占掉近三成）；
#:     · 更糟的是每个检索结果都会塞进上下文 → 上下文窗口被瞬间打满
#:       → 下一轮请求被 llama-server 以 400 拒绝 → 任务半途而废。
#:   表现就是用户说的「没几步就停了」。
#:
#: 语义：**同一个工具名**在**一轮内**最多执行 N 次。超出部分**丢弃**
#: （不是让整轮失败）—— 让 agent 还能用剩下的调用继续推进。
#:
#: 取值理由：
#:   · ``kb_search`` = 3 —— 一次复杂问题确实可能需要 2~3 次不同角度的检索；
#:   · 写类工具（``ws_*``）= 2 —— 一轮里改两个文件是合理的，改三个就该分轮；
#:   · 默认 = 4 —— 其它工具（sql_query / kb_read 等）留出足够余地。
MAX_PER_ROUND_TOOL_CALLS = 4

#: **检索类工具**的单轮上限（比默认更严）
#:
#: 检索是最"贵"的一类工具：一次要跑 embedding + rerank，还把大段 chunk
#: 塞回上下文。同一轮里跑太多次检索基本等于把上下文预算一次烧光。
MAX_PER_ROUND_RETRIEVAL_CALLS = 3

#: **写类工具**的单轮上限（比默认更严）
#:
#: 写操作有副作用（HITL 审批也要人看），一轮里连发多个写请求既容易误操作，
#: 也让审批卡片变得难以判断。留 2 个足够（一轮改两个文件）。
MAX_PER_ROUND_WRITE_CALLS = 2

#: 是否打印「上下文估算 vs 模型端真实 prompt token」的自检日志。
#: 生产环境保持 False（零开销）；校准 estimate_tokens 时置 1。
_DEBUG_TOKENS = os.environ.get("AGENT_DEBUG_TOKENS", "") not in ("", "0", "false")


# ----------------------------------------------------------------------
# 状态定义
# ----------------------------------------------------------------------
class AgentState(TypedDict, total=False):
    """Agent 图状态。

    messages      —— 对话 + 工具轨迹（add_messages reducer）
    approvals     —— {tool_call_id: ApprovalDecision.to_dict()}
    pending_hitl  —— 待人工审批的 PendingApproval 列表
    tokens_used   —— 已消耗 token
    tool_calls_used —— 已执行工具调用次数
    rounds        —— **已完成的 ReAct 轮次**（一次 LLM 调用 = 一轮）
    stop_reason   —— 终止原因（done / max_rounds / budget_exceeded / rejected_by_human ...）
    final_answer  —— 最终答案文本
    failure_counts —— {"tool_name::args_hash": 连续失败次数}（防死循环）
    citations     —— 最终答案中抽取到的引用（docs/01 §3.1 第 9 步）
    invalid_citations —— 引用了不存在 chunk 的引用
    session_id / thread_id / user_id —— 会话上下文

    retrieval_needed   —— 本轮是否需要走知识库检索（True/False）
    retrieval_rule     —— 命中的判定规则名（如 identity_question / force_factual_signal）
    retrieval_reason   —— 人类可读的理由（进 SSE / 前端）
    retrieval_policy_version —— 判定策略版本（如 rp_v1.0）
    reasoning_summary  —— **模型推理摘要**（前 1-2 句，非原始思维链）
    """

    messages: Annotated[List[Dict[str, Any]], add_messages]  # type: ignore[valid-type]
    approvals: Dict[str, Any]
    pending_hitl: List[Dict[str, Any]]
    tokens_used: int
    tool_calls_used: int
    #: **已完成的 ReAct 轮次**（2026-11-24 新增）。
    #:
    #: 为什么必须有这个字段（真实故障）：
    #:   ``BudgetGuard.rounds`` 只存在于**每次请求新建**的闭包对象里，
    #:   从不写回 graph state → API 层拿不到真实轮次，只能拿
    #:   「trace 条目数」当兜底 → 前端显示「9/8」这种越界数字。
    #:   现在由 agent 节点在 ``budget.charge_round()`` 之后**回写**，
    #:   API 快照就能读到**真实**轮次（trace 兜底只统计 llm/tool 两类条目）。
    rounds: int
    stop_reason: Optional[str]
    final_answer: Optional[str]
    failure_counts: Dict[str, int]
    citations: List[Dict[str, str]]
    invalid_citations: List[Dict[str, str]]
    session_id: Optional[str]
    thread_id: Optional[str]
    user_id: Optional[str]
    mode: Optional[str]
    plan: Optional[Dict[str, Any]]
    ask_rounds: int
    retrieval_needed: bool
    retrieval_rule: str
    retrieval_reason: str
    retrieval_policy_version: str
    tool_call_counts: Dict[str, int]
    query_counts: Dict[str, int]
    #: **推理摘要**（截断后的前 1-2 句）——用于「决策轨迹」的「思考」段。
    #: ⚠️ 只放**摘要**，原始思维链不进 state、不进消息历史。
    reasoning_summary: str
    #: **审批快速通道标记（2026-11-24 新增，修复「审批太慢」）**
    #:
    #: 真实性能故障（实测）：审批续跑时，图拓扑是
    #: ``agent → tools → hitl → tools → agent``（见 build_graph 的
    #: ``builder.add_edge("hitl", "tools")`` 与 ``_route_after_tools``）。
    #: 也就是说**批准之后还要再跑一次 LLM**，只为把工具结果改写成一段散文。
    #: 实测：``/chat``→pending 0.45~1.10 s，``/approve``→done 1.06~2.33 s，
    #: 后者**100% 是 LLM 时间** → 用户点一下「批准」要等一秒多。
    #:
    #: 现在：**当被批准的工具本身就是终态动作**（写工具一次执行即产出
    #: 完整、自描述的结果 JSON，例如 ws_write_file 返回
    #: ``{"status":"applied","path":...,"change_id":...,"undo_id":...}``），
    #: 就**跳过第二次 LLM 调用**，直接用工具结果作为最终答案。
    #:
    #: ⚠️ **必须是「按意图」而非「按启发式猜测」**：
    #:   只有当本轮**待审批项恰好 1 个**、且 agent 那条消息里**只有这一个**
    #:   工具调用（没有"写完再校验"这类多步计划）时才置位。
    #:   多步计划（写 2 个文件、再读回来校验）**绝不能**走这条捷径。
    approved_result_ready: bool
    #: 快速通道下工具产出的**完整结果**（JSON 兼容的 dict）。
    approved_result: Dict[str, Any]
    #: 快速通道命中的**工具名**（如 "ws_write_file"），用于拼最终答案。
    approved_result_tool: str
    #: 本轮挂起审批时的**待审批项数量**（用于判定「是否唯一动作」）。
    pending_hitl_count: int
    #: 本轮挂起审批时的**待审批工具名列表**（顺序与 pending_hitl 一致）。
    pending_hitl_tools: List[str]
    #: **LLM 轮次耗时（2026-11-22 新增，可观测性）**：
    #: 本轮模型调用的真实耗时（ms）与起止时刻（Unix 秒）。
    #: 瀑布图据此画出「LLM 调用」这一步的时间条 —— 没有它，
    #: 时间轴上只剩工具调用，模型思考时间（往往是大头）完全不可见。
    llm_duration_ms: int
    llm_started_at: float
    llm_ended_at: float


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------
def _args_key(tool_name: str, args: Dict[str, Any]) -> str:
    """同一工具同一参数的稳定标识（用于连续失败计数）。"""
    try:
        blob = json.dumps(args or {}, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        blob = str(args)
    return f"{tool_name}::{hash(blob) & 0xFFFFFFFF:08x}"


#: **写类工具**清单（按目标路径计数，而不是按工具名计数）。
#:
#: 为什么需要（2026-11-23 修复）：``MAX_SAME_TOOL_CALLS`` 的语义是
#: 「同一轮内不要**重复做同一件事**」（prompts/system.md 规则 9），
#: 最初只想拦"换个查询词再检索一遍"的空转，因此按**工具名**计数。
#: 但写类工具按工具名计数会**误伤正常任务**：
#: 用户让 agent "在 lz/ 下写 Tensor 与自动微分的代码和教程"，
#: agent 需要写 ``demo.py`` + ``tutorial.md`` 两个文件，
#: 第 3 个文件就被"同名工具已调用 2 次"拦下 → **代码写不进去**。
#:
#: 写多个**不同目标**的文件不是"重复做同一件事"。
#: 因此写类工具改为按 **path** 计数：同一个路径写 2 次以上才拦。
_WRITE_TOOLS_BY_PATH = ("ws_write_file", "ws_edit_file")

#: **「一次执行即产出完整结果」的工具**清单（2026-11-24 新增，审批快速通道）。
#:
#: 为什么需要这张清单（真实性能故障）：
#:   审批续跑的图拓扑是 ``agent → tools → hitl → tools → agent``，
#:   批准后**还要再跑一次 LLM**，只为把工具结果改写成一段散文。
#:   实测这一次 LLM 要 1.06~2.33 s，占整个审批耗时的 **100%**。
#:
#:   但 ``ws_write_file`` / ``ws_edit_file`` 返回的结果**本身就是完整的**：
#:   ``{"status":"applied","path":"...","change_id":"...","undo_id":"...",
#:     "size":N,"added":N,"removed":N,"note":"已落盘；如需回滚请调用 POST /workspace/undo。"}``
#:   —— 路径、大小、撤销 id 一应俱全，**不需要** LLM 来"总结"它。
#:
#: ⚠️ **只有明确列入本清单的工具才允许走快速通道** ——
#:   绝不能靠"看结果像不像 JSON"来猜测（那属于启发式猜测，会误判）。
#:   检索类工具（kb_search 等）产出的结果需要模型**再加工**，
#:   绝不在此清单内。
_SELF_DESCRIBING_WRITE_TOOLS = ("ws_write_file", "ws_edit_file")


def _is_self_describing_write(tool_name: str, payload: Any) -> bool:
    """判断工具结果是否「一次执行即完整、可以直接当最终答案」。

    **只认白名单工具**（:data:`_SELF_DESCRIBING_WRITE_TOOLS`），
    且必须真的返回了 ``status == "applied"`` 的结构化结果。
    失败结果（``code`` 开头的错误字典）**不算** —— 那种情况必须
    回到 agent 节点让模型解释为什么失败。
    """
    if str(tool_name or "") not in _SELF_DESCRIBING_WRITE_TOOLS:
        return False
    if not isinstance(payload, dict):
        return False
    if str(payload.get("status") or "") != "applied":
        return False
    #: 必须带 path，否则没法告诉用户文件写到哪里了
    return bool(str(payload.get("path") or "").strip())


def _fast_path_final_answer(tool_name: str, payload: Dict[str, Any]) -> str:
    """拼接「审批快速通道」的最终答案。

    结构：**一句中文结论 + 原始 JSON**（前端会把 JSON 渲染得很好看）。
    之所以把原始 JSON 也带上：它是**可核验的事实**（路径、change_id、
    undo_id），比 LLM 的散文更可靠，也方便用户直接拿去用。
    """
    path = str(payload.get("path") or "")
    size = int(payload.get("size", 0) or 0)
    added = int(payload.get("added", 0) or 0)
    removed = int(payload.get("removed", 0) or 0)
    undo_id = str(payload.get("undo_id") or "")
    change_id = str(payload.get("change_id") or "")
    lines = [
        f"已完成写入：`{path}`（{size} 字节，新增 {added} 行 / 删除 {removed} 行）。",
    ]
    if undo_id:
        lines.append(f"如需回滚，请调用 `POST /workspace/undo`（undo_id=`{undo_id}`）。")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(payload, ensure_ascii=False, indent=2))
    lines.append("```")
    return "\n".join(lines)


def _dup_count_key_for(tool_name: str, args: Dict[str, Any]) -> str:
    """计算「同轮重复调用」的计数键。

    · **写类工具**（``ws_write_file`` / ``ws_edit_file``）→ 按 **path**：
      写不同文件互不干扰；只有反复写**同一个路径**才算重复。
    · **其它工具** → 按**工具名**：保持原有"换个查询词也算同一件事"的语义。

    Args:
        tool_name: 工具名。
        args: 工具入参。

    Returns:
        计数键（写入 ``tool_call_counts`` 的 key）。
    """
    if tool_name in _WRITE_TOOLS_BY_PATH:
        path = str((args or {}).get("path", "") or "").strip()
        if path:
            return f"{tool_name}@path:{path}"
    return tool_name


#: **检索类工具**清单（单轮上限比默认更严，见
#: :data:`MAX_PER_ROUND_RETRIEVAL_CALLS`）。
_RETRIEVAL_TOOLS_FOR_ROUND_CAP = ("kb_search", "kb_read", "tp_lookup", "spec_extract")

#: **写类工具**清单（单轮上限比默认更严，见
#: :data:`MAX_PER_ROUND_WRITE_CALLS`）。
_WRITE_TOOLS_FOR_ROUND_CAP = ("ws_write_file", "ws_edit_file", "ws_apply_patch")


def per_round_cap_for(tool_name: str) -> int:
    """返回「某个工具在一轮内最多可执行几次」。

    分档理由（2026-11-24 新增）
    --------------------------
    · **检索类**（``kb_search`` / ``kb_read`` / …）→ 3：
      检索最贵（embedding + rerank + 大段 chunk 回灌上下文）。
      实测 gpt-oss 一轮连发 7 个并行 ``kb_search``，一轮就把上下文烧穿。
    · **写类**（``ws_write_file`` / ``ws_edit_file`` / …）→ 2：
      有副作用，且要人审批；一轮改两个文件足够。
    · **其它** → 4（``MAX_PER_ROUND_TOOL_CALLS``）：留足余地。

    Args:
        tool_name: 工具名。

    Returns:
        该工具的单轮执行次数上限（>= 1）。
    """
    name = str(tool_name or "")
    if name in _RETRIEVAL_TOOLS_FOR_ROUND_CAP:
        return MAX_PER_ROUND_RETRIEVAL_CALLS
    if name in _WRITE_TOOLS_FOR_ROUND_CAP:
        return MAX_PER_ROUND_WRITE_CALLS
    return MAX_PER_ROUND_TOOL_CALLS


#: 推理摘要的最大字符数（硬截断，避免把长推理塞进 SSE / 状态）
REASONING_SUMMARY_MAX = 220


def _summarize_reasoning(raw: Any) -> str:
    """把模型返回的推理压成**简短摘要**（前 1-2 句）。

    ⚠️ **合规红线**：本函数**不是**展示完整思维链的地方。
    它只做一件事：把 harmony（gpt-oss）返回的 ``reasoning_content``
    压成**一到两句**的概括，并硬截断到 ``REASONING_SUMMARY_MAX`` 字符。

    为什么这样做：
      1. 原始思维链属于模型的内部推理过程，既冗长又可能包含不适合展示的
         内部措辞；产品上用户真正需要的是「**这一步为什么这么做**」。
      2. 因此前端把它标注为「模型推理摘要（截断）」，而不是「思维链」。
      3. 模型未返回推理时返回空串 —— **绝不伪造**推理内容。

    Args:
        raw: ``reasoning_content`` 原文（可能为空 / None）。

    Returns:
        摘要文本；无可展示内容时为空串。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    parts: List[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in "。.!！\n":
            s = buf.strip()
            if s:
                parts.append(s)
            buf = ""
            if len(parts) >= 2:
                break
    if not parts and buf.strip():
        parts.append(buf.strip())
    summary = " ".join(parts).strip()
    if len(summary) > REASONING_SUMMARY_MAX:
        summary = summary[:REASONING_SUMMARY_MAX] + "…"
    return summary


def _last_user_question(messages: Sequence[Any]) -> str:
    """取最后一条用户消息的文本（检索必要性判定的输入）。

    找不到用户消息时返回空串 —— 此时判定器会走"空问题 → 默认按需检索"，
    也就是保守路径（宁可多检索一次，也不给错答案）。
    """
    for m in reversed(list(messages or [])):
        d = _as_dict_message(m)
        if d.get("role") == "user":
            content = d.get("content", "")
            if isinstance(content, str):
                return content
            return str(content or "")
    return ""


def _has_marker(messages: Sequence[Any], marker: str) -> bool:
    """消息列表里是否已经出现过某个标记（用于避免重复注入提示）。"""
    for m in list(messages or []):
        d = _as_dict_message(m)
        content = d.get("content", "")
        if isinstance(content, str) and marker in content:
            return True
    return False


def _normalize_query(query: str) -> str:
    """归一化检索查询词，用于**同义重排去重**。

    目的：让 ``"ChatGPT 模型"`` 与 ``"模型 ChatGPT"`` 归一成同一个 key，
    从而被识别为"同一件事检索了两次"（用户实测的重复检索场景）。

    规则：
        1. 去掉标点与多余空白；
        2. **按字符排序**后拼接 —— 这样词序不同的同一组词会得到相同的 key。
           （这是有意为之的"松散归一"：只用于**去重拦截**，
           不会改变实际发给检索引擎的查询词。）
    """
    s = str(query or "").lower().strip()
    if not s:
        return ""
    #: 去掉中英文标点与空白字符
    cleaned = re.sub(r"[\s,，。.;；:：!！?？、\"'“”‘’()（）\[\]【】<>《》\-_/\\|]+", "", s)
    if not cleaned:
        return ""
    return "".join(sorted(cleaned))


def _validate_answer_citations(
    answer: str, state: AgentState
) -> tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """校验最终答案里的引用是否存在（docs/01 §3.1 第 9 步）。

    合法 chunk_id 的来源：本次会话中**所有工具返回**里出现过的 chunk_id
    （``kb_search`` / ``kb_read`` / ``spec_extract`` 等）。凡是工具返回里
    出现过的 chunk_id 都可以被引用。

    Args:
        answer: 最终答案文本。
        state: 图状态。

    Returns:
        ``(citations, invalid_citations)``。工具未产出任何 chunk 时，
        引用全部视为无效（因为没有可核对的依据）。
    """
    citations = extract_citations(answer)
    if not citations:
        return [], []

    valid_ids: set[str] = set()
    for m in state.get("messages", []) or []:
        d = _as_dict_message(m)
        if d.get("role") != "tool":
            continue
        content = d.get("content", "")
        if not isinstance(content, str):
            continue
        try:
            payload = json.loads(content)
        except Exception:
            continue
        valid_ids.update(collect_chunk_ids(payload))

    invalid = validate_citations(citations, valid_ids)
    return citations, invalid


def _as_dict_message(msg: Any) -> Dict[str, Any]:
    """把 LangChain 消息对象转成 OpenAI 风格 dict（供 openai SDK 直接发送）。

    兼容两种 tool_calls 表达：
        · OpenAI 风格：{"id","type":"function","function":{"name","arguments"}}
        · LangChain 风格：{"name","args","id","type":"tool_call"}
    """
    if isinstance(msg, dict):
        return msg
    role = getattr(msg, "type", None) or getattr(msg, "role", "assistant")
    role = {"human": "user", "ai": "assistant", "tool": "tool"}.get(role, role)
    content = getattr(msg, "content", "")
    if not isinstance(content, str):
        try:
            content = json.dumps(content, ensure_ascii=False, default=str)
        except Exception:
            content = str(content)
    out: Dict[str, Any] = {"role": role, "content": content}
    if role == "assistant":
        tc = getattr(msg, "tool_calls", None)
        if tc:
            out["tool_calls"] = [_normalize_tool_call(t) for t in tc]
    if role == "tool":
        out["tool_call_id"] = getattr(msg, "tool_call_id", "")
        out["name"] = getattr(msg, "name", "")
    return out


def _normalize_tool_call(t: Any) -> Dict[str, Any]:
    """把 tool_call 统一成 OpenAI 风格。"""
    if isinstance(t, dict):
        if "function" in t:
            fn = t["function"] or {}
            return {
                "id": t.get("id", ""),
                "type": "function",
                "function": {
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments", "{}"),
                },
            }
        # LangChain 风格
        return {
            "id": t.get("id", ""),
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "arguments": json.dumps(t.get("args", {}), ensure_ascii=False, default=str),
            },
        }
    fn = getattr(t, "function", None)
    if fn is not None:
        return {
            "id": getattr(t, "id", ""),
            "type": "function",
            "function": {
                "name": getattr(fn, "name", ""),
                "arguments": getattr(fn, "arguments", "{}") or "{}",
            },
        }
    # LangChain ToolCall
    return {
        "id": getattr(t, "id", ""),
        "type": "function",
        "function": {
            "name": getattr(t, "name", ""),
            "arguments": json.dumps(getattr(t, "args", {}) or {}, ensure_ascii=False, default=str),
        },
    }


def _lc_tool_call(tc: Dict[str, Any]) -> Dict[str, Any]:
    """构造 LangChain 风格的 tool_call（{name, args, id, type}）。"""
    if "function" in tc:
        fn = tc["function"] or {}
        raw_args = fn.get("arguments", "{}")
        name = fn.get("name", "")
    else:
        raw_args = tc.get("arguments", "{}")
        name = tc.get("name", "")
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
    except Exception:
        args = {}
    return {"name": name, "args": args, "id": tc.get("id", ""), "type": "tool_call"}


def _to_langchain_messages(messages: Sequence[Any]) -> List[Any]:
    """构造 LangChain 消息对象；langgraph 不可用时退化为原始 dict。"""
    if not LANGGRAPH_AVAILABLE:
        return list(messages)
    try:
        from langchain_core.messages import (  # type: ignore
            AIMessage, HumanMessage, SystemMessage, ToolMessage,
        )
    except Exception:
        return list(messages)
    out: List[Any] = []
    for m in messages:
        if isinstance(m, (HumanMessage, AIMessage, SystemMessage, ToolMessage)):
            out.append(m)
            continue
        d = m if isinstance(m, dict) else _as_dict_message(m)
        role = d.get("role", "user")
        content = d.get("content", "")
        if role == "system":
            out.append(SystemMessage(content=content))
        elif role == "user":
            out.append(HumanMessage(content=content))
        elif role == "assistant":
            kwargs: Dict[str, Any] = {"content": content}
            if d.get("tool_calls"):
                kwargs["tool_calls"] = [_lc_tool_call(tc) for tc in d["tool_calls"]]
            out.append(AIMessage(**kwargs))
        elif role == "tool":
            #: **耗时透传（2026-11-22 新增）**：ToolMessage 没有 duration_ms 字段，
            #: 直接传会被丢掉。放进 ``additional_kwargs``（LangChain 会原样保留
            #: 并序列化），api 层 ``_msg_to_dict`` 再把它读出来回填进 trace。
            extra: Dict[str, Any] = {}
            if d.get("duration_ms") is not None:
                extra["duration_ms"] = d.get("duration_ms")
            out.append(ToolMessage(
                content=content,
                tool_call_id=d.get("tool_call_id", ""),
                name=d.get("name", ""),
                **({"additional_kwargs": extra} if extra else {}),
            ))
        else:
            out.append(HumanMessage(content=content))
    return out


# ----------------------------------------------------------------------
# 节点实现
# ----------------------------------------------------------------------
def make_agent_node(llm: Any, registry: Any, budget: BudgetGuard,
                    system_prompt: Optional[str] = None) -> Any:
    """构造 agent 节点：调用 LLM，决定下一步。

    · 调用前先过 BudgetGuard.check()
    · 归一化输出（reasoning_content 不入历史）
    · 有 tool_calls → 转到 tools 节点；否则产出 final_answer 并结束
    """

    def agent_node(state: AgentState) -> Dict[str, Any]:
        messages: List[Any] = list(state.get("messages", []))
        tokens_used = int(state.get("tokens_used", 0))
        tool_calls_used = int(state.get("tool_calls_used", 0))
        #: **真实轮次**（2026-11-24 新增）：从 state 读回上一次写入的值，
        #: 每次 LLM 调用成功后 +1 并写回 state（见 ``budget.charge_round()`` 之后）。
        #: 为什么必须回写：``BudgetGuard.rounds`` 只活在**每次请求新建**的闭包里，
        #: API 层看不到它 → 只能用 trace 长度兜底 → 前端出现「9/8」这种越界数字。
        rounds = int(state.get("rounds", 0) or 0)

        prompt_text = system_prompt or get_system_prompt()

        # ------------------------------------------------------------------
        # 检索必要性闸门（Task 2/3）
        # ------------------------------------------------------------------
        # 用户实测反馈：问"你是什么模型"这类通用问题时，系统仍强行调用 kb_search，
        # 浪费 20 秒并把答案搞成"未在知识库中找到"。
        # 这里在**下发工具 schema 之前**做一次判定：
        #   · 判定为"不需要检索" → 本轮**摘掉** kb_search/kb_read/tp_lookup/spec_extract，
        #     模型无从调用；其它工具（sql_query / 写类工具）照常保留（写类仍走 HITL）；
        #   · 判定为"需要检索" → 工具照常全量下发，严格性不变。
        from ..core.retrieval_policy import (  # noqa: PLC0415
            classify_retrieval_need, filter_retrieval_tools,
        )

        decision = classify_retrieval_need(_last_user_question(messages))
        retrieval_needed = decision.needed
        #: 判定结果写进 state，供 SSE / 前端观测
        policy_state: Dict[str, Any] = {
            "retrieval_needed": retrieval_needed,
            "retrieval_rule": decision.rule,
            "retrieval_reason": decision.reason,
            "retrieval_policy_version": decision.policy_version,
        }

        # 组装发给 LLM 的消息：system 在最前，其余为历史
        llm_messages: List[Dict[str, Any]] = [{"role": "system", "content": prompt_text}]
        for m in messages:
            llm_messages.append(_as_dict_message(m))

        # 估算本次 prompt token（**保守估算**，见 core.budget.estimate_tokens）
        # 为什么要换掉原来的「字符数 // 2」：
        #   · 中文 1 字 ≈ 0.7 token，//2 严重低估；
        #   · 工具结果是 JSON + 中文混排，低估更明显；
        #   · 低估会让请求超过 llama-server 的 -c 32768 窗口 → 400 BadRequest，
        #     任务直接变 exception（而不是优雅收尾）。
        # 现在用按字符类别分别计数的启发式，宁可早停一轮也不超窗。
        try:
            budget.check(est_tokens=estimate_messages_tokens(llm_messages))
        except BudgetExceeded as exc:
            return {
                "stop_reason": f"budget_exceeded:{exc.reason}",
                "final_answer": f"任务因预算/超时限制终止：{exc.detail}",
                "tokens_used": tokens_used,
                "tool_calls_used": tool_calls_used,
                "rounds": rounds,
                **policy_state,
            }

        # ------------------------------------------------------------------
        # **轮次计费只在这里发生（2026-11-24 修复：轮次被双倍消耗）**
        # ------------------------------------------------------------------
        # 此前 tools 节点也会调 ``budget.check()``，而 ``check()`` 里
        # ``self.rounds >= self.max_rounds`` 的判定是**基于已计费轮次**的。
        # 于是 agent→tools 一个来回里 rounds 被推进 2 次，
        # ``max_rounds=8`` 实际只够用 **4 轮** —— 用户抱怨「没几步就停了」。
        # 现在语义改为：**轮次只在 agent 节点计费**（一次 LLM 调用 = 一轮），
        # tools 节点只检查工具调用数 / 总时长 / 上下文窗口。
        budget.charge_round()
        #: 把**真实**轮次回写 state，供 API 快照 / 前端预算条读取
        #: （否则 API 只能拿 trace 长度兜底 → 出现「9/8」这种越界显示）。
        rounds = int(budget.rounds)

        # ---- Ask 模式已达工具轮次上限：**不下发工具 schema**，模型无从调用工具 ----
        from ..core.modes import (  # noqa: PLC0415
            ASK_MAX_TOOL_ROUNDS, normalize_mode,
        )

        mode_name = normalize_mode(state.get("mode") or "agent")
        ask_capped = (mode_name == "ask"
                      and int(state.get("ask_rounds", 0) or 0) >= ASK_MAX_TOOL_ROUNDS)
        if ask_capped:
            llm_messages = list(llm_messages) + [{
                "role": "user",
                "content": ("[Ask 模式] 工具调用轮次已达上限，"
                            "请直接给出最终答案，不要再请求工具。"),
            }]

        # ---- 按检索必要性过滤工具 schema ----
        tools_schema: Optional[List[Dict[str, Any]]] = None
        if registry is not None and not ask_capped:
            all_names = [s.name for s in registry.list_specs()]
            allowed = filter_retrieval_tools(all_names, retrieval_needed)
            if allowed:
                tools_schema = registry.openai_schemas(only=allowed)
            else:
                # 一个工具都不剩 → 完全不下发 tools（模型只能纯文本作答）
                tools_schema = None

        # ---- 判定为"不需要检索"时，明确告诉模型"本轮无需检索" ----
        # ⚠️ 只注入**一次**：多轮工具循环（agent→tools→agent）时反复注入
        #    同一条 user 消息会污染历史，也会让"最后一条用户消息"不再是原问题。
        if not retrieval_needed and not _has_marker(llm_messages, "本轮无需检索"):
            llm_messages = list(llm_messages) + [{
                "role": "user",
                "content": (
                    "[本轮无需检索] 系统判定该问题**不依赖知识库**"
                    f"（依据：{decision.reason}）。"
                    "本轮未提供任何检索类工具，请**直接基于自身能力作答**，"
                    "**不要**输出\"未在知识库中找到\"这类结论。"
                ),
            }]

        #: **LLM 轮次耗时（2026-11-22 新增，可观测性）**：
        #: 瀑布图 / 时序图需要「LLM 调用」这一步的**真实开始/结束时刻与耗时**，
        #: 否则时间轴上只剩工具调用，模型的思考时间（往往是大头）完全不可见。
        #: 这里用单调时钟测一次，写进 state，API 层回填进 trace 条目。
        #: 这是**附加字段**，老客户端按字段名读取不受影响。

        # ------------------------------------------------------------------
        # **上下文窗口熔断（2026-11-22 新增）** —— 含工具 schema 的最终估算
        # ------------------------------------------------------------------
        # 上面的 check 只估了「system + 历史消息」。但真正发给模型的一次请求
        # 还包含**工具 schema**（本项目 18 个工具，JSON 描述相当可观）。
        # 必须把它算进去再判一次，否则「历史没超窗 + schema 很肥」时仍会
        # 被 llama-server 以 400 拒绝。
        # 触发时以 budget_exceeded:MAX_CONTEXT **优雅收尾**（有 final_answer），
        # 而不是让 BadRequestError 把整个请求变成 exception。
        if tools_schema:
            try:
                est_final = (estimate_messages_tokens(llm_messages)
                             + estimate_tokens(json.dumps(
                                 tools_schema, ensure_ascii=False)))
                budget.check(est_tokens=est_final)
            except BudgetExceeded as exc:
                return {
                    "stop_reason": f"budget_exceeded:{exc.reason}",
                    "final_answer": (
                        f"任务因上下文窗口限制终止：{exc.detail}。"
                        "请缩小问题范围，或先压缩历史轮次再试。"),
                    "tokens_used": tokens_used,
                    "tool_calls_used": tool_calls_used,
                    "rounds": rounds,
                    **policy_state,
                }

        llm_started_at = time.time()
        llm_t0 = time.perf_counter()
        try:
            resp = llm.chat_sync(llm_messages, tools=tools_schema)
        except TypeError:
            # 兼容只接受 messages 的简单 llm 实现
            resp = llm.chat_sync(llm_messages)
        except Exception as exc:
            return {
                "stop_reason": f"llm_error:{type(exc).__name__}",
                "final_answer": f"模型调用失败：{type(exc).__name__}: {exc}",
                "tokens_used": tokens_used,
                "tool_calls_used": tool_calls_used,
                "rounds": rounds,
                **policy_state,
            }
        llm_duration_ms = int((time.perf_counter() - llm_t0) * 1000)
        llm_ended_at = time.time()
        usage = resp.get("usage", {}) or {}
        #: **上下文估算自检（2026-11-22 新增）**：
        #: 打开 ``AGENT_DEBUG_TOKENS=1`` 时，把「本地估算」与「模型端真实
        #: prompt token」打到 stderr，用来校准 :func:`estimate_tokens`。
        #: 生产环境不开，零开销（一行 env 判断）。
        if _DEBUG_TOKENS:
            import sys as _sys
            _real = int(usage.get("prompt", 0) or 0)
            _est = (estimate_messages_tokens(llm_messages)
                    + (estimate_tokens(json.dumps(tools_schema, ensure_ascii=False))
                       if tools_schema else 0))
            print(f"[tok] est={_est} real_prompt={_real} msgs={len(llm_messages)}",
                  file=_sys.stderr, flush=True)
        tokens_used += int(usage.get("total", 0) or 0)
        try:
            budget.charge_tokens(prompt=int(usage.get("prompt", 0) or 0),
                                 completion=int(usage.get("completion", 0) or 0))
        except BudgetExceeded as exc:
            return {
                "stop_reason": f"budget_exceeded:{exc.reason}",
                "final_answer": f"任务因 token 预算终止：{exc.detail}",
                "tokens_used": tokens_used,
                "tool_calls_used": tool_calls_used,
                "rounds": rounds,
                **policy_state,
            }

        tool_calls = resp.get("tool_calls", []) or []
        content = resp.get("content", "") or ""
        #: **模型推理摘要**（harmony / gpt-oss 会返回 reasoning_content）
        #:
        #: ⚠️ **不写入对话历史**（见下方 assistant_msg）——原始思维链不进入
        #:    消息序列，也不发给模型。这里只把**摘要**放进 state，
        #:    供 SSE 的 ``step`` 事件推给前端做「决策轨迹」的「思考」段。
        #:    摘要由 ``_summarize_reasoning`` 压成前 1-2 句并硬截断。
        reasoning_summary = _summarize_reasoning(resp.get("reasoning_content"))

        # 追加 assistant 消息；**reasoning_content 不写入历史**
        assistant_msg: Dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            # ------------------------------------------------------------------
            # **并行工具调用数上限（2026-11-22 修复）**
            # ------------------------------------------------------------------
            # 实测：gpt-oss 在检索类问题上偶发一次性返回**数千个** tool_calls。
            # 若原样写进历史：
            #   · 消息序列体积暴涨 → 下一轮请求超过 llama-server 的
            #     ``-c 32768`` 上下文窗口 → BadRequestError；
            #   · tools 节点逐条遍历 → tool_calls_used 累加到 5003；
            #   · 随后 agent↔tools 死循环 → GraphRecursionError。
            # 所以**在这里（源头）截断**：历史里最多留 MAX_PARALLEL_TOOL_CALLS
            # 条 tool_calls，tools 节点也就最多处理这么多条 —— 双重保险。
            # 被丢弃的调用会在 tools 节点收到一条 "TOO_MANY_PARALLEL_CALLS"
            # 的 tool 消息（保证 tool_calls 与 tool 消息一一对应，
            # 否则 OpenAI 兼容端点会拒绝整个消息序列）。
            kept = tool_calls[:MAX_PARALLEL_TOOL_CALLS]
            dropped = len(tool_calls) - len(kept)
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": tc.get("name", ""),
                        "arguments": json.dumps(tc.get("args", {}), ensure_ascii=False),
                    },
                }
                for tc in kept
            ]
            if dropped:
                assistant_msg["content"] = (
                    (content or "")
                    + f"\n[本次请求的 {dropped} 个额外并行工具调用已被系统丢弃："
                      f"单轮并行调用上限为 {MAX_PARALLEL_TOOL_CALLS} 个]"
                )
                # 记录到 state 的失败计数里，便于观测面板展示
                tool_calls = kept
        new_messages: List[Any] = [assistant_msg]
        if LANGGRAPH_AVAILABLE:
            new_messages = _to_langchain_messages(new_messages)

        if not tool_calls:
            # ---- docs/01 §3.1 第 9 步：引用块存在性校验 ----
            # 早期实现里这一步只在 Retriever 生成路径上有，Agent ReAct
            # 路径完全没有，导致引用幻觉率对 Agent 路径恒为 0（假绿）。
            citations, invalid = _validate_answer_citations(content, state)
            # ⚠️ **空答案兜底**（2026-11-22 新增）：
            # 模型偶尔会返回既无 content 也无 tool_calls 的空响应。
            # 旧实现直接把空串当 final_answer → 前端显示一片空白，
            # 用户完全看不出发生了什么。这里给一句明确的说明，
            # 并把 stop_reason 标成 empty_response（**不冒充 done**）。
            empty_answer = not (content or "").strip()
            return {
                "messages": new_messages,
                "tokens_used": tokens_used,
                "tool_calls_used": tool_calls_used,
                "rounds": rounds,
                "stop_reason": "empty_response" if empty_answer else "done",
                "final_answer": (
                    "模型返回了空响应（既无正文也无工具调用），任务已中止。"
                    "请换一种问法重试。" if empty_answer else content),
                "citations": citations,
                "invalid_citations": invalid,
                "reasoning_summary": reasoning_summary,
                # **可观测性（2026-11-22 新增）**：本轮 LLM 调用的真实耗时与时刻。
                "llm_duration_ms": llm_duration_ms,
                "llm_started_at": llm_started_at,
                "llm_ended_at": llm_ended_at,
                **policy_state,
            }

        return {
            "messages": new_messages,
            "tokens_used": tokens_used,
            "tool_calls_used": tool_calls_used,
            "rounds": rounds,
            "stop_reason": None,
            "reasoning_summary": reasoning_summary,
            # **可观测性（2026-11-22 新增）**：本轮 LLM 调用的真实耗时与时刻。
            "llm_duration_ms": llm_duration_ms,
            "llm_started_at": llm_started_at,
            "llm_ended_at": llm_ended_at,
            **policy_state,
        }

    return agent_node


def _pending_metadata(spec: Any, tool_name: str,
                      args: Dict[str, Any]) -> Dict[str, Any]:
    """构造待审批项的 metadata（**向后兼容**：只做增量扩展）。

    对工作区写工具（``ws_write_file`` / ``ws_edit_file``）额外计算
    **行级 diff**，让审批卡片能真正看清「改了什么」。

    非工作区工具的 metadata 原样返回 —— 既有工具的卡片完全不变。
    """
    metadata: Dict[str, Any] = dict(getattr(spec, "metadata", {}) or {})
    if tool_name not in ("ws_write_file", "ws_edit_file"):
        return metadata
    try:
        from ..core.workspace import get_workspace  # noqa: PLC0415

        ws = get_workspace()
        diff = ws.preview_tool_diff(tool_name, args or {})
        err = ws.preview_tool_error(tool_name, args or {})
    except Exception:
        diff = None
        err = {}
    if diff:
        metadata["diff"] = diff
        metadata["diff_path"] = (diff or {}).get("path", "")
    if err:
        # 预演失败 → 把原因带给审批人（如 old_text 不唯一）
        metadata["preflight_error"] = err
    return metadata


def make_tools_node(registry: Any, budget: BudgetGuard, gate: HITLGate,
                    *, mode: str = "agent") -> Any:
    """构造 tools 节点（两段式）。

    第一段（gate）：检查每个 tool_call 是否需要 HITL。
        需要但未审批 → 放入 pending_hitl，跳 hitl 节点，**不产生任何 tool 消息**。
    第二段（execute）：全部审批通过 → 真正执行工具，产生 tool 消息。

    **Ask 模式下的额外约束**（``mode="ask"``）：
        · 只允许 ``READ_ONLY`` / ``COMPUTE`` 权限的工具；
          写类工具**直接被拦截**，返回 ``MODE_FORBIDDEN_TOOL`` 错误消息，
          工具**根本不会被执行**（不是执行完再拒绝）。
        · 最多 ``ASK_MAX_TOOL_ROUNDS`` 轮工具调用，超出后直接终止并让模型作答。
    """

    def tools_node(state: AgentState) -> Dict[str, Any]:
        from ..core.modes import (  # noqa: PLC0415
            ASK_ALLOWED_PERMISSIONS, ASK_MAX_TOOL_ROUNDS, normalize_mode,
        )

        mode_name = normalize_mode(state.get("mode") or mode)
        ask_mode = (mode_name == "ask")

        messages: List[Any] = list(state.get("messages", []))
        approvals: Dict[str, Any] = dict(state.get("approvals", {}) or {})
        pending: List[Dict[str, Any]] = list(state.get("pending_hitl", []) or [])
        tokens_used = int(state.get("tokens_used", 0))
        tool_calls_used = int(state.get("tool_calls_used", 0))
        failures: Dict[str, int] = dict(state.get("failure_counts", {}) or {})
        #: 同一轮内"同名工具"的累计调用次数（跨 agent↔tools 循环累积）
        #: ⚠️ 必须在 **Ask 模式的早退分支之前**定义 —— 那些分支也会回写计数。
        tool_name_counts: Dict[str, int] = dict(state.get("tool_call_counts", {}) or {})
        #: 同一轮内"归一化查询词"的累计次数（kb_search 专用，防止换个词再查一遍）
        query_counts: Dict[str, int] = dict(state.get("query_counts", {}) or {})
        session_id = state.get("session_id")
        thread_id = state.get("thread_id")
        user_id = state.get("user_id")
        #: Ask 模式的工具轮次计数（每次进入 tools 节点 +1）
        ask_rounds = int(state.get("ask_rounds", 0) or 0)
        #: 本轮检索必要性判定结果（由 agent 节点写入）
        retrieval_needed = bool(state.get("retrieval_needed", True))
        retrieval_reason = str(state.get("retrieval_reason", "") or "")

        # 找到最后一条 assistant 消息里的 tool_calls
        last_assistant: Optional[Dict[str, Any]] = None
        for m in reversed(messages):
            d = _as_dict_message(m)
            if d.get("role") == "assistant" and d.get("tool_calls"):
                last_assistant = d
                break
        if last_assistant is None:
            return {"stop_reason": "done", "final_answer": state.get("final_answer")}

        raw_calls = last_assistant.get("tool_calls", []) or []

        # ------------------------------------------------------------------
        # 兜底闸门：本轮判定为"无需检索"，但模型仍尝试调检索工具
        # ------------------------------------------------------------------
        # 正常路径下 agent 节点已经**不下发**检索工具的 schema，模型调不出来。
        # 这里是**兜底**：模型可能凭训练里的记忆硬编一个工具名，或上游
        # 复用了带 tool_calls 的历史消息。此时必须拦截并给出**明确且不误导**的
        # 错误码 —— 绝不能返回"未在知识库中找到"这种误导性结论。
        if not retrieval_needed:
            from ..core.retrieval_policy import RETRIEVAL_TOOL_NAMES  # noqa: PLC0415

            ret_names = set(RETRIEVAL_TOOL_NAMES)
            offending = [
                tc for tc in raw_calls
                if ((tc.get("function", {}) or {}).get("name", "") or tc.get("name", "")) in ret_names
            ]
            if offending:
                new_messages = list(messages)
                for tc in offending:
                    fn = tc.get("function", {}) or {}
                    tname = fn.get("name", "") or tc.get("name", "")
                    new_messages.append(_tool_message(
                        tc.get("id", ""), tname,
                        {"code": "RETRIEVAL_NOT_NEEDED",
                         "message": (
                             f"本轮判定该问题**不依赖知识库**"
                             f"（依据：{retrieval_reason or '通用/常识类问题'}），"
                             f"因此不提供检索工具 '{tname}'。"
                         ),
                         "retryable": False,
                         "hint": (
                             "该问题不依赖知识库，请**直接基于自身能力回答**。"
                             "**不要**改写成\"未在知识库中找到\"，"
                             "也不要用其它检索词重试。"
                         )}))
                new_messages.append({
                    "role": "user",
                    "content": ("[本轮无需检索] 该问题不依赖知识库，"
                                "请**直接给出答案**，不要再调用任何检索工具。"),
                })
                if LANGGRAPH_AVAILABLE:
                    new_messages = _to_langchain_messages(new_messages)
                return {
                    "messages": new_messages,
                    "pending_hitl": [],
                    "approvals": approvals,
                    "tokens_used": tokens_used,
                    "tool_calls_used": tool_calls_used,
                    "failure_counts": failures,
                    # 计数一并回写，避免这条路径把计数丢掉。
                    "tool_call_counts": tool_name_counts,
                    "query_counts": query_counts,
                    "ask_rounds": ask_rounds + 1,
                    "stop_reason": None,
                }

        # ---- Ask 模式：轮次上限 ----
        # 达到上限后**不再执行工具**，而是提示模型基于已有信息直接作答
        # （而不是直接返回一个占位文案，那等于没回答）。
        if ask_mode and ask_rounds >= ASK_MAX_TOOL_ROUNDS:
            note = {
                "role": "user",
                "content": (
                    "[Ask 模式] 工具调用轮次已达上限，请**不要再调用任何工具**，"
                    "直接基于已有信息给出最终答案。"
                ),
            }
            new_messages = list(messages)
            # 关键：assistant.tool_calls 必须逐条跟一条 tool 消息，
            # 否则 OpenAI 兼容端点会因非法消息序列返回空内容。
            for tc in raw_calls:
                fn = tc.get("function", {}) or {}
                tname = fn.get("name", "")
                new_messages.append(_tool_message(
                    tc.get("id", ""), tname,
                    {"code": "ASK_MAX_ROUNDS",
                     "message": "Ask 模式已达到工具调用轮次上限，本次调用未执行。",
                     "retryable": False,
                     "hint": "请基于已有信息直接作答。"}))
            new_messages.append(note)
            if LANGGRAPH_AVAILABLE:
                new_messages = _to_langchain_messages(new_messages)
            return {
                "messages": new_messages,
                "pending_hitl": [],
                "approvals": approvals,
                "ask_rounds": ask_rounds,
                "stop_reason": None,
            }

        # ---- 第一段：HITL 闸门检查 ----
        need_approval: List[PendingApproval] = []
        for tc in raw_calls:
            fn = tc.get("function", {}) or {}
            tname = fn.get("name", "")
            try:
                targs = json.loads(fn.get("arguments", "{}") or "{}")
            except Exception:
                targs = {}
            spec = registry.get(tname) if registry is not None else None
            if spec is None or not spec.needs_hitl:
                continue
            cid = tc.get("id", "")
            if cid in approvals:
                continue
            # 会话级授权 → 直接记为已批准
            if gate.has_session_grant(session_id or "", tname):
                approvals[cid] = ApprovalDecision(
                    tool_call_id=cid, approved=True,
                    actor="session-grant", reason="会话级授权",
                ).to_dict()
                continue
            need_approval.append(PendingApproval(
                tool_call_id=cid,
                tool_name=tname,
                args=targs,
                permission=spec.permission,
                description=spec.metadata.get("description", spec.description),
                impact=spec.metadata.get("impact", ""),
                reversible=bool(spec.metadata.get("reversible", spec.idempotent)),
                metadata=_pending_metadata(spec, tname, targs),
            ))

        if need_approval:
            # 只挂起，不执行任何工具 → 不产生 tool 消息
            return {
                "pending_hitl": [p.to_card() for p in need_approval],
                "approvals": approvals,
                "stop_reason": "pending_approval",
                # ---- 记录「本轮挂起时的审批规模」（2026-11-24 新增）----
                # 审批续跑时（resume 后再进 tools 节点）``pending_hitl`` 已被清空，
                # 这里就没有信息判断「被批准的动作是不是**唯一**的动作」了。
                # 所以在**挂起的那一刻**先把规模记进 state：
                # · pending_hitl_count == 1 → 本轮只有**一个**待审批动作；
                # · 再结合 agent 那条消息里的 tool_calls 总数 == 1
                #   → 才能判定这是「一次执行即终态」的单步意图。
                "pending_hitl_count": len(need_approval),
                "pending_hitl_tools": [p.tool_name for p in need_approval],
            }

        # ---- 第二段：真正执行工具 ----
        new_tool_messages: List[Any] = []
        # 注：``tool_name_counts`` / ``query_counts`` 已在函数开头定义
        # （Ask 模式的早退分支也要回写它们）。

        # ------------------------------------------------------------------
        # 单轮并行工具调用数上限（2026-11-22 修复死循环）—— **兜底**
        # ------------------------------------------------------------------
        # 上游（agent 节点）已在**写入历史前**把 tool_calls 截断到
        # MAX_PARALLEL_TOOL_CALLS 条，所以正常情况下这里不会触发。
        # 这里再加一道，是防"上游漏网"（例如历史会话被外部灌入畸形数据）。
        # 实测触发过的两个故障：
        #   · tool_calls_used 一路累加到 5003 → agent 熔断后
        #     _route_after_agent 又路由回 tools → 死循环（GraphRecursionError）；
        #   · 数千条 tool_calls 写进历史 → 下一轮请求超过 llama-server 的
        #     ``-c 32768`` 上下文窗口 → BadRequestError。
        max_parallel = min(int(budget.max_tool_calls) if budget else 24,
                           MAX_PARALLEL_TOOL_CALLS)
        truncated = 0
        all_calls = raw_calls
        if len(raw_calls) > max_parallel:
            truncated = len(raw_calls) - max_parallel
            raw_calls = raw_calls[:max_parallel]

        # ------------------------------------------------------------------
        # **单工具单轮上限（2026-11-24 新增）** —— 防止一轮烧完整轮预算
        # ------------------------------------------------------------------
        # 为什么需要（真实故障）：
        #   ``MAX_PARALLEL_TOOL_CALLS`` 只限制「一轮一共多少个调用」，
        #   **不限制「同一个工具占几个」**。实测 gpt-oss 一轮连发
        #   **7 个并行 ``kb_search``**，每个都要跑 embedding+rerank，
        #   每个结果都塞回上下文 —— 一轮就把上下文窗口烧穿，
        #   下一轮请求被 llama-server 以 400 拒绝，任务半途而废。
        #   表现就是用户说的「没几步就停了」。
        #
        # 处理方式（**丢弃而非报错**）：
        #   超出单轮上限的调用**不执行**，但仍然回一条 tool 消息
        #   （否则 assistant.tool_calls 没有对应 tool 消息，
        #   OpenAI 兼容端点会拒绝整个消息序列）。
        #   这样 agent 还能用剩下的调用继续推进，**整轮不会失败**。
        per_round_counts: Dict[str, int] = {}
        kept_calls: List[Any] = []
        dropped_calls: List[Any] = []
        for tc in raw_calls:
            fn = tc.get("function", {}) or {}
            tname = fn.get("name", "") or tc.get("name", "")
            cap = per_round_cap_for(tname)
            if per_round_counts.get(tname, 0) >= cap:
                dropped_calls.append(tc)
                continue
            per_round_counts[tname] = per_round_counts.get(tname, 0) + 1
            kept_calls.append(tc)
        raw_calls = kept_calls

        #: **本轮实际执行成功、且结果"一次执行即完整"的写工具**
        #: （审批快速通道用，见 :data:`_SELF_DESCRIBING_WRITE_TOOLS`）。
        #: 只有**唯一**一个的时候才允许走快速通道（多步计划不能走）。
        self_describing_write: Optional[Dict[str, Any]] = None
        #: 本轮**实际执行**的工具调用总数（用于判定"是不是只有这一步"）。
        executed_call_count = 0

        for tc in raw_calls:
            fn = tc.get("function", {}) or {}
            tname = fn.get("name", "")
            cid = tc.get("id", "")
            try:
                targs = json.loads(fn.get("arguments", "{}") or "{}")
            except Exception:
                targs = {}

            # ------------------------------------------------------------------
            # 防重复：同一轮内同名工具调用次数 > 2 → 拦截
            # ------------------------------------------------------------------
            # 用户实测：问"你是什么模型"时 kb_search 被调了两次
            # （query 分别是"ChatGPT 模型"和"模型 ChatGPT"）——参数不同，
            # 原来的 `failures.get(key) >= 2`（同参数失败计数）拦不住。
            # 这里按**工具名**计数，与参数无关。
            #
            # **例外：写类工具按「路径」区分（2026-11-23 修复）**
            #   真实故障：用户让 agent 在 ``lz/`` 下写 3 个文件，
            #   写到第 3 个时就被"同名工具已调用 2 次"拦下，
            #   表现为"代码都写不进去"。
            #   但"写 3 个**不同**文件"根本不是"重复做同一件事"——
            #   规则 9 要防的是"换个词再查一遍"这种空转，
            #   不是"写多个不同目标文件"。
            #   因此写类工具改为按 **path** 计数：同一个路径写 2 次以上才拦。
            _dup_count_key = _dup_count_key_for(tname, targs)
            _dup_count = tool_name_counts.get(_dup_count_key, 0)
            #: 写类工具按路径计数，阈值放宽到 3（"先建再改"是正常流程）；
            #: 其余工具沿用工具名计数，阈值 2（"换个词再查一遍"要拦）。
            _dup_limit = (MAX_SAME_WRITE_PATH_CALLS
                          if tname in _WRITE_TOOLS_BY_PATH
                          else MAX_SAME_TOOL_CALLS)
            if _dup_count >= _dup_limit:
                err_payload = {
                    "code": "DUPLICATE_TOOL_CALL",
                    "message": (f"工具 '{tname}' 在本轮已被调用 "
                                f"{_dup_count} 次，已拦截本次重复调用"),
                    "retryable": False,
                    "hint": ("不要用不同的查询词重复检索同一件事。"
                             "请基于已有检索结果**换一种方式或直接作答**。"),
                }
                # 注意：这里也计入 tool_calls_used（被拦截的调用同样消耗预算），
                # 但**不再无限累加** —— 上面的 max_parallel 截断已把单轮
                # 并行调用数限制在 32 以内，不会再出现累加到 5003 的情况。
                tool_calls_used += 1
                new_tool_messages.append(_tool_message(cid, tname, err_payload))
                continue

            # ---- kb_search 查询词归一化去重（"ChatGPT 模型" == "模型 ChatGPT"）----
            if tname == "kb_search":
                norm_q = _normalize_query(str(targs.get("query", "") or ""))
                if norm_q:
                    if query_counts.get(norm_q, 0) >= MAX_SAME_QUERY_CALLS:
                        err_payload = {
                            "code": "DUPLICATE_QUERY",
                            "message": (f"查询词 '{norm_q}'（归一化后）在本轮已检索 "
                                        f"{query_counts.get(norm_q)} 次，已拦截本次重复检索"),
                            "retryable": False,
                            "hint": ("本次查询与之前的检索等价，"
                                     "请基于已有结果**换一种方式或直接作答**。"),
                        }
                        tool_calls_used += 1
                        new_tool_messages.append(_tool_message(cid, tname, err_payload))
                        continue
                    query_counts[norm_q] = query_counts.get(norm_q, 0) + 1

            # ---- Ask 模式：只读白名单（写类工具**不执行**，直接拦截）----
            if ask_mode:
                spec = registry.get(tname) if registry is not None else None
                perm = str(getattr(spec, "permission", "") or "").upper()
                if spec is None or perm not in ASK_ALLOWED_PERMISSIONS:
                    err_payload = {
                        "code": "MODE_FORBIDDEN_TOOL",
                        "message": f"Ask 模式为只读问答模式，禁止调用工具 '{tname}'"
                                    f"（权限 {perm or 'UNKNOWN'}）",
                        "retryable": False,
                        "hint": "请改用只读检索工具（READ_ONLY / COMPUTE），"
                                "或直接基于已有信息回答用户。",
                    }
                    tool_calls_used += 1
                    new_tool_messages.append(_tool_message(cid, tname, err_payload))
                    continue

            # 审批被拒 → 不执行
            decision = approvals.get(cid)
            if decision is not None and not decision.get("approved", False):
                err_payload = {
                    "code": "REJECTED_BY_HUMAN",
                    "message": f"人工审批拒绝了工具 '{tname}' 的调用"
                                + (f"（原因：{decision.get('reason')}）" if decision.get("reason") else ""),
                    "retryable": False,
                    "hint": "请不要再调用该工具，向用户说明未获批准的原因，并询问是否需要调整方案。",
                }
                tool_calls_used += 1
                new_tool_messages.append(_tool_message(cid, tname, err_payload))
                continue

            key = _args_key(tname, targs)
            if failures.get(key, 0) >= 2:
                # 同一工具同一参数连续失败 2 次 → 终止（docs/01 §3.2）
                err_payload = {
                    "code": "REPEATED_FAILURE",
                    "message": f"工具 '{tname}' 以相同参数连续失败 "
                               f"{failures.get(key)} 次，已终止该调用路径",
                    "retryable": False,
                    "hint": "请改用其它工具、调整参数策略，或直接向用户说明当前无法完成。",
                }
                new_tool_messages.append(_tool_message(cid, tname, err_payload))
                return {
                    "messages": _as_message_list(new_tool_messages),
                    "approvals": approvals,
                    "pending_hitl": [],
                    "tokens_used": tokens_used,
                    "tool_calls_used": tool_calls_used,
                    "failure_counts": failures,
                    # 计数一并回写（否则这条终止路径会丢掉计数，
                    # 让"同参数连续失败 2 次"的检测在下一轮失效）。
                    "tool_call_counts": tool_name_counts,
                    "query_counts": query_counts,
                    "stop_reason": "repeated_failure",
                    "final_answer": f"工具 '{tname}' 以相同参数连续失败，任务已终止以避免死循环。",
                }

            try:
                # ------------------------------------------------------------------
                # **轮次只在 agent 节点检查**（2026-11-24 修复：轮次被双倍消耗）
                # ------------------------------------------------------------------
                # 此前这里调 ``budget.check()``，而 ``check()`` 会判定
                # ``rounds >= max_rounds`` —— 但轮次是 **agent 节点**推进的，
                # tools 节点自己一次都没推进过。于是每跑一轮（agent→tools）
                # 就要消耗**两轮**预算，``max_rounds=8`` 实际只够 4 轮。
                # 现在 tools 节点只检查工具调用数 / 时长 / 上下文窗口。
                budget.check_no_rounds()
                budget.charge_tool_call()
                tool_calls_used += 1
                tool_name_counts[_dup_count_key_for(tname, targs)] = (
                    tool_name_counts.get(_dup_count_key_for(tname, targs), 0) + 1)
                #: **工具耗时计量（2026-11-22 新增）**：决策轨迹要显示「这一步
                #: 花了多久」。此前 ToolMessage 不带耗时，前端轨迹面板的耗时
                #: 永远是「–」。这里用单调时钟测一次，挂到消息的
                #: ``duration_ms`` 上，由 api 层回填进 trace 条目。
                t_start = time.perf_counter()
                result = registry.call(
                    tname, targs,
                    session_id=session_id, thread_id=thread_id, user_id=user_id,
                )
                duration_ms = int((time.perf_counter() - t_start) * 1000)
                failures[key] = 0
                payload = result
                executed_call_count += 1
                # ---- 审批快速通道的判定材料（2026-11-24 新增）----
                # 只有**白名单里的写工具**、且结果确实是
                # ``status="applied"`` 的完整结构，才算
                # "一次执行即终态"。
                if _is_self_describing_write(tname, payload):
                    self_describing_write = {
                        "tool_name": tname,
                        "payload": payload,
                    }
            except ToolError as exc:
                failures[key] = failures.get(key, 0) + 1
                tool_name_counts[_dup_count_key_for(tname, targs)] = (
                    tool_name_counts.get(_dup_count_key_for(tname, targs), 0) + 1)
                duration_ms = int((time.perf_counter() - t_start) * 1000)
                payload = exc.to_dict()
            except BudgetExceeded as exc:
                return {
                    "messages": _as_message_list(new_tool_messages),
                    "approvals": approvals,
                    "pending_hitl": [],
                    "tokens_used": tokens_used,
                    "tool_calls_used": tool_calls_used,
                    "failure_counts": failures,
                    "tool_call_counts": tool_name_counts,
                    "query_counts": query_counts,
                    "stop_reason": f"budget_exceeded:{exc.reason}",
                    "final_answer": f"任务因预算/超时限制终止：{exc.detail}",
                }
            except Exception as exc:
                failures[key] = failures.get(key, 0) + 1
                tool_name_counts[_dup_count_key_for(tname, targs)] = (
                    tool_name_counts.get(_dup_count_key_for(tname, targs), 0) + 1)
                duration_ms = int((time.perf_counter() - t_start) * 1000)
                payload = {
                    "code": "TOOL_EXECUTION_ERROR",
                    "message": f"{type(exc).__name__}: {exc}",
                    "retryable": False,
                    "hint": "请修正调用方式或改用其它工具。",
                }
            # ---- **工具结果按 token 上限截断**（docs/01 §2.2）----
            # BudgetGuard.max_tokens_per_tool_result（默认 4000）此前只声明
            # 未启用；检索类工具单次就能返回数千 token，两三次就把上下文
            # 窗口塞满 → 模型端 400 BadRequest。详见
            # :func:`_truncate_tool_result` 的说明。
            payload = _truncate_tool_result(
                payload, budget.max_tokens_per_tool_result if budget else 0)
            new_tool_messages.append(
                _tool_message(cid, tname, payload, duration_ms=duration_ms))

        # ------------------------------------------------------------------
        # 被截断的并行调用：**每个 tool_call_id 都必须有一条 tool 消息**，
        # 否则 OpenAI 兼容端点会因"assistant.tool_calls 没有对应 tool 消息"
        # 而拒绝整段消息序列（返回空内容 / 400）。
        # ------------------------------------------------------------------
        if truncated > 0:
            for tc in all_calls[max_parallel:]:
                fn = tc.get("function", {}) or {}
                tname = fn.get("name", "") or tc.get("name", "") or "(未知工具)"
                new_tool_messages.append(_tool_message(
                    tc.get("id", ""), tname,
                    {"code": "TOO_MANY_PARALLEL_CALLS",
                     "message": (f"本轮一次性请求了过多并行工具调用"
                                 f"（共 {len(all_calls)} 个，上限 {max_parallel} 个），"
                                 f"超出部分未执行。"),
                     "retryable": False,
                     "hint": ("请减少单轮的并行调用数量，"
                              "或直接基于已有结果作答。")}))

        # ------------------------------------------------------------------
        # **单工具单轮上限被触发的调用**：同样要有一条 tool 消息
        # ------------------------------------------------------------------
        # 与上面同理：assistant.tool_calls 必须逐条跟一条 tool 消息，
        # 否则 OpenAI 兼容端点会以非法消息序列拒绝整个请求。
        # 语义是「本轮丢弃、下一轮可以再来」，**不是**失败。
        if dropped_calls:
            for tc in dropped_calls:
                fn = tc.get("function", {}) or {}
                tname = fn.get("name", "") or tc.get("name", "") or "(未知工具)"
                cap = per_round_cap_for(tname)
                new_tool_messages.append(_tool_message(
                    tc.get("id", ""), tname,
                    {"code": "PER_ROUND_TOOL_LIMIT",
                     "message": (f"工具 '{tname}' 在本轮已执行 {cap} 次"
                                 f"（单轮上限 {cap} 次），本次重复调用已丢弃。"),
                     "retryable": True,
                     "hint": ("请基于本轮已获得的结果继续推进，"
                              "不要在同一轮内反复调用同一个工具。")}))

        # ------------------------------------------------------------------
        # **审批快速通道（2026-11-24 新增，修复「审批太慢」）**
        # ------------------------------------------------------------------
        # 真实性能故障（实测）：审批续跑的图拓扑是
        # ``agent → tools → hitl → tools → agent``，批准后**还要再跑一次 LLM**，
        # 只为把工具结果改写成一段散文。那一次 LLM 要 1.06~2.33 s，
        # 占整个审批耗时的 **100%**（实测点击→完成 1410 ms）。
        #
        # 现在：**当被批准的动作本身就是终态**（写工具一次执行即产出
        # 完整、自描述的结果），就**跳过第二次 LLM**，直接用工具结果作答。
        #
        # ⚠️ **必须按"意图"判定，不能靠启发式猜测**。这里用**三个硬条件**：
        #   ① 本轮挂起审批时**恰好 1 个**待审批项
        #      （``pending_hitl_count == 1``）；
        #   ② agent 那条消息里**只有这一个**工具调用
        #      （``len(raw_calls) == 1``）——
        #      否则"写 2 个文件"或"写完再读回来校验"这类**多步计划**
        #      会被误判成单步 → 就会**丢掉**后续步骤；
        #   ③ 该工具确实产出了**完整成功**的结果
        #      （``_is_self_describing_write``）。
        # 三条同时满足才走快速通道；任何一条不满足就**照旧**回到 agent 节点。
        pending_count = int(state.get("pending_hitl_count", 0) or 0)
        sole_action = (pending_count == 1
                       and len(raw_calls) == 1
                       and executed_call_count == 1)
        #: **用户显式要求模型总结时禁用快速通道**（2026-11-24 新增）。
        #: ``summarize=True`` 的语义就是"我要一段人话"，
        #: 这时必须照旧跑第二次 LLM。
        want_summarize = any(
            bool((d or {}).get("summarize")) for d in approvals.values())
        if sole_action and self_describing_write is not None and not want_summarize:
            tname = str(self_describing_write.get("tool_name") or "")
            payload = dict(self_describing_write.get("payload") or {})
            return {
                "messages": _as_message_list(new_tool_messages),
                "approvals": approvals,
                "pending_hitl": [],
                "tokens_used": tokens_used,
                "tool_calls_used": tool_calls_used,
                "failure_counts": failures,
                "tool_call_counts": tool_name_counts,
                "query_counts": query_counts,
                "ask_rounds": ask_rounds + 1,
                # ---- 快速通道标记（供 _route_after_tools 路由到 end）----
                "approved_result_ready": True,
                "approved_result": payload,
                "approved_result_tool": tname,
                "stop_reason": "done",
                "final_answer": _fast_path_final_answer(tname, payload),
            }

        return {
            "messages": _as_message_list(new_tool_messages),
            "approvals": approvals,
            "pending_hitl": [],
            "tokens_used": tokens_used,
            "tool_calls_used": tool_calls_used,
            "failure_counts": failures,
            "tool_call_counts": tool_name_counts,
            "query_counts": query_counts,
            "ask_rounds": ask_rounds + 1,
            "stop_reason": None,
        }

    return tools_node


def make_hitl_node(gate: HITLGate, audit: Any = None) -> Any:
    """构造 hitl 节点。

    职责：
        1. 对每个 pending_hitl 条目调用 gate.require() → 内部 interrupt() 挂起
        2. resume 后拿到人工决策，写入 approvals
        3. 记录 HITL 审计（docs/02 §5.3）
        4. 回到 tools 节点执行（此时 approvals 已就绪，工具只会被执行一次）

    **关键**：interrupt() 之前不做任何副作用。
    LangGraph resume 时会从 interrupt 所在节点（hitl）**从头重跑**，
    因此 hitl 节点在 interrupt 之前不得执行工具或写库。
    """

    def hitl_node(state: AgentState) -> Dict[str, Any]:
        approvals: Dict[str, Any] = dict(state.get("approvals", {}) or {})
        pending: List[Dict[str, Any]] = list(state.get("pending_hitl", []) or [])
        session_id = state.get("session_id")

        for item in pending:
            cid = item.get("tool_call_id", "")
            if cid in approvals:
                continue
            p = PendingApproval(
                tool_call_id=cid,
                tool_name=item.get("tool_name", ""),
                args=item.get("args", {}) or {},
                permission=item.get("permission", "WRITE_DATA"),
                description=item.get("description", ""),
                impact=item.get("impact", ""),
                reversible=bool(item.get("reversible", True)),
                metadata=item.get("metadata", {}) or {},
            )
            # interrupt() 在首次进入时抛出 GraphInterrupt；
            # resume 后同一位置返回审批 payload。
            decision = gate.require(p, session_id=session_id)
            approvals[cid] = decision.to_dict()
            if decision.approved and decision.session_grant and session_id:
                gate.grant_session(session_id, p.tool_name)
            if audit is not None:
                try:
                    audit.log(
                        action="hitl_decision",
                        target=p.tool_name,
                        args=p.args,
                        result=decision.to_dict(),
                        status="approved" if decision.approved else "rejected",
                        session_id=session_id,
                        thread_id=state.get("thread_id"),
                        user_id=state.get("user_id"),
                        actor=decision.actor,
                        extra={"prompt_version": PROMPT_VERSION,
                               "impact": p.impact, "reversible": p.reversible},
                    )
                except Exception:
                    pass
        return {
            "approvals": approvals,
            "pending_hitl": [],
            "stop_reason": None,
        }

    return hitl_node


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------
def _tool_message(tool_call_id: str, name: str, payload: Any,
                   *, duration_ms: Optional[int] = None) -> Dict[str, Any]:
    """构造 tool 角色消息（内容为 JSON 字符串）。

    **``duration_ms``（2026-11-22 新增）**：本次工具执行的真实耗时。
    ToolMessage 本身没有耗时字段，此前决策轨迹面板的「耗时」永远是「–」。
    这里额外挂一个顶层字段，由 api 层 ``_tool_result_entries`` 读出并回填
    进 trace 条目的 ``duration_ms``。
    """
    content = payload if isinstance(payload, str) else json.dumps(
        payload, ensure_ascii=False, default=str)
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": name,
        "content": content,
        "duration_ms": duration_ms,
    }


def _truncate_tool_result(payload: Any, max_tokens: int) -> Any:
    """按 token 上限**截断工具结果**（docs/01 §2.2「单工具结果最多 4K token」）。

    为什么需要（2026-11-22 新增）：
        ``BudgetGuard.max_tokens_per_tool_result``（默认 4000）**一直只声明
        未启用**。检索类工具（``kb_read`` 会带 ``parent_text``、``kb_search``
        会带整块 chunk）单次就能返回数千 token，两三次就把上下文窗口
        塞满 → 模型端 400 BadRequest，任务直接变 exception。
        实测「SOP 里 FMEA 排查步骤是哪几步」有 40% 的概率因此失败。

    截断策略（**结构优先**，保证可用性）：
        1. 先按 token 估算把 JSON 序列化结果压到上限内；
        2. 若整体太大，**先丢大字段**（``parent_text`` / ``neighbors`` /
           ``raw`` / ``text``），保留结构与关键字段（``chunk_id`` / ``source``）；
        3. 仍超限才做**字符串硬截断**，并在末尾标注截断字节数。

    ⚠️ 这是**有损截断**：模型看到的工具结果不完整。但好过整个请求被拒。
    """
    if not max_tokens or max_tokens <= 0:
        return payload
    if isinstance(payload, str):
        text = payload
        if estimate_tokens(text) <= max_tokens:
            return payload
        cut = max(max_tokens * 2, 64)
        return (text[:cut]
                + f"\n…[结果已截断：原 {len(text)} 字符，保留前 {cut} 字符]")

    if not isinstance(payload, (dict, list)):
        return payload
    try:
        full = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        return payload
    if estimate_tokens(full) <= max_tokens:
        return payload

    # ---- 第 2 步：先丢"纯冗余"的大字段，再压缩"有信息量"的列表 ----
    #: 纯冗余：可以直接丢，丢了不丢信息量（都是原文的重复）
    _REDUNDANT = ("parent_text", "neighbors", "raw")
    #: 有信息量：不能整块丢，只能**逐条压缩**
    _COMPRESS = ("chunks", "items")

    def _compress_list(items: Any, per_item_tokens: int) -> Any:
        """列表逐条压缩：每条只留关键字段 + 截断文本。"""
        if not isinstance(items, list):
            return items
        out = []
        for it in items[:12]:
            if isinstance(it, dict):
                slim_it = {k: v for k, v in it.items() if k not in _REDUNDANT}
                text = slim_it.get("text")
                if isinstance(text, str) and estimate_tokens(text) > per_item_tokens:
                    cut = max(per_item_tokens * 2, 32)
                    slim_it["text"] = text[:cut] + "…[已截断]"
                out.append(slim_it)
            else:
                out.append(it)
        return out

    def _slim(obj: Any) -> Any:
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if k in _REDUNDANT:
                    continue
                if k in _COMPRESS:
                    out[k] = _compress_list(v, max(max_tokens // 8, 200))
                    continue
                out[k] = _slim(v)
            return out
        if isinstance(obj, list):
            return [_slim(x) for x in obj[:12]]
        return obj

    slim = _slim(payload)
    try:
        slim_full = json.dumps(slim, ensure_ascii=False, default=str)
    except Exception:
        slim = payload
        slim_full = full
    if estimate_tokens(slim_full) <= max_tokens:
        if isinstance(slim, dict):
            slim["_truncated"] = (
                "结果过大，已省略 parent_text / neighbors / raw 等冗余字段，"
                "并压缩了部分正文；如需完整内容请用更精确的参数重查。")
        return slim

    # ---- 第 3 步：硬截断 ----
    cut = max(max_tokens * 2, 64)
    return (slim_full[:cut]
            + f"\n…[结果已截断：原 {len(slim_full)} 字符，保留前 {cut} 字符]")


def _as_message_list(items: List[Dict[str, Any]]) -> List[Any]:
    """把 dict 消息转成 LangChain 消息（若可用）。"""
    return _to_langchain_messages(items) if LANGGRAPH_AVAILABLE else list(items)


def _extract_resume_payload(state: AgentState) -> Any:
    """从 state 中提取 resume payload。

    LangGraph 的 interrupt resume 值会作为一个 HumanMessage/ToolMessage
    出现在 messages 中（由 Command(resume=...) 注入）。
    这里兼容多种形态：
        · 消息 content 是 JSON 字符串
        · 消息 additional_kwargs / metadata 中带结构化数据
    """
    messages: List[Any] = list(state.get("messages", []) or [])
    # 从后往前找最后一条"非 assistant/tool 轨迹"的可解析消息
    for m in reversed(messages):
        d = _as_dict_message(m)
        content = d.get("content", "")
        if d.get("role") == "user" and content:
            try:
                parsed = json.loads(content)
                if isinstance(parsed, (dict, list)):
                    return parsed
            except Exception:
                pass
        for attr in ("additional_kwargs", "metadata", "kwargs"):
            extra = getattr(m, attr, None)
            if isinstance(extra, dict):
                for key in ("resume", "approval", "approvals", "hitl"):
                    if key in extra:
                        return extra[key]
    return None


# ----------------------------------------------------------------------
# 工厂
# ----------------------------------------------------------------------
def make_plan_node(llm: Any, registry: Any = None,
                   budget: Optional[BudgetGuard] = None) -> Any:
    """构造 plan 节点：**只规划不执行**。

    行为：
        1. 组装「规划专用」消息（system + user + 真实工具名清单）；
        2. 调用 LLM 产出**结构化 JSON 计划**；
        3. 归一成 ``{goal, steps[], risks[], questions[]}`` 写入 state["plan"]；
        4. **绝不调用任何工具**。

    与 agent 节点共用同一个 llm 实例（但 llm 本身可以是按 plan 路由选出的
    客户端，见 ``LLMRouter.route("plan")``）。
    """

    def plan_node(state: AgentState) -> Dict[str, Any]:
        from ..core.modes import normalize_plan, plan_prompt_for  # noqa: PLC0415

        messages: List[Any] = list(state.get("messages", []) or [])
        tokens_used = int(state.get("tokens_used", 0))
        tool_calls_used = int(state.get("tool_calls_used", 0))

        #: 取最后一条用户消息作为规划目标
        question = ""
        for m in reversed(messages):
            d = _as_dict_message(m)
            if d.get("role") == "user":
                question = str(d.get("content", "") or "")
                break
        if not question:
            question = str(state.get("final_answer") or "") or ""

        tool_names: List[str] = []
        try:
            if registry is not None:
                tool_names = [s.name for s in registry.list_specs()]
        except Exception:  # pragma: no cover
            tool_names = []

        llm_messages = plan_prompt_for(question, tool_names)

        est = estimate_messages_tokens(llm_messages)
        try:
            if budget is not None:
                budget.check(est_tokens=est)
                budget.charge_round()
        except BudgetExceeded as exc:
            return {
                "stop_reason": f"budget_exceeded:{exc.reason}",
                "final_answer": f"规划因预算/超时限制终止：{exc.detail}",
                "tokens_used": tokens_used,
                "tool_calls_used": tool_calls_used,
            }

        try:
            resp = llm.chat_sync(llm_messages)
        except Exception as exc:
            return {
                "stop_reason": f"llm_error:{type(exc).__name__}",
                "final_answer": f"规划模型调用失败：{type(exc).__name__}: {exc}",
                "tokens_used": tokens_used,
                "tool_calls_used": tool_calls_used,
            }

        usage = resp.get("usage", {}) or {}
        tokens_used += int(usage.get("total", 0) or 0)
        try:
            if budget is not None:
                budget.charge_tokens(prompt=int(usage.get("prompt", 0) or 0),
                                     completion=int(usage.get("completion", 0) or 0))
        except BudgetExceeded as exc:
            return {
                "stop_reason": f"budget_exceeded:{exc.reason}",
                "final_answer": f"规划因 token 预算终止：{exc.detail}",
                "tokens_used": tokens_used,
                "tool_calls_used": tool_calls_used,
            }

        raw = resp.get("content", "") or ""
        plan = normalize_plan(raw, goal_hint=question)
        plan["model"] = getattr(getattr(llm, "config", None), "model", "") or ""

        assistant_msg: Dict[str, Any] = {
            "role": "assistant",
            "content": raw or json.dumps(plan, ensure_ascii=False),
        }
        new_messages: List[Any] = [assistant_msg]
        if LANGGRAPH_AVAILABLE:
            new_messages = _to_langchain_messages(new_messages)

        return {
            "messages": new_messages,
            "plan": plan,
            "tokens_used": tokens_used,
            "tool_calls_used": tool_calls_used,
            "stop_reason": "plan_ready",
            "final_answer": plan.get("goal") or raw[:500],
        }

    return plan_node


def build_graph(llm: Any, registry: Any, gate: HITLGate, budget: BudgetGuard,
                checkpointer: Any = None, *, audit: Any = None,
                system_prompt: Optional[str] = None,
                mode: str = "agent") -> Any:
    """构建 Agent 图。

    节点：agent → tools → (hitl → tools)* → agent → END

    **模式分支**（Cursor 式 Agent / Ask / Plan 三档）：
        · ``agent`` —— 完整 ReAct 循环（默认，历史行为不变）
        · ``ask``   —— 仍走 agent 节点，但工具节点会按**只读白名单**过滤，
                       且最多 ``ASK_MAX_TOOL_ROUNDS`` 轮工具调用
        · ``plan``  —— START 直接进 ``plan`` 节点，**不经过 tools 节点**，
                       因此**不可能调用任何工具**；产出结构化计划后结束。

    :param llm:          LLMClient / LLMRouter（需提供 chat_sync）
    :param registry:     ToolRegistry
    :param gate:         HITLGate
    :param budget:       BudgetGuard
    :param checkpointer: LangGraph checkpointer；None 时用 MemorySaver
    :param audit:        Audit（可选，写 HITL 审计）
    :param mode:         交互模式（agent / ask / plan）
    """
    if not LANGGRAPH_AVAILABLE:
        raise RuntimeError(
            "未安装 langgraph，无法构建 Agent 图。请执行：pip install langgraph"
        )

    from ..core.modes import normalize_mode  # noqa: PLC0415

    m = normalize_mode(mode)

    builder = StateGraph(AgentState)

    builder.add_node("agent", make_agent_node(llm, registry, budget, system_prompt))
    builder.add_node("tools", make_tools_node(registry, budget, gate, mode=m))
    builder.add_node("hitl", make_hitl_node(gate, audit))
    builder.add_node("plan", make_plan_node(llm, registry, budget))

    if m == "plan":
        # Plan 模式：直接进 plan 节点，**完全不经过 tools 节点**
        builder.add_edge(START, "plan")
        builder.add_edge("plan", END)
    else:
        builder.add_edge(START, "agent")
        # agent → tools；tools 内部判断是回到 agent 还是去 hitl
        builder.add_conditional_edges(
            "agent",
            _route_after_agent,
            {"tools": "tools", "end": END},
        )
        builder.add_conditional_edges(
            "tools",
            _route_after_tools,
            {"hitl": "hitl", "agent": "agent", "end": END},
        )
        # hitl 处理完审批后回到 tools 执行
        builder.add_edge("hitl", "tools")

    if checkpointer is None:
        checkpointer = _default_checkpointer()

    return builder.compile(checkpointer=checkpointer)


def _default_checkpointer() -> Any:
    """默认 checkpointer：优先 MemorySaver，缺失时再试 PostgresSaver。

    对应 docs/01 §2.4「Checkpoint / 断点续跑」：
        A. LangGraph Sqlite/PG Saver
    DB 不可用时自动降级为内存 saver，保证无数据库环境仍可跑通。
    """
    try:
        from langgraph.checkpoint.memory import MemorySaver  # type: ignore
        return MemorySaver()
    except Exception:
        return None


def build_postgres_checkpointer(dsn: str) -> Any:
    """构造 PostgresSaver（可选依赖，失败返回 None 表示降级）。"""
    try:
        from langgraph.checkpoint.postgres import PostgresSaver  # type: ignore
    except Exception:
        return None
    try:
        return PostgresSaver.from_conn_string(dsn)
    except Exception:
        return None


def _route_after_agent(state: AgentState) -> str:
    """agent 之后：有 tool_calls → tools；否则结束。

    ⚠️ **必须先看 stop_reason**（2026-11-22 修复）：
    agent 节点在**调用 LLM 之前**就可能因预算熔断返回
    ``stop_reason="budget_exceeded:..."``（见 :func:`make_agent_node`）。
    此时消息历史里最后一条 assistant 消息**仍带着上一轮的 tool_calls**，
    若按消息判断就会再次路由回 tools 节点，而 tools 节点因为没有新调用
    会原样返回 → agent → tools → … 形成**死循环**，直到 LangGraph 的
    recursion_limit（10007）被耗尽，抛出 GraphRecursionError。

    实测触发路径：模型一次性返回**数千个并行 tool_calls** 时，
    tools 节点把 tool_calls_used 累加到 5003，下一轮 agent 节点熔断，
    于是进入上面那条死循环。所以这里**先判 stop_reason**。
    """
    reason = state.get("stop_reason")
    if reason and reason != "pending_approval":
        return "end"
    messages = state.get("messages", []) or []
    for m in reversed(messages):
        d = _as_dict_message(m)
        if d.get("role") == "assistant" and d.get("tool_calls"):
            return "tools"
        if d.get("role") == "assistant":
            return "end"
    return "end"


def _route_after_tools(state: AgentState) -> str:
    """tools 之后：有待审批 → hitl；终止原因已定 → end；否则回 agent。

    **审批快速通道（2026-11-24 新增）**：
        若本轮被批准的动作本身就是终态（写工具一次执行即产出完整结果），
        tools 节点会置 ``approved_result_ready=True`` 并写好 ``final_answer``。
        此时**直接结束**，不再回到 agent 节点跑第二次 LLM
        （实测省掉 1.06~2.33 s，占审批总耗时的 100%）。
    """
    if state.get("pending_hitl"):
        return "hitl"
    #: 快速通道：答案已就绪 → 结束本轮（不再消耗 LLM）
    if state.get("approved_result_ready"):
        return "end"
    reason = state.get("stop_reason")
    if reason and reason != "pending_approval":
        return "end"
    return "agent"


__all__ = [
    "AgentState",
    "build_graph",
    "build_postgres_checkpointer",
    "make_agent_node",
    "make_tools_node",
    "make_hitl_node",
    "make_plan_node",
    "LANGGRAPH_AVAILABLE",
]
