"""The pure admission checks and the policy object they read.

The load-bearing test here is
:meth:`TestPolicyDocumentRoundTrip.test_document_round_trips_to_an_identical_policy`.
``GET /api/v1/policy`` exists so a caller can pre-check a manifest without
submitting it, and that promise only holds if the document the endpoint emits
reconstructs the policy the cluster actually enforces. Those are two separate
code paths over the same attributes, so they can drift -- add a cap to the
validator, forget it in the document, and every remote pre-check silently stops
checking it while still reporting success. The round-trip pins them together.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gco.job_admission import (
    DEFAULT_ALLOWED_KINDS,
    DEFAULT_TRUSTED_DOCKERHUB_ORGS,
    DEFAULT_TRUSTED_REGISTRIES,
    JobValidationPolicy,
    check_resource_caps,
    check_security_context,
    check_tolerations,
    hyperpod_routing,
    parse_cpu_millicores,
    parse_memory_bytes,
    weighted_pod_specs,
)


@pytest.fixture
def processor():
    """A ManifestProcessor with its Kubernetes clients stubbed out."""
    from gco.services.manifest_processor import ManifestProcessor

    with (
        patch("gco.services.manifest_processor.config.load_incluster_config"),
        patch("gco.services.manifest_processor.client.ApiClient", MagicMock()),
        patch("gco.services.manifest_processor.client.CoreV1Api", MagicMock()),
        patch("gco.services.manifest_processor.client.AppsV1Api", MagicMock()),
        patch("gco.services.manifest_processor.client.BatchV1Api", MagicMock()),
        patch("gco.services.manifest_processor.client.NetworkingV1Api", MagicMock()),
        patch("gco.services.manifest_processor.client.CustomObjectsApi", MagicMock()),
    ):
        yield ManifestProcessor("test-cluster", "us-east-1", {})


def _gpu_job(gpus: int = 1, *, tolerations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    pod_spec: dict[str, Any] = {
        "containers": [
            {
                "name": "trainer",
                "image": "docker.io/pytorch/pytorch:latest",
                "resources": {"limits": {"nvidia.com/gpu": str(gpus)}},
            }
        ]
    }
    if tolerations is not None:
        pod_spec["tolerations"] = tolerations
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "j", "namespace": "gco-jobs"},
        "spec": {"template": {"spec": pod_spec}},
    }


class TestPolicyDocumentRoundTrip:
    def test_document_round_trips_to_an_identical_policy(self, processor) -> None:
        """The endpoint's document must reconstruct what the validator enforces."""
        document = processor.effective_job_validation_policy()
        rebuilt = JobValidationPolicy.from_policy_document(document)

        assert rebuilt == processor.job_validation_policy()

    def test_round_trip_survives_non_default_configuration(self) -> None:
        """Defaults agreeing proves little; a customized deployment proves more."""
        from gco.services.manifest_processor import ManifestProcessor

        config = {
            "max_cpu_per_manifest": "12",
            "max_memory_per_manifest": "48Gi",
            "max_gpu_per_manifest": 3,
            "allowed_namespaces": ["gco-jobs", "team-b"],
            "allowed_kinds": ["Job", "Pod"],
            "trusted_registries": ["docker.io", "123456789012.dkr.ecr.us-east-1.amazonaws.com"],
            "trusted_dockerhub_orgs": ["pytorch"],
            "require_accelerator_toleration": False,
            "yaml_max_depth": 25,
            "manifest_security_policy": {"block_run_as_root": True, "block_host_ipc": False},
        }
        with (
            patch("gco.services.manifest_processor.config.load_incluster_config"),
            patch("gco.services.manifest_processor.client.ApiClient", MagicMock()),
            patch("gco.services.manifest_processor.client.CoreV1Api", MagicMock()),
            patch("gco.services.manifest_processor.client.AppsV1Api", MagicMock()),
            patch("gco.services.manifest_processor.client.BatchV1Api", MagicMock()),
            patch("gco.services.manifest_processor.client.NetworkingV1Api", MagicMock()),
            patch("gco.services.manifest_processor.client.CustomObjectsApi", MagicMock()),
        ):
            proc = ManifestProcessor("c", "us-east-2", config)

        rebuilt = JobValidationPolicy.from_policy_document(proc.effective_job_validation_policy())
        assert rebuilt == proc.job_validation_policy()
        assert rebuilt.max_cpu_millicores == 12000
        assert rebuilt.max_memory_bytes == 48 * 1024**3
        assert rebuilt.security["block_run_as_root"] is True
        assert rebuilt.security["block_host_ipc"] is False

    def test_every_security_toggle_survives_the_round_trip(self, processor) -> None:
        """A toggle the document omits would silently read as its default."""
        policy = processor.job_validation_policy()
        rebuilt = JobValidationPolicy.from_policy_document(
            processor.effective_job_validation_policy()
        )
        assert rebuilt.security == policy.security
        assert len(rebuilt.security) == 8


