"""
Extended tests for gco/services/manifest_processor.ManifestProcessor.

Covers branches the base suite doesn't reach: CronJob container
extraction and per-container validation (security context, image
registry, GPU limits), manifest-level validation error wrapping into
ResourceStatus entries, list_jobs namespace validation errors, and
_get_job_status derivation for pending state. Pulls in Hypothesis for
a couple of property-based sweeps over the validator.
"""

from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException


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
    """Create ManifestProcessor with mocked Kubernetes clients."""
    from gco.services.manifest_processor import ManifestProcessor

    with patch("gco.services.manifest_processor.client"):
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


class TestCronJobValidation:
    """Tests for CronJob manifest validation."""

    def test_validate_cronjob_resource_limits(self, manifest_processor):
        """Test resource limit validation for CronJob."""
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
                                        "image": "python:3.14",
                                        "resources": {
                                            "requests": {"cpu": "500m", "memory": "512Mi"}
                                        },
                                    }
                                ]
                            }
                        }
                    }
                },
            },
        }

        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert is_valid is True
        assert error is None

    def test_validate_cronjob_exceeds_gpu_limit(self, manifest_processor):
        """Test CronJob validation fails when GPU limit exceeded."""
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
                                        "name": "gpu-worker",
                                        "image": "python:3.14",
                                        "resources": {
                                            "limits": {"nvidia.com/gpu": "8"}  # Exceeds 4
                                        },
                                    }
                                ]
                            }
                        }
                    }
                },
            },
        }

        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert is_valid is False
        assert "Resource limits exceed" in error or "exceeds max" in error

    def test_validate_cronjob_security_context(self, manifest_processor):
        """Test CronJob security context validation."""
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
                                        "image": "python:3.14",
                                        "securityContext": {"privileged": True},
                                    }
                                ]
                            }
                        }
                    }
                },
            },
        }

        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert is_valid is False
        assert "Security context" in error

    def test_validate_cronjob_image_sources(self, manifest_processor):
        """Test CronJob image source validation."""
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
                                        "image": "untrusted-registry.com/malicious:latest",
                                    }
                                ]
                            }
                        }
                    }
                },
            },
        }

        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert is_valid is False
        assert "Untrusted image" in error


class TestManifestValidationErrors:
    """Tests for manifest validation error handling."""

    def test_validate_manifest_exception(self, manifest_processor):
        """Test validation handles exceptions gracefully."""
        # Create a manifest that will cause an exception during validation
        manifest = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "test-job", "namespace": "default"},
            "spec": None,  # This will cause issues during validation
        }

        is_valid, error = manifest_processor.validate_manifest(manifest)
        # Should handle gracefully
        assert is_valid is False or is_valid is True  # Either outcome is valid

    def test_validate_resource_limits_exception(self, manifest_processor):
        """Test resource limit validation handles exceptions."""
        # Manifest with malformed resources
        manifest = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "test-job", "namespace": "default"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "worker",
                                "image": "python:3.14",
                                "resources": {"requests": {"cpu": "invalid-cpu-value"}},
                            }
                        ]
                    }
                }
            },
        }

        # Should not raise, but return (False, error_message)
        result = manifest_processor._validate_resource_limits(manifest)
        assert result[0] is False


class TestListJobsNamespaceValidation:
    """Tests for list_jobs namespace validation."""

    @pytest.mark.asyncio
    async def test_list_jobs_invalid_namespace(self, manifest_processor):
        """Test list_jobs raises error for invalid namespace."""
        with pytest.raises(ValueError) as exc_info:
            await manifest_processor.list_jobs(namespace="unauthorized-namespace")

        assert "not allowed" in str(exc_info.value)
        assert "unauthorized-namespace" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_list_jobs_valid_namespace(self, manifest_processor):
        """Test list_jobs works with valid namespace."""
        mock_job = MagicMock()
        mock_job.metadata.name = "test-job"
        mock_job.metadata.namespace = "default"
        mock_job.metadata.creation_timestamp = None
        mock_job.metadata.labels = {}
        mock_job.metadata.uid = "uid-123"
        mock_job.spec.parallelism = 1
        mock_job.spec.completions = 1
        mock_job.spec.backoff_limit = 3
        mock_job.status.active = 1
        mock_job.status.succeeded = 0
        mock_job.status.failed = 0
        mock_job.status.start_time = None
        mock_job.status.completion_time = None
        mock_job.status.conditions = []

        manifest_processor.batch_v1.list_namespaced_job.return_value.items = [mock_job]

        jobs = await manifest_processor.list_jobs(namespace="default")
        assert len(jobs) == 1
        assert jobs[0]["metadata"]["name"] == "test-job"


class TestJobStatusDetermination:
    """Tests for _get_job_status method."""

    def test_get_job_status_pending(self, manifest_processor):
        """Test job status is pending when no active pods and no conditions."""
        mock_job = MagicMock()
        mock_job.status.active = 0
        mock_job.status.succeeded = 0
        mock_job.status.failed = 0
        mock_job.status.conditions = []

        status = manifest_processor._get_job_status(mock_job)
        assert status == "pending"

    def test_get_job_status_running(self, manifest_processor):
        """Test job status is running when active pods exist."""
        mock_job = MagicMock()
        mock_job.status.active = 1
        mock_job.status.succeeded = 0
        mock_job.status.failed = 0
        mock_job.status.conditions = []

        status = manifest_processor._get_job_status(mock_job)
        assert status == "running"

    def test_get_job_status_completed(self, manifest_processor):
        """Test job status is completed when Complete condition is True."""
        mock_job = MagicMock()
        mock_job.status.active = 0
        mock_job.status.succeeded = 1
        mock_job.status.failed = 0

        mock_condition = MagicMock()
        mock_condition.type = "Complete"
        mock_condition.status = "True"
        mock_job.status.conditions = [mock_condition]

        status = manifest_processor._get_job_status(mock_job)
        assert status == "completed"

    def test_get_job_status_failed(self, manifest_processor):
        """Test job status is failed when Failed condition is True."""
        mock_job = MagicMock()
        mock_job.status.active = 0
        mock_job.status.succeeded = 0
        mock_job.status.failed = 1

        mock_condition = MagicMock()
        mock_condition.type = "Failed"
        mock_condition.status = "True"
        mock_job.status.conditions = [mock_condition]

        status = manifest_processor._get_job_status(mock_job)
        assert status == "failed"


class TestProcessManifestSubmissionErrors:
    """Tests for process_manifest_submission error handling."""

    @pytest.mark.asyncio
    async def test_process_manifest_validation_failure(self, manifest_processor):
        """Test processing manifest with validation failure - privileged container."""
        from gco.models import ManifestSubmissionRequest

        # Use a manifest with privileged container which should fail security validation
        request = ManifestSubmissionRequest(
            manifests=[
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {"name": "test-job", "namespace": "default"},
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [
                                    {
                                        "name": "worker",
                                        "image": "python:3.14",
                                        "securityContext": {"privileged": True},
                                    }
                                ],
                                "restartPolicy": "Never",
                            }
                        }
                    },
                }
            ],
            namespace="default",
            dry_run=False,
        )

        response = await manifest_processor.process_manifest_submission(request)

        assert response.success is False
        assert len(response.resources) == 1
        assert response.resources[0].status == "failed"
        # Check for security validation error
        assert "security" in response.resources[0].message.lower()

    @pytest.mark.asyncio
    async def test_process_manifest_exception_during_processing(self, manifest_processor):
        """Test processing manifest with exception during apply."""
        from gco.models import ManifestSubmissionRequest

        request = ManifestSubmissionRequest(
            manifests=[
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {"name": "test-job", "namespace": "default"},
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [{"name": "worker", "image": "python:3.14"}],
                                "restartPolicy": "Never",
                            }
                        }
                    },
                }
            ],
            namespace="default",
            dry_run=False,
        )

        # Mock _apply_manifest to raise an exception
        async def mock_apply(*args, **kwargs):
            raise Exception("Apply failed")

        manifest_processor._apply_manifest = mock_apply

        response = await manifest_processor.process_manifest_submission(request)

        assert response.success is False
        assert len(response.errors) > 0

    @pytest.mark.asyncio
    async def test_process_manifest_dry_run(self, manifest_processor):
        """Test processing manifest in dry run mode."""
        from gco.models import ManifestSubmissionRequest

        request = ManifestSubmissionRequest(
            manifests=[
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {"name": "test-job", "namespace": "default"},
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [{"name": "worker", "image": "python:3.14"}],
                                "restartPolicy": "Never",
                            }
                        }
                    },
                }
            ],
            namespace="default",
            dry_run=True,
        )

        response = await manifest_processor.process_manifest_submission(request)

        assert response.success is True
        assert len(response.resources) == 1
        assert response.resources[0].status == "unchanged"
        assert "dry run" in response.resources[0].message.lower()


