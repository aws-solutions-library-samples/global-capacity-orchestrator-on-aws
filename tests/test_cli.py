"""
Foundational tests for the CLI modules.

Covers cli/config.GCOConfig defaults and round-trips: construction
from dict, to_dict serialization, from_file for both YAML and JSON,
from_env for the GCO_* variable set, and the get_config singleton
merging file and env sources. Sits alongside the more targeted
test_cli_config.py and test_cli_commands.py suites — this file is
the broad happy-path smoke test.
"""

import json
import os
import tempfile
from datetime import datetime
from unittest.mock import patch

import yaml


class TestGCOConfig:
    """Tests for CLI configuration management."""

    def test_default_config(self):
        """Test default configuration values."""
        from cli.config import GCOConfig

        config = GCOConfig()
        assert config.project_name == "gco"
        assert config.default_region == "us-east-1"
        assert config.api_gateway_region == "us-east-2"
        assert config.default_namespace == "gco-jobs"
        assert config.output_format == "table"
        assert config.verbose is False

    def test_config_from_dict(self):
        """Test creating config from dictionary."""
        from cli.config import GCOConfig

        config = GCOConfig(
            project_name="test-project",
            default_region="us-west-2",
            verbose=True,
        )
        assert config.project_name == "test-project"
        assert config.default_region == "us-west-2"
        assert config.verbose is True

    def test_config_to_dict(self):
        """Test converting config to dictionary."""
        from cli.config import GCOConfig

        config = GCOConfig()
        config_dict = config.to_dict()

        assert isinstance(config_dict, dict)
        assert "project_name" in config_dict
        assert "default_region" in config_dict
        assert "default_namespace" in config_dict

    def test_config_from_yaml_file(self):
        """Test loading config from YAML file."""
        from cli.config import GCOConfig

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(
                {
                    "project_name": "yaml-project",
                    "default_region": "eu-west-1",
                    "verbose": True,
                },
                f,
            )
            f.flush()

            config = GCOConfig.from_file(f.name)
            assert config.project_name == "yaml-project"
            assert config.default_region == "eu-west-1"
            assert config.verbose is True

            os.unlink(f.name)

    def test_config_from_json_file(self):
        """Test loading config from JSON file."""
        from cli.config import GCOConfig

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(
                {
                    "project_name": "json-project",
                    "default_region": "ap-northeast-1",
                },
                f,
            )
            f.flush()

            config = GCOConfig.from_file(f.name)
            assert config.project_name == "json-project"
            assert config.default_region == "ap-northeast-1"

            os.unlink(f.name)

    def test_config_from_env(self):
        """Test loading config from environment variables."""
        from cli.config import GCOConfig

        with patch.dict(
            os.environ,
            {
                "GCO_PROJECT_NAME": "env-project",
                "GCO_DEFAULT_REGION": "sa-east-1",
                "GCO_VERBOSE": "true",
            },
        ):
            config = GCOConfig.from_env()
            assert config.project_name == "env-project"
            assert config.default_region == "sa-east-1"
            assert config.verbose is True

    def test_get_config_merges_sources(self):
        """Test that get_config merges file and env sources."""
        from cli.config import get_config

        # Should not raise even without config file
        config = get_config()
        assert config is not None
        assert isinstance(config.project_name, str)


