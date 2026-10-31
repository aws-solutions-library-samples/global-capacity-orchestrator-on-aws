"""Preflight for the ``hyperpod`` block: what would make the deploy fail or the nodes never join.

``gco stacks hyperpod preflight`` and the hook on ``gco stacks deploy`` run
these checks for every Region whose block turns HyperPod on. Like the PCS
preflight (:mod:`cli.pcs_preflight`, whose check/report shapes this module
shares): an AWS error while checking is a warning, and only a fact that would
break the deploy is a failure.

Checks, per Region:

- ``agent-image``: SageMaker HyperPod publishes a health-monitoring agent
  image for the Region (the regional stack refuses to synthesize otherwise).
- ``instance-types``: every instance group's ``<type> for cluster usage``
  Service Quota covers its ``instance_count``; a type with no such quota is
  not a HyperPod type in this Region.
- ``training-plans``: every ``training_plan_arn`` names a plan that exists,
  is usable by a HyperPod cluster, is ``Active`` or ``Scheduled``, reserves
  the group's instance type and sits in the configured zone.
- ``availability-zone``: a configured zone exists in the Region.
- ``eks-cluster``: the regional EKS cluster, once deployed, is ``ACTIVE`` and
  authenticates with access entries (the nodes join through one).
- ``cluster-name``: no HyperPod cluster of the same name that this project
  does not own already exists in the Region.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

from gco.hyperpod_config import (
    HEALTH_MONITORING_AGENT_ACCOUNTS,
    HYPERPOD_FEATURE_KEY,
    HYPERPOD_OVERRIDES_CONTEXT_KEY,
    HYPERPOD_REGIONS_KEY,
    HyperpodConfigError,
    apply_hyperpod_overrides,
    cluster_name,
    validate_hyperpod_config,
)

from .pcs_preflight import (
    FAIL,
    PASS,
    SKIP,
    WARN,
    Check,
    ClientFactory,
    PcsPreflightError,
    _cdk_context,
    _Checker,
    _deep_merge,
    _error_text,
    project_name,
)

__all__ = [
    "FAIL",
    "PASS",
    "SKIP",
    "WARN",
    "Check",
    "HyperpodPreflightError",
    "HyperpodPreflightReport",
    "load_hyperpod_config",
    "project_name",
    "raw_hyperpod_config",
    "render_report",
    "run_preflight",
    "summarize",
]

#: Training-plan states an instance group can launch into.
_USABLE_PLAN_STATES = frozenset({"Active", "Scheduled"})
_HYPERPOD_TARGET = "hyperpod-cluster"


class HyperpodPreflightError(RuntimeError):
    """The block could not be read or validated; nothing was checked."""


@dataclass
class HyperpodPreflightReport:
    """Every check for one Region's block."""

    region: str
    enabled: bool
    cluster: str
    checks: list[Check] = field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if check.status == FAIL]

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "region": self.region,
            "enabled": self.enabled,
            "cluster": self.cluster,
            "ok": self.ok,
            "checks": [check.to_dict() for check in self.checks],
        }


# ---------------------------------------------------------------------------
# cdk.json
# ---------------------------------------------------------------------------


def raw_hyperpod_config(context: Mapping[str, Any], region: str) -> object:
    """The ``hyperpod`` block for ``region`` as the loader layers it (overlay, then overrides)."""
    global_raw = context.get(HYPERPOD_FEATURE_KEY)
    overlays = context.get(HYPERPOD_REGIONS_KEY)
    overrides = context.get(HYPERPOD_OVERRIDES_CONTEXT_KEY)
    if isinstance(overlays, Mapping) and isinstance(overlays.get(region), Mapping):
        base: Mapping[str, Any] = global_raw if isinstance(global_raw, Mapping) else {}
        return apply_hyperpod_overrides(_deep_merge(base, overlays[region]), overrides)
    return apply_hyperpod_overrides(global_raw, overrides)


def load_hyperpod_config(
    region: str, *, cdk_json_path: Path | None = None, force_enabled: bool = False
) -> dict[str, Any]:
    """The validated ``hyperpod`` block for ``region`` as the regional stack will read it.

    ``force_enabled`` mirrors the run-scoped ``--enable hyperpod`` override.
    """
    try:
        context = _cdk_context(cdk_json_path)
    except PcsPreflightError as exc:  # a missing or unparsable cdk.json
        raise HyperpodPreflightError(str(exc)) from exc
    try:
        config = validate_hyperpod_config(raw_hyperpod_config(context, region))
    except HyperpodConfigError as exc:
        raise HyperpodPreflightError(
            f"cdk.json hyperpod block is invalid for {region}: {exc}"
        ) from exc
    if force_enabled:
        config["enabled"] = True
    return config


# ---------------------------------------------------------------------------
# Live checks
# ---------------------------------------------------------------------------


def _check_agent_image(checker: _Checker) -> None:
    if checker.region in HEALTH_MONITORING_AGENT_ACCOUNTS:
        checker.add("agent-image", PASS, "health-monitoring agent image published for this Region")
        return
    checker.add(
        "agent-image",
        FAIL,
        f"SageMaker HyperPod publishes no health-monitoring agent for {checker.region}; "
        f"supported: {', '.join(sorted(HEALTH_MONITORING_AGENT_ACCOUNTS))}",
    )


