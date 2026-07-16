"""
Tests for gco/services/manifest_processor.ManifestProcessor.

Covers the core validation pipeline — structure checks, namespace
allowlist, per-manifest CPU/memory/GPU caps, Pod Security
Admission-style security context enforcement, image-registry
allowlist — plus the apply/submission pipeline and CRUD helpers
against the Kubernetes APIs. Uses pytest fixtures that construct a
processor with mocked Kubernetes config plus sample valid Deployment
and Job manifests so each test starts from a known-good baseline.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException

from gco.models import ManifestSubmissionRequest
from gco.services.manifest_processor import (
    DEFAULT_ALLOWED_KINDS,
    ManifestProcessor,
    create_manifest_processor_from_env,
)


@pytest.fixture
def processor():
    """Create ManifestProcessor with mocked Kubernetes config."""
    with patch("gco.services.manifest_processor.config"):
        processor = ManifestProcessor(
            cluster_id="test-cluster",
            region="us-east-1",
            config_dict={
                "max_cpu_per_manifest": "10",
                "max_memory_per_manifest": "32Gi",
                "max_gpu_per_manifest": 4,
                "allowed_namespaces": ["default", "gco-jobs"],
                "validation_enabled": True,
            },
        )
        return processor


@pytest.fixture
def valid_deployment():
    """Create a valid deployment manifest."""
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "test-app", "namespace": "default"},
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": {"app": "test"}},
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "image": "docker.io/nginx:latest",
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "128Mi"},
                                "limits": {"cpu": "500m", "memory": "512Mi"},
                            },
                        }
                    ]
                }
            },
        },
    }


@pytest.fixture
def valid_job():
    """Create a valid job manifest."""
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


class TestBasicValidation:
    """Tests for basic manifest structure validation."""

    def test_valid_deployment(self, processor, valid_deployment):
        """Test valid deployment passes validation."""
        is_valid, error = processor.validate_manifest(valid_deployment)
        assert is_valid is True
        assert error is None

    def test_valid_job(self, processor, valid_job):
        """Test valid job passes validation."""
        is_valid, error = processor.validate_manifest(valid_job)
        assert is_valid is True
        assert error is None

    def test_missing_api_version(self, processor):
        """Test missing apiVersion fails validation."""
        manifest = {"kind": "Deployment", "metadata": {"name": "test"}}
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is False
        assert "Missing required field: apiVersion" in error

    def test_missing_kind(self, processor):
        """Test missing kind fails validation."""
        manifest = {"apiVersion": "apps/v1", "metadata": {"name": "test"}}
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is False
        assert "Missing required field: kind" in error

    def test_missing_metadata(self, processor):
        """Test missing metadata fails validation."""
        manifest = {"apiVersion": "apps/v1", "kind": "Deployment"}
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is False
        assert "Missing required field: metadata" in error

    def test_missing_name(self, processor):
        """Test missing name in metadata fails validation."""
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"namespace": "default"},
        }
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is False
        assert "Missing metadata.name field" in error

    def test_disallowed_resource_kind_fails_closed(self, processor):
        """The shared kind policy rejects resources outside its allowlist."""
        manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "credentials", "namespace": "gco-jobs"},
            "data": {"token": "not-a-real-secret"},
        }

        is_valid, error = processor.validate_manifest(manifest)

        assert is_valid is False
        assert "Resource kind 'Secret' is not allowed" in error


class TestNamespaceValidation:
    """Tests for namespace restriction validation."""

    def test_allowed_namespace_default(self, processor, valid_deployment):
        """Test default namespace is allowed."""
        valid_deployment["metadata"]["namespace"] = "default"
        is_valid, error = processor.validate_manifest(valid_deployment)
        assert is_valid is True

    def test_allowed_namespace_gco_jobs(self, processor, valid_deployment):
        """Test gco-jobs namespace is allowed."""
        valid_deployment["metadata"]["namespace"] = "gco-jobs"
        is_valid, error = processor.validate_manifest(valid_deployment)
        assert is_valid is True

    def test_disallowed_namespace(self, processor, valid_deployment):
        """Test disallowed namespace fails validation."""
        valid_deployment["metadata"]["namespace"] = "kube-system"
        is_valid, error = processor.validate_manifest(valid_deployment)
        assert is_valid is False
        assert "Namespace 'kube-system' not allowed" in error

    def test_default_namespace_when_not_specified(self, processor):
        """Test default namespace is used when not specified."""
        manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "test-config"},
            "data": {"key": "value"},
        }
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is True


class TestResourceLimitValidation:
    """Tests for resource limit validation."""

    def test_within_cpu_limits(self, processor, valid_deployment):
        """Test CPU within limits passes validation."""
        is_valid, error = processor.validate_manifest(valid_deployment)
        assert is_valid is True

    def test_exceeds_cpu_limits(self, processor, valid_deployment):
        """Test CPU exceeding limits fails validation."""
        valid_deployment["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"][
            "cpu"
        ] = "20"
        is_valid, error = processor.validate_manifest(valid_deployment)
        assert is_valid is False
        assert "CPU" in error and "exceeds max" in error

    def test_exceeds_memory_limits(self, processor, valid_deployment):
        """Test memory exceeding limits fails validation."""
        valid_deployment["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"][
            "memory"
        ] = "64Gi"
        is_valid, error = processor.validate_manifest(valid_deployment)
        assert is_valid is False
        assert "Memory" in error and "exceeds max" in error

    def test_exceeds_gpu_limits(self, processor, valid_deployment):
        """Test GPU exceeding limits fails validation."""
        valid_deployment["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"][
            "nvidia.com/gpu"
        ] = "8"
        is_valid, error = processor.validate_manifest(valid_deployment)
        assert is_valid is False
        assert "GPU" in error and "exceeds max" in error

    def test_multiple_containers_aggregate(self, processor, valid_deployment):
        """Test resource limits are aggregated across containers."""
        # Add second container that pushes total over limit
        # First container has 500m CPU, add 10 cores to exceed 10 core limit
        valid_deployment["spec"]["template"]["spec"]["containers"].append(
            {
                "name": "sidecar",
                "image": "docker.io/busybox:latest",
                "resources": {"limits": {"cpu": "10", "memory": "16Gi"}},
            }
        )
        is_valid, error = processor.validate_manifest(valid_deployment)
        assert is_valid is False
        assert "exceeds max" in error

    def test_resource_limit_error_includes_hint(self, processor, valid_deployment):
        """Test that resource limit errors include a hint about cdk.json."""
        valid_deployment["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"][
            "cpu"
        ] = "20"
        is_valid, error = processor.validate_manifest(valid_deployment)
        assert is_valid is False
        assert "cdk.json" in error


class TestSecurityContextValidation:
    """Tests for security context validation."""

    def test_non_privileged_passes(self, processor, valid_deployment):
        """Test non-privileged container passes validation."""
        is_valid, error = processor.validate_manifest(valid_deployment)
        assert is_valid is True

    def test_privileged_container_fails(self, processor, valid_deployment):
        """Test privileged container fails validation."""
        valid_deployment["spec"]["template"]["spec"]["containers"][0]["securityContext"] = {
            "privileged": True
        }
        is_valid, error = processor.validate_manifest(valid_deployment)
        assert is_valid is False
        assert "Security context validation failed" in error

    def test_privilege_escalation_fails(self, processor, valid_deployment):
        """Test allowPrivilegeEscalation fails validation."""
        valid_deployment["spec"]["template"]["spec"]["containers"][0]["securityContext"] = {
            "allowPrivilegeEscalation": True
        }
        is_valid, error = processor.validate_manifest(valid_deployment)
        assert is_valid is False
        assert "Security context validation failed" in error

    def test_privileged_pod_security_context_fails(self, processor, valid_deployment):
        """Test privileged pod security context fails validation."""
        valid_deployment["spec"]["template"]["spec"]["securityContext"] = {"privileged": True}
        is_valid, error = processor.validate_manifest(valid_deployment)
        assert is_valid is False
        assert "Security context validation failed" in error


class TestImageSourceValidation:
    """Tests for image source validation."""

    def test_docker_hub_allowed(self, processor, valid_deployment):
        """Test docker.io images are allowed."""
        valid_deployment["spec"]["template"]["spec"]["containers"][0]["image"] = (
            "docker.io/nginx:latest"
        )
        is_valid, error = processor.validate_manifest(valid_deployment)
        assert is_valid is True

    def test_gcr_allowed(self, processor, valid_deployment):
        """Test gcr.io images are allowed."""
        valid_deployment["spec"]["template"]["spec"]["containers"][0]["image"] = (
            "gcr.io/project/image:v1"
        )
        is_valid, error = processor.validate_manifest(valid_deployment)
        assert is_valid is True

    def test_public_ecr_allowed(self, processor, valid_deployment):
        """Test public.ecr.aws images are allowed."""
        valid_deployment["spec"]["template"]["spec"]["containers"][0]["image"] = (
            "public.ecr.aws/test/image:v1"
        )
        is_valid, error = processor.validate_manifest(valid_deployment)
        assert is_valid is True

    def test_quay_allowed(self, processor, valid_deployment):
        """Test quay.io images are allowed."""
        valid_deployment["spec"]["template"]["spec"]["containers"][0]["image"] = (
            "quay.io/test/image:v1"
        )
        is_valid, error = processor.validate_manifest(valid_deployment)
        assert is_valid is True

    def test_official_image_allowed(self, processor, valid_deployment):
        """Test official images without registry prefix are allowed."""
        valid_deployment["spec"]["template"]["spec"]["containers"][0]["image"] = "nginx:latest"
        is_valid, error = processor.validate_manifest(valid_deployment)
        assert is_valid is True

    def test_untrusted_registry_fails(self, processor, valid_deployment):
        """Test untrusted registry fails validation."""
        valid_deployment["spec"]["template"]["spec"]["containers"][0]["image"] = (
            "evil-registry.com/malware:latest"
        )
        is_valid, error = processor.validate_manifest(valid_deployment)
        assert is_valid is False
        assert "Untrusted image source" in error


class TestCronJobValidation:
    """Tests for CronJob manifest validation."""

    def test_valid_cronjob(self, processor):
        """Test valid CronJob passes validation."""
        manifest = {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {"name": "test-cronjob", "namespace": "default"},
            "spec": {
                "schedule": "*/5 * * * *",
                "jobTemplate": {
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [
                                    {
                                        "name": "worker",
                                        "image": "docker.io/busybox:latest",
                                        "resources": {"limits": {"cpu": "500m", "memory": "256Mi"}},
                                    }
                                ],
                                "restartPolicy": "OnFailure",
                            }
                        }
                    }
                },
            },
        }
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is True

    def test_cronjob_exceeds_limits(self, processor):
        """Test CronJob exceeding limits fails validation."""
        manifest = {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {"name": "test-cronjob", "namespace": "default"},
            "spec": {
                "schedule": "*/5 * * * *",
                "jobTemplate": {
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [
                                    {
                                        "name": "worker",
                                        "image": "docker.io/busybox:latest",
                                        "resources": {"limits": {"cpu": "50", "memory": "64Gi"}},
                                    }
                                ],
                                "restartPolicy": "OnFailure",
                            }
                        }
                    }
                },
            },
        }
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is False


class TestValidationDisabled:
    """Tests for validation disabled mode."""

    def test_validation_disabled_allows_all(self):
        """Test that disabled validation allows all manifests."""
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={"validation_enabled": False, "allowed_namespaces": ["default"]},
            )

        # This would normally fail - disallowed namespace
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "test", "namespace": "kube-system"},
        }
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is True


class TestResourceParsing:
    """Tests for CPU and memory string parsing."""

    def test_parse_cpu_millicores(self, processor):
        """Test parsing CPU in millicores."""
        assert processor._parse_cpu_string("500m") == 500
        assert processor._parse_cpu_string("1000m") == 1000

    def test_parse_cpu_cores(self, processor):
        """Test parsing CPU in cores."""
        assert processor._parse_cpu_string("1") == 1000
        assert processor._parse_cpu_string("2") == 2000

    def test_parse_memory_ki(self, processor):
        """Test parsing memory in Ki."""
        assert processor._parse_memory_string("1024Ki") == 1024 * 1024

    def test_parse_memory_mi(self, processor):
        """Test parsing memory in Mi."""
        assert processor._parse_memory_string("256Mi") == 256 * 1024 * 1024

    def test_parse_memory_gi(self, processor):
        """Test parsing memory in Gi."""
        assert processor._parse_memory_string("2Gi") == 2 * 1024 * 1024 * 1024

    def test_parse_memory_decimal_units(self, processor):
        """Test parsing memory in decimal units."""
        assert processor._parse_memory_string("1000k") == 1000 * 1000
        assert processor._parse_memory_string("1M") == 1000 * 1000
        assert processor._parse_memory_string("1G") == 1000 * 1000 * 1000

    def test_parse_empty_string(self, processor):
        """Test parsing empty strings returns 0."""
        assert processor._parse_cpu_string("") == 0
        assert processor._parse_memory_string("") == 0


class TestCreateManifestProcessorFromEnv:
    """Tests for create_manifest_processor_from_env factory function."""

    def test_create_from_env_defaults(self):
        """Test creation with default environment values."""
        with (
            patch("gco.services.manifest_processor.config") as mock_config,
            patch.dict("os.environ", {}, clear=True),
        ):
            mock_config.ConfigException = k8s_config.ConfigException
            mock_config.load_incluster_config.side_effect = k8s_config.ConfigException(
                "Not in cluster"
            )
            mock_config.load_kube_config.return_value = None

            processor = create_manifest_processor_from_env()

            assert processor.cluster_id == "unknown-cluster"
            assert processor.region == "unknown-region"
            assert processor.max_cpu_per_manifest == 10000  # 10 cores in millicores
            assert processor.max_gpu_per_manifest == 4
            assert processor.allowed_namespaces == {"gco-jobs"}
            assert processor.allowed_kinds == set(DEFAULT_ALLOWED_KINDS)
            assert processor.validation_enabled is True

    def test_create_from_env_custom_values(self):
        """Test creation with custom environment values."""
        env_vars = {
            "CLUSTER_NAME": "my-cluster",
            "REGION": "eu-west-1",
            "MAX_CPU_PER_MANIFEST": "20",
            "MAX_MEMORY_PER_MANIFEST": "64Gi",
            "MAX_GPU_PER_MANIFEST": "8",
            "ALLOWED_NAMESPACES": "default,production,staging",
            "ALLOWED_KINDS": "Job,ConfigMap",
            "VALIDATION_ENABLED": "false",
        }

        with (
            patch("gco.services.manifest_processor.config") as mock_config,
            patch.dict("os.environ", env_vars, clear=True),
        ):
            mock_config.ConfigException = k8s_config.ConfigException
            mock_config.load_incluster_config.side_effect = k8s_config.ConfigException(
                "Not in cluster"
            )
            mock_config.load_kube_config.return_value = None

            processor = create_manifest_processor_from_env()

            assert processor.cluster_id == "my-cluster"
            assert processor.region == "eu-west-1"
            assert processor.max_cpu_per_manifest == 20000  # 20 cores in millicores
            assert processor.max_gpu_per_manifest == 8
            assert processor.validation_enabled is False
            assert "default" in processor.allowed_namespaces
            assert "production" in processor.allowed_namespaces
            assert "staging" in processor.allowed_namespaces
            assert processor.allowed_kinds == {"Job", "ConfigMap"}

    def test_create_from_env_explicit_empty_allowed_kinds_denies_all(self):
        """An explicit empty policy must not silently restore default kinds."""
        with (
            patch("gco.services.manifest_processor.config") as mock_config,
            patch.dict("os.environ", {"ALLOWED_KINDS": ""}, clear=True),
        ):
            mock_config.ConfigException = k8s_config.ConfigException
            mock_config.load_incluster_config.side_effect = k8s_config.ConfigException(
                "Not in cluster"
            )
            mock_config.load_kube_config.return_value = None

            processor = create_manifest_processor_from_env()

            assert processor.allowed_kinds == set()
            valid, reason = processor.validate_manifest(
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {"name": "denied", "namespace": "gco-jobs"},
                    "spec": {"template": {"spec": {"containers": []}}},
                }
            )
            assert valid is False
            assert "not allowed" in reason

    def test_create_from_env_explicit_empty_namespaces_denies_all(self):
        """An explicit empty namespace policy must not restore gco-jobs."""
        with (
            patch("gco.services.manifest_processor.config") as mock_config,
            patch.dict("os.environ", {"ALLOWED_NAMESPACES": ""}, clear=True),
        ):
            mock_config.ConfigException = k8s_config.ConfigException
            mock_config.load_incluster_config.side_effect = k8s_config.ConfigException(
                "Not in cluster"
            )
            mock_config.load_kube_config.return_value = None

            processor = create_manifest_processor_from_env()

            assert processor.allowed_namespaces == set()
            valid, reason = processor.validate_manifest(
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "denied", "namespace": "gco-jobs"},
                    "data": {"key": "value"},
                }
            )
            assert valid is False
            assert "not allowed" in reason


class TestProcessManifestSubmission:
    """Tests for process_manifest_submission method."""

    @pytest.fixture
    def processor_with_mocks(self):
        """Create ManifestProcessor with mocked Kubernetes clients."""
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "max_cpu_per_manifest": "10",
                    "max_memory_per_manifest": "32Gi",
                    "max_gpu_per_manifest": 4,
                    "allowed_namespaces": ["default", "gco-jobs"],
                    "validation_enabled": True,
                },
            )
            return processor

    @pytest.mark.asyncio
    async def test_process_dry_run_valid_manifest(self, processor_with_mocks):
        """Test dry run with valid manifest."""
        request = ManifestSubmissionRequest(
            manifests=[
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "test-config", "namespace": "default"},
                    "data": {"key": "value"},
                }
            ],
            namespace="default",
            dry_run=True,
        )

        response = await processor_with_mocks.process_manifest_submission(request)

        assert response.success is True
        assert len(response.resources) == 1
        assert response.resources[0].status == "unchanged"
        assert "Dry run" in response.resources[0].message

    @pytest.mark.asyncio
    async def test_process_disallowed_namespace(self, processor_with_mocks):
        """Test processing manifest with disallowed namespace."""
        request = ManifestSubmissionRequest(
            manifests=[
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "test-config", "namespace": "kube-system"},
                    "data": {"key": "value"},
                }
            ],
            namespace="kube-system",
            dry_run=True,
        )

        response = await processor_with_mocks.process_manifest_submission(request)

        assert response.success is False
        assert "not allowed" in response.errors[0]

    @pytest.mark.asyncio
    async def test_request_namespace_is_validated_when_manifest_omits_it(
        self, processor_with_mocks
    ):
        """The request default and applied namespace share one allowlist check."""
        manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "test-config"},
            "data": {"key": "value"},
        }

        allowed = await processor_with_mocks.process_manifest_submission(
            ManifestSubmissionRequest(manifests=[manifest], namespace="default", dry_run=True)
        )
        denied = await processor_with_mocks.process_manifest_submission(
            ManifestSubmissionRequest(manifests=[manifest], namespace="kube-system", dry_run=True)
        )

        assert allowed.success is True
        assert allowed.resources[0].namespace == "default"
        assert denied.success is False
        assert denied.resources[0].namespace == "kube-system"
        assert "not allowed" in denied.errors[0]

    @pytest.mark.asyncio
    async def test_process_multiple_valid_manifests(self, processor_with_mocks):
        """Test processing multiple valid manifests."""
        request = ManifestSubmissionRequest(
            manifests=[
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "config-1", "namespace": "default"},
                    "data": {"key": "value1"},
                },
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "config-2", "namespace": "default"},
                    "data": {"key": "value2"},
                },
            ],
            namespace="default",
            dry_run=True,
        )

        response = await processor_with_mocks.process_manifest_submission(request)

        assert response.success is True
        assert len(response.resources) == 2


class TestDeleteResource:
    """Tests for delete_resource method."""

    @pytest.fixture
    def processor_with_mocks(self):
        """Create ManifestProcessor with mocked Kubernetes clients."""
        with (
            patch("gco.services.manifest_processor.config") as mock_config,
            patch("gco.services.manifest_processor.client"),
            patch("gco.services.manifest_processor.dynamic"),
        ):
            mock_config.ConfigException = k8s_config.ConfigException
            mock_config.load_incluster_config.side_effect = k8s_config.ConfigException(
                "Not in cluster"
            )
            mock_config.load_kube_config.return_value = None

            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            return processor

    @pytest.mark.asyncio
    async def test_delete_deployment_success(self, processor_with_mocks):
        """Test successful deployment deletion."""
        # Mock the dynamic client
        mock_resource = MagicMock()
        mock_resource.namespaced = True
        mock_resource.delete = MagicMock()
        processor_with_mocks._dynamic_client = MagicMock()
        processor_with_mocks._dynamic_client.resources.get.return_value = mock_resource

        result = await processor_with_mocks.delete_resource(
            api_version="apps/v1",
            kind="Deployment",
            name="test-deployment",
            namespace="default",
        )

        assert result.status == "deleted"
        assert result.is_successful()
        mock_resource.delete.assert_called_once_with(name="test-deployment", namespace="default")

    @pytest.mark.asyncio
    async def test_delete_service_success(self, processor_with_mocks):
        """Test successful service deletion."""
        # Mock the dynamic client
        mock_resource = MagicMock()
        mock_resource.namespaced = True
        mock_resource.delete = MagicMock()
        processor_with_mocks._dynamic_client = MagicMock()
        processor_with_mocks._dynamic_client.resources.get.return_value = mock_resource

        result = await processor_with_mocks.delete_resource(
            api_version="v1",
            kind="Service",
            name="test-service",
            namespace="default",
        )

        assert result.status == "deleted"
        mock_resource.delete.assert_called_once_with(name="test-service", namespace="default")

    @pytest.mark.asyncio
    async def test_delete_job_success(self, processor_with_mocks):
        """Test successful job deletion."""
        # Mock the dynamic client
        mock_resource = MagicMock()
        mock_resource.namespaced = True
        mock_resource.delete = MagicMock()
        processor_with_mocks._dynamic_client = MagicMock()
        processor_with_mocks._dynamic_client.resources.get.return_value = mock_resource

        result = await processor_with_mocks.delete_resource(
            api_version="batch/v1",
            kind="Job",
            name="test-job",
            namespace="default",
        )

        assert result.status == "deleted"
        mock_resource.delete.assert_called_once_with(name="test-job", namespace="default")

    @pytest.mark.asyncio
    async def test_delete_configmap_success(self, processor_with_mocks):
        """Test successful configmap deletion."""
        # Mock the dynamic client
        mock_resource = MagicMock()
        mock_resource.namespaced = True
        mock_resource.delete = MagicMock()
        processor_with_mocks._dynamic_client = MagicMock()
        processor_with_mocks._dynamic_client.resources.get.return_value = mock_resource

        result = await processor_with_mocks.delete_resource(
            api_version="v1",
            kind="ConfigMap",
            name="test-configmap",
            namespace="default",
        )

        assert result.status == "deleted"
        mock_resource.delete.assert_called_once_with(name="test-configmap", namespace="default")

    @pytest.mark.asyncio
    async def test_delete_secret_success(self, processor_with_mocks):
        """Test successful secret deletion."""
        # Mock the dynamic client
        mock_resource = MagicMock()
        mock_resource.namespaced = True
        mock_resource.delete = MagicMock()
        processor_with_mocks._dynamic_client = MagicMock()
        processor_with_mocks._dynamic_client.resources.get.return_value = mock_resource

        result = await processor_with_mocks.delete_resource(
            api_version="v1",
            kind="Secret",
            name="test-secret",
            namespace="default",
        )

        assert result.status == "deleted"
        mock_resource.delete.assert_called_once_with(name="test-secret", namespace="default")

    @pytest.mark.asyncio
    async def test_delete_not_found(self, processor_with_mocks):
        """Test deletion of non-existent resource."""
        # Mock the dynamic client to raise 404
        mock_resource = MagicMock()
        mock_resource.namespaced = True
        mock_resource.delete = MagicMock(side_effect=ApiException(status=404, reason="Not Found"))
        processor_with_mocks._dynamic_client = MagicMock()
        processor_with_mocks._dynamic_client.resources.get.return_value = mock_resource

        result = await processor_with_mocks.delete_resource(
            api_version="apps/v1",
            kind="Deployment",
            name="nonexistent",
            namespace="default",
        )

        assert result.status == "unchanged"
        assert "not found" in result.message.lower()

    @pytest.mark.asyncio
    async def test_delete_api_error(self, processor_with_mocks):
        """Test deletion with API error."""
        # Mock the dynamic client to raise 500
        mock_resource = MagicMock()
        mock_resource.namespaced = True
        mock_resource.delete = MagicMock(
            side_effect=ApiException(status=500, reason="Internal Server Error")
        )
        processor_with_mocks._dynamic_client = MagicMock()
        processor_with_mocks._dynamic_client.resources.get.return_value = mock_resource

        result = await processor_with_mocks.delete_resource(
            api_version="apps/v1",
            kind="Deployment",
            name="test-deployment",
            namespace="default",
        )

        assert result.status == "failed"
        assert "Internal Server Error" in result.message

    @pytest.mark.asyncio
    async def test_delete_unsupported_kind(self, processor_with_mocks):
        """Test deletion of unsupported resource kind."""
        from kubernetes.dynamic.exceptions import ResourceNotFoundError

        # Mock the dynamic client to raise ResourceNotFoundError for unknown kind
        processor_with_mocks._dynamic_client = MagicMock()
        processor_with_mocks._dynamic_client.resources.get.side_effect = ResourceNotFoundError(
            "Resource not found"
        )

        result = await processor_with_mocks.delete_resource(
            api_version="v1",
            kind="UnsupportedKind",
            name="test",
            namespace="default",
        )

        assert result.status == "failed"
        assert "Unknown resource type" in result.message


class TestGetResourceStatus:
    """Tests for get_resource_status method."""

    @pytest.fixture
    def processor_with_mocks(self):
        """Create ManifestProcessor with mocked Kubernetes clients."""
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            return processor

    def _setup_dynamic_mock(self, processor, return_value=None, side_effect=None):
        """Helper to set up dynamic client mock."""
        mock_resource_obj = MagicMock()
        if return_value:
            mock_resource_obj.to_dict.return_value = return_value
        mock_resource = MagicMock()
        mock_resource.namespaced = True
        if side_effect:
            mock_resource.get = MagicMock(side_effect=side_effect)
        else:
            mock_resource.get = MagicMock(return_value=mock_resource_obj if return_value else None)
        processor._dynamic_client = MagicMock()
        processor._dynamic_client.resources.get.return_value = mock_resource

    @pytest.mark.asyncio
    async def test_get_resource_status_exists(self, processor_with_mocks):
        """Test getting status of existing resource."""
        self._setup_dynamic_mock(
            processor_with_mocks,
            {
                "metadata": {"name": "test-deployment", "namespace": "default"},
                "spec": {"replicas": 2},
                "status": {"availableReplicas": 2},
            },
        )

        result = await processor_with_mocks.get_resource_status(
            api_version="apps/v1",
            kind="Deployment",
            name="test-deployment",
            namespace="default",
        )

        assert result is not None
        assert result["exists"] is True
        assert result["name"] == "test-deployment"

    @pytest.mark.asyncio
    async def test_get_resource_status_not_found(self, processor_with_mocks):
        """Test getting status of non-existent resource."""
        self._setup_dynamic_mock(
            processor_with_mocks, side_effect=ApiException(status=404, reason="Not Found")
        )

        result = await processor_with_mocks.get_resource_status(
            api_version="apps/v1",
            kind="Deployment",
            name="nonexistent",
            namespace="default",
        )

        assert result is not None
        assert result["exists"] is False

    @pytest.mark.asyncio
    async def test_get_resource_status_error(self, processor_with_mocks):
        """Test getting status with API error."""
        self._setup_dynamic_mock(processor_with_mocks, side_effect=Exception("Connection error"))

        result = await processor_with_mocks.get_resource_status(
            api_version="apps/v1",
            kind="Deployment",
            name="test",
            namespace="default",
        )

        assert result is None


class TestGetExistingResource:
    """Tests for _get_existing_resource method."""

    @pytest.fixture
    def processor_with_mocks(self):
        """Create ManifestProcessor with mocked Kubernetes clients."""
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            return processor

    @pytest.mark.asyncio
    async def test_get_existing_service(self, processor_with_mocks):
        """Test getting existing service."""
        mock_resource_obj = MagicMock()
        mock_resource_obj.to_dict.return_value = {"metadata": {"name": "test-service"}}
        mock_resource = MagicMock()
        mock_resource.namespaced = True
        mock_resource.get = MagicMock(return_value=mock_resource_obj)
        processor_with_mocks._dynamic_client = MagicMock()
        processor_with_mocks._dynamic_client.resources.get.return_value = mock_resource

        result = await processor_with_mocks._get_existing_resource(
            api_version="v1",
            kind="Service",
            name="test-service",
            namespace="default",
        )

        assert result is not None
        assert result["metadata"]["name"] == "test-service"

    @pytest.mark.asyncio
    async def test_get_existing_job(self, processor_with_mocks):
        """Test getting existing job."""
        mock_resource_obj = MagicMock()
        mock_resource_obj.to_dict.return_value = {"metadata": {"name": "test-job"}}
        mock_resource = MagicMock()
        mock_resource.namespaced = True
        mock_resource.get = MagicMock(return_value=mock_resource_obj)
        processor_with_mocks._dynamic_client = MagicMock()
        processor_with_mocks._dynamic_client.resources.get.return_value = mock_resource

        result = await processor_with_mocks._get_existing_resource(
            api_version="batch/v1",
            kind="Job",
            name="test-job",
            namespace="default",
        )

        assert result is not None

    @pytest.mark.asyncio
    async def test_get_existing_configmap(self, processor_with_mocks):
        """Test getting existing configmap."""
        mock_resource_obj = MagicMock()
        mock_resource_obj.to_dict.return_value = {"metadata": {"name": "test-cm"}}
        mock_resource = MagicMock()
        mock_resource.namespaced = True
        mock_resource.get = MagicMock(return_value=mock_resource_obj)
        processor_with_mocks._dynamic_client = MagicMock()
        processor_with_mocks._dynamic_client.resources.get.return_value = mock_resource

        result = await processor_with_mocks._get_existing_resource(
            api_version="v1",
            kind="ConfigMap",
            name="test-cm",
            namespace="default",
        )

        assert result is not None

    @pytest.mark.asyncio
    async def test_get_existing_secret(self, processor_with_mocks):
        """Test getting existing secret."""
        mock_resource_obj = MagicMock()
        mock_resource_obj.to_dict.return_value = {"metadata": {"name": "test-secret"}}
        mock_resource = MagicMock()
        mock_resource.namespaced = True
        mock_resource.get = MagicMock(return_value=mock_resource_obj)
        processor_with_mocks._dynamic_client = MagicMock()
        processor_with_mocks._dynamic_client.resources.get.return_value = mock_resource

        result = await processor_with_mocks._get_existing_resource(
            api_version="v1",
            kind="Secret",
            name="test-secret",
            namespace="default",
        )

        assert result is not None
        assert result["metadata"]["name"] == "test-secret"


class TestApplyManifest:
    """Tests for _apply_manifest method."""

    @pytest.fixture
    def processor_with_mocks(self):
        """Create ManifestProcessor with mocked Kubernetes clients."""
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            return processor

    @pytest.mark.asyncio
    async def test_apply_manifest_create_new(self, processor_with_mocks):
        """Test applying manifest creates new resource."""
        processor_with_mocks._get_existing_resource = AsyncMock(return_value=None)
        processor_with_mocks._create_resource = AsyncMock(return_value=True)

        manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "test-config", "namespace": "default"},
            "data": {"key": "value"},
        }

        result = await processor_with_mocks._apply_manifest(manifest)

        assert result.status == "created"
        processor_with_mocks._create_resource.assert_called_once()

    @pytest.mark.asyncio
    async def test_apply_manifest_update_existing(self, processor_with_mocks):
        """Test applying manifest updates existing resource."""
        processor_with_mocks._get_existing_resource = AsyncMock(
            return_value={"metadata": {"name": "test-config"}}
        )
        processor_with_mocks._update_resource = AsyncMock(return_value=True)

        manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "test-config", "namespace": "default"},
            "data": {"key": "new-value"},
        }

        result = await processor_with_mocks._apply_manifest(manifest)

        assert result.status == "updated"
        processor_with_mocks._update_resource.assert_called_once()

    @pytest.mark.asyncio
    async def test_apply_manifest_api_exception(self, processor_with_mocks):
        """Test applying manifest with API exception."""
        processor_with_mocks._get_existing_resource = AsyncMock(return_value=None)
        processor_with_mocks._create_resource = AsyncMock(
            side_effect=ApiException(status=403, reason="Forbidden")
        )

        manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "test-config", "namespace": "default"},
            "data": {"key": "value"},
        }

        result = await processor_with_mocks._apply_manifest(manifest)

        assert result.status == "failed"
        assert "Forbidden" in result.message

    @pytest.mark.asyncio
    async def test_apply_manifest_sets_default_namespace(self, processor_with_mocks):
        """Test applying manifest sets default namespace if not specified."""
        processor_with_mocks._get_existing_resource = AsyncMock(return_value=None)
        processor_with_mocks._create_resource = AsyncMock(return_value=True)

        manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "test-config"},
            "data": {"key": "value"},
        }

        result = await processor_with_mocks._apply_manifest(manifest, default_namespace="gco")

        assert result.namespace == "gco"

    @pytest.mark.asyncio
    async def test_apply_manifest_replaces_finished_job(self, processor_with_mocks):
        """Test that a finished Job is deleted and recreated."""
        existing_job = {"status": {"conditions": [{"type": "Complete", "status": "True"}]}}
        processor_with_mocks._get_existing_resource = AsyncMock(return_value=existing_job)
        processor_with_mocks._is_job_finished = MagicMock(return_value=True)
        processor_with_mocks.delete_resource = AsyncMock(return_value=True)
        processor_with_mocks._create_resource = AsyncMock(return_value=True)

        manifest = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "my-job", "namespace": "default"},
            "spec": {},
        }

        result = await processor_with_mocks._apply_manifest(manifest)

        assert result.status == "created"
        assert "replaced" in result.message.lower()
        processor_with_mocks.delete_resource.assert_called_once()
        processor_with_mocks._create_resource.assert_called_once()

    @pytest.mark.asyncio
    async def test_apply_manifest_renames_active_job(self, processor_with_mocks):
        """Test that an active Job triggers auto-rename instead of deletion."""
        existing_job = {"status": {"conditions": []}}
        processor_with_mocks._get_existing_resource = AsyncMock(return_value=existing_job)
        processor_with_mocks._is_job_finished = MagicMock(return_value=False)
        processor_with_mocks._create_resource = AsyncMock(return_value=True)

        manifest = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "my-job", "namespace": "default"},
            "spec": {},
        }

        result = await processor_with_mocks._apply_manifest(manifest)

        assert result.status == "created"
        assert "still running" in result.message.lower()
        assert "renamed" in result.message.lower()
        # Name should have been changed
        assert result.name != "my-job"
        assert result.name.startswith("my-job-")
        # delete_resource should NOT have been called — we didn't mock it,
        # so if it were called it would have raised an error.
        processor_with_mocks._create_resource.assert_called_once()


class TestResourceLimitEdgeCases:
    """Tests for edge cases in resource limit validation."""

    def test_validate_pod_spec_directly(self, processor):
        """Test validation of Pod with containers directly in spec."""
        manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "test-pod", "namespace": "default"},
            "spec": {
                "containers": [
                    {
                        "name": "app",
                        "image": "nginx:latest",
                        "resources": {"limits": {"cpu": "500m", "memory": "256Mi"}},
                    }
                ]
            },
        }
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is True

    def test_validate_statefulset(self, processor):
        """Test validation of StatefulSet."""
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": {"name": "test-statefulset", "namespace": "default"},
            "spec": {
                "serviceName": "test",
                "replicas": 1,
                "selector": {"matchLabels": {"app": "test"}},
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "app",
                                "image": "nginx:latest",
                                "resources": {"limits": {"cpu": "500m", "memory": "256Mi"}},
                            }
                        ]
                    }
                },
            },
        }
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is True

    def test_validate_daemonset(self, processor):
        """Test validation of DaemonSet."""
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": "test-daemonset", "namespace": "default"},
            "spec": {
                "selector": {"matchLabels": {"app": "test"}},
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "app",
                                "image": "nginx:latest",
                                "resources": {"limits": {"cpu": "500m", "memory": "256Mi"}},
                            }
                        ]
                    }
                },
            },
        }
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is True

    def test_validate_uses_requests_when_no_limits(self, processor):
        """Test validation uses requests when limits not specified."""
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "test-deployment", "namespace": "default"},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "test"}},
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "app",
                                "image": "nginx:latest",
                                "resources": {"requests": {"cpu": "500m", "memory": "256Mi"}},
                            }
                        ]
                    }
                },
            },
        }
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is True


class TestMemoryParsingEdgeCases:
    """Tests for edge cases in memory parsing."""

    def test_parse_memory_ti(self, processor):
        """Test parsing memory in Ti."""
        assert processor._parse_memory_string("1Ti") == 1024 * 1024 * 1024 * 1024

    def test_parse_memory_raw_bytes(self, processor):
        """Test parsing raw bytes."""
        assert processor._parse_memory_string("1048576") == 1048576

    def test_parse_memory_with_whitespace(self, processor):
        """Test parsing memory with whitespace."""
        assert processor._parse_memory_string("  256Mi  ") == 256 * 1024 * 1024

    def test_parse_cpu_with_whitespace(self, processor):
        """Test parsing CPU with whitespace."""
        assert processor._parse_cpu_string("  500m  ") == 500


class TestImageValidationEdgeCases:
    """Tests for edge cases in image validation."""

    def test_registry_k8s_io_allowed(self, processor, valid_deployment):
        """Test registry.k8s.io images are allowed."""
        valid_deployment["spec"]["template"]["spec"]["containers"][0]["image"] = (
            "registry.k8s.io/pause:3.9"
        )
        is_valid, error = processor.validate_manifest(valid_deployment)
        assert is_valid is True

    def test_k8s_gcr_io_allowed(self, processor, valid_deployment):
        """Test k8s.gcr.io images are allowed."""
        valid_deployment["spec"]["template"]["spec"]["containers"][0]["image"] = (
            "k8s.gcr.io/pause:3.9"
        )
        is_valid, error = processor.validate_manifest(valid_deployment)
        assert is_valid is True

    def test_gco_registry_allowed(self, processor, valid_deployment):
        """Test gco registry images are allowed."""
        valid_deployment["spec"]["template"]["spec"]["containers"][0]["image"] = "gco/worker:v1"
        is_valid, error = processor.validate_manifest(valid_deployment)
        assert is_valid is True

    def test_empty_image_allowed(self, processor, valid_deployment):
        """Test empty image is allowed (will fail at apply time)."""
        valid_deployment["spec"]["template"]["spec"]["containers"][0]["image"] = ""
        is_valid, error = processor.validate_manifest(valid_deployment)
        assert is_valid is True


class TestKubernetesConfigLoading:
    """Tests for Kubernetes configuration loading."""

    def test_loads_incluster_config_first(self):
        """Test that in-cluster config is tried first."""
        with (
            patch("gco.services.manifest_processor.config") as mock_config,
            patch("gco.services.manifest_processor.client"),
        ):
            mock_config.ConfigException = k8s_config.ConfigException
            mock_config.load_incluster_config.return_value = None

            ManifestProcessor(
                cluster_id="test",
                region="us-east-1",
                config_dict={"allowed_namespaces": ["default"]},
            )

            mock_config.load_incluster_config.assert_called_once()
            mock_config.load_kube_config.assert_not_called()

    def test_falls_back_to_kubeconfig(self):
        """Test fallback to local kubeconfig."""
        with (
            patch("gco.services.manifest_processor.config") as mock_config,
            patch("gco.services.manifest_processor.client"),
        ):
            mock_config.ConfigException = k8s_config.ConfigException
            mock_config.load_incluster_config.side_effect = k8s_config.ConfigException(
                "Not in cluster"
            )
            mock_config.load_kube_config.return_value = None

            ManifestProcessor(
                cluster_id="test",
                region="us-east-1",
                config_dict={"allowed_namespaces": ["default"]},
            )

            mock_config.load_kube_config.assert_called_once()

    def test_raises_on_config_failure(self):
        """Test raises exception when both config methods fail."""
        with (
            patch("gco.services.manifest_processor.config") as mock_config,
            patch("gco.services.manifest_processor.client"),
        ):
            mock_config.ConfigException = k8s_config.ConfigException
            mock_config.load_incluster_config.side_effect = k8s_config.ConfigException(
                "Not in cluster"
            )
            mock_config.load_kube_config.side_effect = k8s_config.ConfigException("No kubeconfig")

            with pytest.raises(k8s_config.ConfigException):
                ManifestProcessor(
                    cluster_id="test",
                    region="us-east-1",
                    config_dict={"allowed_namespaces": ["default"]},
                )


class TestListJobs:
    """Tests for list_jobs method."""

    @pytest.fixture
    def processor_with_mocks(self):
        """Create ManifestProcessor with mocked Kubernetes clients."""
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default", "gco-jobs"],
                    "validation_enabled": True,
                },
            )
            return processor

    def _create_mock_job(self, name, namespace, active=0, succeeded=0, failed=0, conditions=None):
        """Helper to create a mock Kubernetes Job object."""
        from datetime import datetime

        mock_job = MagicMock()
        mock_job.metadata.name = name
        mock_job.metadata.namespace = namespace
        mock_job.metadata.creation_timestamp = datetime(2024, 1, 1, 0, 0, 0)
        mock_job.metadata.labels = {"app": "test"}
        mock_job.metadata.uid = f"uid-{name}"
        mock_job.spec.parallelism = 1
        mock_job.spec.completions = 1
        mock_job.spec.backoff_limit = 6
        mock_job.status.active = active
        mock_job.status.succeeded = succeeded
        mock_job.status.failed = failed
        mock_job.status.start_time = datetime(2024, 1, 1, 0, 0, 1) if active or succeeded else None
        mock_job.status.completion_time = datetime(2024, 1, 1, 0, 1, 0) if succeeded else None
        mock_job.status.conditions = conditions or []
        return mock_job

    @pytest.mark.asyncio
    async def test_list_jobs_all_namespaces(self, processor_with_mocks):
        """Test listing jobs from all allowed namespaces."""
        mock_job1 = self._create_mock_job("job1", "default", active=1)
        mock_job2 = self._create_mock_job("job2", "gco-jobs", succeeded=1)

        mock_job_list1 = MagicMock()
        mock_job_list1.items = [mock_job1]
        mock_job_list2 = MagicMock()
        mock_job_list2.items = [mock_job2]

        processor_with_mocks.batch_v1.list_namespaced_job = MagicMock(
            side_effect=[mock_job_list1, mock_job_list2]
        )

        jobs = await processor_with_mocks.list_jobs()

        assert len(jobs) == 2
        assert processor_with_mocks.batch_v1.list_namespaced_job.call_count == 2

    @pytest.mark.asyncio
    async def test_list_jobs_specific_namespace(self, processor_with_mocks):
        """Test listing jobs from a specific namespace."""
        mock_job = self._create_mock_job("job1", "default", active=1)
        mock_job_list = MagicMock()
        mock_job_list.items = [mock_job]

        processor_with_mocks.batch_v1.list_namespaced_job = MagicMock(return_value=mock_job_list)

        jobs = await processor_with_mocks.list_jobs(namespace="default")

        assert len(jobs) == 1
        processor_with_mocks.batch_v1.list_namespaced_job.assert_called_once_with(
            namespace="default", _request_timeout=30
        )

    @pytest.mark.asyncio
    async def test_list_jobs_disallowed_namespace(self, processor_with_mocks):
        """Test listing jobs from disallowed namespace raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            await processor_with_mocks.list_jobs(namespace="kube-system")

        assert "not allowed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_list_jobs_with_status_filter_running(self, processor_with_mocks):
        """Test listing jobs with running status filter."""
        mock_job_running = self._create_mock_job("job-running", "default", active=1)
        mock_job_completed = self._create_mock_job("job-completed", "default", succeeded=1)

        # Add Complete condition to completed job
        mock_condition = MagicMock()
        mock_condition.type = "Complete"
        mock_condition.status = "True"
        mock_condition.reason = "Completed"
        mock_condition.message = "Job completed"
        mock_job_completed.status.conditions = [mock_condition]

        mock_job_list = MagicMock()
        mock_job_list.items = [mock_job_running, mock_job_completed]

        processor_with_mocks.batch_v1.list_namespaced_job = MagicMock(return_value=mock_job_list)

        jobs = await processor_with_mocks.list_jobs(namespace="default", status_filter="running")

        assert len(jobs) == 1
        assert jobs[0]["metadata"]["name"] == "job-running"

    @pytest.mark.asyncio
    async def test_list_jobs_with_status_filter_completed(self, processor_with_mocks):
        """Test listing jobs with completed status filter."""
        mock_job_running = self._create_mock_job("job-running", "default", active=1)
        mock_job_completed = self._create_mock_job("job-completed", "default", succeeded=1)

        # Add Complete condition to completed job
        mock_condition = MagicMock()
        mock_condition.type = "Complete"
        mock_condition.status = "True"
        mock_condition.reason = "Completed"
        mock_condition.message = "Job completed"
        mock_job_completed.status.conditions = [mock_condition]

        mock_job_list = MagicMock()
        mock_job_list.items = [mock_job_running, mock_job_completed]

        processor_with_mocks.batch_v1.list_namespaced_job = MagicMock(return_value=mock_job_list)

        jobs = await processor_with_mocks.list_jobs(namespace="default", status_filter="completed")

        assert len(jobs) == 1
        assert jobs[0]["metadata"]["name"] == "job-completed"

    @pytest.mark.asyncio
    async def test_list_jobs_with_status_filter_failed(self, processor_with_mocks):
        """Test listing jobs with failed status filter."""
        mock_job_running = self._create_mock_job("job-running", "default", active=1)
        mock_job_failed = self._create_mock_job("job-failed", "default", failed=1)

        # Add Failed condition to failed job
        mock_condition = MagicMock()
        mock_condition.type = "Failed"
        mock_condition.status = "True"
        mock_condition.reason = "BackoffLimitExceeded"
        mock_condition.message = "Job failed"
        mock_job_failed.status.conditions = [mock_condition]

        mock_job_list = MagicMock()
        mock_job_list.items = [mock_job_running, mock_job_failed]

        processor_with_mocks.batch_v1.list_namespaced_job = MagicMock(return_value=mock_job_list)

        jobs = await processor_with_mocks.list_jobs(namespace="default", status_filter="failed")

        assert len(jobs) == 1
        assert jobs[0]["metadata"]["name"] == "job-failed"

    @pytest.mark.asyncio
    async def test_list_jobs_api_error_continues(self, processor_with_mocks):
        """Test that API errors in one namespace don't stop listing from others."""
        mock_job = self._create_mock_job("job1", "gco-jobs", active=1)
        mock_job_list = MagicMock()
        mock_job_list.items = [mock_job]

        # First namespace fails, second succeeds
        processor_with_mocks.batch_v1.list_namespaced_job = MagicMock(
            side_effect=[ApiException(status=403, reason="Forbidden"), mock_job_list]
        )

        jobs = await processor_with_mocks.list_jobs()

        # Should still get jobs from the second namespace
        assert len(jobs) == 1

    @pytest.mark.asyncio
    async def test_list_jobs_empty_result(self, processor_with_mocks):
        """Test listing jobs when no jobs exist."""
        mock_job_list = MagicMock()
        mock_job_list.items = []

        processor_with_mocks.batch_v1.list_namespaced_job = MagicMock(return_value=mock_job_list)

        jobs = await processor_with_mocks.list_jobs(namespace="default")

        assert len(jobs) == 0