class TestCapacityChecker:
    """Tests for capacity checking functionality."""

    def test_instance_info_gpu(self):
        """GPU instance info comes from the live EC2 description, not a table.

        There is deliberately no checked-in specification catalog behind
        get_instance_info, so this asserts the EC2 round-trip and the mapping
        rather than a hardcoded expectation.
        """
        from unittest.mock import MagicMock, patch

        from cli.capacity import CapacityChecker

        record = {
            "InstanceType": "g4dn.xlarge",
            "VCpuInfo": {"DefaultVCpus": 4, "DefaultCores": 2, "DefaultThreadsPerCore": 2},
            "MemoryInfo": {"SizeInMiB": 16384},
            "ProcessorInfo": {"SupportedArchitectures": ["x86_64"]},
            "GpuInfo": {
                "Gpus": [
                    {
                        "Name": "T4",
                        "Manufacturer": "NVIDIA",
                        "Count": 1,
                        "MemoryInfo": {"SizeInMiB": 16384},
                    }
                ],
                "TotalGpuMemoryInMiB": 16384,
            },
        }

        with patch("cli.capacity.checker.boto3.Session") as session_cls:
            ec2 = MagicMock()
            ec2.describe_instance_types.return_value = {"InstanceTypes": [record]}
            session_cls.return_value.client.return_value = ec2
            checker = CapacityChecker()
            info = checker.get_instance_info("g4dn.xlarge")

        assert info is not None
        assert info.instance_type == "g4dn.xlarge"
        assert info.vcpus == 4
        assert info.memory_gib == 16
        assert info.gpu_count == 1
        assert info.gpu_type == "T4"
        assert info.is_gpu is True
        ec2.describe_instance_types.assert_called_once_with(InstanceTypes=["g4dn.xlarge"])

    def test_instance_info_returns_none_when_region_does_not_offer_type(self):
        """An empty EC2 result is "not offered here", reported as None."""
        from unittest.mock import MagicMock, patch

        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.boto3.Session") as session_cls:
            ec2 = MagicMock()
            ec2.describe_instance_types.return_value = {"InstanceTypes": []}
            session_cls.return_value.client.return_value = ec2
            checker = CapacityChecker()
            assert checker.get_instance_info("p5.48xlarge") is None

    def test_spot_price_info_dataclass(self):
        """Test SpotPriceInfo dataclass."""
        from cli.capacity import SpotPriceInfo

        info = SpotPriceInfo(
            instance_type="g4dn.xlarge",
            availability_zone="us-east-1a",
            current_price=0.50,
            avg_price_7d=0.45,
            min_price_7d=0.40,
            max_price_7d=0.60,
            price_stability=0.85,
        )

        assert info.instance_type == "g4dn.xlarge"
        assert info.current_price == 0.50
        assert info.price_stability == 0.85

    def test_capacity_estimate_dataclass(self):
        """Test CapacityEstimate dataclass."""
        from cli.capacity import CapacityEstimate

        estimate = CapacityEstimate(
            instance_type="g4dn.xlarge",
            region="us-east-1",
            availability_zone="us-east-1a",
            capacity_type="spot",
            availability="high",
            confidence=0.9,
            price_per_hour=0.50,
            recommendation="Good for spot",
        )

        assert estimate.instance_type == "g4dn.xlarge"
        assert estimate.capacity_type == "spot"
        assert estimate.availability == "high"


class TestJobManager:
    """Tests for job management functionality."""

    def test_job_info_dataclass(self):
        """Test JobInfo dataclass."""
        from cli.jobs import JobInfo

        job = JobInfo(
            name="test-job",
            namespace="gco-jobs",
            region="us-east-1",
            status="running",
            active_pods=2,
            succeeded_pods=0,
            failed_pods=0,
        )

        assert job.name == "test-job"
        assert job.is_complete is False
        assert job.duration_seconds is None

    def test_job_info_completed(self):
        """Test JobInfo for completed job."""
        from cli.jobs import JobInfo

        job = JobInfo(
            name="completed-job",
            namespace="gco-jobs",
            region="us-east-1",
            status="succeeded",
            start_time=datetime(2024, 1, 1, 10, 0, 0),
            completion_time=datetime(2024, 1, 1, 10, 30, 0),
            succeeded_pods=1,
        )

        assert job.is_complete is True
        assert job.duration_seconds == 1800  # 30 minutes

    def test_load_manifests_single_file(self):
        """Test loading manifests from a single YAML file."""
        from cli.jobs import JobManager

        manager = JobManager()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {"name": "test-job"},
                },
                f,
            )
            f.flush()

            manifests = manager.load_manifests(f.name)
            assert len(manifests) == 1
            assert manifests[0]["kind"] == "Job"

            os.unlink(f.name)

    def test_load_manifests_multi_document(self):
        """Test loading multi-document YAML file."""
        from cli.jobs import JobManager

        manager = JobManager()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("apiVersion: v1\nkind: Namespace\nmetadata:\n  name: test\n")
            f.write("---\n")
            f.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: test-job\n")
            f.flush()

            manifests = manager.load_manifests(f.name)
            assert len(manifests) == 2
            assert manifests[0]["kind"] == "Namespace"
            assert manifests[1]["kind"] == "Job"

            os.unlink(f.name)


