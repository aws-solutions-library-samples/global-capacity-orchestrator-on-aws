"""Readiness counts only pods of the Deployment's current template.

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


# ---------------------------------------------------------------------------
# Mooncake roles
# ---------------------------------------------------------------------------

SPLIT = {"mode": "disaggregated", "topology": {"prefill": 1, "decode": 2}}


def _rolled_out(name: str, image: str, replicas: int) -> client.V1Deployment:
    return _wire_deployment(
        name, image, spec_replicas=replicas, replicas=replicas, updated=replicas, ready=replicas
    )


def _split_deployments(**overrides: client.V1Deployment) -> dict[str, client.V1Deployment]:
    """A split endpoint's role and proxy Deployments, rolled out unless overridden."""
    deployments = {
        "ep-prefill": _rolled_out("ep-prefill", "img:v2", 1),
        "ep-decode": _rolled_out("ep-decode", "img:v2", 2),
        "ep-proxy": _rolled_out("ep-proxy", "img:v1", 1),
    }
    deployments.update(overrides)
    return deployments


def _report(
    mooncake: dict[str, Any], deployments: dict[str, client.V1Deployment]
) -> tuple[Any, MagicMock]:
    store = MagicMock()
    monitor = _make_monitor(store)
    with patch.object(monitor, "_get_deployment", side_effect=lambda n, _ns: deployments.get(n)):
        report = monitor._report_role_status("ep", NAMESPACE, mooncake, {}, _endpoint())
    return report, store


@pytest.mark.parametrize(
    "rolling",
    [
        {
            "ep-decode": _wire_deployment(
                "ep-decode", "img:v2", spec_replicas=2, replicas=3, updated=1, ready=2
            )
        },
        {
            "ep-prefill": _wire_deployment(
                "ep-prefill",
                "img:v2",
                spec_replicas=1,
                generation=3,
                observed=2,
                replicas=1,
                updated=1,
                ready=1,
            )
        },
        {
            "ep-proxy": _wire_deployment(
                "ep-proxy", "img:v2", spec_replicas=1, replicas=2, updated=1, ready=1
            )
        },
    ],
    ids=["decode-half-rolled", "prefill-unobserved", "proxy-surged"],
)
def test_a_role_still_rolling_out_serves_but_is_not_rolled_out(
    rolling: dict[str, client.V1Deployment],
) -> None:
    """After update-image the old role pods keep serving; the update is not applied yet."""
    report, store = _report(SPLIT, _split_deployments(**rolling))

    assert (report.state, report.rollout_complete) == ("running", False)
    status = store.update_region_status.call_args
    assert status.args[2] == "running"
    assert status.kwargs["extra"]["rollout_complete"] is False


def test_rolled_out_roles_report_the_rollout_complete() -> None:
    report, store = _report(SPLIT, _split_deployments())

    assert (report.state, report.rollout_complete) == ("running", True)
    assert "rollout_complete" not in store.update_region_status.call_args.kwargs["extra"]


@pytest.mark.parametrize(
    ("deployment", "rollout_complete"),
    [
        (_wire_deployment("ep", "img:v2", spec_replicas=1, replicas=2, updated=1, ready=1), False),
        (_rolled_out("ep", "img:v2", 1), True),
    ],
    ids=["rolling", "rolled-out"],
)
def test_a_store_endpoint_counts_its_current_template(
    deployment: client.V1Deployment, rollout_complete: bool
) -> None:
    report, store = _report({"mode": "store"}, {"ep": deployment})

    assert (report.state, report.rollout_complete) == ("running", rollout_complete)
    extra = store.update_region_status.call_args.kwargs["extra"]
    assert extra == ({"rollout_complete": False} if not rollout_complete else None)


def test_a_creating_region_carries_no_rollout_flag() -> None:
    """The flag qualifies a serving region only; a creating one is not promoted anyway."""
    unready = _wire_deployment(
        "ep-decode", "img:v2", spec_replicas=2, replicas=2, updated=2, ready=1
    )

    report, store = _report(SPLIT, _split_deployments(**{"ep-decode": unready}))

    assert (report.state, report.rollout_complete) == ("creating", False)
    assert "rollout_complete" not in store.update_region_status.call_args.kwargs["extra"]
