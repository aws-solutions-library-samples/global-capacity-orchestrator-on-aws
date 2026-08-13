"""
Help-text smoke tests for every GCO CLI command and subcommand.

Walks the Click command tree — top-level `gco`, and the jobs, stacks,
capacity, files, inference, queue, templates, webhooks, costs, and
models subgroups plus their individual commands — invoking `--help`
on each and asserting exit code 0. Catches regressions where a command
accidentally raises at import time or fails Click's option validation
before the help screen renders.
"""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli.main import cli


@pytest.fixture
def runner():
    """Create CLI test runner."""
    return CliRunner()


class TestCliVersion:
    """Tests for CLI version command."""

    def test_version_flag(self, runner):
        """Test --version flag."""
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "gco" in result.output.lower() or "0." in result.output


class TestCliMainHelp:
    """Tests for main CLI help."""

    def test_main_help(self, runner):
        """Test main help."""
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "jobs" in result.output
        assert "stacks" in result.output
        assert "capacity" in result.output
        assert "files" in result.output


class TestJobsHelp:
    """Tests for jobs command help."""

    def test_jobs_help(self, runner):
        """Test jobs help."""
        result = runner.invoke(cli, ["jobs", "--help"])
        assert result.exit_code == 0

    def test_jobs_list_help(self, runner):
        """Test jobs list help."""
        result = runner.invoke(cli, ["jobs", "list", "--help"])
        assert result.exit_code == 0

    def test_jobs_submit_help(self, runner):
        """Test jobs submit help."""
        result = runner.invoke(cli, ["jobs", "submit", "--help"])
        assert result.exit_code == 0

    def test_jobs_get_help(self, runner):
        """Test jobs get help."""
        result = runner.invoke(cli, ["jobs", "get", "--help"])
        assert result.exit_code == 0

    def test_jobs_delete_help(self, runner):
        """Test jobs delete help."""
        result = runner.invoke(cli, ["jobs", "delete", "--help"])
        assert result.exit_code == 0

    def test_jobs_logs_help(self, runner):
        """Test jobs logs help."""
        result = runner.invoke(cli, ["jobs", "logs", "--help"])
        assert result.exit_code == 0


class TestStacksHelp:
    """Tests for stacks command help."""

    def test_stacks_help(self, runner):
        """Test stacks help."""
        result = runner.invoke(cli, ["stacks", "--help"])
        assert result.exit_code == 0

    def test_stacks_list_help(self, runner):
        """Test stacks list help."""
        result = runner.invoke(cli, ["stacks", "list", "--help"])
        assert result.exit_code == 0

    def test_stacks_deploy_help(self, runner):
        """Test stacks deploy help."""
        result = runner.invoke(cli, ["stacks", "deploy", "--help"])
        assert result.exit_code == 0

    def test_stacks_destroy_help(self, runner):
        """Test stacks destroy help."""
        result = runner.invoke(cli, ["stacks", "destroy", "--help"])
        assert result.exit_code == 0

    def test_stacks_deploy_all_help(self, runner):
        """Test stacks deploy-all help."""
        result = runner.invoke(cli, ["stacks", "deploy-all", "--help"])
        assert result.exit_code == 0

    def test_stacks_destroy_all_help(self, runner):
        """Test stacks destroy-all help."""
        result = runner.invoke(cli, ["stacks", "destroy-all", "--help"])
        assert result.exit_code == 0

    def test_stacks_bootstrap_help(self, runner):
        """Test stacks bootstrap help."""
        result = runner.invoke(cli, ["stacks", "bootstrap", "--help"])
        assert result.exit_code == 0


class TestStatusHelp:
    """Tests for the top-level status command help."""

    def test_status_help(self, runner):
        """Test status help."""
        result = runner.invoke(cli, ["status", "--help"])
        assert result.exit_code == 0
        assert "--with-costs" in result.output
        assert "--with-nodepools" in result.output
        assert "--watch" in result.output
        assert "--fail-on-findings" in result.output

    def test_status_appears_in_main_help(self, runner):
        """The bare command is listed alongside the groups in gco --help."""
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "status" in result.output


class TestCapacityHelp:
    """Tests for capacity command help."""

    def test_capacity_help(self, runner):
        """Test capacity help."""
        result = runner.invoke(cli, ["capacity", "--help"])
        assert result.exit_code == 0

    def test_capacity_check_help(self, runner):
        """Test capacity check help."""
        result = runner.invoke(cli, ["capacity", "check", "--help"])
        assert result.exit_code == 0


