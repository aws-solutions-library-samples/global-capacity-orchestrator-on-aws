"""Readiness counts only pods of the Deployment's current template (finding I12).

``readyReplicas`` counts every Ready pod a Deployment selects, including the
previous template's pods while a rollout replaces them. Right after
``gco inference canary -i v3`` the canary's template runs v3 while its v2 pods
are still Ready, and the monitor reported the canary ``running`` on v3, so the
inference proxy sampled canary traffic onto v2. The primary had the same gap:
after ``update-image`` the old pods kept the region ``running`` and the endpoint
was promoted from ``deploying`` before any pod ran the new image.

The canary now counts only pods known to run its current template. The primary
keeps serving from the old pods through a rolling update (its region stays
``running``, as the inference proxy requires) but reports
``rollout_complete: false`` until pods of the current template meet the
readiness floor, and the endpoint is promoted only once every region is past
that point. The Deployments here are wire JSON as the API server returns them.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from kubernetes import client

from gco.services.inference_monitor import InferenceMonitor, ResourceCleanupResult

REGION = "us-east-1"
NAMESPACE = "gco-inference"
LIFECYCLE = "life-1"


def _wire_deployment(
    name: str,
    image: str,
    *,
    spec_replicas: int,
    generation: int = 2,
    observed: int | None = 2,
    replicas: int | None = None,
    updated: int | None = None,
    ready: int | None = None,
) -> client.V1Deployment:
    """A Deployment as the API returns it; zero counts are omitted, as on the wire."""
    status: dict[str, Any] = {}
    for key, value in (
        ("observedGeneration", observed),
        ("replicas", replicas),
        ("updatedReplicas", updated),
        ("readyReplicas", ready),
    ):
        if value:
            status[key] = value
    return client.V1Deployment.from_dict(
        {
            "metadata": {
                "name": name,
                "namespace": NAMESPACE,
                "generation": generation,
                "uid": f"uid-{name}",
                "resourceVersion": "9",
            },
            "spec": {
                "replicas": spec_replicas,
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {"labels": {"app": name}},
                    "spec": {"containers": [{"name": "inference", "image": image}]},
                },
            },
            "status": status,
        }
    )


def _make_monitor(store: MagicMock | None = None) -> InferenceMonitor:
    with (
        patch("gco.services.inference_monitor.config.load_incluster_config"),
        patch("gco.services.inference_monitor.client.AppsV1Api"),
        patch("gco.services.inference_monitor.client.CoreV1Api"),
        patch("gco.services.inference_monitor.client.NetworkingV1Api"),
        patch("gco.services.inference_monitor.client.DiscoveryV1Api"),
    ):
        return InferenceMonitor(
            cluster_id="cluster-a",
            region=REGION,
            store=store or MagicMock(),
            namespace=NAMESPACE,
            reconcile_interval=1,
        )


# ---------------------------------------------------------------------------
# Canary
# ---------------------------------------------------------------------------

CANARY = {"image": "img:v3", "replicas": 2, "weight": 10}


@pytest.mark.parametrize(
    ("rollout", "expected_ready", "expected_state"),
    [
        # The template changed to v3; the controller has not acted on it yet.
        ({"observed": 1, "replicas": 2, "updated": 2, "ready": 2}, 0, "creating"),
        # Two new pods surged in beside the two Ready v2 pods, not Ready yet.
        ({"replicas": 4, "updated": 2, "ready": 2}, 0, "creating"),
        # One v3 pod Ready, one v2 pod left (counted as Ready).
        ({"replicas": 3, "updated": 2, "ready": 2}, 1, "creating"),
        # The v2 pods are gone and both v3 pods are Ready.
        ({"replicas": 2, "updated": 2, "ready": 2}, 2, "running"),
    ],
    ids=["unobserved", "surged-unready", "half-rolled", "rolled-out"],
)
def test_canary_counts_only_pods_of_its_current_template(
    rollout: dict[str, int], expected_ready: int, expected_state: str
) -> None:
    monitor = _make_monitor()
    deployment = _wire_deployment("ep-canary", "img:v3", spec_replicas=2, **rollout)
    with (
        patch.object(monitor, "_get_deployment", return_value=deployment),
        patch.object(monitor, "_ensure_service"),
        patch.object(monitor, "_converge_endpoint_tls_proxy"),
        patch.object(monitor, "_update_deployment_image") as update_image,
        patch.object(monitor, "_scale_deployment") as scale,
    ):
        status = monitor._reconcile_canary("ep", NAMESPACE, {"image": "img:v1"}, CANARY, {})

    update_image.assert_not_called()
    scale.assert_not_called()
    assert (status["state"], status["replicas_ready"]) == (expected_state, expected_ready)


# ---------------------------------------------------------------------------
# Primary
# ---------------------------------------------------------------------------


def _endpoint(**updates: Any) -> dict[str, Any]:
    endpoint: dict[str, Any] = {
        "endpoint_name": "ep",
        "lifecycle_id": LIFECYCLE,
        "desired_state": "deploying",
        "target_regions": [REGION],
        "cleanup_regions": [REGION],
        "region_generations": {REGION: "region-1"},
        "updated_at": "2026-01-01T00:00:00+00:00",
        "namespace": NAMESPACE,
        "spec": {"image": "img:v2", "replicas": 2},
        "region_status": {},
    }
    endpoint.update(updates)
    return endpoint


async def _reconcile_primary(
    store: MagicMock, deployment: client.V1Deployment, endpoint: dict[str, Any]
) -> None:
    monitor = _make_monitor(store)
    with (
        patch.object(monitor, "_get_deployment", return_value=deployment),
        patch.object(monitor, "_ensure_service"),
        patch.object(monitor, "_ensure_tls_proxy_configmap"),
        patch.object(monitor, "_converge_endpoint_tls_proxy"),
        patch.object(monitor, "_cleanup_canary"),
        patch.object(monitor, "_delete_autoscalers", return_value=ResourceCleanupResult()),
    ):
        assert await monitor._reconcile_running("ep", NAMESPACE, endpoint["spec"], endpoint) is None


@pytest.mark.asyncio
async def test_old_pods_keep_the_region_serving_but_do_not_promote_the_update() -> None:
    """After update-image the v1 pods still serve; the endpoint stays ``deploying``."""
    store = MagicMock()
    deployment = _wire_deployment("ep", "img:v2", spec_replicas=2, replicas=3, updated=1, ready=2)

    await _reconcile_primary(store, deployment, _endpoint())

    status = store.update_region_status.call_args
    assert status.args[2] == "running"
    assert status.kwargs["replicas_ready"] == 2
    assert status.kwargs["extra"] == {"rollout_complete": False}
    store.update_desired_state.assert_not_called()


@pytest.mark.asyncio
async def test_a_template_the_controller_has_not_observed_is_not_rolled_out() -> None:
    store = MagicMock()
    deployment = _wire_deployment(
        "ep", "img:v2", spec_replicas=2, observed=1, replicas=2, updated=2, ready=2
    )

    await _reconcile_primary(store, deployment, _endpoint())

    assert store.update_region_status.call_args.kwargs["extra"] == {"rollout_complete": False}
    store.update_desired_state.assert_not_called()


@pytest.mark.asyncio
async def test_the_endpoint_is_promoted_once_the_current_template_is_ready() -> None:
    store = MagicMock()
    deployment = _wire_deployment("ep", "img:v2", spec_replicas=2, replicas=2, updated=2, ready=2)

    await _reconcile_primary(store, deployment, _endpoint())

    status = store.update_region_status.call_args
    assert status.args[2] == "running"
    assert status.kwargs["extra"] is None
    store.update_desired_state.assert_called_once_with(
        "ep", "running", expected_lifecycle_id=LIFECYCLE, expected_desired_state="deploying"
    )


@pytest.mark.asyncio
async def test_promotion_waits_for_another_region_still_rolling_out() -> None:
    store = MagicMock()
    deployment = _wire_deployment("ep", "img:v2", spec_replicas=2, replicas=2, updated=2, ready=2)
    endpoint = _endpoint(
        target_regions=[REGION, "eu-west-1"],
        region_status={"eu-west-1": {"state": "running", "rollout_complete": False}},
    )

    await _reconcile_primary(store, deployment, endpoint)

    store.update_desired_state.assert_not_called()
