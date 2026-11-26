"""Behaviour of ``FleetData``'s reads: what every dashboard page shows, from status to the console.

Each read is driven through the boundary ``cli.dashboard.data`` itself uses:
the signed API client (an autospec of ``SharedDiscoveryClient``, so a call
the real client would refuse fails the test), the CLI's managers and
trackers (autospecs returned from their patched factories), and boto3
clients (real CloudWatch and EC2 clients behind ``botocore.stub.Stubber``,
so every request is checked against the service model). The cases cover the
success paths, the fallbacks the dashboard promises (a Region that fails on
its own, missing or null fields, an optional read that fails), paging, and
the streaming chat. Nothing reaches AWS or the network.
"""

from __future__ import annotations

import copy
import logging
import time
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call, create_autospec

import pytest
import requests
from botocore.exceptions import ClientError

import cli.status
from cli.aws_client import ApiEndpoint, APIRequestError, RegionalApiDiscoveryError
from cli.capacity.checker import CapacityChecker, SpotPlacementConfigLimitError
from cli.capacity.models import InstanceTypeInfo, SpotPriceInfo
from cli.config import GCOConfig
from cli.cost_analytics import AthenaQueryError, CostAnalytics, QueryResult
from cli.costs import CostDataUnavailableError, CostSummary, CostTracker, ResourceCost
from cli.dashboard import data
from cli.dashboard.data import (
    CONSOLE_ROUTES,
    DashboardError,
    FleetData,
    JobsSnapshot,
    RegionRead,
    UtilizationRows,
)
from cli.inference import InferenceManager
from cli.jobs import JobManager
from cli.status import Finding, FleetStatus, Section
from tests.test_dashboard_data import (
    NOW,
    api_fixture,  # noqa: F401
    by_region,
    caused_by,
    client_error,
    fleet_fixture,  # noqa: F401
    freeze_now,
    http_response,
    k8s_job,
    patch_client,
    patch_factory,
    patch_session,
    regional_api_fixture,  # noqa: F401
    regional_fleet_fixture,  # noqa: F401
)

#: The hints ``describe_error`` gives, as an operator reads them after a failure.
EXPIRED = "Your session has probably expired: sign in again and refresh."
DENIED = "The signed-in principal lacks a permission this view needs."

# ---------------------------------------------------------------------------
# Fleet status and per-Region API reads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "costs", "policy"),
    [({}, False, False), ({"with_costs": True, "with_policy": True}, True, True)],
    ids=["defaults", "costs-and-policy"],
)
def test_fleet_status_is_the_gco_status_document_as_plain_data(
    monkeypatch: pytest.MonkeyPatch,
    fleet: FleetData,
    kwargs: dict[str, bool],
    costs: bool,
    policy: bool,
) -> None:
    document = FleetStatus(
        generated_at=NOW.isoformat(),
        project_name="gco",
        overall="degraded",
        degraded=["queue"],
        findings=[Finding(severity="warning", section="queue", message="us-west-2 DLQ holds 1")],
        sections={
            "queue": Section(
                name="queue", status="ok", data={"by_region": {"us-west-2": {"dlq": 1}}}
            )
        },
    )
    gather = create_autospec(cli.status.gather_fleet_status, return_value=document)
    monkeypatch.setattr(cli.status, "gather_fleet_status", gather)
    assert fleet.fleet_status(**kwargs) == {
        "generated_at": NOW.isoformat(),
        "project_name": "gco",
        "overall": "degraded",
        "degraded": ["queue"],
        "findings": [
            {"severity": "warning", "section": "queue", "message": "us-west-2 DLQ holds 1"}
        ],
        "sections": {
            "queue": {
                "name": "queue",
                "status": "ok",
                "data": {"by_region": {"us-west-2": {"dlq": 1}}},
                "reason": None,
                "errors": [],
            }
        },
    }
    gather.assert_called_once_with(fleet.config, with_costs=costs, with_policy=policy)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("cluster_metrics", "/api/v1/metrics"),
        ("cost_monitors", "/api/v1/cost/status"),
        ("regional_policy", "/api/v1/policy"),
        ("regional_status", "/api/v1/status"),
    ],
)
def test_per_region_reads_ask_each_bridge_once_and_keep_failures_to_that_region(
    fleet: FleetData, api: Any, method: str, path: str
) -> None:
    def answer(verb: str, route: str, region: str | None = None, **kwargs: Any) -> dict[str, Any]:
        if region == "us-west-2":
            raise APIRequestError(502, "Bad Gateway")
        return {"region": region, "status": "healthy"}

    api.call_api.side_effect = answer
    reads = getattr(fleet, method)(["us-east-1", "us-west-2"])
    assert reads == [
        RegionRead("us-east-1", {"region": "us-east-1", "status": "healthy"}),
        RegionRead("us-west-2", error="API request failed: Bad Gateway (HTTP 502)"),
    ]
    assert by_region(api.call_api) == [
        call("GET", path, region="us-east-1", max_attempts=1),
        call("GET", path, region="us-west-2", max_attempts=1),
    ]


# ---------------------------------------------------------------------------
# Utilization history (CloudWatch GetMetricData)
# ---------------------------------------------------------------------------

_SERIES = [
    ("cpu", "ClusterCpuUtilization"),
    ("memory", "ClusterMemoryUtilization"),
    ("gpu", "ClusterGpuUtilization"),
    ("active_jobs", "ActiveJobs"),
]


def metric_request(
    region: str, *, hours: int = 3, period: int = 60, token: str | None = None
) -> dict[str, Any]:
    """The GetMetricData request for one Region's health-monitor series."""
    request: dict[str, Any] = {
        "MetricDataQueries": [
            {
                "Id": metric,
                "MetricStat": {
                    "Metric": {
                        "Namespace": "GCO/HealthMonitor",
                        "MetricName": name,
                        "Dimensions": [
                            {"Name": "ClusterName", "Value": f"gco-{region}"},
                            {"Name": "Region", "Value": region},
                        ],
                    },
                    "Period": period,
                    "Stat": "Average",
                },
            }
            for metric, name in _SERIES
        ],
        "StartTime": NOW - timedelta(hours=hours),
        "EndTime": NOW,
        "ScanBy": "TimestampAscending",
    }
    if token:
        request["NextToken"] = token
    return request


