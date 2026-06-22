"""Tests for the sequence reducer that collapses history to one number.

Several metric sources carry more than one value: a training run logs a loss
every step, a CSV column holds one number per row, a log tail matches a
pattern many times. The reducer turns such a sequence into a single number
according to a caller-chosen mode (``last``, ``first``, ``min``, ``max``,
``mean``), and it does so over only the entries that are real, finite numbers
— booleans, NaN, the infinities, strings, and ``None`` are skipped before any
reduction happens.

The property test below pins that contract down. It builds sequences that mix
genuine numbers with a variety of non-numeric junk, then checks that every
mode produces exactly the number a reference reduction computes over the
number-only subsequence. Because the junk is dropped first, ``last`` and
``first`` mean "the most recent / earliest *number*", and ``min``, ``max``,
and ``mean`` range over the surviving numbers alone.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Sequence
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ``gco_mcp/run_mcp.py`` puts ``gco_mcp/`` on ``sys.path`` at runtime; mirror that
# here so the pure ``metric_readers`` package imports the same way it does
# in production, matching the convention used by the sibling tests.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from metric_readers.aggregate import VALID_MODES, reduce_sequence  # noqa: E402
from metric_readers.shape import (  # noqa: E402
    ErrorCode,
    MetricReaderError,
    is_numeric_value,
)

# Every mode the reducer accepts. The test exercises all of them on each
# generated sequence.
_MODES = ("last", "first", "min", "max", "mean")

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Entries that the numeric guard accepts: ints (never bools, since
# ``st.integers`` does not emit them) and finite floats (never NaN or +/-inf).
_numeric_entries = st.one_of(
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
)

# Entries that the numeric guard rejects and the reducer must ignore:
# booleans, NaN, the infinities, strings, and ``None``.
_non_numeric_entries = st.one_of(
    st.booleans(),
    st.just(float("nan")),
    st.sampled_from([float("inf"), float("-inf")]),
    st.text(max_size=8),
    st.none(),
)


@st.composite
def _mixed_sequences_with_at_least_one_number(draw: st.DrawFn) -> list[object]:
    """Draw a sequence mixing numbers with non-numeric junk, in random order.

    At least one real number is guaranteed so the reduction always succeeds,
    and the non-numeric entries are shuffled in among the numbers so their
    position relative to the numbers is arbitrary.
    """
    numbers = draw(st.lists(_numeric_entries, min_size=1, max_size=20))
    junk = draw(st.lists(_non_numeric_entries, max_size=10))
    return draw(st.permutations(numbers + junk))


# ---------------------------------------------------------------------------
# Reference reduction
# ---------------------------------------------------------------------------


def _reference_reduce(values: Sequence[object], mode: str) -> float:
    """Reduce ``values`` the obvious way, over the number-only subsequence.

    Mirrors the reducer's own definition: filter to real, finite numbers
    using the same guard, preserve their original order, then apply the mode.
    This is intentionally a straightforward re-derivation so a divergence
    points at the reducer, not at clever test logic.
    """
    numbers = [v for v in values if is_numeric_value(v)]
    if mode == "last":
        return numbers[-1]
    if mode == "first":
        return numbers[0]
    if mode == "min":
        return min(numbers)
    if mode == "max":
        return max(numbers)
    # mode == "mean"
    return sum(numbers) / len(numbers)


# ---------------------------------------------------------------------------
# Property: every mode reduces over the number-only subsequence
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(values=_mixed_sequences_with_at_least_one_number())
def test_reduce_sequence_matches_reference_over_numbers_only(
    values: list[object],
) -> None:
    """Each mode equals a reference reduction over the number-only entries.

    For an arbitrary sequence containing at least one number plus arbitrary
    non-numeric junk, every mode (``last``, ``first``, ``min``, ``max``,
    ``mean``) returns exactly what the same reduction yields once the junk is
    dropped — confirming non-numeric entries are always ignored.
    """
    for mode in _MODES:
        expected = _reference_reduce(values, mode)
        actual = reduce_sequence(values, mode)
        # The reducer and the reference perform identical arithmetic in the
        # same order, so exact equality holds (NaN never survives filtering,
        # so there is no NaN-inequality pitfall here).
        assert actual == expected, (
            f"mode={mode!r} values={values!r} expected={expected!r} actual={actual!r}"
        )
        # A reduction over finite numbers stays a real, finite number unless
        # a sum genuinely overflows; guard against silently passing on a NaN.
        assert not (isinstance(actual, float) and math.isnan(actual))


# ===========================================================================
# The reducer's three "nothing to reduce" failures
# ===========================================================================
#
# The reducer rejects three distinct degenerate inputs, and it does so in a
# fixed precedence order (see ``reduce_sequence``): an unrecognized mode is
# refused *before* the sequence is even looked at, an empty sequence is
# refused *before* numeric filtering, and a sequence that had entries but no
# numbers is refused *after* filtering. Each failure carries its own stable
# code, and the codes are distinct so a caller can tell the three apart:
#
#   * unknown mode                -> INVALID_AGGREGATION_MODE
#   * empty sequence              -> EMPTY_SEQUENCE
#   * non-empty, all non-numeric  -> NO_NUMERIC_VALUE
#
# The properties below generate inputs in each category (and combinations
# that exercise the precedence) and assert exactly one code is raised, never
# a returned value, confirming the three outcomes are mutually exclusive.

import pytest  # noqa: E402

# Mode strings the reducer must reject: any text that is not one of the five
# valid modes. Near-misses like "LAST", "median", and "" are all in range.
_invalid_modes = st.text(max_size=12).filter(lambda s: s not in VALID_MODES)

# Any single entry, numeric or not — used to build arbitrary sequences for
# the precedence checks.
_arbitrary_entries = st.one_of(_numeric_entries, _non_numeric_entries)

# Arbitrary sequences of any length, including empty.
_any_sequences = st.lists(_arbitrary_entries, max_size=15)

# Non-empty sequences whose every entry the numeric guard rejects.
_nonempty_non_numeric_sequences = st.lists(_non_numeric_entries, min_size=1, max_size=15)


def _assert_raises_code(values: Sequence[object], mode: str, code: str) -> None:
    """Assert ``reduce_sequence`` raises ``MetricReaderError`` with ``code``.

    Also confirms the reducer never produced a value for a degenerate input:
    ``pytest.raises`` would fail the test if the call returned instead.
    """
    with pytest.raises(MetricReaderError) as excinfo:
        reduce_sequence(values, mode)
    assert excinfo.value.code == code, (
        f"values={values!r} mode={mode!r} expected code={code!r} got code={excinfo.value.code!r}"
    )


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(values=_any_sequences, mode=_invalid_modes)
def test_unknown_mode_is_rejected_before_anything_else(values: list[object], mode: str) -> None:
    """An unrecognized mode always yields ``INVALID_AGGREGATION_MODE``.

    The mode is validated first, so the verdict does not depend on the
    sequence: an unknown mode wins over an empty sequence and over an
    all-non-numeric sequence alike (by reducer precedence).
    """
    _assert_raises_code(values, mode, ErrorCode.INVALID_AGGREGATION_MODE)


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(mode=st.sampled_from(_MODES))
def test_empty_sequence_under_a_valid_mode_yields_empty_sequence(mode: str) -> None:
    """An empty sequence under any valid mode yields ``EMPTY_SEQUENCE``.

    With a recognized mode the next gate is the empty check, which fires
    before numeric filtering, so an empty input is reported as an empty
    sequence rather than as "no numeric value".
    """
    _assert_raises_code([], mode, ErrorCode.EMPTY_SEQUENCE)


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(values=_nonempty_non_numeric_sequences, mode=st.sampled_from(_MODES))
def test_all_non_numeric_under_a_valid_mode_yields_no_numeric_value(
    values: list[object], mode: str
) -> None:
    """A non-empty all-non-numeric sequence yields ``NO_NUMERIC_VALUE``.

    The sequence has entries (so it is not empty) but none survive the
    numeric guard, so the reducer reports the distinct "had data, none of it
    numeric" code rather than the empty-sequence code.
    """
    # Guard the generator's intent: every entry really is non-numeric.
    assert all(not is_numeric_value(v) for v in values)
    _assert_raises_code(values, mode, ErrorCode.NO_NUMERIC_VALUE)


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(data=st.data())
def test_three_failures_are_mutually_exclusive_with_distinct_codes(
    data: st.DataObject,
) -> None:
    """The three degenerate inputs map to three distinct, exclusive codes.

    A single draw picks one of the three failure categories, builds a
    matching ``(values, mode)`` input, and asserts the reducer raises exactly
    the code that category owns. Across categories the three codes are
    pairwise distinct, so the outcomes cannot be confused.
    """
    # The three codes are distinct constants — the contract that makes the
    # outcomes distinguishable in the first place.
    codes = {
        ErrorCode.INVALID_AGGREGATION_MODE,
        ErrorCode.EMPTY_SEQUENCE,
        ErrorCode.NO_NUMERIC_VALUE,
    }
    assert len(codes) == 3

    category = data.draw(st.sampled_from(("invalid_mode", "empty", "all_non_numeric")))
    if category == "invalid_mode":
        values = data.draw(_any_sequences)
        mode = data.draw(_invalid_modes)
        _assert_raises_code(values, mode, ErrorCode.INVALID_AGGREGATION_MODE)
    elif category == "empty":
        mode = data.draw(st.sampled_from(_MODES))
        _assert_raises_code([], mode, ErrorCode.EMPTY_SEQUENCE)
    else:  # all_non_numeric
        values = data.draw(_nonempty_non_numeric_sequences)
        mode = data.draw(st.sampled_from(_MODES))
        _assert_raises_code(values, mode, ErrorCode.NO_NUMERIC_VALUE)
