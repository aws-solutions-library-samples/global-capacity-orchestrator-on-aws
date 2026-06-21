"""EFA fabric placement for RDMA KV-transfer role pods.

When a disaggregated endpoint moves KV cache over RoCE, its role pods must land
on EFA-enabled nodes. Those nodes carry a ``vpc.amazonaws.com/efa`` taint,
advertise the ``vpc.amazonaws.com/efa`` extended resource, and are labelled
``efa=true``. :func:`gco.services.inference_monitor.apply_efa_scheduling`
mutates a pod spec in place to satisfy all three while leaving the pod's
existing GPU asks untouched.

This module checks the contract that holds for every RDMA spec: each role pod
ends up tolerating the EFA taint, selecting ``efa=true`` and ``mooncake-efa=true``
(the latter pinning it to the dedicated mooncake EFA pool that excludes the
A100-40GB p4d family), requesting at least one EFA device, and keeping the GPU
request and limit it started with. It also checks the complementary case — a
transfer protocol explicitly set to something other than RDMA leaves the pod's
tolerations, node selector, and resource asks exactly as they were — and the
default case — a spec that omits the protocol (or the whole ``transfer`` block)
defaults to RDMA and is placed on EFA like any other RDMA spec.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given
from hypothesis import strategies as st
from kubernetes import client

from gco.services.inference_monitor import (
    EFA_NODE_SELECTOR_KEY,
    EFA_NODE_SELECTOR_VALUE,
    EFA_RESOURCE_NAME,
    MOONCAKE_EFA_NODE_SELECTOR_KEY,
    MOONCAKE_EFA_NODE_SELECTOR_VALUE,
    apply_efa_scheduling,
)

# Accelerators a role pod may already ask for; the EFA request must sit
# alongside these without disturbing them.
_GPU_RESOURCE_KEYS = ("nvidia.com/gpu", "aws.amazon.com/neuron")


@st.composite
def _gpu_container(draw: st.DrawFn, index: int) -> client.V1Container:
    """Draw a container that already requests a GPU/Neuron accelerator.

    The accelerator count is recorded on both ``requests`` and ``limits`` so the
    test can later confirm those exact values survive EFA scheduling.
    """
    gpu_key = draw(st.sampled_from(_GPU_RESOURCE_KEYS))
    gpu_count = str(draw(st.integers(min_value=1, max_value=8)))
    return client.V1Container(
        name=f"worker-{index}",
        image="example/vllm:pinned",
        resources=client.V1ResourceRequirements(
            requests={gpu_key: gpu_count},
            limits={gpu_key: gpu_count},
        ),
    )


@st.composite
def _rdma_scenario(draw: st.DrawFn) -> dict[str, Any]:
    """Draw an RDMA mooncake block plus a pod spec carrying GPU containers.

    The mooncake block always sets ``transfer.protocol`` to ``rdma`` and may
    carry an arbitrary RDMA device name. The pod spec holds one or more
    GPU-requesting containers and may already carry unrelated tolerations and
    node-selector entries, which EFA scheduling must preserve.
    """
    device_name = draw(st.sampled_from(["", "mlx5_0", "mlx5_1", "rdma0"]))
    mooncake = {"transfer": {"protocol": "rdma", "device_name": device_name}}

    container_count = draw(st.integers(min_value=1, max_value=4))
    containers = [draw(_gpu_container(i)) for i in range(container_count)]

    # Optional pre-existing tolerations (e.g. the GPU toleration) that must
    # remain in place after EFA scheduling.
    existing_tolerations = draw(
        st.lists(
            st.sampled_from(["nvidia.com/gpu", "node.kubernetes.io/unschedulable"]),
            max_size=2,
            unique=True,
        )
    )
    tolerations = [
        client.V1Toleration(key=key, operator="Exists", effect="NoSchedule")
        for key in existing_tolerations
    ]

    # Optional pre-existing node selectors that must survive the merge.
    existing_selector = draw(
        st.dictionaries(
            st.sampled_from(["zone", "instance-type", "capacity-type"]),
            st.text(
                alphabet="abcdefghijklmnopqrstuvwxyz0123456789-",
                min_size=1,
                max_size=8,
            ),
            max_size=3,
        )
    )

    pod_spec = client.V1PodSpec(
        containers=containers,
        tolerations=tolerations or None,
        node_selector=dict(existing_selector) or None,
    )

    # Snapshot the GPU asks before mutation so the test can assert they survive.
    gpu_snapshot = [
        {
            "requests": dict(c.resources.requests),
            "limits": dict(c.resources.limits),
        }
        for c in containers
    ]

    return {
        "mooncake": mooncake,
        "pod_spec": pod_spec,
        "gpu_snapshot": gpu_snapshot,
        "existing_tolerations": existing_tolerations,
        "existing_selector": existing_selector,
    }


def _gpu_asks(resources: client.V1ResourceRequirements) -> dict[str, dict[str, str]]:
    """Extract only the GPU/Neuron entries from a container's resources."""
    requests = resources.requests or {}
    limits = resources.limits or {}
    return {
        "requests": {k: v for k, v in requests.items() if k in _GPU_RESOURCE_KEYS},
        "limits": {k: v for k, v in limits.items() if k in _GPU_RESOURCE_KEYS},
    }


