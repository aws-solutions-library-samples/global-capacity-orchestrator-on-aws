"""
Tests for ``gco stacks addons`` (status / install) and the ``--all-regions`` flag.

Add-on installation is decoupled from the CloudFormation rollback path: these
commands inspect per-chart status recorded in SSM and re-trigger the installer
state machine without a full redeploy. ``--all-regions`` fans the operation out
across every configured regional deployment region.

Covered:
* ``_target_regions``: single-region (explicit / first-regional / default) and
  all-regions resolution from cdk.json.
* ``addons status`` for one region and ``--all-regions`` (both regions shown).
* ``addons install`` for one region and ``--all-regions`` (execution started in
  each), plus the missing-input error path and a non-zero exit when a region
  has no persisted input.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import boto3
import pytest
from click.testing import CliRunner
from moto import mock_aws


@pytest.fixture
def aws_creds_env():
    """Deterministic moto-friendly AWS credentials."""
    previous = {k: os.environ.get(k) for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")}
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    yield
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _seed_addon_status(region: str, project: str, chart: str, status: str) -> None:
    boto3.client("ssm", region_name=region).put_parameter(
        Name=f"/{project}/addons/{region}/{chart}",
        Value=json.dumps({"chart": chart, "status": status, "message": "ok"}),
        Type="String",
        Overwrite=True,
    )


def _seed_addon_input(region: str, project: str) -> None:
    # The orchestrator persists the replay input zlib+base64 encoded because
    # SSM rejects raw {{PLACEHOLDER}} tokens; seed the same encoding.
    import base64
    import zlib

    raw = json.dumps(
        {
            "ClusterName": f"{project}-{region}",
            "ImageReplacements": {"{{CLUSTER_NAME}}": f"{project}-{region}"},
            "Region": region,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    boto3.client("ssm", region_name=region).put_parameter(
        Name=f"/{project}/addons/{region}/_input",
        Value=base64.b64encode(zlib.compress(raw.encode("utf-8"), 9)).decode("ascii"),
        Type="String",
        Overwrite=True,
    )


def _seed_state_machine(region: str) -> None:
    sfn = boto3.client("stepfunctions", region_name=region)
    sfn.create_state_machine(
        name="HelmInstallStateMachine7DB71CDC-abc",
        definition=json.dumps({"StartAt": "x", "States": {"x": {"Type": "Succeed"}}}),
        roleArn=f"arn:aws:iam::123456789012:role/helm-sfn-{region}",
    )


# ---------------------------------------------------------------------------
# _target_regions
# ---------------------------------------------------------------------------


class TestTargetRegions:
    def test_all_regions_returns_every_regional_region(self):
        from cli.commands import stacks_cmd

        with patch.object(
            stacks_cmd, "_load_cdk_json", return_value={"regional": ["us-east-1", "us-west-2"]}
        ):
            regions = stacks_cmd._target_regions(config=object(), region=None, all_regions=True)
        assert regions == ["us-east-1", "us-west-2"]

    def test_explicit_region_wins_when_not_all(self):
        from cli.commands import stacks_cmd

        with patch.object(
            stacks_cmd, "_load_cdk_json", return_value={"regional": ["us-east-1", "us-west-2"]}
        ):
            regions = stacks_cmd._target_regions(
                config=object(), region="eu-west-1", all_regions=False
            )
        assert regions == ["eu-west-1"]

    def test_single_defaults_to_first_regional(self):
        from cli.commands import stacks_cmd

        with patch.object(
            stacks_cmd, "_load_cdk_json", return_value={"regional": ["ap-south-1", "us-west-2"]}
        ):
            regions = stacks_cmd._target_regions(config=object(), region=None, all_regions=False)
        assert regions == ["ap-south-1"]

    def test_all_regions_empty_when_none_configured(self):
        from cli.commands import stacks_cmd

        with patch.object(stacks_cmd, "_load_cdk_json", return_value={}):
            regions = stacks_cmd._target_regions(config=object(), region=None, all_regions=True)
        assert regions == []


# ---------------------------------------------------------------------------
# addons status
# ---------------------------------------------------------------------------


@mock_aws
class TestAddonsStatus:
    def test_status_single_region(self, aws_creds_env):
        from cli.commands import stacks_cmd
        from cli.main import cli

        _seed_addon_status("us-east-1", "gco", "keda", "installed")
        _seed_addon_status("us-east-1", "gco", "volcano", "failed")

        runner = CliRunner()
        with (
            patch.object(stacks_cmd, "_project_name", return_value="gco"),
            patch.object(stacks_cmd, "_load_cdk_json", return_value={"regional": ["us-east-1"]}),
        ):
            result = runner.invoke(cli, ["stacks", "addons", "status", "-r", "us-east-1"])

        assert result.exit_code == 0, result.output
        assert "keda" in result.output
        assert "volcano" in result.output
        assert "installed" in result.output
        assert "failed" in result.output

    def test_status_all_regions_shows_every_region(self, aws_creds_env):
        from cli.commands import stacks_cmd
        from cli.main import cli

        _seed_addon_status("us-east-1", "gco", "keda", "installed")
        _seed_addon_status("us-west-2", "gco", "kueue", "installed")

        runner = CliRunner()
        with (
            patch.object(stacks_cmd, "_project_name", return_value="gco"),
            patch.object(
                stacks_cmd,
                "_load_cdk_json",
                return_value={"regional": ["us-east-1", "us-west-2"]},
            ),
        ):
            result = runner.invoke(cli, ["stacks", "addons", "status", "--all-regions"])

        assert result.exit_code == 0, result.output
        # Both regions' tables are rendered.
        assert "us-east-1" in result.output
        assert "us-west-2" in result.output
        assert "keda" in result.output
        assert "kueue" in result.output


# ---------------------------------------------------------------------------
# addons install
# ---------------------------------------------------------------------------


@mock_aws
class TestAddonsInstall:
    def test_install_single_region_starts_execution(self, aws_creds_env):
        from cli.commands import stacks_cmd
        from cli.main import cli

        _seed_addon_input("us-east-1", "gco")
        _seed_state_machine("us-east-1")

        runner = CliRunner()
        with (
            patch.object(stacks_cmd, "_project_name", return_value="gco"),
            patch.object(stacks_cmd, "_load_cdk_json", return_value={"regional": ["us-east-1"]}),
        ):
            result = runner.invoke(cli, ["stacks", "addons", "install", "-r", "us-east-1"])

        assert result.exit_code == 0, result.output
        assert "Started add-on install" in result.output

    def test_install_all_regions_starts_each(self, aws_creds_env):
        from cli.commands import stacks_cmd
        from cli.main import cli

        for region in ("us-east-1", "us-west-2"):
            _seed_addon_input(region, "gco")
            _seed_state_machine(region)

        runner = CliRunner()
        with (
            patch.object(stacks_cmd, "_project_name", return_value="gco"),
            patch.object(
                stacks_cmd,
                "_load_cdk_json",
                return_value={"regional": ["us-east-1", "us-west-2"]},
            ),
        ):
            result = runner.invoke(cli, ["stacks", "addons", "install", "--all-regions"])

        assert result.exit_code == 0, result.output
        assert result.output.count("Started add-on install") == 2
        assert "us-east-1" in result.output
        assert "us-west-2" in result.output

    def test_install_missing_input_exits_nonzero(self, aws_creds_env):
        from cli.commands import stacks_cmd
        from cli.main import cli

        # No _input parameter seeded for this region.
        runner = CliRunner()
        with (
            patch.object(stacks_cmd, "_project_name", return_value="gco"),
            patch.object(stacks_cmd, "_load_cdk_json", return_value={"regional": ["us-east-1"]}),
        ):
            result = runner.invoke(cli, ["stacks", "addons", "install", "-r", "us-east-1"])

        assert result.exit_code == 1
        assert "Could not read" in result.output

    def test_install_all_regions_partial_failure_exits_nonzero(self, aws_creds_env):
        from cli.commands import stacks_cmd
        from cli.main import cli

        # us-east-1 is ready; us-west-2 has no persisted input -> partial failure.
        _seed_addon_input("us-east-1", "gco")
        _seed_state_machine("us-east-1")

        runner = CliRunner()
        with (
            patch.object(stacks_cmd, "_project_name", return_value="gco"),
            patch.object(
                stacks_cmd,
                "_load_cdk_json",
                return_value={"regional": ["us-east-1", "us-west-2"]},
            ),
        ):
            result = runner.invoke(cli, ["stacks", "addons", "install", "--all-regions"])

        assert result.exit_code == 1
        assert "Started add-on install" in result.output  # us-east-1 still ran
        assert "us-west-2" in result.output  # failure reported for the other
