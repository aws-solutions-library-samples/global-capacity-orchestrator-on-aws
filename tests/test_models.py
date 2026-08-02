"""
Unit tests for gco/models data classes.

Covers validation rules across the model surface: ResourceThresholds
(boundary values, -1 disable sentinel, out-of-range rejection),
ResourceUtilization, HealthStatus,
KubernetesManifest, ManifestSubmissionRequest/Response, and
ResourceStatus. Each dataclass enforces invariants in __post_init__,
and these tests pin the error messages so callers can rely on them.
"""

from datetime import datetime

import pytest

from gco.models import (
    HealthStatus,
    KubernetesManifest,
    ManifestSubmissionRequest,
    ManifestSubmissionResponse,
    ResourceStatus,
    ResourceThresholds,
    ResourceUtilization,
)


class TestResourceThresholds:
    """Tests for ResourceThresholds model."""

    def test_valid_thresholds(self):
        """Test creating valid thresholds."""
        thresholds = ResourceThresholds(cpu_threshold=80, memory_threshold=85, gpu_threshold=90)
        assert thresholds.cpu_threshold == 80
        assert thresholds.memory_threshold == 85
        assert thresholds.gpu_threshold == 90

    def test_boundary_values(self):
        """Test boundary values (0 and 100)."""
        thresholds = ResourceThresholds(cpu_threshold=0, memory_threshold=100, gpu_threshold=50)
        assert thresholds.cpu_threshold == 0
        assert thresholds.memory_threshold == 100

    def test_invalid_cpu_threshold_negative(self):
        """Test that negative CPU threshold (other than -1) raises error."""
        with pytest.raises(ValueError, match="cpu_threshold"):
            ResourceThresholds(cpu_threshold=-2, memory_threshold=85, gpu_threshold=90)

    def test_disabled_cpu_threshold(self):
        """Test that -1 disables the CPU threshold check."""
        thresholds = ResourceThresholds(cpu_threshold=-1, memory_threshold=85, gpu_threshold=90)
        assert thresholds.cpu_threshold == -1
        assert thresholds.is_disabled("cpu_threshold") is True
        assert thresholds.is_disabled("memory_threshold") is False

    def test_invalid_cpu_threshold_over_100(self):
        """Test that CPU threshold over 100 raises error."""
        with pytest.raises(ValueError, match="cpu_threshold"):
            ResourceThresholds(cpu_threshold=101, memory_threshold=85, gpu_threshold=90)

    def test_invalid_memory_threshold(self):
        """Test that invalid memory threshold raises error."""
        with pytest.raises(ValueError, match="memory_threshold"):
            ResourceThresholds(cpu_threshold=80, memory_threshold=150, gpu_threshold=90)

    def test_invalid_gpu_threshold(self):
        """Test that invalid GPU threshold raises error."""
        with pytest.raises(ValueError, match="gpu_threshold"):
            ResourceThresholds(cpu_threshold=80, memory_threshold=85, gpu_threshold=-10)


class TestResourceUtilization:
    """Tests for ResourceUtilization model."""

    def test_valid_utilization(self):
        """Test creating valid utilization."""
        util = ResourceUtilization(cpu=45.5, memory=62.3, gpu=0.0)
        assert util.cpu == 45.5
        assert util.memory == 62.3
        assert util.gpu == 0.0

    def test_boundary_values(self):
        """Test boundary values."""
        util = ResourceUtilization(cpu=0.0, memory=100.0, gpu=50.0)
        assert util.cpu == 0.0
        assert util.memory == 100.0

    def test_invalid_cpu_negative(self):
        """Test that negative CPU raises error."""
        with pytest.raises(ValueError, match="cpu"):
            ResourceUtilization(cpu=-1.0, memory=50.0, gpu=0.0)

    def test_invalid_memory_over_100(self):
        """Test that memory over 100 raises error."""
        with pytest.raises(ValueError, match="memory"):
            ResourceUtilization(cpu=50.0, memory=101.0, gpu=0.0)


