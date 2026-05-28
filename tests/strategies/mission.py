"""Reusable Hypothesis strategies for Mission domain objects.

The strategies here build well-formed dicts shaped like the Mission
``TypedDict``s in ``mcp/mission/types.py``. They are deliberately
minimal: every required key is populated with a JSON-serializable
value drawn from a constrained leaf strategy, and the optional keys
are populated only when a test composes them in. The ``IterationRecord``
generator keeps the in-progress iteration's ``criteria_evaluation``
consistent with whatever ``criteria`` list the session declares so the
``decide_verdict`` cascade exercises every branch of its completion
check (some required, some optional, some met, some unmet).

Two top-level composites are exported:

* :func:`session_states` — yields a session whose ``iterations``
  list carries between zero and three already-persisted
  ``IterationRecord``s. The session's ``status`` is always
  ``"running"`` because the verdict cascade only ever runs on a
  session in that state.
* :func:`iteration_records` — yields a single in-progress
  ``IterationRecord`` whose ``criteria_evaluation`` matches the
  ``criterion_id`` set on the supplied criteria list. When called
  without an explicit criteria list, the strategy first draws a small
  criteria list and then matches the evaluation entries to it.

Both composites accept optional ``draw``-time overrides so tests can
pin specific fields (a fixed budget, a forced cadence kind, an
already-met criterion) without having to assemble the surrounding
shape by hand.

Datetime values are constrained to a narrow tz-aware range so the
strategies cannot draw timestamps that overflow Python's
``datetime`` arithmetic on edge platforms. Timestamps are emitted as
ISO 8601 strings on the dict to match the on-disk session format.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hypothesis import strategies as st

# Mirror the import pattern used by every other Mission test:
# ``mcp/run_mcp.py`` adds ``mcp/`` to ``sys.path`` at runtime, but
# pytest has to do it itself before the import below resolves.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcp"))

from mission.types import SCHEMA_VERSION  # noqa: E402

# ---------------------------------------------------------------------------
# Constants — narrow range chosen so datetime arithmetic never overflows.
# ---------------------------------------------------------------------------

#: Lower bound for any ``datetime`` strategy in this module.
MIN_DT: datetime = datetime(2025, 1, 1, tzinfo=UTC)

#: Upper bound for any ``datetime`` strategy in this module.
MAX_DT: datetime = datetime(2030, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Leaf strategies
# ---------------------------------------------------------------------------

# Identifier-shaped text — short, ASCII, no surrogates. Used wherever
# the tests need a string but the exact bytes don't matter.
_id_text = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), max_codepoint=127),
    min_size=1,
    max_size=12,
)

# Free-form text without unpaired surrogates so json.dumps in any
# downstream test never has to fall through to its non-strict path.
_free_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    max_size=24,
)

#: Tz-aware UTC datetimes within the supported window.
datetimes_utc = st.datetimes(
    min_value=MIN_DT.replace(tzinfo=None),
    max_value=MAX_DT.replace(tzinfo=None),
    timezones=st.just(UTC),
)


# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------


@st.composite
def cadences(draw: st.DrawFn) -> dict[str, Any]:
    """Draw any of the four supported Cadence shapes.

    The validators in ``mcp/mission/validation.py`` allow only the
    keys this strategy emits: ``every_iteration`` carries no extras,
    ``every_n_iterations`` carries a positive int ``n``,
    ``every_t_seconds`` carries a positive int ``t``, ``on_event``
    carries a non-empty ``event_name``.
    """
    kind = draw(
        st.sampled_from(
            [
                "every_iteration",
                "every_n_iterations",
                "every_t_seconds",
                "on_event",
            ]
        )
    )
    if kind == "every_iteration":
        return {"kind": kind}
    if kind == "every_n_iterations":
        return {"kind": kind, "n": draw(st.integers(min_value=1, max_value=8))}
    if kind == "every_t_seconds":
        return {"kind": kind, "t": draw(st.integers(min_value=1, max_value=3600))}
    return {"kind": kind, "event_name": draw(_id_text)}


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


@st.composite
def budgets(
    draw: st.DrawFn,
) -> dict[str, Any]:
    """Draw a BudgetControls dict.

    ``max_iterations`` and ``max_wall_clock_seconds`` are always
    drawn from a small positive-int range so verdict-cascade tests
    can exercise both "budget exhausted" and "budget plenty" cases.
    """
    return {
        "max_iterations": draw(st.integers(min_value=1, max_value=20)),
        "max_wall_clock_seconds": draw(st.integers(min_value=1, max_value=86_400)),
    }


# ---------------------------------------------------------------------------
# Criteria
# ---------------------------------------------------------------------------


@st.composite
def criteria_lists(
    draw: st.DrawFn,
    *,
    min_size: int = 0,
    max_size: int = 3,
) -> list[dict[str, Any]]:
    """Draw a list of unique-id criteria.

    The verdict cascade only inspects ``criterion_id`` and ``required``;
    every entry uses ``kind="event"`` because the cascade itself never
    re-evaluates a criterion (the engine has already populated
    ``iteration["criteria_evaluation"]`` by the time the cascade runs).
    Using a single kind keeps the strategy small without losing test
    coverage.
    """
    count = draw(st.integers(min_value=min_size, max_value=max_size))
    ids: list[str] = []
    while len(ids) < count:
        candidate = draw(_id_text)
        if candidate not in ids:
            ids.append(candidate)
    return [
        {
            "criterion_id": cid,
            "kind": "event",
            "required": draw(st.booleans()),
            "event_name": draw(_id_text),
        }
        for cid in ids
    ]


# ---------------------------------------------------------------------------
# CriterionResult
# ---------------------------------------------------------------------------


@st.composite
def criterion_results_for(
    draw: st.DrawFn,
    criteria: list[dict[str, Any]],
    *,
    evaluated_at: datetime | None = None,
) -> list[dict[str, Any]]:
    """Build a CriterionResult list aligned to ``criteria`` by id.

    Every entry in ``criteria`` gets exactly one matching result. The
    status mix is drawn freely so tests cover ``met``, ``unmet``, and
    ``inconclusive`` distributions.
    """
    when_iso = (evaluated_at or draw(datetimes_utc)).isoformat()
    return [
        {
            "criterion_id": c["criterion_id"],
            "status": draw(st.sampled_from(["met", "unmet", "inconclusive"])),
            "evidence": draw(st.one_of(st.none(), _free_text, st.integers())),
            "evaluated_at": when_iso,
        }
        for c in criteria
    ]


# ---------------------------------------------------------------------------
# Strategy / Observation / PhaseRecord
# ---------------------------------------------------------------------------


@st.composite
def tool_call_strategies(draw: st.DrawFn) -> dict[str, Any]:
    """Draw a Strategy that carries a non-empty ``tool_calls`` list.

    Scripted strategies are out of scope for the verdict-cascade tests
    because the heuristic compares tool-name sequences; an empty
    sequence (which scripts emit) would degenerate the property. A
    sibling strategy can be added later when a test specifically wants
    to exercise the scripted path.
    """
    count = draw(st.integers(min_value=1, max_value=3))
    return {
        "tool_calls": [{"tool_name": draw(_id_text), "args": {}} for _ in range(count)],
    }


@st.composite
def observations(
    draw: st.DrawFn,
    *,
    started_at: datetime | None = None,
) -> dict[str, Any]:
    """Draw an Observation dict with a small events/errors mix."""
    start = started_at or draw(datetimes_utc)
    end = start + timedelta(milliseconds=draw(st.integers(min_value=0, max_value=5_000)))
    return {
        "tool_results": draw(st.lists(st.integers(), max_size=3)),
        "metrics": {},
        "events": draw(
            st.lists(
                st.fixed_dictionaries({"event_name": _id_text}),
                max_size=2,
            )
        ),
        "errors": draw(
            st.lists(
                st.fixed_dictionaries({"message": _id_text}),
                max_size=2,
            )
        ),
        "phase_started_at": start.isoformat(),
        "phase_ended_at": end.isoformat(),
    }


@st.composite
def phase_records(
    draw: st.DrawFn,
    *,
    base_time: datetime | None = None,
) -> list[dict[str, Any]]:
    """Draw the five PhaseRecord entries for a successful iteration.

    The verdict cascade does not consult phase records, so every record
    here is shaped as ``status="succeeded"`` for simplicity.
    """
    start = base_time or draw(datetimes_utc)
    out: list[dict[str, Any]] = []
    for offset, phase in enumerate(("propose", "execute", "observe", "evaluate", "decide")):
        phase_start = start + timedelta(milliseconds=offset * 10)
        phase_end = phase_start + timedelta(milliseconds=5)
        out.append(
            {
                "phase": phase,
                "status": "succeeded",
                "started_at": phase_start.isoformat(),
                "ended_at": phase_end.isoformat(),
            }
        )
    return out


# ---------------------------------------------------------------------------
# IterationRecord
# ---------------------------------------------------------------------------


@st.composite
def iteration_records(
    draw: st.DrawFn,
    *,
    iteration_index: int | None = None,
    criteria: list[dict[str, Any]] | None = None,
    base_time: datetime | None = None,
) -> dict[str, Any]:
    """Draw a single, well-formed IterationRecord dict.

    When ``criteria`` is supplied the iteration's
    ``criteria_evaluation`` aligns to it by ``criterion_id``. When it
    is not, the strategy draws a fresh criteria list and uses it to
    keep the evaluation entries internally consistent.
    """
    if iteration_index is None:
        iteration_index = draw(st.integers(min_value=0, max_value=10))
    if criteria is None:
        criteria = draw(criteria_lists(min_size=0, max_size=3))
    when = base_time or draw(datetimes_utc)
    end = when + timedelta(milliseconds=draw(st.integers(min_value=0, max_value=10_000)))
    return {
        "iteration_index": iteration_index,
        "started_at": when.isoformat(),
        "ended_at": end.isoformat(),
        "phases": draw(phase_records(base_time=when)),
        "strategy": draw(tool_call_strategies()),
        "observation": draw(observations(started_at=when)),
        "criteria_evaluation": draw(criterion_results_for(criteria, evaluated_at=end)),
        "verdict": "continue",
        "verdict_reason": "in_progress",
        "checkpoint_evaluated": draw(st.booleans()),
    }


# ---------------------------------------------------------------------------
# SessionState
# ---------------------------------------------------------------------------


@st.composite
def session_states(
    draw: st.DrawFn,
    *,
    min_prior_iterations: int = 0,
    max_prior_iterations: int = 3,
    criteria: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Draw a well-formed ``SessionState`` dict.

    ``status`` is always ``"running"`` because the verdict cascade only
    ever runs on a non-terminal session. The session's ``iterations``
    list carries between ``min_prior_iterations`` and
    ``max_prior_iterations`` already-persisted records; the in-progress
    iteration is supplied separately by the caller.
    """
    if criteria is None:
        criteria = draw(criteria_lists(min_size=0, max_size=3))
    created = draw(datetimes_utc)
    started = created + timedelta(seconds=draw(st.integers(min_value=0, max_value=60)))
    last_checkpoint = started + timedelta(seconds=draw(st.integers(min_value=0, max_value=300)))
    cadence = draw(cadences())
    budget = draw(budgets())
    prior_count = draw(st.integers(min_value=min_prior_iterations, max_value=max_prior_iterations))
    iterations = [
        draw(
            iteration_records(
                iteration_index=index,
                criteria=criteria,
                base_time=started + timedelta(seconds=index),
            )
        )
        for index in range(prior_count)
    ]
    stagnation_threshold = draw(st.integers(min_value=1, max_value=10))
    no_progress = draw(st.integers(min_value=0, max_value=stagnation_threshold + 2))
    return {
        "version": SCHEMA_VERSION,
        "session_id": draw(_id_text),
        "directive_text": draw(_free_text),
        "criteria": criteria,
        "budget": budget,
        "tool_allowlist": draw(st.lists(_id_text, min_size=1, max_size=4, unique=True)),
        "checkpoint_cadence": cadence,
        "stagnation_threshold": stagnation_threshold,
        "use_sampling": draw(st.booleans()),
        "allow_scripted_strategies": draw(st.booleans()),
        "status": "running",
        "created_at": created.isoformat(),
        "started_at": started.isoformat(),
        "iterations": iterations,
        "no_progress_counter": no_progress,
        "last_checkpoint_at": last_checkpoint.isoformat(),
    }


