# Mission Judge — LLM-as-Judge Progress Scoring

`mcp/mission_judge/` is the dependency-light core behind GCO's read-only
**semantic-progress judge** MCP tool. The tool scores how close a Mission is to
satisfying its natural-language directive and returns that score in the
canonical `{"metrics": {"progress_score": <number>}}` shape the Mission Observe
phase merges, so a plain `metric_threshold` (e.g. `progress_score >= 0.8`) or
`metric_trend` (e.g. `progress_score` increasing) criterion can read it with no
special handling.

This package is **pure** and free of FastMCP: it builds the prompt, owns the
fixed rubric, and parses the model's answer. The single non-deterministic step —
the model call itself — lives in the thin `@mcp.tool` wrapper at
[`mcp/tools/semantic_progress.py`](../tools/semantic_progress.py), which reuses
the Mission sampling backend rather than reconstructing one. That split is the
key to customizing safely: changing *how progress is scored* is almost always a
prompt or rubric edit here, not a wrapper change.

The tool is **off by default**. Enable it by setting
`GCO_ENABLE_SEMANTIC_PROGRESS=true` (or the umbrella `GCO_ENABLE_ALL_TOOLS`) for
the MCP server, because each invocation incurs one LLM call. See
[Feature Flags](../README.md#feature-flags).

## Table of Contents

- [Overview](#overview)
- [Module Map](#module-map)
- [The Scoring Flow](#the-scoring-flow)
- [Customizing for Your Use Case](#customizing-for-your-use-case)
  - [Edit the Rubric (and Bump the Version)](#edit-the-rubric-and-bump-the-version)
  - [Reshape the Prompt](#reshape-the-prompt)
  - [Tune the Context and Rationale Budgets](#tune-the-context-and-rationale-budgets)
  - [Change the Score Range or Field Names](#change-the-score-range-or-field-names)
  - [Loosen or Tighten Response Parsing](#loosen-or-tighten-response-parsing)
  - [Change the Default Metric Key or Model](#change-the-default-metric-key-or-model)
  - [Add a New Failure Code](#add-a-new-failure-code)
- [Design Principles](#design-principles)
- [Testing Your Changes](#testing-your-changes)
- [Related Documentation](#related-documentation)

## Overview

The judge has exactly two possible outcomes, mirroring the metric readers:

1. **Success** — one finite float in `[0.0, 1.0]` wrapped by
   [`metrics_result`](shape.py) as
   `{"metrics": {"progress_score": <score>}, ...provenance}`. Provenance
   (rationale, source, backend name, model id, rubric version, and the
   pre-clamp `raw_score`) sits beside `metrics`, never inside it.
2. **Failure** — a [`JudgeError`](shape.py) rendered by the wrapper into an
   `{"code", "details"}` envelope via [`error_envelope`](shape.py), which never
   carries a top-level `metrics` key, so a failed score merges as
   `inconclusive` and the Mission loop keeps running.

Everything that builds the request is a pure function of its inputs, so two
identical inputs produce a byte-identical prompt. The only place randomness
enters is the model's answer.

## Module Map

| Module | Responsibility | The knob you'll most likely turn |
|--------|----------------|----------------------------------|
| [`rubric.py`](rubric.py) | The fixed, versioned scoring guidance folded into every prompt. | `RUBRIC` text and `RUBRIC_VERSION`. |
| [`prompt.py`](prompt.py) | `truncate_context`, the frozen `JudgePrompt`, and `build_prompt` — assemble the deterministic prompt. | `assemble()` layout; `MAX_CONTEXT_CHARS`. |
| [`score.py`](score.py) | `parse_score` (the only failure path) and `clamp_score` (a total function onto `[0.0, 1.0]`). | Parsing tolerance; the bounds. |
| [`shape.py`](shape.py) | `ErrorCode`, `JudgeError`, output-name validation, the finite-float guard, and the success/failure builders. | `ErrorCode` strings; the provenance fields. |
| [`__init__.py`](__init__.py) | Package docstring only — no exports to maintain. | — |

## The Scoring Flow

The wrapper drives these pure pieces in a fixed order:

1. **Validate** the optional `output_name` and require a non-empty `directive`
   ([`shape.validate_output_name`](shape.py)).
2. **Build the prompt** — `build_prompt(directive, recent_context, RUBRIC_VERSION)`
   truncates context keep-newest and binds the rubric ([`prompt.py`](prompt.py)).
3. **Sample** — the wrapper selects a Mission sampling backend and calls
   `backend.sample(prompt)`. This is the only non-deterministic step and is not
   retried.
4. **Parse** — `parse_score` decodes the model's JSON, validating a real, finite
   numeric `score` field, and returns `(raw_score, rationale)`
   ([`score.py`](score.py)).
5. **Clamp** — `clamp_score` folds the raw score onto `[0.0, 1.0]`.
6. **Wrap** — `metrics_result` emits the canonical shape with the clamped score
   and records `raw_score` verbatim as provenance.

## Customizing for Your Use Case

### Edit the Rubric (and Bump the Version)

The rubric in [`rubric.py`](rubric.py) is the scoring yardstick. If the default
`0.0`–`1.0` progress scale doesn't match how you want progress judged — say you
want the model to weight a passing eval more heavily, or to anchor the
milestones to your domain — edit the `RUBRIC` string.

**Always bump `RUBRIC_VERSION` in the same edit.** The version is recorded in
every result's provenance so a historical score can be traced back to the exact
guidance that produced it; the two constants are intentionally kept side by side
so changing one without the other is obvious in review.

```python
RUBRIC_VERSION: str = "spj-v2"  # was "spj-v1"
RUBRIC: str = """\
You are scoring a fine-tuning run against its objective. Return a score in
[0.0, 1.0]:
  - 0.0  no eval improvement over baseline
  - 0.5  eval metric halfway to the target threshold
  - 1.0  eval metric at or past the target threshold
...
"""
```

Keep the text byte-identical across invocations — never interpolate a clock, a
random value, or other ambient state into `RUBRIC`, or you lose the determinism
guarantee the tests assert.

### Reshape the Prompt

`JudgePrompt.assemble` in [`prompt.py`](prompt.py) renders the fixed
`=== Section ===` layout: the rubric, the directive, the recent progress
context, and the output-format instruction. To add a section (for example a
"Known constraints" block) or reorder them, edit `assemble` and add the
corresponding field to the frozen `JudgePrompt` dataclass and to `build_prompt`.

Two rules keep the contract intact:

- The output-format section must keep instructing the model to answer with a
  single JSON object carrying a numeric `score` and a `rationale` — that's what
  `parse_score` depends on.
- `assemble` must stay a pure function of its bound fields (no clock, no
  randomness), so identical inputs keep producing an identical string.

### Tune the Context and Rationale Budgets

Two character budgets live at the top of [`prompt.py`](prompt.py):

| Constant | Default | Controls |
|----------|---------|----------|
| `MAX_CONTEXT_CHARS` | `8000` | How much recent progress context is folded into the prompt. Oversized context is truncated **keep-newest** (oldest dropped, `TRUNCATION_MARKER` prepended). |
| `MAX_RATIONALE_CHARS` | `2000` | How much of the model's rationale is retained in provenance (the wrapper slices to this). |

Raise `MAX_CONTEXT_CHARS` to give the model more history at the cost of a larger
prompt (and token spend); lower it to keep prompts tight. `truncate_context`
guarantees the result — marker included — never exceeds the limit, so the bound
holds for any value.

### Change the Score Range or Field Names

The interval bounds and the expected JSON field names are constants in
[`score.py`](score.py):

```python
SCORE_FIELD = "score"
RATIONALE_FIELD = "rationale"
_LOWER_BOUND = 0.0
_UPPER_BOUND = 1.0
```

If you move to a different scale (e.g. `0`–`100`), change `_LOWER_BOUND` /
`_UPPER_BOUND` **and** the rubric so the model is told the same range it's
clamped to. If you rename the fields, update both the constants here and the
output-format instruction in [`prompt.py`](prompt.py) so the model is asked for
exactly what `parse_score` looks for. Note the downstream criterion compares
against whatever range you emit, so a `progress_score >= 0.8` example becomes
`progress_score >= 80` on a 0–100 scale.

### Loosen or Tighten Response Parsing

`parse_score` in [`score.py`](score.py) is the only failure path and is
deliberately forgiving of how chat models wrap JSON: `_strip_code_fence` peels a
whole-response Markdown fence, and `_extract_json_payload` falls back to the
substring spanning the first `{` and last `}`. If your model wraps answers
differently (XML tags, a custom delimiter), extend `_extract_json_payload` to
peel that wrapper before the decode. If you want *stricter* parsing (reject
anything that isn't already clean JSON), remove the fallback carve-out and let a
non-JSON payload raise `INVALID_MODEL_SCORE` directly.

Whatever you change, preserve the rejection rules the tests rely on: a missing
`score`, a `bool`, a string, `null`, `NaN`, or `±inf` must all still raise
`JudgeError(INVALID_MODEL_SCORE)` with a `reason` in `details`.

### Change the Default Metric Key or Model

These two live in the wrapper, [`mcp/tools/semantic_progress.py`](../tools/semantic_progress.py):

- **Default metric key** — when `output_name` is omitted the key defaults to
  `"progress_score"`. Change the literal in the wrapper if you want a different
  default key under `metrics`. (A caller can already override it per call.)
- **Model** — the wrapper forwards an optional `model_id` to
  `select_sampling_backend`; `None` uses the sampling seam's resolved default.
  To change the default model for *every* call, configure the Mission sampling
  backend rather than editing this tool — it shares the same seam as the rest of
  Mission.

### Add a New Failure Code

`ErrorCode` in [`shape.py`](shape.py) is a frozen vocabulary of stable strings
(`invalid_output_name`, `missing_directive`, `no_sampling_backend`,
`sampling_transport_error`, `invalid_model_score`). To add one:

1. Add the class attribute with a short, lowercase, snake_case value.
2. Raise `JudgeError(NEW_CODE, {...details})` from the relevant pure helper.
3. Render it in the wrapper's `except JudgeError` path (already generic via
   `error_envelope(err.code, **err.details)`, so usually no wrapper edit is
   needed).

Don't rename existing codes — callers, tests, and operators branch on the exact
strings.

## Design Principles

- **Deterministic prompt, single point of non-determinism.** Everything that
  builds the request is pure; only `backend.sample(...)` is non-deterministic,
  and it is never retried.
- **The rubric is versioned on purpose.** Any edit to `RUBRIC` must bump
  `RUBRIC_VERSION` so a recorded score stays interpretable.
- **One finite float or a structured error — never a crash.** The wrapper's
  `except Exception` catch-all guarantees nothing escapes the tool boundary; a
  failure becomes an envelope and the Mission loop continues.
- **Pure core, thin wrapper.** FastMCP, the sampling transport, and the event
  loop live in `mcp/tools/semantic_progress.py`; the scoring logic lives here.
- **The finite-float guard is the single gate.** `is_finite_float` decides what
  can stand in for the score (rejecting `bool`, `NaN`, `±inf`). Route values
  through it.

## Testing Your Changes

Each module has a dedicated, prefixed test module under `tests/`
(`test_semantic_progress_prompt.py`, `test_semantic_progress_score.py`,
`test_semantic_progress_shape.py`, plus the `test_semantic_progress_tool.py` and
`test_semantic_progress_observe.py` integration modules; the rubric is exercised
through the prompt tests). Add cases to the matching file:

```bash
pytest tests/test_semantic_progress_prompt.py -q   # after a prompt.py / rubric.py change
pytest tests/ -k semantic_progress -q              # the whole judge suite
```

The determinism tests assert that two `build_prompt(...).assemble()` calls
produce a byte-identical string — if you add ambient state to the prompt those
will (correctly) fail. The doc-hygiene guard (`tests/test_doc_hygiene.py`) scans
this package's `.py` files for planning-doc breadcrumbs; this README is out of
scope, but keep new source comments free of spec references.

## Related Documentation

- [`mcp/tools/semantic_progress.py`](../tools/semantic_progress.py) — the
  flag-gated tool wrapper that drives this package.
- [`mcp/README.md`](../README.md#metrics) — the Metrics tool table and the
  [Feature Flags](../README.md#feature-flags) reference
  (`GCO_ENABLE_SEMANTIC_PROGRESS`).
- [`mcp/metric_readers/README.md`](../metric_readers/README.md) — the sibling
  read-only metric readers that emit the same canonical shape.
- [`mcp/mission/README.md`](../mission/README.md) — the Mission loop whose
  Observe phase merges this score, and its sampling seam.
- [`docs/MISSION.md`](../../docs/MISSION.md) — the user-facing Mission guide and
  `metric_threshold` / `metric_trend` criteria.
