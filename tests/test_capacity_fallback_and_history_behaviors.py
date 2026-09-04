"""Focused regression tests for capacity fallback, history, and DryRun boundaries."""

from __future__ import annotations

import builtins
import sys
from datetime import UTC
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from cli.capacity import blocks
from cli.capacity import checker as checker_module
from cli.capacity import history as history_module
from cli.capacity.advisor import BedrockCapacityAdvisor
from cli.capacity.checker import CapacityChecker, SpotPlacementConfigLimitError
from cli.capacity.models import InstanceTypeInfo, _mib_to_gib
from cli.capacity.multi_region import MultiRegionCapacityChecker, RegionCapacity
from cli.capacity.traffic_dial import TrafficDialManager, get_traffic_dial_manager
from gco.bedrock import BedrockResponseTruncatedError


def _client_error(code: str, operation: str = "DryRun") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"{code} message"}}, operation)


def _checker() -> CapacityChecker:
    config = SimpleNamespace(default_region="us-east-1")
    with patch("cli.capacity.checker.boto3.Session", return_value=MagicMock()):
        return CapacityChecker(config)  # type: ignore[arg-type]


def _advisor() -> BedrockCapacityAdvisor:
    advisor = object.__new__(BedrockCapacityAdvisor)
    advisor.config = SimpleNamespace(default_region="us-east-1")
    advisor.model_id = "test.model"
    advisor._uses_default_model = False
    advisor._capacity_checker = MagicMock()
    advisor._multi_region_checker = MagicMock()
    advisor._session = MagicMock()
    return advisor


def _multi_region_checker() -> MultiRegionCapacityChecker:
    checker = object.__new__(MultiRegionCapacityChecker)
    checker.config = SimpleNamespace(default_region="us-east-1")
    checker._session = MagicMock()
    checker._last_region_errors = []
    return checker


def test_advisor_records_each_isolated_market_lookup_failure() -> None:
    advisor = _advisor()
    capacity = advisor._capacity_checker
    capacity.get_spot_placement_score.return_value = {}
    capacity.get_spot_price_history.side_effect = RuntimeError("spot history unavailable")
    capacity.get_on_demand_price.side_effect = RuntimeError("pricing unavailable")
    capacity.check_instance_available_in_region.side_effect = _client_error(
        "RequestLimitExceeded", "DescribeInstanceTypeOfferings"
    )
    capacity.list_capacity_reservations.return_value = []
    capacity.list_capacity_block_offerings.return_value = []
    capacity.get_capacity_block_trend.return_value = 0.0
    advisor._multi_region_checker.get_region_capacity.side_effect = RuntimeError("no cluster")
    advisor._multi_region_checker.recommend_region_for_job.return_value = {}
    advisor._session.client.return_value.describe_spot_price_history.return_value = {
        "SpotPriceHistory": []
    }

    data = advisor.gather_capacity_data(["g5.xlarge"], ["us-east-1"])

    assert data["spot_data"]["g5.xlarge"]["us-east-1"]["prices"] == []
    assert data["on_demand_data"]["g5.xlarge"]["us-east-1"] == {
        "price_per_hour": None,
        "available": None,
    }
    assert {(gap["source"], gap["error"]) for gap in data["data_gaps"]} == {
        ("spot price history", "RuntimeError"),
        ("on-demand price", "RuntimeError"),
        ("region availability", "RequestLimitExceeded"),
    }


def test_advisor_preserves_bedrock_truncation_remediation() -> None:
    advisor = _advisor()
    advisor.gather_capacity_data = MagicMock(return_value={})
    advisor._gather_historical_context = MagicMock(return_value=None)
    advisor._build_prompt = MagicMock(return_value="prompt")
    bedrock = MagicMock()
    bedrock.converse.return_value = {"stopReason": "max_tokens"}
    advisor._get_bedrock_client = MagicMock(return_value=bedrock)

    with pytest.raises(BedrockResponseTruncatedError, match="output"):
        advisor.get_recommendation()