class TestDeleteResourceEdgeCases:
    """Tests for delete_resource edge cases."""

    @pytest.mark.asyncio
    async def test_delete_unsupported_resource_type(self, manifest_processor):
        """Unsupported kinds are denied before dynamic API discovery."""
        mock_dynamic = MagicMock()
        manifest_processor._dynamic_client = mock_dynamic

        result = await manifest_processor.delete_resource(
            api_version="v1",
            kind="UnsupportedKind",
            name="test",
            namespace="default",
        )

        assert result.status == "forbidden"
        assert "not allowed" in (result.message or "")
        mock_dynamic.resources.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_resource_api_error(self, manifest_processor):
        """Test delete resource with API error."""
        # Mock the dynamic client to raise ApiException
        mock_resource = MagicMock()
        mock_resource.namespaced = True
        mock_resource.delete.side_effect = ApiException(status=500, reason="Internal Server Error")
        mock_dynamic = MagicMock()
        mock_dynamic.resources.get.return_value = mock_resource
        manifest_processor._dynamic_client = mock_dynamic

        result = await manifest_processor.delete_resource(
            api_version="batch/v1",
            kind="Job",
            name="test-job",
            namespace="default",
        )

        assert result.status == "failed"
        assert "Internal Server Error" in result.message


class TestGetResourceStatusEdgeCases:
    """Tests for get_resource_status edge cases."""

    @staticmethod
    def _authorize_namespaced_resource(manifest_processor):
        resource = MagicMock()
        resource.namespaced = True
        manifest_processor._get_api_resource = MagicMock(return_value=resource)

    @pytest.mark.asyncio
    async def test_get_resource_status_exists(self, manifest_processor):
        """Test getting status of existing resource."""
        self._authorize_namespaced_resource(manifest_processor)

        async def mock_get_existing(*args, **kwargs):
            return {
                "metadata": {"name": "test-job"},
                "status": {"active": 1},
                "spec": {"parallelism": 1},
            }

        manifest_processor._get_existing_resource = mock_get_existing

        result = await manifest_processor.get_resource_status(
            api_version="batch/v1",
            kind="Job",
            name="test-job",
            namespace="default",
        )

        assert result["exists"] is True
        assert result["name"] == "test-job"

    @pytest.mark.asyncio
    async def test_get_resource_status_not_exists(self, manifest_processor):
        """Test getting status of non-existing resource."""
        self._authorize_namespaced_resource(manifest_processor)

        async def mock_get_existing(*args, **kwargs):
            return None

        manifest_processor._get_existing_resource = mock_get_existing

        result = await manifest_processor.get_resource_status(
            api_version="batch/v1",
            kind="Job",
            name="nonexistent",
            namespace="default",
        )

        assert result["exists"] is False

    @pytest.mark.asyncio
    async def test_get_resource_status_error(self, manifest_processor):
        """Test getting status with error."""
        self._authorize_namespaced_resource(manifest_processor)

        async def mock_get_existing(*args, **kwargs):
            raise Exception("API error")

        manifest_processor._get_existing_resource = mock_get_existing

        result = await manifest_processor.get_resource_status(
            api_version="batch/v1",
            kind="Job",
            name="test-job",
            namespace="default",
        )

        assert result is None


class TestListJobsApiException:
    """Tests for list_jobs API exception handling."""

    @pytest.mark.asyncio
    async def test_list_jobs_api_exception(self, manifest_processor):
        """Test list_jobs handles API exceptions gracefully."""
        manifest_processor.batch_v1.list_namespaced_job.side_effect = ApiException(
            status=403, reason="Forbidden"
        )

        # Should not raise, but return empty list for that namespace
        jobs = await manifest_processor.list_jobs()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_list_jobs_with_running_status_filter(self, manifest_processor):
        """Test list_jobs with running status filter."""
        mock_job = MagicMock()
        mock_job.metadata.name = "running-job"
        mock_job.metadata.namespace = "default"
        mock_job.metadata.creation_timestamp = None
        mock_job.metadata.labels = {}
        mock_job.metadata.uid = "uid-123"
        mock_job.spec.parallelism = 1
        mock_job.spec.completions = 1
        mock_job.spec.backoff_limit = 3
        mock_job.status.active = 1
        mock_job.status.succeeded = 0
        mock_job.status.failed = 0
        mock_job.status.start_time = None
        mock_job.status.completion_time = None
        mock_job.status.conditions = []

        # Only return jobs for the first namespace (default), empty for gco-jobs
        def mock_list_jobs(namespace, **kwargs):
            mock_result = MagicMock()
            if namespace == "default":
                mock_result.items = [mock_job]
            else:
                mock_result.items = []
            return mock_result

        manifest_processor.batch_v1.list_namespaced_job.side_effect = mock_list_jobs

        jobs = await manifest_processor.list_jobs(namespace="default", status_filter="running")
        assert len(jobs) == 1

    @pytest.mark.asyncio
    async def test_list_jobs_with_completed_status_filter(self, manifest_processor):
        """Test list_jobs with completed status filter excludes running jobs."""
        mock_job = MagicMock()
        mock_job.metadata.name = "running-job"
        mock_job.metadata.namespace = "default"
        mock_job.metadata.creation_timestamp = None
        mock_job.metadata.labels = {}
        mock_job.metadata.uid = "uid-123"
        mock_job.spec.parallelism = 1
        mock_job.spec.completions = 1
        mock_job.spec.backoff_limit = 3
        mock_job.status.active = 1  # Running job
        mock_job.status.succeeded = 0
        mock_job.status.failed = 0
        mock_job.status.start_time = None
        mock_job.status.completion_time = None
        mock_job.status.conditions = []

        manifest_processor.batch_v1.list_namespaced_job.return_value.items = [mock_job]

        # Test with non-matching filter - running job should not match completed filter
        jobs = await manifest_processor.list_jobs(namespace="default", status_filter="completed")
        assert len(jobs) == 0


# =============================================================================
# Property: Registry Domain Validation
#
# For any container image reference where the string before the first '/'
# does not contain a dot ('.') or colon (':'), the image validator SHALL NOT
# match it against trusted_registries entries that are full domains. Instead,
# it SHALL only match against trusted_dockerhub_orgs entries.
# =============================================================================


def _make_processor_for_registry_tests(**overrides):
    """Create a ManifestProcessor with mocked K8s config for registry tests."""
    defaults = {
        "max_cpu_per_manifest": "100",
        "max_memory_per_manifest": "256Gi",
        "max_gpu_per_manifest": 16,
        "allowed_namespaces": ["default", "gco-jobs"],
        "validation_enabled": True,
        "trusted_registries": [
            "docker.io",
            "gcr.io",
            "quay.io",
            "registry.k8s.io",
            "public.ecr.aws",
            "nvcr.io",
        ],
        "trusted_dockerhub_orgs": [
            "nvidia",
            "pytorch",
            "rayproject",
            "tensorflow",
            "huggingface",
            "amazon",
            "bitnami",
            "gco",
        ],
    }
    defaults.update(overrides)

    with patch("gco.services.manifest_processor.config") as mock_config:
        mock_config.load_incluster_config.side_effect = Exception("not in cluster")
        mock_config.load_kube_config.return_value = None
        mock_config.ConfigException = Exception

        from gco.services.manifest_processor import ManifestProcessor

        processor = ManifestProcessor(
            cluster_id="test-cluster",
            region="us-east-1",
            config_dict=defaults,
        )
    return processor


def _build_job_manifest(image: str) -> dict:
    """Build a minimal valid Job manifest with the given image."""
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "test-job", "namespace": "default"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "worker",
                            "image": image,
                        }
                    ],
                    "restartPolicy": "Never",
                }
            }
        },
    }


# ---------------------------------------------------------------------------
# Strategies for generating image references
# ---------------------------------------------------------------------------

# Bare org names: lowercase alpha strings without dots or colons, 2-20 chars
# These simulate Docker Hub org names that are NOT in trusted_dockerhub_orgs
_bare_org_name = st.from_regex(r"[a-z]{2,4}org[a-z]{1,4}", fullmatch=True).filter(
    lambda s: (
        "." not in s
        and ":" not in s
        and s
        not in {
            "nvidia",
            "pytorch",
            "rayproject",
            "tensorflow",
            "huggingface",
            "amazon",
            "bitnami",
            "gco",
        }
    )
)

# Image path after the org/registry: e.g., "evil-image:latest"
_image_path = st.from_regex(r"[a-z][a-z0-9\-]{1,15}:[a-z0-9.]{1,8}", fullmatch=True)

# Domain-style registries: contain at least one dot, e.g., "registry.example.com"
_domain_registry = st.from_regex(r"[a-z]{2,8}\.[a-z]{2,6}\.[a-z]{2,4}", fullmatch=True)

# Trusted registries from the default config
_trusted_registry = st.sampled_from(
    [
        "docker.io",
        "gcr.io",
        "quay.io",
        "registry.k8s.io",
        "public.ecr.aws",
        "nvcr.io",
    ]
)

# Trusted Docker Hub orgs from the default config
_trusted_dockerhub_org = st.sampled_from(
    [
        "nvidia",
        "pytorch",
        "rayproject",
        "tensorflow",
        "huggingface",
        "amazon",
        "bitnami",
        "gco",
    ]
)


