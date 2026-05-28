"""Mission goal-directed iteration loop CLI commands.

The whole subcommand group is gated by ``GCO_ENABLE_MISSION``: when
the env var is unset, the group prints a one-line hint and exits with
code 2 before dispatching to any subcommand. With the flag set, the
nine subcommands talk directly to the persistence backend and the
:class:`mission.engine.MissionEngine` — no MCP round-trip is involved
so the CLI works without the MCP server running.

Subcommands:

* ``start`` — validate inputs, resolve sampling state, persist a new
  ``SessionState``. With ``--run``, iterate to completion synchronously.
* ``status`` — read the full session JSON.
* ``iterate`` — drive one or more iterations of an existing session.
* ``checkpoint`` — re-run the verdict cascade on the latest iteration.
* ``complete`` — force a session into ``completed``.
* ``abort`` — pause or terminate a session.
* ``resume`` — transition ``paused`` to ``running``.
* ``history`` — return the iteration history (full or summary).
* ``list`` — list sessions across the configured backend.

Output formats: every subcommand defaults to ``--output json``; pass
``--output table`` for a human-readable summary.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import click

# The Mission package lives under ``mcp/mission/`` and is imported as
# ``mission.*``. Match the path-injection pattern used throughout the
# MCP module surface and the ``test_mission_*`` test files so the
# imports below resolve regardless of how this module is loaded.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "mcp"))


if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from mission.types import SessionState


_FEATURE_FLAG_HINT = (
    "Mission tools are gated. Set GCO_ENABLE_MISSION=true (or GCO_ENABLE_ALL_TOOLS=true) to enable."
)


def _flag_enabled() -> bool:
    """Return True iff ``GCO_ENABLE_MISSION`` (or umbrella) is truthy."""
    truthy = {"true", "1", "yes", "on"}
    return (
        os.environ.get("GCO_ENABLE_MISSION", "").strip().lower() in truthy
        or os.environ.get("GCO_ENABLE_ALL_TOOLS", "").strip().lower() in truthy
    )


def _check_feature_flag() -> None:
    """Print the hint and exit with code 2 when the gating flag is unset."""
    if not _flag_enabled():
        click.echo(_FEATURE_FLAG_HINT, err=True)
        raise SystemExit(2)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _strip_private_criteria(session: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe copy of ``session`` with private criterion keys dropped.

    The validators cache an ``ast.Expression`` on each ``predicate``
    criterion under the private key ``_parsed_ast``. That object is not
    JSON-serialisable, so strip every leading-underscore key from each
    Criterion dict before output. Iteration history is similarly
    cleaned: each ``criteria_evaluation`` entry gets the same treatment.
    """
    cleaned = dict(session)
    criteria = cleaned.get("criteria")
    if isinstance(criteria, list):
        cleaned["criteria"] = [
            {k: v for k, v in c.items() if not str(k).startswith("_")} if isinstance(c, dict) else c
            for c in criteria
        ]
    iterations = cleaned.get("iterations")
    if isinstance(iterations, list):
        cleaned["iterations"] = [_strip_iteration(it) for it in iterations]
    return cleaned


def _strip_iteration(iteration: Any) -> Any:
    """Strip private keys from an iteration's ``criteria_evaluation`` shape."""
    if not isinstance(iteration, dict):
        return iteration
    copy = dict(iteration)
    evals = copy.get("criteria_evaluation")
    if isinstance(evals, list):
        copy["criteria_evaluation"] = [
            {k: v for k, v in e.items() if not str(k).startswith("_")} if isinstance(e, dict) else e
            for e in evals
        ]
    return copy


def _emit_json(payload: Any, *, err: bool = False) -> None:
    """Emit ``payload`` as a single JSON line.

    ``default=str`` keeps any straggling datetime / Path objects from
    raising — the engine's persisted shapes are already pure JSON, but
    a CLI command may surface a partially-built dict (e.g., the start
    summary before save) and we want every output path to succeed.
    """
    click.echo(json.dumps(payload, default=str), err=err)


def _emit_error(code: str, details: dict[str, Any] | None = None) -> None:
    """Emit a structured error envelope to stderr."""
    payload: dict[str, Any] = {"code": code}
    if details is not None:
        payload["details"] = details
    _emit_json(payload, err=True)


# ---------------------------------------------------------------------------
# Stub dispatcher
# ---------------------------------------------------------------------------


