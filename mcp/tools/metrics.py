"""Read-only metric-reader MCP tools.

These tools surface a single training-style scalar (loss, accuracy, throughput,
GPU utilisation, …) in the canonical ``{"metrics": {"<key>": <number>}}`` shape
the Mission Observe_Phase already merges, so a ``metric_threshold`` criterion can
observe progress with zero scripting.

Three readers are registered **default-on** here:

* :func:`metrics_cloudwatch_get` — one CloudWatch ``GetMetricStatistics``
  datapoint, region-scoped.
* :func:`metrics_from_job_logs` — a scalar pulled from the tail of a job's logs
  by JSON key or regex, reusing the existing read-only job-log retrieval path.
* :func:`metrics_from_shared_storage_file` — a named field read from a small
  metrics file on shared storage, reusing the existing read-only ``gco files``
  path and dispatching on a ``format`` parameter.

Every tool returns a plain ``dict`` (so FastMCP passes the top-level ``metrics``
key through verbatim as ``structured_content``) and **never raises**: each wraps
its body in a ``try/except MetricReaderError`` that renders a structured error
envelope, plus a final ``except Exception`` that maps any unexpected
library/API failure to the reader's stable catch-all code. A failed read
therefore merges as ``inconclusive`` and the Mission loop keeps running.

All three are strictly read-only against *remote* AWS / storage / job-log
surfaces. A fourth reader, :func:`metrics_from_local_file`, surfaces a field
from a *local-filesystem* metrics file confined to an allowlisted root; because
reading the local filesystem is a real security concern even for a read-only
tool, it is **flag-gated, default-off** behind ``GCO_ENABLE_LOCAL_METRICS`` and
its decorator only fires when that flag (or the umbrella ``GCO_ENABLE_ALL_TOOLS``)
is enabled.
"""

import asyncio
import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import cli_runner
from audit import audit_logged
from feature_flags import is_enabled
from server import mcp

# The pure ``metric_readers`` package lives under ``mcp/`` alongside this
# ``tools`` package. ``mcp/run_mcp.py`` puts ``mcp/`` on ``sys.path`` at
# runtime; mirror the mission.py path-injection convention so ``import
# metric_readers`` resolves the same way regardless of entrypoint.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from metric_readers import aggregate, cloudwatch, files, localfs, logs  # noqa: E402
from metric_readers.shape import (  # noqa: E402
    ErrorCode,
    MetricReaderError,
    default_metric_key,
    error_envelope,
    metrics_result,
    validate_metric_name,
)

# Job-log tail bounds: a caller-supplied or default tail size is clamped
# into this inclusive range before any log volume is retrieved.
_TAIL_MIN = 1
_TAIL_MAX = 10_000
_TAIL_DEFAULT = 1000

# File-reader default size cap: 10 MiB. The artifact's size is checked
# before its full content is read into memory.
_MAX_BYTES_DEFAULT = 10_485_760


def _resolve_key(output_name: str | None, source_hint: str) -> str:
    """Return a validated metric key from an explicit name or a source hint.

    When ``output_name`` is supplied it must satisfy the single-Dot_Path-segment
    naming constraint; otherwise a deterministic, well-formed key
    is derived from ``source_hint``.
    """
    if output_name:
        return validate_metric_name(output_name)
    return default_metric_key(source_hint)


# =============================================================================
# CloudWatch reader (default-on)
# =============================================================================