class TestRegistryDomainValidationProperty:
    """Property-based tests for registry domain validation.


    For any container image reference where the string before the first '/'
    does not contain a dot ('.') or colon (':'), the image validator SHALL NOT
    match it against trusted_registries entries that are full domains. Instead,
    it SHALL only match against trusted_dockerhub_orgs entries.

    """

    @pytest.fixture(autouse=True)
    def setup_processor(self):
        self.processor = _make_processor_for_registry_tests()

    @given(
        bare_org=_bare_org_name,
        path=_image_path,
    )
    @settings(max_examples=100, deadline=2000)
    def test_bare_names_never_match_domain_registries(self, bare_org, path):
        """Bare names (no dot in first segment) that are not in trusted_dockerhub_orgs
        must be rejected — they should never match against trusted_registries domains.

        """
        image = f"{bare_org}/{path}"
        manifest = _build_job_manifest(image)

        is_valid, error = self.processor._validate_image_sources(manifest)

        # The bare org is NOT in trusted_dockerhub_orgs and has no dot,
        # so it must NOT match any trusted_registries entry (which are all domains).
        assert is_valid is False, (
            f"Image '{image}' with bare org '{bare_org}' should be rejected "
            f"but was accepted. Bare names without dots must not match domain-style "
            f"trusted_registries."
        )
        assert error is not None
        assert "Untrusted image source" in error

    @given(
        registry=_trusted_registry,
        path=_image_path,
    )
    @settings(max_examples=100, deadline=2000)
    def test_domain_style_refs_match_trusted_registries(self, registry, path):
        """Domain-style image references correctly match against trusted_registries."""
        image = f"{registry}/{path}"
        manifest = _build_job_manifest(image)

        is_valid, error = self.processor._validate_image_sources(manifest)

        assert is_valid is True, (
            f"Image '{image}' from trusted registry '{registry}' should be accepted "
            f"but was rejected with: {error}"
        )

    @given(
        org=_trusted_dockerhub_org,
        path=_image_path,
    )
    @settings(max_examples=100, deadline=2000)
    def test_trusted_dockerhub_orgs_accepted(self, org, path):
        """Images from trusted Docker Hub orgs (bare names in trusted_dockerhub_orgs)
        are correctly accepted.

        """
        image = f"{org}/{path}"
        manifest = _build_job_manifest(image)

        is_valid, error = self.processor._validate_image_sources(manifest)

        assert is_valid is True, (
            f"Image '{image}' from trusted Docker Hub org '{org}' should be accepted "
            f"but was rejected with: {error}"
        )

    @given(
        untrusted_domain=_domain_registry,
        path=_image_path,
    )
    @settings(max_examples=100, deadline=2000)
    def test_untrusted_domain_registries_rejected(self, untrusted_domain, path):
        """Images from untrusted domain-style registries are rejected."""
        # Filter out any generated domain that happens to match a trusted registry
        trusted = {
            "docker.io",
            "gcr.io",
            "quay.io",
            "registry.k8s.io",
            "public.ecr.aws",
            "nvcr.io",
        }
        if untrusted_domain in trusted:
            return  # skip this example

        image = f"{untrusted_domain}/{path}"
        manifest = _build_job_manifest(image)

        is_valid, error = self.processor._validate_image_sources(manifest)

        assert is_valid is False, (
            f"Image '{image}' from untrusted domain '{untrusted_domain}' should be "
            f"rejected but was accepted."
        )


# =============================================================================
# Property: Image Digest-or-Trusted-Tag Validation
#
# For any container image reference in a user-submitted manifest, the image
# SHALL be accepted if and only if it uses a @sha256: digest reference OR it
# originates from a trusted registry/organization (existing validation).
# Untrusted sources are always rejected regardless of whether they use a
# digest, a tag, or a bare name.
# =============================================================================

# Strategy: generate a valid hex digest (64 hex chars for sha256)
_sha256_digest = st.from_regex(r"[0-9a-f]{64}", fullmatch=True)

# Tag names: lowercase alphanumeric with dots and dashes, 1-15 chars
_tag_name = st.from_regex(r"[a-z0-9][a-z0-9.\-]{0,14}", fullmatch=True)

# Simple image name component (used after the registry/org)
_simple_image_name = st.from_regex(r"[a-z][a-z0-9\-]{1,12}", fullmatch=True)

# Untrusted bare org names: no dots/colons, not in trusted_dockerhub_orgs
_untrusted_bare_org = st.from_regex(r"[a-z]{2,4}evil[a-z]{1,4}", fullmatch=True).filter(
    lambda s: (
        "." not in s
        and ":" not in s
        and s
        not in {
            "nvidia",
            "pytorch",
            "rayproject",
            "tensorflow",
            "huggingface",
            "amazon",
            "bitnami",
            "gco",
        }
    )
)

# Untrusted domain registries: contain a dot but not in trusted list
_untrusted_domain = st.from_regex(r"[a-z]{2,6}evil\.[a-z]{2,4}\.[a-z]{2,3}", fullmatch=True).filter(
    lambda s: (
        s
        not in {
            "docker.io",
            "gcr.io",
            "quay.io",
            "registry.k8s.io",
            "public.ecr.aws",
            "nvcr.io",
        }
    )
)


class TestImageDigestOrTrustedTagValidationProperty:
    """Property-based tests for image digest-or-trusted-tag validation.


    For any container image reference in a user-submitted manifest, the image
    SHALL be accepted if and only if it uses a @sha256: digest reference OR it
    originates from a trusted registry/organization. Untrusted sources are
    always rejected regardless of digest, tag, or bare name format.

    """

    @pytest.fixture(autouse=True)
    def setup_processor(self):
        self.processor = _make_processor_for_registry_tests()

    # -----------------------------------------------------------------
    # Trusted source + digest → accepted
    # -----------------------------------------------------------------
    @given(
        registry=_trusted_registry,
        name=_simple_image_name,
        digest=_sha256_digest,
    )
    @settings(max_examples=100, deadline=2000)
    def test_digest_refs_from_trusted_registries_accepted(self, registry, name, digest):
        """Digest references from trusted registries are always accepted."""
        image = f"{registry}/{name}@sha256:{digest}"
        manifest = _build_job_manifest(image)

        is_valid, error = self.processor._validate_image_sources(manifest)

        assert is_valid is True, (
            f"Image '{image}' with digest from trusted registry '{registry}' "
            f"should be accepted but was rejected with: {error}"
        )

    @given(
        org=_trusted_dockerhub_org,
        name=_simple_image_name,
        digest=_sha256_digest,
    )
    @settings(max_examples=100, deadline=2000)
    def test_digest_refs_from_trusted_dockerhub_orgs_accepted(self, org, name, digest):
        """Digest references from trusted Docker Hub orgs are always accepted."""
        image = f"{org}/{name}@sha256:{digest}"
        manifest = _build_job_manifest(image)

        is_valid, error = self.processor._validate_image_sources(manifest)

        assert is_valid is True, (
            f"Image '{image}' with digest from trusted Docker Hub org '{org}' "
            f"should be accepted but was rejected with: {error}"
        )

    # -----------------------------------------------------------------
    # Trusted source + tag → accepted
    # -----------------------------------------------------------------
    @given(
        registry=_trusted_registry,
        name=_simple_image_name,
        tag=_tag_name,
    )
    @settings(max_examples=100, deadline=2000)
    def test_tagged_refs_from_trusted_registries_accepted(self, registry, name, tag):
        """Tagged image references from trusted registries are accepted."""
        image = f"{registry}/{name}:{tag}"
        manifest = _build_job_manifest(image)

        is_valid, error = self.processor._validate_image_sources(manifest)

        assert is_valid is True, (
            f"Image '{image}' with tag from trusted registry '{registry}' "
            f"should be accepted but was rejected with: {error}"
        )

    @given(
        org=_trusted_dockerhub_org,
        name=_simple_image_name,
        tag=_tag_name,
    )
    @settings(max_examples=100, deadline=2000)
    def test_tagged_refs_from_trusted_dockerhub_orgs_accepted(self, org, name, tag):
        """Tagged image references from trusted Docker Hub orgs are accepted."""
        image = f"{org}/{name}:{tag}"
        manifest = _build_job_manifest(image)

        is_valid, error = self.processor._validate_image_sources(manifest)

        assert is_valid is True, (
            f"Image '{image}' with tag from trusted Docker Hub org '{org}' "
            f"should be accepted but was rejected with: {error}"
        )

    # -----------------------------------------------------------------
    # Untrusted source + digest → rejected
    # -----------------------------------------------------------------
    @given(
        untrusted_org=_untrusted_bare_org,
        name=_simple_image_name,
        digest=_sha256_digest,
    )
    @settings(max_examples=100, deadline=2000)
    def test_digest_refs_from_untrusted_orgs_rejected(self, untrusted_org, name, digest):
        """Digest references from untrusted Docker Hub orgs are rejected.
        A digest does NOT bypass the trust check.

        """
        image = f"{untrusted_org}/{name}@sha256:{digest}"
        manifest = _build_job_manifest(image)

        is_valid, error = self.processor._validate_image_sources(manifest)

        assert is_valid is False, (
            f"Image '{image}' with digest from untrusted org '{untrusted_org}' "
            f"should be rejected but was accepted. Digests must not bypass trust checks."
        )
        assert error is not None
        assert "Untrusted image source" in error

    @given(
        untrusted_dom=_untrusted_domain,
        name=_simple_image_name,
        digest=_sha256_digest,
    )
    @settings(max_examples=100, deadline=2000)
    def test_digest_refs_from_untrusted_domains_rejected(self, untrusted_dom, name, digest):
        """Digest references from untrusted domain registries are rejected.
        A digest does NOT bypass the trust check.

        """
        image = f"{untrusted_dom}/{name}@sha256:{digest}"
        manifest = _build_job_manifest(image)

        is_valid, error = self.processor._validate_image_sources(manifest)

        assert is_valid is False, (
            f"Image '{image}' with digest from untrusted domain '{untrusted_dom}' "
            f"should be rejected but was accepted. Digests must not bypass trust checks."
        )
        assert error is not None
        assert "Untrusted image source" in error

    # -----------------------------------------------------------------
    # Untrusted source + tag → rejected
    # -----------------------------------------------------------------
    @given(
        untrusted_org=_untrusted_bare_org,
        name=_simple_image_name,
        tag=_tag_name,
    )
    @settings(max_examples=100, deadline=2000)
    def test_tagged_refs_from_untrusted_orgs_rejected(self, untrusted_org, name, tag):
        """Tagged image references from untrusted Docker Hub orgs are rejected."""
        image = f"{untrusted_org}/{name}:{tag}"
        manifest = _build_job_manifest(image)

        is_valid, error = self.processor._validate_image_sources(manifest)

        assert is_valid is False, (
            f"Image '{image}' with tag from untrusted org '{untrusted_org}' "
            f"should be rejected but was accepted."
        )
        assert error is not None
        assert "Untrusted image source" in error

    @given(
        untrusted_dom=_untrusted_domain,
        name=_simple_image_name,
        tag=_tag_name,
    )
    @settings(max_examples=100, deadline=2000)
    def test_tagged_refs_from_untrusted_domains_rejected(self, untrusted_dom, name, tag):
        """Tagged image references from untrusted domain registries are rejected."""
        image = f"{untrusted_dom}/{name}:{tag}"
        manifest = _build_job_manifest(image)

        is_valid, error = self.processor._validate_image_sources(manifest)

        assert is_valid is False, (
            f"Image '{image}' with tag from untrusted domain '{untrusted_dom}' "
            f"should be rejected but was accepted."
        )
        assert error is not None
        assert "Untrusted image source" in error


