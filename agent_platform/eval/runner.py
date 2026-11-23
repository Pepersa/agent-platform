"""评测 Runner —— 回归评测流水线。

对应设计文档：
    docs/02_场景方案_STDF分析与测试用例生成.md
        §6 评测指标体系 —— 四层指标
        §6.6 指标归因分解 —— 失败闭环（检索未召回 / 召回了排序靠后 /
          召回了但模型误用 / SQL错 / 数据缺失 / 意图理解错）
        §7.2 规模与分层 —— L1~L6 分层配比
        §7.4 回归评测流水线 —— 静态门禁 → 离线回归 → 阈值判定 →
          LLM-as-Judge → 对比报告 → 灰度
        §7.5 LLM-as-Judge 的使用边界
    docs/01_架构方案_RAG_Agent.md
        §2.12 评测层 —— 「离线回测」「LLM-as-Judge」「检索质量评估」「回归与门禁」

流水线（docs/02 §7.4）：
    ① 静态门禁（格式校验）→ ② 离线回归（全量评测集，可并行）
    → ③ 阈值判定（门禁规则）→ ④ LLM-as-Judge 自动评分
    → ⑤ 生成对比报告（按分层拆分 + 失败归因分解）
"""

from __future__ import annotations

import csv
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from agent_platform.eval import metrics as M
from agent_platform.eval.judge import Judge

logger = logging.getLogger(__name__)

__all__ = ["EvalRunner", "EvalCase", "EvalOutcome", "GATE_RULES", "load_dataset"]


# ---------------------------------------------------------------------------
# 门禁规则（docs/02 §7.4 步骤 ③）
# ---------------------------------------------------------------------------
GATE_RULES: dict[str, dict[str, Any]] = {
    "L2": {
        "recall_at_10": {"op": ">=", "value": 0.97, "vs_main_max_drop": 0.01},
        "ndcg_at_10": {"op": ">=", "value": 0.82, "vs_main_max_drop": 0.01},
    },
    "L3": {
        "execution_accuracy": {"op": ">=", "value": 0.85, "vs_main_max_drop": 0.02},
        "overall_hallucination_rate": {"op": "<=", "value": 0.05},
    },
    "L1": {
        "e2e_latency_p95": {"op": "<=", "value": 25000},
        "cost_per_request_cny": {"op": "<=", "value": 0.15},
    },
}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class EvalCase:
    """一条评测用例。

    对应 docs/02 §7.2 的评测集记录格式。

    Attributes:
        id: 用例 ID（如 ``L1-001``）。
        category: 分层（L1~L6）。
        query: 用户问题。
        gold_answer: 标准答案。
        gold_chunks: gold chunk_id 列表（检索类用）。
        gold_sql: 标准 SQL（问数类用）。
        tags: 标签。
        unanswerable: 是否为不可回答用例（对抗集）。
        raw: 原始 JSON 记录。
    """

    id: str
    category: str
    query: str
    gold_answer: str = ""
    gold_chunks: list[str] = field(default_factory=list)
    gold_sql: str = ""
    tags: list[str] = field(default_factory=list)
    unanswerable: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EvalCase":
        """从 JSON 记录构造。

        Args:
            d: JSON 记录。

        Returns:
            :class:`EvalCase`。
        """
        return cls(
            id=str(d.get("id") or ""),
            category=str(d.get("category") or ""),
            query=str(d.get("query") or ""),
            gold_answer=str(d.get("gold_answer") or ""),
            gold_chunks=list(d.get("gold_chunks") or []),
            gold_sql=str(d.get("gold_sql") or ""),
            tags=list(d.get("tags") or []),
            unanswerable=bool(d.get("unanswerable") or False),
            raw=dict(d),
        )


