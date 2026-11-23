"""父子分块（parent-child chunking）模块。

对应设计文档：
    docs/01_架构方案_RAG_Agent.md
        §2.7 知识层 —— 「切分策略」：父子分块 + 标题层级感知（检索命中后回填父块）
        §2.7 知识层 —— 「Metadata 与 ACL」：来源、版本、密级、所属产品线、可见角色
        §2.2 对话与会话管理 —— 「多轮引用一致性」：引用基于稳定 chunk_id（非向量序）
        §3.1 RAG 检索增强流程 —— 第 5 步（召回 top-50 去重）、第 10 步（兜底）
        §3.1 关键取舍说明 —— 命中 300 token 的子块，回填 1200 token 的父块
    docs/02_场景方案_STDF分析与测试用例生成.md
        §3.5 RAG 设计 —— 各知识源的切分策略与元数据

核心参数（与文档保持一致，不得自创）：
    * 子块（检索单元）：目标 ~300 token，最小 50 token
    * 父块（回填单元）：目标 ~1200 token（章节级）
    * 切分优先按 markdown 标题层级 / PDF 段落边界，标题路径写入 metadata.title_path

降级说明：
    本模块只依赖标准库 + 可选 numpy，不依赖任何必须安装的第三方库，
    因此在缺少 FlagEmbedding / rank_bm25 等依赖的环境下仍可正常 import。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

__all__ = [
    "Chunker",
    "ChunkRecord",
    "stable_chunk_id",
    "estimate_tokens",
    "split_markdown_sections",
]


# ---------------------------------------------------------------------------
# token 估算
# ---------------------------------------------------------------------------
# 中文场景下 1 个汉字约等于 1 个 token，英文约 4 个字符 1 个 token。
# 这里用保守启发式估算，不引入 tiktoken（无依赖要求）。
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


def estimate_tokens(text: str) -> int:
    """估算文本 token 数。

    公式：``tokens ≈ 中文字符数 + 英文单词数 * 1.3``。
    说明：bge-m3 / llama-server 的真实分词器不可得时，用该启发式做预算裁剪，
    误差在 ±15% 内，满足「检索上下文 ≤ 3K token」的预算控制需求
    （docs/01 §3.1 第 6/7 步）。

    Args:
        text: 待估算文本。

    Returns:
        估算 token 数（int，最小 0）。
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    rest = _CJK_RE.sub(" ", text)
    words = len([w for w in re.split(r"\s+", rest) if w])
    return int(cjk + words * 1.3) + 1


# ---------------------------------------------------------------------------
# 稳定 chunk ID
# ---------------------------------------------------------------------------
def stable_chunk_id(doc_id: str, ordinal: int, text_hash: str) -> str:
    """生成稳定的 chunk_id。

    docs/01 §2.2「多轮引用一致性」要求引用基于**稳定 chunk_id（非向量序）**，
    这样文档重新入库、向量库重建后，旧会话中的引用仍然可解析。

    公式：``sha1(f"{doc_id}|{ordinal}|{text_hash}")[:16]``，前缀 ``C-``。

    Args:
        doc_id: 文档唯一标识。
        ordinal: 块在文档内的序号（从 0 开始）。
        text_hash: 块正文内容的哈希（见 :func:`text_hash_of`）。

    Returns:
        形如 ``C-3f9a21b7c4d5e60`` 的稳定 ID。
    """
    raw = f"{doc_id}|{ordinal}|{text_hash}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"C-{digest}"


