"""Cost visibility for GCO workloads.

Uses AWS Cost Explorer for historical spend and the Pricing API
for real-time cost estimates on running workloads.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3

from .config import GCOConfig, get_config

logger = logging.getLogger(__name__)

# Cost allocation tag keys GCO reporting relies on. ``Project`` is the
# user-defined tag applied to every CDK-managed resource (cdk.json
# ``context.tags``) and is the key every Cost Explorer query in this module
# filters on; ``aws:eks:cluster-name`` is the AWS-generated tag EKS Auto
# Mode stamps on the EC2 capacity it launches outside CloudFormation, which
# is what makes per-cluster attribution possible for Karpenter-provisioned
# GPU spend.
DEFAULT_COST_ALLOCATION_TAG_KEYS: tuple[str, ...] = ("Project", "aws:eks:cluster-name")

# UpdateCostAllocationTagsStatus accepts at most 20 entries per call and
# ListCostAllocationTags at most 20 TagKeys filters; batch both.
_COST_ALLOCATION_TAG_BATCH_SIZE = 20


@dataclass
class ResourceCost:
    """Cost for a single resource or service."""

    service: str
    amount: float
    currency: str = "USD"
    region: str | None = None
    detail: str | None = None


@dataclass
class CostSummary:
    """Aggregated cost summary."""

    total: float
    currency: str = "USD"
    period_start: str = ""
    period_end: str = ""
    by_service: list[ResourceCost] = field(default_factory=list)
    by_region: dict[str, float] = field(default_factory=dict)


@dataclass
class WorkloadCost:
    """Estimated cost for a running workload."""

    name: str
    workload_type: str  # "job" or "inference"
    instance_type: str
    gpu_count: int
    hourly_rate: float
    runtime_hours: float
    estimated_cost: float
    region: str
    status: str


class CostTracker:
    """Track and estimate costs for GCO resources."""

    def __init__(self, config: GCOConfig | None = None):
        self._config = config
        self._session = boto3.Session()
        self._pricing_cache: dict[str, float | None] = {}

    def get_cost_summary(
        self,
        days: int = 30,
        granularity: str = "MONTHLY",
        unfiltered: bool = False,
    ) -> CostSummary:
        """Get cost summary from Cost Explorer filtered by GCO tags."""
        ce = self._session.client("ce", region_name="us-east-1")

        end = datetime.now(UTC).date()
        start = end - timedelta(days=days)

        kwargs: dict[str, Any] = {
            "TimePeriod": {
                "Start": start.isoformat(),
                "End": end.isoformat(),
            },
            "Granularity": granularity,
            "Metrics": ["UnblendedCost"],
            "GroupBy": [
                {"Type": "DIMENSION", "Key": "SERVICE"},
            ],
        }

        if not unfiltered:
            kwargs["Filter"] = {
                "Tags": {
                    "Key": "Project",
                    "Values": ["GCO"],
                }
            }

        try:
            response = ce.get_cost_and_usage(**kwargs)
        except Exception as e:
            raise RuntimeError(f"Cost Explorer query failed: {e}") from e

        summary = CostSummary(
            total=0.0,
            period_start=start.isoformat(),
            period_end=end.isoformat(),
        )

        for result in response.get("ResultsByTime", []):
            for group in result.get("Groups", []):
                service = group["Keys"][0]
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                if amount > 0.001:
                    summary.by_service.append(ResourceCost(service=service, amount=amount))
                    summary.total += amount

        # Sort by cost descending
        summary.by_service.sort(key=lambda x: x.amount, reverse=True)

        return summary

    def get_cost_by_region(self, days: int = 30) -> dict[str, float]:
        """Get cost breakdown by region."""
        ce = self._session.client("ce", region_name="us-east-1")

        end = datetime.now(UTC).date()
        start = end - timedelta(days=days)

        try:
            response = ce.get_cost_and_usage(
                TimePeriod={
                    "Start": start.isoformat(),
                    "End": end.isoformat(),
                },
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
                Filter={
                    "Tags": {
                        "Key": "Project",
                        "Values": ["GCO"],
                    }
                },
                GroupBy=[
                    {"Type": "DIMENSION", "Key": "REGION"},
                ],
            )
        except Exception as e:
            raise RuntimeError(f"Cost Explorer query failed: {e}") from e

        by_region: dict[str, float] = {}
        for result in response.get("ResultsByTime", []):
            for group in result.get("Groups", []):
                region = group["Keys"][0]
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                if amount > 0.001:
                    by_region[region] = by_region.get(region, 0) + amount

        return dict(sorted(by_region.items(), key=lambda x: x[1], reverse=True))

    def get_daily_trend(self, days: int = 14, unfiltered: bool = False) -> list[dict[str, Any]]:
        """Get daily cost trend."""
        ce = self._session.client("ce", region_name="us-east-1")

        end = datetime.now(UTC).date()
        start = end - timedelta(days=days)

        kwargs: dict[str, Any] = {
            "TimePeriod": {
                "Start": start.isoformat(),
                "End": end.isoformat(),
            },
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost"],
        }

        if not unfiltered:
            kwargs["Filter"] = {
                "Tags": {
                    "Key": "Project",
                    "Values": ["GCO"],
                }
            }

        try:
            response = ce.get_cost_and_usage(**kwargs)
        except Exception as e:
            raise RuntimeError(f"Cost Explorer query failed: {e}") from e

        trend = []
        for result in response.get("ResultsByTime", []):
            date = result["TimePeriod"]["Start"]
            amount = float(result["Total"]["UnblendedCost"]["Amount"])
            trend.append({"date": date, "amount": amount})

        return trend

    def estimate_running_workloads(self, region: str) -> list[WorkloadCost]:
        """Estimate costs for currently running workloads in a region."""
        try:
            from .capacity import get_capacity_checker
        except ImportError:
            return []

        checker = get_capacity_checker(self._config)
        estimates: list[WorkloadCost] = []

        # Get running pods from EKS
        try:
            project_name = (self._config or get_config()).project_name
            cluster_name = f"{project_name}-{region}"

            from .kubectl_helpers import update_kubeconfig

            update_kubeconfig(cluster_name, region)

            from kubernetes import client as k8s_client
            from kubernetes import config as k8s_config

            k8s_config.load_kube_config()
            v1 = k8s_client.CoreV1Api()

            # Check inference namespace
            for ns in ["gco-inference", "gco-jobs"]:
                try:
                    pods = v1.list_namespaced_pod(namespace=ns)
                except Exception as e:
                    logger.debug("Failed to list pods in %s: %s", ns, e)
                    continue

                for pod in pods.items:
                    if pod.status.phase not in ("Running", "Pending"):
                        continue

                    name = pod.metadata.name
                    gpu_count = 0
                    instance_type = "unknown"

                    # Get GPU requests
                    for container in pod.spec.containers or []:
                        requests = container.resources.requests or {}
                        gpu_req = requests.get(  # nosec B113 - dict.get(), not HTTP requests
                            "nvidia.com/gpu", "0"
                        )
                        gpu_count += int(gpu_req)

                    # Get node instance type
                    if pod.spec.node_name:
                        try:
                            node = v1.read_node(pod.spec.node_name)
                            instance_type = node.metadata.labels.get(
                                "node.kubernetes.io/instance-type", "unknown"
                            )
                        except Exception as e:
                            logger.debug(
                                "Failed to get node info for %s: %s", pod.spec.node_name, e
                            )

                    # Calculate cost
                    hourly_rate = checker.get_on_demand_price(instance_type, region) or 0.0

                    # Calculate runtime
                    start_time = pod.status.start_time
                    if start_time:
                        runtime = datetime.now(UTC) - start_time
                        runtime_hours = runtime.total_seconds() / 3600
                    else:
                        runtime_hours = 0.0

                    workload_type = "inference" if ns == "gco-inference" else "job"

                    estimates.append(
                        WorkloadCost(
                            name=name,
                            workload_type=workload_type,
                            instance_type=instance_type,
                            gpu_count=gpu_count,
                            hourly_rate=hourly_rate,
                            runtime_hours=round(runtime_hours, 2),
                            estimated_cost=round(hourly_rate * runtime_hours, 4),
                            region=region,
                            status=pod.status.phase,
                        )
                    )

        except Exception as e:
            logger.debug("Failed to estimate workload costs: %s", e)

        return estimates

    def get_forecast(self, days_ahead: int = 30) -> dict[str, Any]:
        """Get cost forecast for the next N days."""
        ce = self._session.client("ce", region_name="us-east-1")

        start = datetime.now(UTC).date()
        end = start + timedelta(days=days_ahead)

        try:
            response = ce.get_cost_forecast(
                TimePeriod={
                    "Start": start.isoformat(),
                    "End": end.isoformat(),
                },
                Metric="UNBLENDED_COST",
                Granularity="MONTHLY",
                Filter={
                    "Tags": {
                        "Key": "Project",
                        "Values": ["GCO"],
                    }
                },
            )

            return {
                "forecast_total": float(response.get("Total", {}).get("Amount", 0)),
                "period_start": start.isoformat(),
                "period_end": end.isoformat(),
            }
        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Cost allocation tags — the switches behind every query above.
    #
    # Every Cost Explorer query in this module filters on the ``Project``
    # tag, but that filter only sees spend if the tag key is *activated*
    # as a cost allocation tag in the billing account. Activation is a
    # separate account-level switch from tagging the resources, and the
    # AWS-generated ``aws:eks:cluster-name`` key (stamped by EKS Auto
    # Mode on the EC2 instances, volumes, and load balancers it launches
    # outside CloudFormation) adds per-cluster attribution for the
    # compute that dominates GCO spend. In an AWS Organization only the
    # management (payer) account may activate keys.
    # ------------------------------------------------------------------

    def get_cost_allocation_tag_status(
        self, tag_keys: Sequence[str] | None = None
    ) -> list[dict[str, str]]:
        """Report cost-allocation status for each tag key, in request order.

        A key that Billing has never seen on billing data is absent from
        ``ListCostAllocationTags``; it is reported here with status
        ``NotFound``. New keys appear up to 24 hours after they are first
        used on a resource that accrues cost.
        """
        keys = list(tag_keys) if tag_keys else list(DEFAULT_COST_ALLOCATION_TAG_KEYS)
        ce = self._session.client("ce", region_name="us-east-1")
        found: dict[str, dict[str, str]] = {}
        for start in range(0, len(keys), _COST_ALLOCATION_TAG_BATCH_SIZE):
            batch = keys[start : start + _COST_ALLOCATION_TAG_BATCH_SIZE]
            token: str | None = None
            while True:
                kwargs: dict[str, Any] = {"TagKeys": batch, "MaxResults": 100}
                if token:
                    kwargs["NextToken"] = token
                response = ce.list_cost_allocation_tags(**kwargs)
                for tag in response.get("CostAllocationTags", []):
                    key = str(tag.get("TagKey", ""))
                    found[key] = {
                        "tag_key": key,
                        "type": str(tag.get("Type", "")),
                        "status": str(tag.get("Status", "")),
                        "last_updated": str(tag.get("LastUpdatedDate", "") or ""),
                        "last_used": str(tag.get("LastUsedDate", "") or ""),
                    }
                token = response.get("NextToken")
                if not token:
                    break
        return [
            found.get(
                key,
                {
                    "tag_key": key,
                    "type": "Unknown",
                    "status": "NotFound",
                    "last_updated": "",
                    "last_used": "",
                },
            )
            for key in keys
        ]

    def activate_cost_allocation_tags(
        self, tag_keys: Sequence[str] | None = None
    ) -> dict[str, Any]:
        """Activate tag keys for cost allocation; report per-key outcomes.

        Activation is idempotent (re-activating an active key succeeds)
        and only affects billing data from activation onward — use
        :meth:`start_cost_allocation_tag_backfill` to re-tag past usage.
        Returns ``{"activated": [keys], "errors": [{tag_key, code,
        message}]}`` without raising on per-key failures, so one
        not-yet-discovered key cannot mask the others succeeding.
        """
        keys = list(tag_keys) if tag_keys else list(DEFAULT_COST_ALLOCATION_TAG_KEYS)
        ce = self._session.client("ce", region_name="us-east-1")
        activated: list[str] = []
        errors: list[dict[str, str]] = []
        for start in range(0, len(keys), _COST_ALLOCATION_TAG_BATCH_SIZE):
            batch = keys[start : start + _COST_ALLOCATION_TAG_BATCH_SIZE]
            response = ce.update_cost_allocation_tags_status(
                CostAllocationTagsStatus=[{"TagKey": key, "Status": "Active"} for key in batch]
            )
            failed_keys: set[str] = set()
            for error in response.get("Errors", []):
                key = str(error.get("TagKey", ""))
                failed_keys.add(key)
                errors.append(
                    {
                        "tag_key": key,
                        "code": str(error.get("Code", "")),
                        "message": str(error.get("Message", "")),
                    }
                )
            activated.extend(key for key in batch if key not in failed_keys)
        return {"activated": activated, "errors": errors}

    def start_cost_allocation_tag_backfill(self, backfill_from: str) -> dict[str, Any]:
        """Ask Billing to re-tag historical usage back to ``backfill_from``.

        ``backfill_from`` is an ISO-8601 timestamp (Billing accepts up to
        12 months back, quarter-start aligned). Returns the request record
        with its ``BackfillStatus``.
        """
        ce = self._session.client("ce", region_name="us-east-1")
        response = ce.start_cost_allocation_tag_backfill(BackfillFrom=backfill_from)
        request = response.get("BackfillRequest", {}) or {}
        return {
            "backfill_from": str(request.get("BackfillFrom", "")),
            "requested_at": str(request.get("RequestedAt", "")),
            "completed_at": str(request.get("CompletedAt", "") or ""),
            "status": str(request.get("BackfillStatus", "")),
        }

    def get_cost_allocation_backfill_history(self) -> list[dict[str, Any]]:
        """List prior backfill requests, newest first per the service order."""
        ce = self._session.client("ce", region_name="us-east-1")
        history: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {}
            if token:
                kwargs["NextToken"] = token
            response = ce.list_cost_allocation_tag_backfill_history(**kwargs)
            for request in response.get("BackfillRequests", []):
                history.append(
                    {
                        "backfill_from": str(request.get("BackfillFrom", "")),
                        "requested_at": str(request.get("RequestedAt", "")),
                        "completed_at": str(request.get("CompletedAt", "") or ""),
                        "status": str(request.get("BackfillStatus", "")),
                    }
                )
            token = response.get("NextToken")
            if not token:
                break
        return history


def get_cost_tracker(config: GCOConfig | None = None) -> CostTracker:
    """Factory function for CostTracker."""
    return CostTracker(config=config)
