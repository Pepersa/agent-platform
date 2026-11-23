# -*- coding: utf-8 -*-
"""知识层扩展工具实现（真实数据源，非 mock）。

对应设计文档：
    docs/02_场景方案_STDF分析与测试用例生成.md
        §4.4  用例生成规则 R01–R10（Lint 规则语义）
        §4.1  结构化测试用例 JSON 结构
        §5.1  通用契约（JSON Schema 入参/出参 + 权限级别 + HITL 标记）
        §5.2  工具清单（tp_lookup / spec_extract / case_lint / report_render / notify）
        §3    规格单位归一规则（pF/nF→F、uA→A、mV→V、mA→A、mΩ→Ω、ns→s、MHz→Hz）

本模块补齐 ``agent_platform/tools/__init__.py`` 通过 duck typing 期望的 5 个函数：

    tp_lookup(tp_name, tp_version=None, test_num=None, keyword=None)
    spec_extract(doc_id, sections=None, param_codes=None)
    case_lint(cases, rules=None, compare_history=False)
    report_render(template, data, include_evidence=True)
    notify(channel, receivers, title, body, severity="info", links=None)

数据来源（全部为真实文件，不返回 mock）：
    * ``data/docs/测试程序TP清单示例.md``      —— TP 清单（DOC-TP-001 / TP_CMOS_32G v2.4）
    * ``data/docs/规格书参数表示例.md``        —— 规格书（DOC-SPEC-001 / SPEC-77 v3.1）
    * ``data/kb/chunks.db``                   —— 检索索引（用于回填 chunk / section / page）

简化实现的边界（详见交付报告）：
    * 只解析 Markdown 表格，不支持 PDF / docling 版面还原；
    * Markdown 源无真实页码，``source.page`` 在无页码信息时为 ``None``，
      溯源仍由 ``source.quote``（表格行原文）+ ``source.section`` 保证；
    * ``case_lint`` 的 R08 依赖外部历史用例集，缺历史数据时不做重复判定；
    * R07 的「FMEA P1/P2 全覆盖」只做单用例级检查（无 FMEA 数据源）；
    * ``report_render`` / ``notify`` 只做本地渲染落盘 / 本地 outbox，不真正外发。
"""

from __future__ import annotations

import difflib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from agent_platform.core.tool_registry import ToolError

__all__ = [
    "tp_lookup",
    "spec_extract",
    "case_lint",
    "report_render",
    "notify",
    "ext_tool_specs",
    "register_knowledge_ext_tools",
    "LINT_RULES",
]

# ---------------------------------------------------------------------------
# 路径常量（可用环境变量覆盖，便于测试）
# ---------------------------------------------------------------------------
from agent_platform.core import paths as _paths  # noqa: PLC0415

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
#: 文档目录：``KB_DOCS_DIR`` > ``AGENT_DATA_DIR``/docs > ``<repo>/data/docs``
DOCS_DIR = (os.environ.get("KB_DOCS_DIR")
            or str(_paths.data_path("docs")))
REPORTS_DIR = (os.environ.get("KB_REPORTS_DIR")
               or str(_paths.data_path("reports")))
OUTBOX_PATH = (os.environ.get("KB_OUTBOX_PATH")
               or str(_paths.data_path("outbox", "notify.jsonl")))
CHUNKS_DB = (os.environ.get("KB_CHUNKS_DB")
             or str(_paths.data_path("kb", "chunks.db")))


def _rel_to_data_root(path: str) -> str:
    """把绝对路径转成**便于下游引用**的相对路径（POSIX 风格）。

    · 数据目录**未外置**（默认）→ 相对**仓库根**，得到 ``data/reports/xxx.md``
      这类路径（与改造前完全一致，下游按它引用）；
    · 数据目录**已外置** → 相对**数据根**，得到 ``reports/xxx.md``
      （避免出现 ``../../D:/agent-data/...`` 这种跨盘符的不可用路径）。

    Args:
        path: 绝对路径。

    Returns:
        POSIX 风格的相对路径。
    """
    try:
        data_root = _paths.data_dir()
        base = (_REPO_ROOT if data_root == _paths.default_data_dir()
                else str(data_root))
        return os.path.relpath(path, base).replace("\\", "/")
    except Exception:  # pragma: no cover - 兜底
        return os.path.relpath(path, _REPO_ROOT).replace("\\", "/")

#: 缺省单元格（只有上限无下限时文档用「—」表示）
_EMPTY_CELLS = {"", "—", "–", "-", "—", "n/a", "N/A", "无", "NA", "TBD"}

_lock = threading.Lock()
_doc_cache: dict[str, dict[str, Any]] = {}

# ---------------------------------------------------------------------------
# 单位归一（docs/02 §3 单位归一规则）
# ---------------------------------------------------------------------------
#: 原始单位 → (归一单位, 换算系数)
UNIT_NORMALIZATION: dict[str, tuple[str, float]] = {
    "pF": ("F", 1e-12),
    "nF": ("F", 1e-9),
    "uF": ("F", 1e-6),
    "uA": ("A", 1e-6),
    "µA": ("A", 1e-6),
    "μA": ("A", 1e-6),
    "mA": ("A", 1e-3),
    "mV": ("V", 1e-3),
    "kV": ("V", 1e3),
    "mΩ": ("Ω", 1e-3),
    "Ω": ("Ω", 1.0),
    "kΩ": ("Ω", 1e3),
    "MΩ": ("Ω", 1e6),
    "ns": ("s", 1e-9),
    "us": ("s", 1e-6),
    "µs": ("s", 1e-6),
    "ms": ("s", 1e-3),
    "ps": ("s", 1e-12),
    "MHz": ("Hz", 1e6),
    "GHz": ("Hz", 1e9),
    "kHz": ("Hz", 1e3),
}

#: 规则等级：error = 硬失败（回炉重生成），warning = 软警告（人工确认）
#: 参考 docs/02 §4.5：硬失败 R01/R02/R09，软警告 R05/R06。
LINT_RULES: dict[str, str] = {
    "R01": "限值必须来自规格书，禁止 LLM 编造数值；无来源则标注「待确认」并阻断",
    "R02": "每个 test_item 必须有 unit；无单位的值判不合格",
    "R03": "上下限必须满足 lower ≤ typical ≤ upper，且限值带至少 1 位有效数字",
    "R04": "步骤必须可复现：每步需有明确 action + params",
    "R05": "必须包含 PVT 条件（至少温度），否则标注条件不完整",
    "R06": "采样量必须明确（lot/wafer/die 数量）",
    "R07": "每个用例至少覆盖 1 个失效模式",
    "R08": "与历史用例语义相似度 > 0.9 且覆盖失效模式不新增 → 标记重复",
    "R09": "判定标准必须二值可判定（PASS/FAIL）",
    "R10": "测试项限值必须与 TP 中同名 test_num 一致",
}

_RULE_LEVEL: dict[str, str] = {
    "R01": "error",
    "R02": "error",
    "R03": "error",
    "R04": "warning",
    "R05": "warning",
    "R06": "warning",
    "R07": "warning",
    "R08": "warning",
    "R09": "error",
    "R10": "warning",
}

#: 判定标准中的模糊表述（R09）
_VAGUE_WORDS = (
    "基本正常", "大致", "大致符合", "基本符合", "基本满足", "基本无异常",
    "大体", "尚可", "大致正常", "approximately", "roughly", "almost",
)


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def _err(code: str, message: str, retryable: bool = False, hint: str = "") -> ToolError:
    """构造 core 契约的 ToolError（docs/01 §3.2 失败归一）。"""
    return ToolError(code, message, retryable=retryable, hint=hint)


def _clean_cell(text: str) -> str:
    """去掉 markdown 行内代码、反引号与首尾空白。"""
    s = (text or "").strip()
    s = s.replace("`", "")
    return s.strip()


def _is_empty_cell(text: str) -> bool:
    """判断单元格是否为空值（「—」等）。"""
    s = _clean_cell(text)
    return s in _EMPTY_CELLS or s == ""


def _to_float(text: str) -> Optional[float]:
    """把单元格转成 float；空值 / 非数值返回 None。"""
    s = _clean_cell(text)
    if s in _EMPTY_CELLS:
        return None
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _normalize_unit(unit: str) -> tuple[Optional[str], float, Optional[str]]:
    """单位归一。

    Args:
        unit: 原始单位字符串。

    Returns:
        ``(归一单位, 换算系数, 原始单位)``；无法归一时归一单位为 None。
    """
    raw = _clean_cell(unit)
    if not raw or raw in _EMPTY_CELLS:
        return None, 1.0, None
    if raw in UNIT_NORMALIZATION:
        norm, factor = UNIT_NORMALIZATION[raw]
        return norm, factor, raw
    return raw, 1.0, raw


def _norm_value(value: Optional[float], factor: float) -> Optional[float]:
    """按系数换算数值（保留 None）。"""
    if value is None:
        return None
    return value * factor


def _round_sig(value: Optional[float], digits: int = 9) -> Optional[float]:
    """消除浮点噪声（3.3000000000000003 → 3.3）。"""
    if value is None:
        return None
    return round(value, digits)


