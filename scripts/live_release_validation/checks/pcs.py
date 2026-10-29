"""Checks behind the AWS PCS half of the ``schedulers`` action, and the run's PCS override.

Every release run deploys the AWS PCS Slurm backend (``cdk.json`` ``pcs``,
``docs/PCS.md``) in managed mode with its metrics scrape on, whatever the
checkout's ``cdk.json`` says, the way it samples every trace: through the
run-scoped ``pcs_overrides`` CDK context (:func:`pcs_overrides_json`), never
by editing ``cdk.json``. PCS is a second Slurm backend behind the same API,
CLI and tools as the Slinky cluster, and a release must prove it the way it
proves the schedulers, so it is not opt-in.

What the probe proves, in the run's first deployment Region, through the
authenticated GCO API alone (no Slurm credential, no kubectl):

1. ``GET /api/v1/slurm/backends`` lists ``pcs`` as an ``ACTIVE`` cluster
   with the REST API on, under the name and Slurm version the block
   configured, and asks callers to choose a backend exactly when the Slinky
   cluster is on too;
2. ``POST /api/v1/slurm/jobs?backend=pcs`` submits a batch script that prints
   this run's marker; the job reaches ``COMPLETED`` within the run's job
   timeout, which means PCS launched a compute node from zero, it joined the
   controller, mounted the shared EFS home and ran the job as the fixed
   identity;
3. the slurmdbd accounting record carries the job as ``COMPLETED`` with the
   caller attribution the API stamped into its comment; and
4. ``gco slurm output --backend pcs`` reads the job's output file from the
   managed login node over SSM and finds the marker: the login node is
   running, SSM-managed, mounts the same ``/home``, and the CLI's PCS path
   works end to end.

The ``preflight`` action runs ``cli.pcs_preflight`` against the same merged
block before ``deploy`` creates anything, so a Region without PCS, an
instance type the Region lacks, or a taken cluster name fails in seconds
rather than an hour into the deploy.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any

from cli.pcs_preflight import fsx_enabled_in_region, raw_pcs_config
from gco.pcs_config import (
    PCS_OVERRIDES_CONTEXT_KEY,
    PCS_SUPPORTED_SLURM_VERSIONS,
    PcsConfigError,
    apply_pcs_overrides,
    is_managed,
    managed_cluster_name,
    pcs_enabled,
    validate_pcs_config,
)

from ..context import _job_transport_region
from ..models import RunContext
from .jobs import _run_token

#: The ``pcs_overrides`` context every release-run CDK invocation carries.
RELEASE_PCS_OVERRIDES: dict[str, Any] = {"enabled": True, "managed": {"metrics": {"enabled": True}}}
#: Slurm job states after which a job never runs again.
TERMINAL_JOB_STATES: frozenset[str] = frozenset(
    {
        "BOOT_FAIL",
        "CANCELLED",
        "COMPLETED",
        "DEADLINE",
        "FAILED",
        "NODE_FAIL",
        "OUT_OF_MEMORY",
        "PREEMPTED",
        "TIMEOUT",
    }
)
#: slurmdbd receives the job's final record a little after slurmctld ends it.
_ACCOUNTING_WAIT_SECONDS = 180
_ACCOUNTING_POLL_SECONDS = 5
#: The login node's output read goes through ssm:SendCommand; a transient SSM
#: refusal is retried, a wrong answer is not.
_OUTPUT_READ_ATTEMPTS = 4
_OUTPUT_READ_RETRY_SECONDS = 30
_BACKENDS_PATH = "/api/v1/slurm/backends"
_JOBS_PATH = "/api/v1/slurm/jobs"
_ACCOUNTING_PATH = "/api/v1/slurm/accounting/jobs"


class PcsValidationError(RuntimeError):
    """The deployed PCS backend did not behave as the block configured."""


def pcs_overrides_json() -> str:
    """Canonical JSON for the ``pcs_overrides`` context and the resume identity."""
    return json.dumps(RELEASE_PCS_OVERRIDES, sort_keys=True, separators=(",", ":"))


def effective_pcs_config(ctx: RunContext, region: str) -> dict[str, Any]:
    """The validated ``pcs`` block the deploy used for ``region``.

    cdk.json's block with the Region's ``pcs_regions`` overlay, then the
    run's ``pcs_overrides`` (from the settings, so a sibling harness that
    sets none sees cdk.json alone), validated the way the regional stack
    validates it.
    """
    overrides = ctx.settings.pcs_overrides_json or None
    try:
        raw = apply_pcs_overrides(raw_pcs_config(ctx.cdk_context, region), overrides)
        return validate_pcs_config(raw, fsx_enabled=fsx_enabled_in_region(ctx.cdk_context, region))
    except PcsConfigError as exc:
        raise PcsValidationError(f"{region}: the pcs block is invalid: {exc}") from exc


def effective_pcs_enablement(ctx: RunContext) -> dict[str, Any]:
    """Whether the run deploys PCS in its probe Region, and what decided it."""
    region = ctx.deployment_regions[0]
    config = effective_pcs_config(ctx, region)
    enabled = pcs_enabled(config)
    if ctx.settings.pcs_overrides_json and json.loads(ctx.settings.pcs_overrides_json).get(
        "enabled"
    ):
        source = f"run-override ({PCS_OVERRIDES_CONTEXT_KEY})"
    else:
        source = "cdk.json pcs"
    return {"enabled": enabled, "source": source, "mode": config["mode"], "region": region}


# ─── the probe ───────────────────────────────────────────────────────────────


def _request(ctx: RunContext, region: str, method: str, path: str, **kwargs: Any) -> Any:
    response = ctx.aws_client.make_authenticated_request(
        method=method, path=path, target_region=_job_transport_region(ctx, region), **kwargs
    )
    if not response.ok:
        raise PcsValidationError(
            f"{region}: {method} {path} answered HTTP {response.status_code}: "
            f"{str(response.text)[:500]}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise PcsValidationError(f"{region}: {method} {path} answered non-JSON") from exc
    if not isinstance(payload, dict):
        raise PcsValidationError(f"{region}: {method} {path} answered {type(payload).__name__}")
    return payload


def _check_backends(ctx: RunContext, region: str, config: dict[str, Any]) -> dict[str, Any]:
    payload = _request(ctx, region, "GET", _BACKENDS_PATH)
    backends = {
        str(entry.get("name")): entry
        for entry in payload.get("backends") or []
        if isinstance(entry, dict)
    }
    pcs = backends.get("pcs")
    if pcs is None:
        raise PcsValidationError(
            f"{region}: /api/v1/slurm/backends does not list pcs (got {sorted(backends)})"
        )
    if "error" in pcs:
        raise PcsValidationError(f"{region}: the pcs backend reports an error: {pcs['error']}")
    problems: list[str] = []
    if pcs.get("status") != "ACTIVE":
        problems.append(f"status is {pcs.get('status')!r}, expected ACTIVE")
    if pcs.get("rest_mode") != "STANDARD":
        problems.append(f"rest_mode is {pcs.get('rest_mode')!r}, expected STANDARD")
    if str(pcs.get("slurm_version")) not in PCS_SUPPORTED_SLURM_VERSIONS:
        problems.append(f"slurm_version {pcs.get('slurm_version')!r} is not one the API speaks")
    if is_managed(config):
        expected_name = managed_cluster_name(config, ctx.config.project_name)
        if pcs.get("cluster_name") != expected_name:
            problems.append(
                f"cluster_name is {pcs.get('cluster_name')!r}, expected {expected_name!r}"
            )
        expected_version = str(config["managed"]["slurm_version"])
        if str(pcs.get("slurm_version")) != expected_version:
            problems.append(
                f"slurm_version is {pcs.get('slurm_version')!r}, the block asked for "
                f"{expected_version!r}"
            )
    both = "slinky" in backends
    if bool(payload.get("selection_required")) != both:
        problems.append(
            f"selection_required is {payload.get('selection_required')!r} with backends "
            f"{sorted(backends)}"
        )
    if payload.get("default") != (None if both else "pcs"):
        problems.append(f"default is {payload.get('default')!r} with backends {sorted(backends)}")
    if problems:
        raise PcsValidationError(f"{region}: the pcs backend is not usable: " + "; ".join(problems))
    return {
        "backends": sorted(backends),
        "selection_required": bool(payload.get("selection_required")),
        "default": payload.get("default"),
        "cluster_id": pcs.get("cluster_id"),
        "cluster_name": pcs.get("cluster_name"),
        "slurm_version": pcs.get("slurm_version"),
        "status": pcs.get("status"),
        "identity": pcs.get("identity"),
    }


def _job_states(job: dict[str, Any]) -> list[str]:
    state = job.get("state")
    if isinstance(state, list):
        return [str(item) for item in state]
    return [str(state)] if state else []


def _submit_job(ctx: RunContext, region: str, *, name: str, marker: str) -> dict[str, Any]:
    script = f"#!/bin/bash\nset -euo pipefail\necho {marker}\nhostname\nid\n"
    payload = _request(
        ctx,
        region,
        "POST",
        f"{_JOBS_PATH}?backend=pcs",
        body={
            "script": script,
            "name": name,
            "time_limit": "00:15:00",
            "comment": "live-release-validation",
        },
    )
    job_id = payload.get("job_id")
    if not isinstance(job_id, int) or job_id <= 0:
        raise PcsValidationError(f"{region}: the submission returned no job id: {payload}")
    if payload.get("backend") != "pcs":
        raise PcsValidationError(
            f"{region}: the submission was answered by backend {payload.get('backend')!r}"
        )
    return {"job_id": job_id, "name": name, "submitted_at": time.time()}


def _wait_for_completion(ctx: RunContext, region: str, job_id: int) -> dict[str, Any]:
    deadline = time.monotonic() + float(ctx.settings.job_timeout_seconds)
    poll = max(1, int(ctx.settings.poll_interval_seconds))
    history: list[dict[str, Any]] = []
    while True:
        payload = _request(ctx, region, "GET", f"{_JOBS_PATH}/{job_id}?backend=pcs")
        job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
        states = _job_states(job)
        reason = str(job.get("state_reason") or "")
        if not history or history[-1]["states"] != states or history[-1]["reason"] != reason:
            history.append({"at": time.time(), "states": states, "reason": reason})
        if "COMPLETED" in states:
            return {
                "states": states,
                "history": history,
                "nodes": job.get("nodes"),
                "exit_code": job.get("exit_code"),
                "source": payload.get("source"),
            }
        terminal = sorted(set(states) & TERMINAL_JOB_STATES)
        if terminal:
            raise PcsValidationError(
                f"{region}: PCS job {job_id} ended {'/'.join(terminal)} "
                f"(reason {reason or 'none'}, exit code {job.get('exit_code')!r})"
            )
        if time.monotonic() >= deadline:
            raise PcsValidationError(
                f"{region}: PCS job {job_id} did not complete within "
                f"{ctx.settings.job_timeout_seconds}s; last state {states} ({reason or 'no reason'})"
            )
        time.sleep(poll)


def _wait_for_accounting(ctx: RunContext, region: str, *, job_id: int, name: str) -> dict[str, Any]:
    deadline = time.monotonic() + _ACCOUNTING_WAIT_SECONDS
    last: dict[str, Any] | None = None
    while True:
        payload = _request(
            ctx,
            region,
            "GET",
            f"{_ACCOUNTING_PATH}?backend=pcs&start=now-6hours&job_name={name}&limit=200",
        )
        rows = [
            row
            for row in payload.get("jobs") or []
            if isinstance(row, dict) and row.get("job_id") == job_id
        ]
        if rows:
            last = rows[0]
            states = _job_states(last)
            comment = str(last.get("comment") or "")
            if "COMPLETED" in states and comment.startswith("gco:"):
                return {"states": states, "comment": comment, "cluster": last.get("cluster")}
            if set(states) & (TERMINAL_JOB_STATES - {"COMPLETED"}):
                raise PcsValidationError(
                    f"{region}: accounting records PCS job {job_id} as {states}"
                )
            if comment and not comment.startswith("gco:"):
                raise PcsValidationError(
                    f"{region}: accounting record of PCS job {job_id} lost the caller "
                    f"attribution (comment {comment!r})"
                )
        if time.monotonic() >= deadline:
            raise PcsValidationError(
                f"{region}: slurmdbd has no COMPLETED record for PCS job {job_id} after "
                f"{_ACCOUNTING_WAIT_SECONDS}s (last seen {last})"
            )
        time.sleep(_ACCOUNTING_POLL_SECONDS)


def _output_argv(region: str, job_id: int) -> list[str]:
    """The real ``gco slurm output`` command, read as JSON so the content is a field."""
    return [
        sys.executable,
        "-m",
        "cli.main",
        "--output",
        "json",
        "slurm",
        "output",
        str(job_id),
        "-r",
        region,
        "--backend",
        "pcs",
    ]


def _read_output(ctx: RunContext, region: str, *, job_id: int, marker: str) -> dict[str, Any]:
    argv = _output_argv(region, job_id)
    attempts: list[dict[str, Any]] = []
    for attempt in range(1, _OUTPUT_READ_ATTEMPTS + 1):
        result = subprocess.run(
            argv,
            cwd=ctx.settings.repo_root,
            capture_output=True,
            text=True,
            timeout=float(ctx.settings.command_timeout_seconds),
            env=dict(os.environ),
            shell=False,
            check=False,
        )
        record: dict[str, Any] = {"attempt": attempt, "returncode": result.returncode}
        if result.returncode == 0:
            try:
                document = json.loads(result.stdout)
            except ValueError as exc:
                raise PcsValidationError(
                    f"{region}: gco slurm output printed non-JSON: {result.stdout[:300]!r}"
                ) from exc
            content = str(document.get("content") or "")
            if marker not in content:
                raise PcsValidationError(
                    f"{region}: the output file of PCS job {job_id} does not carry the "
                    f"marker (truncated={document.get('truncated')!r}, "
                    f"{len(content)} characters)"
                )
            return {
                "argv": argv,
                "attempts": [*attempts, record],
                "truncated": bool(document.get("truncated")),
                "characters": len(content),
                "backend": document.get("backend"),
            }
        record["stderr"] = result.stderr[-1000:]
        attempts.append(record)
        if attempt < _OUTPUT_READ_ATTEMPTS:
            time.sleep(_OUTPUT_READ_RETRY_SECONDS)
    raise PcsValidationError(
        f"{region}: gco slurm output --backend pcs failed {_OUTPUT_READ_ATTEMPTS} times for job "
        f"{job_id}; last stderr: {attempts[-1].get('stderr', '')!r}"
    )


def probe_pcs(ctx: RunContext) -> dict[str, Any]:
    """Prove the deployed PCS backend end to end; raises :class:`PcsValidationError`."""
    region = ctx.deployment_regions[0]
    config = effective_pcs_config(ctx, region)
    token = _run_token(ctx.settings.run_id)
    marker = f"GCO_LIVE_PCS_{token}"
    name = f"gco-live-pcs-{token}"
    evidence: dict[str, Any] = {
        "region": region,
        "mode": config["mode"],
        "marker": marker,
        "backend": _check_backends(ctx, region, config),
    }
    submission = _submit_job(ctx, region, name=name, marker=marker)
    evidence["job"] = submission
    evidence["completion"] = _wait_for_completion(ctx, region, submission["job_id"])
    evidence["accounting"] = _wait_for_accounting(
        ctx, region, job_id=submission["job_id"], name=name
    )
    if is_managed(config) and config["managed"]["login"]["enabled"]:
        evidence["output"] = _read_output(ctx, region, job_id=submission["job_id"], marker=marker)
    else:
        evidence["output"] = {
            "status": "skipped",
            "reason": "no managed login node to read the output file from",
        }
    return evidence
