"""RAG 检索链验证：验证 docs\01 §3.1 的关键参数与检索质量是否生效。

运行：
    $env:BGE_M3_PATH="E:\model\models\bge-m3"
    python scripts\verify_rag.py
"""
import os
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"  [{PASS if cond else FAIL}] {name}" + (f"  -- {detail}" if detail else ""))
    return bool(cond)


def main():
    from agent_platform.knowledge.embed import Embedder
    from agent_platform.knowledge.reranker import Reranker
    from agent_platform.knowledge.retriever import Retriever
    from agent_platform.knowledge.store import build_store

    print("\n=== 1. Embedding / Reranker 后端 ===")
    emb = Embedder()
    check("Embedding 为真实 bge-m3（非 hash 降级）",
          "hash" not in str(emb.backend).lower(), f"backend={emb.backend}")
    rr = Reranker(embedder=emb)
    check("Reranker 已加载", bool(rr.backend), f"backend={rr.backend}")

    print("\n=== 2. 存储与检索链 ===")
    store = build_store("sqlite", sqlite_path=r"data\kb\chunks.db")
    stats = store.stats() if hasattr(store, "stats") else {}
    n = stats.get("chunks", stats.get("total_chunks", -1))
    check("索引已加载", n > 0, f"{n} chunks, stats={ {k: v for k, v in stats.items() if isinstance(v, (int, float))} }")
    r = Retriever(store=store, embedder=emb, reranker=rr)
    check("Retriever 构建成功", True, type(r).__name__)

    print("\n=== 3. 检索参数（docs\\01 §3.1）===")
    check("RECALL_N=50", r.recall_n == 50, f"recall_n={r.recall_n}")
    check("TOP_K=6", r.top_k == 6, f"top_k={r.top_k}")
    check("RRF_K=60", r.rrf_k == 60, f"rrf_k={r.rrf_k}")
    check("检索上下文预算 ≤3000 token", r.retrieval_budget <= 3000,
          f"budget={r.retrieval_budget}")

    print("\n=== 4. 语义检索质量 ===")
    cases = [
        ("bin1 良率的口径是什么？分子分母怎么算？", ["bin1", "良率", "is_retest"]),
        ("T101_VDD_FUNCT 的上下限是多少？", ["VDD", "3.135", "T101"]),
        ("探针接触不良怎么排查？", ["探针", "接触", "针尖"]),
        ("STDF 里 PCRR 和 MRR 有什么区别？", ["PCRR", "MRR"]),
        ("良率突然下降怎么排查？", ["排查", "SOP", "下降"]),
    ]
    for q, keywords in cases:
        try:
            hits = r.retrieve(q)
            top = hits[0] if hits else None
            blob = " ".join((h.get("text") or "") for h in hits)
            hit_kw = [k for k in keywords if k.lower() in blob.lower()]
            check(f"命中: {q[:24]}", len(hits) > 0 and len(hit_kw) > 0,
                  f"{len(hits)} 条, 命中关键词 {hit_kw}"
                  + (f", top={top.get('chunk_id')}" if top else ""))
            if hits:
                check(f"  结构完整: {q[:18]}",
                      bool(top.get("chunk_id")) and bool(top.get("source")),
                      f"source={top.get('source')}")
        except Exception as e:
            check(f"命中: {q[:24]}", False, f"{type(e).__name__}: {e}")

    print("\n=== 5. 无关问题应低分拒答 ===")
    try:
        hits = r.retrieve("今天北京天气怎么样，顺便帮我订张去上海的高铁票")
        top_score = hits[0].get("score", 0) if hits else 0.0
        check("无关问题被阈值挡下", len(hits) == 0 or top_score < 0.35,
              f"{len(hits)} 条, top_score={top_score:.3f}")
    except Exception as e:
        check("无关问题被阈值挡下", False, f"{type(e).__name__}: {e}")

    print("\n=== 6. 检索链路 trace（docs\\01 §3.1 十步）===")
    try:
        res = r.retrieve_with_trace("bin1 良率的口径是什么？")
        steps = getattr(res, "stage_trace", None) or []
        check("trace 含多步记录", len(steps) >= 5, f"{len(steps)} 步")
        step_names = {s.get("name") for s in steps}
        check("ACL 过滤在检索前下推", any("ACL" in (n or "") for n in step_names),
              f"步骤: {sorted(x for x in step_names if x)}")
        check("RRF 融合已执行", any("RRF" in (n or "") for n in step_names))
        check("Reranker 精排已执行", any("Reranker" in (n or "") for n in step_names))
        for s in steps:
            print(f"      · step{s.get('step')} {s.get('name')}: "
                  f"{ {k: v for k, v in s.items() if k not in ('step', 'name')} }")
    except Exception as e:
        check("trace 可用", False, f"{type(e).__name__}: {e}")

    print("\n" + "=" * 62)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"结果: {passed}/{len(results)} 通过")
    for nme, ok, d in results:
        if not ok:
            print(f"  失败: {nme}  {d}")
    print("=" * 62)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
