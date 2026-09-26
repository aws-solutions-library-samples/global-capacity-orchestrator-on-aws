"""Put the account's Transaction Search state back the way the baseline found it.

Runs as a retained-resource cleanup phase, i.e. only after every exact target
stack is proven absent, so no GCO component is left to write a span or to
re-run the enabler. Per regional Region, in dependency order:

1. **Destination.** If the baseline destination was ``XRay`` and the Region
   now sends segments to ``CloudWatchLogs``, switch it back with
   ``UpdateTraceSegmentDestination(Destination="XRay")`` (after a bounded wait
   for a still-``PENDING`` switch to settle), then wait, bounded, for the
   switch back to become ``ACTIVE``: while it is ``PENDING``, X-Ray may still
   deliver spans through the policy into the log groups. A destination that
   was already ``CloudWatchLogs`` at baseline is never touched.
2. **Resource policy.** The ``gco-transaction-search-xray-access`` policy is
   deleted only when it did not exist at baseline, still has the X-Ray span
   delivery shape, and was last written during this run.
3. **Span log groups.** ``aws/spans`` and ``/aws/application-signals/data``
   are deleted only when they did not exist at baseline, the destination is
   ``XRay`` again (so nothing re-creates them), and the live generation was
   created during this run and carries no other owner's markers. The delete
   follows one exact identity read with nothing in between, and absence must
   then hold across repeated reads. When Transaction Search was already on at
   baseline, a new span log group is shared with every other span producer in
   the account and is retained (``final-inventory`` reports it as accepted).

These groups are service-created, carry no run tags, and are not stack
resources, so they cannot join the tag-conditioned ``owned_log_groups``
authority; the baseline absence proof plus the creation-time fence are their
ownership proof. Pre-existing state is never modified. A destination switch
that fails, or has not become ``ACTIVE`` by the deadline, stops that Region
before the policy or log groups are touched, because a Region that may still
deliver spans to ``CloudWatchLogs`` still needs both; resuming ``destroy``
retries it.
"""

from __future__ import annotations

import copy
import json
import time
from datetime import datetime
from typing import Any

from botocore.exceptions import ClientError

from ..cleanup.log_groups import _log_group_adoption_blockers
from ..models import RunContext
from ..ownership.log_groups import _observe_log_group_stability
from ..ownership.stacks import _verify_target_stack_absence
from ..ownership.transaction_search import (
    TRANSACTION_SEARCH_LOG_GROUPS,
    TRANSACTION_SEARCH_POLICY_NAME,
    _gco_resource_policy,
    _observe_transaction_search_state,
    _policy_summary,
    _read_trace_segment_destination,
    _validated_transaction_search_baseline,
)

#: Bounded wait for a ``PENDING`` destination switch to settle before undoing it.
_DESTINATION_SETTLE_SECONDS = 600
_DESTINATION_POLL_SECONDS = 15
#: Reads that must agree before a delete, and absence reads after it.
_PRESENT_OBSERVATIONS = 2
_ABSENT_OBSERVATIONS = 3


class TransactionSearchRestoreError(RuntimeError):
    """A Region could not be restored; carries the partial evidence."""

    def __init__(self, message: str, details: dict[str, Any]) -> None:
        super().__init__(message)
        self.details = copy.deepcopy(details)


def _run_started_ms(ctx: RunContext) -> int:
    try:
        return int(datetime.fromisoformat(ctx.checkpoint.created_at).timestamp() * 1000)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Checkpoint created_at is not a valid timestamp") from exc


def _settled_destination(xray: Any) -> dict[str, str]:
    """Wait (bounded) for a ``PENDING`` destination to become ``ACTIVE``."""
    deadline = time.monotonic() + _DESTINATION_SETTLE_SECONDS
    while True:
        state = _read_trace_segment_destination(xray)
        if state["status"] == "ACTIVE" or time.monotonic() >= deadline:
            return state
        time.sleep(_DESTINATION_POLL_SECONDS)


def _restore_destination(
    xray: Any,
    region: str,
    baseline: dict[str, Any],
    actions: list[dict[str, Any]],
) -> None:
    """Leave a baseline-``XRay`` Region on an ``ACTIVE`` ``XRay`` destination, or raise.

    A Region that was on ``CloudWatchLogs`` at baseline is never touched.
    """
    if baseline["destination"] != "XRay":
        return
    current = _settled_destination(xray)
    if current["destination"] == "CloudWatchLogs":
        response = xray.update_trace_segment_destination(Destination="XRay")
        actions.append(
            {
                "action": "update-trace-segment-destination",
                "from": "CloudWatchLogs",
                "to": "XRay",
                "status_before": current["status"],
                "response_status": str(response.get("Status") or ""),
            }
        )
        current = _settled_destination(xray)
    if current["destination"] != "XRay" or current["status"] != "ACTIVE":
        raise RuntimeError(
            f"{region}: trace segment destination is {current['destination']} "
            f"({current['status']}), not ACTIVE on XRay, after restoring it; the resource "
            "policy and span log groups stay until it is (resume destroy to retry)"
        )


