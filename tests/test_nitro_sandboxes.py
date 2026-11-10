"""AWS Nitro Sandboxes: admission rules, the RuntimeClass check, and ``--sandbox``.

Every admission rule here is a documented Nitro Sandbox constraint that would
otherwise surface late (a pod accepted, then Pending forever or refused at
creation), so the REST service, the SQS processor, ``gco jobs check-policy`` and
the example validator all reject it at submission through one function in
``gco.job_admission``. The parity tests hold the REST and SQS paths to the same
verdict, and the CLI tests pin that ``--sandbox`` reaches every transport and
fails before any request when the result could not run in a sandbox.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner
from kubernetes.client.rest import ApiException

from gco import job_admission as ja
from gco.services.manifest_processor import RuntimeClassPresence


def _container(**resources: Any) -> dict[str, Any]:
    container: dict[str, Any] = {
        "name": "main",
        "image": "public.ecr.aws/amazonlinux/amazonlinux:2023",
    }
    if resources:
        container["resources"] = resources
    return container


def _job(runtime_class: str | None = ja.ISOLATED_SANDBOX, **pod: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {"containers": [_container(limits={"cpu": "1", "memory": "1Gi"})], **pod}
    if runtime_class is not None:
        spec["runtimeClassName"] = runtime_class
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "sandboxed", "namespace": "gco-jobs"},
        "spec": {"template": {"metadata": {}, "spec": spec}},
    }


def _pod(
    runtime_class: str = ja.ISOLATED_SANDBOX, annotations: dict[str, str] | None = None
) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "p", "annotations": annotations or {}},
        "spec": {"runtimeClassName": runtime_class, "containers": [_container()]},
    }


def _confidential_job() -> dict[str, Any]:
    """A confidential Job that satisfies every rule: policy and token mount set."""
    manifest = _job(runtime_class=ja.CONFIDENTIAL_SANDBOX, automountServiceAccountToken=False)
    manifest["spec"]["template"]["metadata"] = {
        "annotations": {ja.SANDBOX_POLICY_ANNOTATION: "base64"}
    }
    return manifest


def _api_exception(status: int) -> ApiException:
    return ApiException(status=status, reason="test")


class TestKindCoverage:
    def test_every_allowed_kind_has_a_sandbox_treatment(self) -> None:
        """A new allowed kind must say whether --sandbox sets, skips or refuses it."""
        template = set(ja.SANDBOX_TEMPLATE_KINDS)
        podless = set(ja.PODLESS_KINDS)
        refused = {"TrainJob"}
        assert template.isdisjoint(podless) and template.isdisjoint(refused)
        assert podless.isdisjoint(refused)
        assert template | podless | refused == set(ja.DEFAULT_ALLOWED_KINDS)

    def test_modes_select_sandbox_runtime_classes(self) -> None:
        assert ja.SANDBOX_MODES == {"isolated": ja.ISOLATED_SANDBOX}
        assert set(ja.SANDBOX_MODES.values()) <= set(ja.SANDBOX_RUNTIME_CLASSES)


class TestPodTemplate:
    @pytest.mark.parametrize("kind", ["Job", "Deployment", "StatefulSet", "DaemonSet"])
    def test_workloads_return_the_template(self, kind: str) -> None:
        manifest = _job()
        manifest["kind"] = kind
        metadata, spec = ja.pod_template(manifest)
        assert metadata is manifest["spec"]["template"]["metadata"]
        assert spec is manifest["spec"]["template"]["spec"]

    def test_cronjob_and_pod(self) -> None:
        cron = {
            "kind": "CronJob",
            "spec": {
                "jobTemplate": {
                    "spec": {"template": {"metadata": {"labels": {}}, "spec": {"containers": []}}}
                }
            },
        }
        assert ja.pod_template(cron) == ({"labels": {}}, {"containers": []})
        pod = _pod()
        assert ja.pod_template(pod) == (pod["metadata"], pod["spec"])

    @pytest.mark.parametrize(
        "manifest",
        [
            {"kind": "TrainJob", "spec": {}},
            {"kind": "ReplicaSet", "spec": {"template": {"spec": {}}}},
            {"kind": "Job", "spec": "junk"},
            {"kind": "Job", "spec": {"template": "junk"}},
            {"kind": "CronJob", "spec": {"jobTemplate": {"spec": "junk"}}},
            {"kind": "Pod", "metadata": "junk", "spec": {"containers": []}},
            {"kind": "Job", "spec": {"template": {"metadata": "junk", "spec": "junk"}}},
        ],
    )
    def test_malformed_or_unsupported_shapes(self, manifest: dict[str, Any]) -> None:
        metadata, spec = ja.pod_template(manifest)
        if manifest["kind"] == "Pod":
            assert metadata is None and spec == {"containers": []}
        else:
            assert spec is None and metadata is None


class TestCheckSandbox:
    def test_unsandboxed_and_plain_isolated_workloads_pass(self) -> None:
        assert ja.check_sandbox(_job(runtime_class=None)) == (True, None)
        assert ja.check_sandbox(_job(runtime_class="nvidia")) == (True, None)
        assert ja.check_sandbox(_job()) == (True, None)
        assert ja.sandbox_runtime_class(_job()) == ja.ISOLATED_SANDBOX
        assert ja.sandbox_runtime_class(_job(runtime_class=None)) is None

    @pytest.mark.parametrize(
        ("resource", "section"),
        [
            ("nvidia.com/gpu", "limits"),
            ("aws.amazon.com/neuron", "requests"),
            ("vpc.amazonaws.com/efa", "limits"),
        ],
    )
    def test_device_requests_are_rejected(self, resource: str, section: str) -> None:
        manifest = _job()
        manifest["spec"]["template"]["spec"]["containers"][0]["resources"] = {
            section: {resource: 1}
        }
        ok, message = ja.check_sandbox(manifest)
        assert not ok
        assert message == (
            f"isolated-sandbox: Nitro Sandboxes have no GPU or device passthrough, so a sandboxed pod "
            f"cannot request {resource}; run this workload without a sandbox runtime class"
        )

    def test_host_network_and_user_namespaces_are_rejected(self) -> None:
        ok, message = ja.check_sandbox(_job(hostNetwork=True))
        assert not ok and "cannot set hostNetwork: true" in str(message)
        ok, message = ja.check_sandbox(_job(hostUsers=False))
        assert not ok and "hostUsers: false (user namespaces) is not supported" in str(message)
        assert ja.check_sandbox(_job(hostNetwork=False, hostUsers=True)) == (True, None)

    @pytest.mark.parametrize(
        ("kind", "where"),
        [
            ("Job", "spec.template.metadata.annotations"),
            ("CronJob", "spec.jobTemplate.spec.template.metadata.annotations"),
        ],
    )
    def test_annotations_on_the_workload_metadata_are_rejected(self, kind: str, where: str) -> None:
        manifest = _job()
        if kind == "CronJob":
            manifest = {
                "kind": "CronJob",
                "metadata": {"name": "c"},
                "spec": {"jobTemplate": {"spec": copy.deepcopy(manifest["spec"])}},
            }
        manifest["metadata"]["annotations"] = {
            "sandbox.amazonaws.com/network-isolated": "true",
            "other": "x",
        }
        ok, message = ja.check_sandbox(manifest)
        assert not ok
        assert message == (
            f"sandbox.amazonaws.com/network-isolated on the {kind}'s own metadata has no effect; "
            f"Kubernetes takes pod annotations from the pod template, so put it under {where}"
        )

    def test_a_bare_pod_carries_its_own_annotations(self) -> None:
        pod = _pod(annotations={"sandbox.amazonaws.com/network-isolated": "true"})
        assert ja.check_sandbox(pod) == (True, None)

    def test_confidential_needs_the_policy_on_the_pod_template(self) -> None:
        manifest = _job(runtime_class=ja.CONFIDENTIAL_SANDBOX, automountServiceAccountToken=False)
        ok, message = ja.check_sandbox(manifest)
        assert not ok
        assert "need a sandbox.amazonaws.com/policy annotation on the pod template" in str(message)
        assert "eks-nitro-sandbox-genpolicy --profile confidential" in str(message)
        assert ja.check_sandbox(_confidential_job()) == (True, None)
        manifest["spec"]["template"]["metadata"] = None
        assert not ja.check_sandbox(manifest)[0]
        pod = _pod(ja.CONFIDENTIAL_SANDBOX, {ja.SANDBOX_POLICY_ANNOTATION: "p"})
        pod["spec"]["automountServiceAccountToken"] = True
        assert ja.check_sandbox(pod) == (True, None)

    def test_confidential_must_pin_the_token_mount_itself(self) -> None:
        """GCO's automountServiceAccountToken default would change a pinned spec."""
        manifest = _confidential_job()
        del manifest["spec"]["template"]["spec"]["automountServiceAccountToken"]
        ok, message = ja.check_sandbox(manifest)
        assert not ok
        assert message == (
            "confidential-sandbox pods must set automountServiceAccountToken (true or false) "
            "before the policy is generated: GCO sets false when it is missing, and the policy "
            "pins the token mount, so the sandbox would deny the pod"
        )

    def test_trainjob_views(self) -> None:
        def trainjob(runtime_class: str, **resources: Any) -> dict[str, Any]:
            patch_spec = {
                "runtimeClassName": runtime_class,
                "containers": [_container(**resources)],
            }
            return {
                "kind": "TrainJob",
                "metadata": {"name": "t"},
                "spec": {
                    "runtimePatches": [{"trainingRuntimeSpec": {"template": {"spec": patch_spec}}}]
                },
            }

        assert ja.check_sandbox(trainjob(ja.ISOLATED_SANDBOX)) == (True, None)
        assert ja.sandbox_runtime_class(trainjob(ja.ISOLATED_SANDBOX)) == ja.ISOLATED_SANDBOX
        ok, message = ja.check_sandbox(trainjob(ja.ISOLATED_SANDBOX, limits={"nvidia.com/gpu": 8}))
        assert not ok and "nvidia.com/gpu" in str(message)
        ok, message = ja.check_sandbox(trainjob(ja.CONFIDENTIAL_SANDBOX))
        assert message == (
            "confidential-sandbox needs a Cedar policy generated from the final pod spec, which "
            "a TrainJob's runtime builds when it runs; submit the workload as a Pod, Job, "
            "CronJob, Deployment, StatefulSet or DaemonSet"
        )
        assert not ok

    def test_metadata_that_is_not_a_mapping(self) -> None:
        manifest = _job()
        manifest["metadata"] = "junk"
        assert ja.check_sandbox(manifest) == (True, None)


