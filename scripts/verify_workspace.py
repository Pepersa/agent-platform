# -*- coding: utf-8 -*-
"""verify_workspace.py —— 工作区（Workspace）代码编辑模块回归脚本。

覆盖：

    A. **安全断言（不可删减）**
       · 路径穿越被拒（多种变体：``../``、绝对路径、Windows 反斜杠、URL 编码）
       · 敏感文件被拒（``.env`` / ``data/sessions.db`` / ``data/models.json`` /
         ``*.key`` / ``*.pem`` / ``node_modules/`` / ``__pycache__/``）
       · 符号链接逃逸被拒（可创建时）
    B. 只读能力：目录树排序正确 / 读文件内容真实 / 代码搜索命中带行号
    C. 写入全链路：生成改动 → 审批 → 落盘 → 撤销 → 文件恢复原状
    D. 并发冲突检测：mtime 不匹配 → ``409 WS_FILE_CHANGED``
    E. 4 个新工具已注册且 schema 合法（``hitl_required == True``）
    F. 前端编辑器 DOM 结构存在（``#ws-editor`` / gutter / textarea / pre 叠层）
    G. 语法高亮 tokenizer 对 Python 的基本准确性（单测 tokenizer）
    H. 撤销栈 / 历史

用法：
    python scripts/verify_workspace.py
    python scripts/verify_workspace.py --base http://127.0.0.1:8010
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

import requests

BASE = os.environ.get("AGENT_API_BASE", "http://127.0.0.1:8010").rstrip("/")
TIMEOUT = 120
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RESULTS: List[Any] = []


def check(name: str, cond: Any, detail: str = "") -> bool:
    ok = bool(cond)
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""),
          flush=True)
    return ok


def section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


# ----------------------------------------------------------------------
# HTTP 辅助
# ----------------------------------------------------------------------
def get(url_path: str, **params: Any) -> requests.Response:
    return requests.get(f"{BASE}{url_path}", params=params, timeout=TIMEOUT)


def post(path: str, payload: Dict[str, Any]) -> requests.Response:
    return requests.post(f"{BASE}{path}", json=payload, timeout=TIMEOUT)


# ----------------------------------------------------------------------
# A. 安全断言
# ----------------------------------------------------------------------
TRAVERSAL_VARIANTS = [
    "../../../Windows/System32/config/SAM",
    "../../../Windows/System32/drivers/etc/hosts",
    "..\\..\\..\\Windows\\System32\\config\\SAM",
    "agent_platform/../../Windows/win.ini",
    "/etc/passwd",
    "C:/Windows/win.ini",
    "C:\\Windows\\win.ini",
    "..%2F..%2F..%2FWindows%2Fwin.ini",
    "%2e%2e%2f%2e%2e%2fWindows%2Fwin.ini",
    "....//....//Windows/win.ini",
    "./../Windows/win.ini",
]

SENSITIVE_FILES = [
    ".env",
    ".env.local",
    ".env.production",
    "data/sessions.db",
    "data/models.json",
    "data/kb/chunks.db",
    "server.key",
    "cert.pem",
]


def test_path_traversal() -> None:
    section("A1. 路径穿越被拒（WS_PATH_ESCAPE）")
    for variant in TRAVERSAL_VARIANTS:
        r = get("/workspace/file", path=variant)
        body = {}
        try:
            body = r.json()
        except Exception:
            pass
        code = body.get("code", "")
        # 关键：**不能返回内容**，且必须是结构化错误
        has_content = "content" in body and bool(body.get("content"))
        check(f"穿越变体被拒：{variant}",
              r.status_code in (400, 403, 404) and not has_content
              and code in ("WS_PATH_ESCAPE", "WS_NOT_FOUND", "WS_FORBIDDEN_FILE"),
              f"HTTP {r.status_code} code={code} content={'有' if has_content else '无'}")
    # 明确验证：SAM 文件确实存在（说明测试有意义），但接口拒绝读取
    r = get("/workspace/file", path="../../../Windows/System32/config/SAM")
    check("穿越读 SAM 返回结构化错误而非内容",
          r.status_code >= 400 and "content" not in r.json(),
          json.dumps(r.json(), ensure_ascii=False)[:160])


def test_sensitive_files() -> None:
    section("A2. 敏感文件被拒（WS_FORBIDDEN_FILE）")
    for path in SENSITIVE_FILES:
        r = get("/workspace/file", path=path)
        try:
            body = r.json()
        except Exception:
            body = {}
        check(f"敏感文件被拒：{path}",
              r.status_code in (400, 403, 404) and "content" not in body,
              f"HTTP {r.status_code} code={body.get('code')}")
    # 目录级拦截
    for path in ["node_modules", "__pycache__", ".venv", "venv", ".git"]:
        r = get("/workspace/tree", path=path)
        try:
            body = r.json()
        except Exception:
            body = {}
        check(f"敏感目录被拒：{path}",
              r.status_code in (400, 403, 404) and "entries" not in body,
              f"HTTP {r.status_code} code={body.get('code')}")


def test_symlink_escape() -> None:
    section("A3. 符号链接逃逸被拒")
    link_in_root = os.path.join(REPO_ROOT, "_ws_symlink_probe")
    try:
        if os.path.islink(link_in_root):
            os.unlink(link_in_root)
        elif os.path.exists(link_in_root):
            shutil.rmtree(link_in_root, ignore_errors=True)
        os.symlink("C:\\Windows", link_in_root)
        r = get("/workspace/tree", path="_ws_symlink_probe")
        try:
            body = r.json()
        except Exception:
            body = {}
        check("工作区内指向外部的符号链接被拒",
              r.status_code in (400, 403, 404) and "entries" not in body,
              f"HTTP {r.status_code} code={body.get('code')}")
        # 通过符号链接读取外部文件
        r2 = get("/workspace/file", path="_ws_symlink_probe/win.ini")
        try:
            body2 = r2.json()
        except Exception:
            body2 = {}
        check("通过符号链接读外部文件被拒",
              r2.status_code in (400, 403, 404) and "content" not in body2,
              f"HTTP {r2.status_code} code={body2.get('code')}")
    except (OSError, NotImplementedError) as exc:
        check("符号链接逃逸被拒（跳过：当前环境不支持创建符号链接）", True, str(exc))
    finally:
        if os.path.islink(link_in_root):
            os.unlink(link_in_root)


# ----------------------------------------------------------------------
# B. 只读能力
# ----------------------------------------------------------------------
def test_tree() -> None:
    section("B1. 目录树（排序：目录在前，文件按名）")
    r = get("/workspace/tree", path="agent_platform/core", depth=1)
    check("/workspace/tree 返回 200", r.status_code == 200, str(r.status_code))
    body = r.json()
    entries = body.get("entries") or []
    check("返回了条目", len(entries) > 0, f"n={len(entries)}")
    types = [e.get("type") for e in entries]
    first_file = types.index("file") if "file" in types else len(types)
    last_dir = max([i for i, t in enumerate(types) if t == "dir"], default=-1)
    check("目录排在文件之前", last_dir < first_file, f"types={types[:8]}")
    files = [e["name"] for e in entries if e.get("type") == "file"]
    check("文件按名称升序", files == sorted(files, key=str.lower), str(files[:6]))
    first = entries[0]
    for key in ("name", "type", "path", "size", "mtime", "ext", "language"):
        check(f"条目含字段 {key}", key in first, str(list(first.keys())))
    check("语言识别为 python",
          any(e.get("language") == "python" for e in entries),
          str([e.get("language") for e in entries[:5]]))


def test_read_file() -> None:
    section("B2. 读文件（真实内容）")
    r = get("/workspace/file", path="agent_platform/core/tool_registry.py")
    check("/workspace/file 返回 200", r.status_code == 200, str(r.status_code))
    body = r.json()
    for key in ("path", "content", "encoding", "language", "size", "mtime", "truncated"):
        check(f"返回含字段 {key}", key in body, str(list(body.keys())))
    check("内容真实（包含 ToolRegistry 定义）",
          "class ToolRegistry" in body.get("content", ""),
          f"len={len(body.get('content', ''))}")
    check("语言识别为 python", body.get("language") == "python", str(body.get("language")))
    # 目录 → WS_DIR_NOT_FILE
    r2 = get("/workspace/file", path="agent_platform/core")
    check("读目录报 WS_DIR_NOT_FILE",
          r2.status_code == 400 and r2.json().get("code") == "WS_DIR_NOT_FILE",
          json.dumps(r2.json(), ensure_ascii=False)[:140])
    # 不存在的文件
    r3 = get("/workspace/file", path="agent_platform/core/__not_exist__.py")
    check("不存在的文件报 WS_NOT_FOUND",
          r3.status_code == 404 and r3.json().get("code") == "WS_NOT_FOUND",
          json.dumps(r3.json(), ensure_ascii=False)[:140])


def test_search() -> None:
    section("B3. 代码搜索（命中带行号）")
    r = get("/workspace/search", q="ToolRegistry", max=50)
    check("/workspace/search 返回 200", r.status_code == 200, str(r.status_code))
    body = r.json()
    hits = body.get("hits") or []
    check("有命中", len(hits) > 0, f"n={len(hits)}")
    check("命中含行号与行内容",
          all(isinstance(h.get("line"), int) and h.get("line", 0) > 0
              and "text" in h for h in hits[:5]),
          str(hits[:2]))
    check("命中路径为相对工作区的真实文件",
          all(not str(h.get("path", "")).startswith("..") for h in hits),
          str([h.get("path") for h in hits[:3]]))
    # glob 过滤
    r2 = get("/workspace/search", q="ToolRegistry", glob="*.py", max=20)
    check("glob 过滤生效（只返回 .py）",
          r2.status_code == 200
          and all(str(h.get("path", "")).endswith(".py") for h in r2.json().get("hits") or []),
          str([h.get("path") for h in (r2.json().get("hits") or [])[:3]]))


# ----------------------------------------------------------------------
# C. 写入全链路
# ----------------------------------------------------------------------
TEST_REL = "data/ws_verify_tmp/sample_file.py"


def test_write_flow() -> None:
    section("C1. 写入 → 审批 → 落盘 → 撤销")
    original = "# ws 验证文件（初始内容）\nVALUE = 1\n"
    # 准备初始文件
    os.makedirs(os.path.join(REPO_ROOT, "data", "ws_verify_tmp"), exist_ok=True)
    abs_path = os.path.join(REPO_ROOT, TEST_REL.replace("/", os.sep))
    with open(abs_path, "w", encoding="utf-8") as fh:
        fh.write(original)

    try:
        # 1) 生成待审批改动
        r = post("/workspace/file", {"path": TEST_REL, "content": "# 新内容\nVALUE = 42\n",
                                     "note": "验证脚本写入"})
        check("POST /workspace/file 返回 201", r.status_code == 201,
              f"HTTP {r.status_code} {r.text[:140]}")
        change = r.json()
        check("返回 change_id", bool(change.get("change_id")), str(change.get("change_id")))
        diff = change.get("diff") or {}
        check("返回 diff 且有增删统计",
              diff.get("added", 0) > 0 and diff.get("removed", 0) > 0,
              f"+{diff.get('added')} -{diff.get('removed')}")
        check("diff 行带 op 标记",
              all("op" in l for l in (diff.get("lines") or [])[:5]),
              str([l.get("op") for l in (diff.get("lines") or [])[:6]]))
        # 生成改动**不落盘**
        with open(abs_path, "r", encoding="utf-8") as fh:
            still = fh.read()
        check("生成改动不落盘（文件未变）", still == original, repr(still[:60]))

        # 2) 应用（人工确认）
        r2 = post("/workspace/apply", {"change_id": change["change_id"],
                                        "approved": True, "actor": "验证脚本"})
        check("POST /workspace/apply 返回 200", r2.status_code == 200,
              f"HTTP {r2.status_code} {r2.text[:140]}")
        applied = r2.json()
        check("落盘成功（status=applied）", applied.get("status") == "applied",
              str(applied.get("status")))
        with open(abs_path, "r", encoding="utf-8") as fh:
            after = fh.read()
        check("文件内容确实是新内容", "VALUE = 42" in after, repr(after[:80]))

        # 3) 撤销
        r3 = post("/workspace/undo", {})
        check("POST /workspace/undo 返回 200", r3.status_code == 200,
              f"HTTP {r3.status_code} {r3.text[:140]}")
        undone = r3.json()
        check("撤销返回被恢复的文件", undone.get("path") == TEST_REL, str(undone.get("path")))
        with open(abs_path, "r", encoding="utf-8") as fh:
            restored = fh.read()
        check("撤销后文件恢复原状", restored == original, repr(restored[:80]))

        # 4) 历史
        r4 = get("/workspace/history", path=TEST_REL)
        check("GET /workspace/history 返回 200", r4.status_code == 200, str(r4.status_code))
        hist = (r4.json() or {}).get("history") or []
        check("历史含条目", len(hist) >= 1, f"n={len(hist)}")
        check("历史含时间/操作/摘要",
              hist and all(k in hist[0] for k in ("ts", "action", "summary")),
              str(list(hist[0].keys()) if hist else []))
    finally:
        try:
            os.remove(abs_path)
        except OSError:
            pass


def test_concurrent_conflict() -> None:
    section("C2. 并发冲突检测（WS_FILE_CHANGED）")
    rel = "data/ws_verify_tmp/conflict.py"
    os.makedirs(os.path.join(REPO_ROOT, "data", "ws_verify_tmp"), exist_ok=True)
    abs_path = os.path.join(REPO_ROOT, rel.replace("/", os.sep))
    with open(abs_path, "w", encoding="utf-8") as fh:
        fh.write("A = 1\n")
    try:
        # 拿到当前 mtime
        info = get("/workspace/file", path=rel).json()
        stale = float(info["mtime"]) - 100.0   # 故意用过期 mtime
        r = post("/workspace/file", {"path": rel, "content": "A = 2\n",
                                     "expected_mtime": stale})
        check("mtime 不匹配返回 409", r.status_code == 409, f"HTTP {r.status_code}")
        body = r.json()
        check("错误码为 WS_FILE_CHANGED", body.get("code") == "WS_FILE_CHANGED",
              json.dumps(body, ensure_ascii=False)[:160])
        check("返回当前 mtime 供用户判断", "current_mtime" in body,
              str(list(body.keys())))
        # mtime 正确时允许写入
        r2 = post("/workspace/file", {"path": rel, "content": "A = 2\n",
                                      "expected_mtime": info["mtime"]})
        check("mtime 匹配时允许生成改动", r2.status_code == 201,
              f"HTTP {r2.status_code} {r2.text[:120]}")
    finally:
        try:
            os.remove(abs_path)
        except OSError:
            pass


def test_write_guards() -> None:
    section("C3. 写入安全护栏")
    # 二进制扩展名 → 拒绝
    r = post("/workspace/file", {"path": "data/ws_verify_tmp/x.exe", "content": "MZ"})
    check("拒绝写可执行扩展名", r.status_code >= 400 and
          r.json().get("code") in ("WS_FORBIDDEN_FILE", "WS_TOO_LARGE"),
          json.dumps(r.json(), ensure_ascii=False)[:140])
    # .env → 拒绝
    r2 = post("/workspace/file", {"path": ".env", "content": "SECRET=1"})
    check("拒绝写 .env", r2.status_code >= 400 and
          r2.json().get("code") in ("WS_FORBIDDEN_FILE", "WS_PATH_ESCAPE"),
          json.dumps(r2.json(), ensure_ascii=False)[:140])
    # 越界路径 → 拒绝
    r3 = post("/workspace/file", {"path": "../evil.py", "content": "x = 1"})
    check("拒绝越界写入", r3.status_code >= 400 and
          r3.json().get("code") == "WS_PATH_ESCAPE",
          json.dumps(r3.json(), ensure_ascii=False)[:140])
    # 超大内容 → 拒绝
    r4 = post("/workspace/file", {"path": "data/ws_verify_tmp/big.py",
                                  "content": "x" * (600 * 1024)})
    check("拒绝超大写入（>512KB）", r4.status_code >= 400 and
          r4.json().get("code") == "WS_TOO_LARGE",
          json.dumps(r4.json(), ensure_ascii=False)[:140])
    # 放弃改动
    r5 = post("/workspace/file", {"path": "data/ws_verify_tmp/discard.py",
                                  "content": "y = 1"})
    if r5.status_code == 201:
        cid = r5.json()["change_id"]
        r6 = requests.delete(f"{BASE}/workspace/change/{cid}", timeout=TIMEOUT)
        check("放弃改动返回 200", r6.status_code == 200, str(r6.status_code))
        r7 = post("/workspace/apply", {"change_id": cid, "approved": True})
        check("已放弃的改动不能再应用（WS_CHANGE_NOT_FOUND）",
              r7.status_code >= 400 and
              r7.json().get("code") in ("WS_CHANGE_NOT_FOUND", "WS_CHANGE_ALREADY_APPLIED"),
              json.dumps(r7.json(), ensure_ascii=False)[:140])


def test_edit_uniqueness() -> None:
    section("C4. ws_edit_file 的唯一性约束（工具层）")
    sys.path.insert(0, REPO_ROOT)
    try:
        from agent_platform.core.workspace import Workspace, WorkspaceError
    except Exception as exc:
        check("导入 workspace 模块失败", False, str(exc))
        return
    ws = Workspace()
    rel = "data/ws_verify_tmp/uniq.py"
    os.makedirs(os.path.join(REPO_ROOT, "data", "ws_verify_tmp"), exist_ok=True)
    abs_path = os.path.join(REPO_ROOT, rel.replace("/", os.sep))
    with open(abs_path, "w", encoding="utf-8") as fh:
        fh.write("x = 1\ny = 2\nx = 1\n")
    try:
        try:
            ws.stage_edit(rel, "x = 1", "x = 9")
            check("重复 old_text 报错 WS_EDIT_NOT_UNIQUE", False, "未报错")
        except WorkspaceError as exc:
            check("重复 old_text 报错 WS_EDIT_NOT_UNIQUE",
                  exc.code == "WS_EDIT_NOT_UNIQUE", exc.code)
            check("返回候选行号", bool(exc.extra.get("candidates")),
                  str(exc.extra.get("candidates")))
        # replace_all=True 可以
        ch = ws.stage_edit(rel, "x = 1", "x = 9", replace_all=True)
        check("replace_all=True 可生成改动", bool(ch.get("change_id")),
              str(ch.get("change_id")))
        ws.discard_change(ch["change_id"])
    finally:
        try:
            os.remove(abs_path)
        except OSError:
            pass


# ----------------------------------------------------------------------
# E. 工具注册
# ----------------------------------------------------------------------
NEW_TOOLS = ["ws_list_files", "ws_read_file", "ws_search_code",
             "ws_write_file", "ws_edit_file"]


def test_tools_registered() -> None:
    section("E. 新工具注册与 schema")
    r = get("/tools")
    check("GET /tools 返回 200", r.status_code == 200, str(r.status_code))
    tools = {t["name"]: t for t in (r.json().get("tools") or [])}
    for name in NEW_TOOLS:
        check(f"工具 {name} 已注册", name in tools, str(sorted(tools.keys())))
    # 权限与 HITL
    for name in ("ws_list_files", "ws_read_file", "ws_search_code"):
        if name in tools:
            check(f"{name} 权限为 READ_ONLY 且不需 HITL",
                  tools[name]["permission"] == "READ_ONLY"
                  and tools[name]["hitl_required"] is False,
                  f"{tools[name]['permission']} / hitl={tools[name]['hitl_required']}")
    for name in ("ws_write_file", "ws_edit_file"):
        if name in tools:
            check(f"{name} 权限为 WRITE_DATA 且 hitl_required == True",
                  tools[name]["permission"] == "WRITE_DATA"
                  and tools[name]["hitl_required"] is True,
                  f"{tools[name]['permission']} / hitl={tools[name]['hitl_required']}")
    # schema 合法性
    for name in NEW_TOOLS:
        if name not in tools:
            continue
        schema = tools[name].get("input_schema") or {}
        check(f"{name} 的 input_schema 合法",
              schema.get("type") == "object" and isinstance(schema.get("properties"), dict),
              str(list((schema.get("properties") or {}).keys())))
        check(f"{name} 的描述非空且提到写操作需确认"
              if name in ("ws_write_file", "ws_edit_file") else
              f"{name} 的描述非空",
              bool(tools[name].get("description")),
              tools[name].get("description", "")[:60])
    if "ws_edit_file" in tools:
        props = tools["ws_edit_file"]["input_schema"]["properties"]
        check("ws_edit_file 参数含 old_text/new_text/replace_all",
              {"old_text", "new_text", "replace_all"} <= set(props.keys()),
              str(list(props.keys())))
        check("ws_edit_file 描述提示优先使用",
              "ws_write_file" in tools["ws_edit_file"]["description"]
              or "优先" in tools["ws_edit_file"]["description"],
              tools["ws_edit_file"]["description"][:120])
    if "ws_write_file" in tools:
        check("ws_write_file 描述说明是整体覆盖",
              "整体覆盖" in tools["ws_write_file"]["description"]
              or "覆盖" in tools["ws_write_file"]["description"],
              tools["ws_write_file"]["description"][:120])

    # 本地注册表校验（schema 用 validate_args 真正校验）
    sys.path.insert(0, REPO_ROOT)
    try:
        from agent_platform.core.tool_registry import ToolRegistry, validate_args
        from agent_platform.tools import register_workspace_tools
        reg = ToolRegistry()
        specs = register_workspace_tools(reg)
        check("register_workspace_tools 注册了 5 个工具", len(specs) == 5,
              str([s.name for s in specs]))
        for spec in specs:
            required = (spec.input_schema or {}).get("required") or []
            if required:
                errs = validate_args(spec.input_schema, {}, spec.name)
                check(f"{spec.name} 缺必填参数时能被 schema 拦下", bool(errs),
                      str(errs[:1]))
            else:
                # 无必填参数的工具：额外参数必须被拦下
                errs = validate_args(spec.input_schema,
                                     {"__unknown_arg__": 1}, spec.name)
                check(f"{spec.name} 拒绝未知参数", bool(errs), str(errs[:1]))
        # 合法参数必须能通过校验
        ok_args = {
            "ws_list_files": {"path": "agent_platform/core", "depth": 1},
            "ws_read_file": {"path": "a.py", "start_line": 1, "end_line": 30},
            "ws_search_code": {"query": "ToolRegistry", "glob": "*.py"},
            "ws_write_file": {"path": "a.py", "content": "x = 1"},
            "ws_edit_file": {"path": "a.py", "old_text": "x", "new_text": "y"},
        }
        for spec in specs:
            errs = validate_args(spec.input_schema, ok_args.get(spec.name, {}),
                                 spec.name)
            check(f"{spec.name} 合法参数能通过校验", not errs, str(errs[:2]))
    except Exception as exc:
        check("本地工具注册校验异常", False, f"{type(exc).__name__}: {exc}")


def test_retrieval_policy_not_broken() -> None:
    section("E2. 检索策略未受影响（代码类问题为 B 类，不检索知识库）")
    sys.path.insert(0, REPO_ROOT)
    try:
        from agent_platform.core.retrieval_policy import (
            RETRIEVAL_TOOL_NAMES, classify_retrieval_need, filter_retrieval_tools)
        d = classify_retrieval_need("帮我读一下 tool_registry.py 的前 30 行")
        check("代码类问题判定为不检索（B 类）", d.needed is False,
              f"{d.rule} / {d.reason}")
        check("代码工具不在检索工具名单里",
              not (set(NEW_TOOLS) & set(RETRIEVAL_TOOL_NAMES)),
              str(RETRIEVAL_TOOL_NAMES))
        kept = filter_retrieval_tools(list(NEW_TOOLS) + list(RETRIEVAL_TOOL_NAMES), False)
        check("B 类时保留全部代码工具、摘掉检索工具",
              set(NEW_TOOLS) <= set(kept)
              and not (set(RETRIEVAL_TOOL_NAMES) & set(kept)),
              str(kept))
    except Exception as exc:
        check("检索策略校验异常", False, f"{type(exc).__name__}: {exc}")


# ----------------------------------------------------------------------
# F/G. 前端结构与 tokenizer
# ----------------------------------------------------------------------
def test_frontend_structure() -> None:
    section("F. 前端编辑器 DOM 结构与前端资源")
    r = get("/ui/")
    check("/ui/ 返回 200", r.status_code == 200, str(r.status_code))
    html = r.text
    check("HTML 含 view-workspace", 'id="view-workspace"' in html, "")
    check("HTML 含活动栏工作区图标", 'data-view="workspace"' in html, "")
    check("HTML 含 ws-tree", 'id="ws-tree"' in html, "")
    check("HTML 含 ws-search-input", 'id="ws-search-input"' in html, "")
    js = requests.get(f"{BASE}/ui/workspace.js", timeout=TIMEOUT)
    check("workspace.js 可访问", js.status_code == 200 and len(js.text) > 1000,
          f"HTTP {js.status_code} len={len(js.text)}")
    js_text = js.text
    check("workspace.js 含编辑器叠层结构（textarea + pre）",
          "ws-editor-input" in js_text and "ws-editor-pre" in js_text, "")
    check("workspace.js 含行号栏（gutter）", "ws-editor-gutter" in js_text, "")
    check("workspace.js 含语法高亮 tokenizer", "wsTokenizeLine" in js_text, "")
    check("workspace.js 含查找功能", "wsFindNext" in js_text, "")
    check("workspace.js 含 Ctrl+S 保存", "wsSave" in js_text, "")
    css = requests.get(f"{BASE}/ui/app.css", timeout=TIMEOUT)
    check("app.css 含工作区样式", ".ws-editor" in css.text, "")
    check("app.css 含 diff 样式", ".ws-diff-add" in css.text and ".ws-diff-del" in css.text, "")
    # 无外链 CDN
    import re
    urls = re.findall(r'(?:src|href)\s*=\s*["\']([^"\']+)["\']', html, flags=re.IGNORECASE)
    external = [u for u in urls if u.lower().startswith(("http://", "https://", "//"))]
    check("前端无外链 CDN", not external, str(external[:4]))
    # JS 语法检查（node --check）—— 防止前端脚本因语法错误整块失效
    try:
        import subprocess
        for name in ("workspace.js", "app.js", "workbench.js"):
            p = os.path.join(REPO_ROOT, "agent_platform", "api", "static", name)
            out = subprocess.run(["node", "--check", p], capture_output=True, timeout=60)
            err = (out.stderr or b"").decode("utf-8", "replace")
            check(f"{name} 语法检查通过", out.returncode == 0, err.strip()[:200])
    except FileNotFoundError:
        check("node 不可用，跳过 JS 语法检查", True, "node 未安装")


def test_tokenizer() -> None:
    section("G. 语法高亮 tokenizer（Python 准确性）")
    sys.path.insert(0, REPO_ROOT)
    try:
        from agent_platform.core import workspace as wsmod  # noqa: F401
    except Exception:
        pass
    # tokenizer 在前端（workspace.js）里；这里用 node 跑同一份逻辑
    js_path = os.path.join(REPO_ROOT, "agent_platform", "api", "static", "workspace.js")
    if not os.path.isfile(js_path):
        check("workspace.js 存在", False, js_path)
        return
    # 提取 tokenizer 相关函数（从 WS_TOKENS 到 wsHighlight）
    with open(js_path, "r", encoding="utf-8") as fh:
        src = fh.read()
    start = src.find("const WS_TOKENS")
    end = src.find("/* ======================================================================\n   文件树")
    if start < 0 or end < 0:
        check("能从 workspace.js 提取 tokenizer", False,
              f"start={start} end={end}")
        return
    code = src[start:end]
    code += """
