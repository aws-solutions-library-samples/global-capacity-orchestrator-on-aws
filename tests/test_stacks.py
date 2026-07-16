"""
Tests for cli/stacks.py — the stack management helpers used by
`gco stacks` commands.

Focuses on _detect_container_runtime: CDK_DOCKER env override takes
priority, docker is selected when the binary is on PATH and
`docker info` returns 0, finch is the fallback when docker isn't
running, and None is returned when nothing is available. Also covers
docker info timeout handling. Uses an autouse fixture to reset the
module-level runtime cache so tests run in any order.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


class TestContainerRuntimeDetection:
    """Tests for container runtime detection."""

    @pytest.fixture(autouse=True)
    def reset_runtime_cache(self):
        """Reset the container runtime cache before each test."""
        import cli.stacks as stacks_module

        stacks_module._container_runtime_checked = False
        stacks_module._container_runtime_cache = None
        yield
        stacks_module._container_runtime_checked = False
        stacks_module._container_runtime_cache = None

    def test_detect_cdk_docker_env_var(self):
        """Test that CDK_DOCKER env var is respected."""
        from cli.stacks import _detect_container_runtime

        with patch.dict(os.environ, {"CDK_DOCKER": "podman"}):
            result = _detect_container_runtime()
            assert result == "podman"

    def test_detect_docker_available(self):
        """Test detection when docker is available and running."""
        from cli.stacks import _detect_container_runtime

        with patch.dict(os.environ, {}, clear=True):
            # Remove CDK_DOCKER if set
            os.environ.pop("CDK_DOCKER", None)

            with patch("shutil.which") as mock_which:
                mock_which.return_value = "/usr/bin/docker"

                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0)
                    result = _detect_container_runtime()
                    assert result == "docker"

    def test_detect_docker_not_running(self):
        """Test fallback to finch when docker is not running."""
        from cli.stacks import _detect_container_runtime

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CDK_DOCKER", None)

            with patch("shutil.which") as mock_which:

                def which_side_effect(cmd):
                    if cmd == "docker":
                        return "/usr/bin/docker"
                    if cmd == "finch":
                        return "/usr/local/bin/finch"
                    return None

                mock_which.side_effect = which_side_effect

                with patch("subprocess.run") as mock_run:

                    def run_side_effect(cmd, **kwargs):
                        if cmd[0] == "docker":
                            return MagicMock(returncode=1)  # Docker not running
                        if cmd[0] == "finch":
                            return MagicMock(returncode=0)  # Finch running
                        return MagicMock(returncode=1)

                    mock_run.side_effect = run_side_effect
                    result = _detect_container_runtime()
                    assert result == "finch"

    def test_detect_no_runtime_available(self):
        """Test when no container runtime is available."""
        from cli.stacks import _detect_container_runtime

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CDK_DOCKER", None)

            with patch("shutil.which") as mock_which:
                mock_which.return_value = None
                result = _detect_container_runtime()
                assert result is None

    def test_detect_docker_timeout(self):
        """Test handling of docker info timeout."""
        from cli.stacks import _detect_container_runtime

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CDK_DOCKER", None)

            with patch("shutil.which") as mock_which:
                mock_which.return_value = "/usr/bin/docker"

                with patch("subprocess.run") as mock_run:
                    mock_run.side_effect = subprocess.TimeoutExpired("docker", 5)
                    result = _detect_container_runtime()
                    # Should return None since docker timed out and finch not found
                    assert result is None


class TestStackManager:
    """Tests for StackManager class."""

    def test_stack_manager_init(self):
        """Test StackManager initialization."""
        from cli.stacks import StackManager

        config = MagicMock()
        manager = StackManager(config)
        assert manager.config == config
        assert manager.project_root is not None

    def test_find_project_root_with_cdk_json(self):
        """Test finding project root when cdk.json exists."""
        from cli.stacks import StackManager

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create cdk.json
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_path.write_text("{}")

            with patch("pathlib.Path.cwd", return_value=Path(tmpdir)):
                config = MagicMock()
                manager = StackManager(config, project_root=Path(tmpdir))
                assert manager.project_root == Path(tmpdir)

    def test_find_cdk_in_path(self):
        """Test finding CDK executable in PATH."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="/usr/local/bin/cdk\n")
            manager = StackManager(config)
            assert "cdk" in manager._cdk_path

    def test_find_cdk_fallback_to_npx(self):
        """Test fallback to npx cdk when cdk not in PATH."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "which")

            with patch("os.path.exists", return_value=False):
                manager = StackManager(config)
                assert manager._cdk_path == "npx cdk"


class TestStackInfo:
    """Tests for StackInfo dataclass."""

    def test_stack_info_creation(self):
        """Test creating StackInfo."""
        from datetime import datetime

        from cli.stacks import StackInfo

        info = StackInfo(
            name="test-stack",
            status="CREATE_COMPLETE",
            region="us-east-1",
            created_time=datetime(2024, 1, 1, 10, 0, 0),
        )

        assert info.name == "test-stack"
        assert info.status == "CREATE_COMPLETE"
        assert info.region == "us-east-1"

    def test_stack_info_to_dict(self):
        """Test StackInfo to_dict method."""
        from datetime import datetime

        from cli.stacks import StackInfo

        info = StackInfo(
            name="test-stack",
            status="CREATE_COMPLETE",
            region="us-east-1",
            created_time=datetime(2024, 1, 1, 10, 0, 0),
            outputs={"OutputKey": "OutputValue"},
            tags={"Environment": "test"},
        )

        result = info.to_dict()
        assert result["name"] == "test-stack"
        assert result["status"] == "CREATE_COMPLETE"
        assert result["outputs"] == {"OutputKey": "OutputValue"}
        assert result["tags"] == {"Environment": "test"}
        assert "2024-01-01" in result["created_time"]

    def test_stack_info_defaults(self):
        """Test StackInfo default values."""
        from cli.stacks import StackInfo

        info = StackInfo(
            name="test-stack",
            status="PENDING",
            region="us-west-2",
        )

        assert info.created_time is None
        assert info.updated_time is None
        assert info.outputs == {}
        assert info.tags == {}


class TestFsxConfig:
    """Tests for FSx configuration functions."""

    def test_get_fsx_config(self):
        """Test getting FSx config from cdk.json."""
        from cli.stacks import get_fsx_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_config = {
                "context": {
                    "fsx_lustre": {
                        "enabled": True,
                        "storage_capacity_gib": 2400,
                    }
                }
            }
            cdk_path.write_text(json.dumps(cdk_config))

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                result = get_fsx_config()
                assert result["enabled"] is True
                assert result["storage_capacity_gib"] == 2400

    def test_get_fsx_config_defaults(self):
        """Test getting FSx config with defaults when not configured."""
        from cli.stacks import get_fsx_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_config = {"context": {}}
            cdk_path.write_text(json.dumps(cdk_config))

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                result = get_fsx_config()
                assert result["enabled"] is False
                assert result["storage_capacity_gib"] == 1200

    def test_get_fsx_config_no_cdk_json(self):
        """Test error when cdk.json not found."""
        from cli.stacks import get_fsx_config

        with (
            patch("cli.stacks._find_cdk_json", return_value=None),
            pytest.raises(RuntimeError, match="cdk.json not found"),
        ):
            get_fsx_config()

    def test_update_fsx_config_enable(self):
        """Test enabling FSx in cdk.json."""
        from cli.stacks import update_fsx_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_config = {"context": {"fsx_lustre": {"enabled": False}}}
            cdk_path.write_text(json.dumps(cdk_config))

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                update_fsx_config({"enabled": True, "storage_capacity_gib": 2400})

            # Verify the file was updated
            with open(cdk_path, encoding="utf-8") as f:
                result = json.load(f)
            assert result["context"]["fsx_lustre"]["enabled"] is True
            assert result["context"]["fsx_lustre"]["storage_capacity_gib"] == 2400

    def test_update_fsx_config_disable(self):
        """Test disabling FSx in cdk.json."""
        from cli.stacks import update_fsx_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_config = {"context": {"fsx_lustre": {"enabled": True}}}
            cdk_path.write_text(json.dumps(cdk_config))

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                update_fsx_config({"enabled": False})

            with open(cdk_path, encoding="utf-8") as f:
                result = json.load(f)
            assert result["context"]["fsx_lustre"]["enabled"] is False

    def test_update_fsx_config_creates_section(self):
        """Test that update creates fsx_lustre section if missing."""
        from cli.stacks import update_fsx_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_config = {"context": {}}
            cdk_path.write_text(json.dumps(cdk_config))

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                update_fsx_config({"enabled": True})

            with open(cdk_path, encoding="utf-8") as f:
                result = json.load(f)
            assert "fsx_lustre" in result["context"]
            assert result["context"]["fsx_lustre"]["enabled"] is True

    def test_update_fsx_config_no_cdk_json(self):
        """Test error when cdk.json not found for update."""
        from cli.stacks import update_fsx_config

        with (
            patch("cli.stacks._find_cdk_json", return_value=None),
            pytest.raises(RuntimeError, match="cdk.json not found"),
        ):
            update_fsx_config({"enabled": True})

    def test_get_fsx_config_per_region(self):
        """Test getting FSx config for a specific region."""
        from cli.stacks import get_fsx_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_config = {
                "context": {
                    "fsx_lustre": {"enabled": False, "storage_capacity_gib": 1200},
                    "fsx_lustre_regions": {
                        "us-east-1": {"enabled": True, "storage_capacity_gib": 2400}
                    },
                }
            }
            cdk_path.write_text(json.dumps(cdk_config))

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                # Global config
                global_config = get_fsx_config()
                assert global_config["enabled"] is False
                assert global_config["storage_capacity_gib"] == 1200

                # Region-specific config
                region_config = get_fsx_config("us-east-1")
                assert region_config["enabled"] is True
                assert region_config["storage_capacity_gib"] == 2400
                assert region_config["is_region_specific"] is True

                # Non-configured region falls back to global
                other_config = get_fsx_config("us-west-2")
                assert other_config["enabled"] is False
                assert other_config["is_region_specific"] is False

    def test_update_fsx_config_per_region(self):
        """Test updating FSx config for a specific region."""
        from cli.stacks import update_fsx_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_config = {"context": {"fsx_lustre": {"enabled": False}}}
            cdk_path.write_text(json.dumps(cdk_config))

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                # Enable for specific region
                update_fsx_config({"enabled": True, "storage_capacity_gib": 2400}, "us-east-1")

            with open(cdk_path, encoding="utf-8") as f:
                result = json.load(f)

            # Global should be unchanged
            assert result["context"]["fsx_lustre"]["enabled"] is False
            # Region-specific should be set
            assert result["context"]["fsx_lustre_regions"]["us-east-1"]["enabled"] is True
            assert (
                result["context"]["fsx_lustre_regions"]["us-east-1"]["storage_capacity_gib"] == 2400
            )


class TestFindCdkJson:
    """Tests for _find_cdk_json function."""

    def test_find_cdk_json_in_current_dir(self):
        """Test finding cdk.json in current directory."""
        from cli.stacks import _find_cdk_json

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_path.write_text("{}")

            with patch("pathlib.Path.cwd", return_value=Path(tmpdir)):
                result = _find_cdk_json()
                assert result == cdk_path

    def test_find_cdk_json_in_parent_dir(self):
        """Test finding cdk.json in parent directory."""
        from cli.stacks import _find_cdk_json

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create cdk.json in parent
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_path.write_text("{}")

            # Create subdirectory
            subdir = Path(tmpdir) / "subdir"
            subdir.mkdir()

            with patch("pathlib.Path.cwd", return_value=subdir):
                result = _find_cdk_json()
                assert result == cdk_path

    def test_find_cdk_json_not_found(self):
        """Test when cdk.json is not found."""
        from cli.stacks import _find_cdk_json

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("pathlib.Path.cwd", return_value=Path(tmpdir)),
        ):
            result = _find_cdk_json()
            assert result is None


class TestStackManagerOperations:
    """Tests for StackManager operations."""

    def test_get_python_path_includes_site_packages(self):
        """Test that _get_python_path includes site-packages directories."""
        from cli.stacks import StackManager

        config = MagicMock()
        manager = StackManager(config)

        python_path = manager._get_python_path()

        # Should be a non-empty string with path separators
        assert python_path
        assert isinstance(python_path, str)
        # Should contain at least one path
        paths = python_path.split(os.pathsep)
        assert len(paths) >= 1

    def test_get_python_path_includes_project_root(self):
        """Test that _get_python_path includes the project root for editable installs."""
        from cli.stacks import StackManager

        config = MagicMock()
        manager = StackManager(config)

        python_path = manager._get_python_path()
        paths = python_path.split(os.pathsep)

        # Should include paths (site-packages or project root for editable installs)
        assert any(p for p in paths if p)

    def test_run_cdk_sets_pythonpath(self):
        """Test that _run_cdk sets PYTHONPATH in environment."""
        from cli.stacks import StackManager

        config = MagicMock()
        manager = StackManager(config)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            manager._run_cdk(["list"], capture_output=True)

            # Verify subprocess.run was called with env containing PYTHONPATH
            call_kwargs = mock_run.call_args[1]
            assert "env" in call_kwargs
            assert "PYTHONPATH" in call_kwargs["env"]
            assert call_kwargs["env"]["PYTHONPATH"]  # Should be non-empty

    def test_list_stacks(self):
        """Test listing CDK stacks."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch.object(StackManager, "_run_cdk") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="stack1\nstack2\nstack3\n", stderr=""
            )

            manager = StackManager(config)
            stacks = manager.list_stacks()

            assert stacks == ["stack1", "stack2", "stack3"]

    def test_list_stacks_error(self):
        """Test error handling when listing stacks fails."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch.object(StackManager, "_run_cdk") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="Error listing stacks"
            )

            manager = StackManager(config)

            with pytest.raises(RuntimeError, match="Failed to list stacks"):
                manager.list_stacks()

    def test_synth(self):
        """Test CDK synth."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch.object(StackManager, "_run_cdk") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="Synthesized", stderr="")

            manager = StackManager(config)
            result = manager.synth("test-stack")

            assert "Synthesized" in result

    def test_synth_error(self):
        """Test error handling when synth fails."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch.object(StackManager, "_run_cdk") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Synth failed")

            manager = StackManager(config)

            with pytest.raises(RuntimeError, match="CDK synth failed"):
                manager.synth("test-stack")

    def test_diff(self):
        """Test CDK diff."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch.object(StackManager, "_run_cdk") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="Stack differences", stderr="")

            manager = StackManager(config)
            result = manager.diff("test-stack")

            assert "Stack differences" in result

    def test_deploy_success(self):
        """Test successful deployment."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "_run_cdk") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            manager = StackManager(config)
            result = manager.deploy("test-stack", require_approval=False)

            assert result is True

    def test_deploy_no_runtime(self):
        """Test deployment fails without container runtime."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch("cli.stacks._detect_container_runtime", return_value=None):
            manager = StackManager(config)

            with pytest.raises(RuntimeError, match="No container runtime found"):
                manager.deploy("test-stack")

    def test_deploy_failure(self):
        """Test deployment failure."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_get_stack_status", return_value=None),
            patch.object(StackManager, "_diagnose_deploy_failure"),
        ):
            mock_run.return_value = MagicMock(returncode=1)

            manager = StackManager(config)
            result = manager.deploy("test-stack", require_approval=False)

            assert result is False

    def test_destroy_success(self):
        """Test successful stack destruction."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.api_gateway_region = "us-east-2"

        with (
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=False),
        ):
            mock_run.return_value = MagicMock(returncode=0)

            manager = StackManager(config)
            result = manager.destroy("test-stack", force=True)

            assert result is True

    def test_bootstrap_success(self):
        """Test successful CDK bootstrap."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch.object(StackManager, "_run_cdk") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            manager = StackManager(config)
            result = manager.bootstrap(account="123456789012", region="us-east-1")

            assert result is True


class TestGetStackManager:
    """Tests for get_stack_manager factory function."""

    def test_get_stack_manager(self):
        """Test factory function returns StackManager."""
        from cli.stacks import StackManager, get_stack_manager

        config = MagicMock()
        manager = get_stack_manager(config)

        assert isinstance(manager, StackManager)
        assert manager.config == config


class TestStackDeploymentOrder:
    """Tests for stack deployment ordering functions."""

    def test_get_stack_deployment_order_global_first(self):
        """Test that global stacks are deployed before regional stacks."""
        from cli.stacks import get_stack_deployment_order

        stacks = [
            "gco-us-east-1",
            "gco-global",
            "gco-us-west-2",
            "gco-api-gateway",
            "gco-monitoring",
        ]

        result = get_stack_deployment_order(stacks)

        # Global stacks should come first in order
        assert result[0] == "gco-global"
        assert result[1] == "gco-api-gateway"
        assert result[2] == "gco-monitoring"
        # Regional stacks should come after, sorted alphabetically
        assert result[3] == "gco-us-east-1"
        assert result[4] == "gco-us-west-2"

    def test_get_stack_deployment_order_only_regional(self):
        """Test ordering with only regional stacks."""
        from cli.stacks import get_stack_deployment_order

        stacks = ["gco-us-west-2", "gco-eu-west-1", "gco-us-east-1"]

        result = get_stack_deployment_order(stacks)

        # Should be sorted alphabetically
        assert result == ["gco-eu-west-1", "gco-us-east-1", "gco-us-west-2"]

    def test_get_stack_deployment_order_only_global(self):
        """Test ordering with only global stacks."""
        from cli.stacks import get_stack_deployment_order

        stacks = ["gco-monitoring", "gco-global", "gco-api-gateway"]

        result = get_stack_deployment_order(stacks)

        assert result == ["gco-global", "gco-api-gateway", "gco-monitoring"]

    def test_get_stack_destroy_order_reverse(self):
        """Test that destroy order is reverse of deploy order."""
        from cli.stacks import get_stack_deployment_order, get_stack_destroy_order

        stacks = [
            "gco-us-east-1",
            "gco-global",
            "gco-api-gateway",
        ]

        deploy_order = get_stack_deployment_order(stacks)
        destroy_order = get_stack_destroy_order(stacks)

        assert destroy_order == list(reversed(deploy_order))

    def test_get_stack_destroy_order_regional_first(self):
        """Test that regional stacks are destroyed before global stacks."""
        from cli.stacks import get_stack_destroy_order

        stacks = [
            "gco-us-east-1",
            "gco-global",
            "gco-us-west-2",
            "gco-api-gateway",
            "gco-monitoring",
        ]

        result = get_stack_destroy_order(stacks)

        # Regional stacks should come first (reverse alphabetical)
        assert result[0] == "gco-us-west-2"
        assert result[1] == "gco-us-east-1"
        # Global stacks should come after (reverse priority)
        assert result[2] == "gco-monitoring"
        assert result[3] == "gco-api-gateway"
        assert result[4] == "gco-global"

    def test_deployment_order_non_gco_project(self):
        """#139: ordering is by suffix, so a non-``gco`` project orders
        identically. Regression for the hardcoded ``gco-*`` priority dict."""
        from cli.stacks import get_stack_deployment_order, get_stack_destroy_order

        stacks = [
            "acme-us-east-1",
            "acme-global",
            "acme-us-west-2",
            "acme-api-gateway",
            "acme-monitoring",
        ]

        deploy_order = get_stack_deployment_order(stacks)
        assert deploy_order == [
            "acme-global",
            "acme-api-gateway",
            "acme-monitoring",
            "acme-us-east-1",
            "acme-us-west-2",
        ]
        # Destroy is the exact reverse.
        assert get_stack_destroy_order(stacks) == list(reversed(deploy_order))


class TestStackManagerOrchestrated:
    """Tests for orchestrated deploy/destroy methods."""

    def test_deploy_orchestrated_success(self):
        """Test successful orchestrated deployment."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "list_stacks") as mock_list,
            patch.object(StackManager, "deploy") as mock_deploy,
        ):
            mock_list.return_value = ["gco-global", "gco-us-east-1"]
            mock_deploy.return_value = True

            manager = StackManager(config)
            success, successful, failed = manager.deploy_orchestrated(require_approval=False)

            assert success is True
            assert successful == ["gco-global", "gco-us-east-1"]
            assert failed == []
            assert mock_deploy.call_count == 2

    def test_deploy_orchestrated_stops_on_failure(self):
        """Test that orchestrated deployment stops on first failure."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "list_stacks") as mock_list,
            patch.object(StackManager, "deploy") as mock_deploy,
        ):
            mock_list.return_value = [
                "gco-global",
                "gco-api-gateway",
                "gco-us-east-1",
            ]
            # First succeeds, second fails
            mock_deploy.side_effect = [True, False]

            manager = StackManager(config)
            success, successful, failed = manager.deploy_orchestrated(require_approval=False)

            assert success is False
            assert successful == ["gco-global"]
            assert failed == ["gco-api-gateway"]
            # Should stop after failure, not try third stack
            assert mock_deploy.call_count == 2

    def test_deploy_orchestrated_with_callbacks(self):
        """Test orchestrated deployment with callbacks."""
        from cli.stacks import StackManager

        config = MagicMock()
        started = []
        completed = []

        def on_start(name):
            started.append(name)

        def on_complete(name, success):
            completed.append((name, success))

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "list_stacks") as mock_list,
            patch.object(StackManager, "deploy") as mock_deploy,
        ):
            mock_list.return_value = ["gco-global", "gco-us-east-1"]
            mock_deploy.return_value = True

            manager = StackManager(config)
            manager.deploy_orchestrated(
                require_approval=False,
                on_stack_start=on_start,
                on_stack_complete=on_complete,
            )

            assert started == ["gco-global", "gco-us-east-1"]
            assert completed == [("gco-global", True), ("gco-us-east-1", True)]

    def test_deploy_orchestrated_uses_exclusively_for_regional_and_monitoring(self):
        """
        Phases 2 (regional) and 3 (monitoring) must pass ``exclusively=True``
        to ``deploy()``. Otherwise CDK re-synthesizes and re-evaluates the
        already-deployed global/api-gateway stacks on every phase, re-running
        their custom resources (notably the HelmInstallCharts convergence
        trigger) and adding
        minutes per phase for no actual change.

        Phase 1 (pre-regional globals) must NOT use ``exclusively`` so the
        first deploy from scratch still resolves and deploys dependencies.
        """
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "list_stacks") as mock_list,
            patch.object(StackManager, "deploy") as mock_deploy,
        ):
            # Full stack set: globals, regional, monitoring
            mock_list.return_value = [
                "gco-global",
                "gco-api-gateway",
                "gco-us-east-1",
                "gco-monitoring",
            ]
            mock_deploy.return_value = True

            manager = StackManager(config)
            manager.deploy_orchestrated(require_approval=False)

        # Build a map of stack-name → exclusively kwarg for every deploy() call.
        exclusively_by_stack = {}
        for call in mock_deploy.call_args_list:
            kwargs = call.kwargs
            stack = kwargs.get("stack_name") or (call.args[0] if call.args else None)
            exclusively_by_stack[stack] = kwargs.get("exclusively", False)

        # Phase 1 globals: no --exclusively (they're the top of the dep tree).
        assert exclusively_by_stack.get("gco-global") is False
        assert exclusively_by_stack.get("gco-api-gateway") is False
        # Phase 2 regional: uses --exclusively.
        assert exclusively_by_stack.get("gco-us-east-1") is True
        # Phase 3 monitoring: uses --exclusively.
        assert exclusively_by_stack.get("gco-monitoring") is True

    def test_deploy_passes_exclusively_flag_to_cdk(self):
        """
        When ``exclusively=True``, the cdk deploy command must include
        ``--exclusively``. The StackManager.deploy() method is the boundary
        between phase orchestration and CDK — this test guards the one-line
        translation.
        """
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "ensure_bootstrapped", return_value=True),
            patch.object(StackManager, "_rebuild_lambda_packages"),
            patch.object(StackManager, "_sync_lambda_sources"),
            patch.object(StackManager, "_check_and_fix_stuck_stack"),
            patch.object(StackManager, "_get_stack_status", return_value="CREATE_COMPLETE"),
            patch.object(StackManager, "_run_cdk") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            manager = StackManager(config)

            # With exclusively=True, the flag must appear in the command.
            manager.deploy(stack_name="gco-us-east-1", exclusively=True)
            assert "--exclusively" in mock_run.call_args.args[0]

            # Without it (default), the flag must NOT appear.
            mock_run.reset_mock()
            manager.deploy(stack_name="gco-us-east-1")
            assert "--exclusively" not in mock_run.call_args.args[0]

            # With all_stacks=True, --exclusively is meaningless and must be
            # suppressed even if the caller passes exclusively=True.
            mock_run.reset_mock()
            manager.deploy(all_stacks=True, exclusively=True)
            assert "--exclusively" not in mock_run.call_args.args[0]

    def test_destroy_orchestrated_success(self):
        """Test successful orchestrated destruction."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch.object(StackManager, "list_stacks") as mock_list,
            patch.object(StackManager, "destroy") as mock_destroy,
        ):
            mock_list.return_value = ["gco-global", "gco-us-east-1"]
            mock_destroy.return_value = True

            manager = StackManager(config)
            success, successful, failed = manager.destroy_orchestrated(force=True)

            assert success is True
            # Destroy order is reverse: regional first, then global
            assert successful == ["gco-us-east-1", "gco-global"]
            assert failed == []

    def test_destroy_orchestrated_runs_image_registry_preflight_first(self):
        """The image-registry preflight must fire before any teardown so
        operators don't end up with monitoring + regional + api-gateway
        already destroyed when the global-stack ECR delete fails on a
        non-empty repo. When the preflight refuses (e.g.
        ``removal_policy: "destroy"`` + ``empty_on_delete: false`` with
        repos still present), ``destroy_orchestrated`` returns
        ``(False, [], <every stack>)`` without invoking ``destroy()``."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch.object(StackManager, "list_stacks") as mock_list,
            patch.object(StackManager, "destroy") as mock_destroy,
            patch.object(StackManager, "_image_registry_destroy_preflight") as mock_preflight,
            patch.object(StackManager, "_cleanup_backup_vault") as mock_cleanup_vault,
        ):
            mock_list.return_value = ["gco-global", "gco-us-east-1", "gco-monitoring"]
            mock_preflight.return_value = False  # preflight refuses

            manager = StackManager(config)
            success, successful, failed = manager.destroy_orchestrated(force=True)

            assert success is False
            assert successful == []
            # Every stack reported as failed (none were attempted).
            assert set(failed) == {"gco-global", "gco-us-east-1", "gco-monitoring"}
            # destroy() must NOT have been called — refusal happens
            # before any teardown.
            mock_destroy.assert_not_called()
            # The preflight took a force=True hint so it can match the
            # caller's confirmation posture.
            mock_preflight.assert_called_once_with(force=True)
            # And the backup-vault cleanup must NOT have run either —
            # we're returning before any AWS-side mutation.
            mock_cleanup_vault.assert_not_called()

    def test_destroy_orchestrated_proceeds_when_preflight_passes(self):
        """When the preflight returns True (the default ``retain`` posture
        or an empty registry under ``destroy``), orchestration proceeds
        to the backup-vault cleanup and per-stack destroy phases."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch.object(StackManager, "list_stacks") as mock_list,
            patch.object(StackManager, "destroy") as mock_destroy,
            patch.object(StackManager, "_image_registry_destroy_preflight") as mock_preflight,
            patch.object(StackManager, "_cleanup_backup_vault") as mock_cleanup_vault,
        ):
            mock_list.return_value = ["gco-global", "gco-us-east-1"]
            mock_destroy.return_value = True
            mock_preflight.return_value = True  # preflight passes

            manager = StackManager(config)
            success, successful, failed = manager.destroy_orchestrated(force=True)

            assert success is True
            assert mock_destroy.call_count >= 2
            mock_cleanup_vault.assert_called_once()

    def test_destroy_orchestrated_continues_on_failure(self):
        """Test that orchestrated destruction continues even on failure."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch.object(StackManager, "list_stacks") as mock_list,
            patch.object(StackManager, "destroy") as mock_destroy,
        ):
            mock_list.return_value = [
                "gco-global",
                "gco-api-gateway",
                "gco-us-east-1",
            ]
            # First fails, rest succeed
            mock_destroy.side_effect = [False, True, True]

            manager = StackManager(config)
            success, successful, failed = manager.destroy_orchestrated(force=True)

            assert success is False
            # Should continue trying all stacks
            assert mock_destroy.call_count == 3
            assert len(failed) == 1

    def test_destroy_orchestrated_with_callbacks(self):
        """Test orchestrated destruction with callbacks."""
        from cli.stacks import StackManager

        config = MagicMock()
        started = []
        completed = []

        def on_start(name):
            started.append(name)

        def on_complete(name, success):
            completed.append((name, success))

        with (
            patch.object(StackManager, "list_stacks") as mock_list,
            patch.object(StackManager, "destroy") as mock_destroy,
        ):
            mock_list.return_value = ["gco-global", "gco-us-east-1"]
            mock_destroy.return_value = True

            manager = StackManager(config)
            manager.destroy_orchestrated(
                force=True,
                on_stack_start=on_start,
                on_stack_complete=on_complete,
            )

            # Destroy order is reverse
            assert started == ["gco-us-east-1", "gco-global"]
            assert completed == [("gco-us-east-1", True), ("gco-global", True)]


class TestParallelDeployment:
    """Tests for parallel stack deployment/destruction."""

    def test_deploy_orchestrated_parallel_regional_stacks(self):
        """Test parallel deployment of regional stacks."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "list_stacks") as mock_list,
            patch.object(StackManager, "deploy") as mock_deploy,
            patch("shutil.rmtree"),  # Mock cleanup
        ):
            mock_list.return_value = [
                "gco-global",
                "gco-us-east-1",
                "gco-us-west-2",
                "gco-eu-west-1",
            ]
            mock_deploy.return_value = True

            manager = StackManager(config)
            success, successful, failed = manager.deploy_orchestrated(
                require_approval=False,
                parallel=True,
                max_workers=4,
            )

            assert success is True
            assert len(successful) == 4
            assert failed == []
            # Global stack should be deployed first
            assert "gco-global" in successful
            # All regional stacks should be deployed
            assert "gco-us-east-1" in successful
            assert "gco-us-west-2" in successful
            assert "gco-eu-west-1" in successful

    def test_deploy_orchestrated_parallel_with_failure(self):
        """Test parallel deployment handles failures correctly."""
        from cli.stacks import StackManager

        config = MagicMock()

        def deploy_side_effect(stack_name, **kwargs):
            # Fail one regional stack
            return stack_name != "gco-us-west-2"

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "list_stacks") as mock_list,
            patch.object(StackManager, "deploy") as mock_deploy,
            patch("shutil.rmtree"),
        ):
            mock_list.return_value = [
                "gco-global",
                "gco-us-east-1",
                "gco-us-west-2",
            ]
            mock_deploy.side_effect = deploy_side_effect

            manager = StackManager(config)
            success, successful, failed = manager.deploy_orchestrated(
                require_approval=False,
                parallel=True,
                max_workers=2,
            )

            assert success is False
            assert "gco-global" in successful
            assert "gco-us-east-1" in successful
            assert "gco-us-west-2" in failed

    def test_deploy_orchestrated_parallel_global_failure_stops(self):
        """Test that global stack failure stops parallel deployment."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "list_stacks") as mock_list,
            patch.object(StackManager, "deploy") as mock_deploy,
        ):
            mock_list.return_value = [
                "gco-global",
                "gco-us-east-1",
                "gco-us-west-2",
            ]
            # Global stack fails
            mock_deploy.return_value = False

            manager = StackManager(config)
            success, successful, failed = manager.deploy_orchestrated(
                require_approval=False,
                parallel=True,
            )

            assert success is False
            assert failed == ["gco-global"]
            # Regional stacks should not be attempted
            assert "gco-us-east-1" not in successful
            assert "gco-us-west-2" not in successful

    def test_destroy_orchestrated_parallel_regional_stacks(self):
        """Test parallel destruction of regional stacks."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch.object(StackManager, "list_stacks") as mock_list,
            patch.object(StackManager, "destroy") as mock_destroy,
            patch("shutil.rmtree"),
        ):
            mock_list.return_value = [
                "gco-global",
                "gco-us-east-1",
                "gco-us-west-2",
                "gco-eu-west-1",
            ]
            mock_destroy.return_value = True

            manager = StackManager(config)
            success, successful, failed = manager.destroy_orchestrated(
                force=True,
                parallel=True,
                max_workers=4,
            )

            assert success is True
            assert len(successful) == 4
            assert failed == []

    def test_destroy_orchestrated_parallel_with_failure(self):
        """Test parallel destruction handles failures correctly."""
        from cli.stacks import StackManager

        config = MagicMock()

        def destroy_side_effect(stack_name, **kwargs):
            return stack_name != "gco-us-west-2"

        with (
            patch.object(StackManager, "list_stacks") as mock_list,
            patch.object(StackManager, "destroy") as mock_destroy,
            patch("shutil.rmtree"),
        ):
            mock_list.return_value = [
                "gco-global",
                "gco-us-east-1",
                "gco-us-west-2",
            ]
            mock_destroy.side_effect = destroy_side_effect

            manager = StackManager(config)
            success, successful, failed = manager.destroy_orchestrated(
                force=True,
                parallel=True,
                max_workers=2,
            )

            assert success is False
            assert "gco-us-west-2" in failed
            # Global stack should still be destroyed
            assert "gco-global" in successful

    def test_deploy_uses_separate_output_dirs_for_parallel(self):
        """Test that parallel deployment uses separate CDK output directories."""
        from cli.stacks import StackManager

        config = MagicMock()
        output_dirs_used = []

        def deploy_capture_output_dir(stack_name, output_dir=None, **kwargs):
            output_dirs_used.append((stack_name, output_dir))
            return True

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "list_stacks") as mock_list,
            patch.object(StackManager, "deploy") as mock_deploy,
            patch("shutil.rmtree"),
        ):
            mock_list.return_value = [
                "gco-global",
                "gco-us-east-1",
                "gco-us-west-2",
            ]
            mock_deploy.side_effect = deploy_capture_output_dir

            manager = StackManager(config)
            manager.deploy_orchestrated(
                require_approval=False,
                parallel=True,
                max_workers=2,
            )

            # Global stack should not use custom output dir (sequential)
            global_calls = [c for c in output_dirs_used if c[0] == "gco-global"]
            assert len(global_calls) == 1
            assert global_calls[0][1] is None

            # Regional stacks should use custom output dirs (parallel)
            regional_calls = [c for c in output_dirs_used if c[0] != "gco-global"]
            for stack_name, output_dir in regional_calls:
                assert output_dir is not None
                assert stack_name in output_dir

    def test_deploy_orchestrated_single_regional_not_parallel(self):
        """Test that single regional stack doesn't use parallel mode."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "list_stacks") as mock_list,
            patch.object(StackManager, "deploy") as mock_deploy,
        ):
            mock_list.return_value = ["gco-global", "gco-us-east-1"]
            mock_deploy.return_value = True

            manager = StackManager(config)
            success, successful, failed = manager.deploy_orchestrated(
                require_approval=False,
                parallel=True,  # Even with parallel=True
            )

            assert success is True
            # With only 1 regional stack, should deploy sequentially
            # (no custom output_dir should be used)
            for call in mock_deploy.call_args_list:
                # Check that output_dir was not passed or is None
                kwargs = call[1] if len(call) > 1 else {}
                assert kwargs.get("output_dir") is None


# =============================================================================
# Additional coverage tests for cli/stacks.py
# =============================================================================


class TestStackManagerContainerRuntimeExtended:
    """Extended tests for container runtime detection."""

    @pytest.fixture(autouse=True)
    def reset_runtime_cache(self):
        """Reset the container runtime cache before each test."""
        import cli.stacks as stacks_module

        stacks_module._container_runtime_checked = False
        stacks_module._container_runtime_cache = None
        yield
        stacks_module._container_runtime_checked = False
        stacks_module._container_runtime_cache = None

    def test_detect_container_runtime_docker(self):
        """Test container runtime detection finds docker."""
        from cli.stacks import _detect_container_runtime

        with (
            patch("cli.stacks.os.environ.get", return_value=None),
            patch("cli.stacks.shutil.which", return_value="/usr/bin/docker"),
            patch("cli.stacks.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runtime = _detect_container_runtime()
            assert runtime == "docker"

    def test_detect_container_runtime_finch(self):
        """Test container runtime detection falls back to finch."""
        from cli.stacks import _detect_container_runtime

        def which_side_effect(cmd):
            if cmd == "docker":
                return None
            elif cmd == "finch":
                return "/usr/local/bin/finch"
            return None

        with (
            patch("cli.stacks.os.environ.get", return_value=None),
            patch("cli.stacks.shutil.which", side_effect=which_side_effect),
            patch("cli.stacks.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runtime = _detect_container_runtime()
            assert runtime == "finch"

    def test_detect_container_runtime_from_env(self, monkeypatch):
        """Test container runtime uses CDK_DOCKER env var."""
        from cli.stacks import _detect_container_runtime

        monkeypatch.setenv("CDK_DOCKER", "podman")
        runtime = _detect_container_runtime()
        assert runtime == "podman"


class TestStackManagerDeploymentExtended:
    """Extended tests for stack deployment functionality."""

    def test_deploy_no_container_runtime(self, tmp_path):
        """Test deploy raises error when no container runtime found."""
        from cli.config import GCOConfig
        from cli.stacks import StackManager

        (tmp_path / "cdk.json").write_text('{"app": "python app.py"}')

        with (
            patch("cli.stacks._detect_container_runtime", return_value=None),
            patch("cli.stacks.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="/usr/bin/cdk")

            config = GCOConfig()
            manager = StackManager(config=config, project_root=tmp_path)

            with pytest.raises(RuntimeError, match="No container runtime"):
                manager.deploy("test-stack")


class TestStackManagerFsxConfigExtended:
    """Extended tests for FSx configuration."""

    def test_get_fsx_config_region_specific(self, tmp_path, monkeypatch):
        """Test get_fsx_config with region-specific override."""
        from cli.stacks import get_fsx_config

        cdk_json = tmp_path / "cdk.json"
        cdk_json.write_text(
            json.dumps(
                {
                    "context": {
                        "fsx_lustre": {"enabled": False, "storage_capacity_gib": 1200},
                        "fsx_lustre_regions": {
                            "us-east-1": {"enabled": True, "storage_capacity_gib": 2400}
                        },
                    }
                }
            )
        )

        monkeypatch.chdir(tmp_path)
        config = get_fsx_config("us-east-1")

        assert config["enabled"] is True
        assert config["storage_capacity_gib"] == 2400
        assert config["is_region_specific"] is True

    def test_update_fsx_config_region_specific(self, tmp_path, monkeypatch):
        """Test update_fsx_config for region-specific settings."""
        from cli.stacks import update_fsx_config

        cdk_json = tmp_path / "cdk.json"
        cdk_json.write_text(json.dumps({"context": {}}))

        monkeypatch.chdir(tmp_path)
        update_fsx_config({"enabled": True, "storage_capacity_gib": 3600}, "us-west-2")

        with open(cdk_json, encoding="utf-8") as f:
            config = json.load(f)

        assert config["context"]["fsx_lustre_regions"]["us-west-2"]["enabled"] is True
        assert config["context"]["fsx_lustre_regions"]["us-west-2"]["storage_capacity_gib"] == 3600


class TestStackDeploymentOrderExtended:
    """Extended tests for stack deployment ordering."""

    def test_get_stack_deployment_order_mixed(self):
        """Test deployment order with mixed stack types."""
        from cli.stacks import get_stack_deployment_order

        stacks = [
            "gco-us-west-2",
            "gco-monitoring",
            "gco-global",
            "gco-us-east-1",
            "gco-api-gateway",
        ]

        ordered = get_stack_deployment_order(stacks)

        assert ordered[0] == "gco-global"
        assert ordered[1] == "gco-api-gateway"
        assert ordered[2] == "gco-monitoring"
        assert "gco-us-east-1" in ordered
        assert "gco-us-west-2" in ordered


# =============================================================================
# Additional coverage tests for uncovered lines in cli/stacks.py
# =============================================================================


class TestStackManagerSyncLambdaSources:
    """Tests for _sync_lambda_sources method."""

    def test_sync_lambda_sources_success(self, tmp_path):
        """Test successful lambda source sync."""
        from cli.stacks import StackManager

        # Create source directory structure
        source_dir = tmp_path / "lambda" / "kubectl-applier-simple"
        source_dir.mkdir(parents=True)
        (source_dir / "handler.py").write_text("# handler code")
        manifests_dir = source_dir / "manifests"
        manifests_dir.mkdir()
        (manifests_dir / "01-test.yaml").write_text("kind: Test")

        # Create build directory structure
        build_dir = tmp_path / "lambda" / "kubectl-applier-simple-build"
        build_dir.mkdir(parents=True)
        (build_dir / "handler.py").write_text("# old handler")
        build_manifests_dir = build_dir / "manifests"
        build_manifests_dir.mkdir()

        config = MagicMock()
        manager = StackManager(config, project_root=tmp_path)
        manager._sync_lambda_sources()

        # Verify files were synced
        assert (build_dir / "handler.py").read_text() == "# handler code"
        assert (build_manifests_dir / "01-test.yaml").read_text() == "kind: Test"

    def test_sync_lambda_sources_no_source_dir(self, tmp_path):
        """Test sync when source directory doesn't exist."""
        from cli.stacks import StackManager

        config = MagicMock()
        manager = StackManager(config, project_root=tmp_path)
        # Should not raise error
        manager._sync_lambda_sources()

    def test_sync_lambda_sources_no_build_dir(self, tmp_path):
        """Test sync when build directory doesn't exist — triggers auto-build."""
        from cli.stacks import StackManager

        # Create source directory with handler and requirements
        source_dir = tmp_path / "lambda" / "kubectl-applier-simple"
        source_dir.mkdir(parents=True)
        (source_dir / "handler.py").write_text("# handler code")
        (source_dir / "requirements.txt").write_text("pyyaml\n")
        manifests_dir = source_dir / "manifests"
        manifests_dir.mkdir()
        (manifests_dir / "test.yaml").write_text("kind: Test")

        config = MagicMock()
        manager = StackManager(config, project_root=tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            manager._sync_lambda_sources()

        # Build dir should now exist with handler and manifests
        build_dir = tmp_path / "lambda" / "kubectl-applier-simple-build"
        assert build_dir.exists()
        assert (build_dir / "handler.py").exists()
        assert (build_dir / "manifests" / "test.yaml").exists()


class TestCheckAndFixStuckStack:
    """Tests for _check_and_fix_stuck_stack auto-recovery."""

    def test_deletes_review_in_progress_stack(self):
        from cli.stacks import StackManager

        config = MagicMock()
        manager = StackManager.__new__(StackManager)
        manager.config = config
        manager.project_root = Path(".")

        mock_cfn = MagicMock()
        mock_cfn.describe_stacks.return_value = {"Stacks": [{"StackStatus": "REVIEW_IN_PROGRESS"}]}
        mock_waiter = MagicMock()
        mock_cfn.get_waiter.return_value = mock_waiter

        with (
            patch.object(manager, "_get_deploy_region", return_value="us-east-1"),
            patch("boto3.client", return_value=mock_cfn),
        ):
            manager._check_and_fix_stuck_stack("gco-monitoring")

        mock_cfn.delete_stack.assert_called_once_with(StackName="gco-monitoring")
        mock_waiter.wait.assert_called_once()

    def test_deletes_rollback_complete_stack(self):
        from cli.stacks import StackManager

        config = MagicMock()
        manager = StackManager.__new__(StackManager)
        manager.config = config
        manager.project_root = Path(".")

        mock_cfn = MagicMock()
        mock_cfn.describe_stacks.return_value = {"Stacks": [{"StackStatus": "ROLLBACK_COMPLETE"}]}
        mock_waiter = MagicMock()
        mock_cfn.get_waiter.return_value = mock_waiter

        with (
            patch.object(manager, "_get_deploy_region", return_value="us-east-1"),
            patch("boto3.client", return_value=mock_cfn),
        ):
            manager._check_and_fix_stuck_stack("gco-global")

        mock_cfn.delete_stack.assert_called_once()

    def test_skips_healthy_stack(self):
        from cli.stacks import StackManager

        config = MagicMock()
        manager = StackManager.__new__(StackManager)
        manager.config = config
        manager.project_root = Path(".")

        mock_cfn = MagicMock()
        mock_cfn.describe_stacks.return_value = {"Stacks": [{"StackStatus": "CREATE_COMPLETE"}]}

        with (
            patch.object(manager, "_get_deploy_region", return_value="us-east-1"),
            patch("boto3.client", return_value=mock_cfn),
        ):
            manager._check_and_fix_stuck_stack("gco-us-east-1")

        mock_cfn.delete_stack.assert_not_called()

    def test_handles_nonexistent_stack(self):
        from cli.stacks import StackManager

        config = MagicMock()
        manager = StackManager.__new__(StackManager)
        manager.config = config
        manager.project_root = Path(".")

        mock_cfn = MagicMock()
        mock_cfn.describe_stacks.side_effect = Exception("Stack does not exist")

        with (
            patch.object(manager, "_get_deploy_region", return_value="us-east-1"),
            patch("boto3.client", return_value=mock_cfn),
        ):
            # Should not raise
            manager._check_and_fix_stuck_stack("gco-new")

    def test_skips_when_no_region(self):
        from cli.stacks import StackManager

        config = MagicMock()
        manager = StackManager.__new__(StackManager)
        manager.config = config
        manager.project_root = Path(".")

        with patch.object(manager, "_get_deploy_region", return_value=None):
            manager._check_and_fix_stuck_stack("unknown-stack")


class TestDiagnoseDeployFailure:
    """Tests for _diagnose_deploy_failure diagnostics."""

    def test_prints_failed_events(self, capsys):
        from cli.stacks import StackManager

        config = MagicMock()
        manager = StackManager.__new__(StackManager)
        manager.config = config
        manager.project_root = Path(".")

        mock_cfn = MagicMock()
        mock_cfn.describe_stack_events.return_value = {
            "StackEvents": [
                {
                    "LogicalResourceId": "MyResource",
                    "ResourceStatus": "CREATE_FAILED",
                    "ResourceStatusReason": "Resource already exists",
                },
                {
                    "LogicalResourceId": "gco-monitoring",
                    "ResourceStatus": "ROLLBACK_IN_PROGRESS",
                    "ResourceStatusReason": "The following resource(s) failed",
                },
            ]
        }
        mock_cfn.describe_stacks.return_value = {"Stacks": [{"StackStatus": "ROLLBACK_COMPLETE"}]}

        with (
            patch.object(manager, "_get_deploy_region", return_value="us-east-2"),
            patch("boto3.client", return_value=mock_cfn),
        ):
            manager._diagnose_deploy_failure("gco-monitoring")

        output = capsys.readouterr().out
        assert "MyResource" in output
        assert "CREATE_FAILED" in output
        assert "Resource already exists" in output
        assert "Suggested fix" in output

    def test_prints_review_in_progress_advice(self, capsys):
        from cli.stacks import StackManager

        config = MagicMock()
        manager = StackManager.__new__(StackManager)
        manager.config = config
        manager.project_root = Path(".")

        mock_cfn = MagicMock()
        mock_cfn.describe_stack_events.return_value = {"StackEvents": []}
        mock_cfn.describe_stacks.return_value = {"Stacks": [{"StackStatus": "REVIEW_IN_PROGRESS"}]}

        with (
            patch.object(manager, "_get_deploy_region", return_value="us-east-2"),
            patch("boto3.client", return_value=mock_cfn),
        ):
            manager._diagnose_deploy_failure("gco-monitoring")

        output = capsys.readouterr().out
        assert "delete-stack" in output
        assert "gco-monitoring" in output

    def test_handles_api_error_gracefully(self):
        from cli.stacks import StackManager

        config = MagicMock()
        manager = StackManager.__new__(StackManager)
        manager.config = config
        manager.project_root = Path(".")

        with (
            patch.object(manager, "_get_deploy_region", return_value="us-east-2"),
            patch("boto3.client", side_effect=Exception("API error")),
        ):
            # Should not raise
            manager._diagnose_deploy_failure("gco-monitoring")

    def test_skips_when_no_region(self):
        from cli.stacks import StackManager

        config = MagicMock()
        manager = StackManager.__new__(StackManager)
        manager.config = config
        manager.project_root = Path(".")

        with patch.object(manager, "_get_deploy_region", return_value=None):
            manager._diagnose_deploy_failure("unknown-stack")


class TestSafeRmtree:
    """Tests for _safe_rmtree path validation."""

    def test_refuses_path_without_lambda(self, tmp_path):
        """_safe_rmtree refuses paths that don't contain 'lambda'."""
        from cli.stacks import _safe_rmtree

        bad_dir = tmp_path / "some-dir-build"
        bad_dir.mkdir()
        with pytest.raises(ValueError, match="Refusing to remove"):
            _safe_rmtree(bad_dir)

    def test_refuses_path_not_ending_with_build(self, tmp_path):
        """_safe_rmtree refuses paths that don't end with '-build'."""
        from cli.stacks import _safe_rmtree

        bad_dir = tmp_path / "lambda" / "kubectl-applier-simple"
        bad_dir.mkdir(parents=True)
        with pytest.raises(ValueError, match="Refusing to remove"):
            _safe_rmtree(bad_dir)

    def test_removes_valid_build_dir(self, tmp_path):
        """_safe_rmtree removes a valid Lambda build directory."""
        from cli.stacks import _safe_rmtree

        build_dir = tmp_path / "lambda" / "kubectl-applier-simple-build"
        build_dir.mkdir(parents=True)
        (build_dir / "handler.py").write_text("# test")
        assert build_dir.exists()
        _safe_rmtree(build_dir)
        assert not build_dir.exists()


class TestBuildLambdaPackages:
    """Tests for _build_lambda_packages."""

    def test_builds_package_with_handler_and_manifests(self, tmp_path):
        from cli.stacks import StackManager

        source_dir = tmp_path / "lambda" / "kubectl-applier-simple"
        source_dir.mkdir(parents=True)
        (source_dir / "handler.py").write_text("# handler")
        (source_dir / "requirements.txt").write_text("pyyaml\n")
        manifests = source_dir / "manifests"
        manifests.mkdir()
        (manifests / "01-test.yaml").write_text("kind: Test")

        config = MagicMock()
        manager = StackManager.__new__(StackManager)
        manager.config = config
        manager.project_root = tmp_path

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            manager._build_lambda_packages()

        build_dir = tmp_path / "lambda" / "kubectl-applier-simple-build"
        assert build_dir.exists()
        assert (build_dir / "handler.py").read_text() == "# handler"
        assert (build_dir / "manifests" / "01-test.yaml").exists()
        mock_run.assert_called_once()

    def test_skips_when_source_missing(self, tmp_path):
        from cli.stacks import StackManager

        config = MagicMock()
        manager = StackManager.__new__(StackManager)
        manager.config = config
        manager.project_root = tmp_path

        # Should not raise when source dir doesn't exist
        manager._build_lambda_packages()

    def test_skips_when_requirements_missing(self, tmp_path):
        from cli.stacks import StackManager

        source_dir = tmp_path / "lambda" / "kubectl-applier-simple"
        source_dir.mkdir(parents=True)
        (source_dir / "handler.py").write_text("# handler")
        # No requirements.txt

        config = MagicMock()
        manager = StackManager.__new__(StackManager)
        manager.config = config
        manager.project_root = tmp_path

        manager._build_lambda_packages()

    def test_cleans_stale_build_dir_before_rebuilding(self, tmp_path):
        """Test that a stale build directory with broken symlinks is cleaned before rebuild."""
        from cli.stacks import StackManager

        source_dir = tmp_path / "lambda" / "kubectl-applier-simple"
        source_dir.mkdir(parents=True)
        (source_dir / "handler.py").write_text("# handler")
        (source_dir / "requirements.txt").write_text("pyyaml\n")
        manifests = source_dir / "manifests"
        manifests.mkdir()
        (manifests / "01-test.yaml").write_text("kind: Test")

        # Create a stale build dir with a broken symlink (simulates the botocore issue)
        build_dir = tmp_path / "lambda" / "kubectl-applier-simple-build"
        build_dir.mkdir(parents=True)
        stale_dir = build_dir / "botocore" / "data"
        stale_dir.mkdir(parents=True)
        broken_symlink = stale_dir / "dataexchange"
        broken_symlink.symlink_to("/nonexistent/path")
        assert broken_symlink.is_symlink()
        assert not broken_symlink.exists()  # broken symlink

        config = MagicMock()
        manager = StackManager.__new__(StackManager)
        manager.config = config
        manager.project_root = tmp_path

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            manager._build_lambda_packages()

        # Stale content should be gone, fresh build in place
        assert build_dir.exists()
        assert (build_dir / "handler.py").read_text() == "# handler"
        assert not (build_dir / "botocore").exists()  # stale dir cleaned


class TestBuildHelmInstallerPackage:
    """Tests for _build_helm_installer_lambda."""

    def test_builds_helm_installer_with_all_files(self, tmp_path):
        from cli.stacks import StackManager

        source_dir = tmp_path / "lambda" / "helm-installer"
        source_dir.mkdir(parents=True)
        (source_dir / "handler.py").write_text("# helm handler")
        (source_dir / "charts.yaml").write_text("charts: {}")
        (source_dir / "requirements.txt").write_text("pyyaml\n")
        (source_dir / "Dockerfile").write_text("FROM python:3.14")

        config = MagicMock()
        manager = StackManager.__new__(StackManager)
        manager.config = config
        manager.project_root = tmp_path

        manager._build_helm_installer_lambda()

        build_dir = tmp_path / "lambda" / "helm-installer-build"
        assert build_dir.exists()
        assert (build_dir / "handler.py").read_text() == "# helm handler"
        assert (build_dir / "charts.yaml").read_text() == "charts: {}"
        assert (build_dir / "requirements.txt").read_text() == "pyyaml\n"
        assert (build_dir / "Dockerfile").read_text() == "FROM python:3.14"

    def test_skips_when_source_missing(self, tmp_path):
        from cli.stacks import StackManager

        config = MagicMock()
        manager = StackManager.__new__(StackManager)
        manager.config = config
        manager.project_root = tmp_path

        # Should not raise when source dir doesn't exist
        manager._build_helm_installer_lambda()

        build_dir = tmp_path / "lambda" / "helm-installer-build"
        assert not build_dir.exists()

    def test_cleans_stale_build_dir_before_rebuilding(self, tmp_path):
        """Test that a stale build directory is cleaned before rebuild."""
        from cli.stacks import StackManager

        source_dir = tmp_path / "lambda" / "helm-installer"
        source_dir.mkdir(parents=True)
        (source_dir / "handler.py").write_text("# new handler")
        (source_dir / "charts.yaml").write_text("charts: {keda: {}}")
        (source_dir / "Dockerfile").write_text("FROM python:3.14")

        # Create a stale build dir with old content
        build_dir = tmp_path / "lambda" / "helm-installer-build"
        build_dir.mkdir(parents=True)
        (build_dir / "handler.py").write_text("# old handler")
        (build_dir / "charts.yaml").write_text("charts: {}")
        (build_dir / "stale_file.txt").write_text("should be removed")

        config = MagicMock()
        manager = StackManager.__new__(StackManager)
        manager.config = config
        manager.project_root = tmp_path

        manager._build_helm_installer_lambda()

        # Stale content should be gone, fresh build in place
        assert build_dir.exists()
        assert (build_dir / "handler.py").read_text() == "# new handler"
        assert (build_dir / "charts.yaml").read_text() == "charts: {keda: {}}"
        assert not (build_dir / "stale_file.txt").exists()

    def test_skips_pycache_directory(self, tmp_path):
        """Test that __pycache__ is not copied to the build directory."""
        from cli.stacks import StackManager

        source_dir = tmp_path / "lambda" / "helm-installer"
        source_dir.mkdir(parents=True)
        (source_dir / "handler.py").write_text("# handler")
        (source_dir / "charts.yaml").write_text("charts: {}")
        (source_dir / "Dockerfile").write_text("FROM python:3.14")
        pycache = source_dir / "__pycache__"
        pycache.mkdir()
        (pycache / "handler.cpython-314.pyc").write_bytes(b"\x00")

        config = MagicMock()
        manager = StackManager.__new__(StackManager)
        manager.config = config
        manager.project_root = tmp_path

        manager._build_helm_installer_lambda()

        build_dir = tmp_path / "lambda" / "helm-installer-build"
        assert build_dir.exists()
        assert not (build_dir / "__pycache__").exists()

    def test_charts_yaml_changes_reflected_in_build(self, tmp_path):
        """Test that updating charts.yaml and rebuilding produces fresh content."""
        from cli.stacks import StackManager

        source_dir = tmp_path / "lambda" / "helm-installer"
        source_dir.mkdir(parents=True)
        (source_dir / "handler.py").write_text("# handler")
        (source_dir / "Dockerfile").write_text("FROM python:3.14")

        config = MagicMock()
        manager = StackManager.__new__(StackManager)
        manager.config = config
        manager.project_root = tmp_path

        # First build with original charts
        (source_dir / "charts.yaml").write_text("charts: {keda: {enabled: true}}")
        manager._build_helm_installer_lambda()
        build_dir = tmp_path / "lambda" / "helm-installer-build"
        assert "keda" in (build_dir / "charts.yaml").read_text()

        # Update charts.yaml and rebuild
        (source_dir / "charts.yaml").write_text(
            "charts: {keda: {enabled: true}, yunikorn: {enabled: true}}"
        )
        manager._build_helm_installer_lambda()
        assert "yunikorn" in (build_dir / "charts.yaml").read_text()

    def test_build_lambda_packages_calls_both_builders(self, tmp_path):
        """Test that _build_lambda_packages calls both kubectl and helm builders."""
        from cli.stacks import StackManager

        config = MagicMock()
        manager = StackManager.__new__(StackManager)
        manager.config = config
        manager.project_root = tmp_path

        with (
            patch.object(manager, "_build_kubectl_lambda") as mock_kubectl,
            patch.object(manager, "_build_helm_installer_lambda") as mock_helm,
        ):
            manager._build_lambda_packages()
            mock_kubectl.assert_called_once()
            mock_helm.assert_called_once()


class TestStackManagerDeployWithOptions:
    """Tests for deploy method with various options."""

    def test_deploy_with_all_options(self):
        """Test deploy with all options specified."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_sync_lambda_sources"),
        ):
            mock_run.return_value = MagicMock(returncode=0)

            manager = StackManager(config)
            result = manager.deploy(
                stack_name="test-stack",
                require_approval=False,
                outputs_file="outputs.json",
                parameters={"Param1": "Value1"},
                tags={"Env": "test"},
                progress="bar",
                output_dir="/tmp/cdk-out",  # nosec B108 - test fixture using temp directory
            )

            assert result is True
            # Verify command includes all options
            call_args = mock_run.call_args[0][0]
            assert "--outputs-file" in call_args
            assert "--parameters" in call_args
            assert "--tags" in call_args
            assert "--output" in call_args

    def test_deploy_all_stacks(self):
        """Test deploy with all_stacks=True."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_sync_lambda_sources"),
        ):
            mock_run.return_value = MagicMock(returncode=0)

            manager = StackManager(config)
            result = manager.deploy(all_stacks=True, require_approval=False)

            assert result is True
            call_args = mock_run.call_args[0][0]
            assert "--all" in call_args


