# -*- coding: utf-8 -*-
"""知识层扩展工具验证：验证 5 个工具为真实实现（非 mock）且契约对齐。

覆盖：
    1. 5 个函数全部返回真实结果（不含 ``"mock": True``）
    2. ``tp_lookup`` 的限值 / 单位 / 版本错误处理
    3. ``spec_extract`` 的参数值、单位归一、缺口报告
    4. ``case_lint`` 的 R01–R10 违规检出
    5. ``report_render`` / ``notify`` 的真实落盘产物
    6. ToolRegistry 注册 / ``openai_schemas()`` 导出 / HITL 标记 / ToolError 契约

运行：
    python scripts\\verify_tools.py
"""
from __future__ import annotations

import json
import os
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"
results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    """记录并打印一条检查结果。"""
    ok = bool(cond)
    results.append((name, ok, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f"  -- {detail}" if detail else ""))
    return ok


def _no_mock(obj: object) -> bool:
    """递归检查返回值中不含 mock 标记。"""
    if isinstance(obj, dict):
        if obj.get("mock") is True:
            return False
        return all(_no_mock(v) for v in obj.values())
    if isinstance(obj, list):
        return all(_no_mock(v) for v in obj)
    return True


# ---------------------------------------------------------------------------
# 刻意违规的用例集：缺单位 / typ 超上限 / 无溯源 / 缺温度 / 缺采样量
# ---------------------------------------------------------------------------
BAD_CASES = [
    {
        "case_id": "TC_BAD_VDD_001",
        "title": "VDD 电源功能测试（违规样例）",
        "preconditions": [
            {"type": "instrument", "value": "SMU 量程 ±5V, 精度 ±0.1%"},
            {"type": "fixture", "value": "探针卡 PC-07"},
        ],
        "test_steps": [
            {"step": 1, "action": "对 DUT 施加 VDD=3.3V", "params": {}},
        ],
        "test_items": [
            {
                "test_num": "T101_VDD_FUNCT",
                "name": "VDD 输出功能",
                # 无 unit（R02）
                "lower_limit": 3.135,
                "typical": 3.600,        # typ > upper（R03）
                "upper_limit": 3.465,
                "measure": "DC",
            }
        ],
        "expected_result": "大致正常",          # 模糊表述（R09）
        "pass_criteria": "基本符合规格",
        "fail_criteria": "",
        "covered_failure_modes": [],
        "traceability": {},                    # 无溯源（R01）
    }
]


