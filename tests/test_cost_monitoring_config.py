"""
Tests for the cost_monitoring block in gco/config/config_loader.py.

Covers the merged defaults (on by default), nested deep-merge of the
reports/athena sub-blocks, type/range validation failing at synth, the
transition-before-expiration lifecycle invariant, and the effective-enable
conjunction with cluster_observability (disabling observability switches the
cost pipeline off without a synthesis error).
"""

from __future__ import annotations

from typing import Any

import pytest

from gco.config.config_loader import ConfigLoader, ConfigValidationError


class _MockNode:
    def __init__(self, context: dict[str, Any]):
        self._context = context

    def try_get_context(self, key: str) -> Any:
        return self._context.get(key)


class _MockApp:
    def __init__(self, context: dict[str, Any]):
        self.node = _MockNode(context)


def _loader(context: dict[str, Any] | None = None) -> ConfigLoader:
    """Minimal fully-valid context so every unrelated validator passes."""
    base: dict[str, Any] = {
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
    base.update(context or {})
    return ConfigLoader(_MockApp(base))


class TestCostMonitoringDefaults:
    def test_defaults_are_enabled_with_documented_values(self):
        config = _loader().get_cost_monitoring_config()
        assert config["enabled"] is True
        assert config["reports"] == {
            "interval_minutes": 60,
            "retention_days": 365,
            "transition_to_infrequent_access_days": 90,
        }
        assert config["athena"] == {"query_results_retention_days": 30}

    def test_enabled_by_default(self):
        assert _loader().get_cost_monitoring_enabled() is True

    def test_partial_sub_block_override_keeps_sibling_defaults(self):
        config = _loader(
            {"cost_monitoring": {"reports": {"retention_days": 120}}}
        ).get_cost_monitoring_config()
        assert config["reports"]["retention_days"] == 120
        assert config["reports"]["interval_minutes"] == 60
        assert config["reports"]["transition_to_infrequent_access_days"] == 90

    def test_malformed_block_falls_back_to_defaults(self):
        config = _loader({"cost_monitoring": "yes-please"}).get_cost_monitoring_config()
        assert config["enabled"] is True


class TestCostMonitoringValidation:
    def test_non_bool_enabled_fails_synth(self):
        with pytest.raises(ConfigValidationError, match="cost_monitoring.enabled"):
            _loader({"cost_monitoring": {"enabled": "true"}})

    @pytest.mark.parametrize(
        ("sub_block", "field", "value"),
        [
            ("reports", "interval_minutes", 0),
            ("reports", "interval_minutes", 10_000),
            ("reports", "interval_minutes", True),
            ("reports", "interval_minutes", "60"),
            ("reports", "retention_days", 0),
            ("reports", "transition_to_infrequent_access_days", 5),
            ("athena", "query_results_retention_days", -1),
        ],
    )
    def test_out_of_range_ints_fail_synth(self, sub_block, field, value):
        with pytest.raises(ConfigValidationError, match=f"cost_monitoring.{sub_block}.{field}"):
            _loader({"cost_monitoring": {sub_block: {field: value}}})

    def test_transition_on_or_after_expiration_fails_synth(self):
        with pytest.raises(ConfigValidationError, match="must be smaller"):
            _loader(
                {
                    "cost_monitoring": {
                        "reports": {
                            "retention_days": 90,
                            "transition_to_infrequent_access_days": 90,
                        }
                    }
                }
            )

    def test_transition_before_expiration_passes(self):
        loader = _loader(
            {
                "cost_monitoring": {
                    "reports": {
                        "retention_days": 91,
                        "transition_to_infrequent_access_days": 90,
                    }
                }
            }
        )
        assert loader.get_cost_monitoring_config()["reports"]["retention_days"] == 91


class TestObservabilityConjunction:
    def test_disabling_observability_disables_cost_monitoring_without_error(self):
        loader = _loader({"cluster_observability": {"enabled": False}})
        assert loader.get_cost_monitoring_enabled() is False
        # The raw block still reports its own toggle as on.
        assert loader.get_cost_monitoring_config()["enabled"] is True

    def test_explicit_cost_toggle_off_disables_regardless_of_observability(self):
        loader = _loader({"cost_monitoring": {"enabled": False}})
        assert loader.get_cost_monitoring_enabled() is False

    def test_both_enabled_yields_enabled(self):
        loader = _loader(
            {
                "cost_monitoring": {"enabled": True},
                "cluster_observability": {"enabled": True},
            }
        )
        assert loader.get_cost_monitoring_enabled() is True
