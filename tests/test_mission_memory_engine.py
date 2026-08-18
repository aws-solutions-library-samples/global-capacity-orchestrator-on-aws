"""Engine and prompt wiring tests for mission memory.

Three surfaces, all hermetic (no AWS, no Bedrock):

* **Terminal-verdict write path** — ``MissionEngine._maybe_write_memory``
  fires after the Final_Report lands, reuses the sampled overlay instead
  of re-sampling, and swallows every store failure. The fault-injection
  cases pin the design's degradation contract: an absent table, a
  backfilling index, Bedrock being down, and an unexpected store bug all
  leave the mission completed with its report on disk.
* **Prior-missions prompt block** — the optional ``prior_missions``
  field on ``SamplingPrompt`` renders under ``=== Prior similar
  missions ===`` with its own byte-cap truncation domain, and its
  ``None`` default keeps the prompt byte-identical to the pre-memory
  shape (the property the determinism suite pins down).
* **Factory retrieval** — the Strategy_Revision sampler closure in
  ``mission._engine_factory`` retrieves similar past missions once per
  wiring (cached), passes them through to
  ``maybe_sample_strategy_revision``, and degrades to ``None`` on an
  empty result, a raising store, or no store at all. Retrieval is
  inherently gated on ``use_sampling`` because the closure only exists
  for sampling sessions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

# Mirror the import pattern used by every other ``test_mission_*`` module.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))
from mission import SCHEMA_VERSION  # noqa: E402
from mission import _engine_factory as engine_factory  # noqa: E402
from mission.embeddings import EmbeddingError  # noqa: E402
from mission.engine import MissionEngine  # noqa: E402
from mission.memory import (  # noqa: E402
    MissionMemoryError,
    MissionMemoryUnavailableError,
)
from mission.sampling import (  # noqa: E402
    PRIOR_MISSIONS_BYTE_CAP,
    PROMPT_BYTE_BUDGET,
    TRUNCATION_MARKER,
    SamplingPrompt,
    _summarise_prior_missions,
)
from mission.state import FilesystemBackend  # noqa: E402

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _make_session(
    *,
    session_id: str = "sess-memory-001",
    max_iterations: int = 10,
    use_sampling: bool = False,
    criteria: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A minimally-populated session dict, tuned to complete in one iteration."""
    if criteria is None:
        criteria = [
            {
                "criterion_id": "loss-under-tenth",
                "kind": "metric_threshold",
                "required": True,
                "metric": "metrics.loss",
                "op": "<",
                "target": 0.1,
            }
        ]
    return {
        "version": SCHEMA_VERSION,
        "session_id": session_id,
        "directive_text": f"Drive {session_id} to a stable state.",
        "criteria": criteria,
        "budget": {"max_iterations": max_iterations, "max_wall_clock_seconds": 600},
        "tool_allowlist": ["fake_tool"],
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 10,
        "use_sampling": use_sampling,
        "allow_scripted_strategies": False,
        "status": "pending",
        "created_at": "2025-01-01T00:00:00Z",
        "iterations": [],
        "no_progress_counter": 0,
    }


async def _completing_dispatcher(tool_name: str, args: dict, ctx: Any) -> dict:
    """Tool result whose metrics satisfy the default criterion immediately."""
    return {"metrics": {"loss": 0.05}}


