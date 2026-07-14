"""GCO cluster observability command group.

Provides the ``gco monitoring`` sub-commands:

* ``enable`` / ``disable`` / ``status`` — flip the
  ``cluster_observability.enabled`` toggle in ``cdk.json`` (on by default).
* ``open`` — ``kubectl port-forward`` to Grafana / Prometheus / Alertmanager
  over the PRIVATE EKS API endpoint. Because the endpoint is private by
  default, ``open`` detects that posture and can tunnel to the API through an
  SSM-managed instance (``--via-ssm``) so the forward works from a laptop.
* ``users`` — manage Grafana users via the admin HTTP API (see
  :mod:`cli.monitoring_user_mgmt`; wired in a follow-up command module).

The Click wiring mirrors ``analytics_cmd.py``. In-cluster access always goes
through the private API endpoint — there is no public Grafana ingress.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import click
import requests

from ..config import GCOConfig
from ..output import get_output_formatter

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)

# kube-prometheus-stack service names (release name = chart key) and their
# service ports, plus a sensible default local port for each.
_SERVICES: dict[str, dict[str, Any]] = {
    "grafana": {
        "target": "svc/kube-prometheus-stack-grafana",
        "remote_port": 80,
        "default_local_port": 3000,
    },
    "prometheus": {
        "target": "svc/kube-prometheus-stack-prometheus",
        "remote_port": 9090,
        "default_local_port": 9090,
    },
    "alertmanager": {
        "target": "svc/kube-prometheus-stack-alertmanager",
        "remote_port": 9093,
        "default_local_port": 9093,
    },
}

_MONITORING_NAMESPACE = "monitoring"
_GRAFANA_SECRET = "kube-prometheus-stack-grafana"

# Local port kubectl uses to reach the API server through the SSM tunnel.
_SSM_API_LOCAL_PORT = 8443


@click.group()
@pass_config
def monitoring(config: Any) -> None:
    """Manage in-cluster observability (Prometheus + Grafana + Alertmanager)."""


# ---------------------------------------------------------------------------
# Toggle commands — status / enable / disable
# ---------------------------------------------------------------------------


@monitoring.command("status")
@pass_config
def monitoring_status(config: Any) -> None:
    """Show the current cluster observability toggle state from cdk.json."""
    from ..stacks import get_cluster_observability_config

    formatter = get_output_formatter(config)
    try:
        current = get_cluster_observability_config()
        formatter.print_info("Cluster observability config:")
        formatter.print(current)
    except Exception as exc:  # noqa: BLE001 — surface every loader error
        formatter.print_error(f"Failed to read cluster observability config: {exc}")
        sys.exit(1)


@monitoring.command("enable")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation.")
@pass_config
def monitoring_enable(config: Any, yes: bool) -> None:
    """Enable cluster observability in cdk.json (installs kube-prometheus-stack).

    Observability is on by default; use this to re-enable after a
    ``gco monitoring disable``. Prints the follow-up deploy command — does not
    deploy automatically.
    """
    from ..stacks import update_cluster_observability_config

    formatter = get_output_formatter(config)
    if not yes:
        formatter.print_info(
            "Cluster observability (kube-prometheus-stack) will be enabled on every region."
        )
        click.confirm("\nEnable cluster observability?", abort=True)

    try:
        update_cluster_observability_config({"enabled": True})
        formatter.print_success("Cluster observability enabled in cdk.json")
        formatter.print_info(
            f"Run `gco stacks deploy {config.project_name}-<region>` (or deploy-all) to apply"
        )
    except Exception as exc:  # noqa: BLE001 — user-facing error from file I/O
        formatter.print_error(f"Failed to enable cluster observability: {exc}")
        sys.exit(1)


@monitoring.command("disable")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation.")
@pass_config
def monitoring_disable(config: Any, yes: bool) -> None:
    """Disable cluster observability in cdk.json.

    Flips ``cluster_observability.enabled`` to ``false``; the grafana /
    prometheus / alertmanager sub-blocks (sizes, retention, rotation schedule)
    are left untouched so preferences survive a disable/enable cycle. The
    in-cluster stack is removed on the next deploy.
    """
    from ..stacks import update_cluster_observability_config

    formatter = get_output_formatter(config)
    if not yes:
        formatter.print_warning("This will disable cluster observability.")
        formatter.print_warning(
            "Prometheus/Grafana/Alertmanager and their EBS volumes are removed on next deploy."
        )
        click.confirm("Are you sure?", abort=True)

    try:
        update_cluster_observability_config({"enabled": False})
        formatter.print_success("Cluster observability disabled in cdk.json")
        formatter.print_info(
            f"Run `gco stacks deploy {config.project_name}-<region>` (or deploy-all) to apply"
        )
    except Exception as exc:  # noqa: BLE001 — user-facing error from file I/O
        formatter.print_error(f"Failed to disable cluster observability: {exc}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# open — port-forward (private endpoint aware)
# ---------------------------------------------------------------------------


def _private_endpoint_guidance(cluster: str, region: str) -> str:
    """Actionable message when the API endpoint is private and no tunnel is set up."""
    return (
        f"Cluster {cluster!r} has a PRIVATE API endpoint, so kubectl cannot reach it from "
        "outside the VPC. Either:\n"
        "  - re-run with `--via-ssm <instance-id>` to tunnel through an SSM-managed instance "
        "in the VPC, or\n"
        "  - connect over a VPN / bastion / AWS SSM session first (see docs/MONITORING.md), or\n"
        f"  - set eks_cluster.endpoint_access to PUBLIC_AND_PRIVATE and redeploy {region}.\n"
        "Attempting the port-forward anyway in case you already have VPC connectivity."
    )


def _resolve_region(config: Any, region: str | None) -> str:
    """Pick the target region: explicit flag, else first cdk.json regional, else default."""
    if region:
        return str(region)
    from ..config import _load_cdk_json

    cdk = _load_cdk_json()
    if isinstance(cdk, dict) and cdk.get("regional"):
        return str(cdk["regional"][0])
    return str(config.default_region or "us-east-1")


@monitoring.command("open")
@click.option(
    "--service",
    type=click.Choice(sorted(_SERVICES)),
    default="grafana",
    show_default=True,
    help="Which component to port-forward.",
)
@click.option("--region", help="Cluster region (defaults to the first cdk.json regional entry).")
@click.option("--local-port", type=int, help="Local port to bind (defaults per-service).")
@click.option(
    "--via-ssm",
    "via_ssm",
    metavar="INSTANCE_ID",
    help="Tunnel to the private API endpoint through this SSM-managed instance id.",
)
@pass_config
def monitoring_open(
    config: Any,
    service: str,
    region: str | None,
    local_port: int | None,
    via_ssm: str | None,
) -> None:
    """Port-forward to a monitoring component over the private EKS endpoint.

    Runs in the foreground; press Ctrl-C to stop. On a private-endpoint cluster
    (the default) pass ``--via-ssm <instance-id>`` to tunnel to the API server
    through an SSM-managed instance in the VPC.
    """
    from ..kubectl_helpers import (
        build_port_forward_command,
        describe_cluster_access,
        update_kubeconfig,
    )

    formatter = get_output_formatter(config)
    svc = _SERVICES[service]
    target_region = _resolve_region(config, region)
    cluster = f"{config.project_name}-{target_region}"
    bind_port = local_port or svc["default_local_port"]

    try:
        update_kubeconfig(cluster, target_region)
    except (RuntimeError, ValueError) as exc:
        formatter.print_error(str(exc))
        sys.exit(1)

    # Endpoint posture drives whether a plain port-forward can reach the API.
    server: str | None = None
    tls_server_name: str | None = None
    tunnel: subprocess.Popen[bytes] | None = None
    try:
        access = describe_cluster_access(cluster, target_region)
    except RuntimeError as exc:
        formatter.print_warning(f"Could not determine endpoint access mode: {exc}")
        access = {"public": True, "endpoint": ""}

    if not access.get("public"):
        if via_ssm:
            from ..ssm_tunnel import endpoint_host, start_api_tunnel

            endpoint = str(access.get("endpoint") or "")
            try:
                formatter.print_info(
                    f"Opening SSM tunnel to the private API endpoint via {via_ssm}..."
                )
                tunnel = start_api_tunnel(via_ssm, endpoint, _SSM_API_LOCAL_PORT, target_region)
                server = f"https://localhost:{_SSM_API_LOCAL_PORT}"
                tls_server_name = endpoint_host(endpoint)
            except (RuntimeError, ValueError) as exc:
                formatter.print_error(str(exc))
                sys.exit(1)
        else:
            formatter.print_warning(_private_endpoint_guidance(cluster, target_region))

    try:
        cmd = build_port_forward_command(
            _MONITORING_NAMESPACE,
            svc["target"],
            bind_port,
            svc["remote_port"],
            server=server,
            tls_server_name=tls_server_name,
        )
    except ValueError as exc:
        formatter.print_error(str(exc))
        if tunnel is not None:
            tunnel.terminate()
        sys.exit(1)

    url = f"http://localhost:{bind_port}"
    formatter.print_success(f"Forwarding {service} → {url} (Ctrl-C to stop)")
    if service == "grafana":
        formatter.print_info(
            "Log in with the Grafana admin credential from the "
            f"{_GRAFANA_SECRET} Secret (monitoring namespace)."
        )
    try:
        _exec_port_forward(cmd)
    except KeyboardInterrupt:  # pragma: no cover - interactive Ctrl-C
        pass
    finally:
        if tunnel is not None:
            tunnel.terminate()


def _exec_port_forward(cmd: list[str]) -> None:
    """Run the (validated) kubectl port-forward argv in the foreground."""
    subprocess.run(
        cmd, check=False
    )  # nosemgrep: dangerous-subprocess-use-audit - argv built by build_port_forward_command; list form, no shell=True


# ---------------------------------------------------------------------------
# users subgroup — Grafana native users over the admin HTTP API
# ---------------------------------------------------------------------------


def _grafana_conn_options(func: Any) -> Any:
    """Shared --grafana-url / --admin-user / --admin-password options."""
    from ..monitoring_user_mgmt import DEFAULT_GRAFANA_URL

    func = click.option(
        "--grafana-url",
        default=DEFAULT_GRAFANA_URL,
        show_default=True,
        help="Grafana base URL (reachable via `gco monitoring open`).",
    )(func)
    func = click.option(
        "--admin-user",
        help="Grafana admin username (default: read from the Grafana Secret).",
    )(func)
    func = click.option(
        "--admin-password",
        envvar="GCO_GRAFANA_ADMIN_PASSWORD",
        help=(
            "Grafana admin password (also $GCO_GRAFANA_ADMIN_PASSWORD; "
            "default: read from the Grafana Secret)."
        ),
    )(func)
    return func


def _resolve_grafana_auth(admin_user: str | None, admin_password: str | None) -> tuple[str, str]:
    """Return ``(user, password)`` from the flags, else from the Grafana Secret."""
    if admin_password:
        return (admin_user or "admin", admin_password)
    from ..monitoring_user_mgmt import read_grafana_admin_credentials

    return read_grafana_admin_credentials()


@monitoring.group("users")
@pass_config
def users_cmd(config: Any) -> None:
    """Manage Grafana users via the admin API (over `gco monitoring open`)."""


@users_cmd.command("add")
@click.option("--username", required=True, help="Grafana login for the new user.")
@click.option("--email", help="Email address for the new user (optional).")
@click.option("--password", help="Set this password. Mutually exclusive with --generate-password.")
@click.option(
    "--generate-password",
    is_flag=True,
    help="Generate a strong random password and print it once.",
)
@_grafana_conn_options
@pass_config
def users_add(
    config: Any,
    username: str,
    email: str | None,
    password: str | None,
    generate_password: bool,
    grafana_url: str,
    admin_user: str | None,
    admin_password: str | None,
) -> None:
    """Create a Grafana user via the admin HTTP API."""
    from ..monitoring_user_mgmt import create_user
    from ..monitoring_user_mgmt import generate_password as _gen

    formatter = get_output_formatter(config)
    if password and generate_password:
        formatter.print_error("--password and --generate-password are mutually exclusive")
        sys.exit(1)
    if not password and not generate_password:
        formatter.print_error("Pass --password or --generate-password")
        sys.exit(1)

    final_password = password or _gen()
    try:
        auth = _resolve_grafana_auth(admin_user, admin_password)
        user_id = create_user(
            grafana_url, auth, login=username, password=final_password, email=email
        )
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        formatter.print_error(f"Failed to create Grafana user {username!r}: {exc}")
        sys.exit(1)

    formatter.print_success(f"Created Grafana user {username!r} (id={user_id})")
    if generate_password:
        formatter.print_info(f"Generated password (printed exactly once): {final_password}")


@users_cmd.command("list")
@click.option("--as-json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
@_grafana_conn_options
@pass_config
def users_list(
    config: Any,
    as_json: bool,
    grafana_url: str,
    admin_user: str | None,
    admin_password: str | None,
) -> None:
    """List Grafana organisation users."""
    from ..monitoring_user_mgmt import list_users

    formatter = get_output_formatter(config)
    try:
        auth = _resolve_grafana_auth(admin_user, admin_password)
        users = list_users(grafana_url, auth)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        formatter.print_error(f"Failed to list Grafana users: {exc}")
        sys.exit(1)

    if as_json:
        import json

        print(json.dumps(users, indent=2))
        return
    formatter.print(users)


@users_cmd.command("remove")
@click.option("--username", required=True, help="Grafana login/email to remove.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@_grafana_conn_options
@pass_config
def users_remove(
    config: Any,
    username: str,
    yes: bool,
    grafana_url: str,
    admin_user: str | None,
    admin_password: str | None,
) -> None:
    """Delete a Grafana user by login or email."""
    from ..monitoring_user_mgmt import delete_user, lookup_user_id

    formatter = get_output_formatter(config)
    if not yes:
        click.confirm(f"Delete Grafana user '{username}'?", abort=True)

    try:
        auth = _resolve_grafana_auth(admin_user, admin_password)
        user_id = lookup_user_id(grafana_url, auth, username)
        delete_user(grafana_url, auth, user_id)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        formatter.print_error(f"Failed to remove Grafana user {username!r}: {exc}")
        sys.exit(1)

    formatter.print_success(f"Deleted Grafana user {username!r}")
