"""Round-trip tests for the file-format metric reader.

A job can persist its metrics in many shapes — a JSON or YAML document, a CSV
table, a Hugging Face ``Trainer`` state file, a stream of one-JSON-object-per
line records, or a columnar Parquet file. Whatever the shape, reading a named
field back out and collapsing it with an Aggregation_Mode must recover exactly
the number a straightforward reduction of the embedded sequence would produce.

The property test below pins that contract down for every parsing format. For
each format it constructs an artifact that embeds a *known* numeric sequence,
reads it back through the format's handler under each mode (``last``,
``first``, ``min``, ``max``, ``mean``), and asserts the recovered scalar equals
a reference reduction of the same sequence under the same mode. Because the
embedded sequence is numeric and each artifact round-trips its values exactly,
a divergence points at the handler, not at the artifact.

The ``parquet`` sub-case depends on pandas/pyarrow (the analytics extra) and on
the columnar handler having been registered. When either is absent the parquet
sub-case is skipped rather than failing, so the test still runs in a minimal
environment.
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ``gco_mcp/run_mcp.py`` puts ``gco_mcp/`` on ``sys.path`` at runtime; mirror that here
# so the pure ``metric_readers`` package imports the same way it does in
# production, matching the convention used by the sibling metric-reader tests.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from metric_readers import files  # noqa: E402
from metric_readers.shape import ErrorCode, MetricReaderError, is_numeric_value  # noqa: E402

# Every Aggregation_Mode the reducer accepts. The property exercises all of
# them against each generated sequence.
_MODES = ("last", "first", "min", "max", "mean")

# The parsing formats handled in the always-run property below. The columnar
# ``parquet`` format is exercised by its own dependency-guarded test.
_TEXT_FORMATS = ("json", "csv", "hf_trainer_state", "jsonl", "yaml")

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# A real, finite number that round-trips exactly through text serialization:
# a bounded int or a bounded, non-subnormal finite float. The bounds keep a
# ``mean`` sum well clear of overflow so every mode stays finite.
_numbers = st.one_of(
    st.integers(min_value=-1_000_000, max_value=1_000_000),
    st.floats(
        min_value=-1e6,
        max_value=1e6,
        allow_nan=False,
        allow_infinity=False,
        allow_subnormal=False,
    ),
)

# A non-empty sequence of numbers — the reducer rejects an empty sequence, so
# every embedded sequence carries at least one value.
_numeric_sequences = st.lists(_numbers, min_size=1, max_size=20)


# ---------------------------------------------------------------------------
# Reference reduction
# ---------------------------------------------------------------------------


def _reference_reduce(values: Sequence[object], mode: str) -> float:
    """Reduce ``values`` the obvious way, over the number-only subsequence.

    Mirrors the reducer's own definition: filter to real, finite numbers with
    the same guard, preserve their original order, then apply the mode. A
    deliberately straightforward re-derivation so a divergence points at the
    handler under test, not at clever test logic.
    """
    numbers = [v for v in values if is_numeric_value(v)]
    if mode == "last":
        return numbers[-1]
    if mode == "first":
        return numbers[0]
    if mode == "min":
        return min(numbers)
    if mode == "max":
        return max(numbers)
    # mode == "mean"
    return sum(numbers) / len(numbers)


def _recovered_ok(actual: float, expected: float, mode: str) -> bool:
    """Whether a recovered scalar matches the reference reduction.

    Selection modes (last/first/min/max) return an exact stored value and must
    match bit-for-bit. ``mean`` performs floating-point arithmetic whose last ULP
    depends on summation order (the reference left-fold vs the columnar reader
    pairwise/NumPy sum), so it is compared with a tight relative+absolute tolerance.
    """
    if mode == "mean":
        return math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9)
    return actual == expected


# ---------------------------------------------------------------------------
# Per-format artifact builders
# ---------------------------------------------------------------------------
#
# Each builder embeds ``seq`` into an artifact of its format and returns
# ``(content_bytes, field)`` — the raw bytes a handler receives and the field
# name to resolve. The field is always a single Dot_Path segment so the
# document handlers resolve it directly.


def _build_json(seq: list[object]) -> tuple[bytes, str]:
    """A plain JSON document whose ``metric`` field holds the sequence as a list."""
    return json.dumps({"metric": list(seq)}).encode("utf-8"), "metric"


def _build_yaml(seq: list[object]) -> tuple[bytes, str]:
    """A YAML document whose ``metric`` field holds the sequence as a list."""
    return yaml.safe_dump({"metric": list(seq)}).encode("utf-8"), "metric"


def _build_csv(seq: list[object]) -> tuple[bytes, str]:
    """A CSV table with a single ``metric`` column, one value per data row."""
    lines = ["metric", *[str(v) for v in seq]]
    return "\n".join(lines).encode("utf-8"), "metric"


def _build_jsonl(seq: list[object]) -> tuple[bytes, str]:
    """A JSON-lines stream of one ``{"metric": v}`` record per value."""
    lines = [json.dumps({"metric": v}) for v in seq]
    return "\n".join(lines).encode("utf-8"), "metric"


def _build_hf(seq: list[object]) -> tuple[bytes, str]:
    """A Hugging Face ``Trainer`` state file logging ``loss`` once per step."""
    doc = {"log_history": [{"loss": v} for v in seq]}
    return json.dumps(doc).encode("utf-8"), "loss"


_BUILDERS = {
    "json": _build_json,
    "csv": _build_csv,
    "hf_trainer_state": _build_hf,
    "jsonl": _build_jsonl,
    "yaml": _build_yaml,
}


# ---------------------------------------------------------------------------
# Per-format round-trip recovers the reduced scalar
# ---------------------------------------------------------------------------


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(seq=_numeric_sequences)
def test_per_format_round_trip_recovers_reduced_scalar(seq: list[object]) -> None:
    """Reading a constructed artifact back recovers the reference reduction.

    For every text parsing format (``json``, ``csv``, ``hf_trainer_state``,
    ``jsonl``, ``yaml``) and every Aggregation_Mode, the scalar the handler
    recovers from an artifact embedding ``seq`` equals the reference reduction
    of ``seq`` under that mode. The artifacts round-trip their numeric values
    exactly, so any divergence is the handler's.
    """
    for fmt in _TEXT_FORMATS:
        content, field = _BUILDERS[fmt](seq)
        handler = files._HANDLERS[fmt]
        for mode in _MODES:
            expected = _reference_reduce(seq, mode)
            actual = handler(content, field, mode)
            assert _recovered_ok(actual, expected, mode), (
                f"format={fmt!r} mode={mode!r} seq={seq!r} expected={expected!r} actual={actual!r}"
            )


# ---------------------------------------------------------------------------
# Parquet round-trip: dependency- and handler-guarded
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(seq=_numeric_sequences)
def test_parquet_round_trip_recovers_reduced_scalar(seq: list[object]) -> None:
    """The columnar reader recovers the reference reduction of a Parquet column.

    Mirrors the text-format property for ``parquet``: a single-column Parquet
    artifact embedding ``seq`` reads back, under each Aggregation_Mode, to the
    same scalar a reference reduction yields. Skipped gracefully when pandas/
    pyarrow (the analytics extra) are unavailable, or when the columnar handler
    has not yet been registered, so a minimal environment does not hard-fail.
    """
    pd = pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    if "parquet" not in files._HANDLERS:
        pytest.skip("parquet handler not registered")

    buffer = io.BytesIO()
    pd.DataFrame({"metric": list(seq)}).to_parquet(buffer, index=False)
    content = buffer.getvalue()
    handler = files._HANDLERS["parquet"]

    for mode in _MODES:
        expected = _reference_reduce(seq, mode)
        actual = handler(content, "metric", mode)
        assert _recovered_ok(actual, expected, mode), (
            f"format='parquet' mode={mode!r} seq={seq!r} expected={expected!r} actual={actual!r}"
        )


# ===========================================================================
# File-reader error-class unit tests
# ===========================================================================
#
# Plain (non-property) unit tests pinning down which stable ErrorCode each
# failure class surfaces, one assertion per documented code.
#
# Layering note. The per-format *handlers* in ``files.py`` raise the codes that
# describe the *content* of an artifact:
#
#   * ``field_not_found``                — a named field/column is absent
#   * ``malformed_file``                 — bytes do not parse under the format
#   * ``no_numeric_value``               — a JSONL stream yields no usable number
#   * ``format_dependency_unavailable``  — a parquet/tfevents lazy import fails
#
# The remaining two codes live at the *tool* boundary in ``gco_mcp/tools/metrics.py``,
# not in any handler:
#
#   * ``file_too_large``    — the ``_read_shared_storage`` size cap, checked
#                             before the artifact's content is read.
#   * ``unsupported_format`` — the ``format not in files._HANDLERS`` guard,
#                             reached when the caller names a format with no handler.
#
# These two are tested against the pure mechanisms in the tool module: the
# size-cap helper is exercised with a stubbed read (no AWS/network), and the
# format guard is exercised by invoking the tool with a bogus format (the guard
# runs before any storage read, so it stays offline).
#
# On ``tfevents`` and ``unsupported_format``. ``tfevents`` is implemented
# rather than deferred: it is a registered ``_HANDLERS`` entry whose lazy
# ``tbparse`` import, when the dependency is absent, yields
# ``format_dependency_unavailable`` — the path exercised below. Were the format
# ever deferred entirely, a ``tfevents`` request would instead surface the same
# ``unsupported_format`` code the tool-boundary guard raises for any format with
# no handler; that code is covered by the unknown-format test below.


# ---------------------------------------------------------------------------
# field_not_found — a named field / column is absent
# ---------------------------------------------------------------------------


def test_json_missing_field_raises_field_not_found() -> None:
    """A JSON dot-path that resolves nowhere is a field-not-found error."""
    content = json.dumps({"present": 1.0}).encode("utf-8")
    with pytest.raises(MetricReaderError) as excinfo:
        files._handle_json(content, "absent", "last")
    assert excinfo.value.code == ErrorCode.FIELD_NOT_FOUND
    assert excinfo.value.details == {"field": "absent"}


def test_csv_missing_column_raises_field_not_found() -> None:
    """A column name absent from the CSV header is a field-not-found error."""
    content = b"colA,colB\n1,2\n3,4\n"
    with pytest.raises(MetricReaderError) as excinfo:
        files._handle_csv(content, "colC", "last")
    assert excinfo.value.code == ErrorCode.FIELD_NOT_FOUND
    assert excinfo.value.details == {"field": "colC"}


def test_hf_missing_field_raises_field_not_found() -> None:
    """A field in neither ``log_history`` nor a top-level key is missing."""
    content = json.dumps({"log_history": [{"loss": 1.0}]}).encode("utf-8")
    with pytest.raises(MetricReaderError) as excinfo:
        files._handle_hf(content, "accuracy", "last")
    assert excinfo.value.code == ErrorCode.FIELD_NOT_FOUND
    assert excinfo.value.details == {"field": "accuracy"}


# ---------------------------------------------------------------------------
# malformed_file — bytes do not parse under the requested format
# ---------------------------------------------------------------------------


def test_json_unparseable_bytes_raise_malformed_file() -> None:
    """Bytes that are not valid JSON are a malformed-file error tagged ``json``."""
    with pytest.raises(MetricReaderError) as excinfo:
        files._handle_json(b"{not valid json", "field", "last")
    assert excinfo.value.code == ErrorCode.MALFORMED_FILE
    assert (excinfo.value.details or {}).get("format") == "json"


def test_yaml_unparseable_bytes_raise_malformed_file() -> None:
    """A document the safe loader rejects is a malformed-file error."""
    with pytest.raises(MetricReaderError) as excinfo:
        files._handle_yaml(b":\n  - [unbalanced", "field", "last")
    assert excinfo.value.code == ErrorCode.MALFORMED_FILE
    assert (excinfo.value.details or {}).get("format") == "yaml"


def test_csv_invalid_utf8_raises_malformed_file() -> None:
    """Bytes that are not valid UTF-8 surface as a malformed-file decode error."""
    with pytest.raises(MetricReaderError) as excinfo:
        files._handle_csv(b"\xff\xfe\x00not utf-8", "field", "last")
    assert excinfo.value.code == ErrorCode.MALFORMED_FILE
    assert (excinfo.value.details or {}).get("format") == "csv"


# ---------------------------------------------------------------------------
# no_numeric_value — a JSONL stream yields no usable number
# ---------------------------------------------------------------------------


def test_jsonl_all_non_numeric_raises_no_numeric_value() -> None:
    """A JSONL stream whose field is never numeric is a no-numeric-value error."""
    content = b'{"metric": "high"}\n{"metric": "low"}\n{"metric": null}\n'
    with pytest.raises(MetricReaderError) as excinfo:
        files._handle_jsonl(content, "metric", "last")
    assert excinfo.value.code == ErrorCode.NO_NUMERIC_VALUE


def test_jsonl_field_never_present_raises_no_numeric_value() -> None:
    """A stream that never carries the field collapses to the same code.

    Lines that are valid JSON objects but lack the requested key contribute
    nothing, so the gathered sequence is empty — the reader maps that to the
    same no-numeric-value outcome as an all-non-numeric stream.
    """
    content = b'{"other": 1}\n{"other": 2}\n'
    with pytest.raises(MetricReaderError) as excinfo:
        files._handle_jsonl(content, "metric", "last")
    assert excinfo.value.code == ErrorCode.NO_NUMERIC_VALUE
    assert (excinfo.value.details or {}).get("field") == "metric"


# ---------------------------------------------------------------------------
# format_dependency_unavailable — a lazy optional import fails
# ---------------------------------------------------------------------------


def test_parquet_missing_dependency_raises_format_dependency_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing pandas/pyarrow wheel is a dependency-unavailable envelope.

    Setting ``sys.modules["pandas"]`` to ``None`` makes ``import pandas`` inside
    the handler raise ``ImportError`` deterministically, regardless of whether
    the analytics extra happens to be installed — so the guard is exercised in
    every environment rather than only minimal ones.
    """
    monkeypatch.setitem(sys.modules, "pandas", None)
    with pytest.raises(MetricReaderError) as excinfo:
        files._handle_parquet(b"any bytes", "column", "last")
    assert excinfo.value.code == ErrorCode.FORMAT_DEPENDENCY_UNAVAILABLE
    assert (excinfo.value.details or {}).get("format") == "parquet"


