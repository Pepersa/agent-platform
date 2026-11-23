# Configuration

Every environment variable the platform reads, what it does, which module reads it, and where it
came from.

For the API surface see [`api.md`](api.md). For the component map see
[`architecture.md`](architecture.md).

---

## Table of contents

- [How configuration is loaded](#how-configuration-is-loaded)
- [Data directory](#data-directory)
- [LLM client](#llm-client)
- [Model routing](#model-routing)
- [Agent behaviour](#agent-behaviour)
- [Knowledge layer](#knowledge-layer)
- [Threading](#threading)
- [Audit](#audit)
- [Workspace](#workspace)
- [Scripts](#scripts)
- [Data-directory externalization](#data-directory-externalization)
- [Model registry file formats](#model-registry-file-formats)
- [Known discrepancies](#known-discrepancies)

---

## How configuration is loaded

The platform reads **environment variables only**. There is no settings object and no config file
loader for scalar settings — `config/routing.yaml` is the one structured file, and it holds the
model registry and routes rather than scalar options.

`.env.example` is the starting template, but it is **not** a faithful mirror of what the code reads.
See [Known discrepancies](#known-discrepancies) before relying on it.

On Windows, set variables for the current session with:

```powershell
$env:AGENT_DATA_DIR = "D:\agent-data"
```

or permanently with:

```powershell
setx AGENT_DATA_DIR "D:\agent-data"
```

Note that `setx` only affects **new** processes.

---

## Data directory

| Variable | Default | Layer | Meaning |
| --- | --- | --- | --- |
| `AGENT_DATA_DIR` | `<repo>/data` | `core/paths.py` | Root for every runtime artifact. Absolute path; any drive. |

`core/paths.py` is the single resolver. Priority, highest first:

1. An explicit `override` argument — for tests and scripts.
2. `AGENT_DATA_DIR`.
3. `<repo_root>/data` — the default, identical to the pre-refactor layout.

Subdirectories created under the data root:

```
kb/            chunks.db, chunk_manifest.json
kb/uploads/    uploaded source files
audit/         audit.db, audit.jsonl
docs/          raw knowledge-base documents
reports/       rendered report artefacts
outbox/        notification outbox
shots/         screenshot artefacts
obs_shots/     observability captures
ui_ws_shots/   workspace UI captures
eval_reports/  evaluation output
_regress/      regression output
```

`scripts/` is deliberately not among them: it holds user-authored custom tool scripts, referenced by
repository-relative paths. The migration script leaves them where they are.

Resolution never creates directories. `ensure_data_dirs()` does that separately, idempotently, and
swallows failures so a read-only filesystem does not crash at startup.

---

## LLM client

Read by `core/llm_client.py`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `LLM_BASE_URL` | `http://127.0.0.1:8080/v1` | Primary OpenAI-compatible endpoint. |
| `LLM_MODEL` | `gpt-oss-20b` | Primary model name. |
| `LLM_REASONING_EFFORT` | `medium` | `low`, `medium`, or `high`. Invalid values fall back to `medium`. |
| `LLM_TEMPERATURE` | `0.2` | Sampling temperature. |
| `LLM_MAX_TOKENS` | `2048` | Maximum completion tokens per call. |
| `LLM_API_KEY` | `local` | Placeholder credential. Local endpoints ignore it; a real endpoint needs a real one. |
| `LLM_TIMEOUT` | `180` | Per-request timeout in seconds. |
| `LLM_FALLBACK_BASE_URL` | *(empty)* | Optional fallback endpoint. Used only when **both** this and `LLM_FALLBACK_MODEL` are set. |
| `LLM_FALLBACK_MODEL` | *(empty)* | Fallback model name. |
| `LLM_CONTEXT_WINDOW` | `32768` | Single-request context ceiling. Must match the llama-server `-c` value. |

**On `LLM_CONTEXT_WINDOW`.** This is a configuration ceiling, not a live probe. If the llama-server
runs with a different `-c`, requests that exceed it are rejected upstream with HTTP 400. The value
must be kept in sync with `deploy/start_servers.ps1`, whose `$CtxSize` default is also `32768`.

The window was raised from 8192 because 8192 is what actually blocked real code tasks: modifying a
200-line class, together with the system prompt and the tool schemas, exceeded it and the model
server returned `request exceeds the available context size`. `gpt-oss-20b` uses GQA with q8_0 KV
quantisation, so the KV cache is only 24 KiB per token — 32k of context costs roughly 0.75 GB of
VRAM, and the marginal cost of a wider window is very low.

---

## Model routing

| Variable | Default | Layer | Meaning |
| --- | --- | --- | --- |
| `ROUTING_CONFIG` | `<repo>/config/routing.yaml` | `api/app.py`, `core/model_registry.py` | Path to the routing table. |

See [Model registry file formats](#model-registry-file-formats).

---

## Agent behaviour

| Variable | Default | Layer | Meaning |
| --- | --- | --- | --- |
| `AGENT_MAX_ROUNDS` | `20` | `api/app.py` | Default ReAct round cap for `/chat`. One LLM call is one round. |
| `AGENT_DEBUG_TOKENS` | *(unset)* | `orchestration/graph.py` | Prints the local token estimate next to the model's real `prompt_tokens`, for calibrating the estimator. Off in production; costs one environment check. |
| `AGENT_FORCE_LEGACY_RETRIEVAL` | *(unset)* | `core/retrieval_policy.py`, `orchestration/prompts/prompts.py` | Set to `1` to restore the pre-policy "always retrieve" behaviour. Exists only for before/after evidence collection. Normal runs never set it. |

**`AGENT_MAX_ROUNDS` versus `BudgetGuard.max_rounds`.** The dataclass default in
`core/budget.py` is `8`; the API's `DEFAULT_MAX_ROUNDS` is `20`. For `/chat` the API value wins.
The per-request `max_rounds` field on `/chat` (range 1-64) overrides it for that request only.

---

## Knowledge layer

| Variable | Default | Layer | Meaning |
| --- | --- | --- | --- |
| `BGE_M3_PATH` | *(unset)* | `knowledge/embed.py` | Local `bge-m3` weights directory. |
| `BGE_RERANKER_PATH` | *(unset)* | `knowledge/reranker.py` | Local `bge-reranker-v2-m3` weights directory. |
| `BGE_DEVICES` | `cpu` | `knowledge/embed.py` | Devices for FlagEmbedding. `cpu` forces CPU. `cuda:0` explicitly allows the GPU (you accept the OOM risk). `auto` returns `None` and lets FlagEmbedding decide. |
| `LLAMA_BASE_URL` | `http://127.0.0.1:8080` | `knowledge/embed.py` | llama-server base URL for embedding calls. |
| `LLAMA_BASE_URL_2` | `http://127.0.0.1:8081` | `knowledge/llm.py` | Secondary llama-server base URL (the Qwen2.5-14B endpoint). |
| `LLAMA_EMBED_MODEL` | *(unset)* | `knowledge/embed.py` | Model alias to request from the llama-server embedding endpoint. |
| `BGE_RERANK_SHORTLIST` | `0` | `knowledge/reranker.py` | `0` reranks every candidate. Non-zero truncates to the RRF order, which measurably loses top-6 quality (23/36 overlap). |
| `KB_DOCS_DIR` | `<data>/docs` | `knowledge/tools_ext.py` | Where raw knowledge-base documents live. |
| `KB_REPORTS_DIR` | `<data>/reports` | `knowledge/tools_ext.py` | Where rendered reports land. |
| `KB_OUTBOX_PATH` | `<data>/outbox/notify.jsonl` | `knowledge/tools_ext.py` | Outbox file for `notify` deliveries. |
| `KB_CHUNKS_DB` | `<data>/kb/chunks.db` | `knowledge/tools_ext.py` | SQLite chunk index path. |

**Backend fallback chains.**

The backends are **auto-probed**, not environment-selected. The chains are hardcoded in the
constructors of `knowledge/embed.py` and `knowledge/reranker.py`:

```
Embedder:  flagembedding  ->  llama-server  ->  hash (degraded)
Reranker:  flag           ->  cosine similarity (degraded)
```

`.env.example` lists `EMBED_BACKEND` and `RERANK_BACKEND`, but no code reads them — see
[Known discrepancies](#known-discrepancies).

The `hash` embedder produces deterministic pseudo-vectors: the same text always yields the same
vector, and shared vocabulary produces some similarity signal. That is not semantic similarity and
must not be used to judge retrieval quality. The embedder logs a warning whenever it degrades.

**On `BGE_DEVICES`.** Keep it at `cpu`. `bge-m3` and the LLM together need 22.3 GB, and the card
has 16 GB. Putting BGE on the GPU produced `CUDA error: an illegal memory access` and took down
both inference servers — reproduced twice. `bge-m3` is a pure encoder and retrieval is low
frequency, so CPU is entirely sufficient.

---

## Threading

| Variable | Default | Layer | Meaning |
| --- | --- | --- | --- |
| `AGENT_TORCH_THREADS` | `4` | `knowledge/threading_limits.py` | PyTorch intra-op threads while idle. |
| `AGENT_TORCH_INFER_THREADS` | `8` | `knowledge/threading_limits.py` | PyTorch threads during inference, restored to the idle value afterwards. |

The two are deliberately different. Idle, the goal is "do not spin"; inference, the goal is "go
fast". The OpenMP pools of the resident BGE models spin-wait after loading, which burned 32% of a
16-thread machine with no requests in flight. The limiter caps that.

During inference the thread count is raised and then restored, because a cross-encoder is pure CPU
matrix work and 4 threads is nearly 2x slower than 8 (measured `n=20, max_length=256`: 19.1 s at 4
threads vs 10.8 s at 8). 16 threads is slower still — oversubscription eats the gain.

The limiter also sets `OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, and
`NUMEXPR_NUM_THREADS` before torch initialises OpenMP, because those variables only take effect at
runtime initialisation.

---

## Audit

| Variable | Default | Layer | Meaning |
| --- | --- | --- | --- |
| `AUDIT_DSN` | *(empty)* | `core/audit.py` | PostgreSQL DSN. |

Fallback chain:

```
AUDIT_DSN set and psycopg importable  →  PostgreSQL
       │ fails
       ▼
SQLite at <data>/audit/audit.db
       │ fails
       ▼
JSONL at <data>/audit/audit.jsonl
```

If the primary backend fails mid-write, the entry goes to JSONL instead, so a record is never
silently dropped.

---

## Workspace

| Variable | Default | Layer | Meaning |
| --- | --- | --- | --- |
| `WORKSPACE_ROOT` | repository root | `core/workspace.py` | Root of the code workspace that `ws_*` tools operate on. |

Resolution order: the explicit `root` argument, then `WORKSPACE_ROOT`, then the repository root.

All workspace paths are relative to this root. `resolve_path()` resolves symlinks and junctions
first, then verifies the result is still inside the root, so symlink escapes are caught too.

---

## Scripts

Read by the scripts in `scripts/`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MODEL_ROOT` | `E:\model\models` | Where the model-fetch scripts download weights. |
| `AGENT_API_BASE` | `http://127.0.0.1:8010` | API base URL for verification scripts. |
| `AGENT_UI_BASE` | `http://127.0.0.1:8010` | UI base URL for capture scripts. |
| `PIP_CACHE_DIR` | *(pip default)* | pip cache location. |
| `HF_HOME` | *(HF default)* | HuggingFace cache location. |

---

## Data-directory externalization

Condensed from [`04_数据目录外置.md`](04_数据目录外置.md) (Chinese original preserved).

### Why

Runtime data used to live under `<repo>/data/`. That caused three problems:

1. Moving to a different drive meant moving the entire project directory.
2. The project directory might end up on a read-only, network, or version-controlled path.
3. Large files — the session database, the knowledge-base index — polluted the code repository.

### How

Set one variable:

```powershell
$env:AGENT_DATA_DIR = "D:\agent-data"       # this session
setx AGENT_DATA_DIR "D:\agent-data"         # permanent
```

Everything follows: `sessions.db`, `workspace.db`, `audit/`, `kb/chunks.db`, `models.json`,
`custom_tools.json`, logs, screenshots, and evaluation output.

### Constraints the implementation honours

1. **Standard library only.** `core/paths.py` introduces no third-party dependency.
2. **The default does not change.** With `AGENT_DATA_DIR` unset, paths are byte-for-byte identical
   to the pre-refactor layout. Existing data — including the `verify-custom-48f74a` model entry — is
   not lost.
3. **No side effects during resolution.** `data_dir()` only computes a path. Directory creation
   happens in `ensure_data_dirs()`.
4. **Creation failures degrade rather than crash.** An unwritable directory is reported by the
   subsequent write operation, not at startup.

### Migrating existing data

```powershell
# Dry run first
python scripts\migrate_data_dir.py --to D:\agent-data --dry-run

# Then for real
python scripts\migrate_data_dir.py --to D:\agent-data
```

The `scripts/` directory is intentionally **not** migrated: it holds user-authored custom tool
scripts referenced by repository-relative paths, and moving them would break those references.

### Verifying

```powershell
$env:AGENT_DATA_DIR = "D:\agent-data"
python -c "from agent_platform.core import paths; print(paths.data_dir_info())"
```

```json
{
  "dir": "D:\\agent-data",
  "exists": true,
  "external": true,
  "source": "env",
  "subdirs": ["kb", "kb/uploads", "audit", "docs", "reports", "outbox", "shots", "obs_shots", "ui_ws_shots", "eval_reports", "_regress"]
}
```

`GET /health` and `GET /platform/status` report the same information.

---

## Model registry file formats

Two sources merge into one registry, handled by `core/model_registry.py`.

### `config/routing.yaml` — static

```yaml
models:
  gpt-oss-20b:
    base_url: http://127.0.0.1:8080/v1
    model: gpt-oss-20b
    reasoning_effort: medium
    temperature: 0.2
    max_tokens: 2048
    api_key: local
    timeout: 180
    capabilities: [code, tool_call, reasoning, chinese]

  qwen2.5-14b:
    base_url: http://127.0.0.1:8081/v1
    model: qwen2.5-14b
    reasoning_effort: low
    temperature: 0.2
    max_tokens: 2048
    api_key: local
    timeout: 180
    # NOTE: deliberately no `code` tag
    capabilities: [tool_call, reasoning, chinese]

  qwen2.5-coder-14b:
    base_url: http://127.0.0.1:8082/v1
    model: qwen2.5-coder-14b
    reasoning_effort: low
    temperature: 0.1
    max_tokens: 4096
    api_key: local
    timeout: 300
    capabilities: [code, tool_call, reasoning, chinese]
    installed: false
    description: 代码专用模型（Qwen2.5-Coder-14B-Instruct Q4_K_M）—— 尚未安装

routes:
  code:
    fallback: [qwen2.5-coder-14b, gpt-oss-20b]
    requires: [code]

  agent:
    fallback: [gpt-oss-20b]
    requires: [code, tool_call]

  reasoning:
    fallback: [gpt-oss-20b]
    requires: [reasoning]

  tool_call:
    fallback: [gpt-oss-20b]
    requires: [tool_call]

  ask:
    fallback: [gpt-oss-20b]
    requires: [reasoning]

  plan:
    fallback: [gpt-oss-20b]
    requires: [reasoning]

  chinese:
    fallback: [qwen2.5-14b, gpt-oss-20b]

  eval:
    fallback: [qwen2.5-14b, gpt-oss-20b]

  summarize:
    fallback: [qwen2.5-14b, gpt-oss-20b]

  default:
    fallback: [gpt-oss-20b, qwen2.5-14b]
```

**Model fields.**

| Field | Type | Meaning |
| --- | --- | --- |
| `base_url` | `string` | The OpenAI-compatible endpoint. |
| `model` | `string` | The model name on that endpoint. |
| `reasoning_effort` | `string` | `low`, `medium`, or `high`. |
| `temperature` | `float` | Sampling temperature. |
| `max_tokens` | `int` | Maximum completion tokens. |
| `api_key` | `string` | Credential. Never echoed back by `GET /models`. |
| `timeout` | `float` | Request timeout in seconds. |
| `capabilities` | `list` | Capability tags. |
| `installed` | `bool` | Defaults to `true`. `false` marks a placeholder that is not deployed. |
| `description` | `string` | Shown in the UI. |

**Capability tags.** `code`, `tool_call`, `reasoning`, `chinese`, `long_context`, `vision`.

**Route fields.**

| Field | Meaning |
| --- | --- |
| `fallback` | Ordered chain. First is primary, the rest are fallbacks. |
| `requires` | Capabilities every chain member must have. Members lacking them are stripped. If the chain empties, the router raises `RoutingCapabilityError` rather than silently downgrading. |

The `code` and `agent` routes intentionally exclude `qwen2.5-14b`. It is a general chat model
without the `code` capability, and the agent loop calls `ws_write_file` / `ws_edit_file`. If it were
a fallback, a code task would land on it whenever `gpt-oss-20b` was unavailable — with no error
raised and no visible symptom beyond "the output got worse".

`pyyaml` is optional. Without it the router falls back to the in-code `DEFAULT_ROUTES` constant.

### `data/models.json` — dynamic

Written by the API when a user adds or edits a model. Written atomically (temp file plus
`os.replace`) so a crash cannot corrupt it.

```json
{
  "version": 1,
  "updated_at": 1795399816.7421675,
  "models": [
    {
      "name": "my-local-llm",
      "label": "My Local LLM",
      "base_url": "http://127.0.0.1:8085/v1",
      "model": "my-model",
      "description": "自建推理服务",
      "reasoning_effort": "medium",
      "temperature": 0.2,
      "max_tokens": 2048,
      "api_key": "local",
      "timeout": 180.0,
      "supports_tools": true,
      "capabilities": [],
      "installed": true,
      "source": "custom",
      "builtin": false,
      "created_at": 1795399816.0,
      "updated_at": 1795399816.0
    }
  ]
}
```

`capabilities` on a custom entry defaults to empty. If you want a custom model to serve a
capability-constrained route, add the tags — otherwise the router will strip it from that chain.

The registry currently contains a leftover test artifact, `verify-custom-48f74a`, registered by
`scripts/verify_modes.py`. It points at `127.0.0.1:8080` and is harmless, but it is noise.

### Merging and probing

`GET /models` returns the merged view. Each entry carries a live reachability probe, so the status
dot in the UI reflects an actual measurement rather than an assumption.

**Security boundary, stated plainly.** `base_url` is whatever the user typed; the platform does not
apply a URL allowlist. A registry entry can therefore point at any HTTP endpoint. This is a
deliberate choice for a local-only deployment — only populate the registry in a trusted
environment.

---

## Known discrepancies

These are real, documented rather than hidden. The code is the source of truth in every case.

### 1. `.env.example` lists variables the code never reads

Twenty-one variables appear in `.env.example` but are hardcoded constants in the source, or are
renamed. Setting them has no effect.

| Variable in `.env.example` | Where the real value actually lives |
| --- | --- |
| `RECALL_N` | `knowledge/retriever.py` — `RECALL_N = 50` |
| `TOP_K` | `knowledge/retriever.py` — `FINAL_TOP_K = 6` |
| `RRF_K` | `knowledge/retriever.py` — `RRF_K = 60` |
| `MIN_SCORE_THRESHOLD` | `knowledge/retriever.py` — `MIN_SCORE_THRESHOLD = 0.012` |
| `EMBEDDING_DIM` | `knowledge/embed.py` — `EMBEDDING_DIM = 1024` |
| `EMBED_BACKEND` | Nowhere — the embedder auto-probes a hardcoded chain |
| `RERANK_BACKEND` | Nowhere — the reranker auto-probes a hardcoded chain |
| `BUDGET_MAX_ROUNDS` | `core/budget.py` — `max_rounds: int = 8` |
| `BUDGET_MAX_TOKENS` | `core/budget.py` — `max_tokens: int = 60_000` |
| `BUDGET_MAX_TOOL_CALLS` | `core/budget.py` — `max_tool_calls: int = 24` |
| `BUDGET_MAX_SECONDS` | `core/budget.py` — `max_seconds: float = 900.0` |
| `MAX_TOKENS_PER_TOOL_RESULT` | `core/budget.py` — `max_tokens_per_tool_result: int = 4_000` |
| `VECTOR_STORE` | `knowledge/store.py` — the store selects its backend internally |
| `PG_DSN` | Not read by the code |
| `CHUNK_DB_PATH` | `knowledge/tools_ext.py` — `KB_CHUNKS_DB` |
| `AUDIT_FALLBACK_PATH` | `core/audit.py` — the fallback directory comes from `AGENT_DATA_DIR` |
| `DOCS_DIR` | `knowledge/tools_ext.py` — `KB_DOCS_DIR` |
| `ARTIFACTS_DIR` | `knowledge/tools_ext.py` — `KB_REPORTS_DIR` |
| `LLM_TIMEOUT_S` | `core/llm_client.py` — the variable is `LLM_TIMEOUT` |
| `LLM_SECONDARY_BASE_URL` | Not read — use `LLAMA_BASE_URL_2` for the knowledge layer |
| `LLM_SECONDARY_MODEL` | Not read — register the alias in `data/models.json` instead |

The retrieval constants in particular are worth knowing: `.env.example` says
`MIN_SCORE_THRESHOLD=0.35`, but the code uses `0.012`. The code's value is correct — it is a
threshold on the RRF score, whose magnitude is `Σ 1/(60+rank)`, not on a normalised similarity. A
dual-branch top-1 hit is about `0.033`, so a `0.35` threshold would reject everything.

### 1a. `.env.example` omits variables the code does read

| Variable | Default | Layer |
| --- | --- | --- |
| `LLM_API_KEY` | `local` | `core/llm_client.py` |
| `LLM_FALLBACK_BASE_URL` | *(empty)* | `core/llm_client.py` |
| `LLM_FALLBACK_MODEL` | *(empty)* | `core/llm_client.py` |
| `WORKSPACE_ROOT` | *(repo root)* | `core/workspace.py` |
| `ROUTING_CONFIG` | `<repo>/config/routing.yaml` | `core/model_registry.py` |

`api_key` is a placeholder for local OpenAI-compatible endpoints; llama-server ignores it.

### 2. `requirements.txt` omits `FlagEmbedding` and friends

`requirements.txt` deliberately lists the **minimum runnable set** — FastAPI, uvicorn, pydantic,
openai, langgraph, sqlglot, psycopg, pyyaml, httpx, pywebview, pytest, aiohttp — with a note that
the local Python is 3.13.9 and some packages lack prebuilt wheels.

Not listed, but needed for full quality:

| Package | Needed for |
| --- | --- |
| `FlagEmbedding` | Real `bge-m3` embeddings and `bge-reranker-v2-m3` reranking |
| `numpy` | Vector operations |
| `torch` | The torch backend for FlagEmbedding |
| `docling` | Rich document parsing |
| `pypdf` | PDF extraction |
| `python-docx` | DOCX extraction |

Without `FlagEmbedding` the platform still runs: the embedder falls back to hash pseudo-vectors and
the reranker to cosine similarity. Retrieval quality is then meaningless, and no error is raised —
only a warning is logged.

### 3. Two naming schemes for the same endpoints

| Variable | Read by | Layer |
| --- | --- | --- |
| `LLM_BASE_URL` | `core/llm_client.py` | The agent's chat completions |
| `LLAMA_BASE_URL` | `knowledge/embed.py` | The knowledge layer's embedding calls |

Both point at a llama-server, but they are read by different modules. Setting one does not affect
the other.

### 4. `LLM_TIMEOUT` versus `LLM_TIMEOUT_S`

`.env.example` uses `LLM_TIMEOUT_S`. The code reads `LLM_TIMEOUT`. The value in `.env.example` has
no effect.

### 5. The API port is inconsistent

| Source | Port |
| --- | --- |
| `deploy/start_api.ps1` | `8000` |
| `deploy/start_platform.ps1` | `8010` |
| `deploy/start_desktop.ps1` | `8010` |
| `deploy/kind.yaml` | `8000` (host port mapping) |
| Verification scripts (`AGENT_API_BASE`) | `8010` |
| This documentation | `8010` |

Use `8010`. If you start the API with `deploy/start_api.ps1`, either pass `-Port 8010` or expect
`8000`.

### 6. Two round-limit knobs

| Knob | Value | Scope |
| --- | --- | --- |
| `BudgetGuard.max_rounds` (dataclass default) | `8` | Any direct user of the guard |
| `DEFAULT_MAX_ROUNDS` (`AGENT_MAX_ROUNDS`) | `20` | The `/chat` endpoint |

For `/chat`, the API value wins. The per-request `max_rounds` field overrides it for that request.

### 7. `data/models.json` ships a test artefact

`verify-custom-48f74a` was registered by `scripts/verify_modes.py` and never removed. It is
harmless — it points at `127.0.0.1:8080` — but it is noise in the model list.

### 8. `SESSION_GRANT_TTL_SECONDS` is not configurable

The session-grant TTL is a constant in `orchestration/hitl.py` — 30 minutes — not an environment
variable.
