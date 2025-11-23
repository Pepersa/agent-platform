# Agent Platform

<div align="center">
  <a href="README.zh-CN.md">中文</a> | <b>English</b>
</div>

A **local-first RAG + agent platform** for engineering teams: a FastAPI backend driving a LangGraph
state machine, two local `llama.cpp` inference servers (GPU + CPU), a hybrid-retrieval knowledge base
with cross-encoder reranking, a 13-tool catalog with human-in-the-loop approval gates, a code-workspace
editor with staged writes and undo, and a vanilla-JS SPA that also ships as a `pywebview` desktop app.

Everything runs on your own machine. No cloud API calls, no telemetry, no account. The only outbound
traffic is to `127.0.0.1`.

![Local inference, local data](https://img.shields.io/badge/inference-local--llama.cpp-blue)
![FastAPI](https://img.shields.io/badge/api-FastAPI-009688)
![LangGraph](https://img.shields.io/badge/orchestration-LangGraph-5319E7)
![License: proprietary](https://img.shields.io/badge/license-all%20rights%20reserved-orange)

---

## Table of contents

- [Feature highlights](#feature-highlights)
- [Screenshots](#screenshots)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [API reference](#api-reference)
- [Tool catalog](#tool-catalog)
- [Interaction modes](#interaction-modes)
- [Knowledge base and RAG](#knowledge-base-and-rag)
- [HITL and approvals](#hitl-and-approvals)
- [Desktop app](#desktop-app)
- [Observability](#observability)
- [Evaluation](#evaluation)
- [Repository layout](#repository-layout)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [Security notes](#security-notes)
- [Known limitations](#known-limitations)
- [Contributing](#contributing)
- [License](#license)
- [Documentation index](#documentation-index)

---

## Feature highlights

| Capability | What it actually does |
| --- | --- |
| **Hybrid RAG, 10-step pipeline** | Query rewrite -> ACL pre-filter -> dual recall (BM25 + dense) -> RRF fusion (`k=60`) -> top-50 dedup -> cross-encoder rerank to top-6 -> context assembly (<= 3000 tokens) -> answer with citations -> citation validation -> abstain threshold. See [Knowledge base and RAG](#knowledge-base-and-rag). |
| **HITL approval gates** | Any tool at `WRITE_DATA`, `EXTERNAL_SIDE_EFFECT`, or `ADMIN` permission suspends the graph via LangGraph `interrupt()`, shows an approval card with a diff, and only runs after a human approves. Approvals can be granted once or for the whole session, and every decision is audited. See [HITL and approvals](#hitl-and-approvals). |
| **Dual local models** | `gpt-oss-20b` on `:8080` with full GPU offload (`-ngl 99`) for the ReAct loop, plus `Qwen2.5-14B` on `:8081` with CPU inference (`-ngl 0`) for Chinese generation, evaluation, and LLM-as-Judge. Routing is capability-aware and refuses to silently downgrade a code task to a non-code model. |
| **Code workspace with undo** | `ws_read_file`, `ws_search_code`, `ws_list_files`, `ws_write_file`, `ws_edit_file` operate on a real local repository. Writes are **staged** as a `change_id`, then applied — which is exactly what makes HITL meaningful and what makes undo possible (`POST /workspace/undo`). |
| **Knowledge-base admin** | Import documents (upload or paste), list them with metadata, reindex individual documents or the whole corpus, delete, and search — all from the UI or the `/kb/*` API. |
| **Custom tools** | Register your own `http` or `script` tools at runtime via `/custom-tools`. They persist to `data/custom_tools.json`, take effect immediately without a restart, and are forced through HITL when they write. |
| **Observability** | A per-request decision trace with three-state tool status (pending / success / failure), a live timeline of LLM and tool steps, per-request budget snapshots, aggregate `/metrics`, and an append-only audit log. See [Observability](#observability). |
| **Evaluation harness** | Four metric tiers (L1 system, L2 retrieval, L3 generation, L4 business) with baseline/target pairs, an LLM-as-Judge with rubric and calibration, a 30-case JSONL dataset, and a regression runner with gate rules. See [Evaluation](#evaluation). |
| **Desktop app** | `pywebview` window packaged with PyInstaller in `--onedir` mode: 823 ms launch instead of 30-90 s, no per-launch 7.5 GB extraction, single-instance mutex, and a silent (no console) build. See [Desktop app](#desktop-app). |
| **Local-first, offline** | All inference is local. All runtime data lives under one configurable `AGENT_DATA_DIR` that can be relocated to another drive. The audit log degrades to SQLite and then JSONL when PostgreSQL is absent. |

---

## Screenshots

All screenshots below are real captures from this repository, stored under `docs/_verify/`.

### Chat with tool rows and dark theme

![Chat tool row, dark theme](docs/_verify/chat-toolrow-fixed-dark.png)

*Chat surface with the tool-call row expanded — the tool row shows the three-state status chip
(pending amber, success green, failure red) rather than a bare spinner.*

### Chat with tool rows and light theme

![Chat tool row, light theme](docs/_verify/chat-toolrow-fixed-light.png)

*The same chat surface in light theme. Both themes ship in the same stylesheet; no separate build.*

### Live timeline, dark theme

![Live timeline, dark theme](docs/_verify/timeline-full-dark.png)

*Live Timeline: every LLM call and every tool execution rendered as a waterfall step, with the
approval-wait steps drawn in a third colour so a stalled HITL is visually obvious.*

### Live timeline, light theme

![Live timeline, light theme](docs/_verify/timeline-full-light.png)

### Decision trace without overlap, dark theme

![Decision trace, dark theme](docs/_verify/trace-overlap-fixed-dark.png)

*Decision Trace panel: reasoning summary, tool name, arguments, and result for each step. This is the
fix for a layout bug where the trace cards overlapped their labels.*

### Decision trace without overlap, light theme

![Decision trace, light theme](docs/_verify/trace-overlap-fixed-light.png)

### Repository map

![Repository map](obs-08-repomap.png)

*Repository map produced by `GET /workspace/repomap` — a flat file listing with per-file line counts,
rendered as a treemap so the size of a codebase is legible at a glance. Feeds the "codebase" treemap
in the observability panel.*

---

## Architecture

### System overview

```
                                   ┌─────────────────────────┐
                                   │  Browser  /  pywebview  │
                                   │  (vanilla-JS SPA)       │
                                   └────────────┬────────────┘
                                                │  HTTP + SSE
                                                ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                          FastAPI  (agent_platform/api/app.py)           │
│                                                                           │
│   /chat (SSE)   /approve (HITL resume)   /workspace/*   /kb/*           │
│   /models/*     /sessions/*   /custom-tools/*   /tasks/*   /metrics      │
│                                                                           │
│   ┌─────────────────────────────────────────────────────────────────┐    │
│   │              LangGraph state machine                            │    │
│   │              (orchestration/graph.py)                           │    │
│   │                                                                 │    │
│   │    START ──▶ agent ──▶ tools ──▶ hitl ──▶ tools ──▶ agent …    │    │
│   │                │        │                  ▲                     │    │
│   │                │        └──▶ END           └── (hard edge)       │    │
│   │                ▼                                               │    │
│   │              plan ──▶ END   (plan mode: never reaches tools)    │    │
│   └───────────┬───────────────────────────────────┬─────────────────┘    │
│               │                                   │                      │
│               ▼                                   ▼                      │
│   ┌────────────────────────┐         ┌──────────────────────────────┐  │
│   │  ToolRegistry          │         │  ModelRouter                 │  │
│   │  (core/tool_registry)  │         │  (core/model_registry +      │  │
│   │  13 built-in tools     │         │   core/llm_client)           │  │
│   │  + N custom tools      │         │  capability-aware routing    │  │
│   └───────────┬────────────┘         └──────────┬───────────────────┘  │
└───────────────┼──────────────────────────────────┼──────────────────────┘
                │                                  │
                ▼                                  ▼
   ┌────────────────────────────┐     ┌───────────────────────────────┐
   │  Knowledge layer           │     │  llama.cpp llama-server       │
   │  chunker / embed /         │     │                               │
   │  reranker / retriever /    │     │  :8080  gpt-oss-20b           │
   │  store (SQLite | pgvector) │     │         GPU full offload      │
   │  FlagEmbedding on CPU      │     │  :8081  Qwen2.5-14B          │
   └────────────┬───────────────┘     │         CPU (-ngl 0)          │
                │                     └───────────────────────────────┘
                ▼
   ┌───────────────────────────────────────────────────────────────────────┐
   │                          Data directory                              │
   │  sessions.db   workspace.db   audit/audit.db   kb/chunks.db          │
   │  models.json   custom_tools.json   logs/   shots/   eval_reports/    │
   └───────────────────────────────────────────────────────────────────────┘
```

### LangGraph nodes and edges

```
mode = plan:
    START ──▶ plan ──▶ END

mode = agent | ask:
    START ──▶ agent
                 │
                 ├── _route_after_agent ──▶ tools
                 │                           │
                 │                           ├── _route_after_tools ──▶ hitl
                 │                           │                          │
                 │                           │                          │  (hard edge)
                 │                           │                          └──▶ tools
                 │                           │
                 │                           ├── _route_after_tools ──▶ agent
                 │                           │
                 └── _route_after_agent ──▶ END
```

Four nodes, defined in `agent_platform/orchestration/graph.py`:

| Node | Line | Role |
| --- | --- | --- |
| `agent` | 639 | Calls the LLM, charges a ReAct round, writes `rounds` / `llm_duration_ms` back to state. |
| `tools` | 968 | Permission check, HITL detection, execution, result truncation, audit. |
| `hitl` | 1528 | Suspension point. Resolves the approval decision, then hands control back to `tools`. |
| `plan` | 1748 | Produces a structured plan (JSON). Never calls a tool. |

**Why `tools` and `hitl` are separate nodes.** LangGraph's `interrupt()` re-runs the interrupted node
from the top when the graph is resumed. If "approve, then execute" lived in one node, resuming would
re-execute the write a second time. Splitting them means `tools` decides *that* approval is needed and
raises the interrupt, while `hitl` only resolves the decision; the actual write happens back in
`tools` on the resume pass.

---

## Quick start

### Prerequisites

- Windows 11 (the desktop shell and launch scripts are Windows-native; the backend itself is portable)
- Python 3.13
- A CUDA-capable GPU (16 GB works; `gpt-oss-20b` MXFP4 occupies roughly 15.4 GB at `-ngl 99`)
- `llama-server` from LM Studio's `llama.cpp` backend, with the CUDA vendor DLLs on `PATH`

### 1. Install dependencies

```powershell
cd E:\PYPROJECT\agent
python -m pip install -r requirements.txt
```

`requirements.txt` lists the minimum runnable set. For full retrieval quality you additionally want
`FlagEmbedding` (see [Known discrepancies](#known-discrepancies-in-this-repo)).

### 2. Fetch the model weights

```powershell
# bge-m3 embedder weights
python scripts\fetch_bge.py

# bge-reranker-v2-m3 cross-encoder weights
python scripts\fetch_reranker.py

# (or the bulk helper, which handles both plus GGUF orchestration)
python scripts\download_models.py
```

Expected paths (override with `BGE_M3_PATH` / `BGE_RERANKER_PATH`):

```
E:\model\models\bge-m3
E:\model\models\bge-reranker-v2-m3
```

The LLM GGUF files are expected at:

```
E:\model\models\lmstudio-community\gpt-oss-20b-GGUF\gpt-oss-20b-MXFP4.gguf
E:\model\models\lmstudio-community\Qwen2.5-14B-Instruct-1M-GGUF\Qwen2.5-14B-Instruct-1M-Q4_K_M.gguf
```

### 3. Start the two inference servers

```powershell
powershell -ExecutionPolicy Bypass -File deploy\start_servers.ps1
```

This launches `gpt-oss-20b` on `:8080` with `-ngl 99` and `Qwen2.5-14B` on `:8081` with `-ngl 0`,
both with `-c 32768`. The script fails fast with a clear message if the CUDA vendor directory is
missing (see [Troubleshooting](#troubleshooting)).

### 4. Configure the environment

```powershell
Copy-Item .env.example .env
```

Edit `.env` to taste. See [Configuration](#configuration) for the full variable table.

### 5. Start the API

```powershell
# Recommended launcher (port 8010, health-checks the model endpoints first)
powershell -ExecutionPolicy Bypass -File deploy\start_platform.ps1

# Or the lighter API-only launcher (note: defaults to port 8000)
powershell -ExecutionPolicy Bypass -File deploy\start_api.ps1

# Or run uvicorn directly
python -m uvicorn agent_platform.api.app:app --host 127.0.0.1 --port 8010 --timeout-keep-alive 120
```

`--timeout-keep-alive 120` matters: the first knowledge-base query pays a cold BGE load of roughly 46 s,
and the default 5 s keep-alive drops the connection mid-request.

### 6. Start the desktop app

```powershell
powershell -ExecutionPolicy Bypass -File deploy\start_desktop.ps1

# Or from source
python -m agent_platform.desktop
```

### 7. Open the workbench

```
http://127.0.0.1:8010/ui/
```

Interactive API docs live at `http://127.0.0.1:8010/docs`.

### Smoke test

```powershell
python scripts\smoke_test.py
```

---

## Configuration

The platform reads configuration from environment variables. `.env.example` is the canonical starting
point, but it is **not** an accurate mirror of what the code reads — see
[Known discrepancies in this repo](#known-discrepancies-in-this-repo).

### Core and data directory

| Variable | Default | Meaning | Read by |
| --- | --- | --- | --- |
| `AGENT_DATA_DIR` | `<repo>/data` | Root for every runtime artifact (databases, indexes, logs, screenshots). Absolute path; any drive works. | `core/paths.py` |
| `WORKSPACE_ROOT` | repo root | Root of the code workspace that `ws_*` tools operate on. | `core/workspace.py` |
| `ROUTING_CONFIG` | `<repo>/config/routing.yaml` | Path to the model routing table. | `api/app.py`, `core/model_registry.py` |
| `KB_DOCS_DIR` | `<data>/docs` | Where imported knowledge-base source documents are stored. | `knowledge/` |
| `KB_REPORTS_DIR` | `<data>/reports` | Where rendered reports land. | `knowledge/tools_ext.py` |
| `KB_OUTBOX_PATH` | `<data>/outbox` | Outbox file for `notify` deliveries. | `knowledge/tools_ext.py` |
| `KB_CHUNKS_DB` | `<data>/kb/chunks.db` | SQLite chunk index path. | `knowledge/store.py` |

### LLM client

| Variable | Default | Meaning | Read by |
| --- | --- | --- | --- |
| `LLM_BASE_URL` | `http://127.0.0.1:8080/v1` | Primary OpenAI-compatible endpoint. | `core/llm_client.py` |
| `LLM_MODEL` | `gpt-oss-20b` | Primary model name. | `core/llm_client.py` |
| `LLM_REASONING_EFFORT` | `medium` | One of `low` / `medium` / `high`. Invalid values fall back to `medium`. | `core/llm_client.py` |
| `LLM_TEMPERATURE` | `0.2` | Sampling temperature. | `core/llm_client.py` |
| `LLM_MAX_TOKENS` | `2048` | Max completion tokens per call. | `core/llm_client.py` |
| `LLM_API_KEY` | `local` | Placeholder credential for local endpoints. | `core/llm_client.py` |
| `LLM_TIMEOUT` | `180` | Per-request timeout in seconds. | `core/llm_client.py` |
| `LLM_FALLBACK_BASE_URL` | *(empty)* | Optional fallback endpoint. Only used when **both** this and `LLM_FALLBACK_MODEL` are set. | `core/llm_client.py` |
| `LLM_FALLBACK_MODEL` | *(empty)* | Fallback model name. | `core/llm_client.py` |
| `LLM_CONTEXT_WINDOW` | `32768` | Single-request context ceiling. Must match the llama-server `-c` value, otherwise requests are rejected upstream with HTTP 400. | `api/app.py` |
| `AGENT_MAX_ROUNDS` | `20` | Default ReAct round cap for `/chat`. One LLM call equals one round. | `api/app.py` |
| `AGENT_DEBUG_TOKENS` | *(unset)* | Set to enable verbose token accounting in logs. | `core/` |
| `AGENT_FORCE_LEGACY_RETRIEVAL` | *(unset)* | Forces the legacy retrieval path instead of the retrieval policy. | `core/retrieval_policy.py` |

### Knowledge / embedding layer

| Variable | Default | Meaning | Read by |
| --- | --- | --- | --- |
| `BGE_M3_PATH` | *(unset)* | Local `bge-m3` weights directory. | `knowledge/embed.py` |
| `BGE_RERANKER_PATH` | *(unset)* | Local `bge-reranker-v2-m3` weights directory. | `knowledge/reranker.py` |
| `BGE_DEVICES` | `cpu` | Devices for FlagEmbedding. Must stay `cpu` — see [Security notes](#security-notes). | `knowledge/embed.py` |
| `LLAMA_BASE_URL` | `http://127.0.0.1:8080` | llama-server base URL used by the **knowledge** layer for embedding calls. | `knowledge/embed.py` |
| `LLAMA_BASE_URL_2` | `http://127.0.0.1:8081` | Secondary llama-server base URL for the knowledge layer. | `knowledge/llm.py` |
| `LLAMA_EMBED_MODEL` | *(unset)* | Model alias to request from the llama-server embedding endpoint. | `knowledge/embed.py` |
| `BGE_RERANK_SHORTLIST` | `0` | `0` means rerank every candidate. Non-zero truncates the candidate list to the RRF order, which measurably loses top-6 quality (23/36 overlap). | `knowledge/reranker.py` |
| `AGENT_TORCH_THREADS` | `4` | PyTorch intra-op threads while idle. Caps OpenMP spin-wait. | `knowledge/threading_limits.py` |
| `AGENT_TORCH_INFER_THREADS` | `8` | PyTorch threads during inference, restored to the idle value afterwards. | `knowledge/threading_limits.py` |

The embedder and reranker backends are **auto-probed**, not environment-selected. The chains are
hardcoded in the constructors:

```
Embedder:  flagembedding  ->  llama-server  ->  hash (degraded)
Reranker:  flag           ->  cosine similarity (degraded)
```

`.env.example` lists `EMBED_BACKEND` and `RERANK_BACKEND`, but no code reads them — see
[Known discrepancies](#known-discrepancies-in-this-repo).

### Audit

| Variable | Default | Meaning | Read by |
| --- | --- | --- | --- |
| `AUDIT_DSN` | *(empty)* | PostgreSQL DSN. When empty, audit degrades to SQLite (`<data>/audit/audit.db`), then to JSONL (`<data>/audit/audit.jsonl`). | `core/audit.py` |

### Known discrepancies in this repo

These are real and intentional to document rather than hide. The code is the source of truth.

1. **`.env.example` lists 21 variables the code never reads.** `RECALL_N`, `TOP_K`, `RRF_K`,
   `MIN_SCORE_THRESHOLD`, `EMBEDDING_DIM`, `EMBED_BACKEND`, `RERANK_BACKEND`,
   `BUDGET_MAX_ROUNDS`, `BUDGET_MAX_TOKENS`, `BUDGET_MAX_TOOL_CALLS`, `BUDGET_MAX_SECONDS`,
   `MAX_TOKENS_PER_TOOL_RESULT`, `VECTOR_STORE`, `PG_DSN`, `CHUNK_DB_PATH`, `AUDIT_FALLBACK_PATH`,
   `DOCS_DIR`, `ARTIFACTS_DIR`, `LLM_TIMEOUT_S`, `LLM_SECONDARY_BASE_URL`, and `LLM_SECONDARY_MODEL`
   all appear in `.env.example` but are **hardcoded constants in the source** (or renamed). Changing
   them in `.env` has no effect. The real values live in `core/budget.py`,
   `knowledge/retriever.py`, `knowledge/embed.py`, `knowledge/chunker.py`, and
   `knowledge/knowledge` module constants.
2. **`.env.example` omits four variables the code does read:** `LLM_API_KEY` (default `local`),
   `LLM_FALLBACK_BASE_URL`, `LLM_FALLBACK_MODEL`, `WORKSPACE_ROOT`, and `ROUTING_CONFIG`.
   Set them yourself if you need them.
3. **`EMBED_BACKEND` and `RERANK_BACKEND` are inert.** The embedder and reranker backends are
   auto-probed against a hardcoded chain (`flagembedding -> llama -> hash`, `flag -> cosine`). The
   `backend=` constructor argument exists, but nothing wires it to an environment variable.
4. **`requirements.txt` omits `FlagEmbedding`** (plus `numpy`, `torch`, `docling`, `pypdf`,
   `python-docx`). Without `FlagEmbedding`, retrieval silently degrades to hash pseudo-vectors and
   cosine reranking — semantically meaningless. See [Troubleshooting](#troubleshooting).
5. **Two naming schemes for the same endpoints.** `LLM_BASE_URL` (`core/llm_client.py`) and
   `LLAMA_BASE_URL` (`knowledge/embed.py`) both point at llama-server but are read by different layers.
   Setting one does not affect the other.
6. **The API port is inconsistent.** `deploy/start_api.ps1` defaults to `8000`, while
   `deploy/start_platform.ps1`, `deploy/start_desktop.ps1`, and roughly 25 verification scripts assume
   `8010`. `deploy/kind.yaml` maps `8000`. This project documents `8010` throughout.
7. **`BudgetGuard.max_rounds` defaults to `8` in the dataclass, but the API uses `AGENT_MAX_ROUNDS`
   (default `20`).** Two separate knobs. For `/chat`, the API value wins.
8. **`data/models.json` contains a leftover test artifact** — a model entry named
   `verify-custom-48f74a` registered by `scripts/verify_modes.py`. It points at `127.0.0.1:8080` and
   is harmless, but it is noise.

---

## API reference

53 HTTP endpoints. Full request/response shapes are in [`docs/api.md`](docs/api.md).

### Chat and HITL

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/chat` | Start a turn. Returns an SSE stream by default (`start` -> `step`* -> `approval?` -> `result` -> `[DONE]`). |
| `POST` | `/approve` | Resolve a pending approval and resume the same graph thread. Returns the final result, decision trace, and budget snapshot. |

### Workspace (10)

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/workspace/info` | Workspace root, size limits, undo depth, forbidden-path list. |
| `GET` | `/workspace/tree` | Directory tree (directories first, files sorted by name). |
| `GET` | `/workspace/repomap` | Flat file list with per-file line counts, for the codebase treemap. |
| `GET` | `/workspace/file` | Read one text file. |
| `POST` | `/workspace/file` | **Stage** a write. Returns `change_id` + diff. Does not touch disk. |
| `GET` | `/workspace/search` | grep-style full-text search over the workspace. |
| `POST` | `/workspace/apply` | Apply a staged change after confirmation. |
| `DELETE` | `/workspace/change/{change_id}` | Discard a staged change. |
| `POST` | `/workspace/undo` | Undo the most recent write (restores previous content). |
| `GET` | `/workspace/history` | Modification history for a file (or all files). |

### Models (9)

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/models` | Merged registry (static `routing.yaml` + dynamic `models.json`) with live endpoint probes. |
| `POST` | `/models` | Register a custom model. |
| `PUT` | `/models/{name}` | Update a custom model. |
| `DELETE` | `/models/{name}` | Remove a custom model. |
| `GET` | `/models/lifecycle` | Lifecycle state of every known model. |
| `POST` | `/models/{alias}/start` | Start the llama-server for an alias. |
| `POST` | `/models/{alias}/stop` | Stop it. |
| `POST` | `/models/start-all` | Start every registered model. |
| `POST` | `/models/stop-all` | Stop every registered model. |

### Knowledge base (7)

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/kb/documents` | List indexed documents with metadata and chunk counts. |
| `POST` | `/kb/documents` | Add a document from inline text. |
| `POST` | `/kb/documents/upload` | Add a document from an uploaded file. |
| `DELETE` | `/kb/documents/{doc_id}` | Remove a document and its chunks. |
| `POST` | `/kb/documents/{doc_id}/reindex` | Re-chunk and re-embed one document. |
| `POST` | `/kb/reindex` | Rebuild the whole index. |
| `GET` | `/kb/search` | Search the knowledge base. |

### Custom tools (6)

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/custom-tools` | List registered custom tools. |
| `POST` | `/custom-tools` | Create one. `409` on name collision with a built-in. |
| `PUT` | `/custom-tools/{name}` | Update one. |
| `DELETE` | `/custom-tools/{name}` | Remove one. |
| `POST` | `/custom-tools/{name}/toggle` | Enable or disable one. |
| `POST` | `/custom-tools/{name}/test` | Execute one against sample arguments. |

### Sessions (5)

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/sessions` | List sessions. |
| `GET` | `/sessions/{thread_id}` | One session's metadata. |
| `GET` | `/sessions/{thread_id}/messages` | Full message history. |
| `DELETE` | `/sessions/{thread_id}` | Delete a session and its messages. |
| `POST` | `/sessions/{thread_id}/summarize` | Generate a summary of a session. |

### Tasks (5)

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/tasks/templates` | Available task templates. |
| `GET` | `/tasks` | List tasks. |
| `GET` | `/tasks/{task_id}` | One task. |
| `GET` | `/tasks/{task_id}/export` | Export a task result. |
| `POST` | `/tasks` | Create a task. |

### Platform, tools, and diagnostics

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/platform/status` | Platform-wide status (data dir, models, tools). |
| `POST` | `/platform/launch` | Launch a platform-managed component. |
| `GET` | `/tools` | Tool catalog with permissions and HITL flags. |
| `GET` | `/tools/{tool_name}` | One tool's full spec. |
| `GET` | `/ping` | **Fast liveness probe.** Touches no model, no DB, no index. |
| `GET` | `/health` | Deep health check: models, tools, knowledge base, data dir. |
| `GET` | `/metrics` | Aggregate counters. |
| `GET` | `/audit` | Audit log entries. |
| `GET` | `/` | Redirects to `/ui/`. |

### The HITL handshake

A write request comes back as `status: "pending_approval"` with an approval card. The client
resumes by posting to `/approve` with the **same `thread_id`**.

**Step 1 — `POST /chat`**

```json
{
  "message": "在 lz/demo.py 写一个张量自动微分的示例",
  "thread_id": "th-a1b2c3d4e5f6",
  "mode": "agent",
  "max_rounds": 20,
  "stream": false
}
```

**Step 2 — response with a pending approval**

```json
{
  "status": "pending_approval",
  "answer": "文件尚未写入。\n\n以下写操作已发起，但正挂在人工审批上、尚未执行：ws_write_file。\n\n请在下方审批卡片上点击「批准」后才会真正落盘；在对话里回复「确认执行」不会自动放行。",
  "thread_id": "th-a1b2c3d4e5f6",
  "approvals": [
    {
      "tool_call_id": "call_9f3a",
      "tool_name": "ws_write_file",
      "args": { "path": "lz/demo.py", "content": "..." },
      "permission": "WRITE_DATA",
      "impact": "写入 lz/demo.py",
      "reversible": true,
      "diff": { "added": 42, "removed": 0 }
    }
  ],
  "tokens_used": 1180,
  "tool_calls_used": 0,
  "rounds": 2,
  "stop_reason": "pending_approval"
}
```

**Step 3 — `POST /approve`**

```json
{
  "thread_id": "th-a1b2c3d4e5f6",
  "tool_call_id": "call_9f3a",
  "approved": true,
  "actor": "operator",
  "reason": "确认",
  "session_grant": false,
  "summarize": false
}
```

**Step 4 — response**

```json
{
  "status": "done",
  "answer": "已写入 lz/demo.py（42 行新增）。如需回滚，请调用 POST /workspace/undo。",
  "thread_id": "th-a1b2c3d4e5f6",
  "approvals": [],
  "tokens_used": 1180,
  "tool_calls_used": 1,
  "rounds": 2,
  "stop_reason": "done",
  "fast_path": true,
  "trace": [ { "seq": 1, "kind": "llm", "ok": true, "duration_ms": 812 }, "..." ],
  "budget": {
    "rounds": 2, "max_rounds": 20,
    "tokens_used": 1180, "max_tokens": 60000,
    "tool_calls_used": 1, "max_tool_calls": 24,
    "context_window": 32768
  }
}
```

Notes:

- `tool_calls_used` stays at `0` in step 2 — the tool has not executed yet. Approval waits do not
  consume the tool-call budget.
- `summarize: false` selects the **fast path**: the write tool's own result is already a complete,
  self-describing JSON payload, so the platform skips the second LLM call that would otherwise
  rewrite it as prose. That took the approve round trip from 1.06-2.33 s down to 0.08 s. Set
  `summarize: true` to force the old behaviour.
- `/approve` reuses the **same graph instance** that produced the approval, so `max_rounds`, the model,
  and the mode all survive the round trip.

---

## Tool catalog

13 built-in tools. `HITL` marks tools that suspend the graph for human approval.

The registry registers `kb_search` and `kb_read` from the knowledge layer
(`knowledge/tools.py`), the five domain tools (`tp_lookup`, `spec_extract`, `case_lint`,
`report_render`, `notify`) from `knowledge/tools_ext.py`, and six platform tools (`sql_query` plus
the five `ws_*` workspace tools) from `tools/__init__.py`. `tools/__init__.py` also carries
placeholder specifications for `kb_search` and `kb_read`, but those are skipped when the knowledge
layer has already registered the same names.

| Tool | Permission | HITL | Description |
| --- | --- | --- | --- |
| `kb_search` | `READ_ONLY` | No | Hybrid search (BM25 + dense -> RRF -> rerank) over the internal knowledge base; returns candidates with `chunk_id`. |
| `kb_read` | `READ_ONLY` | No | Read one full chunk plus its parent context by `chunk_id`. |
| `sql_query` | `READ_ONLY` | No | Execute a read-only `SELECT`/`WITH` query against the semantic warehouse. DDL/DML and multi-statements are rejected. |
| `tp_lookup` | `READ_ONLY` | No | Look up test-program item definitions and limits (lower/upper/typ/unit/dft_method). |
| `ws_list_files` | `READ_ONLY` | No | List directories and files in the code workspace as a tree. |
| `ws_read_file` | `READ_ONLY` | No | Read a workspace text file with line numbers. Binary files and files over 512 KB are rejected explicitly. |
| `ws_search_code` | `READ_ONLY` | No | grep-style full-text search over workspace code; returns matching files with line numbers and hit content. |
| `spec_extract` | `COMPUTE` | No | Extract structured parameters (code/min/typ/max/unit/condition) from a device datasheet, with source page and quoted text; missing parameters are listed under `gaps`. |
| `case_lint` | `COMPUTE` | No | Static quality check of a test-case set against rules R01-R10 (limit traceability, units, bound sanity, step reproducibility, PVT conditions, sample size, failure-mode coverage, duplication, binary pass/fail, TP consistency). |
| `report_render` | `WRITE_DATA` | **Yes** | Render a structured report (daily / weekly / customer / spec); returns a report ID, storage URI, and Markdown preview. |
| `ws_write_file` | `WRITE_DATA` | **Yes** | Write a whole file (replaces all content). Staged, then applied only after approval. |
| `ws_edit_file` | `WRITE_DATA` | **Yes** | Replace an exact `old_text` fragment with `new_text`, leaving every other byte unchanged. Staged, then applied only after approval. |
| `notify` | `EXTERNAL_SIDE_EFFECT` | **Yes** | Send an alert or notification to recipients over WeCom, Feishu, or email. |

### Permission levels

| Level | Meaning |
| --- | --- |
| `READ_ONLY` | Reads data. Runs automatically, is audited. |
| `COMPUTE` | Pure computation or model inference. Runs automatically, cost is metered. |
| `WRITE_DATA` | Writes files or databases. Requires idempotency, a rollback path, and audit. |
| `EXTERNAL_SIDE_EFFECT` | Sends messages, creates tickets. Requires HITL. |
| `ADMIN` | Changes configuration or permissions. Administrators only. Requires HITL. |

`HITL_REQUIRED = (WRITE_DATA, EXTERNAL_SIDE_EFFECT, ADMIN)`. A tool's `hitl_required` flag defaults
to whether its permission is in that set, and can be overridden explicitly.

### Custom tools

Users can register their own tools at runtime. Two kinds:

- **`http`** — an HTTP call to a user-supplied URL, with templated arguments
  (`{{ args.foo }}`), a method, headers, and a JSON body template. Default timeout 15 s.
- **`script`** — a local Python script executed as a subprocess. Output is capped at 1,000,000 bytes.
  Default timeout 15 s, hard ceiling 120 s.

Definitions persist to `data/custom_tools.json` and take effect **immediately** in the running
process: the manager unregisters then re-registers the tool it manages, so no restart is needed.
Built-in tool names are protected — a custom tool cannot shadow one, and the API returns `409` with
code `NAME_CONFLICT` on collision. Custom tools with write permissions are forced through HITL
regardless of what the definition declares.

**Security posture.** Custom script tools execute arbitrary local code with the privileges of the
API process, and custom HTTP tools can reach any host the machine can reach. The platform does not
restrict `base_url` to a whitelist — that is a deliberate choice for a local-only deployment, and
it means the registry should only be populated in a trusted environment. The `api_key` field of a
registered model is never echoed back in plaintext by `GET /models`; only whether one is configured.

---

## Interaction modes

Three modes, mirroring the Cursor-style Agent / Ask / Plan split. Defined in `core/modes.py`.

| Mode | Tools | Rounds | Behaviour |
| --- | --- | --- | --- |
| `agent` | Any tool | `AGENT_MAX_ROUNDS` (default 20) | Full ReAct loop. Reads and writes. Writes go through HITL. |
| `ask` | `READ_ONLY` and `COMPUTE` only | `ASK_MAX_TOOL_ROUNDS = 2` | Read-only Q&A. After two tool rounds the model is told to answer from what it has. |
| `plan` | **None** | n/a | Emits a structured JSON plan and stops. The plan node is not connected to the tools node, so a tool call is structurally impossible. |

**Ask mode** whitelists tools by *permission class*, not by hard-coded name, so newly added read-only
tools join automatically. One "round" means one agent-to-tools round trip (a batch of tool calls),
not one tool call — a single round may invoke several different tools.

**Plan mode** produces a JSON object with `goal`, `steps` (1-8 entries, each with `title`, `detail`,
`tools`, `risk`), `risks`, and `questions`. The frontend renders it as a plan card. Steps that write
data must be marked `risk` of 中 or 高.

**Retrieval gating.** `core/retrieval_policy.py` decides per turn whether knowledge-base retrieval
is needed. When retrieval is not needed, the model does not even receive the retrieval tool schema,
so general questions consume no tool rounds. The decision, its rule name, and a human-readable reason
travel to the client as `retrieval_needed` / `retrieval_rule` / `retrieval_reason` /
`retrieval_policy_version`.

---

## Knowledge base and RAG

### Chunking

Parent-child chunking, implemented in `knowledge/chunker.py`:

| Level | Size | Purpose |
| --- | --- | --- |
| Child | ~300 tokens (overlap 80, minimum 50) | The retrieval unit. Small enough to rank precisely. |
| Parent | ~1200 tokens (minimum 200) | The refill unit. A hit child pulls its parent in for full context. |

Splitting prefers markdown heading levels and PDF paragraph boundaries. The heading path is stored in
`metadata.title_path`.

### Embedding and reranking

| Component | Model | Fallback chain |
| --- | --- | --- |
| Embedder | `bge-m3`, `EMBEDDING_DIM = 1024` | `flagembedding` -> `llama` (llama-server `/embedding` or `/v1/embeddings`) -> `hash` |
| Reranker | `bge-reranker-v2-m3` | `flag` -> `cosine` (embedding similarity) |

The `hash` fallback produces deterministic pseudo-vectors: the same text always yields the same
vector, and overlapping vocabulary produces some similarity signal. It is **not** semantic similarity
and must not be used to judge retrieval quality. The embedder logs a warning whenever it degrades.

Both models run on **CPU**, always. `bge-m3` is a pure encoder and retrieval is low-frequency, while
the single 16 GB GPU is reserved for `gpt-oss-20b`. Putting BGE on the GPU produced
`CUDA error: an illegal memory access` and took down both inference servers (reproduced twice).

### Retrieval constants

| Constant | Value | Where |
| --- | --- | --- |
| `RRF_K` | `60` | RRF fusion constant |
| `RECALL_N` | `50` | Candidates recalled per branch |
| `FINAL_TOP_K` | `6` | Candidates kept after reranking |
| `RETRIEVAL_TOKEN_BUDGET` | `3000` | Context assembly ceiling |
| `MIN_SCORE_THRESHOLD` | `0.012` | Abstain threshold on the RRF score |

The abstain threshold is on the **RRF score**, not on reranker logits. RRF scores have a stable
magnitude (`sum 1/(60+rank)`): a dual-branch top-1 hit is about `0.033`, a single-branch hit about
`0.016`. `0.012` corresponds to a weak single-branch top-3 hit; below it, the platform explicitly
answers "not found in the knowledge base" rather than confabulating.

### The 10-step chain

1. **Query understanding and rewrite** — extract entity / model / time / product-line slots, then
   produce 2-3 rewritten queries. A rule channel always works; a small-model channel supplements it.
2. **Pre-retrieval ACL and confidentiality filter** — pushed down into the store, so unauthorised
   chunks never enter the candidate pool.
3. **Dual recall** — BM25 and dense vector search run independently.
4. **RRF fusion** — `score = sum_i 1/(60 + rank_i)`.
5. **Dedup and top-50** — by `chunk_id` plus content hash.
6. **Cross-encoder rerank** — `bge-reranker-v2-m3` reorders to top-6.
7. **Context assembly** — packed up to 3000 tokens; over-budget chunks are dropped.
8. **Answer generation** — with inline citations.
9. **Citation validation** — chunk existence is checked; invalid citations are collected separately
   in `invalid_citations`.
10. **Abstain threshold** — if the top RRF score is below `0.012`, refuse rather than guess.

### Degradation behaviour

| Missing dependency | Effect |
| --- | --- |
| `FlagEmbedding` | Embedder falls back to `hash`, reranker to `cosine`. Retrieval still runs but semantic quality is gone. A warning is logged. |
| Model weights not on disk | Same as above — the package can be installed while the weights are absent. |
| `psycopg` | Audit falls back to SQLite, then JSONL. |
| `langgraph-checkpoint-postgres` | Checkpointer falls back to `MemorySaver` (in-memory). Sessions still persist via `data/sessions.db`. |
| `pyyaml` | Routing table falls back to the in-code `DEFAULT_ROUTES` constant. |
| `sqlglot` | `sql_query` degrades to a conservative regex-based read-only check. |
| `pywebview` | The desktop shell falls back to opening the default browser. |

### Importing documents

- **From the UI**: the knowledge-base panel has an import modal with an "Upload file" tab and a
  "Paste text" tab.
- **From the API**: `POST /kb/documents` (inline text) or `POST /kb/documents/upload` (multipart).
- **Rebuilding**: `POST /kb/documents/{doc_id}/reindex` for one document, `POST /kb/reindex` for the
  whole corpus.

Source documents live under `<data>/docs`; the chunk index is `<data>/kb/chunks.db`.

---

## HITL and approvals

### The two-stage split

```
tools node                        hitl node                     tools node
──────────                        ─────────                     ──────────
1. detect tool needs approval
2. build PendingApproval
3. raise interrupt()  ─────────▶  4. resolve the decision
                                    (approve / reject /
                                     session grant)
                                ─────────────────────────▶  5. execute the tool
                                                                6. truncate result
                                                                7. audit
```

LangGraph's `interrupt()` re-runs the interrupted node from the top on resume. If "approve and
execute" were one node, resuming would execute the write twice. The split makes the resume pass
idempotent: `hitl` only reads the decision, `tools` performs the write.

### The approval card

A pending approval carries `tool_call_id`, `tool_name`, `args`, `permission`, `impact`,
`reversible`, and `diff` (added/removed line counts, and a full diff for workspace writes). The
frontend renders it as a card with Approve / Reject buttons.

### Session grants

`session_grant: true` on an approval grants that tool permission for the rest of the session.
Grants are keyed by `session_id` and expire after `SESSION_GRANT_TTL_SECONDS`. The decision is
recorded in the audit log as a session grant, not a per-call approval.

### Approval fast path

When the approved action is itself terminal — a write tool whose result is already a complete,
self-describing JSON payload such as
`{"status":"applied","path":...,"change_id":...,"undo_id":...,"size":N,"added":N,"removed":N}` —
the platform skips the second LLM call and uses the tool result directly as the answer.

The gate is **intent-based, not heuristic**: the fast path only fires when exactly one approval is
pending *and* the agent's message contained exactly that one tool call. Multi-step plans (write two
files, then read one back to verify) never take the shortcut. The tool whitelist is an explicit
constant (`_WRITE_TOOLS_BY_PATH` and the fast-path tool list), never a guess based on whether the
result "looks like JSON".

Measured: `/approve` went from 1.06-2.33 s to 0.08 s.

### Audit trail

Every approval decision is written to the audit log with the tool name, arguments, decision,
approver (`actor`), reason, and whether it was a session grant. The audit log is append-only:
there is no update or delete entry point. Storage is PostgreSQL when `AUDIT_DSN` is set, otherwise
SQLite, otherwise JSONL.

### Tools that require approval

`report_render`, `ws_write_file`, `ws_edit_file`, `notify` — plus any custom tool whose permission
is `WRITE_DATA`, `EXTERNAL_SIDE_EFFECT`, or `ADMIN`.

---

## Desktop app

A `pywebview` shell around the same SPA, in `agent_platform/desktop/`.

### Build

```powershell
powershell -ExecutionPolicy Bypass -File deploy\build_desktop.ps1

# Output:
dist\desktop\AgentPlatform\AgentPlatform.exe
```

### Why onedir, not onefile

The previous build used `--onefile`. Every launch extracted the entire runtime into
`%TEMP%\_MEIxxxxxx` — roughly **7.5 GB** — which meant:

- **30 to 90 seconds** to start.
- Clicking the desktop icon several times spawned several concurrent instances, each extracting its
  own copy, competing for disk I/O and space. When extraction failed the bootloader exited
  **silently**, with no error shown.

`--onedir` writes the runtime to disk at build time (`dist\desktop\AgentPlatform\`) and launching is
just an ordinary exe load — measured at **823 ms** in this session, and 1-2 s in the build script's
own estimate. There is no temporary extraction directory and no `_MEI*` residue.

`--runtime-tmpdir` is deliberately **not** used: it only applies to onefile, PyInstaller warns about
it in onedir mode, and it was the original source of the "C: disk full" problem.

### Single instance

A Win32 named mutex `Local\AgentPlatformDesktopSingleton` guards the app. On a second launch,
`CreateMutexW` returns `ERROR_ALREADY_EXISTS` (183); the new process finds the existing window by
title, restores it (`ShowWindow(SW_RESTORE)`) and brings it to the front (`SetForegroundWindow`),
prints a one-line notice, and exits cleanly.

The `Local\` namespace is used so different users on a terminal server do not block each other.

The mutex handle is **never closed**. Closing it would release the mutex immediately and make the
single-instance protection useless — the handle must die with the process.

### Silent mode

The build passes `--window`, so double-clicking the exe no longer spawns a console window alongside
the app. The consequence is that `sys.stdout` / `sys.stderr` are `None` in a windowed build, so
`desktop/entry_point.py` redirects them to a log file **before** anything else runs — otherwise the
first `print()` raises `AttributeError` and the app dies.

Logs: `%LOCALAPPDATA%\AgentPlatform\logs\desktop.log`.

For a console-visible run, use the launcher script instead of the packaged exe.

---

## Observability

### Decision trace

Every turn produces a trace: an ordered list of entries, each tagged with a `kind`:

| `kind` | Meaning |
| --- | --- |
| `llm` | One model call. Carries `duration_ms`, `started_at`, `ended_at`, token count, and the reasoning summary. |
| `tool` | One tool call. Carries arguments, result, and error. |
| `approval` | An approval wait. Carries permission, impact, reversibility. |

**Tool status is three-state**, not boolean:

| `ok` | `status` | Rendered as |
| --- | --- | --- |
| `true` | `success` | Green check |
| `false` | `error` | Red cross |
| `null` | `pending_approval` | Amber "awaiting approval" |

This matters: the trace entry for a tool call is created when the agent node emits the call, which
happens *before* the tools node discovers the call needs approval. The old code computed `ok=True`
at creation time, so the UI showed a green "success" for a file that was never written. Now the
approval event rewrites the matching entry in place to `ok=null` / `pending_approval`.

Approval waits do not consume the tool-call budget.

### Live timeline

A waterfall view of the same data: LLM calls and tool executions as time bars, plus approval waits
as a third step type. The LLM entries matter because model thinking is usually the largest share of
wall-clock time — in one measured multi-tool turn the LLM took 11.9 s while all tools together took
9 ms. Without LLM entries the waterfall is badly skewed.

### Budget snapshot

`rounds`, `max_rounds`, `tokens_used`, `max_tokens`, `tool_calls_used`, `max_tool_calls`,
`context_window`, and `system_prompt_chars` (a real measurement, not an estimate). `rounds` is read
from graph state, where the agent node writes the true count after each LLM call; the trace-based
fallback counts only `kind == "llm"` entries, so a nine-entry trace never displays as "9/8".

### SSE events

| Event | Payload highlights |
| --- | --- |
| `start` | `thread_id`, `ts`, `message`, `mode`, `model` |
| `step` | `node`, `tokens_used`, `tool_calls_used`, `ts`, optional `llm_duration_ms` / `llm_started_at` / `llm_ended_at`, optional `retrieval_*`, optional `tool_calls` / `tool_results` / `reasoning_summary` |
| `approval` | `thread_id`, `approvals[]` (each with `tool_call_id`, `tool_name`, `args`, `permission`, `impact`, `reversible`, `diff`) |
| `plan` | `thread_id`, `plan` (plan mode only) |
| `result` | `status`, `answer`, `approvals`, `tokens_used`, `tool_calls_used`, `rounds`, `stop_reason`, `trace`, `budget`, optional `plan`, optional `retrieval_*` |
| `[DONE]` | Terminal sentinel |

### Metrics and audit

- `GET /metrics` — counters such as `chat_total`, `approve_total`, `total_duration_ms`, plus token
  and tool-call aggregates.
- `GET /audit` — recent audit entries.
- `GET /health` — deep status across models, tools, the knowledge base, and the data directory.
- `GET /ping` — liveness only. It touches no model, no database, and no index, and returns in
  milliseconds. The desktop shell probes `/ping` with a 3 s timeout; probing `/health` instead
  caused spurious "API unreachable" exits when a model endpoint was busy.

---

## Evaluation

Four metric tiers, implemented in `agent_platform/eval/`.

| Tier | Scope | Examples |
| --- | --- | --- |
| **L1** | System | TTFT P50/P95, end-to-end latency P50/P95, tool calls per request, tool success rate, cache hit rate, token cost |
| **L2** | Retrieval | Recall@5 / @10, Precision@5, MRR, NDCG@10, ground-truth support rate, traceable rate, empty-recall rate, ACL filter correctness |
| **L3** | Generation | SQL executable rate, SQL semantic correctness, execution accuracy, tolerance accuracy, multi-hop accuracy, time-semantics accuracy; case field completeness, format validity, limit traceability, unit consistency, limit sanity, duplication; root-cause Top1/Top3, wrong-attribution rate, correct-abstain rate, evidence rate; numeric / citation / logical hallucination rates, unsupported-claim rate |
| **L4** | Business | Analysis time reduction, case workload reduction, expert rewrite rate, adoption rate, first-pass rate, self-serve rate |

Every metric has a `(baseline, target)` pair in `METRIC_TARGETS`. The targets come from the design
documents in `docs/02`; they are not invented locally.

### LLM-as-Judge

`eval/judge.py` scores free-text answers against a rubric (`RUBRIC`), producing `JudgeResult`
objects. A `CalibrationStore` records human-verified judgements and `cohens_kappa` measures judge
agreement with those references. Failed cases are attributed to a layer with `attribute_failure`,
closing the loop from symptom to cause.

### Dataset and runner

The dataset is `agent_platform/eval/datasets/v1.jsonl` — 30 cases, each with `id`, `category`,
`query`, `gold_answer`, `gold_chunks`, `gold_sql`, `tags`, and `unanswerable`. The runner applies
`GATE_RULES` so a regression that breaks a gate fails the run.

```powershell
python scripts\run_eval.py
python scripts\run_regression.py
```

---

## Repository layout

```
agent_platform/
├── api/
│   ├── app.py                     FastAPI app: 53 endpoints, SSE /chat, HITL /approve
│   └── static/                    Vanilla-JS SPA (no build step, no framework)
│       ├── index.html             Shell markup and view containers
│       ├── app.js                 ~4000 lines: chat, SSE client, views, workbench
│       ├── app.css                Full theme (light and dark in one stylesheet)
│       ├── approval.js            Unified approval entry point, broadcasts hitl-resolved
│       ├── observability.js       Decision trace, Live Timeline, metrics, treemaps
│       ├── timeline.js            Waterfall timeline rendering
│       ├── trace_view.js          Decision-trace card rendering
│       ├── workspace.js           Code workspace explorer and editor
│       ├── workbench.js           Task workbench
│       ├── kb_admin.js            Knowledge-base administration
│       └── custom_tools.js        Custom tool management UI
├── core/
│   ├── tool_registry.py           ToolSpec, Permission, JSON-Schema validation, registry
│   ├── llm_client.py              OpenAI-compatible client, reasoning effort, fallbacks
│   ├── model_registry.py          Merges routing.yaml + models.json, capability tagging
│   ├── model_lifecycle.py         Start/stop/health for llama-server processes
│   ├── budget.py                  BudgetGuard: rounds, tokens, tool calls, time, context
│   ├── audit.py                   Append-only audit with PG -> SQLite -> JSONL fallback
│   ├── session_store.py           SQLite session and message persistence
│   ├── workspace.py               Staged writes, path safety, atomic apply, undo
│   ├── paths.py                   Single data-directory resolver
│   ├── modes.py                   Agent / Ask / Plan semantics and limits
│   ├── retrieval_policy.py        Per-turn decision on whether retrieval is needed
│   ├── custom_tools.py            Runtime http/script tool registry and persistence
│   ├── script_runner.py            Subprocess execution for script tools
│   ├── citations.py               Citation parsing and validation
│   └── event_loop.py              Async event-loop helper
├── orchestration/
│   ├── graph.py                   LangGraph: 4 nodes, routes, AgentState
│   ├── hitl.py                    HITLGate: approvals, session grants, interrupts
│   └── prompts/                   System prompt (system.md) and prompt builders
├── knowledge/
│   ├── chunker.py                 Parent-child chunking
│   ├── embed.py                   Embedder with backend fallback chain
│   ├── reranker.py                Cross-encoder reranker with fallback
│   ├── retriever.py               10-step retrieval pipeline
│   ├── store.py                   SQLite and pgvector chunk stores
│   ├── admin.py                   Knowledge-base administration
│   ├── ingest.py                  Document ingestion pipeline
│   ├── tools.py                   kb_search / kb_read tool specs and handlers
│   ├── tools_ext.py               tp_lookup / spec_extract / case_lint / report_render / notify
│   ├── threading_limits.py         PyTorch thread capping (idle vs inference)
│   ├── schema.sql                 Chunk store schema
│   └── README.md                  Knowledge-layer notes (Chinese, preserved)
├── tools/__init__.py              register_all() and the 12 in-module tool specs
├── eval/
│   ├── metrics.py                 L1-L4 metric implementations and targets
│   ├── judge.py                   LLM-as-Judge, rubric, calibration
│   ├── runner.py                  Evaluation and regression runner, gate rules
│   └── datasets/v1.jsonl          30-case evaluation dataset
└── desktop/
    ├── __main__.py                Entry point
    ├── launcher.py                API and model bootstrapping for the desktop build
    ├── window.py                  pywebview window
    ├── single_instance.py          Win32 mutex and window activation
    ├── console_redirect.py        stdout/stderr redirection for windowed builds
    └── entry_point.py             PyInstaller entry point

config/routing.yaml                Static model registry and capability-aware routes
deploy/
├── start_servers.ps1              Launch both llama-server processes
├── start_api.ps1                  Launch the API only
├── start_platform.ps1             Launch models + API + desktop
├── start_desktop.ps1              Launch the desktop shell
├── build_desktop.ps1              PyInstaller onedir build
├── create_shortcut.py             Desktop shortcut helper
├── enable_k8s.ps1, k8s.yaml, kind.yaml   Optional container deployment

scripts/                            67 verification, diagnostic, and utility scripts
data/                               Runtime data (relocatable via AGENT_DATA_DIR)
docs/                               Chinese design documents plus the English docs listed below
```

---

## Development

### Running the API with reload

```powershell
python -m uvicorn agent_platform.api.app:app --host 127.0.0.1 --port 8010 --reload
```

### Tests and verification

```powershell
python scripts\smoke_test.py          # Fast smoke test
python scripts\run_all_tests.py       # Full verification sweep
python scripts\run_eval.py            # Evaluation
python scripts\run_regression.py      # Regression with gate rules
```

The `scripts/` directory holds 61 files (60 Python, 1 JavaScript), grouped by purpose:

| Group | Examples |
| --- | --- |
| Feature verification | `verify_e2e.py`, `verify_rag.py`, `verify_tools.py`, `verify_workspace.py`, `verify_modes.py`, `verify_kb_admin.py`, `verify_model_routing.py`, `verify_model_lifecycle.py`, `verify_observability.py`, `verify_trace_view.py`, `verify_desktop.py`, `verify_platform_launch.py`, `verify_web_ui.py`, `verify_retrieval_policy.py` |
| Health and performance | `verify_api_responsiveness.py`, `verify_cold_health.py`, `verify_health_during_cold.py`, `verify_kb_search_latency.py`, `diag_health_cpu.py`, `diag_health_breakdown.py` |
| Diagnostics | `diag_warmup_lock.py`, `dump_api_stacks.py`, `probe_reranker.py`, `probe_search.py`, `sessions_db_dump.py`, `modes_behavior_evidence.py`, `selfhost_verify.py` |
| Model fetching | `fetch_bge.py`, `fetch_bge_ms.py`, `fetch_bge_shards.py`, `fetch_reranker.py`, `download_models.py` |
| UI capture | `ui_shots.py`, `ui_shots_task.py`, `ui_newfeatures_shots.py`, `ui_overlap_check.py`, `ui_input_check.py`, `ui_style_check.py`, `ui_ws_check.py`, `tl_shots.py`, `capture_observability.py`, `capture_delivery.py`, `capture_desktop_window.py`, `cdp_shot.py` |
| Maintenance | `migrate_data_dir.py`, `build_index.py`, `clean_c_drive.py`, `restart_api.py`, `kill_stale_tests.py`, `js_syntax_check.py`, `desktop_title.py` |

Script environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `MODEL_ROOT` | `E:\model\models` | Where the model-fetch scripts download weights. |
| `AGENT_API_BASE` | `http://127.0.0.1:8010` | API base URL for verification scripts. |
| `AGENT_UI_BASE` | `http://127.0.0.1:8010` | UI base URL for capture scripts. |
| `PIP_CACHE_DIR` | *(pip default)* | pip cache location. |
| `HF_HOME` | *(HF default)* | HuggingFace cache location. |

### Building the desktop bundle

```powershell
powershell -ExecutionPolicy Bypass -File deploy\build_desktop.ps1
powershell -ExecutionPolicy Bypass -File deploy\build_desktop.ps1 -SkipInstall   # reuse current env
```

Output: `dist\desktop\AgentPlatform\AgentPlatform.exe`.

### Adding a tool

1. Write a `ToolSpec` factory in `agent_platform/tools/__init__.py` (or
   `agent_platform/knowledge/tools_ext.py` for domain tools). Set `name`, `description`,
   `input_schema`, `permission`, `timeout_ms`, and `metadata`.
2. Implement the handler as `(args: dict, ctx: dict) -> Any`.
3. Register it in `register_all()`'s `pairs` list, or expose a `register_knowledge_ext_tools`
   function if it lives in the knowledge layer.
4. Verify with `GET /tools/{name}` and `python scripts\verify_tools.py`.

Choosing the permission correctly is what determines whether HITL fires: `WRITE_DATA`,
`EXTERNAL_SIDE_EFFECT`, and `ADMIN` suspend the graph.

### Adding an endpoint

Endpoints live in `agent_platform/api/app.py` as decorated functions inside the `create_app`
factory. Use `response_model=None` for handlers that return a `JSONResponse` directly.

### Adding a model alias

Add an entry under `models:` in `config/routing.yaml`, including `capabilities`. If a route should
use it, add it to that route's `fallback` list. If the route declares `requires:`, the router will
strip any model lacking those capability tags — and raise `RoutingCapabilityError` if nothing is
left, rather than silently downgrading.

---

## Troubleshooting

### `llama-server` exits immediately with `0xC0000135`

**Symptom.** The process starts and dies with no output. Windows exit code `0xC0000135` is
`STATUS_DLL_NOT_FOUND`.

**Cause.** `cublas64_12.dll` / `cudart64_12.dll` are not on `PATH`. LM Studio ships them in a
separate vendor directory that is not added automatically.

**Fix.** Add both directories to `PATH` before launching:

```
C:\Users\admin\.lmstudio\extensions\backends\llama.cpp-win-x86_64-nvidia-cuda12-avx2-2.65.1
C:\Users\admin\.lmstudio\extensions\backends\vendor\win-llama-cuda12-vendor-v2
```

`deploy/start_servers.ps1` does this for you and fails fast with
`找不到 CUDA vendor DLL 目录` if the directory is missing. The PyInstaller build needs the same
treatment — see the DLL fix in the [CHANGELOG](CHANGELOG.md).

### The API process burns ~32% CPU while completely idle

**Symptom.** With no requests, the API process holds roughly 32% of 16 logical cores. Inside the
process, 106 threads and 20 CPU-seconds accumulate over a 20 s window.

**Cause.** Not request handling. PyTorch opens its intra-op thread pool to every logical core by
default, and the resident `bge-m3` / `bge-reranker-v2-m3` OpenMP pools **spin-wait** after the model
loads. `nice` does not help: it lowers scheduling weight but does not stop the spin.

**Fix.** `knowledge/threading_limits.py` caps threads to 4 while idle and raises them to 8 during
inference, restoring 4 afterwards. Override with `AGENT_TORCH_THREADS` and
`AGENT_TORCH_INFER_THREADS`. Idle consumption drops from 32% to below 0.03%.

The inference window is worth keeping: a cross-encoder is pure CPU matrix work, and 4 threads is
nearly 2x slower than 8 (measured `n=20, max_length=256`: 19.1 s at 4 threads vs 10.8 s at 8).
16 threads is slower still — oversubscription eats the gain.

### `CUDA error: an illegal memory access` when both models are loaded

**Symptom.** Both inference servers crash at once.

**Cause.** `bge-m3` was placed on the GPU. Together they need 22.3 GB on a 16 GB card.

**Fix.** Keep BGE on CPU. `BGE_DEVICES=cpu` is the default and `resolve_flag_devices` forces it.
The GPU is for `gpt-oss-20b` alone.

### The first knowledge-base query times out

**Symptom.** `[TIMEOUT] 工具 'kb_search' 执行超时` on a cold index.

**Cause.** The first call pays the BGE weight load — about 46 seconds. The knowledge tool timeout
was raised from 20 s to 60 s precisely because 20 s killed a healthy load.

**Fix.** Nothing to change in the config. If the connection still drops, check that the API was
started with `--timeout-keep-alive 120`; the default 5 s keep-alive reclaims the socket while the
client is still waiting, producing `ReadError: [WinError 10054]`.

### `MAX_ROUNDS` fires after only a few steps

**Symptom.** "Budget exhausted" after what looks like four rounds, with `max_rounds=8`.

**Cause.** This was a double-count bug. Both the agent node and the tools node called
`BudgetGuard.check()`, which tests `rounds >= max_rounds`. But rounds are only charged by the agent
node, so one agent-to-tools round trip triggered the check twice — 8 rounds of budget gave 4 usable
rounds.

**Fix.** `check_no_rounds()` now exists for the tools node: it checks tool calls, elapsed time, and
the context window, but not rounds. Rounds are charged and judged in the agent node only. The
default cap was also raised from 8 to 20 via `AGENT_MAX_ROUNDS`, and `/chat` now accepts a
per-request `max_rounds` (1-64).

### An approval appears to hang

**Symptom.** The approval card shows, the file never appears on disk, and the trace shows the write
as successful.

**Causes and fixes.**

- The trace shows a green check for a file that was never written. This was the three-state bug:
  the trace entry was created with `ok=True` before the tools node discovered the approval
  requirement. It now renders as amber "pending approval", with `ok=null`.
- The write never ran because the approval was never granted. Clicking Approve on the card is the
  only thing that releases it — replying "确认执行" in the chat does not.
- The approval was granted but the tool still did not run, because the graph instance was rebuilt on
  resume. `/approve` now reuses the graph registered for that `thread_id`.
- The trace panel did not update after approval, because `/approve` ran through `graph.invoke()`
  and produced no trace. `/approve` now rebuilds the trace from the final state and returns it.

### The C: drive fills up

**Symptom.** `C:` runs out of space after using the desktop app.

**Cause.** The old `--onefile` build extracted about 7.5 GB into `%TEMP%\_MEIxxxxxx` on every launch,
and abandoned `_MEI*` directories accumulated.

**Fix.** Switched to `--onedir` — no runtime extraction at all. To clean up leftovers from earlier
builds, run `python scripts\clean_c_drive.py`. Also consider relocating the data directory with
`AGENT_DATA_DIR`; see [`docs/04_数据目录外置.md`](docs/04_数据目录外置.md).

### Retrieval quality is poor and embeddings look meaningless

**Symptom.** `kb_search` returns odd results; the logs warn that embedding has degraded.

**Cause.** `FlagEmbedding` is not installed, or the `bge-m3` weights are not on disk. The embedder
fell back to hash pseudo-vectors and the reranker to cosine similarity.

**Fix.** `python -m pip install FlagEmbedding` and fetch the weights
(`python scripts\fetch_bge.py`, `python scripts\fetch_reranker.py`). Confirm the paths with
`BGE_M3_PATH` and `BGE_RERANKER_PATH`. `requirements.txt` intentionally lists only the minimum
runnable set, so this is an expected gap.

### The desktop app opens a console window as well as the app window

**Fix.** Build with `--window` (the current `deploy/build_desktop.ps1` default). If you built
without it, rebuild. The consequence of `--window` is that stdout and stderr are `None`, so
`desktop/entry_point.py` redirects them to
`%LOCALAPPDATA%\AgentPlatform\logs\desktop.log`.

### The second desktop launch does nothing

**Fix.** That is the intended behaviour. The second instance finds the existing window, restores it,
and exits. If the first instance was killed abnormally, the mutex dies with the process and the next
launch starts normally.

---

## Security notes

- **Local-first by design.** All inference is local (`127.0.0.1`). No telemetry, no analytics, no
  outbound calls to any third party.
- **The workspace tools execute on the local filesystem.** `ws_write_file` and `ws_edit_file` modify
  real files under `WORKSPACE_ROOT`. They are staged rather than written directly, and they require
  approval, but a user who approves a write is approving a real filesystem mutation. Point
  `WORKSPACE_ROOT` at a scratch directory if that is not what you want.
- **Custom script tools run local code.** A `script`-kind custom tool executes a Python file as a
  subprocess with the API process's privileges. There is no sandbox. Treat `data/custom_tools.json`
  as executable configuration and review it before deploying.
- **Custom HTTP tools can reach any host.** There is no URL allowlist. This is deliberate for a
  local-only deployment; do not populate the registry in an untrusted environment.
- **`.env` must never be committed.** It can contain real `api_key` values for non-local endpoints.
  Keep it out of version control.
- **`api_key: local` is a placeholder.** The default `LLM_API_KEY=local` exists only because the
  OpenAI-compatible client requires an Authorization header. It grants no access to anything. Real
  credentials belong in `LLM_API_KEY` for the specific endpoint being used.
- **The model registry does not echo secrets.** `GET /models` reports whether an `api_key` is
  configured, never its value.
- **BGE models stay on CPU.** This is a resource decision, but it also means the GPU is dedicated to
  the LLM. Do not "optimise" BGE onto the GPU on a 16 GB card.
- **Path traversal is blocked.** `Workspace.resolve_path()` resolves symlinks and junctions first,
  then verifies the result is still inside the workspace root. Writes into the data directory and
  into forbidden path names are rejected outright.
- **The audit log is append-only.** There is no update or delete API. It is a local record, so an
  attacker with write access to the data directory can still tamper with the file — it is an
  accountability record, not a tamper-proof log.

---

## Known limitations

- **The knowledge layer needs `FlagEmbedding`, which `requirements.txt` does not list.** Without it
  retrieval quality is not meaningful.
- **Single-node only.** The LangGraph checkpointer falls back to `MemorySaver` unless
  `langgraph-checkpoint-postgres` is installed. The API does not scale horizontally.
- **Windows-native tooling.** The desktop shell, the single-instance mutex, the console redirect, and
  the launch scripts are Windows-specific. The backend runs elsewhere; the desktop shell does not.
- **`qwen2.5-coder-14b` is a placeholder.** It is declared in `routing.yaml` with
  `installed: false`, but the endpoint on `:8082` does not exist. Code tasks fall back to
  `gpt-oss-20b`.
- **PostgreSQL and pgvector are optional.** SQLite is the default. The pgvector path exists in the
  store layer but is not the tested default.
- **The knowledge base targets one domain.** The `spec_extract` / `case_lint` / `tp_lookup` tools are
  built for semiconductor test-program analysis, not for general-purpose RAG.
- **`sql_query` needs a semantic warehouse.** It is read-only by construction, but its usefulness
  depends entirely on the warehouse being present and reachable.
- **The API port is inconsistent across scripts.** See
  [Known discrepancies](#known-discrepancies-in-this-repo).
- **Retrieval constants are hardcoded.** `RECALL_N`, `TOP_K`, `RRF_K`, and `MIN_SCORE_THRESHOLD`
  cannot be tuned through the environment despite what `.env.example` implies.
- **`data/models.json` ships a test artifact.** `verify-custom-48f74a` is leftover from
  `scripts/verify_modes.py`.
- **The in-process HITL gate is not durable.** Approval decisions live in process memory alongside
  the graph. A process restart during an approval loses the decision and the pending state.

---

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full guide: coding conventions, how to add tools and
endpoints, commit and PR conventions, and documentation expectations.

The short version:

- Comment **why**, not **what**. The house style is a Chinese comment that explains the reasoning
  behind a non-obvious decision, usually with the date and the concrete failure it prevents.
- Reference the design document section you are implementing.
- Include the measured numbers when you claim a performance property.
- Update the documentation in the same change. If you changed a constant, a default, or an endpoint,
  the English docs and `CHANGELOG.md` need updating too.

---

## License

**This project has no license file. All rights are reserved.**

The code, documentation, configuration, and build scripts in this repository are the property of
their authors. No permission is granted to use, copy, modify, merge, publish, distribute,
sublicense, or sell any part of this work, in any form, without prior written permission from the
copyright holders.

This is the default under copyright law when a project ships no license. It is not an oversight and
it is not a placeholder — it is a deliberate restriction. If you would like to use this project,
fork it, or contribute to it, please contact the maintainers first and obtain explicit permission.
Contributions submitted to this repository are understood to be offered under the same terms.

The third-party components this project depends on — FastAPI, LangGraph, llama.cpp, FlagEmbedding,
bge-m3, bge-reranker-v2-m3, gpt-oss-20b, and Qwen2.5 — carry their own licenses, which apply to
those components and not to this repository's code.

---

## Documentation index

### English documentation

| Document | Contents |
| --- | --- |
| [`docs/architecture.md`](docs/architecture.md) | Component diagram, request lifecycle, LangGraph node/edge/state reference, budget accounting, tool and retrieval pipelines, data flow and storage. |
| [`docs/api.md`](docs/api.md) | Full endpoint reference with request and response shapes, the SSE event schema, and the HITL handshake with real JSON. |
| [`docs/configuration.md`](docs/configuration.md) | Every environment variable with defaults and the layer that reads it, the data-directory externalization guide, the model registry file formats, and the known discrepancies. |
| [`docs/development.md`](docs/development.md) | Dev setup, the script inventory by purpose, how to add a tool / endpoint / model alias, how to build the desktop bundle, and the observed coding conventions. |
| [`docs/troubleshooting.md`](docs/troubleshooting.md) | Each issue as symptom, cause, and fix, with the actual error strings. |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Contribution guide, conventions, commit and PR conventions, documentation expectations. |
| [`CHANGELOG.md`](CHANGELOG.md) | Release history in Keep a Changelog format. |
| [`README.md`](README.md) | This file: the English landing page, kept in section-for-section sync with the Chinese translation. |
| [`README.zh-CN.md`](README.zh-CN.md) | The full Simplified Chinese translation of this README, kept in section-for-section sync with it. |

### Chinese design documents (preserved as written)

These are the original design documents. They remain the authoritative source for the domain
requirements, and they are kept in Chinese deliberately.

| Document | Abstract |
| --- | --- |
| [`docs/01_架构方案_RAG_Agent.md`](docs/01_架构方案_RAG_Agent.md) | **Architecture design for the RAG + Agent platform.** Component layering, the retrieval pipeline, the budget and timeout gate, context-window management, and the technology selection rationale. |
| [`docs/02_场景方案_STDF分析与测试用例生成.md`](docs/02_场景方案_STDF分析与测试用例生成.md) | **Domain scenario: STDF analysis and test-case generation.** The tool contract and permission model, the HITL card fields, the four-tier metric definitions, and the retrieval design for each knowledge source. |
| [`docs/03_显存与模型选型计算表.md`](docs/03_显存与模型选型计算表.md) | **VRAM budget and model selection worksheet.** Why `gpt-oss-20b` at MXFP4 fits a 16 GB card, what the Qwen2.5-14B CPU path costs, and the headroom analysis for adding a code-specialised model. |
| [`docs/04_数据目录外置.md`](docs/04_数据目录外置.md) | **Data-directory externalization.** How to relocate every runtime artifact off the system drive with `AGENT_DATA_DIR`, and how to migrate existing data. |
| [`docs/05_Agent_UI_设计调研与规格.md`](docs/05_Agent_UI_设计调研与规格.md) | **UI design research and specification.** Layout, the interaction model, the observability panels, the empty and error states, and the dark and light theme rules. |
| [`docs/06_代码模型接入策略.md`](docs/06_代码模型接入策略.md) | **Code-model integration strategy.** How to add a code-specialised model, the capability-tagging scheme, and the degradation protection that refuses to route code tasks to a non-code model. |
| [`docs/07_C盘空间治理.md`](docs/07_C盘空间治理.md) | **System-drive disk governance.** What consumes space on `C:`, the PyInstaller onefile problem, and the migration path to an onedir build. |
| [`agent_platform/knowledge/README.md`](agent_platform/knowledge/README.md) | **Knowledge-layer notes.** Chunking, embedding, reranking, and the retrieval chain, from the module that implements them. |
