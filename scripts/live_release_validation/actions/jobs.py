"""api and sqs: authenticated API and direct regional SQS Job lifecycles."""

from __future__ import annotations

from typing import Any

from ..checks.jobs import (
    _complete_job_lifecycle,
    _get_owned_job,
    _job_appearance_timeout,
    _load_manifest,
    _register_job,
    _run_api_transport_lifecycle,
    _run_token,
    _wait_for_ambiguous_job_reconciliation,
    _wait_for_owned_job_appearance,
)
from ..constants import (
    _RUN_JOB_LABEL,
)
from ..models import RunContext


def action_api_lifecycle(ctx: RunContext) -> dict[str, Any]:
    """Submit, observe, read logs, and delete an authenticated API Job.

    The full crash-safe lifecycle lives in
    :func:`..checks.jobs._run_api_transport_lifecycle`, shared with the
    scheduler probes so the transport dance cannot drift between them.
    """
    return _run_api_transport_lifecycle(
        ctx,
        manifest_filename="api-smoke-job.yaml",
        path="api",
        marker_prefix="API",
    )


def action_sqs_lifecycle(ctx: RunContext) -> dict[str, Any]:
    """Submit, observe, read logs, and delete a direct regional-SQS Job."""
    manifests, name, namespace = _load_manifest(ctx, "sqs-smoke-job.yaml")
    token = _run_token(ctx.settings.run_id)
    marker = f"GCO_LIVE_SQS_{token}"
    region = ctx.deployment_regions[0]
    record = _register_job(
        ctx,
        name=name,
        namespace=namespace,
        execution_region=region,
        path="sqs",
    )
    envelope = {
        "transport": "direct-sqs",
        "manifests": manifests,
        "region": region,
        "namespace": namespace,
        "labels": {_RUN_JOB_LABEL: token},
        "priority": 100,
    }
    ctx.prepare_job_submission(record, envelope=envelope, resumable=False)

    existing = _get_owned_job(ctx, record)
    state = str(record.get("submission_state") or "")
    if existing is None and state == "submitting":
        existing = _wait_for_ambiguous_job_reconciliation(ctx, record)
        if existing is None:
            reason = (
                "Direct SQS submission crossed a non-idempotent boundary but no Job appeared; "
                "automatic replay is forbidden"
            )
            ctx.block_job_submission(record, reason)
            raise RuntimeError(reason)
    elif existing is None and state == "submitted":
        existing = _wait_for_owned_job_appearance(ctx, record)
    elif existing is None and state == "blocked":
        raise RuntimeError(str(record.get("submission_blocked_reason") or "SQS submission blocked"))

    if existing is None:
        if state != "prepared":
            raise RuntimeError(f"Cannot submit SQS Job from state {state!r}")
        ctx.begin_job_submission(
            record,
            reconciliation_timeout_seconds=_job_appearance_timeout(ctx),
        )
        submission = ctx.job_manager.submit_job_sqs(
            manifests,
            region=region,
            namespace=namespace,
            labels={_RUN_JOB_LABEL: token},
            priority=100,
        )
        if submission.get("job_name") != name:
            raise RuntimeError(
                f"SQS submission returned unexpected job name: {submission.get('job_name')}"
            )
        ctx.finish_job_submission(
            record,
            submission,
            appearance_timeout_seconds=_job_appearance_timeout(ctx),
        )
        ctx.checkpoint.state["sqs_submission"] = submission
        ctx.persist()
    else:
        submission = {"reconciled_existing_job": True}

    lifecycle = _complete_job_lifecycle(ctx, record=record, marker=marker)
    lifecycle["submission"] = submission
    return lifecycle
