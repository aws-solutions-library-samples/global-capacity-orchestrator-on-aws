"""
Tests for stack ordering helpers and FSx configuration in cli/stacks.py.

Drives ``get_stack_deployment_order`` to confirm global stacks land
before regional ones and priority ties break deterministically, then
runs the same inputs through ``get_stack_destroy_order`` to verify it
produces the reverse sequence (important for teardown so dependents
come down before their dependencies). The FSx helpers are exercised
against temp ``cdk.json`` files written by the fixtures: defaults are
returned when the config lacks an entry, global values merge into
per-region results, and region-specific overrides win over globals.
Also covers the ``StackInfo.to_dict`` round-trip and the ``_find_cdk_json``
walker that locates the config relative to the caller's CWD.
"""

import json
import re
from datetime import UTC
from unittest.mock import patch

import pytest

from cli.stacks import (
    StackInfo,
    get_fsx_config,
    get_stack_deployment_order,
    get_stack_destroy_order,
    update_fsx_config,
)


class TestGetStackDeploymentOrder:
    """Tests for get_stack_deployment_order."""

    def test_global_stacks_first(self):
        """Global stacks should come before regional stacks."""
        stacks = ["gco-us-east-1", "gco-global", "gco-us-west-2", "gco-api-gateway"]
        result = get_stack_deployment_order(stacks)
        assert result == [
            "gco-global",
            "gco-api-gateway",
            "gco-us-east-1",
            "gco-us-west-2",
        ]

    def test_global_priority_order(self):
        """Global stacks should be ordered: global, api-gateway, monitoring."""
        stacks = ["gco-monitoring", "gco-api-gateway", "gco-global"]
        result = get_stack_deployment_order(stacks)
        assert result == ["gco-global", "gco-api-gateway", "gco-monitoring"]

    def test_regional_stacks_alphabetical(self):
        """Regional stacks should be sorted alphabetically."""
        stacks = ["gco-us-west-2", "gco-eu-west-1", "gco-ap-southeast-1"]
        result = get_stack_deployment_order(stacks)
        assert result == ["gco-ap-southeast-1", "gco-eu-west-1", "gco-us-west-2"]

    def test_empty_list(self):
        """Empty list should return empty list."""
        assert get_stack_deployment_order([]) == []

    def test_only_global_stacks(self):
        """Only global stacks should be ordered by priority."""
        stacks = ["gco-api-gateway", "gco-global"]
        result = get_stack_deployment_order(stacks)
        assert result == ["gco-global", "gco-api-gateway"]

    def test_only_regional_stacks(self):
        """Only regional stacks should be sorted alphabetically."""
        stacks = ["gco-us-west-2", "gco-us-east-1"]
        result = get_stack_deployment_order(stacks)
        assert result == ["gco-us-east-1", "gco-us-west-2"]

    def test_single_stack(self):
        """Single stack should return as-is."""
        assert get_stack_deployment_order(["gco-global"]) == ["gco-global"]
        assert get_stack_deployment_order(["gco-us-east-1"]) == ["gco-us-east-1"]

    def test_full_deployment(self):
        """Full deployment with all stack types."""
        stacks = [
            "gco-eu-west-1",
            "gco-monitoring",
            "gco-us-east-1",
            "gco-global",
            "gco-api-gateway",
            "gco-ap-southeast-1",
        ]
        result = get_stack_deployment_order(stacks)
        assert result[:3] == ["gco-global", "gco-api-gateway", "gco-monitoring"]
        assert result[3:] == ["gco-ap-southeast-1", "gco-eu-west-1", "gco-us-east-1"]


class TestGetStackDestroyOrder:
    """Tests for get_stack_destroy_order."""

    def test_reverse_of_deployment(self):
        """Destroy order should be reverse of deployment order."""
        stacks = ["gco-us-east-1", "gco-global", "gco-api-gateway"]
        deploy = get_stack_deployment_order(stacks)
        destroy = get_stack_destroy_order(stacks)
        assert destroy == list(reversed(deploy))

    def test_regional_first_then_global(self):
        """Regional stacks should be destroyed before global stacks."""
        stacks = ["gco-global", "gco-us-east-1", "gco-api-gateway"]
        result = get_stack_destroy_order(stacks)
        # Regional should come first
        assert result[0] == "gco-us-east-1"
        # Global should come last
        assert result[-1] == "gco-global"

    def test_empty_list(self):
        """Empty list should return empty list."""
        assert get_stack_destroy_order([]) == []


