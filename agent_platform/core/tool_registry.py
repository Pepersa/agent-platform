# -*- coding: utf-8 -*-
"""ToolRegistry —— 平台核心资产：统一工具注册、校验、执行与审计。

对应设计文档：
    《01_架构方案_RAG_Agent.md》§2.5 工具层（Tool Registry 为核心资产）
        · 统一注册 / JSON-Schema 校验 / 版本管理 / 租户可见性 / 权限标签
        · 工具描述与版本：Registry 内版本表 + CI 校验（工具变更必须跑评测）
        · 权限与风险分级：只读 / 可写 / 可执行 / 高危
    《01_架构方案_RAG_Agent.md》§3.2 Agent 工具调用可靠性设计
        · 参数校验（JSON Schema 结构层）
        · 超时控制（工具整体 60s）
        · 失败归一 {code, message, retryable, hint}
        · HITL 人工闸门（≥写操作的动作挂起）
        · 熔断降级（连续失败）
    《02_场景方案_STDF分析与测试用例生成.md》§5 工具集设计（Agent Tool Contract）
        · §5.1 通用契约（name/description/input_schema/output_schema/
          permission/hitl_required/timeout_ms/retry/audit）
        · §5.2 工具清单中的权限级别定义

设计要点：
    1. 工具规范与实现分离：register(spec, impl)，spec 是"契约"（可审计/可文档化），
       impl 是可执行体。
    2. 重复注册直接报错——"工具变更必须走版本+回归评测"，不允许静默覆盖。
    3. 参数校验错误信息**面向模型**：必须带 hint，告诉模型怎么改。
    4. 工具结果裁剪：超过 max_tool_result_chars 时按 rows 优先截断，
       防止单个工具冲爆上下文（docs/01 §2.2 工具结果上限）。
    5. 超时保护基于 ThreadPoolExecutor + future.result(timeout)。
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

# 工具实现签名：(args: dict, ctx: dict) -> Any
ToolImpl = Callable[[Dict[str, Any], Dict[str, Any]], Any]


# ----------------------------------------------------------------------
# 权限与错误
# ----------------------------------------------------------------------
class Permission:
    """权限级别（docs/02 §5.2 权限级别定义）。

    READ_ONLY              只读数据，自动执行，记录审计
    COMPUTE                纯计算/模型推理，自动执行，成本计量
    WRITE_DATA             写库/写文件，幂等 + 回滚预案 + 审计
    EXTERNAL_SIDE_EFFECT   对外发消息/建工单，强制 HITL 确认
    ADMIN                  改配置/权限，仅管理员，强制 HITL
    """

    READ_ONLY = "READ_ONLY"
    COMPUTE = "COMPUTE"
    WRITE_DATA = "WRITE_DATA"
    EXTERNAL_SIDE_EFFECT = "EXTERNAL_SIDE_EFFECT"
    ADMIN = "ADMIN"

    ALL = (READ_ONLY, COMPUTE, WRITE_DATA, EXTERNAL_SIDE_EFFECT, ADMIN)

    #: 默认需要 HITL 人工确认的权限
    HITL_REQUIRED = (WRITE_DATA, EXTERNAL_SIDE_EFFECT, ADMIN)


class ToolError(Exception):
    """统一工具错误（docs/01 §3.2 失败归一）。

    对外结构固定为 {code, message, retryable, hint}，hint 面向模型。
    """

    def __init__(self, code: str, message: str, retryable: bool = False,
                 hint: str = "") -> None:
        self.code = code
        self.message = message
        self.retryable = bool(retryable)
        self.hint = hint
        super().__init__(f"[{code}] {message}")

    def to_dict(self) -> Dict[str, Any]:
        """统一错误结构，直接喂回模型。"""
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "hint": self.hint,
        }


# ----------------------------------------------------------------------
# 工具规范
# ----------------------------------------------------------------------
@dataclass
class ToolSpec:
    """工具契约（docs/02 §5.1 通用契约）。"""

    name: str
    description: str
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any] = field(default_factory=dict)
    permission: str = Permission.READ_ONLY
    #: None → 按 permission 自动推导（WRITE_DATA / EXTERNAL_SIDE_EFFECT / ADMIN 需 HITL）
    hitl_required: Optional[bool] = None
    timeout_ms: int = 60_000
    max_retries: int = 0
    version: str = "1.0.0"
    idempotent: bool = True
    #: 工具元信息：审批人、影响范围描述等（docs/02 §5.3 HITL 卡片字段）
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.hitl_required is None:
            self.hitl_required = self.permission in Permission.HITL_REQUIRED
        else:
            self.hitl_required = bool(self.hitl_required)
        if self.permission not in Permission.ALL:
            raise ValueError(
                f"工具 {self.name} 的 permission 非法: {self.permission}，"
                f"合法取值为 {Permission.ALL}"
            )

    @property
    def needs_hitl(self) -> bool:
        """是否需要 HITL 人工确认。"""
        return bool(self.hitl_required)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "permission": self.permission,
            "hitl_required": self.hitl_required,
            "timeout_ms": self.timeout_ms,
            "max_retries": self.max_retries,
            "version": self.version,
            "idempotent": self.idempotent,
            "metadata": self.metadata,
        }

    def openai_function(self) -> Dict[str, Any]:
        """转 OpenAI function calling schema。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema or {"type": "object", "properties": {}},
            },
        }


