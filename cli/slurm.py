"""Business logic behind ``gco slurm`` (cli/commands/slurm_cmd.py).

The command module owns the Click surface; this module owns everything that
can be tested without a terminal: reading a batch script and its ``#SBATCH``
directives into the API's submission body, Slurm's relative time windows,
the human renderings of the API's answers, and the ``kubectl exec`` argv for
the login pod (``shell`` / ``output``) that the cluster tunnel reaches.

The API half lives in gco/services/api_routes/slurm.py; every request here
is pinned to one region because each region runs its own Slurm cluster.
"""

from __future__ import annotations

import re
import shlex
import subprocess  # nosec B404  # kubectl runs as a validated argv list, never through a shell
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gco.slurm_config import SLURM_LOGIN_NAME, SLURM_NAMESPACE

from . import cluster_ui

#: The Slinky LoginSet GCO deploys and its namespace; ``shell`` and ``output``
#: exec into its Deployment (the operator names it after the LoginSet) because
#: it mounts the same /home (EFS) and /scratch (FSx) the workers do and
#: carries every Slurm client. ``login`` is the operator's container name.
LOGIN_NAMESPACE = SLURM_NAMESPACE
LOGIN_DEPLOYMENT = SLURM_LOGIN_NAME
LOGIN_CONTAINER = "login"

#: ``--since`` suffixes to Slurm's relative time units (``now-7days``).
_DURATION_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_DURATION_RE = re.compile(r"^(?P<count>\d{1,6})(?P<unit>[smhdw])$")
#: Absolute Slurm times (``parse_time``): YYYY-MM-DD[THH:MM[:SS]], HH:MM[:SS], MM/DD[/YY], epoch.
_SLURM_TIME_RE = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?|\d{2}:\d{2}(?::\d{2})?"
    r"|\d{2}/\d{2}(?:/\d{2,4})?|\d{9,10}|now(?:[+-]\d+(?:seconds|minutes|hours|days|weeks))?)$"
)
_SBATCH_RE = re.compile(r"^#SBATCH\s+(?P<body>.+?)\s*$")
_SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/@+%:=,-]{0,1023}$")
MAX_SCRIPT_BYTES = 64 * 1024


class SlurmCliError(ValueError):
    """A request that cannot be shaped: bad script, bad window, bad path."""


# ---------------------------------------------------------------------------
# Time windows
# ---------------------------------------------------------------------------


def slurm_time_window(since: str | None, start: str | None, end: str | None) -> dict[str, str]:
    """Translate ``--since 7d`` / ``--start`` / ``--end`` into slurmdb query parameters.

    ``since`` is a relative window (``30m``, ``24h``, ``7d``, ``2w``) rendered
    as Slurm's own ``now-7days`` form so slurmdbd does the arithmetic in the
    cluster's clock; ``start``/``end`` are passed through when they look like
    a Slurm time. ``since`` and ``start`` are alternatives.
    """
    if since and start:
        raise SlurmCliError("pass either --since or --start, not both")
    params: dict[str, str] = {}
    if since:
        match = _DURATION_RE.match(since.strip().lower())
        if match is None:
            raise SlurmCliError(
                f"invalid --since {since!r}: use a count and unit such as 30m, 24h, 7d or 2w"
            )
        count, unit = int(match["count"]), _DURATION_UNITS[match["unit"]]
        if count == 0:
            raise SlurmCliError("--since must be a positive window")
        params["start"] = f"now-{count}{unit}"
    elif start:
        params["start"] = _slurm_time(start, "--start")
    if end:
        params["end"] = _slurm_time(end, "--end")
    return params


def _slurm_time(value: str, flag: str) -> str:
    text = value.strip()
    if not _SLURM_TIME_RE.match(text):
        raise SlurmCliError(
            f"invalid {flag} {value!r}: use YYYY-MM-DD[THH:MM[:SS]], HH:MM, an epoch, "
            "or Slurm's relative form such as now-2days"
        )
    return text


