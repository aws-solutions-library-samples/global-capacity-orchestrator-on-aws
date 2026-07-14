"""Shared helpers for reaching a cluster's (possibly private) EKS API endpoint.

GCO clusters default to a PRIVATE EKS API endpoint, so ``kubectl`` cannot reach
the API server from outside the VPC. Two CLI commands need the same machinery to
bridge that gap:

* ``gco monitoring open`` — port-forward Grafana/Prometheus over the API server;
* ``gco cluster tunnel`` — a general SSM tunnel so plain ``kubectl`` works.

Rather than duplicate "detect endpoint posture → (optionally provision an
ephemeral bastion) → open an SSM tunnel → tear everything down", both commands
share this module:

* :class:`TunnelPlan` + :func:`resolve_tunnel_plan` — a pure, request/response
  friendly description of how to reach the endpoint (the ``aws ssm start-session``
  command and the ``kubectl`` flags). This is what ``--print`` and the MCP
  ``cluster_tunnel_command`` tool return.
* :func:`provision_bastion` / :func:`teardown_bastion` — the ephemeral-bastion
  lifecycle with a confirmation prompt and an orphan-check hint on failure.
* :func:`open_api_server_tunnel` — a context manager that ties it together and
  guarantees teardown (including when setup fails before yielding).

``describe_cluster_access`` and ``start_api_tunnel`` are reached via their
modules (not ``from ... import``) so tests can monkeypatch them at the source.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import click

from . import ephemeral_bastion, kubectl_helpers, ssm_tunnel

# Sentinel for --via-ssm: provision (and later destroy) an ephemeral bastion
# instead of tunnelling through a caller-supplied instance id.
AUTO_BASTION = "auto"

# Local port kubectl uses to reach the API server through the SSM tunnel.
DEFAULT_API_LOCAL_PORT = 8443

# A syntactically valid placeholder instance id, used only to render a copy-paste
# command *template* when the caller hasn't supplied a real instance id.
_PLACEHOLDER_INSTANCE = "i-0123456789abcdef0"


@dataclass(frozen=True)
class TunnelPlan:
    """How to reach a cluster's EKS API endpoint from a laptop.

    Pure data + pure builders — no side effects — so it can back a ``--print``
    CLI mode and an MCP tool that just *describe* the connection.
    """

    cluster: str
    region: str
    endpoint: str
    public: bool
    private: bool
    local_port: int = DEFAULT_API_LOCAL_PORT

    @property
    def endpoint_host(self) -> str:
        """The bare endpoint hostname (kubectl ``--tls-server-name``), or ``""``."""
        try:
            return ssm_tunnel.endpoint_host(self.endpoint)
        except ValueError:
            return ""

    def ssm_command(self, instance_id: str) -> list[str]:
        """The ``aws ssm start-session`` argv that tunnels to the API endpoint."""
        return ssm_tunnel.build_remote_host_port_forward_command(
            instance_id, self.endpoint_host, self.local_port, self.region
        )

    def kubectl_flags(self) -> list[str]:
        """The ``kubectl`` flags that point at the tunnel with the right TLS SNI."""
        return [
            "--server",
            f"https://localhost:{self.local_port}",
            "--tls-server-name",
            self.endpoint_host,
        ]

    def update_kubeconfig_command(self) -> list[str]:
        """The ``aws eks update-kubeconfig`` argv for this cluster."""
        return [
            "aws",
            "eks",
            "update-kubeconfig",
            "--name",
            self.cluster,
            "--region",
            self.region,
        ]

    def as_dict(self, instance_id: str | None = None) -> dict[str, Any]:
        """A JSON-friendly connection plan (what ``--print`` / the MCP tool emit)."""
        data: dict[str, Any] = {
            "cluster": self.cluster,
            "region": self.region,
            "endpoint": self.endpoint,
            "endpoint_host": self.endpoint_host,
            "public": self.public,
            "private": self.private,
            "local_port": self.local_port,
            "update_kubeconfig": self.update_kubeconfig_command(),
        }
        if not self.private:
            data["reachable"] = "direct"
            data["note"] = (
                "The API endpoint is publicly reachable — run `aws eks update-kubeconfig` "
                "then use kubectl directly; no SSM tunnel is required."
            )
            return data

        data["reachable"] = "ssm-tunnel"
        data["kubectl_flags"] = self.kubectl_flags()
        data["kubectl_example"] = "kubectl " + " ".join(self.kubectl_flags()) + " get nodes"
        if instance_id:
            cmd = self.ssm_command(instance_id)
            data["ssm_command"] = cmd
            data["ssm_command_str"] = " ".join(cmd)
            data["note"] = (
                "Run ssm_command_str in one shell to open the tunnel (keep it open), then "
                "use kubectl_flags (or kubectl_example) in another shell."
            )
        else:
            template = " ".join(self.ssm_command(_PLACEHOLDER_INSTANCE)).replace(
                _PLACEHOLDER_INSTANCE, "<INSTANCE_ID>"
            )
            data["ssm_command_template"] = template
            data["note"] = (
                "Private endpoint. Replace <INSTANCE_ID> in ssm_command_template with an "
                "SSM-managed instance in the cluster VPC and run it to open the tunnel "
                "(or run `gco cluster tunnel --via-ssm auto` to auto-provision one), then "
                "use kubectl_flags in another shell."
            )
        return data


def resolve_tunnel_plan(
    cluster: str, region: str, *, local_port: int = DEFAULT_API_LOCAL_PORT
) -> TunnelPlan:
    """Describe how to reach ``cluster``'s API endpoint (raises on lookup failure)."""
    access = kubectl_helpers.describe_cluster_access(cluster, region)
    return TunnelPlan(
        cluster=cluster,
        region=region,
        endpoint=str(access.get("endpoint") or ""),
        public=bool(access.get("public")),
        private=bool(access.get("private")),
        local_port=local_port,
    )