class TestJobToDictAndGetJobStatus:
    """Tests for _job_to_dict and _get_job_status helper methods."""

    @pytest.fixture
    def processor_with_mocks(self):
        """Create ManifestProcessor with mocked Kubernetes clients."""
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            return processor

    def test_get_job_status_pending(self, processor_with_mocks):
        """Test _get_job_status returns pending for job with no activity."""
        mock_job = MagicMock()
        mock_job.status.active = 0
        mock_job.status.succeeded = 0
        mock_job.status.failed = 0
        mock_job.status.conditions = []

        status = processor_with_mocks._get_job_status(mock_job)
        assert status == "pending"

    def test_get_job_status_running(self, processor_with_mocks):
        """Test _get_job_status returns running for active job."""
        mock_job = MagicMock()
        mock_job.status.active = 1
        mock_job.status.succeeded = 0
        mock_job.status.failed = 0
        mock_job.status.conditions = []

        status = processor_with_mocks._get_job_status(mock_job)
        assert status == "running"

    def test_get_job_status_completed(self, processor_with_mocks):
        """Test _get_job_status returns completed for job with Complete condition."""
        mock_job = MagicMock()
        mock_job.status.active = 0
        mock_job.status.succeeded = 1
        mock_job.status.failed = 0

        mock_condition = MagicMock()
        mock_condition.type = "Complete"
        mock_condition.status = "True"
        mock_job.status.conditions = [mock_condition]

        status = processor_with_mocks._get_job_status(mock_job)
        assert status == "completed"

    def test_get_job_status_failed(self, processor_with_mocks):
        """Test _get_job_status returns failed for job with Failed condition."""
        mock_job = MagicMock()
        mock_job.status.active = 0
        mock_job.status.succeeded = 0
        mock_job.status.failed = 1

        mock_condition = MagicMock()
        mock_condition.type = "Failed"
        mock_condition.status = "True"
        mock_job.status.conditions = [mock_condition]

        status = processor_with_mocks._get_job_status(mock_job)
        assert status == "failed"

    def test_job_to_dict_complete(self, processor_with_mocks):
        """Test _job_to_dict converts job to dictionary correctly."""
        from datetime import datetime

        mock_job = MagicMock()
        mock_job.metadata.name = "test-job"
        mock_job.metadata.namespace = "default"
        mock_job.metadata.creation_timestamp = datetime(2024, 1, 1, 0, 0, 0)
        mock_job.metadata.labels = {"app": "test"}
        mock_job.metadata.uid = "test-uid"
        mock_job.spec.parallelism = 2
        mock_job.spec.completions = 4
        mock_job.spec.backoff_limit = 3
        mock_job.status.active = 1
        mock_job.status.succeeded = 2
        mock_job.status.failed = 0
        mock_job.status.start_time = datetime(2024, 1, 1, 0, 0, 1)
        mock_job.status.completion_time = None
        mock_job.status.conditions = []

        result = processor_with_mocks._job_to_dict(mock_job)

        assert result["metadata"]["name"] == "test-job"
        assert result["metadata"]["namespace"] == "default"
        assert result["metadata"]["labels"] == {"app": "test"}
        assert result["spec"]["parallelism"] == 2
        assert result["spec"]["completions"] == 4
        assert result["status"]["active"] == 1
        assert result["status"]["succeeded"] == 2

    def test_job_to_dict_with_conditions(self, processor_with_mocks):
        """Test _job_to_dict includes conditions."""
        from datetime import datetime

        mock_job = MagicMock()
        mock_job.metadata.name = "test-job"
        mock_job.metadata.namespace = "default"
        mock_job.metadata.creation_timestamp = datetime(2024, 1, 1, 0, 0, 0)
        mock_job.metadata.labels = None
        mock_job.metadata.uid = "test-uid"
        mock_job.spec.parallelism = 1
        mock_job.spec.completions = 1
        mock_job.spec.backoff_limit = 6
        mock_job.status.active = 0
        mock_job.status.succeeded = 1
        mock_job.status.failed = 0
        mock_job.status.start_time = datetime(2024, 1, 1, 0, 0, 1)
        mock_job.status.completion_time = datetime(2024, 1, 1, 0, 1, 0)

        mock_condition = MagicMock()
        mock_condition.type = "Complete"
        mock_condition.status = "True"
        mock_condition.reason = "Completed"
        mock_condition.message = "Job completed successfully"
        mock_job.status.conditions = [mock_condition]

        result = processor_with_mocks._job_to_dict(mock_job)

        assert len(result["status"]["conditions"]) == 1
        assert result["status"]["conditions"][0]["type"] == "Complete"
        assert result["status"]["completionTime"] is not None


