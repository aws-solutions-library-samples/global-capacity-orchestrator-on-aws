# Tests for lambda/capacity-poller/handler.py -- Historical Capacity Surface poller.
# Drives the env parsing, the pooled/batched/multi-capacity SPS collector with its
# bounded completeness retry and throttle visibility, the region-enablement
# pre-check, and lambda_handler (write path, env guard, isolation).

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from tests._lambda_imports import load_lambda_module

# A three-member pool (the documented SPS minimum) containing the default
# watched type used across these tests.
POOL_24GB = {"name": "single-gpu-24gb", "members": ["g5.xlarge", "g5.2xlarge", "g6.xlarge"]}
CAPACITY_1 = {"target_capacity": 1, "metric_field": "spot_score"}
CAPACITY_10 = {"target_capacity": 10, "metric_field": "spot_score_at_10"}


@pytest.fixture
def handler():
    return load_lambda_module("capacity-poller")


class _ConfigLimitError(Exception):
    """Duck-typed botocore ClientError carrying MaxConfigLimitExceeded."""

    response = {"Error": {"Code": "MaxConfigLimitExceeded"}}


def _sps_response(scores_by_region):
    return {
        "SpotPlacementScores": [
            {"Region": region, "Score": score} for region, score in scores_by_region.items()
        ]
    }


def _fake_ec2():
    ec2 = MagicMock()
    ec2.get_spot_placement_scores.return_value = _sps_response({"us-east-1": 7})
    ec2.describe_spot_price_history.return_value = {
        "SpotPriceHistory": [
            {"AvailabilityZone": "a", "SpotPrice": "1.0"},
            {"AvailabilityZone": "b", "SpotPrice": "2.0"},
        ],
    }
    ec2.describe_capacity_block_offerings.return_value = {
        "CapacityBlockOfferings": [{"InstanceCount": 2}, {"InstanceCount": 3}],
    }
    return ec2


def _fake_boto3(mock_table, ec2):
    fake = MagicMock()
    fake.resource.return_value.Table.return_value = mock_table
    fake.client.return_value = ec2
    return fake


def _set_env(monkeypatch):
    monkeypatch.setenv("CAPACITY_HISTORY_TABLE_NAME", "gco-capacity-history")
    monkeypatch.setenv("WATCH_INSTANCE_TYPES", "g5.xlarge")
    monkeypatch.setenv("ENABLED_REGIONS", "us-east-1")
    monkeypatch.setenv("CAPACITY_HISTORY_RETENTION_DAYS", "90")
    monkeypatch.setenv("SPOT_SCORE_TARGET_CAPACITIES", json.dumps([CAPACITY_1]))
    monkeypatch.setenv("INSTANCE_POOLS", json.dumps([POOL_24GB]))


class TestTargetCapacityParsing:
    def test_unset_defaults_to_capacity_one_spot_score(self, handler):
        assert handler._parse_target_capacities(None) == ((1, "spot_score"),)
        assert handler._parse_target_capacities("") == ((1, "spot_score"),)

    def test_valid_mapping_preserves_order(self, handler):
        raw = json.dumps(
            [
                CAPACITY_1,
                CAPACITY_10,
                {"target_capacity": 50, "metric_field": "spot_score_at_50"},
            ]
        )
        assert handler._parse_target_capacities(raw) == (
            (1, "spot_score"),
            (10, "spot_score_at_10"),
            (50, "spot_score_at_50"),
        )

    @pytest.mark.parametrize(
        "raw",
        [
            "not json",
            "{}",
            "[]",
            '["1"]',
            '[{"metric_field": "spot_score"}]',
            '[{"target_capacity": 0, "metric_field": "spot_score"}]',
            '[{"target_capacity": -1, "metric_field": "spot_score"}]',
            '[{"target_capacity": true, "metric_field": "spot_score"}]',
            '[{"target_capacity": 1, "metric_field": ""}]',
            '[{"target_capacity": 1}]',
        ],
    )
    def test_malformed_mapping_raises(self, handler, raw):
        with pytest.raises(ValueError):
            handler._parse_target_capacities(raw)


