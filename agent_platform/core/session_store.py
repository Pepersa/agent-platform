# -*- coding: utf-8 -*-
"""会话持久化（SQLite）：消息历史 + 会话摘要。

背景（缺陷 4 / 缺陷 5）：
    原来的 ``AppState.touch_session()`` 只在内存里存一个 120 字的摘要
    与消息条数，**消息内容根本没落盘**，导致：
        · 历史会话点开只能看到 thread_id，看不到任何对话内容
        · 进程重启后历史全部消失
        · 无法生成会话摘要（没有消息可总结）

本模块用**独立 SQLite 文件**（默认 ``data/sessions.db``）持久化：

    · ``sessions``        —— 会话元信息（thread_id / 摘要 / 摘要状态 / 时间戳）
    · ``session_messages``—— 逐条消息（thread_id / seq / role / content /
                            tool_calls / citations / ts）

**为什么不复用知识库的 chunks.db**：知识库库由 Embedder / Retriever 持有连接，
混用会让两边的生命周期与锁策略互相干扰，单独一个库更干净。

**诚实说明（重要）**：
    LangGraph 的 checkpointer 是**进程内** ``InMemorySaver``，
    因此 **重启后图状态（graph state）会丢**：
        · 消息历史**不会丢**（本模块落盘）
        · 但不能「从历史中间续跑 ReAct」，只能基于历史**继续提问**
    前端会据此给出明确提示，避免用户误以为可以断点续跑。

线程安全：
    · 每个操作使用**独立连接**（``sqlite3.connect``），避免跨线程共享连接；
    · ``check_same_thread=False`` + 写操作用 ``threading.Lock`` 串行化；
    · 开启 WAL 模式，降低读写互相阻塞的概率。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

#: 默认落盘位置（数据根目录 / sessions.db）
DEFAULT_DB_NAME = "sessions.db"

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    thread_id       TEXT PRIMARY KEY,
    session_id      TEXT,
    user_id         TEXT,
    title           TEXT,
    summary         TEXT,
    summary_status  TEXT DEFAULT 'none',
    summary_json    TEXT,
    message_count   INTEGER DEFAULT 0,
    mode            TEXT,
    model           TEXT,
    created_at      REAL,
    updated_at      REAL
);

CREATE TABLE IF NOT EXISTS session_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id   TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT,
    tool_calls  TEXT,
    citations   TEXT,
    extra       TEXT,
    ts          REAL NOT NULL,
    UNIQUE(thread_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_msg_thread ON session_messages(thread_id, seq);
CREATE INDEX IF NOT EXISTS idx_sess_updated ON sessions(updated_at);
"""


def _default_db_path() -> Path:
    """默认路径：数据根目录 / sessions.db。

    数据根目录由 :mod:`agent_platform.core.paths` 统一解析：
    环境变量 ``AGENT_DATA_DIR`` > 项目默认 ``<repo_root>/data``。
    不设环境变量时与改造前完全一致。
    """
    from . import paths  # noqa: PLC0415 - 延迟导入避免循环依赖

    return paths.data_path(DEFAULT_DB_NAME)


def _dumps(value: Any) -> str:
    """JSON 序列化（兜底字符串）。"""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return ""


def _loads(raw: Any, default: Any = None) -> Any:
    """JSON 反序列化（容错）。"""
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


