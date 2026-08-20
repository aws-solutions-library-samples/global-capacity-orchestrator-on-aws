"""Property test proving just-in-time safety changes prevent volume deletion."""

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from cli.volume_cleanup import (
    DeletionAuthorizationSource,
    RegionalVolumeTarget,
    VolumeAction,
    VolumeActionResult,
    VolumeCleanupRequest,
    VolumeCleanupService,
    VolumePolicy,
    VolumeReasonCode,
    VolumeSnapshot,
    evaluate_recheck,
)

_REGION = "us-east-1"
_OTHER_REGION = "eu-central-1"
_STACK = f"gco-{_REGION}"
_TAG_KEY = f"kubernetes.io/cluster/{_STACK}"
_NEAR_MISS_TAG_KEY = "kubernetes.io/cluster/gco-us-east-1-analytics"
_TARGET = RegionalVolumeTarget(
    stack_name=_STACK,
    stack_id=None,
    region=_REGION,
    cluster_name=_STACK,
    cluster_tag_key=_TAG_KEY,
)
_REQUEST = VolumeCleanupRequest(
    policy=VolumePolicy.DELETE,
    deletion_authorized=True,
    authorization_source=DeletionAuthorizationSource.DESTROY_ALL_WITH_YES,
)

_ZONE_SUFFIXES = ("a", "b", "c")
# Exact `owned` plus near-miss values that must never keep a candidate eligible.
_TAG_VALUES = ("owned", "Owned", "OWNED", " owned", "owned ", "shared", "")
# Exact `available` plus every other observable EBS state.
_STATES = ("available", "creating", "in-use", "deleting", "deleted", "error")

_VOLUME_IDS = st.from_regex(r"vol-[0-9a-f]{17}", fullmatch=True)
_INSTANCE_IDS = st.from_regex(r"i-[0-9a-f]{8}", fullmatch=True)


class _FakeEC2:
    """Return one canned just-in-time page and record every request."""

    def __init__(self, describe: object) -> None:
        self._describe = describe
        self.describe_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []

    def describe_volumes(self, **kwargs: Any) -> Any:
        self.describe_calls.append(kwargs)
        return self._describe

    def delete_volume(self, **kwargs: Any) -> dict[str, Any]:
        self.delete_calls.append(kwargs)
        return {}


def _unusable_factory(service_name: str, *, region_name: str) -> Any:
    raise AssertionError(
        f"the just-in-time recheck must reuse the supplied {service_name} client for {region_name}"
    )


def _other_volume_id(volume_id: str) -> str:
    """Return a distinct but equally well-formed volume identifier."""
    return volume_id[:-1] + ("1" if volume_id.endswith("0") else "0")


def _current_dto(
    *,
    volume_id: str,
    tag_key: str,
    tag_value: str,
    availability_zone: str,
    size_gib: int,
    state: str,
    attachment_ids: list[str],
) -> dict[str, object]:
    return {
        "VolumeId": volume_id,
        "AvailabilityZone": availability_zone,
        "Size": size_gib,
        "State": state,
        "Tags": [
            {"Key": "Name", "Value": "prometheus-data"},
            {"Key": tag_key, "Value": tag_value},
        ],
        "Attachments": [{"InstanceId": instance_id} for instance_id in attachment_ids],
    }


