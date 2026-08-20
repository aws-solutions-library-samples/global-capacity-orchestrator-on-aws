"""Delete only this run's own retained validation-fixture EBS volumes.

The retain-override case exists to prove that ``gco stacks destroy-all -y
--retain-volumes`` keeps volumes alive, so a successful run *ends* with real EBS
volumes still in the account. Leaving them there would turn a passing validation
into a recurring bill, so once the retention evidence is durable this module
deletes them — as a harness fixture teardown, never as part of the cleanup
outcome the run just measured.

Four constraints make that safe:

* **Only after durable retain evidence.** The independent post-destroy
  observation must already be persisted and verified; this refuses to run
  otherwise, so a fixture deletion can never destroy the evidence that would have
  shown a retention bug.
* **Only exact checkpointed identities.** Candidates come from the observations
  of the volume IDs recorded before deletion. Nothing is discovered here, so no
  volume this run did not record can be reached.
* **Only with explicit harness authorization.** ``_authorize_volume_scenario``
  requires ``--confirm-ebs-fixture-cleanup`` and the retain-override case.
  Without it the fixtures are recorded as unresolved and final inventory fails
  the run on the residual rather than deleting anything implicitly.
* **Only through production's just-in-time checks.** Deletion goes through
  ``VolumeCleanupService.delete_candidates``, so the same recheck of identity,
  Region, exact ``owned`` tag, ``available`` state, and zero attachments — and
  the same exact not-found idempotency — applies to fixtures as to operators.

Never raises to hide a residual: an unresolved fixture is persisted and surfaced
through final inventory, which is what fails the run.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cli.volume_cleanup import (
    DeletionAuthorizationSource,
    RegionalVolumeTarget,
    VolumeCleanupRequest,
    VolumeCleanupService,
    VolumeOutcome,
    VolumePolicy,
    VolumeSnapshot,
)

from ..models import RunContext, utc_now
from ..ownership.volumes import (
    FIXTURE_CLEANUP_KEY,
    _authorize_volume_scenario,
    _volume_scenario_state,
)
from ..volume_scenario import validated_volume_scenario_case

#: The harness's own deletion request. It is authorized by
#: ``--confirm-ebs-fixture-cleanup``, which is an explicit non-interactive
#: operator decision, and it is deliberately separate from the request the run
#: measured: the retain outcome the command published stays exactly as reported.
_FIXTURE_CLEANUP_REQUEST = VolumeCleanupRequest(
    policy=VolumePolicy.DELETE,
    deletion_authorized=True,
    authorization_source=DeletionAuthorizationSource.EXPLICIT_DELETE_WITH_YES,
)

#: Terminal actions that resolve one fixture. Anything else leaves the volume in
#: the account, which final inventory reports as a residual.
_RESOLVED_ACTIONS = frozenset({"delete-requested", "already-absent"})


def _candidate_snapshot(observation: Mapping[str, Any]) -> VolumeSnapshot | None:
    """Rebuild one normalized snapshot from an independent observation record."""
    tag_value = observation.get("cluster_tag_value")
    raw_attachments = observation.get("attachment_ids")
    try:
        return VolumeSnapshot(
            volume_id=str(observation["volume_id"]),
            region=str(observation["region"]),
            availability_zone=str(observation["availability_zone"]),
            size_gib=int(observation["size_gib"]),
            state=str(observation["state"]),
            cluster_tag_value=tag_value if isinstance(tag_value, str) else None,
            attachment_ids=tuple(
                str(value)
                for value in (raw_attachments if isinstance(raw_attachments, list) else ())
            ),
        )
    except KeyError, TypeError, ValueError:
        return None


def _present_candidates(entry: Mapping[str, Any]) -> tuple[list[VolumeSnapshot], list[str]]:
    """Return the still-present recorded volumes of one target, and any problems."""
    raw_volumes = entry.get("volumes")
    volumes = raw_volumes if isinstance(raw_volumes, list) else []
    candidates: list[VolumeSnapshot] = []
    problems: list[str] = []
    for volume in volumes:
        if not isinstance(volume, Mapping) or not volume.get("observed_present"):
            continue
        observation = volume.get("observation")
        snapshot = _candidate_snapshot(observation) if isinstance(observation, Mapping) else None
        if snapshot is None:
            problems.append(
                f"{volume.get('volume_id')} could not be rebuilt from its own observation, "
                "so no fixture deletion was attempted for it"
            )
            continue
        candidates.append(snapshot)
    return candidates, problems


def _outcome_record(record: VolumeOutcome) -> dict[str, Any]:
    """Serialize one fixture-deletion outcome as stable evidence."""
    return {
        "volume_id": record.volume_id,
        "region": record.region,
        "availability_zone": record.availability_zone,
        "size_gib": record.size_gib,
        "observed_state": record.observed_state,
        "cluster_tag_value": record.cluster_tag_value,
        "attachment_ids": list(record.attachment_ids),
        "action": str(record.action),
        "action_result": str(record.action_result),
        "reason_code": None if record.reason_code is None else str(record.reason_code),
        "reason": record.reason,
        "follow_up": record.follow_up,
        "error": None if record.error is None else str(record.error.error_type),
    }


def _cleanup_target(
    ctx: RunContext,
    *,
    service: VolumeCleanupService,
    target: RegionalVolumeTarget,
    entry: Mapping[str, Any],
) -> dict[str, Any]:
    """Delete one target's still-present recorded fixtures behind the gate."""
    candidates, problems = _present_candidates(entry)
    if not candidates:
        return {
            "stack_name": target.stack_name,
            "region": target.region,
            "candidate_volume_ids": [],
            "volumes": [],
            "status": "failed" if problems else "clear",
            "problems": problems,
        }

    _authorize_volume_scenario(
        ctx,
        action=f"fixture-cleanup:{target.region}",
        fixture_cleanup=True,
    )
    records = service.delete_candidates(
        target=target,
        request=_FIXTURE_CLEANUP_REQUEST,
        candidates=candidates,
    )
    serialized = [_outcome_record(record) for record in records]
    problems.extend(
        f"{record['volume_id']} remains after fixture cleanup: {record['action']}"
        f"/{record['action_result']} ({record['reason_code']})"
        for record in serialized
        if record["action"] not in _RESOLVED_ACTIONS
    )
    return {
        "stack_name": target.stack_name,
        "region": target.region,
        "candidate_volume_ids": [snapshot.volume_id for snapshot in candidates],
        "volumes": serialized,
        "status": "failed" if problems else "cleaned",
        "problems": problems,
    }


