"""Bootstrap-port assignment for KV-transfer workers is collision-free.

Each disaggregated worker is identified by a ``(dp_rank, tp_rank)`` pair and
needs its own TCP port to bootstrap the KV transfer. The monitor derives that
port from a per-endpoint ``base_port`` and the tensor-parallel world size
``tp_size`` via :func:`gco.services.inference_monitor.bootstrap_port_for_worker`.

For a fixed ``(base_port, tp_size)`` the assignment must never hand two
different workers the same port — otherwise two workers would race for one
socket. This module checks that distinct workers always receive distinct ports
and that every assigned port lands inside the usable TCP range ``1024..65535``.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from gco.services.inference_monitor import (
    MAX_BOOTSTRAP_PORT,
    MIN_BOOTSTRAP_PORT,
    bootstrap_port_for_worker,
)


@st.composite
def _fixed_endpoint_with_workers(draw: st.DrawFn) -> dict[str, Any]:
    """Draw a ``(base_port, tp_size)`` plus distinct in-range worker pairs.

    A worker is a ``(dp_rank, tp_rank)`` pair with ``0 <= tp_rank < tp_size``,
    the domain over which the port map is meant to be one-to-one. ``dp_rank`` is
    bounded so the computed port stays at or below ``MAX_BOOTSTRAP_PORT``; pairs
    are drawn as a set so they are pairwise distinct by construction.
    """
    base_port = draw(st.integers(min_value=MIN_BOOTSTRAP_PORT, max_value=MAX_BOOTSTRAP_PORT))
    tp_size = draw(st.integers(min_value=1, max_value=1024))

    # Largest dp_rank whose smallest-rank worker still fits under the ceiling.
    headroom = MAX_BOOTSTRAP_PORT - base_port
    max_dp_rank = headroom // tp_size

    pairs = draw(
        st.sets(
            st.tuples(
                st.integers(min_value=0, max_value=max_dp_rank),
                st.integers(min_value=0, max_value=tp_size - 1),
            ),
            min_size=1,
            max_size=64,
        )
    )
    # Keep only the pairs that actually land in range (the dp_rank == max_dp_rank
    # row can overshoot the ceiling for high tp_rank values).
    in_range = [
        (dp_rank, tp_rank)
        for (dp_rank, tp_rank) in pairs
        if base_port + dp_rank * tp_size + tp_rank <= MAX_BOOTSTRAP_PORT
    ]
    return {"base_port": base_port, "tp_size": tp_size, "workers": in_range}


@given(scenario=_fixed_endpoint_with_workers())
def test_distinct_workers_get_distinct_in_range_ports(scenario: dict[str, Any]) -> None:
    """Distinct workers under one endpoint never share a bootstrap port.

    Holding ``base_port`` and ``tp_size`` fixed, every distinct
    ``(dp_rank, tp_rank)`` worker maps to its own port, and each port sits
    within the usable TCP range.
    """
    base_port = scenario["base_port"]
    tp_size = scenario["tp_size"]
    workers = scenario["workers"]

    ports = [
        bootstrap_port_for_worker(base_port, dp_rank, tp_size, tp_rank)
        for (dp_rank, tp_rank) in workers
    ]

    # Every assigned port is usable.
    for port in ports:
        assert MIN_BOOTSTRAP_PORT <= port <= MAX_BOOTSTRAP_PORT

    # Distinct workers never collide on a port.
    assert len(set(ports)) == len(ports)
