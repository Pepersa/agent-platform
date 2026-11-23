"""Reranker 封装（交叉编码精排）。

对应设计文档：
    docs/01_架构方案_RAG_Agent.md
        §2.7 知识层 —— 「Reranker」：推荐 BGE-Reranker-v2-m3（中文+英文+代码混合都好、可私有化）
        §3.1 RAG 检索增强流程 —— 第 6 步：Reranker 精排取 top-6；片段超长按 token 截断
        §3.1 关键取舍说明 —— 重排比盲目加长上下文更划算
    docs/02_场景方案_STDF分析与测试用例生成.md
        §2.2 模型选型与理由 —— Reranker 选 bge-reranker-v2-m3（中文效果好、CPU 可跑）

降级链：
    1. ``FlagEmbedding.FlagReranker``（bge-reranker-v2-m3）
    2. embedding 余弦相似度排序（**降级，必须打日志警告**）

★★ 2026-11-23 性能修复 ★★
    1. **``max_length`` 从 1024 降到 512**：
       实测本库 chunk 的 token 长度 p50=24 / p90=211 / max=1459，
       512 token 足以覆盖绝大多数 chunk，
       交叉编码器耗时由序列长度主导，截断上限降低直接省算力。
       ★ 质量验证：在**真实检索候选**（RRF 序，6 个查询 × 50 候选）上，
         ml=512 与 ml=1024 的 top-6 **完全一致（交集 36/36）**。

    2. **推理期临时提高 torch 线程数**（见 ``knowledge/threading_limits.py``）：
       空闲期把 intra-op 线程压到 4 是为了防 OpenMP 空转；
       但**推理时**恰恰需要并行度 —— 4 线程比 8 线程慢近 **2 倍**。
       真正的提速来源在这里（约 2×），且**零质量影响**。

    3. **shortlist 短名单精排**：默认**关闭**（见下方说明），
       保留为可选开关 ``BGE_RERANK_SHORTLIST``。
"""

from __future__ import annotations

import logging
import os
from contextlib import nullcontext
from typing import Any, Sequence

from agent_platform.knowledge.embed import resolve_flag_devices

__all__ = ["Reranker", "RerankerBackend"]

logger = logging.getLogger(__name__)


class RerankerBackend:
    """后端类型常量。"""

    FLAG = "flagembedding_bge_reranker"
    COSINE = "embedding_cosine_degraded"


