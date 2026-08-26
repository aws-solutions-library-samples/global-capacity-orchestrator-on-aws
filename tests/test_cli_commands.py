"""
Tests for the top-level Click CLI command surface.

Drives `gco --help`, `--version`, and `--config` plus the jobs
subgroup (list with required --region/--all-regions guard, get by
name, not-found handling) through CliRunner with the job manager
and output formatter mocked out. This is the command-layer companion
to test_cli_main.py — focused on command wiring and error paths
rather than the full CLI surface.
"""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner


class TestCLIMain:
    """Tests for main CLI group."""

    def test_cli_help(self):
        """Test CLI help output."""
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "GCO CLI" in result.output

    def test_cli_version(self):
        """Test CLI version output."""
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0

    def test_cli_with_config_option(self):
        """Test CLI with config file option."""
        from cli.main import cli

        runner = CliRunner()
        with runner.isolated_filesystem():
            # Create a config file
            with open("config.yaml", "w", encoding="utf-8") as f:
                f.write("default_region: us-west-2\n")

            result = runner.invoke(cli, ["--config", "config.yaml", "--help"])
            assert result.exit_code == 0


class TestJobsCommands:
    """Tests for jobs CLI commands."""

    def test_jobs_help(self):
        """Test jobs group help."""
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["jobs", "--help"])
        assert result.exit_code == 0
        assert "Manage jobs" in result.output

    def test_jobs_list_help(self):
        """Test jobs list help."""
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["jobs", "list", "--help"])
        assert result.exit_code == 0

    @patch("cli.commands.jobs_cmd.get_job_manager")
    def test_jobs_list(self, mock_manager):
        """Test jobs list command requires --region or --all-regions."""
        from cli.main import cli

        mock_job_manager = MagicMock()
        mock_job_manager.list_jobs.return_value = []
        mock_manager.return_value = mock_job_manager

        runner = CliRunner()
        # Without --region or --all-regions should fail
        result = runner.invoke(cli, ["jobs", "list"])
        assert result.exit_code == 1
        # The error message is printed via formatter
        assert "--region" in result.output or "region" in result.output.lower()

    @patch("cli.commands.jobs_cmd.get_job_manager")
    @patch("cli.commands.jobs_cmd.get_output_formatter")
    def test_jobs_list_with_region(self, mock_formatter, mock_manager):
        """Test jobs list with region filter."""
        from cli.main import cli

        mock_job_manager = MagicMock()
        mock_job_manager.list_jobs.return_value = []
        mock_manager.return_value = mock_job_manager

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(cli, ["jobs", "list", "--region", "us-east-1"])
        assert result.exit_code == 0
        mock_job_manager.list_jobs.assert_called_once()

    @patch("cli.commands.jobs_cmd.get_job_manager")
    @patch("cli.commands.jobs_cmd.get_output_formatter")
    def test_jobs_get(self, mock_formatter, mock_manager):
        """Test jobs get command."""
        from cli.main import cli

        mock_job_manager = MagicMock()
        mock_job_manager.get_job.return_value = {"name": "test-job", "status": "running"}
        mock_manager.return_value = mock_job_manager

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(cli, ["jobs", "get", "test-job", "--region", "us-east-1"])
        assert result.exit_code == 0

    @patch("cli.commands.jobs_cmd.get_job_manager")
    @patch("cli.commands.jobs_cmd.get_output_formatter")
    def test_jobs_get_not_found(self, mock_formatter, mock_manager):
        """Test jobs get when job not found."""
        from cli.main import cli

        mock_job_manager = MagicMock()
        mock_job_manager.get_job.return_value = None
        mock_manager.return_value = mock_job_manager

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(cli, ["jobs", "get", "nonexistent-job", "--region", "us-east-1"])
        assert result.exit_code == 1

    def test_jobs_get_missing_regional_bridge_is_not_reported_as_not_found(self):
        """Issue #258: an unreachable regional API bridge must surface as such.

        Runs the real command handler, JobManager, and AWS client request
        path; only regional endpoint discovery is stubbed to "stack not
        deployed" (None), exactly what happens after deploying the regional
        stack without gco-regional-api-<region>.
        """
        from cli.aws_client import GCOAWSClient
        from cli.main import cli

        with patch.object(GCOAWSClient, "get_regional_api_endpoint", return_value=None):
            runner = CliRunner()
            result = runner.invoke(cli, ["jobs", "get", "gpu-test-job", "--region", "us-east-1"])

        assert result.exit_code == 1
        assert "Regional API endpoint is not deployed in us-east-1" in result.output
        # The old behavior collapsed this failure into "Job ... not found".
        assert "not found" not in result.output.lower()

    @patch("cli.commands.jobs_cmd.get_job_manager")
    @patch("cli.commands.jobs_cmd.get_output_formatter")
    def test_jobs_logs(self, mock_formatter, mock_manager):
        """Test jobs logs command."""
        from cli.main import cli

        mock_job_manager = MagicMock()
        mock_job_manager.get_job_logs.return_value = "Log output here"
        mock_manager.return_value = mock_job_manager

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(cli, ["jobs", "logs", "test-job", "--region", "us-east-1"])
        assert result.exit_code == 0
        assert "Log output" in result.output

    @patch("cli.commands.jobs_cmd.get_job_manager")
    @patch("cli.commands.jobs_cmd.get_output_formatter")
    def test_jobs_delete_with_confirm(self, mock_formatter, mock_manager):
        """Test jobs delete with confirmation."""
        from cli.main import cli

        mock_job_manager = MagicMock()
        mock_manager.return_value = mock_job_manager

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(cli, ["jobs", "delete", "test-job", "--region", "us-east-1", "-y"])
        assert result.exit_code == 0


