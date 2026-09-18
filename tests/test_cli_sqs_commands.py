"""
Tests for the SQS-backed job CLI commands.

Drives `gco jobs submit-sqs` (with labels, priority, auto-region
discovery) and `queue-status`, using tempfile-backed YAML manifests
and a mocked JobManager.submit_job_sqs. Targets the code paths in
cli/main.py that talk to the SQS consumer pipeline rather than the
REST manifest endpoint.
"""

import os
import tempfile
from unittest.mock import MagicMock, patch

from click.testing import CliRunner


class TestJobsSubmitSqsCommand:
    """Tests for jobs submit-sqs command."""

    def test_jobs_submit_sqs_success(self):
        """Test jobs submit-sqs command success."""
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

    def test_jobs_submit_sqs_with_labels(self):
        """Test jobs submit-sqs with labels."""
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
                        "-l",
                        "env=prod",
                    ],
                )
                assert result.exit_code == 0
                call_kwargs = mock_jm.submit_job_sqs.call_args.kwargs
                assert call_kwargs.get("labels") == {"team": "ml", "env": "prod"}
        finally:
            os.unlink(manifest_path)

    def test_jobs_submit_sqs_with_priority(self):
        """Test jobs submit-sqs with priority."""
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
                        "--priority",
                        "10",
                    ],
                )
                assert result.exit_code == 0
                call_kwargs = mock_jm.submit_job_sqs.call_args.kwargs
                assert call_kwargs.get("priority") == 10
        finally:
            os.unlink(manifest_path)

    def test_jobs_submit_sqs_auto_region(self):
        """Test jobs submit-sqs with --auto-region."""
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
                # The auto-region feature uses the capacity checker
                assert "queued" in result.output.lower()
        finally:
            os.unlink(manifest_path)

    def test_jobs_submit_sqs_error(self):
        """Test jobs submit-sqs with error."""
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

    def test_jobs_submit_sqs_default_region(self):
        """Test jobs submit-sqs uses default region when not specified."""
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

                result = runner.invoke(cli, ["jobs", "submit-sqs", manifest_path])
                assert result.exit_code == 0
        finally:
            os.unlink(manifest_path)


