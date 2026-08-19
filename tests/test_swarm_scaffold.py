"""Tests for Swarm_Plan generation: deterministic fallback and sampled path.

The sampled path uses a stub backend (canned ``sample`` responses), the
same duck-typing seam the shipped backends satisfy — no transport, no
AWS, no MCP host.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

import json  # noqa: E402

from mission.swarm import validate_spawn, validate_swarm_config  # noqa: E402
from mission.swarm_scaffold import (  # noqa: E402
    SwarmScaffoldError,
    build_plan_prompt,
    generate_deterministic_plan,
    generate_sampled_plan,
    sample_revised_directive,
    validate_plan,
)
from mission.validation import MissionValidationError  # noqa: E402

REGISTERED_TOOLS: dict[str, Any] = {
    "find_docs": object(),
    "find_examples": object(),
    "jobs_submit": object(),
}
REGISTERED_TAGS: dict[str, set[str]] = {
    "find_docs": {"safe"},
    "find_examples": {"safe"},
    "jobs_submit": {"low-risk"},
}
CONFIG = validate_swarm_config({"max_children": 3, "child_iteration_pool": 12})


def plan_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "config": CONFIG,
        "registered_tools": REGISTERED_TOOLS,
        "registered_tags": REGISTERED_TAGS,
    }
    kwargs.update(overrides)
    return kwargs


class StubBackend:
    """Canned sampling backend; records every prompt it was handed."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    async def sample(self, prompt: Any) -> str:
        self.prompts.append(prompt.assemble())
        if not self._responses:
            raise AssertionError("stub backend exhausted")
        return self._responses.pop(0)


def child_entry(slot: str, **overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "slot": slot,
        "directive": f"Handle the {slot} shard.",
        "criteria": [
            {
                "criterion_id": f"{slot}_done",
                "kind": "tool_call_succeeded",
                "required": True,
                "tool_name": "find_docs",
            }
        ],
        "budget": {"max_iterations": 3, "max_wall_clock_seconds": 60},
        "tool_allowlist": ["find_docs"],
    }
    entry.update(overrides)
    return entry


# ---------------------------------------------------------------------------
# Deterministic fallback
# ---------------------------------------------------------------------------


class TestDeterministicPlan:
    def test_single_child_mirrors_directive(self) -> None:
        """The fallback is one validated worker mirroring the directive."""
        plan = generate_deterministic_plan(
            "Find documentation about inference endpoints.",
            tool_allowlist=["find_docs"],
            **plan_kwargs(),
        )
        assert len(plan) == 1
        entry = plan[0]
        assert entry["slot"] == "worker-1"
        assert entry["directive"] == "Find documentation about inference endpoints."
        assert entry["restart_policy"] == "never"
        assert entry["use_sampling"] is False
        assert entry["budget"]["max_iterations"] == 5

    def test_budget_bounded_by_pool(self) -> None:
        """A small pool clamps the default child iteration budget."""
        tight = validate_swarm_config({"max_children": 1, "child_iteration_pool": 2})
        plan = generate_deterministic_plan(
            "Search the catalog.",
            tool_allowlist=["find_docs"],
            **plan_kwargs(config=tight),
        )
        assert plan[0]["budget"]["max_iterations"] == 2

    def test_plan_entries_readmit_through_spawn_validation(self) -> None:
        """A returned plan entry passes validate_spawn verbatim."""
        plan = generate_deterministic_plan(
            "Find documentation about inference endpoints.",
            tool_allowlist=["find_docs"],
            **plan_kwargs(),
        )
        spec = validate_spawn(
            parent_role="orchestrator",
            config=CONFIG,
            children=[],
            request=plan[0],
            registered_tools=REGISTERED_TOOLS,
            registered_tags=REGISTERED_TAGS,
            sibling_allowlists={},
        )
        assert spec["slot"] == "worker-1"

    def test_plan_is_json_safe(self) -> None:
        """Plans serialize cleanly for --save-plan and review."""
        plan = generate_deterministic_plan(
            "Find documentation.", tool_allowlist=["find_docs"], **plan_kwargs()
        )
        assert json.loads(json.dumps(plan)) == plan


# ---------------------------------------------------------------------------
# Whole-plan validation
# ---------------------------------------------------------------------------


class TestValidatePlan:
    def test_pool_enforced_across_entries(self) -> None:
        """Entry budgets draw from one pool across the whole plan."""
        entries = [
            child_entry("a", budget={"max_iterations": 8, "max_wall_clock_seconds": 60}),
            child_entry("b", budget={"max_iterations": 8, "max_wall_clock_seconds": 60}),
        ]
        with pytest.raises(MissionValidationError) as excinfo:
            validate_plan(entries, **plan_kwargs())
        details = excinfo.value.details
        assert details is not None
        assert details["reason"] == "iteration_pool_exhausted"
        assert details["plan_index"] == 1

    def test_overlap_enforced_across_entries(self) -> None:
        """Two plan entries cannot share a mutating tool."""
        entries = [
            child_entry("a", tool_allowlist=["jobs_submit"]),
            child_entry("b", tool_allowlist=["jobs_submit"]),
        ]
        with pytest.raises(MissionValidationError) as excinfo:
            validate_plan(entries, **plan_kwargs())
        details = excinfo.value.details
        assert details is not None
        assert details["reason"] == "mutating_tool_overlap"
        assert details["plan_index"] == 1

    def test_empty_plan_rejected(self) -> None:
        """An empty array is not a plan."""
        with pytest.raises(MissionValidationError) as excinfo:
            validate_plan([], **plan_kwargs())
        details = excinfo.value.details
        assert details is not None
        assert details["reason"] == "empty_or_not_a_list"


