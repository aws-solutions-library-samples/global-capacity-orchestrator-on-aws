"""The keep-warm probe: two back-to-back GPU Jobs share one node, and its idle time is recorded.

Run by the ``keep-warm`` action against the first deployed Region. Both Jobs
come from ``manifests/keep-warm-smoke-job.yaml``, which selects the keep-warm
pool (``gco.aws/keep-warm``) and pins the instance and capacity type, so the
second one fits the node the first one leaves idle. The probe holds the
platform to what ``docs/GPU_KEEP_WARM.md`` promises:

* the first Job runs on a node of ``gpu-keep-warm-pool``;
* the second, submitted as soon as the first completes, runs on that same
  node, and the pool has no NodeClaim it did not have before;
* the first Job's ``keep_warm`` record closes as ``reused``;
* the node is reclaimed about one window after the second Job's pod finished,
  and the second Job's record closes as ``reclaimed`` with that idle time.

The Jobs stay in place until the records are read (the idle record lives on
the Job), and the probe deletes them before it returns; the run's teardown
deletes them if a check fails first.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from gco.keep_warm import (
    ENDED_BY_RECLAIMED,
    ENDED_BY_REUSED,
    KEEP_WARM_CONTEXT_KEY,
    KEEP_WARM_LABEL,
    KEEP_WARM_NODEPOOL,
    validate_keep_warm_config,
)

from ..models import RunContext
from .cluster import KubectlRunner, kubectl_json
from .jobs import (
    _delete_owned_job,
    _get_owned_job,
    _load_manifest,
    _register_job,
    _run_api_transport_lifecycle,
)

KEEP_WARM_MANIFEST = "keep-warm-smoke-job.yaml"
#: The manifest's placeholder for which of the two Jobs it is.
STEP_PLACEHOLDER = "__KEEP_WARM_STEP__"
STEPS = ("first", "second")
#: The health monitor records on a 30-second loop; three loops and a margin.
RECORD_TIMEOUT_SECONDS = 180
#: How long past the window the node may take to go: Karpenter's disruption
#: pass, the drain, and the instance's termination.
RECLAIM_MARGIN_SECONDS = 900
#: The reclaimed interval may start a little after Karpenter's timer did: the
#: record starts at the container's finish, and both clocks keep seconds.
CLOCK_SLACK_SECONDS = 30
_KUBECTL_TIMEOUT_SECONDS = 60.0


def _step_path(step: str) -> str:
    return f"keep-warm-{step}"


def deployed_window_seconds(ctx: RunContext) -> int:
    """The keep-warm window this run deployed, from cdk.json ``gpu_keep_warm``."""
    return int(
        validate_keep_warm_config(ctx.cdk_context.get(KEEP_WARM_CONTEXT_KEY))["window_seconds"]
    )


def _placement(evidence: dict[str, Any], step: str) -> dict[str, Any]:
    """Where a completed Job's pod ran, as Get Job reported it; it must be a keep-warm node."""
    scheduling = evidence.get("scheduling")
    scheduling = scheduling if isinstance(scheduling, dict) else {}
    node = scheduling.get("node_name")
    labels = scheduling.get("node_labels")
    labels = labels if isinstance(labels, dict) else {}
    if not isinstance(node, str) or not node:
        raise RuntimeError(f"The {step} keep-warm Job completed, but Get Job names no node")
    if (
        labels.get("karpenter.sh/nodepool") != KEEP_WARM_NODEPOOL
        or labels.get(KEEP_WARM_LABEL) != "true"
    ):
        raise RuntimeError(
            f"The {step} keep-warm Job ran on {node}, which is not a {KEEP_WARM_NODEPOOL} node "
            f"(node labels {labels})"
        )
    return {
        "node": node,
        "instance_type": scheduling.get("node_instance_type"),
        "capacity_type": scheduling.get("node_capacity_type"),
    }


def pool_nodeclaims(kubectl: KubectlRunner, evidence: dict[str, Any]) -> dict[str, str | None]:
    """The keep-warm pool's NodeClaims, by name, with the node each one launched."""
    payload = kubectl_json(
        kubectl,
        evidence,
        "get",
        "nodeclaims",
        "--selector",
        f"karpenter.sh/nodepool={KEEP_WARM_NODEPOOL}",
        timeout=_KUBECTL_TIMEOUT_SECONDS,
    )
    claims: dict[str, str | None] = {}
    for item in (payload or {}).get("items") or []:
        name = (item.get("metadata") or {}).get("name")
        if isinstance(name, str) and name:
            node = (item.get("status") or {}).get("nodeName")
            claims[name] = node if isinstance(node, str) and node else None
    return claims


def _poll(
    ctx: RunContext,
    probe: Callable[[], Any],
    *,
    timeout: float,
    what: str,
) -> Any:
    """Call ``probe`` every poll interval until it returns something other than ``None``."""
    deadline = time.monotonic() + timeout
    while True:
        value = probe()
        if value is not None:
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{what} within {int(timeout)}s")
        time.sleep(ctx.settings.poll_interval_seconds)


