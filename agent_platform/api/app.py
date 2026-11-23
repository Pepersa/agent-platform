# -*- coding: utf-8 -*-
"""FastAPI 接入层：/chat、/approve、/health、/tools + 本地 Web 工作台。

对应设计文档：
    《01_架构方案_RAG_Agent.md》§2.1 接入层
        · 开放 API 网关（FastAPI 直出，M1 最快）
        · Web 工作台：SSE 流 + 引用卡片 + 成本可见
    《01_架构方案_RAG_Agent.md》§1.4 图 2（API 以 SSE 输出流式回答）
    《02_场景方案_STDF分析与测试用例生成.md》§5.3 HITL 交互规范
        （pending_approval → 人工确认卡片 → approve 续跑）

关键实现约定：
    · /approve 必须使用**同一个 thread_id** 与**同一个 checkpointer 实例**，
      否则 Command(resume=...) 找不到被 interrupt 的图执行。
    · /chat 默认 SSE 流式返回（?stream=false 时返回普通 JSON）。
    · 组件全部懒加载/可降级：缺 langgraph / fastapi 时给出明确报错而非 ImportError 崩溃。

本文件新增（M3 Web 工作台）：
    · 静态目录挂载 ``/ui``（单页工作台：聊天 + 引用卡片 + HITL 卡片 + 任务发布台）
    · ``/chat`` 改为**真正分块流式**（graph.astream），先推 start 事件，
      再逐步推 step/approval 事件，最后推 result 事件 + ``[DONE]``。
    · ``POST /tasks`` / ``GET /tasks``：任务发布台后端（内存态队列 + 后台异步执行）
    · ``GET /tasks/templates``：从 agent_platform/eval/datasets/v1.jsonl 读种子集
    · ``GET /sessions``：会话列表（thread_id + 摘要 + 更新时间）
    · ``GET /metrics``：简易运行统计

**兼容性**：``/chat`` 的 SSE 行仍是 ``data: {json}``，且**最后一行 result 事件
携带与旧版完全相同的字段**（status / answer / thread_id / approvals /
tokens_used / tool_calls_used / stop_reason），旧客户端按字段名读取不受影响。
"""

from __future__ import annotations

import asyncio
import faulthandler
import json
import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.audit import Audit
from ..core.budget import BudgetGuard
from ..core.custom_tools import CustomToolError, CustomToolManager
from ..core.llm_client import LLMClient, LLMConfig, LLMRouter
from ..core.model_registry import ModelRegistry
from ..core.modes import (
    ASK_ROUTE,
    MODES,
    MODE_DESCRIPTIONS,
    MODE_LABELS,
    PLAN_ROUTE,
    allowed_permissions,
    mode_description,
    mode_label,
    normalize_mode,
    normalize_plan,
)
from ..core.session_store import SessionStore
from ..core.tool_registry import ToolRegistry
from ..orchestration.graph import build_graph
from ..orchestration.hitl import HITLGate
from ..orchestration.prompts import PROMPT_VERSION, get_system_prompt
from ..tools import register_all

# ----------------------------------------------------------------------
# Windows 事件循环（2026-11-22 修复 —— 服务被客户端断连搞崩）
# ----------------------------------------------------------------------
# 症状：浏览器在 ``/chat`` 的 SSE 流式响应中途刷新/关页签（客户端主动断连），
#       Windows 上默认的 **Proactor** 事件循环会在 socket 上收到
#       ``ConnectionResetError [WinError 995]``，随后在 ``_poll`` 里
#       对**已完成**的 future 调 ``set_exception``，抛出
#       ``asyncio.exceptions.InvalidStateError: invalid state``。
#       这个异常发生在 uvicorn 的事件循环顶层 → **整个 API 进程崩溃**
#       （实测：跑回归时服务静默退出，后续脚本全部 ConnectError；
#         浏览器侧表现为「保存失败：Failed to fetch」）。
#
# 修复：Windows 上改用 **Selector** 事件循环。
#   · 本项目**不使用** asyncio 子进程（已全仓检索确认：没有 create_subprocess），
#     而 Selector 唯一不支持的就是子进程，因此没有功能损失；
#   · Selector 的断连处理是标准的「读事件 → EOF」，不会出现 Proactor 的
#     双重 close 竞态。
#
# ⚠️ **两处都要做**（uvicorn >= 0.36 不读策略，只读 ``loop_factory``）：
#   1. 这里设置**事件循环策略** —— 供非 uvicorn 宿主（如 pytest-asyncio、
#      桌面版内嵌线程里的其它 asyncio 用法）使用；
#   2. **真正生效的是** ``agent_platform/core/event_loop.py`` 里那个
#      ``selector_loop_factory``，通过 uvicorn 的 ``--loop`` 参数 /
#      ``uvicorn.Config(loop=...)`` 注入（见 scripts/restart_api.py 与
#      agent_platform/desktop/launcher.py）。
if os.name == "nt":  # pragma: no cover - 平台相关
    try:
        _policy = asyncio.get_event_loop_policy()
        if not isinstance(_policy, asyncio.WindowsSelectorEventLoopPolicy):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        # 老版本 Python 没有 WindowsSelectorEventLoopPolicy → 保持默认
        pass

# ----------------------------------------------------------------------
# 后台常驻资源治理（2026-11-23）—— 限制 PyTorch/BLAS 线程数
# ----------------------------------------------------------------------
# 用户要求平台后台进程**总占用不超过机器资源的 50%**，以免影响其同时用机。
#
# 实测（本机 8 核 16 线程）：API **完全空闲**时仍占 **32% 的 16 逻辑核**，
# 因为 bge-m3 / bge-reranker 常驻 CPU 后，其 OpenMP intra-op 线程池
# （默认开到全部逻辑核）会持续 spin-wait 空转。
#
# ⚠️ BGE **不能**挪到 GPU：16 GB 单卡上 gpt-oss-20b 已常驻 ~15.4 GB，
#    BGE 上 GPU 会 OOM 并连带两个 llama-server 一起崩
#    （详见 knowledge/embed.py: resolve_flag_devices）。
#    因此唯一的正确解法是**限制线程数**，而不是换设备或降优先级
#    （nice 只改调度权重，阻止不了 OpenMP 空转）。
#
# 详见 knowledge/threading_limits.py。
try:  # pragma: no cover - 防御性：限制线程失败不能阻断启动
    from ..knowledge.threading_limits import limit_torch_threads as _limit_torch_threads

    _limit_torch_threads()
except Exception:  # noqa: BLE001
    pass

try:  # pragma: no cover - 环境缺依赖
    from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
    from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel, Field
    #: **注意**：``Request`` 只用来给新加的 ``/platform/status`` 做类型标注
    #: （FastAPI 靠类型标注识别「这是请求对象」而不是查询参数）。
    #: 这里从 ``starlette`` 取 —— starlette 是 FastAPI 的硬依赖，一定存在；
    #: 单独走 starlette 是为了**不改动** fastapi 的 import 行
    #: （那一行由并行 agent 同时在改，避免行级冲突）。
    from starlette.requests import Request
    FASTAPI_AVAILABLE = True
except Exception:  # pragma: no cover
    FASTAPI_AVAILABLE = False
    BaseModel = object  # type: ignore

    def BackgroundTasks(*args: Any, **kwargs: Any) -> Any:  # type: ignore
        return None

    def HTTPException(status_code: int = 500, detail: str = "") -> Exception:  # type: ignore
        return RuntimeError(detail)

    def JSONResponse(*args: Any, **kwargs: Any) -> Any:  # type: ignore
        return None

    def StreamingResponse(*args: Any, **kwargs: Any) -> Any:  # type: ignore
        return None

    def RedirectResponse(*args: Any, **kwargs: Any) -> Any:  # type: ignore
        return None

    def File(*args: Any, **kwargs: Any) -> Any:  # type: ignore
        return None

    def Form(*args: Any, **kwargs: Any) -> Any:  # type: ignore
        return None

    def UploadFile(*args: Any, **kwargs: Any) -> Any:  # type: ignore
        return None

    def Field(*args: Any, **kwargs: Any) -> Any:  # type: ignore
        return None


#: 本地两个 llama-server 端点（**不要重启/停止它们**，工作台只读其 /v1/models）。
LOCAL_MODELS: Dict[str, Dict[str, Any]] = {
    "gpt-oss-20b": {
        "label": "gpt-oss-20b (GPU :8080)",
        "base_url": "http://127.0.0.1:8080/v1",
        "model": "gpt-oss-20b",
        "reasoning_effort": "medium",
    },
    "qwen2.5-14b": {
        "label": "qwen2.5-14b (CPU :8081)",
        "base_url": "http://127.0.0.1:8081/v1",
        "model": "qwen2.5-14b",
        "reasoning_effort": "low",
    },
}

#: 健康探测的**读超时**（秒）。
#:
#: ★ 2026-11-23 修复：此常量曾被误删，导致 ``/health`` 直接抛
#:   ``NameError: name 'PROBE_READ_TIMEOUT_S' is not defined`` → 500。
#:   读超时必须**短**：llama-server 以 ``--parallel 1`` 运行，
#:   ``/v1/models`` 会排在正在进行的生成请求后面，端口在排队时不会立刻回。
PROBE_READ_TIMEOUT_S = 2.0
#: 健康探测的**连接超时**（秒）。
#:
#: ★ Windows 对「本地未监听端口」的 TCP 连接要等 ~2 秒 SYN 超时才放弃，
#:   探测必须更快放弃，否则每次探一个死端口就白等 2 秒。
PROBE_CONNECT_TIMEOUT_S = 0.5


def _probe_client(st: Any, base_url: str, model: str,
                  api_key: str = "local") -> "LLMClient":
    """取（并缓存）一个**健康探测专用**的 LLM 客户端。

    为什么需要缓存（2026-11-23 修正）
    ---------------------------------
    每次新建 ``LLMClient`` 都会新建一个 ``httpx.AsyncClient``。openai SDK
    在客户端被回收时**不保证关闭**底层 httpx 客户端，于是：

    * 连接池 / 传输层对象持续泄漏；
    * Windows 上积累到一定程度，Selector 事件循环里的残留传输层
      会把**整个 API 进程**搞崩 —— 实测第 3 次 ``/health`` 请求就进程退出，
      且 stderr 里**没有任何 traceback**（硬崩，不是异常）。

    所以这里按 ``(base_url, model)`` 缓存探测客户端，跨请求复用。
    ``LLMClient`` 内部已按事件循环缓存 ``AsyncOpenAI`` 实例
    （见 ``llm_client.py::LLMClient.client``），复用是安全的。

    ★★ 2026-11-23 再修正：**绕开系统代理 + 极短 connect 超时** ★★
    ------------------------------------------------------------------
    本机 WinINET 里开着 ``ProxyEnable=1``（代理在 127.0.0.1:17997）。
    httpx 默认 ``trust_env=True``，会**读注册表**把**所有**请求 ——
    包括指向 127.0.0.1:8080 / :8081 的 llama-server —— 都交给那个代理。
    代理连不通本地端口时要 **2~4 秒**才吐一个 502；而**直连**是
    「连接被拒绝」，几乎瞬时返回。

    再叠加 Windows 对「本地未监听端口」的 TCP 连接要等 **~2 秒**
    SYN 超时才放弃，3 个探测串起来就是 **6~10 秒** —— 这正是
    ``/health`` 要 8~9 秒的根因。

    因此探测客户端：
      * ``trust_env=False`` —— 不走系统代理；
      * ``connect=0.5s``      —— 端口没人监听时快速失败；
      * ``read=2.0s``        —— 端点在但排队生成时也不会久等；
      * ``max_retries=0``    —— 探测只是「现在通不通」，重试只会翻倍耗时。

    ⚠️ 不能改 ``client.config.timeout``（那是真实对话用的 180 s），
    所以另建一个**独立的**探测客户端。
    """
    cache = getattr(st, "_health_probe_clients", None)
    if cache is None:
        cache = {}
        try:
            st._health_probe_clients = cache
        except Exception:  # noqa: BLE001 - st 可能是不可变的代理
            pass
    key = (base_url, model)
    client = cache.get(key)
    if client is None:
        http_client = _probe_http_client(st)
        client = LLMClient(
            LLMConfig(base_url=base_url, model=model,
                      api_key=api_key or "local", timeout=PROBE_READ_TIMEOUT_S),
            max_retries=0,
            http_client=http_client,
        )
        try:
            cache[key] = client
        except Exception:  # noqa: BLE001
            pass
    return client


