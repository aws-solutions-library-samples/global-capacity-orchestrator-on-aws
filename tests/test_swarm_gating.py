"""Gating tests for the swarm feature flag and allowlist exclusions.

``GCO_ENABLE_SWARM`` follows the house feature-flag contract: a constant
in ``gco_mcp/feature_flags.py`` registered in ``ALL_FLAGS`` (so the
umbrella override, the startup log, and every iterating caller cover it
with no further changes), default-off, and resolvable through
``gco autopilot --enable``. The allowlist side of the recursion guard is
pinned here too: the all-tools expansion never resolves a
loop-management name — the ``mission_*`` control tools, the ``swarm_*``
MCP tools, or the in-process supervisor tools.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

import feature_flags  # noqa: E402
from mission.validation import (  # noqa: E402
    SUPERVISOR_TOOLS,
    SWARM_EXCLUDED_TOOLS,
    SWARM_MCP_TOOLS,
    resolve_effective_allowlist,
)


class TestSwarmFlagRegistration:
    def test_flag_constant_and_membership(self) -> None:
        """FLAG_SWARM is GCO_ENABLE_SWARM and registered in ALL_FLAGS.

        Membership is what keeps iterating callers (the umbrella
        truth-table, the startup-log emitter, the feature-flags
        resource) covering the flag with no further changes.
        """
        assert feature_flags.FLAG_SWARM == "GCO_ENABLE_SWARM"
        assert feature_flags.FLAG_SWARM in feature_flags.ALL_FLAGS

    def test_default_off(self) -> None:
        """With neither the flag nor the umbrella set, swarm is disabled."""
        clean = {"GCO_ENABLE_SWARM": "", "GCO_ENABLE_ALL_TOOLS": ""}
        with patch.dict(os.environ, clean):
            assert feature_flags.is_enabled(feature_flags.FLAG_SWARM) is False

    def test_enabled_by_own_flag(self) -> None:
        """GCO_ENABLE_SWARM=true enables the flag on its own."""
        env = {"GCO_ENABLE_SWARM": "true", "GCO_ENABLE_ALL_TOOLS": ""}
        with patch.dict(os.environ, env):
            assert feature_flags.is_enabled(feature_flags.FLAG_SWARM) is True

    def test_enabled_by_umbrella(self) -> None:
        """The GCO_ENABLE_ALL_TOOLS umbrella covers the swarm flag."""
        env = {"GCO_ENABLE_SWARM": "", "GCO_ENABLE_ALL_TOOLS": "true"}
        with patch.dict(os.environ, env):
            assert feature_flags.is_enabled(feature_flags.FLAG_SWARM) is True


class TestAllowlistExclusions:
    def test_excluded_set_is_the_union_of_the_three_families(self) -> None:
        """The exclusion set covers control, supervisor, and swarm names."""
        assert "mission_start" in SWARM_EXCLUDED_TOOLS
        assert SUPERVISOR_TOOLS <= SWARM_EXCLUDED_TOOLS
        assert SWARM_MCP_TOOLS <= SWARM_EXCLUDED_TOOLS

    def test_allow_all_expansion_excludes_loop_management_names(self) -> None:
        """A default all-tools resolution never reaches a loop manager.

        Even when swarm and mission tools are registered (their flags
        enabled), the expansion resolves around them — the same guard
        the mission control tools have always had.
        """
        registered = {
            "find_docs": object(),
            "find_examples": object(),
            "mission_start": object(),
            "swarm_status": object(),
            "swarm_start": object(),
        }
        resolved = resolve_effective_allowlist(
            allow_all_tools=True,
            explicit_allowlist=None,
            registered_tools=registered,
        )
        assert resolved == ["find_docs", "find_examples"]


# ---------------------------------------------------------------------------
# MCP registration gating (reload pattern, matching the managed-config tests)
# ---------------------------------------------------------------------------

SWARM_TOOLS = (
    "swarm_start",
    "swarm_iterate",
    "swarm_status",
    "swarm_abort",
    "swarm_list",
    "swarm_plan",
)


def _list_tool_names() -> set[str]:
    import asyncio

    import run_mcp

    tools = asyncio.run(run_mcp.mcp._list_tools())
    return {tool.name for tool in tools}


def _reload_run_mcp() -> None:
    import importlib

    import run_mcp

    importlib.reload(run_mcp)


def _strip_swarm_tools() -> None:
    """Remove any swarm registrations left by a prior test's reload."""
    import contextlib

    import run_mcp

    for name in SWARM_TOOLS:
        with contextlib.suppress(Exception):
            run_mcp.mcp.local_provider.remove_tool(name)


class TestSwarmToolRegistration:
    def test_absent_by_default(self) -> None:
        """With the flag unset, no swarm tool reaches the registry."""
        clean = {"GCO_ENABLE_SWARM": "", "GCO_ENABLE_ALL_TOOLS": ""}
        with patch.dict(os.environ, clean):
            _strip_swarm_tools()
            _reload_run_mcp()
            names = _list_tool_names()
        for tool in SWARM_TOOLS:
            assert tool not in names, f"{tool} registered without GCO_ENABLE_SWARM"

    def test_registered_under_flag(self) -> None:
        """GCO_ENABLE_SWARM=true registers exactly the six swarm tools."""
        env = {"GCO_ENABLE_SWARM": "true", "GCO_ENABLE_ALL_TOOLS": ""}
        try:
            with patch.dict(os.environ, env):
                _reload_run_mcp()
                names = _list_tool_names()
            for tool in SWARM_TOOLS:
                assert tool in names, f"{tool} missing under GCO_ENABLE_SWARM"
        finally:
            clean = {"GCO_ENABLE_SWARM": "", "GCO_ENABLE_ALL_TOOLS": ""}
            with patch.dict(os.environ, clean):
                _strip_swarm_tools()
                _reload_run_mcp()
                _strip_swarm_tools()

    def test_docstrings_carry_gate_prefix(self) -> None:
        """Every registered swarm tool declares its gate in its docstring."""
        env = {"GCO_ENABLE_SWARM": "true", "GCO_ENABLE_ALL_TOOLS": ""}
        try:
            with patch.dict(os.environ, env):
                _reload_run_mcp()
                import asyncio

                import run_mcp

                tools = asyncio.run(run_mcp.mcp._list_tools())
                by_name = {tool.name: tool for tool in tools}
                for name in SWARM_TOOLS:
                    description = str(getattr(by_name[name], "description", ""))
                    assert "[gated by GCO_ENABLE_SWARM]" in description
        finally:
            clean = {"GCO_ENABLE_SWARM": "", "GCO_ENABLE_ALL_TOOLS": ""}
            with patch.dict(os.environ, clean):
                _strip_swarm_tools()
                _reload_run_mcp()
                _strip_swarm_tools()
