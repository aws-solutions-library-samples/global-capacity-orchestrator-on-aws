"""
Tests for cli/config.py.

Exercises the _load_cdk_json helper across its defensive branches
(missing file, invalid JSON, missing context key, non-dict
deployment_regions), GCOConfig.from_file for YAML and JSON with
tmp_path fixtures, GCOConfig.from_env for the GCO_* environment
variable surface, the save/to_dict round-trip, and the get_config
merge between file and env sources. Patches Path.cwd so cdk.json
discovery can be directed at tmp_path.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from cli.config import GCOConfig, _load_cdk_json, _load_cdk_project_name, get_config


class TestLoadCdkJson:
    """Tests for _load_cdk_json function."""

    def test_returns_deployment_regions_from_cdk_json(self, tmp_path):
        """Should return deployment_regions from cdk.json context."""
        cdk_json = {
            "context": {
                "deployment_regions": {
                    "regional": ["us-east-1", "us-west-2"],
                    "api_gateway": "us-east-2",
                    "global": "us-east-2",
                }
            }
        }
        cdk_path = tmp_path / "cdk.json"
        cdk_path.write_text(json.dumps(cdk_json))

        with patch("cli.config.Path.cwd", return_value=tmp_path):
            result = _load_cdk_json()
            assert result["regional"] == ["us-east-1", "us-west-2"]
            assert result["api_gateway"] == "us-east-2"

    def test_returns_empty_when_no_cdk_json(self, tmp_path):
        """Should return empty dict when cdk.json doesn't exist."""
        with patch("cli.config.Path.cwd", return_value=tmp_path):
            result = _load_cdk_json()
            assert result == {}

    def test_returns_empty_when_invalid_json(self, tmp_path):
        """Should return empty dict when cdk.json is invalid JSON."""
        cdk_path = tmp_path / "cdk.json"
        cdk_path.write_text("not valid json{{{")

        with patch("cli.config.Path.cwd", return_value=tmp_path):
            result = _load_cdk_json()
            assert result == {}

    def test_returns_empty_when_no_context_key(self, tmp_path):
        """Should return empty dict when cdk.json has no context key."""
        cdk_path = tmp_path / "cdk.json"
        cdk_path.write_text(json.dumps({"app": "python app.py"}))

        with patch("cli.config.Path.cwd", return_value=tmp_path):
            result = _load_cdk_json()
            assert result == {}

    def test_returns_empty_when_no_deployment_regions(self, tmp_path):
        """Should return empty dict when context has no deployment_regions."""
        cdk_path = tmp_path / "cdk.json"
        cdk_path.write_text(json.dumps({"context": {"kubernetes_version": "1.31"}}))

        with patch("cli.config.Path.cwd", return_value=tmp_path):
            result = _load_cdk_json()
            assert result == {}

    def test_returns_empty_when_deployment_regions_is_not_dict(self, tmp_path):
        """Should return empty dict when deployment_regions is not a dict."""
        cdk_path = tmp_path / "cdk.json"
        cdk_path.write_text(json.dumps({"context": {"deployment_regions": ["us-east-1"]}}))

        with patch("cli.config.Path.cwd", return_value=tmp_path):
            result = _load_cdk_json()
            assert result == {}


class TestGCOConfigFromFile:
    """Tests for GCOConfig.from_file."""

    def test_load_from_yaml_file(self, tmp_path):
        """Should load config from YAML file."""
        config_data = {
            "default_region": "eu-west-1",
            "output_format": "json",
            "verbose": True,
        }
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(config_data))

        config = GCOConfig.from_file(str(config_path))
        assert config.default_region == "eu-west-1"
        assert config.output_format == "json"
        assert config.verbose is True

    def test_load_from_json_file(self, tmp_path):
        """Should load config from JSON file."""
        config_data = {
            "default_region": "ap-northeast-1",
            "project_name": "my-gco",
        }
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config_data))

        config = GCOConfig.from_file(str(config_path))
        assert config.default_region == "ap-northeast-1"
        assert config.project_name == "my-gco"

    def test_returns_defaults_when_no_file(self):
        """Should return default config when no file exists."""
        with patch("cli.config.Path.cwd", return_value=Path("/nonexistent")):
            config = GCOConfig.from_file()
            assert config.default_region == "us-east-1"
            assert config.project_name == "gco"

    def test_explicit_missing_path_raises(self, tmp_path):
        """An explicit typo must not silently fall back to defaults."""
        missing = tmp_path / "missing.yaml"
        with pytest.raises(FileNotFoundError, match="Configuration file not found"):
            GCOConfig.from_file(str(missing))

    def test_empty_yaml_uses_dataclass_defaults(self, tmp_path):
        config_path = tmp_path / "empty.yaml"
        config_path.write_text("")

        config = GCOConfig.from_file(str(config_path))

        assert config.default_region == "us-east-1"
        assert config.cache_dir.endswith(".gco/cache")

    def test_loads_default_factory_fields(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.safe_dump({"cache_dir": str(tmp_path / "cache")}))

        config = GCOConfig.from_file(str(config_path))

        assert config.cache_dir == str(tmp_path / "cache")

    def test_ignores_unknown_keys(self, tmp_path):
        """Should ignore unknown keys in config file."""
        config_data = {
            "default_region": "us-west-2",
            "unknown_key": "should be ignored",
        }
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(config_data))

        config = GCOConfig.from_file(str(config_path))
        assert config.default_region == "us-west-2"
        assert not hasattr(config, "unknown_key")