def cleanup_validation_fixture_volumes(
    ctx: RunContext,
    *,
    request: VolumeCleanupRequest | None,
    targets: Mapping[str, RegionalVolumeTarget],
    observations: Mapping[str, Any],
) -> dict[str, Any]:
    """Delete this run's retained fixtures once their retention is proved.

    Applies only to the retain-override case, whose whole point is that volumes
    survive; the delete case's eligible volumes were disposed of by the command
    under test and its ineligible volumes are reported as residuals instead.
    Returns durable evidence in every path, including the unauthorized one, so
    final inventory can fail the run on anything still present.

    Raises:
        ValueError: If the independent post-destroy retention evidence is not
            verified and durable, because fixture deletion would then destroy the
            proof a retention bug needs.
    """
    if request is None:
        return {
            "status": "skipped",
            "case": ctx.settings.volume_scenario_case,
            "reason": (
                "No E2E volume scenario is selected, so this run created no retained "
                "validation fixture"
            ),
        }

    case = validated_volume_scenario_case(ctx.settings.volume_scenario_case)
    state = _volume_scenario_state(ctx)
    if case != "retain-override":
        evidence: dict[str, Any] = {
            "status": "skipped",
            "case": case,
            "reason": (
                "Retained-fixture cleanup belongs to the retain-override case; the delete "
                "case's eligible volumes were disposed of by the command under test and any "
                "preserved volume is reported as a final-inventory residual"
            ),
            "recorded_at": utc_now(),
        }
        with ctx.state_lock:
            state[FIXTURE_CLEANUP_KEY] = evidence
            ctx.persist_callback(ctx.checkpoint)
        return evidence

    if observations.get("status") != "verified":
        raise ValueError(
            "Retained-fixture cleanup requires durable, verified post-destroy retention "
            f"evidence; the observation record is {observations.get('status')!r}"
        )

    if not ctx.settings.confirm_ebs_fixture_cleanup:
        evidence = {
            "status": "unauthorized",
            "case": case,
            "reason": (
                "This run retained its validation volumes but did not pass "
                "--confirm-ebs-fixture-cleanup, so nothing was deleted"
            ),
            "follow_up": (
                "Delete the reported volumes manually, or re-run with "
                "--confirm-ebs-fixture-cleanup; final inventory fails on the residual"
            ),
            "recorded_at": utc_now(),
        }
        with ctx.state_lock:
            state[FIXTURE_CLEANUP_KEY] = evidence
            ctx.persist_callback(ctx.checkpoint)
        return evidence

    evidence = {
        "status": "cleaning",
        "case": case,
        "started_at": utc_now(),
        "targets": {},
        "problems": [],
    }
    with ctx.state_lock:
        state[FIXTURE_CLEANUP_KEY] = evidence
        ctx.persist_callback(ctx.checkpoint)

    raw_targets = observations.get("targets")
    target_entries = raw_targets if isinstance(raw_targets, Mapping) else {}
    service = VolumeCleanupService(
        lambda service_name, *, region_name: ctx.session.client(
            service_name,
            region_name=region_name,
        )
    )
    entries: dict[str, Any] = evidence["targets"]
    problems: list[str] = []
    for stack_name in sorted(targets):
        entry = target_entries.get(stack_name)
        result = _cleanup_target(
            ctx,
            service=service,
            target=targets[stack_name],
            entry=entry if isinstance(entry, Mapping) else {},
        )
        with ctx.state_lock:
            entries[stack_name] = result
            ctx.persist_callback(ctx.checkpoint)
        problems.extend(f"{stack_name}: {problem}" for problem in result["problems"])

    with ctx.state_lock:
        evidence["problems"] = problems
        evidence["status"] = "unresolved" if problems else "cleaned"
        evidence["completed_at"] = utc_now()
        ctx.persist_callback(ctx.checkpoint)
    return evidence
