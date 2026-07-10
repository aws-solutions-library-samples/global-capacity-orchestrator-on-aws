"""
Job submission validation tests for the accelerator-toleration requirement
and the parallelism cap.

These two checks are INTENTIONALLY duplicated in the REST ``ManifestProcessor``
(``gco/services/manifest_processor.py``) and the SQS ``queue_processor``
(``gco/services/queue_processor.py``) so the SQS path cannot be used to bypass
admission control. Every scenario here is exercised against BOTH processors so
the two implementations are proven to stay behaviorally identical.
"""

from __future__ import annotations

import importlib
from unittest.mock import patch

import pytest
from kubernetes import config as k8s_config

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
                "max_parallelism": 50,
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
    "MAX_PARALLELISM": "50",
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


def _job(pod_spec_overrides=None, container_overrides=None, spec_overrides=None):
    container = {"name": "worker", "image": "python:3.14"}
    if container_overrides:
        container.update(container_overrides)
    pod_spec = {"containers": [container], "restartPolicy": "Never"}
    if pod_spec_overrides:
        pod_spec.update(pod_spec_overrides)
    spec = {"template": {"spec": pod_spec}}
    if spec_overrides:
        spec.update(spec_overrides)
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


@pytest.mark.parametrize("resource_key,taint_key", _ACCEL_CASES)
def test_accelerator_without_toleration_rejected(manifest_processor, qp, resource_key, taint_key):
    """A job requesting an accelerator but with no toleration is rejected by both paths."""
    manifest = _job(container_overrides={"resources": {"limits": {resource_key: "1"}}})

    (mp_ok, mp_err), (qp_ok, qp_err) = _validate_both(manifest_processor, qp, manifest)

    assert mp_ok is False, f"REST accepted {resource_key} job with no toleration"
    assert qp_ok is False, f"SQS accepted {resource_key} job with no toleration"
    assert taint_key in mp_err
    assert taint_key in qp_err


@pytest.mark.parametrize("resource_key,taint_key", _ACCEL_CASES)
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
# Parallelism cap (item 5)
# ===========================================================================


def test_parallelism_above_cap_rejected(manifest_processor, qp):
    """A Job with parallelism above the cap is rejected by both paths."""
    manifest = _job(spec_overrides={"parallelism": 51})

    (mp_ok, mp_err), (qp_ok, qp_err) = _validate_both(manifest_processor, qp, manifest)

    assert mp_ok is False
    assert qp_ok is False
    assert "51" in mp_err and "50" in mp_err
    assert "51" in qp_err and "50" in qp_err


def test_parallelism_at_cap_admitted(manifest_processor, qp):
    """A Job with parallelism exactly at the cap is admitted."""
    manifest = _job(spec_overrides={"parallelism": 50})

    (mp_ok, _), (qp_ok, _) = _validate_both(manifest_processor, qp, manifest)

    assert mp_ok is True
    assert qp_ok is True


def test_no_parallelism_admitted(manifest_processor, qp):
    """A Job without a parallelism field (K8s default 1) is admitted."""
    manifest = _job()

    (mp_ok, _), (qp_ok, _) = _validate_both(manifest_processor, qp, manifest)

    assert mp_ok is True
    assert qp_ok is True


def test_cronjob_parallelism_above_cap_rejected(manifest_processor, qp):
    """A CronJob whose jobTemplate parallelism exceeds the cap is rejected."""
    manifest = {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {"name": "cron", "namespace": "default"},
        "spec": {
            "schedule": "* * * * *",
            "jobTemplate": {
                "spec": {
                    "parallelism": 99,
                    "template": {
                        "spec": {
                            "restartPolicy": "Never",
                            "containers": [{"name": "worker", "image": "python:3.14"}],
                        }
                    },
                }
            },
        },
    }

    (mp_ok, mp_err), (qp_ok, qp_err) = _validate_both(manifest_processor, qp, manifest)

    assert mp_ok is False
    assert qp_ok is False
    assert "99" in mp_err
    assert "99" in qp_err
