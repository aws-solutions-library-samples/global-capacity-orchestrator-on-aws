"""Live MCP integration tests for the Mission tool surface.

Boots the FastMCP server in-process via ``Client(run_mcp.mcp)`` and
drives the full nine-tool ``mission_*`` lifecycle through the actual
JSON-RPC protocol layer — tool discovery, ``mission_start``,
``mission_iterate`` (with the live FastMCP tool registry as the
dispatcher target), ``mission_status``, ``mission_history``,
``mission_checkpoint``, ``mission_abort --pause``, ``mission_resume``,
``mission_complete``, ``mission_list``, plus the
``mission://sessions/{id}`` and ``mission://sessions/{id}/audit-replay``
resource templates.

Why this layer matters
======================

The other ``test_mission_e2e_*.py`` files drive
``MissionEngine.run_iteration`` directly with a stub dispatcher; they
catch engine-internal regressions but do not exercise the FastMCP
JSON-RPC surface, the schema validation that wraps every tool, the
audit middleware that the engine traverses, or the
``ResourcesAsTools`` round-trip that the audit-replay resource gets
served through. This file pins those connections.

Test isolation
==============

Three reload-isolation hooks make the suite stable regardless of
execution order:

* ``GCO_ENABLE_MISSION=true`` is set per-class via ``@patch.dict``,
  followed by ``importlib.reload(run_mcp)`` so the gated
  ``mission_*`` registrations actually run. The destructive-gating
  test file uses the same pattern.
* ``_force_unregister_mission_tools`` strips every ``mission_*`` name
  off the live FastMCP singleton between tests so a flag-set test in
  one file doesn't leak registrations into a flag-unset test in
  another.
* The Mission state backend is pointed at a per-test ``tmp_path`` via
  ``monkeypatch.setattr(mission_state, "_BACKEND_INSTANCE", ...)``
  so sessions created here never touch ``~/.gco/missions/``.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

# The Mission package lives under ``mcp/mission`` and is imported as
# ``mission.*``. Mirror the path-injection pattern used throughout the
# rest of the ``test_mission_*`` files so the imports below resolve
# regardless of how pytest is invoked.
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))

import run_mcp  # noqa: E402

PROJECT_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_MISSION_TOOL_NAMES = (
    "mission_start",
    "mission_status",
    "mission_iterate",
    "mission_checkpoint",
    "mission_complete",
    "mission_abort",
    "mission_resume",
    "mission_history",
    "mission_list",
)


def _force_unregister_mission_tools() -> None:
    """Strip every ``mission_*`` registration off the live FastMCP singleton.

    The FastMCP ``mcp`` instance is module-level in ``mcp/server.py``
    and survives ``importlib.reload(run_mcp)``. Once a flag-set test
    registers the nine ``mission_*`` tools, those registrations
    persist on the live singleton — which would leak into flag-unset
    tests in other files. Clear them before each test in this file so
    we always know whether the post-reload snapshot reflects the test's
    own flag state, not leaked state from a sibling.
    """
    for name in _MISSION_TOOL_NAMES:
        with contextlib.suppress(Exception):
            # ``remove_tool`` raises when the name isn't registered —
            # fine, the post-state is what we wanted regardless.
            run_mcp.mcp.local_provider.remove_tool(name)


def _reload_with_mission_flag(flag_value: bool) -> None:
    """Reload run_mcp so the mission-tools gating re-evaluates.

    The gate ``if is_enabled(FLAG_MISSION):`` in ``mcp/tools/mission.py``
    fires at module import time, not on every reload of the parent.
    Once ``tools.mission`` is in ``sys.modules`` (which it will be
    after the first call to ``register_all_tools``), a plain
    ``importlib.reload(run_mcp)`` re-runs the parent's body but does
    NOT re-execute ``tools/mission.py``'s gate — so a flag flip
    appears not to take effect.

    The reliable pattern (the same one ``test_mission_mcp_tools.py``
    uses) is:

    1. Strip ``tools.mission`` from ``sys.modules``.
    2. Strip the ``mission`` attribute from the ``tools`` package
       (because ``register_all_tools`` does ``from tools import
       (... mission ...)`` and a cached attribute on the package
       short-circuits the re-import).
    3. Force-unregister any leaked ``mission_*`` decorators off the
       FastMCP singleton.
    4. Reload ``run_mcp`` — which re-imports ``tools.mission``,
       which re-evaluates the gate against the current env.
    """
    if flag_value:
        os.environ["GCO_ENABLE_MISSION"] = "true"
    else:
        with contextlib.suppress(KeyError):
            del os.environ["GCO_ENABLE_MISSION"]
    with contextlib.suppress(KeyError):
        del os.environ["GCO_ENABLE_ALL_TOOLS"]

    # Drop the cached tools.mission so the gate re-runs.
    sys.modules.pop("tools.mission", None)
    import tools as _tools_pkg

    if hasattr(_tools_pkg, "mission"):
        delattr(_tools_pkg, "mission")

    _force_unregister_mission_tools()
    importlib.reload(run_mcp)


def _isolate_mission_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Point the Mission state backend at a per-test ``tmp_path``."""
    from mission import state as mission_state
    from mission.state import FilesystemBackend

    backend = FilesystemBackend(root=tmp_path)
    monkeypatch.setattr(mission_state, "_BACKEND_INSTANCE", backend)