def test_prediction_keeps_raw_response_when_embedded_json_is_malformed() -> None:
    advisor = _advisor()
    store = MagicMock()
    store.get_statistics.return_value = {"sample_count": 1, "metrics": {}}
    store.get_temporal_patterns.return_value = {"patterns": {}, "best_windows": []}
    bedrock = MagicMock()
    raw = "prefix {not-valid-json} suffix"
    bedrock.converse.return_value = {
        "output": {"message": {"content": [{"text": raw}]}},
        "stopReason": "end_turn",
    }
    advisor._get_bedrock_client = MagicMock(return_value=bedrock)

    with patch("cli.capacity.history.get_capacity_history_store", return_value=store):
        result = advisor.predict_capacity_window("g5.xlarge", "us-east-1")

    assert result.best_windows == []
    assert result.reasoning == ""
    assert result.confidence == "low"
    assert result.raw_response == raw


def test_capacity_pricing_rejects_bad_rates_and_handles_absent_counts() -> None:
    assert blocks.compute_reservation_pricing("not-a-rate", 2, 8) == {
        "price_per_instance_hour": None,
        "price_per_hour": None,
        "price_per_gpu_hour": None,
    }
    assert blocks.compute_reservation_pricing(-1.0, 2, 8) == {
        "price_per_instance_hour": None,
        "price_per_hour": None,
        "price_per_gpu_hour": None,
    }
    reservation = blocks.compute_reservation_pricing(8.0, None, 8)
    assert reservation == {
        "price_per_instance_hour": 8.0,
        "price_per_hour": None,
        "price_per_gpu_hour": 1.0,
    }
    offering = blocks.compute_offering_pricing("240", 24, None, 8)
    assert offering["price_per_hour"] == 10.0
    assert offering["price_per_instance_hour"] is None
    assert offering["price_per_gpu_hour"] is None


