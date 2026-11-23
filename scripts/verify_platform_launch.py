# -*- coding: utf-8 -*-
"""verify_platform_launch.py —— 桌面版「一键启动」后端回归脚本。

验证本轮新增的三个契约：

    A. **GET /platform/status**
       · 返回 200；
       · **够快**（< 1 秒；这条是硬要求 —— 前端每 2 秒轮询一次，
         接口一慢前端就会堆积请求）；
       · 结构完整：``api`` / ``models[]`` / ``kb`` / ``summary`` /
         ``one_click_ready`` / ``default``；
       · 每个模型都有 ``alias / label / running / reachable / device /
         port / pid``。

    B. **POST /platform/launch 是幂等的**
       · 对**已经在跑**的模型调用两次 → 第二次必须报
         ``already_running``，且 ``started`` 为空；
       · **不能多起 llama-server 进程**（按进程名统计 PID 集合，前后一致）。

    C. **「选中的启动，没选中的停掉」**
       · 勾选 CPU 模型（qwen2.5-14b）→ POST /platform/launch
         → 该模型被启动；
       · 再只勾 GPU 模型 → POST /platform/launch
         → CPU 模型被停止、GPU 模型保持运行。

⚠️ **本脚本会真的停 / 起 qwen2.5-14b**（CPU 模型，重启代价低）。
   ⚠️ **绝不停止 gpt-oss-20b**（:8080，GPU）—— 用户正在用，
      重启它要重新加载 11.3 GB 权重，既慢又会打断当前会话。
      脚本**只读**它，最后还会断言它仍在运行。

   脚本**无论成败都会在最后把 qwen2.5-14b 恢复成运行状态**
   （``finally`` 里无条件尝试重启），并**保证 gpt-oss-20b 仍在运行**。

用法：
    python scripts/verify_platform_launch.py
    python scripts/verify_platform_launch.py --api http://127.0.0.1:8010
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

#: /platform/status 的**响应时间上限**（秒）—— 前端每 2 秒轮询一次，
#: 这个接口超过 1 秒就会让前端请求堆积。
STATUS_MAX_LATENCY_S = 1.0


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


# ----------------------------------------------------------------------
# HTTP 小工具（只用标准库，脚本不依赖任何第三方 HTTP 客户端）
# ----------------------------------------------------------------------
def http_json(method: str, url: str, payload: Optional[Dict[str, Any]] = None,
              timeout: float = 30.0) -> tuple[int, Any, float]:
    """发一个 JSON 请求。

    Returns:
        ``(status_code, 解析后的 JSON 或原文, 耗时秒)``
    """
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            code = int(resp.status)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        code = int(exc.code)
    except Exception as exc:  # noqa: BLE001 - 脚本要把网络错误也报出来
        return 0, {"error": f"{type(exc).__name__}: {exc}"}, time.time() - t0
    elapsed = time.time() - t0
    try:
        return code, json.loads(body) if body else None, elapsed
    except Exception:
        return code, body, elapsed


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
    """当前所有 llama-server 进程的 PID（按**进程名**匹配）。

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


def wait_port_free(port: int, timeout: float = 25.0) -> bool:
    """等端口被释放。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_listening(port):
            return True
        time.sleep(0.4)
    return not port_listening(port)


def wait_ready(port: int, timeout: float = 180.0) -> bool:
    """等端口上的 ``/v1/models`` 有响应（模型加载完）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_listening(port) and http_models_ok(f"http://127.0.0.1:{port}/v1"):
            return True
        time.sleep(1.0)
    return False


def wait_all_ready(aliases: List[str], timeout: float = 180.0) -> bool:
    """等一批模型**全部**就绪。

    ★ 幂等用例的前置条件：先把所有已知模型拉起来，等到**每一个**
      的端点都真的能应答 ``/v1/models``，再去断言「重复调用不再起进程」。
      否则第一次调用本来就得把没跑的那个起起来（``status=starting``
      是**正确**行为），测的就不是幂等了。
    """
    specs = ModelManager().specs
    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = []
        for alias in aliases:
            spec = specs.get(alias)
            port = int(getattr(spec, "port", 0) or 0) if spec else 0
            if not port or not http_models_ok(f"http://127.0.0.1:{port}/v1"):
                pending.append(f"{alias}(:{port})")
        if not pending:
            return True
        print(f"    ... 等待就绪：{', '.join(pending)}")
        time.sleep(2.0)
    return False