# ---------------------------------------------------------------------------
# Batch scripts
# ---------------------------------------------------------------------------


def read_batch_script(path: Path) -> str:
    """Read a batch script, refusing empty, oversized or shebang-less files."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SlurmCliError(f"cannot read {path}: {exc.strerror or exc}") from exc
    if not raw.strip():
        raise SlurmCliError(f"{path} is empty")
    if len(raw) > MAX_SCRIPT_BYTES:
        raise SlurmCliError(
            f"{path} is {len(raw)} bytes; batch scripts are limited to {MAX_SCRIPT_BYTES} "
            "(put the payload in a container image or on /home or /scratch)"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SlurmCliError(f"{path} is not UTF-8 text") from exc
    if not text.lstrip().startswith("#!"):
        raise SlurmCliError(f"{path} must start with an interpreter line such as #!/bin/bash")
    return text


#: ``#SBATCH`` options the CLI understands, mapped to the API body's fields.
#: slurmrestd does not read ``#SBATCH`` lines (sbatch the client does), so the
#: CLI applies them as defaults the explicit flags override; anything else in
#: the script is left for Slurm to ignore, as sbatch would with unknown options.
_SBATCH_OPTIONS: dict[str, str] = {
    "-J": "name",
    "--job-name": "name",
    "-p": "partition",
    "--partition": "partition",
    "-N": "nodes",
    "--nodes": "nodes",
    "-n": "tasks",
    "--ntasks": "tasks",
    "-c": "cpus_per_task",
    "--cpus-per-task": "cpus_per_task",
    "--gpus-per-node": "gpus_per_node",
    "--mem": "memory_mb",
    "-t": "time_limit",
    "--time": "time_limit",
    "-D": "working_directory",
    "--chdir": "working_directory",
    "--comment": "comment",
    "--exclusive": "exclusive",
}
_INT_FIELDS = {"nodes", "tasks", "cpus_per_task", "gpus_per_node"}


def sbatch_directives(script: str) -> dict[str, Any]:
    """Collect the ``#SBATCH`` directives the API body can carry.

    Reads the leading comment block the way sbatch does (directives stop at
    the first non-comment, non-blank line). Unknown options are ignored.
    ``--mem`` accepts Slurm's K/M/G/T suffixes and lands in MiB; ``--gpus-per-node``
    accepts ``N`` or ``type:N``; ``--exclusive`` is a flag.
    """
    found: dict[str, Any] = {}
    for line in script.splitlines()[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("#"):
            break
        match = _SBATCH_RE.match(stripped)
        if match is None:
            continue
        try:
            tokens = shlex.split(match["body"], comments=True)
        except ValueError:
            continue
        index = 0
        while index < len(tokens):
            token = tokens[index]
            option, value = token, None
            if token.startswith("--") and "=" in token:
                option, value = token.split("=", 1)
            elif len(token) > 2 and token[0] == "-" and token[1] != "-":
                option, value = token[:2], token[2:]
            field = _SBATCH_OPTIONS.get(option)
            if field is None:
                index += 1
                continue
            if field == "exclusive":
                found[field] = True
                index += 1
                continue
            if value is None:
                index += 1
                if index >= len(tokens):
                    break
                value = tokens[index]
            index += 1
            found[field] = _directive_value(field, value)
    return found


def _directive_value(field: str, value: str) -> Any:
    if field in _INT_FIELDS:
        text = value.split(":")[-1] if field == "gpus_per_node" else value
        try:
            return int(text)
        except ValueError as exc:
            raise SlurmCliError(
                f"#SBATCH {field.replace('_', '-')} {value!r} is not a number"
            ) from exc
    if field == "memory_mb":
        return memory_mb(value)
    return value


def memory_mb(value: str) -> int:
    """Slurm's ``--mem`` (``4000``, ``4G``, ``512M``, ``1T``) in MiB."""
    match = re.fullmatch(r"(\d+)([KkMmGgTt]?)", value.strip())
    if match is None:
        raise SlurmCliError(f"invalid memory {value!r}: use MiB, or a K/M/G/T suffix such as 64G")
    amount, unit = int(match[1]), match[2].upper() or "M"
    factor = {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}[unit]
    return max(int(amount * factor), 1)


