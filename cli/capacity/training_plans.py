"""SageMaker Training Plans: the reserved capacity a HyperPod instance group launches into.

A Training Plan is the SageMaker counterpart of an EC2 Capacity Block: a fixed
window of accelerated instances bought up front. Two read-only lookups live
here, behind ``gco capacity find-training-plans`` and ``gco capacity
training-plans``:

- :func:`search_offerings` asks every requested Region what could be bought
  (``SearchTrainingPlanOfferings``) and ranks the answers cheapest per
  instance-hour first;
- :func:`list_plans` lists the plans the account already holds
  (``ListTrainingPlans``) with their reserved capacity.

Buying a plan is an account commitment and stays a console or
``aws sagemaker create-training-plan`` action; nothing here purchases. The
Region fan-out mirrors the Capacity Block search: one Region's error is
recorded and the others still answer, and only when every Region refuses does
the lookup raise.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from . import blocks

__all__ = [
    "TARGET_RESOURCES",
    "TrainingPlanError",
    "list_plans",
    "normalize_ml_instance_type",
    "search_offerings",
]

ClientFactory = Callable[..., Any]

#: What a plan's capacity can be used for; HyperPod clusters are GCO's case.
TARGET_RESOURCES: tuple[str, ...] = ("hyperpod-cluster", "training-job")
DEFAULT_TARGET = "hyperpod-cluster"
_MAX_WORKERS = 8


class TrainingPlanError(RuntimeError):
    """Every Region refused the lookup; the message carries SageMaker's reasons."""


def normalize_ml_instance_type(name: str) -> str:
    """The SageMaker spelling of an instance type: ``p5.48xlarge`` -> ``ml.p5.48xlarge``.

    Capacity Block aliases (``p6-b200``) expand first, so the two searches
    accept the same shorthand.
    """
    canonical, _note = blocks.normalize_instance_type(name)
    canonical = canonical.strip()
    if not canonical:
        raise ValueError("an instance type is required")
    return canonical if canonical.startswith("ml.") else f"ml.{canonical}"


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None


