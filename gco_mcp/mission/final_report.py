"""Mission Final_Report writer.

Builds and persists the durable JSON artifact that ends a Mission_Session.
The report captures the directive, criteria, budget, allowlist, cadence,
the full iteration history (with private parser caches stripped), and the
terminal verdict. Two surfaces:

* :func:`build_deterministic_report` — pure: takes a session and the
  terminal ``(verdict, reason)`` tuple, returns a dict containing only
  fields that can be derived from the session payload without consulting
  any LLM. The ``lessons`` and ``recommended_followups`` slots are
  pre-populated with templated text so a Mission running with sampling
  disabled — or with a sampling backend that fails — still produces a
  complete, useful report.
* :func:`write_final_report` — calls :func:`build_deterministic_report`,
  optionally overlays the sampler-supplied ``lessons`` /
  ``recommended_followups``, persists the report, and updates
  ``session["final_report_path"]``. Returns the persisted-path identifier.

The writer is deliberately backend-aware. :class:`FilesystemBackend` writes
the report as a sibling file at ``<root>/<session_id>.report.json`` using
the same temp-file + ``fsync`` + ``os.replace`` atomic pattern that
:meth:`FilesystemBackend.save_session` uses, so a reader concurrent with a
writer never sees a partial JSON document. Other backends (today, the
:class:`DynamoDBBackend` stub) embed the report on the session under a
``final_report`` key and re-save the session — DynamoDB's single-item
``put_item`` is atomic, so no separate dance is needed. The synthetic
identifier returned in that case is ``"dynamodb://{session_id}/report"`` so
callers always have a stable string to record on
``session["final_report_path"]``.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from .state import FilesystemBackend
from .types import IterationRecord, SessionState, VerdictLabel, VerdictReason

__all__ = [
    "build_deterministic_report",
    "write_final_report",
]

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Type aliases
# --------------------------------------------------------------------------

# A sampler callable supplies LLM-derived ``lessons`` /
# ``recommended_followups`` overlays for the report. It receives the
# session and the terminal verdict tuple, and returns a dict carrying the
# two keys — or ``None`` when the call failed and the deterministic
# templates should be kept.
Sampler = Callable[
    [SessionState, VerdictLabel, VerdictReason],
    "dict[str, Any] | None",
]


# Private cache key written by ``validate_criteria`` onto every
# ``predicate`` Criterion. We strip it from anything that lands in the
# report so the artifact stays portable JSON.
_PARSED_AST_KEY = "_parsed_ast"


# --------------------------------------------------------------------------
# Public surface
# --------------------------------------------------------------------------


def build_deterministic_report(
    session: SessionState,
    verdict: VerdictLabel,
    reason: VerdictReason,
) -> dict[str, Any]:
    """Return the Final_Report dict using only deterministic session fields.

    The returned dict carries:

    * Identification — ``session_id`` and the verbatim ``directive_text``.
    * Configuration snapshot — ``criteria`` (with the cached parser AST
      stripped), ``budget``, ``tool_allowlist``, ``checkpoint_cadence``,
      and ``stagnation_threshold``.
    * Lifecycle timestamps — ``created_at``, ``started_at`` (``None``
      when the session never ran a real iteration), and a fresh
      ``ended_at`` set to the current UTC time.
    * Outcome — ``iterations_run``, ``final_verdict``,
      ``final_verdict_reason``, ``final_criteria_evaluation`` (the last
      iteration's per-Criterion results, or ``None`` when no iteration
      ran).
    * Iteration history — ``iterations``, deep-copied with private
      ``_parsed_ast`` keys stripped throughout.
    * Templated narrative — ``lessons`` and ``recommended_followups``
      pre-populated with deterministic template text so a session that
      ran with sampling disabled, or whose sampler failed, still
      produces a useful report. :func:`write_final_report` overlays
      these two fields when a working sampler is supplied.

    Pure: depends only on the session payload and the verdict tuple, and
    produces nothing that a caller could not regenerate from the same
    inputs. The single ``datetime.now`` call records the moment the
    report was assembled — that is itself the deterministic function of
    "now I am writing the report" rather than business logic that
    consults the clock.
    """
    now_iso = datetime.now(UTC).isoformat()

    report: dict[str, Any] = {
        "session_id": session["session_id"],
        "directive_text": session["directive_text"],
        "criteria": _strip_parsed_ast_from_criteria(
            cast("list[dict[str, Any]]", list(session.get("criteria") or []))
        ),
        "budget": dict(session.get("budget") or {}),
        "tool_allowlist": list(session.get("tool_allowlist") or []),
        "checkpoint_cadence": dict(session.get("checkpoint_cadence") or {}),
        "stagnation_threshold": session.get("stagnation_threshold"),
        "created_at": session.get("created_at"),
        "started_at": session.get("started_at"),
        "ended_at": now_iso,
        "iterations_run": len(session.get("iterations") or []),
        "final_verdict": verdict,
        "final_verdict_reason": reason,
        "final_criteria_evaluation": _final_criteria_evaluation(session),
        "lessons": _build_lessons_template(session, verdict, reason),
        "recommended_followups": _build_followups_template(session, verdict, reason),
        "iterations": _strip_parsed_ast_from_iterations(session.get("iterations") or []),
    }
    return report


def write_final_report(
    backend: Any,
    session: SessionState,
    verdict: VerdictLabel,
    reason: VerdictReason,
    sampler: Sampler | None = None,
) -> str:
    """Build, optionally overlay, and persist the Final_Report.

    The flow is:

    1. :func:`build_deterministic_report` produces a complete report
       dict with templated ``lessons`` / ``recommended_followups``.
    2. When ``sampler`` is supplied, it is called once with
       ``(session, verdict, reason)``. A returned dict whose ``lessons``
       and / or ``recommended_followups`` keys are well-typed overlays
       the corresponding template values; any other return (``None``,
       a dict missing both keys, or an exception) leaves the templates
       intact. Sampler failures are logged at WARNING and never
       propagated — the report must always land.
    3. The report is persisted alongside (or on) the session, depending
       on the backend type:

       * :class:`mcp.mission.state.FilesystemBackend` writes
         ``<root>/<session_id>.report.json`` using the same temp-file +
         ``fsync`` + ``os.replace`` atomic pattern as
         :meth:`FilesystemBackend.save_session`. Returns the absolute
         path of the report file.
       * Any other backend (today, the DynamoDB stub) attaches the
         report dict to the session under ``final_report`` and calls
         ``backend.save_session(session)``. DynamoDB's single-item
         ``put_item`` is atomic so no separate dance is needed. Returns
         ``"dynamodb://{session_id}/report"`` as a stable synthetic
         identifier.

    4. ``session["final_report_path"]`` is updated with the returned
       identifier so callers (and the next ``backend.save_session``)
       record where the report lives.
    """
    report = build_deterministic_report(session, verdict, reason)

    if sampler is not None:
        overlay = _safely_invoke_sampler(sampler, session, verdict, reason)
        if overlay is not None:
            _apply_sampler_overlay(report, overlay)

    if isinstance(backend, FilesystemBackend):
        path = _write_report_to_filesystem(backend, session["session_id"], report)
    else:
        path = _attach_report_to_session(backend, session, report)

    session["final_report_path"] = path
    return path


# --------------------------------------------------------------------------
# Templated narrative
# --------------------------------------------------------------------------


def _build_lessons_template(
    session: SessionState,
    verdict: VerdictLabel,
    reason: VerdictReason,
) -> str:
    """Deterministic ``lessons`` paragraph for sessions without sampling overlay.

    A few lines of operator-readable narrative pulling exclusively from
    the persisted session: the directive, the terminal verdict and
    reason, the iteration count, and a comma-separated list of unmet or
    inconclusive criterion ids drawn from the final iteration's
    evaluation. Stays short and machine-parseable so it is easy to grep
    or display in a CLI summary.
    """
    iterations = session.get("iterations") or []
    iteration_count = len(iterations)
    directive = session.get("directive_text", "")
    # Trim the directive so a verbose multi-line directive does not turn
    # this paragraph into a wall of text.
    if len(directive) > 240:
        directive = directive[:237] + "..."

    final_eval = _final_criteria_evaluation(session) or []
    not_met_ids = [
        result["criterion_id"]
        for result in final_eval
        if result.get("status") in ("unmet", "inconclusive")
    ]
    not_met_summary = ", ".join(not_met_ids) if not_met_ids else "none"

    return (
        f"Mission ended with verdict {verdict!r} (reason {reason!r}) after "
        f"{iteration_count} iteration(s). Directive: {directive!r}. "
        f"Outstanding criteria at termination: {not_met_summary}. "
        "This summary is templated text — re-run with sampling enabled to "
        "replace it with a model-derived narrative."
    )


def _build_followups_template(
    session: SessionState,
    verdict: VerdictLabel,
    reason: VerdictReason,
) -> list[str]:
    """Deterministic ``recommended_followups`` for templated reports.

    Returns 1–3 generic next-step suggestions chosen from the verdict
    reason. Pure: same inputs → same outputs. Wording stays short so
    callers can render the list as bullet points in a CLI summary.

    The ``session`` argument is unused today but kept on the signature
    so a future enhancement that consults the iteration history (e.g.
    naming the most-used tool) can be added without changing every
    call site.
    """
    del session  # currently unused; kept for signature stability

    suggestions: list[str] = []

    if verdict == "complete":
        suggestions.append(
            "Persist any artefacts produced by the final iteration so the "
            "outcome survives beyond the session JSON."
        )
        suggestions.append(
            "Re-run with tighter criteria thresholds to confirm the result "
            "was not a borderline match."
        )
    elif reason == "max_iterations":
        suggestions.append(
            "Re-run with a higher max_iterations cap if more iterations "
            "would plausibly close the remaining gap."
        )
        suggestions.append(
            "Inspect the iteration history for repeated tool sequences and "
            "consider tightening the strategy revision heuristic."
        )
    elif reason == "max_wall_clock":
        suggestions.append(
            "Re-run with a higher max_wall_clock_seconds budget, or split "
            "the directive into smaller sub-goals."
        )
    elif reason == "no_progress":
        suggestions.append(
            "Re-evaluate criteria thresholds — sustained no-progress may "
            "indicate the targets are unreachable with the current tool "
            "allowlist."
        )
        suggestions.append(
            "Widen the tool allowlist or supply a richer directive so the "
            "loop can explore alternative strategies."
        )
    elif reason == "user_abort":
        suggestions.append(
            "Resume the session with mission_resume once the manual intervention is complete."
        )
    else:
        suggestions.append(
            "Inspect the iteration history for the last verdict and adjust "
            "the directive, criteria, or allowlist accordingly."
        )

    suggestions.append(
        "These suggestions are templated — re-run with sampling enabled to "
        "replace them with model-derived followups."
    )
    return suggestions[:3]


# --------------------------------------------------------------------------
# Strip helpers — pure
# --------------------------------------------------------------------------


def _strip_parsed_ast_from_criteria(criteria: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a shallow copy of ``criteria`` with private parser caches removed.

    The ``validate_criteria`` validator caches the parsed AST under
    ``_parsed_ast`` on every ``predicate`` Criterion. The Final_Report
    is meant to be portable JSON, so we strip the cache before
    serialisation. The strip is also defensive: the report dict is
    later passed through ``json.dumps``, and an ``ast.Expression``
    object would raise there with a less obvious error than this.
    """
    cleaned: list[dict[str, Any]] = []
    for criterion in criteria:
        if not isinstance(criterion, dict):
            cleaned.append(criterion)
            continue
        cleaned.append({k: v for k, v in criterion.items() if k != _PARSED_AST_KEY})
    return cleaned


