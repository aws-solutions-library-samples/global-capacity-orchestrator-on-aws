"""
Tests for cli/jobs.py — the JobManager surface.

Covers the JobInfo dataclass (is_complete derivation across
running/succeeded/failed/pending states and duration_seconds math
with/without start_time and completion_time) plus JobManager
initialization and the rest of the job CRUD surface. Targeted
extensions live in test_jobs_dag_extended.py.
"""

import os
import tempfile
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
import yaml


class TestJobInfo:
    """Tests for JobInfo dataclass."""

    def test_job_info_creation(self):
        """Test creating JobInfo."""
        from cli.jobs import JobInfo

        job = JobInfo(
            name="test-job",
            namespace="gco-jobs",
            region="us-east-1",
            status="running",
            active_pods=2,
        )

        assert job.name == "test-job"
        assert job.namespace == "gco-jobs"
        assert job.status == "running"
        assert job.is_complete is False

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

    def test_job_info_failed(self):
        """Test JobInfo for failed job."""
        from cli.jobs import JobInfo

        job = JobInfo(
            name="failed-job",
            namespace="gco-jobs",
            region="us-east-1",
            status="failed",
            failed_pods=1,
        )

        assert job.is_complete is True

    def test_job_info_duration_running(self):
        """Test duration calculation for running job."""
        from cli.jobs import JobInfo

        job = JobInfo(
            name="running-job",
            namespace="gco-jobs",
            region="us-east-1",
            status="running",
            start_time=datetime.now(UTC),
        )

        # Duration should be close to 0 for just-started job
        assert job.duration_seconds is not None
        assert job.duration_seconds >= 0

    def test_job_info_duration_no_start(self):
        """Test duration when no start time."""
        from cli.jobs import JobInfo

        job = JobInfo(
            name="pending-job",
            namespace="gco-jobs",
            region="us-east-1",
            status="pending",
        )

        assert job.duration_seconds is None