class TestUtilizationHistory:
    def test_every_page_of_every_region_becomes_rows(
        self, monkeypatch: pytest.MonkeyPatch, fleet: FleetData
    ) -> None:
        freeze_now(monkeypatch)
        session = patch_session(monkeypatch, fleet)
        early, late = NOW - timedelta(minutes=10), NOW - timedelta(minutes=5)
        east = session.stub("cloudwatch", "us-east-1")
        east.add_response(
            "get_metric_data",
            {
                "MetricDataResults": [
                    {"Id": "cpu", "Timestamps": [early, late], "Values": [12.5, 14]},
                    {"Id": "gpu", "Timestamps": [early], "Values": [50]},
                ],
                "NextToken": "page-2",
            },
            metric_request("us-east-1", hours=6, period=300),
        )
        east.add_response(
            "get_metric_data",
            {
                "MetricDataResults": [
                    {"Id": "memory", "Timestamps": [late], "Values": [40.0]},
                    {"Id": "active_jobs", "StatusCode": "Complete"},
                ]
            },
            metric_request("us-east-1", hours=6, period=300, token="page-2"),
        )
        session.stub("cloudwatch", "us-west-2").add_response(
            "get_metric_data", {}, metric_request("us-west-2", hours=6, period=300)
        )
        rows = fleet.utilization_history(["us-east-1", "us-west-2"], hours=6, period=300)
        assert rows == [
            {"region": "us-east-1", "metric": "cpu", "timestamp": early, "value": 12.5},
            {"region": "us-east-1", "metric": "cpu", "timestamp": late, "value": 14.0},
            {"region": "us-east-1", "metric": "gpu", "timestamp": early, "value": 50.0},
            {"region": "us-east-1", "metric": "memory", "timestamp": late, "value": 40.0},
        ]
        assert all(isinstance(row["value"], float) for row in rows)
        assert isinstance(rows, UtilizationRows)
        assert rows.errors == {}
        session.assert_no_pending_responses()

    def test_a_region_that_fails_is_named_beside_the_samples_of_the_others(
        self, monkeypatch: pytest.MonkeyPatch, fleet: FleetData
    ) -> None:
        freeze_now(monkeypatch)
        session = patch_session(monkeypatch, fleet)
        session.stub("cloudwatch", "us-east-1").add_response(
            "get_metric_data",
            {"MetricDataResults": [{"Id": "cpu", "Timestamps": [NOW], "Values": [3.0]}]},
            metric_request("us-east-1"),
        )
        session.stub("cloudwatch", "us-west-2").add_client_error(
            "get_metric_data",
            service_error_code="AccessDenied",
            service_message="not authorized to perform: cloudwatch:GetMetricData",
            http_status_code=403,
        )
        rows = fleet.utilization_history(["us-east-1", "us-west-2"])
        assert rows == [{"region": "us-east-1", "metric": "cpu", "timestamp": NOW, "value": 3.0}]
        assert rows.errors == {
            "us-west-2": "AWS denied the request: not authorized to perform: "
            f"cloudwatch:GetMetricData. {DENIED}"
        }

    def test_no_samples_and_failures_is_one_error_naming_regions_that_failed_alike_once(
        self, monkeypatch: pytest.MonkeyPatch, fleet: FleetData
    ) -> None:
        freeze_now(monkeypatch)
        session = patch_session(monkeypatch, fleet)
        for region in ("us-east-1", "us-west-2"):
            session.stub("cloudwatch", region).add_client_error(
                "get_metric_data",
                service_error_code="ExpiredToken",
                service_message="The security token included in the request is expired",
                http_status_code=403,
            )
        session.stub("cloudwatch", "eu-west-1").add_client_error(
            "get_metric_data",
            service_error_code="Throttling",
            service_message="Rate exceeded",
            http_status_code=400,
        )
        # A Region that answers without samples has not failed.
        session.stub("cloudwatch", "ap-south-1").add_response(
            "get_metric_data", {}, metric_request("ap-south-1")
        )
        with pytest.raises(DashboardError) as raised:
            fleet.utilization_history(["us-east-1", "us-west-2", "eu-west-1", "ap-south-1"])
        assert str(raised.value) == (
            "Utilization history is unavailable: "
            f"us-east-1, us-west-2: AWS rejected the credentials (ExpiredToken). {EXPIRED}; "
            "eu-west-1: Throttling: Rate exceeded"
        )
        assert raised.value.hint is None

    def test_no_regions_is_no_rows(self, monkeypatch: pytest.MonkeyPatch, fleet: FleetData) -> None:
        patch_session(monkeypatch, fleet)
        rows = fleet.utilization_history([])
        assert (rows, rows.errors) == ([], {})


# ---------------------------------------------------------------------------
# Kubernetes Jobs
# ---------------------------------------------------------------------------


class TestJobs:
    def test_global_mode_reads_the_cross_region_aggregator(
        self, fleet: FleetData, api: Any
    ) -> None:
        api.get_global_jobs.return_value = {
            "total": 7,
            "count": 3,
            "limit": 1000,
            "regions_queried": 3,
            "regions_successful": 2,
            "jobs": [
                k8s_job("train-a", region="us-east-1", active=1),
                k8s_job("eval-b", region="us-west-2", succeeded=1, complete=True),
                k8s_job("orphan"),
                "junk",
            ],
            "errors": [{"region": "eu-west-1", "error": "Regional request failed"}],
        }
        snapshot = fleet.jobs(["us-east-1", "us-west-2", "eu-west-1"], limit=5000)
        assert [(job.name, job.region, job.status, job.uid) for job in snapshot.jobs] == [
            ("train-a", "us-east-1", "running", "uid-train-a"),
            ("eval-b", "us-west-2", "succeeded", "uid-eval-b"),
            ("orphan", "", "pending", "uid-orphan"),
        ]
        assert (snapshot.errors, snapshot.regions_queried, snapshot.total, snapshot.source) == (
            {"eu-west-1": "Regional request failed"},
            3,
            7,
            "global",
        )
        api.get_global_jobs.assert_called_once_with(limit=1000)
        api.get_jobs.assert_not_called()

    @pytest.mark.parametrize(
        ("answer", "total"),
        [
            ({"jobs": [k8s_job("a", region="us-east-1"), k8s_job("b", region="us-east-1")]}, 2),
            ({"jobs": [], "total": None, "regions_queried": None, "errors": None}, 0),
        ],
        ids=["counts-missing", "counts-null"],
    )
    def test_missing_aggregator_counts_fall_back(
        self, fleet: FleetData, api: Any, answer: dict[str, Any], total: int
    ) -> None:
        api.get_global_jobs.return_value = answer
        snapshot = fleet.jobs(["us-east-1"])
        assert (snapshot.total, snapshot.regions_queried, snapshot.errors) == (total, 0, {})
        api.get_global_jobs.assert_called_once_with(limit=200)

    def test_regional_mode_reads_each_bridge_and_keeps_its_failures(
        self, regional_fleet: FleetData, regional_api: Any
    ) -> None:
        def answer(region: str | None = None, **kwargs: Any) -> dict[str, Any]:
            if region == "us-west-2":
                raise requests.HTTPError(
                    "503 Server Error",
                    response=http_response(503, {"detail": "bridge unavailable"}),
                )
            if region == "eu-west-1":
                return {"jobs": [], "total": None}
            return {"jobs": [k8s_job("train-a", active=1), "junk"], "total": 4}

        regional_api.get_jobs.side_effect = answer
        snapshot = regional_fleet.jobs(["us-east-1", "us-west-2", "eu-west-1"], limit=5000)
        assert [(job.name, job.region, job.status) for job in snapshot.jobs] == [
            ("train-a", "us-east-1", "running")
        ]
        assert snapshot.errors == {"us-west-2": "The API answered HTTP 503: bridge unavailable"}
        assert (snapshot.regions_queried, snapshot.total, snapshot.source) == (3, 4, "regional")
        assert by_region(regional_api.get_jobs) == [
            call(region="eu-west-1", limit=1000),
            call(region="us-east-1", limit=1000),
            call(region="us-west-2", limit=1000),
        ]
        regional_api.get_global_jobs.assert_not_called()

    def test_a_null_job_list_from_the_aggregator_reads_as_no_jobs(
        self, fleet: FleetData, api: Any
    ) -> None:
        api.get_global_jobs.return_value = {
            "jobs": None,
            "total": 0,
            "regions_queried": 3,
            "errors": None,
        }
        assert fleet.jobs(["us-east-1"]) == JobsSnapshot([], {}, 3, 0, "global")

    def test_a_null_job_list_from_a_bridge_reads_as_no_jobs_there(
        self, regional_fleet: FleetData, regional_api: Any
    ) -> None:
        def answer(region: str | None = None, **kwargs: Any) -> dict[str, Any]:
            if region == "us-east-1":
                return {"jobs": None, "total": None}
            return {"jobs": [k8s_job("train-a", active=1)], "total": 1}

        regional_api.get_jobs.side_effect = answer
        snapshot = regional_fleet.jobs(["us-east-1", "us-west-2"])
        assert [(job.name, job.region) for job in snapshot.jobs] == [("train-a", "us-west-2")]
        assert (snapshot.errors, snapshot.total) == ({}, 1)

    def test_no_regions_reads_nothing(self, fleet: FleetData, api: Any) -> None:
        assert fleet.jobs([]) == JobsSnapshot([], {}, 0, 0, "regional")
        api.get_global_jobs.assert_not_called()
        api.get_jobs.assert_not_called()

    @pytest.mark.parametrize(
        ("method", "client_method", "expected"),
        [
            ("job_detail", "get_job_details", call("trainer", "gco-jobs", region="us-west-2")),
            ("job_pods", "get_job_pods", call("trainer", "gco-jobs", "us-west-2")),
            ("job_events", "get_job_events", call("trainer", "gco-jobs", "us-west-2")),
            ("job_metrics", "get_job_metrics", call("trainer", "gco-jobs", "us-west-2")),
        ],
    )
    def test_one_jobs_reads_ask_the_region_it_runs_in(
        self, fleet: FleetData, api: Any, method: str, client_method: str, expected: Any
    ) -> None:
        answer = {"job_name": "trainer", "namespace": "gco-jobs", "region": "us-west-2"}
        getattr(api, client_method).return_value = answer
        assert getattr(fleet, method)("trainer", "gco-jobs", "us-west-2") == answer
        assert getattr(api, client_method).call_args_list == [expected]

    @pytest.mark.parametrize(
        ("kwargs", "tail", "container"),
        [({}, 200, None), ({"tail": 50, "container": "sidecar"}, 50, "sidecar")],
        ids=["defaults", "tail-and-container"],
    )
    def test_job_logs_use_the_cli_job_manager(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fleet: FleetData,
        kwargs: dict[str, Any],
        tail: int,
        container: str | None,
    ) -> None:
        manager, factory = patch_factory(
            monkeypatch, "cli.dashboard.data.get_job_manager", JobManager
        )
        manager.get_job_logs.return_value = "epoch 1 loss 0.42\n"
        assert fleet.job_logs("trainer", "gco-jobs", "us-east-1", **kwargs) == "epoch 1 loss 0.42\n"
        factory.assert_called_once_with(fleet.config)
        manager.get_job_logs.assert_called_once_with(
            "trainer", "gco-jobs", region="us-east-1", tail_lines=tail, container=container
        )


