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

Note: queue_depth is intentionally not collected here; it is a cluster-level signal
that requires EKS access, which this EC2-only poller does not have. The history
store treats a missing metric as absent, so omitting it is safe.
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
CAPACITY_BLOCK_DURATION_HOURS = 24


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


def _capacity_block_summary(ec2: Any, instance_type: str) -> tuple[int, int]:
    """Return (offering count, total instance count) for available capacity blocks."""
    try:
        resp = ec2.describe_capacity_block_offerings(
            InstanceType=instance_type,
            InstanceCount=1,
            CapacityDurationHours=CAPACITY_BLOCK_DURATION_HOURS,
        )
    except Exception as exc:
        logger.debug("capacity block offerings unavailable for %s: %s", instance_type, exc)
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

    if not instance_types:
        logger.warning("WATCH_INSTANCE_TYPES is empty; nothing to poll")
    if not regions:
        logger.warning("ENABLED_REGIONS is empty; nothing to poll")

    table = boto3.resource("dynamodb").Table(table_name)
    now = datetime.now(UTC)
    written = 0
    errors = 0

    for region in regions:
        ec2 = boto3.client("ec2", region_name=region)
        for instance_type in instance_types:
            try:
                spot_score = _regional_spot_score(ec2, instance_type, region)
                spot_price, az_count = _spot_price_summary(ec2, instance_type)
                blocks_available, blocks_total = _capacity_block_summary(ec2, instance_type)
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
                )
                table.put_item(Item=item)
                written += 1
            except Exception as exc:
                errors += 1
                logger.exception("failed to record %s/%s: %s", instance_type, region, exc)

    logger.info("capacity poll complete: written=%d errors=%d", written, errors)
    return {"written": written, "errors": errors, "timestamp": now.isoformat()}
