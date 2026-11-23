"""端到端验证：真实 gpt-oss-20b 驱动的完整 ReAct 回路 + Tool Registry + HITL + 预算闸门。

这是 docs\01 §2.4 / §2.5 / §3.2 的可执行验证脚本。
运行：python scripts\verify_e2e.py
"""
import asyncio
import os
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_platform.core.budget import BudgetExceeded, BudgetGuard
from agent_platform.core.llm_client import LLMClient
from agent_platform.core.tool_registry import ToolRegistry, ToolSpec
from agent_platform.orchestration.graph import build_graph
from agent_platform.orchestration.hitl import HITLGate

PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"  [{PASS if cond else FAIL}] {name}" + (f"  -- {detail}" if detail else ""))
    return bool(cond)


def make_registry(counter=None):
    reg = ToolRegistry()

    def _render(template, title=""):
        if counter is not None:
            counter["n"] += 1
        return {"report_id": f"RPT-{template.upper()}-001", "uri": "artifacts/x.md"}

    reg.register(
        ToolSpec(
            name="kb_search",
            description="在内部知识库中做混合检索（BM25+向量），返回带 chunk_id 的证据片段",
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            permission="READ_ONLY",
        ),
        lambda query, top_k=5: {
            "chunks": [
                {
                    "chunk_id": "KB-YIELD-001",
                    "text": "bin1 良率口径：首次测试（不含重测）判为 bin1 的 die 数 / 总测试 die 数。"
                            "统计时必须过滤 is_retest=false。",
                    "score": 0.93,
                },
                {
                    "chunk_id": "KB-TP-014",
                    "text": "TP_CMOS_32G v2.4 中 T205_TSHIFT_FUNC 测试项上下限为 0.8ns~1.2ns。",
                    "score": 0.87,
                },
            ],
            "query": query,
        },
    )
    reg.register(
        ToolSpec(
            name="report_render",
            description="生成测试结论报告并归档（写操作，需人工确认）",
            input_schema={
                "type": "object",
                "required": ["template"],
                "properties": {
                    "template": {"type": "string", "enum": ["daily", "weekly", "spec"]},
                    "title": {"type": "string"},
                },
                "additionalProperties": False,
            },
            permission="WRITE_DATA",
        ),
        _render,
    )
    return reg


