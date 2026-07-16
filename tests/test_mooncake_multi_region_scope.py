"""Region-boundary checks for disaggregated mooncake topologies.

KV cache transfer over RoCE cannot cross a region boundary, so every peer
address and the shared master a disaggregated topology wires to must resolve to
the monitor's own region. :meth:`InferenceMonitor._resolve_regional_scope` is
the single gate that confirms this before any role pod is materialized.

An endpoint that targets several regions runs one independent topology per
region: each region's monitor reconciles only its own topology, and the gate
confirms that topology's master, metadata server, and peer addresses never
escape the region. When a peer or master resolves elsewhere the gate reports a
failure and the caller materializes nothing, leaving prior resources untouched.

These examples pin both halves of that contract: one in-region topology per
target region, and the out-of-region rejection that materializes no role
Deployments.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

NAMESPACE = "gco-inference"


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


def _disaggregated_spec() -> dict:
    return {
        "mooncake": {
            "mode": "disaggregated",
            "topology": {"prefill": 2, "decode": 2},
        }
    }


def _assert_nothing_materialized(monitor) -> None:
    """No K8s objects were created, patched, or deleted during resolution."""
    for verb in ("create", "patch", "replace", "delete"):
        getattr(monitor.apps_v1, f"{verb}_namespaced_deployment").assert_not_called()
    monitor.core_v1.create_namespaced_service.assert_not_called()
    monitor.networking_v1.create_namespaced_ingress.assert_not_called()


# --------------------------------------------------------------------------
# One independent in-region topology per target region.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("region", ["us-east-1", "eu-west-1", "ap-southeast-2"])
def test_each_target_region_topology_confirms_only_its_own_region(region):
    """A multi-region endpoint resolves an independent in-region topology.

    The same endpoint spec is reconciled by the monitor in each target region.
    Each region's monitor resolves the master and metadata server for its own
    region, and the gate confirms every checked address belongs to that region
    alone — never to a sibling target region.
    """
    monitor = _make_monitor(region=region)
    region_services = {
        "master_server_address": f"mooncake-master.{region}.internal:50051",
        "metadata_server": f"http://mooncake-master.{region}.internal:8080/metadata",
    }

    result = monitor._resolve_regional_scope(
        "shared-endpoint", NAMESPACE, _disaggregated_spec(), region_services
    )

    assert result.in_region is True
    assert result.state is None
    assert result.error is None
    # Every address the topology wires to resolves to this region only.
    assert result.peer_addresses
    for address in result.peer_addresses:
        assert monitor._region_of_address(address) == region
    # Resolution is read-only; it materializes no role Deployments.
    _assert_nothing_materialized(monitor)


def test_two_regions_yield_two_independent_in_region_topologies():
    """Two monitors for one endpoint each confirm their own region in isolation.

    The us-east-1 monitor wires only us-east-1 addresses and the eu-west-1
    monitor wires only eu-west-1 addresses; neither topology's resolved
    addresses leak into the other's region.
    """
    spec = _disaggregated_spec()

    east = _make_monitor(region="us-east-1")
    east_result = east._resolve_regional_scope(
        "shared-endpoint",
        NAMESPACE,
        spec,
        {
            "master_server_address": "mooncake-master.us-east-1.internal:50051",
            "metadata_server": "http://mooncake-master.us-east-1.internal:8080/metadata",
        },
    )

    west = _make_monitor(region="eu-west-1")
    west_result = west._resolve_regional_scope(
        "shared-endpoint",
        NAMESPACE,
        spec,
        {
            "master_server_address": "mooncake-master.eu-west-1.internal:50051",
            "metadata_server": "http://mooncake-master.eu-west-1.internal:8080/metadata",
        },
    )

    assert east_result.in_region is True
    assert west_result.in_region is True
    # No us-east-1 monitor address carries the eu-west-1 token and vice versa.
    assert not any("eu-west-1" in addr for addr in east_result.peer_addresses)
    assert not any("us-east-1" in addr for addr in west_result.peer_addresses)


# --------------------------------------------------------------------------
# Out-of-region rejection leaves prior resources unchanged.
# --------------------------------------------------------------------------


def test_out_of_region_master_is_rejected_and_materializes_nothing():
    """A master resolving to another region fails the gate and builds nothing.

    The endpoint runs in us-east-1 but its resolved master points at eu-west-1.
    The gate reports a failure that names the offending master address, and no
    role Deployment, Service, or Ingress is created — any prior resources are
    left exactly as they were.
    """
    monitor = _make_monitor(region="us-east-1")
    foreign_master = "mooncake-master.eu-west-1.internal:50051"
    region_services = {
        "master_server_address": foreign_master,
        "metadata_server": "http://mooncake-master.eu-west-1.internal:8080/metadata",
    }

    result = monitor._resolve_regional_scope(
        "shared-endpoint", NAMESPACE, _disaggregated_spec(), region_services
    )

    assert result.in_region is False
    assert result.state == "failed"
    assert result.error is not None
    assert foreign_master in result.error
    assert "eu-west-1" in result.error
    _assert_nothing_materialized(monitor)


def test_out_of_region_explicit_peer_is_rejected():
    """A hand-edited peer pointing at another region is caught.

    Even with an own-region master, an explicit peer address authored on the
    spec that resolves to a foreign region trips the gate; the failure names
    the foreign peer and nothing is materialized.
    """
    monitor = _make_monitor(region="us-east-1")
    foreign_peer = "sibling-prefill.ap-southeast-2.internal:8000"
    spec = {
        "mooncake": {
            "mode": "disaggregated",
            "topology": {"prefill": 1, "decode": 1},
            "transfer": {"peer_addresses": [foreign_peer]},
        }
    }
    region_services = {"master_server_address": "mooncake-master:50051"}

    result = monitor._resolve_regional_scope("shared-endpoint", NAMESPACE, spec, region_services)

    assert result.in_region is False
    assert result.state == "failed"
    assert foreign_peer in result.error
    assert "ap-southeast-2" in result.error
    _assert_nothing_materialized(monitor)


def test_in_region_master_with_local_kubernetes_peers_passes():
    """An own-region master and namespace-qualified Service peers pass.

    Kubernetes ``.svc.cluster.local`` names are region-local by construction,
    so a topology whose only region-tokened address is the own-region master
    is wholly in-region.
    """
    monitor = _make_monitor(region="us-east-1")
    region_services = {"master_server_address": "mooncake-master.us-east-1.internal:50051"}

    result = monitor._resolve_regional_scope(
        "shared-endpoint", NAMESPACE, _disaggregated_spec(), region_services
    )

    assert result.in_region is True
    assert result.error is None
    assert f"shared-endpoint-prefill.{NAMESPACE}.svc.cluster.local" in result.peer_addresses
    assert f"shared-endpoint-decode.{NAMESPACE}.svc.cluster.local" in result.peer_addresses
    _assert_nothing_materialized(monitor)


def test_unknown_external_host_is_rejected_and_materializes_nothing():
    """An untagged external host is ambiguous and therefore fails closed."""
    monitor = _make_monitor(region="us-east-1")
    unknown_peer = "kv-peer.example.com:50051"
    spec = {
        "mooncake": {
            "mode": "disaggregated",
            "topology": {"prefill": 1, "decode": 1},
            "transfer": {"peer_addresses": [unknown_peer]},
        }
    }

    result = monitor._resolve_regional_scope(
        "shared-endpoint",
        NAMESPACE,
        spec,
        {"master_server_address": "mooncake-master:50051"},
    )

    assert monitor._region_of_address(unknown_peer) == "unknown"
    assert result.in_region is False
    assert result.state == "failed"
    assert result.error is not None
    assert unknown_peer in result.error
    assert "region unknown" in result.error
    _assert_nothing_materialized(monitor)
