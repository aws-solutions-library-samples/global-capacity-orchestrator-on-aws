"""Offline unit tests for per-volume isolation, retries, and aggregate status."""

from typing import Any

import pytest
from botocore.exceptions import ClientError

from cli import volume_cleanup
from cli.volume_cleanup import (
    VOLUME_NOT_FOUND_ERROR_CODE,
    ClusterAbsenceProof,
    DeletionAuthorizationSource,
    RegionalVolumeTarget,
    VolumeAction,
    VolumeActionResult,
    VolumeCleanupRequest,
    VolumeCleanupService,
    VolumeCleanupStatus,
    VolumeOutcome,
    VolumePolicy,
    VolumeReasonCode,
    VolumeReportingError,
    VolumeSnapshot,
    aggregate_target_outcome,
    verify_volume_reporting,
)

REGION = "us-east-1"
STACK = "gco-us-east-1"
TAG_KEY = f"kubernetes.io/cluster/{STACK}"


def target() -> RegionalVolumeTarget:
    return RegionalVolumeTarget(
        stack_name=STACK,
        stack_id=None,
        region=REGION,
        cluster_name=STACK,
        cluster_tag_key=TAG_KEY,
    )


def absence(**overrides: Any) -> ClusterAbsenceProof:
    values: dict[str, Any] = {
        "stack_name": STACK,
        "region": REGION,
        "cluster_name": STACK,
        "verified_at": "2026-01-01T00:00:00Z",
    }
    values.update(overrides)
    return ClusterAbsenceProof(**values)


def request(
    policy: VolumePolicy = VolumePolicy.DELETE,
    *,
    authorized: bool | None = None,
) -> VolumeCleanupRequest:
    granted = (policy is VolumePolicy.DELETE) if authorized is None else authorized
    return VolumeCleanupRequest(
        policy=policy,
        deletion_authorized=granted,
        authorization_source=(
            DeletionAuthorizationSource.DESTROY_ALL_WITH_YES
            if granted
            else DeletionAuthorizationSource.NONE
        ),
    )


def dto(
    volume_id: str,
    *,
    tag_key: str = TAG_KEY,
    tag_value: str = "owned",
    zone: str = "us-east-1a",
    state: str = "available",
    size: int = 50,
    attachments: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "VolumeId": volume_id,
        "AvailabilityZone": zone,
        "Size": size,
        "State": state,
        "Tags": [{"Key": tag_key, "Value": tag_value}],
        "Attachments": attachments or [],
    }


def snapshot(volume_id: str = "vol-0aaa", **overrides: Any) -> VolumeSnapshot:
    values: dict[str, Any] = {
        "volume_id": volume_id,
        "region": REGION,
        "availability_zone": "us-east-1a",
        "size_gib": 50,
        "state": "available",
        "cluster_tag_value": "owned",
        "attachment_ids": (),
    }
    values.update(overrides)
    return VolumeSnapshot(**values)


def outcome_record(
    volume_id: str,
    *,
    action: VolumeAction,
    policy: VolumePolicy = VolumePolicy.DELETE,
    tag_value: str | None = "owned",
) -> VolumeOutcome:
    reasons: dict[str, Any] = {
        "reason_code": VolumeReasonCode.RETAIN_POLICY,
        "reason": "reason",
        "follow_up": "follow up",
    }
    extra: dict[str, Any] = {}
    if action is VolumeAction.DELETE_REQUESTED:
        result = VolumeActionResult.SUCCESS
        extra["recheck"] = snapshot(volume_id)
    elif action is VolumeAction.ALREADY_ABSENT:
        result = VolumeActionResult.IDEMPOTENT_SUCCESS
    elif action is VolumeAction.FAILED:
        result = VolumeActionResult.ERROR
    else:
        result = VolumeActionResult.SAFETY_PRESERVED
    return VolumeOutcome(
        volume_id=volume_id,
        region=REGION,
        availability_zone="us-east-1a",
        size_gib=50,
        observed_state="available",
        cluster_tag_value=tag_value,
        attachment_ids=(),
        policy=policy,
        action=action,
        action_result=result,
        **reasons,
        **extra,
    )


