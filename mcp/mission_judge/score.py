"""Parse and bound the model's raw response into a usable progress score.

The judge asks a model for a single progress score and gets back a string.
Turning that string into a number a threshold comparison can read is split
into two deliberately separate steps:

* :func:`parse_score` owns the only failure path. It decodes the raw text,
  validates that it carries a real, finite numeric ``score`` field, and
  returns that score together with the model's rationale. Anything it cannot
  trust — non-JSON text, a missing or non-numeric field, a boolean, or a
  NaN/infinite value — becomes a :class:`JudgeError`.
* :func:`clamp_score` owns no failure path. It is a total function on finite
  floats that folds an out-of-range value onto the nearest bound of the
  closed interval ``[0.0, 1.0]`` and returns an in-range value untouched.

Keeping the two apart means the caller can record the unmodified parsed value
as provenance and emit the clamped value as the metric, without either step
second-guessing the other.
"""

from __future__ import annotations

import json
import math
from typing import Any

from .shape import ErrorCode, JudgeError

# The field the model is instructed to populate with its numeric score.
SCORE_FIELD = "score"
# The field the model is instructed to populate with its free-text rationale.
RATIONALE_FIELD = "rationale"

# The inclusive bounds of the progress-score interval.
_LOWER_BOUND = 0.0
_UPPER_BOUND = 1.0


def parse_score(raw_text: str) -> tuple[float, str]:
    """Decode the model's raw response into ``(raw_score, rationale)``.

    The raw text must be a JSON object carrying a real, finite numeric
    ``score`` field. The returned ``raw_score`` is that value coerced to a
    float and is **not** yet clamped, so the caller can record it verbatim as
    provenance. The returned ``rationale`` is the model's ``rationale`` field
    coerced to a string, defaulting to an empty string when absent.

    Every way the response can fail to yield a trustworthy number raises
    :class:`JudgeError` with code :attr:`ErrorCode.INVALID_MODEL_SCORE` and a
    ``reason`` in ``details`` identifying the specific failure:

    * ``non_json`` — the text is not valid JSON.
    * ``missing_score_field`` — the JSON is not an object, or has no
      ``score`` field.
    * ``non_numeric`` — the ``score`` is a boolean, string, null, or other
      non-numeric value.
    * ``non_finite`` — the ``score`` is NaN or positive/negative infinity.
    """
    try:
        parsed = json.loads(raw_text)
    except (ValueError, TypeError) as err:
        raise JudgeError(
            ErrorCode.INVALID_MODEL_SCORE,
            {"reason": "non_json"},
        ) from err

    if not isinstance(parsed, dict) or SCORE_FIELD not in parsed:
        raise JudgeError(
            ErrorCode.INVALID_MODEL_SCORE,
            {"reason": "missing_score_field"},
        )

    raw_value: Any = parsed[SCORE_FIELD]
    # bool is a subclass of int, so reject it explicitly before the int/float
    # check below would otherwise accept True/False as 1/0.
    if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
        raise JudgeError(
            ErrorCode.INVALID_MODEL_SCORE,
            {"reason": "non_numeric"},
        )

    raw_score = float(raw_value)
    if math.isnan(raw_score) or math.isinf(raw_score):
        raise JudgeError(
            ErrorCode.INVALID_MODEL_SCORE,
            {"reason": "non_finite"},
        )

    rationale = str(parsed.get(RATIONALE_FIELD, ""))
    return raw_score, rationale


def clamp_score(value: float) -> float:
    """Fold a finite float onto the closed interval ``[0.0, 1.0]``.

    A value below ``0.0`` becomes ``0.0`` and a value above ``1.0`` becomes
    ``1.0``; an in-range value is returned unchanged with no rounding or
    scaling. The input is assumed finite — :func:`parse_score` has already
    rejected NaN and the infinities — so this function performs no parsing
    and never raises.
    """
    if value < _LOWER_BOUND:
        return _LOWER_BOUND
    if value > _UPPER_BOUND:
        return _UPPER_BOUND
    return value
