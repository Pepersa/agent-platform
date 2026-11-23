"""RAG 检索增强链 —— 严格实现 docs/01 §3.1 的 10 个步骤。

对应设计文档：
    docs/01_架构方案_RAG_Agent.md
        §2.7 知识层 —— 「混合检索融合」「Reranker」「Metadata 与 ACL」
        §3.1 RAG 检索增强流程 —— **本文件的 10 个步骤逐一对应**
    docs/02_场景方案_STDF分析与测试用例生成.md
        §3.5 RAG 设计 —— 检索流程（查询改写 → BM25 ⊕ Dense → RRF(k=60) → Reranker → Top-k）

参数（**必须与文档一致，不得自创**）：
    * 混合检索 = BM25 + 向量，RRF 融合 **k = 60**
    * 召回 **top-50** → 去重 → Reranker 精排 → 取 **top-6**
    * 上下文预算：System(1–2K) + Few-shot(0–2K) + 历史(≤2K) + 检索(**≤3K**) + 工具结果(≤4K)
    * Embedding 用 bge-m3，Reranker 用 bge-reranker-v2-m3
    * 权限/密级过滤在**检索前**完成（过滤条件下推到检索层）
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from agent_platform.core.citations import (
    CITE_LABELS,
    CITATION_RE,
    extract_citations,
    validate_citations,
)
from agent_platform.knowledge.chunker import estimate_tokens
from agent_platform.knowledge.embed import Embedder
from agent_platform.knowledge.reranker import Reranker
from agent_platform.knowledge.store import AclFilter, ChunkStore

__all__ = [
    "Retriever",
    "RetrievalResult",
    "UserContext",
    "MIN_SCORE_THRESHOLD",
    "RRF_K",
    "RECALL_N",
    "FINAL_TOP_K",
    "RETRIEVAL_TOKEN_BUDGET",
    "extract_slots",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# docs/01 §3.1 常量（不得自创）
# ---------------------------------------------------------------------------
RRF_K = 60                    # 第 4 步：RRF 融合常数 k = 60
RECALL_N = 50                 # 第 5 步：召回 top-50
FINAL_TOP_K = 6               # 第 6 步：Reranker 精排取 top-6
RETRIEVAL_TOKEN_BUDGET = 3000  # 第 7 步：检索上下文预算 ≤ 3K token

# 第 10 步兜底阈值：最高分低于该值 → 明确拒答。
# 说明：RRF 分数量纲为 Σ1/(60+rank)，top-1 双路命中约 0.033，
# 单路命中约 0.016。阈值取 0.012 对应「单路 top-3 以内」的弱命中，
# 低于此值视为「未在知识库中找到」。
MIN_SCORE_THRESHOLD = 0.012


# ---------------------------------------------------------------------------
# 用户上下文
# ---------------------------------------------------------------------------
@dataclass
class UserContext:
    """用户上下文（用于**检索前**权限与密级过滤）。

    对应 docs/01 §3.1 第 2 步与 §2.7「Metadata 与 ACL」。

    Attributes:
        user_id: 用户 ID。
        roles: 用户角色列表。
        max_security_level: 用户可见的最高密级。
        product_line: 用户可见的产品线。
        dept: 部门（预留）。
        doc_ids: 限定可访问的文档集合（None 表示不限）。
    """

    user_id: str = "anonymous"
    roles: list[str] = field(default_factory=list)
    max_security_level: str = "internal"
    product_line: str | None = None
    dept: str | None = None
    doc_ids: list[str] | None = None

    def to_acl_filter(self) -> AclFilter:
        """转成存储层的 ACL 过滤器。"""
        return AclFilter(
            max_security_level=self.max_security_level,
            product_line=self.product_line,
            roles=self.roles,
            doc_ids=self.doc_ids,
        )


# ---------------------------------------------------------------------------
# 检索结果
# ---------------------------------------------------------------------------
@dataclass
class RetrievalResult:
    """一次检索的完整结果。

    Attributes:
        hits: 命中的 chunk 列表。
        reason: 拒答/降级原因（为空表示正常命中）。
        queries: 第 1 步生成的改写查询列表。
        slots: 第 1 步抽取的槽位。
        bm25_hits / vector_hits: 两路召回数量。
        dedup_count: 去重后候选数。
        total_ms: 总耗时。
        stage_trace: 每一步的耗时与规模（可观测性，docs/01 §2.11）。
    """

    hits: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    queries: list[str] = field(default_factory=list)
    slots: dict[str, Any] = field(default_factory=dict)
    bm25_hits: int = 0
    vector_hits: int = 0
    dedup_count: int = 0
    total_ms: float = 0.0
    stage_trace: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转 dict（报告落盘用）。"""
        return {
            "hits": self.hits,
            "reason": self.reason,
            "queries": self.queries,
            "slots": self.slots,
            "bm25_hits": self.bm25_hits,
            "vector_hits": self.vector_hits,
            "dedup_count": self.dedup_count,
            "total_ms": self.total_ms,
            "stage_trace": self.stage_trace,
        }


