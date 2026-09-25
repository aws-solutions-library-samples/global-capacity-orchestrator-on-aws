"""The self-managed Crossplane behind ``gco crossplane`` and the ``crossplane_status`` MCP tool.

GCO installs Crossplane and its Crossview dashboard into each regional cluster
from the upstream charts when ``cdk.json`` ``helm.crossplane.enabled`` is true
(see ``docs/CROSSPLANE.md``). This module holds the command logic:

* :func:`build_status` — what cdk.json says (enabled), the pinned charts, the
  composition functions GCO installs (read from the shipped
  ``post-helm-crossplane.yaml``) and how to reach the dashboard. Pure: reads
  cdk.json, charts.yaml and the manifest only, so it works anywhere.
* :func:`capture_dashboard_screenshot` — a headless capture of Crossview over
  the port-forward (the dashboard runs with auth mode none behind it).

Imports nothing from ``aws_cdk``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import cluster_ui

#: ``cdk.json`` ``helm`` block key (and ``helm_enabled_overrides`` name).
CROSSPLANE_HELM_KEY = "crossplane"

#: charts.yaml entries the one toggle installs, in install order.
CROSSPLANE_CHARTS: tuple[str, ...] = ("crossplane", "crossview")

CROSSPLANE_NAMESPACE = "crossplane-system"

#: The GCO-owned post-Helm manifest carrying the composition functions.
CROSSPLANE_MANIFEST = "post-helm-crossplane.yaml"

#: The dashboard Service (chart release ``crossview``) and its port.
DASHBOARD_SERVICE = "crossview-service"
DASHBOARD_PORT = 80

#: Default local port for ``gco crossplane open`` / ``screenshot`` (Crossview's
#: own container port; Grafana keeps 3000, Argo CD 8080).
DEFAULT_LOCAL_PORT = 3001

#: Default screenshot filename; ``docs/CROSSPLANE.md`` embeds
#: ``images/crossview-dashboard.png``.
DEFAULT_SCREENSHOT_FILENAME = "crossview-dashboard.png"

#: What ``open`` forwards (namespace, target, remote port).
PORT_FORWARD_TARGET = (CROSSPLANE_NAMESPACE, f"svc/{DASHBOARD_SERVICE}", DASHBOARD_PORT)


def crossplane_enabled(context: Mapping[str, Any]) -> bool:
    """The ``helm.crossplane.enabled`` toggle (absent means off, like the stack)."""
    helm = context.get("helm")
    block = helm.get(CROSSPLANE_HELM_KEY) if isinstance(helm, Mapping) else None
    if block is None:
        return False
    if not isinstance(block, Mapping) or type(block.get("enabled", False)) is not bool:
        raise ValueError(f"helm.crossplane must be an object with a boolean enabled, got {block!r}")
    return bool(block.get("enabled", False))


def shipped_functions(manifests_dir: Path | None = None) -> list[dict[str, str]]:
    """The ``Function`` packages ``post-helm-crossplane.yaml`` installs."""
    return [
        {
            "name": str((doc.get("metadata") or {}).get("name") or ""),
            "package": str((doc.get("spec") or {}).get("package") or ""),
        }
        for doc in cluster_ui.manifest_documents(CROSSPLANE_MANIFEST, manifests_dir)
        if doc.get("kind") == "Function"
    ]


def build_status(
    *,
    enabled: bool,
    charts: list[dict[str, Any]],
    functions: list[dict[str, str]],
) -> dict[str, Any]:
    """The configured Crossplane as one document."""
    return {
        "enabled": enabled,
        "charts": charts,
        "namespace": CROSSPLANE_NAMESPACE,
        "functions": functions,
        "dashboard": {
            "name": "Crossview",
            "service": f"svc/{DASHBOARD_SERVICE}",
            "open": "gco crossplane open",
            "auth": "none (reachable only through the kubectl port-forward)",
        },
    }


def crossplane_status(
    *,
    cdk_json_path: Path | None = None,
    charts_yaml: Path | None = None,
    manifests_dir: Path | None = None,
) -> dict[str, Any]:
    """:func:`build_status` over the working directory's cdk.json and the shipped files."""
    context = cluster_ui.load_cdk_context(cdk_json_path)
    charts = [
        entry
        for entry in (cluster_ui.chart_entry(name, charts_yaml) for name in CROSSPLANE_CHARTS)
        if entry is not None
    ]
    return build_status(
        enabled=crossplane_enabled(context),
        charts=charts,
        functions=shipped_functions(manifests_dir),
    )


def capture_dashboard_screenshot(base_url: str, output: Path, *, headless: bool = True) -> Path:
    """Screenshot the Crossview dashboard's landing page."""
    return cluster_ui.capture_page(f"{base_url.rstrip('/')}/", output, headless=headless)
