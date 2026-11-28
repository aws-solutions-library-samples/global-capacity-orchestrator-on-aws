"""
Job submission validation on both submission paths.

The REST ``ManifestProcessor`` (``gco/services/manifest_processor.py``) and
the SQS ``queue_processor`` (``gco/services/queue_processor.py``) both return
``gco.job_admission.admit_manifest``, so the SQS path cannot be used to bypass
admission control. Every scenario here is exercised against BOTH processors:
the accelerator-toleration requirement, every document in ``examples/*.yaml``,
crafted manifests aimed at each check, and the policy each service builds from
the same environment.
"""

from __future__ import annotations

import copy
import importlib
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from kubernetes import config as k8s_config

from gco.hyperpod_config import CAPACITY_PROVIDER_HYPERPOD, CAPACITY_PROVIDER_LABEL
from gco.job_admission import (
    CONFIDENTIAL_SANDBOX,
    ISOLATED_SANDBOX,
    SANDBOX_POLICY_ANNOTATION,
)
from gco.keep_warm import KEEP_WARM_LABEL
from gco.manifest_security_policy import MANIFEST_SECURITY_POLICY_DEFAULTS

# ---------------------------------------------------------------------------
# REST ManifestProcessor fixture (mirrors test_manifest_security_validation.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_k8s_config():
    """Mock Kubernetes configuration loading."""
    with patch("gco.services.manifest_processor.config") as mock_config:
        mock_config.ConfigException = k8s_config.ConfigException
        mock_config.load_incluster_config.side_effect = k8s_config.ConfigException("Not in cluster")
        mock_config.load_kube_config.return_value = None
        yield mock_config


@pytest.fixture
def manifest_processor(mock_k8s_config):
    """ManifestProcessor with mocked K8s clients and the new policy enabled."""
    from gco.services.manifest_processor import ManifestProcessor

    with patch("gco.services.manifest_processor.client"):
        return ManifestProcessor(
            cluster_id="test-cluster",
            region="us-east-1",
            config_dict={
                "max_cpu_per_manifest": "100",
                "max_memory_per_manifest": "256Gi",
                "max_gpu_per_manifest": 8,
                "require_accelerator_toleration": True,
                "allowed_namespaces": ["default", "gco-jobs"],
                "validation_enabled": True,
            },
        )


# ---------------------------------------------------------------------------
# SQS queue_processor helper (mirrors test_queue_processor.py)
# ---------------------------------------------------------------------------

_QP_ENV = {
    "ALLOWED_NAMESPACES": "default,gco-jobs",
    "MAX_CPU_PER_MANIFEST": "100",
    "MAX_MEMORY_PER_MANIFEST": "256Gi",
    "MAX_GPU_PER_MANIFEST": "8",
    "REQUIRE_ACCELERATOR_TOLERATION": "true",
}


@pytest.fixture
def qp(monkeypatch):
    """Reload queue_processor so module-level env reads pick up test values."""
    for name, value in _QP_ENV.items():
        monkeypatch.setenv(name, value)
    import gco.services.queue_processor as qp_module

    return importlib.reload(qp_module)


# ---------------------------------------------------------------------------
# Manifest builders
# ---------------------------------------------------------------------------


def _job(pod_spec_overrides=None, container_overrides=None):
    container = {"name": "worker", "image": "python:3.14"}
    if container_overrides:
        container.update(container_overrides)
    pod_spec = {"containers": [container], "restartPolicy": "Never"}
    if pod_spec_overrides:
        pod_spec.update(pod_spec_overrides)
    spec = {"template": {"spec": pod_spec}}
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "test-job", "namespace": "default"},
        "spec": spec,
    }


def _validate_both(manifest_processor, qp, manifest):
    """Validate the same manifest through both processors.

    Returns ((mp_ok, mp_err), (qp_ok, qp_err)).
    """
    return manifest_processor.validate_manifest(manifest), qp.validate_manifest(manifest)


# ===========================================================================
# Accelerator toleration requirement (item 4)
# ===========================================================================

_ACCEL_CASES = [
    ("nvidia.com/gpu", "nvidia.com/gpu"),
    ("aws.amazon.com/neuron", "aws.amazon.com/neuron"),
    ("vpc.amazonaws.com/efa", "vpc.amazonaws.com/efa"),
]


