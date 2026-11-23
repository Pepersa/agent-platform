# -*- coding: utf-8 -*-
"""verify_model_routing.py —— 模型路由与「降级保护」回归脚本。

对应设计文档：
    《06_代码模型接入策略.md》§4 降级保护机制 / §7 验证

本脚本**不需要启动 API 服务**，也不需要模型在线（纯静态校验 + 内存内构造）。
它验证三件事：

    A. **路由表完整性**
       · ``config/routing.yaml`` 的 ``routes:`` 与代码内 ``DEFAULT_ROUTES``
         的路由项**一致**（yaml 优先，但要能看出漂移）
       · 每个路由项的降级链**非空**
       · 降级链里的每个模型都在 ``models:`` 段**已登记**

    B. **能力标签正确性**
       · 每个模型的 ``capabilities`` 都是**合法标签**（在 ``CAPABILITIES`` 内）
       · ``gpt-oss-20b`` 必须具备 ``code`` / ``tool_call`` / ``reasoning``
       · ``qwen2.5-14b``（通用对话模型）**刻意不给** ``code``
       · 每个路由项声明的 ``requires`` 都是合法标签

    C. **fallback 链不含违规降级**
       · 对每个有能力要求的路由项，链中**不允许**出现不具备所需能力的模型
       · 能力校验后的「有效链」必须非空（否则该路由项永远无法调用）
       · 端到端行为：把一个「缺能力」的模型塞进链里，路由器**必须**
         剔除它；把整条链都换成缺能力的模型，路由器**必须显式报错**
         （而不是静默降级）

用法：
    python scripts/verify_model_routing.py
    python scripts/verify_model_routing.py --routing config/routing.yaml
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

PASS = 0
FAIL = 0
FAILURES: List[str] = []


def ok(name: str, detail: str = "") -> None:
    global PASS
    PASS += 1
    print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))


def bad(name: str, detail: str = "") -> None:
    global FAIL
    FAIL += 1
    FAILURES.append(f"{name}: {detail}")
    print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def check(cond: bool, name: str, detail: str = "") -> bool:
    if cond:
        ok(name, detail)
    else:
        bad(name, detail)
    return bool(cond)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ----------------------------------------------------------------------
# 加载
# ----------------------------------------------------------------------
def load_router(routing_path: str) -> Any:
    """构造一个加载了 routing.yaml 的 LLMRouter。"""
    from agent_platform.core.llm_client import LLMRouter  # type: ignore
    return LLMRouter(routing_file=routing_path)


def load_yaml_raw(routing_path: str) -> Dict[str, Any]:
    """直接读 yaml 原文（拿到 ``requires:`` 声明）。"""
    import yaml  # type: ignore
    with open(routing_path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# ======================================================================
# A. 路由表完整性
# ======================================================================
def test_route_completeness(router: Any, raw: Dict[str, Any]) -> None:
    section("A. 路由表完整性")

    from agent_platform.core.llm_client import DEFAULT_ROUTES  # type: ignore

    yaml_routes = raw.get("routes") or {}
    code_routes = raw.get("models") or {}

    # --- A1. yaml 与代码内 DEFAULT_ROUTES 的路由项一致 ---
    yaml_keys = set(yaml_routes.keys())
    code_keys = set(DEFAULT_ROUTES.keys())
    check(yaml_keys == code_keys,
          "routing.yaml 的 routes 键与 DEFAULT_ROUTES 完全一致",
          f"yaml-only={sorted(yaml_keys - code_keys)} "
          f"code-only={sorted(code_keys - yaml_keys)}")

    # --- A2. 每个路由项的降级链非空 ---
    for task_type in sorted(yaml_keys):
        entry = yaml_routes.get(task_type) or {}
        chain = entry.get("fallback") or entry.get("models") or []
        check(isinstance(chain, list) and len(chain) > 0,
              f"路由 '{task_type}' 的降级链非空", str(chain))

    # --- A3. 降级链里的每个模型都已登记 ---
    for task_type in sorted(yaml_keys):
        entry = yaml_routes.get(task_type) or {}
        chain = entry.get("fallback") or entry.get("models") or []
        for model_name in chain:
            check(str(model_name) in code_routes,
                  f"路由 '{task_type}' 的模型 '{model_name}' 已在 models: 段登记",
                  "")

    # --- A4. 关键路由项存在 ---
    for required_task in ("code", "agent", "tool_call", "ask", "plan", "default"):
        check(required_task in yaml_keys,
              f"路由项 '{required_task}' 存在", "")

    # --- A5. 既有测试依赖的两个模型仍在（verify_modes.py 断言） ---
    for required_model in ("gpt-oss-20b", "qwen2.5-14b"):
        check(required_model in code_routes,
              f"模型 '{required_model}' 仍在 models: 段（verify_modes 依赖）", "")

    # --- A6. 路由表实际被路由器加载到了 ---
    check(set(router.routes.keys()) == yaml_keys,
          "LLMRouter 实际加载到的 routes 与 yaml 一致",
          f"loaded={sorted(router.routes.keys())}")


# ======================================================================
# B. 能力标签正确性
# ======================================================================
def test_capabilities(router: Any, raw: Dict[str, Any]) -> None:
    section("B. 能力标签正确性")

    from agent_platform.core.llm_client import CAPABILITIES  # type: ignore

    yaml_models = raw.get("models") or {}

    # --- B1. 所有能力标签都合法 ---
    for name in sorted(yaml_models):
        conf = yaml_models.get(name) or {}
        caps = conf.get("capabilities") or []
        illegal = [c for c in caps if c not in CAPABILITIES]
        check(not illegal,
              f"模型 '{name}' 的 capabilities 全是合法标签",
              f"caps={caps} illegal={illegal}")

    # --- B2. gpt-oss-20b 必须具备核心能力 ---
    caps = router.capabilities_for("gpt-oss-20b")
    for cap in ("code", "tool_call", "reasoning"):
        check(cap in caps, f"gpt-oss-20b 具备 '{cap}' 能力", str(caps))

    # --- B3. qwen2.5-14b（通用对话模型）刻意不给 code ---
    caps14 = router.capabilities_for("qwen2.5-14b")
    check("code" not in caps14,
          "qwen2.5-14b **不具备** code 能力（通用对话模型，不是 Coder）",
          str(caps14))

    # --- B4. code 模型具备 code 能力 ---
    coder_caps = router.capabilities_for("qwen2.5-coder-14b")
    check("code" in coder_caps,
          "qwen2.5-coder-14b 具备 'code' 能力", str(coder_caps))

    # --- B5. 每个路由项的 requires 都是合法标签 ---
    yaml_routes = raw.get("routes") or {}
    for task_type in sorted(yaml_routes):
        entry = yaml_routes.get(task_type) or {}
        req = entry.get("requires") or []
        illegal = [c for c in req if c not in CAPABILITIES]
        check(not illegal,
              f"路由 '{task_type}' 的 requires 全是合法标签",
              f"requires={req} illegal={illegal}")
        check(req == router.required_capabilities(task_type),
              f"路由 '{task_type}' 的 requires 与路由器解析一致",
              f"yaml={req} router={router.required_capabilities(task_type)}")


# ======================================================================
# C. fallback 链不含违规降级
# ======================================================================
def test_no_illegal_fallback(router: Any, raw: Dict[str, Any]) -> None:
    section("C. fallback 链不含违规降级")

    # --- C1. 静态自检：无违规 ---
    violations = router.validate_routes()
    check(not violations,
          "所有路由项的降级链都不含「缺能力」的模型",
          f"violations={violations}")

    # --- C2. 能力校验后的有效链非空 ---
    for task_type in sorted(router.routes):
        req = router.required_capabilities(task_type)
        if not req:
            continue
        eff = router.filtered_chain_for(task_type)
        check(len(eff) > 0,
              f"路由 '{task_type}' 经能力校验后的有效链非空",
              f"requires={req} effective={eff}")

    # --- C3. 关键边界：代码类路由的链里不得出现 qwen2.5-14b ---
    for task_type in ("code", "agent", "tool_call"):
        chain = router.chain_for(task_type)
        check("qwen2.5-14b" not in chain,
              f"路由 '{task_type}' 的降级链不含 qwen2.5-14b（通用对话模型）",
              str(chain))

    # --- C4. 端到端：把缺能力的模型塞进链 → 路由器必须剔除它 ---
    from agent_platform.core.llm_client import LLMRouter  # type: ignore
    r2 = LLMRouter(routing_file="__nonexistent__")  # 不加载 yaml，用默认表
    r2.routes["code"] = ["qwen2.5-14b", "gpt-oss-20b"]
    eff = r2.filtered_chain_for("code")
    check("qwen2.5-14b" not in eff and "gpt-oss-20b" in eff,
          "缺能力的 qwen2.5-14b 被能力校验剔除", f"effective={eff}")

    # --- C5. 端到端：整条链都缺能力 → 必须**显式报错**，不静默降级 ---
    from agent_platform.core.llm_client import RoutingCapabilityError  # type: ignore
    r3 = LLMRouter(routing_file="__nonexistent__")
    r3.routes["code"] = ["qwen2.5-14b"]
    raised = False
    msg = ""
    try:
        r3.route("code")
    except RoutingCapabilityError as exc:
        raised = True
        msg = str(exc)
    except Exception as exc:  # pragma: no cover - 不该走到这
        msg = f"{type(exc).__name__}: {exc}"
    check(raised,
          "整条链都缺能力时 route() 抛 RoutingCapabilityError（显式失败）",
          msg[:120])

    # --- C6. 同步版 chat() 也必须拒绝降级 ---
    raised2 = False
    try:
        import asyncio  # noqa: PLC0415
        asyncio.run(r3.chat("code", [{"role": "user", "content": "hi"}]))
    except RoutingCapabilityError as exc:
        raised2 = True
        msg2 = str(exc)
    except Exception as exc:  # pragma: no cover
        msg2 = f"{type(exc).__name__}: {exc}"
    check(raised2,
          "整条链都缺能力时 chat() 也抛 RoutingCapabilityError（不调模型）",
          msg2[:120])

    # --- C7. 默认路由（无能力约束）仍可正常取到主模型 ---
    client = r3.route("default")
    check(getattr(client, "config", None) is not None,
          "default 路由（无能力约束）仍能正常取到主模型客户端",
          getattr(getattr(client, "config", None), "model", ""))


# ======================================================================
# D. 注册表暴露能力标签
# ======================================================================
def test_registry_exposes_capabilities(routing_path: str) -> None:
    section("D. 模型注册表暴露能力标签")

    try:
        from agent_platform.core.model_registry import ModelRegistry  # type: ignore
    except Exception as exc:  # pragma: no cover
        bad("导入 ModelRegistry", str(exc))
        return

    try:
        reg = ModelRegistry(routing_file=routing_path)
    except Exception as exc:  # pragma: no cover
        bad("构造 ModelRegistry", str(exc))
        return

    models = reg.all_models()
    names = [m.get("name") for m in models]
    check("gpt-oss-20b" in names, "注册表包含 gpt-oss-20b", str(names))
    check("qwen2.5-14b" in names, "注册表包含 qwen2.5-14b", str(names))

    by_name = {m.get("name"): m for m in models}
    gpt = by_name.get("gpt-oss-20b") or {}
    check("code" in (gpt.get("capabilities") or []),
          "注册表里 gpt-oss-20b 带 capabilities 且含 code",
          str(gpt.get("capabilities")))
    qwen = by_name.get("qwen2.5-14b") or {}
    check("code" not in (qwen.get("capabilities") or []),
          "注册表里 qwen2.5-14b 的 capabilities 不含 code",
          str(qwen.get("capabilities")))

    # 占位的 code 模型应带 installed=false 标记
    coder = by_name.get("qwen2.5-coder-14b") or {}
    check("capabilities" in coder,
          "注册表里 code 模型条目带 capabilities 字段",
          str(coder.get("capabilities")))
    check(coder.get("installed") is False,
          "占位的 code 模型标记 installed=false（如实告知未安装）",
          str(coder.get("installed")))


# ======================================================================
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--routing", default=os.path.join(REPO_ROOT, "config", "routing.yaml"))
    args = ap.parse_args()
    routing_path = args.routing

    print("=" * 70)
    print("模型路由与降级保护回归（verify_model_routing.py）")
    print("=" * 70)
    print(f"routing 配置：{routing_path}")

    if not os.path.isfile(routing_path):
        print(f"[FAIL] 找不到 routing 配置：{routing_path}")
        return 1

    try:
        raw = load_yaml_raw(routing_path)
    except Exception as exc:
        print(f"[FAIL] 解析 routing 配置失败：{exc}")
        return 1

    try:
        router = load_router(routing_path)
    except Exception as exc:
        print(f"[FAIL] 构造 LLMRouter 失败：{exc}")
        return 1

    test_route_completeness(router, raw)
    test_capabilities(router, raw)
    test_no_illegal_fallback(router, raw)
    test_registry_exposes_capabilities(routing_path)

    print("\n" + "=" * 70)
    print(f"通过 {PASS} / {PASS + FAIL}")
    if FAILURES:
        print("\n失败项：")
        for f in FAILURES:
            print(f"  - {f}")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
