"""Endpoints without a ``mooncake`` block keep their classic workload shape.

An endpoint spec that carries no ``mooncake`` block is an ordinary inference
endpoint and reconciles to one Deployment at the configured replica count and
one ClusterIP Service. It deliberately has no endpoint-specific Ingress: all
public requests traverse the shared platform Ingress and authenticated manifest
processor before reaching that Service. No role-split prefill/decode
Deployments, prefill-decode proxy, per-role autoscaler, or shared per-region
master StatefulSet may appear.

:meth:`InferenceMonitor._reconcile_mooncake` is the branch that recognises the
distributed shape; for a plain spec it returns ``None`` to hand control back to
the single-instance path. :meth:`InferenceMonitor._reconcile_running` is that
path, and on a first reconcile (no Deployment yet) it creates the Deployment
and Service while removing any historical direct Ingress.

These checks generate a wide spread of plain specs — varied images, replica
counts, ports, and accelerators — and confirm that for every one of them the
distributed branch declines and the single-instance reconcile materialises
precisely one Deployment and one Service, with no direct Ingress or distributed
extras.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st
from kubernetes.client.rest import ApiException

OWN_REGION = "us-east-1"
NAMESPACE = "gco-inference"

# A mix of server images: vLLM/TGI take the root-path serving branch while
# plain images do not, so both container argument shapes are exercised.
IMAGES = [
    "vllm/vllm-openai:v0.6.0",
    "ghcr.io/huggingface/text-generation-inference:2.0",
    "tgi:latest",
    "myregistry/custom-model:1.2.3",
    "123456789012.dkr.ecr.us-east-1.amazonaws.com/models/llama:prod",
]

ACCELERATORS = ["nvidia", "neuron"]


def _make_monitor(region: str = OWN_REGION):
    """Build an :class:`InferenceMonitor` with every K8s client mocked out.

    The Deployment read is wired to report a 404 so the single-instance
    reconcile takes its first-create branch rather than its update branch.
    """
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

    # No Deployment exists yet — drive the first-create path.
    monitor.apps_v1.read_namespaced_deployment.side_effect = ApiException(status=404)
    return monitor


@st.composite
def _plain_endpoints(draw):
    """Generate an endpoint spec that carries no ``mooncake`` block.

    The spec varies the image, replica count, port, and accelerator, and never
    enables the legacy autoscaling field or a canary so the expected output is
    strictly the one-each object set.
    """
    name = draw(st.sampled_from(["ep", "endpoint", "model", "svc", "demo", "llama"]))
    spec = {
        "image": draw(st.sampled_from(IMAGES)),
        "replicas": draw(st.integers(min_value=1, max_value=8)),
        "accelerator": draw(st.sampled_from(ACCELERATORS)),
        "gpu_count": draw(st.integers(min_value=0, max_value=4)),
    }
    if draw(st.booleans()):
        spec["port"] = draw(st.integers(min_value=8000, max_value=9000))
    if draw(st.booleans()):
        spec["health_check_path"] = draw(st.sampled_from(["/health", "/healthz", "/ping"]))

    endpoint = {
        "endpoint_name": name,
        "desired_state": "deploying",
        "target_regions": [OWN_REGION],
        "spec": spec,
        "namespace": NAMESPACE,
    }
    return {"name": name, "spec": spec, "endpoint": endpoint}


@settings(max_examples=150, deadline=None)
@given(bundle=_plain_endpoints())
def test_plain_spec_declines_the_distributed_branch(bundle: dict) -> None:
    """The distributed branch hands a plain spec back to the single-instance path.

    With no ``mooncake`` block the branch returns ``None``, which is the signal
    that nothing distributed should be materialised and the classic path owns
    the endpoint.
    """
    monitor = _make_monitor()

    result = asyncio.run(
        monitor._reconcile_mooncake(bundle["name"], NAMESPACE, bundle["spec"], bundle["endpoint"])
    )

    assert result is None
    # Declining means it touched no Kubernetes objects of its own.
    monitor.apps_v1.create_namespaced_deployment.assert_not_called()
    monitor.apps_v1.create_namespaced_stateful_set.assert_not_called()
    monitor.core_v1.create_namespaced_service.assert_not_called()
    monitor.networking_v1.create_namespaced_ingress.assert_not_called()


@settings(max_examples=150, deadline=None)
@given(bundle=_plain_endpoints())
def test_plain_spec_reconciles_to_one_deployment_and_service(bundle: dict) -> None:
    """A plain endpoint creates one Deployment and one internal Service.

    On a first reconcile the single-instance path creates exactly those two
    workload objects, removes the historical endpoint Ingress if present, and
    adds none of the distributed extras: no role-split Deployments, proxy,
    per-role autoscaler, or shared master StatefulSet.
    """
    monitor = _make_monitor()

    with patch("gco.services.inference_monitor.client.AutoscalingV2Api") as mock_hpa_api:
        result = asyncio.run(
            monitor._reconcile_running(
                bundle["name"], NAMESPACE, bundle["spec"], bundle["endpoint"]
            )
        )

    # The first reconcile reports a create action for this endpoint.
    assert result is not None
    assert result["action"] == "create"
    assert result["endpoint"] == bundle["name"]

    # Exactly one Deployment and one ClusterIP Service are materialized. The
    # old direct Ingress is removed rather than recreated.
    assert monitor.apps_v1.create_namespaced_deployment.call_count == 1
    assert monitor.core_v1.create_namespaced_service.call_count == 1
    monitor.networking_v1.create_namespaced_ingress.assert_not_called()
    monitor.networking_v1.patch_namespaced_ingress.assert_not_called()
    delete_args = monitor.networking_v1.delete_namespaced_ingress.call_args.args
    assert delete_args[:2] == (f"inference-{bundle['name']}", NAMESPACE)

    # The single created Deployment carries the endpoint's configured replicas.
    _, created = monitor.apps_v1.create_namespaced_deployment.call_args[0][:2]
    assert created.spec.replicas == bundle["spec"]["replicas"]
    assert created.metadata.name == bundle["name"]

    # None of the distributed extras are materialised.
    monitor.apps_v1.create_namespaced_stateful_set.assert_not_called()
    mock_hpa_api.return_value.create_namespaced_horizontal_pod_autoscaler.assert_not_called()
