"""Observe_Phase merge-contract integration test.

This module proves the load-bearing claim of the whole feature: a metric-reader
tool's return value flows through the **real, unmodified** Mission engine and
lands where a ``metric_threshold`` criterion can read it — and an error
envelope flows through the same path and leaves the criterion ``inconclusive``
rather than failing the loop.

Nothing here patches, subclasses, or otherwise alters
``gco_mcp/mission/engine.py``. The test imports ``MissionEngine`` as-is and drives
two surfaces of it:

* the private :meth:`MissionEngine._build_observation` /
  :meth:`MissionEngine._evaluate_metric_threshold` pair directly, so the merge
  contract is pinned at the smallest possible scope (the exact
  ``metrics.update(result_metrics)`` merge the engine performs); and
* a full :meth:`MissionEngine.run_iteration` session against a stub dispatcher
  that returns a reader-shaped result, mirroring the existing
  ``test_mission_e2e_*`` modules, so the merge is also confirmed end-to-end
  through the production Observe → Evaluate path.

The reader-shaped inputs are built with the **real** reader helpers
``metric_readers.shape.metrics_result`` and ``metric_readers.shape.error_envelope``
so the test exercises the genuine wire shapes, not hand-rolled look-alikes.

Validates the merge contract: the merged Observation resolves
``metrics.<name>`` to the emitted Numeric_Value; an error envelope has no
top-level ``metrics`` key, so the criterion is left inconclusive; and the
reader result is consumable by a ``metric_threshold`` criterion against the
engine as-is.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

# ``gco_mcp/run_mcp.py`` puts ``gco_mcp/`` on ``sys.path`` at runtime; pytest mirrors
# that before any ``mission.*`` / ``metric_readers.*`` import resolves. Same
# idiom used by every other ``test_mission_*`` and ``test_metric_readers_*``
# module.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from metric_readers.shape import error_envelope, metrics_result  # noqa: E402
from mission import SCHEMA_VERSION  # noqa: E402
from mission.engine import MissionEngine  # noqa: E402
from mission.state import FilesystemBackend  # noqa: E402
from mission.types import ToolCallRecord  # noqa: E402

# The metric key the reader emits and the criterion reads back by dot-path.
_METRIC_KEY = "loss"
# The Numeric_Value the reader emits. Chosen so a ``< 0.5`` threshold is met
# and a ``< 0.4`` threshold is unmet, exercising both decided outcomes.
_METRIC_VALUE = 0.42
# The dot-path a ``metric_threshold`` criterion uses to resolve the merged
# value: ``metrics.<caller_supplied_name>``.
_METRIC_PATH = f"metrics.{_METRIC_KEY}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_engine() -> MissionEngine:
    """Construct a ``MissionEngine`` adequate for the direct merge tests.

    ``_build_observation`` and ``_evaluate_metric_threshold`` touch neither the
    backend nor the dispatcher — the former only reads ``self.now()`` and calls
    the static annotator, the latter is a pure staticmethod. We still pass a
    real (if unused) async dispatcher so the construction matches the engine's
    declared protocol rather than relying on ``None`` slipping through.
    """

    async def _noop_dispatcher(tool_name: str, args: dict[str, Any], ctx: Any) -> Any:
        return None

    return MissionEngine(
        backend=None,
        tool_dispatcher=_noop_dispatcher,
        sampling_callable=None,
        sandbox_runner=None,
    )


def _ok_call(result_summary: Any, tool_name: str) -> ToolCallRecord:
    """Wrap a tool result as a successful :class:`ToolCallRecord`.

    The Observe_Phase merge only fires for a call whose ``status`` is ``"ok"``
    and whose ``result_summary`` is a dict carrying a top-level ``metrics``
    dict — exactly the shape a reader tool returns. An error envelope is also
    a successful return (the tool did not raise; it reported a structured
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


def _metric_threshold_criterion(op: str, target: float) -> dict[str, Any]:
    """Build a ``metric_threshold`` criterion reading ``metrics.loss``."""
    return {
        "criterion_id": f"loss_{op}_{target}",
        "kind": "metric_threshold",
        "required": True,
        "metric": _METRIC_PATH,
        "op": op,
        "target": target,
    }


def _make_session(session_id: str, op: str, target: float) -> dict[str, Any]:
    """Build a one-criterion Mission session for the end-to-end driver.

    Mirrors the hand-built ``SessionState`` dicts in
    ``tests/test_mission_e2e_budget.py``: the typed fields are consumed by the
    engine directly (validators are exercised in their own module). The single
    criterion is a ``metric_threshold`` reading ``metrics.loss`` so the
    Observe_Phase merge is what decides its outcome.
    """
    return {
        "version": SCHEMA_VERSION,
        "session_id": session_id,
        "directive_text": "Observe a reader-emitted scalar via metric_threshold.",
        "criteria": [_metric_threshold_criterion(op, target)],
        "budget": {"max_iterations": 1, "max_wall_clock_seconds": 600},
        "tool_allowlist": ["metrics_cloudwatch_get"],
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 100,
        "use_sampling": False,
        "allow_scripted_strategies": False,
        "status": "pending",
        "created_at": "2025-01-01T00:00:00Z",
        "iterations": [],
        "no_progress_counter": 0,
    }


def _reader_result_dispatcher(result: dict[str, Any]) -> Any:
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
        """A reader success result lands at ``observation['metrics']['loss']``.

        The reader emits the canonical shape with provenance *outside*
        ``metrics``; after the engine's permissive
        ``metrics.update(result_metrics)`` merge, the dot-path the criterion
        uses resolves to exactly the emitted Numeric_Value, and the provenance
        never pollutes the merged ``metrics`` dict.
        """
        engine = _minimal_engine()
        result = metrics_result(
            _METRIC_KEY,
            _METRIC_VALUE,
            source="cloudwatch:Loss",
            region="us-east-1",
        )
        # Sanity: the reader keeps provenance out of the metrics object.
        assert result["metrics"] == {_METRIC_KEY: _METRIC_VALUE}
        assert "source" in result and "source" not in result["metrics"]

        observation = engine._build_observation(
            [_ok_call(result, "metrics_cloudwatch_get")],
            datetime.now(UTC),
        )

        assert observation["metrics"][_METRIC_KEY] == _METRIC_VALUE
        # Only the numeric value made it into the merged metrics dict.
        assert observation["metrics"] == {_METRIC_KEY: _METRIC_VALUE}

    def test_merged_value_evaluates_met_and_unmet(self) -> None:
        """The merged value drives a ``metric_threshold`` criterion both ways.

        ``metrics.loss < 0.5`` is met and ``metrics.loss < 0.4`` is unmet for
        the emitted 0.42, and the evidence the engine returns is the merged
        Numeric_Value itself.
        """
        engine = _minimal_engine()
        observation = engine._build_observation(
            [_ok_call(metrics_result(_METRIC_KEY, _METRIC_VALUE), "metrics_cloudwatch_get")],
            datetime.now(UTC),
        )

        met_status, met_evidence = MissionEngine._evaluate_metric_threshold(
            _metric_threshold_criterion("<", 0.5), observation
        )
        assert met_status == "met"
        assert met_evidence == _METRIC_VALUE

        unmet_status, unmet_evidence = MissionEngine._evaluate_metric_threshold(
            _metric_threshold_criterion("<", 0.4), observation
        )
        assert unmet_status == "unmet"
        assert unmet_evidence == _METRIC_VALUE

    def test_error_envelope_is_skipped_and_criterion_inconclusive(self) -> None:
        """An error envelope merges no metric and leaves the criterion undecided.

        The envelope ``{"code", "details"}`` has no top-level ``metrics`` key,
        so the permissive merge skips it: the merged ``metrics`` dict stays
        empty and the dot-path lookup misses, so the criterion is
        ``inconclusive`` (with ``metric_path_missing`` evidence) rather than
        failing the loop.
        """
        engine = _minimal_engine()
        envelope = error_envelope("file_not_found", path="x")
        # Sanity: the envelope structurally carries no top-level metrics key.
        assert "metrics" not in envelope
        assert envelope["code"] == "file_not_found"

        observation = engine._build_observation(
            [_ok_call(envelope, "metrics_from_shared_storage_file")],
            datetime.now(UTC),
        )
        # Nothing was merged.
        assert observation["metrics"] == {}

        status, evidence = MissionEngine._evaluate_metric_threshold(
            _metric_threshold_criterion("<", 0.5), observation
        )
        assert status == "inconclusive"
        assert isinstance(evidence, str)
        assert evidence.startswith("metric_path_missing:")


# ---------------------------------------------------------------------------
# End-to-end driver tests (full run_iteration through the engine as-is)
# ---------------------------------------------------------------------------


class TestObserveMergeContractEndToEnd:
    """Confirm the merge through the production Observe → Evaluate path."""

    @pytest.mark.mission_e2e
    async def test_success_result_drives_criterion_met(self, tmp_path: Path) -> None:
        """A reader success result run through ``run_iteration`` evaluates met.

        The stub dispatcher returns the canonical reader shape; the unmodified
        engine merges it in Observe_Phase and evaluates the ``metrics.loss <
        0.5`` criterion as met in the iteration's ``criteria_evaluation``.
        """
        backend = FilesystemBackend(root=tmp_path)
        session = _make_session("sess-observe-met", "<", 0.5)
        backend.save_session(session)

        engine = MissionEngine(
            backend=backend,
            tool_dispatcher=_reader_result_dispatcher(
                metrics_result(_METRIC_KEY, _METRIC_VALUE, source="cloudwatch", region="us-east-1")
            ),
            sampling_callable=None,
            sandbox_runner=None,
        )

        record = await engine.run_iteration(session["session_id"])

        observation = record["observation"]
        assert observation["metrics"][_METRIC_KEY] == _METRIC_VALUE

        results = record["criteria_evaluation"]
        assert len(results) == 1
        assert results[0]["status"] == "met"
        assert results[0]["evidence"] == _METRIC_VALUE

    @pytest.mark.mission_e2e
    async def test_success_result_drives_criterion_unmet(self, tmp_path: Path) -> None:
        """The same merged value evaluates unmet against a tighter threshold.

        ``metrics.loss < 0.4`` is unmet for the emitted 0.42 — the merge is
        identical; only the target differs.
        """
        backend = FilesystemBackend(root=tmp_path)
        session = _make_session("sess-observe-unmet", "<", 0.4)
        backend.save_session(session)

        engine = MissionEngine(
            backend=backend,
            tool_dispatcher=_reader_result_dispatcher(
                metrics_result(_METRIC_KEY, _METRIC_VALUE, source="cloudwatch", region="us-east-1")
            ),
            sampling_callable=None,
            sandbox_runner=None,
        )

        record = await engine.run_iteration(session["session_id"])

        assert record["observation"]["metrics"][_METRIC_KEY] == _METRIC_VALUE
        results = record["criteria_evaluation"]
        assert len(results) == 1
        assert results[0]["status"] == "unmet"
        assert results[0]["evidence"] == _METRIC_VALUE

    @pytest.mark.mission_e2e
    async def test_error_envelope_drives_criterion_inconclusive(self, tmp_path: Path) -> None:
        """A reader error envelope run through ``run_iteration`` is inconclusive.

        The dispatcher returns an error envelope (no top-level ``metrics``); the
        unmodified engine merges nothing, so the criterion is ``inconclusive``
        and the loop is never failed on bad data.
        """
        backend = FilesystemBackend(root=tmp_path)
        session = _make_session("sess-observe-inconclusive", "<", 0.5)
        backend.save_session(session)

        engine = MissionEngine(
            backend=backend,
            tool_dispatcher=_reader_result_dispatcher(error_envelope("file_not_found", path="x")),
            sampling_callable=None,
            sandbox_runner=None,
        )

        record = await engine.run_iteration(session["session_id"])

        # No metric was merged from the envelope.
        assert record["observation"]["metrics"] == {}
        results = record["criteria_evaluation"]
        assert len(results) == 1
        assert results[0]["status"] == "inconclusive"
