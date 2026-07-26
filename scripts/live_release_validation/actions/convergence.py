"""convergence: require stable SQS/DLQ and DynamoDB convergence."""

from __future__ import annotations

import json
import time
from typing import Any

from ..checks.central_queue import (
    _read_central_job_item,
)
from ..checks.topology import (
    _queue_counts,
)
from ..models import RunContext


def action_convergence(ctx: RunContext) -> dict[str, Any]:
    """Require stable empty SQS/DLQ counters and terminal DynamoDB records."""
    baseline = ctx.checkpoint.state.get("queue_baseline")
    if not baseline:
        raise RuntimeError("Topology action did not record queue baselines")

    deadline = time.monotonic() + ctx.settings.queue_timeout_seconds
    stable_observations = 0
    samples: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        sample = {
            region: ctx.job_manager.get_queue_status(region) for region in ctx.deployment_regions
        }
        counts = {region: _queue_counts(status) for region, status in sample.items()}
        samples.append({"at": time.time(), "counts": counts})
        expected_dlq = {region: _queue_counts(status)["dlq"] for region, status in baseline.items()}
        converged = all(
            values["available"] == 0
            and values["in_flight"] == 0
            and values["delayed"] == 0
            and values["dlq"] == expected_dlq.get(region, 0)
            for region, values in counts.items()
        )
        stable_observations = stable_observations + 1 if converged else 0
        if stable_observations >= 3:
            break
        time.sleep(ctx.settings.poll_interval_seconds)
    if stable_observations < 3:
        raise TimeoutError(
            "Regional SQS/DLQ counters did not converge for three observations: "
            + json.dumps(samples[-5:], sort_keys=True)
        )

    dynamodb_records: dict[str, Any] = {}
    for central_job in ctx.checkpoint.state.get("central_jobs", []):
        job_id = str(central_job["job_id"])
        item = _read_central_job_item(ctx, job_id)
        if item.get("status") != "succeeded":
            raise RuntimeError(f"DynamoDB record {job_id} regressed to {item.get('status')}")
        dynamodb_records[job_id] = item
    return {
        "stable_observations": stable_observations,
        "queue_samples": samples[-10:],
        "dynamodb_records": dynamodb_records,
    }
