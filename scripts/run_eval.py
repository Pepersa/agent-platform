#!/usr/bin/env python
"""评测命令行入口：跑 RAG 评测集并输出分层指标报告。

对应设计文档：
    docs/02_场景方案_STDF分析与测试用例生成.md
        §7.4 回归评测流水线 —— 离线回归 → 阈值判定 → LLM-as-Judge → 对比报告
        §7.5 LLM-as-Judge 的使用边界（judge 用不同模型避免自评偏差）
    docs/01_架构方案_RAG_Agent.md §2.12 评测层

用法示例::

    # 单模型跑全量
    python scripts/run_eval.py --model gpt-oss-20b

    # 多模型对比（同一份集）
    python scripts/run_eval.py --model gpt-oss-20b,qwen2.5-14b

    # 只跑 RAG 知识问答层
    python scripts/run_eval.py --categories L5 --limit 10

    # 只跑不评（跳过 Judge）
    python scripts/run_eval.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent_platform.eval.judge import JUDGE_MODEL_PAIRS, Judge  # noqa: E402
from agent_platform.eval.runner import EvalRunner, load_dataset  # noqa: E402
from agent_platform.knowledge.embed import Embedder  # noqa: E402
from agent_platform.knowledge.llm import LLMClient  # noqa: E402
from agent_platform.knowledge.reranker import Reranker  # noqa: E402
from agent_platform.knowledge.retriever import Retriever, UserContext  # noqa: E402
from agent_platform.knowledge.store import build_store  # noqa: E402

logger = logging.getLogger("run_eval")

DEFAULT_DATASET = os.path.join("agent_platform", "eval", "datasets", "v1.jsonl")
DEFAULT_STORE = os.path.join("data", "kb", "chunks.db")
DEFAULT_OUT = os.path.join("data", "eval_reports")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。

    Args:
        argv: 参数列表。

    Returns:
        参数命名空间。
    """
    p = argparse.ArgumentParser(
        description="RAG 评测 Runner（docs/02 §7.4 回归评测流水线）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--dataset", default=DEFAULT_DATASET, help="评测集 JSONL 路径")
    p.add_argument(
        "--model",
        default="gpt-oss-20b",
        help="被评模型 alias，逗号分隔可传多个做对比（如 gpt-oss-20b,qwen2.5-14b）",
    )
    p.add_argument("--judge-model", default=None, help="Judge 模型（默认按互评规则自动选）")
    p.add_argument(
        "--judge-timeout",
        type=float,
        default=900.0,
        help="Judge 单次调用超时秒数（CPU 推理需放大）",
    )
    p.add_argument(
        "--case-timeout",
        type=float,
        default=300.0,
        help="单条用例总超时秒数（CPU 推理需放大）",
    )
    p.add_argument(
        "--llm-timeout",
        type=float,
        default=300.0,
        help="被评模型单次生成调用超时秒数",
    )
    p.add_argument("--store", default=DEFAULT_STORE, help="chunk 索引路径（SQLite）")
    p.add_argument("--out-dir", default=DEFAULT_OUT, help="报告输出目录")
    p.add_argument(
        "--categories",
        default=None,
        help="只跑指定分层，逗号分隔（L1,L2,L3,L4,L5,L6）",
    )
    p.add_argument("--limit", type=int, default=None, help="限制条数")
    p.add_argument(
        "--dry-run", action="store_true", help="只跑不评（跳过 LLM-as-Judge）"
    )
    p.add_argument(
        "--security-level",
        default="confidential",
        help="检索用户的可见最高密级（模拟权限）",
    )
    p.add_argument(
        "--roles", default="test_eng,process_eng", help="检索用户角色（逗号分隔）"
    )
    p.add_argument("--verbose", action="store_true", help="输出调试日志")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """主流程。

    Args:
        argv: 参数列表。

    Returns:
        进程退出码（0 成功）。
    """
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    try:
        cases = load_dataset(args.dataset)
    except FileNotFoundError:
        logger.error("评测集不存在：%s", args.dataset)
        return 2
    logger.info("加载评测集 %s：%d 条", args.dataset, len(cases))

    categories = (
        [c.strip() for c in args.categories.split(",") if c.strip()]
        if args.categories else None
    )
    models = [m.strip() for m in args.model.split(",") if m.strip()]

    # 共享存储（多模型对比时用同一份索引，保证可比）
    store = build_store("sqlite", sqlite_path=args.store)
    embedder = Embedder()
    reranker = Reranker(embedder=embedder)

    user_ctx = UserContext(
        user_id="eval_user",
        roles=[r.strip() for r in args.roles.split(",") if r.strip()],
        max_security_level=args.security_level,
    )

    # 全部 chunk_id（用于引用可溯源率）
    valid_chunk_ids = _all_chunk_ids(store)

    summaries: dict[str, dict] = {}
    for model in models:
        logger.info("=" * 60)
        logger.info("开始评测模型：%s", model)
        llm = LLMClient(model=model, timeout=args.llm_timeout)
        retriever = Retriever(
            store=store, embedder=embedder, reranker=reranker, llm=llm, llm_model=model
        )
        # Judge 必须用**另一个模型**互评（docs/02 §7.5），且必须连到该模型自己的
        # llama-server 端点 —— llama-server 会忽略请求里的 model 字段，
        # 始终返回本机加载的模型，所以不能复用被评模型的 llm 客户端。
        judge = None
        if not args.dry_run:
            judge_model = args.judge_model or JUDGE_MODEL_PAIRS.get(model, "qwen2.5-14b")
            judge_llm = LLMClient(model=judge_model, timeout=args.judge_timeout)
            judge = Judge(model=model, judge_model=judge_model, llm=judge_llm)
            logger.info("Judge 配置：被评=%s，评判=%s（%s）", model, judge_model,
                        judge_llm.base_url)
        runner = EvalRunner(
            retriever=retriever, llm=llm, judge=judge, store=store,
            user_ctx=user_ctx, model=model,
        )
        outcomes = runner.run(
            cases, dry_run=args.dry_run, categories=categories, limit=args.limit,
            timeout_ms=int(args.case_timeout * 1000),
        )
        summary = runner.summarize(outcomes, valid_chunk_ids)
        gates = runner.gate_check(summary)
        summary["gates"] = gates
        summary["gate_passed"] = all(g["passed"] for g in gates) if gates else None
        summaries[model] = summary

        paths = runner.write_report(outcomes, summary, out_dir=args.out_dir, tag=model)
        logger.info("报告已输出：%s", paths)

        # 控制台摘要
        print("\n" + "=" * 66)
        print(f"评测摘要 —— {model}")
        print("=" * 66)
        for layer, vals in (summary.get("overall") or {}).items():
            for metric, value in vals.items():
                base, target = _target(metric)
                print(f"  {layer} {metric:<34} = {value:<12.4g}  基线 {base} → 目标 {target}")
        print(f"  失败归因：{summary.get('failure_attribution')}")
        print(f"  门禁：{'通过' if summary.get('gate_passed') else '未通过/无门禁项'}")
        for g in gates:
            flag = "PASS" if g["passed"] else "FAIL"
            print(
                f"    [{flag}] {g['layer']}.{g['metric']} = {g['value']:.4g} "
                f"{g['op']} {g['threshold']}"
            )
        print("=" * 66 + "\n")

    # 多模型对比报告
    if len(summaries) > 1:
        cmp_path = os.path.join(args.out_dir, "eval_compare.json")
        os.makedirs(args.out_dir, exist_ok=True)
        with open(cmp_path, "w", encoding="utf-8") as f:
            json.dump(summaries, f, ensure_ascii=False, indent=2)
        logger.info("多模型对比报告：%s", cmp_path)

    store.close()
    return 0


def _all_chunk_ids(store: object) -> list[str]:
    """从存储层取全部 chunk_id（用于引用可溯源率）。

    Args:
        store: 存储实例。

    Returns:
        chunk_id 列表。
    """
    try:
        conn = getattr(store, "conn", None)
        if conn is not None:
            rows = conn.execute("SELECT chunk_id FROM chunks").fetchall()
            return [r["chunk_id"] for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning("取 chunk_id 列表失败：%s", exc)
    return []


def _target(metric: str) -> tuple[str, str]:
    """取指标的目标值（基线 → 优化）。

    Args:
        metric: 指标名。

    Returns:
        ``(基线字符串, 目标字符串)``。
    """
    from agent_platform.eval.metrics import METRIC_TARGETS

    base, target = METRIC_TARGETS.get(metric, (None, None))
    return (str(base) if base is not None else "—",
            str(target) if target is not None else "—")


if __name__ == "__main__":
    raise SystemExit(main())
