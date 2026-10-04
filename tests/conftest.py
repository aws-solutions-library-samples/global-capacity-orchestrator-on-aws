"""
Pytest configuration and shared fixtures for GCO tests.

This module provides common fixtures used across multiple test modules,
including mock Kubernetes clients, sample manifests, and configuration objects.
"""

import os
import sys
from datetime import UTC, datetime
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
# Process-wide: no ambient AWS credentials reach the test process
# ============================================================================
#
# CI runs the suite with no AWS credentials at all, so any boto3 call a test
# leaves unmocked fails fast with NoCredentialsError there. A developer
# machine is different: ``~/.aws/credentials``, ``AWS_PROFILE``, exported
# keys, or an SSO cache give that same unmocked call a real identity. Caught
# live on 2026-10-04: ``tests/test_stacks.py`` run from a shell with
# administrator credentials, while a live-validation deployment was up,
# drove ``StackManager.destroy_orchestrated`` through two tests whose class
# was exempt from the sweep guard below but mocked only the SG helpers. The
# real implicit log-group sweep deleted the live stacks' Lambda, EKS and
# Container Insights log groups (28 ``DeleteLogGroup`` calls in CloudTrail)
# and the real bastion sweep terminated an operator's SSM bastion; the
# validation harness then refused to tear the deployment down because the
# log-group generations it had checkpointed were gone.
#
# Make every pytest process look like CI before anything else imports boto3:
# drop the credential and profile variables, point the shared credentials
# and config files at paths that do not exist, and disable the instance
# metadata credential provider. The CI job's default Region is mirrored so a
# client built without an explicit Region behaves the same here as there.
# Tests that need credentials set their own (``tests/_floci.py`` installs the
# emulator session's throwaway key over these; ``test_addons_cli.py`` and
# friends export ``testing``), which nests over this and wins.
for _variable in (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_ARN",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_BEARER_TOKEN_BEDROCK",
):
    os.environ.pop(_variable, None)
_NO_AWS_FILE = str(Path(__file__).resolve().parent / ".no-aws-credentials-in-tests")
os.environ["AWS_SHARED_CREDENTIALS_FILE"] = _NO_AWS_FILE
os.environ["AWS_CONFIG_FILE"] = _NO_AWS_FILE
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

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
def _neutralize_lambda_build(ensure_lambda_build_dirs):  # dep order only
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
# Function-scoped: never read the developer's real kubeconfig from tests
# ============================================================================
#
# ``cli.kubectl_helpers.update_kubeconfig`` reads the kubeconfig that
# ``aws eks update-kubeconfig`` would write and, when the cluster entry is
# already pinned to a local tunnel, deliberately leaves it alone and skips the
# refresh. That is the right production behaviour and the wrong thing to let a
# test observe: on a machine where a past ``gco cluster tunnel`` (or a live
# validation run) left ``gco-us-east-1`` pinned in ``~/.kube/config``, every
# test that expects the refresh subprocess to run fails, while CI — with no
# kubeconfig at all — passes. Point ``KUBECONFIG`` at a per-test path that does
# not exist so the helper sees the same empty world everywhere. Tests that
# exercise the pinning itself write their own file and set the variable
# themselves, which nests over this one and wins.


