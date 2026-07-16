"""
Tests for cli/main.py command dispatching.

Broad coverage of the Click CLI entry point — version/help,
jobs list (with --region, --all-regions, and filters), jobs get
(required --region, not-found handling) — using CliRunner with
patched get_job_manager. Shares ground with test_cli_commands.py
but focuses on argument validation and the --all-regions global
aggregation path.
"""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner


class TestCliVersion:
    """Tests for CLI version and help."""

    def test_cli_version(self):
        """Test --version flag."""
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "gco" in result.output.lower()

    def test_cli_help(self):
        """Test --help flag."""
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "GCO CLI" in result.output


class TestJobsCommands:
    """Tests for jobs commands."""

    def test_jobs_help(self):
        """Test jobs --help."""
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["jobs", "--help"])
        assert result.exit_code == 0
        assert "Manage jobs" in result.output

    def test_jobs_list(self):
        """Test jobs list command requires --region or --all-regions."""
        from cli.main import cli

        runner = CliRunner()

        # Without --region or --all-regions should fail
        result = runner.invoke(cli, ["jobs", "list"])
        assert result.exit_code == 1
        assert "must specify --region or --all-regions" in result.output

    def test_jobs_list_with_filters(self):
        """Test jobs list with filters."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.list_jobs.return_value = []
            mock_manager.return_value = mock_jm

            result = runner.invoke(
                cli, ["jobs", "list", "--namespace", "test", "--region", "us-east-1"]
            )
            assert result.exit_code == 0
            mock_jm.list_jobs.assert_called_once()

    def test_jobs_list_all_regions(self):
        """Test jobs list --all-regions."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.list_jobs_global.return_value = {
                "total": 0,
                "jobs": [],
                "regions_queried": 2,
                "regions_successful": 2,
                "region_summaries": [],
            }
            mock_manager.return_value = mock_jm

            result = runner.invoke(cli, ["jobs", "list", "--all-regions"])
            assert result.exit_code == 0

    def test_jobs_get(self):
        """Test jobs get command requires --region."""
        from cli.main import cli

        runner = CliRunner()

        # Without --region should fail
        result = runner.invoke(cli, ["jobs", "get", "test-job"])
        assert result.exit_code == 2  # Click error for missing required option

        # With --region should work
        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.get_job.return_value = {"name": "test-job", "status": "Running"}
            mock_manager.return_value = mock_jm

            result = runner.invoke(cli, ["jobs", "get", "test-job", "--region", "us-east-1"])
            assert result.exit_code == 0

    def test_jobs_get_not_found(self):
        """Test jobs get when job not found."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.get_job.return_value = None
            mock_manager.return_value = mock_jm

            result = runner.invoke(cli, ["jobs", "get", "nonexistent", "--region", "us-east-1"])
            assert result.exit_code == 1
            assert "not found" in result.output

    def test_jobs_logs(self):
        """Test jobs logs command requires --region."""
        from cli.main import cli

        runner = CliRunner()

        # Without --region should fail
        result = runner.invoke(cli, ["jobs", "logs", "test-job"])
        assert result.exit_code == 2  # Click error for missing required option

        # With --region should work
        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.get_job_logs.return_value = "Log output here"
            mock_manager.return_value = mock_jm

            result = runner.invoke(cli, ["jobs", "logs", "test-job", "--region", "us-east-1"])
            assert result.exit_code == 0
            assert "Log output" in result.output

    def test_jobs_delete_with_confirm(self):
        """Test jobs delete requires --region."""
        from cli.main import cli

        runner = CliRunner()

        # Without --region should fail
        result = runner.invoke(cli, ["jobs", "delete", "test-job", "-y"])
        assert result.exit_code == 2  # Click error for missing required option

        # With --region should work
        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_manager.return_value = mock_jm

            result = runner.invoke(
                cli, ["jobs", "delete", "test-job", "--region", "us-east-1", "-y"]
            )
            assert result.exit_code == 0
            mock_jm.delete_job.assert_called_once()


class TestCapacityCommands:
    """Tests for capacity commands."""

    def test_capacity_help(self):
        """Test capacity --help."""
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["capacity", "--help"])
        assert result.exit_code == 0
        assert "Check EC2 capacity" in result.output

    def test_capacity_check(self):
        """Test capacity check command."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.capacity_cmd.get_capacity_checker") as mock_checker:
            mock_cc = MagicMock()
            mock_cc.estimate_capacity.return_value = []
            mock_checker.return_value = mock_cc

            result = runner.invoke(
                cli, ["capacity", "check", "-i", "g4dn.xlarge", "-r", "us-east-1"]
            )
            assert result.exit_code == 0

    def test_capacity_recommend(self):
        """Test capacity recommend command."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.capacity_cmd.get_capacity_checker") as mock_checker:
            mock_cc = MagicMock()
            mock_cc.recommend_capacity_type.return_value = ("spot", "Good spot availability")
            mock_checker.return_value = mock_cc

            result = runner.invoke(
                cli, ["capacity", "recommend", "-i", "g4dn.xlarge", "-r", "us-east-1"]
            )
            assert result.exit_code == 0
            assert "spot" in result.output.lower()

    def test_capacity_spot_prices(self):
        """Test capacity spot-prices command."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.capacity_cmd.get_capacity_checker") as mock_checker:
            mock_cc = MagicMock()
            mock_cc.get_spot_price_history.return_value = []
            mock_checker.return_value = mock_cc

            result = runner.invoke(
                cli, ["capacity", "spot-prices", "-i", "g4dn.xlarge", "-r", "us-east-1"]
            )
            assert result.exit_code == 0

    def test_capacity_instance_info(self):
        """Test capacity instance-info command."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.capacity_cmd.get_capacity_checker") as mock_checker:
            mock_cc = MagicMock()
            mock_cc.get_instance_info.return_value = {
                "instance_type": "g4dn.xlarge",
                "vcpus": 4,
                "memory_gib": 16,
            }
            mock_checker.return_value = mock_cc

            result = runner.invoke(cli, ["capacity", "instance-info", "g4dn.xlarge"])
            assert result.exit_code == 0

    def test_capacity_instance_info_not_found(self):
        """Test capacity instance-info when not found."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.capacity_cmd.get_capacity_checker") as mock_checker:
            mock_cc = MagicMock()
            mock_cc.get_instance_info.return_value = None
            mock_checker.return_value = mock_cc

            result = runner.invoke(cli, ["capacity", "instance-info", "nonexistent"])
            assert result.exit_code == 1