def _cluster_usage_quotas(checker: _Checker) -> dict[str, float] | None:
    """``instance type -> applied quota`` for every ``<type> for cluster usage`` quota."""

    def collect() -> dict[str, float]:
        quotas: dict[str, float] = {}
        paginator = checker.client("service-quotas").get_paginator("list_service_quotas")
        for page in paginator.paginate(ServiceCode="sagemaker"):
            for quota in page.get("Quotas") or []:
                name = str(quota.get("QuotaName") or "")
                if name.endswith(" for cluster usage"):
                    quotas[name.removesuffix(" for cluster usage")] = float(quota.get("Value") or 0)
        return quotas

    result = checker.guarded("instance-types", collect)
    return dict(result) if isinstance(result, dict) else None


def _check_instance_types(checker: _Checker, config: Mapping[str, Any]) -> None:
    groups = list(config["instance_groups"])
    if not groups:
        checker.add("instance-types", SKIP, "no instance groups")
        return
    quotas = _cluster_usage_quotas(checker)
    if quotas is None:
        return  # the warning is already recorded
    wanted: dict[str, int] = {}
    for group in groups:
        wanted[str(group["instance_type"])] = wanted.get(str(group["instance_type"]), 0) + int(
            group["instance_count"]
        )
    for instance_type, count in wanted.items():
        quota = quotas.get(instance_type)
        if quota is None:
            checker.add(
                "instance-types",
                FAIL,
                f"{instance_type}: no '{instance_type} for cluster usage' quota in "
                f"{checker.region}; not a HyperPod instance type here",
            )
        elif count > quota:
            checker.add(
                "instance-types",
                FAIL,
                f"{instance_type}: instance groups ask for {count}, the account's "
                f"'for cluster usage' quota is {quota:g}; request an increase",
            )
        else:
            checker.add(
                "instance-types",
                PASS,
                f"{instance_type}: {count} of {quota:g} quota for cluster usage",
            )


def _plan_name(arn: str) -> str:
    return arn.rsplit("/", 1)[-1]


def _check_training_plans(checker: _Checker, config: Mapping[str, Any]) -> None:
    groups = [g for g in config["instance_groups"] if g.get("training_plan_arn")]
    if not groups:
        checker.add("training-plans", SKIP, "no instance group names a training plan")
        return
    sagemaker = checker.client("sagemaker")
    zone = config.get("availability_zone")
    for group in groups:
        arn = str(group["training_plan_arn"])
        name = _plan_name(arn)

        def describe(plan: str = name) -> dict[str, Any] | None:
            try:
                return dict(sagemaker.describe_training_plan(TrainingPlanName=plan))
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in (
                    "ResourceNotFound",
                    "ResourceNotFoundException",
                ):
                    return None
                raise

        recorded = len(checker.checks)
        plan = checker.guarded("training-plans", describe)
        if plan is None:
            if len(checker.checks) == recorded:  # no warning recorded: the plan is absent
                checker.add(
                    "training-plans", FAIL, f"{group['name']}: training plan {name} does not exist"
                )
            continue
        problems: list[str] = []
        status = str(plan.get("Status") or "")
        if status not in _USABLE_PLAN_STATES:
            problems.append(f"status {status or 'unknown'}")
        targets = [str(t) for t in plan.get("TargetResources") or []]
        if _HYPERPOD_TARGET not in targets:
            problems.append(f"targets {', '.join(targets) or 'nothing'}, not {_HYPERPOD_TARGET}")
        summaries = [dict(s) for s in plan.get("ReservedCapacitySummaries") or []]
        types = {str(s.get("InstanceType")) for s in summaries}
        if types and str(group["instance_type"]) not in types:
            problems.append(f"reserves {', '.join(sorted(types))}, not {group['instance_type']}")
        zones = {str(s.get("AvailabilityZone")) for s in summaries if s.get("AvailabilityZone")}
        if zone and zones and str(zone) not in zones:
            problems.append(f"sits in {', '.join(sorted(zones))}, not {zone}")
        if problems:
            checker.add("training-plans", FAIL, f"{group['name']}: {name} " + "; ".join(problems))
        else:
            checker.add(
                "training-plans",
                PASS,
                f"{group['name']}: {name} {status}, {group['instance_type']}"
                + (f" in {', '.join(sorted(zones))}" if zones else ""),
            )