@pytest.fixture(autouse=True)
def _strip_mission_registrations() -> Any:
    """Clear leaked ``mission_*`` registrations before and after every test."""
    _force_unregister_mission_tools()
    yield
    _force_unregister_mission_tools()


# ---------------------------------------------------------------------------
# Suite
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMissionMcpToolDiscovery:
    """``mission_*`` tools register only when GCO_ENABLE_MISSION is set."""

    async def test_mission_tools_absent_by_default(self) -> None:
        """With no flag set, no ``mission_*`` tools register."""
        _reload_with_mission_flag(False)

        tools = await run_mcp.mcp._list_tools()
        names = {t.name for t in tools}
        for name in _MISSION_TOOL_NAMES:
            assert name not in names, f"{name!r} leaked into the default registry"

    async def test_all_nine_mission_tools_register_when_flag_set(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Every ``mission_*`` tool appears once GCO_ENABLE_MISSION=true."""
        _reload_with_mission_flag(True)

        tools = await run_mcp.mcp._list_tools()
        names = {t.name for t in tools}
        for name in _MISSION_TOOL_NAMES:
            assert name in names, f"{name!r} missing under GCO_ENABLE_MISSION=true"


@pytest.mark.asyncio
class TestMissionMcpFullLifecycle:
    """End-to-end Mission lifecycle through the FastMCP ``Client``.

    Drives every Mission tool over the actual JSON-RPC layer so a
    regression in the FastMCP wrapper's argument schema, the audit
    middleware, or the resource registration breaks here even if the
    underlying engine still works in unit tests.
    """

    async def test_full_lifecycle_through_fastmcp_client(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """``start`` → ``iterate`` → ``status`` → ``history`` → ``checkpoint``
        → ``abort --pause`` → ``resume`` → ``complete`` → ``list`` → resources.

        Each step calls the live ``Client(run_mcp.mcp)`` over the
        in-process protocol layer. The dispatcher target is the live
        FastMCP tool registry — Mission's ``_dispatch_tool`` resolves
        ``find_examples`` against ``mcp.get_tool`` and runs the real
        tool, so the Observation carries the actual structured result
        the operator would see in production.
        """
        from fastmcp import Client

        _reload_with_mission_flag(True)

        # Other tests in this process may have left the audit-collector
        # singleton in a torn-down state — ``test_mission_coverage.py
        # ::TestAuditCollector::test_install_collector_floors_logger_at_info``
        # uses ``monkeypatch.setattr(mission_audit, "_COLLECTOR", None)``
        # which restores the attribute on teardown, but the handler
        # additions / removals to ``audit_logger.handlers`` are not
        # tracked by monkeypatch and can leave the singleton pointing
        # at a Handler instance that's no longer attached to the
        # logger. Force a clean install: drop the cached singleton,
        # remove every leftover handler instance, then re-install.
        from mission import audit as mission_audit

        mission_audit._COLLECTOR = None
        for handler in list(mission_audit.audit_logger.handlers):
            if isinstance(handler, mission_audit.MissionAuditCollectorHandler):
                mission_audit.audit_logger.removeHandler(handler)
        collector = mission_audit.install_collector()
        collector.clear()

        _isolate_mission_backend(monkeypatch, tmp_path)

        # Predicate criterion that completes the moment the dispatcher
        # records any tool_results — the live ``find_examples`` tool
        # always emits at least one result for the empty-query case,
        # so iteration 0 should fire ``met``.
        criteria = [
            {
                "criterion_id": "got_results",
                "kind": "predicate",
                "required": True,
                "expression": "len(obs['tool_results']) >= 1",
            }
        ]

        async with Client(run_mcp.mcp) as client:
            # ---- start ----
            start_raw = await client.call_tool(
                "mission_start",
                {
                    "directive": "Live MCP integration smoke test",
                    "criteria": criteria,
                    "tool_allowlist": ["find_examples"],
                    "budget": {
                        "max_iterations": 5,
                        "max_wall_clock_seconds": 60,
                    },
                    "stagnation_threshold": 100,
                    "use_sampling": False,
                },
            )
            start_payload = _unwrap_text_json(start_raw)
            assert "session_id" in start_payload
            sid: str = start_payload["session_id"]
            assert start_payload["status"] == "pending"

            # ---- iterate (one iteration; should complete) ----
            iter_raw = await client.call_tool(
                "mission_iterate",
                {"session_id": sid, "max_iterations_this_call": 1},
            )
            iter_payload = _unwrap_text_json(iter_raw)
            iters = iter_payload.get("iterations") or []
            assert len(iters) == 1
            assert iters[0]["verdict"] in ("complete", "continue", "adjust", "terminate")
            # We expect ``complete/criteria_met`` because the live
            # ``find_examples`` tool emits at least one result.
            assert iters[0]["verdict"] == "complete"
            assert iters[0]["verdict_reason"] == "criteria_met"

            # ---- status ----
            status_raw = await client.call_tool("mission_status", {"session_id": sid})
            status_payload = _unwrap_text_json(status_raw)
            assert status_payload["status"] == "completed"
            assert status_payload["session_id"] == sid

            # ---- history (summary mode) ----
            hist_raw = await client.call_tool(
                "mission_history",
                {"session_id": sid, "format": "summary"},
            )
            hist_payload = _unwrap_text_json(hist_raw)
            hist_iters = hist_payload.get("iterations") or []
            assert len(hist_iters) == 1
            assert hist_iters[0]["iteration_index"] == 0

            # ---- checkpoint (re-evaluates the latest iteration) ----
            chk_raw = await client.call_tool("mission_checkpoint", {"session_id": sid})
            chk_payload = _unwrap_text_json(chk_raw)
            assert chk_payload["iteration_index"] == 0
            assert chk_payload["verdict"] == "complete"

            # ---- list ----
            list_raw = await client.call_tool("mission_list", {})
            list_payload = _unwrap_text_json(list_raw)
            ids = {s["session_id"] for s in (list_payload.get("sessions") or [])}
            assert sid in ids

            # ---- resources: mission://sessions/{id} ----
            resource_uri = f"mission://sessions/{sid}"
            resource_payload = await client.read_resource(resource_uri)
            assert resource_payload, f"resource {resource_uri!r} returned no content"
            text = resource_payload[0].text or ""
            persisted = json.loads(text)
            assert persisted["session_id"] == sid
            assert persisted["status"] == "completed"

            # ---- resources: mission://sessions/{id}/audit-replay ----
            replay_uri = f"mission://sessions/{sid}/audit-replay"
            replay_payload = await client.read_resource(replay_uri)
            assert replay_payload, f"resource {replay_uri!r} returned no content"
            replay_text = replay_payload[0].text or ""
            replay = json.loads(replay_text)
            assert replay["session_id"] == sid
            # Iteration 0 was driven through this client so the
            # in-process audit collector ring buffer has its phase
            # and verdict entries — replay must reconstruct one
            # iteration with verdict=complete.
            assert len(replay["iterations"]) == 1
            assert replay["iterations"][0]["verdict"] == "complete"
            assert replay["iterations"][0]["verdict_reason"] == "criteria_met"

    async def test_pause_resume_complete_through_client(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Round-trip the pause/resume/complete state machine via the protocol.

        Distinct test rather than a continuation of the lifecycle
        case so a regression that breaks ``mission_resume`` or
        ``mission_complete`` doesn't mask itself behind an earlier
        completed verdict.
        """
        from fastmcp import Client

        _reload_with_mission_flag(True)

        _isolate_mission_backend(monkeypatch, tmp_path)

        # Unreachable predicate so iteration 0 stays ``continue``.
        criteria = [
            {
                "criterion_id": "never_met",
                "kind": "predicate",
                "required": True,
                "expression": "False",
            }
        ]
        async with Client(run_mcp.mcp) as client:
            start_payload = _unwrap_text_json(
                await client.call_tool(
                    "mission_start",
                    {
                        "directive": "Pause/resume/complete round trip",
                        "criteria": criteria,
                        "tool_allowlist": ["find_examples"],
                        "budget": {
                            "max_iterations": 10,
                            "max_wall_clock_seconds": 60,
                        },
                        "stagnation_threshold": 100,
                        "use_sampling": False,
                    },
                )
            )
            sid = start_payload["session_id"]

            await client.call_tool(
                "mission_iterate",
                {"session_id": sid, "max_iterations_this_call": 1},
            )

            # ---- pause ----
            pause_payload = _unwrap_text_json(
                await client.call_tool(
                    "mission_abort",
                    {"session_id": sid, "pause": True},
                )
            )
            assert pause_payload["status"] == "paused"

            # iterate should refuse on a paused session
            iter_payload = _unwrap_text_json(
                await client.call_tool(
                    "mission_iterate",
                    {"session_id": sid, "max_iterations_this_call": 1},
                )
            )
            # ``mission_iterate`` returns a top-level ``code`` envelope
            # when the engine raises a stable code mid-call. ``iterations``
            # is the empty list because no iteration record was appended.
            assert iter_payload.get("code") == "session_paused"
            assert iter_payload.get("iterations", []) == []

            # ---- resume ----
            resume_payload = _unwrap_text_json(
                await client.call_tool("mission_resume", {"session_id": sid})
            )
            assert resume_payload["status"] == "running"

            # ---- complete (force-terminal) ----
            complete_payload = _unwrap_text_json(
                await client.call_tool("mission_complete", {"session_id": sid})
            )
            assert complete_payload["status"] == "completed"

            # idempotent rejection on the second call
            second_payload = _unwrap_text_json(
                await client.call_tool("mission_complete", {"session_id": sid})
            )
            assert second_payload.get("code") == "session_terminal"


# ---------------------------------------------------------------------------
# Result unwrapping
# ---------------------------------------------------------------------------


def _unwrap_text_json(result: Any) -> dict[str, Any]:
    """Extract the JSON payload from a FastMCP ``CallToolResult``.

    Mission tools return JSON-encoded strings; FastMCP wraps the
    response in a ``CallToolResult`` whose ``content`` is a list of
    content blocks. The first block's ``text`` field carries the
    string the tool returned. Pull it out and ``json.loads`` so the
    test sees a plain dict regardless of which protocol shape FastMCP
    happens to surface for ``raise_on_error=True`` calls.

    A FastMCP version that surfaces ``structured_content`` instead of
    relying on the text payload is also handled. Two flavours of
    structured content show up in practice:

    * ``{"result": "<json-string>"}`` — the canonical wrapper for a
      string-returning tool. JSON-parse the inner string.
    * ``{"result": {<dict>}, ...}`` — already-normalised structured
      content for tools whose return type is a typed model.
    """
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict) and structured:
        # Mission tools wrap their JSON return inside a single ``result``
        # key when FastMCP's structured-content normaliser picks up
        # a string return. Unwrap and JSON-parse where needed.
        if set(structured) == {"result"}:
            inner = structured["result"]
            if isinstance(inner, str):
                return json.loads(inner)
            if isinstance(inner, dict):
                return inner
        return structured
    content = getattr(result, "content", None) or []
    if not content:
        raise AssertionError(f"tool result has no content: {result!r}")
    first = content[0]
    text = getattr(first, "text", None)
    if text is None:
        raise AssertionError(f"tool result content block has no text: {first!r}")
    return json.loads(text)


# ---------------------------------------------------------------------------
# Sanity: importing this file should not raise
# ---------------------------------------------------------------------------


def test_module_loads() -> None:
    """Pin against an import-time regression in this test file itself.

    The other tests are async and require GCO_ENABLE_MISSION; this
    bare top-level test runs always so a typo in the module body
    surfaces fast in CI.
    """
    assert callable(_unwrap_text_json)
    assert callable(_force_unregister_mission_tools)
    assert asyncio is not None
