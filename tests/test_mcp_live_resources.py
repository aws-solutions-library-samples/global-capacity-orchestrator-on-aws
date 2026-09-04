"""
Tests for the live-state resources under gco_mcp/resources/.

Covers the six live-state resource paths:

* ``gco://jobs/{region}/{job_name}`` — wraps region-pinned ``kubectl get job``.
* ``gco://inference/{endpoint_name}`` — reads the inference DynamoDB store.
* ``gco://k8s/{region}/{namespace}/{kind}/{name}`` — wraps region-pinned ``kubectl get``.
* ``gco://cluster/{region}/topology`` — nodepools + Pending pods aggregator.
* ``costs://gco/summary/{days_window}`` — wraps ``gco costs summary``.
* ``tasks://gco/{task_id}`` — FastMCP task-state lookup.

Each test mocks the single underlying call (``cli_runner.subprocess.run``,
``cli_runner._run_cli``, or ``cli.inference.InferenceManager``) so the
resources never reach AWS or a live cluster. Mirrors the read_resource
pattern used by ``tests/test_mcp_image_resources.py``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure gco_mcp/ is importable, mirroring the other test modules.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

import run_mcp  # noqa: E402

_EKS_CONTEXT = "arn:aws:eks:us-east-1:123456789012:cluster/gco-us-east-1"


def _read_resource(uri: str) -> str:
    """Synchronous helper that returns the text content of a resource read."""
    result = asyncio.run(run_mcp.mcp.read_resource(uri))
    return result.contents[0].content


class TestEksContextResolution:
    @pytest.mark.parametrize(
        ("region", "partition"),
        [
            ("us-gov-west-1", "aws-us-gov"),
            ("eusc-de-east-1", "aws-eusc"),
            ("us-iso-east-1", "aws-iso"),
        ],
    )
    def test_builds_configured_project_account_and_partition_aware_arn(
        self,
        region: str,
        partition: str,
    ):
        sts = MagicMock()
        sts.get_caller_identity.return_value = {"Account": "123456789012"}
        session = MagicMock()
        session.get_partition_for_region.return_value = partition
        config = MagicMock(project_name="foo-regional-api-bar")

        with (
            patch("resources._eks.get_config", return_value=config) as get_config,
            patch("resources._eks.boto3.client", return_value=sts) as client,
            patch("resources._eks.boto3.session.Session", return_value=session),
        ):
            from resources._eks import eks_context_for_region

            arn = eks_context_for_region(region)

        assert arn == (
            f"arn:{partition}:eks:{region}:123456789012:cluster/foo-regional-api-bar-{region}"
        )
        get_config.assert_called_once_with()
        client.assert_called_once_with("sts", region_name=region)
        session.get_partition_for_region.assert_called_once_with(region)

    def test_rejects_invalid_sts_account(self):
        sts = MagicMock()
        sts.get_caller_identity.return_value = {"Account": "not-an-account"}
        with (
            patch("resources._eks.boto3.client", return_value=sts),
            pytest.raises(ValueError, match="account ID"),
        ):
            from resources._eks import eks_context_for_region

            eks_context_for_region("us-east-1", project_name="gco")

    def test_rejects_invalid_project_before_aws_calls(self):
        from resources._eks import eks_context_for_region

        with (
            patch("resources._eks.boto3.client") as client,
            pytest.raises(ValueError, match="project name"),
        ):
            eks_context_for_region("us-east-1", project_name="../other")

        client.assert_not_called()


# ---------------------------------------------------------------------------
# gco://jobs/{region}/{job_name}
# ---------------------------------------------------------------------------


class TestJobsLiveResource:
    def test_jobs_resource_returns_kubectl_yaml(self):
        fake = MagicMock(returncode=0, stdout="apiVersion: batch/v1\nkind: Job\n", stderr="")
        with (
            patch("resources.jobs.eks_context_for_region", return_value=_EKS_CONTEXT),
            patch("cli_runner.subprocess.run", return_value=fake) as mock,
        ):
            content = _read_resource("gco://jobs/us-east-1/my-job")
        assert "kind: Job" in content
        argv = mock.call_args[0][0]
        assert argv[:3] == ["kubectl", "get", "job"]
        assert "my-job" in argv
        assert "-n" in argv
        assert "gco-jobs" in argv
        assert argv[-2:] == ["--context", _EKS_CONTEXT]

    def test_jobs_legacy_uri_requires_region(self):
        with patch("cli_runner.subprocess.run") as mock:
            content = _read_resource("gco://jobs/my-job")
        mock.assert_not_called()
        parsed = json.loads(content)
        assert parsed["code"] == "eks_region_required"

    def test_jobs_resource_rejects_invalid_region(self):
        with patch("cli_runner.subprocess.run") as mock:
            content = _read_resource("gco://jobs/not_a_region/my-job")
        mock.assert_not_called()
        parsed = json.loads(content)
        assert parsed["error"] == "invalid region"
        assert parsed["value"] == "not_a_region"

    def test_jobs_resource_rejects_invalid_name(self):
        with patch("cli_runner.subprocess.run") as mock:
            content = _read_resource("gco://jobs/us-east-1/Bad_Name")
        mock.assert_not_called()
        parsed = json.loads(content)
        assert parsed["error"] == "invalid job_name"
        assert parsed["value"] == "Bad_Name"

    def test_jobs_resource_reports_unresolvable_eks_context(self):
        """A credential/session failure while resolving the context is a structured error."""
        with (
            patch("resources.jobs.eks_context_for_region", side_effect=ValueError("no creds")),
            patch("cli_runner.subprocess.run") as mock,
        ):
            content = _read_resource("gco://jobs/us-east-1/my-job")
        mock.assert_not_called()
        parsed = json.loads(content)
        assert parsed["error"] == "unable to resolve EKS context"
        assert "no creds" in parsed["detail"]

    def test_jobs_resource_reports_kubectl_failure(self):
        fake = MagicMock(returncode=1, stdout="", stderr="not found\n")
        with (
            patch("resources.jobs.eks_context_for_region", return_value=_EKS_CONTEXT),
            patch("cli_runner.subprocess.run", return_value=fake),
        ):
            content = _read_resource("gco://jobs/us-east-1/missing-job")
        parsed = json.loads(content)
        assert "not found" in parsed["error"]
        assert parsed["exit_code"] == 1

    def test_jobs_resource_reports_kubectl_not_found(self):
        """When ``kubectl`` is missing from PATH, return a structured error."""
        with (
            patch("resources.jobs.eks_context_for_region", return_value=_EKS_CONTEXT),
            patch("cli_runner.subprocess.run", side_effect=FileNotFoundError),
        ):
            content = _read_resource("gco://jobs/us-east-1/some-job")
        parsed = json.loads(content)
        assert parsed["error"] == "kubectl not found"

    def test_jobs_resource_reports_kubectl_timeout(self):
        """A ``kubectl`` invocation that exceeds the timeout returns a structured error."""
        import subprocess as _subprocess

        with (
            patch("resources.jobs.eks_context_for_region", return_value=_EKS_CONTEXT),
            patch(
                "cli_runner.subprocess.run",
                side_effect=_subprocess.TimeoutExpired(cmd="kubectl", timeout=30),
            ),
        ):
            content = _read_resource("gco://jobs/us-east-1/some-job")
        parsed = json.loads(content)
        assert "timed out" in parsed["error"]


# ---------------------------------------------------------------------------
# gco://inference/{endpoint_name}
# ---------------------------------------------------------------------------


class TestInferenceLiveResource:
    def test_inference_resource_returns_endpoint_record_as_json(self):
        manager = MagicMock()
        manager.get_endpoint.return_value = {
            "endpoint_name": "my-llm",
            "spec": {"image": "vllm/vllm-openai:v0.22.0"},
            "desired_state": "running",
        }
        with patch("cli.inference.InferenceManager", return_value=manager):
            content = _read_resource("gco://inference/my-llm")
        manager.get_endpoint.assert_called_once_with("my-llm")
        parsed = json.loads(content)
        assert parsed["endpoint_name"] == "my-llm"
        assert parsed["spec"]["image"] == "vllm/vllm-openai:v0.22.0"

    def test_inference_resource_missing_endpoint_returns_error_json(self):
        manager = MagicMock()
        manager.get_endpoint.return_value = None
        with patch("cli.inference.InferenceManager", return_value=manager):
            content = _read_resource("gco://inference/missing")
        parsed = json.loads(content)
        assert parsed["error"] == "endpoint not found"
        assert parsed["endpoint_name"] == "missing"

    def test_inference_resource_rejects_invalid_name(self):
        with patch("cli.inference.InferenceManager") as mock:
            content = _read_resource("gco://inference/UPPER")
        mock.assert_not_called()
        parsed = json.loads(content)
        assert parsed["error"] == "invalid endpoint_name"

    def test_inference_resource_wraps_manager_exception_as_error_json(self):
        """``InferenceManager.get_endpoint`` raising returns a structured error.

        Pins the broad ``except Exception`` branch in
        ``gco_mcp/resources/inference.py``: any unexpected error from
        the manager (network blip, malformed DynamoDB record, etc.)
        surfaces as ``{"error": str(e), "endpoint_name": ...}``
        rather than propagating to the resource layer where it would
        become an opaque JSON-RPC ``-32603`` internal error.
        """
        manager = MagicMock()
        manager.get_endpoint.side_effect = RuntimeError("dynamodb throttled")
        with patch("cli.inference.InferenceManager", return_value=manager):
            content = _read_resource("gco://inference/some-endpoint")
        parsed = json.loads(content)
        assert "throttled" in parsed["error"]
        assert parsed["endpoint_name"] == "some-endpoint"


# ---------------------------------------------------------------------------
# gco://k8s/{region}/{namespace}/{kind}/{name}
# ---------------------------------------------------------------------------


class TestK8sLiveResource:
    def test_k8s_resource_returns_kubectl_yaml(self):
        fake = MagicMock(returncode=0, stdout="apiVersion: apps/v1\nkind: Deployment\n", stderr="")
        with (
            patch("resources.k8s.eks_context_for_region", return_value=_EKS_CONTEXT),
            patch("cli_runner.subprocess.run", return_value=fake) as mock,
        ):
            content = _read_resource("gco://k8s/us-east-1/gco-jobs/deployment/my-app")
        assert "kind: Deployment" in content
        argv = mock.call_args[0][0]
        # ``kubectl get <kind> <name> -n <ns> -o yaml``
        assert argv[:2] == ["kubectl", "get"]
        assert argv[2] == "deployment"
        assert argv[3] == "my-app"
        assert "gco-jobs" in argv
        assert argv[-2:] == ["--context", _EKS_CONTEXT]

    def test_k8s_legacy_uri_requires_region(self):
        with patch("cli_runner.subprocess.run") as mock:
            content = _read_resource("gco://k8s/gco-jobs/deployment/my-app")
        mock.assert_not_called()
        parsed = json.loads(content)
        assert parsed["code"] == "eks_region_required"

    def test_k8s_resource_rejects_invalid_kind(self):
        with patch("cli_runner.subprocess.run") as mock:
            content = _read_resource("gco://k8s/us-east-1/gco-jobs/bad;kind/my-app")
        mock.assert_not_called()
        parsed = json.loads(content)
        assert parsed["error"] == "invalid kind"

    def test_k8s_resource_rejects_invalid_namespace(self):
        with patch("cli_runner.subprocess.run") as mock:
            content = _read_resource("gco://k8s/us-east-1/Bad_NS/pod/my-pod")
        mock.assert_not_called()
        parsed = json.loads(content)
        assert parsed["error"] == "invalid namespace"

    def test_k8s_resource_rejects_invalid_name(self):
        with patch("cli_runner.subprocess.run") as mock:
            content = _read_resource("gco://k8s/us-east-1/gco-jobs/pod/Bad_Pod")
        mock.assert_not_called()
        parsed = json.loads(content)
        assert parsed["error"] == "invalid name"


# ---------------------------------------------------------------------------
# gco://cluster/{region}/topology
# ---------------------------------------------------------------------------


class TestClusterTopologyResource:
    def test_topology_aggregates_nodepools_and_pending_pods(self):
        nodepools_payload = json.dumps(
            {"nodepools": [{"name": "gpu", "instance_type": "g5.xlarge"}]}
        )
        pending_pods_payload = json.dumps({"items": [{"metadata": {"name": "stuck-pod"}}]})
        kubectl_result = MagicMock(returncode=0, stdout=pending_pods_payload, stderr="")
        with (
            patch("resources.cluster.eks_context_for_region", return_value=_EKS_CONTEXT),
            patch("cli_runner._run_cli", return_value=nodepools_payload) as mock_cli,
            patch("cli_runner.subprocess.run", return_value=kubectl_result) as mock_run,
        ):
            content = _read_resource("gco://cluster/us-east-1/topology")
        parsed = json.loads(content)
        assert parsed["region"] == "us-east-1"
        assert parsed["nodepools"]["nodepools"][0]["name"] == "gpu"
        assert parsed["pending_pods"]["items"][0]["metadata"]["name"] == "stuck-pod"
        # Verified the two underlying calls fired.
        cli_args = mock_cli.call_args[0]
        assert cli_args[:2] == ("nodepools", "list")
        assert "us-east-1" in cli_args
        kubectl_argv = mock_run.call_args[0][0]
        assert kubectl_argv[:3] == ["kubectl", "get", "pods"]
        assert "status.phase=Pending" in kubectl_argv
        assert kubectl_argv[-2:] == ["--context", _EKS_CONTEXT]

    def test_topology_rejects_invalid_region(self):
        with (
            patch("cli_runner._run_cli") as mock_cli,
            patch("cli_runner.subprocess.run") as mock_run,
        ):
            content = _read_resource("gco://cluster/not_a_region/topology")
        mock_cli.assert_not_called()
        mock_run.assert_not_called()
        parsed = json.loads(content)
        assert parsed["error"] == "invalid region"


# ---------------------------------------------------------------------------
# costs://gco/summary/{days_window}
# ---------------------------------------------------------------------------


class TestCostsSummaryResource:
    def test_costs_summary_resource_invokes_cli(self):
        with patch("cli_runner._run_cli", return_value='{"total": 123.45}') as mock:
            content = _read_resource("costs://gco/summary/30")
        assert content == '{"total": 123.45}'
        argv = mock.call_args[0]
        assert argv == ("costs", "summary", "--days", "30")

    def test_costs_summary_rejects_non_integer_window(self):
        with patch("cli_runner._run_cli") as mock:
            content = _read_resource("costs://gco/summary/notanumber")
        mock.assert_not_called()
        parsed = json.loads(content)
        assert "positive integer" in parsed["error"]

    def test_costs_summary_rejects_zero_window(self):
        with patch("cli_runner._run_cli") as mock:
            content = _read_resource("costs://gco/summary/0")
        mock.assert_not_called()
        parsed = json.loads(content)
        assert "positive integer" in parsed["error"]


# ---------------------------------------------------------------------------
# tasks://gco/{task_id}
# ---------------------------------------------------------------------------


class TestTaskStatusResource:
    def test_tasks_resource_returns_state_when_extension_answers(self):
        # Patch the tasks extension's ``tasks/get`` handler — the single
        # seam the resource reads through — to return a canned record.
        record = MagicMock()
        record.model_dump.return_value = {
            "status": "working",
            "progress": {"completed": 1, "total": 5},
        }
        with patch(
            "fastmcp_tasks.handlers.tasks_get",
            new=AsyncMock(return_value=record),
        ):
            content = _read_resource("tasks://gco/abc123")
        parsed = json.loads(content)
        assert parsed["task_id"] == "abc123"
        assert parsed["state"]["status"] == "working"

    def test_tasks_resource_returns_not_found_for_unknown_id(self):
        # No patching: the real extension handler reports an id the docket
        # has never seen as not-found, and the resource maps that to the
        # documented error JSON rather than crashing.
        content = _read_resource("tasks://gco/no-such-task")
        parsed = json.loads(content)
        assert parsed["error"] == "task not found"
        assert parsed["task_id"] == "no-such-task"

    def test_tasks_resource_rejects_invalid_task_id(self):
        content = _read_resource("tasks://gco/has spaces and !!! chars")
        parsed = json.loads(content)
        assert parsed["error"] == "invalid task_id"


# ---------------------------------------------------------------------------
# Resources As Tools round-trip
# ---------------------------------------------------------------------------


class TestResourcesAsToolsRoundTrip:
    """The synthetic ``read_resource`` tool exposed by the Resources As Tools
    transform must return the same content as a direct resource read for
    every live-state resource path."""

    def test_synthetic_read_resource_proxies_inference_payload(self):
        manager = MagicMock()
        manager.get_endpoint.return_value = {
            "endpoint_name": "my-llm",
            "spec": {"image": "vllm/vllm-openai:v0.22.0"},
        }

        async def _drive() -> tuple[str, object]:
            with patch("cli.inference.InferenceManager", return_value=manager):
                direct = await run_mcp.mcp.read_resource("gco://inference/my-llm")
                tool_result = await run_mcp.mcp.call_tool(
                    "read_resource", {"uri": "gco://inference/my-llm"}
                )
            return direct.contents[0].content, tool_result

        direct_content, tool_result = asyncio.run(_drive())
        # The synthetic tool returns text content blocks; the first block's
        # text must match the resource handler's direct return.
        assert tool_result.content, "read_resource returned no content blocks"
        assert tool_result.content[0].text == direct_content
