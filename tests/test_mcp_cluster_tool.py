"""Functional test for the `cluster_tunnel_command` MCP tool (gco_mcp/tools/cluster.py).

The tool is a thin wrapper that shells out to `gco cluster tunnel --print` via
``cli_runner._run_cli``. This asserts the exact argv it constructs (so the MCP
surface stays in lockstep with the CLI) with the CLI subprocess mocked.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

# Ensure gco_mcp/ is importable (mirrors tests/test_mcp_server.py).
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from tools import cluster as cluster_tool  # noqa: E402


def test_tool_builds_print_argv_with_region_and_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, tuple] = {}

    def _fake_run_cli(*args: str) -> str:
        captured["args"] = args
        return '{"reachable": "ssm-tunnel"}'

    monkeypatch.setattr(cluster_tool.cli_runner, "_run_cli", _fake_run_cli)
    out = asyncio.run(
        cluster_tool.cluster_tunnel_command(region="us-east-1", instance_id="i-0123456789abcdef0")
    )
    assert '"reachable"' in out
    assert captured["args"] == (
        "cluster",
        "tunnel",
        "--print",
        "--local-port",
        "8443",
        "--region",
        "us-east-1",
        "--via-ssm",
        "i-0123456789abcdef0",
    )


def test_tool_omits_optional_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, tuple] = {}

    def _fake_run_cli(*args: str) -> str:
        captured["args"] = args
        return "{}"

    monkeypatch.setattr(cluster_tool.cli_runner, "_run_cli", _fake_run_cli)
    asyncio.run(cluster_tool.cluster_tunnel_command())
    # No region / instance → only the base --print invocation with default port.
    assert captured["args"] == ("cluster", "tunnel", "--print", "--local-port", "8443")


def test_tool_passes_custom_local_port(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, tuple] = {}
    monkeypatch.setattr(
        cluster_tool.cli_runner, "_run_cli", lambda *a: captured.__setitem__("args", a) or "{}"
    )
    asyncio.run(cluster_tool.cluster_tunnel_command(local_port=9443))
    assert "9443" in captured["args"]
    assert "--local-port" in captured["args"]
