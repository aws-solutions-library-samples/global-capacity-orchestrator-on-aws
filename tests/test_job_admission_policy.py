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
    WORKLOAD_KINDS,
    JobValidationPolicy,
    admit_manifest,
    check_resource_caps,
    check_tolerations,
    hyperpod_routing,
    parse_cpu_millicores,
    parse_gpu_count,
    parse_memory_bytes,
    resource_totals,
    trainjob_validation_pod_specs,
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
    """The REST service returns the shared sequence's verdict, unchanged."""

    @pytest.mark.parametrize(
        ("manifest", "fragment"),
        [
            (_gpu_job(1, tolerations=[{"key": "nvidia.com/gpu", "operator": "Exists"}]), None),
            (_gpu_job(1), "nvidia.com/gpu=true:NoSchedule"),
            (_gpu_job(999), "GPU 999 exceeds max 16"),
        ],
        ids=["tolerated", "missing-toleration", "over-cap"],
    )
    def test_the_service_returns_admit_manifest(self, processor, manifest, fragment) -> None:
        verdict = admit_manifest(
            manifest,
            processor.job_validation_policy(),
            runtime_class_exists=processor.runtime_class_exists,
        )
        assert processor.validate_manifest(manifest) == verdict
        if fragment is None:
            assert verdict == (True, None)
        else:
            assert verdict[0] is False and fragment in (verdict[1] or "")

    def test_policy_reflects_attributes_mutated_after_construction(self, processor) -> None:
        """The policy is rebuilt per call, so a late attribute change is honored.

        Tests reach in and set these caps directly; a snapshot taken in
        __init__ would keep enforcing the old value while
        effective_job_validation_policy() reported the new one.
        """
        processor.max_gpu_per_manifest = 0
        ok, message = processor.validate_manifest(_gpu_job(1))
        assert ok is False
        assert "GPU 1 exceeds max 0" in (message or "")

    def test_an_error_building_the_policy_is_a_refusal(self, processor) -> None:
        """Outside the sequence too, an exception refuses rather than admits."""
        with patch.object(processor, "job_validation_policy", side_effect=RuntimeError("boom")):
            assert processor.validate_manifest(_gpu_job(1)) == (False, "Validation error: boom")


def _admit(manifest: dict[str, Any], **changes: Any) -> tuple[bool, str | None]:
    """admit_manifest under the shipped policy with *changes*, every RuntimeClass present."""
    policy = JobValidationPolicy.from_cdk_context({})._replace(**changes)
    return admit_manifest(manifest, policy, runtime_class_exists=lambda name: True)


