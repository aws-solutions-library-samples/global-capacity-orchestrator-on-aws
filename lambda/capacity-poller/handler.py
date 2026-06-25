"""Capacity poller Lambda for the Historical Capacity Surface.

Invoked on a schedule by an EventBridge rule (see GCOGlobalStack._create_capacity_poller in gco/stacks/global_stack.py).
For each configured (instance_type, region) pair it snapshots capacity signals via
read-only EC2 APIs and writes one item per pair into the capacity-history DynamoDB
table.

This handler is self-contained (boto3 + stdlib only) and does not import the
CLI/gco packages, matching the convention used by the other GCO Lambdas. It writes
the same DynamoDB item shape that cli/capacity/history.CapacityHistoryStore reads
back.

Environment variables:
    CAPACITY_HISTORY_TABLE_NAME      DynamoDB table to write snapshots to
    WATCH_INSTANCE_TYPES             comma-separated instance types to poll
    ENABLED_REGIONS                  comma-separated regions to poll
    CAPACITY_HISTORY_RETENTION_DAYS  TTL window in days (default 90)
    CAPACITY_BLOCK_DURATION_HOURS    short Capacity Block probe duration (default 24h = 1 day)
    CAPACITY_BLOCK_LONG_DURATION_HOURS
        long Capacity Block probe duration in hours (default 1512h = 63 days).
        Set to 0 to skip the long probe. AWS allows durations in 1-day
        increments up to 14 days, then 7-day increments up to 182 days.

Note: queue_depth is intentionally not collected here; it is a cluster-level signal
that requires EKS access, which this EC2-only poller does not have. The history
store treats a missing metric as absent, so omitting it is safe.

The poller records two Capacity Block availability tiers per snapshot: the short
duration (``capacity_blocks_available`` / ``capacity_blocks_total``) and the long
duration (``capacity_blocks_long_available`` / ``capacity_blocks_long_total``), so
history captures whether *extended-term* blocks (e.g. a 63-day P6 block) are
available, not just the soonest 1-day block.
"""

from __future__ import annotations

import logging
import os
import statistics
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DEFAULT_RETENTION_DAYS = 90
SPOT_PRICE_LOOKBACK_DAYS = 7

# Short Capacity Block probe: the smallest valid block (1 day), captures
# soonest-available capacity. Long probe: an extended-term block (default 63
# days = 9 weeks) so the history surface can answer "is a multi-week block
# available?" for alerting and trend analysis. Both are configurable via the
# CAPACITY_BLOCK_DURATION_HOURS / CAPACITY_BLOCK_LONG_DURATION_HOURS env vars.
DEFAULT_BLOCK_DURATION_HOURS = 24
DEFAULT_LONG_BLOCK_DURATION_HOURS = 63 * 24  # 1512h = 63 days = 9 weeks (a valid CB duration)