def _restore_resource_policy(
    logs: Any,
    region: str,
    baseline: dict[str, Any],
    run_started_ms: int,
    actions: list[dict[str, Any]],
) -> None:
    """Delete the GCO policy only when this run wrote it."""
    if baseline["resource_policy"]["present"]:
        return
    policy = _gco_resource_policy(logs)
    if policy is None:
        return
    summary = _policy_summary(policy)
    last_updated = summary["last_updated_time"]
    if not summary["grants_xray_span_delivery"]:
        raise RuntimeError(
            f"{region}: resource policy {TRANSACTION_SEARCH_POLICY_NAME} does not have the "
            "X-Ray span delivery shape; refusing to delete it"
        )
    if last_updated is None or last_updated < run_started_ms:
        raise RuntimeError(
            f"{region}: resource policy {TRANSACTION_SEARCH_POLICY_NAME} was not written during "
            "this run; refusing to delete it"
        )
    logs.delete_resource_policy(policyName=TRANSACTION_SEARCH_POLICY_NAME)
    if _gco_resource_policy(logs) is not None:
        raise RuntimeError(
            f"{region}: resource policy {TRANSACTION_SEARCH_POLICY_NAME} is still present "
            "after deletion"
        )
    actions.append({"action": "delete-resource-policy", "policy": TRANSACTION_SEARCH_POLICY_NAME})


def _restore_log_group(
    ctx: RunContext,
    logs: Any,
    region: str,
    name: str,
    *,
    baseline: dict[str, Any],
    run_started_ms: int,
) -> dict[str, Any]:
    """Return one span log group's disposition, deleting it only when the run owns it.

    Only called once :func:`_restore_destination` has left a baseline-``XRay``
    Region ``ACTIVE`` on ``XRay``, so nothing re-creates a deleted group.
    """
    if baseline["log_groups"][name]["present"]:
        return {"disposition": "preexisting-untouched"}
    observed = _observe_log_group_stability(
        logs,
        region,
        name,
        required_present=_PRESENT_OBSERVATIONS,
        required_absent=_ABSENT_OBSERVATIONS,
    )
    if observed["status"] == "absent":
        return {"disposition": "absent"}
    identity = observed.get("identity")
    if observed["status"] != "present" or not isinstance(identity, dict):
        raise RuntimeError(
            f"{region}:{name} did not settle before restoration: {observed['status']}"
        )
    if baseline["destination"] != "XRay":
        # Transaction Search was already on: every span producer in the account
        # shares this group, whatever the destination reads now.
        return {"disposition": "retained-transaction-search-enabled-at-baseline"}
    if int(identity["creation_time"]) < run_started_ms:
        raise RuntimeError(f"{region}:{name} predates this validation run; refusing to delete it")
    blockers = _log_group_adoption_blockers(
        identity,
        run_id=ctx.settings.run_id,
        cleanup_token=str(ctx.checkpoint.state.get("log_group_cleanup_token") or ""),
    )
    if blockers:
        raise RuntimeError(
            f"{region}:{name} carries another owner's markers: {', '.join(blockers)}"
        )
    # Nothing may run between this exact read and the delete request.
    pre_delete = _observe_log_group_stability(
        logs,
        region,
        name,
        expected_identity=identity,
        required_present=1,
        required_absent=1,
    )
    if pre_delete["status"] == "present":
        try:
            logs.delete_log_group(logGroupName=name)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                raise
    elif pre_delete["status"] != "absent":
        raise RuntimeError(f"{region}:{name} changed immediately before deletion")
    absence = _observe_log_group_stability(
        logs,
        region,
        name,
        expected_identity=identity,
        required_present=None,
        required_absent=_ABSENT_OBSERVATIONS,
    )
    if absence["status"] != "absent":
        raise RuntimeError(f"{region}:{name} did not stay absent after deletion")
    return {
        "disposition": "deleted" if pre_delete["status"] == "present" else "absent",
        "creation_time": int(identity["creation_time"]),
    }


def _restore_region(
    ctx: RunContext,
    region: str,
    baseline: dict[str, Any],
    run_started_ms: int,
) -> dict[str, Any]:
    xray = ctx.session.client("xray", region_name=region)
    logs = ctx.session.client("logs", region_name=region)
    before = _observe_transaction_search_state(ctx, region)
    actions: list[dict[str, Any]] = []
    _restore_destination(xray, region, baseline, actions)
    _restore_resource_policy(logs, region, baseline, run_started_ms, actions)
    log_groups: dict[str, Any] = {}
    for name in TRANSACTION_SEARCH_LOG_GROUPS:
        log_groups[name] = _restore_log_group(
            ctx,
            logs,
            region,
            name,
            baseline=baseline,
            run_started_ms=run_started_ms,
        )
        if log_groups[name]["disposition"] == "deleted":
            actions.append({"action": "delete-log-group", "log_group": name})
    return {
        "baseline": copy.deepcopy(baseline),
        "before": before,
        "actions": actions,
        "log_groups": log_groups,
        "after": _observe_transaction_search_state(ctx, region),
    }


def _restore_transaction_search(ctx: RunContext) -> dict[str, Any]:
    """Restore every regional Region; one Region's failure never skips another."""
    baseline = _validated_transaction_search_baseline(ctx)
    stack_absence = _verify_target_stack_absence(ctx)
    if not stack_absence["all_absent"]:
        raise RuntimeError(
            "Transaction Search restoration requires every exact target stack to be absent"
        )
    run_started_ms = _run_started_ms(ctx)
    regions: dict[str, Any] = {}
    errors: list[dict[str, str]] = []
    for region in sorted(baseline["regions"]):
        try:
            regions[region] = _restore_region(
                ctx, region, baseline["regions"][region], run_started_ms
            )
        except Exception as exc:  # restore every other Region, then fail with all evidence
            error = f"{type(exc).__name__}: {exc}"
            regions[region] = {"error": error}
            errors.append({"region": region, "error": error})
    result = {"regions": regions, "errors": errors}
    with ctx.state_lock:
        ctx.checkpoint.state.setdefault("transaction_search_restore_attempts", []).append(
            copy.deepcopy(result)
        )
    ctx.persist()
    if errors:
        raise TransactionSearchRestoreError(
            "Transaction Search restoration failed: " + json.dumps(errors, sort_keys=True),
            result,
        )
    return result
