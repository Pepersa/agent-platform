"""知识检索工具（Tool Registry 接入层）。

对应设计文档：
    docs/02_场景方案_STDF分析与测试用例生成.md
        §5.2 工具清单 —— ``kb_search`` / ``kb_read``
        §5.1 通用契约 —— JSON Schema 入参/出参 + 权限级别 + HITL 标记 + 超时/重试
    docs/01_架构方案_RAG_Agent.md
        §2.5 工具层 —— Tool Registry 是核心资产
        §3.1 RAG 检索增强流程 —— 第 8 步（要求逐句附引用）

**本文件不修改 ``tools/__init__.py``**，只提供可被 ``core/tool_registry.py``
侧调用的 spec 工厂函数。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

__all__ = [
    "kb_search_spec",
    "kb_read_spec",
    "kb_search",
    "kb_read",
    "get_shared_retriever",
    "reset_shared_retriever",
    "start_background_warmup",
    "warmup_status",
    "is_warm",
]

logger = logging.getLogger(__name__)

# 全局共享的 Retriever（懒构造，避免每个工具调用都重建索引）
_shared: dict[str, Any] = {}
# ★ 必须是 **RLock（可重入）**，不能用普通 ``threading.Lock``（2026-11-23 死锁修复）：
#   本模块存在**嵌套取锁**的调用链：
#     ``get_shared_retriever()`` → ``_get_cached_embedder()`` / ``_get_cached_reranker()``
#     ``_get_cached_reranker()`` → ``_get_cached_embedder()``（embedder 为 None 时）
#     后台预热线程 ``start_background_warmup()`` 也走同一条链
#   普通 ``Lock`` 不可重入 → 同线程二次 acquire 直接**自死锁**（永久挂起），
#   表现为 ``GET /kb/search`` 无限挂死、随后整个 API 事件循环被冻结
#   （``/health`` 也超时），但进程仍存活、端口仍 LISTENING。
#   用 ``RLock`` 后：同一线程可重入获取（计数式释放），跨线程仍互斥，
#   因此「bge-m3 只加载一份」的语义保持不变。
#   （配合下面的**双检锁 + 锁外构造**，锁只会被持有极短时间，
#     不会有人为了等 22s 的权重加载而堵在锁上。）
_lock = threading.RLock()

# ★★ 2026-11-23 性能修复：后台预热（warmup）状态 ★★
#
#   背景（实测）：bge-m3 权重加载 **22.2s**、bge-reranker-v2-m3 **10.4s**，
#   合计约 **33s**。这段开销原本落在**第一个用户请求**上 →
#   冷启动 ``GET /kb/search`` 约 **46s**，直接撞穿工具层 20s 超时，
#   Agent 侧只能看到 ``[TIMEOUT] 工具 'kb_search' 执行超时``。
#
#   方案：API 启动时用**守护线程**把两个重模型提前加载好（与其它启动工作并发），
#   第一个用户请求就能直接命中缓存 → 只剩推理耗时（~3~5s）。
#
#   ★ 关键点：模型构造必须**在锁外**完成（见 :func:`_get_cached_embedder`）。
#     如果 warmup 线程持锁 22s，期间任何并发请求调用 ``get_shared_retriever()``
#     都会在锁上阻塞 22s —— 等于把「首请求慢」换成了「首请求慢 + 别人也慢」。
#     双检锁（double-checked locking）保证：构造期间锁是空闲的，
#     其它线程最多等一个构造周期，且**不会**重复加载（构造完立刻回填缓存）。
_warmup_state: dict[str, Any] = {
    "state": "idle",       # idle | running | ready | failed
    "started_at": 0.0,
    "finished_at": 0.0,
    "error": "",
}
_warmup_thread: Optional[threading.Thread] = None


def warmup_status() -> dict[str, Any]:
    """返回 BGE 预热状态（供 API 层 ``/health`` 等对外暴露）。

    ★ 用途（2026-11-23）：让前端能显示「知识库模型加载中…」，
      而不是让用户以为服务卡死。本机 bge-m3 + reranker 冷加载约 **33s**，
      这段时间内 ``/kb/search`` 会很慢。

    Returns:
        ``{"state": "idle"|"running"|"ready"|"failed", "ready": bool,
        "elapsed_s": float, "error": str, "embedder_cached": bool,
        "reranker_cached": bool}``。
    """
    with _lock:
        state = dict(_warmup_state)
        state["embedder_cached"] = "embedder" in _shared
        state["reranker_cached"] = "reranker" in _shared
    elapsed = 0.0
    if state.get("started_at"):
        end = state.get("finished_at") or time.time()
        elapsed = max(0.0, end - float(state["started_at"]))
    state["elapsed_s"] = round(elapsed, 2)
    state["ready"] = bool(state.get("embedder_cached") and state.get("reranker_cached"))
    return state


def is_warm() -> bool:
    """BGE 重模型是否已就绪（embedder + reranker 均已缓存）。

    Returns:
        True 表示已预热完成。
    """
    return bool(warmup_status().get("ready"))


def start_background_warmup() -> Optional[threading.Thread]:
    """在**守护线程**里预热 BGE 重模型（embedder + reranker）。

    ★ 设计约束（2026-11-23）：
      · **守护线程**（``daemon=True``）—— 绝不阻塞进程退出 / API 关停；
      · **不阻塞调用方** —— 返回后立即返回，API 启动流程继续；
      · **失败不炸** —— 任何异常都吞掉并记录到 :func:`warmup_status`，
        预热失败只是「首请求慢一点」，绝不能把 API 拖垮；
      · 加载的正是**请求路径用的同一份缓存**（``_shared["embedder"]`` /
        ``_shared["reranker"]``），因此预热后请求路径直接命中，不会重复加载。

    Returns:
        启动的线程对象；若已在预热或已就绪则返回 None。
    """
    global _warmup_thread
    with _lock:
        if _warmup_thread is not None and _warmup_thread.is_alive():
            return None
        if "embedder" in _shared and "reranker" in _shared:
            _warmup_state["state"] = "ready"
            return None
        _warmup_state.update(
            {"state": "running", "started_at": time.time(),
             "finished_at": 0.0, "error": ""}
        )

    def _work() -> None:
        t0 = time.perf_counter()
        try:
            # ★ 构造在**锁外**完成（双检锁，见 _get_cached_embedder）。
            #   预热线程绝不长时间持有 _lock，否则并发请求会被拖住。
            emb = _get_cached_embedder()
            rr = _get_cached_reranker(emb)
            with _lock:
                _warmup_state["state"] = "ready"
                _warmup_state["finished_at"] = time.time()
            logger.info(
                "★ BGE 预热完成：embedder=%s reranker=%s 耗时 %.1fs",
                type(emb).__name__, type(rr).__name__, time.perf_counter() - t0,
            )
        except Exception as exc:  # noqa: BLE001 - 预热失败绝不能影响 API
            logger.warning("★ BGE 预热失败（不影响 API 使用）：%s: %s",
                           type(exc).__name__, exc)
            with _lock:
                _warmup_state["state"] = "failed"
                _warmup_state["finished_at"] = time.time()
                _warmup_state["error"] = f"{type(exc).__name__}: {exc}"

    th = threading.Thread(
        target=_work, name="bge-warmup", daemon=True,
    )
    _warmup_thread = th
    th.start()
    logger.info("★ BGE 预热线程已启动（daemon，不阻塞 API 启动）")
    return th


def _tool_error(code: str, message: str, retryable: bool, hint: str) -> Any:
    """构造统一工具错误。

    优先抛出 ``core.tool_registry.ToolError``（docs/01 §3.2 失败归一：
    ``{code, message, retryable, hint}``）；若 core 侧不可用则退回返回同结构 dict，
    保证本模块不被 core 的 import 顺序绑死。

    Args:
        code: 错误码。
        message: 错误信息。
        retryable: 是否可重试。
        hint: 面向模型的修复提示。

    Returns:
        永远不会正常返回（总是抛异常）；异常被上层吞掉时返回错误 dict。
    """
    payload = {"code": code, "message": message, "retryable": retryable, "hint": hint}
    try:
        from agent_platform.core.tool_registry import ToolError  # type: ignore
    except ImportError:
        return payload
    raise ToolError(code, message, retryable=retryable, hint=hint)


def _is_tool_error(exc: BaseException) -> bool:
    """判断异常是否为 core 侧定义的 ToolError。

    Args:
        exc: 异常对象。

    Returns:
        是否为 ToolError。
    """
    return type(exc).__name__ == "ToolError" and hasattr(exc, "to_dict")


def get_shared_retriever(
    store_path: Optional[str] = None,
    model: str = "gpt-oss-20b",
) -> Any:
    """获取（或懒构造）共享的 Retriever。

    ★★ 2026-11-23 性能修复：**双检锁（double-checked locking）** ★★
      昂贵的动作（加载 bge-m3 / bge-reranker 权重，合计约 **33s**）
      **一律在锁外**完成：

        1. 持锁 → 查缓存 → 命中就直接返回（快路径，纳秒级）；
        2. 未命中 → **放掉锁** → 在锁外构造模型（22s / 10s）；
        3. 重新持锁 → 再查一次缓存（防止并发重复构造）→ 未命中才写入。

      为什么必须这样：之前整个构造过程都在锁内 → 后台预热线程会**持锁 22s**，
      期间任何并发请求调用 ``get_shared_retriever()`` 都卡在锁上 22s，
      表现就是「预热反而让所有人一起变慢」。放到锁外后，
      并发请求最多等一个构造周期（不会重复加载），但**锁本身不阻塞任何人**。

    Args:
        store_path: SQLite 索引路径；None 时取统一数据目录下的
            ``kb/chunks.db``（可通过 ``AGENT_DATA_DIR`` 整体外置）。
        model: 生成用模型 alias。

    Returns:
        :class:`agent_platform.knowledge.retriever.Retriever` 实例。
    """
    # ---- 快路径：命中缓存（锁内极短临界区）----
    with _lock:
        cached = _shared.get("retriever")
    if cached is not None:
        return cached

    # ---- 慢路径：构造全部在锁外 ----
    from agent_platform.knowledge.llm import LLMClient
    from agent_platform.knowledge.retriever import Retriever
    from agent_platform.knowledge.store import build_store

    store = build_store("sqlite", sqlite_path=store_path)
    # ★ 重模型（Embedder / Reranker）走**独立缓存**，
    #   重建索引时只需换 SQLite 连接，不必重新加载 bge-m3 权重。
    #   原因（2026-11-23 修复）：
    #     之前 ``reset_shared_retriever()`` 会把整个 Retriever 丢掉，
    #     下一次 kb_search 就要重新 ``Embedder()`` → 重新加载 bge-m3
    #     （本机走 CPU，实测 **30~50 秒**）→ 工具层 20s 超时直接报
    #     ``[TIMEOUT] 工具 'kb_search' 执行超时``。
    #   实测：缓存后重建后的首次检索 **< 1s**。
    embedder = _get_cached_embedder()
    reranker = _get_cached_reranker(embedder)
    llm = LLMClient(model=model)
    retriever = Retriever(
        store=store, embedder=embedder, reranker=reranker, llm=llm
    )

    # ---- 回填（双检：并发时只有一个线程写入成功）----
    with _lock:
        existing = _shared.get("retriever")
        if existing is not None:
            # 另一个线程已经建好了 → 用它，丢弃自己这份（避免重复的 SQLite 连接）
            return existing
        _shared["retriever"] = retriever
        return retriever


def _get_cached_embedder() -> Any:
    """取（并缓存）全局 Embedder 实例。

    ★ bge-m3 权重加载很贵（本机 CPU 推理时 **30~50 秒**），
    绝不能因为「重建索引换了个 SQLite 连接」就重新加载一次。

    ★★ 2026-11-23 性能修复：**构造在锁外**（双检锁）★★
      之前整个 ``Embedder()`` 构造都在 ``_lock`` 内 → 后台预热线程会持锁 22s，
      期间所有并发请求全部堵在锁上。现在：

        1. 持锁查缓存 → 命中直接返回；
        2. 未命中 → **放锁** → 锁外构造（22s，此时锁是空闲的）；
        3. 重新持锁 → 再查一次（并发时只有一个真正构造）→ 写入缓存。

      ⚠️ 副作用：两个线程同时首次访问时，可能**各自构造一个** Embedder
        （白费一次 22s 的 CPU 与内存），但最终缓存里只有一份，
        语义「bge-m3 只留一份」依然成立 —— 这比「所有请求堵 22s」划算得多。
        预热线程的存在也让这个竞态在实践中几乎不会发生。

    ★★ 锁的可重入性（2026-11-23 死锁修复）★★
      ``_lock`` **必须**是 :class:`threading.RLock`（可重入），**不能**用普通
      ``threading.Lock``：本函数会被**已持有 ``_lock``** 的调用方触发
      （例如 :func:`_get_cached_reranker` 在构造 Reranker 时回调本函数、
      以及 ``get_shared_retriever()`` 的嵌套取缓存路径）。
      普通 ``Lock`` 不可重入 → 同线程二次 acquire 直接**自死锁**（永久挂起），
      表现为 ``GET /kb/search`` 无限挂死、随后整个 API 事件循环被冻结
      （``/health`` 也超时），但进程仍存活、端口仍 LISTENING。
    """
    with _lock:
        cached = _shared.get("embedder")
    if cached is not None:
        return cached

    from agent_platform.knowledge.embed import Embedder

    # ★ 昂贵构造：**锁外**
    embedder = Embedder()

    with _lock:
        cached = _shared.get("embedder")
        if cached is not None:
            return cached
        _shared["embedder"] = embedder
        return embedder


def _get_cached_reranker(embedder: Any = None) -> Any:
    """取（并缓存）全局 Reranker 实例（复用同一个 Embedder）。

    ★ 同 :func:`_get_cached_embedder`：**构造在锁外**（双检锁），
      避免 bge-reranker-v2-m3 的 **10.4s** 权重加载把锁按住。

    ★★ 本函数是**可重入调用** ★★
      当 ``embedder`` 为 None 时它会回调 :func:`_get_cached_embedder`，
      而那条路径可能是在**已持有 ``_lock``** 的上下文中被触发的
      → ``_lock`` 必须是 :class:`threading.RLock`（普通 ``Lock`` 会自死锁）。
    """
    with _lock:
        cached = _shared.get("reranker")
    if cached is not None:
        return cached

    from agent_platform.knowledge.reranker import Reranker

    # ★ 昂贵构造：**锁外**
    base = embedder
    if base is None:
        base = _get_cached_embedder()
    reranker = Reranker(embedder=base)

    with _lock:
        cached = _shared.get("reranker")
        if cached is not None:
            return cached
        _shared["reranker"] = reranker
        return reranker


def reset_shared_retriever() -> None:
    """重置共享 Retriever（重建索引后调用）。

    ★ **只丢 Retriever（换 SQLite 连接），保留重模型缓存**
      （2026-11-23 修复）：

      之前这里把整个 ``_shared["retriever"]`` 丢掉，导致下一次
      ``kb_search`` 必须重新加载 bge-m3 权重（本机 CPU 推理实测 30~50 秒），
      直接撞上工具层 20s 超时，报 ``[TIMEOUT] 工具 'kb_search' 执行超时``。

      现在 ``embedder`` / ``reranker`` 缓存在 ``_shared`` 里单独保留，
      重建索引后首次检索 **< 1s**。

    ★★ 2026-11-23 性能修复：**语义不变** ★★
      仍然**只**丢弃 SQLite Retriever 句柄（``_shared["retriever"]``），
      **保留** ``_shared["embedder"]`` / ``_shared["reranker"]`` 两个重模型缓存。
      预热线程（:func:`start_background_warmup`）加载的也正是这两个缓存项，
      因此重建索引**不会**让预热白费。
    """
    with _lock:
        _shared.pop("retriever", None)


# ---------------------------------------------------------------------------
# kb_search
# ---------------------------------------------------------------------------
def kb_search_spec() -> dict[str, Any]:
    """构造 ``kb_search`` 工具规格。

    对应 docs/02 §5.2：「kb_search —— 混合检索知识库」，入参
    ``{query, top_k, filters}``，返回
    ``{chunks:[{chunk_id, text, score, source:{doc,page,section}, rerank_score}]}``。

    Returns:
        ToolSpec 字典。
    """
    return {
        "name": "kb_search",
        "description": (
            "在内部知识库中做混合检索（BM25 + 向量 → RRF 融合 → Reranker 精排）。"
            "用于回答 TP 清单、规格书限值、SOP 步骤、故障知识库（FMEA）等知识型问题。"
            "返回的 chunk 必须作为答案引用来源。"
        ),
        "input_schema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "检索查询文本"},
                "top_k": {
                    "type": "integer",
                    "default": 6,
                    "description": "返回条数，默认 6（docs/01 §3.1 第 6 步）",
                },
                "filters": {
                    "type": "object",
                    "description": "元数据过滤（检索前下推到存储层）",
                    "properties": {
                        "product_line": {"type": "string"},
                        "tp_name": {"type": "string"},
                        "tp_version": {"type": "string"},
                        "doc_type": {"type": "string"},
                        "time_range": {"type": "string"},
                    },
                },
                "user_id": {"type": "string", "description": "调用者用户 ID（用于权限过滤）"},
                "roles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "调用者角色列表（ACL 可见角色过滤）",
                },
                "max_security_level": {
                    "type": "string",
                    "default": "internal",
                    "description": "可见最高密级：public/internal/confidential/secret",
                },
            },
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "chunks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "chunk_id": {"type": "string"},
                            "doc_id": {"type": "string"},
                            "text": {"type": "string"},
                            "score": {"type": "number"},
                            "rrf_score": {"type": "number"},
                            "rerank_score": {"type": "number"},
                            "source": {
                                "type": "object",
                                "properties": {
                                    "doc": {"type": "string"},
                                    "page": {"type": "integer"},
                                    "section": {"type": "string"},
                                    "version": {"type": "string"},
                                },
                            },
                        },
                    },
                },
                "refused": {"type": "boolean"},
                "reason": {"type": "string"},
            },
        },
        "permission": "READ_ONLY",
        "hitl_required": False,
        # ★★ 2026-11-23 性能修复：超时预算从 20s 提到 **60s** ★★
        #   实测（CPU 推理，BGE 强制不上 GPU）：
        #     · 冷启动（含权重加载）≈ 46s —— 20s 必然超时；
        #     · 预热后暖路径 ≈ 3~5s（重排只跑 top-20 候选后）。
        #   20s 的老预算在「预热尚未完成」时会直接报
        #   ``[TIMEOUT] 工具 'kb_search' 执行超时``，
        #   而此时模型其实正在正常加载 —— 属于**误杀**。
        #   60s 覆盖「最坏情况下的冷启动」（46s），给足余量。
        #   ★ 工具层没有全局超时上限（core/tool_registry.py 的
        #     ``eff_timeout = timeout_ms or spec.timeout_ms``），
        #     因此这里的值就是实际上限，不会被别处截断。
        "timeout_ms": 60000,
        "retry": {"max": 2, "backoff_ms": 500},
        "audit": True,
        "handler": _kb_search_handler,
    }


def _kb_search_handler(**kwargs: Any) -> dict[str, Any]:
    """``kb_search`` 处理函数。

    Args:
        **kwargs: 工具入参。

    Returns:
        工具输出 dict；出错时返回统一错误结构
        ``{code, message, retryable, hint}``（docs/01 §3.2 失败归一）。
    """
    try:
        query = str(kwargs.get("query") or "").strip()
        if not query:
            return _tool_error(
                "INVALID_ARGUMENTS",
                "query 不能为空",
                False,
                "请提供非空的 query 字符串，例如：'T101_VDD_FUNCT 的 VDD 上下限是多少'",
            )
        top_k = int(kwargs.get("top_k") or 6)
        filters = kwargs.get("filters") or {}
        from agent_platform.knowledge.retriever import UserContext

        ctx = UserContext(
            user_id=str(kwargs.get("user_id") or "anonymous"),
            roles=list(kwargs.get("roles") or []),
            max_security_level=str(kwargs.get("max_security_level") or "internal"),
            product_line=filters.get("product_line"),
        )
        retriever = get_shared_retriever()
        res = retriever.retrieve_with_trace(query, ctx, top_k=top_k)
        # 记录「该文档被检索到多少次」（真实发生才记，不臆造）
        try:
            retriever.store.record_hits(
                {h.get("doc_id") for h in res.hits if h.get("doc_id")}
            )
        except Exception:  # noqa: BLE001
            pass
        chunks = [
            {
                "chunk_id": h["chunk_id"],
                "doc_id": h.get("doc_id"),
                "text": h.get("text"),
                "score": h.get("rerank_score") or h.get("rrf_score"),
                "rrf_score": h.get("rrf_score"),
                "rerank_score": h.get("rerank_score"),
                "source": {
                    "doc": h.get("source"),
                    "page": h.get("page"),
                    "section": h.get("section"),
                    "version": h.get("doc_version"),
                },
            }
            for h in res.hits
        ]
        return {
            "chunks": chunks,
            "refused": bool(res.reason),
            "reason": res.reason,
        }
    except Exception as exc:  # noqa: BLE001
        if _is_tool_error(exc):
            raise
        logger.exception("kb_search 执行失败")
        return _tool_error(
            "TOOL_EXECUTION_ERROR",
            f"kb_search 执行失败：{type(exc).__name__}: {exc}",
            True,
            "检索服务异常，可重试；持续失败请检查知识库索引是否已构建"
            "（python scripts/build_index.py --docs-dir data/docs --store sqlite）。",
        )


# ---------------------------------------------------------------------------
# kb_read
# ---------------------------------------------------------------------------
def kb_read_spec() -> dict[str, Any]:
    """构造 ``kb_read`` 工具规格。

    对应 docs/02 §5.2：「kb_read —— 读全文片段」，入参 ``{chunk_id}``，
    返回 ``{text, source, neighbors[]}``。

    Returns:
        ToolSpec 字典。
    """
    return {
        "name": "kb_read",
        "description": (
            "按 chunk_id 读取知识库中的完整片段与其父块上下文。"
            "当检索结果被截断、需要该测试项的完整限值表或完整步骤时使用。"
        ),
        "input_schema": {
            "type": "object",
            "required": ["chunk_id"],
            "properties": {
                "chunk_id": {"type": "string", "description": "稳定 chunk 标识"},
                "include_parent": {
                    "type": "boolean",
                    "default": True,
                    "description": "是否回填父块（章节级 ~1200 token）",
                },
            },
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "string"},
                "text": {"type": "string"},
                "source": {
                    "type": "object",
                    "properties": {
                        "doc": {"type": "string"},
                        "page": {"type": "integer"},
                        "section": {"type": "string"},
                        "version": {"type": "string"},
                    },
                },
                "parent_text": {"type": "string"},
                "neighbors": {"type": "array", "items": {"type": "object"}},
            },
        },
        "permission": "READ_ONLY",
        "hitl_required": False,
        "timeout_ms": 10000,
        "retry": {"max": 2, "backoff_ms": 500},
        "audit": True,
        "handler": _kb_read_handler,
    }


def _kb_read_handler(**kwargs: Any) -> dict[str, Any]:
    """``kb_read`` 处理函数。

    Args:
        **kwargs: 工具入参。

    Returns:
        工具输出 dict；出错时返回统一错误结构。
    """
    try:
        chunk_id = str(kwargs.get("chunk_id") or "").strip()
        if not chunk_id:
            return _tool_error(
                "INVALID_ARGUMENTS",
                "chunk_id 不能为空",
                False,
                "请先调用 kb_search 获取 chunk_id，再调用 kb_read 读取全文",
            )
        retriever = get_shared_retriever()
        chunk = retriever.store.get_by_id(chunk_id)
        if not chunk:
            return _tool_error(
                "NOT_FOUND",
                f"chunk_id 不存在：{chunk_id}",
                False,
                "chunk_id 可能来自旧版本索引，请重新调用 kb_search 获取最新的 chunk_id",
            )
        meta = chunk.get("metadata") or {}
        out: dict[str, Any] = {
            "chunk_id": chunk_id,
            "text": chunk.get("text"),
            "source": {
                "doc": meta.get("source"),
                "page": meta.get("page"),
                "section": meta.get("section"),
                "version": chunk.get("doc_version"),
            },
            "neighbors": [],
        }
        if kwargs.get("include_parent", True) and chunk.get("parent_id"):
            parent = retriever.store.get_parent(chunk["parent_id"])
            if parent:
                out["parent_text"] = parent.get("text")
        return out
    except Exception as exc:  # noqa: BLE001
        if _is_tool_error(exc):
            raise
        logger.exception("kb_read 执行失败")
        return _tool_error(
            "TOOL_EXECUTION_ERROR",
            f"kb_read 执行失败：{type(exc).__name__}: {exc}",
            True,
            "读取失败，可重试",
        )


# ---------------------------------------------------------------------------
# kb_search / kb_read 的 duck typing 入口
# ---------------------------------------------------------------------------
# ``agent_platform.tools`` 通过 duck typing 在 knowledge 层查找同名函数
# （签名见 tools/__init__.py 模块 docstring）。这里提供等价的函数入口，
# 使 ``_resolve("kb_search")`` 能命中真实实现而不是退回 mock 占位。
def kb_search(
    query: str,
    top_k: int = 8,
    filters: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """知识库混合检索（duck typing 入口，签名同 tools/__init__.py 约定）。

    Args:
        query: 检索查询文本。
        top_k: 返回条数。
        filters: 元数据过滤。

    Returns:
        ``{"chunks":[...], "total": int, "refused": bool, "reason": str}``。
    """
    res = _kb_search_handler(query=query, top_k=top_k, filters=filters or {})
    chunks = res.get("chunks") or []
    return {
        "chunks": chunks,
        "total": len(chunks),
        "refused": bool(res.get("refused")),
        "reason": res.get("reason"),
    }


def kb_read(chunk_id: str, include_parent: bool = True) -> dict[str, Any]:
    """按 chunk_id 读全文片段（duck typing 入口）。

    Args:
        chunk_id: 稳定 chunk 标识。
        include_parent: 是否回填父块。

    Returns:
        ``{"chunk_id","text","source","parent_text","neighbors"}``。
    """
    return _kb_read_handler(chunk_id=chunk_id, include_parent=include_parent)
