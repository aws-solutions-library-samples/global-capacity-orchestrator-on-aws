"""Tests for the cumulative-metrics view and the ``metric_trend`` criterion.

These exercise the engine capability that lets a Mission criterion observe how
a metric moves *across iterations* — the history-aware counterpart to the
point-in-time ``metric_threshold`` kind. Three layers are covered:

* :meth:`MissionEngine._build_cumulative_observation` accumulating a
  ``metric_history`` series (oldest→newest, numeric-only) the same way it
  already accumulates ``tool_results``;
* :meth:`MissionEngine._evaluate_metric_trend` deciding met / unmet /
  inconclusive over that series per ``direction``, ``window``, and
  ``min_points``;
* :func:`mcp.mission.validation.validate_criteria` accepting a well-formed
  ``metric_trend`` criterion and rejecting the malformed shapes.

Pure unit tests: no live MCP server, no AWS, no LLM.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Match the import pattern used by every other Mission test module.
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))

from mission import validation  # noqa: E402
from mission.engine import MissionEngine  # noqa: E402
from mission.validation import MissionValidationError  # noqa: E402


def _session_with_metric_iterations(values: list[float | str], key: str = "loss") -> dict:
    """Build a minimal session whose iterations each carry one ``metrics`` reading.

    Each entry in ``values`` becomes one prior iteration whose Observation has
    ``metrics = {key: value}``. Non-numeric entries are included deliberately so
    tests can assert the accumulator skips them.
    """
    iterations = []
    for i, value in enumerate(values):
        iterations.append(
            {
                "iteration_index": i,
                "observation": {"metrics": {key: value}, "tool_results": []},
            }
        )
    return {"iterations": iterations}


# ---------------------------------------------------------------------------
# Cumulative metric_history accumulation
# ---------------------------------------------------------------------------


class TestCumulativeMetricHistory:
    """The cumulative observation accumulates an oldest→newest numeric series."""

    def test_history_orders_prior_then_current(self) -> None:
        """Prior iterations come first; the current reading lands last."""
        session = _session_with_metric_iterations([2.0, 1.5])
        current = {"metrics": {"loss": 1.0}, "tool_results": []}

        cumulative = MissionEngine._build_cumulative_observation(current, session)  # type: ignore[arg-type]

        assert cumulative["metric_history"]["loss"] == [2.0, 1.5, 1.0]

    def test_history_skips_non_numeric_and_bool(self) -> None:
        """Strings, None, and bools never enter the numeric series."""
        session = _session_with_metric_iterations([2.0, "n/a", True])
        current = {"metrics": {"loss": 1.0, "flag": False}, "tool_results": []}

        cumulative = MissionEngine._build_cumulative_observation(current, session)  # type: ignore[arg-type]

        # Only the two real numbers survive; bool/str are filtered.
        assert cumulative["metric_history"]["loss"] == [2.0, 1.0]
        assert "flag" not in cumulative["metric_history"]

    def test_per_iteration_metrics_stay_point_in_time(self) -> None:
        """The cumulative view's ``metrics`` is still the current reading only."""
        session = _session_with_metric_iterations([2.0, 1.5])
        current = {"metrics": {"loss": 1.0}, "tool_results": []}

        cumulative = MissionEngine._build_cumulative_observation(current, session)  # type: ignore[arg-type]

        # metrics is point-in-time (current), metric_history is the series.
        assert cumulative["metrics"] == {"loss": 1.0}
        assert cumulative["metric_history"]["loss"][-1] == 1.0


# ---------------------------------------------------------------------------
# metric_trend evaluator
# ---------------------------------------------------------------------------


