"""
Tests for the queue, templates, and webhooks CLI subgroups.

Exercises `gco queue submit` with manifest files, priority, and
labels (writing real temp YAML files and mocking the AWS client's
call_api), plus the templates and webhooks subgroups. Verifies the
request body built by the CLI matches what the server-side handler
expects — a contract test between the Click command and the /queue
endpoints.
"""

import os
import tempfile
from unittest.mock import MagicMock, patch

from click.testing import CliRunner


class TestQueueCommands:
    """Tests for queue CLI commands."""

    def test_queue_help(self):
        """Test queue --help."""
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["queue", "--help"])
        assert result.exit_code == 0
        assert "global job queue" in result.output.lower()

    def test_queue_submit_success(self):
        """Test queue submit command success."""
        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: test-job\n")
            f.flush()
            manifest_path = f.name

        try:
            with patch("cli.aws_client.get_aws_client") as mock_client:
                mock_aws = MagicMock()
                mock_aws.call_api.return_value = {"job": {"job_id": "abc123", "status": "queued"}}
                mock_client.return_value = mock_aws

                result = runner.invoke(
                    cli, ["queue", "submit", manifest_path, "--region", "us-east-1"]
                )
                assert result.exit_code == 0
                assert "queued" in result.output.lower()
        finally:
            os.unlink(manifest_path)

    def test_queue_submit_with_priority(self):
        """Test queue submit with priority option."""
        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: test-job\n")
            f.flush()
            manifest_path = f.name

        try:
            with patch("cli.aws_client.get_aws_client") as mock_client:
                mock_aws = MagicMock()
                mock_aws.call_api.return_value = {"job": {"job_id": "abc123"}}
                mock_client.return_value = mock_aws

                result = runner.invoke(
                    cli,
                    [
                        "queue",
                        "submit",
                        manifest_path,
                        "--region",
                        "us-east-1",
                        "--priority",
                        "50",
                    ],
                )
                assert result.exit_code == 0
                # Verify priority was passed
                call_kwargs = mock_aws.call_api.call_args.kwargs
                assert call_kwargs["body"]["priority"] == 50
        finally:
            os.unlink(manifest_path)

    def test_queue_submit_with_labels(self):
        """Test queue submit with labels."""
        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: test-job\n")
            f.flush()
            manifest_path = f.name

        try:
            with patch("cli.aws_client.get_aws_client") as mock_client:
                mock_aws = MagicMock()
                mock_aws.call_api.return_value = {"job": {"job_id": "abc123"}}
                mock_client.return_value = mock_aws

                result = runner.invoke(
                    cli,
                    [
                        "queue",
                        "submit",
                        manifest_path,
                        "--region",
                        "us-east-1",
                        "-l",
                        "team=ml",
                        "-l",
                        "env=prod",
                    ],
                )
                assert result.exit_code == 0
                call_kwargs = mock_aws.call_api.call_args.kwargs
                assert call_kwargs["body"]["labels"] == {"team": "ml", "env": "prod"}
        finally:
            os.unlink(manifest_path)

    def test_queue_submit_error(self):
        """Test queue submit with error."""
        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: test-job\n")
            f.flush()
            manifest_path = f.name

        try:
            with patch("cli.aws_client.get_aws_client") as mock_client:
                mock_aws = MagicMock()
                mock_aws.call_api.side_effect = Exception("API error")
                mock_client.return_value = mock_aws

                result = runner.invoke(
                    cli, ["queue", "submit", manifest_path, "--region", "us-east-1"]
                )
                assert result.exit_code == 1
                assert "failed" in result.output.lower()
        finally:
            os.unlink(manifest_path)

    def test_queue_list_success(self):
        """Test queue list command success."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.return_value = {
                "count": 2,
                "jobs": [
                    {
                        "job_id": "abc123",
                        "job_name": "test-job-1",
                        "target_region": "us-east-1",
                        "status": "queued",
                    },
                    {
                        "job_id": "def456",
                        "job_name": "test-job-2",
                        "target_region": "us-west-2",
                        "status": "running",
                    },
                ],
            }
            mock_client.return_value = mock_aws

            result = runner.invoke(cli, ["queue", "list"])
            assert result.exit_code == 0
            assert "test-job-1" in result.output or "abc123" in result.output

    def test_queue_list_with_filters(self):
        """Test queue list with filters."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.return_value = {"count": 0, "jobs": []}
            mock_client.return_value = mock_aws

            result = runner.invoke(
                cli,
                [
                    "queue",
                    "list",
                    "--region",
                    "us-east-1",
                    "--status",
                    "queued",
                    "--namespace",
                    "gco-jobs",
                ],
            )
            assert result.exit_code == 0
            # Verify filters were passed
            call_kwargs = mock_aws.call_api.call_args.kwargs
            assert call_kwargs["params"]["target_region"] == "us-east-1"
            assert call_kwargs["params"]["status"] == "queued"
            assert call_kwargs["params"]["namespace"] == "gco-jobs"

    def test_queue_list_empty(self):
        """Test queue list when no jobs found."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.return_value = {"count": 0, "jobs": []}
            mock_client.return_value = mock_aws

            result = runner.invoke(cli, ["queue", "list"])
            assert result.exit_code == 0
            assert "no jobs" in result.output.lower()

    def test_queue_list_error(self):
        """Test queue list with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.side_effect = Exception("API error")
            mock_client.return_value = mock_aws

            result = runner.invoke(cli, ["queue", "list"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_queue_get_success(self):
        """Test queue get command success."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.return_value = {
                "job": {
                    "job_id": "abc123",
                    "job_name": "test-job",
                    "target_region": "us-east-1",
                    "namespace": "gco-jobs",
                    "status": "running",
                    "priority": 10,
                    "submitted_at": "2024-01-01T00:00:00Z",
                    "claimed_by": "us-east-1",
                    "status_history": [
                        {
                            "timestamp": "2024-01-01T00:00:00Z",
                            "status": "queued",
                            "message": "Job queued",
                        }
                    ],
                }
            }
            mock_client.return_value = mock_aws

            result = runner.invoke(cli, ["queue", "get", "abc123"])
            assert result.exit_code == 0
            assert "abc123" in result.output or "test-job" in result.output

    def test_queue_get_error(self):
        """Test queue get with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.side_effect = Exception("Job not found")
            mock_client.return_value = mock_aws

            result = runner.invoke(cli, ["queue", "get", "nonexistent"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_queue_cancel_success(self):
        """Test queue cancel command success."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.return_value = {"message": "Job cancelled"}
            mock_client.return_value = mock_aws

            result = runner.invoke(cli, ["queue", "cancel", "abc123", "-y"])
            assert result.exit_code == 0
            assert "cancelled" in result.output.lower()

    def test_queue_cancel_with_reason(self):
        """Test queue cancel with reason."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.return_value = {"message": "Job cancelled"}
            mock_client.return_value = mock_aws

            result = runner.invoke(
                cli, ["queue", "cancel", "abc123", "--reason", "No longer needed", "-y"]
            )
            assert result.exit_code == 0
            call_kwargs = mock_aws.call_api.call_args.kwargs
            assert call_kwargs["params"]["reason"] == "No longer needed"

    def test_queue_cancel_error(self):
        """Test queue cancel with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.side_effect = Exception("Cannot cancel running job")
            mock_client.return_value = mock_aws

            result = runner.invoke(cli, ["queue", "cancel", "abc123", "-y"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_queue_stats_success(self):
        """Test queue stats command success."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.return_value = {
                "summary": {
                    "total_jobs": 100,
                    "total_queued": 10,
                    "total_running": 5,
                },
                "by_region": {
                    "us-east-1": {"queued": 5, "running": 3, "succeeded": 40, "failed": 2},
                    "us-west-2": {"queued": 5, "running": 2, "succeeded": 35, "failed": 3},
                },
            }
            mock_client.return_value = mock_aws

            result = runner.invoke(cli, ["queue", "stats"])
            assert result.exit_code == 0
            assert "100" in result.output or "total" in result.output.lower()

    def test_queue_stats_error(self):
        """Test queue stats with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.side_effect = Exception("API error")
            mock_client.return_value = mock_aws

            result = runner.invoke(cli, ["queue", "stats"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestJobsSubmitQueueCommand:
    """Tests for jobs submit-queue CLI command (alias for queue submit)."""

    def test_jobs_submit_queue_help(self):
        """Test jobs submit-queue --help."""
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["jobs", "submit-queue", "--help"])
        assert result.exit_code == 0
        assert "dynamodb" in result.output.lower()
        assert "global" in result.output.lower()

    def test_jobs_submit_queue_success(self):
        """Test jobs submit-queue command success."""
        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: test-job\n")
            f.flush()
            manifest_path = f.name

        try:
            with patch("cli.aws_client.get_aws_client") as mock_client:
                mock_aws = MagicMock()
                mock_aws.call_api.return_value = {"job": {"job_id": "abc123", "status": "queued"}}
                mock_client.return_value = mock_aws

                result = runner.invoke(
                    cli, ["jobs", "submit-queue", manifest_path, "--region", "us-east-1"]
                )
                assert result.exit_code == 0
                assert "queued" in result.output.lower()
                # Verify it mentions how to track status
                assert "queue" in result.output.lower()
        finally:
            os.unlink(manifest_path)

    def test_jobs_submit_queue_with_priority(self):
        """Test jobs submit-queue with priority option."""
        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: test-job\n")
            f.flush()
            manifest_path = f.name

        try:
            with patch("cli.aws_client.get_aws_client") as mock_client:
                mock_aws = MagicMock()
                mock_aws.call_api.return_value = {"job": {"job_id": "abc123", "status": "queued"}}
                mock_client.return_value = mock_aws

                result = runner.invoke(
                    cli,
                    [
                        "jobs",
                        "submit-queue",
                        manifest_path,
                        "--region",
                        "us-east-1",
                        "--priority",
                        "50",
                    ],
                )
                assert result.exit_code == 0

                # Verify priority was passed in the API call
                call_args = mock_aws.call_api.call_args
                assert call_args.kwargs["body"]["priority"] == 50
        finally:
            os.unlink(manifest_path)

    def test_jobs_submit_queue_with_labels(self):
        """Test jobs submit-queue with labels."""
        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: test-job\n")
            f.flush()
            manifest_path = f.name

        try:
            with patch("cli.aws_client.get_aws_client") as mock_client:
                mock_aws = MagicMock()
                mock_aws.call_api.return_value = {"job": {"job_id": "abc123", "status": "queued"}}
                mock_client.return_value = mock_aws

                result = runner.invoke(
                    cli,
                    [
                        "jobs",
                        "submit-queue",
                        manifest_path,
                        "--region",
                        "us-east-1",
                        "-l",
                        "team=ml",
                        "-l",
                        "project=training",
                    ],
                )
                assert result.exit_code == 0

                # Verify labels were passed in the API call
                call_args = mock_aws.call_api.call_args
                assert call_args.kwargs["body"]["labels"] == {"team": "ml", "project": "training"}
        finally:
            os.unlink(manifest_path)

    def test_jobs_submit_queue_error(self):
        """Test jobs submit-queue with error."""
        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: test-job\n")
            f.flush()
            manifest_path = f.name

        try:
            with patch("cli.aws_client.get_aws_client") as mock_client:
                mock_aws = MagicMock()
                mock_aws.call_api.side_effect = Exception("API error")
                mock_client.return_value = mock_aws

                result = runner.invoke(
                    cli, ["jobs", "submit-queue", manifest_path, "--region", "us-east-1"]
                )
                assert result.exit_code == 1
                assert "failed" in result.output.lower()
        finally:
            os.unlink(manifest_path)

    def test_jobs_submit_queue_requires_region(self):
        """Test jobs submit-queue requires --region."""
        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: test-job\n")
            f.flush()
            manifest_path = f.name

        try:
            result = runner.invoke(cli, ["jobs", "submit-queue", manifest_path])
            assert result.exit_code != 0
            assert "region" in result.output.lower()
        finally:
            os.unlink(manifest_path)


class TestTemplatesCommands:
    """Tests for templates CLI commands."""

    def test_templates_help(self):
        """Test templates --help."""
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["templates", "--help"])
        assert result.exit_code == 0
        assert "job templates" in result.output.lower()

    def test_templates_list_success(self):
        """Test templates list command success."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.return_value = {
                "count": 2,
                "templates": [
                    {
                        "name": "gpu-template",
                        "description": "GPU training template",
                        "created_at": "2024-01-01T00:00:00Z",
                    },
                    {
                        "name": "cpu-template",
                        "description": "CPU batch template",
                        "created_at": "2024-01-02T00:00:00Z",
                    },
                ],
            }
            mock_client.return_value = mock_aws

            result = runner.invoke(cli, ["templates", "list"])
            assert result.exit_code == 0
            assert "gpu-template" in result.output or "2" in result.output

    def test_templates_list_empty(self):
        """Test templates list when no templates found."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.return_value = {"count": 0, "templates": []}
            mock_client.return_value = mock_aws

            result = runner.invoke(cli, ["templates", "list"])
            assert result.exit_code == 0
            assert "no templates" in result.output.lower()

    def test_templates_list_error(self):
        """Test templates list with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.side_effect = Exception("API error")
            mock_client.return_value = mock_aws

            result = runner.invoke(cli, ["templates", "list"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_templates_get_success(self):
        """Test templates get command success."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.return_value = {
                "template": {
                    "name": "gpu-template",
                    "description": "GPU training template",
                    "created_at": "2024-01-01T00:00:00Z",
                    "updated_at": "2024-01-01T00:00:00Z",
                    "parameters": {"image": "pytorch:latest", "gpus": "4"},
                    "manifest": {
                        "apiVersion": "batch/v1",
                        "kind": "Job",
                        "metadata": {"name": "{{name}}"},
                    },
                }
            }
            mock_client.return_value = mock_aws

            result = runner.invoke(cli, ["templates", "get", "gpu-template"])
            assert result.exit_code == 0
            assert "gpu-template" in result.output

    def test_templates_get_error(self):
        """Test templates get with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.side_effect = Exception("Template not found")
            mock_client.return_value = mock_aws

            result = runner.invoke(cli, ["templates", "get", "nonexistent"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_templates_create_success(self):
        """Test templates create command success."""
        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(
                "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: '{{name}}'\n"
                "spec:\n  template:\n    spec:\n      containers:\n      - name: main\n        image: '{{image}}'\n"
            )
            f.flush()
            manifest_path = f.name

        try:
            with patch("cli.aws_client.get_aws_client") as mock_client:
                mock_aws = MagicMock()
                mock_aws.call_api.return_value = {
                    "template": {"name": "my-template", "created_at": "2024-01-01T00:00:00Z"}
                }
                mock_client.return_value = mock_aws

                result = runner.invoke(
                    cli,
                    [
                        "templates",
                        "create",
                        manifest_path,
                        "--name",
                        "my-template",
                        "-d",
                        "My template description",
                    ],
                )
                assert result.exit_code == 0
                assert "created" in result.output.lower()
        finally:
            os.unlink(manifest_path)

    def test_templates_create_with_params(self):
        """Test templates create with default parameters."""
        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: '{{name}}'\n")
            f.flush()
            manifest_path = f.name

        try:
            with patch("cli.aws_client.get_aws_client") as mock_client:
                mock_aws = MagicMock()
                mock_aws.call_api.return_value = {"template": {"name": "my-template"}}
                mock_client.return_value = mock_aws

                result = runner.invoke(
                    cli,
                    [
                        "templates",
                        "create",
                        manifest_path,
                        "--name",
                        "my-template",
                        "-p",
                        "image=pytorch:latest",
                        "-p",
                        "gpus=4",
                    ],
                )
                assert result.exit_code == 0
                call_kwargs = mock_aws.call_api.call_args.kwargs
                assert call_kwargs["body"]["parameters"] == {
                    "image": "pytorch:latest",
                    "gpus": "4",
                }
        finally:
            os.unlink(manifest_path)

    def test_templates_create_error(self):
        """Test templates create with error."""
        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: test\n")
            f.flush()
            manifest_path = f.name

        try:
            with patch("cli.aws_client.get_aws_client") as mock_client:
                mock_aws = MagicMock()
                mock_aws.call_api.side_effect = Exception("Template already exists")
                mock_client.return_value = mock_aws

                result = runner.invoke(
                    cli, ["templates", "create", manifest_path, "--name", "existing-template"]
                )
                assert result.exit_code == 1
                assert "failed" in result.output.lower()
        finally:
            os.unlink(manifest_path)

    def test_templates_delete_success(self):
        """Test templates delete command success."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.return_value = {"message": "Template deleted"}
            mock_client.return_value = mock_aws

            result = runner.invoke(cli, ["templates", "delete", "old-template", "-y"])
            assert result.exit_code == 0
            assert "deleted" in result.output.lower()

    def test_templates_delete_error(self):
        """Test templates delete with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.side_effect = Exception("Template not found")
            mock_client.return_value = mock_aws

            result = runner.invoke(cli, ["templates", "delete", "nonexistent", "-y"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_templates_run_success(self):
        """Test templates run command success."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.return_value = {
                "success": True,
                "job_name": "my-job",
                "template": "gpu-template",
            }
            mock_client.return_value = mock_aws

            result = runner.invoke(
                cli,
                [
                    "templates",
                    "run",
                    "gpu-template",
                    "--name",
                    "my-job",
                    "--region",
                    "us-east-1",
                ],
            )
            assert result.exit_code == 0
            assert "created" in result.output.lower() or "my-job" in result.output

    def test_templates_run_with_params(self):
        """Test templates run with parameter overrides."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.return_value = {"success": True, "job_name": "my-job"}
            mock_client.return_value = mock_aws

            result = runner.invoke(
                cli,
                [
                    "templates",
                    "run",
                    "gpu-template",
                    "--name",
                    "my-job",
                    "--region",
                    "us-east-1",
                    "-p",
                    "image=custom:v1",
                    "-p",
                    "gpus=8",
                ],
            )
            assert result.exit_code == 0
            call_kwargs = mock_aws.call_api.call_args.kwargs
            assert call_kwargs["body"]["parameters"] == {"image": "custom:v1", "gpus": "8"}

    def test_templates_run_failure(self):
        """Test templates run when job creation fails."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.return_value = {"success": False, "errors": ["Validation failed"]}
            mock_client.return_value = mock_aws

            result = runner.invoke(
                cli,
                [
                    "templates",
                    "run",
                    "gpu-template",
                    "--name",
                    "my-job",
                    "--region",
                    "us-east-1",
                ],
            )
            assert result.exit_code == 0  # Command succeeds but reports failure
            assert "failed" in result.output.lower()

    def test_templates_run_error(self):
        """Test templates run with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.side_effect = Exception("Template not found")
            mock_client.return_value = mock_aws

            result = runner.invoke(
                cli,
                [
                    "templates",
                    "run",
                    "nonexistent",
                    "--name",
                    "my-job",
                    "--region",
                    "us-east-1",
                ],
            )
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestWebhooksCommands:
    """Tests for webhooks CLI commands."""

    def test_webhooks_help(self):
        """Test webhooks --help."""
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["webhooks", "--help"])
        assert result.exit_code == 0
        assert "webhook" in result.output.lower()

    def test_webhooks_list_success(self):
        """Test webhooks list command success."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.return_value = {
                "count": 2,
                "webhooks": [
                    {
                        "id": "abc123",
                        "url": "https://example.com/webhook1",
                        "events": ["job.completed", "job.failed"],
                        "namespace": "default",
                    },
                    {
                        "id": "def456",
                        "url": "https://example.com/webhook2",
                        "events": ["job.started"],
                        "namespace": None,
                    },
                ],
            }
            mock_client.return_value = mock_aws

            result = runner.invoke(cli, ["webhooks", "list"])
            assert result.exit_code == 0
            assert "abc123" in result.output or "example.com" in result.output

    def test_webhooks_list_with_namespace(self):
        """Test webhooks list with namespace filter."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.return_value = {"count": 0, "webhooks": []}
            mock_client.return_value = mock_aws

            result = runner.invoke(cli, ["webhooks", "list", "--namespace", "gco-jobs"])
            assert result.exit_code == 0
            call_kwargs = mock_aws.call_api.call_args.kwargs
            assert call_kwargs["params"]["namespace"] == "gco-jobs"

    def test_webhooks_list_empty(self):
        """Test webhooks list when no webhooks found."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.return_value = {"count": 0, "webhooks": []}
            mock_client.return_value = mock_aws

            result = runner.invoke(cli, ["webhooks", "list"])
            assert result.exit_code == 0
            assert "no webhooks" in result.output.lower()

    def test_webhooks_list_error(self):
        """Test webhooks list with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.side_effect = Exception("API error")
            mock_client.return_value = mock_aws

            result = runner.invoke(cli, ["webhooks", "list"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_webhooks_create_success(self):
        """Test webhooks create command success."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.return_value = {
                "webhook": {
                    "id": "abc123",
                    "url": "https://example.com/webhook",
                    "events": ["job.completed", "job.failed"],
                }
            }
            mock_client.return_value = mock_aws

            result = runner.invoke(
                cli,
                [
                    "webhooks",
                    "create",
                    "--url",
                    "https://example.com/webhook",
                    "-e",
                    "job.completed",
                    "-e",
                    "job.failed",
                ],
            )
            assert result.exit_code == 0
            assert "registered" in result.output.lower()

    def test_webhooks_create_with_namespace(self):
        """Test webhooks create with namespace filter."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.return_value = {"webhook": {"id": "abc123"}}
            mock_client.return_value = mock_aws

            result = runner.invoke(
                cli,
                [
                    "webhooks",
                    "create",
                    "--url",
                    "https://example.com/webhook",
                    "-e",
                    "job.completed",
                    "--namespace",
                    "gco-jobs",
                ],
            )
            assert result.exit_code == 0
            call_kwargs = mock_aws.call_api.call_args.kwargs
            assert call_kwargs["body"]["namespace"] == "gco-jobs"

    def test_webhooks_create_with_secret(self):
        """Test webhooks create with secret for HMAC."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.return_value = {"webhook": {"id": "abc123"}}
            mock_client.return_value = mock_aws

            result = runner.invoke(
                cli,
                [
                    "webhooks",
                    "create",
                    "--url",
                    "https://example.com/webhook",
                    "-e",
                    "job.completed",
                    "--secret",
                    "my-secret-key",
                ],
            )
            assert result.exit_code == 0
            call_kwargs = mock_aws.call_api.call_args.kwargs
            assert call_kwargs["body"]["secret"] == "my-secret-key"

    def test_webhooks_create_error(self):
        """Test webhooks create with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.side_effect = Exception("Invalid URL")
            mock_client.return_value = mock_aws

            result = runner.invoke(
                cli,
                [
                    "webhooks",
                    "create",
                    "--url",
                    "invalid-url",
                    "-e",
                    "job.completed",
                ],
            )
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_webhooks_delete_success(self):
        """Test webhooks delete command success."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.return_value = {"message": "Webhook deleted"}
            mock_client.return_value = mock_aws

            result = runner.invoke(cli, ["webhooks", "delete", "abc123", "-y"])
            assert result.exit_code == 0
            assert "deleted" in result.output.lower()

    def test_webhooks_delete_error(self):
        """Test webhooks delete with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.aws_client.get_aws_client") as mock_client:
            mock_aws = MagicMock()
            mock_aws.call_api.side_effect = Exception("Webhook not found")
            mock_client.return_value = mock_aws

            result = runner.invoke(cli, ["webhooks", "delete", "nonexistent", "-y"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestJobsHealthCommand:
    """Tests for jobs health command."""

    def test_jobs_health_requires_region_or_all_regions(self):
        """Test jobs health requires --region or --all-regions."""
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["jobs", "health"])
        assert result.exit_code == 1
        assert "must specify --region or --all-regions" in result.output

    def test_jobs_health_all_regions_success(self):
        """Test jobs health --all-regions success."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.get_global_health.return_value = {
                "overall_status": "healthy",
                "healthy_regions": 2,
                "total_regions": 2,
                "regions": [
                    {"region": "us-east-1", "status": "healthy", "cluster_id": "gco-us-east-1"},
                    {"region": "us-west-2", "status": "healthy", "cluster_id": "gco-us-west-2"},
                ],
            }
            mock_manager.return_value = mock_jm

            result = runner.invoke(cli, ["jobs", "health", "--all-regions"])
            assert result.exit_code == 0
            assert "healthy" in result.output.lower()

    def test_jobs_health_single_region_success(self):
        """Test jobs health --region success."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm._aws_client.get_health.return_value = {
                "status": "healthy",
                "cluster_id": "gco-us-east-1",
            }
            mock_manager.return_value = mock_jm

            result = runner.invoke(cli, ["jobs", "health", "--region", "us-east-1"])
            assert result.exit_code == 0

    def test_jobs_health_error(self):
        """Test jobs health with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.get_global_health.side_effect = Exception("API error")
            mock_manager.return_value = mock_jm

            result = runner.invoke(cli, ["jobs", "health", "--all-regions"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestJobsEventsCommand:
    """Tests for jobs events command."""

    def test_jobs_events_success(self):
        """Test jobs events command success."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.get_job_events.return_value = {
                "count": 2,
                "events": [
                    {
                        "type": "Normal",
                        "reason": "SuccessfulCreate",
                        "message": "Created pod",
                        "lastTimestamp": "2024-01-01T00:00:00Z",
                    },
                    {
                        "type": "Warning",
                        "reason": "BackOff",
                        "message": "Back-off restarting",
                        "lastTimestamp": "2024-01-01T00:01:00Z",
                    },
                ],
            }
            mock_manager.return_value = mock_jm

            result = runner.invoke(cli, ["jobs", "events", "test-job", "--region", "us-east-1"])
            assert result.exit_code == 0

    def test_jobs_events_error(self):
        """Test jobs events with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.get_job_events.side_effect = Exception("Job not found")
            mock_manager.return_value = mock_jm

            result = runner.invoke(cli, ["jobs", "events", "nonexistent", "--region", "us-east-1"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestJobsPodsCommand:
    """Tests for jobs pods command."""

    def test_jobs_pods_success(self):
        """Test jobs pods command success."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.get_job_pods.return_value = {
                "count": 2,
                "pods": [
                    {
                        "metadata": {"name": "test-job-abc123"},
                        "spec": {"nodeName": "node-1"},
                        "status": {"phase": "Running"},
                    },
                    {
                        "metadata": {"name": "test-job-def456"},
                        "spec": {"nodeName": "node-2"},
                        "status": {"phase": "Succeeded"},
                    },
                ],
            }
            mock_manager.return_value = mock_jm

            result = runner.invoke(cli, ["jobs", "pods", "test-job", "--region", "us-east-1"])
            assert result.exit_code == 0

    def test_jobs_pods_error(self):
        """Test jobs pods with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.get_job_pods.side_effect = Exception("Job not found")
            mock_manager.return_value = mock_jm

            result = runner.invoke(cli, ["jobs", "pods", "nonexistent", "--region", "us-east-1"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestJobsMetricsCommand:
    """Tests for jobs metrics command."""

    def test_jobs_metrics_success(self):
        """Test jobs metrics command success."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.get_job_metrics.return_value = {
                "summary": {
                    "total_cpu_millicores": 500,
                    "total_memory_mib": 1024,
                    "pod_count": 2,
                },
                "pods": [
                    {
                        "pod_name": "test-job-abc123",
                        "containers": [{"name": "main", "cpu_millicores": 250, "memory_mib": 512}],
                    },
                ],
            }
            mock_manager.return_value = mock_jm

            result = runner.invoke(cli, ["jobs", "metrics", "test-job", "--region", "us-east-1"])
            assert result.exit_code == 0

    def test_jobs_metrics_error(self):
        """Test jobs metrics with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.get_job_metrics.side_effect = Exception("Metrics not available")
            mock_manager.return_value = mock_jm

            result = runner.invoke(cli, ["jobs", "metrics", "test-job", "--region", "us-east-1"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestJobsRetryCommand:
    """Tests for jobs retry command."""

    def test_jobs_retry_success(self):
        """Test jobs retry command success."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.retry_job.return_value = {
                "success": True,
                "new_job": "test-job-retry-20240101000000",
            }
            mock_manager.return_value = mock_jm

            result = runner.invoke(
                cli, ["jobs", "retry", "test-job", "--region", "us-east-1", "-y"]
            )
            assert result.exit_code == 0
            assert "retry" in result.output.lower()

    def test_jobs_retry_failure(self):
        """Test jobs retry when retry fails."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.retry_job.return_value = {
                "success": False,
                "message": "Job is still running",
            }
            mock_manager.return_value = mock_jm

            result = runner.invoke(
                cli, ["jobs", "retry", "test-job", "--region", "us-east-1", "-y"]
            )
            assert result.exit_code == 1

    def test_jobs_retry_error(self):
        """Test jobs retry with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.retry_job.side_effect = Exception("Job not found")
            mock_manager.return_value = mock_jm

            result = runner.invoke(
                cli, ["jobs", "retry", "nonexistent", "--region", "us-east-1", "-y"]
            )
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestJobsBulkDeleteCommand:
    """Tests for jobs bulk-delete command."""

    def test_jobs_bulk_delete_requires_region_or_all_regions(self):
        """Test jobs bulk-delete requires --region or --all-regions."""
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["jobs", "bulk-delete"])
        assert result.exit_code == 1
        assert "must specify --region or --all-regions" in result.output

    def test_jobs_bulk_delete_dry_run(self):
        """Test jobs bulk-delete with dry run."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.bulk_delete_jobs.return_value = {
                "total_matched": 5,
                "deleted_count": 0,
            }
            mock_manager.return_value = mock_jm

            result = runner.invoke(
                cli,
                [
                    "jobs",
                    "bulk-delete",
                    "--region",
                    "us-east-1",
                    "--status",
                    "completed",
                ],
            )
            assert result.exit_code == 0
            assert "dry run" in result.output.lower()

    def test_jobs_bulk_delete_execute(self):
        """Test jobs bulk-delete with --execute."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.bulk_delete_jobs.return_value = {
                "total_matched": 5,
                "deleted_count": 5,
            }
            mock_manager.return_value = mock_jm

            result = runner.invoke(
                cli,
                [
                    "jobs",
                    "bulk-delete",
                    "--region",
                    "us-east-1",
                    "--status",
                    "completed",
                    "--execute",
                    "-y",
                ],
            )
            assert result.exit_code == 0
            assert "deleted" in result.output.lower()

    def test_jobs_bulk_delete_all_regions(self):
        """Test jobs bulk-delete --all-regions."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.bulk_delete_global.return_value = {
                "total_deleted": 10,
            }
            mock_manager.return_value = mock_jm

            result = runner.invoke(
                cli,
                [
                    "jobs",
                    "bulk-delete",
                    "--all-regions",
                    "--status",
                    "failed",
                    "--label-selector",
                    "team=ml",
                    "--execute",
                    "-y",
                ],
            )
            assert result.exit_code == 0
            mock_jm.bulk_delete_global.assert_called_once_with(
                namespace=None,
                status="failed",
                older_than_days=None,
                label_selector="team=ml",
                dry_run=False,
            )

    def test_jobs_bulk_delete_error(self):
        """Test jobs bulk-delete with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.bulk_delete_jobs.side_effect = Exception("API error")
            mock_manager.return_value = mock_jm

            result = runner.invoke(
                cli,
                [
                    "jobs",
                    "bulk-delete",
                    "--region",
                    "us-east-1",
                    "--execute",
                    "-y",
                ],
            )
            assert result.exit_code == 1
            assert "failed" in result.output.lower()
