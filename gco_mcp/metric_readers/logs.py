"""Pull a scalar out of a job's log lines.

A job often prints its progress to stdout — sometimes as a structured JSON
object per line, sometimes as free text a regular expression can pick apart.
This module turns either of those into a list of candidate values that a
later reduction step collapses into one number.

It offers three pure helpers:

* :func:`extract_by_json_key` — parse each line as a JSON object and pull the
  value at a dotted key path.
* :func:`extract_by_regex` — match each line against a compiled pattern and
  pull its first capture group.
* :func:`coerce_scalar` — turn one extracted value (a parsed JSON value or a
  captured string) into a real, finite number, or fail with a structured
  error.

Everything here is pure: no log fetching, no I/O, no environment lookups. The
caller supplies the already-retrieved lines and (for the regex path) an
already-compiled pattern, which keeps these functions trivial to test in
isolation.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, cast

from . import shape


def extract_by_json_key(lines: list[str], key: str) -> list[object]:
    """Collect the value at ``key`` from every line that is a JSON object.

    Each line is parsed independently. A line that is not valid JSON, or
    that parses to something other than a JSON object (a bare number, string,
    list, ``true``/``false``/``null``), is skipped — it contributes nothing
    and never raises.

    ``key`` is treated as a dotted path and resolved segment by segment
    against the parsed object: ``"metrics.loss"`` walks ``obj["metrics"]
    ["loss"]``. A line whose object does not contain the full path resolves
    to nothing and is skipped too. The resolved values are returned in the
    order the lines appeared; coercion to a number happens later.
    """
    segments = key.split(".")
    collected: list[object] = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except ValueError:
            # Not parseable as JSON at all (covers JSONDecodeError, which is
            # a ValueError subclass, and empty/whitespace-only lines).
            continue
        if not isinstance(parsed, dict):
            # Parsed fine, but it is not a JSON object — skip it.
            continue
        value: Any = parsed
        resolved = True
        for segment in segments:
            if isinstance(value, dict) and segment in value:
                value = value[segment]
            else:
                resolved = False
                break
        if resolved:
            collected.append(value)
    return collected


def extract_by_regex(lines: list[str], pattern: re.Pattern[str]) -> list[str]:
    """Collect the first capture group from every line the pattern matches.

    Each line is searched (not anchored) with ``pattern``; on a match the
    substring captured by the pattern's first group is collected. Lines that
    do not match contribute nothing. The pattern is expected to define at
    least one capture group — the caller validates that before compiling —
    and the captures are returned in line order for later coercion.
    """
    collected: list[str] = []
    for line in lines:
        match = pattern.search(line)
        if match is not None:
            collected.append(match.group(1))
    return collected


def _parse_numeric_string(raw: str) -> float:
    """Parse a string into a real, finite number or raise.

    Integers are recognized first so a whole number keeps its integer form;
    anything else falls back to a float parse. A value that does not parse,
    or that parses to NaN or an infinity, raises
    :class:`~.shape.MetricReaderError` with code
    :attr:`~.shape.ErrorCode.NON_NUMERIC_VALUE` and the offending raw string
    in the details.
    """
    text = raw.strip()
    try:
        return int(text)
    except ValueError:
        pass
    try:
        parsed = float(text)
    except ValueError, OverflowError:
        raise shape.MetricReaderError(
            shape.ErrorCode.NON_NUMERIC_VALUE,
            {"raw": raw, "type": "str"},
        ) from None
    if not math.isfinite(parsed):
        raise shape.MetricReaderError(
            shape.ErrorCode.NON_NUMERIC_VALUE,
            {"raw": raw, "type": "str"},
        )
    return parsed


def coerce_scalar(raw: object) -> float:
    """Turn one extracted value into a real, finite number, or fail.

    A value that is already a real, finite number (and not a boolean) is
    returned unchanged. A string is parsed — an integer form when possible,
    otherwise a float — and rejected if it does not represent a finite
    number. Every other input (a boolean, ``None``, NaN, an infinity, a list,
    an object) raises :class:`~.shape.MetricReaderError` with code
    :attr:`~.shape.ErrorCode.NON_NUMERIC_VALUE` and the offending raw value
    recorded in the details.
    """
    if shape.is_numeric_value(raw):
        return cast(float, raw)
    if isinstance(raw, str):
        return _parse_numeric_string(raw)
    raise shape.MetricReaderError(
        shape.ErrorCode.NON_NUMERIC_VALUE,
        {"raw": raw, "type": type(raw).__name__},
    )
