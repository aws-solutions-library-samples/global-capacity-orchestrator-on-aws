"""Strict destroy-time capture of exact regional EBS volume-cleanup targets.

Volume cleanup runs *after* the regional stack and its cluster are gone, but the
identity that authorizes it can only be read while they still exist. This module
is that capture step for strict validation: before any stack is deleted it turns
the checkpointed pre-destroy inventory into one complete strict target per
Region — exact CloudFormation stack ARN, Region, EKS cluster physical ID, exact
cluster tag key, and the recorded volume identities the post-destroy assertions
will be measured against — and persists it as durable evidence.

Two rules shape it:

* **Only complete strict targets travel forward.** A missing inventory, a stack
  ARN that is absent or disagrees with checkpointed ownership, a missing or
  ambiguous cluster physical ID, a tag-key mismatch, or a recorded volume
  identity that is duplicated or out of Region is captured as a blocked target
  and never becomes a cleanup target. :func:`strict_volume_cleanup_targets`
  hands the common ``StackManager`` cleanup helper only the entries that carry a
  complete identity.
* **Blocking is checkpointed before anything is asked of EKS or EC2.** Nothing
  here creates an EKS or EC2 client. Every blocked target is persisted with a
  machine-readable reason first, and only then does the capture raise, so the
  run stops with its evidence intact instead of destroying the resources the
  scenario still needed to describe.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from cli.volume_cleanup import (
    RegionalVolumeTarget,
    TargetResolutionKind,
    resolve_regional_volume_target,
)

from ..models import RunContext, utc_now
from .stacks import _owned_stack_record
from .volumes import (
    PRE_DESTROY_INVENTORY_KEY,
    _authorize_volume_scenario,
    _volume_scenario_state,
)

#: Checkpoint key under ``volume_scenario`` holding this run's captured strict
#: destroy targets. Stable by contract: the destroy step and the post-destroy
#: assertions both read it.
STRICT_DESTROY_TARGETS_KEY = "strict_destroy_targets"


def _blocked(
    *,
    stack_name: str,
    region: str,
    reason_code: str,
    reason: str,
) -> dict[str, Any]:
    """Return one blocked target entry; no EKS or EC2 request may follow it."""
    return {
        "stack_name": stack_name,
        "region": region,
        "complete": False,
        "result": "blocked",
        "reason_code": reason_code,
        "reason": reason,
        "captured_at": utc_now(),
    }


def _recorded_volume_identities(
    evidence: Mapping[str, Any],
    *,
    target: RegionalVolumeTarget,
) -> tuple[tuple[str, ...], tuple[str, str] | None]:
    """Return this Region's recorded volume IDs, or the reason they are unusable.

    The recorded identities are part of the strict target: an ambiguous,
    malformed, out-of-Region, or differently tagged record cannot authorize a
    later retain/delete assertion, so it blocks the target instead of being
    silently dropped.
    """
    raw_ids = evidence.get("volume_ids")
    raw_volumes = evidence.get("volumes")
    if not isinstance(raw_ids, list) or not isinstance(raw_volumes, list):
        return (), (
            "recorded-volume-identities-malformed",
            f"The pre-destroy inventory for {target.stack_name} recorded no volume identity list",
        )

    ids: list[str] = []
    for value in raw_ids:
        if not isinstance(value, str) or not value:
            return (), (
                "recorded-volume-identities-malformed",
                f"The pre-destroy inventory for {target.stack_name} recorded a volume "
                f"identity that is not an exact volume ID: {value!r}",
            )
        ids.append(value)
    duplicates = sorted(value for value, count in Counter(ids).items() if count > 1)
    if duplicates:
        return (), (
            "recorded-volume-identity-ambiguous",
            f"The pre-destroy inventory for {target.stack_name} recorded ambiguous volume "
            f"identities: {', '.join(duplicates)}",
        )

    observed: set[str] = set()
    for volume in raw_volumes:
        if not isinstance(volume, Mapping):
            return (), (
                "recorded-volume-identities-malformed",
                f"The pre-destroy inventory for {target.stack_name} recorded a malformed volume",
            )
        volume_id = volume.get("volume_id")
        if not isinstance(volume_id, str) or volume_id not in ids or volume_id in observed:
            return (), (
                "recorded-volume-identity-ambiguous",
                f"The pre-destroy inventory for {target.stack_name} recorded volume "
                f"{volume_id!r} outside its own identity list",
            )
        if volume.get("region") != target.region:
            return (), (
                "recorded-volume-outside-target-region",
                f"Recorded volume {volume_id} lies in {volume.get('region')!r}, not "
                f"{target.region!r}",
            )
        if volume.get("cluster_tag_key") != target.cluster_tag_key:
            return (), (
                "recorded-volume-tag-mismatch",
                f"Recorded volume {volume_id} carries tag key "
                f"{volume.get('cluster_tag_key')!r}, not {target.cluster_tag_key!r}",
            )
        observed.add(volume_id)
    if observed != set(ids):
        missing = sorted(set(ids) - observed)
        return (), (
            "recorded-volume-identities-incomplete",
            f"The pre-destroy inventory for {target.stack_name} recorded no observation for "
            f"{', '.join(missing)}",
        )
    return tuple(sorted(ids)), None


def _checkpointed_identity(
    ctx: RunContext,
    *,
    region: str,
    stack_name: str,
    evidence: Mapping[str, Any],
) -> tuple[dict[str, str], tuple[str, str] | None]:
    """Return the exact strict resource identity, or why it cannot be established."""
    if evidence.get("result") != "recorded":
        return {}, (
            "pre-destroy-inventory-incomplete",
            f"The pre-destroy inventory for {region}:{stack_name} is "
            f"{evidence.get('result')!r}, so no volume identity was recorded",
        )
    if evidence.get("stack_name") != stack_name or evidence.get("region") != region:
        return {}, (
            "pre-destroy-inventory-identity-mismatch",
            f"The pre-destroy inventory recorded {evidence.get('region')!r}:"
            f"{evidence.get('stack_name')!r}, not {region}:{stack_name}",
        )

    stack_id = evidence.get("stack_id")
    if not isinstance(stack_id, str) or not stack_id.startswith("arn:"):
        return {}, (
            "missing-strict-stack-arn",
            f"The pre-destroy inventory has no exact CloudFormation stack ARN for "
            f"{region}:{stack_name}",
        )
    record = _owned_stack_record(ctx, region, stack_name)
    if record is None:
        return {}, (
            "owned-stack-record-missing",
            f"No checkpointed CloudFormation ownership exists for {region}:{stack_name}",
        )
    if str(record.get("stack_id") or "") != stack_id:
        return {}, (
            "strict-stack-arn-mismatch",
            f"Checkpointed ownership for {region}:{stack_name} authorizes "
            f"{record.get('stack_id')!r}, not the recorded {stack_id!r}",
        )

    cluster_name = evidence.get("cluster_name")
    if not isinstance(cluster_name, str) or not cluster_name:
        return {}, (
            "strict-cluster-identity-unresolved",
            f"The pre-destroy inventory has no EKS cluster physical ID for {region}:{stack_name}",
        )
    return (
        {
            "stack_name": stack_name,
            "stack_id": stack_id,
            "region": region,
            "cluster_name": cluster_name,
        },
        None,
    )


def _capture_region(
    ctx: RunContext,
    *,
    region: str,
    regions_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Capture one Region's strict volume target, or the reason it is blocked."""
    stack_name = f"{ctx.config.project_name}-{region}"
    evidence = regions_evidence.get(region)
    if not isinstance(evidence, Mapping):
        return _blocked(
            stack_name=stack_name,
            region=region,
            reason_code="pre-destroy-inventory-missing",
            reason=(
                f"No pre-destroy volume inventory was checkpointed for {region}:{stack_name}; "
                "volume identities must be recorded while the cluster still exists"
            ),
        )

    identity, failure = _checkpointed_identity(
        ctx,
        region=region,
        stack_name=stack_name,
        evidence=evidence,
    )
    if failure is not None:
        return _blocked(
            stack_name=stack_name,
            region=region,
            reason_code=failure[0],
            reason=failure[1],
        )

    try:
        _authorize_volume_scenario(
            ctx,
            action=f"strict-volume-target:{region}",
            stack_name=identity["stack_name"],
            region=identity["region"],
            stack_id=identity["stack_id"],
        )
    except Exception as exc:
        return _blocked(
            stack_name=stack_name,
            region=region,
            reason_code="strict-stack-authorization-failed",
            reason=(
                f"Could not authorize the exact stack identity for {region}:{stack_name}: "
                f"{type(exc).__name__}: {exc}"
            ),
        )

    resolution = resolve_regional_volume_target(
        project_name=ctx.config.project_name,
        stack_name=stack_name,
        configured_regions=ctx.deployment_regions,
        strict=True,
        strict_resource=identity,
    )
    target = resolution.target
    if resolution.kind is not TargetResolutionKind.TARGET or target is None:
        return _blocked(
            stack_name=stack_name,
            region=region,
            reason_code=resolution.reason_code or "strict-target-unresolved",
            reason=(
                resolution.reason
                or f"Strict teardown could not authorize a volume target for {stack_name!r}"
            ),
        )
    if evidence.get("cluster_tag_key") != target.cluster_tag_key:
        return _blocked(
            stack_name=stack_name,
            region=region,
            reason_code="strict-cluster-tag-mismatch",
            reason=(
                f"The pre-destroy inventory recorded tag key "
                f"{evidence.get('cluster_tag_key')!r}, not {target.cluster_tag_key!r}"
            ),
        )

    volume_ids, volume_failure = _recorded_volume_identities(evidence, target=target)
    if volume_failure is not None:
        return _blocked(
            stack_name=stack_name,
            region=region,
            reason_code=volume_failure[0],
            reason=volume_failure[1],
        )

    return {
        "stack_name": target.stack_name,
        "stack_id": target.stack_id,
        "region": target.region,
        "cluster_name": target.cluster_name,
        "cluster_tag_key": target.cluster_tag_key,
        "recorded_volume_ids": list(volume_ids),
        "recorded_volume_count": len(volume_ids),
        "complete": True,
        "result": "captured",
        "captured_at": utc_now(),
    }


