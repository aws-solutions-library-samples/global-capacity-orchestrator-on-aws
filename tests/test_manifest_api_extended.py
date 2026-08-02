"""
Extended tests for gco/services/manifest_api.py.

Covers the async lifespan (successful startup wires a
ManifestProcessor into the module global, failures propagate), the
submit_manifests endpoint with a full ResourceStatus response, and
other endpoint wiring that pairs with the focused coverage tests.
Unlike the TestClient-based suites, a couple of these call the route
functions directly after mutating module-level state so the handler
logic can be asserted in isolation.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestManifestAPILifespan:
    """Tests for Manifest API lifespan management."""

    @pytest.mark.asyncio
    async def test_lifespan_startup_success(self):
        """Test successful lifespan startup."""
        mock_processor = MagicMock()
        mock_processor.cluster_id = "test-cluster"
        mock_processor.region = "us-east-1"

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_processor,
        ):
            import gco.services.manifest_api as manifest_api_module
            from gco.services.manifest_api import app, lifespan

            async with lifespan(app):
                assert manifest_api_module.manifest_processor is not None

    @pytest.mark.asyncio
    async def test_lifespan_startup_failure(self):
        """Test lifespan startup failure."""
        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            side_effect=Exception("Failed to create processor"),
        ):
            from gco.services.manifest_api import app, lifespan

            with pytest.raises(Exception, match="Failed to create processor"):
                async with lifespan(app):
                    pass


class TestSubmitManifestsEndpoint:
    """Tests for manifest submission endpoint."""

    @pytest.mark.asyncio
    async def test_submit_manifests_success(self):
        """Test successful manifest submission."""
        from gco.models import ManifestSubmissionResponse, ResourceStatus

        mock_response = ManifestSubmissionResponse(
            success=True,
            cluster_id="test-cluster",
            region="us-east-1",
            resources=[
                ResourceStatus(
                    api_version="v1",
                    kind="ConfigMap",
                    name="test-config",
                    namespace="default",
                    status="created",
                )
            ],
        )

        mock_processor = MagicMock()
        mock_processor.cluster_id = "test-cluster"
        mock_processor.region = "us-east-1"
        mock_processor.process_manifest_submission = AsyncMock(return_value=mock_response)

        import gco.services.manifest_api as manifest_api_module

        original_processor = manifest_api_module.manifest_processor
        manifest_api_module.manifest_processor = mock_processor

        try:
            from gco.services.api_routes.manifests import submit_manifests
            from gco.services.api_shared import ManifestSubmissionAPIRequest

            request = ManifestSubmissionAPIRequest(
                manifests=[
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {"name": "test", "namespace": "default"},
                        "data": {"key": "value"},
                    }
                ],
                namespace="default",
            )

            response = await submit_manifests(request)
            assert response.status_code == 200
        finally:
            manifest_api_module.manifest_processor = original_processor

    @pytest.mark.asyncio
    async def test_submit_manifests_failure(self):
        """Test manifest submission failure."""
        from gco.models import ManifestSubmissionResponse, ResourceStatus

        mock_response = ManifestSubmissionResponse(
            success=False,
            cluster_id="test-cluster",
            region="us-east-1",
            resources=[
                ResourceStatus(
                    api_version="v1",
                    kind="ConfigMap",
                    name="test-config",
                    namespace="default",
                    status="failed",
                    message="Validation failed",
                )
            ],
            errors=["Validation failed"],
        )

        mock_processor = MagicMock()
        mock_processor.cluster_id = "test-cluster"
        mock_processor.region = "us-east-1"
        mock_processor.process_manifest_submission = AsyncMock(return_value=mock_response)

        import gco.services.manifest_api as manifest_api_module

        original_processor = manifest_api_module.manifest_processor
        manifest_api_module.manifest_processor = mock_processor

        try:
            from gco.services.api_routes.manifests import submit_manifests
            from gco.services.api_shared import ManifestSubmissionAPIRequest

            request = ManifestSubmissionAPIRequest(
                manifests=[
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {"name": "test", "namespace": "default"},
                        "data": {"key": "value"},
                    }
                ],
            )

            response = await submit_manifests(request)
            assert response.status_code == 400
        finally:
            manifest_api_module.manifest_processor = original_processor

    @pytest.mark.asyncio
    async def test_submit_manifests_no_processor(self):
        """Test manifest submission when processor is None."""
        import gco.services.manifest_api as manifest_api_module

        original_processor = manifest_api_module.manifest_processor
        manifest_api_module.manifest_processor = None

        try:
            from fastapi import HTTPException

            from gco.services.api_routes.manifests import submit_manifests
            from gco.services.api_shared import ManifestSubmissionAPIRequest

            request = ManifestSubmissionAPIRequest(
                manifests=[{"apiVersion": "v1", "kind": "ConfigMap"}],
            )

            with pytest.raises(HTTPException) as exc_info:
                await submit_manifests(request)
            assert exc_info.value.status_code == 503
        finally:
            manifest_api_module.manifest_processor = original_processor

    @pytest.mark.asyncio
    async def test_submit_manifests_exception(self):
        """Test manifest submission exception handling."""
        mock_processor = MagicMock()
        mock_processor.process_manifest_submission = AsyncMock(
            side_effect=Exception("Processing error")
        )

        import gco.services.manifest_api as manifest_api_module

        original_processor = manifest_api_module.manifest_processor
        manifest_api_module.manifest_processor = mock_processor

        try:
            from fastapi import HTTPException

            from gco.services.api_routes.manifests import submit_manifests
            from gco.services.api_shared import ManifestSubmissionAPIRequest

            # A well-formed manifest so the request construction succeeds and
            # the mocked processor's generic Exception is what propagates.
            request = ManifestSubmissionAPIRequest(
                manifests=[
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {"name": "cm", "namespace": "gco-jobs"},
                        "data": {"key": "value"},
                    }
                ],
            )

            with pytest.raises(HTTPException) as exc_info:
                await submit_manifests(request)
            assert exc_info.value.status_code == 500
        finally:
            manifest_api_module.manifest_processor = original_processor


class TestDeleteResourceEndpoint:
    """Tests for delete resource endpoint."""

    @pytest.mark.asyncio
    async def test_delete_resource_success(self):
        """Test successful resource deletion."""
        from gco.models import ResourceStatus

        mock_status = ResourceStatus(
            api_version="apps/v1",
            kind="Deployment",
            name="test-app",
            namespace="default",
            status="deleted",
        )

        mock_processor = MagicMock()
        mock_processor.cluster_id = "test-cluster"
        mock_processor.region = "us-east-1"
        mock_processor.delete_resource = AsyncMock(return_value=mock_status)

        import gco.services.manifest_api as manifest_api_module

        original_processor = manifest_api_module.manifest_processor
        manifest_api_module.manifest_processor = mock_processor

        try:
            from gco.services.api_routes.manifests import delete_resource

            response = await delete_resource("default", "test-app")
            assert response.status_code == 200
        finally:
            manifest_api_module.manifest_processor = original_processor

    @pytest.mark.asyncio
    async def test_delete_resource_failure(self):
        """Test resource deletion failure."""
        from gco.models import ResourceStatus

        mock_status = ResourceStatus(
            api_version="apps/v1",
            kind="Deployment",
            name="test-app",
            namespace="default",
            status="failed",
            message="Resource not found",
        )

        mock_processor = MagicMock()
        mock_processor.cluster_id = "test-cluster"
        mock_processor.region = "us-east-1"
        mock_processor.delete_resource = AsyncMock(return_value=mock_status)

        import gco.services.manifest_api as manifest_api_module

        original_processor = manifest_api_module.manifest_processor
        manifest_api_module.manifest_processor = mock_processor

        try:
            from gco.services.api_routes.manifests import delete_resource

            response = await delete_resource("default", "test-app")
            assert response.status_code == 400
        finally:
            manifest_api_module.manifest_processor = original_processor

    @pytest.mark.asyncio
    async def test_delete_resource_forbidden(self):
        """Policy denials are preserved as HTTP 403 responses."""
        from fastapi import HTTPException

        from gco.models import ResourceStatus

        mock_status = ResourceStatus(
            api_version="v1",
            kind="Secret",
            name="credentials",
            namespace="default",
            status="forbidden",
            message="Resource kind 'Secret' is not allowed",
        )
        mock_processor = MagicMock()
        mock_processor.delete_resource = AsyncMock(return_value=mock_status)

        import gco.services.manifest_api as manifest_api_module

        original_processor = manifest_api_module.manifest_processor
        manifest_api_module.manifest_processor = mock_processor

        try:
            from gco.services.api_routes.manifests import delete_resource

            with pytest.raises(HTTPException) as exc_info:
                await delete_resource("default", "credentials", kind="Secret")
            assert exc_info.value.status_code == 403
            assert "not allowed" in exc_info.value.detail
        finally:
            manifest_api_module.manifest_processor = original_processor

    @pytest.mark.asyncio
    async def test_delete_resource_no_processor(self):
        """Test resource deletion when processor is None."""
        import gco.services.manifest_api as manifest_api_module

        original_processor = manifest_api_module.manifest_processor
        manifest_api_module.manifest_processor = None

        try:
            from fastapi import HTTPException

            from gco.services.api_routes.manifests import delete_resource

            with pytest.raises(HTTPException) as exc_info:
                await delete_resource("default", "test-app")
            assert exc_info.value.status_code == 503
        finally:
            manifest_api_module.manifest_processor = original_processor

    @pytest.mark.asyncio
    async def test_delete_resource_exception(self):
        """Test resource deletion exception handling."""
        mock_processor = MagicMock()
        mock_processor.delete_resource = AsyncMock(side_effect=Exception("Delete error"))

        import gco.services.manifest_api as manifest_api_module

        original_processor = manifest_api_module.manifest_processor
        manifest_api_module.manifest_processor = mock_processor

        try:
            from fastapi import HTTPException

            from gco.services.api_routes.manifests import delete_resource

            with pytest.raises(HTTPException) as exc_info:
                await delete_resource("default", "test-app")
            assert exc_info.value.status_code == 500
        finally:
            manifest_api_module.manifest_processor = original_processor


class TestGetResourceEndpoint:
    """Tests for get resource endpoint."""

    @pytest.mark.asyncio
    async def test_get_resource_found(self):
        """Test getting resource that exists."""
        mock_processor = MagicMock()
        mock_processor.cluster_id = "test-cluster"
        mock_processor.region = "us-east-1"
        mock_processor.get_resource_status = AsyncMock(
            return_value={"exists": True, "name": "test-app", "status": "Running"}
        )

        import gco.services.manifest_api as manifest_api_module

        original_processor = manifest_api_module.manifest_processor
        manifest_api_module.manifest_processor = mock_processor

        try:
            from gco.services.api_routes.manifests import get_resource_status

            response = await get_resource_status("default", "test-app")
            assert response.status_code == 200
        finally:
            manifest_api_module.manifest_processor = original_processor

    @pytest.mark.asyncio
    async def test_get_resource_not_found(self):
        """Test getting resource that doesn't exist."""
        mock_processor = MagicMock()
        mock_processor.cluster_id = "test-cluster"
        mock_processor.region = "us-east-1"
        mock_processor.get_resource_status = AsyncMock(return_value={"exists": False})

        import gco.services.manifest_api as manifest_api_module

        original_processor = manifest_api_module.manifest_processor
        manifest_api_module.manifest_processor = mock_processor

        try:
            from gco.services.api_routes.manifests import get_resource_status

            response = await get_resource_status("default", "nonexistent")
            assert response.status_code == 404
        finally:
            manifest_api_module.manifest_processor = original_processor

    @pytest.mark.asyncio
    async def test_get_resource_forbidden(self):
        """Policy denials are preserved as HTTP 403 responses."""
        from fastapi import HTTPException

        mock_processor = MagicMock()
        mock_processor.get_resource_status = AsyncMock(
            return_value={
                "exists": False,
                "forbidden": True,
                "error": "Namespace 'kube-system' is not allowed",
            }
        )

        import gco.services.manifest_api as manifest_api_module

        original_processor = manifest_api_module.manifest_processor
        manifest_api_module.manifest_processor = mock_processor

        try:
            from gco.services.api_routes.manifests import get_resource_status

            with pytest.raises(HTTPException) as exc_info:
                await get_resource_status("kube-system", "test-app")
            assert exc_info.value.status_code == 403
            assert "not allowed" in exc_info.value.detail
        finally:
            manifest_api_module.manifest_processor = original_processor

    @pytest.mark.asyncio
    async def test_get_resource_returns_none(self):
        """Test getting resource when get_resource_status returns None."""
        mock_processor = MagicMock()
        mock_processor.cluster_id = "test-cluster"
        mock_processor.region = "us-east-1"
        mock_processor.get_resource_status = AsyncMock(return_value=None)

        import gco.services.manifest_api as manifest_api_module

        original_processor = manifest_api_module.manifest_processor
        manifest_api_module.manifest_processor = mock_processor

        try:
            from fastapi import HTTPException

            from gco.services.api_routes.manifests import get_resource_status

            with pytest.raises(HTTPException) as exc_info:
                await get_resource_status("default", "test-app")
            assert exc_info.value.status_code == 500
        finally:
            manifest_api_module.manifest_processor = original_processor

    @pytest.mark.asyncio
    async def test_get_resource_no_processor(self):
        """Test getting resource when processor is None."""
        import gco.services.manifest_api as manifest_api_module

        original_processor = manifest_api_module.manifest_processor
        manifest_api_module.manifest_processor = None

        try:
            from fastapi import HTTPException

            from gco.services.api_routes.manifests import get_resource_status

            with pytest.raises(HTTPException) as exc_info:
                await get_resource_status("default", "test-app")
            assert exc_info.value.status_code == 503
        finally:
            manifest_api_module.manifest_processor = original_processor

    @pytest.mark.asyncio
    async def test_get_resource_exception(self):
        """Test getting resource exception handling."""
        mock_processor = MagicMock()
        mock_processor.get_resource_status = AsyncMock(side_effect=Exception("Get error"))

        import gco.services.manifest_api as manifest_api_module

        original_processor = manifest_api_module.manifest_processor
        manifest_api_module.manifest_processor = mock_processor

        try:
            from fastapi import HTTPException

            from gco.services.api_routes.manifests import get_resource_status

            with pytest.raises(HTTPException) as exc_info:
                await get_resource_status("default", "test-app")
            assert exc_info.value.status_code == 500
        finally:
            manifest_api_module.manifest_processor = original_processor


