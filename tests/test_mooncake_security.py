"""Access-control guarantees for disaggregated inference.

Splitting an endpoint into prefill, decode, proxy, and a shared store widens
the surface that has to stay locked down. The examples here pin the four
guarantees that keep that surface closed:

- The prefill-decode proxy fronts a privileged admin path, so it never starts
  without a usable admin key. A *named* admin Secret that is missing or carries
  an empty ``ADMIN_API_KEY`` rejects the proxy, and no proxy Deployment or
  Service is created. When the spec names no Secret, the monitor auto-provisions
  a ``{name}-admin`` Secret with a generated key instead.
- On the happy path the admin key reaches the container only through a Secret
  reference — never as an inline environment value or command argument. The
  proxy remains an internal ClusterIP Service; reconciliation creates no
  endpoint-specific Ingress and removes historical direct routes.
- The intra-namespace allow rules are widening rules layered on top of a
  default-deny posture. When an allow rule cannot be applied, the failure names
  the offending rule and the default-deny policy is never read, modified, or
  deleted, so the namespace fails closed.
- Deleting a split prefill/decode endpoint is held behind the
  ``GCO_ENABLE_DESTRUCTIVE_OPERATIONS`` switch: the deletion tool is registered
  only when the switch is on, and then deletes it like any other endpoint.

The monitor examples build an :class:`InferenceMonitor` with every Kubernetes
client mocked and drive the security entry points directly. The deletion
example exercises the audited tool surface registered with the switch on.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

# Mirror the other MCP test modules so the tool surface is importable.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))


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
        # An empty namespace: the monitor reads before it creates, so every
        # object these checks expect it to create first reads as absent.
        absent = ApiException(status=404, reason="Not Found")
        monitor.apps_v1.read_namespaced_deployment.side_effect = absent
        monitor.core_v1.read_namespaced_service.side_effect = absent
        monitor.core_v1.read_namespaced_config_map.side_effect = absent
        monitor.networking_v1.read_namespaced_network_policy.side_effect = absent
        return monitor


@pytest.fixture
def monitor():
    return _make_monitor()


LIFECYCLE_ID = "a" * 64
GENERATED_SECRET_LABELS = {
    "app": "endpoint-admin",
    "project": "gco",
    "gco.io/type": "inference",
}


def _secret_with_key(
    value_b64: str | None = None,
    plain: str | None = None,
    *,
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
    resource_version: str | None = None,
    uid: str | None = None,
):
    """Return a Secret view carrying key material and explicit metadata."""
    secret = MagicMock()
    secret.string_data = {"ADMIN_API_KEY": plain} if plain is not None else None
    secret.data = {"ADMIN_API_KEY": value_b64} if value_b64 is not None else None
    secret.type = "Opaque"
    secret.metadata = SimpleNamespace(
        labels=labels,
        annotations=annotations,
        resource_version=resource_version,
        uid=uid,
    )
    return secret


def _endpoint_record() -> dict[str, str]:
    return {"lifecycle_id": LIFECYCLE_ID}


def _proxy_spec(secret_name: str | None = "endpoint-admin") -> dict:
    """A disaggregated spec whose proxy points (or not) at an admin Secret."""
    proxy: dict = {"image": "gco/proxy:pinned", "replicas": 2}
    if secret_name is not None:
        proxy["admin_api_key_secret"] = secret_name
    return {"mooncake": {"mode": "disaggregated", "proxy": proxy}}


# =============================================================================
# Admin API key Secret: the proxy never starts without a usable key
# =============================================================================


def test_unnamed_admin_secret_auto_provisions_and_creates_proxy(monitor):
    """A proxy that names no admin Secret gets one auto-provisioned.

    With no ``admin_api_key_secret`` on the proxy block, the monitor creates a
    per-endpoint ``{name}-admin`` Secret (create-if-absent) carrying a generated
    ``ADMIN_API_KEY``, then materializes the proxy referencing it. The generated
    key only lives in the Secret — never on the spec or a command argument.
    """
    from gco.services.inference_monitor import (
        ADMIN_API_KEY_SECRET_DATA_KEY,
        PD_PROXY_ADMIN_API_KEY_ENV,
    )

    # The {name}-admin Secret does not exist yet, so provisioning creates it.
    monitor.core_v1.read_namespaced_secret.side_effect = ApiException(status=404)

    monitor._create_pd_proxy(
        "endpoint", "gco-inference", _proxy_spec(secret_name=None), _endpoint_record()
    )

    # A Secret named {name}-admin was created with a non-empty generated key.
    monitor.core_v1.create_namespaced_secret.assert_called_once()
    create_args, _ = monitor.core_v1.create_namespaced_secret.call_args
    created = create_args[1]
    assert created.metadata.name == "endpoint-admin"
    generated = (created.string_data or {})[ADMIN_API_KEY_SECRET_DATA_KEY]
    assert generated  # non-empty generated key
    assert len(generated) >= 32
    assert created.metadata.labels == GENERATED_SECRET_LABELS
    assert "gco.io/lifecycle-id" not in created.metadata.labels
    assert all(len(value) <= 63 for value in created.metadata.labels.values())
    assert created.metadata.annotations == {"gco.io/lifecycle-id": LIFECYCLE_ID}

    # The proxy is materialized and references the provisioned Secret by name.
    monitor.apps_v1.create_namespaced_deployment.assert_called_once()
    deploy_args, _ = monitor.apps_v1.create_namespaced_deployment.call_args
    pod = deploy_args[1].spec.template.spec
    assert pod.service_account_name == "gco-service-account"
    assert pod.automount_service_account_token is False
    container = pod.containers[0]
    admin_entries = [e for e in (container.env or []) if e.name == PD_PROXY_ADMIN_API_KEY_ENV]
    assert len(admin_entries) == 1
    assert admin_entries[0].value is None
    assert admin_entries[0].value_from.secret_key_ref.name == "endpoint-admin"

    # The generated key is never inlined into any other environment value.
    for entry in container.env or []:
        if entry.value is not None:
            assert generated not in entry.value


def test_existing_proxy_deployment_is_reconciled_after_stop(monitor):
    """A stopped proxy Deployment is patched back to configured capacity."""
    import base64

    monitor.core_v1.read_namespaced_secret.return_value = _secret_with_key(
        value_b64=base64.b64encode(b"byo-key").decode()
    )
    # The stopped proxy Deployment exists, so it is read and patched, not created.
    monitor.apps_v1.read_namespaced_deployment.side_effect = None
    monitor.apps_v1.read_namespaced_deployment.return_value = MagicMock()

    monitor._create_pd_proxy(
        "endpoint", "gco-inference", _proxy_spec("endpoint-admin"), _endpoint_record()
    )

    monitor.apps_v1.create_namespaced_deployment.assert_not_called()
    monitor.apps_v1.patch_namespaced_deployment.assert_called_once()
    args = monitor.apps_v1.patch_namespaced_deployment.call_args.args
    assert args[:2] == ("endpoint-proxy", "gco-inference")
    desired = monitor.apps_v1.patch_namespaced_deployment.call_args.kwargs["body"]
    assert desired.spec.replicas == 2
    assert desired.spec.template.spec.containers[0].image == "gco/proxy:pinned"


def test_admin_secret_provision_is_idempotent_when_present(monitor):
    """An already-present {name}-admin Secret is left untouched (key stays stable).

    Create-if-absent means a reconcile pass that finds the Secret already there
    does not recreate it, so proxy pods never see the key churn underneath them.
    """
    current = _secret_with_key(
        plain="stable-key",
        labels=GENERATED_SECRET_LABELS,
        annotations={"gco.io/lifecycle-id": LIFECYCLE_ID},
    )
    monitor.core_v1.read_namespaced_secret.return_value = current
    name = monitor._provision_admin_api_key_secret("endpoint-admin", "gco-inference", LIFECYCLE_ID)
    assert name == "endpoint-admin"
    monitor.core_v1.create_namespaced_secret.assert_not_called()


def test_admin_secret_provision_treats_conflict_as_success(monitor):
    """A concurrent create is accepted only after exact ownership readback."""
    current = _secret_with_key(
        plain="winner-key",
        labels=GENERATED_SECRET_LABELS,
        annotations={"gco.io/lifecycle-id": LIFECYCLE_ID},
    )
    monitor.core_v1.read_namespaced_secret.side_effect = [
        ApiException(status=404),
        current,
    ]
    monitor.core_v1.create_namespaced_secret.side_effect = ApiException(status=409)
    name = monitor._provision_admin_api_key_secret("endpoint-admin", "gco-inference", LIFECYCLE_ID)
    assert name == "endpoint-admin"


def test_exact_legacy_generated_secret_is_annotated_then_verified(monitor):
    legacy = _secret_with_key(
        plain="legacy-key",
        labels=GENERATED_SECRET_LABELS,
        annotations={},
        resource_version="17",
        uid="legacy-uid",
    )
    adopted = _secret_with_key(
        plain="legacy-key",
        labels=GENERATED_SECRET_LABELS,
        annotations={"gco.io/lifecycle-id": LIFECYCLE_ID},
        resource_version="18",
        uid="legacy-uid",
    )
    monitor.core_v1.read_namespaced_secret.side_effect = [legacy, adopted]

    assert (
        monitor._provision_admin_api_key_secret("endpoint-admin", "gco-inference", LIFECYCLE_ID)
        == "endpoint-admin"
    )

    monitor.core_v1.patch_namespaced_secret.assert_called_once_with(
        "endpoint-admin",
        "gco-inference",
        body={
            "metadata": {
                "resourceVersion": "17",
                "annotations": {"gco.io/lifecycle-id": LIFECYCLE_ID},
            }
        },
        _request_timeout=30,
    )
    monitor.core_v1.create_namespaced_secret.assert_not_called()


def test_legacy_secret_replacement_race_blocks_without_adopting(monitor):
    legacy = _secret_with_key(
        plain="legacy-key",
        labels=GENERATED_SECRET_LABELS,
        annotations={},
        resource_version="17",
        uid="legacy-uid",
    )
    monitor.core_v1.read_namespaced_secret.return_value = legacy
    monitor.core_v1.patch_namespaced_secret.side_effect = ApiException(status=409)
    from gco.services.inference_monitor import AdminApiKeySecretError

    with pytest.raises(AdminApiKeySecretError, match="changed during lifecycle migration"):
        monitor._provision_admin_api_key_secret("endpoint-admin", "gco-inference", LIFECYCLE_ID)

    patch_body = monitor.core_v1.patch_namespaced_secret.call_args.kwargs["body"]
    assert patch_body["metadata"]["resourceVersion"] == "17"
    monitor.core_v1.create_namespaced_secret.assert_not_called()


@pytest.mark.parametrize(
    ("labels", "annotations"),
    [
        ({}, {}),
        ({**GENERATED_SECRET_LABELS, "collision": "foreign"}, {}),
        (GENERATED_SECRET_LABELS, {"gco.io/lifecycle-id": "other-lifecycle"}),
    ],
)
def test_ambiguous_generated_secret_is_never_adopted(monitor, labels, annotations):
    monitor.core_v1.read_namespaced_secret.return_value = _secret_with_key(
        plain="foreign-key",
        labels=labels,
        annotations=annotations,
    )
    from gco.services.inference_monitor import AdminApiKeySecretError

    with pytest.raises(AdminApiKeySecretError, match="lifecycle provenance"):
        monitor._provision_admin_api_key_secret("endpoint-admin", "gco-inference", LIFECYCLE_ID)

    monitor.core_v1.patch_namespaced_secret.assert_not_called()
    monitor.core_v1.create_namespaced_secret.assert_not_called()


def test_ensure_admin_secret_prefers_named_byo_secret(monitor):
    """When the proxy names a Secret, the named one is verified and used as-is.

    The bring-your-own path reads the named Secret and never auto-provisions a
    ``{name}-admin`` Secret.
    """
    import base64

    valid = _secret_with_key(value_b64=base64.b64encode(b"a-real-key").decode())
    monitor.core_v1.read_namespaced_secret.return_value = valid
    proxy = {"admin_api_key_secret": "byo-secret"}
    name = monitor._ensure_admin_api_key_secret("endpoint", proxy, "gco-inference", LIFECYCLE_ID)
    assert name == "byo-secret"
    monitor.core_v1.create_namespaced_secret.assert_not_called()


def test_absent_admin_secret_rejects_and_creates_no_proxy(monitor):
    """A named admin Secret that does not exist rejects the proxy.

    A lookup that comes back ``404`` fails the verification, names the missing
    Secret, and leaves the cluster untouched — no proxy objects are created.
    """
    from gco.services.inference_monitor import AdminApiKeySecretError

    monitor.core_v1.read_namespaced_secret.side_effect = ApiException(status=404)

    with pytest.raises(AdminApiKeySecretError) as excinfo:
        monitor._create_pd_proxy(
            "endpoint", "gco-inference", _proxy_spec("endpoint-admin"), _endpoint_record()
        )

    assert excinfo.value.secret == "endpoint-admin"
    monitor.apps_v1.create_namespaced_deployment.assert_not_called()
    monitor.core_v1.create_namespaced_service.assert_not_called()
    monitor.networking_v1.create_namespaced_ingress.assert_not_called()


def test_empty_admin_key_rejects_and_creates_no_proxy(monitor):
    """A present Secret with an empty ``ADMIN_API_KEY`` rejects the proxy.

    The Secret exists but carries no usable key value, so the proxy is rejected
    and no proxy Deployment, Service, or Ingress is created.
    """
    from gco.services.inference_monitor import AdminApiKeySecretError

    empty = MagicMock()
    empty.string_data = {"ADMIN_API_KEY": ""}
    empty.data = {"ADMIN_API_KEY": ""}
    monitor.core_v1.read_namespaced_secret.return_value = empty

    with pytest.raises(AdminApiKeySecretError) as excinfo:
        monitor._create_pd_proxy(
            "endpoint", "gco-inference", _proxy_spec("endpoint-admin"), _endpoint_record()
        )

    assert excinfo.value.secret == "endpoint-admin"
    monitor.apps_v1.create_namespaced_deployment.assert_not_called()
    monitor.core_v1.create_namespaced_service.assert_not_called()
    monitor.networking_v1.create_namespaced_ingress.assert_not_called()


def test_verify_admin_secret_reports_each_failure_mode(monitor):
    """The verification distinguishes unnamed, absent, and empty-key Secrets."""
    from gco.services.inference_monitor import AdminApiKeySecretError

    # Names no Secret at all.
    with pytest.raises(AdminApiKeySecretError) as unnamed:
        monitor._verify_admin_api_key_secret({}, "gco-inference")
    assert unnamed.value.secret is None

    # Names a Secret that the API cannot find.
    monitor.core_v1.read_namespaced_secret.side_effect = ApiException(status=404)
    with pytest.raises(AdminApiKeySecretError) as absent:
        monitor._verify_admin_api_key_secret({"admin_api_key_secret": "missing"}, "gco-inference")
    assert absent.value.secret == "missing"

    # Names a Secret whose key value is empty.
    monitor.core_v1.read_namespaced_secret.side_effect = None
    blank = MagicMock()
    blank.string_data = None
    blank.data = {"ADMIN_API_KEY": ""}
    monitor.core_v1.read_namespaced_secret.return_value = blank
    with pytest.raises(AdminApiKeySecretError) as empty:
        monitor._verify_admin_api_key_secret({"admin_api_key_secret": "blank"}, "gco-inference")
    assert empty.value.secret == "blank"


def test_admin_key_injected_only_by_secret_reference(monitor):
    """On success the admin key flows in by Secret reference, never inline.

    The proxy container gets an ``ADMIN_API_KEY`` environment entry sourced from
    the named Secret's key. The entry carries no inline value, no other
    environment value or command argument carries the key material, and the
    proxy remains reachable only through its internal ClusterIP Service behind
    the shared authenticated route.
    """
    import base64

    from gco.services.inference_monitor import (
        ADMIN_API_KEY_SECRET_DATA_KEY,
        PD_PROXY_ADMIN_API_KEY_ENV,
        PD_PROXY_SCRIPT_PATH,
    )

    # A real key value lives only in the Secret; it is never handed to the spec.
    key_material = "super-secret-admin-key-value"
    secret = _secret_with_key(value_b64=base64.b64encode(key_material.encode()).decode())
    monitor.core_v1.read_namespaced_secret.return_value = secret

    monitor._create_pd_proxy(
        "endpoint", "gco-inference", _proxy_spec("endpoint-admin"), _endpoint_record()
    )

    deploy_args, _ = monitor.apps_v1.create_namespaced_deployment.call_args
    deployment = deploy_args[1]
    container = deployment.spec.template.spec.containers[0]
    env = container.env or []

    admin_entries = [e for e in env if e.name == PD_PROXY_ADMIN_API_KEY_ENV]
    assert len(admin_entries) == 1
    admin = admin_entries[0]

    # The key arrives by Secret reference, with no inline value.
    assert admin.value is None
    assert admin.value_from is not None
    ref = admin.value_from.secret_key_ref
    assert ref.name == "endpoint-admin"
    assert ref.key == ADMIN_API_KEY_SECRET_DATA_KEY

    # The key material is nowhere in any inline environment value.
    for entry in env:
        if entry.value is not None:
            assert key_material not in entry.value

    # The proxy is launched by a fixed command (the bundled router script); the
    # admin key arrives only by Secret reference and is never smuggled through a
    # command-line argument.
    assert container.args is None
    assert container.command == ["python3", PD_PROXY_SCRIPT_PATH]
    for token in container.command:
        assert key_material not in token

    # The Service is ClusterIP-only, and reconciliation cannot create a direct
    # endpoint Ingress that would expose the proxy's admin API.
    svc_args, _ = monitor.core_v1.create_namespaced_service.call_args
    assert svc_args[1].spec.type == "ClusterIP"
    monitor.networking_v1.create_namespaced_ingress.assert_not_called()
    monitor.networking_v1.patch_namespaced_ingress.assert_not_called()


# =============================================================================
# Network policy: a failed allow rule fails closed and keeps default-deny
# =============================================================================


def _deny_policy_untouched(monitor) -> None:
    """Assert only the monitor's own allow rules were read, and no policy was changed."""
    from gco.services.inference_monitor import (
        NETWORK_POLICY_INFERENCE_INTERNAL,
        NETWORK_POLICY_POD_TO_MASTER,
        NETWORK_POLICY_POD_TO_METADATA,
        NETWORK_POLICY_RDMA_BOOTSTRAP,
    )

    read_names = {
        call.args[0] for call in monitor.networking_v1.read_namespaced_network_policy.call_args_list
    }
    assert read_names <= {
        NETWORK_POLICY_INFERENCE_INTERNAL,
        NETWORK_POLICY_POD_TO_MASTER,
        NETWORK_POLICY_POD_TO_METADATA,
        NETWORK_POLICY_RDMA_BOOTSTRAP,
    }
    monitor.networking_v1.patch_namespaced_network_policy.assert_not_called()
    monitor.networking_v1.replace_namespaced_network_policy.assert_not_called()
    monitor.networking_v1.delete_namespaced_network_policy.assert_not_called()


