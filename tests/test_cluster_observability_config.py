"""Tests for the in-cluster observability configuration in ConfigLoader.

Drives ``ConfigLoader.get_cluster_observability_config`` /
``get_cluster_observability_enabled`` and the synth-time validator against a
MockApp/MockNode pair surfacing a hand-crafted CDK context. Covers:

- the on-by-default posture (the block is absent -> observability is enabled),
- the full default shape,
- deep-merge of a partial override (a single nested key does not wipe the rest),
- an explicit ``enabled: false`` opt-out,
- validation errors for malformed values (non-bool enabled, non-string sizes,
  non-bool alertmanager.enabled),
- a round-trip: whatever boolean the context carries is exactly what
  ``get_cluster_observability_enabled`` returns.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from gco.config.config_loader import ConfigLoader, ConfigValidationError


class _MockNode:
    def __init__(self, context: dict[str, Any]):
        self._context = context

    def try_get_context(self, key: str) -> Any:
        return self._context.get(key)


class _MockApp:
    def __init__(self, context: dict[str, Any]):
        self.node = _MockNode(context)


def _base_context() -> dict[str, Any]:
    """A minimal, fully-valid context so ConfigLoader construction runs all
    validators (including the observability one) without tripping an
    unrelated required-field error."""
    return {
        "project_name": "gco",
        "deployment_regions": {
            "global": "us-east-2",
            "api_gateway": "us-east-2",
            "monitoring": "us-east-2",
            "regional": ["us-east-1"],
        },
        "kubernetes_version": "1.36",
        "resource_thresholds": {
            "cpu_threshold": 80,
            "memory_threshold": 85,
            "gpu_threshold": 90,
        },
        "global_accelerator": {
            "health_check_grace_period": 30,
            "health_check_interval": 30,
            "health_check_timeout": 5,
            "health_check_path": "/api/v1/health",
        },
        "alb_config": {
            "health_check_interval": 30,
            "health_check_timeout": 5,
            "healthy_threshold": 2,
            "unhealthy_threshold": 2,
        },
        "manifest_processor": {
            "image": "gco/manifest-processor:latest",
            "replicas": 3,
            "resource_limits": {"cpu": "1000m", "memory": "2Gi"},
        },
        "job_validation_policy": {
            "allowed_namespaces": ["default", "gco-jobs"],
            "resource_quotas": {
                "max_cpu_per_manifest": "10",
                "max_memory_per_manifest": "32Gi",
                "max_gpu_per_manifest": 4,
            },
        },
        "api_gateway": {
            "throttle_rate_limit": 1000,
            "throttle_burst_limit": 2000,
            "log_level": "INFO",
            "metrics_enabled": True,
            "tracing_enabled": True,
        },
        "tags": {"Environment": "test"},
    }


def _loader(observability: dict[str, Any] | None = None) -> ConfigLoader:
    ctx = _base_context()
    if observability is not None:
        ctx["cluster_observability"] = observability
    return ConfigLoader(_MockApp(ctx))


# --- defaults / on-by-default ------------------------------------------------


def test_absent_block_defaults_to_enabled() -> None:
    cfg = _loader().get_cluster_observability_config()
    assert cfg["enabled"] is True


def test_absent_block_returns_full_default_shape() -> None:
    cfg = _loader().get_cluster_observability_config()
    assert cfg["grafana"]["persistence_size"] == "10Gi"
    assert cfg["grafana"]["admin_user"] == "admin"
    assert cfg["prometheus"]["persistence_size"] == "50Gi"
    assert cfg["prometheus"]["retention"] == "15d"
    assert cfg["alertmanager"]["enabled"] is True
    assert cfg["alertmanager"]["persistence_size"] == "5Gi"


def test_enabled_wrapper_matches_config() -> None:
    loader = _loader()
    assert loader.get_cluster_observability_enabled() is True
    assert (
        loader.get_cluster_observability_enabled()
        == loader.get_cluster_observability_config()["enabled"]
    )


# --- deep-merge of partial overrides ----------------------------------------


def test_partial_override_preserves_other_subblock_defaults() -> None:
    """Overriding one nested key must not drop the sub-block's other defaults."""
    cfg = _loader({"prometheus": {"retention": "30d"}}).get_cluster_observability_config()
    assert cfg["prometheus"]["retention"] == "30d"
    # persistence_size default survives the partial override.
    assert cfg["prometheus"]["persistence_size"] == "50Gi"
    # untouched sub-blocks keep their defaults.
    assert cfg["grafana"]["persistence_size"] == "10Gi"
    assert cfg["enabled"] is True


def test_explicit_disable_opts_out() -> None:
    cfg = _loader({"enabled": False}).get_cluster_observability_config()
    assert cfg["enabled"] is False
    # sub-block defaults are still populated even when disabled.
    assert cfg["prometheus"]["retention"] == "15d"


# --- validation --------------------------------------------------------------


def test_non_bool_enabled_is_rejected() -> None:
    with pytest.raises(ConfigValidationError, match="cluster_observability.enabled must be a bool"):
        _loader({"enabled": "yes"})


def test_non_string_persistence_size_is_rejected() -> None:
    with pytest.raises(ConfigValidationError, match="persistence_size must be a non-empty string"):
        _loader({"prometheus": {"persistence_size": 50}})


def test_empty_retention_is_rejected() -> None:
    with pytest.raises(ConfigValidationError, match="retention must be a non-empty string"):
        _loader({"prometheus": {"retention": "   "}})


def test_non_bool_alertmanager_enabled_is_rejected() -> None:
    with pytest.raises(
        ConfigValidationError, match="cluster_observability.alertmanager.enabled must be a bool"
    ):
        _loader({"alertmanager": {"enabled": 1}})


def test_wellformed_block_passes_validation() -> None:
    # Construction (which runs the validator) must not raise.
    cfg = _loader(
        {
            "enabled": True,
            "grafana": {"persistence_size": "20Gi"},
            "prometheus": {"persistence_size": "100Gi", "retention": "30d"},
            "alertmanager": {"enabled": False, "persistence_size": "2Gi"},
        }
    ).get_cluster_observability_config()
    assert cfg["grafana"]["persistence_size"] == "20Gi"
    assert cfg["alertmanager"]["enabled"] is False


# --- round-trip property -----------------------------------------------------


@given(enabled=st.booleans())
def test_enabled_flag_round_trips(enabled: bool) -> None:
    """Whatever boolean the context carries is exactly what the getter returns."""
    loader = _loader({"enabled": enabled})
    assert loader.get_cluster_observability_enabled() is enabled