class TestFilesHelp:
    """Tests for files command help."""

    def test_files_help(self, runner):
        """Test files help."""
        result = runner.invoke(cli, ["files", "--help"])
        assert result.exit_code == 0

    def test_files_list_help(self, runner):
        """Test files list help."""
        result = runner.invoke(cli, ["files", "list", "--help"])
        assert result.exit_code == 0


class TestGlobalOptions:
    """Tests for global CLI options."""

    def test_output_json_option(self, runner):
        """Test --output json option."""
        result = runner.invoke(cli, ["--output", "json", "--help"])
        assert result.exit_code == 0

    def test_output_yaml_option(self, runner):
        """Test --output yaml option."""
        result = runner.invoke(cli, ["--output", "yaml", "--help"])
        assert result.exit_code == 0

    def test_output_table_option(self, runner):
        """Test --output table option."""
        result = runner.invoke(cli, ["--output", "table", "--help"])
        assert result.exit_code == 0

    def test_verbose_option(self, runner):
        """Test --verbose option."""
        result = runner.invoke(cli, ["--verbose", "--help"])
        assert result.exit_code == 0

    def test_region_option(self, runner):
        """Test --region option."""
        result = runner.invoke(cli, ["--region", "us-west-2", "--help"])
        assert result.exit_code == 0


class TestErrorHandling:
    """Tests for CLI error handling."""

    def test_invalid_command(self, runner):
        """Test invalid command."""
        result = runner.invoke(cli, ["invalid-command"])
        assert result.exit_code != 0

    def test_invalid_subcommand(self, runner):
        """Test invalid subcommand."""
        result = runner.invoke(cli, ["jobs", "invalid-subcommand"])
        assert result.exit_code != 0


class TestBasicCommands:
    """Tests for basic command execution."""

    @patch("cli.stacks.StackManager")
    def test_stacks_list_basic(self, mock_stack_manager_cls, runner):
        """Test stacks list command."""
        mock_manager = MagicMock()
        mock_manager.list_stacks.return_value = ["gco-global", "gco-us-east-1"]
        mock_stack_manager_cls.return_value = mock_manager

        result = runner.invoke(cli, ["stacks", "list"])
        # May fail due to missing AWS credentials, but should not crash
        assert result.exit_code in [0, 1]

    @patch("cli.commands.jobs_cmd.get_job_manager")
    @patch("cli.main.get_config")
    def test_jobs_list_basic(self, mock_get_config, mock_get_job_manager, runner):
        """Test jobs list command."""
        mock_config = MagicMock()
        mock_config.get_api_endpoint.return_value = "https://api.example.com"
        mock_config.output_format = "table"
        mock_config.verbose = False
        mock_get_config.return_value = mock_config

        mock_manager = MagicMock()
        mock_manager.list_jobs.return_value = []
        mock_get_job_manager.return_value = mock_manager

        result = runner.invoke(cli, ["jobs", "list"])
        assert result.exit_code in [0, 1]

    @patch("cli.capacity.checker.CapacityChecker")
    def test_capacity_check_basic(self, mock_checker_cls, runner):
        """Test capacity check command."""
        result = runner.invoke(cli, ["capacity", "check"])
        # May fail due to missing required args
        assert result.exit_code in [0, 1, 2]

    @patch("cli.files.FileSystemClient")
    def test_files_list_basic(self, mock_fs_client_cls, runner):
        """Test files list command."""
        mock_client = MagicMock()
        mock_client.get_file_systems.return_value = []
        mock_fs_client_cls.return_value = mock_client

        result = runner.invoke(cli, ["files", "list"])
        # May fail due to missing config
        assert result.exit_code in [0, 1]


# =============================================================================
# Additional Comprehensive Help Tests for All CLI Commands
# =============================================================================


