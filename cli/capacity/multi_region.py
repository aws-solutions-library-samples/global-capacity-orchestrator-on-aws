"""Multi-region capacity checking and weighted scoring."""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.exceptions import ClientError

from cli.config import GCOConfig, get_config

from .checker import CapacityChecker

logger = logging.getLogger(__name__)

_TELEMETRY_MISSING_SIGNAL_PENALTY = 1000.0
_SCORED_TELEMETRY_SIGNALS = frozenset({"queue", "gpu"})


def _missing_scored_signal_count(capacity: RegionCapacity) -> int:
    """Return the number of unavailable signals used for placement scoring."""
    return len(_SCORED_TELEMETRY_SIGNALS.intersection(capacity.unavailable_signals))


@dataclass
class RegionCapacity:
    """Capacity information for a region."""

    region: str
    queue_depth: int = 0
    pending_jobs: int = 0
    running_jobs: int = 0
    gpu_utilization: float = 0.0
    cpu_utilization: float = 0.0
    available_gpus: int = 0
    total_gpus: int = 0
    avg_wait_time_seconds: int = 0
    recommendation_score: float = 0.0
    telemetry_status: str = "unknown"
    unavailable_signals: list[str] = field(default_factory=list)
    telemetry_errors: list[str] = field(default_factory=list)