@given(scenario=_rdma_scenario())
def test_rdma_pod_lands_on_efa_fabric_and_keeps_gpu_asks(scenario: dict[str, Any]) -> None:
    """An RDMA role pod tolerates EFA, selects it, asks for it, and keeps GPUs.

    For any RDMA transfer spec, every role pod ends up tolerating the EFA taint,
    carrying the ``efa=true`` node selector, requesting at least one EFA device,
    and retaining each container's original GPU request and limit unchanged.
    Pre-existing tolerations and node-selector entries are preserved.
    """
    mooncake = scenario["mooncake"]
    pod_spec = scenario["pod_spec"]
    gpu_snapshot = scenario["gpu_snapshot"]

    apply_efa_scheduling(mooncake, pod_spec)

    # Tolerates the EFA taint, in addition to anything it already tolerated.
    toleration_keys = {t.key for t in (pod_spec.tolerations or [])}
    assert EFA_RESOURCE_NAME in toleration_keys
    for existing in scenario["existing_tolerations"]:
        assert existing in toleration_keys

    # Selects EFA-labelled nodes, keeping any selectors it already had.
    assert pod_spec.node_selector[EFA_NODE_SELECTOR_KEY] == EFA_NODE_SELECTOR_VALUE
    # Also pins to the dedicated mooncake-efa pool (excludes A100-40GB p4d).
    assert (
        pod_spec.node_selector[MOONCAKE_EFA_NODE_SELECTOR_KEY] == MOONCAKE_EFA_NODE_SELECTOR_VALUE
    )
    for key, value in scenario["existing_selector"].items():
        assert pod_spec.node_selector[key] == value

    # Every container asks for at least one EFA device and keeps its GPU asks.
    for container, snapshot in zip(pod_spec.containers, gpu_snapshot, strict=True):
        requests = container.resources.requests or {}
        limits = container.resources.limits or {}
        assert int(requests[EFA_RESOURCE_NAME]) >= 1
        assert int(limits[EFA_RESOURCE_NAME]) >= 1
        assert _gpu_asks(container.resources) == {
            "requests": snapshot["requests"],
            "limits": snapshot["limits"],
        }


