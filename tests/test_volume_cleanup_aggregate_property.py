"""Property test proving aggregate cleanup success reflects every volume failure."""

from dataclasses import dataclass
from typing import cast

from hypothesis import given, settings
from hypothesis import strategies as st

from cli.volume_cleanup import (
    DeletionAuthorizationSource,
    RegionalVolumeTarget,
    SafeError,
    TargetVolumeCleanupOutcome,
    VolumeAction,
    VolumeActionResult,
    VolumeCleanupRequest,
    VolumeCleanupStatus,
    VolumeDiscoveryFailure,
    VolumeOutcome,
    VolumePolicy,
    VolumeReasonCode,
    VolumeReportingError,
    VolumeSnapshot,
    aggregate_target_outcome,
    verify_volume_reporting,
)

_REGION = "us-east-1"
_STACK = f"gco-{_REGION}"
_TAG_KEY = f"kubernetes.io/cluster/{_STACK}"
_TARGET = RegionalVolumeTarget(
    stack_name=_STACK,
    stack_id=None,
    region=_REGION,
    cluster_name=_STACK,
    cluster_tag_key=_TAG_KEY,
)

_REQUESTS = (
    VolumeCleanupRequest(
        policy=VolumePolicy.RETAIN,
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
    VolumeCleanupRequest(
        policy=VolumePolicy.DELETE,
        deletion_authorized=False,
        authorization_source=DeletionAuthorizationSource.NONE,
    ),
)

_PRESERVED_ACTIONS = frozenset({VolumeAction.RETAINED, VolumeAction.SKIPPED})
_DISPOSED_ACTIONS = frozenset({VolumeAction.DELETE_REQUESTED, VolumeAction.ALREADY_ABSENT})
_SUCCESS_STATUSES = frozenset(
    {VolumeCleanupStatus.COMPLETED, VolumeCleanupStatus.COMPLETED_WITH_SAFETY_RETENTIONS}
)
_FAILURE_REASONS = (
    VolumeReasonCode.EVALUATION_ERROR,
    VolumeReasonCode.RECHECK_ERROR,
    VolumeReasonCode.DELETE_ERROR,
    VolumeReasonCode.REPORTING_ERROR,
    VolumeReasonCode.NORMALIZATION_ERROR,
)
_VOLUME_IDS = st.from_regex(r"vol-[0-9a-f]{17}", fullmatch=True)
_INSTANCE_IDS = st.from_regex(r"i-[0-9a-f]{8}", fullmatch=True)
_TAG_VALUES = ("owned", "shared", "Owned", "", None)
_STATES = ("available", "in-use", "creating", "deleting")
_REPORTING_DEFECTS = ("none", "missing-reason", "unserializable")
_REASON = "Generated terminal record for aggregate status validation."
_FOLLOW_UP = "Review the recorded volume facts before taking manual action."


@dataclass(frozen=True)
class _RecordPlan:
    """One generated terminal record plus its generated reporting state."""

    volume_id: str
    action: VolumeAction
    tag_value: str | None
    state: str
    size_gib: int
    zone_suffix: str
    attachment_ids: tuple[str, ...]
    failure_reason: VolumeReasonCode
    reporting_defect: str

    @property
    def availability_zone(self) -> str:
        return f"{_REGION}{self.zone_suffix}"

    @property
    def reporting_incomplete(self) -> bool:
        """Return whether the generated reporting state omits a required field."""
        if self.reporting_defect == "missing-reason":
            return self.action is VolumeAction.ALREADY_ABSENT
        if self.reporting_defect == "unserializable":
            return self.action is not VolumeAction.DELETE_REQUESTED
        return False


def _preserved_reason(plan: _RecordPlan) -> VolumeReasonCode:
    """Return the safety reason implied by the generated volume facts."""
    if plan.tag_value != "owned":
        return VolumeReasonCode.OWNERSHIP_SAFETY
    if plan.attachment_ids:
        return VolumeReasonCode.ATTACHMENTS_PRESENT
    if plan.state != "available":
        return VolumeReasonCode.STATE_NOT_AVAILABLE
    return (
        VolumeReasonCode.SAFETY_RECHECK_CHANGED
        if plan.action is VolumeAction.SKIPPED
        else VolumeReasonCode.RETAIN_POLICY
    )


def _record(plan: _RecordPlan, policy: VolumePolicy) -> VolumeOutcome:
    """Build one internally valid terminal record for the generated plan."""
    tag_value = plan.tag_value
    state = plan.state
    attachment_ids = plan.attachment_ids
    recheck: VolumeSnapshot | None = None
    reason_code: VolumeReasonCode | None
    error: SafeError | None = None

    if plan.action is VolumeAction.DELETE_REQUESTED:
        tag_value, state, attachment_ids = "owned", "available", ()
        recheck = VolumeSnapshot(
            volume_id=plan.volume_id,
            region=_REGION,
            availability_zone=plan.availability_zone,
            size_gib=plan.size_gib,
            state="available",
            cluster_tag_value="owned",
            attachment_ids=(),
        )
        action_result = VolumeActionResult.SUCCESS
        reason_code = VolumeReasonCode.DELETE_REQUEST_ACCEPTED
    elif plan.action is VolumeAction.ALREADY_ABSENT:
        action_result = VolumeActionResult.IDEMPOTENT_SUCCESS
        missing_reason = plan.reporting_defect == "missing-reason"
        reason_code = None if missing_reason else VolumeReasonCode.ALREADY_ABSENT
    elif plan.action is VolumeAction.FAILED:
        action_result = VolumeActionResult.ERROR
        reason_code = plan.failure_reason
        error = SafeError(
            error_code="RequestLimitExceeded",
            error_type="ClientError",
            message="injected per-volume failure",
        )
    else:
        action_result = (
            VolumeActionResult.SAFETY_PRESERVED
            if plan.action is VolumeAction.SKIPPED
            else VolumeActionResult.SUCCESS
        )
        reason_code = _preserved_reason(plan)

    if (
        plan.reporting_defect == "unserializable"
        and plan.action is not VolumeAction.DELETE_REQUESTED
    ):
        # A required reporting field that cannot be serialized for evidence.
        recheck = cast(VolumeSnapshot, object())

    return VolumeOutcome(
        volume_id=plan.volume_id,
        region=_REGION,
        availability_zone=plan.availability_zone,
        size_gib=plan.size_gib,
        observed_state=state,
        cluster_tag_value=tag_value,
        attachment_ids=attachment_ids,
        policy=policy,
        action=plan.action,
        action_result=action_result,
        reason_code=reason_code,
        reason=None if reason_code is None else _REASON,
        follow_up=None if reason_code is None else _FOLLOW_UP,
        recheck=recheck,
        error=error,
    )


def _partition_reporting(
    records: tuple[VolumeOutcome, ...],
) -> tuple[tuple[VolumeOutcome, ...], tuple[VolumeDiscoveryFailure, ...]]:
    """Split records exactly as the service does: reportable, then unreportable."""
    reportable: list[VolumeOutcome] = []
    unreportable: list[VolumeDiscoveryFailure] = []
    for record in records:
        try:
            verify_volume_reporting(record)
        except VolumeReportingError:
            unreportable.append(
                VolumeDiscoveryFailure(
                    reason_code=VolumeReasonCode.REPORTING_ERROR,
                    reason="A volume record could not be reported completely.",
                    follow_up=_FOLLOW_UP,
                    volume_id=record.volume_id,
                )
            )
            continue
        reportable.append(record)
    return tuple(reportable), tuple(unreportable)


def _expected_status(
    request: VolumeCleanupRequest,
    records: tuple[VolumeOutcome, ...],
    unreportable: tuple[VolumeDiscoveryFailure, ...],
) -> VolumeCleanupStatus:
    """Independently restate Property 9 over final records and reporting states."""
    authorized_delete = request.policy is VolumePolicy.DELETE and request.deletion_authorized
    evaluation_failed = any(record.action is VolumeAction.FAILED for record in records)
    owned_undeleted = any(
        record.cluster_tag_value == "owned" and record.action not in _DISPOSED_ACTIONS
        for record in records
    )
    if evaluation_failed or unreportable or (authorized_delete and owned_undeleted):
        return VolumeCleanupStatus.FAILED
    if authorized_delete and any(record.action in _PRESERVED_ACTIONS for record in records):
        return VolumeCleanupStatus.COMPLETED_WITH_SAFETY_RETENTIONS
    return VolumeCleanupStatus.COMPLETED


@st.composite
def _aggregation_states(
    draw: st.DrawFn,
) -> tuple[VolumeCleanupRequest, tuple[_RecordPlan, ...], tuple[VolumeDiscoveryFailure, ...]]:
    request = draw(st.sampled_from(_REQUESTS))
    authorized_delete = request.policy is VolumePolicy.DELETE and request.deletion_authorized
    actions = tuple(
        action
        for action in VolumeAction
        if authorized_delete or action is not VolumeAction.DELETE_REQUESTED
    )
    volume_ids = draw(st.lists(_VOLUME_IDS, min_size=1, max_size=6, unique=True))
    plans = tuple(
        _RecordPlan(
            volume_id=volume_id,
            action=draw(st.sampled_from(actions)),
            tag_value=draw(st.sampled_from(_TAG_VALUES)),
            state=draw(st.sampled_from(_STATES)),
            size_gib=draw(st.integers(min_value=1, max_value=16384)),
            zone_suffix=draw(st.sampled_from(("a", "b", "c"))),
            attachment_ids=tuple(draw(st.lists(_INSTANCE_IDS, max_size=2, unique=True))),
            failure_reason=draw(st.sampled_from(_FAILURE_REASONS)),
            reporting_defect=draw(st.sampled_from(_REPORTING_DEFECTS)),
        )
        for volume_id in volume_ids
    )
    discovery_failures = tuple(
        VolumeDiscoveryFailure(
            reason_code=reason_code,
            reason="A returned volume could not become a complete record.",
            follow_up=_FOLLOW_UP,
        )
        for reason_code in draw(
            st.lists(
                st.sampled_from(
                    (
                        VolumeReasonCode.NORMALIZATION_ERROR,
                        VolumeReasonCode.EVALUATION_ERROR,
                        VolumeReasonCode.REPORTING_ERROR,
                    )
                ),
                max_size=2,
            )
        )
    )
    return request, plans, discovery_failures


@settings(max_examples=150, deadline=None)
@given(generated=_aggregation_states())
def test_aggregate_success_reflects_all_volume_failures(
    generated: tuple[
        VolumeCleanupRequest, tuple[_RecordPlan, ...], tuple[VolumeDiscoveryFailure, ...]
    ],
) -> None:
    # Feature: cleanup-dynamic-ebs-volumes, Property 9: Aggregate success reflects all volume failures
    #
    # **Validates: Requirements 5.3, 5.6, 6.8**
    request, plans, discovery_failures = generated

    candidates = tuple(_record(plan, request.policy) for plan in plans)

    # Requirement 6.8: reporting completeness is decided per record, so an
    # incomplete record becomes reported evidence instead of silent success.
    reportable, reporting_failures = _partition_reporting(candidates)
    expected_unreportable = {plan.volume_id for plan in plans if plan.reporting_incomplete}
    assert {failure.volume_id for failure in reporting_failures} == expected_unreportable
    assert len(reportable) + len(reporting_failures) == len(plans)

    unreportable = (*discovery_failures, *reporting_failures)
    outcome = aggregate_target_outcome(
        target=_TARGET,
        request=request,
        records=reportable,
        unprocessable=unreportable,
    )

    expected_status = _expected_status(request, reportable, unreportable)
    assert outcome.status is expected_status
    assert outcome.successful is (expected_status in _SUCCESS_STATUSES)
    assert outcome.successful is (outcome.status is not VolumeCleanupStatus.FAILED)

    # Requirement 5.6: any evaluation failure keeps the target unsuccessful.
    if any(record.action is VolumeAction.FAILED for record in reportable):
        assert outcome.successful is False
    # Requirement 6.8: any reporting failure keeps the target unsuccessful.
    if unreportable:
        assert outcome.successful is False
        assert outcome.error is not None
    else:
        assert outcome.error is None
    # Requirement 5.3: authorized delete cannot succeed while an owned volume
    # remains because of an error or a safety condition. Requirement 5.6 also
    # keeps the target unsuccessful whenever any evaluation fails, regardless of
    # ownership, so an evaluation failure on a non-owned volume still blocks
    # success.
    if request.policy is VolumePolicy.DELETE and request.deletion_authorized:
        remaining_owned = [
            record
            for record in reportable
            if record.cluster_tag_value == "owned" and record.action not in _DISPOSED_ACTIONS
        ]
        any_evaluation_failed = any(record.action is VolumeAction.FAILED for record in reportable)
        assert outcome.successful is not bool(
            remaining_owned or unreportable or any_evaluation_failed
        )
    if outcome.successful:
        assert all(record.action is not VolumeAction.FAILED for record in outcome.volumes)

    # Every status decision is read back from the final records only.
    assert outcome.volumes == tuple(sorted(reportable, key=lambda record: record.volume_id))
    assert outcome.counts.discovered == len(reportable)
    assert (
        outcome.counts.deleted
        + outcome.counts.retained
        + outcome.counts.skipped
        + outcome.counts.already_absent
        + outcome.counts.failed
        == outcome.counts.discovered
    )
    if outcome.counts.deleted:
        assert request.policy is VolumePolicy.DELETE
        assert request.deletion_authorized is True
    assert isinstance(outcome, TargetVolumeCleanupOutcome)
    assert outcome == aggregate_target_outcome(
        target=_TARGET,
        request=request,
        records=reportable,
        unprocessable=unreportable,
    )

    # Requirement 6.8 independently of stack and volume disposition: adding one
    # unreportable record to any aggregate makes the cleanup unsuccessful.
    with_reporting_failure = aggregate_target_outcome(
        target=_TARGET,
        request=request,
        records=reportable,
        unprocessable=(
            *unreportable,
            VolumeDiscoveryFailure(
                reason_code=VolumeReasonCode.REPORTING_ERROR,
                reason="A required reporting field could not be produced.",
                follow_up=_FOLLOW_UP,
            ),
        ),
    )
    assert with_reporting_failure.status is VolumeCleanupStatus.FAILED
    assert with_reporting_failure.successful is False
    assert with_reporting_failure.volumes == outcome.volumes