def _make_stub_dispatcher() -> Any:
    """Return a tool dispatcher that returns canned responses.

    The CLI does not have access to the live FastMCP tool registry, so
    a real dispatcher would require booting the MCP server in-process.
    For ``--run`` and ``iterate`` modes we substitute a stub that
    returns ``{"_status": "ok", "_stub": True, ...}`` for every call.
    This keeps the CLI useful for smoke-testing the loop's bookkeeping
    without dragging the MCP transport in; real tool execution still
    happens through the MCP tools.
    """

    async def _dispatch(tool_name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return {
            "_status": "ok",
            "_stub": True,
            "tool_name": tool_name,
            "args": dict(args),
        }

    return _dispatch


# ---------------------------------------------------------------------------
# Click group
# ---------------------------------------------------------------------------


@click.group("mission")
def mission_cmd() -> None:
    """Mission goal-directed iteration loop commands.

    Subcommands manage Mission sessions: ``start``, ``status``,
    ``iterate``, ``checkpoint``, ``complete``, ``abort``, ``resume``,
    ``history``, ``list``.

    Gated by the ``GCO_ENABLE_MISSION`` environment variable. With
    the flag unset, every subcommand prints a one-line hint to stderr
    and exits with code 2.
    """
    _check_feature_flag()


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


@mission_cmd.command("start")
@click.option("--directive", required=True, help="Natural-language goal description.")
@click.option(
    "--criteria-file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="JSON file containing the criteria list. Required unless --with-defaults is set.",
)
@click.option(
    "--max-iterations",
    type=int,
    required=True,
    help="Hard cap on the number of iterations.",
)
@click.option(
    "--max-wall-clock",
    type=int,
    required=True,
    help="Hard cap on wall-clock seconds.",
)
@click.option(
    "--max-cost",
    type=float,
    default=None,
    help="Hard cap on USD cost. Required when the allowlist contains a cost-incurring tool.",
)
@click.option(
    "--tool-allowlist",
    multiple=True,
    required=True,
    help="Tool name to allowlist; pass multiple times for multiple tools.",
)
@click.option(
    "--cadence",
    type=click.Choice(["every_iteration", "every_n_iterations", "every_t_seconds", "on_event"]),
    default="every_iteration",
    show_default=True,
    help="Checkpoint cadence kind.",
)
@click.option("--cadence-n", type=int, default=None, help="Cadence n parameter.")
@click.option(
    "--cadence-t",
    type=int,
    default=None,
    help="Cadence t parameter (seconds).",
)
@click.option(
    "--cadence-event",
    default=None,
    help="Cadence event_name parameter.",
)
@click.option(
    "--stagnation-threshold",
    type=int,
    default=3,
    show_default=True,
    help="Iterations of no progress before terminate.",
)
@click.option(
    "--use-sampling/--no-sampling",
    "use_sampling",
    default=None,
    help="Enable/disable LLM sampling (default: auto-detect).",
)
@click.option(
    "--bedrock-model-id",
    default=None,
    help="Override the Bedrock model id used by the CLI sampling backend.",
)
@click.option(
    "--allow-scripted-strategies",
    is_flag=True,
    help="Allow scripted strategies to run via the Mission sandbox.",
)
@click.option(
    "--with-defaults",
    is_flag=True,
    help="Use a basic placeholder predicate criterion when no --criteria-file is provided.",
)
@click.option(
    "--run",
    "run_mode",
    is_flag=True,
    help="Iterate to completion synchronously after creating the session.",
)
@click.option(
    "--output",
    type=click.Choice(["json", "table"]),
    default="json",
    show_default=True,
    help="Output format.",
)
def mission_start(
    directive: str,
    criteria_file: str | None,
    max_iterations: int,
    max_wall_clock: int,
    max_cost: float | None,
    tool_allowlist: tuple[str, ...],
    cadence: str,
    cadence_n: int | None,
    cadence_t: int | None,
    cadence_event: str | None,
    stagnation_threshold: int,
    use_sampling: bool | None,
    bedrock_model_id: str | None,
    allow_scripted_strategies: bool,
    with_defaults: bool,
    run_mode: bool,
    output: str,
) -> None:
    """Start a new Mission session.

    Validates inputs through the shared validators in
    ``mission.validation``, resolves the sampling state via
    ``mission.sampling.resolve_sampling_state``, and persists the
    session through the configured backend (``GCO_MISSION_STATE_BACKEND``,
    defaults to filesystem under ``~/.gco/missions``).

    With ``--run``, iterates to completion synchronously: each verdict
    is printed as one JSON line to stderr; the final stdout is the
    Final_Report JSON.
    """
    from mission import (  # noqa: PLC0415 — lazy: avoids cost when help-only
        sampling as mission_sampling,
    )
    from mission import (
        state as mission_state,
    )
    from mission import (
        validation as mission_validation,
    )
    from mission.types import SCHEMA_VERSION
    from mission.validation import MissionValidationError

    # Build the criteria list from the file or the placeholder default.
    criteria: list[dict[str, Any]]
    if criteria_file:
        try:
            with open(criteria_file, encoding="utf-8") as fp:
                criteria = json.load(fp)
        except (OSError, ValueError) as exc:
            _emit_error(
                "validation_error",
                {"field": "criteria-file", "reason": str(exc)},
            )
            sys.exit(1)
    elif with_defaults:
        criteria = [
            {
                "criterion_id": "default",
                "kind": "predicate",
                "required": True,
                "expression": "True",
            }
        ]
    else:
        _emit_error(
            "validation_error",
            {
                "field": "criteria",
                "reason": "either --criteria-file or --with-defaults is required",
            },
        )
        sys.exit(1)

    # Build the budget dict.
    budget: dict[str, Any] = {
        "max_iterations": max_iterations,
        "max_wall_clock_seconds": max_wall_clock,
    }
    if max_cost is not None:
        budget["max_cost_usd"] = max_cost

    # Build the cadence dict.
    cadence_dict: dict[str, Any] = {"kind": cadence}
    if cadence_n is not None:
        cadence_dict["n"] = cadence_n
    if cadence_t is not None:
        cadence_dict["t"] = cadence_t
    if cadence_event is not None:
        cadence_dict["event_name"] = cadence_event

    # Validate inputs. The CLI has no live FastMCP tool registry, so the
    # tool-allowlist validator is skipped and the budget validator gets
    # an empty tag map — meaning a CLI-started session with a cost-
    # incurring tool will only be caught at iterate time when the engine
    # routes through the real tool dispatcher. The MCP tool surface
    # performs the full validation; the CLI is intentionally a thin
    # smoke-test path.
    try:
        directive_clean = mission_validation.validate_directive(directive)
        criteria_clean = mission_validation.validate_criteria(criteria)
        budget_clean = mission_validation.validate_budget(budget, list(tool_allowlist), {})
        cadence_clean = mission_validation.validate_cadence(cadence_dict)
    except MissionValidationError as exc:
        _emit_error(exc.code, exc.details)
        sys.exit(1)

    if not isinstance(stagnation_threshold, int) or stagnation_threshold <= 0:
        _emit_error(
            "validation_error",
            {"field": "stagnation-threshold", "reason": "must_be_positive_int"},
        )
        sys.exit(1)

    # Resolve sampling state. ``ctx=None`` because this is the CLI path;
    # the helper's third precedence branch then probes local AWS
    # credentials and returns ``("bedrock", True)`` when they resolve.
    use_sampling_resolved, backend_resolved = mission_sampling.resolve_sampling_state(
        None, use_sampling
    )

    session_id = f"mission-{secrets.token_hex(8)}"
    now_iso = datetime.now(UTC).isoformat()
    session: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "session_id": session_id,
        "directive_text": directive_clean,
        "criteria": criteria_clean,
        "budget": budget_clean,
        "tool_allowlist": list(tool_allowlist),
        "checkpoint_cadence": cadence_clean,
        "stagnation_threshold": stagnation_threshold,
        "use_sampling": use_sampling_resolved,
        "sampling_backend_resolved": backend_resolved,
        "allow_scripted_strategies": bool(allow_scripted_strategies),
        "status": "pending",
        "created_at": now_iso,
        "iterations": [],
        "no_progress_counter": 0,
        "accumulated_cost_usd": 0.0,
    }
    if bedrock_model_id:
        session["bedrock_model_id"] = bedrock_model_id

    backend = mission_state.get_backend()

    # ``save_session`` will not accept the cached ``_parsed_ast`` AST on
    # predicate criteria when the backend is the filesystem JSON writer.
    # Strip them just before persistence; the validators left them on
    # the in-memory copy so the engine can use them at iterate time —
    # we'll re-validate when iterate next runs against the loaded
    # session.
    backend.save_session(cast("SessionState", _strip_private_criteria(session)))

    summary = {
        "session_id": session_id,
        "status": "pending",
        "use_sampling": use_sampling_resolved,
        "sampling_backend_resolved": backend_resolved,
    }

    if not run_mode:
        if output == "table":
            click.echo(f"Session ID:    {session_id}")
            click.echo("Status:        pending")
            click.echo(
                f"Sampling:      {'on' if use_sampling_resolved else 'off'} ({backend_resolved})"
            )
        else:
            _emit_json(summary)
        return

    # --run mode: iterate to completion.
    _run_to_completion(session_id)


