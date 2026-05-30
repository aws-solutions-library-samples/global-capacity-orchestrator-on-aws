"""Reduce a sequence of observed values down to one number.

Several metric sources carry history rather than a single point: a training
run logs a loss every step, a CSV column holds one value per row, a log tail
matches a pattern many times. A threshold check, though, wants exactly one
number. This module bridges that gap with a single pure function,
:func:`reduce_sequence`, that collapses a sequence into one finite number
according to a caller-chosen mode.

The supported modes are:

* ``last`` — the most recent number (the last one in the original order).
* ``first`` — the earliest number (the first one in the original order).
* ``min`` — the smallest number.
* ``max`` — the largest number.
* ``mean`` — the arithmetic mean of every number.

Non-numeric entries (booleans, NaN, the infinities, strings, ``None``,
containers) are never counted: every mode reduces only the values that pass
the numeric guard, so ``last`` and ``first`` mean "the most recent / earliest
*number*", skipping anything in between that is not one.

The reducer distinguishes two empty-ish failures on purpose. A sequence that
carried no entries at all fails with :attr:`~.shape.ErrorCode.EMPTY_SEQUENCE`;
a sequence that had entries but none that were numbers fails with
:attr:`~.shape.ErrorCode.NO_NUMERIC_VALUE`. Keeping them apart lets a caller
tell "the source was empty" from "the source had data but none of it was a
number".
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, cast

from . import shape

# The aggregation modes a caller may ask for. Kept as both a typing Literal
# (for signatures) and a runtime frozenset (for membership checks).
AggregationMode = Literal["last", "first", "min", "max", "mean"]
VALID_MODES: frozenset[str] = frozenset({"last", "first", "min", "max", "mean"})


def reduce_sequence(values: Sequence[object], mode: str) -> float:
    """Collapse ``values`` to a single finite number using ``mode``.

    The reduction proceeds in fixed order so the failure reported is always
    the most specific one that applies:

    1. An unrecognized ``mode`` raises
       :class:`~.shape.MetricReaderError` with code
       :attr:`~.shape.ErrorCode.INVALID_AGGREGATION_MODE`.
    2. A sequence with no entries at all raises
       :attr:`~.shape.ErrorCode.EMPTY_SEQUENCE`.
    3. Entries that are not real, finite numbers are dropped (booleans, NaN,
       the infinities, strings, ``None``, containers — anything
       :func:`~.shape.is_numeric_value` rejects).
    4. If nothing survives the filter, raise
       :attr:`~.shape.ErrorCode.NO_NUMERIC_VALUE`.
    5. Otherwise reduce the surviving numbers: ``last`` and ``first`` pick the
       most recent / earliest survivor in the original order; ``min``,
       ``max``, and ``mean`` reduce across all of them.

    The returned value is always a real, finite number.
    """
    if mode not in VALID_MODES:
        raise shape.MetricReaderError(
            shape.ErrorCode.INVALID_AGGREGATION_MODE,
            {"mode": mode, "valid_modes": sorted(VALID_MODES)},
        )

    if len(values) == 0:
        raise shape.MetricReaderError(
            shape.ErrorCode.EMPTY_SEQUENCE,
            {"mode": mode},
        )

    # Keep only real, finite numbers, preserving their original order so
    # ``last`` and ``first`` stay meaningful.
    numeric: list[float] = [cast(float, v) for v in values if shape.is_numeric_value(v)]

    if not numeric:
        raise shape.MetricReaderError(
            shape.ErrorCode.NO_NUMERIC_VALUE,
            {"mode": mode, "entry_count": len(values)},
        )

    if mode == "last":
        return numeric[-1]
    if mode == "first":
        return numeric[0]
    if mode == "min":
        return min(numeric)
    if mode == "max":
        return max(numeric)
    # mode == "mean": membership in VALID_MODES guarantees this is the only
    # remaining possibility.
    return sum(numeric) / len(numeric)
