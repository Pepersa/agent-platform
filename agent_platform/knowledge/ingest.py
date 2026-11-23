"""文档接入与解析流水线。

对应设计文档：
    docs/01_架构方案_RAG_Agent.md
        §2.7 知识层 —— 「文档接入与解析」（Docling / MinerU 版面与表格还原好）、
          「OCR / 图纸理解」、「表格结构化」
    docs/02_场景方案_STDF分析与测试用例生成.md
        §4.2 文档解析流水线 —— PDF/Word/Excel → 预处理 → 结构化解析 → 语义切块 →
          规格抽取 → 单位归一 → 入库
        §3.5 RAG 设计 —— 各知识源的切分策略

解析降级链（**任何一级失败都不得让模块 import 失败**）：
    1. ``docling`` —— 版面与表格还原（推荐）
    2. ``pypdf``（PDF） / ``python-docx``（Word）—— 纯文本提取
    3. 按 ``\\n\\n`` 切段的极简降级（连 pypdf 都没有时的兜底）

支持的文件类型：``.pdf`` ``.md`` ``.txt`` ``.docx`` ``.html``
"""

from __future__ import annotations

import html
import logging
import os
import re
from typing import Any, Iterable

from agent_platform.knowledge.chunker import split_markdown_sections

__all__ = [
    "ingest_file",
    "ingest_directory",
    "parse_document",
    "read_front_matter",
    "SUPPORTED_SUFFIXES",
    "DEFAULT_ACL",
]

logger = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = (".pdf", ".md", ".markdown", ".txt", ".docx", ".html", ".htm")

# 默认 ACL（docs/01 §2.7「Metadata 与 ACL」：密级 / 产品线 / 可见角色）
DEFAULT_ACL: dict[str, Any] = {
    "security_level": "internal",
    "product_line": None,
    "visible_roles": ["*"],
}


# ---------------------------------------------------------------------------
# 文件 ID / 元数据
# ---------------------------------------------------------------------------
def _doc_id_from_path(path: str) -> str:
    """由文件路径生成 doc_id。

    Args:
        path: 文件路径。

    Returns:
        形如 ``DOC-良率口径定义`` 的稳定 ID。
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    safe = re.sub(r"[^\w一-鿿\-]+", "-", stem).strip("-")
    return f"DOC-{safe}"


def _read_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """解析 markdown YAML front matter（简易实现，不依赖 pyyaml）。

    支持 ``key: value`` 与 ``key: [a, b]`` 两种写法。

    Args:
        text: markdown 原文。

    Returns:
        ``(元数据字典, 去掉 front matter 的正文)``。
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict[str, Any] = {}
    for i in range(1, len(lines)):
        line = lines[i]
        if line.strip() == "---":
            body = "\n".join(lines[i + 1 :])
            return meta, body
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if val.startswith("[") and val.endswith("]"):
            items = [v.strip().strip("'\"") for v in val[1:-1].split(",")]
            meta[key] = [v for v in items if v]
        else:
            meta[key] = val.strip("'\"")
    return meta, "\n".join(lines[1:])


def read_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """对外暴露的 front matter 解析（复用 :func:`_read_front_matter`）。

    Args:
        text: 原文。

    Returns:
        ``(元数据字典, 去掉 front matter 的正文)``。
    """
    return _read_front_matter(text)


# ---------------------------------------------------------------------------
# 各格式解析
# ---------------------------------------------------------------------------
def _parse_markdown(path: str) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """解析 Markdown。

    Args:
        path: 文件路径。

    Returns:
        ``(正文, 章节列表, front matter)``。
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    meta, body = _read_front_matter(raw)
    return body, split_markdown_sections(body), meta


def _parse_txt(path: str) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """解析纯文本（按空行切段）。

    Args:
        path: 文件路径。

    Returns:
        ``(正文, 章节列表, 元数据)``。
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    meta, body = _read_front_matter(raw)
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    sections = [
        {"title_path": [], "level": 0, "body": p} for p in paras
    ]
    return body, sections, meta


