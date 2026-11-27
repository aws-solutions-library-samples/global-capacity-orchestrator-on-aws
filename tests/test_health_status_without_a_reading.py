"""A health status without a cluster reading reports no utilization.

When the health monitor's cluster reads fail and no recent reading can stand
in, it reports ``unhealthy`` with the failed read named. Its utilization,
active Jobs and pending pods are None, not zeros: ``/api/v1/metrics`` serves
them as null and nothing is published to the utilization metrics, because a
zero there charted an idle cluster and ``gco capacity`` ranked the Region as
the emptiest one. A status with a reading publishes its GPU counts beside the
GPU share.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gco.models import HealthStatus, RequestedResources, ResourceThresholds, ResourceUtilization

THRESHOLDS = ResourceThresholds(cpu_threshold=80, memory_threshold=80, gpu_threshold=60)
NOW = datetime(2026, 11, 27, 10, 0, tzinfo=UTC)


def _status(**overrides: object) -> HealthStatus:
    fields: dict[str, object] = {
        "cluster_id": "gco-us-east-1",
        "region": "us-east-1",
        "timestamp": NOW,
        "status": "unhealthy",
        "resource_utilization": None,
        "thresholds": THRESHOLDS,
        "active_jobs": None,
        "pending_pods": None,
        "message": "Health check error: listing pods failed",
    }
    fields.update(overrides)
    return HealthStatus(**fields)  # type: ignore[arg-type]


def _measured(**overrides: object) -> HealthStatus:
    fields: dict[str, object] = {
        "status": "healthy",
        "resource_utilization": ResourceUtilization(
            cpu=12.3456, memory=40.0, gpu=25.0, gpu_allocatable=8, gpu_requested=2
        ),
        "active_jobs": 3,
        "pending_pods": 0,
        "pending_requested": RequestedResources(cpu_vcpus=0.0, memory_gb=0.0, gpus=0),
        "message": None,
    }
    fields.update(overrides)
    return _status(**fields)


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


class TestTheModel:
    def test_a_status_without_a_reading_is_never_healthy_and_violates_nothing(self) -> None:
        status = _status()

        assert status.is_healthy() is False
        assert status.get_threshold_violations() == []

    @pytest.mark.parametrize(
        "parts",
        [
            {"active_jobs": 3},
            {"pending_pods": 0},
            {"resource_utilization": ResourceUtilization(1.0, 1.0, 0.0)},
            {"resource_utilization": ResourceUtilization(1.0, 1.0, 0.0), "active_jobs": 3},
        ],
        ids=["jobs-only", "pending-only", "utilization-only", "no-pending-pods"],
    )
    def test_a_reading_is_set_whole_or_not_at_all(self, parts: dict[str, object]) -> None:
        with pytest.raises(ValueError, match="one cluster reading"):
            _status(**parts)

    def test_a_status_without_a_reading_must_be_unhealthy(self) -> None:
        with pytest.raises(ValueError, match="must be 'unhealthy'"):
            _status(status="healthy")

    def test_a_measured_status_is_judged_as_before(self) -> None:
        status = _measured(resource_utilization=ResourceUtilization(90.0, 10.0, 0.0))

        assert status.is_healthy() is False
        assert status.get_threshold_violations() == ["CPU: 90.0% > 80%"]


# ---------------------------------------------------------------------------
# GET /api/v1/metrics
# ---------------------------------------------------------------------------


@pytest.fixture
def api():
    import gco.services.health_api as health_api_module

    saved = (
        health_api_module.health_monitor,
        health_api_module.current_health_status,
        health_api_module.health_metrics,
    )
    yield health_api_module
    (
        health_api_module.health_monitor,
        health_api_module.current_health_status,
        health_api_module.health_metrics,
    ) = saved


class TestTheMetricsEndpoint:
    @pytest.mark.asyncio
    async def test_a_status_without_a_reading_serves_nulls(self, api) -> None:
        api.current_health_status = _status()

        body = await api.get_metrics()

        assert body["status"] == "unhealthy"
        assert body["resource_utilization"] == {
            "cpu_percent": None,
            "memory_percent": None,
            "gpu_percent": None,
            "gpu_allocatable": None,
            "gpu_requested": None,
        }
        assert body["active_jobs"] is None
        assert body["threshold_violations"] == []
        assert body["message"] == "Health check error: listing pods failed"

    @pytest.mark.asyncio
    async def test_a_reading_serves_its_values_and_gpu_counts(self, api) -> None:
        api.current_health_status = _measured()

        body = await api.get_metrics()

        assert body["resource_utilization"] == {
            "cpu_percent": 12.35,
            "memory_percent": 40.0,
            "gpu_percent": 25.0,
            "gpu_allocatable": 8,
            "gpu_requested": 2,
        }
        assert tuple(body["resource_utilization"]) == api.UTILIZATION_KEYS
        assert body["active_jobs"] == 3


# ---------------------------------------------------------------------------
# What reaches CloudWatch
# ---------------------------------------------------------------------------


async def _one_pass(api, status: HealthStatus) -> MagicMock:
    """Run the background loop once with ``status`` and return the publisher."""
    monitor = MagicMock()
    monitor.get_health_status = AsyncMock(return_value=status)
    monitor.sync_alb_registration = AsyncMock()
    monitor.record_keep_warm_idle = AsyncMock()
    metrics = MagicMock()
    api.health_monitor = monitor
    api.health_metrics = metrics
    with patch.object(api.asyncio, "sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
        await api.background_health_monitor()
    return metrics


class TestPublishing:
    @pytest.mark.asyncio
    async def test_a_status_without_a_reading_publishes_only_its_health(self, api) -> None:
        metrics = await _one_pass(api, _status())

        metrics.publish_resource_utilization.assert_not_called()
        metrics.publish_health_status.assert_called_once_with(
            is_healthy=False, threshold_violations=[]
        )

    @pytest.mark.asyncio
    async def test_a_reading_publishes_its_utilization_and_gpu_counts(self, api) -> None:
        metrics = await _one_pass(api, _measured())

        metrics.publish_resource_utilization.assert_called_once_with(
            cpu_percent=12.3456,
            memory_percent=40.0,
            gpu_percent=25.0,
            active_jobs=3,
            gpu_allocatable=8,
            gpu_requested=2,
        )
        metrics.publish_health_status.assert_called_once_with(
            is_healthy=True, threshold_violations=[]
        )


class TestThePublisher:
    def _published(self, **counts: int | None) -> list[tuple[str, float, str]]:
        with patch("gco.services.metrics_publisher.boto3.client") as client_factory:
            from gco.services.metrics_publisher import HealthMonitorMetrics

            publisher = HealthMonitorMetrics(cluster_name="gco-us-east-1", region="us-east-1")
            assert publisher.publish_resource_utilization(
                cpu_percent=10.0, memory_percent=20.0, gpu_percent=0.0, active_jobs=0, **counts
            )
            sent = client_factory.return_value.put_metric_data.call_args.kwargs
        return [(m["MetricName"], m["Value"], m["Unit"]) for m in sent["MetricData"]]

    def test_the_counts_ride_with_the_gpu_share(self) -> None:
        assert self._published(gpu_allocatable=0, gpu_requested=0)[-2:] == [
            ("ClusterGpuAllocatable", 0, "Count"),
            ("ClusterGpuRequested", 0, "Count"),
        ]

    @pytest.mark.parametrize(
        "counts",
        [{}, {"gpu_allocatable": 4}, {"gpu_requested": 1}],
        ids=["no-counts", "allocatable-only", "requested-only"],
    )
    def test_a_partial_count_is_not_published(self, counts: dict[str, int]) -> None:
        names = [name for name, _value, _unit in self._published(**counts)]
        assert names == [
            "ClusterCpuUtilization",
            "ClusterMemoryUtilization",
            "ClusterGpuUtilization",
            "ActiveJobs",
        ]


# ---------------------------------------------------------------------------
# The monitor's own loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_monitor_loop_prints_that_it_has_no_reading(capsys) -> None:
    import gco.services.health_monitor as health_monitor_module

    dispatcher = MagicMock()
    dispatcher.start = AsyncMock()
    dispatcher.stop = AsyncMock()
    dispatcher.get_metrics.return_value = {
        "deliveries_total": 0,
        "deliveries_success": 0,
        "deliveries_failed": 0,
    }
    monitor = MagicMock()
    monitor.get_health_status = AsyncMock(return_value=_status())
    with (
        patch.object(health_monitor_module, "create_health_monitor_from_env", return_value=monitor),
        patch(
            "gco.services.webhook_dispatcher.create_webhook_dispatcher_from_env",
            return_value=dispatcher,
        ),
        patch.object(health_monitor_module.asyncio, "sleep", side_effect=KeyboardInterrupt()),
    ):
        await health_monitor_module.main()

    printed = capsys.readouterr().out
    assert "Health Status: unhealthy" in printed
    assert "No cluster reading" in printed
    assert "CPU:" not in printed and "Active Jobs:" not in printed
    assert "Message: Health check error: listing pods failed" in printed