def _probe_http_client(st: Any) -> Any:
    """构造（并缓存）健康探测专用的 ``httpx.AsyncClient``。

    ★ ``trust_env=False``：绕开 Windows 注册表里的系统代理。
      本机代理（127.0.0.1:17997）会把对 127.0.0.1 的探测也代理走，
      每次失败要多花 2~4 秒拿一个无意义的 502。
    ★ ``connect=PROBE_CONNECT_TIMEOUT_S``：本地端口没人监听时
      Windows 要等 ~2 秒 SYN 超时，探测必须更快放弃。
    ★ **复用**同一个客户端：避免 httpx 连接池泄漏（见 ``_probe_client`` 说明）。

    Returns:
        ``httpx.AsyncClient``；httpx 不可用时返回 None（openai SDK 会自建）。
    """
    cache = getattr(st, "_health_probe_http", None)
    if cache is not None:
        return cache
    try:
        import httpx  # type: ignore

        client = httpx.AsyncClient(
            trust_env=False,
            timeout=httpx.Timeout(
                connect=PROBE_CONNECT_TIMEOUT_S,
                read=PROBE_READ_TIMEOUT_S,
                write=PROBE_READ_TIMEOUT_S,
                pool=PROBE_CONNECT_TIMEOUT_S,
            ),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
    except Exception:  # noqa: BLE001 - httpx 缺失时退回 SDK 默认客户端
        client = None
    try:
        st._health_probe_http = client
    except Exception:  # noqa: BLE001
        pass
    return client

#: 静态资源目录（agent_platform/api/static）
STATIC_DIR = Path(__file__).resolve().parent / "static"

#: 模块级日志器（启动期预热等非请求路径的告警走这里）
logger = logging.getLogger(__name__)


def _kb_warmup_status() -> Dict[str, Any]:
    """返回知识库重模型（BGE）的预热状态（``/health`` 用）。

    ★ 2026-11-23 新增：API 启动时后台预热 bge-m3 + bge-reranker-v2-m3
      （实测合计约 **33s**）。前端可以用这个字段显示「知识库模型加载中…」。

    ★ 这个函数**绝不抛异常**：预热状态拿不到时返回 ``{"state": "unknown"}``，
      绝不能因为一个诊断字段让 ``/health`` 整体失败。

    Returns:
        预热状态字典；knowledge 层不可用时返回 ``{"state": "unknown"}``。
    """
    try:
        from ..knowledge.tools import warmup_status  # noqa: PLC0415

        return warmup_status()
    except Exception as exc:  # noqa: BLE001 - 健康检查不该因它失败
        return {"state": "unknown", "ready": False,
                "error": f"{type(exc).__name__}: {exc}"}


def _kb_runtime_info() -> Dict[str, Any]:
    """返回知识库检索链的**运行时调优参数**（``/health`` 用）。

    ★ 2026-11-23 新增：把「当前生效的性能参数」暴露出来，
      这样「为什么这次检索慢 / 快」可以直接从 ``/health`` 读出来，
      不必去翻代码或猜。

      包含：
        · ``torch_threads``：空闲期线程数（防 OpenMP 空转，默认 4）
        · ``infer_threads``：推理期线程数（要吞吐，默认 8）
        · ``rerank_max_length``：交叉编码器最大序列长度（默认 512）
        · ``rerank_shortlist``：候选截断上限（默认 0 = 不截断）

    ★ 这个函数**绝不抛异常**。

    Returns:
        运行时参数字典；knowledge 层不可用时返回 ``{}``。
    """
    info: Dict[str, Any] = {}
    try:
        from ..knowledge import threading_limits as _tl  # noqa: PLC0415

        info["torch_threads"] = _tl.resolve_torch_threads()
        info["infer_threads"] = _tl.resolve_inference_threads()
    except Exception:  # noqa: BLE001 - 健康检查不该因它失败
        pass
    try:
        import inspect  # noqa: PLC0415

        from ..knowledge.reranker import Reranker  # noqa: PLC0415

        # ★ 只读**签名默认值**，绝不实例化 Reranker ——
        #   实例化会加载 bge-reranker 权重（几十秒），/health 扛不住。
        _sig = inspect.signature(Reranker.__init__)
        info["rerank_max_length"] = int(
            _sig.parameters["max_length"].default
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        import os  # noqa: PLC0415

        info["rerank_shortlist"] = int(
            os.environ.get("BGE_RERANK_SHORTLIST", "0") or 0
        )
    except Exception:  # noqa: BLE001
        pass
    return info


def _data_dir_info() -> Dict[str, Any]:
    """返回数据目录诊断信息（``/health`` 用）。

    数据目录可用环境变量 ``AGENT_DATA_DIR`` 整体外置到任意盘符；
    不设时仍为 ``<repo>/data``（向后兼容）。
    """
    try:
        from ..core import paths as _paths  # noqa: PLC0415

        return _paths.data_dir_info()
    except Exception as exc:  # noqa: BLE001 - 健康检查不该因它失败
        return {"dir": "", "exists": False, "external": False,
                "source": "unknown", "error": f"{type(exc).__name__}: {exc}"}


def _probe_models_endpoint(base_url: str, timeout: float = 1.5) -> bool:
    """探测某个 OpenAI 兼容端点的 ``/v1/models`` 是否真的可用（阻塞）。

    ★ **只在端口确实有人监听时才调用**（见 ``_listening_ports()``），
      所以正常情况下最多 1~2 次快速请求；用 urllib 而不是 httpx，
      免得为了「一次性探测」新建连接池。

    Args:
        base_url: 形如 ``http://127.0.0.1:8080/v1``。
        timeout: 单次请求超时（秒）。

    Returns:
        ``/v1/models`` 是否返回了非空模型列表。
    """
    url = base_url.rstrip("/") + "/models"
    try:
        import urllib.request  # noqa: PLC0415

        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if int(resp.status) != 200:
                return False
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        data = payload.get("data") if isinstance(payload, dict) else None
        return bool(data)
    except Exception:
        return False


def _listening_ports() -> Dict[int, int]:
    """一次性拿到「本机正在监听的端口 → PID」。

    ★ **为什么要自己实现，而不是直接用** ``ModelManager.list_models()``：
      后者的 ``_port_is_listening()`` 用 ``socket.connect_ex()`` 逐个端口探测，
      在 **Windows** 上对一个**没人监听**的端口，这个调用要等满
      ``settimeout(0.5)`` 才会返回（实测每个端口 500 ms）。
      两个模型 = 1 秒 —— 正好卡死「前端每 2 秒轮询一次」这条设计。

      这里的做法：用 ``psutil.net_connections(kind='tcp')`` **一次**拿到
      全部 TCP 连接（内核侧一次遍历，实测 3~16 ms），再筛出 LISTENING 的。
      psutil 不可用时退回 socket 探测（慢，但保证功能不缺失）。

    Returns:
        ``{port: pid}``；pid 拿不到时值为 0。
    """
    try:
        import psutil  # noqa: PLC0415

        out: Dict[int, int] = {}
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status != psutil.CONN_LISTEN:
                continue
            if not conn.laddr:
                continue
            try:
                out[int(conn.laddr.port)] = int(conn.pid) if conn.pid else 0
            except Exception:  # pragma: no cover - 极端数据
                continue
        return out
    except Exception:
        # ---- 兜底：没有 psutil 时用 socket 逐个探测（慢但能工作）----
        import socket  # noqa: PLC0415

        out = {}
        for port in (8080, 8081):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.settimeout(0.2)
                    if sock.connect_ex(("127.0.0.1", port)) == 0:
                        out[port] = 0
            except Exception:  # pragma: no cover
                continue
        return out


if FASTAPI_AVAILABLE:  # pragma: no cover - 依赖可用时始终成立
    class NoCacheStaticFiles(StaticFiles):
        """静态资源**禁用缓存**（但仍走 ETag 协商）。

        **回归点**：StaticFiles 默认只发 ``last-modified`` / ``etag``，
        没有 ``cache-control``，浏览器会启发式缓存 app.js / app.css。
        结果是：改了前端并重启桌面版，窗口里**仍然是旧界面**，
        排障时极难判断"到底生效没有"。这里强制 ``no-cache``：
        每次都回源校验，ETag 未变才返回 304，带上最新内容。
        """

        def file_response(self, *args: Any, **kwargs: Any) -> Any:
            resp = super().file_response(*args, **kwargs)
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
            return resp

#: 评测种子集路径（agent_platform/eval/datasets/v1.jsonl）
SEED_DATASET = (
    Path(__file__).resolve().parent.parent
    / "eval" / "datasets" / "v1.jsonl"
)


# ----------------------------------------------------------------------
# 请求/响应模型
# ----------------------------------------------------------------------
if FASTAPI_AVAILABLE:
    class ChatRequest(BaseModel):
        """POST /chat 请求体。

        **向后兼容**：新增字段全部可选且有默认值，老调用方不受影响。
        """
        message: str = Field(..., description="用户输入")
        thread_id: Optional[str] = Field(None, description="会话线程 ID（断点续跑用）")
        user_id: Optional[str] = Field(None, description="用户 ID")
        session_id: Optional[str] = Field(None, description="会话 ID")
        stream: Optional[bool] = Field(None, description="是否 SSE 流式（默认 true）")
        model: Optional[str] = Field(None, description="目标模型名（模型注册表中的 name）")
        mode: Optional[str] = Field(None, description="交互模式：agent | ask | plan（默认 agent）")
        #: **本次请求的 ReAct 轮次上限**（2026-11-24 新增）。
        #:
        #: 此前 ``/chat`` **完全没有**这个入口 → 无论前端怎么设置，
        #: 服务端都用硬编码的 8 轮，用户抱怨「才几步就停了」。
        #: 现在前端「对话」区也能调这个值，并随请求下发。
        max_rounds: Optional[int] = Field(
            None, ge=1, le=64,
            description="本次请求的 ReAct 轮次上限（缺省 20；一次 LLM 调用 = 一轮）")

    class ApproveRequest(BaseModel):
        """POST /approve 请求体（docs/02 §5.3 决策字段）。"""
        thread_id: str = Field(..., description="待审批线程 ID")
        tool_call_id: str = Field(..., description="待审批的工具调用 ID")
        approved: bool = Field(..., description="是否批准")
        actor: str = Field(..., description="审批人")
        reason: str = Field("", description="审批理由")
        session_grant: bool = Field(False, description="是否授予会话级授权（确认一次本会话内允许）")
        #: **是否强制走「模型总结」路径**（2026-11-24 新增，修复「审批太慢」）。
        #:
        #: 默认 **False**（快速路径）：批准后若被批准的动作本身就是终态
        #: （写工具一次执行即产出完整、自描述的结果 JSON），
        #: 就**跳过第二次 LLM 调用**，直接用工具结果作答 —— 实测省掉 1.06~2.33 s。
        #:
        #: 置 True 则**完全照旧**：跑第二次 LLM 生成自然语言总结
        #: （用户想要一段人话、或想让模型解释工具结果的场合）。
        summarize: bool = Field(
            False, description="是否强制用模型生成自然语言总结（默认 False = 快速路径）")

    class ChatResponse(BaseModel):
        """POST /chat 响应体（非流式）。"""
        status: str = Field(..., description="done | pending_approval | error")
        answer: Optional[str] = Field(None, description="最终答案")
        thread_id: str = Field(..., description="线程 ID")
        approvals: List[Dict[str, Any]] = Field(default_factory=list, description="待审批项")
        tokens_used: int = 0
        tool_calls_used: int = 0
        #: **真实 ReAct 轮次**（2026-11-24 新增）：由 agent 节点回写 state。
        #: 此前 API 只能用 trace 长度兜底 → 出现「9/8」这种越界显示。
        rounds: int = 0
        stop_reason: Optional[str] = None
        model: Optional[str] = Field(None, description="实际使用的模型")
        mode: Optional[str] = Field(None, description="实际使用的交互模式")
        plan: Optional[Dict[str, Any]] = Field(None, description="Plan 模式下的结构化计划")
        retrieval_needed: Optional[bool] = Field(
            None, description="本轮是否需要走知识库检索（True=已检索 / False=已跳过）")
        retrieval_rule: Optional[str] = Field(
            None, description="命中的检索必要性判定规则名")
        retrieval_reason: Optional[str] = Field(
            None, description="检索必要性判定理由（人类可读）")
        retrieval_label: Optional[str] = Field(
            None, description="前端展示用的中文标记，如「检索: 已跳过」")
        #: **决策轨迹**（2026-11-24 新增）：``/approve`` 也返回它，
        #: 否则审批后「决策轨迹」面板拿不到新快照。
        trace: List[Dict[str, Any]] = Field(default_factory=list)

    class ModelRequest(BaseModel):
        """POST /models / PUT /models/{name} 请求体。"""
        name: str = Field(..., description="模型名（字母/数字/下划线/短横线/点）")
        label: str = Field("", description="展示名（缺省用 name）")
        base_url: str = Field(..., description="OpenAI 兼容端点，如 http://127.0.0.1:8080/v1")
        model: str = Field("", description="端点上的模型名（缺省用 name）")
        description: str = Field("", description="模型描述（给用户看的）")
        reasoning_effort: str = Field("medium", description="推理强度：low | medium | high")
        temperature: float = Field(0.2, description="采样温度")
        max_tokens: int = Field(2048, description="单次最大 token")
        api_key: str = Field("local", description="API Key（不会在 GET /models 里明文回显）")
        timeout: float = Field(180.0, description="请求超时（秒）")
        supports_tools: bool = Field(True, description="是否支持工具调用（函数调用）")

    class ModelUpdateRequest(BaseModel):
        """PUT /models/{name} 请求体（**全部字段可选**，只覆盖传入的字段）。"""
        label: Optional[str] = Field(None, description="展示名")
        base_url: Optional[str] = Field(None, description="OpenAI 兼容端点")
        model: Optional[str] = Field(None, description="端点上的模型名")
        description: Optional[str] = Field(None, description="模型描述")
        reasoning_effort: Optional[str] = Field(None, description="推理强度：low | medium | high")
        temperature: Optional[float] = Field(None, description="采样温度")
        max_tokens: Optional[int] = Field(None, description="单次最大 token")
        api_key: Optional[str] = Field(None, description="API Key")
        timeout: Optional[float] = Field(None, description="请求超时（秒）")
        supports_tools: Optional[bool] = Field(None, description="是否支持工具调用")

    class TaskCreateRequest(BaseModel):
        """POST /tasks 请求体（任务发布台）。"""
        name: str = Field(..., description="任务名")
        description: str = Field("", description="任务描述")
        prompt: Optional[str] = Field(None, description="实际执行的 prompt（缺省用 description）")
        model: str = Field("gpt-oss-20b", description="目标模型（模型注册表中的 name）")
        params: Dict[str, Any] = Field(default_factory=dict,
                                       description="参数：max_rounds / temperature / reasoning_effort / max_tokens")
        mode: Optional[str] = Field(None, description="交互模式：agent | ask | plan（默认 agent）")

    class CustomToolRequest(BaseModel):
        """POST /custom-tools / PUT /custom-tools/{name} 请求体。"""
        name: str = Field(..., description="工具名（字母/数字/下划线，不与内置工具重名）")
        description: str = Field("", description="工具描述（给模型看的）")
        input_schema: Dict[str, Any] = Field(
            default_factory=lambda: {"type": "object", "properties": {}},
            description="入参 JSON Schema（type 必须为 object）")
        kind: str = Field("http", description="工具类型：http | script")
        config: Dict[str, Any] = Field(default_factory=dict,
                                       description="类型相关配置：http → {url,method,headers,body_template,result_path}；"
                                                   "script → {script_path,function_name,timeout_s}")
        permission: str = Field("READ_ONLY",
                                description="权限：READ_ONLY | COMPUTE | WRITE_DATA | EXTERNAL_SIDE_EFFECT | ADMIN")
        hitl_required: bool = Field(False, description="是否需要人工确认（写/对外/管理员权限下强制为 True）")
        enabled: bool = Field(True, description="是否启用")

    class CustomToolTestRequest(BaseModel):
        """POST /custom-tools/{name}/test 请求体（试运行）。"""
        args: Dict[str, Any] = Field(default_factory=dict, description="示例入参")

    class KbDocumentTextRequest(BaseModel):
        """POST /kb/documents（粘贴文本方式）请求体。"""
        title: str = Field("", description="文档标题（缺省取 front matter 或自动生成）")
        text: str = Field(..., description="文档正文（可含 YAML front matter）")
        doc_id: str = Field("", description="文档 ID（缺省自动生成 DOC-<hash>）")
        doc_type: str = Field("document", description="文档类型")
        version: str = Field("", description="版本号")
        owner: str = Field("", description="负责人")
        security_level: str = Field("", description="密级：public/internal/confidential/secret")
        visible_roles: List[str] = Field(default_factory=list, description="可见角色列表")
        product_line: str = Field("", description="产品线")

    class WorkspaceWriteRequest(BaseModel):
        """POST /workspace/file 请求体（生成待审批改动）。"""
        path: str = Field(..., description="相对工作区根目录的文件路径")
        content: str = Field("", description="文件的新完整内容")
        expected_mtime: Optional[float] = Field(
            None, description="用户打开文件时的 mtime；不一致则返回 409 WS_FILE_CHANGED")
        note: str = Field("", description="改动说明（展示在审批卡片上）")

    class PlatformLaunchRequest(BaseModel):
        """POST /platform/launch 请求体。

        ★ ``models`` 为空/缺省 = **平台默认**（当前是 ``gpt-oss-20b``）；
          非空 = 「**勾选的启动，没勾的停掉**」——用户明确要求的语义。
        """
        models: List[str] = Field(
            default_factory=list,
            description="要启动的模型别名列表；为空则使用平台默认模型")

    class WorkspaceApplyRequest(BaseModel):
        """POST /workspace/apply 请求体（应用待审批改动）。"""
        change_id: str = Field(..., description="改动 ID")
        approved: bool = Field(True, description="是否批准（false 表示放弃）")
        actor: str = Field("operator", description="操作人")
        reason: str = Field("", description="理由")
else:  # pragma: no cover
    ChatRequest = None  # type: ignore
    ApproveRequest = None  # type: ignore
    ChatResponse = None  # type: ignore
    TaskCreateRequest = None  # type: ignore
    CustomToolRequest = None  # type: ignore
    CustomToolTestRequest = None  # type: ignore
    KbDocumentTextRequest = None  # type: ignore
    WorkspaceWriteRequest = None  # type: ignore
    WorkspaceApplyRequest = None  # type: ignore
    PlatformLaunchRequest = None  # type: ignore
    ModelRequest = None  # type: ignore


# ----------------------------------------------------------------------
# 应用装配
# ----------------------------------------------------------------------
class AppState:
    """进程内单例组件集合（Windows 单进程部署的最小实现）。"""

    def __init__(self) -> None:
        # 确保数据目录（含各子目录）存在 —— 数据目录可能指向**别的盘符**
        # （环境变量 AGENT_DATA_DIR），首次启动时需要自动创建。
        try:
            from ..core import paths as _paths  # noqa: PLC0415

            _paths.ensure_data_dirs()
        except Exception:  # noqa: BLE001 - 建目录失败不应阻断启动
            pass
        self.audit = Audit()
        self.registry = ToolRegistry(audit=self.audit)
        register_all(self.registry)
        #: 内置工具名（受保护：自定义工具不得重名，管理器也不会 unregister 它们）
        self.builtin_tool_names: List[str] = list(self.registry.names())
        #: 自定义工具管理器（持久化 data/custom_tools.json + 立即注册生效）
        self.custom_tools = CustomToolManager(
            registry=self.registry,
            reserved_names=self.builtin_tool_names,
        )
        self.gate = HITLGate(audit=self.audit)
        #: **全局唯一的 checkpointer**：所有图（含按模型变体建的图）共享它，
        #: 这样 /approve 才能用 Command(resume=...) 找到被 interrupt 的执行。
        self.checkpointer = _make_checkpointer()
        self.graph = None
        self.router: Optional[LLMRouter] = None
        self.client: Optional[LLMClient] = None
        self._lock = asyncio.Lock()
        #: 按参数缓存的图变体：{(model, temp, effort, max_tokens): graph}
        self._graph_variants: Dict[tuple, Any] = {}
        #: **按线程缓存的「本次请求专用图」**（2026-11-24 新增）。
        #:
        #: 为什么需要（FIX 3：/approve 必须复用同一个图）：
        #:   ``make_graph`` 的文档明确写着「每次请求新建一份 BudgetGuard，
        #:   否则轮次计数会在多次请求间累加」。因此 ``/chat`` 每次都建**新图**。
        #:   而 ``/approve`` 此前调 ``ensure_graph()``（**无参数**）→ 又建一个
        #:   **参数完全不同**的图（新预算、新模型默认值）。
        #:   虽然 checkpointer 共享，resume 本身能跑通，但：
        #:     · 被 resume 的执行用的**不是**发起审批时那个图的 BudgetGuard；
        #:     · ``max_rounds`` 被重置回默认值（8 → 现 20），
        #:       用户设的轮次预算在审批后续跑时**悄悄失效**。
        #:   所以这里按 ``thread_id`` 记住「本次 /chat 用的那个图实例」，
        #:   ``/approve`` 直接取回同一个实例。
        #:
        #: 生命周期：``/chat`` 每次覆盖写入；``/approve`` 成功后删除
        #: （避免内存无限增长）。
        self._thread_graphs: Dict[str, Any] = {}
        #: 已构建图的计数（用于 /health 展示）
        self._graphs_built = 0
        #: 会话表：{thread_id: {...}}（内存态；消息正文在 SessionStore 里落盘）
        self.sessions: Dict[str, Dict[str, Any]] = {}
        #: **模型注册表**（config/routing.yaml + data/models.json）
        self.models = ModelRegistry(routing_file=os.environ.get("ROUTING_CONFIG") or None)
        #: **会话持久化**（SQLite：data/sessions.db）
        self.session_store = SessionStore()
        #: 任务表：{task_id: {...}}
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self._task_order: List[str] = []
        #: 知识库管理（用户自助导入 / 删除 / 重建索引）
        self.kb_admin = _make_kb_admin()
        #: 后台任务句柄（避免被 GC 回收）
        self._bg: List[Any] = []
        #: 简易运行统计
        self.metrics = {
            "requests_total": 0,
            "chat_total": 0,
            "approve_total": 0,
            "tasks_total": 0,
            "tokens_total": 0,
            "tool_calls_total": 0,
            "errors_total": 0,
            "total_duration_ms": 0,
        }

    # ---------------- LLM ----------------
    def llm(self) -> Any:
        """取 LLM 客户端（Router 优先）。"""
        if self.router is None:
            routing_file = os.environ.get(
                "ROUTING_CONFIG",
                os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__)))), "config", "routing.yaml"),
            )
            self.router = LLMRouter(routing_file=routing_file)
        if self.client is None:
            self.client = self.router.client_for(self.router.chain_for("agent")[0])
        return self.client

    def llm_for(self, model: Optional[str] = None, *,
                temperature: Optional[float] = None,
                effort: Optional[str] = None,
                max_tokens: Optional[int] = None,
                mode: str = "agent") -> LLMClient:
        """按（可选的）模型/温度/推理强度构造 LLM 客户端。

        * model 为空且无其它覆盖 → 走 **模式对应路由**（ask/plan 复用
          ``config/routing.yaml`` 的 ``routes:`` 机制，不硬编码模型）
        * model 命中**模型注册表** → 用注册表里的 base_url / model 等参数
        * model 命中 LOCAL_MODELS → 用本地端点常量（8080 / 8081）
        * 其它 model → 走 routing.yaml 的模型配置
        """
        m = normalize_mode(mode)
        if not model and temperature is None and effort is None and max_tokens is None:
            # 按模式路由：agent → "agent"，ask → "ask"，plan → "plan"
            route = {"agent": "agent", "ask": ASK_ROUTE, "plan": PLAN_ROUTE}[m]
            return self.router_client(route)

        cfg: LLMConfig
        if model:
            reg_cfg = self.models.to_client_config(model)
            if reg_cfg is not None:
                cfg = reg_cfg
            elif model in LOCAL_MODELS:
                info = LOCAL_MODELS[model]
                cfg = LLMConfig(
                    base_url=info["base_url"],
                    model=info["model"],
                    reasoning_effort=str(info.get("reasoning_effort", "medium")),
                    api_key="local",
                    timeout=180.0,
                )
            else:
                try:
                    cfg = self._router().config_for(model)
                except Exception:
                    cfg = LLMConfig.from_env()
                    cfg.model = model
        else:
            cfg = LLMConfig.from_env()

        if temperature is not None:
            cfg.temperature = float(temperature)
        if effort:
            eff = str(effort).lower()
            cfg.reasoning_effort = eff if eff in ("low", "medium", "high") else cfg.reasoning_effort
        if max_tokens:
            cfg.max_tokens = int(max_tokens)
        return LLMClient(cfg)

    def router_client(self, task_type: str = "agent") -> LLMClient:
        """按路由项取 LLM 客户端（复用 ``config/routing.yaml`` 的 routes）。

        **能力校验**：委托给 :meth:`LLMRouter.route`，它会剔除降级链中
        不具备该路由项所需能力的模型；若链中无合格模型，
        抛 ``RoutingCapabilityError``（**显式失败**，不静默降级）。

        ⚠️ 历史实现直接取 ``chain_for(task_type)[0]``，跳过了能力校验，
        会让代码类任务静默落到不具备代码能力的模型上 —— 现已修正。
        """
        if self.router is None:
            self.llm()
        assert self.router is not None
        return self.router.route(task_type)

    def _router(self) -> LLMRouter:
        if self.router is None:
            self.llm()
        assert self.router is not None
        return self.router

    # ---------------- 图 ----------------
    def make_graph(self, *, model: Optional[str] = None,
                   temperature: Optional[float] = None,
                   effort: Optional[str] = None,
                   max_tokens: Optional[int] = None,
                   max_rounds: Optional[int] = None,
                   mode: str = "agent") -> Any:
        """构建一个**本次请求专用**的图。

        **为什么每次都新建 BudgetGuard**：图节点在 build 时就闭包捕获了 budget
        对象，若把同一份 BudgetGuard 复用给所有请求，轮次计数会在多次请求间累加，
        第 8 轮之后**所有**新请求都会被 MAX_ROUNDS 直接拦掉（真实踩过）。
        因此预算必须按请求隔离。

        **但 checkpointer 必须共享**：所有图（含按模型/参数建的变体）都用
        ``self.checkpointer`` 这一个实例，/approve 才能用 Command(resume=...)
        找到被 interrupt 的执行。

        **模式分支**：``mode="plan"`` 时图里只挂 ``plan`` 节点（不挂 tools），
        因此**结构上不可能**调用任何工具；``mode="ask"`` 时 tools 节点按只读白名单过滤。
        """
        m = normalize_mode(mode)
        client = self.llm_for(model, temperature=temperature, effort=effort,
                               max_tokens=max_tokens, mode=m)
        budget = BudgetGuard(
            #: **默认轮次上限 8 → 20（2026-11-24）**
            #:
            #: 为什么提高：8 轮在真实任务里远远不够 —— 检索 2~3 轮、
            #: 工具调用 2~3 轮、汇总成答案 1~2 轮，轻松就用完。
            #: 用户反馈「才几步就提示预算用尽」。20 轮让绝大多数任务
            #: 能自然跑完，同时仍有硬上限防止无限循环。
            #: ★ 轮次语义在 2026-11-24 修正为「**一次 LLM 调用 = 一轮**」
            #:   （此前 agent 与 tools 各算一次，实际只有一半可用轮次）。
            max_rounds=int(max_rounds) if max_rounds else DEFAULT_MAX_ROUNDS,
            context_window=CONTEXT_WINDOW_TOKENS,
        )
        graph = build_graph(
            client, self.registry, self.gate, budget,
            checkpointer=self.checkpointer, audit=self.audit,
            system_prompt=get_system_prompt(),
            mode=m,
        )
        self.graph = graph          # 供 /health 展示"图已构建"
        self._graphs_built += 1
        return graph

    def ensure_graph(self, *, model: Optional[str] = None,
                     mode: str = "agent",
                     max_rounds: Optional[int] = None) -> Any:
        """取本次请求专用的图（默认模型 + 默认参数）。

        Args:
            model: 目标模型名（None = 默认）。
            mode: 交互模式（agent / ask / plan）。
            max_rounds: **本次请求的 ReAct 轮次上限**（None = 用默认值 20）。
                此前 ``ensure_graph`` **不接受**这个参数 → ``/chat`` 永远
                拿到硬编码的 8 轮，用户在前端改「max_rounds」对对话无效。
        """
        return self.make_graph(model=model, mode=mode, max_rounds=max_rounds)

    # ---------------- 按线程的图注册表（FIX 3：/approve 复用同一个图）----------------
    def register_thread_graph(self, thread_id: str, graph: Any) -> None:
        """记住「本次 /chat 用的那个图实例」，供 /approve 复用。

        为什么必须复用同一个图实例（2026-11-24）
        -------------------------------------------
        ``make_graph`` 每次都**新建 BudgetGuard**（刻意为之：预算必须按请求隔离）。
        如果 ``/approve`` 另建一个图，被 resume 的执行就跑在新图上：
        · ``max_rounds`` 被重置成默认值 → 用户设的轮次预算悄悄失效；
        · 模型 / 模式也可能与发起审批的那次不一致。

        共享 checkpointer 让 resume **能跑通**，但「跑在哪个图上」决定了
        预算与参数是否一致 —— 所以这里按 thread_id 记下原图。

        Args:
            thread_id: 会话线程 ID。
            graph: 该次请求使用的图实例。
        """
        if not thread_id or graph is None:
            return
        try:
            self._thread_graphs[thread_id] = graph
        except Exception:  # pragma: no cover —— 注册失败不应阻断主流程
            pass

    def thread_graph(self, thread_id: str) -> Any:
        """取回该线程「最近一次 /chat 使用的图实例」（没有则 None）。"""
        if not thread_id:
            return None
        try:
            return self._thread_graphs.get(thread_id)
        except Exception:  # pragma: no cover
            return None

    def drop_thread_graph(self, thread_id: str) -> None:
        """审批结束后丢弃该线程的图引用（避免内存无限增长）。"""
        if not thread_id:
            return
        try:
            self._thread_graphs.pop(thread_id, None)
        except Exception:  # pragma: no cover
            pass

    # ---------------- 预算 ----------------
    @staticmethod
    def new_budget() -> BudgetGuard:
        """每个请求一份预算（对应"任务总时长 15min、20 轮 ReAct"的红线）。"""
        return BudgetGuard()

    # ---------------- 会话 ----------------
    def touch_session(self, thread_id: str, *, summary: str = "",
                      session_id: Optional[str] = None,
                      user_id: Optional[str] = None,
                      mode: str = "",
                      model: str = "",
                      bump_count: bool = True) -> None:
        """登记/更新一个会话（内存态 + SQLite 落盘）。

        **消息正文**由 :meth:`record_message` 写入 ``data/sessions.db``；
        这里只维护会话元信息（标题 / 摘要 / 计数 / 模式 / 模型）。
        """
        now = time.time()
        rec = self.sessions.get(thread_id) or {
            "thread_id": thread_id,
            "created_at": now,
            "session_id": session_id or "",
            "user_id": user_id or "",
            "summary": "",
            "message_count": 0,
        }
        if summary:
            rec["summary"] = summary[:120]
        if session_id:
            rec["session_id"] = session_id
        if user_id:
            rec["user_id"] = user_id
        if mode:
            rec["mode"] = mode
        if model:
            rec["model"] = model
        if bump_count:
            rec["message_count"] = int(rec.get("message_count", 0)) + 1
        rec["updated_at"] = now
        self.sessions[thread_id] = rec
        # 同步落盘（失败不阻断主流程）
        try:
            self.session_store.touch_session(
                thread_id,
                title=summary,
                session_id=session_id or "",
                user_id=user_id or "",
                mode=mode or "",
                model=model or "",
                bump_count=bump_count,
            )
        except Exception:  # pragma: no cover - 落盘失败不影响主流程
            pass

    def record_message(self, thread_id: str, *, role: str, content: str,
                       tool_calls: Optional[Any] = None,
                       citations: Optional[Any] = None,
                       extra: Optional[Any] = None) -> int:
        """把一条消息写入 ``data/sessions.db``（返回 seq）。"""
        try:
            return self.session_store.append_message(
                thread_id, role=role, content=content,
                tool_calls=tool_calls, citations=citations, extra=extra)
        except Exception:  # pragma: no cover
            return 0

    def list_sessions_merged(self) -> List[Dict[str, Any]]:
        """合并「内存态」与「SQLite 落盘」的会话列表。

        以 SQLite 为准（含摘要状态），内存态里独有的会话也补进来，
        保证**重启后历史会话仍在**。
        """
        rows = self.session_store.list_sessions()
        seen = set()
        out: List[Dict[str, Any]] = []
        for r in rows:
            tid = r.get("thread_id", "")
            if not tid:
                continue
            seen.add(tid)
            mem = self.sessions.get(tid) or {}
            item = {
                "thread_id": tid,
                "session_id": r.get("session_id") or mem.get("session_id") or "",
                "user_id": r.get("user_id") or mem.get("user_id") or "",
                "title": r.get("title") or mem.get("summary") or "",
                "summary": r.get("summary") or "",
                "summary_status": r.get("summary_status") or "none",
                "summary_json": r.get("summary_json"),
                "has_summary": bool(r.get("has_summary")),
                "message_count": int(r.get("message_count") or 0),
                "has_trace": bool(r.get("has_trace")),
                "mode": r.get("mode") or mem.get("mode") or "",
                "model": r.get("model") or mem.get("model") or "",
                "created_at": r.get("created_at") or mem.get("created_at"),
                "updated_at": r.get("updated_at") or mem.get("updated_at"),
            }
            out.append(item)
        for tid, mem in self.sessions.items():
            if tid in seen:
                continue
            out.append({
                "thread_id": tid,
                "session_id": mem.get("session_id", ""),
                "user_id": mem.get("user_id", ""),
                "title": mem.get("summary", ""),
                "summary": mem.get("summary", ""),
                "summary_status": "none",
                "summary_json": None,
                "has_summary": False,
                "message_count": int(mem.get("message_count", 0)),
                "has_trace": False,
                "mode": mem.get("mode", ""),
                "model": mem.get("model", ""),
                "created_at": mem.get("created_at"),
                "updated_at": mem.get("updated_at"),
            })
        out.sort(key=lambda s: s.get("updated_at") or 0, reverse=True)
        return out

    # ---------------- 统计 ----------------
    def record_metrics(self, *, tokens: int = 0, tool_calls: int = 0,
                        duration_ms: int = 0, error: bool = False) -> None:
        m = self.metrics
        m["requests_total"] += 1
        m["tokens_total"] += int(tokens or 0)
        m["tool_calls_total"] += int(tool_calls or 0)
        m["total_duration_ms"] += int(duration_ms or 0)
        if error:
            m["errors_total"] += 1


def _make_checkpointer() -> Any:
    """构造进程内共享的 checkpointer（MemorySaver）。

    必须在**进程内唯一**：/approve 依赖同一实例 resume 被 interrupt 的执行。
    """
    try:
        from langgraph.checkpoint.memory import MemorySaver  # type: ignore
        return MemorySaver()
    except Exception:
        return None


def _make_kb_admin() -> Any:
    """构造知识库管理器（失败时返回 None，接口层给出明确错误）。

    说明：Embedder 加载 bge-m3 较慢（本机实测 **数分钟**，会连带拖慢整个
    API 启动），这里改成**真正的懒构造**：

    · 返回一个 :class:`_LazyKBAdmin` 代理对象，构造它**不加载任何模型**；
    · 只有真正调用 ``KBAdmin`` 的方法时，才在首次访问里构造真实实例并缓存；
    · 构造失败（缺依赖 / 权重不可用）时抛 ``KBAdminError``，由接口层转成
      结构化 503 响应（与旧版行为一致）。

    这样 API 能在几秒内就绪，``/kb/*`` 接口在首次被调用时才付出加载代价。

    Returns:
        懒加载代理对象（构造失败时同样返回代理，由调用时抛错）。
    """
    return _LazyKBAdmin()


class _LazyKBAdmin:
    """知识库管理器的懒加载代理（不构造 Embedder / 不加载 bge-m3 权重）。

    用法与真实 ``KBAdmin`` 完全一致：``_kb().list_documents()`` 之类。
    线程安全：构造过程用锁串行化，避免并发请求重复加载模型。
    """

    def __init__(self) -> None:
        self._inner: Any = None
        self._error: Optional[BaseException] = None
        self._lock = threading.Lock()

    # -- 内部 ------------------------------------------------------------
    def _resolve(self) -> Any:
        """返回真实实例（必要时构造）；失败则抛 KBAdminError。"""
        if self._inner is not None:
            return self._inner
        with self._lock:
            if self._inner is not None:
                return self._inner
            if self._error is not None:
                self._raise()
            try:
                from ..knowledge.admin import KBAdmin  # noqa: PLC0415

                self._inner = KBAdmin()
            except Exception as exc:  # noqa: BLE001 - 统一转成结构化错误
                self._error = exc
                self._raise()
            return self._inner

    def _raise(self) -> None:
        """构造失败 → 抛 KBAdminError（结构化 503）。"""
        from ..knowledge.admin import KBAdminError  # noqa: PLC0415

        raise KBAdminError(
            code="KB_ADMIN_UNAVAILABLE",
            message="知识库管理组件不可用（Embedding 后端初始化失败）",
            hint="请检查 BGE_M3_PATH 指向的 bge-m3 权重目录是否有效。",
            status_code=503,
        )

    # -- 代理转发 --------------------------------------------------------
    def __getattr__(self, name: str) -> Any:
        # 只有在真正取属性时才解析实例（触发懒加载）
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._resolve(), name)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        state = "loaded" if self._inner is not None else "lazy"
        return f"<LazyKBAdmin {state}>"


