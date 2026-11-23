# -*- coding: utf-8 -*-
"""引用标记（citation）的统一解析、归一化与校验。

对应设计文档：
    《01_架构方案_RAG_Agent.md》§3.1 第 8 步（生成必须附引用）
        与第 9 步（引用块存在性校验）
    《02_场景方案_STDF分析与测试用例生成.md》§6.4（引用可溯源率 / 引用幻觉率）

**为什么独立成模块**：
    引用格式在项目里有两条生成路径，且历史上不兼容：

        1. 检索生成路径（``knowledge/retriever.py`` 的 ``_generate``）
           → 产出 ``[doc_id:chunk_id]``
        2. Agent ReAct 路径（``orchestration/graph.py`` + system prompt 规则 3）
           → 产出 ``[来源:chunk_id]``

    早期两处各自写正则、且只认其中一种格式，导致：
        · Agent 路径产出的引用**从未被校验**（引用幻觉率恒为 0，是假绿）
        · 模型输出 Unicode 变体连字符（U+2011 ``‑``）时，
          合法引用被整条漏掉，误判为「无引用」

    本模块提供唯一实现，两条路径共用。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Sequence

__all__ = [
    "CITATION_RE",
    "CITE_LABELS",
    "normalize_cite_token",
    "extract_citations",
    "validate_citations",
    "has_citation",
]


#: Unicode 变体连字符集合。推理型模型常把这些字符混用输出，
#: 若不归一化，chunk_id 无法与检索结果里的真实 chunk_id 对上，
#: 合法引用会被误判为「引用了不存在的块」。
CITE_DASHES = "‐‑‒–—−－"

#: 中文/英文引用标签。出现时 ``[标签:chunk_id]`` 的左侧不是 doc_id。
CITE_LABELS: tuple[str, ...] = (
    "来源", "出处", "参考", "引用", "source", "ref", "citation",
)

#: 引用标记正则。兼容：
#:   · 三种括号：ASCII ``[]`` / 全角 ``【】`` / 中文 ``［］``
#:   · 全角冒号 ``：``
#:   · Unicode 变体连字符
#:   · 两种格式：``[doc_id:chunk_id]`` 与 ``[来源:chunk_id]``
CITATION_RE = re.compile(
    r"[\[［【]\s*"
    r"(?P<doc>[^:：\[\]［］【】\n]{0,60}?)\s*[:：]\s*"
    r"(?P<chunk>[A-Za-z0-9_\-‐‑‒–—−－]{2,})"
    r"\s*[\]］】]"
)


def normalize_cite_token(token: str) -> str:
    """归一化引用标记中的标识符。

    把 Unicode 变体连字符统一成 ASCII ``-``，这样 ``C‑10231``
    （U+2011）与 ``C-10231`` 归一为同一个 chunk_id。

    Args:
        token: 原始标识符。

    Returns:
        归一化后的标识符。
    """
    if not token:
        return token
    return "".join("-" if ch in CITE_DASHES else ch for ch in token)


def extract_citations(answer_text: str) -> List[Dict[str, str]]:
    """抽取答案中的引用标记。

    支持两种格式：
        * ``[doc_id:chunk_id]`` —— 检索生成路径
        * ``[来源:chunk_id]``   —— Agent ReAct 路径（左侧为标签）

    Args:
        answer_text: 答案文本。

    Returns:
        ``[{"doc_id":..., "chunk_id":..., "raw": "..."}]``，已去重。
        标签形式的 ``doc_id`` 为空串。
    """
    out: List[Dict[str, str]] = []
    seen: set[str] = set()
    for m in CITATION_RE.finditer(answer_text or ""):
        left = normalize_cite_token(m.group("doc").strip())
        chunk_id = normalize_cite_token(m.group("chunk"))
        if not left or left.lower() in CITE_LABELS:
            doc_id = ""
        else:
            doc_id = left
        key = f"{doc_id}:{chunk_id}"
        if key in seen:
            continue
        seen.add(key)
        raw = f"[{doc_id}:{chunk_id}]" if doc_id else f"[来源:{chunk_id}]"
        out.append({"doc_id": doc_id, "chunk_id": chunk_id, "raw": raw})
    return out


def validate_citations(
    citations: Sequence[Dict[str, str]], valid_chunk_ids: Iterable[str]
) -> List[Dict[str, str]]:
    """校验引用块存在性（docs/01 §3.1 第 9 步）。

    Args:
        citations: 抽取到的引用。
        valid_chunk_ids: 本次可用（召回命中）的 chunk_id 集合。

    Returns:
        非法引用列表（引用了不存在的 chunk）。
    """
    valid = set(valid_chunk_ids or ())
    return [c for c in citations if c["chunk_id"] not in valid]


def has_citation(text: str) -> bool:
    """判断文本中是否含引用标记。

    Args:
        text: 待检查文本。

    Returns:
        是否含引用标记。
    """
    return bool(CITATION_RE.search(text or ""))


def collect_chunk_ids(payload: Any) -> List[str]:
    """从工具返回结果里收集 chunk_id。

    工具（``kb_search`` / ``kb_read`` / ``spec_extract`` 等）返回的结构里，
    chunk 可能出现在 ``chunks[].chunk_id``、``hits[].chunk_id``、
    ``items[].chunk_id``、``params[].source.chunk_id`` 等位置。
    这里做一次宽松遍历，把所有能识别到的 chunk_id 收齐，
    供引用存在性校验使用。

    Args:
        payload: 工具返回的任意可 JSON 化结构。

    Returns:
        去重后的 chunk_id 列表。
    """
    found: List[str] = []
    seen: set[str] = set()

    def _walk(node: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(node, dict):
            cid = node.get("chunk_id")
            if isinstance(cid, str) and cid:
                if cid not in seen:
                    seen.add(cid)
                    found.append(cid)
            for v in node.values():
                _walk(v, depth + 1)
        elif isinstance(node, (list, tuple)):
            for v in node:
                _walk(v, depth + 1)

    _walk(payload)
    return found
