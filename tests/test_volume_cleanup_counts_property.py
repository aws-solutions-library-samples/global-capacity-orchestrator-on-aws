"""Property test proving derived summary counts partition discovered records."""

from collections import Counter

from hypothesis import given, settings
from hypothesis import strategies as st

from cli.volume_cleanup import (
    DeletionAuthorizationSource,
    TargetVolumeCleanupOutcome,
    VolumeAction,
    VolumeActionResult,
    VolumeCleanupStatus,
    VolumeOutcome,
    VolumeOutcomeCounts,
    VolumePolicy,
    VolumeReasonCode,
    VolumeSnapshot,
)

_PROJECT = "gco"
_REGION = "us-east-1"
_STACK = f"{_PROJECT}-{_REGION}"
_CLUSTER_TAG_KEY = f"kubernetes.io/cluster/{_STACK}"

_SUCCESS_ACTIONS = frozenset(
    {VolumeAction.DELETE_REQUESTED, VolumeAction.RETAINED, VolumeAction.ALREADY_ABSENT}
)
_REASONED_ACTIONS = frozenset({VolumeAction.RETAINED, VolumeAction.SKIPPED, VolumeAction.FAILED})
_VOLUME_IDS = st.from_regex(r"vol-[0-9a-f]{17}", fullmatch=True)
_INSTANCE_IDS = st.from_regex(r"i-[0-9a-f]{8}", fullmatch=True)


def _action_result(action: VolumeAction) -> VolumeActionResult:
    if action is VolumeAction.ALREADY_ABSENT:
        return VolumeActionResult.IDEMPOTENT_SUCCESS
    if action is VolumeAction.FAILED:
        return VolumeActionResult.ERROR
    if action is VolumeAction.SKIPPED:
        return VolumeActionResult.SAFETY_PRESERVED
    if action is VolumeAction.RETAINED:
        return VolumeActionResult.SUCCESS
    return VolumeActionResult.SUCCESS


def _outcome(
    *,
    volume_id: str,
    policy: VolumePolicy,
    action: VolumeAction,
    zone_suffix: str,
    size_gib: int,
    state: str,
    tag_value: str,
    attachment_ids: tuple[str, ...],
) -> VolumeOutcome:
    deleting = action is VolumeAction.DELETE_REQUESTED
    observed_state = "available" if deleting else state
    observed_tag = "owned" if deleting else tag_value
    observed_attachments: tuple[str, ...] = () if deleting else attachment_ids
    availability_zone = f"{_REGION}{zone_suffix}"
    recheck = (
        VolumeSnapshot(
            volume_id=volume_id,
            region=_REGION,
            availability_zone=availability_zone,
            size_gib=size_gib,
            state="available",
            cluster_tag_value="owned",
            attachment_ids=(),
        )
        if deleting
        else None
    )
    reasoned = action in _REASONED_ACTIONS
    return VolumeOutcome(
        volume_id=volume_id,
        region=_REGION,
        availability_zone=availability_zone,
        size_gib=size_gib,
        observed_state=observed_state,
        cluster_tag_value=observed_tag,
        attachment_ids=observed_attachments,
        policy=policy,
        action=action,
        action_result=_action_result(action),
        reason_code=VolumeReasonCode.RETAIN_POLICY if reasoned else None,
        reason="Generated terminal record for count partition validation." if reasoned else None,
        follow_up="Review the recorded volume facts before manual action." if reasoned else None,
        recheck=recheck,
    )


@st.composite
def _final_outcome_lists(
    draw: st.DrawFn,
) -> tuple[VolumePolicy, tuple[VolumeOutcome, ...]]:
    policy = draw(st.sampled_from(tuple(VolumePolicy)))
    allowed_actions = tuple(
        action
        for action in VolumeAction
        if policy is VolumePolicy.DELETE or action is not VolumeAction.DELETE_REQUESTED
    )
    volume_ids = draw(st.lists(_VOLUME_IDS, max_size=8, unique=True))
    records = tuple(
        _outcome(
            volume_id=volume_id,
            policy=policy,
            action=draw(st.sampled_from(allowed_actions)),
            zone_suffix=draw(st.sampled_from(("a", "b", "c", "-wl1-bos-wlz-1"))),
            size_gib=draw(st.integers(min_value=0, max_value=16384)),
            state=draw(st.sampled_from(("available", "in-use", "creating", "error"))),
            tag_value=draw(st.sampled_from(("owned", "shared", "Owned", ""))),
            attachment_ids=tuple(draw(st.lists(_INSTANCE_IDS, max_size=2, unique=True))),
        )
        for volume_id in volume_ids
    )
    return policy, records


@settings(max_examples=150, deadline=None)
@given(generated=_final_outcome_lists())
def test_summary_counts_partition_discovered_records(
    generated: tuple[VolumePolicy, tuple[VolumeOutcome, ...]],
) -> None:
    # Feature: cleanup-dynamic-ebs-volumes, Property 11: Summary counts partition discovered records
    #
    # **Validates: Requirements 6.3**
    policy, records = generated

    reference = Counter(record.action for record in records)
    counts = VolumeOutcomeCounts.from_outcomes(records)

    assert counts.discovered == len(records)
    assert counts.deleted == reference[VolumeAction.DELETE_REQUESTED]
    assert counts.retained == reference[VolumeAction.RETAINED]
    assert counts.skipped == reference[VolumeAction.SKIPPED]
    assert counts.already_absent == reference[VolumeAction.ALREADY_ABSENT]
    assert counts.failed == reference[VolumeAction.FAILED]
    assert (
        counts.deleted + counts.retained + counts.skipped + counts.already_absent + counts.failed
        == counts.discovered
    )
    assert counts == VolumeOutcomeCounts(
        discovered=len(records),
        deleted=reference[VolumeAction.DELETE_REQUESTED],
        retained=reference[VolumeAction.RETAINED],
        skipped=reference[VolumeAction.SKIPPED],
        already_absent=reference[VolumeAction.ALREADY_ABSENT],
        failed=reference[VolumeAction.FAILED],
    )

    completed = all(record.action in _SUCCESS_ACTIONS for record in records)
    status = VolumeCleanupStatus.COMPLETED if completed else VolumeCleanupStatus.FAILED
    target_outcome = TargetVolumeCleanupOutcome(
        stack_name=_STACK,
        stack_id=None,
        target_region=_REGION,
        target_cluster=_STACK,
        cluster_tag_key=_CLUSTER_TAG_KEY,
        policy=policy,
        deletion_authorized=policy is VolumePolicy.DELETE,
        authorization_source=(
            DeletionAuthorizationSource.DESTROY_ALL_WITH_YES
            if policy is VolumePolicy.DELETE
            else DeletionAuthorizationSource.NONE
        ),
        status=status,
        volumes=records,
        successful=completed,
    )

    assert target_outcome.counts == counts
    assert target_outcome.counts.discovered == len(target_outcome.volumes)
    serialized = target_outcome.to_dict()["counts"]
    assert serialized == {
        "discovered": counts.discovered,
        "deleted": counts.deleted,
        "retained": counts.retained,
        "skipped": counts.skipped,
        "already_absent": counts.already_absent,
        "failed": counts.failed,
    }
