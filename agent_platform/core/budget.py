# -*- coding: utf-8 -*-
"""BudgetGuard —— Agent 预算与超时闸门。

对应设计文档：
    《01_架构方案_RAG_Agent.md》§2.4 Agent 编排层 →「预算与超时控制」
        技术选型 A. 自研 BudgetGuard（M1 必备，成本红线）
    《01_架构方案_RAG_Agent.md》§3.2 Agent 工具调用可靠性设计 →「循环与预算」
        最多 8 轮 ReAct、同一工具同一参数连续 2 次失败即终止
    《01_架构方案_RAG_Agent.md》§2.2 上下文窗口管理
        「工具结果可单独设上限，如"单工具结果最多 4K token"」

默认值与 docs/01 §3.2「超时控制」保持一致：
    工具整体 60s、任务总时长 15min；单工具结果上限 4K token。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional


def estimate_tokens(text: str) -> int:
    """估算一段文本的 token 数（**启发式，非精确**）。

    为什么需要它（2026-11-22 新增）：
        llama-server 的单次请求上下文窗口有限（现为 32768 token，见
        ``deploy/start_servers.ps1``），而 ``BudgetGuard.max_tokens``（60k）
        是**整个会话**的累计预算 —— 两者不等价。若只用「字符数 // 2」
        这种乐观估计，中文与 JSON 工具结果会被**严重低估**，
        于是请求直接被模型端以 400 拒绝（BadRequestError），
        任务变成 exception 而不是优雅收尾。

    估算规则（**实测标定**，2026-11-23 修正）：
        · **CJK 汉字/假名/韩文**：1 字 ≈ **0.70** token
        · **ASCII 其它字符**：4 字符 ≈ 1 token
        · **ASCII 空白**：不计

    ★ **0.70 这个系数是实测出来的**，不是拍脑袋：
        用 gpt-oss-20b 端点标定 —— 2000 个中文字符的 prompt，
        模型端返回 ``usage.prompt_tokens = 1400``，即 **0.7000 token/字**。
        原实现按 1.0 计，会**高估约 43%**，在 32K 窗口下白白浪费
        ~9K token 的可用空间（够放 200 行中文注释的代码）。
        改为 0.70 后估算与真实值吻合，同时仍保留安全余量
        （下面的每条消息固定开销与舍入），宁可早停也不超窗。
    """
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        code = ord(ch)
        # CJK 统一表意文字（含扩展 A）、假名、韩文音节、汉字标点
        if (0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF
                or 0x3040 <= code <= 0x30FF or 0xAC00 <= code <= 0xD7AF
                or 0x3000 <= code <= 0x303F or 0xFF00 <= code <= 0xFFEF):
            cjk += 1
        elif not ch.isspace():
            other += 1
    return int(cjk * 0.70 + 0.5) + (other + 3) // 4


def estimate_messages_tokens(messages: Iterable[Any]) -> int:
    """估算一组消息（dict 或 LangChain 对象）的 token 总量。

    ⚠️ 对非 dict 的消息，必须把 ``content`` 归一成字符串再估 ——
    LangChain 的 ``ToolMessage.content`` 可能是**列表**（内容块），
    直接 ``str()`` 会得到带转义的 repr，估算偏差很大。
    """
    total = 0
    for m in messages or []:
        content = ""
        if isinstance(m, dict):
            content = str(m.get("content", "") or "")
            # 工具调用的参数 JSON 也要计入
            for tc in (m.get("tool_calls") or []):
                if isinstance(tc, dict):
                    fn = tc.get("function") or {}
                    content += str(fn.get("arguments", "") or "")
        else:
            raw = getattr(m, "content", "") or ""
            if not isinstance(raw, str):
                try:
                    raw = json.dumps(raw, ensure_ascii=False, default=str)
                except Exception:
                    raw = str(raw)
            content = raw
        # 每条消息有 role/分隔符的固定开销（约 4 token）
        total += estimate_tokens(content) + 4
    return total


class BudgetExceeded(RuntimeError):
    """预算/超时超限异常。

    对应 docs/01 §3.2「终止原因记录到 Trace，便于失败归因」：
    reason 区分超限维度，detail 给出可读的阈值与当前值。
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"[{reason}] {detail}" if detail else f"[{reason}]")


