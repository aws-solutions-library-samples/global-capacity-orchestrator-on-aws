"""Which worker Deployments a reconcile materializes for each serving mode.

A reconcile of an endpoint that carries a ``mooncake`` block fans the endpoint
out into worker Deployments whose shape is decided entirely by the serving
mode:

- ``disaggregated`` and ``both`` split the work across two Deployments,
  ``{name}-prefill`` and ``{name}-decode``, and never stand up a single
  combined worker.
- ``store`` runs a single combined worker under the bare endpoint name whose
  KV-transfer role is ``kv_both``, and never splits into prefill/decode.

The example below drives :meth:`InferenceMonitor._reconcile_mooncake` against a
fake Kubernetes API that records every Deployment create, with the surrounding
resolution, master-gating, front-end, and status steps stubbed so only the
mode-driven worker materialization is exercised. It then checks that the set of
created worker Deployments matches the mode exactly.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st
from kubernetes.client.rest import ApiException


class _RecordingK8s:
    """A minimal apps API double that records Deployment creates.

    Deployments are keyed by ``(namespace, name)``. A read of an unknown name
    raises ``404`` exactly as the live API server does, so the monitor's
    create-if-absent path always reaches the create. Every create is recorded
    so a test can assert precisely which workers were materialized.
    """

    def __init__(self) -> None:
        self.deployments: dict[tuple[str, str], Any] = {}

    def read_namespaced_deployment(self, name, namespace, **_kwargs):
        key = (namespace, name)
        if key not in self.deployments:
            raise ApiException(status=404, reason="Not Found")
        return self.deployments[key]

    def create_namespaced_deployment(self, namespace, body, **_kwargs):
        self.deployments[(namespace, body.metadata.name)] = body


def _make_monitor(region: str = "us-east-1"):
    """Build an :class:`InferenceMonitor` with every K8s client mocked out."""
    with (
        patch("gco.services.inference_monitor.config.load_incluster_config"),
        patch("gco.services.inference_monitor.client.AppsV1Api"),
        patch("gco.services.inference_monitor.client.CoreV1Api"),
        patch("gco.services.inference_monitor.client.NetworkingV1Api"),
        patch("gco.services.inference_monitor.client.AutoscalingV2Api"),
    ):
        from gco.services.inference_monitor import InferenceMonitor

        return InferenceMonitor(
            cluster_id="test-cluster",
            region=region,
            store=MagicMock(),
            namespace="gco-inference",
            reconcile_interval=5,
        )


def _build_spec(mode: str, prefill: int, decode: int, protocol: str) -> dict[str, Any]:
    """Assemble a minimal endpoint spec carrying a ``mooncake`` block."""
    mooncake: dict[str, Any] = {
        "mode": mode,
        "transfer": {"protocol": protocol, "device_name": ""},
    }
    if mode in ("disaggregated", "both"):
        mooncake["topology"] = {"prefill": prefill, "decode": decode}
    if mode in ("store", "both"):
        mooncake["store"] = {"enabled": True}
    return {
        "image": "example/mooncake-vllm:pinned",
        "port": 8000,
        "gpu_count": 1,
        "mooncake": mooncake,
    }


def _reconcile_workers(mode: str, prefill: int, decode: int, protocol: str):
    """Drive a reconcile for one spec and return the created worker names.

    The resolution, master-gating, front-end, and status steps are stubbed so
    the reconcile reaches worker materialization unconditionally; only the
    mode-driven Deployment creates are exercised and observed.
    """
    from gco.services import inference_monitor as mod

    monitor = _make_monitor()
    fake = _RecordingK8s()
    monitor.apps_v1 = fake

    region_services = {
        "metadata_server": "http://mooncake-master:8080/metadata",
        "master_server_address": "mooncake-master:50051",
        "cold_tier_s3_uri": "s3://example-bucket/mooncake-kv",
    }

    # Hold every step except mode-driven worker materialization constant.
    monitor._resolve_region_services = lambda *_a, **_k: mod.RegionServicesResolution(
        region_services=region_services
    )
    monitor._resolve_regional_scope = lambda *_a, **_k: mod.RegionalScopeResolution(in_region=True)
    monitor._gate_on_mooncake_master = lambda *_a, **_k: mod.MasterReadinessGate(proceed=True)
    monitor._ensure_mooncake_configmap = lambda *_a, **_k: None
    monitor._create_pd_proxy = lambda *_a, **_k: None
    monitor._create_service = lambda *_a, **_k: None
    monitor._ensure_ingress = lambda *_a, **_k: None
    monitor._create_role_hpa = lambda *_a, **_k: None
    monitor._report_role_status = lambda *_a, **_k: "creating"

    spec = _build_spec(mode, prefill, decode, protocol)
    endpoint = {"name": "endpoint", "spec": spec}

    asyncio.run(monitor._reconcile_mooncake("endpoint", "gco-inference", spec, endpoint))

    created = {name for (_ns, name) in fake.deployments}
    return created, fake.deployments


def _kv_role_of(deployment) -> str | None:
    """Read the ``kv_role`` from a worker's ``--kv-transfer-config`` argument."""
    container = deployment.spec.template.spec.containers[0]
    args = container.args or []
    if "--kv-transfer-config" not in args:
        return None
    cfg = json.loads(args[args.index("--kv-transfer-config") + 1])
    return cfg.get("kv_role")


@settings(max_examples=60, deadline=None)
@given(
    mode=st.sampled_from(["disaggregated", "both", "store"]),
    prefill=st.integers(min_value=1, max_value=4),
    decode=st.integers(min_value=1, max_value=4),
    protocol=st.sampled_from(["tcp", "rdma"]),
)
def test_split_workers_iff_disaggregated_or_both_else_single_combined(
    mode: str, prefill: int, decode: int, protocol: str
) -> None:
    """Split prefill/decode for disaggregated and both; one combined for store.

    For the splitting modes the reconcile materializes exactly the two role
    workers and no bare combined worker. For store it materializes exactly one
    combined worker under the endpoint name, whose KV-transfer role is
    ``kv_both``, and neither role-split worker.
    """
    created, deployments = _reconcile_workers(mode, prefill, decode, protocol)

    split = {"endpoint-prefill", "endpoint-decode"}
    combined = "endpoint"

    if mode in ("disaggregated", "both"):
        assert created == split
        assert combined not in created
    else:
        assert created == {combined}
        assert split.isdisjoint(created)
        assert _kv_role_of(deployments[("gco-inference", combined)]) == "kv_both"