def _error_record(region: str, exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        return {
            "region": region,
            "error_code": str(error.get("Code") or "ClientError"),
            "error": str(error.get("Message") or exc),
        }
    return {"region": region, "error_code": type(exc).__name__, "error": str(exc)}


def _fan_out(
    regions: Iterable[str],
    probe: Callable[[str], list[dict[str, Any]]],
    *,
    what: str,
    max_workers: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run ``probe`` per Region in parallel; return ``(rows, errors)`` or raise when all fail."""
    targets = list(dict.fromkeys(regions))
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    workers = max(1, min(max_workers or _MAX_WORKERS, len(targets)))

    def _guarded(region: str) -> list[dict[str, Any]]:
        try:
            return probe(region)
        except (ClientError, BotoCoreError) as exc:
            errors.append(_error_record(region, exc))
            return []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_guarded, region) for region in targets]
        for future in as_completed(futures):
            rows.extend(future.result())
    errors.sort(key=lambda item: str(item["region"]))
    if targets and len(errors) == len(targets):
        reasons = "; ".join(f"{e['region']}: {e['error_code']}: {e['error']}" for e in errors)
        raise TrainingPlanError(f"SageMaker refused the {what} in every Region ({reasons})")
    return rows, errors


# ---------------------------------------------------------------------------
# Offerings
# ---------------------------------------------------------------------------


def _normalize_offering(region: str, offering: dict[str, Any]) -> dict[str, Any]:
    capacity = [
        {
            "instance_type": str(item.get("InstanceType") or ""),
            "instance_count": int(item.get("InstanceCount") or 0),
            "availability_zone": item.get("AvailabilityZone"),
            "duration_hours": item.get("DurationHours"),
            "start_time": _iso(item.get("StartTime")),
            "end_time": _iso(item.get("EndTime")),
            "type": item.get("ReservedCapacityType"),
        }
        for item in offering.get("ReservedCapacityOfferings") or []
    ]
    fee = blocks.parse_upfront_fee(offering.get("UpfrontFee"))
    hours = offering.get("DurationHours")
    instances = sum(item["instance_count"] for item in capacity)
    per_instance_hour = (
        round(fee / (instances * int(hours)), 4)
        if fee is not None and instances and hours
        else None
    )
    starts = sorted(s for s in (item["start_time"] for item in capacity) if s)
    ends = sorted(e for e in (item["end_time"] for item in capacity) if e)
    return {
        "region": region,
        "offering_id": str(offering.get("TrainingPlanOfferingId") or ""),
        "target_resources": [str(t) for t in offering.get("TargetResources") or []],
        "duration_hours": hours,
        "duration_days": blocks.hours_to_days(int(hours)) if hours else None,
        "upfront_fee_usd": fee,
        "currency": offering.get("CurrencyCode"),
        "instance_count": instances,
        "availability_zones": sorted(
            {str(c["availability_zone"]) for c in capacity if c.get("availability_zone")}
        ),
        "start_time": starts[0] if starts else None,
        "end_time": ends[-1] if ends else None,
        "price_per_instance_hour": per_instance_hour,
        "reserved_capacity": capacity,
    }


def _offering_rank(offering: dict[str, Any]) -> tuple[float, str, str]:
    price = offering.get("price_per_instance_hour")
    return (
        float(price) if isinstance(price, int | float) else float("inf"),
        str(offering.get("start_time") or "9999"),
        str(offering.get("region") or ""),
    )


def search_offerings(
    instance_type: str,
    *,
    regions: list[str],
    instance_count: int = 1,
    duration_hours: int | None = None,
    start_after: str | datetime | None = None,
    end_before: str | datetime | None = None,
    target: str = DEFAULT_TARGET,
    client_factory: ClientFactory = boto3.client,
    max_workers: int | None = None,
) -> dict[str, Any]:
    """Training Plan offerings for ``instance_count`` x ``instance_type`` in each Region.

    ``duration_hours`` and the ``start_after`` / ``end_before`` window
    (``YYYY-MM-DD`` or ISO timestamps; a date for ``end_before`` means the
    whole of that day) narrow the search the way the console does. Offerings
    are ranked cheapest per instance-hour first, then by earliest start.
    """
    if target not in TARGET_RESOURCES:
        raise ValueError(f"target must be one of {', '.join(TARGET_RESOURCES)}, got {target!r}")
    if not regions:
        raise ValueError("at least one Region is required")
    ml_type = normalize_ml_instance_type(instance_type)
    after = blocks.parse_date_input(start_after)
    before = blocks.parse_date_input(end_before, through_end_of_day=True)
    request: dict[str, Any] = {
        "InstanceType": ml_type,
        "InstanceCount": int(instance_count),
        "TargetResources": [target],
    }
    if duration_hours is not None:
        request["DurationHours"] = int(duration_hours)
    if after is not None:
        request["StartTimeAfter"] = after
    if before is not None:
        request["EndTimeBefore"] = before

    def probe(region: str) -> list[dict[str, Any]]:
        sagemaker = client_factory("sagemaker", region_name=region)
        response = sagemaker.search_training_plan_offerings(**request)
        return [
            _normalize_offering(region, offering)
            for offering in response.get("TrainingPlanOfferings") or []
        ]

    offerings, errors = _fan_out(
        regions, probe, what="Training Plan offering search", max_workers=max_workers
    )
    ranked = sorted(offerings, key=_offering_rank)
    return {
        "instance_type": ml_type,
        "requested_instance_type": instance_type,
        "instance_count": int(instance_count),
        "target": target,
        "duration_hours": duration_hours,
        "window": {
            "start_after": after.isoformat() if after else None,
            "end_before": before.isoformat() if before else None,
        },
        "regions_checked": list(dict.fromkeys(regions)),
        "regions_with_offerings": sorted({o["region"] for o in offerings}),
        "offerings_found": len(ranked),
        "offerings": ranked,
        "best": ranked[0] if ranked else None,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Plans the account holds
# ---------------------------------------------------------------------------


def _normalize_plan(region: str, plan: dict[str, Any]) -> dict[str, Any]:
    capacity = [
        {
            "instance_type": str(item.get("InstanceType") or ""),
            "total_instances": int(item.get("TotalInstanceCount") or 0),
            "availability_zone": item.get("AvailabilityZone"),
            "status": item.get("Status"),
            "start_time": _iso(item.get("StartTime")),
            "end_time": _iso(item.get("EndTime")),
        }
        for item in plan.get("ReservedCapacitySummaries") or []
    ]
    return {
        "region": region,
        "arn": str(plan.get("TrainingPlanArn") or ""),
        "name": str(plan.get("TrainingPlanName") or ""),
        "status": str(plan.get("Status") or ""),
        "status_message": plan.get("StatusMessage"),
        "target_resources": [str(t) for t in plan.get("TargetResources") or []],
        "duration_hours": plan.get("DurationHours"),
        "start_time": _iso(plan.get("StartTime")),
        "end_time": _iso(plan.get("EndTime")),
        "upfront_fee_usd": blocks.parse_upfront_fee(plan.get("UpfrontFee")),
        "currency": plan.get("CurrencyCode"),
        "total_instances": int(plan.get("TotalInstanceCount") or 0),
        "available_instances": int(plan.get("AvailableInstanceCount") or 0),
        "in_use_instances": int(plan.get("InUseInstanceCount") or 0),
        "instance_types": sorted({c["instance_type"] for c in capacity if c["instance_type"]}),
        "availability_zones": sorted(
            {str(c["availability_zone"]) for c in capacity if c.get("availability_zone")}
        ),
        "reserved_capacity": capacity,
    }


def list_plans(
    *,
    regions: list[str],
    instance_type: str | None = None,
    status: str | None = None,
    client_factory: ClientFactory = boto3.client,
    max_workers: int | None = None,
) -> dict[str, Any]:
    """The Training Plans the account holds in each Region, soonest start first.

    ``status`` filters server-side (``Pending``, ``Active``, ``Scheduled``,
    ``Expired``, ``Failed``); ``instance_type`` keeps the plans whose reserved
    capacity includes that type.
    """
    if not regions:
        raise ValueError("at least one Region is required")
    ml_type = normalize_ml_instance_type(instance_type) if instance_type else None
    request: dict[str, Any] = {}
    if status:
        request["Filters"] = [{"Name": "Status", "Value": status}]

    def probe(region: str) -> list[dict[str, Any]]:
        sagemaker = client_factory("sagemaker", region_name=region)
        paginator = sagemaker.get_paginator("list_training_plans")
        rows: list[dict[str, Any]] = []
        for page in paginator.paginate(**request):
            for summary in page.get("TrainingPlanSummaries") or []:
                plan = _normalize_plan(region, summary)
                if ml_type is None or ml_type in plan["instance_types"]:
                    rows.append(plan)
        return rows

    plans, errors = _fan_out(regions, probe, what="Training Plan listing", max_workers=max_workers)
    plans.sort(key=lambda p: (str(p.get("start_time") or "9999"), p["region"], p["name"]))
    return {
        "instance_type": ml_type,
        "status": status,
        "regions_checked": list(dict.fromkeys(regions)),
        "plans_found": len(plans),
        "total_instances": sum(p["total_instances"] for p in plans),
        "available_instances": sum(p["available_instances"] for p in plans),
        "plans": plans,
        "errors": errors,
    }
