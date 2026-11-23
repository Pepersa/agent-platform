# -*- coding: utf-8 -*-
"""审计日志（append-only）。

对应设计文档：
    《01_架构方案_RAG_Agent.md》§2.10 治理层 →「审计日志」
        技术选型 B. 直接写审计 PG 表；要求不可篡改
    《02_场景方案_STDF分析与测试用例生成.md》§9.3 审计日志
        字段：audit_id / ts / user_id / session_id / request_id / intent /
              model / prompt_hash / prompt_version / tool_calls /
              retrieval / output_hash / hitl / pii_masked / data_scope / cost

核心设计（工程取舍）：
    1. 优先写 PostgreSQL 表 audit_log；不可用时自动降级到本地 SQLite；
       SQLite 也不可用时降级到 JSONL 文件。**审计永远不阻断 Agent 主流程**。
    2. 落盘位置由环境变量 AUDIT_DSN 控制：
         有 AUDIT_DSN  -> PostgreSQL（失败再降级）
         无 AUDIT_DSN  -> SQLite：<数据目录>/audit/audit.db
         再降级        -> JSONL：<数据目录>/audit/audit.jsonl
         其中「数据目录」由环境变量 AGENT_DATA_DIR 决定（默认 <repo>/data），
         详见 :mod:`agent_platform.core.paths`。
    3. 表结构为 append-only：只 INSERT，不提供 UPDATE/DELETE 入口。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# audit_log 表结构（PostgreSQL 与 SQLite 共用同一套列定义）
AUDIT_COLUMNS: List[str] = [
    "audit_id", "ts", "session_id", "thread_id", "user_id", "actor", "action",
    "target", "args", "result", "status", "duration_ms", "model",
    "prompt_hash", "output_hash", "extra",
]

_PG_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id    TEXT PRIMARY KEY,
    ts          TEXT        NOT NULL,
    session_id  TEXT,
    thread_id   TEXT,
    user_id     TEXT,
    actor       TEXT,
    action      TEXT        NOT NULL,
    target      TEXT,
    args        JSONB,
    result      JSONB,
    status      TEXT,
    duration_ms INTEGER,
    model       TEXT,
    prompt_hash TEXT,
    output_hash TEXT,
    extra       JSONB
);
CREATE INDEX IF NOT EXISTS idx_audit_log_session ON audit_log(session_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_user    ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_ts      ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_log_action  ON audit_log(action);
"""