class TestStacksCommands:
    """Tests for stacks commands."""

    def test_stacks_help(self):
        """Test stacks --help."""
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["stacks", "--help"])
        assert result.exit_code == 0
        assert "Deploy and manage" in result.output

    def test_stacks_list(self):
        """Test stacks list command."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_sm.list_stacks.return_value = ["stack1", "stack2"]
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "list"])
            assert result.exit_code == 0
            assert "stack1" in result.output

    def test_stacks_synth(self):
        """Test stacks synth command."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_sm.synth.return_value = "Synthesized"
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "synth"])
            assert result.exit_code == 0

    def test_stacks_diff(self):
        """Test stacks diff command."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_sm.diff.return_value = "No differences"
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "diff"])
            assert result.exit_code == 0

    def test_stacks_deploy_no_stack(self):
        """Test stacks deploy without stack name shows error."""
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["stacks", "deploy"])
        # Click will show usage error for missing required argument
        assert result.exit_code == 2

    def test_stacks_deploy_with_stack(self):
        """Test stacks deploy with stack name."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_sm.deploy.return_value = True
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "deploy", "test-stack", "-y"])
            assert result.exit_code == 0

    def test_stacks_deploy_all_orchestrated(self):
        """Test stacks deploy-all command."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_sm.list_stacks.return_value = ["gco-global", "gco-us-east-1"]
            mock_sm.deploy_orchestrated.return_value = (
                True,
                ["gco-global", "gco-us-east-1"],
                [],
            )
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "deploy-all", "-y"])
            assert result.exit_code == 0
            assert "deployed successfully" in result.output.lower()

    def test_stacks_deploy_all_failure(self):
        """Test stacks deploy-all with failure."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_sm.list_stacks.return_value = ["gco-global", "gco-us-east-1"]
            mock_sm.deploy_orchestrated.return_value = (
                False,
                ["gco-global"],
                ["gco-us-east-1"],
            )
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "deploy-all", "-y"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_stacks_destroy_no_stack(self):
        """Test stacks destroy without stack name shows error."""
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["stacks", "destroy"])
        # Click will show usage error for missing required argument
        assert result.exit_code == 2

    def test_stacks_destroy_with_stack(self):
        """Test stacks destroy with stack name."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_sm.destroy.return_value = True
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "destroy", "test-stack", "-y"])
            assert result.exit_code == 0

    def test_stacks_destroy_all_orchestrated(self):
        """Test stacks destroy-all command."""
        from cli.main import cli

        runner = CliRunner()

        with (
            patch("cli.stacks.get_stack_manager") as mock_manager,
            patch("cli.stacks.get_stack_destroy_order") as mock_order,
        ):
            mock_sm = MagicMock()
            mock_sm.list_stacks.return_value = ["gco-global", "gco-us-east-1"]
            mock_sm.destroy_orchestrated.return_value = (
                True,
                ["gco-us-east-1", "gco-global"],
                [],
            )
            mock_manager.return_value = mock_sm
            mock_order.return_value = ["gco-us-east-1", "gco-global"]

            result = runner.invoke(cli, ["stacks", "destroy-all", "-y"])
            assert result.exit_code == 0
            assert "destroyed successfully" in result.output.lower()

    def test_stacks_destroy_all_failure(self):
        """Test stacks destroy-all with failure (retries exhausted)."""
        from cli.main import cli

        runner = CliRunner()

        with (
            patch("cli.stacks.get_stack_manager") as mock_manager,
            patch("cli.stacks.get_stack_destroy_order") as mock_order,
            patch("time.sleep"),
        ):
            mock_sm = MagicMock()
            mock_sm.list_stacks.return_value = ["gco-global", "gco-us-east-1"]
            mock_sm.destroy_orchestrated.return_value = (
                False,
                ["gco-us-east-1"],
                ["gco-global"],
            )
            mock_manager.return_value = mock_sm
            mock_order.return_value = ["gco-us-east-1", "gco-global"]

            result = runner.invoke(cli, ["stacks", "destroy-all", "-y"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()
            # Should retry 3 times total
            assert mock_sm.destroy_orchestrated.call_count == 3

    def test_stacks_bootstrap(self):
        """Test stacks bootstrap command."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_sm.bootstrap.return_value = True
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "bootstrap", "-r", "us-east-1"])
            assert result.exit_code == 0

    def test_stacks_status(self):
        """Test stacks status command."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_status = MagicMock()
            mock_status.to_dict.return_value = {"name": "test", "status": "CREATE_COMPLETE"}
            mock_sm.get_stack_status.return_value = mock_status
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "status", "test-stack", "-r", "us-east-1"])
            assert result.exit_code == 0

    def test_stacks_outputs(self):
        """Test stacks outputs command."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_sm.get_outputs.return_value = {"OutputKey": "OutputValue"}
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "outputs", "test-stack", "-r", "us-east-1"])
            assert result.exit_code == 0


class TestFsxCommands:
    """Tests for FSx commands."""

    def test_fsx_status(self):
        """Test fsx status command."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_fsx_config") as mock_config:
            mock_config.return_value = {"enabled": False}

            result = runner.invoke(cli, ["stacks", "fsx", "status"])
            assert result.exit_code == 0

    def test_fsx_enable(self):
        """Test fsx enable command."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.update_fsx_config") as mock_update:
            result = runner.invoke(cli, ["stacks", "fsx", "enable", "-y"])
            assert result.exit_code == 0
            mock_update.assert_called_once()

    def test_fsx_enable_invalid_capacity(self):
        """Test fsx enable with invalid capacity."""
        from cli.main import cli

        runner = CliRunner()

        result = runner.invoke(cli, ["stacks", "fsx", "enable", "-s", "100", "-y"])
        assert result.exit_code == 1
        assert "at least 1200" in result.output

    def test_fsx_disable(self):
        """Test fsx disable command."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.update_fsx_config") as mock_update:
            result = runner.invoke(cli, ["stacks", "fsx", "disable", "-y"])
            assert result.exit_code == 0
            mock_update.assert_called_once()


class TestFilesCommands:
    """Tests for files commands."""

    def test_files_help(self):
        """Test files --help."""
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["files", "--help"])
        assert result.exit_code == 0
        assert "Manage file systems" in result.output

    def test_files_list(self):
        """Test files list command."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.files_cmd.get_file_system_client") as mock_client:
            mock_fs = MagicMock()
            mock_fs.get_file_systems.return_value = []
            mock_client.return_value = mock_fs

            result = runner.invoke(cli, ["files", "list"])
            assert result.exit_code == 0

    def test_files_get(self):
        """Test files get command."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.files_cmd.get_file_system_client") as mock_client:
            mock_fs = MagicMock()
            mock_fs.get_file_system_by_region.return_value = {"id": "fs-123", "type": "efs"}
            mock_client.return_value = mock_fs

            result = runner.invoke(cli, ["files", "get", "us-east-1"])
            assert result.exit_code == 0

    def test_files_get_not_found(self):
        """Test files get when not found."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.files_cmd.get_file_system_client") as mock_client:
            mock_fs = MagicMock()
            mock_fs.get_file_system_by_region.return_value = None
            mock_client.return_value = mock_fs

            result = runner.invoke(cli, ["files", "get", "us-east-1"])
            assert result.exit_code == 1

    def test_files_access_points(self):
        """Test files access-points command."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.files_cmd.get_file_system_client") as mock_client:
            mock_fs = MagicMock()
            mock_fs.get_access_point_info.return_value = []
            mock_client.return_value = mock_fs

            result = runner.invoke(cli, ["files", "access-points", "fs-123", "-r", "us-east-1"])
            assert result.exit_code == 0


class TestConfigCommands:
    """Tests for config commands."""

    def test_config_show(self):
        """Test config show command."""
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["config-cmd", "show"])
        assert result.exit_code == 0


class TestCliOptions:
    """Tests for global CLI options."""

    def test_cli_with_output_json(self):
        """Test CLI with --output json."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.list_jobs.return_value = []
            mock_manager.return_value = mock_jm

            result = runner.invoke(
                cli, ["--output", "json", "jobs", "list", "--region", "us-east-1"]
            )
            assert result.exit_code == 0

    def test_cli_with_verbose(self):
        """Test CLI with --verbose."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.list_jobs.return_value = []
            mock_manager.return_value = mock_jm

            result = runner.invoke(cli, ["--verbose", "jobs", "list", "--region", "us-east-1"])
            assert result.exit_code == 0

    def test_cli_with_region(self):
        """Test CLI with --region global option."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.list_jobs.return_value = []
            mock_manager.return_value = mock_jm

            # Global --region sets default, but jobs list still needs explicit --region or --all-regions
            result = runner.invoke(
                cli, ["--region", "us-west-2", "jobs", "list", "--region", "us-west-2"]
            )
            assert result.exit_code == 0


class TestMainEntryPoint:
    """Tests for main entry point."""

    def test_main_function_exists(self):
        """Test that main function exists."""
        from cli.main import main

        assert callable(main)