# ---------------------------------------------------------------------------
# Central job queue reads
# ---------------------------------------------------------------------------


class TestQueueReads:
    def test_queue_jobs_sends_only_the_filters_given_and_caps_the_page(
        self, fleet: FleetData, api: Any
    ) -> None:
        api.call_api.return_value = {"jobs": [], "count": 0}
        assert fleet.queue_jobs() == {"jobs": [], "count": 0}
        fleet.queue_jobs(status="queued", target_region="us-west-2", namespace="ml", limit=5000)
        assert api.call_api.call_args_list == [
            call("GET", "/api/v1/queue/jobs", region=None, params={"limit": "200"}),
            call(
                "GET",
                "/api/v1/queue/jobs",
                region=None,
                params={
                    "limit": "1000",
                    "status": "queued",
                    "target_region": "us-west-2",
                    "namespace": "ml",
                },
            ),
        ]

    @pytest.mark.parametrize(
        ("answer", "expected"),
        [
            ({"job": {"job_id": "q-1", "status": "queued"}}, {"job_id": "q-1", "status": "queued"}),
            ({"job": None}, {}),
            ({"job": ["q-1"]}, {}),
            ({}, {}),
        ],
        ids=["record", "null", "not-an-object", "missing"],
    )
    def test_queue_job_returns_the_record_or_nothing(
        self, fleet: FleetData, api: Any, answer: dict[str, Any], expected: dict[str, Any]
    ) -> None:
        api.call_api.return_value = answer
        assert fleet.queue_job("q/1 2") == expected
        api.call_api.assert_called_once_with("GET", "/api/v1/queue/jobs/q%2F1%202", region=None)

    def test_queue_job_refuses_an_id_that_would_climb_the_path(
        self, fleet: FleetData, api: Any
    ) -> None:
        with pytest.raises(ValueError, match="cannot be used as a name or ID"):
            fleet.queue_job("..")
        api.call_api.assert_not_called()

    def test_queue_stats_rides_the_global_api(self, fleet: FleetData, api: Any) -> None:
        api.call_api.return_value = {"by_region": {"us-east-1": {"queued": 2}}}
        assert fleet.queue_stats() == {"by_region": {"us-east-1": {"queued": 2}}}
        api.call_api.assert_called_once_with("GET", "/api/v1/queue/stats", region=None)

    def test_regional_mode_asks_the_default_regions_bridge(
        self, regional_fleet: FleetData, regional_api: Any
    ) -> None:
        regional_api.call_api.return_value = {"jobs": []}
        regional_fleet.queue_stats()
        regional_fleet.queue_jobs()
        regional_fleet.queue_job("q-1")
        assert [made.kwargs["region"] for made in regional_api.call_api.call_args_list] == [
            "us-east-1"
        ] * 3


# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------


def _spot_request(days: int, token: str | None = None) -> dict[str, Any]:
    request: dict[str, Any] = {
        "InstanceTypes": ["g5.xlarge"],
        "ProductDescriptions": ["Linux/UNIX"],
        "StartTime": NOW - timedelta(days=days),
        "EndTime": NOW,
    }
    if token:
        request["NextToken"] = token
    return request


def _spot_item(zone: str, price: str, stamp: Any) -> dict[str, Any]:
    return {
        "AvailabilityZone": zone,
        "InstanceType": "g5.xlarge",
        "ProductDescription": "Linux/UNIX",
        "SpotPrice": price,
        "Timestamp": stamp,
    }