@given(
    protocol=st.sampled_from(["tcp", "TCP", "rocev2", "", "ib"]),
    container_count=st.integers(min_value=1, max_value=4),
)
def test_non_rdma_pod_gets_no_efa_scheduling(protocol: str, container_count: int) -> None:
    """A non-RDMA transfer leaves the pod's scheduling and resources untouched.

    When the transfer protocol is anything other than ``rdma``, the pod gains no
    EFA toleration, no ``efa`` node selector, and no EFA device request; its
    GPU asks are likewise unchanged.
    """
    containers = [
        client.V1Container(
            name=f"worker-{i}",
            image="example/vllm:pinned",
            resources=client.V1ResourceRequirements(
                requests={"nvidia.com/gpu": "1"},
                limits={"nvidia.com/gpu": "1"},
            ),
        )
        for i in range(container_count)
    ]
    pod_spec = client.V1PodSpec(containers=containers)

    apply_efa_scheduling({"transfer": {"protocol": protocol}}, pod_spec)

    toleration_keys = {t.key for t in (pod_spec.tolerations or [])}
    assert EFA_RESOURCE_NAME not in toleration_keys
    assert EFA_NODE_SELECTOR_KEY not in (pod_spec.node_selector or {})
    assert MOONCAKE_EFA_NODE_SELECTOR_KEY not in (pod_spec.node_selector or {})
    for container in pod_spec.containers:
        requests = container.resources.requests or {}
        limits = container.resources.limits or {}
        assert EFA_RESOURCE_NAME not in requests
        assert EFA_RESOURCE_NAME not in limits
        assert requests == {"nvidia.com/gpu": "1"}
        assert limits == {"nvidia.com/gpu": "1"}


@given(container_count=st.integers(min_value=1, max_value=4))
def test_absent_protocol_defaults_to_rdma_and_lands_on_efa(container_count: int) -> None:
    """A spec with no ``transfer`` block defaults to RDMA and lands on EFA.

    This is the common CLI-deployed disaggregated case: the deploy path writes
    only ``{mode, topology}`` with no ``transfer`` block. The rest of the
    Mooncake path treats an unset protocol as ``rdma`` (the shared master is
    still required, and the transport config renders ``rdma``), so EFA placement
    must match — otherwise role pods schedule onto plain GPU nodes and never
    reach the EFA fabric the connector needs.
    """
    containers = [
        client.V1Container(
            name=f"worker-{i}",
            image="example/vllm:pinned",
            resources=client.V1ResourceRequirements(
                requests={"nvidia.com/gpu": "1"},
                limits={"nvidia.com/gpu": "1"},
            ),
        )
        for i in range(container_count)
    ]
    pod_spec = client.V1PodSpec(containers=containers)

    # No "transfer" block at all — the protocol must default to rdma.
    apply_efa_scheduling({"mode": "disaggregated"}, pod_spec)

    toleration_keys = {t.key for t in (pod_spec.tolerations or [])}
    assert EFA_RESOURCE_NAME in toleration_keys
    assert pod_spec.node_selector[EFA_NODE_SELECTOR_KEY] == EFA_NODE_SELECTOR_VALUE
    assert (
        pod_spec.node_selector[MOONCAKE_EFA_NODE_SELECTOR_KEY] == MOONCAKE_EFA_NODE_SELECTOR_VALUE
    )
    for container in pod_spec.containers:
        requests = container.resources.requests or {}
        limits = container.resources.limits or {}
        assert int(requests[EFA_RESOURCE_NAME]) >= 1
        assert int(limits[EFA_RESOURCE_NAME]) >= 1


def test_transfer_block_without_protocol_key_defaults_to_rdma() -> None:
    """A ``transfer`` block that omits ``protocol`` still defaults to RDMA -> EFA.

    A device name may be supplied without spelling out the protocol; the absent
    protocol key must still resolve to ``rdma`` and place the pod on EFA.
    """
    pod_spec = client.V1PodSpec(
        containers=[
            client.V1Container(
                name="worker-0",
                image="example/vllm:pinned",
                resources=client.V1ResourceRequirements(
                    requests={"nvidia.com/gpu": "1"},
                    limits={"nvidia.com/gpu": "1"},
                ),
            )
        ]
    )

    apply_efa_scheduling({"transfer": {"device_name": "mlx5_0"}}, pod_spec)

    assert EFA_RESOURCE_NAME in {t.key for t in (pod_spec.tolerations or [])}
    assert pod_spec.node_selector[EFA_NODE_SELECTOR_KEY] == EFA_NODE_SELECTOR_VALUE
    assert (
        pod_spec.node_selector[MOONCAKE_EFA_NODE_SELECTOR_KEY] == MOONCAKE_EFA_NODE_SELECTOR_VALUE
    )
    container = pod_spec.containers[0]
    assert int((container.resources.requests or {})[EFA_RESOURCE_NAME]) >= 1
    assert int((container.resources.limits or {})[EFA_RESOURCE_NAME]) >= 1
