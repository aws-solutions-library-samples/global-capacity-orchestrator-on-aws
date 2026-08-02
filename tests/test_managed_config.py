"""Tests for the managed deployment-config engine and its region veneers.

Covers the three layers introduced for issue #221:

* ``cli/managed_config.py`` — the engine: writable-config resolution,
  result-only validation (including the repair path), idempotent no-ops,
  atomic writes that preserve comments/order/mode/trailing-newline, the
  uniform :class:`ChangeReport`, and the ``gco.cli.managed_config`` audit
  log lines.
* ``gco stacks regions list/add/remove`` — the Click veneers (CliRunner).
* ``list/add/remove_deployment_region`` — the MCP tools: absent by default,
  registered under ``GCO_ENABLE_CONFIG_MANAGEMENT=true``, and shelling to
  the documented CLI argv.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
import os
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli.main import cli
from cli.managed_config import (
    BEDROCK_DEFAULT_MODEL,
    DEPLOYMENT_REGION_SCALARS,
    REGIONAL_DEPLOYMENT_REGIONS,
    ChangeReport,
    ManagedConfigError,
    add_deployment_region,
    get_bedrock_model_status,
    get_deployment_regions_status,
    remove_deployment_region,
    set_default_bedrock_model,
    set_deployment_region_role,
)

# Ensure gco_mcp/ is importable, mirroring the other MCP test modules.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

import run_mcp  # noqa: E402

REGION_TOOLS = (
    "list_deployment_regions",
    "add_deployment_region",
    "remove_deployment_region",
    "set_deployment_region",
    "set_default_bedrock_model",
)

BASE_CONFIG: dict = {
    "app": "python3 app.py",
    "context": {
        "_comment_deployment_regions": "where each stack class deploys",
        "deployment_regions": {
            "global": "us-east-2",
            "api_gateway": "us-east-2",
            "monitoring": "us-east-2",
            "regional": ["us-east-1"],
        },
        "bedrock": {
            "default_model_id": "global.anthropic.claude-opus-5",
            "thinking": {"effort": "high"},
        },
        "project_name": "gco",
    },
}


@pytest.fixture()
def cdk_json(tmp_path: Path) -> Path:
    """A realistic cdk.json fixture (comment key first, trailing newline)."""
    path = tmp_path / "cdk.json"
    path.write_text(json.dumps(BASE_CONFIG, indent=2) + "\n", encoding="utf-8")
    return path


# =============================================================================
# Engine: resolution
# =============================================================================


class TestEngineResolution:
    def test_explicit_path_must_exist(self, tmp_path: Path):
        with pytest.raises(ManagedConfigError, match="does not exist"):
            get_deployment_regions_status(config_path=tmp_path / "missing.json")

    def test_no_cdk_json_found_names_the_remedies(self):
        with (
            patch("cli.managed_config._find_cdk_json", return_value=None),
            pytest.raises(ManagedConfigError) as excinfo,
        ):
            get_deployment_regions_status()
        message = str(excinfo.value)
        assert "--config-path" in message
        assert "uvx/pip" in message

    def test_default_resolution_uses_find_cdk_json(self, cdk_json: Path):
        with patch("cli.managed_config._find_cdk_json", return_value=cdk_json):
            status = get_deployment_regions_status()
        assert status["config_path"] == str(cdk_json)


# =============================================================================
# Engine: validation of the result (and only the result)
# =============================================================================


class TestEngineValidation:
    def test_unknown_region_rejected_without_write(self, cdk_json: Path):
        before = cdk_json.read_bytes()
        with pytest.raises(ManagedConfigError, match="Invalid region 'xx-bogus-9'"):
            add_deployment_region("xx-bogus-9", config_path=cdk_json)
        assert cdk_json.read_bytes() == before

    def test_cross_partition_add_rejected(self, cdk_json: Path):
        with pytest.raises(ManagedConfigError, match="single AWS partition"):
            add_deployment_region("cn-north-1", config_path=cdk_json)

    def test_removing_last_region_rejected(self, cdk_json: Path):
        with pytest.raises(ManagedConfigError, match="At least one region"):
            remove_deployment_region("us-east-1", config_path=cdk_json)

    def test_malformed_json_refused(self, tmp_path: Path):
        path = tmp_path / "cdk.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ManagedConfigError, match="not valid JSON"):
            add_deployment_region("us-west-2", config_path=path)

    def test_missing_context_refused(self, tmp_path: Path):
        path = tmp_path / "cdk.json"
        path.write_text(json.dumps({"app": "x"}), encoding="utf-8")
        with pytest.raises(ManagedConfigError, match="does not look like a GCO cdk.json"):
            add_deployment_region("us-west-2", config_path=path)

    def test_container_wrong_type_refused(self, tmp_path: Path):
        path = tmp_path / "cdk.json"
        path.write_text(
            json.dumps({"context": {"deployment_regions": ["not", "a", "dict"]}}),
            encoding="utf-8",
        )
        with pytest.raises(ManagedConfigError, match="must be a JSON object"):
            add_deployment_region("us-west-2", config_path=path)

    def test_leaf_wrong_type_refused(self, tmp_path: Path):
        path = tmp_path / "cdk.json"
        path.write_text(
            json.dumps({"context": {"deployment_regions": {"regional": "us-east-1"}}}),
            encoding="utf-8",
        )
        with pytest.raises(ManagedConfigError, match="must be a JSON array"):
            add_deployment_region("us-west-2", config_path=path)

    def test_absent_container_starts_from_effective_default(self, tmp_path: Path):
        # No deployment_regions key at all: the effective default regional
        # list is ["us-east-1"]; an add materializes only the managed leaf.
        path = tmp_path / "cdk.json"
        path.write_text(json.dumps({"context": {"project_name": "gco"}}), encoding="utf-8")
        report = add_deployment_region("us-west-2", config_path=path)
        assert report.old == ("us-east-1",)
        assert report.new == ("us-east-1", "us-west-2")
        written = json.loads(path.read_text(encoding="utf-8"))
        assert written["context"]["deployment_regions"] == {"regional": ["us-east-1", "us-west-2"]}
        # Sibling scalars stay unmaterialized (reader defaults keep applying).
        assert "global" not in written["context"]["deployment_regions"]


# =============================================================================
# Engine: idempotency
# =============================================================================


class TestEngineIdempotency:
    def test_re_adding_present_region_is_reported_noop(self, cdk_json: Path):
        before = cdk_json.read_bytes()
        report = add_deployment_region("us-east-1", config_path=cdk_json)
        assert report.changed is False
        assert report.old == report.new == ("us-east-1",)
        assert "already present" in report.summary()
        assert cdk_json.read_bytes() == before  # no write at all

    def test_removing_absent_region_is_reported_noop(self, cdk_json: Path):
        before = cdk_json.read_bytes()
        report = remove_deployment_region("eu-west-1", config_path=cdk_json)
        assert report.changed is False
        assert "not present" in report.summary()
        assert cdk_json.read_bytes() == before


# =============================================================================
# Engine: write mechanics
# =============================================================================


class TestEngineWriteMechanics:
    def test_comments_order_and_newline_survive(self, cdk_json: Path):
        report = add_deployment_region("us-west-2", config_path=cdk_json)
        assert isinstance(report, ChangeReport)
        assert report.changed is True
        raw = cdk_json.read_text(encoding="utf-8")
        written = json.loads(raw)
        keys = list(written["context"])
        assert keys[0] == "_comment_deployment_regions"  # placement preserved
        assert keys == list(BASE_CONFIG["context"])  # full order preserved
        assert raw.endswith("\n") and not raw.endswith("\n\n")
        assert written["context"]["deployment_regions"]["regional"] == [
            "us-east-1",
            "us-west-2",
        ]

    def test_no_trailing_newline_stays_that_way(self, tmp_path: Path):
        path = tmp_path / "cdk.json"
        path.write_text(json.dumps(BASE_CONFIG, indent=2), encoding="utf-8")
        add_deployment_region("us-west-2", config_path=path)
        assert not path.read_text(encoding="utf-8").endswith("\n")

    def test_file_mode_preserved(self, cdk_json: Path):
        os.chmod(cdk_json, 0o600)
        add_deployment_region("us-west-2", config_path=cdk_json)
        assert stat.S_IMODE(cdk_json.stat().st_mode) == 0o600

    def test_read_only_target_refused_with_guidance(self, cdk_json: Path):
        os.chmod(cdk_json, 0o444)
        try:
            with pytest.raises(ManagedConfigError, match="uvx/pip"):
                add_deployment_region("us-west-2", config_path=cdk_json)
        finally:
            os.chmod(cdk_json, 0o644)

    def test_change_report_summary_names_transition(self, cdk_json: Path):
        report = add_deployment_region("us-west-2", config_path=cdk_json)
        summary = report.summary()
        assert "deployment_regions.regional" in summary
        assert "us-west-2" in summary
        assert str(cdk_json) in summary


# =============================================================================
# Engine: repair path (validate the result, not the starting state)
# =============================================================================


class TestEngineRepair:
    def test_bogus_entry_can_be_removed_from_broken_config(self, tmp_path: Path):
        broken = json.loads(json.dumps(BASE_CONFIG))
        broken["context"]["deployment_regions"]["regional"] = ["us-east-1", "xx-typo-1"]
        path = tmp_path / "cdk.json"
        path.write_text(json.dumps(broken, indent=2), encoding="utf-8")

        report = remove_deployment_region("xx-typo-1", config_path=path)
        assert report.changed is True
        assert report.new == ("us-east-1",)
        assert get_deployment_regions_status(config_path=path)["partition"] == "aws"


# =============================================================================
# Engine: status
# =============================================================================


class TestEngineStatus:
    def test_effective_defaults_when_keys_absent(self, tmp_path: Path):
        path = tmp_path / "cdk.json"
        path.write_text(json.dumps({"context": {}}), encoding="utf-8")
        status = get_deployment_regions_status(config_path=path)
        assert status["global"] == "us-east-2"
        assert status["api_gateway"] == "us-east-2"
        assert status["monitoring"] == "us-east-2"
        assert status["regional"] == ["us-east-1"]
        assert status["partition"] == "aws"

    def test_broken_config_reports_partition_error(self, tmp_path: Path):
        broken = json.loads(json.dumps(BASE_CONFIG))
        broken["context"]["deployment_regions"]["regional"] = ["us-east-1", "xx-typo-1"]
        path = tmp_path / "cdk.json"
        path.write_text(json.dumps(broken), encoding="utf-8")
        status = get_deployment_regions_status(config_path=path)
        assert status["partition"] is None
        assert "xx-typo-1" in status["partition_error"]


# =============================================================================
# Engine: audit logging
# =============================================================================


class TestEngineAudit:
    LOGGER = "gco.cli.managed_config"

    def test_write_logs_info(self, cdk_json: Path, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.INFO, logger=self.LOGGER):
            add_deployment_region("us-west-2", config_path=cdk_json)
        line = next(r for r in caplog.records if "managed-config write" in r.getMessage())
        message = line.getMessage()
        assert "key=deployment_regions.regional" in message
        assert "action=add" in message
        assert "value=us-west-2" in message

    def test_noop_logs_info(self, cdk_json: Path, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.INFO, logger=self.LOGGER):
            add_deployment_region("us-east-1", config_path=cdk_json)
        assert any("managed-config no-op" in r.getMessage() for r in caplog.records)

    def test_refusal_logs_warning(self, cdk_json: Path, caplog: pytest.LogCaptureFixture):
        with (
            caplog.at_level(logging.WARNING, logger=self.LOGGER),
            pytest.raises(ManagedConfigError),
        ):
            add_deployment_region("xx-bogus-9", config_path=cdk_json)
        refused = [r for r in caplog.records if "managed-config refused" in r.getMessage()]
        assert refused and refused[0].levelno == logging.WARNING


# =============================================================================
# Engine: scalar keys (region roles + bedrock default model)
# =============================================================================


class TestEngineScalars:
    def test_set_role_scalar_writes_and_reports(self, cdk_json: Path):
        report = set_deployment_region_role("monitoring", "us-west-2", config_path=cdk_json)
        assert report.changed is True
        assert report.action == "set"
        assert report.old == "us-east-2"
        assert report.new == "us-west-2"
        written = json.loads(cdk_json.read_text(encoding="utf-8"))
        assert written["context"]["deployment_regions"]["monitoring"] == "us-west-2"
        # Untouched siblings stay untouched.
        assert written["context"]["deployment_regions"]["global"] == "us-east-2"

    def test_set_role_scalar_is_idempotent(self, cdk_json: Path):
        before = cdk_json.read_bytes()
        report = set_deployment_region_role("global", "us-east-2", config_path=cdk_json)
        assert report.changed is False
        assert "already the value" in report.summary()
        assert cdk_json.read_bytes() == before

    def test_set_role_scalar_unknown_region_rejected(self, cdk_json: Path):
        with pytest.raises(ManagedConfigError, match="Invalid region 'xx-bogus-9'"):
            set_deployment_region_role("global", "xx-bogus-9", config_path=cdk_json)

    def test_set_role_scalar_cross_partition_rejected(self, cdk_json: Path):
        with pytest.raises(ManagedConfigError, match="single AWS partition"):
            set_deployment_region_role("api_gateway", "cn-north-1", config_path=cdk_json)

    def test_unknown_role_rejected(self, cdk_json: Path):
        with pytest.raises(ManagedConfigError, match="unknown deployment-region role"):
            set_deployment_region_role("bogus_role", "us-east-1", config_path=cdk_json)

    def test_scalar_wrong_type_refused(self, tmp_path: Path):
        path = tmp_path / "cdk.json"
        path.write_text(
            json.dumps({"context": {"deployment_regions": {"global": 42}}}),
            encoding="utf-8",
        )
        with pytest.raises(ManagedConfigError, match="must be a JSON string"):
            set_deployment_region_role("global", "us-east-1", config_path=path)

    def test_set_bedrock_model_preserves_thinking_sibling(self, cdk_json: Path):
        report = set_default_bedrock_model("us.amazon.nova-2-lite-v1:0", config_path=cdk_json)
        assert report.changed is True
        written = json.loads(cdk_json.read_text(encoding="utf-8"))
        bedrock = written["context"]["bedrock"]
        assert bedrock["default_model_id"] == "us.amazon.nova-2-lite-v1:0"
        assert bedrock["thinking"] == {"effort": "high"}

    def test_set_bedrock_model_empty_rejected(self, cdk_json: Path):
        with pytest.raises(ManagedConfigError, match="non-empty string"):
            set_default_bedrock_model("   ", config_path=cdk_json)

    def test_set_bedrock_model_surrounding_whitespace_rejected(self, cdk_json: Path):
        with pytest.raises(ManagedConfigError, match="whitespace"):
            set_default_bedrock_model(" model-id ", config_path=cdk_json)

    def test_bedrock_status_reads_configured_value(self, cdk_json: Path):
        status = get_bedrock_model_status(config_path=cdk_json)
        assert status["default_model_id"] == "global.anthropic.claude-opus-5"
        assert status["config_path"] == str(cdk_json)

    def test_bedrock_container_materialized_when_absent(self, tmp_path: Path):
        path = tmp_path / "cdk.json"
        path.write_text(json.dumps({"context": {"project_name": "gco"}}), encoding="utf-8")
        report = set_default_bedrock_model("global.anthropic.claude-opus-5", config_path=path)
        assert report.changed is True
        assert report.old == ""  # the reader-level "unset" default
        written = json.loads(path.read_text(encoding="utf-8"))
        assert written["context"]["bedrock"] == {
            "default_model_id": "global.anthropic.claude-opus-5"
        }


# =============================================================================
# CLI veneers: gco stacks regions list/add/remove
# =============================================================================


class TestRegionsCli:
    def test_list_json_round_trips_the_status(self, cdk_json: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--output", "json", "stacks", "regions", "list", "--config-path", str(cdk_json)],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["regional"] == ["us-east-1"]  # real list on the MCP path
        assert payload["partition"] == "aws"

    def test_list_table_joins_regional_for_humans(self, cdk_json: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["stacks", "regions", "list", "--config-path", str(cdk_json)])
        assert result.exit_code == 0, result.output
        assert "us-east-1" in result.output
        assert "[1 items]" not in result.output

    def test_add_with_yes_writes_and_hints_deploy(self, cdk_json: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["stacks", "regions", "add", "us-west-2", "--config-path", str(cdk_json), "-y"],
        )
        assert result.exit_code == 0, result.output
        assert "add 'us-west-2'" in result.output
        assert "no stacks were deployed" in result.output.lower()
        written = json.loads(cdk_json.read_text(encoding="utf-8"))
        assert written["context"]["deployment_regions"]["regional"] == [
            "us-east-1",
            "us-west-2",
        ]

    def test_add_declined_confirmation_aborts_without_write(self, cdk_json: Path):
        before = cdk_json.read_bytes()
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["stacks", "regions", "add", "us-west-2", "--config-path", str(cdk_json)],
            input="n\n",
        )
        assert result.exit_code != 0
        assert cdk_json.read_bytes() == before

    def test_add_invalid_region_exits_nonzero(self, cdk_json: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["stacks", "regions", "add", "xx-bogus-9", "--config-path", str(cdk_json), "-y"],
        )
        assert result.exit_code == 1
        assert "refusing to update" in result.output

    def test_remove_warns_stack_not_destroyed(self, cdk_json: Path):
        runner = CliRunner()
        runner.invoke(
            cli,
            ["stacks", "regions", "add", "us-west-2", "--config-path", str(cdk_json), "-y"],
        )
        result = runner.invoke(
            cli,
            [
                "stacks",
                "regions",
                "remove",
                "us-west-2",
                "--config-path",
                str(cdk_json),
                "-y",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "remove 'us-west-2'" in result.output
        assert "destroy" in result.output.lower()

    def test_remove_absent_region_is_noop_success(self, cdk_json: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "stacks",
                "regions",
                "remove",
                "eu-west-1",
                "--config-path",
                str(cdk_json),
                "-y",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "no change" in result.output

    def test_set_role_with_yes_writes_and_hints(self, cdk_json: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "stacks",
                "regions",
                "set",
                "monitoring",
                "us-west-2",
                "--config-path",
                str(cdk_json),
                "-y",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "set 'us-west-2'" in result.output
        assert "deploy-all" in result.output
        written = json.loads(cdk_json.read_text(encoding="utf-8"))
        assert written["context"]["deployment_regions"]["monitoring"] == "us-west-2"

    def test_set_rejects_bad_role_at_parse_time(self, cdk_json: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["stacks", "regions", "set", "bogus", "us-east-1", "--config-path", str(cdk_json)],
        )
        assert result.exit_code == 2  # click.Choice rejects before our code runs

    def test_set_declined_confirmation_aborts_without_write(self, cdk_json: Path):
        before = cdk_json.read_bytes()
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "stacks",
                "regions",
                "set",
                "global",
                "us-west-2",
                "--config-path",
                str(cdk_json),
            ],
            input="n\n",
        )
        assert result.exit_code != 0
        assert cdk_json.read_bytes() == before


class TestBedrockCli:
    def test_show_reports_model_and_path(self, cdk_json: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--output", "json", "stacks", "bedrock", "show", "--config-path", str(cdk_json)],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["default_model_id"] == "global.anthropic.claude-opus-5"

    def test_set_model_with_yes_writes(self, cdk_json: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "stacks",
                "bedrock",
                "set-model",
                "us.amazon.nova-2-lite-v1:0",
                "--config-path",
                str(cdk_json),
                "-y",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "set 'us.amazon.nova-2-lite-v1:0'" in result.output
        written = json.loads(cdk_json.read_text(encoding="utf-8"))
        assert written["context"]["bedrock"]["default_model_id"] == "us.amazon.nova-2-lite-v1:0"

    def test_set_model_empty_exits_nonzero(self, cdk_json: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["stacks", "bedrock", "set-model", "  ", "--config-path", str(cdk_json), "-y"],
        )
        assert result.exit_code == 1
        assert "refusing to update" in result.output


# =============================================================================
# MCP tools: gating + argv translation
# =============================================================================


def _strip_region_tools() -> None:
    """Remove the gated region tools from the module-level mcp singleton."""
    for name in REGION_TOOLS:
        with contextlib.suppress(Exception):
            run_mcp.mcp.local_provider.remove_tool(name)


def _list_tool_names() -> set[str]:
    tools = asyncio.run(run_mcp.mcp._list_tools())
    return {t.name for t in tools}


class TestMcpRegionToolsGating:
    @pytest.fixture(autouse=True)
    def _clean(self):
        _strip_region_tools()
        importlib.reload(run_mcp)
        _strip_region_tools()

    def test_absent_by_default(self):
        names = _list_tool_names()
        for tool in REGION_TOOLS:
            assert tool not in names, f"{tool} leaked past the flag gate"

    def test_register_under_config_management_flag(self):
        with patch.dict(os.environ, {"GCO_ENABLE_CONFIG_MANAGEMENT": "true"}):
            importlib.reload(run_mcp)
            names = _list_tool_names()
        for tool in REGION_TOOLS:
            assert tool in names, f"{tool} missing under GCO_ENABLE_CONFIG_MANAGEMENT"
            assert hasattr(run_mcp, tool)


class TestMcpRegionToolsArgv:
    """The gated tools shell to the documented `gco stacks regions` argv."""

    @patch.dict(os.environ, {"GCO_ENABLE_CONFIG_MANAGEMENT": "true"})
    def test_list_argv(self):
        importlib.reload(run_mcp)
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.list_deployment_regions()
            cmd = mock.call_args[0][0]
        assert cmd[-3:] == ["stacks", "regions", "list"]

    @patch.dict(os.environ, {"GCO_ENABLE_CONFIG_MANAGEMENT": "true"})
    def test_add_argv(self):
        importlib.reload(run_mcp)
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.add_deployment_region(region="us-west-2")
            cmd = mock.call_args[0][0]
        assert cmd[-5:] == ["stacks", "regions", "add", "us-west-2", "-y"]

    @patch.dict(os.environ, {"GCO_ENABLE_CONFIG_MANAGEMENT": "true"})
    def test_remove_argv(self):
        importlib.reload(run_mcp)
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.remove_deployment_region(region="us-west-2")
            cmd = mock.call_args[0][0]
        assert cmd[-5:] == ["stacks", "regions", "remove", "us-west-2", "-y"]

    @patch.dict(os.environ, {"GCO_ENABLE_CONFIG_MANAGEMENT": "true"})
    def test_set_role_argv(self):
        importlib.reload(run_mcp)
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.set_deployment_region(role="monitoring", region="us-west-2")
            cmd = mock.call_args[0][0]
        assert cmd[-6:] == ["stacks", "regions", "set", "monitoring", "us-west-2", "-y"]

    @patch.dict(os.environ, {"GCO_ENABLE_CONFIG_MANAGEMENT": "true"})
    def test_set_bedrock_model_argv(self):
        importlib.reload(run_mcp)
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.set_default_bedrock_model(model_id="global.anthropic.claude-opus-5")
            cmd = mock.call_args[0][0]
        assert cmd[-5:] == [
            "stacks",
            "bedrock",
            "set-model",
            "global.anthropic.claude-opus-5",
            "-y",
        ]


# =============================================================================
# Registry contract
# =============================================================================


class TestRegistryContract:
    def test_registry_entry_shape(self):
        key = REGIONAL_DEPLOYMENT_REGIONS
        assert key.key_id == "deployment_regions.regional"
        assert key.container == "deployment_regions"
        assert key.leaf == "regional"
        assert key.default == ("us-east-1",)

    def test_scalar_registry_covers_the_three_roles(self):
        assert sorted(DEPLOYMENT_REGION_SCALARS) == ["api_gateway", "global", "monitoring"]
        for role, key in DEPLOYMENT_REGION_SCALARS.items():
            assert key.key_id == f"deployment_regions.{role}"
            assert key.container == "deployment_regions"
            assert key.leaf == role
            assert key.default == "us-east-2"  # matches the reader contract

    def test_bedrock_registry_entry_shape(self):
        key = BEDROCK_DEFAULT_MODEL
        assert key.key_id == "bedrock.default_model_id"
        assert key.container == "bedrock"
        assert key.leaf == "default_model_id"