@dataclass
class BudgetGuard:
    """Agent 级预算闸门。

    用法：
        budget.check(est_tokens=1200)   # 调用 LLM 前检查
        budget.charge_tokens(prompt=100, completion=300)
        budget.charge_tool_call()        # 每次真实执行工具时计一次
    """

    max_rounds: int = 8
    max_tokens: int = 60_000
    max_tool_calls: int = 24
    max_seconds: float = 900.0
    max_tokens_per_tool_result: int = 4_000
    #: **单次请求的上下文窗口上限**（llama-server 的 ``-c`` 值，现为 32768）。
    #:
    #: 为什么需要（2026-11-22 新增）：``max_tokens``（60k）是**整个会话**的
    #: 累计预算，但 llama-server 的**单次请求**上下文窗口有限（8192 → 32768）。
    #: 二者不等价：会话累计才 20k token 时，单次请求也可能因为
    #: system prompt + 工具 schema + 若干大体积工具结果而**超窗**，
    #: 模型端直接返回 400（"request exceeds the available context size"）。
    #:
    #: 这里在**调用 LLM 之前**按估算值拦截，让任务以
    #: ``budget_exceeded:MAX_CONTEXT`` **优雅收尾**（有 final_answer），
    #: 而不是抛 BadRequestError 让整个请求变成 exception。
    #: 取 0 表示不启用该限制（保持旧行为）。
    context_window: int = 0

    # ---- 运行态 ----
    rounds: int = 0
    tokens_used: int = 0
    tool_calls_used: int = 0
    started_at: float = field(default_factory=time.monotonic)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ------------------------------------------------------------------
    # 检查类方法
    # ------------------------------------------------------------------
    def check(self, est_tokens: int = 0) -> None:
        """在调用 LLM 之前检查预算是否仍然可用。

        est_tokens 为本次预计消耗（通常按 prompt token 估算），
        用于在真正超限前提前熔断，避免产生"最后一轮才发现爆预算"。
        """
        with self._lock:
            elapsed = time.monotonic() - self.started_at
            if self.rounds >= self.max_rounds:
                raise BudgetExceeded(
                    "MAX_ROUNDS",
                    f"ReAct 轮次已达上限 rounds={self.rounds} >= max_rounds={self.max_rounds}",
                )
            if self.tokens_used + max(0, int(est_tokens)) > self.max_tokens:
                raise BudgetExceeded(
                    "MAX_TOKENS",
                    f"token 预算不足 tokens_used={self.tokens_used} + est={est_tokens} "
                    f"> max_tokens={self.max_tokens}",
                )
            # ---- 单次请求上下文窗口（llama-server -c）----
            if self.context_window and max(0, int(est_tokens)) > self.context_window:
                raise BudgetExceeded(
                    "MAX_CONTEXT",
                    f"单次请求预计 token {est_tokens} 超过上下文窗口 "
                    f"{self.context_window}，请精简上下文或压缩历史轮次",
                )
            if self.tool_calls_used >= self.max_tool_calls:
                raise BudgetExceeded(
                    "MAX_TOOL_CALLS",
                    f"工具调用次数已达上限 tool_calls_used={self.tool_calls_used} "
                    f">= max_tool_calls={self.max_tool_calls}",
                )
            if elapsed > self.max_seconds:
                raise BudgetExceeded(
                    "MAX_SECONDS",
                    f"任务总时长超限 elapsed={elapsed:.1f}s > max_seconds={self.max_seconds}",
                )

    # ------------------------------------------------------------------
    # 计费类方法
    # ------------------------------------------------------------------
    def charge_tokens(self, tokens: int = 0, prompt: Optional[int] = None,
                      completion: Optional[int] = None) -> int:
        """累计 token 用量。prompt/completion 同时给出时按其和计费。"""
        if prompt is not None or completion is not None:
            total = int(prompt or 0) + int(completion or 0)
        else:
            total = int(tokens or 0)
        with self._lock:
            self.tokens_used += total
            if self.tokens_used > self.max_tokens:
                raise BudgetExceeded(
                    "MAX_TOKENS",
                    f"token 用量超限 tokens_used={self.tokens_used} > max_tokens={self.max_tokens}",
                )
        return self.tokens_used

    def charge_tool_call(self, count: int = 1) -> int:
        """累计工具调用次数。"""
        with self._lock:
            self.tool_calls_used += int(count)
            if self.tool_calls_used > self.max_tool_calls:
                raise BudgetExceeded(
                    "MAX_TOOL_CALLS",
                    f"工具调用次数超限 tool_calls_used={self.tool_calls_used} "
                    f"> max_tool_calls={self.max_tool_calls}",
                )
        return self.tool_calls_used

    def charge_round(self, count: int = 1) -> int:
        """累计 ReAct 轮次。"""
        with self._lock:
            self.rounds += int(count)
        return self.rounds

    def check_no_rounds(self, est_tokens: int = 0) -> None:
        """检查预算，但**不检查轮次上限**（tools 节点专用）。

        为什么需要这个方法（2026-11-24 修复）
        --------------------------------------
        ``check()`` 里的 ``self.rounds >= self.max_rounds`` 是**基于已计费轮次**
        的判定。而轮次是在 **agent 节点**调 ``charge_round()`` 时推进的。
        此前 **tools 节点也调了 ``check()``**，于是 agent→tools 一个来回里
        判定被触发两次：

            第 1 轮 agent：charge_round → rounds=1
            第 1 轮 tools：check → rounds(1) >= max_rounds(1) → 立即熔断

        实际上工具节点**根本没有推进过轮次**，却因为 agent 已经推进了轮次
        而被误判「轮次已用完」。结果：``max_rounds=8`` 实际只够跑 **4 轮**，
        用户抱怨「才几步就提示预算用尽」。

        因此语义明确为：**轮次只在 agent 节点计费与判定**；
        tools 节点只检查工具调用数 / 总时长 / 上下文窗口。

        Args:
            est_tokens: 本次预计消耗（可选）。

        Raises:
            BudgetExceeded: 工具调用数 / 时长 / 上下文窗口超限时抛出。
        """
        with self._lock:
            elapsed = time.monotonic() - self.started_at
            if self.tokens_used + max(0, int(est_tokens)) > self.max_tokens:
                raise BudgetExceeded(
                    "MAX_TOKENS",
                    f"token 预算不足 tokens_used={self.tokens_used} + est={est_tokens} "
                    f"> max_tokens={self.max_tokens}",
                )
            if self.context_window and max(0, int(est_tokens)) > self.context_window:
                raise BudgetExceeded(
                    "MAX_CONTEXT",
                    f"单次请求预计 token {est_tokens} 超过上下文窗口 "
                    f"{self.context_window}，请精简上下文或压缩历史轮次",
                )
            if self.tool_calls_used >= self.max_tool_calls:
                raise BudgetExceeded(
                    "MAX_TOOL_CALLS",
                    f"工具调用次数已达上限 tool_calls_used={self.tool_calls_used} "
                    f">= max_tool_calls={self.max_tool_calls}",
                )
            if elapsed > self.max_seconds:
                raise BudgetExceeded(
                    "MAX_SECONDS",
                    f"任务总时长超限 elapsed={elapsed:.1f}s > max_seconds={self.max_seconds}",
                )

    # ------------------------------------------------------------------
    # 查询类方法
    # ------------------------------------------------------------------
    @property
    def elapsed(self) -> float:
        """已耗时（秒）。"""
        return time.monotonic() - self.started_at

    def snapshot(self) -> Dict[str, Any]:
        """返回当前预算快照，供 API / 审计 / Trace 使用。"""
        return {
            "rounds": self.rounds,
            "max_rounds": self.max_rounds,
            "tokens_used": self.tokens_used,
            "max_tokens": self.max_tokens,
            "tool_calls_used": self.tool_calls_used,
            "max_tool_calls": self.max_tool_calls,
            "elapsed_seconds": round(self.elapsed, 2),
            "max_seconds": self.max_seconds,
            "max_tokens_per_tool_result": self.max_tokens_per_tool_result,
            "context_window": self.context_window,
        }

    def reset(self) -> None:
        """重置运行态（新建会话时使用）。"""
        with self._lock:
            self.rounds = 0
            self.tokens_used = 0
            self.tool_calls_used = 0
            self.started_at = time.monotonic()
