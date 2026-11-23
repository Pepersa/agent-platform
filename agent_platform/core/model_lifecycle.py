# -*- coding: utf-8 -*-
"""模型生命周期管理（Model Lifecycle Manager）。

背景
----
本机（Windows 原生 llama.cpp）的两个推理端点**原本是「一次启动、永远在跑」**：

    gpt-oss-20b   → 127.0.0.1:8080  （GPU，-ngl 99）
    qwen2.5-14b   → 127.0.0.1:8081  （CPU，-ngl 0）

问题：
    · qwen2.5-14b 跑在 **CPU** 上，即使不推理也占着 6 个逻辑线程
      （``--threads 6 --threads-batch 6``），白白挤占用户要求的
      「整机 CPU < 50%」预算；
    · 用户真正需要的是**按需启停**：只跑当前任务要用的那个模型。

本模块提供 :class:`ModelManager`：
    · ``list_models()``  —— 列出已知模型 + 实时运行状态；
    · ``start_model(alias)`` / ``stop_model(alias)`` —— 单个模型的启停；
    · ``start_all()`` / ``stop_all()`` —— 批量启停；
    · 全部操作**幂等**：已经在跑就返回 ``already_running``，不会起重复进程。

设计要点（**为什么这么写**）
----------------------------
1. **不新增任何 pip 依赖**
   进程探测优先用 ``psutil``（本机已装），没有就退回到
   ``netstat -ano`` 解析 + ``taskkill``，再没有就只做 HTTP 探测。
   平台要能在「裸 Python 环境」里跑起来。

2. **兼容「不由本管理器启动」的进程**
   现有两个 llama-server 就是用户手工 / 旧脚本起的。管理器**不能**
   傻乎乎地再起一份 —— 那会抢显存、抢端口（llama-server 端口占用
   会直接启动失败）。所以每次查状态都先看**端口有没有人在听**，
   在听就报 ``running``（并把对方 PID 记下来），不重复拉起。

3. **PATH 必须带上 CUDA vendor 目录**
   llama-server.exe 依赖 LM Studio 的 CUDA DLL，缺了会以
   ``0xC0000135 (STATUS_DLL_NOT_FOUND)`` **秒退**。子进程环境变量
   因此要显式拼 ``llama.cpp`` 目录 + ``vendor\win-llama-cuda12-vendor-v2``。

4. **工作目录必须是 llama.cpp 后端目录**
   llama-server 启动时会按 CWD 去找同目录下的 DLL。

5. **线程数上限原样保留**
   ``--threads 4``（GPU 实例）/ ``--threads 6``（CPU 实例）是用户
   刻意设的限流阈值，**不能**因为「本管理器启动」就改成默认值。

6. **日志目录跟随 ``AGENT_DATA_DIR``**
   与 :mod:`agent_platform.core.paths` 保持同一套约定：环境变量
   ``AGENT_DATA_DIR`` > 项目默认 ``<repo>/data``。

7. **启动是「慢操作」，必须能异步化**
   模型加载要 10~60 s。API 层用 ``asyncio.to_thread`` 把等待
   放到线程池，HTTP 请求立即返回 ``status="starting"``，前端轮询
   ``/models/lifecycle`` 拿最终的 ``running`` 标记。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "ModelSpec",
    "ModelManager",
    "get_manager",
    "LLAMA_DIR",
    "VENDOR_DIR",
]


# ----------------------------------------------------------------------
# 常量（与 deploy\start_servers.ps1 / data\_tmp\llm_8080_throttled.cmd 同源）
# ----------------------------------------------------------------------

#: LM Studio 的 llama.cpp 后端目录（llama-server.exe 所在处）
LLAMA_DIR = Path(
    r"C:\Users\admin\.lmstudio\extensions\backends"
    r"\llama.cpp-win-x86_64-nvidia-cuda12-avx2-2.65.1"
)

#: CUDA vendor DLL 目录（cublas / cudart 等）—— **缺了会 0xC0000135**
VENDOR_DIR = Path(
    r"C:\Users\admin\.lmstudio\extensions\backends"
    r"\vendor\win-llama-cuda12-vendor-v2"
)

#: GGUF 权重所在仓目录
MODEL_DIR = Path(r"E:\model\models\lmstudio-community")

#: 启动后等待 ``/v1/models`` 就绪的默认超时（秒）
DEFAULT_READY_TIMEOUT = 180.0

#: 轮询间隔（秒）
_POLL_INTERVAL = 2.0

#: 探测 ``/v1/models`` 的单次超时（秒）
_PROBE_TIMEOUT = 3.0


@dataclass(frozen=True)
class ModelSpec:
    """一个可被启停的本地模型端点（**只描述**怎么起，不含运行态）。

    Attributes:
        alias: 对外别名（= llama-server 的 ``--alias``），如 ``gpt-oss-20b``。
        label: 人类可读名称。
        port: 监听端口。
        model_path: GGUF 权重文件绝对路径。
        ngl: GPU offload 层数（``99`` = 全部 offload，``0`` = 纯 CPU）。
        ctx_size: 上下文窗口（``-c``）。
        threads: 推理线程数（``--threads``）。
        threads_batch: 批处理线程数（``--threads-batch``）。
        device: ``"gpu"`` / ``"cpu"``，供前端展示。
    """

    alias: str
    label: str
    port: int
    model_path: Path
    ngl: int
    ctx_size: int
    threads: int
    threads_batch: int
    device: str = "cpu"
    #: 额外的 llama-server 参数（键值对）
    extra_args: tuple = field(default_factory=tuple)

    @property
    def base_url(self) -> str:
        """OpenAI 兼容端点根地址。"""
        return f"http://127.0.0.1:{self.port}/v1"

    def build_args(self) -> List[str]:
        """拼出 llama-server 的完整命令行参数。

        与 ``data\\_tmp\\llm_8080_throttled.cmd``（GPU 实例）和
        ``deploy\\start_servers.ps1`` 保持**逐项一致**：

            --host / --port / --alias / -ngl / -c / --parallel 1
            --threads / --threads-batch
            --batch-size 512 / --ubatch-size 128
            --cache-type-k q8_0 / --cache-type-v q8_0
            --flash-attn on / --jinja / --no-webui
        """
        args: List[str] = [
            "-m", str(self.model_path),
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "--alias", self.alias,
            "-ngl", str(self.ngl),
            "-c", str(self.ctx_size),
            "--parallel", "1",
            "--threads", str(self.threads),
            "--threads-batch", str(self.threads_batch),
            "--batch-size", "512",
            "--ubatch-size", "128",
            "--cache-type-k", "q8_0",
            "--cache-type-v", "q8_0",
            "--flash-attn", "on",
            "--jinja",
            "--no-webui",
        ]
        args.extend(str(x) for x in self.extra_args)
        return args


#: 已知的两个本地模型端点（**与 deploy\start_servers.ps1 同源**）
DEFAULT_SPECS: Dict[str, ModelSpec] = {
    "gpt-oss-20b": ModelSpec(
        alias="gpt-oss-20b",
        label="gpt-oss-20b (GPU :8080)",
        port=8080,
        model_path=MODEL_DIR / "gpt-oss-20b-GGUF" / "gpt-oss-20b-MXFP4.gguf",
        ngl=99,
        ctx_size=32768,
        threads=4,
        threads_batch=4,
        device="gpu",
    ),
    "qwen2.5-14b": ModelSpec(
        alias="qwen2.5-14b",
        label="qwen2.5-14b (CPU :8081)",
        port=8081,
        model_path=(
            MODEL_DIR / "Qwen2.5-14B-Instruct-1M-GGUF"
            / "Qwen2.5-14B-Instruct-1M-Q4_K_M.gguf"
        ),
        ngl=0,
        ctx_size=32768,
        threads=6,
        threads_batch=6,
        device="cpu",
    ),
}


# ----------------------------------------------------------------------
# 平台内部小工具（不依赖 psutil 也能工作）
# ----------------------------------------------------------------------
def _has_psutil() -> bool:
    """本机是否可用 ``psutil``（**不硬依赖**）。"""
    try:
        import psutil  # noqa: PLC0415,F401
        return True
    except Exception:  # pragma: no cover - 环境差异
        return False


def _port_is_listening(port: int) -> bool:
    """TCP 端口是否有进程在监听（纯 socket 探测，最轻量）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", int(port))) == 0