class TestDelegationIsFaithful:
    """The instance methods must be pure pass-throughs after the extraction."""

    @pytest.mark.parametrize(
        "manifest",
        [
            _gpu_job(1, tolerations=[{"key": "nvidia.com/gpu", "operator": "Exists"}]),
            _gpu_job(1),
            _gpu_job(999),
        ],
        ids=["tolerated", "missing-toleration", "over-cap"],
    )
    def test_methods_and_functions_agree(self, processor, manifest) -> None:
        policy = processor.job_validation_policy()

        assert processor._validate_resource_limits(manifest) == check_resource_caps(
            manifest, policy
        )
        assert processor._validate_security_context(manifest) == check_security_context(
            manifest, policy
        )
        assert processor._validate_tolerations(manifest) == check_tolerations(manifest)

    def test_policy_reflects_attributes_mutated_after_construction(self, processor) -> None:
        """The policy is rebuilt per call, so a late attribute change is honored.

        Tests reach in and set these caps directly; a snapshot taken in
        __init__ would keep enforcing the old value while
        effective_job_validation_policy() reported the new one.
        """
        processor.max_gpu_per_manifest = 0
        ok, message = processor._validate_resource_limits(_gpu_job(1))
        assert ok is False
        assert "GPU 1 exceeds max 0" in message


class TestCdkContextSource:
    def test_absent_keys_fall_back_to_shipped_defaults(self) -> None:
        policy = JobValidationPolicy.from_cdk_context({})

        assert policy.allowed_kinds == frozenset(DEFAULT_ALLOWED_KINDS)
        assert policy.trusted_registries == tuple(sorted(DEFAULT_TRUSTED_REGISTRIES))
        assert policy.trusted_dockerhub_orgs == tuple(sorted(DEFAULT_TRUSTED_DOCKERHUB_ORGS))
        assert policy.allowed_namespaces == frozenset({"gco-jobs"})
        assert policy.require_accelerator_toleration is True
        assert policy.max_gpu_count > 0, "a zero cap would reject every GPU job"

    def test_string_caps_are_parsed_into_the_validator_units(self) -> None:
        policy = JobValidationPolicy.from_cdk_context(
            {"max_cpu_per_manifest": "384", "max_memory_per_manifest": "1536Gi"}
        )
        assert policy.max_cpu_millicores == 384_000
        assert policy.max_memory_bytes == 1536 * 1024**3

    def test_cdk_context_can_be_stricter_than_the_deployment(self) -> None:
        """Documents the known gap: CDK adds project ECR hosts at synth time.

        A region deployed from this file trusts registries the file never
        mentions, so a cdk.json-sourced rejection on image provenance can be a
        false positive. That is why the offline validator reports advisories.
        """
        configured = JobValidationPolicy.from_cdk_context({"trusted_registries": ["docker.io"]})
        manifest = _gpu_job()
        manifest["spec"]["template"]["spec"]["containers"][0]["image"] = (
            "760425982254.dkr.ecr.us-east-1.amazonaws.com/gco/trainer:1"
        )

        from gco.job_admission import validate_image_sources

        ok, _ = validate_image_sources(
            manifest,
            trusted_registries=list(configured.trusted_registries),
            trusted_dockerhub_orgs=list(configured.trusted_dockerhub_orgs),
        )
        assert ok is False, (
            "an ECR image is rejected by the configured policy even though the "
            "deployed policy accepts it — the reason offline results are advisory"
        )


