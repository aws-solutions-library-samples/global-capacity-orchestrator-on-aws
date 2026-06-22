"""Tests for pulling a scalar out of a job's log lines.

A job prints its progress to stdout in one of two shapes: a structured JSON
object per line, or free text a regular expression can pick apart. The log
helpers turn either shape into an ordered list of candidate values, and a
final coercion step turns one of those candidates into a real number.

These tests pin down three behaviors that matter most:

* JSON-key extraction tolerates noise — a line that is not a JSON object
  contributes nothing and never raises, while well-formed object lines still
  yield their value.
* Regex extraction collects the first capture group from each matching line
  and ignores lines that do not match.
* Coercion of a value that is not a real number fails with a structured error
  that records the offending raw value.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# ``gco_mcp/run_mcp.py`` puts ``gco_mcp/`` on ``sys.path`` at runtime; mirror that
# here so the pure ``metric_readers`` package imports the same way it does
# in production, matching the convention used by the sibling tests.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from metric_readers.logs import (  # noqa: E402
    coerce_scalar,
    extract_by_json_key,
    extract_by_regex,
)
from metric_readers.shape import ErrorCode, MetricReaderError  # noqa: E402

# ---------------------------------------------------------------------------
# JSON-key extraction: non-object lines are skipped, object lines still extract
# ---------------------------------------------------------------------------


def test_non_json_lines_are_skipped_during_key_extraction() -> None:
    """Lines that are not JSON objects contribute nothing; object lines still extract.

    The input mixes well-formed object lines carrying the key with every kind
    of line the parser must tolerate: truncated/garbage JSON, a bare number, a
    bare string, a JSON array, a JSON ``null``, and an empty line. Only the
    values from the two valid object lines come back, in order.
    """
    lines = [
        '{"loss": 0.5}',  # valid object, has the key
        "not json at all",  # not parseable as JSON
        "{bad json",  # truncated JSON
        "42",  # bare number, not an object
        '"just a string"',  # bare JSON string, not an object
        "[1, 2, 3]",  # JSON array, not an object
        "null",  # JSON null, not an object
        "",  # empty line
        '{"loss": 0.25}',  # valid object, has the key
    ]

    assert extract_by_json_key(lines, "loss") == [0.5, 0.25]


def test_dotted_key_resolves_nested_json_objects() -> None:
    """A dotted key walks nested objects segment by segment."""
    lines = [
        '{"metrics": {"loss": 1.0}}',
        '{"metrics": {"loss": 0.75}}',
    ]

    assert extract_by_json_key(lines, "metrics.loss") == [1.0, 0.75]


def test_lines_missing_the_key_path_contribute_nothing() -> None:
    """Object lines that do not resolve the full key path are skipped.

    Lines whose object lacks the key, or whose intermediate segment is not an
    object, drop out — only the line that resolves the whole path remains.
    """
    lines = [
        '{"accuracy": 0.9}',  # object, but missing the key entirely
        '{"metrics": 5}',  # intermediate segment is not an object
        '{"metrics": {"loss": 0.1}}',  # resolves the full path
    ]

    assert extract_by_json_key(lines, "metrics.loss") == [0.1]


def test_key_extraction_returns_empty_list_for_no_lines() -> None:
    """No input lines yields no candidate values."""
    assert extract_by_json_key([], "loss") == []


# ---------------------------------------------------------------------------
# Regex extraction: first capture group from matching lines, others ignored
# ---------------------------------------------------------------------------


def test_regex_extraction_collects_first_capture_group() -> None:
    """Matching lines yield their first capture group; non-matching lines are ignored.

    The pattern captures the numeric text after ``loss=``. The two lines that
    contain the token contribute their captured substrings in order; the line
    without it contributes nothing.
    """
    pattern = re.compile(r"loss=([0-9.]+)")
    lines = [
        "step 1 loss=0.50 acc=0.9",
        "no metric on this line",
        "step 2 loss=0.25 acc=0.95",
    ]

    assert extract_by_regex(lines, pattern) == ["0.50", "0.25"]


def test_regex_extraction_captures_only_the_first_group() -> None:
    """When a pattern defines several groups, only the first capture is collected."""
    pattern = re.compile(r"loss=([0-9.]+) acc=([0-9.]+)")
    lines = ["loss=0.3 acc=0.8"]

    assert extract_by_regex(lines, pattern) == ["0.3"]


def test_regex_extraction_returns_empty_list_when_nothing_matches() -> None:
    """A pattern that matches no line yields no candidate values."""
    pattern = re.compile(r"loss=([0-9.]+)")
    lines = ["nothing here", "still nothing"]

    assert extract_by_regex(lines, pattern) == []


# ---------------------------------------------------------------------------
# coerce_scalar: success cases and the non-numeric error with the raw value
# ---------------------------------------------------------------------------


def test_coerce_scalar_accepts_numbers_and_numeric_strings() -> None:
    """Real numbers pass through and numeric strings parse to numbers."""
    assert coerce_scalar(42) == 42
    assert coerce_scalar(0.5) == 0.5
    assert coerce_scalar("7") == 7
    assert coerce_scalar("3.14") == 3.14


def test_coerce_scalar_raises_non_numeric_for_unparseable_string() -> None:
    """A string that is not a number raises the non-numeric error with the raw value."""
    with pytest.raises(MetricReaderError) as exc_info:
        coerce_scalar("not-a-number")

    error = exc_info.value
    assert error.code == ErrorCode.NON_NUMERIC_VALUE
    assert error.details is not None
    assert error.details["raw"] == "not-a-number"


def test_coerce_scalar_error_records_offending_non_string_value() -> None:
    """A non-string, non-numeric value is reported with the offending raw value.

    ``None`` is neither a number nor a parseable string, so coercion fails and
    the structured error carries the original value plus its type name.
    """
    with pytest.raises(MetricReaderError) as exc_info:
        coerce_scalar(None)

    error = exc_info.value
    assert error.code == ErrorCode.NON_NUMERIC_VALUE
    assert error.details is not None
    assert error.details["raw"] is None
    assert error.details["type"] == "NoneType"


def test_coerce_scalar_rejects_booleans_with_raw_value() -> None:
    """A boolean is not a metric value and is reported as non-numeric."""
    with pytest.raises(MetricReaderError) as exc_info:
        coerce_scalar(True)

    error = exc_info.value
    assert error.code == ErrorCode.NON_NUMERIC_VALUE
    assert error.details is not None
    assert error.details["raw"] is True