class TestValidationEdgeCases:
    """Tests for validation edge cases."""

    def test_validation_exception_handling(self, processor):
        """Test validation handles exceptions gracefully."""
        # Create a manifest that will cause an exception during validation
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "test", "namespace": "default"},
            "spec": None,  # This will cause issues when accessing spec
        }
        is_valid, error = processor.validate_manifest(manifest)
        # Should handle gracefully - either pass or return validation error
        assert isinstance(is_valid, bool)

    def test_pod_manifest_validation(self, processor):
        """Test Pod manifest validation (containers directly in spec)."""
        manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "test-pod", "namespace": "default"},
            "spec": {
                "containers": [
                    {
                        "name": "app",
                        "image": "docker.io/nginx:latest",
                        "resources": {
                            "limits": {"cpu": "500m", "memory": "512Mi"},
                        },
                    }
                ],
            },
        }
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is True

    def test_container_without_resources(self, processor):
        """Test container without resources section."""
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "test", "namespace": "default"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "app",
                                "image": "docker.io/nginx:latest",
                                # No resources section
                            }
                        ]
                    }
                }
            },
        }
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is True

    def test_container_with_only_requests(self, processor):
        """Test container with only requests (no limits)."""
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "test", "namespace": "default"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "app",
                                "image": "docker.io/nginx:latest",
                                "resources": {
                                    "requests": {"cpu": "100m", "memory": "128Mi"},
                                },
                            }
                        ]
                    }
                }
            },
        }
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is True