def _run_to_completion(session_id: str) -> None:
    """Drive ``session_id`` through iterations until terminal verdict.

    Writes one JSON line per iteration's verdict to stderr; the final
    stdout is the Final_Report JSON when present, falling back to the
    persisted session JSON otherwise.
    """
    from mission import state as mission_state  # noqa: PLC0415
    from mission.engine import MissionEngine, MissionEngineError  # noqa: PLC0415
    from mission.state import FilesystemBackend  # noqa: PLC0415

    backend = mission_state.get_backend()

    async def _drive() -> None:
        engine = MissionEngine(
            backend=backend,
            tool_dispatcher=_make_stub_dispatcher(),
            sampling_callable=None,
            sandbox_runner=None,
            cost_estimators={},
        )
        while True:
            try:
                record = await engine.run_iteration(session_id, ctx=None)
            except MissionEngineError as exc:
                _emit_error(exc.code, {"session_id": session_id})
                sys.exit(1)
            _emit_json(
                {
                    "iteration_index": record["iteration_index"],
                    "verdict": record["verdict"],
                    "verdict_reason": record["verdict_reason"],
                },
                err=True,
            )
            if record["verdict"] in ("complete", "terminate"):
                break

    asyncio.run(_drive())

    # Emit the final report when the filesystem backend wrote one;
    # fall back to the persisted session for other backends.
    session = backend.load_session(session_id)
    if isinstance(backend, FilesystemBackend):
        report_path = backend.root / f"{session_id}.report.json"
        if report_path.exists():
            click.echo(report_path.read_text(encoding="utf-8"))
            return
    if session is not None:
        _emit_json(_strip_private_criteria(session))
    else:
        _emit_error("session_disappeared", {"session_id": session_id})
        sys.exit(1)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@mission_cmd.command("status")
