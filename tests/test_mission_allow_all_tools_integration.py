"""Integration checks that an all-tools-resolved session behaves like an explicit one.

When an operator asks a session to reach every registered tool, that request
is expanded once, at session-start time, into a concrete enumerated list of
tool names with the session-management control tools removed. The expanded
list is stored on the session exactly as if the operator had typed each name
by hand. Nothing downstream is told how the list came to be — it is a plain
list of strings either way.

This module exercises the unchanged iteration engine against such a session
and confirms the Execute phase gates tool calls the same way it would for a
hand-typed list: a name that is on the list dispatches and records a normal
result, while a name that is off the list (including a control tool that the
expansion deliberately dropped) is recorded as skipped without ever reaching
the dispatcher. The same strategy run against a session whose list was typed
out explicitly produces byte-identical call records, which is the fidelity
guarantee under test.

The module is organised so later fidelity checks can append cleanly: the
registry fixture, the all-tools resolution helper, the session builder, and
the single-iteration driver are all shared module-level helpers.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Mirror the import pattern every other Mission test uses: ``mcp/run_mcp.py``
# adds ``mcp/`` to ``sys.path`` at runtime, but pytest has to do it itself
# before the imports below resolve.
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))

from mission import (
    SCHEMA_VERSION,  # noqa: E402
    validation,  # noqa: E402
)
from mission.engine import MissionEngine  # noqa: E402
from mission.sampling import SamplingUsed, _render_tool_allowlist  # noqa: E402
from mission.state import FilesystemBackend  # noqa: E402

# ---------------------------------------------------------------------------
# Shared fixtures, helpers, and constants
# ---------------------------------------------------------------------------

# A name that is registered but is a session-management control tool, so the
# all-tools expansion drops it from the resolved list. Reusing it as a call
# target proves the dropped name is gated exactly like any other off-list name.
_CONTROL_TOOL = "mission_status"

# Three ordinary tools the expansion keeps. One of them is the allowlisted
# call target exercised below.
_ALLOWED_TOOLS: tuple[str, ...] = ("find_docs", "find_examples", "list_jobs")

# The allowlisted tool the driver dispatches. Picked from the kept set above.
_DISPATCHED_TOOL = "find_examples"


def _registry_with_control_tool() -> dict[str, Any]:
    """Build a registered-tools mapping that includes one control tool.

    Only the keys of this mapping are read by the resolver, so the values are
    simple placeholders. The control tool is present in the registry but is
    expected to be excluded from the resolved list.
    """
    registry: dict[str, Any] = {name: object() for name in _ALLOWED_TOOLS}
    registry[_CONTROL_TOOL] = object()
    return registry


def _resolve_all_tools_allowlist() -> list[str]:
    """Resolve a session allowlist the way the all-tools path produces it.

    Expands a registry to every registered name minus the control tools,
    yielding a concrete, sorted, duplicate-free enumerated list that is
    indistinguishable from one an operator could have typed.
    """
    return validation.resolve_effective_allowlist(
        allow_all_tools=True,
        explicit_allowlist=None,
        registered_tools=_registry_with_control_tool(),
    )


def _make_session(
    *,
    session_id: str,
    tool_allowlist: list[str],
) -> dict[str, Any]:
    """Build a running session that is poised to consult the sampler.

    The session carries a synthetic prior iteration whose verdict is
    ``adjust`` and turns sampling on, so the next Propose phase adopts a
    sampler-supplied strategy. Built by hand because the engine consumes the
    typed fields directly and the allowlist provenance is the only thing that
    differs between the cases under test.
    """
    return {
        "version": SCHEMA_VERSION,
        "session_id": session_id,
        "directive_text": "Exercise Execute-phase gating against the session allowlist.",
        "criteria": [
            {
                "criterion_id": "unreachable",
                "kind": "metric_threshold",
                "required": True,
                "metric": "metrics.val_loss",
                "op": "<=",
                "target": -1.0,
            }
        ],
        "budget": {"max_iterations": 10, "max_wall_clock_seconds": 600},
        "tool_allowlist": tool_allowlist,
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 100,
        "use_sampling": True,
        "allow_scripted_strategies": False,
        "status": "running",
        "created_at": "2025-01-01T00:00:00Z",
        "started_at": "2025-01-01T00:00:00+00:00",
        "iterations": [
            {
                "iteration_index": 0,
                "started_at": "2025-01-01T00:00:00+00:00",
                "ended_at": "2025-01-01T00:00:01+00:00",
                "phases": [],
                "strategy": {"tool_calls": []},
                "observation": {
                    "tool_results": [],
                    "metrics": {},
                    "events": [],
                    "phase_started_at": "2025-01-01T00:00:00+00:00",
                    "phase_ended_at": "2025-01-01T00:00:01+00:00",
                },
                "criteria_evaluation": [],
                "verdict": "adjust",
                "verdict_reason": "heuristic_unproductive",
                "checkpoint_evaluated": True,
            }
        ],
        "no_progress_counter": 0,
    }


def _sampler_returning(strategy: dict[str, Any]) -> Any:
    """Build a sampling callable that always proposes ``strategy``.

    The returned strategy is delivered as an accepted sampler output so the
    engine adopts it verbatim for the Execute phase.
    """

    async def sampling_callable(*, session: dict, ctx: Any) -> SamplingUsed:
        del session, ctx
        return SamplingUsed(
            output_text="{}",
            parsed={"revision_rationale": "r", "next_strategy": strategy},
            backend_name="mcp",
            model_id="test-model",
        )

    return sampling_callable


async def _run_one_iteration(
    backend: FilesystemBackend,
    session: dict[str, Any],
    strategy: dict[str, Any],
) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
    """Persist ``session``, run one iteration with ``strategy``, return results.

    Returns the iteration record and the list of ``(tool_name, args)`` pairs
    the dispatcher actually received, so callers can assert both the recorded
    call outcomes and which calls reached dispatch.
    """
    backend.save_session(session)
    dispatched: list[tuple[str, dict[str, Any]]] = []

    async def dispatcher(tool_name: str, args: dict, ctx: Any) -> dict[str, Any]:
        del ctx
        dispatched.append((tool_name, dict(args)))
        return {"ok": True}

    engine = MissionEngine(
        backend=backend,
        tool_dispatcher=dispatcher,
        sampling_callable=_sampler_returning(strategy),
        sandbox_runner=None,
    )
    record = await engine.run_iteration(session["session_id"])
    return record, dispatched


def _call_outcomes(record: dict[str, Any]) -> list[tuple[str, str]]:
    """Extract ``(tool_name, status)`` pairs from an iteration's executed calls."""
    calls = record["strategy"]["tool_calls"]
    return [(call["tool_name"], call["status"]) for call in calls]


