"""Tests for the ``gco status`` command surface.

Drives the command through :class:`click.testing.CliRunner` with a directly
constructed :class:`GCOConfig`. The gatherer's file boundary is patched so
no test depends on the checkout's real ``cdk.json``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from cli.commands.status_cmd import status
from cli.config import GCOConfig

_CDK_REGIONS = {
    "global": "us-east-2",
    "api_gateway": "us-east-2",
    "monitoring": "us-east-2",
    "regional": ["us-east-1", "us-west-2"],
}

_TOP_LEVEL_KEYS = {"generated_at", "project_name", "overall", "degraded", "findings", "sections"}


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _config(*, output_format: str = "table") -> GCOConfig:
    return GCOConfig(
        project_name="test-gco",
        default_region="us-east-1",
        output_format=output_format,
    )


def _invoke(
    runner: CliRunner,
    args: list[str],
    *,
    config: GCOConfig,
    cdk_regions: dict[str, Any] | None = None,
) -> Any:
    regions = dict(_CDK_REGIONS) if cdk_regions is None else cdk_regions
    with patch("cli.status._load_cdk_json", return_value=regions):
        return runner.invoke(status, args, obj=config)


def test_status_json_emits_one_document_and_nothing_else(runner: CliRunner) -> None:
    result = _invoke(runner, [], config=_config(output_format="json"))

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert set(document) == _TOP_LEVEL_KEYS
    assert document["project_name"] == "test-gco"
    assert set(document["sections"]) == {
        "regions",
        "stacks",
        "queue",
        "jobs",
        "capacity",
        "inference",
        "costs",
        "nodepools",
    }
    for glyph in ("ℹ", "✓", "⚠", "✗"):
        assert glyph not in result.stdout
    assert "REGION" not in result.stdout


def test_status_json_region_flag_narrows_the_workload_list(runner: CliRunner) -> None:
    result = _invoke(runner, ["-r", "eu-west-1"], config=_config(output_format="json"))

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["sections"]["regions"]["data"]["workload"] == ["eu-west-1"]


def test_status_yaml_emits_one_parseable_document(runner: CliRunner) -> None:
    result = _invoke(runner, [], config=_config(output_format="yaml"))

    assert result.exit_code == 0, result.output
    document = yaml.safe_load(result.stdout)
    assert set(document) == _TOP_LEVEL_KEYS


def test_status_exits_zero_when_the_document_is_degraded(runner: CliRunner) -> None:
    result = _invoke(runner, [], config=_config(output_format="json"), cdk_regions={})

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["overall"] == "degraded"
    assert document["sections"]["regions"]["status"] == "unavailable"


def test_status_table_mode_exits_zero(runner: CliRunner) -> None:
    result = _invoke(runner, [], config=_config(output_format="table"))

    assert result.exit_code == 0, result.output
    assert "ok" in result.output
