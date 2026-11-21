"""Runtime contract shared by the platform services and their pod manifests.

Two things the manifests promise about the processes inside them, pinned
against the code that has to keep the promise:

* **Drain budgets.** Every FastAPI service forwards
  ``GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS`` to Uvicorn's
  ``timeout_graceful_shutdown``, and the value each manifest injects equals
  the module default, sits below the pod's ``terminationGracePeriodSeconds``
  once the preStop sleep is added, and (where a TLS sidecar exists) matches
  the sidecar's own drain budget. A pod is only as graceful as its shortest
  budget, so the four numbers are checked together.
* **Loop staleness.** The inference monitor has no request path, so its
  probes hit the Prometheus endpoint; a loop that is wedged rather than
  crashed only shows in the exported ``seconds_since_last_pass`` and
  ``seconds_since_last_progress`` gauges. They exist from construction and
  reset on every completed iteration, and the liveness probe the manifest
  runs fails once progress is older than the exported
  ``liveness_window_seconds`` (the stall deadline plus one Lease duration).
* **Observable fast path.** The service images ship uvloop and httptools for
  uvicorn's ``auto`` selection. Each entrypoint keeps the ``auto`` defaults
  (so a checkout without the wheels still runs) and logs the implementations
  uvicorn actually resolved, read from the objects uvicorn itself consults.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from gco.services import cost_api, health_api, inference_api, manifest_api
from gco.services.uvicorn_runtime import describe_uvicorn_runtime

_MANIFEST_DIR = Path(__file__).parent.parent / "lambda" / "kubectl-applier-simple" / "manifests"

# (manifest, application container, entrypoint module) for every FastAPI service.
_FASTAPI_SERVICES = [
    ("30-health-monitor.yaml", "health-monitor", health_api),
    ("31-manifest-processor.yaml", "manifest-processor", manifest_api),
    ("33-inference-proxy.yaml", "inference-proxy", inference_api),
    ("34-cost-monitor.yaml", "cost-monitor", cost_api),
]


def _deployment(filename: str) -> dict:
    text = (_MANIFEST_DIR / filename).read_text(encoding="utf-8")
    rendered = re.sub(r"\{\{[A-Z0-9_]+\}\}", "test-value", text)
    documents = [doc for doc in yaml.safe_load_all(rendered) if doc]
    return next(doc for doc in documents if doc["kind"] == "Deployment")


def _env(container: dict) -> dict[str, str | None]:
    return {item["name"]: item.get("value") for item in container.get("env", [])}


def _pre_stop_seconds(container: dict) -> int:
    code = container["lifecycle"]["preStop"]["exec"]["command"][2]
    match = re.fullmatch(r"import time; time\.sleep\((\d+)\)", code)
    assert match is not None, f"{container['name']} preStop is not the bounded python sleep"
    return int(match.group(1))


@pytest.mark.parametrize(("filename", "app", "module"), _FASTAPI_SERVICES)
def test_run_server_forwards_the_manifest_drain_budget_to_uvicorn(filename, app, module):
    """Each entrypoint passes GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS as the Uvicorn drain."""
    default = module.DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS
    with (
        patch.dict(
            "os.environ",
            {"GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS": str(default + 1), "PORT": "8080"},
        ),
        patch("uvicorn.run") as mock_run,
    ):
        module._run_server()

    kwargs = mock_run.call_args.kwargs
    assert mock_run.call_args.args == (f"gco.services.{module.__name__.rsplit('.', 1)[1]}:app",)
    assert kwargs["timeout_graceful_shutdown"] == default + 1
    assert kwargs["port"] == 8080
    assert kwargs["reload"] is False
    # The loop and HTTP parser stay on uvicorn's ``auto`` selection: the images
    # provide uvloop/httptools, a bare checkout may not, and both must serve.
    assert "loop" not in kwargs
    assert "http" not in kwargs


@pytest.mark.parametrize(("filename", "app", "module"), _FASTAPI_SERVICES)
def test_run_server_logs_the_resolved_uvicorn_loop_and_http_parser(filename, app, module, caplog):
    """The startup line names the implementations uvicorn's auto mode resolved to."""
    with (
        patch("uvicorn.run"),
        patch(
            "gco.services.uvicorn_runtime.describe_uvicorn_runtime",
            return_value=("fake.loop_factory", "fake.HttpProtocol"),
        ),
        caplog.at_level(logging.INFO, logger=module.__name__),
    ):
        module._run_server()

    startup_lines = [record.getMessage() for record in caplog.records if "Starting" in record.msg]
    assert startup_lines, caplog.text
    assert "uvicorn loop=fake.loop_factory http=fake.HttpProtocol" in startup_lines[-1]