class TestHealthStatus:
    """Tests for HealthStatus model."""

    @pytest.fixture
    def thresholds(self):
        """Create test thresholds."""
        return ResourceThresholds(cpu_threshold=80, memory_threshold=85, gpu_threshold=90)

    @pytest.fixture
    def utilization(self):
        """Create test utilization."""
        return ResourceUtilization(cpu=50.0, memory=60.0, gpu=0.0)

    def test_valid_healthy_status(self, thresholds, utilization):
        """Test creating valid healthy status."""
        status = HealthStatus(
            cluster_id="gco-us-east-1",
            region="us-east-1",
            timestamp=datetime.now(),
            status="healthy",
            resource_utilization=utilization,
            thresholds=thresholds,
            active_jobs=5,
        )
        assert status.status == "healthy"
        assert status.is_healthy()

    def test_valid_unhealthy_status(self, thresholds, utilization):
        """Test creating valid unhealthy status."""
        status = HealthStatus(
            cluster_id="gco-us-east-1",
            region="us-east-1",
            timestamp=datetime.now(),
            status="unhealthy",
            resource_utilization=utilization,
            thresholds=thresholds,
            active_jobs=5,
            message="High CPU utilization",
        )
        assert status.status == "unhealthy"

    def test_empty_cluster_id_raises_error(self, thresholds, utilization):
        """Test that empty cluster_id raises error."""
        with pytest.raises(ValueError, match="Cluster ID cannot be empty"):
            HealthStatus(
                cluster_id="",
                region="us-east-1",
                timestamp=datetime.now(),
                status="healthy",
                resource_utilization=utilization,
                thresholds=thresholds,
                active_jobs=5,
            )

    def test_invalid_status_value(self, thresholds, utilization):
        """Test that invalid status value raises error."""
        with pytest.raises(ValueError, match="Status must be"):
            HealthStatus(
                cluster_id="gco-us-east-1",
                region="us-east-1",
                timestamp=datetime.now(),
                status="unknown",
                resource_utilization=utilization,
                thresholds=thresholds,
                active_jobs=5,
            )

    def test_negative_active_jobs_raises_error(self, thresholds, utilization):
        """Test that negative active_jobs raises error."""
        with pytest.raises(ValueError, match="Active jobs count cannot be negative"):
            HealthStatus(
                cluster_id="gco-us-east-1",
                region="us-east-1",
                timestamp=datetime.now(),
                status="healthy",
                resource_utilization=utilization,
                thresholds=thresholds,
                active_jobs=-1,
            )

    def test_get_threshold_violations(self, thresholds):
        """Test getting threshold violations."""
        high_util = ResourceUtilization(cpu=85.0, memory=90.0, gpu=0.0)
        status = HealthStatus(
            cluster_id="gco-us-east-1",
            region="us-east-1",
            timestamp=datetime.now(),
            status="unhealthy",
            resource_utilization=high_util,
            thresholds=thresholds,
            active_jobs=5,
        )
        violations = status.get_threshold_violations()
        assert len(violations) == 2
        assert any("CPU" in v for v in violations)
        assert any("Memory" in v for v in violations)