class TestCheckPolicyReportsSandboxRules:
    def test_a_sandbox_violation_is_its_own_check(self) -> None:
        """``gco jobs check-policy`` names the sandbox rule rather than a generic failure."""
        from cli.job_policy import CHECK_SANDBOX, evaluate_manifest

        policy = ja.JobValidationPolicy.from_cdk_context({})
        assert evaluate_manifest(_job(), policy) == []
        (issue,) = evaluate_manifest(_job(hostUsers=False), policy)
        assert issue.check == CHECK_SANDBOX == "sandbox"
        assert "hostUsers: false (user namespaces) is not supported" in issue.message


class TestApplySandboxRuntimeClass:
    def test_sets_the_pod_template_runtime_class(self) -> None:
        manifest = _job(runtime_class=None)
        ja.apply_sandbox_runtime_class(manifest, ja.ISOLATED_SANDBOX)
        assert manifest["spec"]["template"]["spec"]["runtimeClassName"] == "isolated-sandbox"
        # Idempotent, and accepts a manifest that already says the same.
        ja.apply_sandbox_runtime_class(manifest, ja.ISOLATED_SANDBOX)

    def test_refuses_a_conflicting_runtime_class(self) -> None:
        with pytest.raises(ValueError, match="already sets runtimeClassName: nvidia"):
            ja.apply_sandbox_runtime_class(_job(runtime_class="nvidia"), ja.ISOLATED_SANDBOX)

    def test_refuses_kinds_without_a_pod_template(self) -> None:
        with pytest.raises(
            ValueError,
            match="TrainJob t: --sandbox needs a Pod, Job, CronJob, Deployment, StatefulSet or "
            "DaemonSet, whose pod template GCO can set",
        ):
            ja.apply_sandbox_runtime_class(
                {"kind": "TrainJob", "metadata": {"name": "t"}, "spec": {}}, ja.ISOLATED_SANDBOX
            )
        with pytest.raises(ValueError, match="TrainJob \\?: --sandbox needs a Pod"):
            ja.apply_sandbox_runtime_class({"kind": "TrainJob", "spec": {}}, ja.ISOLATED_SANDBOX)
        with pytest.raises(ValueError, match="TrainJob \\?: --sandbox needs a Pod"):
            ja.apply_sandbox_runtime_class(
                {"kind": "TrainJob", "metadata": "junk", "spec": {}}, ja.ISOLATED_SANDBOX
            )

    def test_refuses_an_unknown_runtime_class(self) -> None:
        with pytest.raises(ValueError, match="unknown sandbox runtime class 'kata'"):
            ja.apply_sandbox_runtime_class(_job(), "kata")


