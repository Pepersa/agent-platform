# -*- coding: utf-8 -*-
"""Agent平台 —— 平台根包。

对应设计文档：《01_架构方案_RAG_Agent.md》§1.2 总体分层架构、
《02_场景方案_STDF分析与测试用例生成.md》§2.1 场景 A/B 共用底座。

本包内的子包划分与 docs/01 §2.4~§2.6 一一对应：
    core/          —— Agent 核心运行时（预算、审计、工具注册、LLM 客户端）
    orchestration/ —— Agent 编排层（LangGraph 状态机、HITL 闸门、Prompt 模板）
    api/           —— 接入层（FastAPI，SSE 流式）
    tools/         —— 工具层薄封装与注册入口
"""

__version__ = "0.1.0"