def _parse_html(path: str) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """解析 HTML（去标签，保留标题层级）。

    Args:
        path: 文件路径。

    Returns:
        ``(正文, 章节列表, 元数据)``。
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n\n", raw)
    raw = re.sub(r"(?i)<h([1-6])[^>]*>", r"\n\n#### ", raw)
    text = re.sub(r"<[^>]+>", "", raw)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, split_markdown_sections(text), {}


def _parse_pdf_docling(path: str) -> tuple[str, list[dict[str, Any]], dict[str, Any]] | None:
    """用 docling 解析 PDF（版面与表格还原好）。

    Args:
        path: 文件路径。

    Returns:
        ``(正文, 章节列表, 元数据)``；不可用时返回 None。
    """
    try:
        from docling.document_converter import DocumentConverter  # type: ignore
    except Exception as exc:  # noqa: BLE001
        logger.info("docling 不可用（%s），改用 pypdf。", exc)
        return None
    try:
        converter = DocumentConverter()
        result = converter.convert(path)
        text = result.document.export_to_markdown()
        return text, split_markdown_sections(text), {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("docling 解析失败（%s）：%s", path, exc)
        return None


def _parse_pdf_pypdf(path: str) -> tuple[str, list[dict[str, Any]], dict[str, Any]] | None:
    """用 pypdf 解析 PDF（纯文本，按页分段）。

    Args:
        path: 文件路径。

    Returns:
        ``(正文, 章节列表, 元数据)``；不可用时返回 None。
    """
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception as exc:  # noqa: BLE001
        logger.info("pypdf 不可用（%s），使用极简降级解析。", exc)
        return None
    try:
        reader = PdfReader(path)
        sections: list[dict[str, Any]] = []
        texts: list[str] = []
        for idx, page in enumerate(reader.pages, start=1):
            try:
                ptxt = (page.extract_text() or "").strip()
            except Exception:
                ptxt = ""
            texts.append(ptxt)
            sections.append(
                {
                    "title_path": [f"第{idx}页"],
                    "level": 2,
                    "body": ptxt,
                    "page": idx,
                }
            )
        body = "\n\n".join(texts)
        return body, sections, {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("pypdf 解析失败（%s）：%s", path, exc)
        return None


def _parse_docx(path: str) -> tuple[str, list[dict[str, Any]], dict[str, Any]] | None:
    """用 python-docx 解析 Word。

    Args:
        path: 文件路径。

    Returns:
        ``(正文, 章节列表, 元数据)``；不可用时返回 None。
    """
    try:
        import docx  # type: ignore
    except Exception as exc:  # noqa: BLE001
        logger.info("python-docx 不可用（%s），使用极简降级解析。", exc)
        return None
    try:
        document = docx.Document(path)
        sections: list[dict[str, Any]] = []
        buf: list[str] = []
        cur_title: list[str] = []
        for para in document.paragraphs:
            text = (para.text or "").strip()
            if not text:
                continue
            style = (para.style.name or "").lower()
            if style.startswith("heading"):
                if buf:
                    sections.append(
                        {"title_path": list(cur_title), "level": 2,
                         "body": "\n\n".join(buf)}
                    )
                    buf = []
                try:
                    lvl = int(style.replace("heading", "").strip() or "1")
                except ValueError:
                    lvl = 1
                cur_title = cur_title[: lvl - 1] + [text]
            else:
                buf.append(text)
        if buf:
            sections.append(
                {"title_path": list(cur_title), "level": 2, "body": "\n\n".join(buf)}
            )
        body = "\n\n".join(s["body"] for s in sections)
        return body, sections, {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("python-docx 解析失败（%s）：%s", path, exc)
        return None


def _parse_fallback(path: str) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """极简降级解析：按 ``\\n\\n`` 切段。

    Args:
        path: 文件路径。

    Returns:
        ``(正文, 章节列表, 元数据)``。
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except Exception:
        raw = ""
    meta, body = _read_front_matter(raw)
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    sections = [{"title_path": [], "level": 0, "body": p} for p in paras]
    return body, sections, meta


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def parse_document(path: str) -> dict[str, Any]:
    """解析单个文档，产出统一中间表示。

    Args:
        path: 文件路径。

    Returns:
        中间表示 dict::

            {"doc_id":..., "title":..., "version":..., "text":...,
             "sections":[{"title_path":[...], "body":..., "page":...}],
             "metadata":{...}, "parser": "docling|pypdf|docx|markdown|txt|html|fallback"}

    Raises:
        ValueError: 文件类型不支持时抛出。
    """
    suffix = os.path.splitext(path)[1].lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(
            f"不支持的文件类型：{suffix}；支持 {SUPPORTED_SUFFIXES}"
        )
    if not os.path.isfile(path):
        raise FileNotFoundError(path)

    parser = "fallback"
    text: Any = ""
    sections: Any = []
    meta: Any = {}
    if suffix in (".md", ".markdown"):
        text, sections, meta = _parse_markdown(path)
        parser = "markdown"
    elif suffix == ".txt":
        text, sections, meta = _parse_txt(path)
        parser = "txt"
    elif suffix in (".html", ".htm"):
        text, sections, meta = _parse_html(path)
        parser = "html"
    elif suffix == ".pdf":
        parsed = _parse_pdf_docling(path)
        if parsed:
            text, sections, meta = parsed
            parser = "docling"
        else:
            parsed = _parse_pdf_pypdf(path)
            if parsed:
                text, sections, meta = parsed
                parser = "pypdf"
            else:
                text, sections, meta = _parse_fallback(path)
                parser = "fallback"
    elif suffix == ".docx":
        parsed = _parse_docx(path)
        if parsed:
            text, sections, meta = parsed
            parser = "docx"
        else:
            text, sections, meta = _parse_fallback(path)
            parser = "fallback"

    return {
        "doc_id": meta.get("doc_id") or _doc_id_from_path(path),
        "title": meta.get("title") or os.path.splitext(os.path.basename(path))[0],
        "version": meta.get("version") or "v1.0",
        "text": text,
        "sections": sections,
        "metadata": meta,
        "parser": parser,
        "source": os.path.basename(path),
    }