class TestGetExistingResourceExtended:
    """Tests for _get_existing_resource method."""

    @pytest.fixture
    def processor_with_mocks(self):
        """Create ManifestProcessor with mocked Kubernetes clients."""
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            return processor

    def _setup_dynamic_mock(self, processor, return_value=None, side_effect=None):
        """Helper to set up dynamic client mock."""
        mock_resource_obj = MagicMock()
        if return_value:
            mock_resource_obj.to_dict.return_value = return_value
        mock_resource = MagicMock()
        mock_resource.namespaced = True
        if side_effect:
            mock_resource.get = MagicMock(side_effect=side_effect)
        else:
            mock_resource.get = MagicMock(return_value=mock_resource_obj if return_value else None)
        processor._dynamic_client = MagicMock()
        processor._dynamic_client.resources.get.return_value = mock_resource

    @pytest.mark.asyncio
    async def test_get_existing_deployment(self, processor_with_mocks):
        """Test getting existing deployment."""
        self._setup_dynamic_mock(processor_with_mocks, {"metadata": {"name": "test"}})

        result = await processor_with_mocks._get_existing_resource(
            "apps/v1", "Deployment", "test", "default"
        )

        assert result is not None
        assert result["metadata"]["name"] == "test"

    @pytest.mark.asyncio
    async def test_get_existing_service(self, processor_with_mocks):
        """Test getting existing service."""
        self._setup_dynamic_mock(processor_with_mocks, {"metadata": {"name": "test-svc"}})

        result = await processor_with_mocks._get_existing_resource(
            "v1", "Service", "test-svc", "default"
        )

        assert result is not None

    @pytest.mark.asyncio
    async def test_get_existing_job(self, processor_with_mocks):
        """Test getting existing job."""
        self._setup_dynamic_mock(processor_with_mocks, {"metadata": {"name": "test-job"}})

        result = await processor_with_mocks._get_existing_resource(
            "batch/v1", "Job", "test-job", "default"
        )

        assert result is not None

    @pytest.mark.asyncio
    async def test_get_existing_configmap(self, processor_with_mocks):
        """Test getting existing configmap."""
        self._setup_dynamic_mock(processor_with_mocks, {"metadata": {"name": "test-cm"}})

        result = await processor_with_mocks._get_existing_resource(
            "v1", "ConfigMap", "test-cm", "default"
        )

        assert result is not None

    @pytest.mark.asyncio
    async def test_get_existing_secret(self, processor_with_mocks):
        """Test getting existing secret."""
        self._setup_dynamic_mock(processor_with_mocks, {"metadata": {"name": "test-secret"}})

        result = await processor_with_mocks._get_existing_resource(
            "v1", "Secret", "test-secret", "default"
        )

        assert result is not None

    @pytest.mark.asyncio
    async def test_get_existing_resource_not_found(self, processor_with_mocks):
        """Test getting non-existent resource returns None."""
        self._setup_dynamic_mock(
            processor_with_mocks, side_effect=ApiException(status=404, reason="Not Found")
        )

        result = await processor_with_mocks._get_existing_resource(
            "apps/v1", "Deployment", "nonexistent", "default"
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_get_existing_resource_api_error(self, processor_with_mocks):
        """Test getting resource with API error raises exception."""
        self._setup_dynamic_mock(
            processor_with_mocks,
            side_effect=ApiException(status=500, reason="Internal Server Error"),
        )

        try:
            await processor_with_mocks._get_existing_resource(
                "apps/v1", "Deployment", "test", "default"
            )
            pytest.fail("Should have raised ApiException")
        except ApiException:
            pass  # Expected


class TestCreateAndUpdateResource:
    """Tests for _create_resource and _update_resource methods."""

    @pytest.fixture
    def processor_with_mocks(self):
        """Create ManifestProcessor with mocked Kubernetes clients."""
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            return processor

    @pytest.mark.asyncio
    async def test_create_resource_success(self, processor_with_mocks):
        """Test successful resource creation."""
        # Mock the dynamic client
        created_resource = MagicMock()
        mock_resource = MagicMock()
        mock_resource.namespaced = True
        mock_resource.create = MagicMock(return_value=created_resource)
        processor_with_mocks._dynamic_client = MagicMock()
        processor_with_mocks._dynamic_client.resources.get.return_value = mock_resource

        manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "test", "namespace": "default"},
        }

        result = await processor_with_mocks._create_resource(manifest)
        assert result is created_resource
        mock_resource.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_resource_failure(self, processor_with_mocks):
        """Test resource creation failure."""
        # Mock the dynamic client to raise an exception
        mock_resource = MagicMock()
        mock_resource.namespaced = True
        mock_resource.create = MagicMock(side_effect=Exception("Creation failed"))
        processor_with_mocks._dynamic_client = MagicMock()
        processor_with_mocks._dynamic_client.resources.get.return_value = mock_resource

        manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "test", "namespace": "default"},
        }

        try:
            await processor_with_mocks._create_resource(manifest)
            pytest.fail("Should have raised exception")
        except Exception as e:
            assert "Creation failed" in str(e)

    @pytest.mark.asyncio
    async def test_update_resource_success(self, processor_with_mocks):
        """Test successful resource update."""
        # Mock the dynamic client
        mock_resource = MagicMock()
        mock_resource.namespaced = True
        mock_resource.patch = MagicMock()
        processor_with_mocks._dynamic_client = MagicMock()
        processor_with_mocks._dynamic_client.resources.get.return_value = mock_resource

        manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "test", "namespace": "default"},
        }

        result = await processor_with_mocks._update_resource(manifest)
        assert result is True
        mock_resource.patch.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_resource_failure(self, processor_with_mocks):
        """Test resource update failure."""
        # Mock the dynamic client to raise an exception
        mock_resource = MagicMock()
        mock_resource.namespaced = True
        mock_resource.patch = MagicMock(side_effect=Exception("Update failed"))
        processor_with_mocks._dynamic_client = MagicMock()
        processor_with_mocks._dynamic_client.resources.get.return_value = mock_resource

        manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "test", "namespace": "default"},
        }

        try:
            await processor_with_mocks._update_resource(manifest)
            pytest.fail("Should have raised exception")
        except Exception as e:
            assert "Update failed" in str(e)


