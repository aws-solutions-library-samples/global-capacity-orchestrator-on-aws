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

# The Markdown code-fence delimiter chat models commonly wrap a JSON answer in.
_CODE_FENCE = "```"


def _strip_code_fence(text: str) -> str:
    """Return the body of a whole-response Markdown code fence, else the text.

    A fenced answer opens with ``` (optionally tagged, e.g. ```json) on its
    own line and closes with ``` on a later line. When ``text`` both opens and
    closes with a fence, the opening line — language tag and all — and the
    trailing fence are dropped and the body between them is returned; anything
    not fully fenced is returned unchanged. Pure and deterministic.
    """
    if not (text.startswith(_CODE_FENCE) and text.endswith(_CODE_FENCE)):
        return text
    # Drop the opening fence line (``` plus any language tag), then everything
    # from the final fence onward, leaving just the fenced body.
    _, _, after_open = text.partition("\n")
    body, _, _ = after_open.rpartition(_CODE_FENCE)
    return body.strip()


def _extract_json_payload(raw_text: str) -> str:
    """Return the most likely JSON substring of a model response.

    Models often wrap the requested JSON object in a Markdown code fence or
    surround it with a sentence of prose. This peels a whole-response code
    fence when present, then, failing a direct decode, falls back to the
    substring spanning the first ``{`` and the last ``}``. The result is only
    a *candidate* — the caller still decodes it and validates the shape, so a
    candidate that is not real JSON is rejected downstream like any other
    untrustworthy output. The transformation is pure and deterministic.
    """
    text = _strip_code_fence(raw_text.strip())

    # If what remains already decodes, use it as-is.
    try:
        json.loads(text)
        return text
    except ValueError, TypeError:
        pass

    # Last resort: carve out the first balanced-looking object span. This
    # rescues a JSON object embedded in leading/trailing prose without trying
    # to be a full parser — the carved span is still decoded by the caller.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]

    return text


def parse_score(raw_text: str) -> tuple[float, str]:
    """Decode the model's raw response into ``(raw_score, rationale)``.

    The raw text must carry a JSON object with a real, finite numeric
    ``score`` field. A whole-response Markdown code fence (```` ```json ... ``` ````)
    or a sentence of surrounding prose is tolerated — :func:`_extract_json_payload`
    peels it before decoding — because chat models routinely wrap a JSON
    answer that way despite being asked for raw JSON. The returned
    ``raw_score`` is that value coerced to a float and is **not** yet clamped,
    so the caller can record it verbatim as provenance. The returned
    ``rationale`` is the model's ``rationale`` field coerced to a string,
    defaulting to an empty string when absent.

    Every way the response can fail to yield a trustworthy number raises
    :class:`JudgeError` with code :attr:`ErrorCode.INVALID_MODEL_SCORE` and a
    ``reason`` in ``details`` identifying the specific failure:

    * ``non_json`` — no JSON object could be decoded from the text.
    * ``missing_score_field`` — the JSON is not an object, or has no
      ``score`` field.
    * ``non_numeric`` — the ``score`` is a boolean, string, null, or other
      non-numeric value.
    * ``non_finite`` — the ``score`` is NaN or positive/negative infinity.
    """
    payload = _extract_json_payload(raw_text)
    try:
        parsed = json.loads(payload)
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
