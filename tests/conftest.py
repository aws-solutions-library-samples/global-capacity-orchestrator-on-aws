"""
Pytest configuration and shared fixtures for GCO tests.

This module provides common fixtures used across multiple test modules,
including mock Kubernetes clients, sample manifests, and configuration objects.
"""

import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gco.models import (
    ClusterConfig,
    HealthStatus,
    ResourceThresholds,
    ResourceUtilization,
)

# ============================================================================
# Session-scoped: ensure Lambda build directories exist for CDK tests
# ============================================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def ensure_lambda_build_dirs():
    """Ensure Lambda build directories exist before any CDK synthesis tests.

    CDK's Code.from_asset() fingerprints these directories during synthesis.
    If they're missing or stale, CDK tests fail with ENOENT errors.
    This fixture runs once per test session and rebuilds if needed.
    """
    kubectl_build = PROJECT_ROOT / "lambda" / "kubectl-applier-simple-build"
    helm_build = PROJECT_ROOT / "lambda" / "helm-installer-build"

    # Rebuild kubectl-applier-simple-build if handler.py is missing
    if not (kubectl_build / "handler.py").exists():
        kubectl_build.mkdir(parents=True, exist_ok=True)
        shutil.copy2(
            PROJECT_ROOT / "lambda" / "kubectl-applier-simple" / "handler.py",
            kubectl_build / "handler.py",
        )
        shutil.copytree(
            PROJECT_ROOT / "lambda" / "kubectl-applier-simple" / "manifests",
            kubectl_build / "manifests",
            dirs_exist_ok=True,
        )
        subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - test fixture: static list [sys.executable,"-m","pip","install",...]; no user input
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "kubernetes",
                "pyyaml",
                "urllib3",
                "-t",
                str(kubectl_build),
                "-q",
            ],
            check=True,
        )

    # Rebuild helm-installer-build if handler.py is missing
    if not (helm_build / "handler.py").exists():
        if helm_build.exists():
            shutil.rmtree(helm_build)
        shutil.copytree(
            PROJECT_ROOT / "lambda" / "helm-installer",
            helm_build,
        )
        # Remove __pycache__ from the copy
        for pycache in helm_build.rglob("__pycache__"):
            shutil.rmtree(pycache)


# ============================================================================
# Session-scoped: neutralize StackManager's self-healing Lambda rebuild during tests
# ============================================================================
#
# ``StackManager.__init__`` calls ``_ensure_lambda_build()`` (and its downstream
# ``_build_kubectl_lambda``) as a self-healing step so any ``gco stacks
# deploy`` succeeds even when a contributor's build tree is stale. That's the
# right behavior at runtime, but it's destructive during tests:
#
#   1. ``_build_kubectl_lambda`` does ``_safe_rmtree(build_dir)`` on the *real*
#      ``lambda/kubectl-applier-simple-build/`` whenever its guard (``yaml/``
#      missing) trips.
#   2. Under pytest-xdist, one worker's rebuild races with another worker's
#      CDK ``Code.from_asset()`` mid-copy, producing the sporadic
#      ``ENOENT: … lstat '…lambda/kubectl-applier-simple-build/botocore/data/…``
#      failures we see on the 2-vCPU CI runner.
#   3. Any test that mocks ``subprocess.run`` while constructing a
#      ``StackManager`` can silently short-circuit the pip-install step and
#      leave the build tree partially populated, which then trips the guard
#      on the NEXT construction and cascades a rebuild.
#   4. ``deploy()`` calls ``_rebuild_lambda_packages()`` which rm-trees and
#      pip-installs into the real build dir even when ``_run_cdk`` is
#      mocked — so every ``test_deploy_*`` hits the real filesystem too.
#
# Tests should never rebuild the *real* Lambda tree. The composite action
# (``.github/actions/build-lambda-package``) populates it before pytest runs
# in CI, and ``ensure_lambda_build_dirs`` above handles the local-dev case.
# Patching ``_ensure_lambda_build`` and ``_rebuild_lambda_packages`` to skip
# when ``project_root`` points at the real repo makes xdist safe; tests that
# intentionally exercise these methods against a ``tmp_path`` keep working
# because the guard lets them through.
@pytest.fixture(scope="session", autouse=True)
def _neutralize_lambda_build(ensure_lambda_build_dirs):  # noqa: ARG001 — dep order only
    from cli import stacks as _stacks

    real_root = PROJECT_ROOT.resolve()
    orig_ensure = _stacks.StackManager._ensure_lambda_build
    orig_rebuild = _stacks.StackManager._rebuild_lambda_packages

    def _guarded_ensure(self):
        try:
            same = Path(self.project_root).resolve() == real_root
        except OSError:
            same = False
        if same:
            return
        return orig_ensure(self)

    def _guarded_rebuild(self):
        try:
            same = Path(self.project_root).resolve() == real_root
        except OSError:
            same = False
        if same:
            return
        return orig_rebuild(self)

    _stacks.StackManager._ensure_lambda_build = _guarded_ensure
    _stacks.StackManager._rebuild_lambda_packages = _guarded_rebuild
    try:
        yield
    finally:
        _stacks.StackManager._ensure_lambda_build = orig_ensure
        _stacks.StackManager._rebuild_lambda_packages = orig_rebuild