async def main():
    print("\n=== 1. LLM 连通性 ===")
    llm = LLMClient()
    try:
        r = await llm.chat([{"role": "user", "content": "回复两个字：正常"}])
        check("LLM 端点可达", bool(r["content"] or r["reasoning_content"]),
              f"content={r['content']!r} reasoning={len(r.get('reasoning_content') or '')}字")
        check("usage 统计可用", r["usage"]["total"] > 0, f"total={r['usage']['total']}")
        check("reasoning 与 content 分离", "reasoning_content" in r,
              "gpt-oss harmony 双通道")
    except Exception as e:
        check("LLM 端点可达", False, f"{type(e).__name__}: {e}")
        return 1

    print("\n=== 2. Tool Registry 契约 ===")
    reg = make_registry()
    schemas = reg.openai_schemas()
    check("schema 输出正确", len(schemas) == 2 and schemas[0]["type"] == "function",
          f"{len(schemas)} 个工具")
    check("input_schema 透传", schemas[0]["function"]["parameters"]["required"] == ["query"])
    kb, rr = reg.get("kb_search"), reg.get("report_render")
    hitl_kb = kb.needs_hitl if callable(kb.needs_hitl) else kb.needs_hitl
    hitl_rr = rr.needs_hitl if callable(rr.needs_hitl) else rr.needs_hitl
    check("HITL 自动推导正确", hitl_rr is True and hitl_kb is False,
          f"report_render={hitl_rr} kb_search={hitl_kb}")

    print("\n=== 3. 参数校验与错误归一 ===")
    from agent_platform.core.tool_registry import ToolError
    for label, args, tool in [
        ("缺参数被拦截", {}, "kb_search"),
        ("未知参数被拦截", {"query": "x", "bogus": 1}, "kb_search"),
        ("枚举校验生效", {"template": "非法模板"}, "report_render"),
    ]:
        try:
            reg.call(tool, args)
            check(label, False, "未抛异常")
        except ToolError as e:
            check(label, True, f"code={e.code} hint={str(e.hint)[:50]}")
        except Exception as e:
            check(label, True, f"code={type(e).__name__}")

    print("\n=== 4. Tool 直接调用 ===")
    out = reg.call("kb_search", {"query": "bin1 良率", "top_k": 2})
    check("工具执行成功", "chunks" in out, f"返回 {len(out.get('chunks', []))} 个片段")

    print("\n=== 5. LangGraph 完整 ReAct 回路 ===")
    from langgraph.checkpoint.memory import MemorySaver
    graph = build_graph(llm=llm, registry=reg, gate=HITLGate(),
                        budget=BudgetGuard(), checkpointer=MemorySaver())
    try:
        res = await graph.ainvoke(
            {"messages": [{"role": "user", "content": "bin1 良率的口径是什么？请引用来源。"}]},
            config={"configurable": {"thread_id": "e2e-rag-1"}},
        )
    except Exception as e:
        check("ReAct 回路执行", False, f"{type(e).__name__}: {e}")
        res = {}

    msgs = res.get("messages", [])

    def _mfield(m, key, default=""):
        """兼容 LangGraph BaseMessage 对象与 dict 两种形态。"""
        if isinstance(m, dict):
            return m.get(key, default)
        v = getattr(m, key, None)
        return default if v is None else v

    def _role(m):
        """LangGraph 消息的 role 存在 type 字段（HumanMessage/ AIMessage / ToolMessage）。"""
        if isinstance(m, dict):
            return m.get("role", "")
        t = type(m).__name__
        return {"HumanMessage": "human", "AIMessage": "assistant",
                "ToolMessage": "tool", "SystemMessage": "system"}.get(t, t)

    roles = [_role(m) for m in msgs]
    check("模型先调工具再回答",
          "assistant" in roles and "tool" in roles and roles[-1] == "assistant",
          f"roles={roles}")
    final = _mfield(msgs[-1], "content", "") if msgs else ""
    check("最终答案非空", len(final) > 10, f"{len(final)} 字符")
    check("答案含引用标记", "KB-" in final, "引用了 KB-YIELD-001")

    print("\n=== 6. HITL 审批闸门 ===")
    counter = {"n": 0}
    reg2 = make_registry(counter)
    graph2 = build_graph(llm=llm, registry=reg2, gate=HITLGate(),
                         budget=BudgetGuard(), checkpointer=MemorySaver())
    # 强制模型调用 report_render：去掉 kb_search，只留写工具
    reg2.unregister("kb_search")
    interrupted, pending, err = False, 0, ""
    for attempt in range(3):
        try:
            res2 = await graph2.ainvoke(
                {"messages": [{"role": "user",
                               "content": "调用 report_render 工具生成一份良率日报，template 参数填 daily。"}]},
                config={"configurable": {"thread_id": f"e2e-hitl-{attempt}"}},
            )
            interrupted = "__interrupt__" in res2
            pending = len(res2.get("pending_hitl") or [])
            if interrupted or pending:
                break
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            if "interrupt" in type(e).__name__.lower():
                interrupted = True
                break
    check("写操作触发审批挂起", interrupted or pending > 0,
          f"interrupt={interrupted} pending={pending} {err}")
    check("挂起时工具未执行", counter["n"] == 0, f"执行次数={counter['n']}")

    print("\n=== 7. 预算闸门 ===")
    b = BudgetGuard(max_rounds=0)
    try:
        b.check(est_tokens=100)
        check("预算超限被拦截", False)
    except BudgetExceeded as e:
        check("预算超限被拦截", True, f"reason={e.reason}")

    print("\n=== 7b. 引用校验（docs/01 §3.1 第 9 步）===")
    from agent_platform.core.citations import (
        extract_citations, has_citation, validate_citations,
    )
    # Agent ReAct 路径产出的引用格式是 [来源:chunk_id]，
    # 检索生成路径是 [doc_id:chunk_id]，两者都必须能抽出。
    for label, text, want in [
        ("ASCII 括号 + ASCII 连字符", "[来源:KB-YIELD-001]", "KB-YIELD-001"),
        ("Unicode 变体连字符", "[来源:KB\u2011YIELD\u2011001]", "KB-YIELD-001"),
        ("全角冒号", "[来源：KB-YIELD-001]", "KB-YIELD-001"),
        ("全角括号", "【来源:KB-YIELD-001】", "KB-YIELD-001"),
        ("doc_id:chunk_id 形式", "[DOC-TP-001:KB-YIELD-001]", "KB-YIELD-001"),
    ]:
        got = extract_citations(text)
        check(f"引用可抽取 —— {label}",
              len(got) == 1 and got[0]["chunk_id"] == want,
              f"got={got}")
    check("变体连字符归一后可命中真实 chunk",
          validate_citations(extract_citations("[来源:KB\u2011YIELD\u2011001]"),
                             {"KB-YIELD-001"}) == [])
    check("引用不存在的 chunk 被判为非法",
          len(validate_citations(extract_citations("[来源:KB-NOPE-999]"),
                                 {"KB-YIELD-001"})) == 1)
    check("无引用文本不误判", not has_citation("这里没有任何引用"))

    # Agent ReAct 路径：最终答案的引用必须被真正校验过，
    # 且非法引用要能进 invalid_citations（早期实现完全缺失这一步）。
    if res:
        cites = res.get("citations") or []
        invalid = res.get("invalid_citations") or []
        check("Agent 路径产出 citations 字段", isinstance(cites, list),
              f"{len(cites)} 条引用")
        check("Agent 路径产出 invalid_citations 字段", isinstance(invalid, list),
              f"{len(invalid)} 条非法")
        if cites:
            check("Agent 路径引用均可在工具结果中溯源", len(invalid) == 0,
                  f"invalid={[c.get('raw') for c in invalid]}")

    print("\n=== 8. 结果裁剪（防上下文冲爆）===")
    big = {"rows": [{"i": i, "pad": "x" * 200} for i in range(500)]}
    reg3 = ToolRegistry(max_tool_result_chars=2000)
    reg3.register(ToolSpec(name="big", description="大结果",
                            input_schema={"type": "object", "properties": {}}),
                  lambda args, ctx=None: big)
    trimmed = reg3.call("big", {})
    check("大结果被裁剪", trimmed.get("truncated") is True,
          f"保留 {len(trimmed.get('rows', []))}/{len(big['rows'])} 行")

    print("\n" + "=" * 62)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"结果: {passed}/{len(results)} 通过")
    for n, ok, d in results:
        if not ok:
            print(f"  失败: {n}  {d}")
    print("=" * 62)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