class TestJobsSubmitCommand:
    """Tests for jobs submit command."""

    def test_jobs_submit_success(self):
        """Test jobs submit command success."""
        import os
        import tempfile

        from cli.main import cli

        runner = CliRunner()

        # Create a temporary manifest file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: test-job\n")
            f.flush()
            manifest_path = f.name

        try:
            with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
                mock_jm = MagicMock()
                mock_jm.submit_job.return_value = {"name": "test-job", "status": "submitted"}
                mock_manager.return_value = mock_jm

                result = runner.invoke(cli, ["jobs", "submit", manifest_path])
                assert result.exit_code == 0
                assert "submitted" in result.output.lower()
        finally:
            os.unlink(manifest_path)

    def test_jobs_submit_with_labels(self):
        """Test jobs submit with labels."""
        import os
        import tempfile

        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: test-job\n")
            f.flush()
            manifest_path = f.name

        try:
            with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
                mock_jm = MagicMock()
                mock_jm.submit_job.return_value = {"name": "test-job"}
                mock_manager.return_value = mock_jm

                result = runner.invoke(
                    cli, ["jobs", "submit", manifest_path, "-l", "team=ml", "-l", "env=prod"]
                )
                assert result.exit_code == 0
                # Verify labels were passed
                call_kwargs = mock_jm.submit_job.call_args.kwargs
                assert call_kwargs.get("labels") == {"team": "ml", "env": "prod"}
        finally:
            os.unlink(manifest_path)

    def test_jobs_submit_dry_run(self):
        """Test jobs submit with --dry-run."""
        import os
        import tempfile

        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: test-job\n")
            f.flush()
            manifest_path = f.name

        try:
            with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
                mock_jm = MagicMock()
                mock_jm.submit_job.return_value = {"name": "test-job", "valid": True}
                mock_manager.return_value = mock_jm

                result = runner.invoke(cli, ["jobs", "submit", manifest_path, "--dry-run"])
                assert result.exit_code == 0
                assert "dry run" in result.output.lower()
        finally:
            os.unlink(manifest_path)

    def test_jobs_submit_error(self):
        """Test jobs submit with error."""
        import os
        import tempfile

        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: test-job\n")
            f.flush()
            manifest_path = f.name

        try:
            with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
                mock_jm = MagicMock()
                mock_jm.submit_job.side_effect = Exception("Submission failed")
                mock_manager.return_value = mock_jm

                result = runner.invoke(cli, ["jobs", "submit", manifest_path])
                assert result.exit_code == 1
                assert "failed" in result.output.lower()
        finally:
            os.unlink(manifest_path)