class TestApplySandbox:
    def test_sandboxes_every_pod_and_passes_podless_kinds_through(self) -> None:
        config_map = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c"}}
        service = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "s"}, "spec": {}}
        job, pod = _job(runtime_class=None), _pod()
        del pod["spec"]["runtimeClassName"]
        manifests = [config_map, job, service, pod]
        before = copy.deepcopy([config_map, service])
        ja.apply_sandbox(manifests, "isolated")
        assert job["spec"]["template"]["spec"]["runtimeClassName"] == ja.ISOLATED_SANDBOX
        assert pod["spec"]["runtimeClassName"] == ja.ISOLATED_SANDBOX
        assert [config_map, service] == before

    def test_an_unknown_mode_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown --sandbox mode 'confidential'; choose from"):
            ja.apply_sandbox([_job(runtime_class=None)], "confidential")

    def test_a_submission_with_no_pods_is_refused(self) -> None:
        with pytest.raises(
            ValueError,
            match="--sandbox found nothing to run in a sandbox: none of the manifests is a Pod, "
            "Job, CronJob, Deployment, StatefulSet or DaemonSet",
        ):
            ja.apply_sandbox([{"kind": "ConfigMap", "metadata": {"name": "c"}}], "isolated")

    def test_a_trainjob_is_refused_rather_than_left_outside(self) -> None:
        with pytest.raises(ValueError, match="TrainJob t: --sandbox needs a Pod"):
            ja.apply_sandbox([{"kind": "TrainJob", "metadata": {"name": "t"}}], "isolated")

    def test_a_result_that_could_not_run_in_a_sandbox_is_refused(self) -> None:
        manifest = _job(runtime_class=None)
        manifest["spec"]["template"]["spec"]["containers"][0]["resources"] = {
            "limits": {"nvidia.com/gpu": 1}
        }
        with pytest.raises(ValueError, match="no GPU or device passthrough"):
            ja.apply_sandbox([manifest], "isolated")


