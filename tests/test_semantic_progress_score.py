"""Tests for the semantic-progress judge's score parsing and clamping.

Turning the model's raw answer into a number a threshold comparison can read
is split into two deliberately separate steps, and these tests cover them in
kind:

* ``clamp_score`` folds a finite float onto the closed unit interval
  ``[0.0, 1.0]`` — values below the floor snap to ``0.0``, values above the
  ceiling snap to ``1.0``, and in-range values pass through untouched. It is a
  total function on finite floats: it parses nothing and never raises.
* ``parse_score`` owns the only failure path, decoding the raw text and
  validating that it carries a real, finite numeric score.

The clamp is the piece exercised here. The property test drives it across the
whole finite real line — below, within, and above the interval — and the
focused unit tests pin the boundary corners (the exact bounds, negative zero,
and far-out magnitudes).
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# ``gco_mcp/run_mcp.py`` puts ``gco_mcp/`` on ``sys.path`` at runtime; mirror that
# here so the pure ``mission_judge`` package imports the same way it does in
# production, matching the convention used by the sibling Mission tests.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from mission_judge.score import clamp_score, parse_score  # noqa: E402
from mission_judge.shape import ErrorCode, JudgeError  # noqa: E402

# The inclusive bounds of the progress-score interval the clamp targets.
_LOWER = 0.0
_UPPER = 1.0


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Finite floats strictly below the floor: every one must snap up to 0.0.
_below = st.floats(max_value=_LOWER, exclude_max=True, allow_nan=False, allow_infinity=False)

# Finite floats inside the closed interval, bounds included: every one must
# pass through unchanged.
_within = st.floats(min_value=_LOWER, max_value=_UPPER, allow_nan=False, allow_infinity=False)

# Finite floats strictly above the ceiling: every one must snap down to 1.0.
_above = st.floats(min_value=_UPPER, exclude_min=True, allow_nan=False, allow_infinity=False)

# The union of all three regions, so a single property exercises values below,
# within, and above the interval.
_finite_floats = st.one_of(_below, _within, _above)


# ---------------------------------------------------------------------------
# Property: clamping folds onto the closed unit interval
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(value=_finite_floats)
def test_clamp_score_folds_onto_unit_interval(value: float) -> None:
    """``clamp_score`` always lands in ``[0.0, 1.0]`` by snapping to the nearest bound.

    For any finite float below, within, or above the interval:

    * the result is always within the closed interval ``[0.0, 1.0]``;
    * a value below ``0.0`` snaps to ``0.0``;
    * a value above ``1.0`` snaps to ``1.0``;
    * an in-range value is returned unchanged, with no rounding or scaling; and
    * the input value is left untouched, so it remains available verbatim for
      the caller to record as the pre-clamp raw score.
    """
    result = clamp_score(value)

    # The clamped result never escapes the closed unit interval.
    assert _LOWER <= result <= _UPPER

    if value < _LOWER:
        assert result == _LOWER
    elif value > _UPPER:
        assert result == _UPPER
    else:
        # In-range values are returned byte-for-byte unchanged.
        assert result == value

    # Clamping reads, but never mutates, its argument: the original finite
    # value is preserved for the caller to keep as raw-score provenance.
    assert math.isfinite(value)


# ---------------------------------------------------------------------------
# Focused unit tests for the clamp boundaries
# ---------------------------------------------------------------------------


def test_clamp_score_returns_bounds_unchanged() -> None:
    """The interval's own bounds are in range and pass through untouched."""
    assert clamp_score(0.0) == 0.0
    assert clamp_score(1.0) == 1.0


def test_clamp_score_snaps_below_zero_up_to_floor() -> None:
    """Any value under the floor — however small — snaps up to ``0.0``."""
    assert clamp_score(-0.0001) == 0.0
    assert clamp_score(-1.0) == 0.0
    assert clamp_score(-1e308) == 0.0


def test_clamp_score_snaps_above_one_down_to_ceiling() -> None:
    """Any value over the ceiling — however large — snaps down to ``1.0``."""
    assert clamp_score(1.0001) == 1.0
    assert clamp_score(2.0) == 1.0
    assert clamp_score(1e308) == 1.0


def test_clamp_score_treats_negative_zero_as_in_range() -> None:
    """Negative zero is not below the floor, so it is returned unchanged."""
    assert clamp_score(-0.0) == 0.0


