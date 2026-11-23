# -*- coding: utf-8 -*-
"""工作区（Workspace）—— 受控的代码浏览与写入能力。

背景
----
平台原本只能对话 / 检索知识库 / 跑任务 / 跑评测，**碰不到代码文件**。
本模块为「工作区代码编辑」功能提供**唯一**的能力入口：

    * 浏览：目录树 / 读文件 / 代码搜索（**只读**）
    * 写入：生成待审批改动 → 人工确认 → 原子落盘 → 可撤销（**必须 HITL**）

安全模型（第一优先级，逐条对应实现）
----------------------------------
1. **路径穿越防护**：所有路径先 ``resolve()``（会展开符号链接与 junction），
   再用 ``os.path.commonpath`` 确认结果**仍在工作区根目录内**，
   否则一律拒绝（``WS_PATH_ESCAPE``）。这同时挡住了 ``../`` 穿越与
   符号链接/junction 逃逸。
2. **敏感文件拦截**：``.git/`` / ``node_modules/`` / ``__pycache__/`` /
   ``.venv/`` / ``venv/`` / ``.env*`` / ``*.key`` / ``*.pem`` /
   ``data/kb/*.db`` / ``data/sessions.db`` / ``data/models.json``
   全部禁止访问（``WS_FORBIDDEN_FILE``）。
3. **大小上限**：单文件读写上限 512KB，超出报 ``WS_TOO_LARGE``。
4. **二进制拒绝**：按扩展名白名单 + 内容嗅探（NUL 字节）双重判定，
   报 ``WS_BINARY``。
5. **并发冲突检测**：写入时携带 ``expected_mtime``，与磁盘实际 mtime
   不一致 → ``409 WS_FILE_CHANGED``，把对方 mtime 返回给用户判断。
6. **原子写**：先写同目录临时文件（``.ws_tmp_*``）→ ``os.replace`` 覆盖，
   中途失败不会留下半截文件。
7. **撤销栈**：每次落盘前把旧内容压入撤销栈（SQLite ``data/workspace.db``），
   可 ``POST /workspace/undo`` 回滚，并提供 ``GET /workspace/history``。
8. **只读浏览**绝不产生任何副作用；**所有写入**都必须先经过人工确认。

本模块只依赖标准库，不引入任何第三方依赖。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "WorkspaceError",
    "Workspace",
    "get_workspace",
    "reset_workspace",
    "DEFAULT_MAX_FILE_BYTES",
    "FORBIDDEN_DIR_NAMES",
    "FORBIDDEN_FILE_PATTERNS",
    "TEXT_EXTENSIONS",
    "LANGUAGE_BY_EXT",
    "detect_language",
    "diff_lines",
]


# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------
#: 单文件读写大小上限（512KB）
DEFAULT_MAX_FILE_BYTES = 512 * 1024

#: 禁止访问的目录名（任意层级）
FORBIDDEN_DIR_NAMES = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__",
    ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
})

#: 禁止访问的文件名模式（正则，对**文件名**与**相对路径**都匹配）
FORBIDDEN_FILE_PATTERNS = (
    re.compile(r"^\.env($|\.)", re.IGNORECASE),      # .env / .env.local / .env.production
    re.compile(r"\.key$", re.IGNORECASE),            # *.key
    re.compile(r"\.pem$", re.IGNORECASE),            # *.pem
    re.compile(r"\.p12$", re.IGNORECASE),
    re.compile(r"\.pfx$", re.IGNORECASE),
)

#: 禁止访问的相对路径前缀/精确匹配（相对工作区根，POSIX 风格）
FORBIDDEN_REL_PATHS = (
    "data/sessions.db",
    "data/models.json",
)

#: 禁止访问的相对路径前缀
FORBIDDEN_REL_PREFIXES = (
    "data/kb/",
)

#: 可写（文本）文件扩展名白名单 —— **不在白名单里的一律拒绝写入**
TEXT_EXTENSIONS = frozenset({
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".json",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".md", ".markdown",
    ".txt", ".rst", ".html", ".htm", ".css", ".scss", ".less", ".xml", ".sql",
    ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".csv", ".tsv", ".log",
    ".env.example", ".gitignore", ".dockerignore", ".editorconfig", ".properties",
    ".gradle", ".cmake", ".make", ".mk", ".lua", ".r", ".go", ".rs", ".java",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".swift", ".kt", ".kts", ".vue", ".svelte",
})

#: 无扩展名但允许写入的文件名（精确匹配）
TEXT_BASENAMES = frozenset({
    "Makefile", "Dockerfile", "LICENSE", "README", "LICENSE.md", "README.md",
})

#: 扩展名 → 语言标识（前端高亮 + 图标用）
LANGUAGE_BY_EXT: Dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".json": "json",
    ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".ini": "ini", ".cfg": "ini", ".conf": "ini",
    ".md": "markdown", ".markdown": "markdown",
    ".txt": "text", ".rst": "text",
    ".html": "html", ".htm": "html",
    ".css": "css", ".scss": "css", ".less": "css",
    ".xml": "xml",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".ps1": "powershell", ".bat": "batch", ".cmd": "batch",
    ".sql": "sql",
    ".csv": "text", ".tsv": "text",
    ".html": "html",
}

#: 搜索时始终跳过的目录
SEARCH_IGNORED_DIRS = FORBIDDEN_DIR_NAMES | {".idea", ".vscode"}

#: 搜索单次最多扫描文件数
DEFAULT_MAX_SCAN_FILES = 500

#: 搜索默认超时（秒）
DEFAULT_SEARCH_TIMEOUT = 10.0

#: 搜索单文件最大读取字节
DEFAULT_MAX_SEARCH_FILE_BYTES = 256 * 1024

#: 撤销栈最大深度
DEFAULT_UNDO_DEPTH = 50


# ----------------------------------------------------------------------
# 错误
# ----------------------------------------------------------------------
class WorkspaceError(Exception):
    """工作区统一结构化错误。

    对外结构固定为 ``{code, message, retryable, hint}``（与 ToolError 一致）。
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False,
                 hint: str = "", status_code: int = 400,
                 extra: Optional[Dict[str, Any]] = None) -> None:
        self.code = code
        self.message = message
        self.retryable = bool(retryable)
        self.hint = hint
        self.status_code = int(status_code)
        self.extra = dict(extra or {})
        super().__init__(f"[{code}] {message}")

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "hint": self.hint,
        }
        out.update(self.extra)
        return out


#: 进程内单例（工作区根目录在进程生命周期内不变）
_WORKSPACE_SINGLETON: Optional["Workspace"] = None
_WORKSPACE_LOCK = threading.Lock()


def get_workspace(*, root: Optional[str] = None, audit: Any = None) -> "Workspace":
    """取工作区单例（首次调用时构造）。

    ``WORKSPACE_ROOT`` 环境变量优先，其次显式 ``root`` 参数，
    最后退回到项目根目录。
    """
    global _WORKSPACE_SINGLETON
    with _WORKSPACE_LOCK:
        if _WORKSPACE_SINGLETON is None:
            _WORKSPACE_SINGLETON = Workspace(root=root, audit=audit)
        return _WORKSPACE_SINGLETON


def reset_workspace() -> None:
    """重置单例（测试用）。"""
    global _WORKSPACE_SINGLETON
    with _WORKSPACE_LOCK:
        _WORKSPACE_SINGLETON = None