# =========================================================================
# Additional coverage tests targeting uncovered lines
# =========================================================================


class TestDynamicClientProperty:
    """Tests for the dynamic_client lazy property (line 136)."""

    def test_dynamic_client_lazy_init(self):
        """Test that dynamic_client is lazily initialized (line 136)."""
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            assert processor._dynamic_client is None

            mock_dynamic = MagicMock()
            with patch(
                "gco.services.manifest_processor.dynamic.DynamicClient",
                return_value=mock_dynamic,
            ):
                result = processor.dynamic_client
                assert result == mock_dynamic

            # Second access should return cached instance
            result2 = processor.dynamic_client
            assert result2 == mock_dynamic


class TestValidateResourceLimitsExceptionPath:
    """Tests for _validate_resource_limits exception handling (lines 223-225)."""

    def test_validate_resource_limits_exception(self):
        """Test _validate_resource_limits returns False on exception (line 223-225)."""
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            # Manifest with invalid structure that causes parsing error
            manifest = {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "test", "namespace": "default"},
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "app",
                                    "image": "docker.io/nginx:latest",
                                    "resources": {
                                        "limits": {"cpu": "not-a-number"},
                                    },
                                }
                            ]
                        }
                    }
                },
            }
            result = processor._validate_resource_limits(manifest)
            assert result[0] is False