def submission_body(
    script: str,
    *,
    flags: Mapping[str, Any],
    environment: Sequence[str] = (),
) -> dict[str, Any]:
    """Compose the ``POST /api/v1/slurm/jobs`` body: directives, then flags on top."""
    body: dict[str, Any] = {"script": script}
    body.update(sbatch_directives(script))
    for key, value in flags.items():
        if value is None or (key == "exclusive" and value is False):
            continue
        body[key] = value
    env: dict[str, str] = {}
    for item in environment:
        name, separator, value = item.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise SlurmCliError(f"invalid --env {item!r}: use NAME=value")
        env[name] = value
    if env:
        body["environment"] = env
    return body


# ---------------------------------------------------------------------------
# Human renderings
# ---------------------------------------------------------------------------


def _when(epoch: Any) -> str:
    if not isinstance(epoch, int) or epoch <= 0:
        return "-"
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")


def _join(states: Any) -> str:
    if isinstance(states, list):
        return ",".join(str(s) for s in states) or "-"
    return str(states or "-")


def render_status(status: Mapping[str, Any], region: str) -> str:
    lines = [f"\n  Slurm — {region} (slurmrestd {status.get('api_version', '?')})", "  " + "-" * 70]
    for controller in status.get("controller") or []:
        state = "UP" if controller.get("responding") else "DOWN"
        latency = controller.get("latency_us")
        suffix = f" ({latency} µs)" if isinstance(latency, int) else ""
        lines.append(
            f"  Controller {controller.get('hostname')}: {state}{suffix} [{controller.get('mode')}]"
        )
    nodes = status.get("nodes") or {}
    jobs = status.get("jobs") or {}
    scaled_in = nodes.get("scaled_in") or 0
    node_line = f"  Nodes: {nodes.get('total', 0)} {_counts(nodes.get('by_state'))}"
    if scaled_in:
        node_line += f" + {scaled_in} scaled in (records kept until the next scale-up)"
    lines.append(node_line)
    lines.append(f"  Jobs:  {jobs.get('total', 0)} {_counts(jobs.get('by_state'))}")
    lines.append("")
    lines.extend(_partition_table(status.get("partitions") or []))
    return "\n".join(lines)


def _counts(by_state: Any) -> str:
    if not isinstance(by_state, dict) or not by_state:
        return ""
    return "(" + ", ".join(f"{state} {count}" for state, count in sorted(by_state.items())) + ")"