# ============================================================================
# Function-scoped: never run the real image-mirror hook during unit tests
# ============================================================================
#
# cdk.json ships ``volcano_image_mirror.enabled=true``, so ``StackManager.deploy()``
# invokes ``_mirror_images_if_enabled`` on every call. Left real, that reaches
# boto3 STS / a container runtime and fails the many ``test_deploy_*`` cases with
# ``NoCredentialsError`` (and would attempt real ECR copies) on CI. No-op the hook
# for every test except ``TestAutoMirrorOnDeploy``, which exercises the hook itself
# with the mirror core mocked.
@pytest.fixture(autouse=True)
def _no_real_image_mirror(request):
    if request.cls is not None and request.cls.__name__ == "TestAutoMirrorOnDeploy":
        yield
        return
    from cli import stacks as _stacks

    with patch.object(_stacks.StackManager, "_mirror_images_if_enabled", return_value=None):
        yield


# ============================================================================
# Model Fixtures
# ============================================================================


@pytest.fixture
def sample_thresholds():
    """Create sample resource thresholds."""
    return ResourceThresholds(cpu_threshold=80, memory_threshold=85, gpu_threshold=90)


@pytest.fixture
def sample_utilization():
    """Create sample resource utilization."""
    return ResourceUtilization(cpu=50.0, memory=60.0, gpu=30.0)


@pytest.fixture
def sample_cluster_config(sample_thresholds):
    """Create sample cluster configuration."""
    return ClusterConfig(
        region="us-east-1",
        cluster_name="gco-us-east-1",
        kubernetes_version="1.36",
        addons=["metrics-server"],
        resource_thresholds=sample_thresholds,
    )


@pytest.fixture
def sample_health_status(sample_thresholds, sample_utilization):
    """Create sample health status."""
    return HealthStatus(
        cluster_id="gco-us-east-1",
        region="us-east-1",
        timestamp=datetime.now(),
        status="healthy",
        resource_utilization=sample_utilization,
        thresholds=sample_thresholds,
        active_jobs=5,
    )


# ============================================================================
# Kubernetes Manifest Fixtures
# ============================================================================


@pytest.fixture
def sample_deployment_manifest():
    """Create sample Kubernetes Deployment manifest."""
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "test-app", "namespace": "default"},
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": {"app": "test"}},
            "template": {
                "metadata": {"labels": {"app": "test"}},
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "image": "docker.io/nginx:latest",
                            "ports": [{"containerPort": 80}],
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "128Mi"},
                                "limits": {"cpu": "500m", "memory": "512Mi"},
                            },
                        }
                    ]
                },
            },
        },
    }


@pytest.fixture
def sample_job_manifest():
    """Create sample Kubernetes Job manifest."""
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "test-job", "namespace": "gco-jobs"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "worker",
                            "image": "public.ecr.aws/test/worker:v1",
                            "resources": {
                                "requests": {"cpu": "1", "memory": "2Gi"},
                                "limits": {"cpu": "2", "memory": "4Gi"},
                            },
                        }
                    ],
                    "restartPolicy": "Never",
                }
            }
        },
    }


