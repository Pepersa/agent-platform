"""Chunk 存储抽象 + SQLite 降级实现 + pgvector 实现。

对应设计文档：
    docs/01_架构方案_RAG_Agent.md
        §2.7 知识层 —— 「向量库」（Milvus / PG+pgvector）、「全文检索」（BM25）、
          「Metadata 与 ACL」（结构化 metadata 字段）
        §3.1 RAG 检索增强流程 —— 第 2 步（**检索前**按用户权限过滤 metadata）、
          第 3 步（多路召回）、第 4 步（RRF 融合 k=60）
        §4 技术选型 —— 向量库选型
    docs/02_场景方案_STDF分析与测试用例生成.md
        §3.5 RAG 设计 —— 检索流程（BM25 ⊕ Dense → RRF(k=60) → Reranker → Top-k）

设计要点：
    * ACL 过滤条件必须**下推到存储层 WHERE 子句**（docs/01 §3.1 第 2 步明确
      「检索后过滤有信息泄漏风险与 top-k 被稀释问题」）；
    * PG 不可用时上层自动降级到 SQLite（numpy 向量 + 纯 Python BM25 倒排），
      本机 WSL/Docker 均 Disabled，不得假设 PostgreSQL 可用。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import threading
from abc import ABC, abstractmethod
from typing import Any, Iterable, Optional, Sequence

__all__ = [
    "ChunkStore",
    "SqliteChunkStore",
    "PgChunkStore",
    "AclFilter",
    "build_store",
    "tokenize",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ACL 过滤器
# ---------------------------------------------------------------------------
class AclFilter:
    """ACL 过滤条件（检索前下推）。

    对应 schema.sql 中的 ``security_level`` / ``product_line`` / ``visible_roles``
    三列，以及 docs/01 §2.7「Metadata 与 ACL」。

    Attributes:
        max_security_level: 用户可见的最高密级（public < internal < confidential < secret）。
        product_line: 用户可见的产品线（None 表示不限）。
        roles: 用户拥有的角色列表（空列表表示不限）。
        doc_ids: 限定文档集合（None 表示不限）。
    """

    __slots__ = ("max_security_level", "product_line", "roles", "doc_ids")

    _LEVEL_ORDER = {"public": 1, "internal": 2, "confidential": 3, "secret": 4}

    def __init__(
        self,
        max_security_level: str = "internal",
        product_line: str | None = None,
        roles: Sequence[str] | None = None,
        doc_ids: Sequence[str] | None = None,
    ) -> None:
        self.max_security_level = max_security_level
        self.product_line = product_line
        self.roles = list(roles or [])
        self.doc_ids = list(doc_ids) if doc_ids else None

    def level_rank(self, level: str | None) -> int:
        """返回密级排序值。

        Args:
            level: 密级字符串。

        Returns:
            排序值（未知密级视为 0，即最低）。
        """
        return self._LEVEL_ORDER.get((level or "").lower(), 0)

    def is_empty(self) -> bool:
        """是否为「无限制」过滤器。"""
        return (
            self.max_security_level in ("", None, "public")
            and not self.product_line
            and not self.roles
            and not self.doc_ids
        )

    def to_dict(self) -> dict[str, Any]:
        """转 dict（便于审计日志记录）。"""
        return {
            "max_security_level": self.max_security_level,
            "product_line": self.product_line,
            "roles": list(self.roles),
            "doc_ids": list(self.doc_ids) if self.doc_ids else None,
        }


# ---------------------------------------------------------------------------
# 分词（纯 Python，供 BM25 倒排使用）
# ---------------------------------------------------------------------------
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: str) -> list[str]:
    """混合分词：英文/数字按词，中文按字 + 二元组。

    说明：工程场景下型号（STW-2100）、条目名（T205_TSHIFT_FUNC）必须精确
    命中，因此英文数字串整体保留；中文用「单字 + 二元组」兼顾召回与精度。

    Args:
        text: 原文。

    Returns:
        token 列表。
    """
    if not text:
        return []
    tokens: list[str] = []
    lower = text.lower()
    for w in _WORD_RE.findall(lower):
        tokens.append(w)
        # 拆分下划线/连字符的复合标识符
        for part in re.split(r"[_\-.]", w):
            if part and part != w:
                tokens.append(part)
    # 中文：单字 + 二元组
    cjk_chars = _CJK_RE.findall(text)
    tokens.extend(cjk_chars)
    for i in range(len(cjk_chars) - 1):
        tokens.append(cjk_chars[i] + cjk_chars[i + 1])
    return tokens


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------
class ChunkStore(ABC):
    """Chunk 存储抽象。

    对应 docs/01 §2.7「向量库」「全文检索」两个模块的统一接口。
    """

    # -- 写入 ---------------------------------------------------------
    @abstractmethod
    def upsert(self, chunks: Sequence[dict[str, Any]]) -> int:
        """写入/更新 chunk。

        Args:
            chunks: chunk dict 列表（含 chunk_id/text/token_count/metadata 等）。

        Returns:
            实际写入条数。
        """

    # -- 检索 ---------------------------------------------------------
    @abstractmethod
    def search_vector(
        self,
        query_vec: Any,
        top_n: int = 50,
        acl_filter: AclFilter | None = None,
    ) -> list[dict[str, Any]]:
        """向量通道检索。

        Args:
            query_vec: 查询向量。
            top_n: 返回条数（docs/01 §3.1 第 5 步召回 top-50）。
            acl_filter: ACL 过滤条件（**在存储层下推**）。

        Returns:
            候选列表，按相似度降序，每项含 ``chunk_id`` / ``score`` / chunk 字段。
        """

    @abstractmethod
    def search_bm25(
        self,
        query: str,
        top_n: int = 50,
        acl_filter: AclFilter | None = None,
    ) -> list[dict[str, Any]]:
        """BM25 通道检索。

        Args:
            query: 查询文本。
            top_n: 返回条数。
            acl_filter: ACL 过滤条件（**在存储层下推**）。

        Returns:
            候选列表，按 BM25 分数降序。
        """

    # -- 读取 ---------------------------------------------------------
    @abstractmethod
    def get_by_id(self, chunk_id: str) -> dict[str, Any] | None:
        """按 chunk_id 取单个 chunk。

        Args:
            chunk_id: 块 ID。

        Returns:
            chunk dict；不存在返回 None。
        """

    @abstractmethod
    def get_parent(self, parent_id: str) -> dict[str, Any] | None:
        """按 parent_id 取父块（上下文回填用）。

        Args:
            parent_id: 父块 ID。

        Returns:
            父块 dict；不存在返回 None。
        """

    # -- 运维 ---------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        """返回统计信息（chunk 数、父块数、token 总量等）。"""
        return {}

    def list_documents(self) -> list[dict[str, Any]]:
        """列出已入库文档（doc_id 维度聚合）。

        Returns:
            每项含 ``doc_id`` / ``doc_title`` / ``doc_version`` / ``chunk_count`` /
            ``child_count`` / ``parent_count`` / ``token_count`` / ``security_level`` /
            ``visible_roles`` / ``product_line`` / ``source`` / ``updated_at`` /
            ``hit_count``。
        """
        return []

    def delete_doc(self, doc_id: str) -> int:
        """删除某文档的全部 chunk（连同倒排项）。

        Args:
            doc_id: 文档 ID。

        Returns:
            删除的 chunk 条数。
        """
        return 0

    def record_hits(self, doc_ids: Iterable[str]) -> None:
        """记录文档被检索命中的次数（真实发生才记）。

        Args:
            doc_ids: 本次检索命中的文档 ID 集合。
        """
        return None

    def save_source(
        self,
        doc_id: str,
        *,
        content: str,
        title: str = "",
        source: str = "",
        meta: dict[str, Any] | None = None,
        origin: str = "upload",
        stored_path: str = "",
    ) -> None:
        """留存文档原文（供「单文档重建索引」与「全量重建」使用）。

        Args:
            doc_id: 文档 ID。
            content: 文档正文（原文全文）。
            title: 标题。
            source: 来源标识。
            meta: 附加元数据。
            origin: 来源类型（``upload`` / ``directory``）。
            stored_path: 原文物理存储位置。
        """
        return None

    def get_source(self, doc_id: str) -> dict[str, Any] | None:
        """取留存的文档原文。

        Args:
            doc_id: 文档 ID。

        Returns:
            原文 dict；不存在返回 None。
        """
        return None

    def list_sources(self) -> list[dict[str, Any]]:
        """列出留存的全部文档原文记录（UI 导入来源）。"""
        return []

    def mark_deleted(
        self, doc_id: str, reason: str = "", origin: str = ""
    ) -> None:
        """写删除墓碑（防止全量重建把已删文档复活）。"""
        return None

    def list_tombstones(self) -> list[str]:
        """列出所有删除墓碑 doc_id。"""
        return []

    def unmark_deleted(self, doc_id: str) -> None:
        """清除删除墓碑（文档被重新导入时调用）。"""
        return None

    def close(self) -> None:
        """关闭底层资源。"""
        return None


# ---------------------------------------------------------------------------
# SQLite 实现（本地降级路径）
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id     TEXT PRIMARY KEY,
    doc_id       TEXT NOT NULL,
    doc_title    TEXT,
    doc_version  TEXT,
    chunk_type   TEXT NOT NULL DEFAULT 'child',
    parent_id    TEXT,
    text         TEXT NOT NULL,
    token_count  INTEGER NOT NULL DEFAULT 0,
    metadata     TEXT NOT NULL DEFAULT '{}',
    acl_level    TEXT NOT NULL DEFAULT 'internal',
    product_line TEXT,
    visible_roles TEXT NOT NULL DEFAULT '["*"]',
    embedding    BLOB,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_chunks_parent ON chunks(parent_id);
CREATE INDEX IF NOT EXISTS idx_chunks_type ON chunks(chunk_type);
CREATE INDEX IF NOT EXISTS idx_chunks_acl ON chunks(acl_level, product_line);

-- BM25 倒排表（自建，非 FTS5；FTS5 的 bm25() 在中文分词上不可控）
CREATE TABLE IF NOT EXISTS chunk_terms (
    chunk_id TEXT NOT NULL,
    term     TEXT NOT NULL,
    tf       INTEGER NOT NULL,
    PRIMARY KEY (chunk_id, term)
);
CREATE INDEX IF NOT EXISTS idx_terms_term ON chunk_terms(term);

-- 文档级统计（被检索命中次数等；仅记录真实发生的检索，不臆造）
CREATE TABLE IF NOT EXISTS doc_usage (
    doc_id      TEXT PRIMARY KEY,
    hit_count   INTEGER NOT NULL DEFAULT 0,
    last_hit_at TEXT
);

-- 文档原文留存（用于「单文档重建索引」与「全量重建时重新切块」；不存原文就无法重建）
CREATE TABLE IF NOT EXISTS doc_source (
    doc_id  TEXT PRIMARY KEY,
    title   TEXT,
    source  TEXT,
    content TEXT NOT NULL,
    meta    TEXT NOT NULL DEFAULT '{}',
    origin      TEXT NOT NULL DEFAULT 'upload',
    stored_path TEXT NOT NULL DEFAULT '',
    char_count  INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 用户显式删除的文档墓碑（防止「全量重建」把已删文档复活）
CREATE TABLE IF NOT EXISTS doc_tombstone (
    doc_id     TEXT PRIMARY KEY,
    deleted_at TEXT NOT NULL DEFAULT (datetime('now')),
    reason     TEXT NOT NULL DEFAULT '',
    origin     TEXT NOT NULL DEFAULT ''
);
"""

