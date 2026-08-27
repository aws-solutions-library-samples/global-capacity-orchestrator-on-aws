"""Multi-region admissibility, cross-region drift, and the advisory pre-checks.

Two behaviors here are easy to get wrong in ways that make the feature worse
than not having it, so they are pinned deliberately:

``unknown`` is not ``reject``. A region whose policy could not be read has not
refused anything. Collapsing the two would report a network failure as a policy
violation, and with ``--fail-on-reject`` that turns an unreachable region into a
failed build.

``trusted_registries`` drift is measured with ECR hostnames stripped. CDK
appends the project's own registry hostnames at synth time and those encode a
region, so a raw comparison reports drift on every multi-region deployment --
which would train people to ignore the check.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from cli.job_policy import (
    FETCH_ERROR,
    FETCH_OK,
    FETCH_UNREACHABLE,
    VERDICT_ADMIT,
    VERDICT_REJECT,
    VERDICT_UNKNOWN,
    RegionPolicy,
    detect_policy_drift,
    ecr_augmentation,
    evaluate_manifest,
    fetch_region_policies,
    fetch_region_policy,
    manifest_label,
    region_verdicts,
    registry_drift,
)
from gco.job_admission import JobValidationPolicy


def _policy_document(**overrides: Any) -> dict[str, Any]:
    """A policy document shaped like GET /api/v1/policy's ``policy`` object."""
    document: dict[str, Any] = {
        "validation_enabled": True,
        "manifest_caps": {
            "max_cpu_millicores": 384_000,
            "max_memory_bytes": 4096 * 1024**3,
            "max_gpu_count": 16,
        },
        "allowed_namespaces": ["gco-jobs"],
        "allowed_kinds": ["Job", "Pod", "TrainJob"],
        "trusted_registries": ["docker.io"],
        "trusted_dockerhub_orgs": ["pytorch"],
        "require_accelerator_toleration": True,
        "yaml_max_depth": 50,
        "manifest_security_policy": {
            "block_privileged": True,
            "block_privilege_escalation": True,
            "block_host_network": True,
            "block_host_pid": True,
            "block_host_ipc": True,
            "block_host_path": True,
            "block_added_capabilities": True,
            "block_run_as_root": False,
        },
    }
    caps = overrides.pop("manifest_caps", None)
    if caps:
        document["manifest_caps"].update(caps)
    document.update(overrides)
    return document


def _region(region: str, **overrides: Any) -> RegionPolicy:
    document = _policy_document(**overrides)
    return RegionPolicy(
        region=region,
        status=FETCH_OK,
        policy=JobValidationPolicy.from_policy_document(document),
        document=document,
    )


def _job(*, gpus: int = 0, image: str = "docker.io/pytorch/pytorch:2", namespace: str = "gco-jobs"):
    pod: dict[str, Any] = {"containers": [{"name": "c", "image": image, "resources": {}}]}
    if gpus:
        pod["containers"][0]["resources"] = {"limits": {"nvidia.com/gpu": str(gpus)}}
        pod["tolerations"] = [{"key": "nvidia.com/gpu", "operator": "Exists"}]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "trainer", "namespace": namespace},
        "spec": {"template": {"spec": pod}},
    }


class TestMultiRegionAdmissibility:
    def test_same_manifest_admitted_in_one_region_and_rejected_in_another(self) -> None:
        """The case that motivates the feature."""
        policies = [
            _region("us-east-1"),
            _region("us-east-2", manifest_caps={"max_gpu_count": 4}),
        ]
        verdicts = region_verdicts([_job(gpus=8)], policies)

        by_region = {v.region: v for v in verdicts}
        assert by_region["us-east-1"].verdict == VERDICT_ADMIT
        assert by_region["us-east-2"].verdict == VERDICT_REJECT
        assert "GPU 8 exceeds max 4" in by_region["us-east-2"].issues[0].message

    def test_unreadable_region_is_unknown_not_reject(self) -> None:
        """A network failure is not a policy violation."""
        policies = [
            _region("us-east-1"),
            RegionPolicy(
                region="eu-west-1", status=FETCH_UNREACHABLE, reason="RuntimeError: not deployed"
            ),
        ]
        verdicts = region_verdicts([_job()], policies)
        by_region = {v.region: v for v in verdicts}

        assert by_region["eu-west-1"].verdict == VERDICT_UNKNOWN
        assert by_region["eu-west-1"].verdict != VERDICT_REJECT
        assert by_region["eu-west-1"].issues == []
        assert "not deployed" in (by_region["eu-west-1"].reason or "")

    def test_every_failing_check_is_reported_not_just_the_first(self) -> None:
        """The server short-circuits; a caller fixing a manifest wants the list."""
        manifest = _job(gpus=999, image="evil.example.com/x:1", namespace="nope")
        issues = evaluate_manifest(manifest, _region("us-east-1").policy)  # type: ignore[arg-type]

        checks = {issue.check for issue in issues}
        assert {"namespace", "images", "resource_caps"} <= checks
        assert len(issues) >= 3

    def test_issues_name_the_manifest_they_came_from(self) -> None:
        policies = [_region("us-east-1")]
        verdicts = region_verdicts([_job(namespace="nope")], policies)
        assert verdicts[0].issues[0].manifest == "Job/trainer"

    def test_validation_disabled_admits_everything(self) -> None:
        policy = _region("us-east-1", validation_enabled=False).policy
        assert evaluate_manifest(_job(namespace="nope", image="evil.io/x:1"), policy) == []  # type: ignore[arg-type]

    def test_enforcement_gaps_are_surfaced_on_an_admit(self) -> None:
        """An admit verdict must not imply more confidence than it has.

        When the live quota is unreadable only the front-door caps were checked,
        so the manifest can still be rejected at pod creation.
        """
        entry = RegionPolicy(
            region="us-east-1",
            status=FETCH_OK,
            policy=JobValidationPolicy.from_policy_document(_policy_document()),
            cluster_enforcement={"gco-jobs": {"status": "unavailable", "reason": "403 Forbidden"}},
        )
        verdict = region_verdicts([_job()], [entry])[0]
        assert verdict.verdict == VERDICT_ADMIT
        assert verdict.enforcement_gaps == ["gco-jobs"]


