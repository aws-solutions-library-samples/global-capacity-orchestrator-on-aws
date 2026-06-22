"""Tool gating tests for the Mission tool surface.

Verifies that the nine ``mission_*`` tools are registered against the
FastMCP server only when ``GCO_ENABLE_MISSION`` (or the umbrella flag
``GCO_ENABLE_ALL_TOOLS``) is set. Mirrors the precedent established by
``test_mcp_destructive_gating.py``: snapshot the registry via the
async ``mcp._list_tools()`` and assert the expected names appear or are
absent.

Test isolation
==============

The FastMCP ``mcp`` instance is module-level in ``gco_mcp/server.py`` and
survives ``importlib.reload(run_mcp)``. Once a flag-set test registers
the nine ``mission_*`` tools, those registrations persist on the live
singleton. To keep tests independent of execution order:

* Before each test, ``_force_unregister_mission_tools()`` strips every
  ``mission_*`` name from the registry via FastMCP's
  ``local_provider.remove_tool`` API — the same hook
  ``test_mcp_destructive_gating.py`` uses for its gated-tool family.
* Before reloading ``run_mcp``, ``tools.mission`` is popped from
  ``sys.modules`` AND deleted as an attribute on the ``tools``
  package. Without that pop, ``register_all_tools()`` resolves the
  cached module reference and the gated ``if is_enabled(...)`` block
  at module top-level never re-evaluates — flipping the env var
  alone would not re-register the tools. Without the ``delattr``,
  the ``from tools import mission`` statement inside
  ``register_all_tools`` rebinds the cached attribute on the
  ``tools`` package and the module body is never re-executed.
* After each test, the same cleanup runs so the next test starts on a
  blank slate.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure gco_mcp/ is importable, mirroring every other test module.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

import run_mcp  # noqa: E402

# Canonical roster of the nine tools surfaced by ``gco_mcp/tools/mission.py``.
# Frozen so accidental in-test mutation is impossible.
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


def _list_tool_names() -> set[str]:
    """Snapshot every registered tool name from the live mcp instance."""
    tools = asyncio.run(run_mcp.mcp._list_tools())
    return {t.name for t in tools}


def _force_unregister_mission_tools() -> None:
    """Strip every ``mission_*`` tool from the live singleton.

    ``remove_tool`` raises when the name isn't registered — that is
    fine, we want the post-state regardless. The
    ``contextlib.suppress`` is the same idiom used by
    ``test_mcp_destructive_gating.py`` and ``test_mcp_images.py``.
    Resource templates registered by ``gco_mcp/resources/mission.py`` are
    cleared too so neighbouring tests (notably
    ``test_mcp_server.py::test_resource_template_count``) don't see
    leaked entries when this file runs ahead of them.
    """
    for name in _MISSION_TOOL_NAMES:
        with contextlib.suppress(Exception):
            run_mcp.mcp.local_provider.remove_tool(name)
    for uri in _MISSION_RESOURCE_TEMPLATES:
        with contextlib.suppress(Exception):
            run_mcp.mcp.local_provider.remove_template(uri)


def _reload_run_mcp_fresh() -> None:
    """Reload ``run_mcp`` after dropping every cache pointing at ``tools.mission``.

    ``register_all_tools()`` does ``from tools import mission`` — Python
    caches the imported module both in ``sys.modules`` and as an
    attribute on the ``tools`` package. ``importlib.reload(run_mcp)``
    re-runs ``register_all_tools()`` but the ``from`` statement
    short-circuits when the attribute already exists, so the gated
    ``if is_enabled(FLAG_MISSION):`` block at the top of
    ``gco_mcp/tools/mission.py`` never re-evaluates against the new env.

    Drop both caches before the reload so the ``from`` statement
    triggers a fresh module body execution under the patched env.
    """
    _drop_mission_module_caches()
    importlib.reload(run_mcp)


def _drop_mission_module_caches() -> None:
    """Drop ``tools.mission`` and ``resources.mission`` from every cache.

    Both ``register_all_tools`` and ``register_all_resources`` use
    ``from <pkg> import mission`` patterns; Python caches the imported
    module in ``sys.modules`` *and* sets it as an attribute on the
    parent package. The ``from`` statement is a no-op when the
    attribute already exists, so the gated module bodies never re-run
    after a flag flip unless we explicitly drop both caches.
    """
    for parent_name, child in (("tools", "mission"), ("resources", "mission")):
        sys.modules.pop(f"{parent_name}.{child}", None)
        parent = sys.modules.get(parent_name)
        if parent is not None and hasattr(parent, child):
            delattr(parent, child)


def _restore_mission_modules_unregistered() -> None:
    """Re-import ``tools.mission`` / ``resources.mission`` under no flags set.

    Forces both module bodies to execute under an environment where
    neither ``GCO_ENABLE_MISSION`` nor ``GCO_ENABLE_ALL_TOOLS`` is
    set, so their gated registrations are no-ops. The cached module
    objects then record "nothing registered", and any subsequent
    ``importlib.reload(run_mcp)`` in a sibling test file that flips
    only the umbrella will find them cached and skip re-execution —
    matching the assumption every other gating fixture in the suite
    makes.
    """
    flag_env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("GCO_ENABLE_MISSION", "GCO_ENABLE_ALL_TOOLS")
    }
    _drop_mission_module_caches()
    with patch.dict(os.environ, flag_env, clear=True):
        # Trigger ``from <pkg> import mission`` against the cleaned
        # env so the gated bodies see the flags off and register
        # nothing.
        import resources  # noqa: F401
        import tools  # noqa: F401

        importlib.import_module("tools.mission")
        importlib.import_module("resources.mission")
    # Also strip any leaked registrations one more time. ``importing``
    # is idempotent on the registry, but a sibling ``importlib.reload``
    # could have run the bodies once before this restore — better to
    # double-tap the registry strip.
    _force_unregister_mission_tools()


@pytest.fixture(autouse=True)
def _isolate_mission_tools():
    """Reset the live mcp singleton before and after every test.

    The teardown is more than a simple registry strip. The catch:
    other test modules — notably ``test_mcp_destructive_gating.py`` —
    set ``GCO_ENABLE_ALL_TOOLS=true`` and call
    ``importlib.reload(run_mcp)``. If we leave the mission modules
    popped from ``sys.modules`` at the end of a test, the umbrella
    reload re-imports them and the gated bodies fire under the
    umbrella flag, leaking nine tools and two resource templates into
    the live singleton with no fixture downstream that knows to clean
    them up.

    The fix: at teardown, force the mission modules to be re-imported
    against an environment where neither the per-tool flag nor the
    umbrella is set. That way their cached module bodies record
    "registered nothing", and any subsequent
    ``importlib.reload(run_mcp)`` in a sibling file finds them cached
    and skips re-execution — same invariant the rest of the suite
    relies on.
    """
    _force_unregister_mission_tools()
    yield
    _force_unregister_mission_tools()
    _restore_mission_modules_unregistered()


class TestMissionToolGating:
    """The nine mission_* tools follow the standard feature-flag gating contract."""

    def test_mission_tools_absent_when_flag_unset(self):
        """With both flags unset, none of the nine tools register."""
        # Drop GCO_ENABLE_MISSION and GCO_ENABLE_ALL_TOOLS from the
        # patched environment so an inherited shell value does not
        # mask the property under test. ``clear=True`` would also
        # nuke ``PATH`` and friends — instead, clone the current env
        # and pop just the two flags we care about.
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("GCO_ENABLE_MISSION", "GCO_ENABLE_ALL_TOOLS")
        }
        with patch.dict(os.environ, env, clear=True):
            _reload_run_mcp_fresh()
            names = _list_tool_names()

        leaked = names & _MISSION_TOOL_NAMES
        assert not leaked, f"unexpected mission tools registered without the flag: {sorted(leaked)}"

    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    def test_mission_tools_present_when_flag_set(self):
        """With ``GCO_ENABLE_MISSION=true``, all nine tools register."""
        _reload_run_mcp_fresh()
        names = _list_tool_names()

        missing = _MISSION_TOOL_NAMES - names
        assert not missing, (
            f"mission tools missing under GCO_ENABLE_MISSION=true: {sorted(missing)}"
        )

    @patch.dict(
        os.environ,
        {
            "GCO_ENABLE_ALL_TOOLS": "true",
            "GCO_ENABLE_MISSION": "false",
        },
    )
    def test_mission_tools_present_via_umbrella(self):
        """Umbrella flag wins even when the per-tool flag is explicitly false."""
        _reload_run_mcp_fresh()
        names = _list_tool_names()

        missing = _MISSION_TOOL_NAMES - names
        assert not missing, (
            f"mission tools missing under GCO_ENABLE_ALL_TOOLS=true (with "
            f"GCO_ENABLE_MISSION=false): {sorted(missing)}"
        )

    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    def test_tool_gating_determinism(self):
        """Two consecutive ``list_tools()`` calls yield identical sets.

        Same env, same registry — no nondeterministic ordering or
        membership drift between calls. Reload once, snapshot twice.
        """
        _reload_run_mcp_fresh()
        first = _list_tool_names()
        second = _list_tool_names()

        assert first == second, (
            "list_tools() returned different sets on repeat calls; "
            f"symmetric difference: {sorted(first ^ second)}"
        )


# =============================================================================
# Happy-path integration tests
# =============================================================================
#
# These tests round-trip every Mission tool through the FastMCP in-process
# Client so the wire shape (json string in ``result.content[0].text``) is
# exercised end-to-end. Each test uses an isolated filesystem backend rooted
# under ``tmp_path`` so sessions written by one test cannot leak into
# another, and each test sets ``GCO_ENABLE_MISSION=true`` so the gated
# tool registrations take effect.

import json  # noqa: E402

import mission.state as mission_state  # noqa: E402
from mission.state import FilesystemBackend  # noqa: E402


@pytest.fixture
def isolated_backend(tmp_path):
    """Reset the cached Mission state backend to a tmp-path filesystem instance.

    The :func:`mission_state.get_backend` helper caches the backend at
    module scope; without this fixture the first test that populated
    the cache would leak its on-disk state into every subsequent test.
    Patching ``_BACKEND_INSTANCE`` directly is the smallest possible
    surgery: the production resolver still constructs a
    :class:`FilesystemBackend` lazily on cache miss, which is exactly
    what we want, just rooted at ``tmp_path / "missions"`` for
    test-isolation.
    """
    previous = mission_state._BACKEND_INSTANCE
    mission_state._BACKEND_INSTANCE = FilesystemBackend(root=tmp_path / "missions")
    yield mission_state._BACKEND_INSTANCE
    mission_state._BACKEND_INSTANCE = previous


def _start_kwargs() -> dict:
    """Return a minimal valid ``mission_start`` argument dict.

    Builds a fresh dict every call so the shared ``criteria`` list
    cannot pick up validator-attached private keys (``_parsed_ast``)
    across tests. ``find_examples`` is registered unconditionally and
    has no required args, so it's the cheapest live tool to put on
    the allowlist for an iteration smoke test.
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
        "tool_allowlist": ["find_examples"],
    }