_SQLITE_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id    TEXT PRIMARY KEY,
    ts          TEXT NOT NULL,
    session_id  TEXT,
    thread_id   TEXT,
    user_id     TEXT,
    actor       TEXT,
    action      TEXT NOT NULL,
    target      TEXT,
    args        TEXT,
    result      TEXT,
    status      TEXT,
    duration_ms INTEGER,
    model       TEXT,
    prompt_hash TEXT,
    output_hash TEXT,
    extra       TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_log_session ON audit_log(session_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_user    ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_ts      ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_log_action  ON audit_log(action);
"""


def _now_iso() -> str:
    """带时区的 ISO8601 时间戳（docs/02 §9.3 示例为 +08:00 格式）。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _json_default(obj: Any) -> Any:
    """JSON 序列化兜底：不可序列化对象转字符串。"""
    try:
        return str(obj)
    except Exception:  # pragma: no cover - 极端兜底
        return "<unserializable>"


def _dumps(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=_json_default)
    except Exception:  # pragma: no cover
        return str(value)


class Audit:
    """审计写入器。

    典型用法：
        audit = Audit()                       # 自动选择后端
        audit.log(action="tool_call", target="kb_search",
                 args={"query": "..."}, result={"chunks": 3},
                 status="ok", duration_ms=842, session_id="S-8842")
    """

    def __init__(self, dsn: Optional[str] = None, *,
                 fallback_dir: Optional[str] = None) -> None:
        """
        :param dsn: PostgreSQL DSN；None 时读环境变量 AUDIT_DSN。
        :param fallback_dir: 降级落盘目录（SQLite / JSONL）；
            None 时取**数据根目录下的 ``audit/``**（由
            :mod:`agent_platform.core.paths` 解析，可通过环境变量
            ``AGENT_DATA_DIR`` 整体外置）。
        """
        from . import paths  # noqa: PLC0415 - 延迟导入避免循环依赖

        self.dsn = dsn if dsn is not None else os.environ.get("AUDIT_DSN", "").strip()
        self.fallback_dir = (fallback_dir if fallback_dir
                             else str(paths.audit_dir()))
        self.mode = "none"
        self._pg = None            # psycopg 连接
        self._sqlite: Optional[sqlite3.Connection] = None
        self._jsonl_path: Optional[str] = None
        self._lock = threading.Lock()
        self._init_backend()

    # ------------------------------------------------------------------
    # 后端初始化与降级
    # ------------------------------------------------------------------
    def _init_backend(self) -> None:
        if self.dsn:
            if self._init_postgres():
                self.mode = "postgres"
                return
            # PG 失败 → 继续降级
        if self._init_sqlite():
            self.mode = "sqlite"
            return
        if self._init_jsonl():
            self.mode = "jsonl"

    def _init_postgres(self) -> bool:
        try:
            import psycopg  # type: ignore
        except Exception:
            return False
        try:
            conn = psycopg.connect(self.dsn, connect_timeout=5)
            with conn.cursor() as cur:
                cur.execute(_PG_CREATE_SQL)
            conn.commit()
            self._pg = conn
            return True
        except Exception:
            return False

    def _init_sqlite(self) -> bool:
        try:
            os.makedirs(self.fallback_dir, exist_ok=True)
            path = os.path.join(self.fallback_dir, "audit.db")
            conn = sqlite3.connect(path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SQLITE_CREATE_SQL)
            conn.commit()
            self._sqlite = conn
            return True
        except Exception:
            self._sqlite = None
            return False

    def _init_jsonl(self) -> bool:
        try:
            os.makedirs(self.fallback_dir, exist_ok=True)
            path = os.path.join(self.fallback_dir, "audit.jsonl")
            with open(path, "a", encoding="utf-8"):
                pass
            self._jsonl_path = path
            return True
        except Exception:
            self._jsonl_path = None
            return False

    # ------------------------------------------------------------------
    # 哈希
    # ------------------------------------------------------------------
    @staticmethod
    def _hash(payload: Any) -> str:
        """sha256 前 24 位 + "sha256:" 前缀（docs/02 §9.3 prompt_hash 格式）。"""
        if payload is None:
            text = ""
        elif isinstance(payload, str):
            text = payload
        else:
            text = _dumps(payload) or ""
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def log(self, **kwargs: Any) -> Dict[str, Any]:
        """写一条审计记录。

        允许的 kwargs（对齐 docs/02 §9.3）：
            audit_id / ts / session_id / thread_id / user_id / actor / action /
            target / args / result / status / duration_ms / model /
            prompt_hash / output_hash / extra
        """
        row: Dict[str, Any] = {
            "audit_id": kwargs.get("audit_id") or self._new_audit_id(),
            "ts": kwargs.get("ts") or _now_iso(),
            "session_id": kwargs.get("session_id"),
            "thread_id": kwargs.get("thread_id"),
            "user_id": kwargs.get("user_id"),
            "actor": kwargs.get("actor"),
            "action": kwargs.get("action") or "unknown",
            "target": kwargs.get("target"),
            "args": kwargs.get("args"),
            "result": kwargs.get("result"),
            "status": kwargs.get("status") or "ok",
            "duration_ms": kwargs.get("duration_ms"),
            "model": kwargs.get("model"),
            "prompt_hash": kwargs.get("prompt_hash"),
            "output_hash": kwargs.get("output_hash"),
            "extra": kwargs.get("extra"),
        }
        # 未显式给出哈希时自动补齐（args/result 哈希便于按内容检索）
        if not row["prompt_hash"]:
            row["prompt_hash"] = self._hash(row["args"])
        if not row["output_hash"]:
            row["output_hash"] = self._hash(row["result"])

        self._write(row)
        return row

    def _new_audit_id(self) -> str:
        """审计 ID 形如 AUD-20261115-009821（docs/02 §9.3 示例格式）。"""
        stamp = datetime.now().strftime("%Y%m%d")
        suffix = uuid.uuid4().hex[:6].upper()
        return f"AUD-{stamp}-{suffix}"

    def _write(self, row: Dict[str, Any]) -> None:
        """实际写库；任何异常都不得向上抛出（审计不能拖垮 Agent）。"""
        with self._lock:
            try:
                if self.mode == "postgres" and self._pg is not None:
                    self._write_postgres(row)
                elif self.mode == "sqlite" and self._sqlite is not None:
                    self._write_sqlite(row)
                elif self.mode == "jsonl" and self._jsonl_path:
                    self._write_jsonl(row)
            except Exception:
                # 主后端写失败 → 兜底 JSONL，保证不丢审计
                try:
                    self._write_jsonl(row)
                    if self.mode != "jsonl":
                        self.mode = "jsonl"
                except Exception:
                    pass

    def _write_postgres(self, row: Dict[str, Any]) -> None:
        import psycopg  # type: ignore
        import json as _json
        cols = ", ".join(AUDIT_COLUMNS)
        placeholders = ", ".join(["%s"] * len(AUDIT_COLUMNS))
        vals = []
        for c in AUDIT_COLUMNS:
            v = row.get(c)
            if c in ("args", "result", "extra"):
                v = _json.dumps(v, ensure_ascii=False, default=_json_default) if v is not None else None
            vals.append(v)
        with self._pg.cursor() as cur:
            cur.execute(f"INSERT INTO audit_log ({cols}) VALUES ({placeholders})", vals)
        self._pg.commit()

    def _write_sqlite(self, row: Dict[str, Any]) -> None:
        cols = ", ".join(AUDIT_COLUMNS)
        placeholders = ", ".join(["?"] * len(AUDIT_COLUMNS))
        vals = []
        for c in AUDIT_COLUMNS:
            v = row.get(c)
            if c in ("args", "result", "extra"):
                v = _dumps(v)
            vals.append(v)
        self._sqlite.execute(
            f"INSERT OR REPLACE INTO audit_log ({cols}) VALUES ({placeholders})", vals
        )
        self._sqlite.commit()

    def _write_jsonl(self, row: Dict[str, Any]) -> None:
        if not self._jsonl_path:
            return
        with open(self._jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=_json_default) + "\n")

    # ------------------------------------------------------------------
    # 查询（供 /health、审计检索使用）
    # ------------------------------------------------------------------
    def recent(self, limit: int = 20, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """读取最近的审计记录（降级后端下仅支持 JSONL/SQLite 读取）。"""
        limit = max(1, int(limit))
        try:
            if self.mode == "postgres" and self._pg is not None:
                with self._pg.cursor() as cur:
                    if session_id:
                        cur.execute(
                            "SELECT * FROM audit_log WHERE session_id = %s "
                            "ORDER BY ts DESC LIMIT %s", (session_id, limit))
                    else:
                        cur.execute("SELECT * FROM audit_log ORDER BY ts DESC LIMIT %s", (limit,))
                    return [dict(r) for r in cur.fetchall()]
            if self.mode == "sqlite" and self._sqlite is not None:
                if session_id:
                    cur = self._sqlite.execute(
                        "SELECT * FROM audit_log WHERE session_id = ? ORDER BY ts DESC LIMIT ?",
                        (session_id, limit))
                else:
                    cur = self._sqlite.execute(
                        "SELECT * FROM audit_log ORDER BY ts DESC LIMIT ?", (limit,))
                return [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
            if self.mode == "jsonl" and self._jsonl_path and os.path.exists(self._jsonl_path):
                rows: List[Dict[str, Any]] = []
                with open(self._jsonl_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            continue
                if session_id:
                    rows = [r for r in rows if r.get("session_id") == session_id]
                rows.reverse()
                return rows[:limit]
        except Exception:
            return []
        return []

    def close(self) -> None:
        """关闭底层连接。"""
        with self._lock:
            try:
                if self._sqlite is not None:
                    self._sqlite.close()
            except Exception:
                pass
            try:
                if self._pg is not None:
                    self._pg.close()
            except Exception:
                pass

    def health(self) -> Dict[str, Any]:
        """返回审计后端健康信息（/health 用）。"""
        return {
            "mode": self.mode,
            "dsn_configured": bool(self.dsn),
            "fallback_dir": self.fallback_dir,
        }
