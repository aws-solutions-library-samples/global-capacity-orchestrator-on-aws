"""Coverage-focused unit tests for the file-format metric reader.

These tests target the error and edge branches of
``gco_mcp/metric_readers/files.py`` that the round-trip property suite in
``tests/test_metric_readers_files.py`` does not exercise: the cell-coercion
helper, the value-describer, the per-format malformed / missing / non-numeric
paths, the columnar (Parquet) read, and the TensorBoard (``tfevents``) read
path reached through an injected fake parser.

They *add to* — never replace — the sibling round-trip suite, and follow the
same import convention: ``gco_mcp/`` is placed on ``sys.path`` so the pure
``metric_readers`` package imports exactly as it does in production.
"""

from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path

import pytest

# ``gco_mcp/run_mcp.py`` puts ``gco_mcp/`` on ``sys.path`` at runtime; mirror that
# here so the pure ``metric_readers`` package imports the same way it does in
# production, matching the convention used by the sibling metric-reader tests.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from metric_readers import files  # noqa: E402
from metric_readers.shape import ErrorCode, MetricReaderError  # noqa: E402

# ---------------------------------------------------------------------------
# _maybe_number — best-effort cell coercion
# ---------------------------------------------------------------------------


def test_maybe_number_passes_non_string_through_unchanged() -> None:
    """A non-string cell is returned verbatim, never coerced."""
    sentinel = [1, 2, 3]
    assert files._maybe_number(sentinel) is sentinel
    assert files._maybe_number(42) == 42
    assert files._maybe_number(None) is None


def test_maybe_number_parses_int_then_float_strings() -> None:
    """An int-looking string becomes an int; a float-looking one becomes a float."""
    assert files._maybe_number("42") == 42
    assert isinstance(files._maybe_number("42"), int)
    parsed = files._maybe_number("3.5")
    assert parsed == 3.5
    assert isinstance(parsed, float)


def test_maybe_number_returns_unparseable_string_raw() -> None:
    """A string that is neither an int nor a float is returned unchanged."""
    assert files._maybe_number("not-a-number") == "not-a-number"
    assert files._maybe_number("") == ""


# ---------------------------------------------------------------------------
# _describe_value — JSON-safe rendering of a non-numeric value
# ---------------------------------------------------------------------------


def test_describe_value_keeps_simple_scalars_verbatim() -> None:
    """A None/str/int/float/bool value is shown verbatim with its type name."""
    assert files._describe_value("loss") == {"value": "loss", "value_type": "str"}
    assert files._describe_value(None) == {"value": None, "value_type": "NoneType"}
    assert files._describe_value(True) == {"value": True, "value_type": "bool"}


def test_describe_value_reprs_non_scalar_values() -> None:
    """A container value is rendered via ``repr`` so the detail stays JSON-safe."""
    assert files._describe_value([1, 2]) == {"value": "[1, 2]", "value_type": "list"}
    described_map = files._describe_value({"a": 1})
    assert described_map["value_type"] == "dict"
    assert described_map["value"] == repr({"a": 1})


# ---------------------------------------------------------------------------
# _reduce_resolved / _handle_json — a present-but-non-numeric value
# ---------------------------------------------------------------------------


def test_reduce_resolved_rejects_non_numeric_non_list() -> None:
    """A resolved scalar that is neither a number nor a list is a non-numeric error."""
    with pytest.raises(MetricReaderError) as excinfo:
        files._reduce_resolved("text", "field", "last")
    assert excinfo.value.code == ErrorCode.NON_NUMERIC_VALUE
    assert (excinfo.value.details or {}).get("field") == "field"


def test_handle_json_string_field_raises_non_numeric_value() -> None:
    """A JSON field that resolves to a string is a non-numeric error carrying the value."""
    content = json.dumps({"metric": "high"}).encode("utf-8")
    with pytest.raises(MetricReaderError) as excinfo:
        files._handle_json(content, "metric", "last")
    assert excinfo.value.code == ErrorCode.NON_NUMERIC_VALUE
    details = excinfo.value.details or {}
    assert details.get("field") == "metric"
    assert details.get("value") == "high"


# ---------------------------------------------------------------------------
# _handle_jsonl — blank lines and invalid lines are skipped
# ---------------------------------------------------------------------------


def test_handle_jsonl_skips_blank_and_invalid_lines() -> None:
    """Blank and non-JSON lines are skipped; the valid records still reduce."""
    lines = ["", "   ", json.dumps({"metric": 1}), "not json at all", json.dumps({"metric": 3})]
    content = ("\n".join(lines) + "\n").encode("utf-8")
    assert files._handle_jsonl(content, "metric", "last") == 3
    assert files._handle_jsonl(content, "metric", "first") == 1
    assert files._handle_jsonl(content, "metric", "max") == 3


