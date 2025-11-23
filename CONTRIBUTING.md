# Contributing

Thanks for considering a contribution. This guide covers how to set up, the conventions the
codebase follows, and what a good pull request looks like here.

---

## Table of contents

- [Before you start](#before-you-start)
- [Setting up](#setting-up)
- [Coding conventions](#coding-conventions)
- [Adding a tool](#adding-a-tool)
- [Adding an endpoint](#adding-an-endpoint)
- [Adding a model alias](#adding-a-model-alias)
- [Testing](#testing)
- [Commit conventions](#commit-conventions)
- [Pull requests](#pull-requests)
- [Documentation expectations](#documentation-expectations)
- [Reporting bugs](#reporting-bugs)
- [Security issues](#security-issues)
- [License](#license)

---

## Before you start

This project has **no license file and all rights are reserved**. See
[License](#license) below. If you intend to contribute, please open an issue first so the
maintainers can confirm that your contribution will be accepted under those terms.

Contributions are understood to be offered under the same terms as the rest of the project.

---

## Setting up

```powershell
git clone <your-fork-url>
cd agent
python -m pip install -r requirements.txt
python -m pip install FlagEmbedding numpy torch   # for real retrieval quality
Copy-Item .env.example .env
```

Then start the inference servers and the API:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\start_servers.ps1
python -m uvicorn agent_platform.api.app:app --host 127.0.0.1 --port 8010 --reload --timeout-keep-alive 120
```

Full details are in [`docs/development.md`](docs/development.md).

---

## Coding conventions

### Comments explain why, in Chinese

This is the house style, and it is the codebase's primary engineering record. A comment should
explain the reasoning behind a non-obvious decision, ideally with the date and the concrete failure
it prevents.

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

**Match the surrounding language.** Chinese comments in modules that already use them; English
comments in modules that already do. Do not translate an existing Chinese comment to English — the
Chinese comments are the record of past incidents, and rewriting them loses that context.

When you fix a bug, add a comment that names the bug. Future readers need to know that the code
below them is the fix, and what it was fixing.

### Docstrings describe the contract

Arguments, return values, raised exceptions, and the failure modes a caller should handle.

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

Use modern syntax: `Optional[X]`, `Dict[str, Any]`, `list[str]`. The codebase targets Python 3.13.

### Deferred imports for optional dependencies

Third-party packages that may not be installed are imported inside the function:

```python
try:
    import torch  # noqa: PLC0415 - 延迟导入
except Exception:  # noqa: BLE001 - 没 torch 就直接跳过
    yield n
    return
```

### Graceful degradation, always logged

Optional dependencies degrade rather than crash. But the degradation must be **visible**. Note that
uvicorn's `LOGGING_CONFIG` only configures `uvicorn.*` loggers, so `agent_platform.*` loggers have
no handler and the `lastResort` fallback does not fire. When a message must be seen, write to
`stderr` as well:

```python
logger.warning(msg)
print(msg, file=sys.stderr, flush=True)
```

### Error normalisation

Tool errors reach the model as `{code, message, retryable, hint}`. Always use `ToolError` and
always fill in the `hint` — it is the model's only guidance on what to do differently.

Never let a Python traceback reach the model.

### No bare except

Broad excepts carry `# noqa: BLE001` and a comment explaining why the exception is swallowed.
Swallowing an exception in a degradation path is acceptable; swallowing one silently is not.

### Naming

| Kind | Convention | Example |
| --- | --- | --- |
| Module | `snake_case` | `tool_registry.py` |
| Class | `PascalCase` | `BudgetGuard` |
| Function / variable | `snake_case` | `estimate_tokens` |
| Module constant | `UPPER_SNAKE_CASE` | `MAX_ROUNDS` |
| Private helper | Leading underscore | `_impl_kb_search` |

### Frontend

- Vanilla JavaScript. No framework, no build step, no dependencies.
- Each file opens with a comment block explaining its responsibility.
- `'use strict';` at the top of each file.
- Cache DOM lookups at the top of the function that uses them.
- All approval decisions go through `approval.js`, which broadcasts `hitl-resolved`. Do not add a
  second resolution path — divergent approval state across views is exactly the bug that broadcast
  exists to prevent.

---

## Adding a tool

Full walkthrough in [`docs/development.md`](docs/development.md#adding-a-tool). The short version:

1. Write a `ToolSpec` factory in `agent_platform/tools/__init__.py`, or in
   `agent_platform/knowledge/tools_ext.py` for domain tools.
2. Choose the permission deliberately: `READ_ONLY` and `COMPUTE` run automatically;
   `WRITE_DATA`, `EXTERNAL_SIDE_EFFECT`, and `ADMIN` suspend for human approval.
3. Implement the handler as `(args: dict, ctx: dict) -> Any`. Raise `ToolError` with a helpful
   `hint`.
4. Register the pair in `register_all()`.
5. Verify with `GET /tools/{name}` and `python scripts\verify_tools.py`.

If the tool writes, also verify that calling it produces `status: "pending_approval"` and that the
approval card carries a sensible `impact` and `reversible`.

---

## Adding an endpoint

Endpoints live in `agent_platform/api/app.py` inside the `create_app` factory.

- Add a Pydantic request model next to the others. Every field needs a default so existing
  clients keep working.
- New response fields are additive. Clients read by field name.
- Resource endpoints return structured errors with a stable `code` rather than bare HTTP errors.
- Update the endpoint tables in [`docs/api.md`](docs/api.md) and the README.

---

## Adding a model alias

Add the model to `config/routing.yaml` under `models:`, then add it to the routes that should use
it.

**Capability tags matter.** If a route declares `requires:`, the router strips every chain member
lacking those tags and raises `RoutingCapabilityError` if nothing is left. A model added to a
constrained chain must carry every required tag.

Do not add a general-purpose model to a `requires: [code]` chain. The whole point of the mechanism
is to prevent code tasks from silently landing on a model without code capability.

---

## Testing

```powershell
python scripts\smoke_test.py          # fast smoke test
python scripts\run_all_tests.py       # full verification sweep
python scripts\run_eval.py            # evaluation
python scripts\run_regression.py      # regression with gate rules
```

There is no `tests/` directory. Verification lives in `scripts/` as executable Python, because the
scripts double as operational tools — they can be pointed at a live deployment to check its health,
not just at a fixture.

If you add a feature, add a `verify_*.py` script for it in the same change and register it in
`scripts/run_all_tests.py`.

If you change a metric target, a gate rule, or the dataset, run the regression suite and report the
result.

---

## Commit conventions

Commit messages are written in English, in the imperative mood, with a short subject line
(optionally prefixed with a scope).

```
fix(budget): stop double-counting rounds in the tools node

The tools node called check(), which tests rounds >= max_rounds, but only
the agent node charges rounds. One agent-to-tools round trip therefore
triggered the check twice, so max_rounds=8 yielded only four usable rounds.

Add check_no_rounds() for the tools node: it checks the token budget, the
context window, tool calls, and elapsed time, but not rounds.
```

Prefixes in use:

| Prefix | Use |
| --- | --- |
| `feat` | A new capability. |
| `fix` | A bug fix. |
| `perf` | A performance change. Include the measured before and after. |
| `refactor` | No behaviour change. |
| `docs` | Documentation only. |
| `build` | Build scripts and packaging. |
| `chore` | Maintenance. |

**Include the measurement** when you claim a performance property. "Faster" is not a claim; "approve
round trip went from 1.06-2.33 s to 0.08 s" is.

**Reference the incident** when you fix a bug. If the bug had a user-visible symptom, quote it.

Keep the subject under about 72 characters. Use the body for the reasoning.

---

## Pull requests

1. Branch from `main` with a descriptive name: `fix/approval-trace-pending-state`.
2. Make the change, with tests where applicable.
3. Run the smoke test and the relevant verification scripts.
4. Update the documentation (see below).
5. Update `CHANGELOG.md` under `Added` / `Changed` / `Fixed`.
6. Open the PR with a description of **what changed, why, and how it was verified**.

A good PR description answers:

- What was broken, or what was missing?
- What is the fix or the addition?
- How was it verified? Paste the actual output, not a summary of it.
- What did you measure, if the change is a performance claim?
- What did you deliberately not change, and why?

**If your change contradicts a design document in `docs/`, update the design document too.** Those
documents are the authoritative source for the domain requirements.

---

## Documentation expectations

Documentation is updated in the same change as the code.

| You changed | Update |
| --- | --- |
| An endpoint | `docs/api.md`, the README's API reference table |
| A tool | The README's tool catalog, `docs/api.md` if it added an endpoint |
| An environment variable | `docs/configuration.md`, the README's configuration table, `.env.example` |
| A default or a constant | The relevant doc **and** `CHANGELOG.md` |
| A file layout | The README's repository layout, `docs/development.md` |
| A behaviour | `CHANGELOG.md`, under `Added` / `Changed` / `Fixed` |
| A known problem | `docs/troubleshooting.md` |
| The README | **Both** `README.md` and `README.zh-CN.md`, in the same PR |

The English documentation lives in `docs/` and the README. The Chinese design documents
(`docs/01_*.md` through `docs/07_*.md`) are preserved as written and are **not** translated. They
remain the authoritative source for the domain requirements, the metric definitions, and the
rationale behind the design.

The README itself is bilingual: `README.md` is the English landing page and `README.zh-CN.md` is its
Simplified Chinese counterpart. Any README change must update **both** files in the same pull
request — translate the prose, leave the code blocks byte-identical, and keep the section count and
ordering the same in each so switching languages stays seamless.

---

## Reporting bugs

Open an issue with:

- What you did, and what you expected.
- What happened instead.
- The full error string, not a paraphrase.
- The relevant log excerpt.
- Your environment: Windows version, Python version, GPU and VRAM, and whether the models are
  running.

If the bug involves the desktop app, include `%LOCALAPPDATA%\AgentPlatform\logs\desktop.log`.

---

## Security issues

**Do not open a public issue for a security problem.** Report it privately to the maintainers.

Things worth reporting:

- Path traversal or symlink escape in the workspace tools.
- A way to reach the write path without an approval.
- A way to bypass the ACL filter in retrieval.
- A way to leak an `api_key` through the API.
- A way to make a custom script tool execute without the expected permission.

The platform is local-first, so the blast radius is one machine. That is not a reason to skip
reporting.

---

## License

**This project has no license file. All rights are reserved.**

The code, documentation, configuration, and build scripts in this repository are the property of
their authors. No permission is granted to use, copy, modify, merge, publish, distribute,
sublicense, or sell any part of this work, in any form, without prior written permission from the
copyright holders.

This is the default under copyright law when a project ships no license. It is deliberate, not an
oversight. If you would like to use this project, fork it, or contribute to it, please contact the
maintainers first and obtain explicit permission.

Contributions submitted to this repository are understood to be offered under the same terms.

The third-party components this project depends on — FastAPI, LangGraph, llama.cpp, FlagEmbedding,
bge-m3, bge-reranker-v2-m3, gpt-oss-20b, and Qwen2.5 — carry their own licenses, which apply to
those components and not to this repository's code.