class MultiRegionCapacityChecker:
    """
    Checks capacity across multiple GCO regions.

    Provides:
    - Multi-region capacity overview
    - Intelligent region recommendation
    - Queue depth analysis
    - Resource utilization metrics
    """

    def __init__(self, config: GCOConfig | None = None):
        self.config = config or get_config()
        self._session = boto3.Session()
        # Errors from the most recent get_all_regions_capacity() sweep, so an
        # empty result can be distinguished as "checks failed" vs "no regions".
        self._last_region_errors: list[str] = []

    def get_region_capacity(self, region: str) -> RegionCapacity:
        """Get capacity information while retaining telemetry uncertainty."""
        from cli.aws_client import get_aws_client

        aws_client = get_aws_client(self.config)
        stack = aws_client.get_regional_stack(region)
        capacity = RegionCapacity(region=region)

        if not stack:
            capacity.telemetry_status = "unavailable"
            capacity.unavailable_signals = ["queue", "gpu", "cpu"]
            capacity.telemetry_errors = ["Regional stack was not found"]
            capacity.recommendation_score = (
                len(_SCORED_TELEMETRY_SIGNALS) * _TELEMETRY_MISSING_SIGNAL_PENALTY
            )
            return capacity

        available = {"queue": False, "gpu": False, "cpu": False}

        # Get queue depth from SQS.
        try:
            cfn = self._session.client("cloudformation", region_name=region)
            response = cfn.describe_stacks(StackName=stack.stack_name)
            outputs = {
                output["OutputKey"]: output["OutputValue"]
                for output in response["Stacks"][0].get("Outputs", [])
            }

            queue_url = outputs.get("JobQueueUrl")
            if queue_url:
                sqs = self._session.client("sqs", region_name=region)
                attrs = sqs.get_queue_attributes(
                    QueueUrl=queue_url,
                    AttributeNames=[
                        "ApproximateNumberOfMessages",
                        "ApproximateNumberOfMessagesNotVisible",
                    ],
                )["Attributes"]
                capacity.queue_depth = int(attrs.get("ApproximateNumberOfMessages", 0))
                capacity.running_jobs = int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0))
                available["queue"] = True
            else:
                capacity.telemetry_errors.append("Job queue URL was not present in stack outputs")
        except ClientError as e:
            logger.debug("Failed to get queue metrics for %s: %s", region, e)
            capacity.telemetry_errors.append(f"Queue telemetry failed: {e}")
        except Exception as e:
            logger.warning("Unexpected error getting queue metrics for %s: %s", region, e)
            capacity.telemetry_errors.append(f"Queue telemetry failed: {e}")

        # Query GPU and CPU independently so one failed metric does not erase
        # the other useful signal.
        try:
            cloudwatch = self._session.client("cloudwatch", region_name=region)
        except Exception as e:
            logger.warning("Failed to create CloudWatch client for %s: %s", region, e)
            capacity.telemetry_errors.append(f"CloudWatch telemetry failed: {e}")
        else:
            metric_specs = (
                ("gpu", "node_gpu_utilization", "gpu_utilization"),
                ("cpu", "node_cpu_utilization", "cpu_utilization"),
            )
            for signal, metric_name, attribute in metric_specs:
                try:
                    response = cloudwatch.get_metric_statistics(
                        Namespace="ContainerInsights",
                        MetricName=metric_name,
                        Dimensions=[{"Name": "ClusterName", "Value": stack.cluster_name}],
                        StartTime=datetime.now(UTC) - timedelta(minutes=5),
                        EndTime=datetime.now(UTC),
                        Period=300,
                        Statistics=["Average"],
                    )
                    datapoints = response.get("Datapoints", [])
                    if datapoints:
                        setattr(capacity, attribute, datapoints[0]["Average"])
                        available[signal] = True
                    else:
                        capacity.telemetry_errors.append(
                            f"{signal.upper()} telemetry returned no datapoints"
                        )
                except ClientError as e:
                    logger.debug("Failed to get %s metrics for %s: %s", signal, region, e)
                    capacity.telemetry_errors.append(f"{signal.upper()} telemetry failed: {e}")
                except Exception as e:
                    logger.warning(
                        "Unexpected error getting %s metrics for %s: %s", signal, region, e
                    )
                    capacity.telemetry_errors.append(f"{signal.upper()} telemetry failed: {e}")

        capacity.unavailable_signals = [
            signal for signal, is_available in available.items() if not is_available
        ]
        if not capacity.unavailable_signals:
            capacity.telemetry_status = "complete"
        elif len(capacity.unavailable_signals) == len(available):
            capacity.telemetry_status = "unavailable"
        else:
            capacity.telemetry_status = "partial"

        base_score = (
            capacity.queue_depth * 10 + capacity.gpu_utilization + capacity.running_jobs * 5
        )
        missing_scored_signals = _SCORED_TELEMETRY_SIGNALS.intersection(
            capacity.unavailable_signals
        )
        capacity.recommendation_score = base_score + (
            len(missing_scored_signals) * _TELEMETRY_MISSING_SIGNAL_PENALTY
        )
        return capacity

    def get_all_regions_capacity(self) -> list[RegionCapacity]:
        """Get capacity information for all deployed regions."""
        from cli.aws_client import get_aws_client

        self._last_region_errors = []
        try:
            aws_client = get_aws_client(self.config)
            stacks = aws_client.discover_regional_stacks()
        except Exception as e:
            logger.warning("Failed to discover regional stacks: %s", e)
            self._last_region_errors.append(f"region discovery: {e}")
            return []

        capacities = []
        for region in stacks:
            try:
                capacity = self.get_region_capacity(region)
                capacities.append(capacity)
            except Exception as e:
                logger.warning("Failed to get capacity for region %s: %s", region, e)
                self._last_region_errors.append(f"{region}: {e}")
                continue

        return capacities

    def recommend_region_for_job(
        self,
        gpu_required: bool = False,
        min_gpus: int = 0,
        instance_type: str | None = None,
        gpu_count: int = 0,
    ) -> dict[str, Any]:
        """
        Recommend the optimal region for job placement.

        When instance_type is provided, uses weighted multi-signal scoring that
        combines spot placement scores, pricing, queue depth, GPU utilization,
        and running job counts. Falls back to simple scoring when instance_type
        is not specified.

        Args:
            gpu_required: Whether the job requires GPUs
            min_gpus: Minimum number of GPUs required
            instance_type: Specific instance type for workload-aware scoring
            gpu_count: Number of GPUs required

        Returns:
            Dictionary with recommended region and justification
        """
        capacities = self.get_all_regions_capacity()

        if not capacities:
            conservative_score = len(_SCORED_TELEMETRY_SIGNALS) * _TELEMETRY_MISSING_SIGNAL_PENALTY
            if self._last_region_errors:
                # The emptiness is due to underlying AWS failures, not an absence
                # of configured regions — surface the real error instead of a
                # benign "no capacity" message that masks it.
                details = "; ".join(self._last_region_errors)
                return {
                    "region": self.config.default_region,
                    "reason": f"Capacity checks failed for all regions: {details}",
                    "score": conservative_score,
                    "error": details,
                    "telemetry_status": "unavailable",
                }
            return {
                "region": self.config.default_region,
                "reason": "No capacity data available, using default region",
                "score": conservative_score,
                "telemetry_status": "unavailable",
            }

        # If every region lacks cluster telemetry, prefer the configured
        # default as a deterministic fallback rather than treating zero-valued
        # failed measurements as evidence of ideal capacity.
        if all(cap.telemetry_status == "unavailable" for cap in capacities):
            capacities = sorted(
                capacities,
                key=lambda cap: (
                    cap.region != self.config.default_region,
                    cap.recommendation_score,
                ),
            )

        # When instance_type is provided, use weighted scoring with capacity data
        if instance_type:
            return self._weighted_recommend(capacities, instance_type, gpu_count or min_gpus)

        # Fallback: simple scoring (existing behavior)
        return self._simple_recommend(capacities)

    def _simple_recommend(self, capacities: list[RegionCapacity]) -> dict[str, Any]:
        """Recommend observed capacity before comparing numeric load scores."""
        all_unavailable = all(cap.telemetry_status == "unavailable" for cap in capacities)
        sorted_capacities = sorted(
            capacities,
            key=lambda cap: (
                cap.region != self.config.default_region if all_unavailable else False,
                _missing_scored_signal_count(cap),
                cap.recommendation_score,
                cap.region,
            ),
        )
        best = sorted_capacities[0]

        reasons = []
        if best.telemetry_errors:
            if best.telemetry_status == "unavailable":
                reasons.append("capacity telemetry unavailable; using conservative fallback")
            else:
                reasons.append("capacity telemetry is partial")

        if "queue" not in best.unavailable_signals:
            if best.queue_depth == 0:
                reasons.append("empty queue")
            elif best.queue_depth < 5:
                reasons.append(f"low queue depth ({best.queue_depth})")

        if "gpu" not in best.unavailable_signals:
            if best.gpu_utilization < 50:
                reasons.append(f"{100 - best.gpu_utilization:.0f}% GPU available")
            elif best.gpu_utilization < 80:
                reasons.append(f"moderate GPU utilization ({best.gpu_utilization:.0f}%)")

        if "queue" not in best.unavailable_signals:
            if best.running_jobs == 0:
                reasons.append("no running jobs")
            elif best.running_jobs < 5:
                reasons.append(f"few running jobs ({best.running_jobs})")

        reason = ", ".join(reasons) if reasons else "best overall capacity"

        return {
            "region": best.region,
            "reason": reason,
            "score": best.recommendation_score,
            "queue_depth": best.queue_depth,
            "gpu_utilization": best.gpu_utilization,
            "running_jobs": best.running_jobs,
            "telemetry_status": best.telemetry_status,
            "telemetry_errors": best.telemetry_errors or None,
            "all_regions": [
                {
                    "region": c.region,
                    "score": c.recommendation_score,
                    "queue_depth": c.queue_depth,
                    "gpu_utilization": c.gpu_utilization,
                    "telemetry_status": c.telemetry_status,
                }
                for c in sorted_capacities
            ],
        }

    def _weighted_recommend(
        self,
        capacities: list[RegionCapacity],
        instance_type: str,
        gpu_count: int = 0,
    ) -> dict[str, Any]:
        """
        Workload-aware recommendation using weighted multi-signal scoring.

        Gathers per-region capacity data for the specific instance type and
        combines it with cluster metrics using weighted scoring.
        """
        capacity_checker = CapacityChecker(self.config)

        scored_regions: list[dict[str, Any]] = []

        for cap in capacities:
            region = cap.region

            # Gather instance-specific signals for this region
            spot_score = 0.0
            spot_price_ratio = 1.0  # spot/on-demand ratio (lower = better savings)

            try:
                placement_scores = capacity_checker.get_spot_placement_score(
                    instance_type, region, target_capacity=max(1, gpu_count)
                )
                if placement_scores:
                    spot_score = placement_scores.get("regional", 0) / 10.0  # Normalize to 0-1
            except Exception as e:
                logger.debug(
                    "Failed to get spot placement score for %s in %s: %s", instance_type, region, e
                )

            try:
                spot_prices = capacity_checker.get_spot_price_history(instance_type, region)
                on_demand_price = capacity_checker.get_on_demand_price(instance_type, region)

                if spot_prices and on_demand_price and on_demand_price > 0:
                    avg_spot = statistics.mean(sp.current_price for sp in spot_prices)
                    spot_price_ratio = avg_spot / on_demand_price
            except Exception as e:
                logger.debug(
                    "Failed to get spot pricing for %s in %s: %s", instance_type, region, e
                )

            # Capacity Block trend — compares near-term vs far-term offering
            # density to detect whether AWS is adding or consuming capacity
            # in this region for the requested instance type.
            try:
                cb_trend = capacity_checker.get_capacity_block_trend(instance_type, region)
            except Exception:
                cb_trend = 0.0

            weighted_score = compute_weighted_score(
                spot_placement_score=spot_score,
                spot_price_ratio=spot_price_ratio,
                queue_depth=cap.queue_depth,
                gpu_utilization=cap.gpu_utilization,
                running_jobs=cap.running_jobs,
                capacity_block_trend=cb_trend,
            )
            missing_scored_signals = _missing_scored_signal_count(cap)

            scored_regions.append(
                {
                    "region": region,
                    "score": weighted_score,
                    "queue_depth": cap.queue_depth,
                    "gpu_utilization": cap.gpu_utilization,
                    "running_jobs": cap.running_jobs,
                    "spot_placement_score": spot_score,
                    "spot_price_ratio": spot_price_ratio,
                    "capacity_block_trend": cb_trend,
                    "telemetry_status": cap.telemetry_status,
                    "telemetry_errors": cap.telemetry_errors or None,
                    "unavailable_scored_signals": missing_scored_signals,
                }
            )

        scored_regions.sort(
            key=lambda item: (
                item["unavailable_scored_signals"],
                item["score"],
                item["region"],
            )
        )
        best = scored_regions[0]

        # Build justification from the signals
        reasons = []
        if best.get("telemetry_errors"):
            if best["telemetry_status"] == "unavailable":
                reasons.append("cluster telemetry unavailable")
            else:
                reasons.append("cluster telemetry is partial")

        if best["spot_placement_score"] >= 0.7:
            reasons.append(f"high spot availability ({best['spot_placement_score']:.0%})")
        elif best["spot_placement_score"] >= 0.4:
            reasons.append(f"moderate spot availability ({best['spot_placement_score']:.0%})")

        if best["spot_price_ratio"] < 0.5:
            reasons.append(f"good spot savings ({1 - best['spot_price_ratio']:.0%} off on-demand)")

        if "queue" not in next(
            cap.unavailable_signals for cap in capacities if cap.region == best["region"]
        ):
            if best["queue_depth"] == 0:
                reasons.append("empty queue")
            elif best["queue_depth"] < 5:
                reasons.append(f"low queue depth ({best['queue_depth']})")

        if (
            "gpu"
            not in next(
                cap.unavailable_signals for cap in capacities if cap.region == best["region"]
            )
            and best["gpu_utilization"] < 50
        ):
            reasons.append(f"{100 - best['gpu_utilization']:.0f}% GPU available")

        if "queue" not in next(
            cap.unavailable_signals for cap in capacities if cap.region == best["region"]
        ):
            if best["running_jobs"] == 0:
                reasons.append("no running jobs")
            elif best["running_jobs"] < 5:
                reasons.append(f"few running jobs ({best['running_jobs']})")

        if best.get("capacity_block_trend", 0) > 0.2:
            reasons.append("capacity block availability trending up")
        elif best.get("capacity_block_trend", 0) < -0.2:
            reasons.append("capacity block availability trending down")

        reason = ", ".join(reasons) if reasons else f"best weighted score for {instance_type}"

        return {
            "region": best["region"],
            "reason": reason,
            "score": best["score"],
            "queue_depth": best["queue_depth"],
            "gpu_utilization": best["gpu_utilization"],
            "running_jobs": best["running_jobs"],
            "instance_type": instance_type,
            "scoring_method": "weighted",
            "telemetry_status": best["telemetry_status"],
            "telemetry_errors": best["telemetry_errors"],
            "all_regions": scored_regions,
        }