class TestMissionToolIntegration:
    """Happy-path integration tests via the FastMCP in-process Client."""

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_mission_start_returns_session_id(self, isolated_backend):
        """``mission_start`` returns a fresh session_id and ``status="pending"``."""
        _reload_run_mcp_fresh()
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.call_tool("mission_start", _start_kwargs())

        payload = json.loads(result.content[0].text)
        assert "session_id" in payload
        assert payload["session_id"].startswith("mission-")
        assert payload["status"] == "pending"

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_mission_iterate_runs_one_iteration(self, isolated_backend):
        """``mission_iterate`` runs one iteration and returns a verdict record."""
        _reload_run_mcp_fresh()
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            start_result = await client.call_tool("mission_start", _start_kwargs())
            session_id = json.loads(start_result.content[0].text)["session_id"]

            iter_result = await client.call_tool(
                "mission_iterate",
                {"session_id": session_id, "max_iterations_this_call": 1},
            )

        payload = json.loads(iter_result.content[0].text)
        assert payload["session_id"] == session_id
        assert "iterations" in payload
        assert len(payload["iterations"]) == 1
        record = payload["iterations"][0]
        assert record["iteration_index"] == 0
        assert record["verdict"] in (
            "continue",
            "adjust",
            "complete",
            "terminate",
        )

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_mission_status_returns_full_json(self, isolated_backend):
        """``mission_status`` returns the persisted session shape."""
        _reload_run_mcp_fresh()
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            start_result = await client.call_tool("mission_start", _start_kwargs())
            session_id = json.loads(start_result.content[0].text)["session_id"]

            await client.call_tool(
                "mission_iterate",
                {"session_id": session_id, "max_iterations_this_call": 1},
            )

            status_result = await client.call_tool("mission_status", {"session_id": session_id})

        payload = json.loads(status_result.content[0].text)
        # Core identifying fields persist verbatim.
        assert payload["session_id"] == session_id
        assert payload["directive_text"] == _start_kwargs()["directive"]
        # Validator-normalised inputs round-trip.
        assert payload["tool_allowlist"] == ["find_examples"]
        assert payload["budget"]["max_iterations"] == 10
        # One iteration was run; the lifecycle moved off ``pending``.
        assert len(payload["iterations"]) == 1
        assert payload["status"] == "running"
        assert "created_at" in payload
        assert "started_at" in payload

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_mission_abort_terminates(self, isolated_backend):
        """``mission_abort(pause=False)`` transitions to ``status="terminated"``."""
        _reload_run_mcp_fresh()
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            start_result = await client.call_tool("mission_start", _start_kwargs())
            session_id = json.loads(start_result.content[0].text)["session_id"]

            abort_result = await client.call_tool(
                "mission_abort", {"session_id": session_id, "pause": False}
            )
            status_result = await client.call_tool("mission_status", {"session_id": session_id})

        abort_payload = json.loads(abort_result.content[0].text)
        assert abort_payload["session_id"] == session_id
        assert abort_payload["status"] == "terminated"

        status_payload = json.loads(status_result.content[0].text)
        assert status_payload["status"] == "terminated"
        assert status_payload["final_verdict"] == "terminate"
        assert "ended_at" in status_payload

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_mission_resume_from_paused(self, isolated_backend):
        """``mission_abort(pause=True)`` then ``mission_resume`` restores running."""
        _reload_run_mcp_fresh()
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            start_result = await client.call_tool("mission_start", _start_kwargs())
            session_id = json.loads(start_result.content[0].text)["session_id"]

            pause_result = await client.call_tool(
                "mission_abort", {"session_id": session_id, "pause": True}
            )
            resume_result = await client.call_tool("mission_resume", {"session_id": session_id})

        pause_payload = json.loads(pause_result.content[0].text)
        assert pause_payload["status"] == "paused"

        resume_payload = json.loads(resume_result.content[0].text)
        assert resume_payload["session_id"] == session_id
        assert resume_payload["status"] == "running"

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_mission_complete_forces_success(self, isolated_backend):
        """``mission_complete`` transitions a non-terminal session to ``completed``."""
        _reload_run_mcp_fresh()
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            start_result = await client.call_tool("mission_start", _start_kwargs())
            session_id = json.loads(start_result.content[0].text)["session_id"]

            complete_result = await client.call_tool("mission_complete", {"session_id": session_id})
            status_result = await client.call_tool("mission_status", {"session_id": session_id})

        complete_payload = json.loads(complete_result.content[0].text)
        assert complete_payload["session_id"] == session_id
        assert complete_payload["status"] == "completed"

        status_payload = json.loads(status_result.content[0].text)
        assert status_payload["status"] == "completed"
        assert status_payload["final_verdict"] == "complete"

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_mission_list_returns_sessions(self, isolated_backend):
        """``mission_list`` returns summaries for every persisted session."""
        _reload_run_mcp_fresh()
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            first_start = await client.call_tool("mission_start", _start_kwargs())
            second_start = await client.call_tool("mission_start", _start_kwargs())
            list_result = await client.call_tool("mission_list", {})

        first_id = json.loads(first_start.content[0].text)["session_id"]
        second_id = json.loads(second_start.content[0].text)["session_id"]

        payload = json.loads(list_result.content[0].text)
        assert "sessions" in payload
        ids = {s["session_id"] for s in payload["sessions"]}
        assert first_id in ids
        assert second_id in ids