def test_first_allow_rule_failure_names_rule_and_keeps_default_deny(monitor):
    """A failure on the first allow rule names it and leaves default-deny intact.

    A non-conflict API error applying the first widening rule surfaces a failure
    naming that rule, and no deny policy is ever read, modified, or deleted.
    """
    from gco.services.inference_monitor import (
        NETWORK_POLICY_INFERENCE_INTERNAL,
        NetworkPolicyApplyError,
    )

    monitor.networking_v1.create_namespaced_network_policy.side_effect = ApiException(
        status=500, reason="Internal"
    )

    with pytest.raises(NetworkPolicyApplyError) as excinfo:
        monitor._ensure_intra_namespace_network_policies("gco-inference", {})

    assert excinfo.value.rule == NETWORK_POLICY_INFERENCE_INTERNAL
    _deny_policy_untouched(monitor)


def test_later_allow_rule_failure_names_that_rule(monitor):
    """A failure on a later allow rule names that specific rule.

    The first two rules apply cleanly; the third fails with a non-conflict
    error. The surfaced failure names the third rule, and default-deny is
    preserved.
    """
    from gco.services.inference_monitor import (
        NETWORK_POLICY_POD_TO_METADATA,
        NetworkPolicyApplyError,
    )

    monitor.networking_v1.create_namespaced_network_policy.side_effect = [
        None,
        None,
        ApiException(status=403, reason="Forbidden"),
    ]

    with pytest.raises(NetworkPolicyApplyError) as excinfo:
        monitor._ensure_intra_namespace_network_policies("gco-inference", {})

    assert excinfo.value.rule == NETWORK_POLICY_POD_TO_METADATA
    _deny_policy_untouched(monitor)