class TestStackManagerDestroyWithOptions:
    """Tests for destroy method with various options."""

    def test_destroy_with_output_dir(self):
        """Test destroy with custom output directory."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.api_gateway_region = "us-east-2"

        with (
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=False),
        ):
            mock_run.return_value = MagicMock(returncode=0)

            manager = StackManager(config)
            result = manager.destroy(
                stack_name="test-stack",
                force=True,
                output_dir="/tmp/cdk-out",  # nosec B108 - test fixture using temp directory
            )

            assert result is True
            call_args = mock_run.call_args[0][0]
            assert "--output" in call_args

    def test_destroy_all_stacks(self):
        """Test destroy with all_stacks=True."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch.object(StackManager, "_run_cdk") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            manager = StackManager(config)
            result = manager.destroy(all_stacks=True, force=True)

            assert result is True
            call_args = mock_run.call_args[0][0]
            assert "--all" in call_args


class TestStackManagerDestroyAnalyticsFallback:
    """Tests for the analytics destroy fallback when the toggle is disabled."""

    def test_destroy_enables_toggle_when_stack_exists(self):
        """When analytics toggle is off but stack exists, destroy enables it first."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.api_gateway_region = "us-east-2"

        with (
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(
                StackManager, "_stack_exists_in_cloudformation", return_value=True
            ) as mock_exists,
            patch.object(
                StackManager, "_ensure_analytics_enabled_for_destroy", return_value=True
            ) as mock_enable,
            patch.object(StackManager, "_restore_analytics_disabled") as mock_restore,
            # Patch the dependency-removal helper so we only exercise
            # the toggle / CDK / fallback logic here.
            patch.object(
                StackManager, "_remove_api_gateway_analytics_dependency", return_value=True
            ),
        ):
            # CDK destroy succeeds (because we enabled the toggle)
            mock_run.return_value = MagicMock(returncode=0)
            # After CDK destroy, stack no longer exists
            mock_exists.side_effect = [True, False]

            manager = StackManager(config)
            result = manager.destroy(stack_name="gco-analytics", force=True)

            assert result is True
            mock_enable.assert_called_once()
            mock_restore.assert_called_once()

    def test_destroy_falls_back_to_cloudformation_when_cdk_doesnt_delete(self):
        """If CDK destroy succeeds but stack still exists, use CloudFormation."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.api_gateway_region = "us-east-2"

        with (
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_stack_exists_in_cloudformation") as mock_exists,
            patch.object(StackManager, "_ensure_analytics_enabled_for_destroy", return_value=True),
            patch.object(StackManager, "_restore_analytics_disabled"),
            patch.object(
                StackManager, "_cloudformation_delete_stack", return_value=True
            ) as mock_cfn_delete,
            patch.object(
                StackManager, "_remove_api_gateway_analytics_dependency", return_value=True
            ),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            # Stack still exists after CDK destroy
            mock_exists.return_value = True

            manager = StackManager(config)
            result = manager.destroy(stack_name="gco-analytics", force=True)

            assert result is True
            mock_cfn_delete.assert_called_once_with("gco-analytics")

    def test_destroy_skips_toggle_for_non_analytics_stacks(self):
        """Non-analytics stacks don't trigger the toggle logic."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=False),
            patch.object(StackManager, "_ensure_analytics_enabled_for_destroy") as mock_enable,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            manager = StackManager(config)
            result = manager.destroy(stack_name="gco-us-east-1", force=True)

            assert result is True
            mock_enable.assert_not_called()


class TestStackManagerDestroyExclusively:
    """Tests for --exclusively flag and API gateway dependency removal."""

    def test_destroy_passes_exclusively_flag(self):
        """Destroy of a specific stack passes --exclusively to CDK."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.api_gateway_region = "us-east-2"

        with (
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=False),
            patch.object(
                StackManager, "_remove_api_gateway_analytics_dependency", return_value=True
            ),
        ):
            mock_run.return_value = MagicMock(returncode=0)

            manager = StackManager(config)
            result = manager.destroy(stack_name="gco-analytics", force=True)

            assert result is True
            call_args = mock_run.call_args[0][0]
            assert "--exclusively" in call_args
            assert "gco-analytics" in call_args

    def test_destroy_all_does_not_pass_exclusively(self):
        """Destroy --all does not pass --exclusively."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch.object(StackManager, "_run_cdk") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            manager = StackManager(config)
            result = manager.destroy(all_stacks=True, force=True)

            assert result is True
            call_args = mock_run.call_args[0][0]
            assert "--exclusively" not in call_args
            assert "--all" in call_args

    def test_destroy_analytics_calls_remove_api_gateway_dependency(self):
        """Destroying analytics stack removes API gateway dependency first."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.api_gateway_region = "us-east-2"

        with (
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=False),
            patch.object(
                StackManager, "_remove_api_gateway_analytics_dependency", return_value=True
            ) as mock_remove,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            manager = StackManager(config)
            manager.destroy(stack_name="gco-analytics", force=True)

            mock_remove.assert_called_once()

    def test_destroy_non_analytics_skips_api_gateway_dependency(self):
        """Non-analytics stacks don't trigger API gateway dependency removal."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=False),
            patch.object(
                StackManager, "_remove_api_gateway_analytics_dependency", return_value=True
            ) as mock_remove,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            manager = StackManager(config)
            manager.destroy(stack_name="gco-us-east-1", force=True)

            mock_remove.assert_not_called()

    def test_destroy_analytics_aborts_when_api_gateway_still_imports(self):
        """If the api-gateway still imports analytics exports after the
        redeploy attempt, the destroy must abort instead of cascading
        into a guaranteed-fail CloudFormation delete."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.api_gateway_region = "us-east-2"

        with (
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=False),
            patch.object(
                StackManager, "_remove_api_gateway_analytics_dependency", return_value=False
            ) as mock_remove,
        ):
            manager = StackManager(config)
            result = manager.destroy(stack_name="gco-analytics", force=True)

            # Short-circuits before invoking cdk destroy.
            assert result is False
            mock_remove.assert_called_once()
            mock_run.assert_not_called()


