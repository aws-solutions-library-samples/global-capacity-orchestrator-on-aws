"""Offline unit tests for EBS DTO normalization and initial safety decisions."""

from copy import deepcopy
from itertools import product

import pytest

from cli.volume_cleanup import (
    DeletionAuthorizationSource,
    RegionalVolumeTarget,
    VolumeAction,
    VolumeActionResult,
    VolumeCleanupRequest,
    VolumeNormalizationError,
    VolumePolicy,
    VolumeReasonCode,
    classify_volume,
    normalize_volume_snapshot,
)


def target() -> RegionalVolumeTarget:
    return RegionalVolumeTarget(
        stack_name="gco-us-east-1",
        stack_id=None,
        region="us-east-1",
        cluster_name="gco-us-east-1",
        cluster_tag_key="kubernetes.io/cluster/gco-us-east-1",
    )


def volume_dto() -> dict[str, object]:
    return {
        "VolumeId": "vol-0123456789abcdef0",
        "AvailabilityZone": "us-east-1a",
        "Size": 50,
        "State": "available",
        "Tags": [{"Key": target().cluster_tag_key, "Value": "owned"}],
        "Attachments": [
            {"VolumeId": "vol-0123456789abcdef0", "InstanceId": "i-02"},
            {"VolumeId": "vol-0123456789abcdef0", "InstanceId": "i-01"},
        ],
    }


def request(policy: VolumePolicy, *, authorized: bool = False) -> VolumeCleanupRequest:
    return VolumeCleanupRequest(
        policy=policy,
        deletion_authorized=authorized,
        authorization_source=(
            DeletionAuthorizationSource.EXPLICIT_DELETE_WITH_YES
            if authorized
            else DeletionAuthorizationSource.NONE
        ),
    )


def test_normalizes_exact_scope_and_attachment_identifiers():
    snapshot = normalize_volume_snapshot(volume_dto(), target=target())

    assert snapshot is not None
    assert snapshot.region == "us-east-1"
    assert snapshot.cluster_tag_value == "owned"
    assert snapshot.attachment_ids == ("i-01", "i-02")


@pytest.mark.parametrize(
    ("field", "malformed_value"),
    [
        ("VolumeId", None),
        ("VolumeId", "vol-invalid id"),
        ("AvailabilityZone", None),
        ("AvailabilityZone", " us-east-1a"),
        ("Size", True),
        ("Size", -1),
        ("Size", "50"),
        ("State", ""),
        ("Tags", None),
        ("Tags", ["not-a-tag-object"]),
        ("Tags", [{"Key": "", "Value": "owned"}]),
        ("Tags", [{"Key": "other", "Value": None}]),
        ("Attachments", None),
        ("Attachments", ["not-an-attachment-object"]),
        ("Attachments", [{"VolumeId": "vol-0123456789abcdef0"}]),
        (
            "Attachments",
            [
                {
                    "VolumeId": "vol-0123456789abcdef0",
                    "InstanceId": "invalid-instance",
                }
            ],
        ),
    ],
)
def test_rejects_malformed_aws_volume_dtos(field, malformed_value):
    dto = volume_dto()
    dto[field] = malformed_value

    with pytest.raises(VolumeNormalizationError):
        normalize_volume_snapshot(dto, target=target())


@pytest.mark.parametrize(
    ("zone", "in_scope"),
    [
        ("us-east-1a", True),
        ("us-east-1-lax-1a", True),
        ("us-east-1-wl1-bos-wlz-1", True),
        ("us-east-1", False),
        ("us-east-1aa", False),
        ("us-east-10a", False),
        ("us-west-2a", False),
    ],
)
def test_availability_zone_must_belong_to_exact_target_region(zone, in_scope):
    dto = volume_dto()
    dto["AvailabilityZone"] = zone

    snapshot = normalize_volume_snapshot(dto, target=target())

    assert (snapshot is not None) is in_scope


@pytest.mark.parametrize(
    "tag_key",
    [
        "kubernetes.io/cluster/gco-us-east-1-near",
        "Kubernetes.io/cluster/gco-us-east-1",
        "kubernetes.io/cluster/gco-us-east-1 ",
    ],
)
def test_excludes_exact_cluster_tag_key_near_misses(tag_key):
    dto = volume_dto()
    dto["Tags"] = [{"Key": tag_key, "Value": "owned"}]

    assert normalize_volume_snapshot(dto, target=target()) is None


@pytest.mark.parametrize("conflicting", [False, True])
def test_rejects_duplicate_and_conflicting_exact_tags(conflicting):
    dto = volume_dto()
    duplicate = {"Key": target().cluster_tag_key, "Value": "shared" if conflicting else "owned"}
    dto["Tags"] = [*dto["Tags"], duplicate]

    with pytest.raises(VolumeNormalizationError, match="conflicting|duplicate"):
        normalize_volume_snapshot(dto, target=target())