class TestCrossRegionDrift:
    def test_identical_regions_report_no_drift(self) -> None:
        assert detect_policy_drift([_region("us-east-1"), _region("us-east-2")]) == []

    def test_differing_cap_is_drift(self) -> None:
        drift = detect_policy_drift(
            [_region("us-east-1"), _region("us-east-2", manifest_caps={"max_gpu_count": 8})]
        )
        assert [item.field for item in drift] == ["max_gpu_count"]
        assert drift[0].values == {"us-east-1": 16, "us-east-2": 8}

    def test_security_toggle_drift_is_detected(self) -> None:
        other = _policy_document()["manifest_security_policy"] | {"block_run_as_root": True}
        drift = detect_policy_drift(
            [_region("us-east-1"), _region("us-east-2", manifest_security_policy=other)]
        )
        assert [item.field for item in drift] == ["security"]

    def test_a_single_region_cannot_drift(self) -> None:
        assert detect_policy_drift([_region("us-east-1")]) == []

    def test_unreadable_regions_are_excluded_from_comparison(self) -> None:
        policies = [
            _region("us-east-1"),
            RegionPolicy(region="us-east-2", status=FETCH_ERROR, reason="boom"),
        ]
        assert detect_policy_drift(policies) == []

    def test_registries_are_not_in_the_generic_field_list(self) -> None:
        """Guards the split: CDK varies registries legitimately."""
        from cli.job_policy import _DRIFT_FIELDS

        assert "trusted_registries" not in _DRIFT_FIELDS


class TestRegistryDrift:
    def test_ecr_augmentation_alone_is_not_drift(self) -> None:
        """The false positive that would make the check useless."""
        east1 = _region(
            "us-east-1",
            trusted_registries=["docker.io", "111122223333.dkr.ecr.us-east-1.amazonaws.com"],
        )
        east2 = _region(
            "us-east-2",
            trusted_registries=["docker.io", "111122223333.dkr.ecr.us-east-2.amazonaws.com"],
        )
        assert registry_drift([east1, east2]) is None

    def test_a_real_registry_difference_is_drift(self) -> None:
        east1 = _region("us-east-1", trusted_registries=["docker.io", "quay.io"])
        east2 = _region("us-east-2", trusted_registries=["docker.io"])
        drift = registry_drift([east1, east2])

        assert drift is not None
        assert drift.field == "trusted_registries"
        assert drift.values == {"us-east-1": ["docker.io", "quay.io"], "us-east-2": ["docker.io"]}

    def test_real_drift_is_still_found_alongside_ecr_entries(self) -> None:
        east1 = _region(
            "us-east-1",
            trusted_registries=[
                "docker.io",
                "quay.io",
                "111122223333.dkr.ecr.us-east-1.amazonaws.com",
            ],
        )
        east2 = _region(
            "us-east-2",
            trusted_registries=["docker.io", "111122223333.dkr.ecr.us-east-2.amazonaws.com"],
        )
        drift = registry_drift([east1, east2])
        assert drift is not None
        assert drift.values["us-east-1"] == ["docker.io", "quay.io"]

    def test_augmentation_is_reported_separately(self) -> None:
        """The concrete evidence that cdk.json is not authoritative."""
        entry = _region(
            "us-east-1",
            trusted_registries=["docker.io", "760425982254.dkr.ecr.us-east-1.amazonaws.com"],
        )
        assert ecr_augmentation([entry]) == {
            "us-east-1": ["760425982254.dkr.ecr.us-east-1.amazonaws.com"]
        }

    @pytest.mark.parametrize(
        "host",
        [
            "111122223333.dkr.ecr.us-east-1.amazonaws.com",
            "111122223333.dkr.ecr.cn-north-1.amazonaws.com.cn",
        ],
    )
    def test_ecr_hostname_shapes_recognized(self, host: str) -> None:
        assert ecr_augmentation([_region("r", trusted_registries=[host])]) == {"r": [host]}

    @pytest.mark.parametrize(
        "host", ["docker.io", "public.ecr.aws", "ghcr.io/huggingface", "quay.io"]
    )
    def test_non_ecr_hosts_are_not_stripped(self, host: str) -> None:
        assert ecr_augmentation([_region("r", trusted_registries=[host])]) == {"r": []}

    @pytest.mark.parametrize(
        "host",
        [
            "1.dkr.ecr.us-east-1.amazonaws.com",
            "1111222233334444.dkr.ecr.us-east-1.amazonaws.com",
            "evil.dkr.ecr.us-east-1.amazonaws.com",
            "111122223333.dkr.ecr.us-east-1.amazonaws.com.evil.io",
        ],
    )
    def test_near_miss_hostnames_are_not_treated_as_ecr(self, host: str) -> None:
        """An account ID is exactly 12 digits, and the host must end there.

        Stripping is a trust decision in miniature: anything stripped is excused
        from drift comparison, so a loose pattern would let a lookalike registry
        hide a real difference between regions.
        """
        assert ecr_augmentation([_region("r", trusted_registries=[host])]) == {"r": []}


