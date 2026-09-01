"""Capacity poller Lambda for the Historical Capacity Surface.

Invoked on a schedule by an EventBridge rule (see GCOGlobalStack._create_capacity_poller in gco/stacks/global_stack.py).
Snapshots capacity signals via read-only EC2 APIs and writes one item per
watched (instance_type, region) pair into the capacity-history DynamoDB table.

This handler is self-contained (boto3 + stdlib only) and does not import the
CLI/gco packages, matching the convention used by the other GCO Lambdas. It writes
the same DynamoDB item shape that cli/capacity/history.CapacityHistoryStore reads
back.

Control flow is phased because the signals have different shapes:

    Phase 0 — region enablement. A client in the Lambda's default Region calls
        ``DescribeRegions(AllRegions=True, RegionNames=[region])`` for each
        configured Region. Only the authoritative ``not-opted-in`` state is
        skipped. Missing/malformed responses, permission failures, throttling,
        and transport errors remain ``unknown`` and fail open to polling so an
        operational probe failure can never masquerade as deliberately absent
        capacity. Explicitly not-enabled Regions are counted in the return
        payload and receive no snapshots.
    Phase 1 — Spot Placement Scores. AWS documents that
        ``GetSpotPlacementScores`` needs at least three instance types for a
        meaningful answer, so scores are requested per *instance pool* (a
        reviewed set of interchangeable types; see INSTANCE_POOLS in
        scripts/accelerator_catalog.py), once per (pool, target capacity),
        with regions batched at most ``SPS_REGION_BATCH_SIZE`` per request —
        the API returns the top 10 scored regions, so larger batches could
        silently drop a requested region. One shared EC2 client issues every
        SPS request; the API is cross-region regardless of endpoint.
    Phase 2 — completeness. Expected (pool, region, capacity) combinations
        are diffed against those received; only the gaps are re-requested,
        for at most ``SPS_MAX_ATTEMPTS`` total passes so a persistent refusal
        can never run the function toward its timeout.
    Phase 3 — per-region metrics and write. Spot price, AZ count, and
        Capacity Block offerings are inherently per-region. Failed probes stay
        absent rather than becoming zero; when every signal for a pair fails,
        no history item is written and the summary records an error.

``MaxConfigLimitExceeded`` (the account has asked SPS about too many distinct
configurations in the rolling window) is detected by error code, logged at
warning level, counted separately in the return payload, and the affected
score fields are omitted — an absent metric means "not obtained", never zero.

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
    SPOT_SCORE_TARGET_CAPACITIES
        JSON array of {"target_capacity": int, "metric_field": str} objects,
        e.g. [{"target_capacity": 1, "metric_field": "spot_score"}, ...].
        The stack serializes this from the supported set and naming rule
        exported by cli/capacity/history.py, so the capacity->field mapping
        has exactly one source of truth even though this module cannot import
        it. Default: capacity 1 -> spot_score.
    INSTANCE_POOLS
        JSON array of {"name": str, "members": [str, ...]} objects in
        priority order; the stack serializes it from
        scripts/accelerator_catalog.py INSTANCE_POOLS. Scores are requested
        with a pool's full member list, and a watched type's snapshot records
        the score of the first pool in this order that contains it, under the
        ``spot_pool`` attribute. Watched types in no pool get price/Capacity
        Block metrics but no placement score (deliberate; see
        UNPOOLED_INSTANCE_TYPES in the catalog). Default: no pools, no SPS.

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

import json
import logging
import os
import statistics
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import batched
from typing import Any, Literal

import boto3

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-09-01T14:42:56Z
# Generated from Git commit: 89b000378ed5a912a38c06f4feab2b029936ebcc
# Flowchart(s) generated from this file:
#   * ``lambda_handler`` -> ``diagrams/code_diagrams/lambda/capacity-poller/handler.lambda_handler.html``
#     (PNG: ``diagrams/code_diagrams/lambda/capacity-poller/handler.lambda_handler.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


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

# GetSpotPlacementScores returns the top 10 scored regions. Requesting more
# regions than the response can name would let a region drop out silently, so
# region batches never exceed this bound.
SPS_REGION_BATCH_SIZE = 10

# Total request passes for Spot Placement Scores: one initial pass plus
# bounded retries of whatever combinations are still missing. Three passes of
# a handful of (pool, capacity) requests complete in seconds, nowhere near the
# 14-minute function timeout, and a combination still missing afterwards is
# reported in the return payload rather than chased indefinitely.
SPS_MAX_ATTEMPTS = 3

# Fallback capacity->field mapping when SPOT_SCORE_TARGET_CAPACITIES is not
# set: the pre-pool behavior of a single implicit target capacity of 1,
# recorded in the original spot_score field.
DEFAULT_TARGET_CAPACITIES: tuple[tuple[int, str], ...] = ((1, "spot_score"),)


def _split_csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _to_decimal(value: Any) -> Any:
    """Convert floats to Decimal for DynamoDB; pass other types through."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return value