# ----------------------------------------------------------------------
# JSON Schema 校验（零依赖实现，覆盖契约所需的类型子集）
# ----------------------------------------------------------------------
_TYPE_MAP = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}


def _type_ok(value: Any, expected: str) -> bool:
    py = _TYPE_MAP.get(expected)
    if py is None:
        return True
    if expected in ("number", "integer") and isinstance(value, bool):
        # bool 是 int 的子类，但 JSON Schema 不视作数字
        return False
    if expected == "boolean":
        return isinstance(value, bool)
    return isinstance(value, py)


def validate_args(schema: Dict[str, Any], args: Dict[str, Any],
                  tool_name: str = "") -> List[str]:
    """按 JSON Schema 校验参数，返回错误信息列表（空表示通过）。

    覆盖：必填缺失、类型不符、enum 不符、additionalProperties=False 时未知参数、
    以及嵌套 object / array items 的基本校验。
    """
    errors: List[str] = []
    if not isinstance(args, dict):
        return [f"参数必须为 JSON 对象，实际为 {type(args).__name__}"]

    schema = schema or {}
    props: Dict[str, Any] = schema.get("properties", {}) or {}
    required: Sequence[str] = schema.get("required", []) or []

    # 1) 必填
    for key in required:
        if key not in args or args[key] is None:
            errors.append(f"缺少必填参数 '{key}'")

    # 2) 未知参数
    if schema.get("additionalProperties") is False:
        for key in args:
            if key not in props:
                errors.append(
                    f"参数 '{key}' 不在允许列表中；可用参数为：{sorted(props.keys())}"
                )

    # 3) 逐字段校验
    for key, value in args.items():
        sub = props.get(key)
        if sub is None:
            continue
        errors.extend(_validate_field(key, value, sub))
    return errors