# ---------------------------------------------------------------------------
# Sampled path
# ---------------------------------------------------------------------------


class TestSampledPlan:
    async def test_happy_path_two_children(self) -> None:
        """A valid model response lands as a validated two-child plan."""
        response = json.dumps([child_entry("shard-a"), child_entry("shard-b")])
        backend = StubBackend([response])
        plan = await generate_sampled_plan(backend, "Cover both shards.", **plan_kwargs())
        assert [entry["slot"] for entry in plan] == ["shard-a", "shard-b"]
        assert len(backend.prompts) == 1
        assert "=== Operator directive ===" in backend.prompts[0]

    async def test_rejection_feeds_back_then_succeeds(self) -> None:
        """A rejected first attempt retries with the validator's reason."""
        bad = json.dumps(
            [
                child_entry("a", tool_allowlist=["jobs_submit"]),
                child_entry("b", tool_allowlist=["jobs_submit"]),
            ]
        )
        good = json.dumps([child_entry("a"), child_entry("b")])
        backend = StubBackend([bad, good])
        plan = await generate_sampled_plan(backend, "Cover both.", **plan_kwargs())
        assert len(plan) == 2
        assert len(backend.prompts) == 2
        assert "mutating_tool_overlap" in backend.prompts[1]

    async def test_junk_json_feeds_back(self) -> None:
        """Unparseable output retries with parse feedback."""
        backend = StubBackend(["definitely not json", json.dumps([child_entry("a")])])
        plan = await generate_sampled_plan(backend, "One worker.", **plan_kwargs())
        assert len(plan) == 1
        assert "could not be parsed" in backend.prompts[1]

    async def test_exhaustion_raises_with_last_reason(self) -> None:
        """Persistent rejection surfaces the final reason token."""
        bad = json.dumps([child_entry("a", budget={"max_iterations": -1})])
        backend = StubBackend([bad, bad, bad])
        with pytest.raises(SwarmScaffoldError) as excinfo:
            await generate_sampled_plan(backend, "Nope.", retries=2, **plan_kwargs())
        assert excinfo.value.last_reason == "missing_or_not_positive_int"

    async def test_transport_error_not_retried(self) -> None:
        """Backend exceptions surface immediately as transport_error."""

        class ExplodingBackend:
            async def sample(self, prompt: Any) -> str:
                raise ConnectionError("wire down")

        with pytest.raises(SwarmScaffoldError) as excinfo:
            await generate_sampled_plan(ExplodingBackend(), "Goal.", **plan_kwargs())
        assert excinfo.value.last_reason == "transport_error"

    def test_prompt_is_deterministic(self) -> None:
        """Same inputs produce a byte-identical prompt."""
        first = build_plan_prompt("Goal.", config=CONFIG, registered_tools=REGISTERED_TOOLS)
        second = build_plan_prompt("Goal.", config=CONFIG, registered_tools=REGISTERED_TOOLS)
        assert first == second
        assert first.index("find_docs") < first.index("find_examples")


# ---------------------------------------------------------------------------
# Respawn directive revision
# ---------------------------------------------------------------------------


class TestSampleRevisedDirective:
    async def test_returns_validated_first_line(self) -> None:
        """The first response line becomes the revised directive."""
        backend = StubBackend(["Retry with a narrower query.\nextra noise"])
        failed = {"directive_text": "Search everything."}
        revised = await sample_revised_directive(backend, failed)  # type: ignore[arg-type]
        assert revised == "Retry with a narrower query."
        assert "=== Original directive ===" in backend.prompts[0]

    async def test_lessons_rendered_into_prompt(self) -> None:
        """Final-report lessons ride along as advisory context."""
        backend = StubBackend(["Narrower retry."])
        failed = {"directive_text": "Search everything."}
        await sample_revised_directive(
            backend,
            failed,  # type: ignore[arg-type]
            lessons=["The query was too broad."],
        )
        assert "The query was too broad." in backend.prompts[0]

    async def test_failure_degrades_to_none(self) -> None:
        """Backend failures and junk responses both fall back to None."""

        class ExplodingBackend:
            async def sample(self, prompt: Any) -> str:
                raise RuntimeError("no backend")

        failed = {"directive_text": "Search everything."}
        assert await sample_revised_directive(ExplodingBackend(), failed) is None  # type: ignore[arg-type]
        blank = StubBackend(["   "])
        assert await sample_revised_directive(blank, failed) is None  # type: ignore[arg-type]