class TestRemoveApiGatewayAnalyticsDependency:
    """Tests for the redeploy/skip logic in _remove_api_gateway_analytics_dependency."""

    def test_skips_redeploy_when_api_gateway_does_not_exist(self):
        """If gco-api-gateway isn't in CloudFormation, no consumer can
        exist, so the expensive redeploy is skipped and the destroy is
        marked safe."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.api_gateway_region = "us-east-2"

        with (
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=False),
            patch.object(StackManager, "deploy") as mock_deploy,
        ):
            manager = StackManager(config)
            result = manager._remove_api_gateway_analytics_dependency()

            assert result is True
            mock_deploy.assert_not_called()

    def test_skips_redeploy_when_no_analytics_imports(self):
        """If gco-api-gateway exists but doesn't import from gco-analytics
        (e.g. analytics was never fully wired up), skip the redeploy."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.api_gateway_region = "us-east-2"

        with (
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=True),
            patch.object(StackManager, "_api_gateway_imports_from_analytics", return_value=False),
            patch.object(StackManager, "deploy") as mock_deploy,
        ):
            manager = StackManager(config)
            result = manager._remove_api_gateway_analytics_dependency()

            assert result is True
            mock_deploy.assert_not_called()

    def test_redeploy_failure_is_fatal_when_imports_remain(self):
        """If the redeploy fails and api-gateway still imports analytics
        exports, the destroy must be blocked."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.api_gateway_region = "us-east-2"

        # The second call (post-redeploy recheck) also reports imports.
        with (
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=True),
            patch.object(StackManager, "_api_gateway_imports_from_analytics", return_value=True),
            patch.object(StackManager, "deploy", return_value=False),
            patch("cli.stacks.get_analytics_config", return_value={"enabled": True}),
            patch("cli.stacks.update_analytics_config"),
        ):
            manager = StackManager(config)
            result = manager._remove_api_gateway_analytics_dependency()

            assert result is False

    def test_redeploy_failure_is_tolerated_when_consumer_vanished(self):
        """If the redeploy failed because api-gateway was auto-deleted
        by the ROLLBACK_COMPLETE cleanup, the post-redeploy recheck
        finds no imports and the destroy is allowed to proceed."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.api_gateway_region = "us-east-2"

        import_results = iter([True, False])

        with (
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=True),
            patch.object(
                StackManager,
                "_api_gateway_imports_from_analytics",
                side_effect=lambda: next(import_results),
            ),
            patch.object(StackManager, "deploy", return_value=False),
            patch("cli.stacks.get_analytics_config", return_value={"enabled": True}),
            patch("cli.stacks.update_analytics_config"),
        ):
            manager = StackManager(config)
            result = manager._remove_api_gateway_analytics_dependency()

            assert result is True