# =============================================================================
# Error-envelope integration tests
# =============================================================================
#
# Each ``mission_*`` tool encodes failure modes as a JSON envelope of the
# shape ``{"code": <stable str>, "details": {...}}`` returned via the same
# ``content[0].text`` channel as the happy path. ``client.call_tool`` resolves
# successfully; the test parses the body and asserts the ``code`` field
# matches the stable identifier promised by the tool surface. The
# ``isolated_backend`` fixture and the ``_isolate_mission_tools`` autouse
# fixture defined above provide registry / on-disk isolation.


class TestMissionToolErrors:
    """Error-code tests for the ``mission_*`` tool surface."""

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_mission_iterate_on_nonexistent_session(self, isolated_backend):
        """``mission_iterate`` on an unknown id returns ``session_not_found``."""
        _reload_run_mcp_fresh()
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.call_tool(
                "mission_iterate", {"session_id": "mission-does-not-exist"}
            )

        payload = json.loads(result.content[0].text)
        assert payload["code"] == "session_not_found"
        assert payload["details"]["session_id"] == "mission-does-not-exist"

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_mission_iterate_on_terminal_session(self, isolated_backend):
        """Iterating a completed session returns ``session_terminal``."""
        _reload_run_mcp_fresh()
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            start_result = await client.call_tool("mission_start", _start_kwargs())
            session_id = json.loads(start_result.content[0].text)["session_id"]

            # Force the session into a terminal state via the dedicated
            # tool. ``mission_complete`` stamps a synthetic ``complete``
            # final verdict and moves the lifecycle into ``completed``,
            # which is one of the ``TERMINAL_STATES`` the engine refuses
            # to iterate on.
            await client.call_tool("mission_complete", {"session_id": session_id})

            iter_result = await client.call_tool("mission_iterate", {"session_id": session_id})

        payload = json.loads(iter_result.content[0].text)
        assert payload["code"] == "session_terminal"
        # The wrapper returns whatever summaries had accumulated before
        # the engine raised; for a brand-new completed session that
        # list is empty.
        assert payload["iterations"] == []

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_mission_iterate_on_paused_session(self, isolated_backend):
        """Iterating a paused session returns ``session_paused``."""
        _reload_run_mcp_fresh()
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            start_result = await client.call_tool("mission_start", _start_kwargs())
            session_id = json.loads(start_result.content[0].text)["session_id"]

            # ``mission_abort(pause=True)`` is the only public path
            # into the ``paused`` state.
            pause_result = await client.call_tool(
                "mission_abort", {"session_id": session_id, "pause": True}
            )
            assert json.loads(pause_result.content[0].text)["status"] == "paused"

            iter_result = await client.call_tool("mission_iterate", {"session_id": session_id})

        payload = json.loads(iter_result.content[0].text)
        assert payload["code"] == "session_paused"
        assert payload["iterations"] == []

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_mission_resume_on_non_paused(self, isolated_backend):
        """Resuming a non-paused session returns ``not_paused``.

        A freshly-started session is in ``pending``, which is not
        ``paused``. The tool returns ``{"code": "not_paused", ...}``
        verbatim — note the actual code is ``not_paused``, not
        ``invalid_state`` as a casual reader might guess.
        """
        _reload_run_mcp_fresh()
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            start_result = await client.call_tool("mission_start", _start_kwargs())
            session_id = json.loads(start_result.content[0].text)["session_id"]

            resume_result = await client.call_tool("mission_resume", {"session_id": session_id})

        payload = json.loads(resume_result.content[0].text)
        assert payload["code"] == "not_paused"
        assert payload["details"]["session_id"] == session_id
        assert payload["details"]["status"] == "pending"

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_mission_start_with_invalid_directive(self, isolated_backend):
        """``mission_start`` with an empty directive returns ``validation_error``."""
        _reload_run_mcp_fresh()
        from fastmcp import Client

        kwargs = _start_kwargs()
        kwargs["directive"] = ""  # empty after strip → directive validator rejects

        async with Client(run_mcp.mcp) as client:
            result = await client.call_tool("mission_start", kwargs)

        payload = json.loads(result.content[0].text)
        assert payload["code"] == "validation_error"
        # The validator's ``details.field`` identifies which input was
        # rejected; ``reason`` is one of the validator's stable strings.
        assert payload["details"]["field"] == "directive"
        assert payload["details"]["reason"] == "empty"