class TestJobManager:
    """Tests for JobManager class."""

    def test_job_manager_initialization(self):
        """Test JobManager initialization."""
        from cli.jobs import JobManager

        with patch("cli.jobs.get_config") as mock_config:
            mock_config.return_value = MagicMock(default_region="us-east-1")
            with patch("cli.jobs.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()
                manager = JobManager()
                assert manager.config is not None


class TestJobManagerManifestLoading:
    """Tests for manifest loading functionality."""

    def test_load_manifests_single_file(self):
        """Test loading manifests from a single YAML file."""
        from cli.jobs import JobManager

        with patch("cli.jobs.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            with patch("cli.jobs.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()
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

        with patch("cli.jobs.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            with patch("cli.jobs.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()
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

    def test_load_manifests_directory(self):
        """Test loading manifests from a directory."""
        from cli.jobs import JobManager

        with patch("cli.jobs.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            with patch("cli.jobs.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()
                manager = JobManager()

                with tempfile.TemporaryDirectory() as tmpdir:
                    # Create two YAML files
                    with open(
                        os.path.join(tmpdir, "01-namespace.yaml"), "w", encoding="utf-8"
                    ) as f:
                        yaml.dump(
                            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "test"}},
                            f,
                        )

                    with open(os.path.join(tmpdir, "02-job.yaml"), "w", encoding="utf-8") as f:
                        yaml.dump(
                            {
                                "apiVersion": "batch/v1",
                                "kind": "Job",
                                "metadata": {"name": "test-job"},
                            },
                            f,
                        )

                    manifests = manager.load_manifests(tmpdir)
                    assert len(manifests) == 2

    def test_load_manifests_file_not_found(self):
        """Test loading manifests from non-existent path."""
        from cli.jobs import JobManager

        with patch("cli.jobs.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            with patch("cli.jobs.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()
                manager = JobManager()

                with pytest.raises(FileNotFoundError):
                    manager.load_manifests("/nonexistent/path")


class TestJobManagerOperations:
    """Tests for job operations."""

    def test_submit_job(self):
        """Test submitting a job."""
        from cli.jobs import JobManager

        with patch("cli.jobs.get_config") as mock_config:
            mock_config.return_value = MagicMock(default_namespace="gco-jobs")
            with patch("cli.jobs.get_aws_client") as mock_aws:
                mock_aws_client = MagicMock()
                mock_aws_client.submit_manifests.return_value = {"status": "submitted"}
                mock_aws.return_value = mock_aws_client

                manager = JobManager()

                with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
                    yaml.dump(
                        {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "test-job"}},
                        f,
                    )
                    f.flush()

                    result = manager.submit_job(f.name)
                    assert result["status"] == "submitted"

                    os.unlink(f.name)

    def test_submit_job_with_namespace_override(self):
        """Test submitting a job with namespace override."""
        from cli.jobs import JobManager

        with patch("cli.jobs.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            with patch("cli.jobs.get_aws_client") as mock_aws:
                mock_aws_client = MagicMock()
                mock_aws_client.submit_manifests.return_value = {"status": "submitted"}
                mock_aws.return_value = mock_aws_client

                manager = JobManager()

                manifests = [
                    {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "test-job"}}
                ]
                manager.submit_job(manifests, namespace="custom-namespace")

                # Verify namespace was set
                call_args = mock_aws_client.submit_manifests.call_args
                submitted_manifests = call_args[1]["manifests"]
                assert submitted_manifests[0]["metadata"]["namespace"] == "custom-namespace"

    def test_submit_job_preserves_manifest_namespace(self):
        """A manifest that declares its own namespace must reach the API
        untouched even when the caller passes a ``namespace`` argument —
        the argument is a fallback for manifests that don't declare one,
        not an override. Regression for the silent-rewrite bug where the
        CLI's default_namespace was overriding ``metadata.namespace``."""
        from cli.jobs import JobManager

        with (
            patch("cli.jobs.get_config") as mock_config,
            patch("cli.jobs.get_aws_client") as mock_aws,
        ):
            mock_config.return_value = MagicMock()
            mock_aws_client = MagicMock()
            mock_aws_client.submit_manifests.return_value = {"status": "submitted"}
            mock_aws.return_value = mock_aws_client

            manager = JobManager()

            manifests = [
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {"name": "test-job", "namespace": "gco-inference"},
                }
            ]
            manager.submit_job(manifests, namespace="gco-jobs")

            submitted = mock_aws_client.submit_manifests.call_args[1]["manifests"]
            assert submitted[0]["metadata"]["namespace"] == "gco-inference"

    def test_submit_job_with_labels(self):
        """Test submitting a job with additional labels."""
        from cli.jobs import JobManager

        with patch("cli.jobs.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            with patch("cli.jobs.get_aws_client") as mock_aws:
                mock_aws_client = MagicMock()
                mock_aws_client.submit_manifests.return_value = {"status": "submitted"}
                mock_aws.return_value = mock_aws_client

                manager = JobManager()

                manifests = [
                    {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "test-job"}}
                ]
                manager.submit_job(manifests, labels={"team": "ml", "env": "prod"})

                call_args = mock_aws_client.submit_manifests.call_args
                submitted_manifests = call_args[1]["manifests"]
                assert submitted_manifests[0]["metadata"]["labels"]["team"] == "ml"
                assert submitted_manifests[0]["metadata"]["labels"]["env"] == "prod"

    def test_list_jobs(self):
        """Test listing jobs."""
        from cli.jobs import JobManager

        with patch("cli.jobs.get_config") as mock_config:
            mock_config.return_value = MagicMock(default_region="us-east-1")
            with patch("cli.jobs.get_aws_client") as mock_aws:
                mock_aws_client = MagicMock()
                mock_aws_client.get_jobs.return_value = {
                    "jobs": [
                        {
                            "metadata": {"name": "job1", "namespace": "default"},
                            "status": {"active": 1},
                            "spec": {},
                        }
                    ]
                }
                mock_aws.return_value = mock_aws_client

                manager = JobManager()
                jobs = manager.list_jobs()

                assert len(jobs) == 1
                assert jobs[0].name == "job1"

    def test_get_job(self):
        """Test getting a specific job."""
        from cli.jobs import JobManager

        with patch("cli.jobs.get_config") as mock_config:
            mock_config.return_value = MagicMock(default_region="us-east-1")
            with patch("cli.jobs.get_aws_client") as mock_aws:
                mock_aws_client = MagicMock()
                mock_aws_client.get_job_details.return_value = {
                    "metadata": {"name": "test-job", "namespace": "gco-jobs"},
                    "status": {"active": 1},
                    "spec": {},
                }
                mock_aws.return_value = mock_aws_client

                manager = JobManager()
                job = manager.get_job("test-job", "gco-jobs")

                assert job is not None
                assert job.name == "test-job"

    def test_get_job_not_found(self):
        """Test getting a job that doesn't exist."""
        from cli.jobs import JobManager

        with patch("cli.jobs.get_config") as mock_config:
            mock_config.return_value = MagicMock(default_region="us-east-1")
            with patch("cli.jobs.get_aws_client") as mock_aws:
                mock_aws_client = MagicMock()
                mock_aws_client.get_job_details.side_effect = Exception("Not found")
                mock_aws.return_value = mock_aws_client

                manager = JobManager()
                job = manager.get_job("nonexistent-job", "gco-jobs")

                assert job is None

    def test_get_job_logs(self):
        """Test getting job logs."""
        from cli.jobs import JobManager

        with patch("cli.jobs.get_config") as mock_config:
            mock_config.return_value = MagicMock(default_region="us-east-1")
            with patch("cli.jobs.get_aws_client") as mock_aws:
                mock_aws_client = MagicMock()
                mock_aws_client.get_job_logs.return_value = "Log line 1\nLog line 2"
                mock_aws.return_value = mock_aws_client

                manager = JobManager()
                logs = manager.get_job_logs("test-job", "gco-jobs")

                assert "Log line 1" in logs

    def test_get_job_logs_follow_not_implemented(self):
        """Test that log following raises NotImplementedError."""
        from cli.jobs import JobManager

        with patch("cli.jobs.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            with patch("cli.jobs.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                manager = JobManager()

                with pytest.raises(NotImplementedError):
                    manager.get_job_logs("test-job", "gco-jobs", follow=True)

    def test_delete_job(self):
        """Test deleting a job."""
        from cli.jobs import JobManager

        with patch("cli.jobs.get_config") as mock_config:
            mock_config.return_value = MagicMock(default_region="us-east-1")
            with patch("cli.jobs.get_aws_client") as mock_aws:
                mock_aws_client = MagicMock()
                mock_aws_client.delete_job.return_value = {"status": "deleted"}
                mock_aws.return_value = mock_aws_client

                manager = JobManager()
                result = manager.delete_job("test-job", "gco-jobs")

                assert result["status"] == "deleted"


class TestJobManagerWait:
    """Tests for job waiting functionality."""

    def test_wait_for_job_completes(self):
        """Test waiting for a job that completes."""
        from cli.jobs import JobInfo, JobManager

        with patch("cli.jobs.get_config") as mock_config:
            mock_config.return_value = MagicMock(default_region="us-east-1")
            with patch("cli.jobs.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                manager = JobManager()

                # Mock get_job to return completed job
                with patch.object(manager, "get_job") as mock_get:
                    mock_get.return_value = JobInfo(
                        name="test-job",
                        namespace="gco-jobs",
                        region="us-east-1",
                        status="succeeded",
                    )

                    result = manager.wait_for_job("test-job", "gco-jobs", timeout_seconds=10)
                    assert result.status == "succeeded"

    def test_wait_for_job_not_found(self):
        """Test waiting for a job that doesn't exist."""
        from cli.jobs import JobManager

        with patch("cli.jobs.get_config") as mock_config:
            mock_config.return_value = MagicMock(default_region="us-east-1")
            with patch("cli.jobs.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                manager = JobManager()

                with patch.object(manager, "get_job") as mock_get:
                    mock_get.return_value = None

                    with pytest.raises(ValueError, match="not found"):
                        manager.wait_for_job("nonexistent-job", "gco-jobs", timeout_seconds=1)


class TestGetJobManager:
    """Tests for get_job_manager factory function."""

    def test_get_job_manager(self):
        """Test factory function returns JobManager."""
        from cli.jobs import JobManager, get_job_manager

        with patch("cli.jobs.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            with patch("cli.jobs.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()
                manager = get_job_manager()
                assert isinstance(manager, JobManager)

    def test_get_job_manager_with_config(self):
        """Test factory function with custom config."""
        from cli.jobs import JobManager, get_job_manager

        with patch("cli.jobs.get_aws_client") as mock_aws:
            mock_aws.return_value = MagicMock()
            custom_config = MagicMock()
            manager = get_job_manager(custom_config)
            assert isinstance(manager, JobManager)
            assert manager.config == custom_config


class TestJobManagerListJobs:
    """Tests for JobManager.list_jobs method."""

    def test_list_jobs_default_region(self):
        """Test list_jobs with default region."""
        from unittest.mock import MagicMock

        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.get_jobs.return_value = {"jobs": []}

        result = manager.list_jobs()
        assert result == []
        manager._aws_client.get_jobs.assert_called_once()

    def test_list_jobs_specific_region(self):
        """Test list_jobs with specific region."""
        from unittest.mock import MagicMock

        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.get_jobs.return_value = {"jobs": []}

        result = manager.list_jobs(region="eu-west-1")
        assert result == []
        manager._aws_client.get_jobs.assert_called_with(
            region="eu-west-1", namespace=None, status=None
        )

    def test_list_jobs_all_regions(self):
        """Test list_jobs with all_regions=True."""
        from unittest.mock import MagicMock

        from cli.aws_client import RegionalStack
        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.discover_regional_stacks.return_value = {
            "us-east-1": RegionalStack(
                region="us-east-1",
                stack_name="test",
                cluster_name="test",
                status="CREATE_COMPLETE",
            ),
        }
        manager._aws_client.get_jobs.return_value = {"jobs": []}

        result = manager.list_jobs(all_regions=True)
        assert result == []


class TestJobManagerGetJob:
    """Tests for JobManager.get_job method."""

    def test_get_job_found(self):
        """Test get_job when job is found."""
        from unittest.mock import MagicMock

        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.get_job_details.return_value = {
            "metadata": {
                "name": "test-job",
                "namespace": "default",
                "creationTimestamp": "2024-01-01T00:00:00Z",
            },
            "spec": {"parallelism": 1, "completions": 1},
            "status": {"active": 1, "succeeded": 0, "failed": 0},
        }

        result = manager.get_job("test-job", "default", "us-east-1")
        assert result is not None
        assert result.name == "test-job"
        assert result.namespace == "default"

    def test_get_job_not_found(self):
        """Test get_job when job is not found."""
        from unittest.mock import MagicMock

        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.get_job_details.side_effect = Exception("Not found")

        result = manager.get_job("nonexistent", "default", "us-east-1")
        assert result is None


class TestJobManagerGetJobLogs:
    """Tests for JobManager.get_job_logs method."""

    def test_get_job_logs_success(self):
        """Test get_job_logs returns logs."""
        from unittest.mock import MagicMock

        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.get_job_logs.return_value = "Log line 1\nLog line 2"

        result = manager.get_job_logs("test-job", "default", "us-east-1")
        assert "Log line 1" in result

    def test_get_job_logs_follow_not_implemented(self):
        """Test get_job_logs raises error for follow=True."""
        from cli.jobs import JobManager

        manager = JobManager()

        with pytest.raises(NotImplementedError, match="Log streaming not yet implemented"):
            manager.get_job_logs("test-job", "default", follow=True)


class TestJobManagerDeleteJob:
    """Tests for JobManager.delete_job method."""

    def test_delete_job_success(self):
        """Test delete_job returns result."""
        from unittest.mock import MagicMock

        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.delete_job.return_value = {"success": True}

        result = manager.delete_job("test-job", "default", "us-east-1")
        assert result["success"] is True


class TestJobManagerSubmitJob:
    """Tests for JobManager.submit_job method."""

    def test_submit_job_with_manifests_list(self):
        """Test submit_job with manifest list."""
        from unittest.mock import MagicMock

        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.submit_manifests.return_value = {"success": True}

        manifests = [
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": "test-job"},
                "spec": {},
            }
        ]

        manager.submit_job(manifests, namespace="default")
        assert manager._aws_client.submit_manifests.called

    def test_submit_job_with_namespace_override(self):
        """Test submit_job applies namespace override."""
        from unittest.mock import MagicMock

        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.submit_manifests.return_value = {"success": True}

        manifests = [
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": "test-job"},
                "spec": {},
            }
        ]

        manager.submit_job(manifests, namespace="custom-ns")

        # Check that namespace was applied
        call_args = manager._aws_client.submit_manifests.call_args
        submitted_manifests = call_args.kwargs.get("manifests") or call_args[1].get("manifests")
        assert submitted_manifests[0]["metadata"]["namespace"] == "custom-ns"

    def test_submit_job_with_labels(self):
        """Test submit_job applies additional labels."""
        from unittest.mock import MagicMock

        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.submit_manifests.return_value = {"success": True}

        manifests = [
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": "test-job"},
                "spec": {},
            }
        ]

        manager.submit_job(manifests, labels={"team": "platform"})

        call_args = manager._aws_client.submit_manifests.call_args
        submitted_manifests = call_args.kwargs.get("manifests") or call_args[1].get("manifests")
        assert submitted_manifests[0]["metadata"]["labels"]["team"] == "platform"


class TestJobManagerWaitForJob:
    """Tests for JobManager.wait_for_job method."""

    def test_wait_for_job_already_complete(self):
        """Test wait_for_job when job is already complete."""
        from unittest.mock import MagicMock

        from cli.jobs import JobInfo, JobManager

        manager = JobManager()
        mock_job = JobInfo(
            name="test-job",
            namespace="default",
            region="us-east-1",
            status="succeeded",
        )
        manager.get_job = MagicMock(return_value=mock_job)

        result = manager.wait_for_job("test-job", "default", "us-east-1")
        assert result.status == "succeeded"

    def test_wait_for_job_not_found(self):
        """Test wait_for_job raises error when job not found."""
        from unittest.mock import MagicMock

        from cli.jobs import JobManager

        manager = JobManager()
        manager.get_job = MagicMock(return_value=None)

        with pytest.raises(ValueError, match="Job test-job not found"):
            manager.wait_for_job("test-job", "default", "us-east-1", timeout_seconds=1)


class TestJobInfoProperties:
    """Tests for JobInfo properties."""

    def test_job_info_is_complete_succeeded(self):
        """Test is_complete for succeeded job."""
        from cli.jobs import JobInfo

        job = JobInfo(
            name="test-job",
            namespace="default",
            region="us-east-1",
            status="succeeded",
        )
        assert job.is_complete is True

    def test_job_info_is_complete_failed(self):
        """Test is_complete for failed job."""
        from cli.jobs import JobInfo

        job = JobInfo(
            name="test-job",
            namespace="default",
            region="us-east-1",
            status="failed",
        )
        assert job.is_complete is True

    def test_job_info_is_complete_running(self):
        """Test is_complete for running job."""
        from cli.jobs import JobInfo

        job = JobInfo(
            name="test-job",
            namespace="default",
            region="us-east-1",
            status="running",
        )
        assert job.is_complete is False

    def test_job_info_duration_completed(self):
        """Test duration_seconds for completed job."""
        from datetime import datetime

        from cli.jobs import JobInfo

        start = datetime(2024, 1, 1, 0, 0, 0)
        end = datetime(2024, 1, 1, 0, 5, 0)  # 5 minutes later

        job = JobInfo(
            name="test-job",
            namespace="default",
            region="us-east-1",
            status="succeeded",
            start_time=start,
            completion_time=end,
        )
        assert job.duration_seconds == 300  # 5 minutes

    def test_job_info_duration_no_start(self):
        """Test duration_seconds when no start time."""
        from cli.jobs import JobInfo

        job = JobInfo(
            name="test-job",
            namespace="default",
            region="us-east-1",
            status="pending",
        )
        assert job.duration_seconds is None


class TestJobManagerSubmitJobDirect:
    """Tests for JobManager.submit_job_direct method."""

    def _make_manager(self):
        """Create a JobManager with mocked AWS client."""
        from cli.aws_client import RegionalStack
        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.get_regional_stack.return_value = RegionalStack(
            region="us-east-1",
            stack_name="gco-us-east-1",
            cluster_name="gco-us-east-1",
            status="CREATE_COMPLETE",
        )
        return manager

    @patch("cli.kubectl_helpers.update_kubeconfig")
    def test_submit_job_direct_success(self, mock_kubeconfig):
        """Test submit_job_direct with no existing job (new submission)."""
        manager = self._make_manager()
        manifests = [
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": "test-job"},
                "spec": {},
            }
        ]

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                # _get_kubectl_job_status: job doesn't exist
                MagicMock(returncode=1, stdout="", stderr="not found"),
                # kubectl apply
                MagicMock(returncode=0, stdout="job/test-job created", stderr=""),
            ]

            result = manager.submit_job_direct(manifests, region="us-east-1")

            assert result["status"] == "success"
            assert result["method"] == "kubectl"
            assert result["region"] == "us-east-1"
            assert result["namespace"] == "gco-jobs"
            assert manifests[0]["metadata"]["namespace"] == "gco-jobs"
            assert "warnings" not in result

    def test_submit_job_direct_no_stack(self):
        """Test submit_job_direct when no stack found."""
        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.get_regional_stack.return_value = None

        manifests = [{"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "test-job"}}]

        with pytest.raises(ValueError, match="No GCO stack found"):
            manager.submit_job_direct(manifests, region="us-east-1")

    @patch("cli.kubectl_helpers.update_kubeconfig")
    def test_submit_job_direct_kubeconfig_failure(self, mock_kubeconfig):
        """Test submit_job_direct when kubeconfig update fails."""
        manager = self._make_manager()
        mock_kubeconfig.side_effect = RuntimeError("Failed to update kubeconfig")

        manifests = [{"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "test-job"}}]

        with pytest.raises(RuntimeError, match="Failed to update kubeconfig"):
            manager.submit_job_direct(manifests, region="us-east-1")

    @patch("cli.kubectl_helpers.update_kubeconfig")
    def test_submit_job_direct_kubectl_failure(self, mock_kubeconfig):
        """Test submit_job_direct when kubectl apply fails."""
        manager = self._make_manager()
        manifests = [{"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "test-job"}}]

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                # _get_kubectl_job_status: job doesn't exist
                MagicMock(returncode=1, stdout="", stderr="not found"),
                # kubectl apply fails
                MagicMock(returncode=1, stdout="", stderr="error: resource not found"),
            ]

            with pytest.raises(RuntimeError, match="kubectl apply failed"):
                manager.submit_job_direct(manifests, region="us-east-1")

    @patch("cli.kubectl_helpers.update_kubeconfig")
    def test_submit_job_direct_with_dry_run(self, mock_kubeconfig):
        """Test submit_job_direct with dry_run=True skips duplicate check."""
        manager = self._make_manager()
        manifests = [{"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "test-job"}}]

        with patch("subprocess.run") as mock_run:
            # dry_run skips the _get_kubectl_job_status call entirely
            mock_run.return_value = MagicMock(
                returncode=0, stdout="job/test-job created (dry run)", stderr=""
            )

            result = manager.submit_job_direct(
                manifests, region="us-east-1", namespace="default", dry_run=True
            )

            assert result["dry_run"] is True
            # Only one subprocess call (kubectl apply), no status check
            assert mock_run.call_count == 1
            kubectl_call = mock_run.call_args_list[0]
            assert "--dry-run=client" in kubectl_call[0][0]

    @patch("cli.kubectl_helpers.update_kubeconfig")
    def test_submit_job_direct_with_labels(self, mock_kubeconfig):
        """Test submit_job_direct applies labels."""
        manager = self._make_manager()
        manifests = [{"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "test-job"}}]

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=1, stdout="", stderr="not found"),
                MagicMock(returncode=0, stdout="job/test-job created", stderr=""),
            ]

            result = manager.submit_job_direct(manifests, region="us-east-1", labels={"team": "ml"})

            assert result["status"] == "success"

    @patch("cli.kubectl_helpers.update_kubeconfig")
    def test_submit_job_direct_from_file(self, mock_kubeconfig):
        """Test submit_job_direct loading from file path."""
        import tempfile

        import yaml

        from cli.aws_client import RegionalStack
        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.get_regional_stack.return_value = RegionalStack(
            region="us-east-1",
            stack_name="gco-us-east-1",
            cluster_name="gco-us-east-1",
            status="CREATE_COMPLETE",
        )

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(
                {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "test-job"}}, f
            )
            f.flush()

            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = [
                    MagicMock(returncode=1, stdout="", stderr="not found"),
                    MagicMock(returncode=0, stdout="job/test-job created", stderr=""),
                ]

                result = manager.submit_job_direct(f.name, region="us-east-1")

                assert result["status"] == "success"

            import os

            os.unlink(f.name)

    @patch("cli.kubectl_helpers.update_kubeconfig")
    def test_submit_job_direct_replaces_completed_job(self, mock_kubeconfig):
        """Test that a completed job is deleted and replaced."""
        import json

        manager = self._make_manager()
        manifests = [
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": "test-job", "namespace": "default"},
                "spec": {},
            }
        ]

        completed_job_json = json.dumps(
            {
                "status": {
                    "conditions": [{"type": "Complete", "status": "True"}],
                }
            }
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                # _get_kubectl_job_status: job exists and is complete
                MagicMock(returncode=0, stdout=completed_job_json, stderr=""),
                # kubectl delete job
                MagicMock(returncode=0, stdout="job deleted", stderr=""),
                # kubectl apply
                MagicMock(returncode=0, stdout="job/test-job created", stderr=""),
            ]

            result = manager.submit_job_direct(manifests, region="us-east-1")

            assert result["status"] == "success"
            assert result["job_name"] == "test-job"
            assert "warnings" not in result
            # Verify delete was called
            delete_call = mock_run.call_args_list[1]
            assert "delete" in delete_call[0][0]
            assert "test-job" in delete_call[0][0]

    @patch("cli.kubectl_helpers.update_kubeconfig")
    def test_submit_job_direct_replaces_failed_job(self, mock_kubeconfig):
        """Test that a failed job is deleted and replaced."""
        import json

        manager = self._make_manager()
        manifests = [
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": "test-job", "namespace": "default"},
                "spec": {},
            }
        ]

        failed_job_json = json.dumps(
            {
                "status": {
                    "conditions": [{"type": "Failed", "status": "True"}],
                }
            }
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=failed_job_json, stderr=""),
                MagicMock(returncode=0, stdout="job deleted", stderr=""),
                MagicMock(returncode=0, stdout="job/test-job created", stderr=""),
            ]

            result = manager.submit_job_direct(manifests, region="us-east-1")

            assert result["status"] == "success"
            assert "warnings" not in result

    @patch("cli.kubectl_helpers.update_kubeconfig")
    def test_submit_job_direct_renames_active_job(self, mock_kubeconfig):
        """Test that an active job triggers auto-rename with warning."""
        import json

        manager = self._make_manager()
        manifests = [
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": "test-job", "namespace": "default"},
                "spec": {},
            }
        ]

        active_job_json = json.dumps(
            {
                "status": {
                    "conditions": [],
                }
            }
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                # _get_kubectl_job_status: job exists and is active
                MagicMock(returncode=0, stdout=active_job_json, stderr=""),
                # kubectl apply (no delete — job is active)
                MagicMock(returncode=0, stdout="job/test-job-abc12 created", stderr=""),
            ]

            result = manager.submit_job_direct(manifests, region="us-east-1")

            assert result["status"] == "success"
            # Job name should have been renamed with a suffix
            assert result["job_name"] != "test-job"
            assert result["job_name"].startswith("test-job-")
            assert len(result["job_name"]) == len("test-job-") + 5
            # Warnings should be present
            assert "warnings" in result
            assert len(result["warnings"]) == 1
            assert "still running" in result["warnings"][0]
            assert "test-job" in result["warnings"][0]


class TestGetKubectlJobStatus:
    """Tests for JobManager._get_kubectl_job_status helper."""

    def test_returns_none_when_job_not_found(self):
        """Job doesn't exist — returncode != 0."""
        from cli.jobs import JobManager

        manager = JobManager()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not found")
            assert manager._get_kubectl_job_status("no-job", "default") is None

    def test_returns_complete(self):
        """Job has Complete condition."""
        import json

        from cli.jobs import JobManager

        manager = JobManager()
        job_json = json.dumps({"status": {"conditions": [{"type": "Complete", "status": "True"}]}})

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=job_json, stderr="")
            assert manager._get_kubectl_job_status("my-job", "default") == "complete"

    def test_returns_failed(self):
        """Job has Failed condition."""
        import json

        from cli.jobs import JobManager

        manager = JobManager()
        job_json = json.dumps({"status": {"conditions": [{"type": "Failed", "status": "True"}]}})

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=job_json, stderr="")
            assert manager._get_kubectl_job_status("my-job", "default") == "failed"

    def test_returns_active_no_conditions(self):
        """Job exists but has no terminal conditions."""
        import json

        from cli.jobs import JobManager

        manager = JobManager()
        job_json = json.dumps({"status": {"conditions": []}})

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=job_json, stderr="")
            assert manager._get_kubectl_job_status("my-job", "default") == "active"

    def test_returns_active_with_non_terminal_condition(self):
        """Job has conditions but none are Complete or Failed."""
        import json

        from cli.jobs import JobManager

        manager = JobManager()
        job_json = json.dumps({"status": {"conditions": [{"type": "Suspended", "status": "True"}]}})

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=job_json, stderr="")
            assert manager._get_kubectl_job_status("my-job", "default") == "active"

    def test_returns_none_on_invalid_json(self):
        """kubectl returns success but stdout is not valid JSON."""
        from cli.jobs import JobManager

        manager = JobManager()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="not-json-at-all", stderr="")
            assert manager._get_kubectl_job_status("my-job", "default") is None