def _split_csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _to_decimal(value: Any) -> Any:
    """Convert floats to Decimal for DynamoDB; pass other types through."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return value


def _regional_spot_score(ec2: Any, instance_type: str, region: str) -> int | None:
    """Return the regional Spot Placement Score (1-10) or None if unavailable."""
    try:
        resp = ec2.get_spot_placement_scores(
            InstanceTypes=[instance_type],
            TargetCapacity=1,
            TargetCapacityUnitType="units",
            RegionNames=[region],
            SingleAvailabilityZone=False,
        )
    except Exception as exc:
        logger.warning("spot placement score failed for %s/%s: %s", instance_type, region, exc)
        return None
    for rec in resp.get("SpotPlacementScores", []):
        if "AvailabilityZoneId" not in rec:
            return int(rec.get("Score", 0))
    return None


def _spot_price_summary(ec2: Any, instance_type: str) -> tuple[float | None, int]:
    """Return (mean latest price across AZs, AZ count) from recent spot history."""
    end = datetime.now(UTC)
    start = end - timedelta(days=SPOT_PRICE_LOOKBACK_DAYS)
    try:
        resp = ec2.describe_spot_price_history(
            InstanceTypes=[instance_type],
            ProductDescriptions=["Linux/UNIX"],
            StartTime=start,
            EndTime=end,
        )
    except Exception as exc:
        logger.warning("spot price history failed for %s: %s", instance_type, exc)
        return None, 0
    latest_by_az: dict[str, float] = {}
    for item in resp.get("SpotPriceHistory", []):
        az = item.get("AvailabilityZone")
        if az and az not in latest_by_az:
            latest_by_az[az] = float(item["SpotPrice"])
    if not latest_by_az:
        return None, 0
    return round(statistics.fmean(latest_by_az.values()), 6), len(latest_by_az)


def _capacity_block_summary(
    ec2: Any, instance_type: str, duration_hours: int = DEFAULT_BLOCK_DURATION_HOURS
) -> tuple[int, int]:
    """Return (offering count, total instance count) for available capacity blocks.

    ``duration_hours`` is the Capacity Block duration to probe; the poller calls
    this once for the short tier and once for the long tier.
    """
    try:
        resp = ec2.describe_capacity_block_offerings(
            InstanceType=instance_type,
            InstanceCount=1,
            CapacityDurationHours=duration_hours,
        )
    except Exception as exc:
        logger.debug(
            "capacity block offerings unavailable for %s (%sh): %s",
            instance_type,
            duration_hours,
            exc,
        )
        return 0, 0
    offerings = resp.get("CapacityBlockOfferings", [])
    total = sum(int(o.get("InstanceCount", 0)) for o in offerings)
    return len(offerings), total


def _build_item(
    instance_type: str,
    region: str,
    now: datetime,
    retention_days: int,
    spot_score: int | None,
    spot_price: float | None,
    az_count: int,
    blocks_available: int,
    blocks_total: int,
    long_blocks_available: int | None = None,
    long_blocks_total: int | None = None,
) -> dict[str, Any]:
    """Assemble a DynamoDB item matching the CapacityHistoryStore schema."""
    ts = now.isoformat()
    item: dict[str, Any] = {
        "pk": f"{instance_type}#{region}",
        "sk": ts,
        "instance_type": instance_type,
        "region": region,
        "timestamp": ts,
        "ttl": int((now + timedelta(days=retention_days)).timestamp()),
    }
    if spot_score is not None:
        item["spot_score"] = spot_score
    if spot_price is not None:
        item["spot_price"] = _to_decimal(spot_price)
    if az_count:
        item["az_count"] = az_count
    item["capacity_blocks_available"] = blocks_available
    item["capacity_blocks_total"] = blocks_total
    # Long-duration tier is omitted entirely when the long probe is disabled
    # (CAPACITY_BLOCK_LONG_DURATION_HOURS=0), so the store treats it as absent
    # rather than recording a misleading zero.
    if long_blocks_available is not None:
        item["capacity_blocks_long_available"] = long_blocks_available
    if long_blocks_total is not None:
        item["capacity_blocks_long_total"] = long_blocks_total
    return item


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Snapshot capacity signals for every watched (instance_type, region) pair."""
    table_name = os.environ.get("CAPACITY_HISTORY_TABLE_NAME")
    if not table_name:
        raise ValueError("CAPACITY_HISTORY_TABLE_NAME environment variable is required")

    instance_types = _split_csv(os.environ.get("WATCH_INSTANCE_TYPES"))
    regions = _split_csv(os.environ.get("ENABLED_REGIONS"))
    retention_days = int(
        os.environ.get("CAPACITY_HISTORY_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))
    )
    block_duration_hours = int(
        os.environ.get("CAPACITY_BLOCK_DURATION_HOURS", str(DEFAULT_BLOCK_DURATION_HOURS))
    )
    long_block_duration_hours = int(
        os.environ.get("CAPACITY_BLOCK_LONG_DURATION_HOURS", str(DEFAULT_LONG_BLOCK_DURATION_HOURS))
    )

    if not instance_types:
        logger.warning("WATCH_INSTANCE_TYPES is empty; nothing to poll")
    if not regions:
        logger.warning("ENABLED_REGIONS is empty; nothing to poll")

    table = boto3.resource("dynamodb").Table(table_name)
    now = datetime.now(UTC)
    written = 0
    errors = 0

    # The long probe is enabled when its duration is positive and differs from
    # the short probe; when equal we reuse the short result to avoid a redundant
    # API call, and when <= 0 we skip it entirely (long fields stay absent).
    long_probe_enabled = long_block_duration_hours > 0

    for region in regions:
        ec2 = boto3.client("ec2", region_name=region)
        for instance_type in instance_types:
            try:
                spot_score = _regional_spot_score(ec2, instance_type, region)
                spot_price, az_count = _spot_price_summary(ec2, instance_type)
                blocks_available, blocks_total = _capacity_block_summary(
                    ec2, instance_type, block_duration_hours
                )
                long_available: int | None = None
                long_total: int | None = None
                if long_probe_enabled:
                    if long_block_duration_hours == block_duration_hours:
                        long_available, long_total = blocks_available, blocks_total
                    else:
                        long_available, long_total = _capacity_block_summary(
                            ec2, instance_type, long_block_duration_hours
                        )
                item = _build_item(
                    instance_type,
                    region,
                    now,
                    retention_days,
                    spot_score,
                    spot_price,
                    az_count,
                    blocks_available,
                    blocks_total,
                    long_available,
                    long_total,
                )
                table.put_item(Item=item)
                written += 1
            except Exception as exc:
                errors += 1
                logger.exception("failed to record %s/%s: %s", instance_type, region, exc)

    logger.info("capacity poll complete: written=%d errors=%d", written, errors)
    return {"written": written, "errors": errors, "timestamp": now.isoformat()}