class TestOutputFormatter:
    """Tests for output formatting."""

    def test_format_json(self):
        """Test JSON output format."""
        from cli.output import OutputFormatter

        formatter = OutputFormatter()
        formatter.set_format("json")

        data = {"name": "test", "value": 123}
        output = formatter.format(data)

        parsed = json.loads(output)
        assert parsed["name"] == "test"
        assert parsed["value"] == 123

    def test_format_yaml(self):
        """Test YAML output format."""
        from cli.output import OutputFormatter

        formatter = OutputFormatter()
        formatter.set_format("yaml")

        data = {"name": "test", "value": 123}
        output = formatter.format(data)

        parsed = yaml.safe_load(output)
        assert parsed["name"] == "test"
        assert parsed["value"] == 123

    def test_format_table(self):
        """Test table output format."""
        from cli.output import OutputFormatter

        formatter = OutputFormatter()
        formatter.set_format("table")

        data = [
            {"name": "job1", "status": "running"},
            {"name": "job2", "status": "completed"},
        ]
        output = formatter.format(data, columns=["name", "status"])

        assert "NAME" in output
        assert "STATUS" in output
        assert "job1" in output
        assert "running" in output

    def test_format_empty_data(self):
        """Test formatting empty data."""
        from cli.output import OutputFormatter

        formatter = OutputFormatter()
        formatter.set_format("table")

        output = formatter.format([])
        assert "No results" in output

    def test_format_none_data(self):
        """Test formatting None data."""
        from cli.output import OutputFormatter

        formatter = OutputFormatter()
        output = formatter.format(None)
        assert "No data" in output

    def test_format_datetime(self):
        """Test formatting datetime values."""
        from cli.output import OutputFormatter

        formatter = OutputFormatter()
        formatter.set_format("json")

        data = {"timestamp": datetime(2024, 1, 15, 10, 30, 0)}
        output = formatter.format(data)

        parsed = json.loads(output)
        assert "2024-01-15" in parsed["timestamp"]


class TestFileSystemClient:
    """Tests for file system client."""

    def test_file_system_info_dataclass(self):
        """Test FileSystemInfo dataclass."""
        from cli.files import FileSystemInfo

        fs = FileSystemInfo(
            file_system_id="fs-12345678",
            file_system_type="efs",
            region="us-east-1",
            dns_name="fs-12345678.efs.us-east-1.amazonaws.com",
            status="available",
        )

        assert fs.file_system_id == "fs-12345678"
        assert fs.file_system_type == "efs"
        assert fs.tags == {}

    def test_file_info_dataclass(self):
        """Test FileInfo dataclass."""
        from cli.files import FileInfo

        file = FileInfo(
            path="/data/output.txt",
            name="output.txt",
            is_directory=False,
            size_bytes=1024,
        )

        assert file.path == "/data/output.txt"
        assert file.is_directory is False
        assert file.size_bytes == 1024


