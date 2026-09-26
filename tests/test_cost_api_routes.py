"""
Tests for gco/services/api_routes/cost.py — the /api/v1/cost/* proxy router.

The router relays authenticated requests to the internal cost-monitor
service over verified HTTPS. These tests mount the router alone (no auth
middleware; the proxy logic is transport-independent) and answer its calls
from an in-memory ``httpx2.MockTransport`` swapped in at the tracing seam
every client passes through, so the real client, request building and error
mapping all run. They prove: happy GET/POST relays against the https default,
the verifying transport the router builds (internal-CA context, no env trust),
error-status propagation from the cost monitor, connection failures and a
missing internal CA both mapping to the clear 503 (the disabled-feature
answer), non-JSON and non-object bodies mapping to 502, and the
COST_MONITOR_URL override (an http override needs no CA bundle).
"""

from __future__ import annotations

import json
import logging
import ssl
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gco.services import internal_tls, tracing
from gco.services.api_routes import cost as cost_routes
from gco.services.api_routes.cost import router
from gco.services.internal_tls import InternalTLSError

DEFAULT_BASE = "https://cost-monitor.gco-system.svc.cluster.local:8443"
# Captured before any test swaps the constructor for a spy.
_REAL_ASYNC_TRANSPORT = httpx2.AsyncHTTPTransport


class _CostMonitorDouble:
    """In-memory cost monitor behind the router's real httpx2 client.

    Records every URL whose trust was resolved, the keyword arguments of every
    transport the router built, each transport handed to the tracing seam,
    and each request that reached the (mock) wire.
    """

    def __init__(self) -> None:
        self.context = ssl.create_default_context()
        self.verified_urls: list[str] = []
        self.transport_kwargs: list[dict[str, Any]] = []
        self.wrapped: list[httpx2.AsyncBaseTransport] = []
        self.requests: list[httpx2.Request] = []
        self.handler: Callable[[httpx2.Request], httpx2.Response] = lambda _request: (
            httpx2.Response(200, json={})
        )

    def verify_for_url(self, url: str, ca_file: object = None) -> ssl.SSLContext:
        self.verified_urls.append(url)
        return self.context

    def wrap(self, transport: httpx2.AsyncBaseTransport) -> httpx2.AsyncBaseTransport:
        self.wrapped.append(transport)

        def handle(request: httpx2.Request) -> httpx2.Response:
            self.requests.append(request)
            return self.handler(request)

        return httpx2.MockTransport(handle)

    def respond(self, status_code: int = 200, **kwargs: Any) -> None:
        self.handler = lambda _request: httpx2.Response(status_code, **kwargs)

    def fail(self, error_type: type[httpx2.TransportError], message: str) -> None:
        def raise_error(request: httpx2.Request) -> httpx2.Response:
            raise error_type(message, request=request)

        self.handler = raise_error


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> _CostMonitorDouble:
    double = _CostMonitorDouble()

    def transport_spy(**kwargs: Any) -> httpx2.AsyncHTTPTransport:
        double.transport_kwargs.append(kwargs)
        return _REAL_ASYNC_TRANSPORT(**kwargs)

    monkeypatch.delenv("COST_MONITOR_URL", raising=False)
    monkeypatch.setattr(internal_tls, "verify_for_url", double.verify_for_url)
    monkeypatch.setattr(tracing, "wrap_async_transport", double.wrap)
    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", transport_spy)
    return double


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


class TestCostStatusRoute:
    def test_relays_status_payload_over_verified_https(self, client, upstream):
        upstream.respond(200, json={"opencost_healthy": True, "region": "us-east-1"})

        result = client.get("/api/v1/cost/status")

        assert result.status_code == 200
        assert result.json()["opencost_healthy"] is True
        [request] = upstream.requests
        assert request.method == "GET"
        assert str(request.url) == f"{DEFAULT_BASE}/internal/status"
        assert upstream.verified_urls == [f"{DEFAULT_BASE}/internal/status"]
        # The verifying context rides on the transport (a client given
        # transport= ignores its own verify), and env trust never applies.
        [transport_kwargs] = upstream.transport_kwargs
        assert transport_kwargs["verify"] is upstream.context
        assert transport_kwargs["trust_env"] is False
        [wrapped] = upstream.wrapped
        assert isinstance(wrapped, _REAL_ASYNC_TRANSPORT)
        assert request.extensions["timeout"] == {
            "connect": 30.0,
            "read": 30.0,
            "write": 30.0,
            "pool": 30.0,
        }

    def test_connection_failure_maps_to_503_with_guidance(self, client, upstream, caplog):
        upstream.fail(httpx2.ConnectError, "refused")

        with caplog.at_level(logging.WARNING, logger=cost_routes.__name__):
            result = client.get("/api/v1/cost/status")

        assert result.status_code == 503
        assert "cost_monitoring" in result.json()["detail"]
        assert "Cost monitor unreachable" in caplog.text

    def test_missing_internal_ca_fails_closed_with_the_same_503(
        self, client, upstream, monkeypatch, caplog
    ):
        def untrusted(url: str, ca_file: object = None) -> ssl.SSLContext:
            raise InternalTLSError("GCO internal CA bundle is not readable")

        monkeypatch.setattr(internal_tls, "verify_for_url", untrusted)

        with caplog.at_level(logging.ERROR, logger=cost_routes.__name__):
            result = client.get("/api/v1/cost/status")

        assert result.status_code == 503
        assert "cost_monitoring" in result.json()["detail"]
        assert "TLS trust is unavailable" in caplog.text
        # Fail closed: nothing was built, nothing left the process.
        assert upstream.transport_kwargs == []
        assert upstream.requests == []

    def test_env_var_overrides_the_service_url(self, client, upstream, monkeypatch):
        monkeypatch.setenv("COST_MONITOR_URL", "http://localhost:9999/")
        upstream.respond(200, json={"ok": True})

        client.get("/api/v1/cost/status")

        assert str(upstream.requests[0].url) == "http://localhost:9999/internal/status"
        assert upstream.verified_urls == ["http://localhost:9999/internal/status"]

    def test_http_override_needs_no_ca_bundle(self, client, monkeypatch, tmp_path):
        """A plain-http local/CI override opens no TLS session, so no CA is read."""
        seen: list[httpx2.Request] = []

        def wrap(_transport: httpx2.AsyncBaseTransport) -> httpx2.AsyncBaseTransport:
            def handle(request: httpx2.Request) -> httpx2.Response:
                seen.append(request)
                return httpx2.Response(200, json={"ok": True})

            return httpx2.MockTransport(handle)

        monkeypatch.setenv("COST_MONITOR_URL", "http://127.0.0.1:8080")
        monkeypatch.setenv("GCO_INTERNAL_CA_FILE", str(tmp_path / "absent-ca.crt"))
        monkeypatch.setattr(tracing, "wrap_async_transport", wrap)

        result = client.get("/api/v1/cost/status")

        assert result.status_code == 200
        assert str(seen[0].url) == "http://127.0.0.1:8080/internal/status"

    def test_https_default_without_a_ca_bundle_answers_503(self, client, monkeypatch, tmp_path):
        """The real trust lookup fails closed when the projected CA is absent."""
        monkeypatch.delenv("COST_MONITOR_URL", raising=False)
        monkeypatch.setenv("GCO_INTERNAL_CA_FILE", str(tmp_path / "absent-ca.crt"))

        result = client.get("/api/v1/cost/status")

        assert result.status_code == 503


