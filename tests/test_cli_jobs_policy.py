"""Behavioral coverage for the jobs CLI policy and safety boundaries.

All AWS, Kubernetes, and queue boundaries are mocked.  The tests exercise the
operator-visible contracts: advisory checks never block submission, offline
checks never call AWS, deployed policy rendering includes live enforcement,
and destructive/input-validation edges fail closed.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
from click.testing import CliRunner

from cli.commands.jobs_cmd import (
    _cdk_job_validation_policy,
    _policy_regions,
    _render_verdicts,
)
from cli.job_policy import (
    FETCH_OK,
    FETCH_UNREACHABLE,
    VERDICT_ADMIT,
    VERDICT_REJECT,
    VERDICT_UNKNOWN,
    AdmissionIssue,
    PolicyDrift,
    RegionPolicy,
    RegionVerdict,
)


@pytest.fixture
def manifest_path(tmp_path: Any) -> str:
    path = tmp_path / "job.yaml"
    path.write_text(
        "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: trainer\n",
        encoding="utf-8",
    )
    return str(path)


def _invoke(
    args: list[str],
    manager: MagicMock | None = None,
    formatter: MagicMock | None = None,
    *,
    input_text: str | None = None,
) -> tuple[Any, MagicMock, MagicMock]:
    """Invoke the root CLI with deterministic jobs dependencies."""
    from cli.main import cli

    manager = manager or MagicMock()
    formatter = formatter or MagicMock()
    with (
        patch("cli.commands.jobs_cmd.get_job_manager", return_value=manager),
        patch("cli.commands.jobs_cmd.get_output_formatter", return_value=formatter),
    ):
        result = CliRunner().invoke(cli, args, input=input_text)
    return result, manager, formatter


def _readable(region: str) -> RegionPolicy:
    return RegionPolicy(region=region, status=FETCH_OK, policy=MagicMock())


class TestSubmissionContracts:
    def test_submit_surfaces_mapping_warnings_and_waits_with_namespace_fallback(
        self, manifest_path: str
    ) -> None:
        manager = MagicMock()
        manager.submit_job.return_value = {
            "resources": [
                "job.batch/trainer created",
                {"kind": "ConfigMap", "message": "unrelated"},
                {"kind": "ConfigMap", "message": "renamed conflicting object"},
                {"kind": "Job", "name": "trainer-generated", "status": "created"},
                {"kind": "Pod", "message": "old job still running"},
            ]
        }
        manager.wait_for_job.return_value = SimpleNamespace(status="Succeeded")

        result, _, formatter = _invoke(
            [
                "jobs",
                "submit",
                manifest_path,
                "--namespace",
                "ml-jobs",
                "--region",
                "us-east-1",
                "--wait",
                "--timeout",
                "17",
            ],
            manager,
        )

        assert result.exit_code == 0, result.output
        assert formatter.print_warning.call_args_list == [
            call("renamed conflicting object"),
            call("old job still running"),
        ]
        manager.wait_for_job.assert_called_once_with(
            job_name="trainer-generated",
            namespace="ml-jobs",
            region="us-east-1",
            timeout_seconds=17,
        )

    @pytest.mark.parametrize(
        ("subcommand", "extra"),
        [
            ("submit", []),
            ("submit-direct", ["--region", "us-east-1"]),
        ],
    )
    def test_non_mapping_submission_result_never_attempts_wait(
        self, manifest_path: str, subcommand: str, extra: list[str]
    ) -> None:
        manager = MagicMock()
        method = manager.submit_job if subcommand == "submit" else manager.submit_job_direct
        method.return_value = "kubectl accepted the manifest"

        result, _, formatter = _invoke(
            ["jobs", subcommand, manifest_path, *extra, "--wait"], manager
        )

        assert result.exit_code == 0, result.output
        formatter.print.assert_called_once_with("kubectl accepted the manifest")
        manager.wait_for_job.assert_not_called()

    def test_direct_submission_surfaces_warnings(self, manifest_path: str) -> None:
        manager = MagicMock()
        manager.submit_job_direct.return_value = {
            "job_name": "trainer",
            "namespace": "gco-jobs",
            "warnings": ["Job renamed after a collision", 42],
        }

        result, _, formatter = _invoke(
            ["jobs", "submit-direct", manifest_path, "-r", "us-east-1"], manager
        )

        assert result.exit_code == 0, result.output
        assert formatter.print_warning.call_args_list == [
            call("Job renamed after a collision"),
            call("42"),
        ]

    def test_dry_run_never_waits(self, manifest_path: str) -> None:
        manager = MagicMock()
        manager.submit_job.return_value = {"name": "trainer"}

        result, _, formatter = _invoke(
            ["jobs", "submit", manifest_path, "--dry-run", "--wait"], manager
        )

        assert result.exit_code == 0, result.output
        manager.wait_for_job.assert_not_called()
        formatter.print_success.assert_called_once_with("Dry run successful - manifests are valid")

    @pytest.mark.parametrize("malformed_label", ["missing-equals", "=value"])
    @pytest.mark.parametrize(
        ("subcommand", "extra", "submit_method"),
        [
            ("submit", [], "submit_job"),
            ("submit-direct", ["--region", "us-east-1"], "submit_job_direct"),
            ("submit-sqs", ["--region", "us-east-1"], "submit_job_sqs"),
        ],
    )
    def test_malformed_labels_are_rejected_before_submission(
        self,
        manifest_path: str,
        subcommand: str,
        extra: list[str],
        submit_method: str,
        malformed_label: str,
    ) -> None:
        manager = MagicMock()

        result, _, _ = _invoke(
            ["jobs", subcommand, manifest_path, *extra, "--label", malformed_label],
            manager,
        )

        assert result.exit_code == 2
        assert "labels must use key=value" in result.output
        getattr(manager, submit_method).assert_not_called()

    @pytest.mark.parametrize("malformed_label", ["missing-equals", "=value"])
    def test_submit_queue_rejects_malformed_labels_before_api_call(
        self, manifest_path: str, malformed_label: str
    ) -> None:
        aws = MagicMock()
        with patch("cli.aws_client.get_aws_client", return_value=aws):
            result, _, _ = _invoke(
                [
                    "jobs",
                    "submit-queue",
                    manifest_path,
                    "--region",
                    "us-east-1",
                    "--label",
                    malformed_label,
                ]
            )

        assert result.exit_code == 2
        assert "labels must use key=value" in result.output
        aws.call_api.assert_not_called()

    def test_label_values_may_contain_equals(self, manifest_path: str) -> None:
        manager = MagicMock()
        manager.submit_job.return_value = {"name": "trainer"}

        result, _, _ = _invoke(
            ["jobs", "submit", manifest_path, "--label", "selector=a=b"], manager
        )

        assert result.exit_code == 0, result.output
        assert manager.submit_job.call_args.kwargs["labels"] == {"selector": "a=b"}

    @pytest.mark.parametrize("priority", ["-1", "101"])
    def test_submit_queue_rejects_priority_outside_documented_range(
        self, manifest_path: str, priority: str
    ) -> None:
        aws = MagicMock()
        with patch("cli.aws_client.get_aws_client", return_value=aws):
            result, _, _ = _invoke(
                [
                    "jobs",
                    "submit-queue",
                    manifest_path,
                    "--region",
                    "us-east-1",
                    "--priority",
                    priority,
                ]
            )

        assert result.exit_code == 2
        assert "0<=x<=100" in result.output
        aws.call_api.assert_not_called()

    def test_submit_queue_validates_complete_request_body(self, manifest_path: str) -> None:
        aws = MagicMock()
        aws.call_api.return_value = {"job": {"job_id": "job-1"}}
        with patch("cli.aws_client.get_aws_client", return_value=aws):
            result, _, _ = _invoke(
                [
                    "jobs",
                    "submit-queue",
                    manifest_path,
                    "--region",
                    "us-east-1",
                    "--namespace",
                    "ml",
                    "--priority",
                    "100",
                    "--label",
                    "selector=a=b",
                ]
            )

        assert result.exit_code == 0, result.output
        aws.call_api.assert_called_once_with(
            method="POST",
            path="/api/v1/queue/jobs",
            region="us-east-1",
            body={
                "manifest": {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {"name": "trainer"},
                },
                "target_region": "us-east-1",
                "namespace": "ml",
                "priority": 100,
                "labels": {"selector": "a=b"},
            },
        )

    def test_submit_sqs_auto_region_forwards_recommendation(self, manifest_path: str) -> None:
        manager = MagicMock()
        manager.submit_job_sqs.return_value = {"message_id": "m-1"}
        checker = MagicMock()
        checker.recommend_region_for_job.return_value = {
            "region": "eu-west-1",
            "reason": "lowest queue",
        }
        with patch("cli.capacity.get_capacity_checker", return_value=checker):
            result, _, formatter = _invoke(
                ["jobs", "submit-sqs", manifest_path, "--auto-region"], manager
            )

        assert result.exit_code == 0, result.output
        checker.recommend_region_for_job.assert_called_once_with()
        assert manager.submit_job_sqs.call_args.kwargs["region"] == "eu-west-1"
        formatter.print_info.assert_any_call("Selected region: eu-west-1 (lowest queue)")

    def test_submit_sqs_explicit_region_skips_auto_selection(self, manifest_path: str) -> None:
        manager = MagicMock()
        manager.submit_job_sqs.return_value = {"message_id": "m-1"}
        checker = MagicMock()
        with patch("cli.capacity.get_capacity_checker", return_value=checker):
            result, _, _ = _invoke(
                [
                    "jobs",
                    "submit-sqs",
                    manifest_path,
                    "--auto-region",
                    "--region",
                    "ap-southeast-2",
                ],
                manager,
            )

        assert result.exit_code == 0, result.output
        checker.recommend_region_for_job.assert_not_called()
        assert manager.submit_job_sqs.call_args.kwargs["region"] == "ap-southeast-2"


class TestPolicyHelpers:
    def test_policy_regions_preserves_requested_order_and_deduplicates(self) -> None:
        config = SimpleNamespace(default_region="us-east-1")
        assert _policy_regions(config, ("eu-west-1", "us-east-2", "eu-west-1")) == [
            "eu-west-1",
            "us-east-2",
        ]

    def test_policy_regions_uses_workload_regions_then_default(self) -> None:
        config = SimpleNamespace(default_region="us-west-2")
        with (
            patch("cli.status.resolve_regions", return_value={"regional": ["a", "b"]}),
            patch("cli.status._workload_regions", return_value=["a", "b"]),
        ):
            assert _policy_regions(config, None) == ["a", "b"]
        with (
            patch("cli.status.resolve_regions", return_value={}),
            patch("cli.status._workload_regions", return_value=[]),
        ):
            assert _policy_regions(config, None) == ["us-west-2"]

    def test_render_verdicts_explains_reject_unknown_and_enforcement_gap(self, capsys: Any) -> None:
        verdicts = [
            RegionVerdict(region="a", verdict=VERDICT_ADMIT),
            RegionVerdict(
                region="b",
                verdict=VERDICT_REJECT,
                issues=[
                    AdmissionIssue("images", "registry is not trusted", "Job/trainer"),
                    AdmissionIssue("caps", "GPU cap exceeded"),
                ],
                enforcement_gaps=["ml", "gco-jobs"],
            ),
            RegionVerdict(
                region="c",
                verdict=VERDICT_UNKNOWN,
                reason="regional bridge unavailable",
            ),
        ]

        _render_verdicts(verdicts, indent="> ")

        output = capsys.readouterr().out
        assert "[admit ] a" in output
        assert "[REJECT] b" in output
        assert "Job/trainer [images] registry is not trusted" in output
        assert "[caps] GPU cap exceeded" in output
        assert "live quota/LimitRange unreadable for ml, gco-jobs" in output
        assert "policy unreadable: regional bridge unavailable" in output

    def test_cdk_policy_loader_requires_checkout_and_extracts_only_policy(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        monkeypatch.chdir(tmp_path)
        with pytest.raises(FileNotFoundError, match="--offline reads the policy"):
            _cdk_job_validation_policy()

        path = tmp_path / "cdk.json"
        path.write_text(
            json.dumps(
                {
                    "context": {
                        "project_name": "ignored",
                        "job_validation_policy": {"validation_enabled": False},
                    }
                }
            ),
            encoding="utf-8",
        )
        policy, source = _cdk_job_validation_policy()
        assert policy == {"validation_enabled": False}
        assert source == str(path)


class TestAdvisoryPreSubmitPolicy:
    @pytest.mark.parametrize(
        ("verdicts", "expected_method", "expected_fragment"),
        [
            (
                [RegionVerdict(region="us-east-1", verdict=VERDICT_ADMIT)],
                "print_success",
                "admissible in us-east-1",
            ),
            (
                [
                    RegionVerdict(
                        region="us-east-1",
                        verdict=VERDICT_REJECT,
                        issues=[AdmissionIssue("caps", "too many GPUs", "Job/trainer")],
                    )
                ],
                "print_warning",
                "would reject",
            ),
            (
                [
                    RegionVerdict(
                        region="us-east-1",
                        verdict=VERDICT_UNKNOWN,
                        reason="not deployed",
                    )
                ],
                "print_warning",
                "no region's policy could be read",
            ),
        ],
    )
    def test_precheck_reports_outcome_but_always_submits(
        self,
        manifest_path: str,
        verdicts: list[RegionVerdict],
        expected_method: str,
        expected_fragment: str,
    ) -> None:
        missing_namespace: dict[str, Any] = {"kind": "Job", "metadata": {"name": "a"}}
        declared_namespace: dict[str, Any] = {
            "kind": "Job",
            "metadata": {"name": "b", "namespace": "declared"},
        }
        manager = MagicMock()
        manager.load_manifests.return_value = [
            missing_namespace,
            declared_namespace,
            "ignored-non-mapping",
        ]
        manager.submit_job.return_value = {"name": "trainer"}
        policies = [_readable("us-east-1")]

        with (
            patch("cli.job_policy.fetch_region_policies", return_value=policies) as fetch,
            patch("cli.job_policy.region_verdicts", return_value=verdicts) as judge,
        ):
            result, _, formatter = _invoke(
                [
                    "jobs",
                    "submit",
                    manifest_path,
                    "--check-policy",
                    "--namespace",
                    "fallback",
                    "--region",
                    "us-east-1",
                ],
                manager,
            )

        assert result.exit_code == 0, result.output
        manager.submit_job.assert_called_once()
        fetch.assert_called_once_with(manager._aws_client, ["us-east-1"])
        judge.assert_called_once_with(
            [missing_namespace, declared_namespace, "ignored-non-mapping"], policies
        )
        assert missing_namespace["metadata"]["namespace"] == "fallback"
        assert declared_namespace["metadata"]["namespace"] == "declared"
        messages = [str(args[0]) for args, _ in getattr(formatter, expected_method).call_args_list]
        assert any(expected_fragment in message for message in messages)

    def test_precheck_failure_is_non_blocking(self, manifest_path: str) -> None:
        manager = MagicMock()
        manager.load_manifests.side_effect = RuntimeError("manifest reader unavailable")
        manager.submit_job.return_value = {"name": "trainer"}

        result, _, formatter = _invoke(["jobs", "submit", manifest_path, "--check-policy"], manager)

        assert result.exit_code == 0, result.output
        manager.submit_job.assert_called_once()
        warning = formatter.print_warning.call_args.args[0]
        assert "could not run" in warning
        assert "submitting anyway" in warning


class TestDeployedPolicyCommand:
    def test_table_renders_front_door_and_live_cluster_enforcement(self) -> None:
        manager = MagicMock()
        manager._aws_client.get_job_validation_policy.return_value = {
            "region": "us-east-1",
            "cluster_id": "gco-us-east-1",
            "policy": {
                "validation_enabled": True,
                "manifest_caps": {
                    "max_cpu_millicores": 4000,
                    "max_memory_bytes": 8589934592,
                    "max_gpu_count": 2,
                },
                "allowed_namespaces": ["gco-jobs", "ml"],
                "allowed_kinds": ["Job", "Pod"],
                "trusted_registries": ["docker.io"],
                "trusted_dockerhub_orgs": ["pytorch"],
                "require_accelerator_toleration": True,
                "yaml_max_depth": 30,
                "manifest_security_policy": {
                    "block_privileged": True,
                    "block_run_as_root": False,
                },
            },
            "cluster_enforcement": {
                "blocked": {"status": "unavailable", "reason": "RBAC denied"},
                "gco-jobs": {
                    "status": "ok",
                    "resource_quotas": {"jobs": {"requests.cpu": "8", "limits.memory": "32Gi"}},
                    "limit_ranges": {
                        "containers": [
                            {"type": "Container", "max": {"cpu": "4"}},
                            {"type": "Pod"},
                        ]
                    },
                },
            },
        }

        result, _, _ = _invoke(["jobs", "policy", "--region", "us-east-1"], manager)

        assert result.exit_code == 0, result.output
        for fragment in (
            "Job Validation Policy",
            "PER-MANIFEST CAPS",
            "block_privileged",
            "block_run_as_root",
            "blocked: unavailable — RBAC denied",
            "ResourceQuota/jobs",
            "requests.cpu: 8",
            "LimitRange/containers",
            "Container max: {'cpu': '4'}",
        ):
            assert fragment in result.output

    def test_structured_policy_is_forwarded_unchanged(self) -> None:
        manager = MagicMock()
        payload = {"region": "us-east-1", "policy": {"validation_enabled": True}}
        manager._aws_client.get_job_validation_policy.return_value = payload

        result, _, formatter = _invoke(
            ["--output", "json", "jobs", "policy", "--region", "us-east-1"], manager
        )

        assert result.exit_code == 0, result.output
        formatter.print.assert_called_once_with(payload)

    def test_policy_read_failure_is_nonzero(self) -> None:
        manager = MagicMock()
        manager._aws_client.get_job_validation_policy.side_effect = RuntimeError("bridge denied")

        result, _, formatter = _invoke(["jobs", "policy", "--region", "us-east-1"], manager)

        assert result.exit_code == 1
        assert "bridge denied" in formatter.print_error.call_args.args[0]


class TestOfflinePolicyCommand:
    def test_offline_requires_a_manifest(self) -> None:
        result, manager, formatter = _invoke(["jobs", "check-policy", "--offline"])
        assert result.exit_code == 1
        formatter.print_error.assert_called_once_with("--offline needs a MANIFEST_PATH to check")
        manager.load_manifests.assert_not_called()

    @pytest.mark.parametrize("fail_on_reject", [False, True])
    def test_offline_reports_every_issue_and_optionally_fails(
        self, manifest_path: str, fail_on_reject: bool
    ) -> None:
        manager = MagicMock()
        manifest: dict[str, Any] = {"kind": "Job", "metadata": {"name": "trainer"}}
        manager.load_manifests.return_value = [manifest, "ignored"]
        issues = [
            AdmissionIssue("images", "untrusted registry", "Job/trainer"),
            AdmissionIssue("caps", "GPU cap exceeded"),
        ]
        policy = MagicMock()
        args = ["jobs", "check-policy", manifest_path, "--offline"]
        if fail_on_reject:
            args.append("--fail-on-reject")

        with (
            patch(
                "cli.commands.jobs_cmd._cdk_job_validation_policy",
                return_value=({"validation_enabled": True}, "/repo/cdk.json"),
            ),
            patch(
                "gco.job_admission.JobValidationPolicy.from_cdk_context",
                return_value=policy,
            ) as build,
            patch("cli.job_policy.evaluate_manifests", return_value=issues) as evaluate,
        ):
            result, _, _ = _invoke(args, manager)

        assert result.exit_code == (1 if fail_on_reject else 0)
        assert "Offline policy check" in result.output
        assert "Job/trainer [images] untrusted registry" in result.output
        assert "[caps] GPU cap exceeded" in result.output
        assert "not what any region has deployed" in result.output
        assert manifest["metadata"]["namespace"] == "gco-jobs"
        build.assert_called_once_with({"validation_enabled": True})
        evaluate.assert_called_once_with([manifest, "ignored"], policy)
        manager._aws_client.get_job_validation_policy.assert_not_called()

    def test_offline_structured_payload_is_complete(self, manifest_path: str) -> None:
        manager = MagicMock()
        manager.load_manifests.return_value = [{"kind": "Job", "metadata": {}}]
        issue = AdmissionIssue("caps", "too large", "Job/trainer")
        with (
            patch(
                "cli.commands.jobs_cmd._cdk_job_validation_policy",
                return_value=({}, "/checkout/cdk.json"),
            ),
            patch("gco.job_admission.JobValidationPolicy.from_cdk_context"),
            patch("cli.job_policy.evaluate_manifests", return_value=[issue]),
        ):
            result, _, formatter = _invoke(
                [
                    "--output",
                    "json",
                    "jobs",
                    "check-policy",
                    manifest_path,
                    "--offline",
                    "--fail-on-reject",
                ],
                manager,
            )

        assert result.exit_code == 1
        payload = formatter.print.call_args.args[0]
        assert payload["source"] == "/checkout/cdk.json"
        assert payload["mode"] == "offline"
        assert payload["admissible"] is False
        assert payload["issues"] == [
            {"check": "caps", "message": "too large", "manifest": "Job/trainer"}
        ]

    def test_offline_loader_failure_is_reported(self, manifest_path: str) -> None:
        with patch(
            "cli.commands.jobs_cmd._cdk_job_validation_policy",
            side_effect=ValueError("invalid cdk.json"),
        ):
            result, manager, formatter = _invoke(
                ["jobs", "check-policy", manifest_path, "--offline"]
            )

        assert result.exit_code == 1
        assert "invalid cdk.json" in formatter.print_error.call_args.args[0]
        manager.load_manifests.assert_not_called()


class TestOnlinePolicyCommand:
    def _policy_patches(
        self,
        *,
        policies: list[RegionPolicy],
        verdicts: list[RegionVerdict] | None = None,
        drift: list[PolicyDrift] | None = None,
        registry: PolicyDrift | None = None,
        augmentation: dict[str, list[str]] | None = None,
    ) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(patch("cli.job_policy.fetch_region_policies", return_value=policies))
        stack.enter_context(patch("cli.job_policy.region_verdicts", return_value=verdicts or []))
        stack.enter_context(patch("cli.job_policy.detect_policy_drift", return_value=drift or []))
        stack.enter_context(patch("cli.job_policy.registry_drift", return_value=registry))
        stack.enter_context(
            patch("cli.job_policy.ecr_augmentation", return_value=augmentation or {})
        )
        return stack

    def test_table_reports_mixed_verdicts_drift_and_ecr_additions(self, manifest_path: str) -> None:
        manager = MagicMock()
        missing_ns: dict[str, Any] = {"kind": "Job", "metadata": {"name": "trainer"}}
        declared: dict[str, Any] = {
            "kind": "Pod",
            "metadata": {"name": "helper", "namespace": "declared"},
        }
        manager.load_manifests.return_value = [missing_ns, declared, "ignored-non-mapping"]
        policies = [
            _readable("us-east-1"),
            _readable("us-west-2"),
            RegionPolicy(
                region="eu-west-1",
                status=FETCH_UNREACHABLE,
                reason="not deployed",
            ),
        ]
        verdicts = [
            RegionVerdict(region="us-east-1", verdict=VERDICT_ADMIT),
            RegionVerdict(
                region="us-west-2",
                verdict=VERDICT_REJECT,
                issues=[AdmissionIssue("caps", "GPU cap exceeded", "Job/trainer")],
            ),
            RegionVerdict(region="eu-west-1", verdict=VERDICT_UNKNOWN, reason="not deployed"),
        ]
        drift = [PolicyDrift(field="max_gpu_count", values={"us-east-1": 8, "us-west-2": 4})]
        registry = PolicyDrift(
            field="trusted_registries",
            values={"us-east-1": ["docker.io"], "us-west-2": ["quay.io"]},
        )
        augmentation = {
            "us-east-1": ["111122223333.dkr.ecr.us-east-1.amazonaws.com"],
            "us-west-2": [],
        }

        with self._policy_patches(
            policies=policies,
            verdicts=verdicts,
            drift=drift,
            registry=registry,
            augmentation=augmentation,
        ):
            result, _, _ = _invoke(
                [
                    "jobs",
                    "check-policy",
                    manifest_path,
                    "--region",
                    "us-east-1",
                    "--region",
                    "us-west-2",
                    "--region",
                    "us-east-1",
                    "--namespace",
                    "fallback",
                    "--fail-on-reject",
                ],
                manager,
            )

        assert result.exit_code == 1
        assert missing_ns["metadata"]["namespace"] == "fallback"
        assert declared["metadata"]["namespace"] == "declared"
        for fragment in (
            "Admissibility — 3 region(s)",
            "[REJECT] us-west-2",
            "policy unreadable: not deployed",
            "max_gpu_count",
            "trusted_registries",
            "ECR hostnames CDK added",
            "111122223333.dkr.ecr.us-east-1.amazonaws.com",
        ):
            assert fragment in result.output

    def test_structured_output_includes_unreadable_and_serialized_results(
        self, manifest_path: str
    ) -> None:
        manager = MagicMock()
        manager.load_manifests.return_value = [{"kind": "Job", "metadata": {}}]
        policies = [
            _readable("us-east-1"),
            RegionPolicy(region="eu-west-1", status=FETCH_UNREACHABLE, reason="no bridge"),
        ]
        verdicts = [
            RegionVerdict(
                region="us-east-1",
                verdict=VERDICT_REJECT,
                issues=[AdmissionIssue("images", "blocked", "Job/x")],
            )
        ]
        drift = [PolicyDrift("max_gpu_count", {"us-east-1": 8, "eu-west-1": None})]
        augmentation = {"us-east-1": ["ecr.example"]}
        with self._policy_patches(
            policies=policies,
            verdicts=verdicts,
            drift=drift,
            augmentation=augmentation,
        ):
            result, _, formatter = _invoke(
                [
                    "--output",
                    "json",
                    "jobs",
                    "check-policy",
                    manifest_path,
                    "--region",
                    "us-east-1",
                    "--region",
                    "eu-west-1",
                    "--fail-on-reject",
                ],
                manager,
            )

        assert result.exit_code == 1
        payload = formatter.print.call_args.args[0]
        assert payload["regions"] == ["us-east-1", "eu-west-1"]
        assert payload["unreadable"] == {"eu-west-1": "no bridge"}
        assert payload["verdicts"][0]["verdict"] == VERDICT_REJECT
        assert payload["policy_drift"][0]["field"] == "max_gpu_count"
        assert payload["ecr_augmentation"] == augmentation

    @pytest.mark.parametrize(
        ("policies", "drift", "expected"),
        [
            ([_readable("us-east-1")], [], "only one region readable"),
            (
                [_readable("us-east-1"), _readable("us-west-2")],
                [],
                "identical across us-east-1, us-west-2",
            ),
        ],
    )
    def test_comparison_only_reports_readability_and_agreement(
        self,
        policies: list[RegionPolicy],
        drift: list[PolicyDrift],
        expected: str,
    ) -> None:
        manager = MagicMock()
        with self._policy_patches(policies=policies, drift=drift):
            result, _, _ = _invoke(
                [
                    "jobs",
                    "check-policy",
                    "--region",
                    "us-east-1",
                    "--region",
                    "us-west-2",
                ],
                manager,
            )

        assert result.exit_code == 0, result.output
        assert expected in result.output
        manager.load_manifests.assert_not_called()

    def test_online_fetch_failure_is_reported(self, manifest_path: str) -> None:
        with patch(
            "cli.job_policy.fetch_region_policies",
            side_effect=RuntimeError("policy service unavailable"),
        ):
            result, _, formatter = _invoke(
                [
                    "jobs",
                    "check-policy",
                    manifest_path,
                    "--region",
                    "us-east-1",
                ]
            )

        assert result.exit_code == 1
        assert "policy service unavailable" in formatter.print_error.call_args.args[0]


class TestRenderingAndConfirmations:
    def test_global_list_renders_summaries_jobs_truncation_and_errors(self) -> None:
        manager = MagicMock()
        manager.list_jobs_global.return_value = {
            "total": 2,
            "regions_queried": 3,
            "regions_successful": 2,
            "region_summaries": [
                {"region": "us-east-1", "count": 1, "total": 1},
                {"region": "us-west-2", "count": 1, "total": 1},
            ],
            "jobs": [
                {
                    "metadata": {
                        "name": "a-very-long-training-job-name-that-is-truncated",
                        "namespace": "a-very-long-namespace",
                    },
                    "_source_region": "us-east-1-long-suffix",
                    "computed_status": "succeeded-long",
                },
                {
                    "metadata": {"name": "second", "namespace": "ml"},
                    "_source_region": "us-west-2",
                    "computed_status": "running",
                },
            ],
            "errors": [{"region": "eu-west-1", "error": "bridge unavailable"}],
        }

        result, _, formatter = _invoke(["jobs", "list", "--all-regions", "--limit", "1"], manager)

        assert result.exit_code == 0, result.output
        assert "Global Jobs Summary" in result.output
        assert "Regions successful: 2" in result.output
        assert "a-very-long-training-job-name" in result.output
        assert "second" not in result.output
        formatter.print_warning.assert_called_once_with("  eu-west-1: bridge unavailable")

    def test_global_list_structured_output_forwards_original_result(self) -> None:
        manager = MagicMock()
        payload = {"total": 0, "jobs": [], "region_summaries": []}
        manager.list_jobs_global.return_value = payload

        result, _, formatter = _invoke(
            ["--output", "json", "jobs", "list", "--all-regions"], manager
        )

        assert result.exit_code == 0, result.output
        formatter.print.assert_called_once_with(payload)

    @pytest.mark.parametrize(
        ("subcommand", "manager_method", "payload", "message"),
        [
            ("events", "get_job_events", {"events": [], "count": 0}, "No events found"),
            ("pods", "get_job_pods", {"pods": [], "count": 0}, "No pods found"),
        ],
    )
    def test_empty_table_diagnostics_are_explicit(
        self, subcommand: str, manager_method: str, payload: dict[str, Any], message: str
    ) -> None:
        manager = MagicMock()
        getattr(manager, manager_method).return_value = payload
        result, _, formatter = _invoke(
            ["jobs", subcommand, "trainer", "--region", "us-east-1"], manager
        )
        assert result.exit_code == 0, result.output
        formatter.print_info.assert_called_once_with(message + " for this job")

    @pytest.mark.parametrize(
        ("subcommand", "manager_method", "payload"),
        [
            ("events", "get_job_events", {"events": [], "count": 0}),
            ("pods", "get_job_pods", {"pods": [], "count": 0}),
            ("metrics", "get_job_metrics", {"summary": {}, "pods": []}),
        ],
    )
    def test_structured_diagnostics_forward_original_payload(
        self, subcommand: str, manager_method: str, payload: dict[str, Any]
    ) -> None:
        manager = MagicMock()
        getattr(manager, manager_method).return_value = payload
        result, _, formatter = _invoke(
            [
                "--output",
                "json",
                "jobs",
                subcommand,
                "trainer",
                "--region",
                "us-east-1",
            ],
            manager,
        )
        assert result.exit_code == 0, result.output
        formatter.print.assert_called_once_with(payload)

    def test_pod_table_tolerates_missing_fields_and_sums_restarts(self) -> None:
        manager = MagicMock()
        manager.get_job_pods.return_value = {
            "count": 1,
            "pods": [
                {
                    "metadata": {"name": "trainer-pod"},
                    "spec": {},
                    "status": {
                        "containerStatuses": [
                            {"restartCount": 2},
                            {"restartCount": 3},
                        ]
                    },
                }
            ],
        }
        result, _, _ = _invoke(["jobs", "pods", "trainer", "--region", "us-east-1"], manager)
        assert result.exit_code == 0, result.output
        assert "Unknown" in result.output
        assert "5" in result.output

    def test_logs_forwards_history_and_trainjob_node_options(self) -> None:
        manager = MagicMock()
        manager.get_job_logs.return_value = "line"
        result, _, _ = _invoke(
            [
                "jobs",
                "logs",
                "trainer",
                "--region",
                "us-east-1",
                "--namespace",
                "ml",
                "--tail",
                "12",
                "--since",
                "48",
                "--node",
                "3",
            ],
            manager,
        )
        assert result.exit_code == 0, result.output
        manager.get_job_logs.assert_called_once_with(
            "trainer",
            "ml",
            "us-east-1",
            tail_lines=12,
            since_hours=48,
            node=3,
        )

    @pytest.mark.parametrize(
        ("args", "method"),
        [
            (["jobs", "delete", "trainer", "--region", "us-east-1"], "delete_job"),
            (["jobs", "retry", "trainer", "--region", "us-east-1"], "retry_job"),
            (
                [
                    "jobs",
                    "bulk-delete",
                    "--region",
                    "us-east-1",
                    "--execute",
                ],
                "bulk_delete_jobs",
            ),
        ],
    )
    def test_destructive_confirmation_abort_never_mutates(
        self, args: list[str], method: str
    ) -> None:
        manager = MagicMock()
        result, _, _ = _invoke(args, manager, input_text="n\n")
        assert result.exit_code == 1
        getattr(manager, method).assert_not_called()

    def test_global_health_structured_output_forwards_result(self) -> None:
        manager = MagicMock()
        payload = {"overall_status": "healthy", "regions": []}
        manager.get_global_health.return_value = payload
        result, _, formatter = _invoke(
            ["--output", "json", "jobs", "health", "--all-regions"], manager
        )
        assert result.exit_code == 0, result.output
        formatter.print.assert_called_once_with(payload)


class TestRemainingJobsStateMatrix:
    def test_queue_status_all_regions_defaults_dlq_and_isolates_one_failure(self) -> None:
        manager = MagicMock()
        manager.get_queue_status.side_effect = [
            {
                "region": "us-east-1",
                "messages_available": 3,
                "messages_in_flight": 2,
                "messages_delayed": 1,
            },
            RuntimeError("bridge unavailable"),
        ]
        aws = MagicMock()
        aws.discover_regional_stacks.return_value = ["us-east-1", "us-west-2"]
        with patch("cli.aws_client.get_aws_client", return_value=aws):
            result, _, _ = _invoke(["jobs", "queue-status", "--all-regions"], manager)
        assert result.exit_code == 0, result.output
        assert "us-east-1" in result.output
        assert result.output.rstrip().endswith("0")
        assert manager.get_queue_status.call_args_list == [
            call("us-east-1"),
            call("us-west-2"),
        ]

    def test_queue_status_discovery_failure_is_nonzero(self) -> None:
        aws = MagicMock()
        aws.discover_regional_stacks.side_effect = RuntimeError("discovery denied")
        with patch("cli.aws_client.get_aws_client", return_value=aws):
            result, _, formatter = _invoke(["jobs", "queue-status", "--all-regions"])
        assert result.exit_code == 1
        assert "discovery denied" in formatter.print_error.call_args.args[0]

    def test_queue_status_all_failed_is_available_but_empty(self) -> None:
        manager = MagicMock()
        manager.get_queue_status.side_effect = RuntimeError("queue absent")
        aws = MagicMock()
        aws.discover_regional_stacks.return_value = ["us-east-1"]
        with patch("cli.aws_client.get_aws_client", return_value=aws):
            result, _, formatter = _invoke(["jobs", "queue-status", "--all-regions"], manager)
        assert result.exit_code == 0
        formatter.print_warning.assert_called_once_with("No queue status available")

    def test_specific_list_table_structured_and_failure_paths(self) -> None:
        manager = MagicMock()
        manager.list_jobs.return_value = []
        with patch("cli.commands.jobs_cmd.format_job_table", return_value="EMPTY") as table:
            result, _, _ = _invoke(["jobs", "list", "--region", "us-east-1"], manager)
        assert result.exit_code == 0
        assert "EMPTY" in result.output
        table.assert_called_once_with([])

        payload = [{"name": "trainer"}]
        manager.list_jobs.return_value = payload
        result, _, formatter = _invoke(
            ["--output", "json", "jobs", "list", "--region", "us-east-1"],
            manager,
        )
        assert result.exit_code == 0
        formatter.print.assert_called_once_with(payload)

        manager.list_jobs.side_effect = RuntimeError("regional API failed")
        result, _, formatter = _invoke(["jobs", "list", "--region", "us-east-1"], manager)
        assert result.exit_code == 1
        assert "regional API failed" in formatter.print_error.call_args.args[0]

    def test_get_job_success_not_found_and_error(self) -> None:
        manager = MagicMock()
        manager.get_job.return_value = {"metadata": {"name": "trainer"}}
        result, _, formatter = _invoke(["jobs", "get", "trainer", "--region", "us-east-1"], manager)
        assert result.exit_code == 0
        formatter.print.assert_called_once()

        manager.get_job.return_value = None
        result, _, formatter = _invoke(["jobs", "get", "missing", "--region", "us-east-1"], manager)
        assert result.exit_code == 1
        formatter.print_error.assert_called_once_with("Job missing not found")

        manager.get_job.side_effect = RuntimeError("read denied")
        result, _, formatter = _invoke(["jobs", "get", "trainer", "--region", "us-east-1"], manager)
        assert result.exit_code == 1
        assert "read denied" in formatter.print_error.call_args.args[0]

    def test_delete_confirmed_success_and_failure(self) -> None:
        manager = MagicMock()
        result, _, formatter = _invoke(
            ["jobs", "delete", "trainer", "--region", "us-east-1"],
            manager,
            input_text="y\n",
        )
        assert result.exit_code == 0
        manager.delete_job.assert_called_once_with("trainer", "gco-jobs", "us-east-1")
        formatter.print_success.assert_called_once_with("Job trainer deleted")

        manager.delete_job.side_effect = RuntimeError("delete denied")
        result, _, formatter = _invoke(
            ["jobs", "delete", "trainer", "--region", "us-east-1", "--yes"],
            manager,
        )
        assert result.exit_code == 1
        assert "delete denied" in formatter.print_error.call_args.args[0]

    def test_events_table_defaults_and_failure(self) -> None:
        manager = MagicMock()
        manager.get_job_events.return_value = {
            "count": 2,
            "events": [
                {"type": "Warning", "reason": None, "message": None},
                {
                    "type": None,
                    "reason": "Created",
                    "message": "pod created",
                    "firstTimestamp": "2026-01-01T00:00:00Z",
                },
            ],
        }
        result, _, _ = _invoke(["jobs", "events", "trainer", "--region", "us-east-1"], manager)
        assert result.exit_code == 0
        assert "⚠" in result.output
        assert "✓" in result.output
        assert "Created" in result.output

        manager.get_job_events.side_effect = RuntimeError("events denied")
        result, _, formatter = _invoke(
            ["jobs", "events", "trainer", "--region", "us-east-1"], manager
        )
        assert result.exit_code == 1
        assert "events denied" in formatter.print_error.call_args.args[0]

    def test_pod_logs_content_empty_and_error(self) -> None:
        manager = MagicMock()
        manager.get_pod_logs.return_value = {"logs": "line one\nline two"}
        result, _, _ = _invoke(
            [
                "jobs",
                "pod-logs",
                "trainer",
                "trainer-pod",
                "--region",
                "us-east-1",
                "--container",
                "sidecar",
                "--tail",
                "17",
            ],
            manager,
        )
        assert result.exit_code == 0
        assert "line one" in result.output
        manager.get_pod_logs.assert_called_once_with(
            job_name="trainer",
            pod_name="trainer-pod",
            namespace="gco-jobs",
            region="us-east-1",
            tail_lines=17,
            container="sidecar",
        )

        manager.get_pod_logs.return_value = {"logs": ""}
        result, _, formatter = _invoke(
            ["jobs", "pod-logs", "trainer", "pod", "-r", "us-east-1"],
            manager,
        )
        assert result.exit_code == 0
        formatter.print_info.assert_called_once_with("No logs available")

        manager.get_pod_logs.side_effect = RuntimeError("pod gone")
        result, _, formatter = _invoke(
            ["jobs", "pod-logs", "trainer", "pod", "-r", "us-east-1"],
            manager,
        )
        assert result.exit_code == 1
        assert "pod gone" in formatter.print_error.call_args.args[0]

    def test_metrics_table_empty_rich_and_error(self) -> None:
        manager = MagicMock()
        manager.get_job_metrics.return_value = {
            "summary": {
                "total_cpu_millicores": 750,
                "total_memory_mib": 1024.5,
                "pod_count": 1,
            },
            "pods": [
                {
                    "pod_name": "trainer-pod",
                    "containers": [
                        {"cpu_millicores": 250, "memory_mib": 512.25},
                        {"cpu_millicores": 500, "memory_mib": 512.25},
                    ],
                }
            ],
        }
        result, _, _ = _invoke(["jobs", "metrics", "trainer", "--region", "us-east-1"], manager)
        assert result.exit_code == 0
        assert "750m" in result.output
        assert "1024.5 MiB" in result.output
        assert "trainer-pod" in result.output

        manager.get_job_metrics.return_value = {"summary": {}, "pods": []}
        result, _, _ = _invoke(["jobs", "metrics", "trainer", "--region", "us-east-1"], manager)
        assert result.exit_code == 0
        assert "Total CPU: 0m" in result.output

        manager.get_job_metrics.side_effect = RuntimeError("metrics unavailable")
        result, _, formatter = _invoke(
            ["jobs", "metrics", "trainer", "--region", "us-east-1"], manager
        )
        assert result.exit_code == 1
        assert "metrics unavailable" in formatter.print_error.call_args.args[0]

    def test_retry_success_refusal_and_exception(self) -> None:
        manager = MagicMock()
        manager.retry_job.return_value = {"success": True, "new_job": "trainer-retry"}
        result, _, formatter = _invoke(
            ["jobs", "retry", "trainer", "--region", "us-east-1"],
            manager,
            input_text="y\n",
        )
        assert result.exit_code == 0
        formatter.print_success.assert_called_once_with("Job retry created: trainer-retry")
        formatter.print.assert_called_once()

        manager.retry_job.return_value = {"success": False, "message": "still running"}
        result, _, formatter = _invoke(
            ["jobs", "retry", "trainer", "--region", "us-east-1", "--yes"],
            manager,
        )
        assert result.exit_code == 1
        formatter.print_error.assert_called_once_with("Failed to retry job: still running")

        manager.retry_job.side_effect = RuntimeError("source missing")
        result, _, formatter = _invoke(
            ["jobs", "retry", "trainer", "--region", "us-east-1", "--yes"],
            manager,
        )
        assert result.exit_code == 1
        assert "source missing" in formatter.print_error.call_args.args[0]

    def test_bulk_delete_single_global_dry_run_success_and_error(self) -> None:
        manager = MagicMock()
        manager.bulk_delete_jobs.return_value = {"total_matched": 4}
        result, _, formatter = _invoke(
            [
                "jobs",
                "bulk-delete",
                "--region",
                "us-east-1",
                "--status",
                "failed",
                "--older-than-days",
                "7",
                "--label-selector",
                "team=ml",
            ],
            manager,
        )
        assert result.exit_code == 0
        manager.bulk_delete_jobs.assert_called_once_with(
            namespace=None,
            status="failed",
            older_than_days=7,
            label_selector="team=ml",
            region="us-east-1",
            dry_run=True,
        )
        formatter.print_info.assert_any_call("Would delete 4 jobs")

        manager.bulk_delete_global.return_value = {"total_deleted": 3}
        result, _, formatter = _invoke(
            ["jobs", "bulk-delete", "--all-regions", "--execute", "--yes"],
            manager,
        )
        assert result.exit_code == 0
        manager.bulk_delete_global.assert_called_once()
        formatter.print_success.assert_called_once_with("Deleted 3 jobs")

        manager.bulk_delete_jobs.side_effect = RuntimeError("bulk denied")
        result, _, formatter = _invoke(
            [
                "jobs",
                "bulk-delete",
                "--region",
                "us-east-1",
                "--execute",
                "--yes",
            ],
            manager,
        )
        assert result.exit_code == 1
        assert "bulk denied" in formatter.print_error.call_args.args[0]

    def test_health_table_empty_mixed_single_region_and_error(self) -> None:
        manager = MagicMock()
        manager.get_global_health.return_value = {
            "overall_status": "degraded",
            "healthy_regions": 1,
            "total_regions": 2,
            "regions": [
                {"region": "us-east-1", "status": "healthy", "cluster_id": "east"},
                {"region": "us-west-2", "status": "failed", "cluster_id": "west"},
            ],
        }
        result, _, _ = _invoke(["jobs", "health", "--all-regions"], manager)
        assert result.exit_code == 0
        assert "✓ us-east-1" in result.output
        assert "✗ us-west-2" in result.output

        manager.get_global_health.return_value = {
            "overall_status": "unknown",
            "healthy_regions": 0,
            "total_regions": 0,
            "regions": [],
        }
        result, _, _ = _invoke(["jobs", "health", "--all-regions"], manager)
        assert result.exit_code == 0
        assert "REGION" not in result.output

        payload = {"status": "healthy"}
        manager._aws_client.get_health.return_value = payload
        result, _, formatter = _invoke(["jobs", "health", "--region", "us-east-1"], manager)
        assert result.exit_code == 0
        formatter.print.assert_called_once_with(payload)

        manager.get_global_health.side_effect = RuntimeError("health denied")
        result, _, formatter = _invoke(["jobs", "health", "--all-regions"], manager)
        assert result.exit_code == 1
        assert "health denied" in formatter.print_error.call_args.args[0]

    def test_submit_direct_wait_failure_and_submit_queue_yaml_failure(
        self, manifest_path: str, tmp_path: Any
    ) -> None:
        manager = MagicMock()
        manager.submit_job_direct.side_effect = RuntimeError("kubectl denied")
        result, _, formatter = _invoke(
            ["jobs", "submit-direct", manifest_path, "-r", "us-east-1"], manager
        )
        assert result.exit_code == 1
        assert "kubectl denied" in formatter.print_error.call_args.args[0]

        bad = tmp_path / "bad.yaml"
        bad.write_text("root: &alias [*alias]\n", encoding="utf-8")
        aws = MagicMock()
        with patch("cli.aws_client.get_aws_client", return_value=aws):
            result, _, formatter = _invoke(
                ["jobs", "submit-queue", str(bad), "--region", "us-east-1"]
            )
        assert result.exit_code == 1
        aws.call_api.assert_not_called()
        assert "Failed to queue job" in formatter.print_error.call_args.args[0]