class TestQueueStatusCommand:
    """Tests for jobs queue-status command."""

    def test_queue_status_single_region(self):
        """Test queue-status for a single region."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.get_queue_status.return_value = {
                "region": "us-east-1",
                "messages_available": 5,
                "messages_in_flight": 2,
                "messages_delayed": 0,
                "dlq_messages": 0,
            }
            mock_manager.return_value = mock_jm

            result = runner.invoke(cli, ["jobs", "queue-status", "-r", "us-east-1"])
            assert result.exit_code == 0

    def test_queue_status_all_regions(self):
        """Test queue-status for all regions."""
        from cli.main import cli

        runner = CliRunner()

        with (
            patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager,
            patch("cli.aws_client.get_aws_client") as mock_aws,
        ):
            mock_jm = MagicMock()
            mock_jm.get_queue_status.side_effect = [
                {
                    "region": "us-east-1",
                    "messages_available": 5,
                    "messages_in_flight": 2,
                    "messages_delayed": 0,
                    "dlq_messages": 0,
                },
                {
                    "region": "us-west-2",
                    "messages_available": 3,
                    "messages_in_flight": 1,
                    "messages_delayed": 0,
                    "dlq_messages": 1,
                },
            ]
            mock_manager.return_value = mock_jm

            mock_client = MagicMock()
            mock_client.discover_regional_stacks.return_value = ["us-east-1", "us-west-2"]
            mock_aws.return_value = mock_client

            result = runner.invoke(cli, ["jobs", "queue-status", "--all-regions"])
            assert result.exit_code == 0
            assert "us-east-1" in result.output
            assert "us-west-2" in result.output

    def test_queue_status_all_regions_no_stacks(self):
        """Test queue-status when no stacks found."""
        from cli.main import cli

        runner = CliRunner()

        with (
            patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager,
            patch("cli.aws_client.get_aws_client") as mock_aws,
        ):
            mock_jm = MagicMock()
            mock_manager.return_value = mock_jm

            mock_client = MagicMock()
            mock_client.discover_regional_stacks.return_value = []
            mock_aws.return_value = mock_client

            result = runner.invoke(cli, ["jobs", "queue-status", "--all-regions"])
            assert result.exit_code == 0
            assert "no queue status" in result.output.lower()

    def test_queue_status_error(self):
        """Test queue-status with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
            mock_jm = MagicMock()
            mock_jm.get_queue_status.side_effect = Exception("SQS error")
            mock_manager.return_value = mock_jm

            result = runner.invoke(cli, ["jobs", "queue-status", "-r", "us-east-1"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_queue_status_all_regions_partial_failure(self):
        """Test queue-status when some regions fail."""
        from cli.main import cli

        runner = CliRunner()

        with (
            patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager,
            patch("cli.aws_client.get_aws_client") as mock_aws,
        ):
            mock_jm = MagicMock()
            # First region succeeds, second fails
            mock_jm.get_queue_status.side_effect = [
                {
                    "region": "us-east-1",
                    "messages_available": 5,
                    "messages_in_flight": 2,
                    "messages_delayed": 0,
                    "dlq_messages": 0,
                },
                Exception("Region unavailable"),
            ]
            mock_manager.return_value = mock_jm

            mock_client = MagicMock()
            mock_client.discover_regional_stacks.return_value = ["us-east-1", "us-west-2"]
            mock_aws.return_value = mock_client

            result = runner.invoke(cli, ["jobs", "queue-status", "--all-regions"])
            assert result.exit_code == 0
            assert "us-east-1" in result.output


class TestCapacityStatusCommand:
    """Tests for capacity status command."""

    def test_capacity_status_all_regions(self):
        """Test capacity status for all regions."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.capacity.get_multi_region_capacity_checker") as mock_checker:
            mock_cap1 = MagicMock()
            mock_cap1.region = "us-east-1"
            mock_cap1.queue_depth = 5
            mock_cap1.running_jobs = 10
            mock_cap1.gpu_utilization = 50.0
            mock_cap1.cpu_utilization = 60.0
            mock_cap1.recommendation_score = 100

            mock_cap2 = MagicMock()
            mock_cap2.region = "us-west-2"
            mock_cap2.queue_depth = 2
            mock_cap2.running_jobs = 5
            mock_cap2.gpu_utilization = 30.0
            mock_cap2.cpu_utilization = 40.0
            mock_cap2.recommendation_score = 50

            mock_cc = MagicMock()
            mock_cc.get_all_regions_capacity.return_value = [mock_cap1, mock_cap2]
            mock_checker.return_value = mock_cc

            result = runner.invoke(cli, ["capacity", "status"])
            assert result.exit_code == 0
            assert "us-east-1" in result.output
            assert "us-west-2" in result.output

    def test_capacity_status_single_region(self):
        """Test capacity status for a single region."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.capacity.get_multi_region_capacity_checker") as mock_checker:
            mock_cc = MagicMock()
            mock_cc.get_region_capacity.return_value = {
                "region": "us-east-1",
                "queue_depth": 5,
                "running_jobs": 10,
            }
            mock_checker.return_value = mock_cc

            result = runner.invoke(cli, ["capacity", "status", "-r", "us-east-1"])
            assert result.exit_code == 0

    def test_capacity_status_no_stacks(self):
        """Test capacity status when no stacks found."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.capacity.get_multi_region_capacity_checker") as mock_checker:
            mock_cc = MagicMock()
            mock_cc.get_all_regions_capacity.return_value = []
            mock_checker.return_value = mock_cc

            result = runner.invoke(cli, ["capacity", "status"])
            assert result.exit_code == 0
            assert "no" in result.output.lower()

    def test_capacity_status_error(self):
        """Test capacity status with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.capacity.get_multi_region_capacity_checker") as mock_checker:
            mock_checker.side_effect = Exception("API error")

            result = runner.invoke(cli, ["capacity", "status"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestCapacityRecommendRegionCommand:
    """Tests for capacity recommend-region command."""

    def test_recommend_region_success(self):
        """Test recommend-region command success."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.capacity.get_multi_region_capacity_checker") as mock_checker:
            mock_cc = MagicMock()
            mock_cc.recommend_region_for_job.return_value = {
                "region": "us-west-2",
                "reason": "Lowest queue depth",
                "all_regions": [
                    {"region": "us-west-2", "score": 50, "queue_depth": 2, "gpu_utilization": 30},
                    {"region": "us-east-1", "score": 100, "queue_depth": 5, "gpu_utilization": 50},
                ],
            }
            mock_checker.return_value = mock_cc

            result = runner.invoke(cli, ["capacity", "recommend-region"])
            assert result.exit_code == 0
            assert "us-west-2" in result.output

    def test_recommend_region_with_gpu(self):
        """Test recommend-region with --gpu flag."""
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
            mock_cc.recommend_region_for_job.assert_called_once_with(
                gpu_required=True, min_gpus=0, instance_type=None, gpu_count=0
            )

    def test_recommend_region_with_min_gpus(self):
        """Test recommend-region with --min-gpus."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.capacity.get_multi_region_capacity_checker") as mock_checker:
            mock_cc = MagicMock()
            mock_cc.recommend_region_for_job.return_value = {
                "region": "us-east-1",
                "reason": "Has 4+ GPUs available",
            }
            mock_checker.return_value = mock_cc

            result = runner.invoke(cli, ["capacity", "recommend-region", "--min-gpus", "4"])
            assert result.exit_code == 0
            mock_cc.recommend_region_for_job.assert_called_once_with(
                gpu_required=False, min_gpus=4, instance_type=None, gpu_count=0
            )

    def test_recommend_region_verbose(self):
        """Test recommend-region with verbose output."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.capacity.get_multi_region_capacity_checker") as mock_checker:
            mock_cc = MagicMock()
            mock_cc.recommend_region_for_job.return_value = {
                "region": "us-west-2",
                "reason": "Lowest queue depth",
                "all_regions": [
                    {"region": "us-west-2", "score": 50, "queue_depth": 2, "gpu_utilization": 30},
                    {"region": "us-east-1", "score": 100, "queue_depth": 5, "gpu_utilization": 50},
                ],
            }
            mock_checker.return_value = mock_cc

            result = runner.invoke(cli, ["--verbose", "capacity", "recommend-region"])
            assert result.exit_code == 0
            assert "ranked" in result.output.lower()

    def test_recommend_region_error(self):
        """Test recommend-region with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.capacity.get_multi_region_capacity_checker") as mock_checker:
            mock_checker.side_effect = Exception("API error")

            result = runner.invoke(cli, ["capacity", "recommend-region"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestNodePoolsCommands:
    """Tests for nodepools commands."""

    def test_nodepools_help(self):
        """Test nodepools --help."""
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["nodepools", "--help"])
        assert result.exit_code == 0
        assert "NodePool" in result.output

    def test_nodepools_create_odcr(self):
        """Test nodepools create-odcr command."""
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
            assert "NodePool" in result.output or "kubectl" in result.output

    def test_nodepools_create_odcr_with_output_file(self):
        """Test nodepools create-odcr with output file."""
        from cli.main import cli

        runner = CliRunner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.flush()  # Ensure file is ready before using f.name
            output_path = f.name  # nosemgrep: tempfile-without-flush

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
                assert "written" in result.output.lower()
        finally:
            os.unlink(output_path)

    def test_nodepools_create_odcr_error(self):
        """Test nodepools create-odcr with error."""
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
                    "cr-invalid",
                ],
            )
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_nodepools_list(self):
        """Test nodepools list command."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.nodepools.list_cluster_nodepools") as mock_list:
            mock_list.return_value = [
                {"name": "gpu-x86-pool", "status": "Ready"},
                {"name": "cpu-pool", "status": "Ready"},
            ]

            result = runner.invoke(cli, ["nodepools", "list", "-r", "us-east-1"])
            assert result.exit_code == 0

    def test_nodepools_list_no_region(self):
        """Test nodepools list without region."""
        from cli.main import cli

        runner = CliRunner()

        result = runner.invoke(cli, ["nodepools", "list"])
        assert result.exit_code == 1
        assert "required" in result.output.lower()

    def test_nodepools_list_empty(self):
        """Test nodepools list when no nodepools found."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.nodepools.list_cluster_nodepools") as mock_list:
            mock_list.return_value = []

            result = runner.invoke(cli, ["nodepools", "list", "-r", "us-east-1"])
            assert result.exit_code == 0
            assert "no" in result.output.lower()

    def test_nodepools_list_error(self):
        """Test nodepools list with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.nodepools.list_cluster_nodepools") as mock_list:
            mock_list.side_effect = Exception("kubectl error")

            result = runner.invoke(cli, ["nodepools", "list", "-r", "us-east-1"])
            assert result.exit_code == 1
            assert "failed" in result.output.lower()

    def test_nodepools_describe(self):
        """Test nodepools describe command."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.nodepools.describe_cluster_nodepool") as mock_describe:
            mock_describe.return_value = {
                "name": "gpu-x86-pool",
                "status": "Ready",
                "spec": {"limits": {"cpu": "100"}},
            }

            result = runner.invoke(
                cli, ["nodepools", "describe", "gpu-x86-pool", "-r", "us-east-1"]
            )
            assert result.exit_code == 0

    def test_nodepools_describe_not_found(self):
        """Test nodepools describe when not found."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.nodepools.describe_cluster_nodepool") as mock_describe:
            mock_describe.return_value = None

            result = runner.invoke(cli, ["nodepools", "describe", "nonexistent", "-r", "us-east-1"])
            assert result.exit_code == 1
            assert "not found" in result.output.lower()

    def test_nodepools_describe_error(self):
        """Test nodepools describe with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.nodepools.describe_cluster_nodepool") as mock_describe:
            mock_describe.side_effect = Exception("kubectl error")

            result = runner.invoke(
                cli, ["nodepools", "describe", "gpu-x86-pool", "-r", "us-east-1"]
            )
            assert result.exit_code == 1
            assert "failed" in result.output.lower()


