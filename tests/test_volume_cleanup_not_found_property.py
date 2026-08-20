"""Property test proving exact EC2 not-found is the only idempotent absence signal."""

from typing import Any

from botocore.exceptions import ClientError, EndpointConnectionError
from hypothesis import given, settings
from hypothesis import strategies as st

from cli.volume_cleanup import (
    VOLUME_NOT_FOUND_ERROR_CODE,
    DeletionAuthorizationSource,
    RegionalVolumeTarget,
    VolumeAction,
    VolumeActionResult,
    VolumeCleanupRequest,
    VolumeCleanupService,
    VolumePolicy,
    VolumeReasonCode,
    VolumeSnapshot,
    is_exact_volume_not_found,
)

_PROJECT = "gco"
_REGION = "ap-southeast-2"
_STACK = f"{_PROJECT}-{_REGION}"
_TARGET = RegionalVolumeTarget(
    stack_name=_STACK,
    stack_id=None,
    region=_REGION,
    cluster_name=_STACK,
    cluster_tag_key=f"kubernetes.io/cluster/{_STACK}",
)

# Every request that can legitimately reach a just-in-time deletion candidate.
_AUTHORIZED_REQUESTS = tuple(
    VolumeCleanupRequest(
        policy=VolumePolicy.DELETE,
        deletion_authorized=True,
        authorization_source=source,
    )
    for source in (
        DeletionAuthorizationSource.DESTROY_ALL_WITH_YES,
        DeletionAuthorizationSource.EXPLICIT_DELETE_WITH_YES,
        DeletionAuthorizationSource.INTERACTIVE_VOLUME_CONFIRMATION,
    )
)

# The exact absence proof plus service errors that must never establish absence,
# including case, punctuation, prefix, suffix, and non-``ClientError`` near misses.
_EXACT_NOT_FOUND = "exact-not-found"
_ERROR_KINDS = (
    _EXACT_NOT_FOUND,
    "not-found-lowercase",
    "not-found-uppercase",
    "not-found-without-dot",
    "not-found-prefixed",
    "not-found-suffixed",
    "not-found-trailing-space",
    "missing-error-code",
    "unauthorized",
    "throttled",
    "volume-in-use",
    "internal-error",
    "invalid-parameter",
    "endpoint-connection",
    "runtime-error-mentioning-not-found",
    "timeout",
)

_STAGES = ("recheck", "delete")


def _client_error(code: str | None, operation: str) -> BaseException:
    error: dict[str, str] = {"Message": f"offline stub for {operation}"}
    if code is not None:
        error["Code"] = code
    client_error: BaseException = ClientError({"Error": error}, operation)
    return client_error


def _service_error(error_kind: str, operation: str) -> BaseException:
    """Build the generated service failure for one just-in-time stage."""
    codes = {
        _EXACT_NOT_FOUND: VOLUME_NOT_FOUND_ERROR_CODE,
        "not-found-lowercase": "invalidvolume.notfound",
        "not-found-uppercase": "INVALIDVOLUME.NOTFOUND",
        "not-found-without-dot": "InvalidVolumeNotFound",
        "not-found-prefixed": f"Client.{VOLUME_NOT_FOUND_ERROR_CODE}",
        "not-found-suffixed": f"{VOLUME_NOT_FOUND_ERROR_CODE}Exception",
        "not-found-trailing-space": f"{VOLUME_NOT_FOUND_ERROR_CODE} ",
        "unauthorized": "UnauthorizedOperation",
        "throttled": "RequestLimitExceeded",
        "volume-in-use": "VolumeInUse",
        "internal-error": "InternalError",
        "invalid-parameter": "InvalidParameterValue",
    }
    if error_kind in codes:
        return _client_error(codes[error_kind], operation)
    if error_kind == "missing-error-code":
        return _client_error(None, operation)
    if error_kind == "endpoint-connection":
        connection_error: BaseException = EndpointConnectionError(
            endpoint_url=f"https://ec2.{_REGION}.amazonaws.com"
        )
        return connection_error
    if error_kind == "runtime-error-mentioning-not-found":
        return RuntimeError(f"{VOLUME_NOT_FOUND_ERROR_CODE} appeared in an unrelated failure")
    if error_kind == "timeout":
        return TimeoutError("the just-in-time request timed out")
    raise AssertionError(f"unexpected error kind: {error_kind}")


def _volume_dto(snapshot: VolumeSnapshot) -> dict[str, object]:
    return {
        "VolumeId": snapshot.volume_id,
        "AvailabilityZone": snapshot.availability_zone,
        "Size": snapshot.size_gib,
        "State": snapshot.state,
        "Tags": [
            {"Key": "Name", "Value": "prometheus-data"},
            {"Key": _TARGET.cluster_tag_key, "Value": snapshot.cluster_tag_value or ""},
        ],
        "Attachments": [],
    }