class TestApiGatewayImportsFromAnalytics:
    """Tests for the CloudFormation-based export/import detection helper."""

    def test_returns_false_when_no_analytics_exports(self):
        """If no exports are owned by gco-analytics, no imports can exist."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"
        config.api_gateway_region = "us-east-2"

        mock_cfn = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {
                "Exports": [
                    {
                        "Name": "other-export",
                        "ExportingStackId": (
                            "arn:aws:cloudformation:us-east-2:111:stack/gco-global/abc"
                        ),
                    }
                ]
            }
        ]
        mock_cfn.get_paginator.return_value = mock_paginator

        with patch("boto3.client", return_value=mock_cfn):
            manager = StackManager(config)
            assert manager._api_gateway_imports_from_analytics() is False

    def test_returns_true_when_api_gateway_is_importer(self):
        """If gco-api-gateway appears in the Imports list for any
        gco-analytics export, return True."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"
        config.api_gateway_region = "us-east-2"

        mock_cfn = MagicMock()
        export_paginator = MagicMock()
        export_paginator.paginate.return_value = [
            {
                "Exports": [
                    {
                        "Name": "gco-analytics:CognitoPoolArn",
                        "ExportingStackId": (
                            "arn:aws:cloudformation:us-east-2:111:stack/gco-analytics/abc"
                        ),
                    }
                ]
            }
        ]
        import_paginator = MagicMock()
        import_paginator.paginate.return_value = [{"Imports": ["gco-api-gateway"]}]

        def get_paginator(op_name):
            return export_paginator if op_name == "list_exports" else import_paginator

        mock_cfn.get_paginator.side_effect = get_paginator

        with patch("boto3.client", return_value=mock_cfn):
            manager = StackManager(config)
            assert manager._api_gateway_imports_from_analytics() is True

    def test_returns_false_when_no_importers(self):
        """Export exists but has no consumers yet."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"
        config.api_gateway_region = "us-east-2"

        mock_cfn = MagicMock()
        export_paginator = MagicMock()
        export_paginator.paginate.return_value = [
            {
                "Exports": [
                    {
                        "Name": "gco-analytics:CognitoPoolArn",
                        "ExportingStackId": (
                            "arn:aws:cloudformation:us-east-2:111:stack/gco-analytics/abc"
                        ),
                    }
                ]
            }
        ]
        import_paginator = MagicMock()
        import_paginator.paginate.return_value = [{"Imports": []}]

        def get_paginator(op_name):
            return export_paginator if op_name == "list_exports" else import_paginator

        mock_cfn.get_paginator.side_effect = get_paginator

        with patch("boto3.client", return_value=mock_cfn):
            manager = StackManager(config)
            assert manager._api_gateway_imports_from_analytics() is False


class TestStackManagerDeployAnalyticsAutoApiGateway:
    """Tests for auto-deploying API gateway after analytics deploy."""

    def test_deploy_analytics_triggers_api_gateway_deploy(self):
        """After deploying gco-analytics, gco-api-gateway is auto-deployed."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"
        config.api_gateway_region = "us-east-2"

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_check_and_fix_stuck_stack"),
            patch.object(StackManager, "ensure_bootstrapped", return_value=True),
            patch.object(StackManager, "_get_stack_status", return_value="CREATE_COMPLETE"),
        ):
            mock_run.return_value = MagicMock(returncode=0)

            manager = StackManager(config)
            manager._lambda_packages_rebuilt = True
            manager._lambda_sources_synced = True
            result = manager.deploy(stack_name="gco-analytics", require_approval=False)

            assert result is True
            # Should be called twice: once for analytics, once for api-gateway.
            assert mock_run.call_count == 2
            second_call_args = mock_run.call_args_list[1][0][0]
            assert "gco-api-gateway" in second_call_args

    def test_deploy_non_analytics_does_not_trigger_api_gateway(self):
        """Deploying a non-analytics stack does not auto-deploy API gateway."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.api_gateway_region = "us-east-2"

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_check_and_fix_stuck_stack"),
            patch.object(StackManager, "ensure_bootstrapped", return_value=True),
            patch.object(StackManager, "_get_stack_status", return_value="CREATE_COMPLETE"),
        ):
            mock_run.return_value = MagicMock(returncode=0)

            manager = StackManager(config)
            manager._lambda_packages_rebuilt = True
            manager._lambda_sources_synced = True
            result = manager.deploy(stack_name="gco-us-east-1", require_approval=False)

            assert result is True
            # Only called once (no follow-up API gateway deploy).
            assert mock_run.call_count == 1


class TestStackManagerOrchestratedParallel:
    """Tests for orchestrated deploy/destroy with parallel execution."""

    def test_deploy_orchestrated_parallel(self):
        """Test orchestrated deployment with parallel regional stacks."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "list_stacks") as mock_list,
            patch.object(StackManager, "deploy") as mock_deploy,
        ):
            mock_list.return_value = [
                "gco-global",
                "gco-us-east-1",
                "gco-us-west-2",
                "gco-monitoring",
            ]
            mock_deploy.return_value = True

            manager = StackManager(config)
            success, successful, failed = manager.deploy_orchestrated(
                require_approval=False,
                parallel=True,
                max_workers=2,
            )

            assert success is True
            assert len(successful) == 4
            assert len(failed) == 0

    def test_destroy_orchestrated_parallel(self):
        """Test orchestrated destruction with parallel regional stacks."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch.object(StackManager, "list_stacks") as mock_list,
            patch.object(StackManager, "destroy") as mock_destroy,
        ):
            mock_list.return_value = [
                "gco-global",
                "gco-us-east-1",
                "gco-us-west-2",
                "gco-monitoring",
            ]
            mock_destroy.return_value = True

            manager = StackManager(config)
            success, successful, failed = manager.destroy_orchestrated(
                force=True,
                parallel=True,
                max_workers=2,
            )

            assert success is True
            assert len(successful) == 4
            assert len(failed) == 0

    def test_deploy_orchestrated_parallel_with_failure(self):
        """Test orchestrated deployment with parallel failure."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "list_stacks") as mock_list,
            patch.object(StackManager, "deploy") as mock_deploy,
        ):
            mock_list.return_value = [
                "gco-global",
                "gco-us-east-1",
                "gco-us-west-2",
            ]

            # Use a function to return results based on stack name, not call order
            # This handles the non-deterministic order of parallel execution
            def deploy_side_effect(stack_name, **kwargs):
                return stack_name != "gco-us-west-2"

            mock_deploy.side_effect = deploy_side_effect

            manager = StackManager(config)
            success, successful, failed = manager.deploy_orchestrated(
                require_approval=False,
                parallel=True,
            )

            assert success is False
            assert "gco-us-west-2" in failed


