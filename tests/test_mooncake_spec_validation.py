"""Fail-fast validation of a ``mooncake`` endpoint-spec block.

An endpoint spec may carry an optional nested ``mooncake`` block describing
disaggregated prefill/decode serving and a shared KV-cache store.
:func:`cli.inference.validate_mooncake_spec` rejects a malformed block before
anything is written, and ``InferenceManager.deploy`` runs that check ahead of
touching the store so a rejected block never reaches DynamoDB.

This module walks every rejection branch of the validator, confirms each error
names the offending field, and proves that a rejected deploy persists nothing
while a well-formed one writes exactly once.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from cli.inference import (
    MOONCAKE_BYTE_SIZE_MAX,
    InferenceManager,
    validate_mooncake_spec,
)


def _valid_disaggregated() -> dict[str, Any]:
    """A minimal well-formed split-topology block used as a baseline."""
    return {"mode": "disaggregated", "topology": {"prefill": 2, "decode": 3}}


def _valid_store() -> dict[str, Any]:
    """A minimal well-formed single-instance store block."""
    return {"mode": "store", "store": {"enabled": True}}


# ---------------------------------------------------------------------------
# Baseline: well-formed blocks pass
# ---------------------------------------------------------------------------


class TestAcceptedBlocks:
    def test_disaggregated_topology_passes(self):
        validate_mooncake_spec(_valid_disaggregated())

    def test_store_only_block_passes(self):
        validate_mooncake_spec(_valid_store())

    def test_both_mode_with_topology_passes(self):
        validate_mooncake_spec({"mode": "both", "topology": {"prefill": 1, "decode": 1}})

    def test_cold_tier_with_enabled_store_passes(self):
        validate_mooncake_spec(
            {"mode": "store", "store": {"enabled": True, "cold_tier_enabled": True}}
        )

    def test_autoscaling_on_split_mode_passes(self):
        validate_mooncake_spec(
            {
                "mode": "disaggregated",
                "topology": {"prefill": 1, "decode": 1},
                "autoscaling": {
                    "enabled": True,
                    "prefill": {"min_replicas": 1, "max_replicas": 4},
                },
            }
        )


# ---------------------------------------------------------------------------
# Shape rejections
# ---------------------------------------------------------------------------


class TestShapeRejections:
    def test_non_mapping_block_is_rejected(self):
        with pytest.raises(ValueError, match="mooncake block must be a mapping"):
            validate_mooncake_spec(["not", "a", "mapping"])  # type: ignore[arg-type]

    def test_non_mapping_store_is_rejected(self):
        block = {"mode": "store", "store": "nope"}
        with pytest.raises(ValueError, match="mooncake.store must be a mapping"):
            validate_mooncake_spec(block)

    def test_non_mapping_autoscaling_is_rejected(self):
        block = _valid_disaggregated()
        block["autoscaling"] = "nope"
        with pytest.raises(ValueError, match="mooncake.autoscaling must be a mapping"):
            validate_mooncake_spec(block)

    def test_non_mapping_role_block_is_rejected(self):
        block = _valid_disaggregated()
        block["autoscaling"] = {"enabled": True, "prefill": "nope"}
        with pytest.raises(ValueError, match="mooncake.autoscaling.prefill must be a mapping"):
            validate_mooncake_spec(block)


# ---------------------------------------------------------------------------
# Mode rejection (also the allowed-mode listing surfaced by deploy)
# ---------------------------------------------------------------------------


class TestModeRejection:
    @pytest.mark.parametrize("bad_mode", ["", "disagg", "Store", "BOTH", None, 1])
    def test_unsupported_mode_is_rejected(self, bad_mode):
        with pytest.raises(ValueError, match="mooncake.mode must be one of"):
            validate_mooncake_spec({"mode": bad_mode})

    def test_mode_error_lists_the_allowed_values(self):
        with pytest.raises(ValueError) as exc:
            validate_mooncake_spec({"mode": "nope"})
        message = str(exc.value)
        assert "disaggregated" in message
        assert "store" in message
        assert "both" in message


# ---------------------------------------------------------------------------
# Byte-size field rejections
# ---------------------------------------------------------------------------


class TestByteSizeRejections:
    @pytest.mark.parametrize(
        "bad_value",
        [-1, "-1", "1.5", "2e9", "0x10", "", "abc", 1.0, True, MOONCAKE_BYTE_SIZE_MAX + 1],
    )
    def test_bad_global_segment_size_is_rejected(self, bad_value):
        block = {"mode": "store", "store": {"enabled": True, "global_segment_size": bad_value}}
        with pytest.raises(ValueError, match="mooncake.store.global_segment_size"):
            validate_mooncake_spec(block)

    @pytest.mark.parametrize("bad_value", [-1, "1.5", "nope", MOONCAKE_BYTE_SIZE_MAX + 1])
    def test_bad_local_buffer_size_is_rejected(self, bad_value):
        block = {"mode": "store", "store": {"enabled": True, "local_buffer_size": bad_value}}
        with pytest.raises(ValueError, match="mooncake.store.local_buffer_size"):
            validate_mooncake_spec(block)


# ---------------------------------------------------------------------------
# Topology rejections for split modes
# ---------------------------------------------------------------------------


class TestTopologyRejections:
    @pytest.mark.parametrize("mode", ["disaggregated", "both"])
    def test_missing_topology_is_rejected(self, mode):
        with pytest.raises(ValueError, match="mooncake.topology is required"):
            validate_mooncake_spec({"mode": mode})

    @pytest.mark.parametrize("mode", ["disaggregated", "both"])
    @pytest.mark.parametrize("field", ["prefill", "decode"])
    def test_missing_count_names_the_field(self, mode, field):
        topology = {"prefill": 2, "decode": 3}
        del topology[field]
        with pytest.raises(ValueError, match=f"mooncake.topology.{field} must be an integer"):
            validate_mooncake_spec({"mode": mode, "topology": topology})

    @pytest.mark.parametrize("field", ["prefill", "decode"])
    @pytest.mark.parametrize("bad", ["2", 1.5, None, True])
    def test_non_integer_count_is_rejected(self, field, bad):
        topology = {"prefill": 2, "decode": 3}
        topology[field] = bad
        with pytest.raises(ValueError, match=f"mooncake.topology.{field} must be an integer"):
            validate_mooncake_spec({"mode": "disaggregated", "topology": topology})

    @pytest.mark.parametrize("field", ["prefill", "decode"])
    @pytest.mark.parametrize("bad", [0, -1, 1001])
    def test_out_of_range_count_is_rejected(self, field, bad):
        topology = {"prefill": 2, "decode": 3}
        topology[field] = bad
        with pytest.raises(ValueError, match=f"mooncake.topology.{field} out of range"):
            validate_mooncake_spec({"mode": "both", "topology": topology})


# ---------------------------------------------------------------------------
# Cold-tier dependency rejection
# ---------------------------------------------------------------------------


class TestColdTierRejection:
    def test_cold_tier_without_enabled_store_is_rejected(self):
        block = {"mode": "store", "store": {"enabled": False, "cold_tier_enabled": True}}
        with pytest.raises(ValueError, match="cold_tier_enabled requires"):
            validate_mooncake_spec(block)

    def test_cold_tier_with_absent_store_enabled_is_rejected(self):
        block = {"mode": "store", "store": {"cold_tier_enabled": True}}
        with pytest.raises(ValueError, match="cold_tier_enabled requires"):
            validate_mooncake_spec(block)

    def test_cold_tier_error_names_both_conflicting_fields(self):
        block = {"mode": "store", "store": {"enabled": False, "cold_tier_enabled": True}}
        with pytest.raises(ValueError) as exc:
            validate_mooncake_spec(block)
        message = str(exc.value)
        assert "cold_tier_enabled" in message
        assert "enabled" in message


# ---------------------------------------------------------------------------
# Autoscaling rejections
# ---------------------------------------------------------------------------


class TestAutoscalingRejections:
    def test_autoscaling_on_store_mode_is_rejected(self):
        block = {
            "mode": "store",
            "store": {"enabled": True},
            "autoscaling": {"enabled": True},
        }
        with pytest.raises(ValueError, match="autoscaling.enabled requires"):
            validate_mooncake_spec(block)

    @pytest.mark.parametrize("role", ["prefill", "decode"])
    def test_min_replicas_below_one_is_rejected(self, role):
        block = _valid_disaggregated()
        block["autoscaling"] = {"enabled": True, role: {"min_replicas": 0}}
        with pytest.raises(
            ValueError, match=f"mooncake.autoscaling.{role}.min_replicas must be >= 1"
        ):
            validate_mooncake_spec(block)

    @pytest.mark.parametrize("role", ["prefill", "decode"])
    def test_max_replicas_below_min_is_rejected(self, role):
        block = _valid_disaggregated()
        block["autoscaling"] = {
            "enabled": True,
            role: {"min_replicas": 5, "max_replicas": 2},
        }
        with pytest.raises(
            ValueError, match=f"mooncake.autoscaling.{role}.max_replicas"
        ):
            validate_mooncake_spec(block)

    @pytest.mark.parametrize("bound", ["min_replicas", "max_replicas"])
    def test_non_integer_bound_is_rejected(self, bound):
        block = _valid_disaggregated()
        block["autoscaling"] = {"enabled": True, "prefill": {bound: "3"}}
        with pytest.raises(
            ValueError, match=f"mooncake.autoscaling.prefill.{bound} must be an integer"
        ):
            validate_mooncake_spec(block)

    def test_max_below_default_min_is_rejected(self):
        # With min_replicas absent the floor defaults to 1, so max_replicas 0
        # still violates the lower bound.
        block = _valid_disaggregated()
        block["autoscaling"] = {"enabled": True, "decode": {"max_replicas": 0}}
        with pytest.raises(ValueError, match="mooncake.autoscaling.decode.max_replicas"):
            validate_mooncake_spec(block)


# ---------------------------------------------------------------------------
# Validate-before-write behavior through InferenceManager.deploy
# ---------------------------------------------------------------------------


@pytest.fixture
def manager_with_spy_store():
    """An ``InferenceManager`` whose store is a spy that records writes.

    The constructor's config and AWS client are mocked so no real cdk.json or
    AWS session is touched, and ``_get_store`` returns a ``MagicMock`` standing
    in for the DynamoDB-backed store.
    """
    store = MagicMock()
    store.create_endpoint.return_value = {"endpoint_name": "ep"}
    with patch("cli.inference.get_aws_client", return_value=MagicMock()):
        mgr = InferenceManager(config=MagicMock())
    with patch.object(mgr, "_get_store", return_value=store):
        yield mgr, store


class TestDeployValidateBeforeWrite:
    def test_invalid_mooncake_mode_is_rejected_and_nothing_is_written(
        self, manager_with_spy_store
    ):
        mgr, store = manager_with_spy_store
        with pytest.raises(ValueError) as exc:
            mgr.deploy(
                "ep",
                target_regions=["us-east-1"],
                mooncake_mode="turbo",
            )
        message = str(exc.value)
        # The rejection names the three allowed mode values.
        assert "disaggregated" in message
        assert "store" in message
        assert "both" in message
        store.create_endpoint.assert_not_called()

    def test_out_of_range_topology_is_rejected_and_nothing_is_written(
        self, manager_with_spy_store
    ):
        mgr, store = manager_with_spy_store
        with pytest.raises(ValueError, match="mooncake.topology.prefill"):
            mgr.deploy(
                "ep",
                target_regions=["us-east-1"],
                mooncake_mode="disaggregated",
                prefill_replicas=0,
                decode_replicas=2,
            )
        store.create_endpoint.assert_not_called()

    def test_bad_store_byte_size_is_rejected_and_nothing_is_written(
        self, manager_with_spy_store
    ):
        mgr, store = manager_with_spy_store
        with pytest.raises(ValueError, match="mooncake.store.global_segment_size"):
            mgr.deploy(
                "ep",
                target_regions=["us-east-1"],
                mooncake_mode="store",
                mooncake_store={"enabled": True, "global_segment_size": -1},
            )
        store.create_endpoint.assert_not_called()

    def test_cold_tier_conflict_is_rejected_and_nothing_is_written(
        self, manager_with_spy_store
    ):
        mgr, store = manager_with_spy_store
        with pytest.raises(ValueError, match="cold_tier_enabled requires"):
            mgr.deploy(
                "ep",
                target_regions=["us-east-1"],
                mooncake_mode="store",
                mooncake_store={"enabled": False, "cold_tier_enabled": True},
            )
        store.create_endpoint.assert_not_called()

    def test_autoscaling_on_store_mode_is_rejected_and_nothing_is_written(
        self, manager_with_spy_store
    ):
        mgr, store = manager_with_spy_store
        with pytest.raises(ValueError, match="autoscaling.enabled requires"):
            mgr.deploy(
                "ep",
                target_regions=["us-east-1"],
                mooncake_mode="store",
                mooncake_store={"enabled": True},
                mooncake_autoscaling={"enabled": True},
            )
        store.create_endpoint.assert_not_called()

    def test_bad_autoscaling_bounds_are_rejected_and_nothing_is_written(
        self, manager_with_spy_store
    ):
        mgr, store = manager_with_spy_store
        with pytest.raises(ValueError, match="min_replicas must be >= 1"):
            mgr.deploy(
                "ep",
                target_regions=["us-east-1"],
                mooncake_mode="disaggregated",
                prefill_replicas=1,
                decode_replicas=1,
                mooncake_autoscaling={
                    "enabled": True,
                    "prefill": {"min_replicas": 0},
                },
            )
        store.create_endpoint.assert_not_called()

    def test_well_formed_disaggregated_deploy_writes_exactly_once(
        self, manager_with_spy_store
    ):
        # Positive control: a valid block reaches the store, proving the spy
        # would have caught a write on any of the rejection paths above.
        mgr, store = manager_with_spy_store
        mgr.deploy(
            "ep",
            image="vllm/vllm-openai:v0.8.0",
            target_regions=["us-east-1"],
            mooncake_mode="disaggregated",
            prefill_replicas=2,
            decode_replicas=3,
        )
        store.create_endpoint.assert_called_once()
        persisted_spec = store.create_endpoint.call_args.kwargs["spec"]
        assert persisted_spec["mooncake"]["topology"] == {"prefill": 2, "decode": 3}
