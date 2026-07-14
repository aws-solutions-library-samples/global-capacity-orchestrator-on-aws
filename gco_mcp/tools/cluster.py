"""Cluster connectivity MCP tools (thin wrapper over `gco cluster tunnel`).

``cluster_tunnel_command`` is ``safe`` / read-only: it resolves how to reach a
cluster's (possibly private) EKS API endpoint and returns the ready-to-run
``aws ssm start-session`` tunnel command plus the ``kubectl`` flags to use with
it. There is intentionally no tool that *holds a tunnel open* — an SSM tunnel is
an interactive, long-running process, not a request/response operation (the same
reason there is no ``monitoring open`` tool). An agent calls this to learn *how*
to connect, then the operator (or the ``gco cluster tunnel`` CLI) runs it.
"""

import asyncio

import cli_runner
from audit import audit_logged
from server import mcp


@mcp.tool(tags={"safe", "cluster"})
@audit_logged
async def cluster_tunnel_command(
    region: str | None = None,
    instance_id: str | None = None,
    local_port: int = 8443,
) -> str:
    """`gco cluster tunnel --print` — connection plan for a cluster's API endpoint.

    Returns a JSON connection plan: the `aws ssm start-session` command that
    tunnels to the (possibly private) EKS API endpoint and the
    `kubectl --server/--tls-server-name` flags to use through it. Read-only — it
    resolves the endpoint and builds commands; it does not open a tunnel or
    launch any instance.

    Args:
        region: AWS region of the target cluster (e.g. us-east-1). When omitted,
            resolves to the first cdk.json regional entry.
        instance_id: Optional SSM-managed instance id to tunnel through. When
            omitted, the plan includes a command template with an <INSTANCE_ID>
            placeholder — use `gco cluster tunnel --via-ssm auto` in the CLI to
            auto-provision a self-terminating ephemeral bastion instead.
        local_port: Local port to bind for the API tunnel (default 8443).
    """
    args = ["cluster", "tunnel", "--print", "--local-port", str(local_port)]
    if region:
        args += ["--region", region]
    if instance_id:
        args += ["--via-ssm", instance_id]
    return await asyncio.to_thread(cli_runner._run_cli, *args)
