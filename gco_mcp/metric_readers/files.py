"""Read a named field out of a metrics file and reduce it to one number.

A job can persist its metrics in many shapes: a hand-written JSON or YAML
document, a CSV table, a Hugging Face ``Trainer`` state file, a stream of
JSON-per-line step records, or a columnar Parquet file. This module turns any
of those into a single finite number a threshold check can read.

The work is split into two layers:

* A small set of pure, per-format **handlers**. Each handler takes the raw file
  bytes, the caller's field name, and an aggregation mode, and returns one
  finite number — or raises :class:`~.shape.MetricReaderError` with a stable
  code describing why it could not. Handlers do no I/O of their own: the bytes
  are handed in already, so the same handler serves both the shared-storage
  reader and the local-filesystem reader.
* A :data:`_HANDLERS` dispatch map from a format name to its handler. The tool
  wrapper looks a format up here; a format with no entry is reported as
  unsupported rather than crashing.

Field resolution differs by format. The document formats (``json``, ``yaml``)
resolve the field as a dot-path walked segment-by-segment through nested
objects. The tabular and record formats (``csv``, ``jsonl``, the Hugging Face
``log_history``) treat the field as a flat column or key name and gather one
value per row, line, or entry. A single resolved number is returned as-is; a
gathered sequence is collapsed with the chosen aggregation mode, which ignores
any non-numeric entries along the way.

Every parsing or decoding failure becomes a malformed-file error; a field that
cannot be located becomes a field-not-found error; a value that is present but
is not a real number becomes a non-numeric error. Nothing escapes as an
unhandled exception.
"""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from collections.abc import Callable
from typing import Literal, cast

import yaml

from . import shape
from .aggregate import reduce_sequence

# The full set of file formats the reader understands. The document and record
# formats are handled here; the columnar and TensorBoard formats are dispatched
# through the same map and carry their own lazy-import handlers.
ReaderFormat = Literal[
    "json",
    "csv",
    "hf_trainer_state",
    "jsonl",
    "yaml",
    "parquet",
    "tfevents",
]


def _decode(content: bytes, fmt: str) -> str:
    """Decode raw file bytes as UTF-8 text, or report a malformed file.

    The text-oriented formats (``csv``, ``jsonl``) need to work over decoded
    lines. Bytes that are not valid UTF-8 are surfaced as a malformed-file
    error tagged with the format, rather than letting the decode error escape.
    """
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise shape.MetricReaderError(
            shape.ErrorCode.MALFORMED_FILE,
            {"format": fmt, "reason": "decode_error"},
        ) from exc


def _maybe_number(raw: object) -> object:
    """Best-effort coerce a raw cell to a number, leaving non-numbers untouched.

    Cells read from a CSV arrive as strings. A string that parses cleanly as an
    integer or a float is returned as that number; anything else (an empty
    cell, a label, ``None`` from a short row) is returned unchanged so the
    downstream numeric filter can drop it. Non-string inputs pass straight
    through.
    """
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return raw


def _describe_value(value: object) -> dict[str, object]:
    """Render a non-numeric value into JSON-safe error-detail fields.

    Returns the offending value (kept verbatim when it is a simple scalar,
    otherwise its ``repr``) alongside its type name, so an operator can see
    both what was found and what kind of thing it was.
    """
    if value is None or isinstance(value, str | int | float | bool):
        shown: object = value
    else:
        shown = repr(value)
    return {"value": shown, "value_type": type(value).__name__}


def _resolve_dot_path(obj: object, field: str) -> object:
    """Walk a dot-separated path through nested objects and return the leaf.

    Each segment of ``field`` indexes one level deeper into a mapping. A
    segment that is missing, or a level that is not a mapping, means the field
    is absent and raises a field-not-found error.
    """
    current: object = obj
    for segment in field.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        else:
            raise shape.MetricReaderError(
                shape.ErrorCode.FIELD_NOT_FOUND,
                {"field": field},
            )
    return current


def _reduce_resolved(value: object, field: str, mode: str) -> float:
    """Turn a resolved field value into one number.

    A value that is already a real, finite number is returned directly. A list
    is collapsed with the aggregation mode. Anything else is present but not a
    number, which is a non-numeric error carrying the offending value.
    """
    if shape.is_numeric_value(value):
        return cast(float, value)
    if isinstance(value, list):
        return reduce_sequence(value, mode)
    raise shape.MetricReaderError(
        shape.ErrorCode.NON_NUMERIC_VALUE,
        {"field": field, **_describe_value(value)},
    )