def _error_code(exc: Exception) -> str | None:
    """Return the AWS error code carried by a botocore ClientError-shaped exception.

    Duck-typed off the ``response`` attribute rather than importing botocore,
    keeping this module's import surface to boto3 + stdlib.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    if not isinstance(error, dict):
        return None
    code = error.get("Code")
    return code if isinstance(code, str) else None


def _parse_target_capacities(raw: str | None) -> tuple[tuple[int, str], ...]:
    """Parse SPOT_SCORE_TARGET_CAPACITIES into ((capacity, metric_field), ...).

    The stack derives the mapping from cli/capacity/history.py, so this
    parser only enforces shape. Malformed configuration raises rather than
    silently collecting under wrong field names.
    """
    if not raw or not raw.strip():
        return DEFAULT_TARGET_CAPACITIES
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"SPOT_SCORE_TARGET_CAPACITIES is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list) or not parsed:
        raise ValueError(
            "SPOT_SCORE_TARGET_CAPACITIES must be a non-empty JSON array of "
            f'{{"target_capacity", "metric_field"}} objects, got {parsed!r}'
        )
    capacities: list[tuple[int, str]] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            raise ValueError(f"SPOT_SCORE_TARGET_CAPACITIES entries must be objects, got {entry!r}")
        capacity = entry.get("target_capacity")
        field = entry.get("metric_field")
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError(
                f"SPOT_SCORE_TARGET_CAPACITIES target_capacity must be a positive "
                f"integer, got {capacity!r}"
            )
        if not isinstance(field, str) or not field:
            raise ValueError(
                f"SPOT_SCORE_TARGET_CAPACITIES metric_field must be a non-empty "
                f"string, got {field!r}"
            )
        capacities.append((capacity, field))
    return tuple(capacities)


def _parse_instance_pools(raw: str | None) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Parse INSTANCE_POOLS into ((pool_name, member_types), ...) in priority order.

    A pool with fewer than three members would reintroduce the depressed-score
    bug this poller exists to fix, so malformed pool configuration raises
    instead of being polled.
    """
    if not raw or not raw.strip():
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"INSTANCE_POOLS is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError(
            f'INSTANCE_POOLS must be a JSON array of {{"name", "members"}}, got {parsed!r}'
        )
    pools: list[tuple[str, tuple[str, ...]]] = []
    seen: set[str] = set()
    for entry in parsed:
        if not isinstance(entry, dict):
            raise ValueError(f"INSTANCE_POOLS entries must be objects, got {entry!r}")
        name = entry.get("name")
        members = entry.get("members")
        if not isinstance(name, str) or not name:
            raise ValueError(f"INSTANCE_POOLS pool name must be a non-empty string, got {name!r}")
        if name in seen:
            raise ValueError(f"INSTANCE_POOLS declares pool {name!r} more than once")
        if (
            not isinstance(members, list)
            or not all(isinstance(member, str) and member for member in members)
            or len(set(members)) < 3
        ):
            raise ValueError(
                f"INSTANCE_POOLS pool {name!r} must list at least three distinct "
                f"instance types (GetSpotPlacementScores needs three for a "
                f"meaningful score), got {members!r}"
            )
        seen.add(name)
        pools.append((name, tuple(members)))
    return tuple(pools)


def _pool_for_instance_type(
    pools: tuple[tuple[str, tuple[str, ...]], ...], instance_type: str
) -> tuple[str, tuple[str, ...]] | None:
    """Return the first pool in priority order containing instance_type, if any."""
    for name, members in pools:
        if instance_type in members:
            return (name, members)
    return None