def _validate_field(path: str, value: Any, schema: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if value is None:
        return errors

    # enum
    if "enum" in schema:
        allowed = schema["enum"]
        if value not in allowed:
            errors.append(
                f"参数 '{path}' 取值非法：{value!r}，合法取值为 {allowed}"
            )

    # 类型
    expected = schema.get("type")
    if isinstance(expected, str) and not _type_ok(value, expected):
        errors.append(
            f"参数 '{path}' 类型不符：期望 {expected}，实际 {type(value).__name__}"
        )
        return errors
    if isinstance(expected, list) and expected:
        if not any(_type_ok(value, e) for e in expected):
            errors.append(
                f"参数 '{path}' 类型不符：期望 {expected} 之一，实际 {type(value).__name__}"
            )
            return errors

    # 数值范围
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"参数 '{path}' 小于最小值 {schema['minimum']}（当前 {value}）")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"参数 '{path}' 大于最大值 {schema['maximum']}（当前 {value}）")

    # 字符串长度
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"参数 '{path}' 长度不足 {schema['minLength']}（当前 {len(value)}）")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"参数 '{path}' 长度超过 {schema['maxLength']}（当前 {len(value)}）")

    # 数组 items
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(value):
                errors.extend(_validate_field(f"{path}[{i}]", item, items))
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"参数 '{path}' 元素个数不足 {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"参数 '{path}' 元素个数超过 {schema['maxItems']}")

    # 嵌套 object
    if isinstance(value, dict):
        nprops = schema.get("properties") or {}
        nrequired = schema.get("required") or []
        for key in nrequired:
            if key not in value or value[key] is None:
                errors.append(f"参数 '{path}.{key}' 缺失（必填）")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in nprops:
                    errors.append(
                        f"参数 '{path}.{key}' 不在允许列表中；可用参数为：{sorted(nprops.keys())}"
                    )
        for key, sub in nprops.items():
            if key in value:
                errors.extend(_validate_field(f"{path}.{key}", value[key], sub))

    return errors