@dataclass
class EvalOutcome:
    """一条用例的评测结果。

    Attributes:
        case: 用例。
        answer: 模型回答。
        hits: 检索命中块。
        retrieved: 检索到的 chunk_id 列表。
        citations: 引用列表。
        invalid_citations: 非法引用列表。
        refused: 是否拒答。
        elapsed_ms: 端到端耗时。
        ttft_ms: 首 token 延迟（若可得）。
        tool_calls: 工具调用次数。
        usage: token 用量。
        judge: Judge 评分结果。
        error: 错误信息。
        extra: 额外字段（如用例生成结果、SQL 执行结果）。
    """

    case: EvalCase
    answer: str = ""
    hits: list[dict[str, Any]] = field(default_factory=list)
    retrieved: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    invalid_citations: list[str] = field(default_factory=list)
    refused: bool = False
    elapsed_ms: float = 0.0
    ttft_ms: float = 0.0
    tool_calls: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    judge: dict[str, Any] | None = None
    error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    @property
    def retrieved_ids(self) -> list[str]:
        """检索到的 chunk_id 列表。"""
        return [h.get("chunk_id") for h in self.hits if h.get("chunk_id")]

    def to_metric_dict(self) -> dict[str, Any]:
        """转成指标函数可消费的 dict。"""
        d: dict[str, Any] = {
            "retrieved": self.retrieved_ids,
            "gold_chunks": self.case.gold_chunks,
            "answer": self.answer,
            "citations": self.citations,
            "evidence": [h.get("text") or "" for h in self.hits],
            "ttft_ms": self.ttft_ms,
            "e2e_ms": self.elapsed_ms,
            "tool_calls": self.tool_calls,
            "usage": self.usage,
            "unanswerable": self.case.unanswerable,
            "refused": self.refused,
            "correct": bool(self.extra.get("correct")),
            "exact_match": bool(self.extra.get("exact_match")),
            "executable": bool(self.extra.get("executable")),
            "semantic_correct": bool(self.extra.get("semantic_correct")),
            "need_clarify": bool(self.extra.get("need_clarify")),
            "clarified": bool(self.extra.get("clarified")),
            "multi_hop": self.case.category == "L2",
            "should_abstain": self.case.unanswerable,
            "abstained": self.refused,
            "cases": self.extra.get("cases") or [],
            "similarities": self.extra.get("similarities") or [],
            "format_valid": self.extra.get("format_valid"),
            "top1_correct": self.extra.get("top1_correct"),
            "top3_hit": self.extra.get("top3_hit"),
            "wrong_confident": self.extra.get("wrong_confident"),
            "has_evidence": self.extra.get("has_evidence"),
            "has_hallucination": bool(self.extra.get("has_hallucination")),
            "hallucinated_numbers": self.extra.get("hallucinated_numbers", 0),
            "total_numbers": self.extra.get("total_numbers", 0),
            "fake_citations": len(self.invalid_citations),
            "total_citations": len(self.citations),
            "contradictory_claims": self.extra.get("contradictory_claims", 0),
            "total_claims": self.extra.get("total_claims", 0),
            "unsupported_claims": self.extra.get("unsupported_claims", 0),
            "total_factual_claims": self.extra.get("total_factual_claims", 0),
            "pred": self.extra.get("pred"),
            "gold": self.extra.get("gold"),
        }
        return d


