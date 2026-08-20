"""Command-equivalent volume-cleanup requests and durable destroy evidence.

Strict validation must exercise the *operator's* commands, not a private
harness policy, so the destroy step resolves its request through the same
command-aware resolver Click uses:

* ``retain-override`` supplies exactly the inputs of
  ``gco stacks destroy-all -y --retain-volumes``; and
* ``delete`` supplies exactly the inputs of ``gco stacks destroy-all -y`` with
  ``delete_volumes=False``, which is what proves the implicit-delete path needs
  neither a ``--delete-volumes`` flag nor a second volume confirmation.

Both properties are asserted rather than assumed: a case that resolves to a
policy other than the one its command means, that would require an interactive
volume prompt, or that would need the delete flag, raises before teardown
begins. The one resolved request is then handed to ``destroy_orchestrated`` so
every exact regional target of the run shares it.

The second half of this module is the teardown barrier for the evidence side of
the same contract. ``StackManager`` publishes one ``ebs-volumes`` cleanup
outcome per exact regional target; :func:`verify_volume_cleanup_evidence` reads
those callbacks back out of the *persisted* checkpoint and refuses to let
teardown be marked complete unless there is exactly one outcome per captured
strict target, each carrying this run's policy and authorization source. A
missing, duplicated, foreign, or mismatched callback is persisted as blocked
evidence and then raised, so the run fails visibly with its evidence intact.

Holds no AWS state: everything here reads checkpointed evidence or pure policy.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from cli.volume_cleanup import (
    DeletionAuthorizationSource,
    DestroyCommandKind,
    VolumeCleanupRequest,
    VolumePolicy,
    resolve_volume_cleanup_request,
)

from ..models import RunContext, utc_now
from ..volume_scenario import VolumeScenarioCase, validated_volume_scenario_case
from .volumes import _authorize_volume_scenario, _volume_scenario_state

#: Checkpoint key under ``volume_scenario`` holding the resolved destroy-time
#: request and the exact command it is equivalent to. Stable by contract: the
#: post-destroy assertions read the policy they must measure against from here.
STRICT_DESTROY_REQUEST_KEY = "strict_destroy_request"

#: Checkpoint key under ``volume_scenario`` holding the durable per-target
#: ``ebs-volumes`` callback barrier that gates teardown completion.
STRICT_DESTROY_CLEANUP_EVIDENCE_KEY = "strict_destroy_cleanup_evidence"

#: Stable cleanup-report name every regional volume outcome is published under.
VOLUME_CLEANUP_CALLBACK_NAME = "ebs-volumes"

#: Checkpoint list the destroy action appends every persisted cleanup callback
#: to. Read-only here; the action owns writing it.
DESTROY_HELPER_OUTCOMES_KEY = "destroy_helper_outcomes"


@dataclass(frozen=True)
class StrictVolumeCommandInputs:
    """Exactly the ``gco stacks destroy-all`` options one case stands for."""

    command: DestroyCommandKind
    yes: bool
    retain_volumes: bool
    delete_volumes: bool

    @property
    def command_line(self) -> str:
        """Return the operator-visible command these inputs are equivalent to."""
        parts = ["gco", "stacks", str(self.command)]
        if self.yes:
            parts.append("-y")
        if self.retain_volumes:
            parts.append("--retain-volumes")
        if self.delete_volumes:
            parts.append("--delete-volumes")
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """Return the stable mapping persisted as destroy evidence."""
        return {
            "command": str(self.command),
            "yes": self.yes,
            "retain_volumes": self.retain_volumes,
            "delete_volumes": self.delete_volumes,
            "command_line": self.command_line,
        }


#: The exact resolver inputs each live case supplies. ``delete_volumes`` is
#: ``False`` in both: the retain case overrides with ``--retain-volumes`` and the
#: delete case relies on the implicit authorization of ``destroy-all -y``.
_CASE_COMMAND_INPUTS: dict[VolumeScenarioCase, StrictVolumeCommandInputs] = {
    "retain-override": StrictVolumeCommandInputs(
        command=DestroyCommandKind.ALL,
        yes=True,
        retain_volumes=True,
        delete_volumes=False,
    ),
    "delete": StrictVolumeCommandInputs(
        command=DestroyCommandKind.ALL,
        yes=True,
        retain_volumes=False,
        delete_volumes=False,
    ),
}

#: The request each case's command must resolve to. Asserted, not assumed, so a
#: change in command semantics fails the harness instead of silently changing
#: what the live run proves.
_CASE_EXPECTED_REQUEST: dict[VolumeScenarioCase, VolumeCleanupRequest] = {
    "retain-override": VolumeCleanupRequest(
        policy=VolumePolicy.RETAIN,
        deletion_authorized=False,
        authorization_source=DeletionAuthorizationSource.NONE,
    ),
    "delete": VolumeCleanupRequest(
        policy=VolumePolicy.DELETE,
        deletion_authorized=True,
        authorization_source=DeletionAuthorizationSource.DESTROY_ALL_WITH_YES,
    ),
}


def strict_volume_command_inputs(case: object) -> StrictVolumeCommandInputs:
    """Return the destroy-all options one live scenario case is equivalent to."""
    validated = validated_volume_scenario_case(case)
    inputs = _CASE_COMMAND_INPUTS.get(validated)
    if inputs is None:
        raise ValueError(
            f"Volume scenario case {validated!r} exercises no destroy command; select "
            "retain-override or delete"
        )
    return inputs


def _requested_policy_evidence(
    *,
    case: VolumeScenarioCase,
    inputs: StrictVolumeCommandInputs,
    request: VolumeCleanupRequest,
) -> dict[str, Any]:
    """Return the durable record of what command semantics this run exercises."""
    return {
        "status": "resolved",
        "case": case,
        "command_line": inputs.command_line,
        "inputs": inputs.to_dict(),
        # Both are recorded as evidence, not as configuration: the implicit
        # delete case must need neither of them.
        "delete_flag_supplied": inputs.delete_volumes,
        "volume_confirmation_required": False,
        "policy": str(request.policy),
        "deletion_authorized": request.deletion_authorized,
        "authorization_source": str(request.authorization_source),
        "resolved_at": utc_now(),
    }


def resolve_strict_volume_cleanup_request(
    ctx: RunContext,
) -> tuple[VolumeCleanupRequest | None, dict[str, Any]]:
    """Resolve and checkpoint this run's one command-equivalent cleanup request.

    Returns the request every exact regional target of this teardown shares and
    the durable evidence of the command it came from. A disabled scenario
    returns ``None``, which keeps teardown on its existing stack-only path with
    no EBS discovery or deletion at all.

    Raises:
        RuntimeError: If the case would need ``--delete-volumes`` or an
            interactive volume confirmation, or if its command does not resolve
            to the policy that case exists to prove.
    """
    case = validated_volume_scenario_case(ctx.settings.volume_scenario_case)
    if case == "disabled":
        return None, {
            "status": "skipped",
            "case": case,
            "reason": (
                "No E2E volume scenario is selected, so teardown resolves no volume "
                "policy and performs no EBS discovery or deletion"
            ),
        }

    inputs = strict_volume_command_inputs(case)
    if inputs.delete_volumes:
        raise RuntimeError(
            f"Volume scenario case {case!r} must not supply --delete-volumes; "
            "destroy-all -y authorizes eligible deletion on its own"
        )
    _authorize_volume_scenario(ctx, action=f"strict-volume-request:{case}")

    decision = resolve_volume_cleanup_request(
        command=inputs.command,
        retain_volumes=inputs.retain_volumes,
        delete_volumes=inputs.delete_volumes,
        yes=inputs.yes,
    )
    if decision.requires_volume_confirmation:
        raise RuntimeError(
            f"{inputs.command_line!r} resolved to a pending interactive volume-deletion "
            f"confirmation for case {case!r}; the harness never answers a CLI volume prompt"
        )
    request = decision.request
    expected = _CASE_EXPECTED_REQUEST[case]
    if request != expected:
        raise RuntimeError(
            f"{inputs.command_line!r} resolved to policy {request.policy} authorized by "
            f"{request.authorization_source}, but case {case!r} exercises policy "
            f"{expected.policy} authorized by {expected.authorization_source}"
        )

    evidence = _requested_policy_evidence(case=case, inputs=inputs, request=request)
    state = _volume_scenario_state(ctx)
    with ctx.state_lock:
        state[STRICT_DESTROY_REQUEST_KEY] = evidence
        ctx.persist_callback(ctx.checkpoint)
    return request, evidence


def _persisted_cleanup_callbacks(
    ctx: RunContext,
    *,
    destroy_sequence: int,
) -> list[Mapping[str, Any]]:
    """Return this attempt's persisted ``ebs-volumes`` callbacks, in order."""
    raw = ctx.checkpoint.state.get(DESTROY_HELPER_OUTCOMES_KEY)
    if not isinstance(raw, list):
        return []
    return [
        entry
        for entry in raw
        if isinstance(entry, Mapping)
        and entry.get("name") == VOLUME_CLEANUP_CALLBACK_NAME
        and entry.get("destroy_sequence") == destroy_sequence
    ]