@pytest.mark.parametrize(("resource_key", "taint_key"), _ACCEL_CASES)
def test_accelerator_without_toleration_rejected(manifest_processor, qp, resource_key, taint_key):
    """A job requesting an accelerator but with no toleration is rejected by both paths."""
    manifest = _job(container_overrides={"resources": {"limits": {resource_key: "1"}}})

    (mp_ok, mp_err), (qp_ok, qp_err) = _validate_both(manifest_processor, qp, manifest)

    assert mp_ok is False, f"REST accepted {resource_key} job with no toleration"
    assert qp_ok is False, f"SQS accepted {resource_key} job with no toleration"
    assert taint_key in mp_err
    assert taint_key in qp_err


@pytest.mark.parametrize(("resource_key", "taint_key"), _ACCEL_CASES)
def test_accelerator_with_exists_toleration_admitted(
    manifest_processor, qp, resource_key, taint_key
):
    """A job with a matching Exists/NoSchedule toleration is admitted by both paths."""
    manifest = _job(
        container_overrides={"resources": {"limits": {resource_key: "1"}}},
        pod_spec_overrides={
            "tolerations": [
                {"key": taint_key, "operator": "Exists", "effect": "NoSchedule"},
            ]
        },
    )

    (mp_ok, _), (qp_ok, _) = _validate_both(manifest_processor, qp, manifest)

    assert mp_ok is True, f"REST rejected {resource_key} job that HAS a toleration"
    assert qp_ok is True, f"SQS rejected {resource_key} job that HAS a toleration"


def test_accelerator_with_equal_true_toleration_admitted(manifest_processor, qp):
    """operator: Equal with value: 'true' also satisfies the requirement."""
    manifest = _job(
        container_overrides={"resources": {"requests": {"nvidia.com/gpu": "2"}}},
        pod_spec_overrides={
            "tolerations": [
                {
                    "key": "nvidia.com/gpu",
                    "operator": "Equal",
                    "value": "true",
                    "effect": "NoSchedule",
                }
            ]
        },
    )

    (mp_ok, _), (qp_ok, _) = _validate_both(manifest_processor, qp, manifest)

    assert mp_ok is True
    assert qp_ok is True


def test_non_accelerator_job_needs_no_toleration(manifest_processor, qp):
    """A plain CPU job is admitted without any toleration."""
    manifest = _job(container_overrides={"resources": {"requests": {"cpu": "1"}}})

    (mp_ok, _), (qp_ok, _) = _validate_both(manifest_processor, qp, manifest)

    assert mp_ok is True
    assert qp_ok is True