def test_rejects_malformed_and_conflicting_attachment_identifiers():
    duplicate = volume_dto()
    duplicate["Attachments"] = [deepcopy(duplicate["Attachments"][0])] * 2
    conflict = volume_dto()
    conflict["Attachments"] = [{"VolumeId": "vol-other", "InstanceId": "i-01"}]

    with pytest.raises(VolumeNormalizationError, match="duplicate attachment"):
        normalize_volume_snapshot(duplicate, target=target())
    with pytest.raises(VolumeNormalizationError, match="conflicts"):
        normalize_volume_snapshot(conflict, target=target())


@pytest.mark.parametrize(
    ("policy", "tag_value", "state", "attachments"),
    [
        pytest.param(policy, tag_value, state, attachments)
        for policy, tag_value, state, attachments in product(
            (VolumePolicy.RETAIN, VolumePolicy.DELETE),
            ("owned", "Owned", "shared"),
            ("available", "in-use"),
            ((), ("i-01",)),
        )
    ],
)
def test_classification_covers_every_ownership_state_attachment_combination(
    policy, tag_value, state, attachments
):
    dto = volume_dto()
    dto["Tags"] = [{"Key": target().cluster_tag_key, "Value": tag_value}]
    dto["State"] = state
    dto["Attachments"] = [{"VolumeId": dto["VolumeId"], "InstanceId": item} for item in attachments]
    snapshot = normalize_volume_snapshot(dto, target=target())
    assert snapshot is not None

    classification = classify_volume(
        snapshot,
        request(policy, authorized=policy is VolumePolicy.DELETE),
    )
    safety_reasons = tuple(
        reason
        for applies, reason in (
            (tag_value != "owned", VolumeReasonCode.OWNERSHIP_SAFETY),
            (state != "available", VolumeReasonCode.STATE_NOT_AVAILABLE),
            (bool(attachments), VolumeReasonCode.ATTACHMENTS_PRESENT),
        )
        if applies
    )

    assert classification.owned is (tag_value == "owned")
    if policy is VolumePolicy.RETAIN:
        assert not classification.delete_candidate
        assert classification.reason_codes == (*safety_reasons, VolumeReasonCode.RETAIN_POLICY)
        assert classification.outcome is not None
        assert classification.outcome.action is VolumeAction.RETAINED
        assert classification.outcome.action_result is (
            VolumeActionResult.SAFETY_PRESERVED if safety_reasons else VolumeActionResult.SUCCESS
        )
        assert classification.outcome.reason_code is (
            VolumeReasonCode.OWNERSHIP_SAFETY
            if tag_value != "owned"
            else VolumeReasonCode.RETAIN_POLICY
        )
        assert classification.outcome.reason
        assert classification.outcome.follow_up
    elif not safety_reasons:
        assert classification.delete_candidate
        assert classification.reason_codes == ()
        assert classification.outcome is None
    else:
        assert not classification.delete_candidate
        assert classification.reason_codes == safety_reasons
        assert classification.outcome is not None
        assert classification.outcome.action is VolumeAction.SKIPPED
        assert classification.outcome.action_result is VolumeActionResult.SAFETY_PRESERVED
        assert classification.outcome.reason_code is safety_reasons[0]
        assert classification.outcome.reason
        assert classification.outcome.follow_up


def test_unauthorized_delete_policy_blocks_even_an_eligible_owned_volume():
    dto = volume_dto()
    dto["Attachments"] = []
    snapshot = normalize_volume_snapshot(dto, target=target())
    assert snapshot is not None

    classification = classify_volume(snapshot, request(VolumePolicy.DELETE))

    assert not classification.delete_candidate
    assert classification.reason_codes == (VolumeReasonCode.EVALUATION_ERROR,)
    assert classification.outcome is not None
    assert classification.outcome.action is VolumeAction.SKIPPED
    assert classification.outcome.action_result is VolumeActionResult.BLOCKED
    assert classification.outcome.reason_code is VolumeReasonCode.EVALUATION_ERROR


def test_reporting_failure_is_machine_readable_and_never_weakens_safety():
    dto = volume_dto()
    dto["Tags"] = [{"Key": target().cluster_tag_key, "Value": "shared"}]
    snapshot = normalize_volume_snapshot(dto, target=target())
    assert snapshot is not None

    classification = classify_volume(
        snapshot,
        request(VolumePolicy.DELETE, authorized=True),
        reporting_error=RuntimeError("report sink unavailable"),
    )

    assert not classification.delete_candidate
    assert classification.reason_codes == (
        VolumeReasonCode.OWNERSHIP_SAFETY,
        VolumeReasonCode.ATTACHMENTS_PRESENT,
        VolumeReasonCode.REPORTING_ERROR,
    )
    assert classification.outcome is not None
    assert classification.outcome.action is VolumeAction.FAILED
    assert classification.outcome.action_result is VolumeActionResult.ERROR
    assert classification.outcome.reason_code is VolumeReasonCode.REPORTING_ERROR
    assert classification.outcome.error is not None
