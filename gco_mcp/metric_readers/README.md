# Metric Readers — Pure Helpers Behind the Read-Only Metric Tools

`gco_mcp/metric_readers/` is the dependency-light core behind GCO's read-only
**metric-reader** MCP tools. Each reader turns one external source — a
CloudWatch datapoint, a job's log tail, a metrics file on shared storage, or a
confined local file — into a single finite number wrapped in the canonical
`{"metrics": {"<key>": <number>}}` shape the Mission Observe phase merges, so a
`metric_threshold` or `metric_trend` criterion can read it with zero scripting.

This package is **pure**: no FastMCP, no decorators, no event loop. The thin
`@mcp.tool` wrappers that call it live in
[`gco_mcp/tools/metrics.py`](../tools/metrics.py). That separation is the single
most useful thing to understand before customizing — almost every change you
will want to make is a small edit to one pure helper here, and the tool surface
picks it up for free.

## Table of Contents

- [Overview](#overview)
- [Module Map](#module-map)
- [The Result Contract](#the-result-contract)
- [Customizing for Your Use Case](#customizing-for-your-use-case)
  - [Add a New Aggregation Mode](#add-a-new-aggregation-mode)
  - [Add a New File Format](#add-a-new-file-format)
  - [Add a New Error Code](#add-a-new-error-code)
  - [Tune the Limits and Bounds](#tune-the-limits-and-bounds)
  - [Adjust CloudWatch Auth Classification](#adjust-cloudwatch-auth-classification)
  - [Change the Local-Root Confinement Policy](#change-the-local-root-confinement-policy)
  - [Add a Whole New Reader Source](#add-a-whole-new-reader-source)
- [Design Principles](#design-principles)
- [Testing Your Changes](#testing-your-changes)
- [Related Documentation](#related-documentation)

## Overview

A metric reader has exactly two possible outcomes:

1. **Success** — one real, finite number wrapped by
   [`metrics_result`](shape.py) as `{"metrics": {key: value}, ...provenance}`.
2. **Failure** — a [`MetricReaderError`](shape.py) raised internally and
   rendered by the tool wrapper into an `{"code", "details"}` envelope via
   [`error_envelope`](shape.py).

The envelope deliberately carries no top-level `metrics` key, so a failed read
merges as `inconclusive` and the Mission loop keeps running rather than acting
on bad data. Every reader honours this contract, which is why the tools "never
raise" — the failure path is data, not an exception that escapes.

## Module Map

| Module | Responsibility | The knob you'll most likely turn |
|--------|----------------|----------------------------------|
| [`shape.py`](shape.py) | The shared building blocks: `ErrorCode`, `MetricReaderError`, metric-name validation, the numeric guard, and the success/failure builders. | `ErrorCode` strings; `_MAX_METRIC_NAME_LEN`. |
| [`aggregate.py`](aggregate.py) | `reduce_sequence` — collapse a sequence of values to one number via `last`/`first`/`min`/`max`/`mean`. | `VALID_MODES` and the reducer body. |
| [`cloudwatch.py`](cloudwatch.py) | Read-only `GetMetricStatistics`: pick the most-recent datapoint, classify failures. | `_UNAUTHORIZED_ERROR_CODES`. |
| [`files.py`](files.py) | Per-format handlers (`json`, `csv`, `yaml`, `jsonl`, `hf_trainer_state`, `parquet`, `tfevents`) and the `_HANDLERS` dispatch map. | `_HANDLERS` — add or swap a format. |
| [`logs.py`](logs.py) | Pull candidate scalars out of log lines by JSON key or regex, then coerce to numbers. | Extraction/coercion rules. |
| [`localfs.py`](localfs.py) | `resolve_within_root` — confine a caller-supplied path to an allowlisted root with realpath containment. | The confinement policy. |
| [`__init__.py`](__init__.py) | Package docstring only — no exports to maintain. | — |

## The Result Contract

Two builders in [`shape.py`](shape.py) own the wire shape, and you should route
every new reader through them rather than hand-rolling a dict:

```python
from metric_readers.shape import metrics_result, error_envelope

# success — provenance lands beside `metrics`, never inside it
metrics_result("loss", 0.0123, source="my_source:run-42", region="us-east-1")
# -> {"source": "...", "region": "...", "metrics": {"loss": 0.0123}}

# failure — no top-level `metrics`, so the Observe merge skips it
error_envelope("my_new_code", reason="whatever happened", extra="context")
# -> {"code": "my_new_code", "details": {"reason": "...", "extra": "..."}}
```

`metrics_result` asserts the value is a real, finite number (via
`is_numeric_value`), so a `bool`, `NaN`, or `±inf` can never reach a criterion.
Keep that guarantee intact in anything you add.

## Customizing for Your Use Case

### Add a New Aggregation Mode

Say you want a `median` or `p95` reduction in addition to the five built-ins.

1. In [`aggregate.py`](aggregate.py), add the name to both the `AggregationMode`
   `Literal` and the `VALID_MODES` frozenset.
2. Add the reduction branch in `reduce_sequence`, after the numeric filter so it
   only ever sees real, finite numbers in original order.

```python
VALID_MODES = frozenset({"last", "first", "min", "max", "mean", "median"})

# ... inside reduce_sequence, after the `numeric` list is built ...
if mode == "median":
    ordered = sorted(numeric)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2
```

The tool wrappers validate `aggregation` against `VALID_MODES` before any I/O,
so the new mode becomes selectable on every sequence-bearing reader
(`metrics_from_job_logs`, `metrics_from_shared_storage_file`,
`metrics_from_local_file`) at once — no wrapper edit required. Update the
`aggregation` docstrings in [`gco_mcp/tools/metrics.py`](../tools/metrics.py) so the
new mode is discoverable.

### Add a New File Format

The file reader dispatches on a `format` string through the `_HANDLERS` map in
[`files.py`](files.py). To support, for example, a TOML metrics file:

1. Write a handler with the shared signature
   `(content: bytes, field: str, mode: str) -> float`. Resolve the field, then
   return a single number directly or hand a sequence to `reduce_sequence`.
2. Register it in `_HANDLERS`.

```python
def _handle_toml(content: bytes, field: str, mode: str) -> float:
    import tomllib  # lazy import keeps the baseline import surface light
    try:
        parsed = tomllib.loads(_decode(content, "toml"))
    except tomllib.TOMLDecodeError as exc:
        raise shape.MetricReaderError(shape.ErrorCode.MALFORMED_FILE, {"format": "toml"}) from exc
    return _reduce_resolved(_resolve_dot_path(parsed, field), field, mode)

_HANDLERS["toml"] = _handle_toml
```

Conventions to follow so your handler behaves like the others:

- **Lazy-import heavy dependencies inside the handler** (as `parquet` and
  `tfevents` do) and translate a missing wheel into
  `FORMAT_DEPENDENCY_UNAVAILABLE` rather than letting `ImportError` escape.
- **Map every parse failure** to `MALFORMED_FILE`, a missing field to
  `FIELD_NOT_FOUND`, and a present-but-non-numeric value to
  `NON_NUMERIC_VALUE`.
- The wrapper reports any `format` absent from `_HANDLERS` as
  `UNSUPPORTED_FORMAT`, so an unregistered name fails cleanly on its own.

Both file-reading tools share `_HANDLERS`, so a new format works on shared
storage and the local reader simultaneously. Add the name to the `format`
docstring in [`gco_mcp/tools/metrics.py`](../tools/metrics.py).

### Add a New Error Code

Failure codes are stable strings on the `ErrorCode` class in
[`shape.py`](shape.py) — callers, tests, and operators branch on the exact
value, so treat them as a frozen vocabulary. To add one:

1. Add the class attribute with a short, lowercase, snake_case value.
2. Raise it from your reader with a `details` payload that explains *why*.
3. If a tool wrapper has a catch-all `except Exception`, decide whether your new
   code is reachable there or only on a specific raise.

Use the `details.kind` discriminator pattern (as `AWS_UNREACHABLE` and
`LOG_RETRIEVAL_FAILED` do) when one code covers several related causes an
operator should still be able to tell apart, rather than minting a separate
code for each.

### Tune the Limits and Bounds

The guard rails are module-level constants, so changing one is a one-line edit:

| Constant | Location | Controls |
|----------|----------|----------|
| `_MAX_METRIC_NAME_LEN` | [`shape.py`](shape.py) | Max length of a metric key (default 128). |
| `_DEFAULT_KEY_FALLBACK` | [`shape.py`](shape.py) | Key used when a source hint sanitizes to nothing. |
| `_TAIL_MIN` / `_TAIL_MAX` / `_TAIL_DEFAULT` | [`gco_mcp/tools/metrics.py`](../tools/metrics.py) | Job-log tail clamp range and default. |
| `_MAX_BYTES_DEFAULT` | [`gco_mcp/tools/metrics.py`](../tools/metrics.py) | File reader size cap (default 10 MiB). |

The size cap is enforced by a `stat` **before** the file is read into memory, so
raising it trades memory for the ability to read larger artifacts — keep that
trade-off in mind on a memory-constrained MCP host.

### Adjust CloudWatch Auth Classification

[`cloudwatch.py`](cloudwatch.py) sorts `ClientError`s into `unauthorized` vs
`client_error` using the `_UNAUTHORIZED_ERROR_CODES` frozenset. If your account
surfaces a credentials/permissions error code that isn't in the set (so it's
being mislabelled `client_error`), add the AWS `Error.Code` string there. This
only changes the `details.kind` discriminator an operator reads — the reader
still leaves the criterion inconclusive either way.

### Change the Local-Root Confinement Policy

[`localfs.py`](localfs.py) `resolve_within_root` is the only place a
caller-supplied path is turned into a real filesystem location, and it is the
security boundary for the flag-gated `metrics_from_local_file` tool. It resolves
with realpath semantics (collapsing `..` and following symlinks) and rejects
anything that escapes the configured root, distinguishing `PATH_TRAVERSAL_ESCAPE`
from `SYMLINK_ESCAPE`.

This is a **security-sensitive** function — change it deliberately:

- The root itself is read at the tool boundary from `GCO_METRICS_LOCAL_ROOT` and
  passed in as an argument; the helper never reads the environment. Keep it that
  way so the policy stays a pure function of its inputs and stays testable.
- If you want to *forbid* symlink following entirely, replace the realpath
  containment with an `os.path.realpath` check that also verifies no component
  is a symlink — don't loosen the containment test.
- Whatever you change, the function must still never open or read a file.

### Add a Whole New Reader Source

To surface a brand-new source (say, a Prometheus query):

1. Add a pure module here, for example `prometheus.py`, that does the I/O and
   returns a `(value, provenance...)` result or raises `MetricReaderError`.
   Reuse `aggregate.reduce_sequence` if the source returns a series.
2. Add any new `ErrorCode`s it needs to [`shape.py`](shape.py).
3. Add a thin `@mcp.tool` + `@audit_logged` wrapper in
   [`gco_mcp/tools/metrics.py`](../tools/metrics.py) that calls your module, runs
   blocking I/O via `asyncio.to_thread`, wraps the body in
   `try/except MetricReaderError` plus a final `except Exception` mapping to a
   stable catch-all code, and returns through `metrics_result` /
   `error_envelope`.
4. If the source touches anything sensitive (the local host, a privileged
   endpoint), gate the registration behind a feature flag the way
   `metrics_from_local_file` is gated — wrap the decorator in
   `if is_enabled("GCO_ENABLE_..."):`. See
   [Feature Flags](../README.md#feature-flags).
5. Add a row to the Metrics table in [`gco_mcp/README.md`](../README.md#metrics) and
   update the count in [`gco_mcp/tools/README.md`](../tools/README.md).

## Design Principles

- **Pure core, thin wrapper.** Everything here is free of FastMCP, the event
  loop, and ambient state. The wrapper in `gco_mcp/tools/metrics.py` owns I/O,
  `asyncio.to_thread`, and the FastMCP decorator; the helpers own the logic.
- **One number or a structured error — never a crash.** A reader either returns
  a finite number or raises a coded `MetricReaderError`. The wrapper's
  `except Exception` catch-all guarantees nothing escapes the tool boundary.
- **The numeric guard is the single gate.** `is_numeric_value` is the one place
  that decides what counts as a real, finite number (rejecting `bool`, `NaN`,
  and the infinities). Route every value through it.
- **Stable codes are a public contract.** `ErrorCode` values are depended on by
  callers and tests — add to the vocabulary, don't rename existing entries.
- **Read-only, always.** No reader writes, moves, or deletes a source artifact.
  Preserve that invariant in anything you add.

## Testing Your Changes

Each module has a dedicated, prefixed test module under `tests/`
(`test_metric_readers_aggregate.py`, `test_metric_readers_files.py`,
`test_metric_readers_cloudwatch.py`, `test_metric_readers_localfs.py`,
`test_metric_readers_logs.py`, `test_metric_readers_shape.py`, and the
`_tools` / `_observe` integration modules). Add cases to the matching file:

```bash
pytest tests/test_metric_readers_aggregate.py -q   # after an aggregate.py change
pytest tests/ -k metric_readers -q                 # the whole reader suite
```

The doc-hygiene guard (`tests/test_doc_hygiene.py`) scans this package's `.py`
files for planning-doc breadcrumbs; documentation like this README is out of
scope, but keep new source comments free of spec references.

## Related Documentation

- [`gco_mcp/tools/metrics.py`](../tools/metrics.py) — the tool wrappers that call
  this package.
- [`gco_mcp/README.md`](../README.md#metrics) — the Metrics tool table and the
  [Feature Flags](../README.md#feature-flags) reference
  (`GCO_ENABLE_LOCAL_METRICS`, `GCO_METRICS_LOCAL_ROOT`).
- [`gco_mcp/mission/README.md`](../mission/README.md) — the Mission loop whose
  Observe phase merges these metrics.
- [`docs/MISSION.md`](../../docs/MISSION.md) — the user-facing Mission guide and
  `metric_threshold` / `metric_trend` criteria.
