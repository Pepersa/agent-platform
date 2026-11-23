# -*- coding: utf-8 -*-
"""模型注册表（Model Registry）。

对应设计文档：
    《01_架构方案_RAG_Agent.md》§2.3 路由层 →「模型路由」
        原则 6「模型可替换」：Prompt、评测集、Tool Schema 三者解耦
    《03_显存与模型选型计算表.md》（本地 GGUF + llama.cpp 推理后端背景）

本模块解决「模型写死在前端下拉框」的问题：

    · **静态来源**：``config/routing.yaml`` 的 ``models:`` 段
      （base_url / model / reasoning_effort / temperature / max_tokens …）
    · **动态来源**：``data/models.json``（运行时新增的自定义模型）
    · 两者合并后形成**统一注册表**，对外暴露 ``GET /models``。
    · 每个条目都做**端点可达性探测**（复用 ``LLMClient.health()``），
      因此前端显示的状态点是**真实探测结果**，不是臆造。

持久化：``data/models.json``，**原子写**（先写临时文件再 ``os.replace``），
避免写一半崩溃导致注册表损坏。

安全边界（诚实说明）：
    · 注册表里的 ``base_url`` 由使用者自己填写，平台**不做白名单限制**；
      这意味着它可以指向任意 HTTP 端点。仅在本机可信环境部署时可用。
    · ``api_key`` 允许配置，但**不会**通过 ``GET /models`` 明文回显
      （只回显是否已配置）。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

#: 自定义模型注册表落盘位置（相对**数据根目录**）
DEFAULT_STORE_PATH = "models.json"

#: 允许的推理强度
REASONING_EFFORTS = ("low", "medium", "high")

#: **模型能力标签**（与 ``core.llm_client.CAPABILITIES`` 保持一致）。
#:
#: 能力标签用于**降级保护**：路由项声明所需能力后，
#: 路由器会剔除降级链中不具备该能力的模型，避免代码类任务
#: 静默降级到通用对话模型上（详见 docs/06_代码模型接入策略.md）。
CAPABILITIES: tuple = (
    "code", "tool_call", "reasoning", "chinese", "long_context", "vision",
)

#: 模型名合法性：字母 / 数字 / 下划线 / 短横线 / 点，1-64 字符
_NAME_MAX = 64


def _default_capabilities(name: str) -> List[str]:
    """取某模型的**默认能力标签**（复用 llm_client 的内置表）。

    延迟导入，避免模块级循环依赖（llm_client 不依赖 model_registry）。
    """
    try:
        from .llm_client import DEFAULT_CAPABILITIES  # noqa: PLC0415
    except Exception:  # pragma: no cover - 理论上不会发生
        return []
    return list(DEFAULT_CAPABILITIES.get(str(name), []))


def _repo_root() -> Path:
    """仓库根目录（``agent_platform/core/model_registry.py`` 上溯三级）。

    静态路由配置 ``config/routing.yaml`` 属于**代码仓库**的一部分，
    与 ``AGENT_DATA_DIR`` 指向的运行时数据目录无关，因此必须按仓库
    根目录来定位（不能跟着数据目录走）。
    """
    return Path(__file__).resolve().parent.parent.parent


def _default_store_path() -> Path:
    """取默认的注册表路径（数据根目录 / models.json）。

    数据根目录由 :mod:`agent_platform.core.paths` 统一解析：
    环境变量 ``AGENT_DATA_DIR`` > 项目默认 ``<repo_root>/data``。
    **不设环境变量时仍然落在 ``<repo>/data/models.json``**，与改造前一致
    （已有的自定义模型条目不会丢）。
    """
    from . import paths  # noqa: PLC0415 - 延迟导入避免循环依赖

    return paths.data_path(DEFAULT_STORE_PATH)


def _atomic_write_json(path: Path, payload: Any) -> None:
    """原子写 JSON（先写 .tmp 再 os.replace）。

    同一目录内 ``os.replace`` 在 Windows / POSIX 上都是原子的，
    因此不会出现「读到半个文件」的情况。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex[:8]}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(path))
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:  # pragma: no cover - 清理失败无所谓
            pass


