"""End-to-end Mission session driven to completion by a predicate criterion.

Walks one full Mission session through the engine without going near the
MCP or CLI surfaces. A stub tool dispatcher returns a ``score`` field
that grows monotonically across calls; a single ``predicate`` criterion
declares the session complete once an iteration's tool_results both
contains at least five entries and includes one with ``score > 0.9``.

The shape mirrors :mod:`tests.test_mission_e2e_train_to_loss` (the
metric_threshold precedent) so any drift in the persisted contract
surfaces in both places. Two structural overrides earn their own short
explanation here because they are non-obvious:

* The Propose_Phase deterministic fallback synthesises a
  *single*-tool-call Strategy per iteration. The predicate the brief
  calls out cannot be satisfied by a one-call iteration's
  ``tool_results`` (which would have length 1, not the required 5),
  so the test monkey-patches :meth:`MissionEngine._build_strategy` to
  return a five-call Strategy each iteration. The engine's
  ``_propose_phase`` wrapper still owns the audit + record bookkeeping;
  only the strategy-shape decision moves into the test.
* The dispatcher returns a synthetic ``score`` that grows ``0.10,
  0.15, 0.20, …`` per call across the entire session — *not* reset
  between iterations. Each iteration's ``tool_results`` therefore
  contains five entries from a sliding window of the score series, and
  the predicate's ``score > 0.9`` clause first fires on iteration 3
  (call 17 produces ``score=0.95``).

The brief's predicate text uses ``obs.get("tool_results", [])`` and
``r.get("score", 0)``. The Mission predicate sandbox is intentionally
tighter than that — calls must target a bare allowlisted callable
name, so attribute calls on ``obs`` (or on a comprehension target) are
rejected at parse time. Subscript notation is the documented allowed
surface for nested data, and the engine guarantees ``tool_results`` is
always present on the Observation, so the rewritten expression is
semantically equivalent for this test's data shape:

    len(obs["tool_results"]) >= 5
    and any(r["score"] > 0.9 for r in obs["tool_results"])

The test runs offline against a :class:`FilesystemBackend` rooted at
``tmp_path`` — no AWS calls, no network, no real LLM.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# ``mcp/run_mcp.py`` adds ``mcp/`` to ``sys.path`` at runtime; pytest has
# to mirror that before any ``mission.*`` import resolves. Same idiom
# used by every other ``test_mission_*`` module.
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))

from mission import SCHEMA_VERSION  # noqa: E402
from mission.engine import MissionEngine  # noqa: E402
from mission.predicate import parse_predicate  # noqa: E402
from mission.state import FilesystemBackend  # noqa: E402

# ---------------------------------------------------------------------------
# Test parameters
# ---------------------------------------------------------------------------

# Five tool calls per iteration. The brief's predicate requires
# ``len(tool_results) >= 5`` to be true within a single iteration's
# Observation (``tool_results`` resets between iterations — see the
# Observe_Phase contract in ``mcp/mission/engine.py``).
_CALLS_PER_ITERATION = 5

# Score increment per call. With this value, scores cross ``0.9`` on
# call 17 (``17 * 0.05 = 0.85``? no: scores are ``0.10, 0.15, 0.20, …``
# i.e. ``0.05 * (n + 1)`` for the n-th 0-indexed call). Call 17 produces
# ``0.05 * 18 = 0.90`` which fails the strict ``> 0.9`` clause; call 18
# produces ``0.95`` which passes. Iteration 3 spans calls 15–19 (0-indexed)
# so the predicate's ``any`` clause fires on iteration 3.
_SCORE_INCREMENT = 0.05

# Predicate expression — equivalent to the brief's text but rewritten in
# the subscript form the predicate sandbox accepts. See module docstring.
_PREDICATE_EXPR = (
    'len(obs["tool_results"]) >= 5 and any(r["score"] > 0.9 for r in obs["tool_results"])'
)

# Operator-language directive carried verbatim through the loop. No
# references to internal planning artifacts so the no-spec-references
# guardrail stays happy.
_DIRECTIVE = (
    "Search a bounded experiment space for an approach scoring above "
    "0.9 on the evaluator. Surface a hit within the iteration budget."
)


def _make_session(*, session_id: str = "sess-search") -> dict[str, Any]:
    """Build a minimally-populated ``SessionState`` dict by hand.

    Bypasses the full ``mission_start`` validation path on purpose —
    the engine consumes the typed fields directly and the validators
    are exercised in their own test module. The one validator we *do*
    call is :func:`mcp.mission.predicate.parse_predicate`, attached on
    the criterion under ``_parsed_ast`` so the engine's
    Evaluate_Phase has the cached AST ready (its production caller
    :func:`mcp.mission.validation.validate_criteria` does the same).

    Tuning notes:

    * ``max_iterations=10`` is generous: with the score series above
      the predicate fires on iteration 3, well inside the cap.
    * ``every_iteration`` cadence makes every iteration an evaluated
      checkpoint, so the verdict cascade reaches the completion check
      on every iteration without synthetic ``cadence_skip`` verdicts.
    * ``stagnation_threshold=100`` keeps the no-progress termination
      branch dormant — a three-iteration run cannot reach a counter
      of 100.
    """
    return {
        "version": SCHEMA_VERSION,
        "session_id": session_id,
        "directive_text": _DIRECTIVE,
        "criteria": [
            {
                "criterion_id": "found_high_score",
                "kind": "predicate",
                "required": True,
                "expression": _PREDICATE_EXPR,
                # Pre-validated AST — the engine's _evaluate_predicate
                # returns "inconclusive" when this slot is missing.
                "_parsed_ast": parse_predicate(_PREDICATE_EXPR),
            }
        ],
        "budget": {
            "max_iterations": 10,
            "max_wall_clock_seconds": 600,
        },
        "tool_allowlist": ["find_examples"],
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 100,
        "use_sampling": False,
        "allow_scripted_strategies": False,
        "status": "pending",
        "created_at": "2025-01-01T00:00:00Z",
        "iterations": [],
        "no_progress_counter": 0,
    }


# ---------------------------------------------------------------------------
# Backend wrapper
# ---------------------------------------------------------------------------


class _PredicateAwareBackend(FilesystemBackend):
    """Filesystem backend that survives a non-JSON ``_parsed_ast`` cache.

    The engine reloads the session from the backend on every
    ``run_iteration`` call. The cached :class:`ast.Expression` attached
    by :func:`parse_predicate` is not JSON-serialisable, so a plain
    :class:`FilesystemBackend.save_session` would raise ``TypeError``
    when handed a session with predicate criteria. Production avoids
    this by pre-stripping private keys at the MCP-tool layer; here we
    fold the same pattern into a thin backend wrapper so the test
    drives the engine through its real persistence cycle.

    On save, we deep-copy the session and drop every leading-underscore
    key from each criterion. On load, we re-parse the criterion
    expressions and re-attach the AST under ``_parsed_ast`` so the
    engine's Evaluate_Phase finds it on every iteration.
    """

    def save_session(self, session: dict[str, Any]) -> None:  # type: ignore[override]
        cleaned = dict(session)
        criteria = cleaned.get("criteria")
        if isinstance(criteria, list):
            cleaned["criteria"] = [
                {k: v for k, v in c.items() if not str(k).startswith("_")}
                if isinstance(c, dict)
                else c
                for c in criteria
            ]
        super().save_session(cleaned)  # type: ignore[arg-type]

    def load_session(self, session_id: str) -> dict[str, Any] | None:  # type: ignore[override]
        loaded = super().load_session(session_id)
        if loaded is None:
            return None
        criteria = loaded.get("criteria")
        if isinstance(criteria, list):
            for criterion in criteria:
                if isinstance(criterion, dict) and criterion.get("kind") == "predicate":
                    expression = criterion.get("expression")
                    if isinstance(expression, str):
                        criterion["_parsed_ast"] = parse_predicate(expression)
        return loaded


# ---------------------------------------------------------------------------
# The end-to-end test
# ---------------------------------------------------------------------------


@pytest.mark.mission_e2e
async def test_bounded_experiment_search_completes_via_predicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive a session whose predicate flips to ``met`` once a high score appears.

    With the dispatcher returning ``score = 0.05 * (n + 1)`` for the
    n-th 0-indexed call and a five-call Strategy per iteration:

    * iteration 0 — calls 0..4, scores ``0.05..0.25`` → ``unmet``
    * iteration 1 — calls 5..9, scores ``0.30..0.50`` → ``unmet``
    * iteration 2 — calls 10..14, scores ``0.55..0.75`` → ``unmet``
    * iteration 3 — calls 15..19, scores ``0.80..1.00`` → ``met``

    The verdict cascade returns ``("complete", "criteria_met")`` on
    iteration 3.
    """
    backend = _PredicateAwareBackend(root=tmp_path)
    session = _make_session()
    backend.save_session(session)

    # Override _build_strategy with a five-tool-call shape. The
    # engine's _propose_phase wrapper still owns audit emission and
    # record bookkeeping; we are replacing only the strategy-shape
    # decision. The deterministic fallback the production engine
    # supplies builds a single-tool-call Strategy, which cannot
    # populate ``tool_results`` to the predicate's required length.
    async def five_call_strategy(
        self: MissionEngine,
        session: dict[str, Any],
        ctx: Any | None,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "tool_calls": [
                {"tool_name": "find_examples", "args": {"page": i}}
                for i in range(_CALLS_PER_ITERATION)
            ],
            "rationale": ("test override: five-call sweep over the experiment space"),
        }

    monkeypatch.setattr(MissionEngine, "_build_strategy", five_call_strategy)

    # Stateful dispatcher — the call counter persists across the
    # entire session so the score series grows monotonically. A
    # closure over a mutable mapping is the simplest pattern that
    # survives the engine's per-iteration re-entry into the
    # dispatcher; same idiom as the train-to-loss precedent.
    state = {"calls": 0}

    async def dispatcher(tool_name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        # Tool name and args are ignored — the stub does not need to
        # discriminate, and the engine is the single place that
        # checks Tool_Allowlist gating before this callable is even
        # invoked.
        state["calls"] += 1
        return {"score": _SCORE_INCREMENT * state["calls"]}

    engine = MissionEngine(
        backend=backend,
        tool_dispatcher=dispatcher,
        sampling_callable=None,
        sandbox_runner=None,
    )

    # Drive iterations until the verdict cascade ends the run. The 10
    # safety bound matches ``budget.max_iterations`` so a regression
    # in completion detection shows up as a test failure here rather
    # than as an infinite loop.
    final_record: dict[str, Any] | None = None
    for _ in range(10):
        record = await engine.run_iteration(session["session_id"])
        if record["verdict"] in ("complete", "terminate"):
            final_record = record
            break
    else:  # pragma: no cover — safety bound
        pytest.fail(
            "Mission did not reach a terminal verdict within "
            "max_iterations; predicate completion may be misconfigured."
        )

    assert final_record is not None
    # ------------------------------------------------------------------ #
    # Verdict invariant — completion fired on the iteration where the
    # predicate first evaluated true.
    # ------------------------------------------------------------------ #
    assert final_record["verdict"] == "complete"
    assert final_record["verdict_reason"] == "criteria_met"

    # ------------------------------------------------------------------ #
    # Persistence invariant — session is now in ``completed`` state,
    # the iteration count matches the expected four-iteration run, and
    # the per-iteration criterion status flipped from ``unmet`` to
    # ``met`` exactly once.
    # ------------------------------------------------------------------ #
    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    assert persisted["status"] == "completed"
    assert persisted["final_verdict"] == "complete"
    assert len(persisted["iterations"]) == 4

    statuses = [
        iteration["criteria_evaluation"][0]["status"] for iteration in persisted["iterations"]
    ]
    assert statuses == ["unmet", "unmet", "unmet", "met"]

    # Sanity-check that every iteration's Observation indeed carried
    # the five-result tool_results list — the predicate's other
    # clause depends on this and a regression in the Observe_Phase
    # would show up as a verdict mismatch otherwise.
    for iteration in persisted["iterations"]:
        observation = iteration["observation"]
        assert len(observation["tool_results"]) == _CALLS_PER_ITERATION
        for result in observation["tool_results"]:
            assert "score" in result

    # On the final iteration, at least one result crossed the 0.9
    # threshold — the predicate's ``any`` clause.
    final_scores = [
        result["score"] for result in persisted["iterations"][-1]["observation"]["tool_results"]
    ]
    assert any(score > 0.9 for score in final_scores)
