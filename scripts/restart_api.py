# -*- coding: utf-8 -*-
"""开发辅助：重启本地 API 进程（仅用于开发验证，不属于交付功能）。

用法：
    python scripts/restart_api.py            # 重启 8010
    python scripts/restart_api.py --port 8011
    python scripts/restart_api.py --data-dir D:\\agent-data   # 数据目录外置
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

import httpx

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def kill_port(port: int) -> None:
    """结束占用端口的进程（仅本项目自己的 uvicorn）。

    ⚠️ **为什么不用 Get-NetTCPConnection**：在部分 Windows 机器上该 cmdlet
    会卡住 2 分钟以上（实测 125s 未返回），导致 kill 静默失败 →
    新进程绑定端口报 ``[Errno 10048]`` → **旧代码继续对外服务**，
    改了后端却看不到效果。这里改用 ``netstat -ano``（毫秒级）解析 PID。
    """
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=30)
    except Exception:
        return
    pids = set()
    for line in (out.stdout or "").splitlines():
        # 只看 LISTENING 且本地端口匹配的：<proto> <local> <foreign> <state> <pid>
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if parts[3].upper() != "LISTENING":
            continue
        local = parts[1]
        if not local.rsplit(":", 1)[-1] == str(port):
            continue
        pid_text = parts[4]
        if pid_text.isdigit():
            pids.add(int(pid_text))
    for pid in sorted(pids):
        killed = False
        try:
            import psutil  # type: ignore

            proc = psutil.Process(pid)
            cmd = " ".join(proc.cmdline())
            if "uvicorn" not in cmd and "agent_platform" not in cmd:
                print(f"[skip] PID {pid} 不是本项目的 uvicorn，跳过")
                continue
            # 先杀子进程，再杀本体 —— uvicorn 在 Windows 上会带一个 conhost 子进程
            for child in proc.children(recursive=True):
                try:
                    child.kill()
                except Exception:
                    pass
            proc.kill()
            proc.wait(timeout=10)
            killed = True
        except Exception as exc:
            print(f"[warn] psutil 结束 PID {pid} 失败：{exc}，改用 taskkill")
        if not killed:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, text=True)
        print(f"[kill] PID {pid}")


def _find_model_dir(candidates: list[str]) -> str:
    """在候选路径里找第一个真实存在的模型目录。

    Args:
        candidates: 候选路径列表。

    Returns:
        存在的目录；都不存在返回空串。
    """
    for c in candidates:
        if c and os.path.isdir(c):
            return c
    return ""


def _user_env(name: str) -> str:
    """读 Windows **用户级**环境变量（HKCU\\Environment）。

    ★ 为什么需要它（实测于 2026-11-23）：
        用户级环境变量只对**新开的**进程生效。本脚本若是从一个
        「改变量之前就开着」的 PowerShell 窗口启动的，
        ``os.environ`` 里**没有** ``PIP_CACHE_DIR`` / ``HF_HOME``，
        此时直接 ``env.setdefault(...)`` 会写入默认值，
        反而把「缓存外置到 E 盘」的设置顶掉 —— 缓存又长回 C 盘。

        所以这里：先看进程环境（最高优先级），没有再回落到注册表。
        注册表读取失败（非 Windows / 权限不足）时静默返回空串，
        交给调用方的 ``setdefault`` 兜底。

    Args:
        name: 环境变量名。

    Returns:
        变量值；不存在返回空串。
    """
    val = os.environ.get(name, "").strip()
    if val:
        return val
    if os.name != "nt":
        return ""
    try:
        import winreg  # type: ignore  # noqa: PLC0415

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            raw, _ = winreg.QueryValueEx(key, name)
        return str(raw).strip()
    except (FileNotFoundError, OSError):
        return ""


def _apply_cache_env(env: dict[str, str]) -> None:
    """把缓存外置相关的环境变量注入子进程环境。

    处理 ``PIP_CACHE_DIR``（pip 下载缓存）与 ``HF_HOME``
    （HuggingFace 全部缓存），保证子进程 **不会** 把缓存写回 C 盘。

    Args:
        env: 待修改的子进程环境字典（原地修改）。
    """
    for name in ("PIP_CACHE_DIR", "HF_HOME"):
        val = _user_env(name)
        if val:
            env[name] = val
            print(f"[env] {name} = {val}（缓存已外置，不写 C 盘）")
        else:
            print(f"[warn] 未找到 {name}，缓存可能仍落在 C 盘"
                  "（用户级环境变量仅对新进程生效）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--data-dir", default=None,
                    help="数据目录（覆盖环境变量 AGENT_DATA_DIR）")
    args = ap.parse_args()
    port = args.port

    kill_port(port)
    time.sleep(1.5)

    env = dict(os.environ)
    # ---- 数据目录（可用环境变量整体外置到任意盘符）----
    # 优先级：命令行 ``--data-dir`` > 环境变量 ``AGENT_DATA_DIR`` > 项目默认。
    if args.data_dir:
        env["AGENT_DATA_DIR"] = os.path.abspath(os.path.expanduser(args.data_dir))
    env.setdefault("LLM_BASE_URL", "http://127.0.0.1:8080/v1")
    env.setdefault("LLM_MODEL", "gpt-oss-20b")
    env.setdefault("LLM_API_KEY", "local")
    env.setdefault("HF_HUB_OFFLINE", "1")
    # ---- 缓存外置（pip / HuggingFace）→ 强制指向 E 盘 ----
    #: 用户级环境变量只对新进程生效；从旧窗口启动时 os.environ 里没有，
    #: 因此这里从注册表回读一次，避免缓存又长回 C 盘。
    _apply_cache_env(env)
    #: **本机代理绕过**：Windows 系统代理（默认 127.0.0.1:17997）会被 httpx /
    #: openai SDK 应用到**所有** http 请求上，包括访问本机 llama-server
    #: （127.0.0.1:8080 / 8081）。代理若不响应，表现为「/health 一直不返回」
    #: （实测现象）。这里显式把本机地址加入 no_proxy，绕开代理直连。
    _no_proxy = "127.0.0.1,localhost,::1"
    env["NO_PROXY"] = _no_proxy + ("," + env["NO_PROXY"] if env.get("NO_PROXY") else "")
    env["no_proxy"] = env["NO_PROXY"]
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")

    # BGE-M3 / reranker 本地权重（**必须**指向真实目录，否则 Embedder 会降级成
    # hash 伪向量，知识库导入会被 KB_ADMIN_EMBEDDING_DEGRADED 拒绝）
    bge_m3 = _find_model_dir([
        os.environ.get("BGE_M3_PATH", ""),
        os.path.join(REPO_ROOT, "models", "BAAI", "bge-m3"),
        r"E:\model\models\bge-m3",
        os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub",
                     "models--BAAI--bge-m3"),
    ])
    bge_rer = _find_model_dir([
        os.environ.get("BGE_RERANKER_PATH", ""),
        os.path.join(REPO_ROOT, "models", "BAAI", "bge-reranker-v2-m3"),
        r"E:\model\models\bge-reranker-v2-m3",
        os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub",
                     "models--BAAI--bge-reranker-v2-m3"),
    ])
    if bge_m3:
        env["BGE_M3_PATH"] = bge_m3
        print(f"[env] BGE_M3_PATH = {bge_m3}")
    else:
        print("[env] !! 未找到本地 bge-m3 权重，Embedding 会降级为 hash 伪向量")
    if bge_rer:
        env["BGE_RERANKER_PATH"] = bge_rer
        print(f"[env] BGE_RERANKER_PATH = {bge_rer}")

    # ---- BGE 设备强制为 CPU ----
    #: ⚠️ **必须**（实测于 2026-11-23）：FlagEmbedding 的 ``devices=None`` 会
    #: 自动选 CUDA（``AbsEmbedder.py:122``），在 16 GB 单卡上把 bge-m3 +
    #: bge-reranker 拉到 **+5.6 GB 显存**，与常驻的 gpt-oss-20b（15.4 GB）
    #: 合计 22.3 GB > 16.3 GB → OOM → llama-server 抛
    #: ``CUDA error: an illegal memory access`` → **两个推理服务一起崩**。
    #: bge-m3 是纯编码器，CPU 推理完全够用；16 GB 单卡必须留给 LLM。
    #: 需要显式用 GPU 时设 ``BGE_DEVICES=cuda:0``（自行承担 OOM 风险）。
    env.setdefault("BGE_DEVICES", "cpu")
    print(f"[env] BGE_DEVICES = {env['BGE_DEVICES']}"
          "（BGE 强制 CPU，避免与常驻 LLM 抢显存导致 llama-server OOM 崩溃）")

    # ---- 打印最终生效的数据目录 ----
    # 优先级与平台一致：命令行 --data-dir > 环境变量 AGENT_DATA_DIR > 项目默认。
    try:
        sys.path.insert(0, REPO_ROOT)
        from agent_platform.core import paths as _paths  # noqa: PLC0415

        data_dir = _paths.data_dir()
        _paths.ensure_data_dirs()
        source = ("--data-dir" if args.data_dir
                  else ("env" if os.environ.get("AGENT_DATA_DIR", "").strip()
                        else "default"))
        print(f"[env] 数据目录 = {data_dir}  (来源: {source})")
        if not _paths.is_external_data_dir():
            print("[env] （未外置，仍在项目目录内；设置 AGENT_DATA_DIR 可整体挪走）")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 解析数据目录失败：{exc}")
        data_dir = os.path.join(REPO_ROOT, "data")

    out_path = os.path.join(str(data_dir), "api_out.log")
    err_path = os.path.join(str(data_dir), "api_err.log")
    with open(out_path, "w", encoding="utf-8") as fo, open(err_path, "w", encoding="utf-8") as fe:
        # ``--loop`` 指向本项目的 Selector 事件循环工厂：
        # uvicorn >= 0.36 用 loop_factory（不再读事件循环策略），
        # 默认在 win32 上强制 Proactor —— 客户端中途断开 SSE 时
        # 会抛 ``InvalidStateError`` 把**整个 API 进程**搞崩。
        # 详见 ``agent_platform/core/event_loop.py``。
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "agent_platform.api.app:app",
             "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
             "--timeout-keep-alive", "120",
             "--loop", "agent_platform.core.event_loop:selector_loop_factory"],
            cwd=REPO_ROOT, env=env, stdout=fo, stderr=fe,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    for _ in range(60):
        time.sleep(1)
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5)
            if r.status_code == 200:
                # 保险：确认 8010 上跑的是**刚启动的**进程，而不是残留的旧进程。
                # 否则会出现「改了后端代码却不生效」���静默失败。
                if proc.poll() is not None:
                    print("[fail] 新进程已退出，但端口上有旧进程在服务"
                          "（旧代码继续对外响应）。请手动结束占用端口的进程。")
                    print(f"       占用 PID 见 {err_path}")
                    return 1
                print(f"[ok] API 已就绪：http://127.0.0.1:{port}/health")
                return 0
        except Exception:
            continue
    print(f"[fail] API 未在 60s 内就绪，请查看 {err_path}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