def test_handle_jsonl_field_absent_everywhere_is_no_numeric_value() -> None:
    """A field present in no record collapses to a no-numeric-value error."""
    content = (json.dumps({"other": 1}) + "\n" + json.dumps({"other": 2}) + "\n").encode("utf-8")
    with pytest.raises(MetricReaderError) as excinfo:
        files._handle_jsonl(content, "metric", "last")
    assert excinfo.value.code == ErrorCode.NO_NUMERIC_VALUE
    assert (excinfo.value.details or {}).get("field") == "metric"


# ---------------------------------------------------------------------------
# _handle_hf — Hugging Face Trainer state
# ---------------------------------------------------------------------------


def test_handle_hf_malformed_bytes_raise_malformed_file() -> None:
    """Bytes that are not valid JSON are a malformed-file error tagged hf_trainer_state."""
    with pytest.raises(MetricReaderError) as excinfo:
        files._handle_hf(b"{not json", "loss", "last")
    assert excinfo.value.code == ErrorCode.MALFORMED_FILE
    assert (excinfo.value.details or {}).get("format") == "hf_trainer_state"


def test_handle_hf_reduces_log_history_sequence() -> None:
    """A ``log_history`` list reduces the per-step field under the chosen mode."""
    content = json.dumps({"log_history": [{"loss": 2.0}, {"loss": 5.0}, {"loss": 1.0}]}).encode(
        "utf-8"
    )
    assert files._handle_hf(content, "loss", "last") == 1.0
    assert files._handle_hf(content, "loss", "min") == 1.0
    assert files._handle_hf(content, "loss", "max") == 5.0


def test_handle_hf_returns_top_level_numeric_field() -> None:
    """With no usable ``log_history``, a top-level numeric field is returned directly."""
    content = json.dumps({"eval_loss": 0.25}).encode("utf-8")
    assert files._handle_hf(content, "eval_loss", "last") == 0.25


def test_handle_hf_top_level_non_numeric_field_raises_non_numeric_value() -> None:
    """A top-level field that is present but non-numeric is a non-numeric error."""
    content = json.dumps({"eval_loss": "n/a"}).encode("utf-8")
    with pytest.raises(MetricReaderError) as excinfo:
        files._handle_hf(content, "eval_loss", "last")
    assert excinfo.value.code == ErrorCode.NON_NUMERIC_VALUE
    assert (excinfo.value.details or {}).get("value") == "n/a"


def test_handle_hf_field_in_neither_place_raises_field_not_found() -> None:
    """A field absent from both ``log_history`` and the top level is field-not-found."""
    content = json.dumps({"log_history": [{"loss": 1.0}], "step": 10}).encode("utf-8")
    with pytest.raises(MetricReaderError) as excinfo:
        files._handle_hf(content, "accuracy", "last")
    assert excinfo.value.code == ErrorCode.FIELD_NOT_FOUND


# ---------------------------------------------------------------------------
# _handle_parquet — columnar read (pandas + pyarrow)
# ---------------------------------------------------------------------------


def _build_parquet(mapping: dict) -> bytes:
    """Serialize ``mapping`` as a Parquet artifact, skipping when the extra is absent."""
    pd = pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    buffer = io.BytesIO()
    pd.DataFrame(mapping).to_parquet(buffer, index=False)
    return buffer.getvalue()


def test_handle_parquet_reduces_column_happy_path() -> None:
    """A real Parquet column reduces to the reference scalar under each selection mode."""
    content = _build_parquet({"metric": [2.0, 5.0, 1.0, 8.0]})
    assert files._handle_parquet(content, "metric", "last") == 8.0
    assert files._handle_parquet(content, "metric", "first") == 2.0
    assert files._handle_parquet(content, "metric", "min") == 1.0
    assert files._handle_parquet(content, "metric", "max") == 8.0


def test_handle_parquet_malformed_bytes_raise_malformed_file() -> None:
    """Bytes that are not a Parquet file are a malformed-file error."""
    pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    with pytest.raises(MetricReaderError) as excinfo:
        files._handle_parquet(b"definitely not parquet", "metric", "last")
    assert excinfo.value.code == ErrorCode.MALFORMED_FILE
    assert (excinfo.value.details or {}).get("format") == "parquet"


def test_handle_parquet_missing_column_raises_field_not_found() -> None:
    """A column absent from the Parquet schema is a field-not-found error."""
    content = _build_parquet({"present": [1.0, 2.0]})
    with pytest.raises(MetricReaderError) as excinfo:
        files._handle_parquet(content, "absent", "last")
    assert excinfo.value.code == ErrorCode.FIELD_NOT_FOUND
    assert (excinfo.value.details or {}).get("field") == "absent"