class TestJobManagerParseJobInfo:
    """Tests for JobManager._parse_job_info method."""

    def test_parse_job_info_running(self):
        """Test parsing running job info."""
        from cli.jobs import JobManager

        manager = JobManager()

        job_data = {
            "metadata": {
                "name": "test-job",
                "namespace": "default",
                "creationTimestamp": "2024-01-01T00:00:00Z",
                "labels": {"app": "test"},
            },
            "spec": {"parallelism": 2, "completions": 4},
            "status": {
                "active": 2,
                "succeeded": 0,
                "failed": 0,
                "startTime": "2024-01-01T00:00:01Z",
            },
        }

        result = manager._parse_job_info(job_data, "us-east-1")

        assert result.name == "test-job"
        assert result.namespace == "default"
        assert result.status == "running"
        assert result.active_pods == 2
        assert result.parallelism == 2
        assert result.completions == 4

    def test_parse_job_info_succeeded(self):
        """Test parsing succeeded job info."""
        from cli.jobs import JobManager

        manager = JobManager()

        job_data = {
            "metadata": {
                "name": "test-job",
                "namespace": "default",
                "creationTimestamp": "2024-01-01T00:00:00Z",
            },
            "spec": {"parallelism": 1, "completions": 1},
            "status": {
                "active": 0,
                "succeeded": 1,
                "failed": 0,
                "startTime": "2024-01-01T00:00:01Z",
                "completionTime": "2024-01-01T00:05:00Z",
                "conditions": [{"type": "Complete", "status": "True"}],
            },
        }

        result = manager._parse_job_info(job_data, "us-east-1")

        assert result.status == "succeeded"
        assert result.succeeded_pods == 1

    def test_parse_job_info_failed(self):
        """Test parsing failed job info."""
        from cli.jobs import JobManager

        manager = JobManager()

        job_data = {
            "metadata": {
                "name": "test-job",
                "namespace": "default",
                "creationTimestamp": "2024-01-01T00:00:00Z",
            },
            "spec": {},
            "status": {
                "active": 0,
                "succeeded": 0,
                "failed": 1,
                "conditions": [{"type": "Failed", "status": "True"}],
            },
        }

        result = manager._parse_job_info(job_data, "us-east-1")

        assert result.status == "failed"
        assert result.failed_pods == 1

    def test_parse_job_info_extracts_image_refs(self):
        """The pod template's containers + initContainers carry the
        image URIs that the orphan-image cross-reference depends on."""
        from cli.jobs import JobManager

        manager = JobManager()

        job_data = {
            "metadata": {
                "name": "with-images",
                "namespace": "gco-jobs",
                "creationTimestamp": "2024-01-01T00:00:00Z",
            },
            "spec": {
                "parallelism": 1,
                "completions": 1,
                "template": {
                    "spec": {
                        "containers": [
                            {"name": "trainer", "image": "registry/train:v1"},
                            {"name": "sidecar", "image": "registry/aux:v2"},
                        ],
                        "initContainers": [
                            {"name": "data-prep", "image": "registry/init:v3"},
                        ],
                    },
                },
            },
            "status": {"active": 1},
        }

        result = manager._parse_job_info(job_data, "us-east-1")

        # Sorted, deduplicated.
        assert result.image_refs == [
            "registry/aux:v2",
            "registry/init:v3",
            "registry/train:v1",
        ]

    def test_parse_job_info_image_refs_empty_when_template_missing(self):
        """A historical Job manifest without a template (or recorded
        without one in DynamoDB) still parses cleanly with an empty
        ``image_refs`` rather than raising."""
        from cli.jobs import JobManager

        manager = JobManager()

        job_data = {
            "metadata": {
                "name": "no-template",
                "namespace": "default",
                "creationTimestamp": "2024-01-01T00:00:00Z",
            },
            "spec": {"parallelism": 1, "completions": 1},
            "status": {"active": 1},
        }

        result = manager._parse_job_info(job_data, "us-east-1")
        assert result.image_refs == []

    def test_parse_job_info_image_refs_dedupes_repeated_refs(self):
        """Multi-container jobs that share an image (common for sidecar
        patterns) collapse to a single entry."""
        from cli.jobs import JobManager

        manager = JobManager()

        job_data = {
            "metadata": {"name": "shared", "creationTimestamp": "2024-01-01T00:00:00Z"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"name": "a", "image": "shared/img:v1"},
                            {"name": "b", "image": "shared/img:v1"},
                        ],
                        "initContainers": [
                            {"name": "init", "image": "shared/img:v1"},
                        ],
                    },
                },
            },
            "status": {"active": 1},
        }

        result = manager._parse_job_info(job_data, "us-east-1")
        assert result.image_refs == ["shared/img:v1"]

    def test_parse_job_info_image_refs_skips_invalid_entries(self):
        """Entries without an ``image`` key, with empty image, or with
        non-string image values are filtered rather than poisoning the
        ref set."""
        from cli.jobs import JobManager

        manager = JobManager()

        job_data = {
            "metadata": {"name": "messy", "creationTimestamp": "2024-01-01T00:00:00Z"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"name": "real", "image": "good/img:v1"},
                            {"name": "no-image"},
                            {"name": "empty-image", "image": ""},
                            {"name": "non-string", "image": 42},
                            "not-even-a-dict",
                        ],
                    },
                },
            },
            "status": {"active": 1},
        }

        result = manager._parse_job_info(job_data, "us-east-1")
        assert result.image_refs == ["good/img:v1"]