def test_wrong_effect_toleration_rejected(manifest_processor, qp):
    """A toleration for a different effect (NoExecute) does not satisfy NoSchedule."""
    manifest = _job(
        container_overrides={"resources": {"limits": {"nvidia.com/gpu": "1"}}},
        pod_spec_overrides={
            "tolerations": [
                {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoExecute"},
            ]
        },
    )

    (mp_ok, _), (qp_ok, _) = _validate_both(manifest_processor, qp, manifest)

    assert mp_ok is False
    assert qp_ok is False


# ===========================================================================
# One admission sequence: the same verdict, word for word, on both paths
# ===========================================================================

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: Every variable either service reads into its admission policy.
_POLICY_VARIABLES = (
    "ALLOWED_NAMESPACES",
    "ALLOWED_KINDS",
    "MAX_CPU_PER_MANIFEST",
    "MAX_MEMORY_PER_MANIFEST",
    "MAX_GPU_PER_MANIFEST",
    "TRUSTED_REGISTRIES",
    "TRUSTED_DOCKERHUB_ORGS",
    "REQUIRE_ACCELERATOR_TOLERATION",
    "YAML_MAX_DEPTH",
    "VALIDATION_ENABLED",
    *(key.upper() for key in MANIFEST_SECURITY_POLICY_DEFAULTS),
)

#: The environments both services start from: no variable set (the shipped
#: defaults); every kind of variable set away from its default, in spellings
#: the shared parsers accept; and the empty values with a secure fallback.
_ENVIRONMENTS: dict[str, dict[str, str]] = {
    "shipped": {},
    "custom": {
        "ALLOWED_NAMESPACES": " gco-jobs, team-a ",
        "ALLOWED_KINDS": "Job,CronJob,Pod,TrainJob,ConfigMap",
        "MAX_CPU_PER_MANIFEST": "8",
        "MAX_MEMORY_PER_MANIFEST": "16Gi",
        "MAX_GPU_PER_MANIFEST": "2",
        "TRUSTED_REGISTRIES": "nvcr.io,public.ecr.aws/amazonlinux",
        "TRUSTED_DOCKERHUB_ORGS": "pytorch",
        "REQUIRE_ACCELERATOR_TOLERATION": "yes",
        "YAML_MAX_DEPTH": "32",
        "BLOCK_RUN_AS_ROOT": "true",
        "BLOCK_HOST_IPC": "off",
    },
    "empty-values": {
        "ALLOWED_KINDS": "",
        "TRUSTED_REGISTRIES": "",
        "TRUSTED_DOCKERHUB_ORGS": " , ",
        "BLOCK_PRIVILEGED": "",
    },
}


def _isolated_sandbox_only(name: str) -> bool:
    """The RuntimeClass lookup both paths get: isolated-sandbox exists, nothing else."""
    return name == ISOLATED_SANDBOX


def _admission_paths(monkeypatch: pytest.MonkeyPatch, environment: dict[str, str]) -> Any:
    """``(REST processor, SQS module)`` started from one environment, as each service starts.

    The REST processor is built by ``create_manifest_processor_from_env`` with
    its Kubernetes clients mocked; the SQS module is reloaded, which reads its
    policy the way a fresh KEDA pod does.
    """
    for name in _POLICY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    import gco.services.manifest_processor as manifest_processor
    import gco.services.queue_processor as queue_processor

    with (
        patch.object(manifest_processor, "configure_structured_logging"),
        patch.object(manifest_processor, "config"),
        patch.object(manifest_processor, "client"),
    ):
        rest = manifest_processor.create_manifest_processor_from_env()
    sqs = importlib.reload(queue_processor)
    rest.runtime_class_exists = _isolated_sandbox_only
    monkeypatch.setattr(sqs, "runtime_class_exists", _isolated_sandbox_only)
    return rest, sqs


def _assert_one_verdict(rest: Any, sqs: Any, manifest: dict[str, Any]) -> tuple[bool, str | None]:
    """Both services' verdict on *manifest*, which must be the same tuple."""
    verdict = rest.validate_manifest(copy.deepcopy(manifest))
    assert sqs.validate_manifest(copy.deepcopy(manifest)) == verdict
    return verdict


def _example_documents() -> list[Any]:
    """Every mapping document in every ``examples/*.yaml``, as pytest params."""
    params = []
    for path in sorted((_REPO_ROOT / "examples").glob("*.yaml")):
        documents = yaml.safe_load_all(path.read_text(encoding="utf-8"))
        for index, document in enumerate(documents):
            if isinstance(document, dict):
                params.append(pytest.param(document, id=f"{path.name}#{index}"))
    return params


_GPU_TOLERATION = {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}


def _container(name: str = "worker", image: str = "python:3.14-slim", **fields: Any) -> Any:
    return {"name": name, "image": image, **fields}


def _workload(pod_spec: dict[str, Any], *, kind: str = "Job", namespace: str = "gco-jobs") -> Any:
    """A Job (or a bare Pod) in *namespace* whose pod is *pod_spec*."""
    spec = pod_spec if kind == "Pod" else {"template": {"spec": pod_spec}}
    return {
        "apiVersion": "v1" if kind == "Pod" else "batch/v1",
        "kind": kind,
        "metadata": {"name": "parity", "namespace": namespace},
        "spec": spec,
    }


def _gpus(count: int) -> dict[str, Any]:
    return {"limits": {"nvidia.com/gpu": count}}


def _trainjob(*node_patches: dict[str, Any]) -> Any:
    """A two-node, one-GPU-per-node TrainJob; each patch is a pod spec for its node pods."""
    return {
        "apiVersion": "trainer.kubeflow.org/v1alpha1",
        "kind": "TrainJob",
        "metadata": {"name": "parity", "namespace": "gco-jobs"},
        "spec": {
            "runtimeRef": {"name": "torch-distributed"},
            "trainer": {
                "image": "pytorch/pytorch:2.13.0-cuda13.0-cudnn9-runtime",
                "numNodes": 2,
                "resourcesPerNode": _gpus(1),
            },
            "runtimePatches": [
                {
                    "manager": f"parity-{index}",
                    "trainingRuntimeSpec": {
                        "template": {
                            "spec": {
                                "replicatedJobs": [
                                    {
                                        "name": "node",
                                        "template": {"spec": {"template": {"spec": pod_spec}}},
                                    }
                                ]
                            }
                        }
                    },
                }
                for index, pod_spec in enumerate(node_patches)
            ],
        },
    }


def _nested_job(depth: int) -> Any:
    """A valid Job carrying one annotation nested *depth* dicts deep."""
    nested: Any = "leaf"
    for _ in range(depth):
        nested = {"nested": nested}
    job = _workload({"containers": [_container()]})
    job["metadata"]["annotations"] = {"deep": nested}
    return job


def _confidential_job() -> Any:
    """A confidential-sandbox Job that satisfies every sandbox rule but the RuntimeClass."""
    job = _workload(
        {
            "containers": [_container()],
            "runtimeClassName": CONFIDENTIAL_SANDBOX,
            "automountServiceAccountToken": False,
        }
    )
    job["spec"]["template"]["metadata"] = {"annotations": {SANDBOX_POLICY_ANNOTATION: "cGFyaXR5"}}
    return job


def _shapeless(kind: str, spec: dict[str, Any]) -> Any:
    return {
        "apiVersion": "batch/v1",
        "kind": kind,
        "metadata": {"name": "parity", "namespace": "gco-jobs"},
        "spec": spec,
    }


#: ``(manifest, the refusal it must get in the "custom" environment)``: a
#: fragment of the message, or None for a manifest both paths admit.
_CRAFTED = [
    pytest.param(
        _workload(
            {
                "containers": [_container()],
                "initContainers": [_container("setup", securityContext={"privileged": True})],
            }
        ),
        "initContainer 'setup': privileged containers are not permitted",
        id="privileged-init-container",
    ),
    pytest.param(
        _workload(
            {
                "containers": [_container()],
                "ephemeralContainers": [_container("debug", securityContext={"privileged": True})],
            },
            kind="Pod",
        ),
        "ephemeralContainer 'debug': privileged containers are not permitted",
        id="privileged-ephemeral-container",
    ),
    pytest.param(
        _workload(
            {"containers": [_container()], "volumes": [{"name": "h", "hostPath": {"path": "/"}}]}
        ),
        "Security context validation failed: hostPath volumes are not permitted",
        id="host-path-volume",
    ),
    pytest.param(
        _workload({"containers": [_container()], "hostNetwork": True}, kind="Pod"),
        "Security context validation failed: hostNetwork is not permitted",
        id="host-network",
    ),
    pytest.param(
        _workload({"containers": [_container()], "securityContext": {"runAsUser": 0}}),
        "Security context validation failed: running as root (runAsUser: 0) is not permitted",
        id="pod-run-as-root",
    ),
    pytest.param(
        _workload({"containers": [_container(securityContext={"runAsUser": 0})]}),
        "container 'worker': running as root (runAsUser: 0) is not permitted",
        id="container-run-as-root",
    ),
    pytest.param(
        _workload(
            {"containers": [_container(securityContext={"capabilities": {"add": ["NET_ADMIN"]}})]}
        ),
        "container 'worker': added capabilities are not permitted",
        id="added-capabilities",
    ),
    pytest.param(
        _workload({"containers": [_container(securityContext={"allowPrivilegeEscalation": True})]}),
        "container 'worker': allowPrivilegeEscalation is not permitted",
        id="privilege-escalation",
    ),
    pytest.param(
        _workload(
            {
                "containers": [_container()],
                "initContainers": [_container("setup", image="registry.example.com/tools:1")],
            }
        ),
        "initContainer 'setup': Untrusted image source 'registry.example.com/tools:1'",
        id="untrusted-init-container-image",
    ),
    pytest.param(
        _workload({"containers": [_container(resources=_gpus(1))]}),
        "no matching toleration for taint nvidia.com/gpu=true:NoSchedule",
        id="gpu-without-toleration",
    ),
    pytest.param(
        _workload(
            {
                "containers": [_container(resources=_gpus(1))],
                "nodeSelector": {CAPACITY_PROVIDER_LABEL: CAPACITY_PROVIDER_HYPERPOD},
            }
        ),
        "no matching toleration for taint gco.aws/hyperpod=true:NoSchedule",
        id="hyperpod-selector-without-toleration",
    ),
    pytest.param(
        _trainjob({"tolerations": [_GPU_TOLERATION]}),
        None,
        id="trainjob-runtime-patches",
    ),
    pytest.param(
        _trainjob(
            {"tolerations": [_GPU_TOLERATION]},
            {"containers": [_container("node", securityContext={"privileged": True})]},
        ),
        "container 'node': privileged containers are not permitted",
        id="trainjob-privileged-runtime-patch",
    ),
    pytest.param(
        _workload(
            {
                "containers": [_container()],
                "initContainers": [
                    _container(name, resources={"requests": {"cpu": "200"}}) for name in "ab"
                ],
            }
        ),
        "CPU 400000m exceeds max",
        id="cpu-over-cap-across-init-containers",
    ),
    pytest.param(
        _workload(
            {
                "containers": [_container()],
                "initContainers": [
                    _container(name, resources={"requests": {"memory": "3000Gi"}}) for name in "ab"
                ],
            }
        ),
        "Memory 6000Gi exceeds max",
        id="memory-over-cap-across-init-containers",
    ),
    pytest.param(
        _workload(
            {
                "containers": [_container()],
                "initContainers": [_container(name, resources=_gpus(10)) for name in "ab"],
                "tolerations": [_GPU_TOLERATION],
            }
        ),
        "GPU 20 exceeds max",
        id="gpu-over-cap-across-init-containers",
    ),
    pytest.param(
        _nested_job(60),
        "Manifest exceeds maximum nesting depth of 32 levels",
        id="nested-past-the-limit",
    ),
    pytest.param(
        _workload({"containers": [_container()]}, namespace="kube-system"),
        "Namespace 'kube-system' not allowed. Allowed namespaces: ['gco-jobs', 'team-a']",
        id="disallowed-namespace",
    ),
    pytest.param(
        {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "parity"}, "data": {}},
        "Resource kind 'Secret' is not allowed",
        id="disallowed-kind",
    ),
    pytest.param(
        _shapeless("Job", {"backoffLimit": 0}),
        "Job manifest must contain a valid pod spec",
        id="job-without-pod-template",
    ),
    pytest.param(
        _shapeless("CronJob", {"schedule": "0 * * * *"}),
        "CronJob manifest must contain a valid pod spec",
        id="cronjob-without-job-template",
    ),
    pytest.param(
        {"apiVersion": "batch/v1", "kind": "Job", "spec": {}},
        "Missing required field: metadata",
        id="missing-metadata",
    ),
    pytest.param(
        {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"namespace": "gco-jobs"}},
        "Missing metadata.name field",
        id="missing-name",
    ),
    pytest.param(
        {"apiVersion": "batch/v1", "kind": "Job", "metadata": None},
        "Validation error: ",
        id="metadata-that-is-not-a-mapping",
    ),
    pytest.param(
        _workload(
            {
                "containers": [_container(resources=_gpus(1))],
                "tolerations": [_GPU_TOLERATION],
                "nodeSelector": {KEEP_WARM_LABEL: "true"},
            }
        ),
        "Keep-warm validation failed: ",
        id="keep-warm-without-its-toleration",
    ),
    pytest.param(
        _workload(
            {
                "containers": [_container(resources=_gpus(1))],
                "tolerations": [_GPU_TOLERATION],
                "runtimeClassName": ISOLATED_SANDBOX,
            }
        ),
        "Nitro Sandbox validation failed: isolated-sandbox: Nitro Sandboxes have no GPU",
        id="gpu-in-a-sandbox",
    ),
    pytest.param(
        _confidential_job(),
        "Nitro Sandbox validation failed: RuntimeClass confidential-sandbox does not exist",
        id="missing-sandbox-runtime-class",
    ),
    pytest.param(
        _workload(
            {"containers": [_container(resources={"requests": {"cpu": "1", "memory": "1Gi"}})]}
        ),
        None,
        id="admitted-job",
    ),
]


