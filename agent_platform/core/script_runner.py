# -*- coding: utf-8 -*-
"""自定义 script 工具的**子进程执行器**（安全沙箱）。

本模块只作为**独立子进程**被调用：
    ``python -m agent_platform.core.script_runner``

协议（stdin / stdout，JSON over pipe）：
    · stdin  ：``{"script_path": str, "function_name": str, "args": dict}``
    · stdout ：一行哨兵 ``__CUSTOM_TOOL_RESULT__`` + ``{"ok":bool,"result":...,"error":str}``

**安全约束（重要）**：
    1. **绝不使用 eval / exec 执行用户提供的代码字符串**；
       脚本以**文件**方式导入执行（``importlib.util.spec_from_file_location``），
       这是「信任用户本地脚本」这一能力的唯一实现路径。
    2. 脚本只能接收 JSON 参数、返回 JSON 结果。
    3. 主进程负责超时 kill 与输出大小上限；本子进程只负责安全地调用函数。
    4. 脚本自己 print 到 stdout 的内容会被保留在哨兵之前，不会影响结果解析。
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import traceback
from typing import Any

#: 结果哨兵（子进程 stdout 中该标记之后是结果 JSON）
RESULT_MARKER = "__CUSTOM_TOOL_RESULT__"


def _load_function(script_path: str, function_name: str) -> Any:
    """按文件路径导入脚本并取出目标函数。

    **不使用 eval / exec**：用 importlib 的文件加载器解析脚本文件。
    """
    spec = importlib.util.spec_from_file_location(
        "custom_tool_script", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载脚本文件：{script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["custom_tool_script"] = module
    spec.loader.exec_module(module)
    fn = getattr(module, function_name, None)
    if fn is None or not callable(fn):
        raise RuntimeError(
            f"脚本中未找到可调用函数 {function_name}（{script_path}）")
    return fn


def main() -> int:
    """子进程入口。"""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw else {}
    except Exception as exc:
        sys.stdout.write(RESULT_MARKER + json.dumps(
            {"ok": False, "error": f"请求解析失败：{exc}"}, ensure_ascii=False) + "\n")
        return 2

    script_path = str(payload.get("script_path") or "")
    function_name = str(payload.get("function_name") or "")
    args = payload.get("args")
    if not isinstance(args, dict):
        args = {}

    # 把脚本自身 print 的内容重定向到 stderr，避免污染 stdout 的结果协议
    orig_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        fn = _load_function(script_path, function_name)
        result = fn(args)
        # 结果必须可 JSON 序列化
        json.dumps(result, ensure_ascii=False, default=str)
        envelope = {"ok": True, "result": result}
        code = 0
    except Exception as exc:
        tb = traceback.format_exc(limit=8)
        envelope = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "traceback": tb[-1500:]}
        code = 1
    finally:
        sys.stdout = orig_stdout

    sys.stdout.write(RESULT_MARKER + json.dumps(
        envelope, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
