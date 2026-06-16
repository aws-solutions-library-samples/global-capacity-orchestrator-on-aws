"""Regional confinement of disaggregated KV-transfer wiring.

KV cache transfer over RoCE is an intra-region transport: every connector peer
and the shared master a topology wires to must live in the same region as the
monitor reconciling it. :meth:`InferenceMonitor._resolve_regional_scope` is the
boundary check that enforces this before any role pod is materialized, and
:meth:`InferenceMonitor._region_of_address` is the per-address classifier it
relies on.

These checks confirm the invariant that matters: whenever a topology is allowed
through, every address it resolved belongs to the monitor's own region, and the
moment any peer or master resolves to a different region the topology is
refused with a failure state. Addresses are generated across many regions so
the boundary holds for arbitrary wiring, not just hand-picked examples.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st

OWN_REGION = "us-east-1"

# Regions other than the monitor's own; any address resolving to one of these
# is, by definition, a cross-region peer the boundary must reject.
FOREIGN_REGIONS = [
    "eu-west-1",
    "eu-central-1",
    "ap-southeast-1",
    "ap-northeast-1",
    "us-west-2",
    "sa-east-1",
]

# Modes that wire a topology together. "disaggregated" and "both" add the
# prefill/decode peer Services; "store" runs a single instance with no peers but
# still reaches the shared master.
TOPOLOGY_MODES = ["disaggregated", "both", "store"]


def _make_monitor(region: str = OWN_REGION):
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


def _address_in(region: str) -> str:
    """Return a host:port address that embeds ``region`` as its region token."""
    return f"mooncake-peer.{region}.internal:50051"


def _metadata_in(region: str) -> str:
    """Return a metadata-server URI that embeds ``region`` as its region token."""
    return f"http://mooncake-meta.{region}.internal:8080/metadata"


@st.composite
def _topologies(draw):
    """Generate a topology spec plus resolved services with tagged regions.

    Each address (the shared master, the metadata server, and any explicit
    connector peers) is independently placed in either the monitor's own region
    or a foreign region. The generated bundle records, alongside the spec, the
    set of regions every address resolves to so the test can predict whether the
    boundary should admit or refuse the topology.
    """
    mode = draw(st.sampled_from(TOPOLOGY_MODES))
    name = draw(st.sampled_from(["ep", "endpoint", "model", "svc", "demo"]))
    ns = "gco-inference"

    def _region_choice():
        # Bias toward own-region so the all-in-region case is well represented,
        # while still exercising every foreign region.
        return draw(
            st.one_of(
                st.just(OWN_REGION),
                st.sampled_from(FOREIGN_REGIONS),
            )
        )

    master_region = _region_choice()
    metadata_region = _region_choice()
    explicit_peer_regions = draw(
        st.lists(st.sampled_from([OWN_REGION, *FOREIGN_REGIONS]), max_size=3)
    )

    spec = {
        "mooncake": {
            "mode": mode,
            "store": {"enabled": True},
            "transfer": {
                "protocol": "rdma",
                "peer_addresses": [_address_in(r) for r in explicit_peer_regions],
            },
        }
    }
    region_services = {
        "master_server_address": _address_in(master_region),
        "metadata_server": _metadata_in(metadata_region),
    }

    resolved_regions = {master_region, metadata_region, *explicit_peer_regions}

    return {
        "name": name,
        "ns": ns,
        "spec": spec,
        "region_services": region_services,
        "resolved_regions": resolved_regions,
    }


@settings(max_examples=200, deadline=None)
@given(bundle=_topologies())
def test_admitted_topology_wires_only_to_its_own_region(bundle: dict) -> None:
    """A topology is admitted only when every wired address is region-local.

    This pins the core guarantee: whenever resolution reports the topology is
    in-region, each address it actually resolved — prefill/decode peers, the
    shared master, and the metadata server — belongs to the monitor's own
    region, so no admitted topology can transfer KV cache across a region edge.
    Conversely, any address resolving to a different region forces a refusal in
    the failed state.
    """
    monitor = _make_monitor(OWN_REGION)

    result = monitor._resolve_regional_scope(
        bundle["name"],
        bundle["ns"],
        bundle["spec"],
        bundle["region_services"],
    )

    everything_local = bundle["resolved_regions"] <= {OWN_REGION}

    if everything_local:
        # Admitted: and every single resolved address stays inside the region.
        assert result.in_region is True
        assert result.state is None
        assert result.error is None
        for address in result.peer_addresses:
            assert monitor._region_of_address(address) == OWN_REGION
    else:
        # Refused: a cross-region peer or master was detected.
        assert result.in_region is False
        assert result.state == "failed"
        assert result.error is not None
        offending = bundle["resolved_regions"] - {OWN_REGION}
        # The failure names the region(s) the topology tried to escape to.
        assert any(region in result.error for region in offending)


@settings(max_examples=200, deadline=None)
@given(bundle=_topologies())
def test_no_admitted_address_escapes_the_region(bundle: dict) -> None:
    """When admitted, not one resolved address resolves outside the region.

    Stated as an invariant independent of how the topology was generated: an
    in-region result never carries an address that the classifier would place
    in another region.
    """
    monitor = _make_monitor(OWN_REGION)

    result = monitor._resolve_regional_scope(
        bundle["name"],
        bundle["ns"],
        bundle["spec"],
        bundle["region_services"],
    )

    if result.in_region:
        escaping = [
            address
            for address in result.peer_addresses
            if monitor._region_of_address(address) != OWN_REGION
        ]
        assert escaping == []


@given(foreign=st.sampled_from(FOREIGN_REGIONS))
def test_a_single_foreign_master_is_enough_to_refuse(foreign: str) -> None:
    """One out-of-region master address fails the whole topology.

    Even with every peer local, a master that resolves elsewhere is refused,
    materializes nothing, and reports the failed state — the prior endpoint
    resources are meant to be left untouched on this path.
    """
    monitor = _make_monitor(OWN_REGION)

    spec = {
        "mooncake": {
            "mode": "disaggregated",
            "store": {"enabled": True},
            "transfer": {"protocol": "rdma"},
        }
    }
    region_services = {
        "master_server_address": _address_in(foreign),
        "metadata_server": _metadata_in(OWN_REGION),
    }

    result = monitor._resolve_regional_scope("endpoint", "gco-inference", spec, region_services)

    assert result.in_region is False
    assert result.state == "failed"
    assert foreign in result.error