class TestOneAdmissionSequence:
    """REST and SQS return the same ``(ok, message)`` under the same policy."""

    @pytest.mark.parametrize("environment", ["shipped", "custom"])
    @pytest.mark.parametrize("document", _example_documents())
    def test_every_example_document_gets_one_verdict(
        self, monkeypatch: pytest.MonkeyPatch, environment: str, document: Any
    ) -> None:
        rest, sqs = _admission_paths(monkeypatch, _ENVIRONMENTS[environment])
        _assert_one_verdict(rest, sqs, document)

    @pytest.mark.parametrize(
        "example", ["simple-job", "gpu-job", "sqs-job-submission", "kubeflow-trainjob"]
    )
    def test_the_examples_documented_for_submit_sqs_are_admitted(
        self, monkeypatch: pytest.MonkeyPatch, example: str
    ) -> None:
        """The examples docs/EXAMPLE_VALIDATION.md runs through ``gco jobs submit-sqs``."""
        rest, sqs = _admission_paths(monkeypatch, _ENVIRONMENTS["shipped"])
        text = (_REPO_ROOT / "examples" / f"{example}.yaml").read_text(encoding="utf-8")
        documents = [document for document in yaml.safe_load_all(text) if document]
        assert documents
        for document in documents:
            assert _assert_one_verdict(rest, sqs, document) == (True, None)

    @pytest.mark.parametrize("environment", ["shipped", "custom"])
    @pytest.mark.parametrize(("manifest", "refusal"), _CRAFTED)
    def test_crafted_manifests_get_one_verdict(
        self,
        monkeypatch: pytest.MonkeyPatch,
        environment: str,
        manifest: Any,
        refusal: str | None,
    ) -> None:
        rest, sqs = _admission_paths(monkeypatch, _ENVIRONMENTS[environment])
        verdict = _assert_one_verdict(rest, sqs, manifest)
        if environment == "custom":
            if refusal is None:
                assert verdict == (True, None)
            else:
                assert verdict[0] is False
                assert refusal in (verdict[1] or ""), verdict