def _reject_ambiguous_volume_identities(targets: dict[str, dict[str, Any]]) -> None:
    """Block every target sharing a recorded volume ID with another target."""
    owners: dict[str, list[str]] = {}
    for stack_name, entry in targets.items():
        if not entry.get("complete"):
            continue
        for volume_id in entry.get("recorded_volume_ids") or []:
            owners.setdefault(str(volume_id), []).append(stack_name)
    shared = {
        volume_id: sorted(stack_names)
        for volume_id, stack_names in owners.items()
        if len(stack_names) > 1
    }
    for volume_id, stack_names in sorted(shared.items()):
        for stack_name in stack_names:
            entry = targets[stack_name]
            targets[stack_name] = _blocked(
                stack_name=stack_name,
                region=str(entry.get("region") or ""),
                reason_code="recorded-volume-identity-ambiguous",
                reason=(
                    f"Recorded volume {volume_id} is claimed by more than one target: "
                    + ", ".join(stack_names)
                ),
            )


def capture_strict_volume_targets(ctx: RunContext) -> dict[str, Any]:
    """Capture and checkpoint one strict volume target per Region before deletion.

    Returns the durable capture evidence. Raises before any stack is deleted, and
    therefore before any EKS or EC2 request, when a Region's target identity is
    missing or ambiguous — the blocked target and its machine-readable reason are
    persisted first so the failure is diagnosable from the checkpoint alone.
    """
    case = ctx.settings.volume_scenario_case
    if case == "disabled":
        return {
            "status": "skipped",
            "case": case,
            "reason": (
                "No E2E volume scenario is selected, so teardown captures no volume "
                "target identity and performs no EBS discovery or deletion"
            ),
        }

    state = _volume_scenario_state(ctx)
    inventory = state.get(PRE_DESTROY_INVENTORY_KEY)
    raw_regions = inventory.get("regions") if isinstance(inventory, Mapping) else None
    regions_evidence: Mapping[str, Any] = raw_regions if isinstance(raw_regions, Mapping) else {}

    capture: dict[str, Any] = {
        "status": "capturing",
        "case": case,
        "started_at": utc_now(),
        "targets": {},
        "blocked": [],
    }
    with ctx.state_lock:
        state[STRICT_DESTROY_TARGETS_KEY] = capture
        ctx.persist_callback(ctx.checkpoint)

    targets: dict[str, dict[str, Any]] = capture["targets"]
    for region in ctx.deployment_regions:
        entry = _capture_region(ctx, region=region, regions_evidence=regions_evidence)
        with ctx.state_lock:
            targets[str(entry["stack_name"])] = entry
            ctx.persist_callback(ctx.checkpoint)

    _reject_ambiguous_volume_identities(targets)
    blocked = [entry for entry in targets.values() if not entry.get("complete")]
    with ctx.state_lock:
        capture["blocked"] = blocked
        capture["status"] = "blocked" if blocked else "captured"
        capture["completed_at"] = utc_now()
        ctx.persist_callback(ctx.checkpoint)

    if blocked:
        raise RuntimeError(
            "Strict teardown cannot establish an exact volume target identity, so no EBS "
            "discovery or deletion may be attempted:\n  "
            + "\n  ".join(
                f"{entry['stack_name']}: {entry['reason_code']} ({entry['reason']})"
                for entry in blocked
            )
        )
    return capture


def strict_volume_cleanup_targets(
    capture: Mapping[str, Any],
) -> dict[str, RegionalVolumeTarget]:
    """Return only the complete strict targets the cleanup helper may act on.

    Blocked and partially recorded entries are excluded by construction, so a
    target whose identity could not be established can never reach EKS or EC2
    through this accessor.
    """
    raw_targets = capture.get("targets")
    if not isinstance(raw_targets, Mapping):
        return {}
    resolved: dict[str, RegionalVolumeTarget] = {}
    for stack_name, entry in raw_targets.items():
        if not isinstance(entry, Mapping) or not entry.get("complete"):
            continue
        fields = {
            key: entry.get(key)
            for key in ("stack_name", "stack_id", "region", "cluster_name", "cluster_tag_key")
        }
        if any(not isinstance(value, str) or not value for value in fields.values()):
            continue
        if fields["stack_name"] != stack_name:
            continue
        resolved[str(stack_name)] = RegionalVolumeTarget(
            stack_name=str(fields["stack_name"]),
            stack_id=str(fields["stack_id"]),
            region=str(fields["region"]),
            cluster_name=str(fields["cluster_name"]),
            cluster_tag_key=str(fields["cluster_tag_key"]),
        )
    return resolved