class TestValidateManifestsEndpoint:
    """Tests for validate manifests endpoint."""

    @pytest.mark.asyncio
    async def test_validate_manifests_all_valid(self):
        """Test validating all valid manifests."""
        mock_processor = MagicMock()
        mock_processor.cluster_id = "test-cluster"
        mock_processor.region = "us-east-1"
        mock_processor.validate_manifest = MagicMock(return_value=(True, None))

        import gco.services.manifest_api as manifest_api_module

        original_processor = manifest_api_module.manifest_processor
        manifest_api_module.manifest_processor = mock_processor

        try:
            from gco.services.api_routes.manifests import validate_manifests
            from gco.services.api_shared import ManifestSubmissionAPIRequest

            request = ManifestSubmissionAPIRequest(
                manifests=[
                    {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "test1"}},
                    {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "test2"}},
                ],
            )

            response = await validate_manifests(request)
            assert response.status_code == 200
        finally:
            manifest_api_module.manifest_processor = original_processor

    @pytest.mark.asyncio
    async def test_validate_manifests_some_invalid(self):
        """Test validating manifests with some invalid."""
        mock_processor = MagicMock()
        mock_processor.cluster_id = "test-cluster"
        mock_processor.region = "us-east-1"

        def validate_side_effect(manifest, _default_namespace=None):
            if manifest.get("metadata", {}).get("name") == "invalid":
                return (False, "Invalid manifest")
            return (True, None)

        mock_processor.validate_manifest = MagicMock(side_effect=validate_side_effect)

        import gco.services.manifest_api as manifest_api_module

        original_processor = manifest_api_module.manifest_processor
        manifest_api_module.manifest_processor = mock_processor

        try:
            from gco.services.api_routes.manifests import validate_manifests
            from gco.services.api_shared import ManifestSubmissionAPIRequest

            request = ManifestSubmissionAPIRequest(
                manifests=[
                    {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "valid"}},
                    {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "invalid"}},
                ],
            )

            response = await validate_manifests(request)
            assert response.status_code == 200
            # Response should indicate not all valid
        finally:
            manifest_api_module.manifest_processor = original_processor

    @pytest.mark.asyncio
    async def test_validate_manifests_no_processor(self):
        """Test validating manifests when processor is None."""
        import gco.services.manifest_api as manifest_api_module

        original_processor = manifest_api_module.manifest_processor
        manifest_api_module.manifest_processor = None

        try:
            from fastapi import HTTPException

            from gco.services.api_routes.manifests import validate_manifests
            from gco.services.api_shared import ManifestSubmissionAPIRequest

            request = ManifestSubmissionAPIRequest(
                manifests=[{"apiVersion": "v1", "kind": "ConfigMap"}],
            )

            with pytest.raises(HTTPException) as exc_info:
                await validate_manifests(request)
            assert exc_info.value.status_code == 503
        finally:
            manifest_api_module.manifest_processor = original_processor

    @pytest.mark.asyncio
    async def test_validate_manifests_exception(self):
        """Test validating manifests exception handling."""
        mock_processor = MagicMock()
        mock_processor.validate_manifest = MagicMock(side_effect=Exception("Validation error"))

        import gco.services.manifest_api as manifest_api_module

        original_processor = manifest_api_module.manifest_processor
        manifest_api_module.manifest_processor = mock_processor

        try:
            from fastapi import HTTPException

            from gco.services.api_routes.manifests import validate_manifests
            from gco.services.api_shared import ManifestSubmissionAPIRequest

            request = ManifestSubmissionAPIRequest(
                manifests=[{"apiVersion": "v1", "kind": "ConfigMap"}],
            )

            with pytest.raises(HTTPException) as exc_info:
                await validate_manifests(request)
            assert exc_info.value.status_code == 500
        finally:
            manifest_api_module.manifest_processor = original_processor