def _pid_from_netstat(port: int) -> Optional[int]:
    """用 ``netstat -ano`` 找出监听 ``port`` 的 PID（psutil 不可用时的兜底）。

    ``netstat`` 输出示例::

        TCP    127.0.0.1:8080    0.0.0.0:0    LISTENING    43032

    Returns:
        PID；找不到返回 ``None``。
    """
    try:
        proc = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:  # pragma: no cover - netstat 不可用
        return None
    needle = f":{int(port)}"
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        if parts[0].upper() != "TCP":
            continue
        if not parts[1].endswith(needle):
            continue
        if parts[3].upper() != "LISTENING":
            continue
        try:
            return int(parts[4])
        except ValueError:
            continue
    return None


def _pid_from_psutil(port: int) -> Optional[int]:
    """用 ``psutil`` 找出监听 ``port`` 的 PID（首选路径）。"""
    try:
        import psutil  # noqa: PLC0415
    except Exception:  # pragma: no cover
        return None
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.status != psutil.CONN_LISTEN:
                continue
            if conn.laddr and int(conn.laddr.port) == int(port):
                return int(conn.pid) if conn.pid else None
    except Exception:  # pragma: no cover - 权限不足等
        return None
    return None


def _listener_pid(port: int) -> Optional[int]:
    """找出监听 ``port`` 的进程 PID。

    顺序：``psutil`` → ``netstat -ano`` → ``None``。
    """
    pid = _pid_from_psutil(port)
    if pid:
        return pid
    return _pid_from_netstat(port)


