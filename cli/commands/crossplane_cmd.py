"""GCO Crossplane command group — the self-managed Crossplane and its Crossview dashboard.

Provides the ``gco crossplane`` sub-commands:

* ``status`` — what cdk.json configures (``helm.crossplane``): enabled, the
  pinned Crossplane and Crossview charts, and the composition functions GCO
  installs.
* ``open`` — ``kubectl port-forward`` to the Crossview dashboard over the
  PRIVATE EKS API endpoint (``--via-ssm`` tunnels through an SSM-managed
  instance), like ``gco monitoring open``.
* ``screenshot`` — a headless capture of the dashboard (full-page PNG).

See docs/CROSSPLANE.md. In-cluster access always goes through the private API
endpoint — there is no public dashboard ingress.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click

from .. import cluster_ui
from ..config import GCOConfig
from ..output import get_output_formatter

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)


@click.group()
@pass_config
def crossplane(config: Any) -> None:
    """Self-managed Crossplane (cdk.json helm.crossplane) and its Crossview dashboard."""


@crossplane.command("status")
@pass_config
def crossplane_status(config: Any) -> None:
    """Show the configured Crossplane: toggle, chart pins and composition functions.

    Reads cdk.json (helm.crossplane), charts.yaml and the shipped post-Helm
    manifest only — no AWS or cluster access.

    Examples:
        gco crossplane status
        gco crossplane status --output json
    """
    from ..crossplane import crossplane_status as _crossplane_status

    formatter = get_output_formatter(config)
    try:
        status = _crossplane_status()
    except (RuntimeError, ValueError) as exc:
        formatter.print_error(f"Failed to read helm.crossplane from cdk.json: {exc}")
        sys.exit(1)
    formatter.print(status)
    if not status["enabled"]:
        formatter.print_info(
            "Crossplane is off (cdk.json helm.crossplane.enabled); enable it and run "
            "'gco stacks deploy' (or deploy once with '--enable crossplane')."
        )


@crossplane.command("open")
@cluster_ui.tunnel_options
@click.option("--local-port", type=int, help="Local port to bind (default 3001).", default=None)
@pass_config
def crossplane_open(
    config: Any,
    region: str | None,
    via_ssm: str | None,
    bastion_ttl_minutes: int,
    assume_yes: bool,
    local_port: int | None,
) -> None:
    """Port-forward to the Crossview dashboard over the private EKS endpoint.

    Runs in the foreground; press Ctrl-C to stop. The dashboard runs without a
    login: the port-forward is the access control.

    Examples:
        gco crossplane open --via-ssm auto -y
        gco crossplane open -r us-west-2 --local-port 13001 --via-ssm i-0123456789abcdef0
    """
    from ..cluster_tunnel import open_api_server_tunnel, resolve_region
    from ..crossplane import DEFAULT_LOCAL_PORT, PORT_FORWARD_TARGET
    from ..kubectl_helpers import build_port_forward_command, update_kubeconfig

    formatter = get_output_formatter(config)
    target_region = resolve_region(config, region)
    cluster = f"{config.project_name}-{target_region}"
    bind_port = local_port or DEFAULT_LOCAL_PORT
    namespace, target, remote_port = PORT_FORWARD_TARGET
    try:
        update_kubeconfig(cluster, target_region)
        with open_api_server_tunnel(
            formatter,
            cluster=cluster,
            region=target_region,
            via_ssm=via_ssm,
            bastion_ttl_minutes=bastion_ttl_minutes,
            assume_yes=assume_yes,
        ) as session:
            cmd = build_port_forward_command(
                namespace,
                target,
                bind_port,
                remote_port,
                server=session.server,
                tls_server_name=session.tls_server_name,
            )
            formatter.print_success(
                f"Forwarding Crossview → http://localhost:{bind_port} (Ctrl-C to stop)"
            )
            try:
                cluster_ui.exec_port_forward(cmd)
            except KeyboardInterrupt:  # pragma: no cover - interactive Ctrl-C
                return
    except (RuntimeError, ValueError) as exc:
        formatter.print_error(str(exc))
        sys.exit(1)


@crossplane.command("screenshot")
@cluster_ui.tunnel_options
@click.option(
    "--output",
    "-o",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="PNG to write (default: ./crossview-dashboard.png).",
)
@click.option("--local-port", type=int, help="Local port to bind (default 3001).", default=None)
@click.option("--headed", is_flag=True, help="Show the browser window while capturing.")
@pass_config
def crossplane_screenshot(
    config: Any,
    region: str | None,
    via_ssm: str | None,
    bastion_ttl_minutes: int,
    assume_yes: bool,
    output_path: Path | None,
    local_port: int | None,
    headed: bool,
) -> None:
    """Capture the Crossview dashboard as a full-page PNG.

    Forwards the dashboard in the background and screenshots its landing
    page. Needs Playwright: pip install 'gco[diagrams]' && playwright install
    chromium.

    Examples:
        gco crossplane screenshot --via-ssm auto -y
        gco crossplane screenshot --output images/crossview-dashboard.png
    """
    from ..cluster_tunnel import open_api_server_tunnel, resolve_region
    from ..crossplane import (
        DEFAULT_LOCAL_PORT,
        DEFAULT_SCREENSHOT_FILENAME,
        PORT_FORWARD_TARGET,
        capture_dashboard_screenshot,
    )
    from ..kubectl_helpers import build_port_forward_command, update_kubeconfig

    formatter = get_output_formatter(config)
    target_region = resolve_region(config, region)
    cluster = f"{config.project_name}-{target_region}"
    bind_port = local_port or DEFAULT_LOCAL_PORT
    output = output_path or Path.cwd() / DEFAULT_SCREENSHOT_FILENAME
    namespace, target, remote_port = PORT_FORWARD_TARGET
    try:
        update_kubeconfig(cluster, target_region)
        with open_api_server_tunnel(
            formatter,
            cluster=cluster,
            region=target_region,
            via_ssm=via_ssm,
            bastion_ttl_minutes=bastion_ttl_minutes,
            assume_yes=assume_yes,
        ) as session:
            cmd = build_port_forward_command(
                namespace,
                target,
                bind_port,
                remote_port,
                server=session.server,
                tls_server_name=session.tls_server_name,
            )
            with cluster_ui.background_port_forward(cmd, bind_port):
                written = capture_dashboard_screenshot(
                    f"http://localhost:{bind_port}", output, headless=not headed
                )
    except (RuntimeError, ValueError) as exc:
        formatter.print_error(str(exc))
        sys.exit(1)
    formatter.print_success(f"Saved the Crossview dashboard to {written}")
    formatter.print({"region": target_region, "output": str(written)})
