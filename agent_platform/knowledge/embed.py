"""Embedding 服务封装（含多级降级路径）。

对应设计文档：
    docs/01_架构方案_RAG_Agent.md
        §2.7 知识层 —— 「Embedding 服务」：推荐 BGE-M3（中英混合好），本地部署不出网
        §3.1 RAG 检索增强流程 —— 第 3 步（向量通道召回）
        §4 技术选型 —— Embedding 选型：bge-m3，维度变更需全量重建索引
    docs/02_场景方案_STDF分析与测试用例生成.md
        §2.2 模型选型与理由 —— Embedding 选 bge-m3（dense + sparse 一套模型）

降级链（**严格按顺序尝试，任何一级都不得让模块 import 失败**）：
    1. ``FlagEmbedding.BGEM3FlagModel`` —— 本地权重，bge-m3（1024 维）
    2. 本地 llama-server 的 embedding 端点（``/embedding`` 或 ``/v1/embeddings``）
    3. hash-based 确定性伪向量 —— **仅用于让管线跑通，必须打日志警告**

注意：降级到第 3 级时，检索语义质量不可用，仅用于开发自测与 CI 冒烟。
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
from contextlib import nullcontext
from typing import Any, Sequence

__all__ = ["Embedder", "EMBEDDING_DIM", "EmbeddingBackend"]

logger = logging.getLogger(__name__)

# bge-m3 输出维度（docs/01 §2.7 / schema.sql 的 vector(1024)）
EMBEDDING_DIM = 1024

# 伪向量维数（降级模式使用，与 bge-m3 不同，故单独定义）
FAKE_DIM = 1024


class EmbeddingBackend:
    """后端类型常量。"""

    FLAG = "flagembedding_bge_m3"
    LLAMA_SERVER = "llama_server_embedding"
    HASH_FAKE = "hash_fake_degraded"


def _l2_normalize(vec: list[float]) -> list[float]:
    """L2 归一化（bge-m3 / 余弦检索要求向量已归一化）。

    Args:
        vec: 原始向量。

    Returns:
        归一化后的向量（零向量原样返回）。
    """
    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 0:
        return vec
    return [v / norm for v in vec]


def resolve_flag_devices() -> str | None:
    """解析 FlagEmbedding 应使用的设备，返回 ``None`` 表示交给它自动探测。

    ⚠️ **为什么需要这个（实测于 2026-11-23）**：

    FlagEmbedding 的 ``devices=None`` 默认走
    ``[f"cuda:{i}" for i in range(torch.cuda.device_count())]``
    （``FlagEmbedding/abc/inference/AbsEmbedder.py:122``），即**只要机器上有
    CUDA 就会抢显存**。

    本机实测：API 进程首次调用 ``/kb/search`` 时，bge-m3 + bge-reranker
    在 CUDA 上加载，**显存从 1327 MiB 涨到 6908 MiB（+5.6 GB）**。
    而 gpt-oss-20b 常驻已占 15.4 GB / 16.3 GB 总显存，
    于是总量 22.3 GB > 16.3 GB → OOM → llama-server 抛
    ``CUDA error: an illegal memory access`` → **两个推理服务全部崩溃**
    （实测复现两次）。

    **结论：API 进程里的 BGE 模型必须留在 CPU 上。**
    bge-m3 是纯编码器，CPU 推理完全够用（检索是低频操作），
    而 16 GB 单卡必须留给 LLM。

    行为：
    - 默认（未设置 ``BGE_DEVICES``）→ 返回 ``"cpu"``，**强制 CPU**；
    - ``BGE_DEVICES=cuda:0`` → 显式允许用 GPU（用户自己承担 OOM 风险）；
    - ``BGE_DEVICES=auto`` → 返回 ``None``，交回 FlagEmbedding 自动探测。

    Returns:
        ``"cpu"`` / ``"cuda:0"`` / ``None``（auto）。
    """
    raw = os.environ.get("BGE_DEVICES", "cpu").strip().lower()
    if not raw or raw == "auto":
        return None
    if raw == "cpu":
        return "cpu"
    return raw  # 例如 "cuda:0" / "cuda:0,cuda:1"


class Embedder:
    """统一 Embedding 入口。

    上层只依赖本接口，不关心底层是 bge-m3、llama-server 还是降级伪向量。

    Args:
        backend: 强制指定后端（``flagembedding`` / ``llama`` / ``hash``），
            为 None 时自动探测。
        model_path: bge-m3 本地权重目录（可选）。
        base_url: llama-server 基地址，默认 ``http://127.0.0.1:8080``。
        dim: 向量维度，默认 1024（bge-m3）。
        batch_size: 批大小。
        timeout: HTTP 超时秒数。
        llama_model: llama-server 上的模型 alias（用于 embedding 端点）。
        allow_fake: 是否允许降级到 hash 伪向量（生产应设为 False）。
    """

    def __init__(
        self,
        backend: str | None = None,
        model_path: str | None = None,
        base_url: str | None = None,
        dim: int = EMBEDDING_DIM,
        batch_size: int = 16,
        timeout: float = 30.0,
        llama_model: str | None = None,
        allow_fake: bool = True,
    ) -> None:
        self.dim = int(dim)
        self.batch_size = int(batch_size)
        self.timeout = float(timeout)
        self.allow_fake = bool(allow_fake)
        self.base_url = (base_url or os.environ.get("LLAMA_BASE_URL")
                         or "http://127.0.0.1:8080").rstrip("/")
        self.llama_model = llama_model or os.environ.get("LLAMA_EMBED_MODEL")
        self.model_path = model_path or os.environ.get("BGE_M3_PATH")

        self.backend: str = ""
        self._model: Any = None
        self._degraded = False

        self._init(backend)
    # ------------------------------------------------------------------
    # 初始化 / 后端探测
    # ------------------------------------------------------------------
    def _init(self, backend: str | None) -> None:
        """按优先级初始化后端。

        Args:
            backend: 强制后端名；None 表示自动探测。
        """
        order = [backend] if backend else ["flagembedding", "llama", "hash"]
        for name in order:
            try:
                if name == "flagembedding":
                    if self._init_flag():
                        self.backend = EmbeddingBackend.FLAG
                        return
                elif name == "llama":
                    if self._init_llama():
                        self.backend = EmbeddingBackend.LLAMA_SERVER
                        return
                elif name == "hash":
                    if self._init_hash():
                        self.backend = EmbeddingBackend.HASH_FAKE
                        return
            except Exception as exc:  # noqa: BLE001 - 降级必须吞掉一切
                logger.warning("Embedding 后端 %s 初始化失败：%s", name, exc)

        # 理论不可达（hash 后端永不失败）
        self._init_hash()
        self.backend = EmbeddingBackend.HASH_FAKE

    def _init_flag(self) -> bool:
        """尝试加载 FlagEmbedding 的 bge-m3。

        注意：``FlagEmbedding`` 包本身可安装，但 **bge-m3 权重需从 HuggingFace
        下载**。权重不在本地时会抛异常，必须优雅降级（不能拖垮整个模块 import）。

        Returns:
            True 表示加载成功。
        """
        try:
            from FlagEmbedding import BGEM3FlagModel  # type: ignore
        except Exception as exc:  # noqa: BLE001
            logger.info("FlagEmbedding 不可用（%s），尝试下一级降级路径。", exc)
            return False
        # ★ 限制 intra-op 线程数：bge-m3 常驻 CPU，其 OpenMP 线程池
        #   在空闲时会持续空转烧满 CPU（实测空闲即占 32% 的 16 逻辑核）。
        #   详见 knowledge/threading_limits.py 的说明。
        try:
            from .threading_limits import limit_torch_threads  # noqa: PLC0415

            limit_torch_threads()
        except Exception as exc:  # noqa: BLE001
            logger.debug("限制 torch 线程数失败（忽略）：%s", exc)
        # 权重路径：显式参数 > 环境变量 > HF 缓存默认位置
        candidates = [
            self.model_path,
            os.environ.get("BGE_M3_PATH"),
            os.path.join(
                os.path.expanduser("~"),
                ".cache", "huggingface", "hub",
                "models--BAAI--bge-m3", "snapshots",
            ),
        ]
        chosen = next(
            (p for p in candidates if p and os.path.isdir(p) and not p.endswith("snapshots")),
            None,
        )
        if chosen is None:
            logger.info(
                "未找到本地 bge-m3 权重目录（可用 BGE_M3_PATH 环境变量指定），"
                "降级到下一级 Embedding 路径。"
            )
            return False
        try:
            self._model = BGEM3FlagModel(
                model_name_or_path=chosen,
                use_fp16=False,
                devices=resolve_flag_devices(),
            )
            logger.info("Embedding 后端：FlagEmbedding bge-m3（dim=%d）", self.dim)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("bge-m3 权重加载失败（%s），降级。", exc)
            self._model = None
            return False

    def _init_llama(self) -> bool:
        """尝试使用本地 llama-server 的 embedding 端点。

        注意：探测必须是**真实的 POST 请求**。仅凭 GET 返回 404 无法区分
        「端点不存在」与「端点存在但方法不对」；而且很多纯文本 LLM 服务
        （如 gpt-oss-20b）**根本不支持 embedding**，此时必须继续降级。

        Returns:
            True 表示端点可用。
        """
        try:
            import requests  # type: ignore
        except Exception:
            return False

        candidates = [
            (f"{self.base_url}/embedding", "llama"),
            (f"{self.base_url}/v1/embeddings", "openai"),
            (f"{self.base_url}/embeddings", "openai"),
        ]
        for url, style in candidates:
            payload: dict[str, Any] = {"input": ["ping"]}
            if self.llama_model:
                payload["model"] = self.llama_model
            if style == "llama":
                payload = {"content": "ping"}
                if self.llama_model:
                    payload["model"] = self.llama_model
            try:
                resp = requests.post(url, json=payload, timeout=min(10.0, self.timeout))
            except Exception:
                continue
            if resp.status_code >= 400:
                logger.info(
                    "llama-server embedding 端点 %s 不可用（HTTP %s），继续降级。",
                    url, resp.status_code,
                )
                continue
            try:
                data = resp.json()
            except Exception:
                continue
            if not (data.get("data") or data.get("embeddings")):
                continue
            self._llama_url = url
            self._llama_style = style
            self._requests = requests
            logger.info("Embedding 后端：llama-server %s", url)
            return True
        logger.info("llama-server 未提供可用的 embedding 端点，继续降级。")
        return False

    def _init_hash(self) -> bool:
        """初始化 hash 伪向量后端（降级模式）。

        Returns:
            恒为 True。
        """
        self._degraded = True
        logger.warning(
            "⚠️ 警告：Embedding 已降级为 **hash 确定性伪向量**。"
            "语义检索质量不可用，仅用于让管线跑通（开发自测 / CI 冒烟）。"
            "生产环境请部署 FlagEmbedding bge-m3 或启用 llama-server embedding 端点。"
        )
        return True

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    @property
    def is_degraded(self) -> bool:
        """当前是否为降级模式（hash 伪向量）。"""
        return self._degraded

    def embed(self, texts: Sequence[str]) -> "Any":
        """批量向量化。

        Args:
            texts: 文本列表。

        Returns:
            ``numpy.ndarray``，形状 ``(len(texts), dim)``。若 numpy 不可用则返回
            ``list[list[float]]``（调用方用 :func:`as_matrix` 统一处理）。
        """
        items = [t if isinstance(t, str) else str(t) for t in (texts or [])]
        if not items:
            return _empty_matrix(0, self.dim)

        if self.backend == EmbeddingBackend.FLAG:
            vecs = self._embed_flag(items)
        elif self.backend == EmbeddingBackend.LLAMA_SERVER:
            vecs = self._embed_llama(items)
        else:
            vecs = [self._fake_vector(t) for t in items]
        return as_matrix(vecs, self.dim)

    def embed_query(self, q: str) -> "Any":
        """单条查询向量化。

        Args:
            q: 查询文本。

        Returns:
            ``numpy.ndarray``，形状 ``(dim,)``。
        """
        m = self.embed([q])
        if hasattr(m, "shape"):
            return m[0]
        return m[0] if m else []

    # ------------------------------------------------------------------
    # 各后端实现
    # ------------------------------------------------------------------
    def _embed_flag(self, items: list[str]) -> list[list[float]]:
        """调用 bge-m3 编码。

        ★ 2026-11-23 性能修复：**推理期临时把 torch 线程数拉到 8**
          （同 reranker 的理由：空闲期压线程是为了防 OpenMP 空转，
          推理期则需要并行度）。实测 embed_query 在 4 线程下 1.2~3.5s，
          8 线程可再快一截。

        Args:
            items: 文本列表。

        Returns:
            向量列表。
        """
        try:
            from .threading_limits import inference_thread_scope  # noqa: PLC0415

            scope = inference_thread_scope()
        except Exception:  # noqa: BLE001 - 没有该工具就按原样跑
            scope = nullcontext()
        with scope:
            out = self._model.encode(
                items,
                batch_size=self.batch_size,
                max_length=1024,
                return_dense=True,
                return_sparse=False,
                return_colbert_vecs=False,
            )
        dense = out["dense_vecs"]
        return [_l2_normalize([float(x) for x in v]) for v in dense]

    def _embed_llama(self, items: list[str]) -> list[list[float]]:
        """调用 llama-server embedding 端点。

        Args:
            items: 文本列表。

        Returns:
            向量列表。

        Raises:
            RuntimeError: 端点调用失败时抛出，由上层降级处理。
        """
        url = getattr(self, "_llama_url", f"{self.base_url}/v1/embeddings")
        style = getattr(self, "_llama_style", "openai")
        payload: dict[str, Any] = {"input": items}
        if style == "llama":
            payload = {"content": items}
        if self.llama_model:
            payload["model"] = self.llama_model
        resp = self._requests.post(url, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        vecs = data.get("data") or data.get("embeddings") or []
        result: list[list[float]] = []
        for item in vecs:
            if isinstance(item, dict):
                vec = item.get("embedding") or item.get("values") or []
            else:
                vec = item
            result.append(_l2_normalize([float(x) for x in vec]))
        if len(result) != len(items):
            raise RuntimeError(
                f"llama-server embedding 返回数量不匹配：{len(result)} != {len(items)}"
            )
        return result

    def _fake_vector(self, text: str) -> list[float]:
        """生成 hash 确定性伪向量（**降级模式，仅用于跑通管线**）。

        原理：对文本做 n-gram 哈希散列到固定维度，token 频次累加后 L2 归一化。
        好处是：同一文本永远得到同一向量（可复现），且相同词重叠会带来一定
        的相似度信号 —— 但这不是语义相似度，不能用于生产检索质量评估。

        Args:
            text: 输入文本。

        Returns:
            归一化后的稠密向量。
        """
        vec = [0.0] * FAKE_DIM
        norm_text = (text or "").lower()
        tokens: list[str] = []
        # 中文按 2-gram，英文按词
        cjk_runs = [s for s in norm_text.split() if s]
        raw = norm_text.replace("\n", " ")
        tokens.extend(cjk_runs)
        for i in range(len(raw) - 1):
            ch = raw[i : i + 2]
            if ch.strip():
                tokens.append(ch)
        for tok in tokens:
            h = hashlib.md5(tok.encode("utf-8")).digest()
            idx = int.from_bytes(h[:4], "big") % FAKE_DIM
            sign = 1.0 if h[4] % 2 == 0 else -1.0
            vec[idx] += sign
        if not any(vec):
            vec[0] = 1.0
        return _l2_normalize(vec)


# ---------------------------------------------------------------------------
# numpy 兼容工具
# ---------------------------------------------------------------------------
def as_matrix(vecs: list[list[float]], dim: int) -> "Any":
    """把向量列表转成 numpy 矩阵（numpy 不可用时退回 list）。

    Args:
        vecs: 向量列表。
        dim: 维度。

    Returns:
        ``numpy.ndarray`` 或 ``list[list[float]]``。
    """
    try:
        import numpy as np  # type: ignore

        arr = np.zeros((len(vecs), dim), dtype=np.float32)
        for i, v in enumerate(vecs):
            n = min(dim, len(v))
            arr[i, :n] = np.asarray(v[:n], dtype=np.float32)
        return arr
    except Exception:  # noqa: BLE001
        return [list(v[:dim]) + [0.0] * max(0, dim - len(v)) for v in vecs]


def _empty_matrix(n: int, dim: int) -> "Any":
    """构造空矩阵。

    Args:
        n: 行数。
        dim: 列数。

    Returns:
        空矩阵。
    """
    try:
        import numpy as np  # type: ignore

        return np.zeros((n, dim), dtype=np.float32)
    except Exception:  # noqa: BLE001
        return []
