"""Bedrock-powered AI capacity advisor."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.exceptions import ClientError

from cli.config import GCOConfig, get_config

from .checker import CapacityChecker
from .multi_region import MultiRegionCapacityChecker, compute_price_trend

logger = logging.getLogger(__name__)


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


class BedrockCapacityAdvisor:
    """
    AI-powered capacity advisor using Amazon Bedrock.

    Gathers comprehensive capacity data and uses an LLM to provide
    intelligent recommendations for workload placement.

    DISCLAIMER: Recommendations are AI-generated and should be validated
    before making production decisions.
    """

    # Default model to use if none specified
    DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

    def __init__(self, config: GCOConfig | None = None, model_id: str | None = None):
        self.config = config or get_config()
        self._session = boto3.Session()
        self._capacity_checker = CapacityChecker(config)
        self._multi_region_checker = MultiRegionCapacityChecker(config)
        self.model_id = model_id or self.DEFAULT_MODEL

    def _get_bedrock_client(self) -> Any:
        """Get Bedrock runtime client."""
        return self._session.client("bedrock-runtime", region_name="us-east-1")

    def gather_capacity_data(
        self,
        instance_types: list[str] | None = None,
        regions: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Gather comprehensive capacity data for AI analysis.

        Args:
            instance_types: List of instance types to analyze (defaults to common GPU types)
            regions: List of regions to check (defaults to deployed GCO regions)

        Returns:
            Dictionary containing all gathered capacity data
        """
        from cli.aws_client import get_aws_client

        # Default to common GPU instance types if not specified
        if not instance_types:
            instance_types = [
                "g4dn.xlarge",
                "g4dn.2xlarge",
                "g4dn.4xlarge",
                "g5.xlarge",
                "g5.2xlarge",
                "g5.4xlarge",
                "p3.2xlarge",
                "p4d.24xlarge",
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
        for instance_type in instance_types:
            data["spot_data"][instance_type] = {}
            data["on_demand_data"][instance_type] = {}

            for region in regions:
                try:
                    # Get spot placement scores and prices
                    spot_scores = self._capacity_checker.get_spot_placement_score(
                        instance_type, region
                    )
                    spot_prices = self._capacity_checker.get_spot_price_history(
                        instance_type, region, days=7
                    )
                    on_demand_price = self._capacity_checker.get_on_demand_price(
                        instance_type, region
                    )

                    data["spot_data"][instance_type][region] = {
                        "placement_scores": spot_scores,
                        "prices": [
                            {
                                "az": p.availability_zone,
                                "current": p.current_price,
                                "avg_7d": p.avg_price_7d,
                                "stability": p.price_stability,
                            }
                            for p in spot_prices
                        ],
                    }

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

                    data["on_demand_data"][instance_type][region] = {
                        "price_per_hour": on_demand_price,
                        "available": self._capacity_checker.check_instance_available_in_region(
                            instance_type, region
                        ),
                    }
                except Exception as e:
                    logger.debug(
                        "Failed to gather capacity data for %s in %s: %s", instance_type, region, e
                    )

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
        prompt += "\n"

        # On-demand data summary
        prompt += "ON-DEMAND PRICING:\n"
        for instance_type, regions_data in capacity_data.get("on_demand_data", {}).items():
            prompt += f"  {instance_type}:\n"
            for region, od_info in regions_data.items():
                price = od_info.get("price_per_hour")
                available = od_info.get("available", False)
                prompt += f"    {region}: ${price:.4f}/hr" if price else f"    {region}: N/A"
                prompt += f" (Available: {available})\n"
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
                inferenceConfig={"maxTokens": 2048, "temperature": 0.1},
            )

            # Extract response text
            response_text = response["output"]["message"]["content"][0]["text"]

            # Parse JSON response
            # Find JSON in response (in case model adds extra text)
            json_start = response_text.find("{")
            json_end = response_text.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                json_str = response_text[json_start:json_end]
                result = json.loads(json_str)
            else:
                raise ValueError("No valid JSON found in response")

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
            raise RuntimeError(f"Failed to parse AI response as JSON: {e}") from e
        except Exception as e:
            raise RuntimeError(f"Failed to get AI recommendation: {e}") from e


def get_bedrock_capacity_advisor(
    config: GCOConfig | None = None, model_id: str | None = None
) -> BedrockCapacityAdvisor:
    """Get a configured Bedrock capacity advisor instance."""
    return BedrockCapacityAdvisor(config, model_id)