# ----------------------------------------------------------------------
# 语言识别
# ----------------------------------------------------------------------
def detect_language(path: str) -> str:
    """按扩展名推断语言标识（前端用于高亮与图标）。"""
    name = os.path.basename(path or "")
    ext = os.path.splitext(name)[1].lower()
    if ext in LANGUAGE_BY_EXT:
        return LANGUAGE_BY_EXT[ext]
    if name.lower() in ("makefile", "dockerfile"):
        return "makefile"
    if name.lower().startswith(".env"):
        return "dotenv"
    return "text"


# ----------------------------------------------------------------------
# Diff（行级）
# ----------------------------------------------------------------------
def diff_lines(before: str, after: str, *, path: str = "",
               context: int = 3) -> Dict[str, Any]:
    """生成行级 diff（before/after + 折叠的大段上下文）。

    Args:
        before: 旧内容。
        after: 新内容。
        path: 文件路径（仅用于展示）。
        context: 上下文行数。

    Returns:
        ``{"path":..., "lines":[{"op":"context|add|del","text":...,"old":n,"new":n}],
           "added":n, "removed":n, "hunks":[...]}``
    """
    b_lines = before.splitlines()
    a_lines = after.splitlines()
    sm = difflib.SequenceMatcher(None, b_lines, a_lines)
    rows: List[Dict[str, Any]] = []
    hunks: List[Dict[str, Any]] = []
    added = 0
    removed = 0

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            # 折叠未改动的大段：只保留首尾 context 行
            total = i2 - i1
            if total > context * 2 + 2:
                head = b_lines[i1:i1 + context]
                tail = b_lines[i2 - context:i2]
                for k, txt in enumerate(head):
                    rows.append({"op": "context", "text": txt,
                                 "old": i1 + k + 1, "new": j1 + k + 1})
                rows.append({"op": "skip", "text": "", "old": 0, "new": 0,
                             "count": total - context * 2,
                             "label": f"展开 {total - context * 2} 行"})
                for k, txt in enumerate(tail):
                    rows.append({"op": "context", "text": txt,
                                 "old": i2 - context + k + 1,
                                 "new": j2 - context + k + 1})
                hunks.append({"start_old": i1 + 1, "end_old": i2,
                              "start_new": j1 + 1, "end_new": j2,
                              "skipped": total - context * 2})
                continue
            for k in range(i1, i2):
                rows.append({"op": "context", "text": b_lines[k],
                             "old": k + 1, "new": j1 + (k - i1) + 1})
        elif tag == "replace":
            removed += i2 - i1
            added += j2 - j1
            for k in range(i1, i2):
                rows.append({"op": "del", "text": b_lines[k], "old": k + 1, "new": 0})
            for k in range(j1, j2):
                rows.append({"op": "add", "text": a_lines[k], "old": 0, "new": k + 1})
        elif tag == "delete":
            removed += i2 - i1
            for k in range(i1, i2):
                rows.append({"op": "del", "text": b_lines[k], "old": k + 1, "new": 0})
        elif tag == "insert":
            added += j2 - j1
            for k in range(j1, j2):
                rows.append({"op": "add", "text": a_lines[k], "old": 0, "new": k + 1})

    return {
        "path": path,
        "lines": rows,
        "added": added,
        "removed": removed,
        "hunks": hunks,
    }


# ----------------------------------------------------------------------
# 撤销栈 / 历史（SQLite）
# ----------------------------------------------------------------------
_UNDO_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS ws_undo (
    undo_id   TEXT PRIMARY KEY,
    ts        REAL NOT NULL,
    path      TEXT NOT NULL,
    action    TEXT NOT NULL,
    existed   INTEGER NOT NULL DEFAULT 0,
    old_text  TEXT,
    new_text  TEXT,
    old_mtime REAL,
    change_id TEXT,
    summary   TEXT
);
CREATE INDEX IF NOT EXISTS idx_ws_undo_path ON ws_undo(path, ts);
CREATE INDEX IF NOT EXISTS idx_ws_undo_ts   ON ws_undo(ts);

CREATE TABLE IF NOT EXISTS ws_history (
    hist_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    path      TEXT NOT NULL,
    action    TEXT NOT NULL,
    summary   TEXT,
    size      INTEGER,
    undo_id   TEXT,
    change_id TEXT,
    actor     TEXT
);
CREATE INDEX IF NOT EXISTS idx_ws_history_path ON ws_history(path, ts);
CREATE INDEX IF NOT EXISTS idx_ws_history_ts   ON ws_history(ts);