class TestValidateResourceLimitsGpuExceeded:
    """Tests for GPU limit exceeded path (lines 244-245)."""

    def test_gpu_limit_exceeded(self):
        """Test that GPU limit exceeded returns False (lines 244-245)."""
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "max_cpu_per_manifest": "100",
                    "max_memory_per_manifest": "128Gi",
                    "max_gpu_per_manifest": 2,
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            manifest = {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": "gpu-job", "namespace": "default"},
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "gpu-worker",
                                    "image": "docker.io/nvidia/cuda:latest",
                                    "resources": {
                                        "limits": {
                                            "cpu": "1",
                                            "memory": "4Gi",
                                            "nvidia.com/gpu": "4",
                                        },
                                    },
                                }
                            ]
                        }
                    }
                },
            }
            result = processor._validate_resource_limits(manifest)
            assert result[0] is False


class TestValidateSecurityContextExceptionPath:
    """Tests for _validate_security_context exception handling (lines 323-325)."""

    def test_security_context_exception(self):
        """Test _validate_security_context returns False on exception (lines 323-325)."""
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            # Cause an exception by making containers a non-iterable type
            # that passes the pod_spec truthy check but fails on iteration
            manifest = {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "test", "namespace": "default"},
                "spec": {
                    "template": {
                        "spec": {
                            "securityContext": MagicMock(
                                get=MagicMock(side_effect=TypeError("boom"))
                            ),
                        }
                    }
                },
            }
            result = processor._validate_security_context(manifest)
            is_valid, error = result
            assert is_valid is False


