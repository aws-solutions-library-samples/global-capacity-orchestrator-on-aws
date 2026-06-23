"""Coverage-focused unit tests for ``cli/capacity/advisor.py`` and ``cli/capacity/multi_region.py``.

These modules back the Bedrock-assisted capacity advisor and the multi-region capacity aggregator. Their happy paths run under ``test_capacity.py``; this module covers the prompt-construction and aggregation branches left untested:

* ``advisor._build_prompt`` requirement rendering: the optional ``min_memory`` / ``fault_tolerance`` / ``max_cost`` fields and the On-Demand Capacity Reservation and Capacity Block prompt sections.
* ``multi_region`` data gathering: the no-stack and no-queue-URL paths, empty-metrics handling, and the SQS / CloudWatch ``ClientError`` and unexpected-error handlers, plus ``get_all_regions_capacity`` continue-on-error.
* Recommendation tie-breaks (simple and weighted), capacity-block trend up/down signals, the ``compute_price_trend`` zero-mean guard, and the module factory function.
"""

from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from cli.aws_client import RegionalStack
from cli.capacity import (
    BedrockCapacityAdvisor,
    MultiRegionCapacityChecker,
    RegionCapacity,
    compute_price_trend,
    get_multi_region_capacity_checker,
)


def _make_advisor():
    with patch("cli.capacity.advisor.get_config") as mock_config, patch("boto3.Session"):
        mock_config.return_value = MagicMock(default_region="us-east-1")
        return BedrockCapacityAdvisor()


def _make_mr():
    with patch("cli.capacity.multi_region.get_config") as mock_config, patch("boto3.Session"):
        mock_config.return_value = MagicMock(default_region="us-east-1")
        return MultiRegionCapacityChecker()


def _stack(region="us-east-1"):
    return RegionalStack(
        region=region,
        stack_name="gco-" + region,
        cluster_name="gco-" + region,
        status="CREATE_COMPLETE",
    )


def _factory(clients):
    def _client(service, region_name=None):
        return clients.get(service, MagicMock())

    return _client


def test_build_prompt_requirement_fields():
    advisor = _make_advisor()
    data = {
        "timestamp": "t",
        "regions_analyzed": ["us-east-1"],
        "instance_types_analyzed": ["g5.xlarge"],
    }
    prompt = advisor._build_prompt(
        data,
        requirements={"min_memory_gb": 16, "fault_tolerance": "high", "max_cost_per_hour": 5.0},
    )
    assert "Minimum Memory: 16 GB" in prompt
    assert "Fault Tolerance: high" in prompt
    assert "Max Cost/Hour: $5.0" in prompt


def test_build_prompt_reservations_and_blocks():
    advisor = _make_advisor()
    data = {
        "timestamp": "t",
        "regions_analyzed": ["us-east-1"],
        "instance_types_analyzed": ["p5.48xlarge"],
        "reservations": {
            "p5.48xlarge": {
                "us-east-1": [
                    {"az": "us-east-1a", "total": 4, "available": 2, "utilization_pct": 50}
                ]
            }
        },
        "capacity_blocks": {
            "p5.48xlarge": {
                "us-east-1": [
                    {
                        "az": "us-east-1a",
                        "duration_hours": 24,
                        "start_date": "2025-01-01",
                        "upfront_fee": 100,
                    }
                ]
            }
        },
    }
    prompt = advisor._build_prompt(data)
    assert "CAPACITY RESERVATIONS (ODCRs)" in prompt
    assert "CAPACITY BLOCK OFFERINGS" in prompt
    assert "2/4 available" in prompt


def test_get_multi_region_capacity_checker_factory():
    with patch("cli.capacity.multi_region.get_config") as mock_config, patch("boto3.Session"):
        mock_config.return_value = MagicMock(default_region="us-east-1")
        checker = get_multi_region_capacity_checker()
    assert isinstance(checker, MultiRegionCapacityChecker)


def test_compute_price_trend_zero_mean_returns_stable():
    result = compute_price_trend([0.0, 0.0, 0.0])
    assert result["direction"] == "stable"
    assert result["slope"] == 0.0


def test_get_region_capacity_no_stack():
    checker = _make_mr()
    with patch("cli.aws_client.get_aws_client") as mock_get:
        mock_get.return_value.get_regional_stack.return_value = None
        capacity = checker.get_region_capacity("us-east-1")
    assert capacity.region == "us-east-1"
    assert capacity.queue_depth == 0


def test_get_region_capacity_no_queue_url_empty_metrics():
    checker = _make_mr()
    mock_cfn = MagicMock()
    mock_cloudwatch = MagicMock()
    mock_cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}
    mock_cloudwatch.get_metric_statistics.return_value = {"Datapoints": []}
    checker._session.client = _factory({"cloudformation": mock_cfn, "cloudwatch": mock_cloudwatch})
    with patch("cli.aws_client.get_aws_client") as mock_get:
        mock_get.return_value.get_regional_stack.return_value = _stack()
        capacity = checker.get_region_capacity("us-east-1")
    assert capacity.queue_depth == 0
    assert capacity.gpu_utilization == 0.0
    assert capacity.cpu_utilization == 0.0


def test_get_region_capacity_queue_client_error():
    checker = _make_mr()
    mock_cfn = MagicMock()
    mock_cloudwatch = MagicMock()
    mock_cfn.describe_stacks.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "no"}}, "DescribeStacks"
    )
    mock_cloudwatch.get_metric_statistics.return_value = {"Datapoints": []}
    checker._session.client = _factory({"cloudformation": mock_cfn, "cloudwatch": mock_cloudwatch})
    with patch("cli.aws_client.get_aws_client") as mock_get:
        mock_get.return_value.get_regional_stack.return_value = _stack()
        capacity = checker.get_region_capacity("us-east-1")
    assert capacity.queue_depth == 0


