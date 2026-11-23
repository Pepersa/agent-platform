# Changelog

All notable changes to the Agent Platform are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

Nothing yet.

---

## [2026-11-24]

A consolidation day. The work below spans the desktop packaging, the approval path, the budget
accounting, the resource governance, and the deployment layout. Each entry names the failure it
prevents, because every one of them was found in production use.

### Added

- **A bilingual README with a language switcher.** `README.md` was the English landing page and
  `README.zh-CN.md` carried a full Simplified Chinese translation of it — same sections, same
  tables, same code blocks, same relative links. Both files carried a text-only switcher in the header
  linking to the other, so a reader could read either language straight from GitHub or from a local
  checkout. This two-file layout was later consolidated into the single-page bilingual README
  described under **Changed** below; `README.zh-CN.md` no longer exists.

- **Configurable ReAct round limit on `/chat`.** The endpoint previously accepted no round budget
  at all, so the server always applied the hardcoded value regardless of what the client asked for.
  `ChatRequest.max_rounds` (range 1-64) is now honoured per request, and `AGENT_MAX_ROUNDS` sets the
  default. The default moved from 8 to 20: eight rounds were exhausted by two or three retrieval
  rounds plus two or three tool rounds, and users reported "budget exhausted after a few steps".

- **A real `rounds` field in the graph state.** `BudgetGuard.rounds` lived only in the per-request
  closure and was never written back, so the API had no way to read the true count. It fell back to
  the trace length, which mixed LLM entries, tool entries, and approval entries — producing
  displays like "9/8" when `max_rounds` was 8. The agent node now writes the real value after each
  LLM call, and the trace fallback counts only `kind == "llm"` entries.

- **Approval fast path.** When the pending approval is exactly one tool call and that tool is on the
  fast-path whitelist — a write whose result is already a complete, self-describing JSON payload —
  the graph skips the trailing LLM call and uses the tool result directly as the answer. The
  `/approve` response reports `fast_path: true`. The round trip went from **1.06-2.33 s to
  0.08 s**. Clients can force the old behaviour with `"summarize": true` on the request.

- **`/approve` now returns the decision trace.** `/chat` streams through `_run_graph_stream` and
  accumulates a trace as it goes; `/approve` used `graph.invoke()`, which returns only the final
  state, and `_build_response` built no trace at all. The trace panel therefore kept showing the
  pre-approval frame — `ws_write_file` still amber "awaiting approval" after the file was already on
  disk. `/approve` now rebuilds the trace from the final state via `_build_trace_from_state`, and
  also returns a `budget` snapshot so the round and token counters do not reset to zero.

- **A unified approval entry point.** All approval decisions now go through `approval.js`, which
  broadcasts a `hitl-resolved` window event. The chat card, the Live Timeline, and the
  approval-card view all subscribe, so their state cannot diverge. Previously each view tracked its
  own state and the UI could show "the panel says it is not approved" next to "already approved".

- **A PyTorch thread limiter.** `knowledge/threading_limits.py` caps intra-op and inter-op threads
  to 4 while idle and raises them to 8 during inference, restoring 4 afterwards. The resident
  `bge-m3` and `bge-reranker-v2-m3` OpenMP pools were spinning-wait after load and burning **32% of
  16 logical cores** with no requests in flight. Idle consumption is now below **0.03%**.
  Configurable with `AGENT_TORCH_THREADS` and `AGENT_TORCH_INFER_THREADS`.

  The inference window is deliberately wider: a cross-encoder is pure CPU matrix work, and 4 threads
  is nearly 2x slower than 8 (measured `n=20, max_length=256`: 19.1 s vs 10.8 s; `n=50`: 32.3 s vs
  18.8 s). Sixteen threads is slower than eight — oversubscription eats the gain.

- **Data-directory externalization.** `AGENT_DATA_DIR` relocates every runtime artifact —
  `sessions.db`, `workspace.db`, `audit/`, `kb/chunks.db`, `models.json`, `custom_tools.json`,
  logs, screenshots, evaluation output — to any drive. `core/paths.py` is the single resolver, with
  a priority order of explicit argument, then environment variable, then `<repo>/data`. The default
  path is byte-for-byte identical to the pre-refactor layout, so existing data is not lost.
  `scripts/migrate_data_dir.py` moves an existing installation. See
  [`docs/04_数据目录外置.md`](docs/04_数据目录外置.md).

