"""Tests for ``cli/capacity/training_plans.py`` and the ``gco capacity`` Training Plan commands.

The module runs against real botocore SageMaker clients behind ``Stubber``
so every call and its parameters are asserted without a network; the
commands run through ``CliRunner`` with the module's lookups patched.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from botocore.stub import Stubber
from click.testing import CliRunner

from cli.capacity import training_plans as tp
from cli.main import cli

_REGIONS = ["us-east-1", "us-west-2"]
_OFFERING_ID = "tpo-0123456789abcdef"
_PLAN_ARN = "arn:aws:sagemaker:us-east-1:123456789012:training-plan/p5-plan"
_START = datetime(2026, 11, 10, 0, 0, tzinfo=UTC)
_END = datetime(2026, 11, 17, 0, 0, tzinfo=UTC)


class _Clients:
    def __init__(self) -> None:
        kwargs = {
            "aws_access_key_id": "testing",
            "aws_secret_access_key": "testing",  # nosec B106  # fake credentials for stubbed clients
        }
        self.clients = {
            region: boto3.client("sagemaker", region_name=region, **kwargs) for region in _REGIONS
        }
        self.stubs = {region: Stubber(client) for region, client in self.clients.items()}
        self.requested: list[tuple[str, str]] = []

    def factory(self, service: str, *, region_name: str) -> Any:
        self.requested.append((service, region_name))
        return self.clients[region_name]

    def __getitem__(self, region: str) -> Stubber:
        return self.stubs[region]


@pytest.fixture
def clients() -> Iterator[_Clients]:
    stubbed = _Clients()
    for stub in stubbed.stubs.values():
        stub.activate()
    yield stubbed
    for stub in stubbed.stubs.values():
        stub.assert_no_pending_responses()
        stub.deactivate()


def _offering(offering_id: str = _OFFERING_ID, **overrides: Any) -> dict[str, Any]:
    offering: dict[str, Any] = {
        "TrainingPlanOfferingId": offering_id,
        "TargetResources": ["hyperpod-cluster"],
        "DurationHours": 168,
        "UpfrontFee": "33600.00",
        "CurrencyCode": "USD",
        "ReservedCapacityOfferings": [
            {
                "InstanceType": "ml.p5.48xlarge",
                "InstanceCount": 4,
                "AvailabilityZone": "us-east-1a",
                "DurationHours": 168,
                "StartTime": _START,
                "EndTime": _END,
                "ReservedCapacityType": "InstanceType",
            }
        ],
    }
    offering.update(overrides)
    return offering


def _plan(name: str = "p5-plan", **overrides: Any) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "TrainingPlanArn": _PLAN_ARN,
        "TrainingPlanName": name,
        "Status": "Active",
        "DurationHours": 168,
        "StartTime": _START,
        "EndTime": _END,
        "UpfrontFee": "33600.00",
        "CurrencyCode": "USD",
        "TotalInstanceCount": 4,
        "AvailableInstanceCount": 3,
        "InUseInstanceCount": 1,
        "TargetResources": ["hyperpod-cluster"],
        "ReservedCapacitySummaries": [
            {
                "ReservedCapacityArn": f"{_PLAN_ARN}/rc-1",
                "InstanceType": "ml.p5.48xlarge",
                "TotalInstanceCount": 4,
                "Status": "Active",
                "AvailabilityZone": "us-east-1a",
                "StartTime": _START,
                "EndTime": _END,
            }
        ],
    }
    plan.update(overrides)
    return plan


# ---------------------------------------------------------------------------
# normalize_ml_instance_type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("ml.p5.48xlarge", "ml.p5.48xlarge"),
        ("p5.48xlarge", "ml.p5.48xlarge"),
        ("p5", "ml.p5.48xlarge"),
        ("p6-b200", "ml.p6-b200.48xlarge"),
        (" ml.g5.8xlarge ", "ml.g5.8xlarge"),
    ],
)
def test_normalize_ml_instance_type(given: str, expected: str) -> None:
    assert tp.normalize_ml_instance_type(given) == expected


def test_normalize_ml_instance_type_requires_a_name() -> None:
    with pytest.raises(ValueError, match="instance type is required"):
        tp.normalize_ml_instance_type("  ")


# ---------------------------------------------------------------------------
# search_offerings
# ---------------------------------------------------------------------------


def test_search_offerings_ranks_cheapest_per_instance_hour_first(clients: _Clients) -> None:
    clients["us-east-1"].add_response(
        "search_training_plan_offerings",
        {
            "TrainingPlanOfferings": [
                _offering("tpo-expensive", UpfrontFee="67200.00"),
                _offering("tpo-cheap"),
            ]
        },
        {
            "InstanceType": "ml.p5.48xlarge",
            "InstanceCount": 4,
            "TargetResources": ["hyperpod-cluster"],
            "DurationHours": 168,
            "StartTimeAfter": datetime(2026, 11, 10, tzinfo=UTC),
            "EndTimeBefore": datetime(2026, 11, 30, 23, 59, 59, tzinfo=UTC),
        },
    )
    clients["us-west-2"].add_response(
        "search_training_plan_offerings",
        {"TrainingPlanOfferings": []},
        {
            "InstanceType": "ml.p5.48xlarge",
            "InstanceCount": 4,
            "TargetResources": ["hyperpod-cluster"],
            "DurationHours": 168,
            "StartTimeAfter": datetime(2026, 11, 10, tzinfo=UTC),
            "EndTimeBefore": datetime(2026, 11, 30, 23, 59, 59, tzinfo=UTC),
        },
    )
    result = tp.search_offerings(
        "p5",
        regions=["us-east-1", "us-west-2", "us-east-1"],
        instance_count=4,
        duration_hours=168,
        start_after="2026-11-10",
        end_before="2026-11-30",
        client_factory=clients.factory,
    )
    assert result["instance_type"] == "ml.p5.48xlarge"
    assert result["requested_instance_type"] == "p5"
    assert result["regions_checked"] == ["us-east-1", "us-west-2"]
    assert result["regions_with_offerings"] == ["us-east-1"]
    assert result["offerings_found"] == 2
    assert result["window"] == {
        "start_after": "2026-11-10T00:00:00+00:00",
        "end_before": "2026-11-30T23:59:59+00:00",
    }
    assert [o["offering_id"] for o in result["offerings"]] == ["tpo-cheap", "tpo-expensive"]
    best = result["best"]
    assert best == {
        "region": "us-east-1",
        "offering_id": "tpo-cheap",
        "target_resources": ["hyperpod-cluster"],
        "duration_hours": 168,
        "duration_days": 7,
        "upfront_fee_usd": 33600.0,
        "currency": "USD",
        "instance_count": 4,
        "availability_zones": ["us-east-1a"],
        "start_time": _START.isoformat(),
        "end_time": _END.isoformat(),
        "price_per_instance_hour": 50.0,
        "reserved_capacity": [
            {
                "instance_type": "ml.p5.48xlarge",
                "instance_count": 4,
                "availability_zone": "us-east-1a",
                "duration_hours": 168,
                "start_time": _START.isoformat(),
                "end_time": _END.isoformat(),
                "type": "InstanceType",
            }
        ],
    }
    assert result["errors"] == []
    assert sorted(clients.requested) == [("sagemaker", "us-east-1"), ("sagemaker", "us-west-2")]


def test_search_offerings_without_optional_filters_and_without_pricing(clients: _Clients) -> None:
    bare = _offering()
    del bare["UpfrontFee"]
    bare["ReservedCapacityOfferings"] = [{"InstanceType": "ml.p5.48xlarge", "InstanceCount": 1}]
    clients["us-east-1"].add_response(
        "search_training_plan_offerings",
        {"TrainingPlanOfferings": [bare, _offering("tpo-later", UpfrontFee="100")]},
        {"InstanceType": "ml.p5.48xlarge", "InstanceCount": 1, "TargetResources": ["training-job"]},
    )
    result = tp.search_offerings(
        "ml.p5.48xlarge",
        regions=["us-east-1"],
        target="training-job",
        client_factory=clients.factory,
    )
    assert result["duration_hours"] is None
    assert result["window"] == {"start_after": None, "end_before": None}
    # The priced offering ranks first; the unpriced one has no rate, zones or times.
    assert [o["offering_id"] for o in result["offerings"]] == ["tpo-later", _OFFERING_ID]
    unpriced = result["offerings"][1]
    assert unpriced["upfront_fee_usd"] is None
    assert unpriced["price_per_instance_hour"] is None
    assert unpriced["availability_zones"] == []
    assert (unpriced["start_time"], unpriced["end_time"]) == (None, None)


def test_search_offerings_records_one_regions_error_and_raises_when_all_fail(
    clients: _Clients,
) -> None:
    clients["us-east-1"].add_client_error(
        "search_training_plan_offerings",
        service_error_code="ValidationException",
        service_message="InstanceCount must be at most 256",
    )
    clients["us-west-2"].add_response(
        "search_training_plan_offerings",
        {"TrainingPlanOfferings": [_offering()]},
        {
            "InstanceType": "ml.p5.48xlarge",
            "InstanceCount": 300,
            "TargetResources": ["hyperpod-cluster"],
        },
    )
    result = tp.search_offerings(
        "ml.p5.48xlarge", regions=_REGIONS, instance_count=300, client_factory=clients.factory
    )
    assert result["offerings_found"] == 1
    assert result["errors"] == [
        {
            "region": "us-east-1",
            "error_code": "ValidationException",
            "error": "InstanceCount must be at most 256",
        }
    ]

    clients["us-east-1"].add_client_error(
        "search_training_plan_offerings", service_error_code="AccessDeniedException"
    )
    clients["us-west-2"].add_client_error(
        "search_training_plan_offerings", service_error_code="ThrottlingException"
    )
    with pytest.raises(tp.TrainingPlanError, match="in every Region") as excinfo:
        tp.search_offerings("ml.p5.48xlarge", regions=_REGIONS, client_factory=clients.factory)
    assert "us-east-1: AccessDeniedException" in str(excinfo.value)
    assert "us-west-2: ThrottlingException" in str(excinfo.value)


def test_search_offerings_transport_errors_are_recorded_by_type() -> None:
    class _Sagemaker:
        def search_training_plan_offerings(self, **kwargs: Any) -> dict[str, Any]:
            raise EndpointConnectionError(endpoint_url="https://sagemaker.example")

    with pytest.raises(tp.TrainingPlanError, match="EndpointConnectionError"):
        tp.search_offerings(
            "ml.p5.48xlarge", regions=["us-east-1"], client_factory=lambda *_a, **_k: _Sagemaker()
        )


def test_search_offerings_rejects_bad_arguments() -> None:
    with pytest.raises(ValueError, match="target must be one of"):
        tp.search_offerings("ml.p5.48xlarge", regions=["us-east-1"], target="endpoint")
    with pytest.raises(ValueError, match="at least one Region"):
        tp.search_offerings("ml.p5.48xlarge", regions=[])
    with pytest.raises(ValueError, match="Invalid date"):
        tp.search_offerings("ml.p5.48xlarge", regions=["us-east-1"], start_after="soon")


# ---------------------------------------------------------------------------
# list_plans
# ---------------------------------------------------------------------------


def test_list_plans_paginates_filters_and_sorts(clients: _Clients) -> None:
    later = _plan(
        "later-plan",
        TrainingPlanArn=_PLAN_ARN.replace("p5-plan", "later-plan"),
        StartTime=datetime(2026, 12, 1, tzinfo=UTC),
        Status="Scheduled",
    )
    other_type = _plan(
        "trn-plan",
        TrainingPlanArn=_PLAN_ARN.replace("p5-plan", "trn-plan"),
        ReservedCapacitySummaries=[
            {
                "ReservedCapacityArn": f"{_PLAN_ARN}/rc-2",
                "InstanceType": "ml.trn2.48xlarge",
                "TotalInstanceCount": 2,
                "Status": "Active",
            }
        ],
    )
    clients["us-east-1"].add_response(
        "list_training_plans",
        {"TrainingPlanSummaries": [later], "NextToken": "page-2"},
        {"Filters": [{"Name": "Status", "Value": "Active"}]},
    )
    clients["us-east-1"].add_response(
        "list_training_plans",
        {"TrainingPlanSummaries": [_plan(), other_type]},
        {"Filters": [{"Name": "Status", "Value": "Active"}], "NextToken": "page-2"},
    )
    clients["us-west-2"].add_response(
        "list_training_plans",
        {"TrainingPlanSummaries": []},
        {"Filters": [{"Name": "Status", "Value": "Active"}]},
    )
    result = tp.list_plans(
        regions=_REGIONS, instance_type="p5", status="Active", client_factory=clients.factory
    )
    assert result["instance_type"] == "ml.p5.48xlarge"
    assert result["status"] == "Active"
    assert result["plans_found"] == 2  # the trn2 plan is filtered out
    assert [p["name"] for p in result["plans"]] == ["p5-plan", "later-plan"]  # soonest first
    assert result["total_instances"] == 8
    assert result["available_instances"] == 6
    assert result["plans"][0] == {
        "region": "us-east-1",
        "arn": _PLAN_ARN,
        "name": "p5-plan",
        "status": "Active",
        "status_message": None,
        "target_resources": ["hyperpod-cluster"],
        "duration_hours": 168,
        "start_time": _START.isoformat(),
        "end_time": _END.isoformat(),
        "upfront_fee_usd": 33600.0,
        "currency": "USD",
        "total_instances": 4,
        "available_instances": 3,
        "in_use_instances": 1,
        "instance_types": ["ml.p5.48xlarge"],
        "availability_zones": ["us-east-1a"],
        "reserved_capacity": [
            {
                "instance_type": "ml.p5.48xlarge",
                "total_instances": 4,
                "availability_zone": "us-east-1a",
                "status": "Active",
                "start_time": _START.isoformat(),
                "end_time": _END.isoformat(),
            }
        ],
    }
    assert result["errors"] == []


def test_list_plans_without_filters_keeps_every_plan(clients: _Clients) -> None:
    bare = _plan("bare")
    for key in ("StartTime", "EndTime", "UpfrontFee", "ReservedCapacitySummaries"):
        del bare[key]
    clients["us-east-1"].add_response(
        "list_training_plans", {"TrainingPlanSummaries": [bare, _plan()]}, {}
    )
    result = tp.list_plans(regions=["us-east-1"], client_factory=clients.factory)
    assert result["instance_type"] is None
    assert [p["name"] for p in result["plans"]] == ["p5-plan", "bare"]  # no start sorts last
    assert result["plans"][1]["instance_types"] == []
    assert result["plans"][1]["upfront_fee_usd"] is None


def test_list_plans_errors(clients: _Clients) -> None:
    clients["us-east-1"].add_client_error(
        "list_training_plans", service_error_code="AccessDeniedException", service_message="no"
    )
    clients["us-west-2"].add_response("list_training_plans", {"TrainingPlanSummaries": []}, {})
    result = tp.list_plans(regions=_REGIONS, client_factory=clients.factory)
    assert result["plans_found"] == 0
    assert result["errors"] == [
        {"region": "us-east-1", "error_code": "AccessDeniedException", "error": "no"}
    ]
    with pytest.raises(ValueError, match="at least one Region"):
        tp.list_plans(regions=[])


# ---------------------------------------------------------------------------
# The commands
# ---------------------------------------------------------------------------


def _search_result(**overrides: Any) -> dict[str, Any]:
    offering = {
        "region": "us-east-1",
        "offering_id": _OFFERING_ID,
        "target_resources": ["hyperpod-cluster"],
        "duration_hours": 168,
        "duration_days": 7,
        "upfront_fee_usd": 33600.0,
        "currency": "USD",
        "instance_count": 4,
        "availability_zones": ["us-east-1a"],
        "start_time": _START.isoformat(),
        "end_time": _END.isoformat(),
        "price_per_instance_hour": 50.0,
        "reserved_capacity": [],
    }
    result: dict[str, Any] = {
        "instance_type": "ml.p5.48xlarge",
        "requested_instance_type": "p5",
        "instance_count": 4,
        "target": "hyperpod-cluster",
        "duration_hours": 168,
        "window": {"start_after": "2026-11-10T00:00:00+00:00", "end_before": None},
        "regions_checked": ["us-east-1"],
        "regions_with_offerings": ["us-east-1"],
        "offerings_found": 1,
        "offerings": [offering],
        "best": offering,
        "errors": [],
    }
    result.update(overrides)
    return result


def _list_result(**overrides: Any) -> dict[str, Any]:
    plan = {
        "region": "us-east-1",
        "arn": _PLAN_ARN,
        "name": "p5-plan",
        "status": "Active",
        "status_message": None,
        "target_resources": ["hyperpod-cluster"],
        "duration_hours": 168,
        "start_time": _START.isoformat(),
        "end_time": _END.isoformat(),
        "upfront_fee_usd": 33600.0,
        "currency": "USD",
        "total_instances": 4,
        "available_instances": 3,
        "in_use_instances": 1,
        "instance_types": ["ml.p5.48xlarge"],
        "availability_zones": ["us-east-1a"],
        "reserved_capacity": [],
    }
    result: dict[str, Any] = {
        "instance_type": None,
        "status": None,
        "regions_checked": ["us-east-1"],
        "plans_found": 1,
        "total_instances": 4,
        "available_instances": 3,
        "plans": [plan],
        "errors": [],
    }
    result.update(overrides)
    return result


class TestFindTrainingPlansCommand:
    def test_table_with_explicit_regions(self) -> None:
        with patch(
            "cli.capacity.training_plans.search_offerings", return_value=_search_result()
        ) as search:
            result = CliRunner().invoke(
                cli,
                [
                    "capacity",
                    "find-training-plans",
                    "-i",
                    "p5",
                    "-c",
                    "4",
                    "-r",
                    "us-east-1",
                    "--duration-days",
                    "7",
                    "--start-after",
                    "2026-11-10",
                ],
            )
        assert result.exit_code == 0, result.output
        assert search.call_args.args == ("p5",)
        assert search.call_args.kwargs == {
            "regions": ["us-east-1"],
            "instance_count": 4,
            "duration_hours": 168,
            "start_after": "2026-11-10",
            "end_before": None,
            "target": "hyperpod-cluster",
        }
        assert "Training Plan offerings for 4 x p5 -> ml.p5.48xlarge" in result.output
        assert "Duration: 168h    Start after: 2026-11-10T00:00    End before: any" in result.output
        assert (
            "us-east-1     us-east-1a    2026-11-10T00:00  2026-11-17T00:00      4     $33,600     $50.00"
            in result.output
        )
        assert f"offering {_OFFERING_ID}" in result.output
        assert "create-training-plan" in result.output

    def test_defaults_to_the_deployed_regions_and_prints_json(self) -> None:
        aws_client = MagicMock()
        aws_client.discover_regional_stacks.return_value = {
            "eu-west-1": object(),
            "us-east-1": object(),
        }
        with (
            patch(
                "cli.capacity.training_plans.search_offerings", return_value=_search_result()
            ) as search,
            patch("cli.aws_client.get_aws_client", return_value=aws_client),
        ):
            result = CliRunner().invoke(
                cli,
                [
                    "--output",
                    "json",
                    "capacity",
                    "find-training-plans",
                    "-i",
                    "ml.p5.48xlarge",
                    "--target",
                    "training-job",
                ],
            )
        assert result.exit_code == 0, result.output
        assert search.call_args.kwargs["regions"] == ["eu-west-1", "us-east-1"]
        assert search.call_args.kwargs["target"] == "training-job"
        assert search.call_args.kwargs["duration_hours"] is None
        assert json.loads(result.output)["offerings_found"] == 1

    def test_no_deployed_stacks_falls_back_to_the_default_region(self) -> None:
        aws_client = MagicMock()
        aws_client.discover_regional_stacks.return_value = {}
        with (
            patch(
                "cli.capacity.training_plans.search_offerings", return_value=_search_result()
            ) as search,
            patch("cli.aws_client.get_aws_client", return_value=aws_client),
        ):
            result = CliRunner().invoke(cli, ["capacity", "find-training-plans", "-i", "p5"])
        assert result.exit_code == 0, result.output
        assert len(search.call_args.kwargs["regions"]) == 1

    def test_no_offerings_and_regional_errors(self) -> None:
        empty = _search_result(
            instance_type="ml.p5.48xlarge",
            requested_instance_type="ml.p5.48xlarge",
            offerings=[],
            best=None,
            offerings_found=0,
            regions_with_offerings=[],
            duration_hours=None,
            window={"start_after": None, "end_before": None},
            errors=[{"region": "us-west-2", "error_code": "AccessDeniedException", "error": "no"}],
        )
        with patch("cli.capacity.training_plans.search_offerings", return_value=empty):
            result = CliRunner().invoke(
                cli, ["capacity", "find-training-plans", "-i", "ml.p5.48xlarge", "-r", "us-east-1"]
            )
        assert result.exit_code == 0, result.output
        assert "Training Plan offerings for 4 x ml.p5.48xlarge" in result.output
        assert "Duration: any" in result.output
        assert "SageMaker refused the lookup in 1 Region(s)" in result.output
        assert "us-west-2: AccessDeniedException: no" in result.output
        assert "No Training Plan offerings match" in result.output

    def test_unpriced_offering_prints_placeholders(self) -> None:
        offering = dict(_search_result()["offerings"][0])
        offering.update(
            upfront_fee_usd=None,
            price_per_instance_hour=None,
            availability_zones=[],
            start_time=None,
            end_time=None,
        )
        with patch(
            "cli.capacity.training_plans.search_offerings",
            return_value=_search_result(offerings=[offering], best=offering),
        ):
            result = CliRunner().invoke(
                cli, ["capacity", "find-training-plans", "-i", "p5", "-r", "us-east-1"]
            )
        assert result.exit_code == 0, result.output
        assert "us-east-1     -" in result.output
        assert "          ?          -" in result.output

    def test_both_durations_are_refused(self) -> None:
        result = CliRunner().invoke(
            cli,
            [
                "capacity",
                "find-training-plans",
                "-i",
                "p5",
                "--duration-days",
                "7",
                "--duration-hours",
                "24",
            ],
        )
        assert result.exit_code == 1
        assert "not both" in result.output

    def test_a_lookup_failure_exits_1(self) -> None:
        with patch(
            "cli.capacity.training_plans.search_offerings",
            side_effect=tp.TrainingPlanError(
                "SageMaker refused the Training Plan offering search in every Region"
            ),
        ):
            result = CliRunner().invoke(
                cli, ["capacity", "find-training-plans", "-i", "p5", "-r", "us-east-1"]
            )
        assert result.exit_code == 1
        assert "Failed to find Training Plan offerings: SageMaker refused" in result.output


class TestTrainingPlansCommand:
    def test_table(self) -> None:
        with patch(
            "cli.capacity.training_plans.list_plans", return_value=_list_result()
        ) as listing:
            result = CliRunner().invoke(
                cli,
                ["capacity", "training-plans", "-r", "us-east-1", "--status", "Active", "-i", "p5"],
            )
        assert result.exit_code == 0, result.output
        assert listing.call_args.kwargs == {
            "regions": ["us-east-1"],
            "instance_type": "p5",
            "status": "Active",
        }
        assert "Training Plans (1 found)" in result.output
        assert (
            "p5-plan                  us-east-1     Active     ml.p5.48xlarge             4     3 2026-11-10T00:00  2026-11-17T00:00"
            in result.output
        )
        assert _PLAN_ARN in result.output
        assert "Total: 4 reserved, 3 available" in result.output

    def test_empty_with_errors_and_json(self) -> None:
        empty = _list_result(
            plans=[],
            plans_found=0,
            total_instances=0,
            available_instances=0,
            errors=[{"region": "us-east-1", "error_code": "AccessDeniedException", "error": "no"}],
        )
        aws_client = MagicMock()
        aws_client.discover_regional_stacks.return_value = {"us-east-1": object()}
        with (
            patch("cli.capacity.training_plans.list_plans", return_value=empty) as listing,
            patch("cli.aws_client.get_aws_client", return_value=aws_client),
        ):
            table = CliRunner().invoke(cli, ["capacity", "training-plans"])
            as_json = CliRunner().invoke(cli, ["--output", "json", "capacity", "training-plans"])
        assert table.exit_code == 0, table.output
        assert listing.call_args.kwargs["regions"] == ["us-east-1"]
        assert "No Training Plans held" in table.output
        assert "us-east-1: AccessDeniedException: no" in table.output
        assert as_json.exit_code == 0, as_json.output
        assert json.loads(as_json.output)["plans_found"] == 0

    def test_a_lookup_failure_exits_1(self) -> None:
        with patch(
            "cli.capacity.training_plans.list_plans",
            side_effect=tp.TrainingPlanError("every Region"),
        ):
            result = CliRunner().invoke(cli, ["capacity", "training-plans", "-r", "us-east-1"])
        assert result.exit_code == 1
        assert "Failed to list Training Plans: every Region" in result.output
