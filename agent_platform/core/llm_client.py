# -*- coding: utf-8 -*-
"""LLM 客户端与模型路由。

对应设计文档：
    《01_架构方案_RAG_Agent.md》§2.3 路由层 →「模型路由」
        A. 自研 ModelRouter + OpenAI 兼容接口（可控、可插拔）
        原则 6「模型可替换」：通过 Model Router 隔离厂商
    《01_架构方案_RAG_Agent.md》§2.9 推理模型层（vLLM/SGLang 提供 OpenAI 兼容接口）
    《03_显存与模型选型计算表.md》（本地 GGUF + llama.cpp 推理后端背景）

本地推理端点（Windows 原生 llama.cpp llama-server，非 vLLM/SGLang）：
    主模型：gpt-oss-20b      → http://127.0.0.1:8080/v1
    副模型：qwen2.5-14b      → http://127.0.0.1:8081/v1（由其它 worker 负责启动）

gpt-oss 系列特性：
    · 推理过程单独放在 reasoning_content 字段，**不得混入对话历史**
    · tool_calls 承载工具调用
    · 推理强度通过 chat_template_kwargs={"reasoning_effort": ...} 调节
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


# ----------------------------------------------------------------------
# Windows WMI 兜底：避免 truststore / openai SDK 导入时卡死
# ----------------------------------------------------------------------
#: ``openai`` SDK → ``httpx2`` → ``httpcore2._ssl`` → ``truststore._api`` 会调用
#: ``platform.win32_ver()``，而后者在 Windows 上走 **WMI 查询**
#: （``platform._wmi_query``）。若本机 WMI 服务异常（实测：查询会无限期挂起），
#: 构造 ``AsyncOpenAI`` 就会**永久阻塞**事件循环，表现为「/health 一直不返回」。
#:
#: 本地 llama-server 走 **明文 HTTP**，根本用不到 TLS 信任库；
#: 因此这里在导入 openai SDK **之前**把 ``platform.uname`` 换成一个
#: **不查 WMI** 的轻量实现，既避免卡死，又不影响功能。
def _patch_platform_uname_no_wmi() -> None:
    """把 ``platform.uname`` 换成不依赖 WMI 的实现（幂等）。"""
    try:
        import platform
    except Exception:  # pragma: no cover - 理论上不会发生
        return
    if getattr(platform, "_agent_platform_no_wmi_patched", False):
        return
    try:
        # 若 WMI 已经可用（正常机器），就不动它
        import subprocess

        subprocess.run(["echo"], capture_output=True, timeout=5)
    except Exception:  # pragma: no cover - 仅用于探测
        pass

    def _uname() -> Any:
        """返回一个 ``uname_result`` 兼容对象（不查 WMI）。"""
        import collections

        fields = ("system", "node", "release", "version", "machine", "processor")
        try:
            uname_result = getattr(platform, "uname_result", None)
            if uname_result is not None:
                return uname_result("Windows", "localhost", "", "", "", "")
        except Exception:  # pragma: no cover - 不同 Python 版本字段不同
            pass
        return collections.namedtuple(
            "uname_result", " ".join(fields)
        )("Windows", "localhost", "", "", "", "")

    try:
        platform.uname = _uname  # type: ignore[assignment]
        platform._agent_platform_no_wmi_patched = True  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - 只读属性等
        pass

    # ``openai._base_client.get_platform()`` → ``platform.platform()`` →
    # ``platform.win32_ver(version)``，同样会走 WMI。这里一并替换成
    # **不查 WMI** 的轻量实现。
    def _win32_ver(release: str = "") -> Any:
        """返回 ``(release, version, csd, ptype)``，不查询 WMI。

        Args:
            release: 形参占位（真实实现用它做版本判定），此处忽略。
        """
        import sys as _sys

        rel = (release or "") or (
            f"{_sys.version_info.major}.{_sys.version_info.minor}"
            f".{_sys.version_info.micro}")
        return (rel, "", "", "Workstation")

    try:
        platform.win32_ver = _win32_ver  # type: ignore[assignment]
    except Exception:  # pragma: no cover
        pass


_patch_platform_uname_no_wmi()

# 允许的推理强度
REASONING_EFFORTS = ("low", "medium", "high")

# ----------------------------------------------------------------------
# 模型能力标签（Capability Tags）
# ----------------------------------------------------------------------
#: **能力标签**清单。用于校验 fallback 链的成员是否具备该任务类型
#: 所要求的能力，防止「代码任务静默降级到不具备代码能力的模型」。
#:
#: 设计动机（见 docs/06_代码模型接入策略.md）：
#:     原来的 ``agent`` / ``tool_call`` 路由链是 ``[gpt-oss-20b, qwen2.5-14b]``。
#:     ``qwen2.5-14b`` 是**通用对话模型**（Qwen2.5-14B-Instruct），**不具备**
#:     专门的代码能力。一旦主模型失败，代码类任务会落到它身上，
#:     产出质量会「静默」崩塌 —— 用户看到的只是一段看起来能跑、
#:     实则不靠谱的代码，没有任何报错提示。
#:
#: 解决方式：给每个模型打**能力标签**，路由时校验 fallback 链成员
#: 是否具备该路由项要求的能力；不具备的成员会被**从链里剔除**，
#: 若剔除后链为空，则**显式抛错**而不是静默降级。
CAPABILITIES: tuple = (
    "code",       # 代码生成 / 编辑 / 重构
    "tool_call",  # OpenAI 风格 function calling
    "reasoning",  # 复杂推理（含 CoT / reasoning_content）
    "chinese",    # 中文生成
    "long_context",  # 长上下文（>32K）
    "vision",     # 多模态（图文输入）
)

#: **默认能力标签**（模型未在 routing.yaml 显式声明 ``capabilities`` 时使用）。
#:
#: 取值原则：**保守但可用**。
#:   · ``gpt-oss-20b`` 是 OpenAI 的推理+代码模型，官方模型卡明确覆盖
#:     SWE-bench Verified / Codeforces / τ-bench function calling，
#:     因此给它 ``code`` + ``tool_call`` + ``reasoning`` 全部标签。
#:   · ``qwen2.5-14b`` 是**通用对话模型**（非 Coder 型号），**刻意不给**
#:     ``code`` 标签 —— 这正是本次改造要保护的那条边界。
#:   · 未知模型（如用户自定义注册的别名）**不给任何标签**，
#:     这样它只会出现在「不要求特定能力」的路由（如 ``default``）里，
#:     一旦被用作代码类任务的备选就会被剔除 —— 宁可显式报错，
#:     也不要静默降级。
DEFAULT_CAPABILITIES: Dict[str, List[str]] = {
    "gpt-oss-20b": ["code", "tool_call", "reasoning", "chinese"],
    "qwen2.5-14b": ["tool_call", "reasoning", "chinese"],
}

#: **路由项 → 所需能力标签**（多标签为「与」关系）。
#:
#: 只有列在这里的路由项会做能力校验；未列出的路由项（如 ``default``、
#: ``chinese``、``eval``、``summarize``）视为**无能力约束**，
#: fallback 链原样保留。
ROUTE_REQUIREMENTS: Dict[str, List[str]] = {
    # 代码类：写代码 / 改代码 / 重构 —— 必须有 code 能力
    "code": ["code"],
    # 工具调用类：必须有 tool_call 能力
    "tool_call": ["tool_call"],
    # Agent 主循环：既要推理又要调工具（写代码时会调 ws_write_file）
    "agent": ["code", "tool_call"],
    # 复杂推理
    "reasoning": ["reasoning"],
    # 只读问答 / 规划：只要推理即可
    "ask": ["reasoning"],
    "plan": ["reasoning"],
}


class RoutingCapabilityError(RuntimeError):
    """fallback 链里没有具备所需能力的模型（**显式失败**，不静默降级）。

    典型场景：代码类任务的主模型（如尚未安装的 code 模型）不可用，
    而 fallback 链里的备选模型都不具备 ``code`` 能力 ——
    此时必须让调用方看到明确错误，而不是把代码任务丢给通用对话模型。
    """


# 默认路由表（可被 config/routing.yaml 覆盖）
# task_type → 主模型 → 备选链
DEFAULT_ROUTES: Dict[str, List[str]] = {
    # 复杂推理 / 工具调用 / ReAct 主循环
    "reasoning": ["gpt-oss-20b"],
    "agent": ["gpt-oss-20b"],
    "tool_call": ["gpt-oss-20b"],
    # 只读问答模式（Ask）
    "ask": ["gpt-oss-20b"],
    # 规划模式（Plan）：只产出结构化计划，不调用工具
    "plan": ["gpt-oss-20b"],
    # 代码专用路由（**占位**：模型尚未安装，见 docs/06）
    #   主模型 qwen2.5-coder-14b 需先按文档安装；装好后此路由自动生效。
    #   降级链里只放具备 code 能力的模型 —— 当前唯一是 gpt-oss-20b。
    "code": ["qwen2.5-coder-14b", "gpt-oss-20b"],
    # 中文生成 / 评测 / 摘要
    "chinese": ["qwen2.5-14b", "gpt-oss-20b"],
    "eval": ["qwen2.5-14b", "gpt-oss-20b"],
    "summarize": ["qwen2.5-14b", "gpt-oss-20b"],
    "default": ["gpt-oss-20b", "qwen2.5-14b"],
}


@dataclass
class LLMConfig:
    """LLM 连接配置（全部可由环境变量覆盖）。"""

    base_url: str = "http://127.0.0.1:8080/v1"
    model: str = "gpt-oss-20b"
    reasoning_effort: str = "medium"
    temperature: float = 0.2
    max_tokens: int = 2048
    api_key: str = "local"
    timeout: float = 180.0
    #: 备用模型配置（fallback 链）
    fallbacks: List["LLMConfig"] = field(default_factory=list)
    #: **模型能力标签**（如 ``["code", "tool_call", "reasoning"]``）。
    #: 由 routing.yaml 的 ``models.<name>.capabilities`` 声明；
    #: 未声明时回落到 :data:`DEFAULT_CAPABILITIES`（见 ``LLMRouter``）。
    capabilities: List[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, **overrides: Any) -> "LLMConfig":
        """从环境变量读取配置。

        环境变量：
            LLM_BASE_URL           默认 http://127.0.0.1:8080/v1
            LLM_MODEL              默认 gpt-oss-20b
            LLM_REASONING_EFFORT   默认 medium（low|medium|high）
            LLM_TEMPERATURE        默认 0.2
            LLM_MAX_TOKENS         默认 2048
            LLM_API_KEY            默认 local
            LLM_TIMEOUT            默认 180
            LLM_FALLBACK_BASE_URL  可选，备用模型端点
            LLM_FALLBACK_MODEL     可选，备用模型名
        """
        cfg = cls(
            base_url=os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8080/v1").rstrip("/"),
            model=os.environ.get("LLM_MODEL", "gpt-oss-20b"),
            reasoning_effort=os.environ.get("LLM_REASONING_EFFORT", "medium").lower(),
            temperature=float(os.environ.get("LLM_TEMPERATURE", "0.2")),
            max_tokens=int(os.environ.get("LLM_MAX_TOKENS", "2048")),
            api_key=os.environ.get("LLM_API_KEY", "local"),
            timeout=float(os.environ.get("LLM_TIMEOUT", "180")),
        )
        if cfg.reasoning_effort not in REASONING_EFFORTS:
            cfg.reasoning_effort = "medium"
        fb_url = os.environ.get("LLM_FALLBACK_BASE_URL", "").strip()
        fb_model = os.environ.get("LLM_FALLBACK_MODEL", "").strip()
        if fb_url and fb_model:
            cfg.fallbacks = [cls(base_url=fb_url.rstrip("/"), model=fb_model,
                                 api_key=cfg.api_key, timeout=cfg.timeout)]
        for k, v in overrides.items():
            if v is not None:
                setattr(cfg, k, v)
        return cfg


def _safe_json(raw: Any) -> Dict[str, Any]:
    """容错解析 tool call arguments。

    模型偶尔会输出 markdown 围栏包裹的 JSON、Python 风格的单引号、
    或者前后带解释文字。这里逐级兜底，保证整条轨迹不崩。
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    text = str(raw).strip()
    if not text:
        return {}
    # 1) 直接解析
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    # 2) 去掉 markdown 围栏
    fenced = re.sub(r"^```(?:json|JSON)?\s*", "", text)
    fenced = re.sub(r"\s*```$", "", fenced).strip()
    try:
        parsed = json.loads(fenced)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    # 3) 抽取第一个 {...} 平衡括号片段
    start = fenced.find("{")
    if start >= 0:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(fenced)):
            ch = fenced[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(fenced[start:i + 1])
                        if isinstance(parsed, dict):
                            return parsed
                    except Exception:
                        break
                    break
    # 4) 单引号兜底
    try:
        import ast
        parsed = ast.literal_eval(fenced)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return {}


def _extract_text(obj: Any) -> str:
    """尽力从各种响应结构里取文本。"""
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        for key in ("content", "text", "output", "message"):
            if key in obj and obj[key]:
                return _extract_text(obj[key])
    return str(obj)


class LLMClient:
    """OpenAI 兼容的 LLM 客户端（基于 openai 异步 SDK）。

    固定返回结构：
        {"role": "assistant", "content": str, "reasoning_content": str,
         "tool_calls": [{"id","name","args"}],
         "usage": {"prompt","completion","total"}}
    """

    def __init__(self, config: Optional[LLMConfig] = None, *, client: Any = None,
                 max_retries: int = 2,
                 http_client: Any = None) -> None:
        self.config = config or LLMConfig.from_env()
        self._client = client  # 便于注入 mock 做测试
        self._owns_client = client is None
        #: **可选的底层 httpx 客户端**（健康探测专用）。
        #:
        #: 健康探测需要两件普通对话不需要的东西：
        #:   1. ``trust_env=False`` —— **绕开系统代理**。本机 WinINET 里开着
        #:      ``ProxyEnable=1``（127.0.0.1:17997），httpx 默认会读注册表
        #:      把**所有**请求（包括 127.0.0.1 的 llama-server）都塞给代理；
        #:      代理再连不通的本地端口要 2~4 秒才吐 502，而直连立刻 RST。
        #:   2. **很短的 connect 超时** —— 端口没人监听时要快速失败。
        #:
        #: 只有显式传了才用；普通聊天路径保持 openai SDK 的默认行为。
        self._http_client = http_client
        #: **重试次数**。健康探测用 ``max_retries=0`` ——
        #: 探测只是「现在通不通」，重试 2 次会让失败探测耗时翻 3 倍
        #: （实测未监听端口 21 秒 → 拖垮整个 /models 端点）。
        self._max_retries = max_retries
        #: **按事件循环缓存的 AsyncOpenAI 实例**。
        #:
        #: httpx 的异步连接池在**首次使用**时绑定到当时的事件循环。一旦
        #: ``/health``（主循环）先用过客户端，随后 ``chat_sync`` 在线程池里
        #: ``asyncio.run()`` 起**新循环**再复用同一个客户端，就会抛
        #: ``RuntimeError: Event loop is closed``（实测现象）。
        #: 这里按「当前运行中的事件循环」分别缓存客户端，跨循环自动新建。
        self._clients_by_loop: Dict[int, Any] = {}

    # ------------------------------------------------------------------
    @property
    def client(self) -> Any:
        """懒加载 AsyncOpenAI（依赖缺失时给出明确错误）。

        **按事件循环分别缓存**：httpx 异步连接池绑定创建它的事件循环，
        跨循环复用会抛 ``Event loop is closed``。
        """
        if self._client is not None:
            # 外部注入的客户端（测试用）：直接返回，不做循环隔离
            return self._client
        try:
            loop_id = id(asyncio.get_running_loop())
        except RuntimeError:
            # 没有运行中的循环（如同步上下文）：用固定 key
            loop_id = 0
        existing = self._clients_by_loop.get(loop_id)
        if existing is not None:
            return existing
        try:
            from openai import AsyncOpenAI  # type: ignore
        except Exception as exc:  # pragma: no cover - 环境缺依赖
            raise RuntimeError(
                "未安装 openai 包，无法访问 LLM。请执行：pip install openai"
            ) from exc
        created = AsyncOpenAI(
            base_url=self.config.base_url,
            api_key=self.config.api_key,
            timeout=self.config.timeout,
            max_retries=self._max_retries,
            **({"http_client": self._http_client} if self._http_client is not None else {}),
        )
        self._clients_by_loop[loop_id] = created
        return created

    # ------------------------------------------------------------------
    async def chat(self, messages: Sequence[Dict[str, Any]], *,
                   tools: Optional[Sequence[Dict[str, Any]]] = None,
                   effort: Optional[str] = None,
                   temperature: Optional[float] = None,
                   max_tokens: Optional[int] = None,
                   model: Optional[str] = None) -> Dict[str, Any]:
        """发起一次 chat completion，返回归一化结构。

        :param messages: OpenAI 风格消息列表。
        :param tools: OpenAI function calling schema 列表。
        :param effort: 推理强度 low|medium|high；None 用配置默认值。
        """
        eff = (effort or self.config.reasoning_effort or "medium").lower()
        if eff not in REASONING_EFFORTS:
            eff = "medium"
        temp = self.config.temperature if temperature is None else temperature
        max_tok = self.config.max_tokens if max_tokens is None else max_tokens
        model_name = model or self.config.model

        kwargs: Dict[str, Any] = {
            "model": model_name,
            "messages": list(messages),
            "temperature": temp,
            "max_tokens": max_tok,
            # llama.cpp / gpt-oss 通过 chat_template_kwargs 传推理强度
            "extra_body": {"chat_template_kwargs": {"reasoning_effort": eff}},
        }
        if tools:
            kwargs["tools"] = list(tools)
            kwargs["tool_choice"] = "auto"

        resp = await self.client.chat.completions.create(**kwargs)
        return self.parse_response(resp)

    # ------------------------------------------------------------------
    @staticmethod
    def parse_response(resp: Any) -> Dict[str, Any]:
        """把 SDK 响应对象归一化为固定结构。"""
        choice = resp.choices[0]
        message = choice.message

        content = getattr(message, "content", None) or ""
        reasoning = (getattr(message, "reasoning_content", None)
                     or getattr(message, "reasoning", None) or "")

        tool_calls: List[Dict[str, Any]] = []
        for tc in (getattr(message, "tool_calls", None) or []):
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", None) or getattr(tc, "name", "") or ""
            raw_args = getattr(fn, "arguments", None)
            if raw_args is None:
                raw_args = getattr(tc, "arguments", None)
            tool_calls.append({
                "id": getattr(tc, "id", None) or f"call_{len(tool_calls)}",
                "name": name,
                "args": _safe_json(raw_args),
            })

        usage_obj = getattr(resp, "usage", None)
        prompt_tokens = int(getattr(usage_obj, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage_obj, "completion_tokens", 0) or 0)
        total_tokens = int(getattr(usage_obj, "total_tokens", 0) or 0) or (
            prompt_tokens + completion_tokens)

        return {
            "role": "assistant",
            "content": content if isinstance(content, str) else _extract_text(content),
            "reasoning_content": reasoning if isinstance(reasoning, str) else _extract_text(reasoning),
            "tool_calls": tool_calls,
            "usage": {
                "prompt": prompt_tokens,
                "completion": completion_tokens,
                "total": total_tokens,
            },
        }

    # ------------------------------------------------------------------
    async def models(self) -> List[Dict[str, Any]]:
        """列出服务端可用模型（/v1/models）。"""
        resp = await self.client.models.list()
        data = getattr(resp, "data", None)
        if data is None and isinstance(resp, dict):
            data = resp.get("data")
        out: List[Dict[str, Any]] = []
        for m in (data or []):
            if isinstance(m, dict):
                out.append(m)
            else:
                out.append({
                    "id": getattr(m, "id", ""),
                    "object": getattr(m, "object", "model"),
                    "owned_by": getattr(m, "owned_by", ""),
                })
        return out

    async def health(self) -> Dict[str, Any]:
        """健康检查：能否拿到 /v1/models。"""
        try:
            models = await self.models()
            return {
                "reachable": True,
                "base_url": self.config.base_url,
                "model": self.config.model,
                "models": [m.get("id") for m in models],
            }
        except Exception as exc:
            return {
                "reachable": False,
                "base_url": self.config.base_url,
                "model": self.config.model,
                "error": f"{type(exc).__name__}: {exc}",
            }

    # ------------------------------------------------------------------
    # 同步桥接：LangGraph 节点为同步函数时使用
    # ------------------------------------------------------------------
    def chat_sync(self, messages: Sequence[Dict[str, Any]], **kwargs: Any) -> Dict[str, Any]:
        """同步调用包装。

        · 无运行中的事件循环 → 直接 asyncio.run
        · 已在事件循环中 → 放到独立线程跑一个新的事件循环，避免嵌套报错
        """
        coro = self.chat(messages, **kwargs)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()


# ----------------------------------------------------------------------
# 模型路由
# ----------------------------------------------------------------------
class LLMRouter:
    """模型路由（docs/01 §2.3）。

    · 按任务类型选择主模型
    · 主模型失败时沿 fallback 链降级
    · 路由表可来自 config/routing.yaml（可选），否则用代码内常量
    """

    def __init__(self, *, routes: Optional[Dict[str, List[str]]] = None,
                 configs: Optional[Dict[str, LLMConfig]] = None,
                 routing_file: Optional[str] = None) -> None:
        self.routes: Dict[str, List[str]] = dict(routes or DEFAULT_ROUTES)
        self.configs: Dict[str, LLMConfig] = dict(configs or {})
        self.clients: Dict[str, LLMClient] = {}
        if routing_file:
            self._load_routing_file(routing_file)

    # ---------------- 路由表加载 ----------------
    def _load_routing_file(self, path: str) -> None:
        """加载 config/routing.yaml（可选依赖 pyyaml，缺失则忽略）。"""
        try:
            import yaml  # type: ignore
        except Exception:
            return
        try:
            if not os.path.exists(path):
                return
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            return
        if not isinstance(data, dict):
            return
        routes = data.get("routes")
        if isinstance(routes, dict):
            for k, v in routes.items():
                if isinstance(v, dict):
                    chain = v.get("fallback") or v.get("models") or []
                    if isinstance(chain, list):
                        self.routes[str(k)] = [str(x) for x in chain]
                elif isinstance(v, list):
                    self.routes[str(k)] = [str(x) for x in v]
        models = data.get("models")
        if isinstance(models, dict):
            for name, conf in models.items():
                if not isinstance(conf, dict):
                    continue
                cfg = LLMConfig(
                    base_url=str(conf.get("base_url", "http://127.0.0.1:8080/v1")).rstrip("/"),
                    model=str(conf.get("model", name)),
                    reasoning_effort=str(conf.get("reasoning_effort", "medium")).lower(),
                    temperature=float(conf.get("temperature", 0.2)),
                    max_tokens=int(conf.get("max_tokens", 2048)),
                    api_key=str(conf.get("api_key", "local")),
                    timeout=float(conf.get("timeout", 180)),
                )
                # 能力标签：yaml 显式声明优先，否则用内置默认表
                caps = conf.get("capabilities")
                if isinstance(caps, list):
                    cfg.capabilities = [str(c).strip() for c in caps if str(c).strip()]
                else:
                    cfg.capabilities = list(DEFAULT_CAPABILITIES.get(str(name), []))
                self.configs[str(name)] = cfg

    # ---------------- 能力标签 ----------------
    def capabilities_for(self, model_name: str) -> List[str]:
        """取某模型的能力标签。

        优先级：
            1. routing.yaml ``models.<name>.capabilities``（已写进 ``self.configs``）
            2. 内置 :data:`DEFAULT_CAPABILITIES`
            3. 空列表（**未知模型没有任何能力** → 不会被用作代码类备选）
        """
        if model_name in self.configs:
            caps = self.configs[model_name].capabilities
            if caps:
                return list(caps)
        return list(DEFAULT_CAPABILITIES.get(model_name, []))

    def has_capability(self, model_name: str, capability: str) -> bool:
        """某模型是否具备指定能力。"""
        return capability in self.capabilities_for(model_name)

    def required_capabilities(self, task_type: str) -> List[str]:
        """某路由项要求的能力标签（空列表 = 无能力约束）。"""
        return list(ROUTE_REQUIREMENTS.get(task_type, []))

    def filtered_chain_for(self, task_type: str) -> List[str]:
        """返回**经过能力校验**的 fallback 链。

        剔除链中不具备该路由项所要求能力的模型。
        若剔除后链为空 → 返回空列表（调用方应**显式报错**，
        绝不静默降级到不具备能力的模型）。
        """
        chain = self.chain_for(task_type)
        required = self.required_capabilities(task_type)
        if not required:
            return chain
        return [m for m in chain
                if all(self.has_capability(m, cap) for cap in required)]

    def validate_routes(self) -> List[str]:
        """自检：返回所有「违规降级」的问题描述列表（空列表 = 全部合法）。

        违规定义：某个路由项声明了能力要求，但它的 fallback 链里
        存在**不具备**该能力的模型（会引发静默质量崩塌）。
        """
        problems: List[str] = []
        for task_type in sorted(self.routes):
            required = self.required_capabilities(task_type)
            if not required:
                continue
            for model_name in self.chain_for(task_type):
                missing = [c for c in required
                           if not self.has_capability(model_name, c)]
                if missing:
                    problems.append(
                        f"路由 '{task_type}' 的降级链包含模型 '{model_name}'，"
                        f"它缺少能力 {missing}（要求 {required}）"
                    )
        return problems

    # ---------------- 路由 ----------------
    def chain_for(self, task_type: str) -> List[str]:
        """返回某任务类型的 fallback 链（模型名列表）。"""
        if task_type in self.routes:
            return list(self.routes[task_type])
        return list(self.routes.get("default", ["gpt-oss-20b"]))

    def config_for(self, model_name: str) -> LLMConfig:
        """取某模型的连接配置（未登记则用主配置并改 model 名）。"""
        if model_name in self.configs:
            cfg = self.configs[model_name]
            # 保证 config 里带能力标签（未从 yaml 加载时也能拿到默认值）
            if not cfg.capabilities:
                cfg.capabilities = list(DEFAULT_CAPABILITIES.get(model_name, []))
            return cfg
        base = LLMConfig.from_env()
        base.model = model_name
        base.capabilities = list(DEFAULT_CAPABILITIES.get(model_name, []))
        return base

    def client_for(self, model_name: str) -> LLMClient:
        """取（或懒创建）某模型的客户端。"""
        if model_name not in self.clients:
            self.clients[model_name] = LLMClient(self.config_for(model_name))
        return self.clients[model_name]

    def route(self, task_type: str = "default") -> LLMClient:
        """按任务类型选出主模型客户端。

        **能力校验**：若该路由项有能力要求，而链首模型不具备这些能力，
        则跳到链中第一个具备能力的模型；若一个都没有，
        抛 :class:`RoutingCapabilityError`（**显式失败**，不静默降级）。
        """
        chain = self.chain_for(task_type)
        if not chain:
            chain = ["gpt-oss-20b"]
        required = self.required_capabilities(task_type)
        if required:
            chain = [m for m in chain
                     if all(self.has_capability(m, cap) for cap in required)]
            if not chain:
                raise RoutingCapabilityError(
                    f"路由 '{task_type}' 要求能力 {required}，"
                    f"但其降级链 {self.chain_for(task_type)} 中没有任何模型具备这些能力。"
                    f"请安装具备相应能力的模型（见 docs/06_代码模型接入策略.md），"
                    f"或调整 config/routing.yaml 的 routes。"
                )
        return self.client_for(chain[0])

    async def chat(self, task_type: str, messages: Sequence[Dict[str, Any]], **kwargs: Any) -> Dict[str, Any]:
        """按任务类型路由并调用；主模型失败沿 fallback 链降级。

        **能力校验**：沿链降级时**跳过**不具备该路由项所需能力的模型。
        若链中无合格模型（或全部调用失败），抛明确异常 —— 绝不把代码类任务
        静默降级到通用对话模型上。
        """
        raw_chain = self.chain_for(task_type) or ["gpt-oss-20b"]
        required = self.required_capabilities(task_type)
        chain = [m for m in raw_chain
                 if not required
                 or all(self.has_capability(m, cap) for cap in required)]
        if not chain:
            raise RoutingCapabilityError(
                f"路由 '{task_type}' 要求能力 {required}，"
                f"但降级链 {raw_chain} 中没有任何模型具备这些能力，"
                f"已拒绝降级（避免代码任务掉到非代码模型上）。"
                f"请安装对应模型，见 docs/06_代码模型接入策略.md。"
            )
        last_error: Optional[Exception] = None
        for model_name in chain:
            client = self.client_for(model_name)
            try:
                return await client.chat(messages, model=model_name, **kwargs)
            except Exception as exc:
                last_error = exc
                continue
        raise RuntimeError(
            f"所有候选模型均调用失败（chain={chain}）：{last_error}"
        )

    def chat_sync(self, task_type: str, messages: Sequence[Dict[str, Any]], **kwargs: Any) -> Dict[str, Any]:
        """同步版路由调用。"""
        coro = self.chat(task_type, messages, **kwargs)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()

    def health(self) -> Dict[str, Any]:
        """逐个探测候选模型端点的可达性。"""
        out: Dict[str, Any] = {}
        names = (set(self.chain_for("default")) | set(self.configs.keys())
                 | {m for chain in self.routes.values() for m in chain})
        for name in sorted(names):
            client = self.client_for(name)
            loop = asyncio.new_event_loop()
            try:
                out[name] = loop.run_until_complete(client.health())
            except Exception as exc:
                out[name] = {"reachable": False, "error": f"{type(exc).__name__}: {exc}"}
            finally:
                try:
                    loop.close()
                except Exception:
                    pass
        return out

    def routing_report(self) -> Dict[str, Any]:
        """导出路由表 + 能力标签的**自检报告**（供 /health 与验证脚本使用）。

        返回结构::

            {
              "routes": {task_type: {"chain": [...],
                                     "required_capabilities": [...],
                                     "effective_chain": [...],
                                     "violations": [...]}},
              "capabilities": {model_name: [...]},
              "violations": [...],       # 全局违规降级列表
            }
        """
        report: Dict[str, Any] = {
            "routes": {},
            "capabilities": {},
            "violations": self.validate_routes(),
        }
        all_models = {m for chain in self.routes.values() for m in chain}
        all_models |= set(self.configs.keys())
        for name in sorted(all_models):
            report["capabilities"][name] = self.capabilities_for(name)
        for task_type in sorted(self.routes):
            required = self.required_capabilities(task_type)
            chain = self.chain_for(task_type)
            violations = []
            for model_name in chain:
                missing = [c for c in required
                           if not self.has_capability(model_name, c)]
                if missing:
                    violations.append({
                        "model": model_name,
                        "missing": missing,
                    })
            report["routes"][task_type] = {
                "chain": chain,
                "required_capabilities": required,
                "effective_chain": self.filtered_chain_for(task_type),
                "violations": violations,
            }
        return report


__all__ = [
    "LLMConfig", "LLMClient", "LLMRouter",
    "DEFAULT_ROUTES", "REASONING_EFFORTS",
    "CAPABILITIES", "DEFAULT_CAPABILITIES", "ROUTE_REQUIREMENTS",
    "RoutingCapabilityError",
]
