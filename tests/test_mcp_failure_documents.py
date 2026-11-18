"""A ``gco`` command that reports failing checks as a JSON document reaches MCP as data.

Several status commands print one JSON document and exit 1 when something
they inspect is unhealthy: ``gco stacks addons status`` (add-on rows),
``gco stacks capabilities status``, ``gco stacks hyperpod status``,
``gco stacks hyperpod preflight``, and ``gco analytics doctor``. The CLI
runners used to return such a document as a JSON *string* inside ``error``
(or drop it when stderr was non-empty). They now return it parsed under
``output`` next to ``error`` and ``exit_code``, so the caller sees both the
failure and the structured result, without a traceback.

Two commands print one document in JSON mode for the same reason:
``gco analytics doctor`` prints ``{"passed", "checks"}`` instead of its
``✓``/``✗`` lines, and ``gco stacks access`` prints the cluster, principal
and kubectl reachability instead of the raw node table (success) or nothing
(an unreachable API server, which only warns).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gco_mcp"))

import cli_runner
import run_mcp

from cli.main import cli

_ROWS = [{"region": "us-east-1", "name": "kueue", "status": "failed", "message": "boom"}]


def _completed(stdout: str, stderr: str = "", code: int = 1) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr=stderr)


class _Process:
    def __init__(self, stdout: str, stderr: str = "", code: int = 1) -> None:
        self._out = stdout.encode()
        self._err = stderr.encode()
        self.returncode: int | None = None
        self._code = code

    async def communicate(self) -> tuple[bytes, bytes]:
        self.returncode = self._code
        return self._out, self._err


def _run_sync(stdout: str, stderr: str = "", code: int = 1) -> dict[str, Any]:
    with patch.object(cli_runner.subprocess, "run", return_value=_completed(stdout, stderr, code)):
        return json.loads(cli_runner._run_cli("stacks", "addons", "status"))


def _run_async(stdout: str, stderr: str = "", code: int = 1) -> dict[str, Any]:
    async def run() -> str:
        spawn = AsyncMock(return_value=_Process(stdout, stderr, code))
        with patch("cli_runner.asyncio.create_subprocess_exec", spawn):
            return await cli_runner._run_cli_async("stacks", "addons", "status")

    return json.loads(asyncio.run(run()))


@pytest.mark.parametrize("run", [_run_sync, _run_async], ids=["sync", "async"])
class TestFailureEnvelope:
    def test_json_document_with_empty_stderr_is_returned_parsed(self, run: Any) -> None:
        assert run(json.dumps(_ROWS, indent=2)) == {
            "error": "gco exited with status 1",
            "exit_code": 1,
            "output": _ROWS,
        }

    def test_json_document_is_kept_next_to_a_stderr_message(self, run: Any) -> None:
        payload = run(json.dumps(_ROWS), "✗ [us-west-2] AccessDenied")
        assert payload == {
            "error": "✗ [us-west-2] AccessDenied",
            "exit_code": 1,
            "output": _ROWS,
        }

    @pytest.mark.parametrize(
        ("stdout", "stderr", "expected_error"),
        [
            ("", "denied", "denied"),
            ("plain text", "", "plain text"),
            ("NaN", "", "NaN"),  # non-standard constants are not JSON here either
            ('{"a": 1}{"b": 2}', "", '{"a": 1}{"b": 2}'),
        ],
        ids=["stderr-only", "text-stdout", "nan", "two-documents"],
    )
    def test_anything_else_keeps_the_plain_envelope(
        self, run: Any, stdout: str, stderr: str, expected_error: str
    ) -> None:
        assert run(stdout, stderr, 3) == {"error": expected_error, "exit_code": 3}


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        ("addons_status", {"region": "us-east-1"}),
        ("eks_capabilities_status", {"region": "us-east-1"}),
        ("hyperpod_status", {"region": "us-east-1"}),
        ("hyperpod_preflight", {"region": "us-east-1"}),
        ("analytics_doctor", {}),
    ],
)
def test_status_tools_return_the_document_with_the_failure(
    tool: str, kwargs: dict[str, Any]
) -> None:
    document = {"region": "us-east-1", "healthy": False}
    with patch.object(cli_runner.subprocess, "run", return_value=_completed(json.dumps(document))):
        payload = json.loads(asyncio.run(getattr(run_mcp, tool)(**kwargs)))
    assert payload == {"error": "gco exited with status 1", "exit_code": 1, "output": document}


# ---------------------------------------------------------------------------
# The real CLI commands print one JSON document and exit 1 when unhealthy
# ---------------------------------------------------------------------------


def _invoke(args: list[str]) -> Any:
    return CliRunner().invoke(cli, ["--output", "json", *args])


def test_cli_addons_status_prints_rows_and_exits_1_when_unhealthy() -> None:
    rows = [{"region": "us-east-1", "name": "kueue", "status": "failed", "message": "boom"}]
    with patch("cli.commands.stacks_cmd._addons_status_one", return_value=(rows, False)):
        result = _invoke(["stacks", "addons", "status", "-r", "us-east-1"])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == rows


def test_cli_analytics_doctor_prints_a_json_document() -> None:
    with (
        patch("cli.analytics_user_mgmt.check_stack_complete", return_value=(True, "")),
        patch("cli.analytics_user_mgmt.check_ssm_parameter", return_value=(False, "missing")),
        patch("cli.analytics_user_mgmt.scan_orphan_analytics_resources", return_value=[]),
    ):
        result = _invoke(["analytics", "doctor"])
    assert result.exit_code == 1
    document = json.loads(result.stdout)
    assert document["passed"] is False
    failed = [check for check in document["checks"] if not check["ok"]]
    assert failed and all(check["remediation"] for check in failed)
    assert "Doctor checks failed" in result.stderr


def test_cli_analytics_doctor_json_success() -> None:
    with (
        patch("cli.analytics_user_mgmt.check_stack_complete", return_value=(True, "")),
        patch("cli.analytics_user_mgmt.check_ssm_parameter", return_value=(True, "")),
        patch("cli.analytics_user_mgmt.scan_orphan_analytics_resources", return_value=[]),
    ):
        result = _invoke(["analytics", "doctor"])
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["passed"] is True
    assert all(check["ok"] for check in document["checks"])


def test_cli_analytics_doctor_table_output_is_unchanged() -> None:
    with (
        patch("cli.analytics_user_mgmt.check_stack_complete", return_value=(True, "")),
        patch("cli.analytics_user_mgmt.check_ssm_parameter", return_value=(False, "missing")),
        patch("cli.analytics_user_mgmt.scan_orphan_analytics_resources", return_value=[]),
    ):
        result = CliRunner().invoke(cli, ["--output", "table", "analytics", "doctor"])
    assert result.exit_code == 1
    assert "  ✓ cdk.json parses as JSON" in result.stdout
    assert "    → deploy " in result.stdout


def _access_stub(kubectl: tuple[int, str, str]) -> Any:
    """``subprocess.run`` stand-in for ``gco stacks access``; ``kubectl`` is (rc, out, err)."""

    def run(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["aws", "eks", "describe-cluster"]:
            return _completed('{"public": true, "private": true, "publicCidrs": []}', code=0)
        if cmd[:3] == ["aws", "sts", "get-caller-identity"]:
            return _completed("arn:aws:iam::123456789012:user/dev\n", code=0)
        if cmd[0] == "kubectl":
            code, out, err = kubectl
            return _completed(out, err, code)
        return _completed("", code=0)

    return run


def _access(kubectl: tuple[int, str, str], output: str = "json") -> Any:
    with patch("subprocess.run", side_effect=_access_stub(kubectl)), patch("time.sleep"):
        return CliRunner().invoke(cli, ["--output", output, "stacks", "access", "-r", "us-east-1"])


_NODES = (
    "NAME STATUS ROLES AGE VERSION\nnode-1 Ready <none> 1d v1.33\nnode-2 Ready <none> 1d v1.33\n"
)


def test_cli_stacks_access_prints_one_document_when_kubectl_connects() -> None:
    result = _access((0, _NODES, ""))
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "cluster": "gco-us-east-1",
        "region": "us-east-1",
        "principal_arn": "arn:aws:iam::123456789012:user/dev",
        "api_server_reachable": True,
        "node_count": 2,
        "kubectl_error": None,
        "warning": None,
    }


def test_cli_stacks_access_reports_an_unreachable_api_server_in_the_document() -> None:
    """The warning path used to print nothing, so JSON callers saw only ``{"status": "ok"}``."""
    refused = "Unable to connect to the server: dial tcp 10.0.0.1:443: i/o timeout"
    result = _access((1, "", refused))
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["api_server_reachable"] is False
    assert document["node_count"] is None
    assert document["kubectl_error"] == refused
    assert document["warning"].startswith("kubectl could not reach the API server.")
    assert document["warning"] in result.stderr


def test_cli_stacks_access_table_output_keeps_the_node_table() -> None:
    result = _access((0, _NODES, ""), output="table")
    assert result.exit_code == 0, result.output
    assert "node-2 Ready" in result.stdout
    assert "Access configured successfully. 2 node(s) ready." in result.stdout
    assert "api_server_reachable" not in result.stdout


def test_setup_cluster_access_returns_the_cli_document() -> None:
    document = {"cluster": "gco-us-east-1", "api_server_reachable": False}
    completed = _completed(json.dumps(document), code=0)
    with patch.object(cli_runner.subprocess, "run", return_value=completed) as run:
        payload = json.loads(run_mcp.setup_cluster_access(region="us-east-1"))
    assert payload == document
    assert run.call_args.args[0][-4:] == ["stacks", "access", "-r", "us-east-1"]