class TestSpotPriceHistory:
    def test_every_page_of_price_changes_becomes_rows(
        self, monkeypatch: pytest.MonkeyPatch, fleet: FleetData
    ) -> None:
        freeze_now(monkeypatch)
        session = patch_session(monkeypatch, fleet)
        first, second = NOW - timedelta(hours=2), NOW - timedelta(hours=1)
        ec2 = session.stub("ec2", "us-west-2")
        ec2.add_response(
            "describe_spot_price_history",
            {"SpotPriceHistory": [_spot_item("us-west-2a", "0.412300", first)], "NextToken": "p2"},
            _spot_request(3),
        )
        ec2.add_response(
            "describe_spot_price_history",
            {"SpotPriceHistory": [_spot_item("us-west-2b", "0.398", second)]},
            _spot_request(3, "p2"),
        )
        assert fleet.spot_price_history("g5.xlarge", "us-west-2", days=3) == [
            {
                "region": "us-west-2",
                "availability_zone": "us-west-2a",
                "timestamp": first,
                "price": 0.4123,
            },
            {
                "region": "us-west-2",
                "availability_zone": "us-west-2b",
                "timestamp": second,
                "price": 0.398,
            },
        ]
        session.assert_no_pending_responses()

    def test_a_type_without_spot_history_is_an_empty_list(
        self, monkeypatch: pytest.MonkeyPatch, fleet: FleetData
    ) -> None:
        freeze_now(monkeypatch)
        session = patch_session(monkeypatch, fleet)
        session.stub("ec2", "us-east-1").add_response(
            "describe_spot_price_history", {}, _spot_request(7)
        )
        assert fleet.spot_price_history("g5.xlarge", "us-east-1") == []


@pytest.fixture(name="checker")
def checker_fixture(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, MagicMock]:
    return patch_factory(monkeypatch, "cli.capacity.get_capacity_checker", CapacityChecker)


def test_instance_report_describes_the_type_in_every_region(
    fleet: FleetData, checker: tuple[Any, MagicMock]
) -> None:
    capacity, factory = checker
    offered = {"us-east-1": True, "us-west-2": True, "eu-west-1": False}

    def available(instance_type: str, region: str) -> bool:
        if region == "ap-south-1":
            raise client_error(
                "UnauthorizedOperation",
                "You are not authorized to perform: ec2:DescribeInstanceTypeOfferings",
                "DescribeInstanceTypeOfferings",
            )
        return offered[region]

    def info(instance_type: str, region: str | None = None) -> InstanceTypeInfo | None:
        if region != "us-east-1":
            return None
        return InstanceTypeInfo(
            instance_type=instance_type,
            vcpus=4,
            memory_gib=16.0,
            gpu_count=1,
            gpu_type="A10G",
            gpu_memory_gib=24.0,
            architecture="x86_64",
            region=region,
        )

    def spot(instance_type: str, region: str, days: int = 7) -> list[SpotPriceInfo]:
        if region != "us-east-1":
            return []
        return [
            SpotPriceInfo(instance_type, "us-east-1a", 0.45, 0.47, 0.40, 0.52, 0.93),
            SpotPriceInfo(instance_type, "us-east-1b", 0.41, 0.43, 0.39, 0.50, 0.88),
        ]

    capacity.check_instance_available_in_region.side_effect = available
    capacity.get_instance_info.side_effect = info
    capacity.get_on_demand_price.side_effect = lambda instance_type, region: (
        1.006 if region == "us-east-1" else None
    )
    capacity.get_spot_price_history.side_effect = spot
    reads = fleet.instance_report(
        "g5.xlarge", ["us-east-1", "us-west-2", "eu-west-1", "ap-south-1"]
    )
    assert reads == [
        RegionRead(
            "us-east-1",
            {
                "offered": True,
                "vcpus": 4,
                "memory_gib": 16.0,
                "gpu_count": 1,
                "gpu_type": "A10G",
                "gpu_memory_gib": 24.0,
                "architecture": "x86_64",
                "on_demand_price": 1.006,
                "spot_price": 0.41,
                "spot_stability": 0.88,
                "spot_zones": 2,
            },
        ),
        RegionRead("us-west-2", {"offered": True, "on_demand_price": None}),
        RegionRead("eu-west-1", {"offered": False}),
        RegionRead(
            "ap-south-1",
            error="AWS denied the request: You are not authorized to perform: "
            f"ec2:DescribeInstanceTypeOfferings. {DENIED}",
        ),
    ]
    # A type the Region does not offer is never described or priced there.
    assert sorted(made.args[1] for made in capacity.get_on_demand_price.call_args_list) == [
        "us-east-1",
        "us-west-2",
    ]
    assert all(made.args == (fleet.config,) for made in factory.call_args_list)


class TestPlacementScore:
    @pytest.mark.parametrize(
        ("scores", "expected"),
        [
            ({"regional": 7, "use1-az1": 9}, 7),
            ({"regional": 6.0}, 6),
            ({}, None),
            ({"regional": "7"}, None),
        ],
        ids=["int", "float", "no-score", "not-a-number"],
    )
    def test_the_score_is_the_regional_one(
        self,
        fleet: FleetData,
        checker: tuple[Any, MagicMock],
        scores: dict[str, Any],
        expected: int | None,
    ) -> None:
        capacity, factory = checker
        capacity.get_spot_placement_score.return_value = scores
        assert fleet.placement_score("g5.xlarge", "us-east-1", capacity=4) == expected
        capacity.get_spot_placement_score.assert_called_once_with(
            "g5.xlarge", "us-east-1", target_capacity=4
        )
        factory.assert_called_once_with(fleet.config)

    def test_the_daily_query_limit_is_an_error_with_a_hint(
        self, fleet: FleetData, checker: tuple[Any, MagicMock]
    ) -> None:
        capacity, _ = checker
        refusal = SpotPlacementConfigLimitError(
            "Spot Placement Score request for pool g5 in us-east-1 was refused"
        )
        capacity.get_spot_placement_score.side_effect = refusal
        with pytest.raises(DashboardError, match="was refused") as raised:
            fleet.placement_score("g5.xlarge", "us-east-1")
        assert raised.value.hint == "AWS limits distinct placement-score queries per day."
        assert raised.value.__cause__ is refusal
        capacity.get_spot_placement_score.assert_called_once_with(
            "g5.xlarge", "us-east-1", target_capacity=1
        )


def _job_manifest(*, gpus: int = 0, labels: dict[str, str] | None = None) -> dict[str, Any]:
    resources = {"limits": {"nvidia.com/gpu": gpus}} if gpus else {}
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "trainer", "labels": labels or {}},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": "main", "image": "busybox:1.38.0", "resources": resources}
                    ]
                }
            }
        },
    }


