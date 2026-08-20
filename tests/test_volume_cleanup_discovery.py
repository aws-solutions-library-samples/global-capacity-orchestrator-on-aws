"""Offline unit tests for complete, Region-scoped EBS volume discovery."""

from collections.abc import Iterator
from typing import Any

import pytest
from botocore.exceptions import ClientError

from cli.volume_cleanup import (
    ClusterAbsenceProof,
    DeletionAuthorizationSource,
    RegionalVolumeTarget,
    VolumeCleanupRequest,
    VolumeCleanupService,
    VolumeCleanupStatus,
    VolumeDiscoveryStatus,
    VolumePolicy,
    VolumeReasonCode,
    discovery_target_outcome,
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


def proof(**overrides: str) -> ClusterAbsenceProof:
    values: dict[str, str] = {
        "stack_name": STACK,
        "region": REGION,
        "cluster_name": STACK,
        "verified_at": "2026-01-01T00:00:00Z",
    }
    values.update(overrides)
    return ClusterAbsenceProof(**values)


def request(policy: VolumePolicy = VolumePolicy.RETAIN) -> VolumeCleanupRequest:
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


def volume(
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


class FakePaginator:
    """Return canned ``DescribeVolumes`` pages and record the exact request."""

    def __init__(self, pages: list[Any], *, fail_after: Exception | None = None) -> None:
        self.pages = pages
        self.fail_after = fail_after
        self.calls: list[dict[str, Any]] = []

    def paginate(self, **kwargs: Any) -> Iterator[Any]:
        self.calls.append(kwargs)
        return self._iterate()

    def _iterate(self) -> Iterator[Any]:
        yield from self.pages
        if self.fail_after is not None:
            raise self.fail_after


class FakeEC2:
    """Minimal EC2 client exposing only the paginator used by discovery."""

    def __init__(self, pages: list[Any], *, fail_after: Exception | None = None) -> None:
        self.paginator = FakePaginator(pages, fail_after=fail_after)
        self.paginator_names: list[str] = []

    def get_paginator(self, operation_name: str) -> FakePaginator:
        self.paginator_names.append(operation_name)
        return self.paginator


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


def throttling_error() -> ClientError:
    return ClientError(
        {"Error": {"Code": "RequestLimitExceeded", "Message": "slow down"}},
        "DescribeVolumes",
    )


def test_creates_only_a_target_region_ec2_client_with_the_exact_tag_filter():
    ec2 = FakeEC2([{"Volumes": [volume("vol-0aaa")]}])
    factory = RecordingFactory(ec2)

    inventory = VolumeCleanupService(factory).discover_volumes(target=target(), absence=proof())

    assert factory.calls == [("ec2", REGION)]
    assert ec2.paginator_names == ["describe_volumes"]
    assert ec2.paginator.calls == [{"Filters": [{"Name": "tag-key", "Values": [TAG_KEY]}]}]
    assert inventory.complete
    assert [snapshot.volume_id for snapshot in inventory.snapshots] == ["vol-0aaa"]


def test_paginated_pages_are_combined_in_stable_volume_id_order():
    ec2 = FakeEC2(
        [
            {"Volumes": [volume("vol-0ccc"), volume("vol-0aaa")]},
            {"Volumes": [volume("vol-0bbb")]},
        ]
    )

    inventory = VolumeCleanupService(RecordingFactory(ec2)).discover_volumes(
        target=target(), absence=proof()
    )

    assert inventory.status is VolumeDiscoveryStatus.COMPLETE
    assert [snapshot.volume_id for snapshot in inventory.snapshots] == [
        "vol-0aaa",
        "vol-0bbb",
        "vol-0ccc",
    ]
    assert inventory.failures == ()


@pytest.mark.parametrize(
    "out_of_scope",
    [
        volume("vol-0out", tag_key="kubernetes.io/cluster/gco-us-west-2"),
        volume("vol-0out", tag_key=f"{TAG_KEY}-suffix"),
        volume("vol-0out", zone="us-east-2a"),
        volume("vol-0out", zone="us-east-1zz"),
    ],
)
def test_re_scopes_every_page_and_excludes_out_of_boundary_volumes(out_of_scope):
    ec2 = FakeEC2([{"Volumes": [volume("vol-0in"), out_of_scope]}])

    inventory = VolumeCleanupService(RecordingFactory(ec2)).discover_volumes(
        target=target(), absence=proof()
    )

    assert [snapshot.volume_id for snapshot in inventory.snapshots] == ["vol-0in"]
    assert inventory.failures == ()


def test_ambiguous_duplicate_volume_ids_become_unprocessable_failures():
    ec2 = FakeEC2(
        [
            {"Volumes": [volume("vol-0dup", size=50), volume("vol-0keep")]},
            {"Volumes": [volume("vol-0dup", size=99)]},
        ]
    )

    inventory = VolumeCleanupService(RecordingFactory(ec2)).discover_volumes(
        target=target(), absence=proof()
    )

    assert [snapshot.volume_id for snapshot in inventory.snapshots] == ["vol-0keep"]
    assert [failure.volume_id for failure in inventory.failures] == ["vol-0dup"]
    assert inventory.failures[0].reason_code is VolumeReasonCode.EVALUATION_ERROR
    assert inventory.failures[0].follow_up


def test_malformed_in_scope_volume_is_reported_without_blocking_other_volumes():
    malformed = volume("vol-0bad")
    malformed["State"] = ""
    ec2 = FakeEC2([{"Volumes": [malformed, volume("vol-0good")]}])

    inventory = VolumeCleanupService(RecordingFactory(ec2)).discover_volumes(
        target=target(), absence=proof()
    )

    assert inventory.complete
    assert [snapshot.volume_id for snapshot in inventory.snapshots] == ["vol-0good"]
    assert [failure.volume_id for failure in inventory.failures] == ["vol-0bad"]
    assert inventory.failures[0].reason_code is VolumeReasonCode.NORMALIZATION_ERROR
    assert inventory.failures[0].error is not None


def test_unidentifiable_volume_objects_are_recorded_without_a_volume_id():
    ec2 = FakeEC2([{"Volumes": ["not-an-object", {"VolumeId": "vol invalid"}]}])

    inventory = VolumeCleanupService(RecordingFactory(ec2)).discover_volumes(
        target=target(), absence=proof()
    )

    assert inventory.snapshots == ()
    assert [failure.volume_id for failure in inventory.failures] == [None, None]


def test_conflicting_page_data_for_one_id_removes_it_from_actionable_snapshots():
    malformed = volume("vol-0conflict")
    malformed["Size"] = "50"
    ec2 = FakeEC2(
        [{"Volumes": [volume("vol-0conflict")]}, {"Volumes": [malformed]}],
    )

    inventory = VolumeCleanupService(RecordingFactory(ec2)).discover_volumes(
        target=target(), absence=proof()
    )

    assert inventory.snapshots == ()
    assert [failure.volume_id for failure in inventory.failures] == ["vol-0conflict"]


def test_failure_partway_through_pagination_yields_no_actionable_snapshot():
    ec2 = FakeEC2(
        [{"Volumes": [volume("vol-0aaa")]}],
        fail_after=throttling_error(),
    )

    inventory = VolumeCleanupService(RecordingFactory(ec2)).discover_volumes(
        target=target(), absence=proof()
    )

    assert inventory.status is VolumeDiscoveryStatus.INCOMPLETE
    assert inventory.snapshots == ()
    assert inventory.reason_code is VolumeReasonCode.EVALUATION_ERROR
    assert inventory.error is not None
    assert inventory.error.error_code == "RequestLimitExceeded"
    assert inventory.follow_up


@pytest.mark.parametrize("page", ["not-a-page", {}, {"Volumes": None}, {"Volumes": {}}])
def test_malformed_pages_fail_closed_with_no_actionable_snapshot(page):
    ec2 = FakeEC2([{"Volumes": [volume("vol-0aaa")]}, page])

    inventory = VolumeCleanupService(RecordingFactory(ec2)).discover_volumes(
        target=target(), absence=proof()
    )

    assert inventory.status is VolumeDiscoveryStatus.INCOMPLETE
    assert inventory.snapshots == ()
    assert inventory.error is not None
    assert inventory.error.error_type == "VolumeInventoryError"


def test_ec2_client_creation_failure_fails_closed():
    factory = RecordingFactory(error=RuntimeError("no credentials"))

    inventory = VolumeCleanupService(factory).discover_volumes(target=target(), absence=proof())

    assert factory.calls == [("ec2", REGION)]
    assert inventory.status is VolumeDiscoveryStatus.INCOMPLETE
    assert inventory.snapshots == ()


def test_paginator_start_failure_fails_closed():
    class BrokenEC2:
        def get_paginator(self, operation_name: str) -> Any:
            raise RuntimeError(f"no paginator for {operation_name}")

    inventory = VolumeCleanupService(RecordingFactory(BrokenEC2())).discover_volumes(
        target=target(), absence=proof()
    )

    assert inventory.status is VolumeDiscoveryStatus.INCOMPLETE
    assert inventory.snapshots == ()


@pytest.mark.parametrize(
    "mismatch",
    [
        {"stack_name": "gco-us-west-2"},
        {"region": "us-west-2"},
        {"cluster_name": "gco-us-west-2"},
    ],
)
def test_mismatched_absence_proof_creates_no_ec2_client(mismatch):
    factory = RecordingFactory(FakeEC2([]))

    with pytest.raises(ValueError):
        VolumeCleanupService(factory).discover_volumes(target=target(), absence=proof(**mismatch))

    assert factory.calls == []


def test_empty_discovery_is_a_completed_no_op():
    ec2 = FakeEC2([{"Volumes": []}])
    inventory = VolumeCleanupService(RecordingFactory(ec2)).discover_volumes(
        target=target(), absence=proof()
    )

    outcome = discovery_target_outcome(
        target=target(), request=request(VolumePolicy.DELETE), inventory=inventory
    )

    assert outcome is not None
    assert outcome.status is VolumeCleanupStatus.COMPLETED
    assert outcome.successful is True
    assert outcome.volumes == ()
    assert outcome.counts.discovered == 0
    assert outcome.target_region == REGION
    assert outcome.cluster_tag_key == TAG_KEY


def test_incomplete_inventory_produces_a_failed_outcome_with_no_volume_records():
    ec2 = FakeEC2([{"Volumes": [volume("vol-0aaa")]}], fail_after=throttling_error())
    inventory = VolumeCleanupService(RecordingFactory(ec2)).discover_volumes(
        target=target(), absence=proof()
    )

    outcome = discovery_target_outcome(
        target=target(), request=request(VolumePolicy.DELETE), inventory=inventory
    )

    assert outcome is not None
    assert outcome.status is VolumeCleanupStatus.FAILED
    assert outcome.successful is False
    assert outcome.volumes == ()
    assert outcome.error is not None


def test_unprocessable_only_inventory_produces_a_failed_outcome():
    ec2 = FakeEC2([{"Volumes": ["not-an-object"]}])
    inventory = VolumeCleanupService(RecordingFactory(ec2)).discover_volumes(
        target=target(), absence=proof()
    )

    outcome = discovery_target_outcome(target=target(), request=request(), inventory=inventory)

    assert outcome is not None
    assert outcome.status is VolumeCleanupStatus.FAILED
    assert outcome.successful is False
    assert outcome.error is not None
    assert "could not be processed" in outcome.error.message


def test_discovered_snapshots_defer_the_target_outcome_to_per_volume_evaluation():
    ec2 = FakeEC2([{"Volumes": [volume("vol-0aaa")]}])
    inventory = VolumeCleanupService(RecordingFactory(ec2)).discover_volumes(
        target=target(), absence=proof()
    )

    assert discovery_target_outcome(target=target(), request=request(), inventory=inventory) is None
