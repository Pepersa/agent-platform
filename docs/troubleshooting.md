# Troubleshooting

Real issues, each written as symptom, cause, and fix, with the error string or observable behaviour
you would actually see.

---

## Table of contents

- [llama-server will not start](#llama-server-will-not-start)
- [High idle CPU usage](#high-idle-cpu-usage)
- [CUDA out of memory](#cuda-out-of-memory)
- [The first knowledge-base query times out](#the-first-knowledge-base-query-times-out)
- [Budget exhausted too early](#budget-exhausted-too-early)
- [Code is never written](#code-is-never-written)
- [The approval appears to hang](#the-approval-appears-to-hang)
- [The trace panel does not update after approval](#the-trace-panel-does-not-update-after-approval)
- [The C: drive fills up](#the-c-drive-fills-up)
- [Retrieval quality is poor](#retrieval-quality-is-poor)
- [The desktop app shows a console window](#the-desktop-app-shows-a-console-window)
- [The second desktop launch does nothing](#the-second-desktop-launch-does-nothing)
- [The API is reported as unreachable](#the-api-is-reported-as-unreachable)
- [The wrong port](#the-wrong-port)
- [Empty answer with no error](#empty-answer-with-no-error)
- [The connection drops during a long request](#the-connection-drops-during-a-long-request)
- [Model routing fails](#model-routing-fails)
- [The knowledge base is empty after a restart](#the-knowledge-base-is-empty-after-a-restart)
- [Logs are missing](#logs-are-missing)
- [Quick diagnostic commands](#quick-diagnostic-commands)

---

## llama-server will not start

### The process exits immediately with `0xC0000135`

**Symptom.** `llama-server.exe` starts and dies instantly with no output. The Windows exit code is
`0xC0000135`, which is `STATUS_DLL_NOT_FOUND`.

**Cause.** `cublas64_12.dll` and `cudart64_12.dll` are not on `PATH`. LM Studio ships them in a
separate vendor directory that it does not add to the system path.

**Fix.** Add both directories to `PATH` before launching:

```
C:\Users\admin\.lmstudio\extensions\backends\llama.cpp-win-x86_64-nvidia-cuda12-avx2-2.65.1
C:\Users\admin\.lmstudio\extensions\backends\vendor\win-llama-cuda12-vendor-v2
```

`deploy/start_servers.ps1` does this and fails fast with:

```
找不到 CUDA vendor DLL 目录: <path>
缺少 cublas/cudart 会导致 llama-server 无法启动
```

The PyInstaller desktop build needs the same treatment — the packaged exe has to find the CUDA
DLLs on its own. That fix is recorded in the [CHANGELOG](../CHANGELOG.md).

### "找不到模型"

**Symptom.**

```
找不到模型: E:\model\models\lmstudio-community\gpt-oss-20b-GGUF\gpt-oss-20b-MXFP4.gguf
```

**Cause.** The GGUF files are not where `deploy/start_servers.ps1` expects them.

**Fix.** Either place the files at the expected paths, or edit the `$ModelDir`, `$GptOssModel`, and
`$QwenModel` constants at the top of `deploy/start_servers.ps1`.

---

## High idle CPU usage

**Symptom.** With no requests in flight, the API process holds roughly 32% of 16 logical cores.
Inside the process, 106 threads and 20 CPU-seconds accumulate over a 20-second window.

**Cause.** Not request handling. PyTorch opens its intra-op thread pool to every logical core by
default, and the resident `bge-m3` / `bge-reranker-v2-m3` OpenMP pools spin-wait after the models
load.

`nice` does not help. It lowers scheduling weight but does not stop the spin-wait; only limiting
the thread count does.

**Fix.** `knowledge/threading_limits.py` caps threads to 4 while idle and raises them to 8 during
inference, restoring 4 afterwards. Override with:

```powershell
$env:AGENT_TORCH_THREADS = "4"
$env:AGENT_TORCH_INFER_THREADS = "8"
```

Idle consumption drops from 32% to below 0.03%.

Do not set `AGENT_TORCH_INFER_THREADS` to 16. The cross-encoder is pure CPU matrix work, and
oversubscription makes it slower:

| Threads | Time (n=20, max_length=256) | Time (n=50, max_length=256) |
| --- | --- | --- |
| 4 | 19.1 s | 32.3 s |
| 8 | 10.8 s | 18.8 s |
| 16 | slower than 8 | slower than 8 |

---

## CUDA out of memory

**Symptom.**

```
CUDA error: an illegal memory access
```

Both inference servers crash at the same time. Reproduced twice on a 16 GB card.

**Cause.** `bge-m3` was placed on the GPU. Together with `gpt-oss-20b` the requirement is 22.3 GB
against 16 GB of VRAM.

**Fix.** Keep BGE on CPU. `BGE_DEVICES` defaults to `cpu` and `resolve_flag_devices` forces it. Do
not "optimise" it onto the GPU:

```powershell
$env:BGE_DEVICES = "cpu"
```

If you have a larger card and genuinely want BGE on the GPU, set `BGE_DEVICES=cuda:0` and check
`nvidia-smi` first. `bge-m3` is a pure encoder and retrieval is low frequency, so CPU is entirely
sufficient.

---

## The first knowledge-base query times out

**Symptom.**

```
[TIMEOUT] 工具 'kb_search' 执行超时
```

**Cause.** The first call pays the BGE weight load — about 46 seconds. The knowledge tool timeout
was raised from 20 s to 60 s precisely because a 20-second budget killed a healthy load, and the
model was actually loading correctly the whole time.

**Fix.** No configuration change is needed for the timeout itself. But the *connection* still
breaks, because the client is waiting while the server's keep-alive expires:

```
ReadError: [WinError 10054]
```

Start the API with a longer keep-alive:

```powershell
python -m uvicorn agent_platform.api.app:app --host 127.0.0.1 --port 8010 --timeout-keep-alive 120
```

`deploy/start_api.ps1` and `deploy/start_platform.ps1` both pass `--timeout-keep-alive 120` for
exactly this reason.

---

## Budget exhausted too early

**Symptom.** "Budget exhausted" after what looks like four rounds, while `max_rounds` is 8.

**Cause.** A double-count bug. Both the agent node and the tools node called
`BudgetGuard.check()`, which tests `rounds >= max_rounds`. But rounds are only charged by the agent
node, so one agent-to-tools round trip triggered the check twice. Eight rounds of budget yielded
four usable rounds.

**Fix.** Already fixed. `check_no_rounds()` now exists for the tools node: it checks the token
budget, the context window, the tool-call count, and elapsed time, but not rounds. Rounds are
charged and judged in the agent node only.

The default cap was also raised from 8 to 20 via `AGENT_MAX_ROUNDS`, and `/chat` now accepts a
per-request `max_rounds` (range 1-64):

```json
{ "message": "...", "max_rounds": 30 }
```

**If you still hit it**, check the `stop_reason` in the response. `budget_exceeded:MAX_ROUNDS` means
the round cap was reached; `budget_exceeded:MAX_TOKENS` means the token budget, which is 60,000
per session.

---

## Code is never written

**Symptom.** The agent says it wrote the file, but nothing appears on disk. Or the model keeps
explaining that a rule is blocking it instead of writing.

**Cause 1 — the write tool was rejected as a duplicate.** The error reaching the model is:

```
DUPLICATE_TOOL_CALL: 工具 'ws_write_file' 在本轮已被调用 N 次，已拦截本次重复调用
```

This was caused by `tool_call_counts` and `query_counts` not being reset between requests. They
are graph state fields, persisted by the checkpointer per `thread_id`, but their semantics are
"do not repeat the same thing within this turn". After asking the agent to write three files, every
later request's `ws_write_file` was rejected — the model could only explain that rule 9 blocked it.

Already fixed: `/chat` now explicitly resets both counters in the initial state.

**Cause 2 — the write was counted per tool name instead of per path.** The rule
`MAX_SAME_TOOL_CALLS = 2` was originally counted by tool name. Asking the agent to write `demo.py`
and `tutorial.md` hit the limit on the third file. Write tools (`ws_write_file`, `ws_edit_file`) are
now counted by **target path**, and the same path may be written up to
`MAX_SAME_WRITE_PATH_CALLS` times — "create the file, then edit it" is a normal workflow.

**Cause 3 — the write is pending approval.** See the next section.

**Fix.** If you hit `DUPLICATE_TOOL_CALL` with the current code, start a **new thread** rather than
reusing the old `thread_id` — a stale checkpoint may still hold old counters.

---

## The approval appears to hang

**Symptom.** The approval card shows, the file never appears on disk, and the trace shows the write
as successful.

**Causes and fixes.**

**The trace shows a green check for a file that was never written.** The trace entry for a tool
call is created when the agent node emits the call, which is before the tools node discovers the
approval requirement. The old code computed `ok=True` at creation time, so the UI rendered a green
"success" for a write that had not executed. That was the main source of the "the code never gets
written" impression.

Already fixed. The status is now three-state:

| `ok` | `status` | Rendered as |
| --- | --- | --- |
| `true` | `success` | Green check |
| `false` | `error` | Red cross |
| `null` | `pending_approval` | Amber "awaiting approval" |

**The approval was never granted.** Clicking **Approve** on the card is the only thing that
releases it. Replying "确认执行" in the chat does not — the platform says so explicitly in the
answer text, because it used to be genuinely ambiguous.

**The graph was rebuilt on resume.** `/approve` used to call `ensure_graph()` with no arguments,
building a fresh graph. That silently reset `max_rounds`, the model, and the mode. Already fixed:
`/approve` now reuses the graph registered for the `thread_id`.

**Fix.** Check the response body. `status` should be `pending_approval` and `approvals` should
contain the item with its `tool_call_id`. If `approvals` is empty but the write did not happen, the
graph instance was lost — usually a process restart between the `/chat` and the `/approve`. Re-issue
the request.

---

## The trace panel does not update after approval

**Symptom.** After approving, the chat card updates but the decision-trace panel still shows the
pre-approval frame — with `ws_write_file` still marked amber "awaiting approval" even though the
file is now on disk.

**Cause.** `/chat` streams through `_run_graph_stream`, which accumulates the trace as it goes.
`/approve` used `graph.invoke()`, which returns only the final state — and `_build_response` did
not build a trace at all.

**Fix.** Already fixed. `/approve` now calls `_build_trace_from_state(result)` and returns the
trace, plus a `budget` snapshot so the round and token counters do not reset to zero.

---

## The C: drive fills up

**Symptom.** `C:` runs out of space after using the desktop app.

**Cause.** The old `--onefile` build extracted roughly 7.5 GB into `%TEMP%\_MEIxxxxxx` on every
launch, and abandoned `_MEI*` directories accumulated.

**Fix.** Switched to `--onedir`. No runtime extraction happens at all; the launch time dropped from
30-90 s to 823 ms.

To clean up leftovers from earlier builds:

```powershell
python scripts\clean_c_drive.py
```

You can also move the runtime data off the system drive entirely:

```powershell
$env:AGENT_DATA_DIR = "D:\agent-data"
```

See [`04_数据目录外置.md`](04_数据目录外置.md) for the full guide.

---

## Retrieval quality is poor

**Symptom.** `kb_search` returns irrelevant results. The logs contain a warning that begins with a
warning sign and the text `警告：Embedding 已降级为 **hash 确定性伪向量**。` — match that text; the
warning sign is the leading emoji character in the log line.

**Cause.** `FlagEmbedding` is not installed, or the `bge-m3` weights are not on disk. The package
can be installed while the weights are absent — the probe fails and the embedder falls back to hash
pseudo-vectors and the reranker to cosine similarity.

Hash vectors are deterministic (the same text always gives the same vector) and shared vocabulary
produces some similarity signal, but it is not semantic similarity. The ranking is not meaningful.

**Fix.**

```powershell
python -m pip install FlagEmbedding
python scripts\fetch_bge.py
python scripts\fetch_reranker.py
```

Then confirm the paths:

```powershell
$env:BGE_M3_PATH = "E:\model\models\bge-m3"
$env:BGE_RERANKER_PATH = "E:\model\models\bge-reranker-v2-m3"
```

`requirements.txt` intentionally lists only the minimum runnable set, so this gap is expected.

**Also check** the `/health` response. `kb_runtime` reports the retrieval chain's runtime
parameters, and the embedder logs a warning naming the active backend when it has degraded. The
authoritative signal is the log line itself — `Embedding 已降级为 hash 确定性伪向量` means the
fallback is active and the ranking is not meaningful.

---

## The desktop app shows a console window

**Symptom.** Double-clicking the packaged exe opens a black console window alongside the app
window.

**Fix.** Rebuild with `--window`. `deploy/build_desktop.ps1` already does this by default.

The consequence of `--window` is that `sys.stdout` and `sys.stderr` are `None` in a windowed build,
so the first `print()` would raise `AttributeError` and kill the app. `desktop/entry_point.py`
redirects both to a log file before anything else runs:

```
%LOCALAPPDATA%\AgentPlatform\logs\desktop.log
```

For a console-visible run, use `deploy/start_desktop.ps1` or `python -m agent_platform.desktop`.

---

## The second desktop launch does nothing

**Symptom.** Launching the app twice results in one window.

**Fix.** That is the intended behaviour. The second instance finds the existing window by title,
restores it, brings it to the front, prints a one-line notice, and exits.

The single-instance guard is a Win32 named mutex, `Local\AgentPlatformDesktopSingleton`. If the
first instance was killed abnormally, the mutex dies with the process and the next launch starts
normally.

---

## The API is reported as unreachable

**Symptom.** The desktop shell exits immediately with an "API unreachable" message, even though the
API is running.

**Cause.** The desktop shell probes `/health` with a 3-second timeout. `/health` probes the LLM
endpoints, scans the tool registry, and queries SQLite. Even after parallelisation it can take 1-3
seconds. If a model endpoint is busy generating and `--parallel 1` queues it behind other work, the
probe times out and the shell concludes the API is down.

**Fix.** Already fixed. `/ping` was added as a pure liveness probe: it touches no model, no
database, and no index, and returns in milliseconds. The desktop shell now probes `/ping`.

If you write your own readiness check, use `/ping` for liveness and `/health` for readiness, with
generous timeouts.

---

## The wrong port

**Symptom.** `http://127.0.0.1:8010/ui/` does not load, or the verification scripts report
connection refused.

**Cause.** The API port is inconsistent across the repository:

| Source | Port |
| --- | --- |
| `deploy/start_api.ps1` | `8000` |
| `deploy/start_platform.ps1` | `8010` |
| `deploy/start_desktop.ps1` | `8010` |
| Verification scripts (`AGENT_API_BASE`) | `8010` |

**Fix.** Use `8010`. If you started the API with `deploy/start_api.ps1`, either pass
`-Port 8010` or use `8000` consistently:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\start_api.ps1 -Port 8010
```

---

## Empty answer with no error

**Symptom.** The response has `status: "done"` but `answer` is an empty string.

**Cause 1 — an approval is pending.** When `pending_hitl` is non-empty, the answer carries an
explicit notice that begins with a warning sign followed by the bold text `**文件尚未写入。**`, and
continues:

> 以下写操作已发起，但正**挂在人工审批上、尚未执行**：ws_write_file。
>
> 请在下方审批卡片上点击「**批准**」后才会真正落盘；**在对话里回复「确认执行」不会自动放行**。

**Cause 2 — the turn ended on a stop reason with no answer.** Check `stop_reason`:

| `stop_reason` | Meaning |
| --- | --- |
| `done` | Completed with an answer. |
| `empty_response` | The model returned nothing usable. |
| `pending_approval` | Waiting for a human. See `approvals`. |
| `budget_exceeded:MAX_ROUNDS` | The round cap was reached. |
| `budget_exceeded:MAX_TOKENS` | The token budget (60,000 per session) was reached. |
| `budget_exceeded:MAX_CONTEXT` | The context window (32,768) was reached. |
| `budget_exceeded:MAX_TOOL_CALLS` | The tool-call cap (24) was reached. |
| `budget_exceeded:MAX_SECONDS` | The wall-clock cap (900 s) was reached. |
| `rejected_by_human` | The approval was denied. |
| `repeated_failure` | The same tool with the same arguments failed repeatedly. |
| `llm_error:<EXC>` | The model call raised. |
| `exception` | An unhandled error; `answer` holds the exception text. |

**Cause 3 — the fast path produced a bare result.** When the approval fast path fires, the tool's
own JSON result becomes the answer. It is complete and self-describing, but it is JSON, not prose.
Set `"summarize": true` on the `/approve` request to force the model to write it as prose.

---

## The connection drops during a long request

**Symptom.**

```
ReadError: [WinError 10054]
```

**Cause.** The server's keep-alive expired while the client was still waiting. The default uvicorn
keep-alive is 5 seconds, and the first knowledge-base query takes around 46 seconds while BGE
loads.

**Fix.** Start the API with `--timeout-keep-alive 120`. Both `deploy/start_api.ps1` and
`deploy/start_platform.ps1` already do.

---

## Model routing fails

**Symptom.**

```
RoutingCapabilityError: ...
```

**Cause.** A route declares `requires: [...]`, and no model in its fallback chain carries all the
required capability tags. The router strips the ineligible members and raises rather than
silently downgrading.

**This is the intended behaviour.** A silent downgrade is worse than an error: an earlier version
of the `agent` and `tool_call` routes listed `qwen2.5-14b` as a fallback, so when `gpt-oss-20b` was
unavailable a code-writing task quietly landed on a general chat model. Nothing raised. The output
just got worse, and the only symptom was "the model seems worse today".

**Fix.** Either add a model with the missing capability to the chain, or relax the route's
`requires:` if the constraint was too strict.

```yaml
routes:
  agent:
    fallback: [gpt-oss-20b]
    requires: [code, tool_call]
```

---

## The knowledge base is empty after a restart

**Symptom.** `GET /kb/documents` returns nothing, even though documents were imported.

**Cause 1 — the data directory moved.** If `AGENT_DATA_DIR` now points somewhere else, the platform
reads a different, empty index.

**Fix.** Check where it is pointing:

```powershell
python -c "from agent_platform.core import paths; print(paths.data_dir_info())"
```

**Cause 2 — the index was never built.** The chunk index lives at `<data>/kb/chunks.db`, separate
from the source documents in `<data>/docs`.

**Fix.** Rebuild it:

```powershell
curl -X POST http://127.0.0.1:8010/kb/reindex
# or
python scripts\build_index.py
```

**Cause 3 — the checkpointer fell back to `MemorySaver`.** Without
`langgraph-checkpoint-postgres`, graph checkpoints are in-memory only. Session messages still
persist in `data/sessions.db`, but in-flight graph state does not survive a restart.

---

## Logs are missing

**Symptom.** `agent_platform.*` log messages never appear in the log file, even though the code
clearly logs them.

**Cause.** uvicorn's `LOGGING_CONFIG` only configures handlers for the three `uvicorn.*` loggers.
There is no root handler, and because `dictConfig` has already created a root logger with no
handlers, the `lastResort` fallback does not fire either. The messages are silently dropped.

**Fix.** The thread limiter works around this by writing to `stderr` in addition to logging:

```python
logger.warning(msg)
print(msg, file=sys.stderr, flush=True)
```

`stderr` is what `api_err.log` captures, so the evidence lands on disk.

When adding logging elsewhere, check whether the message reaches a handler. If it does not, write
to `stderr` as well.

---

## Quick diagnostic commands

```powershell
# Is the API alive? (fast - no model, no database)
curl http://127.0.0.1:8010/ping

# Full health: models, tools, knowledge base, data directory
curl http://127.0.0.1:8010/health

# Which model endpoints are up?
curl http://127.0.0.1:8080/v1/models
curl http://127.0.0.1:8081/v1/models

# Where is the data directory?
python -c "from agent_platform.core import paths; print(paths.data_dir_info())"

# What tools are registered?
curl http://127.0.0.1:8010/tools

# Is retrieval degraded?
curl "http://127.0.0.1:8010/kb/search?query=test"

# What is the model registry, and is each endpoint reachable?
curl http://127.0.0.1:8010/models

# Counters and recent audit entries
curl http://127.0.0.1:8010/metrics
curl http://127.0.0.1:8010/audit

# Where is the CPU going?
python scripts\diag_health_cpu.py
```

The desktop application log is at:

```
%LOCALAPPDATA%\AgentPlatform\logs\desktop.log
```