- **`/ping`, a fast liveness probe.** `/health` probes the LLM endpoints, scans the tool registry,
  and queries SQLite, so it can take 1-3 seconds. The desktop shell probes `/health` with a 3-second
  timeout, so a busy model endpoint made it conclude the API was unreachable and exit. `/ping`
  touches no model, no database, and no index, and returns in milliseconds.

- **`GET /workspace/repomap`.** A flat file listing with per-file line counts, feeding the
  codebase treemap in the observability panel. `/workspace/tree` only returns byte sizes, which is
  not enough to size a treemap.

### Changed

- **The bilingual README was consolidated into a single landing page.** `README.zh-CN.md` was
  deleted and its full Simplified Chinese translation now lives in the root `README.md`, below the
  English text, in a second collapsible `<details>` block. The two-file layout could not keep the
  reader on the repository landing page: GitHub renders the root README onto
  `https://github.com/Pepersa/agent-platform`, but that landing page has no standalone URL, so any
  link to a README *file* necessarily landed on the `blob/main/<file>` file view with the
  Preview / Code / Blame / Raw toolbar — the reader left the landing page every time they switched
  language. Both halves of the merged page are reachable through same-page anchors (`#english` and
  `#chinese`), so switching language now keeps the browser on the landing page. Each block's
  `<summary>` carries a link to the *other* block, so the switch still works when one half is
  collapsed. The English and Chinese halves retain their original heading counts (22 `##` and 75
  `###` each) and their 25 code blocks are byte-identical, so every table-of-contents anchor and
  every relative link resolves exactly as before.

- **The desktop build switched from `--onefile` to `--onedir`.** Onefile extracted the whole
  runtime — roughly **7.5 GB** — into `%TEMP%\_MEIxxxxxx` on every launch. Startup took **30 to 90
  seconds**. Clicking the icon several times spawned several concurrent instances, each extracting
  its own copy and competing for disk I/O; when extraction failed the bootloader exited
  **silently**, with no error shown. Onedir writes the runtime to disk at build time and launching
  is an ordinary exe load, measured at **823 ms**. There is no temporary extraction directory and no
  `_MEI*` residue.

  The artifact is now a directory: `dist\desktop\AgentPlatform\AgentPlatform.exe`. Shortcuts, VBS
  launchers, and documentation references must be updated to point at the file inside it.

- **`--runtime-tmpdir` was removed.** It only applies to onefile, PyInstaller warns about it under
  onedir, and it was the original source of the "C: drive is full" problem.

- **The desktop build is now silent (`--window`).** Double-clicking the exe no longer opens a
  console window next to the app window. Because a windowed build has `sys.stdout` and
  `sys.stderr` set to `None`, `desktop/entry_point.py` redirects both to
  `%LOCALAPPDATA%\AgentPlatform\logs\desktop.log` before anything else runs — otherwise the first
  `print()` raises `AttributeError` and the app dies.