@mcp.tool(tags={"safe", "metrics"})
@audit_logged
async def metrics_cloudwatch_get(
    metric_name: str,
    namespace: str,
    region: str,
    dimensions: dict[str, str] | None = None,
    statistic: str = "Average",
    period: int = 300,
    minutes_back: int = 60,
    start_time: str | None = None,
    end_time: str | None = None,
    output_name: str | None = None,
) -> dict[str, Any]:
    """[read-only] Read one CloudWatch datapoint as a canonical metric.

    Issues a single region-scoped, read-only ``GetMetricStatistics`` request and
    returns the most-recent datapoint's statistic value in the canonical
    ``{"metrics": {...}}`` shape consumable by a ``metric_threshold`` criterion.
    The metric key defaults to the CloudWatch metric name when ``output_name``
    is omitted.

    Args:
        metric_name: The CloudWatch metric name.
        namespace: The CloudWatch namespace the metric lives in.
        region: AWS region to scope the read to (supports Multi_Region).
        dimensions: Name/value dimension pairs passed through unchanged.
        statistic: The statistic to request (Average, Sum, Maximum, Minimum,
            SampleCount).
        period: Aggregation period in seconds (default 300).
        minutes_back: Lookback window in minutes when start/end are not given.
        start_time: ISO-8601 window start (used with ``end_time``).
        end_time: ISO-8601 window end (used with ``start_time``).
        output_name: Explicit metric key; defaults to ``metric_name``.

    Returns the Canonical_Metrics_Shape on success, or a Tool_Error_Envelope
    (``metric_name_invalid``, ``no_datapoints``, ``aws_unreachable``) on failure.
    """
    try:
        key = _resolve_key(output_name, metric_name)

        if start_time is not None and end_time is not None:
            start_dt = datetime.fromisoformat(start_time)
            end_dt = datetime.fromisoformat(end_time)
        else:
            end_dt = datetime.now(UTC)
            start_dt = end_dt - timedelta(minutes=minutes_back)

        # The boto3 call blocks, so run it off the event loop.
        value, iso_timestamp = await asyncio.to_thread(
            cloudwatch.get_datapoint,
            metric_name=metric_name,
            namespace=namespace,
            dimensions=dimensions,
            region=region,
            period=period,
            statistic=statistic,
            start_time=start_dt,
            end_time=end_dt,
        )

        return metrics_result(
            key,
            value,
            source=f"cloudwatch:{namespace}/{metric_name}",
            region=region,
            statistic=statistic,
            datapoint_timestamp=iso_timestamp,
        )
    except MetricReaderError as err:
        return error_envelope(err.code, **(err.details or {}))
    except Exception as exc:  # noqa: BLE001 - no exception may escape the tool boundary
        # Catch-all for the CloudWatch reader maps to the unreachable class so
        # the criterion is left inconclusive rather than crashing the loop.
        return error_envelope(
            ErrorCode.AWS_UNREACHABLE,
            kind="client_error",
            region=region,
            message=str(exc),
        )


# =============================================================================
# Job-log reader (default-on)
# =============================================================================


@mcp.tool(tags={"safe", "metrics"})
@audit_logged
async def metrics_from_job_logs(
    job_name: str,
    region: str,
    namespace: str = "gco-jobs",
    json_key: str | None = None,
    regex: str | None = None,
    aggregation: str = "last",
    tail: int = _TAIL_DEFAULT,
    output_name: str | None = None,
) -> dict[str, Any]:
    """[read-only] Extract a scalar from the tail of a job's logs.

    Tails a job's logs through the existing read-only retrieval path and pulls a
    candidate scalar from each line by **either** a JSON key (resolved as a
    dot-path) **or** a regex (first capture group) — exactly one must be set.
    The matched values are coerced to numbers and reduced to one Numeric_Value
    via the aggregation mode (default ``last`` = most recent match). Returns the
    canonical ``{"metrics": {...}}`` shape. The metric key defaults to the last
    segment of ``json_key`` (or ``value`` in regex mode) when ``output_name`` is
    omitted.

    Args:
        job_name: Name of the job whose logs to read.
        region: AWS region where the job ran.
        namespace: Kubernetes namespace (default ``gco-jobs``).
        json_key: JSON dot-path key to resolve per line (mutually exclusive
            with ``regex``).
        regex: Regex whose first capture group is the scalar (mutually
            exclusive with ``json_key``).
        aggregation: One of last, first, min, max, mean (default ``last``).
        tail: Number of log lines to read; clamped to [1, 10000] (default 1000).
        output_name: Explicit metric key.

    Returns the Canonical_Metrics_Shape on success, or a Tool_Error_Envelope
    (``invalid_extraction_mode``, ``invalid_regex``, ``invalid_aggregation_mode``,
    ``log_retrieval_failed``, ``no_match``, ``non_numeric_value``) on failure.
    """
    try:
        has_key = bool(json_key)
        has_regex = bool(regex)
        # Exactly one extraction mode.
        if has_key == has_regex:
            raise MetricReaderError(
                ErrorCode.INVALID_EXTRACTION_MODE,
                {"json_key": json_key, "regex": regex},
            )

        # Fail fast on an unknown aggregation mode before any log retrieval.
        if aggregation not in aggregate.VALID_MODES:
            raise MetricReaderError(
                ErrorCode.INVALID_AGGREGATION_MODE,
                {"mode": aggregation, "valid_modes": sorted(aggregate.VALID_MODES)},
            )

        pattern: re.Pattern[str] | None = None
        if has_regex:
            # Compile and reject uncompilable or zero-capture-group patterns
            # .
            try:
                pattern = re.compile(regex)  # type: ignore[arg-type]
            except re.error as exc:
                raise MetricReaderError(
                    ErrorCode.INVALID_REGEX,
                    {"regex": regex, "reason": str(exc)},
                ) from exc
            if pattern.groups < 1:
                raise MetricReaderError(
                    ErrorCode.INVALID_REGEX,
                    {"regex": regex, "reason": "no capture group"},
                )

        # Resolve the metric key.
        if has_key:
            key = _resolve_key(output_name, json_key.rsplit(".", 1)[-1])  # type: ignore[union-attr]
        else:
            key = _resolve_key(output_name, "value")

        # Clamp the tail size into [1, 10_000].
        clamped = max(_TAIL_MIN, min(int(tail), _TAIL_MAX))

        raw = await asyncio.to_thread(
            cli_runner._run_cli,
            "jobs",
            "logs",
            job_name,
            "-r",
            region,
            "-n",
            namespace,
            "--tail",
            str(clamped),
        )

        # Translate an ``{"error": ...}`` CLI payload into a retrieval failure
        # . Plain log text is not JSON and is used as-is.
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        if isinstance(payload, dict) and "error" in payload:
            message = str(payload["error"])
            kind = "unknown_job" if "not found" in message.lower() else "unreachable"
            raise MetricReaderError(
                ErrorCode.LOG_RETRIEVAL_FAILED,
                {"kind": kind, "job_name": job_name, "message": message},
            )

        lines = raw.splitlines()

        if pattern is not None:
            candidates: list[object] = list(logs.extract_by_regex(lines, pattern))
        else:
            candidates = logs.extract_by_json_key(lines, json_key)  # type: ignore[arg-type]

        # No line matched the key/pattern.
        if not candidates:
            raise MetricReaderError(
                ErrorCode.NO_MATCH,
                {"job_name": job_name, "mode": "json_key" if has_key else "regex"},
            )

        # Coerce each matched value to a Numeric_Value; a value that cannot be
        # parsed surfaces as ``non_numeric_value`` with the offending raw value
        # .
        numeric = [logs.coerce_scalar(candidate) for candidate in candidates]
        value = aggregate.reduce_sequence(numeric, aggregation)

        return metrics_result(
            key,
            value,
            source=f"job_logs:{job_name}",
            region=region,
            aggregation=aggregation,
            match_count=len(candidates),
        )
    except MetricReaderError as err:
        return error_envelope(err.code, **(err.details or {}))
    except Exception as exc:  # noqa: BLE001 - no exception may escape the tool boundary
        return error_envelope(
            ErrorCode.LOG_RETRIEVAL_FAILED,
            kind="unreachable",
            job_name=job_name,
            message=str(exc),
        )