@click.argument("session_id")
@click.option(
    "--output",
    type=click.Choice(["json", "table"]),
    default="json",
    show_default=True,
)
def mission_status_cmd(session_id: str, output: str) -> None:
    """Get the full state of a Mission session."""
    from mission.state import get_backend  # noqa: PLC0415

    backend = get_backend()
    session = backend.load_session(session_id)
    if session is None:
        _emit_error("session_not_found", {"session_id": session_id})
        sys.exit(1)
    cleaned = _strip_private_criteria(session)
    if output == "table":
        click.echo(f"Session ID:    {cleaned.get('session_id', '')}")
        click.echo(f"Status:        {cleaned.get('status', '')}")
        click.echo(f"Directive:     {cleaned.get('directive_text', '')}")
        click.echo(f"Iterations:    {len(cleaned.get('iterations', []) or [])}")
        allowlist = cleaned.get("tool_allowlist", []) or []
        click.echo(f"Allowlist:     {', '.join(allowlist)}")
        click.echo(
            f"Sampling:      {'on' if cleaned.get('use_sampling') else 'off'} "
            f"({cleaned.get('sampling_backend_resolved', 'none')})"
        )
    else:
        _emit_json(cleaned)


# ---------------------------------------------------------------------------
# iterate
# ---------------------------------------------------------------------------


