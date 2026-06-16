"""Gating dependent role-pod creation on the shared master's readiness.

A store-bearing endpoint must not stand up its role pods until the single
shared ``mooncake-master`` reports a Ready replica. The monitor decides this in
:meth:`InferenceMonitor._gate_on_mooncake_master`, which maintains the master
(create-if-absent), then reports whether dependent pods may proceed:

- While the master reports fewer than 1 Ready replica, creation is deferred and
  the endpoint stays in ``creating`` with no error; the first deferral starts a
  clock.
- Once the master reports at least 1 Ready replica, the gate opens, the clock is
  cleared, and the endpoint advances out of ``creating``.
- If the master stays unready past the 600-second wait window, the endpoint
  keeps deferring and stays in ``creating`` but surfaces a not-ready error,
  while the master itself is left untouched.
- If maintaining the master fails outright, no dependent pods are produced, any
  existing master is left untouched, and the endpoint stays in ``creating`` with
  a create-failure error.

These examples drive that decision with the shared master's Ready replica count
controlled directly and the deferral clock seeded, so each branch is exercised
without waiting on real time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

NAMESPACE = "gco-inference"
ENDPOINT = "my-endpoint"

# A store-bearing endpoint whose role pods depend on the shared master.
STORE_SPEC = {
    "mooncake": {
        "mode": "store",
        "store": {"enabled": True, "master_image": "example/mooncake-master:pinned"},
    }
}


def _make_monitor(region: str = "us-east-1"):
    """Build an :class:`InferenceMonitor` with every K8s client mocked out."""
    with (
        patch("gco.services.inference_monitor.config.load_incluster_config"),
        patch("gco.services.inference_monitor.client.AppsV1Api") as mock_apps,
        patch("gco.services.inference_monitor.client.CoreV1Api") as mock_core,
        patch("gco.services.inference_monitor.client.NetworkingV1Api") as mock_net,
        patch("gco.services.inference_monitor.client.AutoscalingV2Api"),
    ):
        from gco.services.inference_monitor import InferenceMonitor

        monitor = InferenceMonitor(
            cluster_id="test-cluster",
            region=region,
            store=MagicMock(),
            namespace=NAMESPACE,
            reconcile_interval=5,
        )
        monitor.apps_v1 = mock_apps.return_value
        monitor.core_v1 = mock_core.return_value
        monitor.networking_v1 = mock_net.return_value
        return monitor


def _master_status(ready_replicas):
    """Shape a StatefulSet status carrying a Ready replica count."""
    return SimpleNamespace(status=SimpleNamespace(ready_replicas=ready_replicas))


def _set_ready_replicas(monitor, ready_replicas: int) -> None:
    """Make the shared master report ``ready_replicas`` Ready replicas."""
    monitor.apps_v1.read_namespaced_stateful_set_status.return_value = _master_status(
        ready_replicas
    )


@pytest.fixture
def monitor():
    return _make_monitor()


def test_defers_while_master_reports_no_ready_replica(monitor):
    """An unready master defers role pods and reports creating with no error.

    The gate stays closed, the endpoint reports ``creating`` without an error
    while the master is simply still coming up, the first deferral is recorded
    so the wait window can later be measured, and no dependent pod is created.
    """
    _set_ready_replicas(monitor, 0)

    gate = monitor._gate_on_mooncake_master(ENDPOINT, NAMESPACE, STORE_SPEC)

    assert gate.proceed is False
    assert gate.state == "creating"
    assert gate.error is None
    assert ENDPOINT in monitor._master_deferral_since
    monitor.apps_v1.create_namespaced_deployment.assert_not_called()


def test_resumes_when_master_reaches_ready_and_clears_clock(monitor):
    """A Ready master opens the gate and clears the recorded deferral.

    After a prior deferral was recorded, the master reaching one Ready replica
    lets creation proceed with no reported state or error, and the deferral
    clock is cleared so a later master restart restarts the window cleanly.
    """
    monitor._master_deferral_since[ENDPOINT] = datetime.now(UTC) - timedelta(seconds=30)
    _set_ready_replicas(monitor, 1)

    gate = monitor._gate_on_mooncake_master(ENDPOINT, NAMESPACE, STORE_SPEC)

    assert gate.proceed is True
    assert gate.state is None
    assert gate.error is None
    assert ENDPOINT not in monitor._master_deferral_since


def test_keeps_deferring_after_wait_window_and_surfaces_not_ready_error(monitor):
    """Past the 600s window the gate keeps deferring and surfaces an error.

    With the first deferral seeded more than the wait window in the past and the
    master still reporting no Ready replica, the gate stays closed, the endpoint
    stays in ``creating`` but now carries a not-ready error, and the master is
    neither deleted nor modified.
    """
    monitor._master_deferral_since[ENDPOINT] = datetime.now(UTC) - timedelta(seconds=601)
    _set_ready_replicas(monitor, 0)

    gate = monitor._gate_on_mooncake_master(ENDPOINT, NAMESPACE, STORE_SPEC)

    assert gate.proceed is False
    assert gate.state == "creating"
    assert gate.error is not None
    assert "ready" in gate.error.lower()
    # Still deferring: the endpoint remains tracked and no dependent pod created.
    assert ENDPOINT in monitor._master_deferral_since
    monitor.apps_v1.create_namespaced_deployment.assert_not_called()
    # The master is left untouched: no in-place change to the StatefulSet.
    monitor.apps_v1.patch_namespaced_stateful_set.assert_not_called()
    monitor.apps_v1.replace_namespaced_stateful_set.assert_not_called()
    monitor.apps_v1.delete_namespaced_stateful_set.assert_not_called()


def test_create_failure_blocks_dependent_pods_and_reports_error(monitor):
    """A failed master create blocks dependent pods and reports the failure.

    When maintaining the shared master raises a non-conflict API error, the gate
    stays closed, the endpoint stays in ``creating`` with a create-failure
    error, and no dependent pod is materialized.
    """
    monitor.core_v1.create_namespaced_service.side_effect = ApiException(
        status=500, reason="Internal Server Error"
    )

    gate = monitor._gate_on_mooncake_master(ENDPOINT, NAMESPACE, STORE_SPEC)

    assert gate.proceed is False
    assert gate.state == "creating"
    assert gate.error is not None
    assert "created" in gate.error.lower()
    monitor.apps_v1.create_namespaced_deployment.assert_not_called()


def test_ready_replica_count_reflects_statefulset_status(monitor):
    """The Ready replica count mirrors the master StatefulSet status."""
    _set_ready_replicas(monitor, 3)

    assert monitor._mooncake_master_ready_replicas(NAMESPACE) == 3


def test_absent_master_reports_zero_ready_replicas(monitor):
    """A missing master StatefulSet reports zero Ready replicas, not an error."""
    monitor.apps_v1.read_namespaced_stateful_set_status.side_effect = ApiException(
        status=404, reason="Not Found"
    )

    assert monitor._mooncake_master_ready_replicas(NAMESPACE) == 0