class TestJobManagerQueryJobsInRegion:
    """Tests for JobManager._query_jobs_in_region method."""

    def test_query_jobs_in_region_success(self):
        """Test querying jobs in a region."""
        from unittest.mock import MagicMock

        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.get_jobs.return_value = {
            "jobs": [
                {
                    "metadata": {"name": "job1", "namespace": "default"},
                    "spec": {},
                    "status": {"active": 1},
                }
            ]
        }

        result = manager._query_jobs_in_region("us-east-1", None, None)

        assert len(result) == 1
        assert result[0].name == "job1"

    def test_query_jobs_in_region_error(self):
        """Test querying jobs when API returns error."""
        from unittest.mock import MagicMock

        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.get_jobs.side_effect = Exception("API error")

        result = manager._query_jobs_in_region("us-east-1", None, None)

        assert result == []

    def test_query_jobs_in_region_list_response(self):
        """Test querying jobs when API returns list directly."""
        from unittest.mock import MagicMock

        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        # Some APIs might return a list directly instead of {"jobs": [...]}
        manager._aws_client.get_jobs.return_value = [
            {
                "metadata": {"name": "job1", "namespace": "default"},
                "spec": {},
                "status": {"active": 1},
            }
        ]

        result = manager._query_jobs_in_region("us-east-1", None, None)

        assert len(result) == 1


