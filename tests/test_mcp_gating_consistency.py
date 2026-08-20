"""Registry / re-export / gating-table consistency under the umbrella flag.

``tests/test_mcp_transforms.py`` proves every *default-registered* tool is
reachable as ``run_mcp.<name>`` and listed in ``__all__`` — but it runs
flag-off, so the gated families are invisible to it. That blind spot let two
real drifts ship: ``mission_memory_search`` registered under
``GCO_ENABLE_MISSION`` yet missed the gating table, the import block, the
reload re-export loop, and ``_PUBLIC_EXPORTS`` (fixed in d44f2b4), and the
seven ``GCO_ENABLE_CONFIG_MANAGEMENT`` tools missed the import block and
``_PUBLIC_EXPORTS`` (fixed alongside this guard). Both were one-name-per-
roster omissions: exactly the drift class hand-maintained rosters invite.

This module closes the blind spot by snapshotting the registry twice — once
under a clean flag-free env, once under ``GCO_ENABLE_ALL_TOOLS`` (the
umbrella that satisfies every ``is_enabled`` gate) — and holding four
invariants:

1. every umbrella-registered tool is reachable as ``run_mcp.<name>``;
2. every umbrella-registered tool appears in ``run_mcp.__all__``;
3. every tool that registers *only* under flags appears in
   ``resources/self.py``'s ``_TOOL_GATING_TABLE`` (so the
   ``mcp://gco/feature-flags`` resource and the ``__all__`` filter both see
   it);
4. every gating-table entry is a real, umbrella-registered tool (no stale or
   renamed names).

The flag list is taken from ``feature_flags.ALL_FLAGS`` at run time, so a new
flag is covered the day it is added, and a tool added to any gated family
without full roster wiring fails here instead of shipping half-exported.

Snapshots are expensive (each rebuilds the FastMCP instance and re-fires
every decorator), so a module-scoped fixture computes both once, evaluates
the attribute/``__all__`` checks while the umbrella state is live, and then
restores the clean default posture for neighbouring test files.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

# Ensure gco_mcp/ is importable, mirroring every other test module.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

import feature_flags  # noqa: E402
import pytest  # noqa: E402

# Transform-synthesized tools are not ``@mcp.tool`` functions and are never
# re-exported on run_mcp (same set test_mcp_transforms.py excludes).
_SYNTHETIC_TOOLS = frozenset({"search_tools", "call_tool", "list_resources", "read_resource"})


def _clean_env() -> dict[str, str]:
    """Every known flag explicitly empty, tool search off. Self-maintaining."""
    env = dict.fromkeys(feature_flags.ALL_FLAGS, "")
    env["GCO_ENABLE_ALL_TOOLS"] = ""
    env["GCO_MCP_TOOL_SEARCH"] = "off"
    return env


def _reload_mcp_with_env() -> object:
    """Rebuild the FastMCP instance and re-register every tool/resource.

    Same recipe as ``tests/test_mcp_transforms.py``: drop every cached
    ``tools.*`` / ``resources.*`` module so the freshly reloaded ``server``
    builds a clean FastMCP and every decorator re-fires against it under the
    *current* environment. Returns the reloaded ``run_mcp`` module.
    """
    import run_mcp
    import server

    cached_submodules = [
        name
        for name in list(sys.modules)
        if name.startswith("tools.")
        or name.startswith("resources.")
        or name in ("tools", "resources")
    ]
    for name in cached_submodules:
        sys.modules.pop(name, None)
    importlib.reload(server)
    importlib.reload(run_mcp)
    return run_mcp


def _registered_tool_names(run_mcp: object) -> set[str]:
    """Full registered set via ``_list_tools()`` (bypasses search transforms)."""
    tools = asyncio.run(run_mcp.mcp._list_tools())
    return {t.name for t in tools} - _SYNTHETIC_TOOLS


@dataclass
class _Snapshots:
    default_registered: set[str] = field(default_factory=set)
    umbrella_registered: set[str] = field(default_factory=set)
    umbrella_missing_attr: list[str] = field(default_factory=list)
    umbrella_missing_all: list[str] = field(default_factory=list)
    gating_table: dict[str, str] = field(default_factory=dict)


@pytest.fixture(scope="module")
def snapshots() -> _Snapshots:
    """Default and umbrella registry snapshots; default posture restored after."""
    result = _Snapshots()
    clean = _clean_env()
    try:
        with patch.dict(os.environ, clean, clear=False):
            run_mcp = _reload_mcp_with_env()
            result.default_registered = _registered_tool_names(run_mcp)

        umbrella = dict(clean)
        umbrella["GCO_ENABLE_ALL_TOOLS"] = "true"
        with patch.dict(os.environ, umbrella, clear=False):
            run_mcp = _reload_mcp_with_env()
            result.umbrella_registered = _registered_tool_names(run_mcp)
            # Attribute / __all__ membership must be evaluated while the
            # umbrella state is live on the module.
            result.umbrella_missing_attr = sorted(
                name for name in result.umbrella_registered if not hasattr(run_mcp, name)
            )
            result.umbrella_missing_all = sorted(
                name for name in result.umbrella_registered if name not in run_mcp.__all__
            )
            from resources.self import _TOOL_GATING_TABLE

            result.gating_table = dict(_TOOL_GATING_TABLE)
    finally:
        # Restore the flag-free default registry for neighbouring test files.
        with patch.dict(os.environ, clean, clear=False):
            _reload_mcp_with_env()
    return result


class TestSnapshotSanity:
    """The fixture measured something real before any invariant is trusted."""

    def test_default_registry_is_populated(self, snapshots: _Snapshots):
        assert len(snapshots.default_registered) > 50, (
            "flag-free reload produced an implausibly small registry; "
            "the snapshot machinery is broken, not the rosters"
        )

    def test_umbrella_strictly_extends_default(self, snapshots: _Snapshots):
        assert snapshots.default_registered <= snapshots.umbrella_registered, (
            "default-registered tools vanished under the umbrella flag: "
            f"{sorted(snapshots.default_registered - snapshots.umbrella_registered)!r}"
        )
        assert snapshots.umbrella_registered - snapshots.default_registered, (
            "umbrella flag registered nothing new; gated families missing entirely"
        )


class TestUmbrellaReexportSurface:
    """Invariants 1 and 2: reachable attributes and ``__all__`` membership."""

    def test_every_registered_tool_reachable_as_attribute(self, snapshots: _Snapshots):
        assert not snapshots.umbrella_missing_attr, (
            "registered tools not reachable as run_mcp.<name> under the umbrella "
            f"flag (import block or reload re-export roster is stale): "
            f"{snapshots.umbrella_missing_attr!r}"
        )

    def test_every_registered_tool_in_dunder_all(self, snapshots: _Snapshots):
        assert not snapshots.umbrella_missing_all, (
            "registered tools absent from run_mcp.__all__ under the umbrella "
            f"flag (_PUBLIC_EXPORTS or the gating table is stale): "
            f"{snapshots.umbrella_missing_all!r}"
        )


class TestGatingTableConsistency:
    """Invariants 3 and 4: the static gating table matches gated reality."""

    def test_every_gated_tool_is_in_the_table(self, snapshots: _Snapshots):
        gated = snapshots.umbrella_registered - snapshots.default_registered
        unmapped = sorted(gated - set(snapshots.gating_table))
        assert not unmapped, (
            "tools register only under a flag but are missing from "
            "resources/self.py _TOOL_GATING_TABLE (feature-flags resource "
            f"under-reports them; __all__ treats them as ungated): {unmapped!r}"
        )

    def test_every_table_entry_is_a_registered_tool(self, snapshots: _Snapshots):
        stale = sorted(set(snapshots.gating_table) - snapshots.umbrella_registered)
        assert not stale, (
            "gating-table entries name tools that never register even under "
            f"the umbrella flag (stale or renamed): {stale!r}"
        )

    def test_table_flags_are_known_flags(self, snapshots: _Snapshots):
        known = set(feature_flags.ALL_FLAGS)
        unknown = sorted(
            f"{tool} -> {flag}"
            for tool, flag in snapshots.gating_table.items()
            if flag not in known
        )
        assert not unknown, f"gating-table entries reference unknown flags: {unknown!r}"