class TestValidateImageSourcesExceptionPath:
    """Tests for _validate_image_sources exception handling (lines 385-387)."""

    def test_image_sources_exception(self):
        """Test _validate_image_sources returns False on exception (lines 385-387)."""
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            # Cause an exception by making containers not iterable
            manifest = {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "test", "namespace": "default"},
                "spec": {"template": {"spec": {"containers": "not-a-list"}}},
            }
            result = processor._validate_image_sources(manifest)
            is_valid, error = result
            assert is_valid is False


class TestImageSourceUntrustedDockerHubOrg:
    """Tests for untrusted Docker Hub org path (line 377)."""

    def test_untrusted_dockerhub_org(self):
        """Test that untrusted Docker Hub org is rejected (line 377)."""
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            manifest = {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "test", "namespace": "default"},
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "app",
                                    "image": "evilorg/malware:latest",
                                }
                            ]
                        }
                    }
                },
            }
            result = processor._validate_image_sources(manifest)
            is_valid, error = result
            assert is_valid is False


class TestProcessManifestSubmissionErrorPaths:
    """Tests for process_manifest_submission error paths (lines 430-433, 465-469)."""

    @pytest.fixture
    def processor_with_mocks(self):
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default", "gco-jobs"],
                    "validation_enabled": True,
                },
            )
            return processor

    @pytest.mark.asyncio
    async def test_process_manifest_inner_exception(self, processor_with_mocks):
        """Test inner exception handling during manifest processing (lines 430-433)."""
        processor = processor_with_mocks

        # Make validate_manifest raise an unexpected exception
        with patch.object(
            processor, "validate_manifest", side_effect=RuntimeError("Unexpected error")
        ):
            request = ManifestSubmissionRequest(
                manifests=[
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {"name": "test", "namespace": "default"},
                        "data": {"key": "value"},
                    }
                ],
                dry_run=False,
            )
            response = await processor.process_manifest_submission(request)
            assert response.success is False
            assert len(response.errors) > 0
            assert "Error processing manifest 1" in response.errors[0]

    @pytest.mark.asyncio
    async def test_process_manifest_fatal_error(self, processor_with_mocks):
        """Test fatal error handling during manifest processing (lines 465-469)."""
        processor = processor_with_mocks

        # Create a request where iterating manifests raises
        request = MagicMock(spec=ManifestSubmissionRequest)
        request.manifests = MagicMock()
        request.manifests.__iter__ = MagicMock(side_effect=RuntimeError("Fatal iteration error"))
        request.dry_run = False
        request.namespace = "default"

        response = await processor.process_manifest_submission(request)
        assert response.success is False
        assert any("Fatal error" in e for e in response.errors)

    @pytest.mark.asyncio
    async def test_process_manifest_apply_failure(self, processor_with_mocks):
        """Test that failed apply sets overall_success to False."""
        processor = processor_with_mocks

        from gco.models import ResourceStatus

        failed_status = ResourceStatus(
            api_version="v1",
            kind="ConfigMap",
            name="test",
            namespace="default",
            status="failed",
            message="Apply failed",
        )

        with (
            patch.object(processor, "validate_manifest", return_value=(True, None)),
            patch.object(processor, "_apply_manifest", return_value=failed_status),
        ):
            request = ManifestSubmissionRequest(
                manifests=[
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {"name": "test", "namespace": "default"},
                        "data": {"key": "value"},
                    }
                ],
                dry_run=False,
            )
            response = await processor.process_manifest_submission(request)
            assert response.success is False


