"""One complete cluster reading per health refresh, and what a failed read means.

Drives ``HealthMonitor.get_health_status`` with typed node and pod objects
(``V1Node.from_dict`` / ``V1Pod.from_dict`` from wire JSON) behind mocked API
clients. Pins that a refresh reads metrics-server once and lists nodes and pods
once each, that concurrent callers share one refresh, that a failed read is
never reported as healthy (the last complete reading stands in for up to 60
seconds, after which the status is unhealthy and names the failed read), that
utilization above 100% (over-commit) is a legal reading judged against the
thresholds instead of a validation error, and that ``active_jobs`` counts Jobs,
not pods, in the job namespaces.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from kubernetes import config as k8s_config
from kubernetes.client.exceptions import ApiException
from kubernetes.client.models import V1Node, V1Pod

from gco.models import ResourceThresholds
from gco.services import health_monitor as hm


class FakeClock:
    def __init__(self) -> None:
        self.now = 500.0

    def __call__(self) -> float:
        return self.now


def _node(name: str, cpu: str = "4", memory: str = "16Gi", gpus: int | None = None) -> V1Node:
    allocatable = {"cpu": cpu, "memory": memory}
    if gpus is not None:
        allocatable["nvidia.com/gpu"] = str(gpus)
    return V1Node.from_dict({"metadata": {"name": name}, "status": {"allocatable": allocatable}})


def _pod(
    name: str,
    namespace: str,
    phase: str,
    *,
    job: str | None = None,
    gpus: int | None = None,
) -> V1Pod:
    requests = {"cpu": "1", "memory": "1Gi"}
    if gpus is not None:
        requests["nvidia.com/gpu"] = str(gpus)
    metadata: dict[str, Any] = {"name": name, "namespace": namespace}
    if job is not None:
        metadata["ownerReferences"] = [
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "name": job,
                "uid": f"uid-{job}",
                "controller": True,
            }
        ]
    return V1Pod.from_dict(
        {
            "metadata": metadata,
            "spec": {
                "containers": [
                    {"name": "main", "image": "busybox", "resources": {"requests": requests}}
                ]
            },
            "status": {"phase": phase},
        }
    )


def _monitor(
    thresholds: ResourceThresholds | None = None,
    clock: FakeClock | None = None,
    job_namespaces: tuple[str, ...] | None = ("gco-jobs",),
) -> hm.HealthMonitor:
    with patch.object(hm, "config") as cfg, patch.object(hm, "client"):
        cfg.ConfigException = k8s_config.ConfigException
        cfg.load_incluster_config.return_value = None
        return hm.HealthMonitor(
            "c1",
            "us-east-1",
            thresholds
            or ResourceThresholds(cpu_threshold=80, memory_threshold=80, gpu_threshold=60),
            job_namespaces=job_namespaces,
            clock=clock or FakeClock(),
        )


def _serve(
    monitor: hm.HealthMonitor,
    *,
    cpu_usage: str = "1",
    memory_usage: str = "4Gi",
    nodes: list[V1Node] | None = None,
    pods: list[V1Pod] | None = None,
) -> None:
    monitor.metrics_v1beta1.list_cluster_custom_object.return_value = {
        "items": [{"usage": {"cpu": cpu_usage, "memory": memory_usage}}]
    }
    monitor.core_v1.list_node.return_value = SimpleNamespace(
        items=nodes if nodes is not None else [_node("n1")]
    )
    monitor.core_v1.list_pod_for_all_namespaces.return_value = SimpleNamespace(items=pods or [])


class TestOneReadingPerRefresh:
    @pytest.mark.asyncio
    async def test_a_refresh_lists_nodes_and_pods_once(self):
        monitor = _monitor()
        _serve(monitor, pods=[_pod("p", "gco-jobs", "Running", job="a")])

        status = await monitor.get_health_status()

        assert status.status == "healthy"
        assert monitor.metrics_v1beta1.list_cluster_custom_object.call_count == 1
        assert monitor.core_v1.list_node.call_count == 1
        assert monitor.core_v1.list_pod_for_all_namespaces.call_count == 1

    @pytest.mark.asyncio
    async def test_concurrent_callers_share_one_refresh(self):
        monitor = _monitor()
        _serve(monitor)
        listing = monitor.core_v1.list_pod_for_all_namespaces.return_value

        def slow_pod_list(**_kwargs: Any) -> Any:
            time.sleep(0.2)
            return listing

        monitor.core_v1.list_pod_for_all_namespaces.side_effect = slow_pod_list

        first, second, third = await asyncio.gather(
            monitor.get_health_status(),
            monitor.get_health_status(),
            monitor.get_health_status(),
        )

        assert first is second is third
        assert monitor.core_v1.list_node.call_count == 1
        assert monitor.core_v1.list_pod_for_all_namespaces.call_count == 1

    @pytest.mark.asyncio
    async def test_a_cancelled_caller_does_not_cancel_the_shared_refresh(self):
        monitor = _monitor()
        _serve(monitor)
        listing = monitor.core_v1.list_pod_for_all_namespaces.return_value
        monitor.core_v1.list_pod_for_all_namespaces.side_effect = lambda **_kw: (
            time.sleep(0.2),
            listing,
        )[1]

        impatient = asyncio.create_task(monitor.get_health_status())
        await asyncio.sleep(0.05)
        impatient.cancel()
        status = await monitor.get_health_status()

        assert status.status == "healthy"
        assert monitor.core_v1.list_pod_for_all_namespaces.call_count == 1

    @pytest.mark.asyncio
    async def test_the_next_refresh_reads_again(self):
        monitor = _monitor()
        _serve(monitor)

        await monitor.get_health_status()
        await monitor.get_health_status()

        assert monitor.core_v1.list_node.call_count == 2
        assert not hasattr(hm.HealthMonitor, "_get_active_jobs_count")


class TestAFailedReadIsNeverHealthy:
    @pytest.mark.asyncio
    async def test_a_failed_pod_list_without_a_previous_reading_is_unhealthy(self):
        monitor = _monitor()
        _serve(monitor)
        monitor.core_v1.list_pod_for_all_namespaces.side_effect = TimeoutError("read timed out")

        status = await monitor.get_health_status()

        assert status.status == "unhealthy"
        assert status.message == "Health check error: listing pods failed: read timed out"

    @pytest.mark.asyncio
    async def test_a_failed_node_list_without_a_previous_reading_is_unhealthy(self):
        monitor = _monitor()
        _serve(monitor)
        monitor.core_v1.list_node.side_effect = ApiException(status=403, reason="Forbidden")

        status = await monitor.get_health_status()

        assert status.status == "unhealthy"
        assert "listing nodes failed" in str(status.message)

    @pytest.mark.asyncio
    async def test_unreadable_metrics_are_a_failed_read(self):
        monitor = _monitor()
        _serve(monitor)
        monitor.metrics_v1beta1.list_cluster_custom_object.return_value = {"items": [{}]}

        status = await monitor.get_health_status()

        assert status.status == "unhealthy"
        assert "reading node and pod resources failed" in str(status.message)

    @pytest.mark.asyncio
    async def test_the_last_good_reading_stands_in_for_sixty_seconds(self):
        clock = FakeClock()
        monitor = _monitor(clock=clock)
        _serve(monitor, cpu_usage="1")  # 25% of 4 cores
        good = await monitor.get_health_status()
        assert good.status == "healthy"
        assert good.message is None

        monitor.core_v1.list_pod_for_all_namespaces.side_effect = TimeoutError("read timed out")
        clock.now += 30
        stale = await monitor.get_health_status()

        assert stale.status == "healthy"
        assert stale.resource_utilization == good.resource_utilization
        assert stale.message == (
            "Using the reading from 30s ago (listing pods failed: read timed out)"
        )

        clock.now += hm.LAST_GOOD_READING_MAX_AGE_SECONDS - 30 + 1
        expired = await monitor.get_health_status()

        assert expired.status == "unhealthy"
        assert expired.message == "Health check error: listing pods failed: read timed out"

    @pytest.mark.asyncio
    async def test_a_stale_reading_keeps_its_threshold_violations(self):
        clock = FakeClock()
        monitor = _monitor(clock=clock)
        _serve(monitor, cpu_usage="3600m")  # 90% of 4 cores
        await monitor.get_health_status()
        monitor.core_v1.list_node.side_effect = TimeoutError("read timed out")
        clock.now += 10

        status = await monitor.get_health_status()

        assert status.status == "unhealthy"
        assert status.message == (
            "Threshold violations: CPU: 90.0% > 80%; Using the reading from 10s ago "
            "(listing nodes failed: read timed out)"
        )

    @pytest.mark.asyncio
    async def test_a_successful_read_renews_the_stand_in(self):
        clock = FakeClock()
        monitor = _monitor(clock=clock)
        _serve(monitor)
        await monitor.get_health_status()
        clock.now += 50
        await monitor.get_health_status()  # fresh reading at +50
        monitor.core_v1.list_pod_for_all_namespaces.side_effect = TimeoutError("read timed out")
        clock.now += 50  # 100 s after the first, 50 s after the latest

        status = await monitor.get_health_status()

        assert status.status == "healthy"
        assert "50s ago" in str(status.message)

    @pytest.mark.asyncio
    async def test_a_reading_that_cannot_be_judged_is_unhealthy(self):
        from gco.models import RequestedResources, ResourceUtilization

        monitor = _monitor()
        reading = (ResourceUtilization(1.0, 1.0, 0.0), -1, 0, RequestedResources(0.0, 0.0, 0))

        with patch.object(monitor, "get_cluster_metrics", return_value=reading):
            status = await monitor.get_health_status()

        assert status.status == "unhealthy"
        assert status.message == "Health check error: Active jobs count cannot be negative"


class TestOverCommitIsALegalReading:
    @pytest.mark.asyncio
    async def test_usage_above_allocatable_with_checks_disabled_is_healthy(self):
        monitor = _monitor(
            ResourceThresholds(cpu_threshold=-1, memory_threshold=-1, gpu_threshold=-1)
        )
        _serve(monitor, cpu_usage="4000m", nodes=[_node("n1", cpu="3920m")])

        status = await monitor.get_health_status()

        assert status.status == "healthy"
        assert status.resource_utilization.cpu == pytest.approx(102.04, abs=0.01)

    @pytest.mark.asyncio
    async def test_gpu_requests_above_allocatable_are_a_threshold_violation(self):
        monitor = _monitor()
        _serve(
            monitor,
            nodes=[_node("n1", gpus=4)],
            pods=[_pod("p", "gco-jobs", "Running", job="train", gpus=8)],
        )

        status = await monitor.get_health_status()

        assert status.status == "unhealthy"
        assert status.resource_utilization.gpu == 200.0
        assert status.message == "Threshold violations: GPU: 200.0% > 60%"


class TestActiveJobs:
    @pytest.mark.asyncio
    async def test_active_jobs_counts_jobs_in_the_job_namespaces(self):
        monitor = _monitor(job_namespaces=("gco-jobs", "team-a"))
        _serve(
            monitor,
            pods=[
                _pod("a-0", "gco-jobs", "Running", job="a"),
                _pod("a-1", "gco-jobs", "Running", job="a"),  # same Job
                _pod("b-0", "team-a", "Pending", job="b"),  # active: waiting to run
                _pod("c-0", "gco-jobs", "Succeeded", job="c"),  # finished
                _pod("web", "gco-jobs", "Running"),  # not owned by a Job
                _pod("rotate", "monitoring", "Running", job="rotate"),  # not a job namespace
                _pod("sys", "kube-system", "Running", job="sys"),
            ],
        )

        status = await monitor.get_health_status()

        assert status.active_jobs == 2
        assert status.pending_pods == 1

    def test_job_namespaces_come_from_allowed_namespaces(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_NAMESPACES", "team-a, team-b,,")
        assert _monitor(job_namespaces=None).job_namespaces == {"team-a", "team-b"}
        monkeypatch.delenv("ALLOWED_NAMESPACES")
        assert _monitor(job_namespaces=None).job_namespaces == {"gco-jobs", "default"}