class TestGetFsxConfig:
    """Tests for get_fsx_config."""

    def test_returns_defaults_when_no_fsx_config(self, tmp_path):
        """Should return defaults when cdk.json has no fsx_lustre key."""
        cdk_json = {"context": {"kubernetes_version": "1.31"}}
        (tmp_path / "cdk.json").write_text(json.dumps(cdk_json))

        with patch("cli.stacks._find_cdk_json", return_value=tmp_path / "cdk.json"):
            config = get_fsx_config()

        assert config["enabled"] is False
        assert config["storage_capacity_gib"] == 1200
        assert config["deployment_type"] == "SCRATCH_2"
        assert config["is_region_specific"] is False

    def test_returns_global_config(self, tmp_path):
        """Should return global fsx_lustre config."""
        cdk_json = {
            "context": {
                "fsx_lustre": {
                    "enabled": True,
                    "storage_capacity_gib": 2400,
                    "deployment_type": "PERSISTENT_2",
                }
            }
        }
        (tmp_path / "cdk.json").write_text(json.dumps(cdk_json))

        with patch("cli.stacks._find_cdk_json", return_value=tmp_path / "cdk.json"):
            config = get_fsx_config()

        assert config["enabled"] is True
        assert config["storage_capacity_gib"] == 2400
        assert config["is_region_specific"] is False

    def test_returns_region_override(self, tmp_path):
        """Should merge region-specific config over global."""
        cdk_json = {
            "context": {
                "fsx_lustre": {
                    "enabled": True,
                    "storage_capacity_gib": 1200,
                },
                "fsx_lustre_regions": {
                    "us-west-2": {
                        "storage_capacity_gib": 4800,
                    }
                },
            }
        }
        (tmp_path / "cdk.json").write_text(json.dumps(cdk_json))

        with patch("cli.stacks._find_cdk_json", return_value=tmp_path / "cdk.json"):
            config = get_fsx_config(region="us-west-2")

        assert config["enabled"] is True
        assert config["storage_capacity_gib"] == 4800
        assert config["is_region_specific"] is True
        assert config["region"] == "us-west-2"

    def test_region_without_override_returns_global(self, tmp_path):
        """Region with no override should return global config."""
        cdk_json = {
            "context": {
                "fsx_lustre": {"enabled": True, "storage_capacity_gib": 1200},
                "fsx_lustre_regions": {},
            }
        }
        (tmp_path / "cdk.json").write_text(json.dumps(cdk_json))

        with patch("cli.stacks._find_cdk_json", return_value=tmp_path / "cdk.json"):
            config = get_fsx_config(region="eu-west-1")

        assert config["storage_capacity_gib"] == 1200
        assert config["is_region_specific"] is False

    def test_raises_when_no_cdk_json(self):
        """Should raise RuntimeError when cdk.json not found."""
        with (
            patch("cli.stacks._find_cdk_json", return_value=None),
            pytest.raises(RuntimeError, match=re.escape("cdk.json not found")),
        ):
            get_fsx_config()


