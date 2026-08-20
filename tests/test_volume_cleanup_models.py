"""Focused offline tests for immutable EBS cleanup outcome models."""

import json
from dataclasses import FrozenInstanceError

import pytest
from botocore.exceptions import ClientError

from cli.volume_cleanup import (
    DeletionAuthorizationSource,
    SafeError,
    TargetVolumeCleanupOutcome,
    VolumeAction,
    VolumeActionResult,
    VolumeCleanupStatus,
    VolumeOutcome,
    VolumeOutcomeCounts,
    VolumePolicy,
    VolumeReasonCode,
    VolumeSnapshot,
    normalize_safe_error,
)


def retained(volume_id: str, *attachments: str) -> VolumeOutcome:
    return VolumeOutcome(
        volume_id=volume_id,
        region="us-east-1",
        availability_zone="us-east-1a",
        size_gib=50,
        observed_state="available",
        cluster_tag_value="owned",
        attachment_ids=tuple(attachments),
        policy=VolumePolicy.RETAIN,
        action=VolumeAction.RETAINED,
        action_result=VolumeActionResult.SUCCESS,
        reason_code=VolumeReasonCode.RETAIN_POLICY,
        reason="The selected policy retains this volume.",
        follow_up="Delete it later only after verifying that its data is no longer needed.",
    )


def terminal_outcome(volume_id: str, action: VolumeAction) -> VolumeOutcome:
    result = {
        VolumeAction.RETAINED: VolumeActionResult.SUCCESS,
        VolumeAction.SKIPPED: VolumeActionResult.SAFETY_PRESERVED,
        VolumeAction.DELETE_REQUESTED: VolumeActionResult.SUCCESS,
        VolumeAction.ALREADY_ABSENT: VolumeActionResult.IDEMPOTENT_SUCCESS,
        VolumeAction.FAILED: VolumeActionResult.ERROR,
    }[action]
    reason_code = {
        VolumeAction.RETAINED: VolumeReasonCode.RETAIN_POLICY,
        VolumeAction.SKIPPED: VolumeReasonCode.ATTACHMENTS_PRESENT,
        VolumeAction.DELETE_REQUESTED: VolumeReasonCode.DELETE_REQUEST_ACCEPTED,
        VolumeAction.ALREADY_ABSENT: VolumeReasonCode.ALREADY_ABSENT,
        VolumeAction.FAILED: VolumeReasonCode.DELETE_ERROR,
    }[action]
    recheck = (
        VolumeSnapshot(
            volume_id=volume_id,
            region="us-east-1",
            availability_zone="us-east-1a",
            size_gib=50,
            state="available",
            cluster_tag_value="owned",
            attachment_ids=(),
        )
        if action is VolumeAction.DELETE_REQUESTED
        else None
    )
    error = (
        SafeError(None, "RuntimeError", "The delete request failed safely.")
        if action is VolumeAction.FAILED
        else None
    )
    return VolumeOutcome(
        volume_id=volume_id,
        region="us-east-1",
        availability_zone="us-east-1a",
        size_gib=50,
        observed_state="available",
        cluster_tag_value="owned",
        attachment_ids=(),
        policy=VolumePolicy.DELETE,
        action=action,
        action_result=result,
        reason_code=reason_code,
        reason=f"Terminal action: {action.value}.",
        follow_up="Review the recorded outcome before taking further action.",
        recheck=recheck,
        error=error,
    )


def completed(*volumes: VolumeOutcome) -> TargetVolumeCleanupOutcome:
    return TargetVolumeCleanupOutcome(
        stack_name="gco-us-east-1",
        stack_id=None,
        target_region="us-east-1",
        target_cluster="gco-us-east-1",
        cluster_tag_key="kubernetes.io/cluster/gco-us-east-1",
        policy=VolumePolicy.RETAIN,
        deletion_authorized=False,
        authorization_source=DeletionAuthorizationSource.NONE,
        status=VolumeCleanupStatus.COMPLETED,
        volumes=volumes,
        successful=True,
    )


def test_snapshots_and_outcomes_are_immutable_and_normalize_identifier_order():
    snapshot = VolumeSnapshot(
        volume_id="vol-2",
        region="us-east-1",
        availability_zone="us-east-1a",
        size_gib=5,
        state="in-use",
        cluster_tag_value="owned",
        attachment_ids=("i-2", "i-1", "i-2"),
    )
    outcome = retained("vol-2", "i-2", "i-1", "i-2")

    assert snapshot.attachment_ids == ("i-1", "i-2")
    assert outcome.attachment_ids == ("i-1", "i-2")
    with pytest.raises(FrozenInstanceError):
        snapshot.state = "available"  # type: ignore[misc]


