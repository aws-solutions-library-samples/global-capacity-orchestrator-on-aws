"""Connector chaining and runtime config rendering for mooncake endpoints.

Two pure builders in :mod:`gco.services.inference_monitor` shape how a
disaggregated endpoint talks to the KV fabric:

- :func:`build_kv_transfer_config` turns a ``mooncake`` spec block plus a worker
  role into vLLM's ``--kv-transfer-config`` JSON. For mode ``both`` it composes
  the transfer connector and the store connector into a single ``MultiConnector``
  chain, and it refuses any role a mode does not support.
- :func:`render_mooncake_config` renders the ``mooncake.json`` contents mounted
  into each pod, carrying the RDMA/TCP transport, the optional key-value store,
  and the optional asynchronous cold tier.

These examples pin the exact connector chain for mode ``both``, the rejection of
an unsupported ``(mode, role)`` pair, and the rendered config across the
rdma/tcp transports and the store-on/store-off and cold-tier-on/off choices.
"""

from __future__ import annotations

import json

import pytest

from gco.services.inference_monitor import (
    build_kv_transfer_config,
    render_mooncake_config,
)


def test_both_mode_chains_transfer_then_store_sharing_kv_role() -> None:
    """Mode ``both`` wraps the transfer and store connectors in order.

    The emitted ``MultiConnector`` carries exactly two ordered sub-connectors —
    ``MooncakeConnector`` first, ``MooncakeStoreConnector`` second — and both
    sub-connectors share the worker role's ``kv_role``.
    """
    rendered = build_kv_transfer_config({"mode": "both"}, "prefill")

    parsed = json.loads(rendered)
    assert parsed["kv_connector"] == "MultiConnector"
    assert parsed["kv_role"] == "kv_producer"

    connectors = parsed["kv_connector_extra_config"]["connectors"]
    assert [c["kv_connector"] for c in connectors] == [
        "MooncakeConnector",
        "MooncakeStoreConnector",
    ]
    assert connectors[0]["kv_role"] == "kv_producer"
    assert connectors[1]["kv_role"] == "kv_producer"


def test_both_mode_decode_role_shares_consumer_role_across_chain() -> None:
    """The decode role propagates ``kv_consumer`` to both chained connectors."""
    parsed = json.loads(build_kv_transfer_config({"mode": "both"}, "decode"))

    connectors = parsed["kv_connector_extra_config"]["connectors"]
    assert parsed["kv_role"] == "kv_consumer"
    assert connectors[0]["kv_role"] == "kv_consumer"
    assert connectors[1]["kv_role"] == "kv_consumer"


@pytest.mark.parametrize(
    "mode, role",
    [
        ("disaggregated", "single"),  # store-only role on a disaggregated mode
        ("store", "prefill"),  # split role on a store-only mode
        ("store", "decode"),
        ("both", "single"),  # single role is not part of a chained mode
        ("unknown", "prefill"),  # unrecognized mode
        ("disaggregated", "worker"),  # unrecognized role
    ],
)
def test_unsupported_mode_role_pair_is_rejected_without_config(
    mode: str, role: str
) -> None:
    """An unsupported ``(mode, role)`` pair raises and emits no configuration."""
    with pytest.raises(ValueError):
        build_kv_transfer_config({"mode": mode}, role)


def _region_services() -> dict[str, str]:
    """Resolved in-region addresses the renderer consumes."""
    return {
        "metadata_server": "http://mooncake-master:8080/metadata",
        "master_server_address": "mooncake-master:50051",
        "cold_tier_s3_uri": "s3://gco-regional-shared-acct-region/kv",
    }


def test_rdma_transport_is_rendered_when_protocol_is_rdma() -> None:
    """The rendered config echoes the rdma protocol and its device name."""
    mooncake = {"transfer": {"protocol": "rdma", "device_name": "eth0"}}

    cfg = render_mooncake_config(mooncake, _region_services())

    assert cfg["protocol"] == "rdma"
    assert cfg["device_name"] == "eth0"
    assert cfg["metadata_server"] == "http://mooncake-master:8080/metadata"


def test_tcp_transport_is_rendered_when_protocol_is_tcp() -> None:
    """A tcp transport is rendered verbatim in the runtime config."""
    mooncake = {"transfer": {"protocol": "tcp", "device_name": ""}}

    cfg = render_mooncake_config(mooncake, _region_services())

    assert cfg["protocol"] == "tcp"
    assert cfg["device_name"] == ""


def test_transport_defaults_to_rdma_when_unspecified() -> None:
    """Absent transport settings fall back to the rdma default."""
    cfg = render_mooncake_config({}, _region_services())

    assert cfg["protocol"] == "rdma"
    assert cfg["device_name"] == ""


def test_master_address_present_only_when_store_enabled() -> None:
    """The master address appears exactly when the store is enabled."""
    enabled = render_mooncake_config(
        {"store": {"enabled": True}}, _region_services()
    )
    assert enabled["master_server_address"] == "mooncake-master:50051"

    disabled = render_mooncake_config(
        {"store": {"enabled": False}}, _region_services()
    )
    assert "master_server_address" not in disabled

    absent = render_mooncake_config({}, _region_services())
    assert "master_server_address" not in absent


def test_cold_tier_uri_present_only_when_cold_tier_enabled_is_true() -> None:
    """The cold-tier URI appears only when ``cold_tier_enabled`` is boolean True."""
    on = render_mooncake_config(
        {"store": {"enabled": True, "cold_tier_enabled": True}},
        _region_services(),
    )
    assert on["cold_tier_s3_uri"] == "s3://gco-regional-shared-acct-region/kv"

    off = render_mooncake_config(
        {"store": {"enabled": True, "cold_tier_enabled": False}},
        _region_services(),
    )
    assert "cold_tier_s3_uri" not in off


@pytest.mark.parametrize("cold_value", [None, "true", 1, "yes"])
def test_non_boolean_true_leaves_cold_tier_off(cold_value: object) -> None:
    """Any non-``True`` ``cold_tier_enabled`` value keeps the cold tier off."""
    cfg = render_mooncake_config(
        {"store": {"enabled": True, "cold_tier_enabled": cold_value}},
        _region_services(),
    )

    assert "cold_tier_s3_uri" not in cfg


def test_cold_tier_stays_off_the_transport_block() -> None:
    """The cold tier never reaches into the rdma transport settings."""
    cfg = render_mooncake_config(
        {
            "transfer": {"protocol": "rdma", "device_name": "eth0"},
            "store": {"enabled": True, "cold_tier_enabled": True},
        },
        _region_services(),
    )

    assert cfg["protocol"] == "rdma"
    assert cfg["device_name"] == "eth0"
    assert "cold_tier_s3_uri" in cfg
    # The cold-tier URI is a separate key, not folded into transport.
    assert cfg["device_name"] != cfg["cold_tier_s3_uri"]
