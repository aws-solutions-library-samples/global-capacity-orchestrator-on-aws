"""Mission goal-directed iteration loop tools.

The whole module body is gated by :data:`feature_flags.FLAG_MISSION` so
the nine ``mission_*`` tool decorators only fire when
``GCO_ENABLE_MISSION=true``. With the flag unset, this module imports
cleanly and FastMCP never sees the tools.

[gated by GCO_ENABLE_MISSION]
"""

from __future__ import annotations

import contextlib
import json
import secrets
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from audit import audit_logged
from feature_flags import FLAG_MISSION, is_enabled
from server import mcp

# Mission package lives under ``mcp/mission/``; the path-injection
# pattern matches the rest of the MCP module surface so ``import
# mission.*`` resolves without making the ``mcp`` directory a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _try_get_context() -> Any | None:
    """Return the active FastMCP Context if inside a request, else ``None``.

    Mirrors :func:`mcp.tools.jobs._ctx_warning`: wraps the optional
    ``fastmcp.server.dependencies.get_context`` import so the helper
    works in unit tests that don't go through an MCP request — those
    raise ``RuntimeError`` from ``get_context()``, which we swallow.
    """
    try:
        from fastmcp.server.dependencies import get_context

        return get_context()
    except Exception:
        return None


# Module body is entirely gated by the feature flag. When the flag is
# unset, none of the tool decorators below fire and FastMCP never sees
# the registrations.
if is_enabled(FLAG_MISSION):
    from mission import (
        sampling as mission_sampling,
    )
    from mission import (
        state as mission_state,
    )
    from mission import (
        validation as mission_validation,
    )
    from mission.decide import decide_verdict
    from mission.engine import MissionEngineError
    from mission.types import SCHEMA_VERSION, TERMINAL_STATES, SessionState
    from mission.validation import MissionValidationError

    # ------------------------------------------------------------------ #
    # Registry introspection helpers
    # ------------------------------------------------------------------ #

    async def _registered_tools_dict() -> dict[str, Any]:
        """Return a name -> Tool object mapping for every registered tool.

        Uses ``mcp._list_tools()`` because that bypasses the catalog-
        replacement transforms (BM25 / Code Mode) and gives us the
        underlying registry. Tolerates FastMCP API drift by falling
        back to an empty mapping on any exception — the validators
        downstream interpret an empty dict as "nothing registered",
        which is benign for sessions that don't lean on tag-based
        cost gating.
        """
        try:
            tools = await mcp._list_tools()
        except Exception:
            return {}
        return {t.name: t for t in tools}

    async def _registered_tool_tags() -> dict[str, set[str]]:
        """Return name -> tag-set for every registered tool.

        Same defensive shape as :func:`_registered_tools_dict`. Tools
        with no declared ``tags`` attribute contribute an empty set so
        the budget validator treats them as non-cost-incurring.
        """
        registered = await _registered_tools_dict()
        out: dict[str, set[str]] = {}
        for name, tool in registered.items():
            tags = getattr(tool, "tags", None)
            out[name] = set(tags) if tags else set()
        return out

    async def _tool_docstrings_dict() -> dict[str, str]:
        """Return name -> docstring/description mapping for sampling prompts."""
        registered = await _registered_tools_dict()
        return {name: (getattr(t, "description", "") or "") for name, t in registered.items()}

    # ------------------------------------------------------------------ #
    # Session helpers
    # ------------------------------------------------------------------ #

    def _strip_private_fields(session: Mapping[str, Any]) -> dict[str, Any]:
        """Return a JSON-safe copy of ``session`` with private criterion keys dropped.

        Thin alias over :func:`mission.validation.strip_private_fields`.
        Kept as a module-private name so call sites in this file
        (``mission_start``, ``mission_complete``, etc.) read at a
        glance without having to qualify the canonical helper through
        the long ``mission.validation`` path.
        """
        return mission_validation.strip_private_fields(session)

    def _strip_private_fields_iterations(
        iterations: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Strip private keys from each iteration's ``criteria_evaluation`` shape.

        Thin alias over
        :func:`mission.validation.strip_private_fields_iterations`.
        """
        return mission_validation.strip_private_fields_iterations(iterations)

    # ------------------------------------------------------------------ #
    # Engine wiring
    # ------------------------------------------------------------------ #

    # The engine factory itself lives in :mod:`mcp.mission._engine_factory`
    # so the CLI can reuse the same wiring without crossing the
    # ``GCO_ENABLE_MISSION`` gate. We keep a thin alias here so call
    # sites in this module stay readable without having to spell out
    # the long import path.
    from mission._engine_factory import build_mission_engine as _build_engine  # noqa: PLC0415

    # ------------------------------------------------------------------ #
    # mission_start
    # ------------------------------------------------------------------ #

    @mcp.tool(tags={"low-risk", "mission"})
    @audit_logged
    async def mission_start(
        directive: str,
        criteria: list[dict[str, Any]],
        budget: dict[str, Any],
        tool_allowlist: list[str] | None = None,
        checkpoint_cadence: dict[str, Any] | None = None,
        stagnation_threshold: int = 3,
        use_sampling: bool | None = None,
        allow_scripted_strategies: bool = False,
        sampling_model_preferences: dict[str, Any] | None = None,
        allow_all_tools: bool = False,
    ) -> str:
        """[gated by GCO_ENABLE_MISSION] Start a new Mission session.

        Args:
            directive: Natural-language goal description.
            criteria: List of success criterion dicts (``metric_threshold``,
                ``event``, or ``predicate`` kinds).
            budget: Budget controls dict with ``max_iterations`` and
                ``max_wall_clock_seconds``. Cost guardrails live
                out-of-band via AWS Budgets and Cost Anomaly Detection.
            tool_allowlist: List of tool names the session may invoke.
                Optional; omit it when ``allow_all_tools`` is set.
            checkpoint_cadence: Optional cadence dict (default
                ``{"kind": "every_iteration"}``).
            stagnation_threshold: Iterations of no progress before
                terminating (default 3).
            use_sampling: Three-state opt-in. ``None`` auto-detects,
                ``True`` opts in explicitly, ``False`` opts out.
            allow_scripted_strategies: When True, the session permits
                scripted strategies (validated via the sandbox AST).
            sampling_model_preferences: Optional FastMCP
                ``ModelPreferences`` payload forwarded to MCP sampling.
            allow_all_tools: When True (default ``False``), resolve the
                session's allowlist to every currently-registered tool
                minus the ``mission_*`` control tools, instead of an
                explicit ``tool_allowlist``. Mutually exclusive with a
                non-empty ``tool_allowlist``.

        Returns a JSON string with the new ``session_id`` and the
        resolved sampling state, or an error envelope.
        """
        try:
            directive_clean = mission_validation.validate_directive(directive)
            criteria_clean = mission_validation.validate_criteria(criteria)
            registered_tools = await _registered_tools_dict()
            registered_tags = await _registered_tool_tags()
            control_tools = {n for n, tags in registered_tags.items() if "mission" in tags}
            allowlist_clean = mission_validation.resolve_effective_allowlist(
                allow_all_tools=allow_all_tools,
                explicit_allowlist=tool_allowlist,
                registered_tools=registered_tools,
                control_tools=control_tools,
            )
            budget_clean = mission_validation.validate_budget(
                budget, allowlist_clean, registered_tags
            )
            cadence_clean = mission_validation.validate_cadence(
                checkpoint_cadence
                if checkpoint_cadence is not None
                else {"kind": "every_iteration"}
            )
        except MissionValidationError as err:
            return json.dumps({"code": err.code, "details": err.details})

        ctx = _try_get_context()
        use_sampling_resolved, backend_resolved = mission_sampling.resolve_sampling_state(
            ctx, use_sampling
        )

        session_id = f"mission-{secrets.token_hex(8)}"
        session: dict[str, Any] = {
            "version": SCHEMA_VERSION,
            "session_id": session_id,
            "directive_text": directive_clean,
            "criteria": criteria_clean,
            "budget": budget_clean,
            "tool_allowlist": allowlist_clean,
            "checkpoint_cadence": cadence_clean,
            "stagnation_threshold": stagnation_threshold,
            "use_sampling": use_sampling_resolved,
            "sampling_backend_resolved": backend_resolved,
            "allow_scripted_strategies": bool(allow_scripted_strategies),
            "status": "pending",
            "created_at": datetime.now(UTC).isoformat(),
            "iterations": [],
            "no_progress_counter": 0,
        }
        if sampling_model_preferences is not None:
            session["sampling_model_preferences"] = sampling_model_preferences

        backend = mission_state.get_backend()
        # Strip the validator's cached ``_parsed_ast`` AST nodes from
        # predicate criteria before persistence — ``ast.Expression``
        # is not JSON-serialisable and the FilesystemBackend writes
        # via ``json.dump``. The engine re-parses on demand from the
        # ``expression`` string when it next loads the session, so
        # stripping here is lossless. Mirrors ``_strip_private_criteria``
        # in ``cli/commands/mission_cmd.py``.
        backend.save_session(cast("SessionState", _strip_private_fields(session)))

        return json.dumps(
            {
                "session_id": session_id,
                "status": "pending",
                "use_sampling": use_sampling_resolved,
                "sampling_backend_resolved": backend_resolved,
            }
        )

    # ------------------------------------------------------------------ #
    # mission_status
    # ------------------------------------------------------------------ #

    @mcp.tool(tags={"safe", "mission"})
    @audit_logged
    async def mission_status(session_id: str) -> str:
        """[gated by GCO_ENABLE_MISSION] Get the full state of a Mission session.

        Args:
            session_id: The session identifier returned by
                :func:`mission_start`.

        Returns the full session JSON or an error envelope when the
        session is unknown.
        """
        backend = mission_state.get_backend()
        session = backend.load_session(session_id)
        if session is None:
            return json.dumps(
                {
                    "code": "session_not_found",
                    "details": {"session_id": session_id},
                }
            )
        cleaned = _strip_private_fields(session)
        return json.dumps(cleaned, default=str)

    # ------------------------------------------------------------------ #
    # mission_iterate
    # ------------------------------------------------------------------ #

    @mcp.tool(tags={"low-risk", "mission"})
    @audit_logged
    async def mission_iterate(
        session_id: str,
        max_iterations_this_call: int = 1,
    ) -> str:
        """[gated by GCO_ENABLE_MISSION] Run iteration(s) on a Mission session.

        Args:
            session_id: The session to iterate.
            max_iterations_this_call: How many iterations to run before
                returning (default 1). The loop exits early on a
                terminal verdict (``complete`` or ``terminate``).

        Returns a JSON object with a ``session_id`` and an
        ``iterations`` list of iteration summaries (verdict, reason,
        iteration index). On engine errors, returns an error envelope
        with whatever summaries had accumulated before the failure.
        """
        if max_iterations_this_call < 1:
            return json.dumps(
                {
                    "code": "invalid_argument",
                    "details": {"reason": "max_iterations_this_call must be >= 1"},
                }
            )

        ctx = _try_get_context()
        backend = mission_state.get_backend()

        session = backend.load_session(session_id)
        if session is None:
            return json.dumps(
                {
                    "code": "session_not_found",
                    "details": {"session_id": session_id},
                }
            )

        engine = await _build_engine(session, ctx)

        summaries: list[dict[str, Any]] = []
        for _ in range(max_iterations_this_call):
            try:
                record = await engine.run_iteration(session_id, ctx=ctx)
            except MissionEngineError as err:
                return json.dumps(
                    {
                        "code": err.code,
                        "details": {"session_id": session_id},
                        "iterations": summaries,
                    }
                )
            summaries.append(
                {
                    "iteration_index": record["iteration_index"],
                    "verdict": record["verdict"],
                    "verdict_reason": record["verdict_reason"],
                }
            )
            if record["verdict"] in ("complete", "terminate"):
                break

        return json.dumps({"session_id": session_id, "iterations": summaries})

    # ------------------------------------------------------------------ #
    # mission_checkpoint
    # ------------------------------------------------------------------ #

    @mcp.tool(tags={"safe", "mission"})
    @audit_logged
    async def mission_checkpoint(session_id: str) -> str:
        """[gated by GCO_ENABLE_MISSION] Re-run the verdict cascade on the latest iteration.

        Args:
            session_id: The session whose latest iteration should be
                re-evaluated.

        Returns a JSON object with the freshly-computed verdict and
        reason. Does not run the propose / execute / observe phases —
        only the deterministic decide cascade. Returns an error envelope
        when the session is missing or has no iterations yet.
        """
        backend = mission_state.get_backend()
        session = backend.load_session(session_id)
        if session is None:
            return json.dumps(
                {
                    "code": "session_not_found",
                    "details": {"session_id": session_id},
                }
            )
        iterations = session.get("iterations") or []
        if not iterations:
            return json.dumps(
                {
                    "code": "no_iterations",
                    "details": {"session_id": session_id},
                }
            )

        latest = iterations[-1]
        verdict, reason = decide_verdict(session, latest, datetime.now(UTC))
        return json.dumps(
            {
                "session_id": session_id,
                "iteration_index": latest["iteration_index"],
                "verdict": verdict,
                "verdict_reason": reason,
            }
        )

    # ------------------------------------------------------------------ #
    # mission_complete
    # ------------------------------------------------------------------ #

    @mcp.tool(tags={"low-risk", "mission"})
    @audit_logged
    async def mission_complete(session_id: str, reason: str = "forced_complete") -> str:
        """[gated by GCO_ENABLE_MISSION] Force a Mission session into completed status.

        Args:
            session_id: The session to complete.
            reason: Free-form reason recorded alongside the synthetic
                final verdict (default ``forced_complete``).

        Stamps a synthetic ``complete`` final verdict and an
        ``ended_at`` timestamp. Refuses sessions already in a terminal
        state.
        """
        del reason  # currently informational; the synthetic verdict is fixed
        backend = mission_state.get_backend()
        session = backend.load_session(session_id)
        if session is None:
            return json.dumps(
                {
                    "code": "session_not_found",
                    "details": {"session_id": session_id},
                }
            )
        if session["status"] in TERMINAL_STATES:
            return json.dumps(
                {
                    "code": "session_terminal",
                    "details": {
                        "session_id": session_id,
                        "status": session["status"],
                    },
                }
            )
        session["status"] = "completed"
        session["final_verdict"] = "complete"
        session["ended_at"] = datetime.now(UTC).isoformat()
        # Defensive strip — sessions loaded from the backend were
        # already private-field-clean, but a future change that
        # re-attaches ``_parsed_ast`` somewhere in the flow shouldn't
        # silently break persistence. ``_strip_private_fields`` is
        # cheap and idempotent on already-clean inputs.
        backend.save_session(cast("SessionState", _strip_private_fields(session)))
        return json.dumps({"session_id": session_id, "status": "completed"})

    # ------------------------------------------------------------------ #
    # mission_abort
    # ------------------------------------------------------------------ #

    @mcp.tool(tags={"low-risk", "mission"})
    @audit_logged
    async def mission_abort(session_id: str, pause: bool = False) -> str:
        """[gated by GCO_ENABLE_MISSION] Pause or terminate a Mission session.

        Args:
            session_id: The session to transition.
            pause: When True, transition to ``paused``. When False
                (default), transition to ``terminated`` with a
                synthetic ``terminate`` final verdict.

        Refuses sessions already in a terminal state. Best-effort
        emits a ``ctx.warning`` when the active request context is
        available so the operator sees the side-effect.
        """
        backend = mission_state.get_backend()
        session = backend.load_session(session_id)
        if session is None:
            return json.dumps(
                {
                    "code": "session_not_found",
                    "details": {"session_id": session_id},
                }
            )
        if session["status"] in TERMINAL_STATES:
            return json.dumps(
                {
                    "code": "session_terminal",
                    "details": {
                        "session_id": session_id,
                        "status": session["status"],
                    },
                }
            )
        if pause:
            session["status"] = "paused"
        else:
            session["status"] = "terminated"
            session["final_verdict"] = "terminate"
            session["ended_at"] = datetime.now(UTC).isoformat()
            ctx = _try_get_context()
            if ctx is not None:
                with contextlib.suppress(Exception):
                    await ctx.warning(f"Mission session {session_id} terminated by operator.")
        # Defensive strip — see mission_complete for the rationale.
        backend.save_session(cast("SessionState", _strip_private_fields(session)))
        return json.dumps({"session_id": session_id, "status": session["status"]})

    # ------------------------------------------------------------------ #
    # mission_resume
    # ------------------------------------------------------------------ #

    @mcp.tool(tags={"low-risk", "mission"})
    @audit_logged
    async def mission_resume(session_id: str) -> str:
        """[gated by GCO_ENABLE_MISSION] Resume a paused Mission session.

        Args:
            session_id: The session to resume.

        Transitions ``paused -> running``. Returns an error envelope
        when the session is missing or not in ``paused`` state.
        """
        backend = mission_state.get_backend()
        session = backend.load_session(session_id)
        if session is None:
            return json.dumps(
                {
                    "code": "session_not_found",
                    "details": {"session_id": session_id},
                }
            )
        if session["status"] != "paused":
            return json.dumps(
                {
                    "code": "not_paused",
                    "details": {
                        "session_id": session_id,
                        "status": session["status"],
                    },
                }
            )
        session["status"] = "running"
        # Defensive strip — see mission_complete for the rationale.
        backend.save_session(cast("SessionState", _strip_private_fields(session)))
        return json.dumps({"session_id": session_id, "status": "running"})

    # ------------------------------------------------------------------ #
    # mission_history
    # ------------------------------------------------------------------ #

    @mcp.tool(tags={"safe", "mission"})
    @audit_logged
    async def mission_history(session_id: str, format: str = "summary") -> str:
        """[gated by GCO_ENABLE_MISSION] Get iteration history for a Mission session.

        Args:
            session_id: The session whose history to retrieve.
            format: ``"summary"`` (default) returns a compact list of
                ``{iteration_index, verdict, verdict_reason,
                started_at, ended_at}`` dicts; ``"full"`` returns the
                complete iteration record dicts.

        Returns a JSON object with an ``iterations`` list, or an error
        envelope when the session is unknown.
        """
        backend = mission_state.get_backend()
        session = backend.load_session(session_id)
        if session is None:
            return json.dumps(
                {
                    "code": "session_not_found",
                    "details": {"session_id": session_id},
                }
            )
        iterations = session.get("iterations") or []
        if format == "full":
            return json.dumps(
                {"iterations": _strip_private_fields_iterations(iterations)},
                default=str,
            )
        summaries = [
            {
                "iteration_index": it.get("iteration_index"),
                "verdict": it.get("verdict"),
                "verdict_reason": it.get("verdict_reason"),
                "started_at": it.get("started_at"),
                "ended_at": it.get("ended_at"),
            }
            for it in iterations
        ]
        return json.dumps({"iterations": summaries})

    # ------------------------------------------------------------------ #
    # mission_list
    # ------------------------------------------------------------------ #

    @mcp.tool(tags={"safe", "mission"})
    @audit_logged
    async def mission_list(status: str | None = None) -> str:
        """[gated by GCO_ENABLE_MISSION] List Mission sessions.

        Args:
            status: Optional filter. Recognised values are ``running``,
                ``completed``, ``terminated``, ``failed``, ``paused``,
                ``pending``. Omit to list every known session.

        Returns a JSON object with a ``sessions`` list of summary dicts
        (``session_id``, ``status``, ``created_at``,
        ``iteration_count``).
        """
        backend = mission_state.get_backend()
        filter_dict = {"status": status} if status else None
        sessions = backend.list_sessions(filter_dict)
        return json.dumps({"sessions": sessions})