def _handle_json(content: bytes, field: str, mode: str) -> float:
    """Read a field from a plain JSON document.

    The document is parsed, the field resolved by dot-path, and the leaf either
    returned directly (a single number) or reduced (a list). Bytes that do not
    parse as JSON are a malformed-file error.
    """
    try:
        parsed: object = json.loads(content)
    except (ValueError, UnicodeDecodeError) as exc:
        raise shape.MetricReaderError(
            shape.ErrorCode.MALFORMED_FILE,
            {"format": "json"},
        ) from exc
    return _reduce_resolved(_resolve_dot_path(parsed, field), field, mode)


def _handle_yaml(content: bytes, field: str, mode: str) -> float:
    """Read a field from a YAML document using the safe loader.

    Mirrors the JSON handler: dot-path resolution, then a single number
    returned directly or a list reduced. A document the safe loader rejects is
    a malformed-file error.
    """
    try:
        parsed: object = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise shape.MetricReaderError(
            shape.ErrorCode.MALFORMED_FILE,
            {"format": "yaml"},
        ) from exc
    return _reduce_resolved(_resolve_dot_path(parsed, field), field, mode)


def _handle_csv(content: bytes, field: str, mode: str) -> float:
    """Read one column from a CSV table and reduce it.

    The first row is the header; ``field`` names one of its columns. Every data
    row's cell in that column is coerced toward a number and the resulting
    sequence is reduced with the aggregation mode (non-numeric cells are
    ignored). A column name absent from the header is a field-not-found error.
    """
    text = _decode(content, "csv")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames
    if not fieldnames or field not in fieldnames:
        raise shape.MetricReaderError(
            shape.ErrorCode.FIELD_NOT_FOUND,
            {"field": field},
        )
    candidates: list[object] = [_maybe_number(row.get(field)) for row in reader]
    return reduce_sequence(candidates, mode)


def _handle_jsonl(content: bytes, field: str, mode: str) -> float:
    """Read a field across a stream of one-JSON-object-per-line records.

    Each non-blank line is parsed on its own; a line that is not valid JSON is
    skipped rather than failing the whole read. The named key is gathered from
    every object that carries it, and the gathered sequence is reduced. When no
    line yields a usable number — whether the key was never present or never
    numeric — the result is a no-numeric-value error.
    """
    text = _decode(content, "jsonl")
    candidates: list[object] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            obj: object = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(obj, dict) and field in obj:
            candidates.append(obj[field])
    try:
        return reduce_sequence(candidates, mode)
    except shape.MetricReaderError as exc:
        # A stream that carried the field nowhere collapses to the same
        # "no usable number" outcome as one where every value was non-numeric.
        if exc.code == shape.ErrorCode.EMPTY_SEQUENCE:
            raise shape.MetricReaderError(
                shape.ErrorCode.NO_NUMERIC_VALUE,
                {"field": field},
            ) from exc
        raise


def _handle_hf(content: bytes, field: str, mode: str) -> float:
    """Read a scalar from a Hugging Face ``Trainer`` state file.

    When the document carries a ``log_history`` list, the named field is
    gathered from every per-step entry that includes it and the sequence is
    reduced — this is the path for per-step scalars such as ``loss`` or
    ``eval_loss``. When ``log_history`` is absent (or the field never appears
    in it), the field is looked up as a top-level key and returned directly, as
    in an ``all_results.json``. A field found in neither place is a
    field-not-found error.
    """
    try:
        parsed: object = json.loads(content)
    except (ValueError, UnicodeDecodeError) as exc:
        raise shape.MetricReaderError(
            shape.ErrorCode.MALFORMED_FILE,
            {"format": "hf_trainer_state"},
        ) from exc

    log_history: object = parsed.get("log_history") if isinstance(parsed, dict) else None
    if isinstance(log_history, list):
        candidates: list[object] = [
            entry[field] for entry in log_history if isinstance(entry, dict) and field in entry
        ]
        if candidates:
            return reduce_sequence(candidates, mode)

    if isinstance(parsed, dict) and field in parsed:
        value = parsed[field]
        if shape.is_numeric_value(value):
            return cast(float, value)
        raise shape.MetricReaderError(
            shape.ErrorCode.NON_NUMERIC_VALUE,
            {"field": field, **_describe_value(value)},
        )

    raise shape.MetricReaderError(
        shape.ErrorCode.FIELD_NOT_FOUND,
        {"field": field},
    )


