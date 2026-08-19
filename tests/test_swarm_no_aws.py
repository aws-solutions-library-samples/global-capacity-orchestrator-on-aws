"""A two-child swarm that completes without any AWS access at all.

The swarm layer inherits Mission's portability contract: an orchestrator
whose children run safe-tier tools must drive its whole fleet — plan
priming, concurrent child sessions, settlement, terminal report — to a
``complete`` verdict on a host with no AWS credentials. As in the
Mission no-AWS smoke test, ``boto3.Session`` is patched to raise on
construction, so the guarantee is enforced rather than incidental:
any code path that reached for an AWS client during the run would fail
this test loudly.

The flow mirrors ``gco swarm run`` semantics end to end: a validated
two-entry plan is dispatched through the runner's spawn seam (the same
admission path the in-process ``mission_spawn`` supervisor tool uses),
the fleet is driven by the concurrent child runner, and the
orchestrator completes through its fleet criteria over the
Children_Observation metrics.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from mission._engine_factory import EngineDependencies  # noqa: E402
from mission.state import FilesystemBackend  # noqa: E402
from mission.swarm import validate_swarm_config  # noqa: E402
from mission.swarm_runner import SwarmRunner  # noqa: E402
from mission.swarm_scaffold import validate_plan  # noqa: E402
from mission.types import SCHEMA_VERSION  # noqa: E402

_DIRECTIVE = "Search the documentation and example catalogs and surface at least one hit from each."

# Both tools are real, registered, safe-tagged MCP tools that read
# in-process catalogs — no AWS, no network. The two children get one
# each, which also exercises the mutating-tool overlap rail's happy
# path (safe tools may be shared or split freely).
_REGISTERED: dict[str, Any] = {"find_docs": object(), "find_examples": object()}
_TAGS: dict[str, set[str]] = {"find_docs": {"safe"}, "find_examples": {"safe"}}


def _plan() -> list[dict[str, Any]]:
    """A two-child plan, admission-validated exactly like a scaffold."""
    config = validate_swarm_config({"max_children": 2, "child_iteration_pool": 10})
    entries = [
        {
            "slot": "docs-worker",
            "directive": "Find documentation about inference endpoints.",
            "criteria": [
                {
                    "criterion_id": "docs_hit",
                    "kind": "tool_call_succeeded",
                    "required": True,
                    "tool_name": "find_docs",
                }
            ],
            "budget": {"max_iterations": 3, "max_wall_clock_seconds": 60},
            "tool_allowlist": ["find_docs"],
        },
        {
            "slot": "examples-worker",
            "directive": "Find an example manifest for inference.",
            "criteria": [
                {
                    "criterion_id": "examples_hit",
                    "kind": "tool_call_succeeded",
                    "required": True,
                    "tool_name": "find_examples",
                }
            ],
            "budget": {"max_iterations": 3, "max_wall_clock_seconds": 60},
            "tool_allowlist": ["find_examples"],
        },
    ]
    return validate_plan(
        entries, config=config, registered_tools=_REGISTERED, registered_tags=_TAGS
    )


def _orchestrator() -> dict[str, Any]:
    """A hand-built orchestrator session over the fleet metrics."""
    return {
        "version": SCHEMA_VERSION,
        "session_id": "mission-noaws-swarm",
        "directive_text": _DIRECTIVE,
        "criteria": [
            {
                "criterion_id": "fleet_completed",
                "kind": "metric_threshold",
                "required": True,
                "metric": "metrics.children_completed",
                "op": ">=",
                "target": 2,
            },
            {
                "criterion_id": "no_failures",
                "kind": "metric_threshold",
                "required": True,
                "metric": "metrics.children_failed",
                "op": "==",
                "target": 0,
            },
        ],
        "budget": {"max_iterations": 20, "max_wall_clock_seconds": 300},
        "tool_allowlist": ["children_status", "mission_spawn", "child_abort"],
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 100,
        "use_sampling": False,
        "allow_scripted_strategies": False,
        "status": "pending",
        "created_at": "2026-01-01T00:00:00Z",
        "iterations": [],
        "no_progress_counter": 0,
        "role": "orchestrator",
        "swarm": {
            "max_children": 2,
            "child_iteration_pool": 10,
            "max_concurrent_children": 2,
            "allow_overlapping_mutating_tools": False,
        },
        "children": [],
    }


@pytest.mark.mission_e2e
async def test_two_child_swarm_completes_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan, prime, drive, and complete a two-child fleet with AWS blocked."""

    def _no_aws_session(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("AWS access blocked in no-AWS swarm smoke test")

    monkeypatch.setattr("boto3.Session", _no_aws_session)
    monkeypatch.setenv("GCO_TASK_STATUS_DIR", str(tmp_path / "tasks"))

    backend = FilesystemBackend(root=tmp_path / "missions")
    backend.save_session(_orchestrator())  # type: ignore[arg-type]

    # The catalog-shaped dispatcher: a fixed hit list for either tool,
    # matching the shape the real safe-tier catalog tools return.
    async def dispatcher(tool_name: str, args: dict[str, Any], ctx: Any) -> list[dict[str, str]]:
        return [{"name": "hit_a"}, {"name": "hit_b"}]

    async def deps_builder(session: Any) -> EngineDependencies:
        return EngineDependencies(
            tool_dispatcher=dispatcher,
            sampling_callable=None,
            sandbox_runner=None,
        )

    runner = SwarmRunner(
        backend=backend,
        orchestrator_id="mission-noaws-swarm",
        deps_builder=deps_builder,
        registered_tools=_REGISTERED,
        registered_tags=_TAGS,
    )
    for request in _plan():
        result = await runner.spawn(request)
        assert result.get("spawned") is True, result

    final = await runner.run_to_completion()

    assert final["status"] == "completed"
    assert final["final_verdict"] == "complete"
    entries = final["children"]
    assert {entry["slot"] for entry in entries} == {"docs-worker", "examples-worker"}
    for entry in entries:
        child = backend.load_session(entry["session_id"])
        assert child is not None
        assert child["status"] == "completed"
        assert entry["settled"] is True
    # Pool arithmetic settled honestly: both reservations folded to the
    # single iteration each child actually ran.
    last_obs = final["iterations"][-1]["observation"]
    assert last_obs["metrics"]["children_completed"] == 2
    assert last_obs["metrics"]["iteration_pool_remaining"] == 10 - sum(
        entry["consumed_iterations"] for entry in entries
    )