class TestCapacityCommands:
    """Tests for capacity CLI commands."""

    def test_capacity_help(self):
        """Test capacity group help."""
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["capacity", "--help"])
        assert result.exit_code == 0
        assert "Check EC2 capacity" in result.output

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_capacity_check(self, mock_formatter, mock_checker):
        """Test capacity check command."""
        from cli.main import cli

        mock_cap_checker = MagicMock()
        mock_cap_checker.estimate_capacity.return_value = []
        mock_checker.return_value = mock_cap_checker

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(
            cli, ["capacity", "check", "--instance-type", "g4dn.xlarge", "--region", "us-east-1"]
        )
        assert result.exit_code == 0

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_capacity_recommend(self, mock_formatter, mock_checker):
        """Test capacity recommend command."""
        from cli.main import cli

        mock_cap_checker = MagicMock()
        mock_cap_checker.recommend_capacity_type.return_value = ("spot", "Good availability")
        mock_checker.return_value = mock_cap_checker

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["capacity", "recommend", "--instance-type", "g4dn.xlarge", "--region", "us-east-1"],
        )
        assert result.exit_code == 0

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_capacity_spot_prices(self, mock_formatter, mock_checker):
        """Test capacity spot-prices command."""
        from cli.main import cli

        mock_cap_checker = MagicMock()
        mock_cap_checker.get_spot_price_history.return_value = [
            {"availability_zone": "us-east-1a", "current_price": 0.50}
        ]
        mock_checker.return_value = mock_cap_checker

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["capacity", "spot-prices", "--instance-type", "g4dn.xlarge", "--region", "us-east-1"],
        )
        assert result.exit_code == 0

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_capacity_instance_info(self, mock_formatter, mock_checker):
        """Test capacity instance-info command."""
        from cli.capacity import InstanceTypeInfo
        from cli.main import cli

        mock_cap_checker = MagicMock()
        mock_cap_checker.get_instance_info.return_value = InstanceTypeInfo(
            instance_type="g4dn.xlarge",
            vcpus=4,
            memory_gib=16,
            region="us-east-1",
        )
        mock_checker.return_value = mock_cap_checker

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(cli, ["capacity", "instance-info", "g4dn.xlarge"])
        assert result.exit_code == 0


