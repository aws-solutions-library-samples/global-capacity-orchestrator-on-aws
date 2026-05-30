"""MCP-tool tests for the ``mission_start`` all-tools resolution path.

These tests round-trip ``mission_start`` through the FastMCP in-process
``Client`` and assert how the new ``allow_all_tools`` boolean parameter
shapes the persisted session's ``tool_allowlist``. The handler reads the
live tool registry via two module-level helpers
(``_registered_tools_dict`` and ``_registered_tool_tags``); most cases
here patch those helpers with a small, deterministic in-memory registry
so the resolved list can be asserted exactly. The registration-separation
cases deliberately use the real registry to prove that starting a session
neither adds nor removes registered tools.

Isolation mirrors ``tests/test_mission_mcp_tools.py``: the autouse fixture
strips every ``mission_*`` registration before and after each test and
re-imports the gated modules under no flags so neighbouring test files
start from a clean registry. The ``isolated_backend`` fixture roots the
Mission state backend under ``tmp_path`` so persisted sessions never leak
between cases.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

# Ensure mcp/ is importable, mirroring every other test module.
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))

import mission.state as mission_state  # noqa: E402
import run_mcp  # noqa: E402
from mission.state import FilesystemBackend  # noqa: E402

# Canonical roster of the nine session-management tools.
_MISSION_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "mission_start",
        "mission_status",
        "mission_iterate",
        "mission_checkpoint",
        "mission_complete",
        "mission_abort",
        "mission_resume",
        "mission_history",
        "mission_list",
    }
)

_MISSION_RESOURCE_TEMPLATES: tuple[str, ...] = (
    "mission://sessions/{session_id}",
    "mission://sessions/{session_id}/report",
    "mission://sessions/{session_id}/audit-replay",
)


# ---------------------------------------------------------------------------
# Registry / reload plumbing (mirrors tests/test_mission_mcp_tools.py)
# ---------------------------------------------------------------------------


async def _list_tool_names_async() -> set[str]:
    """Snapshot every registered tool name from inside a running event loop."""
    tools = await run_mcp.mcp._list_tools()
    return {t.name for t in tools}


def _force_unregister_mission_tools() -> None:
    """Strip every ``mission_*`` tool and resource template from the singleton."""
    for name in _MISSION_TOOL_NAMES:
        with contextlib.suppress(Exception):
            run_mcp.mcp.local_provider.remove_tool(name)
    for uri in _MISSION_RESOURCE_TEMPLATES:
        with contextlib.suppress(Exception):
            run_mcp.mcp.local_provider.remove_template(uri)


def _drop_mission_module_caches() -> None:
    """Drop ``tools.mission`` / ``resources.mission`` from every import cache."""
    for parent_name, child in (("tools", "mission"), ("resources", "mission")):
        sys.modules.pop(f"{parent_name}.{child}", None)
        parent = sys.modules.get(parent_name)
        if parent is not None and hasattr(parent, child):
            delattr(parent, child)


def _reload_run_mcp_fresh() -> None:
    """Reload ``run_mcp`` after dropping caches so gated bodies re-evaluate."""
    _drop_mission_module_caches()
    importlib.reload(run_mcp)


def _restore_mission_modules_unregistered() -> None:
    """Re-import the gated modules under no flags so registrations are no-ops."""
    flag_env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("GCO_ENABLE_MISSION", "GCO_ENABLE_ALL_TOOLS")
    }
    _drop_mission_module_caches()
    with patch.dict(os.environ, flag_env, clear=True):
        import resources  # noqa: F401
        import tools  # noqa: F401

        importlib.import_module("tools.mission")
        importlib.import_module("resources.mission")
    _force_unregister_mission_tools()


@pytest.fixture(autouse=True)
def _isolate_mission_tools():
    """Reset the live mcp singleton before and after every test."""
    _force_unregister_mission_tools()
    yield
    _force_unregister_mission_tools()
    _restore_mission_modules_unregistered()


@pytest.fixture
def isolated_backend(tmp_path):
    """Root the Mission state backend at a per-test tmp filesystem instance."""
    previous = mission_state._BACKEND_INSTANCE
    mission_state._BACKEND_INSTANCE = FilesystemBackend(root=tmp_path / "missions")
    yield mission_state._BACKEND_INSTANCE
    mission_state._BACKEND_INSTANCE = previous


# ---------------------------------------------------------------------------
# Controlled-registry helpers
# ---------------------------------------------------------------------------


class _FakeTool:
    """Minimal stand-in for a FastMCP Tool: a name, a tag set, a description."""

    def __init__(self, name: str, tags: set[str]) -> None:
        self.name = name
        self.tags = set(tags)
        self.description = f"{name} description."


@contextlib.contextmanager
def _patched_registry(tools_by_tag: dict[str, set[str]]):
    """Patch the handler's registry helpers with a deterministic in-memory set.

    ``tools_by_tag`` maps each tool name to its tag set. Control tools are
    those carrying the ``"mission"`` tag, exactly as the live handler
    derives them. Both async helpers are replaced for the duration of the
    block so the resolver sees only this registry.
    """
    mission_mod = importlib.import_module("tools.mission")
    tools_dict = {name: _FakeTool(name, tags) for name, tags in tools_by_tag.items()}
    tags_dict = {name: set(tags) for name, tags in tools_by_tag.items()}
    with (
        patch.object(mission_mod, "_registered_tools_dict", AsyncMock(return_value=tools_dict)),
        patch.object(mission_mod, "_registered_tool_tags", AsyncMock(return_value=tags_dict)),
    ):
        yield


def _expected_resolution(tools_by_tag: dict[str, set[str]]) -> list[str]:
    """Return the sorted non-control names for a controlled registry."""
    control = {name for name, tags in tools_by_tag.items() if "mission" in tags}
    return sorted(set(tools_by_tag) - control)


def _base_start_kwargs() -> dict[str, Any]:
    """Return a minimal valid ``mission_start`` payload without an allowlist.

    The allowlist is intentionally omitted so each test supplies whatever
    combination of ``allow_all_tools`` / ``tool_allowlist`` it exercises.
    The criteria and budget are independent of the allowlist path.
    """
    return {
        "directive": "Drive validation loss below 0.1.",
        "criteria": [
            {
                "criterion_id": "loss",
                "kind": "metric_threshold",
                "required": True,
                "metric": "val_loss",
                "op": "<",
                "target": 0.1,
            }
        ],
        "budget": {"max_iterations": 10, "max_wall_clock_seconds": 3600},
    }


def _payload(result: Any) -> dict[str, Any]:
    """Decode the JSON body FastMCP returns on the first content block."""
    return json.loads(result.content[0].text)  # type: ignore[no-any-return]


# A registry that carries two control tools, two read-only tools, and one
# non-``safe`` (destructive/infrastructure) tool. Used to prove the
# expansion spans every tier while still dropping the control tools.
_MIXED_REGISTRY: dict[str, set[str]] = {
    "mission_start": {"low-risk", "mission"},
    "mission_iterate": {"low-risk", "mission"},
    "list_jobs": {"safe"},
    "cost_summary": {"safe"},
    "deploy_stack": {"destructive", "infrastructure"},
}


class TestMissionStartAllowAllTools:
    """``allow_all_tools`` resolution behaviour on the ``mission_start`` tool."""

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_allow_all_persists_registered_minus_control(self, isolated_backend):
        """Setting the flag persists the sorted registered set minus control tools.

        The mixed registry includes control tools and a destructive,
        non-read-only tool; the persisted allowlist must list every
        non-control name regardless of tier and must omit the control
        tools entirely.
        """
        _reload_run_mcp_fresh()
        from fastmcp import Client

        expected = _expected_resolution(_MIXED_REGISTRY)
        with _patched_registry(_MIXED_REGISTRY):
            async with Client(run_mcp.mcp) as client:
                result = await client.call_tool(
                    "mission_start",
                    {**_base_start_kwargs(), "allow_all_tools": True},
                )

        payload = _payload(result)
        session_id = payload["session_id"]
        persisted = isolated_backend.load_session(session_id)["tool_allowlist"]

        assert persisted == expected
        assert persisted == ["cost_summary", "deploy_stack", "list_jobs"]
        # The destructive, non-read-only tool is in scope (tier-agnostic).
        assert "deploy_stack" in persisted
        # No control tool leaks into the resolved allowlist.
        assert not (set(persisted) & {"mission_start", "mission_iterate"})

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_allow_all_with_explicit_list_is_rejected(self, isolated_backend):
        """The flag combined with a non-empty explicit list is mutually exclusive.

        The tool returns the mutual-exclusivity envelope and persists no
        session.
        """
        _reload_run_mcp_fresh()
        from fastmcp import Client

        with _patched_registry(_MIXED_REGISTRY):
            async with Client(run_mcp.mcp) as client:
                result = await client.call_tool(
                    "mission_start",
                    {
                        **_base_start_kwargs(),
                        "allow_all_tools": True,
                        "tool_allowlist": ["list_jobs"],
                    },
                )

        payload = _payload(result)
        assert payload["code"] == "validation_error"
        assert payload["details"]["field"] == "tool_allowlist"
        assert payload["details"]["reason"] == "allow_all_and_explicit_allowlist_mutually_exclusive"
        # Nothing was persisted before the rejection.
        assert isolated_backend.list_sessions() == []

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_omitting_flag_uses_explicit_path_unchanged(self, isolated_backend):
        """Omitting the flag behaves as disabled and keeps the explicit list verbatim.

        With no ``allow_all_tools`` argument and an explicit single-tool
        list, the persisted allowlist is exactly that list, order
        preserved.
        """
        _reload_run_mcp_fresh()
        from fastmcp import Client

        with _patched_registry(_MIXED_REGISTRY):
            async with Client(run_mcp.mcp) as client:
                result = await client.call_tool(
                    "mission_start",
                    {**_base_start_kwargs(), "tool_allowlist": ["list_jobs"]},
                )

        payload = _payload(result)
        session_id = payload["session_id"]
        persisted = isolated_backend.load_session(session_id)["tool_allowlist"]
        assert persisted == ["list_jobs"]

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_empty_resolution_registry_is_rejected(self, isolated_backend):
        """A registry of only control tools resolves to nothing and is rejected.

        When the flag is set but the registered set minus the control
        tools is empty, the tool returns the empty-registry envelope and
        persists no session.
        """
        _reload_run_mcp_fresh()
        from fastmcp import Client

        control_only = {
            "mission_start": {"low-risk", "mission"},
            "mission_status": {"safe", "mission"},
        }
        with _patched_registry(control_only):
            async with Client(run_mcp.mcp) as client:
                result = await client.call_tool(
                    "mission_start",
                    {**_base_start_kwargs(), "allow_all_tools": True},
                )

        payload = _payload(result)
        assert payload["code"] == "validation_error"
        assert payload["details"]["field"] == "tool_allowlist"
        assert payload["details"]["reason"] == "allow_all_tools_empty_registry"
        assert isolated_backend.list_sessions() == []

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_resolution_is_snapshotted_once_at_start(self, isolated_backend):
        """The resolved list is frozen at start and ignores later registry changes.

        Resolve against one registry, persist, then swap in a larger
        registry. Reloading the session still returns the original
        resolution, proving a concrete list was stored rather than a
        live-evaluated sentinel.
        """
        _reload_run_mcp_fresh()
        from fastmcp import Client

        first_registry = {
            "mission_start": {"low-risk", "mission"},
            "alpha_tool": {"safe"},
            "beta_tool": {"low-risk"},
        }
        expected_first = _expected_resolution(first_registry)
        assert expected_first == ["alpha_tool", "beta_tool"]

        with _patched_registry(first_registry):
            async with Client(run_mcp.mcp) as client:
                start = await client.call_tool(
                    "mission_start",
                    {**_base_start_kwargs(), "allow_all_tools": True},
                )
        session_id = _payload(start)["session_id"]
        assert isolated_backend.load_session(session_id)["tool_allowlist"] == expected_first

        # Swap in a wider registry; a plain reload must not re-resolve.
        second_registry = {
            **first_registry,
            "gamma_tool": {"safe"},
            "delta_tool": {"destructive"},
        }
        with _patched_registry(second_registry):
            async with Client(run_mcp.mcp) as client:
                status = await client.call_tool("mission_status", {"session_id": session_id})

        reloaded = _payload(status)["tool_allowlist"]
        assert reloaded == expected_first
        assert isolated_backend.load_session(session_id)["tool_allowlist"] == expected_first

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_starting_session_does_not_change_registration(self, isolated_backend):
        """Starting an all-tools session leaves the registered tool set untouched.

        The per-session option resolves from whatever is registered; it
        never registers or unregisters tools. The live registry snapshot
        is identical before and after the call.
        """
        _reload_run_mcp_fresh()
        from fastmcp import Client

        before = await _list_tool_names_async()
        async with Client(run_mcp.mcp) as client:
            await client.call_tool(
                "mission_start",
                {**_base_start_kwargs(), "allow_all_tools": True},
            )
        after = await _list_tool_names_async()

        assert before == after, f"registration changed: {sorted(before ^ after)}"

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_more_registered_tools_widens_resolution(self, isolated_backend):
        """Registering more tools widens the resolved allowlist by exactly those tools.

        A narrow registry resolves to a small set; a superset registry
        resolves to a strictly larger set whose extra members are precisely
        the newly-registered non-control tools.
        """
        _reload_run_mcp_fresh()
        from fastmcp import Client

        narrow = {
            "mission_start": {"low-risk", "mission"},
            "alpha_tool": {"safe"},
        }
        wide = {
            **narrow,
            "omega_tool": {"safe"},
            "deploy_stack": {"destructive"},
        }

        with _patched_registry(narrow):
            async with Client(run_mcp.mcp) as client:
                narrow_start = await client.call_tool(
                    "mission_start",
                    {**_base_start_kwargs(), "allow_all_tools": True},
                )
        narrow_id = _payload(narrow_start)["session_id"]
        narrow_list = isolated_backend.load_session(narrow_id)["tool_allowlist"]

        with _patched_registry(wide):
            async with Client(run_mcp.mcp) as client:
                wide_start = await client.call_tool(
                    "mission_start",
                    {**_base_start_kwargs(), "allow_all_tools": True},
                )
        wide_id = _payload(wide_start)["session_id"]
        wide_list = isolated_backend.load_session(wide_id)["tool_allowlist"]

        assert set(narrow_list) == {"alpha_tool"}
        assert set(wide_list) > set(narrow_list)
        assert set(wide_list) - set(narrow_list) == {"omega_tool", "deploy_stack"}
