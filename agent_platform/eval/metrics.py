"""四层指标计算（L1 系统层 / L2 检索层 / L3 生成层 / L4 业务层）。

对应设计文档：
    docs/02_场景方案_STDF分析与测试用例生成.md
        §6.1 总体框架 —— L4 业务 / L3 生成 / L2 检索 / L1 系统
        §6.2 L1 系统层指标（TTFT P50/P95、端到端延迟、工具调用、缓存命中、token 成本）
        §6.3 L2 检索层指标（Recall@k、Precision@k、MRR、NDCG@10、支持率、可溯源率、
          空召回率、权限过滤正确率）
        §6.4.1 数值问答（SQL 可执行率、语义正确率、EX、容差准确率、多跳、时间口径、
          澄清、拒答）
        §6.4.2 用例生成（字段完整率、格式合规率、限值可溯源率、单位一致率、
          上下限合理率、重复率）
        §6.4.3 归因分析（Top1/Top3、错误归因率、正确弃权率、证据完整率）
        §6.4.4 幻觉率（数值/引用/逻辑/综合、无依据断言率）
        §6.5 L4 业务层指标
        §6.6 指标归因分解（失败闭环）

每个指标的 docstring 均写明：**公式、数据来源、目标值（基线 → 优化目标）**。
目标值全部取自 docs/02 §6 的表格，**不得自创**。
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable, Iterable, Sequence

__all__ = [
    "METRIC_TARGETS",
    "recall_at_k",
    "precision_at_k",
    "mrr",
    "ndcg_at_k",
    "ground_truth_support_rate",
    "citation_traceable_rate",
    "empty_retrieval_rate",
    "acl_filter_accuracy",
    "sql_executable_rate",
    "sql_semantic_correct_rate",
    "execution_accuracy",
    "tolerance_accuracy",
    "multi_hop_accuracy",
    "time_semantics_accuracy",
    "clarification_accuracy",
    "refusal_accuracy",
    "case_field_completeness",
    "case_format_valid_rate",
    "case_limit_traceable_rate",
    "case_unit_consistent_rate",
    "case_limit_sane_rate",
    "case_duplicate_rate",
    "root_cause_top1",
    "root_cause_top3",
    "wrong_attribution_rate",
    "correct_abstain_rate",
    "attribution_evidence_rate",
    "numeric_hallucination_rate",
    "citation_hallucination_rate",
    "logical_hallucination_rate",
    "overall_hallucination_rate",
    "unsupported_claim_rate",
    "percentile",
    "ttft_p50",
    "ttft_p95",
    "e2e_latency_p50",
    "e2e_latency_p95",
    "tool_calls_per_request",
    "tool_success_rate",
    "tokens_per_request",
    "cache_hit_rate",
    "compute_l2_metrics",
    "compute_l3_numerical_metrics",
    "compute_l3_case_metrics",
    "compute_l3_hallucination_metrics",
    "compute_l3_attribution_metrics",
    "compute_l1_metrics",
    "compute_all",
    "attribute_failure",
]


# ---------------------------------------------------------------------------
# 指标目标值（docs/02 §6 各表「目标值（基线 → 优化目标）」列）
# ---------------------------------------------------------------------------
METRIC_TARGETS: dict[str, tuple[float | None, float | None]] = {
    # ---- L1 系统层（§6.2）----
    "ttft_p50": (2.0, 1.2),
    "ttft_p95": (8.0, 4.0),
    "e2e_latency_p50": (10.0, 6.0),
    "e2e_latency_p95": (45.0, 25.0),
    "tool_calls_per_request": (3.5, 2.5),
    "tool_success_rate": (0.92, 0.97),
    "cache_hit_rate": (0.18, 0.30),
    "tokens_per_request_in": (6500, 4000),
    "tokens_per_request_out": (900, 600),
    "cost_per_request_cny": (0.35, 0.15),
    # ---- L2 检索层（§6.3）----
    "recall_at_5": (0.85, 0.92),
    "recall_at_10": (0.93, 0.97),
    "precision_at_5": (0.55, 0.70),
    "mrr": (0.72, 0.85),
    "ndcg_at_10": (0.70, 0.82),
    "ground_truth_support_rate": (0.90, 0.96),
    "citation_traceable_rate": (0.95, 0.99),
    "empty_retrieval_rate": (0.12, 0.06),
    "acl_filter_accuracy": (1.0, 1.0),
    # ---- L3 数值问答（§6.4.1）----
    "sql_executable_rate": (0.85, 0.96),
    "sql_semantic_correct_rate": (0.78, 0.90),
    "execution_accuracy": (0.65, 0.85),
    "tolerance_accuracy": (0.80, 0.93),
    "multi_hop_accuracy": (0.55, 0.78),
    "time_semantics_accuracy": (0.80, 0.93),
    "clarification_accuracy": (0.70, 0.88),
    "refusal_accuracy": (0.95, 0.99),
    # ---- L3 用例生成（§6.4.2）----
    "case_field_completeness": (0.82, 0.95),
    "case_format_valid_rate": (0.88, 0.98),
    "case_limit_traceable_rate": (0.70, 0.98),
    "case_unit_consistent_rate": (0.90, 0.99),
    "case_limit_sane_rate": (0.85, 0.97),
    "case_duplicate_rate": (0.25, 0.10),
    "case_expert_pass_rate": (0.55, 0.75),
    "case_fm_coverage": (0.72, 0.92),
    # ---- L3 归因（§6.4.3）----
    "root_cause_top1": (0.42, 0.65),
    "root_cause_top3": (0.68, 0.85),
    "wrong_attribution_rate": (0.18, 0.06),
    "correct_abstain_rate": (0.60, 0.88),
    "attribution_evidence_rate": (0.85, 0.98),
    # ---- L3 幻觉（§6.4.4）----
    "numeric_hallucination_rate": (0.08, 0.01),
    "citation_hallucination_rate": (0.10, 0.02),
    "logical_hallucination_rate": (0.12, 0.04),
    "overall_hallucination_rate": (0.18, 0.05),
    "unsupported_claim_rate": (0.12, 0.03),
    # ---- L4 业务层（§6.5）----
    "analysis_time_reduction": (0.45, 0.70),
    "case_workload_reduction": (0.30, 0.50),
    "expert_rewrite_rate": (0.40, 0.18),
    "adoption_rate": (0.45, 0.75),
    "first_pass_rate": (0.25, 0.55),
    "self_serve_rate": (0.55, 0.80),
}


# ===========================================================================
# 通用工具
# ===========================================================================
def _safe_div(a: float, b: float) -> float:
    """安全除法（分母为 0 返回 0.0）。

    Args:
        a: 分子。
        b: 分母。

    Returns:
        商。
    """
    return float(a) / float(b) if b else 0.0


def percentile(values: Sequence[float], q: float) -> float:
    """计算分位数（线性插值法）。

    Args:
        values: 样本列表。
        q: 分位（0~1）。

    Returns:
        分位值；样本为空返回 0.0。
    """
    if not values:
        return 0.0
    xs = sorted(float(v) for v in values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * max(0.0, min(1.0, q))
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    return xs[lo] * (hi - pos) + xs[hi] * (pos - lo)


def _extract_numbers(text: str) -> list[float]:
    """从文本中抽取数值（幻觉率计算用）。

    Args:
        text: 文本。

    Returns:
        数值列表（不含百分号单位处理）。
    """
    out: list[float] = []
    for m in re.finditer(r"-?\d+(?:\.\d+)?", text or ""):
        try:
            out.append(float(m.group(0)))
        except ValueError:
            continue
    return out


def _extract_citations(text: str) -> list[str]:
    """从文本中抽取引用标记。

    兼容 ASCII ``[]`` / 全角 ``【】`` / 中文 ``［］``（推理型模型常输出全角括号）。

    Args:
        text: 文本。

    Returns:
        引用原文列表。
    """
    return re.findall(
        r"[\[［【]\s*[A-Za-z0-9_\-]+\s*:\s*[A-Za-z0-9_\-]+\s*[\]］】]", text or ""
    )


# ===========================================================================
# L2 检索层指标（docs/02 §6.3）
# ===========================================================================
def recall_at_k(retrieved: Sequence[str], gold: Sequence[str], k: int = 10) -> float:
    """Recall@k：top-k 是否召回全部相关文档。

    公式：``|R_k ∩ G| / |G|``。
    数据来源：离线评测集（标注了 gold chunk 的 Qrels）。
    目标值：Recall@5 ≥ 0.85 → 0.92；Recall@10 ≥ 0.93 → 0.97。
    常见失败原因：切分策略不当、专有名词切碎、缺同义词表。

    Args:
        retrieved: 检索结果 chunk_id 列表（按排名）。
        gold: gold chunk_id 列表。
        k: 截断位置。

    Returns:
        Recall@k（0~1）。
    """
    if not gold:
        return 0.0
    top = set(list(retrieved)[:k])
    hit = len(top & set(gold))
    return _safe_div(hit, len(set(gold)))


def precision_at_k(retrieved: Sequence[str], gold: Sequence[str], k: int = 5) -> float:
    """Precision@k：top-k 中相关比例。

    公式：``|R_k ∩ G| / |R_k|``。
    数据来源：离线评测集。
    目标值：P@5 ≥ 0.55 → 0.70。
    常见失败原因：文档质量差、模板重复文档堆叠。

    Args:
        retrieved: 检索结果 chunk_id 列表。
        gold: gold chunk_id 列表。
        k: 截断位置。

    Returns:
        Precision@k（0~1）。
    """
    top = list(retrieved)[:k]
    if not top:
        return 0.0
    hit = len(set(top) & set(gold))
    return _safe_div(hit, len(top))


def mrr(retrieved: Sequence[str], gold: Sequence[str]) -> float:
    """MRR：首个正确结果的排名倒数均值。

    公式：``mean(1/rank_first)``，rank 从 1 开始；未命中记 0。
    数据来源：离线评测集。
    目标值：≥ 0.72 → 0.85。
    常见失败原因：排序模型偏弱、缺 rerank。

    Args:
        retrieved: 检索结果 chunk_id 列表。
        gold: gold chunk_id 列表。

    Returns:
        MRR（0~1）。
    """
    if not retrieved or not gold:
        return 0.0
    g = set(gold)
    for rank, cid in enumerate(retrieved, start=1):
        if cid in g:
            return _safe_div(1.0, rank)
    return 0.0


def ndcg_at_k(
    retrieved: Sequence[str],
    gold: Sequence[str],
    k: int = 10,
    grades: dict[str, int] | None = None,
) -> float:
    """NDCG@10：归一化折损累积增益。

    公式：``DCG@10 = Σ rel_i / log2(i+1)``，``NDCG = DCG / IDCG``。
    数据来源：离线评测集（分级相关性标注）。
    目标值：≥ 0.70 → 0.82。
    常见失败原因：分级相关性标注缺失、位置偏置。

    Args:
        retrieved: 检索结果 chunk_id 列表。
        gold: gold chunk_id 列表（相关集合）。
        k: 截断位置。
        grades: 可选的「分级相关性」映射 ``{chunk_id: grade}``；未提供时二值处理。

    Returns:
        NDCG@k（0~1）。
    """
    if not retrieved or not gold:
        return 0.0
    g = set(gold)

    def _rel(cid: str) -> int:
        if grades and cid in grades:
            return int(grades[cid])
        return 1 if cid in g else 0

    dcg = 0.0
    for i, cid in enumerate(list(retrieved)[:k]):
        rel = _rel(cid)
        if rel:
            dcg += rel / math.log2(i + 2)
    ideal_grades = sorted(
        ([_rel(c) for c in retrieved if c in g] if grades else [1] * len(g)),
        reverse=True,
    )[:k]
    idcg = sum(g_ / math.log2(i + 2) for i, g_ in enumerate(ideal_grades))
    return _safe_div(dcg, idcg)


def ground_truth_support_rate(
    answers: Sequence[str],
    evidence_texts: Sequence[str],
) -> float:
    """ground-truth 支持率：回答中每个数值/断言能在检索结果中找到依据的比例。

    公式：``supported_assertions / total_assertions``（此处以"答案中出现的数值
    能否在证据文本中找到"作为断言的可自动代理）。
    数据来源：生成日志 + NLI 校验器。
    目标值：≥ 90% → 96%。
    常见失败原因：数值来自 SQL 但口径未引用、模型凭记忆补全。

    Args:
        answers: 答案文本列表。
        evidence_texts: 对应的检索证据文本列表（与 answers 一一对应）。

    Returns:
        支持率（0~1）。
    """
    supported = 0
    total = 0
    for ans, ev in zip(answers, evidence_texts):
        ev_nums = set(_extract_numbers(ev))
        for n in _extract_numbers(ans):
            total += 1
            if n in ev_nums:
                supported += 1
            elif any(abs(n - e) <= max(abs(e) * 0.001, 1e-9) for e in ev_nums):
                supported += 1
    return _safe_div(supported, total)


def citation_traceable_rate(
    citations: Sequence[Sequence[str]],
    valid_chunk_ids: Sequence[str],
) -> float:
    """引用可溯源率：引用能定位到真实来源文件且内容匹配。

    公式：``valid_citations / total_citations``。
    数据来源：点击 / 校验脚本。
    目标值：≥ 95% → 99%。
    常见失败原因：chunk_id 与源文件映射丢失、引用行号漂移。

    Args:
        citations: 每条回答的引用标记列表。
        valid_chunk_ids: 知识库中真实存在的 chunk_id 集合（列表形式）。

    Returns:
        可溯源率（0~1）。
    """
    valid_set = set(valid_chunk_ids)
    total = 0
    ok = 0
    for cites in citations:
        for c in cites:
            total += 1
            m = re.match(r"[\[［【]\s*([^\]:]+)\s*:\s*([^\]］】]+)\s*[\]］】]", c)
            if m and m.group(2).strip() in valid_set:
                ok += 1
    return _safe_div(ok, total)


def empty_retrieval_rate(retrieval_counts: Sequence[int]) -> float:
    """空召回率：检索无结果导致需澄清/拒答。

    公式：``empty_retrieval / queries``。
    数据来源：检索日志。
    目标值：≤ 12% → ≤ 6%。
    常见失败原因：用户表达模糊、别名未维护。

    Args:
        retrieval_counts: 每条查询的召回条数。

    Returns:
        空召回率（0~1）。
    """
    if not retrieval_counts:
        return 0.0
    return _safe_div(sum(1 for c in retrieval_counts if c == 0), len(retrieval_counts))


def acl_filter_accuracy(
    retrieved_all: Sequence[Sequence[str]],
    retrieved_allowed: Sequence[Sequence[str]],
    forbidden_ids: Iterable[str],
) -> float:
    """权限过滤正确率：按用户权限过滤后无误召回/漏召回。

    公式：``1 - (越权召回数 + 应召回但未召回数) / 总 gold 数``。
    数据来源：人工抽检 + 权限模拟（安全测试）。
    目标值：**100%（不可妥协）**。
    常见失败原因：过滤条件未下推到检索层。

    Args:
        retrieved_all: 未加权限过滤时的检索结果（每条一组 chunk_id）。
        retrieved_allowed: 加权限过滤后的检索结果（每条一组 chunk_id）。
        forbidden_ids: 用户无权访问的 chunk_id 集合。

    Returns:
        正确率（0~1）。
    """
    forbidden = set(forbidden_ids)
    total = 0
    wrong = 0
    for allr, allow in zip(retrieved_all, retrieved_allowed):
        total += len(forbidden & set(allr))
        wrong += len(forbidden & set(allow))
    return 1.0 - _safe_div(wrong, total)


# ===========================================================================
# L3 数值问答指标（docs/02 §6.4.1）
# ===========================================================================
def sql_executable_rate(results: Sequence[dict[str, Any]]) -> float:
    """SQL 可执行率：生成的 SQL 能在沙箱跑通。

    公式：``executable_sql / total_sql``。
    数据来源：执行日志。
    目标值：≥ 85% → 96%。
    常见失败原因：列名猜错、缺 join、方言不匹配。

    Args:
        results: 每条含 ``executable``（bool）的结果字典。

    Returns:
        可执行率（0~1）。
    """
    if not results:
        return 0.0
    return _safe_div(sum(1 for r in results if r.get("executable")), len(results))


def sql_semantic_correct_rate(results: Sequence[dict[str, Any]]) -> float:
    """SQL 语义正确率：跑通且语义与标准答案一致。

    公式：``semantic_correct / executable_sql``。
    数据来源：专家标注 + Judge。
    目标值：≥ 78% → 90%。
    常见失败原因：时间口径错、重测未过滤、维度映射错。

    Args:
        results: 每条含 ``executable`` 与 ``semantic_correct`` 的结果字典。

    Returns:
        语义正确率（0~1）。
    """
    execu = [r for r in results if r.get("executable")]
    if not execu:
        return 0.0
    return _safe_div(sum(1 for r in execu if r.get("semantic_correct")), len(execu))


def execution_accuracy(results: Sequence[dict[str, Any]]) -> float:
    """执行准确率 EX：最终答案数值与标准答案完全一致。

    公式：``exact_match / answers``。
    数据来源：评测集。
    目标值：≥ 65% → 85%。
    常见失败原因：语义正确但数值精度/舍入不一致。

    Args:
        results: 每条含 ``exact_match``（bool）的结果字典。

    Returns:
        EX（0~1）。
    """
    if not results:
        return 0.0
    return _safe_div(sum(1 for r in results if r.get("exact_match")), len(results))


def tolerance_accuracy(
    results: Sequence[dict[str, Any]],
    rel_tol: float = 0.001,
) -> float:
    """容差准确率：相对误差 ≤ 阈值即算对。

    公式：``abs(pred - gold) / abs(gold) ≤ 0.001``。
    数据来源：评测集。
    目标值：≥ 80% → 93%。
    常见失败原因：百分比 vs 百分点混淆（下降 3% vs 下降 3 个百分点）。

    Args:
        results: 每条含 ``pred`` 与 ``gold`` 数值的结果字典。
        rel_tol: 相对误差阈值（默认 0.001）。

    Returns:
        容差准确率（0~1）。
    """
    if not results:
        return 0.0
    hit = 0
    for r in results:
        pred, gold = r.get("pred"), r.get("gold")
        if pred is None or gold is None:
            continue
        try:
            p, g = float(pred), float(gold)
        except (TypeError, ValueError):
            continue
        if g == 0:
            if abs(p) <= rel_tol:
                hit += 1
        elif abs(p - g) / abs(g) <= rel_tol:
            hit += 1
    return _safe_div(hit, len(results))


def multi_hop_accuracy(results: Sequence[dict[str, Any]]) -> float:
    """多跳问题正确率：需 2 步以上推理的问题正确率。

    公式：分层统计（仅统计 ``multi_hop=True`` 的子集）。
    数据来源：评测集。
    目标值：≥ 55% → 78%。
    常见失败原因：分解错误、上下文丢失。

    Args:
        results: 每条含 ``multi_hop``（bool）与 ``correct``（bool）的结果字典。

    Returns:
        多跳正确率（0~1）。
    """
    sub = [r for r in results if r.get("multi_hop")]
    if not sub:
        return 0.0
    return _safe_div(sum(1 for r in sub if r.get("correct")), len(sub))


def time_semantics_accuracy(results: Sequence[dict[str, Any]]) -> float:
    """时间口径准确率：生成的 SQL 时间窗与用户意图一致。

    公式：人工/规则判定后取正确占比。
    数据来源：评测集。
    目标值：≥ 80% → 93%。
    常见失败原因：「上个月」= 自然月 / 滚动月歧义、时区。

    Args:
        results: 每条含 ``time_ok``（bool）的结果字典。

    Returns:
        时间口径准确率（0~1）。
    """
    if not results:
        return 0.0
    return _safe_div(sum(1 for r in results if r.get("time_ok")), len(results))


def clarification_accuracy(results: Sequence[dict[str, Any]]) -> float:
    """澄清正确率：应澄清时正确发起澄清。

    公式：``correct_clarify / needed_clarify``。
    数据来源：评测集。
    目标值：≥ 70% → 88%。
    常见失败原因：过度澄清（用户烦）、不澄清直接错答。

    Args:
        results: 每条含 ``need_clarify``（bool）与 ``clarified``（bool）的结果字典。

    Returns:
        澄清正确率（0~1）。
    """
    sub = [r for r in results if r.get("need_clarify")]
    if not sub:
        return 0.0
    return _safe_div(sum(1 for r in sub if r.get("clarified")), len(sub))


def refusal_accuracy(results: Sequence[dict[str, Any]]) -> float:
    """拒答正确率：无数据时正确拒答而非编造。

    公式：``correct_refuse / unanswerable``。
    数据来源：对抗集。
    目标值：≥ 95% → 99%。
    常见失败原因：检索为空仍强行作答。

    Args:
        results: 每条含 ``unanswerable``（bool）与 ``refused``（bool）的结果字典。

    Returns:
        拒答正确率（0~1）。
    """
    sub = [r for r in results if r.get("unanswerable")]
    if not sub:
        return 0.0
    return _safe_div(sum(1 for r in sub if r.get("refused")), len(sub))


# ===========================================================================
# L3 用例生成指标（docs/02 §6.4.2）
# ===========================================================================
def case_field_completeness(
    cases: Sequence[dict[str, Any]],
    required_fields: Sequence[str],
) -> float:
    """字段完整率：必填字段非空比例。

    公式：``non_null_required_fields / required_fields``（按用例平均）。
    数据来源：``case_lint``。
    目标值：≥ 82% → 95%。
    常见失败原因：模板缺字段、未结合规格。

    Args:
        cases: 用例 dict 列表。
        required_fields: 必填字段名列表。

    Returns:
        字段完整率（0~1）。
    """
    if not cases or not required_fields:
        return 0.0
    total = 0
    filled = 0
    for c in cases:
        for f in required_fields:
            total += 1
            v = c.get(f)
            if v is not None and v != "" and v != [] and v != {}:
                filled += 1
    return _safe_div(filled, total)


def case_format_valid_rate(results: Sequence[dict[str, Any]]) -> float:
    """格式合规率：通过 Schema 与格式规则。

    公式：``valid_cases / total``。
    数据来源：``case_lint``。
    目标值：≥ 88% → 98%。
    常见失败原因：JSON 截断、字段名不一致。

    Args:
        results: 每条含 ``format_valid``（bool）的结果字典。

    Returns:
        格式合规率（0~1）。
    """
    if not results:
        return 0.0
    return _safe_div(sum(1 for r in results if r.get("format_valid")), len(results))


def case_limit_traceable_rate(cases: Sequence[dict[str, Any]]) -> float:
    """限值可溯源率：限值能定位到规格出处。

    公式：``traceable_limit_items / total_items``。
    数据来源：``case_lint``。
    目标值：≥ 70% → 98%。
    常见失败原因：模型编造数值（规则 R01 违例）。

    溯源来源按 docs/02 §4.1 的用例结构依次查找（item 级 → 用例级）：

    1. ``test_item.source`` / ``test_item.traceability``（item 级溯源）
    2. ``case.traceability.spec_ref[]``（用例级溯源，含 ``doc_id`` / ``page`` / ``quote``）

    Args:
        cases: 用例 dict 列表；每个用例含 ``test_items`` 列表。

    Returns:
        限值可溯源率（0~1）。
    """
    total = 0
    ok = 0
    for c in cases:
        case_refs = c.get("traceability") or {}
        if not isinstance(case_refs, dict):
            case_refs = {}
        spec_refs = case_refs.get("spec_ref") or case_refs.get("spec_refs") or []
        case_traceable = any(
            isinstance(r, dict)
            and (r.get("doc_id") or r.get("page") or r.get("quote"))
            for r in spec_refs
        )
        for item in (c.get("test_items") or []):
            total += 1
            trace = item.get("traceability") or item.get("source")
            item_traceable = isinstance(trace, dict) and (
                trace.get("doc_id") or trace.get("page") or trace.get("quote")
            )
            if item_traceable or case_traceable:
                ok += 1
    return _safe_div(ok, total)


def case_unit_consistent_rate(
    cases: Sequence[dict[str, Any]],
    spec_units: dict[str, str] | None = None,
) -> float:
    """单位一致率：单位合法且与规格一致。

    公式：``consistent_unit_items / total_items``。
    数据来源：``case_lint``。
    目标值：≥ 90% → 99%。
    常见失败原因：单位换算（pF/nF）、大小写。

    Args:
        cases: 用例 dict 列表；每项含 ``unit``。
        spec_units: 规格书单位映射 ``{param_code: unit}``（可选）。

    Returns:
        单位一致率（0~1）。
    """
    total = 0
    ok = 0
    for c in cases:
        for item in (c.get("test_items") or []):
            total += 1
            unit = (item.get("unit") or "").strip()
            if not unit:
                continue
            if spec_units:
                code = item.get("param_code") or item.get("name") or ""
                expect = spec_units.get(code)
                if expect is None or unit == expect:
                    ok += 1
            else:
                ok += 1
    return _safe_div(ok, total)


def case_limit_sane_rate(cases: Sequence[dict[str, Any]]) -> float:
    """上下限合理率：满足 lower ≤ typ ≤ upper 且物理合理。

    公式：``sane_items / total_items``（规则 R03）。
    数据来源：``case_lint``。
    目标值：≥ 85% → 97%。
    常见失败原因：单边限值被填成双边、精度过多。

    Args:
        cases: 用例 dict 列表；每项含 ``lower_limit`` / ``upper_limit`` /
            ``typical``（或 ``expect_value``）。

    Returns:
        上下限合理率（0~1）。
    """
    total = 0
    ok = 0
    for c in cases:
        for item in (c.get("test_items") or []):
            total += 1
            lo = item.get("lower_limit")
            hi = item.get("upper_limit")
            typ = item.get("typical")
            if typ is None:
                typ = item.get("expect_value")
            vals = [v for v in (lo, hi, typ) if v is not None]
            if len(vals) < 2:
                continue
            try:
                lo_v = float(lo) if lo is not None else None
                hi_v = float(hi) if hi is not None else None
                typ_v = float(typ) if typ is not None else None
            except (TypeError, ValueError):
                continue
            sane = True
            if lo_v is not None and hi_v is not None and lo_v > hi_v:
                sane = False
            if lo_v is not None and typ_v is not None and typ_v < lo_v:
                sane = False
            if hi_v is not None and typ_v is not None and typ_v > hi_v:
                sane = False
            if sane:
                ok += 1
    return _safe_div(ok, total)


def case_duplicate_rate(
    similarities: Sequence[float],
    threshold: float = 0.9,
) -> float:
    """与历史重复率：相似度 > 0.9 且无新增价值的比例。

    公式：``dup_cases / total``（相似度超阈值计为重复）。
    数据来源：``case_dedupe``。
    目标值：≤ 25% → ≤ 10%（越低越好）。
    常见失败原因：模板化生成、未读历史用例。

    Args:
        similarities: 与历史用例的最大相似度列表。
        threshold: 重复阈值。

    Returns:
        重复率（0~1）。
    """
    if not similarities:
        return 0.0
    return _safe_div(sum(1 for s in similarities if s > threshold), len(similarities))


# ===========================================================================
# L3 归因指标（docs/02 §6.4.3）
# ===========================================================================
def root_cause_top1(results: Sequence[dict[str, Any]]) -> float:
    """根因命中率 Top1：首选根因与专家一致。

    公式：``top1_correct / total``。
    数据来源：专家标注（FA / 维修）。
    目标值：≥ 42% → 65%。
    常见失败原因：特征不足、缺 MES 事件、历史故障库为空。

    Args:
        results: 每条含 ``top1_correct``（bool）的结果字典。

    Returns:
        Top1 命中率（0~1）。
    """
    if not results:
        return 0.0
    return _safe_div(sum(1 for r in results if r.get("top1_correct")), len(results))


def root_cause_top3(results: Sequence[dict[str, Any]]) -> float:
    """根因命中率 Top3：前三候选含正确根因。

    公式：``hit_in_top3 / total``。
    数据来源：专家标注。
    目标值：≥ 68% → 85%。
    常见失败原因：候选生成池太小。

    Args:
        results: 每条含 ``top3_hit``（bool）的结果字典。

    Returns:
        Top3 命中率（0~1）。
    """
    if not results:
        return 0.0
    return _safe_div(sum(1 for r in results if r.get("top3_hit")), len(results))


def wrong_attribution_rate(results: Sequence[dict[str, Any]]) -> float:
    """错误归因率：给出错误根因（而非"不确定"）的比例。

    公式：``wrong_confident / total``。
    数据来源：专家标注。
    目标值：≤ 18% → ≤ 6%（越低越好）。
    常见失败原因：过度自信、未校准置信度。

    Args:
        results: 每条含 ``wrong_confident``（bool）的结果字典。

    Returns:
        错误归因率（0~1）。
    """
    if not results:
        return 0.0
    return _safe_div(sum(1 for r in results if r.get("wrong_confident")), len(results))


def correct_abstain_rate(results: Sequence[dict[str, Any]]) -> float:
    """正确弃权率：应弃权时输出"需人工 / 证据不足"。

    公式：``correct_abstain / should_abstain``。
    数据来源：对抗案例集。
    目标值：≥ 60% → 88%。
    常见失败原因：缺少不确定度校准机制。

    Args:
        results: 每条含 ``should_abstain``（bool）与 ``abstained``（bool）的结果字典。

    Returns:
        正确弃权率（0~1）。
    """
    sub = [r for r in results if r.get("should_abstain")]
    if not sub:
        return 0.0
    return _safe_div(sum(1 for r in sub if r.get("abstained")), len(sub))


def attribution_evidence_rate(results: Sequence[dict[str, Any]]) -> float:
    """归因证据完整率：结论附带可验证证据的比例。

    公式：``cases_with_evidence / total``。
    数据来源：生成日志。
    目标值：≥ 85% → 98%。
    常见失败原因：提示词未强制证据格式。

    Args:
        results: 每条含 ``has_evidence``（bool）的结果字典。

    Returns:
        证据完整率（0~1）。
    """
    if not results:
        return 0.0
    return _safe_div(sum(1 for r in results if r.get("has_evidence")), len(results))


# ===========================================================================
# L3 幻觉指标（docs/02 §6.4.4）
# ===========================================================================
def numeric_hallucination_rate(results: Sequence[dict[str, Any]]) -> float:
    """数值幻觉率：回答中的数值在数据/检索中不存在。

    公式：``hallucinated_numbers / total_numbers``。
    数据来源：数值回溯校验器。
    目标值：≤ 8% → ≤ 1%（越低越好）。
    常见失败原因：模型自由算数、SQL 结果被改写、缓存污染。

    Args:
        results: 每条含 ``hallucinated_numbers`` 与 ``total_numbers`` 的结果字典。

    Returns:
        数值幻觉率（0~1）。
    """
    total = sum(int(r.get("total_numbers") or 0) for r in results)
    bad = sum(int(r.get("hallucinated_numbers") or 0) for r in results)
    return _safe_div(bad, total)


def citation_hallucination_rate(results: Sequence[dict[str, Any]]) -> float:
    """引用幻觉率：引用了不存在的来源/页码。

    公式：``fake_citations / total_citations``。
    数据来源：引用校验器。
    目标值：≤ 10% → ≤ 2%（越低越好）。
    常见失败原因：chunk_id 编造、PDF 页码错位。

    Args:
        results: 每条含 ``fake_citations`` 与 ``total_citations`` 的结果字典。

    Returns:
        引用幻觉率（0~1）。
    """
    total = sum(int(r.get("total_citations") or 0) for r in results)
    bad = sum(int(r.get("fake_citations") or 0) for r in results)
    return _safe_div(bad, total)


def logical_hallucination_rate(results: Sequence[dict[str, Any]]) -> float:
    """逻辑幻觉率：结论与数据矛盾（数值对但推论错）。

    公式：``contradictory_claims / total_claims``。
    数据来源：NLI 校验器 + 抽检。
    目标值：≤ 12% → ≤ 4%（越低越好）。
    常见失败原因：归因模板套用、忽视反例。

    Args:
        results: 每条含 ``contradictory_claims`` 与 ``total_claims`` 的结果字典。

    Returns:
        逻辑幻觉率（0~1）。
    """
    total = sum(int(r.get("total_claims") or 0) for r in results)
    bad = sum(int(r.get("contradictory_claims") or 0) for r in results)
    return _safe_div(bad, total)


def overall_hallucination_rate(results: Sequence[dict[str, Any]]) -> float:
    """综合幻觉率：至少含一类幻觉的回答占比。

    公式：``answers_with_hallucination / total``。
    数据来源：NLI 校验器 + 数值回溯 + 引用校验。
    目标值：≤ 18% → ≤ 5%（越低越好）。

    Args:
        results: 每条含 ``has_hallucination``（bool）的结果字典。

    Returns:
        综合幻觉率（0~1）。
    """
    if not results:
        return 0.0
    return _safe_div(sum(1 for r in results if r.get("has_hallucination")), len(results))


def unsupported_claim_rate(results: Sequence[dict[str, Any]]) -> float:
    """无依据断言率：无引用支撑的事实性断言占比。

    公式：``unsupported_claims / total_factual_claims``。
    数据来源：校验器。
    目标值：≤ 12% → ≤ 3%（越低越好）。
    常见失败原因：模板化套话。

    Args:
        results: 每条含 ``unsupported_claims`` 与 ``total_factual_claims`` 的结果字典。

    Returns:
        无依据断言率（0~1）。
    """
    total = sum(int(r.get("total_factual_claims") or 0) for r in results)
    bad = sum(int(r.get("unsupported_claims") or 0) for r in results)
    return _safe_div(bad, total)


# ===========================================================================
# L1 系统层指标（docs/02 §6.2）
# ===========================================================================
def ttft_p50(samples: Sequence[float]) -> float:
    """首 token 延迟 P50。

    公式：``P50(t_first_token - t_request)``。
    数据来源：LLM Gateway 埋点 + Nginx 日志。
    目标值：≤ 2.0s → ≤ 1.2s。
    常见失败原因：网关排队、TTFT 受 prefill 长度影响、上下文过大。

    Args:
        samples: TTFT 样本（毫秒）。

    Returns:
        P50（毫秒）。
    """
    return percentile(samples, 0.50)


def ttft_p95(samples: Sequence[float]) -> float:
    """首 token 延迟 P95。

    公式：``P95(t_first_token - t_request)``。
    数据来源：同 TTFT P50。
    目标值：≤ 8.0s → ≤ 4.0s。
    常见失败原因：冷启动/模型加载、显存不足降级。

    Args:
        samples: TTFT 样本（毫秒）。

    Returns:
        P95（毫秒）。
    """
    return percentile(samples, 0.95)


def e2e_latency_p50(samples: Sequence[float]) -> float:
    """端到端延迟 P50：请求到完整响应。

    公式：``P50(t_end - t_request)``。
    数据来源：LLM Gateway 埋点。
    目标值：≤ 10s → ≤ 6s。
    常见失败原因：工具调用链长、串行检索。

    Args:
        samples: 端到端耗时样本（毫秒）。

    Returns:
        P50（毫秒）。
    """
    return percentile(samples, 0.50)


def e2e_latency_p95(samples: Sequence[float]) -> float:
    """端到端延迟 P95。

    公式：``P95(t_end - t_request)``。
    数据来源：同 E2E P50。
    目标值：≤ 45s → ≤ 25s。
    常见失败原因：多轮工具调用、SQL 全表扫描。

    Args:
        samples: 端到端耗时样本（毫秒）。

    Returns:
        P95（毫秒）。
    """
    return percentile(samples, 0.95)


def tool_calls_per_request(tool_call_counts: Sequence[int]) -> float:
    """工具调用次数/请求：平均工具调用链长度。

    公式：``sum(tool_calls) / requests``。
    数据来源：Tool Registry 日志。
    目标值：≤ 3.5 → ≤ 2.5。
    常见失败原因：工具选错、缺工具、规划发散。

    Args:
        tool_call_counts: 每请求的工具调用次数。

    Returns:
        平均次数。
    """
    if not tool_call_counts:
        return 0.0
    return _safe_div(sum(tool_call_counts), len(tool_call_counts))


def tool_success_rate(results: Sequence[dict[str, Any]]) -> float:
    """工具调用成功率：工具无错返回比例。

    公式：``ok_calls / total_calls``。
    数据来源：Tool Registry。
    目标值：≥ 92% → ≥ 97%。
    常见失败原因：SQL 语法错、API 超时、参数错。

    Args:
        results: 每条含 ``ok_calls`` 与 ``total_calls`` 的结果字典。

    Returns:
        成功率（0~1）。
    """
    total = sum(int(r.get("total_calls") or 0) for r in results)
    ok = sum(int(r.get("ok_calls") or 0) for r in results)
    return _safe_div(ok, total)


def tokens_per_request(usages: Sequence[dict[str, Any]]) -> tuple[float, float]:
    """每请求 Token 成本：平均消耗 token 数（区分 in/out）。

    公式：``sum(tokens) / requests``（区分 in/out）。
    数据来源：LLM Gateway。
    目标值：≤ 6.5k in / ≤ 900 out → ≤ 4k in / ≤ 600 out。
    常见失败原因：上下文塞入过多 chunk、历史消息未裁剪。

    Args:
        usages: 每请求的 token 用量字典（含 ``prompt_tokens`` /
            ``completion_tokens``，或 ``input_tokens`` / ``output_tokens``）。

    Returns:
        ``(平均输入 token, 平均输出 token)``。
    """
    if not usages:
        return 0.0, 0.0
    ins, outs = [], []
    for u in usages:
        ins.append(float(u.get("prompt_tokens") or u.get("input_tokens") or 0))
        outs.append(float(u.get("completion_tokens") or u.get("output_tokens") or 0))
    return _safe_div(sum(ins), len(ins)), _safe_div(sum(outs), len(outs))


def cache_hit_rate(results: Sequence[dict[str, Any]]) -> float:
    """缓存命中率：命中语义缓存/前缀缓存的比例。

    公式：``cache_hits / total_requests``。
    数据来源：网关缓存层。
    目标值：≥ 18% → ≥ 30%。
    常见失败原因：缓存键设计过严（时间戳进键）、语义缓存相似度阈值高。

    Args:
        results: 每条含 ``cache_hit``（bool）的结果字典。

    Returns:
        命中率（0~1）。
    """
    if not results:
        return 0.0
    return _safe_div(sum(1 for r in results if r.get("cache_hit")), len(results))


# ===========================================================================
# 聚合与归因
# ===========================================================================
def compute_l2_metrics(
    per_case: Sequence[dict[str, Any]],
    valid_chunk_ids: Sequence[str] = (),
) -> dict[str, float]:
    """汇总 L2 检索层指标。

    对应 docs/02 §6.3。

    Args:
        per_case: 每条用例的结果字典，需含：
            ``retrieved``（chunk_id 列表）、``gold_chunks``（列表）、
            ``answer``（str）、``citations``（list）、
            ``evidence``（list[str]，可省）。
        valid_chunk_ids: 知识库中真实存在的 chunk_id（用于引用可溯源率）。

    Returns:
        指标字典。
    """
    if not per_case:
        return {}
    retrieved_all = [list(c.get("retrieved") or []) for c in per_case]
    golds = [list(c.get("gold_chunks") or []) for c in per_case]
    answers = [str(c.get("answer") or "") for c in per_case]
    evidences = [
        list(c.get("evidence") or []) for c in per_case
    ]
    all_cites = [list(c.get("citations") or []) for c in per_case]
    counts = [len(r) for r in retrieved_all]

    out = {
        "recall_at_5": _safe_div(
            sum(recall_at_k(r, g, 5) for r, g in zip(retrieved_all, golds)), len(per_case)
        ),
        "recall_at_10": _safe_div(
            sum(recall_at_k(r, g, 10) for r, g in zip(retrieved_all, golds)), len(per_case)
        ),
        "precision_at_5": _safe_div(
            sum(precision_at_k(r, g, 5) for r, g in zip(retrieved_all, golds)), len(per_case)
        ),
        "mrr": _safe_div(
            sum(mrr(r, g) for r, g in zip(retrieved_all, golds)), len(per_case)
        ),
        "ndcg_at_10": _safe_div(
            sum(ndcg_at_k(r, g, 10) for r, g in zip(retrieved_all, golds)), len(per_case)
        ),
        "empty_retrieval_rate": empty_retrieval_rate(counts),
    }
    if valid_chunk_ids:
        out["citation_traceable_rate"] = citation_traceable_rate(
            all_cites, valid_chunk_ids
        )
    if any(evidences):
        flat_ans: list[str] = []
        flat_ev: list[str] = []
        for a, ev in zip(answers, evidences):
            flat_ans.append(a)
            flat_ev.append("\n".join(ev))
        out["ground_truth_support_rate"] = ground_truth_support_rate(flat_ans, flat_ev)
    return out


def compute_l3_numerical_metrics(per_case: Sequence[dict[str, Any]]) -> dict[str, float]:
    """汇总 L3 数值问答指标。

    对应 docs/02 §6.4.1。

    Args:
        per_case: 每条用例结果字典。

    Returns:
        指标字典。
    """
    if not per_case:
        return {}
    return {
        "sql_executable_rate": sql_executable_rate(per_case),
        "sql_semantic_correct_rate": sql_semantic_correct_rate(per_case),
        "execution_accuracy": execution_accuracy(per_case),
        "tolerance_accuracy": tolerance_accuracy(per_case),
        "multi_hop_accuracy": multi_hop_accuracy(per_case),
        "time_semantics_accuracy": time_semantics_accuracy(per_case),
        "clarification_accuracy": clarification_accuracy(per_case),
        "refusal_accuracy": refusal_accuracy(per_case),
    }


def compute_l3_case_metrics(per_case: Sequence[dict[str, Any]]) -> dict[str, float]:
    """汇总 L3 用例生成指标。

    对应 docs/02 §6.4.2。

    Args:
        per_case: 每条用例结果字典，需含 ``cases``（生成用例列表）。

    Returns:
        指标字典。
    """
    if not per_case:
        return {}
    all_cases: list[dict[str, Any]] = []
    for c in per_case:
        all_cases.extend(c.get("cases") or [])
    required = (
        "case_id", "title", "category", "preconditions", "test_steps",
        "test_items", "expected_result", "pass_criteria", "fail_criteria",
    )
    sims: list[float] = []
    for c in per_case:
        sims.extend(float(s) for s in (c.get("similarities") or []))
    out = {
        "case_field_completeness": case_field_completeness(all_cases, required),
        "case_limit_traceable_rate": case_limit_traceable_rate(all_cases),
        "case_unit_consistent_rate": case_unit_consistent_rate(all_cases),
        "case_limit_sane_rate": case_limit_sane_rate(all_cases),
    }
    if any("format_valid" in c for c in per_case):
        out["case_format_valid_rate"] = case_format_valid_rate(per_case)
    if sims:
        out["case_duplicate_rate"] = case_duplicate_rate(sims)
    return out


def compute_l3_hallucination_metrics(per_case: Sequence[dict[str, Any]]) -> dict[str, float]:
    """汇总 L3 幻觉率指标。

    对应 docs/02 §6.4.4。

    Args:
        per_case: 每条用例结果字典。

    Returns:
        指标字典。
    """
    if not per_case:
        return {}
    return {
        "numeric_hallucination_rate": numeric_hallucination_rate(per_case),
        "citation_hallucination_rate": citation_hallucination_rate(per_case),
        "logical_hallucination_rate": logical_hallucination_rate(per_case),
        "overall_hallucination_rate": overall_hallucination_rate(per_case),
        "unsupported_claim_rate": unsupported_claim_rate(per_case),
    }


def compute_l3_attribution_metrics(per_case: Sequence[dict[str, Any]]) -> dict[str, float]:
    """汇总 L3 归因指标。

    对应 docs/02 §6.4.3。

    Args:
        per_case: 每条用例结果字典。

    Returns:
        指标字典。
    """
    if not per_case:
        return {}
    return {
        "root_cause_top1": root_cause_top1(per_case),
        "root_cause_top3": root_cause_top3(per_case),
        "wrong_attribution_rate": wrong_attribution_rate(per_case),
        "correct_abstain_rate": correct_abstain_rate(per_case),
        "attribution_evidence_rate": attribution_evidence_rate(per_case),
    }


def compute_l1_metrics(per_case: Sequence[dict[str, Any]]) -> dict[str, float]:
    """汇总 L1 系统层指标。

    对应 docs/02 §6.2。

    Args:
        per_case: 每条用例结果字典，需含 ``ttft_ms`` / ``e2e_ms`` /
            ``tool_calls`` / ``usage`` 等。

    Returns:
        指标字典。
    """
    if not per_case:
        return {}
    ttfts = [float(c.get("ttft_ms") or 0.0) for c in per_case]
    e2es = [float(c.get("e2e_ms") or 0.0) for c in per_case]
    calls = [int(c.get("tool_calls") or 0) for c in per_case]
    usages = [c.get("usage") or {} for c in per_case]
    tin, tout = tokens_per_request(usages)
    out = {
        "ttft_p50": ttft_p50(ttfts),
        "ttft_p95": ttft_p95(ttfts),
        "e2e_latency_p50": e2e_latency_p50(e2es),
        "e2e_latency_p95": e2e_latency_p95(e2es),
        "tool_calls_per_request": tool_calls_per_request(calls),
        "tokens_per_request_in": tin,
        "tokens_per_request_out": tout,
    }
    if any("ok_calls" in c for c in per_case):
        out["tool_success_rate"] = tool_success_rate(per_case)
    if any("cache_hit" in c for c in per_case):
        out["cache_hit_rate"] = cache_hit_rate(per_case)
    return out


def compute_all(
    per_case: Sequence[dict[str, Any]],
    valid_chunk_ids: Sequence[str] = (),
) -> dict[str, dict[str, float]]:
    """一次性汇总四层指标。

    对应 docs/02 §6.1 总体框架。

    Args:
        per_case: 每条用例结果字典。
        valid_chunk_ids: 真实 chunk_id 集合。

    Returns:
        ``{"L1": {...}, "L2": {...}, "L3": {...}}``。
    """
    return {
        "L1": compute_l1_metrics(per_case),
        "L2": compute_l2_metrics(per_case, valid_chunk_ids),
        "L3": {
            **compute_l3_numerical_metrics(per_case),
            **compute_l3_case_metrics(per_case),
            **compute_l3_hallucination_metrics(per_case),
            **compute_l3_attribution_metrics(per_case),
        },
    }


def attribute_failure(case: dict[str, Any]) -> str:
    """失败归因分解（docs/02 §6.6）。

    错误答案的失败闭环分解::

        检索未召回        → L2: Recall@k 未达标
        召回了但排序靠后  → L2: MRR/NDCG 未达标
        召回了但模型误用  → L3: 上下文选择/指令遵循问题 → Prompt/模型
        SQL 本身错        → L3: SQL 语义正确率未达标 → Schema Linking/规则
        数据本身缺失/错    → 数据治理问题（绕过 Agent 层）
        用户意图理解错    → 澄清率/意图分类问题

    Args:
        case: 单条用例结果字典，需含 ``correct`` / ``retrieved`` /
            ``gold_chunks`` / ``executable`` / ``semantic_correct`` /
            ``need_clarify`` 等字段。

    Returns:
        归因标签字符串。
    """
    if case.get("correct") or case.get("exact_match"):
        return "正确"
    gold = set(case.get("gold_chunks") or [])
    retrieved = list(case.get("retrieved") or [])
    if not retrieved:
        return "数据缺失/错" if case.get("data_missing") else "检索未召回"
    if gold and not (gold & set(retrieved)):
        return "检索未召回"
    if case.get("need_clarify") and not case.get("clarified"):
        return "用户意图理解错"
    if case.get("executable") and not case.get("semantic_correct"):
        return "SQL错"
    if case.get("executable"):
        return "召回了但模型误用"
    # gold 召回了但排在后面
    positions = [i + 1 for i, c in enumerate(retrieved) if c in gold]
    if positions and min(positions) > 1:
        return "召回了排序靠后"
    return "召回了但模型误用"
