# Architecture

Deep dive into how the Agent Platform is put together: the components, what happens during a
request, how the LangGraph state machine works, and where state lives.

This document assumes you have read the [README](../README.md) for the feature overview.

---

## Table of contents

- [Component diagram](#component-diagram)
- [Request lifecycle](#request-lifecycle)
  - [A normal `/chat` turn](#a-normal-chat-turn)
  - [A HITL round trip](#a-hitl-round-trip)
  - [A plan-mode turn](#a-plan-mode-turn)
- [LangGraph reference](#langgraph-reference)
  - [Nodes](#nodes)
  - [Edges and routing](#edges-and-routing)
  - [AgentState](#agentstate)
- [Budget accounting](#budget-accounting)
- [Tool execution pipeline](#tool-execution-pipeline)
- [Knowledge retrieval pipeline](#knowledge-retrieval-pipeline)
- [Data flow and storage](#data-flow-and-storage)
- [Model routing](#model-routing)
- [Frontend architecture](#frontend-architecture)
- [Design decisions](#design-decisions)

---

## Component diagram

```
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│                                    CLIENTS                                                 │
│                                                                                          │
│   ┌──────────────────────┐            ┌──────────────────────────────┐                   │
│   │  Browser tab         │            │  pywebview desktop window    │                   │
│   │  (any modern browser)│            │  (WebView2 / Edge runtime)   │                   │
│   └──────────┬───────────┘            └──────────────┬───────────────┘                   │
└──────────────┼────────────────────────────────────────┼─────────────────────────────────┘
               │  HTTP/1.1 + SSE                        │
               ▼                                        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│                        agent_platform/api/app.py   (FastAPI, single module)               │
│                                                                                          │
│  ┌────────────────┐  ┌────────────────┐  ┌─────────────────┐  ┌──────────────────────┐   │
│  │ Chat + HITL    │  │ Workspace      │  │ Knowledge base  │  │ Models / Lifecycle   │   │
│  │ /chat (SSE)    │  │ /workspace/*   │  │ /kb/*           │  │ /models/*            │   │
│  │ /approve       │  │ 10 endpoints   │  │ 7 endpoints     │  │ 9 endpoints          │   │
│  └───────┬────────┘  └────────┬───────┘  └────────┬────────┘  └──────────┬───────────┘   │
│          │                    │                   │                     │               │
│          │              ┌─────▼─────┐      ┌──────▼──────┐      ┌───────▼────────┐      │
│  ┌───────▼────────────────────────▼──┐   │  knowledge/  │      │ model_registry │      │
│  │   orchestration/graph.py           │   │  (below)     │      │ model_lifecycle│      │
│  │   LangGraph StateGraph             │   └──────────────┘      └────────────────┘      │
│  │   agent / tools / hitl / plan      │                                                 │
│  └───────┬────────────────────────┬───┘                                                 │
│          │                        │                                                     │
│  ┌───────▼──────────┐   ┌─────────▼──────────┐                                          │
│  │ core/tool_       │   │ core/llm_client    │                                          │
│  │   registry       │   │ core/model_registry│                                          │
│  │ 13 built-in +    │   │ capability routing │                                          │
│  │ N custom tools   │   └─────────┬──────────┘                                          │
│  └───────┬──────────┘             │                                                     │
│          │                        │                                                     │
│  ┌───────▼──────────┐             │                                                     │
│  │ core/workspace   │             │                                                     │
│  │ staged writes    │             │                                                     │
│  │ undo store       │             │                                                     │
│  └───────┬──────────┘             │                                                     │
└──────────┼────────────────────────┼─────────────────────────────────────────────────────┘
           │                        │
           ▼                        ▼
┌────────────────────────┐   ┌────────────────────────────────────────────┐
│  KNOWLEDGE LAYER       │   │  INFERENCE LAYER (llama.cpp llama-server)  │
│                        │   │                                            │
│  chunker.py            │   │  :8080  gpt-oss-20b  (MXFP4, -ngl 99)    │
│   parent  ~1200 tok    │   │         GPU full offload                  │
│   child    ~300 tok    │   │         ReAct loop, tool calls, code     │
│  embed.py              │   │                                            │
│   bge-m3, dim 1024     │   │  :8081  Qwen2.5-14B    (-ngl 0, CPU)     │
│   fallback: llama,hash │   │         Chinese generation, eval, judge  │
│  reranker.py           │   │                                            │
│   bge-reranker-v2-m3   │   │  :8082  qwen2.5-coder-14b (placeholder)   │
│   fallback: cosine     │   │         code-specialised, not installed   │
│  retriever.py          │   └────────────────────────────────────────────┘
│   10-step pipeline     │
│  store.py              │
│   SQLite | pgvector    │
│  threading_limits.py   │
│   idle 4 / infer 8     │
└───────────┬────────────┘
            │
            ▼
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│                             DATA DIRECTORY  (AGENT_DATA_DIR)                              │
│                                                                                          │
│  sessions.db   workspace.db   audit/audit.db   audit/audit.jsonl   kb/chunks.db          │
│  models.json   custom_tools.json   docs/   reports/   outbox/   logs/   shots/           │
│  eval_reports/   _regress/   kb/uploads/   kb/chunk_manifest.json                          │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## Request lifecycle

### A normal `/chat` turn

```
Client                    API                          LangGraph                    LLM
  │                        │                               │                          │
  │── POST /chat ─────────▶│                               │                          │
  │  {message, mode,       │── ensure_graph() ───────────▶│                          │
  │   thread_id,           │   register_thread_graph()    │                          │
  │   max_rounds}          │   new_budget()               │                          │
  │                        │── graph.astream() ──────────▶│                          │
  │                        │                               │── agent node ────────────▶│
  │                        │                               │   chat_sync(prompt,      │
  │                        │                               │     tools, budget)       │
  │                        │                               │◀── response ─────────────│
  │                        │                               │   charge_round() → 1     │
  │                        │                               │   write rounds,          │
  │                        │                               │     llm_duration_ms      │
  │                        │◀── SSE: start ───────────────┤                          │
  │◀── SSE: start ────────│                               │                          │
  │                        │                               │── tools node ───────────▶│
  │                        │                               │   permission check       │
  │                        │                               │   execute kb_search       │
  │                        │◀── SSE: step ───────────────┤   truncate result        │
  │◀── SSE: step ─────────│                               │   audit                  │
  │                        │                               │── agent node ────────────▶│
  │                        │                               │   ...                    │
  │                        │◀── SSE: result + [DONE] ─────┤                          │
  │◀── SSE: result ────────│                               │                          │
  │                        │── persist assistant message ──│                          │
  │                        │── record_metrics()            │                          │
```

Key points:

- The API registers the graph instance under the `thread_id` before streaming, so a later
  `/approve` can resume the exact same graph with the same `max_rounds`, model, and mode.
- `rounds` is written back to graph state by the agent node after each LLM call. This is the
  authoritative count; the trace-length fallback is only used when state has not arrived yet.
- `tokens_used` and `tool_calls_used` are accumulated on the `BudgetGuard`, which is created fresh
  per request.

### A HITL round trip

```
Client                API                       LangGraph                   Tool
  │                    │                            │                         │
  │── POST /chat ─────▶│                            │                         │
  │                    │── astream() ──────────────▶│                         │
  │                    │                            │── agent node ──────────▶│
  │                    │                            │   LLM requests          │
  │                    │                            │      ws_write_file      │
  │                    │                            │◀── tool_call ───────────│
  │                    │                            │   charge_round() → 1    │
  │                    │                            │── tools node ──────────▶│
  │                    │                            │   permission check      │
  │                    │                            │   needs_hitl → True     │
  │                    │                            │   build PendingApproval │
  │                    │                            │   gate.require() ───────┤
  │                    │                            │      interrupt() ↑       │
  │                    │◀── SSE: approval ──────────┤                         │
  │◀── SSE: approval ───│   (pending_approval)      │                         │
  │                    │                            │   ┌──── SUSPENDED ────┐  │
  │                    │                            │   │  graph halted at  │  │
  │                    │                            │   │  the interrupt   │  │
  │                    │                            │   └──────────────────┘  │
  │                    │                            │                         │
  │── POST /approve ───▶│                            │                         │
  │  {thread_id,        │── thread_graph(thread_id)  │                         │
  │   tool_call_id,     │── graph.invoke(            │                         │
  │   approved, actor,  │     Command(resume=...)) ──▶│                         │
  │   session_grant,    │                            │── hitl node ───────────▶│
  │   summarize}        │                            │   resolve decision      │
  │                    │                            │   audit decision        │
  │                    │                            │── tools node ──────────▶│
  │                    │                            │   execute write ────────┤
  │                    │                            │   truncate, audit       │
  │                    │                            │   (fast path: no 2nd    │
  │                    │                            │    LLM call)            │
  │                    │                            │── agent node (skipped   │
  │                    │                            │   on fast path)         │
  │                    │◀── final state ────────────┤                         │
  │                    │── _build_response()        │                         │
  │                    │── _build_trace_from_state()│                         │
  │                    │── JSONResponse             │                         │
  │◀── 200 OK ──────────│   {status, answer, trace,  │                         │
  │                    │    budget, fast_path}       │                         │
```

The split between `tools` and `hitl` is what makes this safe. LangGraph re-runs an interrupted node
from the top on resume. If "approve then write" were one node, the resume would execute the write
twice. Instead:

- The `tools` node detects the approval requirement and raises the interrupt. On resume it re-runs,
  but this time it finds the cached decision and proceeds to execute exactly once.
- The `hitl` node only resolves the decision and audits it. It never touches the filesystem.

**Fast path.** When the pending approval is exactly one tool call, and that tool is on the
fast-path whitelist (a write whose result is already complete and self-describing), the graph skips
the trailing LLM call entirely. The tool result becomes the final answer. `fast_path: true` in the
response tells the client this happened.

**Rejected approval.** `approved: false` sets `stop_reason` to `rejected_by_human` and the graph
ends without executing the tool.

### A plan-mode turn

```
Client                API                       LangGraph                   LLM
  │                    │                            │                         │
  │── POST /chat ─────▶│── ensure_graph(mode=plan) ▶│                         │
  │  {mode: "plan"}    │                            │── plan node ───────────▶│
  │                    │                            │   structured JSON out   │
  │                    │                            │◀── plan ────────────────│
  │                    │◀── SSE: plan ──────────────┤                         │
  │◀── SSE: plan ──────│                            │                         │
  │                    │◀── SSE: result + [DONE] ───┤                         │
```

In plan mode the graph is built with `START -> plan -> END`. The `tools` node is not connected at
all, so a tool call is structurally impossible rather than merely discouraged.

---

## LangGraph reference

Source: `agent_platform/orchestration/graph.py`.

### Nodes

| Node | Definition line | Responsibility |
| --- | --- | --- |
| `agent` | 639 | Calls the LLM with the accumulated message history and the tool schemas. Charges a round. Writes `rounds`, `llm_duration_ms`, `llm_started_at`, `llm_ended_at`, and `reasoning_summary` back to state. Emits the requested tool calls. |
| `tools` | 968 | Checks each requested tool against its permission, decides whether HITL is needed, executes approved and automatic tools, truncates results to `max_tokens_per_tool_result`, records audit entries, and counts tool calls. |
| `hitl` | 1528 | Resolves a pending approval decision, records it in the audit log, and manages session-level grants. Raises the interrupt when no decision exists yet. |
| `plan` | 1748 | Produces a structured plan (JSON with `goal`, `steps`, `risks`, `questions`). Never calls a tool. |

The graph is built by `build_graph(...)`, which takes the LLM client, the tool registry, a
`BudgetGuard`, an optional `HITLGate`, the mode, and an optional checkpointer.

### Edges and routing

```
if mode == "plan":
    START -> plan -> END
else:
    START -> agent
    agent --_route_after_agent--> tools | END
    tools --_route_after_tools--> hitl | agent | END
    hitl -> tools            # hard edge
```

**`_route_after_agent`** inspects `stop_reason` first. If a terminal reason is already set, it ends
the graph. Otherwise, if the assistant message carries tool calls, it routes to `tools`; if not, it
ends the graph with the answer.

**`_route_after_tools`** decides among three destinations:

- `hitl` — there is a pending approval that needs a human.
- `agent` — more work to do; feed the tool results back to the model.
- `END` — a terminal condition is reached (budget exhausted, rejected by the human, the fast path
  produced a final answer).

### AgentState

Defined as a `TypedDict` with `total=False` at `graph.py:135`.

| Field | Type | Meaning |
| --- | --- | --- |
| `messages` | `Annotated[List[Dict], add_messages]` | Conversation and tool trace. The `add_messages` reducer appends and dedupes. |
| `approvals` | `Dict[str, Any]` | `{tool_call_id: ApprovalDecision.to_dict()}`. |
| `pending_hitl` | `List[Dict[str, Any]]` | The pending `PendingApproval` entries. |
| `tokens_used` | `int` | Cumulative tokens consumed. |
| `tool_calls_used` | `int` | Cumulative tool executions. Approval waits do not count. |
| `rounds` | `int` | Completed ReAct rounds. One LLM call is one round. Written by the agent node. |
| `stop_reason` | `Optional[str]` | Why the loop stopped. `done` / `empty_response` / `pending_approval` / `rejected_by_human` / `repeated_failure` / `plan_ready` / `exception` / `llm_error:<EXC>` / `budget_exceeded:<REASON>`. The `budget_exceeded` form always carries the budget reason after the colon — `MAX_TOKENS`, `MAX_ROUNDS`, `MAX_CONTEXT`, `MAX_TOOL_CALLS`, `MAX_SECONDS`. |
| `final_answer` | `Optional[str]` | The answer text. |
| `failure_counts` | `Dict[str, int]` | `{tool_name::args_hash: consecutive_failures}` — the same tool with the same arguments failing repeatedly terminates the loop. |
| `citations` | `List[Dict[str, str]]` | Citations extracted from the final answer. |
| `invalid_citations` | `List[Dict[str, str]]` | Citations that point at chunks that do not exist. |
| `session_id` | `Optional[str]` | Session identifier. |
| `thread_id` | `Optional[str]` | Graph thread identifier (the checkpoint key). |
| `user_id` | `Optional[str]` | Acting user. |
| `mode` | `Optional[str]` | `agent` / `ask` / `plan`. |
| `plan` | `Optional[Dict[str, Any]]` | Structured plan, plan mode only. |
| `ask_rounds` | `int` | Tool rounds consumed in ask mode. |
| `retrieval_needed` | `bool` | Whether this turn should use the knowledge base. |
| `retrieval_rule` | `str` | The rule that decided (e.g. `identity_question`, `force_factual_signal`). |
| `retrieval_reason` | `str` | Human-readable reason; travels to the client. |
| `retrieval_policy_version` | `str` | Policy version, e.g. `rp_v1.0`. |
| `tool_call_counts` | `Dict[str, int]` | Per-tool call counts within the current turn. Reset per request. |
| `query_counts` | `Dict[str, int]` | Per-query counts within the current turn. Reset per request. |
| `reasoning_summary` | `str` | The first one or two sentences of the model's reasoning. The raw chain of thought never enters state or history. |
| `approved_result_ready` | `bool` | The fast path fired: the tool result is the answer. |
| `approved_result` | `Dict[str, Any]` | The full tool result on the fast path. |
| `approved_result_tool` | `str` | The tool that produced it. |
| `pending_hitl_count` | `int` | How many items were pending when the approval was raised. |
| `pending_hitl_tools` | `List[str]` | Their tool names, in `pending_hitl` order. |
| `llm_duration_ms` | `int` | Real duration of the last LLM call. |
| `llm_started_at` | `float` | Unix seconds when the LLM call started. |
| `llm_ended_at` | `float` | Unix seconds when it ended. |

**Why `rounds` had to become a real field.** `BudgetGuard.rounds` lived only in the closure object
created per request and was never written back. The API therefore had no way to read the true count
and fell back to `len(trace)`, which produced displays like "9/8" — nine trace entries is not nine
rounds, because the trace mixes LLM entries, tool entries, and approval entries. The agent node now
writes the real value, and the fallback counts only `kind == "llm"` entries.

**Why `tool_call_counts` and `query_counts` are reset per request.** They are state fields, and the
checkpointer persists them per `thread_id`. Semantics are "do not repeat the same thing *within
this turn*" (system prompt rule 9), not "within the session". Without an explicit reset, asking the
agent to write three files made every subsequent request's `ws_write_file` fail with
`DUPLICATE_TOOL_CALL` — the model would just explain that rule 9 blocked it. The `/chat` handler
now resets both.

---

## Budget accounting

Source: `agent_platform/core/budget.py`.

### Limits

| Field | Default | Overridden by |
| --- | --- | --- |
| `max_rounds` | `8` | `AGENT_MAX_ROUNDS` (the API's `DEFAULT_MAX_ROUNDS` is `20`); per-request `max_rounds` on `/chat` (range 1-64) |
| `max_tokens` | `60_000` | Not environment-configurable |
| `max_tool_calls` | `24` | Not environment-configurable |
| `max_seconds` | `900.0` | Not environment-configurable |
| `max_tokens_per_tool_result` | `4_000` | Not environment-configurable |
| `context_window` | `0` (disabled) | The API sets `32768` from `LLM_CONTEXT_WINDOW` |

The dataclass default of `8` and the API default of `20` are two separate knobs. For `/chat`, the
API value is what is used.

### Reasons

`BudgetExceeded` carries a `reason` string and a human-readable `detail`:

| Reason | Raised when |
| --- | --- |
| `MAX_ROUNDS` | `rounds >= max_rounds` at the agent node. |
| `MAX_TOKENS` | Projected or actual token usage exceeds `max_tokens`. |
| `MAX_CONTEXT` | A single request's projected token count exceeds `context_window`. |
| `MAX_TOOL_CALLS` | Tool executions reach `max_tool_calls`. |
| `MAX_SECONDS` | Elapsed wall-clock time exceeds `max_seconds`. |

`MAX_CONTEXT` exists because the session budget and the per-request context window are different
things. A session can be at 20k tokens while a single request still exceeds the llama-server's
`-c` limit, because the system prompt plus the tool schemas plus a few large tool results can add
up on their own. Checking before the call lets the turn end gracefully with
`budget_exceeded:MAX_CONTEXT` and a final answer, instead of raising a `BadRequestError` that turns
the whole request into an exception.

### `check()` versus `check_no_rounds()`

| Method | Checks rounds | Used by |
| --- | --- | --- |
| `check(est_tokens)` | Yes | The agent node, before each LLM call. |
| `check_no_rounds(est_tokens)` | No | The tools node. |

The tools node originally called `check()`. Since rounds are only charged by the agent node, one
agent-to-tools round trip tested the round limit twice — 8 rounds of budget yielded 4 usable rounds,
and users reported "budget exhausted" after only a few steps. `check_no_rounds()` checks token
budget, context window, tool-call count, and elapsed time, but not rounds.

### Token estimation

`estimate_tokens(text)` is a heuristic calibrated against a real endpoint, not a guess:

- CJK characters (Han, kana, Hangul, and their punctuation ranges): **0.70 token per character**.
- Other non-whitespace ASCII: **4 characters per token**.
- Whitespace: not counted.
- Every message adds a fixed 4-token overhead for the role and separators.

The 0.70 coefficient came from measurement: a 2000-character Chinese prompt returned
`usage.prompt_tokens = 1400`, so exactly 0.7000 token per character. The original implementation
used 1.0, which **over**-estimated by about 43% and wasted roughly 9k tokens of a 32k window — enough
for a 200-line commented class.

`estimate_messages_tokens` normalises non-dict messages first. LangChain's `ToolMessage.content` may
be a list of content blocks; calling `str()` on it produces an escaped repr and a badly skewed
estimate.

---

## Tool execution pipeline

```
   LLM requests a tool call
            │
            ▼
   ┌────────────────────┐
   │ 1. Registry lookup │  ToolRegistry.get(name)
   │                    │  Unknown name → structured error to the model
   └─────────┬──────────┘
             ▼
   ┌────────────────────┐
   │ 2. Schema check    │  validate_args(input_schema, args)
   │                    │  Missing required, wrong types, bad enums,
   │                    │  unexpected properties → error to the model
   └─────────┬──────────┘
             ▼
   ┌────────────────────┐
   │ 3. Permission gate │  ToolSpec.permission
   │                    │  READ_ONLY / COMPUTE → run
   │                    │  WRITE_DATA / EXTERNAL_SIDE_EFFECT / ADMIN → HITL
   └─────────┬──────────┘
             │ needs_hitl and no session grant
             ▼
   ┌────────────────────┐
   │ 4. HITL            │  HITLGate.require(pending)
   │                    │  Decision cached? → use it
   │                    │  Session grant?  → auto-approve
   │                    │  Otherwise       → interrupt()
   └─────────┬──────────┘
             │ approved
             ▼
   ┌────────────────────┐
   │ 5. Execute         │  impl(args, ctx)
   │                    │  Workspace writes stage a change_id first,
   │                    │  then apply atomically.
   └─────────┬──────────┘
             ▼
   ┌────────────────────┐
   │ 6. Truncate        │  Result clamped to max_tokens_per_tool_result
   │                    │  (4000) before it enters the message history
   └─────────┬──────────┘
             ▼
   ┌────────────────────┐
   │ 7. Audit           │  Tool name, arguments, result, duration, actor
   │                    │  Append-only
   └─────────┬──────────┘
             ▼
   ┌────────────────────┐
   │ 8. Loop guard      │  Same tool + same arguments failing twice in a row
   │                    │  → terminate (failure_counts)
   │                    │  Write tools counted per path, not per tool name
   └────────────────────┘
```

**Write tools counted per path.** `MAX_SAME_TOOL_CALLS` means "do not repeat the same thing in this
turn". Counting by tool name was wrong for writes: asking the agent to write `demo.py` and
`tutorial.md` hit the limit on the third file, and the code silently did not get written. Write tools
(`ws_write_file`, `ws_edit_file`) are therefore counted by **target path** — writing two different
files is not repetition.

**Errors are normalised.** Every tool error reaches the model as
`{code, message, retryable, hint}`, so the model can decide whether to retry or change approach
rather than seeing a Python traceback.

---

## Knowledge retrieval pipeline

Source: `agent_platform/knowledge/retriever.py`.

```
query
  │
  ▼
① Query understanding / rewrite
   extract_slots(query) → entity / model / time / product-line slots
   _rewrite_queries()   → 2-3 rewritten queries
   (rule channel always available; small-model channel supplements)
  │
  ▼
② Pre-retrieval ACL and confidentiality filter
   ctx.to_acl_filter() → pushed down into the store
   Unauthorised chunks never enter the candidate pool
  │
  ▼
③ Dual recall
   BM25 (lexical)  ─┐
                    ├─→ two pools
   dense (vector)  ─┘
  │
  ▼
④ RRF fusion   score = Σ_i 1/(60 + rank_i)
  │
  ▼
⑤ Dedup + top-50
   by chunk_id and content hash
  │
  ▼
⑥ Cross-encoder rerank  (bge-reranker-v2-m3) → top-6
  │
  ▼
⑦ Context assembly   ≤ 3000 tokens; over-budget chunks dropped
  │
  ▼
⑧ Answer generation with inline citations
  │
  ▼
⑨ Citation validation
   chunk existence checked; bad ones collected in invalid_citations
  │
  ▼
⑩ Abstain threshold
   top RRF score < 0.012 → "not found in the knowledge base"
```

### Constants

| Constant | Value |
| --- | --- |
| `RRF_K` | 60 |
| `RECALL_N` | 50 |
| `FINAL_TOP_K` | 6 |
| `RETRIEVAL_TOKEN_BUDGET` | 3000 |
| `MIN_SCORE_THRESHOLD` | 0.012 |

The threshold is on the **RRF score**, not on the reranker's logits, because RRF has a stable
magnitude (`Σ 1/(60+rank)`): a dual-branch top-1 hit is about 0.033, a single-branch hit about
0.016. `0.012` corresponds to a weak single-branch top-3 hit. Cross-encoder logits can be negative,
which would make an abstention threshold on them unreliable.

### Degradation

| Backend order | Embedder | Reranker |
| --- | --- | --- |
| 1 | `FlagEmbedding` bge-m3 (CPU) | `FlagReranker` bge-reranker-v2-m3 (CPU) |
| 2 | llama-server `/embedding` or `/v1/embeddings` | embedding cosine similarity |
| 3 | hash pseudo-vectors | (same as 2) |

The hash fallback is deterministic: the same text always yields the same vector, and shared
vocabulary produces some similarity signal. It is not semantic similarity, and the embedder logs a
warning when it activates.

### Chunking

`knowledge/chunker.py` uses parent-child chunking:

- **Child** — target ~300 tokens, overlap 80, minimum 50. This is the retrieval unit.
- **Parent** — target ~1200 tokens, minimum 200. This is the refill unit. A hit child pulls in its
  parent for the full context.

Splitting prefers markdown heading levels and PDF paragraph boundaries. The heading path is stored
as `metadata.title_path`.

---

## Data flow and storage

### Data directory resolution

`agent_platform/core/paths.py` is the single resolver. Priority, highest first:

1. An explicit `override` argument — for tests and scripts.
2. The `AGENT_DATA_DIR` environment variable — the user-facing entry point.
3. `<repo_root>/data` — the default, byte-for-byte identical to the pre-refactor layout.

Paths are expanded (`expanduser`) and resolved (absolute), with an in-process cache to avoid repeated
`resolve()` calls on Windows. `reset_cache()` clears it when a test needs to switch directories
dynamically.

The module never creates directories during resolution. `ensure_data_dirs()` does that separately,
idempotently, and swallows failures so that a read-only filesystem does not crash at startup — the
real error surfaces at the first write.

### Subdirectories

```
kb/           knowledge base: chunks.db, chunk_manifest.json, uploads/
kb/uploads/   uploaded source files
audit/        audit.db, audit.jsonl
docs/         raw knowledge-base documents
reports/      rendered report artefacts
outbox/       notification outbox
shots/        screenshot artefacts
obs_shots/    observability captures
ui_ws_shots/  workspace UI captures
eval_reports/ evaluation output
_regress/     regression output
```

`scripts/` is deliberately **not** in this list: it holds user-authored custom tool scripts, which
have historically been referenced by repository-relative paths. The migration script leaves them
where they are.

### Databases

| File | Contents | Backend |
| --- | --- | --- |
| `data/sessions.db` | Sessions and messages | SQLite, always |
| `data/workspace.db` | Staged changes and the undo stack | SQLite, always |
| `data/audit/audit.db` | Audit entries | SQLite, when PostgreSQL is unavailable |
| `data/audit/audit.jsonl` | Audit entries | JSONL, the last-resort fallback |
| `data/kb/chunks.db` | Chunk index | SQLite by default; pgvector when configured |

### Audit fallback chain

```
AUDIT_DSN set and psycopg available  →  PostgreSQL  (append-only)
       │ fails
       ▼
SQLite at <data>/audit/audit.db
       │ fails
       ▼
JSONL at <data>/audit/audit.jsonl
```

If the primary backend fails mid-write, the entry is written to JSONL instead, so an audit record is
never silently lost.

### Workspace write flow

```
ws_write_file / ws_edit_file / POST /workspace/file
        │
        ▼
  resolve_path(raw)
    • resolve() first, so symlinks and junctions are expanded
    • commonpath check against the workspace root
    • _check_forbidden()  — directory names, file names, specific relative paths
    • _check_data_dir()   — the *actual* data directory, wherever AGENT_DATA_DIR points
        │
        ▼
  stage_write() → change_id + diff      (nothing touches the disk)
        │
        ▼
  apply_change(change_id, actor, approved)
    • path safety re-check
    • concurrent-modification detection (expected_mtime)
    • push onto the undo stack
    • temp file + os.replace  (atomic)
    • audit
        │
        ▼
  undo()  →  restores the previous content
```

`_check_data_dir()` exists because `FORBIDDEN_REL_PATHS` protects `<workspace>/data/`. Once the data
directory is relocated elsewhere, those relative paths no longer match anything — but if the user
points `AGENT_DATA_DIR` back inside the workspace, the protection has to work against the real
location, not a hardcoded guess.

---

## Model routing

`core/model_registry.py` merges two sources:

- **Static** — `config/routing.yaml`, the `models:` section.
- **Dynamic** — `data/models.json`, the `models:` array, written atomically (temp file plus
  `os.replace`) so a crash cannot corrupt the registry.

`GET /models` exposes the merged view with a live reachability probe for each entry, so the status
dot in the UI is a real measurement.

### Capability degradation protection

Every model declares `capabilities`. Every route may declare `requires`.

```yaml
routes:
  code:
    fallback: [qwen2.5-coder-14b, gpt-oss-20b]
    requires: [code]
```

The router strips any chain member lacking the required tags. If the chain becomes empty, it raises
`RoutingCapabilityError`. It never silently downgrades.

The reason is concrete: `qwen2.5-14b` is a general chat model, not a Coder variant. An earlier
version of the `agent` and `tool_call` routes listed it as a fallback, so when `gpt-oss-20b` was
unavailable a code-writing task quietly landed on a model without the `code` capability. Nothing
raised. The output just got worse, and the only symptom was "the model seems worse today".

Current routes: `code`, `agent`, `reasoning`, `tool_call`, `ask`, `plan`, `chinese`, `eval`,
`summarize`, `default`.

---

## Frontend architecture

The SPA is vanilla JavaScript — no framework, no build step, no `node_modules`. Files are served
straight from `agent_platform/api/static/`.

| File | Responsibility |
| --- | --- |
| `index.html` | The shell: view containers, tab bar, modals, and inline SVG icons. |
| `app.js` | The main application: SSE client, chat rendering, view routing, the run-status indicator. |
| `approval.js` | The single approval entry point. Every approval decision broadcasts a `hitl-resolved` window event so all views refresh together. |
| `observability.js` | The decision trace, the Live Timeline, metrics, and the treemaps. |
| `timeline.js` | The waterfall rendering. |
| `trace_view.js` | Decision-trace card rendering. |
| `workspace.js` | The code explorer and editor. |
| `workbench.js` | The task workbench. |
| `kb_admin.js` | Knowledge-base administration. |
| `custom_tools.js` | Custom tool management. |

### The approval bus

`approval.js` is the only place a decision is made. On resolution it dispatches:

```js
window.dispatchEvent(new CustomEvent('hitl-resolved', { detail: detail }));
```

The chat card, the Live Timeline, and the approval-card view all subscribe. Before this existed,
each view tracked its own state, and a user could see "the panel says it is not approved" in one
place and "already approved" in another. A single broadcast event makes divergence impossible.

### Styling

One stylesheet carries both light and dark themes. There is no separate theme build and no
`prefers-color-scheme` split — the theme is an explicit class on the root, so a screenshot of
either theme is deterministic.

---

## Design decisions

| Decision | Why |
| --- | --- |
| LangGraph over a hand-rolled loop | Checkpointing and `interrupt()` are the hard parts of resumable HITL. Reimplementing them correctly is more work than adopting them. |
| `tools` and `hitl` as separate nodes | `interrupt()` re-runs the node on resume. Splitting avoids double execution of writes. |
| LangGraph optional at import time | `build_graph` raises a clear error; the module still imports so the rest of the platform works without it. |
| One FastAPI module | The API is the integration point for four subsystems. Splitting it early would add indirection without adding clarity. The cost is a large file. |
| Vanilla JS, no build step | The platform must run from a source checkout and from a PyInstaller bundle with the same files. A build step would need to be replicated in both. |
| Capability-tagged routing with hard failure | Silent degradation is worse than an error. See the routing section above. |
| Staged workspace writes | Staging is what makes HITL meaningful and undo possible. Direct writes would make both impossible. |
| Three-state tool status | A boolean cannot distinguish "pending approval" from "succeeded", and confusing them produced a green check on files that were never written. |
| Per-path counting for write tools | Counting by tool name broke legitimate multi-file tasks. |
| CPU-only BGE | The 16 GB GPU belongs to `gpt-oss-20b`. Sharing it produced `CUDA error: an illegal memory access` that killed both servers. |
| Idle/inference thread split | Idle spinning wastes 32% of the machine; inference needs parallelism. One setting cannot serve both. |
| onedir desktop build | onefile's 7.5 GB per-launch extraction caused 30-90 s startups and silent crashes under concurrent launches. |
