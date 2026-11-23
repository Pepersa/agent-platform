# -*- coding: utf-8 -*-
"""verify_model_lifecycle.py —— 模型生命周期（启停）回归脚本。

验证 ``agent_platform.core.model_lifecycle.ModelManager`` 的四件事：

    A. **状态识别**
       两个模型（gpt-oss-20b :8080 / qwen2.5-14b :8081）都被识别为
       ``running=True`` 且 ``reachable=True``，PID 正确。

    B. **幂等启动**
       对**已经在跑**的模型调用 ``start_model()`` → 返回
       ``already_running``，且**没有**多出一个 llama-server 进程
       （按进程名统计数量，前后必须一致）。

    C. **停止 → 端口释放**
       停掉 qwen2.5-14b（:8081）→ 报 ``stopped``，端口 8081 真的没人听了。

    D. **重启 → 端点恢复**
       再把 qwen2.5-14b 拉起来 → ``/v1/models`` 有响应，
       端口重新被监听，PID 换成新的。

⚠️ **本脚本会真的停 / 起 qwen2.5-14b**（CPU 模型，重启代价低）。
   ⚠️ **绝不触碰 gpt-oss-20b**（:8080，PID 43032）—— 用户正在用，
      重启它要重新加载 11.3 GB 权重，既慢又会打断当前会话。

   脚本**无论成败都会在最后把 qwen2.5-14b 恢复成运行状态**
   （``finally`` 里无条件尝试重启）。

用法：
    python scripts/verify_model_lifecycle.py
    python scripts/verify_model_lifecycle.py --api http://127.0.0.1:8010
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from agent_platform.core.model_lifecycle import ModelManager  # noqa: E402

PASS = 0
FAIL = 0
FAILURES: List[str] = []


def ok(name: str, detail: str = "") -> None:
    """记一次通过。"""
    global PASS
    PASS += 1
    print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))


def bad(name: str, detail: str = "") -> None:
    """记一次失败。"""
    global FAIL
    FAIL += 1
    FAILURES.append(f"{name}: {detail}")
    print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    """打印分节标题。"""
    print(f"\n--- {title} ---")


def port_listening(port: int) -> bool:
    """端口有没有人在听（独立于 ModelManager 的复核手段）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", int(port))) == 0


def http_models_ok(base_url: str, timeout: float = 5.0) -> bool:
    """请求 ``{base_url}/models`` 是否返回模型列表。"""
    url = base_url.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        return bool(isinstance(payload, dict) and payload.get("data"))
    except Exception:
        return False


def llama_server_pids() -> List[int]:
    """当前所有 llama-server 进程的 PID（按名字匹配）。

    用来判断「有没有多起了一个」。
    """
    try:
        import psutil  # noqa: PLC0415
    except Exception:
        return []
    out: List[int] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if "llama-server" in name:
                out.append(int(proc.info["pid"]))
        except Exception:
            continue
    return sorted(out)


def wait_port_free(port: int, timeout: float = 20.0) -> bool:
    """等端口被释放。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_listening(port):
            return True
        time.sleep(0.4)
    return not port_listening(port)


def wait_port_listening(port: int, timeout: float = 60.0) -> bool:
    """等端口被监听。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_listening(port):
            return True
        time.sleep(0.4)
    return port_listening(port)


