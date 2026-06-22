"""
Tests for the disaggregated-serving CLI and MCP surface.

Exercises ``cli.inference.InferenceManager`` —

* ``deploy`` falling back to the default upstream Mooncake-enabled vLLM image
  when
  a split-serving deploy supplies no explicit image, and rejecting an
  unsupported serving mode before anything is persisted;
* ``set_topology`` accepting integer prefill/decode counts in range and
  re-triggering reconciliation, while rejecting out-of-range counts and
  leaving the stored topology untouched;
* ``configure_store`` merging a new store block and re-triggering
  reconciliation, and reporting a not-found endpoint without writing.

It also drives the additive MCP tools (``deploy_disaggregated_inference``,
``set_mooncake_topology``, ``mooncake_topology_status``) to confirm they shell
through the CLI runner and land an audit-log entry whether the runner exits
zero or non-zero. The DynamoDB store and the CLI runner's subprocess call are
mocked so nothing touches AWS or a real ``gco`` binary.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli.inference import InferenceManager

# Ensure gco_mcp/ is importable, then load the server so every tool is registered.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

import run_mcp  # noqa: E402  (import for path/registration side effects)
from tools.inference import (  # noqa: E402
    deploy_disaggregated_inference,
    mooncake_topology_status,
    set_mooncake_topology,
)

assert run_mcp is not None  # keep the registration import from being pruned


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager() -> InferenceManager:
    """Build an InferenceManager without touching AWS in the constructor."""
    with patch("cli.inference.get_aws_client", return_value=MagicMock()):
        return InferenceManager(config=MagicMock())


def _audit_entries(caplog) -> list[dict]:
    """Pull the JSON audit entries out of caplog, in order."""
    return [
        json.loads(record.message) for record in caplog.records if record.name == "gco.mcp.audit"
    ]


# ===========================================================================
# InferenceManager.deploy
# ===========================================================================


class TestDeployDefaultImage:
    def test_split_deploy_without_image_uses_default_image(self):
        """A split-serving deploy with no image falls back to the default
        upstream Mooncake-enabled vLLM image and still persists the topology block."""
        mock_store = MagicMock()
        mock_store.create_endpoint.return_value = {"endpoint_name": "ep"}
        mgr = _make_manager()

        with (
            patch.object(mgr, "_get_store", return_value=mock_store),
            patch(
                "cli.images.default_disaggregated_image",
                return_value="123.dkr.ecr.us-east-1.amazonaws.com/gco/mooncake-vllm:v1",
            ) as mock_default,
        ):
            mgr.deploy(
                "ep",
                image=None,
                target_regions=["us-east-1"],
                mooncake_mode="disaggregated",
                prefill_replicas=2,
                decode_replicas=3,
                rewrite_image=False,
            )

        mock_default.assert_called_once()
        spec = mock_store.create_endpoint.call_args.kwargs["spec"]
        assert spec["image"] == "123.dkr.ecr.us-east-1.amazonaws.com/gco/mooncake-vllm:v1"
        assert spec["mooncake"]["mode"] == "disaggregated"
        assert spec["mooncake"]["topology"] == {"prefill": 2, "decode": 3}

    def test_explicit_image_is_preserved_for_split_deploy(self):
        """An explicitly supplied image is kept even when a mode is set."""
        mock_store = MagicMock()
        mock_store.create_endpoint.return_value = {"endpoint_name": "ep"}
        mgr = _make_manager()

        with (
            patch.object(mgr, "_get_store", return_value=mock_store),
            patch("cli.images.default_disaggregated_image") as mock_default,
        ):
            mgr.deploy(
                "ep",
                image="my/custom:v9",
                target_regions=["us-east-1"],
                mooncake_mode="store",
                rewrite_image=False,
            )

        mock_default.assert_not_called()
        spec = mock_store.create_endpoint.call_args.kwargs["spec"]
        assert spec["image"] == "my/custom:v9"

    def test_plain_deploy_without_image_is_rejected(self):
        """A non-split deploy still requires an explicit image."""
        mock_store = MagicMock()
        mgr = _make_manager()

        with (
            patch.object(mgr, "_get_store", return_value=mock_store),
            pytest.raises(ValueError, match="image is required"),
        ):
            mgr.deploy("ep", image=None, target_regions=["us-east-1"])

        mock_store.create_endpoint.assert_not_called()


class TestDeployModeRejection:
    def test_unsupported_mode_rejected_before_persistence(self):
        """An unsupported serving mode is rejected and nothing is persisted."""
        mock_store = MagicMock()
        mgr = _make_manager()

        with (
            patch.object(mgr, "_get_store", return_value=mock_store),
            pytest.raises(ValueError, match="mode"),
        ):
            mgr.deploy(
                "ep",
                image="img:v1",
                target_regions=["us-east-1"],
                mooncake_mode="turbo",
                rewrite_image=False,
            )

        mock_store.create_endpoint.assert_not_called()

    def test_rejection_names_allowed_modes(self):
        """The rejection message lists the allowed mode values."""
        mgr = _make_manager()
        with (
            patch.object(mgr, "_get_store", return_value=MagicMock()),
            pytest.raises(ValueError) as excinfo,
        ):
            mgr.deploy(
                "ep",
                image="img:v1",
                target_regions=["us-east-1"],
                mooncake_mode="turbo",
                rewrite_image=False,
            )
        message = str(excinfo.value)
        assert "disaggregated" in message
        assert "store" in message
        assert "both" in message


# ===========================================================================
# InferenceManager.set_topology
# ===========================================================================


class TestSetTopology:
    def test_valid_counts_update_topology_and_retrigger(self):
        """In-range counts replace the topology and flip the endpoint back to
        deploying via update_spec."""
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "ep",
            "spec": {"image": "img:v1", "mooncake": {"mode": "disaggregated"}},
        }
        mock_store.update_spec.return_value = {"endpoint_name": "ep", "desired_state": "deploying"}
        mgr = _make_manager()

        with patch.object(mgr, "_get_store", return_value=mock_store):
            result = mgr.set_topology("ep", prefill=4, decode=5)

        assert result == {"endpoint_name": "ep", "desired_state": "deploying"}
        name_arg, spec_arg = mock_store.update_spec.call_args.args
        assert name_arg == "ep"
        assert spec_arg["mooncake"]["topology"] == {"prefill": 4, "decode": 5}
        # Existing mooncake fields are preserved.
        assert spec_arg["mooncake"]["mode"] == "disaggregated"

    def test_out_of_range_count_rejected_without_touching_store(self):
        """A count above the allowed maximum is rejected before any read or
        write, leaving the stored topology and desired_state unchanged."""
        mock_store = MagicMock()
        mgr = _make_manager()

        with (
            patch.object(mgr, "_get_store", return_value=mock_store),
            pytest.raises(ValueError, match="decode"),
        ):
            mgr.set_topology("ep", prefill=2, decode=5000)

        mock_store.get_endpoint.assert_not_called()
        mock_store.update_spec.assert_not_called()

    def test_non_integer_count_rejected(self):
        """A non-integer count is rejected and names the offending field."""
        mock_store = MagicMock()
        mgr = _make_manager()

        with (
            patch.object(mgr, "_get_store", return_value=mock_store),
            pytest.raises(ValueError, match="prefill"),
        ):
            mgr.set_topology("ep", prefill="two", decode=1)  # type: ignore[arg-type]

        mock_store.update_spec.assert_not_called()

    def test_missing_endpoint_returns_none(self):
        """A topology change against an unknown endpoint returns None and
        writes nothing."""
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = None
        mgr = _make_manager()

        with patch.object(mgr, "_get_store", return_value=mock_store):
            result = mgr.set_topology("ghost", prefill=1, decode=1)

        assert result is None
        mock_store.update_spec.assert_not_called()


# ===========================================================================
# InferenceManager.configure_store
# ===========================================================================


class TestConfigureStore:
    def test_store_block_merged_and_retriggered(self):
        """A new store block is merged into the spec and reconciliation is
        re-triggered through update_spec."""
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "ep",
            "spec": {
                "image": "img:v1",
                "mooncake": {"mode": "both", "topology": {"prefill": 1, "decode": 1}},
            },
        }
        mock_store.update_spec.return_value = {"endpoint_name": "ep", "desired_state": "deploying"}
        mgr = _make_manager()

        with patch.object(mgr, "_get_store", return_value=mock_store):
            result = mgr.configure_store(
                "ep",
                {"enabled": True, "protocol": "rdma", "global_segment_size": 1024},
            )

        assert result == {"endpoint_name": "ep", "desired_state": "deploying"}
        _name_arg, spec_arg = mock_store.update_spec.call_args.args
        store_block = spec_arg["mooncake"]["store"]
        assert store_block["enabled"] is True
        assert store_block["protocol"] == "rdma"
        # Byte-size fields are authored as base-10 integer decimal strings.
        assert store_block["global_segment_size"] == "1024"
        # Pre-existing mooncake fields survive the merge.
        assert spec_arg["mooncake"]["mode"] == "both"

    def test_missing_endpoint_returns_none(self):
        """Configuring the store on a non-existent endpoint returns None and
        modifies nothing."""
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = None
        mgr = _make_manager()

        with patch.object(mgr, "_get_store", return_value=mock_store):
            result = mgr.configure_store("ghost", {"enabled": True})

        assert result is None
        mock_store.update_spec.assert_not_called()

    def test_invalid_store_size_rejected_without_writing(self):
        """An out-of-range byte-size field is rejected and the stored spec is
        left untouched."""
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "ep",
            "spec": {"mooncake": {"mode": "store"}},
        }
        mgr = _make_manager()

        with (
            patch.object(mgr, "_get_store", return_value=mock_store),
            pytest.raises(ValueError, match="global_segment_size"),
        ):
            mgr.configure_store("ep", {"enabled": True, "global_segment_size": -5})

        mock_store.update_spec.assert_not_called()


# ===========================================================================
# MCP tools — audited shell-out on success and non-zero exit
# ===========================================================================


class TestDeployDisaggregatedInferenceTool:
    def test_success_shell_out_is_audited(self, caplog):
        with (
            caplog.at_level(logging.INFO, logger="gco.mcp.audit"),
            patch("cli_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(
                returncode=0, stdout='{"endpoint_name":"pd"}', stderr=""
            )
            result = deploy_disaggregated_inference("pd", prefill=2, decode=2)

        assert result == '{"endpoint_name":"pd"}'
        cmd = mock_run.call_args[0][0]
        assert "deploy" in cmd
        assert "pd" in cmd
        assert "--mooncake-mode" in cmd
        assert "--prefill-replicas" in cmd
        assert "--decode-replicas" in cmd

        entries = _audit_entries(caplog)
        assert len(entries) == 1
        assert entries[0]["tool"] == "deploy_disaggregated_inference"
        assert entries[0]["status"] == "success"

    def test_non_zero_exit_is_recorded(self, caplog):
        """A non-zero CLI exit is surfaced as an error payload and still
        produces an audit-log entry."""
        with (
            caplog.at_level(logging.INFO, logger="gco.mcp.audit"),
            patch("cli_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom: no regions")
            result = deploy_disaggregated_inference("pd")

        payload = json.loads(result)
        assert payload["exit_code"] == 1
        assert "boom: no regions" in payload["error"]

        entries = _audit_entries(caplog)
        assert len(entries) == 1
        assert entries[0]["tool"] == "deploy_disaggregated_inference"

    def test_image_passed_through_when_supplied(self, caplog):
        with patch("cli_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            deploy_disaggregated_inference("pd", image="my/img:v2")
        cmd = mock_run.call_args[0][0]
        assert "-i" in cmd
        assert "my/img:v2" in cmd


class TestSetMooncakeTopologyTool:
    def test_success_shell_out_is_audited(self, caplog):
        with (
            caplog.at_level(logging.INFO, logger="gco.mcp.audit"),
            patch("cli_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            result = set_mooncake_topology("pd", prefill=3, decode=4)

        assert result == "{}"
        cmd = mock_run.call_args[0][0]
        assert "set-topology" in cmd
        assert "--prefill" in cmd
        assert "3" in cmd
        assert "--decode" in cmd
        assert "4" in cmd

        entries = _audit_entries(caplog)
        assert len(entries) == 1
        assert entries[0]["tool"] == "set_mooncake_topology"
        assert entries[0]["status"] == "success"

    def test_non_zero_exit_is_recorded(self, caplog):
        with (
            caplog.at_level(logging.INFO, logger="gco.mcp.audit"),
            patch("cli_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="count out of range")
            result = set_mooncake_topology("pd", prefill=1, decode=99999)

        payload = json.loads(result)
        assert payload["exit_code"] == 2
        assert "count out of range" in payload["error"]

        entries = _audit_entries(caplog)
        assert len(entries) == 1
        assert entries[0]["tool"] == "set_mooncake_topology"


class TestMooncakeTopologyStatusTool:
    def test_success_shell_out_is_audited(self, caplog):
        with (
            caplog.at_level(logging.INFO, logger="gco.mcp.audit"),
            patch("cli_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(
                returncode=0, stdout='{"region_status":{}}', stderr=""
            )
            result = mooncake_topology_status("pd")

        assert result == '{"region_status":{}}'
        cmd = mock_run.call_args[0][0]
        assert "status" in cmd
        assert "pd" in cmd

        entries = _audit_entries(caplog)
        assert len(entries) == 1
        assert entries[0]["tool"] == "mooncake_topology_status"
        assert entries[0]["status"] == "success"

    def test_non_zero_exit_is_recorded(self, caplog):
        with (
            caplog.at_level(logging.INFO, logger="gco.mcp.audit"),
            patch("cli_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="endpoint not found")
            result = mooncake_topology_status("ghost")

        payload = json.loads(result)
        assert payload["exit_code"] == 1
        assert "endpoint not found" in payload["error"]

        entries = _audit_entries(caplog)
        assert len(entries) == 1
        assert entries[0]["tool"] == "mooncake_topology_status"