def test_get_region_capacity_queue_unexpected_error():
    checker = _make_mr()
    mock_cfn = MagicMock()
    mock_cloudwatch = MagicMock()
    mock_cfn.describe_stacks.side_effect = RuntimeError("boom")
    mock_cloudwatch.get_metric_statistics.return_value = {"Datapoints": []}
    checker._session.client = _factory({"cloudformation": mock_cfn, "cloudwatch": mock_cloudwatch})
    with patch("cli.aws_client.get_aws_client") as mock_get:
        mock_get.return_value.get_regional_stack.return_value = _stack()
        capacity = checker.get_region_capacity("us-east-1")
    assert capacity.region == "us-east-1"


def test_get_region_capacity_cloudwatch_client_error():
    checker = _make_mr()
    mock_cfn = MagicMock()
    mock_sqs = MagicMock()
    mock_cloudwatch = MagicMock()
    mock_cfn.describe_stacks.return_value = {
        "Stacks": [
            {"Outputs": [{"OutputKey": "JobQueueUrl", "OutputValue": "https://sqs.example/q"}]}
        ]
    }
    mock_sqs.get_queue_attributes.return_value = {
        "Attributes": {
            "ApproximateNumberOfMessages": "3",
            "ApproximateNumberOfMessagesNotVisible": "1",
        }
    }
    mock_cloudwatch.get_metric_statistics.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "no"}}, "GetMetricStatistics"
    )
    checker._session.client = _factory(
        {"cloudformation": mock_cfn, "sqs": mock_sqs, "cloudwatch": mock_cloudwatch}
    )
    with patch("cli.aws_client.get_aws_client") as mock_get:
        mock_get.return_value.get_regional_stack.return_value = _stack()
        capacity = checker.get_region_capacity("us-east-1")
    assert capacity.queue_depth == 3
    assert capacity.running_jobs == 1


def test_get_region_capacity_cloudwatch_unexpected_error():
    checker = _make_mr()
    mock_cfn = MagicMock()
    mock_sqs = MagicMock()
    mock_cloudwatch = MagicMock()
    mock_cfn.describe_stacks.return_value = {
        "Stacks": [
            {"Outputs": [{"OutputKey": "JobQueueUrl", "OutputValue": "https://sqs.example/q"}]}
        ]
    }
    mock_sqs.get_queue_attributes.return_value = {
        "Attributes": {
            "ApproximateNumberOfMessages": "2",
            "ApproximateNumberOfMessagesNotVisible": "0",
        }
    }
    mock_cloudwatch.get_metric_statistics.side_effect = RuntimeError("boom")
    checker._session.client = _factory(
        {"cloudformation": mock_cfn, "sqs": mock_sqs, "cloudwatch": mock_cloudwatch}
    )
    with patch("cli.aws_client.get_aws_client") as mock_get:
        mock_get.return_value.get_regional_stack.return_value = _stack()
        capacity = checker.get_region_capacity("us-east-1")
    assert capacity.queue_depth == 2


def test_get_all_regions_capacity_with_exception():
    checker = _make_mr()
    good = RegionCapacity(region="us-east-1")
    with (
        patch("cli.aws_client.get_aws_client") as mock_get,
        patch.object(checker, "get_region_capacity", side_effect=[good, RuntimeError("boom")]),
    ):
        mock_get.return_value.discover_regional_stacks.return_value = {
            "us-east-1": MagicMock(),
            "us-west-2": MagicMock(),
        }
        capacities = checker.get_all_regions_capacity()
    assert len(capacities) == 1
    assert capacities[0].region == "us-east-1"


def test_simple_recommend_high_metrics_no_reasons():
    checker = _make_mr()
    cap = RegionCapacity(region="us-east-1", queue_depth=10, gpu_utilization=85.0, running_jobs=10)
    result = checker._simple_recommend([cap])
    assert result["region"] == "us-east-1"
    assert result["reason"] == "best overall capacity"


def test_weighted_recommend_high_metrics_trend_up():
    checker = _make_mr()
    cap = RegionCapacity(region="us-east-1", queue_depth=10, gpu_utilization=80.0, running_jobs=10)
    with patch("cli.capacity.multi_region.CapacityChecker") as mock_cls:
        inst = mock_cls.return_value
        inst.get_spot_placement_score.return_value = {}
        inst.get_spot_price_history.return_value = []
        inst.get_on_demand_price.return_value = None
        inst.get_capacity_block_trend.return_value = 0.5
        result = checker._weighted_recommend([cap], "p5.48xlarge", gpu_count=8)
    assert result["region"] == "us-east-1"
    assert "trending up" in result["reason"]


def test_weighted_recommend_trend_down():
    checker = _make_mr()
    cap = RegionCapacity(region="us-east-1", queue_depth=10, gpu_utilization=80.0, running_jobs=10)
    with patch("cli.capacity.multi_region.CapacityChecker") as mock_cls:
        inst = mock_cls.return_value
        inst.get_spot_placement_score.return_value = {}
        inst.get_spot_price_history.return_value = []
        inst.get_on_demand_price.return_value = None
        inst.get_capacity_block_trend.return_value = -0.5
        result = checker._weighted_recommend([cap], "p5.48xlarge", gpu_count=8)
    assert "trending down" in result["reason"]