class TestStackManagerGetStackStatus:
    """Tests for get_stack_status method."""

    def test_get_stack_status_success(self):
        """Test successful stack status retrieval."""
        from datetime import datetime

        from cli.stacks import StackManager

        config = MagicMock()

        with patch("boto3.client") as mock_boto:
            mock_cf = MagicMock()
            mock_boto.return_value = mock_cf
            mock_cf.describe_stacks.return_value = {
                "Stacks": [
                    {
                        "StackName": "test-stack",
                        "StackStatus": "CREATE_COMPLETE",
                        "CreationTime": datetime(2024, 1, 1),
                        "LastUpdatedTime": datetime(2024, 1, 2),
                        "Outputs": [{"OutputKey": "Key1", "OutputValue": "Value1"}],
                        "Tags": [{"Key": "Env", "Value": "test"}],
                    }
                ]
            }

            manager = StackManager(config)
            status = manager.get_stack_status("test-stack", "us-east-1")

            assert status is not None
            assert status.name == "test-stack"
            assert status.status == "CREATE_COMPLETE"
            assert status.outputs == {"Key1": "Value1"}
            assert status.tags == {"Env": "test"}

    def test_get_stack_status_not_found(self):
        """Test stack status when stack not found."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch("boto3.client") as mock_boto:
            mock_cf = MagicMock()
            mock_boto.return_value = mock_cf
            mock_cf.describe_stacks.return_value = {"Stacks": []}

            manager = StackManager(config)
            status = manager.get_stack_status("nonexistent", "us-east-1")

            assert status is None

    def test_get_stack_status_error(self):
        """Test stack status with API error."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch("boto3.client") as mock_boto:
            mock_cf = MagicMock()
            mock_boto.return_value = mock_cf
            mock_cf.describe_stacks.side_effect = Exception("API error")

            manager = StackManager(config)
            status = manager.get_stack_status("test-stack", "us-east-1")

            assert status is None


