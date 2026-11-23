"""评测层模块。

对应设计文档：
    docs/02_场景方案_STDF分析与测试用例生成.md
        §6 评测指标体系（四层）
        §7 评测集建设方案（分层配比 / 回归流水线 / LLM-as-Judge 边界）
    docs/01_架构方案_RAG_Agent.md §2.12 评测层

对外暴露：
    * :mod:`agent_platform.eval.metrics` —— 四层指标计算
    * :mod:`agent_platform.eval.judge` —— LLM-as-Judge + rubric + Kappa 校准
    * :mod:`agent_platform.eval.runner` —— 评测 Runner（回归流水线）
"""

from __future__ import annotations

from agent_platform.eval.judge import (
    CalibrationRecord,
    CalibrationStore,
    Judge,
    JudgeResult,
    RUBRIC,
    cohens_kappa,
)
from agent_platform.eval.metrics import (
    METRIC_TARGETS,
    attribute_failure,
    compute_all,
    compute_l1_metrics,
    compute_l2_metrics,
    compute_l3_attribution_metrics,
    compute_l3_case_metrics,
    compute_l3_hallucination_metrics,
    compute_l3_numerical_metrics,
)
from agent_platform.eval.runner import (
    GATE_RULES,
    EvalCase,
    EvalOutcome,
    EvalRunner,
    load_dataset,
)

__all__ = [
    "METRIC_TARGETS",
    "Judge",
    "JudgeResult",
    "RUBRIC",
    "CalibrationStore",
    "CalibrationRecord",
    "cohens_kappa",
    "EvalRunner",
    "EvalCase",
    "EvalOutcome",
    "GATE_RULES",
    "load_dataset",
    "attribute_failure",
    "compute_all",
    "compute_l1_metrics",
    "compute_l2_metrics",
    "compute_l3_numerical_metrics",
    "compute_l3_case_metrics",
    "compute_l3_hallucination_metrics",
    "compute_l3_attribution_metrics",
]
