"""Idempotent maintenance of the shared per-region Mooncake master.

The master is region-shared, not per-endpoint: every endpoint that needs the
key-value store reaches the same ``mooncake-master`` StatefulSet and the headless
Service fronting its RPC and metadata ports. The monitor maintains it with
create-if-absent semantics, so repeated reconciliation in a region must converge
on exactly one StatefulSet running a single replica — and must never stand up a
separate metadata Deployment, since the metadata server is the built-in HTTP
server inside the master process.

The example below drives :meth:`InferenceMonitor._ensure_mooncake_store` an
arbitrary number of times against a fake Kubernetes API that mimics the real
create-if-absent behavior (a second create of an existing object raises a
``409 Conflict``), and checks the converged shape.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st
from kubernetes.client.rest import ApiException


class _CreateIfAbsentK8s:
    """A minimal Kubernetes API double with create-if-absent semantics.

    Objects are keyed by ``(namespace, name)``. The first create of a key
    records the body; any later create of the same key raises a ``409``, exactly
    as the real API server does when the object already exists. Deployment
    creates are recorded so a test can assert none were ever attempted.
    """

    def __init__(self) -> None:
        self.statefulsets: dict[tuple[str, str], Any] = {}
        self.services: dict[tuple[str, str], Any] = {}
        self.deployments: dict[tuple[str, str], Any] = {}

    def create_namespaced_stateful_set(self, namespace, body, **_kwargs):
        key = (namespace, body.metadata.name)
        if key in self.statefulsets:
            raise ApiException(status=409, reason="Conflict")
        self.statefulsets[key] = body

    def create_namespaced_service(self, namespace, body, **_kwargs):
        key = (namespace, body.metadata.name)
        if key in self.services:
            raise ApiException(status=409, reason="Conflict")
        self.services[key] = body

    def create_namespaced_deployment(self, namespace, body, **_kwargs):
        key = (namespace, body.metadata.name)
        self.deployments[key] = body


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


@settings(max_examples=50, deadline=None)
@given(call_count=st.integers(min_value=1, max_value=25))
def test_repeated_calls_converge_on_one_master_and_no_metadata_deployment(
    call_count: int,
) -> None:
    """Any number of calls keep one single-replica master and no Deployment.

    The fake API shares one backing store between the apps and core clients so
    the second and later creates collide with a ``409`` just like the live
    cluster. After driving the call an arbitrary number of times, exactly one
    ``mooncake-master`` StatefulSet exists with a single replica, exactly one
    headless Service exists, and no Deployment was ever created for the metadata
    server.
    """
    monitor = _make_monitor()
    fake = _CreateIfAbsentK8s()
    monitor.apps_v1 = fake
    monitor.core_v1 = fake

    namespace = "gco-inference"
    spec = {
        "mooncake": {
            "mode": "store",
            "store": {"enabled": True, "master_image": "example/mooncake-master:pinned"},
        }
    }

    for _ in range(call_count):
        monitor._ensure_mooncake_store(namespace, spec)

    masters = [body for (_ns, name), body in fake.statefulsets.items() if name == "mooncake-master"]
    assert len(masters) == 1
    assert masters[0].spec.replicas == 1

    assert len(fake.statefulsets) == 1
    assert len(fake.services) == 1
    # The metadata server is the master's built-in HTTP server, so no separate
    # metadata Deployment is ever materialized.
    assert fake.deployments == {}


def test_master_runs_as_root_with_writable_root_filesystem() -> None:
    """The master runs as root with a writable root filesystem, caps dropped.

    The upstream ``mooncake_master`` launcher chmods its bundled, root-owned
    binary on startup. That needs a writable root filesystem (a read-only root
    raised ``OSError: Read-only file system``) and root privileges (a non-root
    uid raised ``PermissionError: Operation not permitted`` chmod-ing a
    root-owned file). The pod therefore runs as uid/gid 0 with
    ``readOnlyRootFilesystem=False``, while the container still disallows
    privilege escalation and drops all Linux capabilities — constrained root.
    """
    monitor = _make_monitor()
    fake = _CreateIfAbsentK8s()
    monitor.apps_v1 = fake
    monitor.core_v1 = fake

    spec = {
        "mooncake": {
            "mode": "store",
            "store": {"enabled": True, "master_image": "example/mooncake-master:pinned"},
        }
    }
    monitor._ensure_mooncake_store("gco-inference", spec)

    master = next(
        body for (_ns, name), body in fake.statefulsets.items() if name == "mooncake-master"
    )
    pod = master.spec.template.spec
    assert pod.service_account_name == "gco-service-account"
    assert pod.automount_service_account_token is False
    # Runs as root so the startup chmod of the root-owned binary is permitted.
    assert pod.security_context.run_as_user == 0
    assert pod.security_context.run_as_group == 0
    # run_as_non_root must not be True (it would contradict uid 0 and be rejected).
    assert not getattr(pod.security_context, "run_as_non_root", None)

    sc = pod.containers[0].security_context
    assert sc.read_only_root_filesystem is False
    assert sc.allow_privilege_escalation is False
    assert "ALL" in (sc.capabilities.drop or [])
