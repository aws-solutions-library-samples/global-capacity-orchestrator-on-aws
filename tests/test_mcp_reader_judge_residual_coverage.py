"""Pure residual coverage for metric readers and the semantic-progress judge."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gco_mcp"))

from metric_readers.logs import coerce_scalar  # noqa: E402
from metric_readers.shape import (  # noqa: E402
    ErrorCode as ReaderErrorCode,
)
from metric_readers.shape import (  # noqa: E402
    MetricReaderError,
    default_metric_key,
    validate_metric_name,
)
from mission_judge.prompt import TRUNCATION_MARKER, truncate_context  # noqa: E402
from mission_judge.shape import (  # noqa: E402
    ErrorCode as JudgeErrorCode,
)
from mission_judge.shape import (  # noqa: E402
    JudgeError,
    validate_output_name,
)


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf"])
def test_log_scalar_rejects_parseable_non_finite_strings(raw: str) -> None:
    """Strings accepted by float() still must represent finite metrics."""
    with pytest.raises(MetricReaderError) as exc_info:
        coerce_scalar(raw)

    assert exc_info.value.code == ReaderErrorCode.NON_NUMERIC_VALUE
    assert exc_info.value.details == {"raw": raw, "type": "str"}


def test_metric_name_runtime_guard_rejects_non_string() -> None:
    """The explicit runtime guard rejects values before string operations."""
    supplied: Any = None
    with pytest.raises(MetricReaderError) as exc_info:
        validate_metric_name(supplied)

    assert exc_info.value.code == ReaderErrorCode.METRIC_NAME_INVALID
    assert exc_info.value.details == {"reason": "not_a_string"}


def test_empty_source_uses_stable_valid_default_metric_key() -> None:
    key = default_metric_key("")
    assert key == "metric"
    assert validate_metric_name(key) == key


@pytest.mark.parametrize("limit", [0, 1, len(TRUNCATION_MARKER)])
def test_tiny_context_budget_keeps_unmarked_newest_tail(limit: int) -> None:
    """Budgets too small for the marker retain exactly the newest characters."""
    context = "oldest:" + ("x" * (len(TRUNCATION_MARKER) + 5)) + ":newest"
    expected = "" if limit == 0 else context[-limit:]

    result = truncate_context(context, limit=limit)

    assert result == expected
    assert len(result) == limit
    assert not result.startswith(TRUNCATION_MARKER)


def test_output_name_runtime_guard_rejects_non_string() -> None:
    supplied: Any = None
    with pytest.raises(JudgeError) as exc_info:
        validate_output_name(supplied)

    assert exc_info.value.code == JudgeErrorCode.INVALID_OUTPUT_NAME
    assert exc_info.value.details == {"reason": "not_a_string", "supplied": None}
