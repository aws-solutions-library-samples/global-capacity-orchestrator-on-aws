# Mission

Mission is GCO's goal-directed iteration loop. The operator declares a directive (natural-language goal), success criteria (machine-checkable), a tool allowlist (what the loop may invoke), and loop-control caps. Mission runs five-phase iterations until a verdict is reached.

## Table of Contents

- [Overview](#overview)
- [Generating a Criteria File](#generating-a-criteria-file)
- [Criteria File Schema](#criteria-file-schema)
  - [`metric_threshold` criterion](#metric_threshold-criterion)
  - [`event` criterion](#event-criterion)
  - [`tool_call_succeeded` criterion](#tool_call_succeeded-criterion)
  - [`predicate` criterion](#predicate-criterion)
  - [Required vs optional criteria](#required-vs-optional-criteria)
  - [Validator rejection reasons](#validator-rejection-reasons)
- [Predicate Permissions](#predicate-permissions)
  - [Allowed names](#allowed-names)
  - [Allowed method calls](#allowed-method-calls)
  - [Allowed operators and constructs](#allowed-operators-and-constructs)
  - [Rejected outright](#rejected-outright)
  - [Why the eval is safe](#why-the-eval-is-safe)
- [Quickstart](#quickstart)
- [One-Command Run](#one-command-run)
- [The No-AWS Smoke Test](#the-no-aws-smoke-test)
- [Sampling](#sampling)
- [Scripted Strategies](#scripted-strategies)
- [Loop Limits](#loop-limits)
- [Checkpoint Cadences](#checkpoint-cadences)
- [Audit Replay](#audit-replay)
- [MCP Self-Indexing Resources](#mcp-self-indexing-resources)
- [Session Lifecycle and Backends](#session-lifecycle-and-backends)
- [CLI Reference](#cli-reference)
- [MCP Tool Reference](#mcp-tool-reference)
- [Troubleshooting](#troubleshooting)

## Overview

A Mission session is a structured loop the operator drives through the CLI or the MCP tools. Each iteration runs five phases in order:

1. **Propose** — pick the next strategy (deterministic by default; advisory LLM sampling shapes it when enabled).
2. **Execute** — dispatch the strategy's tool calls, or run a script in the sandbox.
3. **Observe** — normalise tool results into a single Observation dict.
4. **Evaluate** — score the Observation against every Criterion.
5. **Decide** — emit a Verdict (`continue`, `adjust`, `complete`, or `terminate`) plus a reason.

The control path is fully deterministic. Sampling, when enabled, only shapes the next strategy and the closing notes in the Final_Report — it never changes the verdict, the loop-limit arithmetic, or the criterion evaluation. Sessions persist as JSON under `~/.gco/missions/` (filesystem backend) or in the `<project>-missions` DynamoDB table when `GCO_MISSION_STATE_BACKEND=dynamodb`.

The whole feature is gated. Set `GCO_ENABLE_MISSION=true` (or the umbrella `GCO_ENABLE_ALL_TOOLS=true`) before any Mission CLI subcommand or MCP tool resolves. See [Feature Flags](../mcp/README.md#feature-flags) for the table.

## Generating a Criteria File

Hand-writing a Criteria File for every Mission session gets repetitive — most sessions fall into a few patterns (drive a metric, wait for an event, ask "did the search return anything"). The `gco mission scaffold-criteria` subcommand turns a natural-language directive into a JSON file the Mission validators accept, ready to feed straight into `gco mission start --criteria-file`. The output is **always** validated through `validate_criteria` so a scaffolded file is never rejected at session-start time.

Two paths produce the file:

- **Sampling path** (when available). With `--use-sampling` and an MCP host that advertises sampling capability, or with Bedrock credentials resolving in the local environment, the resolved sampling backend is asked for a JSON array of criteria for the directive. The response runs through `validate_criteria`; on rejection, the prompt is retried with a feedback message containing the rejection reason (`--retries` controls the count, default 3). After retries are exhausted the deterministic fallback runs.
- **Deterministic fallback** (always available). The directive is keyword-matched against a small template table:
  - `loss`, `error`, `latency`, `cost` → `metric_threshold` with `op: "<="`.
  - `accuracy`, `throughput`, `f1`, `recall` → `metric_threshold` with `op: ">="`.
  - `find`, `search`, `discover`, `locate` → `predicate` checking `len(obs['tool_results']) > 0`.
  - Anything else → a single placeholder `predicate` with `expression: "True"` and a TODO note in the description.

The deterministic fallback runs without sampling, without AWS credentials, and without an MCP host — it's the path CI takes and the path operators get when they pass `--no-sampling`.

If you want to skip straight from a directive to a finished session in one call, use [`gco mission run`](#one-command-run) instead — it chains the scaffold + start + iterate-to-completion pipeline without intermediate files.

### Example invocation

```bash
gco mission scaffold-criteria \
  --directive "Drive validation loss below 0.1." \
  --allowlist find_examples --allowlist find_docs \
  --no-sampling \
  --max-criteria 3 \
  --output-file criteria.json
```

This writes a `criteria.json` file containing a single `metric_threshold` criterion targeting `val_loss <= 0.1`. Without `--output-file` the JSON is printed to stdout instead, ready to pipe.

### Example outputs

A loss-keyword directive produces a `metric_threshold`:

```json
[
  {
    "criterion_id": "loss_target",
    "kind": "metric_threshold",
    "required": true,
    "metric": "metrics.val_loss",
    "op": "<=",
    "target": 0.1
  }
]
```

An event-keyword directive produces an `event` criterion:

```json
[
  {
    "criterion_id": "training_done",
    "kind": "event",
    "required": true,
    "event_name": "job_succeeded"
  }
]
```

A "find documentation" directive produces a `predicate`:

```json
[
  {
    "criterion_id": "results_present",
    "kind": "predicate",
    "required": true,
    "expression": "len(obs['tool_results']) > 0"
  }
]
```

The `--allowlist` flag (repeatable) is informational for the deterministic path; on the sampling path it shapes the prompt so the model picks `metric_threshold` metric names or `event` event_names that the listed tools plausibly produce.

## Criteria File Schema

The criteria file is a JSON **array** — one or more criterion objects. Every criterion declares what "done" looks like for that dimension of the goal. The Evaluate phase walks the array on every checkpoint and stamps each criterion as `met`, `unmet`, or `inconclusive`. The session reaches a `complete` verdict only when **every required criterion is `met`** and **none are `inconclusive`** — see [Required vs optional criteria](#required-vs-optional-criteria).

Four criterion kinds are supported. Every criterion regardless of kind carries these three required keys:

| Key | Type | Description |
|-----|------|-------------|
| `criterion_id` | non-empty string | Stable identifier, unique across the file. Used by the audit log, the Final_Report, and the verdict cascade. |
| `kind` | string | One of `metric_threshold`, `event`, `predicate`, `tool_call_succeeded`. |
| `required` | bool | When `true`, this criterion must be `met` for the session to complete. When `false`, it's tracked but not blocking. |

The kind-specific keys are documented per section below.

### `metric_threshold` criterion

Compares a numeric value pulled from the Observation against a fixed target. Use this for metric goals like "validation loss below 0.1" or "tool count at least 5".

| Key | Type | Description |
|-----|------|-------------|
| `metric` | non-empty string | Dot-path into the Observation (e.g. `metrics.val_loss`, `tool_results.0.score`). The Evaluate phase resolves the path; missing paths produce `inconclusive`. |
| `op` | string | One of `<`, `<=`, `>`, `>=`, `==`, `!=`. |
| `target` | number | The right-hand side of the comparison (int or float). |

Example:

```json
{
  "criterion_id": "loss",
  "kind": "metric_threshold",
  "required": true,
  "metric": "metrics.val_loss",
  "op": "<",
  "target": 0.1
}
```

### `event` criterion

Met when the Observation's `events` list contains an entry whose name matches `event_name`. Use this for "the training run logged a checkpoint" or "the evaluator emitted a `goal_reached` event".

| Key | Type | Description |
|-----|------|-------------|
| `event_name` | non-empty string | The event name to look for. |

Example:

```json
{
  "criterion_id": "checkpoint_logged",
  "kind": "event",
  "required": true,
  "event_name": "training_checkpoint_saved"
}
```

### `tool_call_succeeded` criterion

The simplest and most common criterion shape: "this tool ran and succeeded". The Evaluate phase counts entries in `obs["tool_results"]` whose `tool_name` matches the criterion's `tool_name` and whose `_status` equals `"ok"`. Met when the count is at least `min_count`.

This kind is **server-evaluated** — it never goes through the predicate AST sandbox, so a sampling model that prefers any Python idiom that the predicate validator rejects (`r.startswith(...)`, `list(r.items())`, `getattr(r, ...)`, etc.) cannot block this kind from being structurally valid. Prefer `tool_call_succeeded` over a `predicate` whenever the goal is "this tool succeeded N times".

| Key | Type | Description |
|-----|------|-------------|
| `tool_name` | non-empty string | The `tool_name` field on a `tool_results` entry to match. |
| `min_count` | positive int | Optional; defaults to `1`. The criterion is met when at least this many matching entries are present in the iteration's `tool_results`. |

Example:

```json
{
  "criterion_id": "find_docs_called",
  "kind": "tool_call_succeeded",
  "required": true,
  "tool_name": "find_docs"
}
```

The `gco mission scaffold-criteria` command emits one `tool_call_succeeded` criterion per allowlisted tool when the directive is search-flavoured and an `--allowlist` is supplied, so the most common case scaffolds without ever consulting the model.

### `predicate` criterion

Evaluates a small Python expression against the Observation. Use this when the success condition is more nuanced than a single metric or event — for example "at least three results with score above 0.9".

| Key | Type | Description |
|-----|------|-------------|
| `expression` | non-empty string | A Python expression (single statement, `eval` mode). The expression has access to a single name, `obs`, holding the Observation dict. The expression is parsed and validated against an allowlist at session start; see [Predicate Permissions](#predicate-permissions) for the precise rules. |

Example:

```json
{
  "criterion_id": "high_score_results",
  "kind": "predicate",
  "required": true,
  "expression": "len([r for r in obs['tool_results'] if r['score'] > 0.9]) >= 3"
}
```

The expression is compiled once at session start and the AST is cached on the criterion. If the parser rejects the expression, `gco mission start` exits with `validation_error` before the session is ever persisted; you cannot land a Mission with a malformed predicate.

### Required vs optional criteria

The completion rule is precisely:

- **`required=true` and `met`** — counts toward completion.
- **`required=true` and `unmet`** — blocks completion.
- **`required=true` and `inconclusive`** — blocks completion.
- **`required=false` and `met`** — tracked but does not advance completion (does not block either).
- **`required=false` and `unmet`** — tracked, does not block.
- **`required=false` and `inconclusive`** — blocks completion. (One inconclusive criterion is enough to keep the session going regardless of `required`, because "we couldn't tell" is a stronger signal than "we know it failed".)

A session whose criteria file is `[]` (empty array) is rejected at validation time — Mission requires at least one criterion to be evaluable.

### Validator rejection reasons

The `validate_criteria` validator emits structured rejections through `MissionValidationError`. Every rejection carries a `code` (`"validation_error"`) and a `details` dict whose `reason` token identifies the precise rule:

| `details.reason` | Meaning |
|------------------|---------|
| `not_a_list` | The criteria payload was not a JSON array. |
| `empty` | The array was empty. |
| `not_a_dict` | One of the entries was not a JSON object. |
| `criterion_id_missing_or_invalid` | A criterion lacks a non-empty string `criterion_id`. |
| `duplicate_criterion_id` | Two criteria share the same `criterion_id`. |
| `kind_invalid` | `kind` was not one of the three supported values. |
| `required_missing_or_not_a_bool` | `required` was missing or not a JSON boolean. |
| `metric_missing_or_invalid` | `metric_threshold` lacks a non-empty string `metric`. |
| `op_invalid` | `metric_threshold` `op` was not one of the six comparison operators. |
| `target_not_a_number` | `metric_threshold` `target` was not a JSON number. |
| `event_name_missing_or_invalid` | `event` criterion lacks a non-empty `event_name`. |
| `expression_missing_or_invalid` | `predicate` criterion lacks a non-empty `expression`. |
| `tool_name_missing_or_invalid` | `tool_call_succeeded` lacks a non-empty `tool_name`. |
| `min_count_must_be_positive_int` | `tool_call_succeeded` `min_count` was not a positive int. |
| any AST-validator reason | The predicate parser rejected the expression. The reason token is propagated verbatim from `predicate.PredicateRejected`. See [Predicate Permissions](#predicate-permissions). |

## Predicate Permissions

Predicate expressions run inside a tight sandbox with two layers of defence: a **parse-time AST allowlist** and an **eval-time isolated namespace**. The full source lives at `mcp/mission/predicate.py`; this section is the operator-facing summary.

### Allowed names

| Name | Type | Notes |
|------|------|-------|
| `obs` | dict | The Observation. The only data name. Cannot be called as a function. |
| `len`, `min`, `max`, `sum`, `abs`, `any`, `all`, `sorted` | builtin callable | Pure, side-effect-free reads / aggregates. |
| `str`, `int`, `float`, `bool` | builtin callable | Pure type coercions used for normalising values before comparison. None can escape the eval-time empty-`__builtins__` namespace. |

No other names are visible — no `print`, no `open`, no `__import__`, no `eval`, no `compile`, no module references, no comprehension target shadows.

### Allowed method calls

A small set of read-only methods may be called on any value the predicate can produce (`obs`, a subscript result, a callable result, or a comprehension-bound name):

| Method | Notes |
|--------|-------|
| `.get(key[, default])` | Read-only dict accessor. Tolerates missing keys without raising; returns `default` (or `None` if omitted). |
| `.keys()`, `.values()`, `.items()` | Read-only dict views; the comprehension protocol then iterates. |
| `.lower()`, `.upper()` | Pure string transformations used in case-insensitive substring search like `'foo' in str(x).lower()`. |
| `.strip()` | Pure string transformation — leading and trailing whitespace removed. |

These seven methods land in this list because they are pure read-only accessors / transformations that cannot mutate state, escape the sandbox, or reach a builtin that the eval-time namespace blocks. Method names outside this list (`.append`, `.update`, `.pop`, `.setdefault`, `.count`, `.startswith`, `.split`, ...) are rejected at parse time.

### Allowed operators and constructs

- Arithmetic: `+ - * / // % ** @`
- Unary: `+ - not ~`
- Comparisons: `< <= > >= == != is is not in not in`
- Boolean: `and or`
- Ternary: `a if b else c`
- Containers: list / tuple / dict / set literals
- Comprehensions: list / set / dict / generator (target names cannot shadow `obs` or any allowed callable)
- Calls: bare-name calls to one of the twelve stdlib callables, or read-only method calls from the seven-method allowlist above.
- Attribute access: only `obs.<attr>` (one level deep). Attribute names cannot start with `__`. Anything more elaborate (chained walks, attributes on a subscript) is rejected — use subscripting for nested data.
- Subscripts: any `value[...]` chain whose ultimate base is an allowed name. Slices with step (`xs[::2]`) are allowed; the slice values themselves go through the same allowlist check.
- f-strings: allowed; embedded expressions re-enter the same allowlist check.

### Rejected outright

| Source | Reason token |
|--------|--------------|
| `import os`, `from x import y` | `forbidden_node` |
| `__import__("os")` | `call_target_not_allowed` |
| `lambda x: x` | `forbidden_node` |
| `(walrus := 1)` | `forbidden_node` |
| `yield`, `yield from`, `await`, async constructs | `forbidden_node` |
| `obs.__class__` | `dunder_attribute` |
| `obs[(0).__class__]` | `attribute_target_not_allowed` |
| `obs.a.b` (chained attribute access) | `attribute_target_not_allowed` |
| `obs["xs"].append(1)` (mutating method) | `call_target_method_not_allowed` |
| `obs["xs"].count(0)` (read-only method outside the seven-method allowlist) | `call_target_method_not_allowed` |
| `getattr(obs, "x").get("y")` (call-then-call shape) | `call_target_not_allowed` |
| `dict(other={"a": 1})` (only the eight pure callables are allowed) | `call_target_not_allowed` |
| `{**other}` | `dict_unpacking` |
| `func(*args)` where `func` is not on the callable allowlist | `call_target_not_allowed` |
| `getattr(obs, "x")` | `call_target_not_allowed` |
| any name starting with `__` | `dunder_name` |
| any string constant starting with `"__"` | `dunder_string` |
| comprehension target shadowing `obs` or an allowed callable | `comprehension_target_shadows_allowlist` |

Violations raise `PredicateRejected` at session-start time, **before** the expression is ever evaluated. The error envelope carries the `reason` token plus the offending node's `lineno` and `col_offset` so the operator can fix the file.

#### Accepted predicates

```python
len(obs["tool_results"]) > 0
any(r["score"] > 0.9 for r in obs["tool_results"])
any(r.get("_status") == "ok" for r in obs["tool_results"])
all(r.get("_status") == "ok" and r.get("tool_name") == "find_docs"
    for r in obs["tool_results"])
len(obs.get("errors", [])) == 0
any(k == "val_loss" for k in obs["metrics"].keys())
any("inference" in str(r).lower() for r in obs["tool_results"])
str(obs.get("count", 0)) == "0"
obs["metrics"]["loss"] < 0.5 and obs["metrics"]["accuracy"] > 0.9
not obs["errors"]
```

#### Rejected predicates

```python
__import__("os").system("rm -rf /")            # → call_target_not_allowed
obs.__class__                                  # → dunder_attribute
obs.metrics.loss                               # → attribute_target_not_allowed (chained attribute)
obs["xs"].append(1)                            # → call_target_method_not_allowed
any(k.startswith("v") for k in obs["m"].keys()) # → call_target_method_not_allowed
[x for x in obs["xs"] for x in [1,2]]          # → comprehension_target_shadows_allowlist
lambda r: r["score"] > 0.9                 # → forbidden_node
```

### Why the eval is safe

The validated AST is compiled and evaluated with `eval(code, globals_, locals_)` where:

- `globals_` is `{"__builtins__": {}}` — the empty mapping is the standard Python sandbox idiom for stripping the builtin namespace. Without `__builtins__`, even a tree that smuggled past the validator could not look up `__import__`, `open`, `compile`, `exec`, etc.
- `locals_` exposes only `obs` plus the eight whitelisted callables.

The double defence (AST allowlist plus empty `__builtins__`) is the same pattern used by the wider script sandbox (`mcp/mission/sandbox.py`). The validator is exercised by a Hypothesis property test (`tests/test_mission_predicate_security.py`) that synthesises forbidden constructs and asserts the evaluator is never reached.

If you need an expression that the allowlist rejects, the right path is to do the work inside an allowlisted tool and surface the result on the Observation under a key the predicate can read. The predicate is intentionally a thin "is the goal hit" check, not a place to put real logic.

## Quickstart

End-to-end via the CLI, with no AWS credentials required:

```bash
export GCO_ENABLE_MISSION=true

# Write a criteria file. (See "Criteria File Schema" for every field, or
# use `gco mission scaffold-criteria` to draft one from a directive.)
cat > criteria.json <<'EOF'
[
  {"criterion_id": "loss",
   "kind": "metric_threshold",
   "required": true,
   "metric": "metrics.val_loss", "op": "<", "target": 0.1}
]
EOF

# Start a session.
gco mission start \
  --directive "Drive validation loss below 0.1." \
  --criteria-file criteria.json \
  --max-iterations 10 --max-wall-clock 3600 \
  --tool-allowlist find_examples
# → {"session_id": "mission-abc123...", "status": "pending", ...}

# Iterate one step.
gco mission iterate mission-abc123 --max-iterations 1

# Check progress.
gco mission status mission-abc123 --output table

# End it manually.
gco mission complete mission-abc123
```

`--run` collapses start + iterate into one synchronous call; verdicts stream to stderr as JSON lines and the Final_Report lands on stdout when the session terminates.

## One-Command Run

For the most common operator workflow — directive → criteria → run — there is a single chained command that handles all three:

```bash
export GCO_ENABLE_MISSION=true

gco mission run \
  --directive "Find documentation about inference endpoints." \
  --tool-allowlist find_examples --tool-allowlist find_docs \
  --max-iterations 5 --max-wall-clock 300
```

`gco mission run` does three things in order:

1. **Scaffold criteria from the directive.** Same logic as `gco mission scaffold-criteria` — sampling path with deterministic fallback. When the directive is search-flavoured and you supplied an allowlist, the deterministic path emits one `tool_call_succeeded` criterion per allowlisted tool, so the most common case scaffolds without ever consulting the model.
2. **Persist a new session** with the same validators `gco mission start` runs. A scaffold-summary JSON line lands on stderr so you can see the criteria shape before tools fire:

   ```json
   {"event": "mission.run.scaffolded", "session_id": "mission-abc123",
    "criteria_count": 2, "sampling_path": false,
    "sampling_backend_resolved": "none"}
   ```

3. **Iterate to completion synchronously**, exactly as `gco mission start --run` does. Per-iteration verdicts stream to stderr as JSON; the Final_Report lands on stdout when a terminal verdict fires.

Useful options:

| Flag | Default | Purpose |
|------|---------|---------|
| `--directive TEXT` | required | Natural-language goal description. |
| `--tool-allowlist NAME` | required, repeatable | One per allowlisted tool. The first allowlisted tool also seeds the deterministic strategy when sampling is off. |
| `--max-iterations N` | `5` | Hard cap on the iteration count. Pass `-1` to opt out. |
| `--max-wall-clock SECONDS` | `300` | Hard cap on wall-clock seconds. Pass `-1` to opt out. |
| `--use-sampling` / `--no-sampling` | auto-detect | Force the sampling path on/off for both the scaffolder and the loop's Strategy_Revision sampler. Default precedence: MCP host capability → Bedrock cred probe → off. |
| `--bedrock-model-id MODEL_ID` | from env / `claude-sonnet-4-5` | Override the Bedrock model id (CLI sampling backend only; MCP sampling uses whichever model the host advertises). |
| `--save-criteria PATH` | unset | Also write the scaffolded criteria JSON to `PATH` for inspection or reuse. |
| `--max-criteria N` | `5` | Cap on the number of criterion entries the scaffolder emits. |
| `--retries N` | `3` | Sampling-path retry budget when the validator rejects the model's response. After exhaustion, falls back to deterministic templates. |
| `--allow-scripted-strategies` | off | Permit scripted strategies (the AST-validated Python sandbox). Disabled by default; required only for goals that exceed what `tool_calls` can express. |
| `--cadence` | `every_iteration` | Checkpoint cadence kind passed through to the engine. |
| `--stagnation-threshold N` | `3` | Iterations of no progress before the cascade emits `terminate, no_progress`. |
| `--dry-run` | off | Use a stub tool dispatcher and disable Strategy_Revision sampling during iteration. The criteria scaffolder still runs through Bedrock when sampling is enabled. Useful for smoke-testing the loop bookkeeping without spending live tool credits. |

### Live dispatch vs dry-run

By default, `gco mission run` (and `gco mission iterate`, `gco mission start --run`) wires the **live FastMCP tool dispatcher** — the same one the MCP tool surface uses. Every allowlisted tool actually fires against the real backend (catalog, cluster, queue, etc.) and the engine evaluates criteria against real tool-result content. This is the mode you want for verifying that a directive converges on a goal end-to-end.

Pass `--dry-run` to substitute a canned-stub dispatcher that returns `{"_status": "ok", "_stub": true, ...}` for every call. The engine bookkeeping still runs (iterations, verdicts, Final_Report), but no real tool fires and no Bedrock Strategy_Revision sampling happens between iterations. This is the mode CI uses and the mode you want when you only care about the loop mechanics, not the tool content.

The criteria scaffolder is unaffected by `--dry-run` — it still calls Bedrock (when `--use-sampling` resolves to a real backend) to generate the criteria from the directive. Only the iteration loop's tool dispatch and strategy-revision sampling are stubbed.

When the sampling path fails (transport error, unparseable JSON, validator rejection after the retry budget is exhausted) the command prints a one-line warning to stderr and falls back to the deterministic generator, so a missing or misbehaving sampling backend never blocks the run.

## The No-AWS Smoke Test

Mission's safe-tier tools (`find_examples`, `find_docs`) hit only in-memory fixtures, so the loop runs end-to-end without AWS credentials.

```bash
export GCO_ENABLE_MISSION=true

cat > criteria.json <<'EOF'
[
  {"criterion_id": "results",
   "kind": "predicate",
   "required": true,
   "expression": "len(obs['tool_results']) > 0"}
]
EOF

gco mission start --run \
  --directive "Find documentation about inference endpoints." \
  --criteria-file criteria.json \
  --max-iterations 1 --max-wall-clock 30 \
  --tool-allowlist find_examples \
  --tool-allowlist find_docs
```

The session terminates on `max_iterations` if the predicate is not satisfied within the iteration budget. The Final_Report streams to stdout as JSON. The same flow runs in CI as `tests/test_mission_no_aws.py` — `boto3.Session` is patched to raise on any client construction so the test fails loudly if a Mission code path tries to reach AWS.

## Sampling

Sampling is **advisory only**. The deterministic verdict cascade decides every Verdict; the sampler shapes only the next Strategy and the closing notes on the Final_Report.

Two backends:

| Backend | Used By | Transport |
|---------|---------|-----------|
| MCP | The `mission_*` MCP tools when the host advertises sampling capability | `ctx.sample(...)` (FastMCP `Context`) |
| Bedrock | The `gco mission` CLI | `bedrock-runtime:Converse` |

Resolution precedence at session start:

1. Explicit `--no-sampling` / `--use-sampling` flag (or the `use_sampling` field on the MCP `mission_start` payload).
2. MCP capability detection — when running inside an MCP host that advertises `sampling`, the MCP backend is selected.
3. CLI Bedrock credential probe — when `boto3` resolves credentials, the Bedrock backend is selected.
4. Otherwise sampling is off and the loop runs in deterministic-fallback mode.

Defaults:

- Model — `us.anthropic.claude-sonnet-4-5-20250929-v1:0`. Override via `GCO_MISSION_BEDROCK_MODEL_ID` or `--bedrock-model-id`.
- Region — `us-east-1`. Override via `GCO_MISSION_BEDROCK_REGION`.

Every sampling attempt emits one structured audit event (`sampling_purpose`, `sampling_status`, `sampling_backend`, `sampling_model_id`, `model_output_bytes`, `validation_error`). Sampling rejections (transport errors, malformed JSON, schema mismatch, allowlist or budget violations, AST rejections on proposed scripts) cause an automatic deterministic fallback — the iteration still runs.

### Environment context

When the MCP `mission_*` tools build the strategy-revision sampler they pass an optional `environment_context` field into the prompt — a once-per-session snapshot of slow-moving live signals so the model is not flying blind on iteration 1. The block lands under `=== Environment context (slow-moving live signals) ===` between the budget block and the iteration history.

Three top-level keys:

- `regions` — sorted list of deployed regions discovered through the same `MultiRegionCapacityChecker` used by `ai_recommend`.
- `cluster_metrics` — per-region snapshot keyed by region. Each entry carries `queue_depth`, `running_jobs`, `pending_jobs`, `gpu_utilization`, `cpu_utilization`, `recommendation_score`. Mirrors the fields `RegionCapacity` exposes; matches the data shape `ai_recommend` already feeds into its own model prompts.
- `reservations` — `{"active_count": int, "by_region": {region: int}}`. Counts only — the full reservation list is reachable through `list_reservations` if the model wants the detail.

The block is byte-capped at 4 KB. When a section overshoots (e.g. a probe returned an unusually large payload), the largest field is dropped first and the dropped key list is recorded under `_dropped_fields` so the operator can spot the gap. Top-level keys are sorted before emission so two callers with semantically-identical context produce a byte-identical prompt — the same property the determinism tests pin down for Observation summaries.

What deliberately does **not** land in the block: spot prices and on-demand prices (large, AZ-fanout, cheap to fetch on demand from `spot_prices` once the model has picked an instance shape), capacity-block offerings (reachable through `reservation_check`), and anything timestamp-stamped to second precision (a wall-clock leak would break the byte-identical determinism property in `test_mission_sampling.py`).

Failure semantics: every AWS probe is wrapped. A total credential failure or a missing checker returns `None` so the prompt omits the section entirely — the model just sees the same prompt shape it would have seen pre-environment-context. Per-region partial failures land as zeroed metrics rather than dropping the region, so the regions list is always honest.

## Scripted Strategies

Scripted strategies are opt-in via `--allow-scripted-strategies` on the CLI (or `allow_scripted_strategies=true` on the MCP `mission_start` payload). With the flag set, a Strategy may carry a Python `script` instead of a list of `tool_calls`. Scripts run inside the Mission sandbox with two layers of defence:

**Parse-time AST validator.** The script must be valid Python in `exec` mode. The validator rejects: `import` and `from ... import`, class definitions, `__import__` / `eval` / `exec` / `compile`, dunder names (anything starting with `__`), `Lambda` containing forbidden patterns, async constructs other than `await <tool>(...)` for allowlisted tools, walrus assignments to protected names, decorators (the decorator allowlist is currently empty), `with`, `match`, `assert`, `del`, `global`, and `nonlocal`. Subscript-then-call chains on disallowed names are rejected too. Violations raise `ScriptRejected` before the script is ever evaluated.

**Runtime resource caps.** The script runs under the same sandbox provider used by the wider tooling. Two env vars cap it:

| Variable | Default | Effect |
|----------|---------|--------|
| `GCO_MCP_CODE_MODE_MAX_DURATION_SECS` | `30` | Wall-clock cap on a single script's execution. |
| `GCO_MCP_CODE_MODE_MAX_MEMORY` | `268435456` (256 MiB) | Resident memory cap on the sandbox process. |

Inside the script, allowlisted tools are exposed as awaitable callables. A `mission` namespace exposes `mission.observe(key, value)` and `mission.event(name)` for incremental Observation building. Every in-script tool call goes through the standard MCP audit decorator and emits a follow-up Mission audit event with `via_script=true`.

A script that exceeds either cap is terminated; partial observations recorded via `mission.observe(...)` are preserved, the iteration's verdict resolves to `terminate`, and the reason carries the loop-limit cap that fired.

## Loop Limits

Two **loop-control** caps gate every session. They limit the controls Mission can directly observe — iteration count and wall-clock seconds — and have nothing to do with money. Cost guardrails live out-of-band: configure AWS Budgets and Cost Anomaly Detection at the account level. Real-time workload cost tracking is structurally inaccurate (Spot vs on-demand drift, EBS / EFA / egress not in the Pricing API, Cost Explorer 24-hour latency) so a Mission cost cap would fire unpredictably.

Each cap accepts either:

- a strictly-positive integer (a hard cap on that axis), or
- the explicit sentinel `-1` (no cap on that axis).

| Cap | Required | Sentinel | Notes |
|-----|----------|----------|-------|
| `max_iterations` | always | `-1` disables iteration-count termination | Hard cap on the iteration count when set to a positive int. |
| `max_wall_clock_seconds` | always | `-1` disables wall-clock termination | Hard cap on elapsed seconds since `started_at` when set to a positive int. |

Zero, other negatives (e.g. `-2`), floats, and bools are rejected with `validation_error` / `missing_or_not_positive_int_or_minus_one`. Setting both to `-1` is allowed — Mission treats double-uncapped as the operator's informed-consent opt-out, where the loop runs until a Criterion satisfies completion, the operator aborts, or (for scripted strategies) the sandbox cap fires.

The Verdict cascade evaluates loop-limit caps first, before any criterion or stagnation check, so a session always exits cleanly the moment a cap is breached. When a cap is set to the `-1` sentinel, that branch short-circuits — the cascade falls through to the next branch rather than terminating.

```bash
# A session that runs for as long as it takes to satisfy the criterion,
# but cuts off after one hour of wall-clock time:
gco mission start \
  --directive "Train until validation loss <= 0.05." \
  --criteria-file criteria.json \
  --max-iterations -1 \
  --max-wall-clock 3600 \
  --tool-allowlist train_model

# A session that runs at most 50 iterations, with no wall-clock cap:
gco mission start \
  --directive "Search for matching documentation." \
  --criteria-file criteria.json \
  --max-iterations 50 \
  --max-wall-clock -1 \
  --tool-allowlist find_docs
```

Stagnation is independent of the loop-limit caps. The engine maintains a `no_progress_counter` that advances by one on every evaluated iteration where no required criterion went from `unmet` to `met`. When the counter hits `stagnation_threshold` (default 3), the cascade emits `("terminate", "no_progress")`. The counter only advances on **evaluated** iterations — see [Checkpoint Cadences](#checkpoint-cadences).

### Why no `max_cost`

Mission caps only the controls the loop has direct visibility into. Real-time workload cost tracking is structurally inaccurate at the level of detail a Mission cap would need:

- The Pricing API returns on-demand prices; a Spot fleet over-counts by 70%+.
- EBS, EFA, data-egress, KMS, and control-plane fees are billed separately and not in the Pricing API estimate.
- Cost Explorer is the source of truth, but its 24h latency means a Mission cap based on it would fire a day after the spend happened.

For account-level cost guardrails, configure **AWS Budgets** ($0.10 / month / budget, alerts at configurable thresholds, uses actual billing data) and **AWS Cost Anomaly Detection** (free, ML-based, catches the "agent went rogue and spawned 50 GPUs" case better than a static cap). Both already exist for everyone running training jobs at scale.

## Checkpoint Cadences

A Cadence controls when the verdict cascade actually runs. Off-cadence iterations short-circuit with `("continue", "cadence_skip")` — the strategy executes, the Observation is recorded, but no Criterion is consulted and no progress counter advances.

| Kind | Required Field | Behaviour |
|------|----------------|-----------|
| `every_iteration` (default) | — | Evaluate every iteration. |
| `every_n_iterations` | `n` | Evaluate every `n`th iteration starting from iteration 0. |
| `every_t_seconds` | `t` | Evaluate when ≥ `t` seconds have elapsed since `last_checkpoint_at`. |
| `on_event` | `event_name` | Evaluate when the prior Observation contains the matching event. |

Stagnation tracking only advances on iterations that actually evaluate. A session with `cadence=every_n_iterations(n=3)` and `stagnation_threshold=4` checks for stagnation every third iteration, so the threshold is reached at iteration 12 in the worst case rather than iteration 4.

## Audit Replay

Mission emits structured audit events on every phase transition, every tool dispatch, every sampling attempt, every script call, and every state-backend write. The bulk of those events stream through the standard MCP audit pipeline. For the **in-process** view of a session, Mission also keeps a bounded ring buffer (5000 entries, FIFO) backed by `MissionAuditCollectorHandler`. The buffer holds the most recent events across all sessions running in the current process; once the cap is reached, the oldest entry is evicted to make room.

The MCP resource template `mission://sessions/{session_id}/audit-replay` reads that buffer, filters by `mission_session_id`, and reconstructs a chronologically-ordered list of audit entries (the `replay_audit_entries` helper does the join). The resource always returns a JSON array — even for sessions whose entries have aged out of the buffer (or for sessions the current process never observed). **Empty list, not 404**, is the contract: the operator can tell "session has no recent audit activity" apart from "session does not exist" by also reading `mission://sessions/{session_id}` (which 404s for unknown sessions).

```jsonc
[
  {"event_type": "mission_phase_event", "mission_session_id": "mission-abc123", "iteration_index": 0, "phase": "propose", "phase_status": "succeeded", "ts": "..."},
  {"event_type": "mission_phase_event", "mission_session_id": "mission-abc123", "iteration_index": 0, "phase": "execute", "phase_status": "succeeded", "ts": "..."},
  {"event_type": "mission_verdict_event", "mission_session_id": "mission-abc123", "iteration_index": 0, "verdict": "continue", "verdict_reason": "in_progress", "ts": "..."}
]
```

Replay is in-process only — restarting the MCP server flushes the buffer. For long-term audit retention, configure the MCP audit pipeline's external sink (CloudWatch Logs, file handler, etc.). The replay resource is meant for short-term debugging while the session is still active.

Every audit event carries `mission_session_id` and `iteration_index` so the iteration history can be reconstructed from the audit stream alone. Four event types fire:

| `event_type` | Per-iteration count | Carries |
|--------------|--------------------|---------|
| `mission_phase_event` | 5 (one per phase) | `phase`, `phase_status`, `phase_started_at`, `phase_ended_at`, optional `error_message`. |
| `mission_verdict_event` | 1 | `verdict`, `verdict_reason`, optional `revision_rationale`. |
| `mission_sampling_event` | 0 or 1 (only when sampling is consulted) | `sampling_purpose`, `sampling_status`, `sampling_backend`, optional `sampling_model_id`, `model_output_bytes`, `validation_error`. |
| `mission_script_call_event` | One per in-script tool call (only when scripted strategies are used) | `tool_name`, `via_script=true`, `args`, optional `error`. |

## MCP Self-Indexing Resources

The MCP server exposes four read-only resources that describe its own surface, useful for tool-only clients that need a runtime catalog without reading source files:

| Resource URI | Returns | When |
|--------------|---------|------|
| `mcp://gco/tools/index` | Array of `{name, description, gating_flag}` for every registered tool. | Always available. |
| `mcp://gco/tools/{name}` | Detailed metadata for the named tool, including its input schema. | Always available; returns a structured 404 envelope for unknown names. |
| `mcp://gco/resources/index` | Array of `{uri, name, description}` for every static resource and resource template. | Always available. |
| `mcp://gco/feature-flags` | The full feature-flag → tool-name map plus the umbrella-flag entry. | Always available. |

These resources are **always-on** — no feature flag gates them. They're present so a client can answer "what can I call?" without round-tripping through the MCP host's introspection commands.

Example read of `mcp://gco/tools/index`:

```jsonc
[
  {
    "name": "find_examples",
    "description": "Search the example-manifest catalog ...",
    "gating_flag": null
  },
  {
    "name": "mission_start",
    "description": "[gated by GCO_ENABLE_MISSION] Start a new Mission session.",
    "gating_flag": "GCO_ENABLE_MISSION"
  }
]
```

Tool-only clients that don't have direct resource access reach all four through the synthetic `read_resource` tool produced by the Resources As Tools transform.

## Session Lifecycle and Backends

Sessions move through these statuses:

- `pending` — created via `mission start`, no iterations yet.
- `running` — at least one iteration has begun. `started_at` is stamped on this transition.
- `paused` — the operator called `mission abort --pause`. Resumable via `mission resume`.
- `completed` — terminal. The verdict cascade emitted `complete`.
- `terminated` — terminal. The verdict cascade emitted `terminate`, or the operator called `mission abort` without `--pause`.
- `failed` — terminal. A phase raised an unhandled exception. The session JSON carries the failed phase's `error_message`.

Two persistence backends:

- **`filesystem`** (default). One JSON file per session under `~/.gco/missions/`. Atomic writes via `tempfile + fsync + os.replace`. POSIX permissions tightened to `0o700` on the directory and `0o600` on each file because session JSON contains operator directives, observations, and tool-call results that should not be readable by other local users.
- **`dynamodb`**. The `<project>-missions` DynamoDB table, provisioned by the global stack. Item shape mirrors the JSON one-to-one; partition key is `session_id`, GSI `status-index` keyed on `(status, created_at)` powers `list_sessions(status=...)` without a full table scan.

Switch via `GCO_MISSION_STATE_BACKEND=dynamodb`. Unrecognised values fall back to filesystem with a one-line warning, matching the `GCO_MCP_TOOL_SEARCH` precedent.

## CLI Reference

All `gco mission` subcommands require `GCO_ENABLE_MISSION=true`. Without the flag, the group exits with code 2 and prints the hint to stderr.

| Subcommand | Purpose |
|------------|---------|
| `gco mission start` | Validate inputs, resolve sampling, persist a new session. With `--run`, iterate to completion synchronously. |
| `gco mission run` | Scaffold criteria from a directive and drive a session to completion in one call. The chained shorthand for the most common operator workflow — see [One-Command Run](#one-command-run). |
| `gco mission scaffold-criteria` | Draft a criteria file from a natural-language directive. Validated through `validate_criteria`; writes JSON to stdout (or to `--output-file`). |
| `gco mission status <id>` | Print the full session JSON (or a table summary with `--output table`). |
| `gco mission iterate <id> [--max-iterations N]` | Drive one or more iterations of an existing session. |
| `gco mission checkpoint <id>` | Re-run the verdict cascade on the latest iteration without producing a new one. |
| `gco mission complete <id>` | Force the session into `completed` and write a Final_Report. |
| `gco mission abort <id> [--pause]` | Terminate the session, or pause it with `--pause`. |
| `gco mission resume <id>` | Transition a paused session back to `running`. |
| `gco mission history <id> [--format full\|summary]` | Print the iteration history. |
| `gco mission list [--status STATUS]` | List known sessions, optionally filtered by status. |

`gco mission start --help` prints the full option list — directive text, criteria file path, the iteration and wall-clock caps (each accepting `-1` to opt out), the tool allowlist, cadence parameters, stagnation threshold, sampling toggles, and the scripted-strategy opt-in.

## MCP Tool Reference

The MCP surface mirrors the CLI. All gated tools require `GCO_ENABLE_MISSION` (or the umbrella) and emit structured audit events on every invocation.

| Tool | Purpose |
|------|---------|
| `mission_start` | Validate inputs, resolve sampling, persist a new session. |
| `mission_status` | Return the full session JSON. |
| `mission_iterate` | Drive one or more iterations. Long-running; supports MCP progress reporting. |
| `mission_checkpoint` | Re-run the verdict cascade on the latest iteration. |
| `mission_complete` | Force the session into `completed`. |
| `mission_abort` | Terminate or pause the session. |
| `mission_resume` | Transition `paused → running`. |
| `mission_history` | Return iteration history (`full` or `summary` format). |
| `mission_list` | List known sessions. |

Resource templates expose session state: `mission://sessions/{session_id}` returns the live session JSON; `mission://sessions/{session_id}/report` returns the Final_Report (only after the session terminates); `mission://sessions/{session_id}/audit-replay` reconstructs the in-process audit history described in [Audit Replay](#audit-replay). The four self-indexing resources documented in [MCP Self-Indexing Resources](#mcp-self-indexing-resources) are also reachable as tools through the Resources As Tools transform.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Mission tools are gated. Set GCO_ENABLE_MISSION=true ...` | The feature flag is unset. | `export GCO_ENABLE_MISSION=true` before the command. |
| `validation_error` with `field=criteria, reason=criterion_id_missing_or_invalid` | A Criterion is missing `criterion_id` or has a non-string value. | Fix the criteria JSON; every entry needs a unique non-empty string `criterion_id`. |
| `validation_error` with `field=budget, reason=missing_or_not_positive_int_or_minus_one` | `max_iterations` or `max_wall_clock_seconds` is missing, zero, a negative other than `-1`, or a non-integer. | Pass a positive int, or pass `-1` to opt out of that cap explicitly. |
| `validation_error` with `field=criteria, reason=expression_missing_or_invalid` | A predicate criterion's `expression` is missing or empty. | Add the `expression` field with a non-empty Python expression. |
| Predicate `validation_error` with an AST-validator reason (e.g. `forbidden_call`, `dunder_name`, `invalid_comprehension_target`) | The expression uses a construct outside the allowlist. | See [Predicate Permissions](#predicate-permissions) for what's allowed. Move the work into a tool and read the result via `obs[...]`. |
| `bedrock_AccessDeniedException` (sampling event `validation_error`) | IAM does not allow `bedrock:InvokeModel` for the resolved model. | Grant the permission on the calling principal, or pass `--no-sampling` to fall back to deterministic mode. |
| `bedrock_no_credentials` (sampling event `validation_error`) | The CLI could not resolve AWS credentials. | Configure credentials, or pass `--no-sampling`. The session still runs deterministically. |
| `session_not_found` | The session id does not exist on the configured backend. | Confirm with `gco mission list`; the id may have a typo or live on a different backend. |
| `session_terminal` | The session already ended (`completed`, `terminated`, or `failed`). | Read the Final_Report via the report resource, or start a new session. |
| `session_paused` | The session was paused via `gco mission abort --pause`. | Run `gco mission resume <id>` first. |
| `Tool '<name>' is not in the allowlist` (script audit event) | A script called a tool that is not in the session's allowlist. | Add the tool to `--tool-allowlist`, or remove the call from the script. |

For a list of every gated tool and the flag that controls it, see [Feature Flags](../mcp/README.md#feature-flags).
