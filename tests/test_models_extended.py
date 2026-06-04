"""
Extended tests for gco/models data classes.

Deeper coverage of validation paths not covered by test_models.py:
RequestedResources (rejects negative cpu_vcpus/memory_gb/gpus and
non-numeric types, accepts zero), ResourceUtilization negative-gpu
and over-100 rejection plus integer acceptance, and additional
KubernetesManifest and ResourceStatus edge cases.
Pins the exact error strings so downstream callers can assert on them.
"""

from datetime import datetime

import pytest

from gco.models import (
    HealthStatus,
    KubernetesManifest,
    ManifestSubmissionRequest,
    ManifestSubmissionResponse,
    RequestedResources,
    ResourceStatus,
    ResourceThresholds,
    ResourceUtilization,
)


class TestRequestedResourcesValidation:
    """Tests for RequestedResources validation."""

    def test_valid_requested_resources(self):
        """Test creating valid requested resources."""
        resources = RequestedResources(cpu_vcpus=10.5, memory_gb=32.0, gpus=4)
        assert resources.cpu_vcpus == 10.5
        assert resources.memory_gb == 32.0
        assert resources.gpus == 4

    def test_zero_values(self):
        """Test zero values are valid."""
        resources = RequestedResources(cpu_vcpus=0.0, memory_gb=0.0, gpus=0)
        assert resources.cpu_vcpus == 0.0

    def test_negative_cpu_raises_error(self):
        """Test that negative cpu_vcpus raises error."""
        with pytest.raises(ValueError, match="cpu_vcpus must be a non-negative number"):
            RequestedResources(cpu_vcpus=-1.0, memory_gb=10.0, gpus=0)

    def test_negative_memory_raises_error(self):
        """Test that negative memory_gb raises error."""
        with pytest.raises(ValueError, match="memory_gb must be a non-negative number"):
            RequestedResources(cpu_vcpus=10.0, memory_gb=-5.0, gpus=0)

    def test_negative_gpus_raises_error(self):
        """Test that negative gpus raises error."""
        with pytest.raises(ValueError, match="gpus must be a non-negative integer"):
            RequestedResources(cpu_vcpus=10.0, memory_gb=10.0, gpus=-1)

    def test_invalid_cpu_type_raises_error(self):
        """Test that invalid cpu type raises error."""
        with pytest.raises(ValueError, match="cpu_vcpus must be a non-negative number"):
            RequestedResources(cpu_vcpus="invalid", memory_gb=10.0, gpus=0)

    def test_invalid_memory_type_raises_error(self):
        """Test that invalid memory type raises error."""
        with pytest.raises(ValueError, match="memory_gb must be a non-negative number"):
            RequestedResources(cpu_vcpus=10.0, memory_gb="invalid", gpus=0)

    def test_invalid_gpus_type_raises_error(self):
        """Test that invalid gpus type raises error."""
        with pytest.raises(ValueError, match="gpus must be a non-negative integer"):
            RequestedResources(cpu_vcpus=10.0, memory_gb=10.0, gpus=1.5)


class TestResourceUtilizationExtended:
    """Extended tests for ResourceUtilization validation."""

    def test_invalid_gpu_negative(self):
        """Test that negative GPU raises error."""
        with pytest.raises(ValueError, match="gpu"):
            ResourceUtilization(cpu=50.0, memory=50.0, gpu=-1.0)

    def test_invalid_gpu_over_100(self):
        """Test that GPU over 100 raises error."""
        with pytest.raises(ValueError, match="gpu"):
            ResourceUtilization(cpu=50.0, memory=50.0, gpu=101.0)

    def test_invalid_cpu_type(self):
        """Test that invalid CPU type raises error."""
        with pytest.raises(ValueError, match="cpu"):
            ResourceUtilization(cpu="invalid", memory=50.0, gpu=0.0)

    def test_integer_values_accepted(self):
        """Test that integer values are accepted."""
        util = ResourceUtilization(cpu=50, memory=60, gpu=0)
        assert util.cpu == 50


