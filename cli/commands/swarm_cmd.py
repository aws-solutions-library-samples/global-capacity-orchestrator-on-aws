"""``gco swarm`` — supervise a fleet of child Mission sessions.

One orchestrator Mission session spawns and drives concurrent child
Mission sessions through in-process supervisor tools, under hard rails
(fleet cap, pooled child-iteration budget, concurrency bound, finite
child budgets), until the orchestrator's deterministic verdict cascade
reaches a terminal verdict. See ``docs/SWARM.md`` for the model.

The whole subcommand group is gated by ``GCO_ENABLE_SWARM``: when the
env var is unset, the group prints a one-line hint and exits with code
2 before dispatching to any subcommand. With the flag set, the
subcommands talk directly to the persistence backend, the swarm rules
in ``mission/swarm.py``, and the child runner — no MCP round-trip is
involved, so the CLI works without the MCP server running.

Subcommands:

* ``run`` — scaffold a plan from a directive, start a swarm, prime the
  fleet, and drive it to completion synchronously.
* ``start`` — validate inputs and persist a new orchestrator session.
* ``iterate`` — drive (or resume) an existing swarm's fleet.
* ``status`` — the one-call fleet rollup document.
* ``abort`` — terminate the orchestrator and abort every live child.
* ``list`` — list orchestrator sessions.
* ``scaffold-plan`` — draft a validated Swarm_Plan without starting.

Output formats: every subcommand defaults to ``--output json``; pass
``--output table`` for a human-readable summary where offered.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

# The Mission package lives under ``gco_mcp/mission/`` and is imported as
# ``mission.*``. Match the path-injection pattern used throughout the
# MCP module surface so the imports below resolve regardless of how this
# module is loaded.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "gco_mcp"))

from gco.bedrock import BedrockFTUFormNotAcceptedError  # noqa: E402

_FEATURE_FLAG_HINT = (
    "Swarm tools are gated. Set GCO_ENABLE_SWARM=true (or GCO_ENABLE_ALL_TOOLS=true) to enable."
)


def _flag_enabled() -> bool:
    """Return True iff ``GCO_ENABLE_SWARM`` (or umbrella) is truthy."""
    truthy = {"true", "1", "yes", "on"}
    return (
        os.environ.get("GCO_ENABLE_SWARM", "").strip().lower() in truthy
        or os.environ.get("GCO_ENABLE_ALL_TOOLS", "").strip().lower() in truthy
    )


def _check_feature_flag() -> None:
    """Print the hint and exit with code 2 when the gating flag is unset."""
    if not _flag_enabled():
        click.echo(_FEATURE_FLAG_HINT, err=True)
        raise SystemExit(2)


def _emit_json(payload: Any, *, err: bool = False) -> None:
    """Emit ``payload`` as a single JSON line."""
    click.echo(json.dumps(payload, default=str), err=err)


def _emit_error(code: str, details: dict[str, Any] | None = None) -> None:
    """Emit a structured error envelope to stderr."""
    payload: dict[str, Any] = {"code": code}
    if details is not None:
        payload["details"] = details
    _emit_json(payload, err=True)


# ---------------------------------------------------------------------------
# Registry and engine wiring
# ---------------------------------------------------------------------------


def _ensure_tool_registry() -> None:
    """Register every MCP tool against the shared FastMCP server, once.

    Same on-demand registration the mission CLI performs: the live tool
    dispatcher and the spawn validators look tools up on the shared
    FastMCP instance, which starts empty in a plain CLI process.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "gco_mcp"))
    from tools import register_all_tools  # noqa: PLC0415

    register_all_tools()


def _resolve_registered_tools_for_cli() -> tuple[dict[str, Any], dict[str, set[str]]]:
    """Snapshot the live registry as ``(name -> Tool, name -> tags)``."""
    _ensure_tool_registry()
    from server import mcp  # noqa: PLC0415 — lazy

    async def _list() -> list[Any]:
        return list(await mcp._list_tools())

    tools = asyncio.run(_list())
    registered = {t.name: t for t in tools}
    tags = {t.name: set(getattr(t, "tags", None) or ()) for t in tools}
    return registered, tags


def _tool_docstrings(registered: dict[str, Any]) -> dict[str, str]:
    return {name: str(getattr(tool, "description", "") or "") for name, tool in registered.items()}


