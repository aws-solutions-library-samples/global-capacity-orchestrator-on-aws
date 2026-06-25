"""
Single-region EC2 capacity checker using real AWS signals.

This is the core capacity intelligence module (~1265 lines). It queries multiple
AWS APIs to build a comprehensive picture of GPU/accelerator availability in a
single region. The MultiRegionCapacityChecker in multi_region.py calls this for
each region in parallel.

Data Sources:
    - EC2 GetSpotPlacementScores: likelihood of getting spot capacity (1-10 score)
    - EC2 DescribeSpotPriceHistory: current and historical spot prices (7-day window)
    - EC2 DescribeInstanceTypes: vCPU, memory, GPU count/type/memory, EFA support
    - EC2 DescribeInstanceTypeOfferings: which instance types are available in the region
    - EC2 DescribeCapacityBlockOfferings: purchasable Capacity Blocks for ML workloads
    - EC2 PurchaseCapacityBlock: (optional) purchase a Capacity Block by offering ID

Key Classes:
    CapacityChecker: Main class. Instantiated with a region and optional GCOConfig.
        - check_capacity(instance_type) → CapacityEstimate
        - get_spot_prices(instance_type) → list[SpotPriceInfo]
        - get_instance_info(instance_type) → InstanceTypeInfo
        - check_capacity_blocks(instance_type, count, duration) → list[dict]

Output Models (defined in models.py):
    - CapacityEstimate: spot score, price, trend, on-demand price, instance specs
    - SpotPriceInfo: AZ, price, timestamp
    - InstanceTypeInfo: vCPU, memory, GPU count/type/memory, EFA, architecture

The GPU_INSTANCE_SPECS lookup table in models.py provides offline specs for common
GPU instances so the checker can return useful information even when the EC2 API
is unavailable or the instance type isn't offered in the region.
"""

from __future__ import annotations

import json
import logging
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from cli.config import GCOConfig, get_config

from . import blocks
from .models import (
    GPU_INSTANCE_SPECS,
    CapacityEstimate,
    InstanceTypeInfo,
    SpotPriceInfo,
)

logger = logging.getLogger(__name__)

# Capacity Block offering API error codes that mean "this instance type / region
# simply doesn't have Capacity Blocks" rather than a real failure — expected and
# handled quietly. ``Unsupported`` covers regions where the API isn't available.
_CB_EXPECTED_ERROR_CODES = frozenset(
    {"Unsupported", "InvalidParameterValue", "InvalidParameterCombination"}
)

# A conservative cap on the parallel fan-out so a wide region x duration sweep
# can't open an unbounded number of threads / sockets at once.
_MAX_SEARCH_WORKERS = 12


def _instance_desc(instance_type: str, gpu_count: int, gpu_type: str, total_gpu_mem: float) -> str:
    """Build a human-readable instance description."""
    if gpu_count > 0 and gpu_type:
        mem_str = f", {total_gpu_mem:.0f}GB" if total_gpu_mem else ""
        return f"{instance_type} ({gpu_count}x {gpu_type}{mem_str})"
    return instance_type


def _offering_fee(offering: dict[str, Any]) -> float:
    """Sort key: an offering's upfront fee in USD, with missing fees sorting last."""
    fee = offering.get("upfront_fee_usd")
    if fee is None:
        fee = blocks.parse_upfront_fee(offering.get("upfront_fee"))
    return fee if fee is not None else float("inf")


