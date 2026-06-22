"""Tests for the semantic-progress judge's canonical-shape builder.

A successful judge result has a fixed shape: a top-level ``metrics`` object
that maps a single well-formed output name to one finite progress score,
with every piece of provenance (the model rationale, the source identifier,
the resolved backend name and model id, the rubric version, and the raw
pre-clamp score) placed *beside* ``metrics`` at the top level rather than
inside it. A downstream consumer merges only the ``metrics`` object, so that
object must contain nothing but the numeric value and provenance must never
leak into it.

These tests pin that invariant down. The property test drives
``metrics_result`` with a wide range of valid output names, finite scores in
the unit interval, rationale strings, and arbitrary provenance values; the
focused unit tests nail the corner case where the chosen output name happens
to equal a provenance field name and must still stay inside ``metrics``.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ``gco_mcp/run_mcp.py`` puts ``gco_mcp/`` on ``sys.path`` at runtime; mirror that
# here so the pure ``mission_judge`` package imports the same way it does in
# production, matching the convention used by the sibling Mission tests.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from mission_judge.shape import (  # noqa: E402
    ErrorCode,
    JudgeError,
    error_envelope,
    is_finite_float,
    metrics_result,
    validate_output_name,
)

# The longest rationale the tool wrapper retains, in characters. The wrapper
# slices the model rationale to this bound before handing it to
# ``metrics_result``; ``metrics_result`` itself stores whatever it is given
# verbatim, so the generated rationale is bounded to the same ceiling to
# mirror what the wrapper passes in. Defined locally because the prompt
# module that owns this constant in production may not be present yet.
_MAX_RATIONALE_CHARS = 2000

# The six provenance fields ``metrics_result`` places beside ``metrics`` at
# the top level, plus the canonical ``metrics`` map itself. A successful
# result's top-level keys are exactly this set.
_PROVENANCE_FIELDS = frozenset(
    {"rationale", "source", "backend_name", "model_id", "rubric_version", "raw_score"}
)
_TOP_LEVEL_KEYS = _PROVENANCE_FIELDS | {"metrics"}


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# A valid output name: 1..128 printable, non-space ASCII characters with the
# "." separator excluded. This is exactly a single well-formed path segment,
# so every generated name is a legal ``metrics`` key.
_output_names = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126, blacklist_characters="."),
    min_size=1,
    max_size=128,
)

# A finite progress score in the closed unit interval [0.0, 1.0]: the space
# the emitted score always occupies once parsed and clamped.
_scores = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)

# A rationale already bounded to the retention ceiling, mirroring the sliced
# string the wrapper passes in.
_rationales = st.text(max_size=_MAX_RATIONALE_CHARS)

# Arbitrary provenance string values: source identifier, backend name, model
# id (which may itself embed ":"), and rubric version.
_provenance_text = st.text(max_size=64)

# The raw, pre-clamp model score recorded in provenance: any finite float,
# including values outside the unit interval (clamping happens before this
# builder is reached, and the raw value is preserved unmodified).
_raw_scores = st.floats(allow_nan=False, allow_infinity=False)


# ---------------------------------------------------------------------------
# Property: the canonical-shape invariant with provenance strictly outside
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(
    output_name=_output_names,
    score=_scores,
    rationale=_rationales,
    source=_provenance_text,
    backend_name=_provenance_text,
    model_id=_provenance_text,
    rubric_version=_provenance_text,
    raw_score=_raw_scores,
)
def test_metrics_result_isolates_score_from_provenance(
    output_name: str,
    score: float,
    rationale: str,
    source: str,
    backend_name: str,
    model_id: str,
    rubric_version: str,
    raw_score: float,
) -> None:
    """``metrics_result`` always yields a canonical, provenance-free metrics map.

    For any valid output name, finite score in the unit interval, bounded
    rationale, and arbitrary provenance values:

    * the result is a dict whose top-level keys are exactly the six
      provenance fields plus ``metrics`` — nothing else;
    * the top-level ``metrics`` object maps exactly the one output name to
      exactly the finite score, with no provenance leaked in;
    * the lone value inside ``metrics`` passes the finite-number guard;
    * every provenance field survives verbatim at the top level, beside the
      ``metrics`` object; and
    * the retained rationale never exceeds the retention ceiling.
    """
    result = metrics_result(
        output_name,
        score,
        rationale=rationale,
        source=source,
        backend_name=backend_name,
        model_id=model_id,
        rubric_version=rubric_version,
        raw_score=raw_score,
    )

    # Top-level container is a dict carrying exactly the provenance fields and
    # the canonical ``metrics`` map.
    assert isinstance(result, dict)
    assert set(result) == _TOP_LEVEL_KEYS

    # The metrics object maps exactly the one output name to exactly the
    # numeric score — one key, one value, no provenance leaked in.
    assert isinstance(result["metrics"], dict)
    assert result["metrics"] == {output_name: score}
    assert set(result["metrics"]) == {output_name}
    assert is_finite_float(result["metrics"][output_name])

    # Every provenance field lives at the top level, outside ``metrics``,
    # preserved verbatim.
    assert result["rationale"] == rationale
    assert result["source"] == source
    assert result["backend_name"] == backend_name
    assert result["model_id"] == model_id
    assert result["rubric_version"] == rubric_version
    assert result["raw_score"] == raw_score

    # The retained rationale is bounded by the retention ceiling.
    assert len(result["rationale"]) <= _MAX_RATIONALE_CHARS


# ---------------------------------------------------------------------------
# Focused unit tests for the canonical-shape builder
# ---------------------------------------------------------------------------


def test_output_name_equal_to_provenance_field_name_stays_in_metrics() -> None:
    """An output name equal to a provenance field name still lives inside ``metrics``.

    The output name is only ever a key inside the ``metrics`` object, so even
    when it collides with the name of a top-level provenance field the two
    values live at different levels: the numeric score inside ``metrics`` and
    the provenance string beside it.
    """
    result = metrics_result(
        "source",
        0.5,
        rationale="steady progress",
        source="bedrock:us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        backend_name="bedrock",
        model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        rubric_version="spj-v1",
        raw_score=0.5,
    )
    assert result["metrics"] == {"source": 0.5}
    assert result["source"] == "bedrock:us.anthropic.claude-sonnet-4-5-20250929-v1:0"


def test_metrics_result_places_all_provenance_beside_the_metrics_map() -> None:
    """A representative result keeps the score in ``metrics`` and provenance beside it."""
    result = metrics_result(
        "progress_score",
        0.75,
        rationale="most of the objective is satisfied",
        source="mcp:claude",
        backend_name="mcp",
        model_id="claude",
        rubric_version="spj-v1",
        raw_score=0.75,
    )
    assert result["metrics"] == {"progress_score": 0.75}
    assert "metrics" not in result["metrics"]
    assert result["rationale"] == "most of the objective is satisfied"
    assert result["source"] == "mcp:claude"
    assert result["backend_name"] == "mcp"
    assert result["model_id"] == "claude"
    assert result["rubric_version"] == "spj-v1"
    assert result["raw_score"] == 0.75


# ---------------------------------------------------------------------------
# Property: the finite-number guard accepts exactly real, finite numbers
# ---------------------------------------------------------------------------

# A mixed bag of values spanning every type the guard must rule on: booleans
# (which masquerade as ints), ``None``, arbitrary text, plain ints, and floats
# that include NaN and the infinities alongside ordinary finite values. Drawing
# from a single shared strategy lets one property exercise both the accept and
# the reject side of the guard.
_mixed_values = st.one_of(
    st.booleans(),
    st.none(),
    st.text(max_size=32),
    st.integers(),
    st.floats(allow_nan=True, allow_infinity=True),
)


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(value=_mixed_values)
def test_is_finite_float_accepts_only_real_finite_numbers(value: object) -> None:
    """``is_finite_float`` is True exactly for non-boolean ints and finite floats.

    Across booleans, ``None``, strings, ints, and floats (including NaN and the
    infinities), the guard agrees with the plain-language rule it stands in for:
    a value counts as a real, finite number when it is an ``int`` or a finite
    ``float`` and is *not* a ``bool``. The check is an exact equivalence — the
    guard accepts every value the rule accepts and rejects every value it
    rejects, with no gap in either direction.
    """
    # The oracle: an int or a finite float, but never a bool. ``isinstance``
    # checks come first so booleans (a subclass of int) and the NaN/inf floats
    # are handled explicitly before ``math.isfinite`` is consulted.
    expected = (not isinstance(value, bool)) and (
        isinstance(value, int) or (isinstance(value, float) and math.isfinite(value))
    )

    assert is_finite_float(value) is expected


# ---------------------------------------------------------------------------
# Property: the output-name validator round-trips valid names and rejects bad
# ---------------------------------------------------------------------------

# A well-formed output name: 1..128 printable, non-space ASCII characters with
# the "." separator excluded — exactly a single legal path segment that the
# validator must return untouched.
_valid_name_cases = _output_names.map(lambda name: (name, True))

# The empty string: rejected because a name must carry at least one character.
_empty_names = st.just("")

# Names longer than the 128-character ceiling. The content is otherwise legal
# (no separator, no whitespace), so length alone is what makes them invalid.
_too_long_names = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126, blacklist_characters="."),
    min_size=129,
    max_size=300,
)

# Names carrying at least one "." separator. Joining two or more legal segments
# on "." guarantees a separator is present regardless of segment content.
_dotted_names = st.lists(
    st.text(
        alphabet=st.characters(min_codepoint=33, max_codepoint=126, blacklist_characters="."),
        max_size=16,
    ),
    min_size=2,
    max_size=4,
).map(".".join)

# Names carrying at least one whitespace character. A legal head and tail are
# joined on a single whitespace character drawn from the usual spread, so a
# whitespace character is always present.
_whitespace_segments = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126, blacklist_characters="."),
    max_size=16,
)
_whitespace_names = st.builds(
    lambda head, ws, tail: head + ws + tail,
    _whitespace_segments,
    st.sampled_from([" ", "\t", "\n", "\r", "\v", "\f"]),
    _whitespace_segments,
)

# The invalid side: empty, over-length, separator-bearing, or whitespace-bearing
# names, each tagged as one the validator must refuse.
_invalid_name_cases = st.one_of(
    _empty_names,
    _too_long_names,
    _dotted_names,
    _whitespace_names,
).map(lambda name: (name, False))

# Both sides of the contract in one stream of (name, is_valid) pairs.
_name_cases = st.one_of(_valid_name_cases, _invalid_name_cases)


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(case=_name_cases)
def test_validate_output_name_round_trips_valid_and_rejects_invalid(
    case: tuple[str, bool],
) -> None:
    """``validate_output_name`` returns well-formed names and refuses bad ones.

    For a name that is a single legal path segment — 1 to 128 characters with
    no "." separator and no whitespace — the validator returns it byte-for-byte
    unchanged. For any name that is empty, longer than 128 characters, carries a
    "." separator, or carries a whitespace character, the validator raises the
    judge's error with the stable invalid-name code rather than returning a
    value.
    """
    name, is_valid = case
    if is_valid:
        assert validate_output_name(name) == name
    else:
        with pytest.raises(JudgeError) as exc_info:
            validate_output_name(name)
        assert exc_info.value.code == ErrorCode.INVALID_OUTPUT_NAME


# ---------------------------------------------------------------------------
# Property: the error envelope never carries a top-level metrics key
# ---------------------------------------------------------------------------

# A failure code: any string the builder might be handed. The builder does
# not validate the code, so the full range of text exercises it.
_error_codes = st.text(max_size=64)

# An identifier-like detail key safe to splat through ``**details``. Keys are
# drawn from ASCII letters and the underscore so every key is a legal keyword
# name, and "code" is excluded so it cannot collide with the positional
# ``code`` parameter the builder already binds.
_detail_keys = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_",
    min_size=1,
    max_size=24,
).filter(lambda key: key != "code")

# Arbitrary detail values: the assorted JSON-ish payloads a failure might
# attach — strings, ints, finite floats, None, and nested lists/dicts. NaN is
# excluded so the round-trip equality check below stays meaningful.
_detail_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=True)
    | st.text(max_size=32),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=16), children, max_size=4)
    ),
    max_leaves=8,
)

# A details payload: a mapping of identifier-like keys to assorted values,
# ready to splat through ``**details``.
_details_payloads = st.dictionaries(_detail_keys, _detail_values, max_size=6)


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(code=_error_codes, details=_details_payloads)
def test_error_envelope_never_carries_top_level_metrics(
    code: str,
    details: dict[str, object],
) -> None:
    """``error_envelope`` always yields ``{"code", "details"}`` with no top-level metrics.

    For any code string and any details payload of identifier-like keys mapped
    to assorted values:

    * the result is a dict whose top-level keys are exactly ``code`` and
      ``details`` — nothing else;
    * the code is preserved verbatim and the details payload round-trips
      unchanged; and
    * there is no top-level ``metrics`` key, so a consumer that merges only
      ``metrics``-shaped results skips the envelope and leaves the
      corresponding check undecided rather than acting on a failure.
    """
    result = error_envelope(code, **details)

    assert isinstance(result, dict)
    assert set(result) == {"code", "details"}
    assert result["code"] == code
    assert result["details"] == details
    assert "metrics" not in result


def test_error_envelope_with_metrics_detail_keeps_it_nested() -> None:
    """A detail named ``metrics`` lands under ``details``, never at the top level.

    Even when a failure attaches its own field literally named ``metrics``, the
    builder nests it under ``details``; the top-level object still exposes only
    ``code`` and ``details``, so a consumer merging top-level ``metrics`` never
    mistakes the diagnostic payload for a real score.
    """
    result = error_envelope("invalid_model_score", metrics={"progress_score": 0.5})

    assert set(result) == {"code", "details"}
    assert "metrics" not in result
    assert result["details"]["metrics"] == {"progress_score": 0.5}
