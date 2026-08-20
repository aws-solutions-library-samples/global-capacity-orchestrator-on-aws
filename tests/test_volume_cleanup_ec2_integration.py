"""Mocked EKS/EC2 integration tests for the whole volume-cleanup sequence.

These tests drive `ClusterAbsenceVerifier` and `VolumeCleanupService` against
region-scoped AWS doubles that journal every client creation and request in
call order. They assert the cross-stage contract that the focused unit modules
cannot see: cluster-absence proof first, then one complete discovery pass, then
a just-in-time recheck per candidate, and only then a deletion request.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from botocore.exceptions import ClientError

from cli.volume_cleanup import (
    VOLUME_NOT_FOUND_ERROR_CODE,
    ClusterAbsenceStatus,
    ClusterAbsenceVerifier,
    DeletionAuthorizationSource,
    RegionalVolumeTarget,
    VolumeAction,
    VolumeActionResult,
    VolumeCleanupRequest,
    VolumeCleanupService,
    VolumeCleanupStatus,
    VolumePolicy,
    VolumeReasonCode,
)

REGION = "us-east-1"
OTHER_REGION = "us-west-2"
PROJECT = "gco"
VERIFIED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def stack_name(region: str = REGION) -> str:
    return f"{PROJECT}-{region}"


def tag_key(region: str = REGION) -> str:
    return f"kubernetes.io/cluster/{stack_name(region)}"


def target(region: str = REGION) -> RegionalVolumeTarget:
    return RegionalVolumeTarget(
        stack_name=stack_name(region),
        stack_id=None,
        region=region,
        cluster_name=stack_name(region),
        cluster_tag_key=tag_key(region),
    )


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
    key: str | None = None,
    tag_value: str = "owned",
    zone: str = "us-east-1a",
    state: str = "available",
    size: int = 50,
    attachments: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    tags: list[dict[str, object]] = []
    resolved_key = tag_key() if key is None else key
    if resolved_key:
        tags.append({"Key": resolved_key, "Value": tag_value})
    return {
        "VolumeId": volume_id,
        "AvailabilityZone": zone,
        "Size": size,
        "State": state,
        "Tags": tags,
        "Attachments": attachments or [],
    }


def error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "mocked"}}, operation)


def not_found(operation: str) -> ClientError:
    return error(VOLUME_NOT_FOUND_ERROR_CODE, operation)


def throttled(operation: str) -> ClientError:
    return error("RequestLimitExceeded", operation)


class FakeEKS:
    """Region-scoped EKS double that journals every DescribeCluster request."""

    def __init__(self, region: str, journal: list[str], clusters: dict[str, object]) -> None:
        self.region = region
        self.journal = journal
        self.clusters = clusters

    def describe_cluster(self, name: str) -> dict[str, object]:
        self.journal.append(f"eks:{self.region}:describe_cluster:{name}")
        cluster = self.clusters.get(name)
        if cluster is None:
            raise error("ResourceNotFoundException", "DescribeCluster")
        return {"cluster": cluster}


class FakeEC2:
    """Region-scoped EC2 double whose deletions change later discovery results."""

    def __init__(
        self,
        region: str,
        journal: list[str],
        volumes: dict[str, dict[str, object]],
        *,
        page_size: int | None = None,
        extra_pages: list[Any] | None = None,
        page_error: BaseException | None = None,
        describe_errors: dict[str, BaseException] | None = None,
        delete_errors: dict[str, BaseException] | None = None,
        recheck_overrides: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self.region = region
        self.journal = journal
        self.volumes = dict(volumes)
        self.page_size = page_size
        self.extra_pages = list(extra_pages or [])
        self.page_error = page_error
        self.describe_errors = dict(describe_errors or {})
        self.delete_errors = dict(delete_errors or {})
        self.recheck_overrides = dict(recheck_overrides or {})
        self.filters: list[Any] = []

    def get_paginator(self, operation_name: str) -> FakeEC2:
        assert operation_name == "describe_volumes"
        return self

    def paginate(self, **kwargs: Any) -> Iterator[Any]:
        self.filters.append(kwargs.get("Filters"))
        requested = self._requested_tag_key(kwargs)
        self.journal.append(f"ec2:{self.region}:discover:{requested}")
        return self._pages(requested)

    @staticmethod
    def _requested_tag_key(kwargs: dict[str, Any]) -> str:
        filters = kwargs.get("Filters")
        if not isinstance(filters, list) or len(filters) != 1:
            raise AssertionError(f"discovery must use exactly one filter, got {filters!r}")
        only = filters[0]
        if not isinstance(only, dict) or only.get("Name") != "tag-key":
            raise AssertionError(f"discovery must filter on tag-key, got {only!r}")
        values = only.get("Values")
        if not isinstance(values, list) or len(values) != 1:
            raise AssertionError(f"discovery must filter one exact tag key, got {values!r}")
        return str(values[0])

    @staticmethod
    def _has_tag_key(volume: dict[str, object], key: str) -> bool:
        tags = volume.get("Tags")
        if not isinstance(tags, list):
            return False
        return any(isinstance(tag, dict) and tag.get("Key") == key for tag in tags)

    def _pages(self, requested: str) -> Iterator[Any]:
        matched = [
            volume for volume in self.volumes.values() if self._has_tag_key(volume, requested)
        ]
        size = self.page_size or max(1, len(matched))
        pages: list[Any] = [
            {"Volumes": matched[index : index + size]} for index in range(0, len(matched), size)
        ] or [{"Volumes": []}]
        pages.extend(self.extra_pages)
        for number, page in enumerate(pages):
            self.journal.append(f"ec2:{self.region}:page:{number}")
            yield page
        if self.page_error is not None:
            self.journal.append(f"ec2:{self.region}:page-error")
            raise self.page_error

    def describe_volumes(self, **kwargs: Any) -> dict[str, object]:
        volume_ids = [str(value) for value in kwargs.get("VolumeIds", [])]
        volume_id = volume_ids[0] if volume_ids else ""
        self.journal.append(f"ec2:{self.region}:recheck:{volume_id}")
        raised = self.describe_errors.get(volume_id)
        if raised is not None:
            raise raised
        override = self.recheck_overrides.get(volume_id)
        if override is not None:
            return {"Volumes": [override]}
        if volume_id not in self.volumes:
            raise not_found("DescribeVolumes")
        return {"Volumes": [self.volumes[volume_id]]}

    def delete_volume(self, **kwargs: Any) -> dict[str, object]:
        volume_id = str(kwargs["VolumeId"])
        self.journal.append(f"ec2:{self.region}:delete:{volume_id}")
        raised = self.delete_errors.get(volume_id)
        if raised is not None:
            raise raised
        if volume_id not in self.volumes:
            raise not_found("DeleteVolume")
        del self.volumes[volume_id]
        return {}


class FakeAWS:
    """Injected client factory that journals exact service and Region creation."""

    def __init__(
        self,
        *,
        ec2: dict[str, FakeEC2] | None = None,
        clusters: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self.journal: list[str] = []
        self.ec2 = dict(ec2 or {})
        self.clusters = dict(clusters or {})

    def __call__(self, service_name: str, *, region_name: str) -> Any:
        self.journal.append(f"client:{service_name}:{region_name}")
        if service_name == "eks":
            return FakeEKS(region_name, self.journal, self.clusters.get(region_name, {}))
        if service_name == "ec2":
            client = self.ec2.get(region_name)
            if client is None:
                raise AssertionError(f"no EC2 double exists for Region {region_name}")
            return client
        raise AssertionError(f"unexpected AWS service {service_name}")


def build(
    volumes: dict[str, dict[str, object]] | None = None,
    *,
    region: str = REGION,
    clusters: dict[str, dict[str, object]] | None = None,
    **ec2_options: Any,
) -> tuple[FakeAWS, FakeEC2]:
    """Return one journaling factory plus its Region-scoped EC2 double."""
    aws = FakeAWS(clusters=clusters)
    client = FakeEC2(region, aws.journal, volumes or {}, **ec2_options)
    aws.ec2[region] = client
    return aws, client


def run_cleanup(
    aws: FakeAWS,
    *,
    region: str = REGION,
    cleanup_request: VolumeCleanupRequest | None = None,
    stack_deleted: bool = True,
) -> Any:
    """Verify cluster absence and then run cleanup exactly as the CLI path does."""
    resolved = target(region)
    verification = ClusterAbsenceVerifier(aws, clock=lambda: VERIFIED_AT).verify(
        target=resolved,
        stack_deleted=stack_deleted,
    )
    if not verification.verified_absent:
        return verification, None
    outcome = VolumeCleanupService(aws).cleanup(
        target=resolved,
        absence=verification.proof_for(resolved),
        request=cleanup_request or request(),
    )
    return verification, outcome


def entries(journal: list[str], marker: str, *, region: str = REGION) -> list[str]:
    prefix = f"ec2:{region}:{marker}:"
    return [entry.removeprefix(prefix) for entry in journal if entry.startswith(prefix)]


def record(outcome: Any, volume_id: str) -> Any:
    return next(item for item in outcome.volumes if item.volume_id == volume_id)


def test_call_order_is_absence_proof_then_discovery_then_recheck_then_deletion():
    aws, ec2 = build({"vol-0aaa": dto("vol-0aaa"), "vol-0bbb": dto("vol-0bbb")})

    verification, outcome = run_cleanup(aws)

    assert verification.status is ClusterAbsenceStatus.VERIFIED_ABSENT
    assert aws.journal == [
        f"client:eks:{REGION}",
        f"eks:{REGION}:describe_cluster:{stack_name()}",
        f"client:ec2:{REGION}",
        f"ec2:{REGION}:discover:{tag_key()}",
        f"ec2:{REGION}:page:0",
        f"client:ec2:{REGION}",
        f"ec2:{REGION}:recheck:vol-0aaa",
        f"ec2:{REGION}:delete:vol-0aaa",
        f"ec2:{REGION}:recheck:vol-0bbb",
        f"ec2:{REGION}:delete:vol-0bbb",
    ]
    assert outcome.status is VolumeCleanupStatus.COMPLETED
    assert outcome.successful is True
    assert outcome.counts.discovered == 2
    assert outcome.counts.deleted == 2
    assert ec2.volumes == {}


def test_complete_paginated_discovery_precedes_the_first_recheck_and_deletion():
    volumes = {volume_id: dto(volume_id) for volume_id in ("vol-0aaa", "vol-0bbb", "vol-0ccc")}
    aws, _ = build(volumes, page_size=1)

    _, outcome = run_cleanup(aws)

    pages = [index for index, entry in enumerate(aws.journal) if ":page:" in entry]
    first_recheck = next(index for index, entry in enumerate(aws.journal) if ":recheck:" in entry)
    assert len(pages) == 3
    assert max(pages) < first_recheck
    assert entries(aws.journal, "recheck") == ["vol-0aaa", "vol-0bbb", "vol-0ccc"]
    assert entries(aws.journal, "delete") == ["vol-0aaa", "vol-0bbb", "vol-0ccc"]
    assert outcome.counts.deleted == 3


def test_empty_discovery_is_a_completed_no_op_without_per_volume_requests():
    aws, _ = build({})

    _, outcome = run_cleanup(aws)

    assert aws.journal == [
        f"client:eks:{REGION}",
        f"eks:{REGION}:describe_cluster:{stack_name()}",
        f"client:ec2:{REGION}",
        f"ec2:{REGION}:discover:{tag_key()}",
        f"ec2:{REGION}:page:0",
    ]
    assert outcome.status is VolumeCleanupStatus.COMPLETED
    assert outcome.successful is True
    assert outcome.counts.discovered == 0


def test_a_present_cluster_blocks_every_ec2_client_and_request():
    aws, ec2 = build(
        {"vol-0aaa": dto("vol-0aaa")},
        clusters={REGION: {stack_name(): {"name": stack_name()}}},
    )

    verification, outcome = run_cleanup(aws)

    assert verification.status is ClusterAbsenceStatus.PRESENT
    assert outcome is None
    assert aws.journal == [
        f"client:eks:{REGION}",
        f"eks:{REGION}:describe_cluster:{stack_name()}",
    ]
    assert ec2.volumes == {"vol-0aaa": dto("vol-0aaa")}


def test_unverified_stack_deletion_creates_no_aws_client_at_all():
    aws, _ = build({"vol-0aaa": dto("vol-0aaa")})

    verification, outcome = run_cleanup(aws, stack_deleted=False)

    assert verification.status is ClusterAbsenceStatus.BLOCKED
    assert verification.reason_code == "stack-deletion-unverified"
    assert outcome is None
    assert aws.journal == []


def test_no_unrelated_volume_receives_any_recheck_or_deletion_request():
    unrelated = {
        "vol-0other": dto("vol-0other", key=tag_key(OTHER_REGION)),
        "vol-0none": dto("vol-0none", key=""),
        "vol-0zone": dto("vol-0zone", zone="us-west-2a"),
        "vol-0near": dto("vol-0near", key=f"{tag_key()}-suffix"),
    }
    aws, ec2 = build({"vol-0own": dto("vol-0own"), **unrelated})

    _, outcome = run_cleanup(aws)

    assert entries(aws.journal, "recheck") == ["vol-0own"]
    assert entries(aws.journal, "delete") == ["vol-0own"]
    assert set(ec2.volumes) == set(unrelated)
    assert [item.volume_id for item in outcome.volumes] == ["vol-0own"]
    assert outcome.status is VolumeCleanupStatus.COMPLETED


def test_each_target_uses_only_its_own_region_clients_and_volumes():
    aws = FakeAWS()
    east = FakeEC2(REGION, aws.journal, {"vol-0east": dto("vol-0east")})
    west = FakeEC2(
        OTHER_REGION,
        aws.journal,
        {"vol-0west": dto("vol-0west", key=tag_key(OTHER_REGION), zone="us-west-2a")},
    )
    aws.ec2.update({REGION: east, OTHER_REGION: west})

    _, east_outcome = run_cleanup(aws, region=REGION)
    east_journal = list(aws.journal)
    aws.journal.clear()
    _, west_outcome = run_cleanup(aws, region=OTHER_REGION)

    assert all(OTHER_REGION not in entry for entry in east_journal)
    assert all(REGION not in entry for entry in aws.journal)
    assert east.filters == [[{"Name": "tag-key", "Values": [tag_key()]}]]
    assert west.filters == [[{"Name": "tag-key", "Values": [tag_key(OTHER_REGION)]}]]
    assert east_outcome.target_region == REGION
    assert west_outcome.target_region == OTHER_REGION
    assert east.volumes == {}
    assert west.volumes == {}


def test_ambiguous_duplicate_pages_fail_the_target_and_delete_nothing_for_that_volume():
    aws, ec2 = build(
        {"vol-0aaa": dto("vol-0aaa"), "vol-0dup": dto("vol-0dup")},
        extra_pages=[{"Volumes": [dto("vol-0dup", size=99)]}],
    )

    _, outcome = run_cleanup(aws)

    assert entries(aws.journal, "recheck") == ["vol-0aaa"]
    assert entries(aws.journal, "delete") == ["vol-0aaa"]
    assert outcome.status is VolumeCleanupStatus.FAILED
    assert outcome.successful is False
    assert outcome.counts.discovered == 1
    assert outcome.counts.deleted == 1
    assert outcome.error is not None
    assert set(ec2.volumes) == {"vol-0dup"}


def test_conflicting_page_data_for_one_id_prevents_its_deletion():
    conflicting = dto("vol-0aaa")
    conflicting["Size"] = "50"
    aws, ec2 = build({"vol-0aaa": dto("vol-0aaa")}, extra_pages=[{"Volumes": [conflicting]}])

    _, outcome = run_cleanup(aws)

    assert entries(aws.journal, "recheck") == []
    assert entries(aws.journal, "delete") == []
    assert outcome.status is VolumeCleanupStatus.FAILED
    assert outcome.counts.discovered == 0
    assert set(ec2.volumes) == {"vol-0aaa"}


def test_partial_page_failure_issues_no_recheck_or_deletion_request():
    aws, ec2 = build(
        {"vol-0aaa": dto("vol-0aaa"), "vol-0bbb": dto("vol-0bbb")},
        page_size=1,
        page_error=throttled("DescribeVolumes"),
    )

    _, outcome = run_cleanup(aws)

    assert f"ec2:{REGION}:page-error" in aws.journal
    assert entries(aws.journal, "recheck") == []
    assert entries(aws.journal, "delete") == []
    assert outcome.status is VolumeCleanupStatus.FAILED
    assert outcome.counts.discovered == 0
    assert outcome.error is not None
    assert outcome.error.error_code == "RequestLimitExceeded"
    assert set(ec2.volumes) == {"vol-0aaa", "vol-0bbb"}


@pytest.mark.parametrize("malformed", ["not-a-page", {}, {"Volumes": None}])
def test_malformed_discovery_response_fails_closed_before_any_recheck(malformed):
    aws, ec2 = build({"vol-0aaa": dto("vol-0aaa")}, extra_pages=[malformed])

    _, outcome = run_cleanup(aws)

    assert entries(aws.journal, "recheck") == []
    assert entries(aws.journal, "delete") == []
    assert outcome.status is VolumeCleanupStatus.FAILED
    assert outcome.counts.discovered == 0
    assert set(ec2.volumes) == {"vol-0aaa"}


@pytest.mark.parametrize(
    ("changed", "fact"),
    [
        (dto("vol-0aaa", state="in-use"), "state"),
        (
            dto(
                "vol-0aaa",
                state="in-use",
                attachments=[{"InstanceId": "i-0123", "VolumeId": "vol-0aaa"}],
            ),
            "attachments",
        ),
        (dto("vol-0aaa", tag_value="shared"), "cluster-tag-value"),
        (dto("vol-0aaa", zone="us-east-1b"), "availability-zone"),
    ],
)
def test_recheck_race_that_changes_safety_facts_prevents_the_deletion(changed, fact):
    aws, ec2 = build(
        {"vol-0aaa": dto("vol-0aaa")},
        recheck_overrides={"vol-0aaa": changed},
    )

    _, outcome = run_cleanup(aws)

    assert entries(aws.journal, "recheck") == ["vol-0aaa"]
    assert entries(aws.journal, "delete") == []
    assert set(ec2.volumes) == {"vol-0aaa"}
    assert outcome.status is VolumeCleanupStatus.FAILED
    result = record(outcome, "vol-0aaa")
    assert result.action is VolumeAction.SKIPPED
    assert result.action_result is VolumeActionResult.SAFETY_PRESERVED
    assert result.reason_code is VolumeReasonCode.SAFETY_RECHECK_CHANGED
    assert result.reason is not None
    assert fact in result.reason
    assert result.follow_up


@pytest.mark.parametrize(
    "out_of_scope",
    [
        dto("vol-0aaa", key="kubernetes.io/cluster/other"),
        dto("vol-0aaa", zone="us-west-2a"),
    ],
)
def test_recheck_race_that_leaves_the_target_scope_prevents_the_deletion(out_of_scope):
    aws, ec2 = build(
        {"vol-0aaa": dto("vol-0aaa")},
        recheck_overrides={"vol-0aaa": out_of_scope},
    )

    _, outcome = run_cleanup(aws)

    assert entries(aws.journal, "delete") == []
    assert set(ec2.volumes) == {"vol-0aaa"}
    result = record(outcome, "vol-0aaa")
    assert result.action is VolumeAction.SKIPPED
    assert result.reason_code is VolumeReasonCode.SAFETY_RECHECK_CHANGED
    assert outcome.status is VolumeCleanupStatus.FAILED


def test_exact_not_found_at_both_stages_is_idempotent_success():
    aws, _ = build(
        {"vol-0aaa": dto("vol-0aaa"), "vol-0bbb": dto("vol-0bbb")},
        describe_errors={"vol-0aaa": not_found("DescribeVolumes")},
        delete_errors={"vol-0bbb": not_found("DeleteVolume")},
    )

    _, outcome = run_cleanup(aws)

    assert entries(aws.journal, "recheck") == ["vol-0aaa", "vol-0bbb"]
    assert entries(aws.journal, "delete") == ["vol-0bbb"]
    assert outcome.status is VolumeCleanupStatus.COMPLETED
    assert outcome.successful is True
    assert outcome.counts.already_absent == 2
    for volume_id in ("vol-0aaa", "vol-0bbb"):
        result = record(outcome, volume_id)
        assert result.action is VolumeAction.ALREADY_ABSENT
        assert result.action_result is VolumeActionResult.IDEMPOTENT_SUCCESS
        assert result.error is None


def test_near_miss_not_found_error_codes_remain_failures():
    aws, ec2 = build(
        {"vol-0aaa": dto("vol-0aaa"), "vol-0bbb": dto("vol-0bbb")},
        describe_errors={"vol-0aaa": error("InvalidVolume.NotFound.Extra", "DescribeVolumes")},
        delete_errors={"vol-0bbb": error("InvalidVolumeID.NotFound", "DeleteVolume")},
    )

    _, outcome = run_cleanup(aws)

    assert outcome.status is VolumeCleanupStatus.FAILED
    assert outcome.counts.failed == 2
    assert outcome.counts.already_absent == 0
    assert record(outcome, "vol-0aaa").reason_code is VolumeReasonCode.RECHECK_ERROR
    assert record(outcome, "vol-0bbb").reason_code is VolumeReasonCode.DELETE_ERROR
    assert set(ec2.volumes) == {"vol-0aaa", "vol-0bbb"}


def test_cleanup_continues_after_recheck_and_deletion_failures():
    aws, ec2 = build(
        {
            "vol-0aaa": dto("vol-0aaa"),
            "vol-0bbb": dto("vol-0bbb"),
            "vol-0ccc": dto("vol-0ccc"),
            "vol-0ddd": dto("vol-0ddd", tag_value="shared"),
            "vol-0eee": dto("vol-0eee", state="in-use"),
        },
        page_size=2,
        describe_errors={"vol-0aaa": RuntimeError("connection reset")},
        delete_errors={"vol-0bbb": throttled("DeleteVolume")},
    )

    _, outcome = run_cleanup(aws)

    assert entries(aws.journal, "recheck") == ["vol-0aaa", "vol-0bbb", "vol-0ccc"]
    assert entries(aws.journal, "delete") == ["vol-0bbb", "vol-0ccc"]
    assert outcome.status is VolumeCleanupStatus.FAILED
    assert outcome.successful is False
    assert outcome.counts.discovered == 5
    assert outcome.counts.deleted == 1
    assert outcome.counts.failed == 2
    assert outcome.counts.skipped == 2
    assert record(outcome, "vol-0aaa").reason_code is VolumeReasonCode.RECHECK_ERROR
    assert record(outcome, "vol-0bbb").reason_code is VolumeReasonCode.DELETE_ERROR
    assert record(outcome, "vol-0ddd").reason_code is VolumeReasonCode.OWNERSHIP_SAFETY
    assert record(outcome, "vol-0eee").reason_code is VolumeReasonCode.STATE_NOT_AVAILABLE
    assert set(ec2.volumes) == {"vol-0aaa", "vol-0bbb", "vol-0ddd", "vol-0eee"}


def test_retry_after_a_discovery_failure_completes_without_earlier_deletions():
    aws, ec2 = build(
        {"vol-0aaa": dto("vol-0aaa")},
        page_error=throttled("DescribeVolumes"),
    )

    _, blocked = run_cleanup(aws)
    assert blocked.status is VolumeCleanupStatus.FAILED
    assert entries(aws.journal, "delete") == []

    ec2.page_error = None
    aws.journal.clear()
    _, retried = run_cleanup(aws)

    assert entries(aws.journal, "delete") == ["vol-0aaa"]
    assert retried.status is VolumeCleanupStatus.COMPLETED
    assert retried.counts.deleted == 1
    assert ec2.volumes == {}


def test_retry_after_a_deletion_failure_preserves_scope_and_prior_safe_results():
    aws, ec2 = build(
        {
            "vol-0aaa": dto("vol-0aaa"),
            "vol-0bbb": dto("vol-0bbb"),
            "vol-0keep": dto("vol-0keep", key=tag_key(OTHER_REGION)),
        },
        delete_errors={"vol-0bbb": throttled("DeleteVolume")},
    )

    _, first = run_cleanup(aws)
    ec2.delete_errors.clear()
    aws.journal.clear()
    _, second = run_cleanup(aws)

    assert first.status is VolumeCleanupStatus.FAILED
    assert first.counts.deleted == 1
    assert first.counts.failed == 1
    assert entries(aws.journal, "recheck") == ["vol-0bbb"]
    assert entries(aws.journal, "delete") == ["vol-0bbb"]
    assert second.status is VolumeCleanupStatus.COMPLETED
    assert second.counts.discovered == 1
    assert second.counts.deleted == 1
    assert set(ec2.volumes) == {"vol-0keep"}


def test_repeating_a_completed_cleanup_touches_nothing():
    aws, ec2 = build({"vol-0aaa": dto("vol-0aaa")})

    _, first = run_cleanup(aws)
    aws.journal.clear()
    _, second = run_cleanup(aws)

    assert first.counts.deleted == 1
    assert entries(aws.journal, "recheck") == []
    assert entries(aws.journal, "delete") == []
    assert second.status is VolumeCleanupStatus.COMPLETED
    assert second.successful is True
    assert second.counts.discovered == 0
    assert ec2.volumes == {}


def test_retain_policy_discovers_every_volume_and_requests_no_ec2_disposition():
    aws, ec2 = build(
        {
            "vol-0aaa": dto("vol-0aaa"),
            "vol-0bbb": dto("vol-0bbb", tag_value="shared", state="in-use"),
        }
    )

    _, outcome = run_cleanup(aws, cleanup_request=request(VolumePolicy.RETAIN))

    assert entries(aws.journal, "recheck") == []
    assert entries(aws.journal, "delete") == []
    assert outcome.status is VolumeCleanupStatus.COMPLETED
    assert outcome.successful is True
    assert outcome.counts.retained == 2
    assert set(ec2.volumes) == {"vol-0aaa", "vol-0bbb"}


def test_unauthorized_delete_policy_never_creates_an_ec2_client():
    aws, ec2 = build({"vol-0aaa": dto("vol-0aaa")})

    _, outcome = run_cleanup(
        aws,
        cleanup_request=request(VolumePolicy.DELETE, authorized=False),
    )

    assert aws.journal == [
        f"client:eks:{REGION}",
        f"eks:{REGION}:describe_cluster:{stack_name()}",
    ]
    assert outcome.status is VolumeCleanupStatus.SKIPPED
    assert outcome.blocking_reason_code == "deletion-unauthorized"
    assert outcome.counts.discovered == 0
    assert set(ec2.volumes) == {"vol-0aaa"}
