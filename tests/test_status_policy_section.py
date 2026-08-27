"""The ``policy`` section of ``gco status`` and the findings it derives.

Cross-region policy divergence is invisible to every other section: each region
is individually healthy and internally consistent, and the only symptom is a
manifest that is admitted in one region and refused in another. This section
exists to make it visible, which means the findings have to explain *why* a
difference matters rather than just reporting a diff.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from cli.job_policy import FETCH_OK, FETCH_UNREACHABLE, RegionPolicy
from cli.status import (
    SECTION_ORDER,
    SECTION_POLICY,
    SEVERITY_WARN,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_SKIPPED,
    STATUS_UNAVAILABLE,
    Section,
    _gather_policy,
    derive_findings,
)
from gco.job_admission import JobValidationPolicy


def _document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "validation_enabled": True,
        "manifest_caps": {
            "max_cpu_millicores": 384_000,
            "max_memory_bytes": 4096 * 1024**3,
            "max_gpu_count": 16,
        },
        "allowed_namespaces": ["gco-jobs"],
        "allowed_kinds": ["Job"],
        "trusted_registries": ["docker.io"],
        "trusted_dockerhub_orgs": ["pytorch"],
        "require_accelerator_toleration": True,
        "yaml_max_depth": 50,
        "manifest_security_policy": {},
    }
    caps = overrides.pop("manifest_caps", None)
    if caps:
        document["manifest_caps"].update(caps)
    document.update(overrides)
    return document


def _entry(region: str, **overrides: Any) -> RegionPolicy:
    document = _document(**overrides)
    return RegionPolicy(
        region=region,
        status=FETCH_OK,
        policy=JobValidationPolicy.from_policy_document(document),
        document=document,
        cluster_enforcement=overrides.get("cluster_enforcement", {}),
    )


class _Config:
    project_name = "gco"
    default_region = "us-east-1"


def _gather(policies: list[RegionPolicy], regions: list[str]) -> Section:
    with (
        patch("cli.aws_client.get_aws_client"),
        patch("cli.job_policy.fetch_region_policies", return_value=policies),
    ):
        return _gather_policy(_Config(), True, regions)  # type: ignore[arg-type]


class TestSectionRegistration:
    def test_section_is_in_the_render_order(self) -> None:
        assert SECTION_POLICY in SECTION_ORDER

    def test_skipped_unless_requested(self) -> None:
        section = _gather_policy(_Config(), False, ["us-east-1"])  # type: ignore[arg-type]
        assert section.status == STATUS_SKIPPED
        assert "--with-policy" in (section.reason or "")

    def test_unavailable_when_regions_are_unresolved(self) -> None:
        section = _gather_policy(_Config(), True, [])  # type: ignore[arg-type]
        assert section.status == STATUS_UNAVAILABLE


class TestGathering:
    def test_agreeing_regions_report_ok_and_agree(self) -> None:
        section = _gather([_entry("us-east-1"), _entry("us-east-2")], ["us-east-1", "us-east-2"])
        assert section.status == STATUS_OK
        assert section.data["agree"] is True
        assert section.data["drift"] == []
        assert section.data["compared"] == ["us-east-1", "us-east-2"]

    def test_drift_is_recorded_with_per_region_values(self) -> None:
        section = _gather(
            [_entry("us-east-1"), _entry("us-east-2", manifest_caps={"max_gpu_count": 4})],
            ["us-east-1", "us-east-2"],
        )
        assert section.data["agree"] is False
        assert section.data["drift"] == [
            {"field": "max_gpu_count", "values": {"us-east-1": 16, "us-east-2": 4}}
        ]

    def test_one_unreadable_region_degrades_to_partial(self) -> None:
        policies = [
            _entry("us-east-1"),
            RegionPolicy(region="us-east-2", status=FETCH_UNREACHABLE, reason="not deployed"),
        ]
        section = _gather(policies, ["us-east-1", "us-east-2"])
        assert section.status == STATUS_PARTIAL
        assert section.data["unreadable"] == {"us-east-2": "not deployed"}
        assert section.errors == ["us-east-2: not deployed"]

    def test_all_unreadable_is_unavailable(self) -> None:
        policies = [
            RegionPolicy(region="us-east-1", status=FETCH_UNREACHABLE, reason="a"),
            RegionPolicy(region="us-east-2", status=FETCH_UNREACHABLE, reason="b"),
        ]
        section = _gather(policies, ["us-east-1", "us-east-2"])
        assert section.status == STATUS_UNAVAILABLE
        assert section.data["compared"] == []

    def test_ecr_augmentation_only_listed_when_present(self) -> None:
        plain = _gather([_entry("us-east-1")], ["us-east-1"])
        assert plain.data["ecr_augmentation"] == {}

        augmented = _gather(
            [
                _entry(
                    "us-east-1",
                    trusted_registries=[
                        "docker.io",
                        "760425982254.dkr.ecr.us-east-1.amazonaws.com",
                    ],
                )
            ],
            ["us-east-1"],
        )
        assert augmented.data["ecr_augmentation"] == {
            "us-east-1": ["760425982254.dkr.ecr.us-east-1.amazonaws.com"]
        }


class TestFindings:
    def test_drift_becomes_a_warn_finding_that_explains_the_cause(self) -> None:
        section = _gather(
            [_entry("us-east-1"), _entry("us-east-2", manifest_caps={"max_gpu_count": 4})],
            ["us-east-1", "us-east-2"],
        )
        findings = [f for f in derive_findings({SECTION_POLICY: section}) if f.section == "policy"]

        assert len(findings) == 1
        assert findings[0].severity == SEVERITY_WARN
        assert "max_gpu_count" in findings[0].message
        # The value of the finding is the explanation, not the diff.
        assert "no per-region policy overrides" in findings[0].message
        assert "different deployment of cdk.json" in findings[0].message

    def test_agreement_produces_no_findings(self) -> None:
        section = _gather([_entry("us-east-1"), _entry("us-east-2")], ["us-east-1", "us-east-2"])
        assert [
            f for f in derive_findings({SECTION_POLICY: section}) if f.section == "policy"
        ] == []

    def test_drift_is_warn_not_error(self) -> None:
        """Drift must not fail --fail-on-findings; it may be a deploy in flight."""
        section = _gather(
            [_entry("us-east-1"), _entry("us-east-2", manifest_caps={"max_gpu_count": 4})],
            ["us-east-1", "us-east-2"],
        )
        findings = derive_findings({SECTION_POLICY: section})
        assert all(f.severity == SEVERITY_WARN for f in findings if f.section == "policy")

    def test_enforcement_gap_becomes_a_finding_pointing_at_rbac(self) -> None:
        entry = RegionPolicy(
            region="us-east-1",
            status=FETCH_OK,
            policy=JobValidationPolicy.from_policy_document(_document()),
            cluster_enforcement={"gco-jobs": {"status": "unavailable", "reason": "403 Forbidden"}},
        )
        section = _gather([entry], ["us-east-1"])
        findings = [f for f in derive_findings({SECTION_POLICY: section}) if f.section == "policy"]

        assert len(findings) == 1
        assert "gco-jobs" in findings[0].message
        assert "front-door caps" in findings[0].message
        assert "Role" in findings[0].message

    def test_skipped_section_produces_no_findings(self) -> None:
        section = _gather_policy(_Config(), False, ["us-east-1"])  # type: ignore[arg-type]
        assert derive_findings({SECTION_POLICY: section}) == []

    def test_findings_survive_an_absent_section(self) -> None:
        assert derive_findings({}) == []
