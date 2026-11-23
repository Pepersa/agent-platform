# -*- coding: utf-8 -*-
"""Prompt 模板包（对外统一出口）。

对应设计文档：《01_架构方案_RAG_Agent.md》§2.6 上下文工程层 →「Prompt 模板与版本管理」。

实际实现位于同目录下的 ``prompts.py``，本文件只做再导出，便于
``from agent_platform.orchestration.prompts import get_system_prompt``。
"""

from .prompts import (  # noqa: F401
    FALLBACK_SYSTEM_PROMPT,
    PROMPT_VERSION,
    SYSTEM_PROMPT_FILE,
    get_system_prompt,
    get_system_prompt_with_version,
    load_prompt,
    render,
)

__all__ = [
    "PROMPT_VERSION",
    "SYSTEM_PROMPT_FILE",
    "FALLBACK_SYSTEM_PROMPT",
    "get_system_prompt",
    "get_system_prompt_with_version",
    "load_prompt",
    "render",
]