@mission_cmd.command("iterate")
@click.argument("session_id")
@click.option(
    "--max-iterations",
    type=int,
    default=1,
    show_default=True,
    help="How many iterations to run in this call.",
)
@click.option(
    "--output",
    type=click.Choice(["json", "table"]),
    default="json",
    show_default=True,
)
def mission_iterate_cmd(session_id: str, max_iterations: int, output: str) -> None:
    """Run one or more iterations on a Mission session.

    Stops early on a terminal verdict. The engine is wired with the CLI
    stub dispatcher; real tool execution requires the MCP server.
    """
    from mission.engine import MissionEngine, MissionEngineError  # noqa: PLC0415
    from mission.state import get_backend  # noqa: PLC0415

    if max_iterations <= 0:
        _emit_error(
            "validation_error",
            {"field": "max-iterations", "reason": "must_be_positive_int"},
        )
        sys.exit(1)

    backend = get_backend()

    async def _drive() -> dict[str, Any]:
        engine = MissionEngine(
            backend=backend,
            tool_dispatcher=_make_stub_dispatcher(),
            sampling_callable=None,
            sandbox_runner=None,
            cost_estimators={},
        )
        records: list[dict[str, Any]] = []
        for _ in range(max_iterations):
            try:
                record = await engine.run_iteration(session_id, ctx=None)
            except MissionEngineError as exc:
                return {
                    "session_id": session_id,
                    "error": {"code": exc.code},
                    "iterations": records,
                }
            records.append(
                {
                    "iteration_index": record["iteration_index"],
                    "verdict": record["verdict"],
                    "verdict_reason": record["verdict_reason"],
                }
            )
            if record["verdict"] in ("complete", "terminate"):
                break
        return {"session_id": session_id, "iterations": records}

    result = asyncio.run(_drive())

    if "error" in result:
        _emit_error(result["error"]["code"], {"session_id": session_id})
        sys.exit(1)

    if output == "table":
        for it in result.get("iterations", []):
            click.echo(
                f"  Iteration {it['iteration_index']}: {it['verdict']} ({it['verdict_reason']})"
            )
    else:
        _emit_json(result)


# ---------------------------------------------------------------------------
# checkpoint
# ---------------------------------------------------------------------------


@mission_cmd.command("checkpoint")
@click.argument("session_id")
@click.option(
    "--output",
    type=click.Choice(["json", "table"]),
    default="json",
    show_default=True,
)
def mission_checkpoint_cmd(session_id: str, output: str) -> None:
    """Re-run the verdict cascade on the latest iteration of a session."""
    from mission.decide import decide_verdict  # noqa: PLC0415
    from mission.state import get_backend  # noqa: PLC0415

    backend = get_backend()
    session = backend.load_session(session_id)
    if session is None:
        _emit_error("session_not_found", {"session_id": session_id})
        sys.exit(1)
    iterations = session.get("iterations") or []
    if not iterations:
        _emit_error("no_iterations", {"session_id": session_id})
        sys.exit(1)
    latest = iterations[-1]
    verdict, reason = decide_verdict(session, latest, datetime.now(UTC))
    payload = {
        "session_id": session_id,
        "iteration_index": latest.get("iteration_index"),
        "verdict": verdict,
        "verdict_reason": reason,
    }
    if output == "table":
        click.echo(f"Iteration {payload['iteration_index']}: {verdict} ({reason})")
    else:
        _emit_json(payload)


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------


@mission_cmd.command("complete")
@click.argument("session_id")
@click.option(
    "--output",
    type=click.Choice(["json", "table"]),
    default="json",
    show_default=True,
)
def mission_complete_cmd(session_id: str, output: str) -> None:
    """Force a Mission session into ``completed`` status."""
    from mission.state import get_backend  # noqa: PLC0415
    from mission.types import TERMINAL_STATES  # noqa: PLC0415

    backend = get_backend()
    session = backend.load_session(session_id)
    if session is None:
        _emit_error("session_not_found", {"session_id": session_id})
        sys.exit(1)
    if session["status"] in TERMINAL_STATES:
        _emit_error(
            "session_terminal",
            {"session_id": session_id, "status": session["status"]},
        )
        sys.exit(1)
    now_iso = datetime.now(UTC).isoformat()
    session["status"] = "completed"
    session["final_verdict"] = "complete"
    session["ended_at"] = now_iso
    backend.save_session(cast("SessionState", _strip_private_criteria(session)))
    payload = {
        "session_id": session_id,
        "status": "completed",
        "final_verdict": "complete",
    }
    if output == "table":
        click.echo(f"Session {session_id}: completed (forced)")
    else:
        _emit_json(payload)


# ---------------------------------------------------------------------------
# abort
# ---------------------------------------------------------------------------


