"""
Tests for gco/services/api_routes/cost.py — the /api/v1/cost/* proxy router.

The router relays authenticated requests to the internal cost-monitor
service. These tests mount the router alone (no auth middleware; the proxy
logic is transport-independent) and patch httpx.AsyncClient to prove: happy
GET/POST relays, error-status propagation from the cost monitor, connection
failures mapping to a clear 503 (the disabled-feature answer), non-JSON and
non-object bodies mapping to 502, and the COST_MONITOR_URL override.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gco.services.api_routes.cost import router


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def _async_client_returning(response=None, error=None):
    """Build an AsyncClient context-manager double for httpx."""
    instance = MagicMock()
    if error is not None:
        instance.get = AsyncMock(side_effect=error)
        instance.post = AsyncMock(side_effect=error)
    else:
        instance.get = AsyncMock(return_value=response)
        instance.post = AsyncMock(return_value=response)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=instance)
    context.__aexit__ = AsyncMock(return_value=False)
    return context, instance


def _response(status_code=200, payload=None, json_error=False):
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    if json_error:
        response.json.side_effect = ValueError("not json")
    else:
        response.json.return_value = payload if payload is not None else {}
    return response


class TestCostStatusRoute:
    def test_relays_status_payload(self, client):
        context, instance = _async_client_returning(
            _response(200, {"opencost_healthy": True, "region": "us-east-1"})
        )
        with patch("gco.services.api_routes.cost.httpx.AsyncClient", return_value=context):
            result = client.get("/api/v1/cost/status")
        assert result.status_code == 200
        assert result.json()["opencost_healthy"] is True
        url = instance.get.call_args.args[0]
        assert url == ("http://cost-monitor.gco-system.svc.cluster.local/internal/status")

    def test_connection_failure_maps_to_503_with_guidance(self, client):
        context, _ = _async_client_returning(error=httpx.ConnectError("refused"))
        with patch("gco.services.api_routes.cost.httpx.AsyncClient", return_value=context):
            result = client.get("/api/v1/cost/status")
        assert result.status_code == 503
        assert "cost_monitoring" in result.json()["detail"]

    def test_env_var_overrides_the_service_url(self, client, monkeypatch):
        monkeypatch.setenv("COST_MONITOR_URL", "http://localhost:9999/")
        context, instance = _async_client_returning(_response(200, {"ok": True}))
        with patch("gco.services.api_routes.cost.httpx.AsyncClient", return_value=context):
            client.get("/api/v1/cost/status")
        assert instance.get.call_args.args[0] == "http://localhost:9999/internal/status"


class TestListReportsRoute:
    def test_relays_query_params(self, client):
        context, instance = _async_client_returning(_response(200, {"count": 0, "reports": []}))
        with patch("gco.services.api_routes.cost.httpx.AsyncClient", return_value=context):
            result = client.get("/api/v1/cost/reports", params={"adhoc": "true", "limit": 7})
        assert result.status_code == 200
        params = instance.get.call_args.kwargs["params"]
        assert params == {"adhoc": "true", "limit": 7}

    def test_propagates_cost_monitor_error_status(self, client):
        context, _ = _async_client_returning(
            _response(502, {"detail": "Failed to list reports: s3 down"})
        )
        with patch("gco.services.api_routes.cost.httpx.AsyncClient", return_value=context):
            result = client.get("/api/v1/cost/reports")
        assert result.status_code == 502
        assert "s3 down" in result.json()["detail"]

    def test_non_json_body_maps_to_502(self, client):
        context, _ = _async_client_returning(_response(200, json_error=True))
        with patch("gco.services.api_routes.cost.httpx.AsyncClient", return_value=context):
            result = client.get("/api/v1/cost/reports")
        assert result.status_code == 502
        assert "non-JSON" in result.json()["detail"]

    def test_non_object_body_maps_to_502(self, client):
        context, _ = _async_client_returning(_response(200, payload=[1, 2, 3]))
        with patch("gco.services.api_routes.cost.httpx.AsyncClient", return_value=context):
            result = client.get("/api/v1/cost/reports")
        assert result.status_code == 502
        assert "non-object" in result.json()["detail"]

    def test_rejects_invalid_limit_before_proxying(self, client):
        with patch("gco.services.api_routes.cost.httpx.AsyncClient") as mock_client:
            result = client.get("/api/v1/cost/reports", params={"limit": 0})
        assert result.status_code == 422
        mock_client.assert_not_called()


class TestGenerateReportRoute:
    def test_posts_body_and_returns_201(self, client):
        context, instance = _async_client_returning(
            _response(
                201,
                {
                    "region": "us-east-1",
                    "bucket": "bucket-x",
                    "report": {"s3_key": "adhoc/x.parquet", "row_count": 3},
                },
            )
        )
        with patch("gco.services.api_routes.cost.httpx.AsyncClient", return_value=context):
            result = client.post(
                "/api/v1/cost/reports", json={"window_hours": 48, "include_rows": True}
            )
        assert result.status_code == 201
        assert result.json()["report"]["row_count"] == 3
        assert "timestamp" in result.json()
        sent = instance.post.call_args.kwargs["json"]
        assert sent == {"window_hours": 48, "include_rows": True}

    def test_rejects_out_of_range_window_before_proxying(self, client):
        with patch("gco.services.api_routes.cost.httpx.AsyncClient") as mock_client:
            result = client.post("/api/v1/cost/reports", json={"window_hours": 999})
        assert result.status_code == 422
        mock_client.assert_not_called()

    def test_opencost_outage_propagates_as_503(self, client):
        context, _ = _async_client_returning(_response(503, {"detail": "OpenCost request failed"}))
        with patch("gco.services.api_routes.cost.httpx.AsyncClient", return_value=context):
            result = client.post("/api/v1/cost/reports", json={})
        assert result.status_code == 503
        assert "OpenCost" in result.json()["detail"]

    def test_connection_failure_maps_to_503(self, client):
        context, _ = _async_client_returning(error=httpx.ReadTimeout("slow"))
        with patch("gco.services.api_routes.cost.httpx.AsyncClient", return_value=context):
            result = client.post("/api/v1/cost/reports", json={})
        assert result.status_code == 503


class TestRouterRegistration:
    def test_cost_router_is_mounted_on_the_manifest_api(self):
        from gco.services.manifest_api import app as manifest_app

        paths = set(manifest_app.openapi()["paths"])
        assert "/api/v1/cost/status" in paths
        assert "/api/v1/cost/reports" in paths