class TestHealthStatusExtended:
    """Extended tests for HealthStatus validation."""

    @pytest.fixture
    def thresholds(self):
        return ResourceThresholds(cpu_threshold=80, memory_threshold=85, gpu_threshold=90)

    @pytest.fixture
    def utilization(self):
        return ResourceUtilization(cpu=50.0, memory=60.0, gpu=0.0)

    def test_empty_region_raises_error(self, thresholds, utilization):
        """Test that empty region raises error."""
        with pytest.raises(ValueError, match="Region cannot be empty"):
            HealthStatus(
                cluster_id="test-cluster",
                region="",
                timestamp=datetime.now(),
                status="healthy",
                resource_utilization=utilization,
                thresholds=thresholds,
                active_jobs=5,
            )

    def test_negative_pending_pods_raises_error(self, thresholds, utilization):
        """Test that negative pending_pods raises error."""
        with pytest.raises(ValueError, match="Pending pods count cannot be negative"):
            HealthStatus(
                cluster_id="test-cluster",
                region="us-east-1",
                timestamp=datetime.now(),
                status="healthy",
                resource_utilization=utilization,
                thresholds=thresholds,
                active_jobs=5,
                pending_pods=-1,
            )

    def test_is_healthy_with_pending_resources(self, thresholds, utilization):
        """Test is_healthy with pending resources within thresholds."""
        pending = RequestedResources(cpu_vcpus=50.0, memory_gb=100.0, gpus=4)
        status = HealthStatus(
            cluster_id="test-cluster",
            region="us-east-1",
            timestamp=datetime.now(),
            status="healthy",
            resource_utilization=utilization,
            thresholds=thresholds,
            active_jobs=5,
            pending_pods=5,
            pending_requested=pending,
        )
        assert status.is_healthy()

    def test_is_healthy_with_pending_resources_exceeded(self, thresholds, utilization):
        """Test is_healthy with pending resources exceeding thresholds."""
        pending = RequestedResources(cpu_vcpus=150.0, memory_gb=250.0, gpus=12)
        status = HealthStatus(
            cluster_id="test-cluster",
            region="us-east-1",
            timestamp=datetime.now(),
            status="unhealthy",
            resource_utilization=utilization,
            thresholds=thresholds,
            active_jobs=5,
            pending_pods=5,
            pending_requested=pending,
        )
        assert not status.is_healthy()

    def test_get_threshold_violations_gpu(self, thresholds):
        """Test getting GPU threshold violations."""
        high_util = ResourceUtilization(cpu=50.0, memory=50.0, gpu=95.0)
        status = HealthStatus(
            cluster_id="test-cluster",
            region="us-east-1",
            timestamp=datetime.now(),
            status="unhealthy",
            resource_utilization=high_util,
            thresholds=thresholds,
            active_jobs=5,
        )
        violations = status.get_threshold_violations()
        assert any("GPU" in v for v in violations)

    def test_get_threshold_violations_pending_pods(self, thresholds, utilization):
        """Test getting pending pods threshold violations."""
        status = HealthStatus(
            cluster_id="test-cluster",
            region="us-east-1",
            timestamp=datetime.now(),
            status="unhealthy",
            resource_utilization=utilization,
            thresholds=thresholds,
            active_jobs=5,
            pending_pods=15,
        )
        violations = status.get_threshold_violations()
        assert any("Pending Pods" in v for v in violations)

    def test_get_threshold_violations_pending_resources(self, thresholds, utilization):
        """Test getting pending resources threshold violations."""
        pending = RequestedResources(cpu_vcpus=150.0, memory_gb=250.0, gpus=12)
        status = HealthStatus(
            cluster_id="test-cluster",
            region="us-east-1",
            timestamp=datetime.now(),
            status="unhealthy",
            resource_utilization=utilization,
            thresholds=thresholds,
            active_jobs=5,
            pending_pods=5,
            pending_requested=pending,
        )
        violations = status.get_threshold_violations()
        assert any("Pending CPU" in v for v in violations)
        assert any("Pending Memory" in v for v in violations)
        assert any("Pending GPUs" in v for v in violations)


