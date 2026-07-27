"""Cost Monitor service core: OpenCost allocation reports as Parquet in S3.

The cost-monitor Deployment (one per regional cluster) runs two surfaces on
top of this module:

1. A scheduled reporter (driven by :mod:`gco.services.cost_api`) that writes
   one Parquet allocation report per interval to the central cost report
   bucket under ``reports/region=<region>/date=<YYYY-MM-DD>/`` — the layout
   the monitoring stack's Glue table reads with partition projection.
2. An internal HTTP API the manifest processor proxies as ``/api/v1/cost/*``,
   serving ad-hoc report generation (written under ``adhoc/`` so overlapping
   windows never double-count in Athena) plus report listing and service
   status.

Report object keys for scheduled windows are **deterministic** — derived only
from the window bounds — so a rollout overlap or retry can never produce two
objects for one window: concurrent writers converge on the same key and the
last write wins with identical content.
"""

from __future__ import annotations

import io
import logging
import math
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import httpx
from botocore.config import Config

logger = logging.getLogger(__name__)

#: Normalized report row fields, in Parquet column order. This is the
#: write-side contract of the monitoring stack's Glue table
#: (``gco/stacks/monitoring_stack.py::_create_cost_analytics``) — the two
#: must stay in lockstep or Athena reads misaligned columns.
ALLOCATION_REPORT_FIELDS: tuple[str, ...] = (
    "window_start",
    "window_end",
    "cluster",
    "namespace",
    "cpu_core_hours",
    "cpu_cost",
    "ram_gib_hours",
    "ram_cost",
    "gpu_hours",
    "gpu_cost",
    "pv_cost",
    "network_cost",
    "load_balancer_cost",
    "shared_cost",
    "external_cost",
    "total_cost",
    "total_efficiency",
)

_GIB = 1024.0**3

#: Prefixes must mirror gco/stacks/constants.py (the service image does not
#: ship the CDK stacks package, so the values are duplicated deliberately —
#: a synth-side test asserts the two stay in lockstep).
SCHEDULED_PREFIX = "reports"
ADHOC_PREFIX = "adhoc"

_MIN_WINDOW_MINUTES = 5
_MAX_WINDOW_HOURS = 7 * 24


class OpenCostUnavailableError(RuntimeError):
    """Raised when the OpenCost API cannot be reached or answers abnormally."""


class ReportWriteError(RuntimeError):
    """Raised when a generated report cannot be persisted to S3."""


def _compact_ts(moment: datetime) -> str:
    """Render a UTC timestamp as a compact S3-key-safe token."""
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _as_float(value: Any) -> float:
    """Coerce OpenCost numeric fields defensively; absent/bad values are 0.

    Non-finite values (NaN and ±infinity — ``json.loads`` accepts both) are
    coerced to 0 too: one poisoned row would otherwise contaminate every
    Athena aggregate over the table.
    """
    try:
        result = float(value)
    except TypeError, ValueError:
        return 0.0
    return result if math.isfinite(result) else 0.0


@dataclass(frozen=True)
class ReportResult:
    """Outcome of one generated allocation report."""

    s3_key: str
    row_count: int
    total_cost: float
    window_start: str
    window_end: str
    rows: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """Operator-facing summary without the full row payload."""
        return {
            "s3_key": self.s3_key,
            "row_count": self.row_count,
            "total_cost": round(self.total_cost, 6),
            "window_start": self.window_start,
            "window_end": self.window_end,
        }