def test_existing_allow_rules_are_left_in_place(monitor):
    """Allow rules already present are the steady state and raise nothing.

    Each widening rule that reads back as present is left untouched without a
    create attempt, the call succeeds, and no deny policy is read or mutated.
    """
    monitor.networking_v1.read_namespaced_network_policy.side_effect = None
    monitor.networking_v1.read_namespaced_network_policy.return_value = MagicMock()

    # Already-present widening rules are accepted without error.
    monitor._ensure_intra_namespace_network_policies("gco-inference", {})

    monitor.networking_v1.create_namespaced_network_policy.assert_not_called()
    assert monitor.networking_v1.read_namespaced_network_policy.call_count == 4
    _deny_policy_untouched(monitor)


def test_an_allow_rule_created_concurrently_is_left_in_place(monitor):
    """A rule another writer creates between the read and the create (409) is kept."""
    existing = MagicMock()
    monitor.networking_v1.read_namespaced_network_policy.side_effect = [
        ApiException(status=404),
        existing,
        existing,
        existing,
        existing,
    ]
    monitor.networking_v1.create_namespaced_network_policy.side_effect = ApiException(status=409)

    monitor._ensure_intra_namespace_network_policies("gco-inference", {})

    monitor.networking_v1.create_namespaced_network_policy.assert_called_once()
    _deny_policy_untouched(monitor)