class Reranker:
    """统一重排入口。

    ★★ 2026-11-23 性能修复 ★★

    1. **``max_length`` 512**（原 1024）：耗时由序列长度主导，截断直接省算力；
       实测 top-6 与 1024 **完全一致（36/36）**。

    2. **推理期临时 8 线程**（``knowledge/threading_limits.py``）：
       空闲期 4 线程防空转，推理期 8 线程要吞吐 —— 实测快约 **2 倍**。

    3. **shortlist 短名单精排**：**默认关闭**。
       原始设想是「召回宽、精排窄」：交叉编码器逐条前向，耗时与候选数成正比，
       只精排 RRF 融合序的前 N 条、其余按 RRF 原序接在后面。
       ★ 但**实测证明这是质量倒退**，因此默认关闭：

           配置                       平均精排耗时   top-6 交集（对基线）
           ml=1024, n=50（基线）      29.20s        36/36
           ml=1024, n=20（shortlist） 22.35s        **23/36（64%）**
           ml=256,  n=50              22.59s        34/36（94%）
           ml=512,  n=50              28.67s        **36/36（100%）**

       即：截断候选只换来 23% 的时间，却丢了 36% 的 top-6 结果
       （"VDD ripple 纹波 规格" 只有 2/6 重合）。
       ★ 提速应当来自「**降 max_length**」与「**提线程数**」，
         而不是「**砍候选**」—— 后者直接损失召回质量。
       保留 ``shortlist`` 参数与 ``BGE_RERANK_SHORTLIST`` 环境变量，
       供确实需要极限延迟的场景显式开启。

    Args:
        backend: 强制后端（``flag`` / ``cosine``），None 自动探测。
        model_path: bge-reranker-v2-m3 本地权重目录（可选）。
        top_k: 默认返回条数（docs/01 §3.1 第 6 步取 top-6）。
        embedder: 降级路径使用的 embedding 实例（可注入，便于复用同一模型）。
        max_length: 交叉编码器最大输入长度。
        batch_size: 批大小。
        shortlist: 实际送进交叉编码器的候选上限（**默认 0 = 不限制**）。
            候选超过该数时，只精排前 ``shortlist`` 条（按传入顺序，
            即 RRF 融合序），其余保持原序接在后面。
            ★ 默认关闭：实测截断候选会损失 top-6 质量
              （交集仅 23/36），提速请用 ``max_length`` 与线程数。
    """

    #: 默认短名单大小：20 条（仅当用户**显式**开启 shortlist 时才使用）。
    #: ★ 历史：曾默认开启，但实测是质量倒退（见类 docstring 的对比表），
    #:   现改为「默认关闭、显式开启」。
    DEFAULT_SHORTLIST = 20

    #: 默认的 shortlist 取值：**0 = 不截断**（全部候选都精排）。
    DEFAULT_SHORTLIST_LIMIT = 0

    def __init__(
        self,
        backend: str | None = None,
        model_path: str | None = None,
        top_k: int = 6,
        embedder: Any = None,
        max_length: int = 512,
        batch_size: int = 16,
        shortlist: int | None = None,
    ) -> None:
        self.model_path = model_path or os.environ.get("BGE_RERANKER_PATH")
        self.top_k = int(top_k)
        self.embedder = embedder
        self.max_length = int(max_length)
        self.batch_size = int(batch_size)
        #: shortlist ≤ 0 表示「不限制，全部精排」（**默认行为**）
        self.shortlist = int(
            shortlist if shortlist is not None
            else os.environ.get(
                "BGE_RERANK_SHORTLIST", self.DEFAULT_SHORTLIST_LIMIT
            )
        )
        self.backend: str = ""
        self._model: Any = None
        self._degraded = False
        self._init(backend)

    # ------------------------------------------------------------------
    def _init(self, backend: str | None) -> None:
        """初始化后端。

        Args:
            backend: 强制后端名。
        """
        order = [backend] if backend else ["flag", "cosine"]
        for name in order:
            try:
                if name == "flag":
                    if self._init_flag():
                        self.backend = RerankerBackend.FLAG
                        return
                elif name == "cosine":
                    if self._init_cosine():
                        self.backend = RerankerBackend.COSINE
                        return
            except Exception as exc:  # noqa: BLE001
                logger.warning("Reranker 后端 %s 初始化失败：%s", name, exc)
        self._init_cosine()
        self.backend = RerankerBackend.COSINE

    def _init_flag(self) -> bool:
        """尝试加载 FlagEmbedding 的 bge-reranker-v2-m3。

        与 Embedding 同理：包可安装但**权重需另行下载**，不在本地则降级。

        Returns:
            True 表示成功。
        """
        try:
            from FlagEmbedding import FlagReranker  # type: ignore
        except Exception as exc:  # noqa: BLE001
            logger.info("FlagEmbedding 不可用（%s），Reranker 降级到余弦排序。", exc)
            return False
        # ★ 限制 intra-op 线程数：reranker 常驻 CPU，其 OpenMP 线程池
        #   空闲时持续空转烧 CPU（详见 knowledge/threading_limits.py）。
        try:
            from .threading_limits import limit_torch_threads  # noqa: PLC0415

            limit_torch_threads()
        except Exception as exc:  # noqa: BLE001
            logger.debug("限制 torch 线程数失败（忽略）：%s", exc)
        candidates = [
            self.model_path,
            os.environ.get("BGE_RERANKER_PATH"),
            os.path.join(
                os.path.expanduser("~"),
                ".cache", "huggingface", "hub",
                "models--BAAI--bge-reranker-v2-m3", "snapshots",
            ),
        ]
        chosen = next(
            (p for p in candidates if p and os.path.isdir(p) and not p.endswith("snapshots")),
            None,
        )
        if chosen is None:
            logger.info(
                "未找到本地 bge-reranker-v2-m3 权重目录"
                "（可用 BGE_RERANKER_PATH 环境变量指定），降级到余弦排序。"
            )
            return False
        try:
            self._model = FlagReranker(
                model_name_or_path=chosen,
                use_fp16=False,
                # ⚠️ 默认强制 CPU：FlagEmbedding 的 devices=None 会自动选 CUDA，
                # 在 16 GB 单卡上会与常驻的 gpt-oss-20b 抢显存导致 OOM，
                # 进而把两个 llama-server 一起拖崩（详见 embed.resolve_flag_devices）。
                devices=resolve_flag_devices(),
            )
            logger.info("Reranker 后端：FlagEmbedding bge-reranker-v2-m3")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("bge-reranker-v2-m3 权重加载失败（%s），降级。", exc)
            self._model = None
            return False

    def _init_cosine(self) -> bool:
        """初始化余弦相似度降级后端。

        Returns:
            True 表示可用（需要 embedder）。
        """
        if self.embedder is None:
            try:
                from agent_platform.knowledge.embed import Embedder

                self.embedder = Embedder()
            except Exception as exc:  # noqa: BLE001
                logger.error("无法构造降级用 Embedder：%s", exc)
                return False
        self._degraded = True
        logger.warning(
            "⚠️ 警告：Reranker 已降级为 **embedding 余弦相似度排序**。"
            "精排质量显著低于交叉编码器（bge-reranker-v2-m3），"
            "评测结果不可用于生产质量结论。"
        )
        return True

    # ------------------------------------------------------------------
    @property
    def is_degraded(self) -> bool:
        """当前是否为降级模式。"""
        return self._degraded

    def rerank(
        self,
        query: str,
        candidates: Sequence[dict[str, Any]],
        top_k: int = 6,
    ) -> list[dict[str, Any]]:
        """对候选块重排。

        对应 docs/01 §3.1 第 6 步：Reranker 精排取 top-6。

        ★★ 2026-11-23 性能修复 ★★
          交叉编码器逐条前向，耗时与候选数成正比。若显式设置了
          ``self.shortlist``（``BGE_RERANK_SHORTLIST`` 环境变量），
          只把**前 shortlist 条**（按传入顺序，即 RRF 融合序）送进模型，
          其余候选**保持原序**接在结果尾部，复用 RRF 的相对分作为兜底排序。

          ★ **默认 ``shortlist=0`（不截断，全部精排）**。
            实测截断会损失 top-6 质量（交集仅 23/36），
            提速应当来自 ``max_length`` 与推理期线程数，详见类 docstring。

        Args:
            query: 查询文本。
            candidates: 候选块列表，每项至少含 ``text``；可选含 ``chunk_id``、
                ``score`` 等字段。函数不会修改入参。
            top_k: 返回条数（默认 6）。

        Returns:
            重排后的候选列表，每项追加 ``rerank_score`` 字段（分数越高越相关）。
        """
        cands = [dict(c) for c in (candidates or [])]
        if not cands:
            return []
        k = int(top_k) if top_k else self.top_k

        # ---- 切分：精排短名单 / 保留尾部 ----
        limit = self.shortlist
        if limit and limit > 0 and len(cands) > limit:
            head, tail = cands[:limit], cands[limit:]
        else:
            head, tail = cands, []

        if self.backend == RerankerBackend.FLAG:
            scores = self._score_flag(query, head)
        else:
            scores = self._score_cosine(query, head)

        for c, s in zip(head, scores):
            c["rerank_score"] = float(s)

        # 稳定排序：分数降序，同分保持原召回顺序
        indexed = list(enumerate(head))
        indexed.sort(key=lambda t: (-t[1]["rerank_score"], t[0]))
        ranked = [c for _, c in indexed]

        # 未精排的尾部：沿用 RRF 分数作为排序依据（分数量纲不同，
        # 因此**排在精排结果之后**，只用于补足 top_k）。
        for c in tail:
            c["rerank_score"] = float(c.get("rrf_score") or 0.0)
        ranked.extend(tail)

        return ranked[:k]

    # ------------------------------------------------------------------
    def _score_flag(self, query: str, cands: list[dict[str, Any]]) -> list[float]:
        """用交叉编码器打分。

        ★ 2026-11-23 性能修复（两处）：
          1. 显式传 ``batch_size``（默认 16，实测已是最优点：
             8→20.1s / 16→15.9s / 32→22.7s / 50→24.3s）。
          2. **推理期临时把 torch 线程数拉到 8**：
             ``knowledge/threading_limits.py`` 平时把 intra-op 线程压到 4，
             目的是让常驻的 OpenMP 线程池**空闲时别空转**；
             但交叉编码器是纯 CPU 矩阵运算，推理时 4 线程比 8 线程慢近 2 倍
             （实测 n=50/max_length=256：4 线程 32.3s → 8 线程 18.8s）。
             这里用 ``inference_thread_scope`` 在**推理窗口内**放宽，
             退出时立刻恢复 → 「空闲不空转」与「推理要快」两个目标同时满足。

        Args:
            query: 查询。
            cands: 候选列表。

        Returns:
            分数列表。
        """
        pairs = [[query, str(c.get("text") or "")] for c in cands]
        try:
            from .threading_limits import inference_thread_scope  # noqa: PLC0415

            scope = inference_thread_scope()
        except Exception:  # noqa: BLE001 - 没有该工具就按原样跑
            scope = nullcontext()
        with scope:
            scores = self._model.compute_score(
                pairs,
                max_length=self.max_length,
                batch_size=self.batch_size,
            )
        return [float(s) for s in scores]

    def _score_cosine(
        self, query: str, cands: list[dict[str, Any]]
    ) -> list[float]:
        """用 embedding 余弦相似度打分（降级路径）。

        Args:
            query: 查询。
            cands: 候选列表。

        Returns:
            分数列表（[-1, 1] 区间的余弦相似度）。
        """
        try:
            import numpy as np  # type: ignore
        except Exception:
            np = None  # type: ignore

        qv = self.embedder.embed_query(query)
        cv = self.embedder.embed([str(c.get("text") or "") for c in cands])
        if np is None:
            # 纯 Python 余弦
            out = []
            for c in cands:
                v = cv[len(out)]
                dot = sum(a * b for a, b in zip(qv, v))
                nq = sum(a * a for a in qv) ** 0.5
                nc = sum(b * b for b in v) ** 0.5
                out.append(dot / (nq * nc) if nq and nc else 0.0)
            return out
        q = np.asarray(qv, dtype=np.float32)
        m = np.asarray(cv, dtype=np.float32)
        sims = m @ q
        qn = float(np.linalg.norm(q)) or 1.0
        mn = np.linalg.norm(m, axis=1)
        mn[mn == 0] = 1.0
        return [float(v) for v in (sims / (qn * mn))]