def test_handle_parquet_missing_dependency_raises_dependency_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing pandas wheel surfaces as a dependency-unavailable envelope, not a crash."""
    monkeypatch.setitem(sys.modules, "pandas", None)
    with pytest.raises(MetricReaderError) as excinfo:
        files._handle_parquet(b"any bytes", "metric", "last")
    assert excinfo.value.code == ErrorCode.FORMAT_DEPENDENCY_UNAVAILABLE
    assert (excinfo.value.details or {}).get("format") == "parquet"


# ---------------------------------------------------------------------------
# _handle_tfevents — dependency guard + injected-parser read path
# ---------------------------------------------------------------------------


def test_handle_tfevents_missing_dependency_raises_dependency_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``tbparse`` absent, the handler reports a dependency-unavailable envelope."""
    monkeypatch.setitem(sys.modules, "tbparse", None)
    with pytest.raises(MetricReaderError) as excinfo:
        files._handle_tfevents(b"any bytes", "loss", "last")
    assert excinfo.value.code == ErrorCode.FORMAT_DEPENDENCY_UNAVAILABLE
    assert (excinfo.value.details or {}).get("format") == "tfevents"


def _install_fake_tbparse(monkeypatch: pytest.MonkeyPatch, scalars_factory) -> None:
    """Inject a fake ``tbparse`` whose ``SummaryReader(...).scalars`` calls ``scalars_factory``.

    ``scalars_factory`` is a zero-arg callable invoked when ``.scalars`` is read,
    so a test can return a frame, return ``None``, or raise to drive each branch
    of the read path. Registered via ``monkeypatch.setitem`` so it is removed
    from ``sys.modules`` automatically at test teardown.
    """

    class _FakeSummaryReader:
        def __init__(self, path, pivot=False):
            self._path = path

        @property
        def scalars(self):
            return scalars_factory()

    fake_module = types.ModuleType("tbparse")
    fake_module.SummaryReader = _FakeSummaryReader
    monkeypatch.setitem(sys.modules, "tbparse", fake_module)


def test_handle_tfevents_reduces_matching_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    """A matching tag's scalar rows reduce to the reference scalar under the mode."""
    pd = pytest.importorskip("pandas")
    frame = pd.DataFrame({"tag": ["loss", "loss", "accuracy"], "value": [2.0, 1.0, 9.0]})
    _install_fake_tbparse(monkeypatch, lambda: frame)
    assert files._handle_tfevents(b"events", "loss", "last") == 1.0
    assert files._handle_tfevents(b"events", "loss", "max") == 2.0


def test_handle_tfevents_none_frame_raises_field_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reader that yields no scalar frame is a field-not-found error."""
    _install_fake_tbparse(monkeypatch, lambda: None)
    with pytest.raises(MetricReaderError) as excinfo:
        files._handle_tfevents(b"events", "loss", "last")
    assert excinfo.value.code == ErrorCode.FIELD_NOT_FOUND


def test_handle_tfevents_empty_frame_raises_field_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty scalar frame is a field-not-found error."""
    pd = pytest.importorskip("pandas")
    _install_fake_tbparse(monkeypatch, lambda: pd.DataFrame())
    with pytest.raises(MetricReaderError) as excinfo:
        files._handle_tfevents(b"events", "loss", "last")
    assert excinfo.value.code == ErrorCode.FIELD_NOT_FOUND


def test_handle_tfevents_frame_without_tag_column_raises_field_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-empty frame lacking a ``tag`` column is a field-not-found error."""
    pd = pytest.importorskip("pandas")
    _install_fake_tbparse(monkeypatch, lambda: pd.DataFrame({"value": [1.0, 2.0]}))
    with pytest.raises(MetricReaderError) as excinfo:
        files._handle_tfevents(b"events", "loss", "last")
    assert excinfo.value.code == ErrorCode.FIELD_NOT_FOUND


def test_handle_tfevents_non_matching_tag_raises_field_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A frame whose rows never carry the requested tag is a field-not-found error."""
    pd = pytest.importorskip("pandas")
    frame = pd.DataFrame({"tag": ["accuracy"], "value": [9.0]})
    _install_fake_tbparse(monkeypatch, lambda: frame)
    with pytest.raises(MetricReaderError) as excinfo:
        files._handle_tfevents(b"events", "loss", "last")
    assert excinfo.value.code == ErrorCode.FIELD_NOT_FOUND


def test_handle_tfevents_reader_failure_raises_malformed_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parser that raises while reading is a malformed-file error."""

    def _boom():
        raise RuntimeError("corrupt event file")

    _install_fake_tbparse(monkeypatch, _boom)
    with pytest.raises(MetricReaderError) as excinfo:
        files._handle_tfevents(b"events", "loss", "last")
    assert excinfo.value.code == ErrorCode.MALFORMED_FILE
    assert (excinfo.value.details or {}).get("format") == "tfevents"
