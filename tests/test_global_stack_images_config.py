"""
Tests for the ``_parse_images_config`` helper in
``gco/stacks/global_stack.py``.

Covers the defaults, explicit pass-through, and validation rules for
the ``images`` cdk.json block, whose only live setting is
``replication``: ``removal_policy``, ``empty_on_delete`` and ``lifecycle``
are deprecated and ignored (no stack creates or deletes the image
repositories). Synthesis-level tests for the global stack's replication
rule live alongside the existing global-stack synthesis tests in
``test_cdk_stacks.py``.
"""

from __future__ import annotations

import re

import pytest

from gco.config.config_loader import ConfigValidationError
from gco.stacks.global_stack import _parse_images_config


class TestImagesConfigDefaults:
    """The defaults applied when no ``images`` block is present."""

    def test_none_returns_full_defaults(self):
        result = _parse_images_config(None)
        assert result == {
            "replication": {
                "enabled": True,
                "destinations": "all_deployed_regions",
            },
        }

    def test_empty_dict_returns_full_defaults(self):
        assert _parse_images_config({}) == _parse_images_config(None)

    def test_the_deprecated_keys_are_not_read(self):
        """Whatever they say, even values the parser used to reject, nothing reads them."""
        deprecated = {
            "removal_policy": "shred",
            "empty_on_delete": "false",
            "lifecycle": ["keep_tagged", 20],
        }
        assert _parse_images_config(deprecated) == _parse_images_config(None)

    def test_partial_replication_block_fills_missing_fields(self):
        result = _parse_images_config({"replication": {"enabled": False}})
        assert result["replication"]["enabled"] is False
        # destinations default still applies even when replication is disabled.
        assert result["replication"]["destinations"] == "all_deployed_regions"


class TestImagesConfigExplicitValues:
    """Explicit values pass through unchanged."""

    def test_explicit_replication_destinations_list(self):
        result = _parse_images_config({"replication": {"destinations": ["us-east-1", "eu-west-1"]}})
        assert result["replication"]["destinations"] == ["us-east-1", "eu-west-1"]

    def test_full_block_round_trips(self):
        cfg = {
            "replication": {
                "enabled": False,
                "destinations": ["ap-northeast-1"],
            },
        }
        result = _parse_images_config(cfg)
        assert result["replication"]["enabled"] is False
        assert result["replication"]["destinations"] == ["ap-northeast-1"]


class TestImagesConfigValidation:
    """Invalid inputs raise ``ValueError`` rather than being silently coerced."""

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

    def test_replication_block_must_be_mapping(self):
        with pytest.raises(ValueError, match=re.escape("images.replication")):
            _parse_images_config({"replication": "all"})


class TestImagesReplicationFlag:
    """``replication.enabled`` is read strictly: ``bool("false")`` is True.

    Earlier releases read every non-empty string as on, so a quoted
    ``"false"`` has been replicating; it now fails instead of switching the
    replication rule off on upgrade (see ``parse_block_flag``).
    """

    @pytest.mark.parametrize("value", ["true", "TRUE"])
    def test_the_string_true_still_enables(self, value):
        assert _parse_images_config({"replication": {"enabled": value}})["replication"]["enabled"]

    @pytest.mark.parametrize("value", ["false", "False", "0", ""])
    def test_other_strings_fail(self, value):
        with pytest.raises(ConfigValidationError, match=r"^images\.replication\.enabled must"):
            _parse_images_config({"replication": {"enabled": value}})

    @pytest.mark.parametrize("value", [0, 1, None])
    def test_other_types_fail(self, value):
        with pytest.raises(ConfigValidationError, match=r"\(a JSON boolean\), got "):
            _parse_images_config({"replication": {"enabled": value}})
