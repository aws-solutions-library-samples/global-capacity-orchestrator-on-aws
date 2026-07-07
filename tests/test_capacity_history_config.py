# Tests for gco/config/config_loader.py historical capacity surface config block.
# Covers defaults (feature off), partial override merging, and validation errors
# for the optional cdk.json "historical" block.

import re

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
        assert cfg["capacity_block_duration_hours"] == 24
        assert cfg["capacity_block_long_duration_hours"] == 1512
        assert len(cfg["watch_instance_types"]) == 59
        assert cfg["enabled_regions"] == []

    def test_default_watch_instance_types_are_well_formed(self, valid_cdk_context):
        # Guard the default watch list shape so an accidental edit (dupes,
        # typos, stray whitespace) is caught here rather than at deploy time.
        cfg = _loader(valid_cdk_context).get_capacity_history_config()
        types = cfg["watch_instance_types"]
        assert len(types) == len(set(types)), "duplicate instance types"
        pattern = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)?\.[a-z0-9]+$")
        malformed = [t for t in types if not pattern.match(t)]
        assert not malformed, malformed
        families = {t.split(".", 1)[0] for t in types}
        for fam in (
            "p4d",
            "p4de",
            "p5",
            "p5e",
            "p6-b200",
            "g5",
            "g5g",
            "g6",
            "g6e",
            "g7e",
            "trn1",
            "inf1",
            "inf2",
        ):
            assert fam in families, fam


class TestEnabledOverride:
    def test_partial_override_merges_defaults(self, valid_cdk_context):
        loader = _loader(valid_cdk_context, {"enabled": True, "poll_interval_minutes": 5})
        assert loader.get_capacity_history_enabled() is True
        cfg = loader.get_capacity_history_config()
        assert cfg["poll_interval_minutes"] == 5
        assert cfg["retention_days"] == 90
        assert len(cfg["watch_instance_types"]) == 59

    def test_block_duration_overrides_merge(self, valid_cdk_context):
        loader = _loader(
            valid_cdk_context,
            {"capacity_block_duration_hours": 48, "capacity_block_long_duration_hours": 0},
        )
        cfg = loader.get_capacity_history_config()
        assert cfg["capacity_block_duration_hours"] == 48
        # 0 is a valid value: it disables the long probe.
        assert cfg["capacity_block_long_duration_hours"] == 0


class TestValidation:
    @pytest.mark.parametrize(
        "historical",
        [
            {"enabled": "yes"},
            {"retention_days": 0},
            {"retention_days": -1},
            {"poll_interval_minutes": "x"},
            {"capacity_block_duration_hours": 0},
            {"capacity_block_duration_hours": -1},
            {"capacity_block_duration_hours": "x"},
            {"capacity_block_long_duration_hours": -1},
            {"capacity_block_long_duration_hours": "x"},
            {"watch_instance_types": "g5.xlarge"},
            {"watch_instance_types": [1, 2]},
            {"enabled_regions": ["not-a-region"]},
        ],
    )
    def test_invalid_historical_raises(self, valid_cdk_context, historical):
        with pytest.raises(ConfigValidationError):
            _loader(valid_cdk_context, historical)

    def test_long_duration_zero_is_valid(self, valid_cdk_context):
        # 0 disables the long probe and must not raise.
        loader = _loader(valid_cdk_context, {"capacity_block_long_duration_hours": 0})
        assert loader.get_capacity_history_config()["capacity_block_long_duration_hours"] == 0
