"""GCO GitOps command group — the self-managed Argo CD.

Provides the ``gco gitops`` sub-commands:

* ``status`` — what cdk.json configures (``helm.argocd``): enabled, the pinned
  chart, the gco-tenants fence, the GitOps hand-off rendered per Region and
  the repo-server size or autoscaler.
* ``open`` — ``kubectl port-forward`` to the Argo CD UI over the PRIVATE EKS
  API endpoint (``--via-ssm`` tunnels through an SSM-managed instance), like
  ``gco monitoring open``.
* ``password`` — the generated ``admin`` password from
  ``argocd-initial-admin-secret``.
* ``screenshot`` — a headless capture of the Applications view (API login,
  session cookie, full-page PNG).

See docs/GITOPS.md. In-cluster access always goes through the private API
endpoint — there is no public Argo CD ingress.
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
def gitops(config: Any) -> None:
    """Self-managed Argo CD (cdk.json helm.argocd): GitOps into the tenant namespaces."""


@gitops.command("status")
@click.option("--region", "-r", help="Render the GitOps path for this region only.")
@pass_config
def gitops_status(config: Any, region: str | None) -> None:
    """Show the configured Argo CD: toggle, chart pin, fence, GitOps hand-off, repo server.

    Reads cdk.json (helm.argocd) and charts.yaml only — no AWS or cluster
    access. The GitOps path is rendered for every regional deployment region
    (or just --region).

    Examples:
        gco gitops status
        gco gitops status -r us-west-2 --output json
    """
    from ..gitops import gitops_status as _gitops_status

    formatter = get_output_formatter(config)
    try:
        regions = (
            [str(region)] if region else cluster_ui.regional_regions(cluster_ui.load_cdk_context())
        )
        status = _gitops_status(regions=regions, project_name=config.project_name)
    except (RuntimeError, ValueError) as exc:
        formatter.print_error(f"Failed to read helm.argocd from cdk.json: {exc}")
        sys.exit(1)
    formatter.print(status)
    if not status["enabled"]:
        formatter.print_info(
            "Argo CD is off (cdk.json helm.argocd.enabled); enable it and run "
            "'gco stacks deploy' (or deploy once with '--enable argocd')."
        )


@gitops.command("open")
@cluster_ui.tunnel_options
@click.option("--local-port", type=int, help="Local port to bind (default 8080).", default=None)
@pass_config
def gitops_open(
    config: Any,
    region: str | None,
    via_ssm: str | None,
    bastion_ttl_minutes: int,
    assume_yes: bool,
    local_port: int | None,
) -> None:
    """Port-forward to the Argo CD UI over the private EKS endpoint.

    Runs in the foreground; press Ctrl-C to stop. Sign in as admin with the
    password from 'gco gitops password'.

    Examples:
        gco gitops open --via-ssm auto -y
        gco gitops open -r us-west-2 --local-port 18080 --via-ssm i-0123456789abcdef0
    """
    from ..cluster_tunnel import open_api_server_tunnel, resolve_region
    from ..gitops import DEFAULT_LOCAL_PORT, PORT_FORWARD_TARGET
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
                f"Forwarding Argo CD → http://localhost:{bind_port} (Ctrl-C to stop)"
            )
            formatter.print_info(
                "Sign in as admin; 'gco gitops password' prints the generated password."
            )
            try:
                cluster_ui.exec_port_forward(cmd)
            except KeyboardInterrupt:  # pragma: no cover - interactive Ctrl-C
                return
    except (RuntimeError, ValueError) as exc:
        formatter.print_error(str(exc))
        sys.exit(1)


@gitops.command("password")
@cluster_ui.tunnel_options
@pass_config
def gitops_password(
    config: Any,
    region: str | None,
    via_ssm: str | None,
    bastion_ttl_minutes: int,
    assume_yes: bool,
) -> None:
    """Print the generated Argo CD admin password (argocd-initial-admin-secret).

    Argo CD writes it on first start. Once you change the admin password,
    delete that Secret; this command then says so.

    Examples:
        gco gitops password --via-ssm auto -y
    """
    from ..cluster_tunnel import open_api_server_tunnel, resolve_region
    from ..gitops import read_admin_password
    from ..kubectl_helpers import update_kubeconfig

    formatter = get_output_formatter(config)
    target_region = resolve_region(config, region)
    cluster = f"{config.project_name}-{target_region}"
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
            password = read_admin_password(
                server=session.server, tls_server_name=session.tls_server_name
            )
    except (RuntimeError, ValueError) as exc:
        formatter.print_error(str(exc))
        sys.exit(1)
    formatter.print({"region": target_region, "username": "admin", "password": password})


@gitops.command("screenshot")
@cluster_ui.tunnel_options
@click.option(
    "--output",
    "-o",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="PNG to write (default: ./argocd-ui.png).",
)
@click.option("--local-port", type=int, help="Local port to bind (default 8080).", default=None)
@click.option(
    "--password",
    envvar="GCO_ARGOCD_ADMIN_PASSWORD",
    help=(
        "Admin password (also $GCO_ARGOCD_ADMIN_PASSWORD; default: read from "
        "argocd-initial-admin-secret)."
    ),
)
@click.option("--headed", is_flag=True, help="Show the browser window while capturing.")
@pass_config
def gitops_screenshot(
    config: Any,
    region: str | None,
    via_ssm: str | None,
    bastion_ttl_minutes: int,
    assume_yes: bool,
    output_path: Path | None,
    local_port: int | None,
    password: str | None,
    headed: bool,
) -> None:
    """Capture the Argo CD Applications view as a full-page PNG.

    Forwards the UI in the background, logs in to the Argo CD API as admin and
    screenshots /applications with the session cookie — no browser sign-in.
    Needs Playwright: pip install 'gco[diagrams]' && playwright install chromium.

    Examples:
        gco gitops screenshot --via-ssm auto -y
        gco gitops screenshot --output images/argocd-ui.png
    """
    from ..cluster_tunnel import open_api_server_tunnel, resolve_region
    from ..cluster_ui import background_port_forward
    from ..gitops import (
        DEFAULT_LOCAL_PORT,
        DEFAULT_SCREENSHOT_FILENAME,
        PORT_FORWARD_TARGET,
        capture_argocd_screenshot,
        create_session_token,
        read_admin_password,
    )
    from ..kubectl_helpers import build_port_forward_command, update_kubeconfig

    formatter = get_output_formatter(config)
    target_region = resolve_region(config, region)
    cluster = f"{config.project_name}-{target_region}"
    bind_port = local_port or DEFAULT_LOCAL_PORT
    output = output_path or Path.cwd() / DEFAULT_SCREENSHOT_FILENAME
    namespace, target, remote_port = PORT_FORWARD_TARGET
    base_url = f"http://localhost:{bind_port}"
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
            admin_password = password or read_admin_password(
                server=session.server, tls_server_name=session.tls_server_name
            )
            cmd = build_port_forward_command(
                namespace,
                target,
                bind_port,
                remote_port,
                server=session.server,
                tls_server_name=session.tls_server_name,
            )
            with background_port_forward(cmd, bind_port):
                token = create_session_token(base_url, admin_password)
                written = capture_argocd_screenshot(
                    base_url, output, token=token, headless=not headed
                )
    except (RuntimeError, ValueError) as exc:
        formatter.print_error(str(exc))
        sys.exit(1)
    formatter.print_success(f"Saved the Argo CD Applications view to {written}")
    formatter.print({"region": target_region, "output": str(written)})