class _RecordingMemoryStore:
    """Duck-typed store that records ``write_memory`` calls."""

    def __init__(self, backend_root: Path | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._backend_root = backend_root

    def write_memory(
        self,
        session: dict[str, Any],
        verdict: str,
        reason: str,
        lessons: str,
        followups: list[str],
    ) -> None:
        report_exists = None
        if self._backend_root is not None:
            report_exists = (self._backend_root / f"{session['session_id']}.report.json").exists()
        self.calls.append(
            {
                "session_id": session["session_id"],
                "verdict": verdict,
                "reason": reason,
                "lessons": lessons,
                "followups": followups,
                "report_already_on_disk": report_exists,
            }
        )


class _RaisingMemoryStore:
    """Duck-typed store whose ``write_memory`` always raises."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.attempts = 0

    def write_memory(self, *args: Any, **kwargs: Any) -> None:
        self.attempts += 1
        raise self._exc


# ---------------------------------------------------------------------------
# Terminal-verdict write path
# ---------------------------------------------------------------------------


class TestTerminalMemoryWrite:
    @pytest.mark.parametrize(
        "exc",
        [
            MissionMemoryUnavailableError("table not found — not provisioned"),
            MissionMemoryUnavailableError("SearchVectors failed — index backfilling"),
            EmbeddingError("embedding_transport_failure"),
            MissionMemoryError("mission-memory write failed: throttled"),
            RuntimeError("unexpected store bug"),
        ],
        ids=["absent-table", "backfilling-index", "bedrock-down", "hard-store-error", "bug"],
    )
    async def test_store_failure_never_fails_the_mission(
        self, tmp_path: Path, exc: Exception
    ) -> None:
        backend = FilesystemBackend(root=tmp_path)
        session = _make_session()
        backend.save_session(session)
        store = _RaisingMemoryStore(exc)
        engine = MissionEngine(
            backend=backend,
            tool_dispatcher=_completing_dispatcher,
            sampling_callable=None,
            sandbox_runner=None,
            memory_store=store,
        )

        record = await engine.run_iteration(session["session_id"])

        assert store.attempts == 1  # the write was attempted, then swallowed
        assert record["verdict"] == "complete"
        persisted = backend.load_session(session["session_id"])
        assert persisted is not None
        assert persisted["status"] == "completed"
        # The Final_Report — the durable exit artifact — landed anyway.
        assert (tmp_path / f"{session['session_id']}.report.json").exists()

    async def test_no_store_is_a_clean_noop(self, tmp_path: Path) -> None:
        backend = FilesystemBackend(root=tmp_path)
        session = _make_session()
        backend.save_session(session)
        engine = MissionEngine(
            backend=backend,
            tool_dispatcher=_completing_dispatcher,
            sampling_callable=None,
            sandbox_runner=None,
        )

        record = await engine.run_iteration(session["session_id"])

        assert record["verdict"] == "complete"
        assert (tmp_path / f"{session['session_id']}.report.json").exists()

    async def test_write_uses_templated_narrative_after_the_report(self, tmp_path: Path) -> None:
        backend = FilesystemBackend(root=tmp_path)
        session = _make_session()
        backend.save_session(session)
        store = _RecordingMemoryStore(backend_root=tmp_path)
        engine = MissionEngine(
            backend=backend,
            tool_dispatcher=_completing_dispatcher,
            sampling_callable=None,
            sandbox_runner=None,
            memory_store=store,
        )

        await engine.run_iteration(session["session_id"])

        (call,) = store.calls
        assert call["session_id"] == session["session_id"]
        assert call["verdict"] == "complete"
        assert call["reason"] == "criteria_met"
        # No sampling: the narrative comes from the deterministic
        # templates and must match what the report on disk recorded.
        report = json.loads((tmp_path / f"{session['session_id']}.report.json").read_text())
        assert call["lessons"] == report["lessons"]
        assert call["followups"] == report["recommended_followups"]
        assert isinstance(call["lessons"], str) and call["lessons"].strip()
        # Ordering contract: the report is the durable artifact and is
        # written first; memory is strictly additive after it.
        assert call["report_already_on_disk"] is True

    async def test_sampled_overlay_is_reused_not_resampled(self, tmp_path: Path) -> None:
        backend = FilesystemBackend(root=tmp_path)
        session = _make_session(use_sampling=True)
        backend.save_session(session)
        store = _RecordingMemoryStore()
        sample_calls: list[int] = []

        async def final_lessons(*, session: dict[str, Any]) -> dict[str, Any]:
            sample_calls.append(1)
            return {
                "lessons": "Sampled lessons paragraph.",
                "recommended_followups": ["Sampled follow-up."],
            }

        engine = MissionEngine(
            backend=backend,
            tool_dispatcher=_completing_dispatcher,
            sampling_callable=None,
            sandbox_runner=None,
            final_lessons_callable=final_lessons,
            memory_store=store,
        )

        await engine.run_iteration(session["session_id"])

        assert len(sample_calls) == 1  # one overlay fetch total — never re-sampled
        (call,) = store.calls
        assert call["lessons"] == "Sampled lessons paragraph."
        assert call["followups"] == ["Sampled follow-up."]

    async def test_terminate_verdict_also_writes(self, tmp_path: Path) -> None:
        backend = FilesystemBackend(root=tmp_path)
        session = _make_session(
            max_iterations=1,
            criteria=[
                {
                    "criterion_id": "unreachable",
                    "kind": "metric_threshold",
                    "required": True,
                    "metric": "metrics.loss",
                    "op": "<",
                    "target": -1.0,
                }
            ],
        )
        backend.save_session(session)
        store = _RecordingMemoryStore()
        engine = MissionEngine(
            backend=backend,
            tool_dispatcher=_completing_dispatcher,
            sampling_callable=None,
            sandbox_runner=None,
            memory_store=store,
        )

        record = await engine.run_iteration(session["session_id"])

        assert record["verdict"] == "terminate"
        (call,) = store.calls
        assert call["verdict"] == "terminate"
        assert call["reason"] == "max_iterations"


# ---------------------------------------------------------------------------
# Prior-missions prompt block
# ---------------------------------------------------------------------------


def _make_prompt(**overrides: Any) -> SamplingPrompt:
    defaults: dict[str, Any] = {
        "directive": "Reduce validation loss below 0.5",
        "success_criteria": [],
        "criteria_status": [],
        "recent_iterations": [],
        "tool_allowlist": ["find_examples"],
        "tool_docstrings": {"find_examples": "Search the example catalog."},
        "remaining_iterations": 5,
        "remaining_wall_clock_secs": 300.0,
    }
    defaults.update(overrides)
    return SamplingPrompt(**defaults)


def _mission(index: int, *, lessons: str = "Use spot pools early.") -> dict[str, Any]:
    return {
        "session_id": f"sess-prior-{index:03d}",
        "directive": f"Prior directive number {index:03d}",
        "lessons": lessons,
        "recommended_followups": ["Check quotas first."],
        "final_verdict": "complete",
        "verdict_reason": "criteria_met",
        "iteration_count": 4,
        "completed_at": "2026-08-01T00:00:00+00:00",
        "score": round(1.0 - index * 0.01, 2),
    }


class TestPriorMissionsPromptBlock:
    def test_absent_by_default_and_when_none(self) -> None:
        # The byte-identical contract: pre-memory prompts are unchanged.
        assert _make_prompt().assemble() == _make_prompt(prior_missions=None).assemble()
        assert "Prior similar missions" not in _make_prompt().assemble()

    def test_section_renders_most_similar_first(self) -> None:
        text = _make_prompt(prior_missions=[_mission(1), _mission(2)]).assemble()
        assert "=== Prior similar missions (institutional memory) ===" in text
        assert "Prior directive number 001" in text
        assert "Prior directive number 002" in text
        assert text.index("Prior directive number 001") < text.index("Prior directive number 002")
        assert "Use spot pools early." in text

    def test_assemble_is_deterministic(self) -> None:
        prompt = _make_prompt(prior_missions=[_mission(1), _mission(2), _mission(3)])
        assert prompt.assemble() == prompt.assemble()

    def test_oversized_list_drops_least_similar_first(self) -> None:
        # 60 missions with ~300-byte lessons blow well past the 4 KiB cap;
        # the tail (least similar) must be dropped, never the head.
        missions = [_mission(i, lessons="x" * 300) for i in range(60)]
        text = _make_prompt(prior_missions=missions).assemble()
        assert "Prior directive number 000" in text
        assert "Prior directive number 059" not in text
        assert _utf8(text) <= PROMPT_BYTE_BUDGET

        summarised = _summarise_prior_missions(missions)
        assert 0 < len(summarised) < len(missions)
        assert _utf8(json.dumps(summarised)) <= PRIOR_MISSIONS_BYTE_CAP + 64

    def test_giant_lessons_field_is_truncated_not_fatal(self) -> None:
        missions = [_mission(0, lessons="L" * 10_000)]
        summarised = _summarise_prior_missions(missions)
        assert summarised, "a single oversize mission must survive via field truncation"
        assert summarised[0]["lessons"].endswith(TRUNCATION_MARKER)
        text = _make_prompt(prior_missions=missions).assemble()
        assert _utf8(text) <= PROMPT_BYTE_BUDGET

    def test_summary_filters_unknown_fields_and_sorts_keys(self) -> None:
        mission = _mission(0)
        mission["directive_embedding"] = [0.1] * 8  # projection drift must not leak
        mission["some_future_field"] = "nope"
        (entry,) = _summarise_prior_missions([mission])
        assert "directive_embedding" not in entry
        assert "some_future_field" not in entry
        assert list(entry.keys()) == sorted(entry.keys())
        assert entry["session_id"] == "sess-prior-000"
        assert entry["score"] == pytest.approx(1.0)


def _utf8(text: str) -> int:
    return len(text.encode("utf-8"))


# ---------------------------------------------------------------------------
# Factory retrieval
# ---------------------------------------------------------------------------


class _StubSearchStore:
    """Duck-typed store for the retrieval path."""

    def __init__(
        self,
        results: list[dict[str, Any]] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._results = results if results is not None else []
        self._raises = raises
        self.queries: list[str] = []

    def search_similar(self, directive: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.queries.append(directive)
        if self._raises is not None:
            raise self._raises
        return list(self._results)


def _sampling_session() -> dict[str, Any]:
    session = _make_session(use_sampling=True)
    session["iterations"] = [{"iteration_index": 0, "criteria_evaluation": []}]
    return session


@pytest.fixture
def _factory_recorder(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Patch the factory's collaborators; return the recorded sampler kwargs.

    ``select_sampling_backend`` returns a sentinel so the closure builds,
    ``maybe_sample_strategy_revision`` records its kwargs instead of
    talking to a model, and the environment gather is pinned to ``None``
    so no AWS probe fires.
    """
    recorded: list[dict[str, Any]] = []

    monkeypatch.setattr(
        engine_factory.mission_sampling,
        "select_sampling_backend",
        lambda *a, **k: object(),
    )

    async def _record(**kwargs: Any) -> str:
        recorded.append(kwargs)
        return "recorded"

    monkeypatch.setattr(engine_factory.mission_sampling, "maybe_sample_strategy_revision", _record)
    monkeypatch.setattr("mission._environment.gather_session_environment", lambda session: None)
    return recorded


class TestFactoryRetrieval:
    async def test_results_flow_into_the_prompt_once(
        self, monkeypatch: pytest.MonkeyPatch, _factory_recorder: list[dict[str, Any]]
    ) -> None:
        store = _StubSearchStore(results=[_mission(1)])
        monkeypatch.setattr(engine_factory, "_build_memory_store", lambda: store)
        session = _sampling_session()

        sampler = engine_factory._build_sampling_callable(
            session, None, registered_tools={}, tool_docstrings={}
        )
        assert sampler is not None
        await sampler(session=session, ctx=None)
        await sampler(session=session, ctx=None)

        # One retrieval per wiring (cached), passed through on every call.
        assert store.queries == [session["directive_text"]]
        assert len(_factory_recorder) == 2
        for kwargs in _factory_recorder:
            assert kwargs["prior_missions"] == [_mission(1)]

    @pytest.mark.parametrize(
        "store_builder",
        [
            lambda: None,
            lambda: _StubSearchStore(results=[]),
            lambda: _StubSearchStore(raises=MissionMemoryUnavailableError("index backfilling")),
            lambda: _StubSearchStore(raises=EmbeddingError("embedding_transport_failure")),
        ],
        ids=["no-store", "empty-results", "unavailable", "bedrock-down"],
    )
    async def test_degrades_to_no_prior_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _factory_recorder: list[dict[str, Any]],
        store_builder: Any,
    ) -> None:
        monkeypatch.setattr(engine_factory, "_build_memory_store", store_builder)
        session = _sampling_session()

        sampler = engine_factory._build_sampling_callable(
            session, None, registered_tools={}, tool_docstrings={}
        )
        assert sampler is not None
        await sampler(session=session, ctx=None)

        (kwargs,) = _factory_recorder
        assert kwargs["prior_missions"] is None

    def test_suite_neutraliser_is_active(self) -> None:
        # The conftest autouse fixture patches the construction seam so no
        # test can reach the real SSM/Bedrock/DynamoDB path implicitly.
        # Memory-specific tests (like the ones above) patch over it.
        assert engine_factory._build_memory_store() is None

    async def test_dry_run_dependencies_stay_memory_free(self) -> None:
        deps = await engine_factory.build_engine_dependencies(
            _make_session(), None, use_stub_dispatcher=True
        )
        assert deps.memory_store is None