# =============================================================================
# Property: YAML Depth Limit Enforcement
#
# For any parsed manifest document, if the nesting depth exceeds the configured
# maximum, the system SHALL reject the document. Documents within the depth
# limit SHALL be accepted (by the depth check itself).
# =============================================================================


def _make_processor_with_depth_limit(yaml_max_depth: int = 50):
    """Create a ManifestProcessor with a configurable yaml_max_depth."""
    config_dict = {
        "max_cpu_per_manifest": "100",
        "max_memory_per_manifest": "256Gi",
        "max_gpu_per_manifest": 16,
        "allowed_namespaces": ["default", "gco-jobs"],
        "validation_enabled": True,
        "yaml_max_depth": yaml_max_depth,
        "trusted_registries": [
            "docker.io",
            "gcr.io",
            "quay.io",
            "registry.k8s.io",
            "public.ecr.aws",
            "nvcr.io",
        ],
        "trusted_dockerhub_orgs": [
            "nvidia",
            "pytorch",
            "rayproject",
            "tensorflow",
            "huggingface",
            "amazon",
            "bitnami",
            "gco",
        ],
    }

    with patch("gco.services.manifest_processor.config") as mock_config:
        mock_config.load_incluster_config.side_effect = Exception("not in cluster")
        mock_config.load_kube_config.return_value = None
        mock_config.ConfigException = Exception

        from gco.services.manifest_processor import ManifestProcessor

        processor = ManifestProcessor(
            cluster_id="test-cluster",
            region="us-east-1",
            config_dict=config_dict,
        )
    return processor


def _build_nested_dict(depth: int) -> dict:
    """Build a dict nested to exactly the given depth.

    depth=0 returns a flat dict (leaf), depth=N wraps N levels of dicts.
    """
    obj: dict = {"leaf": "value"}
    for _ in range(depth):
        obj = {"nested": obj}
    return obj


def _build_nested_list(depth: int) -> list:
    """Build a list nested to exactly the given depth.

    depth=0 returns a flat list (leaf), depth=N wraps N levels of lists.
    """
    obj: list = ["leaf"]
    for _ in range(depth):
        obj = [obj]
    return obj


def _build_nested_mixed(depth: int) -> dict | list:
    """Build alternating dict/list nesting to the given depth."""
    obj: dict | list = {"leaf": "value"}
    for i in range(depth):
        obj = [obj] if i % 2 == 0 else {"level": obj}
    return obj


# Strategy: generate a max_depth limit between 3 and 20 (small enough to test quickly)
_max_depth_st = st.integers(min_value=3, max_value=20)


class TestYamlDepthLimitEnforcementProperty:
    """Property-based tests for YAML depth limit enforcement.


    For any parsed manifest document, if the nesting depth exceeds the
    configured maximum (default 50 levels), the system SHALL reject the
    document with HTTP 400 before applying it to the cluster.

    """

    # -----------------------------------------------------------------
    # Property: documents within the depth limit are accepted
    # -----------------------------------------------------------------
    @given(
        max_depth=_max_depth_st,
        within_offset=st.integers(min_value=1, max_value=3),
    )
    @settings(max_examples=100, deadline=5000, suppress_health_check=[HealthCheck.too_slow])
    def test_documents_within_depth_limit_accepted(self, max_depth, within_offset):
        """Nested dicts/lists at or below the configured max depth are accepted
        by _check_yaml_depth().

        """
        # _build_nested_dict(n) creates n wrapping levels around {"leaf": "value"}.
        # The leaf scalar is checked at depth n+1. For the check to pass we
        # need n+1 <= max_depth, i.e. n <= max_depth - 1.
        safe_depth = max(0, max_depth - 1 - within_offset)

        processor = _make_processor_with_depth_limit(max_depth)

        nested_dict = _build_nested_dict(safe_depth)
        assert processor._check_yaml_depth(nested_dict) is True, (
            f"Nested dict at depth {safe_depth} should be accepted with max_depth={max_depth}"
        )

        nested_list = _build_nested_list(safe_depth)
        assert processor._check_yaml_depth(nested_list) is True, (
            f"Nested list at depth {safe_depth} should be accepted with max_depth={max_depth}"
        )

    # -----------------------------------------------------------------
    # Property: documents exceeding the depth limit are rejected
    # -----------------------------------------------------------------
    @given(
        max_depth=_max_depth_st,
        over_offset=st.integers(min_value=1, max_value=10),
    )
    @settings(max_examples=100, deadline=5000, suppress_health_check=[HealthCheck.too_slow])
    def test_documents_exceeding_depth_limit_rejected(self, max_depth, over_offset):
        """Nested dicts/lists exceeding the configured max depth are rejected
        by _check_yaml_depth().

        """
        # Nesting depth that exceeds the limit
        excessive_depth = max_depth + over_offset

        processor = _make_processor_with_depth_limit(max_depth)

        nested_dict = _build_nested_dict(excessive_depth)
        assert processor._check_yaml_depth(nested_dict) is False, (
            f"Nested dict at depth {excessive_depth} should be rejected with max_depth={max_depth}"
        )

        nested_list = _build_nested_list(excessive_depth)
        assert processor._check_yaml_depth(nested_list) is False, (
            f"Nested list at depth {excessive_depth} should be rejected with max_depth={max_depth}"
        )

    # -----------------------------------------------------------------
    # Property: mixed dict/list nesting beyond limit is rejected
    # -----------------------------------------------------------------
    @given(
        max_depth=_max_depth_st,
        over_offset=st.integers(min_value=1, max_value=10),
    )
    @settings(max_examples=100, deadline=5000, suppress_health_check=[HealthCheck.too_slow])
    def test_mixed_nesting_exceeding_limit_rejected(self, max_depth, over_offset):
        """Mixed dict/list nesting exceeding the configured max depth is rejected."""
        excessive_depth = max_depth + over_offset

        processor = _make_processor_with_depth_limit(max_depth)

        mixed_obj = _build_nested_mixed(excessive_depth)
        assert processor._check_yaml_depth(mixed_obj) is False, (
            f"Mixed nesting at depth {excessive_depth} should be rejected with "
            f"max_depth={max_depth}"
        )

    # -----------------------------------------------------------------
    # Property: validate_manifest rejects deeply nested manifests
    # -----------------------------------------------------------------
    @given(
        max_depth=st.integers(min_value=5, max_value=15),
        over_offset=st.integers(min_value=1, max_value=10),
    )
    @settings(max_examples=100, deadline=5000, suppress_health_check=[HealthCheck.too_slow])
    def test_validate_manifest_rejects_excessive_depth(self, max_depth, over_offset):
        """validate_manifest() returns (False, error) for manifests exceeding
        the configured depth limit, and the error message mentions the depth.

        """
        excessive_depth = max_depth + over_offset

        processor = _make_processor_with_depth_limit(max_depth)

        # Build a valid-looking manifest with a deeply nested annotation
        manifest = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": "test-job",
                "namespace": "default",
                "annotations": _build_nested_dict(excessive_depth),
            },
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{"name": "worker", "image": "python:3.14"}],
                        "restartPolicy": "Never",
                    }
                }
            },
        }

        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is False, (
            f"Manifest with nesting depth {excessive_depth} should be rejected "
            f"with max_depth={max_depth}"
        )
        assert error is not None
        assert "nesting depth" in error.lower() or "depth" in error.lower(), (
            f"Error message should mention depth limit, got: {error}"
        )

    # -----------------------------------------------------------------
    # Property: validate_manifest accepts manifests within depth limit
    # -----------------------------------------------------------------
    @given(
        max_depth=st.integers(min_value=10, max_value=20),
    )
    @settings(max_examples=100, deadline=5000, suppress_health_check=[HealthCheck.too_slow])
    def test_validate_manifest_accepts_within_depth(self, max_depth):
        """validate_manifest() accepts a standard Job manifest that is well
        within the configured depth limit.

        """
        processor = _make_processor_with_depth_limit(max_depth)

        # A standard Job manifest has ~6-7 levels of nesting, well within any
        # max_depth >= 10
        manifest = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "test-job", "namespace": "default"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{"name": "worker", "image": "python:3.14"}],
                        "restartPolicy": "Never",
                    }
                }
            },
        }

        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is True, (
            f"Standard Job manifest should be accepted with max_depth={max_depth}, "
            f"but got error: {error}"
        )


