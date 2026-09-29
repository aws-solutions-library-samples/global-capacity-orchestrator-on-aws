"""Tracing wiring of the four traced GCO API services.

Each service configures the process tracer provider under its own service
name and instruments the FastAPI app it builds, at import time (a service
module runs twice, once as ``python -m`` and once through Uvicorn's import
string, and both builds must be wired), then flushes spans on lifespan
shutdown. The tracing module itself is exercised in ``test_tracing.py``;
these checks replace its entry points with recorders and pin only the calls
the services make.

Import-time wiring is observed by executing a fresh copy of each service
module, so the shared module objects (and the apps other suites hold) are
never rebuilt.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gco.services import tracing

_TRACED_SERVICES = [
    ("gco.services.health_api", "health-monitor"),
    ("gco.services.manifest_api", "manifest-processor"),
    ("gco.services.inference_api", "inference-proxy"),
    ("gco.services.cost_api", "cost-monitor"),
]


def _exec_fresh_copy(source: ModuleType, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Execute ``source``'s file as a new, temporarily registered module."""
    copy_name = f"_tracing_wiring_copy_{source.__name__.replace('.', '_')}"
    spec = importlib.util.spec_from_file_location(copy_name, source.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered for the duration of the test so pydantic and FastAPI resolve
    # the copy's postponed annotations exactly as they do for the real module.
    monkeypatch.setitem(sys.modules, copy_name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("module_name", "service_name"), _TRACED_SERVICES)
def test_service_configures_tracing_then_instruments_its_app(
    module_name: str, service_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Imported before the recorders go in, so only the copy's calls are seen.
    # The names come from this module's own _TRACED_SERVICES constant.
    # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
    source = importlib.import_module(module_name)
    calls: list[tuple[str, Any]] = []

    def configure(name: str) -> bool:
        calls.append(("configure", name))
        return True

    monkeypatch.setattr(tracing, "configure_tracing", configure)
    monkeypatch.setattr(
        tracing, "instrument_fastapi_app", lambda app: calls.append(("instrument", app))
    )

    module = _exec_fresh_copy(source, monkeypatch)

    assert calls == [("configure", service_name), ("instrument", module.app)]


def _recording_shutdown(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    monkeypatch.setattr(tracing, "shutdown_tracing", lambda: events.append("shutdown_tracing"))


async def test_cost_monitor_shutdown_stops_the_reporter_then_flushes_traces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every OpenCost call closes its own client; shutdown has none to close."""
    import gco.services.cost_api as cost_api

    events: list[str] = []
    _recording_shutdown(monkeypatch, events)
    monitor = MagicMock(cluster="gco-us-east-1", region="us-east-1")
    monkeypatch.setattr(cost_api, "create_cost_monitor_from_env", lambda: monitor)
    monkeypatch.setattr(cost_api, "preload_report_writer", lambda: True)
    monkeypatch.setattr(cost_api, "configure_structured_logging", MagicMock())
    monkeypatch.setattr(cost_api, "cost_monitor", None)

    async with cost_api.lifespan(cost_api.app):
        assert cost_api.cost_monitor is monitor
        assert events == []
        loop_task = cost_api.app.state.scheduled_report_task
        # The reporter runs on the instance the lifespan built even when the
        # global moved.
        cost_api.cost_monitor = None

    assert loop_task.done()
    assert events == ["shutdown_tracing"]
    assert monitor.opencost.mock_calls == []


async def test_inference_proxy_shutdown_only_flushes_traces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each request releases its own upstream client; shutdown has none to close."""
    import gco.services.inference_api as inference_api

    events: list[str] = []
    _recording_shutdown(monkeypatch, events)

    async with inference_api.lifespan(inference_api.app):
        assert events == []

    assert events == ["shutdown_tracing"]


async def test_inference_proxy_shutdown_still_flushes_when_the_server_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gco.services.inference_api as inference_api

    events: list[str] = []
    _recording_shutdown(monkeypatch, events)

    with pytest.raises(RuntimeError, match="server crashed"):
        async with inference_api.lifespan(inference_api.app):
            raise RuntimeError("server crashed")

    assert events == ["shutdown_tracing"]


async def test_inference_proxy_app_runs_the_lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
    """The app is built with the lifespan, so the server runs its shutdown."""
    import gco.services.inference_api as inference_api

    shutdown = MagicMock()
    monkeypatch.setattr(tracing, "shutdown_tracing", shutdown)

    async with inference_api.app.router.lifespan_context(inference_api.app):
        shutdown.assert_not_called()

    shutdown.assert_called_once_with()


async def test_health_monitor_shutdown_flushes_traces(monkeypatch: pytest.MonkeyPatch) -> None:
    import gco.services.health_api as health_api

    events: list[str] = []
    _recording_shutdown(monkeypatch, events)
    # The lifespan assigns these module globals; restore them for other suites.
    for name in ("health_monitor", "health_metrics", "webhook_dispatcher", "health_check_task"):
        monkeypatch.setattr(health_api, name, getattr(health_api, name))
    monitor = MagicMock(cluster_id="gco-us-east-1", region="us-east-1")
    monitor.get_health_status = AsyncMock(side_effect=RuntimeError("not polled in this test"))
    dispatcher = MagicMock()
    dispatcher.start = AsyncMock()
    dispatcher.stop = AsyncMock(side_effect=lambda: events.append("webhooks.stop"))

    async def idle_background_monitor() -> None:
        await asyncio.Event().wait()

    with (
        patch.object(health_api, "create_health_monitor_from_env", return_value=monitor),
        patch.object(health_api, "HealthMonitorMetrics"),
        patch.object(health_api, "create_webhook_dispatcher_from_env", return_value=dispatcher),
        patch.object(health_api, "configure_structured_logging"),
        patch.object(health_api, "background_health_monitor", idle_background_monitor),
    ):
        async with health_api.lifespan(health_api.app):
            assert events == []

    assert events == ["webhooks.stop", "shutdown_tracing"]


async def test_manifest_processor_shutdown_flushes_traces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gco.services.manifest_api as manifest_api

    events: list[str] = []
    _recording_shutdown(monkeypatch, events)
    monkeypatch.delenv("CENTRAL_QUEUE_WORKER_ENABLED", raising=False)
    # The lifespan assigns these module globals; restore them for other suites.
    for name in ("manifest_processor", "manifest_metrics", "template_store", "webhook_store"):
        monkeypatch.setattr(manifest_api, name, getattr(manifest_api, name))
    monkeypatch.setattr(manifest_api, "job_store", manifest_api.job_store)
    processor = SimpleNamespace(cluster_id="gco-us-east-1", region="us-east-1")

    with (
        patch.object(manifest_api, "create_manifest_processor_from_env", return_value=processor),
        patch.object(manifest_api, "ManifestProcessorMetrics"),
        patch.object(manifest_api, "configure_structured_logging"),
        patch.object(manifest_api, "get_template_store", return_value=MagicMock()),
        patch.object(manifest_api, "get_webhook_store", return_value=MagicMock()),
        patch.object(manifest_api, "get_job_store", return_value=MagicMock()),
    ):
        async with manifest_api.lifespan(manifest_api.app):
            assert events == []

    assert events == ["shutdown_tracing"]