class TestFilesLsCommand:
    """Tests for files ls command."""

    def test_files_ls_success(self):
        """Test files ls command success."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.files_cmd.get_file_system_client") as mock_client:
            mock_fs = MagicMock()
            mock_fs.list_storage_contents.return_value = {
                "status": "success",
                "message": "Listed 3 items",
                "contents": [
                    {"name": "output", "is_directory": True, "size_bytes": 0},
                    {"name": "results.json", "is_directory": False, "size_bytes": 1024},
                    {"name": "model.pt", "is_directory": False, "size_bytes": 10240},
                ],
            }
            mock_client.return_value = mock_fs

            result = runner.invoke(cli, ["files", "ls", "-r", "us-east-1"])
            assert result.exit_code == 0
            assert "output" in result.output or "results" in result.output

    def test_files_ls_with_path(self):
        """Test files ls with specific path."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.files_cmd.get_file_system_client") as mock_client:
            mock_fs = MagicMock()
            mock_fs.list_storage_contents.return_value = {
                "status": "success",
                "message": "Listed 2 items",
                "contents": [
                    {"name": "checkpoint.pt", "is_directory": False, "size_bytes": 5000},
                ],
            }
            mock_client.return_value = mock_fs

            result = runner.invoke(cli, ["files", "ls", "my-job/outputs", "-r", "us-east-1"])
            assert result.exit_code == 0
            mock_fs.list_storage_contents.assert_called_once()

    def test_files_ls_empty_directory(self):
        """Test files ls with empty directory."""
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

    def test_files_ls_fsx(self):
        """Test files ls with FSx storage type."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.files_cmd.get_file_system_client") as mock_client:
            mock_fs = MagicMock()
            mock_fs.list_storage_contents.return_value = {
                "status": "success",
                "message": "Listed 1 item",
                "contents": [
                    {"name": "data", "is_directory": True, "size_bytes": 0},
                ],
            }
            mock_client.return_value = mock_fs

            result = runner.invoke(cli, ["files", "ls", "-r", "us-east-1", "-t", "fsx"])
            assert result.exit_code == 0
            mock_fs.list_storage_contents.assert_called_once_with(
                region="us-east-1",
                remote_path="/",
                storage_type="fsx",
                namespace="gco-jobs",
                pvc_name=None,
            )

    def test_files_ls_error(self):
        """Test files ls with error."""
        from cli.main import cli

        runner = CliRunner()

        with patch("cli.commands.files_cmd.get_file_system_client") as mock_client:
            mock_fs = MagicMock()
            mock_fs.list_storage_contents.return_value = {
                "status": "error",
                "message": "Pod creation failed",
            }
            mock_client.return_value = mock_fs

            result = runner.invoke(cli, ["files", "ls", "-r", "us-east-1"])
            assert result.exit_code == 1

    def test_files_ls_exception(self):
        """Test files ls with exception."""
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

    def test_config_init_new_file(self, tmp_path, monkeypatch):
        """Test config init creates new file."""
        from cli.main import cli

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()

        with (
            patch("pathlib.Path.home") as mock_home,
            patch("cli.config.GCOConfig.save") as mock_save,
        ):
            mock_home.return_value = MagicMock()
            mock_home.return_value.__truediv__ = MagicMock(return_value=MagicMock())

            result = runner.invoke(cli, ["config-cmd", "init", "--force"])
            # The command should attempt to save
            assert result.exit_code == 0 or mock_save.called

    def test_config_init_force_overwrite(self, tmp_path, monkeypatch):
        """Test config init with --force flag."""
        from cli.main import cli

        monkeypatch.chdir(tmp_path)
        (tmp_path / ".gco").mkdir()
        runner = CliRunner()

        with patch("cli.config.GCOConfig.save") as mock_save:
            result = runner.invoke(cli, ["config-cmd", "init", "--force"])
            # Should attempt to save regardless of existing file
            assert result.exit_code == 0 or mock_save.called


class TestSqsNamespaceHandling:
    """Regression tests: the CLI must not silently rewrite a manifest's
    declared namespace.

    Earlier behavior applied ``--namespace`` (defaulting to
    ``config.default_namespace``) as a hard override on every manifest,
    so a manifest that declared ``metadata.namespace: gco-inference``
    got silently rewritten to ``gco-jobs``. The fix preserves any
    namespace the manifest declared and only fills in missing values
    when the user explicitly passed ``--namespace``."""

    def test_manifest_declared_namespace_preserved_through_cli(self):
        """Manifest with its own namespace reaches the JobManager untouched
        even when --namespace isn't passed."""
        import yaml as _yaml

        from cli.main import cli

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = os.path.join(tmpdir, "job.yaml")
            with open(manifest_path, "w") as f:
                _yaml.safe_dump(
                    {
                        "apiVersion": "batch/v1",
                        "kind": "Job",
                        "metadata": {"name": "j", "namespace": "gco-inference"},
                        "spec": {
                            "template": {
                                "spec": {
                                    "restartPolicy": "Never",
                                    "containers": [{"name": "c", "image": "busybox"}],
                                }
                            }
                        },
                    },
                    f,
                )

            with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
                mock_jm = MagicMock()
                mock_jm.submit_job_sqs.return_value = {"message_id": "m1"}
                mock_manager.return_value = mock_jm

                result = runner.invoke(
                    cli, ["jobs", "submit-sqs", manifest_path, "-r", "us-east-1"]
                )
                assert result.exit_code == 0
                # The CLI must NOT inject a default namespace — that would
                # override the manifest's declared `gco-inference`.
                call_kwargs = mock_jm.submit_job_sqs.call_args.kwargs
                assert call_kwargs.get("namespace") is None

    def test_explicit_flag_passed_through_as_fallback(self):
        """When the user passes --namespace, the flag value reaches the
        JobManager so it can be used as a fallback for manifests without
        their own namespace."""
        import yaml as _yaml

        from cli.main import cli

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = os.path.join(tmpdir, "job.yaml")
            with open(manifest_path, "w") as f:
                _yaml.safe_dump(
                    {
                        "apiVersion": "batch/v1",
                        "kind": "Job",
                        "metadata": {"name": "j"},
                        "spec": {
                            "template": {
                                "spec": {
                                    "restartPolicy": "Never",
                                    "containers": [{"name": "c", "image": "busybox"}],
                                }
                            }
                        },
                    },
                    f,
                )

            with patch("cli.commands.jobs_cmd.get_job_manager") as mock_manager:
                mock_jm = MagicMock()
                mock_jm.submit_job_sqs.return_value = {"message_id": "m2"}
                mock_manager.return_value = mock_jm

                result = runner.invoke(
                    cli,
                    [
                        "jobs",
                        "submit-sqs",
                        manifest_path,
                        "-r",
                        "us-east-1",
                        "-n",
                        "default",
                    ],
                )
                assert result.exit_code == 0
                call_kwargs = mock_jm.submit_job_sqs.call_args.kwargs
                assert call_kwargs.get("namespace") == "default"
