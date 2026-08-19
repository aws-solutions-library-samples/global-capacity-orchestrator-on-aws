"""Swarm supervision tools: one orchestrator Mission driving child Missions.

The whole module body is gated by :data:`feature_flags.FLAG_SWARM` so the
six ``swarm_*`` tool decorators only fire when ``GCO_ENABLE_SWARM=true``.
With the flag unset, this module imports cleanly and FastMCP never sees
the tools.

The tools are thin wrappers over the mission-package swarm machinery:
validation and pool rules in ``mission/swarm.py``, the concurrent child
runner in ``mission/swarm_runner.py``, and plan scaffolding in
``mission/swarm_scaffold.py``. The three in-process supervisor tools
(``mission_spawn`` / ``children_status`` / ``child_abort``) are **not**
registered here — they exist only inside an orchestrator engine's
dispatcher, which is the recursion guard.

[gated by GCO_ENABLE_SWARM]
"""

from __future__ import annotations

import json
import secrets
import sys
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from audit import audit_logged
from feature_flags import FLAG_SWARM, is_enabled
from server import mcp

# Mission package lives under ``gco_mcp/mission/``; the path-injection
# pattern matches the rest of the MCP module surface.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _try_get_context() -> Any | None:
    """Return the active FastMCP Context if inside a request, else ``None``."""
    try:
        from fastmcp.server.dependencies import get_context

        return get_context()
    except Exception:
        return None