def compute_price_trend(prices: list[float]) -> dict[str, Any]:
    """
    Compute a linear regression trend over a price time series.

    Prices are assumed to be ordered most-recent-first (as returned by
    the EC2 spot price history API). The series is reversed internally
    so the slope represents change over time (positive = prices rising).

    Args:
        prices: List of price points, most recent first.

    Returns:
        Dict with:
          slope: price change per sample period (positive = rising)
          normalized_slope: slope / mean_price (scale-independent, -1 to 1 clamped)
          price_changes: number of distinct price transitions (proxy for volatility)
          direction: "rising", "falling", or "stable"
    """
    if len(prices) < 2:
        return {
            "slope": 0.0,
            "normalized_slope": 0.0,
            "price_changes": 0,
            "direction": "stable",
        }

    # Reverse so index 0 = oldest, index N = newest
    series = list(reversed(prices))
    n = len(series)

    # Count distinct price transitions (proxy for interruption frequency)
    price_changes = sum(1 for i in range(1, n) if series[i] != series[i - 1])

    # Linear regression: slope of price over time
    x_mean = (n - 1) / 2.0
    y_mean = statistics.mean(series)

    numerator = sum((i - x_mean) * (series[i] - y_mean) for i in range(n))
    denominator = sum((i - x_mean) ** 2 for i in range(n))

    if denominator == 0 or y_mean == 0:
        return {
            "slope": 0.0,
            "normalized_slope": 0.0,
            "price_changes": price_changes,
            "direction": "stable",
        }

    slope = numerator / denominator
    normalized = max(-1.0, min(1.0, slope / y_mean))

    if normalized > 0.05:
        direction = "rising"
    elif normalized < -0.05:
        direction = "falling"
    else:
        direction = "stable"

    return {
        "slope": round(slope, 6),
        "normalized_slope": round(normalized, 4),
        "price_changes": price_changes,
        "direction": direction,
    }


