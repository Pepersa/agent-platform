# -*- coding: utf-8 -*-
"""开发辅助：导出 data/sessions.db 的表结构与样例行（用于交付报告取证）。"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.request

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "sessions.db")


def main() -> int:
    con = sqlite3.connect(DB)
    cur = con.cursor()
    print("== 表结构 ==")
    for name, sql in cur.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table'"):
        print(f"{name} -> {(sql or '')[:300]}\n")
    print("== 行数 ==")
    for t in ("sessions", "session_messages"):
        try:
            print(f"{t}: {cur.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0]}")
        except Exception as exc:
            print(f"{t}: {exc}")

    try:
        data = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:8010/sessions", timeout=30).read().decode())
        tid = data["sessions"][0]["thread_id"]
    except Exception:
        tid = ""
    if tid:
        print(f"\n== 示例会话 {tid} 的消息行 ==")
        for r in cur.execute(
                "SELECT seq, role, substr(content,1,70), tool_calls, citations, ts "
                "FROM session_messages WHERE thread_id=? ORDER BY seq LIMIT 8", (tid,)):
            print(r)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
