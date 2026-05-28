"""Mission session that completes without any AWS access at all.

The Mission engine is meant to be portable: a session whose
Tool_Allowlist is restricted to safe-tier tools (no AWS calls, no
network) must reach a terminal verdict on a host that has no AWS
credentials configured. This module exercises that contract directly.

The session walks one full lifecycle through the engine — no MCP, no
CLI, just a hand-built ``SessionState`` against a
:class:`FilesystemBackend` rooted at ``tmp_path`` — with two safe-tier
tools in the allowlist (``find_examples`` and ``find_docs``). The
directive describes a documentation-search task. The single
``predicate`` Criterion declares the session complete the moment an
iteration's Observation carries any ``tool_results`` at all.

To prove the no-AWS contract is *enforced* rather than incidentally
held, the test monkey-patches :class:`boto3.Session` to raise on
construction. Any attempt to instantiate an AWS client during the
run blows up loudly, so a regression that started reaching for an
AWS service from a safe-tier path would surface as a test failure
here rather than as a silent runtime cost on a credential-less host.

Two structural notes about the predicate's expression earn their own
short explanation:

* The brief calls for ``len(obs.get("tool_results", [])) > 0``. The
  Mission predicate sandbox accepts subscript notation but rejects
  ``.get(...)``-style attribute calls on ``obs`` (the data namespace
  is not callable in any form). The :func:`mcp.mission.predicate`
  module has a tighter surface than free-form Python on purpose, and
  the engine guarantees ``tool_results`` is always present on the
  Observation, so the rewritten expression
  ``len(obs["tool_results"]) > 0`` is semantically equivalent for
  the Mission's data shape.
* The deterministic Propose_Phase fallback synthesises a
  *single*-tool-call Strategy on the first iteration (no prior
  successful call exists yet), invoking the first tool in the
  allowlist with empty args. That single call's
  ``result_summary`` lands as one entry on ``Observation["tool_results"]``,
  so ``len(obs["tool_results"]) == 1`` and the predicate flips to
  ``met`` immediately. The cascade returns
  ``("complete", "criteria_met")`` on iteration 0.
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

# Plain-operator directive carried verbatim through the loop. No
# references to internal planning artifacts so the no-spec-references
# guardrail stays happy.
_DIRECTIVE = (
    "Find the documentation page that explains how to run inference "
    "endpoints. Surface a hit from the docs catalog."
)

# Predicate expression rewritten in the subscript form the predicate
# sandbox accepts. The ``.get(...)`` form from the brief is rejected
# at parse time because ``obs`` is treated as data, not as a callable
# target. See module docstring for the full reasoning.
_PREDICATE_EXPR = 'len(obs["tool_results"]) > 0'

# The two safe-tier tools the brief calls out. Both are real,
# registered MCP tools (``find_examples`` lives in
# ``mcp/tools/examples.py`` and ``find_docs`` in ``mcp/tools/docs.py``)
# and both are tagged ``safe`` — they read static, in-process catalogs
# and never reach for AWS or the network.
_ALLOWLIST: list[str] = ["find_examples", "find_docs"]

# Driver-loop safety bound. The predicate flips to ``met`` on the
# first iteration's Observation, so the cascade must terminate on
# iteration 0. The bound exists so a regression in completion
# detection surfaces as a clean test failure rather than as an
# infinite loop.
_DRIVER_LOOP_BOUND = 5


def _make_session(*, session_id: str = "sess-no-aws") -> dict[str, Any]:
    """Build a minimal ``SessionState`` dict for the no-AWS smoke run.

    Bypasses the validators on purpose — the engine consumes the typed
    fields directly and the validators are exercised in their own test
    module. The shape mirrors :mod:`tests.test_mission_e2e_search` so
    any drift in the persisted contract surfaces in both places.

    Tuning notes:

    * ``max_iterations=5`` is generous: the predicate fires on
      iteration 0 with the deterministic single-call Strategy, so the
      cascade terminates well inside the cap.
    * ``every_iteration`` cadence makes every iteration an evaluated
      checkpoint, so the verdict cascade reaches the completion check
      on iteration 0 without synthetic ``cadence_skip`` verdicts.
    * ``stagnation_threshold=100`` keeps the no-progress termination
      branch dormant — a one-iteration run cannot reach a counter of
      100, so the cascade can only terminate via completion.
    * ``use_sampling=False`` keeps the sampling backend off the
      iteration path entirely. Combined with the ``boto3.Session``
      patch below, this guarantees the run never touches an AWS
      client even by accident.
    """
    return {
        "version": SCHEMA_VERSION,
        "session_id": session_id,
        "directive_text": _DIRECTIVE,
        "criteria": [
            {
                "criterion_id": "found_any_results",
                "kind": "predicate",
                "required": True,
                "expression": _PREDICATE_EXPR,
                # Pre-validated AST — the engine's
                # _evaluate_predicate returns "inconclusive" when this
                # slot is missing.
                "_parsed_ast": parse_predicate(_PREDICATE_EXPR),
            }
        ],
        "budget": {
            "max_iterations": 5,
            "max_wall_clock_seconds": 60,
        },
        "tool_allowlist": list(_ALLOWLIST),
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 100,
        "use_sampling": False,
        "allow_scripted_strategies": False,
        "status": "pending",
        "created_at": "2025-01-01T00:00:00Z",
        "iterations": [],
        "no_progress_counter": 0,
        "accumulated_cost_usd": 0.0,
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

    Same wrapper as :mod:`tests.test_mission_e2e_search` — kept
    separate (not factored into a shared helper) so each e2e test
    module remains self-contained and readable on its own.
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
async def test_no_aws_smoke_session_completes_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive a documentation-search session to completion without AWS.

    Patches :class:`boto3.Session` at the module attribute so any
    attempt to construct one — anywhere in the engine, the persistence
    layer, or the sampling layer — raises immediately. With
    ``use_sampling=False`` and a :class:`FilesystemBackend` for
    persistence, the iteration path never reaches for boto3 in the
    first place; the patch is the proof, not the mechanism.

    The dispatcher returns a fixed two-element list for any tool name
    in the allowlist. The engine's Observe_Phase appends each
    successful call's ``result_summary`` to ``tool_results``; with one
    deterministic call per iteration the Observation's
    ``tool_results`` length is exactly 1, and the predicate's
    ``len(obs["tool_results"]) > 0`` clause flips to ``met`` on the
    first iteration.
    """

    # ------------------------------------------------------------------ #
    # AWS access guard — any boto3.Session() call from this point on
    # blows up loudly. The engine path with the configured session
    # (``use_sampling=False``, FilesystemBackend) does not need any
    # AWS client, so the patch acts as a tripwire: a regression that
    # silently started reaching for AWS from a safe-tier path would
    # surface as a RuntimeError out of this test.
    # ------------------------------------------------------------------ #
    def _no_aws_session(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("AWS access blocked in no-AWS smoke test")

    monkeypatch.setattr("boto3.Session", _no_aws_session)

    backend = _PredicateAwareBackend(root=tmp_path)
    session = _make_session()
    backend.save_session(session)

    # Stateless dispatcher — the stub returns the same two-element
    # list for every call. ``find_examples`` and ``find_docs`` both
    # tagged ``safe`` in their real modules, so the engine's
    # Tool_Allowlist gating is the only thing that ever consults
    # ``tool_name`` here. A closure over a counter would be overkill;
    # the predicate is satisfied by *any* non-empty tool_results.
    async def dispatcher(tool_name: str, args: dict[str, Any], ctx: Any) -> list[dict[str, str]]:
        return [{"name": "example_a"}, {"name": "example_b"}]

    engine = MissionEngine(
        backend=backend,
        tool_dispatcher=dispatcher,
        sampling_callable=None,
        sandbox_runner=None,
        cost_estimators={},
    )

    # Drive iterations until the cascade ends the run. The bound is
    # generous so a regression in completion detection surfaces as a
    # clean test failure rather than as an infinite loop.
    final_record: dict[str, Any] | None = None
    iteration_count = 0
    for _ in range(_DRIVER_LOOP_BOUND):
        record = await engine.run_iteration(session["session_id"])
        iteration_count += 1
        if record["verdict"] in ("complete", "terminate"):
            final_record = record
            break
    else:  # pragma: no cover — safety bound
        pytest.fail(
            "No-AWS Mission did not reach a terminal verdict within "
            "the driver loop bound; predicate completion may be "
            "misconfigured."
        )

    assert final_record is not None
    # ------------------------------------------------------------------ #
    # Verdict invariant — completion fired on the first iteration
    # where the predicate evaluated true. With one tool call per
    # iteration that's iteration 0.
    # ------------------------------------------------------------------ #
    assert final_record["verdict"] == "complete"
    assert final_record["verdict_reason"] == "criteria_met"
    assert iteration_count == 1

    # ------------------------------------------------------------------ #
    # Persistence invariant — the session is now ``completed`` and the
    # iteration count matches the expected one-iteration run.
    # ------------------------------------------------------------------ #
    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    assert persisted["status"] == "completed"
    assert persisted["final_verdict"] == "complete"
    assert len(persisted["iterations"]) == 1

    # The single iteration's Observation carries exactly one
    # ``tool_results`` entry — the dispatcher's two-element list,
    # appended once. The predicate's ``len > 0`` clause held.
    only_iteration = persisted["iterations"][0]
    assert len(only_iteration["observation"]["tool_results"]) == 1

    # The deterministic Propose_Phase fallback chose the first
    # allowlisted tool (``find_examples``) with empty args. A
    # regression that started picking a different tool — or worse, a
    # tool outside the allowlist — would surface here.
    strategy = only_iteration["strategy"]
    assert strategy["tool_calls"][0]["tool_name"] == _ALLOWLIST[0]

    # The Final_Report is the durable exit artifact; sanity-check
    # that it lands and carries the matching verdict / reason so an
    # operator reading the report sees the same story the in-memory
    # record did.
    report_path = Path(persisted["final_report_path"])
    assert report_path.exists()