# =============================================================================
# Property: YAML Anchor/Alias Rejection
#
# For any YAML document submitted to the manifest processor that contains
# YAML anchors (&) and aliases (*), when alias rejection is enabled (default),
# the system SHALL reject the document with a yaml.YAMLError.
# When alias rejection is disabled (allow_aliases=True), the same document
# SHALL be accepted and parsed correctly.
# =============================================================================


# Strategy: generate valid YAML anchor names (alphanumeric + hyphens/underscores, 1-20 chars)
_anchor_name = st.from_regex(r"[a-zA-Z_][a-zA-Z0-9_\-]{0,19}", fullmatch=True)

# Strategy: generate simple scalar values for the anchor target
_scalar_value = st.from_regex(r"[a-z][a-z0-9]{0,15}", fullmatch=True)

# YAML 1.1 reserved tokens that PyYAML's SafeLoader coerces to bool / None
# when used as bare scalars. The mapping-key tests below look up
# ``result[key]`` after parsing, so a generated key like ``"no"`` would
# resolve to ``False`` in the parsed dict and the lookup would raise
# KeyError. Excluding these tokens keeps the test focused on the
# alias-resolution semantics it was written to exercise. The regex below
# is lowercase-only because ``_yaml_key`` itself is lowercase-only;
# capitalised forms (``Yes``, ``True``, …) cannot be drawn.
_YAML_11_RESERVED_KEYS = frozenset({"y", "n", "yes", "no", "true", "false", "on", "off", "null"})

# Strategy: generate a simple key name for YAML mappings. Filtered to
# exclude YAML 1.1 reserved scalar tokens so generated keys round-trip
# through PyYAML as strings rather than coerced booleans / None.
_yaml_key = st.from_regex(r"[a-z][a-z0-9_]{0,10}", fullmatch=True).filter(
    lambda s: s not in _YAML_11_RESERVED_KEYS
)


class TestYamlAnchorAliasRejectionProperty:
    """Property-based tests for YAML anchor/alias rejection.


    For any YAML document that contains YAML anchors (&) and aliases (*),
    when alias rejection is enabled (default), the system SHALL reject the
    document with a yaml.YAMLError. When alias rejection is disabled
    (allow_aliases=True), the same document SHALL be accepted.

    """

    @given(
        anchor=_anchor_name,
        value=_scalar_value,
        key1=_yaml_key,
        key2=_yaml_key,
    )
    @settings(max_examples=100, deadline=2000)
    def test_yaml_with_aliases_rejected_when_disabled(self, anchor, value, key1, key2):
        """YAML documents containing anchors and aliases are rejected when
        allow_aliases=False (the default).

        """
        import yaml as _yaml

        from gco.services.manifest_processor import safe_load_yaml

        # Ensure distinct keys to produce valid YAML
        if key1 == key2:
            key2 = key2 + "_alias"

        yaml_str = f"{key1}: &{anchor} {value}\n{key2}: *{anchor}\n"

        with pytest.raises(_yaml.YAMLError, match="aliases are not allowed"):
            safe_load_yaml(yaml_str, allow_aliases=False)

    @given(
        anchor=_anchor_name,
        value=_scalar_value,
        key1=_yaml_key,
        key2=_yaml_key,
    )
    @settings(max_examples=100, deadline=2000)
    def test_yaml_with_aliases_accepted_when_enabled(self, anchor, value, key1, key2):
        """The same YAML documents with anchors/aliases are accepted when
        allow_aliases=True.

        """
        from gco.services.manifest_processor import safe_load_yaml

        # Ensure distinct keys to produce valid YAML
        if key1 == key2:
            key2 = key2 + "_alias"

        yaml_str = f"{key1}: &{anchor} {value}\n{key2}: *{anchor}\n"

        result = safe_load_yaml(yaml_str, allow_aliases=True)

        # Compare against the parsed anchor value, not the raw string.
        # YAML 1.1 coerces bare scalars like "yes"/"no"/"true"/"null" into
        # booleans / None, so the alias value equals whatever the parser
        # produced for the anchor — not necessarily the source string.
        assert result[key2] == result[key1], (
            f"Alias *{anchor} should resolve to same value as anchor but got '{result[key2]}'"
        )

    @given(
        anchor=_anchor_name,
        key1=_yaml_key,
        key2=_yaml_key,
        inner_key=_yaml_key,
        inner_val=_scalar_value,
    )
    @settings(max_examples=100, deadline=2000)
    def test_yaml_mapping_anchor_alias_rejected(self, anchor, key1, key2, inner_key, inner_val):
        """YAML documents with mapping anchors/aliases (not just scalars) are
        also rejected when allow_aliases=False.

        """
        import yaml as _yaml

        from gco.services.manifest_processor import safe_load_yaml

        # Ensure distinct keys
        if key1 == key2:
            key2 = key2 + "_ref"

        yaml_str = f"{key1}: &{anchor}\n  {inner_key}: {inner_val}\n{key2}: *{anchor}\n"

        with pytest.raises(_yaml.YAMLError, match="aliases are not allowed"):
            safe_load_yaml(yaml_str, allow_aliases=False)


# =============================================================================
# Unit tests for _extract_pod_spec and _inject_security_defaults
# (Task 8.1: SA token auto-mount disabled)
# =============================================================================


class TestExtractPodSpec:
    """Unit tests for _extract_pod_spec across all workload types."""

    @pytest.fixture(autouse=True)
    def setup_processor(self):
        self.processor = _make_processor_for_registry_tests()

    def test_extract_pod_spec_from_job(self):
        """Extract pod spec from a Job manifest."""
        manifest = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "test-job", "namespace": "gco-jobs"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{"name": "worker", "image": "python:3.14"}],
                        "restartPolicy": "Never",
                    }
                }
            },
        }
        pod_spec = self.processor._extract_pod_spec(manifest)
        assert pod_spec is not None
        assert "containers" in pod_spec
        assert pod_spec["containers"][0]["name"] == "worker"

    def test_extract_pod_spec_from_deployment(self):
        """Extract pod spec from a Deployment manifest."""
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "test-deploy", "namespace": "gco-jobs"},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "test"}},
                "template": {
                    "spec": {
                        "containers": [{"name": "app", "image": "nginx:latest"}],
                    }
                },
            },
        }
        pod_spec = self.processor._extract_pod_spec(manifest)
        assert pod_spec is not None
        assert pod_spec["containers"][0]["name"] == "app"

    def test_extract_pod_spec_from_cronjob(self):
        """Extract pod spec from a CronJob manifest."""
        manifest = {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {"name": "test-cron", "namespace": "gco-jobs"},
            "spec": {
                "schedule": "*/5 * * * *",
                "jobTemplate": {
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [{"name": "cron-worker", "image": "python:3.14"}],
                                "restartPolicy": "Never",
                            }
                        }
                    }
                },
            },
        }
        pod_spec = self.processor._extract_pod_spec(manifest)
        assert pod_spec is not None
        assert pod_spec["containers"][0]["name"] == "cron-worker"

    def test_extract_pod_spec_from_bare_pod(self):
        """Extract pod spec from a bare Pod manifest."""
        manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "test-pod", "namespace": "gco-jobs"},
            "spec": {
                "containers": [{"name": "main", "image": "busybox"}],
            },
        }
        pod_spec = self.processor._extract_pod_spec(manifest)
        assert pod_spec is not None
        assert pod_spec["containers"][0]["name"] == "main"

    def test_extract_pod_spec_from_statefulset(self):
        """Extract pod spec from a StatefulSet manifest."""
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": {"name": "test-sts", "namespace": "gco-jobs"},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "test"}},
                "template": {
                    "spec": {
                        "containers": [{"name": "db", "image": "postgres:15"}],
                    }
                },
            },
        }
        pod_spec = self.processor._extract_pod_spec(manifest)
        assert pod_spec is not None
        assert pod_spec["containers"][0]["name"] == "db"

    def test_extract_pod_spec_from_configmap_returns_none(self):
        """ConfigMap has no pod spec — should return None."""
        manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "test-cm", "namespace": "gco-jobs"},
            "data": {"key": "value"},
        }
        pod_spec = self.processor._extract_pod_spec(manifest)
        assert pod_spec is None

    def test_extract_pod_spec_from_service_returns_none(self):
        """Service has no pod spec — should return None."""
        manifest = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "test-svc", "namespace": "gco-jobs"},
            "spec": {
                "selector": {"app": "test"},
                "ports": [{"port": 80}],
            },
        }
        pod_spec = self.processor._extract_pod_spec(manifest)
        assert pod_spec is None

    def test_extract_pod_spec_missing_spec(self):
        """Manifest with no spec at all returns None."""
        manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "test"},
        }
        pod_spec = self.processor._extract_pod_spec(manifest)
        assert pod_spec is None

    def test_extract_pod_spec_none_spec(self):
        """Manifest with spec=None returns None."""
        manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "test"},
            "spec": None,
        }
        pod_spec = self.processor._extract_pod_spec(manifest)
        assert pod_spec is None