@pytest.fixture(autouse=True)
def _isolated_kubeconfig(monkeypatch, tmp_path):
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / ".kube-isolated" / "config"))


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
# ``StackManager.destroy_orchestrated`` runs a set of boto3/AWS-CLI-backed
# sweeps around the stack deletions: the image-registry preflight, the
# ephemeral-bastion sweep (EC2 TerminateInstances), the backup-vault purge,
# the EKS security-group watchdog and its final pass, the implicit log-group
# sweep (CloudWatch DeleteLogGroup on every Lambda/EKS/Container Insights
# group the stacks imply), bastion IAM retirement, the traffic-dial SSM purge,
# and the post-regional EBS volume sweep. Orchestration tests that mock
# ``destroy`` / ``list_stacks`` but not these helpers otherwise fire real AWS
# calls: slow and non-hermetic, and outright destructive when
# ``config.project_name`` resolves to a live value (see the credential scrub
# at the top of this module for the day that happened).
#
# Every helper is no-oped for every test, except the exact helpers a test
# class (or module-level test) owns and exercises for real with its own boto3
# mocks. The map is per helper, not per class: a class that owns the SG
# watchdog still gets the log-group and bastion sweeps stubbed, which is the
# gap the 2026-10-04 incident fell through. Tests that assert one of these
# methods was called still patch it locally, so their patch nests over this
# one and wins.
_DESTROY_CLEANUP_STUBS: dict[str, object] = {
    "_image_registry_destroy_preflight": True,
    "cleanup_orphaned_bastions": 0,
    "_cleanup_backup_vault": None,
    "_cleanup_eks_security_groups": None,
    "_start_eks_sg_watchdog": MagicMock(),
    "_collect_implicit_log_groups": {},
    "_cleanup_implicit_log_groups": {"deleted": [], "missing": [], "errors": []},
    "_cleanup_bastion_iam": {"completed_steps": 0, "absent_steps": 0, "errors": []},
    "_cleanup_traffic_dial_parameters": {"deleted": [], "errors": []},
    # The post-regional EBS sweep calls EC2 DeleteVolume. Left real, an
    # orchestration test that names a live regional stack would destroy that
    # cluster's Prometheus/Grafana data — the most destructive helper here.
    "_cleanup_cluster_volumes": {"deleted": [], "surviving": [], "errors": []},
}
# Class name (or module-level test function name) -> the helpers it owns.
_DESTROY_CLEANUP_OWNERS: dict[str, frozenset[str]] = {
    "TestImageRegistryDestroyPreflight": frozenset({"_image_registry_destroy_preflight"}),
    "TestCleanupOrphanedBastions": frozenset({"cleanup_orphaned_bastions"}),
    "test_cleanup_orphaned_bastions_filters_stacks_and_parallelizes": frozenset(
        {"cleanup_orphaned_bastions"}
    ),
    "test_cleanup_orphaned_bastions_default_strict_and_empty_inputs": frozenset(
        {"cleanup_orphaned_bastions"}
    ),
    "TestCleanupBackupVault": frozenset({"_cleanup_backup_vault"}),
    "TestEksSecurityGroupCleanup": frozenset({"_cleanup_eks_security_groups"}),
    "TestCleanupEksSecurityGroups": frozenset({"_cleanup_eks_security_groups"}),
    "TestEksSgWatchdog": frozenset({"_start_eks_sg_watchdog", "_cleanup_eks_security_groups"}),
    "TestImplicitLogGroupCleanup": frozenset(
        {"_collect_implicit_log_groups", "_cleanup_implicit_log_groups"}
    ),
    "TestBastionIamCleanup": frozenset({"_cleanup_bastion_iam"}),
    "TestTrafficDialParameterCleanup": frozenset({"_cleanup_traffic_dial_parameters"}),
    "TestClusterVolumeCleanup": frozenset({"_cleanup_cluster_volumes"}),
    # Floci layer: drives the real method against the local emulator, so the
    # no-op stub would defeat the entire point of the module.
    "TestClusterVolumeSweepOverTheWire": frozenset({"_cleanup_cluster_volumes"}),
    # Patches every sweep itself to assert the wiring; owns none for real.
    "TestDestroyOrchestratedImplicitCleanupWiring": frozenset(),
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
    # Floci layer: drives the real pre-check against a rolled-back stack in
    # the local emulator, so the no-op stub would defeat the module.
    "TestStuckStackRecoveryOverTheWire",
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
    owner = request.cls.__name__ if request.cls is not None else request.function.__name__
    owned = _DESTROY_CLEANUP_OWNERS.get(owner, frozenset())
    from contextlib import ExitStack

    from cli import stacks as _stacks

    with ExitStack() as stack:
        for helper, stub in _DESTROY_CLEANUP_STUBS.items():
            if helper in owned:
                continue
            stack.enter_context(patch.object(_stacks.StackManager, helper, return_value=stub))
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
        kubernetes_version="1.37",
        addons=["metrics-server"],
        resource_thresholds=sample_thresholds,
    )


@pytest.fixture
def sample_health_status(sample_thresholds, sample_utilization):
    """Create sample health status."""
    return HealthStatus(
        cluster_id="gco-us-east-1",
        region="us-east-1",
        timestamp=datetime.now(UTC),
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
        "kubernetes_version": "1.37",
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
