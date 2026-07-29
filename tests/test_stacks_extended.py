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
import signal
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest


class _FakePopenProcess:
    """Deterministic ``Popen`` process used by direct ``_run_cdk`` tests."""

    def __init__(
        self,
        *,
        pid=4242,
        returncode=0,
        stdout=None,
        stderr=None,
        communicate_error=None,
        wait_errors=(),
    ):
        self.pid = pid
        self.returncode = None
        self._completed_returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._communicate_error = communicate_error
        self._wait_errors = list(wait_errors)
        self.communicate_timeouts = []
        self.poll_calls = 0
        self.wait_timeouts = []
        self.terminate_calls = 0
        self.kill_calls = 0

    def communicate(self, timeout=None):
        self.communicate_timeouts.append(timeout)
        if self._communicate_error is not None:
            raise self._communicate_error
        self.returncode = self._completed_returncode
        return self._stdout, self._stderr

    def poll(self):
        self.poll_calls += 1
        return self.returncode

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if self._wait_errors:
            raise self._wait_errors.pop(0)
        self.returncode = self._completed_returncode
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1

    def kill(self):
        self.kill_calls += 1


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

    @pytest.fixture(autouse=True)
    def isolate_named_deploy_boundaries(self):
        """Stub AWS-facing preflight boundaries outside these argv tests."""
        from cli.stacks import StackManager

        with (
            patch.object(StackManager, "_get_deploy_region", return_value="us-east-1"),
            patch.object(StackManager, "ensure_bootstrapped", return_value=True),
            patch.object(StackManager, "_check_and_fix_stuck_stack"),
            patch.object(StackManager, "_mirror_images_if_enabled"),
            patch.object(StackManager, "_get_stack_status", return_value=None),
            patch.object(StackManager, "_diagnose_deploy_failure"),
        ):
            yield

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
        """Popen receives merged custom environment and captured text pipes."""
        from cli.stacks import StackManager

        config = MagicMock()
        manager = StackManager(config)
        manager._cdk_path = "cdk"
        process = _FakePopenProcess(stdout="output", stderr="")

        with (
            patch.object(manager, "_ensure_cdk_toolchain"),
            patch.object(manager, "_ensure_lambda_build"),
            patch("cli.stacks.subprocess.Popen", return_value=process) as mock_popen,
        ):
            result = manager._run_cdk(
                ["list"],
                capture_output=True,
                env={"CUSTOM_VAR": "value"},
            )

        assert result.returncode == 0
        assert result.stdout == "output"
        assert mock_popen.call_args.args[0] == ["cdk", "list"]
        call_kwargs = mock_popen.call_args.kwargs
        assert call_kwargs["cwd"] == manager.project_root
        assert call_kwargs["stdout"] is subprocess.PIPE
        assert call_kwargs["stderr"] is subprocess.PIPE
        assert call_kwargs["text"] is True
        assert call_kwargs["env"]["CUSTOM_VAR"] == "value"
        assert call_kwargs["env"]["PYTHONPATH"]
        assert call_kwargs["start_new_session"] is (os.name == "posix")
        assert process.communicate_timeouts == [None]

    def test_run_cdk_without_capture(self):
        """Popen inherits output streams when capture is disabled."""
        from cli.stacks import StackManager

        config = MagicMock()
        manager = StackManager(config)
        manager._cdk_path = "cdk"
        process = _FakePopenProcess()

        with (
            patch.object(manager, "_ensure_cdk_toolchain"),
            patch.object(manager, "_ensure_lambda_build"),
            patch("cli.stacks.subprocess.Popen", return_value=process) as mock_popen,
        ):
            result = manager._run_cdk(["deploy"], capture_output=False)

        assert result.returncode == 0
        assert mock_popen.call_args.args[0] == ["cdk", "deploy"]
        call_kwargs = mock_popen.call_args.kwargs
        assert call_kwargs["stdout"] is None
        assert call_kwargs["stderr"] is None
        assert call_kwargs["start_new_session"] is (os.name == "posix")
        assert process.communicate_timeouts == [None]