class TestCheckSandboxRuntimeClass:
    def test_an_unsandboxed_manifest_never_asks_the_cluster(self) -> None:
        lookup = MagicMock()
        assert ja.check_sandbox_runtime_class(_job(runtime_class=None), lookup) == (True, None)
        lookup.assert_not_called()

    def test_present_and_missing(self) -> None:
        assert ja.check_sandbox_runtime_class(_job(), lambda name: True) == (True, None)
        ok, message = ja.check_sandbox_runtime_class(_job(), lambda name: False)
        assert not ok
        assert message == (
            "RuntimeClass isolated-sandbox does not exist on this cluster, so its pods would be "
            "refused at creation: AWS Nitro Sandboxes need EKS 1.36 or later in a Region where "
            "EKS offers them (docs/NITRO_SANDBOXES.md)"
        )


class TestRuntimeClassPresence:
    def test_found_is_cached_until_the_ttl_passes(self) -> None:
        now = [100.0]
        read = MagicMock()
        presence = RuntimeClassPresence(read, ttl_seconds=60, clock=lambda: now[0])
        assert presence("isolated-sandbox") is True
        now[0] += 59
        assert presence("isolated-sandbox") is True
        assert read.call_count == 1
        now[0] += 1
        assert presence("isolated-sandbox") is True
        assert read.call_count == 2

    def test_a_404_means_missing_and_is_cached(self) -> None:
        read = MagicMock(side_effect=_api_exception(404))
        presence = RuntimeClassPresence(read, clock=lambda: 0.0)
        assert presence("isolated-sandbox") is False
        assert presence("isolated-sandbox") is False
        read.assert_called_once_with("isolated-sandbox")

    @pytest.mark.parametrize(
        ("error", "logged"),
        [(_api_exception(403), "HTTP 403"), (TimeoutError("read timed out"), "read timed out")],
    )
    def test_being_unable_to_ask_reads_as_present_and_is_not_cached(
        self, error: Exception, logged: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        read = MagicMock(side_effect=error)
        presence = RuntimeClassPresence(read, clock=lambda: 0.0)
        with caplog.at_level(logging.WARNING, logger="gco.services.manifest_processor"):
            assert presence("isolated-sandbox") is True
            assert presence("isolated-sandbox") is True
        assert read.call_count == 2
        assert logged in caplog.text and "not checking it" in caplog.text


class TestServiceWiring:
    def test_the_rest_processor_reads_runtime_classes_with_its_timeout(self) -> None:
        from gco.services.manifest_processor import ManifestProcessor

        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor("c", "us-east-1", {"validation_enabled": True})
        processor.node_v1 = MagicMock()
        assert processor.runtime_class_exists("isolated-sandbox") is True
        processor.node_v1.read_runtime_class.assert_called_once_with(
            "isolated-sandbox", _request_timeout=processor._k8s_timeout
        )

    def test_the_sqs_processor_builds_its_client_per_lookup(self) -> None:
        from gco.services import queue_processor

        with patch("gco.services.queue_processor.client.NodeV1Api") as node_v1:
            queue_processor.runtime_class_exists._read("isolated-sandbox")
        node_v1.return_value.read_runtime_class.assert_called_once_with("isolated-sandbox")


class TestKueueCoverage:
    def test_the_default_cluster_queue_covers_the_sandbox_slot(self) -> None:
        """Kueue adds RuntimeClass overhead to a workload's requests.

        Each sandbox pod's overhead includes one eks.amazonaws.com/nitro-sandbox,
        and a ClusterQueue that does not cover a requested resource never admits
        the workload: a queue-labeled sandboxed Job would stay suspended.
        """
        path = (
            Path(__file__).resolve().parents[1]
            / "lambda/kubectl-applier-simple/manifests/post-helm-kueue-default-queues.yaml"
        )
        documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        queue = next(doc for doc in documents if doc["kind"] == "ClusterQueue")
        (group,) = queue["spec"]["resourceGroups"]
        (flavor,) = group["flavors"]
        quotas = {resource["name"]: resource["nominalQuota"] for resource in flavor["resources"]}
        assert ja.SANDBOX_SLOT_RESOURCE in group["coveredResources"]
        assert set(quotas) == set(group["coveredResources"])
        assert quotas[ja.SANDBOX_SLOT_RESOURCE] == "{{QUOTA_MAX_PODS}}"


class TestSubmissionPathParity:
    """The REST service and the SQS processor give a sandboxed manifest the same verdict."""

    CASES = [
        (_job(), True, True),
        (_confidential_job(), True, True),
        (_job(hostUsers=False), True, False),
        (_job(runtime_class=ja.CONFIDENTIAL_SANDBOX), True, False),
        (_job(), False, False),
        (_job(runtime_class=None), False, True),
    ]

    @pytest.mark.parametrize(("manifest", "runtime_class_present", "expected"), CASES)
    def test_rest_and_sqs_agree(
        self,
        manifest: dict[str, Any],
        runtime_class_present: bool,
        expected: bool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gco.services import queue_processor
        from gco.services.manifest_processor import ManifestProcessor

        processor = ManifestProcessor.__new__(ManifestProcessor)
        processor.validation_enabled = True
        processor.yaml_max_depth = 50
        processor.allowed_namespaces = {"gco-jobs"}
        processor.allowed_kinds = set(ja.DEFAULT_ALLOWED_KINDS)
        processor.require_accelerator_toleration = False
        processor._validate_resource_limits = lambda m: (True, "")  # type: ignore[method-assign]
        processor._validate_security_context = lambda m: (True, None)  # type: ignore[method-assign]
        processor._validate_image_sources = lambda m: (True, None)  # type: ignore[method-assign]
        processor.runtime_class_exists = lambda name: runtime_class_present
        monkeypatch.setattr(
            queue_processor, "runtime_class_exists", lambda name: runtime_class_present
        )
        rest_ok, rest_message = processor.validate_manifest(copy.deepcopy(manifest))
        sqs_ok, sqs_message = queue_processor.validate_manifest(copy.deepcopy(manifest))
        assert rest_ok is sqs_ok is expected
        if not expected:
            assert str(rest_message).startswith("Nitro Sandbox validation failed: ")
            assert rest_message == sqs_message


# ---------------------------------------------------------------------------
# --sandbox on the CLI
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, *documents: dict[str, Any]) -> str:
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump_all(documents), encoding="utf-8")
    return str(path)