class ModelRegistry:
    """模型注册表（静态 routing.yaml + 动态 models.json 合并）。

    线程安全：所有读操作加锁；写操作加锁 + 原子落盘。
    """

    def __init__(self, *, store_path: Optional[str] = None,
                 routing_file: Optional[str] = None) -> None:
        self.store_path = Path(store_path) if store_path else _default_store_path()
        self.routing_file = routing_file
        self._lock = threading.RLock()
        #: 自定义模型（来自 data/models.json）：{name: {...}}
        self._custom: Dict[str, Dict[str, Any]] = {}
        #: 静态模型（来自 routing.yaml）：{name: {...}}
        self._static: Dict[str, Dict[str, Any]] = {}
        self._load()

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------
    def _load(self) -> None:
        """从磁盘加载静态与自定义模型。"""
        with self._lock:
            self._static = self._read_static()
            self._custom = self._read_custom()

    def _read_static(self) -> Dict[str, Dict[str, Any]]:
        """读 ``config/routing.yaml`` 的 ``models:`` 段。"""
        path = self.routing_file
        if not path:
            env = os.environ.get("ROUTING_CONFIG", "").strip()
            if env:
                path = env
            else:
                path = str(_default_store_path().parent.parent / "config" / "routing.yaml")
        try:
            import yaml  # type: ignore
        except Exception:
            return {}
        try:
            p = Path(path)
            if not p.is_file():
                # ★ 兜底（2026-11-23）：``_default_store_path()`` 落在
                #   ``AGENT_DATA_DIR`` 指向的**数据目录**下，其 ``parent.parent``
                #   在隔离数据目录（如 ``data/_tmp/selfhost_data_8021``）里
                #   根本不存在 ``config/routing.yaml`` → 静态模型列表为空 →
                #   ``/models`` 返回 0 条、``verify_modes.py`` 全部失败。
                #   静态路由配置属于**代码仓库**（不是运行时数据），
                #   所以按「仓库根 / config / routing.yaml」再兜一次。
                repo_cfg = _repo_root() / "config" / "routing.yaml"
                if repo_cfg.is_file():
                    p = repo_cfg
                else:
                    return {}
            with p.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        models = data.get("models")
        if not isinstance(models, dict):
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for name, conf in models.items():
            if not isinstance(conf, dict):
                continue
            # 能力标签：yaml 显式声明优先，否则用 llm_client 的内置默认表
            caps = conf.get("capabilities")
            if isinstance(caps, list):
                capabilities = [str(c).strip() for c in caps if str(c).strip()]
            else:
                capabilities = list(_default_capabilities(str(name)))
            out[str(name)] = {
                "name": str(name),
                "label": str(conf.get("label") or name),
                "base_url": str(conf.get("base_url", "") or "").rstrip("/"),
                "model": str(conf.get("model") or name),
                "description": str(conf.get("description", "") or ""),
                "reasoning_effort": str(conf.get("reasoning_effort", "medium")).lower(),
                "temperature": float(conf.get("temperature", 0.2) or 0.2),
                "max_tokens": int(conf.get("max_tokens", 2048) or 2048),
                "api_key": str(conf.get("api_key", "local") or "local"),
                "timeout": float(conf.get("timeout", 180) or 180),
                "capabilities": capabilities,
                "installed": bool(conf.get("installed", True)),
                "source": "routing.yaml",
                "builtin": True,
            }
        return out

    def _read_custom(self) -> Dict[str, Dict[str, Any]]:
        """读 ``data/models.json``。"""
        try:
            if not self.store_path.is_file():
                return {}
            with self.store_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return {}
        items = data.get("models") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for it in items:
            if not isinstance(it, dict):
                continue
            name = str(it.get("name", "") or "").strip()
            if not name:
                continue
            out[name] = self._normalize(it, builtin=False)
        return out

    @staticmethod
    def _normalize(raw: Dict[str, Any], *, builtin: bool) -> Dict[str, Any]:
        """把一条模型定义归一成统一结构。"""
        effort = str(raw.get("reasoning_effort", "medium")).lower()
        if effort not in REASONING_EFFORTS:
            effort = "medium"
        try:
            temperature = float(raw.get("temperature", 0.2))
        except Exception:
            temperature = 0.2
        try:
            max_tokens = int(raw.get("max_tokens", 2048))
        except Exception:
            max_tokens = 2048
        try:
            timeout = float(raw.get("timeout", 180))
        except Exception:
            timeout = 180.0
        # 能力标签：显式声明优先，否则按模型名取内置默认表
        caps_raw = raw.get("capabilities")
        if isinstance(caps_raw, list):
            capabilities = [str(c).strip() for c in caps_raw if str(c).strip()]
        else:
            capabilities = _default_capabilities(str(raw.get("name", "") or ""))
        return {
            "name": str(raw.get("name", "") or "").strip(),
            "label": str(raw.get("label") or raw.get("name") or "").strip(),
            "base_url": str(raw.get("base_url", "") or "").rstrip("/"),
            "model": str(raw.get("model") or raw.get("name") or "").strip(),
            "description": str(raw.get("description", "") or ""),
            "reasoning_effort": effort,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "api_key": str(raw.get("api_key", "local") or "local"),
            "timeout": timeout,
            "supports_tools": bool(raw.get("supports_tools", True)),
            "capabilities": capabilities,
            "installed": bool(raw.get("installed", True)),
            "source": "custom" if not builtin else "routing.yaml",
            "builtin": bool(builtin),
            "created_at": raw.get("created_at"),
            "updated_at": raw.get("updated_at"),
        }

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def all_models(self) -> List[Dict[str, Any]]:
        """返回全部模型（静态 + 自定义，自定义同名时覆盖静态）。"""
        with self._lock:
            merged: Dict[str, Dict[str, Any]] = {}
            merged.update(self._static)
            merged.update(self._custom)
            return [merged[k] for k in sorted(merged.keys())]

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        """按名称取模型定义（None 表示未登记）。"""
        with self._lock:
            if name in self._custom:
                return dict(self._custom[name])
            if name in self._static:
                return dict(self._static[name])
            return None

    def exists(self, name: str) -> bool:
        """模型是否已登记。"""
        return self.get(name) is not None

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """注册一个自定义模型（同名 → 抛 ``ValueError``）。

        Args:
            payload: ``{name, base_url, model, label, description, ...}``。

        Returns:
            归一化后的模型定义。

        Raises:
            ValueError: 名称非法 / 已存在 / base_url 非法。
        """
        name = str(payload.get("name", "") or "").strip()
        if not name:
            raise ValueError("模型名称不能为空")
        if len(name) > _NAME_MAX:
            raise ValueError(f"模型名称过长（最多 {_NAME_MAX} 字符）")
        if not all(ch.isalnum() or ch in "-_." for ch in name):
            raise ValueError("模型名称只能包含字母、数字、下划线、短横线与点")
        base_url = str(payload.get("base_url", "") or "").strip().rstrip("/")
        if not base_url:
            raise ValueError("base_url 不能为空")
        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        with self._lock:
            if name in self._custom:
                raise ValueError(f"模型 '{name}' 已存在，请使用 PUT 更新")
            now = time.time()
            rec = self._normalize({
                "name": name,
                "label": payload.get("label") or name,
                "base_url": base_url,
                "model": payload.get("model") or name,
                "description": payload.get("description", "") or "",
                "reasoning_effort": payload.get("reasoning_effort", "medium"),
                "temperature": payload.get("temperature", 0.2),
                "max_tokens": payload.get("max_tokens", 2048),
                "api_key": payload.get("api_key", "local") or "local",
                "timeout": payload.get("timeout", 180),
                "supports_tools": payload.get("supports_tools", True),
            }, builtin=False)
            rec["created_at"] = now
            rec["updated_at"] = now
            self._custom[name] = rec
            self._persist()
            return dict(rec)

    def update(self, name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """更新一个自定义模型（不存在 → 抛 ``ValueError``）。"""
        with self._lock:
            cur = self._custom.get(name)
            if cur is None:
                raise ValueError(f"模型 '{name}' 不存在（内置模型不可修改）")
            merged = dict(cur)
            for key in ("label", "description", "base_url", "model",
                        "reasoning_effort", "temperature", "max_tokens",
                        "api_key", "timeout", "supports_tools"):
                if key in payload and payload[key] is not None:
                    merged[key] = payload[key]
            merged["name"] = name
            base_url = str(merged.get("base_url", "") or "").rstrip("/")
            if not base_url.startswith("http://") and not base_url.startswith("https://"):
                raise ValueError("base_url 必须以 http:// 或 https:// 开头")
            rec = self._normalize(merged, builtin=False)
            rec["created_at"] = cur.get("created_at")
            rec["updated_at"] = time.time()
            self._custom[name] = rec
            self._persist()
            return dict(rec)

    def delete(self, name: str) -> bool:
        """删除一个自定义模型（内置模型不可删 → 返回 False）。"""
        with self._lock:
            if name not in self._custom:
                return False
            self._custom.pop(name, None)
            self._persist()
            return True

    def _persist(self) -> None:
        """把自定义模型写回 ``data/models.json``（原子写）。"""
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "models": [self._custom[k] for k in sorted(self._custom.keys())],
        }
        _atomic_write_json(self.store_path, payload)

    # ------------------------------------------------------------------
    # 健康探测
    # ------------------------------------------------------------------
    async def probe(self, name: str) -> Dict[str, Any]:
        """探测某模型端点的可达性（真实请求 ``/v1/models``）。

        Returns:
            ``{name, reachable, error, base_url, model, latency_ms, server_models}``
        """
        rec = self.get(name)
        if rec is None:
            return {"name": name, "reachable": False,
                    "error": f"模型 '{name}' 未登记", "base_url": "", "model": ""}
        # 延迟导入，避免模块级耦合
        from .llm_client import LLMClient, LLMConfig  # noqa: PLC0415

        cfg = LLMConfig(
            base_url=rec["base_url"],
            model=rec["model"],
            reasoning_effort=rec.get("reasoning_effort", "medium"),
            temperature=float(rec.get("temperature", 0.2) or 0.2),
            max_tokens=int(rec.get("max_tokens", 2048) or 2048),
            api_key=rec.get("api_key", "local") or "local",
            timeout=min(float(rec.get("timeout", 180) or 180), 15.0),
        )
        client = LLMClient(cfg, max_retries=0)
        # ★ 探测必须**快速失败**（2026-11-23 修正）
        #   端点没起时，openai SDK 的连接错误要等满 ``timeout`` 才抛
        #   （实测一个未监听的端口耗掉 21 秒），把整个 /models 端点拖成
        #   20+ 秒，前端 ``wait_until="networkidle"`` 直接超时。
        #   这里给探测单独设 3 秒上限 + 关闭重试：
        #   探测只是「现在通不通」，慢一点不代表不可达，不值得让整页等。
        probe_timeout = min(float(rec.get("timeout", 180) or 180), 3.0)
        client.config.timeout = probe_timeout
        t0 = time.time()
        try:
            h = await client.health()
            latency = int((time.time() - t0) * 1000)
            return {
                "name": name,
                "reachable": bool(h.get("reachable")),
                "error": str(h.get("error", "") or ""),
                "base_url": rec["base_url"],
                "model": rec["model"],
                "latency_ms": latency,
                "server_models": h.get("models", []) or [],
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "name": name,
                "reachable": False,
                "error": f"{type(exc).__name__}: {exc}",
                "base_url": rec["base_url"],
                "model": rec["model"],
                "latency_ms": int((time.time() - t0) * 1000),
                "server_models": [],
            }

    async def probe_all(self) -> Dict[str, Dict[str, Any]]:
        """并发探测全部模型端点。"""
        import asyncio  # noqa: PLC0415

        names = [m["name"] for m in self.all_models()]
        if not names:
            return {}
        results = await asyncio.gather(
            *[self.probe(n) for n in names], return_exceptions=True)
        out: Dict[str, Dict[str, Any]] = {}
        for name, res in zip(names, results):
            if isinstance(res, BaseException):
                out[name] = {"name": name, "reachable": False,
                             "error": f"{type(res).__name__}: {res}",
                             "base_url": "", "model": ""}
            else:
                out[name] = res
        return out

    def to_client_config(self, name: str) -> Optional[Any]:
        """把注册表条目转成 ``LLMConfig``（未登记返回 None）。"""
        rec = self.get(name)
        if rec is None:
            return None
        from .llm_client import LLMConfig  # noqa: PLC0415

        return LLMConfig(
            base_url=rec["base_url"],
            model=rec["model"],
            reasoning_effort=rec.get("reasoning_effort", "medium"),
            temperature=float(rec.get("temperature", 0.2) or 0.2),
            max_tokens=int(rec.get("max_tokens", 2048) or 2048),
            api_key=rec.get("api_key", "local") or "local",
            timeout=float(rec.get("timeout", 180) or 180),
        )


__all__ = ["ModelRegistry"]
