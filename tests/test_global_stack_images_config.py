"""
Tests for the ``_parse_images_config`` helper in
``gco/stacks/global_stack.py``.

Covers the defaults, explicit pass-through, and validation rules for
the ``images`` cdk.json block. Synthesis-level tests for the global
stack's replication rule and lookup-or-create Lambda live alongside
the existing global-stack synthesis tests in ``test_cdk_stacks.py``.
"""

from __future__ import annotations

import re

import pytest

from gco.stacks.global_stack import _parse_images_config


class TestImagesConfigDefaults:
    """The defaults applied when no ``images`` block is present."""

    def test_none_returns_full_defaults(self):
        result = _parse_images_config(None)
        assert result == {
            "removal_policy": "retain",
            "empty_on_delete": False,
            "lifecycle": {
                "keep_tagged": 20,
                "expire_untagged_days": 7,
            },
            "replication": {
                "enabled": True,
                "destinations": "all_deployed_regions",
            },
        }

    def test_empty_dict_returns_full_defaults(self):
        result = _parse_images_config({})
        assert result["removal_policy"] == "retain"
        assert result["empty_on_delete"] is False
        assert result["lifecycle"]["keep_tagged"] == 20
        assert result["lifecycle"]["expire_untagged_days"] == 7
        assert result["replication"]["enabled"] is True
        assert result["replication"]["destinations"] == "all_deployed_regions"

    def test_partial_lifecycle_block_fills_missing_fields(self):
        result = _parse_images_config({"lifecycle": {"keep_tagged": 5}})
        assert result["lifecycle"]["keep_tagged"] == 5
        assert result["lifecycle"]["expire_untagged_days"] == 7

    def test_partial_replication_block_fills_missing_fields(self):
        result = _parse_images_config({"replication": {"enabled": False}})
        assert result["replication"]["enabled"] is False
        # destinations default still applies even when replication is disabled.
        assert result["replication"]["destinations"] == "all_deployed_regions"


class TestImagesConfigExplicitValues:
    """Explicit values pass through unchanged."""

    def test_explicit_destroy_policy(self):
        result = _parse_images_config({"removal_policy": "destroy"})
        assert result["removal_policy"] == "destroy"

    def test_explicit_empty_on_delete_true(self):
        result = _parse_images_config({"empty_on_delete": True})
        assert result["empty_on_delete"] is True

    def test_explicit_lifecycle_values(self):
        result = _parse_images_config(
            {"lifecycle": {"keep_tagged": 100, "expire_untagged_days": 30}}
        )
        assert result["lifecycle"]["keep_tagged"] == 100
        assert result["lifecycle"]["expire_untagged_days"] == 30

    def test_explicit_replication_destinations_list(self):
        result = _parse_images_config({"replication": {"destinations": ["us-east-1", "eu-west-1"]}})
        assert result["replication"]["destinations"] == ["us-east-1", "eu-west-1"]

    def test_full_block_round_trips(self):
        cfg = {
            "removal_policy": "destroy",
            "empty_on_delete": True,
            "lifecycle": {"keep_tagged": 50, "expire_untagged_days": 14},
            "replication": {
                "enabled": False,
                "destinations": ["ap-northeast-1"],
            },
        }
        result = _parse_images_config(cfg)
        assert result["removal_policy"] == "destroy"
        assert result["empty_on_delete"] is True
        assert result["lifecycle"]["keep_tagged"] == 50
        assert result["lifecycle"]["expire_untagged_days"] == 14
        assert result["replication"]["enabled"] is False
        assert result["replication"]["destinations"] == ["ap-northeast-1"]


class TestImagesConfigValidation:
    """Invalid inputs raise ``ValueError`` rather than being silently coerced."""

    @pytest.mark.parametrize("bad_value", ["RETAIN", "DESTROY", "keep", "delete", "", "true", None])
    def test_invalid_removal_policy_raises(self, bad_value):
        with pytest.raises(ValueError, match=re.escape("images.removal_policy")):
            _parse_images_config({"removal_policy": bad_value})

    def test_invalid_replication_destinations_string_raises(self):
        with pytest.raises(ValueError, match=re.escape("images.replication.destinations")):
            _parse_images_config({"replication": {"destinations": "every-region"}})

    def test_invalid_replication_destinations_dict_raises(self):
        with pytest.raises(ValueError, match=re.escape("images.replication.destinations")):
            _parse_images_config({"replication": {"destinations": {"us-east-1": True}}})

    def test_invalid_replication_destinations_int_raises(self):
        with pytest.raises(ValueError, match=re.escape("images.replication.destinations")):
            _parse_images_config({"replication": {"destinations": 1}})

    def test_replication_destinations_list_with_non_string_raises(self):
        with pytest.raises(ValueError, match="region name strings"):
            _parse_images_config({"replication": {"destinations": ["us-east-1", 42, "eu-west-1"]}})

    def test_lifecycle_block_must_be_mapping(self):
        with pytest.raises(ValueError, match=re.escape("images.lifecycle")):
            _parse_images_config({"lifecycle": ["keep_tagged", 20]})

    def test_replication_block_must_be_mapping(self):
        with pytest.raises(ValueError, match=re.escape("images.replication")):
            _parse_images_config({"replication": "all"})