class TestInstancePoolParsing:
    def test_unset_means_no_pools(self, handler):
        assert handler._parse_instance_pools(None) == ()
        assert handler._parse_instance_pools("") == ()

    def test_valid_pools_preserve_priority_order(self, handler):
        raw = json.dumps(
            [
                {"name": "first", "members": ["a.1x", "b.1x", "c.1x"]},
                {"name": "second", "members": ["a.1x", "d.1x", "e.1x"]},
            ]
        )
        assert handler._parse_instance_pools(raw) == (
            ("first", ("a.1x", "b.1x", "c.1x")),
            ("second", ("a.1x", "d.1x", "e.1x")),
        )

    @pytest.mark.parametrize(
        "raw",
        [
            "not json",
            "{}",
            '[{"members": ["a.1x", "b.1x", "c.1x"]}]',
            '[{"name": "", "members": ["a.1x", "b.1x", "c.1x"]}]',
            '[{"name": "p"}]',
            '[{"name": "p", "members": ["a.1x", "b.1x"]}]',
            '[{"name": "p", "members": ["a.1x", "a.1x", "b.1x"]}]',
            '[{"name": "p", "members": ["a.1x", "b.1x", 3]}]',
        ],
    )
    def test_malformed_pools_raise(self, handler, raw):
        # A two-member pool (including one padded with a duplicate) would
        # silently reintroduce the depressed-score bug, so parsing fails loud.
        with pytest.raises(ValueError):
            handler._parse_instance_pools(raw)

    def test_duplicate_pool_name_raises(self, handler):
        raw = json.dumps(
            [
                {"name": "p", "members": ["a.1x", "b.1x", "c.1x"]},
                {"name": "p", "members": ["d.1x", "e.1x", "f.1x"]},
            ]
        )
        with pytest.raises(ValueError, match="more than once"):
            handler._parse_instance_pools(raw)


class TestPoolForInstanceType:
    def test_first_pool_in_order_wins(self, handler):
        pools = (
            ("first", ("a.1x", "b.1x", "c.1x")),
            ("second", ("a.1x", "d.1x", "e.1x")),
        )
        assert handler._pool_for_instance_type(pools, "a.1x")[0] == "first"
        assert handler._pool_for_instance_type(pools, "d.1x")[0] == "second"

    def test_unpooled_type_returns_none(self, handler):
        pools = (("first", ("a.1x", "b.1x", "c.1x")),)
        assert handler._pool_for_instance_type(pools, "z.9x") is None