class TestFindCdkExecutable:
    """Tests for finding CDK executable."""

    def test_find_cdk_in_common_location(self):
        """Test finding CDK in common location."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("pathlib.Path.is_file", return_value=False),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.side_effect = subprocess.CalledProcessError(1, "which")

            with patch("os.path.exists") as mock_exists:
                mock_exists.side_effect = lambda p: p == "/usr/local/bin/cdk"

                manager = StackManager(config)
                assert manager._cdk_path is None
                assert manager._find_cdk() == "/usr/local/bin/cdk"


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
        with patch(
            "cli.stacks.StackManager._find_project_root",
            return_value=Path("/tmp"),  # nosec B108 - test fixture using temp directory
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


class TestStrictBootstrapValidation:
    """Strict mode reuses only the exact preflighted CDKToolkit stack."""

    _STACK_ID = "arn:aws:cloudformation:us-east-1:123456789012:stack/CDKToolkit/toolkit-uuid"

    def _make_manager(self):
        config = MagicMock()
        with patch(
            "cli.stacks.StackManager._find_project_root",
            return_value=Path("/tmp"),  # nosec B108 - test fixture using temp directory
        ):
            return __import__("cli.stacks", fromlist=["StackManager"]).StackManager(config)

    @patch("boto3.client")
    def test_accepts_exact_healthy_identity(self, mock_boto_client):
        cfn = MagicMock()
        mock_boto_client.return_value = cfn
        cfn.describe_stacks.return_value = {
            "Stacks": [
                {
                    "StackName": "CDKToolkit",
                    "StackId": self._STACK_ID,
                    "StackStatus": "CREATE_COMPLETE",
                }
            ]
        }
        manager = self._make_manager()

        manager._validate_bootstrap_stack(
            "us-east-1",
            {"stack_id": self._STACK_ID, "status": "CREATE_COMPLETE"},
        )

        cfn.describe_stacks.assert_called_once_with(StackName=self._STACK_ID)

    @pytest.mark.parametrize(
        ("stack_name", "stack_id", "status", "message"),
        (
            ("Replacement", _STACK_ID, "CREATE_COMPLETE", "identity changed"),
            (
                "CDKToolkit",
                _STACK_ID.replace("toolkit-uuid", "replacement"),
                "CREATE_COMPLETE",
                "identity changed",
            ),
            ("CDKToolkit", _STACK_ID, "UPDATE_COMPLETE", "status changed"),
        ),
    )
    @patch("boto3.client")
    def test_rejects_changed_identity_or_status(
        self,
        mock_boto_client,
        stack_name,
        stack_id,
        status,
        message,
    ):
        cfn = MagicMock()
        mock_boto_client.return_value = cfn
        cfn.describe_stacks.return_value = {
            "Stacks": [
                {
                    "StackName": stack_name,
                    "StackId": stack_id,
                    "StackStatus": status,
                }
            ]
        }
        manager = self._make_manager()

        with pytest.raises(RuntimeError, match=message):
            manager._validate_bootstrap_stack(
                "us-east-1",
                {"stack_id": self._STACK_ID, "status": "CREATE_COMPLETE"},
            )


class TestEnsureBootstrapped:
    """Tests for StackManager.ensure_bootstrapped()."""

    def _make_manager(self):
        config = MagicMock()
        with patch(
            "cli.stacks.StackManager._find_project_root",
            return_value=Path("/tmp"),  # nosec B108 - test fixture using temp directory
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
        config.project_name = "gco"
        config.global_region = "us-east-2"
        config.api_gateway_region = "us-east-1"
        config.monitoring_region = "us-east-2"
        with patch(
            "cli.stacks.StackManager._find_project_root",
            return_value=Path("/tmp"),  # nosec B108 - test fixture using temp directory
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

    @patch(
        "cli.config._load_cdk_json",
        return_value={"regional": ["us-east-1", "eu-west-1"]},
    )
    def test_regional_api_bridge_uses_configured_aws_region(self, _mock_cdk):
        """Bridge IDs resolve to their AWS region, not regional-api-<region>."""
        mgr = self._make_manager()
        assert mgr._get_deploy_region("gco-regional-api-us-east-1") == "us-east-1"
        assert mgr._get_destroy_region("gco-regional-api-eu-west-1") == "eu-west-1"

    @patch(
        "cli.config._load_cdk_json",
        return_value={"regional": ["us-east-1"]},
    )
    def test_regional_api_bridge_rejects_unconfigured_region(self, _mock_cdk):
        """Bridge-shaped typos cannot become malformed CDK/AWS regions."""
        mgr = self._make_manager()
        assert mgr._get_deploy_region("gco-regional-api-us-west-2") is None

    @patch(
        "cli.config._load_cdk_json",
        return_value={"regional": ["us-east-1"]},
    )
    def test_destroy_resolves_unconfigured_orphan_bridge_region(self, _mock_cdk, tmp_path):
        """Destroy still finds an orphan bridge after its Region leaves config."""
        (tmp_path / "cdk.json").write_text(
            json.dumps(
                {
                    "context": {
                        "project_name": "gco",
                        "deployment_regions": {
                            "global": "us-east-2",
                            "api_gateway": "us-east-1",
                            "monitoring": "us-east-2",
                            "regional": ["us-east-1"],
                        },
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        mgr = self._make_manager()
        mgr.project_root = tmp_path
        with patch(
            "cli.stacks._known_cloudformation_regions",
            return_value=frozenset({"us-east-1", "us-east-2", "us-west-2"}),
        ):
            assert mgr._get_deploy_region("gco-regional-api-us-west-2") is None
            assert mgr._get_destroy_region("gco-regional-api-us-west-2") == "us-west-2"

    @patch(
        "cli.config._load_cdk_json",
        return_value={"regional": ["us-east-1"]},
    )
    def test_destroy_rejects_bridge_with_non_region_suffix(self, _mock_cdk):
        """An exact project bridge prefix cannot turn ``bar`` into a Region."""
        mgr = self._make_manager()
        mgr.config.project_name = "foo"
        with patch(
            "cli.stacks._known_cloudformation_regions",
            return_value=frozenset({"us-east-1", "us-west-2"}),
        ):
            assert mgr._get_deploy_region("foo-regional-api-bar") is None
            assert mgr._get_destroy_region("foo-regional-api-bar") == "us-east-1"

    @patch(
        "cli.config._load_cdk_json",
        return_value={"regional": ["us-east-1"]},
    )
    def test_bridge_resolution_handles_project_containing_marker(self, _mock_cdk):
        """An embedded regional-api marker in project_name stays unambiguous."""
        mgr = self._make_manager()
        mgr.config.project_name = "foo-regional-api-bar"
        assert mgr._get_deploy_region("foo-regional-api-bar-us-east-1") == "us-east-1"
        assert mgr._get_deploy_region("foo-regional-api-bar-regional-api-us-east-1") == "us-east-1"

    @patch("cli.config._load_cdk_json", return_value={})
    def test_unknown_stack_returns_none(self, _mock_cdk):
        """Unrecognized stack name without gco- prefix → None."""
        mgr = self._make_manager()
        assert mgr._get_deploy_region("some-other-stack") is None


class TestStrictDeployStackOwnership:
    """Strict deploy mode never adopts a same-name stack without a checkpointed ARN."""

    _STACK_ID = "arn:aws:cloudformation:us-east-1:123456789012:stack/gco-live-global/stack-uuid"

    def test_rejects_uncheckpointed_healthy_same_name_stack(self):
        from cli.stacks import StackManager

        manager = StackManager(MagicMock())
        cloudformation = MagicMock()
        cloudformation.describe_stacks.return_value = {
            "Stacks": [
                {
                    "StackName": "gco-live-global",
                    "StackId": self._STACK_ID,
                    "StackStatus": "CREATE_COMPLETE",
                }
            ]
        }

        with (
            patch.object(manager, "_get_deploy_region", return_value="us-east-1"),
            patch("boto3.client", return_value=cloudformation),
            pytest.raises(RuntimeError, match="Refusing to adopt uncheckpointed stack"),
        ):
            manager._check_and_fix_stuck_stack(
                "gco-live-global",
                strict_ownership=True,
            )

        cloudformation.delete_stack.assert_not_called()


class TestDeployCallsEnsureBootstrapped:
    """Tests that deploy() integrates with ensure_bootstrapped correctly."""

    def _make_manager(self):
        config = MagicMock()
        config.global_region = "us-east-2"
        with patch(
            "cli.stacks.StackManager._find_project_root",
            return_value=Path("/tmp"),  # nosec B108 - test fixture using temp directory
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
# budget (default 90 min for destroy, 60 min for deploy, env-tunable) and
# reconciles against `DescribeStacks` so a hung cdk doesn't block the
# orchestrator forever.


class TestDestroyTimeoutAndReconciliation:
    @staticmethod
    def _write_root_config(
        tmp_path: Path,
        regional: list[str],
        control_region: str = "us-east-1",
    ) -> None:
        (tmp_path / "cdk.json").write_text(
            json.dumps(
                {
                    "context": {
                        "project_name": "acme",
                        "deployment_regions": {
                            "global": control_region,
                            "api_gateway": control_region,
                            "monitoring": control_region,
                            "regional": regional,
                        },
                    }
                }
            ),
            encoding="utf-8",
        )

    def test_destroy_directly_deletes_valid_unconfigured_bridge(self, tmp_path):
        """A valid root config can prove a bridge was removed from the app."""
        from cli.stacks import StackManager

        self._write_root_config(tmp_path, ["us-east-1"])
        config = MagicMock(project_name="acme", api_gateway_region="us-east-1")
        with (
            patch(
                "cli.config._load_cdk_json",
                return_value={"regional": ["us-east-1"]},
            ),
            patch(
                "cli.stacks._known_cloudformation_regions",
                return_value=frozenset({"us-east-1", "us-west-2"}),
            ),
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=True),
            patch.object(
                StackManager,
                "_cloudformation_delete_stack",
                return_value=True,
            ) as mock_delete,
        ):
            manager = StackManager(config, project_root=tmp_path)
            assert manager.destroy("acme-regional-api-us-west-2", force=True) is True

        mock_run.assert_not_called()
        mock_delete.assert_called_once_with(
            "acme-regional-api-us-west-2",
            expected_stack_id=None,
            authorize_stack=None,
        )

    @pytest.mark.parametrize(
        ("control_region", "configured_region", "candidate_region"),
        (
            ("us-east-1", "us-west-2", "cn-north-1"),
            ("cn-north-1", "cn-northwest-1", "us-west-2"),
        ),
    )
    def test_destroy_never_probes_a_cross_partition_orphan_candidate(
        self,
        tmp_path,
        control_region,
        configured_region,
        candidate_region,
    ):
        """A coherent deployment cannot authorize a bridge in another partition."""
        from cli.stacks import StackManager

        self._write_root_config(
            tmp_path,
            [configured_region],
            control_region=control_region,
        )
        config = MagicMock(project_name="acme", api_gateway_region=control_region)
        stack_name = f"acme-regional-api-{candidate_region}"
        partition = "aws-cn" if control_region.startswith("cn-") else "aws"
        stack_id = (
            f"arn:{partition}:cloudformation:{control_region}:123456789012:"
            f"stack/{stack_name}/stack-id"
        )
        cloudformation = MagicMock()
        cloudformation.describe_stacks.return_value = {
            "Stacks": [
                {
                    "StackName": stack_name,
                    "StackId": stack_id,
                    "StackStatus": "UPDATE_COMPLETE",
                }
            ]
        }
        with (
            patch(
                "cli.config._load_cdk_json",
                return_value={"regional": [configured_region]},
            ),
            patch(
                "cli.stacks._known_cloudformation_regions",
                return_value=frozenset(
                    {
                        "us-east-1",
                        "us-west-2",
                        "cn-north-1",
                        "cn-northwest-1",
                    }
                ),
            ),
            patch.object(
                StackManager,
                "_run_cdk",
                return_value=MagicMock(returncode=1),
            ) as mock_run,
            patch("boto3.client", return_value=cloudformation) as cloudformation_client,
            patch.object(StackManager, "_cloudformation_delete_stack") as direct_delete,
        ):
            manager = StackManager(config, project_root=tmp_path)
            assert manager.destroy(stack_name, force=True) is False

        mock_run.assert_called_once()
        assert cloudformation_client.call_args_list == [
            call("cloudformation", region_name=control_region),
            call("cloudformation", region_name=control_region),
        ]
        direct_delete.assert_not_called()

    def test_destroy_fails_closed_for_mixed_partition_root_config(self, tmp_path):
        """Mixed-partition config cannot authorize direct orphan deletion."""
        from cli.stacks import StackManager

        self._write_root_config(tmp_path, ["cn-north-1"])
        config = MagicMock(project_name="acme", api_gateway_region="us-east-1")
        with (
            patch(
                "cli.config._load_cdk_json",
                return_value={"regional": ["cn-north-1"]},
            ),
            patch(
                "cli.stacks._known_cloudformation_regions",
                return_value=frozenset({"us-east-1", "us-west-2", "cn-north-1"}),
            ),
            patch(
                "cli.stacks.StackManager._run_cdk",
                return_value=MagicMock(returncode=1),
            ) as mock_run,
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=True),
            patch.object(StackManager, "_cloudformation_delete_stack") as mock_delete,
        ):
            manager = StackManager(config, project_root=tmp_path)
            assert manager.destroy("acme-regional-api-us-west-2", force=True) is False

        mock_run.assert_called_once()
        mock_delete.assert_not_called()

    @pytest.mark.parametrize(
        "stack_name",
        ("acme-regional-api-bar", "acme-regional-api-us-east-1"),
    )
    def test_destroy_does_not_bypass_cdk_for_invalid_or_configured_bridge(
        self, stack_name, tmp_path
    ):
        """Only an SDK-known bridge absent from valid config bypasses CDK."""
        from cli.stacks import StackManager

        self._write_root_config(tmp_path, ["us-east-1"])
        config = MagicMock(project_name="acme", api_gateway_region="us-east-1")
        with (
            patch(
                "cli.config._load_cdk_json",
                return_value={"regional": ["us-east-1"]},
            ),
            patch(
                "cli.stacks._known_cloudformation_regions",
                return_value=frozenset({"us-east-1", "us-west-2"}),
            ),
            patch.object(
                StackManager,
                "_run_cdk",
                return_value=MagicMock(returncode=1),
            ) as mock_run,
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=True),
            patch.object(StackManager, "_cloudformation_delete_stack") as mock_delete,
        ):
            manager = StackManager(config, project_root=tmp_path)
            assert manager.destroy(stack_name, force=True) is False

        mock_run.assert_called_once()
        mock_delete.assert_not_called()

    @pytest.mark.parametrize(
        "contents",
        (
            None,
            "{not-json",
            json.dumps({"context": {}}),
            json.dumps(
                {
                    "context": {
                        "deployment_regions": {
                            "global": "us-east-1",
                            "api_gateway": "us-east-1",
                            "monitoring": "us-east-1",
                            "regional": [],
                        }
                    }
                }
            ),
            json.dumps(
                {
                    "context": {
                        "project_name": "acme",
                        "deployment_regions": {"regional": "us-east-1"},
                    }
                }
            ),
            json.dumps(
                {
                    "context": {
                        "project_name": "acme",
                        "deployment_regions": {
                            "global": "us-east-1",
                            "api_gateway": "us-east-1",
                            "monitoring": "us-east-1",
                            "regional": ["unknown-1"],
                        },
                    }
                }
            ),
            json.dumps(
                {
                    "context": {
                        "project_name": "acme",
                        "deployment_regions": {
                            "global": "us-east-1",
                            "api_gateway": "us-east-1",
                            "monitoring": "us-east-1",
                            "regional": ["us-east-1", "us-east-1"],
                        },
                    }
                }
            ),
        ),
    )
    def test_orphan_detection_fails_closed_without_valid_root_config(self, contents, tmp_path):
        """Missing, malformed, and incomplete root config never authorize deletion."""
        from cli.stacks import StackManager

        if contents is not None:
            (tmp_path / "cdk.json").write_text(contents, encoding="utf-8")
        config = MagicMock(project_name="acme", api_gateway_region="us-east-1")
        manager = StackManager(config, project_root=tmp_path)
        with patch(
            "cli.stacks._known_cloudformation_regions",
            return_value=frozenset({"us-east-1", "us-west-2"}),
        ):
            assert manager._get_orphan_regional_api_region("acme-regional-api-us-west-2") is None

    def test_orphan_detection_fails_closed_for_malformed_utf8(self, tmp_path):
        """Malformed UTF-8 is not evidence that a bridge left configuration."""
        from cli.stacks import StackManager

        (tmp_path / "cdk.json").write_bytes(b"\xff\xfe")
        config = MagicMock(project_name="acme", api_gateway_region="us-east-1")
        manager = StackManager(config, project_root=tmp_path)
        with patch(
            "cli.stacks._known_cloudformation_regions",
            return_value=frozenset({"us-west-2"}),
        ):
            assert manager._get_orphan_regional_api_region("acme-regional-api-us-west-2") is None

    def test_orphan_detection_fails_closed_when_root_config_is_unreadable(self, tmp_path):
        """An I/O error is not evidence that a bridge left configuration."""
        from cli.stacks import StackManager

        self._write_root_config(tmp_path, [])
        config = MagicMock(project_name="acme", api_gateway_region="us-east-1")
        manager = StackManager(config, project_root=tmp_path)
        with (
            patch(
                "cli.stacks._known_cloudformation_regions",
                return_value=frozenset({"us-west-2"}),
            ),
            patch("pathlib.Path.read_text", side_effect=OSError("denied")),
        ):
            assert manager._get_orphan_regional_api_region("acme-regional-api-us-west-2") is None

    def test_destroy_passes_timeout_to_run_cdk_with_default_budget(self):
        """``destroy()`` must pass the default 90-minute timeout to
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
        # Default is 5400s (90 min): a healthy EKS regional teardown has
        # been observed needing ~60, so 45 marked normal deletes as wedged.
        assert mock_run.call_args.kwargs["timeout"] == 5400.0

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


