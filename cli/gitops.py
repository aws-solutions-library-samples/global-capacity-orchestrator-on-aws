"""The self-managed Argo CD behind ``gco gitops`` and the ``gitops_status`` MCP tool.

GCO installs Argo CD into each regional cluster from the upstream chart when
``cdk.json`` ``helm.argocd.enabled`` is true (see ``gco/argocd_config.py`` and
``docs/GITOPS.md``). This module holds the command logic:

* :func:`build_status` — what cdk.json says about Argo CD (enabled, the pinned
  chart, the fence, the GitOps hand-off rendered per Region, the repo-server
  size or autoscaler) plus how to reach the UI. Pure: reads cdk.json and
  charts.yaml only, so it works anywhere.
* :func:`read_admin_password` — the generated ``admin`` password from
  ``argocd-initial-admin-secret`` (through the tunnelled kubectl session).
* :func:`create_session_token` / :func:`capture_argocd_screenshot` — log in to
  the Argo CD API over the port-forward and screenshot the Applications view
  with the session cookie, so a capture needs no human.

Imports nothing from ``aws_cdk``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gco.argocd_config import (
    ARGOCD_ADMIN_USERNAME,
    ARGOCD_CHART_NAME,
    ARGOCD_HELM_KEY,
    ARGOCD_INITIAL_ADMIN_SECRET,
    ARGOCD_NAMESPACE,
    ARGOCD_REPO_SERVER_DEPLOYMENT,
    ARGOCD_SERVER_PORT,
    ARGOCD_SERVER_SERVICE,
    GITOPS_DEFAULT_NAMESPACE,
    GITOPS_PROJECT_NAME,
    GITOPS_ROOT_APPLICATION_NAME,
    GITOPS_TENANT_NAMESPACES,
    gitops_enabled,
    render_gitops_path,
    validate_argocd_config,
)

from . import cluster_ui

#: Default local port for ``gco gitops open`` / ``screenshot`` (Argo CD's
#: customary local port; Grafana keeps 3000, MLflow 5000).
DEFAULT_LOCAL_PORT = 8080

#: Default screenshot filename; ``docs/GITOPS.md`` embeds ``images/argocd-ui.png``.
DEFAULT_SCREENSHOT_FILENAME = "argocd-ui.png"

#: The Argo CD UI route a capture lands on.
APPLICATIONS_PATH = "/applications"

#: Cookie the Argo CD UI reads its session token from.
SESSION_COOKIE = "argocd.token"


def load_argocd_config(cdk_json_path: Path | None = None) -> dict[str, Any]:
    """The validated ``helm.argocd`` block of cdk.json (defaults filled in).

    Raises ``RuntimeError`` when cdk.json is missing and
    ``gco.argocd_config.ArgoCdConfigError`` (a ``ValueError``) when the block
    is malformed — the same rules the CDK app applies.
    """
    context = cluster_ui.load_cdk_context(cdk_json_path)
    helm = context.get("helm")
    return validate_argocd_config(helm.get(ARGOCD_HELM_KEY) if isinstance(helm, dict) else None)


def build_status(
    config: Mapping[str, Any],
    *,
    regions: list[str],
    project_name: str,
    chart: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The configured Argo CD for ``regions`` as one document.

    ``gitops.paths`` renders the per-cluster path placeholders for every
    Region, so an operator sees exactly which directory each cluster syncs.
    ``enabled`` is the cdk.json toggle; a run-scoped
    ``helm_enabled_overrides=argocd`` deploy is not visible here.
    """
    document: dict[str, Any] = {
        "enabled": bool(config["enabled"]),
        "chart": dict(chart) if chart else None,
        "namespace": ARGOCD_NAMESPACE,
        "project": GITOPS_PROJECT_NAME,
        "destinations": list(GITOPS_TENANT_NAMESPACES),
        "source_repos": list(config["source_repos"]),
    }
    if gitops_enabled(config):
        gitops = config["gitops"]
        document["gitops"] = {
            "enabled": True,
            "application": GITOPS_ROOT_APPLICATION_NAME,
            "repo_url": str(gitops["repo_url"]).strip(),
            "revision": str(gitops["revision"]).strip(),
            "path": str(gitops["path"]),
            "paths": {
                region: render_gitops_path(
                    str(gitops["path"]), region=region, cluster_name=f"{project_name}-{region}"
                )
                for region in regions
            },
            "default_namespace": GITOPS_DEFAULT_NAMESPACE,
            "sync_policy": str(gitops["sync_policy"]),
        }
    else:
        document["gitops"] = {"enabled": False}
    repo_server = config["repo_server"]
    autoscaling = repo_server["autoscaling"]
    document["repo_server"] = {
        "deployment": ARGOCD_REPO_SERVER_DEPLOYMENT,
        "replicas": int(repo_server["replicas"]),
        "autoscaling": (
            {
                "enabled": True,
                "min_replicas": int(repo_server["replicas"]),
                "max_replicas": int(autoscaling["max_replicas"]),
                "cpu_target_utilization_percentage": int(
                    autoscaling["cpu_target_utilization_percentage"]
                ),
            }
            if autoscaling["enabled"]
            else {"enabled": False}
        ),
    }
    document["ui"] = {
        "service": f"svc/{ARGOCD_SERVER_SERVICE}",
        "open": "gco gitops open",
        "username": ARGOCD_ADMIN_USERNAME,
        "password": "gco gitops password",  # nosec B105  # the command that prints it, not a credential
    }
    return document