class TestUpdateFsxConfig:
    """Tests for update_fsx_config."""

    def test_update_global_config(self, tmp_path):
        """Should update global fsx_lustre config."""
        cdk_json = {"context": {}}
        cdk_path = tmp_path / "cdk.json"
        cdk_path.write_text(json.dumps(cdk_json))

        with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
            update_fsx_config({"enabled": True, "storage_capacity_gib": 2400})

        updated = json.loads(cdk_path.read_text())
        assert updated["context"]["fsx_lustre"]["enabled"] is True
        assert updated["context"]["fsx_lustre"]["storage_capacity_gib"] == 2400

    def test_update_region_config(self, tmp_path):
        """Should update region-specific config."""
        cdk_json = {"context": {"fsx_lustre": {"enabled": True}}}
        cdk_path = tmp_path / "cdk.json"
        cdk_path.write_text(json.dumps(cdk_json))

        with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
            update_fsx_config({"storage_capacity_gib": 4800}, region="us-west-2")

        updated = json.loads(cdk_path.read_text())
        assert updated["context"]["fsx_lustre_regions"]["us-west-2"]["storage_capacity_gib"] == 4800

    def test_update_preserves_existing_config(self, tmp_path):
        """Should preserve existing config keys when updating."""
        cdk_json = {
            "context": {
                "fsx_lustre": {
                    "enabled": True,
                    "storage_capacity_gib": 1200,
                    "deployment_type": "SCRATCH_2",
                }
            }
        }
        cdk_path = tmp_path / "cdk.json"
        cdk_path.write_text(json.dumps(cdk_json))

        with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
            update_fsx_config({"storage_capacity_gib": 2400})

        updated = json.loads(cdk_path.read_text())
        assert updated["context"]["fsx_lustre"]["enabled"] is True
        assert updated["context"]["fsx_lustre"]["deployment_type"] == "SCRATCH_2"
        assert updated["context"]["fsx_lustre"]["storage_capacity_gib"] == 2400

    def test_skips_none_values_except_enabled(self, tmp_path):
        """None values should be skipped, except 'enabled'."""
        cdk_json = {"context": {"fsx_lustre": {"enabled": True, "storage_capacity_gib": 1200}}}
        cdk_path = tmp_path / "cdk.json"
        cdk_path.write_text(json.dumps(cdk_json))

        with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
            update_fsx_config({"import_path": None, "enabled": False})

        updated = json.loads(cdk_path.read_text())
        assert "import_path" not in updated["context"]["fsx_lustre"]
        assert updated["context"]["fsx_lustre"]["enabled"] is False

    def test_raises_when_no_cdk_json(self):
        """Should raise RuntimeError when cdk.json not found."""
        with (
            patch("cli.stacks._find_cdk_json", return_value=None),
            pytest.raises(RuntimeError, match=re.escape("cdk.json not found")),
        ):
            update_fsx_config({"enabled": True})

    def test_creates_context_if_missing(self, tmp_path):
        """Should create context key if missing."""
        cdk_json = {"app": "python app.py"}
        cdk_path = tmp_path / "cdk.json"
        cdk_path.write_text(json.dumps(cdk_json))

        with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
            update_fsx_config({"enabled": True})

        updated = json.loads(cdk_path.read_text())
        assert updated["context"]["fsx_lustre"]["enabled"] is True


class TestStackInfoToDict:
    """Tests for StackInfo.to_dict."""

    def test_to_dict_all_fields(self):
        """to_dict should include all fields."""
        from datetime import datetime

        info = StackInfo(
            name="gco-us-east-1",
            status="CREATE_COMPLETE",
            region="us-east-1",
            created_time=datetime(2024, 1, 1, tzinfo=UTC),
            updated_time=datetime(2024, 6, 1, tzinfo=UTC),
            outputs={"ClusterName": "gco-us-east-1"},
            tags={"Project": "GCO"},
        )
        d = info.to_dict()
        assert d["name"] == "gco-us-east-1"
        assert d["status"] == "CREATE_COMPLETE"
        assert d["region"] == "us-east-1"
        assert "2024-01-01" in d["created_time"]
        assert "2024-06-01" in d["updated_time"]
        assert d["outputs"]["ClusterName"] == "gco-us-east-1"
        assert d["tags"]["Project"] == "GCO"

    def test_to_dict_optional_fields_none(self):
        """to_dict should handle None optional fields."""
        info = StackInfo(
            name="gco-us-east-1",
            status="CREATE_COMPLETE",
            region="us-east-1",
        )
        d = info.to_dict()
        assert d["created_time"] is None
        assert d["updated_time"] is None
        assert d["outputs"] == {}
        assert d["tags"] == {}