class _StubEc2Client:
    """Region-scoped EC2 stand-in that fails at exactly one just-in-time stage."""

    def __init__(self, *, snapshot: VolumeSnapshot, failing_stage: str, error_kind: str) -> None:
        self.snapshot = snapshot
        self.failing_stage = failing_stage
        self.error_kind = error_kind
        self.calls: list[str] = []

    def describe_volumes(self, *, VolumeIds: list[str]) -> dict[str, Any]:  # noqa: N803
        self.calls.append("describe_volumes")
        assert VolumeIds == [self.snapshot.volume_id]
        if self.failing_stage == "recheck":
            raise _service_error(self.error_kind, "DescribeVolumes")
        return {"Volumes": [_volume_dto(self.snapshot)]}

    def delete_volume(self, *, VolumeId: str) -> dict[str, Any]:  # noqa: N803
        self.calls.append("delete_volume")
        assert VolumeId == self.snapshot.volume_id
        raise _service_error(self.error_kind, "DeleteVolume")


def _unused_client_factory(service_name: str, *, region_name: str) -> Any:
    raise AssertionError(f"no additional client is allowed: {service_name} in {region_name}")


@settings(max_examples=200, deadline=None)
@given(
    request=st.sampled_from(_AUTHORIZED_REQUESTS),
    failing_stage=st.sampled_from(_STAGES),
    error_kind=st.sampled_from(_ERROR_KINDS),
    volume_id=st.from_regex(r"vol-[0-9a-f]{17}", fullmatch=True),
    zone_suffix=st.sampled_from(("a", "b", "c")),
    size_gib=st.integers(min_value=1, max_value=16384),
)
def test_exact_not_found_is_idempotent_success(
    request: VolumeCleanupRequest,
    failing_stage: str,
    error_kind: str,
    volume_id: str,
    zone_suffix: str,
    size_gib: int,
) -> None:
    # Feature: cleanup-dynamic-ebs-volumes, Property 7: Exact not-found is idempotent success
    #
    # **Validates: Requirements 5.2**
    snapshot = VolumeSnapshot(
        volume_id=volume_id,
        region=_REGION,
        availability_zone=f"{_REGION}{zone_suffix}",
        size_gib=size_gib,
        state="available",
        cluster_tag_value="owned",
        attachment_ids=(),
    )
    client = _StubEc2Client(
        snapshot=snapshot,
        failing_stage=failing_stage,
        error_kind=error_kind,
    )
    service = VolumeCleanupService(_unused_client_factory)

    outcome = service.delete_candidate(
        ec2=client,
        target=_TARGET,
        request=request,
        snapshot=snapshot,
    )

    exact_not_found = error_kind == _EXACT_NOT_FOUND
    # Only the exact documented EC2 error code proves absence.
    assert is_exact_volume_not_found(_service_error(error_kind, "DeleteVolume")) is exact_not_found

    # Every record keeps the reported volume identity regardless of the stage or error.
    assert outcome.volume_id == snapshot.volume_id
    assert outcome.region == _REGION
    assert outcome.observed_state == snapshot.state
    assert outcome.cluster_tag_value == "owned"
    assert outcome.policy is VolumePolicy.DELETE
    assert outcome.reason and outcome.follow_up

    if exact_not_found:
        # Requirement 5.2: an already-absent target volume is an idempotent success.
        assert outcome.action is VolumeAction.ALREADY_ABSENT
        assert outcome.action_result is VolumeActionResult.IDEMPOTENT_SUCCESS
        assert outcome.reason_code is VolumeReasonCode.ALREADY_ABSENT
        assert VOLUME_NOT_FOUND_ERROR_CODE in (outcome.reason or "")
        assert outcome.error is None
        if failing_stage == "recheck":
            # Absence at the recheck stage issues no deletion request.
            assert client.calls == ["describe_volumes"]
            assert outcome.recheck is None
        else:
            assert client.calls == ["describe_volumes", "delete_volume"]
            assert outcome.recheck == snapshot
        return

    # Requirement 5.2: no other service error may be converted into absence.
    assert outcome.action is VolumeAction.FAILED
    assert outcome.action_result is VolumeActionResult.ERROR
    assert outcome.reason_code is not VolumeReasonCode.ALREADY_ABSENT
    assert outcome.error is not None
    expected_stage_reason = (
        VolumeReasonCode.RECHECK_ERROR
        if failing_stage == "recheck"
        else VolumeReasonCode.DELETE_ERROR
    )
    assert outcome.reason_code is expected_stage_reason
    if failing_stage == "recheck":
        assert client.calls == ["describe_volumes"]
    else:
        assert client.calls == ["describe_volumes", "delete_volume"]