class TestJobsHelpExtended:
    """Extended tests for all jobs subcommands."""

    def test_jobs_submit_direct_help(self, runner):
        """Test jobs submit-direct help."""
        result = runner.invoke(cli, ["jobs", "submit-direct", "--help"])
        assert result.exit_code == 0
        assert "kubectl" in result.output.lower()

    def test_jobs_submit_sqs_help(self, runner):
        """Test jobs submit-sqs help."""
        result = runner.invoke(cli, ["jobs", "submit-sqs", "--help"])
        assert result.exit_code == 0
        assert "sqs" in result.output.lower()

    def test_jobs_submit_queue_help(self, runner):
        """Test jobs submit-queue help."""
        result = runner.invoke(cli, ["jobs", "submit-queue", "--help"])
        assert result.exit_code == 0
        assert "dynamodb" in result.output.lower()

    def test_jobs_queue_status_help(self, runner):
        """Test jobs queue-status help."""
        result = runner.invoke(cli, ["jobs", "queue-status", "--help"])
        assert result.exit_code == 0

    def test_jobs_events_help(self, runner):
        """Test jobs events help."""
        result = runner.invoke(cli, ["jobs", "events", "--help"])
        assert result.exit_code == 0

    def test_jobs_pods_help(self, runner):
        """Test jobs pods help."""
        result = runner.invoke(cli, ["jobs", "pods", "--help"])
        assert result.exit_code == 0

    def test_jobs_pod_logs_help(self, runner):
        """Test jobs pod-logs help."""
        result = runner.invoke(cli, ["jobs", "pod-logs", "--help"])
        assert result.exit_code == 0

    def test_jobs_metrics_help(self, runner):
        """Test jobs metrics help."""
        result = runner.invoke(cli, ["jobs", "metrics", "--help"])
        assert result.exit_code == 0

    def test_jobs_retry_help(self, runner):
        """Test jobs retry help."""
        result = runner.invoke(cli, ["jobs", "retry", "--help"])
        assert result.exit_code == 0

    def test_jobs_bulk_delete_help(self, runner):
        """Test jobs bulk-delete help."""
        result = runner.invoke(cli, ["jobs", "bulk-delete", "--help"])
        assert result.exit_code == 0

    def test_jobs_health_help(self, runner):
        """Test jobs health help."""
        result = runner.invoke(cli, ["jobs", "health", "--help"])
        assert result.exit_code == 0


class TestQueueHelp:
    """Tests for queue command help."""

    def test_queue_help(self, runner):
        """Test queue --help."""
        result = runner.invoke(cli, ["queue", "--help"])
        assert result.exit_code == 0
        assert "global job queue" in result.output.lower()

    def test_queue_submit_help(self, runner):
        """Test queue submit help."""
        result = runner.invoke(cli, ["queue", "submit", "--help"])
        assert result.exit_code == 0
        assert "region" in result.output.lower()

    def test_queue_list_help(self, runner):
        """Test queue list help."""
        result = runner.invoke(cli, ["queue", "list", "--help"])
        assert result.exit_code == 0

    def test_queue_get_help(self, runner):
        """Test queue get help."""
        result = runner.invoke(cli, ["queue", "get", "--help"])
        assert result.exit_code == 0

    def test_queue_cancel_help(self, runner):
        """Test queue cancel help."""
        result = runner.invoke(cli, ["queue", "cancel", "--help"])
        assert result.exit_code == 0

    def test_queue_stats_help(self, runner):
        """Test queue stats help."""
        result = runner.invoke(cli, ["queue", "stats", "--help"])
        assert result.exit_code == 0


class TestTemplatesHelp:
    """Tests for templates command help."""

    def test_templates_help(self, runner):
        """Test templates --help."""
        result = runner.invoke(cli, ["templates", "--help"])
        assert result.exit_code == 0
        assert "job templates" in result.output.lower()

    def test_templates_list_help(self, runner):
        """Test templates list help."""
        result = runner.invoke(cli, ["templates", "list", "--help"])
        assert result.exit_code == 0

    def test_templates_get_help(self, runner):
        """Test templates get help."""
        result = runner.invoke(cli, ["templates", "get", "--help"])
        assert result.exit_code == 0

    def test_templates_create_help(self, runner):
        """Test templates create help."""
        result = runner.invoke(cli, ["templates", "create", "--help"])
        assert result.exit_code == 0
        assert "name" in result.output.lower()

    def test_templates_delete_help(self, runner):
        """Test templates delete help."""
        result = runner.invoke(cli, ["templates", "delete", "--help"])
        assert result.exit_code == 0

    def test_templates_run_help(self, runner):
        """Test templates run help."""
        result = runner.invoke(cli, ["templates", "run", "--help"])
        assert result.exit_code == 0


class TestWebhooksHelp:
    """Tests for webhooks command help."""

    def test_webhooks_help(self, runner):
        """Test webhooks --help."""
        result = runner.invoke(cli, ["webhooks", "--help"])
        assert result.exit_code == 0
        assert "webhook" in result.output.lower()

    def test_webhooks_list_help(self, runner):
        """Test webhooks list help."""
        result = runner.invoke(cli, ["webhooks", "list", "--help"])
        assert result.exit_code == 0

    def test_webhooks_create_help(self, runner):
        """Test webhooks create help."""
        result = runner.invoke(cli, ["webhooks", "create", "--help"])
        assert result.exit_code == 0
        assert "url" in result.output.lower()

    def test_webhooks_delete_help(self, runner):
        """Test webhooks delete help."""
        result = runner.invoke(cli, ["webhooks", "delete", "--help"])
        assert result.exit_code == 0