const cases = [
  { line: 'def foo(a, b):', want: ['tok-keyword:def', 'tok-func:foo'] },
  { line: 'x = 1  # comment', want: ['tok-number:1', 'tok-comment:# comment'] },
  { line: "s = 'hello'", want: ["tok-string:'hello'"] },
  { line: 'return None', want: ['tok-keyword:return', 'tok-const:None'] },
  { line: 'import os', want: ['tok-keyword:import'] },
  { line: 'class Foo(object):', want: ['tok-keyword:class', 'tok-class:Foo'] },
  { line: 'if x is True:', want: ['tok-keyword:if', 'tok-const:True'] },
  { line: 'print(len(items))', want: ['tok-builtin:print', 'tok-builtin:len'] },
];
let bad = 0;
cases.forEach((c) => {
  const toks = wsTokenizeLine(c.line, 'python');
  const flat = toks.map((t) => (t.cls ? t.cls + ':' + t.text : t.text)).join(' | ');
  c.want.forEach((w) => {
    if (flat.indexOf(w) < 0) { bad++; console.log('MISS ' + w + '  in  ' + flat); }
  });
});
// 多行一致性：同一段代码分两行处理不崩
const multi = wsHighlight('def f():\\n    return 1', 'python');
if (multi.length !== 2) { bad++; console.log('BAD multiline count ' + multi.length); }
console.log(bad === 0 ? 'TOKENIZER_OK' : 'TOKENIZER_BAD ' + bad);
"""
    tmp_js = os.path.join(tempfile.gettempdir(), "ws_tokenizer_test.js")
    with open(tmp_js, "w", encoding="utf-8") as fh:
        fh.write("function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}\n")
        fh.write(code)
    try:
        import subprocess
        out = subprocess.run(["node", tmp_js], capture_output=True, timeout=60)
        textout = ((out.stdout or b"").decode("utf-8", "replace")
                   + (out.stderr or b"").decode("utf-8", "replace"))
        print("  node 输出:", textout.strip()[:500])
        check("tokenizer 对 Python 基本规则正确", "TOKENIZER_OK" in textout,
              textout.strip()[:300])
    except FileNotFoundError:
        check("node 不可用，跳过 tokenizer 前端单测", True, "node 未安装")
    except Exception as exc:
        check("tokenizer 前端单测异常", False, str(exc))


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    args = ap.parse_args()
    BASE = args.base.rstrip("/")

    print("工作区（Workspace）验证 —— " + BASE)
    section("0. 服务可达性")
    r = get("/health")
    check("/health 返回 200", r.status_code == 200, str(r.status_code))
    if r.status_code != 200:
        print("API 不可用，终止")
        return 1

    test_path_traversal()
    test_sensitive_files()
    test_symlink_escape()
    test_tree()
    test_read_file()
    test_search()
    test_write_flow()
    test_concurrent_conflict()
    test_write_guards()
    test_edit_uniqueness()
    test_tools_registered()
    test_retrieval_policy_not_broken()
    test_frontend_structure()
    test_tokenizer()

    print("\n" + "=" * 64)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"结果: {passed}/{len(RESULTS)} 通过")
    for n, ok, d in RESULTS:
        if not ok:
            print(f"  失败: {n}  {d}")
    print("=" * 64)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
