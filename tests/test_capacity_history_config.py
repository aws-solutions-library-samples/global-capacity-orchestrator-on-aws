# Tests for gco/config/config_loader.py historical capacity surface config block.
# Covers defaults (feature off), partial override merging, and validation errors
# for the optional cdk.json "historical" block.

import aws_cdk as cdk
import pytest

from gco.config.config_loader import ConfigLoader, ConfigValidationError


def _loader(valid_cdk_context, historical=None):
    context = dict(valid_cdk_context)
    if historical is not None:
        context["historical"] = historical
    return ConfigLoader(cdk.App(context=context))


class TestDefaults:
    def test_disabled_by_default(self, valid_cdk_context):
        loader = _loader(valid_cdk_context)
        assert loader.get_capacity_history_enabled() is False
        cfg = loader.get_capacity_history_config()
        assert cfg["enabled"] is False
        assert cfg["retention_days"] == 90
        assert cfg["poll_interval_minutes"] == 15
        assert len(cfg["watch_instance_types"]) == 9
        assert cfg["enabled_regions"] == []


class TestEnabledOverride:
    def test_partial_override_merges_defaults(self, valid_cdk_context):
        loader = _loader(valid_cdk_context, {"enabled": True, "poll_interval_minutes": 5})
        assert loader.get_capacity_history_enabled() is True
        cfg = loader.get_capacity_history_config()
        assert cfg["poll_interval_minutes"] == 5
        assert cfg["retention_days"] == 90
        assert len(cfg["watch_instance_types"]) == 9


class TestValidation:
    @pytest.mark.parametrize(
        "historical",
        [
            {"enabled": "yes"},
            {"retention_days": 0},
            {"retention_days": -1},
            {"poll_interval_minutes": "x"},
            {"watch_instance_types": "g5.xlarge"},
            {"watch_instance_types": [1, 2]},
            {"enabled_regions": ["not-a-region"]},
        ],
    )
    def test_invalid_historical_raises(self, valid_cdk_context, historical):
        with pytest.raises(ConfigValidationError):
            _loader(valid_cdk_context, historical)