def persisted_target_outcomes(
    ctx: RunContext,
    *,
    destroy_sequence: int,
) -> dict[str, dict[str, Any]]:
    """Return one durable published target outcome per stack for one attempt.

    Reads the same persisted callbacks the completion barrier verifies, so the
    independent post-destroy assertions measure against evidence that is already
    on disk rather than an in-flight publication. The first outcome per target
    wins; duplicates, foreign targets, and policy mismatches are
    :func:`verify_volume_cleanup_evidence`'s concern, not this accessor's.
    """
    outcomes: dict[str, dict[str, Any]] = {}
    for entry in _persisted_cleanup_callbacks(ctx, destroy_sequence=destroy_sequence):
        resolved = _callback_target(entry.get("details"))
        if resolved is None:
            continue
        stack_name, details = resolved
        outcomes.setdefault(stack_name, copy.deepcopy(dict(details)))
    return outcomes


def _callback_target(details: object) -> tuple[str, Mapping[str, Any]] | None:
    """Return one callback's stack name and complete details, or ``None``."""
    if not isinstance(details, Mapping):
        return None
    stack_name = details.get("stack_name")
    if not isinstance(stack_name, str) or not stack_name:
        return None
    return stack_name, details


def _target_evidence(details: Mapping[str, Any], *, at: object) -> dict[str, Any]:
    """Summarize one published target outcome for the durable evidence record."""
    counts = details.get("counts")
    return {
        "status": details.get("status"),
        "successful": details.get("successful"),
        "policy": details.get("policy"),
        "authorization_source": details.get("authorization_source"),
        "counts": copy.deepcopy(counts) if isinstance(counts, Mapping) else None,
        "published_at": at,
    }