class TestWeightedPodSpecs:
    def test_trainjob_multiplies_by_num_nodes(self) -> None:
        manifest = {
            "apiVersion": "trainer.kubeflow.org/v1alpha1",
            "kind": "TrainJob",
            "spec": {
                "trainer": {
                    "numNodes": 16,
                    "resourcesPerNode": {"limits": {"nvidia.com/gpu": "8"}},
                }
            },
        }
        specs = weighted_pod_specs(manifest)
        assert any(multiplier == 16 for _spec, multiplier in specs), (
            "a 16-node job counted as one node would make the cap meaningless"
        )

    def test_plain_workloads_weigh_one(self) -> None:
        assert [m for _s, m in weighted_pod_specs(_gpu_job())] == [1]


class TestQuantityParsersMatchTheGate:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [("", 0), ("500m", 500), ("2", 2000), (" 4 ", 4000)],
    )
    def test_cpu(self, value: str, expected: int) -> None:
        assert parse_cpu_millicores(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("", 0),
            ("1Ki", 1024),
            ("1Mi", 1024**2),
            ("1Gi", 1024**3),
            ("1Ti", 1024**4),
            ("1k", 1000),
            ("1M", 1000**2),
            ("1G", 1000**3),
            ("4096", 4096),
        ],
    )
    def test_memory(self, value: str, expected: int) -> None:
        assert parse_memory_bytes(value) == expected


def _hyperpod_toleration() -> dict[str, str]:
    return {"key": "gco.aws/hyperpod", "operator": "Equal", "value": "true", "effect": "NoSchedule"}