class TestHealthCheckEndpoint:
    """Tests for health check endpoint."""

    @pytest.mark.asyncio
    async def test_health_check_healthy(self):
        """Test health check when healthy."""
        mock_processor = MagicMock()
        mock_processor.cluster_id = "test-cluster"
        mock_processor.region = "us-east-1"
        mock_processor.core_v1 = MagicMock()
        mock_processor.core_v1.list_namespace = MagicMock()

        import gco.services.manifest_api as manifest_api_module

        original_processor = manifest_api_module.manifest_processor
        manifest_api_module.manifest_processor = mock_processor

        try:
            from gco.services.manifest_api import health_check

            response = await health_check()
            assert response.status_code == 200
        finally:
            manifest_api_module.manifest_processor = original_processor

    @pytest.mark.asyncio
    async def test_health_check_unhealthy(self):
        """Test health check when Kubernetes API is unhealthy."""
        mock_processor = MagicMock()
        mock_processor.cluster_id = "test-cluster"
        mock_processor.region = "us-east-1"
        mock_processor.core_v1 = MagicMock()
        mock_processor.core_v1.list_namespace = MagicMock(side_effect=Exception("API error"))

        import gco.services.manifest_api as manifest_api_module

        original_processor = manifest_api_module.manifest_processor
        manifest_api_module.manifest_processor = mock_processor

        try:
            from gco.services.manifest_api import health_check

            response = await health_check()
            assert response.status_code == 503
        finally:
            manifest_api_module.manifest_processor = original_processor

    @pytest.mark.asyncio
    async def test_health_check_no_processor(self):
        """Test health check when processor is None."""
        import gco.services.manifest_api as manifest_api_module

        original_processor = manifest_api_module.manifest_processor
        manifest_api_module.manifest_processor = None

        try:
            from gco.services.manifest_api import health_check

            response = await health_check()
            assert response.status_code == 503
        finally:
            manifest_api_module.manifest_processor = original_processor


