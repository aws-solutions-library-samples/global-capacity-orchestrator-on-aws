"""Building blocks shared by every metric-reader tool.

A metric reader either produces one numeric scalar wrapped in a canonical
result shape, or it fails and produces a structured error envelope. This
module owns the small, pure pieces both outcomes are built from:

* :class:`ErrorCode` — the frozen set of stable, machine-readable failure
  codes a reader can surface.
* :class:`MetricReaderError` — the exception readers raise internally;
  tool wrappers translate it into an error envelope.
* :func:`validate_metric_name` — guards the caller-supplied metric name so
  the resulting key is a single, well-formed path segment.
* :func:`default_metric_key` — derives a deterministic, well-formed key from
  a source identifier when the caller supplies no explicit name.
* :func:`is_numeric_value` — the single source of truth for "is this a real,
  finite number?".
* :func:`metrics_result` — assembles the canonical
  ``{"metrics": {key: value}, ...}`` success shape.
* :func:`error_envelope` — assembles the ``{"code", "details"}`` failure
  shape.

Everything here is pure: no I/O, no clocks, no environment lookups. That
keeps the pieces trivial to test in isolation and safe to call from both
async tool handlers and synchronous code.
"""

from __future__ import annotations

import math
from typing import Any

# The longest a metric name may be, in characters.
_MAX_METRIC_NAME_LEN = 128

# The fallback key used when a source identifier sanitizes down to nothing.
_DEFAULT_KEY_FALLBACK = "metric"


class ErrorCode:
    """Stable, machine-readable failure codes a metric reader can surface.

    Each value is a short string an operator (or an automated caller) can
    branch on without parsing a human message. The values are deliberately
    frozen: callers and tests may depend on the exact strings.
    """

    METRIC_NAME_INVALID = "metric_name_invalid"
    INVALID_AGGREGATION_MODE = "invalid_aggregation_mode"
    EMPTY_SEQUENCE = "empty_sequence"
    # A sequence had entries, but none survived the numeric filter.
    NO_NUMERIC_VALUE = "no_numeric_value"
    # A single resolved value was not a real, finite number.
    NON_NUMERIC_VALUE = "non_numeric_value"
    NO_DATAPOINTS = "no_datapoints"
    # details.kind discriminates unreachable / unauthorized / client_error.
    AWS_UNREACHABLE = "aws_unreachable"
    INVALID_EXTRACTION_MODE = "invalid_extraction_mode"
    INVALID_REGEX = "invalid_regex"
    NO_MATCH = "no_match"
    # details.kind discriminates unknown_job / unreachable.
    LOG_RETRIEVAL_FAILED = "log_retrieval_failed"
    FILE_NOT_FOUND = "file_not_found"
    MALFORMED_FILE = "malformed_file"
    # Covers both a missing object field and a missing table column.
    FIELD_NOT_FOUND = "field_not_found"
    # The same failure class as NON_NUMERIC_VALUE, named for file readers.
    NON_NUMERIC_VALUE_FILE = NON_NUMERIC_VALUE
    FILE_TOO_LARGE = "file_too_large"
    UNSUPPORTED_FORMAT = "unsupported_format"
    FORMAT_DEPENDENCY_UNAVAILABLE = "format_dependency_unavailable"
    # Local-filesystem reader confinement codes. Kept distinct so an
    # operator can tell a traversal attempt, a symlink escape, and an
    # unconfigured root apart without inspecting the details payload.
    #
    # A supplied path's ".." segments escaped the allowlisted root.
    PATH_TRAVERSAL_ESCAPE = "path_traversal_escape"
    # A symlink within the path resolved to a target outside the root.
    SYMLINK_ESCAPE = "symlink_escape"
    # The reader is enabled but no allowlisted root is configured.
    LOCAL_ROOT_NOT_CONFIGURED = "local_root_not_configured"


class MetricReaderError(Exception):
    """Raised internally when a reader cannot produce a metric.

    Carries a stable short ``code`` (one of :class:`ErrorCode`) and an
    optional structured ``details`` dict the tool wrapper renders into an
    error envelope. The constructor accepts ``(code, details=None, *,
    message=None)``; when ``message`` is omitted the exception's string
    form falls back to ``code`` so logs always show something meaningful.
    """

    def __init__(
        self,
        code: str,
        details: dict[str, Any] | None = None,
        *,
        message: str | None = None,
    ) -> None:
        self.code: str = code
        self.details: dict[str, Any] | None = details
        rendered = message if message is not None else code
        super().__init__(rendered)