def test_clamp_score_leaves_a_mid_interval_value_unchanged() -> None:
    """A representative in-range value is emitted with no modification."""
    assert clamp_score(0.42) == 0.42


# ---------------------------------------------------------------------------
# Strategies for score parsing
# ---------------------------------------------------------------------------

# Finite, non-bool numbers the model is allowed to report as its score. Both
# integers and finite floats round-trip cleanly through json.dumps/json.loads,
# so the parsed value matches float(score) exactly.
_valid_scores = st.one_of(
    st.integers(min_value=-(10**6), max_value=10**6),
    st.floats(allow_nan=False, allow_infinity=False, allow_subnormal=False),
)

# Free-text rationales the model may attach. Plain printable text keeps the
# round-trip assertion focused on the numeric field rather than JSON escaping.
_rationales = st.text(max_size=200)

# Values that are NOT a usable numeric score: booleans (a JSON-legal type that
# must be rejected), strings, and null. NaN/inf are handled separately because
# json.dumps emits them as non-standard tokens.
_non_numeric_score_values = st.one_of(
    st.booleans(),
    st.text(max_size=50),
    st.none(),
)

# Arbitrary text that is overwhelmingly unlikely to be valid JSON. Filtering
# out the rare strings that happen to parse keeps the "non-JSON" branch honest.
_non_json_text = st.text(max_size=200).filter(lambda s: not _is_json(s))

# JSON values that parse successfully but are not objects: arrays, bare
# numbers, bare strings, bare booleans, and null. None of these carry a
# ``score`` field, so all must be rejected.
_non_object_json = st.one_of(
    st.lists(st.integers(), max_size=5),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.booleans(),
    st.none(),
).map(json.dumps)


def _is_json(text: str) -> bool:
    """True when ``text`` decodes as JSON, used to exclude accidental hits."""
    try:
        json.loads(text)
    except ValueError, TypeError:
        return False
    return True


# ---------------------------------------------------------------------------
# Property: parsing accepts valid scores and rejects every invalid output
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(score=_valid_scores, rationale=_rationales)
def test_parse_score_accepts_valid_numeric_scores(score: float | int, rationale: str) -> None:
    """A JSON object with a finite, non-bool numeric ``score`` parses cleanly.

    ``parse_score`` returns the score coerced to a float — matching the value
    JSON round-trips to — alongside the rationale string, and never raises for
    a well-formed object. The returned score is finite, so it is always safe
    to hand straight to ``clamp_score``.
    """
    raw = json.dumps({"score": score, "rationale": rationale})

    raw_score, parsed_rationale = parse_score(raw)

    expected = float(json.loads(raw)["score"])
    assert raw_score == expected
    assert math.isfinite(raw_score)
    assert parsed_rationale == rationale


@settings(max_examples=200, deadline=None)
@given(score=_valid_scores)
def test_parse_score_defaults_rationale_when_absent(score: float | int) -> None:
    """A valid score with no ``rationale`` field parses with an empty rationale."""
    raw = json.dumps({"score": score})

    raw_score, parsed_rationale = parse_score(raw)

    assert raw_score == float(json.loads(raw)["score"])
    assert parsed_rationale == ""


@settings(max_examples=200, deadline=None)
@given(text=_non_json_text)
def test_parse_score_rejects_non_json_text(text: str) -> None:
    """Text that is not JSON raises an invalid-score error and yields no value."""
    with pytest.raises(JudgeError) as exc_info:
        parse_score(text)

    assert exc_info.value.code == ErrorCode.INVALID_MODEL_SCORE


@settings(max_examples=200, deadline=None)
@given(raw=_non_object_json)
def test_parse_score_rejects_json_that_is_not_an_object(raw: str) -> None:
    """JSON that decodes to a non-object (array, bare scalar, null) is rejected."""
    with pytest.raises(JudgeError) as exc_info:
        parse_score(raw)

    assert exc_info.value.code == ErrorCode.INVALID_MODEL_SCORE


@settings(max_examples=200, deadline=None)
@given(rationale=_rationales)
def test_parse_score_rejects_object_without_score_field(rationale: str) -> None:
    """A JSON object that carries no ``score`` field is rejected."""
    raw = json.dumps({"rationale": rationale})

    with pytest.raises(JudgeError) as exc_info:
        parse_score(raw)

    assert exc_info.value.code == ErrorCode.INVALID_MODEL_SCORE


