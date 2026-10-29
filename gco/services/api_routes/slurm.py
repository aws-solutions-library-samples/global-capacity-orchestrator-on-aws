"""Slurm endpoints — the authenticated ``/api/v1/slurm/*`` surface.

The manifest API is the cluster's authenticated ingress (HMAC middleware plus
the IAM-authorized API Gateway in front), so Slurm is exposed here and
proxied to slurmrestd through :mod:`gco.services.slurm_client`. Two Slurm
backends can stand behind the routes (:mod:`gco.services.slurm_backends`):
the Slinky cluster on the EKS cluster (``helm.slurm``) and an AWS PCS cluster
in the regional VPC (``pcs``). Callers never reach slurmrestd or hold a Slurm
credential: the API acts as one fixed Slurm identity per backend and stamps
the submitting caller into each job's ``comment`` for accounting attribution.

- ``GET    /api/v1/slurm/backends``             — the enabled backends and their facts
- ``GET    /api/v1/slurm/status``               — controller reachability, partitions, node and job counts
- ``GET    /api/v1/slurm/partitions``           — partitions with their state, limits and node counts
- ``GET    /api/v1/slurm/nodes``                — Slurm nodes with state and resources
- ``GET    /api/v1/slurm/jobs``                 — the queue (filter by state, partition, name)
- ``POST   /api/v1/slurm/jobs``                 — submit a batch script (sbatch)
- ``GET    /api/v1/slurm/jobs/{job_id}``        — one job (from slurmdbd once slurmctld forgets it)
- ``DELETE /api/v1/slurm/jobs/{job_id}``        — cancel (scancel)
- ``GET    /api/v1/slurm/accounting/jobs``      — slurmdbd job history (sacct)

Every route except ``/backends`` takes an optional ``backend`` query parameter
(``slinky`` or ``pcs``). With one backend enabled it may be omitted; with both
enabled it is required (400 otherwise); naming a backend that is not deployed
answers 404 with its enablement remedy. Every response carries ``backend``.
When no backend is deployed every route answers 404 with the remedy; an
unreachable slurmrestd or missing credential answers 503.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, field_validator

from gco.services import slurm_client
from gco.services.structured_logging import sanitize_log_value

router = APIRouter(prefix="/api/v1/slurm", tags=["Slurm"])
logger = logging.getLogger(__name__)

#: Batch scripts are limited well under slurmrestd's request limits; a job that
#: needs more belongs in a container image or on the shared filesystem.
MAX_SCRIPT_BYTES = 64 * 1024
#: Slurm job names: printable, no whitespace control characters.
_JOB_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,127}$")
_PARTITION_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_TIME_LIMIT_RE = re.compile(
    r"^(UNLIMITED|INFINITE|\d+|\d+:\d{2}|\d+:\d{2}:\d{2}|\d+-\d{1,2}|\d+-\d{1,2}:\d{2}|\d+-\d{1,2}:\d{2}:\d{2})$"
)
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: Headers the caller's identity may arrive in: the VPC proxy Lambda forwards
#: the SigV4 principal of the API Gateway request as ``x-gco-caller`` when it
#: knows it; otherwise the comment records the API itself.
_CALLER_HEADERS = ("x-gco-caller", "x-gco-caller-arn")

#: The ``backend`` query parameter every route but ``/backends`` takes.
BackendParam = Query(
    None,
    description=(
        "Which Slurm cluster: 'slinky' (the cluster on the EKS cluster) or 'pcs' (AWS PCS). "
        "Optional when one backend is enabled; required when both are."
    ),
)


class SlurmJobSubmission(BaseModel):
    """Request body for ``POST /api/v1/slurm/jobs`` (the sbatch equivalent)."""

    script: str = Field(..., description="The batch script, starting with a #! line")
    name: str = Field("gco-job", description="Slurm job name")
    partition: str | None = Field(None, description="Partition (default: the cluster default)")
    nodes: int = Field(1, ge=1, le=1024, description="Node count (--nodes)")
    tasks: int = Field(1, ge=1, le=65536, description="Task count (--ntasks)")
    cpus_per_task: int = Field(1, ge=1, le=1024, description="CPUs per task (--cpus-per-task)")
    gpus_per_node: int = Field(0, ge=0, le=64, description="GPUs per node (--gpus-per-node)")
    memory_mb: int | None = Field(None, ge=1, description="Memory per node in MiB (--mem)")
    time_limit: str | None = Field(None, description="Wall time (--time), e.g. 01:00:00")
    working_directory: str | None = Field(
        None,
        description="Working directory on the worker (default: the backend's Slurm user home on "
        "the shared filesystem, where slurm-<jobid>.out lands)",
    )
    environment: dict[str, str] = Field(default_factory=dict, description="Extra environment")
    comment: str | None = Field(None, max_length=512, description="Free-text comment")
    exclusive: bool = Field(False, description="Whole-node allocation (--exclusive)")

    @field_validator("script")
    @classmethod
    def _script_shape(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("script must not be empty")
        if len(value.encode("utf-8")) > MAX_SCRIPT_BYTES:
            raise ValueError(f"script exceeds {MAX_SCRIPT_BYTES} bytes")
        if not value.lstrip().startswith("#!"):
            raise ValueError("script must start with an interpreter line (#!/bin/bash)")
        return value

    @field_validator("name")
    @classmethod
    def _name_shape(cls, value: str) -> str:
        if not _JOB_NAME_RE.match(value):
            raise ValueError("name must be 1-128 characters of letters, digits, _ . : + -")
        return value

    @field_validator("partition")
    @classmethod
    def _partition_shape(cls, value: str | None) -> str | None:
        if value is not None and not _PARTITION_RE.match(value):
            raise ValueError("partition must be a lowercase partition name")
        return value

    @field_validator("time_limit")
    @classmethod
    def _time_shape(cls, value: str | None) -> str | None:
        if value is not None and not _TIME_LIMIT_RE.match(value.strip()):
            raise ValueError("time_limit must be a Slurm time (minutes, HH:MM:SS, D-HH:MM:SS)")
        return value.strip() if value else value

    @field_validator("working_directory")
    @classmethod
    def _cwd_shape(cls, value: str | None) -> str | None:
        if value is not None and (not value.startswith("/") or "\n" in value):
            raise ValueError("working_directory must be an absolute path")
        return value

    @field_validator("environment")
    @classmethod
    def _environment_shape(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if not _ENV_NAME_RE.match(key):
                raise ValueError(f"environment variable name {key!r} is not valid")
            if "\n" in item or "\x00" in item:
                raise ValueError(f"environment variable {key!r} must be a single-line value")
        if len(value) > 64:
            raise ValueError("at most 64 environment variables")
        return value


def _client(backend: str | None) -> slurm_client.SlurmRestClient:
    try:
        return slurm_client.get_client(backend)
    except (slurm_client.SlurmDisabledError, slurm_client.SlurmBackendSelectionError) as exc:
        raise slurm_client.http_exception_for(exc) from exc


def _backend_name(client: slurm_client.SlurmRestClient) -> str:
    return str(client.backend.name)


def _raise(error: Exception, context: str) -> HTTPException:
    """Translate a client error, logging the ones that point at the deployment.

    4xx answers are the caller's to read (an unknown job, a bad partition);
    5xx ones are logged with the underlying cause so the operator can find
    slurmrestd or the token problem the caller only sees as 503/500.
    """
    http_error = slurm_client.http_exception_for(error, context=context)
    if http_error.status_code >= 500:
        # ``context`` can carry a path parameter (the job id), so it is
        # sanitized like the error text even though the router already
        # validated it as an integer.
        logger.warning(
            "Slurm %s failed (HTTP %s): %s: %s",
            sanitize_log_value(context),
            http_error.status_code,
            type(error).__name__,
            sanitize_log_value(str(error)),
        )
    return http_error


def _envelope(payload: dict[str, Any], *, backend: str | None = None) -> dict[str, Any]:
    body = {"timestamp": datetime.now(UTC).isoformat()}
    if backend is not None:
        body["backend"] = backend
    return {**body, **payload}


def _number(value: Any) -> int | None:
    """slurmrestd wraps many numbers as {set, infinite, number}; unwrap them."""
    if isinstance(value, dict):
        if value.get("infinite"):
            return None
        return int(value["number"]) if value.get("set") and "number" in value else None
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _states(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [value]
    return []


def _mapping(value: Any) -> dict[str, Any]:
    """The value when it is a JSON object, else an empty one (sparse payloads)."""
    return value if isinstance(value, dict) else {}


def _summarize_partition(item: dict[str, Any]) -> dict[str, Any]:
    nodes = _mapping(item.get("nodes"))
    partition_state = _mapping(item.get("partition"))
    maximums = _mapping(item.get("maximums"))
    cpus = item.get("cpus")
    states = _states(partition_state.get("state"))
    return {
        "name": item.get("name"),
        "state": states,
        "default": "DEFAULT" in {s.upper() for s in states},
        "total_nodes": _number(nodes.get("total")),
        "node_names": nodes.get("configured") or nodes.get("allowed_allocation") or "",
        "max_time_minutes": _number(maximums.get("time")),
        "total_cpus": _number(cpus.get("total")) if isinstance(cpus, dict) else None,
    }


#: The slurm-operator drains a worker before KEDA removes its pod and, in
#: StatefulSet scaling mode, leaves the node record behind as DOWN+DRAIN with
#: this reason prefix until the next scale-up re-registers it (observed live).
_SCALED_IN_REASON_PREFIX = "slurm-operator:"


def _scaled_in(item: dict[str, Any]) -> bool:
    """A node whose only problem is that its pod was scaled away."""
    states = {s.upper() for s in _states(item.get("state"))}
    reason = str(item.get("reason") or "")
    return "DRAIN" in states and "DOWN" in states and reason.startswith(_SCALED_IN_REASON_PREFIX)


def _summarize_node(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": item.get("name"),
        "hostname": item.get("hostname"),
        "state": _states(item.get("state")),
        "scaled_in": _scaled_in(item),
        "partitions": list(item.get("partitions") or []),
        "cpus": _number(item.get("cpus")),
        "cpus_allocated": _number(item.get("alloc_cpus")),
        "real_memory_mb": _number(item.get("real_memory")),
        "memory_allocated_mb": _number(item.get("alloc_memory")),
        "gres": item.get("gres") or "",
        "gres_used": item.get("gres_used") or "",
        "reason": item.get("reason") or "",
    }


def _summarize_job(item: dict[str, Any]) -> dict[str, Any]:
    state = item.get("job_state")
    return {
        "job_id": item.get("job_id"),
        "name": item.get("name"),
        "user": item.get("user_name"),
        "account": item.get("account"),
        "partition": item.get("partition"),
        "state": _states(state),
        "state_reason": item.get("state_reason"),
        "nodes": item.get("nodes") or "",
        "node_count": _number(item.get("node_count")),
        "cpus": _number(item.get("cpus")),
        "tres_requested": item.get("tres_req_str") or "",
        "tres_allocated": item.get("tres_alloc_str") or "",
        "submit_time": _number(item.get("submit_time")),
        "start_time": _number(item.get("start_time")),
        "end_time": _number(item.get("end_time")),
        "time_limit_minutes": _number(item.get("time_limit")),
        "exit_code": _exit_code(item.get("exit_code")),
        "comment": item.get("comment") or "",
        "working_directory": item.get("current_working_directory"),
        "standard_output": _output_path(item, item.get("standard_output")),
        "standard_error": _output_path(item, item.get("standard_error")),
    }


def _output_path(item: dict[str, Any], pattern: Any) -> str | None:
    """slurmctld reports stdout/stderr as the submitted pattern (``slurm-%j.out``,
    relative to the working directory); expand the common filename patterns and
    anchor the path so ``gco slurm output`` can read it from the login pod."""
    if not isinstance(pattern, str) or not pattern:
        return None
    replacements = {
        "%j": item.get("job_id"),
        "%J": item.get("job_id"),
        "%u": item.get("user_name"),
        "%x": item.get("name"),
    }
    array = item.get("array_job_id"), item.get("array_task_id")
    if _number(array[0]) and _number(array[1]) is not None:
        replacements["%A"] = _number(array[0])
        replacements["%a"] = _number(array[1])
    expanded = pattern.replace("%%", "\x00")
    for token, value in replacements.items():
        if value is not None:
            expanded = expanded.replace(token, str(value))
    expanded = expanded.replace("\x00", "%")
    if not expanded.startswith("/"):
        cwd = str(item.get("current_working_directory") or "").rstrip("/")
        expanded = f"{cwd}/{expanded}" if cwd else expanded
    return expanded


def _exit_code(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "status": _states(value.get("status")),
        "return_code": _number(value.get("return_code")),
    }


def _summarize_accounting_job(item: dict[str, Any]) -> dict[str, Any]:
    state = _mapping(item.get("state"))
    time = _mapping(item.get("time"))
    exit_code = _mapping(item.get("exit_code"))
    tres = _mapping(item.get("tres"))
    comment = item.get("comment")
    return {
        "job_id": item.get("job_id"),
        "name": item.get("name"),
        "user": item.get("user"),
        "account": item.get("account"),
        "partition": item.get("partition"),
        "cluster": item.get("cluster"),
        "state": _states(state.get("current")),
        "state_reason": state.get("reason"),
        "nodes": item.get("nodes") or "",
        "submit_time": _number(time.get("submission")),
        "start_time": _number(time.get("start")),
        "end_time": _number(time.get("end")),
        "elapsed_seconds": _number(time.get("elapsed")),
        "time_limit_minutes": _number(time.get("limit")),
        "exit_code": {
            "status": _states(exit_code.get("status")),
            "return_code": _number(exit_code.get("return_code")),
        },
        "tres_allocated": tres.get("allocated") or [],
        "comment": comment.get("job") if isinstance(comment, dict) else (comment or ""),
    }


def _accounting_record_as_job(item: dict[str, Any]) -> dict[str, Any]:
    """The slurmctld job shape, filled from a slurmdbd record.

    slurmctld forgets a finished job after ``MinJobAge`` (ten minutes here);
    slurmdbd keeps it, including the expanded stdout/stderr paths and the
    working directory that ``gco slurm output`` needs. Fields slurmctld alone
    knows (``state_reason`` for a pending job, ``tres_requested``) are what the
    record offers, and ``source`` says which daemon answered.
    """
    record = _summarize_accounting_job(item)
    tres = ",".join(
        f"{entry.get('type')}={entry.get('count')}"
        for entry in record["tres_allocated"]
        if isinstance(entry, dict) and entry.get("type")
    )
    return {
        "job_id": record["job_id"],
        "name": record["name"],
        "user": record["user"],
        "account": record["account"],
        "partition": record["partition"],
        "state": record["state"],
        "state_reason": record["state_reason"],
        "nodes": record["nodes"],
        "node_count": _number(item.get("allocation_nodes")),
        "cpus": _number(_mapping(item.get("required")).get("CPUs")),
        "tres_requested": tres,
        "tres_allocated": tres,
        "submit_time": record["submit_time"],
        "start_time": record["start_time"],
        "end_time": record["end_time"],
        "time_limit_minutes": record["time_limit_minutes"],
        "exit_code": record["exit_code"],
        "comment": record["comment"],
        "working_directory": item.get("working_directory"),
        "standard_output": item.get("stdout_expanded") or None,
        "standard_error": item.get("stderr_expanded") or None,
        "source": "slurmdbd",
    }


def _ping_up(ping: dict[str, Any]) -> bool:
    """slurmrestd reports controller reachability as ``pinged: "UP"`` (older
    plugin versions) or ``responding: true`` (newer ones)."""
    if ping.get("responding") is True:
        return True
    return str(ping.get("pinged") or "").strip().upper() == "UP"


def _caller_identity(request: Request) -> str:
    for header in _CALLER_HEADERS:
        value = request.headers.get(header)
        if value:
            return value.strip()[:200]
    return "gco-api"


@router.get("/backends")
async def slurm_backends() -> Response:
    """The Slurm backends enabled in this region with what each one is and where.

    Answers 200 with an empty list when none is deployed, so a client can tell
    "Slurm is off here" from "the API is down" without parsing a 404. Each
    entry's ``describe()`` is best-effort: a backend whose discovery fails
    still appears, with the failure in ``error`` instead of its facts, so one
    broken backend never hides the other.
    """
    backends: list[dict[str, Any]] = []
    for name in slurm_client.enabled_backends():
        try:
            client = slurm_client.get_client(name)
            backends.append(await client.backend.describe())
        except Exception as exc:
            http_error = _raise(exc, f"describing backend {name}")
            backends.append({"name": name, "error": http_error.detail})
    return JSONResponse(
        status_code=200,
        content=_envelope(
            {
                "backends": backends,
                "default": backends[0]["name"] if len(backends) == 1 else None,
                "selection_required": len(backends) > 1,
            }
        ),
    )


@router.get("/status")
async def slurm_status(backend: str | None = BackendParam) -> Response:
    """Controller reachability plus partition, node and job counts for this region."""
    client = _client(backend)
    try:
        ping = await client.ping()
        partitions = await client.partitions()
        nodes = await client.nodes()
        jobs = await client.jobs()
    except Exception as exc:
        raise _raise(exc, "reading status") from exc
    node_items = [n for n in nodes.get("nodes", []) if isinstance(n, dict)]
    job_items = [j for j in jobs.get("jobs", []) if isinstance(j, dict)]
    job_states: dict[str, int] = {}
    for job in job_items:
        for state in _states(job.get("job_state")) or ["UNKNOWN"]:
            job_states[state] = job_states.get(state, 0) + 1
    node_states: dict[str, int] = {}
    scaled_in = 0
    for node in node_items:
        if _scaled_in(node):
            scaled_in += 1
            continue
        for state in _states(node.get("state")) or ["UNKNOWN"]:
            node_states[state] = node_states.get(state, 0) + 1
    pings = [p for p in ping.get("pings", []) if isinstance(p, dict)]
    try:
        facts = await client.backend.describe()
    except Exception as exc:
        raise _raise(exc, "describing backend") from exc
    return JSONResponse(
        status_code=200,
        content=_envelope(
            {
                "enabled": True,
                "cluster": facts,
                "controller": [
                    {
                        "hostname": p.get("hostname"),
                        "responding": _ping_up(p),
                        "latency_us": _number(p.get("latency")),
                        "mode": p.get("mode"),
                    }
                    for p in pings
                ],
                "partitions": [
                    _summarize_partition(p)
                    for p in partitions.get("partitions", [])
                    if isinstance(p, dict)
                ],
                "nodes": {
                    "total": len(node_items),
                    "by_state": node_states,
                    "scaled_in": scaled_in,
                },
                "jobs": {"total": len(job_items), "by_state": job_states},
                "api_version": client.version,
            },
            backend=_backend_name(client),
        ),
    )


@router.get("/partitions")
async def slurm_partitions(backend: str | None = BackendParam) -> Response:
    """Every Slurm partition with its state, time limit and node count."""
    client = _client(backend)
    try:
        payload = await client.partitions()
    except Exception as exc:
        raise _raise(exc, "listing partitions") from exc
    return JSONResponse(
        status_code=200,
        content=_envelope(
            {
                "partitions": [
                    _summarize_partition(p)
                    for p in payload.get("partitions", [])
                    if isinstance(p, dict)
                ]
            },
            backend=_backend_name(client),
        ),
    )


@router.get("/nodes")
async def slurm_nodes(
    partition: str | None = Query(None, description="Only nodes in this partition"),
    backend: str | None = BackendParam,
) -> Response:
    """Slurm nodes (slurmd worker pods or PCS instances) with state, resources and gres."""
    client = _client(backend)
    try:
        payload = await client.nodes()
    except Exception as exc:
        raise _raise(exc, "listing nodes") from exc
    nodes = [_summarize_node(n) for n in payload.get("nodes", []) if isinstance(n, dict)]
    if partition:
        nodes = [n for n in nodes if partition in n["partitions"]]
    return JSONResponse(
        status_code=200, content=_envelope({"nodes": nodes}, backend=_backend_name(client))
    )


@router.get("/jobs")
async def slurm_jobs(
    state: str | None = Query(None, description="Job state filter (PENDING, RUNNING, ...)"),
    partition: str | None = Query(None, description="Partition filter"),
    name: str | None = Query(None, description="Exact job name filter"),
    limit: int = Query(200, ge=1, le=5000, description="Maximum jobs returned"),
    backend: str | None = BackendParam,
) -> Response:
    """The live queue as slurmctld sees it (pending, running and recently finished jobs)."""
    client = _client(backend)
    try:
        payload = await client.jobs()
    except Exception as exc:
        raise _raise(exc, "listing jobs") from exc
    jobs = [_summarize_job(j) for j in payload.get("jobs", []) if isinstance(j, dict)]
    if state:
        wanted = state.upper()
        jobs = [j for j in jobs if wanted in {s.upper() for s in j["state"]}]
    if partition:
        jobs = [j for j in jobs if j["partition"] == partition]
    if name:
        jobs = [j for j in jobs if j["name"] == name]
    return JSONResponse(
        status_code=200,
        content=_envelope(
            {"jobs": jobs[:limit], "total": len(jobs)}, backend=_backend_name(client)
        ),
    )


@router.post("/jobs", status_code=201)
async def slurm_submit(
    submission: SlurmJobSubmission, request: Request, backend: str | None = BackendParam
) -> Response:
    """Submit a batch script to Slurm (sbatch through slurmrestd)."""
    client = _client(backend)
    caller = _caller_identity(request)
    comment = f"gco:{caller}"
    if submission.comment:
        comment = f"{comment} {submission.comment}"
    defaults = client.backend.defaults
    partition = submission.partition or defaults.partition
    working_directory = submission.working_directory or defaults.working_directory
    # slurmrestd's job_desc_msg (v0.0.44): environment is a list of NAME=VALUE
    # strings, a whole-node request is shared=none (sbatch --exclusive), and
    # PATH must be given because the batch step inherits no environment.
    environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", **submission.environment}
    job: dict[str, Any] = {
        "name": submission.name,
        "nodes": str(submission.nodes),
        "tasks": submission.tasks,
        "cpus_per_task": submission.cpus_per_task,
        "current_working_directory": working_directory,
        "comment": comment[:1024],
        "environment": [f"{key}={value}" for key, value in environment.items()],
    }
    if partition:
        job["partition"] = partition
    if submission.gpus_per_node:
        job["tres_per_node"] = f"gres/gpu:{submission.gpus_per_node}"
    if submission.memory_mb is not None:
        job["memory_per_node"] = {"set": True, "number": submission.memory_mb}
    if submission.time_limit:
        job["time_limit"] = _time_limit_minutes(submission.time_limit)
    if submission.exclusive:
        job["shared"] = ["none"]
    try:
        payload = await client.submit(job, submission.script)
    except Exception as exc:
        raise _raise(exc, "submitting job") from exc
    job_id = payload.get("job_id")
    logger.info(
        "Submitted Slurm job %s (%s) to %s for %s",
        job_id,
        sanitize_log_value(submission.name),
        _backend_name(client),
        sanitize_log_value(caller),
    )
    return JSONResponse(
        status_code=201,
        content=_envelope(
            {
                "job_id": job_id,
                "step_id": payload.get("step_id"),
                "name": submission.name,
                "partition": partition,
                "working_directory": working_directory,
                "submitted_by": caller,
                "message": payload.get("job_submit_user_msg") or "",
            },
            backend=_backend_name(client),
        ),
    )


def _time_limit_minutes(value: str) -> dict[str, Any]:
    """Convert a Slurm time string to the minutes slurmrestd's time_limit wants."""
    text = value.strip().upper()
    if text in {"UNLIMITED", "INFINITE"}:
        return {"set": True, "infinite": True}
    days = 0
    if "-" in text:
        day_part, text = text.split("-", 1)
        days = int(day_part)
        parts = [int(p) for p in text.split(":")]
        hours, minutes, seconds = [*parts, 0, 0, 0][:3]
        total = days * 1440 + hours * 60 + minutes + (1 if seconds else 0)
    else:
        parts = [int(p) for p in text.split(":")]
        if len(parts) == 1:
            total = parts[0]
        elif len(parts) == 2:
            total = parts[0] + (1 if parts[1] else 0)
        else:
            hours, minutes, seconds = parts[:3]
            total = hours * 60 + minutes + (1 if seconds else 0)
    return {"set": True, "number": max(total, 1)}