def test_target_derives_partition_counts_and_sorts_volume_ids():
    outcome = completed(retained("vol-z"), retained("vol-a"))

    assert [record.volume_id for record in outcome.volumes] == ["vol-a", "vol-z"]
    assert outcome.counts == VolumeOutcomeCounts(
        discovered=2,
        deleted=0,
        retained=2,
        skipped=0,
        already_absent=0,
        failed=0,
    )


def test_serialization_is_deterministic_json_compatible_and_preserves_nulls():
    first = completed(retained("vol-z"), retained("vol-a"))
    second = completed(retained("vol-a"), retained("vol-z"))

    assert first.to_json() == second.to_json()
    payload = json.loads(first.to_json())
    assert payload == first.to_dict()
    assert payload["stack_id"] is None
    assert payload["volumes"][0]["action"] == "retained"
    assert payload["counts"]["discovered"] == 2


def test_zero_volume_completed_outcome_has_derived_zero_counts():
    outcome = completed()

    assert outcome.counts == VolumeOutcomeCounts(0, 0, 0, 0, 0, 0)
    assert outcome.to_dict()["volumes"] == []


def test_safe_error_normalization_excludes_raw_response_and_redacts_credentials():
    # Assembled from fragments so this test fixture is not flagged as a real
    # hard-coded credential by secret scanners; it is a synthetic placeholder.
    access_key = "AKIA" + "ABCDEFGHIJKLMNOP"
    error = ClientError(
        {
            "Error": {
                "Code": "AccessDeniedException",
                "Message": f"password=hunter2 key={access_key}",
            },
            "ResponseMetadata": {"HTTPHeaders": {"authorization": "raw-secret"}},
        },
        "DescribeVolumes",
    )

    safe = normalize_safe_error(error)
    serialized = completed().to_json()

    assert safe == SafeError(
        error_code="AccessDeniedException",
        error_type="ClientError",
        message="password=[REDACTED] key=[REDACTED]",
    )
    assert "raw-secret" not in repr(safe)
    assert "ResponseMetadata" not in repr(safe)
    assert "raw-secret" not in serialized


def test_counts_reject_independent_values_that_do_not_partition_discovered():
    with pytest.raises(ValueError, match="partition"):
        VolumeOutcomeCounts(
            discovered=2,
            deleted=0,
            retained=1,
            skipped=0,
            already_absent=0,
            failed=0,
        )


def test_skipped_target_requires_actionable_reason_and_zero_records():
    with pytest.raises(ValueError, match="reason and zero"):
        TargetVolumeCleanupOutcome(
            stack_name="gco-us-east-1",
            stack_id=None,
            target_region="us-east-1",
            target_cluster="gco-us-east-1",
            cluster_tag_key="kubernetes.io/cluster/gco-us-east-1",
            policy=VolumePolicy.RETAIN,
            deletion_authorized=False,
            authorization_source=DeletionAuthorizationSource.NONE,
            status=VolumeCleanupStatus.SKIPPED,
            volumes=(retained("vol-1"),),
            successful=False,
        )


def test_numeric_model_fields_reject_non_integer_values():
    with pytest.raises(ValueError, match="size_gib must be a non-negative integer"):
        VolumeSnapshot(
            volume_id="vol-1",
            region="us-east-1",
            availability_zone="us-east-1a",
            size_gib=1.5,  # type: ignore[arg-type]
            state="available",
            cluster_tag_value="owned",
            attachment_ids=(),
        )

    with pytest.raises(ValueError, match="volume outcome count"):
        VolumeOutcomeCounts(
            discovered=0.0,  # type: ignore[arg-type]
            deleted=0,
            retained=0,
            skipped=0,
            already_absent=0,
            failed=0,
        )


def test_safe_error_normalization_preserves_sanitized_generic_message():
    safe = normalize_safe_error(ValueError("password=hunter2 malformed volume response"))

    assert safe == SafeError(
        error_code=None,
        error_type="ValueError",
        message="password=[REDACTED] malformed volume response",
    )


