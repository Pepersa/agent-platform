#!/usr/bin/env python
"""构建知识库索引：扫描文档目录 **+ UI 导入文档** → 切块 → 向量化 → 写入存储。

对应设计文档：
    docs/01_架构方案_RAG_Agent.md
        §2.7 知识层 —— 「文档接入与解析」「切分策略」「Embedding 服务」「向量库」
        §3.1 RAG 检索增强流程 —— 第 5 步（候选规模与去重）
    docs/02_场景方案_STDF分析与测试用例生成.md
        §3.5 RAG 设计 —— 各知识源的切分策略与元数据

**数据源（两个，都会索引）**：
    1. **目录来源**：``data/docs/*.md`` —— 磁盘上的文件；
    2. **UI 导入来源**：``data/kb/chunks.db`` 的 ``doc_source`` 表 —— 用户在
       Web 工作台（``/kb/documents``）上传 / 粘贴的文档（原文存在库里）。

    按 ``doc_id`` 去重（同一 doc_id 只保留一份 chunk）；去重取舍与「删除不复活」
    墓碑逻辑见 :mod:`agent_platform.knowledge.indexer` 的模块 docstring。

    ⚠️ 本脚本只负责**解析命令行参数与打印进度**，索引逻辑全部委托给
    :func:`agent_platform.knowledge.indexer.build_index`（与 ``POST /kb/reindex``
    共用同一份实现）。

用法示例::

    python scripts/build_index.py --docs-dir data/docs --store sqlite
    python scripts/build_index.py --docs-dir data/docs --store sqlite --rebuild
    python scripts/build_index.py --docs-dir data/docs --store pg --dsn "postgresql://..."
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

# 允许以 `python scripts/build_index.py` 直接运行（把仓库根加入 sys.path）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent_platform.knowledge.chunker import Chunker  # noqa: E402
from agent_platform.core import paths as _paths  # noqa: E402
from agent_platform.knowledge.embed import Embedder  # noqa: E402
from agent_platform.knowledge.indexer import (  # noqa: E402
    IndexBuildError,
    build_index,
)
from agent_platform.knowledge.store import build_store  # noqa: E402

logger = logging.getLogger("build_index")

#: 默认文档目录（统一数据目录下的 ``docs/``；可用 AGENT_DATA_DIR 整体外置）
DEFAULT_DOCS_DIR = str(_paths.data_path("docs"))
#: 默认 SQLite 索引路径（统一数据目录下的 ``kb/chunks.db``）
DEFAULT_SQLITE_PATH = str(_paths.data_path("kb", "chunks.db"))
DEFAULT_SCHEMA = os.path.join("agent_platform", "knowledge", "schema.sql")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。

    Args:
        argv: 参数列表（None 表示取 sys.argv）。

    Returns:
        参数命名空间。
    """
    p = argparse.ArgumentParser(
        description="构建 RAG 知识库索引（docs/01 §2.7 知识层）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--docs-dir", default=DEFAULT_DOCS_DIR, help="文档目录")
    p.add_argument(
        "--store",
        choices=["sqlite", "pg"],
        default="sqlite",
        help="存储后端（pg 不可用时自动降级为 sqlite）",
    )
    p.add_argument("--dsn", default=None, help="PostgreSQL 连接串（--store pg 时使用）")
    p.add_argument(
        "--sqlite-path", default=DEFAULT_SQLITE_PATH, help="SQLite 索引文件路径"
    )
    p.add_argument(
        "--rebuild", action="store_true", help="重建索引（先清空现有数据）"
    )
    p.add_argument(
        "--no-uploads",
        action="store_true",
        help="不索引 UI 导入的文档（只索引 docs-dir 目录来源；默认两者都索引）",
    )
    p.add_argument("--chunk-size", type=int, default=300, help="子块目标 token 数")
    p.add_argument("--chunk-overlap", type=int, default=80, help="子块重叠 token 数")
    p.add_argument("--min-chunk", type=int, default=50, help="最小块 token 数")
    p.add_argument("--parent-size", type=int, default=1200, help="父块目标 token 数")
    p.add_argument("--batch-size", type=int, default=16, help="Embedding 批大小")
    p.add_argument(
        "--embed-backend",
        choices=["auto", "flagembedding", "llama", "hash"],
        default="auto",
        help="Embedding 后端（auto 自动探测）",
    )
    p.add_argument("--schema", default=DEFAULT_SCHEMA, help="schema.sql 路径（PG 建表用）")
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

    docs_dir = os.path.abspath(args.docs_dir)
    if not os.path.isdir(docs_dir):
        logger.error("文档目录不存在：%s", docs_dir)
        return 2

    t0 = time.perf_counter()

    # ---- 1. Embedding（docs/01 §2.7 Embedding 服务）----
    embedder = Embedder(
        backend=None if args.embed_backend == "auto" else args.embed_backend,
        batch_size=args.batch_size,
    )
    logger.info("Embedding 后端：%s（降级模式=%s）", embedder.backend, embedder.is_degraded)
    if embedder.is_degraded:
        logger.warning(
            "⚠️ 当前为 hash 伪向量降级模式，检索语义质量不可用，"
            "仅用于让管线跑通。生产请部署 bge-m3 或 llama-server embedding 端点。"
        )

    # ---- 2. 父子切分器（docs/01 §2.7 切分策略）----
    chunker = Chunker(
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
        min_chunk=args.min_chunk,
        parent_size=args.parent_size,
    )

    # ---- 3. 存储 ----
    store = build_store(
        kind=args.store,
        dsn=args.dsn,
        sqlite_path=args.sqlite_path,
        schema_path=os.path.abspath(args.schema) if os.path.isfile(args.schema) else None,
    )
    logger.info("存储后端：%s", getattr(store, "path", None) or "pgvector")

    # ---- 4. 索引构建（共享实现：目录来源 + UI 导入来源）----
    # 说明：核心逻辑在 agent_platform.knowledge.indexer.build_index，
    #      与 POST /kb/reindex 完全一致（避免两套实现漂移导致数据丢失）。
    try:
        result = build_index(
            store,
            embedder,
            chunker,
            docs_dir=docs_dir,
            include_uploads=not args.no_uploads,
            rebuild=args.rebuild,
            progress=lambda m: logger.info("%s", m),
        )
    except IndexBuildError as exc:
        logger.error("索引构建失败：%s", exc)
        return 3

    chunks = result.get("chunks") or []
    docs = result.get("docs") or []
    parents = [c for c in chunks if c["chunk_type"] == "parent"]
    children = [c for c in chunks if c["chunk_type"] == "child"]
    total_tokens = sum(c["token_count"] for c in chunks)
    stats = result.get("stats") or {}

    # ---- 5. 统计输出 ----
    elapsed = time.perf_counter() - t0
    print("\n" + "=" * 62)
    print("知识库索引构建完成")
    print("=" * 62)
    print(f"  文档数        : {stats.get('doc_count', len(docs))}")
    print(f"  chunk 总数    : {stats.get('total_chunks', len(chunks))}")
    print(f"  父块数        : {stats.get('parent_chunks', len(parents))}")
    print(f"  子块数        : {stats.get('child_chunks', len(children))}")
    print(f"  token 总量    : {stats.get('total_tokens', total_tokens)}")
    print(f"  平均 chunk tok: {total_tokens / max(1, len(chunks)):.1f}")
    print(f"  Embedding     : {embedder.backend}")
    print(f"  存储后端      : {stats.get('backend', args.store)}")
    print(f"  耗时          : {elapsed:.2f}s")
    print("=" * 62)

    # 输出 chunk 清单（便于评测集写 gold_chunks）
    manifest_path = str(_paths.data_path("kb", "chunk_manifest.json"))
    try:
        os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {
                        "chunk_id": c["chunk_id"],
                        "doc_id": c["doc_id"],
                        "doc_title": c["doc_title"],
                        "doc_version": c["doc_version"],
                        "chunk_type": c["chunk_type"],
                        "parent_id": c["parent_id"],
                        "token_count": c["token_count"],
                        "section": c["metadata"].get("section"),
                        "title_path": c["metadata"].get("title_path"),
                        "source": c["metadata"].get("source"),
                        "text_head": c["text"][:120],
                    }
                    for c in chunks
                ],
                f,
                ensure_ascii=False,
                indent=2,
            )
        print(f"  chunk 清单    : {manifest_path}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("chunk 清单写入失败：%s", exc)

    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