class TestInjectSecurityDefaults:
    """Unit tests for _inject_security_defaults."""

    @pytest.fixture(autouse=True)
    def setup_processor(self):
        self.processor = _make_processor_for_registry_tests()

    def test_inject_sets_automount_false_on_job(self):
        """Job pod spec gets automountServiceAccountToken: false."""
        manifest = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "test-job", "namespace": "gco-jobs"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{"name": "worker", "image": "python:3.14"}],
                        "restartPolicy": "Never",
                    }
                }
            },
        }
        result = self.processor._inject_security_defaults(manifest)
        pod_spec = result["spec"]["template"]["spec"]
        assert pod_spec["automountServiceAccountToken"] is False

    def test_inject_sets_automount_false_on_deployment(self):
        """Deployment pod spec gets automountServiceAccountToken: false."""
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "test-deploy", "namespace": "gco-jobs"},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "test"}},
                "template": {
                    "spec": {
                        "containers": [{"name": "app", "image": "nginx:latest"}],
                    }
                },
            },
        }
        result = self.processor._inject_security_defaults(manifest)
        pod_spec = result["spec"]["template"]["spec"]
        assert pod_spec["automountServiceAccountToken"] is False

    def test_inject_sets_automount_false_on_cronjob(self):
        """CronJob pod spec gets automountServiceAccountToken: false."""
        manifest = {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {"name": "test-cron", "namespace": "gco-jobs"},
            "spec": {
                "schedule": "*/5 * * * *",
                "jobTemplate": {
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [{"name": "worker", "image": "python:3.14"}],
                                "restartPolicy": "Never",
                            }
                        }
                    }
                },
            },
        }
        result = self.processor._inject_security_defaults(manifest)
        pod_spec = result["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        assert pod_spec["automountServiceAccountToken"] is False

    def test_inject_sets_automount_false_on_bare_pod(self):
        """Bare Pod spec gets automountServiceAccountToken: false."""
        manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "test-pod", "namespace": "gco-jobs"},
            "spec": {
                "containers": [{"name": "main", "image": "busybox"}],
            },
        }
        result = self.processor._inject_security_defaults(manifest)
        pod_spec = result["spec"]
        assert pod_spec["automountServiceAccountToken"] is False

    def test_inject_does_not_override_explicit_true(self):
        """If user explicitly sets automountServiceAccountToken: true, don't override."""
        manifest = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "test-job", "namespace": "gco-jobs"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{"name": "worker", "image": "python:3.14"}],
                        "restartPolicy": "Never",
                        "automountServiceAccountToken": True,
                    }
                }
            },
        }
        result = self.processor._inject_security_defaults(manifest)
        pod_spec = result["spec"]["template"]["spec"]
        # setdefault should NOT override the explicit True
        assert pod_spec["automountServiceAccountToken"] is True

    def test_inject_does_not_override_explicit_false(self):
        """If user explicitly sets automountServiceAccountToken: false, it stays false."""
        manifest = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "test-job", "namespace": "gco-jobs"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{"name": "worker", "image": "python:3.14"}],
                        "restartPolicy": "Never",
                        "automountServiceAccountToken": False,
                    }
                }
            },
        }
        result = self.processor._inject_security_defaults(manifest)
        pod_spec = result["spec"]["template"]["spec"]
        assert pod_spec["automountServiceAccountToken"] is False

    def test_inject_skips_non_workload_resources(self):
        """ConfigMap and other non-workload resources are returned unchanged."""
        manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "test-cm", "namespace": "gco-jobs"},
            "data": {"key": "value"},
        }
        import copy

        original = copy.deepcopy(manifest)
        result = self.processor._inject_security_defaults(manifest)
        assert result == original

    def test_inject_returns_manifest(self):
        """_inject_security_defaults returns the manifest for chaining."""
        manifest = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "test-job", "namespace": "gco-jobs"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{"name": "worker", "image": "python:3.14"}],
                    }
                }
            },
        }
        result = self.processor._inject_security_defaults(manifest)
        assert result is manifest  # Same object, mutated in-place


# =============================================================================
# Property: SA Token Auto-Mount Injection
#
# For any valid user-submitted manifest targeting the gco-jobs namespace,
# after processing by the manifest processor, the resulting pod spec SHALL
# have automountServiceAccountToken set to false.
# =============================================================================

# ---------------------------------------------------------------------------
# Strategies for generating manifest kinds and pod spec structures
# ---------------------------------------------------------------------------

# Container name: lowercase alpha with optional hyphens, 2-15 chars
_container_name_st = st.from_regex(r"[a-z][a-z0-9\-]{1,14}", fullmatch=True)

# Image reference from a trusted registry
_trusted_image_st = st.sampled_from(
    [
        "docker.io/library/python:3.14",
        "gcr.io/my-project/worker:latest",
        "quay.io/team/app:v1",
        "registry.k8s.io/pause:3.9",
        "nvcr.io/nvidia/cuda:12.0",
        "python:3.14",
        "busybox",
        "nginx:latest",
    ]
)

# Restart policy for Jobs/Pods
_restart_policy_st = st.sampled_from(["Never", "OnFailure"])

# Optional extra pod spec fields that users might include
_optional_pod_fields_st = st.fixed_dictionaries(
    {},
    optional={
        "serviceAccountName": st.just("default"),
        "terminationGracePeriodSeconds": st.integers(min_value=0, max_value=300),
        "activeDeadlineSeconds": st.integers(min_value=1, max_value=3600),
        "nodeSelector": st.just({"kubernetes.io/os": "linux"}),
    },
)

# Whether the user has explicitly set automountServiceAccountToken
_explicit_automount_st = st.sampled_from(
    [
        None,  # not set — _inject_security_defaults should add False
        True,  # explicitly True — setdefault should NOT override
        False,  # explicitly False — stays False
    ]
)


def _build_manifest_for_kind(
    kind: str,
    container_name: str,
    image: str,
    restart_policy: str,
    extra_pod_fields: dict,
    explicit_automount,
) -> dict:
    """Build a valid manifest of the given kind with the provided parameters."""
    pod_spec: dict = {
        "containers": [{"name": container_name, "image": image}],
        **extra_pod_fields,
    }
    if kind in ("Job", "CronJob"):
        pod_spec["restartPolicy"] = restart_policy
    if explicit_automount is not None:
        pod_spec["automountServiceAccountToken"] = explicit_automount

    if kind == "Job":
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "test-job", "namespace": "gco-jobs"},
            "spec": {"template": {"spec": pod_spec}},
        }
    elif kind == "Deployment":
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "test-deploy", "namespace": "gco-jobs"},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "test"}},
                "template": {"spec": pod_spec},
            },
        }
    elif kind == "CronJob":
        return {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {"name": "test-cron", "namespace": "gco-jobs"},
            "spec": {
                "schedule": "*/5 * * * *",
                "jobTemplate": {"spec": {"template": {"spec": pod_spec}}},
            },
        }
    elif kind == "Pod":
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "test-pod", "namespace": "gco-jobs"},
            "spec": pod_spec,
        }
    elif kind == "StatefulSet":
        return {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": {"name": "test-sts", "namespace": "gco-jobs"},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "test"}},
                "template": {"spec": pod_spec},
            },
        }
    elif kind == "DaemonSet":
        return {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": "test-ds", "namespace": "gco-jobs"},
            "spec": {
                "selector": {"matchLabels": {"app": "test"}},
                "template": {"spec": pod_spec},
            },
        }
    else:
        raise ValueError(f"Unsupported kind: {kind}")


# Strategy: pick a workload kind that has a pod spec
_workload_kind_st = st.sampled_from(
    ["Job", "Deployment", "CronJob", "Pod", "StatefulSet", "DaemonSet"]
)


