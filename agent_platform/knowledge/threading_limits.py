"""PyTorch / BLAS 线程数限制。

★★ 2026-11-23 资源治理 ★★
  用户要求：平台后台运行的进程**总占用不得超过机器资源的 50%**，
  以免影响其同时使用电脑。

  实测（本机 Ryzen 7 9800X3D / 8 核 16 线程）：
    API 进程**完全空闲**时仍稳定占用 **32% 的 16 逻辑核**
    （20s 窗口内累积 102 CPU-秒，进程内 106 个线程）。

  根因：**不是**有请求在处理，而是 PyTorch 默认把 intra-op 线程池
  开到「全部逻辑核」，而 FlagEmbedding 的 bge-m3 / bge-reranker-v2-m3
  常驻 CPU（见 ``embed.resolve_flag_devices`` —— 必须留 CPU，
  否则与常驻 GPU 的 gpt-oss-20b 抢显存导致 OOM），
  其 OpenMP 线程池会在模型加载后**持续空转**（spin-wait）。

  对策：把 intra-op / inter-op 线程数**显式压到一个小值**，
  并同步设置 ``OMP_NUM_THREADS`` 等环境变量（必须在 numpy/torch
  初始化**之前**设置才生效，故此处同时写环境变量）。

  为什么不能把 BGE 挪到 GPU：单卡 16 GB，gpt-oss-20b 常驻已占
  ~15.4 GB，BGE 上 GPU 会 OOM 并连带两个推理服务崩溃
  （见 embed.py 中 ``resolve_flag_devices`` 的详细说明）。

  为什么不用进程优先级（nice）：nice 只降低**调度权重**，
  不阻止 OpenMP 线程空转，空闲时依然烧满 CPU；只有真正限制
  **线程数**才有效。

默认 4 线程：足以让 bge-m3 在检索时保持可接受的延迟
（检索是低频操作），同时把常驻占用压到 ~8% 以内。
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager
from typing import Any, Optional

__all__ = [
    "resolve_torch_threads",
    "limit_torch_threads",
    "resolve_inference_threads",
    "inference_thread_scope",
]

logger = logging.getLogger(__name__)

#: 环境变量名 → torch 对应 setter
_ENV_VARS = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)

#: 已经设置过的标记，避免重复生效 / 重复打日志
_applied = False

#: 推理期线程数的**兜底值**（当 ``AGENT_TORCH_THREADS`` 未设置时）。
#: ★ 为什么推理期要放宽：
#:   ``limit_torch_threads()`` 的目的是**空闲时别空转**（OpenMP spin-wait），
#:   但**推理时**我们恰恰需要并行度 —— 交叉编码器是纯 CPU 矩阵运算，
#:   4 线程比 8 线程慢近 **2 倍**（实测 n=20/ml=256：19.1s → 10.8s）。
#:   所以策略是「**空闲压低、推理拉高**」：
#:     · 空闲期（模型常驻、没人检索）：4 线程 → 常驻占用 ~8%
#:     · 推理期（有人在检索）：8 线程 → 吞吐翻倍
#:   推理结束后立刻把线程数降回去，空闲期依然不空转。
_INFERENCE_THREADS_DEFAULT = 8

#: 进程内记录的「空闲期线程数」（用于推理结束后恢复）
_idle_threads: Optional[int] = None


def resolve_inference_threads(default: int = _INFERENCE_THREADS_DEFAULT) -> int:
    """解析**推理期**应该给 PyTorch 用的线程数。

    优先级：环境变量 ``AGENT_TORCH_INFER_THREADS`` > ``default``（8）。

    ★ 与 :func:`resolve_torch_threads`（空闲期，默认 4）**故意不同**：
      空闲期要的是「别空转」，推理期要的是「跑得快」。

    Args:
        default: 未显式配置时的默认线程数。

    Returns:
        至少为 1 的线程数。
    """
    raw = os.environ.get("AGENT_TORCH_INFER_THREADS", "").strip()
    if not raw:
        return max(1, int(default))
    try:
        n = int(raw)
    except ValueError:
        logger.warning(
            "AGENT_TORCH_INFER_THREADS=%r 不是合法整数，回退到 %d。", raw, default
        )
        return max(1, int(default))
    return max(1, n)


@contextmanager
def inference_thread_scope(threads: Optional[int] = None) -> Any:
    """推理期临时提高 torch 线程数，退出时**恢复**空闲期设置。

    ★ 为什么需要（实测于 2026-11-23）：
      ``limit_torch_threads()`` 把 intra-op 线程压到 4，理由是
      「OpenMP 线程池空闲时持续空转烧 CPU」。但这个代价在**推理时**
      是白白付出的 —— 交叉编码器是纯 CPU 矩阵运算，4 线程比 8 线程慢近 2 倍：

        n=20, max_length=256 → 4 线程 19.1s / 8 线程 **10.8s**
        n=50, max_length=256 → 4 线程 32.3s / 8 线程 **18.8s**

      （16 线程反而更慢 —— 超订，调度开销吃掉收益。）

      所以这里在**推理窗口内**把线程数临时拉高，**退出时立刻恢复**，
      保证「空闲期不空转」这个原始目标不受影响。

    Args:
        threads: 推理期线程数；None 表示走 :func:`resolve_inference_threads`。

    Yields:
        实际生效的线程数。
    """
    global _idle_threads
    n = resolve_inference_threads() if threads is None else max(1, int(threads))
    try:
        import torch  # noqa: PLC0415 - 延迟导入
    except Exception:  # noqa: BLE001 - 没 torch 就直接跳过
        yield n
        return
    try:
        prev = int(torch.get_num_threads())
    except Exception:  # noqa: BLE001
        prev = None
    try:
        if prev is not None and prev != n:
            torch.set_num_threads(n)
        yield n
    finally:
        # ★ 恢复空闲期线程数（让「空闲不空转」继续成立）
        try:
            back = _idle_threads if _idle_threads is not None else prev
            if back is not None and int(torch.get_num_threads()) != int(back):
                torch.set_num_threads(int(back))
        except Exception:  # noqa: BLE001 - 恢复失败不影响主流程
            pass



def resolve_torch_threads(default: int = 4) -> int:
    """解析应该给 PyTorch 用的线程数。

    优先级：环境变量 ``AGENT_TORCH_THREADS`` > ``default``。

    Args:
        default: 未显式配置时的默认线程数。

    Returns:
        至少为 1 的线程数。
    """
    raw = os.environ.get("AGENT_TORCH_THREADS", "").strip()
    if not raw:
        return max(1, int(default))
    try:
        n = int(raw)
    except ValueError:
        logger.warning(
            "AGENT_TORCH_THREADS=%r 不是合法整数，回退到 %d。", raw, default
        )
        return max(1, int(default))
    return max(1, n)


def limit_torch_threads(threads: Optional[int] = None) -> int:
    """把 PyTorch 与 BLAS 的线程数压到指定值。

    ⚠️ ``OMP_NUM_THREADS`` 等环境变量只有在 OpenMP 运行时**初始化之前**
    设置才生效。torch 通常在首次 import 时初始化 OpenMP，
    所以本函数应在**任何 torch 使用之前**调用；即便调用晚了，
    ``torch.set_num_threads`` 依然可以对 intra-op 池生效。

    Args:
        threads: 目标线程数；None 表示走 :func:`resolve_torch_threads`。

    Returns:
        实际设置的线程数。
    """
    global _applied, _idle_threads

    n = resolve_torch_threads() if threads is None else max(1, int(threads))
    # ★ 记录「空闲期线程数」，供 inference_thread_scope 退出时恢复
    _idle_threads = n

    # 1) 环境变量（对尚未初始化的 OpenMP 运行时有效）
    for var in _ENV_VARS:
        os.environ.setdefault(var, str(n))

    # 2) torch 自身的线程池（无论何时调用都生效）
    try:
        import torch  # noqa: PLC0415 - 延迟导入，避免非 torch 场景付出代价

        torch.set_num_threads(n)
        try:
            torch.set_num_interop_threads(n)
        except RuntimeError:
            # interop 池一旦初始化就不可改，忽略即可（不影响 intra-op）
            pass
    except Exception as exc:  # noqa: BLE001 - 限制线程失败不能拖垮启动
        logger.warning("设置 torch 线程数失败（忽略）：%s", exc)
        return n

    if not _applied:
        # 用 warning 级别 + 同时直写 stderr：
        #   uvicorn 的 LOGGING_CONFIG 只为 uvicorn.* 三个 logger 配了 handler，
        #   **没有 root handler**。因此 `agent_platform.*` 的日志记录虽然
        #   propagate=True，却找不到任何 handler，lastResort 兜底也不会触发
        #   （dictConfig 已经创建了一个无 handler 的 root logger），
        #   结果就是日志被静默丢弃 —— 实测 api_err.log 里看不到任何这条信息。
        #   直写 stderr 可以保证证据一定落盘（api_err.log 捕获的是 stderr）。
        msg = "PyTorch/BLAS 线程数已限制为 %d（后台常驻负载可控）" % n
        logger.warning(msg)
        try:
            print(msg, file=sys.stderr, flush=True)
        except Exception:  # noqa: BLE001 - 打印失败不能影响启动
            pass
        _applied = True
    return n