def _partition_table(partitions: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = [
        f"  {'PARTITION':<14} {'STATE':<12} {'NODES':>5} {'CPUS':>5} {'MAX TIME':>10}  NODELIST"
    ]
    lines.append("  " + "-" * 70)
    for partition in partitions:
        name = str(partition.get("name") or "-")
        if partition.get("default"):
            name += "*"
        max_time = partition.get("max_time_minutes")
        lines.append(
            f"  {name:<14} {_join(partition.get('state')):<12} "
            f"{partition.get('total_nodes') if partition.get('total_nodes') is not None else '-':>5} "
            f"{partition.get('total_cpus') if partition.get('total_cpus') is not None else '-':>5} "
            f"{(str(max_time) + 'm') if isinstance(max_time, int) else 'infinite':>10}  "
            f"{partition.get('node_names') or '-'}"
        )
    if not partitions:
        lines.append("  (no partitions)")
    return lines


def render_partitions(partitions: Sequence[Mapping[str, Any]], region: str) -> str:
    return "\n".join([f"\n  Slurm partitions — {region}", *_partition_table(partitions), ""])


def render_nodes(nodes: Sequence[Mapping[str, Any]], region: str) -> str:
    lines = [
        f"\n  Slurm nodes — {region} ({len(nodes)})",
        f"  {'NODE':<28} {'STATE':<18} {'CPUS':>9} {'MEMORY MB':>17} {'GRES':<20} PARTITIONS",
        "  " + "-" * 110,
    ]
    for node in nodes:
        cpus = f"{node.get('cpus_allocated') or 0}/{node.get('cpus') or 0}"
        memory = f"{node.get('memory_allocated_mb') or 0}/{node.get('real_memory_mb') or 0}"
        gres = str(node.get("gres_used") or node.get("gres") or "-")
        state = "SCALED-IN" if node.get("scaled_in") else _join(node.get("state"))
        lines.append(
            f"  {node.get('name') or '-'!s:<28} {state:<18} {cpus:>9} "
            f"{memory:>17} {gres[:20]:<20} {','.join(node.get('partitions') or []) or '-'}"
        )
        if node.get("reason") and not node.get("scaled_in"):
            lines.append(f"    reason: {node['reason']}")
    if not nodes:
        lines.append("  (no nodes — autoscaled partitions start at zero workers)")
    lines.append("")
    return "\n".join(lines)


def render_queue(jobs: Sequence[Mapping[str, Any]], total: Any, region: str) -> str:
    lines = [
        f"\n  Slurm queue — {region} ({len(jobs)} of {total} shown)",
        f"  {'JOBID':>8} {'NAME':<24} {'PARTITION':<10} {'STATE':<12} {'NODES':>5} {'SUBMITTED':<19}  REASON/NODELIST",
        "  " + "-" * 110,
    ]
    for job in jobs:
        tail = job.get("nodes") or job.get("state_reason") or "-"
        lines.append(
            f"  {job.get('job_id') or '-'!s:>8} {str(job.get('name') or '-')[:24]:<24} "
            f"{str(job.get('partition') or '-')[:10]:<10} {_join(job.get('state'))[:12]:<12} "
            f"{job.get('node_count') if job.get('node_count') is not None else '-':>5} "
            f"{_when(job.get('submit_time')):<19}  {tail}"
        )
    if not jobs:
        lines.append("  (queue is empty)")
    lines.append("")
    return "\n".join(lines)


def _state_line(job: Mapping[str, Any]) -> str:
    """``PENDING — Resources`` while the job waits; the bare state otherwise.

    Slurm's state reason says why a job is still pending. Both daemons keep
    reporting the last one afterwards (``ReqNodeNotAvail`` on a COMPLETED job,
    ``None`` on a running one), where it is noise.
    """
    states = job.get("state")
    state = _join(states)
    reason = job.get("state_reason")
    pending = "PENDING" in (states if isinstance(states, list) else [states])
    if pending and reason and reason != "None":
        return f"{state} — {reason}"
    return state


def render_job(job: Mapping[str, Any], region: str) -> str:
    exit_code = job.get("exit_code") or {}
    rows = [
        ("Job", f"{job.get('job_id')} ({job.get('name')})"),
        ("Region", region),
        (
            "Source",
            "slurmdbd (finished; slurmctld has forgotten it)"
            if job.get("source") == "slurmdbd"
            else "slurmctld",
        ),
        ("State", _state_line(job)),
        ("User / account", f"{job.get('user') or '-'} / {job.get('account') or '-'}"),
        ("Partition", str(job.get("partition") or "-")),
        (
            "Nodes",
            f"{job.get('node_count') if job.get('node_count') is not None else '-'} {job.get('nodes') or ''}".strip(),
        ),
        ("Requested", str(job.get("tres_requested") or "-")),
        ("Allocated", str(job.get("tres_allocated") or "-")),
        ("Submitted", _when(job.get("submit_time"))),
        ("Started", _when(job.get("start_time"))),
        ("Ended", _when(job.get("end_time"))),
        (
            "Time limit",
            f"{job.get('time_limit_minutes')} min" if job.get("time_limit_minutes") else "infinite",
        ),
        (
            "Exit",
            f"{_join(exit_code.get('status'))} rc={exit_code.get('return_code')}"
            if exit_code
            else "-",
        ),
        ("Comment", str(job.get("comment") or "-")),
        ("Working dir", str(job.get("working_directory") or "-")),
        ("Stdout", str(job.get("standard_output") or "-")),
        ("Stderr", str(job.get("standard_error") or "-")),
    ]
    width = max(len(label) for label, _value in rows)
    return "\n".join(["", *(f"  {label:<{width}}  {value}" for label, value in rows), ""])


def render_accounting(jobs: Sequence[Mapping[str, Any]], total: Any, region: str) -> str:
    lines = [
        f"\n  Slurm accounting — {region} ({len(jobs)} of {total} shown)",
        f"  {'JOBID':>8} {'NAME':<22} {'USER':<10} {'PARTITION':<10} {'STATE':<12} {'ELAPSED':>9} {'EXIT':>6} {'STARTED':<19}  COMMENT",
        "  " + "-" * 120,
    ]
    for job in jobs:
        exit_code = job.get("exit_code") or {}
        elapsed = job.get("elapsed_seconds")
        lines.append(
            f"  {job.get('job_id') or '-'!s:>8} {str(job.get('name') or '-')[:22]:<22} "
            f"{str(job.get('user') or '-')[:10]:<10} {str(job.get('partition') or '-')[:10]:<10} "
            f"{_join(job.get('state'))[:12]:<12} {_elapsed(elapsed):>9} "
            f"{exit_code.get('return_code') if exit_code.get('return_code') is not None else '-'!s:>6} "
            f"{_when(job.get('start_time')):<19}  {str(job.get('comment') or '')[:40]}"
        )
    if not jobs:
        lines.append("  (no accounting records in this window)")
    lines.append("")
    return "\n".join(lines)


def _elapsed(seconds: Any) -> str:
    if not isinstance(seconds, int) or seconds < 0:
        return "-"
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


# ---------------------------------------------------------------------------
# Login pod access (kubectl exec over the cluster tunnel)
# ---------------------------------------------------------------------------


def login_exec_command(
    args: Sequence[str],
    *,
    interactive: bool = False,
    server: str | None = None,
    tls_server_name: str | None = None,
) -> list[str]:
    """``kubectl exec`` into the login Deployment with the tunnel's flags."""
    cmd = ["kubectl", "exec"]
    if interactive:
        cmd.append("-it")
    cmd += [
        f"deploy/{LOGIN_DEPLOYMENT}",
        "-n",
        LOGIN_NAMESPACE,
        "-c",
        LOGIN_CONTAINER,
        *cluster_ui.kubectl_server_flags(server, tls_server_name),
        "--",
        *args,
    ]
    return cmd


def validate_remote_path(path: str) -> str:
    """An absolute path the login pod may read (``cat`` / ``tail``), with no shell metacharacters."""
    if not _SAFE_PATH_RE.match(path) or ".." in path.split("/"):
        raise SlurmCliError(
            f"refusing to read {path!r}: expected an absolute path on the login pod"
        )
    return path


def run_foreground(cmd: Sequence[str]) -> int:
    """Run kubectl with inherited stdio (an interactive shell, a tail -f); return its exit code."""
    try:
        completed = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - argv built by login_exec_command; list form, no shell=True
            list(cmd), check=False
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "kubectl not found. Install kubectl and ensure it's on your PATH."
        ) from exc
    return int(completed.returncode)


def run_capture(cmd: Sequence[str], *, timeout: float = 120.0) -> str:
    """Run kubectl and return its stdout; a failure carries kubectl's stderr."""
    try:
        completed = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - argv built by login_exec_command; list form, no shell=True
            list(cmd), capture_output=True, text=True, check=False, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "kubectl not found. Install kubectl and ensure it's on your PATH."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Timed out after {timeout:.0f}s running {cmd[0]} {cmd[1]}") from exc
    if completed.returncode != 0:
        raise RuntimeError(
            f"kubectl exec failed ({completed.returncode}): {(completed.stderr or '').strip()}"
        )
    return completed.stdout