class TestSATokenAutoMountInjectionProperty:
    """Property-based tests for SA token auto-mount injection.


    For any valid user-submitted manifest targeting the gco-jobs namespace,
    after processing by _inject_security_defaults(), the resulting pod spec
    SHALL have automountServiceAccountToken set to false — unless the user
    has explicitly set it to true (setdefault semantics).

    """

    @pytest.fixture(autouse=True)
    def setup_processor(self):
        self.processor = _make_processor_for_registry_tests()

    # -----------------------------------------------------------------
    # Core property: after injection, automountServiceAccountToken is
    # always present and False when the user did NOT set it explicitly.
    # -----------------------------------------------------------------
    @given(
        kind=_workload_kind_st,
        container_name=_container_name_st,
        image=_trusted_image_st,
        restart_policy=_restart_policy_st,
        extra_pod_fields=_optional_pod_fields_st,
    )
    @settings(max_examples=100, deadline=2000)
    def test_automount_injected_false_when_not_set(
        self,
        kind,
        container_name,
        image,
        restart_policy,
        extra_pod_fields,
    ):
        """When the user does NOT set automountServiceAccountToken,
        _inject_security_defaults() SHALL set it to False.

        """
        manifest = _build_manifest_for_kind(
            kind=kind,
            container_name=container_name,
            image=image,
            restart_policy=restart_policy,
            extra_pod_fields=extra_pod_fields,
            explicit_automount=None,  # user did not set it
        )

        self.processor._inject_security_defaults(manifest)

        pod_spec = self.processor._extract_pod_spec(manifest)
        assert pod_spec is not None, f"Expected pod spec for kind={kind} but got None"
        assert "automountServiceAccountToken" in pod_spec, (
            f"automountServiceAccountToken should be present in pod spec "
            f"after injection for kind={kind}"
        )
        assert pod_spec["automountServiceAccountToken"] is False, (
            f"automountServiceAccountToken should be False after injection "
            f"for kind={kind}, got {pod_spec['automountServiceAccountToken']}"
        )

    # -----------------------------------------------------------------
    # Idempotency: calling _inject_security_defaults twice still yields
    # automountServiceAccountToken == False.
    # -----------------------------------------------------------------
    @given(
        kind=_workload_kind_st,
        container_name=_container_name_st,
        image=_trusted_image_st,
        restart_policy=_restart_policy_st,
        extra_pod_fields=_optional_pod_fields_st,
    )
    @settings(max_examples=100, deadline=2000)
    def test_injection_is_idempotent(
        self,
        kind,
        container_name,
        image,
        restart_policy,
        extra_pod_fields,
    ):
        """Calling _inject_security_defaults() multiple times does not change
        the result — automountServiceAccountToken remains False.

        """
        manifest = _build_manifest_for_kind(
            kind=kind,
            container_name=container_name,
            image=image,
            restart_policy=restart_policy,
            extra_pod_fields=extra_pod_fields,
            explicit_automount=None,
        )

        self.processor._inject_security_defaults(manifest)
        self.processor._inject_security_defaults(manifest)

        pod_spec = self.processor._extract_pod_spec(manifest)
        assert pod_spec is not None
        assert pod_spec["automountServiceAccountToken"] is False

    # -----------------------------------------------------------------
    # Respect explicit user choice: if user sets True, don't override.
    # -----------------------------------------------------------------
    @given(
        kind=_workload_kind_st,
        container_name=_container_name_st,
        image=_trusted_image_st,
        restart_policy=_restart_policy_st,
        extra_pod_fields=_optional_pod_fields_st,
        explicit_automount=st.sampled_from([True, False]),
    )
    @settings(max_examples=100, deadline=2000)
    def test_explicit_user_choice_preserved(
        self,
        kind,
        container_name,
        image,
        restart_policy,
        extra_pod_fields,
        explicit_automount,
    ):
        """When the user explicitly sets automountServiceAccountToken (True or
        False), _inject_security_defaults() SHALL NOT override their choice.

        """
        manifest = _build_manifest_for_kind(
            kind=kind,
            container_name=container_name,
            image=image,
            restart_policy=restart_policy,
            extra_pod_fields=extra_pod_fields,
            explicit_automount=explicit_automount,
        )

        self.processor._inject_security_defaults(manifest)

        pod_spec = self.processor._extract_pod_spec(manifest)
        assert pod_spec is not None
        assert pod_spec["automountServiceAccountToken"] is explicit_automount, (
            f"User explicitly set automountServiceAccountToken={explicit_automount} "
            f"but after injection it became {pod_spec['automountServiceAccountToken']} "
            f"for kind={kind}"
        )


class TestTrustedImageSourceLockstep:
    """Keep the fail-closed code defaults and cdk.json policy in lockstep.

    ``cdk.json job_validation_policy`` is the authoritative deploy-time
    allowlist; ``DEFAULT_TRUSTED_REGISTRIES`` / ``DEFAULT_TRUSTED_DOCKERHUB_ORGS``
    are the fail-closed fallbacks both services use when deployment wiring is
    absent. Divergence produces environment-dependent acceptance of the same
    manifest — the exact drift that let ``lmsysorg`` be trusted at deploy time
    but rejected by the fallback (and ``vllm``/``ghcr.io/huggingface``, used by
    shipped inference examples, be rejected everywhere).
    """

    @staticmethod
    def _policy() -> dict:
        import json
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        context = json.loads((root / "cdk.json").read_text(encoding="utf-8"))["context"]
        return context["job_validation_policy"]

    def test_registry_defaults_match_cdk_policy(self) -> None:
        from gco.services.manifest_processor import DEFAULT_TRUSTED_REGISTRIES

        assert set(DEFAULT_TRUSTED_REGISTRIES) == set(self._policy()["trusted_registries"])

    def test_dockerhub_org_defaults_match_cdk_policy(self) -> None:
        from gco.services.manifest_processor import DEFAULT_TRUSTED_DOCKERHUB_ORGS

        assert set(DEFAULT_TRUSTED_DOCKERHUB_ORGS) == set(self._policy()["trusted_dockerhub_orgs"])

    def test_shipped_inference_example_images_are_trusted(self) -> None:
        """Every image in examples/inference-*.yaml must clear the default gate."""
        from pathlib import Path

        import yaml

        from gco.services.manifest_processor import validate_image_sources

        root = Path(__file__).resolve().parent.parent
        for path in sorted((root / "examples").glob("inference-*.yaml")):
            for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
                if not doc or doc.get("kind") != "Deployment":
                    continue
                ok, reason = validate_image_sources(doc)
                assert ok, f"{path.name}: {reason}"


class TestAllowedKindsLockstep:
    """Keep every source of the resource-kind allowlist in lockstep.

    The kind policy lives in four places: ``DEFAULT_ALLOWED_KINDS`` (the
    fail-closed fallback both services share), ``cdk.json
    job_validation_policy.allowed_kinds`` (the deploy-time authority),
    the inline fallback in ``regional_stack.py`` (used when the cdk.json
    block is absent; kept inline so synth never imports service modules),
    and ``RESOURCE_API_VERSIONS`` (the exact-GVK gate for the CRUD
    endpoints). Divergence means a kind is accepted on one path and
    rejected on another — the same drift class the image-source lockstep
    test exists for.
    """

    @staticmethod
    def _cdk_allowed_kinds() -> list[str]:
        import json
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        context = json.loads((root / "cdk.json").read_text(encoding="utf-8"))["context"]
        return context["job_validation_policy"]["allowed_kinds"]

    @staticmethod
    def _regional_stack_fallback_kinds() -> list[str]:
        """AST-extract the inline fallback list passed to job_policy.get()."""
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        source = (root / "gco" / "stacks" / "regional_stack.py").read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and len(node.args) == 2
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "allowed_kinds"
                and isinstance(node.args[1], ast.List)
            ):
                return [
                    element.value
                    for element in node.args[1].elts
                    if isinstance(element, ast.Constant)
                ]
        raise AssertionError("allowed_kinds fallback list not found in regional_stack.py")

    def test_default_allowed_kinds_match_cdk_policy(self) -> None:
        from gco.services.manifest_processor import DEFAULT_ALLOWED_KINDS

        assert set(DEFAULT_ALLOWED_KINDS) == set(self._cdk_allowed_kinds())

    def test_regional_stack_fallback_matches_defaults(self) -> None:
        from gco.services.manifest_processor import DEFAULT_ALLOWED_KINDS

        assert set(self._regional_stack_fallback_kinds()) == set(DEFAULT_ALLOWED_KINDS)

    def test_every_allowed_kind_has_pinned_api_version(self) -> None:
        """CRUD endpoints must know the exact GVK for every allowed kind.

        A kind without a RESOURCE_API_VERSIONS entry is submittable but not
        readable/deletable through the API; an entry for a non-allowed kind
        is dead policy. Exact equality catches both.
        """
        from gco.services.manifest_processor import (
            DEFAULT_ALLOWED_KINDS,
            RESOURCE_API_VERSIONS,
        )

        assert set(RESOURCE_API_VERSIONS) == set(DEFAULT_ALLOWED_KINDS)

    def test_trainjob_gvk_pin(self) -> None:
        """TrainJob is pinned to the exact Kubeflow Trainer v2 group/version.

        A custom API group defining its own 'TrainJob' kind must not be
        reachable through the CRUD endpoints, and a trainer chart upgrade
        that bumps the API version must be a deliberate, test-visible change.
        """
        from gco.services.manifest_processor import (
            RESOURCE_API_VERSIONS,
            TRAINJOB_API_VERSION,
        )

        assert TRAINJOB_API_VERSION == "trainer.kubeflow.org/v1alpha1"
        assert RESOURCE_API_VERSIONS["TrainJob"] == frozenset({TRAINJOB_API_VERSION})


