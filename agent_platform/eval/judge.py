"""LLM-as-Judge 封装 + 评分 rubric 模板 + 人工校准子集记录机制。

对应设计文档：
    docs/02_场景方案_STDF分析与测试用例生成.md
        §6.4 评测层 —— LLM-as-Judge 语义质量打分（相关性/完整性/忠实度）
        §7.3 标注规范与质量控制 —— 一致性门槛 Cohen's Kappa ≥ 0.75
        §7.5 LLM-as-Judge 的使用边界 —— 可自动评估 vs 必须人工评估
        §7.5 Judge 模型选型与校准 —— Kappa < 0.6 调整 rubric；成对比较；
              打分前不透露被评对象来源；随机交换 A/B 顺序
    docs/01_架构方案_RAG_Agent.md
        §2.12 评测层 —— 「LLM-as-Judge」「回归与门禁」

**使用边界（docs/02 §7.5）**：
    可自动评估：格式合规、字段完整、限值可溯源、数值是否存在于数据中、
                引用是否存在且内容匹配、相似度/覆盖率等客观统计、
                SQL 是否可执行/语义等价、拒答/澄清行为是否符合规则。
    必须人工评估：专家盲评 1-5 分（整体质量）、根因是否"专业上合理"、
                归因与现场实际是否一致、步骤在机台上的可执行性、
                是否遗漏了关键失效模式、报告表述是否适合发给客户。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

__all__ = [
    "Judge",
    "RUBRIC",
    "JudgeResult",
    "cohens_kappa",
    "CalibrationRecord",
    "CalibrationStore",
    "JUDGE_MODEL_PAIRS",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Judge 模型互评配对（docs/02 §7.5：Judge 用不同模型避免自评偏差）
# ---------------------------------------------------------------------------
JUDGE_MODEL_PAIRS: dict[str, str] = {
    "gpt-oss-20b": "qwen2.5-14b",
    "qwen2.5-14b": "gpt-oss-20b",
}


# ---------------------------------------------------------------------------
# 评分 rubric 模板（各维度 1-5 分）
# ---------------------------------------------------------------------------
RUBRIC: dict[str, dict[str, Any]] = {
    "relevance": {
        "name": "相关性",
        "question": "回答是否直接针对用户的问题？是否包含无关内容？",
        "anchors": {
            5: "完全切题，只回答被问的内容",
            4: "切题，个别次要信息冗余",
            3: "基本切题但有跑题段落",
            2: "部分切题，主要内容答非所问",
            1: "完全未回答问题",
        },
        "auto_evaluable": True,
    },
    "completeness": {
        "name": "完整性",
        "question": "回答是否覆盖了问题所需的全部要点（数值、限值、条件、步骤）？",
        "anchors": {
            5: "所有必需要素齐备",
            4: "缺少一个次要要素",
            3: "缺少一个主要要素",
            2: "缺少多个主要要素",
            1: "几乎没有提供有效信息",
        },
        "auto_evaluable": True,
    },
    "faithfulness": {
        "name": "忠实度",
        "question": "回答中的每个数值/断言是否都能在参考资料中找到依据？有无编造？",
        "anchors": {
            5: "全部断言可溯源，无编造",
            4: "个别表述模糊但无编造",
            3: "存在一处无明确依据的断言",
            2: "存在多处无依据断言或数值偏差",
            1: "大面积编造",
        },
        "auto_evaluable": True,
    },
    "citation_validity": {
        "name": "引用有效性",
        "question": "引用的 [doc_id:chunk_id] 是否存在且内容确实支持对应断言？",
        "anchors": {
            5: "全部引用有效且精准支持断言",
            4: "引用基本有效，个别位置不够精确",
            3: "部分引用无效或引用位置错位",
            2: "多数引用无效",
            1: "引用基本全部虚构",
        },
        "auto_evaluable": True,
    },
    "professional_root_cause": {
        "name": "根因专业性",
        "question": "根因分析在工程技术领域是否专业、是否符合 FMEA 规范？",
        "anchors": {5: "专业且证据充分", 3: "方向对但论证不足", 1: "明显不专业"},
        "auto_evaluable": False,  # docs/02 §7.5：必须人工评估
    },
    "executability": {
        "name": "步骤可执行性",
        "question": "测试步骤能否在机台上被复现（参数完整、条件明确）？",
        "anchors": {5: "可直接执行", 3: "需补充信息", 1: "无法执行"},
        "auto_evaluable": False,  # docs/02 §7.5：必须人工评估
    },
    "expertise_blind_score": {
        "name": "专家盲评总分",
        "question": "专家在不知来源情况下打的整体质量分（1-5）。",
        "anchors": {5: "可直接采用", 3: "需少量修改", 1: "不可用"},
        "auto_evaluable": False,  # docs/02 §7.5：必须人工评估
    },
}


# ---------------------------------------------------------------------------
# Judge 结果
# ---------------------------------------------------------------------------
@dataclass
class JudgeResult:
    """一次评分的结构化结果。

    Attributes:
        dimensions: 各维度得分 ``{dim: score}``。
        overall: 总分（各自动维度均值，1-5）。
        reasoning: Judge 给出的理由。
        raw: Judge 原始输出。
        model: 使用的 Judge 模型。
        ok: 是否成功。
        error: 错误信息。
        needs_human: 命中的「必须人工评估」维度。
    """

    dimensions: dict[str, float] = field(default_factory=dict)
    overall: float = 0.0
    reasoning: str = ""
    raw: str = ""
    model: str = ""
    ok: bool = True
    error: str | None = None
    needs_human: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转 dict。"""
        return {
            "dimensions": self.dimensions,
            "overall": self.overall,
            "reasoning": self.reasoning,
            "model": self.model,
            "ok": self.ok,
            "error": self.error,
            "needs_human": self.needs_human,
        }


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------
class Judge:
    """LLM-as-Judge。

    Args:
        model: 被评对象模型 alias（用于自动选择互评的 Judge 模型）。
        judge_model: 强制指定 Judge 模型；None 时按 :data:`JUDGE_MODEL_PAIRS` 自动互评。
        llm: LLM 客户端（可为 None，None 时 Judge 直接返回不可用）。
        dimensions: 参与评分的维度名列表（默认 4 个可自动评估维度）。
    """

    def __init__(
        self,
        model: str = "gpt-oss-20b",
        judge_model: str | None = None,
        llm: Any = None,
        dimensions: Sequence[str] | None = None,
    ) -> None:
        self.model = model
        self.judge_model = judge_model or JUDGE_MODEL_PAIRS.get(model, "qwen2.5-14b")
        self.llm = llm
        self.dimensions = list(
            dimensions or ["relevance", "completeness", "faithfulness", "citation_validity"]
        )

    # ------------------------------------------------------------------
    def judge(
        self,
        query: str,
        answer: str,
        context: str = "",
        gold_answer: str = "",
        extra_instructions: str = "",
    ) -> JudgeResult:
        """按 rubric 对一条回答打分。

        Args:
            query: 用户问题。
            answer: 待评回答。
            context: 参考资料（检索到的证据）。
            gold_answer: 标准答案（可选，作为对照但不透露被评来源）。
            extra_instructions: 额外评分要求。

        Returns:
            :class:`JudgeResult`。LLM 不可用时 ``ok=False``。
        """
        if self.llm is None:
            return JudgeResult(ok=False, error="未配置 LLM", model=self.judge_model)

        prompt = self.build_prompt(
            query, answer, context, gold_answer, extra_instructions
        )
        resp = self.llm.chat(
            [{"role": "system", "content": "你是严格、可复现的评测裁判。"},
             {"role": "user", "content": prompt}],
            model=self.judge_model,
            temperature=0.0,
            max_tokens=2048,
        )
        if not resp.ok:
            return JudgeResult(ok=False, error=resp.error, model=self.judge_model)
        return self.parse(resp.text, self.judge_model)

    # ------------------------------------------------------------------
    def build_prompt(
        self,
        query: str,
        answer: str,
        context: str = "",
        gold_answer: str = "",
        extra_instructions: str = "",
    ) -> str:
        """构造评分 prompt。

        注意（docs/02 §7.5）：**不透露被评对象来源**（模型名/来源标注），
        避免来源偏好与位置偏好。

        Args:
            query: 用户问题。
            answer: 待评回答。
            context: 参考资料。
            gold_answer: 标准答案。
            extra_instructions: 额外要求。

        Returns:
            prompt 文本。
        """
        parts: list[str] = [
            "请对下面这条回答按各维度 1-5 分打分。",
            "评分锚点：",
        ]
        for dim in self.dimensions:
            spec = RUBRIC.get(dim)
            if not spec:
                continue
            parts.append(f"\n### {dim}（{spec['name']}）")
            parts.append(f"评估问题：{spec['question']}")
            for score in sorted(spec["anchors"], reverse=True):
                parts.append(f"  {score} 分：{spec['anchors'][score]}")
        parts.append("\n【问题】\n" + (query or ""))
        if context:
            parts.append("\n【参考资料】\n" + context[:8000])
        parts.append("\n【待评回答】\n" + (answer or ""))
        if gold_answer:
            parts.append("\n【参考要点】（仅作对照，不要因措辞不同而扣分）\n" + gold_answer)
        if extra_instructions:
            parts.append("\n【额外要求】\n" + extra_instructions)
        parts.append(
            '\n严格输出 JSON，格式：{"scores": {"<维度名>": 分数}, '
            '"reasoning": "打分理由", "summary": "整体结论"}'
        )
        return "\n".join(parts)

    # ------------------------------------------------------------------
    @staticmethod
    def parse(raw: str, model: str = "") -> JudgeResult:
        """解析 Judge 输出。

        Args:
            raw: Judge 原始文本。
            model: Judge 模型名。

        Returns:
            :class:`JudgeResult`。
        """
        text = (raw or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        text = text.strip()
        data: Any = None
        try:
            data = json.loads(text)
        except Exception:
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                try:
                    data = json.loads(text[start : end + 1])
                except Exception:
                    data = None
        if not isinstance(data, dict):
            # 退化解析：抓 "维度: 分数" 形式
            dims = {
                m.group(1): float(m.group(2))
                for m in re.finditer(r'"?(relevance|completeness|faithfulness|'
                                     r'citation_validity)"?\s*[:：]\s*(\d(?:\.\d)?)', text)
            }
            return JudgeResult(
                dimensions=dims,
                overall=_mean(dims.values()),
                reasoning=text[:500],
                raw=raw,
                model=model,
                ok=bool(dims),
                error=None if dims else "Judge 输出无法解析为 JSON",
            )
        scores = data.get("scores") or data.get("dimensions") or {}
        dims: dict[str, float] = {}
        for k, v in scores.items():
            try:
                dims[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        return JudgeResult(
            dimensions=dims,
            overall=_mean(dims.values()),
            reasoning=str(data.get("reasoning") or data.get("summary") or ""),
            raw=raw,
            model=model,
            ok=True,
            needs_human=[
                d for d in dims
                if RUBRIC.get(d, {}).get("auto_evaluable") is False
            ],
        )


# ---------------------------------------------------------------------------
# Kappa 与人工校准
# ---------------------------------------------------------------------------
def cohens_kappa(
    labels_a: Sequence[Any],
    labels_b: Sequence[Any],
) -> float:
    """Cohen's Kappa：两个标注者的一致性系数。

    公式：``κ = (Po - Pe) / (1 - Pe)``，其中 ``Po`` 为实际一致率，
    ``Pe`` 为随机一致率（按类别边缘分布计算）。

    用途（docs/02 §7.3 / §7.5）：
        * 标注入库门槛 **Kappa ≥ 0.75**；
        * Judge 与人工一致性校准，**Kappa < 0.6 → 调整 rubric 或更换模型**。

    Args:
        labels_a: 标注者 A 的标签序列。
        labels_b: 标注者 B 的标签序列。

    Returns:
        Kappa 值（-1 ~ 1）。样本为空或单一类别时返回 0.0。
    """
    n = min(len(labels_a), len(labels_b))
    if n == 0:
        return 0.0
    a = list(labels_a[:n])
    b = list(labels_b[:n])
    observed = sum(1 for x, y in zip(a, b) if x == y) / n
    cats = set(a) | set(b)
    expected = 0.0
    for c in cats:
        pa = sum(1 for x in a if x == c) / n
        pb = sum(1 for y in b if y == c) / n
        expected += pa * pb
    if abs(1.0 - expected) < 1e-12:
        return 0.0
    return (observed - expected) / (1.0 - expected)


@dataclass
class CalibrationRecord:
    """一条人工校准记录。

    Attributes:
        case_id: 用例 ID。
        dimension: 评估维度。
        judge_score: Judge 给出的分数。
        human_score: 人工给出的分数。
        note: 备注。
    """

    case_id: str
    dimension: str
    judge_score: float
    human_score: float
    note: str = ""


class CalibrationStore:
    """人工校准子集的记录机制。

    对应 docs/02 §7.5：「用 100~200 条人工标注样本建立"人—Judge"对照，
    算 Judge 与人工的 Kappa」。本类负责**记录**人工标注并**计算 Kappa**，
    阈值判断留给调用方。

    Args:
        path: JSONL 落盘路径（None 表示仅内存）。
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = path
        self.records: list[CalibrationRecord] = []
        if path and os.path.isfile(path):
            self.load()

    # ------------------------------------------------------------------
    def add(
        self,
        case_id: str,
        dimension: str,
        judge_score: float,
        human_score: float,
        note: str = "",
    ) -> CalibrationRecord:
        """记录一条人工标注。

        Args:
            case_id: 用例 ID。
            dimension: 维度名。
            judge_score: Judge 分数。
            human_score: 人工分数。
            note: 备注。

        Returns:
            记录对象。
        """
        rec = CalibrationRecord(case_id, dimension, float(judge_score),
                                float(human_score), note)
        self.records.append(rec)
        return rec

    # ------------------------------------------------------------------
    def kappa(self, dimension: str | None = None) -> float:
        """计算指定维度（或全部维度）的 Judge-人工 Kappa。

        Args:
            dimension: 维度名；None 表示跨全部维度合并计算。

        Returns:
            Kappa 值。
        """
        recs = [r for r in self.records if dimension is None or r.dimension == dimension]
        if not recs:
            return 0.0
        # 二值化：>=4 分视为「通过」，用于与 docs/02 §6.4.3 「scores>=4」口径对齐
        j = [1 if r.judge_score >= 4 else 0 for r in recs]
        h = [1 if r.human_score >= 4 else 0 for r in recs]
        return cohens_kappa(j, h)

    # ------------------------------------------------------------------
    def status(self, dimension: str | None = None) -> dict[str, Any]:
        """返回校准状态（是否达到阈值）。

        阈值（docs/02 §7.5）：
            * Kappa < 0.6 → **停用该 Judge**（或调整 rubric / 更换模型）；
            * Kappa ≥ 0.75 → 标注质量达标。

        Args:
            dimension: 维度名。

        Returns:
            状态字典。
        """
        k = self.kappa(dimension)
        n = len([r for r in self.records
                 if dimension is None or r.dimension == dimension])
        if k < 0.6:
            verdict = "不合格：Kappa < 0.6，需调整 rubric 或更换 Judge 模型"
        elif k < 0.75:
            verdict = "可用但未达标注门槛：0.6 ≤ Kappa < 0.75，进入复核队列"
        else:
            verdict = "合格：Kappa ≥ 0.75"
        return {"kappa": k, "samples": n, "verdict": verdict}

    # ------------------------------------------------------------------
    def save(self) -> None:
        """把记录追加写入 JSONL。"""
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            for r in self.records:
                f.write(
                    json.dumps(
                        {
                            "case_id": r.case_id,
                            "dimension": r.dimension,
                            "judge_score": r.judge_score,
                            "human_score": r.human_score,
                            "note": r.note,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

    def load(self) -> None:
        """从 JSONL 读取记录。"""
        self.records = []
        if not self.path or not os.path.isfile(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                self.records.append(
                    CalibrationRecord(
                        d.get("case_id", ""),
                        d.get("dimension", ""),
                        float(d.get("judge_score") or 0),
                        float(d.get("human_score") or 0),
                        d.get("note", ""),
                    )
                )


# ---------------------------------------------------------------------------
def _mean(values: Any) -> float:
    """算术平均（空集合返回 0）。

    Args:
        values: 数值可迭代对象。

    Returns:
        平均值。
    """
    vals = [float(v) for v in values]
    return sum(vals) / len(vals) if vals else 0.0
