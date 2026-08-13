"""Tests for fleet status document assembly (``cli/status.py``).

Covers region topology resolution, the section boundary that converts an
escaping exception into a degraded section, and the overall verdict. All
AWS boundaries are mocked; no network or credentials are used.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from cli.config import GCOConfig
from cli.status import (
    OVERALL_DEGRADED,
    OVERALL_OK,
    REGION_SOURCE_CDK_JSON,
    REGION_SOURCE_FLAG,
    SECTION_CAPACITY,
    SECTION_ORDER,
    SECTION_QUEUE,
    SECTION_REGIONS,
    SECTION_STACKS,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_SKIPPED,
    STATUS_UNAVAILABLE,
    gather_fleet_status,
    resolve_regions,
)

_CDK_REGIONS = {
    "global": "us-east-2",
    "api_gateway": "us-east-2",
    "monitoring": "us-east-2",
    "regional": ["us-east-1", "us-west-2"],
}


def _config() -> GCOConfig:
    return GCOConfig(project_name="test-gco", default_region="us-east-1")


# ---------------------------------------------------------------------------
# Region resolution
# ---------------------------------------------------------------------------


def test_resolve_regions_reads_the_configured_topology() -> None:
    with patch("cli.status._load_cdk_json", return_value=dict(_CDK_REGIONS)):
        section = resolve_regions(_config())

    assert section.status == STATUS_OK
    assert section.data == {
        "global": "us-east-2",
        "api_gateway": "us-east-2",
        "monitoring": "us-east-2",
        "workload": ["us-east-1", "us-west-2"],
        "source": REGION_SOURCE_CDK_JSON,
    }


def test_resolve_regions_narrows_to_the_explicit_flag_region() -> None:
    with patch("cli.status._load_cdk_json", return_value=dict(_CDK_REGIONS)):
        section = resolve_regions(_config(), region="us-west-2")

    assert section.status == STATUS_OK
    assert section.data["workload"] == ["us-west-2"]
    assert section.data["source"] == REGION_SOURCE_FLAG


def test_resolve_regions_flag_works_without_cdk_json() -> None:
    with patch("cli.status._load_cdk_json", return_value={}):
        section = resolve_regions(_config(), region="eu-west-1")

    assert section.status == STATUS_OK
    assert section.data["workload"] == ["eu-west-1"]
    assert section.data["source"] == REGION_SOURCE_FLAG


def test_resolve_regions_missing_cdk_json_names_both_remedies() -> None:
    with patch("cli.status._load_cdk_json", return_value={}):
        section = resolve_regions(_config())

    assert section.status == STATUS_UNAVAILABLE
    assert section.reason is not None
    assert "cdk.json" in section.reason
    assert "--region" in section.reason
    assert section.data == {}


def test_resolve_regions_empty_regional_list_is_unavailable() -> None:
    with patch("cli.status._load_cdk_json", return_value={"regional": []}):
        section = resolve_regions(_config())

    assert section.status == STATUS_UNAVAILABLE


def test_resolve_regions_ignores_non_string_regional_entries() -> None:
    with patch("cli.status._load_cdk_json", return_value={"regional": [None, 3]}):
        section = resolve_regions(_config())

    assert section.status == STATUS_UNAVAILABLE


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def test_gather_assembles_every_section_in_order() -> None:
    with patch("cli.status._load_cdk_json", return_value=dict(_CDK_REGIONS)):
        doc = gather_fleet_status(_config())

    assert tuple(doc.sections) == SECTION_ORDER
    assert doc.project_name == "test-gco"
    assert doc.sections[SECTION_REGIONS].status == STATUS_OK
    # The timestamp is ISO-8601 with an explicit UTC offset.
    parsed = datetime.fromisoformat(doc.generated_at)
    assert parsed.utcoffset() is not None


def test_gather_never_calls_the_all_region_stack_scan() -> None:
    scan = MagicMock(name="discover_regional_stacks")
    with (
        patch("cli.status._load_cdk_json", return_value={}),
        patch("cli.aws_client.GCOAWSClient.discover_regional_stacks", scan),
    ):
        gather_fleet_status(_config())

    scan.assert_not_called()


def test_gather_short_circuits_per_region_sections_when_regions_unresolved() -> None:
    with patch("cli.status._load_cdk_json", return_value={}):
        doc = gather_fleet_status(_config())

    for name in (SECTION_STACKS, SECTION_QUEUE, SECTION_CAPACITY):
        section = doc.sections[name]
        assert section.status == STATUS_UNAVAILABLE
        assert section.reason is not None
        assert "regions section" in section.reason


def test_gather_overall_is_ok_when_only_skipped_sections_remain() -> None:
    with patch("cli.status._load_cdk_json", return_value=dict(_CDK_REGIONS)):
        doc = gather_fleet_status(_config())

    assert doc.overall == OVERALL_OK
    assert doc.degraded == []


def test_gather_overall_degrades_when_regions_are_unresolved() -> None:
    with patch("cli.status._load_cdk_json", return_value={}):
        doc = gather_fleet_status(_config())

    assert doc.overall == OVERALL_DEGRADED
    assert SECTION_REGIONS in doc.degraded
    # Degraded names follow section order.
    assert doc.degraded == [
        name for name in SECTION_ORDER if doc.sections[name].status == STATUS_UNAVAILABLE
    ]


def test_gather_converts_an_escaping_exception_into_an_error_section() -> None:
    with patch("cli.status.resolve_regions", side_effect=RuntimeError("boom")):
        doc = gather_fleet_status(_config())

    section = doc.sections[SECTION_REGIONS]
    assert section.status == STATUS_ERROR
    assert section.reason is not None
    assert any("boom" in error for error in section.errors)
    # The failure degraded its own section, never the document assembly.
    assert tuple(doc.sections) == SECTION_ORDER
    assert doc.overall == OVERALL_DEGRADED


def test_gather_marks_opt_in_sections_skipped_by_default() -> None:
    with patch("cli.status._load_cdk_json", return_value=dict(_CDK_REGIONS)):
        doc = gather_fleet_status(_config())

    costs = doc.sections["costs"]
    nodepools = doc.sections["nodepools"]
    assert costs.status == STATUS_SKIPPED
    assert costs.reason is not None
    assert "--with-costs" in costs.reason
    assert nodepools.status == STATUS_SKIPPED
    assert nodepools.reason is not None
    assert "--with-nodepools" in nodepools.reason