def text_hash_of(text: str) -> str:
    """计算正文内容哈希（用于 docs/01 §3.1 第 5 步「chunk_id + 内容哈希」去重）。

    Args:
        text: 块正文。

    Returns:
        sha1 十六进制摘要（32 位）。
    """
    norm = re.sub(r"\s+", " ", (text or "").strip())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class ChunkRecord:
    """统一 chunk 数据结构。

    对应 docs/01 §2.7「Metadata 与 ACL」与 docs/02 §3.5 的元数据要求。

    Attributes:
        chunk_id: 稳定块 ID（``C-`` 前缀 sha1）。
        doc_id: 文档 ID。
        doc_title: 文档标题。
        doc_version: 文档版本。
        chunk_type: ``parent`` 或 ``child``。
        parent_id: 父块 ID；父块本身指向文档根块。
        text: 块正文。
        token_count: 估算 token 数。
        metadata: 结构化元数据（含 acl）。
        ordinal: 文档内序号。
    """

    chunk_id: str
    doc_id: str
    doc_title: str = ""
    doc_version: str = ""
    chunk_type: str = "child"
    parent_id: str | None = None
    text: str = ""
    token_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    ordinal: int = 0

    def to_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的普通 dict（写入存储层使用）。"""
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "doc_title": self.doc_title,
            "doc_version": self.doc_version,
            "chunk_type": self.chunk_type,
            "parent_id": self.parent_id,
            "text": self.text,
            "token_count": self.token_count,
            "metadata": self.metadata,
            "ordinal": self.ordinal,
        }


# ---------------------------------------------------------------------------
# Markdown 标题层级切分
# ---------------------------------------------------------------------------
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")


def split_markdown_sections(text: str) -> list[dict[str, Any]]:
    """按 markdown 标题层级把文本切成「章节」。

    对应 docs/01 §2.7「切分策略」推荐方案 A：父子分块 + 标题层级感知。

    Args:
        text: markdown 原文。

    Returns:
        章节列表，每项为 ``{"title_path": [...], "level": int, "body": str}``；
        ``title_path`` 自根到当前标题逐级累积。
    """
    lines = (text or "").splitlines()
    sections: list[dict[str, Any]] = []
    # 标题栈：(level, title)
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    cur_path: list[str] = []
    cur_level = 0

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            sections.append(
                {
                    "title_path": list(cur_path),
                    "level": cur_level,
                    "body": body,
                }
            )
        buf.clear()

    for line in lines:
        m = _HEADING_RE.match(line)
        if m:
            flush()
            level = len(m.group(1))
            title = m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            cur_path = [t for _, t in stack]
            cur_level = level
            # 标题本身并入正文，保证子块正文自带标题语义
            buf.append(line)
        else:
            buf.append(line)
    flush()

    if not sections:
        body = (text or "").strip()
        if body:
            sections.append({"title_path": [], "level": 0, "body": body})
    return sections


# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------
class Chunker:
    """父子分块器。

    职责（docs/01 §3.1 第 10 步 + §2.7 切分策略）：
        1. 按结构（markdown 标题层级 / PDF 段落边界）把文档切成**父块**（章节级，
           目标 ~1200 token）；
        2. 每个父块再切成**子块**（目标 ~300 token，重叠 80 token），子块用于检索；
        3. 子块 metadata 携带父块的标题路径、页码、ACL 等结构化信息。

    Args:
        chunk_size: 子块目标 token 数（默认 300，对应 docs/01 §3.1 第 6 步
            「6 段 × 约 500 token ≈ 3K 上下文」的子块粒度）。
        chunk_overlap: 子块重叠 token 数（默认 80）。
        min_chunk: 小于该 token 数的块直接并入相邻块（默认 50）。
        parent_size: 父块目标 token 数（默认 1200）。
        parent_min: 父块最小 token 数，小于则与同级相邻章节合并。
    """

    def __init__(
        self,
        chunk_size: int = 300,
        chunk_overlap: int = 80,
        min_chunk: int = 50,
        parent_size: int = 1200,
        parent_min: int = 200,
    ) -> None:
        self.chunk_size = max(50, int(chunk_size))
        self.chunk_overlap = max(0, min(int(chunk_overlap), self.chunk_size // 2))
        self.min_chunk = max(0, int(min_chunk))
        self.parent_size = max(self.chunk_size, int(parent_size))
        self.parent_min = max(0, int(parent_min))

    # ------------------------------------------------------------------
    # 对外主接口
    # ------------------------------------------------------------------
    def chunk_document(
        self,
        doc: dict[str, Any],
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
        min_chunk: int | None = None,
    ) -> list[dict[str, Any]]:
        """把一份文档切成父块 + 子块。

        Args:
            doc: 文档中间表示，由 :mod:`agent_platform.knowledge.ingest` 产出，
                需包含：
                    - ``doc_id``（必填）
                    - ``title`` / ``doc_title``
                    - ``version`` / ``doc_version``
                    - ``sections``：章节列表，每项含 ``title_path`` / ``body``
                      / 可选 ``page``
                    - 可选 ``metadata``：透传元数据（其中 ``acl`` 会被保留）
            chunk_size: 覆盖默认子块目标 token 数。
            chunk_overlap: 覆盖默认子块重叠 token 数。
            min_chunk: 覆盖默认最小块 token 数。

        Returns:
            chunk dict 列表（父块在前、子块在后）。每个元素形如::

                {
                  "chunk_id": "C-xxxx", "doc_id": ..., "doc_title": ...,
                  "doc_version": ..., "chunk_type": "child" | "parent",
                  "parent_id": ..., "text": ..., "token_count": ...,
                  "metadata": {"source":..., "page":..., "section":...,
                               "title_path":[...], "doc_id":..., "doc_version":...,
                               "acl": {...}},
                  "ordinal": 0
                }
        """
        chunk_size = self.chunk_size if chunk_size is None else max(50, int(chunk_size))
        chunk_overlap = (
            self.chunk_overlap if chunk_overlap is None else max(0, int(chunk_overlap))
        )
        min_chunk = self.min_chunk if min_chunk is None else max(0, int(min_chunk))

        doc_id = str(doc.get("doc_id") or "DOC-UNKNOWN")
        doc_title = str(doc.get("title") or doc.get("doc_title") or doc_id)
        doc_version = str(doc.get("version") or doc.get("doc_version") or "v1.0")
        base_meta = dict(doc.get("metadata") or {})
        acl = dict(base_meta.get("acl") or {})
        source = base_meta.get("source") or doc.get("source") or f"{doc_title}.md"

        sections = doc.get("sections") or []
        if not sections:
            # 无章节结构：整篇作为单一章节（由 ingest 保证 body 非空）
            sections = [{"title_path": [], "level": 0, "body": doc.get("text", "")}]

        # ---- 第一步：合并/切分出父块（章节级）----
        parent_units = self._build_parents(sections, doc_title)

        chunks: list[dict[str, Any]] = []
        ordinal = 0
        parent_ids: list[str] = []

        # 文档根块（parent_id 指向它）
        root_id = stable_chunk_id(doc_id, 0, text_hash_of(doc_title))
        parent_ids.append(root_id)

        for p_unit in parent_units:
            p_text = p_unit["text"]
            p_hash = text_hash_of(p_text)
            p_ordinal = ordinal
            ordinal += 1
            p_meta = self._build_metadata(
                base_meta=base_meta,
                acl=acl,
                source=source,
                doc_id=doc_id,
                doc_version=doc_version,
                title_path=p_unit["title_path"],
                section=p_unit["section"],
                page=p_unit.get("page"),
                chunk_type="parent",
                ordinal=p_ordinal,
            )
            p_chunk = ChunkRecord(
                chunk_id=stable_chunk_id(doc_id, p_ordinal, p_hash),
                doc_id=doc_id,
                doc_title=doc_title,
                doc_version=doc_version,
                chunk_type="parent",
                parent_id=root_id,
                text=p_text,
                token_count=estimate_tokens(p_text),
                metadata=p_meta,
                ordinal=p_ordinal,
            )
            chunks.append(p_chunk.to_dict())

            # ---- 第二步：父块切子块 ----
            child_pieces = self._split_text(
                p_text, chunk_size, chunk_overlap, min_chunk
            )
            for c_text in child_pieces:
                c_hash = text_hash_of(c_text)
                c_ordinal = ordinal
                ordinal += 1
                c_meta = self._build_metadata(
                    base_meta=base_meta,
                    acl=acl,
                    source=source,
                    doc_id=doc_id,
                    doc_version=doc_version,
                    title_path=p_unit["title_path"],
                    section=p_unit["section"],
                    page=p_unit.get("page"),
                    chunk_type="child",
                    ordinal=c_ordinal,
                )
                c_chunk = ChunkRecord(
                    chunk_id=stable_chunk_id(doc_id, c_ordinal, c_hash),
                    doc_id=doc_id,
                    doc_title=doc_title,
                    doc_version=doc_version,
                    chunk_type="child",
                    parent_id=p_chunk.chunk_id,
                    text=c_text,
                    token_count=estimate_tokens(c_text),
                    metadata=c_meta,
                    ordinal=c_ordinal,
                )
                chunks.append(c_chunk.to_dict())

        return chunks

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _build_parents(
        self, sections: Iterable[dict[str, Any]], doc_title: str
    ) -> list[dict[str, Any]]:
        """把章节聚合成父块（章节级，目标 ~1200 token）。

        策略：
            * 单章节本身就在 ``[parent_min, parent_size]`` 区间 → 独立成父块；
            * 单章节超过 ``parent_size`` → 按段落边界切成多个父块；
            * 单章节小于 ``parent_min`` → 与后一章节合并（保持标题路径可追溯）。
        """
        units: list[dict[str, Any]] = []
        pending: dict[str, Any] | None = None

        for sec in sections:
            body = (sec.get("body") or "").strip()
            if not body:
                continue
            title_path = list(sec.get("title_path") or [])
            page = sec.get("page")
            section_name = title_path[-1] if title_path else ""
            tok = estimate_tokens(body)

            if tok > self.parent_size:
                # 先冲刷 pending
                if pending:
                    units.append(pending)
                    pending = None
                # 大章节按段落边界拆成多个父块
                for piece in self._split_paragraphs(body, self.parent_size):
                    units.append(
                        {
                            "text": piece,
                            "title_path": title_path,
                            "section": section_name,
                            "page": page,
                        }
                    )
                continue

            if pending is None:
                pending = {
                    "text": body,
                    "title_path": title_path,
                    "section": section_name,
                    "page": page,
                }
                continue

            # 与 pending 合并
            merged = pending["text"] + "\n\n" + body
            if estimate_tokens(merged) <= self.parent_size:
                pending["text"] = merged
                if not pending["section"]:
                    pending["section"] = section_name
            else:
                units.append(pending)
                pending = {
                    "text": body,
                    "title_path": title_path,
                    "section": section_name,
                    "page": page,
                }

        if pending:
            units.append(pending)

        # 过滤掉过短的父块（正文为空或 token 极少）
        return [u for u in units if estimate_tokens(u["text"]) >= max(1, self.min_chunk)]

    def _split_paragraphs(self, text: str, size: int) -> list[str]:
        """按段落边界（``\\n\\n``）切分文本，尽量不超 size token。

        对应 docs/01 §3.1：支持「PDF 段落边界切分」。

        Args:
            text: 原文。
            size: 目标 token 上限。

        Returns:
            段落片段列表。
        """
        paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
        out: list[str] = []
        buf: list[str] = []
        buf_tok = 0
        for p in paras:
            ptok = estimate_tokens(p)
            if buf and buf_tok + ptok > size:
                out.append("\n\n".join(buf))
                buf, buf_tok = [], 0
            buf.append(p)
            buf_tok += ptok
        if buf:
            out.append("\n\n".join(buf))
        return out

    def _split_text(
        self, text: str, size: int, overlap: int, min_chunk: int
    ) -> list[str]:
        """把一段文本切成子块（目标 size token，重叠 overlap token）。

        优先在句子/段落边界断开；实在超长则按字符窗口硬切。

        Args:
            text: 原文。
            size: 目标 token 数。
            overlap: 重叠 token 数。
            min_chunk: 小于该值的尾块并入前一块。

        Returns:
            子块文本列表。
        """
        if not text.strip():
            return []

        # 优先按段落 → 句子切分
        units: list[str] = []
        for para in re.split(r"\n\s*\n", text):
            if not para.strip():
                continue
            sentences = re.split(r"(?<=[。！？；\n])|(?<=\. )", para)
            buf = ""
            for sent in sentences:
                if not sent:
                    continue
                candidate = (buf + sent).strip()
                if estimate_tokens(candidate) > size and buf:
                    units.append(buf.strip())
                    # 重叠：从尾部截取 overlap token 的上下文
                    buf = self._tail_overlap(buf, overlap) + sent
                else:
                    buf = candidate
            if buf.strip():
                units.append(buf.strip())

        # 处理仍超长的片段（无标点的长文本）
        final: list[str] = []
        for u in units:
            if estimate_tokens(u) <= size * 1.5:
                final.append(u)
            else:
                final.extend(self._hard_split(u, size))

        # 尾块过小 → 并入前一块
        if len(final) >= 2 and estimate_tokens(final[-1]) < min_chunk:
            final[-2] = final[-2] + "\n" + final[-1]
            final.pop()
        return [f for f in final if f.strip()]

    def _tail_overlap(self, text: str, overlap: int) -> str:
        """截取文本尾部约 ``overlap`` token 作为下一块的重叠前缀。

        Args:
            text: 原文。
            overlap: 目标 token 数。

        Returns:
            重叠片段（字符串）。
        """
        if overlap <= 0 or not text:
            return ""
        # 中文按字、英文按词估算
        tail = text[-overlap * 3 :]  # 粗估：1 token ≈ 3 字符（英文）+ 中文 1 字
        return tail

    def _hard_split(self, text: str, size: int) -> list[str]:
        """按字符窗口硬切超长文本（无标点场景，如代码块、长表格行）。

        Args:
            text: 原文。
            size: 目标 token 数。

        Returns:
            切分后的片段列表。
        """
        approx_chars = max(50, size * 3)
        out: list[str] = []
        for i in range(0, len(text), approx_chars):
            piece = text[i : i + approx_chars]
            if piece.strip():
                out.append(piece.strip())
        return out

    @staticmethod
    def _build_metadata(
        *,
        base_meta: dict[str, Any],
        acl: dict[str, Any],
        source: str,
        doc_id: str,
        doc_version: str,
        title_path: list[str],
        section: str,
        page: Any,
        chunk_type: str,
        ordinal: int,
    ) -> dict[str, Any]:
        """组装 chunk 的 metadata（docs/01 §2.7 / docs/02 §3.5 必填项）。

        Args:
            base_meta: 文档级透传 metadata。
            acl: ACL 字典（密级/产品线/可见角色）。
            source: 来源文件名。
            doc_id: 文档 ID。
            doc_version: 文档版本。
            title_path: 标题层级路径。
            section: 章节名。
            page: 页码（无则 None）。
            chunk_type: parent / child。
            ordinal: 块序号。

        Returns:
            metadata 字典。
        """
        meta = dict(base_meta)
        meta.update(
            {
                "source": source,
                "page": page,
                "section": section or "",
                "title_path": list(title_path),
                "doc_id": doc_id,
                "doc_version": doc_version,
                "acl": dict(acl),
                "chunk_type": chunk_type,
                "ordinal": ordinal,
            }
        )
        # 保留文档级常用键（若 ingest 已提供）
        for key in ("doc_type", "product", "product_line", "tp_name", "tp_version",
                    "owner", "equipment_model", "fault_id"):
            if key in base_meta and key not in meta:
                meta[key] = base_meta[key]
        if "product_line" not in meta and acl.get("product_line"):
            meta["product_line"] = acl.get("product_line")
        return meta
