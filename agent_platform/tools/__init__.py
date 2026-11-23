# -*- coding: utf-8 -*-
"""工具层（Tool Layer）—— 注册入口与薄封装。

对应设计文档：
    《01_架构方案_RAG_Agent.md》§2.5 工具层（Tool Registry 为核心资产）
    《02_场景方案_STDF分析与测试用例生成.md》§5 工具集设计（Agent Tool Contract）
        §5.1 通用契约 / §5.2 工具清单 / §5.3 HITL 交互规范

**与 knowledge 层的边界（重要，跨 worker 协作约定）**：

    本文件只负责「工具契约（ToolSpec）+ 薄封装」。
    真正的检索/抽取/渲染能力由 ``knowledge/`` 目录（另一位 worker 负责）提供。
    期望的 knowledge 层接口签名（duck typing，运行时探测）：

        # E:\PYPROJECT\agent\knowledge\__init__.py
        def kb_search(query: str, top_k: int = 8, filters: dict | None = None) -> dict
            -> {"chunks": [{"chunk_id": str, "text": str, "score": float,
                           "source": {"doc": str, "page": int, "section": str}}],
                "total": int}

        def tp_lookup(tp_name: str, tp_version: str | None = None,
                      test_num: str | None = None, keyword: str | None = None) -> dict
            -> {"tp_id": str, "version": str,
                "items": [{"test_num": str, "name": str, "unit": str,
                           "lower": float, "upper": float, "dft_method": str}]}

        def spec_extract(doc_id: str, sections: list | None = None,
                         param_codes: list | None = None) -> dict
            -> {"params": [{"param_code": str, "min": float, "typ": float,
                            "max": float, "unit": str, "condition": str,
                            "source": {"page": int, "quote": str}}],
                "gaps": []}

        def case_lint(cases: list, rules: list | None = None,
                      compare_history: bool = False) -> dict
            -> {"report": [{"case_id": str, "rule_id": str, "level": str,
                           "message": str, "evidence": str}],
                "summary": {"pass_rate": float, "coverage_matrix": {}}}

        def report_render(template: str, data: dict,
                          include_evidence: bool = True) -> dict
            -> {"report_id": str, "uri": str, "preview_md": str}

        def notify(channel: str, receivers: list, title: str, body: str,
                   severity: str = "info", links: list | None = None) -> dict
            -> {"message_id": str, "sent_at": str, "status": str}

    实际实现中，本模块会依次尝试：
        1. ``knowledge.<func>``
        2. ``knowledge.tools.<func>``
        3. ``knowledge.service.<func>``
    都找不到时使用内置的 mock 实现（返回明确标注 mock 的空结果），
    保证无 knowledge 层也能跑通冒烟测试。
"""

from __future__ import annotations

import importlib
import re
from typing import Any, Callable, Dict, List, Optional

from ..core.tool_registry import Permission, ToolError, ToolSpec
from ..core.workspace import WorkspaceError, get_workspace


# ----------------------------------------------------------------------
# 工作区（代码）工具适配
# ----------------------------------------------------------------------
def _workspace() -> Any:
    """取工作区单例（导入失败时返回 None，由调用方给出明确错误）。"""
    try:
        return get_workspace()
    except Exception:
        return None


def _ws_error(exc: WorkspaceError) -> ToolError:
    """把 WorkspaceError 转成 ToolError（错误面向模型）。"""
    return ToolError(
        exc.code,
        exc.message,
        retryable=bool(exc.retryable),
        hint=exc.hint or "请按错误提示修正参数后重试。",
    )


