"""
DynamoDB-backed time-series store for historical capacity signals.

This is the storage and query layer for the Historical Capacity Surface.
A scheduled poller (lambda/capacity-poller) snapshots capacity signals --
spot placement score, spot price, AZ coverage, queue depth, and capacity
block availability -- for a watched set of instance types across regions and
writes them here. The CLI (gco capacity history ...) and the Bedrock capacity
advisor read them back for temporal querying and prompt enrichment.

Table schema (a single global table; see GCOGlobalStack._create_capacity_poller):

    pk (partition) = "{instance_type}#{region}"
    sk (sort) = ISO-8601 UTC timestamp of the snapshot

    GSI "by-timestamp": pk = instance_type, sk = timestamp
    (cross-region trend queries for one instance type)

Item attributes: instance_type, region, timestamp, spot_score, spot_price,
az_count, queue_depth, capacity_blocks_available, capacity_blocks_total,
capacity_blocks_long_available, capacity_blocks_long_total, and ttl (epoch
seconds for DynamoDB auto-expiry, default 90 days). The ``*_long_*`` fields
track availability of extended-term blocks (the poller's long-duration probe,
default 63 days) separately from the soonest short block.

Numbers are stored as DynamoDB Decimal (the resource API rejects float) and
re-hydrated to int/float on read. The poller Lambda is self-contained and
writes the same item shape directly with boto3; this module is the canonical
writer/reader used by the CLI and advisor.
"""

from __future__ import annotations

import logging
import os
import statistics
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

DEFAULT_TABLE_NAME = "gco-capacity-history"
DEFAULT_RETENTION_DAYS = 90
GSI_BY_TIMESTAMP = "by-timestamp"

# The numeric metrics tracked per snapshot. Statistics and temporal-pattern
# aggregation iterate over this tuple, so adding a metric is a one-line change.
# The ``capacity_blocks_long_*`` pair mirrors the short-duration block metrics
# but for the poller's long-duration probe (default 63 days), so trend/alerting
# queries can distinguish soonest-available blocks from extended-term ones.
METRIC_FIELDS: tuple[str, ...] = (
    "spot_score",
    "spot_price",
    "az_count",
    "queue_depth",
    "capacity_blocks_available",
    "capacity_blocks_total",
    "capacity_blocks_long_available",
    "capacity_blocks_long_total",
)

# weekday() -> name. Monday is 0, matching datetime.weekday().
DAY_NAMES: tuple[str, ...] = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def make_pk(instance_type: str, region: str) -> str:
    """Build the partition key for a (instance_type, region) series."""
    return f"{instance_type}#{region}"


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating a trailing Z and naive values."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError, AttributeError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _to_dynamo(value: Any) -> Any:
    """Convert a Python value into a DynamoDB-storable type.

    The DynamoDB resource API rejects float, so numbers must be Decimal.
    Floats are routed through Decimal(str(x)) so the decimal string round-trips
    without binary-float artifacts. bool is checked before the numeric branch
    because bool is a subclass of int.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_dynamo(v) for v in value]
    return value


def _from_dynamo(value: Any) -> Any:
    """Convert DynamoDB types back to plain Python (Decimal -> int/float)."""
    if isinstance(value, Decimal):
        return int(value) if value == int(value) else float(value)
    if isinstance(value, dict):
        return {k: _from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_dynamo(v) for v in value]
    return value


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (pct in [0, 100]) over sorted values."""
    if not sorted_values:
        raise ValueError("percentile of empty sequence")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = rank - lo
    return float(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac)