class TestAWSClient:
    """Tests for AWS client."""

    def test_regional_stack_dataclass(self):
        """Test RegionalStack dataclass."""
        from cli.aws_client import RegionalStack

        stack = RegionalStack(
            region="us-east-1",
            stack_name="gco-us-east-1",
            cluster_name="gco-us-east-1",
            status="CREATE_COMPLETE",
            efs_file_system_id="fs-12345678",
        )

        assert stack.region == "us-east-1"
        assert stack.status == "CREATE_COMPLETE"

    def test_api_endpoint_dataclass(self):
        """Test ApiEndpoint dataclass."""
        from cli.aws_client import ApiEndpoint

        endpoint = ApiEndpoint(
            url="https://abc123.execute-api.us-east-2.amazonaws.com/prod",
            region="us-east-2",
            api_id="abc123",
        )

        assert endpoint.url.startswith("https://")
        assert endpoint.region == "us-east-2"


class TestCLIMain:
    """Tests for CLI main entry point."""

    def test_cli_imports(self):
        """Test that CLI main module imports correctly."""
        from cli.main import capacity, cli, files, jobs, stacks

        assert cli is not None
        assert jobs is not None
        assert capacity is not None
        assert stacks is not None
        assert files is not None

    def test_cli_version(self):
        """Test CLI version is a valid semver string."""
        from cli import __version__

        # Version should be a valid semver-like string (x.y.z)
        parts = __version__.split(".")
        assert len(parts) == 3
        assert all(part.isdigit() for part in parts)

    def test_cli_exports(self):
        """Test CLI package exports."""
        from cli import (
            CapacityChecker,
            FileSystemClient,
            GCOAWSClient,
            GCOConfig,
            JobManager,
            OutputFormatter,
            get_aws_client,
            get_capacity_checker,
            get_config,
            get_file_system_client,
            get_job_manager,
            get_output_formatter,
        )

        # Verify imports work (suppress unused variable warnings)
        _ = (
            CapacityChecker,
            FileSystemClient,
            JobManager,
            GCOAWSClient,
            GCOConfig,
            OutputFormatter,
        )

        # All exports should be callable or classes
        assert callable(get_config)
        assert callable(get_aws_client)
        assert callable(get_job_manager)
        assert callable(get_capacity_checker)
        assert callable(get_file_system_client)
        assert callable(get_output_formatter)


# =============================================================================
# Additional coverage tests for cli/__init__.py
# =============================================================================


class TestCliInitImportFallback:
    """Tests for cli/__init__.py import fallback."""

    def test_version_fallback_when_gco_not_installed(self):
        """Test version fallback when gco package is not installed."""
        import cli

        assert hasattr(cli, "__version__")
        assert cli.__version__ is not None


# =============================================================================
# Additional coverage tests for cli/config.py
# =============================================================================


class TestConfigLoadCdkJson:
    """Tests for _load_cdk_json function."""

    def test_load_cdk_json_exception_handling(self, tmp_path, monkeypatch):
        """Test _load_cdk_json handles malformed JSON gracefully."""
        from cli.config import _load_cdk_json

        cdk_json = tmp_path / "cdk.json"
        cdk_json.write_text("{ invalid json }")

        monkeypatch.chdir(tmp_path)
        result = _load_cdk_json()
        assert result == {}

    def test_load_cdk_json_missing_context(self, tmp_path, monkeypatch):
        """Test _load_cdk_json handles missing context key."""
        from cli.config import _load_cdk_json

        cdk_json = tmp_path / "cdk.json"
        cdk_json.write_text('{"app": "python app.py"}')

        monkeypatch.chdir(tmp_path)
        result = _load_cdk_json()
        assert result == {}


class TestGCOConfigFromFileExtended:
    """Extended tests for GCOConfig.from_file method."""

    def test_from_file_ignores_unknown_fields(self, tmp_path):
        """Test from_file ignores unknown configuration fields."""
        from cli.config import GCOConfig

        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