class TestEvaluateMetricTrend:
    """Direct exercises of the metric_trend evaluator branches."""

    @staticmethod
    def _obs(series: list[float], key: str = "loss") -> dict:
        return {"metric_history": {key: series}}

    def test_decreasing_met(self) -> None:
        result = MissionEngine._evaluate_metric_trend(
            {"metric": "metrics.loss", "direction": "decreasing"},  # type: ignore[arg-type]
            self._obs([3.0, 2.0, 1.0]),
        )
        assert result[0] == "met"
        assert result[1]["delta"] == -2.0

    def test_decreasing_unmet_when_rising(self) -> None:
        result = MissionEngine._evaluate_metric_trend(
            {"metric": "metrics.loss", "direction": "decreasing"},  # type: ignore[arg-type]
            self._obs([1.0, 2.0, 3.0]),
        )
        assert result[0] == "unmet"

    def test_increasing_met(self) -> None:
        result = MissionEngine._evaluate_metric_trend(
            {"metric": "acc", "direction": "increasing"},  # type: ignore[arg-type]
            self._obs([0.1, 0.5, 0.9], key="acc"),
        )
        assert result[0] == "met"

    def test_non_increasing_allows_flat(self) -> None:
        """A flat series satisfies ``non_increasing`` (last <= first)."""
        result = MissionEngine._evaluate_metric_trend(
            {"metric": "metrics.loss", "direction": "non_increasing"},  # type: ignore[arg-type]
            self._obs([2.0, 2.0, 2.0]),
        )
        assert result[0] == "met"

    def test_decreasing_unmet_on_flat(self) -> None:
        """A flat series does NOT satisfy strict ``decreasing``."""
        result = MissionEngine._evaluate_metric_trend(
            {"metric": "metrics.loss", "direction": "decreasing"},  # type: ignore[arg-type]
            self._obs([2.0, 2.0, 2.0]),
        )
        assert result[0] == "unmet"

    def test_window_limits_to_recent_points(self) -> None:
        """``window`` considers only the most-recent N readings.

        The full series rises then falls; a window of 2 sees only the final
        two points (5.0 → 1.0), so ``decreasing`` is met even though the
        series as a whole started lower.
        """
        result = MissionEngine._evaluate_metric_trend(
            {"metric": "metrics.loss", "direction": "decreasing", "window": 2},  # type: ignore[arg-type]
            self._obs([1.0, 9.0, 5.0, 1.0]),
        )
        assert result[0] == "met"
        assert result[1]["points"] == [5.0, 1.0]

    def test_inconclusive_below_min_points(self) -> None:
        """A single reading is inconclusive — a trend needs at least two points."""
        result = MissionEngine._evaluate_metric_trend(
            {"metric": "metrics.loss", "direction": "decreasing"},  # type: ignore[arg-type]
            self._obs([1.0]),
        )
        assert result[0] == "inconclusive"
        assert result[1]["reason"] == "insufficient_history"

    def test_inconclusive_when_custom_min_points_unmet(self) -> None:
        """An explicit ``min_points`` raises the bar for a decision."""
        result = MissionEngine._evaluate_metric_trend(
            {"metric": "metrics.loss", "direction": "decreasing", "min_points": 4},  # type: ignore[arg-type]
            self._obs([3.0, 2.0, 1.0]),
        )
        assert result[0] == "inconclusive"
        assert result[1]["required_points"] == 4

    def test_inconclusive_when_history_missing(self) -> None:
        """No metric_history map on the observation is inconclusive, not a crash."""
        result = MissionEngine._evaluate_metric_trend(
            {"metric": "metrics.loss", "direction": "decreasing"},  # type: ignore[arg-type]
            {},
        )
        assert result[0] == "inconclusive"

    def test_inconclusive_when_series_empty_for_key(self) -> None:
        """A metric with no recorded history is inconclusive."""
        result = MissionEngine._evaluate_metric_trend(
            {"metric": "metrics.other", "direction": "decreasing"},  # type: ignore[arg-type]
            self._obs([1.0, 2.0]),
        )
        assert result[0] == "inconclusive"

    def test_bare_metric_name_resolves(self) -> None:
        """A bare key (no ``metrics.`` prefix) addresses the same series."""
        result = MissionEngine._evaluate_metric_trend(
            {"metric": "loss", "direction": "decreasing"},  # type: ignore[arg-type]
            self._obs([3.0, 1.0]),
        )
        assert result[0] == "met"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidateMetricTrend:
    """The validator accepts well-formed metric_trend and rejects bad shapes."""

    def test_accepts_minimal(self) -> None:
        criteria = [
            {
                "criterion_id": "loss_falling",
                "kind": "metric_trend",
                "required": True,
                "metric": "metrics.loss",
                "direction": "decreasing",
            }
        ]
        out = validation.validate_criteria(criteria)
        assert out[0]["kind"] == "metric_trend"
        assert out[0]["direction"] == "decreasing"

    def test_accepts_window_and_min_points(self) -> None:
        criteria = [
            {
                "criterion_id": "loss_falling",
                "kind": "metric_trend",
                "required": True,
                "metric": "metrics.loss",
                "direction": "non_increasing",
                "window": 5,
                "min_points": 3,
            }
        ]
        out = validation.validate_criteria(criteria)
        assert out[0]["window"] == 5
        assert out[0]["min_points"] == 3

    def test_rejects_missing_metric(self) -> None:
        criteria = [
            {
                "criterion_id": "bad",
                "kind": "metric_trend",
                "required": True,
                "direction": "decreasing",
            }
        ]
        with pytest.raises(MissionValidationError) as exc:
            validation.validate_criteria(criteria)
        assert exc.value.details["reason"] == "metric_missing_or_invalid"

    def test_rejects_bad_direction(self) -> None:
        criteria = [
            {
                "criterion_id": "bad",
                "kind": "metric_trend",
                "required": True,
                "metric": "metrics.loss",
                "direction": "sideways",
            }
        ]
        with pytest.raises(MissionValidationError) as exc:
            validation.validate_criteria(criteria)
        assert exc.value.details["reason"] == "direction_invalid"

    def test_rejects_non_positive_window(self) -> None:
        criteria = [
            {
                "criterion_id": "bad",
                "kind": "metric_trend",
                "required": True,
                "metric": "metrics.loss",
                "direction": "decreasing",
                "window": 0,
            }
        ]
        with pytest.raises(MissionValidationError) as exc:
            validation.validate_criteria(criteria)
        assert exc.value.details["reason"] == "window_must_be_positive_int"

    def test_rejects_non_positive_min_points(self) -> None:
        criteria = [
            {
                "criterion_id": "bad",
                "kind": "metric_trend",
                "required": True,
                "metric": "metrics.loss",
                "direction": "decreasing",
                "min_points": -1,
            }
        ]
        with pytest.raises(MissionValidationError) as exc:
            validation.validate_criteria(criteria)
        assert exc.value.details["reason"] == "min_points_must_be_positive_int"


