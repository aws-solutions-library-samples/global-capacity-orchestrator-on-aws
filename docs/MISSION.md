# Mission

Mission is GCO's goal-directed iteration loop. The operator declares a directive (natural-language goal), success criteria (machine-checkable), a tool allowlist (what the loop may invoke), and loop-control caps. Mission runs five-phase iterations until a verdict is reached.

## Table of Contents

- [Overview](#overview)
- [Generating a Criteria File](#generating-a-criteria-file)
- [Criteria File Schema](#criteria-file-schema)
  - [`metric_threshold` criterion](#metric_threshold-criterion)
  - [`metric_trend` criterion](#metric_trend-criterion)
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
- [Reading Metrics with Reader Tools](#reading-metrics-with-reader-tools)
- [Scoring Progress with the Semantic-Progress Judge](#scoring-progress-with-the-semantic-progress-judge)
- [Quickstart](#quickstart)
- [One-Command Run](#one-command-run)
- [The No-AWS Smoke Test](#the-no-aws-smoke-test)
- [Tool Allowlist](#tool-allowlist)
  - [`--allow-all-tools` / `allow_all_tools`](#--allow-all-tools--allow_all_tools)
  - [Distinct from `GCO_ENABLE_ALL_TOOLS`](#distinct-from-gco_enable_all_tools)
  - [Risk-tier scope](#risk-tier-scope)
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

The whole feature is gated. Set `GCO_ENABLE_MISSION=true` (or the umbrella `GCO_ENABLE_ALL_TOOLS=true`) before any Mission CLI subcommand or MCP tool resolves. See [Feature Flags](../gco_mcp/README.md#feature-flags) for the table.

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

This writes a criteria.json file containing a single `metric_threshold` criterion targeting `val_loss <= 0.1`. Without `--output-file` the JSON is printed to stdout instead, ready to pipe.

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

Five criterion kinds are supported. Every criterion regardless of kind carries these three required keys:

| Key | Type | Description |
|-----|------|-------------|
| `criterion_id` | non-empty string | Stable identifier, unique across the file. Used by the audit log, the Final_Report, and the verdict cascade. |
| `kind` | string | One of `metric_threshold`, `metric_trend`, `event`, `predicate`, `tool_call_succeeded`. |
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

### `metric_trend` criterion

Evaluates the *direction* of a metric across iterations rather than comparing a single point-in-time value to a fixed target. Use this for goals like "loss is falling" or "throughput is not regressing" where the shape of the curve matters more than any one reading.

Unlike `metric_threshold`, which reads the latest value off the per-iteration Observation, `metric_trend` reads the metric's **history** — the ordered series of numeric readings the engine accumulates across iterations under `metric_history` (oldest→newest). The engine builds that series in the Evaluate phase; you don't have to make tools emit it. Non-numeric readings are skipped so a stray string can't poison the series.

| Key | Type | Description |
|-----|------|-------------|
| `metric` | non-empty string | The metric to track. Accepts the same dot-path form as `metric_threshold` (`metrics.loss`) or the bare metric name (`loss`); a leading `metrics.` is stripped before the history lookup. |
| `direction` | string | One of `decreasing` (last < first), `increasing` (last > first), `non_increasing` (last <= first), `non_decreasing` (last >= first). The strict forms require an actual net change; the `non_` forms allow a flat series. |
| `window` | positive int | Optional. Consider only the most-recent `window` readings. Default: the entire history. |
| `min_points` | positive int | Optional. The minimum number of numeric readings required before the criterion decides `met`/`unmet`. With fewer points the criterion is `inconclusive` (a trend is undefined on a single reading, and the loop is never failed for lack of history). Default and floor: `2`. |

The verdict compares the last reading of the windowed series to its first reading per `direction`. Evidence is a structured dict (`direction`, the windowed `points`, `first`, `last`, and net `delta`) so the audit log shows exactly what the verdict was computed from.

Example — "drive validation loss down across the run, looking at the last 5 readings":

```json
{
  "criterion_id": "loss_falling",
  "kind": "metric_trend",
  "required": true,
  "metric": "metrics.val_loss",
  "direction": "decreasing",
  "window": 5,
  "min_points": 3
}
```

Pair `metric_trend` with `metric_threshold` to express "loss is both below 0.1 **and** still falling" — two criteria over the same metric, one point-in-time and one history-aware.

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

This kind is **server-evaluated** — it never goes through the predicate AST sandbox, so a sampling model that prefers any Python idiom that the predicate validator rejects (`r.count(...)`, `list(r.items())`, `getattr(r, ...)`, etc.) cannot block this kind from being structurally valid. Prefer `tool_call_succeeded` over a `predicate` whenever the goal is "this tool succeeded N times".

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
| `kind_invalid` | `kind` was not one of the supported values. |
| `required_missing_or_not_a_bool` | `required` was missing or not a JSON boolean. |
| `metric_missing_or_invalid` | `metric_threshold` or `metric_trend` lacks a non-empty string `metric`. |
| `op_invalid` | `metric_threshold` `op` was not one of the six comparison operators. |
| `target_not_a_number` | `metric_threshold` `target` was not a JSON number. |
| `direction_invalid` | `metric_trend` `direction` was not one of `decreasing`, `increasing`, `non_increasing`, `non_decreasing`. |
| `window_must_be_positive_int` | `metric_trend` `window` was present but not a positive int. |
| `min_points_must_be_positive_int` | `metric_trend` `min_points` was present but not a positive int. |
| `event_name_missing_or_invalid` | `event` criterion lacks a non-empty `event_name`. |
| `expression_missing_or_invalid` | `predicate` criterion lacks a non-empty `expression`. |
| `tool_name_missing_or_invalid` | `tool_call_succeeded` lacks a non-empty `tool_name`. |
| `min_count_must_be_positive_int` | `tool_call_succeeded` `min_count` was not a positive int. |
| any AST-validator reason | The predicate parser rejected the expression. The reason token is propagated verbatim from `predicate.PredicateRejected`. See [Predicate Permissions](#predicate-permissions). |

## Predicate Permissions

Predicate expressions run inside a tight sandbox with two layers of defence: a **parse-time AST allowlist** and an **eval-time isolated namespace**. The full source lives at `gco_mcp/mission/predicate.py`; this section is the operator-facing summary.

### Allowed names

| Name | Type | Notes |
|------|------|-------|
| `obs` | dict | The Observation. The only data name. Cannot be called as a function. |
| `len`, `min`, `max`, `sum`, `abs`, `any`, `all`, `sorted` | builtin callable | Pure, side-effect-free reads / aggregates. |
| `str`, `int`, `float`, `bool` | builtin callable | Pure type coercions used for normalising values before comparison. None can escape the eval-time empty-`__builtins__` namespace. |

No other names are visible — no `print`, no `open`, no `__import__`, no `eval`, no `compile`, no module references, no comprehension target shadows.

### Allowed method calls

A small set of read-only methods may be called on any value the predicate can produce (`obs`, a subscript result, a literal or container expression, a safe-call result, or a comprehension-bound name):

| Method | Notes |
|--------|-------|
| `.get(key[, default])` | Read-only dict accessor. Tolerates missing keys without raising; returns `default` (or `None` if omitted). |
| `.keys()`, `.values()`, `.items()` | Read-only dict views; the comprehension protocol then iterates. |
| `.lower()`, `.upper()` | Pure string transformations used in case-insensitive substring search like `'foo' in str(x).lower()`. |
| `.strip()` | Pure string transformation — leading and trailing whitespace removed. |
| `.startswith(prefix[, start[, end]])` | Pure string prefix test used to classify metric and event names. |

These eight methods land in this list because they are pure read-only accessors / transformations that cannot mutate state, escape the sandbox, or reach a builtin that the eval-time namespace blocks. Method names outside this list (`.append`, `.update`, `.pop`, `.setdefault`, `.count`, `.split`, ...) are rejected at parse time.

### Allowed operators and constructs

- Arithmetic: `+ - * / // % ** @`
- Unary: `+ - not ~`
- Comparisons: `< <= > >= == != is is not in not in`
- Boolean: `and or`
- Ternary: `a if b else c`
- Containers: list / tuple / dict / set literals
- Comprehensions: list / set / dict / generator (target names cannot shadow `obs` or any allowed callable)
- Calls: bare-name calls to one of the twelve stdlib callables, or read-only method calls from the eight-method allowlist above.
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
| `obs["xs"].count(0)` (read-only method outside the eight-method allowlist) | `call_target_method_not_allowed` |
| `getattr(obs, "x").get("y")` (receiver contains the forbidden `getattr` call) | `call_target_not_allowed` |
| `dict(other={"a": 1})` (only the twelve pure callables are allowed) | `call_target_not_allowed` |
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
any(k.startswith("val_") for k in obs["metrics"].keys())
any("inference" in str(r).lower() for r in obs["tool_results"])
str(obs.get("count", 0)) == "0"
obs["metrics"]["loss"] < 0.5 and obs["metrics"]["accuracy"] > 0.9
not obs["errors"]
```

#### Rejected predicates

```python
__import__("os").system("rm -rf /")             # → call_target_not_allowed
obs.__class__                                   # → dunder_attribute
obs.metrics.loss                                # → attribute_target_not_allowed (chained attribute)
obs["xs"].append(1)                             # → call_target_method_not_allowed
obs["xs"].count(0)                              # → call_target_method_not_allowed
[x for x in obs["xs"] for x in [1,2]]           # → comprehension_target_shadows_allowlist
lambda r: r["score"] > 0.9                      # → forbidden_node
```

### Why the eval is safe

The validated expression is evaluated with an explicit namespace:

- `globals_` contains `{"__builtins__": {}}`, `obs`, and the twelve whitelisted callables. The callables and `obs` must be globals because Python compiles comprehensions and generator expressions into nested scopes that do not resolve free names from the separate `locals` mapping. No other globals are exposed.
- `locals_` is empty. The explicit empty `__builtins__` entry prevents Python from automatically inserting the normal builtin namespace, so even a tree that smuggled past validation cannot look up `__import__`, `open`, `compile`, `exec`, or similar capabilities.

The double defence (AST allowlist plus empty `__builtins__`) is the same pattern used by the wider script sandbox (`gco_mcp/mission/sandbox.py`). The validator is exercised by a Hypothesis property test (`tests/test_mission_predicate_security.py`) that synthesises forbidden constructs and asserts the evaluator is never reached.

If you need an expression that the allowlist rejects, the right path is to do the work inside an allowlisted tool and surface the result on the Observation under a key the predicate can read. The predicate is intentionally a thin "is the goal hit" check, not a place to put real logic.

## Reading Metrics with Reader Tools

A `metric_threshold` criterion reads a number off the Observation by dot-path — but something has to *put* that number there. The metric-reader tools (registered under `gco_mcp/tools/metrics.py`, all carrying the `safe` tag) are read-only tools that surface a single training-style scalar in the exact shape the Observe phase merges, so a criterion can watch training loss, eval accuracy, throughput, or GPU utilisation with zero scripting.

### The canonical metrics shape

Every reader tool returns a JSON object with a top-level `metrics` key whose value maps metric names to numbers:

```json
{
  "metrics": {"loss": 0.42},
  "source": "file:s3://cluster-shared/run-7/trainer_state.json",
  "region": "us-east-1",
  "format": "hf_trainer_state",
  "aggregation": "min"
}
```

This is the **canonical metrics shape**: a dict with a top-level `metrics` key mapping string names to numeric (`int`/`float`) values. When a tool call returns this shape during the Execute phase, the Observe phase merges it into the iteration's Observation via `metrics.update(...)`, so the value lands at `observation["metrics"]["loss"]`. Everything else in the result — `source`, `region`, `format`, `aggregation`, timestamps, raw datapoints — is provenance that lives strictly *outside* the `metrics` object, so the merged `observation["metrics"]` dict only ever contains numbers.

Two consequences follow directly:

- A reader's success result merges cleanly, and a criterion reads the value by dot-path.
- A reader's *failure* result is a structured error envelope (`{"code": "...", "details": {...}}`) with **no** top-level `metrics` key, so the Observe phase skips it and the criterion is left `inconclusive` rather than failing the loop. A failed read never crashes the session.

### The `metrics.<name>` dot-path convention

A reader emits its value under a single metric name — either the caller-supplied output name or a deterministic default derived from the source (the CloudWatch metric name, the extracted field, the file field). The name is constrained to a single dot-path segment: 1–128 characters, no `.` separator, no whitespace. The resulting dot-path a criterion reads is therefore always exactly `metrics.<name>`.

So a reader that emits `{"metrics": {"loss": 0.42}}` is observed by a criterion whose `metric` field is `"metrics.loss"`:

```json
{
  "criterion_id": "loss_target",
  "kind": "metric_threshold",
  "required": true,
  "metric": "metrics.loss",
  "op": "<",
  "target": 0.1
}
```

Pick the output name on the tool call and the `metrics.<name>` dot-path on the criterion so the two line up.

### Aggregation modes for history-bearing readers

Mission keeps the per-iteration `metrics` dict **point-in-time** — each iteration's Observation carries the latest reading, and a `metric_threshold` criterion compares that single value to a target. Training artifacts and logs, though, frequently carry *history*: a Hugging Face `log_history` array, JSONL step lines, a column of values. A **history-bearing reader** (the job-log reader, and the file readers for sequence-bearing formats) reduces that history to one number itself via an `aggregation` parameter, so a criterion can express goals like "best loss so far" at the source level — independent of, and complementary to, the cross-iteration history the engine accumulates for [`metric_trend`](#metric_trend-criterion).

| `aggregation` | Reduces the observed sequence to |
|---------------|----------------------------------|
| `last` (default) | The most recent numeric value. |
| `first` | The earliest numeric value. |
| `min` | The smallest numeric value. |
| `max` | The largest numeric value. |
| `mean` | The arithmetic mean of the numeric values. |

Non-numeric entries in the sequence are ignored before reducing. The applied mode is reported as a diagnostic field outside `metrics`. When the caller supplies no mode, the reader defaults to `last`, so the most recent value is returned. An out-of-set mode, an empty sequence, and a sequence with no numeric values each surface as a distinct error envelope code.

### The four reader tools

| Tool | Source | History-bearing? | Availability |
|------|--------|------------------|--------------|
| `metrics_cloudwatch_get` | A single CloudWatch `GetMetricStatistics` datapoint for a named metric/namespace/dimensions/region (most-recent datapoint selected deterministically). | No | default-on |
| `metrics_from_job_logs` | The tail of a job's logs, extracted by JSON key (dot-path) or regex (first capture group), reduced via the aggregation mode. | Yes | default-on |
| `metrics_from_shared_storage_file` | A metrics file a job wrote to EFS or the cluster shared bucket, dispatched on a `format` parameter (`json`, `csv`, `hf_trainer_state`, `jsonl`, `yaml`, `parquet`, and optional/stretch `tfevents`). | Yes for sequence-bearing formats | default-on |
| `metrics_from_local_file` | A metrics file on the **local filesystem**, confined to an allowlisted root. Reuses every piece of the shared-storage reader (same `format` set, aggregation handling, size cap, output shape, error model) and adds only local-path confinement. | Yes for sequence-bearing formats | **gated** by `GCO_ENABLE_LOCAL_METRICS`, default-off |

The three default-on readers are read-only against remote AWS resources, incur only normal API-call-rate charges, and are always present in `mcp.list_tools()` — referenceable from a Mission session's tool allowlist with no flag juggling.

`metrics_from_local_file` is the deliberate exception. Reading the MCP host's local filesystem is a real security concern even for a read-only tool, so it is gated default-off behind `GCO_ENABLE_LOCAL_METRICS` (or the umbrella `GCO_ENABLE_ALL_TOOLS=true`) and confined to the single root configured via `GCO_METRICS_LOCAL_ROOT`. Every read path is fully resolved — collapsing `..` segments and following symlinks — and rejected unless it stays inside that root: a `..` escape returns a `path_traversal_escape` envelope, a symlink whose target jumps out returns `symlink_escape`, and an enabled gate with no configured root returns `local_root_not_configured`. Its docstring begins with the literal prefix `[gated by GCO_ENABLE_LOCAL_METRICS]`.

### Example: observe training loss from an HF Trainer state file

A common case is driving validation/training loss down while a Hugging Face `Trainer` writes `trainer_state.json` (its `log_history` is a list of per-step dicts carrying `loss`, `eval_loss`, and friends). Point the file reader at the artifact, ask for the `loss` field with `format: hf_trainer_state`, and reduce with `aggregation: min` to track the best loss seen so far:

```jsonc
// tool call the strategy issues during the Execute phase
{
  "tool": "metrics_from_shared_storage_file",
  "args": {
    "path": "s3://cluster-shared/run-7/trainer_state.json",
    "region": "us-east-1",
    "field": "loss",
    "format": "hf_trainer_state",
    "aggregation": "min",
    "output_name": "loss"
  }
}
```

```json
// the matching metric_threshold criterion
{
  "criterion_id": "best_loss",
  "kind": "metric_threshold",
  "required": true,
  "metric": "metrics.loss",
  "op": "<=",
  "target": 0.1
}
```

The reader collects every `loss` entry from `log_history`, reduces to the minimum, and emits `{"metrics": {"loss": <best>}, "format": "hf_trainer_state", "aggregation": "min", ...}`. The Observe phase merges it, and the criterion compares `metrics.loss <= 0.1`. If the file is missing, malformed, or carries no numeric `loss`, the reader returns an error envelope instead and the criterion reads `inconclusive` — the loop keeps running.

### Cumulative metrics and the `metric_trend` criterion

There are two complementary ways to observe a metric's history, and they operate at different layers:

- **Reader-level reduction (`aggregation`)** collapses history that already exists *inside a single artifact* — a `log_history` array, a JSONL stream, a column — down to one number before it ever reaches the engine. Use it for "best/earliest/mean value within this file or log tail".
- **Engine-level history (`metric_trend`)** observes how a metric moves *across iterations* of the Mission loop. The engine accumulates a `metric_history` series for every numeric metric it sees (oldest→newest) in the Evaluate phase's cumulative observation — the same place it already accumulates `tool_results`. A [`metric_trend`](#metric_trend-criterion) criterion reads that series and evaluates its direction (`decreasing`, `increasing`, `non_increasing`, `non_decreasing`) over an optional `window`, with a `min_points` floor below which it stays `inconclusive`.

So "drive loss down and keep it falling" is expressible as two criteria over the same metric: a `metric_threshold` (`metrics.loss <= 0.1`) for the point-in-time floor, and a `metric_trend` (`direction: decreasing`) for the cross-iteration shape. The per-iteration `metrics` dict stays point-in-time exactly as before — `metric_history` lives alongside it on the cumulative view, so existing `metric_threshold`, `event`, and `predicate` criteria are unaffected, and predicates can read `obs['metric_history']` directly when they need the raw series.

## Scoring Progress with the Semantic-Progress Judge

The reader tools above surface a number that already exists somewhere — a CloudWatch datapoint, a field in a training artifact, a line in a log. But not every goal has a clean scalar to read. "Is the cluster getting closer to a healthy multi-region posture?" or "Is this investigation actually converging on the directive?" are real progress questions with no single number to point a `metric_threshold` at. The `metrics_semantic_progress` tool — the **semantic-progress judge** — fills that gap: it asks an LLM to score how close the Mission is to satisfying its directive, then emits that score in the same canonical metrics shape the reader tools use, so a plain criterion can watch it.

This is the **LLM-as-judge** pattern. The model is used as a scoring function — given the directive and recent progress context, return one number in `[0.0, 1.0]` (`0.0` = no progress toward the directive, `1.0` = directive fully satisfied) against a fixed, versioned rubric. The score then flows through the engine exactly like any other metric.

### Prerequisite: a gated, default-off tool that costs one LLM call

Each invocation makes one LLM call (a Bedrock cost on the CLI path, or a sampling round-trip on an MCP host that advertises sampling capability). Because that cost is real and incurred per call, the tool is **gated default-off** behind `GCO_ENABLE_SEMANTIC_PROGRESS` (or the umbrella `GCO_ENABLE_ALL_TOOLS=true`). With neither flag set the tool is absent from `mcp.list_tools()` and cannot be allowlisted into a session. Its docstring begins with the literal prefix `[gated by GCO_ENABLE_SEMANTIC_PROGRESS]`, and it carries the `safe` tag — it is strictly read-only, mutating nothing; it only reads its inputs and asks the model for a score.

```bash
export GCO_ENABLE_SEMANTIC_PROGRESS=true   # or GCO_ENABLE_ALL_TOOLS=true
```

The tool takes the directive plus an optional `recent_context` string (recent observations and/or metric-history the strategy chooses to pass), an optional `output_name` (the metric key, default `progress_score`), and an optional `model_id`. It obtains its model output through Mission's existing sampling seam — the same one [Sampling](#sampling) describes — so it works on both the MCP-context path and the Bedrock CLI path without configuring a backend of its own.

### The canonical shape and the `metrics.progress_score` dot-path

On success the judge returns the **canonical metrics shape** — the very shape the Observe phase merges:

```json
{
  "metrics": {"progress_score": 0.72},
  "rationale": "Two of the three regions report healthy nodepools; the third is still scaling.",
  "source": "bedrock:global.amazon.nova-2-lite-v1:0",
  "backend_name": "bedrock",
  "model_id": "global.amazon.nova-2-lite-v1:0",
  "rubric_version": "spj-v1",
  "raw_score": 0.72
}
```

The single number lands under `metrics.progress_score`, so the Observe phase merges it via `metrics.update(...)` and a criterion reads it by the dot-path `metrics.progress_score`. Everything else — `rationale`, `source`, `backend_name`, `model_id`, `rubric_version`, and the pre-clamp `raw_score` — is **provenance that lives strictly outside the `metrics` object**, so the merged `observation["metrics"]` dict only ever holds the one number. That provenance is what makes a non-deterministic datapoint auditable after the fact: a reviewer can see which backend and model produced the score, under which rubric version, and what the model's own rationale was.

The metric key follows the same `metrics.<name>` convention as the reader tools: pass `output_name` to rename it (constrained to a single dot-path segment — 1–128 characters, no `.` separator, no whitespace) and the criterion's `metric` field changes to match. The default is `progress_score`, so the dot-path is `metrics.progress_score` unless you override it.

A *failure* result is a structured error envelope (`{"code": "...", "details": {...}}`) with **no** top-level `metrics` key — exactly like a reader's failure. The Observe phase skips it, the criterion reads `inconclusive` rather than `unmet`, and the loop keeps running. A missing directive, an unavailable sampling backend, a transport error, or an unparseable model score all surface this way; nothing escapes the tool to crash the session.

### The determinism boundary

The judge's whole design rests on one property: **all the non-determinism lives in a single place**. The only non-deterministic step is the one `sample()` call to the model. Prompt construction (around the fixed, versioned rubric), score parsing, clamping into `[0.0, 1.0]`, output-name validation, and the canonical-shape building are every one of them deterministic given their inputs — and so is the downstream criterion comparison the engine performs over the emitted number.

That makes a judge score *exactly analogous to a CloudWatch read*. A CloudWatch datapoint varies between calls because the world changed, not because the comparison is fuzzy; the `metric_threshold` that reads it is still a plain, reproducible `>=`. A judge score varies between calls because the model's answer varies — but once the number is on the Observation, the criterion comparing it is byte-for-byte the same deterministic comparison the engine runs on any numeric metric. There is no judge-specific code path in the engine, and the same score value always yields the same `met`/`unmet`/`inconclusive` result on repeated evaluation. The per-call variability of the score is expected and treated like the variability of any external reading — the loop's *decision* over the score stays reproducible.

### Example: a threshold and a trend over the judge's score

Because the judge emits an ordinary number under `metrics.progress_score`, the two history-aware and point-in-time criterion kinds consume it with no special handling.

A `metric_threshold` criterion completes the session once the judge is confident the directive is essentially satisfied:

```json
{
  "criterion_id": "progress_threshold",
  "kind": "metric_threshold",
  "required": true,
  "metric": "metrics.progress_score",
  "op": ">=",
  "target": 0.8
}
```

A `metric_trend` criterion asserts the run is actually moving toward the goal rather than stalling — the engine accumulates each iteration's `progress_score` into the `metric_history` series and the criterion checks its direction:

```json
{
  "criterion_id": "progress_climbing",
  "kind": "metric_trend",
  "required": true,
  "metric": "metrics.progress_score",
  "direction": "increasing"
}
```

Pair the two to express "the judge says we're nearly done **and** progress has been climbing across iterations" — one point-in-time floor, one cross-iteration shape, both reading the same `metrics.progress_score` the judge emits. Allowlist `metrics_semantic_progress` into the session so the strategy can call it during the Execute phase, and remember it only resolves when `GCO_ENABLE_SEMANTIC_PROGRESS` (or `GCO_ENABLE_ALL_TOOLS`) is set.

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
| `--tool-allowlist NAME` | repeatable | One per allowlisted tool. The first allowlisted tool also seeds the deterministic strategy when sampling is off. Required unless `--allow-all-tools` is set; mutually exclusive with it. |
| `--allow-all-tools` | off | Resolve the allowlist to every registered tool (minus the `mission_*` control tools). Makes `--tool-allowlist` optional; mutually exclusive with it. See [Tool Allowlist](#tool-allowlist). |
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

## Tool Allowlist

Every Mission session carries a **tool allowlist** — the set of tool names the loop may invoke during its Execute phase and inside scripted strategies. By default you spell that list out tool-by-tool: `--tool-allowlist NAME` (repeatable) on the CLI, or the `tool_allowlist` array on the `mission_start` MCP tool. The allowlist is resolved and persisted on the session at start time, so the iteration loop, the script sandbox, the strategy-revision sampling prompt, and the Final_Report all see exactly the same concrete list.

### `--allow-all-tools` / `allow_all_tools`

Enumerating every tool name is tedious when you simply want the loop to reach for whatever the server exposes. Both surfaces accept an all-tools option that resolves the session's allowlist to **every currently-registered tool** (minus the nine `mission_*` control tools — see [Risk-tier scope](#risk-tier-scope)) without naming each one:

| Surface | Input | Default |
|---------|-------|---------|
| `gco mission start` / `gco mission run` | `--allow-all-tools` (boolean flag) | disabled |
| `mission_start` MCP tool | `allow_all_tools: bool` parameter | `false` |

When the option is set, the effective allowlist is **resolved once at session-start time** to the concrete list of tool names registered with the MCP server at that moment, and that enumerated list is persisted on the session exactly as if you had typed every name. The persisted list does not change if the registered set changes later in the session.

On the CLI, `--allow-all-tools` makes `--tool-allowlist` **optional** — you can omit it entirely:

```bash
export GCO_ENABLE_MISSION=true

# Start a session against every registered tool, no per-tool enumeration:
gco mission start \
  --directive "Investigate why the us-east-1 queue is backing up." \
  --criteria-file criteria.json \
  --max-iterations 10 --max-wall-clock 3600 \
  --allow-all-tools

# Same for the one-command run. With no explicit allowlist, the criteria
# scaffolder falls back to its directive-only path, then the session's
# allowlist is filled from the registry:
gco mission run \
  --directive "Find documentation about inference endpoints." \
  --max-iterations 5 --max-wall-clock 300 \
  --allow-all-tools
```

The MCP equivalent passes `allow_all_tools: true` and omits `tool_allowlist`:

```jsonc
{
  "directive": "Investigate why the us-east-1 queue is backing up.",
  "criteria": [ /* ... */ ],
  "budget": {"max_iterations": 10, "max_wall_clock_seconds": 3600},
  "allow_all_tools": true
}
```

**Interaction with `--tool-allowlist` (mutually exclusive).** The all-tools option and an explicit allowlist are mutually exclusive. If you set `--allow-all-tools` (or `allow_all_tools: true`) **and** also supply a non-empty `--tool-allowlist` / `tool_allowlist`, the request is rejected with a `validation_error` whose `details.reason` is `allow_all_and_explicit_allowlist_mutually_exclusive`, and no session is persisted. Supply one or the other, never both.

**Empty registry.** If the all-tools option is set but nothing resolvable is registered (the registry is empty, or only the `mission_*` control tools are registered), the request is rejected with `validation_error` / `allow_all_tools_empty_registry`, and no session is persisted — you never start a session that can invoke no tools.

| `details.reason` | When |
|------------------|------|
| `allow_all_and_explicit_allowlist_mutually_exclusive` | `--allow-all-tools` set **and** a non-empty `--tool-allowlist` supplied. |
| `allow_all_tools_empty_registry` | `--allow-all-tools` set but the resolved set (registered tools minus the `mission_*` control tools) is empty. |
| `empty` | Neither option set nor any `--tool-allowlist` supplied (the existing empty-allowlist error). |

### Distinct from `GCO_ENABLE_ALL_TOOLS`

The per-session all-tools option is **not** the same thing as the `GCO_ENABLE_ALL_TOOLS` environment variable, even though both mention "all tools". They operate at different layers and never substitute for one another:

| Mechanism | Layer | Controls |
|-----------|-------|----------|
| `GCO_ENABLE_ALL_TOOLS` (environment variable) | server startup / **registration** | *which tools get registered* with the MCP server. |
| `--allow-all-tools` / `allow_all_tools` (per-session option) | a single Mission session | *which already-registered tools that one session may call*. |

The per-session option resolves its allowlist **from whatever is registered** — it never widens reach beyond the registered set. An operator who has not enabled the destructive, infrastructure, or cost-incurring feature flags will never see those tools registered, so they cannot enter a session's resolved allowlist. Setting `GCO_ENABLE_ALL_TOOLS=true` widens what `--allow-all-tools` resolves to *only* by virtue of registering more tools in the first place. See [Feature Flags](../gco_mcp/README.md#feature-flags) for the registration-layer flags.

### Risk-tier scope

Every MCP tool carries a risk-tier tag (`safe`, `low-risk`, `destructive`, `infrastructure`, `cost-incurring`, `data-upload`, `image`). The all-tools option resolves to **all registered tools regardless of risk tier** — it does not stop at read-only `safe` tools. The blast-radius control is the **registration layer**, not this option: a destructive, infrastructure, or cost-incurring tool enters scope only if its gating feature flag was set when the server registered tools, in which case you have already opted into exposing it. Concretely, registering those higher-risk tools via their feature flags (including the `GCO_ENABLE_ALL_TOOLS` umbrella) brings them into scope of the all-tools expansion.

The **only** names excluded from the expansion are the nine `mission_*` control tools (`mission_start`, `mission_status`, `mission_iterate`, `mission_checkpoint`, `mission_complete`, `mission_abort`, `mission_resume`, `mission_history`, `mission_list`), so a Mission session can never recursively invoke the session-management tools that drive it. If you want a session bounded to read-only tools only, use an explicit `--tool-allowlist` of `safe`-tagged tools instead of `--allow-all-tools`.

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

- Model — `cdk.json` `context.bedrock.default_model_id` (stock value:
  `global.amazon.nova-2-lite-v1:0`, Amazon Nova 2 Lite's global inference
  profile). The stock `context.bedrock.thinking.effort` is `high`, Nova 2
  Lite's maximum; it can materially increase billed output tokens and latency.
  Explicit model overrides do not inherit this Nova-specific reasoning field.
  Override via `GCO_MISSION_BEDROCK_MODEL_ID` or `--bedrock-model-id`; see
  [Bedrock Model Selection](CUSTOMIZATION.md#bedrock-model-selection).
- Region — `us-east-1`. Override via `GCO_MISSION_BEDROCK_REGION`.

Every sampling attempt emits one structured audit event (`sampling_purpose`, `sampling_status`, `sampling_backend`, `sampling_model_id`, `model_output_bytes`, `validation_error`). Sampling rejections (transport errors, malformed JSON, schema mismatch, allowlist or budget violations, AST rejections on proposed scripts) cause an automatic deterministic fallback — the iteration still runs.

### Environment context

When the MCP `mission_*` tools build the strategy-revision sampler they pass an optional `environment_context` field into the prompt — a once-per-session snapshot of slow-moving live signals so the model is not flying blind on iteration 1. The block lands under `=== Environment context (slow-moving live signals) ===` between the budget block and the iteration history.

Three top-level keys:

- `regions` — sorted list of deployed regions discovered through the same `MultiRegionCapacityChecker` used by `ai_recommend`.
- `cluster_metrics` — per-region snapshot keyed by region. Each entry carries `queue_depth`, `running_jobs`, `pending_jobs`, `gpu_utilization`, `cpu_utilization`, `recommendation_score`. Mirrors the fields `RegionCapacity` exposes; matches the data shape `ai_recommend` already feeds into its own model prompts.
- `reservations` — `{"active_count": int, "by_region": {region: int}}`. Counts only — the full reservation list is reachable through `list_reservations` if the model wants the detail.

The block is byte-capped at 4 KB. When a section overshoots (e.g. a probe returned an unusually large payload), the largest field is dropped first and the dropped key list is recorded under `_dropped_fields` so the operator can spot the gap. Top-level keys are sorted before emission so two callers with semantically-identical context produce a byte-identical prompt — the same property the determinism tests pin down for Observation summaries.

What deliberately does **not** land in the block: spot prices and on-demand prices (large, AZ-fanout, cheap to fetch on demand from `spot_prices` once the model has picked an instance shape), capacity-block offerings (reachable through `reservation_check`), and anything timestamp-stamped to second precision (a wall-clock leak would break the byte-identical determinism property in `tests/test_mission_sampling.py`).

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

`gco mission start --help` prints the full option list — directive text, criteria file path, the iteration and wall-clock caps (each accepting `-1` to opt out), the tool allowlist, cadence parameters, stagnation threshold, sampling toggles, and the scripted-strategy opt-in. The `--tool-allowlist` option (repeatable) is required unless `--allow-all-tools` is set; the two are mutually exclusive. `--allow-all-tools` (default disabled) resolves the session's allowlist to every registered tool and is available on both `start` and `run` — see [Tool Allowlist](#tool-allowlist).

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

`mission_start` accepts an `allow_all_tools` boolean parameter (default `false`). When `true`, the session's `tool_allowlist` is resolved to every registered tool (minus the `mission_*` control tools) and `tool_allowlist` may be omitted; supplying both a non-empty `tool_allowlist` and `allow_all_tools: true` is rejected as mutually exclusive. See [Tool Allowlist](#tool-allowlist) for the full semantics, the empty-registry error, and the distinction from `GCO_ENABLE_ALL_TOOLS`.

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

For a list of every gated tool and the flag that controls it, see [Feature Flags](../gco_mcp/README.md#feature-flags).
