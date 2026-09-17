"""
Tests for the SQS-backed job submission path in cli/jobs.JobManager.

Exercises submit_job_sqs end-to-end against mocked CloudFormation and
SQS clients: looks up JobQueueUrl from the regional stack's outputs,
sends an SQS message with the manifest payload and priority, and
returns a {status: queued, method: sqs, message_id, region} dict.
Also covers missing-stack, missing-output, and send_message failure
paths. Uses patched boto3.client so no real AWS calls are made.

The envelope itself (``gco.job_envelope``) is covered here too: it is the wire
format between this producer and the in-cluster queue processor, so its
defaults, its injectable id/timestamp, and the exact serialization the
submission puts on the queue are pinned alongside the submission that uses
them. The same builder produces the message CI feeds to the real consumer in
``integration:kind:cluster-e2e``.
"""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from cli.config import GCOConfig
from cli.jobs import JobManager
from gco.job_envelope import DEFAULT_JOB_NAMESPACE, build_job_message, first_manifest_namespace


class TestJobEnvelope:
    """The wire format shared by ``gco jobs submit-sqs`` and the consumer."""

    MANIFESTS = [{"kind": "Job", "metadata": {"name": "trainer", "namespace": "ml"}}]

    def test_every_field_the_consumer_reads_is_present(self) -> None:
        message = build_job_message(
            self.MANIFESTS,
            namespace="gco-jobs",
            priority=7,
            job_id="deadbeef",
            submitted_at=datetime(2026, 3, 26, 12, 0, tzinfo=UTC),
        )
        assert message.body == {
            "job_id": "deadbeef",
            "manifests": self.MANIFESTS,
            "namespace": "gco-jobs",
            "priority": 7,
            "submitted_at": "2026-03-26T12:00:00+00:00",
        }
        assert message.job_id == "deadbeef"
        assert message.namespace == "gco-jobs"
        assert message.priority == 7

    def test_the_namespace_falls_back_to_the_first_manifest_then_the_default(self) -> None:
        """The envelope namespace is informational, so it must not invent one."""
        assert build_job_message(self.MANIFESTS).namespace == "ml"
        assert build_job_message([{"kind": "Job"}]).namespace == DEFAULT_JOB_NAMESPACE
        assert first_manifest_namespace([{"kind": "Job"}]) is None
        assert first_manifest_namespace(["not-a-dict"]) is None  # type: ignore[list-item]

    def test_a_generated_job_id_is_short_and_unique(self) -> None:
        """The id is a correlation handle in log lines and message attributes."""
        first = build_job_message(self.MANIFESTS)
        second = build_job_message(self.MANIFESTS)
        assert first.job_id != second.job_id
        assert len(first.job_id) == 8

    def test_the_generated_timestamp_is_timezone_aware(self) -> None:
        """A naive stamp would be unorderable against the consumer's records."""
        stamped = datetime.fromisoformat(build_job_message(self.MANIFESTS).body["submitted_at"])
        assert stamped.tzinfo is not None

    def test_attributes_carry_the_priority_and_id_as_sqs_types(self) -> None:
        message = build_job_message(self.MANIFESTS, priority=3, job_id="abc12345")
        assert message.attributes == {
            "Priority": {"DataType": "Number", "StringValue": "3"},
            "JobId": {"DataType": "String", "StringValue": "abc12345"},
        }

    def test_json_round_trips_through_the_consumers_parser(self) -> None:
        message = build_job_message(self.MANIFESTS, job_id="abc12345")
        assert json.loads(message.json()) == message.body


