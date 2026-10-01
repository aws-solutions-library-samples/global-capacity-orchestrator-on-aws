"""GCO cluster connectivity commands.

``gco cluster tunnel`` opens an AWS SSM Session Manager tunnel to a cluster's
PRIVATE EKS API endpoint so ``kubectl`` works from a laptop outside the VPC —
optionally provisioning a self-terminating ephemeral bastion (``--via-ssm
auto``). It reuses the same tunnel/bastion machinery as ``gco monitoring open``
(:mod:`cli.cluster_tunnel`).

``--print`` emits the ready-to-run tunnel + ``kubectl`` commands (a connection
plan) instead of opening the tunnel — the request/response form the MCP
``cluster_tunnel_command`` tool returns, and handy for scripting or when you
already have your own SSM instance.
"""

from __future__ import annotations

import sys
import time
from typing import Any

import click

from ..cluster_tunnel import (
    DEFAULT_API_LOCAL_PORT,
    ROUTE_DIRECT,
    open_api_server_tunnel,
    require_private_access,
    resolve_region,
    resolve_tunnel_plan,
)
from ..config import GCOConfig
from ..ephemeral_bastion import DEFAULT_TTL_MINUTES
from ..output import get_output_formatter

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)


@click.group()
@pass_config
def cluster(config: Any) -> None:
    """Cluster connectivity helpers (SSM tunnel to the private EKS API)."""


@cluster.command("tunnel")
@click.option("--region", help="Cluster region (defaults to the first cdk.json regional entry).")
@click.option(
    "--via-ssm",
    "via_ssm",
    metavar="INSTANCE_ID|auto",
    help=(
        "Tunnel through an SSM-managed instance id, or 'auto' to provision a "
        "self-terminating ephemeral bastion and tear it down on exit. Used whenever "
        "private endpoint access is on, PUBLIC_AND_PRIVATE included."
    ),
)
@click.option(
    "--local-port",
    type=int,
    default=DEFAULT_API_LOCAL_PORT,
    show_default=True,
    help="Local port to bind for the API tunnel.",
)
@click.option(
    "--print",
    "print_plan",
    is_flag=True,
    help="Print the tunnel + kubectl commands (connection plan) instead of opening the tunnel.",
)
@click.option(
    "--bastion-ttl-minutes",
    type=int,
    default=DEFAULT_TTL_MINUTES,
    show_default=True,
    help="Self-terminate backstop (minutes) for an `--via-ssm auto` bastion.",
)
@click.option(
    "--yes",
    "-y",
    "assume_yes",
    is_flag=True,
    help="Skip the confirmation prompt when provisioning an `--via-ssm auto` bastion.",
)
@pass_config
def tunnel_cmd(
    config: Any,
    region: str | None,
    via_ssm: str | None,
    local_port: int,
    print_plan: bool,
    bastion_ttl_minutes: int,
    assume_yes: bool,
) -> None:
    """Open (or ``--print``) an SSM tunnel to a cluster's private EKS API endpoint.

    Interactive mode holds the tunnel open in the foreground (Ctrl-C to stop) and
    prints the ``kubectl`` flags to use in another shell. Pass ``--via-ssm
    <instance-id>`` to tunnel through an existing SSM-managed instance, or
    ``--via-ssm auto`` to provision a minimal, self-terminating ephemeral bastion
    for the session. Either is honored whenever private endpoint access is on,
    including a PUBLIC_AND_PRIVATE cluster whose public endpoint this network
    cannot reach. Without ``--via-ssm``, a public endpoint is used directly.
    """
    formatter = get_output_formatter(config)
    target_region = resolve_region(config, region)
    cluster_name = f"{config.project_name}-{target_region}"

    if print_plan:
        _print_connection_plan(config, formatter, cluster_name, target_region, via_ssm, local_port)
        return

    from ..kubectl_helpers import update_kubeconfig

    try:
        update_kubeconfig(cluster_name, target_region)
    except (RuntimeError, ValueError) as exc:
        formatter.print_error(str(exc))
        sys.exit(1)

    try:
        with open_api_server_tunnel(
            formatter,
            cluster=cluster_name,
            region=target_region,
            via_ssm=via_ssm,
            local_port=local_port,
            bastion_ttl_minutes=bastion_ttl_minutes,
            assume_yes=assume_yes,
        ) as session:
            if session.active:
                formatter.print_success(
                    f"SSM tunnel open: {session.server} → {session.plan.endpoint} (Ctrl-C to stop)"
                )
                formatter.print_info(
                    "Run kubectl in another shell with:\n    kubectl "
                    + " ".join(session.plan.kubectl_flags())
                    + " get nodes"
                )
                _block_until_interrupt()
            elif session.route == ROUTE_DIRECT:
                formatter.print_success(
                    f"{cluster_name} has a PUBLIC API endpoint — kubectl reaches it directly "
                    "(after `aws eks update-kubeconfig`); no tunnel needed."
                )
            # A private endpoint with no --via-ssm, or one whose access mode could
            # not be read, was already explained by the context manager; there is
            # nothing to hold open.
    except (RuntimeError, ValueError) as exc:
        formatter.print_error(str(exc))
        sys.exit(1)


