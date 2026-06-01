"""
Tests for the live-state resources under mcp/resources/.

Covers the six live-state resource paths:

* ``gco://jobs/{job_name}`` — wraps ``kubectl get job ... -o yaml``.
* ``gco://inference/{endpoint_name}`` — reads the inference DynamoDB store.
* ``gco://k8s/{namespace}/{kind}/{name}`` — wraps ``kubectl get`` for any kind.
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
import contextlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure mcp/ is importable, mirroring the other test modules.
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))

import run_mcp  # noqa: E402


def _read_resource(uri: str) -> str:
    """Synchronous helper that returns the text content of a resource read."""
    result = asyncio.run(run_mcp.mcp.read_resource(uri))
    return result.contents[0].content


# ---------------------------------------------------------------------------
# gco://jobs/{job_name}
# ---------------------------------------------------------------------------


class TestJobsLiveResource:
    def test_jobs_resource_returns_kubectl_yaml(self):
        fake = MagicMock(returncode=0, stdout="apiVersion: batch/v1\nkind: Job\n", stderr="")
        with patch("cli_runner.subprocess.run", return_value=fake) as mock:
            content = _read_resource("gco://jobs/my-job")
        assert "kind: Job" in content
        argv = mock.call_args[0][0]
        assert argv[:3] == ["kubectl", "get", "job"]
        assert "my-job" in argv
        assert "-n" in argv
        assert "gco-jobs" in argv
        assert argv[-2:] == ["-o", "yaml"]

    def test_jobs_resource_rejects_invalid_name(self):
        with patch("cli_runner.subprocess.run") as mock:
            content = _read_resource("gco://jobs/Bad_Name")
        mock.assert_not_called()
        parsed = json.loads(content)
        assert parsed["error"] == "invalid job_name"
        assert parsed["value"] == "Bad_Name"

    def test_jobs_resource_reports_kubectl_failure(self):
        fake = MagicMock(returncode=1, stdout="", stderr="not found\n")
        with patch("cli_runner.subprocess.run", return_value=fake):
            content = _read_resource("gco://jobs/missing-job")
        parsed = json.loads(content)
        assert "not found" in parsed["error"]
        assert parsed["exit_code"] == 1

    def test_jobs_resource_reports_kubectl_not_found(self):
        """When ``kubectl`` is missing from PATH, return a structured error."""
        with patch("cli_runner.subprocess.run", side_effect=FileNotFoundError):
            content = _read_resource("gco://jobs/some-job")
        parsed = json.loads(content)
        assert parsed["error"] == "kubectl not found"

    def test_jobs_resource_reports_kubectl_timeout(self):
        """A ``kubectl`` invocation that exceeds the timeout returns a structured error."""
        import subprocess as _subprocess

        with patch(
            "cli_runner.subprocess.run",
            side_effect=_subprocess.TimeoutExpired(cmd="kubectl", timeout=30),
        ):
            content = _read_resource("gco://jobs/some-job")
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
        ``mcp/resources/inference.py``: any unexpected error from
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
# gco://k8s/{namespace}/{kind}/{name}
# ---------------------------------------------------------------------------


class TestK8sLiveResource:
    def test_k8s_resource_returns_kubectl_yaml(self):
        fake = MagicMock(returncode=0, stdout="apiVersion: apps/v1\nkind: Deployment\n", stderr="")
        with patch("cli_runner.subprocess.run", return_value=fake) as mock:
            content = _read_resource("gco://k8s/gco-jobs/deployment/my-app")
        assert "kind: Deployment" in content
        argv = mock.call_args[0][0]
        # ``kubectl get <kind> <name> -n <ns> -o yaml``
        assert argv[:2] == ["kubectl", "get"]
        assert argv[2] == "deployment"
        assert argv[3] == "my-app"
        assert "gco-jobs" in argv

    def test_k8s_resource_rejects_invalid_kind(self):
        with patch("cli_runner.subprocess.run") as mock:
            content = _read_resource("gco://k8s/gco-jobs/bad;kind/my-app")
        mock.assert_not_called()
        parsed = json.loads(content)
        assert parsed["error"] == "invalid kind"

    def test_k8s_resource_rejects_invalid_namespace(self):
        with patch("cli_runner.subprocess.run") as mock:
            content = _read_resource("gco://k8s/Bad_NS/pod/my-pod")
        mock.assert_not_called()
        parsed = json.loads(content)
        assert parsed["error"] == "invalid namespace"

    def test_k8s_resource_rejects_invalid_name(self):
        with patch("cli_runner.subprocess.run") as mock:
            content = _read_resource("gco://k8s/gco-jobs/pod/Bad_Pod")
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
    def test_tasks_resource_returns_state_when_accessor_available(self):
        # Patch the runtime FastMCP instance to expose a ``get_task`` accessor
        # the resource handler can call. ``object.__setattr__`` bypasses
        # FastMCP's frozen-attribute guard for the duration of this test.
        record = {"status": "running", "progress": {"completed": 1, "total": 5}}
        try:
            object.__setattr__(run_mcp.mcp, "get_task", lambda tid: record)
            content = _read_resource("tasks://gco/abc123")
        finally:
            with contextlib.suppress(AttributeError):
                object.__delattr__(run_mcp.mcp, "get_task")
        parsed = json.loads(content)
        assert parsed["task_id"] == "abc123"
        assert parsed["state"]["status"] == "running"

    def test_tasks_resource_returns_graceful_error_when_unavailable(self):
        # No accessor on the live mcp instance — the handler returns the
        # documented graceful error JSON rather than crashing.
        if hasattr(run_mcp.mcp, "get_task"):
            with contextlib.suppress(AttributeError):
                object.__delattr__(run_mcp.mcp, "get_task")
        content = _read_resource("tasks://gco/no-such-task")
        parsed = json.loads(content)
        assert parsed["error"] == "task protocol not available"
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
