"""Spot price gating for the central DynamoDB job queue.

A job submitted to the central queue may carry a spot price cap:
``spot_max_price`` (USD/hour) for ``spot_instance_type``. The regional queue
worker consults this gate before claiming such a job — while the instance
type's current spot price in the worker's region sits above the cap, the job
stays queued and is re-evaluated on every worker pass. The moment pricing
drops to or below the cap, dispatch proceeds normally.

Price lookups use ``ec2:DescribeSpotPriceHistory`` and take the *minimum*
current price across the region's Availability Zones — a capacity-flexible
job can land in whichever zone currently clears its cap. Results are cached
briefly so a busy queue never hammers the EC2 API, and lookup failures fail
open at the per-pass level by deferring only the affected job (never by
dispatching above the cap).
"""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

#: EC2 instance type shape (``g5.xlarge``, ``p6-b200.48xlarge``, ``trn2.3xlarge``).
INSTANCE_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,29}\.[a-z0-9]{1,20}$")

#: Spot price caps accepted at submission time (USD/hour).
MIN_SPOT_PRICE = 0.0001
MAX_SPOT_PRICE = 1_000.0

_PRICE_CACHE_TTL_SECONDS = 60.0
_PRICE_LOOKBACK_HOURS = 4

#: Minimum seconds between persisted gate observations per job. In-memory
#: evaluation still happens every pass; only the DynamoDB write is throttled.
OBSERVATION_WRITE_INTERVAL_SECONDS = 60.0


def validate_spot_gate_fields(max_price: float | None, instance_type: str | None) -> str | None:
    """Validate the submission-time gate pair; return an error or ``None``.

    The two fields are all-or-nothing: a cap without an instance type is
    unenforceable, and an instance type without a cap is meaningless.
    """
    if max_price is None and instance_type is None:
        return None
    if max_price is None or instance_type is None:
        return "max_spot_price and spot_instance_type must be provided together"
    if not (MIN_SPOT_PRICE <= max_price <= MAX_SPOT_PRICE):
        return f"max_spot_price must be between {MIN_SPOT_PRICE} and {MAX_SPOT_PRICE} USD/hour"
    if not INSTANCE_TYPE_PATTERN.fullmatch(instance_type):
        return "spot_instance_type is not a valid EC2 instance type"
    return None


@dataclass(frozen=True)
class SpotGateDecision:
    """Outcome of evaluating one job's spot price gate."""

    gated: bool
    instance_type: str
    max_price: float
    observed_price: float | None
    reason: str


