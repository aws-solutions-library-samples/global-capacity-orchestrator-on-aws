"""The services read the resource-metrics API through one shared pin.

``metrics.k8s.io/v1`` is GA in Kubernetes 1.37 but the pinned metrics-server
add-on (``v0.9.x``) registers only ``v1beta1``. Both callers must take the
group and version from ``gco.k8s_api_versions`` so the flip to ``v1`` is one
edit, and the pin must be re-evaluated when the add-on moves past ``v0.9``.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

from gco import k8s_api_versions
from gco.k8s_api_versions import METRICS_API_GROUP, METRICS_API_VERSION
from gco.stacks.constants import EKS_ADDON_METRICS_SERVER


def test_pin_values_are_the_metrics_server_resource_metrics_api() -> None:
    assert METRICS_API_GROUP == "metrics.k8s.io"
    assert METRICS_API_VERSION in {"v1beta1", "v1"}


def test_module_is_standard_library_only() -> None:
    # Every service image copies gco/ wholesale, so this module must not pull
    # service-specific dependencies into the import graph of the health monitor.
    source = Path(k8s_api_versions.__file__).read_text(encoding="utf-8")
    assert not re.search(r"^(import|from)\s", source, flags=re.MULTILINE)


def test_pin_tracks_the_metrics_server_addon_line() -> None:
    """Reminder: flip to ``v1`` once the pinned add-on registers it.

    metrics-server ``v0.9.x`` serves only ``v1beta1``; upstream ``master``
    registers ``v1`` as well, so the first release carrying it is expected to
    be ``v0.10.x``. This fails on a ``v0.10+`` pin while the services still
    read ``v1beta1``, which is the moment to re-check ``pkg/api/install.go``
    of the pinned release and move ``METRICS_API_VERSION``.
    """
    match = re.fullmatch(r"v(\d+)\.(\d+)\.\d+-eksbuild\.\d+", EKS_ADDON_METRICS_SERVER)
    assert match, EKS_ADDON_METRICS_SERVER
    major_minor = (int(match.group(1)), int(match.group(2)))
    if METRICS_API_VERSION == "v1beta1":
        assert major_minor <= (0, 9), (
            f"EKS_ADDON_METRICS_SERVER is {EKS_ADDON_METRICS_SERVER}; releases past v0.9 "
            "are expected to register metrics.k8s.io/v1. Check pkg/api/install.go of the "
            "pinned release and flip METRICS_API_VERSION in gco/k8s_api_versions.py (or "
            "update this expectation if the release still serves only v1beta1)."
        )
    else:
        assert major_minor >= (0, 10), (
            "METRICS_API_VERSION is v1 but the pinned metrics-server is a v0.9 build, "
            "which serves only v1beta1."
        )


async def test_health_monitor_reads_node_metrics_through_the_pin() -> None:
    from gco.models import ResourceThresholds
    from gco.services.health_monitor import HealthMonitor

    with (
        patch("gco.services.health_monitor.config"),
        patch("gco.services.health_monitor.client"),
    ):
        monitor = HealthMonitor(
            "gco-test",
            "us-east-1",
            ResourceThresholds(cpu_threshold=80, memory_threshold=85, gpu_threshold=90),
        )
    monitor.metrics_v1beta1 = MagicMock()
    monitor.metrics_v1beta1.list_cluster_custom_object.return_value = {"items": []}

    await monitor._get_node_metrics()

    monitor.metrics_v1beta1.list_cluster_custom_object.assert_called_once_with(
        group=METRICS_API_GROUP,
        version=METRICS_API_VERSION,
        plural="nodes",
        _request_timeout=monitor._k8s_timeout,
    )


async def test_job_metrics_route_reads_pod_metrics_through_the_pin() -> None:
    from gco.services.api_routes import jobs as routes

    pod = MagicMock()
    pod.metadata.name = "job-pod-1"
    processor = MagicMock()
    processor.cluster_id = "gco-test"
    processor.region = "us-east-1"
    processor.core_v1.list_namespaced_pod.return_value = MagicMock(items=[pod])
    processor.custom_objects.get_namespaced_custom_object.return_value = {
        "containers": [{"name": "main", "usage": {"cpu": "250m", "memory": "64Mi"}}]
    }

    with (
        patch.object(routes, "_check_processor", return_value=processor),
        patch.object(routes, "_check_namespace"),
    ):
        response = await routes.get_job_metrics("gco-jobs", "job")

    assert response.status_code == 200
    processor.custom_objects.get_namespaced_custom_object.assert_called_once_with(
        group=METRICS_API_GROUP,
        version=METRICS_API_VERSION,
        namespace="gco-jobs",
        plural="pods",
        name="job-pod-1",
    )
