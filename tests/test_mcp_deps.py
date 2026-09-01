"""Tests for the MCP ``deps_scan`` tool (gco_mcp/tools/deps.py)."""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

import run_mcp  # noqa: E402  -- registers tools on the shared server


def _run(coro):
    return asyncio.run(coro)


def test_deps_scan_is_registered_ungated() -> None:
    """The tool registers unconditionally as a safe observability tool."""
    tool = _run(run_mcp.mcp.get_tool("deps_scan"))
    assert tool is not None
    assert {"safe", "observability"} <= set(tool.tags)


def test_full_scan_argv_and_timeout() -> None:
    canned = json.dumps({"has_drift": True, "scan_complete": True, "report_markdown": "# r"})
    with patch("cli_runner._run_cli_async", new=AsyncMock(return_value=canned)) as runner:
        result = _run(run_mcp.deps_scan())
    runner.assert_awaited_once_with("deps", "scan", timeout_seconds=1800)
    assert json.loads(result)["has_drift"] is True


def test_nodepools_only_argv_and_tighter_timeout() -> None:
    canned = json.dumps({"nodepools_only": True, "has_drift": False})
    with patch("cli_runner._run_cli_async", new=AsyncMock(return_value=canned)) as runner:
        result = _run(run_mcp.deps_scan(nodepools_only=True))
    runner.assert_awaited_once_with("deps", "scan", "--nodepools-only", timeout_seconds=300)
    assert json.loads(result)["nodepools_only"] is True


def test_cli_error_envelope_passes_through() -> None:
    """cli_runner's own JSON error envelopes are returned verbatim."""
    envelope = json.dumps({"error": "Command timed out after 1800 seconds"})
    with patch("cli_runner._run_cli_async", new=AsyncMock(return_value=envelope)):
        result = _run(run_mcp.deps_scan())
    assert json.loads(result)["error"].startswith("Command timed out")


def test_unparseable_output_is_wrapped() -> None:
    with patch("cli_runner._run_cli_async", new=AsyncMock(return_value="not json at all")):
        result = _run(run_mcp.deps_scan())
    payload = json.loads(result)
    assert payload["error"] == "deps scan produced unparseable output"
    assert payload["raw"] == "not json at all"
