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


def test_status_opt_in_flags_are_forwarded_to_the_gatherer(runner: CliRunner) -> None:
    _, gather_default = _invoke(runner, [], config=_config(output_format="json"))
    _, gather_opted = _invoke(
        runner,
        ["--with-costs", "--with-nodepools"],
        config=_config(output_format="json"),
    )

    assert gather_default.call_args.kwargs["with_costs"] is False
    assert gather_default.call_args.kwargs["with_nodepools"] is False
    assert gather_opted.call_args.kwargs["with_costs"] is True
    assert gather_opted.call_args.kwargs["with_nodepools"] is True


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
    assert "ok" in result.output.lower()


# ---------------------------------------------------------------------------
# Table rendering
# ---------------------------------------------------------------------------


def _rich_document() -> FleetStatus:
    """A document exercising every section status the renderer must show."""
    return FleetStatus(
        generated_at="2026-08-13T18:22:04+00:00",
        project_name="test-gco",
        overall=OVERALL_DEGRADED,
        degraded=["stacks", "capacity", "nodepools"],
        findings=[
            Finding(
                severity="error",
                section="stacks",
                message="test-gco-us-west-2 is UPDATE_ROLLBACK_COMPLETE in us-west-2",
            ),
            Finding(
                severity="warn",
                section="queue",
                message="us-west-2 dead-letter queue holds 2 messages",
            ),
        ],
        sections={
            "regions": Section(
                name="regions",
                status=STATUS_OK,
                data={
                    "global": "us-east-2",
                    "api_gateway": "us-east-2",
                    "monitoring": "us-east-2",
                    "workload": ["us-east-1", "us-west-2"],
                    "source": "cdk.json",
                },
            ),
            "stacks": Section(
                name="stacks",
                status=STATUS_OK,
                data={
                    "expected": [
                        {
                            "name": "test-gco-us-west-2",
                            "region": "us-west-2",
                            "status": "UPDATE_ROLLBACK_COMPLETE",
                            "health": "unhealthy",
                            "updated_time": None,
                        }
                    ],
                    "optional": [
                        {
                            "name": "test-gco-regional-api-us-east-1",
                            "region": "us-east-1",
                            "status": "CREATE_COMPLETE",
                            "health": "healthy",
                            "updated_time": None,
                        }
                    ],
                },
            ),
            "queue": Section(
                name="queue",
                status=STATUS_OK,
                data={
                    "by_region": {
                        "us-east-1": {"available": 3, "in_flight": 1, "delayed": 0, "dlq": 0},
                        "us-west-2": {"available": 0, "in_flight": 0, "delayed": 0, "dlq": 2},
                    },
                    "totals": {"available": 3, "in_flight": 1, "delayed": 0, "dlq": 2},
                },
            ),
            "jobs": Section(
                name="jobs",
                status=STATUS_OK,
                data={
                    "totals": {"total": 41, "queued": 3, "running": 1},
                    "by_region": {"us-east-1": {"queued": 3, "running": 1}},
                    "complete": False,
                    "records_evaluated": 41,
                },
            ),
            "capacity": Section(
                name="capacity",
                status="partial",
                reason="1 of 2 regions reported incomplete telemetry",
                errors=["us-west-2: GPU telemetry returned no datapoints"],
                data={
                    "by_region": {
                        "us-east-1": {
                            "queue_depth": 3,
                            "running_jobs": 1,
                            "gpu_utilization": 62.5,
                            "cpu_utilization": 31.0,
                            "telemetry_status": "complete",
                            "unavailable_signals": [],
                        },
                        "us-west-2": {
                            "queue_depth": 0,
                            "running_jobs": 0,
                            "gpu_utilization": 0.0,
                            "cpu_utilization": 12.0,
                            "telemetry_status": "partial",
                            "unavailable_signals": ["gpu"],
                        },
                    }
                },
            ),
            "inference": Section(
                name="inference", status="empty", data={"totals": {}, "count": 0, "endpoints": []}
            ),
            "costs": Section(
                name="costs",
                status=STATUS_SKIPPED,
                reason="not requested; pass --with-costs (Cost Explorer bills per request)",
            ),
            "nodepools": Section(
                name="nodepools",
                status=STATUS_UNAVAILABLE,
                reason="cluster endpoint is private; open a tunnel with `gco cluster tunnel`",
            ),
        },
    )


def test_status_table_renders_every_section_status_with_reasons(runner: CliRunner) -> None:
    result, _ = _invoke(
        runner, [], config=_config(output_format="table"), document=_rich_document()
    )

    assert result.exit_code == 0, result.output
    output = result.output
    assert "Fleet status: DEGRADED" in output
    assert "degraded sections: stacks, capacity, nodepools" in output
    # Sections in every status render, with reasons for the degraded ones.
    assert "regions [ok]" in output
    assert "capacity [partial]" in output
    assert "1 of 2 regions reported incomplete telemetry" in output
    assert "inference [empty]" in output
    assert "costs [skipped]" in output
    assert "not requested; pass --with-costs" in output
    assert "nodepools [unavailable]" in output
    assert "gco cluster tunnel" in output


def test_status_table_renders_findings_above_section_detail(runner: CliRunner) -> None:
    result, _ = _invoke(
        runner, [], config=_config(output_format="table"), document=_rich_document()
    )

    output = result.output
    finding_pos = output.index("UPDATE_ROLLBACK_COMPLETE in us-west-2")
    first_section_pos = output.index("regions [ok]")
    assert finding_pos < first_section_pos
    assert "[error] stacks:" in output
    assert "[warn] queue:" in output


def test_status_table_states_the_absence_of_findings_explicitly(runner: CliRunner) -> None:
    result, _ = _invoke(runner, [], config=_config(output_format="table"))

    assert "Findings: none" in result.output


def test_status_table_names_drill_down_commands(runner: CliRunner) -> None:
    result, _ = _invoke(
        runner, [], config=_config(output_format="table"), document=_rich_document()
    )

    output = result.output
    assert "gco queue stats" in output
    assert "gco capacity status" in output
    assert "gco inference list" in output


def test_status_table_and_json_render_the_same_document(runner: CliRunner) -> None:
    document = _rich_document()
    table_result, _ = _invoke(runner, [], config=_config(output_format="table"), document=document)
    json_result, _ = _invoke(runner, [], config=_config(output_format="json"), document=document)

    payload = json.loads(json_result.stdout)
    # The same values from the one document appear in both renderings.
    assert payload["overall"] == "degraded"
    assert "Fleet status: DEGRADED" in table_result.output
    assert payload["sections"]["queue"]["data"]["by_region"]["us-west-2"]["dlq"] == 2
    assert "dlq 2" in table_result.output
    assert payload["findings"][0]["message"] in table_result.output
    assert payload["sections"]["capacity"]["reason"] in table_result.output
    # The truncated-scan marker renders in both.
    assert payload["sections"]["jobs"]["data"]["complete"] is False
    assert "TRUNCATED after 41 records" in table_result.output