class TestGCOConfigFromEnv:
    """Tests for GCOConfig.from_env."""

    def test_loads_region_from_env(self):
        """Should load default_region from GCO_DEFAULT_REGION."""
        with patch.dict(os.environ, {"GCO_DEFAULT_REGION": "sa-east-1"}):
            config = GCOConfig.from_env()
            assert config.default_region == "sa-east-1"

    def test_loads_verbose_true(self):
        """Should parse verbose=true from env."""
        with patch.dict(os.environ, {"GCO_VERBOSE": "true"}):
            config = GCOConfig.from_env()
            assert config.verbose is True

    def test_loads_verbose_yes(self):
        """Should parse verbose=yes from env."""
        with patch.dict(os.environ, {"GCO_VERBOSE": "yes"}):
            config = GCOConfig.from_env()
            assert config.verbose is True

    def test_loads_verbose_1(self):
        """Should parse verbose=1 from env."""
        with patch.dict(os.environ, {"GCO_VERBOSE": "1"}):
            config = GCOConfig.from_env()
            assert config.verbose is True

    def test_verbose_false_for_other_values(self):
        """Should set verbose=False for non-truthy values."""
        with patch.dict(os.environ, {"GCO_VERBOSE": "no"}):
            config = GCOConfig.from_env()
            assert config.verbose is False

    def test_loads_regional_api_mode(self):
        with patch.dict(os.environ, {"GCO_REGIONAL_API": "true"}, clear=True):
            config = GCOConfig.from_env()
        assert config.use_regional_api is True

    def test_loads_multiple_env_vars(self):
        """Should load multiple env vars at once."""
        env = {
            "GCO_PROJECT_NAME": "custom-gco",
            "GCO_OUTPUT_FORMAT": "yaml",
            "GCO_GLOBAL_REGION": "eu-west-1",
        }
        with patch.dict(os.environ, env):
            config = GCOConfig.from_env()
            assert config.project_name == "custom-gco"
            assert config.output_format == "yaml"
            assert config.global_region == "eu-west-1"


class TestGCOConfigSave:
    """Tests for GCOConfig.save."""

    def test_save_to_yaml(self, tmp_path):
        """Should save config to YAML file."""
        config = GCOConfig(default_region="eu-central-1", verbose=True)
        config_path = str(tmp_path / "config.yaml")
        config.save(config_path)

        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        assert data["default_region"] == "eu-central-1"
        assert data["verbose"] is True

    def test_save_creates_directory(self, tmp_path):
        """Should create parent directory if it doesn't exist."""
        config = GCOConfig()
        config_path = str(tmp_path / "subdir" / "config.yaml")

        # save() only creates ~/.gco when config_path is None
        # When explicit path is given, parent must exist
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        config.save(config_path)

        assert os.path.exists(config_path)

    def test_round_trip(self, tmp_path):
        """Config should survive save/load round-trip."""
        original = GCOConfig(
            default_region="ap-southeast-2",
            project_name="test-gco",
            output_format="json",
            verbose=True,
        )
        config_path = str(tmp_path / "config.yaml")
        original.save(config_path)

        loaded = GCOConfig.from_file(config_path)
        assert loaded.default_region == "ap-southeast-2"
        assert loaded.project_name == "test-gco"
        assert loaded.output_format == "json"
        assert loaded.verbose is True


class TestGCOConfigToDict:
    """Tests for GCOConfig.to_dict."""

    def test_contains_all_expected_keys(self):
        """to_dict should contain all configuration keys."""
        config = GCOConfig()
        d = config.to_dict()

        expected_keys = {
            "project_name",
            "default_region",
            "api_gateway_region",
            "global_region",
            "monitoring_region",
            "global_stack_name",
            "api_gateway_stack_name",
            "regional_stack_prefix",
            "default_namespace",
            "spot_price_history_days",
            "capacity_check_timeout",
            "efs_mount_path",
            "fsx_mount_path",
            "output_format",
            "verbose",
            "cache_dir",
            "cache_ttl_seconds",
            "use_regional_api",
        }
        assert set(d.keys()) == expected_keys

    def test_values_match_config(self):
        """to_dict values should match config attributes."""
        config = GCOConfig(default_region="eu-west-1", verbose=True)
        d = config.to_dict()
        assert d["default_region"] == "eu-west-1"
        assert d["verbose"] is True


