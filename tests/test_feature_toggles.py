"""
Tests for the Valkey and Aurora CLI feature toggles.

Covers the generic _get_feature_config / _update_feature_config helpers,
the Valkey and Aurora config functions, and verifies the FSx refactor
to use the same generic helpers still works.
"""

import json
import re
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


class TestGenericFeatureConfig:
    """Tests for the generic _get_feature_config / _update_feature_config helpers."""

    def test_get_feature_config_reads_from_context(self):
        from cli.stacks import _get_feature_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_path.write_text(
                json.dumps({"context": {"my_feature": {"enabled": True, "size": 10}}})
            )

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                result = _get_feature_config("my_feature", {"enabled": False, "size": 5})
                assert result["enabled"] is True
                assert result["size"] == 10

    def test_get_feature_config_uses_defaults_when_missing(self):
        from cli.stacks import _get_feature_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_path.write_text(json.dumps({"context": {}}))

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                result = _get_feature_config("my_feature", {"enabled": False, "size": 5})
                assert result["enabled"] is False
                assert result["size"] == 5

    def test_get_feature_config_no_cdk_json(self):
        from cli.stacks import _get_feature_config

        with (
            patch("cli.stacks._find_cdk_json", return_value=None),
            pytest.raises(RuntimeError, match=re.escape("cdk.json not found")),
        ):
            _get_feature_config("my_feature", {"enabled": False})

    def test_get_feature_config_region_override(self):
        from cli.stacks import _get_feature_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_path.write_text(
                json.dumps(
                    {
                        "context": {
                            "my_feature": {"enabled": True, "size": 5},
                            "my_feature_regions": {"us-west-2": {"size": 20}},
                        }
                    }
                )
            )

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                result = _get_feature_config(
                    "my_feature", {"enabled": False, "size": 5}, "us-west-2"
                )
                assert result["enabled"] is True
                assert result["size"] == 20
                assert result["is_region_specific"] is True

    def test_get_feature_config_no_region_override_falls_back(self):
        from cli.stacks import _get_feature_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_path.write_text(
                json.dumps({"context": {"my_feature": {"enabled": True, "size": 10}}})
            )

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                result = _get_feature_config(
                    "my_feature", {"enabled": False, "size": 5}, "us-west-2"
                )
                assert result["enabled"] is True
                assert result["size"] == 10
                assert result["is_region_specific"] is False

    def test_update_feature_config_writes_to_context(self):
        from cli.stacks import _update_feature_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_path.write_text(json.dumps({"context": {"my_feature": {"enabled": False}}}))

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                _update_feature_config(
                    "my_feature", {"enabled": True, "size": 10}, {"enabled": False}
                )

            result = json.loads(cdk_path.read_text())
            assert result["context"]["my_feature"]["enabled"] is True
            assert result["context"]["my_feature"]["size"] == 10

    def test_update_feature_config_creates_section(self):
        from cli.stacks import _update_feature_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_path.write_text(json.dumps({"context": {}}))

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                _update_feature_config(
                    "my_feature", {"enabled": True}, {"enabled": False, "size": 5}
                )

            result = json.loads(cdk_path.read_text())
            assert result["context"]["my_feature"]["enabled"] is True

    def test_update_feature_config_no_cdk_json(self):
        from cli.stacks import _update_feature_config

        with (
            patch("cli.stacks._find_cdk_json", return_value=None),
            pytest.raises(RuntimeError, match=re.escape("cdk.json not found")),
        ):
            _update_feature_config("my_feature", {"enabled": True}, {"enabled": False})

    def test_update_feature_config_skips_none_values(self):
        from cli.stacks import _update_feature_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_path.write_text(
                json.dumps({"context": {"my_feature": {"enabled": True, "size": 10}}})
            )

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                _update_feature_config(
                    "my_feature", {"enabled": True, "optional": None}, {"enabled": False}
                )

            result = json.loads(cdk_path.read_text())
            assert "optional" not in result["context"]["my_feature"]
            assert result["context"]["my_feature"]["size"] == 10


class TestValkeyConfig:
    """Tests for Valkey configuration functions."""

    def test_get_valkey_config(self):
        from cli.stacks import get_valkey_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_path.write_text(
                json.dumps(
                    {
                        "context": {
                            "valkey": {
                                "enabled": True,
                                "max_data_storage_gb": 10,
                                "max_ecpu_per_second": 10000,
                            }
                        }
                    }
                )
            )

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                result = get_valkey_config()
                assert result["enabled"] is True
                assert result["max_data_storage_gb"] == 10
                assert result["max_ecpu_per_second"] == 10000

    def test_get_valkey_config_defaults(self):
        from cli.stacks import get_valkey_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_path.write_text(json.dumps({"context": {}}))

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                result = get_valkey_config()
                assert result["enabled"] is False
                assert result["max_data_storage_gb"] == 5
                assert result["max_ecpu_per_second"] == 5000
                assert result["snapshot_retention_limit"] == 1

    def test_get_valkey_config_no_cdk_json(self):
        from cli.stacks import get_valkey_config

        with (
            patch("cli.stacks._find_cdk_json", return_value=None),
            pytest.raises(RuntimeError, match=re.escape("cdk.json not found")),
        ):
            get_valkey_config()

    def test_update_valkey_config_enable(self):
        from cli.stacks import update_valkey_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_path.write_text(json.dumps({"context": {"valkey": {"enabled": False}}}))

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                update_valkey_config(
                    {"enabled": True, "max_data_storage_gb": 10, "max_ecpu_per_second": 8000}
                )

            result = json.loads(cdk_path.read_text())
            assert result["context"]["valkey"]["enabled"] is True
            assert result["context"]["valkey"]["max_data_storage_gb"] == 10
            assert result["context"]["valkey"]["max_ecpu_per_second"] == 8000

    def test_update_valkey_config_disable(self):
        from cli.stacks import update_valkey_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_path.write_text(json.dumps({"context": {"valkey": {"enabled": True}}}))

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                update_valkey_config({"enabled": False})

            result = json.loads(cdk_path.read_text())
            assert result["context"]["valkey"]["enabled"] is False

    def test_update_valkey_config_creates_section(self):
        from cli.stacks import update_valkey_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_path.write_text(json.dumps({"context": {}}))

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                update_valkey_config({"enabled": True})

            result = json.loads(cdk_path.read_text())
            assert "valkey" in result["context"]
            assert result["context"]["valkey"]["enabled"] is True

    def test_update_valkey_config_no_cdk_json(self):
        from cli.stacks import update_valkey_config

        with (
            patch("cli.stacks._find_cdk_json", return_value=None),
            pytest.raises(RuntimeError, match=re.escape("cdk.json not found")),
        ):
            update_valkey_config({"enabled": True})