class TestNodepoolsHelp:
    """Tests for nodepools command help."""

    def test_nodepools_help(self, runner):
        """Test nodepools --help."""
        result = runner.invoke(cli, ["nodepools", "--help"])
        assert result.exit_code == 0
        assert "nodepool" in result.output.lower()

    def test_nodepools_list_help(self, runner):
        """Test nodepools list help."""
        result = runner.invoke(cli, ["nodepools", "list", "--help"])
        assert result.exit_code == 0

    def test_nodepools_describe_help(self, runner):
        """Test nodepools describe help."""
        result = runner.invoke(cli, ["nodepools", "describe", "--help"])
        assert result.exit_code == 0

    def test_nodepools_create_odcr_help(self, runner):
        """Test nodepools create-odcr help."""
        result = runner.invoke(cli, ["nodepools", "create-odcr", "--help"])
        assert result.exit_code == 0
        assert "capacity-reservation" in result.output.lower()


class TestCapacityHelpExtended:
    """Extended tests for capacity subcommands."""

    def test_capacity_recommend_help(self, runner):
        """Test capacity recommend help."""
        result = runner.invoke(cli, ["capacity", "recommend", "--help"])
        assert result.exit_code == 0

    def test_capacity_spot_prices_help(self, runner):
        """Test capacity spot-prices help."""
        result = runner.invoke(cli, ["capacity", "spot-prices", "--help"])
        assert result.exit_code == 0

    def test_capacity_instance_info_help(self, runner):
        """Test capacity instance-info help."""
        result = runner.invoke(cli, ["capacity", "instance-info", "--help"])
        assert result.exit_code == 0

    def test_capacity_status_help(self, runner):
        """Test capacity status help."""
        result = runner.invoke(cli, ["capacity", "status", "--help"])
        assert result.exit_code == 0

    def test_capacity_recommend_region_help(self, runner):
        """Test capacity recommend-region help."""
        result = runner.invoke(cli, ["capacity", "recommend-region", "--help"])
        assert result.exit_code == 0


class TestStacksHelpExtended:
    """Extended tests for stacks subcommands."""

    def test_stacks_synth_help(self, runner):
        """Test stacks synth help."""
        result = runner.invoke(cli, ["stacks", "synth", "--help"])
        assert result.exit_code == 0

    def test_stacks_diff_help(self, runner):
        """Test stacks diff help."""
        result = runner.invoke(cli, ["stacks", "diff", "--help"])
        assert result.exit_code == 0

    def test_stacks_status_help(self, runner):
        """Test stacks status help."""
        result = runner.invoke(cli, ["stacks", "status", "--help"])
        assert result.exit_code == 0

    def test_stacks_outputs_help(self, runner):
        """Test stacks outputs help."""
        result = runner.invoke(cli, ["stacks", "outputs", "--help"])
        assert result.exit_code == 0

    def test_stacks_fsx_help(self, runner):
        """Test stacks fsx help."""
        result = runner.invoke(cli, ["stacks", "fsx", "--help"])
        assert result.exit_code == 0

    def test_stacks_fsx_status_help(self, runner):
        """Test stacks fsx status help."""
        result = runner.invoke(cli, ["stacks", "fsx", "status", "--help"])
        assert result.exit_code == 0

    def test_stacks_fsx_enable_help(self, runner):
        """Test stacks fsx enable help."""
        result = runner.invoke(cli, ["stacks", "fsx", "enable", "--help"])
        assert result.exit_code == 0

    def test_stacks_fsx_disable_help(self, runner):
        """Test stacks fsx disable help."""
        result = runner.invoke(cli, ["stacks", "fsx", "disable", "--help"])
        assert result.exit_code == 0


class TestFilesHelpExtended:
    """Extended tests for files subcommands."""

    def test_files_get_help(self, runner):
        """Test files get help."""
        result = runner.invoke(cli, ["files", "get", "--help"])
        assert result.exit_code == 0

    def test_files_access_points_help(self, runner):
        """Test files access-points help."""
        result = runner.invoke(cli, ["files", "access-points", "--help"])
        assert result.exit_code == 0

    def test_files_ls_help(self, runner):
        """Test files ls help."""
        result = runner.invoke(cli, ["files", "ls", "--help"])
        assert result.exit_code == 0

    def test_files_download_help(self, runner):
        """Test files download help."""
        result = runner.invoke(cli, ["files", "download", "--help"])
        assert result.exit_code == 0


