"""Tests for the canonical metric-result builder.

A metric reader's success output has a fixed shape: a top-level ``metrics``
object that maps a single well-formed key to one real number, with every
piece of provenance (source, region, timestamp, aggregation mode, raw
datapoint, and so on) placed *beside* ``metrics`` at the top level rather
than inside it. Downstream consumers merge only the ``metrics`` object, so
that object must contain nothing but the numeric value, and provenance must
never leak into it — not even a provenance field that happens to be named
``metrics``.

These tests pin that invariant down. The property test drives
``metrics_result`` with a wide range of valid keys, numeric values, and
arbitrary provenance payloads; the focused unit tests nail the two corner
cases that matter most: a provenance field colliding with the canonical
``metrics`` key, and a provenance field colliding with the metric key name.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ``mcp/run_mcp.py`` puts ``mcp/`` on ``sys.path`` at runtime; mirror that
# here so the pure ``metric_readers`` package imports the same way it does
# in production, matching the convention used by the sibling Mission tests.
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))

from metric_readers.shape import (  # noqa: E402
    is_numeric_value,
    metrics_result,
    validate_metric_name,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# A valid metric key: 1..128 printable non-space ASCII characters with no
# "." separator. This is exactly the space ``validate_metric_name`` accepts,
# so every generated key is a legal single path segment.
_metric_keys = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126, blacklist_characters="."),
    min_size=1,
    max_size=128,
)

# A real, finite number: an int (never a bool, since ``st.integers`` does not
# emit bools) or a finite float (never NaN or +/-inf).
_numeric_values = st.one_of(
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
)

# Provenance keys are ordinary identifier-ish ASCII strings. Provenance values
# are JSON-shaped and finite, so a plain ``==`` deep-compare is meaningful.
_provenance_keys = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), max_codepoint=127),
    min_size=1,
    max_size=16,
)
_provenance_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=24),
)
_provenance_values = st.recursive(
    _provenance_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(_provenance_keys, children, max_size=4),
    ),
    max_leaves=8,
)


@st.composite
def _provenance(draw: st.DrawFn) -> dict[str, Any]:
    """Draw an arbitrary provenance payload.

    Roughly half the time a key literally named ``metrics`` is injected, so
    the property exercises the case where a provenance field collides with
    the canonical ``metrics`` map and must lose.
    """
    payload: dict[str, Any] = draw(
        st.dictionaries(_provenance_keys, _provenance_values, max_size=6)
    )
    if draw(st.booleans()):
        payload["metrics"] = draw(_provenance_values)
    return payload


# ---------------------------------------------------------------------------
# Property: the canonical-shape invariant
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(key=_metric_keys, value=_numeric_values, provenance=_provenance())
def test_metrics_result_isolates_numeric_metric_from_provenance(
    key: str,
    value: float,
    provenance: dict[str, Any],
) -> None:
    """``metrics_result`` always yields a canonical, provenance-free metrics map.

    For any valid key, finite numeric value, and arbitrary provenance payload:

    * the result is a dict whose top-level ``metrics`` is exactly
      ``{key: value}`` — one key mapping to one numeric value, nothing else;
    * the lone value inside ``metrics`` passes the numeric guard;
    * every provenance field (other than a colliding ``metrics`` key) survives
      verbatim at the top level, outside the ``metrics`` object; and
    * a provenance field named ``metrics`` never clobbers the canonical map.
    """
    result = metrics_result(key, value, **provenance)

    # Top-level container is a dict carrying a ``metrics`` object.
    assert isinstance(result, dict)
    assert isinstance(result["metrics"], dict)

    # The metrics object maps exactly the one key to exactly the numeric
    # value — no provenance leaked in, and a colliding ``metrics`` provenance
    # field did not overwrite it.
    assert result["metrics"] == {key: value}
    assert set(result["metrics"]) == {key}
    assert is_numeric_value(result["metrics"][key])

    # Every provenance field lives at the top level, beside ``metrics``.
    for prov_key, prov_value in provenance.items():
        if prov_key == "metrics":
            # The canonical map wins over any provenance field of this name.
            continue
        assert result[prov_key] == prov_value


# ---------------------------------------------------------------------------
# Focused unit tests for the two collision corner cases
# ---------------------------------------------------------------------------


def test_provenance_named_metrics_cannot_clobber_canonical_map() -> None:
    """A provenance field named ``metrics`` loses to the canonical metrics map."""
    result = metrics_result(
        "loss",
        0.42,
        metrics="not-a-metrics-map",
        source="job_logs:trainer",
    )
    assert result["metrics"] == {"loss": 0.42}
    assert result["source"] == "job_logs:trainer"


def test_provenance_sharing_the_metric_key_name_stays_at_top_level() -> None:
    """A provenance field whose name equals the metric key sits beside ``metrics``.

    The two values live at different levels: the raw provenance value at the
    top level, and the numeric metric inside the ``metrics`` object.
    """
    result = metrics_result("loss", 0.42, loss="raw-string-value")
    assert result["metrics"] == {"loss": 0.42}
    assert result["loss"] == "raw-string-value"


def test_metric_key_generator_produces_only_valid_names() -> None:
    """Sanity check: a representative generated-style key validates cleanly."""
    name = "eval_loss-step42"
    assert validate_metric_name(name) == name


# ---------------------------------------------------------------------------
# Property: the numeric-value guard
# ---------------------------------------------------------------------------

# A grab-bag of values spanning every type the guard must rule on. It mixes
# the two accepted kinds (plain ints and finite floats) with the look-alikes
# that must be rejected: booleans (an ``int`` subclass), the non-finite floats
# (NaN and +/-inf), numeric-looking and arbitrary strings, ``None``, and a
# range of container and other object types.
_mixed_values = st.one_of(
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.just(float("nan")),
    st.just(float("inf")),
    st.just(float("-inf")),
    st.text(),
    st.from_regex(r"-?\d+(\.\d+)?", fullmatch=True),  # numeric-looking strings
    st.none(),
    st.lists(st.integers(), max_size=4),
    st.tuples(st.integers(), st.integers()),
    st.dictionaries(st.text(max_size=8), st.integers(), max_size=4),
    st.sets(st.integers(), max_size=4),
    st.binary(max_size=8),
    st.complex_numbers(allow_nan=False, allow_infinity=False),
)


def _is_real_finite_number(x: object) -> bool:
    """Reference oracle: a real, finite number that is not a boolean.

    Independent of the implementation under test: ``True`` exactly when ``x``
    is a plain ``int`` (excluding ``bool``) or a finite ``float``.
    """
    if isinstance(x, bool):
        return False
    if isinstance(x, int):
        return True
    if isinstance(x, float):
        return math.isfinite(x)
    return False


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(value=_mixed_values)
def test_numeric_guard_accepts_only_real_finite_numbers(value: object) -> None:
    """``is_numeric_value`` is True iff the value is a non-bool int or finite float.

    Across every generated type, the guard agrees with an independent oracle:
    plain ints and finite floats pass; booleans, NaN, the infinities, strings
    (even numeric-looking ones), ``None``, and containers are all rejected.
    """
    assert is_numeric_value(value) is _is_real_finite_number(value)


def test_numeric_guard_rejects_representative_non_numerics() -> None:
    """Spot-check the headline rejections the guard must make."""
    for rejected in (
        True,
        False,
        float("nan"),
        float("inf"),
        float("-inf"),
        "3.14",
        "42",
        "",
        None,
        [1, 2, 3],
        {"loss": 0.5},
        (1, 2),
        b"1.0",
    ):
        assert is_numeric_value(rejected) is False


def test_numeric_guard_accepts_representative_numerics() -> None:
    """Spot-check the values the guard must accept."""
    for accepted in (0, 1, -7, 3.14, -0.0, 1e308):
        assert is_numeric_value(accepted) is True


# ---------------------------------------------------------------------------
# Property: the metric-name validator round-trip
# ---------------------------------------------------------------------------

import pytest  # noqa: E402
from metric_readers.shape import (  # noqa: E402
    ErrorCode,
    MetricReaderError,
)

# A valid metric name: 1..128 characters drawn from printable, non-space
# ASCII with the "." separator excluded. This is exactly the space
# ``validate_metric_name`` must accept and echo back unchanged: a single
# well-formed path segment with no "." and no whitespace.
_valid_metric_names = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126, blacklist_characters="."),
    min_size=1,
    max_size=128,
)

# An over-length name: 129..256 characters, otherwise well-formed, so the
# *only* reason it is invalid is that it exceeds the 128-character cap.
_oversize_metric_names = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126, blacklist_characters="."),
    min_size=129,
    max_size=256,
)

# Every character ``str.isspace`` recognises as whitespace and that the
# validator must therefore reject.
_whitespace_chars = st.sampled_from([" ", "\t", "\n", "\r", "\f", "\v"])

# A short, "." -free, whitespace-free fragment used to build invalid names by
# splicing in an illegal character.
_clean_fragments = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126, blacklist_characters="."),
    max_size=64,
)


@st.composite
def _names_with_dot(draw: st.DrawFn) -> str:
    """Build a name that is invalid because it contains a ``.`` separator."""
    left = draw(_clean_fragments)
    right = draw(_clean_fragments)
    return left + "." + right


@st.composite
def _names_with_whitespace(draw: st.DrawFn) -> str:
    """Build a name that is invalid because it contains a whitespace character."""
    left = draw(_clean_fragments)
    right = draw(_clean_fragments)
    return left + draw(_whitespace_chars) + right


# An invalid metric name spanning all four rejection reasons in R1.7: empty,
# longer than 128 characters, containing a "." separator, or containing
# whitespace.
_invalid_metric_names = st.one_of(
    st.just(""),
    _oversize_metric_names,
    _names_with_dot(),
    _names_with_whitespace(),
)

# A classified name: the string paired with whether the validator must accept
# it. Drawing both arms in one strategy lets the single property exercise the
# accept-unchanged and reject-with-code halves of the round-trip together.
_classified_metric_names = st.one_of(
    _valid_metric_names.map(lambda n: (n, True)),
    _invalid_metric_names.map(lambda n: (n, False)),
)


# Feature: mission-metric-reader-tools, Property 3: Metric-name validator round-trip
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(case=_classified_metric_names)
def test_metric_name_validator_round_trip(case: tuple[str, bool]) -> None:
    """``validate_metric_name`` echoes valid names and rejects invalid ones.

    Validates: Requirements 1.3, 1.7

    For any generated name:

    * a valid name — a non-empty string of at most 128 characters with no
      ``.`` separator and no whitespace — is returned unchanged; and
    * an invalid name — empty, longer than 128 characters, containing a
      ``.`` separator, or containing any whitespace character — raises
      :class:`MetricReaderError` carrying
      :attr:`ErrorCode.METRIC_NAME_INVALID`.
    """
    name, is_valid = case
    if is_valid:
        assert validate_metric_name(name) == name
    else:
        with pytest.raises(MetricReaderError) as exc_info:
            validate_metric_name(name)
        assert exc_info.value.code == ErrorCode.METRIC_NAME_INVALID


# ---------------------------------------------------------------------------
# Focused unit tests for the validator's boundaries and each rejection reason
# ---------------------------------------------------------------------------


def test_valid_name_is_returned_unchanged() -> None:
    """A well-formed name comes back as the same object, untouched."""
    name = "eval_loss-step42"
    assert validate_metric_name(name) is name


def test_name_at_the_128_character_boundary_is_accepted() -> None:
    """Exactly 128 characters is the longest name the validator allows."""
    name = "a" * 128
    assert validate_metric_name(name) == name


def test_name_one_over_the_boundary_is_rejected() -> None:
    """129 characters trips the length cap with the invalid-name code."""
    with pytest.raises(MetricReaderError) as exc_info:
        validate_metric_name("a" * 129)
    assert exc_info.value.code == ErrorCode.METRIC_NAME_INVALID


def test_empty_name_is_rejected() -> None:
    """An empty name is invalid."""
    with pytest.raises(MetricReaderError) as exc_info:
        validate_metric_name("")
    assert exc_info.value.code == ErrorCode.METRIC_NAME_INVALID


def test_name_with_dot_separator_is_rejected() -> None:
    """A name containing the ``.`` Dot_Path separator is invalid."""
    with pytest.raises(MetricReaderError) as exc_info:
        validate_metric_name("metrics.loss")
    assert exc_info.value.code == ErrorCode.METRIC_NAME_INVALID


def test_name_with_whitespace_is_rejected() -> None:
    """A name containing any whitespace character is invalid."""
    for bad in ("eval loss", "loss\t", "\nloss", "loss\r"):
        with pytest.raises(MetricReaderError) as exc_info:
            validate_metric_name(bad)
        assert exc_info.value.code == ErrorCode.METRIC_NAME_INVALID


# ---------------------------------------------------------------------------
# Property: error envelopes never carry top-level ``metrics``
# ---------------------------------------------------------------------------

from metric_readers.shape import error_envelope  # noqa: E402

# An error code: either one of the frozen stable codes a reader actually
# surfaces, or an arbitrary string. Mixing the two ensures the builder is
# pinned for the real codes *and* never special-cases its first argument.
_error_codes = st.one_of(
    st.sampled_from(
        [
            ErrorCode.METRIC_NAME_INVALID,
            ErrorCode.INVALID_AGGREGATION_MODE,
            ErrorCode.EMPTY_SEQUENCE,
            ErrorCode.NO_NUMERIC_VALUE,
            ErrorCode.NON_NUMERIC_VALUE,
            ErrorCode.NO_DATAPOINTS,
            ErrorCode.AWS_UNREACHABLE,
            ErrorCode.INVALID_EXTRACTION_MODE,
            ErrorCode.INVALID_REGEX,
            ErrorCode.NO_MATCH,
            ErrorCode.LOG_RETRIEVAL_FAILED,
            ErrorCode.FILE_NOT_FOUND,
            ErrorCode.MALFORMED_FILE,
            ErrorCode.FIELD_NOT_FOUND,
            ErrorCode.FILE_TOO_LARGE,
            ErrorCode.UNSUPPORTED_FORMAT,
            ErrorCode.FORMAT_DEPENDENCY_UNAVAILABLE,
            ErrorCode.PATH_TRAVERSAL_ESCAPE,
            ErrorCode.SYMLINK_ESCAPE,
            ErrorCode.LOCAL_ROOT_NOT_CONFIGURED,
        ]
    ),
    st.text(max_size=40),
)

# Details keys are passed as keyword arguments, so the reserved positional
# ``code`` parameter name is excluded; everything else (including a key named
# ``metrics``) is fair game and must end up nested under ``details``.
_detail_keys = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), max_codepoint=127),
    min_size=1,
    max_size=16,
).filter(lambda k: k != "code")

_detail_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=24),
)
_detail_values = st.recursive(
    _detail_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(_detail_keys, children, max_size=4),
    ),
    max_leaves=8,
)


@st.composite
def _details(draw: st.DrawFn) -> dict[str, Any]:
    """Draw an arbitrary error-details payload.

    Roughly half the time a key literally named ``metrics`` is injected, so
    the property exercises the case where a details field is named
    ``metrics`` and must be nested *inside* ``details`` rather than leaking
    to the top level where the Observe_Phase merge would pick it up.
    """
    payload: dict[str, Any] = draw(st.dictionaries(_detail_keys, _detail_values, max_size=6))
    if draw(st.booleans()):
        payload["metrics"] = draw(_detail_values)
    return payload


# Feature: mission-metric-reader-tools, Property 8: Error envelopes never carry top-level metrics
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(code=_error_codes, details=_details())
def test_error_envelope_never_carries_top_level_metrics(
    code: str,
    details: dict[str, Any],
) -> None:
    """``error_envelope`` always yields ``{"code","details"}`` with no top-level metrics.

    Validates: Requirements 14.1, 14.4, 14.5

    For any error code and arbitrary details payload:

    * the result is a dict whose top-level keys are exactly ``code`` and
      ``details`` — nothing else;
    * there is no top-level ``metrics`` key, so the Observe_Phase merge skips
      the envelope and a ``metric_threshold`` criterion is left
      ``inconclusive`` rather than ``met``/``unmet`` (R14.4); and
    * the supplied code and the full details payload (including a field that
      happens to be named ``metrics``) are preserved verbatim — the details
      provenance survives nested under ``details`` (R14.5).
    """
    envelope = error_envelope(code, **details)

    # Structural shape: exactly ``code`` and ``details``, and crucially no
    # top-level ``metrics`` key for the merge to pick up.
    assert isinstance(envelope, dict)
    assert set(envelope) == {"code", "details"}
    assert "metrics" not in envelope

    # The code passes through unchanged.
    assert envelope["code"] == code

    # All provenance is preserved, nested under ``details`` — even a details
    # field named ``metrics`` stays inside ``details`` and never surfaces at
    # the top level.
    assert isinstance(envelope["details"], dict)
    assert envelope["details"] == details


# ---------------------------------------------------------------------------
# Focused unit tests for the error-envelope builder
# ---------------------------------------------------------------------------


def test_error_envelope_with_no_details_has_empty_details_dict() -> None:
    """An envelope built with no details carries an empty ``details`` dict."""
    envelope = error_envelope(ErrorCode.NO_DATAPOINTS)
    assert envelope == {"code": ErrorCode.NO_DATAPOINTS, "details": {}}
    assert "metrics" not in envelope


def test_error_envelope_preserves_diagnostic_provenance() -> None:
    """Diagnostic provenance is preserved under ``details`` for operator triage."""
    envelope = error_envelope(
        ErrorCode.FILE_NOT_FOUND,
        path="s3://bucket/run/metrics.json",
        format="json",
    )
    assert envelope["code"] == ErrorCode.FILE_NOT_FOUND
    assert envelope["details"] == {
        "path": "s3://bucket/run/metrics.json",
        "format": "json",
    }
    assert "metrics" not in envelope


def test_error_envelope_details_field_named_metrics_stays_nested() -> None:
    """A details field named ``metrics`` is nested, never a top-level key.

    The Observe_Phase merge only reads a *top-level* ``metrics`` object, so a
    provenance field that happens to be named ``metrics`` must live inside
    ``details`` where the merge will not pick it up.
    """
    envelope = error_envelope(ErrorCode.MALFORMED_FILE, metrics="not-a-metrics-map")
    assert "metrics" not in envelope
    assert envelope["details"] == {"metrics": "not-a-metrics-map"}