def _job_manager() -> Any:
    from cli.jobs import JobManager

    manager = JobManager.__new__(JobManager)
    manager.config = MagicMock(default_namespace="gco-jobs")
    manager._aws_client = MagicMock()
    return manager


def _gpu_job() -> dict[str, Any]:
    manifest = _job(runtime_class=None)
    manifest["spec"]["template"]["spec"]["containers"][0]["resources"] = {
        "limits": {"nvidia.com/gpu": 1}
    }
    return manifest


class TestJobManagerSandbox:
    def test_api_submission_sends_the_sandboxed_manifest(self, tmp_path: Path) -> None:
        manager = _job_manager()
        manager.submit_job(_write(tmp_path, _job(runtime_class=None)), sandbox="isolated")
        sent = manager._aws_client.submit_manifests.call_args.kwargs["manifests"]
        assert sent[0]["spec"]["template"]["spec"]["runtimeClassName"] == ja.ISOLATED_SANDBOX

    def test_sqs_submission_queues_the_sandboxed_manifest(self, tmp_path: Path) -> None:
        manager = _job_manager()
        manager._aws_client.get_regional_stack.return_value = MagicMock(stack_name="gco-us-east-1")
        cloudformation, sqs = MagicMock(), MagicMock()
        cloudformation.describe_stacks.return_value = {
            "Stacks": [{"Outputs": [{"OutputKey": "JobQueueUrl", "OutputValue": "https://q"}]}]
        }
        sqs.send_message.return_value = {"MessageId": "m-1"}
        with patch(
            "boto3.client", side_effect=lambda name, **_: {"sqs": sqs}.get(name, cloudformation)
        ):
            manager.submit_job_sqs(
                _write(tmp_path, _job(runtime_class=None)), region="us-east-1", sandbox="isolated"
            )
        body = json.loads(sqs.send_message.call_args.kwargs["MessageBody"])
        pod_spec = body["manifests"][0]["spec"]["template"]["spec"]
        assert pod_spec["runtimeClassName"] == ja.ISOLATED_SANDBOX

    @pytest.mark.parametrize("method", ["submit_job", "submit_job_direct", "submit_job_sqs"])
    def test_every_transport_refuses_before_any_request(self, method: str, tmp_path: Path) -> None:
        manager = _job_manager()
        kwargs: dict[str, Any] = {"sandbox": "isolated"}
        if method != "submit_job":
            kwargs["region"] = "us-east-1"
        with pytest.raises(ValueError, match="no GPU or device passthrough"):
            getattr(manager, method)(_write(tmp_path, _gpu_job()), **kwargs)
        assert manager._aws_client.mock_calls == []