def _impl_ws_list_files(args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
    """ws_list_files：列出工作区目录（只读）。"""
    ws = _workspace()
    if ws is None:
        raise ToolError("WS_UNAVAILABLE", "工作区未就绪",
                        hint="请联系管理员检查 WORKSPACE_ROOT 配置。")
    try:
        return ws.tree(args.get("path", ""), depth=int(args.get("depth", 1) or 1))
    except WorkspaceError as exc:
        raise _ws_error(exc) from exc


def _impl_ws_read_file(args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
    """ws_read_file：读取工作区文件（只读，支持行范围）。"""
    ws = _workspace()
    if ws is None:
        raise ToolError("WS_UNAVAILABLE", "工作区未就绪",
                        hint="请联系管理员检查 WORKSPACE_ROOT 配置。")
    try:
        return ws.read_file(
            args.get("path", ""),
            start_line=args.get("start_line"),
            end_line=args.get("end_line"),
        )
    except WorkspaceError as exc:
        raise _ws_error(exc) from exc


def _impl_ws_search_code(args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
    """ws_search_code：工作区代码搜索（只读）。"""
    ws = _workspace()
    if ws is None:
        raise ToolError("WS_UNAVAILABLE", "工作区未就绪",
                        hint="请联系管理员检查 WORKSPACE_ROOT 配置。")
    try:
        return ws.search(
            args.get("query", ""),
            glob=str(args.get("glob", "") or ""),
            max_results=int(args.get("max_results", 50) or 50),
        )
    except WorkspaceError as exc:
        raise _ws_error(exc) from exc


def _impl_ws_write_file(args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
    """ws_write_file：整体覆盖写文件（WRITE_DATA，需人工确认）。

    **只有通过 HITL 审批后才会真正执行到这一步**（图的两段式设计：
    先判审批，再执行）。因此这里直接落盘即可。
    """
    ws = _workspace()
    if ws is None:
        raise ToolError("WS_UNAVAILABLE", "工作区未就绪",
                        hint="请联系管理员检查 WORKSPACE_ROOT 配置。")
    try:
        change = ws.stage_write(
            args.get("path", ""),
            args.get("content", ""),
            note=str(args.get("note", "") or ""),
        )
        result = ws.apply_change(change["change_id"], actor=str(ctx.get("actor") or "agent"))
        return {
            "status": "applied",
            "path": result["path"],
            "change_id": result["change_id"],
            "undo_id": result["undo_id"],
            "size": result["size"],
            "added": (change.get("diff") or {}).get("added", 0),
            "removed": (change.get("diff") or {}).get("removed", 0),
            "note": "已落盘；如需回滚请调用 POST /workspace/undo。",
        }
    except WorkspaceError as exc:
        raise _ws_error(exc) from exc


def _impl_ws_edit_file(args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
    """ws_edit_file：精确替换式编辑（WRITE_DATA，需人工确认）。

    比整文件重写更省 token、也更安全：只改动指定片段，
    其余内容逐字节保持不变。
    """
    ws = _workspace()
    if ws is None:
        raise ToolError("WS_UNAVAILABLE", "工作区未就绪",
                        hint="请联系管理员检查 WORKSPACE_ROOT 配置。")
    try:
        change = ws.stage_edit(
            args.get("path", ""),
            args.get("old_text", ""),
            args.get("new_text", ""),
            replace_all=bool(args.get("replace_all", False)),
            note=str(args.get("note", "") or ""),
        )
        result = ws.apply_change(change["change_id"], actor=str(ctx.get("actor") or "agent"))
        return {
            "status": "applied",
            "path": result["path"],
            "change_id": result["change_id"],
            "undo_id": result["undo_id"],
            "size": result["size"],
            "added": (change.get("diff") or {}).get("added", 0),
            "removed": (change.get("diff") or {}).get("removed", 0),
            "note": "已落盘；如需回滚请调用 POST /workspace/undo。",
        }
    except WorkspaceError as exc:
        raise _ws_error(exc) from exc

# ----------------------------------------------------------------------
# knowledge 层适配（duck typing）
# ----------------------------------------------------------------------
_KNOWLEDGE_MODULE_CANDIDATES = (
    "agent_platform.knowledge",
    "agent_platform.knowledge.tools",
    "agent_platform.knowledge.service",
    "knowledge",
    "knowledge.tools",
    "knowledge.service",
)


def _load_knowledge() -> Any:
    """尝试加载 knowledge 层模块（失败返回 None）。"""
    for mod_name in _KNOWLEDGE_MODULE_CANDIDATES:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        return mod
    return None


def _resolve(name: str) -> Optional[Callable[..., Any]]:
    """在 knowledge 层中解析函数 name。"""
    mod = _load_knowledge()
    if mod is None:
        return None
    fn = getattr(mod, name, None)
    if callable(fn):
        return fn
    # 也支持 knowledge 层用 ToolSpec 注册（返回可调用对象）
    return None


def _mock_result(name: str, **kwargs: Any) -> Dict[str, Any]:
    """knowledge 层未就绪时的占位结果（明确标注 mock）。"""
    return {
        "mock": True,
        "tool": name,
        "note": "knowledge 层尚未接入（TODO：由 knowledge worker 实现），返回占位结果。",
        "args": kwargs,
        "chunks": [],
        "items": [],
        "params": [],
        "gaps": [],
    }


# ----------------------------------------------------------------------
# 只读 SQL 校验
# ----------------------------------------------------------------------
_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|"
    r"replace|merge|copy|call|execute|vacuum|reindex|attach|detach|commit|"
    r"rollback|set|reset|lock|do)\b",
    re.IGNORECASE,
)


def validate_readonly_sql(sql: str) -> Dict[str, Any]:
    """只读 SQL 校验（docs/01 §2.5「SQL 工具：只读视图 + SQLGlot AST 校验 + 行数/超时限制」）。

    规则：
        · 只允许 SELECT / WITH 开头的单条语句
        · 禁止 DDL / DML / 事务控制 / 权限变更
        · 禁止多语句（分号分隔）

    返回 {"ok": bool, "code": str, "message": str, "hint": str}。
    优先使用 sqlglot 做 AST 校验；sqlglot 不可用时退化为正则保守校验。
    """
    if not isinstance(sql, str) or not sql.strip():
        return {
            "ok": False, "code": "INVALID_ARGUMENTS",
            "message": "SQL 为空",
            "hint": "请提供一条只读 SELECT 语句。",
        }

    text = sql.strip().rstrip(";")

    # 1) 多语句检查
    if ";" in text:
        return {
            "ok": False, "code": "SQL_MULTI_STATEMENT",
            "message": "不允许一次执行多条 SQL",
            "hint": "请只保留一条 SELECT/WITH 语句，去掉分号分隔的多语句。",
        }

    # 2) sqlglot AST 校验
    try:
        import sqlglot  # type: ignore
        try:
            parsed = sqlglot.parse_one(text, read=None)
        except Exception as exc:
            return {
                "ok": False, "code": "SQL_PARSE_ERROR",
                "message": f"SQL 语法解析失败：{exc}",
                "hint": "请检查 SQL 语法后重试。",
            }
        if parsed is None:
            return {
                "ok": False, "code": "SQL_PARSE_ERROR",
                "message": "SQL 解析结果为空",
                "hint": "请提供一条只读 SELECT 语句。",
            }
        root = parsed.__class__.__name__.upper()
        if root not in ("SELECT", "SELECTION"):
            return {
                "ok": False, "code": "SQL_NOT_READONLY",
                "message": f"只允许 SELECT / WITH 语句，当前为 {root}",
                "hint": "请改写为只读 SELECT 查询；数据修改由写工具在人工确认后执行。",
            }
        # 拒绝 CTE 中的写操作（WITH ... AS (INSERT ...)）
        with_sql = parsed.find(sqlglot.exp.With) if hasattr(sqlglot, "exp") else None
        if with_sql is not None:
            for node in with_sql.find_all(sqlglot.exp.Insert) if hasattr(sqlglot.exp, "Insert") else []:
                return {
                    "ok": False, "code": "SQL_NOT_READONLY",
                    "message": "WITH 子句中不允许写操作",
                    "hint": "请改写为纯 SELECT 查询。",
                }
        return {"ok": True, "code": "OK", "message": "只读校验通过", "hint": ""}
    except ImportError:
        pass
    except Exception:
        pass

    # 3) 正则保守校验（sqlglot 不可用）
    lowered = text.lower()
    if not re.match(r"^\s*(select|with)\b", lowered):
        return {
            "ok": False, "code": "SQL_NOT_READONLY",
            "message": "只允许 SELECT / WITH 语句",
            "hint": "请改写为只读 SELECT 查询。",
        }
    forbidden = _FORBIDDEN_SQL.search(lowered)
    if forbidden:
        return {
            "ok": False, "code": "SQL_NOT_READONLY",
            "message": f"检测到禁止的写操作关键字：{forbidden.group(0)}",
            "hint": "只允许 SELECT/WITH；写操作请走带人工确认的写工具。",
        }
    return {"ok": True, "code": "OK", "message": "只读校验通过", "hint": ""}


# ----------------------------------------------------------------------
# 各工具实现（薄封装：优先调用 knowledge 层）
# ----------------------------------------------------------------------
def _impl_kb_search(args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
    """kb_search：混合检索知识库（docs/02 §5.2）。"""
    fn = _resolve("kb_search")
    if fn is None:
        return _mock_result("kb_search", **args)
    try:
        return fn(query=args.get("query", ""), top_k=int(args.get("top_k", 8)),
                  filters=args.get("filters"))
    except TypeError:
        return fn(args.get("query", ""), args.get("top_k", 8))


def _impl_tp_lookup(args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
    """tp_lookup：查 TP 测试项与限值（docs/02 §5.2）。"""
    fn = _resolve("tp_lookup")
    if fn is None:
        return _mock_result("tp_lookup", **args)
    try:
        return fn(tp_name=args.get("tp_name", ""),
                  tp_version=args.get("tp_version"),
                  test_num=args.get("test_num"),
                  keyword=args.get("keyword"))
    except TypeError:
        return fn(args.get("tp_name", ""))


def _impl_sql_query(args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
    """sql_query：语义层只读查询（docs/02 §5.1 契约）。

    安全：SQL 强制只读校验（只允许 SELECT/WITH，禁 DDL/DML，禁多语句），
    行数与超时由参数控制。
    """
    sql = args.get("sql", "")
    check = validate_readonly_sql(sql)
    if not check["ok"]:
        raise ToolError(
            "SQL_NOT_READONLY",
            check["message"],
            retryable=False,
            hint=check["hint"],
        )

    fn = _resolve("sql_query")
    if fn is None:
        return {
            "columns": [],
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "elapsed_ms": 0,
            "source_tables": [],
            "mock": True,
            "note": "knowledge/数据层尚未接入（TODO），返回空结果集。",
            "purpose": args.get("purpose", ""),
        }
    max_rows = int(args.get("max_rows", 1000))
    timeout_ms = int(args.get("timeout_ms", 15000))
    try:
        result = fn(sql=sql, purpose=args.get("purpose", ""),
                    max_rows=max_rows, timeout_ms=timeout_ms)
    except TypeError:
        result = fn(sql)
    if isinstance(result, dict):
        rows = result.get("rows")
        if isinstance(rows, list) and len(rows) > max_rows:
            result = dict(result)
            result["rows"] = rows[:max_rows]
            result["truncated"] = True
            result["row_count_original"] = len(rows)
    return result


def _impl_spec_extract(args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
    """spec_extract：从规格文档抽取参数（docs/02 §5.2）。"""
    fn = _resolve("spec_extract")
    if fn is None:
        return _mock_result("spec_extract", **args)
    try:
        return fn(doc_id=args.get("doc_id", ""),
                  sections=args.get("sections"),
                  param_codes=args.get("param_codes"))
    except TypeError:
        return fn(args.get("doc_id", ""))


def _impl_case_lint(args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
    """case_lint：用例质量静态检查（docs/02 §4.4 R01–R10）。"""
    fn = _resolve("case_lint")
    if fn is None:
        return _mock_result("case_lint", **args)
    try:
        return fn(cases=args.get("cases", []), rules=args.get("rules"),
                  compare_history=bool(args.get("compare_history", False)))
    except TypeError:
        return fn(args.get("cases", []))


def _impl_report_render(args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
    """report_render：生成报告（WRITE_DATA，需 HITL）。"""
    fn = _resolve("report_render")
    if fn is None:
        return {
            "mock": True,
            "report_id": "mock-report-0001",
            "uri": "mock://report/mock-report-0001",
            "preview_md": "# 模拟报告\n\n（knowledge/渲染层尚未接入）",
            "note": "TODO：report_render 由 knowledge/render 层接管。",
        }
    try:
        return fn(template=args.get("template", "daily"), data=args.get("data", {}) or {},
                  include_evidence=bool(args.get("include_evidence", True)))
    except TypeError:
        return fn(args.get("template", "daily"), args.get("data", {}) or {})


def _impl_notify(args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
    """notify：发送告警/通知（EXTERNAL_SIDE_EFFECT，需 HITL）。"""
    fn = _resolve("notify")
    if fn is None:
        return {
            "mock": True,
            "message_id": "mock-msg-0001",
            "sent_at": "",
            "status": "mocked",
            "note": "TODO：notify 由通知适配层接管。",
        }
    try:
        return fn(channel=args.get("channel", "wecom"),
                  receivers=args.get("receivers", []) or [],
                  title=args.get("title", ""), body=args.get("body", ""),
                  severity=args.get("severity", "info"),
                  links=args.get("links"))
    except TypeError:
        return fn(args.get("channel", "wecom"), args.get("receivers", []) or [],
                  args.get("title", ""), args.get("body", ""))


class _InvalidSQL(Exception):
    """只读校验失败（保留供外部直接调用 validate_readonly_sql 的场景使用）。"""

    def __init__(self, message: str, hint: str) -> None:
        self.hint = hint
        super().__init__(message)


# ----------------------------------------------------------------------
# 工具规范定义
# ----------------------------------------------------------------------
def _kb_search_spec() -> ToolSpec:
    return ToolSpec(
        name="kb_search",
        description=(
            "在内部知识库（测试手册、SOP、告警码表、规格书等）做混合检索"
            "（BM25 + 向量），返回带 chunk_id 的候选片段。"
            "凡涉及测试参数、限值、告警码含义、操作规程的问答，必须先调用本工具查证。"
        ),
        input_schema={
            "type": "object",
            "required": ["query"],
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "minLength": 1,
                          "description": "自然语言查询或型号/告警码等精确关键词"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 50, "default": 8,
                           "description": "返回片段数"},
                "filters": {
                    "type": "object", "default": {},
                    "description": "检索过滤条件（产品线、文档类型、时间范围等）",
                    "additionalProperties": True,
                },
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "chunks": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
            },
        },
        permission=Permission.READ_ONLY,
        hitl_required=False,
        timeout_ms=30_000,
        max_retries=2,
        version="1.0.0",
        idempotent=True,
        metadata={"description": "检索知识库并返回可引用片段", "impact": "只读检索，无副作用"},
    )


def _sql_query_spec() -> ToolSpec:
    return ToolSpec(
        name="sql_query",
        description=(
            "在语义层只读数据仓库上执行只读 SQL，返回结构化结果集。"
            "只允许 SELECT/WITH 语句，禁止 DDL/DML 与多语句。"
            "用于良率、bin 分布、批量统计等数值聚合类问题。"
        ),
        input_schema={
            "type": "object",
            "required": ["sql", "purpose"],
            "additionalProperties": False,
            "properties": {
                "sql": {"type": "string", "minLength": 1,
                        "description": "只读 SELECT/WITH 语句，禁止 DDL/DML"},
                "purpose": {"type": "string", "minLength": 1,
                            "description": "该查询要回答的问题，用于审计与复核"},
                "max_rows": {"type": "integer", "minimum": 1, "maximum": 10000,
                             "default": 1000, "description": "最大返回行数"},
                "timeout_ms": {"type": "integer", "minimum": 100, "maximum": 120000,
                               "default": 15000, "description": "查询超时（毫秒）"},
                "metric_codes": {
                    "type": "array", "items": {"type": "string"},
                    "description": "引用的指标定义编码，便于口径追溯",
                },
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "columns": {"type": "array", "items": {"type": "object"}},
                "rows": {"type": "array"},
                "row_count": {"type": "integer"},
                "truncated": {"type": "boolean"},
                "elapsed_ms": {"type": "number"},
                "source_tables": {"type": "array", "items": {"type": "string"}},
            },
        },
        permission=Permission.READ_ONLY,
        hitl_required=False,
        timeout_ms=20_000,
        max_retries=2,
        version="1.0.0",
        idempotent=True,
        metadata={"description": "只读查询数仓", "impact": "只读查询，无数据写入"},
    )


def _tp_lookup_spec() -> ToolSpec:
    return ToolSpec(
        name="tp_lookup",
        description=(
            "查询测试程序（TP）中的测试项定义与限值（lower/upper/typ/unit/dft_method）。"
            "凡涉及某个测试项的限值、量程、单位，必须调用本工具查证，禁止凭记忆作答。"
        ),
        input_schema={
            "type": "object",
            "required": ["tp_name"],
            "additionalProperties": False,
            "properties": {
                "tp_name": {"type": "string", "minLength": 1,
                            "description": "测试程序名，如 STW-2100"},
                "tp_version": {"type": "string", "description": "TP 版本号"},
                "test_num": {"type": "string", "description": "测试项编号，如 TST_VDD_1"},
                "keyword": {"type": "string", "description": "测试项名称关键词"},
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "tp_id": {"type": "string"},
                "version": {"type": "string"},
                "items": {"type": "array", "items": {"type": "object"}},
            },
        },
        permission=Permission.READ_ONLY,
        hitl_required=False,
        timeout_ms=20_000,
        max_retries=2,
        version="1.0.0",
        idempotent=True,
        metadata={"description": "查 TP 测试项与限值", "impact": "只读查询，无副作用"},
    )


def _spec_extract_spec() -> ToolSpec:
    return ToolSpec(
        name="spec_extract",
        description=(
            "从器件规格书文档中抽取结构化参数（param_code / min / typ / max / unit / "
            "condition），并标注来源页码与原文引用；缺失参数写入 gaps。"
            "用于测试用例生成时的限值溯源（docs/02 §4.4 R01）。"
        ),
        input_schema={
            "type": "object",
            "required": ["doc_id"],
            "additionalProperties": False,
            "properties": {
                "doc_id": {"type": "string", "minLength": 1, "description": "规格书文档 ID"},
                "sections": {"type": "array", "items": {"type": "string"},
                             "description": "限定抽取的章节标题"},
                "param_codes": {"type": "array", "items": {"type": "string"},
                                "description": "限定抽取的参数编码"},
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "params": {"type": "array", "items": {"type": "object"}},
                "gaps": {"type": "array", "items": {"type": "string"}},
            },
        },
        permission=Permission.COMPUTE,
        hitl_required=False,
        timeout_ms=60_000,
        max_retries=1,
        version="1.0.0",
        idempotent=True,
        metadata={"description": "从规格书抽取参数", "impact": "纯计算，无副作用"},
    )


def _case_lint_spec() -> ToolSpec:
    return ToolSpec(
        name="case_lint",
        description=(
            "对测试用例集做质量静态检查（规则 R01–R10：限值溯源、单位、上下限合理性、"
            "步骤可复现、PVT 条件、采样量、失效模式覆盖、重复度、二值判定、与 TP 一致性），"
            "返回逐条问题与通过率摘要。"
        ),
        input_schema={
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
        output_schema={
            "type": "object",
            "properties": {
                "report": {"type": "array", "items": {"type": "object"}},
                "summary": {"type": "object"},
            },
        },
        permission=Permission.COMPUTE,
        hitl_required=False,
        timeout_ms=60_000,
        max_retries=1,
        version="1.0.0",
        idempotent=True,
        metadata={"description": "用例质量静态检查", "impact": "纯计算，无副作用"},
    )


def _report_render_spec() -> ToolSpec:
    return ToolSpec(
        name="report_render",
        description=(
            "按模板渲染结构化报告（daily / weekly / customer / spec），"
            "输出报告 ID、存储 URI 与 Markdown 预览。属于写操作，必须经人工确认。"
        ),
        input_schema={
            "type": "object",
            "required": ["template", "data"],
            "additionalProperties": False,
            "properties": {
                "template": {"type": "string", "enum": ["daily", "weekly", "customer", "spec"],
                             "description": "报告模板"},
                "data": {"type": "object", "description": "报告数据体（结论、数据范围、证据等）"},
                "include_evidence": {"type": "boolean", "default": True,
                                     "description": "是否包含证据/原始记录索引"},
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "report_id": {"type": "string"},
                "uri": {"type": "string"},
                "preview_md": {"type": "string"},
            },
        },
        permission=Permission.WRITE_DATA,
        hitl_required=None,   # → 自动推导为 True
        timeout_ms=60_000,
        max_retries=1,
        version="1.0.0",
        idempotent=True,
        metadata={
            "description": "生成结构化分析报告",
            "impact": "生成文件/记录，可能被下游引用",
            "reversible": True,
            "approver": "测试主管",
        },
    )


def _notify_spec() -> ToolSpec:
    return ToolSpec(
        name="notify",
        description=(
            "向指定渠道（wecom / feishu / email）向若干接收人发送告警或通知。"
            "属于对外副作用操作，必须经人工确认后方可执行。"
        ),
        input_schema={
            "type": "object",
            "required": ["channel", "receivers", "title", "body"],
            "additionalProperties": False,
            "properties": {
                "channel": {"type": "string", "enum": ["wecom", "feishu", "dingtalk", "email"],
                            "description": "通知渠道"},
                "receivers": {"type": "array", "minItems": 1, "items": {"type": "string"},
                              "description": "接收人列表（用户 ID 或邮箱）"},
                "title": {"type": "string", "minLength": 1, "description": "通知标题"},
                "body": {"type": "string", "minLength": 1, "description": "通知正文"},
                "severity": {"type": "string",
                             "enum": ["info", "warning", "critical"], "default": "info",
                             "description": "严重级别"},
                "links": {"type": "array", "items": {"type": "object"},
                          "description": "相关链接，如报告 URI"},
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "sent_at": {"type": "string"},
                "status": {"type": "string"},
            },
        },
        permission=Permission.EXTERNAL_SIDE_EFFECT,
        hitl_required=None,   # → 自动推导为 True
        timeout_ms=30_000,
        max_retries=1,
        version="1.0.0",
        idempotent=False,      # 通知不可重复发送 → 幂等键必须谨慎
        metadata={
            "description": "发送告警/通知",
            "impact": "对外发送消息，可能触达多人",
            "reversible": False,
            "approver": "值班工程师",
        },
    )


# ----------------------------------------------------------------------
# 工作区（代码）工具规范
# ----------------------------------------------------------------------
def _ws_list_files_spec() -> ToolSpec:
    return ToolSpec(
        name="ws_list_files",
        description=(
            "列出**工作区（本地代码仓库）**里的目录与文件，返回目录树。"
            "用于回答「这个仓库有哪些文件 / 某个目录下有什么」这类问题。"
            "**只读**，不会修改任何文件。"
            "返回的每一项都带 name / type(dir|file) / path / size / mtime / ext / language，"
            "其中 path 是**相对工作区根目录**的路径，可直接传给 ws_read_file。"
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string", "default": "",
                         "description": "相对工作区根目录的路径；留空表示根目录"},
                "depth": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1,
                          "description": "展开深度（1=只列当前层，2=再列一层子目录）"},
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "entries": {"type": "array", "items": {"type": "object"}},
            },
        },
        permission=Permission.READ_ONLY,
        hitl_required=False,
        timeout_ms=30_000,
        max_retries=1,
        version="1.0.0",
        idempotent=True,
        metadata={"description": "列出工作区目录树",
                  "impact": "只读浏览，不修改任何文件"},
    )


def _ws_read_file_spec() -> ToolSpec:
    return ToolSpec(
        name="ws_read_file",
        description=(
            "读取**工作区**里的一个文本文件内容，返回源码文本与行号信息。"
            "**只读**。文件超过 512KB 或为二进制文件时会明确报错。"
            "⚠️ **省 token 的正确姿势**：读大文件时用 start_line / end_line 只取需要的行段"
            "（例如读前 30 行传 start_line=1, end_line=30）。"
        ),
        input_schema={
            "type": "object",
            "required": ["path"],
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string", "minLength": 1,
                         "description": "相对工作区根目录的文件路径"},
                "start_line": {"type": "integer", "minimum": 1,
                               "description": "起始行号（1 起）；缺省表示从第一行开始"},
                "end_line": {"type": "integer", "minimum": 1,
                             "description": "结束行号（含）；缺省表示到文件末尾"},
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "encoding": {"type": "string"},
                "language": {"type": "string"},
                "line_count": {"type": "integer"},
                "truncated": {"type": "boolean"},
            },
        },
        permission=Permission.READ_ONLY,
        hitl_required=False,
        timeout_ms=30_000,
        max_retries=1,
        version="1.0.0",
        idempotent=True,
        metadata={"description": "读取工作区文件内容",
                  "impact": "只读浏览，不修改任何文件"},
    )


def _ws_search_code_spec() -> ToolSpec:
    return ToolSpec(
        name="ws_search_code",
        description=(
            "在**工作区代码**里做全文搜索（类似 grep），返回命中文件列表"
            "以及每处命中的**行号与行内容**。用于「某个函数/类名在哪些地方被用到」"
            "这类问题。**只读**。"
            "⚠️ 这是**代码搜索**，与知识库检索（kb_search）完全不同："
            "查代码里的符号请用本工具，不要用 kb_search。"
        ),
        input_schema={
            "type": "object",
            "required": ["query"],
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "minLength": 1,
                          "description": "要搜索的关键词（支持子串匹配）"},
                "glob": {"type": "string", "default": "",
                         "description": "文件名过滤（如 '*.py'）；留空表示全部"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 200,
                                "default": 50, "description": "最多返回的命中条数"},
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "hits": {"type": "array", "items": {"type": "object"}},
                "files": {"type": "array", "items": {"type": "object"}},
                "total_hits": {"type": "integer"},
                "truncated": {"type": "boolean"},
            },
        },
        permission=Permission.READ_ONLY,
        hitl_required=False,
        timeout_ms=60_000,
        max_retries=1,
        version="1.0.0",
        idempotent=True,
        metadata={"description": "在工作区代码中搜索关键词",
                  "impact": "只读搜索，不修改任何文件"},
    )


def _ws_write_file_spec() -> ToolSpec:
    return ToolSpec(
        name="ws_write_file",
        description=(
            "**整体覆盖**写入工作区里的一个文件（content 会替换文件的全部内容）。"
            "⚠️ 这是**写操作，会触发人工确认（HITL）**：系统会先挂起本次调用，"
            "把改动 diff 展示给用户，用户批准后才会真正落盘。"
            "⚠️ **优先使用 ws_edit_file**：只有需要新建文件、或需要重写整个文件时"
            "才用本工具；改几行代码请用 ws_edit_file（更省 token、更安全、"
            "不会误删你没打算改的内容）。"
        ),
        input_schema={
            "type": "object",
            "required": ["path", "content"],
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string", "minLength": 1,
                         "description": "相对工作区根目录的文件路径（必须是文本文件类型）"},
                "content": {"type": "string",
                            "description": "文件的新完整内容（会覆盖原有内容）"},
                "note": {"type": "string", "default": "",
                         "description": "改动说明（会展示在人工确认卡片上）"},
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "path": {"type": "string"},
                "change_id": {"type": "string"},
                "undo_id": {"type": "string"},
                "added": {"type": "integer"},
                "removed": {"type": "integer"},
            },
        },
        permission=Permission.WRITE_DATA,
        hitl_required=None,   # → 自动推导为 True（人工确认）
        timeout_ms=60_000,
        max_retries=0,
        version="1.0.0",
        idempotent=False,
        metadata={
            "description": "整体覆盖写入工作区文件（需人工确认）",
            "impact": "会修改磁盘上的文件内容（可撤销）",
            "reversible": True,
            "approver": "工作台用户",
        },
    )


def _ws_edit_file_spec() -> ToolSpec:
    return ToolSpec(
        name="ws_edit_file",
        description=(
            "**局部替换**式编辑工作区里的文件：把 path 中出现的 old_text 片段"
            "替换为 new_text，其余内容**逐字节保持不变**。"
            "⚠️ 这是**写操作，会触发人工确认（HITL）**：系统会先挂起本次调用，"
            "把改动 diff 展示给用户，用户批准后才会真正落盘。"
            "✅ **改代码首选本工具**（比 ws_write_file 更省 token、更安全）。"
            "old_text 必须**唯一匹配**；若出现多处会报错并返回候选行号，"
            "此时请补充更长的 old_text，或显式传 replace_all=true 替换全部。"
        ),
        input_schema={
            "type": "object",
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string", "minLength": 1,
                         "description": "相对工作区根目录的文件路径"},
                "old_text": {"type": "string", "minLength": 1,
                             "description": "要被替换的原文片段（必须唯一匹配，除非 replace_all=true）"},
                "new_text": {"type": "string",
                             "description": "替换后的新内容（可以是空串表示删除该片段）"},
                "replace_all": {"type": "boolean", "default": False,
                                "description": "是否替换所有出现位置（默认只替换唯一一处）"},
                "note": {"type": "string", "default": "",
                         "description": "改动说明（会展示在人工确认卡片上）"},
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "path": {"type": "string"},
                "change_id": {"type": "string"},
                "undo_id": {"type": "string"},
                "added": {"type": "integer"},
                "removed": {"type": "integer"},
            },
        },
        permission=Permission.WRITE_DATA,
        hitl_required=None,   # → 自动推导为 True（人工确认）
        timeout_ms=60_000,
        max_retries=0,
        version="1.0.0",
        idempotent=False,
        metadata={
            "description": "精确替换式编辑工作区文件（需人工确认）",
            "impact": "会修改磁盘上的文件内容（可撤销）",
            "reversible": True,
            "approver": "工作台用户",
        },
    )