class TestListReportsRoute:
    def test_relays_query_params(self, client, upstream):
        upstream.respond(200, json={"count": 0, "reports": []})

        result = client.get("/api/v1/cost/reports", params={"adhoc": "true", "limit": 7})

        assert result.status_code == 200
        [request] = upstream.requests
        assert request.url.path == "/internal/reports"
        assert dict(request.url.params) == {"adhoc": "true", "limit": "7"}

    def test_propagates_cost_monitor_error_status(self, client, upstream):
        upstream.respond(502, json={"detail": "Failed to list reports: s3 down"})

        result = client.get("/api/v1/cost/reports")

        assert result.status_code == 502
        assert "s3 down" in result.json()["detail"]

    def test_error_status_without_detail_uses_a_generic_message(self, client, upstream):
        upstream.respond(500, json=["not", "an", "object"])

        result = client.get("/api/v1/cost/reports")

        assert result.status_code == 500
        assert result.json()["detail"] == "Cost monitor request failed"

    def test_non_json_body_maps_to_502(self, client, upstream):
        upstream.respond(200, content=b"<html>not json</html>")

        result = client.get("/api/v1/cost/reports")

        assert result.status_code == 502
        assert "non-JSON" in result.json()["detail"]

    def test_non_object_body_maps_to_502(self, client, upstream):
        upstream.respond(200, json=[1, 2, 3])

        result = client.get("/api/v1/cost/reports")

        assert result.status_code == 502
        assert "non-object" in result.json()["detail"]

    def test_rejects_invalid_limit_before_proxying(self, client, upstream):
        result = client.get("/api/v1/cost/reports", params={"limit": 0})

        assert result.status_code == 422
        assert upstream.transport_kwargs == []
        assert upstream.requests == []


class TestGenerateReportRoute:
    def test_posts_body_and_returns_201(self, client, upstream):
        upstream.respond(
            201,
            json={
                "region": "us-east-1",
                "bucket": "bucket-x",
                "report": {"s3_key": "adhoc/x.parquet", "row_count": 3},
            },
        )

        result = client.post(
            "/api/v1/cost/reports", json={"window_hours": 48, "include_rows": True}
        )

        assert result.status_code == 201
        assert result.json()["report"]["row_count"] == 3
        assert "timestamp" in result.json()
        [request] = upstream.requests
        assert request.method == "POST"
        assert str(request.url) == f"{DEFAULT_BASE}/internal/reports"
        assert json.loads(request.read()) == {"window_hours": 48, "include_rows": True}
        # Report generation gets the longer budget.
        assert request.extensions["timeout"]["read"] == 120.0

    def test_keeps_the_cost_monitor_timestamp_when_present(self, client, upstream):
        upstream.respond(201, json={"timestamp": "2026-07-26T10:00:00+00:00"})

        result = client.post("/api/v1/cost/reports", json={})

        assert result.json()["timestamp"] == "2026-07-26T10:00:00+00:00"

    def test_rejects_out_of_range_window_before_proxying(self, client, upstream):
        result = client.post("/api/v1/cost/reports", json={"window_hours": 999})

        assert result.status_code == 422
        assert upstream.requests == []

    def test_opencost_outage_propagates_as_503(self, client, upstream):
        upstream.respond(503, json={"detail": "OpenCost request failed"})

        result = client.post("/api/v1/cost/reports", json={})

        assert result.status_code == 503
        assert "OpenCost" in result.json()["detail"]

    def test_connection_failure_maps_to_503(self, client, upstream):
        upstream.fail(httpx2.ReadTimeout, "slow")

        result = client.post("/api/v1/cost/reports", json={})

        assert result.status_code == 503
        assert "cost_monitoring" in result.json()["detail"]


class TestRouterRegistration:
    def test_cost_router_is_mounted_on_the_manifest_api(self):
        from gco.services.manifest_api import app as manifest_app

        paths = set(manifest_app.openapi()["paths"])
        assert "/api/v1/cost/status" in paths
        assert "/api/v1/cost/reports" in paths