# ---------------------------------------------------------------------------
# 文档读取与 Markdown 表格解析
# ---------------------------------------------------------------------------
def _read_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """解析 YAML front matter（与 ``knowledge.ingest`` 同一套简易实现）。

    Args:
        text: markdown 原文。

    Returns:
        ``(元数据, 正文)``。
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
            return meta, "\n".join(lines[i + 1 :])
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


def _load_doc(path: str) -> dict[str, Any]:
    """读取并缓存单个 markdown 文档。

    Args:
        path: 文档路径。

    Returns:
        ``{"path","name","meta","body","mtime"}``。
    """
    mtime = os.path.getmtime(path)
    with _lock:
        cached = _doc_cache.get(path)
        if cached and cached.get("mtime") == mtime:
            return cached
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    meta, body = _read_front_matter(raw)
    doc = {
        "path": path,
        "name": os.path.basename(path),
        "meta": meta,
        "body": body,
        "mtime": mtime,
    }
    with _lock:
        _doc_cache[path] = doc
    return doc


def _all_docs() -> list[dict[str, Any]]:
    """读取 ``data/docs/`` 下的全部 markdown 文档。"""
    docs: list[dict[str, Any]] = []
    if not os.path.isdir(DOCS_DIR):
        return docs
    for name in sorted(os.listdir(DOCS_DIR)):
        if not name.lower().endswith((".md", ".markdown")):
            continue
        try:
            docs.append(_load_doc(os.path.join(DOCS_DIR, name)))
        except OSError:
            continue
    return docs


def _iter_tables(body: str) -> list[dict[str, Any]]:
    """把 markdown 正文拆成表格。

    Args:
        body: markdown 正文（不含 front matter）。

    Returns:
        表格列表，每项为
        ``{"section": str, "header": [str], "rows": [{"cells": [str], "raw": str}]}``。
    """
    lines = body.splitlines()
    tables: list[dict[str, Any]] = []
    section = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title:
                section = title
            i += 1
            continue
        if stripped.startswith("|") and i + 1 < len(lines):
            sep = lines[i + 1].strip()
            if sep.startswith("|") and re.fullmatch(r"\|[\s:\-|]+\|?", sep):
                header = _split_row(lines[i])
                rows: list[dict[str, Any]] = []
                j = i + 2
                while j < len(lines) and lines[j].strip().startswith("|"):
                    raw = lines[j].strip()
                    cells = _split_row(lines[j])
                    if not all(_is_empty_cell(c) or set(c) <= set("-: ") for c in cells):
                        rows.append({"cells": cells, "raw": raw})
                    j += 1
                tables.append({"section": section, "header": header, "rows": rows})
                i = j
                continue
        i += 1
    return tables


def _split_row(line: str) -> list[str]:
    """把一行 markdown 表格切成单元格。"""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    cells: list[str] = []
    buf = ""
    escaped = False
    for ch in s:
        if escaped:
            buf += ch
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "|":
            cells.append(buf.strip())
            buf = ""
            continue
        buf += ch
    cells.append(buf.strip())
    return cells


def _header_index(header: list[str], *names: str) -> Optional[int]:
    """在表头里找列下标（支持多个别名，忽略大小写与空白）。"""
    normalized = [h.replace(" ", "").lower() for h in header]
    for name in names:
        target = name.replace(" ", "").lower()
        if target in normalized:
            return normalized.index(target)
    return None


def _cell(cells: list[str], idx: Optional[int]) -> str:
    """按列下标取单元格（越界返回空串）。"""
    if idx is None or idx >= len(cells):
        return ""
    return cells[idx]


def _lookup_chunk(doc_id: str, quote: str) -> dict[str, Any]:
    """在检索索引中回填 chunk / section / page（索引缺失时返回空 dict）。

    匹配策略：优先用表格行中的标识符（如 ``T101_VDD_FUNCT``）做子串匹配，
    命中后回填 chunk_id / page / section。

    Args:
        doc_id: 文档 ID。
        quote: 原文片段（表格行）。

    Returns:
        ``{"chunk_id","page","section","source"}``（可能缺字段）。
    """
    if not os.path.exists(CHUNKS_DB):
        return {}
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{CHUNKS_DB}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "SELECT chunk_id, text, metadata FROM chunks WHERE doc_id = ?", (doc_id,)
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()

    # 提取标识符：表格行第一个单元格（如 T101_VDD_FUNCT）
    needle = ""
    cells = _split_row(quote) if quote else []
    if cells:
        needle = _clean_cell(cells[0])
    if not needle:
        needle = _clean_cell(quote)[:24]
    if not needle:
        return {}

    parsed: list[tuple[str, str, Any]] = []
    for chunk_id, text, meta in rows:
        try:
            meta_obj = json.loads(meta) if isinstance(meta, str) else (meta or {})
        except (TypeError, ValueError):
            meta_obj = {}
        parsed.append((chunk_id, text or "", meta_obj))

    # 优先：整行原文命中（最精确）
    full = _clean_cell(quote)
    for chunk_id, text, meta_obj in parsed:
        if full and full in text:
            return {
                "chunk_id": chunk_id,
                "page": meta_obj.get("page"),
                "section": meta_obj.get("section"),
                "source": meta_obj.get("source"),
            }
    # 次选：标识符命中
    for chunk_id, text, meta_obj in parsed:
        if needle in text:
            return {
                "chunk_id": chunk_id,
                "page": meta_obj.get("page"),
                "section": meta_obj.get("section"),
                "source": meta_obj.get("source"),
            }
    return {}


# ---------------------------------------------------------------------------
# TP 解析
# ---------------------------------------------------------------------------
def _parse_tp_items(body: str) -> list[dict[str, Any]]:
    """从 TP 文档正文解析测试项清单。

    Args:
        body: TP 文档正文。

    Returns:
        测试项列表，每项为
        ``{test_num,name,type,unit,lower,typ,upper,dft_method,bin,note,coverage,criteria,section,raw}``。
    """
    items: list[dict[str, Any]] = []
    for table in _iter_tables(body):
        header = table["header"]
        idx_test = _header_index(header, "test_num", "测试项编号", "testnum")
        if idx_test is None:
            continue
        idx_name = _header_index(header, "测试项名", "测试项名称", "name")
        idx_type = _header_index(header, "类型", "type")
        idx_unit = _header_index(header, "单位", "unit")
        idx_lower = _header_index(header, "下限", "lower", "下限值")
        idx_typ = _header_index(header, "典型值", "typ", "typical")
        idx_upper = _header_index(header, "上限", "upper", "上限值")
        idx_dft = _header_index(header, "dft方法", "dft_method", "方法")
        idx_bin = _header_index(header, "失效bin", "bin", "失效 bin")
        idx_note = _header_index(header, "说明", "note", "备注")
        idx_cov = _header_index(header, "覆盖率目标", "coverage")
        idx_cri = _header_index(header, "通过判据", "criteria", "判据")
        for row in table["rows"]:
            cells = row["cells"]
            test_num = _clean_cell(_cell(cells, idx_test))
            if not test_num:
                continue
            unit_raw = _clean_cell(_cell(cells, idx_unit))
            items.append(
                {
                    "test_num": test_num,
                    "name": _clean_cell(_cell(cells, idx_name)),
                    "type": _clean_cell(_cell(cells, idx_type)),
                    "unit": None if _is_empty_cell(unit_raw) else unit_raw,
                    "lower": _to_float(_cell(cells, idx_lower)),
                    "typ": _to_float(_cell(cells, idx_typ)),
                    "upper": _to_float(_cell(cells, idx_upper)),
                    "dft_method": _clean_cell(_cell(cells, idx_dft)),
                    "bin": _clean_cell(_cell(cells, idx_bin)),
                    "note": _clean_cell(_cell(cells, idx_note)),
                    "coverage": _clean_cell(_cell(cells, idx_cov)),
                    "criteria": _clean_cell(_cell(cells, idx_cri)),
                    "section": table["section"],
                    "raw": row["raw"],
                }
            )
    return items


def _find_tp_doc(tp_name: str) -> dict[str, Any]:
    """按 TP 名找对应文档。

    Args:
        tp_name: TP 名（如 ``TP_CMOS_32G``）。

    Returns:
        文档 dict。

    Raises:
        ToolError: 找不到时抛 ``NOT_FOUND``。
    """
    want = (tp_name or "").strip()
    if not want:
        raise _err(
            "INVALID_ARGUMENTS",
            "tp_name 不能为空",
            False,
            "请提供 TP 名，例如 'TP_CMOS_32G'。",
        )
    docs = _all_docs()
    exact: list[dict[str, Any]] = []
    loose: list[dict[str, Any]] = []
    for doc in docs:
        meta = doc["meta"]
        candidates = {
            str(meta.get("tp_name") or ""),
            str(meta.get("title") or ""),
            str(meta.get("doc_id") or ""),
        }
        if want in candidates:
            exact.append(doc)
        elif want and want in doc["body"]:
            loose.append(doc)
    if exact:
        return exact[0]
    if loose:
        return loose[0]
    known = sorted(
        {
            str(d["meta"].get("tp_name"))
            for d in docs
            if d["meta"].get("tp_name")
        }
    )
    raise _err(
        "NOT_FOUND",
        f"未找到 TP '{want}' 对应的文档",
        False,
        "可用的 TP：" + (", ".join(known) if known else "（知识库暂无 TP 文档）"),
    )


def tp_lookup(
    tp_name: str,
    tp_version: Optional[str] = None,
    test_num: Optional[str] = None,
    keyword: Optional[str] = None,
) -> dict[str, Any]:
    """查询测试程序（TP）中的测试项定义与限值。

    数据来源：``data/docs/测试程序TP清单示例.md``（DOC-TP-001 / TP_CMOS_32G v2.4）
    的 Markdown 测试项清单表，真实解析，不返回 mock。

    Args:
        tp_name: TP 名（如 ``TP_CMOS_32G``）。
        tp_version: TP 版本（如 ``v2.4``）。与文档版本不一致时**显式报错**，
            不做静默降级（docs/02 §4.4 R01 溯源要求）。
        test_num: 测试项编号（如 ``T101_VDD_FUNCT``），精确匹配（大小写不敏感）。
        keyword: 测试项名 / 编号 / 类型 / DFT 方法的模糊关键词。

    Returns:
        ``{"tp_id","tp_name","version","doc_id","items":[...],"total", "source"}``；
        每个 item 含 ``{test_num,name,type,unit,lower,typ,upper,dft_method,bin,
        note,coverage,criteria,section,source{page,quote,section,doc_id}}``。

    Raises:
        ToolError: ``INVALID_ARGUMENTS`` / ``NOT_FOUND`` / ``VERSION_MISMATCH``。
    """
    doc = _find_tp_doc(tp_name)
    meta = doc["meta"]
    doc_id = str(meta.get("doc_id") or doc["name"])
    version = str(meta.get("tp_version") or meta.get("version") or "")
    if tp_version:
        want_v = str(tp_version).strip().lstrip("vV")
        have_v = version.strip().lstrip("vV")
        if want_v != have_v:
            raise _err(
                "VERSION_MISMATCH",
                f"TP '{tp_name}' 版本不匹配：请求 {tp_version}，知识库中为 {version or '未知'}",
                False,
                "同一 test_num 在不同 TP 版本下语义可能不同，请改用正确版本，"
                f"或用 tp_lookup(tp_name='{tp_name}', tp_version='{version}') 查询。",
            )

    all_items = _parse_tp_items(doc["body"])
    items = list(all_items)

    if test_num:
        want = str(test_num).strip()
        hit = [it for it in items if it["test_num"].lower() == want.lower()]
        if not hit:
            raise _err(
                "NOT_FOUND",
                f"TP '{tp_name}' 中不存在 test_num '{want}'",
                False,
                "请先用 keyword 模糊检索该 TP 的测试项清单，确认 test_num 拼写。",
            )
        items = hit

    if keyword:
        kw = str(keyword).strip().lower()
        if kw:
            items = [
                it
                for it in items
                if kw in " ".join(
                    str(it.get(k) or "")
                    for k in ("test_num", "name", "type", "dft_method", "note", "criteria")
                ).lower()
            ]

    out_items: list[dict[str, Any]] = []
    for it in items:
        chunk = _lookup_chunk(doc_id, it["raw"])
        item = dict(it)
        item["source"] = {
            "doc_id": doc_id,
            "page": chunk.get("page"),
            "section": it["section"],
            "quote": it["raw"],
            "chunk_id": chunk.get("chunk_id"),
        }
        out_items.append(item)

    return {
        "tp_id": str(meta.get("tp_name") or tp_name),
        "tp_name": str(meta.get("tp_name") or tp_name),
        "version": version,
        "doc_id": doc_id,
        "items": out_items,
        "total": len(out_items),
        "source": {
            "doc_id": doc_id,
            "file": doc["name"],
            "version": version,
            "section": "测试项清单",
        },
    }


# ---------------------------------------------------------------------------
# 规格书解析
# ---------------------------------------------------------------------------
def _find_spec_doc(doc_id: str) -> dict[str, Any]:
    """按规格书 ID 找对应文档。

    Args:
        doc_id: 规格书 ID（如 ``SPEC-77`` 或 ``DOC-SPEC-001``）。

    Returns:
        文档 dict。

    Raises:
        ToolError: 找不到时抛 ``NOT_FOUND``。
    """
    want = (doc_id or "").strip()
    if not want:
        raise _err(
            "INVALID_ARGUMENTS",
            "doc_id 不能为空",
            False,
            "请提供规格书 ID，例如 'SPEC-77'。",
        )
    docs = _all_docs()
    for doc in docs:
        meta = doc["meta"]
        ids = {
            str(meta.get("doc_id") or ""),
            str(meta.get("spec_id") or ""),
            str(meta.get("title") or ""),
        }
        if want in ids:
            return doc
    for doc in docs:
        if want and want in doc["body"]:
            return doc
    known = sorted(
        {str(d["meta"].get("doc_id")) for d in docs if d["meta"].get("doc_id")}
    )
    raise _err(
        "NOT_FOUND",
        f"未找到规格书 '{want}' 对应的文档",
        False,
        "可用文档：" + (", ".join(known) if known else "（知识库暂无规格书文档）"),
    )


def _parse_spec_params(body: str) -> list[dict[str, Any]]:
    """解析规格书参数表。

    只解析「参数符号 + 最小值/典型值/最大值」结构的表；
    「绝对最大额定值」表（无最小值/典型值列）自动跳过（不是测试判据）。

    Args:
        body: 规格书正文。

    Returns:
        参数列表，每项含
        ``{param_code,name,min,typ,max,unit,condition,raw_min,raw_typ,raw_max,
        raw_unit,raw,section}``。
    """
    params: list[dict[str, Any]] = []
    for table in _iter_tables(body):
        header = table["header"]
        idx_code = _header_index(header, "参数符号", "param_code", "参数编码", "符号")
        if idx_code is None:
            continue
        idx_min = _header_index(header, "最小值", "min")
        idx_typ = _header_index(header, "典型值", "typ", "typical")
        idx_max = _header_index(header, "最大值", "max")
        if idx_min is None and idx_typ is None and idx_max is None:
            continue  # 绝对最大额定值表等，跳过
        idx_name = _header_index(header, "参数名称", "name")
        idx_unit = _header_index(header, "单位", "unit")
        idx_cond = _header_index(header, "测试条件", "条件", "condition")
        for row in table["rows"]:
            cells = row["cells"]
            code = _clean_cell(_cell(cells, idx_code))
            if not code:
                continue
            params.append(
                {
                    "param_code": code,
                    "name": _clean_cell(_cell(cells, idx_name)),
                    "min": _to_float(_cell(cells, idx_min)),
                    "typ": _to_float(_cell(cells, idx_typ)),
                    "max": _to_float(_cell(cells, idx_max)),
                    "unit": _clean_cell(_cell(cells, idx_unit)),
                    "condition": _clean_cell(_cell(cells, idx_cond)),
                    "raw_min": _clean_cell(_cell(cells, idx_min)),
                    "raw_typ": _clean_cell(_cell(cells, idx_typ)),
                    "raw_max": _clean_cell(_cell(cells, idx_max)),
                    "raw_unit": _clean_cell(_cell(cells, idx_unit)),
                    "raw": row["raw"],
                    "section": table["section"],
                }
            )
    return params


def _parse_spec_gaps(body: str) -> list[dict[str, str]]:
    """解析规格书「规格缺口清单」表。

    Args:
        body: 规格书正文。

    Returns:
        缺口列表，每项 ``{param_code,gap,impact,disposition}``。
    """
    gaps: list[dict[str, str]] = []
    for table in _iter_tables(body):
        header = table["header"]
        idx_param = _header_index(header, "参数", "param_code", "参数符号")
        idx_gap = _header_index(header, "缺口", "gap")
        if idx_param is None or idx_gap is None:
            continue
        idx_impact = _header_index(header, "影响", "impact")
        idx_disp = _header_index(header, "处置", "disposition", "处理")
        for row in table["rows"]:
            cells = row["cells"]
            code = _clean_cell(_cell(cells, idx_param))
            if not code:
                continue
            gaps.append(
                {
                    "param_code": code,
                    "gap": _clean_cell(_cell(cells, idx_gap)),
                    "impact": _clean_cell(_cell(cells, idx_impact)),
                    "disposition": _clean_cell(_cell(cells, idx_disp)),
                }
            )
    return gaps


def spec_extract(
    doc_id: str,
    sections: Optional[list[str]] = None,
    param_codes: Optional[list[str]] = None,
) -> dict[str, Any]:
    """从规格书文档抽取结构化参数（含溯源与单位归一）。

    数据来源：``data/docs/规格书参数表示例.md``（DOC-SPEC-001 / SPEC-77 v3.1）
    的参数表、单位归一规则表与规格缺口清单表，真实解析，不返回 mock。

    归一规则（docs/02 §3）：pF/nF→F、uA/µA→A、mV→V、mA→A、mΩ→Ω、ns→s、MHz→Hz；
    归一后同时保留 ``raw_value``（原始值）与 ``source.quote``（表格行原文），保证可溯源。

    Args:
        doc_id: 规格书 ID（``SPEC-77`` 或 ``DOC-SPEC-001``）。
        sections: 限定章节标题（可选，模糊包含匹配）。
        param_codes: 限定参数编码（可选，大小写不敏感）。

    Returns:
        ``{"doc_id","spec_id","params":[...],"gaps":[...]}``；
        每个 param 含
        ``{param_code,name,min,typ,max,unit,condition,raw_value{min,typ,max,unit},
        source{doc_id,page,quote,section,table,chunk_id}}``；
        ``gaps`` 为字符串列表（``"<param_code>: <缺口描述>"``），显式报告规格缺失。

    Raises:
        ToolError: ``INVALID_ARGUMENTS`` / ``NOT_FOUND``。
    """
    doc = _find_spec_doc(doc_id)
    meta = doc["meta"]
    real_doc_id = str(meta.get("doc_id") or doc["name"])
    spec_id = str(meta.get("spec_id") or "")
    if not spec_id:
        m = re.search(r"对应规格书：\s*`?([A-Za-z0-9\-_]+)`?", doc["body"])
        spec_id = m.group(1) if m else real_doc_id

    params = _parse_spec_params(doc["body"])
    doc_gaps = _parse_spec_gaps(doc["body"])

    if sections:
        wanted = [str(s).strip().lower() for s in sections if str(s).strip()]
        params = [
            p
            for p in params
            if any(w in str(p["section"]).lower() for w in wanted)
        ]

    scope: Optional[set[str]] = None
    if param_codes:
        scope = {str(c).strip().upper() for c in param_codes if str(c).strip()}
        params = [p for p in params if p["param_code"].upper() in scope]

    out_params: list[dict[str, Any]] = []
    derived_gaps: list[dict[str, str]] = []
    for p in params:
        norm_unit, factor, raw_unit = _normalize_unit(p["unit"])
        chunk = _lookup_chunk(real_doc_id, p["raw"])
        raw_value = {
            "min": p["raw_min"],
            "typ": p["raw_typ"],
            "max": p["raw_max"],
            "unit": raw_unit,
        }
        out_params.append(
            {
                "param_code": p["param_code"],
                "name": p["name"],
                "min": _round_sig(_norm_value(p["min"], factor)),
                "typ": _round_sig(_norm_value(p["typ"], factor)),
                "max": _round_sig(_norm_value(p["max"], factor)),
                "unit": norm_unit,
                "condition": p["condition"],
                "raw_value": raw_value,
                "source": {
                    "doc_id": real_doc_id,
                    "spec_id": spec_id,
                    "page": chunk.get("page"),
                    "section": p["section"],
                    "table": p["section"],
                    "quote": p["raw"],
                    "chunk_id": chunk.get("chunk_id"),
                },
            }
        )
        # 单边限值允许缺失（docs/02 §2 字段说明），但缺典型值无法做上下限合理性检查
        if p["typ"] is None and (p["min"] is not None or p["max"] is not None):
            derived_gaps.append(
                {
                    "param_code": p["param_code"],
                    "gap": "规格书未给典型值（typ 缺失），无法做上下限合理性检查",
                    "impact": "用例无法做 R03 上下限合理性检查",
                    "disposition": "用例标注「待确认」，仅测单边限值，不得臆造典型值",
                }
            )

    # 文档级缺口清单（按 scope 过滤）
    gap_entries: list[dict[str, str]] = []
    seen_codes: set[str] = set()
    for g in doc_gaps:
        if scope is not None and g["param_code"].upper() not in scope:
            continue
        gap_entries.append(g)
        seen_codes.add(g["param_code"].upper())
    for g in derived_gaps:
        if g["param_code"].upper() in seen_codes:
            continue
        if scope is not None and g["param_code"].upper() not in scope:
            continue
        gap_entries.append(g)
        seen_codes.add(g["param_code"].upper())

    gaps = [
        f"{g['param_code']}: {g['gap']}"
        + (f"（影响：{g['impact']}；处置：{g['disposition']}）"
           if g.get("impact") or g.get("disposition") else "")
        for g in gap_entries
    ]

    return {
        "doc_id": real_doc_id,
        "spec_id": spec_id,
        "version": str(meta.get("version") or ""),
        "params": out_params,
        "gaps": gaps,
        "gap_details": gap_entries,
    }


# ---------------------------------------------------------------------------
# case_lint（R01–R10）
# ---------------------------------------------------------------------------
def _first_present(case: dict[str, Any], *names: str) -> Any:
    """取第一个存在且非 None 的键。"""
    for name in names:
        if name in case and case[name] is not None:
            return case[name]
    return None


def _case_items(case: dict[str, Any]) -> list[dict[str, Any]]:
    """归一化用例中的 test_items 列表（兼容多种字段命名）。"""
    raw = _first_present(case, "test_items", "testItems", "items", "test_item") or []
    if isinstance(raw, dict):
        raw = [raw]
    out: list[dict[str, Any]] = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        unit = _first_present(it, "unit", "单位")
        unit_s = _clean_cell(str(unit)) if unit is not None else ""
        lower = _first_present(it, "lower_limit", "lower", "min", "lower_value")
        typ = _first_present(it, "typical", "typ", "typical_value", "typ_value")
        upper = _first_present(it, "upper_limit", "upper", "max", "upper_value")
        out.append(
            {
                "test_num": _clean_cell(str(_first_present(it, "test_num", "testNum") or "")),
                "name": _clean_cell(str(_first_present(it, "name", "测试项名", "title") or "")),
                "unit": None if (not unit_s or _is_empty_cell(unit_s)) else unit_s,
                "lower": lower if isinstance(lower, (int, float)) else _to_float(str(lower or "")),
                "typ": typ if isinstance(typ, (int, float)) else _to_float(str(typ or "")),
                "upper": upper if isinstance(upper, (int, float)) else _to_float(str(upper or "")),
                "source": it.get("source") if isinstance(it.get("source"), dict) else None,
                "raw": it,
            }
        )
    return out


def _case_preconditions(case: dict[str, Any]) -> list[dict[str, Any]]:
    """归一化 preconditions。"""
    raw = _first_present(case, "preconditions", "前置条件", "conditions") or []
    if isinstance(raw, dict):
        raw = [raw]
    out: list[dict[str, Any]] = []
    for pre in raw:
        if isinstance(pre, dict):
            out.append(
                {
                    "type": _clean_cell(str(pre.get("type") or "")),
                    "value": _clean_cell(str(pre.get("value") or "")),
                }
            )
        else:
            out.append({"type": "", "value": _clean_cell(str(pre))})
    return out


def _case_steps(case: dict[str, Any]) -> list[dict[str, Any]]:
    """归一化 test_steps。"""
    raw = _first_present(case, "test_steps", "steps", "步骤") or []
    if isinstance(raw, dict):
        raw = [raw]
    out: list[dict[str, Any]] = []
    for st in raw:
        if isinstance(st, dict):
            out.append(
                {
                    "step": st.get("step"),
                    "action": _clean_cell(str(st.get("action") or "")),
                    "params": st.get("params") if isinstance(st.get("params"), dict) else {},
                }
            )
    return out


def _case_failure_modes(case: dict[str, Any]) -> list[str]:
    """返回用例覆盖的失效模式 ID 列表。"""
    raw = _first_present(
        case, "covered_failure_modes", "coveredFailureModes", "failure_modes", "fms"
    ) or []
    if isinstance(raw, dict):
        raw = [raw]
    out: list[str] = []
    for fm in raw:
        if isinstance(fm, dict):
            out.append(_clean_cell(str(fm.get("mode_id") or fm.get("id") or fm.get("mode") or "")))
        else:
            out.append(_clean_cell(str(fm)))
    return [m for m in out if m]


def _case_traceability(case: dict[str, Any]) -> dict[str, Any]:
    """返回 traceability 字典。"""
    tr = case.get("traceability")
    return tr if isinstance(tr, dict) else {}


def _spec_refs(case: dict[str, Any]) -> list[dict[str, Any]]:
    """返回 traceability.spec_ref 列表。"""
    tr = _case_traceability(case)
    refs = _first_present(tr, "spec_ref", "spec_refs", "specRefs") or []
    if isinstance(refs, dict):
        refs = [refs]
    return [r for r in refs if isinstance(r, dict)]


def _has_temperature(case: dict[str, Any]) -> bool:
    """判断用例是否包含温度条件（R05）。"""
    for pre in _case_preconditions(case):
        blob = f"{pre['type']} {pre['value']}".lower()
        if "温度" in blob or "℃" in blob or "temp" in blob:
            return True
    blob = json.dumps(case, ensure_ascii=False, default=str).lower()
    return "温度" in blob or "℃" in blob


def _has_sample(case: dict[str, Any]) -> bool:
    """判断用例是否明确采样量（R06）。"""
    for pre in _case_preconditions(case):
        blob = f"{pre['type']} {pre['value']}".lower()
        if any(k in blob for k in ("sample", "采样", "lot", "wafer", "die")) and re.search(
            r"\d", blob
        ):
            return True
    return False


def _similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    """用例语义相似度（Jaccard 词集 + 序列相似度取大）。

    Args:
        a: 用例 A。
        b: 用例 B。

    Returns:
        0.0 ~ 1.0 的相似度。
    """

    def tokens(case: dict[str, Any]) -> set[str]:
        bag: set[str] = set()
        bag.update(re.findall(r"[A-Za-z0-9_]+", str(case.get("title") or "")))
        for it in _case_items(case):
            bag.update(re.findall(r"[A-Za-z0-9_]+", it["test_num"]))
            bag.update(re.findall(r"[一-鿿]+", it["name"]))
        for st in _case_steps(case):
            bag.update(re.findall(r"[A-Za-z0-9_]+", st["action"]))
        return {t.lower() for t in bag if t}

    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    jac = len(ta & tb) / len(ta | tb)
    seq = difflib.SequenceMatcher(
        None, " ".join(sorted(ta)), " ".join(sorted(tb))
    ).ratio()
    return round(max(jac, seq), 4)


def _fmt_num(value: Any) -> str:
    """数值转可读字符串。"""
    if value is None:
        return "None"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _rule(
    report: list[dict[str, Any]],
    case_id: str,
    rule_id: str,
    message: str,
    evidence: str,
    level: Optional[str] = None,
) -> None:
    """追加一条 lint 结果。"""
    report.append(
        {
            "case_id": case_id,
            "rule_id": rule_id,
            "level": level or _RULE_LEVEL.get(rule_id, "warning"),
            "message": message,
            "evidence": evidence,
        }
    )


def _lint_one_case(
    case: dict[str, Any],
    enabled: set[str],
    report: list[dict[str, Any]],
    tp_items: dict[str, dict[str, Any]],
    history: list[dict[str, Any]],
    compare_history: bool,
) -> None:
    """对单个用例执行 R01–R10 检查。

    Args:
        case: 用例字典。
        enabled: 启用的规则 ID 集合。
        report: 结果列表（原地追加）。
        tp_items: TP 测试项索引 ``{test_num: item}``。
        history: 历史用例列表。
        compare_history: 是否做 R08 重复判定。
    """
    case_id = _clean_cell(str(case.get("case_id") or case.get("id") or "<no_case_id>"))
    items = _case_items(case)
    steps = _case_steps(case)
    trace = _case_traceability(case)
    spec_refs = _spec_refs(case)

    # ---- R01 限值可溯源 ----
    if "R01" in enabled:
        for idx, it in enumerate(items):
            has_limit = any(v is not None for v in (it["lower"], it["typ"], it["upper"]))
            if not has_limit:
                continue
            item_src = it.get("source") or {}
            item_ok = bool(item_src.get("quote") or item_src.get("doc_id"))
            ref_ok = bool(spec_refs)
            quote_ok = any(r.get("quote") for r in spec_refs)
            if not (item_ok or ref_ok):
                _rule(
                    report,
                    case_id,
                    "R01",
                    f"测试项 {it['test_num'] or idx} 的限值无可溯源来源（缺 traceability.spec_ref / source）",
                    f"test_num={it['test_num']} lower={_fmt_num(it['lower'])} "
                    f"upper={_fmt_num(it['upper'])} unit={it['unit']}；"
                    f"traceability.spec_ref={spec_refs}",
                )
            elif not (quote_ok or item_src.get("quote")):
                _rule(
                    report,
                    case_id,
                    "R01",
                    f"测试项 {it['test_num'] or idx} 的限值溯源缺少原文引用（quote）",
                    f"traceability.spec_ref={spec_refs}",
                    level="warning",
                )

    # ---- R02 必须有单位 ----
    if "R02" in enabled:
        for idx, it in enumerate(items):
            if it["unit"] is None:
                _rule(
                    report,
                    case_id,
                    "R02",
                    f"测试项 {it['test_num'] or idx} 缺少单位",
                    f"test_item={json.dumps(it['raw'], ensure_ascii=False, default=str)}",
                )

    # ---- R03 上下限合理性 ----
    if "R03" in enabled:
        for idx, it in enumerate(items):
            lower, typ, upper = it["lower"], it["typ"], it["upper"]
            if lower is not None and upper is not None and lower > upper:
                _rule(
                    report,
                    case_id,
                    "R03",
                    f"测试项 {it['test_num'] or idx} 下限({lower}) 大于上限({upper})",
                    f"lower={_fmt_num(lower)} typ={_fmt_num(typ)} upper={_fmt_num(upper)}",
                )
            if typ is not None:
                if lower is not None and typ < lower:
                    _rule(
                        report,
                        case_id,
                        "R03",
                        f"测试项 {it['test_num'] or idx} 典型值({typ}) 低于下限({lower})",
                        f"lower={_fmt_num(lower)} typ={_fmt_num(typ)} upper={_fmt_num(upper)}",
                    )
                if upper is not None and typ > upper:
                    _rule(
                        report,
                        case_id,
                        "R03",
                        f"测试项 {it['test_num'] or idx} 典型值({typ}) 超出上限({upper})",
                        f"lower={_fmt_num(lower)} typ={_fmt_num(typ)} upper={_fmt_num(upper)}",
                    )
            for label, value in (("lower", lower), ("typ", typ), ("upper", upper)):
                if isinstance(value, float) and round(value, 6) != value:
                    _rule(
                        report,
                        case_id,
                        "R03",
                        f"测试项 {it['test_num'] or idx} 的 {label} 有效数字过多",
                        f"{label}={value!r}",
                        level="warning",
                    )

    # ---- R04 步骤可复现 ----
    if "R04" in enabled:
        if not steps:
            _rule(
                report,
                case_id,
                "R04",
                "用例缺少 test_steps，步骤不可复现",
                "test_steps=[]",
            )
        for st in steps:
            if not st["action"]:
                _rule(
                    report,
                    case_id,
                    "R04",
                    f"步骤 {st['step']} 缺少 action",
                    f"step={json.dumps(st, ensure_ascii=False, default=str)}",
                )
            if not st["params"]:
                _rule(
                    report,
                    case_id,
                    "R04",
                    f"步骤 {st['step']} 缺少 params（电压/电流/时间/温度/负载）",
                    f"step={json.dumps(st, ensure_ascii=False, default=str)}",
                )

    # ---- R05 PVT 条件（至少温度） ----
    if "R05" in enabled and not _has_temperature(case):
        _rule(
            report,
            case_id,
            "R05",
            "条件不完整：未包含温度条件（PVT）",
            f"preconditions={json.dumps(_case_preconditions(case), ensure_ascii=False)}",
        )

    # ---- R06 采样量 ----
    if "R06" in enabled and not _has_sample(case):
        _rule(
            report,
            case_id,
            "R06",
            "采样量未明确（缺 lot/wafer/die 数量）",
            f"preconditions={json.dumps(_case_preconditions(case), ensure_ascii=False)}",
        )

    # ---- R07 失效模式覆盖 ----
    if "R07" in enabled and not _case_failure_modes(case):
        _rule(
            report,
            case_id,
            "R07",
            "用例未覆盖任何失效模式",
            "covered_failure_modes=[]",
        )

    # ---- R08 重复度 ----
    if "R08" in enabled and compare_history:
        if not history:
            _rule(
                report,
                case_id,
                "R08",
                "缺少历史用例集，无法做重复度判定（compare_history=True 但历史库为空）",
                "history=[]",
                level="info",
            )
        else:
            own_modes = set(_case_failure_modes(case))
            best: Optional[tuple[float, dict[str, Any]]] = None
            for h in history:
                if h is case:
                    continue
                sim = _similarity(case, h)
                if best is None or sim > best[0]:
                    best = (sim, h)
            if best and best[0] > 0.9:
                h = best[1]
                h_modes = set(_case_failure_modes(h))
                if not (own_modes - h_modes):
                    _rule(
                        report,
                        case_id,
                        "R08",
                        f"与历史用例 {_clean_cell(str(h.get('case_id') or h.get('id') or '历史用例'))} "
                        f"相似度 {best[0]:.2f} > 0.9 且覆盖失效模式不新增，判定为重复",
                        f"similarity={best[0]:.4f}; "
                        f"current_modes={sorted(own_modes)}; history_modes={sorted(h_modes)}",
                    )

    # ---- R09 判定标准二值可判定 ----
    if "R09" in enabled:
        pass_criteria = _clean_boxed(case, "pass_criteria", "passCriteria", "判定标准")
        fail_criteria = _clean_boxed(case, "fail_criteria", "failCriteria")
        expected = _clean_boxed(case, "expected_result", "expectedResult", "预期结果")
        if not pass_criteria and not expected:
            _rule(
                report,
                case_id,
                "R09",
                "缺少判定标准（pass_criteria / expected_result），无法二值判定",
                f"pass_criteria={pass_criteria!r} expected_result={expected!r}",
            )
        else:
            blob = " ".join([pass_criteria, fail_criteria, expected])
            hits = [w for w in _VAGUE_WORDS if w.lower() in blob.lower()]
            if hits:
                _rule(
                    report,
                    case_id,
                    "R09",
                    f"判定标准含模糊表述 {hits}，必须二值可判定（PASS/FAIL）",
                    f"pass_criteria={pass_criteria!r} expected_result={expected!r}",
                )

    # ---- R10 与 TP 一致性 ----
    if "R10" in enabled and tp_items:
        for it in items:
            if not it["test_num"]:
                continue
            tp = tp_items.get(it["test_num"].upper())
            if tp is None:
                continue
            for label, case_val, tp_val in (
                ("lower", it["lower"], tp.get("lower")),
                ("upper", it["upper"], tp.get("upper")),
            ):
                if case_val is None or tp_val is None:
                    continue
                if abs(float(case_val) - float(tp_val)) > 1e-9:
                    _rule(
                        report,
                        case_id,
                        "R10",
                        f"测试项 {it['test_num']} 的 {label}({case_val}) 与 TP 中同名 "
                        f"test_num 的 {label}({tp_val}) 不一致，需更新 TP",
                        f"case={label}:{case_val}; tp({tp.get('test_num')}):{label}:{tp_val}",
                    )


def _clean_boxed(case: dict[str, Any], *names: str) -> str:
    """取字符串字段并清洗。"""
    val = _first_present(case, *names)
    if val is None:
        return ""
    return _clean_cell(str(val))


def case_lint(
    cases: Any,
    rules: Optional[list[str]] = None,
    compare_history: bool = False,
    history: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """测试用例质量静态检查（docs/02 §4.4 规则 R01–R10）。

    **入参 ``cases`` 的结构**（兼容 docs/02 §4.1 的结构化用例 JSON，也兼容评测集
    L4 ``gold_answer`` 描述的扁平形态）::

        {
          "case_id": "TC_FUNC_VDD_001",
          "title": "VDD 电源功能测试-常温常压",
          "preconditions": [
            {"type": "environment", "value": "T=25±3℃, Humi=30~70%RH"},
            {"type": "sample", "value": "lot>=3, 每 lot wafer>=3, 每 wafer die>=25"}
          ],
          "test_steps": [{"step": 1, "action": "施加 VDD=3.3V", "params": {"VDD": "3.3V"}}],
          "test_items": [{"test_num": "T101_VDD_FUNCT", "name": "VDD 输出功能",
                          "unit": "V", "lower_limit": 3.135, "upper_limit": 3.465,
                          "typical": 3.3, "measure": "DC"}],
          "expected_result": "...",
          "pass_criteria": "...",
          "fail_criteria": "...",
          "covered_failure_modes": [{"mode_id": "FM-012", "mode": "...", "coverage": "direct"}],
          "traceability": {"spec_ref": [{"doc_id": "SPEC-77", "page": 12,
                                         "quote": "VDD 3.135 3.300 3.465 V"}],
                           "tp_ref": {"tp_name": "TP_CMOS_32G", "tp_version": "v2.4"}}
        }

    字段别名（均被接受）：``test_items``/``testItems``/``items``；
    ``lower_limit``/``lower``/``min``；``upper_limit``/``upper``/``max``；
    ``typical``/``typ``；``test_steps``/``steps``；``covered_failure_modes``/``failure_modes``。

    Args:
        cases: 用例列表；也接受 ``{"cases": [...], "history": [...]}`` 形态。
        rules: 启用的规则 ID 列表（如 ``["R01","R02"]``）；缺省为全部 R01–R10。
        compare_history: 是否做 R08 重复度比对。
        history: 历史用例列表（可选）；缺省时从 ``cases`` 的 dict 形态里取。

    Returns:
        ``{"report":[{"case_id","rule_id","level","message","evidence"}],
        "summary":{"pass_rate","coverage_matrix",...}}``；
        ``coverage_matrix`` 为 ``{rule_id: {"checked": n, "violations": m, "levels": {...}}}``。

    Raises:
        ToolError: ``INVALID_ARGUMENTS``（cases 为空或类型不对）。
    """
    history_cases: list[dict[str, Any]] = list(history or [])
    if isinstance(cases, dict):
        history_cases.extend(cases.get("history") or [])
        case_list = list(cases.get("cases") or [])
    elif isinstance(cases, list):
        case_list = list(cases)
    else:
        raise _err(
            "INVALID_ARGUMENTS",
            f"cases 必须是列表或含 cases 字段的字典，实际为 {type(cases).__name__}",
            False,
            "请传入测试用例数组，例如 [{'case_id': 'TC_001', 'test_items': [...]}]。",
        )
    if not case_list:
        raise _err(
            "INVALID_ARGUMENTS",
            "cases 为空，无法执行 Lint",
            False,
            "请至少提供一个测试用例对象。",
        )

    enabled = (
        {str(r).strip().upper() for r in rules if str(r).strip()}
        if rules
        else set(LINT_RULES)
    )
    unknown = sorted(enabled - set(LINT_RULES))
    if unknown:
        raise _err(
            "INVALID_ARGUMENTS",
            f"未知规则 ID：{unknown}",
            False,
            f"可用规则 ID：{sorted(LINT_RULES)}",
        )

    # TP 索引（R10 用）
    tp_items: dict[str, dict[str, Any]] = {}
    try:
        tp_doc = _find_tp_doc("TP_CMOS_32G")
        for it in _parse_tp_items(tp_doc["body"]):
            tp_items[it["test_num"].upper()] = it
    except ToolError:
        tp_items = {}

    report: list[dict[str, Any]] = []
    for case in case_list:
        if not isinstance(case, dict):
            continue
        _lint_one_case(
            case, enabled, report, tp_items, history_cases, bool(compare_history)
        )

    # ---- 汇总 ----
    total = len([c for c in case_list if isinstance(c, dict)])
    bad_cases = {r["case_id"] for r in report if r["level"] == "error"}
    pass_rate = round((total - len(bad_cases)) / total, 4) if total else 0.0

    coverage_matrix: dict[str, Any] = {}
    for rule_id in sorted(LINT_RULES):
        rows = [r for r in report if r["rule_id"] == rule_id]
        levels: dict[str, int] = {}
        for r in rows:
            levels[r["level"]] = levels.get(r["level"], 0) + 1
        coverage_matrix[rule_id] = {
            "rule": LINT_RULES[rule_id],
            "level_default": _RULE_LEVEL[rule_id],
            "violations": len(rows),
            "levels": levels,
            "checked": total if rule_id in enabled else 0,
            "enabled": rule_id in enabled,
        }

    summary = {
        "pass_rate": pass_rate,
        "total_cases": total,
        "failed_cases": len(bad_cases),
        "error_count": sum(1 for r in report if r["level"] == "error"),
        "warning_count": sum(1 for r in report if r["level"] == "warning"),
        "info_count": sum(1 for r in report if r["level"] == "info"),
        "rules_enabled": sorted(enabled),
        "coverage_matrix": coverage_matrix,
        "limitations": [
            "R08 需要外部历史用例集（history/compare_history），缺数据时不做重复判定",
            "R07 仅做单用例级失效模式覆盖检查，无 FMEA P1/P2 全量覆盖校验",
            "R10 仅覆盖 TP 清单中已有的 test_num",
        ],
    }
    return {"report": report, "summary": summary}


# ---------------------------------------------------------------------------
# report_render
# ---------------------------------------------------------------------------
def _md_table(columns: list[str], rows: list[list[Any]]) -> list[str]:
    """渲染 markdown 表格。"""
    if not columns:
        return []
    out = ["| " + " | ".join(str(c) for c in columns) + " |"]
    out.append("|" + "|".join(["---"] * len(columns)) + "|")
    for row in rows:
        cells = ["" if c is None else str(c) for c in row]
        cells += [""] * (len(columns) - len(cells))
        out.append("| " + " | ".join(cells[: len(columns)]) + " |")
    return out


def _render_daily(data: dict[str, Any], include_evidence: bool) -> str:
    """渲染 daily 模板。"""
    lines = [f"# {data.get('title') or '测试日报'}", ""]
    if data.get("date"):
        lines += [f"**日期**：{data['date']}", ""]
    if data.get("scope"):
        lines += [f"**范围**：{data['scope']}", ""]
    if data.get("summary"):
        lines += ["## 概要", "", str(data["summary"]), ""]
    metrics = data.get("metrics") or []
    if metrics:
        lines += ["## 关键指标", ""]
        lines += _md_table(
            ["指标", "数值", "单位"],
            [[m.get("name"), m.get("value"), m.get("unit")] for m in metrics],
        )
        lines += [""]
    for sec in data.get("sections") or []:
        lines += [f"## {sec.get('heading') or sec.get('title') or '章节'}", ""]
        if sec.get("body"):
            lines += [str(sec["body"]), ""]
        table = sec.get("table")
        if table:
            lines += _md_table(
                list(table.get("columns") or []),
                [list(r) for r in (table.get("rows") or [])],
            )
            lines += [""]
    if data.get("conclusion"):
        lines += ["## 结论", "", str(data["conclusion"]), ""]
    if include_evidence and (data.get("evidence") or data.get("evidence_refs")):
        lines += ["## 证据索引", ""]
        for ev in data.get("evidence") or data.get("evidence_refs") or []:
            lines.append(f"- {ev}")
        lines += [""]
    return "\n".join(lines).rstrip() + "\n"


def _render_weekly(data: dict[str, Any], include_evidence: bool) -> str:
    """渲染 weekly 模板。"""
    lines = [f"# {data.get('title') or '测试周报'}", ""]
    if data.get("date"):
        lines += [f"**周期**：{data['date']}", ""]
    if data.get("scope"):
        lines += [f"**范围**：{data['scope']}", ""]
    if data.get("summary"):
        lines += ["## 本周概要", "", str(data["summary"]), ""]
    metrics = data.get("metrics") or []
    if metrics:
        lines += ["## 指标对比", ""]
        lines += _md_table(
            ["指标", "本周", "上周", "单位"],
            [
                [m.get("name"), m.get("value"), m.get("previous"), m.get("unit")]
                for m in metrics
            ],
        )
        lines += [""]
    trends = data.get("trends") or []
    if trends:
        lines += ["## 趋势与异常", ""]
        for t in trends:
            lines.append(f"- **{t.get('name')}**：{t.get('description') or t.get('value')}")
        lines += [""]
    for sec in data.get("sections") or []:
        lines += [f"## {sec.get('heading') or sec.get('title') or '章节'}", ""]
        if sec.get("body"):
            lines += [str(sec["body"]), ""]
        table = sec.get("table")
        if table:
            lines += _md_table(
                list(table.get("columns") or []),
                [list(r) for r in (table.get("rows") or [])],
            )
            lines += [""]
    if data.get("conclusion"):
        lines += ["## 结论与后续行动", "", str(data["conclusion"]), ""]
    if include_evidence and (data.get("evidence") or data.get("evidence_refs")):
        lines += ["## 证据索引", ""]
        for ev in data.get("evidence") or data.get("evidence_refs") or []:
            lines.append(f"- {ev}")
        lines += [""]
    return "\n".join(lines).rstrip() + "\n"


def _render_customer(data: dict[str, Any], include_evidence: bool) -> str:
    """渲染 customer 模板（对外，措辞中性，不暴露内部 ID 细节）。"""
    lines = [f"# {data.get('title') or '测试情况报告'}", ""]
    if data.get("customer"):
        lines += [f"**客户**：{data['customer']}", ""]
    if data.get("date"):
        lines += [f"**日期**：{data['date']}", ""]
    if data.get("product"):
        lines += [f"**产品**：{data['product']}", ""]
    if data.get("summary"):
        lines += ["## 总体结论", "", str(data["summary"]), ""]
    metrics = data.get("metrics") or []
    if metrics:
        lines += ["## 主要指标", ""]
        lines += _md_table(
            ["指标", "结果"],
            [[m.get("name"), m.get("value")] for m in metrics],
        )
        lines += [""]
    for sec in data.get("sections") or []:
        lines += [f"## {sec.get('heading') or sec.get('title') or '说明'}", ""]
        if sec.get("body"):
            lines += [str(sec["body"]), ""]
    if data.get("conclusion"):
        lines += ["## 结论", "", str(data["conclusion"]), ""]
    if include_evidence and data.get("evidence"):
        lines += ["## 附录：数据来源", ""]
        for ev in data["evidence"]:
            lines.append(f"- {ev}")
        lines += [""]
    return "\n".join(lines).rstrip() + "\n"


def _render_spec(data: dict[str, Any], include_evidence: bool) -> str:
    """渲染 spec 模板（规格 / 限值对照）。"""
    lines = [f"# {data.get('title') or '规格参数对照报告'}", ""]
    if data.get("spec_id"):
        lines += [f"**规格书**：{data['spec_id']}", ""]
    if data.get("date"):
        lines += [f"**日期**：{data['date']}", ""]
    if data.get("summary"):
        lines += ["## 摘要", "", str(data["summary"]), ""]
    params = data.get("params") or []
    if params:
        lines += ["## 参数与限值", ""]
        lines += _md_table(
            ["参数", "名称", "下限", "典型", "上限", "单位", "条件"],
            [
                [
                    p.get("param_code"),
                    p.get("name"),
                    p.get("min"),
                    p.get("typ"),
                    p.get("max"),
                    p.get("unit"),
                    p.get("condition"),
                ]
                for p in params
            ],
        )
        lines += [""]
    gaps = data.get("gaps") or []
    if gaps:
        lines += ["## 规格缺口", ""]
        for g in gaps:
            lines.append(f"- {g}")
        lines += [""]
    for sec in data.get("sections") or []:
        lines += [f"## {sec.get('heading') or sec.get('title') or '章节'}", ""]
        if sec.get("body"):
            lines += [str(sec["body"]), ""]
    if include_evidence and (data.get("evidence") or data.get("evidence_refs")):
        lines += ["## 溯源索引", ""]
        for ev in data.get("evidence") or data.get("evidence_refs") or []:
            lines.append(f"- {ev}")
        lines += [""]
    return "\n".join(lines).rstrip() + "\n"


_REPORT_TEMPLATES = {
    "daily": _render_daily,
    "weekly": _render_weekly,
    "customer": _render_customer,
    "spec": _render_spec,
}


def report_render(
    template: str,
    data: Optional[dict[str, Any]] = None,
    include_evidence: bool = True,
) -> dict[str, Any]:
    """按模板渲染报告并落盘到 ``data/reports/``。

    权限语义：本函数只负责「渲染 + 落盘」；``WRITE_DATA`` 的 HITL 确认由上层
    ToolRegistry 依据 ToolSpec 的 ``hitl_required`` 判定（本工具在 core 侧注册为
    ``WRITE_DATA`` → ``hitl_required=True``）。

    Args:
        template: 模板名（``daily`` / ``weekly`` / ``customer`` / ``spec``）。
        data: 报告数据体（标题、指标、章节、证据索引等）。
        include_evidence: 是否包含证据 / 溯源索引。

    Returns:
        ``{"report_id","uri","preview_md","path","template","generated_at"}``；
        ``uri`` 为相对仓库根目录的路径（``data/reports/xxx.md``）。

    Raises:
        ToolError: ``INVALID_ARGUMENTS``（模板不支持 / data 不是字典）。
    """
    tpl = str(template or "").strip().lower()
    if tpl not in _REPORT_TEMPLATES:
        raise _err(
            "INVALID_ARGUMENTS",
            f"不支持的报告模板：{template!r}",
            False,
            "可用模板：daily / weekly / customer / spec",
        )
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise _err(
            "INVALID_ARGUMENTS",
            f"data 必须是对象，实际为 {type(data).__name__}",
            False,
            "请传入报告数据字典，例如 {'title': 'L1 日报', 'metrics': [...]}。",
        )

    now = datetime.now(timezone.utc).astimezone()
    report_id = (
        f"RPT-{tpl.upper()}-{now.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"
    )
    md = _REPORT_TEMPLATES[tpl](data, bool(include_evidence))
    md = md.replace("{{report_id}}", report_id)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = os.path.join(REPORTS_DIR, f"{report_id}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)

    # URI 计算：
    #   · 数据目录**未外置**（默认）→ 相对仓库根，得到 ``data/reports/xxx.md``
    #     （与改造前完全一致，下游按这个路径引用）；
    #   · 数据目录**已外置** → 相对数据根，得到 ``reports/xxx.md``
    #     （避免出现 ``../../D:/agent-data/...`` 这种不可用的路径）。
    rel = _rel_to_data_root(path)
    return {
        "report_id": report_id,
        "uri": rel,
        "path": path,
        "preview_md": md,
        "template": tpl,
        "generated_at": now.isoformat(),
        "include_evidence": bool(include_evidence),
    }


# ---------------------------------------------------------------------------
# notify
# ---------------------------------------------------------------------------
_NOTIFY_CHANNELS = ("wecom", "feishu", "dingtalk", "email")
_NOTIFY_SEVERITY = ("info", "warning", "critical")


def notify(
    channel: str,
    receivers: Optional[list[str]] = None,
    title: str = "",
    body: str = "",
    severity: str = "info",
    links: Optional[list[Any]] = None,
) -> dict[str, Any]:
    """发送通知（本地 outbox 落盘，不真正外发）。

    权限语义：``EXTERNAL_SIDE_EFFECT`` → 强制 HITL 确认，由上层 ToolRegistry 判定；
    本函数只负责把通知写入本地 outbox ``data/outbox/notify.jsonl``（append）。

    Args:
        channel: 通知渠道（``wecom`` / ``feishu`` / ``dingtalk`` / ``email``）。
        receivers: 接收人列表（用户 ID 或邮箱）。
        title: 通知标题。
        body: 通知正文。
        severity: 严重级别（``info`` / ``warning`` / ``critical``）。
        links: 相关链接列表。

    Returns:
        ``{"message_id","sent_at","status","channel","receivers","severity","level",
        "outbox_path","external_sent"}``。

    Raises:
        ToolError: ``INVALID_ARGUMENTS``（渠道 / 接收人 / 标题 / 正文不合法）。
    """
    ch = str(channel or "").strip().lower()
    if ch not in _NOTIFY_CHANNELS:
        raise _err(
            "INVALID_ARGUMENTS",
            f"不支持的通知渠道：{channel!r}",
            False,
            "可用渠道：wecom / feishu / dingtalk / email",
        )
    recv = [str(r).strip() for r in (receivers or []) if str(r).strip()]
    if not recv:
        raise _err(
            "INVALID_ARGUMENTS",
            "receivers 不能为空",
            False,
            "请提供至少一个接收人（用户 ID 或邮箱）。",
        )
    if not str(title or "").strip():
        raise _err(
            "INVALID_ARGUMENTS",
            "title 不能为空",
            False,
            "请提供通知标题。",
        )
    if not str(body or "").strip():
        raise _err(
            "INVALID_ARGUMENTS",
            "body 不能为空",
            False,
            "请提供通知正文。",
        )
    sev = str(severity or "info").strip().lower()
    if sev not in _NOTIFY_SEVERITY:
        raise _err(
            "INVALID_ARGUMENTS",
            f"不支持的 severity：{severity!r}",
            False,
            "可用 severity：info / warning / critical",
        )

    now = datetime.now(timezone.utc).astimezone()
    message_id = f"MSG-{now.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8].upper()}"
    record = {
        "message_id": message_id,
        "sent_at": now.isoformat(),
        "channel": ch,
        "receivers": recv,
        "title": str(title),
        "body": str(body),
        "severity": sev,
        "level": sev,
        "links": list(links or []),
        "status": "queued",
        "external_sent": False,
        "note": "本地 outbox 落盘，未真正外发（外发需上层 HITL 批准后由通知适配器执行）",
    }
    os.makedirs(os.path.dirname(OUTBOX_PATH), exist_ok=True)
    with open(OUTBOX_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return {
        "message_id": message_id,
        "sent_at": record["sent_at"],
        "status": "queued",
        "channel": ch,
        "receivers": recv,
        "severity": sev,
        "level": sev,
        "links": record["links"],
        # 与 report_render 一致：未外置时相对仓库根（``data/outbox/...``），
        # 已外置时相对数据根（``outbox/...``）。
        "outbox_path": _rel_to_data_root(OUTBOX_PATH),
        "external_sent": False,
    }


# ---------------------------------------------------------------------------
# Tool Registry 适配层（kwargs 形态 handler）
# ---------------------------------------------------------------------------
# ToolRegistry._invoke 依据签名区分调用形态：``impl(args, ctx)`` 与 ``impl(**kwargs)``。
# 下列 handler 全部为纯 kwargs 形态，保证被 registry 直接注册时参数正确展开。
def _tp_lookup_handler(**kwargs: Any) -> dict[str, Any]:
    """``tp_lookup`` 的 kwargs 形态 handler。"""
    return tp_lookup(
        tp_name=str(kwargs.get("tp_name") or ""),
        tp_version=kwargs.get("tp_version"),
        test_num=kwargs.get("test_num"),
        keyword=kwargs.get("keyword"),
    )


def _spec_extract_handler(**kwargs: Any) -> dict[str, Any]:
    """``spec_extract`` 的 kwargs 形态 handler。"""
    return spec_extract(
        doc_id=str(kwargs.get("doc_id") or ""),
        sections=kwargs.get("sections"),
        param_codes=kwargs.get("param_codes"),
    )


def _case_lint_handler(**kwargs: Any) -> dict[str, Any]:
    """``case_lint`` 的 kwargs 形态 handler。"""
    return case_lint(
        cases=kwargs.get("cases") or [],
        rules=kwargs.get("rules"),
        compare_history=bool(kwargs.get("compare_history", False)),
    )


def _report_render_handler(**kwargs: Any) -> dict[str, Any]:
    """``report_render`` 的 kwargs 形态 handler。"""
    return report_render(
        template=str(kwargs.get("template") or "daily"),
        data=kwargs.get("data") or {},
        include_evidence=bool(kwargs.get("include_evidence", True)),
    )


def _notify_handler(**kwargs: Any) -> dict[str, Any]:
    """``notify`` 的 kwargs 形态 handler。"""
    return notify(
        channel=str(kwargs.get("channel") or "wecom"),
        receivers=kwargs.get("receivers") or [],
        title=str(kwargs.get("title") or ""),
        body=str(kwargs.get("body") or ""),
        severity=str(kwargs.get("severity") or "info"),
        links=kwargs.get("links"),
    )


# ---------------------------------------------------------------------------
# ToolSpec 工厂（供 core ToolRegistry 直接注册）
# ---------------------------------------------------------------------------
def ext_tool_specs() -> list[dict[str, Any]]:
    """构造 5 个扩展工具的 spec（dict 形态，含 ``handler``）。

    权限级别与 HITL 标记严格对齐 docs/02 §5.2：
        tp_lookup     → READ_ONLY            （无 HITL）
        spec_extract  → COMPUTE              （无 HITL）
        case_lint     → COMPUTE              （无 HITL）
        report_render → WRITE_DATA           （HITL ✅）
        notify        → EXTERNAL_SIDE_EFFECT （HITL ✅）

    Returns:
        spec dict 列表。
    """
    return [
        {
            "name": "tp_lookup",
            "description": (
                "查询测试程序（TP）中的测试项定义与限值"
                "（test_num / name / unit / lower / upper / dft_method），"
                "限值来自真实 TP 清单文档，可溯源。"
            ),
            "input_schema": {
                "type": "object",
                "required": ["tp_name"],
                "additionalProperties": False,
                "properties": {
                    "tp_name": {"type": "string", "minLength": 1,
                                "description": "测试程序名，如 TP_CMOS_32G"},
                    "tp_version": {"type": "string", "description": "TP 版本号，如 v2.4"},
                    "test_num": {"type": "string", "description": "测试项编号，如 T101_VDD_FUNCT"},
                    "keyword": {"type": "string", "description": "测试项名称关键词"},
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "tp_id": {"type": "string"},
                    "version": {"type": "string"},
                    "items": {"type": "array", "items": {"type": "object"}},
                    "total": {"type": "integer"},
                },
            },
            "permission": "READ_ONLY",
            "hitl_required": False,
            "timeout_ms": 20_000,
            "retry": {"max": 2, "backoff_ms": 500},
            "audit": True,
            "idempotent": True,
            "metadata": {
                "description": "查 TP 测试项与限值（可溯源）",
                "impact": "只读查询，无副作用",
                "reversible": True,
            },
            "handler": _tp_lookup_handler,
        },
        {
            "name": "spec_extract",
            "description": (
                "从规格书抽取结构化参数（param_code / min / typ / max / unit / condition），"
                "带单位归一与原文溯源（source.quote），缺失项写入 gaps。"
            ),
            "input_schema": {
                "type": "object",
                "required": ["doc_id"],
                "additionalProperties": False,
                "properties": {
                    "doc_id": {"type": "string", "minLength": 1,
                               "description": "规格书文档 ID，如 SPEC-77"},
                    "sections": {"type": "array", "items": {"type": "string"},
                                 "description": "限定抽取的章节标题"},
                    "param_codes": {"type": "array", "items": {"type": "string"},
                                    "description": "限定抽取的参数编码"},
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "params": {"type": "array", "items": {"type": "object"}},
                    "gaps": {"type": "array", "items": {"type": "string"}},
                },
            },
            "permission": "COMPUTE",
            "hitl_required": False,
            "timeout_ms": 60_000,
            "retry": {"max": 1, "backoff_ms": 500},
            "audit": True,
            "idempotent": True,
            "metadata": {
                "description": "从规格书抽取参数（含单位归一与溯源）",
                "impact": "纯计算，无副作用",
                "reversible": True,
            },
            "handler": _spec_extract_handler,
        },
        {
            "name": "case_lint",
            "description": (
                "对测试用例集做质量静态检查（规则 R01–R10：限值溯源、单位、上下限合理性、"
                "步骤可复现、PVT 条件、采样量、失效模式覆盖、重复度、二值判定、与 TP 一致性），"
                "返回逐条问题与通过率摘要。"
            ),
            "input_schema": {
                "type": "object",
                "required": ["cases"],
                "additionalProperties": False,
                "properties": {
                    "cases": {"type": "array", "minItems": 1,
                              "items": {"type": "object"},
                              "description": "测试用例对象数组"},
                    "rules": {"type": "array", "items": {"type": "string"},
                              "description": "启用的规则 ID，如 ['R01','R02']；缺省为全部"},
                    "compare_history": {"type": "boolean", "default": False,
                                        "description": "是否与历史用例做重复度比对（R08）"},
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "report": {"type": "array", "items": {"type": "object"}},
                    "summary": {"type": "object"},
                },
            },
            "permission": "COMPUTE",
            "hitl_required": False,
            "timeout_ms": 60_000,
            "retry": {"max": 1, "backoff_ms": 500},
            "audit": True,
            "idempotent": True,
            "metadata": {
                "description": "用例质量静态检查（R01–R10）",
                "impact": "纯计算，无副作用",
                "reversible": True,
            },
            "handler": _case_lint_handler,
        },
        {
            "name": "report_render",
            "description": (
                "按模板（daily / weekly / customer / spec）渲染结构化报告，"
                "落盘到 data/reports/ 并返回报告 ID、URI 与 Markdown 预览。"
                "属于写操作，必须经人工确认（HITL）。"
            ),
            "input_schema": {
                "type": "object",
                "required": ["template", "data"],
                "additionalProperties": False,
                "properties": {
                    "template": {"type": "string",
                                 "enum": ["daily", "weekly", "customer", "spec"],
                                 "description": "报告模板"},
                    "data": {"type": "object", "description": "报告数据体"},
                    "include_evidence": {"type": "boolean", "default": True,
                                         "description": "是否包含证据 / 溯源索引"},
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "report_id": {"type": "string"},
                    "uri": {"type": "string"},
                    "preview_md": {"type": "string"},
                },
            },
            "permission": "WRITE_DATA",
            "hitl_required": True,
            "timeout_ms": 60_000,
            "retry": {"max": 1, "backoff_ms": 500},
            "audit": True,
            "idempotent": True,
            "metadata": {
                "description": "生成结构化分析报告并落盘到 data/reports/",
                "impact": "在 data/reports/ 下生成 Markdown 文件，可被下游引用",
                "reversible": True,
                "approver": "测试主管",
            },
            "handler": _report_render_handler,
        },
        {
            "name": "notify",
            "description": (
                "向指定渠道（wecom / feishu / dingtalk / email）向若干接收人发送告警或通知。"
                "属于对外副作用操作，必须经人工确认（HITL）。"
            ),
            "input_schema": {
                "type": "object",
                "required": ["channel", "receivers", "title", "body"],
                "additionalProperties": False,
                "properties": {
                    "channel": {"type": "string",
                                "enum": ["wecom", "feishu", "dingtalk", "email"],
                                "description": "通知渠道"},
                    "receivers": {"type": "array", "minItems": 1,
                                  "items": {"type": "string"},
                                  "description": "接收人列表"},
                    "title": {"type": "string", "minLength": 1, "description": "通知标题"},
                    "body": {"type": "string", "minLength": 1, "description": "通知正文"},
                    "severity": {"type": "string",
                                 "enum": ["info", "warning", "critical"], "default": "info",
                                 "description": "严重级别"},
                    "links": {"type": "array", "items": {"type": "object"},
                              "description": "相关链接"},
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                    "sent_at": {"type": "string"},
                    "status": {"type": "string"},
                },
            },
            "permission": "EXTERNAL_SIDE_EFFECT",
            "hitl_required": True,
            "timeout_ms": 30_000,
            "retry": {"max": 1, "backoff_ms": 500},
            "audit": True,
            "idempotent": False,
            "metadata": {
                "description": "发送告警/通知（写入本地 outbox，由通知适配器外发）",
                "impact": "对外发送消息，可能触达多人",
                "reversible": False,
                "approver": "值班工程师",
            },
            "handler": _notify_handler,
        },
    ]


def register_knowledge_ext_tools(registry: Any) -> list[str]:
    """把 5 个扩展工具注册到 core 的 ToolRegistry。

    与 ``knowledge.register_knowledge_tools``（注册 kb_search / kb_read）并列，
    提供「不经 tools 层」的注册路径。已注册过的工具名会被跳过（不覆盖）。

    Args:
        registry: Tool Registry 实例（需具备 ``register(spec)`` 方法）。

    Returns:
        实际注册的工具名列表。
    """
    if registry is None or not hasattr(registry, "register"):
        return []
    registered: list[str] = []
    for spec in ext_tool_specs():
        name = spec["name"]
        try:
            if hasattr(registry, "has") and registry.has(name):
                continue
        except Exception:  # noqa: BLE001
            pass
        try:
            registry.register(spec)
            registered.append(name)
        except ValueError:
            continue
        except Exception as exc:  # noqa: BLE001
            import logging

            logging.getLogger(__name__).error("扩展知识工具注册失败：%s", exc)
    return registered