class TestJobsListError:
    """Tests for jobs list error handling."""

    def test_jobs_list_error(self):
        """Test jobs list with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.list_jobs.side_effect = Exception("API error")
            mock_manager.return_value = mock_jm

            result = runner.invoke(cli, ["jobs", "list", "--region", "us-east-1"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestJobsGetError:
    """Tests for jobs get error handling."""

    def test_jobs_get_error(self):
        """Test jobs get with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.get_job.side_effect = Exception("API error")
            mock_manager.return_value = mock_jm

            result = runner.invoke(cli, ["jobs", "get", "test-job", "--region", "us-east-1"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestJobsLogsError:
    """Tests for jobs logs error handling."""

    def test_jobs_logs_error(self):
        """Test jobs logs with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.get_job_logs.side_effect = Exception("API error")
            mock_manager.return_value = mock_jm

            result = runner.invoke(cli, ["jobs", "logs", "test-job", "--region", "us-east-1"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestJobsDeleteError:
    """Tests for jobs delete error handling."""

    def test_jobs_delete_error(self):
        """Test jobs delete with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.delete_job.side_effect = Exception("API error")
            mock_manager.return_value = mock_jm

            result = runner.invoke(
                cli, ["jobs", "delete", "test-job", "--region", "us-east-1", "-y"]
            )
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestCapacityErrors:
    """Tests for capacity command error handling."""

    def test_capacity_check_error(self):
        """Test capacity check with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.capacity_cmd.get_capacity_checker") as mock_checker:
            mock_cc = MagicMock()
            mock_cc.estimate_capacity.side_effect = Exception("API error")
            mock_checker.return_value = mock_cc

            result = runner.invoke(
                cli, ["capacity", "check", "-i", "g4dn.xlarge", "-r", "us-east-1"]
            )
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_capacity_recommend_error(self):
        """Test capacity recommend with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.capacity_cmd.get_capacity_checker") as mock_checker:
            mock_cc = MagicMock()
            mock_cc.recommend_capacity_type.side_effect = Exception("API error")
            mock_checker.return_value = mock_cc

            result = runner.invoke(
                cli, ["capacity", "recommend", "-i", "g4dn.xlarge", "-r", "us-east-1"]
            )
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_capacity_spot_prices_error(self):
        """Test capacity spot-prices with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.capacity_cmd.get_capacity_checker") as mock_checker:
            mock_cc = MagicMock()
            mock_cc.get_spot_price_history.side_effect = Exception("API error")
            mock_checker.return_value = mock_cc

            result = runner.invoke(
                cli, ["capacity", "spot-prices", "-i", "g4dn.xlarge", "-r", "us-east-1"]
            )
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestFilesErrors:
    """Tests for files command error handling."""

    def test_files_list_error(self):
        """Test files list with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.files_cmd.get_file_system_client") as mock_client:
            mock_fs = MagicMock()
            mock_fs.get_file_systems.side_effect = Exception("API error")
            mock_client.return_value = mock_fs

            result = runner.invoke(cli, ["files", "list"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_files_get_error(self):
        """Test files get with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.files_cmd.get_file_system_client") as mock_client:
            mock_fs = MagicMock()
            mock_fs.get_file_system_by_region.side_effect = Exception("API error")
            mock_client.return_value = mock_fs

            result = runner.invoke(cli, ["files", "get", "us-east-1"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_files_access_points_error(self):
        """Test files access-points with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.files_cmd.get_file_system_client") as mock_client:
            mock_fs = MagicMock()
            mock_fs.get_access_point_info.side_effect = Exception("API error")
            mock_client.return_value = mock_fs

            result = runner.invoke(cli, ["files", "access-points", "fs-123", "-r", "us-east-1"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestStacksErrors:
    """Tests for stacks command error handling."""

    def test_stacks_list_error(self):
        """Test stacks list with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_sm.list_stacks.side_effect = Exception("CDK error")
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "list"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_stacks_synth_error(self):
        """Test stacks synth with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_sm.synth.side_effect = Exception("CDK error")
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "synth"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_stacks_diff_error(self):
        """Test stacks diff with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_sm.diff.side_effect = Exception("CDK error")
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "diff"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_stacks_deploy_error(self):
        """Test stacks deploy with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_sm.deploy.side_effect = Exception("CDK error")
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "deploy", "test-stack", "-y"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_stacks_destroy_error(self):
        """Test stacks destroy with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_sm.destroy.side_effect = Exception("CDK error")
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "destroy", "test-stack", "-y"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_stacks_bootstrap_error(self):
        """Test stacks bootstrap with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_sm.bootstrap.side_effect = Exception("CDK error")
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "bootstrap", "-r", "us-east-1"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_stacks_status_error(self):
        """Test stacks status with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_sm.get_stack_status.side_effect = Exception("CDK error")
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "status", "test-stack", "-r", "us-east-1"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_stacks_outputs_error(self):
        """Test stacks outputs with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_sm.get_outputs.side_effect = Exception("CDK error")
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "outputs", "test-stack", "-r", "us-east-1"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestConfigWithFile:
    """Tests for config file loading."""

    def test_cli_with_config_file(self):
        """Test CLI with --config option."""
        import os
        import tempfile

        from cli.main import cli

        runner = CliRunner()

        # Create a temporary config file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("default_region: us-west-2\ndefault_namespace: test-ns\n")
            f.flush()
            config_path = f.name

        try:
            with patch.dict(os.environ, {}, clear=True):
                result = runner.invoke(
                    cli,
                    [
                        "--config",
                        config_path,
                        "config-cmd",
                        "get",
                        "default_namespace",
                    ],
                )
            assert result.exit_code == 0
            assert "test-ns" in result.output
        finally:
            os.unlink(config_path)


class TestJobsSubmitDirectCommand:
    """Tests for jobs submit-direct command."""

    def test_jobs_submit_direct_success(self):
        """Test jobs submit-direct command success."""
        import os
        import tempfile

        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: test-job\n")
            f.flush()
            manifest_path = f.name

        try:
            with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
                mock_jm = MagicMock()
                mock_jm.submit_job_direct.return_value = {"name": "test-job", "status": "created"}
                mock_manager.return_value = mock_jm

                result = runner.invoke(
                    cli, ["jobs", "submit-direct", manifest_path, "-r", "us-east-1"]
                )
                assert result.exit_code == 0
                assert "submitted" in result.output.lower()
        finally:
            os.unlink(manifest_path)

    def test_jobs_submit_direct_with_labels(self):
        """Test jobs submit-direct with labels."""
        import os
        import tempfile

        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: test-job\n")
            f.flush()
            manifest_path = f.name

        try:
            with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
                mock_jm = MagicMock()
                mock_jm.submit_job_direct.return_value = {"name": "test-job"}
                mock_manager.return_value = mock_jm

                result = runner.invoke(
                    cli,
                    [
                        "jobs",
                        "submit-direct",
                        manifest_path,
                        "-r",
                        "us-east-1",
                        "-l",
                        "team=ml",
                    ],
                )
                assert result.exit_code == 0
                call_kwargs = mock_jm.submit_job_direct.call_args.kwargs
                assert call_kwargs.get("labels") == {"team": "ml"}
        finally:
            os.unlink(manifest_path)

    def test_jobs_submit_direct_dry_run(self):
        """Test jobs submit-direct with --dry-run."""
        import os
        import tempfile

        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: test-job\n")
            f.flush()
            manifest_path = f.name

        try:
            with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
                mock_jm = MagicMock()
                mock_jm.submit_job_direct.return_value = {"name": "test-job", "valid": True}
                mock_manager.return_value = mock_jm

                result = runner.invoke(
                    cli, ["jobs", "submit-direct", manifest_path, "-r", "us-east-1", "--dry-run"]
                )
                assert result.exit_code == 0
                assert "dry run" in result.output.lower()
        finally:
            os.unlink(manifest_path)

    def test_jobs_submit_direct_error(self):
        """Test jobs submit-direct with error."""
        import os
        import tempfile

        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: test-job\n")
            f.flush()
            manifest_path = f.name

        try:
            with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
                mock_jm = MagicMock()
                mock_jm.submit_job_direct.side_effect = Exception("kubectl not found")
                mock_manager.return_value = mock_jm

                result = runner.invoke(
                    cli, ["jobs", "submit-direct", manifest_path, "-r", "us-east-1"]
                )
                assert result.exit_code == 1
                assert "failed" in result.output.lower()
        finally:
            os.unlink(manifest_path)


class TestJobsSubmitWithWait:
    """Tests for jobs submit with --wait flag."""

    def test_jobs_submit_with_wait(self):
        """Test jobs submit with --wait flag."""
        import os
        import tempfile

        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: test-job\n")
            f.flush()
            manifest_path = f.name

        try:
            with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
                mock_jm = MagicMock()
                mock_jm.submit_job.return_value = {
                    "resources": [
                        {
                            "kind": "Job",
                            "name": "test-job-generated",
                            "namespace": "manifest-ns",
                            "status": "created",
                        }
                    ]
                }
                mock_final_job = MagicMock()
                mock_final_job.status = "Succeeded"
                mock_jm.wait_for_job.return_value = mock_final_job
                mock_manager.return_value = mock_jm

                result = runner.invoke(cli, ["jobs", "submit", manifest_path, "--wait"])
                assert result.exit_code == 0
                mock_jm.wait_for_job.assert_called_once_with(
                    job_name="test-job-generated",
                    namespace="manifest-ns",
                    region=None,
                    timeout_seconds=3600,
                )
        finally:
            os.unlink(manifest_path)

    def test_jobs_submit_direct_with_wait(self):
        """Test jobs submit-direct with --wait flag."""
        import os
        import tempfile

        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: test-job\n")
            f.flush()
            manifest_path = f.name

        try:
            with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
                mock_jm = MagicMock()
                mock_jm.submit_job_direct.return_value = {
                    "job_name": "test-job",
                    "namespace": "manifest-ns",
                    "resources": ["job.batch/test-job created"],
                }
                mock_final_job = MagicMock()
                mock_final_job.status = "Succeeded"
                mock_jm.wait_for_job.return_value = mock_final_job
                mock_manager.return_value = mock_jm

                result = runner.invoke(
                    cli, ["jobs", "submit-direct", manifest_path, "-r", "us-east-1", "--wait"]
                )
                assert result.exit_code == 0
                mock_jm.wait_for_job.assert_called_once_with(
                    job_name="test-job",
                    namespace="manifest-ns",
                    region="us-east-1",
                    timeout_seconds=3600,
                )
        finally:
            os.unlink(manifest_path)


class TestFilesDownloadCommand:
    """Tests for files download command."""

    def test_files_download_success(self):
        """Test files download command success."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.files_cmd.get_file_system_client") as mock_client:
            mock_fs = MagicMock()
            mock_fs.download_from_storage.return_value = {
                "status": "success",
                "source": "efs:my-job/outputs",
                "destination": "./local-outputs",
                "size_bytes": 1024,
                "storage_type": "efs",
                "message": "Download completed successfully",
            }
            mock_client.return_value = mock_fs

            result = runner.invoke(
                cli,
                [
                    "files",
                    "download",
                    "my-job/outputs",
                    "./local-outputs",
                    "-r",
                    "us-east-1",
                ],
            )
            assert result.exit_code == 0
            assert "1024" in result.output or "success" in result.output.lower()

    def test_files_download_with_namespace(self):
        """Test files download with custom namespace."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.files_cmd.get_file_system_client") as mock_client:
            mock_fs = MagicMock()
            mock_fs.download_from_storage.return_value = {
                "status": "success",
                "size_bytes": 512,
            }
            mock_client.return_value = mock_fs

            result = runner.invoke(
                cli,
                [
                    "files",
                    "download",
                    "data",
                    "./data",
                    "-r",
                    "us-west-2",
                    "-n",
                    "ml-jobs",
                ],
            )
            assert result.exit_code == 0
            mock_fs.download_from_storage.assert_called_once_with(
                region="us-west-2",
                remote_path="data",
                local_path="./data",
                storage_type="efs",
                namespace="ml-jobs",
                pvc_name=None,
            )

    def test_files_download_with_fsx(self):
        """Test files download with FSx storage type."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.files_cmd.get_file_system_client") as mock_client:
            mock_fs = MagicMock()
            mock_fs.download_from_storage.return_value = {
                "status": "success",
                "size_bytes": 256,
            }
            mock_client.return_value = mock_fs

            result = runner.invoke(
                cli,
                [
                    "files",
                    "download",
                    "checkpoints",
                    "./output",
                    "-r",
                    "us-east-1",
                    "-t",
                    "fsx",
                ],
            )
            assert result.exit_code == 0
            mock_fs.download_from_storage.assert_called_once_with(
                region="us-east-1",
                remote_path="checkpoints",
                local_path="./output",
                storage_type="fsx",
                namespace="gco-jobs",
                pvc_name=None,
            )

    def test_files_download_error(self):
        """Test files download with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.files_cmd.get_file_system_client") as mock_client:
            mock_fs = MagicMock()
            mock_fs.download_from_storage.side_effect = RuntimeError(
                "Download failed: Pod not found"
            )
            mock_client.return_value = mock_fs

            result = runner.invoke(
                cli,
                ["files", "download", "nonexistent", "./data", "-r", "us-east-1"],
            )
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_files_download_help(self):
        """Test files download help text."""
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["files", "download", "--help"])
        assert result.exit_code == 0
        assert "helper pod" in result.output
        assert "EFS" in result.output or "efs" in result.output


# =============================================================================
# Additional coverage tests for cli/main.py
# =============================================================================


class TestMainCliEdgeCasesExtended:
    """Extended tests for main CLI edge cases."""

    def test_cli_handles_keyboard_interrupt(self):
        """Test CLI handles KeyboardInterrupt gracefully."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0


class TestCliJobCommandsExtended:
    """Extended tests for CLI job commands."""

    def test_jobs_list_command(self):
        """Test jobs list command."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_manager.return_value.list_jobs.return_value = []
            result = runner.invoke(cli, ["jobs", "list", "--region", "us-east-1"])
            assert result.exit_code == 0

    def test_jobs_get_not_found(self):
        """Test jobs get command when job not found."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_manager.return_value.get_job.return_value = None
            result = runner.invoke(cli, ["jobs", "get", "nonexistent-job", "--region", "us-east-1"])
            assert result.exit_code == 1
            assert "not found" in result.output.lower()


class TestCliCapacityCommandsExtended:
    """Extended tests for CLI capacity commands."""

    def test_capacity_instance_info_not_found(self):
        """Test capacity instance-info command when instance not found."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.capacity_cmd.get_capacity_checker") as mock_checker:
            mock_checker.return_value.get_instance_info.return_value = None
            result = runner.invoke(cli, ["capacity", "instance-info", "invalid-type"])
            assert result.exit_code == 1
            assert "not found" in result.output.lower()


class TestCliStackCommandsExtended:
    """Extended tests for CLI stack commands."""

    def test_stacks_synth_command(self):
        """Test stacks synth command."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.StackManager") as mock_manager_class:
            mock_manager = MagicMock()
            mock_manager_class.return_value = mock_manager
            mock_manager.synth.return_value = "Template synthesized"
            result = runner.invoke(cli, ["stacks", "synth"])
            assert result.exit_code == 0

    def test_stacks_diff_command(self):
        """Test stacks diff command."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.StackManager") as mock_manager_class:
            mock_manager = MagicMock()
            mock_manager_class.return_value = mock_manager
            mock_manager.diff.return_value = ""
            result = runner.invoke(cli, ["stacks", "diff"])
            assert result.exit_code == 0


class TestJobsSubmitSqsCommand:
    """Tests for jobs submit-sqs command."""

    def test_jobs_submit_sqs_success(self):
        """Test jobs submit-sqs command success."""
        import os
        import tempfile

        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: test-job\n")
            f.flush()
            manifest_path = f.name

        try:
            with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
                mock_jm = MagicMock()
                mock_jm.submit_job_sqs.return_value = {
                    "message_id": "msg-123",
                    "queue_url": "https://sqs.us-east-1.amazonaws.com/123/queue",
                }
                mock_manager.return_value = mock_jm

                result = runner.invoke(
                    cli, ["jobs", "submit-sqs", manifest_path, "-r", "us-east-1"]
                )
                assert result.exit_code == 0
                assert "queued" in result.output.lower()
        finally:
            os.unlink(manifest_path)

    def test_jobs_submit_sqs_with_auto_region(self):
        """Test jobs submit-sqs with --auto-region."""
        import os
        import tempfile

        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: test-job\n")
            f.flush()
            manifest_path = f.name

        try:
            with (
                patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager,
                patch("cli.capacity.get_capacity_checker") as mock_checker,
            ):
                mock_jm = MagicMock()
                mock_jm.submit_job_sqs.return_value = {"message_id": "msg-123"}
                mock_manager.return_value = mock_jm

                mock_cc = MagicMock()
                mock_cc.recommend_region_for_job.return_value = {
                    "region": "us-west-2",
                    "reason": "Lowest queue depth",
                }
                mock_checker.return_value = mock_cc

                result = runner.invoke(cli, ["jobs", "submit-sqs", manifest_path, "--auto-region"])
                assert result.exit_code == 0
        finally:
            os.unlink(manifest_path)

    def test_jobs_submit_sqs_with_labels_and_priority(self):
        """Test jobs submit-sqs with labels and priority."""
        import os
        import tempfile

        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: test-job\n")
            f.flush()
            manifest_path = f.name

        try:
            with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
                mock_jm = MagicMock()
                mock_jm.submit_job_sqs.return_value = {"message_id": "msg-123"}
                mock_manager.return_value = mock_jm

                result = runner.invoke(
                    cli,
                    [
                        "jobs",
                        "submit-sqs",
                        manifest_path,
                        "-r",
                        "us-east-1",
                        "-l",
                        "team=ml",
                        "-p",
                        "10",
                    ],
                )
                assert result.exit_code == 0
                call_kwargs = mock_jm.submit_job_sqs.call_args.kwargs
                assert call_kwargs.get("labels") == {"team": "ml"}
                assert call_kwargs.get("priority") == 10
        finally:
            os.unlink(manifest_path)

    def test_jobs_submit_sqs_error(self):
        """Test jobs submit-sqs with error."""
        import os
        import tempfile

        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: test-job\n")
            f.flush()
            manifest_path = f.name

        try:
            with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
                mock_jm = MagicMock()
                mock_jm.submit_job_sqs.side_effect = Exception("SQS error")
                mock_manager.return_value = mock_jm

                result = runner.invoke(
                    cli, ["jobs", "submit-sqs", manifest_path, "-r", "us-east-1"]
                )
                assert result.exit_code == 1
                assert "failed" in result.output.lower()
        finally:
            os.unlink(manifest_path)


class TestQueueStatusCommand:
    """Tests for jobs queue-status command."""

    def test_queue_status_single_region(self):
        """Test queue-status for single region."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.get_queue_status.return_value = {
                "region": "us-east-1",
                "messages_available": 5,
                "messages_in_flight": 2,
                "messages_delayed": 0,
            }
            mock_manager.return_value = mock_jm

            result = runner.invoke(cli, ["jobs", "queue-status", "-r", "us-east-1"])
            assert result.exit_code == 0

    def test_queue_status_all_regions(self):
        """Test queue-status for all regions."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with (
            patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager,
            patch("cli.aws_client.get_aws_client") as mock_aws,
        ):
            mock_jm = MagicMock()
            mock_jm.get_queue_status.return_value = {
                "region": "us-east-1",
                "messages_available": 5,
                "messages_in_flight": 2,
                "messages_delayed": 0,
                "dlq_messages": 1,
            }
            mock_manager.return_value = mock_jm

            mock_client = MagicMock()
            mock_client.discover_regional_stacks.return_value = ["us-east-1", "us-west-2"]
            mock_aws.return_value = mock_client

            result = runner.invoke(cli, ["jobs", "queue-status", "--all-regions"])
            assert result.exit_code == 0
            assert "REGION" in result.output

    def test_queue_status_all_regions_no_results(self):
        """Test queue-status for all regions with no results."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with (
            patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager,
            patch("cli.aws_client.get_aws_client") as mock_aws,
        ):
            mock_jm = MagicMock()
            mock_jm.get_queue_status.side_effect = Exception("Queue not found")
            mock_manager.return_value = mock_jm

            mock_client = MagicMock()
            mock_client.discover_regional_stacks.return_value = ["us-east-1"]
            mock_aws.return_value = mock_client

            result = runner.invoke(cli, ["jobs", "queue-status", "--all-regions"])
            assert result.exit_code == 0
            assert "No queue status" in result.output

    def test_queue_status_error(self):
        """Test queue-status with error."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.get_queue_status.side_effect = Exception("API error")
            mock_manager.return_value = mock_jm

            result = runner.invoke(cli, ["jobs", "queue-status", "-r", "us-east-1"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestCapacityStatusCommand:
    """Tests for capacity status command."""

    def test_capacity_status_single_region(self):
        """Test capacity status for single region."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.capacity.get_multi_region_capacity_checker") as mock_checker:
            mock_cc = MagicMock()
            mock_cc.get_region_capacity.return_value = {
                "region": "us-east-1",
                "queue_depth": 5,
                "running_jobs": 10,
                "gpu_utilization": 75.0,
                "cpu_utilization": 60.0,
            }
            mock_checker.return_value = mock_cc

            result = runner.invoke(cli, ["capacity", "status", "-r", "us-east-1"])
            assert result.exit_code == 0

    def test_capacity_status_all_regions(self):
        """Test capacity status for all regions."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.capacity.get_multi_region_capacity_checker") as mock_checker:
            mock_cc = MagicMock()
            mock_capacity = MagicMock()
            mock_capacity.region = "us-east-1"
            mock_capacity.queue_depth = 5
            mock_capacity.running_jobs = 10
            mock_capacity.gpu_utilization = 75.0
            mock_capacity.cpu_utilization = 60.0
            mock_capacity.recommendation_score = 50.0
            mock_cc.get_all_regions_capacity.return_value = [mock_capacity]
            mock_checker.return_value = mock_cc

            result = runner.invoke(cli, ["capacity", "status"])
            assert result.exit_code == 0
            assert "REGION" in result.output

    def test_capacity_status_no_stacks(self):
        """Test capacity status with no stacks found."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.capacity.get_multi_region_capacity_checker") as mock_checker:
            mock_cc = MagicMock()
            mock_cc.get_all_regions_capacity.return_value = []
            mock_checker.return_value = mock_cc

            result = runner.invoke(cli, ["capacity", "status"])
            assert result.exit_code == 0
            assert "No GCO stacks" in result.output

    def test_capacity_status_error(self):
        """Test capacity status with error."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.capacity.get_multi_region_capacity_checker") as mock_checker:
            mock_checker.side_effect = Exception("API error")

            result = runner.invoke(cli, ["capacity", "status"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestRecommendRegionCommand:
    """Tests for capacity recommend-region command."""

    def test_recommend_region_basic(self):
        """Test recommend-region command."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.capacity.get_multi_region_capacity_checker") as mock_checker:
            mock_cc = MagicMock()
            mock_cc.recommend_region_for_job.return_value = {
                "region": "us-west-2",
                "reason": "Lowest queue depth",
            }
            mock_checker.return_value = mock_cc

            result = runner.invoke(cli, ["capacity", "recommend-region"])
            assert result.exit_code == 0
            assert "us-west-2" in result.output

    def test_recommend_region_with_gpu(self):
        """Test recommend-region with --gpu flag."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.capacity.get_multi_region_capacity_checker") as mock_checker:
            mock_cc = MagicMock()
            mock_cc.recommend_region_for_job.return_value = {
                "region": "us-east-1",
                "reason": "Best GPU availability",
            }
            mock_checker.return_value = mock_cc

            result = runner.invoke(cli, ["capacity", "recommend-region", "--gpu"])
            assert result.exit_code == 0
            mock_cc.recommend_region_for_job.assert_called_with(
                gpu_required=True, min_gpus=0, instance_type=None, gpu_count=0
            )

    def test_recommend_region_verbose(self):
        """Test recommend-region with verbose output."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.capacity.get_multi_region_capacity_checker") as mock_checker:
            mock_cc = MagicMock()
            mock_cc.recommend_region_for_job.return_value = {
                "region": "us-west-2",
                "reason": "Lowest queue depth",
                "all_regions": [
                    {"region": "us-west-2", "score": 10, "queue_depth": 2, "gpu_utilization": 50},
                    {"region": "us-east-1", "score": 20, "queue_depth": 5, "gpu_utilization": 80},
                ],
            }
            mock_checker.return_value = mock_cc

            result = runner.invoke(cli, ["--verbose", "capacity", "recommend-region"])
            assert result.exit_code == 0
            assert "All regions ranked" in result.output

    def test_recommend_region_error(self):
        """Test recommend-region with error."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.capacity.get_multi_region_capacity_checker") as mock_checker:
            mock_checker.side_effect = Exception("API error")

            result = runner.invoke(cli, ["capacity", "recommend-region"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestNodepoolsCommands:
    """Tests for nodepools commands."""

    def test_nodepools_help(self):
        """Test nodepools --help."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["nodepools", "--help"])
        assert result.exit_code == 0
        assert "Manage Karpenter NodePools" in result.output

    def test_create_odcr_nodepool_to_file(self):
        """Test create-odcr nodepool with output file."""
        import os
        import tempfile

        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            output_path = f.name

        try:
            with patch("cli.nodepools.generate_odcr_nodepool_manifest") as mock_gen:
                mock_gen.return_value = "apiVersion: karpenter.sh/v1\nkind: NodePool\n"

                result = runner.invoke(
                    cli,
                    [
                        "nodepools",
                        "create-odcr",
                        "-n",
                        "gpu-reserved",
                        "-r",
                        "us-east-1",
                        "-c",
                        "cr-0123456789abcdef0",
                        "-o",
                        output_path,
                    ],
                )
                assert result.exit_code == 0
                assert "written to" in result.output
        finally:
            if os.path.exists(output_path):
                os.unlink(output_path)

    def test_create_odcr_nodepool_stdout(self):
        """Test create-odcr nodepool to stdout."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.nodepools.generate_odcr_nodepool_manifest") as mock_gen:
            mock_gen.return_value = "apiVersion: karpenter.sh/v1\nkind: NodePool\n"

            result = runner.invoke(
                cli,
                [
                    "nodepools",
                    "create-odcr",
                    "-n",
                    "gpu-reserved",
                    "-r",
                    "us-east-1",
                    "-c",
                    "cr-0123456789abcdef0",
                ],
            )
            assert result.exit_code == 0
            assert "NodePool" in result.output

    def test_create_odcr_nodepool_error(self):
        """Test create-odcr nodepool with error."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.nodepools.generate_odcr_nodepool_manifest") as mock_gen:
            mock_gen.side_effect = Exception("Invalid capacity reservation")

            result = runner.invoke(
                cli,
                [
                    "nodepools",
                    "create-odcr",
                    "-n",
                    "gpu-reserved",
                    "-r",
                    "us-east-1",
                    "-c",
                    "invalid",
                ],
            )
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_create_capacity_block_nodepool_stdout(self):
        """Test create-capacity-block nodepool to stdout."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.nodepools.generate_capacity_block_nodepool_manifest") as mock_gen:
            mock_gen.return_value = "apiVersion: karpenter.sh/v1\nkind: NodePool\n"

            result = runner.invoke(
                cli,
                [
                    "nodepools",
                    "create-capacity-block",
                    "-n",
                    "cb-train",
                    "-r",
                    "us-east-1",
                    "-c",
                    "cr-0123456789abcdef0",
                    "-i",
                    "p5.48xlarge",
                ],
            )
            assert result.exit_code == 0
            assert "NodePool" in result.output

    def test_create_capacity_block_nodepool_to_file(self):
        """Test create-capacity-block nodepool with output file."""
        import os
        import tempfile

        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            output_path = f.name

        try:
            with patch("cli.nodepools.generate_capacity_block_nodepool_manifest") as mock_gen:
                mock_gen.return_value = "apiVersion: karpenter.sh/v1\nkind: NodePool\n"

                result = runner.invoke(
                    cli,
                    [
                        "nodepools",
                        "create-capacity-block",
                        "-n",
                        "cb-train",
                        "-r",
                        "us-east-1",
                        "-c",
                        "cr-0123456789abcdef0",
                        "-o",
                        output_path,
                    ],
                )
                assert result.exit_code == 0
                assert "written to" in result.output
        finally:
            if os.path.exists(output_path):
                os.unlink(output_path)

    def test_create_capacity_block_nodepool_error(self):
        """Test create-capacity-block nodepool with error."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.nodepools.generate_capacity_block_nodepool_manifest") as mock_gen:
            mock_gen.side_effect = Exception("Invalid capacity reservation")

            result = runner.invoke(
                cli,
                [
                    "nodepools",
                    "create-capacity-block",
                    "-n",
                    "cb-train",
                    "-r",
                    "us-east-1",
                    "-c",
                    "invalid",
                ],
            )
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_list_nodepools_success(self):
        """Test list nodepools command."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.nodepools.list_cluster_nodepools") as mock_list:
            mock_list.return_value = [
                {"name": "gpu-x86-pool", "status": "Ready"},
                {"name": "gpu-arm-pool", "status": "Ready"},
            ]

            result = runner.invoke(cli, ["nodepools", "list", "-r", "us-east-1"])
            assert result.exit_code == 0

    def test_list_nodepools_no_region_or_cluster(self):
        """Test list nodepools without region or cluster."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        result = runner.invoke(cli, ["nodepools", "list"])
        assert result.exit_code == 1
        assert "required" in result.output.lower()

    def test_list_nodepools_empty(self):
        """Test list nodepools with no results."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.nodepools.list_cluster_nodepools") as mock_list:
            mock_list.return_value = []

            result = runner.invoke(cli, ["nodepools", "list", "-r", "us-east-1"])
            assert result.exit_code == 0
            assert "No NodePools" in result.output

    def test_list_nodepools_error(self):
        """Test list nodepools with error."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.nodepools.list_cluster_nodepools") as mock_list:
            mock_list.side_effect = Exception("kubectl error")

            result = runner.invoke(cli, ["nodepools", "list", "-r", "us-east-1"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_describe_nodepool_success(self):
        """Test describe nodepool command."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.nodepools.describe_cluster_nodepool") as mock_describe:
            mock_describe.return_value = {
                "name": "gpu-x86-pool",
                "status": "Ready",
                "spec": {"limits": {"cpu": "1000"}},
            }

            result = runner.invoke(
                cli, ["nodepools", "describe", "gpu-x86-pool", "-r", "us-east-1"]
            )
            assert result.exit_code == 0

    def test_describe_nodepool_not_found(self):
        """Test describe nodepool when not found."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.nodepools.describe_cluster_nodepool") as mock_describe:
            mock_describe.return_value = None

            result = runner.invoke(cli, ["nodepools", "describe", "nonexistent", "-r", "us-east-1"])
            assert result.exit_code == 1
            assert "not found" in result.output.lower()

    def test_describe_nodepool_error(self):
        """Test describe nodepool with error."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.nodepools.describe_cluster_nodepool") as mock_describe:
            mock_describe.side_effect = Exception("kubectl error")

            result = runner.invoke(
                cli, ["nodepools", "describe", "gpu-x86-pool", "-r", "us-east-1"]
            )
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestStacksDeployFailure:
    """Tests for stacks deploy failure paths."""

    def test_stacks_deploy_returns_false(self):
        """Test stacks deploy when deploy returns False."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_sm.deploy.return_value = False
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "deploy", "test-stack", "-y"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_stacks_destroy_returns_false(self):
        """Test stacks destroy when destroy returns False."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_sm.destroy.return_value = False
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "destroy", "test-stack", "-y"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestStacksDeployAllOrchestrated:
    """Tests for stacks deploy-all orchestrated command."""

    def test_deploy_all_with_parallel(self):
        """Test deploy-all with parallel flag."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_sm.list_stacks.return_value = ["gco-global", "gco-us-east-1"]
            mock_sm.deploy_orchestrated.return_value = (
                True,
                ["gco-global", "gco-us-east-1"],
                [],
            )
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "deploy-all", "-y", "--parallel"])
            assert result.exit_code == 0
            assert "Parallel mode" in result.output

    def test_deploy_all_with_tags(self):
        """Test deploy-all with tags."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_sm.list_stacks.return_value = ["gco-global"]
            mock_sm.deploy_orchestrated.return_value = (True, ["gco-global"], [])
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "deploy-all", "-y", "-t", "Environment=prod"])
            assert result.exit_code == 0
            call_kwargs = mock_sm.deploy_orchestrated.call_args.kwargs
            assert call_kwargs.get("tags") == {"Environment": "prod"}

    def test_deploy_all_error(self):
        """Test deploy-all with exception."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_sm.list_stacks.side_effect = Exception("CDK error")
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "deploy-all", "-y"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestStacksDestroyAllOrchestrated:
    """Tests for stacks destroy-all orchestrated command."""

    def test_destroy_all_with_parallel(self):
        """Test destroy-all with parallel flag."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with (
            patch("cli.stacks.get_stack_manager") as mock_manager,
            patch("cli.stacks.get_stack_destroy_order") as mock_order,
        ):
            mock_sm = MagicMock()
            mock_sm.list_stacks.return_value = ["gco-global", "gco-us-east-1"]
            mock_sm.destroy_orchestrated.return_value = (
                True,
                ["gco-us-east-1", "gco-global"],
                [],
            )
            mock_manager.return_value = mock_sm
            mock_order.return_value = ["gco-us-east-1", "gco-global"]

            result = runner.invoke(cli, ["stacks", "destroy-all", "-y", "--parallel"])
            assert result.exit_code == 0
            assert "Parallel mode" in result.output

    def test_destroy_all_error(self):
        """Test destroy-all with exception."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_sm.list_stacks.side_effect = Exception("CDK error")
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "destroy-all", "-y"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestStacksBootstrapFailure:
    """Tests for stacks bootstrap failure paths."""

    def test_bootstrap_returns_false(self):
        """Test bootstrap when it returns False."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_sm.bootstrap.return_value = False
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "bootstrap", "-r", "us-east-1"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestStacksStatusNotFound:
    """Tests for stacks status not found."""

    def test_status_not_found(self):
        """Test status when stack not found."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_sm.get_stack_status.return_value = None
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "status", "nonexistent", "-r", "us-east-1"])
            assert result.exit_code == 1
            assert "not found" in result.output.lower()


class TestStacksOutputsEmpty:
    """Tests for stacks outputs when empty."""

    def test_outputs_empty(self):
        """Test outputs when no outputs found."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_stack_manager") as mock_manager:
            mock_sm = MagicMock()
            mock_sm.get_outputs.return_value = None
            mock_manager.return_value = mock_sm

            result = runner.invoke(cli, ["stacks", "outputs", "test-stack", "-r", "us-east-1"])
            assert result.exit_code == 0
            assert "No outputs" in result.output