# =============================================================================
# Additional coverage tests for cli/jobs.py
# =============================================================================


class TestJobManagerSubmitDirectExtended:
    """Extended tests for direct job submission."""

    def test_submit_job_direct_no_stack(self):
        """Test submit_job_direct raises error when no stack found."""
        from cli.jobs import JobManager

        with patch("cli.jobs.get_aws_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.get_regional_stack.return_value = None

            manager = JobManager()

            with pytest.raises(ValueError, match="No GCO stack found"):
                manager.submit_job_direct(
                    manifests=[{"kind": "Job", "metadata": {"name": "test"}}],
                    region="us-east-1",
                )

    def test_load_manifests_directory(self, tmp_path):
        """Test loading manifests from directory."""
        from cli.jobs import JobManager

        (tmp_path / "job1.yaml").write_text("kind: Job\nmetadata:\n  name: job1")
        (tmp_path / "job2.yml").write_text("kind: Job\nmetadata:\n  name: job2")

        with patch("cli.jobs.get_aws_client"):
            manager = JobManager()
            manifests = manager.load_manifests(str(tmp_path))

            assert len(manifests) == 2

    def test_load_manifests_file_not_found(self):
        """Test load_manifests raises error for non-existent path."""
        from cli.jobs import JobManager

        with patch("cli.jobs.get_aws_client"):
            manager = JobManager()

            with pytest.raises(FileNotFoundError):
                manager.load_manifests("/nonexistent/path")


class TestJobManagerSQSExtended:
    """Extended tests for SQS job submission."""

    def test_submit_job_sqs_no_stack(self):
        """Test submit_job_sqs raises error when no stack found."""
        from cli.jobs import JobManager

        with patch("cli.jobs.get_aws_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.get_regional_stack.return_value = None

            manager = JobManager()

            with pytest.raises(ValueError, match="No GCO stack found"):
                manager.submit_job_sqs(
                    manifests=[{"kind": "Job", "metadata": {"name": "test"}}],
                    region="us-east-1",
                )

    def test_get_queue_status_no_stack(self):
        """Test get_queue_status raises error when no stack found."""
        from cli.jobs import JobManager

        with patch("cli.jobs.get_aws_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.get_regional_stack.return_value = None

            manager = JobManager()

            with pytest.raises(ValueError, match="No GCO stack found"):
                manager.get_queue_status("us-east-1")


class TestJobManagerWaitExtended:
    """Extended tests for job wait functionality."""

    def test_wait_for_job_not_found(self):
        """Test wait_for_job raises error when job not found."""
        from cli.jobs import JobManager

        with patch("cli.jobs.get_aws_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.get_job_details.side_effect = Exception("Not found")

            manager = JobManager()

            with pytest.raises(ValueError, match="not found"):
                manager.wait_for_job("nonexistent-job", "default", timeout_seconds=1)


class TestJobManagerLogsExtended:
    """Extended tests for job logs functionality."""

    def test_get_job_logs_follow_not_implemented(self):
        """Test get_job_logs raises NotImplementedError for follow mode."""
        from cli.jobs import JobManager

        with patch("cli.jobs.get_aws_client"):
            manager = JobManager()

            with pytest.raises(NotImplementedError, match="streaming"):
                manager.get_job_logs("test-job", "default", follow=True)


# =============================================================================
# Additional coverage tests for uncovered lines in cli/jobs.py
# =============================================================================


class TestJobManagerSubmitJobWithMetadataCreation:
    """Tests for submit_job when metadata needs to be created."""

    def test_submit_job_creates_metadata_for_namespace(self):
        """Test submit_job creates metadata dict when missing for namespace override."""
        from unittest.mock import MagicMock

        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.submit_manifests.return_value = {"status": "submitted"}

        # Manifest without metadata
        manifests = [{"apiVersion": "batch/v1", "kind": "Job"}]

        manager.submit_job(manifests, namespace="custom-ns")

        call_args = manager._aws_client.submit_manifests.call_args
        submitted = call_args.kwargs.get("manifests") or call_args[1].get("manifests")
        assert submitted[0]["metadata"]["namespace"] == "custom-ns"

    def test_submit_job_creates_metadata_for_labels(self):
        """Test submit_job creates metadata dict when missing for labels."""
        from unittest.mock import MagicMock

        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.submit_manifests.return_value = {"status": "submitted"}

        # Manifest without metadata
        manifests = [{"apiVersion": "batch/v1", "kind": "Job"}]

        manager.submit_job(manifests, labels={"team": "ml"})

        call_args = manager._aws_client.submit_manifests.call_args
        submitted = call_args.kwargs.get("manifests") or call_args[1].get("manifests")
        assert submitted[0]["metadata"]["labels"]["team"] == "ml"

    def test_submit_job_creates_labels_dict_when_missing(self):
        """Test submit_job creates labels dict when metadata exists but labels don't."""
        from unittest.mock import MagicMock

        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.submit_manifests.return_value = {"status": "submitted"}

        # Manifest with metadata but no labels
        manifests = [{"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "test"}}]

        manager.submit_job(manifests, labels={"team": "ml"})

        call_args = manager._aws_client.submit_manifests.call_args
        submitted = call_args.kwargs.get("manifests") or call_args[1].get("manifests")
        assert submitted[0]["metadata"]["labels"]["team"] == "ml"


class TestJobManagerSubmitDirectMetadataCreation:
    """Tests for submit_job_direct when metadata needs to be created."""

    @patch("cli.kubectl_helpers.update_kubeconfig")
    def test_submit_job_direct_creates_metadata_for_namespace(self, mock_kubeconfig):
        """Test submit_job_direct creates metadata dict when missing for namespace."""
        from unittest.mock import MagicMock, patch

        from cli.aws_client import RegionalStack
        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.get_regional_stack.return_value = RegionalStack(
            region="us-east-1",
            stack_name="gco-us-east-1",
            cluster_name="gco-us-east-1",
            status="CREATE_COMPLETE",
        )

        # Manifest without metadata — kind is Job but no name, so status check is skipped
        manifests = [{"apiVersion": "batch/v1", "kind": "Job"}]

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="job created", stderr="")

            result = manager.submit_job_direct(manifests, region="us-east-1", namespace="custom-ns")

            assert result["status"] == "success"

    @patch("cli.kubectl_helpers.update_kubeconfig")
    def test_submit_job_direct_creates_metadata_for_labels(self, mock_kubeconfig):
        """Test submit_job_direct creates metadata dict when missing for labels."""
        from unittest.mock import MagicMock, patch

        from cli.aws_client import RegionalStack
        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.get_regional_stack.return_value = RegionalStack(
            region="us-east-1",
            stack_name="gco-us-east-1",
            cluster_name="gco-us-east-1",
            status="CREATE_COMPLETE",
        )

        # Manifest without metadata
        manifests = [{"apiVersion": "batch/v1", "kind": "Job"}]

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="job created", stderr="")

            result = manager.submit_job_direct(manifests, region="us-east-1", labels={"team": "ml"})

            assert result["status"] == "success"

    @patch("cli.kubectl_helpers.update_kubeconfig")
    def test_submit_job_direct_creates_labels_dict_when_missing(self, mock_kubeconfig):
        """Test submit_job_direct creates labels dict when metadata exists but labels don't."""
        from unittest.mock import MagicMock, patch

        from cli.aws_client import RegionalStack
        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.get_regional_stack.return_value = RegionalStack(
            region="us-east-1",
            stack_name="gco-us-east-1",
            cluster_name="gco-us-east-1",
            status="CREATE_COMPLETE",
        )

        # Manifest with metadata but no labels
        manifests = [{"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "test"}}]

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                # _get_kubectl_job_status: job doesn't exist
                MagicMock(returncode=1, stdout="", stderr="not found"),
                # kubectl apply
                MagicMock(returncode=0, stdout="job created", stderr=""),
            ]

            result = manager.submit_job_direct(manifests, region="us-east-1", labels={"team": "ml"})

            assert result["status"] == "success"


class TestJobManagerListJobsAllRegions:
    """Tests for list_jobs with all_regions=True."""

    def test_list_jobs_all_regions_with_errors(self):
        """Test list_jobs continues when some regions have errors."""
        from unittest.mock import MagicMock

        from cli.aws_client import RegionalStack
        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.discover_regional_stacks.return_value = {
            "us-east-1": RegionalStack(
                region="us-east-1",
                stack_name="test",
                cluster_name="test",
                status="CREATE_COMPLETE",
            ),
            "us-west-2": RegionalStack(
                region="us-west-2",
                stack_name="test",
                cluster_name="test",
                status="CREATE_COMPLETE",
            ),
        }

        # First region succeeds, second fails
        def get_jobs_side_effect(region, namespace, status):
            if region == "us-east-1":
                return {
                    "jobs": [
                        {
                            "metadata": {"name": "job1", "namespace": "default"},
                            "spec": {},
                            "status": {},
                        }
                    ]
                }
            else:
                raise Exception("API error")

        manager._aws_client.get_jobs.side_effect = get_jobs_side_effect

        result = manager.list_jobs(all_regions=True)

        # Should have job from us-east-1, us-west-2 error is skipped
        assert len(result) == 1
        assert result[0].name == "job1"


class TestJobManagerSQSSubmitSuccess:
    """Tests for successful SQS job submission."""

    def test_submit_job_sqs_success(self):
        """Test successful SQS job submission."""
        from unittest.mock import MagicMock, patch

        from cli.aws_client import RegionalStack
        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.get_regional_stack.return_value = RegionalStack(
            region="us-east-1",
            stack_name="gco-us-east-1",
            cluster_name="gco-us-east-1",
            status="CREATE_COMPLETE",
        )

        manifests = [{"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "test-job"}}]

        with patch("boto3.client") as mock_boto:
            mock_cfn = MagicMock()
            mock_sqs = MagicMock()

            def client_factory(service, region_name=None):
                if service == "cloudformation":
                    return mock_cfn
                return mock_sqs

            mock_boto.side_effect = client_factory

            mock_cfn.describe_stacks.return_value = {
                "Stacks": [
                    {
                        "Outputs": [
                            {
                                "OutputKey": "JobQueueUrl",
                                "OutputValue": "https://sqs.us-east-1.amazonaws.com/123/queue",
                            }
                        ]
                    }
                ]
            }
            mock_sqs.send_message.return_value = {"MessageId": "msg-123"}

            result = manager.submit_job_sqs(manifests, region="us-east-1")

            assert result["status"] == "queued"
            assert result["method"] == "sqs"
            assert result["message_id"] == "msg-123"

    def test_submit_job_sqs_no_queue_url(self):
        """Test submit_job_sqs raises error when queue URL not found."""
        from unittest.mock import MagicMock, patch

        from cli.aws_client import RegionalStack
        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.get_regional_stack.return_value = RegionalStack(
            region="us-east-1",
            stack_name="gco-us-east-1",
            cluster_name="gco-us-east-1",
            status="CREATE_COMPLETE",
        )

        manifests = [{"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "test-job"}}]

        with patch("boto3.client") as mock_boto:
            mock_cfn = MagicMock()
            mock_boto.return_value = mock_cfn

            mock_cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}  # No JobQueueUrl

            with pytest.raises(ValueError, match="Job queue not found"):
                manager.submit_job_sqs(manifests, region="us-east-1")

    def test_submit_job_sqs_with_labels_and_namespace(self):
        """Test submit_job_sqs applies labels and namespace."""
        from unittest.mock import MagicMock, patch

        from cli.aws_client import RegionalStack
        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.get_regional_stack.return_value = RegionalStack(
            region="us-east-1",
            stack_name="gco-us-east-1",
            cluster_name="gco-us-east-1",
            status="CREATE_COMPLETE",
        )

        # Manifest without metadata
        manifests = [{"apiVersion": "batch/v1", "kind": "Job"}]

        with patch("boto3.client") as mock_boto:
            mock_cfn = MagicMock()
            mock_sqs = MagicMock()

            def client_factory(service, region_name=None):
                if service == "cloudformation":
                    return mock_cfn
                return mock_sqs

            mock_boto.side_effect = client_factory

            mock_cfn.describe_stacks.return_value = {
                "Stacks": [
                    {
                        "Outputs": [
                            {
                                "OutputKey": "JobQueueUrl",
                                "OutputValue": "https://sqs.us-east-1.amazonaws.com/123/queue",
                            }
                        ]
                    }
                ]
            }
            mock_sqs.send_message.return_value = {"MessageId": "msg-123"}

            result = manager.submit_job_sqs(
                manifests, region="us-east-1", namespace="custom-ns", labels={"team": "ml"}
            )

            assert result["status"] == "queued"
            assert result["namespace"] == "custom-ns"


class TestJobManagerGetQueueStatusSuccess:
    """Tests for successful queue status retrieval."""

    def test_get_queue_status_success(self):
        """Test successful queue status retrieval."""
        from unittest.mock import MagicMock, patch

        from cli.aws_client import RegionalStack
        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.get_regional_stack.return_value = RegionalStack(
            region="us-east-1",
            stack_name="gco-us-east-1",
            cluster_name="gco-us-east-1",
            status="CREATE_COMPLETE",
        )

        with patch("boto3.client") as mock_boto:
            mock_cfn = MagicMock()
            mock_sqs = MagicMock()

            def client_factory(service, region_name=None):
                if service == "cloudformation":
                    return mock_cfn
                return mock_sqs

            mock_boto.side_effect = client_factory

            mock_cfn.describe_stacks.return_value = {
                "Stacks": [
                    {
                        "Outputs": [
                            {
                                "OutputKey": "JobQueueUrl",
                                "OutputValue": "https://sqs.us-east-1.amazonaws.com/123/queue",
                            },
                            {
                                "OutputKey": "JobDlqUrl",
                                "OutputValue": "https://sqs.us-east-1.amazonaws.com/123/dlq",
                            },
                        ]
                    }
                ]
            }
            mock_sqs.get_queue_attributes.side_effect = [
                {
                    "Attributes": {
                        "ApproximateNumberOfMessages": "5",
                        "ApproximateNumberOfMessagesNotVisible": "2",
                        "ApproximateNumberOfMessagesDelayed": "1",
                    }
                },
                {"Attributes": {"ApproximateNumberOfMessages": "3"}},
            ]

            result = manager.get_queue_status("us-east-1")

            assert result["messages_available"] == 5
            assert result["messages_in_flight"] == 2
            assert result["messages_delayed"] == 1
            assert result["dlq_messages"] == 3

    def test_get_queue_status_no_queue_url(self):
        """Test get_queue_status raises error when queue URL not found."""
        from unittest.mock import MagicMock, patch

        from cli.aws_client import RegionalStack
        from cli.jobs import JobManager

        manager = JobManager()
        manager._aws_client = MagicMock()
        manager._aws_client.get_regional_stack.return_value = RegionalStack(
            region="us-east-1",
            stack_name="gco-us-east-1",
            cluster_name="gco-us-east-1",
            status="CREATE_COMPLETE",
        )

        with patch("boto3.client") as mock_boto:
            mock_cfn = MagicMock()
            mock_boto.return_value = mock_cfn

            mock_cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}  # No JobQueueUrl

            with pytest.raises(ValueError, match="Job queue not found"):
                manager.get_queue_status("us-east-1")