class TestDestroyCloudFormationConvergence:
    """Regression coverage for AWS-side delete convergence and phase barriers."""

    def test_destroy_timeout_waits_for_active_cloudformation_delete(self):
        """A CDK timeout is not failure while CloudFormation is deleting."""
        from cli.stacks import StackManager

        config = MagicMock()
        timeout = subprocess.TimeoutExpired(cmd=["cdk"], timeout=2700)
        with (
            patch.object(StackManager, "_run_cdk", side_effect=timeout),
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=True),
            patch.object(StackManager, "_get_stack_status", return_value="DELETE_IN_PROGRESS"),
            patch.object(
                StackManager,
                "_wait_for_stack_delete_convergence",
                return_value=True,
            ) as mock_wait,
            patch.object(StackManager, "_cloudformation_delete_stack") as mock_direct_delete,
        ):
            manager = StackManager(config)
            assert manager.destroy("gco-us-east-1", force=True) is True

        mock_wait.assert_called_once_with(
            "gco-us-east-1",
            initial_status="DELETE_IN_PROGRESS",
        )
        mock_direct_delete.assert_not_called()

    def test_destroy_nonzero_exit_waits_for_active_cloudformation_delete(self):
        """A nonzero CDK exit also defers to an active AWS delete."""
        from cli.stacks import StackManager

        config = MagicMock()
        with (
            patch.object(
                StackManager,
                "_run_cdk",
                return_value=MagicMock(returncode=1),
            ),
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=True),
            patch.object(StackManager, "_get_stack_status", return_value="DELETE_IN_PROGRESS"),
            patch.object(
                StackManager,
                "_wait_for_stack_delete_convergence",
                return_value=True,
            ) as mock_wait,
        ):
            manager = StackManager(config)
            assert manager.destroy("gco-us-east-1", force=True) is True

        mock_wait.assert_called_once_with(
            "gco-us-east-1",
            initial_status="DELETE_IN_PROGRESS",
        )

    def test_destroy_delete_failed_fails_without_starting_direct_delete(self):
        """DELETE_FAILED is terminal and must not trigger a second delete."""
        from cli.stacks import StackManager

        config = MagicMock()
        with (
            patch.object(
                StackManager,
                "_run_cdk",
                return_value=MagicMock(returncode=1),
            ),
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=True),
            patch.object(StackManager, "_get_stack_status", return_value="DELETE_FAILED"),
            patch.object(StackManager, "_print_stack_delete_heartbeat") as mock_heartbeat,
            patch.object(StackManager, "_cloudformation_delete_stack") as mock_direct_delete,
        ):
            manager = StackManager(config)
            assert manager.destroy("gco-us-east-1", force=True) is False

        mock_heartbeat.assert_called_once_with(
            "gco-us-east-1",
            "DELETE_FAILED",
            None,
        )
        mock_direct_delete.assert_not_called()

    def test_delete_convergence_deadline_fails_closed(self):
        """A present stack at the AWS convergence deadline remains failure."""
        from cli.stacks import StackManager

        manager = StackManager(MagicMock())
        with (
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=True),
            patch.object(StackManager, "_print_stack_delete_heartbeat") as mock_heartbeat,
            patch("cli.stacks.time.monotonic", side_effect=[0.0, 0.0, 1.0]),
            patch("cli.stacks.time.sleep") as mock_sleep,
        ):
            assert (
                manager._wait_for_stack_delete_convergence(
                    "gco-us-east-1",
                    timeout=0.5,
                    poll_interval=0.1,
                    heartbeat_interval=0.1,
                )
                is False
            )

        mock_heartbeat.assert_called_once_with(
            "gco-us-east-1",
            "DELETE_IN_PROGRESS",
            None,
        )
        mock_sleep.assert_not_called()

    @pytest.mark.parametrize(
        ("field", "value"),
        (
            ("timeout", float("nan")),
            ("timeout", float("inf")),
            ("poll_interval", float("nan")),
            ("poll_interval", float("inf")),
            ("heartbeat_interval", float("nan")),
            ("heartbeat_interval", float("inf")),
        ),
    )
    def test_delete_convergence_rejects_non_finite_budgets(self, field, value):
        """NaN/Infinity can never disable the bounded delete deadline."""
        from cli.stacks import StackManager

        manager = StackManager(MagicMock())
        kwargs = {
            "timeout": 1.0,
            "poll_interval": 0.1,
            "heartbeat_interval": 0.1,
        }
        kwargs[field] = value
        with pytest.raises(ValueError, match="positive and finite"):
            manager._wait_for_stack_delete_convergence("gco-us-east-1", **kwargs)

    @pytest.mark.parametrize("regional_destroy_succeeded", (False, True))
    def test_regional_barrier_prevents_api_and_global_destroy(
        self,
        regional_destroy_succeeded,
    ):
        """Failure or lingering regional resources block dependent globals."""
        from cli.stacks import StackManager

        manager = StackManager(MagicMock(project_name="gco"))
        thread = MagicMock()
        stop_events = []

        def start_watchdog(_stack_name, stop_event, **_kwargs):
            stop_events.append(stop_event)
            return thread

        stacks = ["gco-global", "gco-api-gateway", "gco-us-east-1"]
        with (
            patch.object(manager, "list_stacks", return_value=stacks),
            patch.object(manager, "_image_registry_destroy_preflight", return_value=True),
            patch.object(manager, "cleanup_orphaned_bastions"),
            patch.object(manager, "_cleanup_backup_vault"),
            patch.object(manager, "_start_eks_sg_watchdog", side_effect=start_watchdog),
            patch.object(manager, "_cleanup_eks_security_groups"),
            patch.object(
                manager,
                "destroy",
                return_value=regional_destroy_succeeded,
            ) as mock_destroy,
            patch.object(manager, "_stack_exists_in_cloudformation", return_value=True),
        ):
            overall_success, _successful, failed = manager.destroy_orchestrated(force=True)

        assert overall_success is False
        assert "gco-us-east-1" in failed
        mock_destroy.assert_called_once()
        destroy_kwargs = mock_destroy.call_args.kwargs
        assert destroy_kwargs["stack_name"] == "gco-us-east-1"
        assert destroy_kwargs["force"] is True
        assert destroy_kwargs["expected_stack_id"] is None
        assert destroy_kwargs["expected_stack_ids"] is None
        assert destroy_kwargs["authorize_stack"] is None
        assert destroy_kwargs["allow_bootstrap"] is True
        assert destroy_kwargs["bootstrap_stacks"] is None
        assert stop_events[0].is_set()
        thread.join.assert_called_once_with(timeout=5)

    def test_regional_watchdog_stops_when_destroy_raises(self):
        """An unexpected regional destroy exception cannot leak a watchdog."""
        from cli.stacks import StackManager

        manager = StackManager(MagicMock(project_name="gco"))
        thread = MagicMock()
        stop_events = []

        def start_watchdog(_stack_name, stop_event, **_kwargs):
            stop_events.append(stop_event)
            return thread

        stacks = ["gco-global", "gco-api-gateway", "gco-us-east-1"]
        with (
            patch.object(manager, "list_stacks", return_value=stacks),
            patch.object(manager, "_image_registry_destroy_preflight", return_value=True),
            patch.object(manager, "cleanup_orphaned_bastions"),
            patch.object(manager, "_cleanup_backup_vault"),
            patch.object(manager, "_start_eks_sg_watchdog", side_effect=start_watchdog),
            patch.object(manager, "_cleanup_eks_security_groups") as mock_cleanup,
            patch.object(manager, "destroy", side_effect=RuntimeError("destroy failed")),
            pytest.raises(RuntimeError, match="destroy failed"),
        ):
            manager.destroy_orchestrated(force=True)

        assert stop_events[0].is_set()
        thread.join.assert_called_once_with(timeout=5)
        mock_cleanup.assert_called_once_with(
            "gco-us-east-1",
            region=None,
            security_group_id=None,
            vpc_id=None,
        )

    def test_started_watchdogs_stop_when_later_start_raises(self):
        """A later watchdog startup exception stops every allocated event."""
        from cli.stacks import StackManager

        manager = StackManager(MagicMock(project_name="gco"))
        thread = MagicMock()
        stop_events = []

        def start_watchdog(stack_name, stop_event, **_kwargs):
            stop_events.append(stop_event)
            if stack_name == "gco-us-east-1":
                raise RuntimeError("watchdog failed")
            return thread

        stacks = [
            "gco-global",
            "gco-api-gateway",
            "gco-us-east-1",
            "gco-us-west-2",
        ]
        with (
            patch.object(manager, "list_stacks", return_value=stacks),
            patch.object(manager, "_image_registry_destroy_preflight", return_value=True),
            patch.object(manager, "cleanup_orphaned_bastions"),
            patch.object(manager, "_cleanup_backup_vault"),
            patch.object(manager, "_start_eks_sg_watchdog", side_effect=start_watchdog),
            patch.object(manager, "_cleanup_eks_security_groups") as mock_cleanup,
            pytest.raises(RuntimeError, match="watchdog failed"),
        ):
            manager.destroy_orchestrated(force=True)

        assert len(stop_events) == 2
        assert all(stop_event.is_set() for stop_event in stop_events)
        thread.join.assert_called_once_with(timeout=5)
        mock_cleanup.assert_called_once_with(
            "gco-us-west-2",
            region=None,
            security_group_id=None,
            vpc_id=None,
        )


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
        """cdk exits non-zero but CFN shows a *fresh* CREATE_COMPLETE (its
        last-operation time is newer than when the deploy started): cdk's
        polling loop merely gave up early, so the deploy actually succeeded."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "_check_and_fix_stuck_stack"),
            patch.object(StackManager, "ensure_bootstrapped", return_value=True),
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_get_stack_status", return_value="CREATE_COMPLETE"),
            patch.object(
                StackManager,
                "_get_stack_last_update_time",
                return_value=datetime(2999, 1, 1, tzinfo=UTC),
            ),
            patch.object(StackManager, "_diagnose_deploy_failure"),
        ):
            mock_run.return_value = MagicMock(returncode=1)
            manager = StackManager(config)
            assert manager.deploy("gco-global", require_approval=False) is True

    def test_deploy_treats_update_complete_as_success_after_timeout(self):
        """Same reconciliation after a cdk timeout: a fresh UPDATE_COMPLETE
        (last-operation time newer than the deploy start) is a real success."""
        import subprocess

        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "_check_and_fix_stuck_stack"),
            patch.object(StackManager, "ensure_bootstrapped", return_value=True),
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_get_stack_status", return_value="UPDATE_COMPLETE"),
            patch.object(
                StackManager,
                "_get_stack_last_update_time",
                return_value=datetime(2999, 1, 1, tzinfo=UTC),
            ),
            patch.object(StackManager, "_diagnose_deploy_failure"),
        ):
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["cdk"], timeout=3600)
            manager = StackManager(config)
            assert manager.deploy("gco-global", require_approval=False) is True

    def test_deploy_failure_with_stale_complete_is_not_masked(self):
        """Regression: cdk exits non-zero *before* touching CloudFormation
        (e.g. a cloud-assembly schema mismatch or a failed asset/image build).
        The stack is left in the CREATE_COMPLETE of a *previous* deploy — its
        last-operation time predates this attempt — so the stale terminal state
        must NOT be reported as a fresh success."""
        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "_check_and_fix_stuck_stack"),
            patch.object(StackManager, "ensure_bootstrapped", return_value=True),
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_get_stack_status", return_value="CREATE_COMPLETE"),
            patch.object(
                StackManager,
                "_get_stack_last_update_time",
                return_value=datetime(2000, 1, 1, tzinfo=UTC),
            ),
            patch.object(StackManager, "_diagnose_deploy_failure"),
        ):
            mock_run.return_value = MagicMock(returncode=1)
            manager = StackManager(config)
            assert manager.deploy("gco-global", require_approval=False) is False

    def test_deploy_timeout_with_stale_complete_is_not_masked(self):
        """A cdk timeout while the stack sits in a *prior* deploy's terminal
        UPDATE_COMPLETE (last-operation time predates this attempt) is a real
        failure, not a reconciled success."""
        import subprocess

        from cli.stacks import StackManager

        config = MagicMock()

        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "_check_and_fix_stuck_stack"),
            patch.object(StackManager, "ensure_bootstrapped", return_value=True),
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_get_stack_status", return_value="UPDATE_COMPLETE"),
            patch.object(
                StackManager,
                "_get_stack_last_update_time",
                return_value=datetime(2000, 1, 1, tzinfo=UTC),
            ),
            patch.object(StackManager, "_diagnose_deploy_failure"),
        ):
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["cdk"], timeout=3600)
            manager = StackManager(config)
            assert manager.deploy("gco-global", require_approval=False) is False

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
        """Without an explicit timeout, communicate waits without a deadline."""
        from cli.stacks import StackManager

        config = MagicMock()
        manager = StackManager(config)
        manager._cdk_path = "cdk"
        process = _FakePopenProcess(stdout="", stderr="")

        with (
            patch.object(manager, "_ensure_cdk_toolchain"),
            patch.object(manager, "_ensure_lambda_build"),
            patch("cli.stacks.subprocess.Popen", return_value=process) as mock_popen,
        ):
            manager._run_cdk(["list"], capture_output=True)

        assert process.communicate_timeouts == [None]
        assert "timeout" not in mock_popen.call_args.kwargs

    def test_run_cdk_propagates_timeout_kwarg(self):
        """An explicit timeout reaches ``Popen.communicate`` exactly."""
        from cli.stacks import StackManager

        config = MagicMock()
        manager = StackManager(config)
        manager._cdk_path = "cdk"
        process = _FakePopenProcess(stdout="", stderr="")

        with (
            patch.object(manager, "_ensure_cdk_toolchain"),
            patch.object(manager, "_ensure_lambda_build"),
            patch("cli.stacks.subprocess.Popen", return_value=process) as mock_popen,
        ):
            manager._run_cdk(["list"], capture_output=True, timeout=42.0)

        assert process.communicate_timeouts == [42.0]
        assert "timeout" not in mock_popen.call_args.kwargs

    def test_run_cdk_re_raises_timeout_expired(self):
        """A timeout terminates the POSIX process group before being re-raised."""
        from cli.stacks import StackManager

        config = MagicMock()
        manager = StackManager(config)
        manager._cdk_path = "cdk"
        process = _FakePopenProcess(
            pid=9876,
            communicate_error=subprocess.TimeoutExpired(
                cmd=["cdk", "destroy"],
                timeout=10.0,
                output="partial stdout",
                stderr="partial stderr",
            ),
            wait_errors=(subprocess.TimeoutExpired(cmd=["cdk", "destroy"], timeout=30),),
        )

        with (
            patch.object(manager, "_ensure_cdk_toolchain"),
            patch.object(manager, "_ensure_lambda_build"),
            patch("cli.stacks.subprocess.Popen", return_value=process) as mock_popen,
            patch("cli.stacks.os.name", "posix"),
            patch("cli.stacks.os.killpg") as mock_killpg,
            pytest.raises(subprocess.TimeoutExpired) as exc_info,
        ):
            manager._run_cdk(["destroy"], timeout=10.0)

        assert mock_popen.call_args.args[0] == ["cdk", "destroy"]
        assert mock_popen.call_args.kwargs["start_new_session"] is True
        assert process.communicate_timeouts == [10.0]
        assert process.poll_calls == 2
        assert process.wait_timeouts == [30, None]
        assert process.terminate_calls == 0
        assert process.kill_calls == 0
        assert mock_killpg.call_args_list == [
            call(9876, signal.SIGTERM),
            call(9876, signal.SIGKILL),
        ]
        assert exc_info.value.cmd == ["cdk", "destroy"]
        assert exc_info.value.timeout == 10.0
        assert exc_info.value.output == "partial stdout"
        assert exc_info.value.stderr == "partial stderr"


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


class TestInferenceStreamingProxyBuild:
    """The deploy path must fail closed unless npm matches packageManager."""

    @staticmethod
    def _manager(tmp_path: Path):
        from cli.stacks import StackManager

        source = tmp_path / "lambda" / "inference-streaming-proxy"
        source.mkdir(parents=True)
        (source / "index.mjs").write_text("export const handler = {};\n", encoding="utf-8")
        (source / "package.json").write_text(
            json.dumps({"packageManager": "npm@11.18.0"}), encoding="utf-8"
        )
        (source / "package-lock.json").write_text("{}\n", encoding="utf-8")
        manager = object.__new__(StackManager)
        manager.project_root = tmp_path
        return manager

    @staticmethod
    def _write_asset_tree(root: Path, files: dict[str, str]) -> None:
        for relative_path, contents in files.items():
            path = root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")

    @staticmethod
    def _complete_asset_manifest(
        source: Path,
        build: Path,
        source_inputs: tuple[str, ...] | None,
    ) -> None:
        from cli.stacks import _asset_tree_digest, _write_build_manifest

        source_digest = _asset_tree_digest(source, source_inputs=source_inputs)
        assert source_digest is not None
        _write_build_manifest(build, source_digest)

    @pytest.mark.parametrize(
        "relative_path",
        ("handler.py", "requirements.txt", "manifests/00-namespaces.yaml"),
    )
    def test_kubectl_freshness_tracks_every_canonical_input(
        self, tmp_path: Path, relative_path: str
    ) -> None:
        from cli.stacks import StackManager

        files = {
            "handler.py": "def handler():\n    return None\n",
            "requirements.txt": "PyYAML==6.0.3\n",
            "manifests/00-namespaces.yaml": "apiVersion: v1\nkind: Namespace\n",
        }
        source = tmp_path / "lambda" / "kubectl-applier-simple"
        build = tmp_path / "lambda" / "kubectl-applier-simple-build"
        self._write_asset_tree(source, files)
        self._write_asset_tree(build, files)
        (build / "yaml").mkdir()
        self._complete_asset_manifest(
            source,
            build,
            ("handler.py", "requirements.txt", "manifests"),
        )
        assert StackManager._kubectl_build_is_fresh(source, build)

        (build / relative_path).write_text("stale\n", encoding="utf-8")
        assert not StackManager._kubectl_build_is_fresh(source, build)

    @pytest.mark.parametrize(
        "relative_path",
        ("Dockerfile", "charts.yaml", "handler.py", "requirements.txt", "teardown_provider.py"),
    )
    def test_helm_freshness_tracks_complete_docker_context(
        self, tmp_path: Path, relative_path: str
    ) -> None:
        from cli.stacks import StackManager

        files = {
            "Dockerfile": "FROM scratch\n",
            "charts.yaml": "charts: []\n",
            "handler.py": "def handler():\n    return None\n",
            "requirements.txt": "PyYAML==6.0.3\n",
            "teardown_provider.py": "def teardown():\n    return None\n",
        }
        source = tmp_path / "lambda" / "helm-installer"
        build = tmp_path / "lambda" / "helm-installer-build"
        self._write_asset_tree(source, files)
        self._write_asset_tree(build, files)
        self._complete_asset_manifest(source, build, None)
        assert StackManager._helm_build_is_fresh(source, build)

        (build / relative_path).write_text("stale\n", encoding="utf-8")
        assert not StackManager._helm_build_is_fresh(source, build)

    def test_asset_preparation_rebuilds_complete_but_stale_python_assets(
        self, tmp_path: Path
    ) -> None:
        from cli.stacks import StackManager

        kubectl_files = {
            "handler.py": "source\n",
            "requirements.txt": "PyYAML==6.0.3\n",
            "manifests/00.yaml": "source\n",
        }
        helm_files = {"Dockerfile": "source\n", "charts.yaml": "source\n"}
        self._write_asset_tree(tmp_path / "lambda" / "kubectl-applier-simple", kubectl_files)
        self._write_asset_tree(
            tmp_path / "lambda" / "kubectl-applier-simple-build",
            {**kubectl_files, "handler.py": "stale\n"},
        )
        (tmp_path / "lambda" / "kubectl-applier-simple-build" / "yaml").mkdir()
        self._write_asset_tree(tmp_path / "lambda" / "helm-installer", helm_files)
        self._write_asset_tree(
            tmp_path / "lambda" / "helm-installer-build",
            {**helm_files, "charts.yaml": "stale\n"},
        )
        manager = object.__new__(StackManager)
        manager.project_root = tmp_path

        with (
            patch.object(manager, "_build_kubectl_lambda") as build_kubectl,
            patch.object(manager, "_build_helm_installer_lambda") as build_helm,
        ):
            manager._ensure_lambda_build()

        build_kubectl.assert_called_once_with()
        build_helm.assert_called_once_with()

    def test_freshness_detects_missing_transitive_dependency(self, tmp_path: Path) -> None:
        manager = self._manager(tmp_path)
        source = tmp_path / "lambda" / "inference-streaming-proxy"
        build = tmp_path / "lambda" / "inference-streaming-proxy-build"
        dependencies = {
            "@aws-sdk/client-secrets-manager": "3.1089.0",
            "@aws-sdk/client-ssm": "3.1089.0",
        }
        (source / "package.json").write_text(
            json.dumps({"packageManager": "npm@11.18.0", "dependencies": dependencies}),
            encoding="utf-8",
        )
        build.mkdir()
        for name in ("index.mjs", "package.json", "package-lock.json"):
            (build / name).write_bytes((source / name).read_bytes())
        for dependency in dependencies:
            marker = build / "node_modules" / dependency / "package.json"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("{}\n", encoding="utf-8")
        transitive_marker = build / "node_modules" / "@smithy" / "core" / "package.json"
        transitive_marker.parent.mkdir(parents=True, exist_ok=True)
        transitive_marker.write_text("{}\n", encoding="utf-8")
        self._complete_asset_manifest(
            source,
            build,
            ("index.mjs", "package.json", "package-lock.json"),
        )

        assert manager._inference_streaming_build_is_fresh(source, build)

        transitive_marker.unlink()
        assert not manager._inference_streaming_build_is_fresh(source, build)

        (source / "package.json").write_text("{", encoding="utf-8")
        assert not manager._inference_streaming_build_is_fresh(source, build)

    def test_build_uses_exact_declared_npm(self, tmp_path: Path) -> None:
        manager = self._manager(tmp_path)
        version = subprocess.CompletedProcess(
            args=["/test/npm", "--version"], returncode=0, stdout="11.18.0\n", stderr=""
        )
        install = subprocess.CompletedProcess(
            args=["/test/npm", "ci"], returncode=0, stdout="", stderr=""
        )

        with (
            patch("cli.stacks.shutil.which", return_value="/test/npm"),
            patch("cli.stacks.subprocess.run", side_effect=[version, install]) as run,
        ):
            manager._build_inference_streaming_proxy_lambda()

        assert run.call_args_list[0].args[0] == ["/test/npm", "--version"]
        assert run.call_args_list[1].args[0][:2] == ["/test/npm", "ci"]

    def test_synth_refreshes_a_complete_but_stale_build(self, tmp_path: Path) -> None:
        """Synth must rebuild when canonical handler or package inputs changed."""
        manager = self._manager(tmp_path)
        source = tmp_path / "lambda" / "inference-streaming-proxy"
        build = tmp_path / "lambda" / "inference-streaming-proxy-build"
        build.mkdir()
        (build / "index.mjs").write_text("export const handler = 'stale';\n", encoding="utf-8")
        for name in ("package.json", "package-lock.json"):
            (build / name).write_bytes((source / name).read_bytes())
        dependency_marker = (
            build / "node_modules" / "@aws-sdk" / "client-secrets-manager" / "package.json"
        )
        dependency_marker.parent.mkdir(parents=True)
        dependency_marker.write_text("{}\n", encoding="utf-8")

        version = subprocess.CompletedProcess(
            args=["/test/npm", "--version"], returncode=0, stdout="11.18.0\n", stderr=""
        )
        install = subprocess.CompletedProcess(
            args=["/test/npm", "ci"], returncode=0, stdout="", stderr=""
        )
        cdk_result = subprocess.CompletedProcess(
            args=["cdk", "synth"], returncode=0, stdout="synthesized", stderr=""
        )

        with (
            patch("cli.stacks.shutil.which", return_value="/test/npm"),
            patch("cli.stacks.subprocess.run", side_effect=[version, install]),
            patch.object(manager, "_run_cdk", return_value=cdk_result) as run_cdk,
        ):
            assert manager.synth("gco-api-gateway") == "synthesized"

        assert (build / "index.mjs").read_bytes() == (source / "index.mjs").read_bytes()
        assert (build / "package.json").read_bytes() == (source / "package.json").read_bytes()
        assert (build / "package-lock.json").read_bytes() == (
            source / "package-lock.json"
        ).read_bytes()
        run_cdk.assert_called_once_with(
            ["synth", "gco-api-gateway", "--quiet"], capture_output=True
        )

    def test_failed_rebuild_preserves_previous_complete_final(self, tmp_path: Path) -> None:
        manager = self._manager(tmp_path)
        source = tmp_path / "lambda" / "inference-streaming-proxy"
        build = tmp_path / "lambda" / "inference-streaming-proxy-build"
        version = subprocess.CompletedProcess(
            args=["/test/npm", "--version"], returncode=0, stdout="11.18.0\n", stderr=""
        )
        installed = subprocess.CompletedProcess(
            args=["/test/npm", "ci"], returncode=0, stdout="", stderr=""
        )
        failed = subprocess.CompletedProcess(
            args=["/test/npm", "ci"], returncode=1, stdout="", stderr="network failure"
        )

        with (
            patch("cli.stacks.shutil.which", return_value="/test/npm"),
            patch("cli.stacks.subprocess.run", side_effect=[version, installed]),
        ):
            manager._build_inference_streaming_proxy_lambda()
        old_handler = (build / "index.mjs").read_bytes()
        old_manifest = (build / ".gco-build-manifest.json").read_bytes()

        (source / "index.mjs").write_text("export const handler = 'new';\n", encoding="utf-8")
        with (
            patch("cli.stacks.shutil.which", return_value="/test/npm"),
            patch("cli.stacks.subprocess.run", side_effect=[version, failed]),
            pytest.raises(RuntimeError, match="Failed to install pinned"),
        ):
            manager._build_inference_streaming_proxy_lambda()

        assert (build / "index.mjs").read_bytes() == old_handler
        assert (build / ".gco-build-manifest.json").read_bytes() == old_manifest
        assert not list(build.parent.glob(f".{build.name}.staging-*"))
        assert not list(build.parent.glob(f".{build.name}.backup-*"))

    def test_build_rejects_ambient_npm_version(self, tmp_path: Path) -> None:
        manager = self._manager(tmp_path)
        version = subprocess.CompletedProcess(
            args=["/test/npm", "--version"], returncode=0, stdout="10.9.4\n", stderr=""
        )

        with (
            patch("cli.stacks.shutil.which", return_value="/test/npm"),
            patch("cli.stacks.subprocess.run", return_value=version),
            pytest.raises(RuntimeError, match=r"npm 11\.18\.0.*found 10\.9\.4"),
        ):
            manager._build_inference_streaming_proxy_lambda()

        assert not (tmp_path / "lambda" / "inference-streaming-proxy-build").exists()

    def test_build_rejects_nonexact_package_manager(self, tmp_path: Path) -> None:
        manager = self._manager(tmp_path)
        package_json = tmp_path / "lambda" / "inference-streaming-proxy" / "package.json"
        package_json.write_text(json.dumps({"packageManager": "npm@^11.18.0"}), encoding="utf-8")

        with pytest.raises(RuntimeError, match="must pin an exact npm version"):
            manager._build_inference_streaming_proxy_lambda()


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