class OpenCostClient:
    """Minimal HTTP client for the in-cluster OpenCost allocation API."""

    def __init__(self, base_url: str, timeout_seconds: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def is_healthy(self) -> bool:
        """Return whether OpenCost answers its /healthz probe."""
        try:
            response = httpx.get(
                f"{self.base_url}/healthz",
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    def get_allocation(
        self,
        window_start: datetime,
        window_end: datetime,
        *,
        aggregate: str = "namespace",
    ) -> dict[str, dict[str, Any]]:
        """Fetch one accumulated allocation set for ``[window_start, window_end)``.

        Returns a mapping of allocation name (namespace, by default) to the
        raw OpenCost allocation object. Raises
        :class:`OpenCostUnavailableError` on transport errors, non-200
        responses, or a malformed body — the caller decides whether that
        fails a scheduled pass or an API request.
        """
        window = (
            f"{window_start.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')},"
            f"{window_end.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}"
        )
        try:
            response = httpx.get(
                f"{self.base_url}/allocation/compute",
                params={
                    "window": window,
                    "aggregate": aggregate,
                    "accumulate": "true",
                },
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise OpenCostUnavailableError(f"OpenCost request failed: {exc}") from exc
        if response.status_code != 200:
            raise OpenCostUnavailableError(
                f"OpenCost allocation query returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise OpenCostUnavailableError("OpenCost returned a non-JSON body") from exc
        data = payload.get("data")
        if not isinstance(data, list):
            raise OpenCostUnavailableError("OpenCost allocation response omitted data")
        merged: dict[str, dict[str, Any]] = {}
        for allocation_set in data:
            if not isinstance(allocation_set, dict):
                continue
            for name, allocation in allocation_set.items():
                if isinstance(allocation, dict):
                    merged[str(name)] = allocation
        return merged


def allocations_to_rows(
    allocations: dict[str, dict[str, Any]],
    *,
    cluster: str,
    window_start: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    """Normalize raw OpenCost allocations into the stable report row schema.

    One row per allocation name (namespace). The ``__idle__`` and
    ``__unallocated__`` synthetic allocations OpenCost emits are kept —
    idle cost is exactly the visibility a cost report exists to provide —
    but rows are sorted by descending total cost for human-readable output.
    """
    start_iso = window_start.astimezone(UTC).isoformat()
    end_iso = window_end.astimezone(UTC).isoformat()
    rows: list[dict[str, Any]] = []
    for name, allocation in allocations.items():
        rows.append(
            {
                "window_start": start_iso,
                "window_end": end_iso,
                "cluster": cluster,
                "namespace": name,
                "cpu_core_hours": _as_float(allocation.get("cpuCoreHours")),
                "cpu_cost": _as_float(allocation.get("cpuCost")),
                "ram_gib_hours": _as_float(allocation.get("ramByteHours")) / _GIB,
                "ram_cost": _as_float(allocation.get("ramCost")),
                "gpu_hours": _as_float(allocation.get("gpuHours")),
                "gpu_cost": _as_float(allocation.get("gpuCost")),
                "pv_cost": _as_float(allocation.get("pvCost")),
                "network_cost": _as_float(allocation.get("networkCost")),
                "load_balancer_cost": _as_float(allocation.get("loadBalancerCost")),
                "shared_cost": _as_float(allocation.get("sharedCost")),
                "external_cost": _as_float(allocation.get("externalCost")),
                "total_cost": _as_float(allocation.get("totalCost")),
                "total_efficiency": _as_float(allocation.get("totalEfficiency")),
            }
        )
    rows.sort(key=lambda row: row["total_cost"], reverse=True)
    return rows


def rows_to_parquet_bytes(rows: list[dict[str, Any]]) -> bytes:
    """Serialize normalized report rows to a Parquet byte payload.

    ``pyarrow`` is imported lazily so environments that never write reports
    (unit tests exercising only transformations, or a future reader-only
    consumer) do not need the dependency at import time. The window bound
    columns are stored as real timestamps so the Glue ``timestamp`` columns
    read them natively.
    """
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - image always ships pyarrow
        raise ReportWriteError(
            "pyarrow is required to write cost reports; install the "
            "image-cost-monitor dependency group"
        ) from exc

    schema = pa.schema(
        [
            ("window_start", pa.timestamp("ms", tz="UTC")),
            ("window_end", pa.timestamp("ms", tz="UTC")),
            ("cluster", pa.string()),
            ("namespace", pa.string()),
            *[
                (name, pa.float64())
                for name in ALLOCATION_REPORT_FIELDS
                if name not in {"window_start", "window_end", "cluster", "namespace"}
            ],
        ]
    )
    columns: dict[str, list[Any]] = {name: [] for name in ALLOCATION_REPORT_FIELDS}
    for row in rows:
        for name in ALLOCATION_REPORT_FIELDS:
            value: Any = row.get(name)
            if name in {"window_start", "window_end"}:
                value = datetime.fromisoformat(str(value))
            columns[name].append(value)
    table = pa.Table.from_pydict(columns, schema=schema)
    sink = io.BytesIO()
    # pyarrow ships no type stubs; route the call through an Any-typed name so
    # environments with pyarrow installed (tests) and without it (the mypy
    # strict CI job) type-check identically — no conditional type: ignore.
    write_table: Any = pq.write_table
    write_table(table, sink)
    return sink.getvalue()


def scheduled_report_key(region: str, window_start: datetime, window_end: datetime) -> str:
    """Deterministic S3 key for one scheduled report window."""
    date_partition = window_start.astimezone(UTC).strftime("%Y-%m-%d")
    return (
        f"{SCHEDULED_PREFIX}/region={region}/date={date_partition}/"
        f"allocation-{_compact_ts(window_start)}-{_compact_ts(window_end)}.parquet"
    )


def adhoc_report_key(region: str, window_start: datetime, window_end: datetime) -> str:
    """Unique S3 key for one ad-hoc report (kept out of the Athena table)."""
    date_partition = datetime.now(UTC).strftime("%Y-%m-%d")
    return (
        f"{ADHOC_PREFIX}/region={region}/date={date_partition}/"
        f"allocation-{_compact_ts(window_start)}-{_compact_ts(window_end)}"
        f"-{uuid.uuid4().hex[:8]}.parquet"
    )


def aligned_window(now: datetime, interval_minutes: int) -> tuple[datetime, datetime]:
    """Return the most recent *completed* interval-aligned window.

    For ``interval_minutes=60`` at 10:25 this yields ``[09:00, 10:00)`` —
    aligning to interval boundaries makes the scheduled key deterministic
    across restarts and replicas, which is what makes report writes
    idempotent.
    """
    interval = timedelta(minutes=interval_minutes)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = now.astimezone(UTC) - epoch
    completed_intervals = int(elapsed / interval)
    window_end = epoch + interval * completed_intervals
    return window_end - interval, window_end


class CostMonitor:
    """Generates OpenCost allocation reports and persists them to S3."""

    def __init__(
        self,
        *,
        region: str,
        cluster: str,
        bucket: str,
        opencost: OpenCostClient,
        report_interval_minutes: int = 60,
        s3_client: Any | None = None,
    ) -> None:
        self.region = region
        self.cluster = cluster
        self.bucket = bucket
        self.opencost = opencost
        self.report_interval_minutes = min(max(int(report_interval_minutes), 5), 1_440)
        self._s3 = s3_client or boto3.client(
            "s3",
            config=Config(
                connect_timeout=5,
                read_timeout=60,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        self.last_scheduled_report: dict[str, Any] | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------------
    # Report generation
    # ------------------------------------------------------------------

    def generate_report(
        self,
        window_start: datetime,
        window_end: datetime,
        *,
        adhoc: bool,
        include_rows: bool = False,
    ) -> ReportResult:
        """Query OpenCost for one window, write Parquet to S3, return the result.

        Raises :class:`OpenCostUnavailableError` when OpenCost cannot answer
        and :class:`ReportWriteError` when S3 persistence fails; callers map
        those to a failed scheduled pass or an HTTP 502/503 respectively.
        """
        if window_end <= window_start:
            raise ValueError("window_end must be after window_start")
        if window_end - window_start > timedelta(hours=_MAX_WINDOW_HOURS):
            raise ValueError(f"report windows are capped at {_MAX_WINDOW_HOURS} hours")
        if window_end - window_start < timedelta(minutes=_MIN_WINDOW_MINUTES):
            raise ValueError(f"report windows must span at least {_MIN_WINDOW_MINUTES} minutes")

        allocations = self.opencost.get_allocation(window_start, window_end)
        rows = allocations_to_rows(
            allocations,
            cluster=self.cluster,
            window_start=window_start,
            window_end=window_end,
        )
        key = (
            adhoc_report_key(self.region, window_start, window_end)
            if adhoc
            else scheduled_report_key(self.region, window_start, window_end)
        )
        payload = rows_to_parquet_bytes(rows)
        try:
            self._s3.put_object(Bucket=self.bucket, Key=key, Body=payload)
        except Exception as exc:  # noqa: BLE001 - boto surfaces many shapes
            raise ReportWriteError(f"Failed to write cost report to S3: {exc}") from exc

        return ReportResult(
            s3_key=key,
            row_count=len(rows),
            total_cost=sum(row["total_cost"] for row in rows),
            window_start=window_start.astimezone(UTC).isoformat(),
            window_end=window_end.astimezone(UTC).isoformat(),
            rows=rows if include_rows else [],
        )

    def run_scheduled_once(self, now: datetime | None = None) -> ReportResult | None:
        """Write the report for the most recent completed aligned window.

        Skips (returns ``None``) when that window's object already exists —
        the previous pass, or another replica during a rollout, already
        persisted it. Failures update ``last_error`` and re-raise so the
        caller's loop logs and retries on the next tick.
        """
        moment = now or datetime.now(UTC)
        window_start, window_end = aligned_window(moment, self.report_interval_minutes)
        key = scheduled_report_key(self.region, window_start, window_end)
        if self._object_exists(key):
            logger.debug("Scheduled cost report already present: %s", key)
            return None
        try:
            result = self.generate_report(window_start, window_end, adhoc=False)
        except Exception as exc:
            self.last_error = str(exc)
            raise
        self.last_scheduled_report = result.summary()
        self.last_error = None
        logger.info(
            "Wrote scheduled cost report %s (%d rows, total %.4f USD)",
            result.s3_key,
            result.row_count,
            result.total_cost,
        )
        return result

    def _object_exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self.bucket, Key=key)
        except Exception:  # noqa: BLE001 - 404 and transport errors both mean "write it"
            return False
        return True

    # ------------------------------------------------------------------
    # Introspection for the API surface
    # ------------------------------------------------------------------

    def list_reports(self, *, adhoc: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        """List this region's most recent report objects, newest first."""
        prefix = (
            f"{ADHOC_PREFIX}/region={self.region}/"
            if adhoc
            else f"{SCHEDULED_PREFIX}/region={self.region}/"
        )
        bounded_limit = min(max(int(limit), 1), 1_000)
        paginator = self._s3.get_paginator("list_objects_v2")
        objects: list[dict[str, Any]] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for entry in page.get("Contents", []):
                objects.append(
                    {
                        "key": entry["Key"],
                        "size_bytes": int(entry.get("Size", 0)),
                        "last_modified": (
                            entry["LastModified"].astimezone(UTC).isoformat()
                            if entry.get("LastModified")
                            else None
                        ),
                    }
                )
        objects.sort(key=lambda item: str(item["last_modified"] or ""), reverse=True)
        return objects[:bounded_limit]

    def status(self) -> dict[str, Any]:
        """Operator-facing service status, including OpenCost health.

        ``opencost_returning_data`` performs a live one-hour allocation probe
        — this is the signal release validation gates on, so a healthy-but-
        empty OpenCost (e.g. Prometheus scrape broken) fails validation
        rather than silently producing empty reports.
        """
        opencost_healthy = self.opencost.is_healthy()
        returning_data = False
        allocation_names: list[str] = []
        if opencost_healthy:
            try:
                now = datetime.now(UTC)
                allocations = self.opencost.get_allocation(now - timedelta(hours=1), now)
                allocation_names = sorted(allocations)
                returning_data = bool(allocations)
            except OpenCostUnavailableError as exc:
                logger.warning("OpenCost allocation probe failed: %s", exc)
        return {
            "service": "cost-monitor",
            "region": self.region,
            "cluster": self.cluster,
            "bucket": self.bucket,
            "report_interval_minutes": self.report_interval_minutes,
            "opencost_healthy": opencost_healthy,
            "opencost_returning_data": returning_data,
            "allocation_names": allocation_names[:25],
            "last_scheduled_report": self.last_scheduled_report,
            "last_error": self.last_error,
            "timestamp": datetime.now(UTC).isoformat(),
        }


def create_cost_monitor_from_env() -> CostMonitor:
    """Build a :class:`CostMonitor` from the Deployment's environment."""
    bucket = os.getenv("COST_REPORT_BUCKET", "")
    if not bucket:
        raise RuntimeError("COST_REPORT_BUCKET environment variable is required")
    region = os.getenv("REGION") or os.getenv("AWS_REGION", "")
    if not region:
        raise RuntimeError("REGION environment variable is required")
    cluster = os.getenv("CLUSTER_NAME", f"gco-{region}")
    base_url = os.getenv(
        "OPENCOST_BASE_URL",
        "http://opencost.monitoring.svc.cluster.local:9003",
    )
    try:
        interval = int(os.getenv("COST_REPORT_INTERVAL_MINUTES", "60"))
    except ValueError:
        interval = 60
    return CostMonitor(
        region=region,
        cluster=cluster,
        bucket=bucket,
        opencost=OpenCostClient(base_url),
        report_interval_minutes=interval,
    )
