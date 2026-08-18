"""
Pytest configuration and shared fixtures for GCO tests.

This module provides common fixtures used across multiple test modules,
including mock Kubernetes clients, sample manifests, and configuration objects.
"""

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

# tests/_floci.py hosts the session fixtures for the opt-in Floci emulator
# layer (see docs/FLOCI_TESTING.md). Registering it as a plugin makes those
# fixtures resolvable from the tests/test_floci_*.py modules without each of
# them re-importing fixture symbols; when GCO_FLOCI_ENDPOINT is unset the
# modules skip at collection time and none of these fixtures ever run.
pytest_plugins = ["tests._floci"]

# ============================================================================
# Session-scoped: ensure Lambda build directories exist for CDK tests
# ============================================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def ensure_lambda_build_dirs():
    """Prepare every ignored Lambda asset before in-process CDK synthesis.

    CI normally supplies source-current assets through the composite build
    action. The shared production entry point is still invoked here so direct
    pytest execution from a fresh checkout has the same precondition as raw
    ``app.py`` and CLI-managed CDK execution.
    """
    from cli.stacks import cdk_asset_consumer

    with cdk_asset_consumer(PROJECT_ROOT):
        yield


# ============================================================================
# Session-scoped: neutralize StackManager Lambda rebuilds during tests
# ============================================================================
#
# ``StackManager.synth()`` / ``diff()`` and ``deploy()`` all call
# ``_ensure_lambda_build()``. Production builders now use per-asset
# interprocess locks, unique staging trees, completion manifests, and atomic
# rename publication, so they never mutate a final build directory in place.
# Tests still should not perform real pip/npm installs against the checkout:
# the composite action prepares it before pytest in CI, and
# ``ensure_lambda_build_dirs`` above handles the local-development case.
# Patch only the real repository root; tests that intentionally exercise asset
# preparation against ``tmp_path`` continue through the production code.
@pytest.fixture(scope="session", autouse=True)
def _neutralize_lambda_build(ensure_lambda_build_dirs):  # noqa: ARG001 — dep order only
    from cli import stacks as _stacks

    real_root = PROJECT_ROOT.resolve()
    orig_ensure = _stacks.StackManager._ensure_lambda_build

    def _guarded_ensure(self):
        try:
            same = Path(self.project_root).resolve() == real_root
        except OSError:
            same = False
        if same:
            return
        return orig_ensure(self)

    _stacks.StackManager._ensure_lambda_build = _guarded_ensure
    try:
        yield
    finally:
        _stacks.StackManager._ensure_lambda_build = orig_ensure


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
# Function-scoped: never touch the real mission-memory table from tests
# ============================================================================
#
# The Mission engine factory wires a ``MissionMemoryStore`` into every
# live-dispatcher engine: terminal verdicts write a memory item (SSM name
# lookup -> Bedrock embedding -> DynamoDB PutItem) and sampling sessions
# retrieve similar past missions. Both paths are best-effort and swallow
# every failure, so on a credential-less CI host they silently no-op — but on
# a developer machine with live credentials and a deployed stack they would
# embed and write *test* sessions into the real institutional-memory table.
# Neutralise the single construction seam for every test; memory-specific
# tests construct engines directly with stub stores (or patch this seam
# themselves, which nests over this one and wins).


_GCO_MCP_PATH = str(PROJECT_ROOT / "gco_mcp")


@pytest.fixture(autouse=True)
def _no_real_mission_memory():
    if _GCO_MCP_PATH not in sys.path:
        sys.path.insert(0, _GCO_MCP_PATH)
    from mission import _engine_factory as _factory

    with patch.object(_factory, "_build_memory_store", return_value=None):
        yield


# ============================================================================
# Function-scoped: never make real AWS calls from the destroy-cleanup helpers
# ============================================================================
#
# ``StackManager.destroy_orchestrated`` invokes three boto3-backed cleanup
# helpers — ``_cleanup_backup_vault`` (deletes backup recovery points),
# ``_start_eks_sg_watchdog`` (spawns a thread that polls EC2), and
# ``_cleanup_eks_security_groups`` (deletes EKS-owned SGs + their ENIs).
# Orchestration tests that mock ``destroy`` / ``list_stacks`` but not these
# helpers otherwise fire real AWS calls: slow and non-hermetic, and outright
# destructive if ``config.project_name`` resolved to a live value. No-op them
# for every test except the classes that exercise them directly (those mock
# boto3 themselves); tests that assert these methods were called still patch
# them locally, so their patch nests over this one and wins.
_DESTROY_CLEANUP_OWNERS = {
    "TestCleanupBackupVault",
    "TestEksSecurityGroupCleanup",
    "TestCleanupEksSecurityGroups",
    "TestEksSgWatchdog",
    "TestImplicitLogGroupCleanup",
    "TestBastionIamCleanup",
    "TestDestroyOrchestratedImplicitCleanupWiring",
}


# ============================================================================
# Function-scoped: never run the stuck-stack pre-check against real AWS
# ============================================================================
#
# ``StackManager.deploy()`` runs ``_check_and_fix_stuck_stack`` before every
# deployment. Left real, it creates its own boto3 CloudFormation client and
# calls ``describe_stacks`` — and for a stack in a genuinely stuck state
# (ROLLBACK_COMPLETE and friends) it proceeds to ``delete_stack``. That is
# non-hermetic in both directions: on a developer machine with live
# credentials, any ``test_deploy_*`` case that names a real stack reads it (and
# in a stuck state would *delete* it); on a shard worker where an earlier test
# leaked a ``boto3.client`` mock, ``describe_stacks`` returns a MagicMock and
# the identity validation fails with "CloudFormation returned an invalid
# identity" — an order-dependent failure that surfaced when sharding changed
# worker composition. No-op the pre-check for every test except the classes
# that exercise it directly with their own boto3 mocks; tests that assert it
# was called still patch it locally, so their patch nests over this one and
# wins.
_STUCK_STACK_PRECHECK_OWNERS = {
    "TestCheckAndFixStuckStack",
    "TestStrictDeployStackOwnership",
}


@pytest.fixture(autouse=True)
def _no_real_stuck_stack_precheck(request):
    if request.cls is not None and request.cls.__name__ in _STUCK_STACK_PRECHECK_OWNERS:
        yield
        return
    from cli import stacks as _stacks

    with patch.object(_stacks.StackManager, "_check_and_fix_stuck_stack", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _no_real_destroy_cleanup_aws_calls(request):
    if request.cls is not None and request.cls.__name__ in _DESTROY_CLEANUP_OWNERS:
        yield
        return
    from cli import stacks as _stacks

    with (
        patch.object(_stacks.StackManager, "_cleanup_backup_vault", return_value=None),
        patch.object(_stacks.StackManager, "_cleanup_eks_security_groups", return_value=None),
        patch.object(_stacks.StackManager, "_start_eks_sg_watchdog", return_value=MagicMock()),
        # The implicit log-group + bastion IAM sweep added for non-strict
        # teardowns is boto3/AWS-CLI-backed as well; orchestration tests
        # that don't own these helpers must never fire them for real.
        patch.object(_stacks.StackManager, "_collect_implicit_log_groups", return_value={}),
        patch.object(
            _stacks.StackManager,
            "_cleanup_implicit_log_groups",
            return_value={"deleted": [], "missing": [], "errors": []},
        ),
        patch.object(
            _stacks.StackManager,
            "_cleanup_bastion_iam",
            return_value={"completed_steps": 0, "absent_steps": 0, "errors": []},
        ),
    ):
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
            "allowed_namespaces": ["gco-jobs"],
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