class TestResourceThresholdsExtended:
    """Extended tests for ResourceThresholds."""

    def test_default_pending_thresholds(self):
        """Test default pending thresholds."""
        thresholds = ResourceThresholds(cpu_threshold=80, memory_threshold=85, gpu_threshold=90)
        assert thresholds.pending_pods_threshold == 10
        assert thresholds.pending_requested_cpu_vcpus == 100
        assert thresholds.pending_requested_memory_gb == 200
        assert thresholds.pending_requested_gpus == 8

    def test_custom_pending_thresholds(self):
        """Test custom pending thresholds."""
        thresholds = ResourceThresholds(
            cpu_threshold=80,
            memory_threshold=85,
            gpu_threshold=90,
            pending_pods_threshold=20,
            pending_requested_cpu_vcpus=50,
            pending_requested_memory_gb=100,
            pending_requested_gpus=4,
        )
        assert thresholds.pending_pods_threshold == 20

    def test_all_zero_thresholds(self):
        """Test all zero thresholds."""
        thresholds = ResourceThresholds(cpu_threshold=0, memory_threshold=0, gpu_threshold=0)
        assert thresholds.cpu_threshold == 0

    def test_all_max_thresholds(self):
        """Test all max thresholds."""
        thresholds = ResourceThresholds(cpu_threshold=100, memory_threshold=100, gpu_threshold=100)
        assert thresholds.cpu_threshold == 100


class TestKubernetesManifestExtended:
    """Extended tests for KubernetesManifest."""

    def test_empty_kind_raises_error(self):
        """Test that empty kind raises error."""
        with pytest.raises(ValueError, match="kind cannot be empty"):
            KubernetesManifest(apiVersion="v1", kind="", metadata={"name": "test"})

    def test_get_namespace_default(self):
        """Test get_namespace returns default when not specified."""
        manifest = KubernetesManifest(
            apiVersion="v1", kind="ConfigMap", metadata={"name": "test"}, data={}
        )
        assert manifest.get_namespace() == "default"

    def test_get_namespace_explicit(self):
        """Test manifest with explicit namespace."""
        manifest = KubernetesManifest(
            apiVersion="v1",
            kind="ConfigMap",
            metadata={"name": "test", "namespace": "custom-ns"},
            data={"key": "value"},
        )
        assert manifest.get_namespace() == "custom-ns"

    def test_to_dict_with_spec(self):
        """Test to_dict includes spec when present."""
        manifest = KubernetesManifest(
            apiVersion="apps/v1",
            kind="Deployment",
            metadata={"name": "test"},
            spec={"replicas": 2},
        )
        d = manifest.to_dict()
        assert "spec" in d
        assert d["spec"]["replicas"] == 2

    def test_from_dict_with_data(self):
        """Test from_dict with data field."""
        data = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "test"},
            "data": {"key": "value"},
        }
        manifest = KubernetesManifest.from_dict(data)
        assert manifest.data == {"key": "value"}

    def test_job_manifest(self):
        """Test Job manifest."""
        manifest = KubernetesManifest(
            apiVersion="batch/v1",
            kind="Job",
            metadata={"name": "test-job", "namespace": "default"},
            spec={
                "template": {
                    "spec": {
                        "containers": [{"name": "worker", "image": "python:3.14"}],
                        "restartPolicy": "Never",
                    }
                }
            },
        )
        assert manifest.kind == "Job"

    def test_secret_manifest(self):
        """Test Secret manifest."""
        manifest = KubernetesManifest(
            apiVersion="v1",
            kind="Secret",
            metadata={"name": "test-secret"},
            data={"password": "base64encoded"},
        )
        assert manifest.kind == "Secret"

    def test_pvc_manifest(self):
        """Test PersistentVolumeClaim manifest."""
        manifest = KubernetesManifest(
            apiVersion="v1",
            kind="PersistentVolumeClaim",
            metadata={"name": "test-pvc"},
            spec={
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": "10Gi"}},
            },
        )
        assert manifest.kind == "PersistentVolumeClaim"