def _handle_parquet(content: bytes, field: str, mode: str) -> float:
    """Reduce one column of a columnar (Parquet) file to a single number.

    The columnar libraries (``pandas`` + ``pyarrow``) ship only in the
    analytics extra, so they are imported lazily inside the handler. When
    either is missing the failure is reported as a
    :attr:`~.shape.ErrorCode.FORMAT_DEPENDENCY_UNAVAILABLE` envelope rather
    than letting the ``ImportError`` escape.

    Once loaded, the file is parsed into a frame; bytes that do not parse as
    Parquet are a malformed-file error. The named column is gathered as native
    Python values — ``Series.tolist()`` converts NumPy scalars to ``int`` /
    ``float`` so the numeric guard recognises them — and reduced with the
    aggregation mode. A column absent from the schema is a field-not-found
    error. An aggregated result of exactly ``0`` is a valid number and is
    returned as-is; the reducer never treats it as "missing".
    """
    try:
        import pandas as pd
        import pyarrow  # noqa: F401  # read_parquet's engine; imported so a missing wheel surfaces here
    except ImportError as exc:
        raise shape.MetricReaderError(
            shape.ErrorCode.FORMAT_DEPENDENCY_UNAVAILABLE,
            {"format": "parquet", "dependency": "pandas+pyarrow"},
        ) from exc

    try:
        frame = pd.read_parquet(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001 - any pandas/pyarrow read failure is a malformed file
        raise shape.MetricReaderError(
            shape.ErrorCode.MALFORMED_FILE,
            {"format": "parquet"},
        ) from exc

    if field not in frame.columns:
        raise shape.MetricReaderError(
            shape.ErrorCode.FIELD_NOT_FOUND,
            {"field": field},
        )

    column_values: list[object] = frame[field].tolist()
    return reduce_sequence(column_values, mode)


def _handle_tfevents(content: bytes, field: str, mode: str) -> float:
    """Reduce the scalar sequence for a TensorBoard tag to a single number.

    TensorBoard ``tfevents`` reading is the optional/stretch format: its parser
    (``tbparse``, which pulls in ``tensorboard``) is not a baseline dependency,
    so it is imported lazily inside the handler. When the parser is not
    installed the failure is reported as a
    :attr:`~.shape.ErrorCode.FORMAT_DEPENDENCY_UNAVAILABLE` envelope rather than
    an import crash, so the baseline reader keeps working without the
    heavyweight dependency.

    When the parser is present, the handed-in bytes are staged into a
    short-lived temporary event file (``tbparse`` reads from a path, not a
    buffer), the scalar rows for the requested ``field`` tag are gathered, and
    the sequence is reduced with the aggregation mode — the caller defaults this
    to ``last``, i.e. the latest scalar for the tag. A tag that carries
    no scalar rows is a field-not-found error, and bytes that do not parse as an
    event file are a malformed-file error.
    """
    try:
        from tbparse import SummaryReader
    except ImportError as exc:
        raise shape.MetricReaderError(
            shape.ErrorCode.FORMAT_DEPENDENCY_UNAVAILABLE,
            {"format": "tfevents", "dependency": "tbparse/tensorboard"},
        ) from exc

    with tempfile.TemporaryDirectory() as tmp_dir:
        event_path = os.path.join(tmp_dir, "events.out.tfevents")
        with open(event_path, "wb") as handle:
            handle.write(content)
        try:
            frame = SummaryReader(event_path, pivot=False).scalars
        except Exception as exc:  # noqa: BLE001 - any tbparse read failure is a malformed file
            raise shape.MetricReaderError(
                shape.ErrorCode.MALFORMED_FILE,
                {"format": "tfevents"},
            ) from exc

    if frame is None or getattr(frame, "empty", True) or "tag" not in frame.columns:
        raise shape.MetricReaderError(
            shape.ErrorCode.FIELD_NOT_FOUND,
            {"field": field},
        )
    matched = frame[frame["tag"] == field]
    if matched.empty:
        raise shape.MetricReaderError(
            shape.ErrorCode.FIELD_NOT_FOUND,
            {"field": field},
        )
    tag_values: list[object] = matched["value"].tolist()
    return reduce_sequence(tag_values, mode)


# Format name -> handler. Each handler shares the
# ``(content_bytes, field, mode) -> float`` contract and either returns one
# finite number or raises a MetricReaderError with a stable code. A format that
# is not a key here is treated as unsupported by the calling tool.
#
# The columnar (``parquet``) and TensorBoard (``tfevents``) handlers are
# registered into this same map; their handlers carry lazy third-party imports
# and are added alongside these baseline entries.
_HANDLERS: dict[str, Callable[..., float]] = {
    "json": _handle_json,
    "csv": _handle_csv,
    "hf_trainer_state": _handle_hf,
    "jsonl": _handle_jsonl,
    "yaml": _handle_yaml,
    "parquet": _handle_parquet,
    "tfevents": _handle_tfevents,
}