class TestKubernetesManifest:
    """Tests for KubernetesManifest model."""

    def test_valid_deployment_manifest(self):
        """Test creating valid deployment manifest."""
        manifest = KubernetesManifest(
            apiVersion="apps/v1",
            kind="Deployment",
            metadata={"name": "test-app", "namespace": "default"},
            spec={"replicas": 2, "selector": {"matchLabels": {"app": "test"}}},
        )
        assert manifest.kind == "Deployment"
        assert manifest.get_name() == "test-app"
        assert manifest.get_namespace() == "default"

    def test_valid_configmap_manifest(self):
        """Test creating valid ConfigMap manifest."""
        manifest = KubernetesManifest(
            apiVersion="v1",
            kind="ConfigMap",
            metadata={"name": "test-config", "namespace": "default"},
            data={"key": "value"},
        )
        assert manifest.kind == "ConfigMap"
        assert manifest.data == {"key": "value"}

    def test_missing_namespace_defaults_to_write_rbac_namespace(self):
        manifest = KubernetesManifest(
            apiVersion="v1",
            kind="ConfigMap",
            metadata={"name": "test-config"},
            data={"key": "value"},
        )

        assert manifest.get_namespace() == "gco-jobs"

    def test_valid_namespace_manifest(self):
        """Test creating valid Namespace manifest (no spec/data required)."""
        manifest = KubernetesManifest(
            apiVersion="v1", kind="Namespace", metadata={"name": "test-namespace"}
        )
        assert manifest.kind == "Namespace"

    def test_empty_api_version_raises_error(self):
        """Test that empty apiVersion raises error."""
        with pytest.raises(ValueError, match="apiVersion cannot be empty"):
            KubernetesManifest(apiVersion="", kind="Deployment", metadata={"name": "test"})

    def test_missing_name_raises_error(self):
        """Test that missing name in metadata raises error."""
        with pytest.raises(ValueError, match="metadata must contain a 'name' field"):
            KubernetesManifest(
                apiVersion="apps/v1", kind="Deployment", metadata={"namespace": "default"}
            )

    def test_to_dict(self):
        """Test converting manifest to dictionary."""
        manifest = KubernetesManifest(
            apiVersion="v1", kind="ConfigMap", metadata={"name": "test"}, data={"key": "value"}
        )
        d = manifest.to_dict()
        assert d["apiVersion"] == "v1"
        assert d["kind"] == "ConfigMap"
        assert d["data"] == {"key": "value"}
        assert "spec" not in d

    def test_from_dict(self):
        """Test creating manifest from dictionary."""
        data = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "test", "namespace": "default"},
            "spec": {"replicas": 1},
        }
        manifest = KubernetesManifest.from_dict(data)
        assert manifest.kind == "Deployment"
        assert manifest.spec["replicas"] == 1


class TestResourceStatus:
    """Tests for ResourceStatus model."""

    def test_valid_created_status(self):
        """Test creating valid 'created' status."""
        status = ResourceStatus(
            api_version="apps/v1",
            kind="Deployment",
            name="test-app",
            namespace="default",
            status="created",
            message="Resource created successfully",
        )
        assert status.is_successful()
        assert status.get_resource_identifier() == "apps/v1/Deployment/default/test-app"

    def test_valid_failed_status(self):
        """Test creating valid 'failed' status."""
        status = ResourceStatus(
            api_version="apps/v1",
            kind="Deployment",
            name="test-app",
            namespace="default",
            status="failed",
            message="Validation error",
        )
        assert not status.is_successful()

    def test_valid_forbidden_status_is_not_successful(self):
        """Authorization denials are explicit unsuccessful results."""
        status = ResourceStatus(
            api_version="v1",
            kind="Secret",
            name="credentials",
            namespace="default",
            status="forbidden",
            message="Resource kind 'Secret' is not allowed",
        )

        assert not status.is_successful()

    def test_invalid_status_value(self):
        """Test that invalid status value raises error."""
        with pytest.raises(ValueError, match="Status must be one of"):
            ResourceStatus(
                api_version="apps/v1",
                kind="Deployment",
                name="test-app",
                namespace="default",
                status="pending",
            )

    def test_empty_name_raises_error(self):
        """Test that empty name raises error."""
        with pytest.raises(ValueError, match="Resource name cannot be empty"):
            ResourceStatus(
                api_version="apps/v1",
                kind="Deployment",
                name="",
                namespace="default",
                status="created",
            )


class TestManifestSubmissionRequest:
    """Tests for ManifestSubmissionRequest model."""

    def test_valid_request(self):
        """Test creating valid submission request."""
        manifests = [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "test"},
                "data": {"key": "value"},
            }
        ]
        request = ManifestSubmissionRequest(manifests=manifests)
        assert request.get_resource_count() == 1
        assert not request.dry_run

    def test_dry_run_request(self):
        """Test creating dry run request."""
        manifests = [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "test"},
                "data": {"key": "value"},
            }
        ]
        request = ManifestSubmissionRequest(manifests=manifests, dry_run=True)
        assert request.dry_run

    def test_empty_manifests_raises_error(self):
        """Test that empty manifests list raises error."""
        with pytest.raises(ValueError, match="At least one manifest must be provided"):
            ManifestSubmissionRequest(manifests=[])

    def test_invalid_manifest_raises_error(self):
        """Test that invalid manifest raises error."""
        manifests = [{"apiVersion": "v1", "kind": "ConfigMap"}]  # Missing metadata
        with pytest.raises(ValueError, match="Invalid manifest"):
            ManifestSubmissionRequest(manifests=manifests)