class TestOnePolicy:
    """The SQS consumer builds the policy the REST service builds from the same variables."""

    @pytest.mark.parametrize("environment", sorted(_ENVIRONMENTS))
    def test_the_sqs_policy_is_the_rest_policy(
        self, monkeypatch: pytest.MonkeyPatch, environment: str
    ) -> None:
        rest, sqs = _admission_paths(monkeypatch, _ENVIRONMENTS[environment])
        assert rest.job_validation_policy() == sqs.JOB_VALIDATION_POLICY

    def test_the_empty_values_keep_their_secure_fallbacks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty allowlist of kinds allows nothing; empty trust lists are the shipped ones."""
        from gco.job_admission import DEFAULT_TRUSTED_REGISTRIES

        _, sqs = _admission_paths(monkeypatch, _ENVIRONMENTS["empty-values"])
        policy = sqs.JOB_VALIDATION_POLICY
        assert policy.allowed_kinds == frozenset()
        assert policy.trusted_registries == tuple(sorted(DEFAULT_TRUSTED_REGISTRIES))
        assert policy.security["block_privileged"] is True

    def test_validation_enabled_is_read_by_the_rest_service_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one field that differs: VALIDATION_ENABLED switches REST admission off.

        The SQS ScaledJob does not carry the variable and the consumer does not
        read it, so a queued manifest is judged even where REST validation is off.
        """
        rest, sqs = _admission_paths(monkeypatch, {"VALIDATION_ENABLED": "false"})
        assert rest.job_validation_policy() == sqs.JOB_VALIDATION_POLICY._replace(
            validation_enabled=False
        )
        privileged = _workload({"containers": [_container(securityContext={"privileged": True})]})
        assert rest.validate_manifest(copy.deepcopy(privileged)) == (True, None)
        assert sqs.validate_manifest(copy.deepcopy(privileged)) == (
            False,
            "Security context validation failed: container 'worker': "
            "privileged containers are not permitted",
        )

    @pytest.mark.parametrize(
        ("name", "value", "error"),
        [
            ("MAX_CPU_PER_MANIFEST", "lots", "'lots' is not a Kubernetes quantity"),
            ("MAX_GPU_PER_MANIFEST", "four", "invalid literal for int"),
            ("YAML_MAX_DEPTH", "deep", "invalid literal for int"),
            ("BLOCK_HOST_PATH", "${UNRESOLVED}", "BLOCK_HOST_PATH must be an explicit boolean"),
            ("REQUIRE_ACCELERATOR_TOLERATION", "maybe", "must be an explicit boolean"),
        ],
    )
    def test_both_services_refuse_to_start_on_a_malformed_value(
        self, monkeypatch: pytest.MonkeyPatch, name: str, value: str, error: str
    ) -> None:
        for variable in _POLICY_VARIABLES:
            monkeypatch.delenv(variable, raising=False)
        monkeypatch.setenv(name, value)
        import gco.services.manifest_processor as manifest_processor
        import gco.services.queue_processor as queue_processor

        with (
            patch.object(manifest_processor, "configure_structured_logging"),
            patch.object(manifest_processor, "config"),
            patch.object(manifest_processor, "client"),
            pytest.raises(ValueError, match=error),
        ):
            manifest_processor.create_manifest_processor_from_env()
        with pytest.raises(ValueError, match=error):
            importlib.reload(queue_processor)