# A strategy that names one allowlisted tool and one off-list control tool, in
# that order, so the Execute phase must dispatch the first and gate the second.
_MIXED_STRATEGY: dict[str, Any] = {
    "tool_calls": [
        {"tool_name": _DISPATCHED_TOOL, "args": {"query": "x"}},
        {"tool_name": _CONTROL_TOOL, "args": {}},
    ],
    "rationale": "one allowlisted call followed by one off-list call",
}


# ---------------------------------------------------------------------------
# Execute-phase gating fidelity
# ---------------------------------------------------------------------------


async def test_all_tools_session_gates_execute_phase_like_an_explicit_list(
    tmp_path: Path,
) -> None:
    """An all-tools-resolved session gates tool calls identically to a typed list.

    Two sessions are run through the unchanged engine with the same proposed
    strategy. The first session's allowlist was produced by the all-tools
    expansion; the second's was typed out explicitly to the same names. In
    both, the allowlisted tool dispatches and records ``ok`` while the off-list
    control tool is recorded ``skipped_not_allowed`` without reaching the
    dispatcher. The two sessions yield identical call outcomes, proving the
    gate does not care how the allowlist was produced.
    """
    resolved_allowlist = _resolve_all_tools_allowlist()

    # The expansion keeps the ordinary tools and drops the control tool.
    assert resolved_allowlist == sorted(_ALLOWED_TOOLS)
    assert _CONTROL_TOOL not in resolved_allowlist

    # All-tools-resolved session.
    all_tools_backend = FilesystemBackend(root=tmp_path / "all_tools")
    all_tools_session = _make_session(
        session_id="sess-all-tools",
        tool_allowlist=list(resolved_allowlist),
    )
    all_tools_record, all_tools_dispatched = await _run_one_iteration(
        all_tools_backend, all_tools_session, _MIXED_STRATEGY
    )

    # Explicit-list session with the same names typed out by hand.
    explicit_backend = FilesystemBackend(root=tmp_path / "explicit")
    explicit_session = _make_session(
        session_id="sess-explicit",
        tool_allowlist=sorted(_ALLOWED_TOOLS),
    )
    explicit_record, explicit_dispatched = await _run_one_iteration(
        explicit_backend, explicit_session, _MIXED_STRATEGY
    )

    # The allowlisted call dispatched; the off-list control call never did.
    expected_dispatched = [(_DISPATCHED_TOOL, {"query": "x"})]
    assert all_tools_dispatched == expected_dispatched
    assert explicit_dispatched == expected_dispatched

    # The recorded outcomes show the allowlisted call succeeding and the
    # off-list control call being gated out.
    expected_outcomes = [(_DISPATCHED_TOOL, "ok"), (_CONTROL_TOOL, "skipped_not_allowed")]
    assert _call_outcomes(all_tools_record) == expected_outcomes

    # The explicit-list session produces identical outcomes — gating is
    # independent of how the allowlist was produced.
    assert _call_outcomes(explicit_record) == _call_outcomes(all_tools_record)