class TestStackManagerGetOutputs:
    """Tests for get_outputs method."""

    def test_get_outputs_success(self):
        """Test successful outputs retrieval."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch("boto3.client") as mock_boto:
            mock_cf = MagicMock()
            mock_boto.return_value = mock_cf
            mock_cf.describe_stacks.return_value = {
                "Stacks": [
                    {
                        "Outputs": [
                            {"OutputKey": "Key1", "OutputValue": "Value1"},
                            {"OutputKey": "Key2", "OutputValue": "Value2"},
                        ]
                    }
                ]
            }

            manager = StackManager(config)
            outputs = manager.get_outputs("test-stack", "us-east-1")

            assert outputs == {"Key1": "Value1", "Key2": "Value2"}

    def test_get_outputs_empty(self):
        """Test outputs when stack has no outputs."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch("boto3.client") as mock_boto:
            mock_cf = MagicMock()
            mock_boto.return_value = mock_cf
            mock_cf.describe_stacks.return_value = {"Stacks": [{}]}

            manager = StackManager(config)
            outputs = manager.get_outputs("test-stack", "us-east-1")

            assert outputs == {}


class TestContainerRuntimeDetectionExtended:
    """Extended tests for container runtime detection."""

    @pytest.fixture(autouse=True)
    def reset_runtime_cache(self):
        """Reset the container runtime cache before each test."""
        import cli.stacks as stacks_module

        stacks_module._container_runtime_checked = False
        stacks_module._container_runtime_cache = None
        yield
        stacks_module._container_runtime_checked = False
        stacks_module._container_runtime_cache = None

    def test_detect_finch_when_docker_fails(self):
        """Test finch detection when docker command fails."""
        from cli.stacks import _detect_container_runtime

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CDK_DOCKER", None)

            with patch("shutil.which") as mock_which:

                def which_side_effect(cmd):
                    if cmd == "docker":
                        return "/usr/bin/docker"
                    if cmd == "finch":
                        return "/usr/local/bin/finch"
                    return None

                mock_which.side_effect = which_side_effect

                with patch("subprocess.run") as mock_run:

                    def run_side_effect(cmd, **kwargs):
                        if cmd[0] == "docker":
                            raise Exception("Docker not running")
                        if cmd[0] == "finch":
                            return MagicMock(returncode=0)
                        return MagicMock(returncode=1)

                    mock_run.side_effect = run_side_effect
                    result = _detect_container_runtime()
                    assert result == "finch"

    def test_detect_finch_timeout(self):
        """Test handling of finch info timeout."""
        from cli.stacks import _detect_container_runtime

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CDK_DOCKER", None)

            with patch("shutil.which") as mock_which:

                def which_side_effect(cmd):
                    if cmd == "docker":
                        return None
                    if cmd == "finch":
                        return "/usr/local/bin/finch"
                    return None

                mock_which.side_effect = which_side_effect

                with patch("subprocess.run") as mock_run:
                    mock_run.side_effect = subprocess.TimeoutExpired("finch", 5)
                    result = _detect_container_runtime()
                    assert result is None

    def test_detect_podman_when_docker_and_finch_unavailable(self):
        """Test podman detection when docker and finch are not available."""
        from cli.stacks import _detect_container_runtime

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CDK_DOCKER", None)

            with patch("shutil.which") as mock_which:

                def which_side_effect(cmd):
                    if cmd == "docker":
                        return None
                    if cmd == "finch":
                        return None
                    if cmd == "podman":
                        return "/usr/local/bin/podman"
                    return None

                mock_which.side_effect = which_side_effect

                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0)
                    result = _detect_container_runtime()
                    assert result == "podman"

    def test_detect_podman_timeout(self):
        """Test handling of podman info timeout."""
        from cli.stacks import _detect_container_runtime

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CDK_DOCKER", None)

            with patch("shutil.which") as mock_which:

                def which_side_effect(cmd):
                    if cmd == "docker":
                        return None
                    if cmd == "finch":
                        return None
                    if cmd == "podman":
                        return "/usr/local/bin/podman"
                    return None

                mock_which.side_effect = which_side_effect

                with patch("subprocess.run") as mock_run:
                    mock_run.side_effect = subprocess.TimeoutExpired("podman", 5)
                    result = _detect_container_runtime()
                    assert result is None


