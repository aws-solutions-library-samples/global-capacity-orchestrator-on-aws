"""
Validation tests for the analytics / cluster-shared-bucket example manifests.

Confirms that the three new examples added by the analytics-environment
spec pass the real ManifestProcessor validation with the trusted-registry
configuration that production uses:

- ``examples/cluster-shared-bucket-upload-job.yaml``
- ``examples/analytics-s3-upload-job.yaml``
- ``examples/analytics-database-export-job.yaml``

Also checks that:

1. Every image is pinned to a specific tag (no ``:latest``).
2. At least one of the three examples references the
   ``gco-cluster-shared-bucket`` ConfigMap via ``envFrom`` (this is the
   headline wiring for the two-layer bucket model).

The fixture pattern mirrors ``tests/test_manifest_security_validation.py``
— we mock the kubernetes client so ``ManifestProcessor`` can be
instantiated without a live cluster — and ``tests/test_queue_processor.py``
for the trusted-registry / allowed-namespace environment setup.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from kubernetes import config as k8s_config

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"

ANALYTICS_EXAMPLE_FILES = [
    EXAMPLES_DIR / "cluster-shared-bucket-upload-job.yaml",
    EXAMPLES_DIR / "analytics-s3-upload-job.yaml",
    EXAMPLES_DIR / "analytics-database-export-job.yaml",
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _trusted_registry_env(monkeypatch):
    """Mirror the env setup used by test_queue_processor.test_gpu_limit_from_env.

    Sets TRUSTED_DOCKERHUB_ORGS and ALLOWED_NAMESPACES so the validator
    sees the same allow-lists that production deploys would.
    """
    monkeypatch.setenv("TRUSTED_DOCKERHUB_ORGS", "gco,nvidia,library")
    monkeypatch.setenv(
        "ALLOWED_NAMESPACES",
        "default,gco-jobs,gco-inference,gco-system",
    )


@pytest.fixture
def _mock_k8s_config():
    """Mock Kubernetes configuration loading (no live cluster needed)."""
    with patch("gco.services.manifest_processor.config") as mock_config:
        mock_config.ConfigException = k8s_config.ConfigException
        mock_config.load_incluster_config.side_effect = k8s_config.ConfigException("Not in cluster")
        mock_config.load_kube_config.return_value = None
        yield mock_config


@pytest.fixture
def manifest_processor(_trusted_registry_env, _mock_k8s_config):
    """Build a ManifestProcessor wired with the trusted-registry allow-lists."""
    from gco.services.manifest_processor import ManifestProcessor

    with patch("gco.services.manifest_processor.client"):
        processor = ManifestProcessor(
            cluster_id="test-cluster",
            region="us-east-1",
            config_dict={
                "max_cpu_per_manifest": "10",
                "max_memory_per_manifest": "32Gi",
                "max_gpu_per_manifest": 4,
                "allowed_namespaces": [
                    "default",
                    "gco-jobs",
                    "gco-inference",
                    "gco-system",
                ],
                "validation_enabled": True,
            },
        )
        return processor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_docs(path: Path) -> list[dict[str, Any]]:
    """Load all YAML documents from ``path``, skipping empty ones."""
    with path.open() as fh:
        return [doc for doc in yaml.safe_load_all(fh) if doc]


def _jobs_in(path: Path) -> Iterable[dict[str, Any]]:
    """Yield every ``kind: Job`` document in ``path``."""
    for doc in _load_docs(path):
        if doc.get("kind") == "Job":
            yield doc


def _iter_containers(job: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Yield every container (main + init) defined in a Job manifest."""
    pod_spec = job.get("spec", {}).get("template", {}).get("spec", {})
    yield from pod_spec.get("containers", [])
    yield from pod_spec.get("initContainers", [])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ANALYTICS_EXAMPLE_FILES,
    ids=[p.name for p in ANALYTICS_EXAMPLE_FILES],
)
def test_example_passes_manifest_validation(manifest_processor, path: Path) -> None:
    """Each new example must pass ManifestProcessor.validate_manifest."""
    assert path.exists(), f"Example manifest missing: {path}"

    jobs = list(_jobs_in(path))
    assert jobs, f"Expected at least one Job in {path.name}"

    for job in jobs:
        result = manifest_processor.validate_manifest(job)
        is_valid, error = result
        assert is_valid is True, f"{path.name}: validate_manifest failed with error={error!r}"
        # The success contract is (True, None) — pin it explicitly so a
        # future signature drift shows up here and not in some downstream
        # caller that happens to treat falsy strings as success.
        assert error is None, f"{path.name}: expected None error on success, got {error!r}"


@pytest.mark.parametrize(
    "path",
    ANALYTICS_EXAMPLE_FILES,
    ids=[p.name for p in ANALYTICS_EXAMPLE_FILES],
)
def test_example_is_batch_job_in_gco_jobs_namespace(path: Path) -> None:
    """Each new example must be a Batch Job in the gco-jobs namespace
    using the gco-service-account service account."""
    jobs = list(_jobs_in(path))
    assert jobs, f"Expected at least one Job in {path.name}"

    for job in jobs:
        assert job.get("kind") == "Job", f"{path.name}: expected kind=Job, got {job.get('kind')!r}"
        metadata = job.get("metadata", {})
        assert metadata.get("namespace") == "gco-jobs", (
            f"{path.name}: expected namespace=gco-jobs, got {metadata.get('namespace')!r}"
        )
        pod_spec = job["spec"]["template"]["spec"]
        assert pod_spec.get("serviceAccountName") == "gco-service-account", (
            f"{path.name}: expected serviceAccountName=gco-service-account, "
            f"got {pod_spec.get('serviceAccountName')!r}"
        )


def test_no_example_uses_latest_image_tag() -> None:
    """No image in any new example may use the ``:latest`` tag.

    The examples should pin every image to a reproducible version so
    cluster operators see the same behaviour week to week.
    """
    offenders: list[str] = []
    for path in ANALYTICS_EXAMPLE_FILES:
        for job in _jobs_in(path):
            for container in _iter_containers(job):
                image = container.get("image", "")
                # An unpinned image is either ``name`` with no tag at all
                # or explicitly ``name:latest``. We flag both cases because
                # untagged images resolve to :latest at pull time.
                name_without_digest = image.split("@", 1)[0]
                tag = (
                    name_without_digest.rsplit(":", 1)[1]
                    if ":" in name_without_digest.rsplit("/", 1)[-1]
                    else ""
                )
                if tag == "latest" or tag == "":
                    offenders.append(f"{path.name}:{container.get('name')} -> {image!r}")

    assert not offenders, (
        "Images must be pinned to a specific tag (no :latest, no untagged). "
        "Offenders:\n  " + "\n  ".join(offenders)
    )


def test_at_least_one_example_uses_cluster_shared_bucket_configmap() -> None:
    """At least one of the three examples must wire in the
    gco-cluster-shared-bucket ConfigMap via envFrom.

    This is the headline feature of the two-layer bucket model — if none
    of the examples references the ConfigMap, the documentation story is
    broken.
    """
    configmap_name = "gco-cluster-shared-bucket"
    referencing: list[str] = []

    for path in ANALYTICS_EXAMPLE_FILES:
        for job in _jobs_in(path):
            for container in _iter_containers(job):
                for entry in container.get("envFrom", []) or []:
                    cm_ref = entry.get("configMapRef") or {}
                    if cm_ref.get("name") == configmap_name:
                        referencing.append(path.name)
                        break

    assert referencing, (
        f"No example references the {configmap_name!r} ConfigMap via envFrom. "
        "At least one must demonstrate the two-layer bucket wiring."
    )