# ----------------------------------------------------------------------
# 工具注册表
# ----------------------------------------------------------------------
class ToolRegistry:
    """工具注册中心（核心资产）。

    线程安全；支持按名称/权限筛选；执行链为
        JSON Schema 校验 → 超时执行 → 结果裁剪 → 审计
    """

    def __init__(self, *, audit: Any = None,
                 max_tool_result_chars: int = 16_000,
                 executor_workers: int = 8) -> None:
        self._tools: Dict[str, ToolSpec] = {}
        self._impls: Dict[str, ToolImpl] = {}
        self._audit = audit
        self.max_tool_result_chars = int(max_tool_result_chars)
        self._executor = ThreadPoolExecutor(
            max_workers=executor_workers, thread_name_prefix="tool-exec")
        self._lock = threading.RLock()
        #: 熔断计数：{tool_name: 连续失败次数}
        self._consecutive_failures: Dict[str, int] = {}
        #: 熔断阈值（docs/01 §3.2：1 分钟内失败率 > 50% 或连续 10 次失败）
        self.circuit_breaker_threshold = 10

    # ---------------- 注册 ----------------
    def register(self, spec: Any, impl: Optional[ToolImpl] = None) -> ToolSpec:
        """注册工具。重复注册直接报错。

        报错文案对应 docs/01 §2.5「工具变更必须走版本+回归评测」：
        不允许静默覆盖已有契约。

        **兼容两种调用形态**（便于与 knowledge 层协作）：
            1. ``register(ToolSpec(...), impl)``            —— 本项目主用法
            2. ``register(spec_dict)`` —— spec_dict 内含 ``handler`` 字段
               （knowledge 层 ``register_knowledge_tools`` 使用该形态）
        """
        if isinstance(spec, dict):
            spec, impl = self._spec_from_dict(spec, impl)
        if not isinstance(spec, ToolSpec):
            raise TypeError("spec 必须是 ToolSpec 实例或包含 handler 的 dict")
        if impl is None:
            raise TypeError(f"工具 {spec.name} 缺少实现（impl / handler）")
        if not callable(impl):
            raise TypeError(f"工具 {spec.name} 的实现必须可调用")
        with self._lock:
            if spec.name in self._tools:
                raise ValueError(
                    f"工具 '{spec.name}' 已注册（当前版本 {self._tools[spec.name].version}）。"
                    f"工具变更必须走版本+回归评测流程，请使用新版本号或先下线旧版本。"
                )
            self._tools[spec.name] = spec
            self._impls[spec.name] = impl
        return spec

    @staticmethod
    def _spec_from_dict(raw: Dict[str, Any], impl: Optional[ToolImpl]) -> Any:
        """把 dict 形式的工具契约转为 ToolSpec。

        字段名对齐 docs/02 §5.1 通用契约：
            name / description / input_schema / output_schema / permission /
            hitl_required / timeout_ms / retry{"max"} / audit / handler
        """
        handler = raw.get("handler") or impl
        retry = raw.get("retry") or {}
        max_retries = retry.get("max", 0) if isinstance(retry, dict) else 0
        hitl = raw.get("hitl_required", None)
        return ToolSpec(
            name=raw["name"],
            description=raw.get("description", ""),
            input_schema=raw.get("input_schema") or {"type": "object", "properties": {}},
            output_schema=raw.get("output_schema") or {},
            permission=raw.get("permission", Permission.READ_ONLY),
            hitl_required=None if hitl is None else bool(hitl),
            timeout_ms=int(raw.get("timeout_ms", 60_000)),
            max_retries=int(max_retries or 0),
            version=str(raw.get("version", "1.0.0")),
            idempotent=bool(raw.get("idempotent", True)),
            metadata=raw.get("metadata") or {},
        ), handler

    def register_many(self, pairs: Sequence[Any]) -> None:
        """批量注册：(ToolSpec, impl) 序列。"""
        for item in pairs:
            spec, impl = item
            self.register(spec, impl)

    def unregister(self, name: str) -> None:
        """下线工具（运维用）。"""
        with self._lock:
            self._tools.pop(name, None)
            self._impls.pop(name, None)

    # ---------------- 查询 ----------------
    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> List[str]:
        return sorted(self._tools.keys())

    def list_specs(self) -> List[ToolSpec]:
        """返回全部工具规范（按名称排序）。"""
        return [self._tools[n] for n in sorted(self._tools.keys())]

    def list_specs_dict(self) -> List[Dict[str, Any]]:
        """返回全部工具规范的可序列化形式，用于生成文档 / GET /tools。"""
        return [s.to_dict() for s in self.list_specs()]

    def openai_schemas(self, only: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        """转 OpenAI function calling schema 列表。

        :param only: 仅导出指定工具名；None 表示全部。
        """
        with self._lock:
            names = sorted(self._tools.keys())
        if only is not None:
            wanted = set(only)
            names = [n for n in names if n in wanted]
        return [self._tools[n].openai_function() for n in names]

    # ---------------- 执行 ----------------
    def call(self, name: str, args: Dict[str, Any], *,
             session_id: Optional[str] = None,
             thread_id: Optional[str] = None,
             user_id: Optional[str] = None,
             actor: Optional[str] = None,
             model: Optional[str] = None,
             timeout_ms: Optional[int] = None) -> Any:
        """执行工具。

        执行链：JSON Schema 校验 → 执行（带超时） → 结果裁剪 → 审计。

        失败时抛 ToolError（结构见 docs/01 §3.2 失败归一）；
        成功返回工具原始结果（必要时为裁剪后的降级结构）。
        """
        started = time.monotonic()
        spec = self._tools.get(name)
        if spec is None:
            err = ToolError(
                "TOOL_NOT_FOUND",
                f"工具 '{name}' 不存在",
                retryable=False,
                hint=f"可用工具列表：{', '.join(self.names())}。请从列表中选择一个工具重新调用。",
            )
            self._audit_call(spec=None, name=name, args=args, status="not_found",
                             session_id=session_id, thread_id=thread_id,
                             user_id=user_id, actor=actor, model=model,
                             duration_ms=int((time.monotonic() - started) * 1000))
            raise err

        # --- 1. 参数校验（错误面向模型） ---
        errors = validate_args(spec.input_schema, args or {}, name)
        if errors:
            err = ToolError(
                "INVALID_ARGUMENTS",
                f"工具 '{name}' 参数校验失败：" + "；".join(errors),
                retryable=False,
                hint=self._build_hint(spec, errors),
            )
            self._audit_call(spec=spec, name=name, args=args, status="invalid_arguments",
                             session_id=session_id, thread_id=thread_id,
                             user_id=user_id, actor=actor, model=model,
                             duration_ms=int((time.monotonic() - started) * 1000),
                             error=err.to_dict())
            raise err

        # --- 2. 熔断检查 ---
        fails = self._consecutive_failures.get(name, 0)
        if fails >= self.circuit_breaker_threshold:
            err = ToolError(
                "CIRCUIT_OPEN",
                f"工具 '{name}' 连续失败 {fails} 次，已被熔断下线",
                retryable=True,
                hint=f"请改用其它工具或向用户说明 '{name}' 当前不可用，不要重复调用。",
            )
            self._audit_call(spec=spec, name=name, args=args, status="circuit_open",
                             session_id=session_id, thread_id=thread_id,
                             user_id=user_id, actor=actor, model=model,
                             duration_ms=int((time.monotonic() - started) * 1000),
                             error=err.to_dict())
            raise err

        impl = self._impls[name]
        eff_timeout = int(timeout_ms or spec.timeout_ms or 60_000) / 1000.0
        ctx = {
            "session_id": session_id,
            "thread_id": thread_id,
            "user_id": user_id,
            "actor": actor,
            "model": model,
            "tool_name": name,
        }

        # --- 3. 执行（超时保护） ---
        try:
            future = self._executor.submit(self._invoke, impl, dict(args or {}), ctx)
            try:
                result = future.result(timeout=eff_timeout)
            except FutureTimeoutError:
                future.cancel()
                err = ToolError(
                    "TIMEOUT",
                    f"工具 '{name}' 执行超时（>{eff_timeout:.0f}s）",
                    retryable=True,
                    hint=("请减少查询范围/缩小 top_k 后重试，或改用其它工具；"
                          "若确需长耗时操作，请先向用户说明。"),
                )
                self._audit_call(spec=spec, name=name, args=args, status="timeout",
                                 session_id=session_id, thread_id=thread_id,
                                 user_id=user_id, actor=actor, model=model,
                                 duration_ms=int((time.monotonic() - started) * 1000),
                                 error=err.to_dict())
                raise err
        except ToolError:
            raise
        except Exception as exc:  # 工具内部异常 → 归一
            detail = f"{type(exc).__name__}: {exc}"
            err = ToolError(
                "TOOL_EXECUTION_ERROR",
                f"工具 '{name}' 执行失败：{detail}",
                retryable=False,
                hint=("请根据错误信息修正参数后重试；若为数据缺失或环境问题，"
                      "请明确告知用户，不要凭空生成数据。"),
            )
            self._consecutive_failures[name] = self._consecutive_failures.get(name, 0) + 1
            self._audit_call(spec=spec, name=name, args=args, status="error",
                             session_id=session_id, thread_id=thread_id,
                             user_id=user_id, actor=actor, model=model,
                             duration_ms=int((time.monotonic() - started) * 1000),
                             error=err.to_dict())
            raise err

        # --- 4. 结果裁剪 ---
        result = self._truncate_result(spec, result)

        # --- 5. 审计 ---
        self._consecutive_failures[name] = 0
        self._audit_call(spec=spec, name=name, args=args, status="ok",
                         session_id=session_id, thread_id=thread_id,
                         user_id=user_id, actor=actor, model=model,
                         duration_ms=int((time.monotonic() - started) * 1000),
                         result=result)
        return result

    # ---------------- 内部：结果裁剪 ----------------
    def _truncate_result(self, spec: ToolSpec, result: Any) -> Any:
        """裁剪超长工具结果，防止单个工具冲爆上下文。

        规则（docs/01 §3.2 + §2.2）：
            · 结果是含 rows 列表的 dict → 截断 rows，并标注
              truncated / row_count_original / note
            · 其它情况 → 返回带 preview 的降级结构
        """
        try:
            size = len(json.dumps(result, ensure_ascii=False, default=str))
        except Exception:
            size = len(str(result))
        if size <= self.max_tool_result_chars:
            return result

        if isinstance(result, dict) and isinstance(result.get("rows"), list):
            rows = result["rows"]
            original = len(rows)
            out = dict(result)
            kept: List[Any] = []
            used = 0
            for row in rows:
                row_size = len(json.dumps(row, ensure_ascii=False, default=str))
                if used + row_size > self.max_tool_result_chars // 2 and kept:
                    break
                kept.append(row)
                used += row_size
            out["rows"] = kept
            out["truncated"] = True
            out["row_count_original"] = original
            out["row_count_returned"] = len(kept)
            out["note"] = (
                f"结果已截断：原始 {original} 行，返回 {len(kept)} 行。"
                f"如需完整数据请缩小查询范围或增加聚合条件。"
            )
            return out

        preview = str(result)[: self.max_tool_result_chars // 2]
        return {
            "truncated": True,
            "note": (
                f"结果过大（{size} 字符）已降级为预览。"
                f"请缩小查询范围（如减少 top_k / 加过滤条件）后重试。"
            ),
            "preview": preview,
            "original_size_chars": size,
        }

    # ---------------- 内部：实现调用 ----------------
    @staticmethod
    def _invoke(impl: ToolImpl, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """调用工具实现。

        兼容两种处理函数签名：
            · ``impl(args: dict, ctx: dict) -> Any``  —— 本项目主用法
            · ``impl(**kwargs) -> Any``               —— knowledge 层 handler 形态
        依据函数签名判断调用方式，避免用 try/except TypeError 误吞真实异常。
        """
        import inspect

        try:
            sig = inspect.signature(impl)
            params = list(sig.parameters.values())
            has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
            positional = [p for p in params
                          if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                        inspect.Parameter.POSITIONAL_OR_KEYWORD)]
            required_pos = [p for p in positional if p.default is inspect.Parameter.empty]
        except (TypeError, ValueError):
            return impl(args, ctx)

        # 形如 handler(**kwargs) 或 handler(query=..., top_k=...) → kwargs 形态
        if has_var_kw and not positional:
            return impl(**args)
        # 能接受两个位置参数 → (args, ctx) 形态
        if len(required_pos) >= 2 or len(positional) >= 2:
            return impl(args, ctx)
        if len(positional) == 1:
            return impl(args)
        if has_var_kw:
            return impl(**args)
        return impl(args, ctx)

    # ---------------- 内部：错误提示 ----------------
    @staticmethod
    def _build_hint(spec: ToolSpec, errors: List[str]) -> str:
        """生成面向模型的修复提示。"""
        props = (spec.input_schema or {}).get("properties", {}) or {}
        required = (spec.input_schema or {}).get("required", []) or []
        lines: List[str] = []
        joined = "；".join(errors)
        if "不在允许列表中" in joined or "缺少必填参数" in joined:
            lines.append(
                f"工具 '{spec.name}' 的入参必须严格符合 input_schema："
                f"必填参数 {sorted(required)}，"
                f"可选参数 {sorted(props.keys())}。"
            )
        if "类型不符" in joined:
            lines.append("请把参数类型改成 schema 中声明的类型后重试。")
        if "取值非法" in joined:
            lines.append("请从 schema 的 enum 列表中选取合法值。")
        lines.append(f"原始错误：{joined}")
        return " ".join(lines)

    # ---------------- 内部：审计 ----------------
    def _audit_call(self, *, spec: Optional[ToolSpec], name: str, args: Dict[str, Any],
                    status: str, session_id: Optional[str], thread_id: Optional[str],
                    user_id: Optional[str], actor: Optional[str], model: Optional[str],
                    duration_ms: int, result: Any = None, error: Any = None) -> None:
        if self._audit is None:
            return
        try:
            payload = {
                "action": "tool_call",
                "target": name,
                "args": args,
                "result": result if error is None else error,
                "status": status,
                "duration_ms": duration_ms,
                "session_id": session_id,
                "thread_id": thread_id,
                "user_id": user_id,
                "actor": actor or user_id,
                "model": model,
                "extra": {
                    "permission": spec.permission if spec else None,
                    "hitl_required": spec.hitl_required if spec else None,
                    "tool_version": spec.version if spec else None,
                },
            }
            self._audit.log(**payload)
        except Exception:
            pass  # 审计异常不影响工具返回

    # ---------------- 资源 ----------------
    def shutdown(self) -> None:
        """关闭线程池。"""
        try:
            self._executor.shutdown(wait=False)
        except Exception:
            pass
