"""Property-based tests for ConfigLoader validation and merge behavior.

The synthesis matrix (``tests/_cdk_config_matrix.py``) proves *named,
curated* configurations synthesize — each entry encodes a reason. Full-app
synth is seconds per example, so random exploration there would be slow,
nondeterministic, and reproducible only via a printed seed. The
config-loader layer is the opposite: milliseconds per example with crisp
properties, which is where Hypothesis earns its keep. These properties
sweep the knob spaces this branch reworked:

1. Any in-range configuration is accepted, and the merged result preserves
   every override while keeping every untouched default (deep-merge never
   drops sibling keys).
2. Any out-of-range or wrongly-typed value raises ``ConfigValidationError``
   specifically — never a ``KeyError``/``TypeError`` leaking from merge
   internals, which is what an operator's typo would otherwise surface as.

The base context is the shipped ``cdk.json``'s own context, so the starting
point is valid by construction and tracks the repo without maintenance.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from gco.config.config_loader import ConfigLoader, ConfigValidationError
from tests.test_config_loader import MockApp

_BASE_CONTEXT: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parent.parent / "cdk.json").read_text(encoding="utf-8")
)["context"]


def _context_with(key: str, value: dict[str, Any]) -> dict[str, Any]:
    context = json.loads(json.dumps(_BASE_CONTEXT))
    context[key] = value
    return context


def _mixed_case(base: str) -> st.SearchStrategy[str]:
    return st.builds(
        lambda flags: "".join(
            ch.upper() if flag else ch.lower() for ch, flag in zip(base, flags, strict=False)
        ),
        st.lists(st.booleans(), min_size=len(base), max_size=len(base)),
    )


# ---------------------------------------------------------------------------
# traffic_dial
# ---------------------------------------------------------------------------

_DIAL_RANGES = {
    "interval_minutes": (1, 1_440),
    "lookback_minutes": (1, 1_440),
    "min_dial_percentage": (0, 100),
    "max_step_percentage": (1, 100),
    "full_health_percentage": (1, 100),
}

_dial_in_range = st.fixed_dictionaries(
    {
        "enabled": st.booleans(),
        "mode": st.one_of(_mixed_case("monitor"), _mixed_case("enforce")),
        **{
            field: st.integers(min_value=lo, max_value=hi)
            for field, (lo, hi) in _DIAL_RANGES.items()
        },
    }
)


class TestTrafficDialProperties:
    @settings(max_examples=40, deadline=None)
    @given(dial=_dial_in_range)
    def test_in_range_dial_configs_merge_losslessly(self, dial: dict[str, Any]) -> None:
        context = _context_with("global_accelerator", {"traffic_dial": dial})
        merged = ConfigLoader(MockApp(context)).get_global_accelerator_config()
        merged_dial = merged["traffic_dial"]
        for key, value in dial.items():
            assert merged_dial[key] == value
        # Sibling GA defaults survive the partial block.
        assert merged["health_check_interval"] in (10, 30)
        assert merged["health_check_path"].startswith("/")

    @settings(max_examples=40, deadline=None)
    @given(
        field=st.sampled_from(sorted(_DIAL_RANGES)),
        value=st.one_of(
            st.integers(min_value=-10_000, max_value=10_000),
            st.booleans(),
            st.text(max_size=6),
            st.none(),
            st.floats(allow_nan=False, allow_infinity=False),
        ),
    )
    def test_out_of_range_dial_values_fail_as_validation_errors(
        self, field: str, value: Any
    ) -> None:
        lo, hi = _DIAL_RANGES[field]
        in_range = isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi
        context = _context_with("global_accelerator", {"traffic_dial": {field: value}})
        if in_range:
            merged = ConfigLoader(MockApp(context)).get_global_accelerator_config()
            assert merged["traffic_dial"][field] == value
        else:
            with pytest.raises(ConfigValidationError):
                ConfigLoader(MockApp(context))

    @settings(max_examples=25, deadline=None)
    @given(mode=st.text(max_size=10))
    def test_arbitrary_mode_strings_never_leak_non_validation_errors(self, mode: str) -> None:
        context = _context_with("global_accelerator", {"traffic_dial": {"mode": mode}})
        if mode.lower() in ("monitor", "enforce"):
            merged = ConfigLoader(MockApp(context)).get_global_accelerator_config()
            assert merged["traffic_dial"]["mode"] == mode
        else:
            with pytest.raises(ConfigValidationError):
                ConfigLoader(MockApp(context))


# ---------------------------------------------------------------------------
# global_accelerator health-check contract
# ---------------------------------------------------------------------------


class TestGlobalAcceleratorProperties:
    @settings(max_examples=40, deadline=None)
    @given(
        interval=st.sampled_from([10, 30]),
        threshold=st.integers(min_value=1, max_value=10),
        path=st.text(
            alphabet="abcdefghijklmnopqrstuvwxyz0123456789/-", min_size=0, max_size=20
        ).map(lambda s: "/" + s),
        affinity=st.one_of(_mixed_case("none"), _mixed_case("source_ip")),
    )
    def test_valid_health_check_contracts_are_accepted(
        self, interval: int, threshold: int, path: str, affinity: str
    ) -> None:
        context = _context_with(
            "global_accelerator",
            {
                "health_check_interval": interval,
                "health_check_threshold": threshold,
                "health_check_path": path,
                "client_affinity": affinity,
            },
        )
        merged = ConfigLoader(MockApp(context)).get_global_accelerator_config()
        assert merged["health_check_interval"] == interval
        assert merged["health_check_threshold"] == threshold
        assert merged["health_check_path"] == path
        # The dial sub-block's defaults survive an override that never
        # mentions it.
        assert merged["traffic_dial"]["enabled"] is False

    @settings(max_examples=40, deadline=None)
    @given(interval=st.integers(min_value=-100, max_value=200))
    def test_only_the_two_api_intervals_validate(self, interval: int) -> None:
        context = _context_with("global_accelerator", {"health_check_interval": interval})
        if interval in (10, 30):
            ConfigLoader(MockApp(context))
        else:
            with pytest.raises(ConfigValidationError):
                ConfigLoader(MockApp(context))


# ---------------------------------------------------------------------------
# historical (capacity-history surface)
# ---------------------------------------------------------------------------


class TestHistoricalProperties:
    @settings(max_examples=40, deadline=None)
    @given(
        retention=st.integers(min_value=1, max_value=3_650),
        poll=st.integers(min_value=1, max_value=1_440),
        short_hours=st.integers(min_value=1, max_value=4_368),
        long_hours=st.one_of(st.just(0), st.integers(min_value=1, max_value=4_368)),
        capacities=st.lists(st.sampled_from([1, 10, 50]), min_size=1, max_size=3, unique=True),
    )
    def test_valid_historical_configs_merge_losslessly(
        self,
        retention: int,
        poll: int,
        short_hours: int,
        long_hours: int,
        capacities: list[int],
    ) -> None:
        context = _context_with(
            "historical",
            {
                "enabled": True,
                "retention_days": retention,
                "poll_interval_minutes": poll,
                "capacity_block_duration_hours": short_hours,
                "capacity_block_long_duration_hours": long_hours,
                "spot_score_target_capacities": capacities,
            },
        )
        merged = ConfigLoader(MockApp(context)).get_capacity_history_config()
        assert merged["retention_days"] == retention
        assert merged["capacity_block_long_duration_hours"] == long_hours
        assert merged["spot_score_target_capacities"] == capacities
        # The default watch list survives a block that never mentions it.
        assert merged["watch_instance_types"]

    @settings(max_examples=40, deadline=None)
    @given(
        capacities=st.lists(
            st.one_of(st.integers(min_value=-5, max_value=100), st.booleans()),
            min_size=0,
            max_size=4,
        )
    )
    def test_capacity_lists_fail_closed_as_validation_errors(self, capacities: list[Any]) -> None:
        valid = bool(capacities) and all(
            isinstance(c, int) and not isinstance(c, bool) and c in (1, 10, 50) for c in capacities
        )
        context = _context_with("historical", {"spot_score_target_capacities": capacities})
        if valid:
            merged = ConfigLoader(MockApp(context)).get_capacity_history_config()
            assert merged["spot_score_target_capacities"] == capacities
        else:
            with pytest.raises(ConfigValidationError):
                ConfigLoader(MockApp(context))