def test_describe_uvicorn_runtime_reports_what_uvicorn_auto_mode_consults():
    """The names come from uvicorn's own auto modules, so the log cannot disagree with the server."""
    from uvicorn.loops.auto import auto_loop_factory
    from uvicorn.protocols.http.auto import AutoHTTPProtocol

    loop_impl, http_impl = describe_uvicorn_runtime()

    factory = auto_loop_factory()
    assert loop_impl == f"{factory.__module__}.{factory.__qualname__}"
    assert http_impl == f"{AutoHTTPProtocol.__module__}.{AutoHTTPProtocol.__qualname__}"
    # The lock installs the image wheels, so the test environment resolves the
    # same fast path the images do.
    assert loop_impl == "uvloop.new_event_loop"
    assert http_impl == "uvicorn.protocols.http.httptools_impl.HttpToolsProtocol"


@pytest.mark.parametrize(("filename", "app", "module"), _FASTAPI_SERVICES)
def test_run_server_default_matches_the_manifest_budget(filename, app, module):
    """Unset in the environment, Uvicorn gets the same budget the manifest injects."""
    with (
        patch.dict("os.environ", {}, clear=False),
        patch("uvicorn.run") as mock_run,
    ):
        import os

        os.environ.pop("GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS", None)
        module._run_server()

    assert (
        mock_run.call_args.kwargs["timeout_graceful_shutdown"]
        == module.DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS
    )


@pytest.mark.parametrize(("filename", "app", "module"), _FASTAPI_SERVICES)
def test_manifest_drain_budget_fits_the_pod_grace_period(filename, app, module):
    """preStop + Uvicorn drain < terminationGracePeriodSeconds, and the sidecar agrees."""
    pod_spec = _deployment(filename)["spec"]["template"]["spec"]
    containers = {container["name"]: container for container in pod_spec["containers"]}
    application = containers[app]

    budget = _env(application)["GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS"]
    assert budget is not None, (
        f"{filename}: {app} does not inject GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS"
    )
    assert int(budget) == module.DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS, (
        f"{filename}: manifest budget {budget} drifted from "
        f"{module.__name__}.DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS"
    )
    grace = pod_spec["terminationGracePeriodSeconds"]
    assert _pre_stop_seconds(application) + int(budget) < grace, (
        f"{filename}: preStop + drain must fit inside terminationGracePeriodSeconds={grace}"
    )

    sidecar = containers.get("api-tls-proxy")
    if sidecar is not None:
        assert _env(sidecar)["GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS"] == budget, (
            f"{filename}: the TLS sidecar drains for a different budget than the application"
        )


def _monitor(reconcile_interval: int = 5):
    from gco.services.inference_monitor import InferenceMonitor

    with (
        patch("gco.services.inference_monitor.config.load_incluster_config"),
        patch("gco.services.inference_monitor.client.AppsV1Api"),
        patch("gco.services.inference_monitor.client.CoreV1Api"),
        patch("gco.services.inference_monitor.client.NetworkingV1Api"),
        patch("gco.services.inference_monitor.client.AutoscalingV2Api"),
    ):
        return InferenceMonitor(
            cluster_id="test-cluster",
            region="us-east-1",
            store=MagicMock(),
            namespace="gco-inference",
            reconcile_interval=reconcile_interval,
        )


def test_inference_monitor_exports_loop_staleness_from_construction():
    """The gauge is present before the first iteration and measures from construction."""
    # Patch the module's own ``time`` name, not time.monotonic globally: asyncio
    # and pytest keep the real clock.
    with patch("gco.services.inference_monitor.time") as fake_time:
        fake_time.monotonic.side_effect = [1000.0, 1007.5]
        monitor = _monitor()
        metrics = monitor.get_metrics()

    assert metrics["seconds_since_last_pass"] == pytest.approx(7.5)
    assert set(metrics) >= {"running", "reconcile_count", "errors_count", "seconds_since_last_pass"}