class TestAuroraConfig:
    """Tests for Aurora pgvector configuration functions."""

    def test_get_aurora_config(self):
        from cli.stacks import get_aurora_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_path.write_text(
                json.dumps(
                    {
                        "context": {
                            "aurora_pgvector": {
                                "enabled": True,
                                "min_acu": 2,
                                "max_acu": 32,
                                "deletion_protection": True,
                            }
                        }
                    }
                )
            )

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                result = get_aurora_config()
                assert result["enabled"] is True
                assert result["min_acu"] == 2
                assert result["max_acu"] == 32
                assert result["deletion_protection"] is True

    def test_get_aurora_config_defaults(self):
        from cli.stacks import get_aurora_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_path.write_text(json.dumps({"context": {}}))

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                result = get_aurora_config()
                assert result["enabled"] is False
                assert result["min_acu"] == 0
                assert result["max_acu"] == 16
                assert result["backup_retention_days"] == 7
                assert result["deletion_protection"] is False

    def test_get_aurora_config_no_cdk_json(self):
        from cli.stacks import get_aurora_config

        with (
            patch("cli.stacks._find_cdk_json", return_value=None),
            pytest.raises(RuntimeError, match=re.escape("cdk.json not found")),
        ):
            get_aurora_config()

    def test_update_aurora_config_enable(self):
        from cli.stacks import update_aurora_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_path.write_text(json.dumps({"context": {"aurora_pgvector": {"enabled": False}}}))

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                update_aurora_config(
                    {
                        "enabled": True,
                        "min_acu": 2,
                        "max_acu": 32,
                        "backup_retention_days": 14,
                        "deletion_protection": True,
                    }
                )

            result = json.loads(cdk_path.read_text())
            assert result["context"]["aurora_pgvector"]["enabled"] is True
            assert result["context"]["aurora_pgvector"]["min_acu"] == 2
            assert result["context"]["aurora_pgvector"]["max_acu"] == 32
            assert result["context"]["aurora_pgvector"]["backup_retention_days"] == 14
            assert result["context"]["aurora_pgvector"]["deletion_protection"] is True

    def test_update_aurora_config_disable(self):
        from cli.stacks import update_aurora_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_path.write_text(json.dumps({"context": {"aurora_pgvector": {"enabled": True}}}))

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                update_aurora_config({"enabled": False})

            result = json.loads(cdk_path.read_text())
            assert result["context"]["aurora_pgvector"]["enabled"] is False

    def test_update_aurora_config_creates_section(self):
        from cli.stacks import update_aurora_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_path.write_text(json.dumps({"context": {}}))

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                update_aurora_config({"enabled": True})

            result = json.loads(cdk_path.read_text())
            assert "aurora_pgvector" in result["context"]
            assert result["context"]["aurora_pgvector"]["enabled"] is True

    def test_update_aurora_config_no_cdk_json(self):
        from cli.stacks import update_aurora_config

        with (
            patch("cli.stacks._find_cdk_json", return_value=None),
            pytest.raises(RuntimeError, match=re.escape("cdk.json not found")),
        ):
            update_aurora_config({"enabled": True})


class TestFsxRefactored:
    """Verify FSx still works after refactoring to use generic helpers."""

    def test_get_fsx_config_still_works(self):
        from cli.stacks import get_fsx_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_path.write_text(
                json.dumps(
                    {"context": {"fsx_lustre": {"enabled": True, "storage_capacity_gib": 2400}}}
                )
            )

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                result = get_fsx_config()
                assert result["enabled"] is True
                assert result["storage_capacity_gib"] == 2400

    def test_update_fsx_config_still_works(self):
        from cli.stacks import update_fsx_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_path.write_text(json.dumps({"context": {"fsx_lustre": {"enabled": False}}}))

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                update_fsx_config({"enabled": True, "storage_capacity_gib": 2400})

            result = json.loads(cdk_path.read_text())
            assert result["context"]["fsx_lustre"]["enabled"] is True
            assert result["context"]["fsx_lustre"]["storage_capacity_gib"] == 2400

    def test_fsx_region_override_returns_merged_config(self):
        from cli.stacks import get_fsx_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_path.write_text(
                json.dumps(
                    {
                        "context": {
                            "fsx_lustre": {"enabled": True, "storage_capacity_gib": 1200},
                            "fsx_lustre_regions": {"us-west-2": {"storage_capacity_gib": 4800}},
                        }
                    }
                )
            )

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                result = get_fsx_config("us-west-2")
                assert result["storage_capacity_gib"] == 4800
                assert result["is_region_specific"] is True

                global_result = get_fsx_config()
                assert global_result["storage_capacity_gib"] == 1200
                assert global_result["is_region_specific"] is False