def flatten_capacity_data(capacity_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten gather_capacity_data() output into per-(instance_type, region) records.

    Returns one record per (instance_type, region) pair that carries at least
    one signal. Each record holds the keys instance_type, region, timestamp
    plus whatever subset of METRIC_FIELDS can be derived. Metrics that cannot
    be derived are omitted (the statistics layer treats a missing metric as
    absent, never as zero).
    """
    timestamp = capacity_data.get("timestamp") or _utc_now().isoformat()

    queue_by_region: dict[str, Any] = {}
    for metric in capacity_data.get("cluster_metrics", []) or []:
        region = metric.get("region")
        if region is not None and metric.get("queue_depth") is not None:
            queue_by_region[region] = metric["queue_depth"]

    spot_data = capacity_data.get("spot_data", {}) or {}
    capacity_blocks = capacity_data.get("capacity_blocks", {}) or {}

    pairs: set[tuple[str, str]] = set()
    for itype, regions in spot_data.items():
        for region in regions or {}:
            pairs.add((itype, region))
    for itype, regions in capacity_blocks.items():
        for region in regions or {}:
            pairs.add((itype, region))

    records: list[dict[str, Any]] = []
    for itype, region in sorted(pairs):
        spot_info = (spot_data.get(itype, {}) or {}).get(region, {}) or {}
        prices = spot_info.get("prices", []) or []
        scores = spot_info.get("placement_scores", {}) or {}

        record: dict[str, Any] = {
            "instance_type": itype,
            "region": region,
            "timestamp": timestamp,
        }

        regional_score = scores.get("regional")
        if regional_score is not None:
            record["spot_score"] = regional_score

        current_prices = [p["current"] for p in prices if p.get("current") is not None]
        if current_prices:
            record["spot_price"] = round(sum(current_prices) / len(current_prices), 6)
            record["az_count"] = len(current_prices)

        if region in queue_by_region:
            record["queue_depth"] = queue_by_region[region]

        blocks = (capacity_blocks.get(itype, {}) or {}).get(region, []) or []
        if blocks:
            record["capacity_blocks_available"] = len(blocks)
            record["capacity_blocks_total"] = len(blocks)

        if any(field in record for field in METRIC_FIELDS):
            records.append(record)

    return records


def _resolve_global_region() -> str:
    """Resolve the region where the capacity-history table lives.

    The table is created by the global stack, so it lives in the GCO global
    region. Resolve that from the CLI config (which reads cdk.json and
    GCO_GLOBAL_REGION) so callers don't need to set DYNAMODB_REGION. Falls back
    to us-east-1 only if the config cannot be loaded.
    """
    try:
        from cli.config import get_config

        return get_config().global_region or "us-east-1"
    except Exception:
        return "us-east-1"


def _resolve_default_table_name() -> str:
    """Resolve the capacity-history table name from the configured project (#139).

    The global stack creates the table as ``{project_name}-capacity-history``
    (see ``GCOGlobalStack._create_capacity_poller``), so derive the same name
    from the CLI config (which reads cdk.json / ``GCO_PROJECT_NAME``). Falls
    back to the default ``gco-capacity-history`` when the config can't be
    loaded. For the default ``gco`` project this is byte-identical.
    """
    try:
        from cli.config import get_config

        project = get_config().project_name
        if project:
            return f"{project}-capacity-history"
    except Exception as exc:
        logger.debug("Falling back to default capacity history table name: %s", exc)
    return DEFAULT_TABLE_NAME


class CapacityHistoryStore:
    """DynamoDB-backed time-series store for capacity snapshots.

    Table name resolves from the CAPACITY_HISTORY_TABLE_NAME env var (default
    gco-capacity-history). Region resolves from an explicit argument, then
    DYNAMODB_REGION or REGION, then the configured GCO global region (where the
    global stack creates the table), falling back to us-east-1. Retention
    defaults to 90 days and feeds the per-item ttl.
    """

    def __init__(
        self,
        table_name: str | None = None,
        region: str | None = None,
        retention_days: int | None = None,
    ):
        self.table_name = (
            table_name or os.getenv("CAPACITY_HISTORY_TABLE_NAME") or _resolve_default_table_name()
        )
        self._region = (
            region
            or os.getenv("DYNAMODB_REGION")
            or os.getenv("REGION")
            or _resolve_global_region()
        )
        if retention_days is not None:
            self.retention_days = retention_days
        else:
            self.retention_days = int(
                os.getenv("CAPACITY_HISTORY_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))
            )
        self._dynamodb = boto3.resource("dynamodb", region_name=self._region)
        self._table = self._dynamodb.Table(self.table_name)

    def put_snapshot(
        self,
        instance_type: str,
        region: str,
        metrics: dict[str, Any],
        *,
        timestamp: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Persist a single capacity snapshot and return the stored item.

        metrics is filtered to METRIC_FIELDS; None values are dropped so an
        absent metric is never stored as zero.
        """
        now = now or _utc_now()
        ts = timestamp or now.isoformat()
        ttl_epoch = int((now + timedelta(days=self.retention_days)).timestamp())

        item: dict[str, Any] = {
            "pk": make_pk(instance_type, region),
            "sk": ts,
            "instance_type": instance_type,
            "region": region,
            "timestamp": ts,
            "ttl": ttl_epoch,
        }
        for field in METRIC_FIELDS:
            value = metrics.get(field)
            if value is not None:
                item[field] = value

        self._table.put_item(Item=_to_dynamo(item))
        stored: dict[str, Any] = _from_dynamo(item)
        return stored

    def record(self, capacity_data: dict[str, Any], *, now: datetime | None = None) -> int:
        """Flatten gather_capacity_data() output and persist one item per
        (instance_type, region) snapshot. Returns the number of items written.
        """
        now = now or _utc_now()
        written = 0
        for rec in flatten_capacity_data(capacity_data):
            self.put_snapshot(
                instance_type=rec["instance_type"],
                region=rec["region"],
                metrics={field: rec[field] for field in METRIC_FIELDS if field in rec},
                timestamp=rec.get("timestamp"),
                now=now,
            )
            written += 1
        return written

    def get_trend(
        self,
        instance_type: str,
        region: str,
        hours_back: int = 168,
    ) -> list[dict[str, Any]]:
        """Return snapshots for one (instance_type, region) within the window,
        oldest first. Default window is 7 days (168 hours).
        """
        cutoff = (_utc_now() - timedelta(hours=hours_back)).isoformat()
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": (
                Key("pk").eq(make_pk(instance_type, region)) & Key("sk").gte(cutoff)
            ),
            "ScanIndexForward": True,
        }
        while True:
            resp = self._table.query(**kwargs)
            items.extend(_from_dynamo(i) for i in resp.get("Items", []))
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
        return items

    def get_statistics(
        self,
        instance_type: str,
        region: str,
        hours_back: int = 168,
    ) -> dict[str, Any]:
        """Compute p25/p50/p75/min/max/stddev (plus count/mean) per metric over
        the window. Metrics with no data points are omitted.
        """
        trend = self.get_trend(instance_type, region, hours_back)
        stats: dict[str, Any] = {
            "instance_type": instance_type,
            "region": region,
            "hours_back": hours_back,
            "sample_count": len(trend),
            "metrics": {},
        }
        for field in METRIC_FIELDS:
            values = [float(rec[field]) for rec in trend if rec.get(field) is not None]
            if not values:
                continue
            values.sort()
            stats["metrics"][field] = {
                "count": len(values),
                "min": values[0],
                "max": values[-1],
                "mean": round(statistics.fmean(values), 6),
                "p25": round(_percentile(values, 25), 6),
                "p50": round(_percentile(values, 50), 6),
                "p75": round(_percentile(values, 75), 6),
                "stddev": round(statistics.stdev(values), 6) if len(values) > 1 else 0.0,
            }
        return stats

    def get_temporal_patterns(
        self,
        instance_type: str,
        region: str,
        hours_back: int = 168,
        metric: str = "spot_score",
    ) -> dict[str, Any]:
        """Group snapshots by day-of-week and hour, returning the average of
        metric (default spot_score) per (day, hour) slot, plus a best_windows
        list sorted by descending average for prompt enrichment.
        """
        trend = self.get_trend(instance_type, region, hours_back)

        buckets: dict[tuple[int, int], list[float]] = {}
        for rec in trend:
            value = rec.get(metric)
            ts = rec.get("timestamp")
            if value is None or not ts:
                continue
            dt = _parse_iso(str(ts))
            if dt is None:
                continue
            buckets.setdefault((dt.weekday(), dt.hour), []).append(float(value))

        patterns: dict[str, dict[int, dict[str, float]]] = {}
        best_windows: list[dict[str, Any]] = []
        for (dow, hour), values in buckets.items():
            avg = round(statistics.fmean(values), 4)
            day = DAY_NAMES[dow]
            patterns.setdefault(day, {})[hour] = {"avg": avg, "count": len(values)}
            best_windows.append({"day": day, "hour": hour, "avg": avg, "count": len(values)})

        best_windows.sort(key=lambda window: window["avg"], reverse=True)
        return {
            "instance_type": instance_type,
            "region": region,
            "metric": metric,
            "patterns": patterns,
            "best_windows": best_windows,
        }

    def get_regions_with_data(
        self,
        instance_type: str,
        hours_back: int = 168,
    ) -> list[str]:
        """Return the distinct regions that have snapshots for an instance type.

        Queries the ``by-timestamp`` GSI (pk=instance_type) within the window
        and collects the distinct ``region`` values, sorted alphabetically.
        Powers cross-region queries such as ``gco capacity predict --all-regions``.
        """
        cutoff = (_utc_now() - timedelta(hours=hours_back)).isoformat()
        regions: set[str] = set()
        kwargs: dict[str, Any] = {
            "IndexName": GSI_BY_TIMESTAMP,
            "KeyConditionExpression": (
                Key("instance_type").eq(instance_type) & Key("sk").gte(cutoff)
            ),
            "ProjectionExpression": "#r",
            "ExpressionAttributeNames": {"#r": "region"},
        }
        while True:
            resp = self._table.query(**kwargs)
            for item in resp.get("Items", []):
                value = item.get("region")
                if value:
                    regions.add(str(value))
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
        return sorted(regions)


def get_capacity_history_store(
    table_name: str | None = None,
    region: str | None = None,
    retention_days: int | None = None,
) -> CapacityHistoryStore:
    """Factory for CapacityHistoryStore."""
    return CapacityHistoryStore(table_name=table_name, region=region, retention_days=retention_days)
