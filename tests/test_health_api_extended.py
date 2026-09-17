"""
Extended tests for gco/services/health_api.py.

Covers the async lifespan context manager (successful startup and
failure propagation), the stale-status refresh logic where the API
reruns the health monitor when the cached HealthStatus is older
than two minutes, and associated edge cases around cluster_id/region
attribute passthrough. Complements test_health_api.py which covers
the route surface.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestHealthAPILifespan:
    """Tests for Health API lifespan management."""

    @pytest.mark.asyncio
    async def test_lifespan_startup_success(self):
        """Test successful lifespan startup."""
        from gco.models import HealthStatus, ResourceThresholds, ResourceUtilization

        mock_monitor = MagicMock()
        mock_status = HealthStatus(
            cluster_id="test-cluster",
            region="us-east-1",
            timestamp=datetime.now(),
            status="healthy",
            resource_utilization=ResourceUtilization(cpu=50.0, memory=60.0, gpu=30.0),
            thresholds=ResourceThresholds(cpu_threshold=80, memory_threshold=85, gpu_threshold=90),
            active_jobs=5,
        )
        mock_monitor.get_health_status = AsyncMock(return_value=mock_status)
        # Set string values for attributes used by HealthMonitorMetrics
        mock_monitor.cluster_id = "test-cluster"
        mock_monitor.region = "us-east-1"

        with (
            patch(
                "gco.services.health_api.create_health_monitor_from_env",
                return_value=mock_monitor,
            ),
            patch("gco.services.health_api.HealthMonitorMetrics"),
            patch("gco.services.health_api.create_webhook_dispatcher_from_env"),
        ):
            import gco.services.health_api as health_api_module
            from gco.services.health_api import app, lifespan

            async with lifespan(app):
                assert health_api_module.health_monitor is mock_monitor

    @pytest.mark.asyncio
    async def test_lifespan_startup_failure(self):
        """Test lifespan startup failure."""
        with (
            patch(
                "gco.services.health_api.create_health_monitor_from_env",
                side_effect=Exception("Failed to create monitor"),
            ),
            patch("gco.services.health_api.HealthMonitorMetrics"),
            patch("gco.services.health_api.create_webhook_dispatcher_from_env"),
        ):
            from gco.services.health_api import app, lifespan

            with pytest.raises(Exception, match="Failed to create monitor"):
                async with lifespan(app):
                    pass


class TestHealthCheckWithStaleStatus:
    """Tests for health check with stale status."""

    @pytest.mark.asyncio
    async def test_health_check_refreshes_stale_status(self):
        """Test that health check refreshes stale status."""
        from gco.models import HealthStatus, ResourceThresholds, ResourceUtilization

        # Create a status that's more than 2 minutes old
        old_timestamp = datetime.now() - timedelta(minutes=3)
        old_status = HealthStatus(
            cluster_id="test-cluster",
            region="us-east-1",
            timestamp=old_timestamp,
            status="healthy",
            resource_utilization=ResourceUtilization(cpu=50.0, memory=60.0, gpu=30.0),
            thresholds=ResourceThresholds(cpu_threshold=80, memory_threshold=85, gpu_threshold=90),
            active_jobs=5,
        )

        new_status = HealthStatus(
            cluster_id="test-cluster",
            region="us-east-1",
            timestamp=datetime.now(),
            status="healthy",
            resource_utilization=ResourceUtilization(cpu=55.0, memory=65.0, gpu=35.0),
            thresholds=ResourceThresholds(cpu_threshold=80, memory_threshold=85, gpu_threshold=90),
            active_jobs=6,
        )

        mock_monitor = MagicMock()
        mock_monitor.get_health_status = AsyncMock(return_value=new_status)

        import gco.services.health_api as health_api_module

        original_monitor = health_api_module.health_monitor
        original_status = health_api_module.current_health_status

        health_api_module.health_monitor = mock_monitor
        health_api_module.current_health_status = old_status

        try:
            from gco.services.health_api import health_check

            await health_check()

            # Should have refreshed the status
            mock_monitor.get_health_status.assert_called()
        finally:
            health_api_module.health_monitor = original_monitor
            health_api_module.current_health_status = original_status


class TestHealthCheckUnhealthy:
    """Tests for unhealthy status responses."""

    @pytest.mark.asyncio
    async def test_health_check_returns_503_when_unhealthy(self):
        """Test that health check returns 503 when unhealthy."""
        from gco.models import HealthStatus, ResourceThresholds, ResourceUtilization

        unhealthy_status = HealthStatus(
            cluster_id="test-cluster",
            region="us-east-1",
            timestamp=datetime.now(),
            status="unhealthy",
            resource_utilization=ResourceUtilization(cpu=95.0, memory=90.0, gpu=85.0),
            thresholds=ResourceThresholds(cpu_threshold=80, memory_threshold=85, gpu_threshold=90),
            active_jobs=10,
            message="CPU threshold exceeded",
        )

        mock_monitor = MagicMock()
        mock_monitor.get_health_status = AsyncMock(return_value=unhealthy_status)

        import gco.services.health_api as health_api_module

        original_monitor = health_api_module.health_monitor
        original_status = health_api_module.current_health_status

        health_api_module.health_monitor = mock_monitor
        health_api_module.current_health_status = unhealthy_status

        try:
            from gco.services.health_api import health_check

            response = await health_check()

            assert response.status_code == 503
        finally:
            health_api_module.health_monitor = original_monitor
            health_api_module.current_health_status = original_status


class TestHealthCheckNoMonitor:
    """Tests for health check when monitor is not initialized."""

    @pytest.mark.asyncio
    async def test_health_check_no_monitor(self):
        """Test health check when monitor is None."""
        import gco.services.health_api as health_api_module

        original_monitor = health_api_module.health_monitor
        original_status = health_api_module.current_health_status

        health_api_module.health_monitor = None
        health_api_module.current_health_status = None

        try:
            from gco.services.health_api import health_check

            response = await health_check()
            # Should return 503 when monitor is not initialized
            assert response.status_code == 503
        finally:
            health_api_module.health_monitor = original_monitor
            health_api_module.current_health_status = original_status


class TestHealthCheckException:
    """Tests for health check exception handling."""

    @pytest.mark.asyncio
    async def test_health_check_handles_exception(self):
        """Test that health check handles exceptions gracefully."""
        mock_monitor = MagicMock()
        mock_monitor.get_health_status = AsyncMock(side_effect=Exception("API error"))

        import gco.services.health_api as health_api_module

        original_monitor = health_api_module.health_monitor
        original_status = health_api_module.current_health_status

        health_api_module.health_monitor = mock_monitor
        health_api_module.current_health_status = None

        try:
            from gco.services.health_api import health_check

            response = await health_check()

            # Should return 503 with error info
            assert response.status_code == 503
        finally:
            health_api_module.health_monitor = original_monitor
            health_api_module.current_health_status = original_status


class TestMetricsEndpointNoMonitor:
    """Tests for metrics endpoint when monitor is not initialized."""

    @pytest.mark.asyncio
    async def test_metrics_no_monitor(self):
        """Test metrics endpoint when monitor is None."""
        import gco.services.health_api as health_api_module

        original_monitor = health_api_module.health_monitor
        original_status = health_api_module.current_health_status

        health_api_module.health_monitor = None
        health_api_module.current_health_status = None

        try:
            from fastapi import HTTPException

            from gco.services.health_api import get_metrics

            with pytest.raises(HTTPException) as exc_info:
                await get_metrics()
            # The error is wrapped in a 500 error with the 503 detail
            assert exc_info.value.status_code == 500
        finally:
            health_api_module.health_monitor = original_monitor
            health_api_module.current_health_status = original_status


class TestMetricsEndpointSuccess:
    """Tests for metrics endpoint success cases."""

    @pytest.mark.asyncio
    async def test_metrics_returns_data(self):
        """Test metrics endpoint returns proper data."""
        from gco.models import HealthStatus, ResourceThresholds, ResourceUtilization

        mock_status = HealthStatus(
            cluster_id="test-cluster",
            region="us-east-1",
            timestamp=datetime.now(),
            status="healthy",
            resource_utilization=ResourceUtilization(cpu=50.0, memory=60.0, gpu=30.0),
            thresholds=ResourceThresholds(cpu_threshold=80, memory_threshold=85, gpu_threshold=90),
            active_jobs=5,
        )

        mock_monitor = MagicMock()
        mock_monitor.get_health_status = AsyncMock(return_value=mock_status)

        import gco.services.health_api as health_api_module

        original_monitor = health_api_module.health_monitor
        original_status = health_api_module.current_health_status

        health_api_module.health_monitor = mock_monitor
        health_api_module.current_health_status = mock_status

        try:
            from gco.services.health_api import get_metrics

            result = await get_metrics()

            assert result["cluster_id"] == "test-cluster"
            assert result["region"] == "us-east-1"
            assert "resource_utilization" in result
            assert result["resource_utilization"]["cpu_percent"] == 50.0
        finally:
            health_api_module.health_monitor = original_monitor
            health_api_module.current_health_status = original_status


class TestMetricsEndpointException:
    """Tests for metrics endpoint exception handling."""

    @pytest.mark.asyncio
    async def test_metrics_handles_exception(self):
        """Test that metrics endpoint handles exceptions."""
        mock_monitor = MagicMock()
        mock_monitor.get_health_status = AsyncMock(side_effect=Exception("Metrics error"))

        import gco.services.health_api as health_api_module

        original_monitor = health_api_module.health_monitor
        original_status = health_api_module.current_health_status

        health_api_module.health_monitor = mock_monitor
        health_api_module.current_health_status = None

        try:
            from fastapi import HTTPException

            from gco.services.health_api import get_metrics

            with pytest.raises(HTTPException) as exc_info:
                await get_metrics()
            assert exc_info.value.status_code == 500
        finally:
            health_api_module.health_monitor = original_monitor
            health_api_module.current_health_status = original_status


class TestReadyzEndpoint:
    """Tests for readiness endpoint."""

    @pytest.mark.asyncio
    async def test_readyz_no_monitor(self):
        """Test readyz returns 503 when monitor is None."""
        import gco.services.health_api as health_api_module

        original_monitor = health_api_module.health_monitor

        health_api_module.health_monitor = None

        try:
            from fastapi import HTTPException

            from gco.services.health_api import kubernetes_readiness_check

            with pytest.raises(HTTPException) as exc_info:
                await kubernetes_readiness_check()
            assert exc_info.value.status_code == 503
        finally:
            health_api_module.health_monitor = original_monitor

    @pytest.mark.asyncio
    async def test_readyz_with_monitor(self):
        """Test readyz returns 200 when monitor is initialized."""
        import gco.services.health_api as health_api_module

        original_monitor = health_api_module.health_monitor

        health_api_module.health_monitor = MagicMock()

        try:
            from gco.services.health_api import kubernetes_readiness_check

            result = await kubernetes_readiness_check()
            assert result["status"] == "ready"
        finally:
            health_api_module.health_monitor = original_monitor


class TestGlobalExceptionHandler:
    """Tests for global exception handler."""

    @pytest.mark.asyncio
    async def test_global_exception_handler(self):
        """Test global exception handler returns proper response."""
        from fastapi import Request

        from gco.services.health_api import global_exception_handler

        mock_request = MagicMock(spec=Request)
        exc = Exception("Test error")

        response = await global_exception_handler(mock_request, exc)

        assert response.status_code == 500

    @pytest.mark.asyncio
    async def test_global_exception_handler_with_debug(self):
        """Test global exception handler with DEBUG mode."""
        import os

        from fastapi import Request

        with patch.dict(os.environ, {"DEBUG": "true"}):
            from gco.services.health_api import global_exception_handler

            mock_request = MagicMock(spec=Request)
            exc = Exception("Detailed error message")

            response = await global_exception_handler(mock_request, exc)

            assert response.status_code == 500