class TestAdmitManifest:
    """The one admission sequence: its order, its switches and its failure mode."""

    def test_validation_disabled_admits_anything(self) -> None:
        assert _admit({"kind": "Secret"}, validation_enabled=False) == (True, None)

    def test_the_first_failing_check_is_the_verdict(self) -> None:
        manifest = _gpu_job(999)
        manifest["metadata"]["namespace"] = "kube-system"
        assert _admit(manifest) == (
            False,
            "Namespace 'kube-system' not allowed. Allowed namespaces: ['gco-jobs']",
        )

    def test_the_default_namespace_applies_only_without_metadata_namespace(self) -> None:
        manifest = _gpu_job(1, tolerations=[{"key": "nvidia.com/gpu", "operator": "Exists"}])
        del manifest["metadata"]["namespace"]
        policy = JobValidationPolicy.from_cdk_context({"allowed_namespaces": ["team-a"]})
        present = lambda name: True  # noqa: E731
        assert admit_manifest(
            manifest, policy, runtime_class_exists=present, default_namespace="team-a"
        ) == (True, None)
        assert admit_manifest(manifest, policy, runtime_class_exists=present) == (
            False,
            "Namespace 'gco-jobs' not allowed. Allowed namespaces: ['team-a']",
        )

    @pytest.mark.parametrize("kind", WORKLOAD_KINDS)
    def test_a_workload_without_a_pod_spec_is_refused(self, kind: str) -> None:
        manifest = {"apiVersion": "v1", "kind": kind, "metadata": {"name": "w"}, "spec": {}}
        assert _admit(manifest, allowed_kinds=frozenset(WORKLOAD_KINDS)) == (
            False,
            f"{kind} manifest must contain a valid pod spec",
        )

    @pytest.mark.parametrize("kind", ["ConfigMap", "Service", "TrainJob"])
    def test_kinds_without_their_own_pod_spec_are_not_held_to_one(self, kind: str) -> None:
        manifest = {"apiVersion": "v1", "kind": kind, "metadata": {"name": "w"}, "spec": {}}
        assert _admit(manifest) == (True, None)

    def test_the_toleration_rule_follows_its_switch(self) -> None:
        assert _admit(_gpu_job(1))[0] is False
        assert _admit(_gpu_job(1), require_accelerator_toleration=False) == (True, None)

    def test_a_missing_sandbox_runtime_class_is_refused(self) -> None:
        manifest = _gpu_job(0)
        manifest["spec"]["template"]["spec"]["runtimeClassName"] = "isolated-sandbox"
        policy = JobValidationPolicy.from_cdk_context({})
        ok, message = admit_manifest(manifest, policy, runtime_class_exists=lambda name: False)
        assert ok is False
        assert (message or "").startswith(
            "Nitro Sandbox validation failed: RuntimeClass isolated-sandbox does not exist"
        )

    def test_an_exception_is_a_refusal(self) -> None:
        ok, message = _admit({"apiVersion": "v1", "kind": "Job", "metadata": None})
        assert ok is False
        assert (message or "").startswith("Validation error: argument of type 'NoneType'")

    def test_the_logged_exception_cannot_forge_a_log_line(self, caplog) -> None:
        with (
            patch("gco.job_admission.check_resource_caps", side_effect=RuntimeError("bad\nforged")),
            caplog.at_level("ERROR", logger="gco.job_admission"),
        ):
            verdict = _admit(_gpu_job(1))
        assert verdict == (False, "Validation error: bad\nforged")
        assert "Error validating manifest: bad\\nforged" in caplog.text


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

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(None, 0), ("", 0), ("  ", 0), (1, 1), ("2", 2), (4.0, 4)],
    )
    def test_gpu(self, value: object, expected: int) -> None:
        assert parse_gpu_count(value) == expected

    @pytest.mark.parametrize("parse", [parse_cpu_millicores, parse_memory_bytes, parse_gpu_count])
    def test_absent_values_count_as_zero(self, parse: Any) -> None:
        assert parse(None) == 0


def _job_with_resources(resources: Any, *, multiplier_kind: str = "Job") -> dict[str, Any]:
    """A trusted-image Job whose one container carries ``resources`` verbatim."""
    container: dict[str, Any] = {"name": "trainer", "image": "docker.io/pytorch/pytorch:latest"}
    if resources is not ...:
        container["resources"] = resources
    return {
        "apiVersion": "batch/v1",
        "kind": multiplier_kind,
        "metadata": {"name": "train", "namespace": "gco-jobs"},
        "spec": {"template": {"spec": {"containers": [container]}}},
    }


