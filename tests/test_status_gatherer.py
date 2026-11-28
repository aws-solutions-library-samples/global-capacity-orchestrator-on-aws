"""Tests for fleet status document assembly (``cli/status.py``).

Covers region topology resolution, each section gatherer against its mocked
manager, the boundary that converts an escaping exception into a degraded
section, and the overall verdict. Managers are patched at their factory
import sites and payloads use the real ``StackInfo``, ``RegionCapacity`` and
``AddonRecord`` dataclasses so field drift breaks these tests. The add-ons
section is also read once through the real SSM reader against a fake
paginator. No network or credentials are used.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
from botocore.exceptions import ClientError

from cli.aws_client import APIRequestError
from cli.capacity.multi_region import MultiRegionCapacityChecker, RegionCapacity
from cli.config import GCOConfig
from cli.costs import CostSummary, ResourceCost
from cli.stacks import AddonRecord, StackInfo
from cli.status import (
    ADDON_FINDING_MESSAGE_CHARS,
    HEALTH_HEALTHY,
    HEALTH_IN_PROGRESS,
    HEALTH_NOT_DEPLOYED,
    HEALTH_UNHEALTHY,
    HEALTH_UNKNOWN,
    OVERALL_DEGRADED,
    OVERALL_OK,
    REGION_SOURCE_CDK_JSON,
    REGION_SOURCE_FLAG,
    SECTION_ADDONS,
    SECTION_CAPACITY,
    SECTION_JOBS,
    SECTION_ORDER,
    SECTION_QUEUE,
    SECTION_REGIONS,
    SECTION_STACKS,
    SEVERITY_ERROR,
    SEVERITY_WARN,
    STATUS_EMPTY,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_SKIPPED,
    STATUS_UNAVAILABLE,
    Finding,
    Section,
    _derive_overall,
    _gather_addons,
    _gather_capacity,
    _gather_costs,
    _gather_inference,
    _gather_jobs,
    _gather_nodepools,
    _gather_queue,
    _gather_stacks,
    derive_findings,
    gather_fleet_status,
    one_line,
    resolve_regions,
)

_CDK_REGIONS = {
    "global": "us-east-2",
    "api_gateway": "us-east-2",
    "monitoring": "us-east-2",
    "regional": ["us-east-1", "us-west-2"],
}

_WORKLOAD = ["us-east-1", "us-west-2"]

_REGIONS_DATA = {
    "global": "us-east-2",
    "api_gateway": "us-east-2",
    "monitoring": "us-east-2",
    "workload": _WORKLOAD,
    "source": REGION_SOURCE_CDK_JSON,
}


def _config() -> GCOConfig:
    return GCOConfig(project_name="test-gco", default_region="us-east-1")


def _stack_info(name: str, region: str, status: str = "UPDATE_COMPLETE") -> StackInfo:
    return StackInfo(
        name=name,
        status=status,
        region=region,
        updated_time=datetime(2026, 8, 11, 9, 14, 22, tzinfo=UTC),
    )


def _probe(config: GCOConfig, regions: list[str], present: bool = True) -> dict[str, Any]:
    prefix = config.regional_stack_prefix
    return {
        region: _stack_info(f"{prefix}-{region}", region) if present else None for region in regions
    }


def _queue_payload(region: str, dlq: int = 0) -> dict[str, Any]:
    return {
        "region": region,
        "queue_url": f"https://sqs.example/{region}",
        "messages_available": 3,
        "messages_in_flight": 1,
        "messages_delayed": 0,
        "dlq_url": f"https://sqs.example/{region}-dlq",
        "dlq_messages": dlq,
    }


def _capacity(region: str, telemetry_status: str = "complete", **overrides: Any) -> RegionCapacity:
    values: dict[str, Any] = {
        "queue_depth": 3,
        "running_jobs": 1,
        "gpu_utilization": 62.5,
        "cpu_utilization": 31.0,
        "telemetry_status": telemetry_status,
        "unavailable_signals": [],
        "telemetry_errors": [],
    }
    values.update(overrides)
    return RegionCapacity(region=region, **values)


def _cost_summary(total: float = 12.34) -> CostSummary:
    by_service = (
        [ResourceCost(service="Amazon Elastic Compute Cloud - Compute", amount=total)]
        if total
        else []
    )
    return CostSummary(
        total=total,
        period_start="2026-07-14",
        period_end="2026-08-13",
        by_service=by_service,
    )


def _nodepool_payload() -> dict[str, Any]:
    return {
        "name": "gpu-pool",
        "capacity_types": "spot",
        "instance_types": "g4dn.xlarge, g5.xlarge",
        "status": "Ready",
        "limits": {"cpu": "1000"},
    }


def _stats_payload() -> dict[str, Any]:
    return {
        "summary": {
            "total_jobs": 4,
            "total_queued": 3,
            "total_running": 1,
            "complete": True,
            "records_evaluated": 4,
        },
        "by_region": {"us-east-1": {"queued": 3, "running": 1}},
    }


_ADDONS_UPDATED = datetime(2026, 8, 11, 9, 31, 5, tzinfo=UTC)

#: What the kubectl applier records when the base pass cannot apply the
#: admission policy, as it did live in every Region.
_BASE_PASS_FAILURE = (
    "04-resource-quotas.yaml:ValidatingAdmissionPolicy/gco-jobs-container-gpu-limit,"
    "04-resource-quotas.yaml:ValidatingAdmissionPolicyBinding/gco-jobs-container-gpu-limit"
)


def _addon(name: str, status: str, message: str = "ok") -> AddonRecord:
    return AddonRecord(name=name, status=status, message=message, updated_at=_ADDONS_UPDATED)


def _converged(_project: str, _region: str) -> list[AddonRecord]:
    """A Region whose add-ons all converged, as the SSM reader returns it."""
    return [
        _addon("base-manifests", "applied", "applied=41 expected=41 skipped=0"),
        _addon("keda", "installed", "keda installed"),
        _addon("manifest-validation", "validated", "validated=41 expected=41"),
    ]


def _base_pass_failed(_project: str, _region: str) -> list[AddonRecord]:
    return [_addon("base-manifests", "failed", _BASE_PASS_FAILURE)]


@contextmanager
def _fleet_boundaries(cdk_regions: dict[str, Any] | None) -> Iterator[SimpleNamespace]:
    """Patch every AWS-facing factory the orchestrator can reach."""
    config = _config()
    stack_manager = MagicMock(name="stack_manager")
    stack_manager.get_stack_status.side_effect = lambda name, region: _stack_info(name, region)
    job_manager = MagicMock(name="job_manager")
    job_manager.get_queue_status.side_effect = _queue_payload
    aws_client = MagicMock(name="aws_client")
    aws_client.call_api.return_value = _stats_payload()
    checker = MagicMock(name="capacity_checker")
    checker._last_region_errors = []
    checker.get_all_regions_capacity.return_value = [_capacity(region) for region in _WORKLOAD]
    inference_manager = MagicMock(name="inference_manager")
    inference_manager.list_endpoints.return_value = []
    cost_tracker = MagicMock(name="cost_tracker")
    cost_tracker.get_cost_summary.return_value = _cost_summary()
    cost_tracker.get_cost_allocation_tag_status.return_value = [
        {"tag_key": "Project", "type": "UserDefined", "status": "Active"}
    ]
    describe_access = MagicMock(
        name="describe_cluster_access",
        return_value={"endpoint": "https://eks.example", "public": True, "private": True},
    )
    list_pools = MagicMock(name="list_cluster_nodepools", return_value=[_nodepool_payload()])
    read_addons = MagicMock(name="read_addon_status", side_effect=_converged)
    with (
        patch("cli.status._load_cdk_json", return_value=cdk_regions or {}),
        patch("cli.stacks.get_stack_manager", return_value=stack_manager),
        patch("cli.stacks.read_addon_status", read_addons),
        patch("cli.jobs.get_job_manager", return_value=job_manager),
        patch("cli.aws_client.get_aws_client", return_value=aws_client),
        patch("cli.capacity.get_multi_region_capacity_checker", return_value=checker),
        patch("cli.inference.get_inference_manager", return_value=inference_manager),
        patch("cli.costs.get_cost_tracker", return_value=cost_tracker),
        patch("cli.kubectl_helpers.describe_cluster_access", describe_access),
        patch("cli.nodepools.list_cluster_nodepools", list_pools),
    ):
        yield SimpleNamespace(
            config=config,
            stack_manager=stack_manager,
            job_manager=job_manager,
            aws_client=aws_client,
            checker=checker,
            inference_manager=inference_manager,
            cost_tracker=cost_tracker,
            describe_access=describe_access,
            list_pools=list_pools,
            read_addons=read_addons,
        )


# ---------------------------------------------------------------------------
# Region resolution
# ---------------------------------------------------------------------------


def test_resolve_regions_reads_the_configured_topology() -> None:
    with patch("cli.status._load_cdk_json", return_value=dict(_CDK_REGIONS)):
        section = resolve_regions(_config())

    assert section.status == STATUS_OK
    assert section.data == {
        "global": "us-east-2",
        "api_gateway": "us-east-2",
        "monitoring": "us-east-2",
        "workload": ["us-east-1", "us-west-2"],
        "source": REGION_SOURCE_CDK_JSON,
    }


def test_resolve_regions_narrows_to_the_explicit_flag_region() -> None:
    with patch("cli.status._load_cdk_json", return_value=dict(_CDK_REGIONS)):
        section = resolve_regions(_config(), region="us-west-2")

    assert section.status == STATUS_OK
    assert section.data["workload"] == ["us-west-2"]
    assert section.data["source"] == REGION_SOURCE_FLAG


def test_resolve_regions_flag_works_without_cdk_json() -> None:
    with patch("cli.status._load_cdk_json", return_value={}):
        section = resolve_regions(_config(), region="eu-west-1")

    assert section.status == STATUS_OK
    assert section.data["workload"] == ["eu-west-1"]
    assert section.data["source"] == REGION_SOURCE_FLAG


def test_resolve_regions_missing_cdk_json_names_both_remedies() -> None:
    with patch("cli.status._load_cdk_json", return_value={}):
        section = resolve_regions(_config())

    assert section.status == STATUS_UNAVAILABLE
    assert section.reason is not None
    assert "cdk.json" in section.reason
    assert "--region" in section.reason
    assert section.data == {}


def test_resolve_regions_empty_regional_list_is_unavailable() -> None:
    with patch("cli.status._load_cdk_json", return_value={"regional": []}):
        section = resolve_regions(_config())

    assert section.status == STATUS_UNAVAILABLE


def test_resolve_regions_ignores_non_string_regional_entries() -> None:
    with patch("cli.status._load_cdk_json", return_value={"regional": [None, 3]}):
        section = resolve_regions(_config())

    assert section.status == STATUS_UNAVAILABLE


# ---------------------------------------------------------------------------
# stacks section
# ---------------------------------------------------------------------------


def test_stacks_section_lists_expected_and_present_optional_stacks() -> None:
    config = _config()
    described = {
        "test-gco-global": "UPDATE_COMPLETE",
        "test-gco-api-gateway": "CREATE_COMPLETE",
        "test-gco-monitoring": "UPDATE_COMPLETE",
        "test-gco-us-east-1": "UPDATE_COMPLETE",
        "test-gco-us-west-2": "UPDATE_ROLLBACK_COMPLETE",
        "test-gco-regional-api-us-east-1": "CREATE_COMPLETE",
        "test-gco-us-east-1-persistent": "CREATE_COMPLETE",
    }
    manager = MagicMock()
    manager.get_stack_status.side_effect = lambda name, region: (
        _stack_info(name, region, described[name]) if name in described else None
    )

    with patch("cli.stacks.get_stack_manager", return_value=manager):
        section = _gather_stacks(config, _REGIONS_DATA, _WORKLOAD)

    assert section.status == STATUS_OK
    expected = {entry["name"]: entry for entry in section.data["expected"]}
    assert set(expected) == {
        "test-gco-global",
        "test-gco-api-gateway",
        "test-gco-monitoring",
        "test-gco-us-east-1",
        "test-gco-us-west-2",
    }
    assert expected["test-gco-us-west-2"]["health"] == HEALTH_UNHEALTHY
    assert expected["test-gco-global"]["health"] == HEALTH_HEALTHY
    assert expected["test-gco-global"]["updated_time"] == "2026-08-11T09:14:22+00:00"
    # Absent optional stacks are omitted; present ones are listed.
    optional = {entry["name"]: entry for entry in section.data["optional"]}
    assert set(optional) == {
        "test-gco-regional-api-us-east-1",
        "test-gco-us-east-1-persistent",
    }


def test_stacks_section_classifies_absent_and_in_progress_stacks() -> None:
    config = _config()
    manager = MagicMock()
    manager.get_stack_status.side_effect = lambda name, region: (
        _stack_info(name, region, "UPDATE_IN_PROGRESS") if name == "test-gco-us-east-1" else None
    )

    with patch("cli.stacks.get_stack_manager", return_value=manager):
        section = _gather_stacks(config, _REGIONS_DATA, ["us-east-1"])

    expected = {entry["name"]: entry for entry in section.data["expected"]}
    assert expected["test-gco-us-east-1"]["health"] == HEALTH_IN_PROGRESS
    assert expected["test-gco-global"]["health"] == HEALTH_NOT_DEPLOYED
    assert expected["test-gco-global"]["status"] is None
    assert expected["test-gco-global"]["updated_time"] is None


def _client_error(code: str, message: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "DescribeStacks")


def test_stacks_section_reports_an_unreadable_stack_instead_of_an_absent_one() -> None:
    config = _config()
    present = {
        "test-gco-global",
        "test-gco-api-gateway",
        "test-gco-monitoring",
        "test-gco-us-east-1",
    }

    def describe(name: str, region: str) -> StackInfo | None:
        if name == "test-gco-us-west-2":
            raise _client_error("AccessDenied", "not authorized to DescribeStacks")
        if name == "test-gco-analytics":
            raise _client_error("Throttling", "Rate exceeded")
        return _stack_info(name, region) if name in present else None

    manager = MagicMock()
    manager.get_stack_status.side_effect = describe

    with patch("cli.stacks.get_stack_manager", return_value=manager):
        section = _gather_stacks(config, _REGIONS_DATA, _WORKLOAD)

    # Five expected stacks and five optional ones were read; two reads failed.
    assert section.status == STATUS_PARTIAL
    assert section.reason == "2 of 10 stacks could not be read"
    expected = {entry["name"]: entry for entry in section.data["expected"]}
    assert expected["test-gco-us-west-2"] == {
        "name": "test-gco-us-west-2",
        "region": "us-west-2",
        "status": None,
        "health": HEALTH_UNKNOWN,
        "updated_time": None,
    }
    assert expected["test-gco-us-east-1"]["health"] == HEALTH_HEALTHY
    # An unreadable optional stack is listed (it may exist); absent ones are not.
    assert [(entry["name"], entry["health"]) for entry in section.data["optional"]] == [
        ("test-gco-analytics", HEALTH_UNKNOWN)
    ]
    assert len(section.errors) == 2
    assert section.errors[0].startswith("test-gco-us-west-2 in us-west-2: ClientError: ")
    assert "AccessDenied" in section.errors[0]
    assert section.errors[1].startswith("test-gco-analytics in us-east-2: ClientError: ")
    assert "Throttling" in section.errors[1]


def test_stacks_section_is_an_error_when_no_stack_can_be_read() -> None:
    config = _config()
    manager = MagicMock()
    manager.get_stack_status.side_effect = RuntimeError("Unable to locate credentials")

    with patch("cli.stacks.get_stack_manager", return_value=manager):
        section = _gather_stacks(config, _REGIONS_DATA, ["us-east-1"])

    assert section.status == STATUS_ERROR
    assert section.reason == "no stack could be read"
    assert {entry["health"] for entry in section.data["expected"]} == {HEALTH_UNKNOWN}
    assert len(section.errors) == manager.get_stack_status.call_count == 7
    assert all("RuntimeError: Unable to locate credentials" in error for error in section.errors)
    # Each expected stack becomes a "could not be read" warning, never "not deployed".
    messages = [finding.message for finding in derive_findings({SECTION_STACKS: section})]
    assert messages == [
        f"{entry['name']} could not be read in {entry['region']}"
        for entry in section.data["expected"]
    ]


# ---------------------------------------------------------------------------
# addons section
# ---------------------------------------------------------------------------


def _ssm_listing(parameters: dict[str, list[dict[str, Any]]]) -> MagicMock:
    """A ``boto3.client`` whose SSM client lists ``parameters[region]`` in two pages."""

    def client(service: str, region_name: str) -> MagicMock:
        assert service == "ssm"
        listed = parameters[region_name]
        ssm = MagicMock(name=f"ssm-{region_name}")
        paginator = ssm.get_paginator.return_value
        paginator.paginate.return_value = [
            {"Parameters": listed[:2]},
            {"Parameters": listed[2:]},
        ]
        return ssm

    return MagicMock(name="boto3.client", side_effect=client)


def _parameter(region: str, name: str, value: Any) -> dict[str, str]:
    text = value if isinstance(value, str) else json.dumps(value)
    return {"Name": f"/test-gco/addons/{region}/{name}", "Value": text}


def test_addons_section_reads_each_region_once_with_the_addons_command_reader() -> None:
    updated = int(_ADDONS_UPDATED.timestamp())
    listing = _ssm_listing(
        {
            "us-east-1": [
                _parameter("us-east-1", "_input", "eJzLSM3JyQcABiwCFQ=="),
                _parameter("us-east-1", "_execution", {"started_at": updated}),
                _parameter(
                    "us-east-1",
                    "base-manifests",
                    {
                        "phase": "base-manifests",
                        "status": "failed",
                        "message": _BASE_PASS_FAILURE,
                        "updated_at": updated,
                    },
                ),
                _parameter("us-east-1", "keda", {"status": "installed", "message": "k" * 120}),
            ],
            "us-west-2": [
                _parameter("us-west-2", "base-manifests", {"status": "applied", "message": "ok"}),
                _parameter("us-west-2", "_teardown", "teardown-abc123"),
            ],
        }
    )

    with patch("boto3.client", listing):
        section = _gather_addons(_config(), _WORKLOAD)

    # One SSM client per Region; the next test checks what each one lists.
    assert sorted(c.kwargs["region_name"] for c in listing.call_args_list) == _WORKLOAD
    assert section.status == STATUS_OK
    assert section.reason is None and section.errors == []
    assert section.data == {
        "by_region": {
            "us-east-1": {
                "readable": True,
                "addons": [
                    {
                        "name": "base-manifests",
                        "status": "failed",
                        "health": HEALTH_UNHEALTHY,
                        "message": _BASE_PASS_FAILURE,
                        "updated_at": "2026-08-11T09:31:05+00:00",
                    },
                    {
                        # A healthy record's message is cut to one line, as
                        # gco stacks addons status prints it.
                        "name": "keda",
                        "status": "installed",
                        "health": HEALTH_HEALTHY,
                        "message": "k" * 80,
                        "updated_at": None,
                    },
                ],
            },
            "us-west-2": {
                "readable": True,
                "addons": [
                    {
                        "name": "_teardown",
                        "status": "teardown-active",
                        "health": HEALTH_IN_PROGRESS,
                        "message": "execution teardown-abc123",
                        "updated_at": None,
                    },
                    {
                        "name": "base-manifests",
                        "status": "applied",
                        "health": HEALTH_HEALTHY,
                        "message": "ok",
                        "updated_at": None,
                    },
                ],
            },
        }
    }


def test_addons_section_lists_one_path_per_region() -> None:
    clients: dict[str, MagicMock] = {}

    def client(service: str, region_name: str) -> MagicMock:
        ssm = clients[region_name] = MagicMock(name=f"ssm-{region_name}")
        ssm.get_paginator.return_value.paginate.return_value = [{"Parameters": []}]
        return ssm

    with patch("boto3.client", side_effect=client):
        _gather_addons(_config(), _WORKLOAD)

    for region in _WORKLOAD:
        ssm = clients[region]
        ssm.get_paginator.assert_called_once_with("get_parameters_by_path")
        ssm.get_paginator.return_value.paginate.assert_called_once_with(
            Path=f"/test-gco/addons/{region}/", Recursive=False
        )


@pytest.mark.parametrize(
    ("status", "health"),
    [
        ("installed", HEALTH_HEALTHY),
        ("uninstalled", HEALTH_HEALTHY),
        ("absent", HEALTH_HEALTHY),
        ("applied", HEALTH_HEALTHY),
        ("validated", HEALTH_HEALTHY),
        # The applier's gateway-teardown phase, when it succeeded.
        ("deleted", HEALTH_HEALTHY),
        ("failed", HEALTH_UNHEALTHY),
        ("teardown-active", HEALTH_IN_PROGRESS),
        ("unknown", HEALTH_UNKNOWN),
    ],
)
def test_addons_section_classifies_each_status_as_the_addons_command_does(
    status: str, health: str
) -> None:
    with patch("cli.stacks.read_addon_status", return_value=[_addon("keda", status)]):
        section = _gather_addons(_config(), ["us-east-1"])

    [record] = section.data["by_region"]["us-east-1"]["addons"]
    assert record["health"] == health
    assert record["status"] == status
    assert record["updated_at"] == "2026-08-11T09:31:05+00:00"


def test_addons_section_without_any_record_yet_is_empty() -> None:
    with patch("cli.stacks.read_addon_status", return_value=[]):
        section = _gather_addons(_config(), _WORKLOAD)

    assert section.status == STATUS_EMPTY
    assert section.data == {
        "by_region": {
            "us-east-1": {"readable": True, "addons": []},
            "us-west-2": {"readable": True, "addons": []},
        }
    }
    assert derive_findings({SECTION_ADDONS: section}) == []


def test_addons_section_with_records_in_only_some_regions_is_ok() -> None:
    def read(project: str, region: str) -> list[AddonRecord]:
        return _converged(project, region) if region == "us-east-1" else []

    with patch("cli.stacks.read_addon_status", side_effect=read):
        section = _gather_addons(_config(), _WORKLOAD)

    assert section.status == STATUS_OK
    assert section.data["by_region"]["us-west-2"] == {"readable": True, "addons": []}


def test_addons_section_an_unreadable_region_costs_only_itself() -> None:
    def read(project: str, region: str) -> list[AddonRecord]:
        if region == "us-west-2":
            raise _client_error("AccessDeniedException", "not authorized")
        return _base_pass_failed(project, region)

    with patch("cli.stacks.read_addon_status", side_effect=read):
        section = _gather_addons(_config(), _WORKLOAD)

    assert section.status == STATUS_PARTIAL
    assert section.reason == "add-on status could not be read in 1 of 2 regions"
    [error] = section.errors
    assert error.startswith("us-west-2: ClientError: ")
    assert "not authorized" in error
    assert section.data["by_region"]["us-west-2"] == {"readable": False, "addons": []}
    # The Region that was read still reports its failed base pass.
    assert [f.message for f in derive_findings({SECTION_ADDONS: section})] == [
        "base-manifests failed in us-east-1: "
        + one_line(_BASE_PASS_FAILURE, ADDON_FINDING_MESSAGE_CHARS)
    ]


def test_addons_section_is_an_error_when_no_region_can_be_read() -> None:
    with patch("cli.stacks.read_addon_status", side_effect=RuntimeError("Unable to locate creds")):
        section = _gather_addons(_config(), _WORKLOAD)

    assert section.status == STATUS_ERROR
    assert section.reason == "add-on status could not be read in any region"
    assert section.errors == [
        "us-east-1: RuntimeError: Unable to locate creds",
        "us-west-2: RuntimeError: Unable to locate creds",
    ]
    assert all(not entry["readable"] for entry in section.data["by_region"].values())


# ---------------------------------------------------------------------------
# queue section
# ---------------------------------------------------------------------------


def test_queue_section_totals_across_regions() -> None:
    config = _config()
    manager = MagicMock()
    manager.get_queue_status.side_effect = lambda region: _queue_payload(
        region, dlq=2 if region == "us-west-2" else 0
    )

    with patch("cli.jobs.get_job_manager", return_value=manager):
        section = _gather_queue(config, _WORKLOAD, _probe(config, _WORKLOAD), True)

    assert section.status == STATUS_OK
    assert section.data["by_region"]["us-west-2"]["dlq"] == 2
    assert section.data["totals"] == {"available": 6, "in_flight": 2, "delayed": 0, "dlq": 2}


def test_queue_section_value_error_degrades_to_partial() -> None:
    config = _config()
    manager = MagicMock()

    def read(region: str) -> dict[str, Any]:
        if region == "us-west-2":
            raise ValueError(f"Job queue not found in stack test-gco-{region}")
        return _queue_payload(region)

    manager.get_queue_status.side_effect = read
    with patch("cli.jobs.get_job_manager", return_value=manager):
        section = _gather_queue(config, _WORKLOAD, _probe(config, _WORKLOAD), True)

    assert section.status == STATUS_PARTIAL
    assert list(section.data["by_region"]) == ["us-east-1"]
    assert any("us-west-2" in error for error in section.errors)
    assert section.reason is not None


def test_queue_section_absent_stacks_are_unavailable_without_a_read() -> None:
    config = _config()
    manager = MagicMock()

    with patch("cli.jobs.get_job_manager", return_value=manager):
        section = _gather_queue(config, _WORKLOAD, _probe(config, _WORKLOAD, present=False), True)

    assert section.status == STATUS_UNAVAILABLE
    manager.get_queue_status.assert_not_called()
    assert section.reason is not None
    assert "test-gco-" in section.reason
    assert all("absent or not readable" in error for error in section.errors)


def test_queue_section_requires_the_configured_region_list() -> None:
    config = _config()
    manager = MagicMock()

    with patch("cli.jobs.get_job_manager", return_value=manager):
        section = _gather_queue(config, ["eu-west-1"], _probe(config, ["eu-west-1"]), False)

    assert section.status == STATUS_UNAVAILABLE
    manager.get_queue_status.assert_not_called()
    assert any("cdk.json" in error for error in section.errors)


def test_queue_section_unexpected_failure_everywhere_is_an_error() -> None:
    config = _config()
    manager = MagicMock()
    manager.get_queue_status.side_effect = RuntimeError("throttled")

    with patch("cli.jobs.get_job_manager", return_value=manager):
        section = _gather_queue(config, _WORKLOAD, _probe(config, _WORKLOAD), True)

    assert section.status == STATUS_ERROR
    assert all("RuntimeError" in error for error in section.errors)


# ---------------------------------------------------------------------------
# jobs section
# ---------------------------------------------------------------------------


def test_jobs_section_surfaces_totals_and_scan_completeness() -> None:
    config = _config()
    client = MagicMock()
    client.call_api.return_value = _stats_payload()

    with patch("cli.aws_client.get_aws_client", return_value=client):
        section = _gather_jobs(config, None)

    assert section.status == STATUS_OK
    assert section.data["totals"] == {"total": 4, "queued": 3, "running": 1}
    assert section.data["complete"] is True
    assert section.data["records_evaluated"] == 4
    # The route aggregates `gco queue submit` records only: the
    # document says so, so `empty` is never read as "no jobs at all".
    assert section.data["source"] == "central job queue"
    # A single attempt: a status snapshot reports a failing route honestly
    # rather than retrying past the section timeout.
    # Each read is a single attempt: the section decides any retry against its
    # own budget instead of the client retrying past the section timeout.
    client.call_api.assert_called_once_with(
        method="GET", path="/api/v1/queue/stats", region=None, max_attempts=1
    )


def _jobs_clock(monkeypatch: pytest.MonkeyPatch, *readings: float) -> None:
    """Give ``_gather_jobs`` these monotonic readings: its start, then after a failed attempt."""
    monkeypatch.setattr("cli.status.time", SimpleNamespace(monotonic=iter(readings).__next__))


@pytest.mark.parametrize("status_code", [429, 502, 503, 504])
def test_jobs_section_retries_a_transient_answer_once_within_the_budget(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    # A cold Lambda behind API Gateway answers the first call after idle
    # with a 5xx; 14 s in, a second attempt as slow still fits in 30 s.
    _jobs_clock(monkeypatch, 0.0, 14.0)
    client = MagicMock()
    client.call_api.side_effect = [APIRequestError(status_code, "cold start"), _stats_payload()]

    with patch("cli.aws_client.get_aws_client", return_value=client):
        section = _gather_jobs(_config(), None)

    assert section.status == STATUS_OK
    assert section.data["totals"] == {"total": 4, "queued": 3, "running": 1}
    assert (
        client.call_api.call_args_list
        == [call(method="GET", path="/api/v1/queue/stats", region=None, max_attempts=1)] * 2
    )


def test_jobs_section_reports_a_transient_answer_that_left_no_room_to_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 16 s in, a second attempt as slow would overrun the 30 s section budget.
    _jobs_clock(monkeypatch, 0.0, 16.0)
    client = MagicMock()
    client.call_api.side_effect = APIRequestError(504, "Endpoint request timed out")

    with (
        patch("cli.aws_client.get_aws_client", return_value=client),
        pytest.raises(APIRequestError, match="Endpoint request timed out"),
    ):
        _gather_jobs(_config(), None)

    client.call_api.assert_called_once()


@pytest.mark.parametrize("status_code", [400, 403, 500])
def test_jobs_section_never_retries_an_answer_the_client_would_not(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    _jobs_clock(monkeypatch, 0.0, 0.5)
    client = MagicMock()
    client.call_api.side_effect = APIRequestError(status_code, "refused")

    with (
        patch("cli.aws_client.get_aws_client", return_value=client),
        pytest.raises(APIRequestError, match="refused"),
    ):
        _gather_jobs(_config(), None)

    client.call_api.assert_called_once()


def test_jobs_section_retries_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _jobs_clock(monkeypatch, 0.0, 1.0)
    client = MagicMock()
    client.call_api.side_effect = [
        APIRequestError(502, "first"),
        APIRequestError(502, "second"),
    ]

    with (
        patch("cli.aws_client.get_aws_client", return_value=client),
        pytest.raises(APIRequestError, match="second"),
    ):
        _gather_jobs(_config(), None)

    assert client.call_api.call_count == 2


def test_jobs_section_pins_the_requested_region() -> None:
    config = _config()
    client = MagicMock()
    client.call_api.return_value = _stats_payload()

    with patch("cli.aws_client.get_aws_client", return_value=client):
        _gather_jobs(config, "us-west-2")

    assert client.call_api.call_args.kwargs["region"] == "us-west-2"


def test_jobs_section_zero_jobs_is_empty_not_unavailable() -> None:
    config = _config()
    client = MagicMock()
    client.call_api.return_value = {
        "summary": {
            "total_jobs": 0,
            "total_queued": 0,
            "total_running": 0,
            "complete": True,
            "records_evaluated": 0,
        },
        "by_region": {},
    }

    with patch("cli.aws_client.get_aws_client", return_value=client):
        section = _gather_jobs(config, None)

    assert section.status == STATUS_EMPTY


def test_jobs_section_missing_api_endpoint_is_unavailable_with_the_deploy_hint() -> None:
    config = _config()
    client = MagicMock()
    client.call_api.side_effect = RuntimeError("Failed to get API endpoint: stack missing")

    with patch("cli.aws_client.get_aws_client", return_value=client):
        section = _gather_jobs(config, None)

    assert section.status == STATUS_UNAVAILABLE
    assert section.reason is not None
    assert "test-gco-api-gateway" in section.reason
    assert "gco stacks deploy" in section.reason


def test_jobs_section_reraises_other_api_failures_for_the_boundary() -> None:
    config = _config()
    client = MagicMock()
    client.call_api.side_effect = RuntimeError("API request failed: 500 oops")

    with patch("cli.aws_client.get_aws_client", return_value=client):
        with pytest.raises(RuntimeError) as excinfo:
            _gather_jobs(config, None)
        e = excinfo.value
        assert "500" in str(e)


# ---------------------------------------------------------------------------
# capacity section
# ---------------------------------------------------------------------------


def test_capacity_section_sweeps_and_carries_the_telemetry_triple() -> None:
    config = _config()
    checker = MagicMock()
    checker._last_region_errors = []
    checker.get_all_regions_capacity.return_value = [
        _capacity("us-east-1"),
        _capacity(
            "us-west-2",
            telemetry_status="partial",
            unavailable_signals=["gpu"],
            telemetry_errors=["GPU telemetry returned no datapoints"],
        ),
    ]

    with patch("cli.capacity.get_multi_region_capacity_checker", return_value=checker):
        section = _gather_capacity(config, _WORKLOAD, _WORKLOAD, _probe(config, _WORKLOAD))

    assert section.status == STATUS_PARTIAL
    west = section.data["by_region"]["us-west-2"]
    assert west["telemetry_status"] == "partial"
    assert west["unavailable_signals"] == ["gpu"]
    assert "us-west-2: GPU telemetry returned no datapoints" in section.errors
    assert section.reason == "1 of 2 regions reported incomplete telemetry"


def test_capacity_section_all_complete_is_ok() -> None:
    config = _config()
    checker = MagicMock()
    checker._last_region_errors = []
    checker.get_all_regions_capacity.return_value = [_capacity(region) for region in _WORKLOAD]

    with patch("cli.capacity.get_multi_region_capacity_checker", return_value=checker):
        section = _gather_capacity(config, _WORKLOAD, _WORKLOAD, _probe(config, _WORKLOAD))

    assert section.status == STATUS_OK
    assert section.data["by_region"]["us-east-1"]["queue_depth"] == 3


def test_capacity_section_empty_sweep_with_errors_is_an_error() -> None:
    config = _config()
    checker = MagicMock()
    checker._last_region_errors = ["region discovery: access denied"]
    checker.get_all_regions_capacity.return_value = []

    with patch("cli.capacity.get_multi_region_capacity_checker", return_value=checker):
        section = _gather_capacity(config, _WORKLOAD, _WORKLOAD, _probe(config, _WORKLOAD))

    assert section.status == STATUS_ERROR
    assert "region discovery: access denied" in section.errors


def test_capacity_section_never_calls_the_checker_when_no_stack_exists() -> None:
    config = _config()
    factory = MagicMock()

    with patch("cli.capacity.get_multi_region_capacity_checker", factory):
        section = _gather_capacity(
            config, _WORKLOAD, _WORKLOAD, _probe(config, _WORKLOAD, present=False)
        )

    factory.assert_not_called()
    assert section.status == STATUS_UNAVAILABLE
    assert all(
        entry["telemetry_status"] == STATUS_UNAVAILABLE
        for entry in section.data["by_region"].values()
    )


def test_capacity_section_unreachable_region_names_every_signal_the_checker_reads() -> None:
    # The zero running_jobs of an unreachable region is not a reading: the
    # entry names the `jobs` signal unavailable, exactly as the checker does
    # for a region without a regional stack.
    config = _config()
    aws_client = MagicMock()
    aws_client.get_regional_stack.return_value = None
    with patch("cli.aws_client.get_aws_client", return_value=aws_client):
        no_stack = MultiRegionCapacityChecker(config).get_region_capacity("us-east-1")

    with patch("cli.capacity.get_multi_region_capacity_checker", MagicMock()):
        section = _gather_capacity(
            config, _WORKLOAD, _WORKLOAD, _probe(config, _WORKLOAD, present=False)
        )

    assert no_stack.telemetry_status == STATUS_UNAVAILABLE
    for entry in section.data["by_region"].values():
        assert entry["unavailable_signals"] == no_stack.unavailable_signals
        assert "jobs" in entry["unavailable_signals"]


def test_capacity_section_never_calls_the_checker_without_configured_regions() -> None:
    config = _config()
    factory = MagicMock()

    with patch("cli.capacity.get_multi_region_capacity_checker", factory):
        section = _gather_capacity(config, ["eu-west-1"], [], _probe(config, ["eu-west-1"]))

    factory.assert_not_called()
    assert section.status == STATUS_UNAVAILABLE
    assert any("cdk.json" in error for error in section.errors)


def test_capacity_section_narrowed_region_uses_a_single_region_read() -> None:
    config = _config()
    checker = MagicMock()
    checker._last_region_errors = []
    checker.get_region_capacity.return_value = _capacity("us-west-2")

    with patch("cli.capacity.get_multi_region_capacity_checker", return_value=checker):
        section = _gather_capacity(config, ["us-west-2"], _WORKLOAD, _probe(config, ["us-west-2"]))

    checker.get_region_capacity.assert_called_once_with("us-west-2")
    checker.get_all_regions_capacity.assert_not_called()
    assert section.status == STATUS_OK


def test_capacity_section_narrowed_read_failure_degrades_that_region() -> None:
    config = _config()
    checker = MagicMock()
    checker._last_region_errors = []
    checker.get_region_capacity.side_effect = RuntimeError("boom")

    with patch("cli.capacity.get_multi_region_capacity_checker", return_value=checker):
        section = _gather_capacity(config, ["us-west-2"], _WORKLOAD, _probe(config, ["us-west-2"]))

    assert section.status == STATUS_UNAVAILABLE
    assert any("boom" in error for error in section.errors)


# ---------------------------------------------------------------------------
# inference section
# ---------------------------------------------------------------------------


def test_inference_section_summarizes_desired_state() -> None:
    config = _config()
    manager = MagicMock()
    manager.list_endpoints.return_value = [
        {
            "endpoint_name": "my-llm",
            "desired_state": "running",
            "target_regions": ["us-east-1"],
            "namespace": "gco-inference",
            "updated_at": "2026-08-12T14:02:55+00:00",
            "spec": {"image": "should-not-leak"},
        },
        {"endpoint_name": "other", "desired_state": "stopped"},
    ]

    with patch("cli.inference.get_inference_manager", return_value=manager):
        section = _gather_inference(config)

    assert section.status == STATUS_OK
    assert section.data["totals"] == {"running": 1, "stopped": 1}
    assert section.data["count"] == 2
    assert section.data["endpoints"][0]["endpoint_name"] == "my-llm"
    assert "spec" not in section.data["endpoints"][0]


def test_inference_section_zero_endpoints_is_empty() -> None:
    config = _config()
    manager = MagicMock()
    manager.list_endpoints.return_value = []

    with patch("cli.inference.get_inference_manager", return_value=manager):
        section = _gather_inference(config)

    assert section.status == STATUS_EMPTY
    assert section.data["count"] == 0


def test_inference_section_missing_table_is_unavailable_with_the_deploy_hint() -> None:
    config = _config()
    manager = MagicMock()
    manager.list_endpoints.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "no table"}}, "Scan"
    )

    with patch("cli.inference.get_inference_manager", return_value=manager):
        section = _gather_inference(config)

    assert section.status == STATUS_UNAVAILABLE
    assert section.reason is not None
    assert "test-gco-global" in section.reason


def test_inference_section_other_client_errors_escape_to_the_boundary() -> None:
    config = _config()
    manager = MagicMock()
    manager.list_endpoints.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "nope"}}, "Scan"
    )

    with patch("cli.inference.get_inference_manager", return_value=manager):
        try:
            _gather_inference(config)
        except ClientError:
            pass
        else:
            raise AssertionError("expected the access failure to escape")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def test_gather_assembles_every_section_in_order() -> None:
    with _fleet_boundaries(_CDK_REGIONS) as boundaries:
        doc = gather_fleet_status(boundaries.config)

    assert tuple(doc.sections) == SECTION_ORDER
    assert doc.project_name == "test-gco"
    assert doc.sections[SECTION_REGIONS].status == STATUS_OK
    assert doc.sections[SECTION_STACKS].status == STATUS_OK
    assert doc.sections[SECTION_ADDONS].status == STATUS_OK
    assert doc.sections[SECTION_QUEUE].status == STATUS_OK
    parsed = datetime.fromisoformat(doc.generated_at)
    assert parsed.utcoffset() is not None


def test_gather_reads_add_on_status_by_default_once_per_workload_region() -> None:
    with _fleet_boundaries(_CDK_REGIONS) as boundaries:
        gather_fleet_status(boundaries.config)

    assert sorted(c.args for c in boundaries.read_addons.call_args_list) == [
        ("test-gco", "us-east-1"),
        ("test-gco", "us-west-2"),
    ]


def test_gather_reads_add_on_status_only_in_the_flag_region() -> None:
    with _fleet_boundaries(_CDK_REGIONS) as boundaries:
        doc = gather_fleet_status(boundaries.config, region="us-west-2")

    boundaries.read_addons.assert_called_once_with("test-gco", "us-west-2")
    assert list(doc.sections[SECTION_ADDONS].data["by_region"]) == ["us-west-2"]


def test_gather_a_failed_add_on_convergence_degrades_an_otherwise_healthy_fleet() -> None:
    """Live, the base pass failed in every Region and gco status still read OK.

    The stacks were all ``UPDATE_COMPLETE``, because a failed convergence never
    fails the regional stack, and the document had no add-on section.
    """
    with _fleet_boundaries(_CDK_REGIONS) as boundaries:
        boundaries.read_addons.side_effect = _base_pass_failed
        doc = gather_fleet_status(boundaries.config)

    assert doc.sections[SECTION_STACKS].status == STATUS_OK
    assert doc.sections[SECTION_ADDONS].status == STATUS_OK
    assert doc.overall == OVERALL_DEGRADED
    assert doc.degraded == [SECTION_ADDONS]
    quoted = one_line(_BASE_PASS_FAILURE, ADDON_FINDING_MESSAGE_CHARS)
    assert doc.findings == [
        Finding(
            severity=SEVERITY_ERROR,
            section=SECTION_ADDONS,
            message=f"base-manifests failed in {region}: {quoted}",
        )
        for region in _WORKLOAD
    ]


def test_a_failed_base_pass_no_longer_prints_nothing_looks_wrong(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cli.commands.status_cmd import _render_table

    with _fleet_boundaries(_CDK_REGIONS) as boundaries:
        boundaries.read_addons.side_effect = _base_pass_failed
        _render_table(gather_fleet_status(boundaries.config))

    output = capsys.readouterr().out
    assert "Fleet status: DEGRADED" in output
    assert "degraded sections: addons" in output
    assert "nothing looks wrong" not in output
    assert "  [error] addons: base-manifests failed in us-east-1: 04-resource-quotas.yaml" in output
    assert "addons [ok]  ·  gco stacks addons status --all-regions" in output


def test_gather_never_calls_the_all_region_stack_scan() -> None:
    scan = MagicMock(name="discover_regional_stacks")
    with (
        _fleet_boundaries(None),
        patch("cli.aws_client.GCOAWSClient.discover_regional_stacks", scan),
    ):
        gather_fleet_status(_config())

    scan.assert_not_called()


def test_gather_an_unreadable_regional_stack_gates_only_its_own_region() -> None:
    def describe(name: str, region: str) -> StackInfo:
        if name == "test-gco-us-west-2":
            raise _client_error("Throttling", "Rate exceeded")
        return _stack_info(name, region)

    with _fleet_boundaries(_CDK_REGIONS) as boundaries:
        boundaries.stack_manager.get_stack_status.side_effect = describe
        doc = gather_fleet_status(boundaries.config)

    # us-east-1's queue is still read; us-west-2 is skipped, not misread.
    assert boundaries.job_manager.get_queue_status.call_args_list == [call("us-east-1")]
    queue = doc.sections[SECTION_QUEUE]
    assert queue.status == STATUS_PARTIAL
    assert list(queue.data["by_region"]) == ["us-east-1"]
    stacks = doc.sections[SECTION_STACKS]
    assert stacks.status == STATUS_PARTIAL
    west = next(e for e in stacks.data["expected"] if e["name"] == "test-gco-us-west-2")
    assert west["health"] == HEALTH_UNKNOWN
    assert any(
        finding.message == "test-gco-us-west-2 could not be read in us-west-2"
        for finding in doc.findings
    )


def test_gather_short_circuits_per_region_sections_when_regions_unresolved() -> None:
    with _fleet_boundaries(None) as boundaries:
        doc = gather_fleet_status(boundaries.config)

    for name in (SECTION_STACKS, SECTION_ADDONS, SECTION_QUEUE, SECTION_CAPACITY):
        section = doc.sections[name]
        assert section.status == STATUS_UNAVAILABLE
        assert section.reason is not None
        assert "regions section" in section.reason
    boundaries.stack_manager.get_stack_status.assert_not_called()
    boundaries.job_manager.get_queue_status.assert_not_called()
    boundaries.read_addons.assert_not_called()


def test_gather_overall_is_ok_when_only_skipped_sections_remain() -> None:
    with _fleet_boundaries(_CDK_REGIONS) as boundaries:
        doc = gather_fleet_status(boundaries.config)

    assert doc.overall == OVERALL_OK
    assert doc.degraded == []
    assert doc.sections["costs"].status == STATUS_SKIPPED
    assert doc.sections["nodepools"].status == STATUS_SKIPPED


def test_gather_overall_degrades_when_regions_are_unresolved() -> None:
    with _fleet_boundaries(None) as boundaries:
        doc = gather_fleet_status(boundaries.config)

    assert doc.overall == OVERALL_DEGRADED
    assert SECTION_REGIONS in doc.degraded
    assert doc.degraded == [
        name for name in SECTION_ORDER if doc.sections[name].status == STATUS_UNAVAILABLE
    ]


def test_gather_converts_an_escaping_exception_into_an_error_section() -> None:
    with (
        _fleet_boundaries(_CDK_REGIONS) as boundaries,
        patch("cli.status.resolve_regions", side_effect=RuntimeError("boom")),
    ):
        doc = gather_fleet_status(boundaries.config)

    section = doc.sections[SECTION_REGIONS]
    assert section.status == STATUS_ERROR
    assert section.reason is not None
    assert any("boom" in error for error in section.errors)
    assert tuple(doc.sections) == SECTION_ORDER
    assert doc.overall == OVERALL_DEGRADED


def test_gather_one_failing_section_never_suppresses_the_others() -> None:
    with _fleet_boundaries(_CDK_REGIONS) as boundaries:
        boundaries.inference_manager.list_endpoints.side_effect = RuntimeError("registry down")
        doc = gather_fleet_status(boundaries.config)

    assert doc.sections["inference"].status == STATUS_ERROR
    assert any("registry down" in error for error in doc.sections["inference"].errors)
    # Every other section still gathered normally.
    assert doc.sections[SECTION_STACKS].status == STATUS_OK
    assert doc.sections[SECTION_QUEUE].status == STATUS_OK
    assert doc.sections["jobs"].status == STATUS_OK
    assert doc.sections[SECTION_CAPACITY].status == STATUS_OK


def test_gather_marks_opt_in_sections_skipped_by_default() -> None:
    with _fleet_boundaries(_CDK_REGIONS) as boundaries:
        doc = gather_fleet_status(boundaries.config)

    costs = doc.sections["costs"]
    nodepools = doc.sections["nodepools"]
    assert costs.status == STATUS_SKIPPED
    assert costs.reason is not None
    assert "--with-costs" in costs.reason
    assert nodepools.status == STATUS_SKIPPED
    assert nodepools.reason is not None
    assert "--with-nodepools" in nodepools.reason


# ---------------------------------------------------------------------------
# costs section (opt-in)
# ---------------------------------------------------------------------------


def test_costs_section_skipped_without_the_flag_and_issues_no_call() -> None:
    factory = MagicMock()

    with patch("cli.costs.get_cost_tracker", factory):
        section = _gather_costs(_config(), False)

    factory.assert_not_called()
    assert section.status == STATUS_SKIPPED
    assert section.reason is not None
    assert "--with-costs" in section.reason
    assert "bills per request" in section.reason


def test_costs_section_reads_summary_and_tag_status() -> None:
    tracker = MagicMock()
    tracker.get_cost_summary.return_value = _cost_summary()
    tracker.get_cost_allocation_tag_status.return_value = [
        {"tag_key": "Project", "type": "UserDefined", "status": "Active"}
    ]

    with patch("cli.costs.get_cost_tracker", return_value=tracker):
        section = _gather_costs(_config(), True)

    assert section.status == STATUS_OK
    tracker.get_cost_summary.assert_called_once_with(days=30)
    assert section.data["total"] == 12.34
    assert section.data["window_days"] == 30
    assert section.data["by_service"] == [
        {"service": "Amazon Elastic Compute Cloud - Compute", "amount": 12.34}
    ]
    assert section.data["allocation_tags"] == [{"tag_key": "Project", "status": "Active"}]
    assert section.data["as_of"]
    # A by-region breakdown would need a second billed request.
    assert "by_region" not in section.data


def test_costs_section_zero_spend_is_empty() -> None:
    tracker = MagicMock()
    tracker.get_cost_summary.return_value = _cost_summary(total=0.0)
    tracker.get_cost_allocation_tag_status.return_value = []

    with patch("cli.costs.get_cost_tracker", return_value=tracker):
        section = _gather_costs(_config(), True)

    assert section.status == STATUS_EMPTY


def test_costs_section_tag_status_failure_degrades_to_partial() -> None:
    tracker = MagicMock()
    tracker.get_cost_summary.return_value = _cost_summary()
    tracker.get_cost_allocation_tag_status.side_effect = RuntimeError("throttled")

    with patch("cli.costs.get_cost_tracker", return_value=tracker):
        section = _gather_costs(_config(), True)

    assert section.status == STATUS_PARTIAL
    assert section.data["allocation_tags"] is None
    assert any("throttled" in error for error in section.errors)


def test_costs_section_summary_failure_escapes_to_the_boundary() -> None:
    tracker = MagicMock()
    tracker.get_cost_summary.side_effect = RuntimeError("Cost Explorer query failed: denied")

    with patch("cli.costs.get_cost_tracker", return_value=tracker):
        with pytest.raises(RuntimeError) as excinfo:
            _gather_costs(_config(), True)
        e = excinfo.value
        assert "denied" in str(e)


# ---------------------------------------------------------------------------
# nodepools section (opt-in)
# ---------------------------------------------------------------------------


def test_nodepools_section_skipped_without_the_flag_and_issues_no_call() -> None:
    describe = MagicMock()

    with patch("cli.kubectl_helpers.describe_cluster_access", describe):
        section = _gather_nodepools(_config(), False, _WORKLOAD)

    describe.assert_not_called()
    assert section.status == STATUS_SKIPPED
    assert section.reason is not None
    assert "--with-nodepools" in section.reason


def test_nodepools_section_private_endpoint_never_attempts_the_list() -> None:
    describe = MagicMock(
        return_value={"endpoint": "https://eks.example", "public": False, "private": True}
    )
    list_pools = MagicMock()

    with (
        patch("cli.kubectl_helpers.describe_cluster_access", describe),
        patch("cli.nodepools.list_cluster_nodepools", list_pools),
    ):
        section = _gather_nodepools(_config(), True, ["us-east-1"])

    list_pools.assert_not_called()
    assert section.status == STATUS_UNAVAILABLE
    assert section.reason is not None
    assert "gco nodepools list -r <region> --via-ssm auto" in section.reason
    entry = section.data["by_region"]["us-east-1"]
    assert entry["reachable"] is False
    assert "gco nodepools list -r us-east-1 --via-ssm auto" in entry["note"]


def test_nodepools_section_public_endpoint_lists_pools() -> None:
    describe = MagicMock(
        return_value={"endpoint": "https://eks.example", "public": True, "private": True}
    )
    list_pools = MagicMock(return_value=[_nodepool_payload()])

    with (
        patch("cli.kubectl_helpers.describe_cluster_access", describe),
        patch("cli.nodepools.list_cluster_nodepools", list_pools),
    ):
        section = _gather_nodepools(_config(), True, ["us-east-1"])

    list_pools.assert_called_once_with("test-gco-us-east-1", "us-east-1")
    assert section.status == STATUS_OK
    pools = section.data["by_region"]["us-east-1"]["nodepools"]
    assert pools == [
        {
            "name": "gpu-pool",
            "status": "Ready",
            "capacity_types": "spot",
            "instance_types": "g4dn.xlarge, g5.xlarge",
        }
    ]


def test_nodepools_section_mixed_reachability_is_partial() -> None:
    def describe(cluster: str, region: str) -> dict[str, Any]:
        return {"endpoint": "https://eks.example", "public": region == "us-east-1", "private": True}

    with (
        patch("cli.kubectl_helpers.describe_cluster_access", side_effect=describe),
        patch("cli.nodepools.list_cluster_nodepools", return_value=[_nodepool_payload()]),
    ):
        section = _gather_nodepools(_config(), True, _WORKLOAD)

    assert section.status == STATUS_PARTIAL
    assert section.reason == "nodepools listed in 1 of 2 regions"


def test_nodepools_section_probe_failure_everywhere_is_an_error() -> None:
    describe = MagicMock(side_effect=RuntimeError("AWS CLI not found"))

    with patch("cli.kubectl_helpers.describe_cluster_access", describe):
        section = _gather_nodepools(_config(), True, ["us-east-1"])

    assert section.status == STATUS_ERROR
    assert any("AWS CLI not found" in error for error in section.errors)


def test_nodepools_section_without_regions_points_at_the_regions_section() -> None:
    section = _gather_nodepools(_config(), True, [])

    assert section.status == STATUS_UNAVAILABLE
    assert section.reason is not None
    assert "regions section" in section.reason


# ---------------------------------------------------------------------------
# Opt-in wiring through the orchestrator
# ---------------------------------------------------------------------------


def test_gather_default_issues_no_cost_explorer_or_eks_call() -> None:
    with _fleet_boundaries(_CDK_REGIONS) as boundaries:
        doc = gather_fleet_status(boundaries.config)

    boundaries.cost_tracker.get_cost_summary.assert_not_called()
    boundaries.describe_access.assert_not_called()
    boundaries.list_pools.assert_not_called()
    assert doc.sections["costs"].status == STATUS_SKIPPED
    assert doc.sections["nodepools"].status == STATUS_SKIPPED


def test_gather_with_costs_gathers_only_the_costs_section() -> None:
    with _fleet_boundaries(_CDK_REGIONS) as boundaries:
        doc = gather_fleet_status(boundaries.config, with_costs=True)

    boundaries.cost_tracker.get_cost_summary.assert_called_once()
    boundaries.describe_access.assert_not_called()
    assert doc.sections["costs"].status == STATUS_OK
    assert doc.sections["nodepools"].status == STATUS_SKIPPED


def test_gather_with_nodepools_gathers_only_the_nodepools_section() -> None:
    with _fleet_boundaries(_CDK_REGIONS) as boundaries:
        doc = gather_fleet_status(boundaries.config, with_nodepools=True)

    boundaries.cost_tracker.get_cost_summary.assert_not_called()
    assert boundaries.describe_access.call_count == 2
    assert doc.sections["nodepools"].status == STATUS_OK
    assert doc.sections["costs"].status == STATUS_SKIPPED


def test_gather_reuses_a_costs_cache_without_a_new_read() -> None:
    cached = Section(name="costs", status=STATUS_OK, data={"total": 1.0, "as_of": "T0"})

    with _fleet_boundaries(_CDK_REGIONS) as boundaries:
        doc = gather_fleet_status(boundaries.config, with_costs=True, costs_cache=cached)

    boundaries.cost_tracker.get_cost_summary.assert_not_called()
    assert doc.sections["costs"] is cached


def test_gather_ignores_the_costs_cache_when_costs_are_not_requested() -> None:
    cached = Section(name="costs", status=STATUS_OK, data={"total": 1.0})

    with _fleet_boundaries(_CDK_REGIONS) as boundaries:
        doc = gather_fleet_status(boundaries.config, with_costs=False, costs_cache=cached)

    assert doc.sections["costs"].status == STATUS_SKIPPED


def test_gather_times_out_a_stuck_section_without_holding_the_document() -> None:
    with _fleet_boundaries(_CDK_REGIONS) as boundaries:
        boundaries.inference_manager.list_endpoints.side_effect = lambda: time.sleep(5)
        with patch("cli.status.SECTION_TIMEOUT_SECONDS", 0.2):
            doc = gather_fleet_status(boundaries.config)

    section = doc.sections["inference"]
    assert section.status == STATUS_ERROR
    assert section.reason is not None
    assert "timeout" in section.reason
    # The stuck section never held the rest of the document.
    assert doc.sections[SECTION_STACKS].status == STATUS_OK
    assert doc.sections[SECTION_QUEUE].status == STATUS_OK


def test_the_policy_budget_covers_the_per_region_policy_timeout() -> None:
    """With ``--with-policy`` the sections' budget outlasts the policy fetch's bound."""
    from cli import status
    from cli.job_policy import FETCH_TIMEOUT_SECONDS

    assert status.POLICY_SECTION_TIMEOUT_SECONDS > FETCH_TIMEOUT_SECONDS
    assert status.POLICY_SECTION_TIMEOUT_SECONDS >= status.SECTION_TIMEOUT_SECONDS


def test_a_hung_region_costs_the_policy_section_one_region_not_the_section() -> None:
    """A region whose policy read hangs is reported unreachable; the others still compare.

    The policy section waits up to ``FETCH_TIMEOUT_SECONDS`` (45 s) for every
    region, but the section budget was a fixed 30 s, so one hung region turned
    the whole section into a timeout error and dropped the regions that had
    answered.
    """
    from cli.job_policy import FETCH_OK, RegionPolicy
    from gco.job_admission import JobValidationPolicy

    document = {"allowed_namespaces": ["gco-jobs"], "allowed_kinds": ["Job"]}

    def fetch(_client: Any, region: str) -> RegionPolicy:
        if region == "us-west-2":
            time.sleep(5)
        return RegionPolicy(
            region=region,
            status=FETCH_OK,
            policy=JobValidationPolicy.from_policy_document(document),
            document=document,
        )

    with (
        _fleet_boundaries(_CDK_REGIONS) as boundaries,
        patch("cli.job_policy.fetch_region_policy", side_effect=fetch),
        patch("cli.job_policy.FETCH_TIMEOUT_SECONDS", 0.3),
        patch("cli.status.SECTION_TIMEOUT_SECONDS", 0.1),
        patch("cli.status.POLICY_SECTION_TIMEOUT_SECONDS", 1.0),
    ):
        doc = gather_fleet_status(boundaries.config, with_policy=True)

    section = doc.sections["policy"]
    assert section.status == STATUS_PARTIAL, section.reason
    assert section.data["compared"] == ["us-east-1"]
    assert section.data["unreadable"] == {"us-west-2": "timed out after 0.3s"}


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


def _stack_finding_entry(name: str, region: str, status: str | None, health: str) -> dict[str, Any]:
    return {
        "name": name,
        "region": region,
        "status": status,
        "health": health,
        "updated_time": None,
    }


def _stacks_section(
    expected: list[dict[str, Any]], optional: list[dict[str, Any]] | None = None
) -> Section:
    return Section(
        name=SECTION_STACKS,
        status=STATUS_OK,
        data={"expected": expected, "optional": optional or []},
    )


def test_finding_error_for_a_rolled_back_expected_stack() -> None:
    sections = {
        SECTION_STACKS: _stacks_section(
            [
                _stack_finding_entry(
                    "test-gco-us-west-2", "us-west-2", "UPDATE_ROLLBACK_COMPLETE", HEALTH_UNHEALTHY
                )
            ]
        )
    }

    findings = derive_findings(sections)

    assert findings == [
        Finding(
            severity=SEVERITY_ERROR,
            section=SECTION_STACKS,
            message="test-gco-us-west-2 is UPDATE_ROLLBACK_COMPLETE in us-west-2",
        )
    ]


def test_finding_error_for_unavailable_region_telemetry() -> None:
    sections = {
        SECTION_CAPACITY: Section(
            name=SECTION_CAPACITY,
            status=STATUS_UNAVAILABLE,
            data={
                "by_region": {
                    "us-east-1": {
                        "telemetry_status": STATUS_UNAVAILABLE,
                        "unavailable_signals": ["queue", "gpu", "cpu"],
                    }
                }
            },
        )
    }

    findings = derive_findings(sections)

    assert findings == [
        Finding(
            severity=SEVERITY_ERROR,
            section=SECTION_CAPACITY,
            message="us-east-1 telemetry is unavailable",
        )
    ]


def test_finding_warn_for_an_absent_expected_stack_says_not_deployed() -> None:
    # not-deployed now means CloudFormation said the stack does not exist; an
    # unreadable stack is `unknown` (next test).
    sections = {
        SECTION_STACKS: _stacks_section(
            [_stack_finding_entry("test-gco-monitoring", "us-east-2", None, HEALTH_NOT_DEPLOYED)]
        )
    }

    findings = derive_findings(sections)

    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_WARN
    assert findings[0].message == "test-gco-monitoring is not deployed in us-east-2"


def test_finding_warn_for_an_unreadable_expected_stack_says_it_could_not_be_read() -> None:
    sections = {
        SECTION_STACKS: _stacks_section(
            [_stack_finding_entry("test-gco-monitoring", "us-east-2", None, HEALTH_UNKNOWN)]
        )
    }

    findings = derive_findings(sections)

    assert findings == [
        Finding(
            severity=SEVERITY_WARN,
            section=SECTION_STACKS,
            message="test-gco-monitoring could not be read in us-east-2",
        )
    ]


def test_finding_warn_for_a_dead_letter_queue_with_messages() -> None:
    sections = {
        SECTION_QUEUE: Section(
            name=SECTION_QUEUE,
            status=STATUS_OK,
            data={
                "by_region": {
                    "us-west-2": {"available": 0, "in_flight": 0, "delayed": 0, "dlq": 2},
                    "us-east-1": {"available": 3, "in_flight": 1, "delayed": 0, "dlq": 0},
                }
            },
        )
    }

    findings = derive_findings(sections)

    assert findings == [
        Finding(
            severity=SEVERITY_WARN,
            section=SECTION_QUEUE,
            message="us-west-2 dead-letter queue holds 2 messages",
        )
    ]


def test_finding_warn_for_a_single_dlq_message_is_singular() -> None:
    sections = {
        SECTION_QUEUE: Section(
            name=SECTION_QUEUE,
            status=STATUS_OK,
            data={"by_region": {"us-east-1": {"dlq": 1}}},
        )
    }

    findings = derive_findings(sections)

    assert findings[0].message == "us-east-1 dead-letter queue holds 1 message"


def test_finding_ignores_unknown_dlq_depth() -> None:
    sections = {
        SECTION_QUEUE: Section(
            name=SECTION_QUEUE,
            status=STATUS_OK,
            data={"by_region": {"us-east-1": {"dlq": None}}},
        )
    }

    assert derive_findings(sections) == []


def test_finding_warn_for_an_in_progress_stack() -> None:
    sections = {
        SECTION_STACKS: _stacks_section(
            [
                _stack_finding_entry(
                    "test-gco-us-east-1", "us-east-1", "UPDATE_IN_PROGRESS", HEALTH_IN_PROGRESS
                )
            ]
        )
    }

    findings = derive_findings(sections)

    assert findings == [
        Finding(
            severity=SEVERITY_WARN,
            section=SECTION_STACKS,
            message="test-gco-us-east-1 is UPDATE_IN_PROGRESS in us-east-1",
        )
    ]


def test_finding_warn_for_partial_region_telemetry_names_the_signals() -> None:
    sections = {
        SECTION_CAPACITY: Section(
            name=SECTION_CAPACITY,
            status=STATUS_PARTIAL,
            data={
                "by_region": {
                    "us-west-2": {
                        "telemetry_status": STATUS_PARTIAL,
                        "unavailable_signals": ["gpu"],
                    }
                }
            },
        )
    }

    findings = derive_findings(sections)

    assert findings == [
        Finding(
            severity=SEVERITY_WARN,
            section=SECTION_CAPACITY,
            message="us-west-2 telemetry is partial (unavailable: gpu)",
        )
    ]


def test_finding_warn_for_a_truncated_job_count_scan() -> None:
    sections = {
        SECTION_JOBS: Section(
            name=SECTION_JOBS,
            status=STATUS_OK,
            data={
                "totals": {"total": 41, "queued": 3, "running": 1},
                "by_region": {},
                "complete": False,
                "records_evaluated": 41,
            },
        )
    }

    findings = derive_findings(sections)

    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_WARN
    assert findings[0].section == SECTION_JOBS
    assert "truncated after 41 records" in findings[0].message
    assert "floor" in findings[0].message


def test_findings_ignore_optional_stacks_entirely() -> None:
    sections = {
        SECTION_STACKS: _stacks_section(
            expected=[
                _stack_finding_entry(
                    "test-gco-global", "us-east-2", "UPDATE_COMPLETE", HEALTH_HEALTHY
                )
            ],
            optional=[
                _stack_finding_entry(
                    "test-gco-analytics", "us-east-2", "UPDATE_ROLLBACK_COMPLETE", HEALTH_UNHEALTHY
                )
            ],
        )
    }

    assert derive_findings(sections) == []


def test_findings_order_errors_before_warns() -> None:
    sections = {
        SECTION_STACKS: _stacks_section(
            [
                _stack_finding_entry("test-gco-monitoring", "us-east-2", None, HEALTH_NOT_DEPLOYED),
                _stack_finding_entry(
                    "test-gco-us-west-2", "us-west-2", "UPDATE_ROLLBACK_COMPLETE", HEALTH_UNHEALTHY
                ),
            ]
        ),
        SECTION_QUEUE: Section(
            name=SECTION_QUEUE,
            status=STATUS_OK,
            data={"by_region": {"us-west-2": {"dlq": 2}}},
        ),
        SECTION_CAPACITY: Section(
            name=SECTION_CAPACITY,
            status=STATUS_UNAVAILABLE,
            data={"by_region": {"us-east-1": {"telemetry_status": STATUS_UNAVAILABLE}}},
        ),
    }

    findings = derive_findings(sections)

    severities = [finding.severity for finding in findings]
    assert severities == [SEVERITY_ERROR, SEVERITY_ERROR, SEVERITY_WARN, SEVERITY_WARN]


def test_findings_are_empty_for_a_clean_document() -> None:
    sections = {
        SECTION_STACKS: _stacks_section(
            [
                _stack_finding_entry(
                    "test-gco-global", "us-east-2", "CREATE_COMPLETE", HEALTH_HEALTHY
                )
            ]
        ),
        SECTION_QUEUE: Section(
            name=SECTION_QUEUE, status=STATUS_OK, data={"by_region": {"us-east-1": {"dlq": 0}}}
        ),
        SECTION_CAPACITY: Section(
            name=SECTION_CAPACITY,
            status=STATUS_OK,
            data={"by_region": {"us-east-1": {"telemetry_status": "complete"}}},
        ),
        SECTION_JOBS: Section(
            name=SECTION_JOBS, status=STATUS_OK, data={"complete": True, "records_evaluated": 4}
        ),
    }

    assert derive_findings(sections) == []


def test_findings_skip_sections_with_no_data() -> None:
    sections = {
        SECTION_STACKS: Section(name=SECTION_STACKS, status=STATUS_UNAVAILABLE),
        SECTION_ADDONS: Section(name=SECTION_ADDONS, status=STATUS_UNAVAILABLE),
        SECTION_QUEUE: Section(name=SECTION_QUEUE, status=STATUS_UNAVAILABLE),
        SECTION_CAPACITY: Section(name=SECTION_CAPACITY, status=STATUS_UNAVAILABLE),
        SECTION_JOBS: Section(name=SECTION_JOBS, status=STATUS_UNAVAILABLE),
    }

    assert derive_findings(sections) == []


def _addons_section(by_region: dict[str, list[dict[str, Any]] | None]) -> Section:
    """An ``addons`` section; a Region given None could not be read."""
    return Section(
        name=SECTION_ADDONS,
        status=STATUS_OK,
        data={
            "by_region": {
                region: {"readable": records is not None, "addons": records or []}
                for region, records in by_region.items()
            }
        },
    )


def _addon_entry(name: str, status: str, health: str, message: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "health": health,
        "message": message,
        "updated_at": None,
    }


def test_finding_error_for_a_failed_add_on_quotes_its_message_shortened() -> None:
    sections = {
        SECTION_ADDONS: _addons_section(
            {
                "us-east-1": [
                    _addon_entry("base-manifests", "failed", HEALTH_UNHEALTHY, _BASE_PASS_FAILURE)
                ]
            }
        )
    }

    [finding] = derive_findings(sections)

    assert len(_BASE_PASS_FAILURE) > ADDON_FINDING_MESSAGE_CHARS
    assert finding == Finding(
        severity=SEVERITY_ERROR,
        section=SECTION_ADDONS,
        message=(
            "base-manifests failed in us-east-1: "
            f"{_BASE_PASS_FAILURE[: ADDON_FINDING_MESSAGE_CHARS - 1]}…"
        ),
    )


def test_finding_for_a_failed_add_on_without_a_message_names_only_where() -> None:
    sections = {
        SECTION_ADDONS: _addons_section(
            {"us-west-2": [_addon_entry("keda", "failed", HEALTH_UNHEALTHY)]}
        )
    }

    assert [f.message for f in derive_findings(sections)] == ["keda failed in us-west-2"]


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (
            _addon_entry(
                "_teardown", "teardown-active", HEALTH_IN_PROGRESS, "execution teardown-abc123"
            ),
            "_teardown is teardown-active in us-east-1: execution teardown-abc123",
        ),
        (
            _addon_entry("volcano", "unknown", HEALTH_UNKNOWN, "not\n  json"),
            "volcano is unknown in us-east-1: not json",
        ),
    ],
    ids=["in-progress", "unknown"],
)
def test_finding_warn_for_an_add_on_neither_healthy_nor_failed(
    entry: dict[str, Any], message: str
) -> None:
    sections = {SECTION_ADDONS: _addons_section({"us-east-1": [entry]})}

    assert derive_findings(sections) == [
        Finding(severity=SEVERITY_WARN, section=SECTION_ADDONS, message=message)
    ]


def test_findings_ignore_healthy_add_ons_and_regions_without_records() -> None:
    sections = {
        SECTION_ADDONS: _addons_section(
            {
                "us-east-1": [
                    _addon_entry("base-manifests", "applied", HEALTH_HEALTHY, "applied=41"),
                    _addon_entry("keda", "installed", HEALTH_HEALTHY),
                ],
                "us-west-2": [],
                "eu-west-1": None,
            }
        )
    }

    assert derive_findings(sections) == []


def test_add_on_findings_come_right_after_the_stack_findings() -> None:
    sections = {
        SECTION_STACKS: _stacks_section(
            [
                _stack_finding_entry(
                    "test-gco-us-west-2", "us-west-2", "UPDATE_ROLLBACK_COMPLETE", HEALTH_UNHEALTHY
                ),
                _stack_finding_entry(
                    "test-gco-us-east-1", "us-east-1", "UPDATE_IN_PROGRESS", HEALTH_IN_PROGRESS
                ),
            ]
        ),
        SECTION_ADDONS: _addons_section(
            {
                "us-east-1": [
                    _addon_entry("_teardown", "teardown-active", HEALTH_IN_PROGRESS),
                    _addon_entry("base-manifests", "failed", HEALTH_UNHEALTHY),
                ]
            }
        ),
        SECTION_CAPACITY: Section(
            name=SECTION_CAPACITY,
            status=STATUS_OK,
            data={"by_region": {"us-east-1": {"telemetry_status": STATUS_UNAVAILABLE}}},
        ),
        SECTION_QUEUE: Section(
            name=SECTION_QUEUE, status=STATUS_OK, data={"by_region": {"us-east-1": {"dlq": 2}}}
        ),
    }

    findings = derive_findings(sections)

    assert [(f.severity, f.section) for f in findings] == [
        (SEVERITY_ERROR, SECTION_STACKS),
        (SEVERITY_ERROR, SECTION_ADDONS),
        (SEVERITY_ERROR, SECTION_CAPACITY),
        (SEVERITY_WARN, SECTION_STACKS),
        (SEVERITY_WARN, SECTION_ADDONS),
        (SEVERITY_WARN, SECTION_QUEUE),
    ]


@pytest.mark.parametrize(
    ("text", "limit", "shortened"),
    [
        ("applied=41 expected=41", 40, "applied=41 expected=41"),
        ("helm: timed out\n\twaiting  for keda", 80, "helm: timed out waiting for keda"),
        ("x" * 12, 10, "x" * 9 + "…"),
        # The cut never leaves a space before the ellipsis.
        ("abcd efgh", 6, "abcd…"),
    ],
)
def test_one_line_collapses_whitespace_and_cuts_long_text(
    text: str, limit: int, shortened: str
) -> None:
    assert one_line(text, limit) == shortened
    assert len(one_line(text, limit)) <= limit


# ---------------------------------------------------------------------------
# Overall verdict derivation
# ---------------------------------------------------------------------------


def _ok_sections() -> dict[str, Section]:
    return {name: Section(name=name, status=STATUS_OK) for name in SECTION_ORDER}


def test_overall_ok_for_a_clean_document() -> None:
    assert _derive_overall(_ok_sections(), []) == (OVERALL_OK, [])


def test_overall_skipped_sections_alone_never_degrade() -> None:
    sections = _ok_sections()
    sections["costs"] = Section(name="costs", status=STATUS_SKIPPED, reason="not requested")
    sections["nodepools"] = Section(name="nodepools", status=STATUS_SKIPPED, reason="not requested")

    assert _derive_overall(sections, []) == (OVERALL_OK, [])


def test_overall_degrades_per_section_status() -> None:
    for degraded_status in (STATUS_PARTIAL, STATUS_UNAVAILABLE, STATUS_ERROR):
        sections = _ok_sections()
        sections[SECTION_QUEUE] = Section(
            name=SECTION_QUEUE, status=degraded_status, reason="because"
        )

        overall, degraded = _derive_overall(sections, [])

        assert overall == OVERALL_DEGRADED, degraded_status
        assert degraded == [SECTION_QUEUE]


def test_overall_error_finding_degrades_and_names_the_section() -> None:
    finding = Finding(severity=SEVERITY_ERROR, section=SECTION_STACKS, message="rolled back")

    overall, degraded = _derive_overall(_ok_sections(), [finding])

    assert overall == OVERALL_DEGRADED
    assert degraded == [SECTION_STACKS]


def test_overall_warn_finding_degrades_without_naming_the_section() -> None:
    finding = Finding(severity=SEVERITY_WARN, section=SECTION_QUEUE, message="dlq depth 2")

    overall, degraded = _derive_overall(_ok_sections(), [finding])

    assert overall == OVERALL_DEGRADED
    assert degraded == []


def test_overall_degraded_list_follows_section_order() -> None:
    sections = _ok_sections()
    sections[SECTION_CAPACITY] = Section(name=SECTION_CAPACITY, status=STATUS_PARTIAL, reason="x")
    finding = Finding(severity=SEVERITY_ERROR, section=SECTION_STACKS, message="rolled back")

    _, degraded = _derive_overall(sections, [finding])

    assert degraded == [SECTION_STACKS, SECTION_CAPACITY]
