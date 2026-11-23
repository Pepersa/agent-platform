# -*- coding: utf-8 -*-
"""Prompt 加载器（Prompt Registry 轻量版）。

对应设计文档：
    《01_架构方案_RAG_Agent.md》§2.6 上下文工程层 →「Prompt 模板与版本管理」
        A. Prompt Registry（PG 表 + Jinja2）—— 可回滚、可评测
    《02_场景方案_STDF分析与测试用例生成.md》§9.3 审计日志 → prompt_version 字段

本模块提供：
    PROMPT_VERSION      —— 当前 prompt 版本号（变更即升版本）
    get_system_prompt() —— 取系统提示词全文
    load_prompt()       —— 从任意路径加载模板文件
    render()            —— 轻量变量渲染（{{var}} 语法，零额外依赖）
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

#: Prompt 版本（写入审计日志 prompt_version）
#:
#: v1.1 —— 引入「检索必要性判断」：把"检索优先于生成"改成**有条件**的检索，
#: 明确 A（事实/参数）/ B（通用/常识）/ C（闲聊）三类问题的不同处理方式，
#: 并把"未在知识库中找到"限定在"本应走检索"的问题上。
#: 详见 ``system.md`` 顶部的 v1.1 改动说明。
PROMPT_VERSION = "p_v1.1"

#: SYSTEM_PROMPT 模板文件路径
SYSTEM_PROMPT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system.md")

#: system.md 缺失时的兜底文本（保留六条硬性规则）
#:
#: ⚠️ 这是 ``system.md`` 的**降级兜底**，不是主提示词。规则 1 必须保留
#: "有条件的检索"语义 —— 否则 system.md 读不到时，系统会退回到"什么都要检索"
#: 的旧行为（即用户报障的现象）。规则 2/3 的严格性**不得**在此处放宽。
FALLBACK_SYSTEM_PROMPT = """你是企业内部的通用 AI 助手，必须遵守：
1. 检索优先于生成（**有条件**）：先判断"答案是否取决于用户上传的文档"。
   A 类事实/参数类（限值、参数、定义、告警码、规程、统计数值）必须先调用 kb_search/tp_lookup 查证；
   B 类通用/常识类（你是谁、你是什么模型、能做什么、写代码、翻译）**不要检索**，直接回答；
   C 类闲聊简短回应。边界不确定时按 A 类处理；
   若系统提示"本轮无需检索"，说明该问题不依赖知识库，请直接作答；
2. 数字只能来自工具结果，禁止自行计算或改写；
3. 引用强制：事实性断言后附 [来源:chunk_id]；
4. 不确定就说不确定（**仅限本应走检索的问题**）：检索为空明确说"未在知识库中找到"，不编造；
   禁止把"没有检索"当成"检索为空"；
5. 只读优先，写操作会要求人工确认（用户要求调用写工具时，先发起调用，由系统挂起审批）；
6. 数值问题走 sql_query，不用知识库检索。
"""

#: 仅供"改前 vs 改后"取证使用的 p_v1.0 原始规则 1 / 规则 4 文本。
#: ⚠️ 仅在环境变量 ``AGENT_FORCE_LEGACY_RETRIEVAL=1`` 时启用，正常运行不会触发。
_LEGACY_V1_SYSTEM_PROMPT = """你是企业内部的通用 AI 助手，必须遵守：
1. 检索优先于生成：涉及具体参数、限值、定义、告警码、手册规程等内容，
   必须先调用 kb_search / tp_lookup / kb_read 查证，再作答。禁止凭记忆直接给出限值或数值。
2. 数字只能来自工具结果，禁止自行计算或改写；
3. 引用强制：事实性断言后附 [来源:chunk_id]；
4. 不确定就说不确定：检索结果为空时，必须明确回答"未在知识库中找到"，
   并说明已尝试的查询词与可考虑的其它来源；禁止编造任何参数、限值、文档名或页码。
5. 只读优先，写操作会要求人工确认（用户要求调用写工具时，先发起调用，由系统挂起审批）；
6. 数值问题走 sql_query，不用知识库检索。
"""


def load_prompt(path: str, *, default: str = "") -> str:
    """从文件加载 prompt 模板；文件缺失或读取失败时返回 default。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return default


def render(template: str, variables: Optional[Dict[str, Any]] = None) -> str:
    """渲染 prompt。

    为避免引入 Jinja2 依赖，使用 ``{{var}}`` 的轻量替换；
    未提供的变量保持原样（便于排障）。
    """
    if not variables:
        return template
    out = template
    for key, value in variables.items():
        out = out.replace("{{" + key + "}}", "" if value is None else str(value))
    return out


def get_system_prompt(variables: Optional[Dict[str, Any]] = None) -> str:
    """取 SYSTEM_PROMPT 全文（可注入少量变量，如工具清单摘要）。"""
    if os.environ.get("AGENT_FORCE_LEGACY_RETRIEVAL") == "1":
        # 仅用于"改前 vs 改后"取证：还原 p_v1.0 的绝对化"检索优先"规则。
        # 正常运行时不会进入这个分支。
        return render(_LEGACY_V1_SYSTEM_PROMPT, variables)
    template = load_prompt(SYSTEM_PROMPT_FILE, default=FALLBACK_SYSTEM_PROMPT)
    return render(template, variables)


def get_system_prompt_with_version(variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """返回 ``{"text": ..., "version": ...}``，便于审计写入 prompt_version。"""
    return {"text": get_system_prompt(variables), "version": PROMPT_VERSION}