# =============================================================================
# Shared-storage file reader (default-on)
# =============================================================================


def _read_shared_storage(path: str, region: str, max_bytes: int) -> bytes:
    """Read a shared-storage artifact through the read-only ``gco files`` path.

    Reuses ``gco files download`` (the existing read-only storage path) to fetch
    the artifact into a short-lived temporary file, then checks its size before
    its full content is read into memory: an artifact larger than ``max_bytes``
    raises ``FILE_TOO_LARGE`` and **no** content is returned. A
    missing or unreadable artifact raises ``FILE_NOT_FOUND``. The reader
    never writes to, moves, or deletes the source artifact.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        local_path = os.path.join(tmp_dir, "artifact")
        raw = cli_runner._run_cli("files", "download", path, local_path, "-r", region)

        # An explicit CLI error payload means the artifact could not be read.
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        if isinstance(payload, dict) and "error" in payload:
            raise MetricReaderError(
                ErrorCode.FILE_NOT_FOUND,
                {"path": path, "message": str(payload["error"])},
            )

        if not os.path.isfile(local_path):
            raise MetricReaderError(ErrorCode.FILE_NOT_FOUND, {"path": path})

        # Size check before the full content is read into memory.
        size = os.path.getsize(local_path)
        if size > max_bytes:
            raise MetricReaderError(
                ErrorCode.FILE_TOO_LARGE,
                {"path": path, "size": size, "max_bytes": max_bytes},
            )

        with open(local_path, "rb") as handle:
            return handle.read()


@mcp.tool(tags={"safe", "metrics"})
@audit_logged
async def metrics_from_shared_storage_file(
    path: str,
    region: str,
    field: str,
    format: str,
    aggregation: str = "last",
    max_bytes: int = _MAX_BYTES_DEFAULT,
    output_name: str | None = None,
) -> dict[str, Any]:
    """[read-only] Read a named field from a shared-storage metrics file.

    Reads a small metrics file from shared storage (EFS / the cluster shared
    bucket) through the existing read-only ``gco files`` path and surfaces a
    named field as a metric in the canonical ``{"metrics": {...}}`` shape,
    dispatching on ``format``. Sequence-bearing formats (a list, a CSV column, a
    Hugging Face ``log_history``, a JSONL stream, a Parquet column) are reduced
    to one Numeric_Value via the aggregation mode. The metric key defaults to
    ``field`` when ``output_name`` is omitted.

    Args:
        path: Shared-storage path to the metrics file.
        region: AWS region of the storage.
        field: Field/column/key name to read (dot-path for document formats).
        format: One of json, csv, hf_trainer_state, jsonl, yaml, parquet,
            tfevents.
        aggregation: One of last, first, min, max, mean (default ``last``).
        max_bytes: Maximum artifact size in bytes (default 10 MiB); the size is
            checked before the content is read.
        output_name: Explicit metric key; defaults to ``field``.

    Returns the Canonical_Metrics_Shape on success, or a Tool_Error_Envelope
    (``unsupported_format``, ``metric_name_invalid``, ``file_not_found``,
    ``file_too_large``, ``malformed_file``, ``field_not_found``,
    ``non_numeric_value``, ``no_numeric_value``, ``empty_sequence``,
    ``format_dependency_unavailable``) on failure.
    """
    try:
        # Reject an unsupported format up front.
        if format not in files._HANDLERS:
            raise MetricReaderError(
                ErrorCode.UNSUPPORTED_FORMAT,
                {"format": format, "supported": sorted(files._HANDLERS)},
            )

        # Fail fast on an unknown aggregation mode before any storage read.
        if aggregation not in aggregate.VALID_MODES:
            raise MetricReaderError(
                ErrorCode.INVALID_AGGREGATION_MODE,
                {"mode": aggregation, "valid_modes": sorted(aggregate.VALID_MODES)},
            )

        key = _resolve_key(output_name, field)

        # Stat-and-read through the read-only storage path (blocking, so off the
        # event loop). Raises FILE_NOT_FOUND / FILE_TOO_LARGE as appropriate.
        content = await asyncio.to_thread(_read_shared_storage, path, region, max_bytes)

        # Dispatch to the shared per-format handler. Handlers raise
        # MALFORMED_FILE / FIELD_NOT_FOUND / numeric-guard / dependency codes.
        handler = files._HANDLERS[format]
        value = handler(content, field, aggregation)

        return metrics_result(
            key,
            value,
            source=f"file:{path}",
            region=region,
            format=format,
            aggregation=aggregation,
        )
    except MetricReaderError as err:
        return error_envelope(err.code, **(err.details or {}))
    except Exception as exc:  # noqa: BLE001 - no exception may escape the tool boundary
        # Catch-all for the file reader maps to the unreadable class.
        return error_envelope(
            ErrorCode.FILE_NOT_FOUND,
            path=path,
            message=str(exc),
        )


# =============================================================================
# Local-filesystem file reader (flag-gated, default-off)
# =============================================================================
#
# This reader is the deliberate exception to the default-on rule: it reads the
# MCP host's *local* filesystem, a real security concern even for a read-only
# tool, so its decorator is wrapped in ``if is_enabled("GCO_ENABLE_LOCAL_METRICS")``
# (mirroring the module-body gate in ``mcp/tools/mission.py``). With the flag
# unset the decorator never fires and FastMCP never sees the tool. The
# gate is evaluated **only** through ``feature_flags.is_enabled`` — never by
# reading ``os.environ`` for the flag decision — and inherits the
# umbrella ``GCO_ENABLE_ALL_TOOLS`` override.


def _read_local_file(resolved_path: Path, path: str, max_bytes: int) -> bytes:
    """Read a confined local artifact, enforcing the same size cap.

    ``resolved_path`` is the Local_Root-confined path produced by
    :func:`localfs.resolve_within_root`; ``path`` is the caller-supplied path,
    carried through only for error provenance. The artifact's size is checked
    via ``stat`` **before** its full content is read into memory: an artifact
    larger than ``max_bytes`` raises ``FILE_TOO_LARGE`` and **no** content is
    returned. A missing or unreadable artifact raises
    ``FILE_NOT_FOUND``. The reader only reads — it never writes to,
    moves, or deletes the artifact.
    """
    if not resolved_path.is_file():
        raise MetricReaderError(ErrorCode.FILE_NOT_FOUND, {"path": path})

    # Size check before the full content is read into memory.
    size = resolved_path.stat().st_size
    if size > max_bytes:
        raise MetricReaderError(
            ErrorCode.FILE_TOO_LARGE,
            {"path": path, "size": size, "max_bytes": max_bytes},
        )

    with open(resolved_path, "rb") as handle:
        return handle.read()


if is_enabled("GCO_ENABLE_LOCAL_METRICS"):

    @mcp.tool(tags={"safe", "metrics"})
    @audit_logged
    async def metrics_from_local_file(
        path: str,
        field: str,
        format: str,
        aggregation: str = "last",
        max_bytes: int = _MAX_BYTES_DEFAULT,
        output_name: str | None = None,
    ) -> dict[str, Any]:
        """[gated by GCO_ENABLE_LOCAL_METRICS] [read-only] Read a named field from a LOCAL metrics file.

        Reads a small metrics file from a LOCAL filesystem path confined to the
        allowlisted root ``GCO_METRICS_LOCAL_ROOT`` and surfaces a named field
        as a metric in the canonical ``{"metrics": {...}}`` shape, dispatching
        on ``format``. Reuses the same format handlers, aggregation modes, size
        cap, and error model as ``metrics_from_shared_storage_file`` — the only
        difference is that the path is resolved and confined to the allowlisted
        root via realpath containment instead of going through the shared-storage
        path. There is **no** ``region`` parameter: local reads are not
        region-scoped. Sequence-bearing formats (a list, a CSV column, a Hugging
        Face ``log_history``, a JSONL stream, a Parquet column) are reduced to one
        Numeric_Value via the aggregation mode. The metric key defaults to
        ``field`` when ``output_name`` is omitted.

        Args:
            path: Local filesystem path to the metrics file (confined to
                ``GCO_METRICS_LOCAL_ROOT``).
            field: Field/column/key name to read (dot-path for document formats).
            format: One of json, csv, hf_trainer_state, jsonl, yaml, parquet,
                tfevents.
            aggregation: One of last, first, min, max, mean (default ``last``).
            max_bytes: Maximum artifact size in bytes (default 10 MiB); the size
                is checked before the content is read.
            output_name: Explicit metric key; defaults to ``field``.

        Returns the Canonical_Metrics_Shape on success, or a Tool_Error_Envelope
        (``local_root_not_configured``, ``path_traversal_escape``,
        ``symlink_escape``, ``unsupported_format``, ``metric_name_invalid``,
        ``file_not_found``, ``file_too_large``, ``malformed_file``,
        ``field_not_found``, ``non_numeric_value``, ``no_numeric_value``,
        ``empty_sequence``, ``format_dependency_unavailable``) on failure.
        """
        try:
            # Reject an unsupported format up front.
            if format not in files._HANDLERS:
                raise MetricReaderError(
                    ErrorCode.UNSUPPORTED_FORMAT,
                    {"format": format, "supported": sorted(files._HANDLERS)},
                )

            # Fail fast on an unknown aggregation mode before any read.
            if aggregation not in aggregate.VALID_MODES:
                raise MetricReaderError(
                    ErrorCode.INVALID_AGGREGATION_MODE,
                    {"mode": aggregation, "valid_modes": sorted(aggregate.VALID_MODES)},
                )

            key = _resolve_key(output_name, field)

            # Read the Local_Root once at the tool boundary and hand it to the
            # pure confinement helper. The helper never touches os.environ; an
            # unset/empty root raises LOCAL_ROOT_NOT_CONFIGURED, a ``..`` escape
            # raises PATH_TRAVERSAL_ESCAPE, and a symlink escape raises
            # SYMLINK_ESCAPE — in every escape case no file is read and the
            # canonical shape is never returned.
            root = os.environ.get("GCO_METRICS_LOCAL_ROOT", "")
            resolved_path = localfs.resolve_within_root(path, root)

            # Stat-and-read the confined local path (blocking, so off the event
            # loop). Raises FILE_NOT_FOUND / FILE_TOO_LARGE as appropriate.
            content = await asyncio.to_thread(_read_local_file, resolved_path, path, max_bytes)

            # Dispatch to the SAME per-format handler the shared-storage reader
            # uses. Handlers raise MALFORMED_FILE / FIELD_NOT_FOUND /
            # numeric-guard / dependency codes.
            handler = files._HANDLERS[format]
            value = handler(content, field, aggregation)

            return metrics_result(
                key,
                value,
                source=f"local_file:{path}",
                format=format,
                aggregation=aggregation,
            )
        except MetricReaderError as err:
            return error_envelope(err.code, **(err.details or {}))
        except Exception as exc:  # noqa: BLE001 - no exception may escape the tool boundary
            # Catch-all for the local-file reader maps to the unreadable class
            # so the criterion is left inconclusive rather than crashing
            # the loop.
            return error_envelope(
                ErrorCode.FILE_NOT_FOUND,
                path=path,
                message=str(exc),
            )
