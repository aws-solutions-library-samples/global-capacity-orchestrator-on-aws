"""The inference monitor's metrics listener can be narrowed to loopback.

The deployed monitor sets ``METRICS_HOST=127.0.0.1`` so its plaintext
Prometheus endpoint is reachable only inside the pod; the ``metrics-tls-proxy``
sidecar serves it to Prometheus over verified HTTPS on 9443
(``32-inference-monitor.yaml``). ``start_metrics_server`` therefore takes a
keyword-only ``host`` that reaches ``prometheus_client.start_http_server`` as
its bind address, defaulting to every interface for bare local runs.
"""

from __future__ import annotations

import inspect
import socket
import urllib.request
from typing import Any

import prometheus_client
import pytest

from gco.services import service_metrics


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_host_is_keyword_only_with_an_all_interfaces_default() -> None:
    parameter = inspect.signature(service_metrics.start_metrics_server).parameters["host"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default == "0.0.0.0"


def test_prometheus_client_accepts_the_bind_address_keyword() -> None:
    """Guards the library contract the forwarding relies on."""
    assert "addr" in inspect.signature(prometheus_client.start_http_server).parameters


@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0"])
def test_host_is_forwarded_as_the_bind_address(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    calls: list[dict[str, Any]] = []

    def fake_start_http_server(port: int, **kwargs: Any) -> None:
        calls.append({"port": port, **kwargs})

    monkeypatch.setattr(prometheus_client, "start_http_server", fake_start_http_server)

    service_metrics.start_metrics_server(9090, "inference-monitor", dict, host=host)

    (call,) = calls
    assert call["port"] == 9090
    assert call["addr"] == host
    assert call["registry"] is not prometheus_client.REGISTRY


def test_loopback_listener_serves_the_collector(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real bind on loopback answers the scrape the TLS sidecar forwards."""
    started: list[tuple[Any, Any]] = []
    real_start = prometheus_client.start_http_server

    def recording_start(*args: Any, **kwargs: Any) -> tuple[Any, Any]:
        result = real_start(*args, **kwargs)
        started.append(result)
        return result

    monkeypatch.setattr(prometheus_client, "start_http_server", recording_start)
    port = _free_port()
    service_metrics.start_metrics_server(
        port, "inference-monitor", lambda: {"reconcile_count": 3}, host="127.0.0.1"
    )
    ((server, thread),) = started
    try:
        assert server.server_address[0] == "127.0.0.1"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as response:
            body = response.read().decode("utf-8")
        assert 'gco_inference_monitor_metric{name="reconcile_count"} 3.0' in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
