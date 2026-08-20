"""Property test proving the retain policy never produces a delete action."""

from hypothesis import given, settings
from hypothesis import strategies as st

from cli.volume_cleanup import (
    DeletionAuthorizationSource,
    DestroyCommandKind,
    RegionalVolumeTarget,
    TargetVolumeCleanupOutcome,
    VolumeAction,
    VolumeActionResult,
    VolumeCleanupStatus,
    VolumePolicy,
    VolumeReasonCode,
    classify_volume,
    normalize_volume_snapshot,
    resolve_volume_cleanup_request,
)

_PROJECT = "gco"
_REGION = "us-east-1"
_STACK = f"{_PROJECT}-{_REGION}"
_TARGET = RegionalVolumeTarget(
    stack_name=_STACK,
    stack_id=None,
    region=_REGION,
    cluster_name=_STACK,
    cluster_tag_key=f"kubernetes.io/cluster/{_STACK}",
)

_TAG_VALUES = ("owned", "shared", "Owned", "OWNED", "owned ", "owned-cluster", "")
_STATES = ("available", "creating", "in-use", "deleting", "deleted", "error")

_INSTANCE_IDS = st.from_regex(r"i-[0-9a-f]{8}", fullmatch=True)


def _volume_dto(
    *,
    volume_id: str,
    zone_suffix: str,
    size_gib: int,
    state: str,
    tag_value: str,
    attachment_ids: list[str],
) -> dict[str, object]:
    return {
        "VolumeId": volume_id,
        "AvailabilityZone": f"{_REGION}{zone_suffix}",
        "Size": size_gib,
        "State": state,
        "Tags": [
            {"Key": "Name", "Value": "prometheus-data"},
            {"Key": _TARGET.cluster_tag_key, "Value": tag_value},
        ],
        "Attachments": [
            {"VolumeId": volume_id, "InstanceId": instance_id} for instance_id in attachment_ids
        ],
    }


@settings(max_examples=150, deadline=None)
@given(
    command=st.sampled_from(tuple(DestroyCommandKind)),
    yes=st.booleans(),
    volume_id=st.from_regex(r"vol-[0-9a-f]{17}", fullmatch=True),
    zone_suffix=st.sampled_from(("a", "b", "c", "-wl1-bos-wlz-1")),
    size_gib=st.integers(min_value=0, max_value=16384),
    state=st.sampled_from(_STATES),
    tag_value=st.sampled_from(_TAG_VALUES),
    attachment_ids=st.lists(_INSTANCE_IDS, max_size=3, unique=True),
)
def test_retain_policy_produces_no_delete_action(
    command: DestroyCommandKind,
    yes: bool,
    volume_id: str,
    zone_suffix: str,
    size_gib: int,
    state: str,
    tag_value: str,
    attachment_ids: list[str],
) -> None:
    # Feature: cleanup-dynamic-ebs-volumes, Property 2: Retain policy produces no delete action
    # **Validates: Requirements 1.6**
    decision = resolve_volume_cleanup_request(
        command=command,
        retain_volumes=True,
        delete_volumes=False,
        yes=yes,
    )
    assert decision.policy is VolumePolicy.RETAIN
    assert decision.requires_volume_confirmation is False
    request = decision.request
    assert request.deletion_authorized is False
    assert request.authorization_source is DeletionAuthorizationSource.NONE

    snapshot = normalize_volume_snapshot(
        _volume_dto(
            volume_id=volume_id,
            zone_suffix=zone_suffix,
            size_gib=size_gib,
            state=state,
            tag_value=tag_value,
            attachment_ids=attachment_ids,
        ),
        target=_TARGET,
    )
    assert snapshot is not None

    classification = classify_volume(snapshot, request)
    assert classification.delete_candidate is False
    assert classification.owned is (tag_value == "owned")
    assert VolumeReasonCode.RETAIN_POLICY in classification.reason_codes

    outcome = classification.outcome
    assert outcome is not None
    assert outcome.action is not VolumeAction.DELETE_REQUESTED
    assert outcome.action is VolumeAction.RETAINED
    assert outcome.policy is VolumePolicy.RETAIN
    assert outcome.recheck is None
    assert outcome.reason and outcome.follow_up

    safety_preserved = tag_value != "owned" or state != "available" or bool(attachment_ids)
    assert outcome.action_result is (
        VolumeActionResult.SAFETY_PRESERVED if safety_preserved else VolumeActionResult.SUCCESS
    )

    target_outcome = TargetVolumeCleanupOutcome(
        stack_name=_TARGET.stack_name,
        stack_id=_TARGET.stack_id,
        target_region=_TARGET.region,
        target_cluster=_TARGET.cluster_name,
        cluster_tag_key=_TARGET.cluster_tag_key,
        policy=request.policy,
        deletion_authorized=request.deletion_authorized,
        authorization_source=request.authorization_source,
        status=VolumeCleanupStatus.COMPLETED,
        volumes=(outcome,),
        successful=True,
    )
    assert target_outcome.counts.deleted == 0
    assert target_outcome.counts.retained == 1
    assert VolumeAction.DELETE_REQUESTED.value not in target_outcome.to_json()