# Module body is entirely gated by the feature flag. When the flag is
# unset, none of the tool decorators below fire and FastMCP never sees
# the registrations.
if is_enabled(FLAG_SWARM):
    from mission import sampling as mission_sampling
    from mission import state as mission_state
    from mission import swarm as swarm_rules
    from mission import swarm_scaffold
    from mission._engine_factory import (
        EngineDependencies,
        build_engine_dependencies,
    )
    from mission.swarm_runner import (
        SwarmRunner,
        SwarmRunnerBusyError,
        abort_swarm,
        build_children_snapshot,
        build_fleet_rollup,
        list_swarms,
    )
    from mission.types import TERMINAL_STATES, SessionState
    from mission.validation import (
        MissionValidationError,
        resolve_effective_allowlist,
        validate_budget,
        validate_cadence,
        validate_criteria,
        validate_directive,
    )

    # ------------------------------------------------------------------ #
    # Registry introspection helpers (mirroring tools/mission.py)
    # ------------------------------------------------------------------ #

    async def _registered_tools_dict() -> dict[str, Any]:
        """Live registered-tool map from the shared FastMCP instance."""
        tools = await mcp._list_tools()
        return {tool.name: tool for tool in tools}

    async def _registered_tool_tags() -> dict[str, set[str]]:
        """Live tool-name → tag-set map for risk-tier checks."""
        tools = await mcp._list_tools()
        return {tool.name: set(getattr(tool, "tags", None) or ()) for tool in tools}

    async def _tool_docstrings_dict() -> dict[str, str]:
        """Live tool-name → description map for the plan prompt."""
        tools = await mcp._list_tools()
        return {tool.name: str(getattr(tool, "description", "") or "") for tool in tools}

    def _error(err: MissionValidationError) -> str:
        return json.dumps({"code": err.code, "details": err.details})

    def _strip_private_criteria(criteria: list[Any]) -> list[Any]:
        return [
            {k: v for k, v in c.items() if not str(k).startswith("_")} if isinstance(c, dict) else c
            for c in criteria
        ]

    def _load_orchestrator(session_id: str) -> tuple[Any, SessionState | None, str | None]:
        """Load and role-check; returns (backend, session, error_json)."""
        backend = mission_state.get_backend()
        session = backend.load_session(session_id)
        if session is None:
            return (
                backend,
                None,
                json.dumps({"code": "session_not_found", "details": {"session_id": session_id}}),
            )
        if session.get("role") != "orchestrator" or "swarm" not in session:
            return (
                backend,
                None,
                json.dumps(
                    {
                        "code": "validation_error",
                        "details": {
                            "field": "role",
                            "reason": "not_an_orchestrator",
                            "session_id": session_id,
                        },
                    }
                ),
            )
        return (backend, session, None)

    class _SchemaShim:
        """Duck-typed Pydantic-model stand-in over a plain JSON schema dict."""

        def __init__(self, schema: dict[str, Any]) -> None:
            self._schema = schema

        def model_json_schema(self) -> dict[str, Any]:
            return self._schema

    class _SupervisorToolStub:
        """Tool-shaped catalog entry for a never-registered supervisor tool.

        Gives the Strategy_Revision sampler a name, description, and args
        schema to validate spawn proposals against, without the tool ever
        touching the FastMCP registry — dispatch still routes in-process
        through the runner's wrapper, and every spawn re-validates.
        """

        def __init__(self, name: str) -> None:
            self.name = name
            self.description = swarm_rules.SUPERVISOR_TOOL_DOCSTRINGS[name]
            self.tags = {"swarm", "supervisor"}
            self.input_schema = _SchemaShim(swarm_rules.SUPERVISOR_TOOL_SCHEMAS[name])

    def _supervisor_tool_metadata() -> tuple[dict[str, Any], dict[str, str]]:
        tools = {name: _SupervisorToolStub(name) for name in swarm_rules.SUPERVISOR_TOOLS}
        return tools, dict(swarm_rules.SUPERVISOR_TOOL_DOCSTRINGS)

    def _deps_builder_for(ctx: Any | None) -> Callable[[Mapping[str, Any]], Awaitable[Any]]:
        async def build(session: Mapping[str, Any]) -> EngineDependencies:
            extra = _supervisor_tool_metadata() if session.get("role") == "orchestrator" else None
            return await build_engine_dependencies(session, ctx, extra_tool_metadata=extra)

        return build

    # ------------------------------------------------------------------ #
    # swarm_start
    # ------------------------------------------------------------------ #

    @mcp.tool(tags={"low-risk", "swarm"})
    @audit_logged
    async def swarm_start(
        directive: str,
        criteria: list[dict[str, Any]],
        budget: dict[str, Any],
        swarm: dict[str, Any],
        tool_allowlist: list[str] | None = None,
        allow_all_tools: bool = False,
        checkpoint_cadence: dict[str, Any] | None = None,
        stagnation_threshold: int = 3,
        use_sampling: bool | None = None,
    ) -> str:
        """[gated by GCO_ENABLE_SWARM] Start a new swarm (orchestrator) session.

        Args:
            directive: The swarm-level goal, natural language.
            criteria: Orchestrator success criteria. These evaluate over
                the fleet snapshot — aggregate metrics like
                ``metrics.children_completed`` and predicates over
                ``obs['children']``.
            budget: Orchestrator loop caps (``max_iterations``,
                ``max_wall_clock_seconds``; Mission semantics, ``-1``
                sentinel allowed on one axis).
            swarm: The swarm rails: ``max_children`` and
                ``child_iteration_pool`` (required, strictly positive),
                ``max_concurrent_children`` (default 3),
                ``allow_overlapping_mutating_tools`` (default false).
            tool_allowlist: Optional extra tools for the orchestrator's
                own Execute phase. The three in-process supervisor tools
                are always present; ``children_status`` leads the list so
                the deterministic strategy polls the fleet.
            allow_all_tools: Resolve the extra allowlist to every
                registered tool (minus loop-management names). Mutually
                exclusive with ``tool_allowlist``.
            checkpoint_cadence: Optional cadence dict (default
                ``{"kind": "every_iteration"}``).
            stagnation_threshold: Evaluated iterations with no criterion
                transition before the cascade terminates (default 3) —
                also the swarm's pool-exhaustion exit.
            use_sampling: Three-state opt-in for the orchestrator's
                advisory sampler. ``None`` auto-detects.

        Returns a JSON string with the new ``session_id``, or a
        structured error envelope.
        """
        try:
            directive_clean = validate_directive(directive)
            criteria_clean = validate_criteria(criteria)
            swarm_clean = swarm_rules.validate_swarm_config(swarm)
            registered_tools = await _registered_tools_dict()
            registered_tags = await _registered_tool_tags()
            budget_clean = validate_budget(budget, [], registered_tags)
            cadence_clean = validate_cadence(
                checkpoint_cadence
                if checkpoint_cadence is not None
                else {"kind": "every_iteration"}
            )
            extra: list[str] = []
            if allow_all_tools or tool_allowlist:
                extra = resolve_effective_allowlist(
                    allow_all_tools=allow_all_tools,
                    explicit_allowlist=tool_allowlist,
                    registered_tools=registered_tools,
                )
        except MissionValidationError as err:
            return _error(err)

        ctx = _try_get_context()
        use_resolved, backend_resolved = mission_sampling.resolve_sampling_state(ctx, use_sampling)
        session_id = f"mission-{secrets.token_hex(8)}"
        session = swarm_rules.build_orchestrator_session(
            session_id=session_id,
            directive=directive_clean,
            criteria=criteria_clean,
            budget=budget_clean,
            swarm_config=swarm_clean,
            cadence=cadence_clean,
            extra_allowlist=extra,
            stagnation_threshold=stagnation_threshold,
            use_sampling=use_resolved,
            sampling_backend_resolved=backend_resolved,
            created_at=datetime.now(UTC).isoformat(),
        )
        mission_state.get_backend().save_session(cast("SessionState", session))
        return json.dumps(
            {
                "session_id": session_id,
                "status": "pending",
                "use_sampling": use_resolved,
                "sampling_backend_resolved": backend_resolved,
                "swarm": swarm_clean,
            }
        )

    # ------------------------------------------------------------------ #
    # swarm_iterate
    # ------------------------------------------------------------------ #

    @mcp.tool(tags={"low-risk", "swarm"})
    @audit_logged
    async def swarm_iterate(
        session_id: str,
        max_orchestrator_iterations: int | None = None,
    ) -> str:
        """[gated by GCO_ENABLE_SWARM] Drive a swarm's fleet forward.

        Long-running: builds the child runner, schedules every live
        child, and iterates the orchestrator — to its terminal verdict
        by default, or detaching after ``max_orchestrator_iterations``
        with the fleet left resumable. Exactly one live runner may
        drive a swarm at a time; a second call while one runs returns a
        ``swarm_runner_active`` envelope naming the holding process.
        """
        backend, session, error = _load_orchestrator(session_id)
        if error is not None:
            return error
        assert session is not None
        if session["status"] in TERMINAL_STATES:
            return json.dumps(
                {
                    "code": "session_terminal",
                    "details": {"session_id": session_id, "status": session["status"]},
                }
            )
        ctx = _try_get_context()
        registered_tools = await _registered_tools_dict()
        registered_tags = await _registered_tool_tags()
        try:
            runner = SwarmRunner(
                backend=backend,
                orchestrator_id=session_id,
                deps_builder=_deps_builder_for(ctx),
                registered_tools=registered_tools,
                registered_tags=registered_tags,
            )
            final = await runner.run_to_completion(
                max_orchestrator_iterations=max_orchestrator_iterations
            )
        except SwarmRunnerBusyError as busy:
            return json.dumps(
                {
                    "code": "swarm_runner_active",
                    "details": {
                        "session_id": session_id,
                        "holder_pid": busy.holder_pid,
                    },
                }
            )
        except MissionValidationError as err:
            return _error(err)
        snapshot = build_children_snapshot(
            final["swarm"], final.get("children", []), backend.load_session
        )
        return json.dumps(
            {
                "session_id": session_id,
                "status": final["status"],
                "final_verdict": final.get("final_verdict"),
                "iterations_run": len(final.get("iterations", [])),
                "children": snapshot["metrics"],
            }
        )

    # ------------------------------------------------------------------ #
    # swarm_status
    # ------------------------------------------------------------------ #

    @mcp.tool(tags={"safe", "swarm"})
    @audit_logged
    async def swarm_status(session_id: str) -> str:
        """[gated by GCO_ENABLE_SWARM] One-call fleet rollup for a swarm.

        Returns the orchestrator summary, the swarm rails, the iteration
        pool balance, the slot-ordered child table, and a findings list
        (orphaned runner heartbeat, unreadable children, exhausted pool)
        in the ``fleet_status`` one-document style.
        """
        backend, session, error = _load_orchestrator(session_id)
        if error is not None:
            return error
        assert session is not None
        return json.dumps(build_fleet_rollup(backend, session))

    # ------------------------------------------------------------------ #
    # swarm_abort
    # ------------------------------------------------------------------ #

    @mcp.tool(tags={"low-risk", "swarm"})
    @audit_logged
    async def swarm_abort(session_id: str) -> str:
        """[gated by GCO_ENABLE_SWARM] Terminate a swarm and its children.

        Transitions the orchestrator to ``terminated`` and aborts every
        non-terminal child through the standard abort transition,
        settling each slot's pool reservation. Works with no live
        runner; a live runner observes the terminal orchestrator at its
        next boundary and stands down.
        """
        backend, session, error = _load_orchestrator(session_id)
        if error is not None:
            return error
        assert session is not None
        if session["status"] in TERMINAL_STATES:
            return json.dumps(
                {
                    "code": "session_terminal",
                    "details": {"session_id": session_id, "status": session["status"]},
                }
            )
        return json.dumps(abort_swarm(backend, session))

    # ------------------------------------------------------------------ #
    # swarm_list
    # ------------------------------------------------------------------ #

    @mcp.tool(tags={"safe", "swarm"})
    @audit_logged
    async def swarm_list(status: str | None = None) -> str:
        """[gated by GCO_ENABLE_SWARM] List swarm (orchestrator) sessions.

        Optionally filtered by lifecycle ``status``. Child and standalone
        sessions never appear here — use ``mission_list`` for those.
        """
        backend = mission_state.get_backend()
        return json.dumps({"swarms": list_swarms(backend, status=status)})

    # ------------------------------------------------------------------ #
    # swarm_plan
    # ------------------------------------------------------------------ #

    @mcp.tool(tags={"safe", "swarm"})
    @audit_logged
    async def swarm_plan(
        directive: str,
        swarm: dict[str, Any],
        tool_allowlist: list[str] | None = None,
        allow_all_tools: bool = False,
        max_children: int | None = None,
        use_sampling: bool | None = None,
        retries: int = 3,
    ) -> str:
        """[gated by GCO_ENABLE_SWARM] Draft a validated swarm plan.

        Decomposes the directive into admission-validated spawn requests
        — the sampled path with retry-and-feedback when a sampling
        backend resolves, always falling back to the deterministic
        single-worker plan. The returned plan feeds ``mission_spawn``
        requests (or ``gco swarm run``) verbatim; review before running.
        """
        try:
            directive_clean = validate_directive(directive)
            config = swarm_rules.validate_swarm_config(swarm)
        except MissionValidationError as err:
            return _error(err)
        registered_tools = await _registered_tools_dict()
        registered_tags = await _registered_tool_tags()
        docstrings = await _tool_docstrings_dict()
        ctx = _try_get_context()
        use_resolved, backend_name = mission_sampling.resolve_sampling_state(ctx, use_sampling)
        plan: list[dict[str, Any]] | None = None
        fallback_reason: str | None = None
        if use_resolved:
            backend_obj = mission_sampling.select_sampling_backend(ctx, None, None)
            if backend_obj is not None:
                try:
                    plan = await swarm_scaffold.generate_sampled_plan(
                        backend_obj,
                        directive_clean,
                        config=config,
                        registered_tools=registered_tools,
                        registered_tags=registered_tags,
                        tool_docstrings=docstrings,
                        max_children=max_children,
                        retries=retries,
                    )
                except swarm_scaffold.SwarmScaffoldError as err:
                    fallback_reason = err.last_reason
        if plan is None:
            try:
                plan = swarm_scaffold.generate_deterministic_plan(
                    directive_clean,
                    config=config,
                    registered_tools=registered_tools,
                    registered_tags=registered_tags,
                    tool_allowlist=tool_allowlist,
                    allow_all_tools=allow_all_tools,
                )
            except MissionValidationError as err:
                return _error(err)
        return json.dumps(
            {
                "plan": plan,
                "sampling_path": fallback_reason is None and use_resolved,
                "sampling_backend_resolved": backend_name,
                "fallback_reason": fallback_reason,
            }
        )