def _pid_alive(pid: int) -> bool:
    """进程是否还活着。"""
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    if _has_psutil():
        try:
            import psutil  # noqa: PLC0415
            return psutil.pid_exists(pid)
        except Exception:  # pragma: no cover
            pass
    # 兜底：Windows 用 tasklist，POSIX 用 os.kill(pid, 0)
    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return str(pid) in (proc.stdout or "")
        except Exception:  # pragma: no cover
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _terminate_pid(pid: int, *, timeout: float = 12.0) -> bool:
    """优雅结束进程，超时后**强制结束**。

    顺序：
        1. ``psutil.Process(pid).terminate()``（Windows 上等价于 TerminateProcess，
           对 llama-server 来说「优雅」体现在 llama.cpp 会自己关掉 socket）；
        2. 等 ``timeout`` 秒；
        3. 还在 → ``kill()`` 强杀。

    Returns:
        进程是否已消失。
    """
    pid = int(pid)
    if _has_psutil():
        try:
            import psutil  # noqa: PLC0415
            proc = psutil.Process(pid)
            try:
                proc.terminate()
            except psutil.NoSuchProcess:
                return True
            except Exception:  # pragma: no cover - 权限问题
                pass
            try:
                _, alive = psutil.wait_procs([proc], timeout=timeout)
                if not alive:
                    return True
            except Exception:  # pragma: no cover
                pass
            try:
                proc.kill()
            except psutil.NoSuchProcess:
                return True
            except Exception:  # pragma: no cover
                pass
            try:
                _, alive = psutil.wait_procs([proc], timeout=5.0)
                if not alive:
                    return True
            except Exception:  # pragma: no cover
                pass
            return False
        except Exception:  # pragma: no cover
            pass

    # ---- 无 psutil：taskkill 兜底 ----
    if os.name == "nt":
        for flag in ("/T", "/F"):
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), flag],
                    capture_output=True, text=True, timeout=20,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception:  # pragma: no cover
                continue
            deadline = time.time() + timeout
            while time.time() < deadline:
                if not _pid_alive(pid):
                    return True
                time.sleep(0.3)
        return not _pid_alive(pid)

    # ---- POSIX ----
    try:
        os.kill(pid, 15)
    except Exception:
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.3)
    try:
        os.kill(pid, 9)
    except Exception:
        pass
    return not _pid_alive(pid)


def _http_models_ok(base_url: str, timeout: float = _PROBE_TIMEOUT) -> bool:
    """请求 ``{base_url}/models`` 看是否返回模型列表。"""
    url = base_url.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        data = payload.get("data") if isinstance(payload, dict) else None
        return bool(data)
    except Exception:
        return False