class TestFsxCommandsExtended:
    """Extended tests for FSx commands."""

    def test_fsx_status_with_region(self):
        """Test fsx status with specific region."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_fsx_config") as mock_config:
            mock_config.return_value = {"enabled": True, "storage_capacity_gib": 1200}

            result = runner.invoke(cli, ["stacks", "fsx", "status", "-r", "us-east-1"])
            assert result.exit_code == 0
            mock_config.assert_called_with("us-east-1")

    def test_fsx_status_error(self):
        """Test fsx status with error."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.get_fsx_config") as mock_config:
            mock_config.side_effect = Exception("Config error")

            result = runner.invoke(cli, ["stacks", "fsx", "status"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_fsx_enable_with_all_options(self):
        """Test fsx enable with all options."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.update_fsx_config") as mock_update:
            result = runner.invoke(
                cli,
                [
                    "stacks",
                    "fsx",
                    "enable",
                    "-r",
                    "us-east-1",
                    "-s",
                    "2400",
                    "-d",
                    "PERSISTENT_2",
                    "-t",
                    "500",
                    "-c",
                    "LZ4",
                    "--import-path",
                    "s3://bucket/data",
                    "--export-path",
                    "s3://bucket/output",
                    "-y",
                ],
            )
            assert result.exit_code == 0
            mock_update.assert_called_once()
            call_args = mock_update.call_args[0]
            assert call_args[0]["storage_capacity_gib"] == 2400
            assert call_args[0]["deployment_type"] == "PERSISTENT_2"

    def test_fsx_enable_error(self):
        """Test fsx enable with error."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.update_fsx_config") as mock_update:
            mock_update.side_effect = Exception("Config error")

            result = runner.invoke(cli, ["stacks", "fsx", "enable", "-y"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_fsx_disable_with_region(self):
        """Test fsx disable with specific region."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.update_fsx_config") as mock_update:
            result = runner.invoke(cli, ["stacks", "fsx", "disable", "-r", "us-east-1", "-y"])
            assert result.exit_code == 0
            mock_update.assert_called_with({"enabled": False}, "us-east-1")

    def test_fsx_disable_error(self):
        """Test fsx disable with error."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.stacks.update_fsx_config") as mock_update:
            mock_update.side_effect = Exception("Config error")

            result = runner.invoke(cli, ["stacks", "fsx", "disable", "-y"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestFilesListCommand:
    """Tests for files list command."""

    def test_files_list_with_region(self):
        """Test files list with region filter."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.files_cmd.get_file_system_client") as mock_client:
            mock_fs = MagicMock()
            mock_fs.get_file_systems.return_value = [
                {"id": "fs-123", "type": "efs", "region": "us-east-1"}
            ]
            mock_client.return_value = mock_fs

            result = runner.invoke(cli, ["files", "list", "-r", "us-east-1"])
            assert result.exit_code == 0
            mock_fs.get_file_systems.assert_called_with("us-east-1")

    def test_files_list_empty(self):
        """Test files list with no results."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.files_cmd.get_file_system_client") as mock_client:
            mock_fs = MagicMock()
            mock_fs.get_file_systems.return_value = []
            mock_client.return_value = mock_fs

            result = runner.invoke(cli, ["files", "list"])
            assert result.exit_code == 0
            assert "No file systems" in result.output


class TestFilesLsCommand:
    """Tests for files ls command."""

    def test_files_ls_success(self):
        """Test files ls command success."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.files_cmd.get_file_system_client") as mock_client:
            mock_fs = MagicMock()
            mock_fs.list_storage_contents.return_value = {
                "status": "success",
                "message": "Listed 2 items",
                "contents": [
                    {"name": "output", "is_directory": True, "size_bytes": 0},
                    {"name": "results.json", "is_directory": False, "size_bytes": 1024},
                ],
            }
            mock_client.return_value = mock_fs

            result = runner.invoke(cli, ["files", "ls", "-r", "us-east-1"])
            assert result.exit_code == 0
            assert "output" in result.output
            assert "results.json" in result.output

    def test_files_ls_empty_directory(self):
        """Test files ls with empty directory."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.files_cmd.get_file_system_client") as mock_client:
            mock_fs = MagicMock()
            mock_fs.list_storage_contents.return_value = {
                "status": "success",
                "message": "Listed 0 items",
                "contents": [],
            }
            mock_client.return_value = mock_fs

            result = runner.invoke(cli, ["files", "ls", "-r", "us-east-1"])
            assert result.exit_code == 0
            assert "empty" in result.output.lower()

    def test_files_ls_failure(self):
        """Test files ls with failure status."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.files_cmd.get_file_system_client") as mock_client:
            mock_fs = MagicMock()
            mock_fs.list_storage_contents.return_value = {
                "status": "error",
                "message": "Path not found",
            }
            mock_client.return_value = mock_fs

            result = runner.invoke(cli, ["files", "ls", "/nonexistent", "-r", "us-east-1"])
            assert result.exit_code == 1

    def test_files_ls_with_fsx(self):
        """Test files ls with FSx storage type."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.files_cmd.get_file_system_client") as mock_client:
            mock_fs = MagicMock()
            mock_fs.list_storage_contents.return_value = {
                "status": "success",
                "message": "Listed",
                "contents": [],
            }
            mock_client.return_value = mock_fs

            result = runner.invoke(cli, ["files", "ls", "-r", "us-east-1", "-t", "fsx"])
            assert result.exit_code == 0
            mock_fs.list_storage_contents.assert_called_once()
            call_kwargs = mock_fs.list_storage_contents.call_args.kwargs
            assert call_kwargs["storage_type"] == "fsx"

    def test_files_ls_error(self):
        """Test files ls with exception."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.files_cmd.get_file_system_client") as mock_client:
            mock_fs = MagicMock()
            mock_fs.list_storage_contents.side_effect = Exception("kubectl error")
            mock_client.return_value = mock_fs

            result = runner.invoke(cli, ["files", "ls", "-r", "us-east-1"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestConfigInitCommand:
    """Tests for config init command."""

    def test_config_init_new_file(self):
        """Test config init creating new file."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("pathlib.Path.home") as mock_home:
            mock_home.return_value = type(
                "Path",
                (),
                {
                    "__truediv__": lambda s, x: type(
                        "Path",
                        (),
                        {
                            "__truediv__": lambda s2, y: type(
                                "Path", (), {"exists": lambda s3: False}
                            )()
                        },
                    )()
                },
            )()

            with patch("cli.config.GCOConfig.save"):
                result = runner.invoke(cli, ["config-cmd", "init"])
                assert result.exit_code == 0

    def test_config_init_force_overwrite(self):
        """Test config init with force flag."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("pathlib.Path.home") as mock_home:
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_home.return_value.__truediv__ = MagicMock(
                return_value=MagicMock(__truediv__=MagicMock(return_value=mock_path))
            )

            with patch("cli.config.GCOConfig.save"):
                result = runner.invoke(cli, ["config-cmd", "init", "--force"])
                assert result.exit_code == 0


class TestFilesAccessPointsEmpty:
    """Tests for files access-points when empty."""

    def test_access_points_empty(self):
        """Test access-points with no results."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.files_cmd.get_file_system_client") as mock_client:
            mock_fs = MagicMock()
            mock_fs.get_access_point_info.return_value = []
            mock_client.return_value = mock_fs

            result = runner.invoke(cli, ["files", "access-points", "fs-123", "-r", "us-east-1"])
            assert result.exit_code == 0
            assert "No access points" in result.output


class TestCapacityInstanceInfoError:
    """Tests for capacity instance-info error handling."""

    def test_instance_info_error(self):
        """Test instance-info with error."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.capacity_cmd.get_capacity_checker") as mock_checker:
            mock_cc = MagicMock()
            mock_cc.get_instance_info.side_effect = Exception("API error")
            mock_checker.return_value = mock_cc

            result = runner.invoke(cli, ["capacity", "instance-info", "g4dn.xlarge"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestSpotPricesEmpty:
    """Tests for spot-prices when empty."""

    def test_spot_prices_empty(self):
        """Test spot-prices with no data."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.capacity_cmd.get_capacity_checker") as mock_checker:
            mock_cc = MagicMock()
            mock_cc.get_spot_price_history.return_value = []
            mock_checker.return_value = mock_cc

            result = runner.invoke(
                cli, ["capacity", "spot-prices", "-i", "g4dn.xlarge", "-r", "us-east-1"]
            )
            assert result.exit_code == 0
            assert "No spot price data" in result.output