# ---------------------------------------------------------------------------
# Final_Report snapshot fidelity
# ---------------------------------------------------------------------------

# Driver-loop safety bound. A session capped at a couple of iterations reaches
# a terminal verdict well under this many turns; the bound turns a regression
# in cap detection into a clean failure rather than an infinite loop.
_REPORT_DRIVER_LOOP_BOUND = 20


def _make_terminal_session(
    *,
    session_id: str,
    tool_allowlist: list[str],
) -> dict[str, Any]:
    """Build a pending session that the engine drives to a terminal verdict.

    The lone criterion targets an unreachable validation-loss value, so the
    completion branch never fires and the run exits through the iteration cap
    instead. Sampling is off, so the Propose phase falls back to invoking the
    first allowlisted tool — which is why the allowlist must be non-empty and
    enumerated, exactly what the all-tools expansion produces.
    """
    return {
        "version": SCHEMA_VERSION,
        "session_id": session_id,
        "directive_text": "Drive to a terminal verdict so the report snapshots the allowlist.",
        "criteria": [
            {
                "criterion_id": "unreachable_loss_target",
                "kind": "metric_threshold",
                "required": True,
                "metric": "metrics.val_loss",
                "op": "<=",
                "target": -1.0,
            }
        ],
        "budget": {"max_iterations": 2, "max_wall_clock_seconds": 600},
        "tool_allowlist": tool_allowlist,
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 100,
        "use_sampling": False,
        "allow_scripted_strategies": False,
        "status": "pending",
        "created_at": "2025-01-01T00:00:00Z",
        "iterations": [],
        "no_progress_counter": 0,
    }


def _unmet_metric_dispatcher() -> Any:
    """Return an async dispatcher that always reports an unmet ``val_loss``.

    The constant positive value keeps the criterion ``unmet`` on every
    iteration, so the run can only end via the iteration cap. The tool name
    and args are ignored — the engine already gated the call against the
    allowlist before this callable runs.
    """

    async def dispatcher(tool_name: str, args: dict, ctx: Any) -> dict[str, Any]:
        del tool_name, args, ctx
        return {"metrics": {"val_loss": 0.5}}

    return dispatcher


async def _drive_to_terminal_verdict(
    backend: FilesystemBackend,
    session: dict[str, Any],
) -> dict[str, Any]:
    """Persist ``session``, run iterations until a terminal verdict, return it.

    Returns the reloaded session once the engine has written the durable exit
    artifact, so a caller can read back the persisted report.
    """
    backend.save_session(session)
    engine = MissionEngine(
        backend=backend,
        tool_dispatcher=_unmet_metric_dispatcher(),
        sampling_callable=None,
        sandbox_runner=None,
    )

    for _ in range(_REPORT_DRIVER_LOOP_BOUND):
        record = await engine.run_iteration(session["session_id"])
        if record["verdict"] in ("complete", "terminate"):
            break
    else:  # pragma: no cover - safety bound
        raise AssertionError(
            "session did not reach a terminal verdict within the driver loop bound"
        )

    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    return persisted