class SpotPriceGate:
    """TTL-cached regional spot price lookups plus per-job gate evaluation."""

    def __init__(
        self,
        region: str,
        *,
        ec2_client: Any | None = None,
        cache_ttl_seconds: float = _PRICE_CACHE_TTL_SECONDS,
    ) -> None:
        self.region = region
        self.cache_ttl_seconds = float(cache_ttl_seconds)
        self._ec2 = ec2_client
        self._cache: dict[str, tuple[float, float | None]] = {}

    def _client(self) -> Any:
        if self._ec2 is None:
            import boto3
            from botocore.config import Config

            self._ec2 = boto3.client(
                "ec2",
                region_name=self.region,
                config=Config(
                    connect_timeout=3,
                    read_timeout=10,
                    retries={"max_attempts": 2, "mode": "standard"},
                ),
            )
        return self._ec2

    def current_min_spot_price(self, instance_type: str) -> float | None:
        """Return the lowest current spot price across AZs, or ``None``.

        ``None`` means the price could not be determined (no offerings in the
        region, API error, malformed response). Callers treat ``None`` as
        "defer the job" — an unknown price must never dispatch a price-capped
        job.
        """
        cached = self._cache.get(instance_type)
        now = time.monotonic()
        if cached is not None and now - cached[0] < self.cache_ttl_seconds:
            return cached[1]

        price = self._fetch_min_spot_price(instance_type)
        self._cache[instance_type] = (now, price)
        return price

    def _fetch_min_spot_price(self, instance_type: str) -> float | None:
        end = datetime.now(UTC)
        try:
            response = self._client().describe_spot_price_history(
                InstanceTypes=[instance_type],
                ProductDescriptions=["Linux/UNIX"],
                StartTime=end - timedelta(hours=_PRICE_LOOKBACK_HOURS),
                EndTime=end,
            )
        except Exception as exc:  # noqa: BLE001 - lookup failures defer, never dispatch
            logger.warning(
                "Spot price lookup failed for %s in %s: %s",
                instance_type,
                self.region,
                exc,
            )
            return None

        # DescribeSpotPriceHistory returns newest-first per AZ; keep each
        # AZ's most recent price and take the minimum across AZs.
        latest_by_az: dict[str, float] = {}
        for entry in response.get("SpotPriceHistory", []):
            az = str(entry.get("AvailabilityZone") or "")
            if not az or az in latest_by_az:
                continue
            try:
                latest_by_az[az] = float(entry["SpotPrice"])
            except KeyError, TypeError, ValueError:
                continue
        if not latest_by_az:
            return None
        return min(latest_by_az.values())

    def evaluate(self, job: dict[str, Any]) -> SpotGateDecision | None:
        """Evaluate one queue record's gate; ``None`` means the job is ungated.

        Malformed gate fields on a stored record gate the job closed (with a
        descriptive reason) rather than dispatching a job whose cap cannot be
        honored.
        """
        raw_price = job.get("spot_max_price")
        instance_type = job.get("spot_instance_type")
        if raw_price is None and instance_type is None:
            return None
        try:
            max_price = float(str(raw_price))
        except TypeError, ValueError:
            max_price = float("nan")
        # A cap must be a finite number: NaN means the stored field is
        # unparseable, and an infinite cap would wave every price through —
        # both gate closed rather than dispatching a job whose cap cannot be
        # honored.
        if not isinstance(instance_type, str) or not instance_type or not math.isfinite(max_price):
            return SpotGateDecision(
                gated=True,
                instance_type=str(instance_type or ""),
                max_price=0.0,
                observed_price=None,
                reason="spot gate fields are malformed; refusing to dispatch",
            )

        observed = self.current_min_spot_price(instance_type)
        if observed is None:
            return SpotGateDecision(
                gated=True,
                instance_type=instance_type,
                max_price=max_price,
                observed_price=None,
                reason=(
                    f"current spot price for {instance_type} in {self.region} "
                    "is unavailable; deferring"
                ),
            )
        if observed > max_price:
            return SpotGateDecision(
                gated=True,
                instance_type=instance_type,
                max_price=max_price,
                observed_price=observed,
                reason=(
                    f"spot price {observed:.4f} USD/h for {instance_type} in "
                    f"{self.region} is above the {max_price:.4f} USD/h cap"
                ),
            )
        return SpotGateDecision(
            gated=False,
            instance_type=instance_type,
            max_price=max_price,
            observed_price=observed,
            reason=(
                f"spot price {observed:.4f} USD/h for {instance_type} in "
                f"{self.region} clears the {max_price:.4f} USD/h cap"
            ),
        )


def should_persist_observation(job: dict[str, Any], now: datetime | None = None) -> bool:
    """Throttle DynamoDB gate-observation writes per job.

    Evaluation happens on every worker pass; persisting every observation
    would add one write per gated job per pass for no operator benefit. Only
    write when the record has no observation yet or the last one is older
    than :data:`OBSERVATION_WRITE_INTERVAL_SECONDS`.
    """
    checked_at = job.get("spot_gate_checked_at")
    if not checked_at:
        return True
    try:
        last = datetime.fromisoformat(str(checked_at))
    except ValueError:
        return True
    moment = now or datetime.now(UTC)
    return (moment - last).total_seconds() >= OBSERVATION_WRITE_INTERVAL_SECONDS