def _strip_parsed_ast_from_iterations(
    iterations: list[IterationRecord],
) -> list[dict[str, Any]]:
    """Return a deep copy of the iteration history with parser caches removed.

    Walks every nested dict and drops any ``_parsed_ast`` entry it
    finds. The validators only cache on Criterion entries today, but
    the strip is intentionally broad so a future code path that
    accidentally embeds a Criterion (with its cache attached) inside an
    IterationRecord cannot corrupt the report's JSON serialisation.
    """
    cloned = copy.deepcopy(list(iterations))
    for entry in cloned:
        _strip_parsed_ast_in_place(entry)
    return cast(list[dict[str, Any]], cloned)


def _strip_parsed_ast_in_place(value: Any) -> None:
    """Recursively delete ``_parsed_ast`` keys from any nested dict."""
    if isinstance(value, dict):
        if _PARSED_AST_KEY in value:
            del value[_PARSED_AST_KEY]
        for inner in value.values():
            _strip_parsed_ast_in_place(inner)
    elif isinstance(value, list):
        for inner in value:
            _strip_parsed_ast_in_place(inner)


def _final_criteria_evaluation(session: SessionState) -> list[dict[str, Any]] | None:
    """Return the last iteration's ``criteria_evaluation`` list, or ``None``.

    Used as the ``final_criteria_evaluation`` field on the report so a
    consumer can answer "which criteria were met at the moment the
    session ended" without scanning the iteration history.
    Returns ``None`` when the session ran no iterations — the report is
    still useful for sessions that terminated at start (e.g. a
    user_abort before the first iteration).
    """
    iterations = session.get("iterations") or []
    if not iterations:
        return None
    last = iterations[-1]
    evaluation = last.get("criteria_evaluation")
    if not evaluation:
        return None
    return [dict(result) for result in evaluation]