class TestResourceStatusExtended:
    """Extended tests for ResourceStatus."""

    def test_updated_status(self):
        """Test 'updated' status is successful."""
        status = ResourceStatus(
            api_version="v1",
            kind="ConfigMap",
            name="test",
            namespace="default",
            status="updated",
        )
        assert status.is_successful()

    def test_unchanged_status(self):
        """Test 'unchanged' status is successful."""
        status = ResourceStatus(
            api_version="v1",
            kind="ConfigMap",
            name="test",
            namespace="default",
            status="unchanged",
        )
        assert status.is_successful()

    def test_message_field(self):
        """Test message field."""
        status = ResourceStatus(
            api_version="v1",
            kind="ConfigMap",
            name="test",
            namespace="default",
            status="failed",
            message="Validation error: invalid field",
        )
        assert status.message == "Validation error: invalid field"

    def test_all_status_types(self):
        """Test all valid status types."""
        for status_type in ["created", "updated", "unchanged", "failed"]:
            status = ResourceStatus(
                api_version="v1",
                kind="ConfigMap",
                name="test",
                namespace="default",
                status=status_type,
            )
            if status_type == "failed":
                assert not status.is_successful()
            else:
                assert status.is_successful()


class TestManifestSubmissionResponseExtended:
    """Extended tests for ManifestSubmissionResponse."""

    def test_get_failed_resources(self):
        """Test getting failed resources."""
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
                status="failed",
                message="Validation error",
            ),
        ]
        response = ManifestSubmissionResponse(
            success=False,
            cluster_id="test-cluster",
            region="us-east-1",
            resources=resources,
        )
        failed = response.get_failed_resources()
        assert len(failed) == 1
        assert failed[0].name == "test2"

    def test_get_summary_all_statuses(self):
        """Test get_summary with all status types."""
        resources = [
            ResourceStatus(
                api_version="v1", kind="ConfigMap", name="t1", namespace="default", status="created"
            ),
            ResourceStatus(
                api_version="v1", kind="ConfigMap", name="t2", namespace="default", status="updated"
            ),
            ResourceStatus(
                api_version="v1",
                kind="ConfigMap",
                name="t3",
                namespace="default",
                status="unchanged",
            ),
            ResourceStatus(
                api_version="v1", kind="ConfigMap", name="t4", namespace="default", status="failed"
            ),
        ]
        response = ManifestSubmissionResponse(
            success=False,
            cluster_id="test-cluster",
            region="us-east-1",
            resources=resources,
        )
        summary = response.get_summary()
        assert summary["created"] == 1
        assert summary["updated"] == 1
        assert summary["unchanged"] == 1
        assert summary["failed"] == 1

    def test_empty_resources(self):
        """Test response with empty resources."""
        response = ManifestSubmissionResponse(
            success=True,
            cluster_id="test-cluster",
            region="us-east-1",
            resources=[],
        )
        assert len(response.get_successful_resources()) == 0
        assert len(response.get_failed_resources()) == 0

    def test_all_failed_resources(self):
        """Test response with all failed resources."""
        resources = [
            ResourceStatus(
                api_version="v1",
                kind="ConfigMap",
                name=f"test-{i}",
                namespace="default",
                status="failed",
                message=f"Error {i}",
            )
            for i in range(3)
        ]
        response = ManifestSubmissionResponse(
            success=False,
            cluster_id="test-cluster",
            region="us-east-1",
            resources=resources,
            errors=["Multiple failures"],
        )
        assert len(response.get_failed_resources()) == 3
        summary = response.get_summary()
        assert summary["failed"] == 3


class TestManifestSubmissionRequestExtended:
    """Extended tests for ManifestSubmissionRequest."""

    def test_multiple_manifests(self):
        """Test request with multiple manifests."""
        manifests = [
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": f"test{i}"}, "data": {}}
            for i in range(10)
        ]
        request = ManifestSubmissionRequest(manifests=manifests)
        assert request.get_resource_count() == 10

    def test_namespace_override(self):
        """Test request with namespace override."""
        manifests = [
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "test"}, "data": {}}
        ]
        request = ManifestSubmissionRequest(manifests=manifests, namespace="custom-ns")
        assert request.namespace == "custom-ns"

    def test_validate_on_server(self):
        """Test request with validate_on_server flag."""
        manifests = [
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "test"}, "data": {}}
        ]
        request = ManifestSubmissionRequest(manifests=manifests, validate=True)
        assert request.validate is True

    def test_with_all_options(self):
        """Test submission request with all options."""
        manifests = [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "test"},
                "data": {"key": "value"},
            }
        ]
        request = ManifestSubmissionRequest(
            manifests=manifests,
            namespace="custom-ns",
            dry_run=True,
            validate=True,
        )
        assert request.namespace == "custom-ns"
        assert request.dry_run is True
        assert request.validate is True