def _closed_record(
    ctx: RunContext, record: dict[str, Any], *, node: str, ended_by: str
) -> Callable[[], dict[str, Any] | None]:
    """A probe for the Job's interval on ``node`` that closed as ``ended_by``."""

    def probe() -> dict[str, Any] | None:
        job = _get_owned_job(ctx, record)
        if job is None:
            raise RuntimeError(f"Job {record['name']} disappeared before its idle record was read")
        summary = job.get("keep_warm")
        for entry in (summary or {}).get("idle") or []:
            if entry.get("node") == node and entry.get("until"):
                if entry.get("ended_by") != ended_by:
                    raise RuntimeError(
                        f"Job {record['name']}'s interval on {node} closed as "
                        f"{entry.get('ended_by')!r}, expected {ended_by!r}: {entry}"
                    )
                return dict(entry)
        return None

    return probe


def _node_absent(
    kubectl: KubectlRunner, evidence: dict[str, Any], node: str
) -> Callable[[], bool | None]:
    def probe() -> bool | None:
        found = kubectl_json(
            kubectl, evidence, "get", "node", node, timeout=_KUBECTL_TIMEOUT_SECONDS
        )
        return True if found is None else None

    return probe


def run_keep_warm_probe(ctx: RunContext, kubectl: KubectlRunner) -> dict[str, Any]:
    """Run both Jobs and the record checks; return the evidence. Raises on any broken promise.

    A failed check leaves the Jobs, and their records, for the report and the
    run's teardown.
    """
    region = ctx.deployment_regions[0]
    window = deployed_window_seconds(ctx)
    evidence: dict[str, Any] = {"region": region, "window_seconds": window}
    records: dict[str, dict[str, Any]] = {}
    placements: dict[str, dict[str, Any]] = {}
    claims_before: dict[str, str | None] = {}
    for step in STEPS:
        if step == "second":
            claims_before = pool_nodeclaims(kubectl, evidence)
            evidence["nodeclaims_before_second"] = sorted(claims_before)
        lifecycle = _run_api_transport_lifecycle(
            ctx,
            manifest_filename=KEEP_WARM_MANIFEST,
            path=_step_path(step),
            marker_prefix="KEEP_WARM",
            tokens={STEP_PLACEHOLDER: step},
            delete=False,
        )
        _, name, namespace = _load_manifest(ctx, KEEP_WARM_MANIFEST, {STEP_PLACEHOLDER: step})
        records[step] = _register_job(
            ctx,
            name=name,
            namespace=namespace,
            execution_region=region,
            path=_step_path(step),
            reactivate_deleted=False,
        )
        placements[step] = _placement(lifecycle, step)
    evidence["jobs"] = {
        step: {"name": records[step]["name"], "uid": records[step].get("uid"), **placements[step]}
        for step in STEPS
    }

    node = placements["first"]["node"]
    if placements["second"]["node"] != node:
        raise RuntimeError(
            f"The second keep-warm Job ran on {placements['second']['node']}, not on {node}, "
            "which the first one had just left idle"
        )
    claims_after = pool_nodeclaims(kubectl, evidence)
    evidence["nodeclaims_after_second"] = sorted(claims_after)
    new_claims = sorted(set(claims_after) - set(claims_before))
    if new_claims:
        raise RuntimeError(
            f"The second keep-warm Job reused {node}, but the pool launched NodeClaims "
            f"{new_claims} meanwhile"
        )
    if node not in claims_after.values():
        raise RuntimeError(f"No {KEEP_WARM_NODEPOOL} NodeClaim names {node}")

    reused = _poll(
        ctx,
        _closed_record(ctx, records["first"], node=node, ended_by=ENDED_BY_REUSED),
        timeout=RECORD_TIMEOUT_SECONDS,
        what=f"The first Job's idle interval on {node} did not close as reused",
    )
    evidence["first_record"] = reused
    if not 0 <= int(reused["seconds"]) < window:
        raise RuntimeError(
            f"The first Job's node sat idle {reused['seconds']}s before the second Job, "
            f"not less than the {window}s window: {reused}"
        )

    started = time.monotonic()
    _poll(
        ctx,
        _node_absent(kubectl, evidence, node),
        timeout=window + RECLAIM_MARGIN_SECONDS,
        what=f"{node} was not reclaimed",
    )
    evidence["reclaim_wait_seconds"] = int(time.monotonic() - started)
    reclaimed = _poll(
        ctx,
        _closed_record(ctx, records["second"], node=node, ended_by=ENDED_BY_RECLAIMED),
        timeout=RECORD_TIMEOUT_SECONDS,
        what=f"The second Job's idle interval on {node} did not close as reclaimed",
    )
    evidence["second_record"] = reclaimed
    seconds = int(reclaimed["seconds"])
    if not window - CLOCK_SLACK_SECONDS <= seconds <= window + RECLAIM_MARGIN_SECONDS:
        raise RuntimeError(
            f"{node} sat idle {seconds}s before it was reclaimed; the {window}s window "
            f"allows {window - CLOCK_SLACK_SECONDS}s to {window + RECLAIM_MARGIN_SECONDS}s: "
            f"{reclaimed}"
        )
    if int(reclaimed.get("window_seconds") or 0) != window:
        raise RuntimeError(
            f"The record carries a {reclaimed.get('window_seconds')}s window, but the run "
            f"deployed {window}s"
        )
    evidence["deletions"] = {step: _delete_owned_job(ctx, records[step]) for step in STEPS}
    return evidence