class TestStacksCommands:
    """Tests for stacks CLI commands."""

    def test_stacks_help(self):
        """Test stacks group help."""
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["stacks", "--help"])
        assert result.exit_code == 0
        assert "Deploy and manage" in result.output

    @patch("cli.stacks.get_stack_manager")
    @patch("cli.commands.stacks_cmd.get_output_formatter")
    def test_stacks_list(self, mock_formatter, mock_manager):
        """Test stacks list command."""
        from cli.main import cli

        mock_stack_manager = MagicMock()
        mock_stack_manager.list_stacks.return_value = ["gco-global", "gco-us-east-1"]
        mock_manager.return_value = mock_stack_manager

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(cli, ["stacks", "list"])
        assert result.exit_code == 0

    @patch("cli.stacks.get_stack_manager")
    @patch("cli.commands.stacks_cmd.get_output_formatter")
    def test_stacks_synth(self, mock_formatter, mock_manager):
        """Test stacks synth command."""
        from cli.main import cli

        mock_stack_manager = MagicMock()
        mock_stack_manager.synth.return_value = "Synthesized"
        mock_manager.return_value = mock_stack_manager

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(cli, ["stacks", "synth"])
        assert result.exit_code == 0

    @patch("cli.stacks.get_stack_manager")
    @patch("cli.commands.stacks_cmd.get_output_formatter")
    def test_stacks_diff(self, mock_formatter, mock_manager):
        """Test stacks diff command."""
        from cli.main import cli

        mock_stack_manager = MagicMock()
        mock_stack_manager.diff.return_value = "No differences"
        mock_manager.return_value = mock_stack_manager

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(cli, ["stacks", "diff"])
        assert result.exit_code == 0

    @patch("cli.stacks.get_stack_manager")
    @patch("cli.commands.stacks_cmd.get_output_formatter")
    def test_stacks_deploy_no_stack(self, mock_formatter, mock_manager):
        """Test stacks deploy without stack name shows usage error."""
        from cli.main import cli

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(cli, ["stacks", "deploy"])
        # Click returns exit code 2 for missing required argument
        assert result.exit_code == 2

    @patch("cli.stacks.get_stack_manager")
    @patch("cli.commands.stacks_cmd.get_output_formatter")
    def test_stacks_deploy_with_stack(self, mock_formatter, mock_manager):
        """Test stacks deploy with stack name."""
        from cli.main import cli

        mock_stack_manager = MagicMock()
        mock_stack_manager.deploy.return_value = True
        mock_manager.return_value = mock_stack_manager

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(cli, ["stacks", "deploy", "gco-global", "-y"])
        assert result.exit_code == 0

    @patch("cli.stacks.get_stack_manager")
    @patch("cli.commands.stacks_cmd.get_output_formatter")
    def test_stacks_destroy_no_stack(self, mock_formatter, mock_manager):
        """Test stacks destroy without stack name shows usage error."""
        from cli.main import cli

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(cli, ["stacks", "destroy"])
        # Click returns exit code 2 for missing required argument
        assert result.exit_code == 2

    @patch("cli.stacks.get_stack_manager")
    @patch("cli.commands.stacks_cmd.get_output_formatter")
    def test_stacks_bootstrap(self, mock_formatter, mock_manager):
        """Test stacks bootstrap command."""
        from cli.main import cli

        mock_stack_manager = MagicMock()
        mock_stack_manager.bootstrap.return_value = True
        mock_manager.return_value = mock_stack_manager

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(cli, ["stacks", "bootstrap", "--region", "us-east-1"])
        assert result.exit_code == 0

    @patch("cli.stacks.get_stack_manager")
    @patch("cli.commands.stacks_cmd.get_output_formatter")
    def test_stacks_status(self, mock_formatter, mock_manager):
        """Test stacks status command."""
        from cli.main import cli

        mock_stack_manager = MagicMock()
        mock_status = MagicMock()
        mock_status.to_dict.return_value = {"status": "CREATE_COMPLETE"}
        mock_stack_manager.get_stack_status.return_value = mock_status
        mock_manager.return_value = mock_stack_manager

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(cli, ["stacks", "status", "gco-us-east-1", "--region", "us-east-1"])
        assert result.exit_code == 0

    @patch("cli.stacks.get_stack_manager")
    @patch("cli.commands.stacks_cmd.get_output_formatter")
    def test_stacks_outputs(self, mock_formatter, mock_manager):
        """Test stacks outputs command."""
        from cli.main import cli

        mock_stack_manager = MagicMock()
        mock_stack_manager.get_outputs.return_value = {"ClusterName": "gco-us-east-1"}
        mock_manager.return_value = mock_stack_manager

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(cli, ["stacks", "outputs", "gco-us-east-1", "--region", "us-east-1"])
        assert result.exit_code == 0