def _deps_builder(*, dry_run: bool) -> Any:
    """The runner's per-session engine dependency factory for the CLI path.

    Orchestrator sessions get the supervisor-tool catalog stubs merged
    into their sampler metadata (spawn proposals then validate against
    the catalog); children build plain engines. ``--dry-run`` swaps in
    the canned-stub dispatcher, mirroring ``gco mission run``.
    """
    from mission._engine_factory import build_engine_dependencies  # noqa: PLC0415
    from mission.swarm import (  # noqa: PLC0415
        SUPERVISOR_TOOL_DOCSTRINGS,
        SUPERVISOR_TOOL_SCHEMAS,
        SUPERVISOR_TOOLS,
    )

    class _SchemaShim:
        def __init__(self, schema: dict[str, Any]) -> None:
            self._schema = schema

        def model_json_schema(self) -> dict[str, Any]:
            return self._schema

    class _SupervisorToolStub:
        def __init__(self, name: str) -> None:
            self.name = name
            self.description = SUPERVISOR_TOOL_DOCSTRINGS[name]
            self.tags = {"swarm", "supervisor"}
            self.input_schema = _SchemaShim(SUPERVISOR_TOOL_SCHEMAS[name])

    async def build(session: Any) -> Any:
        extra = None
        if session.get("role") == "orchestrator":
            extra = (
                {name: _SupervisorToolStub(name) for name in SUPERVISOR_TOOLS},
                dict(SUPERVISOR_TOOL_DOCSTRINGS),
            )
        return await build_engine_dependencies(
            session, None, use_stub_dispatcher=dry_run, extra_tool_metadata=extra
        )

    return build


def _make_runner(orchestrator_id: str, *, dry_run: bool) -> Any:
    from mission.state import get_backend  # noqa: PLC0415
    from mission.swarm_runner import SwarmRunner  # noqa: PLC0415

    registered, tags = _resolve_registered_tools_for_cli()

    def _stream_verdict(record: Any) -> None:
        _emit_json(
            {
                "event": "swarm.iteration",
                "iteration_index": record.get("iteration_index"),
                "verdict": record.get("verdict"),
                "verdict_reason": record.get("verdict_reason"),
            },
            err=True,
        )

    return SwarmRunner(
        backend=get_backend(),
        orchestrator_id=orchestrator_id,
        deps_builder=_deps_builder(dry_run=dry_run),
        registered_tools=registered,
        registered_tags=tags,
        on_orchestrator_iteration=_stream_verdict,
    )


