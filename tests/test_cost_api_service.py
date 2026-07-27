"""
Tests for gco/services/cost_api.py — the cost-monitor HTTP service.

Exercises the probe endpoints, /internal/status, /internal/reports (list +
ad-hoc generation with error mapping: OpenCost outages to 503, S3 failures
to 502, window validation to 422), readiness coupling to the scheduled
reporter task, and the scheduled report loop's failure isolation. The
CostMonitor is a mock patched into the module global; no lifespan
initialization or AWS access occurs.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import gco.services.cost_api as cost_api_module
from gco.services.cost_api import _scheduled_report_loop, app
from gco.services.cost_monitor import (
    OpenCostUnavailableError,
    ReportResult,
    ReportWriteError,
)


@pytest.fixture
def monitor():
    """Mock CostMonitor the lifespan installs via the patched factory."""
    mock_monitor = MagicMock()
    mock_monitor.region = "us-east-1"
    mock_monitor.cluster = "gco-us-east-1"
    mock_monitor.bucket = "gco-cost-reports-123456789012-us-east-2"
    return mock_monitor


@pytest.fixture
def client(monitor, monkeypatch):
    """TestClient whose lifespan initializes against the mock monitor."""
    monkeypatch.setattr(cost_api_module, "create_cost_monitor_from_env", lambda: monitor)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _result(rows=None) -> ReportResult:
    return ReportResult(
        s3_key="adhoc/region=us-east-1/date=2026-07-26/allocation-x.parquet",
        row_count=2,
        total_cost=3.25,
        window_start="2026-07-25T10:00:00+00:00",
        window_end="2026-07-26T10:00:00+00:00",
        rows=rows or [],
    )


class TestProbes:
    def test_healthz_always_ok(self, client):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_readyz_ready_with_monitor(self, client):
        assert client.get("/readyz").status_code == 200

    def test_readyz_503_without_monitor(self, client, monkeypatch):
        monkeypatch.setattr(cost_api_module, "cost_monitor", None)
        assert client.get("/readyz").status_code == 503

    def test_readyz_503_when_scheduler_task_died(self, client):
        live_task = app.state.scheduled_report_task
        done_task = MagicMock()
        done_task.done.return_value = True
        app.state.scheduled_report_task = done_task
        try:
            response = client.get("/readyz")
        finally:
            app.state.scheduled_report_task = live_task
        assert response.status_code == 503
        assert "stopped" in response.json()["detail"]

    def test_metrics_endpoint_mounted(self, client):
        assert client.get("/metrics").status_code == 200


class TestStatusEndpoint:
    def test_relays_monitor_status(self, client, monitor):
        monitor.status.return_value = {"opencost_healthy": True, "region": "us-east-1"}
        response = client.get("/internal/status")
        assert response.status_code == 200
        assert response.json()["opencost_healthy"] is True

    def test_503_without_monitor(self, client, monkeypatch):
        monkeypatch.setattr(cost_api_module, "cost_monitor", None)
        assert client.get("/internal/status").status_code == 503


class TestListReportsEndpoint:
    def test_lists_with_bounded_query_params(self, client, monitor):
        monitor.list_reports.return_value = [{"key": "reports/x.parquet"}]
        response = client.get("/internal/reports", params={"adhoc": "true", "limit": 5})
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 1
        assert body["region"] == "us-east-1"
        monitor.list_reports.assert_called_once_with(adhoc=True, limit=5)

    def test_rejects_out_of_range_limit(self, client):
        assert client.get("/internal/reports", params={"limit": 0}).status_code == 422

    def test_s3_failure_maps_to_502(self, client, monitor):
        monitor.list_reports.side_effect = RuntimeError("s3 down")
        response = client.get("/internal/reports")
        assert response.status_code == 502


class TestGenerateReportEndpoint:
    def test_generates_with_default_window(self, client, monitor):
        monitor.generate_report.return_value = _result()
        response = client.post("/internal/reports", json={})
        assert response.status_code == 201
        body = response.json()
        assert body["report"]["row_count"] == 2
        assert "rows" not in body
        kwargs = monitor.generate_report.call_args.kwargs
        assert kwargs["adhoc"] is True
        assert kwargs["include_rows"] is False
        args = monitor.generate_report.call_args.args
        assert (args[1] - args[0]).total_seconds() == pytest.approx(24 * 3600)

    def test_includes_rows_when_requested(self, client, monitor):
        monitor.generate_report.return_value = _result(rows=[{"namespace": "gco-jobs"}])
        response = client.post("/internal/reports", json={"window_hours": 2, "include_rows": True})
        assert response.status_code == 201
        assert response.json()["rows"] == [{"namespace": "gco-jobs"}]

    def test_rejects_out_of_range_window(self, client):
        assert client.post("/internal/reports", json={"window_hours": 0}).status_code == 422
        assert client.post("/internal/reports", json={"window_hours": 1_000}).status_code == 422

    def test_opencost_outage_maps_to_503(self, client, monitor):
        monitor.generate_report.side_effect = OpenCostUnavailableError("down")
        assert client.post("/internal/reports", json={}).status_code == 503

    def test_write_failure_maps_to_502(self, client, monitor):
        monitor.generate_report.side_effect = ReportWriteError("denied")
        assert client.post("/internal/reports", json={}).status_code == 502

    def test_window_validation_maps_to_422(self, client, monitor):
        monitor.generate_report.side_effect = ValueError("window_end must be after")
        assert client.post("/internal/reports", json={}).status_code == 422


class TestScheduledReportLoop:
    @pytest.mark.asyncio
    async def test_loop_survives_pass_failures_and_stops_cleanly(self, monkeypatch):
        monkeypatch.setattr(cost_api_module, "_SCHEDULER_TICK_SECONDS", 0.01)
        monitor = MagicMock()
        calls = []

        def run_once():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("transient failure")

        monitor.run_scheduled_once.side_effect = run_once
        stop = asyncio.Event()

        loop_task = asyncio.create_task(_scheduled_report_loop(monitor, stop))
        # Let the loop take at least two passes (one failing, one succeeding).
        for _ in range(200):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(loop_task, timeout=5)

        assert len(calls) >= 2

    @pytest.mark.asyncio
    async def test_loop_exits_immediately_when_pre_stopped(self):
        monitor = MagicMock()
        stop = asyncio.Event()
        stop.set()
        await asyncio.wait_for(_scheduled_report_loop(monitor, stop), timeout=1)
        monitor.run_scheduled_once.assert_not_called()