def test_inference_monitor_resets_loop_staleness_after_each_iteration():
    """A completed iteration — leader or standby — re-stamps the gauge."""
    monitor = _monitor()
    monitor._try_acquire_lease = MagicMock(return_value=False)
    clock = iter([250.0, 251.0])

    async def stop_after_first_pause():
        monitor._running = False

    with (
        patch("gco.services.inference_monitor.time") as fake_time,
        patch.object(monitor, "_pause_between_passes", side_effect=stop_after_first_pause),
    ):
        fake_time.monotonic.side_effect = lambda: next(clock)
        monitor._last_loop_completed_at = 100.0
        asyncio.run(monitor.start())
        # start() stamped 250.0 after the standby iteration; the scrape at
        # 251.0 sees one second of staleness instead of 151.
        assert monitor.get_metrics()["seconds_since_last_pass"] == pytest.approx(1.0)


def _monitor_container(probe: str | None = None) -> dict:
    pod = _deployment("32-inference-monitor.yaml")["spec"]["template"]["spec"]
    return next(c for c in pod["containers"] if c["name"] == "inference-monitor")


def _manifest_reconcile_interval() -> int:
    return int(_env(_monitor_container())["RECONCILE_INTERVAL_SECONDS"] or "")


@contextlib.contextmanager
def _metrics_served(monitor) -> Iterator[int]:
    """Serve ``monitor``'s gauges as the deployed monitor does; yield the port."""
    import prometheus_client

    from gco.services.service_metrics import start_metrics_server

    started = []
    real_start = prometheus_client.start_http_server

    def start(*args, **kwargs):
        started.append(real_start(*args, **kwargs))
        return started[-1]

    with patch("prometheus_client.start_http_server", side_effect=start):
        start_metrics_server(0, "inference-monitor", monitor.get_metrics, host="127.0.0.1")
    [(server, thread)] = started
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _liveness_exit_code(port: int) -> int:
    """Run the manifest's liveness command against the listener on ``port``."""
    command = _monitor_container()["livenessProbe"]["exec"]["command"]
    source = command[-1].replace("('127.0.0.1',9090)", f"('127.0.0.1',{port})")
    assert source != command[-1], "the probe no longer dials 127.0.0.1:9090"
    assert command[0] == "python"
    completed = subprocess.run(
        [sys.executable, *command[1:-1], source], capture_output=True, timeout=30, check=False
    )
    return completed.returncode


def test_inference_monitor_liveness_window_is_the_stall_deadline_plus_one_lease():
    """The window the probe enforces derives from the code's constants."""
    from gco.services.inference_monitor import LEASE_DURATION_INTERVALS, PASS_STALL_INTERVALS

    interval = _manifest_reconcile_interval()
    monitor = _monitor(reconcile_interval=interval)

    window = monitor.get_metrics()["liveness_window_seconds"]

    assert window == (PASS_STALL_INTERVALS + LEASE_DURATION_INTERVALS) * interval == 195
    assert window > monitor._pass_stall_seconds


@pytest.mark.parametrize(
    ("since_iteration", "since_pass_step", "live"),
    [
        (5.0, 10_000.0, True),
        (400.0, 5.0, True),
        (194.0, 194.0, True),
        (200.0, 200.0, False),
    ],
    ids=["standby", "long-pass-still-progressing", "inside-window", "wedged"],
)
def test_inference_monitor_liveness_probe_fails_only_once_progress_stops(
    since_iteration: float, since_pass_step: float, live: bool
):
    """Liveness follows loop progress, so a standby never fails it."""
    monitor = _monitor(reconcile_interval=_manifest_reconcile_interval())
    now = time.monotonic()
    monitor._last_loop_completed_at = now - since_iteration
    monitor._pass_progress_at = now - since_pass_step

    with _metrics_served(monitor) as port:
        exit_code = _liveness_exit_code(port)

    assert (exit_code == 0) is live


def test_inference_monitor_liveness_probe_fails_without_the_progress_gauges():
    monitor = _monitor(reconcile_interval=_manifest_reconcile_interval())

    with (
        patch.object(monitor, "get_metrics", return_value={"running": True}),
        _metrics_served(monitor) as port,
    ):
        exit_code = _liveness_exit_code(port)

    assert exit_code != 0


def test_inference_monitor_progress_gauge_counts_from_the_latest_pass_step():
    with patch("gco.services.inference_monitor.time") as fake_time:
        fake_time.monotonic.side_effect = [1000.0, 1100.0]
        monitor = _monitor()
        monitor._pass_progress_at = 1090.0
        metrics = monitor.get_metrics()

    assert metrics["seconds_since_last_pass"] == pytest.approx(100.0)
    assert metrics["seconds_since_last_progress"] == pytest.approx(10.0)