- **The desktop app is single-instance.** A Win32 named mutex, `Local\AgentPlatformDesktopSingleton`,
  guards the app. A second launch finds the existing window by title, restores it
  (`ShowWindow(SW_RESTORE)`) and brings it to the front (`SetForegroundWindow`), then exits
  cleanly. The `Local\` namespace keeps different users on a terminal server from blocking each
  other.

- **`/approve` reuses the graph that produced the approval.** It previously called
  `ensure_graph()` with no arguments, building a fresh graph — so on resume the `max_rounds`,
  model, and mode silently reverted to their defaults and the user's settings were discarded. The
  API now registers each thread's graph and looks it up on approval, falling back to a new graph
  only when the original is genuinely gone (a process restart, or a cross-instance approval).

- **The knowledge tool timeout rose from 20 s to 60 s.** The cold BGE weight load takes about 46
  seconds, so the 20-second budget killed a healthy load and reported
  `[TIMEOUT] 工具 'kb_search' 执行超时` while the model was loading correctly.

- **The reranker's `max_length` dropped from 1024 to 512.** Cross-encoder cost is dominated by
  sequence length, and the corpus's chunk lengths are p50=24 / p90=211 / max=1459 — 512 tokens
  covers the vast majority. Quality was verified against real retrieval candidates.

- **`BGE_RERANK_SHORTLIST` now defaults to off.** Truncating the candidate list loses top-6
  quality — the overlap with the untruncated result was only 23 of 36. The speedup comes from
  `max_length` and from the inference-time thread count instead.

- **Write tools are counted per target path, not per tool name.** `MAX_SAME_TOOL_CALLS` means "do
  not repeat the same thing in this turn". Counting by tool name broke legitimate multi-file tasks:
  writing `demo.py` and `tutorial.md` hit the limit on the third file, and the code silently did not
  get written. `ws_write_file` and `ws_edit_file` are now counted by path, and the same path may be
  written up to `MAX_SAME_WRITE_PATH_CALLS` times — "create the file, then edit it" is normal.

- **Tool status in the decision trace is three-state.** `ok` is now `true` (success), `false`
  (failure), or `null` with `status: "pending_approval"`. See "Fixed" below for why the boolean
  was wrong.

- **The welcome empty state was rebuilt.** A new chat opened blank. The welcome state now renders
  properly, and the chat tab is no longer closable — closing it left the user with no way back.

### Fixed

- **Rounds were double-counted.** Both the agent node and the tools node called
  `BudgetGuard.check()`, which tests `rounds >= max_rounds`. But rounds are only charged by the
  agent node, so a single agent-to-tools round trip triggered the check twice. `max_rounds=8`
  actually allowed four usable rounds. `check_no_rounds()` now exists for the tools node: it checks
  the token budget, the context window, the tool-call count, and elapsed time, but not rounds.
  Rounds are charged and judged in the agent node only.

- **Pending approvals were rendered as successes.** The trace entry for a tool call is created when
  the agent node emits the call — before the tools node discovers that approval is required. The old
  code computed `ok=True` at creation time, and the frontend treated `ok !== false` as success. So
  the UI showed a green check for a file that was never written, which was the main source of the
  "the code never gets written" impression. The approval event now rewrites the matching entry in
  place to `ok=None` / `status="pending_approval"`, and the frontend renders it amber.

- **Approval waits consumed the tool-call budget.** A pending approval is not an execution. The
  trace now records approvals with `kind="approval"`, and `tool_calls_used` stays unchanged while
  an approval is outstanding.

- **Per-turn counters leaked across requests.** `tool_call_counts` and `query_counts` are graph
  state fields, persisted by the checkpointer per `thread_id`, but their semantics are "do not
  repeat the same thing within this turn". The `/chat` handler reset `failure_counts` but not
  these two, so the counts accumulated across the session. After asking the agent to write three
  files, every subsequent request's `ws_write_file` was rejected with `DUPLICATE_TOOL_CALL`, and
  the model could only explain that rule 9 was blocking it. Both counters are now reset explicitly
  in the initial state.

- **The PyInstaller build could not find the CUDA DLLs.** The packaged exe failed with
  `0xC0000135` (`STATUS_DLL_NOT_FOUND`) because `cublas64_12.dll` and `cudart64_12.dll` live in
  LM Studio's separate CUDA vendor directory, which is not on the system path. The build now
  resolves that directory and puts it on `PATH` for the packaged process, the same way
  `deploy/start_servers.ps1` does for the source-run servers.

- **The desktop app reported the API as unreachable while it was healthy.** The single-instance
  check and the API probe used `/health` with a 3-second timeout. When a model endpoint was busy
  generating, the probe timed out and the shell exited. The probe now uses `/ping`.

### Performance

| Change | Before | After |
| --- | --- | --- |
| Desktop launch | 30-90 s | **823 ms** |
| Approval round trip | 1.06-2.33 s | **0.08 s** |
| API idle CPU (16 logical cores) | 32% | **< 0.03%** |
| Per-launch temp extraction | ~7.5 GB | **0** |
| Usable rounds at `max_rounds=8` | 4 | **8** |

---

## [2026-11-23]

### Added

- **The retrieval policy.** `core/retrieval_policy.py` decides per turn whether the knowledge base
  is needed. When it is not, the model does not even receive the retrieval tool schema, so general
  questions consume no tool rounds. The decision, its rule name, and a human-readable reason travel
  to the client as `retrieval_needed` / `retrieval_rule` / `retrieval_reason` /
  `retrieval_policy_version`. `AGENT_FORCE_LEGACY_RETRIEVAL=1` restores the previous
  "always retrieve" behaviour for before/after evidence collection.

- **The decision trace and the Live Timeline.** Every turn now produces an ordered trace tagged by
  `kind` (`llm`, `tool`, `approval`), plus a waterfall view. LLM calls are recorded as their own
  step type because model thinking is usually the largest share of wall-clock time — in one
  measured multi-tool turn the LLM took 11.9 s while all tools together took 9 ms.

- **The repository map.** `GET /workspace/repomap` and the codebase treemap in the observability
  panel.

### Fixed

- **BGE models were allowed onto the GPU.** With `bge-m3` on the GPU, the requirement was 22.3 GB
  against a 16 GB card, and both inference servers crashed with
  `CUDA error: an illegal memory access`. Reproduced twice. `resolve_flag_devices` now forces CPU
  by default; `BGE_DEVICES=cpu` is the default and `BGE_DEVICES=cuda:0` is an explicit opt-in that
  the user accepts the risk for.

- **The token estimator over-counted Chinese by about 43%.** The estimator used 1.0 token per CJK
  character. Measured against a real endpoint — a 2000-character Chinese prompt returned
  `usage.prompt_tokens = 1400` — the true coefficient is **0.70**. The old value wasted roughly 9k
  tokens of a 32k window, enough for a 200-line commented class. The estimator now also normalises
  non-string message content before measuring, because LangChain's `ToolMessage.content` may be a
  list of content blocks and `str()` on it produces an escaped repr.

- **The context window was raised from 8192 to 32768.** 8192 was what actually blocked real code
  tasks: modifying a 200-line class, together with the system prompt and the tool schemas,
  exceeded it and the model server returned
  `request exceeds the available context size`. `gpt-oss-20b` uses GQA with q8_0 KV quantisation,
  so the KV cache is only 24 KiB per token — 32k of context costs roughly 0.75 GB of VRAM.

- **A cold knowledge-base query was killed by the keep-alive.** The first query pays a ~46 s BGE
  load, during which the default 5-second keep-alive reclaimed the socket and the client saw
  `ReadError: [WinError 10054]`. The API is now started with `--timeout-keep-alive 120`.

### Changed

- **BGE models were forced onto the CPU**, and the reasoning was recorded in the source: the
  16 GB card belongs to `gpt-oss-20b`, and `bge-m3` is a pure encoder whose inference is
  low-frequency.

---

## [2026-11-22]

### Added

- **Observability foundations.** The decision trace, the Live Timeline, the per-request budget
  snapshot, the `context_window` and `system_prompt_chars` fields (a real measurement rather than
  an estimate), and `/metrics`.

- **The retrieval policy and `AGENT_FORCE_LEGACY_RETRIEVAL`.**

### Fixed

- **The budget snapshot always read zero.** The trace event is yielded inside the
  `chunk_mode == "updates"` branch, where `last_values` is still the *previous* chunk — or an empty
  dict. The panel showed `ReAct 轮次 0/8 · token 0/60000 · 工具调用 0/24` permanently. The
  snapshot now prefers the graph state and falls back to the trace.

- **`stop_reason` was checked after `tool_calls`.** `_route_after_agent` looked for tool calls
  before checking whether a terminal reason had already been set, so a stopped graph could loop
  again.

---

## [2026-11-21]

### Added

- **The LangGraph orchestration layer.** Four nodes — `agent`, `tools`, `hitl`, `plan` — with the
  two-stage HITL split. LangGraph's `interrupt()` re-runs the interrupted node from the top on
  resume, so combining "approve" and "execute" in one node would have executed writes twice. The
  `tools` node decides that approval is needed and raises the interrupt; the `hitl` node only
  resolves the decision; the write happens back in `tools` on the resume pass.

- **The three interaction modes.** `agent` (full ReAct), `ask` (read-only, `ASK_MAX_TOOL_ROUNDS =
  2`), and `plan` (structured plan, no tools — the `plan` node is not connected to the `tools`
  node, so a tool call is structurally impossible).

- **The capability-aware model router.** Routes declare `requires: [...]`; the router strips chain
  members lacking the tags and raises `RoutingCapabilityError` if the chain empties. `qwen2.5-14b`
  is deliberately excluded from the code and agent chains — it is a general chat model, and routing
  a code task to it produced quietly bad output with no error signal.

---

## Notes

- **No version tags have been cut yet.** The entries above are dated working sessions rather than
  formal releases.
- **The Chinese design documents** in `docs/` are the authoritative source for the domain
  requirements and are preserved as written.