def resolve_region(config: Any, region: str | None) -> str:
    """Pick the target region: explicit flag, else first cdk.json regional, else default."""
    if region:
        return str(region)
    from .config import _load_cdk_json

    cdk = _load_cdk_json()
    if isinstance(cdk, dict) and cdk.get("regional"):
        return str(cdk["regional"][0])
    return str(config.default_region or "us-east-1")


def _project_name_from_cluster(cluster: str, region: str) -> str:
    """Recover the project key from a ``{project_name}-{region}`` cluster name.

    Cluster names are built as ``f"{config.project_name}-{region}"`` (cli/config.py),
    so stripping the ``-{region}`` suffix yields the project key the bastion's IAM
    role / instance profile are scoped to — matching the deployment's other
    project-scoped resources without threading a second config value down here.
    """
    suffix = f"-{region}"
    return cluster[: -len(suffix)] if cluster.endswith(suffix) else cluster


def private_endpoint_guidance(cluster: str, region: str) -> str:
    """Actionable message when the API endpoint is private and no tunnel is set up."""
    return (
        f"Cluster {cluster!r} has a PRIVATE API endpoint, so kubectl cannot reach it from "
        "outside the VPC. Either:\n"
        "  - re-run with `--via-ssm <instance-id>` to tunnel through an SSM-managed instance "
        "in the VPC,\n"
        "  - re-run with `--via-ssm auto` to provision a self-terminating ephemeral bastion,\n"
        "  - connect over a VPN / bastion / AWS SSM session first (see docs/MONITORING.md), or\n"
        f"  - set eks_cluster.endpoint_access to PUBLIC_AND_PRIVATE and redeploy {region}.\n"
        "Attempting to continue in case you already have VPC connectivity."
    )


def provision_bastion(
    formatter: Any,
    cluster: str,
    region: str,
    ttl_minutes: int,
    assume_yes: bool,
    project_name: str = ephemeral_bastion.DEFAULT_PROJECT_NAME,
) -> str:
    """Provision an ephemeral SSM bastion for the tunnel; return its instance id.

    Prompts for confirmation (it launches a billable EC2 instance) unless
    ``assume_yes``. The instance self-terminates after ``ttl_minutes`` and is
    torn down by :func:`teardown_bastion` when the tunnel closes. Its IAM role /
    instance profile are named for ``project_name``.
    """
    if not assume_yes:
        formatter.print_warning(
            f"--via-ssm auto will launch a {ephemeral_bastion.BASTION_INSTANCE_TYPE} ephemeral "
            f"bastion in the {cluster} VPC to reach the private API endpoint. It requires no "
            f"inbound ports (SSM is outbound-only), self-terminates after {ttl_minutes} minutes, "
            "and is torn down automatically when the tunnel closes."
        )
        click.confirm("Provision the ephemeral bastion?", abort=True)

    formatter.print_info(
        f"Provisioning ephemeral SSM bastion in {region} "
        "(waiting for it to register with SSM; this takes a minute)..."
    )
    instance_id = ephemeral_bastion.create_ephemeral_bastion(
        cluster, region, project_name=project_name, ttl_minutes=ttl_minutes
    )
    formatter.print_success(f"Ephemeral bastion {instance_id} is online.")
    return instance_id


