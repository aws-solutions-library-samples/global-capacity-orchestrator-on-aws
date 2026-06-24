"""
Extended coverage for cli/stacks.StackManager — CloudFormation output
discovery, bootstrap gating, and the deploy/destroy CLI wrapper.

Exercises get_outputs and get_stack_status against mocked boto3
CloudFormation clients (success, missing outputs, stack-not-found,
ClientError), deploy/destroy argv shape with --all/--outputs-file/
--parameters/--tags/CDK_DOCKER env handling, _get_deploy_region
mapping for gco-global/gco-api-gateway/gco-monitoring/regional
stacks (with cdk.json override support), and the
is_bootstrapped + ensure_bootstrapped pair that gates cdk deploy on
a live CDKToolkit stack. Also covers update_fsx_config tmp_path
round-trips and the deploy() integration with ensure_bootstrapped.
"""

import json
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestStackManagerGetOutputs:
    """Tests for StackManager.get_outputs method."""

    def test_get_outputs_success(self):
        """Test getting stack outputs successfully."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch("boto3.client") as mock_boto:
            mock_cf = MagicMock()
            mock_cf.describe_stacks.return_value = {
                "Stacks": [
                    {
                        "StackName": "test-stack",
                        "Outputs": [
                            {"OutputKey": "VpcId", "OutputValue": "vpc-12345"},
                            {
                                "OutputKey": "ClusterArn",
                                "OutputValue": "arn:aws:eks:us-east-1:123:cluster/test",
                            },
                        ],
                    }
                ]
            }
            mock_boto.return_value = mock_cf

            manager = StackManager(config)
            outputs = manager.get_outputs("test-stack", "us-east-1")

            assert outputs["VpcId"] == "vpc-12345"
            assert outputs["ClusterArn"] == "arn:aws:eks:us-east-1:123:cluster/test"

    def test_get_outputs_no_outputs(self):
        """Test getting stack outputs when stack has no outputs."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch("boto3.client") as mock_boto:
            mock_cf = MagicMock()
            mock_cf.describe_stacks.return_value = {"Stacks": [{"StackName": "test-stack"}]}
            mock_boto.return_value = mock_cf

            manager = StackManager(config)
            outputs = manager.get_outputs("test-stack", "us-east-1")

            assert outputs == {}

    def test_get_outputs_stack_not_found(self):
        """Test getting outputs when stack doesn't exist."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch("boto3.client") as mock_boto:
            mock_cf = MagicMock()
            mock_cf.describe_stacks.return_value = {"Stacks": []}
            mock_boto.return_value = mock_cf

            manager = StackManager(config)
            outputs = manager.get_outputs("nonexistent-stack", "us-east-1")

            assert outputs == {}

    def test_get_outputs_exception(self):
        """Test getting outputs when exception occurs."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch("boto3.client") as mock_boto:
            mock_cf = MagicMock()
            mock_cf.describe_stacks.side_effect = Exception("Stack not found")
            mock_boto.return_value = mock_cf

            manager = StackManager(config)
            outputs = manager.get_outputs("test-stack", "us-east-1")

            assert outputs == {}


class TestStackManagerGetStackStatus:
    """Tests for StackManager.get_stack_status method."""

    def test_get_stack_status_success(self):
        """Test getting stack status successfully."""
        from cli.stacks import StackManager

        config = MagicMock()
        created_time = datetime(2024, 1, 1, 10, 0, 0)
        updated_time = datetime(2024, 1, 15, 14, 30, 0)

        with patch("boto3.client") as mock_boto:
            mock_cf = MagicMock()
            mock_cf.describe_stacks.return_value = {
                "Stacks": [
                    {
                        "StackName": "test-stack",
                        "StackStatus": "CREATE_COMPLETE",
                        "CreationTime": created_time,
                        "LastUpdatedTime": updated_time,
                        "Outputs": [
                            {"OutputKey": "VpcId", "OutputValue": "vpc-12345"},
                        ],
                        "Tags": [
                            {"Key": "Environment", "Value": "production"},
                        ],
                    }
                ]
            }
            mock_boto.return_value = mock_cf

            manager = StackManager(config)
            status = manager.get_stack_status("test-stack", "us-east-1")

            assert status is not None
            assert status.name == "test-stack"
            assert status.status == "CREATE_COMPLETE"
            assert status.region == "us-east-1"
            assert status.created_time == created_time
            assert status.updated_time == updated_time
            assert status.outputs["VpcId"] == "vpc-12345"
            assert status.tags["Environment"] == "production"

    def test_get_stack_status_not_found(self):
        """Test getting status when stack doesn't exist."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch("boto3.client") as mock_boto:
            mock_cf = MagicMock()
            mock_cf.describe_stacks.return_value = {"Stacks": []}
            mock_boto.return_value = mock_cf

            manager = StackManager(config)
            status = manager.get_stack_status("nonexistent-stack", "us-east-1")

            assert status is None

    def test_get_stack_status_exception(self):
        """Test getting status when exception occurs."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch("boto3.client") as mock_boto:
            mock_cf = MagicMock()
            mock_cf.describe_stacks.side_effect = Exception("Access denied")
            mock_boto.return_value = mock_cf

            manager = StackManager(config)
            status = manager.get_stack_status("test-stack", "us-east-1")

            assert status is None


