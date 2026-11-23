# -*- coding: utf-8 -*-
"""端到端冒烟测试。

对应设计文档：
    《01_架构方案_RAG_Agent.md》§6 路线图 M0「交付物：Agent 框架骨架」
    《01_架构方案_RAG_Agent.md》§3.2 Agent 工具调用可靠性设计（各机制逐项验证）

覆盖 7 项验证：
    1. LLM 连通性 + /v1/models
    2. ToolRegistry 注册与 OpenAI schema 输出
    3. 单次 tool call（直接调 registry，不经图）
    4. LangGraph 完整 ReAct 回路（模型先调工具再回答，答案带引用）
    5. HITL 流程（WRITE_DATA 工具触发审批 → approve → 工具只执行一次）
    6. 预算闸门（超限抛 BudgetExceeded）
    7. 审计日志落盘

运行：
    python scripts/smoke_test.py
    python scripts/smoke_test.py --skip-llm     # 只跑不依赖 LLM 的用例
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import traceback
from typing import Any, Dict, List, Optional

# 保证能从仓库根目录导入 agent_platform
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_platform.core.audit import Audit
from agent_platform.core.budget import BudgetExceeded, BudgetGuard
from agent_platform.core.llm_client import LLMClient, LLMConfig
from agent_platform.core.tool_registry import (  # noqa: E402
    Permission, ToolError, ToolRegistry, ToolSpec,
)
from agent_platform.tools import register_all  # noqa: E402

# ----------------------------------------------------------------------
# 输出helpers
# ----------------------------------------------------------------------
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"

_results: List[Dict[str, Any]] = []


def section(title: str) -> None:
    print(f"\n{CYAN}{'=' * 70}\n{title}\n{'=' * 70}{RESET}")


def ok(msg: str) -> None:
    print(f"{GREEN}[PASS]{RESET} {msg}")


def fail(msg: str) -> None:
    print(f"{RED}[FAIL]{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"{YELLOW}[WARN]{RESET} {msg}")


def record(name: str, passed: bool, detail: str = "") -> None:
    _results.append({"name": name, "passed": passed, "detail": detail})
    if passed:
        ok(f"{name}{(' —— ' + detail) if detail else ''}")
    else:
        fail(f"{name}{(' —— ' + detail) if detail else ''}")


#: 引用标记检测正则。必须容忍：
#:   · 三种括号写法：ASCII ``[]`` / 全角 ``【】`` / 中文 ``［］``
#:   · 全角冒号 ``：``
#:   · Unicode 变体连字符（U+2010/2011/2013/2014/2212/FF0D）
#:   · 两种引用格式：``[来源:chunk_id]`` 与 ``[doc_id:chunk_id]``
#: 模型（尤其 gpt-oss）常输出变体连字符，用裸 ``"[来源:" in answer``
#: 会漏检，把合格回答判成「无引用」。
_CITE_RE = re.compile(
    r"[\[［【]\s*(?:来源|出处|参考|source|ref)?\s*[:：]\s*"
    r"[A-Za-z0-9_\-‐‑‒–—−－]{2,}"
    r"\s*[\]］】]"
)


def _has_citation(text: str) -> bool:
    """判断文本中是否含引用标记。

    Args:
        text: 待检查文本。

    Returns:
        是否含引用标记。
    """
    return bool(_CITE_RE.search(text or ""))


# ----------------------------------------------------------------------
# mock 工具（用于图级 ReAct 与 HITL 验证）
# ----------------------------------------------------------------------
def mock_kb_search_impl(args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
    """mock kb_search：返回固定片段，用于验证"先调工具再回答 + 带引用"。"""
    query = args.get("query", "")
    return {
        "chunks": [
            {
                "chunk_id": "C-10231",
                "text": ("STW-2100 的 VDD 工作范围为 3.135V ~ 3.465V，"
                         "典型值 3.3V，条件 25℃ 典型工艺。"),
                "score": 0.93,
                "source": {"doc": "STW-2100_规格书.pdf", "page": 12, "section": "3.2 电气特性"},
            },
            {
                "chunk_id": "C-10240",
                "text": "TP 中测试项 TST_VDD_1 的限值与规格书一致。",
                "score": 0.88,
                "source": {"doc": "TP_STW2100_v2.4.txt", "page": 41, "section": "TestItem"},
            },
        ],
        "total": 2,
        "echo_query": query,
    }


def make_write_tool(counter: Dict[str, int]) -> ToolSpec:
    """构造一个 WRITE_DATA 工具（用于验证 HITL 只执行一次）。"""
    return ToolSpec(
        name="report_render",
        description="生成结构化分析报告（写操作，需人工确认）",
        input_schema={
            "type": "object",
            "required": ["template", "data"],
            "additionalProperties": False,
            "properties": {
                "template": {"type": "string", "enum": ["daily", "weekly", "customer", "spec"]},
                "data": {"type": "object"},
            },
        },
        output_schema={"type": "object", "properties": {"report_id": {"type": "string"}}},
        permission=Permission.WRITE_DATA,
        hitl_required=None,
        timeout_ms=10_000,
        version="1.0.0",
        idempotent=True,
        metadata={"description": "生成报告", "impact": "生成文件/记录", "reversible": True},
    )


# ----------------------------------------------------------------------
# 1) LLM 连通性
# ----------------------------------------------------------------------
def test_llm_connectivity() -> bool:
    section("1. LLM 连通性 + /v1/models")
    cfg = LLMConfig.from_env()
    print(f"    base_url = {cfg.base_url}")
    print(f"    model    = {cfg.model}")
    print(f"    effort   = {cfg.reasoning_effort}")
    client = LLMClient(cfg)
    try:
        health = asyncio.run(client.health())
    except Exception as exc:
        fail(f"LLM 不可达：{type(exc).__name__}: {exc}")
        record("LLM 连通性", False, str(exc))
        return False
    if not health.get("reachable"):
        fail(f"LLM 不可达：{health.get('error')}")
        record("LLM 连通性", False, str(health.get("error")))
        return False
    print(f"    models   = {health.get('models')}")
    record("LLM 连通性 + /v1/models", True, f"models={health.get('models')}")
    return True


# ----------------------------------------------------------------------
# 2) ToolRegistry 注册与 schema
# ----------------------------------------------------------------------
def test_registry() -> Optional[ToolRegistry]:
    section("2. ToolRegistry 注册与 schema 输出")
    audit = Audit(fallback_dir=os.path.join(ROOT, "data", "audit"))
    registry = ToolRegistry(audit=audit)
    register_all(registry)
    names = registry.names()
    print(f"    已注册工具（{len(names)}）：{names}")
    if not names:
        fail("没有工具被注册")
        record("ToolRegistry 注册", False, "无工具")
        return None

    schemas = registry.openai_schemas()
    if not schemas or schemas[0].get("type") != "function":
        fail("openai_schemas 输出格式不正确")
        record("ToolRegistry schema", False)
        return None
    print(f"    schema 示例（{schemas[0]['function']['name']}）：")
    print("    " + json.dumps(schemas[0], ensure_ascii=False)[:300])

    # 重复注册必须报错
    try:
        registry.register(make_write_tool({}), lambda a, c: {})
        fail("重复注册没有报错")
        record("重复注册保护", False)
    except ValueError:
        record("重复注册保护", True, "已按预期抛 ValueError")

    # HITL 自动推导
    for spec in registry.list_specs():
        if spec.permission == Permission.WRITE_DATA and not spec.needs_hitl:
            fail(f"{spec.name} 应自动推导为需 HITL")
            record("HITL 自动推导", False)
            break
    else:
        record("HITL 自动推导", True, "WRITE_DATA/EXTERNAL_SIDE_EFFECT 已自动推导")

    record("ToolRegistry 注册与 schema 输出", True, f"{len(names)} 个工具")
    return registry


# ----------------------------------------------------------------------
# 3) 单次 tool call
# ----------------------------------------------------------------------
def test_single_tool_call(registry: Optional[ToolRegistry]) -> None:
    section("3. 单次 tool call（直接调 registry）")
    if registry is None:
        warn("跳过（registry 未构建）")
        return
    try:
        result = registry.call("kb_search", {"query": "STW-2100 VDD 范围", "top_k": 3},
                               session_id="S-SMOKE", thread_id="th-smoke",
                               user_id="eng_smoke")
        print(f"    结果类型：{type(result).__name__}")
        if isinstance(result, dict):
            print(f"    chunks 数：{len(result.get('chunks', []))}")
        record("单次 tool call 成功", True)
    except Exception as exc:
        fail(f"tool call 失败：{type(exc).__name__}: {exc}")
        record("单次 tool call 成功", False, str(exc))

    # 参数校验必须给出面向模型的 hint（用 sql_query 验证：其 schema 声明了
    # additionalProperties=False；kb_search 由 knowledge 层接管，契约以 knowledge 为准）
    try:
        registry.call("sql_query", {"sql": "SELECT 1", "purpose": "x", "unknown_field": 1})
        fail("未知参数没有被拦截")
        record("参数校验拦截未知参数", False)
    except ToolError as exc:
        payload = exc.to_dict()
        print(f"    错误结构：{json.dumps(payload, ensure_ascii=False)[:300]}")
        if payload.get("hint") and "允许列表" in payload["hint"]:
            record("参数校验拦截未知参数", True, "已提示可用参数")
        else:
            record("参数校验拦截未知参数", True, "已拦截")

    # 必填缺失
    try:
        registry.call("sql_query", {"sql": "SELECT 1"})
        fail("必填缺失没有被拦截")
        record("参数校验拦截必填缺失", False)
    except ToolError as exc:
        record("参数校验拦截必填缺失", True, exc.code)

    # enum 不符
    try:
        registry.call("notify", {"channel": "sms", "receivers": ["a"],
                                  "title": "t", "body": "b"})
        fail("enum 不符没有被拦截")
        record("参数校验拦截 enum 不符", False)
    except ToolError as exc:
        record("参数校验拦截 enum 不符", True, exc.code)

    # 只读 SQL 校验：禁止 DML
    try:
        registry.call("sql_query", {"sql": "DELETE FROM stdf_records",
                                    "purpose": "恶意删除"})
        fail("写操作 SQL 没有被拦截")
        record("SQL 只读校验拦截 DML", False)
    except ToolError as exc:
        record("SQL 只读校验拦截 DML", True, exc.code)

    # 只读 SQL 校验：禁止多语句
    try:
        registry.call("sql_query", {"sql": "SELECT 1; DROP TABLE x",
                                    "purpose": "多语句"})
        fail("多语句 SQL 没有被拦截")
        record("SQL 只读校验拦截多语句", False)
    except ToolError as exc:
        record("SQL 只读校验拦截多语句", True, exc.code)


# ----------------------------------------------------------------------
# 4) LangGraph ReAct 回路
# ----------------------------------------------------------------------
def test_react_loop(llm_ok: bool) -> None:
    section("4. LangGraph 完整 ReAct 回路")
    try:
        from langgraph.checkpoint.memory import MemorySaver  # type: ignore
        from agent_platform.orchestration.graph import build_graph
        from agent_platform.orchestration.hitl import HITLGate
        from agent_platform.orchestration.prompts import get_system_prompt
    except Exception as exc:
        warn(f"跳过：未安装 langgraph（{exc}）。安装后 pip install langgraph 可验证。")
        record("LangGraph ReAct 回路", False, "langgraph 未安装")
        return

    audit = Audit(fallback_dir=os.path.join(ROOT, "data", "audit"))
    registry = ToolRegistry(audit=audit)
    # 用 mock kb_search 覆盖默认注册
    register_all(registry)
    registry.unregister("kb_search")
    registry.register(
        ToolSpec(
            name="kb_search",
            description="在知识库检索，返回带 chunk_id 的片段",
            input_schema={
                "type": "object", "required": ["query"], "additionalProperties": False,
                "properties": {"query": {"type": "string"},
                               "top_k": {"type": "integer", "default": 8}},
            },
            permission=Permission.READ_ONLY, hitl_required=False,
        ),
        mock_kb_search_impl,
    )

    cfg = LLMConfig.from_env()
    client = LLMClient(cfg)
    gate = HITLGate(audit=audit)
    budget = BudgetGuard()
    graph = build_graph(client, registry, gate, budget,
                        checkpointer=MemorySaver(), audit=audit,
                        system_prompt=get_system_prompt())

    if not llm_ok:
        warn("LLM 不可达，跳过真实 ReAct 回路（仅验证图可构建）")
        record("LangGraph ReAct 回路", False, "LLM 不可达")
        return

    thread_id = f"th-smoke-{int(time.time())}"
    initial = {
        "messages": [{"role": "user", "content": "STW-2100 的 VDD 工作范围是多少？请给出引用。"}],
        "session_id": "S-SMOKE",
        "thread_id": thread_id,
        "user_id": "eng_smoke",
    }
    try:
        result = graph.invoke(initial, {"configurable": {"thread_id": thread_id}})
    except Exception as exc:
        fail(f"图执行失败：{type(exc).__name__}: {exc}")
        traceback.print_exc()
        record("LangGraph ReAct 回路", False, str(exc))
        return

    answer = result.get("final_answer") or ""
    print(f"    答案：{answer[:400]}")
    print(f"    tool_calls_used={result.get('tool_calls_used')} "
          f"tokens_used={result.get('tokens_used')}")
    has_cite = _has_citation(answer)
    called_tool = int(result.get("tool_calls_used", 0) or 0) >= 1
    if has_cite and called_tool:
        record("LangGraph ReAct 回路（先调工具 + 带引用）", True)
    else:
        warn(f"引用={has_cite} 调用工具={called_tool}（模型行为可能波动）")
        record("LangGraph ReAct 回路（先调工具 + 带引用）", False,
               f"cite={has_cite} tools={called_tool}")


# ----------------------------------------------------------------------
# 5) HITL 流程
# ----------------------------------------------------------------------
def test_hitl() -> None:
    section("5. HITL 流程（审批 → 工具只执行一次）")
    try:
        from langgraph.checkpoint.memory import MemorySaver  # type: ignore
        from langgraph.types import Command  # type: ignore
        from agent_platform.orchestration.graph import build_graph
        from agent_platform.orchestration.hitl import HITLGate
    except Exception as exc:
        warn(f"跳过：未安装 langgraph（{exc}）")
        record("HITL 流程", False, "langgraph 未安装")
        return

    counter = {"exec": 0}

    def write_impl(args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        counter["exec"] += 1
        return {"report_id": "R-0001", "uri": "file://report/R-0001.md"}

    class _FakeLLM:
        """假 LLM：先要求写报告，被拒/批准后再给最终答案。"""

        def __init__(self) -> None:
            self.step = 0

        def chat_sync(self, messages: List[Dict[str, Any]], **kwargs: Any) -> Dict[str, Any]:
            self.step += 1
            if self.step == 1:
                return {
                    "role": "assistant", "content": "",
                    "reasoning_content": "",
                    "tool_calls": [{
                        "id": "call_write_1", "name": "report_render",
                        "args": {"template": "daily", "data": {"lot": "L1"}},
                    }],
                    "usage": {"prompt": 10, "completion": 5, "total": 15},
                }
            return {
                "role": "assistant",
                "content": "报告已生成，编号 R-0001。",
                "reasoning_content": "",
                "tool_calls": [],
                "usage": {"prompt": 20, "completion": 8, "total": 28},
            }

    audit = Audit(fallback_dir=os.path.join(ROOT, "data", "audit"))
    registry = ToolRegistry(audit=audit)
    registry.register(make_write_tool(counter), write_impl)
    gate = HITLGate(audit=audit)
    budget = BudgetGuard()
    graph = build_graph(_FakeLLM(), registry, gate, budget,
                        checkpointer=MemorySaver(), audit=audit)

    thread_id = f"th-hitl-{int(time.time())}"
    initial = {
        "messages": [{"role": "user", "content": "生成 L1 的日报告"}],
        "session_id": "S-HITL", "thread_id": thread_id, "user_id": "eng_hitl",
    }
    config = {"configurable": {"thread_id": thread_id}}

    try:
        result = graph.invoke(initial, config)
    except Exception as exc:
        fail(f"HITL 图执行失败：{type(exc).__name__}: {exc}")
        traceback.print_exc()
        record("HITL 流程", False, str(exc))
        return

    pending = result.get("pending_hitl") or []
    print(f"    pending_hitl 数：{len(pending)}")
    if not pending:
        fail("未产生待审批项（pending_hitl 为空）")
        record("HITL 流程", False, "没有产生待审批项")
        return
    print(f"    确认卡片：{json.dumps(pending[0], ensure_ascii=False)[:280]}")
    record("HITL 触发审批", True, f"pending={len(pending)}")

    # 此时工具尚未执行
    if counter["exec"] != 0:
        fail(f"审批前工具已执行 {counter['exec']} 次（不应执行）")
        record("HITL 前置无副作用", False)
    else:
        record("HITL 前置无副作用", True, "工具执行次数=0")

    # 审批通过并续跑
    cid = pending[0]["tool_call_id"]
    try:
        result2 = graph.invoke(
            Command(resume={
                "tool_call_id": cid, "approved": True,
                "actor": "eng_hitl", "reason": "同意生成报告",
            }),
            config,
        )
    except Exception as exc:
        fail(f"approve 续跑失败：{type(exc).__name__}: {exc}")
        traceback.print_exc()
        record("HITL 审批续跑", False, str(exc))
        return

    print(f"    续跑后答案：{(result2.get('final_answer') or '')[:120]}")
    print(f"    工具实际执行次数：{counter['exec']}")
    if counter["exec"] == 1:
        record("HITL 审批后续跑", True, "工具执行 1 次")
    else:
        fail(f"工具执行了 {counter['exec']} 次（应恰好 1 次）")
        record("HITL 审批后续跑", False, f"exec={counter['exec']}")


# ----------------------------------------------------------------------
# 6) 预算闸门
# ----------------------------------------------------------------------
def test_budget() -> None:
    section("6. 预算闸门")
    budget = BudgetGuard()
    try:
        budget.check(est_tokens=10)
        budget.charge_tokens(prompt=100, completion=50)
        budget.charge_tool_call()
        record("预算正常路径", True, json.dumps(budget.snapshot(), ensure_ascii=False))
    except BudgetExceeded as exc:
        fail(f"正常路径不应超限：{exc}")
        record("预算正常路径", False, str(exc))

    # token 超限
    b2 = BudgetGuard(max_tokens=100)
    try:
        b2.check(est_tokens=200)
        fail("token 超限未被拦截")
        record("预算 token 超限", False)
    except BudgetExceeded as exc:
        record("预算 token 超限", True, f"reason={exc.reason}")

    # 轮次超限
    b3 = BudgetGuard(max_rounds=2)
    try:
        b3.check()
        b3.charge_round()
        b3.charge_round()
        b3.check()
        fail("轮次超限未被拦截")
        record("预算轮次超限", False)
    except BudgetExceeded as exc:
        record("预算轮次超限", True, f"reason={exc.reason}")

    # 工具调用超限
    b4 = BudgetGuard(max_tool_calls=2)
    try:
        b4.charge_tool_call()
        b4.charge_tool_call()
        b4.charge_tool_call()
        fail("工具调用超限未被拦截")
        record("预算工具调用超限", False)
    except BudgetExceeded as exc:
        record("预算工具调用超限", True, f"reason={exc.reason}")


# ----------------------------------------------------------------------
# 7) 审计落盘
# ----------------------------------------------------------------------
def test_audit() -> None:
    section("7. 审计日志落盘")
    audit_dir = os.path.join(ROOT, "data", "audit")
    audit = Audit(fallback_dir=audit_dir)
    print(f"    审计后端模式：{audit.mode}")
    audit.log(action="smoke_test", target="audit_self_check",
              args={"hello": "world"}, result={"ok": True},
              status="ok", duration_ms=1, session_id="S-SMOKE",
              user_id="eng_smoke", model="gpt-oss-20b")
    rows = audit.recent(limit=5)
    print(f"    读回记录数：{len(rows)}")
    if not rows:
        fail("审计记录读回为空")
        record("审计日志落盘", False, f"mode={audit.mode}")
        return
    last = rows[0]
    print(f"    最新记录：{json.dumps(last, ensure_ascii=False, default=str)[:280]}")
    has_hash = isinstance(last.get("prompt_hash"), str) and last["prompt_hash"].startswith("sha256:")
    record("审计日志落盘", True, f"mode={audit.mode}")
    record("审计哈希格式 sha256:xxxx", bool(has_hash),
           str(last.get("prompt_hash")))

    # 文件确实落盘
    found = []
    for name in ("audit.db", "audit.jsonl"):
        p = os.path.join(audit_dir, name)
        if os.path.exists(p) and os.path.getsize(p) > 0:
            found.append(name)
    print(f"    落盘文件：{found or '（内存/数据库模式）'}")
    record("审计文件落盘", bool(found) or audit.mode == "postgres",
           ", ".join(found))


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Agent 平台端到端冒烟测试")
    parser.add_argument("--skip-llm", action="store_true", help="跳过需要真实 LLM 的用例")
    args = parser.parse_args()

    print(f"{CYAN}Agent平台 —— 端到端冒烟测试{RESET}")
    print(f"仓库根目录：{ROOT}")

    llm_ok = False
    if args.skip_llm:
        section("1. LLM 连通性")
        warn("按 --skip-llm 跳过")
    else:
        llm_ok = test_llm_connectivity()

    registry = test_registry()
    test_single_tool_call(registry)
    test_react_loop(llm_ok)
    test_hitl()
    test_budget()
    test_audit()

    section("汇总")
    passed = sum(1 for r in _results if r["passed"])
    total = len(_results)
    for r in _results:
        flag = "PASS" if r["passed"] else "FAIL"
        print(f"  [{flag}] {r['name']}" + (f"  ({r['detail']})" if r["detail"] else ""))
    print(f"\n结果：{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