# =============================================================================
# Destructive deletion gating for split prefill/decode endpoints
# =============================================================================


def _registered_tool_names(run_mcp) -> set[str]:
    """Snapshot every tool name on the live mcp singleton."""
    tools = asyncio.run(run_mcp.mcp._list_tools())
    return {t.name for t in tools}


def test_split_endpoint_deletion_is_gated_only_by_registration():
    """Deleting a split prefill/decode endpoint is held behind the destructive switch.

    With ``GCO_ENABLE_DESTRUCTIVE_OPERATIONS`` off, ``delete_inference`` is not
    registered, so no deletion can be requested at all (pinned for every
    destructive tool in ``test_mcp_destructive_gating.py``). With it on, the
    tool deletes a split endpoint the way ``gco inference delete`` does: one
    delete command, with no status lookup and no second reading of the switch,
    so flipping the variable after startup changes nothing. Registration is
    the gate, as for every other destructive tool.
    """
    import os

    import run_mcp

    # Snapshot the registry so the gated tools this load adds can be removed
    # precisely, leaving other modules an unpolluted registry.
    before = _registered_tool_names(run_mcp)
    with patch.dict(os.environ, {"GCO_ENABLE_DESTRUCTIVE_OPERATIONS": "true"}):
        importlib.reload(run_mcp)
    try:
        with (
            patch.dict(
                os.environ,
                {
                    "GCO_ENABLE_DESTRUCTIVE_OPERATIONS": "false",
                    "GCO_ENABLE_ALL_TOOLS": "false",
                },
            ),
            patch("cli_runner._run_cli") as mock_run,
        ):
            mock_run.return_value = json.dumps({"deleted": "split-endpoint"})
            result = asyncio.run(run_mcp.delete_inference("split-endpoint"))
        assert json.loads(result) == {"deleted": "split-endpoint"}
        invoked = [call.args for call in mock_run.call_args_list]
        assert invoked == [("inference", "delete", "split-endpoint", "-y")]
    finally:
        # Remove exactly the tools this load added so the registry returns to
        # the state other test modules expect.
        for name in _registered_tool_names(run_mcp) - before:
            with contextlib.suppress(Exception):
                run_mcp.mcp.local_provider.remove_tool(name)