class TestStackManagerBootstrapOptions:
    """Tests for bootstrap method with various options."""

    def test_bootstrap_with_region_only(self):
        """Test bootstrap with region only."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_ensure_lambda_build"),
        ):
            mock_run.return_value = MagicMock(returncode=0)

            manager = StackManager(config)
            result = manager.bootstrap(region="us-east-1")

            assert result is True
            call_args = mock_run.call_args[0][0]
            assert "aws://unknown-account/us-east-1" in call_args

    def test_bootstrap_failure(self):
        """Test bootstrap failure."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_ensure_lambda_build"),
        ):
            mock_run.return_value = MagicMock(returncode=1)

            manager = StackManager(config)
            result = manager.bootstrap(region="us-east-1")

            assert result is False


class TestCleanupEksSecurityGroups:
    """Tests for EKS security group cleanup during destroy."""

    def test_cleanup_finds_and_deletes_orphaned_sg(self):
        """Test that cleanup finds and deletes an orphaned EKS security group."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"

        mock_ec2 = MagicMock()
        mock_ec2.describe_security_groups.return_value = {
            "SecurityGroups": [
                {
                    "GroupId": "sg-12345",
                    "GroupName": "eks-cluster-sg-gco-us-east-1-123456",
                }
            ]
        }
        mock_ec2.describe_network_interfaces.return_value = {"NetworkInterfaces": []}

        with (
            patch("boto3.client", return_value=mock_ec2),
            patch.object(StackManager, "_find_project_root", return_value=Path(".")),
        ):
            manager = StackManager(config)
            manager._cleanup_eks_security_groups("gco-us-east-1")

            mock_ec2.describe_security_groups.assert_called_once_with(
                Filters=[{"Name": "group-name", "Values": ["eks-cluster-sg-gco-us-east-1-*"]}]
            )
            mock_ec2.delete_security_group.assert_called_once_with(GroupId="sg-12345")

    def test_cleanup_detaches_enis_before_deleting_sg(self):
        """Test that cleanup detaches ENIs before deleting the security group."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"

        mock_ec2 = MagicMock()
        mock_ec2.describe_security_groups.return_value = {
            "SecurityGroups": [
                {
                    "GroupId": "sg-12345",
                    "GroupName": "eks-cluster-sg-gco-us-east-1-123456",
                }
            ]
        }
        mock_ec2.describe_network_interfaces.return_value = {
            "NetworkInterfaces": [
                {
                    "NetworkInterfaceId": "eni-abc123",
                    "Attachment": {"AttachmentId": "attach-xyz"},
                }
            ]
        }

        with (
            patch("boto3.client", return_value=mock_ec2),
            patch("time.sleep"),
            patch.object(StackManager, "_find_project_root", return_value=Path(".")),
        ):
            manager = StackManager(config)
            manager._cleanup_eks_security_groups("gco-us-east-1")

            mock_ec2.detach_network_interface.assert_called_once_with(
                AttachmentId="attach-xyz", Force=True
            )
            mock_ec2.delete_network_interface.assert_called_once_with(
                NetworkInterfaceId="eni-abc123"
            )
            mock_ec2.delete_security_group.assert_called_once_with(GroupId="sg-12345")

    def test_cleanup_no_orphaned_sgs(self):
        """Test that cleanup does nothing when no orphaned SGs exist."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"

        mock_ec2 = MagicMock()
        mock_ec2.describe_security_groups.return_value = {"SecurityGroups": []}

        with (
            patch("boto3.client", return_value=mock_ec2),
            patch.object(StackManager, "_find_project_root", return_value=Path(".")),
        ):
            manager = StackManager(config)
            manager._cleanup_eks_security_groups("gco-us-east-1")

            mock_ec2.delete_security_group.assert_not_called()

    def test_cleanup_handles_api_errors_gracefully(self):
        """Test that cleanup doesn't raise on API errors."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"

        mock_ec2 = MagicMock()
        mock_ec2.describe_security_groups.side_effect = Exception("API error")

        with (
            patch("boto3.client", return_value=mock_ec2),
            patch.object(StackManager, "_find_project_root", return_value=Path(".")),
        ):
            manager = StackManager(config)
            # Should not raise
            manager._cleanup_eks_security_groups("gco-us-east-1")

    def test_cleanup_extracts_region_from_stack_name(self):
        """Test that cleanup correctly extracts region from stack name."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"

        mock_ec2 = MagicMock()
        mock_ec2.describe_security_groups.return_value = {"SecurityGroups": []}

        with (
            patch("boto3.client", return_value=mock_ec2) as mock_client,
            patch.object(StackManager, "_find_project_root", return_value=Path(".")),
        ):
            manager = StackManager(config)
            manager._cleanup_eks_security_groups("gco-eu-west-1")

            mock_client.assert_called_with("ec2", region_name="eu-west-1")

    def test_public_cleanup_iterates_regional_stacks(self):
        """Test that the public cleanup method iterates over regional stacks only."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"

        with (
            patch.object(
                StackManager,
                "list_stacks",
                return_value=[
                    "gco-global",
                    "gco-api-gateway",
                    "gco-monitoring",
                    "gco-us-east-1",
                    "gco-eu-west-1",
                ],
            ),
            patch.object(StackManager, "_cleanup_eks_security_groups") as mock_cleanup,
            patch.object(StackManager, "_find_project_root", return_value=Path(".")),
        ):
            manager = StackManager(config)
            manager.cleanup_eks_security_groups()

            # Should only clean up regional stacks, not global/api-gateway/monitoring
            assert mock_cleanup.call_count == 2
            mock_cleanup.assert_any_call("gco-us-east-1")
            mock_cleanup.assert_any_call("gco-eu-west-1")