class TestManifestSubmissionResponse:
    """Tests for ManifestSubmissionResponse model."""

    def test_valid_success_response(self):
        """Test creating valid success response."""
        resources = [
            ResourceStatus(
                api_version="v1",
                kind="ConfigMap",
                name="test",
                namespace="default",
                status="created",
            )
        ]
        response = ManifestSubmissionResponse(
            success=True, cluster_id="gco-us-east-1", region="us-east-1", resources=resources
        )
        assert response.success
        assert len(response.get_successful_resources()) == 1
        assert len(response.get_failed_resources()) == 0

    def test_get_summary(self):
        """Test getting response summary."""
        resources = [
            ResourceStatus(
                api_version="v1",
                kind="ConfigMap",
                name="test1",
                namespace="default",
                status="created",
            ),
            ResourceStatus(
                api_version="v1",
                kind="ConfigMap",
                name="test2",
                namespace="default",
                status="updated",
            ),
            ResourceStatus(
                api_version="v1",
                kind="ConfigMap",
                name="test3",
                namespace="default",
                status="failed",
            ),
        ]
        response = ManifestSubmissionResponse(
            success=False,
            cluster_id="gco-us-east-1",
            region="us-east-1",
            resources=resources,
            errors=["Validation failed for test3"],
        )
        summary = response.get_summary()
        assert summary["created"] == 1
        assert summary["updated"] == 1
        assert summary["failed"] == 1
        assert summary["unchanged"] == 0


# =============================================================================
# Disabled Threshold Tests (-1 sentinel)
# =============================================================================


class TestDisabledThresholds:
    """Tests for the -1 disabled threshold feature."""

    def test_all_thresholds_disabled(self):
        t = ResourceThresholds(
            cpu_threshold=-1,
            memory_threshold=-1,
            gpu_threshold=-1,
            pending_pods_threshold=-1,
            pending_requested_cpu_vcpus=-1,
            pending_requested_memory_gb=-1,
            pending_requested_gpus=-1,
        )
        assert t.is_disabled("cpu_threshold")
        assert t.is_disabled("memory_threshold")
        assert t.is_disabled("gpu_threshold")
        assert t.is_disabled("pending_pods_threshold")
        assert t.is_disabled("pending_requested_cpu_vcpus")
        assert t.is_disabled("pending_requested_memory_gb")
        assert t.is_disabled("pending_requested_gpus")

    def test_mixed_enabled_disabled(self):
        t = ResourceThresholds(
            cpu_threshold=80,
            memory_threshold=-1,
            gpu_threshold=-1,
            pending_pods_threshold=10,
        )
        assert not t.is_disabled("cpu_threshold")
        assert t.is_disabled("memory_threshold")
        assert t.is_disabled("gpu_threshold")
        assert not t.is_disabled("pending_pods_threshold")

    def test_zero_is_not_disabled(self):
        t = ResourceThresholds(cpu_threshold=0, memory_threshold=0, gpu_threshold=0)
        assert not t.is_disabled("cpu_threshold")
        assert not t.is_disabled("memory_threshold")
        assert not t.is_disabled("gpu_threshold")

    def test_invalid_negative_not_minus_one(self):
        with pytest.raises(ValueError):
            ResourceThresholds(cpu_threshold=-5, memory_threshold=80, gpu_threshold=90)

    def test_invalid_pending_negative_not_minus_one(self):
        with pytest.raises(ValueError):
            ResourceThresholds(
                cpu_threshold=80,
                memory_threshold=80,
                gpu_threshold=80,
                pending_pods_threshold=-3,
            )