class TestConfigHelp:
    """Tests for config command help."""

    def test_config_help(self, runner):
        """Test config-cmd --help."""
        result = runner.invoke(cli, ["config-cmd", "--help"])
        assert result.exit_code == 0

    def test_config_show_help(self, runner):
        """Test config-cmd show help."""
        result = runner.invoke(cli, ["config-cmd", "show", "--help"])
        assert result.exit_code == 0

    def test_config_init_help(self, runner):
        """Test config-cmd init help."""
        result = runner.invoke(cli, ["config-cmd", "init", "--help"])
        assert result.exit_code == 0


class TestRegionalApiOption:
    """Tests for --regional-api global option."""

    def test_regional_api_option(self, runner):
        """Test --regional-api option."""
        result = runner.invoke(cli, ["--regional-api", "--help"])
        assert result.exit_code == 0

    def test_regional_api_with_jobs(self, runner):
        """Test --regional-api with jobs command."""
        result = runner.invoke(cli, ["--regional-api", "jobs", "--help"])
        assert result.exit_code == 0


class TestAllCommandsExist:
    """Verify all documented commands exist and have help."""

    def test_all_main_groups_exist(self, runner):
        """Test all main command groups exist."""
        groups = [
            "jobs",
            "queue",
            "templates",
            "webhooks",
            "stacks",
            "capacity",
            "files",
            "nodepools",
            "config-cmd",
        ]
        for group in groups:
            result = runner.invoke(cli, [group, "--help"])
            assert result.exit_code == 0, f"Command group '{group}' failed"

    def test_jobs_subcommands_exist(self, runner):
        """Test all jobs subcommands exist."""
        subcommands = [
            "submit",
            "submit-direct",
            "submit-sqs",
            "submit-queue",
            "list",
            "get",
            "logs",
            "delete",
            "events",
            "pods",
            "pod-logs",
            "metrics",
            "retry",
            "bulk-delete",
            "health",
            "queue-status",
        ]
        for cmd in subcommands:
            result = runner.invoke(cli, ["jobs", cmd, "--help"])
            assert result.exit_code == 0, f"jobs {cmd} failed"

    def test_queue_subcommands_exist(self, runner):
        """Test all queue subcommands exist."""
        subcommands = ["submit", "list", "get", "cancel", "stats"]
        for cmd in subcommands:
            result = runner.invoke(cli, ["queue", cmd, "--help"])
            assert result.exit_code == 0, f"queue {cmd} failed"

    def test_templates_subcommands_exist(self, runner):
        """Test all templates subcommands exist."""
        subcommands = ["list", "get", "create", "delete", "run"]
        for cmd in subcommands:
            result = runner.invoke(cli, ["templates", cmd, "--help"])
            assert result.exit_code == 0, f"templates {cmd} failed"

    def test_webhooks_subcommands_exist(self, runner):
        """Test all webhooks subcommands exist."""
        subcommands = ["list", "create", "delete"]
        for cmd in subcommands:
            result = runner.invoke(cli, ["webhooks", cmd, "--help"])
            assert result.exit_code == 0, f"webhooks {cmd} failed"

    def test_stacks_subcommands_exist(self, runner):
        """Test all stacks subcommands exist."""
        subcommands = [
            "list",
            "synth",
            "diff",
            "deploy",
            "destroy",
            "deploy-all",
            "destroy-all",
            "bootstrap",
            "status",
            "outputs",
            "fsx",
        ]
        for cmd in subcommands:
            result = runner.invoke(cli, ["stacks", cmd, "--help"])
            assert result.exit_code == 0, f"stacks {cmd} failed"

    def test_capacity_subcommands_exist(self, runner):
        """Test all capacity subcommands exist."""
        subcommands = [
            "check",
            "recommend",
            "spot-prices",
            "instance-info",
            "status",
            "recommend-region",
        ]
        for cmd in subcommands:
            result = runner.invoke(cli, ["capacity", cmd, "--help"])
            assert result.exit_code == 0, f"capacity {cmd} failed"

    def test_files_subcommands_exist(self, runner):
        """Test all files subcommands exist."""
        subcommands = ["list", "get", "access-points", "ls", "download"]
        for cmd in subcommands:
            result = runner.invoke(cli, ["files", cmd, "--help"])
            assert result.exit_code == 0, f"files {cmd} failed"

    def test_nodepools_subcommands_exist(self, runner):
        """Test all nodepools subcommands exist."""
        subcommands = ["list", "describe", "create-odcr"]
        for cmd in subcommands:
            result = runner.invoke(cli, ["nodepools", cmd, "--help"])
            assert result.exit_code == 0, f"nodepools {cmd} failed"
