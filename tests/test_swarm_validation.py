"""Unit tests for the pure swarm-supervision primitives.

Covers the swarm-config validator, the spawn-admission pipeline (every
rejection reason and the normalized happy path), the registry/pool
transforms (settle, respawn, balance), and the deterministic restart
policy table in ``gco_mcp/mission/swarm.py``. Everything here is pure:
no engine, no runner, no I/O.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# Mirror the import pattern the other Mission tests use: ``gco_mcp/run_mcp.py``
# adds ``gco_mcp/`` to ``sys.path`` at runtime, but pytest has to do it itself
# before the import below resolves.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from mission.swarm import (  # noqa: E402
    DEFAULT_MAX_CONCURRENT_CHILDREN,
    SWARM_EXCLUDED_TOOLS,
    compute_pool_balance,
    new_registry_entry,
    respawn_entry,
    settle_entry,
    should_respawn,
    validate_spawn,
    validate_swarm_config,
)
from mission.validation import MissionValidationError  # noqa: E402

# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------


def make_config(**overrides: Any) -> dict[str, Any]:
    """A valid swarm config dict, overridable per test."""
    config: dict[str, Any] = {
        "max_children": 4,
        "child_iteration_pool": 40,
        "max_concurrent_children": 2,
        "allow_overlapping_mutating_tools": False,
    }
    config.update(overrides)
    return config


def make_request(**overrides: Any) -> dict[str, Any]:
    """A valid spawn request dict, overridable per test."""
    request: dict[str, Any] = {
        "slot": "worker-1",
        "directive": "Find documentation about inference endpoints.",
        "criteria": [
            {
                "criterion_id": "docs_found",
                "kind": "tool_call_succeeded",
                "required": True,
                "tool_name": "find_docs",
            }
        ],
        "budget": {"max_iterations": 5, "max_wall_clock_seconds": 300},
        "tool_allowlist": ["find_docs"],
    }
    request.update(overrides)
    return request


REGISTERED_TOOLS: dict[str, Any] = {
    "find_docs": object(),
    "find_examples": object(),
    "jobs_submit": object(),
    "jobs_delete": object(),
    "mission_start": object(),
}

REGISTERED_TAGS: dict[str, set[str]] = {
    "find_docs": {"safe", "docs"},
    "find_examples": {"safe", "docs"},
    "jobs_submit": {"low-risk", "jobs"},
    "jobs_delete": {"destructive", "jobs"},
    "mission_start": {"safe"},
}


def run_spawn(**overrides: Any) -> Any:
    """Run validate_spawn with sane defaults, overridable per test."""
    kwargs: dict[str, Any] = {
        "parent_role": "orchestrator",
        "config": validate_swarm_config(make_config()),
        "children": [],
        "request": make_request(),
        "registered_tools": REGISTERED_TOOLS,
        "registered_tags": REGISTERED_TAGS,
        "sibling_allowlists": {},
        "flag_lookup": None,
        "respawn_of_slot": None,
    }
    kwargs.update(overrides)
    return validate_spawn(**kwargs)


def rejection(excinfo: pytest.ExceptionInfo[MissionValidationError]) -> dict[str, Any]:
    """Return the structured details of a validation rejection."""
    details = excinfo.value.details
    assert details is not None
    return details


def live_entry(slot: str = "worker-1", reserved: int = 5) -> dict[str, Any]:
    """A live (non-settled) registry entry."""
    return {
        "slot": slot,
        "session_id": f"mission-{slot}",
        "spawned_at": "2026-08-19T00:00:00+00:00",
        "reserved_iterations": reserved,
        "restart_policy": "never",
        "max_respawns": 0,
        "respawn_count": 0,
        "consumed_iterations": 0,
    }


# ---------------------------------------------------------------------------
# Swarm config validation
# ---------------------------------------------------------------------------


class TestValidateSwarmConfig:
    def test_normalizes_defaults(self) -> None:
        """Omitted optional keys land as their documented defaults."""
        config = validate_swarm_config({"max_children": 2, "child_iteration_pool": 10})
        assert config == {
            "max_children": 2,
            "child_iteration_pool": 10,
            "max_concurrent_children": DEFAULT_MAX_CONCURRENT_CHILDREN,
            "allow_overlapping_mutating_tools": False,
        }

    def test_rejects_non_dict(self) -> None:
        """A non-dict payload is rejected before any key is read."""
        with pytest.raises(MissionValidationError) as excinfo:
            validate_swarm_config([])  # type: ignore[arg-type]
        assert rejection(excinfo)["reason"] == "not_a_dict"

    @pytest.mark.parametrize("bad", [None, 0, -1, -5, True, 2.5, "3"])
    def test_rejects_bad_max_children(self, bad: Any) -> None:
        """max_children must be a strictly positive int; no -1 sentinel."""
        with pytest.raises(MissionValidationError) as excinfo:
            validate_swarm_config(make_config(max_children=bad))
        details = rejection(excinfo)
        assert details["subfield"] == "max_children"
        assert details["reason"] == "missing_or_not_positive_int"

    @pytest.mark.parametrize("bad", [None, 0, -1, False, "10"])
    def test_rejects_bad_pool(self, bad: Any) -> None:
        """child_iteration_pool must be a strictly positive int."""
        with pytest.raises(MissionValidationError) as excinfo:
            validate_swarm_config(make_config(child_iteration_pool=bad))
        details = rejection(excinfo)
        assert details["subfield"] == "child_iteration_pool"
        assert details["reason"] == "missing_or_not_positive_int"

    @pytest.mark.parametrize("bad", [0, -2, True, "2"])
    def test_rejects_bad_concurrency(self, bad: Any) -> None:
        """max_concurrent_children, when supplied, must be a positive int."""
        with pytest.raises(MissionValidationError) as excinfo:
            validate_swarm_config(make_config(max_concurrent_children=bad))
        details = rejection(excinfo)
        assert details["subfield"] == "max_concurrent_children"
        assert details["reason"] == "not_positive_int"

    @pytest.mark.parametrize("bad", [1, "true", None])
    def test_rejects_non_bool_overlap_opt_out(self, bad: Any) -> None:
        """allow_overlapping_mutating_tools must be a real bool."""
        with pytest.raises(MissionValidationError) as excinfo:
            validate_swarm_config(make_config(allow_overlapping_mutating_tools=bad))
        details = rejection(excinfo)
        assert details["subfield"] == "allow_overlapping_mutating_tools"
        assert details["reason"] == "not_a_bool"


# ---------------------------------------------------------------------------
# Spawn admission
# ---------------------------------------------------------------------------


class TestSpawnAdmission:
    @pytest.mark.parametrize("role", [None, "child", "bystander"])
    def test_rejects_non_orchestrator_dispatch(self, role: str | None) -> None:
        """Only an orchestrator session may spawn; depth stops at one."""
        with pytest.raises(MissionValidationError) as excinfo:
            run_spawn(parent_role=role)
        assert rejection(excinfo)["reason"] == "spawn_depth_exceeded"

    @pytest.mark.parametrize(
        "bad_slot",
        [None, "", "has space", "-leading", "a" * 65, "tab\tname", 7],
    )
    def test_rejects_bad_slot(self, bad_slot: Any) -> None:
        """Slots become file names and audit keys, so the charset is tight."""
        with pytest.raises(MissionValidationError) as excinfo:
            run_spawn(request=make_request(slot=bad_slot))
        assert rejection(excinfo)["reason"] == "slot_missing_or_invalid"

    def test_rejects_duplicate_slot(self) -> None:
        """A slot name is unique across the registry, live or settled."""
        with pytest.raises(MissionValidationError) as excinfo:
            run_spawn(children=[live_entry("worker-1")])
        details = rejection(excinfo)
        assert details["reason"] == "duplicate_slot"
        assert details["slot"] == "worker-1"

    def test_respawn_exempts_its_own_slot(self) -> None:
        """A respawn reuses its slot name without tripping uniqueness."""
        settled = settle_entry(live_entry("worker-1", reserved=5), 5)
        spec = run_spawn(children=[settled], respawn_of_slot="worker-1")
        assert spec["slot"] == "worker-1"

    @pytest.mark.parametrize(
        ("budget", "subfield"),
        [
            (None, "max_iterations"),
            ({}, "max_iterations"),
            ({"max_iterations": -1, "max_wall_clock_seconds": 60}, "max_iterations"),
            ({"max_iterations": 0, "max_wall_clock_seconds": 60}, "max_iterations"),
            ({"max_iterations": 5}, "max_wall_clock_seconds"),
            ({"max_iterations": 5, "max_wall_clock_seconds": -1}, "max_wall_clock_seconds"),
            ({"max_iterations": True, "max_wall_clock_seconds": 60}, "max_iterations"),
        ],
    )
    def test_rejects_uncapped_or_malformed_child_budget(self, budget: Any, subfield: str) -> None:
        """Child budgets are mandatory-finite: the -1 sentinel is refused."""
        with pytest.raises(MissionValidationError) as excinfo:
            run_spawn(request=make_request(budget=budget))
        details = rejection(excinfo)
        assert details["field"] == "budget"
        if budget is not None and budget != {}:
            assert details["subfield"] == subfield

    def test_rejects_unknown_restart_policy(self) -> None:
        """restart_policy must be one of the three documented values."""
        with pytest.raises(MissionValidationError) as excinfo:
            run_spawn(request=make_request(restart_policy="always"))
        assert rejection(excinfo)["reason"] == "restart_policy_invalid"

    @pytest.mark.parametrize(
        ("policy", "supplied", "expected"),
        [
            ("never", None, 0),
            ("on_failure", None, 1),
            ("on_failure_with_revision", None, 1),
            ("on_failure", 3, 3),
            ("never", 3, 0),  # never means never, whatever the count says
        ],
    )
    def test_max_respawns_defaults_and_normalization(
        self, policy: str, supplied: int | None, expected: int
    ) -> None:
        """max_respawns defaults per policy; the never policy forces zero."""
        request = make_request(restart_policy=policy)
        if supplied is not None:
            request["max_respawns"] = supplied
        spec = run_spawn(request=request)
        assert spec["restart_policy"] == policy
        assert spec["max_respawns"] == expected

    @pytest.mark.parametrize("bad", [-1, True, 1.5, "2"])
    def test_rejects_bad_max_respawns(self, bad: Any) -> None:
        """max_respawns, when supplied, must be a non-negative int."""
        with pytest.raises(MissionValidationError) as excinfo:
            run_spawn(request=make_request(restart_policy="on_failure", max_respawns=bad))
        assert rejection(excinfo)["reason"] == "max_respawns_not_a_non_negative_int"

    def test_rejects_non_bool_use_sampling(self) -> None:
        """use_sampling is a strict bool; children default deterministic."""
        with pytest.raises(MissionValidationError) as excinfo:
            run_spawn(request=make_request(use_sampling="yes"))
        assert rejection(excinfo)["reason"] == "use_sampling_not_a_bool"

    def test_enforces_fleet_cap_over_live_slots(self) -> None:
        """Live slots fill the fleet; the cap rejects one more."""
        config = validate_swarm_config(make_config(max_children=2))
        children = [live_entry("a"), live_entry("b")]
        with pytest.raises(MissionValidationError) as excinfo:
            run_spawn(config=config, children=children, request=make_request(slot="c"))
        details = rejection(excinfo)
        assert details["reason"] == "fleet_cap_exceeded"
        assert details["max_children"] == 2
        assert details["live_children"] == 2

    def test_settled_slot_frees_fleet_capacity(self) -> None:
        """A settled (terminal) slot no longer occupies the fleet."""
        config = validate_swarm_config(make_config(max_children=2))
        children = [live_entry("a"), settle_entry(live_entry("b", reserved=5), 5)]
        spec = run_spawn(config=config, children=children, request=make_request(slot="c"))
        assert spec["slot"] == "c"

    def test_enforces_iteration_pool(self) -> None:
        """A spawn drawing more than the remaining pool is refused."""
        config = validate_swarm_config(make_config(child_iteration_pool=8))
        children = [live_entry("a", reserved=5)]
        request = make_request(slot="b", budget={"max_iterations": 4, "max_wall_clock_seconds": 60})
        with pytest.raises(MissionValidationError) as excinfo:
            run_spawn(config=config, children=children, request=request)
        details = rejection(excinfo)
        assert details["reason"] == "iteration_pool_exhausted"
        assert details["requested"] == 4
        assert details["remaining"] == 3

    def test_settled_refund_replenishes_pool(self) -> None:
        """Settling a child refunds its unused reservation to the pool."""
        config = validate_swarm_config(make_config(child_iteration_pool=8))
        children = [settle_entry(live_entry("a", reserved=5), 2)]  # 3 refunded
        request = make_request(slot="b", budget={"max_iterations": 6, "max_wall_clock_seconds": 60})
        spec = run_spawn(config=config, children=children, request=request)
        assert spec["budget"]["max_iterations"] == 6

    def test_delegates_directive_validation(self) -> None:
        """Directive rules are the shared Mission validator's, unchanged."""
        with pytest.raises(MissionValidationError) as excinfo:
            run_spawn(request=make_request(directive="   "))
        details = rejection(excinfo)
        assert details["field"] == "directive"
        assert details["reason"] == "empty"

    def test_delegates_criteria_validation(self) -> None:
        """Criteria rules are the shared Mission validator's, unchanged."""
        with pytest.raises(MissionValidationError) as excinfo:
            run_spawn(request=make_request(criteria=[]))
        details = rejection(excinfo)
        assert details["field"] == "criteria"
        assert details["reason"] == "empty"

    @pytest.mark.parametrize("name", ["mission_start", "mission_spawn", "swarm_status"])
    def test_rejects_control_plane_tools_in_explicit_allowlist(self, name: str) -> None:
        """No child allowlist may name a loop-management tool."""
        with pytest.raises(MissionValidationError) as excinfo:
            run_spawn(request=make_request(tool_allowlist=["find_docs", name]))
        details = rejection(excinfo)
        assert details["reason"] == "control_tool_not_allowed"
        assert details["tool_name"] == name

    def test_allow_all_expansion_excludes_control_plane_tools(self) -> None:
        """The all-tools expansion never resolves loop-management names."""
        request = make_request(tool_allowlist=None, allow_all_tools=True)
        spec = run_spawn(request=request)
        assert "mission_start" not in spec["tool_allowlist"]
        assert set(spec["tool_allowlist"]) & SWARM_EXCLUDED_TOOLS == set()
        assert "find_docs" in spec["tool_allowlist"]

    def test_rejects_mutating_overlap_with_live_sibling(self) -> None:
        """Two live children sharing a non-safe tool is refused by default."""
        request = make_request(slot="b", tool_allowlist=["find_docs", "jobs_submit"])
        with pytest.raises(MissionValidationError) as excinfo:
            run_spawn(request=request, sibling_allowlists={"a": ["jobs_submit"]})
        details = rejection(excinfo)
        assert details["reason"] == "mutating_tool_overlap"
        assert details["tools"] == ["jobs_submit"]
        assert details["sibling_slot"] == "a"

    def test_safe_tool_overlap_is_fine(self) -> None:
        """Read-only (safe-tagged) tools may be shared freely."""
        request = make_request(slot="b", tool_allowlist=["find_docs"])
        spec = run_spawn(request=request, sibling_allowlists={"a": ["find_docs"]})
        assert spec["tool_allowlist"] == ["find_docs"]

    def test_overlap_opt_out_allows_sharing(self) -> None:
        """The config opt-out disables the overlap rail explicitly."""
        config = validate_swarm_config(make_config(allow_overlapping_mutating_tools=True))
        request = make_request(slot="b", tool_allowlist=["jobs_submit"])
        spec = run_spawn(config=config, request=request, sibling_allowlists={"a": ["jobs_submit"]})
        assert spec["tool_allowlist"] == ["jobs_submit"]

    def test_untagged_tool_counts_as_mutating(self) -> None:
        """A tool with no tag information fails closed as non-safe."""
        tools = dict(REGISTERED_TOOLS)
        tools["mystery_tool"] = object()
        request = make_request(slot="b", tool_allowlist=["mystery_tool"])
        with pytest.raises(MissionValidationError) as excinfo:
            run_spawn(
                registered_tools=tools,
                request=request,
                sibling_allowlists={"a": ["mystery_tool"]},
            )
        assert rejection(excinfo)["reason"] == "mutating_tool_overlap"

    def test_happy_path_returns_normalized_spec(self) -> None:
        """A valid request lands as the fully normalized spawn spec."""
        spec = run_spawn()
        assert spec["slot"] == "worker-1"
        assert spec["directive"].startswith("Find documentation")
        assert spec["budget"] == {"max_iterations": 5, "max_wall_clock_seconds": 300}
        assert spec["tool_allowlist"] == ["find_docs"]
        assert spec["checkpoint_cadence"] == {"kind": "every_iteration"}
        assert spec["restart_policy"] == "never"
        assert spec["max_respawns"] == 0
        assert spec["use_sampling"] is False
        assert spec["criteria"][0]["criterion_id"] == "docs_found"


