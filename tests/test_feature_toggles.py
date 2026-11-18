"""
Tests for the Valkey and Aurora CLI feature toggles.

Covers the generic _get_feature_config / _update_feature_config helpers,
the Valkey and Aurora config functions, and verifies the FSx refactor
to use the same generic helpers still works.
"""

import json
import os
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

    def test_serialize_cdk_json_is_the_shared_on_disk_format(self):
        from cli.stacks import serialize_cdk_json

        document = {"context": {"_comment_x": "a \u2014 b", "items": [1, 2]}}

        assert serialize_cdk_json(document) == (
            b'{\n  "context": {\n    "_comment_x": "a \xe2\x80\x94 b",\n'
            b'    "items": [\n      1,\n      2\n    ]\n  }\n}\n'
        )

    def test_the_committed_cdk_json_is_in_the_shared_format(self):
        """The committed file is byte for byte what the CLI writes.

        Otherwise the first ``gco stacks ... enable`` rewrites every line of
        the file (inline arrays, blank lines) instead of the keys it changes.
        """
        from cli.stacks import serialize_cdk_json

        raw = (Path(__file__).resolve().parents[1] / "cdk.json").read_bytes()
        assert raw == serialize_cdk_json(json.loads(raw))

    def test_update_feature_config_keeps_utf8_and_ends_with_newline(self, tmp_path: Path):
        """A toggle writes the shared format: no \\uXXXX churn, one trailing newline."""
        from cli.stacks import _update_feature_config, serialize_cdk_json

        comment = "Valkey cache \u2014 see docs/CUSTOMIZATION.md \u2192 R\u00e9gions"
        cdk_path = tmp_path / "cdk.json"
        cdk_path.write_text(
            json.dumps({"context": {"_comment_valkey": comment, "valkey": {"enabled": False}}}),
            encoding="utf-8",
        )
        with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
            _update_feature_config("valkey", {"enabled": True}, {"enabled": False})

        raw = cdk_path.read_bytes()
        assert raw == serialize_cdk_json(json.loads(raw))
        assert comment.encode("utf-8") in raw
        assert b"\\u2014" not in raw
        assert raw.endswith(b"}\n")

    @pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
    def test_update_feature_config_through_symlink_updates_target(self, tmp_path: Path):
        """A symlinked cdk.json stays a link; the file it points at changes."""
        from cli.stacks import _update_feature_config

        real = tmp_path / "deployments" / "cdk.json"
        real.parent.mkdir()
        real.write_text(json.dumps({"context": {"valkey": {"enabled": False}}}), encoding="utf-8")
        link = tmp_path / "checkout" / "cdk.json"
        link.parent.mkdir()
        link.symlink_to(real)
        with patch("cli.stacks._find_cdk_json", return_value=link):
            _update_feature_config("valkey", {"enabled": True}, {"enabled": False})

        assert link.is_symlink()
        assert json.loads(real.read_text(encoding="utf-8"))["context"]["valkey"]["enabled"]
        assert sorted(path.name for path in link.parent.iterdir()) == ["cdk.json"]

    def test_update_feature_config_removes_keys(self, tmp_path: Path):
        """A key in ``remove`` is deleted unless the same call gives it a value."""
        from cli.stacks import _update_feature_config

        cdk_path = tmp_path / "cdk.json"
        cdk_path.write_text(
            json.dumps(
                {"context": {"my_feature": {"enabled": True, "stale": 1, "old": 2, "kept": 3}}}
            )
        )
        with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
            _update_feature_config(
                "my_feature",
                {"enabled": True, "stale": None, "kept": 4},
                {"enabled": False},
                remove=["stale", "old", "kept", "never_there"],
            )

        assert json.loads(cdk_path.read_text())["context"]["my_feature"] == {
            "enabled": True,
            "kept": 4,
        }

    def test_update_feature_config_removes_keys_from_the_region_overlay_only(self, tmp_path: Path):
        from cli.stacks import _update_feature_config

        cdk_path = tmp_path / "cdk.json"
        cdk_path.write_text(
            json.dumps(
                {
                    "context": {
                        "my_feature": {"enabled": False, "path": "global"},
                        "my_feature_regions": {"us-west-2": {"enabled": True, "path": "region"}},
                    }
                }
            )
        )
        with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
            _update_feature_config(
                "my_feature", {"enabled": True}, {"enabled": False}, "us-west-2", remove=["path"]
            )

        context = json.loads(cdk_path.read_text())["context"]
        assert context["my_feature_regions"] == {"us-west-2": {"enabled": True}}
        assert context["my_feature"] == {"enabled": False, "path": "global"}

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


class TestFsxEnableWritesWhatItShows:
    """``gco stacks fsx enable`` removes the settings it was not given.

    It used to skip every option left out, so an import path, export path or
    PERSISTENT throughput from an earlier run stayed in cdk.json although the
    confirmation summary did not show it, and the deprecated ``node_group``
    stayed too.
    """

    _STALE = {
        "enabled": False,
        "storage_capacity_gib": 2400,
        "deployment_type": "PERSISTENT_2",
        "per_unit_storage_throughput": 500,
        "data_compression_type": "LZ4",
        "import_path": "s3://example-bucket/in",
        "export_path": "s3://example-bucket/out",
        "auto_import_policy": "NEW",
        "node_group": {"instance_types": ["m5.large"]},
    }

    @staticmethod
    def _enable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> dict:
        from click.testing import CliRunner

        from cli.main import cli

        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(cli, ["stacks", "fsx", "enable", "-y", *args])
        assert result.exit_code == 0, result.output
        return json.loads((tmp_path / "cdk.json").read_text(encoding="utf-8"))["context"]

    def test_the_global_block_holds_exactly_the_shown_settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "cdk.json").write_text(json.dumps({"context": {"fsx_lustre": self._STALE}}))
        context = self._enable(tmp_path, monkeypatch, "-d", "SCRATCH_2")
        assert context["fsx_lustre"] == {
            "enabled": True,
            "storage_capacity_gib": 1200,
            "deployment_type": "SCRATCH_2",
            "data_compression_type": "LZ4",
        }

    def test_a_region_entry_drops_its_stale_settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "cdk.json").write_text(
            json.dumps(
                {
                    "context": {
                        "fsx_lustre": {"enabled": False},
                        "fsx_lustre_regions": {"us-west-2": self._STALE},
                    }
                }
            )
        )
        context = self._enable(tmp_path, monkeypatch, "-r", "us-west-2", "-d", "PERSISTENT_1")
        assert context["fsx_lustre_regions"]["us-west-2"] == {
            "enabled": True,
            "storage_capacity_gib": 1200,
            "deployment_type": "PERSISTENT_1",
            "per_unit_storage_throughput": 200,
            "data_compression_type": "LZ4",
        }
        assert context["fsx_lustre"] == {"enabled": False}
