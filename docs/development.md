# Development Guide

How to set up, extend, test, and ship this project.

For the system design see [`architecture.md`](architecture.md). For the API surface see
[`api.md`](api.md). For configuration see [`configuration.md`](configuration.md).

---

## Table of contents

- [Environment setup](#environment-setup)
- [Running the platform](#running-the-platform)
- [Tests and verification](#tests-and-verification)
- [Script inventory](#script-inventory)
- [Adding a tool](#adding-a-tool)
- [Adding an endpoint](#adding-an-endpoint)
- [Adding a model alias](#adding-a-model-alias)
- [Building the desktop bundle](#building-the-desktop-bundle)
- [Coding conventions](#coding-conventions)
- [Documentation expectations](#documentation-expectations)

---

## Environment setup

### Requirements

| Item | Version | Notes |
| --- | --- | --- |
| Python | 3.13 | The local interpreter is 3.13.9 on Windows 11. Some packages lack prebuilt wheels for 3.13; the code handles that with lazy imports and graceful degradation. |
| OS | Windows 11 | The backend is portable; the desktop shell and the launch scripts are Windows-native. |
| GPU | 16 GB VRAM | `gpt-oss-20b` MXFP4 at `-ngl 99` occupies about 15.4 GB. |
| RAM | 94 GB | `Qwen2.5-14B` resident on CPU needs about 9 GB plus the BGE models. |
| llama.cpp | LM Studio backend 2.65.1 | Provides `llama-server.exe` plus the CUDA vendor DLLs. |

### Install

```powershell
cd E:\PYPROJECT\agent
python -m pip install -r requirements.txt
```

`requirements.txt` is the **minimum runnable set**. For real retrieval quality you also need:

```powershell
python -m pip install FlagEmbedding numpy torch
```

Without `FlagEmbedding` the embedder degrades to hash pseudo-vectors and the reranker to cosine
similarity. It runs, it just does not retrieve meaningfully, and only a log warning tells you.

### Environment file

```powershell
Copy-Item .env.example .env
```

`.env.example` contains 16 variables the code does not read. See
[Known discrepancies](configuration.md#known-discrepancies) before relying on it. The variables that
matter most:

```
AGENT_DATA_DIR=
LLM_BASE_URL=http://127.0.0.1:8080/v1
LLM_MODEL=gpt-oss-20b
LLM_CONTEXT_WINDOW=32768
BGE_M3_PATH=E:\model\models\bge-m3
BGE_RERANKER_PATH=E:\model\models\bge-reranker-v2-m3
```

### Model weights

```powershell
python scripts\fetch_bge.py          # bge-m3
python scripts\fetch_reranker.py     # bge-reranker-v2-m3
python scripts\download_models.py    # both, plus GGUF orchestration
```

The LLM GGUF files are expected at:

```
E:\model\models\lmstudio-community\gpt-oss-20b-GGUF\gpt-oss-20b-MXFP4.gguf
E:\model\models\lmstudio-community\Qwen2.5-14B-Instruct-1M-GGUF\Qwen2.5-14B-Instruct-1M-Q4_K_M.gguf
```

### The llama-server PATH requirement

Both directories must be on `PATH`, or `llama-server.exe` dies with exit code `0xC0000135`
(`STATUS_DLL_NOT_FOUND`) because `cublas64_12.dll` and `cudart64_12.dll` are missing:

```
C:\Users\admin\.lmstudio\extensions\backends\llama.cpp-win-x86_64-nvidia-cuda12-avx2-2.65.1
C:\Users\admin\.lmstudio\extensions\backends\vendor\win-llama-cuda12-vendor-v2
```

`deploy/start_servers.ps1` handles this and fails fast with a clear message.

---

## Running the platform

```powershell
# Inference servers
powershell -ExecutionPolicy Bypass -File deploy\start_servers.ps1

# API (port 8010)
powershell -ExecutionPolicy Bypass -File deploy\start_platform.ps1

# API with autoreload, for development
python -m uvicorn agent_platform.api.app:app --host 127.0.0.1 --port 8010 --reload --timeout-keep-alive 120

# Desktop shell
python -m agent_platform.desktop
```

Always pass `--timeout-keep-alive 120`. The first knowledge-base query pays a cold BGE load of
roughly 46 seconds, and the default 5-second keep-alive reclaims the socket while the client is
still waiting — producing `ReadError: [WinError 10054]`.

### Static assets

The SPA is served straight from `agent_platform/api/static/`. There is no build step. Editing a
`.js` or `.css` file and reloading the browser is the whole loop.

For the desktop build, the static directory is bundled with
`--add-data "$StaticDir;agent_platform/api/static"`.

---

## Tests and verification

```powershell
python scripts\smoke_test.py          # fast smoke test
python scripts\run_all_tests.py       # full verification sweep
python scripts\run_eval.py            # evaluation
python scripts\run_regression.py      # regression with gate rules
python scripts\selfhost_verify.py     # self-hosted end-to-end verification
```

`run_all_tests.py` is the sweep entry point. The individual `verify_*.py` scripts can also be run
individually; most assume the API is already running on `AGENT_API_BASE`.

### Where the tests live

There is no `tests/` directory. Verification lives in `scripts/` as executable Python. This is a
deliberate choice: the scripts double as operational tools — they can be pointed at a live
deployment to check its health, not just at a test fixture.

---

## Script inventory

61 files in `scripts/` — 60 Python and 1 JavaScript.

### Feature verification

| Script | Verifies |
| --- | --- |
| `verify_e2e.py` | The full chat-to-answer path end to end. |
| `verify_rag.py` | Retrieval quality against the knowledge base. |
| `verify_tools.py` | The tool registry: registration, schema validation, permissions. |
| `verify_workspace.py` | Staged writes, apply, undo, path safety. |
| `verify_modes.py` | The agent / ask / plan mode semantics and limits. |
| `verify_kb_admin.py` | Knowledge-base document administration. |
| `verify_kb_rebuild_persistence.py` | The index survives a rebuild. |
| `verify_model_routing.py` | Capability-aware routing and degradation protection. |
| `verify_model_lifecycle.py` | Start/stop of llama-server processes. |
| `verify_observability.py` | The trace, the timeline, and the metrics. |
| `verify_trace_view.py` | The decision-trace rendering. |
| `verify_desktop.py` | The desktop shell launches and connects. |
| `verify_platform_launch.py` | The platform launch endpoint. |
| `verify_web_ui.py` | The SPA loads and its assets resolve. |
| `verify_retrieval_policy.py` | The per-turn retrieval decision. |

### Health and performance

| Script | Verifies |
| --- | --- |
| `verify_api_responsiveness.py` | Response latency under load. |
| `verify_cold_health.py` | Health during a cold start. |
| `verify_health_during_cold.py` | Health while a model is loading. |
| `verify_kb_search_latency.py` | Knowledge-base search latency. |
| `diag_health_cpu.py` | CPU consumption of the API process. |
| `diag_health_breakdown.py` | Where the health-check time goes. |

### Diagnostics

| Script | Purpose |
| --- | --- |
| `diag_warmup_lock.py` | The BGE warm-up lock. |
| `dump_api_stacks.py` | Dumps thread stacks from a running API process. |
| `probe_reranker.py` | Reranker backend and quality probe. |
| `probe_search.py` | Search backend probe. |
| `sessions_db_dump.py` | Dumps the sessions database. |
| `modes_behavior_evidence.py` | Captures evidence of the mode semantics. |
| `selfhost_verify.py` | Self-hosted end-to-end verification. |
| `kill_stale_tests.py` | Cleans up leftover test processes. |

### Model fetching

| Script | Purpose |
| --- | --- |
| `fetch_bge.py` | Downloads the `bge-m3` weights. |
| `fetch_bge_ms.py` | Multi-shard `bge-m3` download. |
| `fetch_bge_shards.py` | Shard-by-shard `bge-m3` download. |
| `fetch_reranker.py` | Downloads the `bge-reranker-v2-m3` weights. |
| `download_models.py` | Orchestrates all of the above. |

### UI capture

| Script | Purpose |
| --- | --- |
| `ui_shots.py` | General UI screenshots. |
| `ui_shots_task.py` | Task-workbench screenshots. |
| `ui_newfeatures_shots.py` | Screenshots of recently added features. |
| `ui_overlap_check.py` | Detects overlapping UI elements. |
| `ui_input_check.py` | Verifies input handling. |
| `ui_style_check.py` | Verifies style consistency. |
| `ui_ws_check.py` | Verifies the workspace view. |
| `tl_shots.py` | Timeline screenshots. |
| `capture_observability.py` | Captures the observability panels. |
| `capture_delivery.py` | Captures the delivery report artefacts. |
| `capture_desktop_window.py` | Captures the desktop window. |
| `cdp_shot.py` | Screenshot via the Chrome DevTools Protocol. |

### Maintenance

| Script | Purpose |
| --- | --- |
| `migrate_data_dir.py` | Moves the data directory to another drive. |
| `build_index.py` | Rebuilds the knowledge-base index. |
| `clean_c_drive.py` | Reclaims disk space on the system drive. |
| `restart_api.py` | Restarts the API process. |
| `js_syntax_check.py` | Syntax-checks the frontend JavaScript. |
| `desktop_title.py` | Inspects or sets the desktop window title. |
| `run_all_tests.py` | The verification sweep. |
| `run_all_tests_selfhosted.py` | The sweep against a self-hosted deployment. |
| `run_eval.py` | Runs the evaluation suite. |
| `run_regression.py` | Runs the regression suite with gate rules. |
| `smoke_test.py` | The fast smoke test. |
| `_obs_suite_runner.py` | The observability capture suite runner. |
| `_patch_base_env.py` | Test helper for environment patching. |

### Script environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `MODEL_ROOT` | `E:\model\models` | Where weights are downloaded. |
| `AGENT_API_BASE` | `http://127.0.0.1:8010` | API base URL. |
| `AGENT_UI_BASE` | `http://127.0.0.1:8010` | UI base URL. |
| `PIP_CACHE_DIR` | *(pip default)* | pip cache location. |
| `HF_HOME` | *(HF default)* | HuggingFace cache location. |

---

## Adding a tool

Tools come in two shapes. Pick the right home.

### Where the code goes

| Tool type | File |
| --- | --- |
| Generic platform tool | `agent_platform/tools/__init__.py` |
| Domain tool that needs the knowledge layer | `agent_platform/knowledge/tools_ext.py` |
| Tool that is really a knowledge-base operation | `agent_platform/knowledge/tools.py` |

### Step 1 — write the specification

A `ToolSpec` factory. The name, the description, and the schema are what the model sees, so they
matter more than the implementation.

```python
def _my_tool_spec() -> ToolSpec:
    return ToolSpec(
        name="my_tool",
        description=(
            "一句话说明这个工具做什么、什么时候该用。"
            "描述会原样进入模型的工具列表 —— 写清楚边界，模型才不会误用。"
        ),
        input_schema={
            "type": "object",
            "required": ["target"],
            "additionalProperties": False,
            "properties": {
                "target": {"type": "string", "minLength": 1,
                           "description": "目标标识"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100,
                          "default": 20, "description": "返回条数上限"},
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
            },
        },
        permission=Permission.READ_ONLY,
        hitl_required=False,
        timeout_ms=30_000,
        max_retries=2,
        version="1.0.0",
        idempotent=True,
        metadata={"description": "人类可读说明", "impact": "只读，无副作用"},
    )
```

**Choosing the permission is what decides whether HITL fires.**

| Permission | Behaviour |
| --- | --- |
| `READ_ONLY` | Runs automatically. |
| `COMPUTE` | Runs automatically, cost is metered. |
| `WRITE_DATA` | Suspends for approval. Needs idempotency and a rollback path. |
| `EXTERNAL_SIDE_EFFECT` | Suspends for approval. |
| `ADMIN` | Suspends for approval. |

`hitl_required` defaults to whether the permission is in `HITL_REQUIRED`
(`WRITE_DATA`, `EXTERNAL_SIDE_EFFECT`, `ADMIN`). Set it explicitly only when you need to override
that.

### Step 2 — write the implementation

The signature is `(args: dict, ctx: dict) -> Any`.

```python
def _impl_my_tool(args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
    target = str(args.get("target") or "").strip()
    if not target:
        raise ToolError("INVALID_ARGS", "target 不能为空", hint="传一个具体的目标标识")
    # ...
    return {"items": [...], "total": len(items)}
```

Raise `ToolError(code, message, retryable, hint)`. The `hint` is shown to the model and is how it
learns what to do differently. Never let a Python traceback reach the model.

### Step 3 — register it

In `agent_platform/tools/__init__.py`, add the pair to the `pairs` list inside `register_all()`:

```python
pairs = [
    (_kb_search_spec(), _impl_kb_search),
    # ...
    (_my_tool_spec(), _impl_my_tool),
]
```

For a knowledge-layer tool, expose a `register_knowledge_ext_tools(registry)` function in
`agent_platform/knowledge/__init__.py` and it will be picked up automatically.

### Step 4 — verify

```powershell
# Is it registered?
curl http://127.0.0.1:8010/tools/my_tool

# Does the registry agree?
python scripts\verify_tools.py
```

### Step 5 — check the audit and HITL behaviour

If the tool writes, confirm that:

1. A call to it produces `status: "pending_approval"`, not a direct execution.
2. The approval card carries a sensible `impact` and `reversible`.
3. The audit log records both the staged attempt and the applied result.

---

## Adding an endpoint

Endpoints live in `agent_platform/api/app.py`, inside the `create_app` factory.

```python
@app.get("/my-feature")
async def my_feature() -> Any:
    """一句话说明这个端点做什么。"""
    try:
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return _ws_error(exc)
```

Conventions:

- Use `response_model=None` when returning a `JSONResponse` directly.
- Add a request model class next to the others (`ChatRequest`, `ApproveRequest`, and so on). Every
  field must have a default so existing clients keep working.
- Return structured errors with a stable `code`, not bare HTTP errors, for the resource endpoints.
- New response fields are additive. Clients read by field name.

After adding an endpoint, update the endpoint tables in [`api.md`](api.md) and the README's API
reference section.

---

## Adding a model alias

### A built-in model

Add it to the `models:` section of `config/routing.yaml`:

```yaml
  my-model-7b:
    base_url: http://127.0.0.1:8083/v1
    model: my-model-7b
    reasoning_effort: low
    temperature: 0.2
    max_tokens: 2048
    api_key: local
    timeout: 180
    capabilities: [tool_call, reasoning, chinese]
    description: 我的 7B 模型
```

Then add it to the routes that should use it:

```yaml
  chinese:
    fallback: [qwen2.5-14b, my-model-7b, gpt-oss-20b]
```

### Capability tags matter

If a route declares `requires:`, the router strips every chain member lacking those tags. If
nothing is left, it raises `RoutingCapabilityError`. This is intentional: silently routing a code
task to a model without the `code` capability produces quietly bad output with no error signal.

So when you add a model to a `requires:`-constrained chain, it must carry every required tag.

### A user-added model

Users add models through the UI or `POST /models`. Those persist to `data/models.json` and do not
need a code change.

### Verify

```powershell
python scripts\verify_model_routing.py
curl http://127.0.0.1:8010/models
```

The response includes a live reachability probe, so the status reflects reality.

---

## Building the desktop bundle

```powershell
powershell -ExecutionPolicy Bypass -File deploy\build_desktop.ps1
powershell -ExecutionPolicy Bypass -File deploy\build_desktop.ps1 -SkipInstall   # reuse the current env
```

Output:

```
dist\desktop\AgentPlatform\AgentPlatform.exe
```

### Build flags and why

| Flag | Reason |
| --- | --- |
| `--onedir` | `--onefile` extracts the whole runtime (~7.5 GB) into `%TEMP%\_MEIxxxxxx` on every launch: 30-90 s startup, concurrent launches fighting for disk, and silent bootloader failures. Onedir launch measured at 823 ms. |
| `--window` | No console window on double-click. The cost is that stdout/stderr are `None`, which `desktop/entry_point.py` handles by redirecting them to a log file before anything else runs. |
| `--name AgentPlatform` | The output executable name. |
| `--distpath`, `--workpath`, `--specpath` | Build outputs under `dist\desktop\` and `build\pyinstaller\`. |
| `--add-data "<static>;agent_platform/api/static"` | Bundles the SPA. |

Do **not** add `--runtime-tmpdir`. It only applies to onefile, PyInstaller warns about it under
onedir, and it was the original source of the "C: drive is full" problem.

### After building

The artifact is a **directory**, not a single file. Any shortcut, VBS launcher, or documentation
reference must point at:

```
dist\desktop\AgentPlatform\AgentPlatform.exe
```

### Desktop diagnostics

| File | Contents |
| --- | --- |
| `%LOCALAPPDATA%\AgentPlatform\logs\desktop.log` | stdout and stderr from a windowed build. |
| `<data>/logs/desktop.log` | Logs when the data directory is configured. |

For a console-visible run, use `deploy/start_desktop.ps1` or `python -m agent_platform.desktop`
instead of the packaged exe.

---

## Coding conventions

These are the conventions observed in the codebase, not aspirational ones.

### Comments explain why, in Chinese

The house style is a Chinese comment that explains the reasoning behind a non-obvious decision,
usually including the date and the concrete failure it prevents. This is not a stylistic accident —
it is the codebase's primary engineering record.

```python
#: **为什么不能把 BGE 挪到 GPU**：单卡 16 GB，gpt-oss-20b 常驻已占
#: ~15.4 GB，BGE 上 GPU 会 OOM 并连带两个推理服务崩溃
#: （见 embed.py 中 ``resolve_flag_devices`` 的详细说明）。
```

Compare with a comment that merely restates the code:

```python
# 设置线程数
```

The first form tells the next reader what will break if they change this. The second does not.

**When writing new comments, match the surrounding language.** Chinese comments in the modules that
already use them; English comments in the modules that already do.

### Docstrings

Docstrings describe the contract: arguments, return values, raised exceptions, and the failure
modes a caller should handle.

```python
def check_no_rounds(self, est_tokens: int = 0) -> None:
    """检查预算，但**不检查轮次上限**（tools 节点专用）。

    为什么需要这个方法（2026-11-24 修复）
    --------------------------------------
    ``check()`` 里的 ``self.rounds >= self.max_rounds`` 是**基于已计费轮次**
    的判定。而轮次是在 **agent 节点**调 ``charge_round()`` 时推进的。
    ...
    """
```

### Type hints

Modern syntax throughout: `Optional[X]`, `Dict[str, Any]`, `list[str]`. The codebase targets
Python 3.13, so `X | None` is also acceptable in new code.

### Deferred imports for optional dependencies

Third-party packages that may not be installed are imported inside the function, with a comment
explaining the deferral:

```python
try:
    import torch  # noqa: PLC0415 - 延迟导入
except Exception:  # noqa: BLE001 - 没 torch 就直接跳过
    yield n
    return
```

### Graceful degradation

Optional dependencies degrade rather than crash. The degradation is always logged, at a level that
actually reaches a handler — uvicorn's `LOGGING_CONFIG` only configures `uvicorn.*`, so
`agent_platform.*` loggers have no handler and `lastResort` does not fire. The thread limiter works
around this by writing directly to `stderr` as well as logging.

### Error normalisation

Tool errors reach the model as `{code, message, retryable, hint}`. Use `ToolError` and always fill
in the `hint` — it is the model's only guidance.

### No bare `except`

Broad excepts carry `# noqa: BLE001` and a comment saying why the exception is swallowed. It is
acceptable to swallow an exception in a degradation path; it is not acceptable to swallow one
silently.

### Naming

- Modules: `snake_case`.
- Classes: `PascalCase`.
- Functions and variables: `snake_case`.
- Module-level constants: `UPPER_SNAKE_CASE`.
- Private helpers: a leading underscore (`_impl_kb_search`, `_build_response`).

### Frontend

- Vanilla JavaScript. No framework, no build step, no dependencies.
- Each file has a top-of-file comment block explaining its responsibility.
- `'use strict';` at the top of each file.
- DOM lookups are cached at the top of the function that uses them, not re-queried per event.
- The approval flow goes through `approval.js`, which broadcasts `hitl-resolved`. Do not add a
  second path that resolves approvals — that is exactly the bug the broadcast fixed.

### Test and verification scripts

Scripts in `scripts/` follow the same conventions and print a clear PASS/FAIL summary with the
actual values observed, not just an exit code.

---

## Documentation expectations

When you change something, update the docs in the same change.

| You changed | Update |
| --- | --- |
| An endpoint | `docs/api.md`, the README's API reference table |
| A tool | The README's tool catalog, `docs/api.md` if it added an endpoint |
| An environment variable | `docs/configuration.md`, the README's configuration table, `.env.example` |
| A default or constant | The relevant doc **and** `CHANGELOG.md` |
| A file layout | The README's repository layout, `docs/development.md` |
| A behaviour | `CHANGELOG.md`, under `Added` / `Changed` / `Fixed` |

If you changed a measured performance property, include the new numbers and the measurement
conditions. If you changed a behaviour that users could depend on, say so explicitly, and say what
breaks.

The Chinese design documents in `docs/` are the authoritative source for the domain requirements.
They are not translated; if you change behaviour that contradicts them, update them too.
