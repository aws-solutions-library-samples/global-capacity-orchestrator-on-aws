"""Tests for the Mission environment-context gathering helper and the
sampling-prompt section it feeds.

Two surfaces under test:

1. ``gco_mcp/mission/_environment.py::gather_session_environment`` — the
   slow-moving live signals helper. Probes the multi-region capacity
   checker for per-region snapshots and the capacity reservation
   service for an active-count summary. The helper is intentionally
   defensive — every AWS call is wrapped, ``None`` is returned on
   total failure, and per-region partial failures land as zeroed
   :class:`RegionCapacity` shapes so the output is always
   JSON-serialisable.

2. ``gco_mcp/mission/sampling.py::SamplingPrompt.environment_context`` — the
   optional field that injects the gathered dict into the
   Strategy_Revision prompt's ``=== Environment context ===`` block.
   The block is byte-capped, key-sorted, and omitted entirely when
   ``environment_context is None`` so the existing byte-identical
   determinism property holds for callers that don't opt in.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Ensure gco_mcp/ is importable so ``from mission import _environment`` works
# without the ``mcp.`` package prefix that fastmcp would shadow.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from mission import _environment  # noqa: E402
from mission.sampling import (  # noqa: E402
    ENVIRONMENT_CONTEXT_BYTE_CAP,
    PROMPT_BYTE_BUDGET,
    SamplingPrompt,
    _summarise_environment_context,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_capacity(
    region: str,
    *,
    queue_depth: int = 0,
    running_jobs: int = 0,
    pending_jobs: int = 0,
    gpu_utilization: float = 0.0,
    cpu_utilization: float = 0.0,
    recommendation_score: float = 0.0,
) -> Any:
    """Build a minimal stand-in for ``RegionCapacity``."""
    cap = MagicMock()
    cap.region = region
    cap.queue_depth = queue_depth
    cap.running_jobs = running_jobs
    cap.pending_jobs = pending_jobs
    cap.gpu_utilization = gpu_utilization
    cap.cpu_utilization = cpu_utilization
    cap.recommendation_score = recommendation_score
    return cap


def _make_checker(
    capacities: list[Any] | None = None,
    *,
    raise_on_get_all: bool = False,
) -> Any:
    """Build a stub ``MultiRegionCapacityChecker``.

    ``capacities`` defaults to a two-region snapshot so the tests have
    something to render. Passing ``[]`` explicitly is honoured (the
    "no deployed regions" case). ``raise_on_get_all=True`` simulates
    an AWS failure during the cluster-metric probe.
    """
    checker = MagicMock()
    checker.config = MagicMock()
    if raise_on_get_all:
        checker.get_all_regions_capacity.side_effect = RuntimeError("aws boom")
    else:
        if capacities is None:
            capacities = [
                _make_capacity("us-east-1", queue_depth=3, gpu_utilization=42.0),
                _make_capacity("us-west-2", queue_depth=0, gpu_utilization=10.0),
            ]
        checker.get_all_regions_capacity.return_value = capacities
    return checker


# ---------------------------------------------------------------------------
# gather_session_environment
# ---------------------------------------------------------------------------


class TestGatherSessionEnvironment:
    def test_returns_none_when_no_checker_and_aws_unavailable(self, monkeypatch) -> None:
        """When no checker is supplied and the safe lookup fails,
        the helper returns ``None`` so the prompt omits the section."""
        monkeypatch.setattr(_environment, "_safe_get_checker", lambda: None)
        assert _environment.gather_session_environment(None) is None

    def test_uses_supplied_checker_and_renders_metrics(self, monkeypatch) -> None:
        """A supplied checker drives the gather without touching AWS."""
        # Stub reservation lookup so we don't reach into boto3 at all.
        monkeypatch.setattr(
            _environment,
            "_summarise_reservations",
            lambda checker, regions: {"active_count": 0, "by_region": dict.fromkeys(regions, 0)},
        )
        checker = _make_checker()
        result = _environment.gather_session_environment(None, multi_region_checker=checker)
        assert result is not None
        assert result["regions"] == ["us-east-1", "us-west-2"]
        assert result["cluster_metrics"]["us-east-1"]["queue_depth"] == 3
        assert result["cluster_metrics"]["us-east-1"]["gpu_utilization"] == 42.0
        # Reservations summary is the counts-only shape.
        assert "active_count" in result["reservations"]
        assert result["reservations"]["by_region"] == {"us-east-1": 0, "us-west-2": 0}

    def test_returns_none_when_checker_raises(self, monkeypatch) -> None:
        """Total AWS probe failure on cluster metrics yields ``None``."""
        checker = _make_checker(raise_on_get_all=True)
        result = _environment.gather_session_environment(None, multi_region_checker=checker)
        assert result is None

    def test_empty_capacities_returns_zeroed_skeleton(self, monkeypatch) -> None:
        """No deployed regions returns an empty-but-shape-stable dict."""
        monkeypatch.setattr(
            _environment,
            "_summarise_reservations",
            lambda checker, regions: {"active_count": 0, "by_region": {}},
        )
        checker = _make_checker(capacities=[])
        result = _environment.gather_session_environment(None, multi_region_checker=checker)
        assert result == {
            "regions": [],
            "cluster_metrics": {},
            "reservations": {"active_count": 0, "by_region": {}},
        }

    def test_no_timestamps_in_output(self, monkeypatch) -> None:
        """The output must not embed wall-clock state.

        The byte-identical determinism property in
        ``test_mission_sampling.test_assemble_is_deterministic`` would
        flap if the env block carried a timestamp.
        """
        monkeypatch.setattr(
            _environment,
            "_summarise_reservations",
            lambda checker, regions: {"active_count": 0, "by_region": dict.fromkeys(regions, 0)},
        )
        checker = _make_checker()
        result = _environment.gather_session_environment(None, multi_region_checker=checker)
        assert result is not None

        def _walk(node: Any) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    assert "timestamp" not in str(k).lower(), f"timestamp leaked at {k!r}"
                    _walk(v)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)
            elif isinstance(node, str):
                assert "T" not in node or not any(node.endswith(z) for z in ("Z", "+00:00")), (
                    f"ISO-8601-shaped string in env block: {node!r}"
                )

        _walk(result)


# ---------------------------------------------------------------------------
# _summarise_environment_context (the byte-cap + key-sort guarantees)
# ---------------------------------------------------------------------------


class TestSummariseEnvironmentContext:
    def test_small_input_passes_through_with_keys_sorted(self) -> None:
        """A small dict round-trips with sorted top-level keys."""
        env = {"zeta": 1, "alpha": 2, "mid": 3}
        out = _summarise_environment_context(env)
        # Insertion order tracks the sorted iteration we asked for.
        assert list(out.keys()) == ["alpha", "mid", "zeta"]
        assert out == {"alpha": 2, "mid": 3, "zeta": 1}

    def test_oversize_input_drops_largest_field_first(self) -> None:
        """When the dict overshoots the cap, the biggest field is pruned."""
        # Build a dict whose ``big`` field clearly dwarfs the others
        # so the dropper has an obvious target.
        env = {
            "tiny": "x",
            "small": "y" * 50,
            "big": "z" * (ENVIRONMENT_CONTEXT_BYTE_CAP + 100),
        }
        out = _summarise_environment_context(env)
        assert "big" not in out
        assert "tiny" in out
        assert "small" in out
        assert out["_dropped_fields"] == ["big"]

    def test_no_dropped_fields_key_when_nothing_dropped(self) -> None:
        """Clean inputs do not pollute the output with the dropped marker."""
        out = _summarise_environment_context({"a": 1})
        assert "_dropped_fields" not in out


# ---------------------------------------------------------------------------
# SamplingPrompt integration
# ---------------------------------------------------------------------------


def _bare_prompt(**overrides: Any) -> SamplingPrompt:
    """Construct a minimal SamplingPrompt with optional overrides."""
    base: dict[str, Any] = {
        "directive": "Test directive.",
        "success_criteria": [
            {
                "criterion_id": "x",
                "kind": "predicate",
                "required": True,
                "expression": "True",
            }
        ],
        "criteria_status": [],
        "recent_iterations": [],
        "tool_allowlist": ["find_examples"],
        "tool_docstrings": {"find_examples": "Search the example catalog."},
        "remaining_iterations": 5,
        "remaining_wall_clock_secs": 600.0,
    }
    base.update(overrides)
    return SamplingPrompt(**base)


class TestSamplingPromptEnvironmentContext:
    def test_section_omitted_when_none(self) -> None:
        """Default ``environment_context=None`` produces no env block."""
        text = _bare_prompt().assemble()
        assert "Environment context" not in text

    def test_section_emitted_when_context_provided(self) -> None:
        """A non-``None`` env context renders the block under its header."""
        env = {"regions": ["us-east-1"], "reservations": {"active_count": 0}}
        text = _bare_prompt(environment_context=env).assemble()
        assert "=== Environment context (slow-moving live signals) ===" in text
        # The actual data lands as JSON below the header.
        assert '"regions"' in text
        assert '"us-east-1"' in text

    def test_assemble_is_deterministic_with_env(self) -> None:
        """Same prompt + same env context → byte-identical output."""
        env = {"regions": ["a", "b"], "reservations": {"active_count": 1}}
        p1 = _bare_prompt(environment_context=env)
        p2 = _bare_prompt(environment_context=env)
        assert p1.assemble() == p2.assemble()

    def test_assemble_is_deterministic_when_env_keys_reordered(self) -> None:
        """Two callers passing the same data with different insertion orders
        produce byte-identical prompts. Pinned by the sort in
        ``_summarise_environment_context``."""
        env_a = {"zeta": 1, "alpha": 2, "mid": 3}
        env_b = {"alpha": 2, "mid": 3, "zeta": 1}
        text_a = _bare_prompt(environment_context=env_a).assemble()
        text_b = _bare_prompt(environment_context=env_b).assemble()
        assert text_a == text_b

    def test_oversize_env_does_not_blow_prompt_budget(self) -> None:
        """An oversize env input gets pruned before it reaches the prompt
        builder's outer byte cap, so the assembled prompt is still
        within :data:`PROMPT_BYTE_BUDGET`."""
        env = {
            "regions": ["us-east-1"],
            "huge": "x" * (ENVIRONMENT_CONTEXT_BYTE_CAP * 4),
        }
        text = _bare_prompt(environment_context=env).assemble()
        assert len(text.encode("utf-8")) <= PROMPT_BYTE_BUDGET
        # The truncation marker for dropped fields is present.
        assert "_dropped_fields" in text
