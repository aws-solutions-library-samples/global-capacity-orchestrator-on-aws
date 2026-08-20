"""Property test proving per-volume cleanup failures stay isolated from other volumes."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

from botocore.exceptions import ClientError
from hypothesis import given, settings
from hypothesis import strategies as st

from cli import volume_cleanup
from cli.volume_cleanup import (
    VOLUME_NOT_FOUND_ERROR_CODE,
    ClusterAbsenceProof,
    DeletionAuthorizationSource,
    RegionalVolumeTarget,
    TargetVolumeCleanupOutcome,
    VolumeAction,
    VolumeActionResult,
    VolumeCleanupRequest,
    VolumeCleanupService,
    VolumeOutcome,
    VolumePolicy,
    VolumeReasonCode,
    VolumeReportingError,
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
_ABSENCE = ClusterAbsenceProof(
    stack_name=_STACK,
    region=_REGION,
    cluster_name=_STACK,
    verified_at="2026-01-01T00:00:00Z",
)
_REQUEST = VolumeCleanupRequest(
    policy=VolumePolicy.DELETE,
    deletion_authorized=True,
    authorization_source=DeletionAuthorizationSource.DESTROY_ALL_WITH_YES,
)

# One unprocessable discovery DTO whose exact ID can never be generated below.
_MALFORMED_VOLUME_ID = "vol-0malformed00000"
_ATTACHED_INSTANCE_ID = "i-0123abcd"

# Injected per-volume dispositions covering evaluation, absence-classification,
# deletion, and safety outcomes. Each key maps to the terminal record the volume
# must receive regardless of what happens to any other volume.
_EXPECTED: dict[str, tuple[VolumeAction, VolumeActionResult, VolumeReasonCode]] = {
    "delete-ok": (
        VolumeAction.DELETE_REQUESTED,
        VolumeActionResult.SUCCESS,
        VolumeReasonCode.DELETE_REQUEST_ACCEPTED,
    ),
    "safety-non-owned": (
        VolumeAction.SKIPPED,
        VolumeActionResult.SAFETY_PRESERVED,
        VolumeReasonCode.OWNERSHIP_SAFETY,
    ),
    "safety-state": (
        VolumeAction.SKIPPED,
        VolumeActionResult.SAFETY_PRESERVED,
        VolumeReasonCode.STATE_NOT_AVAILABLE,
    ),
    "recheck-changed": (
        VolumeAction.SKIPPED,
        VolumeActionResult.SAFETY_PRESERVED,
        VolumeReasonCode.SAFETY_RECHECK_CHANGED,
    ),
    "recheck-not-found": (
        VolumeAction.ALREADY_ABSENT,
        VolumeActionResult.IDEMPOTENT_SUCCESS,
        VolumeReasonCode.ALREADY_ABSENT,
    ),
    "delete-not-found": (
        VolumeAction.ALREADY_ABSENT,
        VolumeActionResult.IDEMPOTENT_SUCCESS,
        VolumeReasonCode.ALREADY_ABSENT,
    ),
    "recheck-error": (
        VolumeAction.FAILED,
        VolumeActionResult.ERROR,
        VolumeReasonCode.RECHECK_ERROR,
    ),
    "recheck-malformed": (
        VolumeAction.FAILED,
        VolumeActionResult.ERROR,
        VolumeReasonCode.RECHECK_ERROR,
    ),
    "recheck-unclassifiable": (
        VolumeAction.FAILED,
        VolumeActionResult.ERROR,
        VolumeReasonCode.RECHECK_ERROR,
    ),
    "delete-error": (
        VolumeAction.FAILED,
        VolumeActionResult.ERROR,
        VolumeReasonCode.DELETE_ERROR,
    ),
    "delete-unclassifiable": (
        VolumeAction.FAILED,
        VolumeActionResult.ERROR,
        VolumeReasonCode.EVALUATION_ERROR,
    ),
}
_BEHAVIORS = tuple(_EXPECTED)
_RECHECKED = frozenset(_BEHAVIORS) - {"safety-non-owned", "safety-state"}
_DELETE_ATTEMPTED = frozenset(
    {"delete-ok", "delete-not-found", "delete-error", "delete-unclassifiable"}
)
_REPORTING_FAILURE = (
    VolumeAction.FAILED,
    VolumeActionResult.ERROR,
    VolumeReasonCode.REPORTING_ERROR,
)


class _UnreadableErrorResponse(Mapping[str, object]):
    """An AWS error payload whose contents cannot be read for classification."""

    def __getitem__(self, key: str) -> object:
        raise RuntimeError("the EC2 error response could not be read")

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 0


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "injected"}}, operation)


def _unclassifiable_error(operation: str) -> ClientError:
    """Return an error whose already-absent classification cannot be completed."""
    error = _client_error("InternalError", operation)
    error.response = _UnreadableErrorResponse()
    return error


@dataclass(frozen=True)
class _VolumePlan:
    """One generated in-scope volume, its injected failure, and its reporting state."""

    volume_id: str
    behavior: str
    reporting_fails: bool
    zone_suffix: str
    size_gib: int

    @property
    def availability_zone(self) -> str:
        return f"{_REGION}{self.zone_suffix}"

    def dto(
        self,
        *,
        tag_value: str | None = None,
        state: str | None = None,
        attached: bool = False,
    ) -> dict[str, object]:
        if tag_value is None:
            tag_value = "shared" if self.behavior == "safety-non-owned" else "owned"
        if state is None:
            state = "in-use" if self.behavior == "safety-state" else "available"
        attachments: list[dict[str, object]] = (
            [{"VolumeId": self.volume_id, "InstanceId": _ATTACHED_INSTANCE_ID}] if attached else []
        )
        return {
            "VolumeId": self.volume_id,
            "AvailabilityZone": self.availability_zone,
            "Size": self.size_gib,
            "State": state,
            "Tags": [
                {"Key": "Name", "Value": "prometheus-data"},
                {"Key": _TAG_KEY, "Value": tag_value},
            ],
            "Attachments": attachments,
        }

    @property
    def expected_record(self) -> tuple[VolumeAction, VolumeActionResult, VolumeReasonCode]:
        return _REPORTING_FAILURE if self.reporting_fails else _EXPECTED[self.behavior]


def _malformed_dto() -> dict[str, object]:
    return {
        "VolumeId": _MALFORMED_VOLUME_ID,
        "AvailabilityZone": f"{_REGION}a",
        "Size": "fifty",
        "State": "available",
        "Tags": [{"Key": _TAG_KEY, "Value": "owned"}],
        "Attachments": [],
    }


class _FakeEC2:
    """EC2 double that injects one failure per volume and records every request."""

    def __init__(self, plans: tuple[_VolumePlan, ...], *, malformed: bool) -> None:
        self._plans = {plan.volume_id: plan for plan in plans}
        self._malformed = malformed
        self.filters: list[Any] = []
        self.describe_calls: list[str] = []
        self.delete_calls: list[str] = []

    def get_paginator(self, operation_name: str) -> Any:
        assert operation_name == "describe_volumes"
        return self

    def paginate(self, **kwargs: Any) -> Any:
        self.filters.append(kwargs.get("Filters"))
        volumes: list[dict[str, object]] = [plan.dto() for plan in self._plans.values()]
        if self._malformed:
            volumes.append(_malformed_dto())
        return iter([{"Volumes": volumes}])

    def describe_volumes(self, **kwargs: Any) -> Any:
        volume_ids = [str(value) for value in kwargs.get("VolumeIds", [])]
        assert len(volume_ids) == 1
        volume_id = volume_ids[0]
        self.describe_calls.append(volume_id)
        plan = self._plans[volume_id]
        if plan.behavior == "recheck-not-found":
            raise _client_error(VOLUME_NOT_FOUND_ERROR_CODE, "DescribeVolumes")
        if plan.behavior == "recheck-error":
            raise _client_error("RequestLimitExceeded", "DescribeVolumes")
        if plan.behavior == "recheck-unclassifiable":
            raise _unclassifiable_error("DescribeVolumes")
        if plan.behavior == "recheck-malformed":
            return {"Volumes": []}
        if plan.behavior == "recheck-changed":
            return {"Volumes": [plan.dto(state="in-use", attached=True)]}
        return {"Volumes": [plan.dto()]}

    def delete_volume(self, **kwargs: Any) -> Any:
        volume_id = str(kwargs["VolumeId"])
        self.delete_calls.append(volume_id)
        plan = self._plans[volume_id]
        if plan.behavior == "delete-not-found":
            raise _client_error(VOLUME_NOT_FOUND_ERROR_CODE, "DeleteVolume")
        if plan.behavior == "delete-error":
            raise _client_error("RequestLimitExceeded", "DeleteVolume")
        if plan.behavior == "delete-unclassifiable":
            raise _unclassifiable_error("DeleteVolume")
        return {}


def _reporting_gate(failing: frozenset[str]) -> Any:
    """Fail required-field reporting for exactly the selected volume IDs."""

    def verify(record: VolumeOutcome) -> None:
        if record.volume_id in failing:
            raise VolumeReportingError(f"volume record field is missing for {record.volume_id}")
        verify_volume_reporting(record)

    return verify


def _run_cleanup(
    plans: tuple[_VolumePlan, ...],
    *,
    malformed: bool,
) -> tuple[TargetVolumeCleanupOutcome, _FakeEC2]:
    ec2 = _FakeEC2(plans, malformed=malformed)
    service = VolumeCleanupService(lambda service_name, *, region_name: ec2)
    failing = frozenset(plan.volume_id for plan in plans if plan.reporting_fails)
    with patch.object(volume_cleanup, "verify_volume_reporting", _reporting_gate(failing)):
        outcome = service.cleanup(target=_TARGET, absence=_ABSENCE, request=_REQUEST)
    return outcome, ec2


@st.composite
def _volume_plans(draw: st.DrawFn) -> tuple[tuple[_VolumePlan, ...], bool]:
    volume_ids = draw(
        st.lists(
            st.from_regex(r"vol-[0-9a-f]{17}", fullmatch=True),
            min_size=1,
            max_size=6,
            unique=True,
        )
    )
    plans = tuple(
        _VolumePlan(
            volume_id=volume_id,
            behavior=draw(st.sampled_from(_BEHAVIORS)),
            reporting_fails=draw(st.booleans()),
            zone_suffix=draw(st.sampled_from(("a", "b", "c"))),
            size_gib=draw(st.integers(min_value=1, max_value=16384)),
        )
        for volume_id in volume_ids
    )
    return plans, draw(st.booleans())


@settings(max_examples=150, deadline=None)
@given(generated=_volume_plans())
def test_per_volume_failures_are_isolated(
    generated: tuple[tuple[_VolumePlan, ...], bool],
) -> None:
    # Feature: cleanup-dynamic-ebs-volumes, Property 6: Per-volume failures are isolated
    #
    # **Validates: Requirements 5.1, 5.7, 6.7**
    plans, malformed = generated

    outcome, ec2 = _run_cleanup(plans, malformed=malformed)

    # Requirements 5.1 and 6.7: every processable volume gets exactly one record.
    records = {record.volume_id: record for record in outcome.volumes}
    assert len(outcome.volumes) == len(plans)
    assert set(records) == {plan.volume_id for plan in plans}
    assert _MALFORMED_VOLUME_ID not in records
    assert outcome.counts.discovered == len(plans)
    assert (
        outcome.counts.deleted
        + outcome.counts.retained
        + outcome.counts.skipped
        + outcome.counts.already_absent
        + outcome.counts.failed
        == outcome.counts.discovered
    )
    assert [record.volume_id for record in outcome.volumes] == sorted(records)

    # Requirement 5.7: an unfinished absence classification is reported, not fatal.
    for plan in plans:
        record = records[plan.volume_id]
        action, action_result, reason_code = plan.expected_record
        assert (record.action, record.action_result, record.reason_code) == (
            action,
            action_result,
            reason_code,
        )
        assert record.reason
        assert record.follow_up
        if action is VolumeAction.FAILED:
            assert record.error is not None

    # Requirement 5.1: each candidate is still evaluated and deleted independently.
    assert ec2.describe_calls == sorted(
        plan.volume_id for plan in plans if plan.behavior in _RECHECKED
    )
    assert ec2.delete_calls == sorted(
        plan.volume_id for plan in plans if plan.behavior in _DELETE_ATTEMPTED
    )
    assert ec2.filters == [[{"Name": "tag-key", "Values": [_TAG_KEY]}]]

    failures = [plan for plan in plans if plan.expected_record[0] is VolumeAction.FAILED]
    preserved = [
        record
        for record in outcome.volumes
        if record.action in {VolumeAction.RETAINED, VolumeAction.SKIPPED}
    ]
    if failures or malformed:
        assert outcome.successful is False
    elif not preserved:
        assert outcome.successful is True

    # An error for one volume changes nothing for any other volume: each record is
    # identical to the record the same volume receives when processed alone.
    for plan in plans:
        solo_outcome, _ = _run_cleanup((plan,), malformed=False)
        assert solo_outcome.volumes == (records[plan.volume_id],)
