"""Offline tests for the example harness's platform add-on and EKS Capability examples.

Covers what ``scripts/example_job_validation`` gained for
``argocd-gitops-job``, ``crossplane-batch-{api,job}``, ``kro-batch-{api,job}``
and ``ack-sqs-queue``: the capability overrides a selection derives (and the
ACK policy settings riding with them), the offline checks (the Argo CD fence,
companion/capability spec shape, and the governance / image / namespace rules
applied to what an Application syncs or an RGD / Composition composes), the
three new success criteria, the Argo CD / companion-API / ACK setup drivers,
the post-cleanup wait for derived Jobs, and ``_run_one_example``'s dispatch —
all against scripted kubectl and fake boto3, with no AWS or cluster access.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from scripts.example_job_validation import actions, drivers, static_checks
from scripts.example_job_validation.specs import (
    ACK_RESOURCE_SYNCED,
    ARGOCD_APP_HEALTHY,
    COMPANION,
    COMPOSED_JOB_COMPLETES,
    EXAMPLE_SPECS,
    required_capability_overrides,
    required_capability_settings,
    required_helm_overrides,
)
from tests.test_example_job_validation import (
    _JOB_COMPLETE,
    REPO_ROOT,
    _FakeSession,
    _install_clock,
    _ScriptedKubectl,
    _settings,
    _synthetic_parsed,
)

_THIS_REPO = (
    "https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws.git"
)
_SHA = "0123456789abcdef0123456789abcdef01234567"
_ACK_POLICY = "arn:aws:iam::aws:policy/AmazonSQSFullAccess"


def _json(payload: dict[str, Any]) -> tuple[int, str, str]:
    return 0, json.dumps(payload), ""


_MISSING = (1, "", 'Error from server (NotFound): "x" not found')


# ─── spec registry and derived settings ──────────────────────────────────────


class TestDerivedCapabilities:
    def test_new_examples_are_registered(self) -> None:
        assert EXAMPLE_SPECS["argocd-gitops-job"].criteria == ARGOCD_APP_HEALTHY
        assert EXAMPLE_SPECS["crossplane-batch-job"].criteria == COMPOSED_JOB_COMPLETES
        assert EXAMPLE_SPECS["kro-batch-job"].criteria == COMPOSED_JOB_COMPLETES
        assert EXAMPLE_SPECS["ack-sqs-queue"].criteria == ACK_RESOURCE_SYNCED
        for companion in ("crossplane-batch-api", "kro-batch-api"):
            assert EXAMPLE_SPECS[companion].submission == COMPANION
        assert EXAMPLE_SPECS["crossplane-batch-job"].companion == "crossplane-batch-api"
        assert EXAMPLE_SPECS["kro-batch-job"].companion == "kro-batch-api"

    def test_helm_and_capability_requirements(self) -> None:
        assert required_helm_overrides(["argocd-gitops-job", "crossplane-batch-job"]) == (
            "argocd",
            "crossplane",
        )
        assert required_capability_overrides(sorted(EXAMPLE_SPECS)) == ("ack", "kro")
        assert required_capability_overrides(["simple-job"]) == ()

    def test_only_ack_examples_carry_capability_settings(self) -> None:
        assert required_capability_settings(["kro-batch-job"]) == {}
        assert required_capability_settings(["ack-sqs-queue", "kro-batch-job"]) == {
            "ack": {"iam_policy_arns": [_ACK_POLICY]}
        }

    def test_selection_derives_the_capability_overrides_context(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, examples=("ack-sqs-queue", "kro-batch-job"))
        assert settings.capability_overrides == ("ack", "kro")
        overrides = json.loads(settings.eks_capabilities_overrides_json)
        assert overrides == {
            "ack": {"enabled": True, "iam_policy_arns": [_ACK_POLICY]},
            "kro": {"enabled": True},
        }
        # Canonical JSON: the same bytes the live harness's --eks-capabilities builds.
        assert settings.eks_capabilities_overrides_json == json.dumps(
            overrides, sort_keys=True, separators=(",", ":")
        )
        context = settings.extra_cdk_context()
        assert context["eks_capabilities_overrides"] == settings.eks_capabilities_overrides_json
        identity = settings.identity()
        assert identity["capability_overrides"] == ["ack", "kro"]
        assert identity["extra_cdk_context"] == context

    def test_selection_without_capabilities_adds_no_context(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, examples=("argocd-gitops-job",))
        assert settings.eks_capabilities_overrides_json == ""
        assert settings.optional_schedulers == ("argocd",)
        assert "eks_capabilities_overrides" not in settings.extra_cdk_context()
        assert settings.identity()["capability_overrides"] == []


class TestSpecShape:
    @pytest.mark.parametrize(
        ("changes", "problem"),
        [
            ({"submission": "carrier-pigeon"}, "unknown submission path"),
            ({"capability_overrides": ("argocd",)}, "unknown EKS capability type(s)"),
            ({"ack_iam_policy_arns": (_ACK_POLICY,)}, "ack_iam_policy_arns needs"),
            ({"companion": "simple-job"}, "is not a companion-artifact spec"),
            ({"companion": "no-such-example"}, "is not a companion-artifact spec"),
        ],
    )
    def test_bad_specs_are_named(
        self, monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any], problem: str
    ) -> None:
        bad = dataclasses.replace(EXAMPLE_SPECS["simple-job"], **changes)
        monkeypatch.setitem(static_checks.EXAMPLE_SPECS, "simple-job", bad)
        finding = static_checks.check_spec_shape("simple-job")
        assert not finding.passed
        assert problem in finding.detail

    def test_shipped_specs_pass(self) -> None:
        for name in EXAMPLE_SPECS:
            assert static_checks.check_spec_shape(name).passed, name


# ─── offline checks over Applications, RGDs and Compositions ─────────────────


def _application(**spec_overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "project": "gco-tenants",
        "source": {"repoURL": _THIS_REPO, "targetRevision": "main", "path": "fixture"},
        "destination": {"server": "https://kubernetes.default.svc", "namespace": "gco-jobs"},
    }
    spec.update(spec_overrides)
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {"name": "app", "namespace": "argocd"},
        "spec": spec,
    }


def _job(name: str = "synced", **metadata: Any) -> dict[str, Any]:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, **metadata},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "c",
                            "image": "busybox:1.38.0",
                            "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}},
                        }
                    ]
                }
            }
        },
    }


def _repo_with_fixture(tmp_path: Path, *documents: Any, suffix: str = ".yaml") -> Path:
    fixture = tmp_path / "fixture"
    fixture.mkdir(parents=True, exist_ok=True)
    (fixture / f"manifest{suffix}").write_text(yaml.safe_dump_all(documents), encoding="utf-8")
    (tmp_path / "examples").mkdir(exist_ok=True)
    return tmp_path


class TestApplicationSourceDocuments:
    def test_reads_the_git_path_and_defaults_the_namespace(self, tmp_path: Path) -> None:
        repo = _repo_with_fixture(
            tmp_path, _job("plain"), _job("pinned", namespace="gco-inference"), "not-a-mapping"
        )
        (repo / "fixture" / "extra.yml").write_text(
            yaml.safe_dump({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "cm"}}),
            encoding="utf-8",
        )
        documents = static_checks.application_source_documents(repo, _application())
        assert [(doc["kind"], doc["metadata"]["namespace"]) for doc in documents] == [
            ("ConfigMap", "gco-jobs"),
            ("Job", "gco-jobs"),
            ("Job", "gco-inference"),
        ]

    @pytest.mark.parametrize(
        "source",
        [
            {"repoURL": "https://github.com/someone/else.git", "path": "fixture"},
            {"repoURL": _THIS_REPO, "path": "../outside"},
            {"repoURL": _THIS_REPO, "path": "missing"},
        ],
    )
    def test_unreadable_sources_yield_nothing(self, tmp_path: Path, source: dict) -> None:
        repo = _repo_with_fixture(tmp_path, _job())
        assert static_checks.application_source_documents(repo, _application(source=source)) == []

    def test_the_shipped_example_syncs_the_hello_job(self) -> None:
        parsed = static_checks.parse_example(REPO_ROOT, "argocd-gitops-job")
        (application,) = parsed.documents
        (job,) = static_checks.application_source_documents(REPO_ROOT, application)
        assert (job["kind"], job["metadata"]["name"]) == ("Job", "gco-gitops-hello")
        assert job["metadata"]["namespace"] == "gco-jobs"
        # A TTL would make self-heal re-create the Job forever.
        assert "ttlSecondsAfterFinished" not in job["spec"]


class TestEmbeddedWorkloads:
    @pytest.mark.parametrize(
        ("namespace", "expected"),
        [
            (None, "gco-jobs"),
            ("${schema.metadata.namespace}", "gco-jobs"),
            ("{{ .observed.composite.resource.metadata.namespace }}", "gco-jobs"),
            ("gco-inference", "gco-inference"),
            (7, 7),
        ],
    )
    def test_templated_namespaces_resolve_to_the_governed_tenant(
        self, namespace: object, expected: object
    ) -> None:
        assert static_checks._tenant_namespace(namespace) == expected

    def test_rgd_templates_are_read_and_odd_entries_skipped(self) -> None:
        rgd = {
            "apiVersion": "kro.run/v1alpha1",
            "kind": "ResourceGraphDefinition",
            "spec": {
                "resources": [
                    {"id": "job", "template": _job(namespace="${schema.metadata.namespace}")},
                    {"id": "ref", "externalRef": {}},
                    "not-a-mapping",
                ]
            },
        }
        (template,) = static_checks.embedded_workload_documents(REPO_ROOT, rgd)
        assert template["metadata"]["namespace"] == "gco-jobs"
        # The source document is not mutated.
        assert rgd["spec"]["resources"][0]["template"]["metadata"]["namespace"] == (
            "${schema.metadata.namespace}"
        )

    def test_composition_templates_render_statically(self) -> None:
        template = "\n".join(
            [
                "apiVersion: batch/v1",
                "kind: Job",
                "metadata:",
                '  name: "{{ .observed.composite.resource.metadata.name }}"',
                "  annotations:",
                "    {{ if true }}",
                '    gotemplating.fn.crossplane.io/ready: "True"',
                "    {{ end }}",
                "---",
                "apiVersion: examples.gco.io/v1alpha1",
                "kind: BatchJob",
                "status: {}",
                "---",
                "just-a-string",
            ]
        )
        composition = {
            "apiVersion": "apiextensions.crossplane.io/v1",
            "kind": "Composition",
            "spec": {
                "compositeTypeRef": {"apiVersion": "examples.gco.io/v1alpha1", "kind": "BatchJob"},
                "pipeline": [
                    {"step": "other", "input": {"kind": "Resources"}},
                    {"step": "remote", "input": {"kind": "GoTemplate", "source": "FileSystem"}},
                    {"step": "no-input"},
                    {
                        "step": "render",
                        "input": {
                            "kind": "GoTemplate",
                            "source": "Inline",
                            "inline": {"template": template},
                        },
                    },
                ],
            },
        }
        (job,) = static_checks.embedded_workload_documents(REPO_ROOT, composition)
        assert job["kind"] == "Job"
        assert job["metadata"]["namespace"] == "gco-jobs"
        assert job["metadata"]["annotations"] == {"gotemplating.fn.crossplane.io/ready": "True"}

    def test_other_kinds_embed_nothing(self) -> None:
        assert static_checks.embedded_workload_documents(REPO_ROOT, _job()) == []
        assert (
            static_checks.embedded_workload_documents(
                REPO_ROOT, {"apiVersion": "other.io/v1", "kind": "Composition", "spec": {}}
            )
            == []
        )

    @pytest.mark.parametrize(
        ("name", "embedded_kind"),
        [("kro-batch-api", "Job"), ("crossplane-batch-api", "Job"), ("argocd-gitops-job", "Job")],
    )
    def test_shipped_companions_compose_a_governed_job(self, name: str, embedded_kind: str) -> None:
        parsed = static_checks.parse_example(REPO_ROOT, name)
        kinds = [
            item["kind"]
            for doc in parsed.documents
            for item in static_checks.embedded_workload_documents(REPO_ROOT, doc)
        ]
        assert kinds == [embedded_kind]
        findings = static_checks.check_embedded_workloads(REPO_ROOT, parsed)
        assert findings and all(finding.passed for finding in findings)

    def test_findings_catch_a_bad_embedded_workload(self, tmp_path: Path) -> None:
        big = _job("big", namespace="kube-system")
        big["spec"]["template"]["spec"]["containers"][0]["image"] = "evil.example.com/x:1"
        repo = _repo_with_fixture(tmp_path, big)
        parsed = _synthetic_parsed("argocd-gitops-job", EXAMPLE_SPECS["argocd-gitops-job"], [])
        parsed = dataclasses.replace(parsed, documents=[_application()])
        failed = {
            finding.check.split(" (")[0]
            for finding in static_checks.check_embedded_workloads(repo, parsed)
            if not finding.passed
        }
        assert failed == {"embedded workload namespace", "trusted image sources"}

    def test_an_application_with_nothing_to_read_fails(self) -> None:
        parsed = _synthetic_parsed(
            "argocd-gitops-job",
            EXAMPLE_SPECS["argocd-gitops-job"],
            [_application(source={"repoURL": "https://example.com/x.git", "path": "."})],
        )
        (finding,) = static_checks.check_embedded_workloads(REPO_ROOT, parsed)
        assert finding.check.startswith("Git source resolves")
        assert not finding.passed
        assert "non-empty directory of this repository" in finding.detail


class TestArgoCdFence:
    def _check(self, doc: dict[str, Any]) -> static_checks.StaticFinding:
        parsed = _synthetic_parsed("argocd-gitops-job", EXAMPLE_SPECS["argocd-gitops-job"], [doc])
        (finding,) = static_checks.check_namespaces(parsed)
        return finding

    def test_the_fenced_application_passes(self) -> None:
        finding = self._check(_application())
        assert finding.passed and finding.check == "Argo CD fence (Application/app)"

    def test_every_escape_is_named(self) -> None:
        doc = _application(
            project="default",
            destination={"server": "https://elsewhere:6443", "namespace": "gco-system"},
        )
        doc["metadata"]["namespace"] = "gco-jobs"
        finding = self._check(doc)
        assert not finding.passed
        for fragment in (
            "namespace 'gco-jobs' is not 'argocd'",
            "project 'default' is not 'gco-tenants'",
            "is not the in-cluster server",
            "destination namespace 'gco-system' is not a workload namespace",
        ):
            assert fragment in finding.detail

    def test_a_non_argo_application_kind_is_an_ordinary_namespaced_object(self) -> None:
        doc = {"apiVersion": "app.k8s.io/v1beta1", "kind": "Application", "metadata": {"name": "a"}}
        parsed = _synthetic_parsed("simple-job", EXAMPLE_SPECS["simple-job"], [doc])
        assert static_checks.check_namespaces(parsed) == []


# ─── the three new success criteria ──────────────────────────────────────────


def _app_status(
    *,
    sync: str = "Synced",
    health: str = "Healthy",
    revision: str = _SHA,
    target: str = _SHA,
    phase: str = "Succeeded",
    resources: list[dict[str, str]] | None = None,
    conditions: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "spec": {"source": {"targetRevision": target}},
        "status": {
            "sync": {"status": sync, "revision": revision},
            "health": {"status": health, "message": "health detail"},
            "operationState": {"phase": phase, "message": "operation detail"},
            "resources": resources
            if resources is not None
            else [{"kind": "Job", "namespace": "gco-jobs", "name": "synced"}],
            "conditions": conditions or [],
        },
    }


class TestArgoCdApplicationWaiter:
    @pytest.fixture
    def parsed(self, tmp_path: Path) -> static_checks.ParsedExample:
        repo = _repo_with_fixture(
            tmp_path, {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "cm"}}, _job()
        )
        return static_checks.ParsedExample(
            name="argocd-gitops-job",
            path=repo / "examples" / "argocd-gitops-job.yaml",
            spec=EXAMPLE_SPECS["argocd-gitops-job"],
            documents=[_application()],
        )

    def test_synced_healthy_at_the_pinned_revision(self, parsed, monkeypatch) -> None:
        clock = _install_clock(monkeypatch)
        kubectl = _ScriptedKubectl(
            {
                ("get", "application"): [
                    _MISSING,
                    _json(_app_status(sync="OutOfSync", health="Progressing", phase="Running")),
                    _json(_app_status()),
                ],
                ("get", "job"): (0, _JOB_COMPLETE, ""),
            }
        )
        evidence = drivers.wait_argocd_application_healthy(parsed, kubectl, timeout=300)
        assert evidence == {
            "applications": {"argocd/app": {"revision": _SHA, "resources": 1}},
            "jobs": {"gco-jobs/synced": "complete"},
        }
        assert clock.sleeps == [drivers._POLL_SECONDS] * 2

    def test_branch_targets_skip_the_revision_pin(self, parsed) -> None:
        kubectl = _ScriptedKubectl(
            {
                ("get", "application"): _json(_app_status(target="main", revision="f" * 40)),
                ("get", "job"): (0, _JOB_COMPLETE, ""),
            }
        )
        evidence = drivers.wait_argocd_application_healthy(parsed, kubectl, timeout=30)
        assert evidence["applications"]["argocd/app"]["revision"] == "f" * 40

    @pytest.mark.parametrize(
        ("status", "message"),
        [
            (_app_status(phase="Failed"), "sync Failed: operation detail"),
            (_app_status(phase="Error"), "sync Error"),
            (_app_status(health="Degraded"), "is Degraded: health detail"),
            (_app_status(revision="e" * 40), "synced revision 'eeee"),
        ],
    )
    def test_terminal_states_fail_fast(self, parsed, status, message) -> None:
        kubectl = _ScriptedKubectl({("get", "application"): _json(status)})
        with pytest.raises(drivers.ExampleValidationError, match=message):
            drivers.wait_argocd_application_healthy(parsed, kubectl, timeout=30)

    def test_timeout_reports_the_last_state_and_conditions(self, parsed, monkeypatch) -> None:
        _install_clock(monkeypatch)
        stuck = _app_status(
            sync="Unknown",
            health="Missing",
            phase="",
            conditions=[{"type": "ComparisonError", "status": "True", "message": "repo 404"}],
        )
        kubectl = _ScriptedKubectl({("get", "application"): _json(stuck)})
        with pytest.raises(drivers.ExampleValidationError) as excinfo:
            drivers.wait_argocd_application_healthy(parsed, kubectl, timeout=30)
        assert "not Synced/Healthy within 30s" in str(excinfo.value)
        assert "ComparisonError=True repo 404" in str(excinfo.value)

    def test_the_git_jobs_must_be_managed_and_complete(self, parsed) -> None:
        unmanaged = _ScriptedKubectl({("get", "application"): _json(_app_status(resources=[]))})
        with pytest.raises(drivers.ExampleValidationError, match="not among the resources"):
            drivers.wait_argocd_application_healthy(parsed, unmanaged, timeout=30)
        running = _ScriptedKubectl(
            {
                ("get", "application"): _json(_app_status()),
                ("get", "job"): (0, '{"status": {}}', ""),
            }
        )
        with pytest.raises(drivers.ExampleValidationError, match="Job gco-jobs/synced is running"):
            drivers.wait_argocd_application_healthy(parsed, running, timeout=30)

    def test_an_application_without_jobs_or_the_file_without_applications(
        self, parsed, tmp_path: Path
    ) -> None:
        (tmp_path / "fixture" / "manifest.yaml").write_text(
            yaml.safe_dump({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "cm"}}),
            encoding="utf-8",
        )
        kubectl = _ScriptedKubectl({("get", "application"): _json(_app_status(resources=[]))})
        with pytest.raises(drivers.ExampleValidationError, match="syncs no Job"):
            drivers.wait_argocd_application_healthy(parsed, kubectl, timeout=30)
        empty = dataclasses.replace(parsed, documents=[_job()])
        with pytest.raises(drivers.ExampleValidationError, match="defines no Application"):
            drivers.wait_argocd_application_healthy(empty, kubectl, timeout=30)


def _instance(kind: str = "BatchJob", api: str = "kro.run/v1alpha1") -> dict[str, Any]:
    return {"apiVersion": api, "kind": kind, "metadata": {"name": "hello", "namespace": "gco-jobs"}}


class TestComposedJobWaiter:
    def _parsed(self, *documents: dict[str, Any]) -> static_checks.ParsedExample:
        return _synthetic_parsed(
            "kro-batch-job", EXAMPLE_SPECS["kro-batch-job"], list(documents) or [_instance()]
        )

    def test_the_composed_job_completes(self, monkeypatch) -> None:
        clock = _install_clock(monkeypatch)
        kubectl = _ScriptedKubectl(
            {
                ("get", "job"): [_MISSING, (0, _JOB_COMPLETE, "")],
                ("get", "events"): (0, "", ""),
                ("get", "batchjob.kro.run"): _json(
                    {
                        "status": {
                            "state": "ACTIVE",
                            "conditions": [{"type": "Ready", "status": "True"}],
                        }
                    }
                ),
            }
        )
        evidence = drivers.wait_composed_jobs_complete(self._parsed(), kubectl, timeout=300)
        assert evidence == {
            "composed_jobs": {
                "gco-jobs/hello": {"job": "complete", "instance": "state=ACTIVE Ready=True"}
            }
        }
        assert clock.sleeps == [drivers._POLL_SECONDS]

    def test_a_failed_job_carries_its_logs(self) -> None:
        kubectl = _ScriptedKubectl(
            {
                ("get", "job"): _json(
                    {
                        "status": {
                            "conditions": [{"type": "Failed", "status": "True", "message": "x"}]
                        }
                    }
                ),
                ("logs",): (0, "boom", ""),
            }
        )
        with pytest.raises(drivers.ExampleValidationError, match="failed: x :: last logs: boom"):
            drivers.wait_composed_jobs_complete(self._parsed(), kubectl, timeout=30)

    def test_admission_rejection_fails_fast(self) -> None:
        kubectl = _ScriptedKubectl(
            {
                ("get", "job"): (0, '{"status": {}}', ""),
                ("get", "events"): (0, 'pods "hello-x" is forbidden: maximum cpu\n', ""),
            }
        )
        with pytest.raises(drivers.ExampleValidationError, match="rejected at admission"):
            drivers.wait_composed_jobs_complete(self._parsed(), kubectl, timeout=30)

    @pytest.mark.parametrize(
        ("instance_response", "summary"),
        [
            (_MISSING, "instance not found"),
            (_json({}), "no status yet"),
            (
                _json({"status": {"conditions": [{"type": "Synced", "status": "False"}]}}),
                "Synced=False",
            ),
        ],
    )
    def test_timeout_reports_the_instance(
        self, monkeypatch, instance_response, summary: str
    ) -> None:
        _install_clock(monkeypatch)
        kubectl = _ScriptedKubectl(
            {
                ("get", "job"): _MISSING,
                ("get", "events"): (0, "", ""),
                ("get", "batchjob.examples.gco.io"): instance_response,
            }
        )
        parsed = self._parsed(_instance(api="examples.gco.io/v1alpha1"))
        with pytest.raises(drivers.ExampleValidationError) as excinfo:
            drivers.wait_composed_jobs_complete(parsed, kubectl, timeout=30)
        assert "is missing after 30s" in str(excinfo.value)
        assert f"batchjob.examples.gco.io: {summary}" in str(excinfo.value)

    def test_a_file_without_instances_is_a_spec_error(self) -> None:
        parsed = self._parsed({"kind": "Nameless", "metadata": {}})
        with pytest.raises(drivers.ExampleValidationError, match="defines nothing"):
            drivers.wait_composed_jobs_complete(parsed, _ScriptedKubectl({}), timeout=30)


def _queue_doc() -> dict[str, Any]:
    return {
        "apiVersion": "sqs.services.k8s.aws/v1alpha1",
        "kind": "Queue",
        "metadata": {"name": "gco-ack-example", "namespace": "gco-jobs"},
        "spec": {"queueName": "gco-ack-example"},
    }


class TestAckResourceWaiter:
    def _parsed(self, *documents: dict[str, Any]) -> static_checks.ParsedExample:
        return _synthetic_parsed(
            "ack-sqs-queue", EXAMPLE_SPECS["ack-sqs-queue"], list(documents) or [_queue_doc()]
        )

    def test_synced_resources_report_aws_identifiers(self, monkeypatch) -> None:
        clock = _install_clock(monkeypatch)
        synced = {
            "status": {
                "ackResourceMetadata": {
                    "arn": "arn:aws:sqs:us-east-1:111122223333:gco-ack-example"
                },
                "queueURL": "https://sqs.us-east-1.amazonaws.com/111122223333/gco-ack-example",
                "notAUrl": 3,
                "conditions": [{"type": "ACK.ResourceSynced", "status": "True"}],
            }
        }
        kubectl = _ScriptedKubectl(
            {
                ("get", "queue.sqs.services.k8s.aws"): [
                    _MISSING,
                    _json(
                        {
                            "status": {
                                "conditions": [
                                    {
                                        "type": "ACK.ResourceSynced",
                                        "status": "False",
                                        "reason": "Pending",
                                    }
                                ]
                            }
                        }
                    ),
                    _json(synced),
                ]
            }
        )
        evidence = drivers.wait_ack_resources_synced(self._parsed(), kubectl, timeout=300)
        assert evidence == {
            "ack_resources": {
                "gco-jobs/gco-ack-example": {
                    "arn": "arn:aws:sqs:us-east-1:111122223333:gco-ack-example",
                    "queueURL": "https://sqs.us-east-1.amazonaws.com/111122223333/gco-ack-example",
                }
            }
        }
        assert len(clock.sleeps) == 2

    def test_terminal_fails_fast(self) -> None:
        terminal = {
            "status": {
                "conditions": [
                    {"type": "ACK.Terminal", "status": "True", "message": "InvalidAttributeValue"}
                ]
            }
        }
        kubectl = _ScriptedKubectl({("get", "queue.sqs.services.k8s.aws"): _json(terminal)})
        with pytest.raises(
            drivers.ExampleValidationError, match=r"is terminal: .*InvalidAttribute"
        ):
            drivers.wait_ack_resources_synced(self._parsed(), kubectl, timeout=30)

    @pytest.mark.parametrize(
        ("response", "last"),
        [
            (_MISSING, "not found"),
            (_json({"status": {}}), "no conditions yet"),
            (
                _json(
                    {
                        "status": {
                            "conditions": [
                                {"type": "ACK.Recoverable", "status": "True", "message": "denied"}
                            ]
                        }
                    }
                ),
                "ACK.Recoverable=True denied",
            ),
        ],
    )
    def test_timeout_reports_the_last_conditions(self, monkeypatch, response, last: str) -> None:
        _install_clock(monkeypatch)
        kubectl = _ScriptedKubectl({("get", "queue.sqs.services.k8s.aws"): response})
        with pytest.raises(drivers.ExampleValidationError, match=f"within 30s: {last}"):
            drivers.wait_ack_resources_synced(self._parsed(), kubectl, timeout=30)

    def test_a_file_without_ack_kinds_is_a_spec_error(self) -> None:
        with pytest.raises(drivers.ExampleValidationError, match="has no ACK kind"):
            drivers.wait_ack_resources_synced(self._parsed(_job()), _ScriptedKubectl({}), timeout=5)


class TestDriverHelpers:
    def test_resource_refs(self) -> None:
        assert drivers._resource_ref(_instance()) == "batchjob.kro.run"
        assert drivers._resource_ref({"apiVersion": "v1", "kind": "ConfigMap"}) == "configmap"

    def test_conditions_ignore_malformed_entries(self) -> None:
        payload = {"status": {"conditions": ["junk", {"type": "Ready", "status": "True"}]}}
        assert drivers._conditions(payload) == [{"type": "Ready", "status": "True"}]
        assert drivers._condition_true(payload, "Ready")
        assert not drivers._condition_true({}, "Ready")
        assert drivers._condition_summary({}) == ""

    def test_wait_until_reports_the_hint_and_keeps_the_last_state(self, monkeypatch) -> None:
        _install_clock(monkeypatch)
        states = iter([(False, "first"), (False, "")])
        with pytest.raises(drivers.ExampleValidationError) as excinfo:
            drivers._wait_until(
                lambda: next(states), deadline=drivers._POLL_SECONDS, what="thing", hint="do x"
            )
        assert str(excinfo.value) == "timed out waiting for thing. Last state: first (do x)"
        with pytest.raises(drivers.ExampleValidationError) as excinfo:
            drivers._wait_until(lambda: (None, "s"), deadline=0, what="other")
        assert str(excinfo.value) == "timed out waiting for other. Last state: s"


# ─── cleanup waits for the Jobs an example creates indirectly ────────────────


class TestDerivedJobCleanup:
    def test_argocd_cleanup_waits_for_the_synced_jobs(self, monkeypatch, tmp_path: Path) -> None:
        clock = _install_clock(monkeypatch)
        repo = _repo_with_fixture(tmp_path, _job())
        parsed = static_checks.ParsedExample(
            name="argocd-gitops-job",
            path=repo / "examples" / "argocd-gitops-job.yaml",
            spec=EXAMPLE_SPECS["argocd-gitops-job"],
            documents=[_application()],
        )
        kubectl = _ScriptedKubectl(
            {
                ("delete",): (0, "application.argoproj.io/app deleted", ""),
                ("get", "job"): [(0, _JOB_COMPLETE, ""), _MISSING],
            }
        )
        result = drivers.cleanup_example(parsed, parsed.path, kubectl)
        assert result == {
            "deleted": ["application.argoproj.io/app deleted"],
            "derived_jobs_removed": ["gco-jobs/synced"],
        }
        assert clock.sleeps == [drivers._POLL_SECONDS]

    def test_composed_jobs_that_linger_fail_cleanup(self, monkeypatch) -> None:
        _install_clock(monkeypatch)
        parsed = _synthetic_parsed(
            "crossplane-batch-job", EXAMPLE_SPECS["crossplane-batch-job"], [_instance()]
        )
        kubectl = _ScriptedKubectl(
            {("delete",): (0, "deleted", ""), ("get", "job"): (0, _JOB_COMPLETE, "")}
        )
        with pytest.raises(drivers.ExampleValidationError, match=r"still present.*gco-jobs/hello"):
            drivers.cleanup_example(parsed, parsed.path, kubectl)

    def test_ordinary_examples_have_no_derived_jobs(self) -> None:
        parsed = static_checks.parse_example(REPO_ROOT, "ack-sqs-queue")
        assert drivers._derived_jobs(parsed) == []


# ─── setup drivers ───────────────────────────────────────────────────────────


class TestArgoCdReadiness:
    _available = {"status": {"conditions": [{"type": "Available", "status": "True"}]}}

    def test_ready(self) -> None:
        kubectl = _ScriptedKubectl(
            {
                ("get", "appproject"): _json({}),
                ("get", "statefulset"): _json({"status": {"readyReplicas": 1}}),
                ("get", "deployment"): _json(self._available),
            }
        )
        assert drivers.wait_argocd_ready(kubectl, timeout=5) == {
            "project": "argocd/gco-tenants",
            "application_controller_ready": 1,
            "repo_server": "Available",
        }

    @pytest.mark.parametrize(
        ("routes", "state"),
        [
            ({("get", "appproject"): _MISSING}, "AppProject argocd/gco-tenants not found"),
            (
                {("get", "appproject"): _json({}), ("get", "statefulset"): _MISSING},
                "has no ready replica",
            ),
            (
                {
                    ("get", "appproject"): _json({}),
                    ("get", "statefulset"): _json({"status": {"readyReplicas": 2}}),
                    ("get", "deployment"): _json({"status": {}}),
                },
                "argocd-repo-server is not Available",
            ),
        ],
    )
    def test_not_ready_is_actionable(self, monkeypatch, routes, state: str) -> None:
        _install_clock(monkeypatch)
        with pytest.raises(drivers.ExampleValidationError) as excinfo:
            drivers.wait_argocd_ready(_ScriptedKubectl(routes), timeout=0)
        assert "is helm.argocd enabled" in str(excinfo.value)
        assert state in str(excinfo.value)


class TestCompanionApi:
    def test_kro_companion_lifecycle(self, monkeypatch) -> None:
        clock = _install_clock(monkeypatch)
        kubectl = _ScriptedKubectl(
            {
                ("get", "crd", "resourcegraphdefinitions.kro.run"): [_MISSING, (0, "", "")],
                ("apply",): (0, "resourcegraphdefinition.kro.run/gco-batch-job created\n", ""),
                ("get", "resourcegraphdefinition"): [
                    _MISSING,
                    _json({"status": {"state": "Active"}}),
                ],
                ("get", "batchjob.kro.run", "-A"): [_MISSING, (0, "", "")],
                ("delete",): (0, "resourcegraphdefinition.kro.run/gco-batch-job deleted", ""),
            }
        )
        companion = drivers.CompanionApi(
            flavor="kro", path=REPO_ROOT / "examples" / "kro-batch-api.yaml", kubectl=kubectl
        )
        assert companion.destroy() == {"deleted": []}
        evidence = companion.create()
        assert evidence == {
            "prerequisites": ["resourcegraphdefinitions.kro.run"],
            "applied": ["resourcegraphdefinition.kro.run/gco-batch-job created"],
            "served": ["batchjob.kro.run"],
        }
        assert companion.applied
        assert len(clock.sleeps) == 3
        assert companion.destroy() == {
            "deleted": ["resourcegraphdefinition.kro.run/gco-batch-job deleted"]
        }
        assert not companion.applied
        (delete,) = kubectl.commands("delete")
        assert delete[:3] == ("delete", "-f", str(REPO_ROOT / "examples" / "kro-batch-api.yaml"))

    def test_crossplane_companion_waits_for_its_function(self) -> None:
        healthy = {"status": {"conditions": [{"type": "Healthy", "status": "True"}]}}
        established = {"status": {"conditions": [{"type": "Established", "status": "True"}]}}
        kubectl = _ScriptedKubectl(
            {
                ("get", "function.pkg.crossplane.io"): _json(healthy),
                ("apply",): (0, "created", ""),
                ("get", "compositeresourcedefinition.apiextensions.crossplane.io"): _json(
                    established
                ),
                ("get", "batchjob.examples.gco.io", "-A"): (0, "", ""),
            }
        )
        companion = drivers.CompanionApi(
            flavor="crossplane",
            path=REPO_ROOT / "examples" / "crossplane-batch-api.yaml",
            kubectl=kubectl,
        )
        evidence = companion.create()
        assert evidence["prerequisites"] == [
            "function/crossplane-contrib-function-go-templating=Healthy"
        ]
        assert evidence["served"] == ["batchjob.examples.gco.io"]

    def test_a_function_that_never_turns_healthy(self, monkeypatch) -> None:
        _install_clock(monkeypatch)
        kubectl = _ScriptedKubectl({("get", "function.pkg.crossplane.io"): _MISSING})
        companion = drivers.CompanionApi(
            flavor="crossplane",
            path=REPO_ROOT / "examples" / "crossplane-batch-api.yaml",
            kubectl=kubectl,
        )
        with pytest.raises(drivers.ExampleValidationError, match=r"is helm\.crossplane enabled"):
            companion.create(timeout=0)
        assert not companion.applied

    def test_rgd_that_never_activates_reports_its_state(self, monkeypatch) -> None:
        _install_clock(monkeypatch)
        kubectl = _ScriptedKubectl(
            {
                ("get", "crd"): (0, "", ""),
                ("apply",): (0, "created", ""),
                ("get", "resourcegraphdefinition"): _json(
                    {
                        "status": {
                            "state": "Inactive",
                            "conditions": [{"type": "Ready", "status": "False", "reason": "Bad"}],
                        }
                    }
                ),
            }
        )
        companion = drivers.CompanionApi(
            flavor="kro", path=REPO_ROOT / "examples" / "kro-batch-api.yaml", kubectl=kubectl
        )
        with pytest.raises(drivers.ExampleValidationError, match="state=Inactive Ready=False Bad"):
            companion.create(timeout=0)
        # Applied before the wait failed: the caller's cleanup must delete it.
        assert companion.applied

    def test_apply_and_delete_failures_are_reported(self, tmp_path: Path) -> None:
        other = tmp_path / "companion.yaml"
        other.write_text(
            yaml.safe_dump_all([{"kind": "ConfigMap", "metadata": {"name": "x"}}, "scalar"]),
            encoding="utf-8",
        )
        kubectl = _ScriptedKubectl(
            {("get", "crd"): (0, "", ""), ("apply",): (1, "", "error: invalid")}
        )
        companion = drivers.CompanionApi(flavor="kro", path=other, kubectl=kubectl)
        with pytest.raises(drivers.ExampleValidationError, match="apply -f examples/companion"):
            companion.create()
        kubectl.routes[("apply",)] = (0, "", "")
        kubectl.routes[("delete",)] = (1, "", "error: timed out")
        assert companion.create()["served"] == []
        with pytest.raises(drivers.ExampleValidationError, match="deleting companion"):
            companion.destroy()

    def test_unknown_flavors_are_refused(self) -> None:
        companion = drivers.CompanionApi(
            flavor="helm", path=REPO_ROOT, kubectl=_ScriptedKubectl({})
        )
        with pytest.raises(drivers.ExampleValidationError, match="unknown companion flavor"):
            companion.create()


class _QueueDoesNotExist(Exception):
    pass


class _AckSqs:
    exceptions = SimpleNamespace(QueueDoesNotExist=_QueueDoesNotExist)

    def __init__(self, answers: list[object]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    def get_queue_url(self, *, QueueName: str) -> dict[str, str]:
        self.calls.append(QueueName)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return {"QueueUrl": str(answer)}


class TestAckSqsQueues:
    _url = "https://sqs.us-east-1.amazonaws.com/111122223333/gco-ack-example"

    def _parsed(self) -> static_checks.ParsedExample:
        return static_checks.parse_example(REPO_ROOT, "ack-sqs-queue")

    def test_queue_names_come_from_the_example(self) -> None:
        parsed = dataclasses.replace(self._parsed(), documents=[_queue_doc(), _job()])
        assert drivers.AckSqsQueues.queue_names(parsed) == ["gco-ack-example"]

    def test_crd_wait(self, monkeypatch) -> None:
        _install_clock(monkeypatch)
        queues = drivers.AckSqsQueues(session=_FakeSession(), region="us-east-1")
        assert queues.wait_ready(_ScriptedKubectl({("get", "crd"): (0, "", "")})) == {
            "crd": "queues.sqs.services.k8s.aws"
        }
        with pytest.raises(
            drivers.ExampleValidationError, match=r"is eks_capabilities\.ack enabled"
        ):
            queues.wait_ready(_ScriptedKubectl({("get", "crd"): _MISSING}), timeout=0)

    def test_created_and_deleted_in_sqs(self, monkeypatch) -> None:
        clock = _install_clock(monkeypatch)
        sqs = _AckSqs([self._url])
        session = _FakeSession(sqs=sqs)
        queues = drivers.AckSqsQueues(session=session, region="us-east-1")
        assert queues.verify_created(self._parsed()) == {
            "sqs_queue_urls": {"gco-ack-example": self._url}
        }
        sqs.answers = [self._url, _QueueDoesNotExist()]
        assert queues.verify_deleted(self._parsed()) == {"sqs_queues_deleted": ["gco-ack-example"]}
        assert clock.sleeps == [drivers._POLL_SECONDS]
        assert session.requests == [("sqs", "us-east-1"), ("sqs", "us-east-1")]

    def test_missing_after_sync_and_surviving_after_cleanup_fail(self, monkeypatch) -> None:
        _install_clock(monkeypatch)
        queues = drivers.AckSqsQueues(
            session=_FakeSession(sqs=_AckSqs([_QueueDoesNotExist()])), region="us-east-1"
        )
        with pytest.raises(drivers.ExampleValidationError, match="SQS does not resolve it"):
            queues.verify_created(self._parsed())
        survivor = drivers.AckSqsQueues(
            session=_FakeSession(sqs=_AckSqs([self._url])), region="us-east-1"
        )
        with pytest.raises(drivers.ExampleValidationError, match="SQS still resolves"):
            survivor.verify_deleted(self._parsed(), timeout=0)


# ─── _run_one_example dispatch ───────────────────────────────────────────────


def _ctx(session: object = None) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(repo_root=REPO_ROOT, run_id="run-1", expected_sha=_SHA),
        session=session,
    )


class TestPinArgoCdRevision:
    def test_every_application_is_pinned_and_other_documents_kept(self, tmp_path, monkeypatch):
        monkeypatch.setattr(drivers.tempfile, "tempdir", str(tmp_path))
        source = tmp_path / "in.yaml"
        source.write_text(
            yaml.safe_dump_all([_application(), None, {"kind": "ConfigMap"}]), encoding="utf-8"
        )
        parsed = static_checks.parse_example(REPO_ROOT, "argocd-gitops-job")
        pinned = actions._pin_argocd_revision(parsed, _SHA, source)
        assert pinned.parent == tmp_path and pinned.name.endswith("-argocd-gitops-job.yaml")
        documents = list(yaml.safe_load_all(pinned.read_text(encoding="utf-8")))
        assert documents[0]["spec"]["source"]["targetRevision"] == _SHA
        assert documents[1] == {"kind": "ConfigMap"}


class TestRunOneExampleDispatch:
    def test_argocd_example_pins_the_revision_and_waits(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(drivers.tempfile, "tempdir", str(tmp_path))
        monkeypatch.setattr(drivers, "wait_argocd_ready", lambda kubectl: {"ready": True})
        seen: dict[str, Any] = {}

        def fake_waiter(parsed, kubectl, *, timeout):
            seen["timeout"] = timeout
            return {"applications": "healthy"}

        monkeypatch.setitem(drivers.CRITERIA_WAITERS, ARGOCD_APP_HEALTHY, fake_waiter)
        monkeypatch.setattr(
            drivers, "cleanup_example", lambda parsed, path, kubectl: {"deleted": [str(path)]}
        )
        kubectl = _ScriptedKubectl({("apply",): (0, "application created", "")})
        result = actions._run_one_example(_ctx(), "argocd-gitops-job", "us-east-1", kubectl)

        assert result.status == "passed", result.detail
        assert result.mutations == {
            "Application.spec.source.targetRevision": f"{_SHA} (the commit under validation)"
        }
        assert result.evidence["setup"] == {"ready": True}
        assert result.evidence["criteria"] == {"applications": "healthy"}
        (apply_call,) = kubectl.commands("apply")
        applied = Path(apply_call[2])
        assert applied.parent == tmp_path
        (application,) = list(yaml.safe_load_all(applied.read_text(encoding="utf-8")))
        assert application["spec"]["source"]["targetRevision"] == _SHA
        assert result.evidence["cleanup"] == {"deleted": [str(applied)]}
        assert seen["timeout"] == EXAMPLE_SPECS["argocd-gitops-job"].timeout_seconds

    @pytest.mark.parametrize(
        ("name", "flavor", "companion"),
        [
            ("kro-batch-job", "kro", "kro-batch-api"),
            ("crossplane-batch-job", "crossplane", "crossplane-batch-api"),
        ],
    )
    def test_instance_examples_bracket_the_run_with_their_companion(
        self, monkeypatch, name: str, flavor: str, companion: str
    ) -> None:
        events: list[str] = []

        class FakeCompanion:
            def __init__(self, *, flavor: str, path: Path, kubectl: Any) -> None:
                events.append(f"init:{flavor}:{path.name}")
                self.applied = False

            def create(self) -> dict[str, Any]:
                events.append("create")
                self.applied = True
                return {"served": ["api"]}

            def destroy(self) -> dict[str, Any]:
                events.append("destroy")
                self.applied = False
                return {"deleted": ["api"]}

        monkeypatch.setattr(drivers, "CompanionApi", FakeCompanion)
        monkeypatch.setitem(
            drivers.CRITERIA_WAITERS,
            COMPOSED_JOB_COMPLETES,
            lambda parsed, kubectl, *, timeout: events.append("wait") or {"composed": "ok"},
        )
        monkeypatch.setattr(
            drivers,
            "cleanup_example",
            lambda parsed, path, kubectl: events.append("cleanup") or {"deleted": []},
        )
        kubectl = _ScriptedKubectl({("apply",): (0, "created", "")})
        result = actions._run_one_example(_ctx(), name, "us-east-1", kubectl)

        assert result.status == "passed", result.detail
        assert events == [f"init:{flavor}:{companion}.yaml", "create", "wait", "cleanup", "destroy"]
        assert result.evidence["setup"] == {"served": ["api"]}
        assert result.evidence["companion_cleanup"] == {"deleted": ["api"]}

    def test_a_failed_instance_still_deletes_its_companion(self, monkeypatch) -> None:
        events: list[str] = []

        class FakeCompanion:
            def __init__(self, **_kwargs: Any) -> None:
                self.applied = False

            def create(self) -> dict[str, Any]:
                self.applied = True
                return {}

            def destroy(self) -> dict[str, Any]:
                events.append("destroy")
                raise drivers.ExampleValidationError("delete timed out")

        def failing_waiter(parsed, kubectl, *, timeout):
            raise drivers.ExampleValidationError("composed Job never ran")

        monkeypatch.setattr(drivers, "CompanionApi", FakeCompanion)
        monkeypatch.setitem(drivers.CRITERIA_WAITERS, COMPOSED_JOB_COMPLETES, failing_waiter)
        monkeypatch.setattr(
            drivers,
            "cleanup_example",
            lambda parsed, path, kubectl: events.append("cleanup") or {},
        )
        result = actions._run_one_example(
            _ctx(), "kro-batch-job", "us-east-1", _ScriptedKubectl({("apply",): (0, "", "")})
        )
        assert result.status == "failed"
        assert result.detail == "composed Job never ran"
        # Instance cleanup first, then the companion; its failure is swallowed.
        assert events == ["cleanup", "destroy"]

    def test_ack_example_proves_the_queue_in_sqs_both_ways(self, monkeypatch) -> None:
        events: list[str] = []

        class FakeQueues:
            def __init__(self, *, session: Any, region: str) -> None:
                assert (session, region) == ("session", "us-east-1")

            def wait_ready(self, kubectl: Any) -> dict[str, Any]:
                events.append("ready")
                return {"crd": "queues"}

            def verify_created(self, parsed: Any) -> dict[str, Any]:
                events.append("created")
                return {"sqs_queue_urls": {}}

            def verify_deleted(self, parsed: Any) -> dict[str, Any]:
                events.append("deleted")
                return {"sqs_queues_deleted": []}

        monkeypatch.setattr(drivers, "AckSqsQueues", FakeQueues)
        monkeypatch.setitem(
            drivers.CRITERIA_WAITERS,
            ACK_RESOURCE_SYNCED,
            lambda parsed, kubectl, *, timeout: events.append("synced") or {"ack": "ok"},
        )
        monkeypatch.setattr(
            drivers,
            "cleanup_example",
            lambda parsed, path, kubectl: events.append("cleanup") or {"deleted": []},
        )
        result = actions._run_one_example(
            _ctx("session"),
            "ack-sqs-queue",
            "us-east-1",
            _ScriptedKubectl({("apply",): (0, "queue created", "")}),
        )
        assert result.status == "passed", result.detail
        assert events == ["ready", "synced", "created", "cleanup", "deleted"]
        assert set(result.evidence) == {
            "setup",
            "submission",
            "criteria",
            "aws",
            "cleanup",
            "aws_cleanup",
        }