def test_tfevents_missing_dependency_raises_format_dependency_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing tbparse/tensorboard parser is a dependency-unavailable envelope.

    ``tfevents`` is implemented (a registered handler), not deferred, so an
    absent parser surfaces as ``format_dependency_unavailable`` and never as an
    import crash. Forcing ``import tbparse`` to fail via ``sys.modules``
    exercises that lazy-import guard.
    """
    monkeypatch.setitem(sys.modules, "tbparse", None)
    with pytest.raises(MetricReaderError) as excinfo:
        files._handle_tfevents(b"any bytes", "tag", "last")
    assert excinfo.value.code == ErrorCode.FORMAT_DEPENDENCY_UNAVAILABLE
    assert (excinfo.value.details or {}).get("format") == "tfevents"


# ---------------------------------------------------------------------------
# Tool-boundary codes: file_too_large and unsupported_format
# ---------------------------------------------------------------------------


def _import_metrics_tool_module():
    """Import ``tools.metrics`` or skip when its FastMCP/server deps are absent.

    The pure ``metric_readers`` package needs nothing beyond the standard
    library; the tool wrappers pull in ``server`` (FastMCP) and ``audit``. Skip
    rather than fail when that surface cannot be imported, so this file's
    handler-level assertions still run in a minimal environment.
    """
    try:
        import tools.metrics as metrics_module
    except Exception as exc:  # noqa: BLE001 - any import-surface failure -> skip
        pytest.skip(f"tools.metrics not importable in this environment: {exc}")
    return metrics_module


def test_oversize_artifact_raises_file_too_large(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An artifact larger than ``max_bytes`` is a file-too-large error.

    Exercises the tool-boundary size cap in ``_read_shared_storage`` without any
    AWS/network: the ``gco files download`` call is stubbed to drop an oversized
    file at the destination, and the helper's stat-before-read check must reject
    it with ``file_too_large`` and the size/limit provenance, returning no
    content.
    """
    metrics_module = _import_metrics_tool_module()

    def _fake_run_cli(*args: object, **_kwargs: object) -> str:
        # ``files download <path> <local_path> -r <region>`` — write an
        # oversized payload to the requested local destination, return no error.
        local_path = args[3]
        with open(str(local_path), "wb") as handle:
            handle.write(b"x" * 4096)
        return "{}"

    monkeypatch.setattr(metrics_module.cli_runner, "_run_cli", _fake_run_cli)

    with pytest.raises(MetricReaderError) as excinfo:
        metrics_module._read_shared_storage("some/remote/path", "us-east-1", max_bytes=1024)
    assert excinfo.value.code == ErrorCode.FILE_TOO_LARGE
    details = excinfo.value.details or {}
    assert details.get("path") == "some/remote/path"
    assert details.get("size") == 4096
    assert details.get("max_bytes") == 1024


def test_unknown_format_returns_unsupported_format() -> None:
    """A ``format`` with no handler is an unsupported-format envelope.

    The tool's format guard runs before any storage read, so invoking the
    shared-storage reader with a bogus format returns the ``unsupported_format``
    envelope offline. This is also the code reserved for a ``tfevents``
    request were the format ever deferred entirely.
    """
    metrics_module = _import_metrics_tool_module()
    result = asyncio.run(
        metrics_module.metrics_from_shared_storage_file(
            path="some/remote/path",
            region="us-east-1",
            field="loss",
            format="bogus_format",
        )
    )
    assert result["code"] == ErrorCode.UNSUPPORTED_FORMAT
    assert result["details"]["format"] == "bogus_format"
    assert "metrics" not in result
