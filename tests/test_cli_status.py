"""Tests for the ``gco status`` command surface.

Drives the command through :class:`click.testing.CliRunner` with a directly
constructed :class:`GCOConfig`. The gatherer is patched at the command's
import site and fed hand-built :class:`FleetStatus` documents, so these
tests cover rendering and exit behavior without any AWS boundary.
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
from cli.status import (
    OVERALL_DEGRADED,
    OVERALL_OK,
    SECTION_ORDER,
    STATUS_OK,
    STATUS_SKIPPED,
    STATUS_UNAVAILABLE,
    Finding,
    FleetStatus,
    Section,
)

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


def _document(
    *,
    overall: str = OVERALL_OK,
    degraded: list[str] | None = None,
    findings: list[Finding] | None = None,
    section_overrides: dict[str, Section] | None = None,
) -> FleetStatus:
    sections = {
        name: Section(name=name, status=STATUS_OK, data={"placeholder": True})
        for name in SECTION_ORDER
    }
    sections["costs"] = Section(
        name="costs", status=STATUS_SKIPPED, reason="not requested; pass --with-costs"
    )
    sections["nodepools"] = Section(
        name="nodepools", status=STATUS_SKIPPED, reason="not requested; pass --with-nodepools"
    )
    sections.update(section_overrides or {})
    return FleetStatus(
        generated_at="2026-08-13T18:22:04+00:00",
        project_name="test-gco",
        overall=overall,
        degraded=degraded or [],
        findings=findings or [],
        sections=sections,
    )


def _invoke(
    runner: CliRunner,
    args: list[str],
    *,
    config: GCOConfig,
    document: FleetStatus | None = None,
) -> tuple[Any, Any]:
    doc = document or _document()
    with patch("cli.commands.status_cmd.gather_fleet_status", return_value=doc) as gather:
        result = runner.invoke(status, args, obj=config)
    return result, gather


def test_status_json_emits_one_document_and_nothing_else(runner: CliRunner) -> None:
    result, _ = _invoke(runner, [], config=_config(output_format="json"))

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert set(document) == _TOP_LEVEL_KEYS
    assert document["project_name"] == "test-gco"
    assert set(document["sections"]) == set(SECTION_ORDER)
    for glyph in ("ℹ", "✓", "⚠", "✗"):
        assert glyph not in result.stdout
    assert "REGION" not in result.stdout


def test_status_json_serializes_findings_as_objects(runner: CliRunner) -> None:
    document = _document(
        overall=OVERALL_DEGRADED,
        degraded=["stacks"],
        findings=[Finding(severity="error", section="stacks", message="stack rolled back")],
    )
    result, _ = _invoke(runner, [], config=_config(output_format="json"), document=document)

    payload = json.loads(result.stdout)
    assert payload["findings"] == [
        {"severity": "error", "section": "stacks", "message": "stack rolled back"}
    ]


def test_status_yaml_emits_one_parseable_document(runner: CliRunner) -> None:
    result, _ = _invoke(runner, [], config=_config(output_format="yaml"))

    assert result.exit_code == 0, result.output
    document = yaml.safe_load(result.stdout)
    assert set(document) == _TOP_LEVEL_KEYS


def test_status_region_flag_is_forwarded_to_the_gatherer(runner: CliRunner) -> None:
    result, gather = _invoke(runner, ["-r", "eu-west-1"], config=_config(output_format="json"))

    assert result.exit_code == 0, result.output
    assert gather.call_args.kwargs["region"] == "eu-west-1"


def test_status_exits_zero_when_the_document_is_degraded(runner: CliRunner) -> None:
    document = _document(
        overall=OVERALL_DEGRADED,
        degraded=["regions"],
        section_overrides={
            "regions": Section(
                name="regions",
                status=STATUS_UNAVAILABLE,
                reason="deployment regions are not configured",
            )
        },
    )
    result, _ = _invoke(runner, [], config=_config(output_format="json"), document=document)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["overall"] == "degraded"
    assert payload["sections"]["regions"]["status"] == "unavailable"


def test_status_table_mode_exits_zero(runner: CliRunner) -> None:
    result, _ = _invoke(runner, [], config=_config(output_format="table"))

    assert result.exit_code == 0, result.output
    assert "ok" in result.output