# ----------------------------------------------------------------------
# 注册入口
# ----------------------------------------------------------------------
def register_all(registry: Any) -> List[ToolSpec]:
    """把所有内置工具注册进 registry。

    顺序说明：
        1. 先尝试接入 knowledge 层（若已就绪，则以 knowledge 的 kb_search 契约为准，
           避免与本模块的占位实现重复注册冲突）
        2. 再注册本模块自带的契约与薄封装

    :param registry: ToolRegistry 实例
    :return: 已注册的工具规范列表
    """
    registered: List[ToolSpec] = []

    # --- 1) knowledge 层接入（可选） ---
    knowledge_registered = register_knowledge_tools(registry)
    if knowledge_registered:
        print(f"[tools] knowledge 层已注册工具：{knowledge_registered}")

    # --- 1b) knowledge 层扩展工具（tp_lookup / spec_extract / case_lint /
    #         report_render / notify）—— 真实实现，权限与 HITL 标记见 docs/02 §5.2 ---
    ext_registered = register_knowledge_ext_tools(registry)
    if ext_registered:
        print(f"[tools] knowledge 扩展工具已注册：{ext_registered}")

    # --- 2) 本模块内置契约 ---
    pairs = [
        (_kb_search_spec(), _impl_kb_search),
        (_sql_query_spec(), _impl_sql_query),
        (_tp_lookup_spec(), _impl_tp_lookup),
        (_spec_extract_spec(), _impl_spec_extract),
        (_case_lint_spec(), _impl_case_lint),
        (_report_render_spec(), _impl_report_render),
        (_notify_spec(), _impl_notify),
        # --- 工作区（代码）工具 ---
        (_ws_list_files_spec(), _impl_ws_list_files),
        (_ws_read_file_spec(), _impl_ws_read_file),
        (_ws_search_code_spec(), _impl_ws_search_code),
        (_ws_write_file_spec(), _impl_ws_write_file),
        (_ws_edit_file_spec(), _impl_ws_edit_file),
    ]
    for spec, impl in pairs:
        try:
            registry.register(spec, impl)
            registered.append(spec)
        except ValueError:
            # 已注册（重复 register_all 调用 / knowledge 层已占用同名工具）→ 跳过
            continue
    return registered