def _persist_orchestrator(
    *,
    directive: str,
    criteria: list[dict[str, Any]],
    budget: dict[str, Any],
    swarm_config: dict[str, Any],
    tool_allowlist: tuple[str, ...],
    allow_all_tools: bool,
    stagnation_threshold: int,
    use_sampling: bool | None,
) -> dict[str, Any]:
    """Validate inputs and persist a new orchestrator session.

    Exits 1 with the structured envelope on any validation failure —
    before anything is persisted.
    """
    from mission import sampling as mission_sampling  # noqa: PLC0415
    from mission import swarm as swarm_rules  # noqa: PLC0415
    from mission import validation as mission_validation  # noqa: PLC0415
    from mission.state import get_backend  # noqa: PLC0415
    from mission.validation import MissionValidationError  # noqa: PLC0415

    try:
        directive_clean = mission_validation.validate_directive(directive)
        criteria_clean = mission_validation.validate_criteria(criteria)
        swarm_clean = swarm_rules.validate_swarm_config(swarm_config)
        budget_clean = mission_validation.validate_budget(budget, [], {})
        cadence_clean = mission_validation.validate_cadence({"kind": "every_iteration"})
        extra: list[str] = []
        if allow_all_tools or tool_allowlist:
            registered, _tags = _resolve_registered_tools_for_cli()
            extra = mission_validation.resolve_effective_allowlist(
                allow_all_tools=allow_all_tools,
                explicit_allowlist=list(tool_allowlist),
                registered_tools=registered,
            )
    except MissionValidationError as exc:
        _emit_error(exc.code, exc.details)
        raise SystemExit(1) from exc

    use_resolved, backend_resolved = mission_sampling.resolve_sampling_state(use_sampling)
    session = swarm_rules.build_orchestrator_session(
        session_id=f"mission-{secrets.token_hex(8)}",
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
    get_backend().save_session(session)  # type: ignore[arg-type]
    return session


def _scaffold_plan(
    *,
    directive: str,
    swarm_config: dict[str, Any],
    tool_allowlist: tuple[str, ...],
    allow_all_tools: bool,
    max_children: int | None,
    use_sampling: bool | None,
    retries: int,
) -> dict[str, Any]:
    """Produce a validated Swarm_Plan; sampled when a backend resolves.

    Returns ``{"plan": [...], "sampling_path": bool, "fallback_reason"}``.
    Exits 1 on config/validation failures. A sampling failure falls back
    to the deterministic single-worker plan with a one-line warning,
    except the permanent Anthropic first-time-use gate, which is
    reported as a hard error (Mission precedent).
    """
    from mission import sampling as mission_sampling  # noqa: PLC0415
    from mission import swarm as swarm_rules  # noqa: PLC0415
    from mission import swarm_scaffold  # noqa: PLC0415
    from mission.validation import MissionValidationError  # noqa: PLC0415

    try:
        config = swarm_rules.validate_swarm_config(swarm_config)
    except MissionValidationError as exc:
        _emit_error(exc.code, exc.details)
        raise SystemExit(1) from exc
    registered, tags = _resolve_registered_tools_for_cli()
    use_resolved, backend_name = mission_sampling.resolve_sampling_state(use_sampling)
    plan: list[dict[str, Any]] | None = None
    fallback_reason: str | None = None
    if use_resolved:
        backend_obj = mission_sampling.select_sampling_backend(None)
        if backend_obj is not None:
            try:
                plan = asyncio.run(
                    swarm_scaffold.generate_sampled_plan(
                        backend_obj,
                        directive,
                        config=config,
                        registered_tools=registered,
                        registered_tags=tags,
                        tool_docstrings=_tool_docstrings(registered),
                        max_children=max_children,
                        tool_allowlist=(None if allow_all_tools else list(tool_allowlist) or None),
                        retries=retries,
                    )
                )
            except BedrockFTUFormNotAcceptedError as exc:
                _emit_error("bedrock_ftu_form_not_accepted", {"message": str(exc)})
                raise SystemExit(1) from exc
            except swarm_scaffold.SwarmScaffoldError as exc:
                fallback_reason = exc.last_reason
                click.echo(
                    f"Sampled plan rejected ({exc.last_reason}); "
                    "falling back to the deterministic single-worker plan.",
                    err=True,
                )
    if plan is None:
        try:
            plan = swarm_scaffold.generate_deterministic_plan(
                directive,
                config=config,
                registered_tools=registered,
                registered_tags=tags,
                tool_allowlist=list(tool_allowlist) or None,
                allow_all_tools=allow_all_tools,
            )
        except MissionValidationError as exc:
            _emit_error(exc.code, exc.details)
            raise SystemExit(1) from exc
    return {
        "plan": plan,
        "sampling_path": fallback_reason is None and use_resolved,
        "sampling_backend_resolved": backend_name,
        "fallback_reason": fallback_reason,
    }


async def _prime_and_run(
    runner: Any, plan: list[dict[str, Any]], *, max_orchestrator_iterations: int | None = None
) -> dict[str, Any]:
    """Dispatch the plan's spawns through the runner seam, then drive.

    Every spawn envelope streams to stderr; a rejected plan entry is a
    hard failure (the plan was pre-validated, so a rejection here means
    the world changed underneath it, and silently running a partial
    fleet would be dishonest).
    """
    for request in plan:
        result = await runner.spawn(request)
        _emit_json({"event": "swarm.spawn", **result}, err=True)
        if not result.get("spawned"):
            raise SystemExit(1)
    return dict(
        await runner.run_to_completion(max_orchestrator_iterations=max_orchestrator_iterations)
    )


# ---------------------------------------------------------------------------
# Click group
# ---------------------------------------------------------------------------


@click.group("swarm")
def swarm_cmd() -> None:
    """Swarm supervision: one orchestrator Mission driving child Missions.

    Gated by GCO_ENABLE_SWARM. See docs/SWARM.md for the supervisor
    model, the rails, and the determinism boundary.
    """
    _check_feature_flag()


_SWARM_OPTIONS = [
    click.option("--max-children", type=int, default=3, show_default=True),
    click.option("--child-iteration-pool", type=int, default=15, show_default=True),
    click.option("--max-concurrent-children", type=int, default=3, show_default=True),
    click.option(
        "--allow-overlapping-mutating-tools",
        is_flag=True,
        default=False,
        help="Allow two live children to share a non-read-only tool.",
    ),
]


def _swarm_options(func: Any) -> Any:
    for option in reversed(_SWARM_OPTIONS):
        func = option(func)
    return func


def _swarm_config_from_flags(
    max_children: int,
    child_iteration_pool: int,
    max_concurrent_children: int,
    allow_overlapping_mutating_tools: bool,
) -> dict[str, Any]:
    return {
        "max_children": max_children,
        "child_iteration_pool": child_iteration_pool,
        "max_concurrent_children": max_concurrent_children,
        "allow_overlapping_mutating_tools": allow_overlapping_mutating_tools,
    }


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@swarm_cmd.command("run")
@click.option("--directive", required=True, help="The swarm-level goal, natural language.")
@click.option(
    "--tool-allowlist",
    multiple=True,
    metavar="NAME",
    help="Tool allowed to scaffolded children (repeatable).",
)
@click.option("--allow-all-tools", is_flag=True, default=False)
@_swarm_options
@click.option("--max-iterations", type=int, default=25, show_default=True)
@click.option("--max-wall-clock", type=int, default=1800, show_default=True)
@click.option("--stagnation-threshold", type=int, default=3, show_default=True)
@click.option("--use-sampling/--no-sampling", "use_sampling", default=None)
@click.option("--retries", type=int, default=3, show_default=True)
@click.option(
    "--save-plan",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Also write the scaffolded plan JSON to this path.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Use the canned-stub tool dispatcher (loop mechanics only).",
)
def swarm_run_cmd(
    directive: str,
    tool_allowlist: tuple[str, ...],
    allow_all_tools: bool,
    max_children: int,
    child_iteration_pool: int,
    max_concurrent_children: int,
    allow_overlapping_mutating_tools: bool,
    max_iterations: int,
    max_wall_clock: int,
    stagnation_threshold: int,
    use_sampling: bool | None,
    retries: int,
    save_plan: str | None,
    dry_run: bool,
) -> None:
    """Scaffold a plan, start a swarm, and drive it to completion.

    Per-iteration verdicts and spawn envelopes stream to stderr as JSON
    lines; the orchestrator's Final_Report lands on stdout when the
    swarm reaches a terminal verdict.
    """
    swarm_config = _swarm_config_from_flags(
        max_children,
        child_iteration_pool,
        max_concurrent_children,
        allow_overlapping_mutating_tools,
    )
    scaffold = _scaffold_plan(
        directive=directive,
        swarm_config=swarm_config,
        tool_allowlist=tool_allowlist,
        allow_all_tools=allow_all_tools,
        max_children=None,
        use_sampling=use_sampling,
        retries=retries,
    )
    plan = scaffold["plan"]
    if save_plan:
        Path(save_plan).write_text(json.dumps(plan, indent=2), encoding="utf-8")
    # The default orchestrator criterion: every planned slot completed.
    # Expressed over the fleet metrics so it is deterministic and
    # readable in the session JSON.
    criteria = [
        {
            "criterion_id": "fleet_completed",
            "kind": "metric_threshold",
            "required": True,
            "metric": "metrics.children_completed",
            "op": ">=",
            "target": len(plan),
        },
        {
            "criterion_id": "no_failed_children",
            "kind": "metric_threshold",
            "required": True,
            "metric": "metrics.children_failed",
            "op": "==",
            "target": 0,
        },
    ]
    session = _persist_orchestrator(
        directive=directive,
        criteria=criteria,
        budget={"max_iterations": max_iterations, "max_wall_clock_seconds": max_wall_clock},
        swarm_config=swarm_config,
        tool_allowlist=(),
        allow_all_tools=False,
        stagnation_threshold=stagnation_threshold,
        use_sampling=use_sampling if not dry_run else False,
    )
    _emit_json(
        {
            "event": "swarm.run.started",
            "session_id": session["session_id"],
            "plan_children": [entry["slot"] for entry in plan],
            "sampling_path": scaffold["sampling_path"],
            "fallback_reason": scaffold["fallback_reason"],
        },
        err=True,
    )
    final = _drive(session["session_id"], plan=plan, dry_run=dry_run)
    _emit_report(final)
    raise SystemExit(0 if final.get("final_verdict") == "complete" else 3)


def _drive(
    orchestrator_id: str,
    *,
    plan: list[dict[str, Any]] | None = None,
    dry_run: bool = False,
    max_orchestrator_iterations: int | None = None,
) -> dict[str, Any]:
    """Build a runner and drive the swarm, mapping runner errors to exits."""
    from mission.swarm_runner import SwarmRunnerBusyError  # noqa: PLC0415
    from mission.validation import MissionValidationError  # noqa: PLC0415

    runner = _make_runner(orchestrator_id, dry_run=dry_run)
    try:
        return asyncio.run(
            _prime_and_run(
                runner,
                plan or [],
                max_orchestrator_iterations=max_orchestrator_iterations,
            )
        )
    except SwarmRunnerBusyError as busy:
        _emit_error(
            "swarm_runner_active",
            {"session_id": orchestrator_id, "holder_pid": busy.holder_pid},
        )
        raise SystemExit(1) from busy
    except MissionValidationError as exc:
        _emit_error(exc.code, exc.details)
        raise SystemExit(1) from exc


def _emit_report(final: dict[str, Any]) -> None:
    """Print the Final_Report JSON to stdout when present, else a summary."""
    report_path = final.get("final_report_path")
    if report_path and Path(str(report_path)).exists():
        click.echo(Path(str(report_path)).read_text(encoding="utf-8"))
        return
    _emit_json(
        {
            "session_id": final.get("session_id"),
            "status": final.get("status"),
            "final_verdict": final.get("final_verdict"),
        }
    )


# ---------------------------------------------------------------------------
# start / iterate
# ---------------------------------------------------------------------------


@swarm_cmd.command("start")
@click.option("--directive", required=True)
@click.option(
    "--criteria-file",
    type=click.Path(exists=True, dir_okay=False),
    required=True,
    help="JSON array of orchestrator criteria (over the fleet metrics).",
)
@click.option("--tool-allowlist", multiple=True, metavar="NAME")
@click.option("--allow-all-tools", is_flag=True, default=False)
@_swarm_options
@click.option("--max-iterations", type=int, default=25, show_default=True)
@click.option("--max-wall-clock", type=int, default=1800, show_default=True)
@click.option("--stagnation-threshold", type=int, default=3, show_default=True)
@click.option("--use-sampling/--no-sampling", "use_sampling", default=None)
def swarm_start_cmd(
    directive: str,
    criteria_file: str,
    tool_allowlist: tuple[str, ...],
    allow_all_tools: bool,
    max_children: int,
    child_iteration_pool: int,
    max_concurrent_children: int,
    allow_overlapping_mutating_tools: bool,
    max_iterations: int,
    max_wall_clock: int,
    stagnation_threshold: int,
    use_sampling: bool | None,
) -> None:
    """Persist a new swarm (orchestrator) session without driving it."""
    try:
        criteria = json.loads(Path(criteria_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _emit_error("validation_error", {"field": "criteria_file", "reason": str(exc)})
        raise SystemExit(1) from exc
    session = _persist_orchestrator(
        directive=directive,
        criteria=criteria,
        budget={"max_iterations": max_iterations, "max_wall_clock_seconds": max_wall_clock},
        swarm_config=_swarm_config_from_flags(
            max_children,
            child_iteration_pool,
            max_concurrent_children,
            allow_overlapping_mutating_tools,
        ),
        tool_allowlist=tool_allowlist,
        allow_all_tools=allow_all_tools,
        stagnation_threshold=stagnation_threshold,
        use_sampling=use_sampling,
    )
    _emit_json(
        {
            "session_id": session["session_id"],
            "status": session["status"],
            "use_sampling": session["use_sampling"],
            "swarm": session["swarm"],
        }
    )


@swarm_cmd.command("iterate")
@click.argument("session_id")
@click.option(
    "--max-orchestrator-iterations",
    type=int,
    default=None,
    help="Detach after this many orchestrator iterations (fleet stays resumable).",
)
@click.option("--dry-run", is_flag=True, default=False)
def swarm_iterate_cmd(
    session_id: str, max_orchestrator_iterations: int | None, dry_run: bool
) -> None:
    """Drive (or resume) an existing swarm's fleet.

    Also the crash-recovery path: a fresh runner re-schedules every live
    child and evaluates restart policy for children that went terminal
    while unsupervised.
    """
    final = _drive(
        session_id,
        dry_run=dry_run,
        max_orchestrator_iterations=max_orchestrator_iterations,
    )
    _emit_json(
        {
            "session_id": session_id,
            "status": final.get("status"),
            "final_verdict": final.get("final_verdict"),
            "iterations_run": len(final.get("iterations", [])),
        }
    )


# ---------------------------------------------------------------------------
# status / abort / list / scaffold-plan
# ---------------------------------------------------------------------------


def _load_orchestrator_or_exit(session_id: str) -> tuple[Any, dict[str, Any]]:
    from mission.state import get_backend  # noqa: PLC0415

    backend = get_backend()
    session = backend.load_session(session_id)
    if session is None:
        _emit_error("session_not_found", {"session_id": session_id})
        raise SystemExit(1)
    if session.get("role") != "orchestrator" or "swarm" not in session:
        _emit_error(
            "validation_error",
            {"field": "role", "reason": "not_an_orchestrator", "session_id": session_id},
        )
        raise SystemExit(1)
    return backend, dict(session)


@swarm_cmd.command("status")
@click.argument("session_id")
@click.option("--output", type=click.Choice(["json", "table"]), default="json", show_default=True)
def swarm_status_cmd(session_id: str, output: str) -> None:
    """One-call fleet rollup: rails, pool, child table, findings."""
    from mission.swarm_runner import build_fleet_rollup  # noqa: PLC0415

    backend, session = _load_orchestrator_or_exit(session_id)
    rollup = build_fleet_rollup(backend, session)  # type: ignore[arg-type]
    if output == "json":
        _emit_json(rollup)
        return
    pool = rollup["pool"]
    click.echo(f"Swarm {rollup['session_id']}  status={rollup['status']}")
    click.echo(
        f"Pool: {pool['remaining']}/{pool['pool']} remaining "
        f"(reserved {pool['reserved']}, consumed {pool['consumed']})"
    )
    click.echo(f"Runner: {rollup['runner_state'] or 'none'}")
    click.echo("Children:")
    for row in rollup["children"]:
        verdict = row.get("final_verdict", "-")
        click.echo(
            f"  {row['slot']:<24} {row['status']:<12} verdict={verdict} "
            f"respawns={row['respawn_count']}"
        )
    for finding in rollup["findings"]:
        click.echo(f"finding: {finding}")


@swarm_cmd.command("abort")
@click.argument("session_id")
def swarm_abort_cmd(session_id: str) -> None:
    """Terminate the orchestrator and abort every non-terminal child."""
    from mission.swarm_runner import abort_swarm  # noqa: PLC0415
    from mission.types import TERMINAL_STATES  # noqa: PLC0415

    backend, session = _load_orchestrator_or_exit(session_id)
    if session["status"] in TERMINAL_STATES:
        _emit_error("session_terminal", {"session_id": session_id, "status": session["status"]})
        raise SystemExit(1)
    _emit_json(abort_swarm(backend, session))  # type: ignore[arg-type]


@swarm_cmd.command("list")
@click.option("--status", default=None, help="Filter by lifecycle status.")
def swarm_list_cmd(status: str | None) -> None:
    """List swarm (orchestrator) sessions on the configured backend."""
    from mission.state import get_backend  # noqa: PLC0415
    from mission.swarm_runner import list_swarms  # noqa: PLC0415

    _emit_json({"swarms": list_swarms(get_backend(), status=status)})


@swarm_cmd.command("scaffold-plan")
@click.option("--directive", required=True)
@click.option("--tool-allowlist", multiple=True, metavar="NAME")
@click.option("--allow-all-tools", is_flag=True, default=False)
@_swarm_options
@click.option("--max-plan-children", "max_plan_children", type=int, default=None)
@click.option("--use-sampling/--no-sampling", "use_sampling", default=None)
@click.option("--retries", type=int, default=3, show_default=True)
@click.option(
    "--output-file",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Write the plan JSON here instead of stdout.",
)
def swarm_scaffold_plan_cmd(
    directive: str,
    tool_allowlist: tuple[str, ...],
    allow_all_tools: bool,
    max_children: int,
    child_iteration_pool: int,
    max_concurrent_children: int,
    allow_overlapping_mutating_tools: bool,
    max_plan_children: int | None,
    use_sampling: bool | None,
    retries: int,
    output_file: str | None,
) -> None:
    """Draft a validated Swarm_Plan for review, without starting anything."""
    scaffold = _scaffold_plan(
        directive=directive,
        swarm_config=_swarm_config_from_flags(
            max_children,
            child_iteration_pool,
            max_concurrent_children,
            allow_overlapping_mutating_tools,
        ),
        tool_allowlist=tool_allowlist,
        allow_all_tools=allow_all_tools,
        max_children=max_plan_children,
        use_sampling=use_sampling,
        retries=retries,
    )
    if output_file:
        Path(output_file).write_text(json.dumps(scaffold["plan"], indent=2), encoding="utf-8")
        _emit_json(
            {
                "written": output_file,
                "children": len(scaffold["plan"]),
                "sampling_path": scaffold["sampling_path"],
            },
            err=True,
        )
        return
    _emit_json(scaffold)
