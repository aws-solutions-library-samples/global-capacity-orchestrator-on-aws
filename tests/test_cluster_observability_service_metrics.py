"""Tests for the GCO in-cluster service Prometheus instrumentation.

Covers gco/services/service_metrics: the FastAPI ``/metrics`` mount + request
instrumentation used by the health-monitor and manifest-processor, and the
scrape-time collector used by the loop-based inference monitor. Also confirms
the two FastAPI services actually register the ``/metrics`` route at import.
"""

from __future__ import annotations

import pytest

from gco.services.service_metrics import _CallableCollector, mount_metrics


def test_mount_metrics_exposes_prometheus_endpoint() -> None:
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    app = fastapi.FastAPI()

    @app.get("/ping")
    def _ping() -> dict[str, bool]:
        return {"ok": True}

    mount_metrics(app, "test-svc")
    client = TestClient(app)

    assert client.get("/ping").status_code == 200
    resp = client.get("/metrics")
    assert resp.status_code == 200
    body = resp.text
    # Liveness marker + request instrumentation are exposed in Prometheus text.
    assert 'gco_service_info{service="test-svc"}' in body
    assert "gco_http_requests_total" in body
    # The instrumented /ping GET is counted.
    assert 'method="GET"' in body


def test_callable_collector_exports_numeric_and_bool_metrics() -> None:
    def fake_metrics() -> dict[str, object]:
        return {
            "cluster_id": "gco-us-east-1",
            "region": "us-east-1",
            "running": True,
            "reconcile_count": 7,
            "errors_count": 2,
        }

    families = list(_CallableCollector("inference-monitor", fake_metrics).collect())
    names = {family.name for family in families}
    assert "gco_service_info" in names

    metric_family = next(f for f in families if f.name == "gco_inference_monitor_metric")
    samples = {s.labels["name"]: s.value for s in metric_family.samples}
    assert samples["reconcile_count"] == 7.0
    assert samples["errors_count"] == 2.0
    assert samples["running"] == 1.0  # bool exported as 1/0
    # Non-numeric fields (ids, regions) are not exported as gauges.
    assert "cluster_id" not in samples
    assert "region" not in samples


def test_callable_collector_survives_metrics_fn_error() -> None:
    def boom() -> dict[str, object]:
        raise RuntimeError("scrape must not crash the service")

    # A failing metrics_fn must never break a scrape — the info series still emits.
    families = list(_CallableCollector("inference-monitor", boom).collect())
    assert "gco_service_info" in {family.name for family in families}


@pytest.mark.parametrize("module_name", ["gco.services.health_api", "gco.services.manifest_api"])
def test_fastapi_services_register_metrics_route(module_name: str) -> None:
    module = pytest.importorskip(module_name)
    paths = {getattr(route, "path", None) for route in module.app.routes}
    assert "/metrics" in paths


def test_start_metrics_server_registers_collector_and_serves(monkeypatch) -> None:
    import prometheus_client

    import gco.services.service_metrics as sm

    registered: list[object] = []
    served: list[int] = []
    monkeypatch.setattr(prometheus_client.REGISTRY, "register", registered.append)
    monkeypatch.setattr(prometheus_client, "start_http_server", served.append)

    sm.start_metrics_server(9099, "inference-monitor", lambda: {"reconcile_count": 1})

    assert served == [9099]
    assert len(registered) == 1
    assert isinstance(registered[0], sm._CallableCollector)