class TestResourceCapsAcceptWhatTheApiServerAccepts:
    """Regression: the gate refused quantities the API server takes.

    ``cpu: 1`` written as a YAML number failed with ``'int' object has no
    attribute 'strip'`` and ``"0.5"`` CPU with ``invalid literal for int()``,
    so a valid manifest was rejected at the front door.
    """

    @pytest.fixture
    def policy(self, processor: Any) -> JobValidationPolicy:
        result: JobValidationPolicy = processor.job_validation_policy()
        return result

    @pytest.mark.parametrize(
        "resources",
        [
            {"limits": {"cpu": 1, "memory": 2147483648}},
            {"requests": {"cpu": 0.5, "memory": "512Mi"}},
            {"limits": {"cpu": "1.5", "memory": "1.5Gi"}},
            {"limits": {"cpu": "1e0", "memory": "1e9"}},
            {"limits": {"nvidia.com/gpu": 1}},
            None,
            {"requests": None, "limits": None},
            ...,  # no resources key at all
        ],
    )
    def test_admits(self, resources: Any, policy: JobValidationPolicy) -> None:
        assert check_resource_caps(_job_with_resources(resources), policy) == (True, "")

    @pytest.mark.parametrize(
        ("resources", "fragment"),
        [
            ({"limits": {"nvidia.com/gpu": "0.5"}}, "nvidia.com/gpu: '0.5' is not a whole number"),
            ({"limits": {"cpu": -400000}}, "cpu: -400000 is negative"),
            ({"limits": {"cpu": True}}, "cpu: True is not a Kubernetes quantity"),
            ({"limits": {"memory": "2Gib"}}, "memory: '2Gib' is not a Kubernetes quantity"),
        ],
    )
    def test_rejects_invalid_quantities_naming_the_container(
        self, resources: Any, fragment: str, policy: JobValidationPolicy
    ) -> None:
        ok, message = check_resource_caps(_job_with_resources(resources), policy)
        assert ok is False
        assert message.startswith("Resource limit validation error: container 'trainer' ")
        assert fragment in message

    def test_a_cap_breach_names_the_key_that_sets_the_caps(
        self, policy: JobValidationPolicy
    ) -> None:
        ok, message = check_resource_caps(_job_with_resources({"limits": {"cpu": 500}}), policy)
        assert ok is False
        assert "CPU 500000m exceeds max" in message
        assert "update job_validation_policy.resource_quotas in cdk.json" in message


