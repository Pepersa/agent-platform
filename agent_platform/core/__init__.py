# -*- coding: utf-8 -*-
"""Agent 核心运行时（Core Runtime）。

对应设计文档：
    《01_架构方案_RAG_Agent.md》§2.2 上下文窗口管理（工具结果上限）
    《01_架构方案_RAG_Agent.md》§2.4 Agent 编排层（预算与超时控制 BudgetGuard）
    《01_架构方案_RAG_Agent.md》§2.5 工具层（Tool Registry 为核心资产）
    《01_架构方案_RAG_Agent.md》§2.10 治理层（审计日志）
    《01_架构方案_RAG_Agent.md》§2.3 路由层（模型路由 ModelRouter）
    《01_架构方案_RAG_Agent.md》§3.1 第 8/9 步（引用强制 + 引用块存在性校验）
    《02_场景方案_STDF分析与测试用例生成.md》§9.3 审计日志

模块一览：
    budget        —— 预算与超时闸门
    audit         —— append-only 审计（PG → SQLite → JSONL 三级降级）
    tool_registry —— 工具契约与注册表（核心资产）
    llm_client    —— OpenAI 兼容客户端 + 模型路由
    citations     —— 引用标记的解析 / 归一化 / 校验（检索与 Agent 两条路径共用）
"""
