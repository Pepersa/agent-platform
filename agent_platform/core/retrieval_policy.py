# -*- coding: utf-8 -*-
"""检索必要性判定（Retrieval Necessity Policy）。

背景
----
用户反馈：**问通用问题（如"你是什么模型"）时，Agent 仍然强制调用 ``kb_search``**，
既浪费 20 秒，又把回答搞砸成"未在知识库中找到"。

根因不在模型能力，而在于：
    1. 系统提示词只写了"涉及参数/限值必须检索"，**没写"什么情况不需要检索"**；
    2. Ask/Agent 模式无条件把 ``kb_search`` / ``kb_read`` / ``tp_lookup``
       暴露给模型，模型想调就能调。

本模块提供一个**轻量、零依赖、可解释**的判定器：把用户问题分成三类。

    · **A 类 · 事实/参数类** —— 答案取决于用户上传的文档
      （限值、参数、定义、告警码、手册规程、统计数值）
      → **必须**先检索查证，工具照常提供。
    · **B 类 · 通用/常识类** —— 答案不取决于文档
      （你是谁、你是什么模型、能做什么、写个函数、翻译、总结刚才的话）
      → **禁止**为此调用检索工具；本轮**不提供**检索工具。
    · **C 类 · 闲聊/元问题** —— 你好、谢谢、再见
      → 简短回应，同样不检索。

判定标准（一句话）
----------------
    **答案是否取决于用户上传的文档？** 取决于 → 先检索；不取决于 → 直接回答。

为什么用规则而不是分类器模型
----------------------------
    1. **零依赖**：不引入 spacy / 分类器模型 / 向量判别器，符合项目
       "不引入新第三方依赖"的硬约束，也避免为一个二分类问题再背一份模型权重。
    2. **可解释**：每次判定都带**命中的规则名**（如 ``identity_question``），
       能进 SSE、能进前端、能被用户核对。分类器模型只给一个 0/1，出了错没法归因。
    3. **延迟**：规则判定是纯正则 + 字符串运算，**微秒级**；分类器要多一次
       前向推理（本地 CPU 端点动辄几百 ms 到数秒），而这一步在**每一轮**请求
       的最前面执行，直接决定是否下发工具 schema。
    4. **本机资源约束**：本项目是本地 llama-server（8080/8081）+ BGE-M3 的
       CPU/GPU 混合部署，再加一个判别模型会挤占推理资源。
    5. **确定性**：同一个问题每次判定结果一致，回归测试可断言；
       分类器存在抖动，A 类问题偶发漏检会让"必须检索"的严格性塌掉。

**已知局限**（诚实记录）：
    · 规则是**词面**的，依赖关键词覆盖；见 ``KNOWN_LIMITATIONS``。
    · "是否依赖文档"本身有主观性，边界案例（如"我们公司的年假制度"）
      只能靠文档清单命中或让用户在问题里点名文档。
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Tuple

__all__ = [
    "RetrievalDecision",
    "RETRIEVAL_TOOL_NAMES",
    "classify_retrieval_need",
    "retrieval_needed",
    "filter_retrieval_tools",
    "RETRIEVAL_POLICY_VERSION",
    "KNOWN_LIMITATIONS",
]


#: 检索必要性策略版本（写入 SSE，便于回溯"当时用的哪套规则"）
RETRIEVAL_POLICY_VERSION = "rp_v1.0"


#: 检索类工具名（判定为"不需要检索"时，本轮把这些工具从 schema 里摘掉）
RETRIEVAL_TOOL_NAMES = ("kb_search", "kb_read", "tp_lookup", "spec_extract")


#: 命中规则 → 该规则对应的类别
#:   ``"generic"``（B 类，通用/常识） / ``"chitchat"``（C 类，闲聊） / ``""``（未命中 → A 类默认）
_RULES: Tuple[Tuple[str, "re.Pattern[str]", str], ...] = (
    # ---- C 类：闲聊 / 纯元问题 ----
    ("chitchat_greeting", re.compile(
        r"^\s*(你好|您好|哈喽|嗨|hi|hello|hey|早上好|下午好|晚上好|在吗|有人吗"
        r"|谢谢|多谢|感谢|辛苦了|再见|拜拜|bye|goodbye|ok|好的|收到|嗯|额)\s*[。.!！?？~]*$",
        re.IGNORECASE), "chitchat"),
    ("chitchat_thanks", re.compile(
        r"(谢谢|多谢|感谢你|辛苦了|感谢|thanks|thank you)", re.IGNORECASE), "chitchat"),

    # ---- B 类：身份 / 能力 ----
    ("identity_question", re.compile(
        r"(你是什么模型|你是哪个模型|你是什么大模型|你的模型(是什么|叫什么|名字)"
        r"|你是谁|你叫什么|你的名字(是什么|叫什么)"
        r"|你是gpt|你是chatgpt|你是claude|你是deepseek|你是qwen|你是llama|你是grok"
        r"|what model are you|which model are you|who are you|your name"
        r"|你基于什么模型|你用的什么模型)", re.IGNORECASE), "generic"),
    ("capability_question", re.compile(
        r"(你能做什么|你能干什么|你会做什么|你有什么功能|你支持什么|你有哪些功能"
        r"|你能帮我做什么|你有哪些能力|你擅长什么|怎么用你|如何使用你|怎么使用你"
        r"|你能做什么用|你的能力(是什么|有哪些)|what can you do|how do i use you"
        r"|你能上网吗|你能联网吗|你的知识截止到什么时候)", re.IGNORECASE), "generic"),
    ("self_referential", re.compile(
        r"(你会(觉得|认为|喜欢|讨厌)吗|你有(感情|情绪|意识)吗|你会死吗"
        r"|你有意识吗)"), "generic"),

    # ---- B 类：明确的通用编程 / 写作请求（不依赖文档）----
    ("generic_coding", re.compile(
        r"(写|实现|生成|来|给).{0,12}(一个|一段|个)?\s*"
        r"(python|java|javascript|js|typescript|ts|golang|go|rust|c\+\+|c语言"
        r"|sql|shell|bash|正则|正则表达式|函数|方法|脚本|算法|单链表|链表|二叉树"
        r"|快排|快速排序|排序算法|二分查找|斐波那契)"
        r"\s*(函数|方法|实现|代码|脚本|程序|算法|示例|例子|吧|的代码|)"), "generic"),
    ("generic_writing", re.compile(
        r"(翻译|译成|translate|润色|改写|缩写|扩写|写一封|写一份|写个邮件"
        r"|写封邮件|写个周报|写一份周报|总结一下|总结这段|概括一下|起个标题"
        r"|写首|作诗|写诗|取个名字)"), "generic"),
    ("explain_concept", re.compile(
        r"(什么是|什么是意思|是什么意思|怎么理解|解释一下|介绍一下|讲讲"
        r"|为什么(是|会|能)|原理是什么|区别是什么|有什么区别)"
        r"[\s\S]{0,30}?"
        r"(算法|数据结构|设计模式|闭包|协程|泛型|指针|引用|递归|哈希|排序"
        r"|二分|堆|栈|队列|网络|协议|线程|进程|虚拟内存|垃圾回收|编译|链接"
        r"|面向对象|函数式|多态|继承|封装|接口|抽象类|微服务|分布式|容器|docker"
        r"|k8s|kubernetes|数据库索引|事务|锁|并发|async|await|promise|装饰器"
        r"|元类|生成器|迭代器|上下文管理器|垃圾回收|性能优化|缓存)"), "generic"),
)


#: 明确的"要查知识库"信号 —— 命中即**强制**判定为需要检索（覆盖 B 类误伤）
_FORCE_RETRIEVAL = re.compile(
    r"(知识库|文档里|文档中|规格书|规格文档|手册|说明书|规范|规程|流程图"
    r"|我们的(资料|文件|文档|制度|规定)|上传的|这份|这本|附件|清单表"
    r"|tp[_ ]?清单|TP清单|数据表|参数表|lims|sop|wiki|readme)",
    re.IGNORECASE,
)

#: **代码类问题** —— 提到「某个源码文件 + 读/改/找」时属于 B 类
#:
#: 背景：工作区模块上线后，用户会问「读一下 tool_registry.py 的前 30 行」。
#: 这种问题里常出现下划线文件名（如 ``tool_registry.py``），会被
#: ``_ENTITY_ANCHOR``（``XXX_YYY`` 形态）误判成"知识库实体"从而强制检索 ——
#: 但代码问题的答案**完全不取决于用户上传的文档**，检索知识库是纯粹的浪费。
#:
#: 因此这里加一条**专用规则**，命中即判为 B 类（不检索）。
#:
#: 触发条件（三者同时满足，避免误伤）：
#:   1. 问题里出现**带代码扩展名的文件名**（``*.py`` / ``*.js`` / ``*.ts`` …）；
#:   2. 问题里出现**代码操作动词**（读/看/打开/改/写/搜索/查找/列出…）；
#:   3. 问题里**没有**"知识库/文档/规格书"等强制检索信号
#:      （该信号在判定顺序里更靠前，命中即强制检索）。
#:
#: ⚠️ 判定顺序很重要：本规则排在 ``_FACTUAL`` **之后**、
#:    ``_ENTITY_ANCHOR`` **之前** —— 既保住"A 类事实问题必须检索"的严格性，
#:    又避免代码文件名被当成知识库实体。
_CODE_FILE_RE = re.compile(
    r"[A-Za-z0-9_\-./\\]*[A-Za-z0-9_\-]\."
    r"(py|pyi|js|jsx|ts|tsx|json|yaml|yml|toml|ini|cfg|md|txt|html|htm|css|scss"
    r"|sh|bash|ps1|bat|sql|xml|go|rs|java|c|cpp|h|cs|kt|swift|lua|r|vue)\b",
    re.IGNORECASE,
)
_CODE_ACTION_RE = re.compile(
    r"(读|看|打开|浏览|查看|改|修改|编辑|写|新增|删除|重命名|搜索|查找|检索|列出"
    r"|定位|找到|看看|读一下|读下|改一下|帮忙改|帮我改|整理|format|重构|refactor"
    r"|read|open|edit|modify|write|search|find|list|show|view|update|refactor)",
    re.IGNORECASE,
)
#: **代码目录路径**（含斜杠的路径式 token，如 ``agent_platform/core``）
#:
#: 与 :data:`_CODE_FILE_RE` 互补：目录名没有扩展名，但同样是"代码工作区"语境。
#: 要求路径里**至少有一段带下划线或全小写单词**，降低误伤概率。
_CODE_PATH_RE = re.compile(
    r"(?<![\w])[A-Za-z0-9_\-]+/[A-Za-z0-9_\-./]*[A-Za-z0-9_\-]"
    r"(?![\w])"
)

#: 明确的 A 类信号 —— 命中即**强制**判定为需要检索
#: 注意：这里刻意要求"实体 + 事实属性"或显式的指标/代码字样，避免把
#: "写个 Python 函数"里的泛化词误判成事实类。
_FACTUAL = re.compile(
    r"(上下限|上限|下限|最大(值)?|最小(值)?|典型值|额定值|精度|分辨率"
    r"|告警码|错误码|故障码|报警码|error\s*code|alarm\s*code"
    r"|默认值|参数值|阈值|门限|限值|规格|指标值"
    r"|标准要求|规程要求|步骤是|流程是|哪几步|第几步"
    r"|有多少|数量是|统计|占比|良率|通过率|分布|汇总|总数|多少条|多少个"
    r"|是哪(个|条|种|类|项)|叫什么名字|怎么定义|定义是什么|含义是什么"
    r"|sop|stdf|std|stp|规格要求|测试项)",
    re.IGNORECASE,
)

#: 明确的"实体锚点" —— 形如 ``XXX_YYY_ZZZ`` / ``TP-123`` / ``DOC-001``
#: 这类 token 几乎必然指向知识库里的某个具体条目。
#: ⚠️ 必须包含数字或下划线分段，否则 ``ChatGPT`` / ``Kubernetes`` 这类普通
#:    驼峰词会被误判成知识库实体（"你是ChatGPT吗"就会被错误地强制检索）。
_ENTITY_ANCHOR = re.compile(
    r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+){1,}\b"      # 至少一个下划线分段
    r"|\b[A-Z]{2,}[-_][0-9][0-9A-Za-z]*\b"                  # TP-001 / SPEC-77
    r"|\b[A-Z]{2,}_[A-Z0-9]{2,}\b"                         # T101_VDD_FUNCT
)


#: 已知限制（供文档 / 前端提示 / 测试引用）
KNOWN_LIMITATIONS: Tuple[str, ...] = (
    "规则是词面匹配：换一种完全不同的说法描述同一件事，可能落到另一类。",
    "'是否依赖文档'本身有主观性：通用制度类问题（如年假制度）"
    "既像闲聊又像文档事实，除非用户点名文档，否则默认按 A 类（走检索）。",
    "B 类通用编程/写作请求若用户确实想引用知识库，需在问题里点名文档"
    "（命中 _FORCE_RETRIEVAL 即可覆盖）。",
    "A 类问题被误判为 B 类会导致'该检索却没检索'；"
    "缓解手段是 _FORCE_RETRIEVAL / _FACTUAL / _ENTITY_ANCHOR 三路强制召回，"
    "且未知问题一律默认 A 类（默认走检索）。",
    "代码类规则（code_workspace_question）只认「带扩展名的文件名 + 代码操作动词」，"
    "若用户用自然语言指代代码文件（如「那个注册表文件」）而不写扩展名，"
    "会落到 A 类默认（走检索）——这是保守方向，不会漏检索。",
)


class RetrievalDecision:
    """检索必要性判定结果。"""

    __slots__ = ("needed", "rule", "reason", "question", "policy_version")

    def __init__(self, needed: bool, rule: str, reason: str,
                 question: str = "", policy_version: str = RETRIEVAL_POLICY_VERSION):
        self.needed = bool(needed)
        #: 命中的规则名（未命中任何规则时为空串 —— 表示落到 A 类默认）
        self.rule = rule
        #: 人类可读的理由（会进 SSE / 前端）
        self.reason = reason
        self.question = question
        self.policy_version = policy_version

    # -- 序列化 ------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "retrieval_needed": self.needed,
            "retrieval_rule": self.rule,
            "retrieval_reason": self.reason,
            "retrieval_policy_version": self.policy_version,
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"<RetrievalDecision needed={self.needed} "
                f"rule={self.rule!r} reason={self.reason!r}>")

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, RetrievalDecision):
            return NotImplemented
        return (self.needed == other.needed and self.rule == other.rule
                and self.reason == other.reason)

    # -- 便捷判定 ----------------------------------------------------
    @property
    def is_generic(self) -> bool:
        """是否判定为"不依赖知识库"（B / C 类）。"""
        return not self.needed


def _normalize(text: str) -> str:
    """归一化：全角空格 → 半角、压缩空白、去首尾空白。"""
    s = str(text or "")
    s = s.replace("\u3000", " ").replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def classify_retrieval_need(question: str) -> RetrievalDecision:
    """判定一个问题是否**必须**走知识库检索。

    判定顺序（先强制召回，再规则命中）：
        1. ``_FORCE_RETRIEVAL`` 命中 → 需要检索（用户明确提到知识库/文档/规格书）
        2. ``_FACTUAL`` 命中 或 ``_ENTITY_ANCHOR`` 命中 → 需要检索
        3. ``_RULES`` 命中 B/C 类 → **不需要**检索
        4. 兜底 → **需要**检索（A 类默认，宁可多查一次也不给错答案）

    Args:
        question: 用户本轮输入。

    Returns:
        ``RetrievalDecision``（带命中的规则名，便于观测）。
    """
    raw = str(question or "")
    q = _normalize(raw)

    if not q:
        return RetrievalDecision(
            True, "empty_question",
            "空问题按需检索处理（默认路径）", raw)

    # 1) 显式提到文档/知识库 → 强制检索
    if _FORCE_RETRIEVAL.search(q):
        return RetrievalDecision(
            True, "force_document_reference",
            "问题中显式提到知识库/文档/规格书/手册等内容，必须检索查证", raw)

    # 2) 事实类信号 → 强制检索
    if _FACTUAL.search(q):
        return RetrievalDecision(
            True, "force_factual_signal",
            "问题包含参数/限值/告警码/统计等事实类信号，必须检索查证", raw)

    # 2.5) **代码类问题** → B 类（不检索知识库）
    #
    # 判定条件：出现「带扩展名的文件名」+「代码操作动词」。
    # ⚠️ 必须排在 ``_ENTITY_ANCHOR`` **之前**：代码文件名（``tool_registry.py``）
    #    会被实体锚点规则误判成知识库实体而强制检索 —— 那纯属浪费。
    #    但排在 ``_FACTUAL`` **之后**：若问题同时带"限值/参数/告警码"等
    #    事实信号，仍按 A 类强制检索（A 类严格性不得削弱）。
    if (_CODE_FILE_RE.search(q) or _CODE_PATH_RE.search(q)) and _CODE_ACTION_RE.search(q):
        return RetrievalDecision(
            False, "code_workspace_question",
            "识别为代码工作区类问题（涉及源码文件或代码目录），"
            "答案不取决于知识库文档，应直接使用代码工具处理", raw)

    # 3) 实体锚点（TP_XXX / DOC-001）→ 强制检索
    if _ENTITY_ANCHOR.search(q):
        return RetrievalDecision(
            True, "force_entity_anchor",
            "问题包含疑似知识库实体标识（如 TP_XXX / DOC-001），必须检索查证", raw)

    # 4) B / C 类规则
    # ⚠️ ``AGENT_FORCE_LEGACY_RETRIEVAL=1`` 仅用于"改前 vs 改后"取证：
    #    还原 p_v1.0 的"什么都要检索"行为，方便复现用户报障的现象。
    #    正常运行不会设置该环境变量。
    if os.environ.get("AGENT_FORCE_LEGACY_RETRIEVAL") != "1":
        for name, pattern, kind in _RULES:
            if pattern.search(q):
                if kind == "chitchat":
                    return RetrievalDecision(
                        False, name,
                        "识别为寒暄/礼貌用语，直接回应即可，无需检索", raw)
                return RetrievalDecision(
                    False, name,
                    "识别为通用/常识类问题，答案不取决于用户上传的文档，直接作答", raw)

    # 5) 兜底：A 类默认
    return RetrievalDecision(
        True, "default_factual",
        "未命中通用问题规则，按事实类默认走检索", raw)


def retrieval_needed(question: str) -> bool:
    """便捷函数：只返回"是否需要检索"。"""
    return classify_retrieval_need(question).needed


def filter_retrieval_tools(tool_names: List[str], needed: bool) -> List[str]:
    """按判定结果过滤工具名列表。

    Args:
        tool_names: 本轮原本可用的全部工具名。
        needed: 是否需要检索。False 时**摘掉**检索类工具。

    Returns:
        过滤后的工具名列表（顺序保持不变）。
    """
    if needed:
        return list(tool_names)
    drop = set(RETRIEVAL_TOOL_NAMES)
    return [n for n in tool_names if n not in drop]


def explain(question: str) -> Dict[str, Any]:
    """返回可观测的判定详情（调试 / 文档用）。"""
    d = classify_retrieval_need(question)
    return {
        "question": question,
        **d.to_dict(),
        "policy_version": RETRIEVAL_POLICY_VERSION,
    }