class TestSpotScoreCollection:
    POOLS = (("pool-a", ("a.1x", "b.1x", "c.1x")),)
    CAPACITIES = ((1, "spot_score"),)

    def test_request_carries_full_pool_and_capacity(self, handler):
        ec2 = MagicMock()
        ec2.get_spot_placement_scores.return_value = _sps_response({"us-east-1": 8})

        scores, counters = handler._collect_spot_placement_scores(
            ec2, self.POOLS, self.CAPACITIES, ["us-east-1"]
        )

        kwargs = ec2.get_spot_placement_scores.call_args.kwargs
        assert kwargs["InstanceTypes"] == ["a.1x", "b.1x", "c.1x"]
        assert len(kwargs["InstanceTypes"]) >= 3
        assert kwargs["TargetCapacity"] == 1
        assert kwargs["TargetCapacityUnitType"] == "units"
        assert kwargs["RegionNames"] == ["us-east-1"]
        assert scores[("pool-a", "us-east-1", 1)] == 8
        assert counters["requests_issued"] == 1
        assert counters["combinations_expected"] == 1
        assert counters["combinations_received"] == 1
        assert counters["combinations_missing_after_retry"] == 0

    def test_regions_are_batched_not_requested_individually(self, handler):
        regions = [f"region-{i}" for i in range(12)]
        ec2 = MagicMock()
        ec2.get_spot_placement_scores.side_effect = lambda **kwargs: _sps_response(
            dict.fromkeys(kwargs["RegionNames"], 5)
        )

        scores, counters = handler._collect_spot_placement_scores(
            ec2, self.POOLS, self.CAPACITIES, regions
        )

        # 12 regions fit in two batches at the top-10 response bound; a
        # request per region (12 calls) would waste quota, and a single
        # 12-region request could silently drop two regions.
        assert counters["requests_issued"] == 2
        sizes = [len(c.kwargs["RegionNames"]) for c in ec2.get_spot_placement_scores.call_args_list]
        assert sizes == [10, 2]
        assert all(size <= handler.SPS_REGION_BATCH_SIZE for size in sizes)
        # Every requested region appears in the merged result.
        assert {region for (_pool, region, _cap) in scores} == set(regions)
        assert counters["combinations_missing_after_retry"] == 0

    def test_multi_capacity_issues_one_request_per_pool_capacity(self, handler):
        capacities = ((1, "spot_score"), (10, "spot_score_at_10"))
        ec2 = MagicMock()
        ec2.get_spot_placement_scores.side_effect = lambda **kwargs: _sps_response(
            dict.fromkeys(kwargs["RegionNames"], 9 if kwargs["TargetCapacity"] == 1 else 4)
        )

        scores, counters = handler._collect_spot_placement_scores(
            ec2, self.POOLS, capacities, ["us-east-1"]
        )

        assert counters["requests_issued"] == 2
        assert scores[("pool-a", "us-east-1", 1)] == 9
        assert scores[("pool-a", "us-east-1", 10)] == 4

    def test_missing_regions_are_retried_and_recovered(self, handler):
        # First response drops us-west-2; the retry requests only the gap.
        ec2 = MagicMock()
        ec2.get_spot_placement_scores.side_effect = [
            _sps_response({"us-east-1": 8}),
            _sps_response({"us-west-2": 6}),
        ]

        scores, counters = handler._collect_spot_placement_scores(
            ec2, self.POOLS, self.CAPACITIES, ["us-east-1", "us-west-2"]
        )

        assert counters["requests_issued"] == 2
        retry_kwargs = ec2.get_spot_placement_scores.call_args_list[1].kwargs
        assert retry_kwargs["RegionNames"] == ["us-west-2"]
        assert scores[("pool-a", "us-east-1", 1)] == 8
        assert scores[("pool-a", "us-west-2", 1)] == 6
        assert counters["combinations_missing_after_retry"] == 0

    def test_persistent_gap_is_bounded_and_reported(self, handler):
        ec2 = MagicMock()
        ec2.get_spot_placement_scores.return_value = _sps_response({})

        scores, counters = handler._collect_spot_placement_scores(
            ec2, self.POOLS, self.CAPACITIES, ["us-east-1"]
        )

        # One request per pass, never more than SPS_MAX_ATTEMPTS passes: a
        # persistent gap cannot run the Lambda toward its timeout.
        assert counters["requests_issued"] == handler.SPS_MAX_ATTEMPTS
        assert scores == {}
        assert counters["combinations_expected"] == 1
        assert counters["combinations_received"] == 0
        assert counters["combinations_missing_after_retry"] == 1

    def test_config_limit_refusal_is_counted_and_leaves_gaps(self, handler):
        ec2 = MagicMock()
        ec2.get_spot_placement_scores.side_effect = _ConfigLimitError("refused")

        scores, counters = handler._collect_spot_placement_scores(
            ec2, self.POOLS, self.CAPACITIES, ["us-east-1"]
        )

        assert scores == {}
        # Each refused request is counted, one per bounded pass.
        assert counters["config_limit_refusals"] == handler.SPS_MAX_ATTEMPTS
        assert counters["combinations_missing_after_retry"] == 1

    def test_config_limit_refusal_is_logged_distinctly(self, handler, caplog):
        ec2 = MagicMock()
        ec2.get_spot_placement_scores.side_effect = _ConfigLimitError("refused")

        with caplog.at_level("WARNING"):
            handler._collect_spot_placement_scores(ec2, self.POOLS, self.CAPACITIES, ["us-east-1"])

        assert any("MaxConfigLimitExceeded" in record.message for record in caplog.records)

    def test_refusal_on_one_pool_does_not_block_others(self, handler):
        pools = (
            ("pool-a", ("a.1x", "b.1x", "c.1x")),
            ("pool-b", ("d.1x", "e.1x", "f.1x")),
        )

        def respond(**kwargs):
            if kwargs["InstanceTypes"] == ["a.1x", "b.1x", "c.1x"]:
                raise _ConfigLimitError("refused")
            return _sps_response(dict.fromkeys(kwargs["RegionNames"], 7))

        ec2 = MagicMock()
        ec2.get_spot_placement_scores.side_effect = respond

        scores, counters = handler._collect_spot_placement_scores(
            ec2, pools, self.CAPACITIES, ["us-east-1"]
        )

        assert ("pool-a", "us-east-1", 1) not in scores
        assert scores[("pool-b", "us-east-1", 1)] == 7
        assert counters["config_limit_refusals"] == handler.SPS_MAX_ATTEMPTS

    def test_pagination_is_followed(self, handler):
        ec2 = MagicMock()
        page_one = _sps_response({"us-east-1": 8})
        page_one["NextToken"] = "token"
        page_two = _sps_response({"us-west-2": 5})
        ec2.get_spot_placement_scores.side_effect = [page_one, page_two]

        scores, counters = handler._collect_spot_placement_scores(
            ec2, self.POOLS, self.CAPACITIES, ["us-east-1", "us-west-2"]
        )

        assert ec2.get_spot_placement_scores.call_args_list[1].kwargs["NextToken"] == "token"
        assert scores[("pool-a", "us-west-2", 1)] == 5
        assert counters["combinations_missing_after_retry"] == 0

    def test_az_level_records_and_foreign_regions_are_ignored(self, handler):
        ec2 = MagicMock()
        ec2.get_spot_placement_scores.return_value = {
            "SpotPlacementScores": [
                {"Region": "us-east-1", "AvailabilityZoneId": "use1-az1", "Score": 9},
                {"Region": "eu-west-1", "Score": 9},
                {"Region": "us-east-1", "Score": 6},
            ]
        }

        scores, _counters = handler._collect_spot_placement_scores(
            ec2, self.POOLS, self.CAPACITIES, ["us-east-1"]
        )

        assert scores == {("pool-a", "us-east-1", 1): 6}

    def test_no_pools_makes_no_requests(self, handler):
        ec2 = MagicMock()

        scores, counters = handler._collect_spot_placement_scores(
            ec2, (), self.CAPACITIES, ["us-east-1"]
        )

        ec2.get_spot_placement_scores.assert_not_called()
        assert scores == {}
        assert counters["combinations_expected"] == 0