class TestReadyzEndpoint:
    """Tests for readiness endpoint."""

    @pytest.mark.asyncio
    async def test_readyz_no_processor(self):
        """Test readyz returns 503 when processor is None."""
        import gco.services.manifest_api as manifest_api_module

        original_processor = manifest_api_module.manifest_processor
        manifest_api_module.manifest_processor = None

        try:
            from fastapi import HTTPException

            from gco.services.manifest_api import kubernetes_readiness_check

            with pytest.raises(HTTPException) as exc_info:
                await kubernetes_readiness_check()
            assert exc_info.value.status_code == 503
        finally:
            manifest_api_module.manifest_processor = original_processor

    @pytest.mark.asyncio
    async def test_readyz_with_processor(self):
        """Test readyz returns 200 when processor is initialized."""
        import gco.services.manifest_api as manifest_api_module

        original_processor = manifest_api_module.manifest_processor
        manifest_api_module.manifest_processor = MagicMock()

        try:
            from gco.services.manifest_api import kubernetes_readiness_check

            result = await kubernetes_readiness_check()
            assert result["status"] == "ready"
        finally:
            manifest_api_module.manifest_processor = original_processor


class TestGlobalExceptionHandler:
    """Tests for global exception handler."""

    @pytest.mark.asyncio
    async def test_global_exception_handler(self):
        """Test global exception handler returns proper response."""
        from fastapi import Request

        from gco.services.manifest_api import global_exception_handler

        mock_request = MagicMock(spec=Request)
        mock_request.method = "POST"
        mock_request.url = "http://test/api/v1/manifests"
        exc = Exception("Test error")

        response = await global_exception_handler(mock_request, exc)

        assert response.status_code == 500


class TestStatusEndpointWithProcessor:
    """Tests for status endpoint with processor."""

    @pytest.mark.asyncio
    async def test_status_with_processor(self):
        """Test status endpoint with initialized processor."""
        mock_processor = MagicMock()
        mock_processor.cluster_id = "test-cluster"
        mock_processor.region = "us-east-1"
        mock_processor.max_cpu_per_manifest = 10000
        mock_processor.max_memory_per_manifest = 34359738368
        mock_processor.max_gpu_per_manifest = 4
        mock_processor.allowed_namespaces = {"default", "gco-jobs"}
        mock_processor.validation_enabled = True

        import gco.services.manifest_api as manifest_api_module

        original_processor = manifest_api_module.manifest_processor
        manifest_api_module.manifest_processor = mock_processor

        try:
            from gco.services.manifest_api import get_service_status

            result = await get_service_status()

            assert result["cluster_id"] == "test-cluster"
            assert result["region"] == "us-east-1"
            assert result["manifest_processor_initialized"] is True
            assert "resource_limits" in result
        finally:
            manifest_api_module.manifest_processor = original_processor