def compute_weighted_score(
    spot_placement_score: float = 0.0,
    spot_price_ratio: float = 1.0,
    queue_depth: int = 0,
    gpu_utilization: float = 0.0,
    running_jobs: int = 0,
    capacity_block_trend: float = 0.0,
    *,
    weights: dict[str, float] | None = None,
) -> float:
    """
    Compute a weighted recommendation score for a region. Lower is better.

    Combines multiple capacity signals into a single score using configurable
    weights. Each signal is normalized to 0-1 where 0 is best, then weighted.

    Args:
        spot_placement_score: Normalized spot placement score (0-1, higher = better availability)
        spot_price_ratio: Spot price / on-demand price (0-1, lower = better savings)
        queue_depth: Number of pending jobs in the region's queue
        gpu_utilization: GPU utilization percentage (0-100)
        running_jobs: Number of currently running jobs
        capacity_block_trend: Trend of capacity block offerings over a 26-week window
            (-1 to 1). Positive means capacity is growing (regression slope positive),
            negative means shrinking. Derived from linear regression over weekly
            offering counts from the describe-capacity-block-offerings API.
        weights: Optional custom weights dict. Keys: spot_placement, spot_price,
                 queue_depth, gpu_utilization, running_jobs, capacity_blocks.
                 Values should sum to 1.0.

    Returns:
        Weighted score (lower is better, range roughly 0-1)
    """
    w = weights or {
        "spot_placement": 0.25,
        "spot_price": 0.20,
        "queue_depth": 0.20,
        "gpu_utilization": 0.15,
        "running_jobs": 0.10,
        "capacity_blocks": 0.10,
    }

    # Normalize each signal to 0-1 where 0 is best
    # Spot placement: invert (high score = good, so 1 - score = low = good)
    norm_spot = 1.0 - min(max(spot_placement_score, 0.0), 1.0)

    # Spot price ratio: already 0-1 where lower is better
    norm_price = min(max(spot_price_ratio, 0.0), 1.0)

    # Queue depth: normalize with diminishing returns (0 = best)
    # Use tanh-like curve: depth / (depth + k) where k controls sensitivity
    norm_queue = queue_depth / (queue_depth + 10.0) if queue_depth >= 0 else 0.0

    # GPU utilization: normalize 0-100 to 0-1
    norm_gpu = min(max(gpu_utilization, 0.0), 100.0) / 100.0

    # Running jobs: normalize with diminishing returns
    norm_jobs = running_jobs / (running_jobs + 20.0) if running_jobs >= 0 else 0.0

    # Capacity block trend: invert and shift from [-1,1] to [0,1]
    # trend +1 (growing) → 0.0 (best), trend -1 (shrinking) → 1.0 (worst)
    clamped_trend = min(max(capacity_block_trend, -1.0), 1.0)
    norm_blocks = (1.0 - clamped_trend) / 2.0

    score = (
        w["spot_placement"] * norm_spot
        + w["spot_price"] * norm_price
        + w["queue_depth"] * norm_queue
        + w["gpu_utilization"] * norm_gpu
        + w["running_jobs"] * norm_jobs
        + w.get("capacity_blocks", 0) * norm_blocks
    )

    return round(score, 4)


def get_multi_region_capacity_checker(
    config: GCOConfig | None = None,
) -> MultiRegionCapacityChecker:
    """Get a configured multi-region capacity checker instance."""
    return MultiRegionCapacityChecker(config)
