# -*- coding: utf-8 -*-
"""自定义工具（用户自助加工具）—— 持久化 + 两种执行内核 + 权限/HITL 强制。

对应需求：
    任务 2「工具列表的自定义功能」。

设计要点：
    1. **两种 kind**
        · ``http``   —— 声明式 HTTP 工具：``{url, method, headers, body_template,
          result_path}``。调用时用标准库 urllib 发请求，把响应用
          ``result_path``（点号路径）取出来返回。支持 ``{args.xxx}`` 占位符渲染。
        · ``script`` —— 本地 Python 脚本工具：``{script_path, function_name,
          timeout_s}``。**在独立子进程**里执行（JSON over stdin/stdout），
          有超时与输出大小上限。

    2. **安全（script kind）**
        · 脚本在 **独立子进程** 执行，主进程只通过 stdin/stdout 传 JSON；
        · 绝不使用 ``eval`` / ``exec`` 执行用户提供的**代码字符串**；脚本文件
          以文件方式导入执行（``importlib.util.spec_from_file_location``），
          这是「信任用户本地脚本」能力的唯一实现路径；
        · 子进程超时即被 kill（``subprocess.run(timeout=...)``）；
        · stdout 大小上限（``MAX_OUTPUT_BYTES``），超限直接判失败；
        · 脚本只接收 JSON 参数，只返回 JSON 结果。

    3. **权限与 HITL**
        自定义工具的 ``permission`` / ``hitl_required`` 默认按用户设定，
        但当 ``permission`` ∈ {WRITE_DATA, EXTERNAL_SIDE_EFFECT, ADMIN} 时
        **强制** ``hitl_required=True``（用户无法关掉），并记录强制原因。

    4. **持久化**
        存 ``data/custom_tools.json``，**原子写**（写临时文件 + ``os.replace``）。

    5. **注册时机**
        新增 / 修改后**立即在当前进程生效**：ToolRegistry 的 ``register`` 在重名时
        会抛错，而 ``unregister(name)`` 可下线工具，因此管理器对**自己管理的**
        工具名执行「先 unregister 再 register」即可热替换，且**不会触碰内置工具**
        （内置工具名在 ``reserved_names`` 中保护，unregister 前会二次确认）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .tool_registry import Permission, ToolError, ToolSpec, validate_args

__all__ = [
    "CustomToolDef",
    "CustomToolManager",
    "CustomToolError",
    "CustomToolStore",
    "KIND_HTTP",
    "KIND_SCRIPT",
    "DEFAULT_DATA_FILE",
    "default_data_file",
]

#: 两种自定义工具类型
KIND_HTTP = "http"
KIND_SCRIPT = "script"
ALL_KINDS = (KIND_HTTP, KIND_SCRIPT)

#: 仓库根目录（agent_platform/core 的上两级）
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: 自定义工具持久化文件（原子写）
#:
#: 注意：默认路径走**统一数据目录解析**（``AGENT_DATA_DIR`` 可整体外置），
#: 不设环境变量时仍是 ``<repo>/data/custom_tools.json``（向后兼容）。
#: 由于需要**运行时**读取环境变量，这里用**函数**而不是常量。
def default_data_file() -> str:
    """返回自定义工具持久化文件的绝对路径。"""
    from . import paths as _paths  # noqa: PLC0415 - 延迟导入避免循环依赖

    return str(_paths.data_path("custom_tools.json"))


#: 自定义工具持久化文件（**模块导入时**的默认值，仅供展示 / 显式传参）
DEFAULT_DATA_FILE = os.path.join(_REPO_ROOT, "data", "custom_tools.json")

#: script kind：子进程输出上限（字节）
MAX_OUTPUT_BYTES = 1_000_000
#: script kind：默认超时（秒）
DEFAULT_SCRIPT_TIMEOUT_S = 15
#: script kind：超时上限（秒）
MAX_SCRIPT_TIMEOUT_S = 120
#: http kind：默认超时（秒）
DEFAULT_HTTP_TIMEOUT_S = 15

#: 工具名合法性：字母 / 数字 / 下划线，且不以数字开头
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

#: 占位符：{args.foo.bar}
_PLACEHOLDER_RE = re.compile(r"\{args\.([A-Za-z0-9_.\[\]]+)\}")


class CustomToolError(Exception):
    """自定义工具配置/执行错误（对外 4xx）。"""

    def __init__(self, message: str, *, code: str = "CUSTOM_TOOL_ERROR",
                 hint: str = "") -> None:
        self.code = code
        self.hint = hint
        super().__init__(message)


# ----------------------------------------------------------------------
# 定义模型
# ----------------------------------------------------------------------
@dataclass
class CustomToolDef:
    """一个自定义工具的定义。"""

    name: str
    description: str = ""
    input_schema: Dict[str, Any] = field(default_factory=lambda: {
        "type": "object", "properties": {},
    })
    kind: str = KIND_HTTP
    config: Dict[str, Any] = field(default_factory=dict)
    permission: str = Permission.READ_ONLY
    hitl_required: bool = False
    enabled: bool = True
    #: 创建 / 更新时间（秒级时间戳）
    created_at: float = 0.0
    updated_at: float = 0.0
    #: 强制 HITL 的原因（permission 为写/对外/管理员时自动填充）
    hitl_forced_reason: str = ""

    # ---------------- 序列化 ----------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "kind": self.kind,
            "config": self.config,
            "permission": self.permission,
            "hitl_required": self.hitl_required,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "hitl_forced_reason": self.hitl_forced_reason,
        }

    @staticmethod
    def from_dict(raw: Dict[str, Any]) -> "CustomToolDef":
        """从 dict 构造（容错：缺字段给默认值）。"""
        raw = raw or {}
        schema = raw.get("input_schema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        cfg = raw.get("config")
        if not isinstance(cfg, dict):
            cfg = {}
        return CustomToolDef(
            name=str(raw.get("name") or "").strip(),
            description=str(raw.get("description") or ""),
            input_schema=schema,
            kind=str(raw.get("kind") or KIND_HTTP).strip().lower(),
            config=cfg,
            permission=str(raw.get("permission") or Permission.READ_ONLY).strip().upper(),
            hitl_required=bool(raw.get("hitl_required", False)),
            enabled=bool(raw.get("enabled", True)),
            created_at=float(raw.get("created_at") or 0.0),
            updated_at=float(raw.get("updated_at") or 0.0),
            hitl_forced_reason=str(raw.get("hitl_forced_reason") or ""),
        )

    # ---------------- 校验 ----------------
    def validate(self) -> None:
        """校验定义；不合法抛 ``CustomToolError``。

        校验点：
            · 名称合法且不与内置工具重名（由管理器额外检查）；
            · kind 在 {http, script}；
            · permission 在 ToolRegistry 的合法集合内；
            · input_schema 是对象型 JSON Schema；
            · kind 专属配置齐全（http 需要 url；script 需要 script_path/function_name）。
        """
        if not _NAME_RE.match(self.name or ""):
            raise CustomToolError(
                f"工具名 '{self.name}' 非法：只允许字母/数字/下划线，且不能以数字开头（≤64 字符）",
                code="INVALID_NAME",
                hint="例如：http_health_check / script_demo",
            )
        if self.kind not in ALL_KINDS:
            raise CustomToolError(
                f"不支持的 kind：{self.kind!r}（可选：{', '.join(ALL_KINDS)}）",
                code="INVALID_KIND",
            )
        if self.permission not in Permission.ALL:
            raise CustomToolError(
                f"permission 非法：{self.permission!r}（可选：{', '.join(Permission.ALL)}）",
                code="INVALID_PERMISSION",
            )
        schema = self.input_schema
        if not isinstance(schema, dict):
            raise CustomToolError("input_schema 必须是 JSON 对象（JSON Schema）",
                                  code="INVALID_SCHEMA")
        if schema.get("type") not in (None, "object"):
            raise CustomToolError("input_schema.type 必须是 \"object\"",
                                  code="INVALID_SCHEMA")
        if not isinstance(schema.get("properties", {}), dict):
            raise CustomToolError("input_schema.properties 必须是对象",
                                  code="INVALID_SCHEMA")
        if self.kind == KIND_HTTP:
            if not str(self.config.get("url") or "").strip():
                raise CustomToolError("HTTP 工具必须填写 url", code="INVALID_CONFIG",
                                      hint="例如：http://127.0.0.1:8010/health")
        else:  # script
            if not str(self.config.get("script_path") or "").strip():
                raise CustomToolError("脚本工具必须填写 script_path", code="INVALID_CONFIG",
                                      hint="相对路径以仓库根目录为基准，例如：data/scripts/demo_tool.py")
            if not str(self.config.get("function_name") or "").strip():
                raise CustomToolError("脚本工具必须填写 function_name", code="INVALID_CONFIG",
                                      hint="脚本内需提供同名函数，签名为 fn(args: dict) -> Any")

    # ---------------- HITL 强制 ----------------
    def apply_hitl_policy(self) -> None:
        """按权限强制 HITL（用户无法关掉）。"""
        if self.permission in Permission.HITL_REQUIRED:
            self.hitl_required = True
            self.hitl_forced_reason = (
                f"权限 {self.permission} 属于「写数据 / 对外副作用 / 管理员」级别，"
                f"按平台安全策略**强制**开启人工确认（HITL），用户不可关闭。"
            )
        else:
            self.hitl_forced_reason = ""

    # ---------------- ToolSpec ----------------
    def to_spec(self) -> ToolSpec:
        """转成 core 的 ToolSpec（注册进 ToolRegistry 用）。"""
        timeout_ms = 30_000
        if self.kind == KIND_SCRIPT:
            secs = self.config.get("timeout_s") or DEFAULT_SCRIPT_TIMEOUT_S
            timeout_ms = int(max(1, min(float(secs), MAX_SCRIPT_TIMEOUT_S)) * 1000)
        else:
            secs = self.config.get("timeout_s") or DEFAULT_HTTP_TIMEOUT_S
            timeout_ms = int(max(1, min(float(secs), 120)) * 1000)
        impact = {
            KIND_HTTP: "对外发起 HTTP 请求，可能产生远端副作用",
            KIND_SCRIPT: "在本地子进程中执行用户脚本（信任用户本地脚本）",
        }.get(self.kind, "")
        return ToolSpec(
            name=self.name,
            description=self.description or f"自定义 {self.kind} 工具",
            input_schema=self.input_schema,
            permission=self.permission,
            hitl_required=bool(self.hitl_required),
            timeout_ms=timeout_ms,
            version="1.0.0",
            idempotent=(self.kind == KIND_HTTP),
            metadata={
                "description": self.description or f"自定义 {self.kind} 工具",
                "impact": impact,
                "reversible": self.kind != KIND_SCRIPT,
                "custom": True,
                "kind": self.kind,
                "approver": "平台管理员",
            },
        )


# ----------------------------------------------------------------------
# 占位符渲染与路径取值
# ----------------------------------------------------------------------
def _resolve_path(data: Any, path: str) -> Any:
    """按点号路径取值：``a.b.0.c``；取不到返回 ``None``。"""
    cur = data
    for part in str(path or "").split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
                continue
            except (ValueError, IndexError):
                return None
        if isinstance(cur, dict):
            if part in cur:
                cur = cur[part]
                continue
            return None
        return None
    return cur


def render_text(template: Any, args: Dict[str, Any]) -> str:
    """把 ``{args.xxx}`` 占位符渲染成字符串。

    标量按 ``str()`` 渲染；dict / list 按 JSON 渲染（便于嵌进 URL 或 JSON body）。
    """
    if not isinstance(template, str):
        return template
    def _sub(match: "re.Match[str]") -> str:
        value = _resolve_path(args or {}, match.group(1))
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    return _PLACEHOLDER_RE.sub(_sub, template)


def render_json_body(template: Any, args: Dict[str, Any]) -> Any:
    """渲染 body_template：优先渲染成合法 JSON，失败则返回原始字符串。"""
    if template is None:
        return None
    if not isinstance(template, str):
        return template
    text = render_text(template, args or {})
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except Exception:
        return text


# ----------------------------------------------------------------------
# HTTP 内核
# ----------------------------------------------------------------------
def run_http_tool(cfg: Dict[str, Any], args: Dict[str, Any]) -> Any:
    """执行声明式 HTTP 工具。

    Args:
        cfg: 配置 ``{url, method, headers, body_template, result_path}``。
        args: 工具入参。

    Returns:
        ``result_path`` 命中时返回该子对象；未命中返回整个响应体（JSON 或文本）。
    """
    url = render_text(str(cfg.get("url") or ""), args or {}).strip()
    if not url:
        raise CustomToolError("HTTP 工具 url 为空", code="INVALID_CONFIG")
    method = str(cfg.get("method") or "GET").strip().upper()
    timeout_s = float(cfg.get("timeout_s") or DEFAULT_HTTP_TIMEOUT_S)
    timeout_s = max(1.0, min(timeout_s, 120.0))

    headers: Dict[str, str] = {}
    raw_headers = cfg.get("headers") or {}
    if isinstance(raw_headers, dict):
        for k, v in raw_headers.items():
            headers[str(k)] = render_text(str(v), args or {})

    body: Any = None
    body_template = cfg.get("body_template")
    if body_template not in (None, ""):
        body = render_json_body(body_template, args or {})

    data: Optional[bytes] = None
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        else:
            data = str(body).encode("utf-8")

    req = urllib.request.Request(url=url, data=data, method=method, headers=headers)
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read(MAX_OUTPUT_BYTES)
            status = resp.status
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(2048).decode("utf-8", "replace")
        except Exception:
            pass
        raise CustomToolError(
            f"HTTP {exc.code}：{url} —— {detail[:500]}",
            code="HTTP_ERROR",
            hint="检查 url / method / headers / body_template 是否正确。",
        ) from exc
    except urllib.error.URLError as exc:
        raise CustomToolError(
            f"HTTP 请求失败：{url} —— {exc.reason}",
            code="HTTP_UNREACHABLE",
            hint="确认目标服务已启动且 url 可达（本机请用 127.0.0.1 而不是 localhost）。",
        ) from exc
    except Exception as exc:
        raise CustomToolError(f"HTTP 请求异常：{type(exc).__name__}: {exc}",
                              code="HTTP_ERROR") from exc

    text = raw.decode("utf-8", "replace")
    payload: Any
    try:
        payload = json.loads(text)
    except Exception:
        payload = text

    result_path = str(cfg.get("result_path") or "").strip()
    if result_path:
        found = _resolve_path(payload, result_path)
        if found is None:
            return {
                "matched": False,
                "result_path": result_path,
                "http_status": status,
                "elapsed_ms": int((time.time() - started) * 1000),
                "body_preview": text[:2000],
                "note": f"result_path '{result_path}' 未在响应中命中，已返回响应预览。",
            }
        return found

    return payload


# ----------------------------------------------------------------------
# script 内核（子进程隔离）
# ----------------------------------------------------------------------
def resolve_script_path(script_path: str) -> str:
    """把脚本路径解析为绝对路径。

    解析顺序（**向后兼容优先**）：
        1. 绝对路径 → 直接用；
        2. 仓库根相对路径（``data/scripts/demo_tool.py``）→ 以**仓库根**为基准
           （历史行为，自定义工具配置里存的就是这种相对路径）；
        3. 数据目录相对路径（``scripts/demo_tool.py``）→ 以**数据目录**为基准
           （数据目录被外置到别的盘时使用）。

    Args:
        script_path: 用户填写的脚本路径。

    Returns:
        绝对路径（不保证存在；存在性由调用方检查）。

    Raises:
        CustomToolError: ``script_path`` 为空。
    """
    p = str(script_path or "").strip()
    if not p:
        raise CustomToolError("script_path 为空", code="INVALID_CONFIG")
    if os.path.isabs(p):
        return os.path.normpath(p)

    from . import paths as _paths  # noqa: PLC0415 - 延迟导入避免循环依赖

    # 先试仓库根（历史相对路径，例如 data/scripts/demo_tool.py）
    repo_candidate = os.path.normpath(os.path.join(_REPO_ROOT, p))
    if os.path.isfile(repo_candidate):
        return repo_candidate
    # 再试数据目录（数据目录外置后的形态）
    data_candidate = os.path.normpath(str(_paths.data_path(p)))
    if os.path.isfile(data_candidate):
        return data_candidate
    # 都不存在 → 返回仓库根候选（报错信息里给出准确路径）
    return repo_candidate


def run_script_tool(cfg: Dict[str, Any], args: Dict[str, Any]) -> Any:
    """在**独立子进程**中执行本地脚本并返回 JSON 结果。

    协议：stdin 传 JSON ``{"script_path","function_name","args"}``，
    stdout 输出一行哨兵前缀 + JSON ``{"ok":bool,"result":...,"error":...}``。

    安全措施：
        · 独立子进程（主进程崩溃不影响脚本，脚本崩溃不影响主进程）；
        · 超时即 kill；
        · stdout 大小上限 ``MAX_OUTPUT_BYTES``；
        · 只走文件导入，**不使用 eval / exec 执行代码字符串**；
        · 参数与结果均为 JSON。
    """
    script_path = resolve_script_path(cfg.get("script_path", ""))
    if not os.path.isfile(script_path):
        raise CustomToolError(
            f"脚本文件不存在：{script_path}",
            code="SCRIPT_NOT_FOUND",
            hint="相对路径以仓库根目录为基准，例如 data/scripts/demo_tool.py",
        )
    function_name = str(cfg.get("function_name") or "").strip()
    if not function_name:
        raise CustomToolError("function_name 为空", code="INVALID_CONFIG")

    timeout_s = float(cfg.get("timeout_s") or DEFAULT_SCRIPT_TIMEOUT_S)
    timeout_s = max(1.0, min(timeout_s, float(MAX_SCRIPT_TIMEOUT_S)))

    payload = {
        "script_path": script_path,
        "function_name": function_name,
        "args": args or {},
    }
    cmd = [sys.executable, "-m", "agent_platform.core.script_runner"]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = _REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")

    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            input=json.dumps(payload, ensure_ascii=False, default=str),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            cwd=_REPO_ROOT,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise CustomToolError(
            f"脚本执行超时（>{timeout_s:.0f}s），已被终止",
            code="SCRIPT_TIMEOUT",
            hint="减少脚本工作量，或调大 config.timeout_s（上限 120s）。",
        ) from None
    except Exception as exc:
        raise CustomToolError(f"脚本子进程启动失败：{type(exc).__name__}: {exc}",
                              code="SCRIPT_LAUNCH_ERROR") from exc

    if len(proc.stdout or "") > MAX_OUTPUT_BYTES:
        raise CustomToolError(
            f"脚本输出超过上限（{MAX_OUTPUT_BYTES} 字节），已拒绝",
            code="SCRIPT_OUTPUT_TOO_LARGE",
            hint="请让脚本只返回必要数据（不要打印大段日志）。",
        )

    from .script_runner import RESULT_MARKER

    stdout = proc.stdout or ""
    idx = stdout.rfind(RESULT_MARKER)
    if idx < 0:
        stderr = (proc.stderr or "")[-800:]
        raise CustomToolError(
            f"脚本未返回结果（退出码 {proc.returncode}）。stderr：{stderr}",
            code="SCRIPT_ERROR",
            hint="确认脚本中定义了函数 %s(args: dict) 并返回可 JSON 序列化的结果。" % function_name,
        )
    try:
        envelope = json.loads(stdout[idx + len(RESULT_MARKER):].strip())
    except Exception as exc:
        raise CustomToolError(f"脚本结果解析失败：{exc}", code="SCRIPT_ERROR") from exc

    if not envelope.get("ok"):
        raise CustomToolError(
            f"脚本执行失败：{envelope.get('error')}",
            code="SCRIPT_ERROR",
            hint="查看脚本内部异常信息并修正后重试。",
        )
    result = envelope.get("result")
    if isinstance(result, dict):
        result = dict(result)
        result.setdefault("_elapsed_ms", int((time.time() - started) * 1000))
    return result


# ----------------------------------------------------------------------
# 工具实现工厂
# ----------------------------------------------------------------------
def build_impl(defn: CustomToolDef) -> Callable[[Dict[str, Any], Dict[str, Any]], Any]:
    """构造工具实现（``(args, ctx) -> result``）。"""

    def _impl(args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        if defn.kind == KIND_HTTP:
            return run_http_tool(defn.config, args or {})
        return run_script_tool(defn.config, args or {})

    _impl.__name__ = f"custom_{defn.name}"
    return _impl


# ----------------------------------------------------------------------
# 持久化（原子写）
# ----------------------------------------------------------------------
class CustomToolStore:
    """自定义工具的 JSON 文件存储（原子写：临时文件 + os.replace）。"""

    def __init__(self, path: Optional[str] = None) -> None:
        # 默认走统一数据目录解析（``AGENT_DATA_DIR`` 可整体外置）；
        # 不设环境变量时仍是 ``<repo>/data/custom_tools.json``（向后兼容）。
        self.path = os.path.abspath(path or default_data_file())
        self._lock = threading.RLock()

    def load(self) -> List[CustomToolDef]:
        """读取全部定义；文件不存在返回空列表。"""
        with self._lock:
            if not os.path.isfile(self.path):
                return []
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
            except Exception:
                return []
        items = raw.get("tools") if isinstance(raw, dict) else raw
        out: List[CustomToolDef] = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            dfn = CustomToolDef.from_dict(it)
            if dfn.name:
                out.append(dfn)
        return out

    def save(self, items: List[CustomToolDef]) -> None:
        """原子写：先写同目录临时文件，再 ``os.replace`` 覆盖。"""
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            payload = {
                "version": 1,
                "updated_at": time.time(),
                "tools": [d.to_dict() for d in items],
            }
            fd, tmp_path = tempfile.mkstemp(
                prefix=".custom_tools_", suffix=".tmp",
                dir=os.path.dirname(self.path))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=2)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_path, self.path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise


# ----------------------------------------------------------------------
# 管理器
# ----------------------------------------------------------------------
class CustomToolManager:
    """自定义工具管理器：CRUD + 持久化 + 立即注册生效。"""

    def __init__(self, *, store: Optional[CustomToolStore] = None,
                 registry: Any = None,
                 reserved_names: Optional[List[str]] = None) -> None:
        self.store = store or CustomToolStore()
        self.registry = registry
        #: 内置工具名（受保护，禁止重名 / 禁止 unregister）
        self.reserved_names: set = set(reserved_names or [])
        self._lock = threading.RLock()
        self._items: Dict[str, CustomToolDef] = {}
        #: 已成功注册进 ToolRegistry 的自定义工具名（用于下线时精确 unregister）
        self._registered: set = set()
        self.reload()

    # ---------------- 加载 / 保存 ----------------
    def reload(self) -> List[CustomToolDef]:
        """从磁盘重新加载（并重新注册）。"""
        with self._lock:
            self._items = {d.name: d for d in self.store.load()}
            self._sync_registry()
            return list(self._items.values())

    def _persist(self) -> None:
        self.store.save(list(self._items.values()))

    # ---------------- 注册表同步 ----------------
    def _sync_registry(self) -> None:
        """把当前定义同步进 ToolRegistry（**立即生效**）。

        做法：
            · 下线：之前注册过、现在不存在或被停用的自定义工具 → ``unregister``；
            · 上线 / 替换：``unregister`` 再 ``register``（ToolRegistry 的 register
              对重名会抛错，所以先下线再上线）。

        **内置工具绝不受影响**：内置工具名保存在 ``reserved_names`` 中，
        永不被本方法 unregister。
        """
        if self.registry is None:
            return
        try:
            # 1) 下线：不再启用 / 已删除的自定义工具
            for name in list(self._registered):
                if name in self.reserved_names:
                    continue
                if name not in self._items or not self._items[name].enabled:
                    try:
                        self.registry.unregister(name)
                    except Exception:
                        pass
                    self._registered.discard(name)

            # 2) 上线 / 替换
            for dfn in self._items.values():
                if not dfn.enabled:
                    continue
                try:
                    if self.registry.has(dfn.name):
                        self.registry.unregister(dfn.name)
                    self.registry.register(dfn.to_spec(), build_impl(dfn))
                    self._registered.add(dfn.name)
                except Exception:
                    # 单个工具注册失败不阻断其它工具
                    self._registered.discard(dfn.name)
                    continue
        except Exception:
            pass

    # ---------------- 查询 ----------------
    def list(self) -> List[CustomToolDef]:
        with self._lock:
            return list(self._items.values())

    def get(self, name: str) -> Optional[CustomToolDef]:
        with self._lock:
            return self._items.get(name)

    # ---------------- 写操作 ----------------
    def create(self, payload: Dict[str, Any]) -> CustomToolDef:
        """新建自定义工具（重名 → 冲突错误）。"""
        dfn = CustomToolDef.from_dict(payload)
        dfn.validate()
        with self._lock:
            if dfn.name in self.reserved_names:
                raise CustomToolError(
                    f"工具名 '{dfn.name}' 与内置工具重名，禁止使用",
                    code="NAME_CONFLICT",
                    hint="换一个名字，例如加前缀 http_ / script_ / user_。",
                )
            if dfn.name in self._items:
                raise CustomToolError(
                    f"自定义工具 '{dfn.name}' 已存在",
                    code="NAME_CONFLICT",
                    hint="请改名，或用 PUT /custom-tools/{name} 更新。",
                )
            dfn.apply_hitl_policy()
            now = time.time()
            dfn.created_at = now
            dfn.updated_at = now
            self._items[dfn.name] = dfn
            self._persist()
            self._sync_registry()
            return dfn

    def update(self, name: str, payload: Dict[str, Any]) -> CustomToolDef:
        """更新自定义工具（改名视为删除旧名 + 新建新名）。"""
        with self._lock:
            old = self._items.get(name)
            if old is None:
                raise CustomToolError(f"自定义工具 '{name}' 不存在", code="NOT_FOUND")
            merged = old.to_dict()
            merged.update({k: v for k, v in (payload or {}).items()
                           if k not in ("created_at",)})
            dfn = CustomToolDef.from_dict(merged)
            dfn.created_at = old.created_at
            dfn.validate()
            if dfn.name != name:
                if dfn.name in self.reserved_names or dfn.name in self._items:
                    raise CustomToolError(
                        f"工具名 '{dfn.name}' 已存在或与内置工具重名",
                        code="NAME_CONFLICT",
                    )
            dfn.apply_hitl_policy()
            dfn.updated_at = time.time()
            self._items.pop(name, None)
            self._items[dfn.name] = dfn
            self._persist()
            self._sync_registry()
            return dfn

    def delete(self, name: str) -> None:
        """删除自定义工具（并从注册表下线）。"""
        with self._lock:
            if name not in self._items:
                raise CustomToolError(f"自定义工具 '{name}' 不存在", code="NOT_FOUND")
            self._items.pop(name, None)
            self._persist()
            self._sync_registry()

    def set_enabled(self, name: str, enabled: bool) -> CustomToolDef:
        """启用 / 停用。"""
        with self._lock:
            dfn = self._items.get(name)
            if dfn is None:
                raise CustomToolError(f"自定义工具 '{name}' 不存在", code="NOT_FOUND")
            dfn.enabled = bool(enabled)
            dfn.updated_at = time.time()
            self._persist()
            self._sync_registry()
            return dfn

    # ---------------- 试运行 ----------------
    def test(self, name: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """**试运行**：不进入 Agent 回路，直接调用一次并返回真实结果 / 错误。

        Returns:
            ``{"ok":bool,"result":Any,"error":{...}|None,"elapsed_ms":int}``
        """
        dfn = self.get(name)
        if dfn is None:
            raise CustomToolError(f"自定义工具 '{name}' 不存在", code="NOT_FOUND")
        args = args if isinstance(args, dict) else {}
        errors = validate_args(dfn.input_schema, args, dfn.name)
        if errors:
            return {
                "ok": False,
                "result": None,
                "error": {
                    "code": "INVALID_ARGUMENTS",
                    "message": "；".join(errors),
                    "hint": "按 input_schema 修正示例参数后重试。",
                },
                "elapsed_ms": 0,
            }
        started = time.time()
        try:
            impl = build_impl(dfn)
            result = impl(args, {})
            return {
                "ok": True,
                "result": result,
                "error": None,
                "elapsed_ms": int((time.time() - started) * 1000),
            }
        except CustomToolError as exc:
            return {
                "ok": False,
                "result": None,
                "error": {"code": exc.code, "message": str(exc), "hint": exc.hint},
                "elapsed_ms": int((time.time() - started) * 1000),
            }
        except ToolError as exc:
            return {
                "ok": False,
                "result": None,
                "error": exc.to_dict(),
                "elapsed_ms": int((time.time() - started) * 1000),
            }
        except Exception as exc:
            return {
                "ok": False,
                "result": None,
                "error": {
                    "code": "CUSTOM_TOOL_ERROR",
                    "message": f"{type(exc).__name__}: {exc}",
                    "hint": "检查工具配置是否正确。",
                },
                "elapsed_ms": int((time.time() - started) * 1000),
            }