def _policy_mismatches(
    targets: Mapping[str, Mapping[str, Any]],
    *,
    request: VolumeCleanupRequest,
) -> list[str]:
    """Return every target whose published outcome carries a foreign policy."""
    policy = str(request.policy)
    source = str(request.authorization_source)
    mismatched: list[str] = []
    for stack_name, evidence in sorted(targets.items()):
        published_policy = evidence.get("policy")
        published_source = evidence.get("authorization_source")
        if published_policy != policy or published_source != source:
            mismatched.append(
                f"{stack_name}: published policy {published_policy!r} authorized by "
                f"{published_source!r}, not this run's {policy!r} authorized by {source!r}"
            )
    return mismatched


def verify_volume_cleanup_evidence(
    ctx: RunContext,
    *,
    request: VolumeCleanupRequest | None,
    expected_stack_names: Iterable[str],
    destroy_sequence: int,
) -> dict[str, Any]:
    """Prove every ``ebs-volumes`` callback is durable before teardown completes.

    Reads the callbacks back out of the persisted checkpoint rather than trusting
    the in-flight publication, and requires exactly one outcome per captured
    strict target, each carrying this run's resolved policy. Returns the durable
    barrier record.

    Raises:
        RuntimeError: If a target's outcome is missing, duplicated, malformed,
            foreign to this run's targets, or published under another policy.
            The blocked evidence is persisted before the raise.
    """
    if request is None:
        return {
            "status": "skipped",
            "destroy_sequence": destroy_sequence,
            "reason": (
                "No E2E volume scenario is selected, so teardown publishes no "
                f"{VOLUME_CLEANUP_CALLBACK_NAME} outcome"
            ),
        }

    expected = {str(name) for name in expected_stack_names}
    targets: dict[str, dict[str, Any]] = {}
    duplicated: list[str] = []
    malformed: list[str] = []
    for entry in _persisted_cleanup_callbacks(ctx, destroy_sequence=destroy_sequence):
        resolved = _callback_target(entry.get("details"))
        if resolved is None:
            malformed.append(str(entry.get("at") or "unknown"))
            continue
        stack_name, details = resolved
        if stack_name in targets:
            duplicated.append(stack_name)
            continue
        targets[stack_name] = _target_evidence(details, at=entry.get("at"))

    problems: list[str] = []
    missing = sorted(expected - set(targets))
    if missing:
        problems.append(
            f"no persisted {VOLUME_CLEANUP_CALLBACK_NAME} outcome for: " + ", ".join(missing)
        )
    foreign = sorted(set(targets) - expected)
    if foreign:
        problems.append(
            "outcomes published for targets this run did not capture: " + ", ".join(foreign)
        )
    if duplicated:
        problems.append(
            "more than one outcome published for: " + ", ".join(sorted(set(duplicated)))
        )
    if malformed:
        problems.append(
            f"{len(malformed)} {VOLUME_CLEANUP_CALLBACK_NAME} callback(s) carried no "
            "target identity"
        )
    problems.extend(_policy_mismatches(targets, request=request))

    evidence: dict[str, Any] = {
        "status": "blocked" if problems else "recorded",
        "case": validated_volume_scenario_case(ctx.settings.volume_scenario_case),
        "destroy_sequence": destroy_sequence,
        "callback_name": VOLUME_CLEANUP_CALLBACK_NAME,
        "policy": str(request.policy),
        "deletion_authorized": request.deletion_authorized,
        "authorization_source": str(request.authorization_source),
        "expected_stack_names": sorted(expected),
        "targets": targets,
        "problems": problems,
        "verified_at": utc_now(),
    }
    state = _volume_scenario_state(ctx)
    with ctx.state_lock:
        state[STRICT_DESTROY_CLEANUP_EVIDENCE_KEY] = evidence
        ctx.persist_callback(ctx.checkpoint)
    if problems:
        raise RuntimeError(
            "Teardown cannot be marked complete without durable volume-cleanup "
            "evidence:\n  " + "\n  ".join(problems)
        )
    return evidence