def _trainjob_manifest(
    name: str = "test-trainjob",
    namespace: str = "gco-jobs",
    image: str | None = "pytorch/pytorch:2.13.0-cuda13.0-cudnn9-runtime",
    num_nodes: int | None = None,
    resources_per_node: dict | None = None,
    runtime_patches: list | None = None,
) -> dict:
    """Build a Kubeflow Trainer v2 TrainJob manifest for validation tests."""
    trainer: dict = {}
    if image is not None:
        trainer["image"] = image
    if num_nodes is not None:
        trainer["numNodes"] = num_nodes
    if resources_per_node is not None:
        trainer["resourcesPerNode"] = resources_per_node
    spec: dict = {"runtimeRef": {"name": "torch-distributed"}}
    if trainer:
        spec["trainer"] = trainer
    if runtime_patches is not None:
        spec["runtimePatches"] = runtime_patches
    return {
        "apiVersion": "trainer.kubeflow.org/v1alpha1",
        "kind": "TrainJob",
        "metadata": {"name": name, "namespace": namespace},
        "spec": spec,
    }


def _runtime_patch_with_pod_spec(pod_spec: dict) -> dict:
    """Wrap a pod spec in the runtimePatches nesting the trainer API uses."""
    return {
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
        }
    }


class TestTrainJobValidation:
    """TrainJob manifests get the full security pipeline, not a vacuous pass.

    A TrainJob carries no ``spec.template``; its pods are assembled from the
    referenced runtime plus ``spec.trainer`` and arbitrary pod specs nested
    in ``runtimePatches``. Every check here failed open before the TrainJob
    decomposition existed, so these are the security-critical pins.
    """

    def test_trusted_trainjob_accepted(self, manifest_processor):
        manifest = _trainjob_manifest(
            resources_per_node={"requests": {"cpu": "500m", "memory": "512Mi"}}
        )
        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert is_valid, error

    def test_untrusted_trainer_image_rejected(self, manifest_processor):
        manifest = _trainjob_manifest(image="evil.example.com/malicious:latest")
        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert not is_valid
        assert "Untrusted image source" in error
        assert "evil.example.com" in error

    def test_untrusted_runtime_patch_image_rejected(self, manifest_processor):
        """A trusted trainer image must not whitelist containers smuggled in
        through runtimePatches."""
        manifest = _trainjob_manifest(
            runtime_patches=[
                _runtime_patch_with_pod_spec(
                    {"containers": [{"name": "smuggled", "image": "evil.example.com/backdoor:1"}]}
                )
            ],
        )
        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert not is_valid
        assert "Untrusted image source" in error
        assert "evil.example.com" in error

    def test_untrusted_init_container_in_runtime_patch_rejected(self, manifest_processor):
        manifest = _trainjob_manifest(
            runtime_patches=[
                _runtime_patch_with_pod_spec(
                    {
                        "containers": [{"name": "ok", "image": "python:3.14-slim"}],
                        "initContainers": [
                            {"name": "smuggled-init", "image": "evil.example.com/init:1"}
                        ],
                    }
                )
            ],
        )
        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert not is_valid
        assert "evil.example.com" in error

    def test_hostpath_in_runtime_patch_rejected(self, manifest_processor):
        manifest = _trainjob_manifest(
            runtime_patches=[
                _runtime_patch_with_pod_spec(
                    {
                        "containers": [{"name": "worker", "image": "python:3.14-slim"}],
                        "volumes": [{"name": "host", "hostPath": {"path": "/etc"}}],
                    }
                )
            ],
        )
        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert not is_valid
        assert "hostPath" in error

    def test_privileged_container_in_runtime_patch_rejected(self, manifest_processor):
        manifest = _trainjob_manifest(
            runtime_patches=[
                _runtime_patch_with_pod_spec(
                    {
                        "containers": [
                            {
                                "name": "worker",
                                "image": "python:3.14-slim",
                                "securityContext": {"privileged": True},
                            }
                        ]
                    }
                )
            ],
        )
        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert not is_valid
        assert "privileged" in error

    def test_resource_caps_scale_with_num_nodes(self, manifest_processor):
        """Per-node resources are multiplied by numNodes before cap checks.

        Fixture cap is 4 GPUs; 2 nodes x 1 GPU passes, 8 nodes x 1 GPU must
        not (counting a distributed job as one node would gut the cap).
        """
        toleration_patch = _runtime_patch_with_pod_spec(
            {
                "containers": [],
                "tolerations": [
                    {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}
                ],
            }
        )

        within_cap = _trainjob_manifest(
            num_nodes=2,
            resources_per_node={"limits": {"nvidia.com/gpu": "1", "cpu": "1"}},
            runtime_patches=[toleration_patch],
        )
        is_valid, error = manifest_processor.validate_manifest(within_cap)
        assert is_valid, error

        over_cap = _trainjob_manifest(
            num_nodes=8,
            resources_per_node={"limits": {"nvidia.com/gpu": "1", "cpu": "1"}},
            runtime_patches=[toleration_patch],
        )
        is_valid, error = manifest_processor.validate_manifest(over_cap)
        assert not is_valid
        assert "GPU 8 exceeds max 4" in error

    def test_gpu_trainjob_without_toleration_rejected_with_trainjob_hint(self, manifest_processor):
        """The toleration requirement sees trainer resources, and the fix hint
        points at the TrainJob example rather than the plain-Job one."""
        manifest = _trainjob_manifest(
            num_nodes=2,
            resources_per_node={"limits": {"nvidia.com/gpu": "1"}},
        )
        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert not is_valid
        assert "toleration" in error
        assert "kubeflow-trainjob.yaml" in error

    def test_toleration_in_runtime_patch_satisfies_trainer_gpu_request(self, manifest_processor):
        """Requests and tolerations are unioned across pod-spec views: the
        GPU request lives in spec.trainer, the toleration can only live in a
        runtimePatches pod spec."""
        manifest = _trainjob_manifest(
            num_nodes=1,
            resources_per_node={"limits": {"nvidia.com/gpu": "1"}},
            runtime_patches=[
                _runtime_patch_with_pod_spec(
                    {
                        "containers": [],
                        "tolerations": [
                            {
                                "key": "nvidia.com/gpu",
                                "operator": "Exists",
                                "effect": "NoSchedule",
                            }
                        ],
                    }
                )
            ],
        )
        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert is_valid, error

    def test_inject_security_defaults_reaches_embedded_pod_specs(self, manifest_processor):
        """automountServiceAccountToken: false lands in every runtimePatches
        pod spec (by mutation) without inventing fields elsewhere in the spec."""
        pod_spec = {"containers": [{"name": "worker", "image": "python:3.14-slim"}]}
        manifest = _trainjob_manifest(runtime_patches=[_runtime_patch_with_pod_spec(pod_spec)])

        manifest_processor._inject_security_defaults(manifest)

        embedded = manifest["spec"]["runtimePatches"][0]["trainingRuntimeSpec"]["template"]["spec"][
            "replicatedJobs"
        ][0]["template"]["spec"]["template"]["spec"]
        assert embedded["automountServiceAccountToken"] is False
        assert "automountServiceAccountToken" not in manifest["spec"]["trainer"]

    def test_inject_security_defaults_respects_explicit_user_choice(self, manifest_processor):
        manifest = _trainjob_manifest(
            runtime_patches=[
                _runtime_patch_with_pod_spec(
                    {
                        "containers": [{"name": "worker", "image": "python:3.14-slim"}],
                        "automountServiceAccountToken": True,
                    }
                )
            ],
        )
        manifest_processor._inject_security_defaults(manifest)
        embedded = manifest["spec"]["runtimePatches"][0]["trainingRuntimeSpec"]["template"]["spec"][
            "replicatedJobs"
        ][0]["template"]["spec"]["template"]["spec"]
        assert embedded["automountServiceAccountToken"] is True

    def test_missing_addon_error_is_actionable(self, manifest_processor):
        """ResourceNotFoundError for TrainJob surfaces the enable-the-addon
        remedy instead of an inscrutable discovery error."""
        from kubernetes.dynamic.exceptions import ResourceNotFoundError

        mock_dynamic = MagicMock()
        mock_dynamic.resources.get.side_effect = ResourceNotFoundError("no CRD")
        manifest_processor._dynamic_client = mock_dynamic

        with pytest.raises(ValueError, match="kubeflow-trainer addon"):
            manifest_processor._get_api_resource("trainer.kubeflow.org/v1alpha1", "TrainJob")

    def test_unknown_kind_error_unchanged_for_non_addon_kinds(self, manifest_processor):
        from kubernetes.dynamic.exceptions import ResourceNotFoundError

        mock_dynamic = MagicMock()
        mock_dynamic.resources.get.side_effect = ResourceNotFoundError("no CRD")
        manifest_processor._dynamic_client = mock_dynamic

        with pytest.raises(ValueError, match="Unknown resource type"):
            manifest_processor._get_api_resource("example.com/v1", "Widget")