class TestClusterConfigValidation:
    """Tests for ClusterConfig validation."""

    @pytest.fixture
    def valid_thresholds(self):
        """Create valid thresholds for testing."""
        return ResourceThresholds(cpu_threshold=80, memory_threshold=85, gpu_threshold=90)

    def test_valid_cluster_config(self, valid_thresholds):
        """Test creating valid cluster config."""
        from gco.models import ClusterConfig

        config = ClusterConfig(
            region="us-east-1",
            cluster_name="test-cluster",
            kubernetes_version="1.36",
            addons=["metrics-server"],
            resource_thresholds=valid_thresholds,
        )
        assert config.region == "us-east-1"
        assert config.cluster_name == "test-cluster"

    def test_empty_region_raises_error(self, valid_thresholds):
        """Test that empty region raises error."""
        from gco.models import ClusterConfig

        with pytest.raises(ValueError, match="Region cannot be empty"):
            ClusterConfig(
                region="",
                cluster_name="test-cluster",
                kubernetes_version="1.36",
                addons=["metrics-server"],
                resource_thresholds=valid_thresholds,
            )

    def test_empty_cluster_name_raises_error(self, valid_thresholds):
        """Test that empty cluster_name raises error."""
        from gco.models import ClusterConfig

        with pytest.raises(ValueError, match="Cluster name cannot be empty"):
            ClusterConfig(
                region="us-east-1",
                cluster_name="",
                kubernetes_version="1.36",
                addons=["metrics-server"],
                resource_thresholds=valid_thresholds,
            )

    def test_empty_kubernetes_version_raises_error(self, valid_thresholds):
        """Test that empty kubernetes_version raises error."""
        from gco.models import ClusterConfig

        with pytest.raises(ValueError, match="Kubernetes version cannot be empty"):
            ClusterConfig(
                region="us-east-1",
                cluster_name="test-cluster",
                kubernetes_version="",
                addons=["metrics-server"],
                resource_thresholds=valid_thresholds,
            )


class TestResourceThresholdsNonIntegerValidation:
    """Tests for ResourceThresholds with non-integer values."""

    def test_float_cpu_threshold_raises_error(self):
        """Test that float cpu_threshold raises error."""
        with pytest.raises(ValueError, match="cpu_threshold must be an integer"):
            ResourceThresholds(cpu_threshold=80.5, memory_threshold=85, gpu_threshold=90)

    def test_float_memory_threshold_raises_error(self):
        """Test that float memory_threshold raises error."""
        with pytest.raises(ValueError, match="memory_threshold must be an integer"):
            ResourceThresholds(cpu_threshold=80, memory_threshold=85.5, gpu_threshold=90)

    def test_float_gpu_threshold_raises_error(self):
        """Test that float gpu_threshold raises error."""
        with pytest.raises(ValueError, match="gpu_threshold must be an integer"):
            ResourceThresholds(cpu_threshold=80, memory_threshold=85, gpu_threshold=90.5)

    def test_string_cpu_threshold_raises_error(self):
        """Test that string cpu_threshold raises error."""
        with pytest.raises(ValueError, match="cpu_threshold must be an integer"):
            ResourceThresholds(cpu_threshold="80", memory_threshold=85, gpu_threshold=90)

    def test_negative_pending_pods_threshold_raises_error(self):
        """Test that negative pending_pods_threshold (other than -1) raises error."""
        with pytest.raises(ValueError, match="pending_pods_threshold"):
            ResourceThresholds(
                cpu_threshold=80,
                memory_threshold=85,
                gpu_threshold=90,
                pending_pods_threshold=-2,
            )

    def test_negative_pending_cpu_vcpus_raises_error(self):
        """Test that negative pending_requested_cpu_vcpus (other than -1) raises error."""
        with pytest.raises(ValueError, match="pending_requested_cpu_vcpus"):
            ResourceThresholds(
                cpu_threshold=80,
                memory_threshold=85,
                gpu_threshold=90,
                pending_requested_cpu_vcpus=-2,
            )

    def test_negative_pending_memory_gb_raises_error(self):
        """Test that negative pending_requested_memory_gb (other than -1) raises error."""
        with pytest.raises(ValueError, match="pending_requested_memory_gb"):
            ResourceThresholds(
                cpu_threshold=80,
                memory_threshold=85,
                gpu_threshold=90,
                pending_requested_memory_gb=-2,
            )

    def test_negative_pending_gpus_raises_error(self):
        """Test that negative pending_requested_gpus (other than -1) raises error."""
        with pytest.raises(ValueError, match="pending_requested_gpus"):
            ResourceThresholds(
                cpu_threshold=80,
                memory_threshold=85,
                gpu_threshold=90,
                pending_requested_gpus=-2,
            )

    def test_float_pending_pods_threshold_raises_error(self):
        """Test that float pending_pods_threshold raises error."""
        with pytest.raises(
            ValueError, match="pending_pods_threshold must be a non-negative integer"
        ):
            ResourceThresholds(
                cpu_threshold=80,
                memory_threshold=85,
                gpu_threshold=90,
                pending_pods_threshold=10.5,
            )


