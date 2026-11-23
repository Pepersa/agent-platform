# API Reference

Complete HTTP reference for the Agent Platform API. 53 endpoints, all served by
`agent_platform/api/app.py`.

Base URL in development: `http://127.0.0.1:8010`

Interactive OpenAPI docs: `http://127.0.0.1:8010/docs`

---

## Table of contents

- [Conventions](#conventions)
- [Chat and HITL](#chat-and-hitl)
  - [POST /chat](#post-chat)
  - [POST /approve](#post-approve)
  - [The HITL handshake](#the-hitl-handshake)
- [SSE event schema](#sse-event-schema)
- [Workspace](#workspace)
- [Models](#models)
- [Knowledge base](#knowledge-base)
- [Custom tools](#custom-tools)
- [Sessions](#sessions)
- [Tasks](#tasks)
- [Platform](#platform)
- [Tools](#tools)
- [Diagnostics](#diagnostics)
- [Endpoint index](#endpoint-index)

---

## Conventions

- **Content type.** Request and response bodies are JSON unless noted. `POST /kb/documents/upload`
  takes `multipart/form-data`.
- **Streaming.** `POST /chat` returns `text/event-stream` by default. Pass `"stream": false` in the
  body to get a single JSON response instead.
- **Error shape.** Workspace and knowledge-base endpoints return a structured error object rather
  than an HTTP error status, with a stable `code` field (`WS_FORBIDDEN_FILE`, `WS_FILE_CHANGED`,
  `WS_NO_UNDO`, `KB_*`, and so on).
- **Backwards compatibility.** Every new response field is additive. Clients read by field name, so
  older clients keep working against a newer server.
- **Field naming.** The API uses English field names. A few human-facing values are Chinese strings
  (for example `retrieval_label`), because they are rendered directly in the UI.

---

## Chat and HITL

### POST /chat

Starts a turn.

**Request body**

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `message` | `string` | *required* | The user input. |
| `thread_id` | `string \| null` | `null` | Thread identifier. Generated as `th-<12 hex>` when omitted. Reuse it to continue a conversation. |
| `user_id` | `string \| null` | `null` | Acting user. |
| `session_id` | `string \| null` | `null` | Session identifier. Generated as `S-<8 hex>` when omitted. |
| `stream` | `boolean \| null` | `null` | `true` for SSE. When `null` it defaults to `true`. |
| `model` | `string \| null` | `null` | Model name from the registry. |
| `mode` | `string \| null` | `null` | `agent` (default), `ask`, or `plan`. |
| `max_rounds` | `integer \| null` | `null` | ReAct round cap for this request. Range 1-64. When `null`, `AGENT_MAX_ROUNDS` applies (default 20). |

```json
{
  "message": "T101_VDD_FUNCT 的上下限是多少？请引用来源。",
  "thread_id": "th-a1b2c3d4e5f6",
  "mode": "agent",
  "max_rounds": 20,
  "stream": false
}
```

**Response body** (non-streaming)

| Field | Type | Meaning |
| --- | --- | --- |
| `status` | `string` | `done`, `pending_approval`, or `error`. |
| `answer` | `string \| null` | The final answer. When an approval is pending, this contains an explicit notice that the operation has **not** executed. |
| `thread_id` | `string` | The thread. |
| `approvals` | `array` | Pending approval cards. Empty when nothing is pending. |
| `tokens_used` | `integer` | Cumulative tokens. |
| `tool_calls_used` | `integer` | Cumulative tool **executions**. Approval waits are not counted. |
| `rounds` | `integer` | Completed ReAct rounds, read from graph state. |
| `stop_reason` | `string \| null` | `done`, `empty_response`, `pending_approval`, `rejected_by_human`, `exception`, or `budget_exceeded:<REASON>`. The `budget_exceeded` form is prefixed with the budget reason — `budget_exceeded:MAX_TOKENS`, `budget_exceeded:MAX_ROUNDS`, `budget_exceeded:MAX_CONTEXT`, `budget_exceeded:MAX_TOOL_CALLS`, `budget_exceeded:MAX_SECONDS`. |
| `model` | `string` | The model actually used. |
| `mode` | `string` | The mode actually used. |
| `plan` | `object \| null` | The structured plan, in plan mode. |
| `retrieval_needed` | `boolean \| null` | Whether this turn used the knowledge base. |
| `retrieval_rule` | `string` | The rule that decided. |
| `retrieval_reason` | `string` | A human-readable reason. |
| `retrieval_policy_version` | `string` | The policy version. |
| `retrieval_label` | `string` | A display string such as `检索: 已跳过`. |
| `trace` | `array` | The decision trace. |
| `budget` | `object` | The budget snapshot (see below). |
| `fast_path` | `boolean` | Present on `/approve`; true when the fast path was taken. |

**Approval card shape** (one element of `approvals`)

| Field | Type | Meaning |
| --- | --- | --- |
| `tool_call_id` | `string` | The identifier to pass to `/approve`. |
| `tool_name` | `string` | The tool awaiting approval. |
| `args` | `object` | The arguments the model proposed. |
| `permission` | `string` | The tool's permission level. |
| `impact` | `string` | A human-readable description of the effect. |
| `reversible` | `boolean` | Whether the operation can be undone. |
| `diff` | `object \| null` | Added/removed line counts, and a full diff for workspace writes. |
| `metadata` | `object` | Tool-specific metadata. |

**Budget snapshot shape**

```json
{
  "rounds": 2,
  "max_rounds": 20,
  "tokens_used": 1180,
  "max_tokens": 60000,
  "tool_calls_used": 1,
  "max_tool_calls": 24,
  "context_window": 32768,
  "system_prompt_chars": 4213
}
```

### POST /approve

Resolves a pending approval and resumes the graph.

**Request body**

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `thread_id` | `string` | *required* | The thread that produced the approval. Must match the `/chat` call. |
| `tool_call_id` | `string` | *required* | The approval to resolve. |
| `approved` | `boolean` | *required* | Approve or reject. |
| `actor` | `string` | *required* | Who decided. |
| `reason` | `string` | `""` | Why. |
| `session_grant` | `boolean` | `false` | Grant this tool for the rest of the session. |
| `summarize` | `boolean` | `false` | `false` (default) takes the fast path and skips the second LLM call. `true` forces the model to write a natural-language summary. |

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

**Response body**

Same shape as the non-streaming `/chat` response, plus:

| Field | Type | Meaning |
| --- | --- | --- |
| `fast_path` | `boolean` | `true` when the second LLM call was skipped. |
| `trace` | `array` | Rebuilt from the final graph state, so the trace panel updates after approval. |
| `budget` | `object` | The budget snapshot after the approval. |

On failure the endpoint returns HTTP 500 with the same field structure, so clients do not need a
separate error branch:

```json
{
  "status": "error",
  "answer": "RuntimeError: ...",
  "thread_id": "th-a1b2c3d4e5f6",
  "approvals": [],
  "tokens_used": 0,
  "tool_calls_used": 0,
  "stop_reason": "exception",
  "trace": []
}
```

**Graph reuse.** `/approve` looks up the graph registered for the `thread_id` rather than building a
new one. Building a new graph would silently reset `max_rounds`, the model, and the mode. If the
original graph is gone (a process restart, or a cross-instance approval) the endpoint falls back to
building a new one, and `max_rounds` reverts to the default — a known degradation.

### The HITL handshake

A complete round trip.

**Step 1 — request**

```http
POST /chat
Content-Type: application/json

{
  "message": "在 lz/demo.py 写一个张量自动微分的示例",
  "thread_id": "th-a1b2c3d4e5f6",
  "mode": "agent",
  "max_rounds": 20,
  "stream": false
}
```

**Step 2 — pending approval**

```json
{
  "status": "pending_approval",
  "answer": "文件尚未写入。\n\n以下写操作已发起，但正挂在人工审批上、尚未执行：ws_write_file。\n\n请在下方审批卡片上点击「批准」后才会真正落盘；在对话里回复「确认执行」不会自动放行。",
  "thread_id": "th-a1b2c3d4e5f6",
  "approvals": [
    {
      "tool_call_id": "call_9f3a",
      "tool_name": "ws_write_file",
      "args": {
        "path": "lz/demo.py",
        "content": "import torch\n\ndef grad_sq(x):\n    return 2 * x\n"
      },
      "permission": "WRITE_DATA",
      "impact": "写入 lz/demo.py",
      "reversible": true,
      "diff": { "added": 4, "removed": 0 },
      "metadata": {}
    }
  ],
  "tokens_used": 1180,
  "tool_calls_used": 0,
  "rounds": 2,
  "stop_reason": "pending_approval",
  "trace": [
    {
      "seq": 1,
      "kind": "llm",
      "name": "gpt-oss-20b",
      "ok": true,
      "duration_ms": 812,
      "started_at": 1795399816.10,
      "ended_at": 1795399816.91,
      "tokens": 1180,
      "reasoning_summary": "用户要求写代码，需要调用 ws_write_file。"
    },
    {
      "seq": 2,
      "kind": "tool",
      "id": "call_9f3a",
      "name": "ws_write_file",
      "args": { "path": "lz/demo.py", "content": "..." },
      "result": null,
      "ok": null,
      "status": "pending_approval",
      "action_summary": "等待人工确认（尚未执行）：ws_write_file",
      "duration_ms": null
    },
    {
      "seq": 3,
      "kind": "approval",
      "id": "call_9f3a",
      "name": "ws_write_file",
      "ok": null,
      "status": "pending_approval",
      "permission": "WRITE_DATA",
      "impact": "写入 lz/demo.py",
      "reversible": true,
      "duration_ms": null
    }
  ],
  "budget": {
    "rounds": 2,
    "max_rounds": 20,
    "tokens_used": 1180,
    "max_tokens": 60000,
    "tool_calls_used": 0,
    "max_tool_calls": 24,
    "context_window": 32768
  }
}
```

Two details are worth calling out:

- `tool_calls_used` is `0`. The tool has not executed.
- The `kind: "tool"` trace entry has `ok: null`, not `ok: true`. The tool-call entry is created when
  the agent node emits the call, which is before the tools node discovers the approval requirement.
  The approval event rewrites the matching entry in place, so the UI shows amber "awaiting
  approval" instead of a green check for a file that was never written.

**Step 3 — approve**

```http
POST /approve
Content-Type: application/json

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

**Step 4 — result**

```json
{
  "status": "done",
  "answer": "已写入 lz/demo.py（4 行新增）。如需回滚，请调用 POST /workspace/undo。",
  "thread_id": "th-a1b2c3d4e5f6",
  "approvals": [],
  "tokens_used": 1180,
  "tool_calls_used": 1,
  "rounds": 2,
  "stop_reason": "done",
  "fast_path": true,
  "trace": [
    { "seq": 1, "kind": "llm", "ok": true, "duration_ms": 812 },
    { "seq": 2, "kind": "tool", "id": "call_9f3a", "name": "ws_write_file", "ok": true, "status": "success", "duration_ms": 14 },
    { "seq": 3, "kind": "approval", "id": "call_9f3a", "ok": null, "status": "pending_approval" }
  ],
  "budget": {
    "rounds": 2,
    "max_rounds": 20,
    "tokens_used": 1180,
    "max_tokens": 60000,
    "tool_calls_used": 1,
    "max_tool_calls": 24,
    "context_window": 32768
  }
}
```

**Rejecting** uses the same request with `"approved": false`. The tool does not run and
`stop_reason` becomes `rejected_by_human`.

**Session grant** uses `"session_grant": true`. The tool runs now, and the same tool is
auto-approved for the rest of the session (until `SESSION_GRANT_TTL_SECONDS` expires).

---

## SSE event schema

`POST /chat` with streaming enabled returns `text/event-stream`. Each event is a single `data:`
line containing a JSON object with an `event` field. The stream ends with `data: [DONE]`.

### `start`

```json
{
  "event": "start",
  "thread_id": "th-a1b2c3d4e5f6",
  "ts": 1795399816.05,
  "message": "T101_VDD_FUNCT 的上下限是多少？",
  "mode": "agent",
  "model": ""
}
```

### `step`

One per graph node update. Fields present depend on the node.

```json
{
  "event": "step",
  "thread_id": "th-a1b2c3d4e5f6",
  "node": "agent",
  "tokens_used": 1180,
  "tool_calls_used": 0,
  "ts": 1795399816.95,
  "llm_duration_ms": 812,
  "llm_started_at": 1795399816.10,
  "llm_ended_at": 1795399816.91,
  "retrieval_needed": true,
  "retrieval_rule": "force_factual_signal",
  "retrieval_reason": "问题包含限值类实体，需查证",
  "retrieval_label": "检索: 已执行",
  "tools": ["kb_search"],
  "tool_calls": [
    {
      "id": "call_9f3a",
      "name": "ws_write_file",
      "args": { "path": "lz/demo.py", "content": "..." }
    }
  ],
  "tool_results": [],
  "reasoning_summary": "用户要求写代码，需要调用 ws_write_file。"
}
```

### `approval`

```json
{
  "event": "approval",
  "thread_id": "th-a1b2c3d4e5f6",
  "approvals": [
    {
      "tool_call_id": "call_9f3a",
      "tool_name": "ws_write_file",
      "args": { "path": "lz/demo.py", "content": "..." },
      "permission": "WRITE_DATA",
      "impact": "写入 lz/demo.py",
      "reversible": true,
      "diff": { "added": 4, "removed": 0 },
      "metadata": {}
    }
  ]
}
```

The same `tool_call_id` is never pushed twice — the server deduplicates.

### `plan`

Plan mode only.

```json
{
  "event": "plan",
  "thread_id": "th-a1b2c3d4e5f6",
  "plan": {
    "goal": "为 demo.py 增加自动微分示例",
    "steps": [
      {
        "title": "编写示例文件",
        "detail": "创建 lz/demo.py，包含梯度计算函数",
        "tools": ["ws_write_file"],
        "risk": "中"
      }
    ],
    "risks": ["覆盖已有文件"],
    "questions": []
  }
}
```

### `result`

The terminal payload, matching the non-streaming response body.

```json
{
  "event": "result",
  "status": "done",
  "answer": "...",
  "thread_id": "th-a1b2c3d4e5f6",
  "approvals": [],
  "tokens_used": 1180,
  "tool_calls_used": 1,
  "rounds": 2,
  "stop_reason": "done",
  "mode": "agent",
  "model": "gpt-oss-20b",
  "trace": [],
  "budget": {}
}
```

### `[DONE]`

```
data: [DONE]
```

---

## Workspace

All workspace paths are relative to `WORKSPACE_ROOT` (default: the repository root).

### GET /workspace/info

Workspace metadata: the root directory, the file size limit, the undo stack depth, and the
forbidden-path lists.

```json
{
  "root": "E:\\PYPROJECT\\agent",
  "max_file_bytes": 524288,
  "undo_depth": 12,
  "undo_max_depth": 50,
  "store_path": "E:\\PYPROJECT\\agent\\data\\workspace.db",
  "forbidden_dirs": [".git", ".hg", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".svn", ".tox", ".venv", "__pycache__", "node_modules", "venv"],
  "forbidden_files": [".env*", "*.key", "*.pem", "*.p12", "*.pfx", "data/kb/*.db", "data/sessions.db", "data/models.json"]
}
```

`undo_depth` is the current stack depth; `undo_max_depth` is the configured ceiling (default 50).
`forbidden_dirs` blocks whole directories at any level; `forbidden_files` blocks specific names and
patterns, matched against both the file name and the relative path.

### GET /workspace/tree

Query: `path` (default `""`), `depth` (default `1`, clamped to 1..3).

Directories come first; files are sorted by name.

```json
{
  "path": "",
  "entries": [
    {
      "name": "agent_platform",
      "type": "dir",
      "path": "agent_platform",
      "size": 0,
      "mtime": 1795399700.0,
      "ext": "",
      "language": ""
    },
    {
      "name": "README.md",
      "type": "file",
      "path": "README.md",
      "size": 21371,
      "mtime": 1795399816.0,
      "ext": ".md",
      "language": "markdown"
    }
  ],
  "depth": 1
}
```

With `depth > 1` each directory entry gains a `children` array of the same entry shape.
Entries under forbidden directories (`.git`, `node_modules`, `.venv`, `__pycache__`, `venv`)
and forbidden filenames are omitted entirely.

### GET /workspace/repomap

Query: `path` (default `""`), `max_files` (default `400`), `max_bytes` (default `64000000`).

A flat file list with per-file line counts, used for the codebase treemap in the observability
panel.

```json
{
  "path": "",
  "files": [
    {
      "path": "agent_platform/api/app.py",
      "name": "app.py",
      "dir": "agent_platform/api",
      "lines": 4700,
      "size": 229577,
      "ext": ".py"
    }
  ],
  "total_lines": 98123,
  "file_count": 412,
  "truncated": false
}
```

Binary files count as zero lines. `truncated` becomes true when either the file count or the byte
budget is exhausted.

### GET /workspace/file

Query: `path` (required), `start_line`, `end_line` (both optional).

```json
{
  "path": "lz/demo.py",
  "content": "import torch\n",
  "encoding": "utf-8",
  "language": "python",
  "size": 13,
  "mtime": 1795399816.0,
  "truncated": false,
  "line_count": 1,
  "start_line": null,
  "end_line": null
}
```

`encoding` is `"utf-8"` or `"gbk"` — GBK files are decoded transparently. `truncated` is true when
`start_line`/`end_line` selected a subset; `line_count` always reports the full file length.
Errors: `413 WS_TOO_LARGE` for files over `max_file_bytes`, `415 WS_BINARY` for files containing
NUL bytes, `415 WS_INVALID_ENCODING` for neither UTF-8 nor GBK.

### POST /workspace/file

**Stages** a write. Nothing touches the disk.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `path` | `string` | *required* | Path relative to the workspace root. |
| `content` | `string` | `""` | The new full content. |
| `expected_mtime` | `float \| null` | `null` | The mtime the editor saw. A mismatch returns `409 WS_FILE_CHANGED`. |
| `note` | `string` | `""` | A description shown on the approval card. |

Response: **HTTP 201**

```json
{
  "change_id": "ch-7f3a2b91c4d5",
  "path": "lz/demo.py",
  "action": "write",
  "status": "pending",
  "diff": { "added": 4, "removed": 0, "unified": "--- a/lz/demo.py\n+++ b/lz/demo.py\n@@ ..." },
  "old_mtime": 1795399700.0,
  "new_mtime": 1795399816.0,
  "note": "",
  "existed": true
}
```

`status` is `"pending"` — the change is recorded in the undo store but the file on disk is
untouched. `existed` tells you whether this is a new file or an overwrite.

### GET /workspace/search

Query: `q` (the search string, required), `glob` (a filename filter), `max` (default `50`),
`case_sensitive` (default `false`).

```json
{
  "query": "def main",
  "glob": "",
  "hits": [
    { "path": "scripts/run_eval.py", "line": 42, "text": "def main():", "language": "python" }
  ],
  "files": [ { "path": "scripts/run_eval.py", "count": 1, "language": "python" } ],
  "total_hits": 1,
  "scanned_files": 214,
  "skipped_binary": 3,
  "max_files": 5000,
  "timed_out": false,
  "truncated": false
}
```

`hits` is capped at `max`; `total_hits` is the true count across all scanned files, so
`truncated` is true when `total_hits` exceeds the number of returned hits. Binary files are
counted in `skipped_binary` and never searched. An empty `q` returns
`400 WS_INVALID_ARGUMENTS`.

### POST /workspace/apply

Applies a staged change.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `change_id` | `string` | *required* | The staged change. |
| `approved` | `boolean` | `true` | `false` discards instead of applying. |
| `actor` | `string` | `"operator"` | Who is applying. |
| `reason` | `string` | `""` | Why. |

```json
{
  "change_id": "chg-7f3a2b91",
  "path": "lz/demo.py",
  "status": "applied",
  "undo_id": "undo-4c1e8b02",
  "size": 13,
  "added": 4,
  "removed": 0,
  "note": "已落盘；如需回滚请调用 POST /workspace/undo。"
}
```

This is the front-end manual-edit path. Agent-initiated writes go through `POST /approve`. Both
share `Workspace.apply_change`, so the safety semantics are identical: path validation, concurrent
-modification detection, undo stack, atomic write, audit.

### DELETE /workspace/change/{change_id}

Discards a staged change without applying it.

```json
{ "change_id": "chg-7f3a2b91", "status": "discarded" }
```

### POST /workspace/undo

Undoes the most recent write, restoring the previous content.

```json
{
  "status": "undone",
  "path": "lz/demo.py",
  "undo_id": "undo-4c1e8b02",
  "size": 9,
  "added": 0,
  "removed": 4
}
```

When the undo stack is empty the response carries the error code `WS_NO_UNDO`.

### GET /workspace/history

Query: `path` (empty means all files).

```json
{
  "path": "lz/demo.py",
  "entries": [
    { "change_id": "chg-7f3a2b91", "action": "applied", "actor": "operator", "ts": 1795399816.0, "added": 4, "removed": 0 }
  ]
}
```

---

## Models

### GET /models

The merged registry — static `config/routing.yaml` plus dynamic `data/models.json` — with a live
reachability probe per entry.

Query parameter: `probe` (default `true`). When `true`, the endpoint really requests each
endpoint's `/v1/models`, so the status dot in the UI is a measurement rather than an assumption.
When `false`, `reachable` is `null` and no probes run.

```json
{
  "count": 3,
  "models": [
    {
      "name": "gpt-oss-20b",
      "label": "gpt-oss-20b",
      "base_url": "http://127.0.0.1:8080/v1",
      "model": "gpt-oss-20b",
      "description": "",
      "reasoning_effort": "medium",
      "temperature": 0.2,
      "max_tokens": 2048,
      "timeout": 180,
      "supports_tools": true,
      "capabilities": ["code", "tool_call", "reasoning", "chinese"],
      "installed": true,
      "source": "routing.yaml",
      "builtin": true,
      "has_api_key": true,
      "created_at": null,
      "updated_at": null,
      "reachable": true,
      "error": "",
      "latency_ms": 12,
      "server_models": ["gpt-oss-20b"]
    }
  ],
  "store": "E:\\PYPROJECT\\agent\\data\\models.json",
  "probed": true,
  "default": "gpt-oss-20b"
}
```

`api_key` is never echoed back — only `has_api_key`, a boolean. `default` is the first entry of the
`agent` route's fallback chain, which is what the platform uses when no model is specified.

### POST /models

Registers a custom model.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | `string` | *required* | Model name. Letters, digits, underscore, hyphen, dot. |
| `label` | `string` | `""` | Display name. Falls back to `name`. |
| `base_url` | `string` | *required* | OpenAI-compatible endpoint. |
| `model` | `string` | `""` | The model name on the endpoint. Falls back to `name`. |
| `description` | `string` | `""` | Shown to the user. |
| `reasoning_effort` | `string` | `"medium"` | `low`, `medium`, or `high`. |
| `temperature` | `float` | `0.2` | Sampling temperature. |
| `max_tokens` | `int` | `2048` | Maximum tokens per call. |
| `api_key` | `string` | `"local"` | Not echoed back. |
| `timeout` | `float` | `180.0` | Request timeout in seconds. |
| `supports_tools` | `boolean` | `true` | Whether the endpoint supports function calling. |

Response: the created model object.

### PUT /models/{name}

Updates a custom model. Every field is optional; only the supplied ones are overwritten.

### DELETE /models/{name}

Removes a custom model. Built-in models from `routing.yaml` cannot be deleted.

### GET /models/lifecycle

Live runtime state of every known model endpoint. `running` means something is listening on the
port; `reachable` means `/v1/models` actually answered. A model that is still loading shows
`running: true` with `reachable: false` — that is the expected transient state.

```json
{
  "count": 3,
  "models": [
    {
      "alias": "gpt-oss-20b",
      "label": "gpt-oss-20b",
      "port": 8080,
      "base_url": "http://127.0.0.1:8080/v1",
      "device": "cuda",
      "running": true,
      "pid": 18432,
      "started_at": "2026-11-24T02:11:04",
      "managed": true,
      "reachable": true,
      "model_path": "E:\\model\\models\\gpt-oss-20b\\gpt-oss-20b.gguf",
      "ctx_size": 32768,
      "ngl": 99,
      "threads": 8
    }
  ],
  "default": "gpt-oss-20b",
  "note": "start/stop 均为异步操作：返回 status='starting' 后请轮询本接口"
}
```

`managed: false` means the process was started outside this API (for example by
`deploy/start_servers.ps1`) and was merely claimed by PID lookup. The `note` field is intentionally
in Chinese because it is emitted verbatim by the API.

### POST /models/{alias}/start

Starts the llama-server process for an alias.

```json
{ "alias": "gpt-oss-20b", "status": "started", "pid": 18432, "port": 8080 }
```

### POST /models/{alias}/stop

Stops it.

```json
{ "alias": "gpt-oss-20b", "status": "stopped" }
```

### POST /models/start-all

Starts every registered model that is not already running.

### POST /models/stop-all

Stops every running model.

---

## Knowledge base

### GET /kb/documents

Lists indexed documents, aggregated by `doc_id`.

```json
{
  "documents": [
    {
      "doc_id": "DOC-7f3a2b91",
      "title": "T101 测试规格",
      "doc_type": "document",
      "version": "1.0",
      "owner": "team-a",
      "security_level": "internal",
      "product_line": "T-series",
      "visible_roles": ["*"],
      "chunk_count": 128,
      "child_count": 96,
      "parent_count": 32,
      "token_count": 38400,
      "size_bytes": 48213,
      "source": "T101.md",
      "origin": "upload",
      "stored_path": "",
      "updated_at": "2026-11-23 10:00:00",
      "hit_count": 42,
      "last_hit_at": "2026-11-24 09:12:00"
    }
  ]
}
```

`visible_roles` defaults to `["*"]` (everyone). `origin` distinguishes a document imported from a
directory (`directory`) from one uploaded through the UI (`upload`). `child_count` and
`parent_count` reflect the parent-child chunking split.

### POST /kb/documents

Adds a document from inline text.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `text` | `string` | *required* | The document body. May contain YAML front matter. |
| `title` | `string` | `""` | From front matter, or generated. |
| `doc_id` | `string` | `""` | Generated as `DOC-<hash>` when omitted. |
| `doc_type` | `string` | `"document"` | Document type. |
| `version` | `string` | `""` | Version. |
| `owner` | `string` | `""` | Owner. |
| `security_level` | `string` | `""` | `public` / `internal` / `confidential` / `secret`. |
| `visible_roles` | `array` | `[]` | Roles permitted to see it. |
| `product_line` | `string` | `""` | Product line. |

Response: the created document plus chunk statistics.

### POST /kb/documents/upload

`multipart/form-data`. Same metadata fields as above, plus a `file` part.

### DELETE /kb/documents/{doc_id}

Removes the document and its chunks.

```json
{ "doc_id": "DOC-7f3a2b91", "status": "deleted", "chunks_removed": 128 }
```

### POST /kb/documents/{doc_id}/reindex

Re-chunks and re-embeds one document.

```json
{ "doc_id": "DOC-7f3a2b91", "status": "reindexed", "chunks": 134 }
```

### POST /kb/reindex

Rebuilds the entire index.

```json
{ "status": "reindexed", "documents": 1, "chunks": 134 }
```

### GET /kb/search

Runs a retrieval directly against the knowledge base, **without going through the LLM**. It exists
to confirm that an import is immediately searchable, and to verify the ACL filter — a low-clearance
user cannot retrieve a `confidential` document.

Query parameters: `query` (required), `max_security_level` (default `internal`), `roles` (a
comma-separated list), `top_k` (default 6).

```json
{
  "query": "T101_VDD_FUNCT 上下限",
  "max_security_level": "internal",
  "roles": [],
  "hit_count": 2,
  "hits": [
    {
      "chunk_id": "C-7e09476c6c2c2864",
      "doc_id": "DOC-7f3a2b91",
      "doc_title": "T101 测试规格",
      "text": "VDD 下限 3.135，典型值 3.300，上限 3.465，DFT 方法为 Functional，失效 bin 为 2。",
      "rrf_score": 0.0325,
      "rerank_score": 0.8734,
      "source": "T101.md",
      "section": "T101/VDD_FUNCT"
    }
  ],
  "reason": "",
  "stage_trace": [
    { "step": 1, "name": "查询理解/改写", "queries": ["T101_VDD_FUNCT 上下限"] },
    { "step": 2, "name": "检索前ACL过滤", "acl": { "max_security_level": "internal", "roles": [] } },
    { "step": 3, "name": "多路召回", "bm25": 50, "vector": 50 },
    { "step": 4, "name": "RRF融合", "k": 60, "fused": 87 },
    { "step": 5, "name": "去重", "count": 82 },
    { "step": 6, "name": "Reranker精排", "kept": 6 },
    { "step": 7, "name": "上下文组装", "used_tokens": 1840, "budget": 3000 }
  ]
}
```

`stage_trace` is the per-step breakdown of the 10-step pipeline, and it is the fastest way to see
*where* a retrieval went wrong. `rrf_score` is the fused rank score that the abstain threshold
(`0.012`) is applied to; `rerank_score` is the cross-encoder score, which is only present when the
`flag` reranker backend is active.

---

## Custom tools

### GET /custom-tools

Lists every custom tool, including disabled ones.

```json
{
  "count": 1,
  "tools": [
    {
      "name": "http_fetch_report",
      "description": "Pull weekly report data",
      "input_schema": { "type": "object", "properties": { "week": { "type": "string" } } },
      "kind": "http",
      "config": { "url": "http://127.0.0.1:9000/report", "method": "GET", "result_path": "data" },
      "permission": "READ_ONLY",
      "hitl_required": false,
      "enabled": true,
      "created_at": 1795399816.0,
      "updated_at": 1795399816.0,
      "hitl_forced_reason": ""
    }
  ],
  "builtin_tools": ["case_lint", "kb_read", "kb_search", "notify", "report_render", "spec_extract", "sql_query", "tp_lookup", "ws_edit_file", "ws_list_files", "ws_read_file", "ws_search_code", "ws_write_file"],
  "data_file": "E:\\PYPROJECT\\agent\\data\\custom_tools.json"
}
```

`builtin_tools` is the protected name list — a custom tool may never take one of these names.
`hitl_forced_reason` explains why `hitl_required` was escalated (for example
`"permission WRITE_DATA requires approval"`); it is empty when the flag was set explicitly.

### POST /custom-tools

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | `string` | *required* | Letters, digits, underscore. Must not collide with a built-in. |
| `description` | `string` | `""` | What the model sees. |
| `input_schema` | `object` | `{"type":"object","properties":{}}` | JSON Schema. `type` must be `object`. |
| `kind` | `string` | `"http"` | `http` or `script`. |
| `config` | `object` | `{}` | `http`: `url`, `method`, `headers`, `body_template`, `result_path`. `script`: `script_path`, `function_name`, `timeout_s`. |
| `permission` | `string` | `"READ_ONLY"` | One of the five permission levels. |
| `hitl_required` | `boolean` | `false` | Forced to `true` for write, external-side-effect, and admin permissions. |
| `enabled` | `boolean` | `true` | Whether it is active. |

Response: **HTTP 201** with the stored definition. A name collision with a built-in tool returns
**HTTP 409** with code `NAME_CONFLICT`.

### PUT /custom-tools/{name}

Updates a custom tool. If the payload carries a **different** `name`, the old tool is deleted and
a new one is created under the new name — this is how renaming works.

### DELETE /custom-tools/{name}

Removes it and unregisters it from the running tool registry.

### POST /custom-tools/{name}/toggle

Enables or disables it. A disabled tool is unregistered immediately.

```json
{ "name": "http_fetch_report", "enabled": false, "status": "disabled" }
```

### POST /custom-tools/{name}/test

Runs it against sample arguments, without going through the agent.

```json
{ "args": { "week": "2026-W47" } }
```

```json
{
  "ok": true,
  "result": { "total": 12, "items": [] },
  "elapsed_ms": 143,
  "stdout": ""
}
```

---

## Sessions

### GET /sessions

Lists sessions, newest first. The list is merged from the persistent store and the in-memory
session map, so a session appears even if it has not been written to disk yet.

```json
{
  "count": 1,
  "sessions": [
    {
      "thread_id": "th-a1b2c3d4e5f6",
      "session_id": "S-1a2b3c4d",
      "user_id": "",
      "title": "T101_VDD_FUNCT 的上下限是多少？",
      "summary": "本次会话讨论了 T101 的 VDD 限值…",
      "summary_status": "ready",
      "summary_json": { "...": "..." },
      "has_summary": true,
      "has_trace": true,
      "message_count": 8,
      "model": "gpt-oss-20b",
      "mode": "agent",
      "created_at": 1795399816.0,
      "updated_at": 1795399820.0
    }
  ],
  "store": { "backend": "sqlite", "path": "E:\\PYPROJECT\\agent\\data\\sessions.db", "exists": true, "sessions": 12, "messages": 96 }
}
```

`summary_status` is `none`, `pending`, `ready`, or `failed`. `has_summary` is `true` only when the
status is `ready`. `message_count` is the real number of rows in `session_messages`, so it always
matches the length of `GET /sessions/{thread_id}/messages`. `has_trace` is `true` only when the
session has a **non-empty** persisted decision trace on disk — the frontend uses it to decide
whether the observability panel can be restored from storage.

### GET /sessions/{thread_id}

One session's metadata, plus the generated summary if there is one. Returns HTTP 404 if the
session does not exist.

### GET /sessions/{thread_id}/messages

The full message history, in order, from `data/sessions.db` — it survives a restart.

```json
{
  "thread_id": "th-a1b2c3d4e5f6",
  "count": 2,
  "messages": [
    {
      "seq": 1,
      "role": "user",
      "content": "T101_VDD_FUNCT 的上下限是多少？",
      "citations": [],
      "tool_calls": [],
      "ts": 1795399816.0,
      "extra": { "mode": "agent" }
    },
    {
      "seq": 2,
      "role": "assistant",
      "content": "VDD 下限 3.135，典型值 3.300，上限 3.465…",
      "citations": [{ "chunk_id": "C-7e09476c6c2c2864", "doc_id": "DOC-7f3a2b91" }],
      "tool_calls": [],
      "ts": 1795399820.0,
      "extra": { "source": "chat", "stop_reason": "done" }
    }
  ],
  "session": {
    "thread_id": "th-a1b2c3d4e5f6",
    "title": "T101_VDD_FUNCT 的上下限是多少？",
    "summary_status": "none",
    "message_count": 2,
    "created_at": 1795399816.0,
    "updated_at": 1795399820.0
  },
  "notice": "消息历史持久化在 SQLite 中，重启后仍可查看；但 LangGraph checkpointer 是进程内内存态，重启后无法从历史中间断点续跑 ReAct，只能基于历史继续提问。"
}
```

### DELETE /sessions/{thread_id}

Deletes the session and its messages.

### POST /sessions/{thread_id}/summarize

Generates a summary of the session.

```json
{ "thread_id": "th-a1b2c3d4e5f6", "summary": "本次会话讨论了 T101 的 VDD 限值…" }
```

---

## Tasks

### GET /tasks/templates

The available task templates.

```json
{
  "templates": [
    { "id": "rag_qa", "name": "知识库问答", "description": "带引用的问答", "params": {} }
  ]
}
```

### GET /tasks

Lists tasks (in-memory state only — tasks are not persisted across restarts).

```json
{
  "count": 1,
  "tasks": [
    {
      "id": "tsk-7f3a2b91",
      "task_id": "tsk-7f3a2b91",
      "name": "Generate T101 test cases",
      "description": "...",
      "prompt": "...",
      "model": "gpt-oss-20b",
      "mode": "agent",
      "params": { "max_rounds": 20 },
      "status": "pending",
      "created_at": 1795399816.0,
      "started_at": 1795399816.1,
      "finished_at": null,
      "duration_ms": null,
      "tokens_used": 0,
      "tool_calls_used": 0,
      "error": "",
      "result_summary": "",
      "stop_reason": null,
      "thread_id": "th-a1b2c3d4e5f6",
      "session_id": "S-1a2b3c4d"
    }
  ]
}
```

### GET /tasks/{task_id}

One task, in full. Beyond the summary fields above it includes the complete prompt, the complete
final answer, the tool-call trace, the recorded SSE event sequence, the error detail, the audit
links, and the citation lists.

```json
{
  "id": "tsk-7f3a2b91",
  "task_id": "tsk-7f3a2b91",
  "name": "Generate T101 test cases",
  "prompt": "Generate test cases for T101",
  "status": "done",
  "final_answer": "...",
  "tool_trace": [ { "tool": "kb_search", "args": {}, "ok": true } ],
  "sse_events": [ { "event": "done", "data": {} } ],
  "audit": { "thread_id": "th-a1b2c3d4e5f6", "session_id": "S-1a2b3c4d", "user_id": "" },
  "citations": [ { "doc_id": "d-1", "chunk_id": "c-1", "score": 0.31 } ],
  "invalid_citations": [],
  "approvals": [ { "tool": "ws_write_file", "decision": "approved" } ]
}
```

Returns HTTP 404 when the task id is unknown.

### GET /tasks/{task_id}/export

Exports the task result as a downloadable file (Markdown for report-style output).

### POST /tasks

Creates a task.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | `string` | *required* | Task name. |
| `description` | `string` | `""` | Description. |
| `prompt` | `string \| null` | `null` | The prompt to execute. Falls back to `description`. |
| `model` | `string` | `"gpt-oss-20b"` | Target model. |
| `params` | `object` | `{}` | `max_rounds`, `temperature`, `reasoning_effort`, `max_tokens`. |
| `mode` | `string \| null` | `null` | `agent`, `ask`, or `plan`. |

---

## Platform

### GET /platform/status

Platform-wide status. This is the polling target for the desktop app's "one-click launch" flow,
and it is deliberately cheap: no knowledge-base directory scan, no route rebuild, and port status
comes from a single `psutil` traversal (3-16 ms) rather than a `socket.connect_ex()` per port
(500 ms per dead port on Windows). Measured at roughly 30 ms.

```json
{
  "api": { "reachable": true, "base_url": "http://127.0.0.1:8010", "port": 8010 },
  "models": [
    {
      "alias": "gpt-oss-20b",
      "label": "gpt-oss-20b",
      "running": true,
      "reachable": true,
      "device": "gpu",
      "port": 8080,
      "pid": 18432
    }
  ],
  "kb": { "ready": true, "state": "ready" },
  "summary": "平台就绪",
  "one_click_ready": true,
  "default": "gpt-oss-20b"
}
```

`summary` is one of `API 未启动` (no models registered), `缺少可用的模型服务` (none reachable),
`模型加载中` (some running but not yet reachable), `平台就绪（知识库模型预热中）`, or `平台就绪`.

### POST /platform/launch

Launches platform-managed components.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `models` | `array` | `[]` | Empty means the platform default (currently `gpt-oss-20b`). A non-empty list means "start the listed ones and stop the ones not listed". |

---

## Tools

### GET /tools

The tool catalog.

```json
{
  "tools": [
    {
      "name": "ws_write_file",
      "description": "整体覆盖写入工作区里的一个文件…",
      "permission": "WRITE_DATA",
      "hitl_required": true,
      "timeout_ms": 60000,
      "max_retries": 0,
      "version": "1.0.0",
      "idempotent": false,
      "input_schema": { "type": "object", "required": ["path"], "properties": {} },
      "output_schema": {},
      "metadata": {}
    }
  ],
  "total": 13
}
```

### GET /tools/{tool_name}

One tool's full specification, including the complete input schema.

---

## Diagnostics

### GET /ping

The fast liveness probe. Touches no model, no database, and no index.

```json
{ "status": "ok", "ts": 1795399816.0 }
```

Use this, not `/health`, for a readiness loop with a short timeout. The desktop shell probes `/ping`
with a 3 s timeout; probing `/health` caused spurious "API unreachable" exits when a model endpoint
was busy generating.

### GET /health

The deep health check: model endpoints, tool registry, knowledge base, and data directory.

```json
{
  "status": "ok",
  "prompt_version": "p_v1.2",
  "llm": { "reachable": true, "error": "" },
  "registry": {
    "tool_count": 13,
    "tools": ["case_lint", "kb_read", "kb_search", "notify", "report_render", "spec_extract", "sql_query", "tp_lookup", "ws_edit_file", "ws_list_files", "ws_read_file", "ws_search_code", "ws_write_file"],
    "hitl_tools": ["notify", "report_render", "ws_edit_file", "ws_write_file"],
    "builtin_tools": ["case_lint", "kb_read", "kb_search", "notify", "report_render", "spec_extract", "sql_query", "tp_lookup", "ws_edit_file", "ws_list_files", "ws_read_file", "ws_search_code", "ws_write_file"],
    "custom_tools": [],
    "custom_tool_count": 0
  },
  "audit": { "mode": "sqlite", "path": "E:\\PYPROJECT\\agent\\data\\audit\\audit.db" },
  "graph": { "built": true, "checkpointer": "MemorySaver", "checkpointer_shared": true, "graphs_built": 1 },
  "endpoints": [
    { "model": "gpt-oss-20b", "label": "gpt-oss-20b", "base_url": "http://127.0.0.1:8080/v1", "reachable": true, "error": "" }
  ],
  "sessions": 12,
  "tasks": 0,
  "models": { "count": 3, "store": "E:\\PYPROJECT\\agent\\data\\models.json" },
  "routing": { "violations": [], "routes": [] },
  "sessions_store": { "path": "E:\\PYPROJECT\\agent\\data\\sessions.db" },
  "kb_warmup": {
    "state": "ready",
    "ready": true,
    "elapsed_s": 33.12,
    "error": "",
    "embedder_cached": true,
    "reranker_cached": true
  },
  "kb_runtime": { "torch_threads": 4, "infer_threads": 8, "rerank_max_length": 512, "rerank_shortlist": 0 },
  "data_dir": { "dir": "E:\\PYPROJECT\\agent\\data", "exists": true, "external": false, "source": "default" },
  "modes": [
    { "id": "agent", "label": "Agent", "description": "完整 ReAct 循环，可调用任意工具（写操作需人工确认）", "allowed_permissions": ["READ_ONLY", "COMPUTE", "WRITE_DATA", "EXTERNAL_SIDE_EFFECT"] },
    { "id": "ask", "label": "Ask", "description": "只读问答：仅允许 READ_ONLY / COMPUTE 工具，最多 1-2 轮调用", "allowed_permissions": ["READ_ONLY", "COMPUTE"] },
    { "id": "plan", "label": "Plan", "description": "只规划不执行：产出结构化执行计划，不调用任何工具", "allowed_permissions": [] }
  ]
}
```

`routing.violations` being non-empty means there is a configuration error that would let a code task
silently degrade to a model without code capability. See
[`docs/06_代码模型接入策略.md`](06_代码模型接入策略.md).

### GET /metrics

Aggregate counters.

```json
{
  "requests_total": 42,
  "chat_total": 38,
  "approve_total": 4,
  "tasks_total": 3,
  "tokens_total": 512340,
  "tool_calls_total": 128,
  "errors_total": 0,
  "total_duration_ms": 184320,
  "avg_duration_ms": 4388.6,
  "sessions": 12,
  "tasks": 3,
  "uptime_seconds": 3601.2
}
```

`avg_duration_ms` is `total_duration_ms / requests_total`, or `0.0` when there have been no
requests. `uptime_seconds` is measured from the moment the app object was created.

### GET /audit

Recent audit entries. Read-only: there is no update or delete entry point anywhere in the API.

Query parameters: `limit` (1-500, default 50), `session_id`, `thread_id`, `action`. Only
`session_id` is applied as a database-level filter; `thread_id` and `action` are applied
client-side after the read.

```json
{
  "count": 1,
  "mode": "sqlite",
  "records": [
    {
      "audit_id": "AUD-20261124-0A3F91",
      "ts": "2026-11-24T02:10:20.123456+00:00",
      "session_id": "S-1a2b3c4d",
      "thread_id": "th-a1b2c3d4e5f6",
      "user_id": "",
      "actor": "operator",
      "action": "hitl_decision",
      "target": "ws_write_file",
      "args": { "path": "lz/demo.py" },
      "result": { "approved": true },
      "status": "ok",
      "duration_ms": 0,
      "model": "gpt-oss-20b",
      "prompt_hash": "9f2c...",
      "output_hash": "41ab...",
      "extra": null
    }
  ]
}
```

`ts` is an ISO-8601 timestamp, not a Unix epoch. `status` defaults to `"ok"` and is also used for
values like `"pending_approval"`, `"success"`, and `"error"`. `prompt_hash` and `output_hash` are
content hashes of `args` and `result`, auto-filled when not supplied, so a record can be matched
by content.

`mode` is `postgres`, `sqlite`, `jsonl`, or `none`, matching the audit backend that is actually in
use. The `args`, `result`, and `extra` fields are parsed back from their stored JSON strings.

The action names are `tool_call` for every tool execution, `hitl_decision` for approval decisions,
and `workspace_*` for workspace writes (including `workspace_change_staged`).

### GET /

Redirects to `/ui/`.

---

## Endpoint index

53 endpoints.

### Chat and HITL (2)

| Method | Path |
| --- | --- |
| `POST` | `/chat` |
| `POST` | `/approve` |

### Workspace (10)

| Method | Path |
| --- | --- |
| `GET` | `/workspace/info` |
| `GET` | `/workspace/tree` |
| `GET` | `/workspace/repomap` |
| `GET` | `/workspace/file` |
| `POST` | `/workspace/file` |
| `GET` | `/workspace/search` |
| `POST` | `/workspace/apply` |
| `DELETE` | `/workspace/change/{change_id}` |
| `POST` | `/workspace/undo` |
| `GET` | `/workspace/history` |

### Models (9)

| Method | Path |
| --- | --- |
| `GET` | `/models` |
| `POST` | `/models` |
| `PUT` | `/models/{name}` |
| `DELETE` | `/models/{name}` |
| `GET` | `/models/lifecycle` |
| `POST` | `/models/{alias}/start` |
| `POST` | `/models/{alias}/stop` |
| `POST` | `/models/start-all` |
| `POST` | `/models/stop-all` |

### Knowledge base (7)

| Method | Path |
| --- | --- |
| `GET` | `/kb/documents` |
| `POST` | `/kb/documents` |
| `POST` | `/kb/documents/upload` |
| `DELETE` | `/kb/documents/{doc_id}` |
| `POST` | `/kb/documents/{doc_id}/reindex` |
| `POST` | `/kb/reindex` |
| `GET` | `/kb/search` |

### Custom tools (6)

| Method | Path |
| --- | --- |
| `GET` | `/custom-tools` |
| `POST` | `/custom-tools` |
| `PUT` | `/custom-tools/{name}` |
| `DELETE` | `/custom-tools/{name}` |
| `POST` | `/custom-tools/{name}/toggle` |
| `POST` | `/custom-tools/{name}/test` |

### Sessions (5)

| Method | Path |
| --- | --- |
| `GET` | `/sessions` |
| `GET` | `/sessions/{thread_id}` |
| `GET` | `/sessions/{thread_id}/messages` |
| `DELETE` | `/sessions/{thread_id}` |
| `POST` | `/sessions/{thread_id}/summarize` |

### Tasks (5)

| Method | Path |
| --- | --- |
| `GET` | `/tasks/templates` |
| `GET` | `/tasks` |
| `GET` | `/tasks/{task_id}` |
| `GET` | `/tasks/{task_id}/export` |
| `POST` | `/tasks` |

### Platform (2)

| Method | Path |
| --- | --- |
| `GET` | `/platform/status` |
| `POST` | `/platform/launch` |

### Tools (2)

| Method | Path |
| --- | --- |
| `GET` | `/tools` |
| `GET` | `/tools/{tool_name}` |

### Diagnostics (5)

| Method | Path |
| --- | --- |
| `GET` | `/ping` |
| `GET` | `/health` |
| `GET` | `/metrics` |
| `GET` | `/audit` |
| `GET` | `/` |