@pytest.mark.parametrize(
    ("manifests", "kwargs", "expected"),
    [
        (
            [_job_manifest(gpus=2)],
            {},
            {"gpu_required": True, "gpu_count": 2, "instance_type": None, "backend": "eks"},
        ),
        (
            [_job_manifest(gpus=2)],
            {"gpu_count": 0, "instance_type": "", "backend": ""},
            {"gpu_required": False, "gpu_count": 0, "instance_type": None, "backend": "eks"},
        ),
        (
            [_job_manifest(labels={"kueue.x-k8s.io/queue-name": "gco-hyperpod"})],
            {},
            {"gpu_required": False, "gpu_count": 0, "instance_type": None, "backend": "hyperpod"},
        ),
        (
            (),
            {"instance_type": "p5.48xlarge", "gpu_count": 8, "backend": "pcs"},
            {
                "gpu_required": True,
                "gpu_count": 8,
                "instance_type": "p5.48xlarge",
                "backend": "pcs",
            },
        ),
    ],
    ids=["from-the-manifest", "explicit-zero", "hyperpod-queue", "explicit-everything"],
)
def test_recommend_region_reads_gpus_and_backend_from_the_manifests(
    fleet: FleetData,
    checker: tuple[Any, MagicMock],
    manifests: Any,
    kwargs: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    capacity, factory = checker
    capacity.recommend_region_for_job.return_value = {"region": "us-west-2", "score": 0.91}
    assert fleet.recommend_region(manifests, **kwargs) == {"region": "us-west-2", "score": 0.91}
    capacity.recommend_region_for_job.assert_called_once_with(**expected)
    factory.assert_called_once_with(fleet.config)


# ---------------------------------------------------------------------------
# Inference: registry reads, health probes, the served model, chat
# ---------------------------------------------------------------------------

_ENDPOINTS: dict[str, dict[str, Any]] = {
    "llm": {
        "endpoint_name": "llm",
        "desired_state": "running",
        "ingress_path": "/inference/llm",
        "namespace": "gco-inference",
        "target_regions": ["us-east-1", "us-west-2"],
        "spec": {
            "image": "vllm/vllm-openai:v0.11.0",
            "port": 8000,
            "replicas": 1,
            "gpu_count": 1,
            "health_check_path": "/healthz",
            "env": {"MODEL": "example-org/example-model"},
        },
    },
    "bare": {"endpoint_name": "bare", "desired_state": "running", "spec": None},
    "climber": {"endpoint_name": "climber", "spec": {"health_check_path": "../admin"}},
}


@pytest.fixture(name="inference")
def inference_fixture(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, MagicMock]:
    """The inference registry: three endpoints; any other name does not exist."""
    manager, factory = patch_factory(
        monkeypatch, "cli.inference.get_inference_manager", InferenceManager
    )
    manager.get_endpoint.side_effect = lambda name: copy.deepcopy(_ENDPOINTS.get(name))
    manager.list_endpoints.return_value = [copy.deepcopy(_ENDPOINTS["llm"])]
    return manager, factory


def test_endpoints_and_endpoint_read_the_inference_registry(
    fleet: FleetData, inference: tuple[Any, MagicMock]
) -> None:
    manager, factory = inference
    assert fleet.endpoints() == [_ENDPOINTS["llm"]]
    assert fleet.endpoint("bare") == _ENDPOINTS["bare"]
    assert fleet.endpoint("ghost") is None
    manager.list_endpoints.assert_called_once_with()
    assert manager.get_endpoint.call_args_list == [call("bare"), call("ghost")]
    assert factory.call_args_list == [call(fleet.config)] * 3


class TestEndpointHealth:
    def test_a_global_probe_resolves_the_global_api_first_then_times_one_attempt(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fleet: FleetData,
        api: Any,
        inference: tuple[Any, MagicMock],
    ) -> None:
        ticks = iter([10.0, 10.25])
        monkeypatch.setattr(
            data,
            "time",
            SimpleNamespace(perf_counter=lambda: next(ticks), monotonic=time.monotonic),
        )
        api.make_authenticated_request.return_value = http_response(200, {"status": "ok"})
        assert fleet.endpoint_health("llm", None) == {
            "region": "global",
            "healthy": True,
            "http_status": 200,
            "latency_ms": 250,
        }
        assert [made[0] for made in api.mock_calls] == [
            "get_api_endpoint",
            "make_authenticated_request",
        ]
        api.make_authenticated_request.assert_called_once_with(
            "GET", "/inference/llm/healthz", target_region=None, max_attempts=1
        )

    @pytest.mark.parametrize(
        ("regional", "region", "bridge"),
        [
            (False, "us-west-2", "us-west-2"),
            # Regional API mode has no global API: an unpinned probe rides the
            # default Region's bridge, and says so.
            (True, None, "us-east-1"),
            (True, "eu-west-1", "eu-west-1"),
        ],
        ids=["pinned", "regional-default", "regional-pinned"],
    )
    def test_a_probe_through_a_bridge_resolves_that_bridge_first_and_names_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
        inference: tuple[Any, MagicMock],
        regional: bool,
        region: str | None,
        bridge: str,
    ) -> None:
        fleet = FleetData(GCOConfig(use_regional_api=regional))
        api = patch_client(monkeypatch, fleet)
        api.make_authenticated_request.return_value = http_response(503, {"detail": "warming up"})
        result = fleet.endpoint_health("bare", region)
        assert (result["region"], result["healthy"], result["http_status"]) == (bridge, False, 503)
        assert result["error"] == "warming up"
        assert isinstance(result["latency_ms"], int) and result["latency_ms"] >= 0
        api.get_regional_api_endpoint.assert_called_once_with(bridge)
        api.get_api_endpoint.assert_not_called()
        api.make_authenticated_request.assert_called_once_with(
            "GET", "/inference/bare/health", target_region=bridge, max_attempts=1
        )

    @pytest.mark.parametrize(
        ("status", "body", "reason", "error"),
        [
            (
                403,
                {"message": "The security token included in the request is expired"},
                None,
                f"The security token included in the request is expired. {EXPIRED}",
            ),
            (
                # API Gateway capitalises the key when IAM denies the caller.
                403,
                {"Message": "User: tester is not authorized to perform: execute-api:Invoke"},
                None,
                f"User: tester is not authorized to perform: execute-api:Invoke. {DENIED}",
            ),
            # The model server's own refusal says what failed and nothing more.
            (404, {"detail": "Not Found"}, None, "Not Found"),
            (502, b"", "Bad Gateway", "Bad Gateway"),
        ],
        ids=["expired-session", "no-invoke-permission", "model-server", "no-body"],
    )
    def test_a_refused_probe_says_why_and_what_to_do(
        self,
        fleet: FleetData,
        api: Any,
        inference: tuple[Any, MagicMock],
        status: int,
        body: object,
        reason: str | None,
        error: str,
    ) -> None:
        api.make_authenticated_request.return_value = http_response(status, body, reason=reason)
        result = fleet.endpoint_health("llm", "us-east-1")
        assert (result["healthy"], result["http_status"], result["error"]) == (
            False,
            status,
            error,
        )
        # The Health table reads every probe, the global one included, the same way.
        assert fleet.endpoint_health_all("llm", ["us-east-1"])[0]["error"] == error

    def test_a_health_path_that_would_leave_the_endpoint_is_refused(
        self, fleet: FleetData, api: Any, inference: tuple[Any, MagicMock]
    ) -> None:
        with pytest.raises(ValueError, match="must start with '/'"):
            fleet.endpoint_health("climber", None)
        api.make_authenticated_request.assert_not_called()

    def test_an_unknown_endpoint_is_an_error_before_any_request(
        self, fleet: FleetData, api: Any, inference: tuple[Any, MagicMock]
    ) -> None:
        with pytest.raises(DashboardError, match=r"^Endpoint 'ghost' does not exist\.$"):
            fleet.endpoint_health("ghost", "us-east-1")
        assert api.mock_calls == []

    def test_health_all_probes_global_and_every_region_and_keeps_each_failure_and_hint(
        self, fleet: FleetData, api: Any, inference: tuple[Any, MagicMock]
    ) -> None:
        unreadable = caused_by(
            RegionalApiDiscoveryError(
                "Regional API endpoint discovery could not run for stack "
                "'gco-regional-api-us-west-2' in us-west-2."
            ),
            client_error("ExpiredToken", "The security token included in the request is expired"),
        )

        def resolve(region: str, force_refresh: bool = False) -> ApiEndpoint:
            if region == "us-west-2":
                raise unreadable
            return ApiEndpoint(url=f"https://{region}.example.com/prod", region=region, api_id="a1")

        api.get_regional_api_endpoint.side_effect = resolve
        api.make_authenticated_request.side_effect = lambda *args, **kwargs: http_response(
            200, b"ok"
        )
        results = fleet.endpoint_health_all("llm", ["us-east-1", "us-west-2"])
        assert [(r["region"], r["healthy"], r["http_status"]) for r in results] == [
            ("global", True, 200),
            ("us-east-1", True, 200),
            ("us-west-2", False, None),
        ]
        assert results[2] == {
            "region": "us-west-2",
            "healthy": False,
            "http_status": None,
            "latency_ms": None,
            "error": f"{unreadable} {EXPIRED}",
        }
        assert sorted(
            str(made.kwargs["target_region"])
            for made in api.make_authenticated_request.call_args_list
        ) == ["None", "us-east-1"]

    def test_regional_mode_has_no_global_api_so_only_the_regions_are_probed(
        self, regional_fleet: FleetData, regional_api: Any, inference: tuple[Any, MagicMock]
    ) -> None:
        def answer(*args: Any, target_region: str | None = None, **kwargs: Any) -> Any:
            if target_region == "us-west-2":
                raise requests.ConnectTimeout("connect timed out")
            return http_response(200, b"ok")

        regional_api.make_authenticated_request.side_effect = answer
        results = regional_fleet.endpoint_health_all("llm", ["us-east-1", "us-west-2"])
        assert [(r["region"], r["healthy"], r.get("error")) for r in results] == [
            ("us-east-1", True, None),
            ("us-west-2", False, "Could not reach the GCO API: ConnectTimeout"),
        ]
        regional_api.get_api_endpoint.assert_not_called()
        assert sorted(
            made.kwargs["target_region"]
            for made in regional_api.make_authenticated_request.call_args_list
        ) == ["us-east-1", "us-west-2"]

    def test_regional_mode_without_regions_probes_nothing(
        self, regional_fleet: FleetData, regional_api: Any, inference: tuple[Any, MagicMock]
    ) -> None:
        assert regional_fleet.endpoint_health_all("llm", []) == []
        assert regional_api.mock_calls == []


