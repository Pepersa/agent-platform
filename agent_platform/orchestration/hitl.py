# -*- coding: utf-8 -*-
"""HITL 人工确认闸门。

对应设计文档：
    《01_架构方案_RAG_Agent.md》§2.4 →「HITL 人工确认闸门」
        A. 状态机挂起 + IM 卡片审批（体验与审计最佳）
    《01_架构方案_RAG_Agent.md》§3.2 →「HITL 人工闸门」
        风险分级 ≥ 写操作 的动作挂起，等待审批；超时自动拒绝（fail-safe）
    《01_架构方案_RAG_Agent.md》§3.2 →「不可逆动作二次确认」
        必须展示"将要执行什么 + 影响范围 + 可回滚性"
    《02_场景方案_STDF分析与测试用例生成.md》§5.3 HITL 交互规范
        · 确认卡片包含：动作摘要 + 完整参数 + 影响范围
        · 支持"确认一次，本次会话内全部允许"（会话级授权，带 TTL）
        · 所有 HITL 决策进入审计表：who / when / what / params / decision

**关键设计（LangGraph interrupt 的正确用法）**：
    interrupt() 之前**绝对不能有任何副作用**。
    因为 LangGraph 在 resume 时会从 interrupt 所在节点**从头重跑**，
    节点内 interrupt 之前的代码会再次执行。
    因此本模块的 require() 只做「读取当前待审批项 + 抛出 interrupt」，
    真正的执行动作交给 tools 节点在审批通过之后做。
    审计写在 interrupt 之后（即恢复之后由 hitl 节点补写）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: 会话级授权的默认 TTL（秒）。docs/02 §5.3「会话级授权，带 TTL」
SESSION_GRANT_TTL_SECONDS = 30 * 60


@dataclass
class PendingApproval:
    """一条待人工确认的工具调用（对应 docs/02 §5.3 确认卡片内容）。"""

    tool_call_id: str
    tool_name: str
    args: Dict[str, Any] = field(default_factory=dict)
    permission: str = "WRITE_DATA"
    #: 动作摘要（"将要执行什么"）
    description: str = ""
    #: 影响范围（例："向 12 人发告警"）
    impact: str = ""
    #: 可回滚性（不可逆动作必须显式说明）
    reversible: bool = True
    created_at: float = field(default_factory=time.time)
    #: 工具规范元信息：审批人、风险说明等
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_card(self) -> Dict[str, Any]:
        """转成前端确认卡片（docs/02 §5.3 要求字段）。

        **向后兼容**：新增 ``diff`` 字段（仅工作区写工具非空），
        既有工具的卡片内容保持不变。
        """
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "permission": self.permission,
            "description": self.description,
            "impact": self.impact,
            "reversible": self.reversible,
            "args": self.args,
            "created_at": self.created_at,
            "metadata": self.metadata,
            "diff": (self.metadata or {}).get("diff"),
        }

    def to_dict(self) -> Dict[str, Any]:
        return self.to_card()


@dataclass
class ApprovalDecision:
    """审批决策（docs/02 §5.3：who / when / what / params / decision）。"""

    tool_call_id: str
    approved: bool
    actor: str = ""
    reason: str = ""
    #: 勾选"确认一次，本次会话内允许"时的会话级授权标记
    session_grant: bool = False
    decided_at: float = field(default_factory=time.time)
    #: **是否强制走「模型总结」路径**（2026-11-24 新增，修复「审批太慢」）。
    #:
    #: 默认 False（快速路径）：批准后若被批准的动作本身就是终态
    #: （写工具一次执行即产出完整、自描述的结果 JSON），
    #: 就**跳过第二次 LLM 调用**，直接用工具结果作答。
    #: 置 True 则**完全照旧**：跑第二次 LLM 生成自然语言总结。
    #:
    #: ⚠️ 这个标记随 resume payload 一起回传，因此能**穿过
    #: interrupt() 的中断/恢复边界**到达 tools 节点
    #: （tools 节点从 state["approvals"] 里读到它）。
    summarize: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "approved": self.approved,
            "actor": self.actor,
            "reason": self.reason,
            "session_grant": self.session_grant,
            "decided_at": self.decided_at,
            "summarize": self.summarize,
        }


class HITLGate:
    """人工确认闸门。

    典型用法（在 LangGraph 节点内）：
        decision = gate.require(pending)          # 未授权 → 抛 interrupt
        if not decision.approved:
            return {"stop_reason": "rejected_by_human", ...}
        # 审批通过 → 继续执行工具
    """

    def __init__(self, *, audit: Any = None,
                 session_grant_ttl: int = SESSION_GRANT_TTL_SECONDS) -> None:
        self._audit = audit
        self.session_grant_ttl = int(session_grant_ttl)
        #: session_id → {tool_name: 过期时间戳}
        self._session_grants: Dict[str, Dict[str, float]] = {}
        #: 决策缓存：tool_call_id → ApprovalDecision（resume 后取回）
        self._decisions: Dict[str, ApprovalDecision] = {}

    # ------------------------------------------------------------------
    # 会话级授权
    # ------------------------------------------------------------------
    def grant_session(self, session_id: str, tool_name: str) -> None:
        """授予会话级权限（"确认一次，本次会话内全部允许"）。"""
        if not session_id:
            return
        self._session_grants.setdefault(session_id, {})[tool_name] = (
            time.time() + self.session_grant_ttl
        )

    def has_session_grant(self, session_id: str, tool_name: str) -> bool:
        """判断会话级授权是否仍然有效（TTL 内）。"""
        if not session_id:
            return False
        grants = self._session_grants.get(session_id) or {}
        expire = grants.get(tool_name)
        if expire is None:
            return False
        if expire < time.time():
            grants.pop(tool_name, None)
            return False
        return True

    # ------------------------------------------------------------------
    # 决策存取
    # ------------------------------------------------------------------
    def record_decision(self, decision: ApprovalDecision, *,
                        tool_name: str = "", session_id: str = "") -> None:
        """记录一条决策（由 hitl 节点在恢复后调用）。"""
        self._decisions[decision.tool_call_id] = decision
        if decision.session_grant and decision.approved and session_id and tool_name:
            self.grant_session(session_id, tool_name)

    def get_decision(self, tool_call_id: str) -> Optional[ApprovalDecision]:
        return self._decisions.get(tool_call_id)

    def put_decision(self, tool_call_id: str, decision: ApprovalDecision) -> None:
        self._decisions[tool_call_id] = decision

    # ------------------------------------------------------------------
    # 核心：require
    # ------------------------------------------------------------------
    def require(self, pending: PendingApproval, *,
                session_id: Optional[str] = None,
                approved: Optional[bool] = None,
                actor: Optional[str] = None,
                reason: str = "",
                session_grant: bool = False,
                use_interrupt: bool = True) -> ApprovalDecision:
        """请求人工审批，返回审批决策。

        流程：
            1. 已有决策缓存 → 直接返回，不抛 interrupt
            2. 会话级授权有效 → 直接视为批准
            3. 显式传入 approved（离线/测试场景）→ 记录决策并返回
            4. 否则 → 调用 langgraph 的 interrupt() 挂起；
               resume 后 interrupt() 返回审批 payload，转成 ApprovalDecision 返回

        **interrupt() 之前不做任何副作用**，这是 resume 不重复执行的关键：
        LangGraph resume 时会从 interrupt 所在节点从头重跑，
        节点内 interrupt 之前的代码会再次执行。
        审计写在 interrupt 返回之后（即审批确实发生之后）。
        """
        # 1) 缓存决策
        cached = self._decisions.get(pending.tool_call_id)
        if cached is not None:
            return cached

        # 2) 会话级授权
        if session_id and self.has_session_grant(session_id, pending.tool_name):
            decision = ApprovalDecision(
                tool_call_id=pending.tool_call_id,
                approved=True,
                actor=actor or "session-grant",
                reason="会话级授权（确认一次，本次会话内允许）",
                session_grant=True,
            )
            self._decisions[pending.tool_call_id] = decision
            return decision

        # 3) 显式审批结果
        if approved is not None:
            decision = ApprovalDecision(
                tool_call_id=pending.tool_call_id,
                approved=bool(approved),
                actor=actor or "unknown",
                reason=reason,
                session_grant=bool(session_grant),
            )
            self._decisions[pending.tool_call_id] = decision
            if decision.approved and session_grant and session_id:
                self.grant_session(session_id, pending.tool_name)
            self._audit_decision(pending, decision, session_id)
            return decision

        # 4) 挂起等待人工确认
        if use_interrupt:
            payload = self._interrupt(pending, session_id)
            decision = self._decision_from_payload(pending, payload)
            self._decisions[pending.tool_call_id] = decision
            if decision.approved and decision.session_grant and session_id:
                self.grant_session(session_id, pending.tool_name)
            self._audit_decision(pending, decision, session_id)
            return decision

        # 未启用 interrupt（如离线校验）→ 视为未决
        decision = ApprovalDecision(
            tool_call_id=pending.tool_call_id,
            approved=False,
            actor="",
            reason="等待人工审批",
        )
        self._decisions[pending.tool_call_id] = decision
        return decision

    # ------------------------------------------------------------------
    def _interrupt(self, pending: PendingApproval, session_id: Optional[str]) -> Any:
        """调用 LangGraph interrupt 挂起。

        payload 是前端所需的确认卡片信息；
        resume 时用 Command(resume=...) 回传审批结果，
        interrupt() 的返回值即为该 payload。
        """
        try:
            from langgraph.types import interrupt  # type: ignore
        except Exception as exc:  # pragma: no cover - 环境缺依赖
            raise RuntimeError(
                "未安装 langgraph，无法使用 HITL interrupt。"
                "请执行：pip install langgraph"
            ) from exc
        return interrupt({
            "type": "hitl_approval",
            "tool_call_id": pending.tool_call_id,
            "tool_name": pending.tool_name,
            "permission": pending.permission,
            "description": pending.description,
            "impact": pending.impact,
            "reversible": pending.reversible,
            "args": pending.args,
            "session_id": session_id,
            "created_at": pending.created_at,
            #: **向后兼容**：只有工作区写工具才带 diff，其余工具不带
            "metadata": pending.metadata,
            "diff": (pending.metadata or {}).get("diff"),
        })

    @staticmethod
    def _decision_from_payload(pending: PendingApproval, payload: Any) -> ApprovalDecision:
        """把 interrupt() 返回的 resume payload 解析为 ApprovalDecision。

        兼容的 payload 形态：
            {"tool_call_id": "...", "approved": true, "actor": "...", "reason": "..."}
            [{"tool_call_id": "...", ...}, ...]   （批量审批）
        """
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        if not isinstance(payload, dict):
            payload = {}
        cid = str(payload.get("tool_call_id") or pending.tool_call_id)
        return ApprovalDecision(
            tool_call_id=cid,
            approved=bool(payload.get("approved", False)),
            actor=str(payload.get("actor", "") or ""),
            reason=str(payload.get("reason", "") or ""),
            session_grant=bool(payload.get("session_grant", False)),
            #: 快速通道开关（2026-11-24 新增）
            summarize=bool(payload.get("summarize", False)),
        )

    # ------------------------------------------------------------------
    def _audit_decision(self, pending: PendingApproval, decision: ApprovalDecision,
                        session_id: Optional[str]) -> None:
        """HITL 决策进审计（docs/02 §5.3）。"""
        if self._audit is None:
            return
        try:
            self._audit.log(
                action="hitl_decision",
                target=pending.tool_name,
                args=pending.args,
                result=decision.to_dict(),
                status="approved" if decision.approved else "rejected",
                session_id=session_id,
                actor=decision.actor,
                extra={
                    "permission": pending.permission,
                    "impact": pending.impact,
                    "reversible": pending.reversible,
                    "session_grant": decision.session_grant,
                },
            )
        except Exception:
            pass