# ---------------------------------------------------------------------------
# End-to-end through the criterion dispatch
# ---------------------------------------------------------------------------


class TestMetricTrendThroughDispatch:
    """The evaluate dispatch routes metric_trend against the cumulative view."""

    def test_dispatch_evaluates_trend_against_history(self) -> None:
        """A full evaluate pass marks a falling-loss trend as met.

        Drives the same path the engine uses: build the cumulative observation
        from a session with prior readings, then evaluate the criterion through
        the public dispatch helper so the routing (trend → cumulative_obs) is
        exercised, not just the leaf evaluator.
        """

        async def _noop_dispatcher(tool_name, args, ctx):  # type: ignore[no-untyped-def]
            return None

        engine = MissionEngine(
            backend=None,
            tool_dispatcher=_noop_dispatcher,
            sampling_callable=None,
            sandbox_runner=None,
        )
        session = _session_with_metric_iterations([3.0, 2.0])
        current = {"metrics": {"loss": 1.0}, "tool_results": []}
        cumulative = engine._build_cumulative_observation(current, session)  # type: ignore[arg-type]

        criterion = {
            "criterion_id": "loss_falling",
            "kind": "metric_trend",
            "required": True,
            "metric": "metrics.loss",
            "direction": "decreasing",
        }
        result = engine._evaluate_one_criterion(criterion, current, cumulative, session)  # type: ignore[arg-type]

        assert result["criterion_id"] == "loss_falling"
        assert result["status"] == "met"
