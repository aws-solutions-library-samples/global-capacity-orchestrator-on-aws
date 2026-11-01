"""Tests for ``cli/hyperpod_preflight.py``, the SageMaker HyperPod preflight.

Real botocore clients (Service Quotas, SageMaker, EC2, EKS) run behind
``Stubber`` so every call and its parameters are asserted without a network.
The block under test comes from ``validate_hyperpod_config`` so the checks see
exactly what the regional stack would.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from botocore.stub import Stubber

from cli import hyperpod_preflight as pf
from cli.pcs_preflight import _Checker
from gco.hyperpod_config import HEALTH_MONITORING_AGENT_ACCOUNTS, validate_hyperpod_config

_REGION = "us-east-1"
_PROJECT = "gco-test"
_EKS_NAME = f"{_PROJECT}-{_REGION}"
_CLUSTER_NAME = f"{_PROJECT}-hyperpod"
_CLUSTER_ARN = f"arn:aws:sagemaker:{_REGION}:123456789012:cluster/abc123"
_PLAN_NAME = "p5-plan"
_PLAN_ARN = f"arn:aws:sagemaker:{_REGION}:123456789012:training-plan/{_PLAN_NAME}"
_ZONE = "us-east-1a"
_NOW = datetime(2026, 10, 1, tzinfo=UTC)
#: The minor the cluster runs in these tests, and the minors HyperPod's AMI tree carries.
_K8S = "1.36"
_AMI_MINORS = ("1.34", "1.35", "1.36")


class _Clients:
    def __init__(self) -> None:
        kwargs = {
            "region_name": _REGION,
            "aws_access_key_id": "testing",
            "aws_secret_access_key": "testing",  # nosec B106  # fake credentials for stubbed clients
        }
        self.clients = {
            service: boto3.client(service, **kwargs)
            for service in ("service-quotas", "sagemaker", "ec2", "eks", "ssm")
        }
        self.stubs = {service: Stubber(client) for service, client in self.clients.items()}
        self.requested: list[str] = []

    def factory(self, service: str, *, region_name: str) -> Any:
        assert region_name == _REGION
        self.requested.append(service)
        return self.clients[service]

    def __getitem__(self, service: str) -> Stubber:
        return self.stubs[service]


@pytest.fixture
def clients() -> Iterator[_Clients]:
    stubbed = _Clients()
    for stub in stubbed.stubs.values():
        stub.activate()
    yield stubbed
    for stub in stubbed.stubs.values():
        stub.assert_no_pending_responses()
        stub.deactivate()


def _group(**overrides: Any) -> dict[str, Any]:
    return {
        "name": "accelerated",
        "instance_type": "ml.g5.8xlarge",
        "instance_count": 2,
        **overrides,
    }


def _config(**overrides: Any) -> dict[str, Any]:
    return validate_hyperpod_config({"enabled": True, "instance_groups": [_group()], **overrides})


def _by_name(report: pf.HyperpodPreflightReport, name: str) -> list[pf.Check]:
    return [check for check in report.checks if check.name == name]


# ---------------------------------------------------------------------------
# Stub helpers: one per AWS call the checks make, in the order run_preflight makes them
# ---------------------------------------------------------------------------


def _quota(instance_type: str, value: float) -> dict[str, Any]:
    return {
        "ServiceCode": "sagemaker",
        "QuotaName": f"{instance_type} for cluster usage",
        "Value": value,
    }


def _expect_quotas(clients: _Clients, *pages: list[dict[str, Any]]) -> None:
    tokens = [None, *[f"token-{i}" for i in range(1, len(pages))]]
    for index, (page, token) in enumerate(zip(pages, tokens, strict=True)):
        response: dict[str, Any] = {"Quotas": page}
        if index + 1 < len(pages):
            response["NextToken"] = tokens[index + 1]
        expected: dict[str, Any] = {"ServiceCode": "sagemaker"}
        if token:
            expected["NextToken"] = token
        clients["service-quotas"].add_response("list_service_quotas", response, expected)


def _expect_default_quotas(clients: _Clients) -> None:
    _expect_quotas(
        clients, [_quota("ml.g5.8xlarge", 4), {"QuotaName": "Something else", "Value": 1}]
    )


def _capacity(instance_type: str, zone: str | None = None) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "ReservedCapacityArn": f"{_PLAN_ARN}/rc-{instance_type}",
        "InstanceType": instance_type,
        "TotalInstanceCount": 2,
        "Status": "Active",
    }
    if zone:
        summary["AvailabilityZone"] = zone
    return summary


def _plan(**overrides: Any) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "TrainingPlanArn": _PLAN_ARN,
        "TrainingPlanName": _PLAN_NAME,
        "Status": "Active",
        "TargetResources": ["hyperpod-cluster"],
        "ReservedCapacitySummaries": [_capacity("ml.g5.8xlarge", _ZONE)],
    }
    plan.update(overrides)
    return plan


def _expect_plan(clients: _Clients, plan: dict[str, Any] | None, *, name: str = _PLAN_NAME) -> None:
    if plan is None:
        clients["sagemaker"].add_client_error(
            "describe_training_plan",
            service_error_code="ResourceNotFound",
            expected_params={"TrainingPlanName": name},
        )
        return
    clients["sagemaker"].add_response("describe_training_plan", plan, {"TrainingPlanName": name})


def _expect_zone(clients: _Clients, state: str | None = "available") -> None:
    zones = [] if state is None else [{"ZoneName": _ZONE, "State": state}]
    clients["ec2"].add_response(
        "describe_availability_zones",
        {"AvailabilityZones": zones},
        {"ZoneNames": [_ZONE], "AllAvailabilityZones": False},
    )


def _expect_eks(
    clients: _Clients, *, status: str | None = "ACTIVE", mode: str = "API_AND_CONFIG_MAP"
) -> None:
    if status is None:
        clients["eks"].add_client_error(
            "describe_cluster",
            service_error_code="ResourceNotFoundException",
            expected_params={"name": _EKS_NAME},
        )
        return
    clients["eks"].add_response(
        "describe_cluster",
        {
            "cluster": {
                "name": _EKS_NAME,
                "status": status,
                "accessConfig": {"authenticationMode": mode},
            }
        },
        {"name": _EKS_NAME},
    )


def _expect_clusters(clients: _Clients, summaries: list[dict[str, Any]]) -> None:
    clients["sagemaker"].add_response(
        "list_clusters", {"ClusterSummaries": summaries}, {"NameContains": _CLUSTER_NAME}
    )


def _summary(name: str = _CLUSTER_NAME) -> dict[str, Any]:
    return {
        "ClusterArn": _CLUSTER_ARN,
        "ClusterName": name,
        "ClusterStatus": "InService",
        "CreationTime": _NOW,
    }


def _expect_tags(clients: _Clients, tags: dict[str, str]) -> None:
    clients["sagemaker"].add_response(
        "list_tags",
        {"Tags": [{"Key": key, "Value": value} for key, value in tags.items()]},
        {"ResourceArn": _CLUSTER_ARN},
    )


def _ami_parameter(minor: str, flavor: str = "amazon-linux-2023") -> dict[str, Any]:
    return {
        "Name": f"{pf.HYPERPOD_AMI_PARAMETER_ROOT}/eks-{minor}-{flavor}/latest/ami-id",
        "Type": "String",
        "Value": "ami-0123456789abcdef0",
    }


def _expect_ami_tree(clients: _Clients, *pages: list[dict[str, Any]]) -> None:
    tokens = [None, *[f"ami-token-{i}" for i in range(1, len(pages))]]
    for index, (page, token) in enumerate(zip(pages, tokens, strict=True)):
        response: dict[str, Any] = {"Parameters": page}
        if index + 1 < len(pages):
            response["NextToken"] = tokens[index + 1]
        expected: dict[str, Any] = {"Path": pf.HYPERPOD_AMI_PARAMETER_ROOT, "Recursive": True}
        if token:
            expected["NextToken"] = token
        clients["ssm"].add_response("get_parameters_by_path", response, expected)


def _expect_default_ami_tree(clients: _Clients) -> None:
    _expect_ami_tree(
        clients,
        [
            *[_ami_parameter(minor) for minor in _AMI_MINORS],
            {
                "Name": f"{pf.HYPERPOD_AMI_PARAMETER_ROOT}/slurm-ubuntu-22.04/latest/ami-id",
                "Type": "String",
                "Value": "ami-0fedcba9876543210",
            },
        ],
    )


def _expect_happy_path(clients: _Clients, *, zone: bool = False, plan: bool = False) -> None:
    _expect_default_quotas(clients)
    if plan:
        _expect_plan(clients, _plan())
    if zone:
        _expect_zone(clients)
    _expect_eks(clients)
    _expect_clusters(clients, [])


def _run(
    clients: _Clients,
    config: dict[str, Any],
    *,
    kubernetes_version: str = _K8S,
    ami_tree: bool = True,
) -> pf.HyperpodPreflightReport:
    """Run the preflight; by default the AMI tree carries ``_AMI_MINORS`` (one SSM page)."""
    if ami_tree:
        _expect_default_ami_tree(clients)
    return pf.run_preflight(
        _REGION,
        config,
        project=_PROJECT,
        kubernetes_version=kubernetes_version,
        client_factory=clients.factory,
    )


# ---------------------------------------------------------------------------
# cdk.json
# ---------------------------------------------------------------------------


def _write_cdk_json(tmp_path: Path, context: dict[str, Any]) -> Path:
    path = tmp_path / "cdk.json"
    path.write_text(json.dumps({"app": "python app.py", "context": context}), encoding="utf-8")
    return path


def test_raw_config_layers_the_regional_overlay_then_the_overrides() -> None:
    context = {
        "hyperpod": {"enabled": False, "node_recovery": "None"},
        "hyperpod_regions": {_REGION: {"enabled": True}},
        "hyperpod_overrides": json.dumps({"node_recovery": "Automatic"}),
    }
    assert pf.raw_hyperpod_config(context, _REGION) == {
        "enabled": True,
        "node_recovery": "Automatic",
    }
    # Another Region sees only the block plus the overrides.
    assert pf.raw_hyperpod_config(context, "eu-west-1") == {
        "enabled": False,
        "node_recovery": "Automatic",
    }


def test_raw_config_overlay_over_a_missing_block_and_no_overrides() -> None:
    context = {"hyperpod_regions": {_REGION: {"enabled": True}}}
    assert pf.raw_hyperpod_config(context, _REGION) == {"enabled": True}
    assert pf.raw_hyperpod_config({}, _REGION) is None


def test_load_config_validates_and_force_enables(tmp_path: Path) -> None:
    path = _write_cdk_json(tmp_path, {"hyperpod": {"enabled": False}})
    config = pf.load_hyperpod_config(_REGION, cdk_json_path=path)
    assert config["enabled"] is False
    assert config["instance_groups"][0]["instance_type"] == "ml.g5.8xlarge"
    forced = pf.load_hyperpod_config(_REGION, cdk_json_path=path, force_enabled=True)
    assert forced["enabled"] is True


def test_load_config_names_an_invalid_block(tmp_path: Path) -> None:
    path = _write_cdk_json(tmp_path, {"hyperpod": {"enabled": "yes"}})
    with pytest.raises(
        pf.HyperpodPreflightError, match=rf"invalid for {_REGION}: hyperpod\.enabled"
    ):
        pf.load_hyperpod_config(_REGION, cdk_json_path=path)


def test_load_config_missing_and_broken_cdk_json_are_hyperpod_errors(tmp_path: Path) -> None:
    with pytest.raises(pf.HyperpodPreflightError, match=r"cdk\.json not found"):
        pf.load_hyperpod_config(_REGION, cdk_json_path=tmp_path / "missing.json")
    broken = tmp_path / "cdk.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(pf.HyperpodPreflightError, match="not valid JSON"):
        pf.load_hyperpod_config(_REGION, cdk_json_path=broken)


def test_load_kubernetes_version_reads_the_cluster_minor(tmp_path: Path) -> None:
    path = _write_cdk_json(tmp_path, {"kubernetes_version": " 1.36 "})
    assert pf.load_kubernetes_version(cdk_json_path=path) == "1.36"
    assert pf.preflight_kwargs(cdk_json_path=path) == {"kubernetes_version": "1.36"}


@pytest.mark.parametrize("context", [{}, {"kubernetes_version": 1.36}, {"kubernetes_version": ""}])
def test_load_kubernetes_version_requires_a_string_minor(
    tmp_path: Path, context: dict[str, Any]
) -> None:
    path = _write_cdk_json(tmp_path, context)
    with pytest.raises(pf.HyperpodPreflightError, match="kubernetes_version is missing or not"):
        pf.load_kubernetes_version(cdk_json_path=path)


def test_load_kubernetes_version_missing_cdk_json_is_a_hyperpod_error(tmp_path: Path) -> None:
    with pytest.raises(pf.HyperpodPreflightError, match=r"cdk\.json not found"):
        pf.preflight_kwargs(cdk_json_path=tmp_path / "missing.json")


def test_project_name_is_re_exported() -> None:
    assert pf.project_name is not None
    assert "project_name" in pf.__all__


# ---------------------------------------------------------------------------
# The whole report
# ---------------------------------------------------------------------------


def test_happy_path_passes_every_check(clients: _Clients) -> None:
    _expect_happy_path(clients)
    report = _run(clients, _config())
    assert report.ok
    assert report.region == _REGION
    assert report.enabled is True
    assert report.cluster == _CLUSTER_NAME
    assert [(c.name, c.status) for c in report.checks] == [
        ("agent-image", pf.PASS),
        ("kubernetes-version", pf.PASS),
        ("instance-types", pf.PASS),
        ("training-plans", pf.SKIP),
        ("availability-zone", pf.SKIP),
        ("eks-cluster", pf.PASS),
        ("cluster-name", pf.PASS),
    ]
    assert (
        _by_name(report, "kubernetes-version")[0].detail
        == "EKS 1.36: HyperPod publishes a node AMI (newest 1.36)"
    )
    assert (
        _by_name(report, "instance-types")[0].detail
        == "ml.g5.8xlarge: 2 of 4 quota for cluster usage"
    )
    assert _by_name(report, "cluster-name")[0].detail == f"{_CLUSTER_NAME} is free"
    # Clients are created lazily, in the order the checks first need them.
    assert clients.requested == ["ssm", "service-quotas", "eks", "sagemaker"]


def test_report_dict_and_summary(clients: _Clients) -> None:
    _expect_happy_path(clients)
    report = _run(clients, _config())
    document = report.to_dict()
    assert document["region"] == _REGION
    assert document["ok"] is True
    assert document["checks"][0] == {
        "name": "agent-image",
        "status": "pass",
        "detail": "health-monitoring agent image published for this Region",
    }
    failing = pf.HyperpodPreflightReport(
        region="eu-west-1",
        enabled=True,
        cluster="x",
        checks=[pf.Check("agent-image", pf.FAIL, "no")],
    )
    assert not failing.ok
    assert failing.failures == [pf.Check("agent-image", pf.FAIL, "no")]
    summary = pf.summarize([report, failing])
    assert summary["ok"] is False
    assert [item["region"] for item in summary["regions"]] == [_REGION, "eu-west-1"]


def test_disabled_block_is_still_checked_and_reported_disabled(clients: _Clients) -> None:
    _expect_happy_path(clients)
    report = _run(clients, _config(enabled=False))
    assert report.ok
    assert report.enabled is False
    assert "disabled in cdk.json" in pf.render_report(report)


def test_configured_cluster_name_is_used(clients: _Clients) -> None:
    _expect_default_quotas(clients)
    _expect_eks(clients)
    clients["sagemaker"].add_response(
        "list_clusters", {"ClusterSummaries": []}, {"NameContains": "custom-name"}
    )
    report = _run(clients, _config(cluster_name="custom-name"))
    assert report.cluster == "custom-name"
    assert _by_name(report, "cluster-name")[0].detail == "custom-name is free"


# ---------------------------------------------------------------------------
# agent-image
# ---------------------------------------------------------------------------


def test_region_without_an_agent_image_fails() -> None:
    region = "ca-central-1"
    assert region not in HEALTH_MONITORING_AGENT_ACCOUNTS
    calls: list[str] = []

    def factory(service: str, *, region_name: str) -> Any:
        calls.append(service)
        raise AssertionError("no AWS call expected for the agent-image check")

    checker = _Checker(region, factory)
    pf._check_agent_image(checker)
    assert checker.checks[0].status == pf.FAIL
    assert f"no health-monitoring agent for {region}" in checker.checks[0].detail
    assert "us-east-1" in checker.checks[0].detail
    assert calls == []


# ---------------------------------------------------------------------------
# kubernetes-version
# ---------------------------------------------------------------------------


def test_kubernetes_version_without_a_hyperpod_ami_fails(clients: _Clients) -> None:
    """The newest EKS minor has no HyperPod AMI until the service catches up: CreateCluster would fail."""
    _expect_happy_path(clients)
    report = _run(clients, _config(), kubernetes_version="1.37")
    [check] = _by_name(report, "kubernetes-version")
    assert check.status == pf.FAIL
    assert check.detail == (
        f"EKS 1.37: SageMaker HyperPod publishes no node AMI for this minor in {_REGION} "
        "(it does for 1.34, 1.35, 1.36), so CreateCluster would fail; deploy the cluster at a "
        "supported minor (cdk.json kubernetes_version, or --kubernetes-version on gco release "
        "validate) — EKS cannot downgrade a running cluster"
    )
    assert not report.ok


def test_kubernetes_version_tree_is_paginated_deduplicated_and_sorted(clients: _Clients) -> None:
    """Both AMI flavors of a minor count once; other HyperPod AMIs (Slurm) are ignored."""
    _expect_ami_tree(
        clients,
        [_ami_parameter("1.36"), _ami_parameter("1.32", "amazon-linux-2"), _ami_parameter("1.9")],
        [
            _ami_parameter("1.32"),
            {
                "Name": f"{pf.HYPERPOD_AMI_PARAMETER_ROOT}/slurm-trainium-ubuntu-22.04/latest/ami-id",
                "Type": "String",
                "Value": "ami-0fedcba9876543210",
            },
            {"Type": "String", "Value": "ami-0fedcba9876543210"},  # a leaf without a name
        ],
    )
    _expect_happy_path(clients)
    report = _run(clients, _config(), kubernetes_version="1.35", ami_tree=False)
    [check] = _by_name(report, "kubernetes-version")
    assert check.status == pf.FAIL
    assert "(it does for 1.9, 1.32, 1.36)" in check.detail  # numeric order, not lexical


def test_kubernetes_version_empty_tree_warns(clients: _Clients) -> None:
    _expect_ami_tree(clients, [])
    _expect_happy_path(clients)
    report = _run(clients, _config(), ami_tree=False)
    [check] = _by_name(report, "kubernetes-version")
    assert check.status == pf.WARN
    assert check.detail == (
        f"no HyperPod node AMI parameters under {pf.HYPERPOD_AMI_PARAMETER_ROOT} in {_REGION}; "
        f"cannot confirm HyperPod supports EKS {_K8S}"
    )
    assert report.ok


def test_kubernetes_version_denied_tree_warns(clients: _Clients) -> None:
    clients["ssm"].add_client_error(
        "get_parameters_by_path",
        service_error_code="AccessDeniedException",
        service_message="no ssm",
        expected_params={"Path": pf.HYPERPOD_AMI_PARAMETER_ROOT, "Recursive": True},
    )
    _expect_happy_path(clients)
    report = _run(clients, _config(), ami_tree=False)
    assert _by_name(report, "kubernetes-version") == [
        pf.Check("kubernetes-version", pf.WARN, "could not check: AccessDeniedException: no ssm")
    ]
    assert report.ok


# ---------------------------------------------------------------------------
# instance-types
# ---------------------------------------------------------------------------


def test_instance_types_sum_groups_and_paginate_quotas(clients: _Clients) -> None:
    _expect_quotas(
        clients,
        [_quota("ml.g5.8xlarge", 4)],
        [_quota("ml.t3.large", 1), _quota("ml.c5.large", 0)],
    )
    _expect_eks(clients)
    _expect_clusters(clients, [])
    config = _config(
        instance_groups=[
            _group(name="a", instance_count=3),
            _group(name="b", instance_count=2),
            _group(name="c", instance_type="ml.t3.large", instance_count=1),
            _group(name="d", instance_type="ml.c5.large", instance_count=1),
            _group(name="e", instance_type="ml.p5.48xlarge", instance_count=1),
        ]
    )
    report = _run(clients, config)
    checks = _by_name(report, "instance-types")
    assert [(c.status, c.detail) for c in checks] == [
        (
            pf.FAIL,
            "ml.g5.8xlarge: instance groups ask for 5, the account's 'for cluster usage' "
            "quota is 4; request an increase",
        ),
        (pf.PASS, "ml.t3.large: 1 of 1 quota for cluster usage"),
        (
            pf.FAIL,
            "ml.c5.large: instance groups ask for 1, the account's 'for cluster usage' "
            "quota is 0; request an increase",
        ),
        (
            pf.FAIL,
            f"ml.p5.48xlarge: no 'ml.p5.48xlarge for cluster usage' quota in {_REGION}; "
            "not a HyperPod instance type here",
        ),
    ]
    assert not report.ok


def test_instance_types_denied_quota_listing_is_a_warning(clients: _Clients) -> None:
    clients["service-quotas"].add_client_error(
        "list_service_quotas",
        service_error_code="AccessDeniedException",
        service_message="nope",
        expected_params={"ServiceCode": "sagemaker"},
    )
    _expect_eks(clients)
    _expect_clusters(clients, [])
    report = _run(clients, _config())
    checks = _by_name(report, "instance-types")
    assert [(c.status, c.detail) for c in checks] == [
        (pf.WARN, "could not check: AccessDeniedException: nope")
    ]
    assert report.ok


def test_instance_types_quota_without_a_value_counts_as_zero(clients: _Clients) -> None:
    _expect_quotas(clients, [{"QuotaName": "ml.g5.8xlarge for cluster usage"}])
    _expect_eks(clients)
    _expect_clusters(clients, [])
    report = _run(clients, _config())
    assert _by_name(report, "instance-types")[0].status == pf.FAIL
    assert "quota is 0" in _by_name(report, "instance-types")[0].detail


def test_instance_types_skip_without_groups() -> None:
    checker = _Checker(_REGION, lambda *a, **k: pytest.fail("no client expected"))
    pf._check_instance_types(checker, {"instance_groups": []})
    assert checker.checks == [pf.Check("instance-types", pf.SKIP, "no instance groups")]


# ---------------------------------------------------------------------------
# training-plans
# ---------------------------------------------------------------------------


def _planned_config(**plan_group: Any) -> dict[str, Any]:
    return _config(
        availability_zone=_ZONE,
        instance_groups=[_group(training_plan_arn=_PLAN_ARN, **plan_group)],
    )


def test_training_plan_that_matches_passes(clients: _Clients) -> None:
    _expect_happy_path(clients, zone=True, plan=True)
    report = _run(clients, _planned_config())
    checks = _by_name(report, "training-plans")
    assert [(c.status, c.detail) for c in checks] == [
        (pf.PASS, f"accelerated: {_PLAN_NAME} Active, ml.g5.8xlarge in {_ZONE}")
    ]
    assert _by_name(report, "availability-zone")[0] == pf.Check(
        "availability-zone", pf.PASS, f"{_ZONE} is available"
    )
    assert report.ok


def test_training_plan_without_capacity_summaries_passes_on_status_alone(clients: _Clients) -> None:
    _expect_default_quotas(clients)
    _expect_plan(clients, _plan(Status="Scheduled", ReservedCapacitySummaries=[]))
    _expect_zone(clients)
    _expect_eks(clients)
    _expect_clusters(clients, [])
    report = _run(clients, _planned_config())
    assert _by_name(report, "training-plans")[0] == pf.Check(
        "training-plans", pf.PASS, f"accelerated: {_PLAN_NAME} Scheduled, ml.g5.8xlarge"
    )


def test_training_plan_mismatches_are_all_named(clients: _Clients) -> None:
    _expect_default_quotas(clients)
    _expect_plan(
        clients,
        _plan(
            Status="Expired",
            TargetResources=["training-job"],
            ReservedCapacitySummaries=[
                _capacity("ml.p5.48xlarge", "us-east-1c"),
                _capacity("ml.p4d.24xlarge"),
            ],
        ),
    )
    _expect_zone(clients)
    _expect_eks(clients)
    _expect_clusters(clients, [])
    report = _run(clients, _planned_config())
    check = _by_name(report, "training-plans")[0]
    assert check.status == pf.FAIL
    assert check.detail == (
        f"accelerated: {_PLAN_NAME} status Expired; targets training-job, not hyperpod-cluster; "
        "reserves ml.p4d.24xlarge, ml.p5.48xlarge, not ml.g5.8xlarge; sits in us-east-1c, "
        f"not {_ZONE}"
    )
    assert not report.ok


def test_training_plan_with_empty_status_and_targets(clients: _Clients) -> None:
    _expect_default_quotas(clients)
    plan = _plan(Status="")
    del plan["TargetResources"]  # the API's minimum length is 1; absent reads as nothing
    _expect_plan(clients, plan)
    _expect_zone(clients)
    _expect_eks(clients)
    _expect_clusters(clients, [])
    report = _run(clients, _planned_config())
    check = _by_name(report, "training-plans")[0]
    assert "status unknown; targets nothing, not hyperpod-cluster" in check.detail


def test_training_plan_zone_is_not_compared_without_a_configured_zone(clients: _Clients) -> None:
    _expect_default_quotas(clients)
    _expect_plan(
        clients, _plan(ReservedCapacitySummaries=[_capacity("ml.g5.8xlarge", "us-east-1c")])
    )
    _expect_eks(clients)
    _expect_clusters(clients, [])
    config = _config(instance_groups=[_group(training_plan_arn=_PLAN_ARN)])
    report = _run(clients, config)
    assert _by_name(report, "training-plans")[0].status == pf.PASS
    assert _by_name(report, "availability-zone")[0].status == pf.SKIP


def test_training_plan_missing_fails_and_denied_warns(clients: _Clients) -> None:
    second_arn = _PLAN_ARN.replace(_PLAN_NAME, "other-plan")
    _expect_default_quotas(clients)
    clients["sagemaker"].add_client_error(
        "describe_training_plan",
        service_error_code="AccessDeniedException",
        service_message="denied",
        expected_params={"TrainingPlanName": _PLAN_NAME},
    )
    _expect_plan(clients, None, name="other-plan")
    _expect_eks(clients)
    _expect_clusters(clients, [])
    config = _config(
        instance_groups=[
            _group(name="a", training_plan_arn=_PLAN_ARN),
            _group(name="b", training_plan_arn=second_arn),
        ]
    )
    report = _run(clients, config)
    checks = _by_name(report, "training-plans")
    assert [(c.status, c.detail) for c in checks] == [
        (pf.WARN, "could not check: AccessDeniedException: denied"),
        (pf.FAIL, "b: training plan other-plan does not exist"),
    ]


def test_training_plan_not_found_under_the_long_code(clients: _Clients) -> None:
    _expect_default_quotas(clients)
    clients["sagemaker"].add_client_error(
        "describe_training_plan",
        service_error_code="ResourceNotFoundException",
        expected_params={"TrainingPlanName": _PLAN_NAME},
    )
    _expect_eks(clients)
    _expect_clusters(clients, [])
    report = _run(clients, _config(instance_groups=[_group(training_plan_arn=_PLAN_ARN)]))
    assert _by_name(report, "training-plans")[0].status == pf.FAIL


# ---------------------------------------------------------------------------
# availability-zone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "status", "detail"),
    [
        ("available", pf.PASS, f"{_ZONE} is available"),
        ("impaired", pf.FAIL, f"{_ZONE}: impaired"),
        (None, pf.FAIL, f"{_ZONE}: not found"),
    ],
)
def test_availability_zone_states(
    clients: _Clients, state: str | None, status: str, detail: str
) -> None:
    _expect_default_quotas(clients)
    _expect_zone(clients, state)
    _expect_eks(clients)
    _expect_clusters(clients, [])
    report = _run(clients, _config(availability_zone=_ZONE))
    assert _by_name(report, "availability-zone")[0] == pf.Check("availability-zone", status, detail)


def test_availability_zone_of_another_region_fails(clients: _Clients) -> None:
    _expect_default_quotas(clients)
    clients["ec2"].add_client_error(
        "describe_availability_zones",
        service_error_code="InvalidParameterValue",
        expected_params={"ZoneNames": [_ZONE], "AllAvailabilityZones": False},
    )
    _expect_eks(clients)
    _expect_clusters(clients, [])
    report = _run(clients, _config(availability_zone=_ZONE))
    assert _by_name(report, "availability-zone")[0] == pf.Check(
        "availability-zone", pf.FAIL, f"{_ZONE} is not a zone of {_REGION}"
    )


def test_availability_zone_other_errors_warn(clients: _Clients) -> None:
    _expect_default_quotas(clients)
    clients["ec2"].add_client_error(
        "describe_availability_zones",
        service_error_code="UnauthorizedOperation",
        service_message="no",
        expected_params={"ZoneNames": [_ZONE], "AllAvailabilityZones": False},
    )
    _expect_eks(clients)
    _expect_clusters(clients, [])
    report = _run(clients, _config(availability_zone=_ZONE))
    assert _by_name(report, "availability-zone")[0] == pf.Check(
        "availability-zone", pf.WARN, "could not check: UnauthorizedOperation: no"
    )


def test_availability_zone_transport_error_warns() -> None:
    class _Ec2:
        def describe_availability_zones(self, **kwargs: Any) -> dict[str, Any]:
            raise EndpointConnectionError(endpoint_url="https://ec2.example")

    checker = _Checker(_REGION, lambda service, *, region_name: _Ec2())
    pf._check_availability_zone(checker, {"availability_zone": _ZONE})
    assert checker.checks[0].status == pf.WARN
    assert checker.checks[0].detail.startswith("could not check: EndpointConnectionError")


# ---------------------------------------------------------------------------
# eks-cluster
# ---------------------------------------------------------------------------


def test_eks_cluster_not_deployed_yet_is_skipped(clients: _Clients) -> None:
    _expect_default_quotas(clients)
    _expect_eks(clients, status=None)
    _expect_clusters(clients, [])
    report = _run(clients, _config())
    assert _by_name(report, "eks-cluster")[0] == pf.Check(
        "eks-cluster",
        pf.SKIP,
        f"{_EKS_NAME} is not deployed yet; the deploy creates it and HyperPod joins it",
    )
    assert report.ok


@pytest.mark.parametrize(
    ("status", "mode", "expected_status", "fragment"),
    [
        ("CREATING", "API", pf.FAIL, f"{_EKS_NAME} is CREATING, not ACTIVE"),
        ("", "API", pf.FAIL, f"{_EKS_NAME} is unknown, not ACTIVE"),
        ("ACTIVE", "CONFIG_MAP", pf.FAIL, "authenticates with CONFIG_MAP; HyperPod nodes join"),
        ("ACTIVE", "", pf.FAIL, "authenticates with unknown"),
        ("ACTIVE", "API", pf.PASS, f"{_EKS_NAME} ACTIVE, access entries API"),
    ],
)
def test_eks_cluster_status_and_authentication_mode(
    clients: _Clients, status: str, mode: str, expected_status: str, fragment: str
) -> None:
    _expect_default_quotas(clients)
    _expect_eks(clients, status=status, mode=mode)
    _expect_clusters(clients, [])
    report = _run(clients, _config())
    check = _by_name(report, "eks-cluster")[0]
    assert check.status == expected_status
    assert fragment in check.detail


def test_eks_cluster_denied_describe_warns(clients: _Clients) -> None:
    _expect_default_quotas(clients)
    clients["eks"].add_client_error(
        "describe_cluster",
        service_error_code="AccessDeniedException",
        expected_params={"name": _EKS_NAME},
    )
    _expect_clusters(clients, [])
    report = _run(clients, _config())
    assert _by_name(report, "eks-cluster") == [
        pf.Check("eks-cluster", pf.WARN, "could not check: AccessDeniedException")
    ]


# ---------------------------------------------------------------------------
# cluster-name
# ---------------------------------------------------------------------------


def test_cluster_name_taken_by_this_project_passes(clients: _Clients) -> None:
    _expect_default_quotas(clients)
    _expect_eks(clients)
    _expect_clusters(clients, [_summary(f"{_CLUSTER_NAME}-2"), _summary()])
    _expect_tags(clients, {"gco:project": _PROJECT})
    report = _run(clients, _config())
    assert _by_name(report, "cluster-name")[0] == pf.Check(
        "cluster-name", pf.PASS, f"{_CLUSTER_NAME} exists (InService) and belongs to this project"
    )


def test_cluster_name_taken_by_someone_else_fails(clients: _Clients) -> None:
    _expect_default_quotas(clients)
    _expect_eks(clients)
    _expect_clusters(clients, [_summary()])
    _expect_tags(clients, {"gco:project": "another"})
    report = _run(clients, _config())
    check = _by_name(report, "cluster-name")[0]
    assert check.status == pf.FAIL
    assert check.detail == (
        f"{_CLUSTER_NAME} already exists in {_REGION} and this project does not own it; "
        "set hyperpod.cluster_name"
    )


def test_cluster_name_prefix_matches_are_not_the_cluster(clients: _Clients) -> None:
    _expect_default_quotas(clients)
    _expect_eks(clients)
    _expect_clusters(clients, [_summary(f"{_CLUSTER_NAME}-other")])
    report = _run(clients, _config())
    assert _by_name(report, "cluster-name")[0].detail == f"{_CLUSTER_NAME} is free"


def test_cluster_name_denied_listing_and_denied_tags_warn(clients: _Clients) -> None:
    _expect_default_quotas(clients)
    _expect_eks(clients)
    clients["sagemaker"].add_client_error(
        "list_clusters",
        service_error_code="AccessDeniedException",
        expected_params={"NameContains": _CLUSTER_NAME},
    )
    report = _run(clients, _config())
    assert _by_name(report, "cluster-name") == [
        pf.Check("cluster-name", pf.WARN, "could not check: AccessDeniedException")
    ]

    _expect_default_quotas(clients)
    _expect_eks(clients)
    _expect_clusters(clients, [_summary()])
    clients["sagemaker"].add_client_error(
        "list_tags",
        service_error_code="AccessDeniedException",
        expected_params={"ResourceArn": _CLUSTER_ARN},
    )
    report = _run(clients, _config())
    assert _by_name(report, "cluster-name") == [
        pf.Check("cluster-name", pf.WARN, "could not check: AccessDeniedException")
    ]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_report_marks_every_status_and_counts_failures() -> None:
    report = pf.HyperpodPreflightReport(
        region=_REGION,
        enabled=True,
        cluster=_CLUSTER_NAME,
        checks=[
            pf.Check("agent-image", pf.PASS, "ok"),
            pf.Check("instance-types", pf.FAIL, "too many"),
            pf.Check("training-plans", pf.WARN, "could not check"),
            pf.Check("availability-zone", pf.SKIP, "none configured"),
        ],
    )
    text = pf.render_report(report)
    assert f"SageMaker HyperPod preflight — {_REGION} (enabled): {_CLUSTER_NAME}" in text
    assert "✓ agent-image" in text
    assert "✗ instance-types" in text
    assert "! training-plans" in text
    assert "- availability-zone" in text
    assert text.rstrip().endswith("1 failure(s)")


def test_render_report_with_nothing_checked() -> None:
    report = pf.HyperpodPreflightReport(region=_REGION, enabled=False, cluster=_CLUSTER_NAME)
    text = pf.render_report(report)
    assert "(nothing to check)" in text
    assert text.rstrip().endswith("ready: no failures")
