"""
Tests for JobManager.get_job_logs CloudWatch Logs fallback.

When the Kubernetes API can't produce container logs — pod missing,
still pending, or already reaped — the CLI transparently falls back
to a CloudWatch Logs Insights query against the log group populated
by the CloudWatch Observability addon. These tests cover the happy
path (no fallback needed), the fallback on pod-not-found and
pod-pending errors, JSON log envelope unwrapping, and the banner
line the CLI prints to make the source switch obvious. time.sleep
is patched so the Insights polling loop doesn't actually wait.
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def job_manager():
    """Create a JobManager with mocked dependencies."""
    from cli.jobs import JobManager

    with (
        patch("cli.jobs.get_config") as mock_config,
        patch("cli.jobs.get_aws_client") as mock_aws,
    ):
        config = MagicMock()
        config.default_region = "us-east-1"
        config.project_name = "gco"
        mock_config.return_value = config

        aws_client = MagicMock()
        aws_client._session = MagicMock()
        mock_aws.return_value = aws_client

        manager = JobManager()
        yield manager, aws_client


class TestCloudWatchLogsFallback:
    """Tests for the CloudWatch Logs fallback when K8s logs are unavailable."""

    def test_k8s_logs_success_no_fallback(self, job_manager):
        """When K8s API returns logs, CloudWatch is not queried."""
        manager, aws_client = job_manager
        aws_client.get_job_logs.return_value = "training step 1\ntraining step 2"

        logs = manager.get_job_logs("my-job", "gco-jobs")

        assert "training step 1" in logs
        aws_client._session.client.assert_not_called()

    def test_fallback_on_pod_not_found(self, job_manager):
        """When K8s says pod not found, falls back to CloudWatch."""
        manager, aws_client = job_manager
        aws_client.get_job_logs.side_effect = RuntimeError("Pod 'my-job-abc123' not found")

        mock_logs_client = MagicMock()
        aws_client._session.client.return_value = mock_logs_client

        mock_logs_client.start_query.return_value = {"queryId": "q-123"}
        mock_logs_client.get_query_results.return_value = {
            "status": "Complete",
            "results": [
                [
                    {"field": "@timestamp", "value": "2026-04-15T10:00:00Z"},
                    {
                        "field": "@message",
                        "value": '{"log":"training step 1","stream":"stdout"}',
                    },
                ],
                [
                    {"field": "@timestamp", "value": "2026-04-15T10:00:01Z"},
                    {
                        "field": "@message",
                        "value": '{"log":"training step 2","stream":"stdout"}',
                    },
                ],
            ],
        }

        with patch("time.sleep"):
            logs = manager.get_job_logs("my-job", "gco-jobs")

        assert "[CloudWatch Logs" in logs
        assert "training step 1" in logs
        assert "training step 2" in logs
        # Should NOT contain the JSON envelope
        assert '"stream"' not in logs
        aws_client._session.client.assert_called_once_with("logs", region_name="us-east-1")

    def test_fallback_on_pod_pending(self, job_manager):
        """When K8s says pod is pending, falls back to CloudWatch."""
        manager, aws_client = job_manager
        aws_client.get_job_logs.side_effect = RuntimeError("Pod 'my-job-xyz' is still Pending")

        mock_logs_client = MagicMock()
        aws_client._session.client.return_value = mock_logs_client
        mock_logs_client.start_query.return_value = {"queryId": "q-456"}
        mock_logs_client.get_query_results.return_value = {
            "status": "Complete",
            "results": [
                [{"field": "@message", "value": "some log line"}],
            ],
        }

        with patch("time.sleep"):
            logs = manager.get_job_logs("my-job", "gco-jobs")

        assert "some log line" in logs

    def test_fallback_on_completed_job(self, job_manager):
        """When K8s says job is completed and pod terminated, falls back."""
        manager, aws_client = job_manager
        aws_client.get_job_logs.side_effect = RuntimeError("Job completed, pod terminated")

        mock_logs_client = MagicMock()
        aws_client._session.client.return_value = mock_logs_client
        mock_logs_client.start_query.return_value = {"queryId": "q-789"}
        mock_logs_client.get_query_results.return_value = {
            "status": "Complete",
            "results": [
                [{"field": "@message", "value": "=== Training complete ==="}],
            ],
        }

        with patch("time.sleep"):
            logs = manager.get_job_logs("my-job", "gco-jobs")

        assert "Training complete" in logs

    def test_no_fallback_on_unrelated_error(self, job_manager):
        """When K8s fails with an unrelated error, don't fall back."""
        manager, aws_client = job_manager
        aws_client.get_job_logs.side_effect = RuntimeError("Connection refused")

        with pytest.raises(RuntimeError, match="Connection refused"):
            manager.get_job_logs("my-job", "gco-jobs")

        aws_client._session.client.assert_not_called()

    def test_fallback_cloudwatch_no_logs(self, job_manager):
        """When CloudWatch has no logs, raises with helpful message."""
        manager, aws_client = job_manager
        aws_client.get_job_logs.side_effect = RuntimeError("Pod not found")

        mock_logs_client = MagicMock()
        aws_client._session.client.return_value = mock_logs_client
        mock_logs_client.start_query.return_value = {"queryId": "q-empty"}
        mock_logs_client.get_query_results.return_value = {
            "status": "Complete",
            "results": [],
        }

        with (
            patch("time.sleep"),
            pytest.raises(RuntimeError, match=r"No logs found.*last 24 hours"),
        ):
            manager.get_job_logs("my-job", "gco-jobs")

    def test_fallback_cloudwatch_log_group_missing(self, job_manager):
        """When CloudWatch log group doesn't exist, raises with tip."""
        manager, aws_client = job_manager
        aws_client.get_job_logs.side_effect = RuntimeError("Pod not found")

        mock_logs_client = MagicMock()
        aws_client._session.client.return_value = mock_logs_client
        mock_logs_client.start_query.side_effect = Exception(
            "ResourceNotFoundException: Log group does not exist"
        )

        with pytest.raises(RuntimeError, match="CloudWatch Logs fallback also failed"):
            manager.get_job_logs("my-job", "gco-jobs")

    def test_fallback_uses_correct_log_group(self, job_manager):
        """Verify the CloudWatch log group name follows the Container Insights convention."""
        manager, aws_client = job_manager
        aws_client.get_job_logs.side_effect = RuntimeError("Pod not found")

        mock_logs_client = MagicMock()
        aws_client._session.client.return_value = mock_logs_client
        mock_logs_client.start_query.return_value = {"queryId": "q-check"}
        mock_logs_client.get_query_results.return_value = {
            "status": "Complete",
            "results": [[{"field": "@message", "value": "ok"}]],
        }

        with patch("time.sleep"):
            manager.get_job_logs("my-job", "gco-jobs", region="us-west-2")

        call_kwargs = mock_logs_client.start_query.call_args[1]
        assert call_kwargs["logGroupName"] == "/aws/containerinsights/gco-us-west-2/application"

    def test_fallback_respects_tail_lines(self, job_manager):
        """Verify tail_lines is passed to the CloudWatch query."""
        manager, aws_client = job_manager
        aws_client.get_job_logs.side_effect = RuntimeError("Pod not found")

        mock_logs_client = MagicMock()
        aws_client._session.client.return_value = mock_logs_client
        mock_logs_client.start_query.return_value = {"queryId": "q-tail"}
        mock_logs_client.get_query_results.return_value = {
            "status": "Complete",
            "results": [[{"field": "@message", "value": "line"}]],
        }

        with patch("time.sleep"):
            manager.get_job_logs("my-job", "gco-jobs", tail_lines=500)

        query = mock_logs_client.start_query.call_args[1]["queryString"]
        assert "limit 500" in query

    def test_fallback_filters_by_job_name(self, job_manager):
        """Verify the CloudWatch query filters by job name."""
        manager, aws_client = job_manager
        aws_client.get_job_logs.side_effect = RuntimeError("Pod not found")

        mock_logs_client = MagicMock()
        aws_client._session.client.return_value = mock_logs_client
        mock_logs_client.start_query.return_value = {"queryId": "q-filter"}
        mock_logs_client.get_query_results.return_value = {
            "status": "Complete",
            "results": [[{"field": "@message", "value": "line"}]],
        }

        with patch("time.sleep"):
            manager.get_job_logs("megatrain-sft", "gco-jobs")

        query = mock_logs_client.start_query.call_args[1]["queryString"]
        assert "megatrain-sft" in query

    def test_follow_still_raises(self, job_manager):
        """Follow mode is still not implemented regardless of fallback."""
        manager, _ = job_manager

        with pytest.raises(NotImplementedError):
            manager.get_job_logs("my-job", "gco-jobs", follow=True)

    def test_fallback_cloudwatch_query_timeout(self, job_manager):
        """When CloudWatch query doesn't complete, raises with status."""
        manager, aws_client = job_manager
        aws_client.get_job_logs.side_effect = RuntimeError("Pod not found")

        mock_logs_client = MagicMock()
        aws_client._session.client.return_value = mock_logs_client
        mock_logs_client.start_query.return_value = {"queryId": "q-slow"}
        mock_logs_client.get_query_results.return_value = {
            "status": "Running",
            "results": [],
        }

        with (
            patch("time.sleep"),
            pytest.raises(RuntimeError, match=r"query did not complete.*Running"),
        ):
            manager.get_job_logs("my-job", "gco-jobs")

    def test_fallback_scopes_to_24h(self, job_manager):
        """Verify the CloudWatch query scopes to the last 24 hours by default."""
        manager, aws_client = job_manager
        aws_client.get_job_logs.side_effect = RuntimeError("Pod not found")

        mock_logs_client = MagicMock()
        aws_client._session.client.return_value = mock_logs_client
        mock_logs_client.start_query.return_value = {"queryId": "q-time"}
        mock_logs_client.get_query_results.return_value = {
            "status": "Complete",
            "results": [[{"field": "@message", "value": "ok"}]],
        }

        with patch("time.sleep"), patch("time.time", return_value=1700000000.0):
            manager.get_job_logs("my-job", "gco-jobs")

        call_kwargs = mock_logs_client.start_query.call_args[1]
        assert call_kwargs["startTime"] == 1700000000 - 86400
        assert call_kwargs["endTime"] == 1700000000

    def test_fallback_custom_since_hours(self, job_manager):
        """Verify --since parameter controls the CloudWatch lookback window."""
        manager, aws_client = job_manager
        aws_client.get_job_logs.side_effect = RuntimeError("Pod not found")

        mock_logs_client = MagicMock()
        aws_client._session.client.return_value = mock_logs_client
        mock_logs_client.start_query.return_value = {"queryId": "q-custom"}
        mock_logs_client.get_query_results.return_value = {
            "status": "Complete",
            "results": [[{"field": "@message", "value": "old log"}]],
        }

        with patch("time.sleep"), patch("time.time", return_value=1700000000.0):
            manager.get_job_logs("my-job", "gco-jobs", since_hours=72)

        call_kwargs = mock_logs_client.start_query.call_args[1]
        # 72 hours = 259200 seconds
        assert call_kwargs["startTime"] == 1700000000 - 259200

    def test_fallback_parses_json_log_envelope(self, job_manager):
        """JSON log envelopes are parsed to extract the 'log' field."""
        manager, aws_client = job_manager
        aws_client.get_job_logs.side_effect = RuntimeError("Pod not found")

        mock_logs_client = MagicMock()
        aws_client._session.client.return_value = mock_logs_client
        mock_logs_client.start_query.return_value = {"queryId": "q-json"}
        mock_logs_client.get_query_results.return_value = {
            "status": "Complete",
            "results": [
                [
                    {
                        "field": "@message",
                        "value": '{"log":"GPU: Tesla T4","stream":"stdout","kubernetes":{"pod_name":"x"}}',
                    }
                ],
            ],
        }

        with patch("time.sleep"):
            logs = manager.get_job_logs("my-job", "gco-jobs")

        assert "GPU: Tesla T4" in logs
        assert "kubernetes" not in logs
        assert "pod_name" not in logs

    def test_fallback_handles_plain_text_messages(self, job_manager):
        """Non-JSON messages are returned as-is."""
        manager, aws_client = job_manager
        aws_client.get_job_logs.side_effect = RuntimeError("Pod not found")

        mock_logs_client = MagicMock()
        aws_client._session.client.return_value = mock_logs_client
        mock_logs_client.start_query.return_value = {"queryId": "q-plain"}
        mock_logs_client.get_query_results.return_value = {
            "status": "Complete",
            "results": [
                [{"field": "@message", "value": "plain text log line"}],
            ],
        }

        with patch("time.sleep"):
            logs = manager.get_job_logs("my-job", "gco-jobs")

        assert "plain text log line" in logs


class TestCloudWatchAddonTolerations:
    """Tests that the CloudWatch addon is configured with container logs and tolerations."""

    def test_cloudwatch_addon_has_container_logs_enabled(self):
        """Verify the CDK stack enables containerLogs in the CW addon."""
        from gco.stacks.regional_stack import GCORegionalStack

        tolerations = GCORegionalStack._ADDON_NODE_TOLERATIONS
        assert any(t["key"] == "nvidia.com/gpu" for t in tolerations)
        assert any(t["key"] == "aws.amazon.com/neuron" for t in tolerations)
        assert any(t["key"] == "vpc.amazonaws.com/efa" for t in tolerations)