def teardown_bastion(
    formatter: Any,
    instance_id: str,
    region: str,
    project_name: str = ephemeral_bastion.DEFAULT_PROJECT_NAME,
) -> None:
    """Tear down an ephemeral bastion; on failure, print the orphan-check command."""
    try:
        formatter.print_info(f"Tearing down ephemeral bastion {instance_id}...")
        ephemeral_bastion.destroy_ephemeral_bastion(instance_id, region, project_name=project_name)
        formatter.print_success(f"Ephemeral bastion {instance_id} terminated.")
    except Exception as exc:  # noqa: BLE001 — never crash teardown; guide the operator
        formatter.print_error(
            f"Failed to tear down bastion {instance_id}: {exc}\n"
            "Check for and terminate any orphan with:\n"
            f"  aws ec2 describe-instances --region {region} "
            "--filters Name=tag:gco:ephemeral,Values=true "
            "Name=instance-state-name,Values=running,pending "
            "--query 'Reservations[].Instances[].InstanceId' --output text"
        )


@dataclass(frozen=True)
class TunnelSession:
    """What :func:`open_api_server_tunnel` yields to the command body."""

    # kubectl --server / --tls-server-name overrides, or None when the endpoint
    # is reachable directly (public) or no tunnel could be established.
    server: str | None
    tls_server_name: str | None
    plan: TunnelPlan
    # True when an SSM tunnel process is running for this session.
    active: bool


@contextmanager
def open_api_server_tunnel(
    formatter: Any,
    *,
    cluster: str,
    region: str,
    via_ssm: str | None,
    local_port: int = DEFAULT_API_LOCAL_PORT,
    bastion_ttl_minutes: int | None = None,
    assume_yes: bool = False,
) -> Iterator[TunnelSession]:
    """Resolve endpoint posture and, when private + ``via_ssm``, open an SSM tunnel.

    Yields a :class:`TunnelSession`. Manages the ephemeral-bastion (``--via-ssm
    auto``) and tunnel lifecycle and tears both down on exit — including when
    setup fails before the ``yield`` (so a failed tunnel never leaks a bastion).
    """
    if bastion_ttl_minutes is None:
        bastion_ttl_minutes = ephemeral_bastion.DEFAULT_TTL_MINUTES
    # The bastion's IAM role/profile are scoped to the deployment's project key,
    # recovered from the cluster name (which is f"{project_name}-{region}").
    project_name = _project_name_from_cluster(cluster, region)

    try:
        plan = resolve_tunnel_plan(cluster, region, local_port=local_port)
    except (RuntimeError, ValueError) as exc:
        # Endpoint posture unknown — fall back to a direct attempt.
        formatter.print_warning(f"Could not determine endpoint access mode: {exc}")
        plan = TunnelPlan(
            cluster=cluster,
            region=region,
            endpoint="",
            public=True,
            private=False,
            local_port=local_port,
        )

    server: str | None = None
    tls_server_name: str | None = None
    tunnel = None
    created_bastion: str | None = None
    try:
        if not plan.public:
            instance_id = via_ssm
            if via_ssm == AUTO_BASTION:
                created_bastion = provision_bastion(
                    formatter, cluster, region, bastion_ttl_minutes, assume_yes, project_name
                )
                instance_id = created_bastion

            if instance_id:
                formatter.print_info(
                    f"Opening SSM tunnel to the private API endpoint via {instance_id}..."
                )
                tunnel = ssm_tunnel.start_api_tunnel(instance_id, plan.endpoint, local_port, region)
                server = f"https://localhost:{local_port}"
                tls_server_name = plan.endpoint_host
            else:
                formatter.print_warning(private_endpoint_guidance(cluster, region))

        yield TunnelSession(
            server=server,
            tls_server_name=tls_server_name,
            plan=plan,
            active=tunnel is not None,
        )
    finally:
        if tunnel is not None:
            tunnel.terminate()
        if created_bastion is not None:
            teardown_bastion(formatter, created_bastion, region, project_name)
