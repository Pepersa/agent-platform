# -*- coding: utf-8 -*-
"""探针：GET /kb/search 是否返回 200 + 命中 JSON。

用法：
    python scripts\\probe_search.py [--base http://127.0.0.1:8010]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("AGENT_API_BASE", "http://127.0.0.1:8010"))
    ap.add_argument("--query", default="标定增益是多少")
    ap.add_argument("--top-k", type=int, default=5)
    args = ap.parse_args()
    base = args.base.rstrip("/")

    t0 = time.time()
    with httpx.Client(timeout=300.0) as c:
        r = c.get(f"{base}/kb/search",
                  params={"query": args.query, "top_k": args.top_k,
                          "max_security_level": "internal", "roles": "engineer"})
    dt = time.time() - t0

    print(f"HTTP {r.status_code}  ({dt:.1f}s)")
    if r.status_code != 200:
        print(r.text[:500])
        return 1
    sj = r.json()
    hits = sj.get("hits") or []
    print(json.dumps({"hit_count": sj.get("hit_count"),
                      "reason": sj.get("reason"),
                      "n_hits": len(hits)},
                     ensure_ascii=False))
    for h in hits[:5]:
        print(f"  - {h.get('doc_id')} / {h.get('chunk_id')}  "
              f"score={h.get('score')}  {str(h.get('text'))[:70]!r}")
    ok = r.status_code == 200 and len(hits) > 0
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