def ingest_file(
    path: str,
    doc_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """接入单个文件，产出统一中间表示。

    对应 docs/01 §2.7「文档接入与解析」。

    Args:
        path: 文件路径。
        doc_id: 覆盖自动生成的文档 ID。
        metadata: 附加元数据（含 ``acl``、``doc_type``、``owner`` 等）。

    Returns:
        中间表示 dict（见 :func:`parse_document`）。
    """
    doc = parse_document(path)
    if doc_id:
        doc["doc_id"] = doc_id
    extra = dict(metadata or {})
    acl = dict(extra.pop("acl", None) or {})
    doc_meta = dict(doc.get("metadata") or {})
    doc_meta.update(extra)
    # 归一化 ACL：文档 front matter 允许把 security_level / product_line /
    # visible_roles 直接写在顶层（见 data/docs/*.md），此处统一收敛到
    # ``acl`` 子字典，保证检索前的 ACL 下推过滤能正确生效
    # （docs/01 §2.7「Metadata 与 ACL」/ §3.1 第 2 步）。
    for key in ("security_level", "product_line", "visible_roles"):
        if key in doc_meta and key not in acl:
            acl[key] = doc_meta.pop(key)
    for key, default in DEFAULT_ACL.items():
        acl.setdefault(key, default)
    doc_meta["acl"] = acl
    doc_meta.setdefault("source", doc.get("source"))
    doc["metadata"] = doc_meta
    return doc


def ingest_directory(
    directory: str,
    doc_id_prefix: str | None = None,
    metadata: dict[str, Any] | None = None,
    recursive: bool = True,
) -> list[dict[str, Any]]:
    """批量接入目录下的所有受支持文件。

    对应 docs/01 §2.7「知识库运营」：定时全量重建（M1 简单方案）。

    Args:
        directory: 目录路径。
        doc_id_prefix: 文档 ID 前缀（默认不���）。
        metadata: 附加到每份文档的元数据。
        recursive: 是否递归子目录。

    Returns:
        中间表示列表（解析失败的文档不中断整体流程）。
    """
    out: list[dict[str, Any]] = []
    if not os.path.isdir(directory):
        raise NotADirectoryError(directory)
    for root, _dirs, files in os.walk(directory):
        for fn in sorted(files):
            suffix = os.path.splitext(fn)[1].lower()
            if suffix not in SUPPORTED_SUFFIXES:
                continue
            path = os.path.join(root, fn)
            doc_id = None
            if doc_id_prefix:
                doc_id = f"{doc_id_prefix}-{os.path.splitext(fn)[0]}"
            try:
                out.append(ingest_file(path, doc_id=doc_id, metadata=metadata))
            except Exception as exc:  # noqa: BLE001
                logger.error("文档接入失败：%s（%s）", path, exc)
    return out