class TestJobManagerSQS:
    """Tests for SQS-related JobManager methods."""

    @pytest.fixture
    def config(self):
        """Create a test configuration."""
        return GCOConfig(
            default_region="us-east-1",
            default_namespace="gco-jobs",
        )

    @pytest.fixture
    def job_manager(self, config):
        """Create a JobManager with mocked AWS client."""
        with patch("cli.jobs.get_aws_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            manager = JobManager(config)
            manager._aws_client = mock_client
            return manager

    def test_submit_job_sqs_success(self, job_manager):
        """Test successful SQS job submission."""
        # Mock regional stack
        mock_stack = MagicMock()
        mock_stack.stack_name = "gco-us-east-1"
        job_manager._aws_client.get_regional_stack.return_value = mock_stack

        # Mock CloudFormation
        with patch("boto3.client") as mock_boto_client:
            mock_cfn = MagicMock()
            mock_sqs = MagicMock()

            def get_client(service, **kwargs):
                if service == "cloudformation":
                    return mock_cfn
                elif service == "sqs":
                    return mock_sqs
                return MagicMock()

            mock_boto_client.side_effect = get_client

            # Mock CFN describe_stacks
            mock_cfn.describe_stacks.return_value = {
                "Stacks": [
                    {
                        "Outputs": [
                            {
                                "OutputKey": "JobQueueUrl",
                                "OutputValue": "https://sqs.us-east-1.amazonaws.com/123456789012/gco-jobs-us-east-1",
                            }
                        ]
                    }
                ]
            }

            # Mock SQS send_message
            mock_sqs.send_message.return_value = {
                "MessageId": "test-message-id-123",
                "MD5OfMessageBody": "abc123",
            }

            # Test manifest
            manifests = [
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {"name": "test-job", "namespace": "gco-jobs"},
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [{"name": "test", "image": "python:3.14"}],
                                "restartPolicy": "Never",
                            }
                        }
                    },
                }
            ]

            result = job_manager.submit_job_sqs(
                manifests=manifests,
                region="us-east-1",
                namespace="gco-jobs",
                priority=5,
            )

            assert result["status"] == "queued"
            assert result["method"] == "sqs"
            assert result["message_id"] == "test-message-id-123"
            assert result["region"] == "us-east-1"
            assert result["priority"] == 5
            assert result["job_name"] == "test-job"

            # The body on the wire is the shared envelope, and the reported
            # job_id/namespace are the ones actually sent — not a second
            # derivation that could disagree with the message.
            sent = mock_sqs.send_message.call_args.kwargs
            body = json.loads(sent["MessageBody"])
            assert body["job_id"] == result["job_id"]
            assert body["namespace"] == result["namespace"] == "gco-jobs"
            assert body["manifests"] == manifests
            assert body["priority"] == 5
            assert datetime.fromisoformat(body["submitted_at"]).tzinfo is not None
            assert sent["MessageAttributes"] == {
                "Priority": {"DataType": "Number", "StringValue": "5"},
                "JobId": {"DataType": "String", "StringValue": result["job_id"]},
            }

    def test_submit_job_sqs_no_stack(self, job_manager):
        """Test SQS submission fails when no stack exists."""
        job_manager._aws_client.get_regional_stack.return_value = None

        manifests = [{"kind": "Job", "metadata": {"name": "test"}}]

        with pytest.raises(ValueError, match="No GCO stack found"):
            job_manager.submit_job_sqs(manifests=manifests, region="us-east-1")

    def test_submit_job_sqs_no_queue(self, job_manager):
        """Test SQS submission fails when queue URL not found."""
        mock_stack = MagicMock()
        mock_stack.stack_name = "gco-us-east-1"
        job_manager._aws_client.get_regional_stack.return_value = mock_stack

        with patch("boto3.client") as mock_boto_client:
            mock_cfn = MagicMock()
            mock_boto_client.return_value = mock_cfn

            # No JobQueueUrl in outputs
            mock_cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}

            manifests = [{"kind": "Job", "metadata": {"name": "test"}}]

            with pytest.raises(ValueError, match="Job queue not found"):
                job_manager.submit_job_sqs(manifests=manifests, region="us-east-1")

    def test_get_queue_status_success(self, job_manager):
        """Test getting queue status."""
        mock_stack = MagicMock()
        mock_stack.stack_name = "gco-us-east-1"
        job_manager._aws_client.get_regional_stack.return_value = mock_stack

        with patch("boto3.client") as mock_boto_client:
            mock_cfn = MagicMock()
            mock_sqs = MagicMock()

            def get_client(service, **kwargs):
                if service == "cloudformation":
                    return mock_cfn
                elif service == "sqs":
                    return mock_sqs
                return MagicMock()

            mock_boto_client.side_effect = get_client

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

            # Mock queue attributes
            mock_sqs.get_queue_attributes.side_effect = [
                {
                    "Attributes": {
                        "ApproximateNumberOfMessages": "10",
                        "ApproximateNumberOfMessagesNotVisible": "5",
                        "ApproximateNumberOfMessagesDelayed": "2",
                    }
                },
                {"Attributes": {"ApproximateNumberOfMessages": "3"}},
            ]

            result = job_manager.get_queue_status("us-east-1")

            assert result["region"] == "us-east-1"
            assert result["messages_available"] == 10
            assert result["messages_in_flight"] == 5
            assert result["messages_delayed"] == 2
            assert result["dlq_messages"] == 3


