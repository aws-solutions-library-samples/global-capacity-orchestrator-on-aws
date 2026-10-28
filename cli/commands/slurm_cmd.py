"""GCO Slurm command group — the Slinky Slurm cluster through the GCO API.

Provides the ``gco slurm`` sub-commands:

* ``status`` / ``partitions`` / ``nodes`` — the cluster as slurmctld sees it
  (``sinfo``), per region.
* ``queue`` / ``job`` / ``submit`` / ``cancel`` — the live queue, one job, an
  ``sbatch`` submission from a local script (its ``#SBATCH`` directives are
  honoured; flags override them) and ``scancel``.
* ``accounting`` — slurmdbd job history (``sacct``) over a time window.
* ``shell`` / ``output`` — an interactive shell on the login pod, or a job's
  output file read from it, over the PRIVATE EKS API endpoint
  (``--via-ssm`` tunnels through an SSM-managed instance, like
  ``gco monitoring open``).

Every API command is pinned to one region (``--region``, else the root
``--region`` / configured default) because each region runs its own Slurm
cluster. See docs/SLURM_OPERATOR.md and docs/API.md#slurm.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click

from .. import cluster_ui
from ..config import GCOConfig
from ..output import confirm, get_output_formatter

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)

_REGION_HELP = "Region whose Slurm cluster to use (default: the root --region / configured default)"


def _slurm_region(config: Any, region: str | None) -> str:
    """The region a Slurm request is pinned to; Slurm clusters are per region."""
    chosen = region or config.default_region
    if not isinstance(chosen, str) or not chosen.strip():
        raise click.UsageError(
            "Slurm commands need a region: pass --region or set one with `gco -r REGION`"
        )
    return chosen.strip()


def _call(config: Any, method: str, path: str, region: str, **kwargs: Any) -> dict[str, Any]:
    from ..aws_client import get_aws_client

    result: dict[str, Any] = get_aws_client(config).call_api(
        method=method, path=path, region=region, **kwargs
    )
    return result


def _fail(formatter: Any, verb: str, exc: Exception) -> None:
    formatter.print_error(f"Failed to {verb}: {exc}")
    sys.exit(1)


@click.group()
@pass_config
def slurm(config: Any) -> None:
    """Slurm cluster (cdk.json helm.slurm): queue, submit, accounting, login shell."""


# ---------------------------------------------------------------------------
# Cluster views
# ---------------------------------------------------------------------------


@slurm.command("status")
@click.option("--region", "-r", help=_REGION_HELP)
@pass_config
def slurm_status(config: Any, region: str | None) -> None:
    """Controller reachability, partitions, node and job counts.

    Examples:
        gco slurm status -r us-east-1
        gco -r us-east-1 slurm status --output json
    """
    from ..slurm import render_status

    formatter = get_output_formatter(config)
    target = _slurm_region(config, region)
    try:
        result = _call(config, "GET", "/api/v1/slurm/status", target)
    except Exception as exc:
        _fail(formatter, "read Slurm status", exc)
    if config.output_format != "table":
        formatter.print(result)
        return
    print(render_status(result, target))


@slurm.command("partitions")
@click.option("--region", "-r", help=_REGION_HELP)
@pass_config
def slurm_partitions(config: Any, region: str | None) -> None:
    """Partitions with their state, time limit and node counts (sinfo).

    Examples:
        gco slurm partitions -r us-east-1
    """
    from ..slurm import render_partitions

    formatter = get_output_formatter(config)
    target = _slurm_region(config, region)
    try:
        result = _call(config, "GET", "/api/v1/slurm/partitions", target)
    except Exception as exc:
        _fail(formatter, "list Slurm partitions", exc)
    if config.output_format != "table":
        formatter.print(result)
        return
    print(render_partitions(result.get("partitions") or [], target))


@slurm.command("nodes")
@click.option("--region", "-r", help=_REGION_HELP)
@click.option("--partition", "-p", help="Only nodes in this partition")
@pass_config
def slurm_nodes(config: Any, region: str | None, partition: str | None) -> None:
    """Slurm nodes (the slurmd worker pods) with state, resources and gres.

    Examples:
        gco slurm nodes -r us-east-1
        gco slurm nodes -r us-east-1 --partition gpu
    """
    from ..slurm import render_nodes

    formatter = get_output_formatter(config)
    target = _slurm_region(config, region)
    try:
        result = _call(
            config, "GET", "/api/v1/slurm/nodes", target, params={"partition": partition}
        )
    except Exception as exc:
        _fail(formatter, "list Slurm nodes", exc)
    if config.output_format != "table":
        formatter.print(result)
        return
    print(render_nodes(result.get("nodes") or [], target))


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@slurm.command("queue")
@click.option("--region", "-r", help=_REGION_HELP)
@click.option("--state", "-s", help="Only jobs in this state (PENDING, RUNNING, ...)")
@click.option("--partition", "-p", help="Only jobs in this partition")
@click.option("--name", "-n", help="Only jobs with exactly this name")
@click.option("--limit", "-l", default=200, type=click.IntRange(1, 5000), help="Maximum jobs")
@pass_config
def slurm_queue(
    config: Any,
    region: str | None,
    state: str | None,
    partition: str | None,
    name: str | None,
    limit: int,
) -> None:
    """The live queue as slurmctld sees it (squeue).

    Examples:
        gco slurm queue -r us-east-1
        gco slurm queue -r us-east-1 --state PENDING --partition gpu
    """
    from ..slurm import render_queue

    formatter = get_output_formatter(config)
    target = _slurm_region(config, region)
    try:
        result = _call(
            config,
            "GET",
            "/api/v1/slurm/jobs",
            target,
            params={
                "state": state.upper() if state else None,
                "partition": partition,
                "name": name,
                "limit": str(limit),
            },
        )
    except Exception as exc:
        _fail(formatter, "list Slurm jobs", exc)
    if config.output_format != "table":
        formatter.print(result)
        return
    print(render_queue(result.get("jobs") or [], result.get("total", 0), target))


@slurm.command("job")
@click.argument("job_id", type=int)
@click.option("--region", "-r", help=_REGION_HELP)
@pass_config
def slurm_job(config: Any, job_id: int, region: str | None) -> None:
    """One job as slurmctld sees it (scontrol show job).

    Examples:
        gco slurm job 42 -r us-east-1
    """
    from ..slurm import render_job

    formatter = get_output_formatter(config)
    target = _slurm_region(config, region)
    try:
        result = _call(config, "GET", f"/api/v1/slurm/jobs/{job_id}", target)
    except Exception as exc:
        _fail(formatter, f"read Slurm job {job_id}", exc)
    if config.output_format != "table":
        formatter.print(result)
        return
    print(render_job(result.get("job") or {}, target))


@slurm.command("submit")
@click.argument("script", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--region", "-r", help=_REGION_HELP)
@click.option(
    "--name", "-J", help="Job name (default: the script's #SBATCH --job-name, else gco-job)"
)
@click.option("--partition", "-p", help="Partition (default: the cluster default partition)")
@click.option("--nodes", "-N", type=click.IntRange(1, 1024), help="Node count")
@click.option("--tasks", "-n", type=click.IntRange(1, 65536), help="Task count (--ntasks)")
@click.option("--cpus-per-task", "-c", type=click.IntRange(1, 1024), help="CPUs per task")
@click.option("--gpus-per-node", type=click.IntRange(0, 64), help="GPUs per node")
@click.option("--mem", "memory", help="Memory per node: MiB or K/M/G/T suffix (e.g. 64G)")
@click.option("--time", "-t", "time_limit", help="Wall time: minutes, HH:MM:SS or D-HH:MM:SS")
@click.option("--chdir", "-D", "working_directory", help="Working directory on the worker")
@click.option(
    "--env",
    "-e",
    "environment",
    multiple=True,
    metavar="NAME=VALUE",
    help="Extra environment (repeatable)",
)
@click.option("--comment", help="Free-text comment stored with the job")
@click.option("--exclusive", is_flag=True, default=False, help="Whole-node allocation")
@pass_config
def slurm_submit(
    config: Any,
    script: Path,
    region: str | None,
    name: str | None,
    partition: str | None,
    nodes: int | None,
    tasks: int | None,
    cpus_per_task: int | None,
    gpus_per_node: int | None,
    memory: str | None,
    time_limit: str | None,
    working_directory: str | None,
    environment: tuple[str, ...],
    comment: str | None,
    exclusive: bool,
) -> None:
    """Submit a batch script (sbatch) through the GCO API.

    The script's leading #SBATCH directives (--job-name, --partition, --nodes,
    --ntasks, --cpus-per-task, --gpus-per-node, --mem, --time, --chdir,
    --comment, --exclusive) are applied first; flags override them. The job
    runs as Slurm's own user with your caller identity in its comment.

    Examples:
        gco slurm submit hello.sbatch -r us-east-1
        gco slurm submit train.sbatch -r us-east-1 -p gpu -N 2 --gpus-per-node 8 -t 04:00:00
    """
    from ..slurm import SlurmCliError, memory_mb, read_batch_script, submission_body

    formatter = get_output_formatter(config)
    target = _slurm_region(config, region)
    try:
        body = submission_body(
            read_batch_script(script),
            flags={
                "name": name,
                "partition": partition,
                "nodes": nodes,
                "tasks": tasks,
                "cpus_per_task": cpus_per_task,
                "gpus_per_node": gpus_per_node,
                "memory_mb": memory_mb(memory) if memory else None,
                "time_limit": time_limit,
                "working_directory": working_directory,
                "comment": comment,
                "exclusive": exclusive,
            },
            environment=environment,
        )
    except SlurmCliError as exc:
        _fail(formatter, f"prepare {script.name}", exc)
    try:
        result = _call(config, "POST", "/api/v1/slurm/jobs", target, body=body)
    except Exception as exc:
        _fail(formatter, f"submit {script.name}", exc)
    formatter.print_success(
        f"Submitted job {result.get('job_id')} ({result.get('name')}) to "
        f"{result.get('partition') or 'the default partition'} in {target}"
    )
    if result.get("message"):
        formatter.print_info(str(result["message"]))
    formatter.print(result)


@slurm.command("cancel")
@click.argument("job_id", type=int)
@click.option("--region", "-r", help=_REGION_HELP)
@click.option("--signal", "-s", help="Signal to send instead of the default (e.g. SIGUSR1)")
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="Skip the confirmation prompt")
@pass_config
def slurm_cancel(
    config: Any, job_id: int, region: str | None, signal: str | None, assume_yes: bool
) -> None:
    """Cancel a job (scancel).

    Examples:
        gco slurm cancel 42 -r us-east-1
        gco slurm cancel 42 -r us-east-1 --signal SIGUSR1 -y
    """
    formatter = get_output_formatter(config)
    target = _slurm_region(config, region)
    if not assume_yes:
        confirm(f"Cancel Slurm job {job_id} in {target}?", abort=True)
    try:
        result = _call(
            config,
            "DELETE",
            f"/api/v1/slurm/jobs/{job_id}",
            target,
            params={"signal": signal.upper() if signal else None},
        )
    except Exception as exc:
        _fail(formatter, f"cancel Slurm job {job_id}", exc)
    formatter.print_success(f"Cancelled job {job_id} in {target}")
    formatter.print(result)


@slurm.command("accounting")
@click.option("--region", "-r", help=_REGION_HELP)
@click.option(
    "--since", help="Relative window (30m, 24h, 7d, 2w); default 1d unless --start is given"
)
@click.option("--start", help="Window start as a Slurm time (2026-10-01T00:00:00, now-2days)")
@click.option("--end", help="Window end as a Slurm time (default: now)")
@click.option("--user", "-u", help="Only this Slurm user")
@click.option("--partition", "-p", help="Only this partition")
@click.option("--state", "-s", help="Only these states (COMPLETED, FAILED,...; comma-separated)")
@click.option("--name", "-n", "job_name", help="Only jobs with exactly this name")
@click.option("--limit", "-l", default=200, type=click.IntRange(1, 5000), help="Maximum rows")
@pass_config
def slurm_accounting(
    config: Any,
    region: str | None,
    since: str | None,
    start: str | None,
    end: str | None,
    user: str | None,
    partition: str | None,
    state: str | None,
    job_name: str | None,
    limit: int,
) -> None:
    """Job history from slurmdbd (sacct).

    Examples:
        gco slurm accounting -r us-east-1
        gco slurm accounting -r us-east-1 --since 7d --state COMPLETED,FAILED
        gco slurm accounting -r us-east-1 --start 2026-10-01 --end 2026-10-08 --user slurm
    """
    from ..slurm import SlurmCliError, render_accounting, slurm_time_window

    formatter = get_output_formatter(config)
    target = _slurm_region(config, region)
    try:
        window = slurm_time_window(since or (None if start else "1d"), start, end)
    except SlurmCliError as exc:
        _fail(formatter, "read Slurm accounting", exc)
    try:
        result = _call(
            config,
            "GET",
            "/api/v1/slurm/accounting/jobs",
            target,
            params={
                **window,
                "user": user,
                "partition": partition,
                "state": state.upper() if state else None,
                "job_name": job_name,
                "limit": str(limit),
            },
        )
    except Exception as exc:
        _fail(formatter, "read Slurm accounting", exc)
    if config.output_format != "table":
        formatter.print(result)
        return
    print(render_accounting(result.get("jobs") or [], result.get("total", 0), target))


# ---------------------------------------------------------------------------
# Login pod
# ---------------------------------------------------------------------------


@slurm.command("shell")
@cluster_ui.tunnel_options
@pass_config
def slurm_shell(
    config: Any,
    region: str | None,
    via_ssm: str | None,
    bastion_ttl_minutes: int,
    assume_yes: bool,
) -> None:
    """Open an interactive shell on the Slurm login pod (sbatch, squeue, sacct, srun).

    Runs `kubectl exec -it` against the private EKS API endpoint; the pod
    mounts /home (EFS) and, when enabled, /scratch (FSx for Lustre). Table
    output only: an interactive terminal cannot be captured as JSON.

    Examples:
        gco slurm shell --via-ssm auto -y
        gco slurm shell -r us-west-2 --via-ssm i-0123456789abcdef0
    """
    from ..cluster_tunnel import open_api_server_tunnel, resolve_region
    from ..kubectl_helpers import update_kubeconfig
    from ..slurm import LOGIN_DEPLOYMENT, LOGIN_NAMESPACE, login_exec_command, run_foreground

    formatter = get_output_formatter(config)
    if config.output_format != "table":
        formatter.print_error("gco slurm shell is interactive; run it without --output json/yaml")
        sys.exit(1)
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
            cmd = login_exec_command(
                ["bash", "-l"],
                interactive=True,
                server=session.server,
                tls_server_name=session.tls_server_name,
            )
            formatter.print_info(
                f"Opening a shell on {LOGIN_NAMESPACE}/{LOGIN_DEPLOYMENT} in {target_region} "
                "(exit the shell to close the tunnel)..."
            )
            code = run_foreground(cmd)
    except (RuntimeError, ValueError) as exc:
        formatter.print_error(str(exc))
        sys.exit(1)
    if code != 0:
        sys.exit(code)


@slurm.command("output")
@click.argument("job_id", type=int)
@cluster_ui.tunnel_options
@click.option("--stderr", "use_stderr", is_flag=True, help="Read the job's stderr file instead")
@click.option("--follow", "-f", is_flag=True, help="Follow the file (tail -f) until Ctrl-C")
@click.option("--tail", "tail_lines", type=click.IntRange(1, 100000), help="Only the last N lines")
@pass_config
def slurm_output(
    config: Any,
    job_id: int,
    region: str | None,
    via_ssm: str | None,
    bastion_ttl_minutes: int,
    assume_yes: bool,
    use_stderr: bool,
    follow: bool,
    tail_lines: int | None,
) -> None:
    """Print a job's output file from the login pod.

    Looks the job up through the API for its stdout/stderr path (the
    default `slurm-<jobid>.out` under the job's working directory), then
    reads it over the private EKS endpoint with `kubectl exec`.

    Examples:
        gco slurm output 42 -r us-east-1 --via-ssm auto -y
        gco slurm output 42 -r us-east-1 --via-ssm i-0123456789abcdef0 --follow
    """
    from ..cluster_tunnel import open_api_server_tunnel, resolve_region
    from ..kubectl_helpers import update_kubeconfig
    from ..slurm import (
        SlurmCliError,
        login_exec_command,
        run_capture,
        run_foreground,
        validate_remote_path,
    )

    formatter = get_output_formatter(config)
    if follow and config.output_format != "table":
        formatter.print_error("--follow streams a terminal; run it without --output json/yaml")
        sys.exit(1)
    target_region = resolve_region(config, region)
    try:
        job = _call(config, "GET", f"/api/v1/slurm/jobs/{job_id}", target_region).get("job") or {}
    except Exception as exc:
        _fail(formatter, f"read Slurm job {job_id}", exc)
    key = "standard_error" if use_stderr else "standard_output"
    try:
        path = validate_remote_path(str(job.get(key) or ""))
    except SlurmCliError as exc:
        _fail(formatter, f"locate the {key.replace('_', ' ')} of job {job_id}", exc)
    reader = (
        ["tail", "-f", path]
        if follow
        else (["tail", "-n", str(tail_lines), path] if tail_lines else ["cat", path])
    )
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
            cmd = login_exec_command(
                reader, server=session.server, tls_server_name=session.tls_server_name
            )
            if follow:
                formatter.print_info(f"Following {path} (Ctrl-C to stop)...")
                code = run_foreground(cmd)
            else:
                text = run_capture(cmd)
                code = 0
    except (RuntimeError, ValueError) as exc:
        formatter.print_error(str(exc))
        sys.exit(1)
    if follow:
        if code != 0:
            sys.exit(code)
        return
    if config.output_format != "table":
        formatter.print({"job_id": job_id, "region": target_region, "path": path, "content": text})
        return
    sys.stdout.write(text)
    if text and not text.endswith("\n"):
        sys.stdout.write("\n")