class TestStackManagerDeployOptions:
    """Tests for StackManager.deploy with various options."""

    def test_deploy_with_all_stacks(self):
        """Test deployment with --all flag."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "_run_cdk") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            manager = StackManager(config)
            result = manager.deploy(all_stacks=True, require_approval=False)

            assert result is True
            # Verify --all was passed
            call_args = mock_run.call_args[0][0]
            assert "--all" in call_args

    def test_deploy_with_outputs_file(self):
        """Test deployment with outputs file."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "_run_cdk") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            manager = StackManager(config)
            result = manager.deploy(
                "test-stack",
                outputs_file="/tmp/outputs.json",  # nosec B108 - test fixture using temp directory
                require_approval=False,
            )

            assert result is True
            call_args = mock_run.call_args[0][0]
            assert "--outputs-file" in call_args
            assert (
                "/tmp/outputs.json" in call_args  # nosec B108 - test fixture using temp directory
            )

    def test_deploy_with_parameters(self):
        """Test deployment with parameters."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "_run_cdk") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            manager = StackManager(config)
            result = manager.deploy(
                "test-stack",
                parameters={"Param1": "Value1", "Param2": "Value2"},
                require_approval=False,
            )

            assert result is True
            call_args = mock_run.call_args[0][0]
            assert "--parameters" in call_args
            # Check parameters are included
            params_str = " ".join(call_args)
            assert "Param1=Value1" in params_str
            assert "Param2=Value2" in params_str

    def test_deploy_with_tags(self):
        """Test deployment with tags."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "_run_cdk") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            manager = StackManager(config)
            result = manager.deploy(
                "test-stack",
                tags={"Environment": "prod", "Team": "platform"},
                require_approval=False,
            )

            assert result is True
            call_args = mock_run.call_args[0][0]
            assert "--tags" in call_args
            tags_str = " ".join(call_args)
            assert "Environment=prod" in tags_str
            assert "Team=platform" in tags_str

    def test_deploy_with_cdk_docker_env_set(self):
        """Test deployment when CDK_DOCKER is already set."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch.dict(os.environ, {"CDK_DOCKER": "finch"}),
            patch("cli.stacks._detect_container_runtime", return_value="finch"),
            patch.object(StackManager, "_run_cdk") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            manager = StackManager(config)
            result = manager.deploy("test-stack", require_approval=False)

            assert result is True
            # env should be None since CDK_DOCKER is already set
            call_kwargs = mock_run.call_args[1]
            assert call_kwargs.get("env") is None


class TestStackManagerDestroyOptions:
    """Tests for StackManager.destroy with various options."""

    def test_destroy_with_all_stacks(self):
        """Test destruction with --all flag."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch.object(StackManager, "_run_cdk") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            manager = StackManager(config)
            result = manager.destroy(all_stacks=True, force=True)

            assert result is True
            call_args = mock_run.call_args[0][0]
            assert "--all" in call_args
            assert "--force" in call_args

    def test_destroy_failure(self):
        """Test destruction failure."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=True),
        ):
            mock_run.return_value = MagicMock(returncode=1)

            manager = StackManager(config)
            result = manager.destroy("test-stack")

            assert result is False


class TestStackManagerBootstrapOptions:
    """Tests for StackManager.bootstrap with various options."""

    def test_bootstrap_with_region_only(self):
        """Test bootstrap with region only."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch.object(StackManager, "_run_cdk") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            manager = StackManager(config)
            result = manager.bootstrap(region="us-west-2")

            assert result is True
            call_args = mock_run.call_args[0][0]
            assert "aws://unknown-account/us-west-2" in call_args

    def test_bootstrap_failure(self):
        """Test bootstrap failure."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch.object(StackManager, "_run_cdk") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)

            manager = StackManager(config)
            result = manager.bootstrap(account="123456789012", region="us-east-1")

            assert result is False


class TestRunCdkMethod:
    """Tests for StackManager._run_cdk method."""

    def test_run_cdk_with_env(self):
        """Test running CDK with custom environment."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="output", stderr="")

            manager = StackManager(config)
            manager._cdk_path = "cdk"
            result = manager._run_cdk(
                ["list"],
                capture_output=True,
                env={"CUSTOM_VAR": "value"},
            )

            assert result.returncode == 0
            # Verify env was passed
            call_kwargs = mock_run.call_args[1]
            assert "CUSTOM_VAR" in call_kwargs["env"]

    def test_run_cdk_without_capture(self):
        """Test running CDK without capturing output."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            manager = StackManager(config)
            manager._cdk_path = "cdk"
            result = manager._run_cdk(["deploy"], capture_output=False)

            assert result.returncode == 0
            call_kwargs = mock_run.call_args[1]
            assert "capture_output" not in call_kwargs or call_kwargs.get("capture_output") is False


class TestFindCdkExecutable:
    """Tests for finding CDK executable."""

    def test_find_cdk_in_common_location(self):
        """Test finding CDK in common location."""
        from cli.stacks import StackManager

        config = MagicMock()

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "which")

            with patch("os.path.exists") as mock_exists:
                mock_exists.side_effect = lambda p: p == "/usr/local/bin/cdk"

                manager = StackManager(config)
                assert manager._cdk_path == "/usr/local/bin/cdk"


class TestUpdateFsxConfigEdgeCases:
    """Tests for update_fsx_config edge cases."""

    def test_update_fsx_config_creates_context(self):
        """Test that update creates context section if missing."""
        from cli.stacks import update_fsx_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_config = {}  # No context section
            cdk_path.write_text(json.dumps(cdk_config))

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                update_fsx_config({"enabled": True})

            with open(cdk_path, encoding="utf-8") as f:
                result = json.load(f)
            assert "context" in result
            assert "fsx_lustre" in result["context"]
            assert result["context"]["fsx_lustre"]["enabled"] is True

    def test_update_fsx_config_preserves_other_settings(self):
        """Test that update preserves other cdk.json settings."""
        from cli.stacks import update_fsx_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_config = {
                "app": "python app.py",
                "context": {
                    "other_setting": "value",
                    "fsx_lustre": {"enabled": False},
                },
            }
            cdk_path.write_text(json.dumps(cdk_config))

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                update_fsx_config({"enabled": True, "storage_capacity_gib": 2400})

            with open(cdk_path, encoding="utf-8") as f:
                result = json.load(f)
            assert result["app"] == "python app.py"
            assert result["context"]["other_setting"] == "value"
            assert result["context"]["fsx_lustre"]["enabled"] is True
            assert result["context"]["fsx_lustre"]["storage_capacity_gib"] == 2400

    def test_update_fsx_config_ignores_none_values(self):
        """Test that update ignores None values except for enabled."""
        from cli.stacks import update_fsx_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cdk_path = Path(tmpdir) / "cdk.json"
            cdk_config = {
                "context": {
                    "fsx_lustre": {
                        "enabled": True,
                        "storage_capacity_gib": 1200,
                    }
                }
            }
            cdk_path.write_text(json.dumps(cdk_config))

            with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
                update_fsx_config({"storage_capacity_gib": None, "enabled": False})

            with open(cdk_path, encoding="utf-8") as f:
                result = json.load(f)
            # storage_capacity_gib should remain unchanged (None ignored)
            assert result["context"]["fsx_lustre"]["storage_capacity_gib"] == 1200
            # enabled should be updated even though it's falsy
            assert result["context"]["fsx_lustre"]["enabled"] is False


class TestIsBootstrapped:
    """Tests for StackManager.is_bootstrapped()."""

    def _make_manager(self):
        config = MagicMock()
        with (
            patch(
                "cli.stacks.StackManager._find_project_root",
                return_value=Path("/tmp"),  # nosec B108 - test fixture using temp directory
            ),
            patch("cli.stacks.StackManager._find_cdk", return_value="npx cdk"),
        ):
            return __import__("cli.stacks", fromlist=["StackManager"]).StackManager(config)

    @patch("boto3.client")
    def test_bootstrapped_active_stack(self, mock_boto_client):
        """CDKToolkit stack exists with CREATE_COMPLETE → True."""
        cf = MagicMock()
        mock_boto_client.return_value = cf
        cf.describe_stacks.return_value = {"Stacks": [{"StackStatus": "CREATE_COMPLETE"}]}
        mgr = self._make_manager()
        assert mgr.is_bootstrapped("us-east-1") is True
        mock_boto_client.assert_called_with("cloudformation", region_name="us-east-1")

    @patch("boto3.client")
    def test_bootstrapped_update_complete(self, mock_boto_client):
        """CDKToolkit stack with UPDATE_COMPLETE → True."""
        cf = MagicMock()
        mock_boto_client.return_value = cf
        cf.describe_stacks.return_value = {"Stacks": [{"StackStatus": "UPDATE_COMPLETE"}]}
        mgr = self._make_manager()
        assert mgr.is_bootstrapped("eu-west-1") is True

    @patch("boto3.client")
    def test_not_bootstrapped_stack_not_found(self, mock_boto_client):
        """describe_stacks raises exception (stack not found) → False."""
        cf = MagicMock()
        mock_boto_client.return_value = cf
        cf.describe_stacks.side_effect = Exception("Stack not found")
        mgr = self._make_manager()
        assert mgr.is_bootstrapped("ap-southeast-1") is False

    @patch("boto3.client")
    def test_not_bootstrapped_delete_complete(self, mock_boto_client):
        """CDKToolkit stack with DELETE_COMPLETE → False."""
        cf = MagicMock()
        mock_boto_client.return_value = cf
        cf.describe_stacks.return_value = {"Stacks": [{"StackStatus": "DELETE_COMPLETE"}]}
        mgr = self._make_manager()
        assert mgr.is_bootstrapped("us-west-2") is False

    @patch("boto3.client")
    def test_not_bootstrapped_delete_in_progress(self, mock_boto_client):
        """CDKToolkit stack with DELETE_IN_PROGRESS → False."""
        cf = MagicMock()
        mock_boto_client.return_value = cf
        cf.describe_stacks.return_value = {
            "Stacks": [{"StackStatus": "DELETE_IN_PROGRESS"}],
        }
        mgr = self._make_manager()
        assert mgr.is_bootstrapped("us-west-2") is False

    @patch("boto3.client")
    def test_not_bootstrapped_empty_stacks(self, mock_boto_client):
        """describe_stacks returns empty list → False."""
        cf = MagicMock()
        mock_boto_client.return_value = cf
        cf.describe_stacks.return_value = {"Stacks": []}
        mgr = self._make_manager()
        assert mgr.is_bootstrapped("us-east-2") is False


class TestEnsureBootstrapped:
    """Tests for StackManager.ensure_bootstrapped()."""

    def _make_manager(self):
        config = MagicMock()
        with (
            patch(
                "cli.stacks.StackManager._find_project_root",
                return_value=Path("/tmp"),  # nosec B108 - test fixture using temp directory
            ),
            patch("cli.stacks.StackManager._find_cdk", return_value="npx cdk"),
        ):
            return __import__("cli.stacks", fromlist=["StackManager"]).StackManager(config)

    def test_already_bootstrapped_skips(self):
        """If is_bootstrapped returns True, bootstrap is not called."""
        mgr = self._make_manager()
        mgr.is_bootstrapped = MagicMock(return_value=True)
        mgr.bootstrap = MagicMock()

        result = mgr.ensure_bootstrapped("us-east-1")
        assert result is True
        mgr.is_bootstrapped.assert_called_once_with("us-east-1")
        mgr.bootstrap.assert_not_called()

    def test_not_bootstrapped_bootstrap_succeeds(self):
        """If not bootstrapped, calls bootstrap and returns True on success."""
        mgr = self._make_manager()
        mgr.is_bootstrapped = MagicMock(return_value=False)
        mgr.bootstrap = MagicMock(return_value=True)

        result = mgr.ensure_bootstrapped("ap-southeast-1")
        assert result is True
        mgr.bootstrap.assert_called_once_with(region="ap-southeast-1")

    def test_not_bootstrapped_bootstrap_fails(self):
        """If not bootstrapped and bootstrap fails, returns False."""
        mgr = self._make_manager()
        mgr.is_bootstrapped = MagicMock(return_value=False)
        mgr.bootstrap = MagicMock(return_value=False)

        result = mgr.ensure_bootstrapped("ap-southeast-1")
        assert result is False
        mgr.bootstrap.assert_called_once_with(region="ap-southeast-1")


class TestGetDeployRegion:
    """Tests for StackManager._get_deploy_region()."""

    def _make_manager(self):
        config = MagicMock()
        config.global_region = "us-east-2"
        config.api_gateway_region = "us-east-1"
        config.monitoring_region = "us-east-2"
        with (
            patch(
                "cli.stacks.StackManager._find_project_root",
                return_value=Path("/tmp"),  # nosec B108 - test fixture using temp directory
            ),
            patch("cli.stacks.StackManager._find_cdk", return_value="npx cdk"),
        ):
            return __import__("cli.stacks", fromlist=["StackManager"]).StackManager(config)

    @patch("cli.config._load_cdk_json", return_value={})
    def test_global_stack_uses_config(self, _mock_cdk):
        """gco-global → config.global_region when cdk.json has no override."""
        mgr = self._make_manager()
        assert mgr._get_deploy_region("gco-global") == "us-east-2"

    @patch("cli.config._load_cdk_json", return_value={"global": "eu-central-1"})
    def test_global_stack_cdk_json_override(self, _mock_cdk):
        """gco-global → cdk.json global region when set."""
        mgr = self._make_manager()
        assert mgr._get_deploy_region("gco-global") == "eu-central-1"

    @patch("cli.config._load_cdk_json", return_value={})
    def test_api_gateway_stack(self, _mock_cdk):
        """gco-api-gateway → config.api_gateway_region."""
        mgr = self._make_manager()
        assert mgr._get_deploy_region("gco-api-gateway") == "us-east-1"

    @patch("cli.config._load_cdk_json", return_value={})
    def test_monitoring_stack(self, _mock_cdk):
        """gco-monitoring → config.monitoring_region."""
        mgr = self._make_manager()
        assert mgr._get_deploy_region("gco-monitoring") == "us-east-2"

    @patch("cli.config._load_cdk_json", return_value={})
    def test_regional_stack_us_east_1(self, _mock_cdk):
        """gco-us-east-1 → us-east-1."""
        mgr = self._make_manager()
        assert mgr._get_deploy_region("gco-us-east-1") == "us-east-1"

    @patch("cli.config._load_cdk_json", return_value={})
    def test_regional_stack_eu_west_1(self, _mock_cdk):
        """gco-eu-west-1 → eu-west-1."""
        mgr = self._make_manager()
        assert mgr._get_deploy_region("gco-eu-west-1") == "eu-west-1"

    @patch("cli.config._load_cdk_json", return_value={})
    def test_regional_stack_ap_southeast_1(self, _mock_cdk):
        """gco-ap-southeast-1 → ap-southeast-1."""
        mgr = self._make_manager()
        assert mgr._get_deploy_region("gco-ap-southeast-1") == "ap-southeast-1"

    @patch("cli.config._load_cdk_json", return_value={})
    def test_unknown_stack_returns_none(self, _mock_cdk):
        """Unrecognized stack name without gco- prefix → None."""
        mgr = self._make_manager()
        assert mgr._get_deploy_region("some-other-stack") is None


class TestDeployCallsEnsureBootstrapped:
    """Tests that deploy() integrates with ensure_bootstrapped correctly."""

    def _make_manager(self):
        config = MagicMock()
        config.global_region = "us-east-2"
        with (
            patch(
                "cli.stacks.StackManager._find_project_root",
                return_value=Path("/tmp"),  # nosec B108 - test fixture using temp directory
            ),
            patch("cli.stacks.StackManager._find_cdk", return_value="npx cdk"),
        ):
            return __import__("cli.stacks", fromlist=["StackManager"]).StackManager(config)

    @patch("cli.stacks._detect_container_runtime", return_value="docker")
    @patch("cli.config._load_cdk_json", return_value={})
    def test_deploy_calls_ensure_bootstrapped(self, _mock_cdk, _mock_runtime):
        """deploy() calls ensure_bootstrapped with the resolved region."""
        mgr = self._make_manager()
        mgr._sync_lambda_sources = MagicMock()
        mgr.ensure_bootstrapped = MagicMock(return_value=True)
        mgr._run_cdk = MagicMock(return_value=MagicMock(returncode=0))
        mgr._get_stack_status = MagicMock(return_value="CREATE_COMPLETE")

        mgr.deploy(stack_name="gco-global", require_approval=False)
        mgr.ensure_bootstrapped.assert_called_once_with("us-east-2")

    @patch("cli.stacks._detect_container_runtime", return_value="docker")
    @patch("cli.config._load_cdk_json", return_value={})
    def test_deploy_raises_on_bootstrap_failure(self, _mock_cdk, _mock_runtime):
        """deploy() raises RuntimeError when ensure_bootstrapped returns False."""
        import pytest

        mgr = self._make_manager()
        mgr._sync_lambda_sources = MagicMock()
        mgr.ensure_bootstrapped = MagicMock(return_value=False)

        with pytest.raises(RuntimeError, match="could not be bootstrapped"):
            mgr.deploy(stack_name="gco-global", require_approval=False)

    @patch("cli.stacks._detect_container_runtime", return_value="docker")
    def test_deploy_skips_bootstrap_when_no_stack_name(self, _mock_runtime):
        """deploy() with all_stacks=True skips bootstrap check."""
        mgr = self._make_manager()
        mgr._sync_lambda_sources = MagicMock()
        mgr.ensure_bootstrapped = MagicMock()
        mgr._run_cdk = MagicMock(return_value=MagicMock(returncode=0))

        mgr.deploy(all_stacks=True, require_approval=False)
        mgr.ensure_bootstrapped.assert_not_called()


# ---------------------------------------------------------------------------
# Timeout + CloudFormation reconciliation
# ---------------------------------------------------------------------------
#
# `cdk destroy` and `cdk deploy` can hang in their post-action polling loops
# even after CloudFormation has already finished the underlying delete or
# create. The orchestrator now caps each cdk subprocess at a wall-clock
# budget (default 45 min for destroy, 60 min for deploy, env-tunable) and
# reconciles against `DescribeStacks` so a hung cdk doesn't block the
# orchestrator forever.


class TestDestroyTimeoutAndReconciliation:
    def test_destroy_passes_timeout_to_run_cdk_with_default_budget(self):
        """``destroy()`` must pass the default 45-minute timeout to
        ``_run_cdk`` so a wedged cdk subprocess can't run forever."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=False),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            manager = StackManager(config)
            assert manager.destroy("gco-monitoring", force=True) is True

        assert "timeout" in mock_run.call_args.kwargs
        # Default is 2700s (45 min).
        assert mock_run.call_args.kwargs["timeout"] == 2700.0

    def test_destroy_timeout_env_override(self, monkeypatch):
        """``GCO_CDK_DESTROY_TIMEOUT_SECONDS`` overrides the default."""
        import subprocess

        from cli.stacks import StackManager

        monkeypatch.setenv("GCO_CDK_DESTROY_TIMEOUT_SECONDS", "120")
        config = MagicMock()

        with (
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=False),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            manager = StackManager(config)
            manager.destroy("gco-monitoring", force=True)

        assert mock_run.call_args.kwargs["timeout"] == 120.0
        # Sanity: subprocess module imported at module scope (used by
        # the TimeoutExpired catch).
        assert subprocess.TimeoutExpired is not None

    def test_destroy_treats_missing_stack_as_success_after_cdk_failure(self):
        """If cdk returns non-zero but the stack is already gone in CFN,
        the destroy succeeded and we treat the cdk exit as a false alarm."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=False),
        ):
            mock_run.return_value = MagicMock(returncode=1)
            manager = StackManager(config)
            assert manager.destroy("gco-us-east-1", force=True) is True

    def test_destroy_treats_missing_stack_as_success_after_timeout(self):
        """Same reconciliation when cdk hangs and we kill it: if the
        stack is gone in CFN, the destroy succeeded."""
        import subprocess

        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=False),
        ):
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["cdk"], timeout=2700)
            manager = StackManager(config)
            assert manager.destroy("gco-us-east-1", force=True) is True

    def test_destroy_falls_back_to_cfn_delete_when_cdk_succeeded_but_stack_remains(self):
        """If cdk exits 0 but the stack is still present (rare CDK bug),
        fall back to a direct CloudFormation delete."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=True),
            patch.object(
                StackManager, "_cloudformation_delete_stack", return_value=True
            ) as mock_cfn,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            manager = StackManager(config)
            assert manager.destroy("gco-us-east-1", force=True) is True
        mock_cfn.assert_called_once_with("gco-us-east-1")

    def test_destroy_returns_false_when_cdk_fails_and_stack_remains(self):
        """The actual failure case: cdk exits non-zero AND stack is still
        in CloudFormation — propagate as failure."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=True),
        ):
            mock_run.return_value = MagicMock(returncode=1)
            manager = StackManager(config)
            assert manager.destroy("gco-us-east-1", force=True) is False


class TestDeployTimeoutAndReconciliation:
    def test_deploy_passes_timeout_to_run_cdk_with_default_budget(self):
        """``deploy()`` must pass the default 60-minute timeout."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "_check_and_fix_stuck_stack"),
            patch.object(StackManager, "ensure_bootstrapped", return_value=True),
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_get_stack_status", return_value="UPDATE_COMPLETE"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            manager = StackManager(config)
            assert manager.deploy("gco-global", require_approval=False) is True

        assert "timeout" in mock_run.call_args.kwargs
        # Default is 3600s (60 min).
        assert mock_run.call_args.kwargs["timeout"] == 3600.0

    def test_deploy_timeout_env_override(self, monkeypatch):
        from cli.stacks import StackManager

        monkeypatch.setenv("GCO_CDK_DEPLOY_TIMEOUT_SECONDS", "300")
        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "_check_and_fix_stuck_stack"),
            patch.object(StackManager, "ensure_bootstrapped", return_value=True),
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_get_stack_status", return_value="UPDATE_COMPLETE"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            manager = StackManager(config)
            manager.deploy("gco-global", require_approval=False)

        assert mock_run.call_args.kwargs["timeout"] == 300.0

    def test_deploy_treats_complete_stack_status_as_success_after_cdk_failure(self):
        """If cdk exits non-zero but CFN says CREATE_COMPLETE, the deploy
        actually succeeded — cdk's polling loop just gave up early."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "_check_and_fix_stuck_stack"),
            patch.object(StackManager, "ensure_bootstrapped", return_value=True),
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_get_stack_status", return_value="CREATE_COMPLETE"),
            patch.object(StackManager, "_diagnose_deploy_failure"),
        ):
            mock_run.return_value = MagicMock(returncode=1)
            manager = StackManager(config)
            assert manager.deploy("gco-global", require_approval=False) is True

    def test_deploy_treats_update_complete_as_success_after_timeout(self):
        """Same reconciliation after a cdk timeout."""
        import subprocess

        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "_check_and_fix_stuck_stack"),
            patch.object(StackManager, "ensure_bootstrapped", return_value=True),
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_get_stack_status", return_value="UPDATE_COMPLETE"),
            patch.object(StackManager, "_diagnose_deploy_failure"),
        ):
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["cdk"], timeout=3600)
            manager = StackManager(config)
            assert manager.deploy("gco-global", require_approval=False) is True

    def test_deploy_returns_false_when_cdk_fails_and_status_is_not_complete(self):
        """When cdk fails AND CFN reports a non-complete status (or the
        lookup itself fails), the deploy is a real failure."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "_check_and_fix_stuck_stack"),
            patch.object(StackManager, "ensure_bootstrapped", return_value=True),
            patch.object(StackManager, "_run_cdk") as mock_run,
            # ROLLBACK_COMPLETE is not a success state.
            patch.object(StackManager, "_get_stack_status", return_value="ROLLBACK_COMPLETE"),
            patch.object(StackManager, "_diagnose_deploy_failure"),
        ):
            mock_run.return_value = MagicMock(returncode=1)
            manager = StackManager(config)
            assert manager.deploy("gco-global", require_approval=False) is False

    def test_deploy_returns_false_when_status_lookup_returns_none(self):
        """When _get_stack_status returns None (stack doesn't exist or
        the lookup itself failed), cdk's verdict stands."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "_check_and_fix_stuck_stack"),
            patch.object(StackManager, "ensure_bootstrapped", return_value=True),
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_get_stack_status", return_value=None),
            patch.object(StackManager, "_diagnose_deploy_failure"),
        ):
            mock_run.return_value = MagicMock(returncode=1)
            manager = StackManager(config)
            assert manager.deploy("gco-global", require_approval=False) is False


class TestRunCdkTimeout:
    def test_run_cdk_no_timeout_default(self):
        """Without an explicit timeout, _run_cdk doesn't pass one
        through (preserves existing behaviour for synth / list)."""
        from cli.stacks import StackManager

        config = MagicMock()
        manager = StackManager(config)

        with patch("cli.stacks.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            manager._run_cdk(["list"], capture_output=True)

        assert mock_run.call_args.kwargs.get("timeout") is None

    def test_run_cdk_propagates_timeout_kwarg(self):
        """When ``timeout`` is set, it reaches subprocess.run."""
        from cli.stacks import StackManager

        config = MagicMock()
        manager = StackManager(config)

        with patch("cli.stacks.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            manager._run_cdk(["list"], capture_output=True, timeout=42.0)

        assert mock_run.call_args.kwargs["timeout"] == 42.0

    def test_run_cdk_re_raises_timeout_expired(self):
        """When subprocess.run raises TimeoutExpired, _run_cdk re-raises
        so callers can verify post-state via CloudFormation."""
        import subprocess

        from cli.stacks import StackManager

        config = MagicMock()
        manager = StackManager(config)

        with patch("cli.stacks.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["cdk"], timeout=10)
            with pytest.raises(subprocess.TimeoutExpired):
                manager._run_cdk(["destroy"], timeout=10.0)


class TestGetStackStatus:
    def test_get_stack_status_returns_status(self):
        """Successful describe_stacks → returns the status string."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.api_gateway_region = "us-east-2"

        fake_cfn = MagicMock()
        fake_cfn.describe_stacks.return_value = {"Stacks": [{"StackStatus": "UPDATE_COMPLETE"}]}

        with patch("boto3.client", return_value=fake_cfn):
            manager = StackManager(config)
            assert manager._get_stack_status("gco-global") == "UPDATE_COMPLETE"

    def test_get_stack_status_returns_none_on_error(self):
        """describe_stacks raises (stack doesn't exist, perms, network) →
        return None so callers fall back to cdk's verdict."""
        from cli.stacks import StackManager

        config = MagicMock()
        config.api_gateway_region = "us-east-2"

        fake_cfn = MagicMock()
        fake_cfn.describe_stacks.side_effect = RuntimeError("not found")

        with patch("boto3.client", return_value=fake_cfn):
            manager = StackManager(config)
            assert manager._get_stack_status("gco-nonexistent") is None