class SessionStore:
    """会话与消息的 SQLite 持久化层。

    线程安全：``_lock`` 串行化所有写操作；读操作用独立连接。
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = Path(db_path) if db_path else _default_db_path()
        self._lock = threading.RLock()
        self._ensure_schema()

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        """打开一个**独立**连接（每次操作一个，避免跨线程共享）。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False,
                               timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        return conn

    def _ensure_schema(self) -> None:
        """建表（幂等）。"""
        try:
            with self._lock:
                conn = self._connect()
                try:
                    conn.executescript(_CREATE_SQL)
                    conn.commit()
                finally:
                    conn.close()
        except Exception:  # pragma: no cover - 磁盘只读等极端情况
            pass

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------
    def touch_session(self, thread_id: str, *, title: str = "",
                      session_id: str = "", user_id: str = "",
                      mode: str = "", model: str = "",
                      bump_count: bool = True) -> Dict[str, Any]:
        """登记 / 更新一个会话。

        Args:
            thread_id: 会话线程 ID（主键）。
            title: 会话标题（默认取首条用户消息摘要）。
            bump_count: 是否把 ``message_count`` 加一。

        Returns:
            会话记录 dict。
        """
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT * FROM sessions WHERE thread_id = ?", (thread_id,))
                row = cur.fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO sessions (thread_id, session_id, user_id, title,"
                        " summary, summary_status, message_count, mode, model,"
                        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (thread_id, session_id or "", user_id or "", title or "",
                         "", "none", 1 if bump_count else 0, mode or "", model or "",
                         now, now),
                    )
                    conn.commit()
                    rec = self.get_session(thread_id) or {}
                    return rec
                cnt = int(row["message_count"] or 0)
                if bump_count:
                    cnt += 1
                new_title = row["title"] or ""
                if title and (not new_title or len(new_title) < 4):
                    new_title = title[:120]
                conn.execute(
                    "UPDATE sessions SET session_id = COALESCE(NULLIF(?,''), session_id),"
                    " user_id = COALESCE(NULLIF(?,''), user_id),"
                    " title = COALESCE(NULLIF(?,''), title),"
                    " message_count = ?, mode = COALESCE(NULLIF(?,''), mode),"
                    " model = COALESCE(NULLIF(?,''), model),"
                    " updated_at = ? WHERE thread_id = ?",
                    (session_id or "", user_id or "", new_title, cnt,
                     mode or "", model or "", now, thread_id),
                )
                conn.commit()
                return self.get_session(thread_id) or {}
            finally:
                conn.close()

    def get_session(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """取单个会话元信息（含摘要）。"""
        try:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT * FROM sessions WHERE thread_id = ?", (thread_id,))
                row = cur.fetchone()
                if row is None:
                    return None
                return self._row_to_session(row)
            finally:
                conn.close()
        except Exception:
            return None

    def list_sessions(self, limit: int = 200) -> List[Dict[str, Any]]:
        """列出会话（按更新时间倒序）。

        ``message_count`` 取 ``session_messages`` 表里的**实际条数**
        （而不是 touch_session 的调用计数），保证与
        ``GET /sessions/{id}/messages`` 的长度一致。
        """
        try:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT s.*, ("
                    "  SELECT COUNT(*) FROM session_messages m"
                    "  WHERE m.thread_id = s.thread_id"
                    ") AS real_msg_count"
                    " FROM sessions s ORDER BY s.updated_at DESC LIMIT ?",
                    (int(limit),))
                out = []
                for r in cur.fetchall():
                    d = self._row_to_session(r)
                    real = int(d.pop("real_msg_count", 0) or 0)
                    if real:
                        d["message_count"] = real
                    # 该会话是否有**非空**的落盘轨迹（extra.trace）——
                    # 前端用它判断「能否从磁盘恢复观测面板数据」。
                    # 注意：必须判非空，只判 key 是否存在会把
                    # ``"trace": []``（空数组）也算成有轨迹，导致恢复拿到空数据。
                    d["has_trace"] = self._session_has_trace(d.get("thread_id", ""))
                    out.append(d)
                return out
            finally:
                conn.close()
        except Exception:
            return []

    def _session_has_trace(self, thread_id: str) -> bool:
        """该会话是否有**非空**的落盘轨迹（``extra.trace``）。

        只看 key 是否存在是不够的 —— ``"trace": []``（空数组）也会命中，
        但对前端毫无价值（恢复出来还是空的）。
        """
        if not thread_id:
            return False
        try:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT extra FROM session_messages "
                    "WHERE thread_id = ? AND extra IS NOT NULL AND extra != ''",
                    (thread_id,))
                for row in cur.fetchall():
                    data = _loads(row[0], None)
                    if isinstance(data, dict):
                        tr = data.get("trace")
                        if isinstance(tr, list) and tr:
                            return True
            finally:
                conn.close()
        except Exception:
            return False
        return False

    def delete_session(self, thread_id: str) -> bool:
        """删除会话及其全部消息。"""
        with self._lock:
            try:
                conn = self._connect()
                try:
                    conn.execute("DELETE FROM session_messages WHERE thread_id = ?",
                                 (thread_id,))
                    cur = conn.execute("DELETE FROM sessions WHERE thread_id = ?",
                                       (thread_id,))
                    conn.commit()
                    return cur.rowcount > 0
                finally:
                    conn.close()
            except Exception:
                return False

    # ------------------------------------------------------------------
    # 消息
    # ------------------------------------------------------------------
    def append_message(self, thread_id: str, *, role: str, content: str,
                       tool_calls: Optional[Any] = None,
                       citations: Optional[Any] = None,
                       extra: Optional[Any] = None,
                       ts: Optional[float] = None) -> int:
        """追加一条消息，返回其 ``seq``（自增，按会话内顺序）。

        Args:
            thread_id: 会话 ID。
            role: ``user`` / ``assistant`` / ``tool`` / ``system``。
            content: 消息正文。
            tool_calls: 工具调用（list / dict，JSON 序列化存储）。
            citations: 引用（list / dict）。
            extra: 附加信息（dict）。
            ts: 时间戳（默认当前时间）。
        """
        stamp = float(ts if ts is not None else time.time())
        with self._lock:
            try:
                conn = self._connect()
                try:
                    cur = conn.execute(
                        "SELECT COALESCE(MAX(seq), 0) FROM session_messages"
                        " WHERE thread_id = ?", (thread_id,))
                    seq = int(cur.fetchone()[0] or 0) + 1
                    conn.execute(
                        "INSERT OR REPLACE INTO session_messages"
                        " (thread_id, seq, role, content, tool_calls, citations,"
                        "  extra, ts) VALUES (?,?,?,?,?,?,?,?)",
                        (thread_id, seq, role or "assistant", content or "",
                         _dumps(tool_calls) if tool_calls else "",
                         _dumps(citations) if citations else "",
                         _dumps(extra) if extra else "", stamp),
                    )
                    conn.commit()
                    return seq
                finally:
                    conn.close()
            except Exception:
                return 0

    def get_messages(self, thread_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        """取会话的完整消息历史（按 seq 升序）。"""
        try:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT seq, role, content, tool_calls, citations, extra, ts"
                    " FROM session_messages WHERE thread_id = ?"
                    " ORDER BY seq ASC LIMIT ?", (thread_id, int(limit)))
                out: List[Dict[str, Any]] = []
                for r in cur.fetchall():
                    out.append({
                        "seq": int(r["seq"]),
                        "role": r["role"],
                        "content": r["content"] or "",
                        "tool_calls": _loads(r["tool_calls"], []) or [],
                        "citations": _loads(r["citations"], []) or [],
                        "extra": _loads(r["extra"], {}) or {},
                        "ts": float(r["ts"] or 0),
                    })
                return out
            finally:
                conn.close()
        except Exception:
            return []

    def count_messages(self, thread_id: str) -> int:
        """会话的消息条数。"""
        try:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM session_messages WHERE thread_id = ?",
                    (thread_id,))
                return int(cur.fetchone()[0] or 0)
            finally:
                conn.close()
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # 摘要
    # ------------------------------------------------------------------
    def save_summary(self, thread_id: str, summary: Dict[str, Any]) -> bool:
        """持久化会话摘要（结构化 JSON + 首句文本）。

        Args:
            thread_id: 会话 ID。
            summary: ``{conclusion, key_points, documents, open_questions, ...}``。

        Returns:
            是否写入成功。
        """
        with self._lock:
            try:
                conn = self._connect()
                try:
                    conclusion = str(summary.get("conclusion", "") or "")
                    conn.execute(
                        "UPDATE sessions SET summary = ?, summary_status = 'ready',"
                        " summary_json = ?, updated_at = ? WHERE thread_id = ?",
                        (conclusion[:500], _dumps(summary), time.time(), thread_id),
                    )
                    if conn.total_changes == 0:
                        now = time.time()
                        conn.execute(
                            "INSERT OR REPLACE INTO sessions (thread_id, session_id,"
                            " user_id, title, summary, summary_status, summary_json,"
                            " message_count, mode, model, created_at, updated_at)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (thread_id, "", "", conclusion[:120], conclusion[:500],
                             "ready", _dumps(summary), 0, "", "", now, now),
                        )
                    conn.commit()
                    return True
                finally:
                    conn.close()
            except Exception:
                return False

    def get_summary(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """取已持久化的摘要（None 表示未生成）。"""
        sess = self.get_session(thread_id)
        if not sess:
            return None
        if sess.get("summary_status") != "ready":
            return None
        return sess.get("summary")

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> Dict[str, Any]:
        """``sqlite3.Row`` → dict（摘要字段反序列化）。"""
        d = {k: row[k] for k in row.keys()}
        d["summary_json"] = _loads(d.get("summary_json"), None)
        d["summary"] = d.get("summary") or ""
        d["summary_status"] = d.get("summary_status") or "none"
        d["message_count"] = int(d.get("message_count") or 0)
        d["has_summary"] = d["summary_status"] == "ready"
        return d

    def health(self) -> Dict[str, Any]:
        """返回存储健康信息（/health 用）。"""
        try:
            conn = self._connect()
            try:
                cur = conn.execute("SELECT COUNT(*) FROM sessions")
                sess = int(cur.fetchone()[0] or 0)
                cur = conn.execute("SELECT COUNT(*) FROM session_messages")
                msgs = int(cur.fetchone()[0] or 0)
            finally:
                conn.close()
            return {
                "backend": "sqlite",
                "path": str(self.db_path),
                "exists": self.db_path.is_file(),
                "sessions": sess,
                "messages": msgs,
            }
        except Exception as exc:  # noqa: BLE001
            return {"backend": "sqlite", "path": str(self.db_path),
                    "error": f"{type(exc).__name__}: {exc}"}


__all__ = ["SessionStore"]