class TestSandboxOption:
    @pytest.mark.parametrize(
        ("command", "method", "extra"),
        [
            ("submit", "submit_job", []),
            ("submit-direct", "submit_job_direct", ["-r", "us-east-1"]),
            ("submit-sqs", "submit_job_sqs", ["-r", "us-east-1"]),
        ],
    )
    def test_reaches_the_job_manager(
        self, command: str, method: str, extra: list[str], tmp_path: Path
    ) -> None:
        from cli.main import cli

        manifest_path = _write(tmp_path, _job(runtime_class=None))
        with patch("cli.commands.jobs_cmd.get_job_manager") as get_manager:
            getattr(get_manager.return_value, method).return_value = {}
            result = CliRunner().invoke(
                cli, ["jobs", command, manifest_path, "--sandbox", "isolated", *extra]
            )
        assert result.exit_code == 0, result.output
        assert getattr(get_manager.return_value, method).call_args.kwargs["sandbox"] == "isolated"

    def test_only_isolated_is_offered(self, tmp_path: Path) -> None:
        from cli.main import cli

        result = CliRunner().invoke(
            cli, ["jobs", "submit", _write(tmp_path, _job()), "--sandbox", "confidential"]
        )
        assert result.exit_code == 2
        assert "'confidential' is not 'isolated'" in result.output

    def test_submit_queue_sandboxes_the_posted_manifest(self, tmp_path: Path) -> None:
        from cli.main import cli

        manifest_path = _write(tmp_path, _job(runtime_class=None))
        with patch("cli.aws_client.get_aws_client") as get_client:
            get_client.return_value.call_api.return_value = {"job": {"job_id": "j-1"}}
            result = CliRunner().invoke(
                cli,
                ["jobs", "submit-queue", manifest_path, "-r", "us-east-1", "--sandbox", "isolated"],
            )
        assert result.exit_code == 0, result.output
        posted = get_client.return_value.call_api.call_args.kwargs["body"]["manifest"]
        assert posted["spec"]["template"]["spec"]["runtimeClassName"] == ja.ISOLATED_SANDBOX

    def test_submit_queue_refuses_what_could_not_run(self, tmp_path: Path) -> None:
        from cli.main import cli

        with patch("cli.aws_client.get_aws_client") as get_client:
            result = CliRunner().invoke(
                cli,
                [
                    "jobs",
                    "submit-queue",
                    _write(tmp_path, _gpu_job()),
                    "-r",
                    "us-east-1",
                    "--sandbox",
                    "isolated",
                ],
            )
        assert result.exit_code == 1
        assert "no GPU or device passthrough" in result.output
        get_client.return_value.call_api.assert_not_called()

    def test_the_policy_pre_check_judges_the_sandboxed_manifest(self, tmp_path: Path) -> None:
        from cli.main import cli

        manifest_path = _write(tmp_path, _job(runtime_class=None))
        with (
            patch("cli.commands.jobs_cmd.get_job_manager") as get_manager,
            patch("cli.job_policy.fetch_region_policies", return_value=[]),
            patch("cli.job_policy.region_verdicts", return_value=[]) as verdicts,
        ):
            manager = get_manager.return_value
            manager.load_manifests.return_value = [_job(runtime_class=None)]
            manager.submit_job.return_value = {}
            result = CliRunner().invoke(
                cli,
                [
                    "jobs",
                    "submit",
                    manifest_path,
                    "-r",
                    "us-east-1",
                    "--check-policy",
                    "--sandbox",
                    "isolated",
                ],
            )
        assert result.exit_code == 0, result.output
        judged = verdicts.call_args.args[0]
        assert judged[0]["spec"]["template"]["spec"]["runtimeClassName"] == ja.ISOLATED_SANDBOX
