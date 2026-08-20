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

Each snapshot runs in a **subprocess** with the target env, importing
``run_mcp`` fresh and shipping results back as JSON. A first draft instead
reloaded ``server``/``run_mcp`` in-process (the ``test_mcp_transforms.py``
recipe) and restored afterwards — and the restore was not faithful: resource
registrations went missing on the shared FastMCP singleton and every
resource-reading test that sorted after this file failed in CI
(``Unknown resource: 'images://gco/index'``). Subprocess isolation makes the
guard hermetic: the test process's singleton is never touched, so file
ordering and xdist scheduling cannot be affected.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import pytest

_GCO_MCP_DIR = Path(__file__).resolve().parent.parent / "gco_mcp"

# Ensure gco_mcp/ is importable in THIS process only for feature_flags —
# a plain constants module whose import has no registration side effects.
sys.path.insert(0, str(_GCO_MCP_DIR))

import feature_flags  # noqa: E402

# Sentinel separating the snapshot JSON from anything the server import
# writes to stdout (audit records target logging handlers, but the guard
# must not depend on that staying true).
_MARKER = "<<<GCO_GATING_SNAPSHOT_JSON>>>"

# Transform-synthesized tools are not ``@mcp.tool`` functions and are never
# re-exported on run_mcp (same set test_mcp_transforms.py excludes).
_SNAPSHOT_SCRIPT = textwrap.dedent(
    """
    import asyncio
    import json
    import sys

    sys.path.insert(0, {gco_mcp_dir!r})

    import run_mcp
    from resources.self import _TOOL_GATING_TABLE

    synthetic = {{"search_tools", "call_tool", "list_resources", "read_resource"}}
    registered = sorted(
        {{t.name for t in asyncio.run(run_mcp.mcp._list_tools())}} - synthetic
    )
    payload = {{
        "registered": registered,
        "missing_attr": sorted(n for n in registered if not hasattr(run_mcp, n)),
        "missing_all": sorted(n for n in registered if n not in run_mcp.__all__),
        "gating_table": dict(_TOOL_GATING_TABLE),
    }}
    print({marker!r} + json.dumps(payload))
    """
)


def _clean_env() -> dict[str, str]:
    """Every known flag explicitly empty, tool search off. Self-maintaining."""
    env = dict.fromkeys(feature_flags.ALL_FLAGS, "")
    env["GCO_ENABLE_ALL_TOOLS"] = ""
    env["GCO_MCP_TOOL_SEARCH"] = "off"
    return env


def _snapshot(flag_overrides: dict[str, str]) -> dict[str, object]:
    """Import run_mcp in a subprocess under ``flag_overrides``; return its report."""
    env = {**os.environ, **_clean_env(), **flag_overrides}
    script = _SNAPSHOT_SCRIPT.format(gco_mcp_dir=str(_GCO_MCP_DIR), marker=_MARKER)
    result = subprocess.run(  # noqa: S603 — fixed interpreter, generated script
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, (
        f"snapshot subprocess failed (rc={result.returncode}); stderr tail:\n"
        + "\n".join(result.stderr.splitlines()[-25:])
    )
    for line in result.stdout.splitlines():
        if line.startswith(_MARKER):
            payload: dict[str, object] = json.loads(line[len(_MARKER) :])
            return payload
    raise AssertionError(
        "snapshot subprocess produced no report line; stdout tail:\n"
        + "\n".join(result.stdout.splitlines()[-25:])
    )


@dataclass
class _Snapshots:
    default_registered: set[str] = field(default_factory=set)
    umbrella_registered: set[str] = field(default_factory=set)
    umbrella_missing_attr: list[str] = field(default_factory=list)
    umbrella_missing_all: list[str] = field(default_factory=list)
    gating_table: dict[str, str] = field(default_factory=dict)


@pytest.fixture(scope="module")
def snapshots() -> _Snapshots:
    """Default and umbrella registry snapshots, each from a fresh subprocess."""
    default = _snapshot({})
    umbrella = _snapshot({"GCO_ENABLE_ALL_TOOLS": "true"})
    return _Snapshots(
        default_registered=set(default["registered"]),
        umbrella_registered=set(umbrella["registered"]),
        umbrella_missing_attr=list(umbrella["missing_attr"]),
        umbrella_missing_all=list(umbrella["missing_all"]),
        gating_table=dict(umbrella["gating_table"]),
    )


class TestSnapshotSanity:
    """The fixture measured something real before any invariant is trusted."""

    def test_default_registry_is_populated(self, snapshots: _Snapshots):
        assert len(snapshots.default_registered) > 50, (
            "flag-free subprocess produced an implausibly small registry; "
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