def _region_enablement_status(region: str) -> Literal["enabled", "not-enabled", "unknown"]:
    """Classify account opt-in state without contacting the target endpoint.

    ``DescribeRegions(AllRegions=True)`` is authoritative for explicit
    ``not-opted-in`` state. Permission, throttling, transport, and malformed
    responses remain unknown and are polled so operational failure cannot be
    reported as intentionally absent capacity.
    """
    try:
        ec2 = boto3.client("ec2")
        response = ec2.describe_regions(AllRegions=True, RegionNames=[region])
        rows = response.get("Regions", [])
        match = next((row for row in rows if row.get("RegionName") == region), None)
        if match is None:
            logger.warning(
                "region-enablement probe for %s returned no matching region; polling it as unknown",
                region,
            )
            return "unknown"
        opt_in_status = match.get("OptInStatus")
        if opt_in_status == "not-opted-in":
            logger.info("region %s is explicitly not opted in; skipping it", region)
            return "not-enabled"
        if opt_in_status in {"opt-in-not-required", "opted-in"}:
            return "enabled"
        logger.warning(
            "region-enablement probe for %s returned unknown OptInStatus %r; polling it",
            region,
            opt_in_status,
        )
        return "unknown"
    except Exception as exc:
        code = _error_code(exc)
        logger.warning(
            "region-enablement ec2:DescribeRegions probe for %s failed (%s); polling it as "
            "unknown so the per-region calls expose any real operational failure",
            region,
            code or type(exc).__name__,
        )
        return "unknown"


def _region_is_enabled(region: str) -> bool:
    """Compatibility predicate: only explicit not-opted-in evidence skips."""
    return _region_enablement_status(region) != "not-enabled"


def _regional_scores_from_response(response: dict[str, Any], regions: set[str]) -> dict[str, int]:
    """Extract region -> regional score from one GetSpotPlacementScores page.

    AZ-level records (carrying AvailabilityZoneId) and regions outside the
    requested batch are ignored.
    """
    scores: dict[str, int] = {}
    for rec in response.get("SpotPlacementScores", []):
        if "AvailabilityZoneId" in rec:
            continue
        region = rec.get("Region")
        if region in regions and rec.get("Score") is not None:
            scores[region] = int(rec["Score"])
    return scores


def _collect_spot_placement_scores(
    ec2: Any,
    pools: tuple[tuple[str, tuple[str, ...]], ...],
    target_capacities: tuple[tuple[int, str], ...],
    regions: list[str],
) -> tuple[dict[tuple[str, str, int], int], dict[str, int]]:
    """Collect pooled Spot Placement Scores for every (pool, region, capacity).

    Issues one request per (pool, target capacity, region batch) against a
    single EC2 client, then re-requests only the missing combinations for at
    most SPS_MAX_ATTEMPTS total passes. Returns the score mapping and the
    counters for the structured summary; a MaxConfigLimitExceeded refusal is
    logged distinctly, counted, and leaves its combinations absent.
    """
    scores: dict[tuple[str, str, int], int] = {}
    counters = {
        "requests_issued": 0,
        "config_limit_refusals": 0,
    }
    expected = {
        (name, region, capacity)
        for name, _members in pools
        for region in regions
        for capacity, _field in target_capacities
    }

    for attempt in range(1, SPS_MAX_ATTEMPTS + 1):
        requested_this_pass = False
        for name, members in pools:
            for capacity, _field in target_capacities:
                missing_regions = [
                    region for region in regions if (name, region, capacity) not in scores
                ]
                if not missing_regions:
                    continue
                if attempt > 1:
                    logger.info(
                        "retrying spot placement scores (attempt %d/%d) for pool=%s "
                        "capacity=%d regions=%s",
                        attempt,
                        SPS_MAX_ATTEMPTS,
                        name,
                        capacity,
                        missing_regions,
                    )
                for batch in batched(missing_regions, SPS_REGION_BATCH_SIZE, strict=False):
                    requested_this_pass = True
                    batch_set = set(batch)
                    kwargs: dict[str, Any] = {
                        "InstanceTypes": list(members),
                        "TargetCapacity": capacity,
                        "TargetCapacityUnitType": "units",
                        "RegionNames": list(batch),
                        "SingleAvailabilityZone": False,
                    }
                    try:
                        while True:
                            counters["requests_issued"] += 1
                            response = ec2.get_spot_placement_scores(**kwargs)
                            for region, score in _regional_scores_from_response(
                                response, batch_set
                            ).items():
                                scores[(name, region, capacity)] = score
                            next_token = response.get("NextToken")
                            if not next_token:
                                break
                            kwargs["NextToken"] = next_token
                    except Exception as exc:
                        if _error_code(exc) == "MaxConfigLimitExceeded":
                            counters["config_limit_refusals"] += 1
                            logger.warning(
                                "spot placement scores REFUSED (MaxConfigLimitExceeded) for "
                                "pool=%s capacity=%d regions=%s: the account has queried too "
                                "many distinct SPS configurations in the rolling window; the "
                                "affected score fields are omitted from this cycle's snapshots",
                                name,
                                capacity,
                                list(batch),
                            )
                        else:
                            logger.warning(
                                "spot placement scores failed for pool=%s capacity=%d "
                                "regions=%s: %s",
                                name,
                                capacity,
                                list(batch),
                                exc,
                            )
        if not requested_this_pass:
            break

    missing = sorted(expected - set(scores))
    if missing:
        logger.warning(
            "spot placement scores missing after %d attempt(s) for %d combination(s): %s",
            SPS_MAX_ATTEMPTS,
            len(missing),
            missing,
        )
    counters["combinations_expected"] = len(expected)
    counters["combinations_received"] = len(expected) - len(missing)
    counters["combinations_missing_after_retry"] = len(missing)
    return scores, counters