@settings(max_examples=200, deadline=None)
@given(value=_non_numeric_score_values, rationale=_rationales)
def test_parse_score_rejects_non_numeric_score(value: object, rationale: str) -> None:
    """A ``score`` that is a bool, string, or null is not a usable number.

    Booleans matter most here: ``bool`` is a subclass of ``int``, so a naive
    numeric check would wrongly accept ``true``/``false``. The parser must
    reject all three without returning a value to clamp.
    """
    raw = json.dumps({"score": value, "rationale": rationale})

    with pytest.raises(JudgeError) as exc_info:
        parse_score(raw)

    assert exc_info.value.code == ErrorCode.INVALID_MODEL_SCORE


@settings(max_examples=200, deadline=None)
@given(token=st.sampled_from(["NaN", "Infinity", "-Infinity"]), rationale=_rationales)
def test_parse_score_rejects_non_finite_score(token: str, rationale: str) -> None:
    """A ``score`` of NaN or +/-infinity is rejected before it can be clamped.

    A model can only express these as the non-standard JSON tokens ``NaN``,
    ``Infinity``, and ``-Infinity``, which Python's ``json.loads`` accepts by
    default. The parser must catch the resulting non-finite float rather than
    pass it through to the clamp.
    """
    raw = '{"score": ' + token + ', "rationale": ' + json.dumps(rationale) + "}"

    # Guard the test's own assumption: these tokens really do decode to a
    # non-finite float, so the rejection is exercising the intended branch.
    assert not math.isfinite(json.loads(raw)["score"])

    with pytest.raises(JudgeError) as exc_info:
        parse_score(raw)

    assert exc_info.value.code == ErrorCode.INVALID_MODEL_SCORE


# ---------------------------------------------------------------------------
# Property: a fenced or prose-wrapped object parses like the bare object
# ---------------------------------------------------------------------------

# The Markdown-fence wrappers a chat model commonly puts around a JSON answer
# despite being asked for raw JSON: a tagged ```json fence, a bare ``` fence,
# and an uppercase language tag, with and without a line of surrounding prose.
# Each must be peeled so the object inside parses exactly as it would alone.
_fence_wrappers = st.sampled_from(
    [
        "```json\n{body}\n```",
        "```\n{body}\n```",
        "```JSON\n{body}\n```",
        "Here is the score:\n```json\n{body}\n```",
        "```json\n{body}\n```\nThat is my assessment.",
    ]
)


@settings(max_examples=200, deadline=None)
@given(score=_valid_scores, rationale=_rationales, wrapper=_fence_wrappers)
def test_parse_score_peels_code_fence_around_object(
    score: float | int, rationale: str, wrapper: str
) -> None:
    """A valid object wrapped in a Markdown fence parses like the bare object.

    Models routinely answer with a ```json ... ``` fence even when asked for
    raw JSON, so the parser peels a whole-response fence (and any one line of
    prose around it) before decoding. The result must equal what the bare,
    unfenced object would have produced.
    """
    body = json.dumps({"score": score, "rationale": rationale})
    raw = wrapper.format(body=body)

    raw_score, parsed_rationale = parse_score(raw)

    assert raw_score == float(json.loads(body)["score"])
    assert math.isfinite(raw_score)
    assert parsed_rationale == rationale


def test_parse_score_peels_realistic_claude_fenced_response() -> None:
    """A representative fenced Claude response parses to its score and rationale.

    Mirrors the exact wrapping observed from a live model: a ```json fence
    around a pretty-printed object carrying both fields.
    """
    raw = '```json\n{\n  "score": 1.0,\n  "rationale": "The objective is fully satisfied."\n}\n```'

    raw_score, rationale = parse_score(raw)

    assert raw_score == 1.0
    assert rationale == "The objective is fully satisfied."


def test_parse_score_extracts_object_embedded_in_prose() -> None:
    """A JSON object surrounded by prose (no fence) is carved out and parsed."""
    raw = 'I judge this run as follows: {"score": 0.5, "rationale": "halfway"} done.'

    raw_score, rationale = parse_score(raw)

    assert raw_score == 0.5
    assert rationale == "halfway"


def test_parse_score_still_rejects_fence_with_no_json_inside() -> None:
    """A code fence whose body is not JSON is still rejected as non-JSON."""
    raw = "```json\nnot a json object at all\n```"

    with pytest.raises(JudgeError) as exc_info:
        parse_score(raw)

    assert exc_info.value.code == ErrorCode.INVALID_MODEL_SCORE