# ---------------------------------------------------------------------------
# Combined draws — the verdict-cascade entry point
# ---------------------------------------------------------------------------


@st.composite
def decide_verdict_inputs(
    draw: st.DrawFn,
) -> tuple[dict[str, Any], dict[str, Any], datetime]:
    """Draw a ``(session, iteration, now)`` triple shaped for ``decide_verdict``.

    The ``iteration`` shares its criteria_id set with ``session["criteria"]``
    so the cascade's completion check exercises every branch (required vs.
    optional, met vs. unmet vs. inconclusive). ``now`` is drawn from a
    range that brackets the session's ``started_at`` so the
    ``max_wall_clock`` branch can fire on some draws and not others.
    """
    session = draw(session_states())
    started = datetime.fromisoformat(session["started_at"])
    iteration = draw(
        iteration_records(
            iteration_index=len(session["iterations"]),
            criteria=session["criteria"],
            base_time=started + timedelta(seconds=len(session["iterations"])),
        )
    )
    # Draw ``now`` either before or well after the session's wall-clock
    # cap so the budget branch sometimes fires and sometimes does not.
    max_seconds = session["budget"]["max_wall_clock_seconds"]
    delta_seconds = draw(st.integers(min_value=0, max_value=max_seconds + max_seconds + 60))
    now = started + timedelta(seconds=delta_seconds)
    if now > MAX_DT:
        # Keep the timestamp inside the strategy's declared window so
        # downstream callers comparing against ``MAX_DT`` aren't surprised.
        now = MAX_DT
    return session, iteration, now


__all__ = [
    "MAX_DT",
    "MIN_DT",
    "budgets",
    "cadences",
    "criteria_lists",
    "criterion_results_for",
    "datetimes_utc",
    "decide_verdict_inputs",
    "iteration_records",
    "observations",
    "phase_records",
    "session_states",
    "tool_call_strategies",
]