# =============================================================================
# Resource template tests — ``mission://sessions/{session_id}`` and ``.../report``
# =============================================================================
#
# The two Mission resource templates registered by ``gco_mcp/resources/mission.py``
# expose live session JSON and the durable Final_Report artifact. Both are
# gated by ``GCO_ENABLE_MISSION``; the template registrations are stripped
# by ``_force_unregister_mission_tools`` between tests so each case starts
# from a clean registry.
#
# Two transport behaviours need exercising:
#
# * Successful reads — the FastMCP in-process ``Client.read_resource`` API
#   returns a list of ``TextResourceContents`` blocks; the body is on
#   ``result[0].text``.
# * Not-found reads — the report handler raises ``NotFoundError`` for
#   pending sessions and missing reports. FastMCP maps that exception to
#   an MCP ``-32002 Resource not found`` JSONRPC error on the wire, which
#   ``Client.read_resource`` re-raises client-side as
#   :class:`mcp.shared.exceptions.McpError`. The catch-all ``Exception``
#   in ``pytest.raises`` is intentional — it keeps the test resilient to
#   FastMCP swapping the concrete exception class between minor releases
#   without losing the "this URI yields a not-found error" invariant.


class TestMissionResources:
    """Resource template tests for ``mission://sessions/{...}``.

    Each case round-trips a resource URI through FastMCP's in-process
    ``Client``, asserting either the JSON body shape (live session
    and terminal report) or the not-found behaviour (pending report).
    The class-scoped autouse ``_enable_mission`` fixture sets the
    gating env var and reloads ``run_mcp`` so the resource templates
    are registered against the live ``mcp`` instance — without it,
    the registrations would be no-ops.
    """

    @pytest.fixture(autouse=True)
    def _enable_mission(self, monkeypatch):
        """Set ``GCO_ENABLE_MISSION=true`` and reload so resources register."""
        monkeypatch.setenv("GCO_ENABLE_MISSION", "true")
        _reload_run_mcp_fresh()

    @pytest.mark.asyncio
    async def test_mission_session_resource_returns_json(self, isolated_backend):
        """Reading ``mission://sessions/<id>`` returns the live session JSON."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            start = await client.call_tool("mission_start", _start_kwargs())
            session_id = json.loads(start.content[0].text)["session_id"]

            result = await client.read_resource(f"mission://sessions/{session_id}")

        # ``Client.read_resource`` returns a list of
        # ``TextResourceContents`` blocks; the JSON body is on the
        # first block's ``text`` field.
        assert result, "expected at least one content block"
        text = result[0].text
        payload = json.loads(text)
        assert payload["session_id"] == session_id
        # ``directive_text`` is the canonical name for the operator-
        # supplied directive on the persisted session shape.
        assert payload["directive_text"] == _start_kwargs()["directive"]
        # The validator-normalised inputs round-trip through the
        # resource handler unchanged.
        assert payload["tool_allowlist"] == ["find_examples"]
        assert payload["status"] == "pending"

    @pytest.mark.asyncio
    async def test_mission_report_resource_404_before_terminal(self, isolated_backend):
        """A pending session's report URI raises a not-found error."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            start = await client.call_tool("mission_start", _start_kwargs())
            session_id = json.loads(start.content[0].text)["session_id"]

            # The report handler raises ``NotFoundError`` server-side
            # because the session is in ``pending``, which is not
            # one of ``TERMINAL_STATES``. FastMCP propagates that as
            # a JSONRPC -32002 error and ``Client.read_resource``
            # re-raises an ``McpError`` on this side. ``Exception``
            # is the broadest match that survives FastMCP swapping
            # the concrete class between minor releases.
            with pytest.raises(Exception) as exc_info:
                await client.read_resource(f"mission://sessions/{session_id}/report")

        # The handler stamps "not terminal" into the message so the
        # caller can distinguish "session unknown" from "session
        # exists but not terminal yet". Match case-insensitively
        # because the wire layer may prepend a wrapping prefix.
        assert "not terminal" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_mission_report_resource_returns_report_after_complete(self, isolated_backend):
        """A terminal session's report URI returns the Final_Report JSON.

        Drives the engine to a natural terminal verdict by setting
        ``max_iterations=1`` on the budget. The decide cascade in
        :func:`mcp.mission.decide.decide_verdict` evaluates
        ``len(iterations)+1 >= max_iterations`` first, so the very
        first iteration on a budget=1 session terminates with reason
        ``max_iterations`` and writes the Final_Report to the
        FilesystemBackend. The resource handler then reads that
        sibling ``<session_id>.report.json`` file back.
        """
        from fastmcp import Client

        kwargs = _start_kwargs()
        # Override only the iteration cap so the first iteration
        # terminates deterministically; keep the wall-clock cap and
        # criteria intact so the rest of the pipeline runs as it
        # would on a normally-budgeted session.
        kwargs["budget"] = {
            "max_iterations": 1,
            "max_wall_clock_seconds": 60,
        }

        async with Client(run_mcp.mcp) as client:
            start = await client.call_tool("mission_start", kwargs)
            session_id = json.loads(start.content[0].text)["session_id"]

            iter_result = await client.call_tool(
                "mission_iterate",
                {"session_id": session_id, "max_iterations_this_call": 1},
            )
            iter_payload = json.loads(iter_result.content[0].text)
            # Confirm we actually hit the terminal path before reading
            # the report — otherwise a regression in the decide
            # cascade would be diagnosed as a resource-handler bug.
            assert iter_payload["iterations"], "expected at least one iteration"
            first = iter_payload["iterations"][0]
            assert first["verdict"] == "terminate"
            assert first["verdict_reason"] == "max_iterations"

            result = await client.read_resource(f"mission://sessions/{session_id}/report")

        assert result, "expected at least one content block"
        text = result[0].text
        report = json.loads(text)
        # The Final_Report carries the session id verbatim and the
        # same terminal verdict tuple the iteration recorded.
        assert report["session_id"] == session_id
        assert report["final_verdict"] == "terminate"
        assert report["final_verdict_reason"] == "max_iterations"
        # The report mirrors the iteration history; with one
        # iteration run, ``iterations_run`` is 1.
        assert "iterations" in report


