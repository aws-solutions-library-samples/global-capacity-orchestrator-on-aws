"""Independent post-destroy EBS observation, policy assertions, and residuals.

The destroy step already gates teardown completion on the ``ebs-volumes``
callbacks the command under test published. That is evidence *from* the code
being validated, so it cannot also be the proof that the code was right. This
module is the independent half: it re-describes the exact volume IDs that were
checkpointed while the cluster still existed and decides, from live EC2 facts,
whether the selected policy actually happened.

What each case must show:

* **retain-override** — every recorded volume still exists and still carries its
  exact recorded cluster-tag value, and the published record for it is a
  retention (a preservation result for anything that was not deletion-eligible).
* **delete** — every recorded volume that the pre-destroy inventory showed as
  owned, ``available``, and detached is *absent*, while every ineligible volume
  remains and its published record is a safety preservation. Absence is proved
  only by the exact EC2 not-found error; an unreadable volume is a failure, not
  an absence.

The same independent observation is reused twice more: ``cleanup/volume_fixtures``
deletes only volumes this module found still present, and
:func:`volume_residual_inventory` re-runs the observation during final inventory
so a fixture cleanup that did not finish fails the run instead of leaving
unaccounted storage cost behind.

Deletes nothing. Every observation here is a ``DescribeVolumes`` call for one
exact volume ID inside one exact target Region.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from cli.volume_cleanup import (
    OWNED_CLUSTER_TAG_VALUE,
    RegionalVolumeTarget,
    VolumeCleanupRequest,
)

from ..models import RunContext, utc_now
from ..ownership.volume_requests import persisted_target_outcomes
from ..ownership.volume_targets import (
    STRICT_DESTROY_TARGETS_KEY,
    strict_volume_cleanup_targets,
)
from ..ownership.volumes import (
    FIXTURE_CLEANUP_KEY,
    PRE_DESTROY_INVENTORY_KEY,
    _authorize_volume_scenario,
    _volume_scenario_state,
)
from ..volume_scenario import VolumeScenarioCase, validated_volume_scenario_case
from .volumes import describe_recorded_volumes

#: Checkpoint key under ``volume_scenario`` holding this run's independent
#: post-destroy observations. Stable by contract: the fixture cleanup and the
#: final-inventory residual accounting both read it.
POST_DESTROY_OBSERVATIONS_KEY = "post_destroy_observations"

#: The only observation reason that *proves* a recorded volume is gone. Anything
#: else — a describe error, a lost tag, an ambiguous response — leaves absence
#: unproven, which fails closed.
_ABSENCE_REASON_CODE = "ebs-volume-absent"

#: EBS states that show an accepted deletion is already in flight, so the volume
#: is accounted for rather than residual.
_DELETING_STATES = frozenset({"deleting", "deleted"})

#: Published records a retained volume may carry. Retention under the retain
#: policy is ``success``; retention that a safety predicate forced is
#: ``safety-preserved``.
_RETAINED_ACTIONS = frozenset({"retained"})
_RETAINED_RESULTS = frozenset({"success", "safety-preserved"})

#: Published records an ineligible volume must carry under either policy.
_PRESERVED_ACTIONS = frozenset({"retained", "skipped"})
_PRESERVED_RESULTS = frozenset({"safety-preserved"})

#: Published records an eligible volume must carry under authorized deletion.
_DISPOSED_ACTIONS = frozenset({"delete-requested", "already-absent"})
_DISPOSED_RESULTS = frozenset({"success", "idempotent-success"})


def _region_inventory(
    state: Mapping[str, Any],
    *,
    target: RegionalVolumeTarget,
) -> Mapping[str, Any] | None:
    """Return one Region's checkpointed pre-destroy inventory evidence."""
    inventory = state.get(PRE_DESTROY_INVENTORY_KEY)
    regions = inventory.get("regions") if isinstance(inventory, Mapping) else None
    evidence = regions.get(target.region) if isinstance(regions, Mapping) else None
    return evidence if isinstance(evidence, Mapping) else None