@router.get("/jobs/{job_id}")
async def slurm_job(job_id: int, backend: str | None = BackendParam) -> Response:
    """One job: slurmctld's live view, else slurmdbd's record once the controller forgot it.

    slurmctld answers "Invalid job id" (rc 2017) for a job older than
    ``MinJobAge``; the accounting record then carries the same summary with
    ``source: slurmdbd`` so ``gco slurm job`` and ``gco slurm output`` keep
    working for finished jobs (verified live: a ten-minute-old job).
    """
    client = _client(backend)
    try:
        payload = await client.job(job_id)
    except slurm_client.SlurmApiError as exc:
        if exc.status_code != 404:
            raise _raise(exc, f"reading job {job_id}") from exc
        payload = {"jobs": []}
    except Exception as exc:
        raise _raise(exc, f"reading job {job_id}") from exc
    jobs = [j for j in payload.get("jobs", []) if isinstance(j, dict)]
    if jobs:
        job = {**_summarize_job(jobs[0]), "source": "slurmctld"}
        return JSONResponse(
            status_code=200, content=_envelope({"job": job}, backend=_backend_name(client))
        )
    try:
        accounted = await client.accounting_job(job_id)
    except Exception as exc:
        raise _raise(exc, f"reading job {job_id} from accounting") from exc
    records = [j for j in accounted.get("jobs", []) if isinstance(j, dict)]
    if not records:
        raise HTTPException(status_code=404, detail=f"Slurm job {job_id} not found")
    return JSONResponse(
        status_code=200,
        content=_envelope(
            {"job": _accounting_record_as_job(records[0])}, backend=_backend_name(client)
        ),
    )


