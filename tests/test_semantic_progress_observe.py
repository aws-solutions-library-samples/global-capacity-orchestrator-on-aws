"""Observe_Phase merge-contract integration test for the progress judge.

This module proves the load-bearing claim of the whole feature: a judge
tool's return value flows through the **real, unmodified** Mission engine and
lands where a ``metric_threshold`` or ``metric_trend`` criterion can read it —
and an error envelope flows through the same path and leaves the criterion
``inconclusive`` rather than failing the loop.

Nothing here patches, subclasses, or otherwise alters ``mcp/mission/engine.py``.
The test imports ``MissionEngine`` as-is and drives four of its surfaces
directly:

* :meth:`MissionEngine._build_observation` — the permissive
  ``metrics.update(result_metrics)`` merge that lifts a tool result's
  top-level ``metrics`` dict into the Observation;
* :meth:`MissionEngine._evaluate_metric_threshold` — the pure, point-in-time
  numeric comparison a ``progress_score >= 0.8`` criterion performs;
* :meth:`MissionEngine._build_cumulative_observation` — the accumulator that
  turns per-iteration ``metrics`` readings into an oldest→newest
  ``metric_history`` series; and
* :meth:`MissionEngine._evaluate_metric_trend` — the history-aware evaluator a
  ``progress_score`` increasing criterion uses to read that series.

A full :meth:`MissionEngine.run_iteration` session against a stub dispatcher
also confirms the success and error paths end-to-end through the production
Observe → Evaluate flow, mirroring the existing ``test_mission_e2e_*`` modules.

The judge-shaped inputs are built with the **real** judge helpers
``mission_judge.shape.metrics_result`` and ``mission_judge.shape.error_envelope``
(and the real ``mission_judge.rubric.RUBRIC_VERSION``) so the test exercises the
genuine wire shapes the judge emits, not hand-rolled look-alikes. The engine is
confirmed to consume them with no modification.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

# ``mcp/run_mcp.py`` puts ``mcp/`` on ``sys.path`` at runtime; pytest mirrors
# that before any ``mission.*`` / ``mission_judge.*`` import resolves. Same
# idiom used by every other ``test_mission_*`` and ``test_semantic_progress_*``
# module.
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))

from mission import SCHEMA_VERSION  # noqa: E402
from mission.engine import MissionEngine  # noqa: E402
from mission.state import FilesystemBackend  # noqa: E402
from mission.types import ToolCallRecord  # noqa: E402
from mission_judge.rubric import RUBRIC_VERSION  # noqa: E402
from mission_judge.shape import error_envelope, metrics_result  # noqa: E402

# The metric key the judge emits and the criterion reads back by dot-path.
_METRIC_KEY = "progress_score"
# The dot-path a criterion uses to resolve the merged value:
# ``metrics.<output_name>``.
_METRIC_PATH = f"metrics.{_METRIC_KEY}"
# The threshold the consuming criterion compares against.
_THRESHOLD = 0.8
# A score at or above the threshold (criterion met) and one below it (unmet).
_MET_VALUE = 0.85
_UNMET_VALUE = 0.6
# The two readings of an increasing run, oldest first.
_TREND_EARLY = 0.4
_TREND_LATER = 0.7

# Representative provenance the judge attaches beside ``metrics``. The exact
# values are irrelevant to the merge — only the top-level ``metrics`` dict is —
# but using realistic ones keeps the fixture honest.
_BACKEND_NAME = "bedrock"
_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
_SOURCE = f"{_BACKEND_NAME}:{_MODEL_ID}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _judge_success(value: float, *, raw_score: float | None = None) -> dict[str, Any]:
    """Build a judge success result carrying ``value`` under ``metrics``.

    Uses the real canonical-shape builder so the result has the exact wire
    shape the judge tool returns: the single score under a top-level
    ``metrics`` map with every provenance field placed beside it. ``raw_score``
    defaults to ``value`` (the in-range, no-clamp case).
    """
    return metrics_result(
        _METRIC_KEY,
        value,
        rationale="steady progress toward the objective",
        source=_SOURCE,
        backend_name=_BACKEND_NAME,
        model_id=_MODEL_ID,
        rubric_version=RUBRIC_VERSION,
        raw_score=value if raw_score is None else raw_score,
    )


def _minimal_engine() -> MissionEngine:
    """Construct a ``MissionEngine`` adequate for the direct merge tests.

    ``_build_observation`` touches neither the backend nor the dispatcher — it
    only reads ``self.now()`` and calls the static annotator — and the
    evaluators / accumulator are pure staticmethods. A real (if unused) async
    dispatcher is still passed so construction matches the engine's declared
    protocol rather than relying on ``None`` slipping through.
    """

    async def _noop_dispatcher(tool_name: str, args: dict[str, Any], ctx: Any) -> Any:
        return None

    return MissionEngine(
        backend=None,
        tool_dispatcher=_noop_dispatcher,
        sampling_callable=None,
        sandbox_runner=None,
    )


def _ok_call(result_summary: Any, tool_name: str = "metrics_semantic_progress") -> ToolCallRecord:
    """Wrap a tool result as a successful :class:`ToolCallRecord`.

    The Observe_Phase merge only fires for a call whose ``status`` is ``"ok"``
    and whose ``result_summary`` is a dict carrying a top-level ``metrics``
    dict — exactly the shape the judge returns on success. An error envelope is
    also a successful return (the tool did not raise; it reported a structured
    failure), so the error-envelope case below uses ``status="ok"`` too. That
    is the whole point: the merge, not the call status, is what skips the
    envelope.
    """
    return {
        "tool_name": tool_name,
        "args": {},
        "status": "ok",
        "result_summary": result_summary,
        "duration_ms": 1,
    }


def _threshold_criterion(op: str, target: float) -> dict[str, Any]:
    """Build a ``metric_threshold`` criterion reading ``metrics.progress_score``."""
    return {
        "criterion_id": f"progress_{op}_{target}",
        "kind": "metric_threshold",
        "required": True,
        "metric": _METRIC_PATH,
        "op": op,
        "target": target,
    }


def _trend_criterion(direction: str) -> dict[str, Any]:
    """Build a ``metric_trend`` criterion reading the ``progress_score`` series."""
    return {
        "criterion_id": f"progress_{direction}",
        "kind": "metric_trend",
        "required": True,
        "metric": _METRIC_PATH,
        "direction": direction,
    }


def _make_session(session_id: str, op: str, target: float) -> dict[str, Any]:
    """Build a one-criterion Mission session for the end-to-end driver.

    Mirrors the hand-built ``SessionState`` dicts in
    ``tests/test_mission_e2e_budget.py``: the typed fields are consumed by the
    engine directly (validators are exercised in their own module). The single
    criterion is a ``metric_threshold`` reading ``metrics.progress_score`` so
    the Observe_Phase merge is what decides its outcome.
    """
    return {
        "version": SCHEMA_VERSION,
        "session_id": session_id,
        "directive_text": "Observe a judge-emitted progress score via metric_threshold.",
        "criteria": [_threshold_criterion(op, target)],
        "budget": {"max_iterations": 1, "max_wall_clock_seconds": 600},
        "tool_allowlist": ["metrics_semantic_progress"],
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 100,
        "use_sampling": False,
        "allow_scripted_strategies": False,
        "status": "pending",
        "created_at": "2025-01-01T00:00:00Z",
        "iterations": [],
        "no_progress_counter": 0,
    }


def _result_dispatcher(result: dict[str, Any]) -> Any:
    """Return an async dispatcher that always emits ``result``.

    Stands in for the FastMCP tool dispatcher: the engine's Execute_Phase
    invokes it and stashes the return as the call's ``result_summary``, which
    the Observe_Phase then merges. ``tool_name`` / ``args`` are ignored because
    the engine alone enforces Tool_Allowlist gating before this is called.
    """

    async def dispatcher(tool_name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return dict(result)

    return dispatcher


# ---------------------------------------------------------------------------
# Direct merge-contract tests (smallest scope)
# ---------------------------------------------------------------------------


class TestObserveMergeContractDirect:
    """Pin the merge contract at the ``_build_observation`` boundary."""

    def test_success_result_merges_and_resolves_by_dot_path(self) -> None:
        """A judge success result lands at ``observation['metrics']['progress_score']``.

        The judge emits the canonical shape with provenance *outside*
        ``metrics``; after the engine's permissive
        ``metrics.update(result_metrics)`` merge, the dot-path the criterion
        uses resolves to exactly the emitted score, and the provenance never
        pollutes the merged ``metrics`` dict.
        """
        engine = _minimal_engine()
        result = _judge_success(_MET_VALUE)
        # Sanity: the judge keeps provenance out of the metrics object.
        assert result["metrics"] == {_METRIC_KEY: _MET_VALUE}
        assert "rationale" in result and "rationale" not in result["metrics"]
        assert "source" in result and "source" not in result["metrics"]

        observation = engine._build_observation([_ok_call(result)], datetime.now(UTC))

        assert observation["metrics"][_METRIC_KEY] == _MET_VALUE
        # Only the numeric value made it into the merged metrics dict.
        assert observation["metrics"] == {_METRIC_KEY: _MET_VALUE}

    def test_threshold_evaluates_met_and_unmet_deterministically(self) -> None:
        """The merged score drives ``progress_score >= 0.8`` both ways, repeatably.

        A 0.85 score satisfies ``>= 0.8`` (met) and a 0.6 score does not
        (unmet); the evidence the engine returns is the merged score itself.
        Each criterion is evaluated twice against the same observation to pin
        the determinism of the unchanged comparison — the same value always
        yields the same met/unmet result.
        """
        engine = _minimal_engine()

        met_obs = engine._build_observation(
            [_ok_call(_judge_success(_MET_VALUE))], datetime.now(UTC)
        )
        met_criterion = _threshold_criterion(">=", _THRESHOLD)
        first_met = MissionEngine._evaluate_metric_threshold(met_criterion, met_obs)
        second_met = MissionEngine._evaluate_metric_threshold(met_criterion, met_obs)
        assert first_met == ("met", _MET_VALUE)
        # Repeated evaluation of the unchanged comparison is identical.
        assert first_met == second_met

        unmet_obs = engine._build_observation(
            [_ok_call(_judge_success(_UNMET_VALUE))], datetime.now(UTC)
        )
        unmet_criterion = _threshold_criterion(">=", _THRESHOLD)
        first_unmet = MissionEngine._evaluate_metric_threshold(unmet_criterion, unmet_obs)
        second_unmet = MissionEngine._evaluate_metric_threshold(unmet_criterion, unmet_obs)
        assert first_unmet == ("unmet", _UNMET_VALUE)
        assert first_unmet == second_unmet

    def test_error_envelope_is_skipped_and_criterion_inconclusive(self) -> None:
        """An error envelope merges no metric and leaves the criterion undecided.

        The envelope ``{"code", "details"}`` has no top-level ``metrics`` key,
        so the permissive merge skips it: the merged ``metrics`` dict stays
        empty and the dot-path lookup misses, so the criterion is
        ``inconclusive`` (with ``metric_path_missing`` evidence) rather than
        failing the loop.
        """
        engine = _minimal_engine()
        envelope = error_envelope(
            "sampling_transport_error",
            transport_code="bedrock_no_credentials",
            backend_name=_BACKEND_NAME,
            model_id=_MODEL_ID,
        )
        # Sanity: the envelope structurally carries no top-level metrics key.
        assert "metrics" not in envelope
        assert envelope["code"] == "sampling_transport_error"

        observation = engine._build_observation([_ok_call(envelope)], datetime.now(UTC))
        # Nothing was merged.
        assert observation["metrics"] == {}

        status, evidence = MissionEngine._evaluate_metric_threshold(
            _threshold_criterion(">=", _THRESHOLD), observation
        )
        assert status == "inconclusive"
        assert isinstance(evidence, str)
        assert evidence.startswith("metric_path_missing:")

    def test_metric_trend_reads_progress_score_series(self) -> None:
        """A two-iteration cumulative view feeds an increasing ``progress_score`` trend.

        Both iterations' Observations are built from real judge success results
        through ``_build_observation``, then the engine's
        ``_build_cumulative_observation`` accumulates their point-in-time
        ``metrics`` readings into an oldest→newest ``metric_history`` series. A
        ``metric_trend`` criterion reads that ``progress_score`` series and, for
        a rising 0.4 → 0.7 run, evaluates the increasing direction as met; a
        decreasing direction over the same rising series is unmet.
        """
        engine = _minimal_engine()

        # Iteration 0: an earlier, lower score.
        prior_obs = engine._build_observation(
            [_ok_call(_judge_success(_TREND_EARLY))], datetime.now(UTC)
        )
        # Current iteration: a later, higher score.
        current_obs = engine._build_observation(
            [_ok_call(_judge_success(_TREND_LATER))], datetime.now(UTC)
        )
        assert prior_obs["metrics"] == {_METRIC_KEY: _TREND_EARLY}
        assert current_obs["metrics"] == {_METRIC_KEY: _TREND_LATER}

        session = {"iterations": [{"iteration_index": 0, "observation": prior_obs}]}
        cumulative = MissionEngine._build_cumulative_observation(current_obs, session)  # type: ignore[arg-type]

        # The accumulator reads the progress_score series oldest→newest.
        assert cumulative["metric_history"][_METRIC_KEY] == [_TREND_EARLY, _TREND_LATER]
        # The point-in-time metrics view still carries only the current reading.
        assert cumulative["metrics"] == {_METRIC_KEY: _TREND_LATER}

        met_status, met_evidence = MissionEngine._evaluate_metric_trend(
            _trend_criterion("increasing"), cumulative
        )
        assert met_status == "met"
        assert met_evidence["points"] == [_TREND_EARLY, _TREND_LATER]
        assert met_evidence["delta"] == pytest.approx(_TREND_LATER - _TREND_EARLY)

        # The same rising series does not satisfy a decreasing trend.
        unmet_status, _ = MissionEngine._evaluate_metric_trend(
            _trend_criterion("decreasing"), cumulative
        )
        assert unmet_status == "unmet"


# ---------------------------------------------------------------------------
# End-to-end driver tests (full run_iteration through the engine as-is)
# ---------------------------------------------------------------------------


class TestObserveMergeContractEndToEnd:
    """Confirm the merge through the production Observe → Evaluate path."""

    @pytest.mark.mission_e2e
    async def test_success_result_drives_criterion_met(self, tmp_path: Path) -> None:
        """A judge success result run through ``run_iteration`` evaluates met.

        The stub dispatcher returns the canonical judge shape; the unmodified
        engine merges it in Observe_Phase and evaluates the
        ``progress_score >= 0.8`` criterion as met in the iteration's
        ``criteria_evaluation``.
        """
        backend = FilesystemBackend(root=tmp_path)
        session = _make_session("sess-judge-met", ">=", _THRESHOLD)
        backend.save_session(session)

        engine = MissionEngine(
            backend=backend,
            tool_dispatcher=_result_dispatcher(_judge_success(_MET_VALUE)),
            sampling_callable=None,
            sandbox_runner=None,
        )

        record = await engine.run_iteration(session["session_id"])

        assert record["observation"]["metrics"][_METRIC_KEY] == _MET_VALUE
        results = record["criteria_evaluation"]
        assert len(results) == 1
        assert results[0]["status"] == "met"
        assert results[0]["evidence"] == _MET_VALUE

    @pytest.mark.mission_e2e
    async def test_error_envelope_drives_criterion_inconclusive(self, tmp_path: Path) -> None:
        """A judge error envelope run through ``run_iteration`` is inconclusive.

        The dispatcher returns an error envelope (no top-level ``metrics``); the
        unmodified engine merges nothing, so the criterion is ``inconclusive``
        and the loop is never failed on bad data.
        """
        backend = FilesystemBackend(root=tmp_path)
        session = _make_session("sess-judge-inconclusive", ">=", _THRESHOLD)
        backend.save_session(session)

        engine = MissionEngine(
            backend=backend,
            tool_dispatcher=_result_dispatcher(error_envelope("no_sampling_backend")),
            sampling_callable=None,
            sandbox_runner=None,
        )

        record = await engine.run_iteration(session["session_id"])

        # No metric was merged from the envelope.
        assert record["observation"]["metrics"] == {}
        results = record["criteria_evaluation"]
        assert len(results) == 1
        assert results[0]["status"] == "inconclusive"