def _without_catalog_import(real_import):
    def import_module(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "scripts.accelerator_catalog":
            raise ImportError("catalog package unavailable")
        return real_import(name, globals, locals, fromlist, level)

    return import_module


def test_pool_catalog_fallback_handles_an_unusable_import_spec() -> None:
    path = MagicMock()
    path.is_file.return_value = True
    checker_module._pool_catalog_lookup.cache_clear()
    try:
        with (
            patch.object(
                builtins, "__import__", side_effect=_without_catalog_import(builtins.__import__)
            ),
            patch.object(checker_module, "_POOL_CATALOG_PATH", path),
            patch("cli.capacity.checker.importlib.util.spec_from_file_location", return_value=None),
        ):
            assert checker_module._pool_catalog_lookup() is None
    finally:
        checker_module._pool_catalog_lookup.cache_clear()


def test_pool_catalog_fallback_removes_a_partially_executed_module() -> None:
    path = MagicMock()
    path.is_file.return_value = True
    spec = MagicMock()
    spec.loader.exec_module.side_effect = RuntimeError("bad catalog")
    module = MagicMock()
    checker_module._pool_catalog_lookup.cache_clear()
    sys.modules.pop("_gco_pool_catalog", None)
    try:
        with (
            patch.object(
                builtins, "__import__", side_effect=_without_catalog_import(builtins.__import__)
            ),
            patch.object(checker_module, "_POOL_CATALOG_PATH", path),
            patch("cli.capacity.checker.importlib.util.spec_from_file_location", return_value=spec),
            patch("cli.capacity.checker.importlib.util.module_from_spec", return_value=module),
        ):
            assert checker_module._pool_catalog_lookup() is None
        assert "_gco_pool_catalog" not in sys.modules
    finally:
        checker_module._pool_catalog_lookup.cache_clear()
        sys.modules.pop("_gco_pool_catalog", None)


def test_on_demand_only_estimate_degrades_when_spot_configuration_is_refused() -> None:
    checker = _checker()
    with (
        patch.object(checker, "check_instance_available_in_region", return_value=True),
        patch.object(checker, "get_instance_info", return_value=None),
        patch.object(
            checker,
            "get_spot_placement_score",
            side_effect=SpotPlacementConfigLimitError("configuration limit"),
        ),
        patch.object(checker, "get_spot_price_history", return_value=[]),
        patch.object(checker, "_estimate_on_demand_capacity", return_value=None) as estimate,
    ):
        assert checker.estimate_capacity("g5.xlarge", "us-east-1", "on-demand") == []

    assert estimate.call_args.args[3] == {}


def test_on_demand_estimate_records_the_average_positive_spot_score() -> None:
    checker = _checker()
    info = InstanceTypeInfo("g5.xlarge", 4, 16, 1, "A10G", 24)
    with (
        patch.object(checker, "get_on_demand_price", return_value=1.0),
        patch.object(checker, "check_instance_available_in_region", return_value=True),
        patch.object(checker, "get_az_coverage", return_value=1.0),
    ):
        estimate = checker._estimate_on_demand_capacity(
            "g5.xlarge",
            "us-east-1",
            info,
            spot_placement_scores={"a": 8, "b": 0, "c": 6},
            spot_prices=[],
        )

    assert estimate is not None
    assert estimate.details["avg_spot_placement_score"] == 7.0


def test_instance_info_cache_and_missing_pricing_fields_are_defensive() -> None:
    checker = _checker()
    info = InstanceTypeInfo("p5.48xlarge", 192, 2048, 8, "H100", 640)
    with patch.object(checker, "get_instance_info", return_value=info) as get_info:
        assert checker._gpus_per_instance("p5.48xlarge", "us-east-1") == 8
        assert checker._gpus_per_instance("p5.48xlarge", "us-east-1") == 8
    get_info.assert_called_once()

    reservation = {"total_instances": 1}
    checker._enrich_reservation_pricing(reservation, "us-east-1")
    assert reservation == {"total_instances": 1}

    offering = checker._build_block_offering(
        {"UpfrontFee": "24", "CapacityBlockDurationHours": 24},
        "us-east-1",
        requested_count=3,
        gpus_per_instance=8,
        requested_duration_hours=24,
    )
    assert offering["instance_count"] == 3


def test_capacity_block_trend_handles_transport_failures() -> None:
    checker = _checker()
    ec2 = MagicMock()
    ec2.describe_capacity_block_offerings.side_effect = EndpointConnectionError(
        endpoint_url="https://ec2.us-east-1.amazonaws.com"
    )
    checker._session.client.return_value = ec2

    assert checker.get_capacity_block_trend("p5.48xlarge", "us-east-1") == 0.0


def test_reservation_search_discovers_regions_and_isolates_a_failed_probe() -> None:
    checker = _checker()
    aws_client = MagicMock()
    aws_client.discover_regional_stacks.return_value = {
        "us-east-1": MagicMock(),
        "us-west-2": MagicMock(),
    }

    def list_reservations(region, **_kwargs):
        if region == "us-west-2":
            raise RuntimeError("regional API unavailable")
        return []

    with (
        patch("cli.aws_client.get_aws_client", return_value=aws_client),
        patch.object(checker, "list_capacity_reservations", side_effect=list_reservations),
    ):
        report = checker.find_capacity_reservations()

    assert report["regions_checked"] == ["us-east-1", "us-west-2"]
    assert report["reservations"] == []
    assert "No active" in report["recommendation"]


def test_search_summaries_cover_insufficient_capacity_and_longest_only() -> None:
    reservation = {
        "region": "us-east-1",
        "availability_zone": "us-east-1a",
        "available_instances": 0,
        "total_instances": 2,
    }
    reservation_summary = CapacityChecker._summarize_reservation_search(
        "p5.48xlarge", ["us-east-1"], [reservation], None, 2, None
    )
    assert "Fewer than the 2 requested" in reservation_summary
    assert "Most available" not in reservation_summary

    longest = {
        "region": "us-west-2",
        "availability_zone": "us-west-2a",
        "duration_days": 14,
    }
    block_summary = CapacityChecker._summarize_block_search(
        "p5.48xlarge", ["us-west-2"], [longest], None, longest, None
    )
    assert "Longest: 14d" in block_summary
    assert "Cheapest:" not in block_summary


def test_capacity_block_dry_run_normal_return_never_purchases() -> None:
    checker = _checker()
    ec2 = MagicMock()
    ec2.purchase_capacity_block.return_value = {}
    checker._session.client.return_value = ec2

    result = checker.purchase_capacity_block("cbo-1", "us-east-1", dry_run=True)

    assert result["success"] is False
    assert result["error_code"] == "DryRunProtocolError"
    ec2.purchase_capacity_block.assert_called_once_with(
        CapacityBlockOfferingId="cbo-1", InstancePlatform="Linux/UNIX", DryRun=True
    )


def test_create_reservation_dry_run_handles_rejection_and_normal_return() -> None:
    checker = _checker()
    checker.validate_instance_type = MagicMock(
        return_value={"instance_type": "p5.48xlarge", "valid": True, "note": None}
    )
    ec2 = MagicMock()
    checker._session.client.return_value = ec2

    ec2.create_capacity_reservation.side_effect = _client_error("UnauthorizedOperation")
    rejected = checker.create_capacity_reservation(
        "p5.48xlarge", "us-east-1", "us-east-1a", dry_run=True
    )
    assert rejected["success"] is False
    assert rejected["error_code"] == "UnauthorizedOperation"

    ec2.reset_mock()
    ec2.create_capacity_reservation.side_effect = None
    ec2.create_capacity_reservation.return_value = {}
    anomaly = checker.create_capacity_reservation(
        "p5.48xlarge", "us-east-1", "us-east-1a", dry_run=True
    )
    assert anomaly["success"] is False
    assert anomaly["error_code"] == "DryRunProtocolError"
    assert ec2.create_capacity_reservation.call_count == 1
    assert ec2.create_capacity_reservation.call_args.kwargs["DryRun"] is True


def test_cancel_reservation_dry_run_handles_rejection_and_normal_return() -> None:
    checker = _checker()
    ec2 = MagicMock()
    checker._session.client.return_value = ec2

    ec2.cancel_capacity_reservation.side_effect = _client_error("AccessDenied")
    rejected = checker.cancel_capacity_reservation("cr-1", "us-east-1", dry_run=True)
    assert rejected["success"] is False
    assert rejected["error_code"] == "AccessDenied"

    ec2.reset_mock()
    ec2.cancel_capacity_reservation.side_effect = None
    ec2.cancel_capacity_reservation.return_value = {}
    anomaly = checker.cancel_capacity_reservation("cr-1", "us-east-1", dry_run=True)
    assert anomaly["success"] is False
    assert anomaly["error_code"] == "DryRunProtocolError"
    assert ec2.cancel_capacity_reservation.call_count == 1
    assert ec2.cancel_capacity_reservation.call_args.kwargs["DryRun"] is True


def test_history_helpers_cover_invalid_naive_recursive_and_empty_values() -> None:
    assert history_module._parse_iso(123) is None  # type: ignore[arg-type]
    parsed = history_module._parse_iso("2026-01-02T03:04:05")
    assert parsed is not None and parsed.tzinfo is UTC
    assert history_module._to_dynamo([1.5, {"nested": [2.5]}]) == [
        Decimal("1.5"),
        {"nested": [Decimal("2.5")]},
    ]
    assert history_module._from_dynamo([Decimal("1"), {"nested": [Decimal("2.5")]}]) == [
        1,
        {"nested": [2.5]},
    ]
    with pytest.raises(ValueError, match="empty"):
        history_module._percentile([], 50)


def test_flatten_capacity_data_drops_a_pair_with_no_usable_signal() -> None:
    records = history_module.flatten_capacity_data(
        {
            "cluster_metrics": [
                {},
                {"region": "us-east-1", "queue_depth": None},
                {"region": None, "queue_depth": 3},
            ],
            "spot_data": {
                "g5.xlarge": {"us-east-1": {"placement_scores": {}, "prices": [{"current": None}]}}
            },
            "capacity_blocks": {},
        }
    )
    assert records == []


@pytest.mark.parametrize("configured_project", ["", None])
def test_default_history_table_name_falls_back_for_a_falsey_project(configured_project) -> None:
    with patch("cli.config.get_config") as get_config:
        get_config.return_value.project_name = configured_project
        assert history_module._resolve_default_table_name() == history_module.DEFAULT_TABLE_NAME


def test_default_history_table_name_falls_back_when_config_loading_fails() -> None:
    with patch("cli.config.get_config", side_effect=RuntimeError("bad config")):
        assert history_module._resolve_default_table_name() == history_module.DEFAULT_TABLE_NAME


def test_temporal_patterns_skip_missing_and_unparseable_rows() -> None:
    store = object.__new__(history_module.CapacityHistoryStore)
    store.get_trend = MagicMock(
        return_value=[
            {},
            {"timestamp": "2026-01-01T00:00:00+00:00"},
            {"spot_score": 8},
            {"spot_score": 7, "timestamp": "not-a-time"},
        ]
    )

    result = store.get_temporal_patterns("g5.xlarge", "us-east-1")

    assert result["patterns"] == {}
    assert result["best_windows"] == []


def test_regions_with_data_ignores_falsey_region_values() -> None:
    store = object.__new__(history_module.CapacityHistoryStore)
    store._table = MagicMock()
    store._table.query.return_value = {"Items": [{}, {"region": ""}, {"region": "us-east-1"}]}

    assert store.get_regions_with_data("g5.xlarge") == ["us-east-1"]


def test_mib_to_gib_returns_none_for_malformed_ec2_values() -> None:
    assert _mib_to_gib("not-a-number") is None


def test_region_capacity_retains_queue_data_when_cloudwatch_client_creation_fails() -> None:
    checker = _multi_region_checker()
    stack = SimpleNamespace(stack_name="gco-us-east-1", cluster_name="gco-us-east-1")
    cloudformation = MagicMock()
    cloudformation.describe_stacks.return_value = {
        "Stacks": [{"Outputs": [{"OutputKey": "JobQueueUrl", "OutputValue": "https://sqs/q"}]}]
    }
    sqs = MagicMock()
    sqs.get_queue_attributes.return_value = {
        "Attributes": {
            "ApproximateNumberOfMessages": "4",
            "ApproximateNumberOfMessagesNotVisible": "2",
        }
    }

    def client(service, **_kwargs):
        if service == "cloudformation":
            return cloudformation
        if service == "sqs":
            return sqs
        if service == "cloudwatch":
            raise RuntimeError("client construction failed")
        raise AssertionError(service)

    checker._session.client.side_effect = client
    aws_client = MagicMock()
    aws_client.get_regional_stack.return_value = stack
    with patch("cli.aws_client.get_aws_client", return_value=aws_client):
        capacity = checker.get_region_capacity("us-east-1")

    assert capacity.queue_depth == 4
    assert capacity.running_jobs == 2
    assert capacity.telemetry_status == "partial"
    assert capacity.unavailable_signals == ["gpu", "cpu"]
    assert any("client construction failed" in error for error in capacity.telemetry_errors)


def test_all_unavailable_recommendation_prefers_the_configured_default() -> None:
    checker = _multi_region_checker()
    checker.get_all_regions_capacity = MagicMock(
        return_value=[
            RegionCapacity(
                region="us-west-2",
                recommendation_score=1.0,
                telemetry_status="unavailable",
                unavailable_signals=["queue", "gpu", "cpu"],
            ),
            RegionCapacity(
                region="us-east-1",
                recommendation_score=99.0,
                telemetry_status="unavailable",
                unavailable_signals=["queue", "gpu", "cpu"],
            ),
        ]
    )

    assert checker.recommend_region_for_job()["region"] == "us-east-1"


@pytest.mark.parametrize(
    ("status", "unavailable", "expected_reason"),
    [
        ("unavailable", ["queue", "gpu", "cpu"], "capacity telemetry unavailable"),
        ("partial", ["queue", "gpu"], "capacity telemetry is partial"),
    ],
)
def test_simple_recommend_uses_provenance_and_skips_unavailable_signals(
    status, unavailable, expected_reason
) -> None:
    checker = _multi_region_checker()
    capacity = RegionCapacity(
        region="us-east-1",
        queue_depth=0,
        running_jobs=0,
        gpu_utilization=0.0,
        telemetry_status=status,
        unavailable_signals=unavailable,
        telemetry_errors=["telemetry failed"],
    )

    reason = checker._simple_recommend([capacity])["reason"]

    assert expected_reason in reason
    assert "empty queue" not in reason
    assert "GPU available" not in reason
    assert "no running jobs" not in reason


@pytest.mark.parametrize(
    ("status", "unavailable", "expected_reason"),
    [
        ("unavailable", ["queue", "gpu", "cpu"], "cluster telemetry unavailable"),
        ("partial", ["queue", "gpu"], "cluster telemetry is partial"),
    ],
)
def test_weighted_recommend_uses_provenance_and_skips_unavailable_signals(
    status, unavailable, expected_reason
) -> None:
    checker = _multi_region_checker()
    capacity = RegionCapacity(
        region="us-east-1",
        queue_depth=0,
        running_jobs=0,
        gpu_utilization=0.0,
        telemetry_status=status,
        unavailable_signals=unavailable,
        telemetry_errors=["telemetry failed"],
    )
    with patch("cli.capacity.multi_region.CapacityChecker") as checker_class:
        market = checker_class.return_value
        market.get_spot_placement_score.return_value = {}
        market.get_spot_price_history.return_value = []
        market.get_on_demand_price.return_value = None
        market.get_capacity_block_trend.return_value = 0.0
        reason = checker._weighted_recommend([capacity], "p5.48xlarge")["reason"]

    assert expected_reason in reason
    assert "empty queue" not in reason
    assert "GPU available" not in reason
    assert "no running jobs" not in reason


def test_override_reader_paginates_and_ignores_non_override_parameters() -> None:
    config = SimpleNamespace(project_name="gco", global_region="us-east-1")
    manager = object.__new__(TrafficDialManager)
    manager.config = config
    ssm = MagicMock()
    ssm.get_parameters_by_path.side_effect = [
        {
            "Parameters": [{"Name": "/gco/traffic-dial/state", "Value": "{}"}],
            "NextToken": "next",
        },
        {"Parameters": [{"Name": "/gco/traffic-dial/override-us-west-2", "Value": "25"}]},
    ]
    manager._session = MagicMock()
    manager._session.client.return_value = ssm

    assert manager.read_overrides() == {"us-west-2": "25"}
    assert ssm.get_parameters_by_path.call_args_list[1].kwargs["NextToken"] == "next"


def test_traffic_dial_factory_returns_a_manager() -> None:
    config = SimpleNamespace(project_name="gco", global_region="us-east-1")
    with patch("cli.capacity.traffic_dial.boto3.Session", return_value=MagicMock()):
        assert isinstance(get_traffic_dial_manager(config), TrafficDialManager)  # type: ignore[arg-type]