class TestSpotPriceSummary:
    def test_mean_and_az_count(self, handler):
        ec2 = _fake_ec2()
        price, az_count = handler._spot_price_summary(ec2, "g5.xlarge")
        assert price == 1.5
        assert az_count == 2


class TestCapacityBlockSummary:
    def test_offering_and_instance_counts(self, handler):
        ec2 = _fake_ec2()
        offerings, total = handler._capacity_block_summary(ec2, "g5.xlarge")
        assert offerings == 2
        assert total == 5

    def test_duration_param_passed_to_api(self, handler):
        ec2 = _fake_ec2()
        handler._capacity_block_summary(ec2, "p5.48xlarge", 1512)
        kwargs = ec2.describe_capacity_block_offerings.call_args.kwargs
        assert kwargs["CapacityDurationHours"] == 1512

    def test_default_duration_is_short(self, handler):
        ec2 = _fake_ec2()
        handler._capacity_block_summary(ec2, "p5.48xlarge")
        kwargs = ec2.describe_capacity_block_offerings.call_args.kwargs
        assert kwargs["CapacityDurationHours"] == handler.DEFAULT_BLOCK_DURATION_HOURS == 24


class TestLambdaHandler:
    def test_writes_one_item(self, handler, monkeypatch):
        _set_env(monkeypatch)
        mock_table = MagicMock()
        ec2 = _fake_ec2()
        with patch.object(handler, "boto3", _fake_boto3(mock_table, ec2)):
            result = handler.lambda_handler({}, None)
        assert result["written"] == 1
        assert result["errors"] == 0
        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert item["pk"] == "g5.xlarge#us-east-1"
        assert item["spot_score"] == 7
        # The score was requested for the whole pool, and the snapshot says so.
        assert item["spot_pool"] == "single-gpu-24gb"
        sps_kwargs = ec2.get_spot_placement_scores.call_args.kwargs
        assert sps_kwargs["InstanceTypes"] == POOL_24GB["members"]
        assert item["spot_price"] == Decimal("1.5")
        assert item["az_count"] == 2
        assert item["capacity_blocks_available"] == 2
        assert item["capacity_blocks_total"] == 5
        # Long-duration tier is on by default (63 days) and probed separately.
        assert item["capacity_blocks_long_available"] == 2
        assert item["capacity_blocks_long_total"] == 5
        assert ec2.describe_capacity_block_offerings.call_count == 2
        durations = {
            c.kwargs["CapacityDurationHours"]
            for c in ec2.describe_capacity_block_offerings.call_args_list
        }
        assert durations == {24, handler.DEFAULT_LONG_BLOCK_DURATION_HOURS}
        assert isinstance(item["ttl"], int)

    def test_multi_capacity_scores_land_in_their_fields(self, handler, monkeypatch):
        _set_env(monkeypatch)
        monkeypatch.setenv("SPOT_SCORE_TARGET_CAPACITIES", json.dumps([CAPACITY_1, CAPACITY_10]))
        mock_table = MagicMock()
        ec2 = _fake_ec2()
        ec2.get_spot_placement_scores.side_effect = lambda **kwargs: _sps_response(
            {"us-east-1": 9 if kwargs["TargetCapacity"] == 1 else 4}
        )
        with patch.object(handler, "boto3", _fake_boto3(mock_table, ec2)):
            result = handler.lambda_handler({}, None)
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert item["spot_score"] == 9
        assert item["spot_score_at_10"] == 4
        assert item["spot_pool"] == "single-gpu-24gb"
        assert result["sps"]["target_capacities"] == [1, 10]
        assert result["sps"]["combinations_expected"] == 2
        assert result["sps"]["combinations_received"] == 2

    def test_unpooled_watch_type_gets_no_score_and_no_sps_request(self, handler, monkeypatch):
        _set_env(monkeypatch)
        monkeypatch.setenv("WATCH_INSTANCE_TYPES", "trn1.2xlarge")
        mock_table = MagicMock()
        ec2 = _fake_ec2()
        with patch.object(handler, "boto3", _fake_boto3(mock_table, ec2)):
            result = handler.lambda_handler({}, None)
        # No pool contains the watched type: a single-type request is a
        # known-invalid measurement, so none is made at all.
        ec2.get_spot_placement_scores.assert_not_called()
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "spot_score" not in item
        assert "spot_pool" not in item
        assert item["spot_price"] == Decimal("1.5")
        assert item["capacity_blocks_available"] == 2
        assert result["sps"]["unpooled_watch_types"] == 1
        assert result["sps"]["combinations_expected"] == 0

    def test_config_limit_refusal_omits_fields_and_is_counted(self, handler, monkeypatch):
        _set_env(monkeypatch)
        mock_table = MagicMock()
        ec2 = _fake_ec2()
        ec2.get_spot_placement_scores.side_effect = _ConfigLimitError("refused")
        with patch.object(handler, "boto3", _fake_boto3(mock_table, ec2)):
            result = handler.lambda_handler({}, None)
        # The snapshot is still written (price and Capacity Blocks are real
        # signals) but carries no score field and no pool attribution.
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "spot_score" not in item
        assert "spot_pool" not in item
        assert item["spot_price"] == Decimal("1.5")
        assert result["sps"]["config_limit_refusals"] == handler.SPS_MAX_ATTEMPTS
        assert result["sps"]["combinations_missing_after_retry"] == 1
        assert result["written"] == 1

    def test_not_enabled_region_is_skipped_counted_and_unwritten(self, handler, monkeypatch):
        _set_env(monkeypatch)
        monkeypatch.setenv("ENABLED_REGIONS", "us-east-1,eu-bad-1")
        mock_table = MagicMock()
        ec2 = _fake_ec2()

        def probe(**kwargs):
            if kwargs.get("RegionNames") == ["eu-bad-1"]:
                raise Exception("AuthFailure: not opted in")
            return {"Regions": [{"RegionName": kwargs["RegionNames"][0]}]}

        ec2.describe_regions.side_effect = probe
        with patch.object(handler, "boto3", _fake_boto3(mock_table, ec2)):
            result = handler.lambda_handler({}, None)

        assert result["regions_polled"] == ["us-east-1"]
        assert result["regions_skipped_not_enabled"] == ["eu-bad-1"]
        # No snapshot was written for the skipped region and SPS never
        # requested it.
        written_pks = [c.kwargs["Item"]["pk"] for c in mock_table.put_item.call_args_list]
        assert written_pks == ["g5.xlarge#us-east-1"]
        for call in ec2.get_spot_placement_scores.call_args_list:
            assert "eu-bad-1" not in call.kwargs["RegionNames"]

    def test_unauthorized_probe_fails_open_and_polls_the_region(self, handler, monkeypatch, caplog):
        # Discovered live: without ec2:DescribeRegions the probe raised
        # UnauthorizedOperation for every region and the poller wrote zero
        # snapshots account-wide. An endpoint that rejects the probe for lack
        # of permission has already accepted the credentials, so the region
        # is enabled; the poller must poll it and name the missing grant.
        _set_env(monkeypatch)
        mock_table = MagicMock()
        ec2 = _fake_ec2()

        class _UnauthorizedError(Exception):
            response = {"Error": {"Code": "UnauthorizedOperation"}}

        ec2.describe_regions.side_effect = _UnauthorizedError("no ec2:DescribeRegions")
        with (
            caplog.at_level("WARNING"),
            patch.object(handler, "boto3", _fake_boto3(mock_table, ec2)),
        ):
            result = handler.lambda_handler({}, None)

        assert result["regions_polled"] == ["us-east-1"]
        assert result["regions_skipped_not_enabled"] == []
        assert result["written"] == 1
        assert any("ec2:DescribeRegions" in record.message for record in caplog.records)

    def test_structured_summary_reports_sps_counters(self, handler, monkeypatch):
        _set_env(monkeypatch)
        mock_table = MagicMock()
        ec2 = _fake_ec2()
        with patch.object(handler, "boto3", _fake_boto3(mock_table, ec2)):
            result = handler.lambda_handler({}, None)
        sps = result["sps"]
        assert sps["requests_issued"] == 1
        assert sps["combinations_expected"] == 1
        assert sps["combinations_received"] == 1
        assert sps["combinations_missing_after_retry"] == 0
        assert sps["config_limit_refusals"] == 0
        assert sps["pools"] == 1
        assert result["regions_skipped_not_enabled"] == []

    def test_long_probe_disabled_omits_long_fields(self, handler, monkeypatch):
        _set_env(monkeypatch)
        monkeypatch.setenv("CAPACITY_BLOCK_LONG_DURATION_HOURS", "0")
        mock_table = MagicMock()
        ec2 = _fake_ec2()
        with patch.object(handler, "boto3", _fake_boto3(mock_table, ec2)):
            handler.lambda_handler({}, None)
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "capacity_blocks_long_available" not in item
        assert "capacity_blocks_long_total" not in item
        # Only the short probe runs when the long probe is disabled.
        assert ec2.describe_capacity_block_offerings.call_count == 1

    def test_long_probe_reused_when_equal_to_short(self, handler, monkeypatch):
        _set_env(monkeypatch)
        monkeypatch.setenv("CAPACITY_BLOCK_DURATION_HOURS", "24")
        monkeypatch.setenv("CAPACITY_BLOCK_LONG_DURATION_HOURS", "24")
        mock_table = MagicMock()
        ec2 = _fake_ec2()
        with patch.object(handler, "boto3", _fake_boto3(mock_table, ec2)):
            handler.lambda_handler({}, None)
        item = mock_table.put_item.call_args.kwargs["Item"]
        # Long fields mirror the short tier without a second API call.
        assert item["capacity_blocks_long_available"] == item["capacity_blocks_available"]
        assert item["capacity_blocks_long_total"] == item["capacity_blocks_total"]
        assert ec2.describe_capacity_block_offerings.call_count == 1

    def test_custom_long_duration_probed_separately(self, handler, monkeypatch):
        _set_env(monkeypatch)
        monkeypatch.setenv("CAPACITY_BLOCK_LONG_DURATION_HOURS", "672")  # 28 days
        mock_table = MagicMock()
        ec2 = _fake_ec2()
        with patch.object(handler, "boto3", _fake_boto3(mock_table, ec2)):
            handler.lambda_handler({}, None)
        durations = {
            c.kwargs["CapacityDurationHours"]
            for c in ec2.describe_capacity_block_offerings.call_args_list
        }
        assert durations == {24, 672}

    def test_missing_table_name_raises(self, handler, monkeypatch):
        monkeypatch.delenv("CAPACITY_HISTORY_TABLE_NAME", raising=False)
        monkeypatch.setenv("WATCH_INSTANCE_TYPES", "g5.xlarge")
        monkeypatch.setenv("ENABLED_REGIONS", "us-east-1")
        with pytest.raises(ValueError):
            handler.lambda_handler({}, None)

    def test_malformed_pool_env_raises(self, handler, monkeypatch):
        _set_env(monkeypatch)
        monkeypatch.setenv("INSTANCE_POOLS", '[{"name": "tiny", "members": ["a.1x", "b.1x"]}]')
        with pytest.raises(ValueError, match="tiny"):
            handler.lambda_handler({}, None)

    def test_put_item_error_is_isolated(self, handler, monkeypatch):
        _set_env(monkeypatch)
        mock_table = MagicMock()
        mock_table.put_item.side_effect = Exception("boom")
        ec2 = _fake_ec2()
        with patch.object(handler, "boto3", _fake_boto3(mock_table, ec2)):
            result = handler.lambda_handler({}, None)
        assert result["errors"] == 1
        assert result["written"] == 0
