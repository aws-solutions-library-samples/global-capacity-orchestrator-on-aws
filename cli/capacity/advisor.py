"""Bedrock-powered AI capacity advisor."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from cli.config import GCOConfig, get_config
from gco.bedrock import (
    BEDROCK_READ_TIMEOUT_SECONDS,
    BedrockResponseTruncatedError,
    build_bedrock_converse_options,
    extract_bedrock_converse_text,
    get_default_bedrock_model_id,
    raise_if_bedrock_ftu_form_error,
)

from .checker import CapacityChecker
from .multi_region import MultiRegionCapacityChecker, compute_price_trend

logger = logging.getLogger(__name__)


def _snippet(text: str, limit: int = 200) -> str:
    """Compact, single-line prefix of ``text`` for parse-failure messages."""
    collapsed = " ".join(text.split())
    return collapsed[:limit] + ("..." if len(collapsed) > limit else "")


@dataclass
class BedrockCapacityRecommendation:
    """AI-generated capacity recommendation from Bedrock."""

    recommended_region: str
    recommended_instance_type: str
    recommended_capacity_type: str  # "spot" or "on-demand"
    reasoning: str
    confidence: str  # "high", "medium", "low"
    cost_estimate: str | None = None
    alternative_options: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw_response: str = ""


@dataclass
class CapacityPredictionResult:
    """Bedrock prediction of the best time(s) to acquire capacity."""

    instance_type: str
    region: str
    best_windows: list[dict[str, Any]] = field(default_factory=list)
    avoid_windows: list[dict[str, Any]] = field(default_factory=list)
    reasoning: str = ""
    confidence: str = "low"
    raw_response: str = ""


class _SharedBedrockModelDefault:
    """Lazily expose the historical advisor class attribute as a string."""

    def __get__(self, instance: object, owner: type[Any] | None = None) -> str:
        return get_default_bedrock_model_id()


class BedrockCapacityAdvisor:
    """
    AI-powered capacity advisor using Amazon Bedrock.

    Gathers comprehensive capacity data and uses an LLM to provide
    intelligent recommendations for workload placement.

    DISCLAIMER: Recommendations are AI-generated and should be validated
    before making production decisions.
    """

    # Backward-compatible lazy class alias for callers that inspect the
    # advisor default. Resolution occurs only when this Bedrock-specific
    # attribute (or an advisor without an explicit model) is used.
    DEFAULT_MODEL = _SharedBedrockModelDefault()

    def __init__(self, config: GCOConfig | None = None, model_id: str | None = None):
        self.config = config or get_config()
        self._session = boto3.Session()
        self._capacity_checker = CapacityChecker(config)
        self._multi_region_checker = MultiRegionCapacityChecker(config)
        self._uses_default_model = model_id is None
        self.model_id: str = self.DEFAULT_MODEL if model_id is None else model_id

    def _get_bedrock_client(self) -> Any:
        """Get Bedrock runtime client."""
        return self._session.client(
            "bedrock-runtime",
            region_name="us-east-1",
            config=Config(read_timeout=BEDROCK_READ_TIMEOUT_SECONDS),
        )

    def gather_capacity_data(
        self,
        instance_types: list[str] | None = None,
        regions: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Gather comprehensive capacity data for AI analysis.

        Args:
            instance_types: List of instance types to analyze (defaults to one
                representative per current GPU generation, T4 through Blackwell)
            regions: List of regions to check (defaults to deployed GCO regions)

        Returns:
            Dictionary containing all gathered capacity data
        """
        from cli.aws_client import get_aws_client

        # Default to one representative per current GPU generation, spanning
        # budget inference through frontier training, so workload questions
        # about any generation get real telemetry. Sibling sizes of the same
        # GPU (e.g. g5.2xlarge/g5.4xlarge) are deliberately omitted — each
        # type costs a full set of AWS API calls per region. GB200/GB300
        # NVL72 are UltraServer families, not standalone EC2 instance types
        # (see cli/capacity/blocks.py NON_STANDALONE_INSTANCE_NOTES), so the
        # standalone Blackwell types represent that generation here.
        if not instance_types:
            instance_types = [
                "g4dn.xlarge",  # T4 — budget inference
                "g6.xlarge",  # L4 — budget inference
                "g5.xlarge",  # A10G — mainstream single-GPU
                "g6e.xlarge",  # L40S — mainstream single-GPU
                "g7.2xlarge",  # RTX PRO 4500 Blackwell — current-gen budget inference
                "g7e.2xlarge",  # RTX PRO 6000 Blackwell — current-gen single-GPU inference
                "p4d.24xlarge",  # 8x A100 — distributed training
                "p5.48xlarge",  # 8x H100 — large-scale training
                "p5en.48xlarge",  # 8x H200 — large-scale training
                "p6-b200.48xlarge",  # 8x B200 (Blackwell) — frontier training
                "p6-b300.48xlarge",  # 8x B300 (Blackwell Ultra) — frontier training
            ]

        # Get deployed regions if not specified
        if not regions:
            aws_client = get_aws_client(self.config)
            stacks = aws_client.discover_regional_stacks()
            regions = list(stacks.keys()) if stacks else [self.config.default_region]

        data: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "regions_analyzed": regions,
            "instance_types_analyzed": instance_types,
            "regional_capacity": {},
            "spot_data": {},
            "on_demand_data": {},
            "cluster_metrics": [],
            "queue_status": {},
        }

        # Gather regional cluster metrics
        for region in regions:
            try:
                capacity = self._multi_region_checker.get_region_capacity(region)
                data["cluster_metrics"].append(
                    {
                        "region": region,
                        "queue_depth": capacity.queue_depth,
                        "running_jobs": capacity.running_jobs,
                        "pending_jobs": capacity.pending_jobs,
                        "gpu_utilization": capacity.gpu_utilization,
                        "cpu_utilization": capacity.cpu_utilization,
                        "recommendation_score": capacity.recommendation_score,
                    }
                )
            except Exception as e:
                logger.debug("Failed to get cluster metrics for %s: %s", region, e)

        # Failed lookups are recorded here and rendered into the prompt so the
        # model reasons about *missing* data instead of inventing a story for
        # why a row is absent (e.g. GetSpotPlacementScores' 24-hour
        # new-configuration limit must not read as "this type has no spot").
        data["data_gaps"] = []

        def record_gap(instance_type: str, region: str, source: str, error: Exception) -> None:
            code = (
                error.response.get("Error", {}).get("Code", "")
                if isinstance(error, ClientError)
                else ""
            ) or type(error).__name__
            data["data_gaps"].append(
                {
                    "instance_type": instance_type,
                    "region": region,
                    "source": source,
                    "error": code,
                }
            )
            logger.debug(
                "Capacity lookup %r failed for %s in %s: %s", source, instance_type, region, error
            )

        for instance_type in instance_types:
            data["spot_data"][instance_type] = {}
            data["on_demand_data"][instance_type] = {}

            for region in regions:
                # Each lookup is isolated so one failing or throttled API
                # cannot discard the other signals for this (type, region)
                # pair, which previously erased real on-demand pricing and
                # spot history whenever the placement-score call failed.
                spot_entry: dict[str, Any] = {"placement_scores": {}, "prices": []}
                try:
                    spot_entry["placement_scores"] = (
                        self._capacity_checker.get_spot_placement_score(instance_type, region)
                    )
                except Exception as e:
                    record_gap(instance_type, region, "spot placement score", e)
                try:
                    spot_prices = self._capacity_checker.get_spot_price_history(
                        instance_type, region, days=7
                    )
                    spot_entry["prices"] = [
                        {
                            "az": p.availability_zone,
                            "current": p.current_price,
                            "avg_7d": p.avg_price_7d,
                            "stability": p.price_stability,
                        }
                        for p in spot_prices
                    ]
                except Exception as e:
                    record_gap(instance_type, region, "spot price history", e)
                data["spot_data"][instance_type][region] = spot_entry

                # Spot price trend analysis per AZ (for AI interpretation)
                try:
                    ec2 = self._session.client("ec2", region_name=region)
                    raw_resp = ec2.describe_spot_price_history(
                        InstanceTypes=[instance_type],
                        ProductDescriptions=["Linux/UNIX"],
                        StartTime=datetime.now(UTC) - timedelta(days=7),
                        EndTime=datetime.now(UTC),
                    )
                    az_raw: dict[str, list[float]] = {}
                    for item in raw_resp.get("SpotPriceHistory", []):
                        az = item["AvailabilityZone"]
                        if az not in az_raw:
                            az_raw[az] = []
                        az_raw[az].append(float(item["SpotPrice"]))
                    az_trends = {
                        az: compute_price_trend(prices)
                        for az, prices in az_raw.items()
                        if len(prices) >= 2
                    }
                    if az_trends:
                        data["spot_data"][instance_type][region]["price_trends"] = az_trends
                except Exception as e:
                    logger.debug(
                        "Failed to get price trends for %s in %s: %s", instance_type, region, e
                    )

                od_entry: dict[str, Any] = {"price_per_hour": None, "available": None}
                try:
                    od_entry["price_per_hour"] = self._capacity_checker.get_on_demand_price(
                        instance_type, region
                    )
                except Exception as e:
                    record_gap(instance_type, region, "on-demand price", e)
                try:
                    od_entry["available"] = (
                        self._capacity_checker.check_instance_available_in_region(
                            instance_type, region
                        )
                    )
                except Exception as e:
                    record_gap(instance_type, region, "region availability", e)
                data["on_demand_data"][instance_type][region] = od_entry

        # Gather capacity reservation and block data
        data["reservations"] = {}
        data["capacity_blocks"] = {}
        for instance_type in instance_types:
            data["reservations"][instance_type] = {}
            data["capacity_blocks"][instance_type] = {}
            for region in regions:
                try:
                    odcrs = self._capacity_checker.list_capacity_reservations(
                        region, instance_type=instance_type
                    )
                    if odcrs:
                        data["reservations"][instance_type][region] = [
                            {
                                "az": r["availability_zone"],
                                "total": r["total_instances"],
                                "available": r["available_instances"],
                                "utilization_pct": r["utilization_pct"],
                            }
                            for r in odcrs
                        ]
                except Exception as e:
                    logger.debug(
                        "Failed to list reservations for %s in %s: %s", instance_type, region, e
                    )

                try:
                    blocks = self._capacity_checker.list_capacity_block_offerings(
                        region, instance_type=instance_type, instance_count=1, duration_hours=24
                    )
                    if blocks:
                        data["capacity_blocks"][instance_type][region] = [
                            {
                                "az": b["availability_zone"],
                                "duration_hours": b["duration_hours"],
                                "start_date": b["start_date"],
                                "upfront_fee": b["upfront_fee"],
                            }
                            for b in blocks
                        ]
                except Exception as e:
                    logger.debug(
                        "Failed to list capacity blocks for %s in %s: %s", instance_type, region, e
                    )

        # Capacity block availability trends (26-week regression per instance type per region)
        data["capacity_block_trends"] = {}
        for instance_type in instance_types:
            data["capacity_block_trends"][instance_type] = {}
            for region in regions:
                try:
                    trend = self._capacity_checker.get_capacity_block_trend(instance_type, region)
                    if trend != 0.0:
                        data["capacity_block_trends"][instance_type][region] = {
                            "trend_score": trend,
                            "interpretation": (
                                "capacity growing"
                                if trend > 0.2
                                else "capacity shrinking"
                                if trend < -0.2
                                else "stable"
                            ),
                        }
                except Exception as e:
                    logger.debug(
                        "Failed to get capacity block trend for %s in %s: %s",
                        instance_type,
                        region,
                        e,
                    )

        # Weighted recommendation scores (algorithmic ranking for AI context)
        try:
            weighted_results = self._multi_region_checker.recommend_region_for_job(
                instance_type=instance_types[0] if instance_types else None,
            )
            data["weighted_recommendation"] = {
                "top_region": weighted_results.get("region"),
                "scoring_method": weighted_results.get("scoring_method", "simple"),
                "instance_type": weighted_results.get("instance_type"),
                "all_regions": weighted_results.get("all_regions", []),
            }
        except Exception as e:
            logger.debug("Failed to compute weighted recommendation: %s", e)

        return data

    def _gather_historical_context(self, capacity_data: dict[str, Any]) -> dict[str, Any]:
        """Best-effort historical enrichment for the Bedrock prompt.

        For each (instance_type, region) with a current spot score, look up the
        7-day statistics and temporal patterns from the capacity history store.
        Returns an empty dict if the history surface is unavailable (table
        missing, no access, or feature disabled) so the advisor still works
        without it.
        """
        try:
            from cli.capacity.history import get_capacity_history_store

            store = get_capacity_history_store()
        except Exception as e:
            logger.debug("Capacity history store unavailable: %s", e)
            return {}

        context: dict[str, Any] = {}
        for instance_type, regions_data in capacity_data.get("spot_data", {}).items():
            for region, spot_info in (regions_data or {}).items():
                current = (spot_info.get("placement_scores") or {}).get("regional")
                if current is None:
                    continue
                try:
                    stats = store.get_statistics(instance_type, region)
                except Exception as e:
                    logger.debug("Historical stats lookup failed: %s", e)
                    return context
                spot_stats = stats.get("metrics", {}).get("spot_score")
                if not spot_stats:
                    continue
                try:
                    patterns = store.get_temporal_patterns(instance_type, region)
                    best_windows = patterns.get("best_windows", [])[:3]
                except Exception:
                    best_windows = []
                context[f"{instance_type}#{region}"] = {
                    "instance_type": instance_type,
                    "region": region,
                    "current_spot_score": current,
                    "p25": spot_stats["p25"],
                    "p50": spot_stats["p50"],
                    "p75": spot_stats["p75"],
                    "best_windows": best_windows,
                }
        return context

    def _build_prompt(
        self,
        capacity_data: dict[str, Any],
        workload_description: str | None = None,
        requirements: dict[str, Any] | None = None,
        historical_context: dict[str, Any] | None = None,
    ) -> str:
        """Build the prompt for Bedrock."""
        requirements = requirements or {}

        prompt = """You are an expert AWS capacity planning advisor for GPU/ML workloads.
Analyze the following capacity data and provide a recommendation for where to place a workload.

IMPORTANT DISCLAIMERS:
- This is AI-generated advice and should be validated before production use
- Capacity availability can change rapidly
- Spot instances may be interrupted at any time
- Pricing data may not reflect real-time prices

"""

        if workload_description:
            prompt += f"WORKLOAD DESCRIPTION:\n{workload_description}\n\n"

        if requirements:
            prompt += "REQUIREMENTS:\n"
            if requirements.get("gpu_required"):
                prompt += "- GPU Required: Yes\n"
            if requirements.get("min_gpus"):
                prompt += f"- Minimum GPUs: {requirements['min_gpus']}\n"
            if requirements.get("min_memory_gb"):
                prompt += f"- Minimum Memory: {requirements['min_memory_gb']} GB\n"
            if requirements.get("fault_tolerance"):
                prompt += f"- Fault Tolerance: {requirements['fault_tolerance']}\n"
            if requirements.get("max_cost_per_hour"):
                prompt += f"- Max Cost/Hour: ${requirements['max_cost_per_hour']}\n"
            prompt += "\n"

        prompt += "CAPACITY DATA:\n"
        prompt += f"Timestamp: {capacity_data.get('timestamp', 'N/A')}\n"
        prompt += f"Regions Analyzed: {', '.join(capacity_data.get('regions_analyzed', []))}\n"
        prompt += (
            f"Instance Types: {', '.join(capacity_data.get('instance_types_analyzed', []))}\n\n"
        )

        # Cluster metrics
        if capacity_data.get("cluster_metrics"):
            prompt += "CLUSTER METRICS BY REGION:\n"
            for m in capacity_data["cluster_metrics"]:
                prompt += f"  {m['region']}:\n"
                prompt += f"    - Queue Depth: {m['queue_depth']}\n"
                prompt += f"    - Running Jobs: {m['running_jobs']}\n"
                prompt += f"    - GPU Utilization: {m['gpu_utilization']:.1f}%\n"
                prompt += f"    - CPU Utilization: {m['cpu_utilization']:.1f}%\n"
            prompt += "\n"

        # Spot data summary
        prompt += "SPOT CAPACITY SUMMARY:\n"
        for instance_type, regions_data in capacity_data.get("spot_data", {}).items():
            prompt += f"  {instance_type}:\n"
            for region, spot_info in regions_data.items():
                scores = spot_info.get("placement_scores", {})
                regional_score = scores.get("regional", "N/A")
                prices = spot_info.get("prices", [])
                avg_price = sum(p["current"] for p in prices) / len(prices) if prices else "N/A"
                prompt += f"    {region}: Score={regional_score}/10, "
                prompt += f"Avg Price=${avg_price if isinstance(avg_price, str) else f'{avg_price:.4f}'}/hr\n"
                trends = spot_info.get("price_trends", {})
                if trends:
                    rendered = ", ".join(
                        f"{az} {t['direction']} "
                        f"(normalized slope {t['normalized_slope']:+.2f}, "
                        f"{t['price_changes']} price changes)"
                        for az, t in sorted(trends.items())
                    )
                    prompt += f"      7-day spot price trend by AZ: {rendered}\n"
        prompt += "\n"

        # On-demand data summary
        prompt += "ON-DEMAND PRICING:\n"
        for instance_type, regions_data in capacity_data.get("on_demand_data", {}).items():
            prompt += f"  {instance_type}:\n"
            for region, od_info in regions_data.items():
                price = od_info.get("price_per_hour")
                available = od_info.get("available")
                # None means the offerings lookup failed — say "unknown" so the
                # model cannot mistake a failed check for "not offered".
                availability = "unknown (lookup failed)" if available is None else available
                prompt += f"    {region}: ${price:.4f}/hr" if price else f"    {region}: N/A"
                prompt += f" (Available: {availability})\n"
        prompt += "\n"

        # Capacity reservations (ODCRs)
        reservations = capacity_data.get("reservations", {})
        has_reservations = any(bool(regions_data) for regions_data in reservations.values())
        if has_reservations:
            prompt += "CAPACITY RESERVATIONS (ODCRs):\n"
            for instance_type, regions_data in reservations.items():
                for region, odcrs in regions_data.items():
                    for r in odcrs:
                        prompt += (
                            f"  {instance_type} in {region} ({r['az']}): "
                            f"{r['available']}/{r['total']} available "
                            f"({r['utilization_pct']}% used)\n"
                        )
            prompt += "\n"

        # Capacity Blocks for ML
        blocks = capacity_data.get("capacity_blocks", {})
        has_blocks = any(bool(regions_data) for regions_data in blocks.values())
        if has_blocks:
            prompt += "CAPACITY BLOCK OFFERINGS (guaranteed GPU blocks):\n"
            for instance_type, regions_data in blocks.items():
                for region, offerings in regions_data.items():
                    for b in offerings:
                        prompt += (
                            f"  {instance_type} in {region} ({b['az']}): "
                            f"{b['duration_hours']}h starting {b['start_date']}, "
                            f"${b['upfront_fee']}\n"
                        )
            prompt += "\n"

        # Capacity block availability trends (26-week offering-density regression)
        block_trends = capacity_data.get("capacity_block_trends", {})
        has_block_trends = any(bool(regions_data) for regions_data in block_trends.values())
        if has_block_trends:
            prompt += "CAPACITY BLOCK AVAILABILITY TRENDS (26-week, near-term vs far-term):\n"
            for instance_type, regions_data in block_trends.items():
                for region, trend in regions_data.items():
                    prompt += (
                        f"  {instance_type} in {region}: "
                        f"{trend['trend_score']:+.2f} ({trend['interpretation']})\n"
                    )
            prompt += "\n"

        # Algorithmic multi-signal ranking (context for the model, not binding)
        weighted = capacity_data.get("weighted_recommendation")
        if weighted and weighted.get("all_regions"):
            scoring_method = weighted.get("scoring_method", "simple")
            scored_for = (
                f" for {weighted['instance_type']}" if weighted.get("instance_type") else ""
            )
            prompt += (
                f"ALGORITHMIC REGION RANKING ({scoring_method} scoring{scored_for}; "
                "lower score = better; advisory pre-computation, weigh it "
                "against the raw data above):\n"
            )
            for entry in weighted["all_regions"]:
                prompt += f"  {entry['region']}: score={entry['score']:.1f}"
                details = []
                if entry.get("spot_placement_score") is not None:
                    details.append(f"spot availability {entry['spot_placement_score']:.0%}")
                if entry.get("spot_price_ratio") is not None:
                    details.append(f"spot/on-demand price ratio {entry['spot_price_ratio']:.2f}")
                if entry.get("capacity_block_trend"):
                    details.append(f"block trend {entry['capacity_block_trend']:+.2f}")
                details.append(f"queue depth {entry.get('queue_depth', 'N/A')}")
                gpu_util = entry.get("gpu_utilization")
                if gpu_util is not None:
                    details.append(f"GPU util {gpu_util:.0f}%")
                prompt += f" ({', '.join(details)})\n"
            prompt += "\n"

        # Failed lookups — spelled out so the model reasons about missing
        # data instead of inventing an explanation for absent rows (e.g. the
        # placement-score API's 24-hour new-configuration limit must not read
        # as "this instance type has no spot pools").
        data_gaps = capacity_data.get("data_gaps") or []
        if data_gaps:
            prompt += "DATA GAPS (lookups that FAILED — treat as unknown, not as unavailable):\n"
            grouped: dict[tuple[str, str, str], list[str]] = {}
            for gap in data_gaps:
                key = (gap["source"], gap["error"], gap["region"])
                grouped.setdefault(key, []).append(gap["instance_type"])
            for (source, error, region), types in sorted(grouped.items()):
                prompt += (
                    f"  {source} in {region} failed with {error} for: {', '.join(sorted(types))}\n"
                )
            prompt += (
                "  Do not draw capacity or availability conclusions from these "
                "missing values; rely on the signals that are present and "
                "mention the gap in your warnings.\n"
            )
            prompt += "\n"

        if historical_context:
            prompt += "## Historical Context (last 7 days)\n"
            for ctx in historical_context.values():
                current = ctx["current_spot_score"]
                p25 = ctx["p25"]
                p50 = ctx["p50"]
                p75 = ctx["p75"]
                if current < p25:
                    interpretation = "likely transient contention"
                elif current <= p75:
                    interpretation = "within normal range"
                else:
                    interpretation = "unusually favorable"
                prompt += f"  {ctx['instance_type']} in {ctx['region']}:\n"
                prompt += f"    Current spot score: {current}\n"
                prompt += f"    Historical p25/p50/p75: {p25}/{p50}/{p75}\n"
                prompt += f"    Interpretation: {interpretation}\n"
                windows = ctx.get("best_windows") or []
                if windows:
                    rendered = ", ".join(
                        f"{w['day']} {w['hour']:02d}:00 (avg {w['avg']})" for w in windows
                    )
                    prompt += f"    Best historical windows (top 3): {rendered}\n"
            prompt += "\n"

        prompt += """Based on this data, provide your recommendation in the following JSON format:
{
    "recommended_region": "region-name",
    "recommended_instance_type": "instance-type",
    "recommended_capacity_type": "spot, on-demand, odcr, or capacity-block",
    "reasoning": "Detailed explanation of why this is the best choice",
    "confidence": "high, medium, or low",
    "cost_estimate": "Estimated hourly cost",
    "reservation_advice": "If ODCRs or Capacity Blocks are available, explain how to use them. If not, suggest whether the user should consider purchasing a Capacity Block.",
    "alternative_options": [
        {"region": "...", "instance_type": "...", "capacity_type": "...", "reason": "..."}
    ],
    "warnings": ["Any important warnings or caveats"]
}

Respond ONLY with the JSON object, no additional text."""

        return prompt

    def get_recommendation(
        self,
        workload_description: str | None = None,
        instance_types: list[str] | None = None,
        regions: list[str] | None = None,
        requirements: dict[str, Any] | None = None,
    ) -> BedrockCapacityRecommendation:
        """
        Get an AI-powered capacity recommendation.

        Args:
            workload_description: Description of the workload
            instance_types: List of instance types to consider
            regions: List of regions to consider
            requirements: Dictionary of requirements (gpu_required, min_gpus, etc.)

        Returns:
            BedrockCapacityRecommendation with the AI's recommendation
        """
        # Gather capacity data
        capacity_data = self.gather_capacity_data(instance_types, regions)

        # Gather best-effort historical context (skipped when unavailable)
        historical_context = self._gather_historical_context(capacity_data)

        # Build prompt
        prompt = self._build_prompt(
            capacity_data, workload_description, requirements, historical_context
        )

        # Call Bedrock
        bedrock = self._get_bedrock_client()

        try:
            # Use the Converse API for better compatibility across models
            response = bedrock.converse(
                modelId=self.model_id,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                **build_bedrock_converse_options(
                    self.model_id,
                    # Deliberately no maxTokens: the Converse default is the
                    # model's own maximum output length, so reasoning plus the
                    # JSON answer can never hit a GCO-imposed cap. A cap is
                    # opt-in — pass maxTokens here to restore one.
                    inference_config={"temperature": 0.1},
                    apply_default_reasoning=self._uses_default_model,
                ),
            )

            # Extended reasoning precedes the final answer with a
            # ``reasoningContent`` block; return the first real text block.
            response_text = extract_bedrock_converse_text(response)

            # Parse JSON response
            # Find JSON in response (in case model adds extra text)
            json_start = response_text.find("{")
            json_end = response_text.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                json_str = response_text[json_start:json_end]
                result = json.loads(json_str)
            else:
                raise ValueError(
                    "No JSON object found in the model response "
                    f"(response begins: {_snippet(response_text)!r})"
                )

            return BedrockCapacityRecommendation(
                recommended_region=result.get("recommended_region", "unknown"),
                recommended_instance_type=result.get("recommended_instance_type", "unknown"),
                recommended_capacity_type=result.get("recommended_capacity_type", "spot"),
                reasoning=result.get("reasoning", ""),
                confidence=result.get("confidence", "low"),
                cost_estimate=result.get("cost_estimate"),
                alternative_options=result.get("alternative_options", []),
                warnings=result.get("warnings", []),
                raw_response=response_text,
            )

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            # Raised as a distinct type (still a RuntimeError) so callers can
            # tell a fixable account-setup gap from a transient Bedrock fault.
            raise_if_bedrock_ftu_form_error(e)
            if error_code == "AccessDeniedException":
                raise RuntimeError(
                    "Access denied to Bedrock. Ensure your IAM role has "
                    "bedrock:InvokeModel permission and the model is enabled in your account."
                ) from e
            if error_code == "ValidationException":
                raise RuntimeError(
                    f"Model {self.model_id} may not be available. "
                    "Try a different model with --model option."
                ) from e
            raise RuntimeError(f"Bedrock API error: {e}") from e
        except json.JSONDecodeError as e:
            # ``response_text`` is always bound here: the decoder can only
            # fail after the response text was extracted.
            raise RuntimeError(
                f"Failed to parse AI response as JSON: {e} "
                f"(response begins: {_snippet(response_text)!r})"
            ) from e
        except BedrockResponseTruncatedError:
            # Already carries its own remediation; wrapping it in the generic
            # "Failed to get AI recommendation" message would only bury it.
            raise
        except Exception as e:
            raise RuntimeError(f"Failed to get AI recommendation: {e}") from e

    def _build_predict_prompt(
        self,
        instance_type: str,
        region: str,
        stats: dict[str, Any],
        patterns: dict[str, Any],
    ) -> str:
        """Build a Bedrock prompt focused on the best time to acquire capacity."""
        metrics = stats.get("metrics", {})
        spot = metrics.get("spot_score", {})
        price = metrics.get("spot_price", {})
        lines = [
            "You are an expert AWS GPU capacity-timing advisor.",
            "",
            (
                f"Based ONLY on the historical capacity patterns below for "
                f"{instance_type} in {region}, recommend the best time window(s) to "
                f"acquire this capacity (spot or capacity blocks), and which windows to avoid."
            ),
            "",
            (
                f"## Historical window: last {stats.get('hours_back')} hours, "
                f"{stats.get('sample_count')} samples"
            ),
        ]
        if spot:
            lines.append(
                f"Spot placement score (1-10, higher = better availability): "
                f"p25={spot.get('p25')} p50={spot.get('p50')} p75={spot.get('p75')} "
                f"min={spot.get('min')} max={spot.get('max')}"
            )
        if price:
            lines.append(
                f"Spot price USD/hr (lower = cheaper): "
                f"p25={price.get('p25')} p50={price.get('p50')} p75={price.get('p75')}"
            )
        best = patterns.get("best_windows", [])[:10]
        if best:
            lines.append("")
            lines.append(
                "Top observed windows by average spot score (day, hour UTC, avg, samples):"
            )
            for window in best:
                lines.append(
                    f"- {window['day']} {window['hour']:02d}:00 UTC: "
                    f"avg {window['avg']} (n={window['count']})"
                )
        lines.append("")
        lines.append("Respond ONLY with a JSON object of this exact shape:")
        lines.append(
            '{"best_windows": [{"day": "Monday", "hour_range": "13:00-16:00 UTC", '
            '"why": "..."}], "avoid_windows": [{"day": "...", "hour_range": "...", '
            '"why": "..."}], "reasoning": "...", "confidence": "high|medium|low"}'
        )
        return "\n".join(lines)

    def predict_capacity_window(
        self,
        instance_type: str,
        region: str,
        hours_back: int = 168,
    ) -> CapacityPredictionResult:
        """Predict the best acquisition window for an instance type in a region.

        Reads the historical capacity surface, builds a timing-focused prompt,
        and asks Bedrock. Raises ``ValueError`` when there are no samples yet;
        propagates the underlying ``ClientError`` (e.g. ResourceNotFoundException)
        when the history table does not exist so callers can surface a hint, and
        ``BedrockResponseTruncatedError`` when the model's answer was cut off by
        an output-token limit.
        """
        from cli.capacity.history import get_capacity_history_store

        store = get_capacity_history_store()
        stats = store.get_statistics(instance_type, region, hours_back)
        if stats.get("sample_count", 0) == 0:
            raise ValueError(
                f"No historical capacity samples for {instance_type} in {region} yet. "
                "The poller records one about every 15 minutes once enabled."
            )
        patterns = store.get_temporal_patterns(instance_type, region, hours_back)
        prompt = self._build_predict_prompt(instance_type, region, stats, patterns)

        bedrock = self._get_bedrock_client()
        response = bedrock.converse(
            modelId=self.model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            **build_bedrock_converse_options(
                self.model_id,
                # No maxTokens by default — see get_recommendation.
                inference_config={"temperature": 0.2},
                apply_default_reasoning=self._uses_default_model,
            ),
        )
        text = extract_bedrock_converse_text(response)

        parsed: dict[str, Any] = {}
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                parsed = json.loads(text[start:end])
            except json.JSONDecodeError:
                parsed = {}
        return CapacityPredictionResult(
            instance_type=instance_type,
            region=region,
            best_windows=parsed.get("best_windows", []),
            avoid_windows=parsed.get("avoid_windows", []),
            reasoning=parsed.get("reasoning", ""),
            confidence=parsed.get("confidence", "low"),
            raw_response=text,
        )

    def predict_capacity_windows_all_regions(
        self,
        instance_type: str,
        hours_back: int = 168,
    ) -> list[CapacityPredictionResult]:
        """Predict acquisition windows for every region that has history.

        Discovers the regions with samples for ``instance_type`` via the history
        store's ``by-timestamp`` GSI and runs :meth:`predict_capacity_window`
        for each. Raises ``ValueError`` when no region has samples yet;
        propagates the underlying ``ClientError`` (e.g. ResourceNotFoundException)
        when the history table does not exist.
        """
        from cli.capacity.history import get_capacity_history_store

        store = get_capacity_history_store()
        regions = store.get_regions_with_data(instance_type, hours_back)
        if not regions:
            raise ValueError(
                f"No historical capacity samples for {instance_type} in any region yet. "
                "The poller records one about every 15 minutes once enabled."
            )
        results: list[CapacityPredictionResult] = []
        for region in regions:
            try:
                results.append(self.predict_capacity_window(instance_type, region, hours_back))
            except ValueError:
                continue
        return results


def get_bedrock_capacity_advisor(
    config: GCOConfig | None = None, model_id: str | None = None
) -> BedrockCapacityAdvisor:
    """Get a configured Bedrock capacity advisor instance."""
    return BedrockCapacityAdvisor(config, model_id)