# ---------------------------------------------------------------------------
# 查询理解（槽位抽取）
# ---------------------------------------------------------------------------
_MODEL_RE = re.compile(r"\b([A-Z]{2,}[A-Z0-9\-]{1,}[A-Z0-9])\b")
_TESTNUM_RE = re.compile(r"\b(T\d{2,4}[A-Z0-9_]*)\b")
_PRODUCT_RE = re.compile(r"\b((?:CIS|PXA|PMIC|DDR5|TSW|STW|MCU)[-_]?[A-Z0-9]{2,})\b")
_TIME_RE = re.compile(
    r"(20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}日?)|"
    r"(上上周|上周|本周|这周|上个月|本月|这个月|上个季度|上季度|去年同期|昨天|今天|最近\d+天)"
)
_LOT_RE = re.compile(r"\b(lot[-_ ]?[A-Za-z0-9]{3,})\b", re.IGNORECASE)


def extract_slots(query: str) -> dict[str, Any]:
    """抽取实体/型号/时间/产品线槽位。

    对应 docs/01 §3.1 第 1 步：「小模型判定：抽取实体/型号/时间/产品线槽位」。
    这里用**规则 + 小模型**双通道：规则先跑（小模型不可用时仍可用），小模型结果合并补充。

    Args:
        query: 原始查询。

    Returns:
        槽位字典，键包括 ``models`` / ``test_nums`` / ``products`` /
        ``times`` / ``lots`` / ``keywords``。
    """
    q = query or ""
    slots: dict[str, Any] = {
        "models": sorted(set(_MODEL_RE.findall(q))),
        "test_nums": sorted(set(_TESTNUM_RE.findall(q.upper()))),
        "products": sorted(set(_PRODUCT_RE.findall(q.upper()))),
        "times": sorted(set(_TIME_RE.findall(q))),
        "lots": sorted(set(m.group(1).upper() for m in _LOT_RE.finditer(q))),
    }
    slots["keywords"] = [
        w for w in re.split(r"[\s,，。？?、；;：:]+", q) if len(w) >= 2
    ][:20]
    return slots