def validate_metric_name(name: str) -> str:
    """Return ``name`` unchanged when it is a single well-formed path segment.

    A valid name is a non-empty string of at most 128 characters that
    contains neither a ``.`` separator nor any whitespace character, so the
    resulting metric path is exactly ``metrics.<name>``. Any other input —
    empty, too long, containing a ``.``, or containing whitespace — raises
    :class:`MetricReaderError` with code
    :attr:`ErrorCode.METRIC_NAME_INVALID` and a ``details`` payload that
    names the specific reason.
    """
    if not isinstance(name, str):
        raise MetricReaderError(
            ErrorCode.METRIC_NAME_INVALID,
            {"reason": "not_a_string"},
        )
    if not name:
        raise MetricReaderError(
            ErrorCode.METRIC_NAME_INVALID,
            {"reason": "empty", "name": name},
        )
    if len(name) > _MAX_METRIC_NAME_LEN:
        raise MetricReaderError(
            ErrorCode.METRIC_NAME_INVALID,
            {
                "reason": "too_long",
                "name": name,
                "max_length": _MAX_METRIC_NAME_LEN,
                "actual_length": len(name),
            },
        )
    if "." in name:
        raise MetricReaderError(
            ErrorCode.METRIC_NAME_INVALID,
            {"reason": "contains_separator", "name": name},
        )
    if any(ch.isspace() for ch in name):
        raise MetricReaderError(
            ErrorCode.METRIC_NAME_INVALID,
            {"reason": "contains_whitespace", "name": name},
        )
    return name


def default_metric_key(source_hint: str) -> str:
    """Derive a deterministic, well-formed key from a source identifier.

    Used when the caller supplies no explicit metric name. Every character
    that a metric name may not contain — a ``.`` separator or any whitespace
    character — is replaced with an underscore, the result is capped at 128
    characters, and an identifier that sanitizes down to nothing falls back
    to a fixed placeholder. The same input always yields the same key, and
    the key always satisfies :func:`validate_metric_name`.
    """
    text = str(source_hint)
    sanitized = "".join("_" if (ch == "." or ch.isspace()) else ch for ch in text)
    sanitized = sanitized[:_MAX_METRIC_NAME_LEN]
    if not sanitized:
        return _DEFAULT_KEY_FALLBACK
    return sanitized


def is_numeric_value(x: object) -> bool:
    """Return True only for a real, finite number.

    An integer qualifies; a float qualifies when it is finite. A boolean is
    rejected even though ``bool`` is a subclass of ``int``, and NaN and the
    infinities are rejected. Everything else — strings, ``None``, containers
    — is rejected too. This is the single gate a value must pass before it
    can stand in for a metric a threshold comparison will read.
    """
    if isinstance(x, bool):
        return False
    if isinstance(x, int):
        return True
    if isinstance(x, float):
        # math.isfinite is False for NaN and +/-inf.
        return math.isfinite(x)
    return False


def metrics_result(key: str, value: float, **provenance: object) -> dict[str, Any]:
    """Assemble the canonical success shape: ``{"metrics": {key: value}, ...}``.

    The single metric lives under the top-level ``metrics`` object; every
    provenance field (source identifier, region, timestamp, aggregation mode
    applied, and so on) is placed beside ``metrics`` at the top level, never
    inside it, so the merged view contains only the numeric value. ``value``
    must already be a real, finite number — callers coerce and check before
    reaching this builder.
    """
    assert is_numeric_value(value), "metrics_result requires a finite numeric value"
    # Lay down provenance first, then the metrics object, so the top-level
    # ``metrics`` key is always the canonical numeric map even if a
    # provenance field happens to share that name.
    result: dict[str, Any] = dict(provenance)
    result["metrics"] = {key: value}
    return result


def error_envelope(code: str, **details: object) -> dict[str, Any]:
    """Assemble the structured failure shape: ``{"code": code, "details": {...}}``.

    The returned object never carries a top-level ``metrics`` key, so a
    consumer that merges only ``metrics``-shaped results skips it and leaves
    the corresponding check undecided rather than acting on bad data. Any
    diagnostic context is nested under ``details``.
    """
    return {"code": code, "details": dict(details)}