class TestHyperpodRouting:
    """Workloads routed to SageMaker HyperPod nodes are held to the HyperPod taint.

    HyperPod nodes carry ``gco.aws/hyperpod=true:NoSchedule`` and none of the
    accelerator taints, so the accelerator rule is a false rejection for them.
    The live failure this pins: the fourth #502 release attempt's probe Job —
    ``kueue.x-k8s.io/queue-name: gco-hyperpod``, one GPU, no tolerations,
    exactly docs/HYPERPOD.md's "a Job needs nothing but the queue name" — was
    rejected at admission for the ``nvidia.com/gpu`` taint it would never meet.
    """

    @staticmethod
    def _queued(manifest: dict[str, Any]) -> dict[str, Any]:
        manifest["metadata"]["labels"] = {"kueue.x-k8s.io/queue-name": "gco-hyperpod"}
        return manifest

    @staticmethod
    def _selecting(manifest: dict[str, Any]) -> dict[str, Any]:
        manifest["spec"]["template"]["spec"]["nodeSelector"] = {
            "gco.aws/capacity-provider": "hyperpod"
        }
        return manifest

    def test_the_hyperpod_queue_needs_no_toleration_at_all(self, processor) -> None:
        manifest = self._queued(_gpu_job())
        assert hyperpod_routing(manifest, [manifest["spec"]["template"]["spec"]]) == "queue"
        assert check_tolerations(manifest) == (True, None)
        assert processor._validate_tolerations(manifest) == (True, None)

    def test_another_queue_is_held_to_the_accelerator_rule(self) -> None:
        manifest = _gpu_job()
        manifest["metadata"]["labels"] = {"kueue.x-k8s.io/queue-name": "gco-default"}
        ok, message = check_tolerations(manifest)
        assert not ok
        assert "nvidia.com/gpu=true:NoSchedule" in (message or "")

    def test_selecting_the_nodes_requires_the_hyperpod_toleration(self) -> None:
        # The "any other scheduler" path of docs/HYPERPOD.md: the author sets
        # the node selector and the toleration. Without the toleration the pod
        # stays Pending forever, which is what this check exists to catch.
        manifest = self._selecting(_gpu_job())
        assert hyperpod_routing(manifest, [manifest["spec"]["template"]["spec"]]) == "selector"
        ok, message = check_tolerations(manifest)
        assert not ok
        assert message is not None
        assert "gco.aws/hyperpod=true:NoSchedule" in message
        assert "nvidia.com/gpu" not in message
        assert "kueue.x-k8s.io/queue-name: gco-hyperpod" in message

        with_toleration = self._selecting(_gpu_job(tolerations=[_hyperpod_toleration()]))
        assert check_tolerations(with_toleration) == (True, None)
        exists = self._selecting(
            _gpu_job(tolerations=[{"key": "gco.aws/hyperpod", "operator": "Exists"}])
        )
        assert check_tolerations(exists) == (True, None)

    def test_a_cpu_job_selecting_the_nodes_is_held_to_the_taint_too(self) -> None:
        manifest = self._selecting(
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": "j", "namespace": "gco-jobs"},
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [{"name": "c", "image": "docker.io/library/busybox"}]
                        }
                    }
                },
            }
        )
        ok, message = check_tolerations(manifest)
        assert not ok
        assert "gco.aws/hyperpod=true:NoSchedule" in (message or "")

    def test_only_a_manifest_whose_every_pod_spec_selects_hyperpod_is_routed(self) -> None:
        selecting = {"nodeSelector": {"gco.aws/capacity-provider": "hyperpod"}, "containers": []}
        other = {"nodeSelector": {"gco.aws/capacity-provider": "karpenter"}, "containers": []}
        manifest: dict[str, Any] = {"kind": "TrainJob", "metadata": {}}
        assert hyperpod_routing(manifest, [selecting, selecting]) == "selector"
        assert hyperpod_routing(manifest, [selecting, other]) is None
        assert hyperpod_routing(manifest, [{"containers": []}]) is None
        assert (
            hyperpod_routing(manifest, [{"nodeSelector": "not-a-dict", "containers": []}]) is None
        )
        assert hyperpod_routing(manifest, []) is None
        assert hyperpod_routing({"kind": "Job"}, [selecting]) == "selector"
        assert hyperpod_routing({"kind": "Job", "metadata": {"labels": "oops"}}, [other]) is None

    def test_the_sqs_path_routes_the_same_way(self, monkeypatch) -> None:
        import importlib

        import gco.services.queue_processor as qp

        monkeypatch.delenv("REQUIRE_ACCELERATOR_TOLERATION", raising=False)
        qp = importlib.reload(qp)
        # The SQS path spells "valid" as (True, "") where the REST path says
        # (True, None); the verdicts and the rejection text must agree.
        queued = self._queued(_gpu_job())
        assert qp.validate_manifest(queued)[0] is True
        assert check_tolerations(queued)[0] is True
        selecting = self._selecting(_gpu_job())
        sqs_ok, sqs_reason = qp.validate_manifest(selecting)
        rest_ok, rest_reason = check_tolerations(selecting)
        assert (sqs_ok, rest_ok) == (False, False)
        assert "gco.aws/hyperpod=true:NoSchedule" in sqs_reason
        assert sqs_reason == rest_reason
        tolerating = self._selecting(_gpu_job(tolerations=[_hyperpod_toleration()]))
        assert qp.validate_manifest(tolerating)[0] is True
        assert check_tolerations(tolerating)[0] is True


class TestNoKubernetesImport:
    def test_module_is_client_free(self) -> None:
        """The point of the split: importable without the Kubernetes client.

        Asserted on the module's own source rather than on sys.modules, since
        another test in the same session may already have imported kubernetes.
        """
        import pathlib

        import gco.job_admission as module

        source = pathlib.Path(module.__file__).read_text()
        for forbidden in ("import kubernetes", "from kubernetes", "import boto3"):
            assert forbidden not in source, f"gco.job_admission must not {forbidden}"
