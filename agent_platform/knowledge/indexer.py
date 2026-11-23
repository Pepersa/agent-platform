# -*- coding: utf-8 -*-
"""统一索引构建器（**目录来源 + UI 导入来源** 双数据源）。

背景（本次修复的缺陷）：
    平台的知识库有两个数据来源，但此前**只有 ``data/docs/`` 目录**会被
    ``scripts/build_index.py --rebuild`` 与 ``POST /kb/reindex`` 扫描：

    1. **目录来源**：``data/docs/*.md`` —— 磁盘上的 Markdown，front matter
       提供元信息；
    2. **UI 导入来源**：用户在 Web UI（``/kb/documents``）上传 / 粘贴的文档，
       只存在于 ``data/kb/chunks.db`` 的 ``doc_source`` 表里。

    由于全量重建只扫目录，UI 导入的文档在重建后**静默消失**（chunk 被删、
    检索不到），构成数据丢失。

本模块把「采集 → 切块 → 向量化 → 落库」抽成**唯一一份实现**，供
``scripts/build_index.py``、``POST /kb/reindex``、``POST /kb/documents/{id}/reindex``
共同调用，避免再次漂移。

去重策略（**目录 vs doc_source 谁优先**）：
    统一以 ``doc_id`` 作为唯一键，**同一 doc_id 只保留一份 chunk**。
    冲突时的取舍规则（确定性、可解释）：

    * ``doc_source.origin == 'upload'``（用户经 UI 导入）→ **以 doc_source 为准**。
      理由：UI 导入时用户显式指定了密级 / 可见角色 / 产品线等 ACL，且原文
      **只存在于库里**；若改用目录副本，会丢失用户设置的 ACL 语义。
    * ``doc_source.origin == 'directory'``（历史重建时从目录留存）→
      **以目录当前文件为准**。理由：磁盘上的文件才是最新版本，库里的只是
      上次重建时的快照，若以快照为准则用户对文件的修改永远不生效。
    * 只在目录出现 → 用目录版本；只在 ``doc_source`` 出现 → 用 ``doc_source`` 版本。

    被用户显式删除的文档（``doc_tombstone`` 表）**一律跳过**，
    除非它是「目录来源」且磁盘目录里仍有同名文件 —— 因为目录文件是运维显式
    维护的权威副本，用户在 UI 上删索引 ≠ 删磁盘文件（产品约定的例外）。
    只有用户重新导入才会清除墓碑。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable, Sequence

from agent_platform.knowledge.chunker import Chunker
from agent_platform.knowledge.embed import Embedder
from agent_platform.knowledge.ingest import ingest_directory

__all__ = [
    "IndexBuildError",
    "SOURCE_DIRECTORY",
    "SOURCE_UPLOAD",
    "build_index",
    "collect_documents",
]

logger = logging.getLogger(__name__)

#: 来源标识：磁盘目录
SOURCE_DIRECTORY = "directory"
#: 来源标识：UI 上传 / 粘贴
SOURCE_UPLOAD = "upload"

ProgressFn = Callable[[str], None]


class IndexBuildError(Exception):
    """索引构建失败（无可用文档 / 文档目录不存在等）。"""


def _paragraph_sections(text: str) -> list[dict[str, Any]]:
    """把正文按空行切成段落级章节（与 KBAdmin 导入路径保持一致）。

    Args:
        text: 正文。

    Returns:
        章节列表。
    """
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    return [{"title_path": [], "level": 0, "body": p} for p in paras]


def _normalize_meta(meta: dict[str, Any] | None) -> dict[str, Any]:
    """归一化元信息（确保有 acl 子字典）。"""
    out = dict(meta or {})
    acl = dict(out.get("acl") or {})
    acl.setdefault("security_level", "internal")
    acl.setdefault("product_line", None)
    acl.setdefault("visible_roles", ["*"])
    out["acl"] = acl
    return out


def _chunk_with_fallback(chunker: Chunker, doc: dict[str, Any]) -> list[dict[str, Any]]:
    """切块（带「正文过短」的降级重试）。

    说明：UI 导入路径（``KBAdmin._write_document``）在正文过短、默认切分器
    切不出块时，会用更小的 ``min_chunk`` 再试一次，保证「随手粘一小段文字」
    也能入库。索引重建必须**复现同样的行为**，否则短文档在重建后会变成
    0 块 —— 表现为「文档还在列表里，但检索不到」，仍是变相的数据丢失。

    Args:
        chunker: 切分器。
        doc: 文档中间表示。

    Returns:
        chunk 列表（可能为空）。
    """
    chunks = chunker.chunk_document(doc)
    if chunks:
        return chunks
    try:
        loose = Chunker(
            chunk_size=chunker.chunk_size,
            chunk_overlap=chunker.chunk_overlap,
            min_chunk=8,
            parent_size=chunker.parent_size,
        )
        return loose.chunk_document(doc)
    except Exception:  # noqa: BLE001
        return []


def _doc_from_source_record(rec: dict[str, Any]) -> dict[str, Any] | None:
    """把 ``doc_source`` 表的一条记录还原成可切块的文档中间表示。

    Args:
        rec: :meth:`SqliteChunkStore.get_source` 的返回。

    Returns:
        文档中间表示；正文为空时返回 None。
    """
    content = rec.get("content") or ""
    if not content.strip():
        return None
    meta = _normalize_meta(rec.get("meta"))
    sections = meta.pop("sections", None)
    if not isinstance(sections, list) or not sections:
        sections = _paragraph_sections(content)
    origin = rec.get("origin") or SOURCE_UPLOAD
    stored_path = rec.get("stored_path") or ""
    source = rec.get("source") or f"{rec.get('doc_id')}.md"
    meta.setdefault("doc_type", "document")
    meta.setdefault("owner", "")
    meta["source"] = source
    meta["origin"] = origin
    meta["stored_path"] = stored_path
    return {
        "doc_id": rec.get("doc_id") or "",
        "title": rec.get("title") or rec.get("doc_id") or "",
        "version": str(meta.get("version") or "v1.0"),
        "text": content,
        "sections": sections,
        "metadata": meta,
        "source": source,
        "origin": origin,
        "stored_path": stored_path,
    }


def collect_documents(
    store: Any,
    *,
    docs_dir: str | None = None,
    include_uploads: bool = True,
    progress: ProgressFn | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """采集两个数据源的全部文档并按 ``doc_id`` 去重。

    Args:
        store: 存储实例（需支持 ``list_sources`` / ``list_tombstones``）。
        docs_dir: 文档目录（``None`` 表示不扫目录）。
        include_uploads: 是否纳入 ``doc_source`` 表（UI 导入）来源。
        progress: 进度回调。

    Returns:
        ``(文档列表, 统计 dict)``；统计含 ``directory`` / ``upload`` /
        ``duplicate`` / ``skipped_deleted`` / ``total``。
    """
    def _say(msg: str) -> None:
        if progress:
            progress(msg)

    docs_by_id: dict[str, dict[str, Any]] = {}
    stats = {"directory": 0, "upload": 0, "duplicate": 0,
             "skipped_deleted": 0, "total": 0}

    # ---- 来源 A：磁盘目录 ----
    if docs_dir:
        if not os.path.isdir(docs_dir):
            raise IndexBuildError(f"文档目录不存在：{docs_dir}")
        dir_docs = ingest_directory(docs_dir)
        for d in dir_docs:
            meta = _normalize_meta(d.get("metadata"))
            meta["origin"] = SOURCE_DIRECTORY
            meta["stored_path"] = d.get("source") or ""
            meta["source"] = d.get("source") or ""
            d["metadata"] = meta
            d["origin"] = SOURCE_DIRECTORY
            d["stored_path"] = meta["stored_path"]
            docs_by_id[d["doc_id"]] = d
            stats["directory"] += 1
        _say(f"目录来源：{stats['directory']} 份文档（{docs_dir}）")

    # 目录里出现过的文件名（用于判定 doc_source 记录是否就是该目录文件的快照）
    dir_sources = {
        (d.get("source") or "").strip() for d in docs_by_id.values() if d.get("source")
    }

    # ---- 来源 B：doc_source 表（UI 导入）----
    if include_uploads:
        try:
            records = store.list_sources()
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 doc_source 表失败（仅索引目录来源）：%s", exc)
            records = []
        for rec in records:
            doc = _doc_from_source_record(rec)
            if doc is None:
                continue
            rec_source = (rec.get("source") or "").strip()
            rec_doc_id = rec.get("doc_id") or ""
            existing = docs_by_id.get(rec_doc_id)
            # 该 doc_source 记录是否只是「磁盘目录那份文件的快照」？
            # 判据：文件名与目录里的某个文件相同（历史 rebuild 留存下来的记录
            #       必然满足；真正的 UI 上传通常文件名不同）。
            is_dir_snapshot = bool(rec_source) and rec_source in dir_sources
            if existing is not None and is_dir_snapshot:
                # 同 doc_id + 同文件名 → 同一份文档的两处副本，**目录优先**
                origin = SOURCE_DIRECTORY
            elif existing is not None:
                # 同 doc_id 但来源不同 → **UI 导入优先**
                origin = SOURCE_UPLOAD
            else:
                # 只在 doc_source 里 → 独立文档（上传来源）
                origin = SOURCE_UPLOAD
            doc["origin"] = origin
            doc["metadata"]["origin"] = origin
            if existing is not None:
                stats["duplicate"] += 1
                if origin == SOURCE_UPLOAD:
                    # **UI 导入优先**：用户设置的 ACL 与原文只在库里，
                    # 目录副本不足以代表用户的意图
                    docs_by_id[rec_doc_id] = doc
                    stats["upload"] += 1
                    stats["directory"] -= 1
                # 否则保留目录版本（磁盘上的文件才是最新）
                continue
            docs_by_id[rec_doc_id] = doc
            if origin == SOURCE_UPLOAD:
                stats["upload"] += 1
            else:
                stats["directory"] += 1
        _say(f"doc_source 来源：{stats['upload']} 份 UI 导入文档"
             f"（与目录去重 {stats['duplicate']} 份）")

    # ---- 删除墓碑过滤：已删除的文档不复活 ----
    # 语义（与产品约定一致）：
    #   * 用户删除了「UI 导入」的文档 → 库里已无原文 → 重建时**不会**复活；
    #   * 用户删除了「目录来源」的文档 → 若磁盘目录里仍有该文件，重建时
    #     **允许**重新接入（这是产品约定的例外：目录文件是运维显式维护的
    #     权威副本，用户删索引不等于删磁盘文件）。
    try:
        tombstones = set(store.list_tombstones())
    except Exception:  # noqa: BLE001
        tombstones = set()
    if tombstones:
        removed = []
        for did in list(docs_by_id.keys()):
            doc = docs_by_id[did]
            if did not in tombstones:
                continue
            if doc.get("origin") == SOURCE_DIRECTORY:
                # 目录来源：磁盘上还有文件 → 允许复活
                continue
            docs_by_id.pop(did, None)
            removed.append(did)
        stats["skipped_deleted"] = len(removed)
        if removed:
            _say(f"跳过 {len(removed)} 份已删除文档（墓碑）：{', '.join(removed)}")

    docs = [docs_by_id[k] for k in sorted(docs_by_id.keys())]
    stats["total"] = len(docs)
    return docs, stats


def build_index(
    store: Any,
    embedder: Embedder,
    chunker: Chunker,
    *,
    docs_dir: str | None = None,
    include_uploads: bool = True,
    rebuild: bool = True,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """**唯一的**索引构建实现（目录 + UI 导入双来源）。

    Args:
        store: 存储实例。
        embedder: Embedding 实例。
        chunker: 切分器。
        docs_dir: 文档目录（``None`` 表示不扫目录）。
        include_uploads: 是否纳入 ``doc_source`` 表来源。
        rebuild: 是否先清空旧索引（清空时**保留原文留存表与删除墓碑**）。
        progress: 进度回调（``Callable[[str], None]``）。

    Returns:
        构建结果 dict（``doc_count`` / ``chunk_count`` / ``docs`` / ``chunks`` 等）。

    Raises:
        IndexBuildError: 没有可用文档。
    """
    def _say(msg: str) -> None:
        if progress:
            progress(msg)
        logger.info(msg)

    docs, src_stats = collect_documents(
        store,
        docs_dir=docs_dir,
        include_uploads=include_uploads,
        progress=progress,
    )
    if not docs:
        raise IndexBuildError("没有可索引的文档（目录为空且 doc_source 表无记录）")

    _say(f"共 {len(docs)} 份文档待索引（目录 {src_stats['directory']} / "
         f"上传 {src_stats['upload']} / 去重 {src_stats['duplicate']}）")

    # ---- 切块 ----
    chunks: list[dict[str, Any]] = []
    per_doc: dict[str, int] = {}
    empty_docs: list[str] = []
    for d in docs:
        cs = _chunk_with_fallback(chunker, d)
        per_doc[d["doc_id"]] = len(cs)
        if not cs:
            empty_docs.append(d["doc_id"])
        chunks.extend(cs)
    if empty_docs:
        _say(f"⚠️ 有 {len(empty_docs)} 份文档切不出任何块（正文过短）："
             f"{', '.join(empty_docs)}")
    _say(f"切分完成：共 {len(chunks)} 块")

    # ---- 向量化 ----
    if chunks:
        matrix = embedder.embed([c["text"] for c in chunks])
        vectors = matrix.tolist() if hasattr(matrix, "tolist") else [list(v) for v in matrix]
        for c, v in zip(chunks, vectors):
            c["embedding"] = v
        _say(f"向量化完成：{len(chunks)} 块（后端 {getattr(embedder, 'backend', '?')}）")

    # ---- 落库 ----
    if rebuild:
        clear = getattr(store, "clear", None)
        if callable(clear):
            # 关键：保留 doc_source（原文留存）与墓碑，否则 UI 导入文档会永久丢失
            try:
                clear(keep_sources=True, keep_tombstones=True, keep_usage=True)
            except TypeError:
                clear()
            _say("已清空旧索引（保留原文留存表与删除墓碑）")
    n = store.upsert(chunks)

    # ---- 留存原文（供下次重建 / 单文档重建）----
    for d in docs:
        meta = dict(d.get("metadata") or {})
        meta["sections"] = d.get("sections") or []
        meta["version"] = d.get("version") or "v1.0"
        try:
            store.save_source(
                d["doc_id"],
                content=d.get("text") or "",
                title=d.get("title") or "",
                source=d.get("source") or "",
                meta=meta,
                origin=d.get("origin") or SOURCE_UPLOAD,
                stored_path=d.get("stored_path") or "",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("留存原文失败（%s）：%s", d.get("doc_id"), exc)

    stats = {}
    try:
        stats = store.stats()
    except Exception:  # noqa: BLE001
        stats = {}

    return {
        "ok": True,
        "doc_count": len(docs),
        "chunk_count": n,
        "docs_dir": os.path.abspath(docs_dir) if docs_dir else "",
        "directory_docs": src_stats["directory"],
        "upload_docs": src_stats["upload"],
        "duplicate_docs": src_stats["duplicate"],
        "skipped_deleted_docs": src_stats["skipped_deleted"],
        "per_doc_chunks": per_doc,
        "embedding_backend": getattr(embedder, "backend", "?"),
        "stats": stats,
        "docs": docs,
        "chunks": chunks,
    }