class TestKubernetesManifestValidationEdgeCases:
    """Tests for KubernetesManifest validation edge cases."""

    def test_metadata_not_dict_raises_error(self):
        """Test that non-dict metadata raises error."""
        with pytest.raises(ValueError, match="metadata must be a dictionary"):
            KubernetesManifest(apiVersion="v1", kind="ConfigMap", metadata="invalid")

    def test_spec_not_dict_raises_error(self):
        """Test that non-dict spec raises error."""
        with pytest.raises(ValueError, match="spec must be a dictionary"):
            KubernetesManifest(
                apiVersion="apps/v1",
                kind="Deployment",
                metadata={"name": "test"},
                spec="invalid",
            )

    def test_data_not_dict_raises_error(self):
        """Test that non-dict data raises error."""
        with pytest.raises(ValueError, match="data must be a dictionary"):
            KubernetesManifest(
                apiVersion="v1",
                kind="ConfigMap",
                metadata={"name": "test"},
                data="invalid",
            )

    def test_missing_spec_and_data_raises_error(self):
        """Test that missing both spec and data raises error for most resources."""
        with pytest.raises(ValueError, match="Either spec or data must be provided"):
            KubernetesManifest(
                apiVersion="apps/v1",
                kind="Deployment",
                metadata={"name": "test"},
            )

    def test_service_account_without_spec_or_data(self):
        """Test that ServiceAccount can be created without spec or data."""
        manifest = KubernetesManifest(
            apiVersion="v1",
            kind="ServiceAccount",
            metadata={"name": "test-sa"},
        )
        assert manifest.kind == "ServiceAccount"


class TestManifestSubmissionRequestValidation:
    """Tests for ManifestSubmissionRequest validation edge cases."""

    def test_invalid_manifest_in_list_raises_error(self):
        """Test that invalid manifest in list raises error with index."""
        manifests = [
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "valid"}, "data": {}},
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {}},  # Missing name
        ]
        with pytest.raises(ValueError, match="Invalid manifest at index 1"):
            ManifestSubmissionRequest(manifests=manifests)


class TestResourceStatusValidation:
    """Tests for ResourceStatus validation edge cases."""

    def test_invalid_status_raises_error(self):
        """Test that invalid status raises error."""
        with pytest.raises(ValueError, match="Status must be one of"):
            ResourceStatus(
                api_version="v1",
                kind="ConfigMap",
                name="test",
                namespace="default",
                status="invalid_status",
            )

    def test_deleted_status_is_successful(self):
        """Test that 'deleted' status is considered successful."""
        status = ResourceStatus(
            api_version="v1",
            kind="ConfigMap",
            name="test",
            namespace="default",
            status="deleted",
        )
        assert status.is_successful()

    def test_get_resource_identifier(self):
        """Test get_resource_identifier method."""
        status = ResourceStatus(
            api_version="v1",
            kind="ConfigMap",
            name="test-config",
            namespace="my-namespace",
            status="created",
        )
        assert status.get_resource_identifier() == "v1/ConfigMap/my-namespace/test-config"


