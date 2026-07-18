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

# The Mission package lives under ``gco_mcp/mission/`` and is imported as
# ``mission.*``. Match the path-injection pattern used throughout the
# MCP module surface and the ``test_mission_*`` test files so the
# imports below resolve regardless of how this module is loaded.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "gco_mcp"))


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

    Thin alias over :func:`mission.validation.strip_private_fields` —
    the canonical implementation lives next to ``validate_criteria``
    (which creates the ``_parsed_ast`` keys). Kept under the older
    ``_strip_private_criteria`` name so the call sites in this file
    don't churn while the underlying logic is consolidated.
    """
    from mission.validation import strip_private_fields  # noqa: PLC0415

    return cast("dict[str, Any]", strip_private_fields(session))


def _strip_iteration(iteration: Any) -> Any:
    """Strip private keys from an iteration's ``criteria_evaluation`` shape.

    Thin alias over the iteration variant of the canonical helper.
    Returns non-dict input verbatim so a corrupt history entry stays
    observable to the caller.
    """
    if not isinstance(iteration, Mapping):
        return iteration
    from mission.validation import strip_private_fields_iterations  # noqa: PLC0415

    return strip_private_fields_iterations([iteration])[0]


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

    Thin wrapper around :func:`mcp.mission._engine_factory.make_stub_dispatcher`
    kept for backward compat with the small set of tests that import
    this name directly. Production paths now go through
    :func:`_build_engine` which decides between the live FastMCP
    dispatcher and this stub based on ``--dry-run`` opt-in.
    """
    from mission._engine_factory import make_stub_dispatcher  # noqa: PLC0415

    return make_stub_dispatcher()


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
    help="Hard cap on the iteration count. Pass -1 to opt out (uncapped).",
)
@click.option(
    "--max-wall-clock",
    type=int,
    required=True,
    help="Hard cap on wall-clock seconds. Pass -1 to opt out (uncapped).",
)
@click.option(
    "--tool-allowlist",
    multiple=True,
    help="Tool name to allowlist; pass multiple times. Optional with --allow-all-tools.",
)
@click.option(
    "--allow-all-tools",
    is_flag=True,
    help=(
        "Resolve the session's tool allowlist to every registered MCP tool "
        "(minus the mission_* control tools). Makes --tool-allowlist optional; "
        "mutually exclusive with it."
    ),
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
    "--dry-run",
    "dry_run",
    is_flag=True,
    help=(
        "Use a stub tool dispatcher and disable Strategy_Revision sampling "
        "during iteration. Useful for smoke-testing the loop bookkeeping "
        "without spending Bedrock or AWS credits. Only meaningful with --run."
    ),
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
    tool_allowlist: tuple[str, ...],
    allow_all_tools: bool,
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
    dry_run: bool,
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

    # Build the cadence dict.
    cadence_dict: dict[str, Any] = {"kind": cadence}
    if cadence_n is not None:
        cadence_dict["n"] = cadence_n
    if cadence_t is not None:
        cadence_dict["t"] = cadence_t
    if cadence_event is not None:
        cadence_dict["event_name"] = cadence_event

    # Resolve the effective allowlist before any persistence. The explicit
    # path keeps the thin CLI behaviour (no live-registry per-name check); the
    # all-tools path resolves from the on-demand registry. A rejection here
    # emits a structured envelope and exits before any session is built.
    allowlist_resolved = _resolve_cli_allowlist(
        allow_all_tools=allow_all_tools, tool_allowlist=tool_allowlist
    )

    # Validate inputs. The CLI has no live FastMCP tool registry on the
    # explicit path, so the tool-allowlist validator is skipped and the
    # budget validator gets an empty tag map — meaning a CLI-started session
    # with a cost-incurring tool will only be caught at iterate time when the
    # engine routes through the real tool dispatcher. The MCP tool surface
    # performs the full validation; the CLI is intentionally a thin
    # smoke-test path.
    try:
        directive_clean = mission_validation.validate_directive(directive)
        criteria_clean = mission_validation.validate_criteria(criteria)
        budget_clean = mission_validation.validate_budget(budget, allowlist_resolved, {})
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
        "tool_allowlist": allowlist_resolved,
        "checkpoint_cadence": cadence_clean,
        "stagnation_threshold": stagnation_threshold,
        "use_sampling": use_sampling_resolved,
        "sampling_backend_resolved": backend_resolved,
        "allow_scripted_strategies": bool(allow_scripted_strategies),
        "status": "pending",
        "created_at": now_iso,
        "iterations": [],
        "no_progress_counter": 0,
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
    _run_to_completion(session_id, dry_run=dry_run)


def _run_to_completion(session_id: str, *, dry_run: bool = False) -> None:
    """Drive ``session_id`` through iterations until terminal verdict.

    When ``dry_run`` is False (the default), wires the live FastMCP
    dispatcher and the Strategy_Revision sampling callable through
    :func:`mcp.mission._engine_factory.build_mission_engine` so the
    loop can actually iterate against real tools and let the model
    revise the strategy between iterations. When ``dry_run`` is True,
    falls back to the canned-stub dispatcher and disables sampling so
    the CLI can smoke-test the loop bookkeeping without spending
    Bedrock or AWS credits.

    Writes one JSON line per iteration's verdict to stderr; the final
    stdout is the Final_Report JSON when present, falling back to the
    persisted session JSON otherwise.
    """
    from mission import state as mission_state  # noqa: PLC0415
    from mission._engine_factory import build_mission_engine  # noqa: PLC0415
    from mission.engine import MissionEngineError  # noqa: PLC0415
    from mission.state import FilesystemBackend  # noqa: PLC0415

    backend = mission_state.get_backend()
    session_for_runner = backend.load_session(session_id)
    if session_for_runner is None:
        _emit_error("session_not_found", {"session_id": session_id})
        sys.exit(1)

    # Populate the FastMCP tool registry so the live dispatcher can
    # find the operator-allowlisted tools. Safe to call repeatedly —
    # ``register_all_tools`` is idempotent (FastMCP rejects duplicate
    # registrations after the first call). Skipped on the dry-run path
    # because the stub dispatcher never consults the registry.
    if not dry_run:
        _ensure_tool_registry()

    async def _drive() -> None:
        engine = await build_mission_engine(
            session_for_runner, ctx=None, use_stub_dispatcher=dry_run
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


def _ensure_tool_registry() -> None:
    """Register every MCP tool against the shared FastMCP server, once.

    The CLI doesn't normally boot the MCP server, so its FastMCP
    instance starts empty. The live tool dispatcher in the engine
    factory looks up tools on that instance, so we eagerly register
    every tool group up-front when the live path is selected. The
    underlying ``register_all_tools`` is import-time side-effects on
    module load; calling it twice is harmless because the per-module
    decorators only fire on the first import.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "gco_mcp"))
    from tools import register_all_tools  # noqa: PLC0415

    register_all_tools()


def _resolve_registered_tools_for_cli() -> tuple[dict[str, Any], set[str]]:
    """Register every MCP tool on demand and snapshot the live registry.

    Returns a ``(name -> Tool, control-tool names)`` pair. The control set is
    derived from the ``"mission"`` tag, so it auto-adapts if a tenth
    session-management tool is ever added. Calls the idempotent
    :func:`_ensure_tool_registry` first, then lists tools through
    ``mcp._list_tools()`` — the same low-level path the engine factory uses.
    Returns ``({}, set())`` only when the registry genuinely holds no tools,
    which the resolver then rejects as ``allow_all_tools_empty_registry``.
    """
    _ensure_tool_registry()
    from server import mcp  # noqa: PLC0415 — lazy

    async def _list() -> list[Any]:
        return list(await mcp._list_tools())

    tools = asyncio.run(_list())
    registered = {t.name: t for t in tools}
    control = {t.name for t in tools if "mission" in (getattr(t, "tags", None) or set())}
    return registered, control


def _resolve_cli_allowlist(*, allow_all_tools: bool, tool_allowlist: tuple[str, ...]) -> list[str]:
    """Resolve a subcommand's effective tool allowlist or exit with code 1.

    The all-tools branch populates the registry on demand and resolves the
    effective list from it. The explicit branch preserves the thin CLI path
    (no per-name registry check) but enforces at-least-one, emitting the
    existing ``empty`` rejection when no name is supplied. On any
    :class:`MissionValidationError` the structured envelope is emitted and the
    process exits 1 — before the caller builds or persists a session.
    """
    from mission import validation as mission_validation  # noqa: PLC0415
    from mission.validation import MissionValidationError  # noqa: PLC0415

    if allow_all_tools:
        registered_tools, control_tools = _resolve_registered_tools_for_cli()
        try:
            resolved: list[str] = mission_validation.resolve_effective_allowlist(
                allow_all_tools=True,
                explicit_allowlist=list(tool_allowlist),
                registered_tools=registered_tools,
                control_tools=control_tools,
            )
        except MissionValidationError as exc:
            _emit_error(exc.code, exc.details)
            sys.exit(1)
        return resolved
    if not tool_allowlist:
        _emit_error("validation_error", {"field": "tool_allowlist", "reason": "empty"})
        sys.exit(1)
    return list(tool_allowlist)


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
    "--dry-run",
    "dry_run",
    is_flag=True,
    help=(
        "Use a stub tool dispatcher and disable Strategy_Revision sampling. "
        "Useful for smoke-testing the loop without spending Bedrock or AWS credits."
    ),
)
@click.option(
    "--output",
    type=click.Choice(["json", "table"]),
    default="json",
    show_default=True,
)
def mission_iterate_cmd(session_id: str, max_iterations: int, dry_run: bool, output: str) -> None:
    """Run one or more iterations on a Mission session.

    Stops early on a terminal verdict. By default the engine is wired
    with the live FastMCP tool dispatcher and the Strategy_Revision
    sampling callable so the loop iterates against real tool results
    and lets the model revise the strategy between iterations.

    Pass ``--dry-run`` to substitute the canned-stub dispatcher and
    disable sampling — useful for smoke-testing the bookkeeping
    without spending Bedrock or AWS credits.
    """
    from mission._engine_factory import build_mission_engine  # noqa: PLC0415
    from mission.engine import MissionEngineError  # noqa: PLC0415
    from mission.state import get_backend  # noqa: PLC0415

    if max_iterations <= 0:
        # This is the per-call iteration count (how many iterations to
        # run THIS call), NOT the session-wide ``budget.max_iterations``
        # cap. The budget cap accepts ``-1`` as the "uncapped" sentinel;
        # this per-call count must always be a positive int because a
        # zero or negative value here would be a no-op invocation.
        _emit_error(
            "validation_error",
            {"field": "max-iterations", "reason": "must_be_positive_int"},
        )
        sys.exit(1)

    backend = get_backend()
    session_for_runner = backend.load_session(session_id)
    if session_for_runner is None:
        _emit_error("session_not_found", {"session_id": session_id})
        sys.exit(1)

    if not dry_run:
        _ensure_tool_registry()

    async def _drive() -> dict[str, Any]:
        engine = await build_mission_engine(
            session_for_runner, ctx=None, use_stub_dispatcher=dry_run
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
    "--include-observations",
    "include_obs",
    is_flag=True,
    help=(
        "Include the observation and strategy dicts in each iteration's "
        "output. Only meaningful with --format full. Useful for debugging "
        "what each tool returned and what strategy was proposed."
    ),
)
@click.option(
    "--output",
    type=click.Choice(["json", "table"]),
    default="json",
    show_default=True,
)
def mission_history_cmd(session_id: str, fmt: str, include_obs: bool, output: str) -> None:
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
        if not include_obs:
            # Strip observation and strategy from the output to keep it
            # concise. Operators who need the full shape pass
            # --include-observations.
            for it in cleaned:
                if isinstance(it, dict):
                    it.pop("observation", None)
                    it.pop("strategy", None)
        if output == "table":
            for it in cleaned:
                if not isinstance(it, dict):
                    continue
                idx = it.get("iteration_index", "?")
                verdict = it.get("verdict", "?")
                reason = it.get("verdict_reason", "?")
                click.echo(f"  Iteration {idx}: {verdict} ({reason})")
                if include_obs:
                    obs = it.get("observation", {})
                    results = obs.get("tool_results", [])
                    errors = obs.get("errors", [])
                    strat = it.get("strategy", {})
                    rationale = strat.get("rationale", "")[:100]
                    calls = strat.get("tool_calls", [])
                    tool_names = [c.get("tool_name", "?") for c in calls if isinstance(c, dict)]
                    click.echo(f"    tools: {tool_names}")
                    click.echo(f"    rationale: {rationale}")
                    click.echo(f"    tool_results: {len(results)} entries, errors: {len(errors)}")
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


# ---------------------------------------------------------------------------
# scaffold-criteria
# ---------------------------------------------------------------------------


@mission_cmd.command("scaffold-criteria")
@click.option(
    "--directive",
    required=True,
    help="Natural-language goal description used to seed the criteria.",
)
@click.option(
    "--allowlist",
    "allowlist",
    multiple=True,
    help=(
        "Optional tool names that the resulting session would be "
        "configured with. Used informationally on the deterministic "
        "path; on the sampling path, shapes the prompt so the model "
        "picks metric/event names plausibly produced by the listed tools."
    ),
)
@click.option(
    "--use-sampling/--no-sampling",
    "use_sampling",
    default=None,
    help=(
        "Force the sampling path on/off. Default auto-detects: MCP "
        "host capability, then Bedrock credentials, then deterministic."
    ),
)
@click.option(
    "--bedrock-model-id",
    default=None,
    help="Override the Bedrock model id used by the CLI sampling backend.",
)
@click.option(
    "--max-criteria",
    type=int,
    default=5,
    show_default=True,
    help="Cap on the number of criterion entries scaffolded.",
)
@click.option(
    "--retries",
    type=int,
    default=3,
    show_default=True,
    help="Sampling-path retry budget on validator rejections.",
)
@click.option(
    "--output-file",
    "output_file",
    type=click.Path(dir_okay=False),
    default=None,
    help="Write the JSON to this file instead of stdout.",
)
@click.option(
    "--output",
    type=click.Choice(["json", "table"]),
    default="json",
    show_default=True,
    help="Output format (table mode prints a per-entry summary alongside the JSON).",
)
def mission_scaffold_criteria_cmd(
    directive: str,
    allowlist: tuple[str, ...],
    use_sampling: bool | None,
    bedrock_model_id: str | None,
    max_criteria: int,
    retries: int,
    output_file: str | None,
    output: str,
) -> None:
    """Scaffold a criteria.json from a natural-language directive.

    Resolves the sampling state via ``mission.sampling.resolve_sampling_state``;
    when a backend resolves and ``--use-sampling`` permits, the resolved
    backend is asked for a JSON array. The response is validated through
    ``validate_criteria`` and retried up to ``--retries`` times on
    rejection. Falls back to the deterministic keyword-template
    generator when sampling is unavailable, disabled, or after the
    retry budget is exhausted.

    The output always validates through ``validate_criteria`` so the
    resulting file is immediately usable with ``mission start
    --criteria-file``.
    """
    import mission.criteria_scaffold as criteria_scaffold  # noqa: PLC0415 — lazy: avoids cost when help-only
    from mission import (
        sampling as mission_sampling,
    )

    if max_criteria < 1:
        _emit_error(
            "validation_error",
            {"field": "max-criteria", "reason": "must_be_positive_int"},
        )
        sys.exit(1)
    if retries < 0:
        _emit_error(
            "validation_error",
            {"field": "retries", "reason": "must_be_non_negative_int"},
        )
        sys.exit(1)

    use_sampling_resolved, backend_resolved = mission_sampling.resolve_sampling_state(
        None, use_sampling
    )

    criteria: list[dict[str, Any]] | None = None
    sampling_path_taken = False
    if use_sampling_resolved and backend_resolved != "none":
        backend_obj = mission_sampling.select_sampling_backend(
            None,
            model_id=bedrock_model_id,
            prefs=None,
        )
        if backend_obj is not None:
            try:
                criteria = asyncio.run(
                    criteria_scaffold.generate_sampled_criteria(
                        backend_obj,
                        directive,
                        allowlist=list(allowlist),
                        max_criteria=max_criteria,
                        retries=retries,
                    )
                )
                sampling_path_taken = True
            except criteria_scaffold.ScaffoldSamplingError as exc:
                # The sampling path failed; emit a one-line warning to
                # stderr so the operator sees what happened, then fall
                # through to the deterministic generator.
                click.echo(
                    f"sampling path failed ({exc.last_reason}); "
                    "falling back to deterministic templates.",
                    err=True,
                )
                criteria = None

    if criteria is None:
        criteria = criteria_scaffold.generate_deterministic_criteria(
            directive,
            allowlist=list(allowlist) or None,
            max_criteria=max_criteria,
        )

    payload = json.dumps(criteria, indent=2, sort_keys=False)

    if output_file:
        Path(output_file).write_text(payload + "\n", encoding="utf-8")
        # Echo a structured summary on the chosen format so the operator
        # can see what was written without re-reading the file.
        if output == "table":
            for c in criteria:
                click.echo(
                    f"  {c.get('criterion_id'):<32}  "
                    f"kind={c.get('kind'):<16}  required={c.get('required')}"
                )
            click.echo(f"  written to {output_file}")
        else:
            _emit_json(
                {
                    "output_file": output_file,
                    "criteria_count": len(criteria),
                    "sampling_path": sampling_path_taken,
                }
            )
        return

    # No --output-file: write JSON to stdout.
    if output == "table":
        for c in criteria:
            click.echo(
                f"  {c.get('criterion_id'):<32}  "
                f"kind={c.get('kind'):<16}  required={c.get('required')}"
            )
        return
    click.echo(payload)


# ---------------------------------------------------------------------------
# run — chain scaffold + start + iterate-to-completion in one call
# ---------------------------------------------------------------------------


@mission_cmd.command("run")
@click.option(
    "--directive",
    required=True,
    help="Natural-language goal description.",
)
@click.option(
    "--tool-allowlist",
    multiple=True,
    help="Tool name to allowlist; pass multiple times. Optional with --allow-all-tools.",
)
@click.option(
    "--allow-all-tools",
    is_flag=True,
    help=(
        "Resolve the session's tool allowlist to every registered MCP tool "
        "(minus the mission_* control tools). Makes --tool-allowlist optional; "
        "mutually exclusive with it."
    ),
)
@click.option(
    "--max-iterations",
    type=int,
    default=5,
    show_default=True,
    help="Hard cap on the iteration count. Pass -1 to opt out (uncapped).",
)
@click.option(
    "--max-wall-clock",
    type=int,
    default=300,
    show_default=True,
    help="Hard cap on wall-clock seconds. Pass -1 to opt out (uncapped).",
)
@click.option(
    "--max-criteria",
    type=int,
    default=5,
    show_default=True,
    help="Cap on the number of criterion entries scaffolded.",
)
@click.option(
    "--retries",
    type=int,
    default=3,
    show_default=True,
    help="Sampling-path retry budget on validator rejections during scaffolding.",
)
@click.option(
    "--use-sampling/--no-sampling",
    "use_sampling",
    default=None,
    help=(
        "Force the sampling path on/off for both the scaffolder and "
        "the loop's Strategy_Revision sampler. Default auto-detects: "
        "MCP host capability, then Bedrock credentials, then deterministic."
    ),
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
    "--save-criteria",
    "save_criteria",
    type=click.Path(dir_okay=False),
    default=None,
    help="Optional path to also persist the scaffolded criteria JSON to disk.",
)
@click.option(
    "--stagnation-threshold",
    type=int,
    default=3,
    show_default=True,
    help="Iterations of no progress before terminate.",
)
@click.option(
    "--cadence",
    type=click.Choice(["every_iteration", "every_n_iterations", "every_t_seconds", "on_event"]),
    default="every_iteration",
    show_default=True,
    help="Checkpoint cadence kind.",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    help=(
        "Use a stub tool dispatcher and disable Strategy_Revision sampling "
        "during iteration. The criteria scaffolder still runs through "
        "Bedrock when sampling is enabled. Useful for smoke-testing the "
        "loop without spending live tool credits."
    ),
)
def mission_run_cmd(
    directive: str,
    tool_allowlist: tuple[str, ...],
    allow_all_tools: bool,
    max_iterations: int,
    max_wall_clock: int,
    max_criteria: int,
    retries: int,
    use_sampling: bool | None,
    bedrock_model_id: str | None,
    allow_scripted_strategies: bool,
    save_criteria: str | None,
    stagnation_threshold: int,
    cadence: str,
    dry_run: bool,
) -> None:
    """Scaffold criteria and run a Mission session to completion in one call.

    The chained shorthand for the most common Mission invocation: turn
    a natural-language directive into a criteria file via
    ``scaffold-criteria`` (sampling path with deterministic fallback),
    persist a new session with ``start``'s validators, then drive it
    through ``run-to-completion`` with the same per-call verdict
    streaming as ``mission start --run``.

    Per-iteration verdict updates land on stderr as JSON lines; the
    Final_Report (or persisted session JSON when no Final_Report file
    was written) lands on stdout when the loop terminates.

    With ``--save-criteria PATH``, the scaffolded criteria JSON is
    also written to ``PATH`` so the operator can inspect / re-use it
    without re-running the scaffold step.
    """
    import mission.criteria_scaffold as criteria_scaffold  # noqa: PLC0415 — lazy
    from mission import (
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

    if max_criteria < 1:
        _emit_error(
            "validation_error",
            {"field": "max-criteria", "reason": "must_be_positive_int"},
        )
        sys.exit(1)
    if retries < 0:
        _emit_error(
            "validation_error",
            {"field": "retries", "reason": "must_be_non_negative_int"},
        )
        sys.exit(1)

    # Resolve the effective allowlist up front, before scaffolding or any
    # persistence. A mutual-exclusivity or empty-registry rejection exits here
    # with no sampling spend, no criteria file write, and no state write. The
    # scaffolder below still consults the explicit ``tool_allowlist`` (empty
    # under --allow-all-tools, which routes it to the directive-only
    # deterministic path); ``allowlist_resolved`` fills the persisted session.
    allowlist_resolved = _resolve_cli_allowlist(
        allow_all_tools=allow_all_tools, tool_allowlist=tool_allowlist
    )

    # ---- Step 1: scaffold criteria. -------------------------------------
    # Resolve the sampling state once; reuse it for both the scaffold
    # call and the persisted session's ``use_sampling`` field so the
    # operator's --use-sampling/--no-sampling intent applies end-to-end.
    use_sampling_resolved, backend_resolved = mission_sampling.resolve_sampling_state(
        None, use_sampling
    )

    criteria: list[dict[str, Any]] | None = None
    sampling_path_taken = False
    if use_sampling_resolved and backend_resolved != "none":
        backend_obj = mission_sampling.select_sampling_backend(
            None,
            model_id=bedrock_model_id,
            prefs=None,
        )
        if backend_obj is not None:
            try:
                criteria = asyncio.run(
                    criteria_scaffold.generate_sampled_criteria(
                        backend_obj,
                        directive,
                        allowlist=list(tool_allowlist),
                        max_criteria=max_criteria,
                        retries=retries,
                    )
                )
                sampling_path_taken = True
            except criteria_scaffold.ScaffoldSamplingError as exc:
                click.echo(
                    f"sampling path failed ({exc.last_reason}); "
                    "falling back to deterministic templates.",
                    err=True,
                )
                criteria = None

    if criteria is None:
        criteria = criteria_scaffold.generate_deterministic_criteria(
            directive,
            allowlist=list(tool_allowlist) or None,
            max_criteria=max_criteria,
        )

    if save_criteria:
        Path(save_criteria).write_text(
            json.dumps(criteria, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )

    # ---- Step 2: validate everything and persist the session. -----------
    budget: dict[str, Any] = {
        "max_iterations": max_iterations,
        "max_wall_clock_seconds": max_wall_clock,
    }
    cadence_dict: dict[str, Any] = {"kind": cadence}

    try:
        directive_clean = mission_validation.validate_directive(directive)
        criteria_clean = mission_validation.validate_criteria(criteria)
        budget_clean = mission_validation.validate_budget(budget, allowlist_resolved, {})
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

    session_id = f"mission-{secrets.token_hex(8)}"
    now_iso = datetime.now(UTC).isoformat()
    session: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "session_id": session_id,
        "directive_text": directive_clean,
        "criteria": criteria_clean,
        "budget": budget_clean,
        "tool_allowlist": allowlist_resolved,
        "checkpoint_cadence": cadence_clean,
        "stagnation_threshold": stagnation_threshold,
        "use_sampling": use_sampling_resolved,
        "sampling_backend_resolved": backend_resolved,
        "allow_scripted_strategies": bool(allow_scripted_strategies),
        "status": "pending",
        "created_at": now_iso,
        "iterations": [],
        "no_progress_counter": 0,
    }
    if bedrock_model_id:
        session["bedrock_model_id"] = bedrock_model_id

    backend = mission_state.get_backend()
    backend.save_session(cast("SessionState", _strip_private_criteria(session)))

    # Emit a one-line scaffold summary to stderr so the operator can see
    # what shape the criteria landed in before the loop starts. Stdout is
    # reserved for the Final_Report at the end.
    _emit_json(
        {
            "event": "mission.run.scaffolded",
            "session_id": session_id,
            "criteria_count": len(criteria),
            "sampling_path": sampling_path_taken,
            "sampling_backend_resolved": backend_resolved,
        },
        err=True,
    )

    # ---- Step 3: iterate to completion. ---------------------------------
    _run_to_completion(session_id, dry_run=dry_run)