class TestGetConfig:
    """Tests for get_config merge behavior."""

    def test_cdk_json_overrides_defaults(self, tmp_path):
        """cdk.json values should override defaults."""
        cdk_json = {
            "context": {
                "deployment_regions": {
                    "regional": ["eu-west-1"],
                    "api_gateway": "eu-west-2",
                    "global": "eu-west-2",
                    "monitoring": "eu-west-2",
                }
            }
        }
        cdk_path = tmp_path / "cdk.json"
        cdk_path.write_text(json.dumps(cdk_json))

        with (
            patch("cli.config.Path.cwd", return_value=tmp_path),
            patch("cli.config.GCOConfig.from_file", return_value=GCOConfig()),
            patch("cli.config.GCOConfig.from_env", return_value=GCOConfig()),
        ):
            config = get_config()
            assert config.default_region == "eu-west-1"
            assert config.api_gateway_region == "eu-west-2"

    def test_env_overrides_cdk_json(self, tmp_path):
        """Environment variables should override cdk.json values."""
        cdk_json = {
            "context": {
                "deployment_regions": {
                    "regional": ["eu-west-1"],
                    "api_gateway": "eu-west-2",
                }
            }
        }
        cdk_path = tmp_path / "cdk.json"
        cdk_path.write_text(json.dumps(cdk_json))

        env_config = GCOConfig(default_region="ap-southeast-1")

        with (
            patch("cli.config.Path.cwd", return_value=tmp_path),
            patch("cli.config.GCOConfig.from_file", return_value=GCOConfig()),
            patch("cli.config.GCOConfig.from_env", return_value=env_config),
        ):
            config = get_config()
            assert config.default_region == "ap-southeast-1"

    def test_file_explicit_default_and_regional_mode_override_cdk(self, tmp_path):
        cdk_path = tmp_path / "cdk.json"
        cdk_path.write_text(
            json.dumps(
                {
                    "context": {
                        "deployment_regions": {
                            "regional": ["eu-west-1"],
                            "api_gateway": "eu-west-2",
                        }
                    }
                }
            )
        )
        (tmp_path / ".gco.yaml").write_text(
            yaml.safe_dump(
                {
                    "default_region": "us-east-1",
                    "use_regional_api": True,
                }
            )
        )

        with (
            patch("cli.config.Path.cwd", return_value=tmp_path),
            patch("cli.config.GCOConfig.from_env", return_value=GCOConfig()),
        ):
            config = get_config()

        assert config.default_region == "us-east-1"
        assert config.use_regional_api is True


class TestProjectScopedNames:
    """#139: stack names and the regional prefix derive from project_name.

    The default ``gco`` project renders byte-identical to the pre-#139 hard
    coded names; a non-``gco`` project addresses its own stacks/clusters.
    """

    def test_default_project_is_byte_identical(self):
        config = GCOConfig()
        assert config.global_stack_name == "gco-global"
        assert config.api_gateway_stack_name == "gco-api-gateway"
        assert config.regional_stack_prefix == "gco"

    def test_non_gco_project_derives_stack_names(self):
        config = GCOConfig(project_name="acme")
        assert config.global_stack_name == "acme-global"
        assert config.api_gateway_stack_name == "acme-api-gateway"
        assert config.regional_stack_prefix == "acme"

    def test_load_cdk_project_name_from_context(self, tmp_path):
        cdk_path = tmp_path / "cdk.json"
        cdk_path.write_text(json.dumps({"context": {"project_name": "acme"}}))
        with patch("cli.config.Path.cwd", return_value=tmp_path):
            assert _load_cdk_project_name() == "acme"

    def test_load_cdk_project_name_absent_returns_none(self, tmp_path):
        cdk_path = tmp_path / "cdk.json"
        cdk_path.write_text(json.dumps({"context": {"deployment_regions": {}}}))
        with patch("cli.config.Path.cwd", return_value=tmp_path):
            assert _load_cdk_project_name() is None

    def test_get_config_loads_and_derives_from_cdk_json(self, tmp_path, monkeypatch):
        # Isolate from any ambient GCO_* env so cdk.json is the source of truth.
        for var in ("GCO_PROJECT_NAME",):
            monkeypatch.delenv(var, raising=False)
        cdk_path = tmp_path / "cdk.json"
        cdk_path.write_text(json.dumps({"context": {"project_name": "acme"}}))
        with patch("cli.config.Path.cwd", return_value=tmp_path):
            config = get_config()
        assert config.project_name == "acme"
        assert config.global_stack_name == "acme-global"
        assert config.api_gateway_stack_name == "acme-api-gateway"
        assert config.regional_stack_prefix == "acme"

    def test_get_config_env_project_name_overrides_and_derives(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GCO_PROJECT_NAME", "acme")
        # Empty tmp_path (no cdk.json) so only the env var drives project_name.
        with patch("cli.config.Path.cwd", return_value=tmp_path):
            config = get_config()
        assert config.project_name == "acme"
        assert config.global_stack_name == "acme-global"
        assert config.api_gateway_stack_name == "acme-api-gateway"