class TestEksSgWatchdog:
    """
    Regression guards for the EKS security-group watchdog that runs
    alongside regional-stack destroy.

    Context: EKS creates ``eks-cluster-sg-<cluster-name>-*`` security
    groups owned by the EKS service (not CloudFormation). When the
    cluster resource deletes, the SG is supposed to GC with its cluster
    ENIs — but on EKS Auto Mode there's a window where the SG lingers
    after the cluster is gone, blocking the subsequent VPC delete with
    ``DependencyViolation``. Without our watchdog, CloudFormation sits in
    ``DELETE_IN_PROGRESS`` on the VPC for ~10 minutes polling its
    dependencies. The watchdog polls every 30s and reaps the SG as soon
    as it's safely deletable, unblocking the VPC delete immediately.
    """

    def _make_manager(self):
        """Minimal StackManager construction for orchestration tests."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"
        return StackManager(config)

    def test_watchdog_starts_daemon_thread_and_calls_cleanup(self):
        """The watchdog spawns a daemon thread that calls
        ``_cleanup_eks_security_groups`` on the given stack."""
        from threading import Event

        mgr = self._make_manager()

        calls: list[str] = []

        def fake_cleanup(stack_name):
            calls.append(stack_name)

        stop = Event()
        with patch.object(mgr, "_cleanup_eks_security_groups", side_effect=fake_cleanup):
            thread = mgr._start_eks_sg_watchdog("gco-us-east-1", stop)
            # Thread must be started as a daemon so a hung watchdog can't
            # wedge interpreter shutdown.
            assert thread.is_alive()
            assert thread.daemon is True
            # Give the watchdog a tick to do its first call, then stop it.
            # The real interval is 30s; we assert against "at least one"
            # rather than busy-waiting for a full tick.
            import time as _t

            for _ in range(20):
                if calls:
                    break
                _t.sleep(0.05)
            stop.set()
            thread.join(timeout=2)

        assert calls and all(c == "gco-us-east-1" for c in calls)
        assert not thread.is_alive()

    def test_watchdog_stop_event_terminates_thread_promptly(self):
        """Setting ``stop_event`` must cause the thread to exit without
        waiting for the full 30-second poll cycle — otherwise destroy
        would hang waiting for the watchdog to notice shutdown."""
        from threading import Event

        mgr = self._make_manager()
        stop = Event()

        with patch.object(mgr, "_cleanup_eks_security_groups"):
            thread = mgr._start_eks_sg_watchdog("gco-us-east-1", stop)
            assert thread.is_alive()
            stop.set()
            thread.join(timeout=2)

        # Should have exited well within 2s despite the 30s sleep interval,
        # because Event.wait returns immediately on set().
        assert not thread.is_alive()

    def test_watchdog_survives_cleanup_exception(self):
        """A transient AWS error in one tick must not kill the watchdog
        thread. Without this guarantee a single throttled API call could
        disable the watchdog for the whole destroy window."""
        from threading import Event

        mgr = self._make_manager()
        first_call_done = Event()

        def flaky_cleanup(stack_name):
            first_call_done.set()
            raise RuntimeError("simulated AWS throttle")

        stop = Event()
        with patch.object(mgr, "_cleanup_eks_security_groups", side_effect=flaky_cleanup):
            thread = mgr._start_eks_sg_watchdog("gco-us-east-1", stop)

            # Wait until the first (failing) call has occurred.
            assert first_call_done.wait(timeout=2), "watchdog never ticked"

            # The thread must STILL be alive even though the tick raised.
            # This is the invariant we care about: one flaky tick does not
            # kill the watchdog.
            assert thread.is_alive()

            stop.set()
            thread.join(timeout=5)

        assert not thread.is_alive()

    def test_destroy_orchestrated_starts_watchdog_per_regional_stack(self):
        """Each regional stack must get its own watchdog thread so the
        multi-region case doesn't rely on one watchdog polling every
        region's SG list."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"

        started_stacks: list[str] = []

        def fake_start_watchdog(self_ref, stack_name, stop_event):
            started_stacks.append(stack_name)
            # Return a no-op thread that exits immediately when stop is set.
            from threading import Thread

            def _noop():
                stop_event.wait(timeout=60)

            t = Thread(target=_noop, daemon=True)
            t.start()
            return t

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "list_stacks") as mock_list,
            patch.object(StackManager, "destroy", return_value=True),
            patch.object(StackManager, "_cleanup_backup_vault"),
            patch.object(StackManager, "_cleanup_eks_security_groups"),
            patch.object(
                StackManager,
                "_start_eks_sg_watchdog",
                autospec=True,
                side_effect=fake_start_watchdog,
            ),
        ):
            mock_list.return_value = [
                "gco-global",
                "gco-api-gateway",
                "gco-us-east-1",
                "gco-us-west-2",
                "gco-monitoring",
            ]
            mgr = StackManager(config)
            mgr.destroy_orchestrated(force=True)

        # Only regional stacks get a watchdog — globals and monitoring
        # don't have VPCs with EKS-owned SGs.
        assert set(started_stacks) == {"gco-us-east-1", "gco-us-west-2"}

    def test_destroy_orchestrated_final_cleanup_runs_after_regional_destroy(self):
        """After each regional destroy exits, a synchronous cleanup pass
        must run to catch anything the watchdog missed during the tail
        of the destroy. Otherwise a late-arriving orphan SG would still
        block the next destroy invocation."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"

        final_cleanup_calls: list[str] = []

        def record_cleanup(self_ref, stack_name):
            final_cleanup_calls.append(stack_name)

        # No-op watchdog: start and stop cleanly without recording cleanup.
        def fake_start_watchdog(self_ref, stack_name, stop_event):
            from threading import Thread

            def _noop():
                stop_event.wait(timeout=60)

            t = Thread(target=_noop, daemon=True)
            t.start()
            return t

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "list_stacks") as mock_list,
            patch.object(StackManager, "destroy", return_value=True),
            patch.object(StackManager, "_cleanup_backup_vault"),
            patch.object(
                StackManager,
                "_cleanup_eks_security_groups",
                autospec=True,
                side_effect=record_cleanup,
            ),
            patch.object(
                StackManager,
                "_start_eks_sg_watchdog",
                autospec=True,
                side_effect=fake_start_watchdog,
            ),
        ):
            mock_list.return_value = ["gco-global", "gco-us-east-1"]
            mgr = StackManager(config)
            mgr.destroy_orchestrated(force=True)

        # The synchronous final pass should hit gco-us-east-1 once. The
        # watchdog is mocked to a no-op, so this call is the orchestrator's.
        assert "gco-us-east-1" in final_cleanup_calls


class TestAutoMirrorOnDeploy:
    """StackManager auto-mirrors third-party images before a regional deploy.

    Covers the deploy hook (``_mirror_images_if_enabled``) and its region
    selection (``_mirror_target_regions``) with the mirror core mocked — no AWS,
    no Docker. The hook must: no-op when disabled, mirror a regional stack's own
    region, skip the named global/api-gateway/monitoring stacks, cover every
    regional region on ``--all``, and fail fast (raise) when an enabled mirror
    errors so the deploy never proceeds with images missing from ECR.
    """

    def _manager(self):
        from cli.stacks import StackManager

        # ``_mirror_target_regions`` derives the regional-stack prefix from
        # ``config.project_name`` (#139) — e.g. ``gco-us-east-1``. A bare
        # MagicMock would make project_name a Mock object, producing a
        # garbage prefix that never matches the stack name, so the mirror
        # would silently no-op. Pin it to the default project so the prefix
        # is the realistic ``gco-``.
        config = MagicMock()
        config.project_name = "gco"
        return StackManager(config)

    _ENABLED = {"enabled": True, "ecr_namespace": "gco/dockerhub"}
    _DISABLED = {"enabled": False, "ecr_namespace": "gco/dockerhub"}

    def test_disabled_is_noop(self):
        mgr = self._manager()
        with (
            patch("cli._image_mirror.read_mirror_config", return_value=self._DISABLED),
            patch("cli._image_mirror.mirror_images") as mock_mirror,
        ):
            mgr._mirror_images_if_enabled(stack_name="gco-us-east-1", all_stacks=False)
        mock_mirror.assert_not_called()

    def test_regional_stack_mirrors_its_region(self):
        mgr = self._manager()
        with (
            patch("cli._image_mirror.read_mirror_config", return_value=self._ENABLED),
            patch("cli._image_mirror.mirror_images") as mock_mirror,
        ):
            mgr._mirror_images_if_enabled(stack_name="gco-us-east-1", all_stacks=False)
        mock_mirror.assert_called_once()
        assert mock_mirror.call_args.args[0] == "us-east-1"
        assert mock_mirror.call_args.kwargs["ecr_namespace"] == "gco/dockerhub"

    def test_named_global_stack_skipped(self):
        mgr = self._manager()
        with (
            patch("cli._image_mirror.read_mirror_config", return_value=self._ENABLED),
            patch("cli._image_mirror.mirror_images") as mock_mirror,
        ):
            mgr._mirror_images_if_enabled(stack_name="gco-global", all_stacks=False)
        mock_mirror.assert_not_called()

    def test_all_stacks_mirrors_every_regional_region(self):
        mgr = self._manager()
        with (
            patch("cli._image_mirror.read_mirror_config", return_value=self._ENABLED),
            patch("cli._image_mirror.mirror_images") as mock_mirror,
            patch(
                "cli.config._load_cdk_json", return_value={"regional": ["us-east-1", "us-west-2"]}
            ),
        ):
            mgr._mirror_images_if_enabled(stack_name=None, all_stacks=True)
        assert [c.args[0] for c in mock_mirror.call_args_list] == ["us-east-1", "us-west-2"]

    def test_mirror_failure_aborts_before_cdk(self):
        mgr = self._manager()
        with (
            patch("cli._image_mirror.read_mirror_config", return_value=self._ENABLED),
            patch("cli._image_mirror.mirror_images", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError, match="Image mirror failed"),
        ):
            mgr._mirror_images_if_enabled(stack_name="gco-us-east-1", all_stacks=False)

    def test_target_regions_selection(self):
        mgr = self._manager()
        assert mgr._mirror_target_regions("gco-us-east-1", False) == ["us-east-1"]
        assert mgr._mirror_target_regions("gco-global", False) == []
        assert mgr._mirror_target_regions("gco-monitoring", False) == []
        with patch(
            "cli.config._load_cdk_json",
            return_value={"regional": ["us-east-1", "eu-west-1", "us-east-1"]},
        ):
            # Deduplicated, order-stable.
            assert mgr._mirror_target_regions(None, True) == ["us-east-1", "eu-west-1"]

    def test_non_gco_project_regional_stack_mirrors_its_region(self):
        """#139 regression: a non-``gco`` project's regional stack must still
        trigger the mirror.

        ``_mirror_target_regions`` -> ``_get_deploy_region`` must resolve the
        region from the project-scoped stack name (``acme-us-east-1``) rather
        than a hardcoded ``gco-`` prefix. The original bug returned ``None``
        for a non-``gco`` regional stack, so the mirror silently no-opped and
        the regional Volcano Helm install had no images to pull.
        """
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "acme"
        mgr = StackManager(config)
        with (
            patch(
                "cli._image_mirror.read_mirror_config",
                return_value={"enabled": True, "ecr_namespace": "acme/dockerhub"},
            ),
            patch("cli._image_mirror.mirror_images") as mock_mirror,
        ):
            mgr._mirror_images_if_enabled(stack_name="acme-us-east-1", all_stacks=False)
        mock_mirror.assert_called_once()
        assert mock_mirror.call_args.args[0] == "us-east-1"
        assert mock_mirror.call_args.kwargs["ecr_namespace"] == "acme/dockerhub"

    def test_non_gco_project_named_stacks_skipped(self):
        """#139 regression: named global/api-gateway/monitoring stacks for a
        non-``gco`` project are classified by suffix and skipped by the mirror
        (only regional stacks run the Helm install that needs it)."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "acme"
        mgr = StackManager(config)
        assert mgr._mirror_target_regions("acme-us-east-1", False) == ["us-east-1"]
        assert mgr._mirror_target_regions("acme-global", False) == []
        assert mgr._mirror_target_regions("acme-api-gateway", False) == []
        assert mgr._mirror_target_regions("acme-monitoring", False) == []


class TestStackExistsInCloudFormation:
    """Directly exercise the existence check that gates destroy success.

    Regression for the false-success bug: a DELETE_FAILED (or
    DELETE_IN_PROGRESS) stack still exists and must report present, so a
    failed ``cdk destroy`` is not reconciled into a "destroyed successfully".
    Only DELETE_COMPLETE (or a missing stack) counts as gone.
    """

    def _check(self, status=None, raises=False):
        from cli.stacks import StackManager

        config = MagicMock()
        config.api_gateway_region = "us-east-2"
        manager = StackManager(config)
        fake_cfn = MagicMock()
        if raises:
            fake_cfn.describe_stacks.side_effect = ClientError(
                {
                    "Error": {
                        "Code": "ValidationError",
                        "Message": "Stack gco-us-east-1 does not exist",
                    }
                },
                "DescribeStacks",
            )
        else:
            fake_cfn.describe_stacks.return_value = {"Stacks": [{"StackStatus": status}]}
        with (
            patch.object(StackManager, "_get_destroy_region", return_value="us-east-1"),
            patch("boto3.client", return_value=fake_cfn),
        ):
            return manager._stack_exists_in_cloudformation("gco-us-east-1")

    def test_create_complete_is_present(self):
        assert self._check("CREATE_COMPLETE") is True

    def test_update_complete_is_present(self):
        assert self._check("UPDATE_COMPLETE") is True

    def test_delete_failed_is_present(self):
        # The bug: a stack that failed to delete is still very much present.
        assert self._check("DELETE_FAILED") is True

    def test_delete_in_progress_is_present(self):
        assert self._check("DELETE_IN_PROGRESS") is True

    def test_delete_complete_is_absent(self):
        assert self._check("DELETE_COMPLETE") is False

    def test_missing_stack_is_absent(self):
        assert self._check(raises=True) is False


class TestDestroyReconciliationDeleteFailed:
    """destroy() must surface failure (not success) when the stack is DELETE_FAILED."""

    def test_destroy_reports_failure_when_stack_delete_failed(self):
        """cdk exits non-zero and CFN still has the stack in DELETE_FAILED.

        Uses the REAL _stack_exists_in_cloudformation (boto3 mocked) so this
        would have caught the original false-success bug end to end.
        """
        from cli.stacks import StackManager

        config = MagicMock()
        config.api_gateway_region = "us-east-2"
        fake_cfn = MagicMock()
        fake_cfn.describe_stacks.return_value = {"Stacks": [{"StackStatus": "DELETE_FAILED"}]}

        with (
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_get_destroy_region", return_value="us-east-1"),
            patch("boto3.client", return_value=fake_cfn),
        ):
            mock_run.return_value = MagicMock(returncode=1)  # cdk destroy failed

            manager = StackManager(config)
            result = manager.destroy(stack_name="gco-us-east-1", force=True)

        assert result is False


class TestCdkToolchainPreflight:
    """Preflight guard: infra ops raise an actionable ``CdkToolchainError``
    when the CDK Python toolchain (aws-cdk-lib / cdk-nag) isn't importable,
    instead of a cryptic ImportError from the ``python3 app.py`` synth
    subprocess.

    Regression coverage for the out-of-the-box failure where a base
    ``uvx`` / ``pip`` install of ``gco-cli`` (no ``[cdk]`` extra) runs
    ``deploy`` / ``synth`` / ``list`` and the CDK subprocess dies importing
    ``aws_cdk``.
    """

    @staticmethod
    def _find_spec_missing(*missing):
        """A ``find_spec`` side effect: ``None`` for the named modules, a
        truthy sentinel for everything else."""

        def _side_effect(name, *args, **kwargs):
            return None if name in missing else object()

        return _side_effect

    def test_missing_toolchain_raises_actionable_error(self):
        from cli.stacks import CdkToolchainError, StackManager

        manager = StackManager(MagicMock())

        with (
            patch(
                "importlib.util.find_spec",
                side_effect=self._find_spec_missing("aws_cdk", "cdk_nag"),
            ),
            pytest.raises(CdkToolchainError) as exc,
        ):
            manager._ensure_cdk_toolchain()

        msg = str(exc.value)
        assert "aws_cdk" in msg
        assert "cdk_nag" in msg
        assert "[cdk]" in msg  # names the extra to install
        assert "pip install" in msg  # actionable install hint

    def test_partial_missing_toolchain_raises(self):
        from cli.stacks import CdkToolchainError, StackManager

        manager = StackManager(MagicMock())

        # Only cdk_nag missing (aws_cdk present) must still raise.
        with (
            patch(
                "importlib.util.find_spec",
                side_effect=self._find_spec_missing("cdk_nag"),
            ),
            pytest.raises(CdkToolchainError, match="cdk_nag"),
        ):
            manager._ensure_cdk_toolchain()

    def test_present_toolchain_passes(self):
        from cli.stacks import StackManager

        manager = StackManager(MagicMock())

        with patch(
            "importlib.util.find_spec",
            side_effect=self._find_spec_missing(),  # nothing missing
        ):
            assert manager._ensure_cdk_toolchain() is None

    def test_run_cdk_preflights_before_spawning_cdk(self):
        """``_run_cdk`` must raise the toolchain error BEFORE spawning cdk."""
        from cli.stacks import CdkToolchainError, StackManager

        manager = StackManager(MagicMock())

        with (
            patch("subprocess.run") as mock_run,
            patch(
                "importlib.util.find_spec",
                side_effect=self._find_spec_missing("aws_cdk", "cdk_nag"),
            ),
            pytest.raises(CdkToolchainError),
        ):
            manager._run_cdk(["list"], capture_output=True)

        mock_run.assert_not_called()