def main() -> int:
    """主流程。"""
    ap = argparse.ArgumentParser(description="模型生命周期回归验证")
    ap.add_argument("--gpu-alias", default="gpt-oss-20b",
                    help="GPU 模型别名（**只读**，脚本绝不启停它）")
    ap.add_argument("--cpu-alias", default="qwen2.5-14b",
                    help="CPU 模型别名（脚本会停 / 起它，结束时恢复）")
    ap.add_argument("--skip-restart-check", action="store_true",
                    help="只做状态检测，不做停止 / 重启（冒烟模式）")
    args = ap.parse_args()

    mgr = ModelManager()
    gpu_alias = args.gpu_alias
    cpu_alias = args.cpu_alias

    print("=" * 66)
    print(" 模型生命周期验证 —— ModelManager")
    print("=" * 66)

    # ------------------------------------------------------------------
    # A. 状态识别
    # ------------------------------------------------------------------
    section("A. 状态识别（两个端点都应 running + reachable）")
    models = {m["alias"]: m for m in mgr.list_models()}
    for alias in (gpu_alias, cpu_alias):
        if alias not in models:
            bad(f"{alias} 在已知模型列表里", f"当前列表：{list(models)}")
            continue
        m = models[alias]
        if m.get("running"):
            ok(f"{alias} 处于运行状态", f"port={m['port']} pid={m.get('pid')}")
        else:
            bad(f"{alias} 处于运行状态", f"running={m.get('running')}")
        if m.get("reachable"):
            ok(f"{alias} /v1/models 有响应", m.get("base_url", ""))
        else:
            bad(f"{alias} /v1/models 有响应", f"reachable={m.get('reachable')}")
        if m.get("pid"):
            ok(f"{alias} 拿到了 PID", str(m.get("pid")))
        else:
            bad(f"{alias} 拿到了 PID", "pid 为空")

    gpu_before = models.get(gpu_alias, {})
    if gpu_before.get("running"):
        ok("GPU 模型保持运行（脚本不会碰它）",
           f"{gpu_alias} pid={gpu_before.get('pid')} port={gpu_before.get('port')}")
    else:
        bad("GPU 模型保持运行", f"{gpu_alias} 当前不在运行")

    if args.skip_restart_check:
        print(f"\n[PASS] {PASS}  [FAIL] {FAIL}")
        return 0 if FAIL == 0 else 1

    # ------------------------------------------------------------------
    # B. 幂等启动（不产生重复进程）
    # ------------------------------------------------------------------
    section("B. 幂等启动（已在跑 → already_running，且不多起进程）")
    pids_before = llama_server_pids()
    res = mgr.start_model(gpu_alias, wait=False)
    if res.get("status") == "already_running":
        ok(f"start_model({gpu_alias}) 返回 already_running",
           res.get("message", ""))
    else:
        bad(f"start_model({gpu_alias}) 返回 already_running",
            f"实际 status={res.get('status')} message={res.get('message')}")
    time.sleep(1.5)
    pids_after = llama_server_pids()
    if pids_after == pids_before:
        ok("没有多起 llama-server 进程",
           f"进程数 {len(pids_before)} → {len(pids_after)}，PID 集合不变")
    else:
        bad("没有多起 llama-server 进程",
            f"前 {pids_before} → 后 {pids_after}")

    # CPU 模型同样测一次（它也在跑）
    res_cpu = mgr.start_model(cpu_alias, wait=False)
    if res_cpu.get("status") == "already_running":
        ok(f"start_model({cpu_alias}) 返回 already_running",
           res_cpu.get("message", ""))
    else:
        bad(f"start_model({cpu_alias}) 返回 already_running",
            f"实际 status={res_cpu.get('status')}")
    time.sleep(1.0)
    if llama_server_pids() == pids_before:
        ok("对 CPU 模型重复 start 也没有多起进程",
           f"llama-server PID 集合仍为 {pids_before}")
    else:
        bad("对 CPU 模型重复 start 也没有多起进程",
            f"当前 PID 集合 {llama_server_pids()}（期望 {pids_before}）")

    # ------------------------------------------------------------------
    # C / D. 停止 → 重启 → 恢复
    # ------------------------------------------------------------------
    section(f"C/D. 停止 {cpu_alias} → 端口释放 → 重启 → 端点恢复")
    cpu_model = models.get(cpu_alias, {})
    cpu_port = int(cpu_model.get("port") or 8081)
    old_pid = cpu_model.get("pid")

    try:
        res_stop = mgr.stop_model(cpu_alias)
        if res_stop.get("status") == "stopped":
            ok(f"stop_model({cpu_alias}) 返回 stopped",
               res_stop.get("message", ""))
        else:
            bad(f"stop_model({cpu_alias}) 返回 stopped",
                f"实际 status={res_stop.get('status')} message={res_stop.get('message')}")

        if wait_port_free(cpu_port, timeout=20.0):
            ok(f"端口 {cpu_port} 已释放", "connect_ex 返回非 0")
        else:
            bad(f"端口 {cpu_port} 已释放", "端口仍在监听")

        st_now = mgr.status(cpu_alias) or {}
        if not st_now.get("running"):
            ok(f"状态显示 {cpu_alias} 未运行", f"running={st_now.get('running')}")
        else:
            bad(f"状态显示 {cpu_alias} 未运行", f"running={st_now.get('running')}")

        # 再停一次 → 幂等返回 not_running
        res_stop2 = mgr.stop_model(cpu_alias)
        if res_stop2.get("status") == "not_running":
            ok(f"重复 stop_model({cpu_alias}) 返回 not_running（幂等）",
               res_stop2.get("message", ""))
        else:
            bad(f"重复 stop_model({cpu_alias}) 返回 not_running",
                f"实际 status={res_stop2.get('status')}")

        # ---- 重启 ----
        print(f"  ... 正在重启 {cpu_alias}（模型加载需要 10~60 秒，请耐心等待）")
        t0 = time.time()
        res_start = mgr.start_model(cpu_alias, wait=True, timeout=180.0)
        elapsed = time.time() - t0
        if res_start.get("status") == "started":
            ok(f"start_model({cpu_alias}) 返回 started",
               f"{res_start.get('message', '')}（耗时 {elapsed:.1f}s）")
        else:
            bad(f"start_model({cpu_alias}) 返回 started",
                f"实际 status={res_start.get('status')} message={res_start.get('message')}")

        if wait_port_listening(cpu_port, timeout=30.0):
            ok(f"端口 {cpu_port} 重新被监听", "")
        else:
            bad(f"端口 {cpu_port} 重新被监听", "端口未恢复")

        if http_models_ok(f"http://127.0.0.1:{cpu_port}/v1"):
            ok(f"{cpu_alias} /v1/models 恢复响应", f":{cpu_port}")
        else:
            bad(f"{cpu_alias} /v1/models 恢复响应", f":{cpu_port} 无响应")

        st_after = mgr.status(cpu_alias) or {}
        new_pid = st_after.get("pid")
        if new_pid and new_pid != old_pid:
            ok(f"{cpu_alias} PID 已更换（是新进程）", f"{old_pid} → {new_pid}")
        elif new_pid == old_pid:
            bad(f"{cpu_alias} PID 已更换（是新进程）",
                f"PID 仍是 {new_pid}，可能没真正重启")
        else:
            bad(f"{cpu_alias} PID 已更换（是新进程）", "拿不到新 PID")

    finally:
        # ------------------------------------------------------------------
        # 恢复原状：**无论成败**都保证 cpu 模型是运行状态
        # ------------------------------------------------------------------
        section("恢复现场（确保被测模型回到运行状态）")
        try:
            cur = mgr.status(cpu_alias) or {}
            if cur.get("running") and http_models_ok(f"http://127.0.0.1:{cpu_port}/v1"):
                ok(f"{cpu_alias} 已在运行（无需恢复）",
                   f"pid={cur.get('pid')} port={cpu_port}")
            else:
                print(f"  ... 正在恢复 {cpu_alias} ...")
                rec = mgr.start_model(cpu_alias, wait=True, timeout=180.0)
                if rec.get("status") == "started" and http_models_ok(
                        f"http://127.0.0.1:{cpu_port}/v1"):
                    ok(f"{cpu_alias} 已恢复运行", rec.get("message", ""))
                else:
                    bad(f"{cpu_alias} 已恢复运行",
                        f"status={rec.get('status')} message={rec.get('message')}")
        except Exception as exc:  # noqa: BLE001
            bad(f"{cpu_alias} 已恢复运行", f"{type(exc).__name__}: {exc}")

        # GPU 模型必须**始终**还在跑
        g = mgr.status(gpu_alias) or {}
        if g.get("running"):
            ok(f"{gpu_alias} 全程未被影响（仍在运行）",
               f"pid={g.get('pid')} port={g.get('port')}")
        else:
            bad(f"{gpu_alias} 全程未被影响（仍在运行）", "GPU 模型不在运行")

    # ------------------------------------------------------------------
    # 汇总
    # ------------------------------------------------------------------
    print("\n" + "=" * 66)
    print(f" 结果： [PASS] {PASS}   [FAIL] {FAIL}")
    if FAILURES:
        print("\n 失败明细：")
        for item in FAILURES:
            print(f"   - {item}")
    print("=" * 66)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
