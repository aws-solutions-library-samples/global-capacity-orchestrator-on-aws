"""
Preservation tests for manifest validation and auth middleware.

Encodes baseline behavior that must remain unchanged as security
validation grows — if a security toggle or auth rule is tightened
without updating these tests, the regression fires here. Mirrors the
processor and middleware fixture patterns from the sibling suites
(mock_k8s_config + reset_auth_cache) and leans on Hypothesis for a
couple of property-based sweeps over the preserved invariants.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from kubernetes import config as k8s_config

from gco.services.auth_middleware import AuthenticationMiddleware


def _signed_headers(signing_key: str, method: str, target: str, body: str = "") -> dict[str, str]:
    """Build a unique request envelope matching the trusted proxy contract."""
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    canonical = "\n".join(["v1", timestamp, nonce, method.upper(), target, content_hash])
    signature = hmac.new(
        signing_key.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "x-gco-signature-version": "v1",
        "x-gco-signature": signature,
        "x-gco-timestamp": timestamp,
        "x-gco-nonce": nonce,
        "x-gco-content-sha256": content_hash,
    }


# ---------------------------------------------------------------------------
# Fixtures — mirrors test_manifest_processor_extended.py
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


# ---------------------------------------------------------------------------
# Auth middleware fixtures — mirrors test_auth_middleware.py
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_auth_cache():
    """Reset signing-key cache, monotonic timers, client, and replay nonces."""
    import gco.services.auth_middleware as auth_module

    auth_module.clear_token_cache()
    auth_module._secrets_client = None
    yield
    auth_module.clear_token_cache()
    auth_module._secrets_client = None


@pytest.fixture
def app_with_middleware():
    """Create FastAPI app with authentication middleware."""
    app = FastAPI()
    app.add_middleware(AuthenticationMiddleware)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request, exc):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.get("/api/v1/health")
    async def get_health():
        return {"status": "healthy"}

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        return {"status": "ready"}

    @app.get("/metrics")
    async def metrics():
        return {"metrics": []}

    @app.post("/api/v1/manifests")
    async def submit_manifest():
        return {"success": True}

    return app


# ---------------------------------------------------------------------------
# Hypothesis strategies for generating safe manifests
# ---------------------------------------------------------------------------

# Trusted image patterns that the unfixed code already accepts
TRUSTED_IMAGES = st.sampled_from(
    [
        "python:3.14",
        "busybox",
        "nginx:latest",
        "public.ecr.aws/my-org/my-image:v1",
        "nvcr.io/nvidia/pytorch:24.01-py3",
        "docker.io/library/alpine:3.19",
        "gcr.io/google-containers/busybox:1.27",
        "quay.io/prometheus/node-exporter:v1.6.0",
        "registry.k8s.io/pause:3.9",
        "nvidia/cuda:12.3.1-base-ubuntu22.04",
        "pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime",
    ]
)

# Allowed namespaces
ALLOWED_NAMESPACES = st.sampled_from(["default", "gco-jobs"])

# Disallowed namespaces (should be rejected)
DISALLOWED_NAMESPACES = st.sampled_from(
    [
        "kube-system",
        "unauthorized",
        "production",
        "admin",
    ]
)

# Allowed workload kinds that the unfixed code validates resource limits on
WORKLOAD_KINDS = st.sampled_from(["Job", "Deployment", "CronJob", "StatefulSet", "DaemonSet"])

# CPU values within limits (max is 10 cores = 10000m)
SAFE_CPU = st.sampled_from(["100m", "500m", "1", "2", "4"])

# Memory values within limits (max is 32Gi)
SAFE_MEMORY = st.sampled_from(["128Mi", "256Mi", "512Mi", "1Gi", "4Gi", "8Gi"])

# GPU values within limits (max is 4)
SAFE_GPU = st.sampled_from(["0", "1", "2", "4"])


def _make_job_manifest(
    name: str = "test-job",
    namespace: str = "default",
    image: str = "python:3.14",
    cpu: str = "500m",
    memory: str = "512Mi",
    gpu: str | None = None,
    privileged: bool = False,
    allow_priv_esc: bool = False,
) -> dict:
    """Build a Job manifest with given parameters."""
    container: dict = {
        "name": "worker",
        "image": image,
        "resources": {
            "requests": {"cpu": cpu, "memory": memory},
        },
    }
    if gpu and gpu != "0":
        container["resources"]["limits"] = {"nvidia.com/gpu": gpu}
    sec_ctx: dict = {}
    if privileged:
        sec_ctx["privileged"] = True
    if allow_priv_esc:
        sec_ctx["allowPrivilegeEscalation"] = True
    if sec_ctx:
        container["securityContext"] = sec_ctx

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "template": {
                "spec": {
                    "containers": [container],
                    "restartPolicy": "Never",
                }
            }
        },
    }


def _make_deployment_manifest(
    name: str = "test-deploy",
    namespace: str = "default",
    image: str = "python:3.14",
    cpu: str = "500m",
    memory: str = "512Mi",
) -> dict:
    """Build a Deployment manifest with given parameters."""
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "image": image,
                            "resources": {
                                "requests": {"cpu": cpu, "memory": memory},
                            },
                        }
                    ]
                }
            },
        },
    }


def _make_cronjob_manifest(
    name: str = "test-cronjob",
    namespace: str = "default",
    image: str = "python:3.14",
    cpu: str = "500m",
    memory: str = "512Mi",
) -> dict:
    """Build a CronJob manifest with given parameters."""
    return {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "schedule": "*/5 * * * *",
            "jobTemplate": {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "worker",
                                    "image": image,
                                    "resources": {
                                        "requests": {"cpu": cpu, "memory": memory},
                                    },
                                }
                            ]
                        }
                    }
                }
            },
        },
    }


# ==========================================================================
# Preservation: Security context
# ==========================================================================


class TestSecurityContextPreservation:
    """Security context validation preserves existing behavior."""

    def test_privileged_true_rejected(self, manifest_processor):
        """privileged: true still rejected."""
        manifest = _make_job_manifest(privileged=True)
        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert is_valid is False
        assert error is not None
        assert "security" in error.lower() or "Security" in error

    def test_allow_privilege_escalation_rejected(self, manifest_processor):
        """allowPrivilegeEscalation: true still rejected."""
        manifest = _make_job_manifest(allow_priv_esc=True)
        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert is_valid is False
        assert error is not None

    def test_safe_manifest_accepted(self, manifest_processor):
        """safe manifests still accepted."""
        manifest = _make_job_manifest()
        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert is_valid is True
        assert error is None

    @given(
        image=TRUSTED_IMAGES,
        namespace=ALLOWED_NAMESPACES,
        cpu=SAFE_CPU,
        memory=SAFE_MEMORY,
    )
    @settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_safe_manifests_always_accepted(
        self, manifest_processor, image, namespace, cpu, memory
    ):
        """For all manifests with no dangerous
        security fields and valid images/namespace/resources, validation passes."""
        manifest = _make_job_manifest(image=image, namespace=namespace, cpu=cpu, memory=memory)
        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert is_valid is True, f"Safe manifest rejected: {error}"


# ==========================================================================
# Preservation: Image source validation
# ==========================================================================


class TestImageSourcePreservation:
    """Image source validation preserves existing behavior."""

    @given(image=TRUSTED_IMAGES)
    @settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_trusted_images_accepted(self, manifest_processor, image):
        """Trusted images still accepted."""
        manifest = _make_job_manifest(image=image)
        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert is_valid is True, f"Trusted image '{image}' rejected: {error}"

    def test_untrusted_image_rejected(self, manifest_processor):
        """Untrusted images in containers[] still rejected."""
        manifest = _make_job_manifest(image="evil-registry.com/malicious:latest")
        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert is_valid is False
        assert error is not None
        assert "untrusted" in error.lower() or "Untrusted" in error

    def test_untrusted_image_with_slash(self, manifest_processor):
        """Untrusted org images rejected."""
        manifest = _make_job_manifest(image="unknown-org/suspicious-tool:v1")
        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert is_valid is False


# ==========================================================================
# Preservation: Resource limits
# ==========================================================================


class TestResourceLimitPreservation:
    """Resource limit validation preserves existing behavior."""

    @given(cpu=SAFE_CPU, memory=SAFE_MEMORY)
    @settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_within_limits_accepted(self, manifest_processor, cpu, memory):
        """Within-limits manifests still accepted."""
        manifest = _make_job_manifest(cpu=cpu, memory=memory)
        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert is_valid is True, (
            f"Within-limits manifest rejected (cpu={cpu}, mem={memory}): {error}"
        )

    def test_exceeds_cpu_limit_rejected(self, manifest_processor):
        """Over-limit CPU still rejected."""
        manifest = _make_job_manifest(cpu="20000m")  # 20 cores, max is 10
        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert is_valid is False
        assert error is not None
        assert "cpu" in error.lower() or "CPU" in error

    def test_exceeds_memory_limit_rejected(self, manifest_processor):
        """Over-limit memory still rejected."""
        manifest = _make_job_manifest(memory="64Gi")  # max is 32Gi
        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert is_valid is False
        assert error is not None

    def test_exceeds_gpu_limit_rejected(self, manifest_processor):
        """Over-limit GPU still rejected."""
        manifest = _make_job_manifest(gpu="8")  # max is 4
        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert is_valid is False
        assert error is not None


# ==========================================================================
# Preservation: Namespace restriction
# ==========================================================================


class TestNamespacePreservation:
    """Namespace restriction preserves existing behavior."""

    @given(namespace=ALLOWED_NAMESPACES)
    @settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_allowed_namespace_accepted(self, manifest_processor, namespace):
        """Allowed namespaces still accepted."""
        manifest = _make_job_manifest(namespace=namespace)
        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert is_valid is True, f"Allowed namespace '{namespace}' rejected: {error}"

    @given(namespace=DISALLOWED_NAMESPACES)
    @settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_disallowed_namespace_rejected(self, manifest_processor, namespace):
        """Disallowed namespaces still rejected."""
        manifest = _make_job_manifest(namespace=namespace)
        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert is_valid is False
        assert "not allowed" in error.lower() or "Namespace" in error


# ==========================================================================
# Preservation: Allowed kinds
# ==========================================================================


class TestAllowedKindPreservation:
    """Allowed kind validation preserves existing behavior."""

    def test_job_kind_accepted(self, manifest_processor):
        """kind: Job still works."""
        manifest = _make_job_manifest()
        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert is_valid is True, f"Job rejected: {error}"

    def test_deployment_kind_accepted(self, manifest_processor):
        """kind: Deployment still works."""
        manifest = _make_deployment_manifest()
        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert is_valid is True, f"Deployment rejected: {error}"

    def test_cronjob_kind_accepted(self, manifest_processor):
        """kind: CronJob still works."""
        manifest = _make_cronjob_manifest()
        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert is_valid is True, f"CronJob rejected: {error}"

    @given(kind=WORKLOAD_KINDS, image=TRUSTED_IMAGES, namespace=ALLOWED_NAMESPACES)
    @settings(max_examples=25, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_all_workload_kinds_with_valid_manifests_accepted(
        self, manifest_processor, kind, image, namespace
    ):
        """All allowed workload
        kinds with valid manifests pass validation."""
        if kind == "CronJob":
            manifest = _make_cronjob_manifest(
                name=f"test-{kind.lower()}", namespace=namespace, image=image
            )
        elif kind == "Deployment":
            manifest = _make_deployment_manifest(
                name=f"test-{kind.lower()}", namespace=namespace, image=image
            )
        else:
            # Job, StatefulSet, DaemonSet — use Job-style template structure
            manifest = {
                "apiVersion": "batch/v1" if kind == "Job" else "apps/v1",
                "kind": kind,
                "metadata": {"name": f"test-{kind.lower()}", "namespace": namespace},
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "worker",
                                    "image": image,
                                    "resources": {
                                        "requests": {"cpu": "500m", "memory": "512Mi"},
                                    },
                                }
                            ]
                        }
                    }
                },
            }
            if kind in ("StatefulSet", "DaemonSet"):
                manifest["spec"]["selector"] = {"matchLabels": {"app": f"test-{kind.lower()}"}}
        is_valid, error = manifest_processor.validate_manifest(manifest)
        assert is_valid is True, f"Kind '{kind}' with valid manifest rejected: {error}"


# ==========================================================================
# Preservation: Backend HMAC envelope validation
# ==========================================================================


class TestAuthEnvelopePreservation:
    """Backend HMAC validation preserves the trusted-proxy boundary."""

    def test_valid_signature_accepted(self, app_with_middleware):
        """A fresh envelope signed by a cached key is accepted."""
        with patch(
            "gco.services.auth_middleware.get_valid_tokens",
            return_value={"valid-key"},
        ):
            client = TestClient(app_with_middleware, raise_server_exceptions=False)
            response = client.post(
                "/api/v1/manifests",
                headers=_signed_headers("valid-key", "POST", "/api/v1/manifests"),
            )
            assert response.status_code == 200

    def test_signature_from_unknown_key_returns_403(self, app_with_middleware):
        """A fresh envelope signed by an unknown key still gets 403."""
        with patch(
            "gco.services.auth_middleware.get_valid_tokens",
            return_value={"valid-key"},
        ):
            client = TestClient(app_with_middleware, raise_server_exceptions=False)
            response = client.post(
                "/api/v1/manifests",
                headers=_signed_headers("wrong-key", "POST", "/api/v1/manifests"),
            )
            assert response.status_code == 403

    def test_missing_signature_returns_403(self, app_with_middleware):
        """Requests without an envelope still get 403."""
        with patch(
            "gco.services.auth_middleware.get_valid_tokens",
            return_value={"valid-key"},
        ):
            client = TestClient(app_with_middleware, raise_server_exceptions=False)
            response = client.post("/api/v1/manifests")
            assert response.status_code == 403

    def test_healthz_bypasses_auth(self, app_with_middleware):
        """/healthz bypasses auth."""
        with (
            patch.dict(
                "os.environ",
                {"AUTH_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test"},
            ),
            patch(
                "gco.services.auth_middleware.get_valid_tokens",
                return_value={"secret-token"},
            ),
        ):
            client = TestClient(app_with_middleware)
            response = client.get("/healthz")
            assert response.status_code == 200

    def test_readyz_bypasses_auth(self, app_with_middleware):
        """/readyz bypasses auth."""
        with (
            patch.dict(
                "os.environ",
                {"AUTH_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test"},
            ),
            patch(
                "gco.services.auth_middleware.get_valid_tokens",
                return_value={"secret-token"},
            ),
        ):
            client = TestClient(app_with_middleware)
            response = client.get("/readyz")
            assert response.status_code == 200

    def test_metrics_bypasses_auth(self, app_with_middleware):
        """/metrics bypasses auth."""
        with (
            patch.dict(
                "os.environ",
                {"AUTH_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test"},
            ),
            patch(
                "gco.services.auth_middleware.get_valid_tokens",
                return_value={"secret-token"},
            ),
        ):
            client = TestClient(app_with_middleware)
            response = client.get("/metrics")
            assert response.status_code == 200

    def test_api_health_bypasses_auth(self, app_with_middleware):
        """/api/v1/health bypasses auth."""
        with (
            patch.dict(
                "os.environ",
                {"AUTH_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test"},
            ),
            patch(
                "gco.services.auth_middleware.get_valid_tokens",
                return_value={"secret-token"},
            ),
        ):
            client = TestClient(app_with_middleware)
            response = client.get("/api/v1/health")
            assert response.status_code == 200

    def test_cached_rotation_keys_sign_distinct_requests(self, app_with_middleware):
        """Either cached rotation key can authenticate its own unique envelope."""
        with patch(
            "gco.services.auth_middleware.get_valid_tokens",
            return_value={"key-a", "key-b"},
        ):
            client = TestClient(app_with_middleware)
            r1 = client.post(
                "/api/v1/manifests",
                headers=_signed_headers("key-a", "POST", "/api/v1/manifests"),
            )
            r2 = client.post(
                "/api/v1/manifests",
                headers=_signed_headers("key-b", "POST", "/api/v1/manifests"),
            )
            assert r1.status_code == 200
            assert r2.status_code == 200


# ==========================================================================
# Preservation: Dry-run mode
# ==========================================================================


class TestDryRunPreservation:
    """Dry-run mode preserves existing behavior."""

    @pytest.mark.asyncio
    async def test_dry_run_validates_without_applying(self, manifest_processor):
        """dry_run: true validates without applying."""
        from gco.models import ManifestSubmissionRequest

        request = ManifestSubmissionRequest(
            manifests=[
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {"name": "dry-run-job", "namespace": "default"},
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

    @pytest.mark.asyncio
    async def test_dry_run_rejects_invalid_manifest(self, manifest_processor):
        """dry_run still validates and rejects bad manifests."""
        from gco.models import ManifestSubmissionRequest

        request = ManifestSubmissionRequest(
            manifests=[
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {"name": "bad-job", "namespace": "default"},
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
            dry_run=True,
        )

        response = await manifest_processor.process_manifest_submission(request)
        assert response.success is False
        assert len(response.resources) == 1
        assert response.resources[0].status == "failed"


# ==========================================================================
# Preservation: Manifest apply flow
# ==========================================================================


class TestManifestApplyFlowPreservation:
    """Manifest apply flow preserves existing behavior."""

    @pytest.mark.asyncio
    async def test_valid_manifest_reaches_apply(self, manifest_processor):
        """Valid manifests proceed to apply."""
        from gco.models import ManifestSubmissionRequest

        apply_called = False

        async def mock_apply(manifest_data, namespace=None):
            nonlocal apply_called
            apply_called = True
            from gco.models import ResourceStatus

            return ResourceStatus(
                api_version="batch/v1",
                kind="Job",
                name="test-job",
                namespace="default",
                status="created",
                message="Resource created successfully",
            )

        manifest_processor._apply_manifest = mock_apply

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

        response = await manifest_processor.process_manifest_submission(request)
        assert apply_called is True
        assert response.success is True