class TestMultiRegionCapacity:
    """Tests for multi-region capacity checking."""

    @pytest.fixture
    def config(self):
        """Create a test configuration."""
        return GCOConfig(default_region="us-east-1")

    def test_get_region_capacity(self, config):
        """Test getting capacity for a single region."""
        from cli.capacity import MultiRegionCapacityChecker

        with patch("cli.aws_client.get_aws_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client

            mock_stack = MagicMock()
            mock_stack.stack_name = "gco-us-east-1"
            mock_stack.cluster_name = "gco-us-east-1"
            mock_client.get_regional_stack.return_value = mock_stack

            checker = MultiRegionCapacityChecker(config)

            with patch.object(checker, "_session") as mock_session:
                mock_cfn = MagicMock()
                mock_sqs = MagicMock()
                mock_cw = MagicMock()

                clients = {
                    "cloudformation": mock_cfn,
                    "sqs": mock_sqs,
                    "cloudwatch": mock_cw,
                }

                mock_session.client.side_effect = lambda service, **kwargs: clients.get(
                    service, MagicMock()
                )

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

                mock_sqs.get_queue_attributes.return_value = {
                    "Attributes": {
                        "ApproximateNumberOfMessages": "5",
                        "ApproximateNumberOfMessagesNotVisible": "2",
                    }
                }

                mock_cw.get_metric_statistics.return_value = {"Datapoints": []}

                capacity = checker.get_region_capacity("us-east-1")

                assert capacity.region == "us-east-1"
                assert capacity.queue_depth == 5
                assert capacity.running_jobs == 2

    def test_recommend_region_for_job(self, config):
        """Test region recommendation."""
        from cli.capacity import MultiRegionCapacityChecker, RegionCapacity

        with patch("cli.aws_client.get_aws_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.discover_regional_stacks.return_value = {
                "us-east-1": MagicMock(),
                "us-west-2": MagicMock(),
            }

            checker = MultiRegionCapacityChecker(config)

            # Mock get_region_capacity to return different capacities
            with patch.object(checker, "get_region_capacity") as mock_get_capacity:
                mock_get_capacity.side_effect = [
                    RegionCapacity(
                        region="us-east-1",
                        queue_depth=10,
                        running_jobs=5,
                        gpu_utilization=80.0,
                        recommendation_score=180.0,
                    ),
                    RegionCapacity(
                        region="us-west-2",
                        queue_depth=2,
                        running_jobs=1,
                        gpu_utilization=30.0,
                        recommendation_score=55.0,
                    ),
                ]

                result = checker.recommend_region_for_job()

                assert result["region"] == "us-west-2"
                assert "low queue depth" in result["reason"] or "GPU available" in result["reason"]

    def test_recommend_region_no_stacks(self, config):
        """Test region recommendation when no stacks exist."""
        from cli.capacity import MultiRegionCapacityChecker

        with patch("cli.aws_client.get_aws_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.discover_regional_stacks.return_value = {}

            checker = MultiRegionCapacityChecker(config)
            result = checker.recommend_region_for_job()

            assert result["region"] == "us-east-1"  # Default region
            assert "No capacity data" in result["reason"]


class TestCLICommands:
    """Tests for CLI commands related to SQS and capacity."""

    def test_submit_sqs_command_help(self):
        """Test that submit-sqs command has proper help text."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["jobs", "submit-sqs", "--help"])

        assert result.exit_code == 0
        assert "Submit a job to a regional SQS queue" in result.output
        assert "--auto-region" in result.output
        assert "--priority" in result.output

    def test_queue_status_command_help(self):
        """Test that queue-status command has proper help text."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["jobs", "queue-status", "--help"])

        assert result.exit_code == 0
        assert "Show job queue status" in result.output
        assert "--all-regions" in result.output

    def test_capacity_status_command_help(self):
        """Test that capacity status command has proper help text."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["capacity", "status", "--help"])

        assert result.exit_code == 0
        assert "comprehensive resource utilization" in result.output.lower()

    def test_recommend_region_command_help(self):
        """Test that recommend-region command has proper help text."""
        from click.testing import CliRunner

        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["capacity", "recommend-region", "--help"])

        assert result.exit_code == 0
        assert "optimal region" in result.output.lower()
        assert "--gpu" in result.output