class TestFSxCommands:
    """Tests for FSx CLI commands."""

    def test_fsx_help(self):
        """Test fsx group help."""
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["stacks", "fsx", "--help"])
        assert result.exit_code == 0
        assert "FSx for Lustre" in result.output

    @patch("cli.stacks.get_fsx_config")
    @patch("cli.commands.stacks_cmd.get_output_formatter")
    def test_fsx_status(self, mock_formatter, mock_get_config):
        """Test fsx status command."""
        from cli.main import cli

        mock_get_config.return_value = {"enabled": True, "storage_capacity_gib": 1200}

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(cli, ["stacks", "fsx", "status"])
        assert result.exit_code == 0

    @patch("cli.stacks.update_fsx_config")
    @patch("cli.commands.stacks_cmd.get_output_formatter")
    def test_fsx_enable(self, mock_formatter, mock_update):
        """Test fsx enable command."""
        from cli.main import cli

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(cli, ["stacks", "fsx", "enable", "-y"])
        assert result.exit_code == 0
        mock_update.assert_called_once()

    @patch("cli.stacks.update_fsx_config")
    @patch("cli.commands.stacks_cmd.get_output_formatter")
    def test_fsx_enable_with_options(self, mock_formatter, mock_update):
        """Test fsx enable with custom options."""
        from cli.main import cli

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "stacks",
                "fsx",
                "enable",
                "--storage-capacity",
                "2400",
                "--deployment-type",
                "PERSISTENT_2",
                "-y",
            ],
        )
        assert result.exit_code == 0

    @patch("cli.stacks.update_fsx_config")
    @patch("cli.commands.stacks_cmd.get_output_formatter")
    def test_fsx_enable_invalid_capacity(self, mock_formatter, mock_update):
        """Test fsx enable with invalid storage capacity."""
        from cli.main import cli

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(cli, ["stacks", "fsx", "enable", "--storage-capacity", "100", "-y"])
        assert result.exit_code == 1

    @patch("cli.stacks.update_fsx_config")
    @patch("cli.commands.stacks_cmd.get_output_formatter")
    def test_fsx_disable(self, mock_formatter, mock_update):
        """Test fsx disable command."""
        from cli.main import cli

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(cli, ["stacks", "fsx", "disable", "-y"])
        assert result.exit_code == 0
        mock_update.assert_called_once_with({"enabled": False}, None)


class TestFilesCommands:
    """Tests for files CLI commands."""

    def test_files_help(self):
        """Test files group help."""
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["files", "--help"])
        assert result.exit_code == 0
        assert "Manage file systems" in result.output

    @patch("cli.commands.files_cmd.get_file_system_client")
    @patch("cli.commands.files_cmd.get_output_formatter")
    def test_files_list(self, mock_formatter, mock_client):
        """Test files list command."""
        from cli.main import cli

        mock_fs_client = MagicMock()
        mock_fs_client.get_file_systems.return_value = []
        mock_client.return_value = mock_fs_client

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(cli, ["files", "list"])
        assert result.exit_code == 0

    @patch("cli.commands.files_cmd.get_file_system_client")
    @patch("cli.commands.files_cmd.get_output_formatter")
    def test_files_get(self, mock_formatter, mock_client):
        """Test files get command."""
        from cli.main import cli

        mock_fs_client = MagicMock()
        mock_fs_client.get_file_system_by_region.return_value = {
            "file_system_id": "fs-12345",
            "status": "available",
        }
        mock_client.return_value = mock_fs_client

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(cli, ["files", "get", "us-east-1"])
        assert result.exit_code == 0

    @patch("cli.commands.files_cmd.get_file_system_client")
    @patch("cli.commands.files_cmd.get_output_formatter")
    def test_files_access_points(self, mock_formatter, mock_client):
        """Test files access-points command."""
        from cli.main import cli

        mock_fs_client = MagicMock()
        mock_fs_client.get_access_point_info.return_value = [{"access_point_id": "fsap-12345"}]
        mock_client.return_value = mock_fs_client

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(cli, ["files", "access-points", "fs-12345", "--region", "us-east-1"])
        assert result.exit_code == 0


class TestConfigCommands:
    """Tests for config CLI commands."""

    def test_config_help(self):
        """Test config group help."""
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["config-cmd", "--help"])
        assert result.exit_code == 0

    @patch("cli.commands.config_cmd.get_output_formatter")
    def test_config_show(self, mock_formatter):
        """Test config show command."""
        from cli.main import cli

        mock_output = MagicMock()
        mock_formatter.return_value = mock_output

        runner = CliRunner()
        result = runner.invoke(cli, ["config-cmd", "show"])
        assert result.exit_code == 0


class TestMainFunction:
    """Tests for main entry point."""

    def test_main_function_exists(self):
        """Test main function exists and is callable."""
        from cli.main import main

        assert callable(main)
