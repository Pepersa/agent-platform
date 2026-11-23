# -*- coding: utf-8 -*-
"""知识库管理（用户自助添加 / 删除 / 重建索引）。

对应设计文档：
    docs/01_架构方案_RAG_Agent.md
        §2.7 知识层 —— 「文档接入与解析」「Metadata 与 ACL」
        §3.1 RAG 检索增强流程 —— 第 2 步（检索前按用户权限过滤）

本模块提供 **产品化** 的知识库管理能力（docs/01 的「M1 简单方案」之外补充）：

    * 用户可在 Web 工作台里上传 / 粘贴自己的文档 → 立即切块 + 向量化 + 入库；
    * 新文档的 ``security_level`` / ``visible_roles`` 会被真正写进 chunk 的
      ``acl`` 子字典，检索前的 ACL 下推过滤（``SqliteChunkStore._acl_sql``）
      因而能把它排除在低密级用户之外；
    * 删除文档会**同时删除该 doc_id 的全部 chunk 与 BM25 倒排项**，不会留下
      检索得到却查不到出处的「幽灵块」。

**Embedding 必须是真实的 bge-m3**（``BGE_M3_PATH`` 环境变量指向本地权重）。
若 Embedder 降级到 hash 伪向量，本模块会抛出 ``KB_ADMIN_EMBEDDING_DEGRADED``
错误 —— 宁可明确报错，也不写入语义不可用的伪向量污染索引。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import tempfile
import time
from typing import Any, Iterable, Sequence

from agent_platform.knowledge.chunker import Chunker
from agent_platform.knowledge.embed import Embedder
from agent_platform.knowledge.ingest import (
    SUPPORTED_SUFFIXES,
    parse_document,
    read_front_matter,
)
from agent_platform.knowledge.store import SqliteChunkStore

__all__ = [
    "KBAdmin",
    "KBAdminError",
    "DEFAULT_SQLITE_PATH",
    "MAX_UPLOAD_BYTES",
    "ALLOWED_SUFFIXES",
    "SECURITY_LEVELS",
]

logger = logging.getLogger(__name__)

#: 默认 SQLite 索引路径（与 scripts/build_index.py 保持一致）
#:
#: 注意：这里是**模块导入时**求值的默认值，仅作展示 / 显式传参用。
#: 真正的默认解析在 :meth:`KBAdmin.__init__` 里按需进行（``None`` 哨兵），
#: 以便运行时读取环境变量 ``AGENT_DATA_DIR``。
DEFAULT_SQLITE_PATH = "data/kb/chunks.db"

#: 上传文件大小上限（20 MB）
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

#: 允许上传的扩展名（白名单）
ALLOWED_SUFFIXES = (".md", ".markdown", ".txt", ".pdf", ".docx")

#: 合法密级（与 AclFilter._LEVEL_ORDER 一致）
SECURITY_LEVELS = ("public", "internal", "confidential", "secret")

#: 默认 chunk 参数（与 scripts/build_index.py 一致）
DEFAULT_CHUNK_SIZE = 300
DEFAULT_CHUNK_OVERLAP = 80
DEFAULT_MIN_CHUNK = 50
DEFAULT_PARENT_SIZE = 1200


class KBAdminError(Exception):
    """知识库管理错误（结构化：code / message / retryable / hint）。

    Attributes:
        code: 错误码（如 ``KB_ADMIN_FILE_TOO_LARGE``）。
        message: 面向用户的错误说明。
        retryable: 是否可重试。
        hint: 修复提示。
        status_code: 建议的 HTTP 状态码。
    """

    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        hint: str = "",
        status_code: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.hint = hint
        self.status_code = status_code

    def to_dict(self) -> dict[str, Any]:
        """转成前端可直接消费的错误结构。"""
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "hint": self.hint,
        }


def _normalize_levels(roles: Any) -> list[str]:
    """归一化 visible_roles 为字符串列表。

    Args:
        roles: 原始值（字符串 / 列表 / None）。

    Returns:
        角色列表；空输入返回 ``["*"]``（所有人可见）。
    """
    if roles is None:
        return ["*"]
    if isinstance(roles, str):
        items = [r.strip() for r in re.split(r"[,\s;、]+", roles) if r.strip()]
    elif isinstance(roles, (list, tuple)):
        items = [str(r).strip() for r in roles if str(r).strip()]
    else:
        items = [str(roles).strip()]
    return items or ["*"]


def _gen_doc_id(title: str, text: str) -> str:
    """生成稳定的文档 ID（``DOC-<hash8>``）。

    说明：同一份内容 + 同一标题 → 同一 doc_id（重复导入会覆盖而不是堆积副本）。

    Args:
        title: 文档标题。
        text: 文档正文。

    Returns:
        形如 ``DOC-1a2b3c4d`` 的 ID。
    """
    basis = f"{title}\n{text}".encode("utf-8")
    return "DOC-" + hashlib.sha1(basis).hexdigest()[:8]


def _safe_stem(filename: str) -> str:
    """把文件名清洗成安全 stem（用于临时文件与 doc_id 兜底）。

    Args:
        filename: 原始文件名。

    Returns:
        安全化后的 stem（仅保留字母数字下划线连字符与中文）。
    """
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    safe = re.sub(r"[^\w一-鿿\-]+", "-", stem).strip("-")
    return safe or "document"


class KBAdmin:
    """知识库管理入口（导入 / 列表 / 删除 / 重建索引）。

    Args:
        store_path: SQLite 索引路径。
        embedder: Embedding 实例（None 时按环境自动构造）。
        chunker: 切分器（None 时用默认参数）。
        allow_degraded_embedding: 是否允许降级到 hash 伪向量写入索引
            （**生产必须保持 False**；仅在测试环境可显式打开）。
    """

    def __init__(
        self,
        store_path: str | None = None,
        embedder: Embedder | None = None,
        chunker: Chunker | None = None,
        allow_degraded_embedding: bool = False,
    ) -> None:
        # 默认索引路径走统一数据目录解析（``AGENT_DATA_DIR`` 可整体外置）；
        # 不设环境变量时仍是 ``<repo>/data/kb/chunks.db``（向后兼容）。
        if store_path is None:
            from agent_platform.core import paths as _paths  # noqa: PLC0415

            store_path = str(_paths.data_path("kb", "chunks.db"))
        self.store_path = store_path
        # ★ 复用全局缓存的 Embedder（2026-11-23 修复）：
        #   之前这里自己 ``Embedder()``，于是 **bge-m3 权重被加载两份**
        #   （KBAdmin 一份 + Retriever 一份），每份 ~2 GB 内存、
        #   加载耗时 30~50 秒（本机 bge-m3 走 CPU，因为显存被 llama-server 占满）。
        #   改成走 tools 模块的进程级缓存，两边共用同一个实例。
        if embedder is None:
            from agent_platform.knowledge.tools import _get_cached_embedder  # noqa: PLC0415

            embedder = _get_cached_embedder()
        self.embedder = embedder
        self.chunker = chunker or Chunker(
            chunk_size=DEFAULT_CHUNK_SIZE,
            chunk_overlap=DEFAULT_CHUNK_OVERLAP,
            min_chunk=DEFAULT_MIN_CHUNK,
            parent_size=DEFAULT_PARENT_SIZE,
        )
        self.allow_degraded_embedding = bool(allow_degraded_embedding)
        self.store = SqliteChunkStore(store_path)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _ensure_embedding(self) -> None:
        """确保使用的是真实语义向量（拒绝 hash 伪向量污染索引）。

        Raises:
            KBAdminError: Embedding 已降级。
        """
        if self.allow_degraded_embedding:
            return
        if getattr(self.embedder, "is_degraded", False):
            raise KBAdminError(
                "KB_ADMIN_EMBEDDING_DEGRADED",
                "Embedding 已降级为 hash 伪向量，语义检索不可用，拒绝写入索引。",
                retryable=True,
                hint=(
                    "请设置 BGE_M3_PATH 指向本地 bge-m3 权重目录后重启 API"
                    "（例如 E:\\model\\models\\bge-m3），再重新导入。"
                ),
                status_code=503,
            )

    def _embed(self, texts: Sequence[str]) -> list[Any]:
        """批量向量化。

        Args:
            texts: 文本列表。

        Returns:
            向量列表（numpy 行或 list）。
        """
        matrix = self.embedder.embed(list(texts))
        if hasattr(matrix, "tolist"):
            return list(matrix.tolist())
        return [list(v) for v in matrix]

    @staticmethod
    def _normalize_acl(
        *,
        front_meta: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """合并 front matter 与请求体的 ACL 字段（请求体优先）。

        Args:
            front_meta: 文档 front matter 元数据。
            payload: 请求体显式字段。

        Returns:
            ``{"security_level":..., "product_line":..., "visible_roles":[...]}``。
        """
        def _pick(key: str, default: Any) -> Any:
            if payload.get(key) not in (None, ""):
                return payload.get(key)
            if front_meta.get(key) not in (None, ""):
                return front_meta.get(key)
            return default

        level = str(_pick("security_level", "internal")).strip().lower()
        if level not in SECURITY_LEVELS:
            raise KBAdminError(
                "KB_ADMIN_INVALID_SECURITY_LEVEL",
                f"非法密级：{level}",
                hint=f"合法密级为 {' / '.join(SECURITY_LEVELS)}",
            )
        roles = _normalize_levels(_pick("visible_roles", ["*"]))
        product_line = _pick("product_line", None)
        return {
            "security_level": level,
            "product_line": str(product_line) if product_line else None,
            "visible_roles": roles,
        }

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------
    def list_documents(self) -> list[dict[str, Any]]:
        """列出已入库文档。

        Returns:
            文档列表（见 ``SqliteChunkStore.list_documents``）。
        """
        return self.store.list_documents()

    def stats(self) -> dict[str, Any]:
        """返回索引统计。"""
        return self.store.stats()

    def import_text(
        self,
        text: str,
        *,
        title: str = "",
        doc_id: str = "",
        doc_type: str = "",
        version: str = "",
        owner: str = "",
        security_level: str = "",
        visible_roles: Any = None,
        product_line: str = "",
        source: str = "",
    ) -> dict[str, Any]:
        """导入纯文本（粘贴文本场景）。

        自动解析 front matter（``---`` 包裹的 YAML），请求体字段优先级更高。

        Args:
            text: 文档正文。
            title: 文档标题（缺省用 front matter / 生成）。
            doc_id: 文档 ID（缺省自动生成）。
            doc_type: 文档类型（缺省用 front matter / ``document``）。
            version: 版本号。
            owner: 负责人。
            security_level: 密级。
            visible_roles: 可见角色（列表或逗号分隔字符串）。
            product_line: 产品线。
            source: 来源标识。

        Returns:
            导入结果 dict（见 :meth:`_write_document`）。

        Raises:
            KBAdminError: 参数非法或 embedding 不可用。
        """
        body = (text or "").strip()
        if not body:
            raise KBAdminError(
                "KB_ADMIN_EMPTY_TEXT",
                "文档内容为空",
                hint="请粘贴至少一段正文，或改用「上传文件」方式导入。",
            )
        front_meta, body = read_front_matter(body)
        return self._write_document(
            body=body,
            front_meta=front_meta,
            title=title,
            doc_id=doc_id,
            doc_type=doc_type,
            version=version,
            owner=owner,
            security_level=security_level,
            visible_roles=visible_roles,
            product_line=product_line,
            source=source,
            parser="text",
        )

    def import_file(
        self,
        filename: str,
        content: bytes,
        *,
        title: str = "",
        doc_id: str = "",
        doc_type: str = "",
        version: str = "",
        owner: str = "",
        security_level: str = "",
        visible_roles: Any = None,
        product_line: str = "",
    ) -> dict[str, Any]:
        """导入上传的文件（.md / .txt / .pdf / .docx）。

        Args:
            filename: 原始文件名（决定解析器与来源标识）。
            content: 文件字节内容。
            其余参数同 :meth:`import_text`。

        Returns:
            导入结果 dict。

        Raises:
            KBAdminError: 类型不支持 / 超过大小上限 / 解析失败。
        """
        suffix = os.path.splitext(filename or "")[1].lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise KBAdminError(
                "KB_ADMIN_UNSUPPORTED_TYPE",
                f"不支持的文件类型：{suffix or '(无扩展名)'}",
                hint=f"支持的文件类型：{' / '.join(ALLOWED_SUFFIXES)}",
            )
        if not content:
            raise KBAdminError(
                "KB_ADMIN_EMPTY_FILE",
                "上传的文件内容为空",
                hint="请确认文件没有损坏。",
            )
        if len(content) > MAX_UPLOAD_BYTES:
            raise KBAdminError(
                "KB_ADMIN_FILE_TOO_LARGE",
                f"文件过大：{len(content) / 1024 / 1024:.1f} MB",
                hint=f"单个文件不得超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MB。",
                status_code=413,
            )

        tmp_dir = tempfile.mkdtemp(prefix="kb_upload_")
        tmp_path = os.path.join(tmp_dir, _safe_stem(filename) + suffix)
        try:
            with open(tmp_path, "wb") as fh:
                fh.write(content)
            try:
                parsed = parse_document(tmp_path)
            except Exception as exc:  # noqa: BLE001
                raise KBAdminError(
                    "KB_ADMIN_PARSE_FAILED",
                    f"文档解析失败：{type(exc).__name__}: {exc}",
                    retryable=False,
                    hint=(
                        "PDF 需要 pypdf、Word 需要 python-docx；"
                        "请确认依赖已安装，或改用 Markdown / 纯文本导入。"
                    ),
                    status_code=422,
                ) from exc
            front_meta = dict(parsed.get("metadata") or {})
            body = parsed.get("text") or ""
            if not body.strip():
                raise KBAdminError(
                    "KB_ADMIN_EMPTY_PARSE",
                    "文档解析后没有可入库的正文（可能是扫描件 PDF，无文本层）",
                    hint="请提供带文本层的 PDF，或改用 Markdown / 纯文本导入。",
                    status_code=422,
                )
            return self._write_document(
                body=body,
                front_meta=front_meta,
                title=title,
                doc_id=doc_id,
                doc_type=doc_type,
                version=version,
                owner=owner,
                security_level=security_level,
                visible_roles=visible_roles,
                product_line=product_line,
                source=parsed.get("source") or filename,
                parser=parsed.get("parser") or "unknown",
                sections=parsed.get("sections") or None,
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    def _write_document(
        self,
        *,
        body: str,
        front_meta: dict[str, Any],
        title: str,
        doc_id: str,
        doc_type: str,
        version: str,
        owner: str,
        security_level: str,
        visible_roles: Any,
        product_line: str,
        source: str,
        parser: str,
        sections: list[dict[str, Any]] | None = None,
        origin: str = "upload",
        stored_path: str = "",
    ) -> dict[str, Any]:
        """把一份文档写入索引（切块 → 向量化 → upsert）。

        Args:
            body: 文档正文。
            front_meta: front matter 元数据。
            title / doc_id / doc_type / version / owner: 文档级字段（请求体优先）。
            security_level / visible_roles / product_line: ACL 字段（请求体优先）。
            source: 来源标识。
            parser: 解析器名。
            sections: 已解析的章节列表（缺省按空行切段）。
            origin: 来源类型（``upload`` / ``directory``）。
            stored_path: 原文物理路径（仅目录来源有值）。

        Returns:
            导入结果 dict。
        """
        self._ensure_embedding()

        payload = {
            "security_level": security_level,
            "visible_roles": visible_roles,
            "product_line": product_line,
        }
        acl = self._normalize_acl(front_meta=front_meta, payload=payload)

        doc_title = str(title or front_meta.get("title") or "").strip()
        if not doc_title:
            doc_title = _safe_stem(source or "document")
        doc_id = str(doc_id or front_meta.get("doc_id") or "").strip()
        if not doc_id:
            doc_id = _gen_doc_id(doc_title, body)
        doc_version = str(version or front_meta.get("version") or "v1.0")
        doc_type = str(doc_type or front_meta.get("doc_type") or "document")
        owner = str(owner or front_meta.get("owner") or "")

        if not sections:
            paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
            sections = [{"title_path": [], "level": 0, "body": p} for p in paras]

        meta: dict[str, Any] = {
            "acl": acl,
            "source": source or f"{_safe_stem(doc_title)}.md",
            "doc_type": doc_type,
            "owner": owner,
            "origin": origin,
            "stored_path": stored_path,
        }
        if acl.get("product_line"):
            meta["product_line"] = acl["product_line"]
        # front matter 里其余常用字段透传
        for key in ("product", "tp_name", "tp_version", "equipment_model", "fault_id"):
            if front_meta.get(key) not in (None, ""):
                meta.setdefault(key, front_meta[key])

        doc = {
            "doc_id": doc_id,
            "title": doc_title,
            "version": doc_version,
            "text": body,
            "sections": sections,
            "metadata": meta,
            "source": meta["source"],
        }

        chunks = self.chunker.chunk_document(doc)
        if not chunks:
            # 正文过短（不足 min_chunk）时用更小的阈值再试一次，
            # 保证「用户随手粘一小段文字」也能入库（而不是直接报错）
            try:
                loose = Chunker(
                    chunk_size=DEFAULT_CHUNK_SIZE,
                    chunk_overlap=DEFAULT_CHUNK_OVERLAP,
                    min_chunk=8,
                    parent_size=DEFAULT_PARENT_SIZE,
                )
                chunks = loose.chunk_document(doc)
            except Exception:  # noqa: BLE001
                chunks = []
        if not chunks:
            raise KBAdminError(
                "KB_ADMIN_NO_CHUNKS",
                "文档切块结果为空（正文可能过短）",
                hint="请补充正文内容后重试。",
                status_code=422,
            )

        vectors = self._embed([c["text"] for c in chunks])
        for c, v in zip(chunks, vectors):
            c["embedding"] = v

        # 先清掉旧块，保证重复导入是「覆盖」而不是「叠加」
        self.store.delete_doc(doc_id)
        n = self.store.upsert(chunks)

        # 留存原文 → 之后才能做「单文档重建索引」与「全量重建」
        # 说明：这里存的是**原文全文**（不截断），全量重建时据此重新切块。
        try:
            self.store.save_source(
                doc_id,
                content=body,
                title=doc_title,
                source=meta["source"],
                meta={
                    "doc_type": doc_type,
                    "version": doc_version,
                    "owner": owner,
                    "acl": acl,
                    "sections": sections or [],
                },
                origin=origin,
                stored_path=stored_path,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("留存文档原文失败（不影响入库）：%s", exc)

        # 重新导入 = 撤销此前的「已删除」墓碑
        try:
            self.store.unmark_deleted(doc_id)
        except Exception:  # noqa: BLE001
            pass

        children = sum(1 for c in chunks if c.get("chunk_type") == "child")
        return {
            "ok": True,
            "doc_id": doc_id,
            "title": doc_title,
            "doc_type": doc_type,
            "version": doc_version,
            "owner": owner,
            "source": meta["source"],
            "security_level": acl["security_level"],
            "visible_roles": acl["visible_roles"],
            "product_line": acl["product_line"] or "",
            "chunk_count": n,
            "child_count": children,
            "parent_count": n - children,
            "token_count": sum(int(c.get("token_count") or 0) for c in chunks),
            "parser": parser,
            "embedding_backend": getattr(self.embedder, "backend", "?"),
            "updated_at": time.time(),
        }

    # ------------------------------------------------------------------
    def delete_document(self, doc_id: str) -> dict[str, Any]:
        """删除文档并从索引中移除其全部 chunk。

        同时写一条**删除墓碑**（``doc_tombstone`` 表）：即使磁盘目录里还有
        同名文件，全量重建也**不会**把该文档复活（只有用户重新导入才会清除墓碑）。

        Args:
            doc_id: 文档 ID。

        Returns:
            ``{"ok": True, "doc_id": ..., "deleted_chunks": n}``。

        Raises:
            KBAdminError: 文档不存在。
        """
        doc_id = (doc_id or "").strip()
        if not doc_id:
            raise KBAdminError("KB_ADMIN_INVALID_DOC_ID", "doc_id 不能为空")
        existing = [d["doc_id"] for d in self.store.list_documents()]
        if doc_id not in existing:
            raise KBAdminError(
                "KB_ADMIN_NOT_FOUND",
                f"文档不存在：{doc_id}",
                hint="请先 GET /kb/documents 查看现有文档列表。",
                status_code=404,
            )
        # 先取来源（在 delete_doc 抹掉 doc_source 之前）
        origin = ""
        try:
            src = self.store.get_source(doc_id)
            origin = (src or {}).get("origin") or ""
        except Exception:  # noqa: BLE001
            pass
        if not origin:
            try:
                docs_map = {d["doc_id"]: d for d in self.store.list_documents()}
                origin = docs_map.get(doc_id, {}).get("origin") or ""
            except Exception:  # noqa: BLE001
                origin = ""

        n = self.store.delete_doc(doc_id)
        # 墓碑：防止 rebuild 时把该文档复活。
        # 语义：
        #   * origin == 'upload'（UI 导入）→ 用户显式删除即**永久删除**，
        #     重建不会复活（即使目录里出现了同名 doc_id 的文件）；
        #   * origin == 'directory'（目录来源）→ 磁盘文件才是权威，
        #     删除只移除索引条目，重建时若目录仍有该文件则重新接入
        #     （产品约定的例外）。
        try:
            self.store.mark_deleted(doc_id, reason="user_delete", origin=origin)
        except Exception as exc:  # noqa: BLE001
            logger.warning("写删除墓碑失败：%s", exc)
        return {"ok": True, "doc_id": doc_id, "deleted_chunks": n}

    def reindex_document(self, doc_id: str) -> dict[str, Any]:
        """重建某文档的 chunk 并重新向量化。

        正文来源优先级：
            1. 导入时留存的原文（``doc_source`` 表）—— **首选**；
            2. 磁盘上仍存在的原始文件（``data/docs`` / ``data/kb/uploads``）。

        注意：重建**保留**该文档的来源标识（``origin``）与 ACL，
        不会把「目录文档」误标成「上传文档」，也不会覆盖用户设置的密级。

        Args:
            doc_id: 文档 ID。

        Returns:
            重建结果 dict。

        Raises:
            KBAdminError: 文档不存在或找不到原始正文。
        """
        doc_id = (doc_id or "").strip()
        docs = {d["doc_id"]: d for d in self.store.list_documents()}
        if doc_id not in docs:
            raise KBAdminError(
                "KB_ADMIN_NOT_FOUND",
                f"文档不存在：{doc_id}",
                status_code=404,
            )
        info = docs[doc_id]

        text = ""
        saved = None
        try:
            saved = self.store.get_source(doc_id)
        except Exception:  # noqa: BLE001
            saved = None
        if saved and (saved.get("content") or "").strip():
            text = saved["content"]
        else:
            text = self._load_original_text(doc_id, info.get("source") or "")
        if not text:
            raise KBAdminError(
                "KB_ADMIN_NO_SOURCE",
                f"找不到文档「{doc_id}」的原始正文（来源：{info.get('source') or '未知'}），无法重建。",
                hint="请删除该文档后重新导入（上传文件或粘贴文本）。",
                status_code=409,
            )
        # 保留来源标识与元信息（origin / stored_path / sections）
        saved_meta = dict((saved or {}).get("meta") or {})
        origin = (saved or {}).get("origin") or info.get("origin") or "upload"
        stored_path = (saved or {}).get("stored_path") or info.get("stored_path") or ""
        return self._write_document(
            body=text,
            front_meta={},
            title=info.get("title") or "",
            doc_id=doc_id,
            doc_type=info.get("doc_type") or "",
            version=info.get("version") or "",
            owner=info.get("owner") or "",
            security_level=info.get("security_level") or "",
            visible_roles=info.get("visible_roles") or ["*"],
            product_line=info.get("product_line") or "",
            source=info.get("source") or "",
            parser="reindex",
            sections=saved_meta.get("sections") or None,
            origin=origin,
            stored_path=stored_path,
        )

    def _load_original_text(self, doc_id: str, source: str) -> str:
        """从已知目录里找回文档的原始正文。

        Args:
            doc_id: 文档 ID。
            source: 来源文件名。

        Returns:
            正文文本；找不到返回空串。
        """
        from agent_platform.core import paths as _paths  # noqa: PLC0415

        search_dirs = [
            str(_paths.data_path("docs")),
            str(_paths.data_path("kb", "uploads")),
        ]
        names = [source, f"{doc_id}.md", f"{doc_id}.txt"]
        for d in search_dirs:
            for name in names:
                if not name:
                    continue
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    try:
                        parsed = parse_document(p)
                        return parsed.get("text") or ""
                    except Exception:  # noqa: BLE001
                        continue
        return ""

    def reindex_all(
        self,
        docs_dir: str | None = None,
        *,
        include_uploads: bool = True,
        progress: Any = None,
    ) -> dict[str, Any]:
        """全量重建索引（**目录来源 + UI 导入来源** 双数据源）。

        与 :mod:`agent_platform.knowledge.indexer` 的共享实现完全一致：
        ``scripts/build_index.py --rebuild`` 与 ``POST /kb/reindex`` 调的是
        同一份代码，不会再出现「一边有 UI 文档、一边没有」的漂移。

        Args:
            docs_dir: 文档目录（缺省 ``data/docs``）。
            include_uploads: 是否纳入 ``doc_source`` 表（UI 导入）来源。
            progress: 进度回调。

        Returns:
            重建统计 dict。

        Raises:
            KBAdminError: 没有可索引的文档 / 目录不存在。
        """
        from agent_platform.knowledge.indexer import (  # noqa: PLC0415
            IndexBuildError,
            build_index,
        )

        from agent_platform.core import paths as _paths  # noqa: PLC0415

        directory = docs_dir or str(_paths.data_path("docs"))
        if not os.path.isdir(directory):
            raise KBAdminError(
                "KB_ADMIN_NO_DOCS_DIR",
                f"文档目录不存在：{directory}",
                hint="请指定 --docs-dir 或先放入示例文档。",
                status_code=404,
            )
        self._ensure_embedding()
        try:
            result = build_index(
                self.store,
                self.embedder,
                self.chunker,
                docs_dir=directory,
                include_uploads=include_uploads,
                rebuild=True,
                progress=progress,
            )
        except IndexBuildError as exc:
            raise KBAdminError(
                "KB_ADMIN_NO_DOCS",
                str(exc),
                hint="支持 .pdf / .md / .txt / .docx / .html；"
                     "也可以在 Web 工作台里上传 / 粘贴文档。",
                status_code=404,
            ) from exc
        # 去掉内部字段（chunks 太大，不适合直接返回给前端）
        result.pop("chunks", None)
        result.pop("docs", None)
        return result

    def close(self) -> None:
        """关闭底层存储连接。"""
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass


def iter_doc_ids(documents: Iterable[dict[str, Any]]) -> list[str]:
    """从文档列表提取 doc_id 列表。

    Args:
        documents: 文档 dict 列表。

    Returns:
        doc_id 列表。
    """
    return [str(d.get("doc_id")) for d in documents if d.get("doc_id")]


#: 允许的上传后缀（对外暴露，便于 API 层做白名单校验）
SUPPORTED_UPLOAD_SUFFIXES = ALLOWED_SUFFIXES

#: 兼容 ingest 层的完整后缀集合（解析器支持但**上传接口不放行** .html/.htm，
#: 避免 HTML 上传引入脚本注入面）
INGEST_SUPPORTED_SUFFIXES = SUPPORTED_SUFFIXES
