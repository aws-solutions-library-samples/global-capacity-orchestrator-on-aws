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
  crashed only shows in the exported ``seconds_since_last_pass`` gauge. The
  gauge exists from construction and resets on every completed iteration.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from gco.services import cost_api, health_api, inference_api, manifest_api

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


def _monitor():
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
            reconcile_interval=5,
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

    async def stop_after_first_sleep(_interval):
        monitor._running = False

    with (
        patch("gco.services.inference_monitor.time") as fake_time,
        patch("gco.services.inference_monitor.asyncio.sleep", side_effect=stop_after_first_sleep),
    ):
        fake_time.monotonic.side_effect = lambda: next(clock)
        monitor._last_loop_completed_at = 100.0
        asyncio.run(monitor.start())
        # start() stamped 250.0 after the standby iteration; the scrape at
        # 251.0 sees one second of staleness instead of 151.
        assert monitor.get_metrics()["seconds_since_last_pass"] == pytest.approx(1.0)