# --------------------------------------------------------------------------
# Sampler overlay
# --------------------------------------------------------------------------


def _safely_invoke_sampler(
    sampler: Sampler,
    session: SessionState,
    verdict: VerdictLabel,
    reason: VerdictReason,
) -> dict[str, Any] | None:
    """Call ``sampler`` and return its dict, or ``None`` on any failure.

    A sampler that raises must not block the report from landing — the
    Final_Report is the durable exit artifact of the loop. Any
    exception is logged at WARNING and swallowed, leaving the
    deterministic templates in place. A non-dict return is treated the
    same way (logged, ignored).
    """
    try:
        result = sampler(session, verdict, reason)
    except Exception:
        logger.warning(
            "Mission sampler raised while building Final_Report for session %s; "
            "keeping templated lessons / recommended_followups.",
            session.get("session_id"),
            exc_info=True,
        )
        return None
    if result is None:
        return None
    if not isinstance(result, dict):
        logger.warning(
            "Mission sampler returned a non-dict (%s) for session %s; "
            "keeping templated lessons / recommended_followups.",
            type(result).__name__,
            session.get("session_id"),
        )
        return None
    return result


def _apply_sampler_overlay(report: dict[str, Any], overlay: dict[str, Any]) -> None:
    """Overwrite ``lessons`` and / or ``recommended_followups`` if well-typed.

    Each field is overlaid independently: a sampler that produced a
    valid ``lessons`` string but malformed ``recommended_followups``
    keeps the lessons replacement and falls back to the template list
    for the followups. The shape checks are defensive — a sampler is
    free-form by contract, and silently dropping a malformed field is
    safer than letting a non-string slip into a downstream consumer.
    """
    lessons = overlay.get("lessons")
    if isinstance(lessons, str) and lessons:
        report["lessons"] = lessons

    followups = overlay.get("recommended_followups")
    if isinstance(followups, list) and all(isinstance(item, str) for item in followups):
        report["recommended_followups"] = list(followups)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def _write_report_to_filesystem(
    backend: FilesystemBackend,
    session_id: str,
    report: dict[str, Any],
) -> str:
    """Persist ``report`` as ``<root>/<session_id>.report.json`` atomically.

    Mirrors the temp-file + ``fsync`` + ``os.replace`` pattern from
    :meth:`FilesystemBackend.save_session`: a partial write leaves the
    temp file behind but never replaces the existing report file, so a
    reader concurrent with a writer always sees either the prior
    version or the new one. Returns the absolute path of the written
    file.

    Uses :meth:`FilesystemBackend._ensure_root` to lazily create the
    backend's root directory on first use; this matches the session
    writer and avoids duplicating the directory-creation logic here.
    """
    backend._ensure_root()
    final = backend.root / f"{session_id}.report.json"
    try:
        tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115 - explicit close+replace below
            mode="w",
            encoding="utf-8",
            dir=str(backend.root),
            prefix=f"{session_id}.report.",
            suffix=".json.tmp",
            delete=False,
        )
        try:
            json.dump(report, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
        finally:
            tmp.close()
        if os.name != "nt":
            with contextlib.suppress(OSError):
                # Same rationale as the session writer: a successful
                # fsync is too valuable to abandon over a permission
                # tightening that the underlying filesystem refused.
                os.chmod(tmp.name, 0o600)
        os.replace(tmp.name, final)
    except OSError as exc:
        # Re-raise with the underlying message intact so operators see
        # the real cause (disk full, permission denied) rather than a
        # wrapped abstraction.
        raise OSError(str(exc)) from exc
    return str(final)


def _attach_report_to_session(
    backend: Any,
    session: SessionState,
    report: dict[str, Any],
) -> str:
    """Embed ``report`` on the session and re-save through the backend.

    Used for backends that do not write sibling files (today, the
    DynamoDB stub). Returns the synthetic identifier
    ``"dynamodb://{session_id}/report"`` so the caller has a stable
    path-like value to record on ``session["final_report_path"]``.

    The session is mutated in place: the ``final_report`` key carries
    the report dict so a later ``backend.load_session`` returns the
    full payload without a second round-trip. The backend's
    ``save_session`` performs whatever atomicity the storage layer
    provides (DynamoDB ``put_item`` is single-item-atomic by contract).
    """
    # ``final_report`` is not declared on :class:`SessionState`; cast
    # through ``dict[str, Any]`` so the assignment lands without a
    # TypedDict-unknown-key complaint while keeping the underlying
    # session object identity intact.
    cast(dict[str, Any], session)["final_report"] = report
    backend.save_session(session)
    return f"dynamodb://{session['session_id']}/report"