# ---------------------------------------------------------------------------
# Coverage backfill — error envelopes and unwrap branches
# ---------------------------------------------------------------------------


class TestMissionToolErrorEnvelopes:
    """Cover the negative-path error envelopes that aren't pinned elsewhere."""

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_mission_status_unknown_session(self, isolated_backend):
        """``mission_status`` against an unknown id returns ``session_not_found``."""
        _reload_run_mcp_fresh()
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.call_tool("mission_status", {"session_id": "no-such"})
        envelope = json.loads(result.content[0].text)
        assert envelope["code"] == "session_not_found"

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_mission_iterate_invalid_max_iterations(self, isolated_backend):
        """``mission_iterate`` with ``max_iterations_this_call=0`` errors."""
        _reload_run_mcp_fresh()
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.call_tool(
                "mission_iterate",
                {"session_id": "any", "max_iterations_this_call": 0},
            )
        envelope = json.loads(result.content[0].text)
        assert envelope["code"] == "invalid_argument"

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_mission_checkpoint_unknown_session(self, isolated_backend):
        """``mission_checkpoint`` on an unknown id returns ``session_not_found``."""
        _reload_run_mcp_fresh()
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.call_tool("mission_checkpoint", {"session_id": "no-such"})
        envelope = json.loads(result.content[0].text)
        assert envelope["code"] == "session_not_found"

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_mission_checkpoint_no_iterations(self, isolated_backend):
        """``mission_checkpoint`` on a fresh session returns ``no_iterations``."""
        _reload_run_mcp_fresh()
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            start = await client.call_tool("mission_start", _start_kwargs())
            sid = json.loads(start.content[0].text)["session_id"]
            result = await client.call_tool("mission_checkpoint", {"session_id": sid})
        envelope = json.loads(result.content[0].text)
        assert envelope["code"] == "no_iterations"

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_mission_complete_unknown_session(self, isolated_backend):
        """``mission_complete`` on an unknown id returns ``session_not_found``."""
        _reload_run_mcp_fresh()
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.call_tool("mission_complete", {"session_id": "no-such"})
        envelope = json.loads(result.content[0].text)
        assert envelope["code"] == "session_not_found"

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_mission_complete_terminal_session(self, isolated_backend):
        """``mission_complete`` on a terminal session returns ``session_terminal``."""
        _reload_run_mcp_fresh()
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            start = await client.call_tool("mission_start", _start_kwargs())
            sid = json.loads(start.content[0].text)["session_id"]
            await client.call_tool("mission_complete", {"session_id": sid})
            result = await client.call_tool("mission_complete", {"session_id": sid})
        envelope = json.loads(result.content[0].text)
        assert envelope["code"] == "session_terminal"

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_mission_abort_unknown_session(self, isolated_backend):
        """``mission_abort`` on an unknown id returns ``session_not_found``."""
        _reload_run_mcp_fresh()
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.call_tool("mission_abort", {"session_id": "no-such"})
        envelope = json.loads(result.content[0].text)
        assert envelope["code"] == "session_not_found"

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_mission_abort_terminal_session(self, isolated_backend):
        """``mission_abort`` on a terminal session returns ``session_terminal``."""
        _reload_run_mcp_fresh()
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            start = await client.call_tool("mission_start", _start_kwargs())
            sid = json.loads(start.content[0].text)["session_id"]
            await client.call_tool("mission_complete", {"session_id": sid})
            result = await client.call_tool("mission_abort", {"session_id": sid})
        envelope = json.loads(result.content[0].text)
        assert envelope["code"] == "session_terminal"

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_mission_resume_unknown_session(self, isolated_backend):
        """``mission_resume`` on an unknown id returns ``session_not_found``."""
        _reload_run_mcp_fresh()
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.call_tool("mission_resume", {"session_id": "no-such"})
        envelope = json.loads(result.content[0].text)
        assert envelope["code"] == "session_not_found"

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_mission_history_unknown_session(self, isolated_backend):
        """``mission_history`` on an unknown id returns ``session_not_found``."""
        _reload_run_mcp_fresh()
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.call_tool("mission_history", {"session_id": "no-such"})
        envelope = json.loads(result.content[0].text)
        assert envelope["code"] == "session_not_found"

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MISSION": "true"})
    async def test_mission_history_full_format(self, isolated_backend):
        """``mission_history(format='full')`` returns full iteration records."""
        _reload_run_mcp_fresh()
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            start = await client.call_tool("mission_start", _start_kwargs())
            sid = json.loads(start.content[0].text)["session_id"]
            await client.call_tool(
                "mission_iterate", {"session_id": sid, "max_iterations_this_call": 1}
            )
            result = await client.call_tool(
                "mission_history", {"session_id": sid, "format": "full"}
            )
        payload = json.loads(result.content[0].text)
        assert "iterations" in payload
        assert len(payload["iterations"]) == 1
        # full format includes the strategy and observation fields
        assert "strategy" in payload["iterations"][0]