# ---------------------------------------------------------------------------
# 数据集加载
# ---------------------------------------------------------------------------
def load_dataset(path: str) -> list[EvalCase]:
    """加载 JSONL 评测集。

    对应 docs/02 §7.2「评测集管理（Git 管理 + 评测集 Schema 自研）」。

    Args:
        path: JSONL 文件路径。

    Returns:
        :class:`EvalCase` 列表。
    """
    cases: list[EvalCase] = []
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                d = json.loads(line)
            except Exception as exc:  # noqa: BLE001
                logger.warning("评测集第 %d 行解析失败：%s", len(cases) + 1, exc)
                continue
            cases.append(EvalCase.from_dict(d))
    return cases


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
class EvalRunner:
    """评测 Runner。

    Args:
        retriever: RAG 检索链（可为 None，则只跑静态检查）。
        llm: LLM 客户端（用于 Judge 与生成）。
        judge: Judge 实例（可为 None，``--dry-run`` 时不评分）。
        store: chunk 存储（用于取全部 chunk_id 算引用可溯源率）。
        user_ctx: 检索用的用户上下文。
        model: 被评模型 alias。
    """

    def __init__(
        self,
        retriever: Any = None,
        llm: Any = None,
        judge: Judge | None = None,
        store: Any = None,
        user_ctx: Any = None,
        model: str = "gpt-oss-20b",
    ) -> None:
        self.retriever = retriever
        self.llm = llm
        self.judge = judge
        self.store = store
        self.user_ctx = user_ctx
        self.model = model

    # ==================================================================
    def run(
        self,
        cases: Sequence[EvalCase],
        dry_run: bool = False,
        categories: Sequence[str] | None = None,
        limit: int | None = None,
        timeout_ms: int = 90000,
    ) -> list[EvalOutcome]:
        """跑评测集。

        Args:
            cases: 用例列表。
            dry_run: 只跑不评（不调用 Judge，不计分）。
            categories: 只跑指定分层（如 ``["L1", "L5"]``）。
            limit: 限制条数。
            timeout_ms: 单条用例超时（毫秒）。超时记为失败并继续，
                避免个别慢推理请求阻塞整个回归流水线。

        Returns:
            结果列表。
        """
        selected = list(cases)
        if categories:
            want = {c.upper() for c in categories}
            selected = [c for c in selected if c.category.upper() in want]
        if limit:
            selected = selected[: int(limit)]
        logger.info(
            "开始评测：%d 条（dry_run=%s, model=%s）", len(selected), dry_run, self.model
        )

        outcomes: list[EvalOutcome] = []
        for i, case in enumerate(selected, start=1):
            t0 = time.perf_counter()
            outcome = self._run_case_with_timeout(case, dry_run, timeout_ms)
            outcome.elapsed_ms = (time.perf_counter() - t0) * 1000
            outcomes.append(outcome)
            logger.info(
                "[%d/%d] %s (%s) 命中=%d 引用=%d 拒答=%s 耗时=%.0fms%s",
                i, len(selected), case.id, case.category,
                len(outcome.retrieved_ids), len(outcome.citations),
                outcome.refused, outcome.elapsed_ms,
                f" 错误={outcome.error}" if outcome.error else "",
            )
        return outcomes

    # ==================================================================
    def _run_case_with_timeout(
        self, case: EvalCase, dry_run: bool, timeout_ms: int
    ) -> EvalOutcome:
        """在独立线程中跑单条用例并施加超时。

        Args:
            case: 用例。
            dry_run: 只跑不评。
            timeout_ms: 超时毫秒。

        Returns:
            :class:`EvalOutcome`（超时则 ``error`` 字段说明原因）。
        """
        import threading

        box: dict[str, EvalOutcome] = {}

        def _target() -> None:
            try:
                box["outcome"] = self.run_case(case, dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001
                o = EvalOutcome(case=case)
                o.error = f"{type(exc).__name__}: {exc}"
                box["outcome"] = o

        th = threading.Thread(target=_target, daemon=True)
        th.start()
        th.join(timeout=max(1.0, timeout_ms / 1000.0))
        if "outcome" in box:
            return box["outcome"]
        outcome = EvalOutcome(case=case)
        outcome.error = f"TIMEOUT after {timeout_ms}ms"
        return outcome

    # ==================================================================
    def run_case(self, case: EvalCase, dry_run: bool = False) -> EvalOutcome:
        """跑单条用例。

        Args:
            case: 用例。
            dry_run: 只跑不评。

        Returns:
            :class:`EvalOutcome`。
        """
        outcome = EvalOutcome(case=case)
        if self.retriever is None:
            outcome.error = "未配置 retriever"
            return outcome
        try:
            res = self.retriever.answer(
                case.query,
                self.user_ctx,
                top_k=6,
                recall_n=50,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("用例 %s 执行失败", case.id)
            outcome.error = str(exc)
            return outcome

        outcome.answer = res.get("answer") or ""
        outcome.hits = res.get("hits") or []
        outcome.retrieved = [h.get("chunk_id") for h in outcome.hits]
        outcome.citations = [c.get("raw") for c in (res.get("citations") or [])]
        outcome.invalid_citations = [
            c.get("raw") for c in (res.get("invalid_citations") or [])
        ]
        outcome.refused = bool(res.get("refused")) or bool(res.get("reason"))
        outcome.usage = res.get("usage") or {}
        outcome.extra["reason"] = res.get("reason")
        outcome.extra["stage_trace"] = res.get("stage_trace")

        # --- 客观校验（可自动评估部分，docs/02 §7.5）---
        outcome.extra["total_numbers"] = len(
            _numbers(outcome.answer)
        )
        evidence_nums = set()
        for h in outcome.hits:
            evidence_nums.update(_numbers(h.get("text") or ""))
        outcome.extra["hallucinated_numbers"] = sum(
            1 for n in _numbers(outcome.answer) if n not in evidence_nums
        )
        outcome.extra["total_citations"] = len(outcome.citations)
        outcome.extra["has_hallucination"] = bool(
            outcome.extra["hallucinated_numbers"] or outcome.invalid_citations
        )
        outcome.extra["correct"] = self._check_correct(case, outcome)
        outcome.extra["exact_match"] = outcome.extra["correct"]

        # --- L4：用例生成 + Lint（docs/02 §4.1 / §4.4 / §6.4.2）---
        # 只有 L4（用例生成层）才产出结构化用例，其余分层的 case_* 指标保持 0。
        if case.category.upper() == "L4":
            self._eval_case_generation(outcome)

        # --- LLM-as-Judge（dry_run 时跳过）---
        if not dry_run and self.judge is not None:
            context = "\n\n".join(
                f"[{h.get('doc_id')}:{h.get('chunk_id')}] {h.get('text')}"
                for h in outcome.hits
            )
            jr = self.judge.judge(
                query=case.query,
                answer=outcome.answer,
                context=context,
                gold_answer=case.gold_answer,
            )
            outcome.judge = jr.to_dict()
        return outcome

    # ==================================================================
    def _eval_case_generation(self, outcome: EvalOutcome) -> None:
        """L4 用例生成层评测：生成结构化用例并做 Lint。

        对应 docs/02 §4.1（结构化用例 JSON）与 §4.4（规则 R01–R10）。
        产出写入 ``outcome.extra``：

        * ``cases``：抽取到的用例列表（供 ``compute_l3_case_metrics`` 统计
          字段完整率 / 限值可溯源率 / 单位一致率 / 上下限合理率）；
        * ``format_valid``：是否至少抽到一条结构完整的用例；
        * ``lint_report`` / ``lint_summary``：``case_lint`` 的原始结果；
        * ``similarities``：与 gold 的语义相似度（用于重复率统计）。

        生成路径：优先用检索到的证据让 LLM 输出**结构化 JSON 用例**；
        LLM 不可用或未产出 JSON 时，退化为从散文回答里抽取 JSON 片段。

        Args:
            outcome: 评测结果（原地写入 extra）。
        """
        answer = outcome.answer or ""
        cases = self._generate_structured_cases(outcome) or _extract_cases(answer)
        outcome.extra["cases"] = cases
        outcome.extra["format_valid"] = bool(cases)
        outcome.extra["similarities"] = [
            _text_similarity(answer, outcome.case.gold_answer or "")
        ]

        if not cases:
            return
        try:
            from agent_platform.knowledge.tools_ext import case_lint

            lint = case_lint(cases)
            outcome.extra["lint_report"] = lint.get("report") or []
            outcome.extra["lint_summary"] = lint.get("summary") or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("case_lint 执行失败：%s", exc)
            outcome.extra["lint_report"] = []
            outcome.extra["lint_summary"] = {}

    # ==================================================================
    def _generate_structured_cases(self, outcome: EvalOutcome) -> list[dict[str, Any]]:
        """让 LLM 基于检索证据输出结构化测试用例 JSON。

        对应 docs/02 §4.1 的用例 JSON 结构；限值必须来自参考资料（规则 R01）。

        Args:
            outcome: 评测结果（含 hits 与 query）。

        Returns:
            用例 dict 列表；生成失败时返回空列表。
        """
        llm = self.llm
        if llm is None or not outcome.hits:
            return []
        context = "\n\n".join(
            f"[{h.get('doc_id')}:{h.get('chunk_id')}] {h.get('text')}"
            for h in outcome.hits[:6]
        )
        system = (
            "你是技术文档用例生成专家。**只能**依据下方【参考资料】生成用例，"
            "严禁编造任何限值、单位或测试项编号。\n"
            "输出**严格的 JSON 数组**，每个元素为一个测试用例对象，字段如下：\n"
            "  case_id (string, 必填)、title (string, 必填)、category (string)、\n"
            "  dft_method (string)、priority (string)、\n"
            "  preconditions (array, 每项 {type, value}，必须含 environment 且 value 含温度，"
            "必须含 sample 且 value 含 lot/wafer/die 数量)、\n"
            "  test_steps (array, 每项 {step, action, params}，params 为非空对象)、\n"
            "  test_items (array, 每项 {test_num, name, unit, lower_limit, typical, "
            "upper_limit, measure}，unit 必填，限值必须出自参考资料)、\n"
            "  expected_result (string)、pass_criteria (string，二值 PASS/FAIL)、"
            "fail_criteria (string)、\n"
            "  covered_failure_modes (array, 每项 {mode_id, mode, coverage})、\n"
            "  traceability (object, 含 spec_ref 数组，每项 {doc_id, section, page, quote}，"
            "quote 必须是参考资料中的原文片段)\n"
            "只输出 JSON 数组，不要输出任何解释文字。"
        )
        user = f"【参考资料】\n{context}\n\n【需求】\n{outcome.case.query}\n\n【JSON 用例数组】"
        try:
            resp = llm.chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                model=self.model,
                max_tokens=4096,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("用例生成 LLM 调用失败：%s", exc)
            return []
        if not getattr(resp, "ok", False) or not (resp.text or "").strip():
            return []
        return _extract_cases(resp.text)

    # ==================================================================
    @staticmethod
    def _check_correct(case: EvalCase, outcome: EvalOutcome) -> bool:
        """用规则做一次「答案是否命中要点」的粗判（数值题）。

        对数值类用例：标准答案中的关键数值是否出现在回答中。

        Args:
            case: 用例。
            outcome: 结果。

        Returns:
            是否命中。
        """
        if case.unanswerable:
            return outcome.refused
        if not case.gold_answer:
            return False
        gold_nums = _numbers(case.gold_answer)
        if not gold_nums:
            # 文本型：用关键 token 覆盖判断
            keys = [k for k in _tokens(case.gold_answer) if len(k) >= 2][:8]
            if not keys:
                return False
            ans_low = (outcome.answer or "").lower()
            hit = sum(1 for k in keys if k.lower() in ans_low)
            return hit / max(1, len(keys)) >= 0.6
        ans_nums = set(_numbers(outcome.answer))
        hit = sum(1 for n in gold_nums if n in ans_nums)
        return hit / len(gold_nums) >= 0.8

    # ==================================================================
    def summarize(
        self,
        outcomes: Sequence[EvalOutcome],
        valid_chunk_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        """汇总评测结果（按分层拆分 + 失败归因分解）。

        对应 docs/02 §6.6 与 §7.4 步骤 ⑤。

        Args:
            outcomes: 结果列表。
            valid_chunk_ids: 真实 chunk_id 集合。

        Returns:
            报告字典。
        """
        per_case = [o.to_metric_dict() for o in outcomes]
        overall = M.compute_all(per_case, valid_chunk_ids)

        by_category: dict[str, Any] = {}
        for cat in sorted({o.case.category for o in outcomes}):
            sub = [o for o in outcomes if o.case.category == cat]
            sub_metrics = [o.to_metric_dict() for o in sub]
            by_category[cat] = {
                "count": len(sub),
                "L2": M.compute_l2_metrics(sub_metrics, valid_chunk_ids),
                "L3": {
                    **M.compute_l3_numerical_metrics(sub_metrics),
                    **M.compute_l3_case_metrics(sub_metrics),
                    **M.compute_l3_hallucination_metrics(sub_metrics),
                    **M.compute_l3_attribution_metrics(sub_metrics),
                },
                "L1": M.compute_l1_metrics(sub_metrics),
            }

        # --- 失败归因分解（docs/02 §6.6）---
        attribution: dict[str, int] = {}
        for o in outcomes:
            label = M.attribute_failure(o.to_metric_dict())
            attribution[label] = attribution.get(label, 0) + 1

        # --- Judge 分数汇总 ---
        judge_scores = [o.judge["overall"] for o in outcomes
                        if o.judge and o.judge.get("ok")]
        judge_dims: dict[str, list[float]] = {}
        for o in outcomes:
            if not o.judge or not o.judge.get("ok"):
                continue
            for k, v in (o.judge.get("dimensions") or {}).items():
                judge_dims.setdefault(k, []).append(float(v))

        return {
            "model": self.model,
            "case_count": len(outcomes),
            "overall": overall,
            "by_category": by_category,
            "failure_attribution": attribution,
            "judge": {
                "scored": len(judge_scores),
                "mean_overall": (sum(judge_scores) / len(judge_scores))
                if judge_scores else 0.0,
                "dimensions": {
                    k: sum(v) / len(v) for k, v in judge_dims.items()
                },
            },
            "refusal_rate": (
                sum(1 for o in outcomes if o.refused) / max(1, len(outcomes))
            ),
            "invalid_citation_total": sum(len(o.invalid_citations) for o in outcomes),
        }

    # ==================================================================
    def gate_check(self, summary: dict[str, Any]) -> list[dict[str, Any]]:
        """阈值判定（docs/02 §7.4 步骤 ③）。

        Args:
            summary: :meth:`summarize` 的输出。

        Returns:
            门禁结果列表，每项 ``{layer, metric, value, threshold, op, passed}``。
        """
        results: list[dict[str, Any]] = []
        for layer, rules in GATE_RULES.items():
            layer_metrics = summary.get("overall", {}).get(layer, {})
            for metric, rule in rules.items():
                value = layer_metrics.get(metric)
                if value is None:
                    continue
                threshold = rule.get("value")
                op = rule.get("op", ">=")
                if op == ">=":
                    passed = value >= threshold
                else:
                    passed = value <= threshold
                results.append(
                    {
                        "layer": layer,
                        "metric": metric,
                        "value": value,
                        "threshold": threshold,
                        "op": op,
                        "passed": passed,
                    }
                )
        return results

    # ==================================================================
    def write_report(
        self,
        outcomes: Sequence[EvalOutcome],
        summary: dict[str, Any],
        out_dir: str | None = None,
        tag: str = "",
    ) -> dict[str, str]:
        """输出报告（JSON + Markdown + CSV）。

        对应 docs/02 §7.4 步骤 ⑤「生成对比报告」。

        Args:
            outcomes: 结果列表。
            summary: 汇总结果。
            out_dir: 输出目录；None 时取统一数据目录下的
                ``eval_reports/``（可通过 ``AGENT_DATA_DIR`` 整体外置）。
            tag: 文件名标签（通常为模型 alias）。

        Returns:
            ``{"json": path, "markdown": path, "csv": path}``。
        """
        if out_dir is None:
            from agent_platform.core import paths as _paths  # noqa: PLC0415

            out_dir = str(_paths.data_path("eval_reports"))
        os.makedirs(out_dir, exist_ok=True)
        tag = tag or self.model or "report"
        paths: dict[str, str] = {}

        # --- JSON ---
        json_path = os.path.join(out_dir, f"eval_{tag}.json")
        payload = {
            "summary": summary,
            "cases": [
                {
                    "id": o.case.id,
                    "category": o.case.category,
                    "query": o.case.query,
                    "gold_answer": o.case.gold_answer,
                    "answer": o.answer,
                    "retrieved": o.retrieved_ids,
                    "gold_chunks": o.case.gold_chunks,
                    "citations": o.citations,
                    "invalid_citations": o.invalid_citations,
                    "refused": o.refused,
                    "elapsed_ms": o.elapsed_ms,
                    "judge": o.judge,
                    "error": o.error,
                }
                for o in outcomes
            ],
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        paths["json"] = json_path

        # --- Markdown ---
        md_path = os.path.join(out_dir, f"eval_{tag}.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(self.render_markdown(outcomes, summary))
        paths["markdown"] = md_path

        # --- CSV ---
        csv_path = os.path.join(out_dir, f"eval_{tag}.csv")
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(
                ["id", "category", "query", "answer", "refused",
                 "retrieved_count", "gold_count", "recall_hit",
                 "citations", "invalid_citations", "judge_overall",
                 "elapsed_ms", "failure_attribution"]
            )
            for o in outcomes:
                md = o.to_metric_dict()
                w.writerow(
                    [
                        o.case.id,
                        o.case.category,
                        o.case.query[:120],
                        (o.answer or "")[:400].replace("\n", " "),
                        int(o.refused),
                        len(o.retrieved_ids),
                        len(o.case.gold_chunks),
                        int(bool(set(o.retrieved_ids) & set(o.case.gold_chunks))),
                        len(o.citations),
                        len(o.invalid_citations),
                        (o.judge or {}).get("overall", ""),
                        round(o.elapsed_ms, 1),
                        M.attribute_failure(md),
                    ]
                )
        paths["csv"] = csv_path
        return paths

    # ==================================================================
    @staticmethod
    def render_markdown(
        outcomes: Sequence[EvalOutcome], summary: dict[str, Any]
    ) -> str:
        """渲染 Markdown 报告（按分层拆分）。

        Args:
            outcomes: 结果列表。
            summary: 汇总结果。

        Returns:
            Markdown 文本。
        """
        lines: list[str] = []
        lines.append(f"# 评测报告 —— {summary.get('model')}")
        lines.append("")
        lines.append(f"- 用例总数：{summary.get('case_count')}")
        j = summary.get("judge") or {}
        lines.append(
            f"- LLM-as-Judge 均分：{j.get('mean_overall', 0):.2f}/5"
            f"（已评 {j.get('scored', 0)} 条）"
        )
        lines.append(f"- 拒答率：{summary.get('refusal_rate', 0):.2%}")
        lines.append(
            f"- 非法引用总数：{summary.get('invalid_citation_total', 0)}"
        )
        lines.append("")

        lines.append("## 1 总体指标（按层）")
        lines.append("")
        for layer, vals in (summary.get("overall") or {}).items():
            lines.append(f"### {layer}")
            lines.append("")
            lines.append("| 指标 | 实测 | 基线目标 | 优化目标 | 达标 |")
            lines.append("|---|---|---|---|---|")
            for metric, value in vals.items():
                base, target = M.METRIC_TARGETS.get(metric, (None, None))
                if base is None:
                    lines.append(f"| {metric} | {value:.4g} | — | — | — |")
                    continue
                if base >= 0 and base <= 1:
                    passed = "✅" if value >= base else "⚠️"
                else:
                    passed = "✅" if value <= base else "⚠️"
                lines.append(
                    f"| {metric} | {value:.4g} | {base} | {target} | {passed} |"
                )
            lines.append("")

        lines.append("## 2 分层指标表")
        lines.append("")
        for cat, data in (summary.get("by_category") or {}).items():
            lines.append(f"### {cat}（{data.get('count')} 条）")
            lines.append("")
            lines.append("| 层 | 指标 | 实测 |")
            lines.append("|---|---|---|")
            for layer in ("L1", "L2", "L3"):
                for metric, value in (data.get(layer) or {}).items():
                    lines.append(f"| {layer} | {metric} | {value:.4g} |")
            lines.append("")

        lines.append("## 3 失败归因分解（docs/02 §6.6）")
        lines.append("")
        lines.append("| 归因 | 条数 |")
        lines.append("|---|---|")
        for label, count in sorted(
            (summary.get("failure_attribution") or {}).items(),
            key=lambda kv: -kv[1],
        ):
            lines.append(f"| {label} | {count} |")
        lines.append("")

        lines.append("## 4 逐条明细")
        lines.append("")
        lines.append("| ID | 分层 | 问题 | 命中 | 引用(非法) | 拒答 | Judge | 归因 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for o in outcomes:
            md = o.to_metric_dict()
            jscore = (o.judge or {}).get("overall", "")
            jscore = f"{jscore:.1f}" if isinstance(jscore, (int, float)) else ""
            lines.append(
                f"| {o.case.id} | {o.case.category} | {o.case.query[:40]} | "
                f"{len(o.retrieved_ids)}/{len(o.case.gold_chunks)} | "
                f"{len(o.citations)}({len(o.invalid_citations)}) | "
                f"{'是' if o.refused else '否'} | {jscore} | "
                f"{M.attribute_failure(md)} |"
            )
        lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
def _numbers(text: str) -> list[float]:
    """抽取数值。

    Args:
        text: 文本。

    Returns:
        数值列表。
    """
    import re as _re

    out: list[float] = []
    for m in _re.finditer(r"-?\d+(?:\.\d+)?", text or ""):
        try:
            out.append(float(m.group(0)))
        except ValueError:
            continue
    return out


def _tokens(text: str) -> list[str]:
    """抽取文本 token（中文按字，英文按词）。

    Args:
        text: 文本。

    Returns:
        token 列表。
    """
    import re as _re

    return _re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", text or "")


def _text_similarity(a: str, b: str) -> float:
    """两段文本的 token 级 Jaccard 相似度。

    Args:
        a: 文本 A。
        b: 文本 B。

    Returns:
        0.0 ~ 1.0 的相似度。
    """
    ta = {t.lower() for t in _tokens(a or "")}
    tb = {t.lower() for t in _tokens(b or "")}
    if not ta or not tb:
        return 0.0
    return round(len(ta & tb) / len(ta | tb), 4)


def _extract_cases(answer: str) -> list[dict[str, Any]]:
    """从模型回答中抽取结构化测试用例（docs/02 §4.1 形态）。

    支持两种来源：

    1. 回答中的 ```json 代码块（对象或数组），字段形如
       ``case_id / test_items / preconditions / test_steps / traceability``；
    2. 回答中直接内嵌的 JSON 对象（花括号配平扫描）。

    抽取失败或不是合法用例对象时返回空列表。

    Args:
        answer: 模型回答文本。

    Returns:
        用例 dict 列表。
    """
    import json as _json
    import re as _re

    text = answer or ""
    if not text.strip():
        return []

    candidates: list[Any] = []
    for block in _re.findall(r"```(?:json)?\s*(.*?)```", text, _re.DOTALL):
        candidates.append(block)
    # 花括号配平扫描（取最外层 JSON 对象 / 数组）
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
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
            continue
        if ch in "{[":
            if depth == 0:
                start = i
            depth += 1
        elif ch in "}]":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    candidates.append(text[start : i + 1])
                    start = -1

    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for cand in candidates:
        raw = cand.strip()
        if not raw or not raw.startswith(("{", "[")):
            continue
        try:
            obj = _json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(obj, dict):
            obj = obj.get("cases") if isinstance(obj.get("cases"), list) else [obj]
        if not isinstance(obj, list):
            continue
        for item in obj:
            if not isinstance(item, dict):
                continue
            if id(item) in seen:
                continue
            seen.add(id(item))
            if not (
                item.get("case_id")
                or item.get("test_items")
                or item.get("test_steps")
            ):
                continue
            out.append(item)
        if out:
            break
    return out