def _spot_price_summary(ec2: Any, instance_type: str) -> tuple[float | None, int | None]:
    """Return (mean latest price, AZ count), preserving probe failure as None."""
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
        return None, None
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
) -> tuple[int | None, int | None]:
    """Return offering/instance counts, or ``(None, None)`` on probe failure.

    ``duration_hours`` is the Capacity Block duration to probe; the poller calls
    this once for the short tier and once for the long tier. A successful empty
    response is ``(0, 0)``; failure stays absent so it cannot become false
    zero-capacity history.
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
        return None, None
    offerings = resp.get("CapacityBlockOfferings", [])
    total = sum(int(o.get("InstanceCount", 0)) for o in offerings)
    return len(offerings), total


def _build_item(
    instance_type: str,
    region: str,
    now: datetime,
    retention_days: int,
    spot_scores: dict[str, int],
    spot_pool: str | None,
    spot_price: float | None,
    az_count: int | None,
    blocks_available: int | None,
    blocks_total: int | None,
    long_blocks_available: int | None = None,
    long_blocks_total: int | None = None,
) -> dict[str, Any]:
    """Assemble a DynamoDB item matching the CapacityHistoryStore schema.

    ``spot_scores`` maps metric field name (spot_score, spot_score_at_N) to
    the pooled score value; ``spot_pool`` names the pool those scores were
    requested for and is recorded only when at least one score was obtained,
    so refused or unpooled snapshots carry neither the fields nor a dangling
    attribution. Per-Region probe values remain optional: ``None`` means the
    API call failed and the field is omitted, while a successful empty
    Capacity Block response is recorded as a real zero.
    """
    ts = now.isoformat()
    item: dict[str, Any] = {
        "pk": f"{instance_type}#{region}",
        "sk": ts,
        "instance_type": instance_type,
        "region": region,
        "timestamp": ts,
        "ttl": int((now + timedelta(days=retention_days)).timestamp()),
    }
    for field, score in spot_scores.items():
        item[field] = score
    if spot_scores and spot_pool is not None:
        item["spot_pool"] = spot_pool
    if spot_price is not None:
        item["spot_price"] = _to_decimal(spot_price)
    if az_count is not None:
        item["az_count"] = az_count
    if blocks_available is not None:
        item["capacity_blocks_available"] = blocks_available
    if blocks_total is not None:
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
    configured_regions = _split_csv(os.environ.get("ENABLED_REGIONS"))
    retention_days = int(
        os.environ.get("CAPACITY_HISTORY_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))
    )
    block_duration_hours = int(
        os.environ.get("CAPACITY_BLOCK_DURATION_HOURS", str(DEFAULT_BLOCK_DURATION_HOURS))
    )
    long_block_duration_hours = int(
        os.environ.get("CAPACITY_BLOCK_LONG_DURATION_HOURS", str(DEFAULT_LONG_BLOCK_DURATION_HOURS))
    )
    target_capacities = _parse_target_capacities(os.environ.get("SPOT_SCORE_TARGET_CAPACITIES"))
    pools = _parse_instance_pools(os.environ.get("INSTANCE_POOLS"))

    if not instance_types:
        logger.warning("WATCH_INSTANCE_TYPES is empty; nothing to poll")
    if not configured_regions:
        logger.warning("ENABLED_REGIONS is empty; nothing to poll")

    table = boto3.resource("dynamodb").Table(table_name)
    now = datetime.now(UTC)
    written = 0
    errors = 0

    # Phase 0 — region enablement pre-check. A not-enabled region would fail
    # every API call in ways the per-type error isolation below would swallow,
    # which is exactly the "absent data that is really a failure" class this
    # poller must not produce.
    regions: list[str] = []
    regions_skipped_not_enabled: list[str] = []
    regions_enablement_unknown: list[str] = []
    for region in configured_regions:
        enablement = _region_enablement_status(region)
        if enablement == "not-enabled":
            regions_skipped_not_enabled.append(region)
            continue
        regions.append(region)
        if enablement == "unknown":
            regions_enablement_unknown.append(region)

    # Phase 1 + 2 — pooled, batched, multi-capacity SPS with bounded
    # completeness retry. Only pools containing at least one watched type are
    # requested, but each request carries the pool's full member list: the
    # score is a property of the whole pool, and shrinking the list to the
    # watched subset would change (and potentially depress) the measurement.
    watched = set(instance_types)
    relevant_pools = tuple(
        (name, members) for name, members in pools if watched.intersection(members)
    )
    unpooled_watch_types = sorted(
        itype for itype in watched if _pool_for_instance_type(pools, itype) is None
    )
    if unpooled_watch_types:
        logger.info(
            "%d watched instance type(s) belong to no instance pool and get no placement "
            "score (price and Capacity Block metrics are still recorded): %s",
            len(unpooled_watch_types),
            unpooled_watch_types,
        )
    sps_client = boto3.client("ec2")
    scores, sps_counters = _collect_spot_placement_scores(
        sps_client, relevant_pools, target_capacities, regions
    )

    # The long probe is enabled when its duration is positive and differs from
    # the short probe; when equal we reuse the short result to avoid a redundant
    # API call, and when <= 0 we skip it entirely (long fields stay absent).
    long_probe_enabled = long_block_duration_hours > 0

    # Phase 3 — per-region price and Capacity Block metrics, then assemble and
    # write. This loop is unchanged in shape from the pre-pool poller; only
    # the score fields now come from the phase-1 pool collection.
    for region in regions:
        ec2 = boto3.client("ec2", region_name=region)
        for instance_type in instance_types:
            try:
                pool = _pool_for_instance_type(pools, instance_type)
                spot_scores: dict[str, int] = {}
                spot_pool: str | None = None
                if pool is not None:
                    pool_name, _members = pool
                    spot_pool = pool_name
                    for capacity, field in target_capacities:
                        value = scores.get((pool_name, region, capacity))
                        if value is not None:
                            spot_scores[field] = value
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
                signal_obtained = bool(spot_scores) or any(
                    value is not None
                    for value in (
                        spot_price,
                        az_count,
                        blocks_available,
                        blocks_total,
                        long_available,
                        long_total,
                    )
                )
                if not signal_obtained:
                    errors += 1
                    logger.warning(
                        "all capacity probes failed for %s/%s; skipping history write "
                        "rather than recording false zero capacity",
                        instance_type,
                        region,
                    )
                    continue
                item = _build_item(
                    instance_type,
                    region,
                    now,
                    retention_days,
                    spot_scores,
                    spot_pool,
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

    summary: dict[str, Any] = {
        "written": written,
        "errors": errors,
        "timestamp": now.isoformat(),
        "regions_polled": regions,
        "regions_skipped_not_enabled": regions_skipped_not_enabled,
        "regions_enablement_unknown": regions_enablement_unknown,
        "sps": {
            **sps_counters,
            "pools": len(relevant_pools),
            "target_capacities": [capacity for capacity, _field in target_capacities],
            "unpooled_watch_types": len(unpooled_watch_types),
        },
    }
    logger.info(
        "capacity poll complete: written=%d errors=%d sps_requests=%d sps_received=%d/%d "
        "sps_missing=%d config_limit_refusals=%d regions_skipped=%d "
        "regions_enablement_unknown=%d",
        written,
        errors,
        summary["sps"]["requests_issued"],
        summary["sps"]["combinations_received"],
        summary["sps"]["combinations_expected"],
        summary["sps"]["combinations_missing_after_retry"],
        summary["sps"]["config_limit_refusals"],
        len(regions_skipped_not_enabled),
        len(regions_enablement_unknown),
    )
    return summary