class TestManifestSubmissionResponseValidation:
    """Tests for ManifestSubmissionResponse validation edge cases."""

    def test_empty_cluster_id_raises_error(self):
        """Test that empty cluster_id raises error."""
        with pytest.raises(ValueError, match="Cluster ID cannot be empty"):
            ManifestSubmissionResponse(
                success=True,
                cluster_id="",
                region="us-east-1",
                resources=[],
            )

    def test_empty_region_raises_error(self):
        """Test that empty region raises error."""
        with pytest.raises(ValueError, match="Region cannot be empty"):
            ManifestSubmissionResponse(
                success=True,
                cluster_id="test-cluster",
                region="",
                resources=[],
            )

    def test_resources_not_list_raises_error(self):
        """Test that non-list resources raises error."""
        with pytest.raises(ValueError, match="Resources must be a list"):
            ManifestSubmissionResponse(
                success=True,
                cluster_id="test-cluster",
                region="us-east-1",
                resources="invalid",
            )


class TestHealthStatusThresholdViolations:
    """Tests for HealthStatus threshold violation edge cases."""

    @pytest.fixture
    def thresholds(self):
        return ResourceThresholds(cpu_threshold=80, memory_threshold=85, gpu_threshold=90)

    @pytest.fixture
    def utilization(self):
        return ResourceUtilization(cpu=50.0, memory=60.0, gpu=0.0)

    def test_get_threshold_violations_no_pending_requested(self, thresholds, utilization):
        """Test get_threshold_violations when pending_requested is None."""
        status = HealthStatus(
            cluster_id="test-cluster",
            region="us-east-1",
            timestamp=datetime.now(),
            status="healthy",
            resource_utilization=utilization,
            thresholds=thresholds,
            active_jobs=5,
            pending_pods=5,
            pending_requested=None,  # Explicitly None
        )
        violations = status.get_threshold_violations()
        # Should not include any pending resource violations
        assert not any("Pending CPU" in v for v in violations)
        assert not any("Pending Memory" in v for v in violations)
        assert not any("Pending GPUs" in v for v in violations)

    def test_get_threshold_violations_all_utilization_exceeded(self, thresholds):
        """Test get_threshold_violations when all utilization thresholds exceeded."""
        high_util = ResourceUtilization(cpu=95.0, memory=95.0, gpu=95.0)
        status = HealthStatus(
            cluster_id="test-cluster",
            region="us-east-1",
            timestamp=datetime.now(),
            status="unhealthy",
            resource_utilization=high_util,
            thresholds=thresholds,
            active_jobs=5,
            pending_pods=5,
        )
        violations = status.get_threshold_violations()
        assert any("CPU" in v for v in violations)
        assert any("Memory" in v for v in violations)
        assert any("GPU" in v for v in violations)

    def test_is_healthy_without_pending_requested(self, thresholds, utilization):
        """Test is_healthy when pending_requested is None."""
        status = HealthStatus(
            cluster_id="test-cluster",
            region="us-east-1",
            timestamp=datetime.now(),
            status="healthy",
            resource_utilization=utilization,
            thresholds=thresholds,
            active_jobs=5,
            pending_pods=5,
            pending_requested=None,
        )
        # Should be healthy since utilization is within thresholds
        assert status.is_healthy()

    def test_invalid_status_value_raises_error(self, thresholds, utilization):
        """Test that invalid status value raises error."""
        with pytest.raises(ValueError, match="Status must be 'healthy' or 'unhealthy'"):
            HealthStatus(
                cluster_id="test-cluster",
                region="us-east-1",
                timestamp=datetime.now(),
                status="unknown",
                resource_utilization=utilization,
                thresholds=thresholds,
                active_jobs=5,
            )


class TestResourceStatusNamespaceValidation:
    """Tests for ResourceStatus name and namespace validation."""

    def test_empty_name_raises_error(self):
        """Test that empty name raises error."""
        with pytest.raises(ValueError, match="Resource name cannot be empty"):
            ResourceStatus(
                api_version="v1",
                kind="ConfigMap",
                name="",
                namespace="default",
                status="created",
            )

    def test_empty_namespace_raises_error(self):
        """Test that empty namespace raises error."""
        with pytest.raises(ValueError, match="Resource namespace cannot be empty"):
            ResourceStatus(
                api_version="v1",
                kind="ConfigMap",
                name="test",
                namespace="",
                status="created",
            )