CREATE TABLE IF NOT EXISTS ws_changes (
    change_id  TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    path       TEXT NOT NULL,
    action     TEXT NOT NULL,
    status     TEXT NOT NULL,
    payload    TEXT,
    diff       TEXT,
    note       TEXT,
    expected_mtime REAL
);
CREATE INDEX IF NOT EXISTS idx_ws_changes_path ON ws_changes(path);
"""


class _UndoStore:
    """撤销栈 + 待审批改动持久化（SQLite + WAL + 线程锁）。

    与 ``core/session_store.py`` 同一套做法：每次操作独立连接、``check_same_thread=False``、
    写操作用线程锁串行化、开 WAL 降低读写互相阻塞。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(_UNDO_CREATE_SQL)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ---------------- 撤销栈 ----------------
    def push_undo(self, *, path: str, action: str, existed: bool,
                  old_text: Optional[str], new_text: Optional[str],
                  old_mtime: Optional[float], change_id: str = "",
                  summary: str = "") -> str:
        """把一次写入的旧内容压入撤销栈，返回 undo_id。"""
        undo_id = "u-" + uuid.uuid4().hex[:12]
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO ws_undo (undo_id, ts, path, action, existed,"
                    " old_text, new_text, old_mtime, change_id, summary)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (undo_id, time.time(), path, action, 1 if existed else 0,
                     old_text, new_text, old_mtime, change_id, summary))
                conn.commit()
            finally:
                conn.close()
        return undo_id

    def pop_undo(self) -> Optional[Dict[str, Any]]:
        """取出最近一次撤销记录（并标记已消费）。"""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT undo_id, ts, path, action, existed, old_text,"
                    " new_text, old_mtime, change_id, summary"
                    " FROM ws_undo ORDER BY ts DESC LIMIT 1")
                row = cur.fetchone()
                if not row:
                    return None
                conn.execute("DELETE FROM ws_undo WHERE undo_id = ?", (row[0],))
                conn.commit()
            finally:
                conn.close()
        return {
            "undo_id": row[0], "ts": row[1], "path": row[2],
            "action": row[3], "existed": bool(row[4]),
            "old_text": row[5], "new_text": row[6],
            "old_mtime": row[7], "change_id": row[8], "summary": row[9],
        }

    def undo_depth(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("SELECT COUNT(*) FROM ws_undo")
                return int(cur.fetchone()[0] or 0)
            finally:
                conn.close()

    def history(self, path: str, limit: int = 50) -> List[Dict[str, Any]]:
        """某文件的修改历史（不含旧内容全文，只含摘要）。"""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT undo_id, ts, path, action, existed, old_mtime,"
                    " change_id, summary FROM ws_undo WHERE path = ?"
                    " ORDER BY ts DESC LIMIT ?", (path, int(limit)))
                rows = cur.fetchall()
            finally:
                conn.close()
        return [
            {"undo_id": r[0], "ts": r[1], "path": r[2], "action": r[3],
             "existed": bool(r[4]), "old_mtime": r[5], "change_id": r[6],
             "summary": r[7]}
            for r in rows
        ]

    # ---------------- 修改历史（append-only，不随撤销栈弹出而消失） ----------------
    def push_history(self, *, path: str, action: str, summary: str = "",
                     size: int = 0, undo_id: str = "", change_id: str = "",
                     actor: str = "") -> None:
        """记录一次修改（**append-only**：撤销栈弹出后历史依然保留）。"""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO ws_history (ts, path, action, summary, size,"
                    " undo_id, change_id, actor) VALUES (?,?,?,?,?,?,?,?)",
                    (time.time(), path, action, summary, int(size or 0),
                     undo_id, change_id, actor or "operator"))
                conn.commit()
            finally:
                conn.close()

    def list_history(self, path: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        """读修改历史（按时间倒序）。"""
        with self._lock:
            conn = self._connect()
            try:
                if path:
                    cur = conn.execute(
                        "SELECT hist_id, ts, path, action, summary, size,"
                        " undo_id, change_id, actor FROM ws_history"
                        " WHERE path = ? ORDER BY ts DESC LIMIT ?",
                        (path, int(limit)))
                else:
                    cur = conn.execute(
                        "SELECT hist_id, ts, path, action, summary, size,"
                        " undo_id, change_id, actor FROM ws_history"
                        " ORDER BY ts DESC LIMIT ?", (int(limit),))
                rows = cur.fetchall()
            finally:
                conn.close()
        return [
            {"hist_id": r[0], "ts": r[1], "path": r[2], "action": r[3],
             "summary": r[4], "size": r[5], "undo_id": r[6],
             "change_id": r[7], "actor": r[8]}
            for r in rows
        ]

    # ---------------- 待审批改动 ----------------
    def save_change(self, change: Dict[str, Any]) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO ws_changes (change_id, created_at,"
                    " path, action, status, payload, diff, note, expected_mtime)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (change["change_id"], change["created_at"], change["path"],
                     change["action"], change["status"],
                     json.dumps(change.get("payload") or {}, ensure_ascii=False),
                     json.dumps(change.get("diff") or {}, ensure_ascii=False),
                     change.get("note") or "",
                     change.get("expected_mtime")))
                conn.commit()
            finally:
                conn.close()

    def get_change(self, change_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT change_id, created_at, path, action, status, payload,"
                    " diff, note, expected_mtime FROM ws_changes WHERE change_id = ?",
                    (change_id,))
                row = cur.fetchone()
            finally:
                conn.close()
        if not row:
            return None
        return {
            "change_id": row[0], "created_at": row[1], "path": row[2],
            "action": row[3], "status": row[4],
            "payload": json.loads(row[5] or "{}"),
            "diff": json.loads(row[6] or "{}"),
            "note": row[7] or "", "expected_mtime": row[8],
        }

    def update_change_status(self, change_id: str, status: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("UPDATE ws_changes SET status = ? WHERE change_id = ?",
                             (status, change_id))
                conn.commit()
            finally:
                conn.close()

    def delete_change(self, change_id: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM ws_changes WHERE change_id = ?", (change_id,))
                conn.commit()
            finally:
                conn.close()


# ----------------------------------------------------------------------
# 工作区
# ----------------------------------------------------------------------
class Workspace:
    """工作区：受控的代码浏览 + 受控写入。

    典型用法::

        ws = Workspace()
        ws.tree("agent_platform/core")          # 目录树
        ws.read_file("agent_platform/core/x.py") # 读文件
        ws.search("ToolRegistry")                # 代码搜索
        change = ws.stage_write(path, content)   # 生成待审批改动
        ws.apply_change(change["change_id"])     # 落盘（人工确认后）
        ws.undo()                                # 撤销
    """

    def __init__(self, *, root: Optional[str] = None,
                 max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
                 store_path: Optional[str] = None,
                 undo_depth: int = DEFAULT_UNDO_DEPTH,
                 audit: Any = None) -> None:
        env_root = os.environ.get("WORKSPACE_ROOT", "").strip()
        base = root or env_root or str(Path(__file__).resolve().parent.parent.parent)
        self.root = Path(base).expanduser().resolve()
        self.max_file_bytes = int(max_file_bytes)
        self.undo_depth = int(undo_depth)
        self._audit = audit
        self._lock = threading.RLock()
        # 撤销栈落盘位置：
        #   · 显式 ``store_path`` 优先；
        #   · 设了 ``AGENT_DATA_DIR``（数据目录已外置）→ 跟随数据目录，
        #     保证「整个数据目录挪到别的盘」时撤销栈也一起走；
        #   · 否则保持原样 ``<工作区根>/data/workspace.db``（向后兼容，
        #     改动前就是这个值）。
        if store_path:
            store_file = str(store_path)
        else:
            from . import paths  # noqa: PLC0415 - 延迟导入避免循环依赖

            if os.environ.get(paths.ENV_DATA_DIR, "").strip():
                store_file = str(paths.data_path("workspace.db"))
            else:
                store_file = str(self.root / "data" / "workspace.db")
        self._store = _UndoStore(Path(store_file))

    # ------------------------------------------------------------------
    # 路径安全
    # ------------------------------------------------------------------
    def _normalize_rel(self, raw: Any) -> str:
        """把用户输入的路径归一成 POSIX 风格的相对路径。

        仅做**词法**归一（不落盘），越界判定交给 :meth:`resolve_path`。
        """
        text = str(raw or "").strip().replace("\\", "/")
        if not text:
            return ""
        # 去掉前导 "./" 与 "/"（相对路径语义）
        while text.startswith("./"):
            text = text[2:]
        text = text.lstrip("/")
        if "\x00" in text:
            raise WorkspaceError(
                "WS_PATH_ESCAPE", "路径包含非法字符（NUL）",
                hint="请使用合法的相对路径。", status_code=400)
        return text

    def resolve_path(self, raw: Any, *, must_exist: bool = True,
                     for_write: bool = False) -> Tuple[Path, str]:
        """把相对路径解析为**绝对路径**，并校验仍在工作区根内。

        这是所有路径操作的**唯一入口**：先 ``resolve()``（展开符号链接 /
        junction）再做 ``commonpath`` 校验，因此符号链接逃逸同样会被拦下。

        Returns:
            ``(绝对路径, 归一后的相对路径)``
        """
        rel = self._normalize_rel(raw)
        if not rel:
            return self.root, ""
        # 绝对路径输入：直接判定越界（Windows 盘符 / UNC）
        if re.match(r"^[A-Za-z]:", rel) or rel.startswith("//"):
            raise WorkspaceError(
                "WS_PATH_ESCAPE",
                f"拒绝访问工作区之外的绝对路径：{rel}",
                hint=f"请使用相对工作区根目录（{self.root}）的相对路径。",
                status_code=403)
        # 词法层面的 ".." 提前拒绝（即使最终仍落在根内，也拒绝以免歧义）
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise WorkspaceError(
                "WS_PATH_ESCAPE",
                f"路径包含 '..'，拒绝访问：{rel}",
                hint="请使用工作区内的相对路径，不要使用上级目录引用。",
                status_code=403)
        target = (self.root / Path(*parts)).resolve()
        try:
            common = os.path.commonpath([str(self.root), str(target)])
        except ValueError:
            common = ""
        if common != str(self.root):
            raise WorkspaceError(
                "WS_PATH_ESCAPE",
                f"路径越出工作区根目录，已拒绝：{rel}",
                hint=f"工作区根目录为 {self.root}，只能访问其中的文件。",
                status_code=403)
        # 敏感文件检查（路径段 + 文件名）
        self._check_forbidden(rel, parts)
        self._check_data_dir(rel)
        if must_exist and not target.exists():
            raise WorkspaceError(
                "WS_NOT_FOUND", f"文件不存在：{rel}",
                hint="请先用目录树接口确认路径。", status_code=404)
        if for_write and target.exists() and target.is_dir():
            raise WorkspaceError(
                "WS_DIR_NOT_FILE", f"目标是目录，不能作为文件写入：{rel}",
                hint="请指定具体文件名。", status_code=400)
        return target, "/".join(parts)

    def _check_forbidden(self, rel: str, parts: Sequence[str]) -> None:
        """敏感路径拦截（目录名 / 文件名 / 特定相对路径）。"""
        for part in parts:
            if part.lower() in FORBIDDEN_DIR_NAMES:
                raise WorkspaceError(
                    "WS_FORBIDDEN_FILE",
                    f"禁止访问目录 '{part}'（{rel}）",
                    hint="该目录包含依赖产物或版本控制数据，不在工作区可访问范围内。",
                    status_code=403)
        name = parts[-1] if parts else ""
        low_rel = rel.lower()
        if any(p.search(low_rel) for p in FORBIDDEN_FILE_PATTERNS):
            raise WorkspaceError(
                "WS_FORBIDDEN_FILE",
                f"禁止访问敏感文件：{rel}",
                hint="密钥 / 环境变量文件不在工作区可访问范围内。",
                status_code=403)
        if low_rel in FORBIDDEN_REL_PATHS:
            raise WorkspaceError(
                "WS_FORBIDDEN_FILE",
                f"禁止访问敏感文件：{rel}",
                hint="该文件包含会话数据或密钥配置，不在工作区可访问范围内。",
                status_code=403)
        for pre in FORBIDDEN_REL_PREFIXES:
            if low_rel.startswith(pre) and low_rel.endswith(".db"):
                raise WorkspaceError(
                    "WS_FORBIDDEN_FILE",
                    f"禁止访问知识库数据库：{rel}",
                    hint="请通过知识库管理接口访问数据库内容。",
                    status_code=403)

    def _check_data_dir(self, rel: str) -> None:
        """拦截对**实际数据目录**（可能已外置）的直接访问。

        背景：``FORBIDDEN_REL_PATHS`` / ``FORBIDDEN_REL_PREFIXES`` 保护的是
        ``<工作区根>/data/`` 下的敏感文件。当数据目录通过环境变量
        ``AGENT_DATA_DIR`` **外置到别的盘**时，这些路径在工作区里已不存在，
        保护自然失效；但如果用户把数据目录指回工作区内部（或工作区根就是
        数据目录所在位置），仍需要拦住对数据库 / 注册表的直接读写。

        这里做一次「数据目录是否落在工作区根内」的判定：
            · 落在工作区外 → 工作区接口本来就访问不到，无需处理；
            · 落在工作区内 → 拦截 ``*.db`` / ``*.db-wal`` / ``*.db-shm`` /
              ``models.json`` / ``custom_tools.json``。
        """
        try:
            from . import paths as _paths  # noqa: PLC0415 - 延迟导入避免循环依赖

            data_root = _paths.data_dir()
        except Exception:  # pragma: no cover - 解析失败不拦截
            return
        try:
            common = os.path.commonpath([str(self.root), str(data_root)])
        except ValueError:  # pragma: no cover - 跨盘符
            return
        if common != str(self.root):
            # 数据目录在工作区之外 —— 工作区接口本来就访问不到
            return
        low_rel = rel.lower()
        if not low_rel:
            return
        name = low_rel.rsplit("/", 1)[-1]
        protected = (
            name.endswith((".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3"))
            or name in ("models.json", "custom_tools.json")
        )
        if protected:
            raise WorkspaceError(
                "WS_FORBIDDEN_FILE",
                f"禁止访问运行时数据文件：{rel}",
                hint="数据目录（可用 AGENT_DATA_DIR 指定）由平台独占管理，"
                     "请通过对应的业务接口访问。",
                status_code=403)

    # ------------------------------------------------------------------
    # 只读：目录树
    # ------------------------------------------------------------------
    def tree(self, path: Any = "", *, depth: int = 1) -> Dict[str, Any]:
        """返回目录树（目录在前，文件按名称排序）。

        Args:
            path: 相对路径（空 = 工作区根）。
            depth: 展开深度（1 = 只列当前层；2 = 列一层子目录）。

        Returns:
            ``{"path":..., "entries":[...], "truncated":bool}``
        """
        target, rel = self.resolve_path(path, must_exist=True)
        if not target.is_dir():
            raise WorkspaceError(
                "WS_DIR_NOT_FILE", f"不是目录：{rel}",
                hint="请使用 /workspace/file 读取文件。", status_code=400)
        d = max(1, min(int(depth or 1), 3))
        entries = self._list_dir(target, rel, depth=d)
        return {"path": rel, "entries": entries, "depth": d}

    def repomap(self, path: Any = "", *, max_files: int = 400,
                max_bytes: int = 64_000_000) -> Dict[str, Any]:
        """返回**扁平化的文件清单**（含行数），供前端 treemap 使用。

        **用途（2026-11-22 新增，可观测性）**：运行观测面板的「代码库图」
        需要按**行数**给每个文件定尺寸，而 ``tree()`` 只给字节数。

        返回：
            ``{"path":..., "files":[{"path","name","dir","lines","size"}],
              "total_lines":int, "file_count":int, "truncated":bool}``

        **安全**：复用 ``tree()`` 的同一套路径校验与禁访问清单
        （``_is_forbidden_name``），不会读到工作区外的文件。
        只读文本文件才统计行数（二进制按 0 行计）。
        """
        target, rel = self.resolve_path(path, must_exist=True)
        if not target.is_dir():
            raise WorkspaceError(
                "WS_DIR_NOT_FILE", f"不是目录：{rel}",
                hint="请使用 /workspace/file 读取文件。", status_code=400)

        out: List[Dict[str, Any]] = []
        total_lines = 0
        total_bytes = 0
        truncated = False

        def _walk(d: Path, drel: str) -> None:
            nonlocal total_lines, total_bytes, truncated
            if truncated:
                return
            try:
                raw = list(d.iterdir())
            except (PermissionError, OSError):
                return
            for p in raw:
                if truncated:
                    return
                try:
                    is_dir = p.is_dir()
                except OSError:
                    continue
                name = p.name
                child_rel = f"{drel}/{name}" if drel else name
                if is_dir:
                    if name.lower() in FORBIDDEN_DIR_NAMES:
                        continue
                    _walk(p, child_rel)
                    continue
                if self._is_forbidden_name(name, child_rel):
                    continue
                if len(out) >= max_files or total_bytes >= max_bytes:
                    truncated = True
                    return
                size = self._safe_size(p)
                lines = self._count_lines(p, size)
                total_lines += lines
                total_bytes += size
                out.append({
                    "path": child_rel,
                    "name": name,
                    "dir": drel,
                    "lines": lines,
                    "size": size,
                    "ext": os.path.splitext(name)[1].lower(),
                })

        _walk(target, rel)
        return {
            "path": rel,
            "files": out,
            "total_lines": total_lines,
            "file_count": len(out),
            "truncated": truncated,
        }

    def _count_lines(self, p: Path, size: int) -> int:
        """统计文本文件的行数（二进制 / 过大文件返回 0）。

        **性能（2026-11-22 修正）**：原实现把整个文件读进内存再 ``decode``
        + ``count("\\n")``，在 /workspace/repomap 扫全仓时要为每个文件
        做一次完整解码，实测整个端点要 17 秒，前端等不起。

        改为**分块二进制扫描**：
            · 只看前 4096 字节判断是不是二进制（含 NUL）；
            · 按 256KB 一块统计 ``\\n``，块之间不丢换行（不跨块拼接）；
            · 末尾非换行结尾时补 1 行。
        不做整体 decode —— 换行统计与编码无关。
        """
        if size <= 0 or size > 512_000:
            return 0
        try:
            with p.open("rb") as fh:
                head = fh.read(4096)
                if not head:
                    return 0
                if b"\x00" in head:
                    return 0          # 二进制
                # 补上第一块（head 可能短于 4096）
                newline = head.count(b"\n")
                last = head[-1:]
                while True:
                    chunk = fh.read(256 * 1024)
                    if not chunk:
                        break
                    newline += chunk.count(b"\n")
                    last = chunk[-1:]
        except (OSError, PermissionError):
            return 0
        if newline == 0:
            return 0
        # 末尾不是换行 → 最后一行没被 ``\n`` 计入，补 1
        return newline + (0 if last == b"\n" else 1)

    def _list_dir(self, target: Path, rel: str, *, depth: int) -> List[Dict[str, Any]]:
        """列目录内容（目录在前、文件按名排序）。"""
        out: List[Dict[str, Any]] = []
        try:
            raw = list(target.iterdir())
        except PermissionError as exc:
            raise WorkspaceError(
                "WS_FORBIDDEN_FILE", f"无权限读取目录：{rel}（{exc}）",
                hint="请换一个目录。", status_code=403) from exc
        except OSError as exc:
            raise WorkspaceError(
                "WS_NOT_FOUND", f"无法读取目录：{rel}（{exc}）",
                hint="请确认路径存在。", status_code=404) from exc

        dirs: List[Dict[str, Any]] = []
        files: List[Dict[str, Any]] = []
        for p in raw:
            try:
                is_dir = p.is_dir()
            except OSError:
                continue
            name = p.name
            child_rel = f"{rel}/{name}" if rel else name
            if is_dir:
                if name.lower() in FORBIDDEN_DIR_NAMES:
                    continue
                item: Dict[str, Any] = {
                    "name": name, "type": "dir", "path": child_rel,
                    "size": 0, "mtime": self._safe_mtime(p),
                    "ext": "", "language": "",
                }
                if depth > 1:
                    item["children"] = self._list_dir(p, child_rel, depth=depth - 1)
                dirs.append(item)
            else:
                if self._is_forbidden_name(name, child_rel):
                    continue
                size = self._safe_size(p)
                files.append({
                    "name": name, "type": "file", "path": child_rel,
                    "size": size, "mtime": self._safe_mtime(p),
                    "ext": os.path.splitext(name)[1].lower(),
                    "language": detect_language(name),
                })
        dirs.sort(key=lambda x: x["name"].lower())
        files.sort(key=lambda x: x["name"].lower())
        return dirs + files

    def _is_forbidden_name(self, name: str, rel: str) -> bool:
        """文件名级敏感判定（目录树遍历时静默跳过，不报错）。"""
        low = rel.lower()
        if any(p.search(low) for p in FORBIDDEN_FILE_PATTERNS):
            return True
        if low in FORBIDDEN_REL_PATHS:
            return True
        for pre in FORBIDDEN_REL_PREFIXES:
            if low.startswith(pre) and low.endswith(".db"):
                return True
        # 实际数据目录（可能已外置）里的运行时数据文件同样跳过
        if self._is_in_data_dir(rel):
            fname = low.rsplit("/", 1)[-1]
            if fname.endswith((".db", ".db-wal", ".db-shm",
                               ".sqlite", ".sqlite3")):
                return True
            if fname in ("models.json", "custom_tools.json"):
                return True
        return False

    def _is_in_data_dir(self, rel: str) -> bool:
        """判断相对路径是否落在**实际数据目录**内。

        数据目录默认在 ``<repo>/data``（工作区根内），因此默认情况下这条
        路径与既有的 ``FORBIDDEN_REL_PATHS`` 规则**重叠但不冲突**；
        外置到别的盘之后则自动失效（工作区接口本就访问不到）。
        """
        try:
            from . import paths as _paths  # noqa: PLC0415 - 延迟导入避免循环依赖

            data_root = _paths.data_dir()
        except Exception:  # pragma: no cover
            return False
        try:
            common = os.path.commonpath([str(self.root), str(data_root)])
        except ValueError:  # pragma: no cover - 跨盘符
            return False
        if common != str(self.root):
            return False
        try:
            rel_common = os.path.commonpath(
                [str(self.root / rel.replace("/", os.sep)), str(data_root)])
        except ValueError:  # pragma: no cover - 跨盘符
            return False
        return rel_common == str(data_root)

    @staticmethod
    def _safe_size(p: Path) -> int:
        try:
            return int(p.stat().st_size)
        except OSError:
            return 0

    @staticmethod
    def _safe_mtime(p: Path) -> float:
        try:
            return float(p.stat().st_mtime)
        except OSError:
            return 0.0

    # ------------------------------------------------------------------
    # 只读：读文件
    # ------------------------------------------------------------------
    def read_file(self, path: Any, *, start_line: Optional[int] = None,
                  end_line: Optional[int] = None) -> Dict[str, Any]:
        """读取文件内容。

        Returns:
            ``{"path","content","encoding","language","size","mtime","truncated",
              "line_count","start_line","end_line"}``
        """
        target, rel = self.resolve_path(path, must_exist=True)
        if target.is_dir():
            raise WorkspaceError(
                "WS_DIR_NOT_FILE", f"目标是目录：{rel}",
                hint="请使用 /workspace/tree 浏览目录。", status_code=400)
        size = self._safe_size(target)
        if size > self.max_file_bytes:
            raise WorkspaceError(
                "WS_TOO_LARGE",
                f"文件过大（{size} 字节 > {self.max_file_bytes} 字节上限）：{rel}",
                hint="请改用 /workspace/search 在大文件内定位关键行。",
                status_code=413)
        raw = target.read_bytes()
        if b"\x00" in raw[:8192]:
            raise WorkspaceError(
                "WS_BINARY", f"二进制文件不可读：{rel}",
                hint="工作区只支持读取文本文件。", status_code=415)
        try:
            text = raw.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            try:
                text = raw.decode("gbk")
                encoding = "gbk"
            except UnicodeDecodeError as exc:
                raise WorkspaceError(
                    "WS_INVALID_ENCODING",
                    f"无法解码文件（既非 UTF-8 也非 GBK）：{rel}",
                    hint="请将文件另存为 UTF-8 后重试。",
                    status_code=415) from exc
        full_lines = text.splitlines()
        total = len(full_lines)
        truncated = False
        if start_line is not None or end_line is not None:
            s = max(1, int(start_line or 1))
            e = int(end_line) if end_line is not None else total
            e = max(s, min(e, total))
            selected = full_lines[s - 1:e]
            truncated = (s > 1) or (e < total)
        else:
            selected = full_lines
        return {
            "path": rel,
            "content": "\n".join(selected),
            "encoding": encoding,
            "language": detect_language(rel),
            "size": size,
            "mtime": self._safe_mtime(target),
            "truncated": truncated,
            "line_count": total,
            "start_line": start_line,
            "end_line": end_line,
        }

    # ------------------------------------------------------------------
    # 只读：代码搜索
    # ------------------------------------------------------------------
    def search(self, query: Any, *, glob: str = "", max_results: int = 50,
               max_files: int = DEFAULT_MAX_SCAN_FILES,
               timeout_s: float = DEFAULT_SEARCH_TIMEOUT,
               case_sensitive: bool = False) -> Dict[str, Any]:
        """在工作区内做代码搜索（类似 grep）。

        限制：单次最多扫描 ``max_files`` 个文件、跳过二进制与忽略目录、
        有超时保护。
        """
        q = str(query or "")
        if not q.strip():
            raise WorkspaceError(
                "WS_INVALID_ARGUMENTS", "搜索关键词不能为空",
                hint="请传入非空的 q 参数。", status_code=400)
        needle = q if case_sensitive else q.lower()
        pattern = re.compile(
            re.escape(q) if case_sensitive else re.escape(q), 0 if case_sensitive else re.IGNORECASE)
        glob_re = None
        if glob:
            glob_re = re.compile(glob.replace("*", ".*").replace("?", "."), re.IGNORECASE)

        deadline = time.monotonic() + max(1.0, float(timeout_s))
        limit = max(1, int(max_results or 50))
        scanned = 0
        skipped_binary = 0
        timed_out = False
        hits: List[Dict[str, Any]] = []
        files_with_hits: List[Dict[str, Any]] = []
        total_hits = 0

        for fpath in self._iter_search_files(max_files=max_files, glob_re=glob_re):
            if time.monotonic() > deadline:
                timed_out = True
                break
            scanned += 1
            try:
                size = fpath.stat().st_size
            except OSError:
                continue
            if size > DEFAULT_MAX_SEARCH_FILE_BYTES:
                continue
            try:
                raw = fpath.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:4096]:
                skipped_binary += 1
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    text = raw.decode("gbk")
                except UnicodeDecodeError:
                    skipped_binary += 1
                    continue
            rel = self._rel_of(fpath)
            file_hits: List[Dict[str, Any]] = []
            for idx, line in enumerate(text.splitlines(), start=1):
                hay = line if case_sensitive else line.lower()
                if needle in hay:
                    file_hits.append({"line": idx, "text": line[:400]})
                    total_hits += 1
                    if len(hits) < limit:
                        hits.append({"path": rel, "line": idx,
                                     "text": line[:400],
                                     "language": detect_language(rel)})
            if file_hits:
                files_with_hits.append({"path": rel, "count": len(file_hits),
                                        "language": detect_language(rel)})
            if len(hits) >= limit:
                break

        return {
            "query": q,
            "glob": glob or "",
            "hits": hits,
            "files": files_with_hits,
            "total_hits": total_hits,
            "scanned_files": scanned,
            "skipped_binary": skipped_binary,
            "max_files": int(max_files),
            "timed_out": timed_out,
            "truncated": timed_out or total_hits > len(hits),
        }

    def _iter_search_files(self, *, max_files: int,
                           glob_re: Optional[re.Pattern]) -> List[Path]:
        """收集待扫描文件（跳过忽略目录，受 max_files 限制）。"""
        out: List[Path] = []
        root = self.root
        for dirpath, dirnames, filenames in os.walk(root):
            d = Path(dirpath)
            dirnames[:] = [x for x in dirnames
                           if x.lower() not in SEARCH_IGNORED_DIRS
                           and not x.startswith(".git")]
            for fn in filenames:
                if len(out) >= max_files:
                    return out
                p = d / fn
                if self._is_forbidden_name(fn, self._rel_of(p)):
                    continue
                if glob_re is not None and not glob_re.search(fn):
                    continue
                out.append(p)
        return out

    def _rel_of(self, p: Path) -> str:
        try:
            return Path(p).resolve().relative_to(self.root).as_posix()
        except Exception:
            return Path(p).name

    # ------------------------------------------------------------------
    # 写：生成待审批改动（不落盘）
    # ------------------------------------------------------------------
    def stage_write(self, path: Any, content: str, *, note: str = "",
                    expected_mtime: Optional[float] = None,
                    change_id: str = "") -> Dict[str, Any]:
        """生成一个**待审批**的文件写入改动（**不落盘**）。

        Returns:
            ``{"change_id","path","action","diff","old_mtime","new_mtime",
              "status":"pending"}``
        """
        computed = self._compute_write(path, content,
                                       expected_mtime=expected_mtime)
        rel = computed["path"]
        text = computed["content"]
        d = computed["diff"]
        cid = change_id or ("ch-" + uuid.uuid4().hex[:12])
        change = {
            "change_id": cid,
            "created_at": time.time(),
            "path": rel,
            "action": "write",
            "status": "pending",
            "payload": {"path": rel, "content": text,
                        "existed": computed["existed"]},
            "diff": d,
            "note": note or "",
            "expected_mtime": expected_mtime,
        }
        with self._lock:
            self._store.save_change(change)
        return {
            "change_id": cid, "path": rel, "action": "write",
            "status": "pending", "diff": d, "old_mtime": computed["old_mtime"],
            "new_mtime": time.time(), "note": note or "",
            "existed": computed["existed"],
        }

    def _compute_write(self, path: Any, content: str, *,
                       expected_mtime: Optional[float] = None
                       ) -> Dict[str, Any]:
        """只**计算**写入结果（不落盘、不落库）。

        供 :meth:`stage_write` 与 :meth:`preview_tool_diff` 共用，
        保证「审批卡片里的 diff」与「真正落盘后的 diff」同源。
        """
        rel = self._normalize_rel(path)
        if not rel:
            raise WorkspaceError(
                "WS_INVALID_ARGUMENTS", "写入路径不能为空",
                hint="请提供相对工作区根目录的路径。", status_code=400)
        target, rel = self.resolve_path(rel, must_exist=False, for_write=True)
        text = str(content if content is not None else "")
        self._assert_writable(rel, target, text)

        existed = target.exists()
        old_text = ""
        old_mtime = None
        if existed:
            old_mtime = self._safe_mtime(target)
            size = self._safe_size(target)
            if size > self.max_file_bytes:
                raise WorkspaceError(
                    "WS_TOO_LARGE",
                    f"文件过大（{size} 字节 > {self.max_file_bytes} 字节），拒绝修改：{rel}",
                    hint="请先手工拆分该文件。", status_code=413)
            raw = target.read_bytes()
            if b"\x00" in raw[:8192]:
                raise WorkspaceError(
                    "WS_BINARY", f"二进制文件不可修改：{rel}",
                    hint="工作区只支持修改文本文件。", status_code=415)
            try:
                old_text = raw.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    old_text = raw.decode("gbk")
                except UnicodeDecodeError as exc:
                    raise WorkspaceError(
                        "WS_INVALID_ENCODING",
                        f"无法解码现有文件（既非 UTF-8 也非 GBK）：{rel}",
                        hint="请将文件另存为 UTF-8 后重试。",
                        status_code=415) from exc

        # 并发冲突检测：用户打开文件后若被外部改过 → 409
        if expected_mtime is not None and existed:
            try:
                want = float(expected_mtime)
            except (TypeError, ValueError):
                want = None
            if want is not None and abs(old_mtime - want) > 1e-6:
                raise WorkspaceError(
                    "WS_FILE_CHANGED",
                    f"文件已被外部修改：{rel}",
                    hint=("磁盘上的 mtime 与你打开时的不一致。"
                          "请重新读取文件后再决定是否覆盖。"),
                    status_code=409,
                    extra={"path": rel, "current_mtime": old_mtime,
                           "expected_mtime": want})

        d = diff_lines(old_text, text, path=rel)
        return {
            "path": rel, "content": text, "old_text": old_text,
            "existed": existed, "old_mtime": old_mtime, "diff": d,
        }

    def stage_edit(self, path: Any, old_text: str, new_text: str, *,
                   replace_all: bool = False, note: str = "",
                   expected_mtime: Optional[float] = None) -> Dict[str, Any]:
        """生成一个**待审批**的精确替换式编辑改动（不落盘）。

        ``old_text`` 必须唯一匹配（除非 ``replace_all=True``），
        否则报 ``WS_EDIT_NOT_UNIQUE`` 并返回候选位置。
        """
        updated, rel = self._compute_edit(
            path, old_text, new_text, replace_all=replace_all)
        return self.stage_write(rel, updated, note=note,
                                expected_mtime=expected_mtime)

    def _compute_edit(self, path: Any, old_text: str, new_text: str, *,
                      replace_all: bool = False) -> Tuple[str, str]:
        """只**计算**替换结果（不落盘、不落库）。"""
        rel = self._normalize_rel(path)
        target, rel = self.resolve_path(rel, must_exist=True, for_write=True)
        current = self.read_file(rel)["content"]
        old = str(old_text if old_text is not None else "")
        new = str(new_text if new_text is not None else "")
        if old == "":
            raise WorkspaceError(
                "WS_INVALID_ARGUMENTS", "old_text 不能为空",
                hint="请给出要替换的原文片段。", status_code=400)
        count = current.count(old)
        if count == 0:
            raise WorkspaceError(
                "WS_EDIT_NOT_FOUND",
                f"在文件中未找到 old_text：{rel}",
                hint="请用 ws_read_file 读取当前内容后再提交编辑。",
                status_code=404)
        if count > 1 and not replace_all:
            candidates = self._line_candidates(current, old)
            raise WorkspaceError(
                "WS_EDIT_NOT_UNIQUE",
                f"old_text 在 {rel} 中出现 {count} 次，无法唯一定位",
                hint=("请提供更长、唯一的 old_text，"
                      "或设置 replace_all=true 替换全部出现位置。"),
                status_code=409,
                extra={"path": rel, "occurrences": count,
                       "candidates": candidates})
        if replace_all:
            updated = current.replace(old, new)
        else:
            updated = current.replace(old, new, 1)
        return updated, rel

    @staticmethod
    def _line_candidates(text: str, needle: str) -> List[Dict[str, Any]]:
        """返回 needle 所有出现位置的行号（供模型定位）。"""
        out: List[Dict[str, Any]] = []
        start = 0
        while True:
            idx = text.find(needle, start)
            if idx < 0:
                break
            line_no = text.count("\n", 0, idx) + 1
            out.append({"line": line_no,
                        "preview": text[max(0, idx - 40):idx + len(needle) + 40]
                        .replace("\n", "\\n")[:120]})
            start = idx + 1
            if len(out) >= 20:
                break
        return out

    def _assert_writable(self, rel: str, target: Path, text: str) -> None:
        """写入前的安全断言：扩展名白名单 + 内容嗅探 + 大小上限。"""
        size = len(text.encode("utf-8"))
        if size > self.max_file_bytes:
            raise WorkspaceError(
                "WS_TOO_LARGE",
                f"写入内容过大（{size} 字节 > {self.max_file_bytes} 字节）：{rel}",
                hint="请分多次写入，或拆分为多个文件。", status_code=413)
        name = os.path.basename(rel)
        ext = os.path.splitext(name)[1].lower()
        base_ok = name.lower() in TEXT_BASENAMES
        if ext and ext not in TEXT_EXTENSIONS and not base_ok:
            raise WorkspaceError(
                "WS_FORBIDDEN_FILE",
                f"拒绝写入非文本文件类型 '{ext}'：{rel}",
                hint=("只允许写入源码 / 配置 / 文本文件"
                      "（.py .js .ts .json .md .yaml .html .css .sh 等）。"),
                status_code=403)
        if not ext and not base_ok:
            raise WorkspaceError(
                "WS_FORBIDDEN_FILE",
                f"拒绝写入无扩展名的未知文件：{rel}",
                hint="请使用带受支持扩展名的文件名。", status_code=403)
        if "\x00" in text[:8192]:
            raise WorkspaceError(
                "WS_BINARY", f"拒绝写入二进制内容：{rel}",
                hint="工作区只支持写入文本文件。", status_code=415)

    # ------------------------------------------------------------------
    # 写：应用改动（人工确认后落盘）
    # ------------------------------------------------------------------
    def apply_change(self, change_id: str, *, actor: str = "",
                     approved: bool = True) -> Dict[str, Any]:
        """应用一个待审批改动（**人工确认之后**才会真正落盘）。

        落盘前：再次做路径安全校验 + 并发冲突检测 + 压入撤销栈；
        落盘时：临时文件 + ``os.replace``（原子）。
        """
        with self._lock:
            change = self._store.get_change(change_id)
            if change is None:
                raise WorkspaceError(
                    "WS_CHANGE_NOT_FOUND", f"改动不存在或已被放弃：{change_id}",
                    hint="请重新发起写入。", status_code=404)
            if change["status"] == "applied":
                raise WorkspaceError(
                    "WS_CHANGE_ALREADY_APPLIED",
                    f"改动已被应用过：{change_id}",
                    hint="无需重复应用。", status_code=409)
            if change["status"] == "discarded":
                raise WorkspaceError(
                    "WS_CHANGE_NOT_FOUND", f"改动已被放弃：{change_id}",
                    hint="请重新发起写入。", status_code=404)
            if not approved:
                self._store.update_change_status(change_id, "discarded")
                raise WorkspaceError(
                    "WS_CHANGE_REJECTED", f"改动已被拒绝：{change_id}",
                    hint="文件未做任何修改。", status_code=400)

            payload = change.get("payload") or {}
            rel = change["path"]
            target, rel = self.resolve_path(rel, must_exist=False, for_write=True)
            text = str(payload.get("content") or "")
            self._assert_writable(rel, target, text)

            existed = target.exists()
            old_text = ""
            old_mtime = None
            if existed:
                old_mtime = self._safe_mtime(target)
                raw = target.read_bytes()
                if b"\x00" in raw[:8192]:
                    raise WorkspaceError(
                        "WS_BINARY", f"二进制文件不可修改：{rel}",
                        hint="工作区只支持修改文本文件。", status_code=415)
                try:
                    old_text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    old_text = raw.decode("gbk", errors="replace")
                # 并发冲突检测（改动生成之后文件又被改过）
                want = change.get("expected_mtime")
                if want is not None and abs(old_mtime - float(want)) > 1e-6:
                    raise WorkspaceError(
                        "WS_FILE_CHANGED",
                        f"文件在改动生成后被外部修改，已放弃本次落盘：{rel}",
                        hint="请重新读取文件并重新发起改动。",
                        status_code=409,
                        extra={"path": rel, "current_mtime": old_mtime,
                               "expected_mtime": want})

            # 撤销栈（旧内容）
            undo_id = self._store.push_undo(
                path=rel, action=change.get("action") or "write",
                existed=existed, old_text=old_text if existed else None,
                new_text=text, old_mtime=old_mtime,
                change_id=change_id,
                summary=f"{'覆盖' if existed else '新建'} {rel}",
            )
            # 原子写
            self._atomic_write(target, text)
            self._store.update_change_status(change_id, "applied")
            new_mtime = self._safe_mtime(target)
            self._store.push_history(
                path=rel, action=change.get("action") or "write",
                summary=("覆盖 " if existed else "新建 ") + rel,
                size=self._safe_size(target), undo_id=undo_id,
                change_id=change_id, actor=actor or "operator")
            self._audit_write(rel=rel, actor=actor, change_id=change_id,
                              undo_id=undo_id, added=(change.get("diff") or {}).get("added", 0),
                              removed=(change.get("diff") or {}).get("removed", 0),
                              existed=existed)
            return {
                "change_id": change_id,
                "path": rel,
                "status": "applied",
                "undo_id": undo_id,
                "old_mtime": old_mtime,
                "new_mtime": new_mtime,
                "size": self._safe_size(target),
                "diff": change.get("diff") or {},
            }

    def _atomic_write(self, target: Path, text: str) -> None:
        """原子写：同目录临时文件 → ``os.replace`` 覆盖。"""
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / (".ws_tmp_" + uuid.uuid4().hex[:10])
        try:
            with open(tmp, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            if target.exists():
                try:
                    shutil.copymode(target, tmp)
                except OSError:
                    pass
            os.replace(str(tmp), str(target))
        except Exception as exc:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise WorkspaceError(
                "WS_WRITE_FAILED", f"写入失败：{type(exc).__name__}: {exc}",
                hint="请检查文件是否被占用（只读 / 权限）。",
                status_code=500) from exc

    # ------------------------------------------------------------------
    # 写：放弃改动
    # ------------------------------------------------------------------
    def discard_change(self, change_id: str) -> Dict[str, Any]:
        """放弃某个待审批改动。"""
        change = self._store.get_change(change_id)
        if change is None:
            raise WorkspaceError(
                "WS_CHANGE_NOT_FOUND", f"改动不存在：{change_id}",
                hint="请确认 change_id。", status_code=404)
        self._store.update_change_status(change_id, "discarded")
        return {"change_id": change_id, "status": "discarded",
                "path": change.get("path", "")}

    # ------------------------------------------------------------------
    # 撤销 / 历史
    # ------------------------------------------------------------------
    def undo(self, *, actor: str = "") -> Dict[str, Any]:
        """撤销最近一次写入（恢复旧内容）。"""
        with self._lock:
            rec = self._store.pop_undo()
            if rec is None:
                raise WorkspaceError(
                    "WS_NO_UNDO", "撤销栈为空，没有可撤销的写入",
                    hint="撤销栈只记录本工作区产生的写入。", status_code=404)
            rel = rec["path"]
            target, rel = self.resolve_path(rel, must_exist=False, for_write=True)
            if rec["existed"]:
                if rec.get("old_text") is None:
                    raise WorkspaceError(
                        "WS_UNDO_FAILED", f"撤销记录缺少旧内容：{rel}",
                        hint="无法自动撤销。", status_code=500)
                self._atomic_write(target, rec["old_text"])
                action = "restored"
            else:
                # 原本不存在 → 撤销 = 删除新建的文件
                if target.exists():
                    try:
                        target.unlink()
                    except OSError as exc:
                        raise WorkspaceError(
                            "WS_UNDO_FAILED", f"撤销（删除）失败：{exc}",
                            hint="请手工删除该文件。", status_code=500) from exc
                action = "deleted"
            self._store.push_history(
                path=rel, action="undo:" + action,
                summary=("撤销并恢复 " if rec["existed"] else "撤销并删除 ") + rel,
                size=self._safe_size(target) if target.exists() else 0,
                undo_id=rec.get("undo_id", ""),
                change_id=rec.get("change_id", ""),
                actor=actor or "operator")
            self._audit_write(rel=rel, actor=actor, change_id=rec.get("change_id", ""),
                              undo_id=rec.get("undo_id", ""), added=0, removed=0,
                              existed=rec["existed"], action="undo:" + action)
            return {
                "undo_id": rec.get("undo_id", ""),
                "path": rel,
                "action": action,
                "existed": bool(rec["existed"]),
                "size": self._safe_size(target) if target.exists() else 0,
                "summary": rec.get("summary", ""),
            }

    def history(self, path: Any = "", *, limit: int = 50) -> Dict[str, Any]:
        """修改历史（某个文件或全部）。

        数据来自 **append-only 的 ``ws_history`` 表**：撤销栈弹出后历史仍在。
        """
        if path:
            target, rel = self.resolve_path(path, must_exist=False)
            items = self._store.list_history(rel, limit=limit)
        else:
            rel = ""
            items = self._store.list_history("", limit=limit)
        return {"path": rel, "history": items,
                "undo_depth": self._store.undo_depth()}

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------
    def _audit_write(self, *, rel: str, actor: str, change_id: str,
                     undo_id: str, added: int, removed: int, existed: bool,
                     action: str = "write") -> None:
        if self._audit is None:
            return
        try:
            self._audit.log(
                action=f"workspace_{action}",
                target=rel,
                args={"path": rel, "change_id": change_id},
                result={"undo_id": undo_id, "added": added,
                        "removed": removed, "existed": existed},
                status="ok",
                actor=actor or "operator",
                extra={"workspace_root": str(self.root)},
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 审批卡片用的 diff 预览
    # ------------------------------------------------------------------
    def preview_tool_diff(self, tool_name: str,
                          args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """在**审批之前**算出写操作的 diff（供 HITL 卡片展示）。

        与真正落盘时的 diff 同源，但**不产生任何副作用**：
        · ``ws_write_file`` → 直接 diff 新内容 vs 现有内容；
        · ``ws_edit_file``  → 先算出替换后的内容再 diff。

        Args:
            tool_name: 工具名。
            args: 工具入参。

        Returns:
            diff 结构；无法预演（如文件不存在 / 参数非法）时返回 None。
        """
        try:
            if tool_name == "ws_write_file":
                computed = self._compute_write(
                    args.get("path", ""), args.get("content", ""))
                return computed.get("diff")
            if tool_name == "ws_edit_file":
                updated, rel = self._compute_edit(
                    args.get("path", ""), args.get("old_text", ""),
                    args.get("new_text", ""),
                    replace_all=bool(args.get("replace_all", False)))
                computed = self._compute_write(rel, updated)
                return computed.get("diff")
        except WorkspaceError:
            return None
        except Exception:
            return None
        return None

    def preview_tool_error(self, tool_name: str,
                           args: Dict[str, Any]) -> Dict[str, Any]:
        """预演写操作并返回**错误信息**（供 HITL 卡片提示用户）。

        与 :meth:`preview_tool_diff` 互补：diff 算不出来时（例如
        ``old_text`` 不唯一），这里给出**具体原因**，让审批人一眼看出
        「这次改动有问题」，而不是稀里糊涂地点批准。
        """
        try:
            if tool_name == "ws_write_file":
                self._compute_write(args.get("path", ""), args.get("content", ""))
            elif tool_name == "ws_edit_file":
                updated, rel = self._compute_edit(
                    args.get("path", ""), args.get("old_text", ""),
                    args.get("new_text", ""),
                    replace_all=bool(args.get("replace_all", False)))
                self._compute_write(rel, updated)
            return {}
        except WorkspaceError as exc:
            out: Dict[str, Any] = {
                "code": exc.code, "message": exc.message, "hint": exc.hint,
            }
            for key in ("occurrences", "candidates", "current_mtime"):
                if key in (exc.extra or {}):
                    out[key] = exc.extra[key]
            return out
        except Exception as exc:  # pragma: no cover - 极端兜底
            return {"code": "WS_ERROR", "message": str(exc), "hint": ""}

    # ------------------------------------------------------------------
    # 元信息
    # ------------------------------------------------------------------
    def info(self) -> Dict[str, Any]:
        """工作区元信息（/health 与前端展示用）。"""
        return {
            "root": str(self.root),
            "max_file_bytes": self.max_file_bytes,
            "undo_depth": self._store.undo_depth(),
            "undo_max_depth": self.undo_depth,
            "store_path": str(self._store.path),
            "forbidden_dirs": sorted(FORBIDDEN_DIR_NAMES),
            "forbidden_files": [".env*", "*.key", "*.pem",
                                "data/kb/*.db", "data/sessions.db",
                                "data/models.json"],
        }