def not_found(operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": VOLUME_NOT_FOUND_ERROR_CODE, "Message": "does not exist"}},
        operation,
    )


def throttled(operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "RequestLimitExceeded", "Message": "slow down"}},
        operation,
    )


class FakeEC2:
    """An in-memory EC2 double whose deletions change later discovery results."""

    def __init__(
        self,
        volumes: dict[str, dict[str, object]],
        *,
        describe_errors: dict[str, BaseException] | None = None,
        delete_errors: dict[str, BaseException] | None = None,
        pages: int = 1,
    ) -> None:
        self.volumes = dict(volumes)
        self.describe_errors = describe_errors or {}
        self.delete_errors = delete_errors or {}
        self.pages = pages
        self.filters: list[Any] = []
        self.describe_calls: list[list[str]] = []
        self.deleted: list[str] = []

    def get_paginator(self, operation_name: str) -> Any:
        assert operation_name == "describe_volumes"
        return self

    @staticmethod
    def _has_cluster_tag(dto_value: dict[str, object]) -> bool:
        tags = dto_value.get("Tags")
        if not isinstance(tags, list):
            return False
        return any(isinstance(tag, dict) and tag.get("Key") == TAG_KEY for tag in tags)

    def paginate(self, **kwargs: Any) -> Any:
        self.filters.append(kwargs.get("Filters"))
        tagged = [
            dto_value for dto_value in self.volumes.values() if self._has_cluster_tag(dto_value)
        ]
        if self.pages == 1:
            return iter([{"Volumes": tagged}])
        chunk = max(1, len(tagged) // self.pages)
        pages = [
            {"Volumes": tagged[index : index + chunk]} for index in range(0, len(tagged), chunk)
        ]
        return iter(pages or [{"Volumes": []}])

    def describe_volumes(self, **kwargs: Any) -> Any:
        volume_ids = [str(value) for value in kwargs.get("VolumeIds", [])]
        self.describe_calls.append(volume_ids)
        volume_id = volume_ids[0]
        if volume_id in self.describe_errors:
            raise self.describe_errors[volume_id]
        if volume_id not in self.volumes:
            raise not_found("DescribeVolumes")
        return {"Volumes": [self.volumes[volume_id]]}

    def delete_volume(self, **kwargs: Any) -> Any:
        volume_id = str(kwargs["VolumeId"])
        self.deleted.append(volume_id)
        if volume_id in self.delete_errors:
            raise self.delete_errors[volume_id]
        if volume_id not in self.volumes:
            raise not_found("DeleteVolume")
        del self.volumes[volume_id]
        return {}


class RecordingFactory:
    """Record each requested client and Region for the injected fake client."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.calls: list[tuple[str, str]] = []

    def __call__(self, service_name: str, *, region_name: str) -> Any:
        self.calls.append((service_name, region_name))
        return self.client


def cleanup(ec2: FakeEC2, cleanup_request: VolumeCleanupRequest | None = None):
    service = VolumeCleanupService(RecordingFactory(ec2))
    return service.cleanup(
        target=target(),
        absence=absence(),
        request=cleanup_request or request(),
    )


def test_authorized_delete_completes_when_every_owned_volume_is_removed():
    ec2 = FakeEC2({"vol-0aaa": dto("vol-0aaa"), "vol-0bbb": dto("vol-0bbb")})

    outcome = cleanup(ec2)

    assert outcome.status is VolumeCleanupStatus.COMPLETED
    assert outcome.successful is True
    assert outcome.counts.discovered == 2
    assert outcome.counts.deleted == 2
    assert ec2.deleted == ["vol-0aaa", "vol-0bbb"]
    assert ec2.volumes == {}


def test_zero_discovered_volumes_is_a_successful_no_op():
    outcome = cleanup(FakeEC2({}))

    assert outcome.status is VolumeCleanupStatus.COMPLETED
    assert outcome.successful is True
    assert outcome.counts.discovered == 0
    assert outcome.volumes == ()


def test_retain_policy_keeps_every_volume_and_still_completes():
    ec2 = FakeEC2(
        {
            "vol-0aaa": dto("vol-0aaa"),
            "vol-0bbb": dto("vol-0bbb", tag_value="shared", state="in-use"),
        }
    )

    outcome = cleanup(ec2, request(VolumePolicy.RETAIN))

    assert outcome.status is VolumeCleanupStatus.COMPLETED
    assert outcome.successful is True
    assert outcome.counts.retained == 2
    assert ec2.deleted == []


def test_non_owned_safety_retention_completes_with_safety_retentions():
    ec2 = FakeEC2(
        {
            "vol-0aaa": dto("vol-0aaa"),
            "vol-0bbb": dto("vol-0bbb", tag_value="shared"),
        }
    )

    outcome = cleanup(ec2)

    assert outcome.status is VolumeCleanupStatus.COMPLETED_WITH_SAFETY_RETENTIONS
    assert outcome.successful is True
    assert outcome.counts.deleted == 1
    assert outcome.counts.skipped == 1
    assert ec2.deleted == ["vol-0aaa"]
    preserved = next(item for item in outcome.volumes if item.volume_id == "vol-0bbb")
    assert preserved.reason_code is VolumeReasonCode.OWNERSHIP_SAFETY
    assert preserved.action_result is VolumeActionResult.SAFETY_PRESERVED


@pytest.mark.parametrize(
    "unsafe",
    [
        dto("vol-0bbb", state="in-use"),
        dto("vol-0bbb", attachments=[{"InstanceId": "i-0123", "VolumeId": "vol-0bbb"}]),
    ],
)
def test_authorized_delete_fails_when_an_owned_volume_remains(unsafe):
    ec2 = FakeEC2({"vol-0aaa": dto("vol-0aaa"), "vol-0bbb": unsafe})

    outcome = cleanup(ec2)

    assert outcome.status is VolumeCleanupStatus.FAILED
    assert outcome.successful is False
    assert outcome.counts.deleted == 1
    assert outcome.counts.skipped == 1
    assert ec2.deleted == ["vol-0aaa"]


def test_one_deletion_failure_does_not_stop_the_remaining_volumes():
    ec2 = FakeEC2(
        {
            "vol-0aaa": dto("vol-0aaa"),
            "vol-0bbb": dto("vol-0bbb"),
            "vol-0ccc": dto("vol-0ccc"),
        },
        delete_errors={"vol-0bbb": throttled("DeleteVolume")},
    )

    outcome = cleanup(ec2)

    assert outcome.status is VolumeCleanupStatus.FAILED
    assert outcome.successful is False
    assert outcome.counts.discovered == 3
    assert outcome.counts.deleted == 2
    assert outcome.counts.failed == 1
    assert ec2.deleted == ["vol-0aaa", "vol-0bbb", "vol-0ccc"]


def test_one_recheck_failure_does_not_stop_the_remaining_volumes():
    ec2 = FakeEC2(
        {"vol-0aaa": dto("vol-0aaa"), "vol-0bbb": dto("vol-0bbb")},
        describe_errors={"vol-0aaa": RuntimeError("connection reset")},
    )

    outcome = cleanup(ec2)

    assert outcome.counts.failed == 1
    assert outcome.counts.deleted == 1
    assert ec2.deleted == ["vol-0bbb"]
    failure = next(record for record in outcome.volumes if record.volume_id == "vol-0aaa")
    assert failure.reason_code is VolumeReasonCode.RECHECK_ERROR
    assert failure.error is not None
    assert failure.follow_up


def test_absent_volume_at_recheck_is_idempotent_success_alongside_a_deletion():
    ec2 = FakeEC2({"vol-0aaa": dto("vol-0aaa"), "vol-0bbb": dto("vol-0bbb")})
    ec2.describe_errors["vol-0aaa"] = not_found("DescribeVolumes")

    outcome = cleanup(ec2)

    assert outcome.status is VolumeCleanupStatus.COMPLETED
    assert outcome.successful is True
    assert outcome.counts.already_absent == 1
    assert outcome.counts.deleted == 1


def test_unprocessable_discovered_volume_fails_the_target_without_blocking_others():
    malformed = dto("vol-0bbb")
    malformed["Size"] = "50"
    ec2 = FakeEC2({"vol-0aaa": dto("vol-0aaa"), "vol-0bbb": malformed})

    outcome = cleanup(ec2)

    assert outcome.status is VolumeCleanupStatus.FAILED
    assert outcome.successful is False
    assert outcome.counts.discovered == 1
    assert outcome.counts.deleted == 1
    assert outcome.error is not None
    assert ec2.deleted == ["vol-0aaa"]


def test_evaluation_failure_for_one_volume_is_isolated(monkeypatch):
    real_classify = volume_cleanup.classify_volume

    def failing_classify(snapshot_value, request_value, **kwargs):
        if snapshot_value.volume_id == "vol-0aaa":
            raise RuntimeError("classification exploded")
        return real_classify(snapshot_value, request_value, **kwargs)

    monkeypatch.setattr(volume_cleanup, "classify_volume", failing_classify)
    ec2 = FakeEC2({"vol-0aaa": dto("vol-0aaa"), "vol-0bbb": dto("vol-0bbb")})

    outcome = cleanup(ec2)

    assert outcome.status is VolumeCleanupStatus.FAILED
    assert outcome.counts.failed == 1
    assert outcome.counts.deleted == 1
    failure = next(record for record in outcome.volumes if record.volume_id == "vol-0aaa")
    assert failure.reason_code is VolumeReasonCode.EVALUATION_ERROR
    assert ec2.deleted == ["vol-0bbb"]


def test_reporting_failure_for_one_volume_is_isolated_and_fails_the_target(monkeypatch):
    def failing_verify(record_value):
        if record_value.volume_id == "vol-0aaa":
            raise VolumeReportingError("required reporting field is missing")

    monkeypatch.setattr(volume_cleanup, "verify_volume_reporting", failing_verify)
    ec2 = FakeEC2({"vol-0aaa": dto("vol-0aaa"), "vol-0bbb": dto("vol-0bbb")})

    outcome = cleanup(ec2)

    assert outcome.status is VolumeCleanupStatus.FAILED
    assert outcome.successful is False
    assert outcome.counts.discovered == 2
    assert outcome.counts.failed == 1
    reporting = next(record for record in outcome.volumes if record.volume_id == "vol-0aaa")
    assert reporting.reason_code is VolumeReasonCode.REPORTING_ERROR
    assert reporting.error is not None


def test_mismatched_absence_evidence_blocks_every_ebs_request():
    ec2 = FakeEC2({"vol-0aaa": dto("vol-0aaa")})
    factory = RecordingFactory(ec2)

    outcome = VolumeCleanupService(factory).cleanup(
        target=target(),
        absence=absence(cluster_name="gco-us-west-2"),
        request=request(),
    )

    assert outcome.status is VolumeCleanupStatus.SKIPPED
    assert outcome.successful is False
    assert outcome.blocking_reason_code == "cluster-absence-proof-mismatch"
    assert outcome.follow_up
    assert outcome.counts.discovered == 0
    assert factory.calls == []
    assert ec2.deleted == []


def test_unauthorized_delete_policy_blocks_every_ebs_request():
    ec2 = FakeEC2({"vol-0aaa": dto("vol-0aaa")})
    factory = RecordingFactory(ec2)

    outcome = VolumeCleanupService(factory).cleanup(
        target=target(),
        absence=absence(),
        request=request(VolumePolicy.DELETE, authorized=False),
    )

    assert outcome.status is VolumeCleanupStatus.SKIPPED
    assert outcome.blocking_reason_code == "deletion-unauthorized"
    assert factory.calls == []
    assert ec2.describe_calls == []


def test_incomplete_discovery_issues_no_deletion_and_fails_the_target():
    class BrokenPaginator(FakeEC2):
        def paginate(self, **kwargs: Any) -> Any:
            super().paginate(**kwargs)
            raise throttled("DescribeVolumes")

    ec2 = BrokenPaginator({"vol-0aaa": dto("vol-0aaa")})

    outcome = cleanup(ec2)

    assert outcome.status is VolumeCleanupStatus.FAILED
    assert outcome.successful is False
    assert outcome.counts.discovered == 0
    assert outcome.error is not None
    assert ec2.deleted == []


def test_repeated_cleanup_preserves_scope_and_prior_safe_results():
    ec2 = FakeEC2(
        {
            "vol-0aaa": dto("vol-0aaa"),
            "vol-0bbb": dto("vol-0bbb", tag_value="shared"),
            "vol-0ccc": {
                "VolumeId": "vol-0ccc",
                "AvailabilityZone": "us-east-1a",
                "Size": 10,
                "State": "available",
                "Tags": [{"Key": "kubernetes.io/cluster/other", "Value": "owned"}],
                "Attachments": [],
            },
        }
    )

    first = cleanup(ec2)
    second = cleanup(ec2)

    assert first.status is VolumeCleanupStatus.COMPLETED_WITH_SAFETY_RETENTIONS
    assert first.counts.deleted == 1
    assert second.status is VolumeCleanupStatus.COMPLETED_WITH_SAFETY_RETENTIONS
    assert second.counts.deleted == 0
    assert second.counts.skipped == 1
    assert [item.volume_id for item in second.volumes] == ["vol-0bbb"]
    assert set(ec2.volumes) == {"vol-0bbb", "vol-0ccc"}
    assert ec2.deleted == ["vol-0aaa"]
    assert all(filters == [{"Name": "tag-key", "Values": [TAG_KEY]}] for filters in ec2.filters)


def test_retry_after_a_deletion_failure_can_complete_the_same_target():
    ec2 = FakeEC2(
        {"vol-0aaa": dto("vol-0aaa")},
        delete_errors={"vol-0aaa": throttled("DeleteVolume")},
    )

    first = cleanup(ec2)
    ec2.delete_errors.clear()
    second = cleanup(ec2)

    assert first.status is VolumeCleanupStatus.FAILED
    assert second.status is VolumeCleanupStatus.COMPLETED
    assert second.counts.deleted == 1
    assert ec2.volumes == {}


def test_paginated_discovery_produces_one_record_per_volume():
    ec2 = FakeEC2(
        {volume_id: dto(volume_id) for volume_id in ("vol-0aaa", "vol-0bbb", "vol-0ccc")},
        pages=3,
    )

    outcome = cleanup(ec2)

    assert outcome.counts.discovered == 3
    assert outcome.counts.deleted == 3
    assert [record.volume_id for record in outcome.volumes] == [
        "vol-0aaa",
        "vol-0bbb",
        "vol-0ccc",
    ]


def test_aggregate_status_reflects_records_only():
    outcome = aggregate_target_outcome(
        target=target(),
        request=request(),
        records=[
            outcome_record("vol-0bbb", action=VolumeAction.ALREADY_ABSENT),
            outcome_record("vol-0aaa", action=VolumeAction.DELETE_REQUESTED),
        ],
    )

    assert outcome.status is VolumeCleanupStatus.COMPLETED
    assert outcome.counts.discovered == 2
    assert [item.volume_id for item in outcome.volumes] == ["vol-0aaa", "vol-0bbb"]


def test_aggregate_status_fails_for_an_owned_skip_under_authorized_delete():
    outcome = aggregate_target_outcome(
        target=target(),
        request=request(),
        records=[outcome_record("vol-0aaa", action=VolumeAction.SKIPPED)],
    )

    assert outcome.status is VolumeCleanupStatus.FAILED
    assert outcome.successful is False


def test_aggregate_status_completes_for_a_non_owned_retention_under_delete():
    outcome = aggregate_target_outcome(
        target=target(),
        request=request(),
        records=[outcome_record("vol-0aaa", action=VolumeAction.RETAINED, tag_value="shared")],
    )

    assert outcome.status is VolumeCleanupStatus.COMPLETED_WITH_SAFETY_RETENTIONS
    assert outcome.successful is True


def test_reporting_verification_accepts_a_complete_record():
    verify_volume_reporting(outcome_record("vol-0aaa", action=VolumeAction.DELETE_REQUESTED))


def test_reporting_verification_rejects_an_incomplete_record():
    with pytest.raises(VolumeReportingError):
        verify_volume_reporting("not-a-record")  # type: ignore[arg-type]