def gitops_status(
    *,
    regions: list[str],
    project_name: str,
    cdk_json_path: Path | None = None,
    charts_yaml: Path | None = None,
) -> dict[str, Any]:
    """:func:`build_status` over the working directory's cdk.json and charts.yaml."""
    return build_status(
        load_argocd_config(cdk_json_path),
        regions=regions,
        project_name=project_name,
        chart=cluster_ui.chart_entry(ARGOCD_CHART_NAME, charts_yaml),
    )


def read_admin_password(*, server: str | None = None, tls_server_name: str | None = None) -> str:
    """The generated ``admin`` password Argo CD wrote on first start.

    Argo CD recommends deleting ``argocd-initial-admin-secret`` once the
    password is changed; a missing Secret is reported with that remedy.
    """
    try:
        return cluster_ui.read_secret_key(
            ARGOCD_NAMESPACE,
            ARGOCD_INITIAL_ADMIN_SECRET,
            "password",
            server=server,
            tls_server_name=tls_server_name,
        )
    except RuntimeError as exc:
        # kubectl reports a missing Secret (or a missing argocd namespace) as
        # "Error from server (NotFound): ..."; "kubectl not found" is not that.
        if "(NotFound)" in str(exc):
            raise RuntimeError(
                f"{ARGOCD_NAMESPACE}/{ARGOCD_INITIAL_ADMIN_SECRET} is gone: the initial admin "
                "password was rotated (or Argo CD is not installed; check 'gco gitops status'). "
                "Use the current admin password."
            ) from exc
        raise


def create_session_token(base_url: str, password: str, *, timeout: float = 30.0) -> str:
    """Log in to the Argo CD API as ``admin`` and return the session token."""
    import requests

    try:
        response = requests.post(
            f"{base_url.rstrip('/')}/api/v1/session",
            json={"username": ARGOCD_ADMIN_USERNAME, "password": password},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not reach the Argo CD API at {base_url}: {exc}") from exc
    if response.status_code != 200:
        raise RuntimeError(
            f"Argo CD refused the admin login ({response.status_code}); "
            "pass the current password with --password"
        )
    token = (response.json() or {}).get("token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("Argo CD returned no session token")
    return token


def capture_argocd_screenshot(
    base_url: str,
    output: Path,
    *,
    token: str,
    headless: bool = True,
) -> Path:
    """Screenshot the Applications view with an authenticated session cookie."""
    base = base_url.rstrip("/")
    return cluster_ui.capture_page(
        f"{base}{APPLICATIONS_PATH}",
        output,
        cookies=[{"name": SESSION_COOKIE, "value": token, "url": base}],
        headless=headless,
    )


#: What ``open`` forwards (namespace, target, remote port).
PORT_FORWARD_TARGET = (ARGOCD_NAMESPACE, f"svc/{ARGOCD_SERVER_SERVICE}", ARGOCD_SERVER_PORT)