class TestResourceTotals:
    def test_sums_every_container_kind_weighted_by_view(self) -> None:
        spec = {
            "containers": [{"resources": {"limits": {"cpu": "1", "memory": "1Gi"}}}],
            "initContainers": [{"resources": {"requests": {"cpu": 0.5}}}],
            "ephemeralContainers": [{"resources": {"limits": {"nvidia.com/gpu": 2}}}],
        }
        assert resource_totals([(spec, 3)]) == (4500, 3 * 2**30, 6)

    def test_null_container_lists_count_as_empty(self) -> None:
        spec = {"containers": None, "initContainers": None, "ephemeralContainers": None}
        assert resource_totals([(spec, 1)]) == (0, 0, 0)

    def test_a_limit_wins_over_a_request(self) -> None:
        spec = {"containers": [{"resources": {"requests": {"cpu": 8}, "limits": {"cpu": 2}}}]}
        assert resource_totals([(spec, 1)]) == (2000, 0, 0)

    def test_an_invalid_quantity_names_the_container_type_and_field(self) -> None:
        spec = {"initContainers": [{"name": "setup", "resources": {"limits": {"memory": "lots"}}}]}
        with pytest.raises(ValueError, match=r"^initContainer 'setup' memory: 'lots' is not"):
            resource_totals([(spec, 1)])


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
        assert processor.validate_manifest(manifest) == (True, None)

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
        manifest: dict[str, Any] = {"kind": "Job", "metadata": {}}
        assert hyperpod_routing(manifest, [selecting, selecting]) == "selector"
        assert hyperpod_routing(manifest, [selecting, other]) is None
        assert hyperpod_routing(manifest, [{"containers": []}]) is None
        assert (
            hyperpod_routing(manifest, [{"nodeSelector": "not-a-dict", "containers": []}]) is None
        )
        assert hyperpod_routing(manifest, []) is None
        assert hyperpod_routing({"kind": "Job"}, [selecting]) == "selector"
        assert hyperpod_routing({"kind": "Job", "metadata": {"labels": "oops"}}, [other]) is None

    @staticmethod
    def _trainjob(*patches: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """A two-node, one-GPU-per-node TrainJob; each patch maps replicated job names to pod specs."""
        return {
            "apiVersion": "trainer.kubeflow.org/v1alpha1",
            "kind": "TrainJob",
            "metadata": {"name": "t", "namespace": "gco-jobs"},
            "spec": {
                "runtimeRef": {"name": "torch-distributed"},
                "trainer": {
                    "image": "docker.io/pytorch/pytorch:latest",
                    "numNodes": 2,
                    "resourcesPerNode": {"limits": {"nvidia.com/gpu": "1"}},
                },
                "runtimePatches": [
                    {
                        "manager": f"example.com/patch-{index}",
                        "trainingRuntimeSpec": {
                            "template": {
                                "spec": {
                                    "replicatedJobs": [
                                        {
                                            "name": name,
                                            "template": {"spec": {"template": {"spec": pod_spec}}},
                                        }
                                        for name, pod_spec in patch.items()
                                    ]
                                }
                            }
                        },
                    }
                    for index, patch in enumerate(patches)
                ],
            },
        }

    _HYPERPOD_SELECTOR = {"nodeSelector": {"gco.aws/capacity-provider": "hyperpod"}}

    def test_a_trainjob_is_routed_by_its_merged_pod_templates(self, monkeypatch) -> None:
        """Trainer merges the runtime patches naming one replicated job into one pod template.

        Here the node selector sits in one patch and the toleration in another:
        one pod that selects and tolerates HyperPod. Judged view by view (each
        patch, and the synthetic trainer spec, which has no scheduling fields),
        the TrainJob was not routed, so the accelerator rule asked for an
        ``nvidia.com/gpu`` toleration that HyperPod nodes have no taint for.
        """
        import importlib

        import gco.services.queue_processor as qp
        from cli.capacity.placement import manifest_placement

        manifest = self._trainjob(
            {"node": self._HYPERPOD_SELECTOR}, {"node": {"tolerations": [_hyperpod_toleration()]}}
        )
        assert hyperpod_routing(manifest, trainjob_validation_pod_specs(manifest)) == "selector"
        assert check_tolerations(manifest) == (True, None)
        monkeypatch.delenv("REQUIRE_ACCELERATOR_TOLERATION", raising=False)
        qp = importlib.reload(qp)
        assert qp.validate_manifest(manifest) == (True, None)
        assert manifest_placement([manifest]).routing == "selector"

        # Routed, so the HyperPod taint is what it must tolerate.
        untolerated = self._trainjob({"node": self._HYPERPOD_SELECTOR})
        ok, message = check_tolerations(untolerated)
        assert not ok
        assert "gco.aws/hyperpod=true:NoSchedule" in (message or "")
        assert qp.validate_manifest(untolerated) == (False, message)

    def test_every_pod_template_a_trainjob_patches_must_select_hyperpod(self) -> None:
        selector = self._HYPERPOD_SELECTOR
        views = trainjob_validation_pod_specs

        def route(manifest: dict[str, Any]) -> str | None:
            return hyperpod_routing(manifest, views(manifest))

        # Another replicated job is another pod.
        assert route(self._trainjob({"node": selector, "dataset-initializer": {}})) is None
        # A later patch's value wins; a null is pruned by the API server.
        karpenter = {"nodeSelector": {"gco.aws/capacity-provider": "karpenter"}}
        assert route(self._trainjob({"node": selector}, {"node": karpenter})) is None
        assert route(self._trainjob({"node": karpenter}, {"node": selector})) == "selector"
        pruned = {"nodeSelector": {"gco.aws/capacity-provider": None, "zone": "a"}}
        assert route(self._trainjob({"node": selector}, {"node": pruned})) == "selector"
        # No patch, or nothing a pod template can be read from.
        assert route(self._trainjob()) is None
        for runtime_patches in (
            "not-a-list",
            [{"manager": "m"}],
            [{"manager": "m", "trainingRuntimeSpec": {"template": "not-a-dict"}}],
            [{"manager": "m", "trainingRuntimeSpec": {"template": {"spec": {}}}}],
        ):
            manifest = self._trainjob()
            manifest["spec"]["runtimePatches"] = runtime_patches
            assert route(manifest) is None, runtime_patches
        malformed = self._trainjob({"node": selector}, {"node": {"nodeSelector": "oops"}})
        jobs = malformed["spec"]["runtimePatches"][1]["trainingRuntimeSpec"]["template"]["spec"]
        jobs["replicatedJobs"].append("not-a-dict")
        assert route(malformed) == "selector"
        assert route({"kind": "TrainJob", "spec": "not-a-dict"}) is None

    def test_the_sqs_path_routes_the_same_way(self, monkeypatch) -> None:
        import importlib

        import gco.services.queue_processor as qp

        monkeypatch.delenv("REQUIRE_ACCELERATOR_TOLERATION", raising=False)
        qp = importlib.reload(qp)
        # The verdicts and the rejection text must agree.
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


def _gpu_toleration() -> dict[str, str]:
    return {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}


def _other_toleration() -> dict[str, str]:
    return {"key": "example.com/dedicated", "operator": "Exists", "effect": "NoSchedule"}


class TestTrainJobTolerationsAreMergedLikeTrainer:
    """A TrainJob's pods carry the tolerations Trainer builds, not every patch's.

    Kubeflow Trainer v2.3.0 applies ``spec.runtimePatches`` in order as
    strategic merge patches onto the runtime's JobSet spec (replicated jobs
    merge by name). ``PodSpec.tolerations`` has no patch merge key
    (``+listType=atomic``), so a later patch that sets tolerations for a
    replicated job replaces the earlier list; the patch type marshals with
    ``omitempty``, so an empty list sets nothing. Both submission paths
    unioned every patch's tolerations and accepted a TrainJob whose pod would
    not carry the toleration they had checked.
    """

    _trainjob = staticmethod(TestHyperpodRouting._trainjob)

    @staticmethod
    def _verdicts(monkeypatch, manifest: dict[str, Any]) -> tuple[tuple[bool, Any], ...]:
        """(REST toleration check, SQS consumer) verdicts."""
        import importlib

        import gco.services.queue_processor as qp

        monkeypatch.delenv("REQUIRE_ACCELERATOR_TOLERATION", raising=False)
        qp = importlib.reload(qp)
        rest = check_tolerations(manifest)
        return rest, qp.validate_manifest(manifest)

    def test_a_later_patch_replaces_the_gpu_toleration(self, monkeypatch) -> None:
        manifest = self._trainjob(
            {"node": {"tolerations": [_gpu_toleration()]}},
            {"node": {"tolerations": [_other_toleration()]}},
        )
        rest, sqs = self._verdicts(monkeypatch, manifest)
        assert rest[0] is False
        assert "nvidia.com/gpu=true:NoSchedule" in (rest[1] or "")
        assert sqs == rest

    def test_the_last_list_set_for_the_pod_template_is_the_one_judged(self, monkeypatch) -> None:
        last_wins = self._trainjob(
            {"node": {"tolerations": [_other_toleration()]}},
            {"node": {"tolerations": [_gpu_toleration()]}},
        )
        assert self._verdicts(monkeypatch, last_wins) == ((True, None), (True, None))
        # An empty list (or null) is dropped by the patch type and replaces nothing.
        for unset in ([], None):
            kept = self._trainjob(
                {"node": {"tolerations": [_gpu_toleration()]}},
                {"node": {"tolerations": unset, "nodeSelector": {"zone": "a"}}},
            )
            assert self._verdicts(monkeypatch, kept) == ((True, None), (True, None)), unset

    def test_the_hyperpod_toleration_replaced_by_a_later_patch_is_missing(
        self, monkeypatch
    ) -> None:
        selector = {"gco.aws/capacity-provider": "hyperpod"}
        manifest = self._trainjob(
            {"node": {"nodeSelector": selector, "tolerations": [_hyperpod_toleration()]}},
            {"node": {"tolerations": [_gpu_toleration()]}},
        )
        rest, sqs = self._verdicts(monkeypatch, manifest)
        assert rest[0] is False
        assert "gco.aws/hyperpod=true:NoSchedule" in (rest[1] or "")
        assert sqs == rest

    def test_a_toleration_outside_the_runtime_patches_reaches_no_pod(self, monkeypatch) -> None:
        """The TrainJob CRD has no other tolerations field and prunes unknown ones."""
        manifest = self._trainjob()
        manifest["spec"]["podTemplateOverrides"] = [{"spec": {"tolerations": [_gpu_toleration()]}}]
        rest, sqs = self._verdicts(monkeypatch, manifest)
        assert rest[0] is False
        assert "nvidia.com/gpu=true:NoSchedule" in (rest[1] or "")
        assert sqs == rest


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
