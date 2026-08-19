"""Property-based tests for the swarm iteration-pool accounting.

The pool contract: every spawn reserves the child's iteration budget,
settling a terminal child folds actual consumption in and refunds the
rest, and a respawn re-reserves from what remains. These tests drive
the pure transforms in ``gco_mcp/mission/swarm.py`` through arbitrary
interleavings of those operations (admission-gated exactly the way
``validate_spawn`` gates them) and assert the balance invariants hold
after every single step:

* the remaining balance is never negative,
* reserved plus consumed never exceed the pool,
* consumption is monotonically non-decreasing,
* settled entries hold no reservation.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from mission.swarm import (  # noqa: E402
    compute_pool_balance,
    respawn_entry,
    settle_entry,
)

# One simulated operation: ("spawn", reservation), ("settle", pick, recorded),
# or ("respawn", pick, reservation). ``pick`` selects among eligible entries
# by modulo, so every generated op is applicable to some registry state.
_OPS = st.one_of(
    st.tuples(st.just("spawn"), st.integers(min_value=1, max_value=25)),
    st.tuples(
        st.just("settle"),
        st.integers(min_value=0, max_value=1_000),
        st.integers(min_value=-5, max_value=30),
    ),
    st.tuples(
        st.just("respawn"),
        st.integers(min_value=0, max_value=1_000),
        st.integers(min_value=1, max_value=25),
    ),
)


def _fresh_entry(slot: str, reserved: int) -> dict[str, Any]:
    """A minimal live registry entry, as new_registry_entry would build it."""
    return {
        "slot": slot,
        "session_id": f"mission-{slot}",
        "spawned_at": "2026-08-19T00:00:00+00:00",
        "reserved_iterations": reserved,
        "restart_policy": "on_failure",
        "max_respawns": 5,
        "respawn_count": 0,
        "consumed_iterations": 0,
    }


def _assert_invariants(pool: int, children: list[dict[str, Any]]) -> None:
    """The balance invariants that must hold after every operation."""
    balance = compute_pool_balance(pool, children)
    assert balance["remaining"] >= 0
    assert balance["reserved"] + balance["consumed"] <= pool
    assert balance["reserved"] >= 0
    assert balance["consumed"] >= 0
    for entry in children:
        if entry.get("settled"):
            assert entry["reserved_iterations"] == 0


@settings(max_examples=120, deadline=4000)
@given(
    pool=st.integers(min_value=1, max_value=60),
    ops=st.lists(_OPS, max_size=40),
)
def test_pool_invariants_hold_under_arbitrary_interleavings(
    pool: int, ops: list[tuple[Any, ...]]
) -> None:
    """No admission-gated op sequence can drive the pool negative."""
    children: list[dict[str, Any]] = []
    consumed_history = 0
    spawn_counter = 0

    for op in ops:
        if op[0] == "spawn":
            _, reservation = op
            balance = compute_pool_balance(pool, children)
            # Admission gate: exactly the pool check validate_spawn runs.
            if reservation <= balance["remaining"]:
                spawn_counter += 1
                children.append(_fresh_entry(f"s{spawn_counter}", reservation))
        elif op[0] == "settle":
            _, pick, recorded = op
            live = [i for i, e in enumerate(children) if not e.get("settled")]
            if live:
                index = live[pick % len(live)]
                children[index] = dict(settle_entry(children[index], recorded))  # type: ignore[arg-type]
        else:  # respawn
            _, pick, reservation = op
            settled = [i for i, e in enumerate(children) if e.get("settled")]
            if settled:
                index = settled[pick % len(settled)]
                balance = compute_pool_balance(pool, children)
                if reservation <= balance["remaining"]:
                    children[index] = dict(
                        respawn_entry(
                            children[index],  # type: ignore[arg-type]
                            new_session_id=f"mission-r{spawn_counter}",
                            reserved_iterations=reservation,
                            spawned_at="2026-08-19T01:00:00+00:00",
                        )
                    )

        _assert_invariants(pool, children)
        new_consumed = compute_pool_balance(pool, children)["consumed"]
        assert new_consumed >= consumed_history
        consumed_history = new_consumed


@settings(max_examples=120, deadline=2000)
@given(
    reserved=st.integers(min_value=1, max_value=50),
    recorded=st.integers(min_value=-10, max_value=80),
)
def test_settle_refund_is_exact(reserved: int, recorded: int) -> None:
    """Settling frees exactly the unconsumed part of the reservation."""
    pool = 100
    entry = _fresh_entry("a", reserved)
    before = compute_pool_balance(pool, [entry])
    settled = settle_entry(entry, recorded)  # type: ignore[arg-type]
    after = compute_pool_balance(pool, [dict(settled)])
    folded = min(max(recorded, 0), reserved)
    assert after["consumed"] == folded
    assert after["remaining"] - before["remaining"] == reserved - folded