# ---------------------------------------------------------------------------
# Retriever
# ---------------------------------------------------------------------------
class Retriever:
    """RAG 检索链（docs/01 §3.1 全流程）。

    Args:
        store: chunk 存储。
        embedder: embedding 服务。
        reranker: 重排服务。
        llm: LLM 客户端（用于查询改写与答案生成）；可为 None（纯检索模式）。
        rrf_k: RRF 常数（默认 60）。
        recall_n: 召回规模（默认 50）。
        top_k: 最终返回条数（默认 6）。
        min_score: 兜底阈值。
        retrieval_budget: 检索上下文 token 预算（默认 3000）。
        llm_model: 用于查询改写/生成的小模型 alias（None 则全规则）。
    """

    def __init__(
        self,
        store: ChunkStore,
        embedder: Embedder | None = None,
        reranker: Reranker | None = None,
        llm: Any = None,
        rrf_k: int = RRF_K,
        recall_n: int = RECALL_N,
        top_k: int = FINAL_TOP_K,
        min_score: float = MIN_SCORE_THRESHOLD,
        retrieval_budget: int = RETRIEVAL_TOKEN_BUDGET,
        llm_model: str | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder or Embedder()
        self.reranker = reranker or Reranker(embedder=self.embedder)
        self.llm = llm
        self.llm_model = llm_model
        self.rrf_k = int(rrf_k)
        self.recall_n = int(recall_n)
        self.top_k = int(top_k)
        self.min_score = float(min_score)
        self.retrieval_budget = int(retrieval_budget)

    # ==================================================================
    # 主入口
    # ==================================================================
    def retrieve(
        self,
        query: str,
        user_ctx: UserContext | None = None,
        top_k: int | None = None,
        recall_n: int | None = None,
    ) -> list[dict[str, Any]]:
        """执行检索链的第 1–7 步（含召回、融合、去重、重排、上下文组装）。

        Args:
            query: 用户查询。
            user_ctx: 用户上下文（ACL 过滤用）。
            top_k: 最终条数（默认 6）。
            recall_n: 召回规模（默认 50）。

        Returns:
            命中列表，每项含 ``chunk_id`` / ``text`` / ``score`` / ``rrf_score`` /
            ``rerank_score`` / ``source`` 等字段。**空召回时返回空列表**，
            拒答原因见 :meth:`retrieve_with_trace`。
        """
        res = self.retrieve_with_trace(
            query, user_ctx, top_k=top_k, recall_n=recall_n
        )
        return res.hits

    def retrieve_with_trace(
        self,
        query: str,
        user_ctx: UserContext | None = None,
        top_k: int | None = None,
        recall_n: int | None = None,
    ) -> RetrievalResult:
        """执行检索链并返回带可观测 trace 的结果。

        Args:
            query: 用户查询。
            user_ctx: 用户上下文。
            top_k: 最终条数。
            recall_n: 召回规模。

        Returns:
            :class:`RetrievalResult`。
        """
        import time

        t_start = time.perf_counter()
        ctx = user_ctx or UserContext()
        k = int(top_k) if top_k else self.top_k
        n = int(recall_n) if recall_n else self.recall_n
        result = RetrievalResult()

        # ★ 2026-11-23 性能修复：为每一阶段记录**耗时**（ms）。
        #   原来 trace 只有规模（召回了多少条），看不出「时间花在哪」，
        #   导致定位「/kb/search 要 16 秒」只能靠反复插桩。
        #   现在每步都带 ``ms`` 字段，前端 / 回归脚本都能直接读。
        def _mark() -> float:
            """返回距上一步的耗时（毫秒）。"""
            now = time.perf_counter()
            delta = (now - _last[0]) * 1000
            _last[0] = now
            return round(delta, 1)

        _last = [t_start]

        # ---------- 第 1 步：查询理解 / 改写 ----------
        slots = extract_slots(query)
        queries = self._rewrite_queries(query, slots)
        result.queries = queries
        result.slots = slots
        result.stage_trace.append(
            {"step": 1, "name": "查询理解/改写", "queries": queries,
             "slots": {k2: v for k2, v in slots.items() if v},
             "ms": _mark()}
        )

        # ---------- 第 2 步：检索前权限与密级过滤（过滤条件下推） ----------
        acl = ctx.to_acl_filter()
        result.stage_trace.append(
            {"step": 2, "name": "检索前ACL过滤", "acl": acl.to_dict(),
             "ms": _mark()}
        )

        # ---------- 第 3 步：多路召回（BM25 + 向量） ----------
        bm25_pool: list[dict[str, Any]] = []
        vec_pool: list[dict[str, Any]] = []
        _t_bm25 = 0.0
        _t_embed = 0.0
        _t_vec = 0.0
        for q in queries:
            try:
                _t0 = time.perf_counter()
                bm25_pool.extend(
                    self.store.search_bm25(q, top_n=n, acl_filter=acl)
                )
                _t_bm25 += time.perf_counter() - _t0
            except Exception as exc:  # noqa: BLE001
                logger.warning("BM25 通道异常（query=%s）：%s", q, exc)
            try:
                _t0 = time.perf_counter()
                qv = self.embedder.embed_query(q)
                _t_embed += time.perf_counter() - _t0
                _t0 = time.perf_counter()
                vec_pool.extend(
                    self.store.search_vector(qv, top_n=n, acl_filter=acl)
                )
                _t_vec += time.perf_counter() - _t0
            except Exception as exc:  # noqa: BLE001
                logger.warning("向量通道异常（query=%s）：%s", q, exc)
        result.bm25_hits = len(bm25_pool)
        result.vector_hits = len(vec_pool)
        result.stage_trace.append(
            {"step": 3, "name": "多路召回", "bm25": len(bm25_pool),
             "vector": len(vec_pool),
             "bm25_ms": round(_t_bm25 * 1000, 1),
             "embed_ms": round(_t_embed * 1000, 1),
             "vector_ms": round(_t_vec * 1000, 1),
             "ms": _mark()}
        )

        # ---------- 第 4 步：RRF 融合（k=60） ----------
        fused = self._rrf_fuse(bm25_pool, vec_pool)
        result.stage_trace.append(
            {"step": 4, "name": "RRF融合", "k": self.rrf_k, "fused": len(fused),
             "ms": _mark()}
        )

        # ---------- 第 5 步：候选规模 top-50 + 去重（chunk_id + 内容哈希） ----------
        candidates = self._dedup(fused, n)
        result.dedup_count = len(candidates)
        result.stage_trace.append(
            {"step": 5, "name": "候选去重", "before": len(fused), "after": len(candidates),
             "ms": _mark()}
        )

        if not candidates:
            result.reason = "未在知识库中找到相关内容"
            result.total_ms = (time.perf_counter() - t_start) * 1000
            result.stage_trace.append(
                {"step": 10, "name": "兜底", "reason": result.reason}
            )
            return result

        # ---------- 第 6 步：Reranker 精排取 top-6 ----------
        try:
            ranked = self.reranker.rerank(query, candidates, top_k=k)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Reranker 异常，回退到 RRF 顺序：%s", exc)
            ranked = candidates[:k]
        for i, c in enumerate(ranked):
            c["rank"] = i + 1
        # ★ 2026-11-23：暴露「实际送进交叉编码器的条数」与
        #   「送进模型的最大序列长度」，便于确认性能优化是否生效。
        _sl = int(getattr(self.reranker, "shortlist", 0) or 0)
        _scored = min(len(candidates), _sl) if _sl > 0 else len(candidates)
        result.stage_trace.append(
            {"step": 6, "name": "Reranker精排", "returned": len(ranked),
             "backend": getattr(self.reranker, "backend", "?"),
             "candidates": len(candidates),
             "scored": _scored,
             "max_length": int(getattr(self.reranker, "max_length", 0) or 0),
             "ms": _mark()}
        )

        # ---------- 第 7 步：上下文组装（检索预算 ≤ 3K token） ----------
        hits = self._assemble_context(ranked)
        result.stage_trace.append(
            {"step": 7, "name": "上下文组装",
             "chunks": len(hits),
             "tokens": sum(estimate_tokens(h["text"]) for h in hits),
             "budget": self.retrieval_budget,
             "ms": _mark()}
        )

        # ---------- 第 10 步（前置判定）：兜底阈值 ----------
        # 阈值判定用 RRF 分（量纲稳定：Σ1/(60+rank)，双路命中 top-1 ≈ 0.033，
        # 单路命中 ≈ 0.016），避免交叉编码器 logits 可能为负导致误拒答。
        top_score = max(
            (float(h.get("rrf_score") or 0.0) for h in hits),
            default=0.0,
        )
        if top_score < self.min_score:
            result.reason = (
                f"未在知识库中找到相关内容（最高分 {top_score:.4f} < "
                f"阈值 {self.min_score}）"
            )
            result.stage_trace.append(
                {"step": 10, "name": "兜底", "reason": result.reason}
            )
            result.hits = []
            result.total_ms = (time.perf_counter() - t_start) * 1000
            return result

        result.hits = hits
        result.total_ms = (time.perf_counter() - t_start) * 1000
        return result

    # ==================================================================
    # 生成 + 校验（第 8、9 步）
    # ==================================================================
    def answer(
        self,
        query: str,
        user_ctx: UserContext | None = None,
        top_k: int | None = None,
        recall_n: int | None = None,
    ) -> dict[str, Any]:
        """执行完整的 RAG 问答链（第 1–10 步）。

        流程：检索 → 组装上下文 → 生成（要求逐句附引用 ``[doc_id:chunk_id]``）→
        引用块存在性校验 → 兜底。

        Args:
            query: 用户查询。
            user_ctx: 用户上下文。
            top_k: 最终条数。
            recall_n: 召回规模。

        Returns:
            字典，含 ``answer`` / ``hits`` / ``citations`` / ``invalid_citations`` /
            ``reason`` / ``refused`` / ``elapsed_ms`` / ``stage_trace``。
        """
        import time

        t0 = time.perf_counter()
        res = self.retrieve_with_trace(query, user_ctx, top_k, recall_n)

        if res.reason:
            return {
                "answer": "未在知识库中找到相关内容，请补充信息或转人工处理。",
                "hits": [],
                "citations": [],
                "invalid_citations": [],
                "reason": res.reason,
                "refused": True,
                "elapsed_ms": (time.perf_counter() - t0) * 1000,
                "stage_trace": res.stage_trace,
                "usage": {},
            }

        context = self.build_context_prompt(res.hits)
        answer_text, usage = self._generate(query, context)
        citations = self.extract_citations(answer_text)
        invalid = self.validate_citations(citations, res.hits)

        res.stage_trace.append(
            {"step": 8, "name": "生成", "chars": len(answer_text),
             "citations": len(citations)}
        )
        res.stage_trace.append(
            {"step": 9, "name": "引用校验", "citations": len(citations),
             "invalid": len(invalid)}
        )

        return {
            "answer": answer_text,
            "hits": res.hits,
            "citations": citations,
            "invalid_citations": invalid,
            "reason": res.reason,
            "refused": not answer_text.strip(),
            "elapsed_ms": (time.perf_counter() - t0) * 1000,
            "stage_trace": res.stage_trace,
            "usage": usage,
        }

    # ------------------------------------------------------------------
    def build_context_prompt(self, hits: Sequence[dict[str, Any]]) -> str:
        """把命中块组装成带引用标记的上下文。

        对应 docs/01 §3.1 第 7 步：上下文预算 System(1–2K) + Few-shot(0–2K) +
        历史(≤2K) + **检索(≤3K)** + 工具结果(≤4K)。

        Args:
            hits: 命中列表。

        Returns:
            上下文字符串。
        """
        parts: list[str] = []
        used = 0
        for h in hits:
            block = (
                f"[{h['doc_id']}:{h['chunk_id']}] "
                f"(来源: {h.get('source')} | 章节: {h.get('section')} | "
                f"版本: {h.get('doc_version')})\n"
                f"{h['text']}"
            )
            tok = estimate_tokens(block)
            if used + tok > self.retrieval_budget:
                logger.info(
                    "检索上下文预算 %d token 已用满，截断后续块（docs/01 §3.1 第 7 步）",
                    self.retrieval_budget,
                )
                break
            parts.append(block)
            used += tok
        return "\n\n---\n\n".join(parts)

    def _generate(self, query: str, context: str) -> tuple[str, dict[str, Any]]:
        """调用 LLM 生成带引用的答案（第 8 步）。

        Args:
            query: 查询。
            context: 组装好的上下文。

        Returns:
            ``(答案文本, token 用量)``。LLM 不可用时返回空串（触发上层拒答）。
        """
        if self.llm is None:
            logger.warning("未配置 LLM，无法生成答案（返回空串触发拒答）。")
            return "", {}
        system = (
            "你是通用技术知识助手。**只能**依据下方【参考资料】回答，"
            "严禁编造任何参数、限值、型号或数值。\n"
            "要求：\n"
            "1. 每个事实性断言后面必须紧跟引用标记，形如 [doc_id:chunk_id]，"
            "chunk_id 必须原样取自参考资料中给出的标记；\n"
            "2. 参考资料不足以回答时，明确回答「未在知识库中找到」，不要猜测；\n"
            "3. 使用中文作答，保留原始数值与单位。"
        )
        user = f"【参考资料】\n{context}\n\n【问题】\n{query}\n\n【回答】"
        resp = self.llm.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=self.llm_model,
            max_tokens=4096,
        )
        if not resp.ok:
            logger.warning("生成失败：%s", resp.error)
            return "", {}
        return resp.text, resp.usage

    # ==================================================================
    # 引用校验（第 9 步）
    # ==================================================================
    # 引用解析/归一化/校验统一实现在 ``agent_platform.core.citations``，
    # 检索生成路径与 Agent ReAct 路径共用同一份实现，避免格式漂移。
    # 详见该模块 docstring：早期两条路径格式不兼容，导致 Agent 路径
    # 的引用从未被校验过。
    CITATION_RE = CITATION_RE

    #: 中文引用前缀（如「来源」），出现时左侧不是 doc_id 而是标签。
    _CITE_LABELS = CITE_LABELS

    @staticmethod
    def extract_citations(answer_text: str) -> list[dict[str, str]]:
        """抽取答案中的引用标记。

        支持两种格式：
            * ``[doc_id:chunk_id]`` —— 检索生成路径
            * ``[来源:chunk_id]``   —— Agent ReAct 路径（左侧为标签）

        同时兼容 ASCII ``[]`` / 全角 ``【】`` / 中文 ``［］`` 三种括号写法、
        全角冒号 ``：`` 与 Unicode 变体连字符。

        Args:
            answer_text: 答案文本。

        Returns:
            ``[{"doc_id":..., "chunk_id":..., "raw": "..."}]``。
            标签形式（``[来源:x]``）的 ``doc_id`` 为空串。
        """
        return extract_citations(answer_text)

    @staticmethod
    def validate_citations(
        citations: Sequence[dict[str, str]], hits: Sequence[dict[str, Any]]
    ) -> list[dict[str, str]]:
        """校验引用块存在性。

        对应 docs/01 §3.1 第 9 步：「引用块存在性校验」。

        Args:
            citations: 抽取到的引用。
            hits: 本次召回的命中块。

        Returns:
            非法引用列表（引用了不存在的 chunk）。
        """
        valid_ids = {h["chunk_id"] for h in hits}
        return validate_citations(citations, valid_ids)

    # ==================================================================
    # 各步骤内部实现
    # ==================================================================
    def _rewrite_queries(
        self, query: str, slots: dict[str, Any]
    ) -> list[str]:
        """第 1 步：生成 2–3 个改写查询。

        对应 docs/01 §3.1 第 1 步：「改写为 2–3 个查询（多查询召回）」。

        策略：
            * 规则通道（永远可用）：原查询 + 型号/测试项精确查询 + 关键实体组合；
            * 小模型通道（LLM 可用时）：让模型做别名归一 / 中英扩展 / 型号归一，
              解析失败则忽略。

        Args:
            query: 原始查询。
            slots: 抽取的槽位。

        Returns:
            查询列表（去重，最多 3 个）。
        """
        out: list[str] = []
        q = (query or "").strip()
        if q:
            out.append(q)

        exact_terms = (slots.get("models") or []) + (slots.get("test_nums") or []) + \
            (slots.get("products") or [])
        if exact_terms:
            out.append(" ".join(dict.fromkeys(exact_terms)))
        kws = [k for k in (slots.get("keywords") or []) if k not in q]
        if kws:
            out.append(" ".join(kws[:6]))

        # 小模型改写
        if self.llm is not None:
            try:
                rewritten = self._llm_rewrite(query, slots)
                for r in rewritten:
                    if r and r not in out:
                        out.append(r)
            except Exception as exc:  # noqa: BLE001
                logger.warning("LLM 查询改写失败，使用规则改写：%s", exc)

        # 去重 + 截断到 3 个
        seen: set[str] = set()
        final: list[str] = []
        for item in out:
            key = item.strip().lower()
            if key and key not in seen:
                seen.add(key)
                final.append(item.strip())
            if len(final) >= 3:
                break
        return final or [q]

    def _llm_rewrite(
        self, query: str, slots: dict[str, Any]
    ) -> list[str]:
        """用小模型做查询改写（别名归一 / 中英扩展 / 型号归一）。

        对应 docs/02 §3.5：「query → 查询改写：别名归一 + 中英扩展 + 型号归一」。

        Args:
            query: 原查询。
            slots: 槽位。

        Returns:
            改写查询列表（失败返回空列表）。
        """
        system = (
            "你是通用知识库的检索查询改写器。请把用户问题改写成 1-2 个更适合"
            "知识库检索的查询变体。规则：型号/编号/指标名必须原样保留；"
            "中文术语可给出英文别名；不要回答问题本身，只输出改写后的查询，"
            "每行一个，最多 2 行。"
        )
        user = (
            f"原查询：{query}\n"
            f"已抽取槽位：{slots.get('models')} / {slots.get('test_nums')} / "
            f"{slots.get('products')} / {slots.get('times')}\n"
            "改写："
        )
        resp = self.llm.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=self.llm_model,
            max_tokens=1024,
            temperature=0.0,
        )
        if not resp.ok or not resp.text:
            return []
        lines = [ln.strip("-*• ").strip() for ln in resp.text.splitlines()]
        return [ln for ln in lines if ln][:2]

    def _rrf_fuse(
        self, bm25_pool: Sequence[dict[str, Any]], vec_pool: Sequence[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """第 4 步：RRF 融合。

        公式（docs/01 §3.1 第 4 步）：``score = Σ_i 1/(k + rank_i)``，``k = 60``，
        其中 ``rank_i`` 是该 chunk 在第 i 路结果中的排名（1-based）。

        Args:
            bm25_pool: BM25 通道结果。
            vec_pool: 向量通道结果。

        Returns:
            融合后的候选列表（按 rrf_score 降序）。
        """
        merged: dict[str, dict[str, Any]] = {}

        def _absorb(pool: Sequence[dict[str, Any]], channel: str) -> None:
            ordered = sorted(pool, key=lambda c: -float(c.get("score") or 0.0))
            for rank, c in enumerate(ordered, start=1):
                cid = c["chunk_id"]
                slot = merged.setdefault(
                    cid,
                    {
                        "chunk_id": cid,
                        "doc_id": c.get("doc_id"),
                        "doc_title": c.get("doc_title"),
                        "doc_version": c.get("doc_version"),
                        "chunk_type": c.get("chunk_type"),
                        "parent_id": c.get("parent_id"),
                        "text": c.get("text"),
                        "token_count": c.get("token_count"),
                        "metadata": c.get("metadata") or {},
                        "rrf_score": 0.0,
                        "scores": {},
                        "ranks": {},
                    },
                )
                slot["rrf_score"] += 1.0 / (self.rrf_k + rank)
                slot["scores"][channel] = float(c.get("score") or 0.0)
                slot["ranks"][channel] = rank
                if c.get("text") and not slot.get("text"):
                    slot["text"] = c["text"]

        _absorb(bm25_pool, "bm25")
        _absorb(vec_pool, "vector")

        out = list(merged.values())
        for item in out:
            meta = item.get("metadata") or {}
            item["source"] = meta.get("source")
            item["section"] = meta.get("section")
            item["page"] = meta.get("page")
            item["title_path"] = meta.get("title_path")
            item["content_hash"] = _content_hash(item.get("text") or "")
        out.sort(key=lambda x: -x["rrf_score"])
        return out

    def _dedup(
        self, fused: Sequence[dict[str, Any]], top_n: int
    ) -> list[dict[str, Any]]:
        """第 5 步：按 chunk_id + 内容哈希去重，并截断到 top-N。

        对应 docs/01 §3.1 第 5 步：「召回 top-50，去重（按 chunk_id + 内容哈希）」。

        Args:
            fused: 融合结果。
            top_n: 候选规模（50）。

        Returns:
            去重后的候选列表。
        """
        seen_ids: set[str] = set()
        seen_hash: set[str] = set()
        out: list[dict[str, Any]] = []
        for c in fused:
            cid = c["chunk_id"]
            h = c.get("content_hash") or _content_hash(c.get("text") or "")
            if cid in seen_ids or h in seen_hash:
                continue
            seen_ids.add(cid)
            seen_hash.add(h)
            out.append(c)
            if len(out) >= top_n:
                break
        return out

    def _assemble_context(
        self, ranked: Sequence[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """第 7 步：上下文组装（检索预算 ≤ 3K token）。

        Args:
            ranked: 重排后的候选。

        Returns:
            命中列表（已裁剪到预算内）。
        """
        hits: list[dict[str, Any]] = []
        used = 0
        for c in ranked:
            tok = estimate_tokens(c.get("text") or "")
            if used + tok > self.retrieval_budget:
                logger.info("检索预算 %d token 用满，停止追加。", self.retrieval_budget)
                break
            used += tok
            hits.append(
                {
                    "chunk_id": c["chunk_id"],
                    "doc_id": c.get("doc_id"),
                    "doc_title": c.get("doc_title"),
                    "doc_version": c.get("doc_version"),
                    "text": c.get("text") or "",
                    "token_count": tok,
                    "score": float(c.get("score") or 0.0),
                    "rrf_score": float(c.get("rrf_score") or 0.0),
                    "rerank_score": float(c.get("rerank_score") or 0.0),
                    "source": c.get("source"),
                    "section": c.get("section"),
                    "page": c.get("page"),
                    "parent_id": c.get("parent_id"),
                    "metadata": c.get("metadata") or {},
                    "ranks": c.get("ranks") or {},
                }
            )
        return hits

    # ==================================================================
    # 父块回填（docs/01 §3.1 关键取舍说明）
    # ==================================================================
    def expand_with_parents(
        self, hits: Sequence[dict[str, Any]], max_parents: int = 3
    ) -> list[dict[str, Any]]:
        """命中子块后回填父块（章节级 ~1200 token），提供完整上下文。

        对应 docs/01 §3.1：「命中 300 token 的子块，回填 1200 token 的父块」。

        Args:
            hits: 命中列表。
            max_parents: 最多回填多少个父块。

        Returns:
            父块列表。
        """
        parents: list[dict[str, Any]] = []
        seen: set[str] = set()
        for h in hits:
            pid = h.get("parent_id")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            try:
                p = self.store.get_parent(pid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("父块回填失败（%s）：%s", pid, exc)
                continue
            if p:
                parents.append(p)
            if len(parents) >= max_parents:
                break
        return parents


# ---------------------------------------------------------------------------
def _content_hash(text: str) -> str:
    """正文内容哈希（用于去重）。

    Args:
        text: 原文。

    Returns:
        sha1 摘要。
    """
    norm = re.sub(r"\s+", " ", (text or "").strip())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()
