"""A ``gco`` command that reports failing checks as a JSON document reaches MCP as data.

Several status commands print one JSON document and exit 1 when something
they inspect is unhealthy: ``gco stacks addons status`` (add-on rows),
``gco stacks capabilities status``, ``gco stacks hyperpod status``,
``gco stacks hyperpod preflight``, and ``gco analytics doctor``. The CLI
runners used to return such a document as a JSON *string* inside ``error``
(or drop it when stderr was non-empty). They now return it parsed under
``output`` next to ``error`` and ``exit_code``, so the caller sees both the
failure and the structured result, without a traceback.
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