class TestServedModel:
    @pytest.mark.parametrize(
        ("status", "body", "expected"),
        [
            (
                200,
                {"object": "list", "data": [{"id": "example-org/served", "object": "model"}]},
                "example-org/served",
            ),
            (200, {"data": []}, "example-org/example-model"),
            (200, {"data": None}, "example-org/example-model"),
            (200, {"data": ["example-org/served"]}, "example-org/example-model"),
            (200, {"data": [{"object": "model"}]}, "example-org/example-model"),
            (404, {"detail": "Not Found"}, "example-org/example-model"),
            (200, b"<html>not json</html>", "example-org/example-model"),
            (200, [{"id": "example-org/served"}], "example-org/example-model"),
        ],
        ids=[
            "listed",
            "empty",
            "null",
            "not-objects",
            "no-id",
            "http-404",
            "not-json",
            "not-an-object",
        ],
    )
    def test_the_model_comes_from_v1_models_else_the_spec(
        self,
        fleet: FleetData,
        api: Any,
        inference: tuple[Any, MagicMock],
        status: int,
        body: object,
        expected: str,
    ) -> None:
        api.make_authenticated_request.return_value = http_response(status, body)
        assert fleet.served_model("llm", "us-west-2") == expected
        api.make_authenticated_request.assert_called_once_with(
            "GET", "/inference/llm/v1/models", target_region="us-west-2", max_attempts=1
        )

    def test_an_unreachable_endpoint_falls_back_to_its_name_and_logs_why(
        self,
        fleet: FleetData,
        api: Any,
        inference: tuple[Any, MagicMock],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        api.make_authenticated_request.side_effect = requests.ConnectionError("connection reset")
        with caplog.at_level(logging.DEBUG, logger="cli.dashboard.data"):
            assert fleet.served_model("bare") == "bare"
        assert "Model discovery for bare failed: connection reset" in caplog.text
        api.make_authenticated_request.assert_called_once_with(
            "GET", "/inference/bare/v1/models", target_region=None, max_attempts=1
        )

    def test_an_unknown_endpoint_is_an_error(
        self, fleet: FleetData, api: Any, inference: tuple[Any, MagicMock]
    ) -> None:
        with pytest.raises(DashboardError, match="does not exist"):
            fleet.served_model("ghost")
        api.make_authenticated_request.assert_not_called()


_SSE = (
    b": keep-alive\n\n"
    b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
    b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
    b"data: not-json\n\n"
    b"data: \xff\xfe\n\n"
    b'data: {"choices":null}\n\n'
    b'data: {"choices":[{"delta":null},{"delta":{"content":""}}]}\n\n'
    b'data:{"choices":[{"delta":{"content":"lo"}}]}\n\n'
    b"event: ping\n"
    b"data: [DONE]\n\n"
    b'data: {"choices":[{"delta":{"content":"after the end"}}]}\n\n'
)


class TestChat:
    def test_tokens_stream_from_the_server_sent_events_until_done(
        self, fleet: FleetData, api: Any, inference: tuple[Any, MagicMock]
    ) -> None:
        response = http_response(200, _SSE)
        api.make_authenticated_request.return_value = response
        messages = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Hi"}]
        tokens = list(
            fleet.chat(
                "llm",
                messages,
                model="example-org/served",
                max_tokens=64,
                temperature=0.2,
                region="us-west-2",
            )
        )
        assert tokens == ["Hel", "lo"]
        api.make_authenticated_request.assert_called_once_with(
            "POST",
            "/inference/llm/v1/chat/completions",
            body={
                "model": "example-org/served",
                "messages": messages,
                "max_tokens": 64,
                "temperature": 0.2,
                "stream": True,
            },
            target_region="us-west-2",
            stream=True,
        )
        assert response.close_calls == 1

    def test_a_stream_that_ends_without_done_still_yields_and_closes(
        self, fleet: FleetData, api: Any, inference: tuple[Any, MagicMock]
    ) -> None:
        response = http_response(200, b'data: {"choices":[{"delta":{"content":"caf\xc3\xa9"}}]}\n')
        api.make_authenticated_request.return_value = response
        assert list(fleet.chat("bare", [{"role": "user", "content": "Hi"}], model="m")) == [
            "caf\u00e9"
        ]
        sent = api.make_authenticated_request.call_args
        assert sent.args == ("POST", "/inference/bare/v1/chat/completions")
        assert sent.kwargs["body"]["max_tokens"] == 256
        assert sent.kwargs["body"]["temperature"] == 0.7
        assert sent.kwargs["target_region"] is None
        assert response.close_calls == 1

    @pytest.mark.parametrize(
        ("status", "message", "hint"),
        [
            (429, "Rate limit exceeded", None),
            (403, "The security token included in the request is expired", EXPIRED),
        ],
        ids=["throttled", "expired-session"],
    )
    def test_an_http_error_is_a_dashboard_error_and_the_stream_is_closed(
        self,
        fleet: FleetData,
        api: Any,
        inference: tuple[Any, MagicMock],
        status: int,
        message: str,
        hint: str | None,
    ) -> None:
        response = http_response(status, {"message": message})
        api.make_authenticated_request.return_value = response
        with pytest.raises(DashboardError) as raised:
            list(fleet.chat("llm", [{"role": "user", "content": "Hi"}], model="m"))
        assert str(raised.value) == f"The endpoint answered HTTP {status}: {message}"
        assert raised.value.hint == hint
        assert response.close_calls == 1

    def test_json_that_is_not_an_object_is_skipped_like_an_unparseable_line(
        self, fleet: FleetData, api: Any, inference: tuple[Any, MagicMock]
    ) -> None:
        response = http_response(
            200,
            b"data: [1, 2]\n\n"
            b'data: "a bare string"\n\n'
            b"data: 42\n\n"
            b"data: null\n\n"
            b'data: {"choices": "not a list"}\n\n'
            b'data: {"choices": ["not an object", {"delta": "not an object"}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"still here"}}]}\n\n'
            b"data: [DONE]\n\n",
        )
        api.make_authenticated_request.return_value = response
        tokens = list(fleet.chat("llm", [{"role": "user", "content": "Hi"}], model="m"))
        assert tokens == ["still here"]
        assert response.close_calls == 1

    def test_a_reader_that_stops_early_closes_the_stream(
        self, fleet: FleetData, api: Any, inference: tuple[Any, MagicMock]
    ) -> None:
        response = http_response(200, _SSE)
        api.make_authenticated_request.return_value = response
        stream = fleet.chat("llm", [{"role": "user", "content": "Hi"}], model="m")
        assert next(stream) == "Hel"
        assert response.close_calls == 0
        stream.close()
        assert response.close_calls == 1

    def test_an_unknown_endpoint_fails_on_the_first_read(
        self, fleet: FleetData, api: Any, inference: tuple[Any, MagicMock]
    ) -> None:
        stream = fleet.chat("ghost", [], model="m")
        with pytest.raises(DashboardError, match="does not exist"):
            next(stream)
        api.make_authenticated_request.assert_not_called()


# ---------------------------------------------------------------------------
# Costs
# ---------------------------------------------------------------------------


@pytest.fixture(name="tracker")
def tracker_fixture(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Cost Explorer through the CLI's tracker, answering a 30-day month."""
    tracker, _ = patch_factory(monkeypatch, "cli.costs.get_cost_tracker", CostTracker)
    tracker.get_cost_summary.return_value = CostSummary(
        total=123.45,
        currency="USD",
        period_start="2026-10-26",
        period_end="2026-11-25",
        by_service=[
            ResourceCost("Amazon Elastic Compute Cloud - Compute", 100.0),
            ResourceCost("Amazon Elastic Container Service for Kubernetes", 23.45),
        ],
    )
    tracker.get_cost_by_region.return_value = {"us-east-1": 80.0, "us-west-2": 43.45}
    tracker.get_daily_trend.return_value = [{"date": "2026-11-24", "amount": 4.1}]
    tracker.get_forecast.return_value = {"forecast_total": 130.0, "period_end": "2026-12-25"}
    tracker.get_cost_allocation_tag_status.return_value = [{"key": "Project", "status": "Active"}]
    return tracker


class TestCosts:
    def test_the_report_has_every_view_of_the_bill(self, fleet: FleetData, tracker: Any) -> None:
        assert fleet.costs(days=14) == {
            "total": 123.45,
            "currency": "USD",
            "period_start": "2026-10-26",
            "period_end": "2026-11-25",
            "by_service": [
                {"service": "Amazon Elastic Compute Cloud - Compute", "amount": 100.0},
                {"service": "Amazon Elastic Container Service for Kubernetes", "amount": 23.45},
            ],
            "by_region": {"us-east-1": 80.0, "us-west-2": 43.45},
            "daily": [{"date": "2026-11-24", "amount": 4.1}],
            "errors": [],
            "forecast": {"forecast_total": 130.0, "period_end": "2026-12-25"},
            "allocation_tags": [{"key": "Project", "status": "Active"}],
        }
        tracker.get_cost_summary.assert_called_once_with(days=14)
        tracker.get_cost_by_region.assert_called_once_with(days=14)
        tracker.get_daily_trend.assert_called_once_with(days=14)
        tracker.get_forecast.assert_called_once_with(days_ahead=30)
        tracker.get_cost_allocation_tag_status.assert_called_once_with()

    def test_the_forecast_and_tag_status_are_optional(self, fleet: FleetData, tracker: Any) -> None:
        tracker.get_forecast.side_effect = CostDataUnavailableError(
            "Cost Explorer has no forecast for this account yet"
        )
        tracker.get_cost_allocation_tag_status.side_effect = client_error(
            "AccessDeniedException",
            "User is not authorized to perform: ce:ListCostAllocationTags",
            "ListCostAllocationTags",
        )
        report = fleet.costs()
        assert (report["forecast"], report["allocation_tags"]) == (None, [])
        # Each line says what failed and, where the failure says it, what to do.
        assert report["errors"] == [
            "forecast: CostDataUnavailableError: Cost Explorer has no forecast for this account yet",
            "allocation tags: AWS denied the request: "
            f"User is not authorized to perform: ce:ListCostAllocationTags. {DENIED}",
        ]
        assert report["total"] == 123.45
        tracker.get_cost_summary.assert_called_once_with(days=30)

    def test_a_failed_forecast_keeps_its_hint(self, fleet: FleetData, tracker: Any) -> None:
        tracker.get_forecast.side_effect = client_error(
            "ExpiredTokenException", "The security token included in the request is expired"
        )
        assert fleet.costs()["errors"] == [
            f"forecast: AWS rejected the credentials (ExpiredTokenException). {EXPIRED}"
        ]

    def test_the_summary_is_required(self, fleet: FleetData, tracker: Any) -> None:
        tracker.get_cost_summary.side_effect = client_error(
            "DataUnavailableException", "Data is not available", "GetCostAndUsage"
        )
        with pytest.raises(ClientError, match="DataUnavailableException"):
            fleet.costs()


@pytest.fixture(name="analytics")
def analytics_fixture(monkeypatch: pytest.MonkeyPatch) -> Any:
    """OpenCost allocation reports through Athena (the CLI's canned queries)."""
    analytics, _ = patch_factory(
        monkeypatch, "cli.cost_analytics.get_cost_analytics", CostAnalytics
    )
    analytics.cost_by_namespace.return_value = QueryResult(
        ["namespace", "total_cost"], [{"namespace": "gco-jobs", "total_cost": 12.5}], "q-1"
    )
    analytics.cost_by_region.return_value = QueryResult(
        ["region", "total_cost"], [{"region": "us-east-1", "total_cost": 10.0}], "q-2"
    )
    analytics.cost_over_time.return_value = QueryResult(
        ["bucket", "total_cost"], [{"bucket": "2026-11-24", "total_cost": 3.0}], "q-3"
    )
    return analytics


class TestAllocationCosts:
    def test_the_three_canned_queries_become_row_lists(
        self, fleet: FleetData, analytics: Any
    ) -> None:
        assert fleet.allocation_costs(days=14, granularity="hourly") == {
            "errors": [],
            "namespaces": [{"namespace": "gco-jobs", "total_cost": 12.5}],
            "regions": [{"region": "us-east-1", "total_cost": 10.0}],
            "trend": [{"bucket": "2026-11-24", "total_cost": 3.0}],
        }
        analytics.cost_by_namespace.assert_called_once_with(days=14)
        analytics.cost_by_region.assert_called_once_with(days=14)
        analytics.cost_over_time.assert_called_once_with(days=14, granularity="hourly")

    def test_a_failed_query_leaves_its_rows_empty_and_says_why(
        self, fleet: FleetData, analytics: Any
    ) -> None:
        analytics.cost_over_time.side_effect = AthenaQueryError("Query timed out after 60s")
        report = fleet.allocation_costs()
        assert report["trend"] == []
        assert report["errors"] == ["trend: AthenaQueryError: Query timed out after 60s"]
        assert report["namespaces"] == [{"namespace": "gco-jobs", "total_cost": 12.5}]
        analytics.cost_over_time.assert_called_once_with(days=7, granularity="daily")

    def test_a_failed_query_keeps_its_hint(self, fleet: FleetData, analytics: Any) -> None:
        analytics.cost_by_region.side_effect = client_error(
            "AccessDeniedException",
            "User is not authorized to perform: athena:StartQueryExecution",
            "StartQueryExecution",
        )
        assert fleet.allocation_costs()["errors"] == [
            "regions: AWS denied the request: "
            f"User is not authorized to perform: athena:StartQueryExecution. {DENIED}"
        ]

    def test_every_query_failing_alike_is_said_once_with_the_feature_hint(
        self, fleet: FleetData, analytics: Any
    ) -> None:
        for query in (
            analytics.cost_by_namespace,
            analytics.cost_by_region,
            analytics.cost_over_time,
        ):
            query.side_effect = client_error(
                "InvalidRequestException", "WorkGroup gco-cost is not found", "StartQueryExecution"
            )
        with pytest.raises(DashboardError) as raised:
            fleet.allocation_costs()
        assert str(raised.value) == (
            "Kubernetes allocation costs are unavailable: namespaces, regions, trend: "
            "InvalidRequestException: WorkGroup gco-cost is not found"
        )
        assert raised.value.hint == (
            "They need cost_monitoring.enabled in cdk.json and the monitoring stack."
        )

    def test_an_expired_session_is_not_blamed_on_a_missing_feature(
        self, fleet: FleetData, analytics: Any
    ) -> None:
        for query in (
            analytics.cost_by_namespace,
            analytics.cost_by_region,
            analytics.cost_over_time,
        ):
            query.side_effect = client_error(
                "ExpiredToken",
                "The security token included in the request is expired",
                "StartQueryExecution",
            )
        with pytest.raises(DashboardError) as raised:
            fleet.allocation_costs()
        assert str(raised.value) == (
            "Kubernetes allocation costs are unavailable: namespaces, regions, trend: "
            f"AWS rejected the credentials (ExpiredToken). {EXPIRED}"
        )
        assert raised.value.hint is None

    def test_failures_that_differ_are_each_named_and_keep_the_feature_hint(
        self, fleet: FleetData, analytics: Any
    ) -> None:
        analytics.cost_by_namespace.side_effect = client_error(
            "AccessDeniedException", "not authorized", "StartQueryExecution"
        )
        for query in (analytics.cost_by_region, analytics.cost_over_time):
            query.side_effect = AthenaQueryError("Table gco_cost.allocations does not exist")
        with pytest.raises(DashboardError) as raised:
            fleet.allocation_costs()
        assert str(raised.value) == (
            "Kubernetes allocation costs are unavailable: "
            f"namespaces: AWS denied the request: not authorized. {DENIED}; "
            "regions, trend: AthenaQueryError: Table gco_cost.allocations does not exist"
        )
        # The failures without a hint of their own may be the missing feature.
        assert raised.value.hint == (
            "They need cost_monitoring.enabled in cdk.json and the monitoring stack."
        )


# ---------------------------------------------------------------------------
# Platform objects and the console
# ---------------------------------------------------------------------------


def test_global_health_is_the_aggregators_answer(fleet: FleetData, api: Any) -> None:
    api.get_global_health.return_value = {"overall_status": "healthy", "regions": []}
    assert fleet.global_health() == {"overall_status": "healthy", "regions": []}
    api.get_global_health.assert_called_once_with()


@pytest.mark.parametrize(
    ("method", "path", "key"),
    [("templates", "/api/v1/templates", "templates"), ("webhooks", "/api/v1/webhooks", "webhooks")],
)
def test_template_and_webhook_lists_keep_only_objects(
    fleet: FleetData, api: Any, method: str, path: str, key: str
) -> None:
    api.call_api.return_value = {key: [{"name": "nightly"}, "junk", None], "count": 3}
    assert getattr(fleet, method)() == [{"name": "nightly"}]
    api.call_api.assert_called_once_with("GET", path, region=None)


@pytest.mark.parametrize("method", ["templates", "webhooks"])
def test_a_null_list_is_empty(fleet: FleetData, api: Any, method: str) -> None:
    api.call_api.return_value = {method: None, "count": 0}
    assert getattr(fleet, method)() == []


@pytest.mark.parametrize("method", ["templates", "webhooks"])
def test_lists_without_their_key_are_empty_and_ride_the_default_bridge_in_regional_mode(
    regional_fleet: FleetData, regional_api: Any, method: str
) -> None:
    regional_api.call_api.return_value = {"count": 0}
    assert getattr(regional_fleet, method)() == []
    assert regional_api.call_api.call_args.kwargs["region"] == "us-east-1"


class TestApiGet:
    def test_a_route_outside_the_console_list_is_refused(self, fleet: FleetData, api: Any) -> None:
        with pytest.raises(DashboardError, match=r"^/api/v1/manifests is not a console route\.$"):
            fleet.api_get("/api/v1/manifests")
        api.call_api.assert_not_called()

    def test_a_global_route_cannot_be_pinned_to_a_region(self, fleet: FleetData, api: Any) -> None:
        with pytest.raises(
            DashboardError, match=r"^/api/v1/global/jobs answers only from the global API\.$"
        ):
            fleet.api_get("/api/v1/global/jobs", region="us-east-1")
        api.call_api.assert_not_called()

    @pytest.mark.parametrize(
        ("path", "region", "params", "sent"),
        [
            ("/api/v1/global/health", None, None, None),
            ("/api/v1/jobs", "us-west-2", {"namespace": "gco-jobs"}, {"namespace": "gco-jobs"}),
            ("/api/v1/templates", None, {}, None),
        ],
        ids=["global", "pinned-with-params", "empty-params"],
    )
    def test_a_console_route_is_one_get(
        self,
        fleet: FleetData,
        api: Any,
        path: str,
        region: str | None,
        params: dict[str, str] | None,
        sent: dict[str, str] | None,
    ) -> None:
        api.call_api.return_value = {"ok": True}
        assert fleet.api_get(path, region=region, params=params) == {"ok": True}
        api.call_api.assert_called_once_with("GET", path, region=region, params=sent)

    def test_every_console_route_can_be_read(self, fleet: FleetData, api: Any) -> None:
        api.call_api.return_value = {}
        for path in CONSOLE_ROUTES:
            fleet.api_get(path)
        assert [made.args[1] for made in api.call_api.call_args_list] == list(CONSOLE_ROUTES)