@mission_cmd.command("abort")
@click.argument("session_id")
@click.option("--pause", is_flag=True, help="Pause the session instead of terminating.")
@click.option(
    "--output",
    type=click.Choice(["json", "table"]),
    default="json",
    show_default=True,
)
def mission_abort_cmd(session_id: str, pause: bool, output: str) -> None:
    """Pause or terminate a Mission session.

    With ``--pause``, transitions the session to ``paused`` (resumable).
    Without ``--pause``, transitions to ``terminated`` and stamps the
    final verdict.
    """
    from mission.state import get_backend  # noqa: PLC0415
    from mission.types import TERMINAL_STATES  # noqa: PLC0415

    backend = get_backend()
    session = backend.load_session(session_id)
    if session is None:
        _emit_error("session_not_found", {"session_id": session_id})
        sys.exit(1)
    if session["status"] in TERMINAL_STATES:
        _emit_error(
            "session_terminal",
            {"session_id": session_id, "status": session["status"]},
        )
        sys.exit(1)
    if pause:
        session["status"] = "paused"
    else:
        now_iso = datetime.now(UTC).isoformat()
        session["status"] = "terminated"
        session["final_verdict"] = "terminate"
        session["ended_at"] = now_iso
    backend.save_session(cast("SessionState", _strip_private_criteria(session)))
    payload = {"session_id": session_id, "status": session["status"]}
    if output == "table":
        click.echo(f"Session {session_id}: {session['status']}")
    else:
        _emit_json(payload)


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


@mission_cmd.command("resume")
@click.argument("session_id")
@click.option(
    "--output",
    type=click.Choice(["json", "table"]),
    default="json",
    show_default=True,
)
def mission_resume_cmd(session_id: str, output: str) -> None:
    """Resume a paused Mission session."""
    from mission.state import get_backend  # noqa: PLC0415

    backend = get_backend()
    session = backend.load_session(session_id)
    if session is None:
        _emit_error("session_not_found", {"session_id": session_id})
        sys.exit(1)
    if session["status"] != "paused":
        _emit_error(
            "invalid_state",
            {"session_id": session_id, "status": session["status"]},
        )
        sys.exit(1)
    session["status"] = "running"
    backend.save_session(cast("SessionState", _strip_private_criteria(session)))
    payload = {"session_id": session_id, "status": "running"}
    if output == "table":
        click.echo(f"Session {session_id}: running")
    else:
        _emit_json(payload)


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


@mission_cmd.command("history")
@click.argument("session_id")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["full", "summary"]),
    default="summary",
    show_default=True,
    help="Iteration history detail level.",
)
@click.option(
    "--output",
    type=click.Choice(["json", "table"]),
    default="json",
    show_default=True,
)
def mission_history_cmd(session_id: str, fmt: str, output: str) -> None:
    """Get the iteration history of a Mission session."""
    from mission.state import get_backend  # noqa: PLC0415

    backend = get_backend()
    session = backend.load_session(session_id)
    if session is None:
        _emit_error("session_not_found", {"session_id": session_id})
        sys.exit(1)
    iterations = session.get("iterations") or []

    if fmt == "full":
        cleaned = [_strip_iteration(it) for it in iterations]
        if output == "table":
            for it in cleaned:
                click.echo(
                    f"  Iteration {it.get('iteration_index')}: "
                    f"{it.get('verdict')} ({it.get('verdict_reason')})"
                )
        else:
            _emit_json({"session_id": session_id, "iterations": cleaned})
        return

    summaries = [
        {
            "iteration_index": it.get("iteration_index"),
            "verdict": it.get("verdict"),
            "verdict_reason": it.get("verdict_reason"),
            "started_at": it.get("started_at"),
            "ended_at": it.get("ended_at"),
            "checkpoint_evaluated": it.get("checkpoint_evaluated", False),
        }
        for it in iterations
    ]
    if output == "table":
        for s in summaries:
            click.echo(
                f"  Iteration {s['iteration_index']}: {s['verdict']} ({s['verdict_reason']})"
            )
    else:
        _emit_json({"session_id": session_id, "iterations": summaries})


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@mission_cmd.command("list")
@click.option(
    "--status",
    default=None,
    help="Filter sessions by status (pending, running, paused, ...).",
)
@click.option(
    "--output",
    type=click.Choice(["json", "table"]),
    default="json",
    show_default=True,
)
def mission_list_cmd(status: str | None, output: str) -> None:
    """List Mission sessions."""
    from mission.state import get_backend  # noqa: PLC0415

    backend = get_backend()
    filter_dict = {"status": status} if status else None
    sessions = backend.list_sessions(filter_dict)

    if output == "table":
        header = f"  {'SESSION ID':<40}  {'STATUS':<11}  {'ITER':>5}  CREATED"
        click.echo(header)
        click.echo("  " + "-" * (len(header) - 2))
        for s in sessions:
            sid = (s.get("session_id") or "")[:40]
            st = (s.get("status") or "")[:11]
            it = s.get("iteration_count", 0)
            ca = (s.get("created_at") or "")[:19]
            click.echo(f"  {sid:<40}  {st:<11}  {it:>5}  {ca}")
    else:
        _emit_json({"sessions": sessions})