# ---------------------------------------------------------------------------
# Registry and pool transforms
# ---------------------------------------------------------------------------


class TestRegistryTransforms:
    def test_new_registry_entry_shape(self) -> None:
        """A fresh entry reserves the child's iteration budget."""
        spec = run_spawn()
        entry = new_registry_entry(spec, "mission-abc", "2026-08-19T00:00:00+00:00")
        assert entry == {
            "slot": "worker-1",
            "session_id": "mission-abc",
            "spawned_at": "2026-08-19T00:00:00+00:00",
            "reserved_iterations": 5,
            "restart_policy": "never",
            "max_respawns": 0,
            "respawn_count": 0,
            "consumed_iterations": 0,
        }

    def test_settle_folds_consumption_and_refunds(self) -> None:
        """Settling records actual consumption and zeroes the reservation."""
        settled = settle_entry(live_entry(reserved=5), 3)
        assert settled["consumed_iterations"] == 3
        assert settled["reserved_iterations"] == 0
        assert settled["settled"] is True

    @pytest.mark.parametrize(("recorded", "expected"), [(9, 5), (-2, 0), (0, 0), (5, 5)])
    def test_settle_clamps_consumption_to_reservation(self, recorded: int, expected: int) -> None:
        """Consumption is clamped into [0, reserved] against corrupt counts."""
        settled = settle_entry(live_entry(reserved=5), recorded)
        assert settled["consumed_iterations"] == expected

    def test_settle_is_idempotent(self) -> None:
        """Settling twice never double-counts consumption."""
        once = settle_entry(live_entry(reserved=5), 4)
        twice = settle_entry(once, 4)
        assert twice == once

    def test_settle_does_not_mutate_input(self) -> None:
        """The transform returns a new entry, leaving the input alone."""
        entry = live_entry(reserved=5)
        settle_entry(entry, 3)
        assert entry["consumed_iterations"] == 0
        assert "settled" not in entry

    def test_respawn_requires_settled_entry(self) -> None:
        """Respawning a live entry is a supervision bug, rejected loudly."""
        with pytest.raises(MissionValidationError) as excinfo:
            respawn_entry(
                live_entry(),
                new_session_id="mission-next",
                reserved_iterations=3,
                spawned_at="2026-08-19T01:00:00+00:00",
            )
        assert rejection(excinfo)["reason"] == "respawn_before_settle"

    def test_respawn_updates_lineage_and_reservation(self) -> None:
        """A respawn keeps consumption, tracks lineage, and goes live again."""
        settled = settle_entry(live_entry(reserved=5), 2)
        respawned = respawn_entry(
            settled,
            new_session_id="mission-next",
            reserved_iterations=3,
            spawned_at="2026-08-19T01:00:00+00:00",
        )
        assert respawned["session_id"] == "mission-next"
        assert respawned["prior_session_ids"] == ["mission-worker-1"]
        assert respawned["respawn_count"] == 1
        assert respawned["reserved_iterations"] == 3
        assert respawned["consumed_iterations"] == 2
        assert "settled" not in respawned

    def test_pool_balance_counts_live_and_settled(self) -> None:
        """Live entries hold reservations; settled ones only consumption."""
        children = [
            live_entry("a", reserved=5),
            settle_entry(live_entry("b", reserved=7), 4),
        ]
        balance = compute_pool_balance(20, children)
        assert balance == {"pool": 20, "reserved": 5, "consumed": 4, "remaining": 11}


# ---------------------------------------------------------------------------
# Restart policy table
# ---------------------------------------------------------------------------


class TestShouldRespawn:
    @pytest.mark.parametrize(
        ("policy", "count", "maximum", "status", "decision", "reason"),
        [
            ("on_failure", 0, 1, "completed", False, "completed_no_respawn"),
            ("never", 0, 0, "failed", False, "policy_never"),
            ("on_failure", 1, 1, "failed", False, "max_respawns_reached"),
            ("on_failure", 0, 1, "failed", True, "respawn"),
            ("on_failure", 0, 1, "terminated", True, "respawn"),
            ("on_failure_with_revision", 0, 1, "failed", True, "respawn"),
            ("on_failure", 0, 1, "running", False, "not_terminal"),
            ("on_failure", 0, 1, "paused", False, "not_terminal"),
        ],
    )
    def test_policy_table(
        self,
        policy: str,
        count: int,
        maximum: int,
        status: str,
        decision: bool,
        reason: str,
    ) -> None:
        """The whole deterministic table, one row per documented rule."""
        entry = live_entry()
        entry["restart_policy"] = policy
        entry["respawn_count"] = count
        entry["max_respawns"] = maximum
        assert should_respawn(entry, status) == (decision, reason)