def _recorded_volumes(evidence: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Return the recorded pre-destroy facts for one Region, by volume ID."""
    raw = evidence.get("volumes")
    records: dict[str, Mapping[str, Any]] = {}
    if not isinstance(raw, list):
        return records
    for volume in raw:
        if not isinstance(volume, Mapping):
            continue
        volume_id = volume.get("volume_id")
        if isinstance(volume_id, str) and volume_id:
            records[volume_id] = volume
    return records


def _published_volumes(details: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    """Return one target's published per-volume records, by volume ID."""
    raw = details.get("volumes") if isinstance(details, Mapping) else None
    records: dict[str, Mapping[str, Any]] = {}
    if not isinstance(raw, list):
        return records
    for record in raw:
        if not isinstance(record, Mapping):
            continue
        volume_id = record.get("volume_id")
        if isinstance(volume_id, str) and volume_id:
            records[volume_id] = record
    return records


def _recorded_eligibility(record: Mapping[str, Any]) -> tuple[bool, tuple[str, ...]]:
    """Return whether the recorded facts made one volume deletion-eligible.

    Eligibility is read from the pre-destroy evidence this run captured itself,
    never from the outcome the command under test published, so the assertion
    cannot agree with a wrong decision by construction.
    """
    reasons: list[str] = []
    if record.get("cluster_tag_value") != OWNED_CLUSTER_TAG_VALUE:
        reasons.append("ownership-safety")
    if record.get("state") != "available":
        reasons.append("state-not-available")
    if record.get("attachment_ids"):
        reasons.append("attachments-present")
    return not reasons, tuple(reasons)


def _presence_problems(
    *,
    volume_id: str,
    target: RegionalVolumeTarget,
    recorded: Mapping[str, Any],
    observation: Mapping[str, Any] | None,
    must_remain: bool,
) -> list[str]:
    """Compare the independent EC2 observation with what the policy requires."""
    if observation is None:
        return [f"{volume_id} was never independently observed after destruction"]
    present = bool(observation.get("observed"))
    if must_remain:
        if not present:
            return [
                f"{volume_id} must still exist with its exact {target.cluster_tag_key} tag, but "
                + str(observation.get("reason") or "it could not be observed")
            ]
        recorded_tag = recorded.get("cluster_tag_value")
        if observation.get("cluster_tag_value") != recorded_tag:
            return [
                f"{volume_id} carries {target.cluster_tag_key} value "
                f"{observation.get('cluster_tag_value')!r}, not the recorded {recorded_tag!r}"
            ]
        return []
    if present:
        return [
            f"{volume_id} was deletion-eligible under the delete policy but still exists in "
            f"state {observation.get('state')!r}"
        ]
    if observation.get("reason_code") != _ABSENCE_REASON_CODE:
        return [
            f"{volume_id} absence is unproven: "
            + str(observation.get("reason") or "EC2 returned no exact not-found evidence")
        ]
    return []


def _outcome_problems(
    *,
    volume_id: str,
    case: VolumeScenarioCase,
    eligible: bool,
    published: Mapping[str, Any] | None,
) -> list[str]:
    """Require the published record to match the safety outcome that must hold."""
    if published is None:
        return [f"volume cleanup published no per-volume outcome for {volume_id}"]
    action = published.get("action")
    result = published.get("action_result")
    if case == "retain-override":
        expected_actions = _RETAINED_ACTIONS
        expected_results = _PRESERVED_RESULTS if not eligible else _RETAINED_RESULTS
    elif eligible:
        expected_actions = _DISPOSED_ACTIONS
        expected_results = _DISPOSED_RESULTS
    else:
        expected_actions = _PRESERVED_ACTIONS
        expected_results = _PRESERVED_RESULTS
    if action in expected_actions and result in expected_results:
        return []
    return [
        f"{volume_id} was reported as {action!r}/{result!r}, not one of "
        f"{sorted(expected_actions)}/{sorted(expected_results)}"
    ]


def _verify_recorded_volume(
    *,
    case: VolumeScenarioCase,
    target: RegionalVolumeTarget,
    volume_id: str,
    recorded: Mapping[str, Any],
    observation: Mapping[str, Any] | None,
    published: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Verify one recorded volume's presence and reported safety outcome."""
    eligible, safety_reasons = _recorded_eligibility(recorded)
    must_remain = case == "retain-override" or not eligible
    problems = _presence_problems(
        volume_id=volume_id,
        target=target,
        recorded=recorded,
        observation=observation,
        must_remain=must_remain,
    )
    problems.extend(
        _outcome_problems(
            volume_id=volume_id,
            case=case,
            eligible=eligible,
            published=published,
        )
    )
    return {
        "volume_id": volume_id,
        "region": target.region,
        "cluster_tag_key": target.cluster_tag_key,
        "recorded": {
            "state": recorded.get("state"),
            "cluster_tag_value": recorded.get("cluster_tag_value"),
            "attachment_ids": list(recorded.get("attachment_ids") or []),
            "size_gib": recorded.get("size_gib"),
        },
        "recorded_eligible": eligible,
        "recorded_safety_reasons": list(safety_reasons),
        "expected_presence": "present" if must_remain else "absent",
        "observed_present": bool(observation.get("observed")) if observation else False,
        "observation": copy.deepcopy(dict(observation)) if observation else None,
        "published_action": published.get("action") if published else None,
        "published_action_result": published.get("action_result") if published else None,
        "published_reason_code": published.get("reason_code") if published else None,
        "status": "failed" if problems else "verified",
        "problems": problems,
    }


def _verify_target(
    ctx: RunContext,
    *,
    case: VolumeScenarioCase,
    target: RegionalVolumeTarget,
    state: Mapping[str, Any],
    published: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Independently verify one exact regional target's recorded volumes."""
    evidence = _region_inventory(state, target=target)
    if evidence is None:
        return {
            "stack_name": target.stack_name,
            "region": target.region,
            "cluster_tag_key": target.cluster_tag_key,
            "recorded_volume_count": 0,
            "volumes": [],
            "status": "failed",
            "problems": [
                f"no pre-destroy volume inventory is checkpointed for {target.region}, so no "
                "recorded volume can be verified independently"
            ],
        }

    recorded = _recorded_volumes(evidence)
    observations = describe_recorded_volumes(
        ctx.session,
        target=target,
        volume_ids=sorted(recorded),
    )
    published_volumes = _published_volumes(published)
    volumes = [
        _verify_recorded_volume(
            case=case,
            target=target,
            volume_id=volume_id,
            recorded=recorded[volume_id],
            observation=observations.get(volume_id),
            published=published_volumes.get(volume_id),
        )
        for volume_id in sorted(recorded)
    ]
    problems = [problem for volume in volumes for problem in volume["problems"]]
    if published is None:
        problems.insert(
            0,
            f"volume cleanup published no outcome for {target.stack_name}",
        )
    return {
        "stack_name": target.stack_name,
        "region": target.region,
        "cluster_tag_key": target.cluster_tag_key,
        "published_status": published.get("status") if published else None,
        "recorded_volume_count": len(recorded),
        "volumes": volumes,
        "status": "failed" if problems else "verified",
        "problems": problems,
    }


def verify_post_destroy_volume_outcomes(
    ctx: RunContext,
    *,
    request: VolumeCleanupRequest | None,
    targets: Mapping[str, RegionalVolumeTarget],
    destroy_sequence: int,
) -> dict[str, Any]:
    """Prove the selected policy happened, from live EC2 facts, per recorded volume.

    Describes only the exact volume IDs checkpointed before deletion, inside their
    own target Region, and compares the result with what the case requires:
    retention with the exact recorded tag for ``retain-override``, absence for
    every eligible volume plus preserved ineligible volumes for ``delete``. Each
    target's observations are persisted as they are made and the whole record is
    durable before this returns.

    Raises:
        RuntimeError: If any recorded volume's presence or reported safety outcome
            disagrees with the case. The failing evidence is persisted first.
    """
    if request is None:
        return {
            "status": "skipped",
            "case": ctx.settings.volume_scenario_case,
            "reason": (
                "No E2E volume scenario is selected, so teardown records no volume "
                "identity to observe independently"
            ),
        }

    case = validated_volume_scenario_case(ctx.settings.volume_scenario_case)
    # Deliberately authorized without a stack target: the exact stack identity was
    # authorized while the stack still existed (see ownership/volume_targets.py),
    # and by this point it is gone. Identity fencing here is the checkpoint
    # identity, account, and branch, plus the fact that only checkpointed volume
    # IDs are ever described.
    _authorize_volume_scenario(ctx, action=f"post-destroy-observation:{case}")
    state = _volume_scenario_state(ctx)
    published = persisted_target_outcomes(ctx, destroy_sequence=destroy_sequence)

    observations: dict[str, Any] = {
        "status": "observing",
        "case": case,
        "policy": str(request.policy),
        "destroy_sequence": destroy_sequence,
        "started_at": utc_now(),
        "targets": {},
        "problems": [],
    }
    with ctx.state_lock:
        state[POST_DESTROY_OBSERVATIONS_KEY] = observations
        ctx.persist_callback(ctx.checkpoint)

    entries: dict[str, Any] = observations["targets"]
    problems: list[str] = []
    if not targets:
        problems.append("teardown captured no exact regional volume target to verify")
    for stack_name in sorted(targets):
        entry = _verify_target(
            ctx,
            case=case,
            target=targets[stack_name],
            state=state,
            published=published.get(stack_name),
        )
        with ctx.state_lock:
            entries[stack_name] = entry
            ctx.persist_callback(ctx.checkpoint)
        problems.extend(f"{stack_name}: {problem}" for problem in entry["problems"])

    with ctx.state_lock:
        observations["problems"] = problems
        observations["status"] = "failed" if problems else "verified"
        observations["completed_at"] = utc_now()
        ctx.persist_callback(ctx.checkpoint)
    if problems:
        raise RuntimeError(
            f"Independent post-destroy observation contradicts the {case} volume policy:\n  "
            + "\n  ".join(problems)
        )
    return observations


def _captured_targets(
    state: Mapping[str, Any],
) -> dict[str, tuple[RegionalVolumeTarget, tuple[str, ...]]]:
    """Return each complete strict target with the volume IDs it recorded."""
    capture = state.get(STRICT_DESTROY_TARGETS_KEY)
    if not isinstance(capture, Mapping):
        return {}
    targets = strict_volume_cleanup_targets(capture)
    raw_targets = capture.get("targets")
    entries = raw_targets if isinstance(raw_targets, Mapping) else {}
    resolved: dict[str, tuple[RegionalVolumeTarget, tuple[str, ...]]] = {}
    for stack_name, target in targets.items():
        entry = entries.get(stack_name)
        raw_ids = entry.get("recorded_volume_ids") if isinstance(entry, Mapping) else None
        volume_ids = tuple(
            sorted(str(value) for value in raw_ids if isinstance(value, str) and value)
            if isinstance(raw_ids, list)
            else ()
        )
        resolved[stack_name] = (target, volume_ids)
    return resolved


def _residual_entry(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Classify one recorded volume as absent, deleting, residual, or unresolved."""
    volume_id = str(observation.get("volume_id") or "")
    if not observation.get("observed"):
        if observation.get("reason_code") == _ABSENCE_REASON_CODE:
            return {
                "volume_id": volume_id,
                "disposition": "absent",
                "reason": str(observation.get("reason") or "EC2 reports the volume is gone"),
            }
        return {
            "volume_id": volume_id,
            "disposition": "unresolved",
            "reason": str(
                observation.get("reason") or "the volume could not be observed to prove absence"
            ),
        }
    state = str(observation.get("state") or "")
    disposition = "deleting" if state in _DELETING_STATES else "residual"
    return {
        "volume_id": volume_id,
        "disposition": disposition,
        "state": state,
        "size_gib": observation.get("size_gib"),
        "availability_zone": observation.get("availability_zone"),
        "cluster_tag_value": observation.get("cluster_tag_value"),
        "attachment_ids": list(observation.get("attachment_ids") or []),
        "reason": (
            "an accepted deletion is still in flight"
            if disposition == "deleting"
            else "the volume still exists and continues to incur storage cost"
        ),
    }


def volume_residual_inventory(ctx: RunContext) -> dict[str, Any]:
    """Account for every recorded volume at final inventory, from live EC2 facts.

    Final inventory is where an incomplete validation-fixture cleanup has to
    become visible: a retained fixture that was never authorized for deletion, a
    deletion that failed, or a volume whose absence cannot be proved all leave a
    residual here. Returns the accounting; the caller fails the run on it.
    """
    case = ctx.settings.volume_scenario_case
    if case == "disabled":
        return {
            "status": "skipped",
            "case": case,
            "reason": (
                "No E2E volume scenario is selected, so this run recorded no EBS volume "
                "identity to account for"
            ),
            "residual_volume_ids": [],
        }

    state = _volume_scenario_state(ctx)
    fixture_cleanup = state.get(FIXTURE_CLEANUP_KEY)
    regions: dict[str, Any] = {}
    residual: list[str] = []
    pending: list[str] = []
    recorded_count = 0
    for stack_name, (target, volume_ids) in sorted(_captured_targets(state).items()):
        observations = describe_recorded_volumes(
            ctx.session,
            target=target,
            volume_ids=volume_ids,
        )
        entries = [_residual_entry(observations[volume_id]) for volume_id in sorted(observations)]
        recorded_count += len(volume_ids)
        region_residual = [
            entry["volume_id"]
            for entry in entries
            if entry["disposition"] in {"residual", "unresolved"}
        ]
        residual.extend(region_residual)
        pending.extend(
            entry["volume_id"] for entry in entries if entry["disposition"] == "deleting"
        )
        regions[stack_name] = {
            "region": target.region,
            "cluster_tag_key": target.cluster_tag_key,
            "recorded_volume_ids": list(volume_ids),
            "volumes": entries,
            "residual_volume_ids": region_residual,
        }

    return {
        "status": "residual" if residual else "clear",
        "case": case,
        "recorded_volume_count": recorded_count,
        "regions": regions,
        "residual_volume_ids": sorted(residual),
        "pending_deletion_volume_ids": sorted(pending),
        "fixture_cleanup_status": (
            fixture_cleanup.get("status") if isinstance(fixture_cleanup, Mapping) else None
        ),
        "follow_up": (
            "Delete the reported volumes, or re-run the retain-override case with "
            "--confirm-ebs-fixture-cleanup so the harness removes its own fixtures."
        ),
        "verified_at": utc_now(),
    }


def accepted_pending_volume_deletions(residuals: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the in-flight volume deletions this run tolerated, as flat evidence.

    :func:`volume_residual_inventory` already decides which recorded volumes are
    residual and which carry an accepted deletion that EC2 has begun but not
    finished. Only the residual half fails the run; the accepted half is a real
    tolerance, and an undisclosed tolerance is exactly what the accepted-residue
    precedents exist to prevent — see ``ownership/dynamodb_streams`` for expired
    table streams and ``ownership/kms`` for keys pending deletion.

    This lifts that tolerance into the same disclosure shape those two use: one
    flat list of self-describing records, each naming the exact API observation
    that established it, always a list so ``final-inventory`` consumers see a
    uniform key whether or not anything was tolerated. Deletes nothing and makes
    no AWS call — it reprojects the observation the caller already paid for, so
    the disclosure cannot disagree with the accounting it came from.
    """
    accepted: list[dict[str, Any]] = []
    for stack_name, region_entry in sorted((residuals.get("regions") or {}).items()):
        region = str(region_entry.get("region") or "")
        for record in region_entry.get("volumes") or []:
            if record.get("disposition") != "deleting":
                continue
            state = str(record.get("state") or "")
            accepted.append(
                {
                    "stack_name": stack_name,
                    "region": region,
                    "volume_id": str(record.get("volume_id") or ""),
                    "state": state,
                    "size_gib": record.get("size_gib"),
                    "availability_zone": record.get("availability_zone"),
                    "authority": f"ec2:DescribeVolumes reported state {state!r}",
                    "note": (
                        "an authorized deletion is already in flight, so the volume is "
                        "accounted for rather than residual and stops incurring storage "
                        "cost once EC2 finishes releasing it"
                    ),
                }
            )
    return accepted
