"""OpenAI 兼容 LLM 客户端（薄封装）。

对应设计文档：
    docs/01_架构方案_RAG_Agent.md
        §2.3 路由层 —— 「模型路由」（大/小模型分流、多模型选择、降级链）
        §2.9 推理模型层 —— 「统一 LLM Gateway 调用（OpenAI 兼容接口）」
        §2.11 可观测层 —— 「Token / 成本核算」「全链路 Trace」
    docs/02_场景方案_STDF分析与测试用例生成.md
        §2.2 模型选型与理由 —— 所有模型通过统一 LLM Gateway 调用

本机环境事实：
    * llama-server 已在 127.0.0.1:8080 运行（alias ``gpt-oss-20b``）；
    * 第二模型计划在 127.0.0.1:8081（alias ``qwen2.5-14b``），由其它 worker 启动；
    * 本模块对此做**双端点预留**：``MODEL_ENDPOINTS`` 映射 alias → base_url。

降级：llama-server 不可用时，本模块的所有调用返回空串并打日志，
上层（检索链）据此走「未在知识库中找到」的拒答路径（docs/01 §3.1 第 10 步）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

__all__ = ["LLMClient", "LLMResponse", "MODEL_ENDPOINTS", "resolve_endpoint"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 模型端点预留（alias → base_url / 默认模型名）
# ---------------------------------------------------------------------------
MODEL_ENDPOINTS: dict[str, dict[str, str]] = {
    "gpt-oss-20b": {
        "base_url": os.environ.get("LLAMA_BASE_URL", "http://127.0.0.1:8080"),
        "model": "gpt-oss-20b",
    },
    "qwen2.5-14b": {
        "base_url": os.environ.get("LLAMA_BASE_URL_2", "http://127.0.0.1:8081"),
        "model": "qwen2.5-14b",
    },
}


def resolve_endpoint(model: str) -> tuple[str, str]:
    """解析模型 alias 对应的 (base_url, model_name)。

    Args:
        model: 模型 alias 或完整模型名。

    Returns:
        ``(base_url, model_name)``。未登记的 alias 默认走 8080 端口。
    """
    if model in MODEL_ENDPOINTS:
        cfg = MODEL_ENDPOINTS[model]
        return cfg["base_url"].rstrip("/"), cfg["model"]
    base = os.environ.get("LLAMA_BASE_URL", "http://127.0.0.1:8080")
    return base.rstrip("/"), model


class LLMResponse:
    """一次 LLM 调用的结果。

    Attributes:
        text: 输出文本。
        usage: token 用量字典。
        elapsed_ms: 端到端耗时（毫秒）。
        model: 实际使用的模型名。
        ok: 是否成功。
        error: 错误信息。
    """

    __slots__ = ("text", "usage", "elapsed_ms", "model", "ok", "error")

    def __init__(
        self,
        text: str = "",
        usage: dict[str, Any] | None = None,
        elapsed_ms: float = 0.0,
        model: str = "",
        ok: bool = True,
        error: str | None = None,
    ) -> None:
        self.text = text
        self.usage = usage or {}
        self.elapsed_ms = elapsed_ms
        self.model = model
        self.ok = ok
        self.error = error

    def to_dict(self) -> dict[str, Any]:
        """转 dict（用于报告落盘）。"""
        return {
            "text": self.text,
            "usage": self.usage,
            "elapsed_ms": self.elapsed_ms,
            "model": self.model,
            "ok": self.ok,
            "error": self.error,
        }


class LLMClient:
    """OpenAI 兼容接口客户端。

    Args:
        model: 模型 alias（见 :data:`MODEL_ENDPOINTS`）。
        base_url: 覆盖 base_url。
        temperature: 采样温度。
        max_tokens: 最大输出 token。
        timeout: HTTP 超时秒数。
    """

    def __init__(
        self,
        model: str = "gpt-oss-20b",
        base_url: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        timeout: float = 120.0,
    ) -> None:
        resolved_url, resolved_model = resolve_endpoint(model)
        self.base_url = (base_url or resolved_url).rstrip("/")
        self.model = resolved_model
        self.alias = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self._requests: Any = None

    # ------------------------------------------------------------------
    def _get_requests(self) -> Any:
        """懒加载 requests。

        Returns:
            requests 模块；不可用时返回 None。
        """
        if self._requests is None:
            try:
                import requests  # type: ignore

                self._requests = requests
            except Exception as exc:  # noqa: BLE001
                logger.error("requests 不可用：%s", exc)
                self._requests = False
        return self._requests or None

    # ------------------------------------------------------------------
    def chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        """发起一次 chat/completions 调用。

        Args:
            messages: 消息列表。
            model: 覆盖模型名。
            temperature: 覆盖温度。
            max_tokens: 覆盖最大输出 token。
            json_mode: 是否要求 JSON 输出。

        Returns:
            :class:`LLMResponse`。失败时 ``ok=False`` 且 text 为空串。
        """
        req = self._get_requests()
        if req is None:
            return LLMResponse(ok=False, error="requests 不可用", model=self.model)
        url = f"{self.base_url}/v1/chat/completions"
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        t0 = time.perf_counter()
        try:
            resp = req.post(url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM 调用失败（%s）：%s", url, exc)
            return LLMResponse(
                ok=False, error=str(exc), model=self.model,
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )
        elapsed = (time.perf_counter() - t0) * 1000
        text = ""
        reasoning = ""
        try:
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            text = (message.get("content") or "")
            reasoning = (
                message.get("reasoning_content")
                or message.get("reasoning")
                or ""
            )
        except Exception:
            text = ""
        # 推理型模型（如 gpt-oss）会先输出 reasoning_content，
        # 若 max_tokens 太小可能导致 content 为空但 reasoning 非空 —— 此时补足预算重试。
        if not (text or "").strip() and (reasoning or "").strip():
            logger.info(
                "模型 %s 仅返回 reasoning 内容（max_tokens=%s 偏小），放宽预算重试。",
                payload["model"], payload["max_tokens"],
            )
            retry_payload = dict(payload)
            # 放宽预算但设上限，避免推理型模型无限扩写导致请求超时
            retry_payload["max_tokens"] = min(
                int(payload["max_tokens"]) * 2 + 512, 8192
            )
            try:
                resp2 = req.post(url, json=retry_payload, timeout=self.timeout)
                resp2.raise_for_status()
                data2 = resp2.json()
                choice2 = (data2.get("choices") or [{}])[0]
                message2 = choice2.get("message") or {}
                text = (message2.get("content") or "") or text
                data = data2
                elapsed = (time.perf_counter() - t0) * 1000
            except Exception as exc:  # noqa: BLE001
                logger.warning("放宽预算重试失败：%s", exc)
        return LLMResponse(
            text=text,
            usage=data.get("usage") or {},
            elapsed_ms=elapsed,
            model=payload["model"],
            ok=True,
        )

    # ------------------------------------------------------------------
    def chat_json(
        self,
        messages: list[dict[str, str]],
        fallback: Any = None,
        **kwargs: Any,
    ) -> Any:
        """发起 chat 调用并解析 JSON 输出。

        Args:
            messages: 消息列表。
            fallback: 解析失败时返回的默认值。
            **kwargs: 透传给 :meth:`chat`。

        Returns:
            解析后的 JSON 对象；失败返回 ``fallback``。
        """
        resp = self.chat(messages, **kwargs)
        if not resp.ok or not resp.text:
            return fallback
        raw = resp.text.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:]
        raw = raw.strip()
        try:
            return json.loads(raw)
        except Exception:
            # 尝试抽取第一个 JSON 对象
            start, end = raw.find("{"), raw.rfind("}")
            if start >= 0 and end > start:
                try:
                    return json.loads(raw[start : end + 1])
                except Exception:
                    pass
            logger.warning("LLM 输出不是合法 JSON：%.200s", resp.text)
            return fallback

    # ------------------------------------------------------------------
    def health(self) -> bool:
        """探活（GET /v1/models）。

        Returns:
            服务是否可达。
        """
        req = self._get_requests()
        if req is None:
            return False
        try:
            resp = req.get(f"{self.base_url}/v1/models", timeout=5)
            return resp.status_code < 500
        except Exception:
            return False