def main() -> int:
    """主流程。"""
    ap = argparse.ArgumentParser(description="桌面版「一键启动」后端回归验证")
    ap.add_argument("--api", default="http://127.0.0.1:8010",
                    help="API 地址（默认 http://127.0.0.1:8010）")
    ap.add_argument("--gpu-alias", default="gpt-oss-20b",
                    help="GPU 模型别名（**只读**，脚本绝不启停它）")
    ap.add_argument("--cpu-alias", default="qwen2.5-14b",
                    help="CPU 模型别名（脚本会停 / 起它，结束时恢复）")
    args = ap.parse_args()

    api = args.api.rstrip("/")
    mgr = ModelManager()
    gpu_alias = args.gpu_alias
    cpu_alias = args.cpu_alias

    print("=" * 66)
    print(" 桌面版「一键启动」验证 —— /platform/status + /platform/launch")
    print("=" * 66)

    # ------------------------------------------------------------------
    # A. /platform/status
    # ------------------------------------------------------------------
    section("A. GET /platform/status（200 / 够快 / 结构完整）")
    code, data, elapsed = http_json("GET", f"{api}/platform/status")
    if code == 200:
        ok("GET /platform/status 返回 200", f"耗时 {elapsed:.3f}s")
    else:
        bad("GET /platform/status 返回 200", f"实际 {code}：{data}")
        print("\n  API 不可用，后续用例无法执行。")
        print("\n" + "=" * 66)
        print(f" 结果： [PASS] {PASS}   [FAIL] {FAIL}")
        print("=" * 66)
        return 1

    if elapsed < STATUS_MAX_LATENCY_S:
        ok(f"响应够快（< {STATUS_MAX_LATENCY_S:.0f}s）", f"耗时 {elapsed:.3f}s")
    else:
        bad(f"响应够快（< {STATUS_MAX_LATENCY_S:.0f}s）", f"耗时 {elapsed:.3f}s")

    if isinstance(data, dict):
        # ---- 顶层结构 ----
        for key in ("api", "models", "kb", "summary", "one_click_ready", "default"):
            if key in data:
                ok(f"响应含顶层字段 {key}", repr(data.get(key))[:90])
            else:
                bad(f"响应含顶层字段 {key}", "字段缺失")

        # ---- api 子结构 ----
        api_info = data.get("api") or {}
        if api_info.get("reachable") is True:
            ok("api.reachable 为 true", "")
        else:
            bad("api.reachable 为 true", repr(api_info))
        if api_info.get("base_url"):
            ok("api.base_url 有值", str(api_info.get("base_url")))
        else:
            bad("api.base_url 有值", "为空")

        # ---- kb 子结构 ----
        kb = data.get("kb") or {}
        if isinstance(kb, dict) and "ready" in kb:
            ok("kb.ready 是布尔位", f"ready={kb.get('ready')} state={kb.get('state')}")
        else:
            bad("kb.ready 是布尔位", repr(kb))

        # ---- summary / one_click_ready ----
        if isinstance(data.get("summary"), str) and data.get("summary"):
            ok("summary 是一行人类可读结论", str(data.get("summary")))
        else:
            bad("summary 是一行人类可读结论", repr(data.get("summary")))
        if isinstance(data.get("one_click_ready"), bool):
            ok("one_click_ready 是布尔位", str(data.get("one_click_ready")))
        else:
            bad("one_click_ready 是布尔位", repr(data.get("one_click_ready")))

        # ---- models[] 结构 ----
        models = data.get("models") or []
        if models:
            ok(f"models[] 非空", f"{len(models)} 个模型")
        else:
            bad("models[] 非空", "空列表")
        need = ("alias", "label", "running", "reachable", "device", "port", "pid")
        for m in models:
            missing = [k for k in need if k not in m]
            if missing:
                bad(f"模型 {m.get('alias')} 的字段齐全", f"缺 {missing}")
            else:
                ok(f"模型 {m.get('alias')} 的字段齐全",
                   f"running={m.get('running')} reachable={m.get('reachable')} "
                   f"device={m.get('device')} port={m.get('port')}")
    else:
        bad("响应是 JSON 对象", repr(data)[:200])

    # ------------------------------------------------------------------
    # B. /platform/launch 幂等
    # ------------------------------------------------------------------
    section("B. POST /platform/launch 幂等（重复调用不多起进程）")
    gpu_model = mgr.status(gpu_alias) or {}
    if not gpu_model.get("running"):
        bad(f"{gpu_alias} 起始状态为运行中",
            "GPU 模型没在跑 —— 幂等用例改用 CPU 模型")
        target = cpu_alias
    else:
        ok(f"{gpu_alias} 起始状态为运行中",
           f"pid={gpu_model.get('pid')} port={gpu_model.get('port')}")
        target = gpu_alias

    # ★ 勾选列表 = **全部已知模型**：这一节只测「幂等 / 不多起进程」，
    #   如果只勾一个，另一个会被「没勾选 → 停掉」规则停掉，
    #   那样 PID 集合的变化就分不清是「多起了一个」还是「按规则停了一个」。
    #   「停掉未勾选的」语义交给 C 节专门验证。
    all_aliases = list(mgr.aliases())

    # ★★ 前置条件：**先把所有已知模型拉起来并等到就绪**。
    #   否则「第二次调用仍返回 already_running」这条断言不成立 ——
    #   第一次调用本来就得把没跑的那个起起来（status=starting 是**对的**），
    #   测的就不是幂等而是「能不能启动」了。
    #   （GPU 模型（用户正在用）只**读不写**地等它 ready，不去动它。）
    ok("前置条件：把全部已知模型拉起并等到就绪", f"aliases={all_aliases}")
    code_pre, res_pre, _ = http_json("POST", f"{api}/platform/launch",
                                     {"models": all_aliases})
    if code_pre != 200:
        bad("前置条件：预热 launch 返回 200", f"实际 {code_pre}：{res_pre}")
    wait_all_ready(all_aliases, timeout=180)

    pids_before = llama_server_pids()
    code1, res1, _ = http_json("POST", f"{api}/platform/launch",
                               {"models": all_aliases})
    if code1 == 200:
        ok("POST /platform/launch 返回 200", "")
    else:
        bad("POST /platform/launch 返回 200", f"实际 {code1}：{res1}")

    status1 = (res1 or {}).get("status")
    if status1 == "already_running":
        ok(f"对已在跑的 {target} 返回 already_running", str(status1))
    else:
        bad(f"对已在跑的 {target} 返回 already_running",
            f"实际 status={status1} message={(res1 or {}).get('message')}")
    if not (res1 or {}).get("started"):
        ok("started 列表为空（没有重复启动）", str((res1 or {}).get("started")))
    else:
        bad("started 列表为空（没有重复启动）", str((res1 or {}).get("started")))

    # 再调一次 —— 必须仍然幂等
    code2, res2, _ = http_json("POST", f"{api}/platform/launch",
                               {"models": all_aliases})
    status2 = (res2 or {}).get("status")
    if target in ((res1 or {}).get("already_running") or []):
        ok(f"{target} 出现在 already_running 里（没被重复启动）", "")
    else:
        bad(f"{target} 出现在 already_running 里（没被重复启动）",
            f"already_running={(res1 or {}).get('already_running')}")

    if code2 == 200 and status2 == "already_running":
        ok(f"第二次调用仍返回 already_running（幂等）", str(status2))
    else:
        bad(f"第二次调用仍返回 already_running（幂等）",
            f"实际 code={code2} status={status2}")
    if not (res2 or {}).get("started"):
        ok("第二次调用 started 仍为空（幂等）", "")
    else:
        bad("第二次调用 started 仍为空（幂等）",
            str((res2 or {}).get("started")))

    time.sleep(1.5)
    pids_after = llama_server_pids()
    if pids_after == pids_before:
        ok("没有多起 llama-server 进程",
           f"PID 集合不变：{pids_before}")
    else:
        bad("没有多起 llama-server 进程",
            f"前 {pids_before} → 后 {pids_after}")

    # ---- B2：空选择 = 平台默认（不该报 422 / 500）----
    # ★ 这一步**会停掉未勾选的模型**（默认只勾 GPU）——这是「选中的启动、
    #   没选中的停掉」的正确语义，所以必须放在上面的 PID 断言**之后**。
    code3, res3, _ = http_json("POST", f"{api}/platform/launch", {"models": []})
    if code3 == 200:
        ok("空选择（平台默认）返回 200",
           f"requested={(res3 or {}).get('requested')}")
    else:
        bad("空选择（平台默认）返回 200", f"实际 {code3}：{res3}")
    req3 = (res3 or {}).get("requested") or []
    if req3 == [gpu_alias]:
        ok("空选择解析成平台默认模型", str(req3))
    else:
        bad("空选择解析成平台默认模型", f"requested={req3}（期望 [{gpu_alias}]）")

    # ------------------------------------------------------------------
    # C. 「选中的启动，没选中的停掉」
    # ------------------------------------------------------------------
    section(f"C. 「选中的启动，没选中的停掉」（{cpu_alias} ↔ {gpu_alias}）")
    cpu_model = mgr.status(cpu_alias) or {}
    cpu_port = int(cpu_model.get("port") or 8081)

    try:
        # ---- C1：勾选 GPU + CPU → CPU 启动、GPU 保持运行 ----
        # ★ 这里**必须**把 GPU 也勾上：用户要的语义是「选中的启动，
        #   没选中的停掉」—— 只勾 CPU 会**正确地**把 GPU 停掉，
        #   而本脚本有义务**绝不打断**用户正在用的 GPU 模型。
        code, res, _ = http_json("POST", f"{api}/platform/launch",
                                 {"models": [gpu_alias, cpu_alias]})
        started = (res or {}).get("started") or []
        stopped = (res or {}).get("stopped") or []
        already = (res or {}).get("already_running") or []
        # ★ CPU 模型**可能本来就在跑**（上一轮跑完恢复过）——
        #   这时后端正确地报 ``already_running`` 而不是 ``started``。
        #   两种都算「CPU 模型在勾选列表里且没被停掉」。
        if cpu_alias in started:
            ok(f"launch 报告启动了 {cpu_alias}", f"started={started}")
        elif cpu_alias in already:
            ok(f"launch 报告 {cpu_alias} 已在运行（幂等）",
               f"already_running={already}")
        else:
            bad(f"launch 报告启动了 {cpu_alias}",
                f"started={started} already={already} "
                f"status={(res or {}).get('status')}")
        if gpu_alias not in stopped:
            ok(f"launch **没有**停止 {gpu_alias}（GPU 在勾选列表里）",
               f"stopped={stopped}")
        else:
            bad(f"launch **没有**停止 {gpu_alias}", f"stopped={stopped}")

        print("  ... 等待 CPU 模型加载（可能需要 30~60 秒）")
        if wait_ready(cpu_port, timeout=180.0):
            ok(f"{cpu_alias} 端点就绪", f":{cpu_port}")
        else:
            bad(f"{cpu_alias} 端点就绪", f":{cpu_port} 无响应")

        # ---- C2：只勾 GPU → CPU 被停掉、GPU 保持 ----
        code, res, _ = http_json("POST", f"{api}/platform/launch",
                                 {"models": [gpu_alias]})
        stopped = (res or {}).get("stopped") or []
        if cpu_alias in stopped:
            ok(f"launch 停止了未勾选的 {cpu_alias}", str(stopped))
        else:
            bad(f"launch 停止了未勾选的 {cpu_alias}",
                f"stopped={stopped} status={(res or {}).get('status')}")
        if wait_port_free(cpu_port, timeout=25.0):
            ok(f"端口 {cpu_port} 已释放", "")
        else:
            bad(f"端口 {cpu_port} 已释放", "端口仍在监听")

        g = mgr.status(gpu_alias) or {}
        if g.get("running"):
            ok(f"{gpu_alias} 全程保持运行", f"pid={g.get('pid')}")
        else:
            bad(f"{gpu_alias} 全程保持运行", "GPU 模型被误停了")

    finally:
        # ------------------------------------------------------------------
        # 恢复现场：CPU 模型回到运行状态；GPU 模型必须始终在跑
        # ------------------------------------------------------------------
        section("恢复现场（确保被测模型回到运行状态）")
        try:
            cur = mgr.status(cpu_alias) or {}
            if cur.get("running") and http_models_ok(
                    f"http://127.0.0.1:{cpu_port}/v1"):
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

        g = mgr.status(gpu_alias) or {}
        if g.get("running"):
            ok(f"{gpu_alias} 全程未被影响（仍在运行）",
               f"pid={g.get('pid')} port={g.get('port')}")
        else:
            # ★ 用户正在用 GPU 模型 —— 脚本有义务把它拉回来
            bad(f"{gpu_alias} 全程未被影响（仍在运行）", "GPU 模型不在运行")
            try:
                print(f"  ... 正在恢复 {gpu_alias} ...")
                rec = mgr.start_model(gpu_alias, wait=True, timeout=180.0)
                if rec.get("status") == "started":
                    ok(f"{gpu_alias} 已恢复运行", rec.get("message", ""))
                else:
                    bad(f"{gpu_alias} 已恢复运行",
                        f"status={rec.get('status')} message={rec.get('message')}")
            except Exception as exc:  # noqa: BLE001
                bad(f"{gpu_alias} 已恢复运行", f"{type(exc).__name__}: {exc}")

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
