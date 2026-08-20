"""Property test proving initial deletion classification enforces every safety predicate."""

from dataclasses import replace

from botocore.exceptions import ClientError
from hypothesis import given, settings
from hypothesis import strategies as st

from cli.volume_cleanup import (
    DeletionAuthorizationSource,
    RegionalVolumeTarget,
    VolumeAction,
    VolumeActionResult,
    VolumeCleanupRequest,
    VolumePolicy,
    VolumeReasonCode,
    VolumeSnapshot,
    classify_volume,
    normalize_volume_snapshot,
)

_PROJECT = "gco"
_REGION = "eu-west-1"
_STACK = f"{_PROJECT}-{_REGION}"
_TARGET = RegionalVolumeTarget(
    stack_name=_STACK,
    stack_id=None,
    region=_REGION,
    cluster_name=_STACK,
    cluster_tag_key=f"kubernetes.io/cluster/{_STACK}",
)

# Exact `owned` plus near-miss values that must never establish ownership.
_TAG_VALUES = ("owned", "Owned", "OWNED", " owned", "owned ", "shared", "owned-cluster", "")
# Exact `available` plus every other observable EBS state.
_STATES = ("available", "creating", "in-use", "deleting", "deleted", "error")

_INSTANCE_IDS = st.from_regex(r"i-[0-9a-f]{8}", fullmatch=True)

_REQUESTS = (
    VolumeCleanupRequest(
        policy=VolumePolicy.RETAIN,
        deletion_authorized=False,
        authorization_source=DeletionAuthorizationSource.NONE,
    ),
    VolumeCleanupRequest(
        policy=VolumePolicy.DELETE,
        deletion_authorized=False,
        authorization_source=DeletionAuthorizationSource.NONE,
    ),
    VolumeCleanupRequest(
        policy=VolumePolicy.DELETE,
        deletion_authorized=True,
        authorization_source=DeletionAuthorizationSource.DESTROY_ALL_WITH_YES,
    ),
    VolumeCleanupRequest(
        policy=VolumePolicy.DELETE,
        deletion_authorized=True,
        authorization_source=DeletionAuthorizationSource.EXPLICIT_DELETE_WITH_YES,
    ),
    VolumeCleanupRequest(
        policy=VolumePolicy.DELETE,
        deletion_authorized=True,
        authorization_source=DeletionAuthorizationSource.INTERACTIVE_VOLUME_CONFIRMATION,
    ),
)

_REPORTING_ERRORS: tuple[BaseException | None, ...] = (
    None,
    ValueError("required reporting field is missing"),
    RuntimeError("report serialization failed"),
    ClientError({"Error": {"Code": "InternalError", "Message": "reporting unavailable"}}, "Report"),
)


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
            {"Key": "Name", "Value": "alertmanager-data"},
            {"Key": _TARGET.cluster_tag_key, "Value": tag_value},
        ],
        "Attachments": [
            {"VolumeId": volume_id, "InstanceId": instance_id} for instance_id in attachment_ids
        ],
    }


@settings(max_examples=200, deadline=None)
@given(
    request=st.sampled_from(_REQUESTS),
    reporting_error=st.sampled_from(_REPORTING_ERRORS),
    volume_id=st.from_regex(r"vol-[0-9a-f]{17}", fullmatch=True),
    zone_suffix=st.sampled_from(("a", "b", "c", "-wl1-lon-wlz-1")),
    size_gib=st.integers(min_value=0, max_value=16384),
    state=st.sampled_from(_STATES),
    tag_value=st.sampled_from(_TAG_VALUES),
    missing_tag_value=st.booleans(),
    attachment_ids=st.lists(_INSTANCE_IDS, max_size=3, unique=True),
)
def test_initial_classification_enforces_every_safety_predicate(
    request: VolumeCleanupRequest,
    reporting_error: BaseException | None,
    volume_id: str,
    zone_suffix: str,
    size_gib: int,
    state: str,
    tag_value: str,
    missing_tag_value: bool,
    attachment_ids: list[str],
) -> None:
    # Feature: cleanup-dynamic-ebs-volumes, Property 4: Initial deletion classification enforces every safety predicate
    # **Validates: Requirements 4.1, 4.2, 4.3, 4.4**
    normalized = normalize_volume_snapshot(
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
    assert normalized is not None
    snapshot: VolumeSnapshot = (
        replace(normalized, cluster_tag_value=None) if missing_tag_value else normalized
    )

    classification = classify_volume(snapshot, request, reporting_error=reporting_error)

    owned = not missing_tag_value and tag_value == "owned"
    available = state == "available"
    detached = not attachment_ids
    expected_candidate = (
        request.policy is VolumePolicy.DELETE
        and request.deletion_authorized
        and reporting_error is None
        and owned
        and available
        and detached
    )

    # Requirement 4.1: deletion candidacy holds exactly under every safety predicate.
    assert classification.owned is owned
    assert classification.delete_candidate is expected_candidate

    if expected_candidate:
        assert classification.outcome is None
        assert classification.reason_codes == ()
        return

    outcome = classification.outcome
    assert outcome is not None
    assert outcome.action is not VolumeAction.DELETE_REQUESTED
    assert outcome.recheck is None
    assert outcome.policy is request.policy
    assert outcome.reason and outcome.follow_up
    assert outcome.reason_code is not None

    # Requirements 4.3 and 4.4: the observed state and attachment identifiers are reported.
    assert outcome.volume_id == snapshot.volume_id
    assert outcome.observed_state == snapshot.state
    assert outcome.attachment_ids == snapshot.attachment_ids
    assert outcome.cluster_tag_value == snapshot.cluster_tag_value

    # Requirements 4.2, 4.3, and 4.4: each applicable safety reason is preserved.
    if not owned:
        assert VolumeReasonCode.OWNERSHIP_SAFETY in classification.reason_codes
    if not available:
        assert VolumeReasonCode.STATE_NOT_AVAILABLE in classification.reason_codes
    if not detached:
        assert VolumeReasonCode.ATTACHMENTS_PRESENT in classification.reason_codes

    if reporting_error is not None:
        # A reporting failure is fail-closed and never creates a delete candidate.
        assert outcome.action is VolumeAction.FAILED
        assert outcome.action_result is VolumeActionResult.ERROR
        assert outcome.reason_code is VolumeReasonCode.REPORTING_ERROR
        assert outcome.error is not None
    elif request.policy is VolumePolicy.RETAIN:
        assert outcome.action is VolumeAction.RETAINED
        assert VolumeReasonCode.RETAIN_POLICY in classification.reason_codes
    elif not request.deletion_authorized:
        assert outcome.action is VolumeAction.SKIPPED
        assert outcome.action_result is VolumeActionResult.BLOCKED
        assert outcome.reason_code is VolumeReasonCode.EVALUATION_ERROR
    else:
        # Requirement 4.1: authorized delete preserves every volume failing a predicate.
        assert outcome.action is VolumeAction.SKIPPED
        assert outcome.action_result is VolumeActionResult.SAFETY_PRESERVED
        expected_reason = (
            VolumeReasonCode.OWNERSHIP_SAFETY
            if not owned
            else (
                VolumeReasonCode.STATE_NOT_AVAILABLE
                if not available
                else VolumeReasonCode.ATTACHMENTS_PRESENT
            )
        )
        assert outcome.reason_code is expected_reason

    # Classification is pure: repeating it yields an identical decision.
    assert classify_volume(snapshot, request, reporting_error=reporting_error) == classification