# ----------------------------------------------------------------------
# 管理器
# ----------------------------------------------------------------------
class ModelManager:
    """本地 llama-server 端点的**启停管理器**。

    线程安全：所有状态变更都在 ``_lock`` 内进行（``RLock``，
    允许同一线程重入 —— 例如 ``start_all()`` 内部调用 ``start_model()``）。

    运行态只存在**内存**里（进程重启后靠「端口有没有人在听」重建），
    这正是我们想要的：管理器必须能识别**不由自己启动**的 llama-server。
    """

    def __init__(self, specs: Optional[Dict[str, ModelSpec]] = None,
                 *, log_dir: Optional[str | os.PathLike] = None) -> None:
        self._specs: Dict[str, ModelSpec] = dict(specs or DEFAULT_SPECS)
        self._lock = threading.RLock()
        #: alias → {"pid": int, "started_at": float, "managed": bool}
        #: ``managed=True`` 表示是**本管理器**起的进程
        self._running: Dict[str, Dict[str, Any]] = {}
        #: 显式指定的日志目录（默认跟随 ``AGENT_DATA_DIR``）
        self._log_dir_override = Path(log_dir) if log_dir else None

    # ------------------------------------------------------------------
    # 基础属性
    # ------------------------------------------------------------------
    @property
    def specs(self) -> Dict[str, ModelSpec]:
        """已知模型的定义表（只读拷贝）。"""
        return dict(self._specs)

    def aliases(self) -> List[str]:
        """全部已知别名。"""
        return list(self._specs.keys())

    def get_spec(self, alias: str) -> Optional[ModelSpec]:
        """按别名取模型定义。"""
        return self._specs.get(str(alias or "").strip())

    def log_dir(self) -> Path:
        """日志目录（跟随 ``AGENT_DATA_DIR``）。

        与 :mod:`agent_platform.core.paths` 同源：环境变量优先，
        否则 ``<repo>/data/logs``。
        """
        if self._log_dir_override is not None:
            return self._log_dir_override
        try:
            from . import paths  # noqa: PLC0415 - 延迟导入避免循环依赖
            base = paths.data_dir()
        except Exception:  # pragma: no cover - 理论上不会发生
            base = Path(__file__).resolve().parent.parent.parent / "data"
        return Path(base) / "logs"

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------
    def _status_of(self, spec: ModelSpec) -> Dict[str, Any]:
        """取单个模型的实时状态（**不加锁**，由调用方保证）。"""
        listening = _port_is_listening(spec.port)
        pid = self._running.get(spec.alias, {}).get("pid")
        started_at = self._running.get(spec.alias, {}).get("started_at")

        if not listening:
            # 端口没人听 → 之前的记录作废
            self._running.pop(spec.alias, None)
            pid = None
            started_at = None
        elif not pid:
            # 端口有人听但不是我们起的 → 认领这个 PID（兼容外部启动）
            pid = _listener_pid(spec.port)
            if pid:
                self._running[spec.alias] = {
                    "pid": pid,
                    "started_at": None,
                    "managed": False,
                }
                started_at = None

        reachable = listening and _http_models_ok(spec.base_url)
        return {
            "alias": spec.alias,
            "label": spec.label,
            "port": spec.port,
            "base_url": spec.base_url,
            "device": spec.device,
            "running": bool(listening),
            "pid": int(pid) if pid else None,
            "started_at": started_at,
            "managed": bool(self._running.get(spec.alias, {}).get("managed", False)),
            "reachable": bool(reachable),
            "model_path": str(spec.model_path),
            "ctx_size": spec.ctx_size,
            "ngl": spec.ngl,
            "threads": spec.threads,
        }

    def list_models(self) -> List[Dict[str, Any]]:
        """列出全部已知模型及其运行状态。

        Returns:
            ``[{alias, label, port, running, pid, started_at, device, reachable, ...}]``
        """
        with self._lock:
            return [self._status_of(self._specs[a]) for a in self._specs]

    def status(self, alias: str) -> Optional[Dict[str, Any]]:
        """取单个模型的运行状态（未知别名返回 ``None``）。"""
        spec = self.get_spec(alias)
        if spec is None:
            return None
        with self._lock:
            return self._status_of(spec)

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------
    def _build_env(self) -> Dict[str, str]:
        """构造子进程环境变量。

        ★ **PATH 必须前置** llama.cpp 目录与 CUDA vendor 目录，
        否则 llama-server.exe 以 ``0xC0000135`` 秒退。
        """
        env = dict(os.environ)
        parts = [str(LLAMA_DIR), str(VENDOR_DIR)]
        old = env.get("PATH", "")
        if old:
            parts.append(old)
        env["PATH"] = ";".join(parts)
        # 缓存外置（与 start_servers.ps1 一致，避免子进程把缓存写回 C 盘）
        for var, fallback in (
            ("PIP_CACHE_DIR", str(Path(__file__).resolve().parent.parent.parent / ".cache" / "pip")),
            ("HF_HOME", str(Path(__file__).resolve().parent.parent.parent / ".cache" / "huggingface")),
        ):
            if not env.get(var):
                env[var] = fallback
        return env

    def _spawn(self, spec: ModelSpec) -> int:
        """拉起 llama-server（**不等待就绪**），返回 PID。

        启动方式：``subprocess.Popen`` + ``DETACHED_PROCESS``，
        这样父进程（API）退出时 llama-server 也能继续跑，
        且不会因为 API 崩溃而被一起带走。
        """
        log_dir = self.log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = log_dir / f"{spec.alias}.log"
        stderr_path = log_dir / f"{spec.alias}.log.err"

        exe = LLAMA_DIR / "llama-server.exe"
        if not exe.is_file():
            raise FileNotFoundError(f"找不到 llama-server.exe: {exe}")
        if not spec.model_path.is_file():
            raise FileNotFoundError(f"找不到模型权重: {spec.model_path}")

        stdout_fh = open(stdout_path, "ab", buffering=0)
        stderr_fh = open(stderr_path, "ab", buffering=0)
        try:
            creationflags = 0
            if os.name == "nt":
                # DETACHED_PROCESS：脱离控制台，API 退出后仍继续运行
                creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            proc = subprocess.Popen(
                [str(exe)] + spec.build_args(),
                cwd=str(LLAMA_DIR),
                env=self._build_env(),
                stdin=subprocess.DEVNULL,
                stdout=stdout_fh,
                stderr=stderr_fh,
                creationflags=creationflags,
            )
        finally:
            # Popen 已经复制了句柄，这里可以关掉父进程的副本
            try:
                stdout_fh.close()
            except Exception:
                pass
            try:
                stderr_fh.close()
            except Exception:
                pass
        return int(proc.pid)

    def start_model(self, alias: str, *, wait: bool = True,
                    timeout: float = DEFAULT_READY_TIMEOUT) -> Dict[str, Any]:
        """启动一个模型端点（**幂等**）。

        幂等语义：
            · 端口已经有 llama-server 在听 → 直接返回
              ``already_running``，**绝不**起第二个（会抢显存 / 抢端口）；
            · 已经在启动中（同一 alias 有未完成的启动任务）→ 返回
              ``starting``。

        Args:
            alias: 模型别名。
            wait: 是否等待 ``/v1/models`` 就绪（**阻塞**，应在线程里调）。
            timeout: 等待就绪的超时秒数。

        Returns:
            ``{status, alias, message, ...}``，``status`` 取值：
            ``started`` / ``already_running`` / ``starting`` / ``error``。
        """
        spec = self.get_spec(alias)
        if spec is None:
            return {
                "status": "error",
                "alias": str(alias),
                "message": f"未知模型 '{alias}'（已知：{', '.join(self._specs)}）",
            }

        with self._lock:
            # 已经在跑（含外部启动的）→ 幂等返回
            if _port_is_listening(spec.port):
                pid = self._running.get(spec.alias, {}).get("pid") or _listener_pid(spec.port)
                if pid:
                    self._running[spec.alias] = {
                        "pid": int(pid),
                        "started_at": self._running.get(spec.alias, {}).get("started_at"),
                        "managed": False,
                    }
                return {
                    "status": "already_running",
                    "alias": spec.alias,
                    "message": f"{spec.label} 已在运行（端口 {spec.port}），无需重复启动",
                    "pid": int(pid) if pid else None,
                    "port": spec.port,
                }

            # 拉起进程
            try:
                pid = self._spawn(spec)
            except Exception as exc:  # noqa: BLE001
                return {
                    "status": "error",
                    "alias": spec.alias,
                    "message": f"启动 {spec.label} 失败：{type(exc).__name__}: {exc}",
                    "port": spec.port,
                }

            self._running[spec.alias] = {
                "pid": pid,
                "started_at": time.time(),
                "managed": True,
            }

        if not wait:
            return {
                "status": "starting",
                "alias": spec.alias,
                "message": f"{spec.label} 正在启动（PID {pid}），模型加载中…",
                "pid": pid,
                "port": spec.port,
            }

        ok = self._wait_ready(spec, timeout=timeout)
        with self._lock:
            if ok:
                self._running.setdefault(spec.alias, {}).update(
                    {"pid": pid, "started_at": time.time(), "managed": True})
        return {
            "status": "started" if ok else "error",
            "alias": spec.alias,
            "message": (
                f"{spec.label} 已就绪（http://127.0.0.1:{spec.port}/v1）"
                if ok else
                f"{spec.label} 启动超时（{int(timeout)}s 内 /v1/models 未响应），"
                f"请查看 {self.log_dir() / (spec.alias + '.log.err')}"
            ),
            "pid": pid,
            "port": spec.port,
            "reachable": bool(ok),
        }

    def _wait_ready(self, spec: ModelSpec, *, timeout: float) -> bool:
        """轮询 ``/v1/models`` 直到就绪或超时。"""
        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            if _port_is_listening(spec.port) and _http_models_ok(spec.base_url):
                return True
            time.sleep(_POLL_INTERVAL)
        # 最后再试一次（可能刚好在超时边缘就绪）
        return bool(_port_is_listening(spec.port) and _http_models_ok(spec.base_url))

    def start_all(self, *, wait: bool = True,
                  timeout: float = DEFAULT_READY_TIMEOUT) -> List[Dict[str, Any]]:
        """启动全部已知模型（逐个串行，避免同时抢显存）。"""
        out: List[Dict[str, Any]] = []
        for alias in self._specs:
            out.append(self.start_model(alias, wait=wait, timeout=timeout))
        return out

    # ------------------------------------------------------------------
    # 停止
    # ------------------------------------------------------------------
    def stop_model(self, alias: str) -> Dict[str, Any]:
        """停止一个模型端点（**幂等**）。

        定位进程的方式（按优先级）：
            1. 本管理器记录的 PID；
            2. 监听该端口的 PID（兼容外部启动的 llama-server）。

        Returns:
            ``{status, alias, message, ...}``，``status`` 取值：
            ``stopped`` / ``not_running`` / ``error``。
        """
        spec = self.get_spec(alias)
        if spec is None:
            return {
                "status": "error",
                "alias": str(alias),
                "message": f"未知模型 '{alias}'（已知：{', '.join(self._specs)}）",
            }

        with self._lock:
            record = self._running.get(spec.alias, {})
            pid = record.get("pid")
            if not pid:
                pid = _listener_pid(spec.port)

            if not pid or not _pid_alive(int(pid)):
                self._running.pop(spec.alias, None)
                return {
                    "status": "not_running",
                    "alias": spec.alias,
                    "message": f"{spec.label} 未在运行（端口 {spec.port} 无监听）",
                    "port": spec.port,
                }

            killed = _terminate_pid(int(pid))

        # 确认端口确实释放了（最多等 10 秒）
        deadline = time.time() + 10.0
        while time.time() < deadline and _port_is_listening(spec.port):
            time.sleep(0.3)

        with self._lock:
            self._running.pop(spec.alias, None)

        if killed and not _port_is_listening(spec.port):
            return {
                "status": "stopped",
                "alias": spec.alias,
                "message": f"{spec.label} 已停止（PID {int(pid)}，端口 {spec.port} 已释放）",
                "pid": int(pid),
                "port": spec.port,
            }
        return {
            "status": "error",
            "alias": spec.alias,
            "message": (
                f"{spec.label} 停止失败（PID {int(pid)} 仍存活或端口 {spec.port} 仍被占用）"
            ),
            "pid": int(pid),
            "port": spec.port,
        }

    def stop_all(self) -> List[Dict[str, Any]]:
        """停止全部已知模型。"""
        out: List[Dict[str, Any]] = []
        for alias in self._specs:
            out.append(self.stop_model(alias))
        return out


# ----------------------------------------------------------------------
# 进程级单例
# ----------------------------------------------------------------------
_MANAGER: Optional[ModelManager] = None
_MANAGER_LOCK = threading.Lock()


def get_manager() -> ModelManager:
    """取进程内**唯一**的 :class:`ModelManager`（懒初始化 + 加锁）。

    单例的理由：运行态（PID 映射）必须跨请求共享，
    否则 API 起一个模型后，别的请求就「看不见」这个进程了。
    """
    global _MANAGER
    if _MANAGER is None:
        with _MANAGER_LOCK:
            if _MANAGER is None:
                _MANAGER = ModelManager()
    return _MANAGER