def _check_availability_zone(checker: _Checker, config: Mapping[str, Any]) -> None:
    zone = config.get("availability_zone")
    if not zone:
        checker.add("availability-zone", SKIP, "none configured: the first EKS-supported subnet")
        return

    def describe() -> list[dict[str, Any]]:
        response = checker.client("ec2").describe_availability_zones(
            ZoneNames=[str(zone)], AllAvailabilityZones=False
        )
        return [dict(z) for z in response.get("AvailabilityZones") or []]

    try:
        zones = describe()
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "InvalidParameterValue":
            checker.add("availability-zone", FAIL, f"{zone} is not a zone of {checker.region}")
            return
        checker.add("availability-zone", WARN, f"could not check: {_error_text(exc)}")
        return
    except Exception as exc:  # BotoCoreError and friends
        checker.add("availability-zone", WARN, f"could not check: {_error_text(exc)}")
        return
    state = str(zones[0].get("State") or "") if zones else ""
    if state == "available":
        checker.add("availability-zone", PASS, f"{zone} is available")
    else:
        checker.add("availability-zone", FAIL, f"{zone}: {state or 'not found'}")


def _check_eks_cluster(checker: _Checker, eks_cluster_name: str) -> None:
    def describe() -> dict[str, Any] | None:
        try:
            response = checker.client("eks").describe_cluster(name=eks_cluster_name)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return None
            raise
        return dict(response.get("cluster") or {})

    cluster = checker.guarded("eks-cluster", describe)
    if cluster is None:
        if not any(c.name == "eks-cluster" for c in checker.checks):
            checker.add(
                "eks-cluster",
                SKIP,
                f"{eks_cluster_name} is not deployed yet; the deploy creates it and HyperPod joins it",
            )
        return
    status = str(cluster.get("status") or "")
    mode = str(((cluster.get("accessConfig") or {}).get("authenticationMode")) or "")
    if status != "ACTIVE":
        checker.add("eks-cluster", FAIL, f"{eks_cluster_name} is {status or 'unknown'}, not ACTIVE")
    elif mode not in ("API", "API_AND_CONFIG_MAP"):
        checker.add(
            "eks-cluster",
            FAIL,
            f"{eks_cluster_name} authenticates with {mode or 'unknown'}; HyperPod nodes join "
            "through an access entry (API or API_AND_CONFIG_MAP)",
        )
    else:
        checker.add("eks-cluster", PASS, f"{eks_cluster_name} ACTIVE, access entries {mode}")


def _check_cluster_name(checker: _Checker, name: str, project: str) -> None:
    sagemaker = checker.client("sagemaker")

    def find() -> dict[str, Any] | None:
        paginator = sagemaker.get_paginator("list_clusters")
        for page in paginator.paginate(NameContains=name):
            for summary in page.get("ClusterSummaries") or []:
                if str(summary.get("ClusterName")) == name:
                    return dict(summary)
        return None

    summary = checker.guarded("cluster-name", find)
    if summary is None:
        if not any(c.name == "cluster-name" for c in checker.checks):
            checker.add("cluster-name", PASS, f"{name} is free")
        return
    arn = str(summary.get("ClusterArn") or "")

    def tags() -> dict[str, str]:
        response = sagemaker.list_tags(ResourceArn=arn)
        return {str(t.get("Key")): str(t.get("Value")) for t in response.get("Tags") or []}

    owned = checker.guarded("cluster-name", tags)
    if owned is None:
        return
    if owned.get("gco:project") == project:
        checker.add(
            "cluster-name",
            PASS,
            f"{name} exists ({summary.get('ClusterStatus')}) and belongs to this project",
        )
    else:
        checker.add(
            "cluster-name",
            FAIL,
            f"{name} already exists in {checker.region} and this project does not own it; "
            "set hyperpod.cluster_name",
        )


def run_preflight(
    region: str,
    config: Mapping[str, Any],
    *,
    project: str,
    client_factory: ClientFactory = boto3.client,
) -> HyperpodPreflightReport:
    """Check the validated ``hyperpod`` block for ``region`` against the live account."""
    checker = _Checker(region, client_factory)
    name = cluster_name(config, project)
    _check_agent_image(checker)
    _check_instance_types(checker, config)
    _check_training_plans(checker, config)
    _check_availability_zone(checker, config)
    _check_eks_cluster(checker, f"{project}-{region}")
    _check_cluster_name(checker, name, project)
    return HyperpodPreflightReport(
        region=region, enabled=bool(config.get("enabled")), cluster=name, checks=checker.checks
    )


_MARKS = {PASS: "✓", FAIL: "✗", WARN: "!", SKIP: "-"}


def render_report(report: HyperpodPreflightReport) -> str:
    """The report as the terminal shows it."""
    lines = [
        f"\n  SageMaker HyperPod preflight — {report.region} "
        f"({'enabled' if report.enabled else 'disabled in cdk.json'}): {report.cluster}",
        "  " + "-" * 70,
    ]
    for check in report.checks:
        lines.append(f"  {_MARKS[check.status]} {check.name:<18} {check.detail}")
    if not report.checks:
        lines.append("  (nothing to check)")
    failures = report.failures
    lines.append("")
    lines.append(f"  {len(failures)} failure(s)" if failures else "  ready: no failures")
    return "\n".join(lines)


def summarize(reports: Iterable[HyperpodPreflightReport]) -> dict[str, Any]:
    """The machine-readable document ``--output json`` prints."""
    items = [report.to_dict() for report in reports]
    return {"ok": all(item["ok"] for item in items), "regions": items}