class TestFetching:
    def test_missing_bridge_is_unreachable_not_error(self) -> None:
        client = MagicMock()
        client.get_job_validation_policy.side_effect = RuntimeError(
            "Regional API endpoint is not deployed in eu-west-1; ..."
        )
        result = fetch_region_policy(client, "eu-west-1")
        assert result.status == FETCH_UNREACHABLE
        assert result.ok is False

    def test_other_failures_are_errors(self) -> None:
        client = MagicMock()
        client.get_job_validation_policy.side_effect = ValueError("bad json")
        assert fetch_region_policy(client, "us-east-1").status == FETCH_ERROR

    def test_response_without_a_policy_object_is_an_error(self) -> None:
        client = MagicMock()
        client.get_job_validation_policy.return_value = {"region": "us-east-1"}
        result = fetch_region_policy(client, "us-east-1")
        assert result.status == FETCH_ERROR
        assert "no policy object" in (result.reason or "")

    def test_successful_read_builds_a_policy(self) -> None:
        client = MagicMock()
        client.get_job_validation_policy.return_value = {
            "policy": _policy_document(),
            "cluster_enforcement": {"gco-jobs": {"status": "ok"}},
        }
        result = fetch_region_policy(client, "us-east-1")
        assert result.ok
        assert result.policy is not None
        assert result.policy.max_gpu_count == 16
        assert result.enforcement_gaps == []

    def test_fanout_preserves_requested_order_despite_completion_order(self) -> None:
        """Output order must be deterministic for a diffable report."""
        client = MagicMock()
        client.get_job_validation_policy.return_value = {"policy": _policy_document()}
        regions = ["us-west-2", "us-east-1", "eu-west-1"]
        assert [entry.region for entry in fetch_region_policies(client, regions)] == regions

    def test_one_failing_region_does_not_sink_the_others(self) -> None:
        client = MagicMock()

        def side_effect(region: str) -> dict[str, Any]:
            if region == "us-east-2":
                raise RuntimeError("boom")
            return {"policy": _policy_document()}

        client.get_job_validation_policy.side_effect = lambda region: side_effect(region)
        results = fetch_region_policies(client, ["us-east-1", "us-east-2"])
        assert [entry.ok for entry in results] == [True, False]

    def test_empty_region_list(self) -> None:
        assert fetch_region_policies(MagicMock(), []) == []


class TestManifestLabel:
    def test_uses_kind_and_name(self) -> None:
        assert manifest_label({"kind": "Job", "metadata": {"name": "x"}}) == "Job/x"

    def test_tolerates_missing_pieces(self) -> None:
        assert manifest_label({}) == "?/<unnamed>"


class TestAdmissionLoggingIsQuiet:
    def test_evaluation_does_not_emit_warnings(self, caplog: Any) -> None:
        """The service logs rejections as an audit trail; the CLI must not.

        The caller is about to be shown the same information as formatted
        findings, so the log line duplicates it and interleaves with the report.
        """
        from cli.job_policy import evaluate_manifests

        with caplog.at_level("WARNING", logger="gco.job_admission"):
            issues = evaluate_manifests([_job(gpus=999)], _region("r").policy)  # type: ignore[arg-type]

        assert issues, "expected the over-cap job to produce findings"
        assert [r for r in caplog.records if r.name == "gco.job_admission"] == []

    def test_logger_level_is_restored(self) -> None:
        import logging

        from cli.job_policy import evaluate_manifests

        admission = logging.getLogger("gco.job_admission")
        before = admission.level
        evaluate_manifests([_job()], _region("r").policy)  # type: ignore[arg-type]
        assert admission.level == before
