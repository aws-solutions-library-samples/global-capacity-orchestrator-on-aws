"""In-region service resolution for mooncake endpoints.

The monitor wires each endpoint to the shared key-value store and the optional
cold tier using values it resolves for its own region — never values typed into
the endpoint spec. :meth:`InferenceMonitor._resolve_region_services` is the
single place that resolution happens:

- The shared master's RPC address comes from the monitor's environment
  (``MOONCAKE_MASTER_ADDRESS``); it is required whenever the store is enabled.
- The cold-tier object-store URI is resolved to the own-region general-purpose
  regional bucket from that region's discovery value, ignoring any bucket URI a
  caller put in the spec.

These examples pin own-region resolution, the spec URI being ignored, the
deferral when no master address is configured, and the cold tier dropping out
while the hot-path store keeps operating when the regional bucket is not
resolvable yet.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


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


@pytest.fixture
def monitor():
    return _make_monitor()


def test_resolves_own_region_master_and_cold_tier_bucket(monitor, monkeypatch):
    """A store-with-cold-tier endpoint resolves the own-region master and bucket.

    With the master address present in the environment and the region's bucket
    discoverable, the resolution carries the master RPC address, the derived
    metadata server, and a cold-tier URI pointing at the own-region bucket.
    """
    monkeypatch.setenv("MOONCAKE_MASTER_ADDRESS", "mooncake-master:50051")
    monkeypatch.delenv("MOONCAKE_METADATA_SERVER", raising=False)

    mooncake = {
        "mode": "store",
        "store": {"enabled": True, "cold_tier_enabled": True},
    }

    with patch(
        "gco.services.aws_ssm.get_ssm_parameter_optional",
        return_value="gco-regional-shared-acct-us-east-1",
    ):
        result = monitor._resolve_region_services("my-endpoint", mooncake)

    assert result.render_skipped is False
    assert result.store_master_unresolved is False
    assert result.cold_tier_unresolved is False
    assert result.error is None

    services = result.region_services
    assert services["master_server_address"] == "mooncake-master:50051"
    assert services["metadata_server"] == "http://mooncake-master:8080/metadata"
    assert (
        services["cold_tier_s3_uri"]
        == "s3://gco-regional-shared-acct-us-east-1/mooncake-kv/my-endpoint/"
    )


def test_spec_supplied_cold_tier_uri_is_ignored(monitor, monkeypatch):
    """A bucket URI typed into the spec never reaches the resolved config.

    The monitor resolves its own region's bucket regardless of any
    ``cold_tier_s3_uri`` a caller placed in the store block.
    """
    monkeypatch.setenv("MOONCAKE_MASTER_ADDRESS", "mooncake-master:50051")

    mooncake = {
        "mode": "store",
        "store": {
            "enabled": True,
            "cold_tier_enabled": True,
            # A caller-supplied URI pointing somewhere else entirely.
            "cold_tier_s3_uri": "s3://attacker-bucket/elsewhere/",
        },
    }

    with patch(
        "gco.services.aws_ssm.get_ssm_parameter_optional",
        return_value="gco-regional-shared-acct-us-east-1",
    ):
        result = monitor._resolve_region_services("my-endpoint", mooncake)

    resolved_uri = result.region_services["cold_tier_s3_uri"]
    assert resolved_uri == (
        "s3://gco-regional-shared-acct-us-east-1/mooncake-kv/my-endpoint/"
    )
    assert "attacker-bucket" not in resolved_uri


def test_store_without_master_address_defers_and_leaves_config_untouched(
    monitor, monkeypatch
):
    """An enabled store with no own-region master address defers rendering.

    Rendering is skipped, the unresolved-master condition is reported, and no
    ``region_services`` dict is produced so the existing endpoint configuration
    is left unchanged.
    """
    monkeypatch.delenv("MOONCAKE_MASTER_ADDRESS", raising=False)

    mooncake = {"mode": "store", "store": {"enabled": True}}

    # No bucket lookup should be needed; resolution short-circuits.
    with patch(
        "gco.services.aws_ssm.get_ssm_parameter_optional"
    ) as mock_ssm:
        result = monitor._resolve_region_services("my-endpoint", mooncake)

    assert result.render_skipped is True
    assert result.store_master_unresolved is True
    assert result.region_services is None
    assert result.error is not None
    assert "us-east-1" in result.error
    mock_ssm.assert_not_called()


def test_blank_master_address_is_treated_as_unresolved(monitor, monkeypatch):
    """A whitespace-only master address counts as no master address."""
    monkeypatch.setenv("MOONCAKE_MASTER_ADDRESS", "   ")

    result = monitor._resolve_region_services(
        "my-endpoint", {"mode": "store", "store": {"enabled": True}}
    )

    assert result.render_skipped is True
    assert result.store_master_unresolved is True
    assert result.region_services is None


def test_unresolved_bucket_drops_cold_tier_but_keeps_hot_store(monitor, monkeypatch):
    """An unresolvable regional bucket disables the cold tier only.

    The hot-path store stays configured (the master address is present and no
    cold-tier URI is emitted), and the unresolved-bucket condition is reported.
    """
    monkeypatch.setenv("MOONCAKE_MASTER_ADDRESS", "mooncake-master:50051")

    mooncake = {
        "mode": "store",
        "store": {"enabled": True, "cold_tier_enabled": True},
    }

    # The region's stack is not yet deployed, so the discovery value is absent.
    with patch(
        "gco.services.aws_ssm.get_ssm_parameter_optional",
        return_value=None,
    ):
        result = monitor._resolve_region_services("my-endpoint", mooncake)

    assert result.render_skipped is False
    assert result.cold_tier_unresolved is True
    assert result.error is not None
    assert "us-east-1" in result.error

    services = result.region_services
    # Hot path is intact ...
    assert services["master_server_address"] == "mooncake-master:50051"
    # ... but no cold-tier object store was wired in.
    assert "cold_tier_s3_uri" not in services


def test_bucket_lookup_failure_is_treated_as_unresolved(monitor, monkeypatch):
    """A discovery-read error drops the cold tier without failing the store."""
    monkeypatch.setenv("MOONCAKE_MASTER_ADDRESS", "mooncake-master:50051")

    mooncake = {
        "mode": "store",
        "store": {"enabled": True, "cold_tier_enabled": True},
    }

    with patch(
        "gco.services.aws_ssm.get_ssm_parameter_optional",
        side_effect=RuntimeError("ssm unreachable"),
    ):
        result = monitor._resolve_region_services("my-endpoint", mooncake)

    assert result.cold_tier_unresolved is True
    assert "cold_tier_s3_uri" not in result.region_services
    assert result.region_services["master_server_address"] == "mooncake-master:50051"