project_name: test
unknown_field: should_be_ignored
another_unknown: also_ignored
""")

        config = GCOConfig.from_file(str(config_file))
        assert config.project_name == "test"
        assert not hasattr(config, "unknown_field")


class TestGCOConfigSaveExtended:
    """Extended tests for GCOConfig.save method."""

    def test_save_creates_directory(self, tmp_path, monkeypatch):
        """Test save creates .gco directory if it doesn't exist."""
        from pathlib import Path

        from cli.config import GCOConfig

        fake_home = tmp_path / "fake_home"
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        config = GCOConfig(project_name="saved-project")
        config_path = str(tmp_path / "test_config.yaml")
        config.save(config_path)

        assert Path(config_path).exists()

    def test_save_to_default_location(self, tmp_path, monkeypatch):
        """Test save to default location creates config file."""
        from pathlib import Path

        from cli.config import GCOConfig

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        config = GCOConfig(project_name="default-save")
        config.save()

        expected_path = fake_home / ".gco" / "config.yaml"
        assert expected_path.exists()


class TestGetConfigMerging:
    """Tests for get_config function merging behavior."""

    def test_get_config_merges_cdk_json(self, tmp_path, monkeypatch):
        """Test get_config merges cdk.json settings."""
        from cli.config import get_config

        cdk_json = tmp_path / "cdk.json"
        cdk_json.write_text(
            json.dumps(
                {
                    "context": {
                        "deployment_regions": {
                            "api_gateway": "eu-west-1",
                            "global": "eu-west-2",
                            "monitoring": "eu-west-3",
                            "regional": ["ap-northeast-1"],
                        }
                    }
                }
            )
        )

        monkeypatch.chdir(tmp_path)
        for key in list(os.environ.keys()):
            if key.startswith("GCO_"):
                monkeypatch.delenv(key, raising=False)

        config = get_config()
        assert config.api_gateway_region == "eu-west-1"
        assert config.global_region == "eu-west-2"
        assert config.monitoring_region == "eu-west-3"
        assert config.default_region == "ap-northeast-1"


class TestCliFromEnvExtended:
    """Extended tests for GCOConfig.from_env method."""

    def test_from_env_with_all_variables(self, monkeypatch):
        """Test from_env loads all environment variables."""
        from cli.config import GCOConfig

        monkeypatch.setenv("GCO_PROJECT_NAME", "env-project")
        monkeypatch.setenv("GCO_DEFAULT_REGION", "sa-east-1")
        monkeypatch.setenv("GCO_API_GATEWAY_REGION", "sa-east-2")
        monkeypatch.setenv("GCO_GLOBAL_REGION", "sa-east-3")
        monkeypatch.setenv("GCO_MONITORING_REGION", "sa-east-4")
        monkeypatch.setenv("GCO_DEFAULT_NAMESPACE", "custom-ns")
        monkeypatch.setenv("GCO_OUTPUT_FORMAT", "json")
        monkeypatch.setenv("GCO_VERBOSE", "true")
        monkeypatch.setenv(
            "GCO_CACHE_DIR",
            "/tmp/cache",  # nosec B108 - test fixture using temp directory
        )

        config = GCOConfig.from_env()

        assert config.project_name == "env-project"
        assert config.default_region == "sa-east-1"
        assert config.api_gateway_region == "sa-east-2"
        assert config.global_region == "sa-east-3"
        assert config.monitoring_region == "sa-east-4"
        assert config.default_namespace == "custom-ns"
        assert config.output_format == "json"
        assert config.verbose is True
        assert config.cache_dir == "/tmp/cache"  # nosec B108 - test fixture using temp directory

    def test_from_env_verbose_variations(self, monkeypatch):
        """Test from_env handles various verbose values."""
        from cli.config import GCOConfig

        for value in ["1", "yes", "TRUE", "True"]:
            monkeypatch.setenv("GCO_VERBOSE", value)
            config = GCOConfig.from_env()
            assert config.verbose is True

        for value in ["0", "no", "false", "FALSE"]:
            monkeypatch.setenv("GCO_VERBOSE", value)
            config = GCOConfig.from_env()
            assert config.verbose is False