def register_knowledge_tools(registry: Any) -> List[str]:
    """尝试调用 knowledge 层的 ``register_knowledge_tools(registry)``。

    knowledge 层约定签名：
        ``agent_platform.knowledge.register_knowledge_tools(registry) -> list[str]``
    其注册项为 dict 形式（含 ``handler`` 字段），本模块的
    ``ToolRegistry.register`` 已兼容该形态。

    返回实际注册成功的工具名列表（未就绪时为空列表）。
    """
    try:
        mod = importlib.import_module("agent_platform.knowledge")
    except Exception:
        return []
    fn = getattr(mod, "register_knowledge_tools", None)
    if not callable(fn):
        return []
    try:
        result = fn(registry)
        return list(result or [])
    except Exception as exc:
        print(f"[tools] knowledge 工具注册失败（降级为内置占位实现）：{exc}")
        return []


def register_knowledge_ext_tools(registry: Any) -> List[str]:
    """注册 knowledge 层的 5 个扩展工具（真实实现，非 mock）。

    对应 docs/02 §5.2 工具清单中的 ``tp_lookup`` / ``spec_extract`` /
    ``case_lint`` / ``report_render`` / ``notify``，实现位于
    ``agent_platform/knowledge/tools_ext.py``。

    权限级别与 HITL 标记：
        tp_lookup=READ_ONLY（无 HITL）、spec_extract=COMPUTE（无 HITL）、
        case_lint=COMPUTE（无 HITL）、report_render=WRITE_DATA（HITL✅）、
        notify=EXTERNAL_SIDE_EFFECT（HITL✅）。

    Args:
        registry: ToolRegistry 实例。

    Returns:
        实际注册成功的工具名列表（未就绪时为空列表）。
    """
    try:
        mod = importlib.import_module("agent_platform.knowledge")
    except Exception:
        return []
    fn = getattr(mod, "register_knowledge_ext_tools", None)
    if not callable(fn):
        return []
    try:
        return list(fn(registry) or [])
    except Exception as exc:
        print(f"[tools] knowledge 扩展工具注册失败：{exc}")
        return []


def register_workspace_tools(registry: Any) -> List[ToolSpec]:
    """单独注册工作区（代码）工具。

    与 :func:`register_all` 里的注册逻辑一致，拆出来是为了：
        · 测试可以只注册这 5 个工具；
        · 未来若要把工作区工具做成「可选插件」，只需调用本函数。

    Args:
        registry: ToolRegistry 实例。

    Returns:
        实际注册成功的工具规范列表。
    """
    pairs = [
        (_ws_list_files_spec(), _impl_ws_list_files),
        (_ws_read_file_spec(), _impl_ws_read_file),
        (_ws_search_code_spec(), _impl_ws_search_code),
        (_ws_write_file_spec(), _impl_ws_write_file),
        (_ws_edit_file_spec(), _impl_ws_edit_file),
    ]
    out: List[ToolSpec] = []
    for spec, impl in pairs:
        try:
            registry.register(spec, impl)
            out.append(spec)
        except ValueError:
            continue
    return out


__all__ = [
    "register_all",
    "register_knowledge_tools",
    "register_knowledge_ext_tools",
    "register_workspace_tools",
    "validate_readonly_sql",
    "Permission",
    "ToolSpec",
]