@router.delete("/jobs/{job_id}")
async def slurm_cancel(
    job_id: int,
    signal: str | None = Query(None, pattern=r"^[A-Z0-9]{1,16}$", description="Signal name"),
    backend: str | None = BackendParam,
) -> Response:
    """Cancel a job (scancel), optionally with a specific signal."""
    client = _client(backend)
    try:
        await client.cancel(job_id, signal=signal)
    except Exception as exc:
        raise _raise(exc, f"cancelling job {job_id}") from exc
    logger.info("Cancelled Slurm job %s on %s", sanitize_log_value(job_id), _backend_name(client))
    return JSONResponse(
        status_code=200,
        content=_envelope({"job_id": job_id, "cancelled": True}, backend=_backend_name(client)),
    )


@router.get("/accounting/jobs")
async def slurm_accounting_jobs(
    start: str | None = Query(None, description="Start of the window (Slurm time or epoch)"),
    end: str | None = Query(None, description="End of the window"),
    user: str | None = Query(None, pattern=r"^[A-Za-z0-9_.-]{1,64}$"),
    partition: str | None = Query(None, pattern=r"^[a-z][a-z0-9-]{0,31}$"),
    state: str | None = Query(None, pattern=r"^[A-Za-z_,]{1,64}$"),
    job_name: str | None = Query(None, pattern=r"^[A-Za-z0-9_.:+-]{1,128}$"),
    limit: int = Query(200, ge=1, le=5000),
    backend: str | None = BackendParam,
) -> Response:
    """Job history from slurmdbd (sacct); requires accounting to be enabled.

    As with ``sacct -s``, a ``state`` filter without an explicit window end
    matches only jobs in that state right now; the end defaults to ``now`` so
    ``state=COMPLETED`` returns the window's completed jobs (verified live).
    """
    client = _client(backend)
    params: dict[str, str] = {}
    if start:
        params["start_time"] = start
    if end:
        params["end_time"] = end
    elif state:
        params["end_time"] = "now"
    if user:
        params["users"] = user
    if partition:
        params["partition"] = partition
    if state:
        params["state"] = state
    if job_name:
        params["job_name"] = job_name
    try:
        payload = await client.accounting_jobs(params)
    except Exception as exc:
        raise _raise(exc, "reading accounting") from exc
    jobs = [_summarize_accounting_job(j) for j in payload.get("jobs", []) if isinstance(j, dict)]
    return JSONResponse(
        status_code=200,
        content=_envelope(
            {"jobs": jobs[:limit], "total": len(jobs)}, backend=_backend_name(client)
        ),
    )