@pytest.fixture
def sample_gpu_job_manifest():
    """Create sample GPU Job manifest."""
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "gpu-training-job", "namespace": "gco-jobs"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "trainer",
                            "image": "docker.io/pytorch/pytorch:latest",
                            "resources": {
                                "requests": {"cpu": "4", "memory": "16Gi", "nvidia.com/gpu": "1"},
                                "limits": {"cpu": "8", "memory": "32Gi", "nvidia.com/gpu": "1"},
                            },
                        }
                    ],
                    "restartPolicy": "Never",
                    "tolerations": [
                        {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}
                    ],
                }
            }
        },
    }


@pytest.fixture
def sample_configmap_manifest():
    """Create sample ConfigMap manifest."""
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "test-config", "namespace": "default"},
        "data": {"config.yaml": "key: value\nother: setting"},
    }


# ============================================================================
# Mock Fixtures
# ============================================================================


@pytest.fixture
def mock_k8s_config():
    """Mock Kubernetes configuration loading."""
    with (
        patch("kubernetes.config.load_incluster_config") as mock_incluster,
        patch("kubernetes.config.load_kube_config") as mock_kubeconfig,
    ):
        mock_incluster.side_effect = Exception("Not in cluster")
        mock_kubeconfig.return_value = None
        yield {"incluster": mock_incluster, "kubeconfig": mock_kubeconfig}


@pytest.fixture
def mock_k8s_clients():
    """Mock Kubernetes API clients."""
    with (
        patch("kubernetes.client.CoreV1Api") as mock_core,
        patch("kubernetes.client.AppsV1Api") as mock_apps,
        patch("kubernetes.client.BatchV1Api") as mock_batch,
        patch("kubernetes.client.CustomObjectsApi") as mock_custom,
    ):
        yield {
            "core_v1": mock_core.return_value,
            "apps_v1": mock_apps.return_value,
            "batch_v1": mock_batch.return_value,
            "custom_objects": mock_custom.return_value,
        }


@pytest.fixture
def mock_secrets_manager():
    """Mock AWS Secrets Manager client."""
    with patch("boto3.client") as mock_boto:
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {
            "SecretString": '{"token": "test-secret-token"}'
        }
        mock_boto.return_value = mock_client
        yield mock_client


# ============================================================================
# Configuration Fixtures
# ============================================================================


@pytest.fixture
def valid_cdk_context():
    """Create valid CDK context for ConfigLoader tests."""
    return {
        "project_name": "gco",
        "deployment_regions": {
            "global": "us-east-2",
            "api_gateway": "us-east-2",
            "monitoring": "us-east-2",
            "regional": ["us-east-1", "us-west-2"],
        },
        "kubernetes_version": "1.36",
        "resource_thresholds": {"cpu_threshold": 80, "memory_threshold": 85, "gpu_threshold": 90},
        "global_accelerator": {
            "name": "gco-accelerator",
            "health_check_grace_period": 30,
            "health_check_interval": 30,
            "health_check_timeout": 5,
            "health_check_path": "/api/v1/health",
        },
        "alb_config": {
            "health_check_interval": 30,
            "health_check_timeout": 5,
            "healthy_threshold": 2,
            "unhealthy_threshold": 2,
        },
        "manifest_processor": {
            "image": "gco/manifest-processor:latest",
            "replicas": 3,
            "resource_limits": {"cpu": "1000m", "memory": "2Gi"},
        },
        "job_validation_policy": {
            "allowed_namespaces": ["default", "gco-jobs"],
            "resource_quotas": {
                "max_cpu_per_manifest": "10",
                "max_memory_per_manifest": "32Gi",
                "max_gpu_per_manifest": 4,
            },
        },
        "api_gateway": {
            "throttle_rate_limit": 1000,
            "throttle_burst_limit": 2000,
            "log_level": "INFO",
            "metrics_enabled": True,
            "tracing_enabled": True,
        },
        "tags": {"Environment": "test", "Project": "gco"},
    }