def test_serialized_outcome_contains_every_required_target_and_volume_field():
    record = terminal_outcome("vol-1", VolumeAction.DELETE_REQUESTED)
    outcome = TargetVolumeCleanupOutcome(
        stack_name="gco-us-east-1",
        stack_id="arn:aws:cloudformation:us-east-1:123456789012:stack/gco-us-east-1/id",
        target_region="us-east-1",
        target_cluster="gco-us-east-1",
        cluster_tag_key="kubernetes.io/cluster/gco-us-east-1",
        policy=VolumePolicy.DELETE,
        deletion_authorized=True,
        authorization_source=DeletionAuthorizationSource.EXPLICIT_DELETE_WITH_YES,
        status=VolumeCleanupStatus.COMPLETED,
        volumes=(record,),
        successful=True,
    )

    payload = outcome.to_dict()

    assert set(payload) == {
        "stack_name",
        "stack_id",
        "target_region",
        "target_cluster",
        "cluster_tag_key",
        "policy",
        "deletion_authorized",
        "authorization_source",
        "status",
        "blocking_reason_code",
        "blocking_reason",
        "follow_up",
        "volumes",
        "successful",
        "error",
        "counts",
    }
    assert set(payload["volumes"][0]) == {
        "volume_id",
        "region",
        "availability_zone",
        "size_gib",
        "observed_state",
        "cluster_tag_value",
        "attachment_ids",
        "policy",
        "action",
        "action_result",
        "reason_code",
        "reason",
        "follow_up",
        "recheck",
        "error",
    }
    assert payload["volumes"][0]["recheck"] == {
        "volume_id": "vol-1",
        "region": "us-east-1",
        "availability_zone": "us-east-1a",
        "size_gib": 50,
        "state": "available",
        "cluster_tag_value": "owned",
        "attachment_ids": [],
    }
    assert payload["volumes"][0]["policy"] == "delete"
    assert payload["volumes"][0]["action"] == "delete-requested"
    assert payload["volumes"][0]["action_result"] == "success"


def test_counts_are_derived_from_all_terminal_actions_and_partition_records():
    records = tuple(
        terminal_outcome(f"vol-{index}", action)
        for index, action in enumerate(VolumeAction, start=1)
    )

    outcome = TargetVolumeCleanupOutcome(
        stack_name="gco-us-east-1",
        stack_id=None,
        target_region="us-east-1",
        target_cluster="gco-us-east-1",
        cluster_tag_key="kubernetes.io/cluster/gco-us-east-1",
        policy=VolumePolicy.DELETE,
        deletion_authorized=True,
        authorization_source=DeletionAuthorizationSource.EXPLICIT_DELETE_WITH_YES,
        status=VolumeCleanupStatus.FAILED,
        volumes=tuple(reversed(records)),
        successful=False,
    )

    assert outcome.counts == VolumeOutcomeCounts(
        discovered=5,
        deleted=1,
        retained=1,
        skipped=1,
        already_absent=1,
        failed=1,
    )
    assert (
        sum(
            (
                outcome.counts.deleted,
                outcome.counts.retained,
                outcome.counts.skipped,
                outcome.counts.already_absent,
                outcome.counts.failed,
            )
        )
        == outcome.counts.discovered
        == len(outcome.volumes)
    )
    assert [record.volume_id for record in outcome.volumes] == [
        "vol-1",
        "vol-2",
        "vol-3",
        "vol-4",
        "vol-5",
    ]


@pytest.mark.parametrize(
    ("action", "result"),
    [
        (VolumeAction.RETAINED, VolumeActionResult.SUCCESS),
        (VolumeAction.SKIPPED, VolumeActionResult.SAFETY_PRESERVED),
        (VolumeAction.FAILED, VolumeActionResult.ERROR),
    ],
)
def test_non_successful_volume_actions_require_reason_and_follow_up(action, result):
    with pytest.raises(ValueError, match="actionable reason"):
        VolumeOutcome(
            volume_id="vol-1",
            region="us-east-1",
            availability_zone="us-east-1a",
            size_gib=50,
            observed_state="available",
            cluster_tag_value="owned",
            attachment_ids=(),
            policy=VolumePolicy.DELETE,
            action=action,
            action_result=result,
        )


def test_completed_zero_volume_no_op_requires_complete_target_identity():
    with pytest.raises(ValueError, match="complete target identity"):
        TargetVolumeCleanupOutcome(
            stack_name="gco-us-east-1",
            stack_id=None,
            target_region="us-east-1",
            target_cluster=None,
            cluster_tag_key="kubernetes.io/cluster/gco-us-east-1",
            policy=VolumePolicy.RETAIN,
            deletion_authorized=False,
            authorization_source=DeletionAuthorizationSource.NONE,
            status=VolumeCleanupStatus.COMPLETED,
            successful=True,
        )


def test_safe_error_normalization_redacts_bearer_and_session_credentials():
    safe = normalize_safe_error(
        RuntimeError("  request failed with Bearer abc.def-123 and session_token=super-secret  ")
    )

    assert safe.error_code is None
    assert safe.error_type == "RuntimeError"
    assert safe.message == ("request failed with Bearer [REDACTED] and session_token=[REDACTED]")
    assert "abc.def-123" not in repr(safe)
    assert "super-secret" not in repr(safe)


def test_safe_error_uses_fallback_for_malformed_client_error_metadata():
    error = ClientError(
        {
            "Error": {"Code": "invalid code with spaces", "Message": None},
            "ResponseMetadata": {"HTTPHeaders": {"authorization": "secret"}},
        },
        "DescribeVolumes",
    )

    safe = normalize_safe_error(error)

    assert safe == SafeError(
        error_code=None,
        error_type="ClientError",
        message="Operation failed with ClientError",
    )
    assert "authorization" not in repr(safe)
