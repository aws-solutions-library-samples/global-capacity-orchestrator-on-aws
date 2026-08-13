"""Coverage-focused unit tests for ``cli/capacity/checker.py``.

The capacity checker wraps several EC2 APIs (instance-type metadata, instance-type offerings, spot-price history, on-demand pricing, and the On-Demand Capacity Reservation / Capacity Block surfaces) and folds the results into an availability assessment. The happy paths run under ``test_capacity.py`` and ``test_capacity_reservations.py``; this module fills in the error and edge branches they leave uncovered:

* ``ClientError`` and empty-result handling for each underlying EC2 call (instance info, offerings, availability zones, spot price, on-demand pricing), plus the spot-score re-raise path.
* The availability-zone coverage fraction (including the empty-AZ case) and the price-fallback availability bands.
* The full live-signal scarcity matrix in ``_assess_on_demand_availability`` -- every spot-score, price-ratio, stability, and AZ-coverage band -- and the ``recommend_capacity_type`` tie-breaks.
* Reservation discovery with no filters, capacity-block trend bucketing (skip, error, and empty branches), and the cross-region reservation and multi-region delegation paths.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from cli.capacity import (
    CapacityChecker,
    CapacityCheckError,
    CapacityEstimate,
    InstanceTypeInfo,
    SpotPriceInfo,
)


def _make_checker():
    with patch("cli.capacity.checker.get_config") as mock_config:
        mock_config.return_value = MagicMock(default_region="us-east-1")
        return CapacityChecker()


def _est(cap_type, availability, price=None):
    return CapacityEstimate(
        instance_type="g5.xlarge",
        region="us-east-1",
        availability_zone="us-east-1a",
        capacity_type=cap_type,
        availability=availability,
        confidence=0.8,
        price_per_hour=price,
    )


def test_get_instance_info_gpuinfo_empty_gpus():
    checker = _make_checker()
    mock_ec2 = MagicMock()
    mock_ec2.describe_instance_types.return_value = {
        "InstanceTypes": [
            {
                "VCpuInfo": {"DefaultVCpus": 16},
                "MemoryInfo": {"SizeInMiB": 65536},
                "GpuInfo": {"Gpus": []},
                "ProcessorInfo": {"SupportedArchitectures": ["x86_64"]},
            }
        ]
    }
    checker._session.client = MagicMock(return_value=mock_ec2)
    info = checker.get_instance_info("m5.4xlarge")
    assert info is not None
    assert info.gpu_count == 0
    assert info.vcpus == 16


def test_get_instance_info_client_error_returns_none():
    checker = _make_checker()
    mock_ec2 = MagicMock()
    mock_ec2.describe_instance_types.side_effect = ClientError(
        {"Error": {"Code": "InvalidParameterValue", "Message": "bad"}}, "DescribeInstanceTypes"
    )
    checker._session.client = MagicMock(return_value=mock_ec2)
    assert checker.get_instance_info("x9.unknown") is None


def test_check_instance_available_client_error_raises():
    """An API failure must raise CapacityCheckError, not mask itself as False."""
    checker = _make_checker()
    mock_ec2 = MagicMock()
    mock_ec2.get_paginator.side_effect = ClientError(
        {"Error": {"Code": "UnauthorizedOperation", "Message": "no"}},
        "DescribeInstanceTypeOfferings",
    )
    checker._session.client = MagicMock(return_value=mock_ec2)
    with pytest.raises(CapacityCheckError, match="us-east-1"):
        checker.check_instance_available_in_region("g5.xlarge", "us-east-1")


def test_get_availability_zones_client_error_returns_empty():
    checker = _make_checker()
    mock_ec2 = MagicMock()
    mock_ec2.describe_availability_zones.side_effect = ClientError(
        {"Error": {"Code": "RequestLimitExceeded", "Message": "slow"}}, "DescribeAvailabilityZones"
    )
    checker._session.client = MagicMock(return_value=mock_ec2)
    assert checker.get_availability_zones("us-east-1") == []


def test_get_az_coverage_no_azs_returns_none():
    checker = _make_checker()
    checker._session.client = MagicMock(return_value=MagicMock())
    with patch.object(checker, "get_availability_zones", return_value=[]):
        assert checker.get_az_coverage("g5.xlarge", "us-east-1") is None


def test_get_az_coverage_success_fraction():
    checker = _make_checker()
    mock_ec2 = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [{"InstanceTypeOfferings": [{"Location": "us-east-1a"}]}]
    mock_ec2.get_paginator.return_value = paginator
    checker._session.client = MagicMock(return_value=mock_ec2)
    with patch.object(checker, "get_availability_zones", return_value=["us-east-1a", "us-east-1b"]):
        coverage = checker.get_az_coverage("g5.xlarge", "us-east-1")
    assert coverage == 0.5


def test_get_spot_placement_score_reraises_other_client_error():
    checker = _make_checker()
    mock_ec2 = MagicMock()
    mock_ec2.get_spot_placement_scores.side_effect = ClientError(
        {"Error": {"Code": "Throttling", "Message": "slow down"}}, "GetSpotPlacementScores"
    )
    checker._session.client = MagicMock(return_value=mock_ec2)
    with pytest.raises(ClientError):
        checker.get_spot_placement_score("g5.xlarge", "us-east-1")


def test_get_on_demand_price_empty_price_dimensions_returns_none():
    checker = _make_checker()
    mock_pricing = MagicMock()
    mock_pricing.get_products.return_value = {
        "PriceList": ['{"terms": {"OnDemand": {"t1": {"priceDimensions": {}}}}}']
    }
    checker._session.client = MagicMock(return_value=mock_pricing)
    assert checker.get_on_demand_price("g5.xlarge", "us-east-1") is None


def test_get_on_demand_price_client_error_returns_none():
    checker = _make_checker()
    mock_pricing = MagicMock()
    mock_pricing.get_products.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "no"}}, "GetProducts"
    )
    checker._session.client = MagicMock(return_value=mock_pricing)
    assert checker.get_on_demand_price("g5.xlarge", "us-east-1") is None


def test_get_spot_price_history_zero_prices_zero_stability():
    checker = _make_checker()
    mock_ec2 = MagicMock()
    mock_ec2.describe_spot_price_history.return_value = {
        "SpotPriceHistory": [
            {"AvailabilityZone": "us-east-1a", "SpotPrice": "0.0"},
            {"AvailabilityZone": "us-east-1a", "SpotPrice": "0.0"},
        ]
    }
    checker._session.client = MagicMock(return_value=mock_ec2)
    results = checker.get_spot_price_history("g5.xlarge", "us-east-1")
    assert len(results) == 1
    assert results[0].price_stability == 0


def test_get_spot_price_history_reraises_other_client_error():
    checker = _make_checker()
    mock_ec2 = MagicMock()
    mock_ec2.describe_spot_price_history.side_effect = ClientError(
        {"Error": {"Code": "Throttling", "Message": "rate exceeded"}}, "DescribeSpotPriceHistory"
    )
    checker._session.client = MagicMock(return_value=mock_ec2)
    with pytest.raises(ClientError):
        checker.get_spot_price_history("g5.xlarge", "us-east-1")


def test_estimate_capacity_on_demand_none_skips_append():
    checker = _make_checker()
    with (
        patch.object(checker, "check_instance_available_in_region", return_value=True),
        patch.object(checker, "get_instance_info", return_value=None),
        patch.object(checker, "get_spot_placement_score", return_value={}),
        patch.object(checker, "get_spot_price_history", return_value=[]),
        patch.object(checker, "_estimate_on_demand_capacity", return_value=None),
    ):
        result = checker.estimate_capacity("g5.xlarge", "us-east-1", "on-demand")
    assert result == []


def test_estimate_spot_limited_capacity_score_three():
    checker = _make_checker()
    with (
        patch.object(checker, "get_spot_placement_score", return_value={"regional": 3}),
        patch.object(checker, "get_spot_price_history", return_value=[]),
        patch.object(checker, "get_on_demand_price", return_value=None),
        patch.object(checker, "get_availability_zones", return_value=["us-east-1a"]),
    ):
        estimates = checker._estimate_spot_capacity("g5.xlarge", "us-east-1", None)
    assert estimates[0].availability == "low"
    assert "Limited spot capacity" in estimates[0].recommendation


def test_estimate_spot_price_fallback_branches():
    checker = _make_checker()
    prices = [
        SpotPriceInfo("g5.xlarge", "us-east-1a", 0.5, 0.5, 0.4, 0.6, 0.7),
        SpotPriceInfo("g5.xlarge", "us-east-1b", 0.5, 0.5, 0.4, 0.6, 0.4),
    ]
    with (
        patch.object(checker, "get_spot_placement_score", return_value={}),
        patch.object(checker, "get_spot_price_history", return_value=prices),
        patch.object(checker, "get_on_demand_price", return_value=None),
        patch.object(checker, "get_availability_zones", return_value=[]),
    ):
        estimates = checker._estimate_spot_capacity("g5.xlarge", "us-east-1", None)
    assert len(estimates) == 2
    assert all(e.availability == "low" for e in estimates)


def test_estimate_on_demand_zero_spot_scores_no_avg_detail():
    checker = _make_checker()
    info = InstanceTypeInfo("g5.xlarge", 4, 16, 1, "A10G", 24)
    with (
        patch.object(checker, "get_on_demand_price", return_value=1.0),
        patch.object(checker, "check_instance_available_in_region", return_value=True),
        patch.object(checker, "get_az_coverage", return_value=None),
    ):
        est = checker._estimate_on_demand_capacity(
            "g5.xlarge", "us-east-1", info, spot_placement_scores={"us-east-1a": 0}, spot_prices=[]
        )
    assert est is not None
    assert "avg_spot_placement_score" not in est.details


def test_assess_no_positive_spot_scores_unknown():
    result = CapacityChecker._assess_on_demand_availability(
        "g5.xlarge", None, None, spot_placement_scores={"a": 0}
    )
    assert result[0] == "unknown"


def test_assess_high_scarcity_low_availability():
    prices = [SpotPriceInfo("g5.xlarge", "a", 0.95, 0.95, 0.9, 1.0, 0.5)]
    avail, _conf, rec = CapacityChecker._assess_on_demand_availability(
        "g5.xlarge",
        None,
        1.0,
        spot_placement_scores={"a": 1, "b": 1},
        spot_prices=prices,
        az_coverage=0.2,
    )
    assert avail == "low"
    assert "extremely scarce" in rec


def test_assess_mid_scarcity_branches():
    prices = [SpotPriceInfo("g5.xlarge", "a", 0.8, 0.8, 0.7, 0.9, 0.9)]
    avail, _conf, rec = CapacityChecker._assess_on_demand_availability(
        "g5.xlarge", None, 1.0, spot_placement_scores={"a": 3, "b": 3}, spot_prices=prices
    )
    assert avail == "low"
    assert "limited availability" in rec


def test_assess_constrained_medium_branches():
    prices = [SpotPriceInfo("g5.xlarge", "a", 0.3, 0.3, 0.2, 0.4, 0.7)]
    avail, _conf, _rec = CapacityChecker._assess_on_demand_availability(
        "g5.xlarge", None, 1.0, spot_placement_scores={"a": 5}, spot_prices=prices, az_coverage=0.4
    )
    assert avail == "medium"


def test_assess_zero_priced_spot_no_price_signal():
    prices = [SpotPriceInfo("g5.xlarge", "a", 0.0, 0.0, 0.0, 0.0, 0.9)]
    result = CapacityChecker._assess_on_demand_availability(
        "g5.xlarge", None, 1.0, spot_placement_scores=None, spot_prices=prices
    )
    assert result[0] == "unknown"


def test_recommend_capacity_type_no_available_spots_default():
    checker = _make_checker()
    estimates = [_est("spot", "unknown"), _est("on-demand", "low")]
    with patch.object(checker, "estimate_capacity", return_value=estimates):
        rec, _reason = checker.recommend_capacity_type("g5.xlarge", "us-east-1", "medium")
    assert rec == "on-demand"


def test_recommend_capacity_type_low_tolerance_limited_od():
    checker = _make_checker()
    estimates = [_est("spot", "high", 0.3), _est("on-demand", "low", 1.0)]
    with patch.object(checker, "estimate_capacity", return_value=estimates):
        rec, reason = checker.recommend_capacity_type("g5.xlarge", "us-east-1", "low")
    assert rec == "on-demand"
    assert "capacity reservation" in reason


def test_recommend_capacity_type_high_spot_no_price_savings():
    checker = _make_checker()
    estimates = [_est("spot", "high", None), _est("on-demand", "high", None)]
    with patch.object(checker, "estimate_capacity", return_value=estimates):
        rec, reason = checker.recommend_capacity_type("g5.xlarge", "us-east-1", "medium")
    assert rec == "spot"
    assert "High spot availability" in reason


def test_recommend_capacity_type_low_spot_medium_tolerance():
    checker = _make_checker()
    estimates = [_est("spot", "low", 0.2), _est("on-demand", "high", 1.0)]
    with patch.object(checker, "estimate_capacity", return_value=estimates):
        rec, reason = checker.recommend_capacity_type("g5.xlarge", "us-east-1", "medium")
    assert rec == "on-demand"
    assert "limited" in reason


def test_list_capacity_reservations_no_filters():
    checker = _make_checker()
    mock_ec2 = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {
            "CapacityReservations": [
                {
                    "CapacityReservationId": "cr-1",
                    "InstanceType": "p5.48xlarge",
                    "AvailabilityZone": "us-east-1a",
                    "State": "active",
                    "TotalInstanceCount": 2,
                    "AvailableInstanceCount": 2,
                }
            ]
        }
    ]
    mock_ec2.get_paginator.return_value = paginator
    checker._session.client = MagicMock(return_value=mock_ec2)
    result = checker.list_capacity_reservations("us-east-1", instance_type=None, state=None)
    assert len(result) == 1
    paginator.paginate.assert_called_once_with()


def test_get_capacity_block_trend_buckets_and_skips():
    checker = _make_checker()
    mock_ec2 = MagicMock()
    now = datetime.now(UTC)
    offerings = [
        {"StartDate": None},
        {"StartDate": now + timedelta(days=2)},
        {"StartDate": now + timedelta(days=9)},
        {"StartDate": now + timedelta(days=16)},
        {"StartDate": now + timedelta(days=400)},
    ]
    mock_ec2.describe_capacity_block_offerings.return_value = {"CapacityBlockOfferings": offerings}
    checker._session.client = MagicMock(return_value=mock_ec2)
    trend = checker.get_capacity_block_trend("p5.48xlarge", "us-east-1")
    assert isinstance(trend, float)
    assert -1.0 <= trend <= 1.0


def test_get_capacity_block_trend_error_returns_zero():
    checker = _make_checker()
    mock_ec2 = MagicMock()
    mock_ec2.describe_capacity_block_offerings.side_effect = ClientError(
        {"Error": {"Code": "Unsupported", "Message": "no"}}, "DescribeCapacityBlockOfferings"
    )
    checker._session.client = MagicMock(return_value=mock_ec2)
    assert checker.get_capacity_block_trend("p5.48xlarge", "us-east-1") == 0.0


def test_get_capacity_block_trend_empty_offerings_returns_zero():
    checker = _make_checker()
    mock_ec2 = MagicMock()
    mock_ec2.describe_capacity_block_offerings.return_value = {"CapacityBlockOfferings": []}
    checker._session.client = MagicMock(return_value=mock_ec2)
    assert checker.get_capacity_block_trend("p5.48xlarge", "us-east-1") == 0.0


def test_get_capacity_block_trend_single_bin_returns_zero():
    checker = _make_checker()
    mock_ec2 = MagicMock()
    now = datetime.now(UTC)
    mock_ec2.describe_capacity_block_offerings.return_value = {
        "CapacityBlockOfferings": [
            {"StartDate": now + timedelta(days=2)},
            {"StartDate": now + timedelta(days=3)},
        ]
    }
    checker._session.client = MagicMock(return_value=mock_ec2)
    assert checker.get_capacity_block_trend("p5.48xlarge", "us-east-1") == 0.0


def test_list_all_reservations_discovers_regions():
    checker = _make_checker()
    with (
        patch("cli.aws_client.get_aws_client") as mock_get,
        patch.object(checker, "list_capacity_reservations", return_value=[]),
    ):
        mock_get.return_value.discover_regional_stacks.return_value = {"us-east-1": MagicMock()}
        result = checker.list_all_reservations()
    assert result["regions_checked"] == ["us-east-1"]
    assert result["total_reservations"] == 0


def test_list_all_reservations_no_stacks_uses_default():
    checker = _make_checker()
    with (
        patch("cli.aws_client.get_aws_client") as mock_get,
        patch.object(checker, "list_capacity_reservations", return_value=[]),
    ):
        mock_get.return_value.discover_regional_stacks.return_value = {}
        result = checker.list_all_reservations()
    assert result["regions_checked"] == ["us-east-1"]


def test_check_reservation_availability_discovers_when_no_region():
    checker = _make_checker()
    with (
        patch("cli.aws_client.get_aws_client") as mock_get,
        patch.object(checker, "list_capacity_reservations", return_value=[]),
        patch.object(checker, "list_capacity_block_offerings", return_value=[]),
    ):
        mock_get.return_value.discover_regional_stacks.return_value = {"us-west-2": MagicMock()}
        result = checker.check_reservation_availability("p5.48xlarge")
    assert result["regions_checked"] == ["us-west-2"]
    assert result["odcr"]["has_availability"] is False


def test_check_reservation_availability_skips_zero_available():
    checker = _make_checker()
    reservations = [{"available_instances": 0, "total_instances": 2}]
    with (
        patch.object(checker, "list_capacity_reservations", return_value=reservations),
        patch.object(checker, "list_capacity_block_offerings", return_value=[]),
    ):
        result = checker.check_reservation_availability(
            "p5.48xlarge", regions=["us-east-1"], min_count=1
        )
    assert result["odcr"]["total_reserved_instances"] == 2
    assert result["odcr"]["total_available_instances"] == 0
    assert result["odcr"]["reservations"] == []


def test_recommend_region_for_job_delegates():
    checker = _make_checker()
    with patch("cli.capacity.multi_region.MultiRegionCapacityChecker") as mock_cls:
        mock_cls.return_value.recommend_region_for_job.return_value = {"region": "us-east-1"}
        result = checker.recommend_region_for_job(
            gpu_required=True, min_gpus=2, instance_type="g5.xlarge", gpu_count=2
        )
    assert result == {"region": "us-east-1"}
    mock_cls.assert_called_once_with(checker.config)


def test_assess_high_az_coverage_no_scarcity():
    avail, _conf, _rec = CapacityChecker._assess_on_demand_availability(
        "g5.xlarge", None, 1.0, spot_placement_scores={"a": 9}, spot_prices=[], az_coverage=0.8
    )
    assert avail == "high"


# ---------------------------------------------------------------------------
# CapacityCheckError propagation: an AWS API failure in the primary
# availability check must surface as a typed error, not a benign
# "unavailable" result that masks throttling / auth / opt-in failures.
# ---------------------------------------------------------------------------


def _offerings_paginator(instance_types):
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {"InstanceTypeOfferings": [{"InstanceType": it} for it in instance_types]}
    ]
    return paginator


def test_check_instance_available_genuinely_not_offered_returns_false():
    """A successful lookup that omits the type still returns False (no regression)."""
    checker = _make_checker()
    mock_ec2 = MagicMock()
    mock_ec2.get_paginator.return_value = _offerings_paginator(["m5.large", "c5.large"])
    checker._session.client = MagicMock(return_value=mock_ec2)
    assert checker.check_instance_available_in_region("p5.48xlarge", "us-east-1") is False


def test_check_instance_available_offered_returns_true():
    checker = _make_checker()
    mock_ec2 = MagicMock()
    mock_ec2.get_paginator.return_value = _offerings_paginator(["g5.xlarge", "m5.large"])
    checker._session.client = MagicMock(return_value=mock_ec2)
    assert checker.check_instance_available_in_region("g5.xlarge", "us-east-1") is True


@pytest.mark.parametrize("code", ["RequestLimitExceeded", "AuthFailure", "OptInRequired"])
def test_check_instance_available_raises_on_api_failure(code):
    checker = _make_checker()
    mock_ec2 = MagicMock()
    mock_ec2.get_paginator.side_effect = ClientError(
        {"Error": {"Code": code, "Message": code}}, "DescribeInstanceTypeOfferings"
    )
    checker._session.client = MagicMock(return_value=mock_ec2)
    with pytest.raises(CapacityCheckError) as exc:
        checker.check_instance_available_in_region("g5.xlarge", "eu-west-1")
    # Message carries region and underlying error for a detailed MCP response.
    assert "eu-west-1" in str(exc.value)
    assert code in str(exc.value)


def test_estimate_capacity_propagates_capacity_check_error():
    """estimate_capacity must not swallow a raised CapacityCheckError."""
    checker = _make_checker()
    mock_ec2 = MagicMock()
    mock_ec2.get_paginator.side_effect = ClientError(
        {"Error": {"Code": "RequestLimitExceeded", "Message": "slow"}},
        "DescribeInstanceTypeOfferings",
    )
    checker._session.client = MagicMock(return_value=mock_ec2)
    with pytest.raises(CapacityCheckError):
        checker.estimate_capacity("g5.xlarge", "us-east-1")


def test_estimate_capacity_genuinely_unavailable_no_regression():
    """A genuinely not-offered type still yields availability='unavailable'."""
    checker = _make_checker()
    mock_ec2 = MagicMock()
    mock_ec2.get_paginator.return_value = _offerings_paginator(["m5.large"])
    checker._session.client = MagicMock(return_value=mock_ec2)
    estimates = checker.estimate_capacity("p5.48xlarge", "us-east-1")
    assert len(estimates) == 1
    assert estimates[0].availability == "unavailable"


# ---------------------------------------------------------------------------
# Pooled Spot Placement Scores on the live path
# ---------------------------------------------------------------------------
# GetSpotPlacementScores needs at least three instance types for a meaningful
# answer, so the checker requests the score of the catalog pool containing the
# requested type, and three non-score outcomes must stay distinguishable:
# unpooled (no request made), refused (configuration limit), and no data.


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "GetSpotPlacementScores")


def test_spot_score_request_carries_the_full_pool():
    from scripts.accelerator_catalog import pool_for_instance_type

    checker = _make_checker()
    mock_ec2 = MagicMock()
    mock_ec2.get_spot_placement_scores.return_value = {"SpotPlacementScores": [{"Score": 8}]}
    checker._session.client = MagicMock(return_value=mock_ec2)

    scores = checker.get_spot_placement_score("g5.xlarge", "us-east-1")

    kwargs = mock_ec2.get_spot_placement_scores.call_args.kwargs
    pool = pool_for_instance_type("g5.xlarge")
    assert kwargs["InstanceTypes"] == list(pool.members)
    assert len(kwargs["InstanceTypes"]) >= 3
    assert "g5.xlarge" in kwargs["InstanceTypes"]
    assert kwargs["TargetCapacity"] == 1
    assert scores == {"regional": 8}


def test_unpooled_type_skips_the_request_entirely():
    checker = _make_checker()
    mock_ec2 = MagicMock()
    checker._session.client = MagicMock(return_value=mock_ec2)

    # m5.large is not an accelerator type and belongs to no pool: issuing a
    # single-type request would return a misleadingly low score, so none is
    # issued at all.
    scores = checker.get_spot_placement_score("m5.large", "us-east-1")

    mock_ec2.get_spot_placement_scores.assert_not_called()
    assert scores == {}


def test_config_limit_refusal_raises_a_named_condition():
    from cli.capacity import SpotPlacementConfigLimitError

    checker = _make_checker()
    mock_ec2 = MagicMock()
    mock_ec2.get_spot_placement_scores.side_effect = _client_error("MaxConfigLimitExceeded")
    checker._session.client = MagicMock(return_value=mock_ec2)

    with pytest.raises(SpotPlacementConfigLimitError) as excinfo:
        checker.get_spot_placement_score("g5.xlarge", "us-east-1")

    # The condition names the refusal, so a caller can never mistake it for
    # an empty-but-successful score lookup.
    assert "MaxConfigLimitExceeded" in str(excinfo.value)


def test_scored_estimate_names_pool_and_target_capacity():
    checker = _make_checker()
    with (
        patch.object(checker, "get_spot_placement_score", return_value={"regional": 8}),
        patch.object(checker, "get_spot_price_history", return_value=[]),
        patch.object(checker, "get_on_demand_price", return_value=None),
        patch.object(checker, "get_availability_zones", return_value=["us-east-1a"]),
    ):
        estimates = checker._estimate_spot_capacity("g5.xlarge", "us-east-1", None)

    details = estimates[0].details
    assert details["spot_pool"] == "single-gpu-24gb"
    assert details["sps_target_capacity"] == 1
    # The interpretation still renders the numeric score and now conveys the
    # pool-at-capacity semantics.
    assert details["spot_placement_score"] == 8
    assert "8/10" in details["score_interpretation"]
    assert "single-gpu-24gb" in details["score_interpretation"]
    assert "target capacity 1" in details["score_interpretation"]
    assert estimates[0].confidence == 0.85


def test_refused_estimate_degrades_without_score_confidence():
    from cli.capacity import SpotPlacementConfigLimitError

    checker = _make_checker()
    prices = [SpotPriceInfo("g5.xlarge", "us-east-1a", 0.5, 0.5, 0.4, 0.6, 0.9)]
    with (
        patch.object(
            checker,
            "get_spot_placement_score",
            side_effect=SpotPlacementConfigLimitError("refused"),
        ),
        patch.object(checker, "get_spot_price_history", return_value=prices),
        patch.object(checker, "get_on_demand_price", return_value=None),
        patch.object(checker, "get_availability_zones", return_value=["us-east-1a"]),
    ):
        estimates = checker._estimate_spot_capacity("g5.xlarge", "us-east-1", None)

    estimate = estimates[0]
    # Price fallback confidence, never the 0.85 reserved for a real score.
    assert estimate.confidence == 0.5
    assert "no placement score was obtained" in estimate.recommendation
    assert "configuration limit" in estimate.recommendation
    assert "spot_placement_score" not in estimate.details


def test_unpooled_estimate_says_no_score_was_obtained():
    checker = _make_checker()
    prices = [SpotPriceInfo("m5.large", "us-east-1a", 0.1, 0.1, 0.08, 0.12, 0.9)]
    mock_ec2 = MagicMock()
    checker._session.client = MagicMock(return_value=mock_ec2)
    with (
        patch.object(checker, "get_spot_price_history", return_value=prices),
        patch.object(checker, "get_on_demand_price", return_value=None),
        patch.object(checker, "get_availability_zones", return_value=["us-east-1a"]),
    ):
        estimates = checker._estimate_spot_capacity("m5.large", "us-east-1", None)

    estimate = estimates[0]
    mock_ec2.get_spot_placement_scores.assert_not_called()
    assert estimate.confidence == 0.5
    assert "no placement score was obtained" in estimate.recommendation
    assert "belongs to no instance pool" in estimate.recommendation


def test_no_data_estimate_reports_why_no_score_exists():
    from cli.capacity import SpotPlacementConfigLimitError

    checker = _make_checker()
    with (
        patch.object(
            checker,
            "get_spot_placement_score",
            side_effect=SpotPlacementConfigLimitError("refused"),
        ),
        patch.object(checker, "get_spot_price_history", return_value=[]),
        patch.object(checker, "get_on_demand_price", return_value=None),
        patch.object(checker, "get_availability_zones", return_value=["us-east-1a"]),
    ):
        estimates = checker._estimate_spot_capacity("g5.xlarge", "us-east-1", None)

    estimate = estimates[0]
    assert estimate.availability == "unknown"
    assert estimate.confidence == 0.1
    assert "configuration limit" in estimate.recommendation


def test_on_demand_only_path_survives_a_config_limit_refusal():
    from cli.capacity import SpotPlacementConfigLimitError

    checker = _make_checker()
    with (
        patch.object(
            checker,
            "get_spot_placement_score",
            side_effect=SpotPlacementConfigLimitError("refused"),
        ),
        patch.object(checker, "get_spot_price_history", return_value=[]),
        patch.object(checker, "get_on_demand_price", return_value=1.0),
        patch.object(checker, "get_availability_zones", return_value=["us-east-1a"]),
        patch.object(checker, "check_instance_available_in_region", return_value=True),
        patch.object(checker, "get_az_coverage", return_value=1.0),
    ):
        estimate = checker._estimate_on_demand_capacity("g5.xlarge", "us-east-1", None)

    # The estimate degrades to the remaining scarcity signals instead of
    # propagating the refusal to the caller.
    assert estimate is not None
    assert estimate.capacity_type == "on-demand"


def test_pool_lookup_loads_the_catalog_without_repo_root_on_sys_path(monkeypatch):
    # The installed `gco` entrypoint does not put the repository root on
    # sys.path, so `import scripts` fails even inside a checkout (live
    # testing caught exactly this). The lookup must fall back to loading the
    # catalog module by its checkout-relative path.
    import builtins

    from cli.capacity import checker as checker_module

    real_import = builtins.__import__

    def missing_scripts(name, *args, **kwargs):
        if name.startswith("scripts"):
            raise ImportError("repository root is not on sys.path")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_scripts)
    checker_module._pool_catalog_lookup.cache_clear()
    try:
        pool = CapacityChecker.instance_pool_for("g5.xlarge")
        assert pool is not None
        assert pool[0] == "single-gpu-24gb"
        assert "g5.xlarge" in pool[1]
    finally:
        checker_module._pool_catalog_lookup.cache_clear()


def test_pool_lookup_degrades_when_catalog_is_unavailable(monkeypatch, tmp_path):
    # An installed wheel outside a checkout ships neither an importable
    # `scripts` package nor the catalog file: the checker treats every type
    # as unpooled and never issues a known-invalid single-type request.
    import builtins

    from cli.capacity import checker as checker_module

    real_import = builtins.__import__

    def missing_scripts(name, *args, **kwargs):
        if name.startswith("scripts"):
            raise ImportError("scripts is not shipped in the installed wheel")
        return real_import(name, *args, **kwargs)

    checker = _make_checker()
    mock_ec2 = MagicMock()
    checker._session.client = MagicMock(return_value=mock_ec2)
    monkeypatch.setattr(builtins, "__import__", missing_scripts)
    monkeypatch.setattr(
        checker_module, "_POOL_CATALOG_PATH", tmp_path / "missing" / "accelerator_catalog.py"
    )
    checker_module._pool_catalog_lookup.cache_clear()
    try:
        scores = checker.get_spot_placement_score("g5.xlarge", "us-east-1")
        assert scores == {}
        mock_ec2.get_spot_placement_scores.assert_not_called()
    finally:
        checker_module._pool_catalog_lookup.cache_clear()