# ---------------------------------------------------------------------------
# Resource handler edge cases
# ---------------------------------------------------------------------------
#
# These hit the fallback paths in ``gco_mcp/resources/mission.py``:
#
# * ``_session_resource`` returning the ``session_not_found`` envelope when
#   the backend has no record of the requested id (line block 74-82).
# * ``_session_report_resource`` taking the non-filesystem-backend branch
#   that reads the embedded ``final_report`` field on the session
#   (line block 131-141).
#
# Both are exercised through direct calls to the handler functions
# rather than through the FastMCP ``read_resource`` transport so the
# test does not depend on FastMCP's error mapping behaviour.


class TestMissionResourceFallbacks:
    """Coverage for the resource-handler fallback paths."""

    def test_session_resource_returns_envelope_for_unknown_id(
        self,
        isolated_backend,  # noqa: ARG002
    ):
        """``_session_resource`` returns a JSON error envelope for unknown ids.

        The handler is intentionally non-raising so the synthetic
        ``read_resource`` tool from the Resources As Tools transform
        gets a string body it can return — raising would surface as an
        MCP error instead of a typed envelope. Calling the handler
        function directly bypasses the FastMCP transport.
        """
        from resources.mission import _session_resource

        body = _session_resource("mission-does-not-exist")
        envelope = json.loads(body)
        assert envelope["error"] == "session_not_found"
        assert envelope["session_id"] == "mission-does-not-exist"

    def test_report_resource_reads_embedded_final_report(
        self,
        isolated_backend,
        tmp_path,
        monkeypatch,  # noqa: ARG002
    ):
        """``_session_report_resource`` reads ``session["final_report"]`` for non-filesystem backends.

        Builds a fake non-filesystem backend that returns a session
        carrying an embedded ``final_report`` field (the contract for
        backends other than :class:`FilesystemBackend`). The handler
        should serialise the embedded payload rather than reading from
        disk.
        """
        from resources.mission import _session_report_resource

        # A minimal non-filesystem backend: returns whatever session
        # we hand it, advertises itself as not-FilesystemBackend.
        class _StubBackend:
            def load_session(self, session_id):  # noqa: ARG002
                return {
                    "session_id": "mission-stub",
                    "status": "completed",
                    "final_report": {
                        "session_id": "mission-stub",
                        "final_verdict": "complete",
                        "iterations_run": 3,
                    },
                }

        # Patch ``get_backend`` so the handler picks up the stub.
        from mission import state as mission_state

        monkeypatch.setattr(mission_state, "_BACKEND_INSTANCE", _StubBackend())

        body = _session_report_resource("mission-stub")
        report = json.loads(body)
        assert report["session_id"] == "mission-stub"
        assert report["final_verdict"] == "complete"
        assert report["iterations_run"] == 3

    def test_report_resource_raises_when_embedded_report_missing(
        self,
        isolated_backend,
        monkeypatch,  # noqa: ARG002
    ):
        """The handler raises not-found when the terminal session has no embedded report.

        Hits the ``report is None`` branch — a non-filesystem backend
        whose terminal session has no ``final_report`` key. The
        handler raises rather than silently returning an empty body.
        """
        from resources.mission import _session_report_resource

        class _BareTerminalBackend:
            def load_session(self, session_id):  # noqa: ARG002
                return {
                    "session_id": "mission-bare",
                    "status": "completed",
                    # No `final_report` key.
                }

        from mission import state as mission_state

        monkeypatch.setattr(mission_state, "_BACKEND_INSTANCE", _BareTerminalBackend())

        with pytest.raises(Exception) as exc_info:
            _session_report_resource("mission-bare")
        # The raised exception's string mentions the not-found shape.
        assert "report not found" in str(exc_info.value).lower()