class CapacityChecker:
    """
    Checks EC2 capacity availability using real AWS capacity signals.

    Uses:
    - Spot Placement Score API for spot capacity estimates
    - EC2 describe-instance-type-offerings for regional availability
    - Spot price history for pricing trends
    - On-demand pricing API
    """

    def __init__(self, config: GCOConfig | None = None):
        self.config = config or get_config()
        self._session = boto3.Session()
        self._pricing_cache: dict[str, Any] = {}
        self._offerings_cache: dict[str, set[str]] = {}

    def get_instance_info(self, instance_type: str) -> InstanceTypeInfo | None:
        """Get information about an instance type."""
        if instance_type in GPU_INSTANCE_SPECS:
            return GPU_INSTANCE_SPECS[instance_type]

        # Try to get from EC2 API
        try:
            ec2 = self._session.client("ec2", region_name="us-east-1")
            response = ec2.describe_instance_types(InstanceTypes=[instance_type])

            if response["InstanceTypes"]:
                info = response["InstanceTypes"][0]
                vcpus = info["VCpuInfo"]["DefaultVCpus"]
                memory = info["MemoryInfo"]["SizeInMiB"] / 1024

                gpu_count = 0
                gpu_type = None
                gpu_memory = 0

                if "GpuInfo" in info:
                    gpus = info["GpuInfo"].get("Gpus", [])
                    if gpus:
                        gpu_count = gpus[0].get("Count", 0)
                        gpu_type = gpus[0].get("Name")
                        gpu_memory = gpus[0].get("MemoryInfo", {}).get("SizeInMiB", 0) / 1024

                arch = info["ProcessorInfo"]["SupportedArchitectures"][0]

                return InstanceTypeInfo(
                    instance_type=instance_type,
                    vcpus=vcpus,
                    memory_gib=memory,
                    gpu_count=gpu_count,
                    gpu_type=gpu_type,
                    gpu_memory_gib=gpu_memory,
                    architecture=arch,
                )
        except ClientError as e:
            logger.debug("Failed to describe instance type %s: %s", instance_type, e)
        except Exception as e:
            logger.warning("Unexpected error getting instance info for %s: %s", instance_type, e)

        return None

    def validate_instance_type(self, instance_type: str) -> dict[str, Any]:
        """Classify an instance type as valid / invalid, with friendly normalization.

        The offering APIs collapse "unknown instance type" and "valid type with
        zero offerings" into the same empty result. This method separates them so
        callers can tell a typo from genuine unavailability:

        * A type in ``GPU_INSTANCE_SPECS`` is valid and ``known`` without any AWS
          call.
        * Otherwise EC2 ``DescribeInstanceTypes`` is consulted; an
          ``InvalidInstanceType`` (or empty result) marks it invalid, while a
          transient API error leaves it valid-but-unverified with a note.
        * Friendly aliases are expanded (``p6-b200`` -> ``p6-b200.48xlarge``) and
          UltraServer-only families (``p6-b300``) are flagged invalid-for-
          ``InstanceType`` with guidance toward the UltraServer search flow.

        Returns a dict: ``requested``, ``instance_type`` (canonical), ``valid``,
        ``known``, ``note``, ``gpu_count``.
        """
        canonical, note = blocks.normalize_instance_type(instance_type)
        result: dict[str, Any] = {
            "requested": instance_type,
            "instance_type": canonical,
            "valid": True,
            "known": False,
            "note": note,
            "gpu_count": None,
        }

        # UltraServer-only families are real accelerators but not standalone EC2
        # instance types, so InstanceType-based searches can never resolve them.
        if (instance_type or "").strip().lower() in blocks.NON_STANDALONE_INSTANCE_NOTES:
            result["valid"] = False
            return result

        if canonical in GPU_INSTANCE_SPECS:
            spec = GPU_INSTANCE_SPECS[canonical]
            result["known"] = True
            result["gpu_count"] = spec.gpu_count
            return result

        # Not in the offline table — ask EC2, separating "invalid" from "API error".
        try:
            ec2 = self._session.client("ec2", region_name="us-east-1")
            response = ec2.describe_instance_types(InstanceTypes=[canonical])
            types = response.get("InstanceTypes", [])
            if not types:
                result["valid"] = False
                return result
            gpu_info = types[0].get("GpuInfo") or {}
            gpus = gpu_info.get("Gpus", [])
            result["gpu_count"] = gpus[0].get("Count", 0) if gpus else 0
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("InvalidInstanceType", "InvalidParameterValue"):
                result["valid"] = False
            else:
                logger.debug("Could not verify instance type %s: %s", canonical, e)
                result["note"] = note or f"Could not verify instance type via EC2 ({code or e})."
        except Exception as e:
            logger.debug("Unexpected error validating instance type %s: %s", canonical, e)
            result["note"] = note or f"Could not verify instance type: {e}"
        return result

    def check_instance_available_in_region(self, instance_type: str, region: str) -> bool:
        """Check if an instance type is offered in a region."""
        cache_key = f"{region}"
        if cache_key not in self._offerings_cache:
            try:
                ec2 = self._session.client("ec2", region_name=region)
                paginator = ec2.get_paginator("describe_instance_type_offerings")
                offerings = set()
                for page in paginator.paginate(LocationType="region"):
                    for offering in page["InstanceTypeOfferings"]:
                        offerings.add(offering["InstanceType"])
                self._offerings_cache[cache_key] = offerings
            except ClientError as e:
                logger.debug("Failed to check instance offerings in %s: %s", region, e)
                return False  # Assume unavailable if we can't check
            except Exception as e:
                logger.warning("Unexpected error checking offerings in %s: %s", region, e)
                return False

        return instance_type in self._offerings_cache.get(cache_key, set())

    def get_availability_zones(self, region: str) -> list[str]:
        """Get availability zones for a region."""
        try:
            ec2 = self._session.client("ec2", region_name=region)
            response = ec2.describe_availability_zones(
                Filters=[{"Name": "state", "Values": ["available"]}]
            )
            return [az["ZoneName"] for az in response["AvailabilityZones"]]
        except ClientError as e:
            logger.debug("Failed to get availability zones for %s: %s", region, e)
            return []
        except Exception as e:
            logger.warning("Unexpected error getting AZs for %s: %s", region, e)
            return []

    def get_az_coverage(self, instance_type: str, region: str) -> float | None:
        """Get the fraction of AZs in a region that offer this instance type.

        Returns a value between 0.0 and 1.0, or None if we can't determine it.
        Constrained instances are often available in fewer AZs.
        """
        try:
            ec2 = self._session.client("ec2", region_name=region)
            total_azs = self.get_availability_zones(region)
            if not total_azs:
                return None

            paginator = ec2.get_paginator("describe_instance_type_offerings")
            offering_azs = set()
            for page in paginator.paginate(
                LocationType="availability-zone",
                Filters=[{"Name": "instance-type", "Values": [instance_type]}],
            ):
                for offering in page["InstanceTypeOfferings"]:
                    offering_azs.add(offering["Location"])

            return len(offering_azs) / len(total_azs) if total_azs else None
        except Exception as e:
            logger.debug("Failed to get AZ coverage for %s in %s: %s", instance_type, region, e)
            return None

    def get_spot_placement_score(
        self, instance_type: str, region: str, target_capacity: int = 1
    ) -> dict[str, int]:
        """
        Get Spot Placement Score for an instance type.

        The Spot Placement Score (1-10) indicates the likelihood of getting
        spot capacity. Higher scores mean better availability.

        Returns:
            Dict mapping AZ to score (1-10), or empty if not available
        """
        try:
            ec2 = self._session.client("ec2", region_name=region)

            response = ec2.get_spot_placement_scores(
                InstanceTypes=[instance_type],
                TargetCapacity=target_capacity,
                TargetCapacityUnitType="units",
                RegionNames=[region],
                SingleAvailabilityZone=False,
            )

            scores = {}
            for recommendation in response.get("SpotPlacementScores", []):
                # Regional score
                if "AvailabilityZoneId" not in recommendation:
                    scores["regional"] = recommendation.get("Score", 0)
                else:
                    az_id = recommendation["AvailabilityZoneId"]
                    scores[az_id] = recommendation.get("Score", 0)

            return scores

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("InvalidParameterValue", "UnsupportedOperation"):
                return {}
            raise
        except Exception as e:
            logger.debug(
                "Failed to get spot placement scores for %s in %s: %s", instance_type, region, e
            )
            return {}

    def get_spot_price_history(
        self, instance_type: str, region: str, days: int = 7
    ) -> list[SpotPriceInfo]:
        """Get spot price history for an instance type."""
        ec2 = self._session.client("ec2", region_name=region)

        end_time = datetime.now(UTC)
        start_time = end_time - timedelta(days=days)

        try:
            response = ec2.describe_spot_price_history(
                InstanceTypes=[instance_type],
                ProductDescriptions=["Linux/UNIX"],
                StartTime=start_time,
                EndTime=end_time,
            )

            # Group by availability zone
            az_prices: dict[str, list[float]] = {}
            for item in response["SpotPriceHistory"]:
                az = item["AvailabilityZone"]
                price = float(item["SpotPrice"])
                if az not in az_prices:
                    az_prices[az] = []
                az_prices[az].append(price)

            results = []
            for az, prices in az_prices.items():
                if not prices:
                    continue

                current = prices[0]
                avg = statistics.mean(prices)
                min_price = min(prices)
                max_price = max(prices)

                if avg > 0:
                    std_dev = statistics.stdev(prices) if len(prices) > 1 else 0
                    cv = std_dev / avg
                    stability = max(0, 1 - cv)
                else:
                    stability = 0

                results.append(
                    SpotPriceInfo(
                        instance_type=instance_type,
                        availability_zone=az,
                        current_price=current,
                        avg_price_7d=avg,
                        min_price_7d=min_price,
                        max_price_7d=max_price,
                        price_stability=stability,
                    )
                )

            return results

        except ClientError as e:
            if "InvalidParameterValue" in str(e):
                return []
            raise

    def get_on_demand_price(self, instance_type: str, region: str) -> float | None:
        """Get on-demand price for an instance type."""
        cache_key = f"{instance_type}:{region}"
        if cache_key in self._pricing_cache:
            cached_value = self._pricing_cache[cache_key]
            return float(cached_value) if cached_value is not None else None

        try:
            pricing = self._session.client("pricing", region_name="us-east-1")

            region_names = {
                "us-east-1": "US East (N. Virginia)",
                "us-east-2": "US East (Ohio)",
                "us-west-1": "US West (N. California)",
                "us-west-2": "US West (Oregon)",
                "eu-west-1": "EU (Ireland)",
                "eu-west-2": "EU (London)",
                "eu-central-1": "EU (Frankfurt)",
                "ap-northeast-1": "Asia Pacific (Tokyo)",
                "ap-southeast-1": "Asia Pacific (Singapore)",
                "ap-southeast-2": "Asia Pacific (Sydney)",
            }

            location = region_names.get(region, region)

            response = pricing.get_products(
                ServiceCode="AmazonEC2",
                Filters=[
                    {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
                    {"Type": "TERM_MATCH", "Field": "location", "Value": location},
                    {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
                    {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
                    {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
                    {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
                ],
                MaxResults=1,
            )

            if response["PriceList"]:
                price_data = json.loads(response["PriceList"][0])
                terms = price_data.get("terms", {}).get("OnDemand", {})
                for term in terms.values():
                    for price_dim in term.get("priceDimensions", {}).values():
                        price = float(price_dim["pricePerUnit"]["USD"])
                        self._pricing_cache[cache_key] = price
                        return price

        except ClientError as e:
            logger.debug("Failed to get on-demand price for %s in %s: %s", instance_type, region, e)
        except Exception as e:
            logger.warning(
                "Unexpected error getting pricing for %s in %s: %s", instance_type, region, e
            )

        return None

    def estimate_capacity(
        self, instance_type: str, region: str, capacity_type: str = "both"
    ) -> list[CapacityEstimate]:
        """
        Estimate capacity availability using real AWS signals.

        Args:
            instance_type: EC2 instance type
            region: AWS region
            capacity_type: "spot", "on-demand", or "both"

        Returns:
            List of CapacityEstimate objects
        """
        estimates = []

        # Check if instance type is available in region
        if not self.check_instance_available_in_region(instance_type, region):
            return [
                CapacityEstimate(
                    instance_type=instance_type,
                    region=region,
                    availability_zone=None,
                    capacity_type="both",
                    availability="unavailable",
                    confidence=1.0,
                    recommendation=f"{instance_type} is not available in {region}",
                    details={"reason": "Instance type not offered in region"},
                )
            ]

        instance_info = self.get_instance_info(instance_type)

        if capacity_type in ("spot", "both"):
            spot_estimates = self._estimate_spot_capacity(instance_type, region, instance_info)
            estimates.extend(spot_estimates)

        if capacity_type in ("on-demand", "both"):
            # Pass spot placement scores to on-demand estimator as a scarcity signal
            spot_scores = (
                self.get_spot_placement_score(instance_type, region)
                if capacity_type == "on-demand"
                else {}
            )
            # If we already fetched spot estimates, extract the scores from them
            if spot_estimates := [e for e in estimates if e.capacity_type == "spot"]:
                spot_scores = {
                    e.availability_zone or "unknown": e.details.get("spot_placement_score", 0)
                    for e in spot_estimates
                    if e.details.get("spot_placement_score") is not None
                }
            # Also gather spot price data for price-ratio signal
            spot_prices = self.get_spot_price_history(instance_type, region)
            od_estimate = self._estimate_on_demand_capacity(
                instance_type, region, instance_info, spot_scores, spot_prices
            )
            if od_estimate:
                estimates.append(od_estimate)

        return estimates

    def _estimate_spot_capacity(
        self, instance_type: str, region: str, instance_info: InstanceTypeInfo | None
    ) -> list[CapacityEstimate]:
        """Estimate spot capacity using Spot Placement Score and price history."""
        estimates = []

        # Get Spot Placement Score (primary signal)
        placement_scores = self.get_spot_placement_score(instance_type, region)

        # Get spot prices for pricing info
        spot_prices = self.get_spot_price_history(instance_type, region)
        price_by_az = {sp.availability_zone: sp for sp in spot_prices}

        on_demand_price = self.get_on_demand_price(instance_type, region)

        # Get AZs in the region
        azs = self.get_availability_zones(region)

        if placement_scores:
            # Use Spot Placement Score as primary signal
            regional_score = placement_scores.get("regional", 0)

            for az in azs:
                # Try to get AZ-specific score, fall back to regional
                az_id = az  # Note: might need to map zone name to zone ID
                score = placement_scores.get(az_id, regional_score)

                # Convert score (1-10) to availability
                if score >= 8:
                    availability = "high"
                    recommendation = "Excellent spot availability"
                elif score >= 5:
                    availability = "medium"
                    recommendation = "Good spot availability, some interruption risk"
                elif score >= 3:
                    availability = "low"
                    recommendation = "Limited spot capacity, consider alternatives"
                else:
                    availability = "low"
                    recommendation = "Very limited spot capacity"

                spot_info = price_by_az.get(az)
                price = spot_info.current_price if spot_info else None

                details: dict[str, Any] = {
                    "spot_placement_score": score,
                    "score_interpretation": f"{score}/10",
                }

                if spot_info:
                    details["current_price"] = spot_info.current_price
                    details["avg_price_7d"] = spot_info.avg_price_7d
                    details["price_stability"] = f"{spot_info.price_stability:.2f}"

                if on_demand_price and price:
                    savings = (1 - price / on_demand_price) * 100
                    details["savings_vs_on_demand"] = f"{savings:.1f}%"
                    details["on_demand_price"] = on_demand_price

                estimates.append(
                    CapacityEstimate(
                        instance_type=instance_type,
                        region=region,
                        availability_zone=az,
                        capacity_type="spot",
                        availability=availability,
                        confidence=0.85,  # Spot Placement Score is reliable
                        price_per_hour=price,
                        recommendation=recommendation,
                        details=details,
                    )
                )

        elif spot_prices:
            # Fall back to price-based estimation if no placement score
            for spot_info in spot_prices:
                # Use price stability as a proxy (less reliable)
                if spot_info.price_stability > 0.8:
                    availability = "medium"
                    recommendation = "Spot prices stable, likely available"
                elif spot_info.price_stability > 0.5:
                    availability = "low"
                    recommendation = "Spot prices volatile, capacity uncertain"
                else:
                    availability = "low"
                    recommendation = "High price volatility, limited capacity likely"

                details = {
                    "current_price": spot_info.current_price,
                    "avg_price_7d": spot_info.avg_price_7d,
                    "price_stability": f"{spot_info.price_stability:.2f}",
                    "note": "Estimate based on price history (Spot Placement Score unavailable)",
                }

                if on_demand_price:
                    savings = (1 - spot_info.current_price / on_demand_price) * 100
                    details["savings_vs_on_demand"] = f"{savings:.1f}%"

                estimates.append(
                    CapacityEstimate(
                        instance_type=instance_type,
                        region=region,
                        availability_zone=spot_info.availability_zone,
                        capacity_type="spot",
                        availability=availability,
                        confidence=0.5,  # Lower confidence without placement score
                        price_per_hour=spot_info.current_price,
                        recommendation=recommendation,
                        details=details,
                    )
                )

        if not estimates:
            estimates.append(
                CapacityEstimate(
                    instance_type=instance_type,
                    region=region,
                    availability_zone=None,
                    capacity_type="spot",
                    availability="unknown",
                    confidence=0.1,
                    recommendation=f"No spot data available for {instance_type} in {region}",
                    details={"reason": "No spot price history or placement score available"},
                )
            )

        return estimates

    def _estimate_on_demand_capacity(
        self,
        instance_type: str,
        region: str,
        instance_info: InstanceTypeInfo | None,
        spot_placement_scores: dict[str, int] | None = None,
        spot_prices: list[SpotPriceInfo] | None = None,
    ) -> CapacityEstimate | None:
        """Estimate on-demand capacity using live signals for ALL instance types.

        Uses spot placement scores, instance size (vCPUs, memory, GPUs),
        pricing, and spot-to-on-demand price ratios as universal scarcity
        signals — no hardcoded instance families or GPU type lists.
        """
        on_demand_price = self.get_on_demand_price(instance_type, region)

        is_offered = self.check_instance_available_in_region(instance_type, region)

        if not is_offered:
            return CapacityEstimate(
                instance_type=instance_type,
                region=region,
                availability_zone=None,
                capacity_type="on-demand",
                availability="unavailable",
                confidence=1.0,
                recommendation=f"{instance_type} is not offered in {region}",
                details={"reason": "Instance type not offered in region"},
            )

        # Fetch spot placement scores if not provided (on-demand only mode)
        if spot_placement_scores is None:
            spot_placement_scores = self.get_spot_placement_score(instance_type, region)

        if spot_prices is None:
            spot_prices = self.get_spot_price_history(instance_type, region)

        az_coverage = self.get_az_coverage(instance_type, region)

        availability, confidence, recommendation = self._assess_on_demand_availability(
            instance_type,
            instance_info,
            on_demand_price,
            spot_placement_scores,
            spot_prices,
            az_coverage,
        )

        if on_demand_price:
            recommendation += f" Price: ${on_demand_price:.4f}/hr."
        else:
            confidence -= 0.1
            recommendation += " Pricing data unavailable."

        details: dict[str, Any] = {
            "price_per_hour": on_demand_price,
            "is_gpu": instance_info.is_gpu if instance_info else False,
        }
        if spot_placement_scores:
            scores = [s for s in spot_placement_scores.values() if s > 0]
            if scores:
                details["avg_spot_placement_score"] = round(sum(scores) / len(scores), 1)

        return CapacityEstimate(
            instance_type=instance_type,
            region=region,
            availability_zone=None,
            capacity_type="on-demand",
            availability=availability,
            confidence=confidence,
            price_per_hour=on_demand_price,
            recommendation=recommendation,
            details=details,
        )

    @staticmethod
    def _assess_on_demand_availability(
        instance_type: str,
        instance_info: InstanceTypeInfo | None,
        on_demand_price: float | None,
        spot_placement_scores: dict[str, int] | None = None,
        spot_prices: list[SpotPriceInfo] | None = None,
        az_coverage: float | None = None,
    ) -> tuple[str, float, str]:
        """Assess on-demand availability using only live market signals.

        Five live signals, zero hardcoded instance families:
        1. Spot placement score — AWS's own capacity assessment (1-10)
        2. Spot-to-on-demand price ratio — when spot approaches on-demand price,
           the spot market has very little excess capacity
        3. Spot price volatility — unstable prices reflect capacity fluctuations
        4. AZ coverage — fraction of AZs that offer this instance type;
           constrained instances are often available in fewer AZs
        5. Spot price availability — how many AZs have spot price data;
           missing price data in some AZs suggests limited capacity there

        Confidence scales with the number of live signals available.
        When no signals exist, returns "unknown" rather than guessing.

        Returns:
            Tuple of (availability, confidence, recommendation)
        """
        price = on_demand_price or 0
        gpu_count = instance_info.gpu_count if instance_info else 0
        gpu_type = (instance_info.gpu_type or "") if instance_info else ""
        total_gpu_mem = instance_info.gpu_memory_gib if instance_info else 0

        # --- Signal 1: Spot placement score ---
        avg_spot_score = 0.0
        has_spot_score = False
        if spot_placement_scores:
            scores = [s for s in spot_placement_scores.values() if s > 0]
            if scores:
                avg_spot_score = sum(scores) / len(scores)
                has_spot_score = True

        # --- Signal 2 & 3: Spot price ratio and volatility ---
        avg_spot_ratio = 0.0
        avg_stability = 1.0
        has_price_signal = False
        if spot_prices and price > 0:
            ratios = [sp.current_price / price for sp in spot_prices if sp.current_price > 0]
            if ratios:
                avg_spot_ratio = sum(ratios) / len(ratios)
                has_price_signal = True
            stabilities = [sp.price_stability for sp in spot_prices]
            if stabilities:
                avg_stability = sum(stabilities) / len(stabilities)

        # --- Signal 4: AZ coverage (passed in from caller) ---
        has_az_signal = az_coverage is not None

        # --- Combine live signals into scarcity (0.0 - 1.0) ---
        scarcity = 0.0
        signal_count = 0

        if has_spot_score:
            signal_count += 1
            if avg_spot_score <= 2:
                scarcity += 0.5
            elif avg_spot_score <= 4:
                scarcity += 0.3
            elif avg_spot_score <= 6:
                scarcity += 0.15

        if has_price_signal:
            signal_count += 1
            # Spot price near on-demand = spot market has minimal excess capacity
            if avg_spot_ratio >= 0.9:
                scarcity += 0.3
            elif avg_spot_ratio >= 0.7:
                scarcity += 0.15
            elif avg_spot_ratio >= 0.5:
                scarcity += 0.05

            # Price instability = capacity fluctuations
            if avg_stability < 0.6:
                scarcity += 0.1
            elif avg_stability < 0.8:
                scarcity += 0.05

        if has_az_signal and az_coverage is not None:
            signal_count += 1
            # Available in fewer than half the AZs = constrained
            if az_coverage <= 0.3:
                scarcity += 0.2
            elif az_coverage <= 0.5:
                scarcity += 0.1

        # --- Confidence scales with signal count ---
        confidence = min(0.5 + (signal_count * 0.12), 0.9)

        # --- Map scarcity to availability ---
        desc = _instance_desc(instance_type, gpu_count, gpu_type, total_gpu_mem)

        if signal_count == 0:
            # No live data — be honest about it
            return (
                "unknown",
                0.3,
                f"No live capacity signals available for {instance_type}."
                " Unable to assess on-demand availability.",
            )

        if scarcity >= 0.6:
            return (
                "low",
                confidence,
                f"On-demand {desc} is extremely scarce based on live capacity signals."
                " Capacity reservations or Capacity Blocks are strongly recommended.",
            )

        if scarcity >= 0.35:
            return (
                "low",
                confidence,
                f"On-demand {desc} has limited availability based on live capacity signals."
                " Consider capacity reservations.",
            )

        if scarcity >= 0.15:
            return (
                "medium",
                confidence,
                f"On-demand {instance_type} may have constrained availability"
                " based on current market conditions.",
            )

        return (
            "high",
            confidence,
            f"On-demand capacity likely available for {instance_type}"
            " based on live capacity signals.",
        )

    def recommend_capacity_type(
        self, instance_type: str, region: str, fault_tolerance: str = "medium"
    ) -> tuple[str, str]:
        """
        Recommend spot vs on-demand based on actual capacity and requirements.

        Args:
            instance_type: EC2 instance type
            region: AWS region
            fault_tolerance: "high" (can handle interruptions),
                           "medium" (some tolerance),
                           "low" (needs stability)

        Returns:
            Tuple of (recommended_capacity_type, explanation)
        """
        estimates = self.estimate_capacity(instance_type, region, "both")

        spot_estimates = [e for e in estimates if e.capacity_type == "spot"]
        od_estimates = [e for e in estimates if e.capacity_type == "on-demand"]

        # Check for unavailable
        if any(e.availability == "unavailable" for e in estimates):
            return "unavailable", f"{instance_type} is not available in {region}"

        # Get best spot option (highest availability)
        best_spot = None
        if spot_estimates:
            available_spots = [e for e in spot_estimates if e.availability != "unknown"]
            if available_spots:
                # Sort by availability (high > medium > low) then by price
                avail_order = {"high": 0, "medium": 1, "low": 2}
                best_spot = min(
                    available_spots,
                    key=lambda x: (avail_order.get(x.availability, 3), x.price_per_hour or 999),
                )

        od_estimate = od_estimates[0] if od_estimates else None

        # Decision logic based on fault tolerance and actual availability
        if fault_tolerance == "low":
            if od_estimate and od_estimate.availability in ("high", "medium"):
                return "on-demand", "Low fault tolerance requires stable on-demand capacity"
            return (
                "on-demand",
                "On-demand recommended but capacity may be limited; consider capacity reservation",
            )

        if best_spot:
            if best_spot.availability == "high":
                savings = ""
                if best_spot.price_per_hour and od_estimate and od_estimate.price_per_hour:
                    pct = (1 - best_spot.price_per_hour / od_estimate.price_per_hour) * 100
                    savings = f" (save ~{pct:.0f}%)"
                return "spot", f"High spot availability (score-based){savings}"

            if best_spot.availability == "medium":
                if fault_tolerance == "high":
                    return "spot", "Medium spot availability acceptable with high fault tolerance"
                return (
                    "on-demand",
                    "Spot availability is medium; on-demand recommended for reliability",
                )

            if best_spot.availability == "low":
                if fault_tolerance == "high":
                    return (
                        "spot",
                        "Low spot availability but acceptable with high fault tolerance",
                    )
                return "on-demand", "Spot capacity is limited; on-demand recommended"

        # Default to on-demand
        return "on-demand", "On-demand recommended (spot availability unknown or limited)"

    # -------------------------------------------------------------------------
    # Capacity Reservations (ODCRs) and Capacity Blocks for ML
    # -------------------------------------------------------------------------

    def list_capacity_reservations(
        self,
        region: str,
        instance_type: str | None = None,
        state: str = "active",
    ) -> list[dict[str, Any]]:
        """
        List EC2 On-Demand Capacity Reservations (ODCRs) in a region.

        Args:
            region: AWS region to query
            instance_type: Filter by instance type (optional)
            state: Filter by state — "active" (default), or None for all

        Returns:
            List of reservation dictionaries with availability details
        """
        ec2 = self._session.client("ec2", region_name=region)

        filters: list[dict[str, Any]] = []
        if state:
            filters.append({"Name": "state", "Values": [state]})
        if instance_type:
            filters.append({"Name": "instance-type", "Values": [instance_type]})

        reservations: list[dict[str, Any]] = []
        try:
            paginator = ec2.get_paginator("describe_capacity_reservations")
            page_kwargs: dict[str, Any] = {}
            if filters:
                page_kwargs["Filters"] = filters

            for page in paginator.paginate(**page_kwargs):
                for cr in page.get("CapacityReservations", []):
                    total = cr.get("TotalInstanceCount", 0)
                    available = cr.get("AvailableInstanceCount", 0)
                    used = total - available

                    reservations.append(
                        {
                            "type": "odcr",
                            "reservation_id": cr.get("CapacityReservationId"),
                            "instance_type": cr.get("InstanceType"),
                            "availability_zone": cr.get("AvailabilityZone"),
                            "region": region,
                            "state": cr.get("State"),
                            "total_instances": total,
                            "available_instances": available,
                            "used_instances": used,
                            "utilization_pct": round(used / total * 100, 1) if total else 0,
                            "instance_platform": cr.get("InstancePlatform"),
                            "tenancy": cr.get("Tenancy"),
                            "instance_match_criteria": cr.get("InstanceMatchCriteria"),
                            "end_date": (cr["EndDate"].isoformat() if cr.get("EndDate") else None),
                            "end_date_type": cr.get("EndDateType"),
                            "tags": {t["Key"]: t["Value"] for t in cr.get("Tags", [])},
                        }
                    )
        except ClientError as e:
            logger.debug("Failed to list capacity reservations in %s: %s", region, e)

        return reservations

    def _build_block_offering(
        self,
        offering: dict[str, Any],
        region: str,
        requested_count: int,
        gpus_per_instance: int | None,
        requested_duration_hours: int,
    ) -> dict[str, Any]:
        """Shape a raw DescribeCapacityBlockOfferings entry into an enriched dict.

        Reports the offering's *actual* duration (the API returns blocks whose
        duration is the closest match to the request, not necessarily equal) and
        adds per-hour / per-GPU-hour pricing derived from the upfront fee. The raw
        ``upfront_fee`` is preserved for backward compatibility; ``upfront_fee_usd``
        is the parsed float used for ranking.
        """
        start_date = offering.get("StartDate")
        end_date = offering.get("EndDate")

        actual_duration = offering.get("CapacityBlockDurationHours")
        if actual_duration is None:
            minutes = offering.get("CapacityBlockDurationMinutes")
            actual_duration = round(minutes / 60) if minutes else requested_duration_hours

        count = offering.get("InstanceCount")
        if count is None:
            count = requested_count

        pricing = blocks.compute_offering_pricing(
            offering.get("UpfrontFee"), actual_duration, count, gpus_per_instance
        )

        return {
            "type": "capacity_block",
            "offering_id": offering.get("CapacityBlockOfferingId"),
            "instance_type": offering.get("InstanceType"),
            "availability_zone": offering.get("AvailabilityZone"),
            "region": region,
            "instance_count": count,
            "duration_hours": actual_duration,
            "duration_days": blocks.hours_to_days(actual_duration),
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
            "upfront_fee": offering.get("UpfrontFee"),
            "upfront_fee_usd": pricing["upfront_fee_usd"],
            "price_per_hour": pricing["price_per_hour"],
            "price_per_instance_hour": pricing["price_per_instance_hour"],
            "price_per_gpu_hour": pricing["price_per_gpu_hour"],
            "gpus_per_instance": gpus_per_instance,
            "currency": offering.get("CurrencyCode", "USD"),
            "tenancy": offering.get("Tenancy"),
        }

    def list_capacity_block_offerings(
        self,
        region: str,
        instance_type: str,
        instance_count: int = 1,
        duration_hours: int = 24,
        *,
        earliest_start: datetime | None = None,
        latest_start: datetime | None = None,
        gpus_per_instance: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        List available Capacity Block offerings for ML workloads.

        Capacity Blocks provide guaranteed GPU capacity for a fixed duration
        at a known price — ideal for training jobs with predictable runtimes.
        Queries a single duration in a single region; the date window and the
        multi-duration / multi-region sweep are layered on top by
        :meth:`find_capacity_blocks`.

        Args:
            region: AWS region to query
            instance_type: GPU instance type (e.g. p5.48xlarge, p4d.24xlarge)
            instance_count: Number of instances needed
            duration_hours: Desired block duration in hours (must be a supported value)
            earliest_start: Only return blocks starting on/after this datetime
                (EC2 StartDateRange). Lets callers ask "blocks starting near D1".
            latest_start: Only return blocks starting on/before this datetime
                (EC2 EndDateRange).
            gpus_per_instance: GPUs per instance, used for per-GPU-hour pricing.
                Resolved from the instance specs when omitted.

        Returns:
            List of available capacity block offerings (enriched with pricing).
            All matching pages are followed via NextToken.
        """
        ec2 = self._session.client("ec2", region_name=region)
        offerings: list[dict[str, Any]] = []

        if gpus_per_instance is None:
            info = self.get_instance_info(instance_type)
            gpus_per_instance = info.gpu_count if info else None

        api_kwargs: dict[str, Any] = {
            "InstanceType": instance_type,
            "InstanceCount": instance_count,
            "CapacityDurationHours": duration_hours,
        }
        if earliest_start is not None:
            api_kwargs["StartDateRange"] = earliest_start
        if latest_start is not None:
            api_kwargs["EndDateRange"] = latest_start

        try:
            next_token: str | None = None
            while True:
                if next_token:
                    api_kwargs["NextToken"] = next_token
                response = ec2.describe_capacity_block_offerings(**api_kwargs)
                for offering in response.get("CapacityBlockOfferings", []):
                    offerings.append(
                        self._build_block_offering(
                            offering, region, instance_count, gpus_per_instance, duration_hours
                        )
                    )
                next_token = response.get("NextToken")
                if not next_token:
                    break
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in _CB_EXPECTED_ERROR_CODES:
                pass  # Type/region doesn't support Capacity Blocks — expected
            else:
                logger.warning("Failed to list capacity block offerings in %s: %s", region, e)
        except BotoCoreError as e:
            # Endpoint resolution / connection errors surface here when the
            # Capacity Block API isn't available in a region.
            logger.warning("Capacity Block API unavailable in %s: %s", region, e)

        return offerings

    def get_capacity_block_trend(
        self,
        instance_type: str,
        region: str,
    ) -> float:
        """
        Estimate capacity block availability trend via time-series regression.

        Queries offerings across the maximum 182-day (26-week) window, buckets
        them into weekly bins by start date, and fits a linear regression to
        the offering counts per week. The normalized slope indicates whether
        capacity is growing or shrinking over time.

        Returns:
            Trend score from -1.0 to 1.0:
              > 0 = capacity growing (offerings increasing week-over-week)
              = 0 = stable or no data
              < 0 = capacity shrinking (offerings decreasing week-over-week)
        """
        ec2 = self._session.client("ec2", region_name=region)

        now = datetime.now(UTC)
        far_end = now + timedelta(days=182)

        try:
            response = ec2.describe_capacity_block_offerings(
                InstanceType=instance_type,
                InstanceCount=1,
                CapacityDurationHours=24,  # Minimum duration for broadest results
                StartDateRange=now,
                EndDateRange=far_end,
            )
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code not in _CB_EXPECTED_ERROR_CODES:
                logger.warning(
                    "Capacity block trend query failed for %s in %s: %s",
                    instance_type,
                    region,
                    e,
                )
            return 0.0
        except BotoCoreError as e:
            logger.warning(
                "Capacity block trend query failed for %s in %s: %s", instance_type, region, e
            )
            return 0.0

        offerings = response.get("CapacityBlockOfferings", [])
        if not offerings:
            return 0.0

        # Bucket offerings into weekly bins (week 0 = this week, week 25 = ~6 months out)
        num_weeks = 26
        bins = [0] * num_weeks
        for o in offerings:
            start = o.get("StartDate")
            if start is None:
                continue
            delta_days = (start - now).total_seconds() / 86400.0
            week_idx = int(delta_days / 7)
            if 0 <= week_idx < num_weeks:
                bins[week_idx] += 1

        # Need at least 2 non-zero bins to detect a meaningful trend
        non_zero = sum(1 for b in bins if b > 0)
        if non_zero < 2:
            return 0.0

        # Linear regression: slope of offerings-per-week over time
        # Using least-squares: slope = Σ((x-x̄)(y-ȳ)) / Σ((x-x̄)²)
        n = len(bins)
        x_mean = (n - 1) / 2.0
        y_mean = statistics.mean(bins)

        numerator = sum((i - x_mean) * (bins[i] - y_mean) for i in range(n))
        denominator = sum((i - x_mean) ** 2 for i in range(n))

        if denominator == 0:
            return 0.0

        slope = numerator / denominator

        # Normalize slope to -1..1 range relative to the mean offering count.
        # A slope of +y_mean per 26 weeks would be a doubling → maps to ~1.0.
        normalized = slope * num_weeks / (y_mean * 2) if y_mean > 0 else 0.0

        return round(max(-1.0, min(1.0, normalized)), 4)

    def list_all_reservations(
        self,
        instance_type: str | None = None,
        regions: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        List all capacity reservations (ODCRs) across deployed regions.

        Args:
            instance_type: Filter by instance type (optional)
            regions: Regions to query (defaults to deployed GCO regions)

        Returns:
            Summary dict with reservations grouped by region
        """
        if not regions:
            from cli.aws_client import get_aws_client

            aws_client = get_aws_client(self.config)
            stacks = aws_client.discover_regional_stacks()
            regions = list(stacks.keys()) if stacks else [self.config.default_region]

        all_reservations: list[dict[str, Any]] = []
        for region in regions:
            all_reservations.extend(
                self.list_capacity_reservations(region, instance_type=instance_type)
            )

        total_reserved = sum(r["total_instances"] for r in all_reservations)
        total_available = sum(r["available_instances"] for r in all_reservations)

        return {
            "regions_checked": regions,
            "instance_type_filter": instance_type,
            "total_reservations": len(all_reservations),
            "total_reserved_instances": total_reserved,
            "total_available_instances": total_available,
            "reservations": all_reservations,
        }

    @staticmethod
    def _summarize_block_search(
        instance_type: str,
        regions: list[str],
        offerings: list[dict[str, Any]],
        best: dict[str, Any] | None,
        longest: dict[str, Any] | None,
        note: str | None,
    ) -> str:
        """Build a one-line human recommendation for a consolidated block search."""
        if not offerings:
            msg = (
                f"No Capacity Block offerings for {instance_type} across "
                f"{len(regions)} region(s) in the requested window. Try a wider "
                "date range, a shorter duration, more regions, or check back later."
            )
            return f"{note} {msg}" if note else msg

        regions_with = sorted({o["region"] for o in offerings if o.get("region")})
        parts = [
            f"Found {len(offerings)} Capacity Block offering(s) for {instance_type} "
            f"across {len(regions_with)} region(s)."
        ]
        if best:
            price = best.get("price_per_gpu_hour")
            price_str = f", from ${price}/GPU-hr" if price is not None else ""
            parts.append(
                f"Cheapest: {best.get('region')}/{best.get('availability_zone')} "
                f"{best.get('duration_days')}d starting "
                f"{(best.get('start_date') or '')[:16]}{price_str}."
            )
        if longest and longest is not best:
            parts.append(
                f"Longest: {longest.get('duration_days')}d in "
                f"{longest.get('region')}/{longest.get('availability_zone')}."
            )
        msg = " ".join(parts)
        return f"{note} {msg}" if note else msg

    def find_capacity_blocks(
        self,
        instance_type: str,
        regions: list[str] | None = None,
        *,
        instance_count: int = 1,
        duration_hours: int | None = None,
        duration_days: int | None = None,
        min_duration_hours: int | None = None,
        min_duration_days: int | None = None,
        max_duration_hours: int | None = None,
        max_duration_days: int | None = None,
        earliest_start: str | datetime | None = None,
        latest_start: str | datetime | None = None,
        find_longest: bool = False,
        max_workers: int | None = None,
    ) -> dict[str, Any]:
        """Sweep regions x durations x a date window for Capacity Blocks in one call.

        This is the high-level search that answers "find 1x p6-b200.48xlarge
        across us-east-1/us-east-2/us-west-2/eu-west-1 for durations 1-63 days
        starting 2026-07-01..2026-07-10" without manual multi-call sweeping.

        Because EC2 requires an exact ``CapacityDurationHours`` per query, a
        duration *range* (or ``find_longest``) expands to every valid Capacity
        Block duration in the range, and each (region, duration) pair is probed
        in parallel. Offerings are de-duplicated across probes, grouped/sorted by
        region + AZ + start date, and ranked cheapest-first by per-GPU-hour price.

        Args:
            instance_type: GPU instance type or friendly alias (e.g. p6-b200).
            regions: Explicit regions to search (any regions, not just deployed).
                Defaults to the deployed GCO regions when omitted.
            instance_count: Instances per block.
            duration_hours / duration_days: A single target duration.
            min_duration_hours / min_duration_days: Lower bound of a duration range.
            max_duration_hours / max_duration_days: Upper bound of a duration range.
            earliest_start / latest_start: Date (YYYY-MM-DD) or ISO datetime window
                for the block start, threaded to StartDateRange / EndDateRange.
            find_longest: Sweep the full duration ladder (within any range given)
                and surface the longest available block.
            max_workers: Override the parallel fan-out width.

        Returns:
            A consolidated report dict (see keys assembled below).
        """
        validation = self.validate_instance_type(instance_type)
        canonical = validation["instance_type"]
        gpus_per_instance = validation["gpu_count"]

        parsed_earliest = blocks.parse_date_input(earliest_start)
        parsed_latest = blocks.parse_date_input(latest_start)

        durations = blocks.resolve_search_durations(
            duration_hours=blocks.coerce_hours(duration_hours, duration_days),
            min_duration_hours=blocks.coerce_hours(min_duration_hours, min_duration_days),
            max_duration_hours=blocks.coerce_hours(max_duration_hours, max_duration_days),
            find_longest=find_longest,
        )

        if not regions:
            from cli.aws_client import get_aws_client

            aws_client = get_aws_client(self.config)
            stacks = aws_client.discover_regional_stacks()
            regions = list(stacks.keys()) if stacks else [self.config.default_region]

        report: dict[str, Any] = {
            "instance_type": canonical,
            "requested_instance_type": instance_type,
            "valid_instance_type": validation["valid"],
            "known_instance_type": validation["known"],
            "note": validation["note"],
            "instance_count": instance_count,
            "regions_checked": list(regions),
            "durations_probed_hours": durations,
            "durations_probed_days": [blocks.hours_to_days(h) for h in durations],
            "date_window": {
                "earliest_start": parsed_earliest.isoformat() if parsed_earliest else None,
                "latest_start": parsed_latest.isoformat() if parsed_latest else None,
            },
            "offerings_found": 0,
            "offerings": [],
            "ranked": [],
            "best": None,
            "longest": None,
            "regions_with_offerings": [],
        }

        if not validation["valid"]:
            report["recommendation"] = (
                validation["note"]
                or f"'{instance_type}' is not a valid standalone EC2 instance type "
                "for Capacity Blocks."
            )
            return report

        probes = [(region, dur) for region in regions for dur in durations]
        collected: list[dict[str, Any]] = []
        workers = max(1, min(max_workers or _MAX_SEARCH_WORKERS, len(probes)))

        def _probe(region: str, dur: int) -> list[dict[str, Any]]:
            try:
                return self.list_capacity_block_offerings(
                    region,
                    canonical,
                    instance_count=instance_count,
                    duration_hours=dur,
                    earliest_start=parsed_earliest,
                    latest_start=parsed_latest,
                    gpus_per_instance=gpus_per_instance,
                )
            except Exception as e:
                logger.warning(
                    "Capacity block probe failed for %s in %s (%sh): %s",
                    canonical,
                    region,
                    dur,
                    e,
                )
                return []

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_probe, region, dur) for region, dur in probes]
            for future in as_completed(futures):
                collected.extend(future.result())

        unique = blocks.dedupe_offerings(collected)
        ranked = blocks.rank_offerings(unique)
        best = ranked[0] if ranked else None
        longest = blocks.longest_offering(unique)

        report.update(
            {
                "offerings_found": len(unique),
                "offerings": blocks.sort_offerings(unique),
                "ranked": ranked,
                "best": best,
                "longest": longest,
                "regions_with_offerings": sorted({o["region"] for o in unique if o.get("region")}),
                "recommendation": self._summarize_block_search(
                    canonical, regions, unique, best, longest, validation["note"]
                ),
            }
        )
        return report

    def check_reservation_availability(
        self,
        instance_type: str,
        region: str | None = None,
        min_count: int = 1,
        include_capacity_blocks: bool = True,
        block_duration_hours: int = 24,
        *,
        regions: list[str] | None = None,
        block_duration_days: int | None = None,
        earliest_start: str | datetime | None = None,
        latest_start: str | datetime | None = None,
        max_workers: int | None = None,
    ) -> dict[str, Any]:
        """
        Check if capacity reservations or blocks have available instances.

        Checks both ODCRs (existing reservations) and Capacity Block offerings
        (purchasable guaranteed capacity) for a given instance type, across one
        or many regions queried in parallel.

        Args:
            instance_type: EC2 instance type to check
            region: A single region (kept for backward compatibility)
            min_count: Minimum number of available instances needed
            include_capacity_blocks: Also check Capacity Block offerings
            block_duration_hours: Duration for capacity block search (hours)
            regions: Explicit list of regions to check in parallel. Takes
                precedence over ``region``; falls back to deployed regions when
                neither is given.
            block_duration_days: Block duration in days (overrides hours when set).
            earliest_start / latest_start: Date window for block start
                (StartDateRange / EndDateRange).
            max_workers: Override the parallel fan-out width.

        Returns:
            Dictionary with ODCR availability and capacity block offerings
        """
        if regions:
            target_regions = list(regions)
        elif region:
            target_regions = [region]
        else:
            from cli.aws_client import get_aws_client

            aws_client = get_aws_client(self.config)
            stacks = aws_client.discover_regional_stacks()
            target_regions = list(stacks.keys()) if stacks else [self.config.default_region]

        effective_duration = blocks.snap_duration_hours(
            blocks.coerce_hours(block_duration_hours, block_duration_days) or 24
        )
        parsed_earliest = blocks.parse_date_input(earliest_start)
        parsed_latest = blocks.parse_date_input(latest_start)

        def _check_region(r: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            reservations = self.list_capacity_reservations(r, instance_type=instance_type)
            region_blocks: list[dict[str, Any]] = []
            if include_capacity_blocks:
                region_blocks = self.list_capacity_block_offerings(
                    r,
                    instance_type=instance_type,
                    instance_count=min_count,
                    duration_hours=effective_duration,
                    earliest_start=parsed_earliest,
                    latest_start=parsed_latest,
                )
            return reservations, region_blocks

        odcr_results: list[dict[str, Any]] = []
        block_offerings: list[dict[str, Any]] = []
        total_available = 0
        total_reserved = 0

        workers = max(1, min(max_workers or _MAX_SEARCH_WORKERS, len(target_regions)))
        if len(target_regions) == 1:
            region_results = [_check_region(target_regions[0])]
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                region_results = list(executor.map(_check_region, target_regions))

        for reservations, region_blocks in region_results:
            for res in reservations:
                avail = res["available_instances"]
                total_available += avail
                total_reserved += res["total_instances"]
                if avail > 0:
                    odcr_results.append(res)
            block_offerings.extend(region_blocks)

        block_offerings = blocks.sort_offerings(block_offerings)
        has_odcr = total_available >= min_count
        has_blocks = len(block_offerings) > 0

        # Build recommendation
        if has_odcr:
            recommendation = (
                f"ODCR capacity available: {total_available} instances "
                f"across {len(odcr_results)} reservation(s)"
            )
        elif has_blocks:
            cheapest = min(block_offerings, key=_offering_fee)
            fee_display = cheapest.get("upfront_fee_usd")
            if fee_display is None:
                fee_display = cheapest.get("upfront_fee", "?")
            recommendation = (
                f"No ODCR capacity, but {len(block_offerings)} Capacity Block offering(s) "
                f"available (from ${fee_display} for {effective_duration}h)"
            )
        else:
            recommendation = (
                "No reserved capacity or block offerings found. "
                "Consider on-demand or spot, or request a Capacity Block "
                "for a different duration/region."
            )

        return {
            "instance_type": instance_type,
            "min_count_requested": min_count,
            "regions_checked": target_regions,
            "odcr": {
                "total_reserved_instances": total_reserved,
                "total_available_instances": total_available,
                "has_availability": has_odcr,
                "reservations": odcr_results,
            },
            "capacity_blocks": {
                "offerings_found": len(block_offerings),
                "has_offerings": has_blocks,
                "duration_hours": effective_duration,
                "date_window": {
                    "earliest_start": parsed_earliest.isoformat() if parsed_earliest else None,
                    "latest_start": parsed_latest.isoformat() if parsed_latest else None,
                },
                "offerings": block_offerings,
            },
            "recommendation": recommendation,
        }

    def purchase_capacity_block(
        self,
        offering_id: str,
        region: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """
        Purchase a Capacity Block offering by its ID.

        Args:
            offering_id: Capacity Block offering ID (cb-xxx) from list_capacity_block_offerings
            region: AWS region where the offering exists
            dry_run: If True, validate the offering without purchasing

        Returns:
            Dictionary with the created capacity reservation details
        """
        ec2 = self._session.client("ec2", region_name=region)

        if dry_run:
            # Validate the offering exists by describing capacity block offerings
            # and matching the ID
            try:
                # Use EC2 DryRun to validate permissions without purchasing
                ec2.purchase_capacity_block(
                    CapacityBlockOfferingId=offering_id,
                    InstancePlatform="Linux/UNIX",
                    DryRun=True,
                )
            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "")
                if error_code == "DryRunOperation":
                    # DryRunOperation means the request would have succeeded
                    return {
                        "success": True,
                        "dry_run": True,
                        "offering_id": offering_id,
                        "region": region,
                        "message": "Dry run succeeded — offering is valid and purchasable",
                    }
                error_msg = e.response.get("Error", {}).get("Message", str(e))
                return {
                    "success": False,
                    "dry_run": True,
                    "offering_id": offering_id,
                    "region": region,
                    "error_code": error_code,
                    "error": error_msg,
                }

        try:
            response = ec2.purchase_capacity_block(
                CapacityBlockOfferingId=offering_id,
                InstancePlatform="Linux/UNIX",
            )

            reservation = response.get("CapacityReservation", {})
            reservation_id = reservation.get("CapacityReservationId", "")
            instance_type = reservation.get("InstanceType", "")
            az = reservation.get("AvailabilityZone", "")
            total = reservation.get("TotalInstanceCount", 0)
            start = reservation.get("StartDate")
            end = reservation.get("EndDate")

            return {
                "success": True,
                "dry_run": False,
                "reservation_id": reservation_id,
                "offering_id": offering_id,
                "instance_type": instance_type,
                "availability_zone": az,
                "region": region,
                "total_instances": total,
                "start_date": start.isoformat() if start else None,
                "end_date": end.isoformat() if end else None,
                "state": reservation.get("State", ""),
            }
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            error_msg = e.response.get("Error", {}).get("Message", str(e))
            return {
                "success": False,
                "dry_run": False,
                "offering_id": offering_id,
                "region": region,
                "error_code": error_code,
                "error": error_msg,
            }

    def recommend_region_for_job(
        self,
        gpu_required: bool = False,
        min_gpus: int = 0,
        instance_type: str | None = None,
        gpu_count: int = 0,
    ) -> dict[str, Any]:
        """
        Recommend the optimal region for job placement.

        Delegates to MultiRegionCapacityChecker for cross-region analysis.

        Args:
            gpu_required: Whether the job requires GPUs
            min_gpus: Minimum number of GPUs required
            instance_type: Specific instance type for workload-aware scoring
            gpu_count: Number of GPUs required

        Returns:
            Dictionary with recommended region and justification
        """
        from .multi_region import MultiRegionCapacityChecker

        checker = MultiRegionCapacityChecker(self.config)
        return checker.recommend_region_for_job(
            gpu_required, min_gpus, instance_type=instance_type, gpu_count=gpu_count
        )


def get_capacity_checker(config: GCOConfig | None = None) -> CapacityChecker:
    """Get a configured capacity checker instance."""
    return CapacityChecker(config)