#: doc_source 表的列定义（用于旧库迁移：新列缺一个就 ALTER TABLE 补一个）
#: 注意：``ALTER TABLE ADD COLUMN`` **不允许非常量默认值**（SQLite 限制），
#: 因此 ``updated_at`` 用常量默认值 ''，迁移后再统一回填。
_DOC_SOURCE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("origin", "TEXT NOT NULL DEFAULT 'upload'"),
    ("stored_path", "TEXT NOT NULL DEFAULT ''"),
    ("char_count", "INTEGER NOT NULL DEFAULT 0"),
    ("updated_at", "TEXT NOT NULL DEFAULT ''"),
)

#: doc_tombstone 表的列定义（同上，用于旧库迁移）
_DOC_TOMBSTONE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("origin", "TEXT NOT NULL DEFAULT ''"),
)



class SqliteChunkStore(ChunkStore):
    """SQLite + numpy 向量 + 纯 Python BM25 倒排的本地降级实现。

    说明：
        * 向量以 float32 BLOB 存储，检索时暴力计算余弦相似度（几千 chunk 量级
          完全够用；docs/01 §7 数据规模假设：M1 阶段远小于此）；
        * BM25 用自建倒排表，参数 k1=1.5 / b=0.75（Lucene 默认量级）；
        * ACL 过滤在 SQL WHERE 中下推。

    Args:
        path: SQLite 文件路径（``:memory:`` 支持内存模式）。
        dimension: 向量维度。
        chunk_types: 参与检索的块类型（默认只检索 child）。
    """

    def __init__(
        self,
        path: Optional[str] = None,
        dimension: int = 1024,
        chunk_types: Sequence[str] = ("child",),
    ) -> None:
        # 默认索引路径走统一数据目录解析（``AGENT_DATA_DIR`` 可整体外置）；
        # 不设环境变量时仍是 ``<repo>/data/kb/chunks.db``（向后兼容）。
        if path is None:
            from agent_platform.core import paths as _paths  # noqa: PLC0415

            path = str(_paths.data_path("kb", "chunks.db"))
        self.path = path
        self.dimension = int(dimension)
        self.chunk_types = tuple(chunk_types)
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # check_same_thread=False：评测 Runner 会把单条用例放到工作线程里跑
        # （带超时保护），sqlite3 默认禁止跨线程使用同一连接。
        # 本模块的访问都是短事务（SELECT / 单条 UPSERT），配合
        # ``self._lock`` 串行化即可安全。
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self.conn.executescript(_SCHEMA)
        self._migrate_doc_source()
        self._migrate_doc_tombstone()
        self.conn.commit()

    def _migrate_columns(
        self, table: str, columns: Sequence[tuple[str, str]]
    ) -> None:
        """给旧表补齐缺失的列（逐列 ALTER TABLE ADD COLUMN）。

        Args:
            table: 表名。
            columns: ``(列名, DDL 片段)`` 列表。
        """
        try:
            rows = list(self.conn.execute(f"PRAGMA table_info({table})"))
        except Exception:  # noqa: BLE001
            return
        existing = {r["name"] for r in rows}
        for name, ddl in columns:
            if name in existing:
                continue
            try:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                logger.info("迁移 %s 表：新增列 %s", table, name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("迁移 %s 表失败（%s）：%s", table, name, exc)

    def _migrate_doc_source(self) -> None:
        """旧库迁移：给 ``doc_source`` 补齐新列（不假设全新安装）。

        说明：
            * 早期版本的 ``doc_source`` 只有 5 列（doc_id/title/source/content/meta），
              后续新增了 ``origin`` / ``stored_path`` / ``char_count`` / ``updated_at``；
            * 旧记录没有 ``origin``，按「上传/历史」处理；存量目录文档会在下次
              重建时被覆盖为 ``directory``。
        """
        self._migrate_columns("doc_source", _DOC_SOURCE_COLUMNS)
        try:
            self.conn.execute(
                "UPDATE doc_source SET origin = 'upload' "
                "WHERE origin IS NULL OR origin = ''"
            )
            # 回填 char_count 与 updated_at（老库没有这两列的数据）
            self.conn.execute(
                "UPDATE doc_source SET char_count = LENGTH(content) "
                "WHERE char_count IS NULL OR char_count = 0"
            )
            self.conn.execute(
                "UPDATE doc_source SET updated_at = datetime('now') "
                "WHERE updated_at IS NULL OR updated_at = ''"
            )
        except Exception:  # noqa: BLE001
            pass

    def _migrate_doc_tombstone(self) -> None:
        """旧库迁移：给 ``doc_tombstone`` 补齐新列。"""
        self._migrate_columns("doc_tombstone", _DOC_TOMBSTONE_COLUMNS)

    # ------------------------------------------------------------------
    def upsert(self, chunks: Sequence[dict[str, Any]]) -> int:
        """写入/更新 chunk（含向量与倒排表）。

        Args:
            chunks: chunk 列表。

        Returns:
            写入条数。
        """
        if not chunks:
            return 0
        try:
            import numpy as np  # type: ignore
        except Exception:
            np = None  # type: ignore

        n = 0
        with self._lock:
            with self.conn:
                for c in chunks:
                    meta = dict(c.get("metadata") or {})
                    acl = dict(meta.get("acl") or {})
                    level = str(
                        acl.get("security_level")
                        or meta.get("security_level")
                        or "internal"
                    )
                    product_line = acl.get("product_line") or meta.get("product_line")
                    roles = acl.get("visible_roles") or meta.get("visible_roles") or ["*"]
                    if isinstance(roles, str):
                        roles = [roles]

                    emb_blob = None
                    vec = c.get("embedding")
                    if vec is not None:
                        if np is not None:
                            arr = np.asarray(vec, dtype=np.float32).ravel()
                            if arr.size > self.dimension:
                                arr = arr[: self.dimension]
                            emb_blob = arr.tobytes()
                        else:
                            emb_blob = b"".join(
                                struct_pack_float(x) for x in list(vec)[: self.dimension]
                            )

                    self.conn.execute(
                        """INSERT INTO chunks
                           (chunk_id, doc_id, doc_title, doc_version, chunk_type,
                            parent_id, text, token_count, metadata,
                            acl_level, product_line, visible_roles, embedding)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(chunk_id) DO UPDATE SET
                             doc_title=excluded.doc_title,
                             doc_version=excluded.doc_version,
                             chunk_type=excluded.chunk_type,
                             parent_id=excluded.parent_id,
                             text=excluded.text,
                             token_count=excluded.token_count,
                             metadata=excluded.metadata,
                             acl_level=excluded.acl_level,
                             product_line=excluded.product_line,
                             visible_roles=excluded.visible_roles,
                             embedding=excluded.embedding""",
                        (
                            c["chunk_id"],
                            c.get("doc_id", ""),
                            c.get("doc_title", ""),
                            c.get("doc_version", ""),
                            c.get("chunk_type", "child"),
                            c.get("parent_id"),
                            c.get("text", ""),
                            int(c.get("token_count") or 0),
                            json.dumps(meta, ensure_ascii=False),
                            level,
                            product_line,
                            json.dumps(list(roles), ensure_ascii=False),
                            emb_blob,
                        ),
                    )
                    # 倒排：先删后插
                    self.conn.execute(
                        "DELETE FROM chunk_terms WHERE chunk_id = ?", (c["chunk_id"],)
                    )
                    tf = _term_freq(c.get("text", ""))
                    if tf:
                        self.conn.executemany(
                            "INSERT INTO chunk_terms (chunk_id, term, tf) VALUES (?,?,?)",
                            [(c["chunk_id"], t, v) for t, v in tf.items()],
                        )
                    n += 1
        return n

    # ------------------------------------------------------------------
    def _acl_sql(self, acl_filter: AclFilter | None) -> tuple[str, list[Any]]:
        """构造 ACL 的 WHERE 片段（**下推到 SQL**）。

        Args:
            acl_filter: 过滤器。

        Returns:
            ``(sql_fragment, params)``。
        """
        if acl_filter is None:
            return "", []
        frags: list[str] = []
        params: list[Any] = []
        max_rank = acl_filter.level_rank(acl_filter.max_security_level)
        if max_rank > 0:
            allowed = [lv for lv, r in AclFilter._LEVEL_ORDER.items() if r <= max_rank]
            frags.append(
                "(" + " OR ".join(["acl_level = ?"] * len(allowed)) + ")"
            )
            params.extend(allowed)
        if acl_filter.product_line:
            frags.append("(product_line IS NULL OR product_line = ?)")
            params.append(acl_filter.product_line)
        if acl_filter.roles:
            frags.append(
                "(visible_roles LIKE '%\"*\"%' OR "
                + " OR ".join(["visible_roles LIKE ?"] * len(acl_filter.roles))
                + ")"
            )
            for r in acl_filter.roles:
                params.append(f'%"{r}"%')
        if acl_filter.doc_ids:
            frags.append(
                "(" + " OR ".join(["doc_id = ?"] * len(acl_filter.doc_ids)) + ")"
            )
            params.extend(acl_filter.doc_ids)
        if not frags:
            return "", []
        return " AND " + " AND ".join(frags), params

    def _candidate_rows(
        self, acl_filter: AclFilter | None, doc_id: str | None = None
    ) -> list[sqlite3.Row]:
        """取出参与检索的候选行（含 ACL 下推）。

        Args:
            acl_filter: ACL 过滤器。
            doc_id: 限定文档。

        Returns:
            sqlite 行列表。
        """
        where = ["chunk_type IN (%s)" % ",".join("?" * len(self.chunk_types))]
        params: list[Any] = list(self.chunk_types)
        acl_sql, acl_params = self._acl_sql(acl_filter)
        if acl_sql:
            # _acl_sql 返回形如 " AND (cond) AND (cond)"，去掉前缀 " AND " 后并入 where
            where.append(acl_sql[len(" AND "):])
            params.extend(acl_params)
        if doc_id:
            where.append("doc_id = ?")
            params.append(doc_id)
        sql = "SELECT * FROM chunks WHERE " + " AND ".join(where)
        with self._lock:
            return list(self.conn.execute(sql, params))

    # ------------------------------------------------------------------
    def search_vector(
        self,
        query_vec: Any,
        top_n: int = 50,
        acl_filter: AclFilter | None = None,
    ) -> list[dict[str, Any]]:
        """暴力余弦相似度检索。

        Args:
            query_vec: 查询向量。
            top_n: 返回条数。
            acl_filter: ACL 过滤器（下推到 SQL WHERE）。

        Returns:
            候选列表（降序）。
        """
        rows = self._candidate_rows(acl_filter)
        if not rows:
            return []
        try:
            import numpy as np  # type: ignore
        except Exception:
            np = None  # type: ignore

        q = list(query_vec) if not hasattr(query_vec, "tolist") else query_vec.tolist()
        scored: list[tuple[float, sqlite3.Row]] = []
        for r in rows:
            blob = r["embedding"]
            if not blob:
                continue
            if np is not None:
                v = np.frombuffer(blob, dtype=np.float32)
                qq = np.asarray(q, dtype=np.float32)
                n = min(len(v), len(qq))
                if n == 0:
                    continue
                dot = float(np.dot(v[:n], qq[:n]))
                nv = float(np.linalg.norm(v[:n])) or 1.0
                nq = float(np.linalg.norm(qq[:n])) or 1.0
                score = dot / (nv * nq)
            else:
                vv = list(struct_unpack_floats(blob))
                n = min(len(vv), len(q))
                dot = sum(a * b for a, b in zip(vv[:n], q[:n]))
                nv = math.sqrt(sum(x * x for x in vv[:n])) or 1.0
                nq = math.sqrt(sum(x * x for x in q[:n])) or 1.0
                score = dot / (nv * nq)
            scored.append((score, r))
        scored.sort(key=lambda t: -t[0])
        return [
            {**self._row_to_dict(r), "score": float(s)} for s, r in scored[: int(top_n)]
        ]

    def search_bm25(
        self,
        query: str,
        top_n: int = 50,
        acl_filter: AclFilter | None = None,
    ) -> list[dict[str, Any]]:
        """BM25 检索。

        公式（标准 BM25）：``score = Σ_t IDF(t) * (tf * (k1+1)) /
        (tf + k1 * (1 - b + b * len/avg_len))``，其中 ``k1=1.5``、``b=0.75``，
        ``IDF(t) = ln(1 + (N - df + 0.5)/(df + 0.5))``。

        Args:
            query: 查询文本。
            top_n: 返回条数。
            acl_filter: ACL 过滤器（下推到 SQL WHERE）。

        Returns:
            候选列表（降序）。
        """
        rows = self._candidate_rows(acl_filter)
        if not rows:
            return []
        by_id = {r["chunk_id"]: r for r in rows}
        lens = {r["chunk_id"]: max(1, _token_len(r["text"])) for r in rows}
        avg_len = sum(lens.values()) / max(1, len(lens))

        q_terms = set(tokenize(query))
        if not q_terms:
            return []

        k1, b = 1.5, 0.75
        total_docs = len(by_id)
        placeholders = ",".join("?" * len(q_terms))
        with self._lock:
            df_rows = list(
                self.conn.execute(
                    f"""SELECT term, COUNT(DISTINCT chunk_id) AS df
                        FROM chunk_terms WHERE term IN ({placeholders})
                        GROUP BY term""",
                    list(q_terms),
                )
            )
            df_map = {r["term"]: int(r["df"]) for r in df_rows}
            if not df_map:
                return []

            scores: dict[str, float] = {}
            for term, df in df_map.items():
                idf = math.log(1 + (total_docs - df + 0.5) / (df + 0.5))
                if idf <= 0:
                    continue
                for tr in self.conn.execute(
                    "SELECT chunk_id, tf FROM chunk_terms WHERE term = ?", (term,)
                ):
                    cid = tr["chunk_id"]
                    if cid not in by_id:
                        continue  # ACL 已过滤掉
                    tf = int(tr["tf"])
                    dl = lens[cid]
                    denom = tf + k1 * (1 - b + b * dl / avg_len)
                    scores[cid] = scores.get(cid, 0.0) + idf * (tf * (k1 + 1)) / denom

        ranked = sorted(scores.items(), key=lambda t: -t[1])[: int(top_n)]
        return [
            {**self._row_to_dict(by_id[cid]), "score": float(sc)} for cid, sc in ranked
        ]

    # ------------------------------------------------------------------
    def get_by_id(self, chunk_id: str) -> dict[str, Any] | None:
        """按 ID 取 chunk。

        Args:
            chunk_id: 块 ID。

        Returns:
            chunk dict 或 None。
        """
        r = self.conn.execute(
            "SELECT * FROM chunks WHERE chunk_id = ?", (chunk_id,)
        ).fetchone()
        return self._row_to_dict(r) if r else None

    def get_parent(self, parent_id: str) -> dict[str, Any] | None:
        """按 parent_id 取父块。

        Args:
            parent_id: 父块 ID。

        Returns:
            父块 dict 或 None。
        """
        return self.get_by_id(parent_id)

    def stats(self) -> dict[str, Any]:
        """统计 chunk 数 / 父块数 / token 总量。"""
        r = self.conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN chunk_type='parent' THEN 1 ELSE 0 END) AS parents,
                      SUM(CASE WHEN chunk_type='child'  THEN 1 ELSE 0 END) AS children,
                      SUM(token_count) AS tokens,
                      COUNT(DISTINCT doc_id) AS docs
               FROM chunks"""
        ).fetchone()
        return {
            "backend": "sqlite",
            "path": self.path,
            "total_chunks": int(r["total"] or 0),
            "parent_chunks": int(r["parents"] or 0),
            "child_chunks": int(r["children"] or 0),
            "total_tokens": int(r["tokens"] or 0),
            "doc_count": int(r["docs"] or 0),
        }

    def delete_doc(self, doc_id: str) -> int:
        """删除某文档的全部块（重建索引用）。

        Args:
            doc_id: 文档 ID。

        Returns:
            删除条数。
        """
        ids = [
            r["chunk_id"]
            for r in self.conn.execute(
                "SELECT chunk_id FROM chunks WHERE doc_id = ?", (doc_id,)
            )
        ]
        with self.conn:
            self.conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            for cid in ids:
                self.conn.execute("DELETE FROM chunk_terms WHERE chunk_id = ?", (cid,))
            try:
                self.conn.execute("DELETE FROM doc_usage WHERE doc_id = ?", (doc_id,))
            except Exception:  # noqa: BLE001
                pass
            try:
                self.conn.execute("DELETE FROM doc_source WHERE doc_id = ?", (doc_id,))
            except Exception:  # noqa: BLE001
                pass
        return len(ids)

    # ------------------------------------------------------------------
    def list_documents(self) -> list[dict[str, Any]]:
        """列出已入库文档（按 doc_id 聚合）。

        返回的字段直接对应前端「知识库」列表所需的展示信息：
        标题 / doc_id / 类型 / 密级 / 可见角色 / 版本 / chunk 数 / 体积 / 更新时间 /
        来源 / 被检索命中次数。

        Returns:
            文档列表（按 doc_id 升序）。
        """
        rows = list(self.conn.execute(
            """
            SELECT doc_id,
                   MAX(doc_title)   AS doc_title,
                   MAX(doc_version) AS doc_version,
                   COUNT(*)          AS chunk_count,
                   SUM(CASE WHEN chunk_type='child'  THEN 1 ELSE 0 END) AS child_count,
                   SUM(CASE WHEN chunk_type='parent' THEN 1 ELSE 0 END) AS parent_count,
                   SUM(token_count)  AS token_count,
                   SUM(LENGTH(text))  AS char_count,
                   MAX(acl_level)    AS acl_level,
                   MAX(product_line) AS product_line,
                   MAX(visible_roles) AS visible_roles,
                   MAX(created_at)   AS updated_at
            FROM chunks
            GROUP BY doc_id
            ORDER BY doc_id
            """
        ))
        # 命中次数（表可能为空）
        hits: dict[str, tuple[int, str]] = {}
        try:
            for h in self.conn.execute(
                "SELECT doc_id, hit_count, last_hit_at FROM doc_usage"
            ):
                hits[h["doc_id"]] = (int(h["hit_count"] or 0), h["last_hit_at"] or "")
        except Exception:  # noqa: BLE001
            hits = {}

        # 来源信息（doc_source 表）：区分「目录来源」与「UI 上传来源」
        origins: dict[str, tuple[str, str]] = {}
        try:
            for s in self.conn.execute(
                "SELECT doc_id, origin, stored_path FROM doc_source"
            ):
                origins[s["doc_id"]] = (
                    s["origin"] or "upload",
                    s["stored_path"] or "",
                )
        except Exception:  # noqa: BLE001
            origins = {}

        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                roles = json.loads(r["visible_roles"]) if r["visible_roles"] else ["*"]
            except Exception:
                roles = ["*"]
            if not isinstance(roles, list):
                roles = [str(roles)]
            meta = self._doc_metadata(r["doc_id"])
            hit, last_hit = hits.get(r["doc_id"], (0, ""))
            # product_line 可能是字符串 "null"（YAML 里写 `null` 被解析成字符串）
            pl = (r["product_line"] or "").strip()
            if pl.lower() in ("null", "none", ""):
                pl = ""
            origin, stored_path = origins.get(r["doc_id"], ("", ""))
            if not origin:
                # 没有 doc_source 记录：按 metadata.source 推断（历史数据兜底）
                origin = "directory" if meta.get("origin") == "directory" else "upload"
            out.append({
                "doc_id": r["doc_id"],
                "title": r["doc_title"] or r["doc_id"],
                "doc_type": meta.get("doc_type") or "document",
                "version": r["doc_version"] or "",
                "owner": meta.get("owner") or "",
                "security_level": r["acl_level"] or "internal",
                "product_line": pl,
                "visible_roles": roles,
                "chunk_count": int(r["chunk_count"] or 0),
                "child_count": int(r["child_count"] or 0),
                "parent_count": int(r["parent_count"] or 0),
                "token_count": int(r["token_count"] or 0),
                "size_bytes": int(r["char_count"] or 0),
                "source": meta.get("source") or f"{r['doc_id']}.md",
                "origin": origin,
                "stored_path": stored_path,
                "updated_at": r["updated_at"] or "",
                "hit_count": hit,
                "last_hit_at": last_hit,
            })
        return out

    def _doc_metadata(self, doc_id: str) -> dict[str, Any]:
        """取某文档任一 chunk 的 metadata（文档级字段都一致，取最新即可）。

        Args:
            doc_id: 文档 ID。

        Returns:
            metadata 字典（解析失败返回空 dict）。
        """
        row = self.conn.execute(
            "SELECT metadata FROM chunks WHERE doc_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (doc_id,),
        ).fetchone()
        if not row:
            return {}
        try:
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
        except Exception:
            return {}
        return meta if isinstance(meta, dict) else {}

    # ------------------------------------------------------------------
    def record_hits(self, doc_ids: Iterable[str]) -> None:
        """记录文档被检索命中的次数（真实发生才记，不臆造）。

        Args:
            doc_ids: 本次命中的文档 ID 集合（去重后写入）。
        """
        ids = sorted({str(d) for d in (doc_ids or []) if d})
        if not ids:
            return
        try:
            with self.conn:
                for did in ids:
                    self.conn.execute(
                        "INSERT INTO doc_usage(doc_id, hit_count, last_hit_at) "
                        "VALUES (?, 1, datetime('now')) "
                        "ON CONFLICT(doc_id) DO UPDATE SET "
                        "hit_count = hit_count + 1, last_hit_at = datetime('now')",
                        (did,),
                    )
        except Exception as exc:  # noqa: BLE001
            logger.debug("记录文档命中次数失败：%s", exc)

    # ------------------------------------------------------------------
    def save_source(
        self,
        doc_id: str,
        *,
        content: str,
        title: str = "",
        source: str = "",
        meta: dict[str, Any] | None = None,
        origin: str = "upload",
        stored_path: str = "",
    ) -> None:
        """留存文档原文（供「单文档重建索引」与「全量重建」使用）。

        **原文存储策略**：全文存储，不做截断。理由：
            * SQLite 单行可存到 GB 级，20 MB 上限的上传文件毫无压力；
            * 截断会导致重建时正文不完整 → 检索静默丢内容，比体积更危险；
            * 存全文才能保证「重建结果 == 导入结果」（幂等的前提）。

        Args:
            doc_id: 文档 ID。
            content: 文档正文（**原文全文**）。
            title: 标题。
            source: 来源标识。
            meta: 附加元数据（JSON 可序列化）。
            origin: 来源类型（``upload`` / ``directory``）。
            stored_path: 原文物理存储位置（目录来源为相对路径；上传来源为空）。
        """
        if not doc_id:
            return
        with self.conn:
            self.conn.execute(
                "INSERT INTO doc_source"
                "(doc_id, title, source, content, meta, origin, stored_path, "
                " char_count, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,datetime('now')) "
                "ON CONFLICT(doc_id) DO UPDATE SET "
                "title=excluded.title, source=excluded.source, "
                "content=excluded.content, meta=excluded.meta, "
                "origin=excluded.origin, stored_path=excluded.stored_path, "
                "char_count=excluded.char_count, updated_at=datetime('now')",
                (doc_id, title or "", source or "", content or "",
                 json.dumps(meta or {}, ensure_ascii=False),
                 origin or "upload", stored_path or "", len(content or "")),
            )

    def get_source(self, doc_id: str) -> dict[str, Any] | None:
        """取留存���文档原文。

        Args:
            doc_id: 文档 ID。

        Returns:
            ``{"doc_id","title","source","content","meta"}``；不存在返回 None。
        """
        r = self.conn.execute(
            "SELECT doc_id, title, source, content, meta, origin, stored_path, "
            "char_count FROM doc_source WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
        if not r:
            return None
        try:
            meta = json.loads(r["meta"]) if r["meta"] else {}
        except Exception:
            meta = {}
        return {
            "doc_id": r["doc_id"],
            "title": r["title"] or "",
            "source": r["source"] or "",
            "content": r["content"] or "",
            "meta": meta if isinstance(meta, dict) else {},
            "origin": r["origin"] or "upload",
            "stored_path": r["stored_path"] or "",
            "char_count": int(r["char_count"] or 0),
        }

    def list_sources(self) -> list[dict[str, Any]]:
        """列出 ``doc_source`` 表中的全部文档原文记录。

        Returns:
            记录列表（字段同 :meth:`get_source`），按 doc_id 升序。
        """
        rows = list(self.conn.execute("SELECT doc_id FROM doc_source ORDER BY doc_id"))
        out: list[dict[str, Any]] = []
        for r in rows:
            item = self.get_source(r["doc_id"])
            if item:
                out.append(item)
        return out

    def mark_deleted(
        self, doc_id: str, reason: str = "", origin: str = ""
    ) -> None:
        """写墓碑：标记某文档已被用户显式删除。

        作用：全量重建时据此**跳过**该 doc_id，避免「删了又被 UI 导入的
        原文复活」（目录来源是例外，见 indexer 模块 docstring）。

        Args:
            doc_id: 文档 ID。
            reason: 删除原因（仅供审计）。
            origin: 被删文档的来源（``upload`` / ``directory``）。
        """
        if not doc_id:
            return
        with self.conn:
            self.conn.execute(
                "INSERT INTO doc_tombstone(doc_id, deleted_at, reason, origin) "
                "VALUES (?, datetime('now'), ?, ?) "
                "ON CONFLICT(doc_id) DO UPDATE SET "
                "deleted_at=datetime('now'), reason=excluded.reason, "
                "origin=excluded.origin",
                (doc_id, reason or "", origin or ""),
            )

    def list_tombstones(self) -> list[str]:
        """列出所有墓碑 doc_id（用户显式删除过的文档）。"""
        try:
            return [
                r["doc_id"]
                for r in self.conn.execute("SELECT doc_id FROM doc_tombstone")
            ]
        except Exception:  # noqa: BLE001
            return []

    def unmark_deleted(self, doc_id: str) -> None:
        """清除墓碑（文档被重新导入时调用）。"""
        if not doc_id:
            return
        try:
            with self.conn:
                self.conn.execute(
                    "DELETE FROM doc_tombstone WHERE doc_id = ?", (doc_id,)
                )
        except Exception:  # noqa: BLE001
            pass

    def clear(
        self,
        *,
        keep_sources: bool = False,
        keep_tombstones: bool = True,
        keep_usage: bool = True,
    ) -> None:
        """清空索引数据（``--rebuild`` 用）。

        Args:
            keep_sources: 是否保留 ``doc_source``（原文留存）表。
                **重建时必须保留**，否则 UI 导入的文档会永久失去原文而无法重建。
            keep_tombstones: 是否保留删除墓碑（默认保留，保证「删了不复活」）。
            keep_usage: 是否保留命中次数统计（按 doc_id 聚合，重建后仍有效）。
        """
        with self.conn:
            self.conn.execute("DELETE FROM chunks")
            self.conn.execute("DELETE FROM chunk_terms")
            if not keep_usage:
                try:
                    self.conn.execute("DELETE FROM doc_usage")
                except Exception:  # noqa: BLE001
                    pass
            if not keep_sources:
                try:
                    self.conn.execute("DELETE FROM doc_source")
                except Exception:  # noqa: BLE001
                    pass
            if not keep_tombstones:
                try:
                    self.conn.execute("DELETE FROM doc_tombstone")
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    def _row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
        """sqlite 行 → chunk dict。

        Args:
            r: 行。

        Returns:
            chunk dict。
        """
        try:
            meta = json.loads(r["metadata"]) if r["metadata"] else {}
        except Exception:
            meta = {}
        try:
            roles = json.loads(r["visible_roles"]) if r["visible_roles"] else ["*"]
        except Exception:
            roles = ["*"]
        meta.setdefault("acl", {})
        if isinstance(meta["acl"], dict):
            meta["acl"].setdefault("security_level", r["acl_level"])
            meta["acl"].setdefault("product_line", r["product_line"])
            meta["acl"].setdefault("visible_roles", roles)
        return {
            "chunk_id": r["chunk_id"],
            "doc_id": r["doc_id"],
            "doc_title": r["doc_title"],
            "doc_version": r["doc_version"],
            "chunk_type": r["chunk_type"],
            "parent_id": r["parent_id"],
            "text": r["text"],
            "token_count": int(r["token_count"] or 0),
            "metadata": meta,
            "ordinal": int(meta.get("ordinal") or 0),
        }

    def close(self) -> None:
        """关闭连接。"""
        try:
            self.conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# PostgreSQL + pgvector 实现
# ---------------------------------------------------------------------------
class PgChunkStore(ChunkStore):
    """PostgreSQL + pgvector 存储实现。

    对应 docs/01 §2.7「向量库」备选方案 B：PG + pgvector（运维最简，M1 规模足够）。

    **构造时若 PG 不可用会抛出清晰异常**，由 :func:`build_store` 自动降级到
    :class:`SqliteChunkStore`。本机 WSL / Docker 服务 Disabled，禁止假设 PG 可用。

    Args:
        dsn: PostgreSQL 连接串。
        schema_path: ``schema.sql`` 路径（可选，用于自动建表）。
        dimension: 向量维度（须与 schema.sql 的 vector(1024) 一致）。
        chunk_types: 参与检索的块类型。

    Raises:
        RuntimeError: 缺少 psycopg 或无法连接 PG 时抛出。
    """

    def __init__(
        self,
        dsn: str,
        schema_path: str | None = None,
        dimension: int = 1024,
        chunk_types: Sequence[str] = ("child",),
    ) -> None:
        try:
            import psycopg  # type: ignore
            from psycopg.types.json import Jsonb  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "PgChunkStore 需要 psycopg（psycopg[binary]），当前环境未安装。"
                f" 请改用 SqliteChunkStore 降级路径。原因：{exc}"
            ) from exc
        self._psycopg = psycopg
        self._Jsonb = Jsonb
        self.dsn = dsn
        self.dimension = int(dimension)
        self.chunk_types = tuple(chunk_types)
        try:
            self.conn = psycopg.connect(dsn, autocommit=False)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"无法连接 PostgreSQL（DSN 已脱敏）：{_mask_dsn(dsn)}。"
                " 请确认 PG 服务可用，或使用 --store sqlite 降级路径。原因：" + str(exc)
            ) from exc
        if schema_path and os.path.isfile(schema_path):
            self._apply_schema(schema_path)

    def _apply_schema(self, schema_path: str) -> None:
        """执行 schema.sql 建表。

        Args:
            schema_path: SQL 文件路径。
        """
        with open(schema_path, "r", encoding="utf-8") as f:
            sql = f.read()
        with self.conn.cursor() as cur:
            cur.execute(sql)
        self.conn.commit()

    # ------------------------------------------------------------------
    def upsert(self, chunks: Sequence[dict[str, Any]]) -> int:
        """批量 upsert。

        Args:
            chunks: chunk 列表。

        Returns:
            写入条数。
        """
        if not chunks:
            return 0
        n = 0
        with self.conn.cursor() as cur:
            for c in chunks:
                meta = dict(c.get("metadata") or {})
                acl = dict(meta.get("acl") or {})
                level = acl.get("security_level") or meta.get("security_level") or "internal"
                product_line = acl.get("product_line") or meta.get("product_line")
                roles = acl.get("visible_roles") or meta.get("visible_roles") or ["*"]
                if isinstance(roles, str):
                    roles = [roles]
                emb = c.get("embedding")
                emb_str = None
                if emb is not None:
                    try:
                        emb_str = "[" + ",".join(f"{float(x):.6f}" for x in emb) + "]"
                    except Exception:
                        emb_str = None
                cur.execute(
                    """INSERT INTO doc_chunk
                       (chunk_id, doc_id, doc_title, doc_version, chunk_type, parent_id,
                        text, token_count, metadata, security_level, product_line,
                        visible_roles, embedding)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector)
                       ON CONFLICT (chunk_id) DO UPDATE SET
                         doc_title=EXCLUDED.doc_title, doc_version=EXCLUDED.doc_version,
                         chunk_type=EXCLUDED.chunk_type, parent_id=EXCLUDED.parent_id,
                         text=EXCLUDED.text, token_count=EXCLUDED.token_count,
                         metadata=EXCLUDED.metadata, security_level=EXCLUDED.security_level,
                         product_line=EXCLUDED.product_line,
                         visible_roles=EXCLUDED.visible_roles,
                         embedding=EXCLUDED.embedding""",
                    (
                        c["chunk_id"], c.get("doc_id", ""), c.get("doc_title", ""),
                        c.get("doc_version", ""), c.get("chunk_type", "child"),
                        c.get("parent_id"), c.get("text", ""),
                        int(c.get("token_count") or 0), self._Jsonb(meta),
                        level, product_line, list(roles), emb_str,
                    ),
                )
                n += 1
        self.conn.commit()
        return n

    def _acl_clause(self, acl_filter: AclFilter | None) -> tuple[str, list[Any]]:
        """构造 ACL WHERE 片段。

        Args:
            acl_filter: 过滤器。

        Returns:
            ``(sql, params)``。
        """
        if acl_filter is None:
            return "", []
        frags: list[str] = []
        params: list[Any] = []
        max_rank = acl_filter.level_rank(acl_filter.max_security_level)
        if max_rank > 0:
            allowed = [lv for lv, r in AclFilter._LEVEL_ORDER.items() if r <= max_rank]
            frags.append("security_level = ANY(%s)")
            params.append(allowed)
        if acl_filter.product_line:
            frags.append("(product_line IS NULL OR product_line = %s)")
            params.append(acl_filter.product_line)
        if acl_filter.roles:
            frags.append("(visible_roles @> ARRAY['*']::text[] OR visible_roles && %s)")
            params.append(list(acl_filter.roles))
        if acl_filter.doc_ids:
            frags.append("doc_id = ANY(%s)")
            params.append(list(acl_filter.doc_ids))
        if not frags:
            return "", []
        return " AND " + " AND ".join(frags), params

    def search_vector(
        self,
        query_vec: Any,
        top_n: int = 50,
        acl_filter: AclFilter | None = None,
    ) -> list[dict[str, Any]]:
        """pgvector 余弦检索（ACL 下推到 WHERE）。

        Args:
            query_vec: 查询向量。
            top_n: 返回条数。
            acl_filter: ACL 过滤器。

        Returns:
            候选列表。
        """
        vec_str = "[" + ",".join(f"{float(x):.6f}" for x in query_vec) + "]"
        acl_sql, acl_params = self._acl_clause(acl_filter)
        sql = f"""
            SELECT chunk_id, doc_id, doc_title, doc_version, chunk_type, parent_id,
                   text, token_count, metadata,
                   1 - (embedding <=> %s::vector) AS score
            FROM doc_chunk
            WHERE embedding IS NOT NULL AND chunk_type = ANY(%s){acl_sql}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, [vec_str, list(self.chunk_types), *acl_params, vec_str, int(top_n)])
            rows = cur.fetchall()
        return [self._row_to_dict(r) for r in rows]

    def search_bm25(
        self,
        query: str,
        top_n: int = 50,
        acl_filter: AclFilter | None = None,
    ) -> list[dict[str, Any]]:
        """PG 侧全文检索。

        说明：生产环境 BM25 通道推荐 Elasticsearch（docs/01 §2.7「全文检索」）。
        此处提供 pg_trgm 模糊匹配的降级实现，供单库部署场景使用。

        Args:
            query: 查询文本。
            top_n: 返回条数。
            acl_filter: ACL 过滤器。

        Returns:
            候选列表。
        """
        acl_sql, acl_params = self._acl_clause(acl_filter)
        sql = f"""
            SELECT chunk_id, doc_id, doc_title, doc_version, chunk_type, parent_id,
                   text, token_count, metadata,
                   similarity(text, %s) AS score
            FROM doc_chunk
            WHERE chunk_type = ANY(%s){acl_sql}
              AND (text %% %s OR text ILIKE %s)
            ORDER BY score DESC
            LIMIT %s
        """
        like = f"%{query}%"
        with self.conn.cursor() as cur:
            cur.execute(sql, [query, list(self.chunk_types), *acl_params, query, like, int(top_n)])
            rows = cur.fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_by_id(self, chunk_id: str) -> dict[str, Any] | None:
        """按 ID 取 chunk。"""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT chunk_id, doc_id, doc_title, doc_version, chunk_type, parent_id,"
                " text, token_count, metadata FROM doc_chunk WHERE chunk_id = %s",
                [chunk_id],
            )
            row = cur.fetchone()
        return self._row_to_dict(row) if row else None

    def get_parent(self, parent_id: str) -> dict[str, Any] | None:
        """按 parent_id 取父块。"""
        return self.get_by_id(parent_id)

    def stats(self) -> dict[str, Any]:
        """统计信息。"""
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) AS total,
                          COUNT(*) FILTER (WHERE chunk_type='parent') AS parents,
                          COUNT(*) FILTER (WHERE chunk_type='child')  AS children,
                          COALESCE(SUM(token_count),0) AS tokens,
                          COUNT(DISTINCT doc_id) AS docs
                   FROM doc_chunk"""
            )
            row = cur.fetchone()
        return {
            "backend": "pgvector",
            "total_chunks": int(row[0] or 0),
            "parent_chunks": int(row[1] or 0),
            "child_chunks": int(row[2] or 0),
            "total_tokens": int(row[3] or 0),
            "doc_count": int(row[4] or 0),
        }

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        """行 → chunk dict。"""
        return {
            "chunk_id": row[0], "doc_id": row[1], "doc_title": row[2],
            "doc_version": row[3], "chunk_type": row[4], "parent_id": row[5],
            "text": row[6], "token_count": int(row[7] or 0),
            "metadata": row[8] if isinstance(row[8], dict) else {},
            "score": float(row[9]) if len(row) > 9 and row[9] is not None else None,
        }

    def close(self) -> None:
        """关闭连接。"""
        try:
            self.conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
def build_store(
    kind: str = "sqlite",
    dsn: str | None = None,
    sqlite_path: Optional[str] = None,
    schema_path: str | None = None,
    dimension: int = 1024,
) -> ChunkStore:
    """构造存储实例，**PG 不可用时自动降级到 SQLite**。

    Args:
        kind: ``sqlite`` 或 ``pg``。
        dsn: PG 连接串（kind=pg 时必需）。
        sqlite_path: SQLite 文件路径；None 时取统一数据目录下的
            ``kb/chunks.db``（可通过 ``AGENT_DATA_DIR`` 整体外置）。
        schema_path: schema.sql 路径（kind=pg 时用于建表）。
        dimension: 向量维度。

    Returns:
        存储实例。
    """
    if sqlite_path is None:
        from agent_platform.core import paths as _paths  # noqa: PLC0415

        sqlite_path = str(_paths.data_path("kb", "chunks.db"))
    if kind == "pg":
        if not dsn:
            logger.warning("未提供 --dsn，自动降级到 SQLite 存储。")
            return SqliteChunkStore(sqlite_path, dimension=dimension)
        try:
            return PgChunkStore(dsn, schema_path=schema_path, dimension=dimension)
        except RuntimeError as exc:
            logger.warning("PG 存储不可用，降级到 SQLite：%s", exc)
            return SqliteChunkStore(sqlite_path, dimension=dimension)
    return SqliteChunkStore(sqlite_path, dimension=dimension)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _term_freq(text: str) -> dict[str, int]:
    """统计 token 频次。

    Args:
        text: 原文。

    Returns:
        ``{token: tf}``。
    """
    tf: dict[str, int] = {}
    for t in tokenize(text):
        tf[t] = tf.get(t, 0) + 1
    return tf


def _token_len(text: str) -> int:
    """估算文档长度（BM25 长度归一用）。

    Args:
        text: 原文。

    Returns:
        token 数。
    """
    return len(tokenize(text))


def struct_pack_float(x: float) -> bytes:
    """float32 打包（numpy 不可用时的 fallback）。

    Args:
        x: 数值。

    Returns:
        4 字节。
    """
    import struct

    return struct.pack("<f", float(x))


def struct_unpack_floats(blob: bytes) -> list[float]:
    """float32 批量解包（numpy 不可用时的 fallback）。

    Args:
        blob: 二进制。

    Returns:
        浮点列表。
    """
    import struct

    n = len(blob) // 4
    return list(struct.unpack("<" + "f" * n, blob[: n * 4]))


def _mask_dsn(dsn: str) -> str:
    """DSN 脱敏（避免密码进日志）。

    Args:
        dsn: 原始 DSN。

    Returns:
        脱敏 DSN。
    """
    return re.sub(r"://([^:/@]+):([^@]*)@", r"://\1:***@", dsn or "")