def create_app(state: Optional[AppState] = None) -> Any:
    """创建 FastAPI 应用。

    ★★ 2026-11-23 性能修复：**lifespan 后台预热 BGE 重模型** ★★
      FastAPI 的 ``lifespan`` 上下文管理器在**应用启动时**执行，
      这里用它启动一个**守护线程**预热 bge-m3 + bge-reranker-v2-m3
      （实测合计约 **33s**）。

      为什么不用 ``@app.on_event("startup")``：该装饰器在新版 FastAPI 已废弃，
      而本机装的是 FastAPI **0.165.0**，``lifespan=`` 完全支持。

      ★ 关键：预热**不阻塞**启动。守护线程跑在后台，API 依然在 ~2s 内就绪
        （``restart_api.py`` 轮询 ``/health`` 的行为不变）。
      ★ 预热**失败不影响** API：异常全部吞掉并记录到 ``/health`` 的
        ``kb_warmup`` 字段；最坏情况只是「首请求慢一点」。
    """
    if not FASTAPI_AVAILABLE:
        raise RuntimeError("未安装 fastapi，无法启动 API 服务。请执行：pip install fastapi uvicorn")

    # ★ 打开 faulthandler（2026-11-23）：
    #   之前 /kb/reindex 全量重建时进程**直接消失**（既没有 traceback，
    #   也没有 Python 异常），只留下 Windows 事件日志里一条
    #   ``ucrtbase.dll / 0xc0000409``。开启 faulthandler 后，
    #   任何**原生级崩溃**（段错误 / abort / 栈溢出）都会把
    #   **所有线程的 Python 调用栈**打到 stderr —— 这才是定位的抓手。
    try:
        if not faulthandler.is_enabled():
            faulthandler.enable(all_threads=True)
    except Exception:  # noqa: BLE001 - 开启失败绝不能阻止 API 启动
        pass

    @asynccontextmanager
    async def _lifespan(app_: Any) -> Any:
        """应用生命周期钩子：启动时预热 BGE，关闭时不做阻塞操作。"""
        # ★ 后台守护线程预热重模型 —— 不阻塞启动，也不阻塞关停。
        try:
            from ..knowledge.tools import start_background_warmup  # noqa: PLC0415

            start_background_warmup()
        except Exception as exc:  # noqa: BLE001 - 预热失败绝不能阻止 API 启动
            logger.warning("BGE 预热启动失败（不影响 API）：%s: %s",
                           type(exc).__name__, exc)
        yield
        # ★ 关闭阶段**故意什么都不做**：预热线程是 daemon 线程，
        #   进程退出时会被自动回收，不必也不应该 join（join 会把
        #   关停时间拖长到 30s+）。

    app = FastAPI(
        title="Agent平台 API",
        description="通用 RAG + Agent 平台接入层（本地推理 · 可自定义知识库）",
        version="0.2.0",
        lifespan=_lifespan,
    )
    app.state.agent_state = state or AppState()
    app.state.started_at = time.time()

    def _st() -> AppState:
        return app.state.agent_state

    # ------------------------------------------------------------------
    # 静态工作台
    # ------------------------------------------------------------------
    if STATIC_DIR.is_dir():
        # 禁用缓存：保证改了前端 / 重启桌面版后，浏览器一定拿到最新资源
        _static_cls = NoCacheStaticFiles if FASTAPI_AVAILABLE else StaticFiles
        app.mount("/ui", _static_cls(directory=str(STATIC_DIR), html=True), name="ui")

    @app.get("/", include_in_schema=False)
    async def root() -> Any:
        """根路径重定向到 Web 工作台。"""
        return RedirectResponse(url="/ui/")

    # ------------------------------------------------------------------
    # 核心：图执行（SSE 与 JSON 共用同一条执行路径）
    # ------------------------------------------------------------------
    async def _run_graph_stream(graph: Any, initial: Dict[str, Any],
                                config: Dict[str, Any], thread_id: str,
                                user_label: str = "",
                                mode: str = "agent",
                                model: str = "",
                                max_rounds: int = DEFAULT_MAX_ROUNDS) -> Any:
        """真正分块地跑图，边跑边推 SSE 事件。

        事件序列：
            start   → 请求已受理（含 thread_id）
            step    → 每个节点完成（node / tokens_used / tool_calls /
                      **tool_calls 结构化参数** / **reasoning_summary 推理摘要**）
            trace   → **决策轨迹**（累积到当前的完整轨迹快照）
            approval→ 出现 HITL 待审批项（approvals 卡片数据）
            result  → 最终结果（携带与旧版完全一致的字段 + ``trace``）
            [DONE]  → 结束
        """
        yield _sse_line({"event": "start", "thread_id": thread_id,
                         "ts": time.time(), "message": user_label,
                         "mode": mode, "model": model})
        last_values: Dict[str, Any] = {}
        #: 已推送过的审批 tool_call_id（去重：同一个待审批项可能被多个
        #: updates/values 分片重复描述，推 3 次会让前端渲染 3 张重复卡片）
        sent_approvals: set = set()
        #: **正在等待人工审批的 tool_call_id 集合**（2026-11-24 新增）。
        #:
        #: 为什么需要：工具调用被 HITL 挂起时，轨迹里只有「调用」没有「结果」，
        #: 旧实现把它算成 ``ok=True``（绿色 ✓「成功」）—— 用户以为文件已写好，
        #: 实际**根本没执行**。现在用这个集合把对应条目标成
        #: ``ok=None`` / ``status="pending_approval"``，前端渲染成琥珀色「待审批」。
        pending_approval_ids: set = set()
        #: **决策轨迹累积器**（本次执行的完整工具调用链）
        #: 每项：``{seq, round, name, args, result, ok, error, reasoning_summary,
        #: action_summary, duration_ms, ts}``
        trace: List[Dict[str, Any]] = []
        #: 已配对过的调用（按 tool_call_id 去重，避免 assistant 消息被
        #: updates 分片重复描述时轨迹里出现两条一样的记录）
        traced_call_ids: set = set()
        #: 最近一次看到的推理摘要（用于给「随后发生的调用」补上思考段）
        pending_reasoning = ""

        def _emit_approvals(items: List[Dict[str, Any]]) -> None:
            """只推送尚未推送过的审批项。"""
            fresh = []
            for it in items or []:
                cid = it.get("tool_call_id", "")
                if cid and cid in sent_approvals:
                    continue
                if cid:
                    sent_approvals.add(cid)
                    #: **标记为「待审批」**（2026-11-24）：工具调用已发起
                    #: 但**尚未执行** —— 轨迹里对应的条目必须标成
                    #: ``ok=None`` / ``status="pending_approval"``，
                    #: 绝不能是绿色 ✓「成功」。
                    pending_approval_ids.add(str(cid))
                fresh.append(it)
            if fresh:
                # **可观测性（2026-11-22 新增）**：把「审批等待」也记进轨迹，
                # kind="approval" —— 时序图 / 瀑布图据此画出第三种步骤类型。
                # 审批条目**不占用**工具调用预算（tool_calls_used 不变）。
                for ap in fresh:
                    # ------------------------------------------------------------------
                    # **把对应的「工具调用」条目就地改成待审批**（2026-11-24 修复）
                    # ------------------------------------------------------------------
                    # 时序问题：agent 节点先产出「工具调用」（只有参数、没有结果），
                    # tools 节点随后发现需要审批 → 才产生 approval 事件。
                    # 所以**调用条目先于审批条目进入轨迹**，创建时还不知道
                    # 它会被挂起 → 当时算出了 ``ok=True``（绿色 ✓「成功」）。
                    #
                    # 真实后果：用户看到 ws_write_file「✓ 成功」，但文件根本没落地
                    # —— 这正是「代码都写不进去」的错觉来源。
                    #
                    # 现在：审批事件到达时，把同 id 的调用条目**就地**改成
                    # ``ok=None`` / ``status="pending_approval"``。
                    acid = str(ap.get("tool_call_id") or "")
                    for e in trace:
                        if acid and e.get("id") == acid and e.get("kind") == "tool":
                            if e.get("result") is None and not (e.get("error") or {}):
                                e["ok"] = None
                                e["status"] = "pending_approval"
                                e["action_summary"] = (
                                    "等待人工确认（尚未执行）："
                                    + str(ap.get("tool_name") or ""))
                            break
                    trace.append({
                        "seq": len(trace) + 1,
                        "round": 0,
                        "id": str(ap.get("tool_call_id") or ""),
                        "name": str(ap.get("tool_name") or "审批"),
                        "args": ap.get("args") or {},
                        "result": None,
                        "result_summary": "",
                        #: **三态**（2026-11-24 修复）：``ok=None`` = **待审批**，
                        #: 不是成功。此前这里硬编码 ``ok: True``，
                        #: 前端按 ``ok !== false`` 判定 → 渲染成绿色 ✓「成功」，
                        #: 用户以为文件已写好，实际**根本没执行**。
                        "ok": None,
                        "status": "pending_approval",
                        "error": {},
                        "duration_ms": None,
                        "reasoning_summary": "",
                        "action_summary": "等待人工确认：" + str(ap.get("tool_name") or ""),
                        "ts": time.time(),
                        "kind": "approval",
                        "started_at": time.time(),
                        "ended_at": None,
                        "tokens": None,
                        "permission": str(ap.get("permission") or ""),
                        "impact": str(ap.get("impact") or ""),
                        "reversible": ap.get("reversible", True),
                    })
                yield {"event": "approval", "thread_id": thread_id,
                       "approvals": fresh}

        def _budget_snapshot(values: Dict[str, Any]) -> Dict[str, Any]:
            """从图状态里取预算消耗（已用 / 上限）。

            BudgetGuard 的上限由 ``core/budget.py`` 决定，
            这里是**只读**展示，不参与判定。

            **回归点（2026-11-22 修复）**：此前直接读 ``values``，
            而 ``trace`` 事件是在 ``chunk_mode == "updates"`` 分支里 yield 的，
            此刻 ``last_values`` 还是**上一片**的状态（甚至空 dict），
            于是前端「决策轨迹」的预算条恒显示
            ``ReAct 轮次 0/8 · token 0/60000 · 工具调用 0/24``（实测）。
            现改为：**优先用图状态；状态缺失时用轨迹自身兜底**。

            **兜底口径修正（2026-11-24）**
            ----------------------------
            此前兜底是 ``rounds = len(trace)`` / ``tool_calls = len(trace)``。
            但 ``trace`` 里**混着三种条目**：

                · ``kind == "llm"``      —— 一轮模型调用（= 一轮）
                · ``kind == "tool"``     —— 一次工具调用
                · ``kind == "approval"`` —— 一次审批等待（**两者都不算**）

            拿总长度当轮次，就会出现「9/8」这种**越界**显示
            （9 条轨迹 ≠ 9 轮），也会把审批条目误算成工具调用。
            现在兜底改为**按 kind 精确计数**：

                · 轮次     = ``kind == "llm"`` 的条目数
                · 工具调用 = ``kind == "tool"`` 的条目数

            ★ 现在 ``rounds`` 已是 graph state 的**真实字段**
              （agent 节点每轮 LLM 调用后回写），兜底只是「状态还没到」
              时的临时近似。
            """
            values = values or {}
            #: 按 kind 精确统计轨迹条目（替代此前的 len(trace)）
            llm_entries = sum(1 for e in trace if e.get("kind") == "llm")
            tool_entries = sum(1 for e in trace if e.get("kind") == "tool")
            rounds = int(values.get("rounds") or 0)
            tool_calls = int(values.get("tool_calls_used") or 0)
            if rounds <= 0 and llm_entries > 0:
                rounds = llm_entries
            if tool_calls <= 0 and tool_entries > 0:
                tool_calls = tool_entries
            return {
                "rounds": rounds,
                "max_rounds": int(max_rounds or DEFAULT_MAX_ROUNDS),
                "tokens_used": int(values.get("tokens_used") or 0),
                "max_tokens": 60_000,
                "tool_calls_used": tool_calls,
                "max_tool_calls": 24,
                # **可观测性（2026-11-22 新增）**：上下文窗口上限（token）。
                # 前端「上下文占用」treemap 用它做总量 vs 上限的分母。
                # 这是**附加字段**，老客户端按字段名读取不受影响。
                "context_window": CONTEXT_WINDOW_TOKENS,
                # **可观测性（2026-11-22 新增）**：system prompt 的**真实字符数**。
                # 此前前端只能盲估（估算值不可信），这里直接给出真实值，
                # 让「上下文占用」treemap 至少这一块是**实测**而非猜测。
                "system_prompt_chars": _SYSTEM_PROMPT_CHARS,
            }

        try:
            async for chunk_mode, chunk in graph.astream(
                initial, config, stream_mode=["updates", "values"]
            ):
                if chunk_mode == "updates":
                    for evt in _summarize_updates(chunk, thread_id):
                        # ---- 累积决策轨迹 ----
                        reasoning = str(evt.get("reasoning_summary") or "")
                        if reasoning:
                            pending_reasoning = reasoning
                        calls = evt.get("tool_calls") or []
                        results = evt.get("tool_results") or []
                        # ---- 0) 登记「LLM 调用」条目（2026-11-22 新增，可观测性）----
                        # 模型思考往往占总耗时的**大头**（实测一次多工具问答里
                        # LLM 约 11.9s、工具合计仅 9ms）。只记工具调用会让
                        # 瀑布图时间轴严重失真，因此这里把每一轮模型调用
                        # 也作为独立步骤记录（kind="llm"）。
                        llm_added = False
                        if evt.get("llm_duration_ms") is not None:
                            llm_started = evt.get("llm_started_at")
                            llm_ended = evt.get("llm_ended_at")
                            trace.append(_build_llm_trace_entry(
                                len(trace) + 1,
                                started_at=llm_started,
                                ended_at=llm_ended,
                                duration_ms=evt.get("llm_duration_ms"),
                                # token 用本轮 delta 的增量更准，但 delta 给的是累计值，
                                # 这里取累计值（前端只展示量级，不做求和校验）。
                                tokens=evt.get("tokens_used"),
                                reasoning=pending_reasoning,
                                tool_names=evt.get("tools") or [],
                                model=str(model or ""),
                            ))
                            llm_added = True
                        if calls or results:
                            # ---- 1) 先登记「调用」条目（agent 节点 delta 只有参数）----
                            for call in calls:
                                cid = str(call.get("id") or "")
                                if cid:
                                    if cid in traced_call_ids:
                                        continue
                                    traced_call_ids.add(cid)
                                #: **待审批标记**（2026-11-24）：若这次调用
                                #: 已经在「等待人工审批」集合里 → 条目标成
                                #: ``ok=None``（琥珀色「待审批」），**不是**绿色 ✓。
                                is_pending = cid in pending_approval_ids
                                trace.append(_build_trace_entry(
                                    len(trace) + 1, call, None,
                                    reasoning=pending_reasoning,
                                    round_index=0,
                                    # **可观测性**：记录该步被观测到的真实时刻，
                                    # 瀑布图据此按时间比例定位（不是等宽列表）。
                                    started_at=evt.get("ts") or time.time(),
                                    tokens=evt.get("tokens_used"),
                                    pending=is_pending,
                                ))
                            # ---- 2) 再把「结果」挂到对应条目上（tools 节点 delta）----
                            # 结果按 tool_call_id 精确配对；配不上就按名称+顺序兜底。
                            for res_entry in results:
                                rcid = str(res_entry.get("tool_call_id") or "")
                                target = None
                                if rcid:
                                    target = next((e for e in trace if e.get("id") == rcid), None)
                                if target is None:
                                    target = next(
                                        (e for e in reversed(trace)
                                         if e.get("result") is None
                                         and e.get("name") == res_entry.get("name")),
                                        None,
                                    )
                                if target is None:
                                    # 实在配不上（例如历史消息里没有 id）→ 单独成条
                                    trace.append(_build_trace_entry(
                                        len(trace) + 1,
                                        {"id": rcid,
                                         "name": res_entry.get("name", ""), "args": {}},
                                        res_entry,
                                        reasoning=pending_reasoning,
                                        round_index=0,
                                        started_at=evt.get("ts") or time.time(),
                                        tokens=evt.get("tokens_used"),
                                    ))
                                    continue
                                # 就地回填结果 / 错误 / 状态 / 耗时
                                body = res_entry.get("result")
                                err = _trace_error_of(body)
                                target["result"] = body
                                target["result_summary"] = _truncate_text(body, 600) if body is not None else ""
                                #: **结果回来了 → 该调用不再是「待审批」**（2026-11-24）
                                if rcid:
                                    pending_approval_ids.discard(str(rcid))
                                #: ★ 之前这里是 ``ok = not err and not _is_tool_failure(body)``，
                                #:   缺了「body 是 None」的分支 → 工具被 HITL 挂起、
                                #:   尚未执行时，``ok`` 也会算出 True（绿色 ✓「成功」），
                                #:   用户以为文件已写好，实际根本没落地。
                                #: 现在：``body is None`` → 三态里的 **None**（待审批 / 未执行）。
                                if body is None and not err:
                                    target["ok"] = None
                                    target["status"] = "pending_approval"
                                else:
                                    target["ok"] = not err and not _is_tool_failure(body)
                                    target["status"] = "success" if target["ok"] else "error"
                                target["error"] = err
                                dur = _tool_duration_ms(res_entry)
                                if dur is not None:
                                    target["duration_ms"] = dur
                                    # **可观测性**：结束时刻 = 开始时刻 + 实测耗时。
                                    # 没有实测耗时就留 None（前端显示「–」，不伪造）。
                                    target["ended_at"] = (target.get("started_at") or time.time()) + dur / 1000.0
                                if target.get("tokens") is None and evt.get("tokens_used") is not None:
                                    target["tokens"] = evt.get("tokens_used")
                        # ---- 审批事件**先于**轨迹快照处理（2026-11-24 修复）----
                        # 时序问题：同一个 update 分片里既有「工具调用」又有
                        # 「待审批」，而 ``_emit_approvals`` 会把对应的调用条目
                        # **就地**改成 ``ok=None``（待审批）。
                        # 若先推 trace 快照再处理审批，前端拿到的第一份快照
                        # 仍是 ``ok=True``（绿色 ✓「成功」）→ 用户以为文件已写好。
                        # 因此这里把审批处理提到轨迹快照**之前**。
                        approvals_here = []
                        if evt.get("event") == "approval":
                            approvals_here = list(evt.get("approvals") or [])
                        # 轨迹快照事件（前端「决策轨迹」面板实时刷新）
                        # **注意**：这一段刻意放在 ``if calls or results:`` **之外** ——
                        # 2026-11-22 新增 LLM 调用条目后，最后一轮「无工具的纯回答」
                        # 也必须推一次快照，否则瀑布图会丢掉最后一根 LLM 条。
                        if calls or results or llm_added:
                            yield _sse_line({
                                "event": "trace",
                                "thread_id": thread_id,
                                "trace": trace,
                                "budget": _budget_snapshot(last_values),
                                #: **真实轮次**（2026-11-24）：此前是 ``len(trace)``，
                                #: 而 trace 混着 llm / tool / approval 三种条目，
                                #: 拿总长当轮次就会出现「9/8」这种越界显示。
                                #: 现在统一走 ``_budget_snapshot`` 的口径
                                #: （优先读 state 的真实 ``rounds``，
                                #: 兜底只统计 ``kind == "llm"`` 的条目）。
                                "rounds": _budget_snapshot(last_values)["rounds"],
                                # **可观测性（2026-11-22 新增）**：上下文窗口上限。
                                "context_window": CONTEXT_WINDOW_TOKENS,
                            })
                        if evt.get("event") == "approval":
                            for a in _emit_approvals(approvals_here):
                                yield _sse_line(a)
                            #: **再推一次轨迹快照**（2026-11-24）：
                            #: ``_emit_approvals`` 刚刚把对应的「工具调用」条目
                            #: 改成了 ``ok=None``（待审批），前端必须收到这份更新，
                            #: 否则仍会显示绿色 ✓「成功」。
                            yield _sse_line({
                                "event": "trace",
                                "thread_id": thread_id,
                                "trace": trace,
                                "budget": _budget_snapshot(last_values),
                                "rounds": _budget_snapshot(last_values)["rounds"],
                                "context_window": CONTEXT_WINDOW_TOKENS,
                            })
                            continue
                        yield _sse_line(evt)
                elif chunk_mode == "values":
                    last_values = chunk or {}
                    for a in _emit_approvals(last_values.get("pending_hitl") or []):
                        yield _sse_line(a)
        except Exception as exc:
            payload = {
                "status": "error",
                "answer": f"{type(exc).__name__}: {exc}",
                "thread_id": thread_id,
                "approvals": [],
                "tokens_used": 0,
                "tool_calls_used": 0,
                "stop_reason": "exception",
                "mode": mode,
                "model": model,
                "trace": trace,
                "budget": _budget_snapshot(last_values),
            }
            _st().record_metrics(error=True)
            yield _sse_line({"event": "result", **payload})
            yield "data: [DONE]\n\n"
            return

        payload = _build_response(last_values, thread_id)
        payload["mode"] = mode
        payload["model"] = model or ""
        #: **决策轨迹**（新增字段，向后兼容：老客户端按字段名读取不受影响）
        payload["trace"] = trace
        payload["budget"] = _budget_snapshot(last_values)
        #: Plan 模式：结构化计划单独推一个 plan 事件（前端渲染计划卡片）
        #:
        #: ⚠️ **必须按 mode 过滤**：``plan`` 存在 LangGraph 的 checkpointer state 里，
        #:    同一 thread 上一轮跑过 Plan 模式后，这个 key 会**残留**在 state 中。
        #:    若不按 mode 判断，后续的 Agent / Ask 轮次会**误推**一个 plan 事件，
        #:    前端就会给普通回答挂上一张"执行计划"卡片。
        #:    （这是实测发现的既有缺陷，与检索策略无关，但同样会干扰用户看答案。）
        plan = last_values.get("plan") if mode == "plan" else None
        if plan:
            payload["plan"] = plan
            yield _sse_line({"event": "plan", "thread_id": thread_id,
                             "plan": plan})
        _st().record_metrics(tokens=payload["tokens_used"],
                             tool_calls=payload["tool_calls_used"])
        #: 持久化本轮 assistant 消息（消息正文真正落盘）
        _persist_turn(_st(), thread_id, last_values, payload)
        yield _sse_line({"event": "result", **payload})
        yield "data: [DONE]\n\n"

    @app.post("/chat", response_model=None)
    async def chat(req: "ChatRequest") -> Any:
        """发起一次对话。

        返回 status：
            done             —— 正常完成，answer 为最终答案
            pending_approval —— 需要人工确认，approvals 给出确认卡片
            error            —— 执行失败（错误信息在 answer 中）

        SSE 模式（默认）会分块推送：start → step* → approval? → result → [DONE]。
        """
        st = _st()
        thread_id = req.thread_id or f"th-{uuid.uuid4().hex[:12]}"
        session_id = req.session_id or f"S-{uuid.uuid4().hex[:8]}"
        mode = normalize_mode(req.mode)
        model = (req.model or "").strip()
        #: **本次请求的轮次上限**（2026-11-24 新增）。
        #: 此前 ``/chat`` 不接受也不传递 ``max_rounds`` → 服务端永远用
        #: 硬编码的 8 轮，前端的设置对「对话」流程完全无效。
        max_rounds = int(req.max_rounds) if req.max_rounds else DEFAULT_MAX_ROUNDS

        graph = st.ensure_graph(model=model or None, mode=mode,
                                 max_rounds=max_rounds)
        budget = st.new_budget()

        initial: Dict[str, Any] = {
            "messages": [{"role": "user", "content": req.message}],
            "approvals": {},
            "pending_hitl": [],
            "tokens_used": 0,
            "tool_calls_used": 0,
            #: **轮次计数从 0 开始**（每次 LLM 调用由 agent 节点 +1 并回写）
            "rounds": 0,
            "stop_reason": None,
            "final_answer": None,
            "failure_counts": {},
            # ------------------------------------------------------------------
            # **跨轮重置（2026-11-23 修复：写不进文件）**
            # ------------------------------------------------------------------
            # 这两个计数器的语义是「**本轮**（单次用户请求）内不要重复做同一件事」
            # （见 prompts/system.md 规则 9），**不是**「整个会话累计」。
            #
            # 但它们是 graph state 字段，由 checkpointer（MemorySaver）
            # **按 thread_id 持久化**。此前 initial 状态重置了 ``failure_counts``
            # 却**漏了这两个** → 计数跨轮累加：
            #   用户让 agent 连写 3 个文件后，之后**每一轮新请求**的
            #   ``ws_write_file`` 都被 ``DUPLICATE_TOOL_CALL`` 拦截，
            #   表现为"代码都写不进去"，模型每轮只会解释"规则 9 阻止再调用"。
            #
            # 必须显式重置，否则旧 checkpoint 里的历史计数会一直生效。
            "tool_call_counts": {},
            "query_counts": {},
            "session_id": session_id,
            "thread_id": thread_id,
            "user_id": req.user_id,
            "mode": mode,
            "ask_rounds": 0,
        }
        config = {"configurable": {"thread_id": thread_id}}

        #: **登记本次用的图**（FIX 3）：``/approve`` 必须 resume **同一个图实例**
        #: —— 否则续跑时 ``max_rounds`` / 模型 / 模式会退回默认值。
        st.register_thread_graph(thread_id, graph)

        st.touch_session(thread_id, summary=req.message,
                         session_id=session_id, user_id=req.user_id,
                         mode=mode, model=model)
        #: 用户消息落盘（真正持久化，不只是计数）
        st.record_message(thread_id, role="user", content=req.message,
                          extra={"mode": mode, "model": model})

        want_stream = req.stream
        if want_stream is None:
            want_stream = True

        st.metrics["chat_total"] += 1
        started = time.time()

        if want_stream:
            return StreamingResponse(
                _run_graph_stream(graph, initial, config, thread_id, req.message,
                                  mode=mode, model=model, max_rounds=max_rounds),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        # 非流式：消费同一条流式路径，取最后的 result 事件
        payload: Dict[str, Any] = {}
        async for line in _run_graph_stream(graph, initial, config, thread_id, req.message,
                                            mode=mode, model=model,
                                            max_rounds=max_rounds):
            if line.startswith("data: ") and not line.startswith("data: [DONE]"):
                try:
                    obj = json.loads(line[6:].strip())
                except Exception:
                    continue
                if obj.get("event") == "result":
                    payload = obj
        payload.pop("event", None)
        st.metrics["total_duration_ms"] += int((time.time() - started) * 1000)
        return JSONResponse(payload)

    # ------------------------------------------------------------------
    @app.post("/approve", response_model=None)
    async def approve(req: "ApproveRequest") -> Any:
        """人工审批后续跑被 interrupt 的图。

        **必须使用同一个 thread_id 与同一个 checkpointer 实例。**

        **必须复用发起审批的那次 /chat 的图实例**（2026-11-24 修复）：
        此前这里调 ``ensure_graph()``（**无参数**）→ 另建一个图，
        于是续跑时 ``max_rounds`` / 模型 / 模式都退回默认值，
        用户在「对话」里设的轮次预算被悄悄重置。
        现在按 ``thread_id`` 取回原图；取不到（进程重启过等）才退回新建。
        """
        st = _st()
        #: **优先复用原图**（FIX 3）
        graph = st.thread_graph(req.thread_id)
        if graph is None:
            # 兜底：原图不在了（进程重启 / 跨实例审批）→ 用默认参数新建。
            #: ⚠️ 此时 max_rounds 会退回默认值，属于**已知降级**。
            graph = st.ensure_graph()
        st.metrics["approve_total"] += 1
        started = time.time()
        try:
            from langgraph.types import Command  # type: ignore
        except Exception as exc:
            raise HTTPException(status_code=500, detail=(
                "未安装 langgraph，无法使用 approve 续跑。请执行：pip install langgraph"
            )) from exc

        resume_payload = {
            "tool_call_id": req.tool_call_id,
            "approved": req.approved,
            "actor": req.actor,
            "reason": req.reason,
            "session_grant": req.session_grant,
            #: **快速通道开关**（2026-11-24 新增）：
            #: False = 快速路径（跳过第二次 LLM，直接用工具结果作答）；
            #: True  = 照旧跑第二次 LLM 生成自然语言总结。
            "summarize": bool(req.summarize),
        }
        config = {"configurable": {"thread_id": req.thread_id}}

        try:
            result = await asyncio.to_thread(
                graph.invoke,
                Command(resume=resume_payload),
                config,
            )
        except Exception as exc:
            st.record_metrics(error=True)
            return JSONResponse({
                "status": "error",
                "answer": f"{type(exc).__name__}: {exc}",
                "thread_id": req.thread_id,
                "approvals": [],
                "tokens_used": 0,
                "tool_calls_used": 0,
                "stop_reason": "exception",
                #: 结构与正常响应保持一致（前端不用特判异常分支）
                "trace": [],
            }, status_code=500)

        payload = _build_response(result, req.thread_id)
        #: **决策轨迹**（2026-11-24 新增，修复「审批后轨迹面板不更新」）。
        #:
        #: 真实故障：``/chat`` 走 ``_run_graph_stream``（SSE），轨迹是
        #: **边跑边攒**的；而 ``/approve`` 走 ``graph.invoke()``
        #: （一次性拿最终状态）→ ``_build_response`` 里**根本没有 trace**
        #: → 审批完成后「决策轨迹」面板拿不到新快照，
        #: 用户看到的仍是审批前那一帧
        #: （ws_write_file 标着琥珀色「待审批」，实际早已落盘）。
        #:
        #: 现在从最终 state 重建轨迹（口径与 ``/chat`` 一致：
        #: 三态 ``ok`` —— True / False / None+pending_approval）。
        payload["trace"] = _build_trace_from_state(result)
        #: **预算快照**（与 ``/chat`` 同构）：轨迹面板的轮次/token 条
        #: 依赖它，否则审批后轮次显示会退回 0。
        payload["budget"] = {
            "rounds": int(result.get("rounds", 0) or 0),
            "max_rounds": int(DEFAULT_MAX_ROUNDS),
            "tokens_used": int(result.get("tokens_used", 0) or 0),
            "max_tokens": 60_000,
            "tool_calls_used": int(result.get("tool_calls_used", 0) or 0),
            "max_tool_calls": 24,
            "context_window": CONTEXT_WINDOW_TOKENS,
        }
        #: **是否走了「审批快速通道」**（2026-11-24 新增）。
        #: True = 跳过了第二次 LLM，直接用工具结果作答（快）；
        #: False = 照旧跑了 LLM 生成自然语言总结。
        #: 前端可用它提示「本次为快速路径」。
        payload["fast_path"] = bool(result.get("approved_result_ready"))
        st.record_metrics(tokens=payload["tokens_used"],
                          tool_calls=payload["tool_calls_used"])
        st.metrics["total_duration_ms"] += int((time.time() - started) * 1000)
        st.touch_session(req.thread_id, summary=f"[审批] {req.actor} "
                                                 f"{'批准' if req.approved else '拒绝'}")
        #: 审批续跑的最终答案也落盘
        st.record_message(
            req.thread_id, role="assistant",
            content=str(payload.get("answer") or ""),
            citations=result.get("citations") or [],
            extra={"source": "approve", "approved": bool(req.approved),
                   "actor": req.actor, "stop_reason": payload.get("stop_reason")},
        )
        #: **本次审批已结束** → 丢弃线程的图引用（避免内存无限增长）。
        #: 若续跑后又出现新的待审批项，前端会再发一次 /chat，
        #: 那时会重新登记一个新的图。
        st.drop_thread_graph(req.thread_id)
        return JSONResponse(payload)

    # ------------------------------------------------------------------
    # ★ 轻量存活探针（2026-11-23 新增）
    # ------------------------------------------------------------------
    @app.get("/ping")
    async def ping() -> Any:
        """**极快**存活探针：只回答"进程还在不在"，不做任何子组件探测。

        为什么需要它
        ------------
        ``/health`` 会探测 LLM 端点、扫描工具注册表、查 SQLite —— 即使已经
        并发化，最坏情况仍要 1~3 秒。而桌面版的 ``probe_api`` 用 **3 秒**
        超时去探 ``/health``：一旦 ``/health`` 稍慢（或某个模型端点正在
        跑长生成、``--parallel 1`` 把它排在队列后面），探测就超时，
        桌面版于是误判"API 不可达"并直接退出。

        ``/ping`` 不碰模型、不碰知识库、不碰数据库 —— 它是**纯进程存活**
        检查，通常 < 5 ms。桌面版优先探它，只有在老版本 API（没有
        ``/ping``）上才回退到 ``/health``。

        返回体刻意保持极小，避免任何序列化开销。
        """
        return {"status": "ok", "ts": time.time()}

    # ------------------------------------------------------------------
    @app.get("/health")
    async def health() -> Any:
        """各组件健康状态。"""
        st = _st()
        llm_health: Dict[str, Any] = {"reachable": False, "error": "未初始化"}
        try:
            client = st.llm()
            # ★ 探测要用**独立的**短超时客户端（2026-11-23 修正）
            #   llama-server 以 ``--parallel 1`` 运行：同一时刻只处理一个请求，
            #   **/v1/models 也会排在正在进行的生成后面**。一次长生成（20 s+）
            #   就能让 /health 整体卡住 —— 前端首屏、回归脚本全部超时。
            #   这里**不能**改 client.config.timeout（那是真实对话用的 180 s），
            #   所以另建一个 3 s 超时、关闭重试的探测客户端。
            #
            # ★ 探测客户端必须**复用**（2026-11-23 修正）：
            #   每次请求都新建 LLMClient → 每次都新建 httpx.AsyncClient，
            #   而 httpx 客户端**从不关闭** → 连接池泄漏 + 未回收的传输层
            #   在 Windows 上会累积到把整个 API 进程搞崩（实测第 3 次
            #   /health 就进程退出，且没有 traceback）。
            #   这里把探测客户端缓存在 st 上（LLMClient 内部已按事件循环缓存
            #   AsyncOpenAI 实例，跨请求复用是安全的）。
            probe = _probe_client(st, client.config.base_url, client.config.model,
                                  client.config.api_key)
            llm_health = await probe.health()
        except Exception as exc:
            llm_health = {"reachable": False, "error": f"{type(exc).__name__}: {exc}"}

        # ★ 本地两个模型端点的可达性（工作台左侧「模型端点状态」）
        #
        # ★ 健康探测必须**快速失败 + 复用客户端**（2026-11-23 修正）
        #   见上方说明：短超时（3 s，llama-server --parallel 1 会把 /v1/models
        #   排在正在进行的生成后面）+ 复用客户端（避免 httpx 连接池泄漏）。
        #
        # ★★ 必须**并发**探测（2026-11-23 再修正）★★
        #   LOCAL_MODELS 的两个端点此前是**串行 await**，而每个探测在最坏情况
        #   下会吃满 3 s 读超时 —— 端口没人监听时还要叠加 Windows ~2 s 的
        #   TCP SYN 超时。两次串行就是 **4~6 秒**，叠加前面主 LLM 的一次
        #   探测，总计 **8~9 秒**。这正是 ``/health`` 慢到让桌面版
        #   ``probe_api``（3 s 超时）永远判定「API 不可达」的原因。
        #   两个端点彼此**完全独立**（不同端口、不同客户端），并发后
        #   总耗时 ≈ 单个探测的耗时，而不是两倍。
        endpoints: List[Dict[str, Any]] = []

        async def _probe_endpoint(name: str, info: Dict[str, Any]) -> Dict[str, Any]:
            """探测单个本地模型端点（异常收敛为 reachable=False）。"""
            probe = _probe_client(st, info["base_url"], info["model"], "local")
            try:
                h = await probe.health()
                return {
                    "model": name,
                    "label": info["label"],
                    "base_url": info["base_url"],
                    "reachable": bool(h.get("reachable")),
                    "error": h.get("error", ""),
                }
            except Exception as exc:
                return {
                    "model": name,
                    "label": info["label"],
                    "base_url": info["base_url"],
                    "reachable": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }

        if LOCAL_MODELS:
            probed = await asyncio.gather(
                *(_probe_endpoint(name, info) for name, info in LOCAL_MODELS.items())
            )
            endpoints.extend(probed)

        return {
            "status": "ok",
            "prompt_version": PROMPT_VERSION,
            "llm": llm_health,
            "registry": {
                "tool_count": len(st.registry.names()),
                "tools": st.registry.names(),
                "hitl_tools": [s.name for s in st.registry.list_specs() if s.needs_hitl],
                "builtin_tools": list(st.builtin_tool_names),
                "custom_tools": [d.name for d in st.custom_tools.list()],
                "custom_tool_count": len(st.custom_tools.list()),
            },
            "audit": st.audit.health(),
            "graph": {
                "built": st._graphs_built > 0,
                "checkpointer": type(st.checkpointer).__name__ if st.checkpointer else "memory",
                "checkpointer_shared": True,
                "graphs_built": st._graphs_built,
            },
            "endpoints": endpoints,
            "sessions": len(st.sessions),
            "tasks": len(st.tasks),
            "models": {
                "count": len(st.models.all_models()),
                "store": str(st.models.store_path),
            },
            #: **路由表 + 能力标签 + 降级保护自检**（docs/06 §4）
            #: violations 非空 = 存在「代码任务会静默降级到非代码模型」的配置错误
            "routing": _routing_report(st),
            "sessions_store": st.session_store.health(),
            # ★★ 2026-11-23：知识库重模型（BGE）预热状态 ★★
            #   前端据此显示「知识库模型加载中…」而不是让用户以为服务卡死。
            #   本机 bge-m3 + bge-reranker 冷加载约 **33s**（CPU 推理）。
            "kb_warmup": _kb_warmup_status(),
            # ★★ 2026-11-23：检索链的性能参数（线程数 / max_length / shortlist）
            #   便于「这次检索为什么慢」直接对照 /health 排查。
            "kb_runtime": _kb_runtime_info(),
            # **数据目录**（可用环境变量 AGENT_DATA_DIR 整体外置到别的盘）
            "data_dir": _data_dir_info(),
            "modes": [
                {"id": m, "label": MODE_LABELS[m], "description": MODE_DESCRIPTIONS[m],
                 "allowed_permissions": list(allowed_permissions(m))}
                for m in MODES
            ],
        }

    # ------------------------------------------------------------------
    @app.get("/tools")
    async def tools() -> Any:
        """列出所有工具契约（给人看的）。

        **向后兼容**：字段结构与旧版完全一致（``count`` + ``tools[]``，
        每项含 name / description / input_schema / output_schema / permission /
        hitl_required / timeout_ms / max_retries / version / idempotent / metadata）。

        新增的 ``detail_url`` 只是**提示**字段（指向 ``GET /tools/{name}``），
        供前端按需拉取单个工具的完整详情而不必把整份 schema 塞进列表响应。
        """
        st = _st()
        items = st.registry.list_specs_dict()
        for it in items:
            it["detail_url"] = f"/tools/{it['name']}"
        return {
            "count": len(items),
            "tools": items,
        }

    @app.get("/tools/{tool_name}")
    async def tool_detail(tool_name: str) -> Any:
        """**单个工具的完整详情**（给人看的完整契约）。

        与 ``GET /tools`` 的区别：列表接口保持轻量（虽然目前也带 schema，
        但本接口额外提供**面向展示的派生结构**），本接口返回：

            · ``tool``        —— ToolSpec 全字段（含完整 input_schema / output_schema）
            · ``openai_function`` —— 该工具在 OpenAI function calling 下
              **模型实际看到的**那个 JSON（这就是「模型看到的工具定义」）
            · ``params``      —— 由 input_schema 派生的**参数表**
              （名称 / 类型 / 必填 / 默认值 / 说明 / enum），前端直接渲染成表格
            · ``required`` / ``optional`` —— 必填与可选参数名列表
            · ``audit``      —— 该工具相关的审计动作名（提示会被记录哪些审计事件）

        不存在的工具返回 404（结构化错误）。
        """
        st = _st()
        spec = st.registry.get(tool_name)
        if spec is None:
            return JSONResponse(
                {
                    "code": "TOOL_NOT_FOUND",
                    "message": f"工具 '{tool_name}' 不存在",
                    "hint": "请调用 GET /tools 获取可用工具列表。",
                    "retryable": False,
                },
                status_code=404,
            )
        detail = spec.to_dict()
        detail["openai_function"] = spec.openai_function()
        detail["params"] = _schema_params(spec.input_schema)
        detail["required"] = list((spec.input_schema or {}).get("required") or [])
        detail["optional"] = [
            k for k in ((spec.input_schema or {}).get("properties") or {}).keys()
            if k not in detail["required"]
        ]
        detail["audit_actions"] = ["tool_call"]
        if spec.needs_hitl:
            detail["audit_actions"].append("hitl_decision")
        return {
            "name": spec.name,
            "tool": detail,
            "openai_function": spec.openai_function(),
            "params": detail["params"],
            "required": detail["required"],
            "optional": detail["optional"],
            "audit_actions": detail["audit_actions"],
        }

    # ------------------------------------------------------------------
    # 自定义工具（用户自助加工具）
    # ------------------------------------------------------------------
    def _custom_error(exc: CustomToolError) -> Any:
        """把 CustomToolError 转成合适的 HTTP 状态码。"""
        status = {
            "NAME_CONFLICT": 409,
            "NOT_FOUND": 404,
            "INVALID_NAME": 400,
            "INVALID_KIND": 400,
            "INVALID_PERMISSION": 400,
            "INVALID_SCHEMA": 400,
            "INVALID_CONFIG": 400,
        }.get(exc.code, 400)
        return JSONResponse(
            {"detail": str(exc), "code": exc.code, "hint": exc.hint},
            status_code=status,
        )

    @app.get("/custom-tools")
    async def list_custom_tools() -> Any:
        """列出全部自定义工具（含未启用）。"""
        st = _st()
        items = st.custom_tools.list()
        return {
            "count": len(items),
            "tools": [d.to_dict() for d in items],
            "builtin_tools": st.builtin_tool_names,
            "data_file": st.custom_tools.store.path,
        }

    @app.post("/custom-tools", response_model=None)
    async def create_custom_tool(req: "CustomToolRequest") -> Any:
        """新建自定义工具（与内置工具重名 → 409）。"""
        st = _st()
        payload = req.model_dump() if hasattr(req, "model_dump") else dict(req)
        try:
            dfn = st.custom_tools.create(payload)
        except CustomToolError as exc:
            return _custom_error(exc)
        return JSONResponse(dfn.to_dict(), status_code=201)

    @app.put("/custom-tools/{name}", response_model=None)
    async def update_custom_tool(name: str, req: "CustomToolRequest") -> Any:
        """更新自定义工具（改名 = 删除旧名 + 新建新名）。"""
        st = _st()
        payload = req.model_dump() if hasattr(req, "model_dump") else dict(req)
        try:
            dfn = st.custom_tools.update(name, payload)
        except CustomToolError as exc:
            return _custom_error(exc)
        return JSONResponse(dfn.to_dict())

    @app.delete("/custom-tools/{name}", response_model=None)
    async def delete_custom_tool(name: str) -> Any:
        """删除自定义工具（并从 ToolRegistry 下线）。"""
        st = _st()
        try:
            st.custom_tools.delete(name)
        except CustomToolError as exc:
            return _custom_error(exc)
        return JSONResponse({"ok": True, "deleted": name})

    @app.post("/custom-tools/{name}/toggle", response_model=None)
    async def toggle_custom_tool(name: str, enabled: bool = True) -> Any:
        """启用 / 停用自定义工具（快捷接口：``?enabled=false``）。"""
        st = _st()
        try:
            dfn = st.custom_tools.set_enabled(name, bool(enabled))
        except CustomToolError as exc:
            return _custom_error(exc)
        return JSONResponse(dfn.to_dict())

    @app.post("/custom-tools/{name}/test", response_model=None)
    async def test_custom_tool(name: str, req: "CustomToolTestRequest") -> Any:
        """**试运行**：不进入 Agent 回路，直接调用一次并返回真实结果 / 错误。

        **注意**：试运行放到线程里执行（``asyncio.to_thread``），
        否则自定义 HTTP 工具若指向本机 API，会把事件循环自己堵死（自调用死锁）。
        """
        st = _st()
        args = req.args if hasattr(req, "args") else {}
        try:
            outcome = await asyncio.to_thread(st.custom_tools.test, name, args or {})
        except CustomToolError as exc:
            return _custom_error(exc)
        return JSONResponse(outcome)

    # ------------------------------------------------------------------
    # 知识库管理（用户自助添加文档）
    # ------------------------------------------------------------------
    def _kb_error(exc: Any) -> Any:
        """把 KBAdminError 转成结构化错误响应 ``{code,message,retryable,hint}``。"""
        code = getattr(exc, "code", "KB_ADMIN_ERROR")
        status = int(getattr(exc, "status_code", 400) or 400)
        payload = {
            "code": code,
            "message": getattr(exc, "message", str(exc)),
            "retryable": bool(getattr(exc, "retryable", False)),
            "hint": getattr(exc, "hint", ""),
        }
        return JSONResponse(payload, status_code=status)

    def _kb() -> Any:
        """取知识库管理器（不可用时抛结构化错误）。

        注意：``st.kb_admin`` 是 :class:`_LazyKBAdmin` 代理，**不会**在 API 启动
        时加载 Embedder；真正的加载发生在首次调用 ``/kb/*`` 接口时。
        若加载失败，代理内部会抛 ``KBAdminError``（503），由上层统一转换。
        """
        st = _st()
        if st.kb_admin is None:
            from ..knowledge.admin import KBAdminError  # noqa: PLC0415

            raise KBAdminError(
                "KB_ADMIN_UNAVAILABLE",
                "知识库管理模块不可用（Embedder 初始化失败）",
                retryable=True,
                hint="请检查 BGE_M3_PATH 指向的 bge-m3 权重目录是否有效。",
                status_code=503,
            )
        return st.kb_admin

    # ------------------------------------------------------------------
    # 工作区（代码浏览 + 受控写入）
    # ------------------------------------------------------------------
    def _ws() -> Any:
        """取工作区实例（带审计）。"""
        st = _st()
        from ..core.workspace import get_workspace  # noqa: PLC0415

        return get_workspace(audit=st.audit)

    def _ws_error(exc: Any) -> Any:
        """把 WorkspaceError 转成结构化 JSON 响应（不 500）。"""
        payload = {
            "code": getattr(exc, "code", "WS_ERROR"),
            "message": getattr(exc, "message", str(exc)),
            "retryable": bool(getattr(exc, "retryable", False)),
            "hint": getattr(exc, "hint", ""),
        }
        for key in ("path", "current_mtime", "expected_mtime",
                    "occurrences", "candidates"):
            val = getattr(exc, "extra", {}).get(key) if hasattr(exc, "extra") else None
            if val is not None:
                payload[key] = val
        return JSONResponse(payload,
                            status_code=int(getattr(exc, "status_code", 400)))

    @app.get("/workspace/info")
    async def workspace_info() -> Any:
        """工作区元信息（根目录、大小上限、撤销栈深度、禁访问清单）。"""
        return _ws().info()

    @app.get("/workspace/tree")
    async def workspace_tree(path: str = "", depth: int = 1) -> Any:
        """返回目录树（目录在前，文件按名排序）。"""
        try:
            return _ws().tree(path, depth=depth)
        except Exception as exc:  # noqa: BLE001
            return _ws_error(exc)

    @app.get("/workspace/repomap")
    async def workspace_repomap(path: str = "", max_files: int = 400) -> Any:
        """返回**扁平文件清单（含行数）**，供运行观测面板的代码库 treemap 使用。

        **新增（2026-11-22，可观测性）**：``/workspace/tree`` 只给字节数，
        而 treemap 需要按**行数**定尺寸。路径校验与禁访问清单与 tree 完全一致。
        """
        try:
            return _ws().repomap(path, max_files=max_files)
        except Exception as exc:  # noqa: BLE001
            return _ws_error(exc)

    @app.get("/workspace/file")
    async def workspace_file(path: str = "") -> Any:
        """读取文件内容（二进制 / 过大文件明确报错）。"""
        try:
            return _ws().read_file(path)
        except Exception as exc:  # noqa: BLE001
            return _ws_error(exc)

    @app.get("/workspace/search")
    async def workspace_search(q: str = "", glob: str = "",
                               max: int = 50) -> Any:
        """工作区代码搜索（grep 风格）。"""
        try:
            return _ws().search(q, glob=glob, max_results=max)
        except Exception as exc:  # noqa: BLE001
            return _ws_error(exc)

    @app.post("/workspace/file", response_model=None)
    async def workspace_write(req: "WorkspaceWriteRequest") -> Any:
        """生成一个**待审批**的文件写入改动（**不落盘**）。

        返回 ``{change_id, diff, ...}``；真正落盘需调用
        ``POST /workspace/apply``（前端确认后）或由 Agent 的 HITL 审批流程完成。
        """
        try:
            ws = _ws()
            change = ws.stage_write(
                req.path, req.content,
                note=req.note or "",
                expected_mtime=req.expected_mtime,
            )
            st = _st()
            try:
                st.audit.log(
                    action="workspace_change_staged",
                    target=change["path"],
                    args={"path": change["path"], "change_id": change["change_id"]},
                    result={"added": (change.get("diff") or {}).get("added", 0),
                            "removed": (change.get("diff") or {}).get("removed", 0)},
                    status="pending_approval",
                    actor="operator",
                )
            except Exception:
                pass
            return JSONResponse(change, status_code=201)
        except Exception as exc:  # noqa: BLE001
            return _ws_error(exc)

    @app.post("/workspace/apply", response_model=None)
    async def workspace_apply(req: "WorkspaceApplyRequest") -> Any:
        """应用待审批改动（人工确认之后落盘）。

        这是**前端手工编辑**的落盘通道；Agent 发起的写操作走
        ``POST /approve``（LangGraph HITL）。两条路径共用同一套
        ``Workspace.apply_change``，因此**安全语义完全一致**：
        路径校验 → 并发冲突检测 → 撤销栈 → 原子写 → 审计。
        """
        try:
            ws = _ws()
            result = ws.apply_change(req.change_id, actor=req.actor or "operator",
                                      approved=bool(req.approved))
            return JSONResponse(result)
        except Exception as exc:  # noqa: BLE001
            return _ws_error(exc)

    @app.delete("/workspace/change/{change_id}")
    async def workspace_discard(change_id: str) -> Any:
        """放弃某个待审批改动。"""
        try:
            return JSONResponse(_ws().discard_change(change_id))
        except Exception as exc:  # noqa: BLE001
            return _ws_error(exc)

    @app.post("/workspace/undo", response_model=None)
    async def workspace_undo() -> Any:
        """撤销最近一次写入（恢复旧内容）。"""
        try:
            return JSONResponse(_ws().undo(actor="operator"))
        except Exception as exc:  # noqa: BLE001
            return _ws_error(exc)

    @app.get("/workspace/history")
    async def workspace_history(path: str = "") -> Any:
        """某个文件（或全部）的修改历史。"""
        try:
            return JSONResponse(_ws().history(path))
        except Exception as exc:  # noqa: BLE001
            return _ws_error(exc)

    # ------------------------------------------------------------------
    @app.get("/kb/documents")
    async def list_kb_documents() -> Any:
        """列出已入库文档。

        每项含：doc_id / title / doc_type / version / owner / security_level /
        visible_roles / product_line / chunk_count / size_bytes / updated_at /
        source / hit_count。
        """
        try:
            admin = _kb()
        except Exception as exc:  # noqa: BLE001
            return _kb_error(exc)

        def _work() -> Dict[str, Any]:
            # ★ 整个查询（含 ``_LazyKBAdmin`` 的首次解析）都放到线程里（2026-11-23 修复）：
            #   ``admin.list_documents()`` 会触发 ``_LazyKBAdmin._resolve()``，
            #   首次解析要构造 ``KBAdmin()`` → ``Embedder()`` → 加载 bge-m3 权重
            #   （本机走 CPU，实测 **30~50 秒**）。若留在事件循环里同步执行，
            #   **整个 FastAPI 事件循环会被堵死**，其它接口（含 /health）全部超时。
            docs = admin.list_documents()
            return {"documents": docs, "stats": admin.stats()}

        try:
            payload = await asyncio.to_thread(_work)
        except Exception as exc:  # noqa: BLE001
            return _kb_error(exc)
        docs = payload["documents"]
        return {
            "count": len(docs),
            "documents": docs,
            "stats": payload["stats"],
        }

    @app.post("/kb/documents", response_model=None)
    async def create_kb_document(req: "KbDocumentTextRequest") -> Any:
        """导入文档（**粘贴文本**方式）。

        请求体：``{title, text, doc_type, security_level, visible_roles,
        product_line, version}``。``text`` 可含 YAML front matter
        （``doc_id`` / ``title`` / ``version`` / ``doc_type`` / ``owner`` /
        ``security_level`` / ``product_line`` / ``visible_roles``）。
        """
        try:
            admin = _kb()
            payload = req.model_dump() if hasattr(req, "model_dump") else dict(req)
            # ★ 放到线程里跑（``asyncio.to_thread``）——
            # ``import_text`` 内部会做 chunk 切分 + **bge-m3 向量化**，
            # 这是 CPU 密集的同步调用。直接在事件循环里调会把整个
            # FastAPI 事件循环堵死（表现为「其它接口全部超时」）。
            #   另外 ``admin.import_text`` 的**属性访问**本身会触发
            #   ``_LazyKBAdmin._resolve()``（首次要加载 bge-m3 权重，30~50 秒），
            #   所以整段（含取方法）都必须推迟到工作线程里。
            def _work() -> Dict[str, Any]:
                return admin.import_text(
                    payload.get("text") or "",
                    title=payload.get("title") or "",
                    doc_id=payload.get("doc_id") or "",
                    doc_type=payload.get("doc_type") or "",
                    version=payload.get("version") or "",
                    owner=payload.get("owner") or "",
                    security_level=payload.get("security_level") or "",
                    visible_roles=payload.get("visible_roles") or None,
                    product_line=payload.get("product_line") or "",
                )

            result = await asyncio.to_thread(_work)
        except Exception as exc:  # noqa: BLE001
            return _kb_error(exc)
        _reset_shared_retriever()
        return JSONResponse(result, status_code=201)

    @app.post("/kb/documents/upload", response_model=None)
    async def upload_kb_document(
        file: UploadFile = File(..., description="要导入的文件（.md/.txt/.pdf/.docx）"),
        title: str = Form(""),
        doc_id: str = Form(""),
        doc_type: str = Form(""),
        version: str = Form(""),
        owner: str = Form(""),
        security_level: str = Form(""),
        visible_roles: str = Form(""),
        product_line: str = Form(""),
    ) -> Any:
        """导入文档（**上传文件**方式，multipart/form-data）。

        字段：``file``（必填）、``title`` / ``doc_id`` / ``doc_type`` /
        ``version`` / ``owner`` / ``security_level`` / ``visible_roles`` /
        ``product_line``（可选，``visible_roles`` 用逗号分隔）。
        """
        from ..knowledge.admin import MAX_UPLOAD_BYTES  # noqa: PLC0415

        try:
            admin = _kb()
        except Exception as exc:  # noqa: BLE001
            return _kb_error(exc)
        try:
            content = await file.read()
        except Exception as exc:  # noqa: BLE001
            return _kb_error(exc)
        if len(content) > MAX_UPLOAD_BYTES:
            return _kb_error(_kb_too_large(len(content)))
        try:
            # ★ 放到线程里跑 —— ``import_file`` 同样要做向量化（CPU 密集）。
            #   ``admin.import_file`` 的属性访问会触发懒加载解析（首次加载
            #   bge-m3 权重 30~50 秒），整段（含取方法）推迟到工作线程里。
            def _work() -> Dict[str, Any]:
                return admin.import_file(
                    file.filename or "upload",
                    content,
                    title=title,
                    doc_id=doc_id,
                    doc_type=doc_type,
                    version=version,
                    owner=owner,
                    security_level=security_level,
                    visible_roles=visible_roles or None,
                    product_line=product_line,
                )

            result = await asyncio.to_thread(_work)
        except Exception as exc:  # noqa: BLE001
            return _kb_error(exc)
        _reset_shared_retriever()
        return JSONResponse(result, status_code=201)

    @app.delete("/kb/documents/{doc_id}", response_model=None)
    async def delete_kb_document(doc_id: str) -> Any:
        """删除文档，并从索引中移除其全部 chunk。"""
        try:
            admin = _kb()
            # ★ 放到线程里跑（删除会连带清理向量，SQLite 写锁也可能短暂阻塞）。
            #   注意：``admin.delete_document`` 这个**属性访问本身**就会触发
            #   ``_LazyKBAdmin._resolve()``（首次要加载 bge-m3 权重，30~50 秒），
            #   如果在事件循环里求值就会把整个事件循环堵死 —— 所以必须用
            #   lambda 把「取方法」也推迟到工作线程里。
            result = await asyncio.to_thread(
                lambda: admin.delete_document(doc_id)
            )
        except Exception as exc:  # noqa: BLE001
            return _kb_error(exc)
        _reset_shared_retriever()
        return JSONResponse(result)

    @app.post("/kb/documents/{doc_id}/reindex", response_model=None)
    async def reindex_kb_document(doc_id: str) -> Any:
        """重建某文档的 chunk 并重新向量化。"""
        try:
            admin = _kb()
            # ★ 放到线程里跑 —— 重建要跑 bge-m3 向量化（CPU 密集，几秒到几十秒）。
            #   同上：``admin.reindex_document`` 的属性访问会触发懒加载解析，
            #   必须整体推迟到工作线程里，不能在事件循环里求值。
            result = await asyncio.to_thread(
                lambda: admin.reindex_document(doc_id)
            )
        except Exception as exc:  # noqa: BLE001
            return _kb_error(exc)
        _reset_shared_retriever()
        return JSONResponse(result)

    @app.post("/kb/reindex", response_model=None)
    async def reindex_kb_all(docs_dir: str = "") -> Any:
        """全量重建索引（**目录来源 + UI 导入来源** 双数据源）。

        与 ``python scripts/build_index.py --rebuild`` 共用同一份实现
        （:func:`agent_platform.knowledge.indexer.build_index`），
        因此 UI 导入的文档**不会**在全量重建后消失。

        ★ **必须放到线程里跑**（2026-11-23 修复）：
            全量重建要把**每个 chunk** 重新过一遍 bge-m3 向量化，
            在本机（bge-m3 走 CPU，因为显存被 llama-server 占满）实测
            单次重建耗时 **几分钟**。这段是**纯 CPU 同步调用**，
            之前直接写在 ``async def`` 里 → **整个 FastAPI 事件循环被堵死**，
            表现是「重建期间所有其它接口全部超时 / Connection refused」，
            连带把回归测试套件打挂。
            现在用 ``asyncio.to_thread`` 挪到工作线程，事件循环保持可响应。
            （``admin.reindex_all`` 的属性访问会触发懒加载解析，同样推迟到线程内。）
        """
        try:
            admin = _kb()
            result = await asyncio.to_thread(
                lambda: admin.reindex_all(docs_dir or None)
            )
        except Exception as exc:  # noqa: BLE001
            return _kb_error(exc)
        _reset_shared_retriever()
        return JSONResponse(result)

    @app.get("/kb/search")
    async def kb_probe(query: str, max_security_level: str = "internal",
                       roles: str = "", top_k: int = 6) -> Any:
        """直接对知识库做一次检索（**不经过 LLM**），用于验证导入是否立即可检索。

        这是 /kb/* 的配套探针端点：导入后可立刻确认「这块能不能被检索到」，
        且能验证 ACL 过滤（低密级用户检索不到 confidential 文档）。
        """
        try:
            from ..knowledge.retriever import Retriever, UserContext  # noqa: PLC0415
            from ..knowledge.tools import get_shared_retriever  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            return _kb_error(exc)
        try:
            ctx = UserContext(
                user_id="kb-probe",
                roles=[r for r in (roles or "").split(",") if r.strip()],
                max_security_level=max_security_level,
            )

            # ★ **整段（构造 + 检索）都放到线程里**（2026-11-23 修复）：
            #   之前只把 ``retrieve_with_trace`` 放进 ``asyncio.to_thread``，
            #   而 ``get_shared_retriever()`` 仍留在事件循环里**同步**调用。
            #   冷启动时它要构造 ``Embedder()`` → 加载 bge-m3 权重
            #   （本机走 CPU，实测 **30~50 秒**）→ **整个 FastAPI 事件循环
            #   被堵死**，表现为「冷启动期间所有其它接口（含 /health）全部超时」，
            #   客户端连接在 ~33s 后被 reset。
            #   现在构造与检索都在工作线程内完成，事件循环始终可响应。
            def _work() -> Any:
                retriever = get_shared_retriever()
                return retriever.retrieve_with_trace(query, ctx, int(top_k))

            res = await asyncio.to_thread(_work)
        except Exception as exc:  # noqa: BLE001
            return _kb_error(exc)
        hits = [
            {
                "chunk_id": h.get("chunk_id"),
                "doc_id": h.get("doc_id"),
                "doc_title": h.get("doc_title"),
                "text": h.get("text"),
                "rrf_score": h.get("rrf_score"),
                "rerank_score": h.get("rerank_score"),
                "source": h.get("source"),
                "section": h.get("section"),
            }
            for h in res.hits
        ]
        try:
            # ★ 同样放到线程里 —— ``admin.store`` 的属性访问会触发
            #   ``_LazyKBAdmin._resolve()``（首次加载 bge-m3 权重 30~50 秒），
            #   留在事件循环里会把整个服务堵死。
            doc_ids = {h.get("doc_id") for h in res.hits if h.get("doc_id")}

            def _record() -> None:
                admin = _kb()
                admin.store.record_hits(doc_ids)

            await asyncio.to_thread(_record)
        except Exception:  # noqa: BLE001
            pass
        return {
            "query": query,
            "max_security_level": max_security_level,
            "roles": ctx.roles,
            "hit_count": len(hits),
            "hits": hits,
            "reason": res.reason,
            "stage_trace": res.stage_trace,
        }

    # ------------------------------------------------------------------
    # 任务发布台
    # ------------------------------------------------------------------
    @app.get("/tasks/templates")
    async def task_templates() -> Any:
        """从评测种子集读取模板（按 L1-L6 分组）。"""
        rows = _load_seed_rows()
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            grouped.setdefault(r.get("category", "L?"), []).append({
                "id": r.get("id", ""),
                "category": r.get("category", ""),
                "query": r.get("query", ""),
                "tags": r.get("tags", []) or [],
                "unanswerable": bool(r.get("unanswerable", False)),
            })
        return {
            "count": len(rows),
            "source": str(SEED_DATASET),
            "categories": sorted(grouped.keys()),
            "groups": grouped,
            "templates": [t for cat in sorted(grouped) for t in grouped[cat]],
        }

    @app.get("/tasks")
    async def list_tasks() -> Any:
        """列出全部任务（内存态）。

        每项含：id/name/prompt/model/params/status/created_at/started_at/
        finished_at/duration_ms/tokens_used/tool_calls_used/error/result_summary。
        """
        st = _st()
        return {
            "count": len(st._task_order),
            "tasks": [_task_summary(st.tasks[t]) for t in st._task_order if t in st.tasks],
        }

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: str) -> Any:
        """返回**完整**任务对象。

        含完整 prompt、完整最终答案全文、工具调用轨迹、SSE 事件序列、
        错误详情、审计关联（thread_id / session_id）。
        """
        st = _st()
        record = st.tasks.get(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"任务 '{task_id}' 不存在")
        detail = dict(record)
        detail["id"] = record.get("task_id", task_id)
        detail["tool_trace"] = list(record.get("tool_trace") or [])
        detail["sse_events"] = list(record.get("sse_events") or [])
        detail["audit"] = {
            "thread_id": record.get("thread_id", ""),
            "session_id": record.get("session_id", ""),
            "user_id": record.get("user_id", ""),
        }
        detail["citations"] = list(record.get("citations") or [])
        detail["invalid_citations"] = list(record.get("invalid_citations") or [])
        detail["approvals"] = list(record.get("approvals") or [])
        return detail

    @app.get("/tasks/{task_id}/export")
    async def export_task(task_id: str, format: str = "json") -> Any:
        """导出任务完整结果（``format=json|md``），作为文件下载返回。"""
        st = _st()
        record = st.tasks.get(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"任务 '{task_id}' 不存在")
        fmt = (format or "json").strip().lower()
        safe_name = _safe_filename(record.get("name") or task_id)
        if fmt == "md":
            from fastapi.responses import PlainTextResponse

            body = _task_to_markdown(record)
            return PlainTextResponse(
                content=body,
                media_type="text/markdown; charset=utf-8",
                headers={
                    "Content-Disposition":
                        f'attachment; filename="{safe_name}.md"; '
                        f"filename*=UTF-8''{_url_quote(safe_name)}.md",
                },
            )
        body = json.dumps(record, ensure_ascii=False, indent=2, default=str)
        return JSONResponse(
            content=json.loads(body),
            headers={
                "Content-Disposition": f'attachment; filename="{safe_name}.json"; '
                f"filename*=UTF-8''{_url_quote(safe_name)}.json",
            },
        )

    @app.post("/tasks", response_model=None)
    async def create_task(req: "TaskCreateRequest") -> Any:
        """创建任务并后台异步执行。

        **复用现有的图执行逻辑**（同一条 astream 路径），不另写一套 Agent。
        """
        st = _st()
        st.metrics["tasks_total"] += 1
        task_id = f"tk-{uuid.uuid4().hex[:10]}"
        params = dict(req.params or {})
        prompt = (req.prompt or req.description or "").strip()
        task_mode = normalize_mode(req.mode)
        record: Dict[str, Any] = {
            "task_id": task_id,
            "id": task_id,
            "name": req.name,
            "description": req.description,
            "prompt": prompt,
            "model": req.model,
            "mode": task_mode,
            "params": params,
            "status": "queued",
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "duration_ms": None,
            "tokens_used": 0,
            "tool_calls_used": 0,
            "result_summary": "",
            "answer": "",
            "error": "",
            "thread_id": "",
            "session_id": "",
            "user_id": "task-runner",
            "stop_reason": None,
            "approvals": [],
            "citations": [],
            "invalid_citations": [],
            #: 工具调用轨迹（按发生顺序）：{seq,name,args,result_summary,result,ok,ts}
            "tool_trace": [],
            #: SSE 步骤事件序列：{event,node,tools,ts,...}
            "sse_events": [],
        }
        st.tasks[task_id] = record
        st._task_order.append(task_id)

        coro = _run_task(st, task_id)
        try:
            handle = asyncio.get_running_loop().create_task(coro)
            st._bg.append(handle)
            handle.add_done_callback(lambda h: st._bg.remove(h) if h in st._bg else None)
        except RuntimeError:
            # 没有运行中的事件循环（例如直接调用函数式用法）→ 同步兜底
            asyncio.run(coro)

        return JSONResponse(record, status_code=201)

    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 模型注册表（GET/POST/PUT/DELETE /models）
    # ------------------------------------------------------------------
    def _routing_report(st: Any) -> Dict[str, Any]:
        """导出**路由表 + 能力标签 + 降级保护自检**结果（供 /health 展示）。

        结构见 :meth:`agent_platform.core.llm_client.LLMRouter.routing_report`。
        ``violations`` 非空 = 存在「代码类任务会静默降级到不具备代码能力的
        模型」的配置错误，需要修 ``config/routing.yaml``。
        """
        try:
            router = st._router()
            report = router.routing_report()
        except Exception as exc:  # pragma: no cover - 路由未初始化时兜底
            return {"error": f"{type(exc).__name__}: {exc}",
                    "routes": {}, "capabilities": {}, "violations": []}
        return report

    def _lifecycle_manager() -> Any:
        """取**进程内唯一**的模型生命周期管理器。

        ★ 必须是单例：运行态（哪些 PID 是我们起的）要跨请求共享，
          否则「起模型」和「查状态」会落在两个不同的实例上，
          前端永远看到「未运行」。

        挂在 ``app.state`` 上而不是模块级全局，是为了**跟随 AppState
        的生命周期**（测试里可以换掉）。
        """
        mgr = getattr(app.state, "model_lifecycle", None)
        if mgr is None:
            from ..core.model_lifecycle import get_manager  # noqa: PLC0415
            mgr = get_manager()
            app.state.model_lifecycle = mgr
        return mgr

    def _model_public(rec: Dict[str, Any], probe: Optional[Dict[str, Any]] = None
                      ) -> Dict[str, Any]:
        """把注册表条目转成对外结构（**不泄露 api_key**）。"""
        out = {
            "name": rec.get("name", ""),
            "label": rec.get("label") or rec.get("name", ""),
            "base_url": rec.get("base_url", ""),
            "model": rec.get("model", ""),
            "description": rec.get("description", ""),
            "reasoning_effort": rec.get("reasoning_effort", "medium"),
            "temperature": rec.get("temperature", 0.2),
            "max_tokens": rec.get("max_tokens", 2048),
            "timeout": rec.get("timeout", 180),
            "supports_tools": bool(rec.get("supports_tools", True)),
            #: **能力标签**（code / tool_call / reasoning / chinese / long_context / vision）
            #: 供前端判断「这个模型能不能干这件事」，与路由的降级保护同一套语义
            "capabilities": list(rec.get("capabilities", []) or []),
            #: 是否**已安装**（routing.yaml 里可显式标 false 表示「占位未安装」）
            "installed": bool(rec.get("installed", True)),
            "source": rec.get("source", "custom"),
            "builtin": bool(rec.get("builtin", False)),
            "has_api_key": bool(rec.get("api_key", "")),
            "created_at": rec.get("created_at"),
            "updated_at": rec.get("updated_at"),
        }
        if probe:
            out["reachable"] = bool(probe.get("reachable"))
            out["error"] = str(probe.get("error", "") or "")
            out["latency_ms"] = int(probe.get("latency_ms", 0) or 0)
            out["server_models"] = list(probe.get("server_models", []) or [])
        else:
            out["reachable"] = None
            out["error"] = ""
        return out

    @app.get("/models")
    async def list_models(probe: bool = True) -> Any:
        """列出**全部可用模型**（注册表 + 端点可达性探测）。

        来源：
            · ``config/routing.yaml`` 的 ``models:`` 段
            · ``data/models.json``（运行时新增的自定义模型）

        ``probe=true``（默认）会**真实请求**每个端点的 ``/v1/models``，
        因此前端显示的可达性状态是探测结果，不是臆造。
        """
        st = _st()
        recs = st.models.all_models()
        probes: Dict[str, Dict[str, Any]] = {}
        if probe and recs:
            try:
                probes = await st.models.probe_all()
            except Exception:  # pragma: no cover
                probes = {}
        items = [_model_public(r, probes.get(r["name"])) for r in recs]
        return {
            "count": len(items),
            "models": items,
            "store": str(st.models.store_path),
            "probed": bool(probe and recs),
            "default": st.router.chain_for("agent")[0] if st.router else "gpt-oss-20b",
        }

    @app.post("/models", response_model=None)
    async def create_model(req: "ModelRequest") -> Any:
        """注册一个自定义模型（持久化到 ``data/models.json``，原子写）。"""
        st = _st()
        payload = req.model_dump() if hasattr(req, "model_dump") else dict(req)
        try:
            rec = st.models.create(payload)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        probe = await st.models.probe(rec["name"])
        return JSONResponse(_model_public(rec, probe), status_code=201)

    @app.put("/models/{name}", response_model=None)
    async def update_model(name: str, req: "ModelUpdateRequest") -> Any:
        """更新一个自定义模型（内置模型不可改 → 409）。"""
        st = _st()
        payload = req.model_dump() if hasattr(req, "model_dump") else dict(req)
        # 只覆盖**显式传入**的字段（None / 未提供都不改）
        payload = {k: v for k, v in payload.items() if v is not None}
        if name in {r["name"] for r in st.models.all_models() if r.get("builtin")}:
            return JSONResponse(
                {"detail": f"模型 '{name}' 来自 routing.yaml（内置），不可修改"},
                status_code=409)
        try:
            rec = st.models.update(name, payload)
        except ValueError as exc:
            code = 404 if "不存在" in str(exc) else 400
            return JSONResponse({"detail": str(exc)}, status_code=code)
        probe = await st.models.probe(name)
        return JSONResponse(_model_public(rec, probe))

    @app.delete("/models/{name}", response_model=None)
    async def delete_model(name: str) -> Any:
        """删除一个自定义模型（内置模型不可删 → 409）。"""
        st = _st()
        if name in {r["name"] for r in st.models.all_models() if r.get("builtin")}:
            return JSONResponse(
                {"detail": f"模型 '{name}' 来自 routing.yaml（内置），不可删除"},
                status_code=409)
        ok = st.models.delete(name)
        if not ok:
            return JSONResponse({"detail": f"模型 '{name}' 不存在"}, status_code=404)
        return JSONResponse({"ok": True, "deleted": name})

    # ------------------------------------------------------------------
    # 模型生命周期（启停本地 llama-server 端点）
    # ------------------------------------------------------------------
    # ★ 为什么单独一组端点：
    #   ``/models`` 讲的是「**有哪些模型可用**」（注册表语义），
    #   ``/models/lifecycle`` 讲的是「**哪些模型现在在跑**」（进程语义）。
    #   两者刻意分开 —— 前端下拉框该显示全部可选项，
    #   而开关面板只该显示本机能启停的端点。
    @app.get("/models/lifecycle")
    async def models_lifecycle() -> Any:
        """列出本地模型端点的**实时运行状态**（可启停的那些）。

        返回每个已知模型的：
            ``alias / label / port / running / pid / started_at /
            device / reachable``

        ``running`` = 端口上有人监听；``reachable`` = ``/v1/models``
        真的返回了模型列表（刚起来还没加载完时，前者为 True、后者为 False）。
        """
        st = _st()
        mgr = _lifecycle_manager()
        items = mgr.list_models()
        return {
            "count": len(items),
            "models": items,
            "default": "gpt-oss-20b",
            "note": "start/stop 均为异步操作：返回 status='starting' 后请轮询本接口",
        }

    @app.post("/models/{alias}/start", response_model=None)
    async def start_model(alias: str, background: BackgroundTasks) -> Any:
        """**启动**一个本地模型端点（幂等）。

        ★ 模型加载要 10~60 s，**绝不能**让 HTTP 请求同步等待
          （前端会挂死、网关会超时）。所以这里：

            1. 立刻检查「是不是已经在跑」→ 是则同步返回 ``already_running``；
            2. 否则把真正的启动动作丢给 ``BackgroundTasks``，
               并在内部用 ``asyncio.to_thread`` 跑阻塞的等待逻辑
               （``to_thread`` 走线程池，**不阻塞事件循环**）；
            3. 立即返回 ``status="starting"``，前端轮询
               ``GET /models/lifecycle`` 看 ``running``/``reachable`` 翻转。
        """
        st = _st()
        mgr = _lifecycle_manager()
        spec = mgr.get_spec(alias)
        if spec is None:
            return JSONResponse(
                {"status": "error", "alias": alias,
                 "message": f"未知模型 '{alias}'"},
                status_code=404,
            )

        # 已经在跑 → 同步返回（幂等），不启后台任务
        current = mgr.status(alias) or {}
        if current.get("running"):
            return JSONResponse({
                "status": "already_running",
                "alias": alias,
                "message": f"{current.get('label', alias)} 已在运行"
                           f"（端口 {current.get('port')}），无需重复启动",
                "model": current,
            })

        # ★ 后台任务：先 spawn（快速），再在线程里等就绪
        async def _do_start() -> None:
            try:
                await asyncio.to_thread(mgr.start_model, alias, wait=True)
            except Exception as exc:  # noqa: BLE001 - 后台任务绝不能把 API 打死
                logger.warning("后台启动模型 '%s' 失败：%s: %s",
                               alias, type(exc).__name__, exc)

        background.add_task(_do_start)
        return JSONResponse({
            "status": "starting",
            "alias": alias,
            "message": f"{spec.label} 正在启动（模型加载需 10~60 秒），"
                       f"请轮询 GET /models/lifecycle 查看就绪状态",
            "model": mgr.status(alias) or {},
        })

    @app.post("/models/{alias}/stop", response_model=None)
    async def stop_model(alias: str) -> Any:
        """**停止**一个本地模型端点（幂等）。

        停止是**快操作**（发终止信号 + 等待端口释放，最多十几秒），
        所以这里同步返回即可，不需要后台任务。
        """
        st = _st()
        mgr = _lifecycle_manager()
        if mgr.get_spec(alias) is None:
            return JSONResponse(
                {"status": "error", "alias": alias,
                 "message": f"未知模型 '{alias}'"},
                status_code=404,
            )
        result = mgr.stop_model(alias)
        status_code = 200 if result.get("status") in ("stopped", "not_running") else 500
        return JSONResponse(result, status_code=status_code)

    @app.post("/models/start-all", response_model=None)
    async def start_all_models(background: BackgroundTasks) -> Any:
        """**启动全部**本地模型端点（后台异步，立即返回）。"""
        st = _st()
        mgr = _lifecycle_manager()

        async def _do_start_all() -> None:
            try:
                await asyncio.to_thread(mgr.start_all, wait=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("后台启动全部模型失败：%s: %s",
                               type(exc).__name__, exc)

        background.add_task(_do_start_all)
        return JSONResponse({
            "status": "starting",
            "message": "正在启动全部模型（模型加载需 10~60 秒），"
                       "请轮询 GET /models/lifecycle 查看就绪状态",
            "models": mgr.list_models(),
        })

    @app.post("/models/stop-all", response_model=None)
    async def stop_all_models() -> Any:
        """**停止全部**本地模型端点（同步返回，逐个停止）。"""
        st = _st()
        mgr = _lifecycle_manager()
        results = mgr.stop_all()
        stopped = [r["alias"] for r in results if r.get("status") == "stopped"]
        return JSONResponse({
            "status": "stopped" if stopped else "not_running",
            "message": (f"已停止：{', '.join(stopped)}" if stopped
                        else "没有正在运行的模型"),
            "results": results,
        })

    # ==================================================================
    # ★★ 桌面版「一键启动」后端（2026-11-23 新增）★★
    #
    # 为什么要有这一组接口：
    #   用户要的是「**桌面上一键把平台拉起来，并自己选要跑哪些模型**」。
    #   之前只有 ``/models/{alias}/start`` 这类**单点**接口，前端要自己
    #   编排 N 次启 + M 次停；一旦中途失败就没有统一的收敛点，
    #   前端也不知道「整体还差什么」。
    #
    #   这里给出**编排层**的两个接口：
    #     · GET  /platform/status —— 平台整体状态（前端轮询它）
    #     · POST /platform/launch —— 按勾选一次性启停（幂等）
    #
    # ⚠️ 冲突提示：本块是**新增**的独立端点，与同文件里的 ``/health``
    #    （由并行 agent 正在改）**没有重叠**，不会产生行级冲突。
    #    唯一共享的是 ``_lifecycle_manager()`` 这个既有 helper。
    # ==================================================================

    #: 平台默认模型（与 ``/models/lifecycle`` 的 default 一致）
    PLATFORM_DEFAULT_MODEL = "gpt-oss-20b"

    @app.get("/platform/status", response_model=None)
    async def platform_status(request: Request) -> Any:
        """**平台整体状态**（桌面版「一键启动」的轮询目标）。

        ★ **必须快（< 500ms，实测 ~30ms）**：前端在启动过程中每 2 秒轮询一次，
          这个接口一旦慢，前端就会堆积请求。

          所以这里刻意**只做最便宜的事**：
            · 不扫知识库目录、不重建路由表；
            · 端口状态用 ``_listening_ports()``（一次 psutil 遍历，3~16ms），
              **不**走 ``ModelManager.list_models()`` 里逐端口
              ``socket.connect_ex()`` 的慢路径（Windows 上每个死端口 500ms）；
            · 只有**确实在监听**的端口才发一次 ``/v1/models`` 短超时探测
              （未监听的端口根本不会走到这一步）；
            · 知识库只报「就绪 / 未就绪」这个**布尔**，不做目录遍历。

        Returns:
            ``{api, models[], kb, summary, one_click_ready, default}``
        """
        mgr = _lifecycle_manager()

        # 本 API 自己的地址：``request.base_url`` 在内嵌启动（随机端口）
        # 与独立部署下都能拿到真实地址，前端可以直接拿去显示 / 复制。
        api_base = str(request.base_url).rstrip("/")
        api_port: Optional[int] = None
        try:
            api_port = int(request.url.port) if request.url.port else None
        except Exception:  # pragma: no cover - 理论上不会发生
            api_port = None

        # ---- 一次拿到全部监听端口（快路径）----
        listening = _listening_ports()
        specs = mgr.specs

        models_out: List[Dict[str, Any]] = []
        for alias, spec in specs.items():
            port = int(getattr(spec, "port", 0) or 0)
            running = port in listening
            pid = listening.get(port, 0) or None
            reachable = False
            if running:
                # 只有**真的在监听**才探测 /v1/models（短超时）
                reachable = await asyncio.to_thread(
                    _probe_models_endpoint, spec.base_url)
            models_out.append({
                "alias": alias,
                "label": getattr(spec, "label", alias) or alias,
                "running": bool(running),
                "reachable": bool(reachable),
                "device": getattr(spec, "device", "") or "",
                "port": port,
                "pid": pid,
            })

        running_list = [m for m in models_out if m["running"]]
        reachable_list = [m for m in models_out if m["running"] and m["reachable"]]

        # ---- 知识库：只要一个布尔位 ----
        # ★ 复用 ``_kb_warmup_status()``：它读的是**内存里的预热状态**，
        #   不做任何 I/O（``knowledge.tools.warmup_status()`` 只读模块级 dict）。
        kb_ready = False
        kb_state = "unknown"
        try:
            warm = _kb_warmup_status()
            kb_state = str(warm.get("state") or "unknown")
            kb_ready = bool(warm.get("ready"))
        except Exception:  # pragma: no cover - 诊断字段不该让接口失败
            kb_state = "unknown"

        # ---- 人类可读的一行结论 ----
        if not reachable_list:
            summary = ("API 未启动" if not models_out
                       else "缺少可用的模型服务")
        elif len(reachable_list) < len(running_list):
            summary = "模型加载中"
        elif kb_state not in ("ready", "done", "warm"):
            summary = "平台就绪（知识库模型预热中）"
        else:
            summary = "平台就绪"

        return JSONResponse({
            "api": {
                "reachable": True,
                "base_url": api_base,
                "port": api_port,
            },
            "models": models_out,
            "kb": {"ready": kb_ready, "state": kb_state},
            "summary": summary,
            "one_click_ready": bool(reachable_list),
            "default": PLATFORM_DEFAULT_MODEL,
        })

    @app.post("/platform/launch", response_model=None)
    async def platform_launch(background: BackgroundTasks,
                              payload: Optional[PlatformLaunchRequest] = None) -> Any:
        """**一键启动**：按用户勾选一次性拉起 / 停掉模型（幂等）。

        语义（用户明确要求）：**「选中的启动，没选中的停掉」**。

            · 请求体 ``{"models": ["gpt-oss-20b"]}``；
            · 空列表 / 缺省 → 用平台默认模型（``gpt-oss-20b``）；
            · 已勾选但**已经在跑** → 不重复起进程（幂等）；
            · **没勾选但正在跑** → 停掉。

        ★ **立刻返回**：模型加载要 3~60 秒，绝不能同步等待。
          这里只做「spawn 进程」这一下快动作，真正的「等就绪」丢给
          ``BackgroundTasks`` + ``asyncio.to_thread``（与
          ``/models/{alias}/start`` 完全同一套模式）。

        Returns:
            ``{status, started[], stopped[], message, models[]}``
            ``status`` ∈ ``starting`` / ``already_running`` / ``error``
        """
        mgr = _lifecycle_manager()
        known = mgr.aliases()

        # ---- 解析目标列表 ----
        requested: List[str]
        if payload is not None and payload.models:
            requested = [str(a).strip() for a in payload.models if str(a).strip()]
        else:
            requested = [PLATFORM_DEFAULT_MODEL]
        # 未知别名直接忽略（不报错）：前端可能带着过期选择来调用
        requested = [a for a in requested if a in known]
        if not requested:
            requested = [PLATFORM_DEFAULT_MODEL] if PLATFORM_DEFAULT_MODEL in known else []

        # ---- 先看现状（幂等的关键）----
        # ★ 用**快路径**看「谁在跑」：``mgr.list_models()`` 内部对每个端口
        #   做 ``socket.connect_ex()``，Windows 上对一个死端口要等 500ms
        #   （实测 1 秒/次），会让这个「要立刻返回」的接口变得很慢。
        #   这里只问「端口有没有人在听」，用一次 psutil 遍历（3~16ms）。
        listening = _listening_ports()
        specs = mgr.specs

        def _is_running(alias: str) -> bool:
            spec = specs.get(alias)
            port = int(getattr(spec, "port", 0) or 0)
            return port in listening

        to_start = [a for a in requested if not _is_running(a)]
        already = [a for a in requested if _is_running(a)]
        # 没勾选但正在跑 → 停掉
        to_stop = [a for a in known if a not in requested and _is_running(a)]

        stopped: List[str] = []
        errors: List[Dict[str, Any]] = []

        # ---- 停止（同步操作，十几秒内完成）----
        for alias in to_stop:
            try:
                res = mgr.stop_model(alias)
            except Exception as exc:  # noqa: BLE001 - 单个失败不该整体失败
                errors.append({"alias": alias,
                               "message": f"{type(exc).__name__}: {exc}"})
                continue
            if res.get("status") == "stopped":
                stopped.append(alias)
            elif res.get("status") == "error":
                errors.append({"alias": alias,
                               "message": str(res.get("message") or "停止失败")})

        # ---- 启动（后台异步）----
        # ★ 用 ``asyncio.to_thread`` 包住**阻塞的等待**：``to_thread``
        #   走线程池，不阻塞事件循环 —— 与既有 /models/{alias}/start 一致。
        async def _do_start(aliases: List[str]) -> None:
            for alias in aliases:
                try:
                    await asyncio.to_thread(mgr.start_model, alias, wait=True)
                except Exception as exc:  # noqa: BLE001 - 后台任务绝不能把 API 打死
                    logger.warning("后台启动模型 '%s' 失败：%s: %s",
                                   alias, type(exc).__name__, exc)

        if to_start:
            background.add_task(_do_start, to_start)

        # ---- 汇总结论 ----
        if errors and not to_start and not stopped:
            status = "error"
        elif to_start:
            status = "starting"
        elif already or stopped:
            status = "already_running"
        else:
            status = "already_running"

        parts: List[str] = []
        if to_start:
            parts.append("启动 " + str(len(to_start)) + " 个")
        if stopped:
            parts.append("停止 " + str(len(stopped)) + " 个")
        if already and not to_start:
            parts.append("所选模型已在运行")
        if errors:
            parts.append(str(len(errors)) + " 项失败")
        message = "；".join(parts) if parts else "状态已符合预期"
        # 只有**真的要加载模型**时才提示「需要 10~60 秒」——
        # 已经就绪的场景不该再吓用户一跳。
        if to_start:
            message += "（模型加载需 10~60 秒，请轮询 GET /platform/status）"

        # ---- 响应里的模型清单（快路径：只看「在不在跑」）----
        # 语义：``already`` 是「本来就在跑且仍被选中」；
        # ``to_start`` 里的进程**已经 spawn**（后台线程在等就绪），也算在跑；
        # ``to_stop`` 里的刚被停掉。
        models_out = [{
            "alias": a,
            "label": getattr(specs.get(a), "label", a) or a,
            "running": (a in already) or (a in to_start),
            "reachable": False,
            "device": getattr(specs.get(a), "device", "") or "",
            "port": int(getattr(specs.get(a), "port", 0) or 0),
            "pid": None,
        } for a in known]

        return JSONResponse({
            "status": status,
            "started": to_start,
            "stopped": stopped,
            "already_running": already,
            "requested": requested,
            "errors": errors,
            "message": message,
            "models": models_out,
        })

    # ------------------------------------------------------------------
    # 会话消息 / 摘要
    # ------------------------------------------------------------------
    @app.get("/sessions")
    async def list_sessions() -> Any:
        """列出会话（含摘要状态：已生成 / 未生成）。"""
        st = _st()
        items = st.list_sessions_merged()
        return {
            "count": len(items),
            "sessions": items,
            "store": st.session_store.health(),
        }

    @app.get("/sessions/{thread_id}")
    async def get_session(thread_id: str) -> Any:
        """返回会话元信息 + 已生成的摘要（若已生成）。"""
        st = _st()
        sess = st.session_store.get_session(thread_id)
        mem = st.sessions.get(thread_id) or {}
        if sess is None and not mem:
            raise HTTPException(status_code=404, detail=f"会话 '{thread_id}' 不存在")
        item = sess or {
            "thread_id": thread_id,
            "session_id": mem.get("session_id", ""),
            "user_id": mem.get("user_id", ""),
            "title": mem.get("summary", ""),
            "summary": "",
            "summary_status": "none",
            "summary_json": None,
            "has_summary": False,
            "message_count": int(mem.get("message_count", 0)),
            "mode": mem.get("mode", ""),
            "model": mem.get("model", ""),
            "created_at": mem.get("created_at"),
            "updated_at": mem.get("updated_at"),
        }
        item = dict(item)
        item["summary"] = item.get("summary_json") or item.get("summary") or None
        item["summary_text"] = (item.get("summary_json") or {}).get("conclusion", "") \
            if isinstance(item.get("summary_json"), dict) else item.get("summary", "")
        item["message_count"] = st.session_store.count_messages(thread_id) \
            or int(item.get("message_count") or 0)
        return item

    @app.get("/sessions/{thread_id}/messages")
    async def get_session_messages(thread_id: str, limit: int = 500) -> Any:
        """返回该会话的**完整消息历史**（有序）。

        每项含：``seq / role / content / citations / tool_calls / ts``。
        消息来自 ``data/sessions.db``（SQLite），**重启后仍在**。
        """
        st = _st()
        msgs = st.session_store.get_messages(thread_id, limit=int(limit))
        sess = st.session_store.get_session(thread_id) or {}
        return {
            "thread_id": thread_id,
            "count": len(msgs),
            "messages": msgs,
            "session": {
                "thread_id": thread_id,
                "title": sess.get("title", ""),
                "summary_status": sess.get("summary_status", "none"),
                "message_count": int(sess.get("message_count", 0) or 0),
                "created_at": sess.get("created_at"),
                "updated_at": sess.get("updated_at"),
            },
            "notice": ("消息历史持久化在 SQLite 中，重启后仍可查看；"
                       "但 LangGraph checkpointer 是进程内内存态，"
                       "重启后无法从历史中间断点续跑 ReAct，只能基于历史继续提问。"),
        }

    @app.delete("/sessions/{thread_id}", response_model=None)
    async def delete_session(thread_id: str) -> Any:
        """删除会话（含全部消息）。"""
        st = _st()
        ok = st.session_store.delete_session(thread_id)
        st.sessions.pop(thread_id, None)
        if not ok:
            return JSONResponse({"detail": f"会话 '{thread_id}' 不存在"}, status_code=404)
        return JSONResponse({"ok": True, "deleted": thread_id})

    @app.post("/sessions/{thread_id}/summarize", response_model=None)
    async def summarize_session(thread_id: str) -> Any:
        """用本地模型（``summarize`` 路由 → qwen2.5-14b）生成**结构化摘要**。

        摘要结构：一句话结论 + 关键要点列表 + 涉及的文档/引用 + 待办/未解决问题。
        结果**持久化**到 ``data/sessions.db`` 的 ``sessions.summary_json``。
        """
        st = _st()
        msgs = st.session_store.get_messages(thread_id, limit=200)
        if not msgs:
            return JSONResponse(
                {"detail": f"会话 '{thread_id}' 还没有消息，无法生成摘要"},
                status_code=400)
        text = _summarize_prompt(msgs)
        try:
            resp = await st.router_client("summarize").chat(
                [{"role": "system", "content": _SUMMARIZE_SYSTEM_PROMPT},
                 {"role": "user", "content": text}])
        except Exception as exc:
            return JSONResponse(
                {"detail": f"摘要模型调用失败：{type(exc).__name__}: {exc}"},
                status_code=502)
        raw = resp.get("content", "") or ""
        summary = _normalize_summary(raw)
        summary["model"] = getattr(
            getattr(st.router_client("summarize"), "config", None), "model", "") or ""
        summary["thread_id"] = thread_id
        summary["message_count"] = len(msgs)
        summary["generated_at"] = time.time()
        st.session_store.save_summary(thread_id, summary)
        return JSONResponse(summary)


    # ------------------------------------------------------------------
    @app.get("/metrics")
    async def metrics() -> Any:
        """简易运行统计。"""
        st = _st()
        m = dict(st.metrics)
        done = m["requests_total"]
        m["avg_duration_ms"] = round(m["total_duration_ms"] / done, 1) if done else 0.0
        m["sessions"] = len(st.sessions)
        m["tasks"] = len(st.tasks)
        m["uptime_seconds"] = round(time.time() - getattr(app.state, "started_at", time.time()), 1)
        return m

    # ------------------------------------------------------------------
    @app.get("/audit")
    async def audit_records(limit: int = 50,
                            session_id: str = "",
                            thread_id: str = "",
                            action: str = "") -> Any:
        """**审计记录**（append-only，供「决策轨迹」旁的历史核对入口）。

        每次工具调用都会由 ``ToolRegistry`` 写一条 ``tool_call`` 审计；
        HITL 决策写 ``hitl_decision``；工作区写入写 ``workspace_*``。
        这里只做**只读**查询，不提供任何修改入口。

        Args:
            limit: 返回条数（1~500）。
            session_id: 按会话过滤（可选）。
            thread_id: 按线程过滤（可选，客户端侧过滤）。
            action: 按动作名过滤（可选，客户端侧过滤）。

        Returns:
            ``{"count":n,"mode":<审计后端>,"records":[...]}``
        """
        st = _st()
        lim = max(1, min(int(limit or 50), 500))
        try:
            rows = st.audit.recent(limit=lim, session_id=session_id or None)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                {"code": "AUDIT_READ_FAILED", "message": str(exc),
                 "retryable": True, "hint": "审计后端暂不可用。"},
                status_code=503)
        if thread_id:
            rows = [r for r in rows if str(r.get("thread_id") or "") == thread_id]
        if action:
            rows = [r for r in rows if str(r.get("action") or "") == action]
        # JSON 字段（SQLite / JSONL 模式）是字符串）→ 尝试解析回对象
        out: List[Dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            for key in ("args", "result", "extra"):
                val = item.get(key)
                if isinstance(val, str) and val:
                    try:
                        item[key] = json.loads(val)
                    except Exception:
                        pass
            out.append(item)
        return {
            "count": len(out),
            "mode": st.audit.mode,
            "records": out,
        }

    return app



# ----------------------------------------------------------------------
# 摘要 / 持久化辅助
# ----------------------------------------------------------------------
#: 会话摘要的系统提示词（要求输出**结构化 JSON**）
_SUMMARIZE_SYSTEM_PROMPT = """你是一个会话总结助手。请阅读下面的对话，输出**严格的 JSON**（不要 markdown 围栏，不要额外解释）：

{
  "conclusion": "一句话结论（不超过 60 字）",
  "key_points": ["关键要点 1", "关键要点 2"],
  "documents": ["涉及的文档名 / chunk_id / 引用标识"],
  "open_questions": ["待办或尚未解决的问题"]
}

要求：
1. conclusion 概括本次会话最终得到的结论；
2. key_points 列出 2-6 条要点，每条不超过 40 字；
3. documents 只列对话中**真实出现**过的文档名或引用标识，没有就空数组；
4. open_questions 列出会话里遗留的待办 / 疑问，没有就空数组；
5. 只输出 JSON 对象本身。"""


def _summarize_prompt(messages: List[Dict[str, Any]]) -> str:
    """把消息列表拼成给摘要模型的输入文本。"""
    lines: List[str] = []
    for m in messages:
        role = m.get("role", "")
        content = str(m.get("content", "") or "")
        if not content:
            continue
        who = {"user": "用户", "assistant": "助手", "tool": "工具",
               "system": "系统"}.get(role, role)
        lines.append(f"[{who}] {content}")
    return "\n".join(lines)[-12000:]


def _normalize_summary(raw: Any) -> Dict[str, Any]:
    """把模型输出归一成结构化摘要。"""
    from agent_platform.core.modes import extract_json_object  # noqa: PLC0415

    obj = None
    if isinstance(raw, dict):
        obj = raw
    elif isinstance(raw, str):
        obj = extract_json_object(raw)
    if obj is None:
        text = str(raw or "").strip()
        return {
            "conclusion": text[:200] or "（摘要模型未返回结构化结果）",
            "key_points": [],
            "documents": [],
            "open_questions": [],
            "parsed": False,
        }

    def _as_list(v: Any) -> List[str]:
        if isinstance(v, str):
            return [v.strip()] if v.strip() else []
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return []

    return {
        "conclusion": str(obj.get("conclusion") or obj.get("summary") or "").strip()[:300],
        "key_points": _as_list(obj.get("key_points"))[:8],
        "documents": _as_list(obj.get("documents"))[:12],
        "open_questions": _as_list(obj.get("open_questions"))[:8],
        "parsed": True,
    }


def _persist_turn(st: AppState, thread_id: str, values: Dict[str, Any],
                  payload: Dict[str, Any]) -> None:
    """把本轮 assistant 回答落盘（正文 + 引用 + 工具调用摘要 + 决策轨迹）。"""
    try:
        answer = str(payload.get("answer") or "")
        citations = list(values.get("citations") or [])
        tool_calls: List[Dict[str, Any]] = []
        for m in (values.get("messages") or []):
            d = m if isinstance(m, dict) else _msg_to_dict(m)
            if d.get("role") == "assistant" and d.get("tool_calls"):
                for tc in d["tool_calls"]:
                    fn = tc.get("function", {}) or {}
                    name = fn.get("name") or tc.get("name") or ""
                    args = tc.get("args")
                    if args is None:
                        args = fn.get("arguments", "{}")
                    tool_calls.append({"name": name, "args": args})
        extra = {
            "tokens_used": payload.get("tokens_used", 0),
            "tool_calls_used": payload.get("tool_calls_used", 0),
            "stop_reason": payload.get("stop_reason", ""),
            "mode": payload.get("mode", ""),
            "model": payload.get("model", ""),
        }
        #: 同上：``plan`` 可能是上一轮 Plan 模式残留在 state 里的，
        #: 只有本轮确实是 Plan 模式才落盘。
        if str(payload.get("mode") or "") == "plan":
            plan = values.get("plan")
            if plan:
                extra["plan"] = plan
        #: **决策轨迹落盘**（历史会话恢复时同样能看轨迹）
        #:
        #: 轨迹项里可能带完整工具结果（体积大），这里做**瘦身**：
        #: 保留结构（参数 / 状态 / 耗时 / 错误 / 推理摘要），
        #: 但把 ``result`` 截断到 4000 字符，避免 SQLite 膨胀。
        trace = payload.get("trace") or []
        if trace:
            slim: List[Dict[str, Any]] = []
            for item in trace:
                row = dict(item)
                body = row.get("result")
                if body is not None:
                    text = _truncate_text(body, 4000)
                    row["result"] = text
                    row["result_truncated"] = len(str(body)) > 4000
                slim.append(row)
            extra["trace"] = slim
        #: **预算快照**（已用 / 上限）
        budget = payload.get("budget")
        if budget:
            extra["budget"] = budget
        st.record_message(thread_id, role="assistant", content=answer,
                          tool_calls=tool_calls, citations=citations,
                          extra=extra)
    except Exception:  # pragma: no cover - 落盘失败不阻断主流程
        pass

# ----------------------------------------------------------------------
# 任务执行（后台）
# ----------------------------------------------------------------------
async def _run_task(st: AppState, task_id: str) -> None:
    """后台执行一个任务：复用 /chat 的图执行逻辑。

    状态流转：queued → running → done / failed
    """
    record = st.tasks.get(task_id)
    if record is None:
        return

    # 让 queued 状态至少可见一瞬，便于前端观察到状态机流转
    await asyncio.sleep(0.05)
    record["status"] = "running"
    record["started_at"] = time.time()

    params = record.get("params") or {}
    try:
        max_rounds = params.get("max_rounds")
        max_rounds = int(max_rounds) if max_rounds else None
    except Exception:
        max_rounds = None
    try:
        temperature = params.get("temperature")
        temperature = float(temperature) if temperature is not None else None
    except Exception:
        temperature = None
    effort = params.get("reasoning_effort") or params.get("effort")
    try:
        max_tokens = params.get("max_tokens")
        max_tokens = int(max_tokens) if max_tokens else None
    except Exception:
        max_tokens = None

    thread_id = f"th-{uuid.uuid4().hex[:12]}"
    record["thread_id"] = thread_id
    record["params"] = {
        "max_rounds": max_rounds,
        "temperature": temperature,
        "reasoning_effort": effort,
        "max_tokens": max_tokens,
    }

    try:
        graph = st.make_graph(model=record.get("model"), temperature=temperature,
                               effort=effort, max_tokens=max_tokens,
                               max_rounds=max_rounds,
                               mode=record.get("mode") or "agent")
    except Exception as exc:
        record["status"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["finished_at"] = time.time()
        record["duration_ms"] = int((record["finished_at"] - record["started_at"]) * 1000)
        return

    session_id = f"S-{uuid.uuid4().hex[:8]}"
    record["session_id"] = session_id
    initial: Dict[str, Any] = {
        "messages": [{"role": "user", "content": record.get("prompt", "")}],
        "approvals": {},
        "pending_hitl": [],
        "tokens_used": 0,
        "tool_calls_used": 0,
        "stop_reason": None,
        "final_answer": None,
        "failure_counts": {},
        # 跨轮重置（2026-11-23）：语义是「本轮内不要重复做同一件事」，
        # 必须显式清零，否则会被 checkpointer 持久化下来跨任务累加。
        "tool_call_counts": {},
        "query_counts": {},
        "session_id": session_id,
        "thread_id": thread_id,
        "user_id": "task-runner",
        "mode": record.get("mode") or "agent",
        "ask_rounds": 0,
    }
    config = {"configurable": {"thread_id": thread_id}}

    st.touch_session(thread_id, summary=f"[任务] {record.get('name', '')}",
                     session_id=session_id, user_id="task-runner",
                     mode=record.get("mode") or "agent",
                     model=record.get("model") or "")

    last: Dict[str, Any] = {}
    #: 工具调用（assistant 侧，含参数）
    calls: List[Dict[str, Any]] = []
    #: 工具结果（tool 侧）
    results: List[Dict[str, Any]] = []
    #: SSE 步骤事件序列
    sse_events: List[Dict[str, Any]] = []
    try:
        async for mode, chunk in graph.astream(
            initial, config, stream_mode=["updates", "values"]
        ):
            if mode == "values":
                last = chunk or {}
            elif mode == "updates":
                for evt in _summarize_updates(chunk, thread_id):
                    sse_events.append({
                        "event": evt.get("event", "step"),
                        "node": evt.get("node", ""),
                        "tools": evt.get("tools", []) or [],
                        "stop_reason": evt.get("stop_reason"),
                        "ts": time.time(),
                    })
                # 从 updates 分片里抽取工具调用轨迹（参数 + 结果）
                # 注意：assistant 的 tool_calls 与 tool 结果可能落在**不同分片**
                # （agent 节点产生调用，tools 节点产生结果），因此分开累积、最后配对。
                for _node, delta in (chunk or {}).items():
                    if not isinstance(delta, dict):
                        continue
                    calls.extend(_tool_call_entries(delta))
                    results.extend(_tool_result_entries(delta))
    except Exception as exc:
        record["status"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["finished_at"] = time.time()
        record["duration_ms"] = int((record["finished_at"] - record["started_at"]) * 1000)
        record["tool_trace"] = _merge_tool_trace(calls, results)
        record["sse_events"] = sse_events
        st.record_metrics(error=True)
        return

    payload = _build_response(last, thread_id)
    record["tokens_used"] = payload["tokens_used"]
    record["tool_calls_used"] = payload["tool_calls_used"]
    record["stop_reason"] = payload["stop_reason"]
    record["answer"] = payload["answer"] or ""
    record["result_summary"] = (record["answer"] or "").strip()[:300]
    record["approvals"] = payload["approvals"]
    record["citations"] = list(last.get("citations") or [])
    record["invalid_citations"] = list(last.get("invalid_citations") or [])
    # 工具轨迹：优先用 updates 分片累积的调用/结果；若为空则从最终 messages 回捞
    tool_trace = _merge_tool_trace(calls, results)
    if not tool_trace:
        tool_trace = _extract_tool_trace_from_messages(last.get("messages") or [])
    record["tool_trace"] = tool_trace
    record["sse_events"] = sse_events
    #: 任务的用户/助手消息也落盘（历史会话可查）
    st.record_message(thread_id, role="user",
                      content=record.get("prompt", "") or "")
    st.record_message(thread_id, role="assistant",
                      content=record.get("answer", "") or "",
                      citations=list(last.get("citations") or []),
                      extra={"source": "task", "task_id": task_id,
                             "model": record.get("model", ""),
                             "mode": record.get("mode", "")})
    if payload["status"] == "error":
        record["status"] = "failed"
        record["error"] = record["answer"] or "执行失败"
    else:
        record["status"] = "done"
    record["finished_at"] = time.time()
    record["duration_ms"] = int((record["finished_at"] - record["started_at"]) * 1000)
    st.record_metrics(tokens=payload["tokens_used"], tool_calls=payload["tool_calls_used"])


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------
def _kb_too_large(size: int) -> Any:
    """构造「文件过大」的 KBAdminError。

    Args:
        size: 实际字节数。

    Returns:
        KBAdminError 实例。
    """
    from ..knowledge.admin import MAX_UPLOAD_BYTES, KBAdminError  # noqa: PLC0415

    return KBAdminError(
        "KB_ADMIN_FILE_TOO_LARGE",
        f"文件过大：{size / 1024 / 1024:.1f} MB",
        hint=f"单个文件不得超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MB。",
        status_code=413,
    )


def _reset_shared_retriever() -> None:
    """索引变动后重置共享 Retriever（让下一次检索立刻看到新数据）。

    共享 Retriever 持有自己的 SQLite 连接与 Embedder；索引写入后必须重置，
    否则旧连接看不到新 chunk。
    """
    try:
        from ..knowledge.tools import reset_shared_retriever  # noqa: PLC0415

        reset_shared_retriever()
    except Exception:  # noqa: BLE001
        pass


def _safe_filename(text: Any) -> str:
    """生成安全的 ASCII 文件名（HTTP header 只能放 latin-1）。"""
    raw = str(text or "").strip()
    keep = [ch if (ch.isascii() and (ch.isalnum() or ch in "-_.")) else "_"
            for ch in raw]
    name = "".join(keep)[:60].strip("._-")
    return name or "task"


# ----------------------------------------------------------------------
# 工具契约派生结构（前端「工具详情」用）
# ----------------------------------------------------------------------
def _schema_type_label(sub: Dict[str, Any]) -> str:
    """把 JSON Schema 的 type 字段转成人读的短标签。

    · ``"string"`` → ``string``
    · ``["string","null"]`` → ``string | null``
    · 没有 type 但有 enum → ``enum``
    · 都没有 → ``any``
    """
    t = sub.get("type")
    if isinstance(t, list):
        return " | ".join(str(x) for x in t)
    if t:
        return str(t)
    if sub.get("enum"):
        return "enum"
    if sub.get("properties"):
        return "object"
    if sub.get("items"):
        return "array"
    return "any"


def _schema_params(schema: Any) -> List[Dict[str, Any]]:
    """把 JSON Schema 派生成**参数表**（前端直接渲染成表格）。

    返回项字段：
        ``name`` / ``type`` / ``required`` / ``description`` / ``default``
        ``enum`` / ``minimum`` / ``maximum`` / ``example`` / ``in``

    目的：裸 JSON Schema 对人不够直观，表格（参数名 / 类型 / 必填 / 说明）
    比 JSON 好读得多；但 JSON 本身仍要展示（折叠区），两者互补。
    """
    if not isinstance(schema, dict):
        return []
    props = schema.get("properties") or {}
    required = list(schema.get("required") or [])
    out: List[Dict[str, Any]] = []
    for name, sub in props.items():
        if not isinstance(sub, dict):
            sub = {}
        item: Dict[str, Any] = {
            "name": name,
            "type": _schema_type_label(sub),
            "required": name in required,
            "description": str(sub.get("description") or ""),
            "default": sub.get("default", None),
            "enum": list(sub.get("enum") or []),
            "minimum": sub.get("minimum", None),
            "maximum": sub.get("maximum", None),
            "in": "required" if name in required else "optional",
        }
        ex = sub.get("examples")
        if isinstance(ex, list) and ex:
            item["example"] = ex[0]
        else:
            item["example"] = None
        out.append(item)
    return out


# ----------------------------------------------------------------------
# 决策轨迹（Decision Trace）数据构造
# ----------------------------------------------------------------------
#: 轨迹里「动作摘要」的最大字符数（超过则截断并标注）
TRACE_SUMMARY_MAX = 220

#: **上下文窗口上限（token）** —— 2026-11-22 新增（可观测性）。
#:
#: 本机两个 llama-server 都以 ``--parallel 1 -c 32768`` 启动
#: （见 ``deploy/start_servers.ps1`` 的 ``$CtxSize`` 默认值 32768），
#: 即单会话上下文 = 32768 token。上下文占用 treemap 用它做分母。
#:
#: ★ **为什么从 8192 提到 32768（2026-11-23）**：
#:   8192 窗口是「复杂代码任务」真正撞墙的原因 —— 改一个 200 行的类
#:   （连同 system prompt + 工具 schema + 若干工具结果）就超窗，
#:   llama-server 直接返回 400 ``request exceeds the available context size``。
#:   而 gpt-oss-20b 用的是 **GQA + q8_0 KV 量化**，
#:   KV cache 只有 24 KiB/token，32K 上下文仅需 ~0.75 GB 显存，
#:   **扩窗口的边际成本极低**，收益却很大。
#:
#: **诚实说明**：这是**配置上限**，不是实时探测值。
#: 若将来改用别的 ``-c`` 参数，请同步改这里（或改为从端点探测）。
CONTEXT_WINDOW_TOKENS = int(os.environ.get("LLM_CONTEXT_WINDOW", "32768") or 32768)

#: **默认 ReAct 轮次上限**（2026-11-24 新增，此前硬编码 8）。
#:
#: 为什么提到 20：
#:   8 轮在真实任务里远远不够 —— 检索 2~3 轮、工具调用 2~3 轮、
#:   汇总成最终答案 1~2 轮，轻松就用完了。配合「轮次双倍消耗」的修复
#:   （原先 agent 与 tools 各算一次，实际只有 4 轮可用），
#:   用户反馈「才几步就提示预算用尽」。
#:
#: ★ **轮次语义（2026-11-24 修正）**：**一次 LLM 调用 = 一轮**。
#:   轮次只在 agent 节点计费与判定（见 ``core/budget.py:check_no_rounds``）。
#:
#: 可用环境变量 ``AGENT_MAX_ROUNDS`` 覆盖。
DEFAULT_MAX_ROUNDS = int(os.environ.get("AGENT_MAX_ROUNDS", "20") or 20)

#: **system prompt 的真实字符数**（2026-11-22 新增，可观测性）。
#: 「上下文占用」treemap 需要区分 system prompt / 工具 schema / 工具结果 /
#: 对话轮次 / 文件引用 五个来源。前端无法拿到 prompt 原文，
#: 因此这里在后端直接算出长度下发 —— 至少这一块是**实测**而非盲估。
#: 用 ``try/except`` 兜底：prompt 组装失败时留 0，前端会标成「未测量」。
try:
    _SYSTEM_PROMPT_CHARS = len(get_system_prompt() or "")
except Exception:  # pragma: no cover —— 防御性：prompt 组装不该拖垮接口
    _SYSTEM_PROMPT_CHARS = 0


def _truncate_text(text: Any, limit: int = 400) -> str:
    """截断文本并标注被截断（用于轨迹里超长结果的预览）。"""
    s = "" if text is None else str(text)
    if len(s) <= limit:
        return s
    return s[:limit] + f"…（共 {len(s)} 字符）"


def _trace_action_summary(name: str, args: Any) -> str:
    """生成「这一步在做什么」的**可解释动作摘要**。

    ⚠️ **合规要求**：这里**不展示**模型的原始思维链（chain-of-thought），
    只根据「工具名 + 入参」生成一句人类可读的说明。
    例如：``kb_search(query="T101_VDD_FUNCT")`` →
    「调用 kb_search 以查证 T101_VDD_FUNCT」。
    """
    tool = str(name or "")
    if not tool:
        return "（未记录工具名）"
    arg_map = args if isinstance(args, dict) else {}
    # 常见的「主参数」名，按优先级取第一个非空值
    for key in ("query", "path", "file", "name", "id", "url", "code",
                "keyword", "text", "content"):
        val = arg_map.get(key)
        if val is None or val == "":
            continue
        short = _truncate_text(val, 80)
        return f"调用 {tool} 以处理「{short}」"
    if arg_map:
        first_key = next(iter(arg_map))
        short = _truncate_text(arg_map[first_key], 60)
        return f"调用 {tool}（{first_key}={short}）"
    return f"调用 {tool}"


def _trace_reasoning_summary(reasoning: Any) -> str:
    """把模型返回的 reasoning 压成**前 1-2 句**的简短摘要。

    ⚠️ 这是「可解释的动作摘要」而非完整思维链：
    · 只取前 2 句
    · 长度硬截断（``TRACE_SUMMARY_MAX``）
    · 前端必须标注「模型推理摘要（截断）」

    若模型未返回 reasoning（``None`` / 空串），返回空串 —— 前端据此显示
    「本轮模型未返回可展示的推理摘要」，而不是伪造一个。
    """
    text = str(reasoning or "").strip()
    if not text:
        return ""
    # 按句号/换行切分，取前 2 句
    parts: List[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in "。.!！\n":
            s = buf.strip()
            if s:
                parts.append(s)
            buf = ""
            if len(parts) >= 2:
                break
    if not parts and buf.strip():
        parts.append(buf.strip())
    summary = " ".join(parts).strip()
    if len(summary) > TRACE_SUMMARY_MAX:
        summary = summary[:TRACE_SUMMARY_MAX] + "…"
    return summary


def _trace_error_of(result: Any) -> Dict[str, Any]:
    """从工具结果里抽出错误信息（code / message / retryable / hint）。"""
    if not isinstance(result, dict):
        return {}
    code = str(result.get("code") or "")
    if not code:
        return {}
    return {
        "code": code,
        "message": str(result.get("message") or ""),
        "retryable": bool(result.get("retryable", False)),
        "hint": str(result.get("hint") or ""),
    }


def _build_trace_entry(seq: int, call: Dict[str, Any],
                       result_entry: Optional[Dict[str, Any]],
                       *, reasoning: str = "",
                       duration_ms: Optional[int] = None,
                       round_index: int = 1,
                       started_at: Optional[float] = None,
                       tokens: Optional[int] = None,
                       pending: bool = False) -> Dict[str, Any]:
    """构造**单步决策轨迹**条目。

    字段：
        ``seq`` / ``round`` / ``name`` / ``args`` / ``result`` / ``result_summary``
        ``ok`` / ``error`` / ``duration_ms`` / ``reasoning_summary``
        ``action_summary`` / ``ts`` / ``status``

    **``id``（2026-11-22 新增）**：透传 tool_call_id。
    前端需要它做去重主键 —— 同一次调用会由两个 SSE 通道各推一次
    （``step.tool_calls`` 只有参数、``trace`` 快照有参数+结果），
    没有共同主键就会把一次调用渲染成两行。

    **``started_at`` / ``ended_at``（2026-11-22 新增，可观测性）**：
    轨迹条形（Trace Waterfall）需要**按真实开始/结束时间定位**，
    而不是等宽列表。``started_at`` = 该次调用被观测到的时刻（Unix 秒），
    ``ended_at`` = 结果回填完成的时刻；两者之差即该步的墙钟跨度。
    **不伪造**：拿不到就留 None，前端显示「–」而不是猜测值。

    **``tokens``（2026-11-22 新增，可观测性）**：该步消耗的 token 数
    （若上层能归集到）。拿不到留 None。

    **``kind``（2026-11-22 新增，可观测性）**：步骤类型
    （``tool`` 工具调用 / ``llm`` 模型调用 / ``approval`` 审批等待），
    前端据此给条形着不同语义色。

    **三态 ``ok`` / ``status``（2026-11-24 修复：待审批被渲染成绿色 ✓）**
    ---------------------------------------------------------------
    真实故障：用户看到 ``ws_write_file`` 标着「✓ 成功」，但**文件根本没落地** ——
    因为该工具被**挂在 HITL 审批上，还没执行**。

    根因是这里的判定：``ok = not err and not _is_tool_failure(body)``。
    当 ``result_entry is None``（只有调用、没有结果）时：
    · ``err`` 是空 dict → ``not err`` 为 True；
    · ``body`` 是 None → ``_is_tool_failure(None)`` 返回 False（它对非 dict 直接返回 False）。

    两个「不是失败」相乘 → ``ok = True`` → **未执行被当成成功**。

    现在改为**三态**：
        · ``ok = True``  —— 已执行且成功
        · ``ok = False`` —— 已执行但失败
        · ``ok = None``  —— **已发起，等待人工审批**（尚未执行）

    并额外给出 ``status`` 字段（``"success"`` / ``"error"`` / ``"pending_approval"``），
    让前端不必靠 ``ok`` 的三态去猜。**老前端按 ``ok !== false`` 判定**，
    对 ``None`` 会当成「成功」—— 因此前端已同步改为识别第三态。
    """
    name = str(call.get("name") or "")
    args = call.get("args")
    if not isinstance(args, dict):
        args = {}
    body = result_entry.get("result") if result_entry else None
    err = _trace_error_of(body)
    #: **待审批判定**：只有「工具调用已发起 + 还没有结果」才是 pending。
    #: ⚠️ 不能只看「没有结果」—— 那也可能是「已执行但结果还没回填」。
    #: 因此这里依赖调用方传入的 ``pending`` 标记（它来自本轮
    #: ``stop_reason == "pending_approval"`` / ``pending_hitl`` 的真实信号）。
    is_pending = bool(pending) and result_entry is None and not err
    if is_pending:
        #: ``ok`` 必须是 **None**（不是 False）：既没成功也没失败，
        #: 只是**还没执行**。前端据此渲染琥珀色「待审批」。
        ok: Optional[bool] = None
        status = "pending_approval"
    else:
        ok = not err and not _is_tool_failure(body)
        status = "success" if ok else "error"
    ts_now = time.time()
    started = started_at if started_at is not None else ts_now
    dur_ms = duration_ms
    ended = started + (dur_ms / 1000.0) if dur_ms is not None else None
    return {
        "seq": seq,
        "round": int(round_index or 1),
        "id": str(call.get("id") or ""),
        "name": name,
        "args": args,
        "result": body,
        "result_summary": _truncate_text(body, 600) if body is not None else "",
        "ok": ok,
        "status": status,
        "error": err,
        "duration_ms": duration_ms,
        "reasoning_summary": reasoning or "",
        "action_summary": _trace_action_summary(name, args),
        "ts": ts_now,
        # ---- 可观测性新增字段（向后兼容：老客户端按字段名读取不受影响）----
        "kind": "tool",
        "started_at": started,
        "ended_at": ended,
        "tokens": tokens,
    }


def _build_llm_trace_entry(seq: int, *, started_at: Optional[float],
                           ended_at: Optional[float],
                           duration_ms: Optional[int],
                           tokens: Optional[int],
                           reasoning: str = "",
                           tool_names: Optional[List[str]] = None,
                           model: str = "") -> Dict[str, Any]:
    """构造**一轮 LLM 调用**的轨迹条目（kind="llm"）。

    **为什么需要**：Trace Waterfall 的时间轴必须覆盖「模型思考」这段 ——
    在实测里模型推理往往是总耗时的**大头**（工具只占几十毫秒）。
    只记工具调用会让时间轴失真：条形全部挤在毫秒级，看不出时间花在哪。

    **不伪造**：``started_at`` / ``ended_at`` / ``duration_ms`` / ``tokens``
    全部来自 graph 层用单调时钟实测的值，拿不到就留 None，
    前端显示「–」而不是猜测。

    这是**附加条目**，不占用工具调用预算，也不影响既有 trace 语义
    （老前端按 ``kind`` 缺省当 "tool" 处理即可）。
    """
    ts_now = time.time()
    started = float(started_at) if started_at is not None else ts_now
    ended = float(ended_at) if ended_at is not None else None
    dur = int(duration_ms) if duration_ms is not None else None
    names = list(tool_names or [])
    if names:
        summary = "模型决策 · 请求 " + "、".join(names)
    else:
        summary = "模型推理 · 生成最终回答"
    return {
        "seq": seq,
        "round": seq,
        "id": "",
        "name": "LLM 调用",
        "args": {},
        "result": None,
        "result_summary": summary,
        "ok": True,
        "error": {},
        "duration_ms": dur,
        "reasoning_summary": reasoning or "",
        "action_summary": summary,
        "ts": started,
        "kind": "llm",
        "started_at": started,
        "ended_at": ended,
        "tokens": tokens,
        "model": model or "",
    }


def _tool_duration_ms(call: Dict[str, Any]) -> Optional[int]:
    """从调用条目里取耗时（毫秒）；没有则 None。"""
    for key in ("duration_ms", "elapsed_ms", "latency_ms"):
        val = call.get(key)
        if val is None:
            continue
        try:
            return int(val)
        except (TypeError, ValueError):
            continue
    return None


def _url_quote(text: Any) -> str:
    """URL 编码（用于 RFC 5987 的 filename* 字段，支持中文）。"""
    from urllib.parse import quote

    return quote(str(text or ""), safe="")


def _task_summary(record: Dict[str, Any]) -> Dict[str, Any]:
    """任务列表项（摘要视图，不含完整答案与轨迹）。"""
    return {
        "id": record.get("task_id", ""),
        "task_id": record.get("task_id", ""),
        "name": record.get("name", ""),
        "description": record.get("description", ""),
        "prompt": record.get("prompt", ""),
        "model": record.get("model", ""),
        "mode": record.get("mode", "") or "agent",
        "params": record.get("params", {}) or {},
        "status": record.get("status", ""),
        "created_at": record.get("created_at"),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "duration_ms": record.get("duration_ms"),
        "tokens_used": record.get("tokens_used", 0),
        "tool_calls_used": record.get("tool_calls_used", 0),
        "error": record.get("error", ""),
        "result_summary": record.get("result_summary", ""),
        "stop_reason": record.get("stop_reason"),
        "thread_id": record.get("thread_id", ""),
        "session_id": record.get("session_id", ""),
    }


def _truncate_text(text: Any, limit: int = 400) -> str:
    """把任意值转成截断后的字符串（用于结果摘要）。"""
    try:
        if isinstance(text, str):
            s = text
        else:
            s = json.dumps(text, ensure_ascii=False, default=str)
    except Exception:
        s = str(text)
    s = s.strip()
    return s[:limit]


def _tool_call_entries(delta: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 messages 里抽出 assistant 的 tool_calls（参数，无结果）。

    兼容两种形态：
        · OpenAI 风格：``{"id","function":{"name","arguments"}}``
        · LangChain 风格（``_msg_to_dict`` 归一后）：``{"name","args"}``
    """
    out: List[Dict[str, Any]] = []
    for msg in delta.get("messages", []) or []:
        d = msg if isinstance(msg, dict) else _msg_to_dict(msg)
        if d.get("role") != "assistant":
            continue
        for tc in d.get("tool_calls") or []:
            fn = tc.get("function", {}) or {}
            name = tc.get("name") or fn.get("name") or ""
            raw_args = tc.get("args")
            if raw_args is None:
                raw_args = fn.get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except Exception:
                args = {"_raw": raw_args}
            out.append({"name": name, "args": args})
    return out


def _tool_result_entries(delta: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 messages 里抽出 tool 角色的结果（工具名 + 结果体）。

    **``tool_call_id``（2026-11-22 新增）**：从 ``ToolMessage.tool_call_id``
    透传出来，供前端把「调用」与「结果」用共同主键配对、去重。
    """
    out: List[Dict[str, Any]] = []
    for msg in delta.get("messages", []) or []:
        d = msg if isinstance(msg, dict) else _msg_to_dict(msg)
        if d.get("role") != "tool":
            continue
        content = d.get("content", "")
        try:
            result = json.loads(content) if isinstance(content, str) else content
        except Exception:
            result = content
        out.append({
            "name": d.get("name", "") or "",
            "result": result,
            "tool_call_id": str(d.get("tool_call_id", "") or ""),
            # **耗时（2026-11-22 新增）**：graph 层测得的执行耗时（毫秒）。
            # 没有就留 None —— 前端显示「–」，**不伪造耗时数字**。
            "duration_ms": d.get("duration_ms"),
        })
    return out


def _is_tool_failure(result: Any) -> bool:
    """判断工具结果是否为失败（错误码前缀匹配）。

    ⚠️ **注意 ``None`` 的语义（2026-11-24）**：
    ``None`` 表示「**还没有结果**」（调用已发起，结果还没回填，
    或者工具正**挂在 HITL 审批上尚未执行**）。
    这里对非 dict 一律返回 False 是**正确的**——「没有结果」不是「失败」；
    但它**绝不能**被当成「成功」。所以调用方（``_build_trace_entry``）
    必须把「无结果」与「成功」区分开：前者走 ``ok = None`` 的**待审批**分支。

    本函数只回答「**有没有拿到一个明确的失败结果**」。
    """
    if not isinstance(result, dict):
        return False
    code = str(result.get("code", ""))
    return code.startswith((
        "TOOL_", "INVALID_", "SQL_", "REPEATED_", "REJECTED_", "CUSTOM_",
        #: **单工具单轮上限（2026-11-24 新增）**：超出单轮上限被丢弃的调用
        #: 也算「本次调用没有生效」—— 前端应显示为失败而不是成功。
        "PER_ROUND_",
    ))


def _merge_tool_trace(calls: List[Dict[str, Any]], results: List[Dict[str, Any]],
                      start_seq: int = 1) -> List[Dict[str, Any]]:
    """把「调用」与「结果」按**发生顺序**配对成工具调用轨迹。

    工具在图里是**顺序执行**的，因此第 n 次调用对应第 n 个结果；
    若数量不等（异常中断），多出来的部分单独成条。
    """
    out: List[Dict[str, Any]] = []
    total = max(len(calls), len(results))
    for i in range(total):
        call = calls[i] if i < len(calls) else {"name": "", "args": {}}
        res = results[i] if i < len(results) else None
        result_body = res["result"] if res else None
        #: **三态 ok**（2026-11-24）：``None`` 表示「调用已发起、结果还没有」
        #: （例如被 HITL 挂起尚未执行）—— 既不是成功也不是失败。
        #: 此前这里算出 ``ok=True`` → 前端渲染成绿色 ✓「成功」，
        #: 用户以为文件已写好，实际根本没落地。
        if result_body is None and res is None and i < len(calls):
            ok: Optional[bool] = None
            status = "pending_approval"
        else:
            ok = not _is_tool_failure(result_body)
            status = "success" if ok else "error"
        out.append({
            "seq": start_seq + i,
            "name": call.get("name") or (res or {}).get("name", ""),
            "args": call.get("args", {}) or {},
            "result_summary": _truncate_text(result_body) if result_body is not None else "",
            "result": result_body,
            "ok": ok,
            "status": status,
            "ts": time.time(),
            "phase": "call",
        })
    return out


def _extract_tool_trace(chunk: Any, start_seq: int = 1) -> List[Dict[str, Any]]:
    """从 astream 的 ``updates`` 分片里抽取工具调用轨迹（单分片内配对）。

    轨迹项：``{seq,name,args,result_summary,result,ok,ts}``
    （``result`` 为完整结果，``result_summary`` 为截断预览）。
    """
    out: List[Dict[str, Any]] = []
    if not isinstance(chunk, dict):
        return out
    for _node, delta in chunk.items():
        if not isinstance(delta, dict):
            continue
        calls = _tool_call_entries(delta)
        results = _tool_result_entries(delta)
        if not calls and not results:
            continue
        out.extend(_merge_tool_trace(calls, results, start_seq + len(out)))
    return out


def _extract_tool_trace_from_messages(messages: Any, start_seq: int = 1
                                       ) -> List[Dict[str, Any]]:
    """从最终 messages 里回捞工具调用轨迹（updates 分片缺失时的兜底）。"""
    out: List[Dict[str, Any]] = []
    if not isinstance(messages, list):
        return out
    calls: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []
    for msg in messages:
        d = msg if isinstance(msg, dict) else _msg_to_dict(msg)
        role = d.get("role", "")
        if role == "assistant":
            for tc in d.get("tool_calls") or []:
                fn = tc.get("function", {}) or {}
                name = tc.get("name") or fn.get("name") or ""
                raw_args = tc.get("args")
                if raw_args is None:
                    raw_args = fn.get("arguments", "{}")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except Exception:
                    args = {"_raw": raw_args}
                calls.append({"name": name, "args": args})
        elif role == "tool":
            content = d.get("content", "")
            try:
                result = json.loads(content) if isinstance(content, str) else content
            except Exception:
                result = content
            results.append({"name": d.get("name", "") or "", "result": result})
    out = _merge_tool_trace(calls, results, start_seq)
    return out


def _task_to_markdown(record: Dict[str, Any]) -> str:
    """把任务完整结果渲染为 Markdown（导出用）。"""
    lines: List[str] = []
    lines.append(f"# 任务结果：{record.get('name') or record.get('task_id') or ''}")
    lines.append("")
    lines.append("## 基本信息")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("| --- | --- |")
    for label, key in (("任务 ID", "task_id"), ("状态", "status"), ("模型", "model"),
                       ("耗时 (ms)", "duration_ms"), ("token", "tokens_used"),
                       ("工具调用", "tool_calls_used"), ("终止原因", "stop_reason"),
                       ("thread_id", "thread_id"), ("session_id", "session_id")):
        lines.append(f"| {label} | {record.get(key, '')} |")
    lines.append("")
    lines.append("## 参数")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(record.get("params") or {}, ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("## Prompt")
    lines.append("")
    lines.append(str(record.get("prompt") or ""))
    lines.append("")
    lines.append("## 最终答案")
    lines.append("")
    lines.append(str(record.get("answer") or "（无）"))
    lines.append("")
    trace = record.get("tool_trace") or []
    lines.append(f"## 工具调用轨迹（{len(trace)} 次）")
    lines.append("")
    if not trace:
        lines.append("（无工具调用）")
    else:
        for item in trace:
            status = "✅" if item.get("ok") else "❌"
            lines.append(f"### {status} {item.get('seq')}. `{item.get('name')}`")
            lines.append("")
            lines.append("**参数**")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(item.get("args") or {}, ensure_ascii=False,
                                    indent=2, default=str))
            lines.append("```")
            lines.append("")
            lines.append("**结果**")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(item.get("result"), ensure_ascii=False,
                                    indent=2, default=str))
            lines.append("```")
            lines.append("")
    if record.get("error"):
        lines.append("## 错误详情")
        lines.append("")
        lines.append("```")
        lines.append(str(record["error"]))
        lines.append("```")
        lines.append("")
    sse = record.get("sse_events") or []
    lines.append(f"## SSE 事件序列（{len(sse)} 条）")
    lines.append("")
    if sse:
        lines.append("| # | 事件 | 节点 | 工具 | 终止原因 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for i, evt in enumerate(sse, 1):
            lines.append(
                f"| {i} | {evt.get('event', '')} | {evt.get('node', '')} | "
                f"{', '.join(evt.get('tools') or [])} | {evt.get('stop_reason') or ''} |")
    else:
        lines.append("（无）")
    lines.append("")
    return "\n".join(lines)


def _build_response(result: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    """把图执行结果转成 API 响应。"""
    result = result or {}
    pending = result.get("pending_hitl") or []
    status = "done"
    if pending:
        status = "pending_approval"
    stop_reason = result.get("stop_reason")
    if stop_reason and stop_reason != "done" and not pending:
        status = "done" if result.get("final_answer") else "error"
    answer = result.get("final_answer")
    # ------------------------------------------------------------------
    # **待审批时的最终答案（2026-11-24 修复）**
    # ------------------------------------------------------------------
    # 真实故障：``ws_write_file`` 被 HITL 挂起时，``final_answer`` 是**空串**，
    # 前端于是显示一片空白 —— 用户既看不到「发生了什么」，也不知道
    # **文件其实根本没写**（这正是「代码都写不进去」的错觉来源：
    # 轨迹上显示绿色 ✓「成功」，但磁盘上什么都没有）。
    #
    # 现在明确告知：① 文件尚未写入；② 必须点击审批卡上的「批准」按钮；
    # ③ **对话文字不会自动放行**（避免用户以为「再回一句话就行」）。
    if pending:
        tool_names = "、".join(
            str((ap or {}).get("tool_name") or "") for ap in pending
            if (ap or {}).get("tool_name"))
        write_tools = [ap for ap in pending
                       if str((ap or {}).get("tool_name") or "").startswith("ws_")]
        if write_tools:
            note = (
                "⚠️ **文件尚未写入。**\n\n"
                f"以下写操作已发起，但正**挂在人工审批上、尚未执行**：{tool_names or '写工具'}。\n\n"
                "请在下方审批卡片上点击「**批准**」后才会真正落盘；"
                "**在对话里回复「确认执行」不会自动放行**。"
            )
        else:
            note = (
                "⚠️ **操作尚未执行。**\n\n"
                f"以下工具调用已发起，正在等待人工审批：{tool_names or '待审批工具'}。\n\n"
                "请在下方审批卡片上点击「**批准**」后才会真正执行；"
                "**在对话里回复「确认执行」不会自动放行**。"
            )
        answer = f"{answer}\n\n{note}".strip() if answer else note
    payload = {
        "status": status,
        "answer": answer,
        "thread_id": thread_id,
        "approvals": pending,
        "tokens_used": int(result.get("tokens_used", 0) or 0),
        "tool_calls_used": int(result.get("tool_calls_used", 0) or 0),
        #: **真实轮次**（2026-11-24 新增）：由 agent 节点回写 state。
        "rounds": int(result.get("rounds", 0) or 0),
        "stop_reason": stop_reason,
    }
    # ---- 检索必要性判定（新增字段，向后兼容：老客户端按字段名读取不受影响）----
    if "retrieval_needed" in result:
        needed = bool(result.get("retrieval_needed"))
        payload["retrieval_needed"] = needed
        payload["retrieval_rule"] = str(result.get("retrieval_rule", "") or "")
        payload["retrieval_reason"] = str(result.get("retrieval_reason", "") or "")
        payload["retrieval_policy_version"] = str(
            result.get("retrieval_policy_version", "") or "")
        #: 前端直接可用的中文标记（零 emoji，纯文字）
        payload["retrieval_label"] = "检索: 已执行" if needed else "检索: 已跳过"
    return payload


# ----------------------------------------------------------------------
# 审批续跑的轨迹构建（2026-11-24 新增）
# ----------------------------------------------------------------------
def _build_trace_from_state(result: Dict[str, Any], *,
                             model: str = "") -> List[Dict[str, Any]]:
    """从**图最终状态**构建决策轨迹条目（供 ``/approve`` 使用）。

    为什么需要（2026-11-24 修复）
    ----------------------------
    ``/chat`` 走的是 ``_run_graph_stream``（SSE），轨迹是**边跑边攒**的；
    而 ``/approve`` 走的是 ``graph.invoke()``（一次性拿最终状态），
    于是 ``_build_response`` 里**根本没有 trace** →
    审批完成后「决策轨迹」面板**拿不到新快照**，
    用户看到的仍是审批前那一帧（ws_write_file 标着琥珀色「待审批」）。

    这里从最终 state 的 ``messages`` 里重建轨迹：
      · assistant 的 ``tool_calls`` → 「调用」条目（先登记，只有参数）
      · tool 角色的结果          → 回填到对应调用条目上
      · 还在 ``approvals`` 里、但**没有**结果的调用 → ``ok=None``（待审批）

    **三态 ``ok`` 与 ``/chat`` 完全一致**（True / False / None）：
    被批准并**真的执行成功**的工具必须是 ``ok: true``（它确实成功了），
    绝不能因为「结果是 None」就误判成待审批。

    Args:
        result: 图执行完的最终状态。
        model: 模型名（写进 LLM 条目，可为空）。

    Returns:
        轨迹条目列表（结构与 ``/chat`` 的 trace 一致）。
    """
    result = result or {}
    trace: List[Dict[str, Any]] = []
    messages = result.get("messages") or []
    approvals = result.get("approvals") or {}

    #: tool_call_id → 轨迹条目（用于回填结果）
    by_id: Dict[str, Dict[str, Any]] = {}
    #: 已收到结果的 tool_call_id 集合
    got_result: set = set()

    for msg in messages:
        d = msg if isinstance(msg, dict) else _msg_to_dict(msg)
        role = d.get("role")
        if role == "assistant":
            for tc in (d.get("tool_calls") or []):
                fn = tc.get("function", {}) or {}
                name = fn.get("name") or tc.get("name") or ""
                cid = str(tc.get("id") or "")
                raw_args = tc.get("args")
                if raw_args is None:
                    raw_args = fn.get("arguments", "{}")
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args)
                    except Exception:
                        args = {"_raw": raw_args}
                elif isinstance(raw_args, dict):
                    args = raw_args
                else:
                    args = {}
                entry = _build_trace_entry(
                    len(trace) + 1,
                    {"id": cid, "name": name, "args": args},
                    None,
                    round_index=0,
                    started_at=time.time(),
                )
                trace.append(entry)
                if cid:
                    by_id[cid] = entry
        elif role == "tool":
            cid = str(d.get("tool_call_id") or "")
            name = str(d.get("name") or "")
            content = d.get("content", "")
            try:
                body = json.loads(content) if isinstance(content, str) else content
            except Exception:
                body = content
            res_entry = {"result": body, "name": name,
                         "tool_call_id": cid,
                         "duration_ms": d.get("duration_ms")}
            target = by_id.get(cid) if cid else None
            if target is None:
                target = next(
                    (e for e in reversed(trace)
                     if e.get("result") is None and e.get("name") == name),
                    None,
                )
            if target is None:
                entry = _build_trace_entry(
                    len(trace) + 1,
                    {"id": cid, "name": name, "args": {}},
                    res_entry,
                    round_index=0,
                    started_at=time.time(),
                )
                trace.append(entry)
                by_id[cid] = entry
                target = entry
            body = res_entry.get("result")
            err = _trace_error_of(body)
            target["result"] = body
            target["result_summary"] = (_truncate_text(body, 600)
                                        if body is not None else "")
            #: **三态 ok**（与 ``/chat`` 一致）：
            #: · body is None → None（**尚未执行**，待审批）
            #: · 有结果       → True / False（已执行）
            #: ⚠️ 被批准的写工具**真的执行成功**时 body 是完整 JSON →
            #:   ok 必须是 **True**，绝不能是 None。
            if body is None and not err:
                target["ok"] = None
                target["status"] = "pending_approval"
            else:
                target["ok"] = not err and not _is_tool_failure(body)
                target["status"] = "success" if target["ok"] else "error"
            target["error"] = err
            dur = d.get("duration_ms")
            if dur is not None:
                target["duration_ms"] = dur
                target["ended_at"] = (target.get("started_at") or time.time()) + dur / 1000.0
            got_result.add(cid)

    #: 仍**只挂着审批、没有任何结果**的调用 → 明确标成「待审批」
    for cid, entry in by_id.items():
        if cid in got_result:
            continue
        decision = approvals.get(cid) or {}
        if decision.get("approved"):
            #: 已批准但本轮 state 里没拿到结果 → 仍按"待审批/未执行"处理，
            #: **不能**谎称成功（这正是此前绿色 ✓ 的来源）。
            entry["ok"] = None
            entry["status"] = "pending_approval"
            entry["action_summary"] = (
                "等待人工确认（尚未执行）：" + str(entry.get("name") or ""))

    #: 追加「审批决策」条目（与 ``/chat`` 的 kind="approval" 同构）
    for cid, decision in (approvals or {}).items():
        if not isinstance(decision, dict):
            continue
        approved = bool(decision.get("approved"))
        entry = {
            "seq": len(trace) + 1,
            "round": 0,
            "id": str(cid or ""),
            "name": "审批",
            "args": {},
            "result": None,
            "result_summary": "",
            #: 审批**本身**没有 ok 三态可言：已批准 → True，已拒绝 → False
            "ok": True if approved else False,
            "status": "success" if approved else "error",
            "error": {} if approved else {"code": "REJECTED_BY_HUMAN",
                                         "message": "人工审批拒绝了该调用"},
            "duration_ms": None,
            "reasoning_summary": "",
            "action_summary": ("人工审批：" + ("已批准" if approved else "已拒绝")),
            "ts": time.time(),
            "kind": "approval",
            "started_at": time.time(),
            "ended_at": None,
            "tokens": None,
            "permission": "",
            "impact": "",
            "reversible": True,
        }
        trace.append(entry)

    return trace


def _sse_line(payload: Dict[str, Any]) -> str:
    """构造一行 SSE：``data: {json}``。"""
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


def _summarize_updates(chunk: Any, thread_id: str) -> List[Dict[str, Any]]:
    """把 astream 的 updates 分片压成前端友好的 step/approval 事件。"""
    out: List[Dict[str, Any]] = []
    if not isinstance(chunk, dict):
        return out
    for node, delta in chunk.items():
        if node == "__interrupt__":
            payload = None
            try:
                payload = delta[0].value  # type: ignore[index]
            except Exception:
                payload = None
            if isinstance(payload, dict):
                out.append({
                    "event": "approval",
                    "thread_id": thread_id,
                    "approvals": [{
                        "tool_call_id": payload.get("tool_call_id", ""),
                        "tool_name": payload.get("tool_name", ""),
                        "permission": payload.get("permission", ""),
                        "description": payload.get("description", ""),
                        "impact": payload.get("impact", ""),
                        "reversible": payload.get("reversible", True),
                        "args": payload.get("args", {}) or {},
                        # **向后兼容**：diff 仅工作区写工具存在，其余为 None
                        "diff": payload.get("diff")
                        or (payload.get("metadata") or {}).get("diff"),
                        "metadata": payload.get("metadata", {}) or {},
                    }],
                })
            continue
        if not isinstance(delta, dict):
            continue
        evt: Dict[str, Any] = {
            "event": "step",
            "thread_id": thread_id,
            "node": node,
            "tokens_used": int(delta.get("tokens_used", 0) or 0),
            "tool_calls_used": int(delta.get("tool_calls_used", 0) or 0),
            # **可观测性（2026-11-22 新增）**：本 step 的真实发生时刻（Unix 秒）。
            # 轨迹瀑布按它定位条形；老客户端按字段名读取，不受影响。
            "ts": time.time(),
        }
        # **LLM 轮次耗时（2026-11-22 新增，可观测性）**：
        # agent 节点 delta 里带 llm_duration_ms / llm_started_at / llm_ended_at
        # （由 graph 层用单调时钟实测）。瀑布图据此画出「LLM 调用」这一步。
        if delta.get("llm_duration_ms") is not None:
            evt["llm_duration_ms"] = int(delta.get("llm_duration_ms") or 0)
        if delta.get("llm_started_at") is not None:
            evt["llm_started_at"] = float(delta.get("llm_started_at") or 0)
        if delta.get("llm_ended_at") is not None:
            evt["llm_ended_at"] = float(delta.get("llm_ended_at") or 0)
        # 检索必要性判定（前端可据此显示"本轮未走检索"标记）
        if "retrieval_needed" in delta:
            needed = bool(delta.get("retrieval_needed"))
            evt["retrieval_needed"] = needed
            evt["retrieval_rule"] = str(delta.get("retrieval_rule", "") or "")
            evt["retrieval_reason"] = str(delta.get("retrieval_reason", "") or "")
            evt["retrieval_label"] = "检索: 已执行" if needed else "检索: 已跳过"
        # agent 节点：把模型请求的工具名带出来
        tool_names: List[str] = []
        #: **结构化工具调用**（含参数）：决策轨迹的「决策 + 动作」两段
        tool_calls: List[Dict[str, Any]] = []
        #: **模型推理摘要**（截断后的前 1-2 句，非原始思维链）
        reasoning_summary = ""
        for m in delta.get("messages", []) or []:
            d = m if isinstance(m, dict) else _msg_to_dict(m)
            for tc in (d.get("tool_calls") or []):
                fn = tc.get("function", {}) or {}
                name = fn.get("name") or tc.get("name") or ""
                if name:
                    tool_names.append(name)
                raw_args = tc.get("args")
                if raw_args is None:
                    raw_args = fn.get("arguments", "{}")
                if isinstance(raw_args, str):
                    try:
                        parsed_args = json.loads(raw_args)
                    except Exception:
                        parsed_args = {"_raw": raw_args}
                else:
                    parsed_args = raw_args or {}
                if not isinstance(parsed_args, dict):
                    parsed_args = {"value": parsed_args}
                tool_calls.append({
                    "id": tc.get("id", "") or "",
                    "name": name,
                    "args": parsed_args,
                    "action_summary": _trace_action_summary(name, parsed_args),
                })
            # reasoning_content（harmony 格式）：只取**前 1-2 句**摘要
            if not reasoning_summary:
                raw_reason = d.get("reasoning_content") or d.get("reasoning") or ""
                reasoning_summary = _trace_reasoning_summary(raw_reason)
        if tool_names:
            evt["tools"] = tool_names
        if tool_calls:
            evt["tool_calls"] = tool_calls
        if reasoning_summary:
            evt["reasoning_summary"] = reasoning_summary
        # 工具结果（tools 节点产出）：把结果挂到本 step 上，供前端渲染
        tool_results = _tool_result_entries(delta)
        if tool_results:
            evt["tool_results"] = tool_results
        pending = delta.get("pending_hitl") or []
        if pending:
            evt["event"] = "approval"
            evt["approvals"] = pending
        if delta.get("stop_reason"):
            evt["stop_reason"] = delta.get("stop_reason")
        out.append(evt)
    return out


def _normalize_role(raw: Any) -> str:
    """把 LangChain 的角色名归一成 OpenAI 风格。

    LangChain：``AIMessage.type == "ai"``、``HumanMessage.type == "human"``、
    ``ToolMessage.type == "tool"``；OpenAI：``assistant`` / ``user`` / ``tool``。
    """
    text = str(raw or "").strip()
    mapping = {
        "ai": "assistant",
        "human": "user",
        "AIMessage": "assistant",
        "HumanMessage": "user",
        "SystemMessage": "system",
        "ToolMessage": "tool",
    }
    return mapping.get(text, text or "assistant")


def _msg_to_dict(msg: Any) -> Dict[str, Any]:
    """把 LangChain 消息对象转成 dict（兼容 dict 输入）。

    LangChain 的 tool_calls 元素可能是 TypedDict（dict）也可能是对象，
    两种形态都要能取到工具名。
    """
    if isinstance(msg, dict):
        return msg
    role = _normalize_role(getattr(msg, "type", None) or getattr(msg, "role", "assistant"))
    out: Dict[str, Any] = {
        "role": role,
        "content": getattr(msg, "content", "") or "",
    }
    #: **推理摘要**：harmony 格式（gpt-oss）会把推理放在 ``reasoning_content``。
    #: 这里只保留**原文**（前端会再压成前 1-2 句并标注截断），
    #: 但**不写入对话历史**（graph.py 里明确不把 reasoning 入历史）。
    raw_reason = (getattr(msg, "reasoning_content", None)
                  or getattr(msg, "reasoning", None) or "")
    if isinstance(raw_reason, str) and raw_reason.strip():
        out["reasoning_content"] = raw_reason
    tcs = getattr(msg, "tool_calls", None)
    if tcs:
        norm = []
        for tc in tcs:
            if isinstance(tc, dict):
                fn = tc.get("function", {}) or {}
                name = fn.get("name") or tc.get("name") or ""
                args = tc.get("args") if tc.get("args") is not None else fn.get("arguments")
                # **保留 tool_call_id**（2026-11-22 修复）：
                # 此前归一化时丢掉了 id，导致前端无法用共同主键去重，
                # 同一次工具调用被 `step.tool_calls` 与 `trace` 快照各渲染一行。
                cid = tc.get("id") or (fn.get("id") if isinstance(fn, dict) else "") or ""
            else:
                name = getattr(tc, "name", "") or ""
                args = getattr(tc, "args", None)
                cid = getattr(tc, "id", "") or ""
            norm.append({"id": str(cid or ""), "name": name, "args": args or {}})
        out["tool_calls"] = norm
    if role == "tool":
        out["tool_call_id"] = getattr(msg, "tool_call_id", "") or ""
        out["name"] = getattr(msg, "name", "") or ""
        #: **工具耗时（2026-11-22 新增）**：graph 层用单调时钟测的执行耗时。
        #: ToolMessage 本身没有这个属性，走 LangChain 时放在
        #: ``additional_kwargs`` 里（见 graph.py ``_to_langchain_messages``）；
        #: 走 dict 消息时是顶层字段。取不到就留 None（前端显示「–」，
        #: **不伪造数字**）。
        dur = getattr(msg, "duration_ms", None)
        if dur is None:
            kwargs = getattr(msg, "additional_kwargs", None)
            if isinstance(kwargs, dict):
                dur = kwargs.get("duration_ms")
        if dur is None and isinstance(msg, dict):
            dur = msg.get("duration_ms")
        if dur is not None:
            try:
                out["duration_ms"] = int(dur)
            except (TypeError, ValueError):
                pass
    return out


def _load_seed_rows() -> List[Dict[str, Any]]:
    """读取评测种子集（agent_platform/eval/datasets/v1.jsonl）。"""
    path = SEED_DATASET
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return rows


async def _sse_gen(payloads: List[Dict[str, Any]]) -> Any:
    """SSE 生成器：逐条推送 data: JSON（兼容旧调用方）。"""
    for payload in payloads:
        yield _sse_line(payload)
    yield "data: [DONE]\n\n"


# ----------------------------------------------------------------------
# 模块级 app（uvicorn 入口：uvicorn agent_platform.api.app:app）
# ----------------------------------------------------------------------
app: Any = None
if FASTAPI_AVAILABLE:
    try:
        app = create_app()
        app.state.started_at = time.time()
    except Exception:  # pragma: no cover
        app = None


if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
