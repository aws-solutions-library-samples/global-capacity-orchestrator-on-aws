"""Offline unit tests for just-in-time volume safety recheck and deletion."""

from typing import Any

import pytest
from botocore.exceptions import ClientError

from cli.volume_cleanup import (
    VOLUME_NOT_FOUND_ERROR_CODE,
    DeletionAuthorizationSource,
    RecheckEvaluation,
    RegionalVolumeTarget,
    VolumeAction,
    VolumeActionResult,
    VolumeCleanupRequest,
    VolumeCleanupService,
    VolumePolicy,
    VolumeReasonCode,
    VolumeSnapshot,
    evaluate_recheck,
    is_exact_volume_not_found,
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


def request(policy: VolumePolicy = VolumePolicy.DELETE) -> VolumeCleanupRequest:
    authorized = policy is VolumePolicy.DELETE
    return VolumeCleanupRequest(
        policy=policy,
        deletion_authorized=authorized,
        authorization_source=(
            DeletionAuthorizationSource.DESTROY_ALL_WITH_YES
            if authorized
            else DeletionAuthorizationSource.NONE
        ),
    )


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


def dto(
    volume_id: str = "vol-0aaa",
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


def page(*volumes: object) -> dict[str, object]:
    return {"Volumes": list(volumes)}


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
    """Record just-in-time describe and delete requests with canned results."""

    def __init__(
        self,
        *,
        describe: object | dict[str, object] | None = None,
        delete: object | dict[str, object] | None = None,
    ) -> None:
        self.describe = describe
        self.delete = delete
        self.describe_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []

    @staticmethod
    def _result(configured: object, volume_id: str) -> object:
        if isinstance(configured, dict) and "Volumes" not in configured:
            return configured.get(volume_id)
        return configured

    def describe_volumes(self, **kwargs: Any) -> Any:
        self.describe_calls.append(kwargs)
        volume_ids = kwargs.get("VolumeIds") or [""]
        result = self._result(self.describe, str(volume_ids[0]))
        if isinstance(result, BaseException):
            raise result
        return result

    def delete_volume(self, **kwargs: Any) -> Any:
        self.delete_calls.append(kwargs)
        result = self._result(self.delete, str(kwargs.get("VolumeId")))
        if isinstance(result, BaseException):
            raise result
        return {}


class RecordingFactory:
    """Record every client request and optionally fail client creation."""

    def __init__(self, client: Any = None, *, error: Exception | None = None) -> None:
        self.client = client
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def __call__(self, service_name: str, *, region_name: str) -> Any:
        self.calls.append((service_name, region_name))
        if self.error is not None:
            raise self.error
        return self.client


def delete_candidate(ec2: FakeEC2, *, initial: VolumeSnapshot | None = None):
    return VolumeCleanupService(RecordingFactory(ec2)).delete_candidate(
        ec2=ec2,
        target=target(),
        request=request(),
        snapshot=initial or snapshot(),
    )


def test_still_eligible_candidate_is_rechecked_by_exact_id_then_deleted():
    ec2 = FakeEC2(describe=page(dto()))

    outcome = delete_candidate(ec2)

    assert ec2.describe_calls == [{"VolumeIds": ["vol-0aaa"]}]
    assert ec2.delete_calls == [{"VolumeId": "vol-0aaa"}]
    assert outcome.action is VolumeAction.DELETE_REQUESTED
    assert outcome.action_result is VolumeActionResult.SUCCESS
    assert outcome.reason_code is VolumeReasonCode.DELETE_REQUEST_ACCEPTED
    assert outcome.recheck == snapshot()
    assert outcome.error is None


@pytest.mark.parametrize(
    ("changed", "fact"),
    [
        (dto(state="in-use"), "state"),
        (
            dto(state="in-use", attachments=[{"InstanceId": "i-0123", "VolumeId": "vol-0aaa"}]),
            "attachments",
        ),
        (dto(tag_value="shared"), "cluster-tag-value"),
        (dto(zone="us-east-1b"), "availability-zone"),
    ],
)
def test_changed_safety_facts_skip_deletion_with_initial_and_current_snapshots(changed, fact):
    ec2 = FakeEC2(describe=page(changed))

    outcome = delete_candidate(ec2)

    assert ec2.delete_calls == []
    assert outcome.action is VolumeAction.SKIPPED
    assert outcome.action_result is VolumeActionResult.SAFETY_PRESERVED
    assert outcome.reason_code is VolumeReasonCode.SAFETY_RECHECK_CHANGED
    assert outcome.observed_state == "available"
    assert outcome.attachment_ids == ()
    assert outcome.recheck is not None
    assert outcome.reason is not None
    assert fact in outcome.reason
    assert outcome.follow_up


def test_lost_cluster_tag_at_recheck_skips_deletion_without_a_current_snapshot():
    ec2 = FakeEC2(describe=page(dto(tag_key="kubernetes.io/cluster/other")))

    outcome = delete_candidate(ec2)

    assert ec2.delete_calls == []
    assert outcome.action is VolumeAction.SKIPPED
    assert outcome.reason_code is VolumeReasonCode.SAFETY_RECHECK_CHANGED
    assert outcome.recheck is None
    assert outcome.reason is not None
    assert TAG_KEY in outcome.reason


def test_exact_not_found_at_recheck_is_idempotent_success_without_a_delete_request():
    ec2 = FakeEC2(describe=not_found("DescribeVolumes"))

    outcome = delete_candidate(ec2)

    assert ec2.delete_calls == []
    assert outcome.action is VolumeAction.ALREADY_ABSENT
    assert outcome.action_result is VolumeActionResult.IDEMPOTENT_SUCCESS
    assert outcome.reason_code is VolumeReasonCode.ALREADY_ABSENT
    assert outcome.error is None


def test_exact_not_found_at_delete_is_idempotent_success():
    ec2 = FakeEC2(describe=page(dto()), delete=not_found("DeleteVolume"))

    outcome = delete_candidate(ec2)

    assert ec2.delete_calls == [{"VolumeId": "vol-0aaa"}]
    assert outcome.action is VolumeAction.ALREADY_ABSENT
    assert outcome.action_result is VolumeActionResult.IDEMPOTENT_SUCCESS
    assert outcome.recheck == snapshot()


@pytest.mark.parametrize(
    "error",
    [
        throttled("DescribeVolumes"),
        ClientError(
            {"Error": {"Code": "InvalidVolume.NotFound.Extra", "Message": "near miss"}},
            "DescribeVolumes",
        ),
        ClientError(
            {"Error": {"Code": "UnauthorizedOperation", "Message": "denied"}},
            "DescribeVolumes",
        ),
        RuntimeError("connection reset"),
    ],
)
def test_every_other_recheck_error_is_a_safe_failed_record(error):
    ec2 = FakeEC2(describe=error)

    outcome = delete_candidate(ec2)

    assert ec2.delete_calls == []
    assert outcome.action is VolumeAction.FAILED
    assert outcome.action_result is VolumeActionResult.ERROR
    assert outcome.reason_code is VolumeReasonCode.RECHECK_ERROR
    assert outcome.error is not None
    assert outcome.follow_up


def test_every_other_delete_error_is_a_safe_failed_record_with_the_current_snapshot():
    ec2 = FakeEC2(describe=page(dto()), delete=throttled("DeleteVolume"))

    outcome = delete_candidate(ec2)

    assert outcome.action is VolumeAction.FAILED
    assert outcome.reason_code is VolumeReasonCode.DELETE_ERROR
    assert outcome.error is not None
    assert outcome.error.error_code == "RequestLimitExceeded"
    assert outcome.recheck == snapshot()


@pytest.mark.parametrize(
    "response",
    [
        "not-a-response",
        page(),
        page(dto(), dto("vol-0bbb")),
        page(dto("vol-0bbb")),
        page("not-an-object"),
        {"Volumes": None},
    ],
)
def test_ambiguous_or_malformed_recheck_responses_fail_closed(response):
    ec2 = FakeEC2(describe=response)

    outcome = delete_candidate(ec2)

    assert ec2.delete_calls == []
    assert outcome.action is VolumeAction.FAILED
    assert outcome.reason_code is VolumeReasonCode.RECHECK_ERROR


def test_malformed_in_scope_recheck_dto_fails_closed():
    malformed = dto()
    malformed["Size"] = "50"
    ec2 = FakeEC2(describe=page(malformed))

    outcome = delete_candidate(ec2)

    assert ec2.delete_calls == []
    assert outcome.action is VolumeAction.FAILED
    assert outcome.reason_code is VolumeReasonCode.RECHECK_ERROR


def test_unauthorized_policies_cannot_reach_the_deletion_stage():
    ec2 = FakeEC2(describe=page(dto()))
    service = VolumeCleanupService(RecordingFactory(ec2))

    with pytest.raises(ValueError):
        service.delete_candidate(
            ec2=ec2,
            target=target(),
            request=request(VolumePolicy.RETAIN),
            snapshot=snapshot(),
        )

    assert ec2.describe_calls == []
    assert ec2.delete_calls == []


def test_candidate_batch_uses_one_region_client_in_stable_volume_id_order():
    ec2 = FakeEC2(
        describe={"vol-0aaa": page(dto("vol-0aaa")), "vol-0bbb": page(dto("vol-0bbb"))},
    )
    factory = RecordingFactory(ec2)

    outcomes = VolumeCleanupService(factory).delete_candidates(
        target=target(),
        request=request(),
        candidates=[snapshot("vol-0bbb"), snapshot("vol-0aaa")],
    )

    assert factory.calls == [("ec2", REGION)]
    assert [outcome.volume_id for outcome in outcomes] == ["vol-0aaa", "vol-0bbb"]
    assert [call["VolumeId"] for call in ec2.delete_calls] == ["vol-0aaa", "vol-0bbb"]
    assert {outcome.action for outcome in outcomes} == {VolumeAction.DELETE_REQUESTED}


def test_candidate_batch_continues_after_one_recheck_failure():
    ec2 = FakeEC2(
        describe={
            "vol-0aaa": throttled("DescribeVolumes"),
            "vol-0bbb": page(dto("vol-0bbb")),
        },
    )

    outcomes = VolumeCleanupService(RecordingFactory(ec2)).delete_candidates(
        target=target(),
        request=request(),
        candidates=[snapshot("vol-0aaa"), snapshot("vol-0bbb")],
    )

    assert [outcome.action for outcome in outcomes] == [
        VolumeAction.FAILED,
        VolumeAction.DELETE_REQUESTED,
    ]
    assert [call["VolumeId"] for call in ec2.delete_calls] == ["vol-0bbb"]


def test_candidate_batch_client_creation_failure_deletes_nothing():
    factory = RecordingFactory(error=RuntimeError("no credentials"))

    outcomes = VolumeCleanupService(factory).delete_candidates(
        target=target(),
        request=request(),
        candidates=[snapshot("vol-0aaa"), snapshot("vol-0bbb")],
    )

    assert factory.calls == [("ec2", REGION)]
    assert [outcome.action for outcome in outcomes] == [
        VolumeAction.FAILED,
        VolumeAction.FAILED,
    ]
    assert all(outcome.reason_code is VolumeReasonCode.RECHECK_ERROR for outcome in outcomes)


def test_empty_candidate_batch_creates_no_client():
    factory = RecordingFactory(FakeEC2())

    outcomes = VolumeCleanupService(factory).delete_candidates(
        target=target(), request=request(), candidates=[]
    )

    assert outcomes == ()
    assert factory.calls == []


def test_unchanged_safety_facts_are_eligible():
    evaluation = evaluate_recheck(snapshot(), snapshot(), target=target())

    assert evaluation.eligible is True
    assert evaluation.changed_facts == ()
    assert evaluation.reason is None


def test_recheck_evaluation_reports_every_changed_fact():
    evaluation = evaluate_recheck(
        snapshot(),
        snapshot(
            volume_id="vol-0bbb",
            availability_zone="us-east-1b",
            state="in-use",
            cluster_tag_value="shared",
            attachment_ids=("i-0123",),
        ),
        target=target(),
    )

    assert evaluation.eligible is False
    assert evaluation.changed_facts == (
        "volume-identity",
        "availability-zone",
        "cluster-tag-value",
        "state",
        "attachments",
    )


def test_out_of_target_region_recheck_is_never_eligible():
    evaluation = evaluate_recheck(
        snapshot(region="us-west-2", availability_zone="us-west-2a"),
        snapshot(region="us-west-2", availability_zone="us-west-2a"),
        target=target(),
    )

    assert evaluation.eligible is False
    assert "region" in evaluation.changed_facts


@pytest.mark.parametrize(
    "kwargs",
    [
        {"eligible": True, "changed_facts": ("state",), "reason": "changed"},
        {"eligible": False},
        {"eligible": False, "changed_facts": ("state",)},
    ],
)
def test_recheck_evaluation_rejects_inconsistent_values(kwargs):
    with pytest.raises(ValueError):
        RecheckEvaluation(**kwargs)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (not_found("DeleteVolume"), True),
        (throttled("DeleteVolume"), False),
        (RuntimeError("boom"), False),
        (ClientError({}, "DeleteVolume"), False),
    ],
)
def test_only_the_exact_not_found_code_is_recognized(error, expected):
    assert is_exact_volume_not_found(error) is expected


def test_a_malformed_error_object_is_not_treated_as_not_found():
    error = not_found("DeleteVolume")
    error.response["Error"] = "not-an-object"

    assert is_exact_volume_not_found(error) is False