# ---------------------------------------------------------------------------
# CDK synthesis assertion: the missions DynamoDB table on GCOGlobalStack
# ---------------------------------------------------------------------------
#
# The mission iteration loop persists session state in a DynamoDB table
# named ``<project>-missions`` with a ``status-index`` GSI for paginated
# listing by status. This class synthesizes ``GCOGlobalStack`` against
# the same ``MockConfigLoader`` fixture used by ``test_regional_stack.py``
# and asserts the table + GSI surface in the synthesized CloudFormation
# template.


class TestGlobalStackMissionsTable:
    """Synthesis-level assertions for the ``MissionsTable`` resource."""

    def test_global_stack_creates_missions_table_with_status_index(self):
        """``GCOGlobalStack`` synthesizes a DynamoDB table named
        ``<project>-missions`` with a GSI named ``status-index``."""
        import aws_cdk as cdk
        from aws_cdk import assertions

        from gco.stacks.global_stack import GCOGlobalStack
        from tests.test_regional_stack import MockConfigLoader

        app = cdk.App()
        config = MockConfigLoader(app)
        stack = GCOGlobalStack(app, "test-synth-missions", config=config)

        template = assertions.Template.from_stack(stack)
        # ``MockConfigLoader.get_project_name()`` returns ``"gco-test"``,
        # so the missions table's TableName is ``"gco-test-missions"``.
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "TableName": f"{config.get_project_name()}-missions",
                "GlobalSecondaryIndexes": assertions.Match.array_with(
                    [
                        assertions.Match.object_like(
                            {"IndexName": "status-index"},
                        ),
                    ],
                ),
            },
        )
