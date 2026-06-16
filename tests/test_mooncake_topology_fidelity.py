"""Materialized role replica counts mirror the requested topology.

A split-mode disaggregated endpoint carries a ``topology`` block naming how many
prefill workers and how many decode workers it wants. When no autoscaler owns a
role's count, the monitor must stamp those exact numbers onto the role
Deployments it lays down: the ``{name}-prefill`` Deployment is created with
``topology.prefill`` replicas and the ``{name}-decode`` Deployment with
``topology.decode`` replicas.

This pins that fidelity across a wide range of generated topologies. Each role
is reconciled from scratch (no Deployment yet exists), the created Deployment
object is captured through a mocked apps client, and its replica count is
compared against the topology value it should reflect.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st

# Modes that split work across prefill and decode role Deployments.
_SPLIT_MODES = ("disaggregated", "both")


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
            namespace="gco-inference",
            reconcile_interval=5,
        )
        monitor.apps_v1 = mock_apps.return_value
        monitor.core_v1 = mock_core.return_value
        monitor.networking_v1 = mock_net.return_value
        return monitor


def _created_replicas_for_role(monitor, name: str, spec: dict[str, Any], role: str) -> int:
    """Reconcile one role from scratch and return its materialized replica count.

    The role's Deployment does not yet exist, so the reconcile pass creates it.
    The created ``V1Deployment`` object is captured from the mocked apps client
    and its requested replica count returned.
    """
    create = monitor.apps_v1.create_namespaced_deployment
    create.reset_mock()
    # No Deployment exists yet: force the create-if-absent branch.
    with patch.object(monitor, "_get_deployment", return_value=None):
        monitor._ensure_role_deployment(name, "gco-inference", spec, role)

    assert create.call_count == 1
    created = create.call_args.args[1]
    return created.spec.replicas


@st.composite
def _split_spec(draw: st.DrawFn) -> dict[str, Any]:
    """A split-mode endpoint spec with an explicit prefill/decode topology.

    No autoscaling block is included, so the role replica counts come straight
    from the topology rather than an autoscaler's lower bound.
    """
    mode = draw(st.sampled_from(_SPLIT_MODES))
    topology = {
        "prefill": draw(st.integers(min_value=1, max_value=1000)),
        "decode": draw(st.integers(min_value=1, max_value=1000)),
    }
    return {
        "image": "vllm/vllm-openai:test",
        "mooncake": {"mode": mode, "topology": topology},
    }


@settings(max_examples=100)
@given(
    spec=_split_spec(),
    name=st.from_regex(r"[a-z][a-z0-9-]{0,12}", fullmatch=True),
)
def test_role_deployments_use_topology_replica_counts(spec, name):
    """Prefill/decode Deployments are stamped with their topology replica counts.

    With no autoscaler owning either role, the created ``{name}-prefill``
    Deployment carries ``topology.prefill`` replicas and the created
    ``{name}-decode`` Deployment carries ``topology.decode`` replicas.
    """
    monitor = _make_monitor()
    topology = spec["mooncake"]["topology"]

    prefill_replicas = _created_replicas_for_role(monitor, name, spec, "prefill")
    decode_replicas = _created_replicas_for_role(monitor, name, spec, "decode")

    assert prefill_replicas == topology["prefill"]
    assert decode_replicas == topology["decode"]