def _print_connection_plan(
    config: Any,
    formatter: Any,
    cluster_name: str,
    region: str,
    via_ssm: str | None,
    local_port: int,
) -> None:
    """Resolve and emit the connection plan (JSON under -o json, else commands).

    The plan follows the interactive route rule: ``--via-ssm`` selects the SSM
    tunnel whenever private access is on, and is rejected when it is off.
    """
    try:
        plan = resolve_tunnel_plan(cluster_name, region, local_port=local_port)
        if via_ssm:
            require_private_access(plan, via_ssm)
        payload = plan.as_dict(via_ssm)
    except (RuntimeError, ValueError) as exc:
        formatter.print_error(f"Failed to resolve tunnel plan: {exc}")
        sys.exit(1)

    if config.output_format in ("json", "yaml"):
        formatter.print(payload)
    else:
        _echo_plan_human(payload)


def _echo_plan_human(payload: dict[str, Any]) -> None:
    """Print the connection plan as copy-paste shell commands."""
    header = f"# {payload['cluster']} ({payload['region']})"
    mode = payload["access_mode"]
    lines: list[str] = []
    if payload.get("reachable") == "direct":
        lines.append(f"{header}: {mode} endpoint — kubectl reaches the public endpoint directly.")
        lines.append("# " + payload["note"])
        lines.append(" ".join(payload["update_kubeconfig"]))
    else:
        lines.append(f"{header}: {mode} endpoint — reach the private endpoint over an SSM tunnel.")
        lines.append("# 1) Ensure a kubeconfig context exists:")
        lines.append(" ".join(payload["update_kubeconfig"]))
        lines.append("# 2) Open the tunnel in one shell (keep it running):")
        lines.append(payload.get("ssm_command_str") or payload.get("ssm_command_template", ""))
        lines.append("# 3) In another shell, run kubectl through the tunnel:")
        lines.append(payload["kubectl_example"])
        if payload.get("note"):
            lines.append("# " + payload["note"])
    click.echo("\n".join(lines))


def _block_until_interrupt() -> None:  # pragma: no cover - interactive wait
    """Hold the process (and thus the tunnel) open until Ctrl-C."""
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


_DOCTOR_STATUS_MARKS = {"ok": "✓", "warn": "⚠", "fail": "✗", "unknown": "?"}


@cluster.command("doctor")
@click.option("--region", help="Cluster region (defaults to the first cdk.json regional entry).")
@pass_config
def doctor_cmd(config: Any, region: str | None) -> None:
    """Diagnose EKS API access: reachability, authentication, authorization.

    The three layers fail with misleading and overlapping symptoms — kubectl
    timeouts (endpoint mode or CIDR allowlist), 'Unauthorized' (no EKS access
    entry), RBAC 'Forbidden' (no associated access policy) — and a stale
    kubeconfig context for a destroyed cluster produces the same 'no such
    host' as a private endpoint. Doctor reports each layer separately with
    the remedy for exactly what it found, and exits nonzero if any layer
    fails.
    """
    from .. import cluster_doctor

    formatter = get_output_formatter(config)
    target_region = resolve_region(config, region)
    cluster_name = f"{config.project_name}-{target_region}"

    probe = cluster_doctor.probe_cluster(cluster_name, target_region)
    checks = cluster_doctor.diagnose(probe)

    if config.output_format in ("json", "yaml"):
        formatter.print(
            {
                "cluster": cluster_name,
                "region": target_region,
                "checks": [check.as_dict() for check in checks],
            }
        )
    else:
        click.echo(f"# {cluster_name} ({target_region})")
        for check in checks:
            mark = _DOCTOR_STATUS_MARKS.get(check.status, "?")
            click.echo(f"{mark} [{check.layer}] {check.finding}")
            if check.remedy:
                click.echo(f"    remedy: {check.remedy}")

    if any(check.status == "fail" for check in checks):
        sys.exit(1)
