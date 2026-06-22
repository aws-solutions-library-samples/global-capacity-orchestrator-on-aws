"""Building blocks shared by the semantic-progress judge tool.

The judge either produces one finite-float progress score wrapped in a
canonical result shape, or it fails and produces a structured error
envelope. This module owns the small, pure pieces both outcomes are built
from:

* :class:`ErrorCode` — the frozen set of stable, machine-readable failure
  codes the judge can surface.
* :class:`JudgeError` — the exception the judge raises internally; the tool
  wrapper translates it into an error envelope.
* :func:`validate_output_name` — guards the caller-supplied metric name so
  the resulting key is a single, well-formed path segment.
* :func:`is_finite_float` — the single source of truth for "is this a real,
  finite number that a threshold comparison can read?".
* :func:`metrics_result` — assembles the canonical
  ``{"metrics": {key: value}, ...}`` success shape with provenance placed
  beside ``metrics`` rather than inside it.
* :func:`error_envelope` — assembles the ``{"code", "details"}`` failure
  shape that never carries a top-level ``metrics`` key.

Everything here is pure: no I/O, no clocks, no environment lookups. That
keeps the pieces trivial to test in isolation and safe to call from both
async tool handlers and synchronous code.
"""

from __future__ import annotations

import math
from typing import Any

# The longest an output name may be, in characters.
_MAX_OUTPUT_NAME_LEN = 128


class ErrorCode:
    """Stable, machine-readable failure codes the judge can surface.

    Each value is a short string an operator (or an automated caller) can
    branch on without parsing a human message. The values are deliberately
    frozen: callers and tests may depend on the exact strings.
    """

    # A caller-supplied output name was empty, too long, or carried a
    # separator or whitespace character.
    INVALID_OUTPUT_NAME = "invalid_output_name"
    # The directive input was absent, empty, or whitespace-only.
    MISSING_DIRECTIVE = "missing_directive"
    # No sampling backend was available to produce a score.
    NO_SAMPLING_BACKEND = "no_sampling_backend"
    # The backend's sample call raised a transport, throttling, credentials,
    # or timeout error.
    SAMPLING_TRANSPORT_ERROR = "sampling_transport_error"
    # The model output could not be parsed into a finite real numeric score.
    INVALID_MODEL_SCORE = "invalid_model_score"


class JudgeError(Exception):
    """Raised internally when the judge cannot produce a score.

    Carries a stable short ``code`` (one of :class:`ErrorCode`) and an
    optional structured ``details`` dict the tool wrapper renders into an
    error envelope. When ``details`` is omitted it defaults to an empty
    dict, and the exception's string form falls back to ``code`` so logs
    always show something meaningful.
    """

    def __init__(self, code: str, details: dict[str, Any] | None = None) -> None:
        self.code: str = code
        self.details: dict[str, Any] = details or {}
        super().__init__(code)


def validate_output_name(name: str) -> str:
    """Return ``name`` unchanged when it is a single well-formed path segment.

    A valid name is a non-empty string of at most 128 characters that
    contains neither a ``.`` separator nor any whitespace character, so the
    resulting metric path is exactly ``metrics.<name>``. Any other input —
    empty, too long, containing a ``.``, or containing whitespace — raises
    :class:`JudgeError` with code :attr:`ErrorCode.INVALID_OUTPUT_NAME` and a
    ``details`` payload that names the specific reason and echoes the
    supplied value.
    """
    if not isinstance(name, str):
        raise JudgeError(
            ErrorCode.INVALID_OUTPUT_NAME,
            {"reason": "not_a_string", "supplied": name},
        )
    if not name:
        raise JudgeError(
            ErrorCode.INVALID_OUTPUT_NAME,
            {"reason": "empty", "supplied": name},
        )
    if len(name) > _MAX_OUTPUT_NAME_LEN:
        raise JudgeError(
            ErrorCode.INVALID_OUTPUT_NAME,
            {
                "reason": "too_long",
                "supplied": name,
                "max_length": _MAX_OUTPUT_NAME_LEN,
                "actual_length": len(name),
            },
        )
    if "." in name:
        raise JudgeError(
            ErrorCode.INVALID_OUTPUT_NAME,
            {"reason": "contains_separator", "supplied": name},
        )
    if any(ch.isspace() for ch in name):
        raise JudgeError(
            ErrorCode.INVALID_OUTPUT_NAME,
            {"reason": "contains_whitespace", "supplied": name},
        )
    return name


def is_finite_float(x: object) -> bool:
    """Return True only for a real, finite number.

    An integer qualifies; a float qualifies when it is finite. A boolean is
    rejected even though ``bool`` is a subclass of ``int``, and NaN and the
    infinities are rejected. Everything else — strings, ``None``, containers
    — is rejected too. This is the single gate the emitted progress score
    must pass before it can stand in for a metric a threshold comparison
    will read.
    """
    if isinstance(x, bool):
        return False
    if isinstance(x, int):
        return True
    if isinstance(x, float):
        # math.isfinite is False for NaN and +/-inf.
        return math.isfinite(x)
    return False


def metrics_result(
    output_name: str,
    score: float,
    *,
    rationale: str,
    source: str,
    backend_name: str,
    model_id: str,
    rubric_version: str,
    raw_score: float,
) -> dict[str, Any]:
    """Assemble the canonical success shape: ``{"metrics": {output_name: score}, ...}``.

    The single progress score lives under the top-level ``metrics`` object;
    every provenance field (rationale, source identifier, resolved backend
    name and model id, rubric version, and the pre-clamp raw score) is placed
    beside ``metrics`` at the top level, never inside it, so the merged view
    contains only the numeric value. ``score`` must already be a real, finite
    number — callers parse and clamp before reaching this builder.
    """
    assert is_finite_float(score), "metrics_result requires a finite numeric score"
    # Lay down provenance first, then the metrics object, so the top-level
    # ``metrics`` key is always the canonical numeric map.
    result: dict[str, Any] = {
        "rationale": rationale,
        "source": source,
        "backend_name": backend_name,
        "model_id": model_id,
        "rubric_version": rubric_version,
        "raw_score": raw_score,
    }
    result["metrics"] = {output_name: score}
    return result


def error_envelope(code: str, **details: object) -> dict[str, Any]:
    """Assemble the structured failure shape: ``{"code": code, "details": {...}}``.

    The returned object never carries a top-level ``metrics`` key, so a
    consumer that merges only ``metrics``-shaped results skips it and leaves
    the corresponding check undecided rather than acting on bad data. Any
    diagnostic context is nested under ``details``.
    """
    return {"code": code, "details": dict(details)}