def main() -> int:
    print("\n=== 1. 导入与解析（5 个函数均来自 knowledge 层真实实现）===")
    from agent_platform.core.tool_registry import ToolError, ToolRegistry
    from agent_platform.knowledge import (
        case_lint,
        notify,
        report_render,
        spec_extract,
        tp_lookup,
    )

    check("5 个函数可导入", all(callable(f) for f in
                               (tp_lookup, spec_extract, case_lint, report_render, notify)))

    # ---- tools 层的 duck typing 解析 ----
    from agent_platform.tools import _resolve

    resolved = {n: _resolve(n) for n in
                ("tp_lookup", "spec_extract", "case_lint", "report_render", "notify")}
    check("tools._resolve() 全部命中真实函数",
          all(fn is not None for fn in resolved.values()),
          str({k: (getattr(v, "__module__", None) if v else None) for k, v in resolved.items()}))

    print("\n=== 2. tp_lookup（真实 TP 清单数据）===")
    tp = tp_lookup("TP_CMOS_32G", test_num="T101_VDD_FUNCT")
    check("tp_lookup 返回非 mock", _no_mock(tp))
    check("tp_lookup 返回 tp_id/version",
          tp.get("tp_id") == "TP_CMOS_32G" and tp.get("version") == "v2.4",
          f"tp_id={tp.get('tp_id')} version={tp.get('version')}")
    items = tp.get("items") or []
    check("tp_lookup 命中 1 条测试项", len(items) == 1, f"items={len(items)}")
    it = items[0] if items else {}
    check("T101_VDD_FUNCT lower=3.135", it.get("lower") == 3.135, f"lower={it.get('lower')}")
    check("T101_VDD_FUNCT upper=3.465", it.get("upper") == 3.465, f"upper={it.get('upper')}")
    check("T101_VDD_FUNCT unit=V", it.get("unit") == "V", f"unit={it.get('unit')}")
    check("tp_lookup 带原文溯源 quote",
          bool(it.get("source", {}).get("quote")),
          (it.get("source", {}) or {}).get("quote", "")[:60])

    # keyword 模糊查
    tp_kw = tp_lookup("TP_CMOS_32G", keyword="漏电")
    check("tp_lookup keyword 模糊查命中多条", len(tp_kw.get("items") or []) >= 2,
          f"命中 {len(tp_kw.get('items') or [])} 条")

    # 版本不匹配 → 明确错误
    try:
        tp_lookup("TP_CMOS_32G", tp_version="v1.0", test_num="T101_VDD_FUNCT")
        check("tp_lookup 版本不匹配报错", False, "未抛异常")
    except ToolError as e:
        check("tp_lookup 版本不匹配报错", e.code == "VERSION_MISMATCH",
              f"code={e.code} message={e.message}")

    # 不存在的 test_num
    try:
        tp_lookup("TP_CMOS_32G", test_num="T999_NOT_EXIST")
        check("tp_lookup 不存在 test_num 报错", False, "未抛异常")
    except ToolError as e:
        check("tp_lookup 不存在 test_num 报错", e.code == "NOT_FOUND", f"code={e.code}")

    print("\n=== 3. spec_extract（真实规格书数据 + 单位归一 + 缺口）===")
    spec = spec_extract("SPEC-77", param_codes=["VDD", "R_CONTACT"])
    check("spec_extract 返回非 mock", _no_mock(spec))
    params = {p["param_code"]: p for p in (spec.get("params") or [])}
    vdd = params.get("VDD", {})
    check("VDD min=3.135", vdd.get("min") == 3.135, f"min={vdd.get('min')}")
    check("VDD typ=3.300", vdd.get("typ") == 3.3, f"typ={vdd.get('typ')}")
    check("VDD max=3.465", vdd.get("max") == 3.465, f"max={vdd.get('max')}")
    check("VDD unit=V", vdd.get("unit") == "V", f"unit={vdd.get('unit')}")
    check("VDD 带 source.quote 原文溯源",
          "VDD" in str(vdd.get("source", {}).get("quote", "")),
          (vdd.get("source", {}) or {}).get("quote", "")[:70])
    rc = params.get("R_CONTACT", {})
    check("R_CONTACT 单位归一 mΩ→Ω", rc.get("unit") == "Ω", f"unit={rc.get('unit')}")
    check("R_CONTACT max=0.5（500 mΩ）", rc.get("max") == 0.5, f"max={rc.get('max')}")
    check("R_CONTACT 保留 raw_value",
          (rc.get("raw_value") or {}).get("unit") == "mΩ",
          f"raw_value={rc.get('raw_value')}")
    gaps = spec.get("gaps") or []
    check("R_CONTACT 进入 gaps", any("R_CONTACT" in g for g in gaps),
          " | ".join(gaps))
    check("gaps 文本含「未给典型值」",
          any("未给典型值" in g for g in gaps if "R_CONTACT" in g),
          " | ".join(gaps))

    # 全量抽取：3 个规格缺口都要显式报告
    spec_all = spec_extract("SPEC-77")
    all_gaps = " ".join(spec_all.get("gaps") or [])
    check("全量抽取报出 R_CONTACT/T_PERIOD/IO_LEAK_P 三项缺口",
          all(k in all_gaps for k in ("R_CONTACT", "T_PERIOD", "IO_LEAK_P")),
          all_gaps)

    # 单位归一：uA → A
    leak = {p["param_code"]: p for p in (spec_all.get("params") or [])}
    io_leak = leak.get("IO_LEAK_P", {})
    check("IO_LEAK_P 单位归一 uA→A", io_leak.get("unit") == "A", f"unit={io_leak.get('unit')}")
    check("IO_LEAK_P max 归一后=1e-06", io_leak.get("max") == 1e-06,
          f"max={io_leak.get('max')} raw={io_leak.get('raw_value')}")

    print("\n=== 4. case_lint（R01–R10 违规检出）===")
    lint = case_lint(BAD_CASES)
    check("case_lint 返回非 mock", _no_mock(lint))
    report = lint.get("report") or []
    rule_ids = [r["rule_id"] for r in report]
    print(f"      检出规则：{sorted(set(rule_ids))}")
    for r in report:
        print(f"      · [{r['level']}] {r['rule_id']} {r['message']}")
    for want in ("R01", "R02", "R03", "R05", "R06"):
        check(f"检出 {want} 违规", want in rule_ids,
              next((r["message"] for r in report if r["rule_id"] == want), ""))
    check("report 条目字段完整（case_id/rule_id/level/message/evidence）",
          all(all(k in r for k in ("case_id", "rule_id", "level", "message", "evidence"))
              for r in report))
    summary = lint.get("summary") or {}
    check("summary 含 pass_rate 与 coverage_matrix",
          "pass_rate" in summary and "coverage_matrix" in summary,
          f"pass_rate={summary.get('pass_rate')}")
    check("coverage_matrix 覆盖 R01–R10",
          all(f"R{i:02d}" in (summary.get("coverage_matrix") or {}) for i in range(1, 11)),
          f"覆盖 {len(summary.get('coverage_matrix') or {})} 条规则")

    # 合规用例不应报错（除已知的 R04/R07 等软项）
    good_case = {
        "case_id": "TC_GOOD_VDD_001",
        "title": "VDD 电源功能测试（合规样例）",
        "preconditions": [
            {"type": "environment", "value": "T=25±3℃, Humi=30~70%RH"},
            {"type": "sample", "value": "lot>=3, 每 lot wafer>=3, 每 wafer die>=25"},
        ],
        "test_steps": [
            {"step": 1, "action": "施加 VDD=3.3V, 等待 10ms", "params": {"VDD": "3.3V"}},
        ],
        "test_items": [
            {"test_num": "T101_VDD_FUNCT", "name": "VDD 输出功能", "unit": "V",
             "lower_limit": 3.135, "typical": 3.3, "upper_limit": 3.465},
        ],
        "expected_result": "DOUT 输出 3.3V±5%，判定 PASS",
        "pass_criteria": "全部 test_item 判定 PASS",
        "fail_criteria": "任一 test_item 判定 FAIL",
        "covered_failure_modes": [{"mode_id": "FM-012", "mode": "VDD 上电后无输出"}],
        "traceability": {
            "spec_ref": [{"doc_id": "SPEC-77", "page": 12,
                          "quote": "VDD 3.135 3.300 3.465 V"}],
            "tp_ref": {"tp_name": "TP_CMOS_32G", "tp_version": "v2.4"},
        },
    }
    lint_good = case_lint([good_case])
    good_ids = [r["rule_id"] for r in (lint_good.get("report") or [])]
    check("合规用例无 R01/R02/R03/R05/R06/R09/R10 报错",
          not ({"R01", "R02", "R03", "R05", "R06", "R09", "R10"} & set(good_ids)),
          f"检出 {sorted(set(good_ids))}")

    # R10：与 TP 不一致
    mismatch = json.loads(json.dumps(good_case))
    mismatch["case_id"] = "TC_MISMATCH_001"
    mismatch["test_items"][0]["upper_limit"] = 3.500
    lint_mis = case_lint([mismatch], rules=["R10"])
    check("R10 检出与 TP 不一致",
          any(r["rule_id"] == "R10" for r in (lint_mis.get("report") or [])),
          "; ".join(r["message"] for r in (lint_mis.get("report") or [])))

    # R08：与历史用例重复
    hist = json.loads(json.dumps(good_case))
    hist["case_id"] = "TC_HIST_001"
    lint_dup = case_lint([good_case], rules=["R08"], compare_history=True, history=[hist])
    check("R08 检出与历史用例重复",
          any(r["rule_id"] == "R08" for r in (lint_dup.get("report") or [])),
          "; ".join(r["message"] for r in (lint_dup.get("report") or [])))

    print("\n=== 5. report_render / notify（真实落盘产物）===")
    rep = report_render(
        "daily",
        {
            "title": "L1 良率日报",
            "date": "2026-11-21",
            "scope": "L1 / CIS32G",
            "summary": "bin1 良率 99.2%，无异常。",
            "metrics": [{"name": "bin1 良率", "value": "99.2", "unit": "%"}],
            "conclusion": "各项指标正常。",
            "evidence": ["SPEC-77 p12 Table 3-2"],
        },
    )
    check("report_render 返回非 mock", _no_mock(rep))
    check("report_render 返回 report_id/uri/preview_md",
          all(rep.get(k) for k in ("report_id", "uri", "preview_md")),
          f"report_id={rep.get('report_id')} uri={rep.get('uri')}")
    check("report_render 产物真实落盘", os.path.exists(rep.get("path", "")),
          rep.get("path", ""))
    check("report_render preview_md 为 Markdown",
          rep.get("preview_md", "").startswith("#"),
          rep.get("preview_md", "")[:50].replace("\n", " "))

    # 4 个模板都可用
    tmpl_ok = []
    for tpl in ("daily", "weekly", "customer", "spec"):
        try:
            out = report_render(tpl, {"title": f"{tpl} 报告", "params": []})
            tmpl_ok.append(os.path.exists(out["path"]))
        except Exception as exc:  # noqa: BLE001
            tmpl_ok.append(False)
            print(f"      模板 {tpl} 失败：{exc}")
    check("4 个模板（daily/weekly/customer/spec）均可渲染落盘",
          all(tmpl_ok), f"{tmpl_ok}")

    # 非法模板报错
    try:
        report_render("bogus", {})
        check("report_render 非法模板报错", False, "未抛异常")
    except ToolError as e:
        check("report_render 非法模板报错", e.code == "INVALID_ARGUMENTS", f"code={e.code}")

    ntf = notify(
        channel="wecom",
        receivers=["eng_a", "eng_b"],
        title="良率异常告警",
        body="bin1 良率下降到 95.1%，请立即排查。",
        severity="critical",
        links=[{"uri": rep["uri"]}],
    )
    check("notify 返回非 mock", _no_mock(ntf))
    check("notify 返回 message_id/sent_at/status",
          all(ntf.get(k) for k in ("message_id", "sent_at", "status")),
          f"message_id={ntf.get('message_id')} status={ntf.get('status')}")
    check("notify severity→level 映射", ntf.get("level") == "critical", f"level={ntf.get('level')}")
    outbox_abs = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "data", "outbox", "notify.jsonl")
    check("notify 写入本地 outbox", os.path.exists(outbox_abs), outbox_abs)
    with open(outbox_abs, "r", encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    check("outbox 末条记录含本次 message_id",
          bool(lines) and json.loads(lines[-1]).get("message_id") == ntf["message_id"],
          f"{len(lines)} 条记录")

    # 非法参数报错
    for label, kwargs in [
        ("非法 channel", dict(channel="sms", receivers=["a"], title="t", body="b")),
        ("空 receivers", dict(channel="wecom", receivers=[], title="t", body="b")),
        ("空 title", dict(channel="wecom", receivers=["a"], title="", body="b")),
        ("非法 severity", dict(channel="wecom", receivers=["a"], title="t", body="b",
                               severity="fatal")),
    ]:
        try:
            notify(**kwargs)
            check(f"notify {label} 报错", False, "未抛异常")
        except ToolError as e:
            check(f"notify {label} 报错", e.code == "INVALID_ARGUMENTS", f"code={e.code}")

    print("\n=== 6. ToolRegistry 注册 / schema 导出 / HITL 标记 ===")
    from agent_platform.tools import register_all

    registry = ToolRegistry()
    register_all(registry)
    names = registry.names()
    print(f"      已注册工具：{names}")
    for tool in ("tp_lookup", "spec_extract", "case_lint", "report_render", "notify"):
        check(f"{tool} 已注册到 ToolRegistry", tool in names)

    expected_perm = {
        "tp_lookup": ("READ_ONLY", False),
        "spec_extract": ("COMPUTE", False),
        "case_lint": ("COMPUTE", False),
        "report_render": ("WRITE_DATA", True),
        "notify": ("EXTERNAL_SIDE_EFFECT", True),
    }
    for tool, (perm, hitl) in expected_perm.items():
        spec_obj = registry.get(tool)
        check(f"{tool} permission={perm} hitl_required={hitl}",
              spec_obj is not None
              and spec_obj.permission == perm
              and spec_obj.hitl_required is hitl,
              f"permission={getattr(spec_obj, 'permission', None)} "
              f"hitl={getattr(spec_obj, 'hitl_required', None)}")

    schemas = registry.openai_schemas()
    schema_names = [s["function"]["name"] for s in schemas]
    check("openai_schemas() 导出全部工具",
          all(t in schema_names for t in expected_perm),
          f"{schema_names}")
    check("openai_schemas() 结构正确",
          all(s.get("type") == "function"
              and isinstance(s["function"].get("parameters"), dict)
              for s in schemas))

    print("\n=== 7. 经 ToolRegistry 实际调用（端到端）===")
    out = registry.call("tp_lookup", {"tp_name": "TP_CMOS_32G",
                                       "test_num": "T101_VDD_FUNCT"})
    check("registry.call(tp_lookup) 返回真实限值",
          bool(out.get("items")) and out["items"][0].get("upper") == 3.465,
          f"upper={out.get('items', [{}])[0].get('upper') if out.get('items') else None}")
    out2 = registry.call("spec_extract", {"doc_id": "SPEC-77", "param_codes": ["VDD"]})
    check("registry.call(spec_extract) 返回真实参数",
          bool(out2.get("params")) and out2["params"][0].get("typ") == 3.3,
          f"typ={out2.get('params', [{}])[0].get('typ') if out2.get('params') else None}")
    out3 = registry.call("case_lint", {"cases": BAD_CASES})
    check("registry.call(case_lint) 返回违规报告",
          any(r["rule_id"] == "R02" for r in (out3.get("report") or [])),
          f"{len(out3.get('report') or [])} 条")
    out4 = registry.call("report_render", {"template": "daily",
                                            "data": {"title": "registry 调用日报"}})
    check("registry.call(report_render) 真实落盘", os.path.exists(out4.get("path", "")),
          out4.get("uri", ""))
    out5 = registry.call("notify", {"channel": "email", "receivers": ["eng_x"],
                                    "title": "registry 调用通知", "body": "测试正文"})
    check("registry.call(notify) 返回 message_id", bool(out5.get("message_id")),
          out5.get("message_id", ""))

    print("\n=== 8. ToolError 契约（core 异常类型 + 四字段）===")
    from agent_platform.core.tool_registry import ToolError as CoreToolError

    try:
        tp_lookup("TP_NOT_EXIST")
        check("抛出的是 core 的 ToolError", False, "未抛异常")
    except CoreToolError as e:
        payload = e.to_dict()
        check("抛出的是 core 的 ToolError", True, type(e).__module__ + "." + type(e).__name__)
        check("ToolError 含 {code,message,retryable,hint}",
              set(payload) == {"code", "message", "retryable", "hint"},
              json.dumps(payload, ensure_ascii=False)[:160])

    print("\n" + "=" * 62)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"结果: {passed}/{len(results)} 通过")
    for n, ok, d in results:
        if not ok:
            print(f"  失败: {n}  {d}")
    print("=" * 62)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