@settings(max_examples=200, deadline=None)
@given(
    volume_id=_VOLUME_IDS,
    initial_zone_suffix=st.sampled_from(_ZONE_SUFFIXES),
    current_zone_suffix=st.sampled_from(_ZONE_SUFFIXES),
    size_gib=st.integers(min_value=1, max_value=16384),
    identity_changed=st.booleans(),
    zone_in_target_region=st.booleans(),
    tag_key_present=st.booleans(),
    current_tag_value=st.sampled_from(_TAG_VALUES),
    current_state=st.sampled_from(_STATES),
    current_attachment_ids=st.lists(_INSTANCE_IDS, max_size=3, unique=True),
)
def test_just_in_time_changes_prevent_deletion(
    volume_id: str,
    initial_zone_suffix: str,
    current_zone_suffix: str,
    size_gib: int,
    identity_changed: bool,
    zone_in_target_region: bool,
    tag_key_present: bool,
    current_tag_value: str,
    current_state: str,
    current_attachment_ids: list[str],
) -> None:
    # Feature: cleanup-dynamic-ebs-volumes, Property 5: Just-in-time changes prevent deletion
    # **Validates: Requirements 4.5, 4.6**
    initial = VolumeSnapshot(
        volume_id=volume_id,
        region=_REGION,
        availability_zone=f"{_REGION}{initial_zone_suffix}",
        size_gib=size_gib,
        state="available",
        cluster_tag_value="owned",
        attachment_ids=(),
    )
    current_volume_id = _other_volume_id(volume_id) if identity_changed else volume_id
    current_region = _REGION if zone_in_target_region else _OTHER_REGION
    current_zone = f"{current_region}{current_zone_suffix}"
    current = VolumeSnapshot(
        volume_id=current_volume_id,
        region=current_region,
        availability_zone=current_zone,
        size_gib=size_gib,
        state=current_state,
        cluster_tag_value=current_tag_value if tag_key_present else None,
        attachment_ids=tuple(current_attachment_ids),
    )

    unchanged_and_valid = (
        current_volume_id == volume_id
        and current_region == _REGION
        and current_zone == initial.availability_zone
        and tag_key_present
        and current_tag_value == "owned"
        and current_state == "available"
        and not current_attachment_ids
    )

    # Requirement 4.5: every deletion predicate is re-evaluated just in time.
    evaluation = evaluate_recheck(initial, current, target=_TARGET)
    assert evaluation.eligible is unchanged_and_valid
    if unchanged_and_valid:
        assert evaluation.changed_facts == ()
        assert evaluation.reason is None
    else:
        # Requirement 4.6: every changed safety fact is reported with its values.
        assert evaluation.changed_facts
        assert evaluation.reason
        expected_facts = {
            "volume-identity": current_volume_id != volume_id,
            "region": current_region != _REGION,
            "availability-zone": current_zone != initial.availability_zone,
            "cluster-tag-value": not tag_key_present or current_tag_value != "owned",
            "state": current_state != "available",
            "attachments": bool(current_attachment_ids),
        }
        assert set(evaluation.changed_facts) == {
            fact for fact, changed in expected_facts.items() if changed
        }
    # The comparison is pure: repeating it yields an identical decision.
    assert evaluate_recheck(initial, current, target=_TARGET) == evaluation

    ec2 = _FakeEC2(
        {
            "Volumes": [
                _current_dto(
                    volume_id=current_volume_id,
                    tag_key=_TAG_KEY if tag_key_present else _NEAR_MISS_TAG_KEY,
                    tag_value=current_tag_value,
                    availability_zone=current_zone,
                    size_gib=size_gib,
                    state=current_state,
                    attachment_ids=current_attachment_ids,
                )
            ]
        }
    )
    outcome = VolumeCleanupService(_unusable_factory).delete_candidate(
        ec2=ec2,
        target=_TARGET,
        request=_REQUEST,
        snapshot=initial,
    )

    # Requirement 4.5: the candidate is re-described by exact volume ID first.
    assert ec2.describe_calls == [{"VolumeIds": [volume_id]}]
    assert outcome.volume_id == volume_id
    assert outcome.observed_state == "available"
    assert outcome.attachment_ids == ()
    assert outcome.policy is VolumePolicy.DELETE

    if unchanged_and_valid:
        # A delete request happens only when every safety fact is unchanged.
        assert ec2.delete_calls == [{"VolumeId": volume_id}]
        assert outcome.action is VolumeAction.DELETE_REQUESTED
        assert outcome.action_result is VolumeActionResult.SUCCESS
        assert outcome.reason_code is VolumeReasonCode.DELETE_REQUEST_ACCEPTED
        assert outcome.recheck == current
        assert outcome.error is None
        return

    # Requirement 4.6: no changed safety fact can reach DeleteVolume.
    assert ec2.delete_calls == []
    assert outcome.reason
    assert outcome.follow_up

    in_scope = tag_key_present and zone_in_target_region
    if identity_changed:
        # A response identifying another volume is ambiguous and fails closed.
        assert outcome.action is VolumeAction.FAILED
        assert outcome.action_result is VolumeActionResult.ERROR
        assert outcome.reason_code is VolumeReasonCode.RECHECK_ERROR
        assert outcome.error is not None
    elif not in_scope:
        # A volume outside the exact tag key or target Region loses candidacy.
        assert outcome.action is VolumeAction.SKIPPED
        assert outcome.action_result is VolumeActionResult.SAFETY_PRESERVED
        assert outcome.reason_code is VolumeReasonCode.SAFETY_RECHECK_CHANGED
        assert outcome.recheck is None
    else:
        assert outcome.action is VolumeAction.SKIPPED
        assert outcome.action_result is VolumeActionResult.SAFETY_PRESERVED
        assert outcome.reason_code is VolumeReasonCode.SAFETY_RECHECK_CHANGED
        assert outcome.recheck == current
        for fact in evaluation.changed_facts:
            assert fact in outcome.reason
