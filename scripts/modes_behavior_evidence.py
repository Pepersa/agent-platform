# -*- coding: utf-8 -*-
"""开发辅助：取证三种模式（agent / ask / plan）的真实行为差异。

用法：
    python scripts/modes_behavior_evidence.py
输出：每个模式的 SSE 事件序列 + 工具调用次数 + 结论。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

BASE = os.environ.get("AGENT_API", "http://127.0.0.1:8010")

PROMPT = "请检索知识库里关于倒排索引的文档，并把结论写入 /tmp/mode_probe.txt。"


def chat(body: dict) -> dict:
    req = urllib.request.Request(
        BASE + "/chat",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    raw = urllib.request.urlopen(req, timeout=300).read().decode("utf-8")
    events = []
    for blk in raw.split("\n\n"):
        line = next((l for l in blk.split("\n") if l.startswith("data: ")), None)
        if not line:
            continue
        payload = line[6:].strip()
        if payload == "[DONE]":
            continue
        try:
            events.append(json.loads(payload))
        except Exception:
            pass
    return {"events": events, "raw_len": len(raw)}


def main() -> int:
    for mode in ("agent", "ask", "plan"):
        r = chat({"message": PROMPT, "mode": mode, "stream": True})
        evs = r["events"]
        kinds = [e.get("event") for e in evs]
        result = next((e for e in evs if e.get("event") == "result"), {})
        plan = next((e for e in evs if e.get("event") == "plan"), None)
        tool_events = [e for e in evs if e.get("tools")]
        print("=" * 70)
        print(f"mode = {mode}")
        print("  事件序列      :", kinds)
        print("  tool_calls_used:", result.get("tool_calls_used"))
        print("  stop_reason   :", result.get("stop_reason"))
        print("  实际调用过的工具:", [e.get("tools") for e in tool_events])
        print("  是否有 plan 事件:", bool(plan))
        if plan:
            p = plan["plan"]
            print("  计划目标      :", p.get("goal"))
            print("  计划步骤      :")
            for i, s in enumerate(p.get("steps") or [], 1):
                print(f"    {i}. {s.get('title')} | tools={s.get('tools')} | risk={s.get('risk')}")
            print("  风险点        :", p.get("risks"))
            print("  待确认        :", p.get("questions"))
        print("  answer 摘要   :", (result.get("answer") or "")[:220].replace("\n", " "))
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