async def test_all_tools_session_report_snapshots_the_persisted_allowlist(
    tmp_path: Path,
) -> None:
    """The exit report snapshots an all-tools session's allowlist verbatim.

    A session whose allowlist came from the all-tools expansion is driven to a
    terminal verdict through the unchanged engine and report writer. The report
    written at termination carries a ``tool_allowlist`` that is byte-for-byte
    the concrete enumerated list persisted on the session — the expanded names
    with the control tool removed — so an operator auditing the report sees
    exactly which tools were in scope, regardless of how the list was produced.
    """
    resolved_allowlist = _resolve_all_tools_allowlist()

    # The expansion keeps the ordinary tools and drops the control tool.
    assert resolved_allowlist == sorted(_ALLOWED_TOOLS)
    assert _CONTROL_TOOL not in resolved_allowlist

    backend = FilesystemBackend(root=tmp_path / "report")
    session = _make_terminal_session(
        session_id="sess-report-all-tools",
        tool_allowlist=list(resolved_allowlist),
    )
    persisted = await _drive_to_terminal_verdict(backend, session)

    # The run ended on the iteration cap, and the durable report landed.
    assert persisted["final_verdict"] == "terminate"
    report_path = Path(persisted["final_report_path"])
    assert report_path.exists()

    report = json.loads(report_path.read_text(encoding="utf-8"))

    # The report snapshots exactly the persisted resolved allowlist.
    assert report["tool_allowlist"] == list(resolved_allowlist)
    assert report["tool_allowlist"] == persisted["tool_allowlist"]


# ---------------------------------------------------------------------------
# Strategy_Revision sampling-prompt fidelity
# ---------------------------------------------------------------------------

# A docstring for each kept tool, so the rendered prompt can be checked for the
# name/docstring pairing the advisory model relies on. Keyed by tool name.
_TOOL_DOCSTRINGS: dict[str, str] = {
    "find_docs": "Search the documentation catalog.",
    "find_examples": "Search the example catalog.",
    "list_jobs": "List the jobs currently known to the orchestrator.",
}


def test_all_tools_session_sampling_prompt_pairs_names_with_docstrings(
    tmp_path: Path,
) -> None:
    """An all-tools session renders each tool with its docstring like a typed list.

    The advisory prompt builder pairs every name on a session's allowlist with
    that tool's docstring. This drives the builder with an allowlist produced by
    the all-tools expansion and again with the same names typed out by hand, and
    confirms both render the identical set of name/docstring entries: every kept
    tool appears exactly once with its own docstring, the dropped control tool
    appears nowhere, and the two renderings match entry-for-entry. The prompt
    builder cannot tell how the list was produced.
    """
    del tmp_path  # The prompt builder is pure; no backend is needed here.

    resolved_allowlist = _resolve_all_tools_allowlist()

    # The expansion keeps the ordinary tools and drops the control tool.
    assert resolved_allowlist == sorted(_ALLOWED_TOOLS)
    assert _CONTROL_TOOL not in resolved_allowlist

    # Render the allowlist that came from the all-tools expansion.
    all_tools_rendered = _render_tool_allowlist(resolved_allowlist, _TOOL_DOCSTRINGS)

    # Render the same names typed out explicitly by hand.
    explicit_rendered = _render_tool_allowlist(sorted(_ALLOWED_TOOLS), _TOOL_DOCSTRINGS)

    # Every kept tool renders exactly once, paired with its own docstring.
    expected_entries = [
        {"tool_name": name, "docstring": _TOOL_DOCSTRINGS[name]} for name in sorted(_ALLOWED_TOOLS)
    ]
    assert all_tools_rendered == expected_entries

    # The dropped control tool never surfaces in the rendered prompt.
    rendered_names = [entry["tool_name"] for entry in all_tools_rendered]
    assert _CONTROL_TOOL not in rendered_names

    # The explicit-list rendering is identical — name/docstring pairing does not
    # depend on how the allowlist was produced.
    assert all_tools_rendered == explicit_rendered