class TestApplyManifestGenericException:
    """Tests for _apply_manifest generic exception path (lines 577-579)."""

    @pytest.fixture
    def processor_with_mocks(self):
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            return processor

    @pytest.mark.asyncio
    async def test_apply_manifest_generic_exception(self, processor_with_mocks):
        """Test _apply_manifest handles generic Exception (lines 577-579)."""
        processor = processor_with_mocks

        with patch.object(
            processor,
            "_get_existing_resource",
            side_effect=RuntimeError("Connection refused"),
        ):
            manifest = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "test", "namespace": "default"},
            }
            result = await processor._apply_manifest(manifest)
            assert result.status == "failed"
            assert "Connection refused" in result.message


class TestIsJobFinishedEdgeCases:
    """Tests for _is_job_finished edge cases (lines 590-597)."""

    def test_job_finished_complete(self):
        """Test _is_job_finished with Complete condition."""
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            job_resource = {
                "status": {
                    "conditions": [
                        {"type": "Complete", "status": "True"},
                    ]
                }
            }
            assert processor._is_job_finished(job_resource) is True

    def test_job_finished_failed(self):
        """Test _is_job_finished with Failed condition."""
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            job_resource = {
                "status": {
                    "conditions": [
                        {"type": "Failed", "status": "True"},
                    ]
                }
            }
            assert processor._is_job_finished(job_resource) is True

    def test_job_not_finished(self):
        """Test _is_job_finished with no terminal condition."""
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            job_resource = {
                "status": {
                    "conditions": [
                        {"type": "Complete", "status": "False"},
                    ]
                }
            }
            assert processor._is_job_finished(job_resource) is False

    def test_job_no_conditions(self):
        """Test _is_job_finished with no conditions."""
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            job_resource = {"status": {}}
            assert processor._is_job_finished(job_resource) is False

    def test_job_null_conditions(self):
        """Test _is_job_finished with None conditions."""
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            job_resource = {"status": {"conditions": None}}
            assert processor._is_job_finished(job_resource) is False


class TestGetExistingResourceValueError:
    """Tests for _get_existing_resource ValueError path (line 622-624)."""

    @pytest.fixture
    def processor_with_mocks(self):
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            return processor

    @pytest.mark.asyncio
    async def test_get_existing_resource_value_error(self, processor_with_mocks):
        """Test _get_existing_resource returns None on ValueError (lines 622-624)."""
        processor = processor_with_mocks
        processor._dynamic_client = MagicMock()
        processor._dynamic_client.resources.get.side_effect = ValueError("Unknown resource type")

        result = await processor._get_existing_resource("v1", "UnknownKind", "test", "default")
        assert result is None


class TestGetApiResourceError:
    """Tests for _get_api_resource error path (line 648)."""

    def test_get_api_resource_not_found(self):
        """Test _get_api_resource raises ValueError on ResourceNotFoundError."""
        from kubernetes.dynamic.exceptions import ResourceNotFoundError

        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            processor._dynamic_client = MagicMock()
            processor._dynamic_client.resources.get.side_effect = ResourceNotFoundError("Not found")

            with pytest.raises(ValueError, match="Unknown resource type"):
                processor._get_api_resource("v1", "NonExistentKind")


class TestCreateResourceNonNamespaced:
    """Tests for _create_resource non-namespaced path (line 675)."""

    @pytest.fixture
    def processor_with_mocks(self):
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            return processor

    @pytest.mark.asyncio
    async def test_create_resource_non_namespaced(self, processor_with_mocks):
        """Test creating a non-namespaced resource (line 675)."""
        processor = processor_with_mocks
        created_resource = MagicMock()
        mock_resource = MagicMock()
        mock_resource.namespaced = False
        mock_resource.create = MagicMock(return_value=created_resource)
        processor._dynamic_client = MagicMock()
        processor._dynamic_client.resources.get.return_value = mock_resource

        manifest = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": "test-ns"},
        }

        result = await processor._create_resource(manifest)
        assert result is created_resource
        mock_resource.create.assert_called_once_with(body=manifest)


class TestUpdateResourceNonNamespaced:
    """Tests for _update_resource non-namespaced path (line 700)."""

    @pytest.fixture
    def processor_with_mocks(self):
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            return processor

    @pytest.mark.asyncio
    async def test_update_resource_non_namespaced(self, processor_with_mocks):
        """Test updating a non-namespaced resource (line 700)."""
        processor = processor_with_mocks
        mock_resource = MagicMock()
        mock_resource.namespaced = False
        mock_resource.patch = MagicMock()
        processor._dynamic_client = MagicMock()
        processor._dynamic_client.resources.get.return_value = mock_resource

        manifest = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": "test-ns"},
        }

        result = await processor._update_resource(manifest)
        assert result is True
        mock_resource.patch.assert_called_once_with(
            body=manifest,
            name="test-ns",
            content_type="application/merge-patch+json",
        )


class TestDeleteResourceEdgeCases:
    """Tests for delete_resource edge cases (lines 740-741)."""

    @pytest.fixture
    def processor_with_mocks(self):
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            return processor

    @pytest.mark.asyncio
    async def test_delete_resource_generic_exception(self, processor_with_mocks):
        """Test delete_resource handles generic Exception (lines 740-741)."""
        processor = processor_with_mocks
        mock_resource = MagicMock()
        mock_resource.namespaced = True
        mock_resource.delete = MagicMock(side_effect=RuntimeError("Unexpected delete error"))
        processor._dynamic_client = MagicMock()
        processor._dynamic_client.resources.get.return_value = mock_resource

        result = await processor.delete_resource("v1", "ConfigMap", "test", "default")
        assert result.status == "failed"
        assert "Unexpected delete error" in result.message

    @pytest.mark.asyncio
    async def test_delete_resource_non_namespaced(self, processor_with_mocks):
        """Test deleting a non-namespaced resource."""
        processor = processor_with_mocks
        mock_resource = MagicMock()
        mock_resource.namespaced = False
        mock_resource.delete = MagicMock()
        processor._dynamic_client = MagicMock()
        processor._dynamic_client.resources.get.return_value = mock_resource

        result = await processor.delete_resource("v1", "Namespace", "test-ns", "cluster-scope")
        assert result.status == "deleted"
        mock_resource.delete.assert_called_once_with(name="test-ns")

    @pytest.mark.asyncio
    async def test_delete_resource_value_error(self, processor_with_mocks):
        """Test delete_resource handles ValueError for unknown resource type."""
        processor = processor_with_mocks
        processor._dynamic_client = MagicMock()
        processor._dynamic_client.resources.get.side_effect = ValueError("Unknown resource type")

        with patch.object(
            processor,
            "_get_api_resource",
            side_effect=ValueError("Unknown resource type: v1/UnknownKind"),
        ):
            result = await processor.delete_resource("v1", "UnknownKind", "test", "default")
            assert result.status == "failed"
            assert "Unknown resource type" in result.message


class TestGetResourceStatusExceptionPath:
    """Tests for get_resource_status exception path (line 611)."""

    @pytest.fixture
    def processor_with_mocks(self):
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            return processor

    @pytest.mark.asyncio
    async def test_get_resource_status_exception_returns_none(self, processor_with_mocks):
        """Test get_resource_status returns None on exception (line 611)."""
        processor = processor_with_mocks

        with patch.object(
            processor,
            "_get_existing_resource",
            side_effect=RuntimeError("Connection error"),
        ):
            result = await processor.get_resource_status("v1", "ConfigMap", "test", "default")
            assert result is None


class TestGetExistingResourceNonNamespaced:
    """Tests for _get_existing_resource non-namespaced path."""

    @pytest.fixture
    def processor_with_mocks(self):
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            return processor

    @pytest.mark.asyncio
    async def test_get_existing_resource_non_namespaced(self, processor_with_mocks):
        """Test _get_existing_resource for non-namespaced resource."""
        processor = processor_with_mocks
        mock_resource_api = MagicMock()
        mock_resource_api.namespaced = False
        mock_result = MagicMock()
        mock_result.to_dict.return_value = {"metadata": {"name": "test-ns"}, "status": {}}
        mock_resource_api.get.return_value = mock_result
        processor._dynamic_client = MagicMock()
        processor._dynamic_client.resources.get.return_value = mock_resource_api

        result = await processor._get_existing_resource("v1", "Namespace", "test-ns", "")
        assert result is not None
        assert result["metadata"]["name"] == "test-ns"
        mock_resource_api.get.assert_called_once_with(name="test-ns")


class TestValidateManifestExceptionPath:
    """Tests for validate_manifest exception handling (line 136 in validate_manifest)."""

    def test_validate_manifest_catches_exception(self):
        """Test validate_manifest catches unexpected exceptions."""
        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
            # Patch _validate_resource_limits to raise
            with patch.object(
                processor,
                "_validate_resource_limits",
                side_effect=RuntimeError("Unexpected"),
            ):
                manifest = {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {"name": "test", "namespace": "default"},
                    "spec": {},
                }
                is_valid, error = processor.validate_manifest(manifest)
                assert is_valid is False
                assert "Validation error" in error
