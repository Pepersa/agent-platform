"""知识层（RAG 检索增强）模块。

对应设计文档：
    docs/01_架构方案_RAG_Agent.md §2.7 知识层 / §3.1 RAG 检索增强流程
    docs/02_场景方案_STDF分析与测试用例生成.md §3.5 RAG 设计

对外暴露：
    * :class:`Chunker` —— 父子分块
    * :class:`Embedder` / :class:`Reranker` —— 向量化与精排封装
    * :class:`Retriever` —— RAG 检索链（docs/01 §3.1 十步）
    * :class:`ChunkStore` / :func:`build_store` —— 存储抽象（SQLite / pgvector）
    * :func:`ingest_file` / :func:`ingest_directory` —— 文档接入
    * :func:`register_knowledge_tools` —— 向 Tool Registry 注册知识检索工具
    * :func:`tp_lookup` / :func:`spec_extract` / :func:`case_lint` /
      :func:`report_render` / :func:`notify` —— 真实数据源的知识层扩展工具
      （实现见 :mod:`agent_platform.knowledge.tools_ext`）
"""

from __future__ import annotations

import logging
from typing import Any

from agent_platform.knowledge.chunker import Chunker, stable_chunk_id
from agent_platform.knowledge.embed import Embedder
from agent_platform.knowledge.ingest import ingest_directory, ingest_file
from agent_platform.knowledge.llm import LLMClient
from agent_platform.knowledge.reranker import Reranker
from agent_platform.knowledge.retriever import (
    FINAL_TOP_K,
    MIN_SCORE_THRESHOLD,
    RECALL_N,
    RETRIEVAL_TOKEN_BUDGET,
    RRF_K,
    Retriever,
    UserContext,
)
from agent_platform.knowledge.store import (
    AclFilter,
    ChunkStore,
    PgChunkStore,
    SqliteChunkStore,
    build_store,
)
from agent_platform.knowledge.tools import kb_read, kb_search
from agent_platform.knowledge.tools_ext import (
    LINT_RULES,
    case_lint,
    ext_tool_specs,
    notify,
    report_render,
    spec_extract,
    tp_lookup,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Chunker",
    "stable_chunk_id",
    "Embedder",
    "Reranker",
    "Retriever",
    "UserContext",
    "ChunkStore",
    "AclFilter",
    "SqliteChunkStore",
    "PgChunkStore",
    "build_store",
    "ingest_file",
    "ingest_directory",
    "LLMClient",
    "register_knowledge_tools",
    "RRF_K",
    "RECALL_N",
    "FINAL_TOP_K",
    "RETRIEVAL_TOKEN_BUDGET",
    "MIN_SCORE_THRESHOLD",
    # --- 知识层扩展工具（真实数据源，非 mock）---
    "tp_lookup",
    "spec_extract",
    "case_lint",
    "report_render",
    "notify",
    "ext_tool_specs",
    "register_knowledge_ext_tools",
    "LINT_RULES",
    # --- duck typing 入口（tools/__init__.py 通过 getattr 解析）---
    "kb_search",
    "kb_read",
]


# ---------------------------------------------------------------------------
# Tool Registry 接入（不修改 tools/__init__.py，由 core 侧调用本函数）
# ---------------------------------------------------------------------------
def register_knowledge_tools(registry: Any) -> list[str]:
    """向 Tool Registry 注册知识检索工具。

    对应 docs/02 §5.2 工具清单中的 ``kb_search`` 与 ``kb_read``：

    =============  ====================================================
    工具名          作用
    =============  ====================================================
    ``kb_search``   混合检索知识库（BM25 ⊕ 向量 → RRF → Reranker → Top-k）
    ``kb_read``     读全文片段（按 chunk_id）
    =============  ====================================================

    **接口契约（与 ``core/tool_registry.py`` 对齐，字段名以本模块为准）**：
        注册项需为 ``ToolSpec`` 或其字典形式，字段如下::

            name:        str           工具名，唯一
            description: str           工具描述
            input_schema: dict         JSON Schema（object）
            output_schema: dict         JSON Schema（object）
            permission:  str           "READ_ONLY" | "COMPUTE" | ...
            hitl_required: bool
            timeout_ms:  int
            retry:       dict          {"max": int, "backoff_ms": int}
            audit:       bool
            handler:     Callable      同步/异步处理函数

        处理函数返回统一错误结构 ``{code, message, retryable, hint}``
        （docs/01 §3.2「失败归一」）。

    Args:
        registry: Tool Registry 实例（需具备 ``register(spec)`` 方法）。

    Returns:
        已注册的工具名列表。registry 不支持 register 时返回空列表并打日志。
    """
    if registry is None or not hasattr(registry, "register"):
        logger.warning("registry 不支持 register()，跳过知识工具注册。")
        return []

    from agent_platform.knowledge.tools import kb_search_spec, kb_read_spec

    registered: list[str] = []
    for factory in (kb_search_spec, kb_read_spec):
        try:
            spec = factory()
            registry.register(spec)
            registered.append(spec["name"] if isinstance(spec, dict) else spec.name)
        except Exception as exc:  # noqa: BLE001
            logger.error("知识工具注册失败：%s", exc)
    return registered


def register_knowledge_ext_tools(registry: Any) -> list[str]:
    """向 Tool Registry 注册 5 个知识层扩展工具（真实实现，非 mock）。

    这是**独立于** :func:`register_knowledge_tools` 的第二条注册路径
    （docs/02 §5.2 工具清单中的 ``tp_lookup`` / ``spec_extract`` / ``case_lint`` /
    ``report_render`` / ``notify``）。

    权限级别与 HITL 标记严格对齐 docs/02 §5.2：

    ==================  =======================  ==========
    工具                 权限                      HITL
    ==================  =======================  ==========
    ``tp_lookup``        ``READ_ONLY``              ❌
    ``spec_extract``     ``COMPUTE``                ❌
    ``case_lint``        ``COMPUTE``                ❌
    ``report_render``    ``WRITE_DATA``             ✅
    ``notify``           ``EXTERNAL_SIDE_EFFECT``   ✅
    ==================  =======================  ==========

    已注册过的工具名会被跳过（不覆盖），因此可与
    ``agent_platform.tools.register_all`` 共存。

    Args:
        registry: Tool Registry 实例（需具备 ``register(spec)`` 方法）。

    Returns:
        实际注册的工具名列表。
    """
    from agent_platform.knowledge.tools_ext import register_knowledge_ext_tools as _reg

    return _reg(registry)
