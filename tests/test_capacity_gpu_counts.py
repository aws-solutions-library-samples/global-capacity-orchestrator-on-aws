"""A Region without GPU nodes is not idle GPU capacity.

The health monitor's ``ClusterGpuUtilization`` share is 0 both for a cluster
with no GPU nodes and for one whose GPUs are all free. It also publishes the
counts behind the share (``ClusterGpuAllocatable``, ``ClusterGpuRequested``),
and every capacity surface reads them: the checker (``RegionCapacity``'s
``total_gpus`` and ``available_gpus``), the recommendation reasons, the
``gco capacity status`` table and verbose ranking, the ``gco status``
document, the capacity advisor's prompt and the mission environment block.
Scoring is unchanged: a Region without GPU nodes is uncontended.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from click.testing import CliRunner

from cli.capacity.advisor import BedrockCapacityAdvisor, _cluster_metrics, _gpu_count_line
from cli.capacity.multi_region import (
    NO_GPU_NODES_REASON,
    MultiRegionCapacityChecker,
    RegionCapacity,
)
from cli.commands.capacity_cmd import _gpu_cell, _ranked_region_line, capacity
from cli.commands.status_cmd import _render_capacity
from cli.config import GCOConfig
from cli.status import _capacity_entry_from, _capacity_unavailable_entry

_T0 = datetime(2026, 11, 27, 10, 0, tzinfo=UTC)
_REGION = "us-east-1"
_CLUSTER = "gco-us-east-1"


def _checker() -> MultiRegionCapacityChecker:
    checker = object.__new__(MultiRegionCapacityChecker)
    checker.config = SimpleNamespace(default_region=_REGION)
    checker._session = MagicMock()
    checker._last_region_errors = []
    return checker


def _metrics(**series: list[dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """A ``get_metric_statistics`` double answering each MetricName from ``series``."""

    def statistics(MetricName: str, **_kwargs: Any) -> dict[str, Any]:
        return {"Datapoints": series.get(MetricName, [])}

    return statistics


#: Every scored signal present, so the counts are the only thing that varies.
_SIGNALS: dict[str, list[dict[str, Any]]] = {
    "node_gpu_utilization": [],
    "ClusterGpuUtilization": [{"Timestamp": _T0, "Average": 0.0}],
    "node_cpu_utilization": [{"Timestamp": _T0, "Average": 20.0}],
    "ActiveJobs": [{"Timestamp": _T0, "Maximum": 0.0}],
}


def _region_capacity(statistics: Callable[..., dict[str, Any]]) -> tuple[RegionCapacity, MagicMock]:
    checker = _checker()
    stack = SimpleNamespace(stack_name=_CLUSTER, cluster_name=_CLUSTER)
    cloudformation = MagicMock()
    cloudformation.describe_stacks.return_value = {
        "Stacks": [{"Outputs": [{"OutputKey": "JobQueueUrl", "OutputValue": "https://sqs/q"}]}]
    }
    sqs = MagicMock()
    sqs.get_queue_attributes.return_value = {"Attributes": {"ApproximateNumberOfMessages": "0"}}
    cloudwatch = MagicMock()
    cloudwatch.get_metric_statistics.side_effect = statistics
    clients = {"cloudformation": cloudformation, "sqs": sqs, "cloudwatch": cloudwatch}
    checker._session.client.side_effect = lambda service, **_kwargs: clients[service]
    aws_client = MagicMock()
    aws_client.get_regional_stack.return_value = stack
    with patch("cli.aws_client.get_aws_client", return_value=aws_client):
        return checker.get_region_capacity(_REGION), cloudwatch


def _count_queries(cloudwatch: MagicMock) -> list[dict[str, Any]]:
    return [
        call.kwargs
        for call in cloudwatch.get_metric_statistics.call_args_list
        if call.kwargs["MetricName"] in ("ClusterGpuAllocatable", "ClusterGpuRequested")
    ]


# ---------------------------------------------------------------------------
# The checker reads the counts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("allocatable", "requested", "expected"),
    [
        pytest.param(
            [
                {"Timestamp": _T0, "Maximum": 4.0},
                {"Timestamp": _T0 + timedelta(minutes=1), "Maximum": 8.0},
                {"Maximum": 99.0},  # no timestamp: sorts oldest
            ],
            [{"Timestamp": _T0, "Maximum": 2.0}],
            (8, 6),
            id="newest-count-wins",
        ),
        pytest.param(
            [{"Timestamp": _T0, "Maximum": 0.0}],
            [{"Timestamp": _T0, "Maximum": 0.0}],
            (0, 0),
            id="no-gpu-nodes",
        ),
        # A device plugin restarting can leave pods holding more GPUs than
        # the nodes report: nothing is free, never a negative count.
        pytest.param(
            [{"Timestamp": _T0, "Maximum": 2.0}],
            [{"Timestamp": _T0, "Maximum": 3.0}],
            (2, 0),
            id="over-committed",
        ),
    ],
)
def test_the_checker_reads_the_health_monitors_gpu_counts(
    allocatable: list[dict[str, Any]],
    requested: list[dict[str, Any]],
    expected: tuple[int, int],
) -> None:
    capacity, cloudwatch = _region_capacity(
        _metrics(**_SIGNALS, ClusterGpuAllocatable=allocatable, ClusterGpuRequested=requested)
    )

    assert (capacity.total_gpus, capacity.available_gpus) == expected
    assert capacity.without_gpu_nodes is (expected[0] == 0)
    # The counts are not scored: the telemetry and the score are as before.
    assert capacity.telemetry_status == "complete"
    assert capacity.telemetry_errors == []
    assert capacity.recommendation_score == 0.0
    queries = _count_queries(cloudwatch)
    assert [query["MetricName"] for query in queries] == [
        "ClusterGpuAllocatable",
        "ClusterGpuRequested",
    ]
    for query in queries:
        assert query["Namespace"] == "GCO/HealthMonitor"
        assert query["Dimensions"] == [
            {"Name": "ClusterName", "Value": _CLUSTER},
            {"Name": "Region", "Value": _REGION},
        ]
        assert (query["Period"], query["Statistics"]) == (60, ["Maximum"])
        assert query["EndTime"] - query["StartTime"] == timedelta(minutes=5)


def test_a_monitor_without_the_counts_leaves_them_unread() -> None:
    """A health monitor that predates the counts publishes neither series."""
    capacity, cloudwatch = _region_capacity(
        _metrics(**_SIGNALS, ClusterGpuAllocatable=[{"Timestamp": _T0, "Maximum": 4.0}])
    )

    assert (capacity.total_gpus, capacity.available_gpus) == (None, None)
    assert capacity.without_gpu_nodes is False
    assert capacity.telemetry_status == "complete"
    assert [query["MetricName"] for query in _count_queries(cloudwatch)] == [
        "ClusterGpuAllocatable",
        "ClusterGpuRequested",
    ]


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(
            ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "no"}}, "GetMetricStatistics"
            ),
            id="client-error",
        ),
        pytest.param({"Datapoints": [{"Timestamp": _T0, "Average": 4.0}]}, id="no-maximum"),
    ],
)
def test_a_failed_count_read_leaves_the_counts_and_the_telemetry_alone(answer: Any) -> None:
    signals = _metrics(**_SIGNALS)

    def statistics(MetricName: str, **kwargs: Any) -> dict[str, Any]:
        if MetricName == "ClusterGpuAllocatable":
            if isinstance(answer, Exception):
                raise answer
            return answer
        return signals(MetricName=MetricName, **kwargs)

    capacity, _cloudwatch = _region_capacity(statistics)

    assert (capacity.total_gpus, capacity.available_gpus) == (None, None)
    assert capacity.telemetry_status == "complete"
    assert capacity.telemetry_errors == []


@pytest.mark.parametrize(("total", "expected"), [(None, False), (0, True), (4, False)])
def test_without_gpu_nodes_needs_a_count_of_zero(total: int | None, expected: bool) -> None:
    assert RegionCapacity(region=_REGION, total_gpus=total).without_gpu_nodes is expected


def test_the_publisher_and_the_checker_name_the_same_series() -> None:
    """The checker's count queries select exactly what the monitor publishes.

    The two sides never import each other, and CloudWatch matches a metric
    only on its exact namespace, name and dimension set, so drift on either
    side would leave the counts unread forever.
    """
    with patch("gco.services.metrics_publisher.boto3.client") as client_factory:
        from gco.services.metrics_publisher import HealthMonitorMetrics

        publisher = HealthMonitorMetrics(cluster_name=_CLUSTER, region=_REGION)
        assert publisher.publish_resource_utilization(
            cpu_percent=10.0,
            memory_percent=20.0,
            gpu_percent=25.0,
            active_jobs=1,
            gpu_allocatable=8,
            gpu_requested=2,
        )
        published = client_factory.return_value.put_metric_data.call_args.kwargs
    series = {
        (
            published["Namespace"],
            metric["MetricName"],
            frozenset((d["Name"], d["Value"]) for d in metric["Dimensions"]),
        ): metric["Value"]
        for metric in published["MetricData"]
    }

    def statistics(**query: Any) -> dict[str, Any]:
        key = (
            query["Namespace"],
            query["MetricName"],
            frozenset((d["Name"], d["Value"]) for d in query["Dimensions"]),
        )
        if key not in series:
            return {"Datapoints": []}
        return {"Datapoints": [{"Timestamp": _T0, "Maximum": float(series[key])}]}

    cloudwatch = MagicMock()
    cloudwatch.get_metric_statistics.side_effect = statistics

    assert MultiRegionCapacityChecker._gpu_counts(cloudwatch, _CLUSTER, _REGION) == (8, 2)


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------


def _capacity(region: str, *, total: int | None, available: int | None, gpu: float = 0.0):
    return RegionCapacity(
        region=region,
        gpu_utilization=gpu,
        total_gpus=total,
        available_gpus=available,
        telemetry_status="complete",
    )


def test_the_simple_recommendation_says_a_region_has_no_gpu_nodes() -> None:
    checker = _checker()
    result = checker._simple_recommend(
        [
            _capacity("us-east-1", total=0, available=0),
            _capacity("us-west-2", total=8, available=8, gpu=0.0),
        ]
    )

    # Both are uncontended (0% GPU, scored alike); the tie breaks on the name.
    assert result["region"] == "us-east-1"
    assert NO_GPU_NODES_REASON in result["reason"]
    assert "GPU available" not in result["reason"]
    assert (result["total_gpus"], result["available_gpus"]) == (0, 0)
    assert [(e["region"], e["total_gpus"], e["available_gpus"]) for e in result["all_regions"]] == [
        ("us-east-1", 0, 0),
        ("us-west-2", 8, 8),
    ]


def test_the_simple_recommendation_still_reports_free_gpus() -> None:
    result = _checker()._simple_recommend([_capacity("us-west-2", total=8, available=6, gpu=25.0)])

    assert "75% GPU available" in result["reason"]
    assert NO_GPU_NODES_REASON not in result["reason"]


def _weighted(capacities: list[RegionCapacity]) -> dict[str, Any]:
    checker = _checker()
    with patch("cli.capacity.multi_region.CapacityChecker") as checker_class:
        market = checker_class.return_value
        market.get_spot_placement_score.return_value = {}
        market.get_spot_price_history.return_value = []
        market.get_on_demand_price.return_value = None
        market.get_capacity_block_trend.return_value = 0.0
        return checker._weighted_recommend(capacities, "g5.xlarge")


@pytest.mark.parametrize(
    ("capacity", "present", "absent"),
    [
        pytest.param(
            _capacity("us-east-1", total=0, available=0),
            NO_GPU_NODES_REASON,
            "GPU available",
            id="no-gpu-nodes",
        ),
        pytest.param(
            _capacity("us-east-1", total=8, available=6, gpu=25.0),
            "75% GPU available",
            NO_GPU_NODES_REASON,
            id="free-gpus",
        ),
        pytest.param(
            _capacity("us-east-1", total=8, available=1, gpu=90.0),
            None,
            "GPU available",
            id="busy-gpus",
        ),
    ],
)
def test_the_weighted_recommendation_reads_the_counts(
    capacity: RegionCapacity, present: str | None, absent: str
) -> None:
    result = _weighted([capacity])

    if present is not None:
        assert present in result["reason"]
    assert absent not in result["reason"]
    assert (result["total_gpus"], result["available_gpus"]) == (
        capacity.total_gpus,
        capacity.available_gpus,
    )
    (entry,) = result["all_regions"]
    assert (entry["total_gpus"], entry["available_gpus"]) == (
        capacity.total_gpus,
        capacity.available_gpus,
    )


# ---------------------------------------------------------------------------
# gco capacity status and recommend-region --verbose
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("utilization", "total", "measured", "cell"),
    [
        (0.0, 0, True, "none"),
        (0.0, 4, True, "0%"),
        (37.4, None, True, "37%"),
        (0.0, 0, False, "-"),
    ],
)
def test_the_gpu_cell(utilization: float, total: int | None, measured: bool, cell: str) -> None:
    assert _gpu_cell(utilization, total, measured=measured) == cell


def _status_table(capacities: list[RegionCapacity]) -> str:
    checker = MagicMock()
    checker.get_all_regions_capacity.return_value = capacities
    checker.region_errors = []
    config = GCOConfig(project_name="test-gco", default_region=_REGION, output_format="table")
    with patch("cli.capacity.get_multi_region_capacity_checker", return_value=checker):
        result = CliRunner().invoke(capacity, ["status"], obj=config)
    assert result.exit_code == 0, result.output
    return result.output


def test_the_status_table_shows_none_for_a_region_without_gpu_nodes() -> None:
    gpu_less = _capacity("us-east-1", total=0, available=0)
    busy = _capacity("us-west-2", total=8, available=2, gpu=75.0)
    unread = RegionCapacity(
        region="eu-west-1", unavailable_signals=["gpu"], telemetry_status="partial", total_gpus=0
    )

    output = _status_table([gpu_less, busy, unread])

    regions = {"us-east-1", "us-west-2", "eu-west-1"}
    rows = {
        cells[0]: cells
        for cells in (line.split() for line in output.splitlines())
        if cells and cells[0] in regions
    }
    assert rows["us-east-1"][3] == "none"
    assert rows["us-west-2"][3] == "75%"
    assert rows["eu-west-1"][3] == "-"  # unmeasured wins over the count
    assert f"GPU% none: {NO_GPU_NODES_REASON}" in output


def test_the_status_table_has_no_footnote_when_every_region_has_gpus() -> None:
    output = _status_table([_capacity("us-west-2", total=8, available=8)])

    assert "GPU% none" not in output


@pytest.mark.parametrize(
    ("entry", "gpu"),
    [
        ({"total_gpus": 0}, "gpu=none"),
        ({"total_gpus": 4}, "gpu=25%"),
        ({}, "gpu=25%"),
    ],
)
def test_the_verbose_ranking_line_names_a_region_without_gpu_nodes(
    entry: dict[str, Any], gpu: str
) -> None:
    line = _ranked_region_line(
        "eks",
        {"region": "us-east-1", "score": 0.5, "queue_depth": 1, "gpu_utilization": 25.0} | entry,
    )

    assert line == f"  us-east-1: score=0.5000, queue=1, {gpu}"


# ---------------------------------------------------------------------------
# gco status
# ---------------------------------------------------------------------------


def test_the_status_document_carries_the_counts() -> None:
    entry = _capacity_entry_from(_capacity("us-east-1", total=8, available=6, gpu=25.0))

    assert (entry["total_gpus"], entry["available_gpus"]) == (8, 6)
    unread = _capacity_entry_from(RegionCapacity(region="us-east-1"))
    assert (unread["total_gpus"], unread["available_gpus"]) == (None, None)
    unavailable = _capacity_unavailable_entry()
    assert (unavailable["total_gpus"], unavailable["available_gpus"]) == (None, None)


def test_the_status_text_says_none_for_a_region_without_gpu_nodes() -> None:
    base = {
        "queue_depth": 0,
        "running_jobs": 0,
        "cpu_utilization": 5.0,
        "telemetry_status": "complete",
        "unavailable_signals": [],
    }
    lines = _render_capacity(
        {
            "by_region": {
                "us-east-1": base | {"gpu_utilization": 0.0, "total_gpus": 0},
                "us-west-2": base | {"gpu_utilization": 75.5, "total_gpus": 8},
                "eu-west-1": base | {"gpu_utilization": 0.0, "total_gpus": None},
            }
        }
    )

    assert "gpu none " in lines[0]
    assert "gpu 75.5 " in lines[1]
    assert "gpu 0.0 " in lines[2]


# ---------------------------------------------------------------------------
# The capacity advisor's prompt
# ---------------------------------------------------------------------------


def test_the_advisor_passes_the_counts_and_drops_unread_ones() -> None:
    read = _cluster_metrics("us-east-1", _capacity("us-east-1", total=8, available=6))
    assert (read["total_gpus"], read["available_gpus"]) == (8, 6)
    # A stand-in without real counts (or with no attributes at all) reads None.
    mocked = _cluster_metrics("us-east-1", MagicMock(unavailable_signals=[]))
    assert (mocked["total_gpus"], mocked["available_gpus"]) == (None, None)
    bare = _cluster_metrics(
        "us-east-1",
        SimpleNamespace(
            unavailable_signals=[],
            telemetry_status="complete",
            queue_depth=0,
            running_jobs=0,
            gpu_utilization=0.0,
            cpu_utilization=0.0,
        ),
    )
    assert (bare["total_gpus"], bare["available_gpus"]) == (None, None)


@pytest.mark.parametrize(
    ("metrics", "line"),
    [
        ({"total_gpus": None}, ""),
        ({}, ""),
        (
            {"total_gpus": 0, "available_gpus": 0},
            "    - GPUs: none allocatable (no GPU nodes running; EKS Auto Mode launches them "
            "on demand)\n",
        ),
        ({"total_gpus": 8, "available_gpus": 6}, "    - GPUs: 6 of 8 allocatable free\n"),
    ],
)
def test_the_gpu_count_line(metrics: dict[str, Any], line: str) -> None:
    assert _gpu_count_line(metrics) == line


def test_the_cluster_section_and_the_ranking_name_a_region_without_gpu_nodes() -> None:
    cluster = BedrockCapacityAdvisor._cluster_section(
        {
            "cluster_metrics": [
                {
                    "region": "us-east-1",
                    "telemetry_status": "complete",
                    "queue_depth": 0,
                    "running_jobs": 0,
                    "gpu_utilization": 0.0,
                    "cpu_utilization": 5.0,
                    "total_gpus": 0,
                    "available_gpus": 0,
                }
            ]
        },
        ["us-east-1"],
    )
    assert "    - GPUs: none allocatable (no GPU nodes running" in cluster

    ranking = BedrockCapacityAdvisor._ranking_section(
        {
            "weighted_recommendation": {
                "scoring_method": "weighted",
                "instance_type": "g5.xlarge",
                "all_regions": [
                    {"region": "us-east-1", "score": 1.0, "gpu_utilization": 0.0, "total_gpus": 0},
                    {"region": "us-west-2", "score": 2.0, "gpu_utilization": 40.0, "total_gpus": 8},
                ],
            }
        }
    )
    east, west = (line for line in ranking.splitlines() if line.startswith("  us-"))
    assert "no GPU nodes" in east and "GPU util" not in east
    assert "GPU util 40%" in west


# ---------------------------------------------------------------------------
# The mission environment block
# ---------------------------------------------------------------------------


def test_the_mission_environment_carries_the_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    from gco_mcp.mission import _environment

    monkeypatch.setattr(
        _environment,
        "_summarise_reservations",
        lambda _checker, regions: {"active_count": 0, "by_region": dict.fromkeys(regions, 0)},
    )
    checker = MagicMock()
    checker.get_all_regions_capacity.return_value = [
        _capacity("us-east-1", total=0, available=0),
        RegionCapacity(region="us-west-2"),
    ]

    result = _environment.gather_session_environment(None, multi_region_checker=checker)

    assert result is not None
    metrics = result["cluster_metrics"]
    assert (metrics["us-east-1"]["total_gpus"], metrics["us-east-1"]["available_gpus"]) == (0, 0)
    assert (metrics["us-west-2"]["total_gpus"], metrics["us-west-2"]["available_gpus"]) == (
        None,
        None,
    )
    assert _environment._gpu_count(True) is None  # a bool is no count
