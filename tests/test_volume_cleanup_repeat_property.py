"""Property test proving repeated volume cleanup is scope-preserving and idempotent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError
from hypothesis import event, given, settings
from hypothesis import strategies as st

from cli.volume_cleanup import (
    VOLUME_NOT_FOUND_ERROR_CODE,
    ClusterAbsenceProof,
    DeletionAuthorizationSource,
    RegionalVolumeTarget,
    TargetVolumeCleanupOutcome,
    VolumeAction,
    VolumeCleanupRequest,
    VolumeCleanupService,
    VolumeCleanupStatus,
    VolumePolicy,
)

_PROJECT = "gco"
_REGIONS = ("us-east-1", "us-west-2", "eu-west-1")
_ZONE_SUFFIXES = ("a", "b", "c")

# Exact cluster-tag value, EC2 state, attachment presence, and whether the volume
# disappears between discovery and the just-in-time recheck.
_VOLUME_KINDS: dict[str, tuple[str, str, bool, bool]] = {
    "eligible": ("owned", "available", False, False),
    "eligible-vanishing": ("owned", "available", False, True),
    "owned-attached": ("owned", "in-use", True, False),
    "owned-creating": ("owned", "creating", False, False),
    "shared": ("shared", "available", False, False),
    "near-miss-tag-value": ("Owned", "available", False, False),
    "empty-tag-value": ("", "available", False, False),
}
_UNRELATED_KINDS = (
    "other-cluster-tag",
    "untagged",
    "name-tag-only",
    "exact-tag-other-region",
)
_PASSES = 3

_RETAIN_REQUEST = VolumeCleanupRequest(
    policy=VolumePolicy.RETAIN,
    deletion_authorized=False,
    authorization_source=DeletionAuthorizationSource.NONE,
)
_DESTROY_ALL_DELETE_REQUEST = VolumeCleanupRequest(
    policy=VolumePolicy.DELETE,
    deletion_authorized=True,
    authorization_source=DeletionAuthorizationSource.DESTROY_ALL_WITH_YES,
)
_EXPLICIT_DELETE_REQUEST = VolumeCleanupRequest(
    policy=VolumePolicy.DELETE,
    deletion_authorized=True,
    authorization_source=DeletionAuthorizationSource.EXPLICIT_DELETE_WITH_YES,
)
_REQUESTS = (_RETAIN_REQUEST, _DESTROY_ALL_DELETE_REQUEST, _EXPLICIT_DELETE_REQUEST)


def _volume_id(index: int) -> str:
    return f"vol-{index:017x}"


def _not_found(operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": VOLUME_NOT_FOUND_ERROR_CODE, "Message": "does not exist"}},
        operation,
    )


@dataclass(frozen=True)
class _TargetVolume:
    """One generated exact-target volume and its reference safety facts."""

    volume_id: str
    tag_value: str
    state: str
    attached: bool
    size_gib: int
    zone: str
    phantom: bool

    @property
    def owned(self) -> bool:
        return self.tag_value == "owned"

    @property
    def eligible(self) -> bool:
        return self.owned and self.state == "available" and not self.attached


def _target_volume(index: int, kind: str, *, size_gib: int, zone: str) -> _TargetVolume:
    tag_value, state, attached, phantom = _VOLUME_KINDS[kind]
    return _TargetVolume(
        volume_id=_volume_id(index),
        tag_value=tag_value,
        state=state,
        attached=attached,
        size_gib=size_gib,
        zone=zone,
        phantom=phantom,
    )


class _InMemoryEC2:
    """In-memory EC2 whose accepted deletions actually remove the stored volume."""

    def __init__(
        self,
        volumes: dict[str, dict[str, Any]],
        *,
        tag_key: str,
        phantoms: frozenset[str],
        page_size: int,
    ) -> None:
        self.volumes = {volume_id: dict(dto) for volume_id, dto in volumes.items()}
        self.tag_key = tag_key
        self.phantoms = set(phantoms)
        self.page_size = page_size
        self.filters: list[Any] = []
        self.described: list[str] = []
        self.delete_requests: list[str] = []

    def get_paginator(self, operation_name: str) -> Any:
        assert operation_name == "describe_volumes"
        return self

    def _tagged(self) -> list[dict[str, Any]]:
        return [
            dto
            for dto in self.volumes.values()
            if any(tag["Key"] == self.tag_key for tag in dto["Tags"])
        ]

    def paginate(self, **kwargs: Any) -> Any:
        self.filters.append(kwargs.get("Filters"))
        tagged = self._tagged()
        pages = [
            {"Volumes": tagged[start : start + self.page_size]}
            for start in range(0, len(tagged), self.page_size)
        ]
        return iter(pages or [{"Volumes": []}])

    def describe_volumes(self, **kwargs: Any) -> Any:
        volume_ids = [str(value) for value in kwargs["VolumeIds"]]
        self.described.extend(volume_ids)
        volume_id = volume_ids[0]
        if volume_id in self.phantoms:
            self.phantoms.discard(volume_id)
            self.volumes.pop(volume_id, None)
            raise _not_found("DescribeVolumes")
        if volume_id not in self.volumes:
            raise _not_found("DescribeVolumes")
        return {"Volumes": [self.volumes[volume_id]]}

    def delete_volume(self, **kwargs: Any) -> Any:
        volume_id = str(kwargs["VolumeId"])
        self.delete_requests.append(volume_id)
        if volume_id not in self.volumes:
            raise _not_found("DeleteVolume")
        del self.volumes[volume_id]
        return {}


class _RegionScopedFactory:
    """Return the single in-memory client only for the exact target Region."""

    def __init__(self, client: _InMemoryEC2, region: str) -> None:
        self.client = client
        self.region = region
        self.calls: list[tuple[str, str]] = []

    def __call__(self, service_name: str, *, region_name: str) -> Any:
        self.calls.append((service_name, region_name))
        assert service_name == "ec2"
        assert region_name == self.region
        return self.client


def _target_dto(volume: _TargetVolume, *, region: str, tag_key: str) -> dict[str, Any]:
    return {
        "VolumeId": volume.volume_id,
        "AvailabilityZone": f"{region}{volume.zone}",
        "Size": volume.size_gib,
        "State": volume.state,
        "Tags": [{"Key": tag_key, "Value": volume.tag_value}],
        "Attachments": (
            [{"VolumeId": volume.volume_id, "InstanceId": "i-0123456789abcdef0"}]
            if volume.attached
            else []
        ),
    }


def _unrelated_dto(volume_id: str, kind: str, *, region: str, tag_key: str) -> dict[str, Any]:
    zone = f"{region}a"
    tags: list[dict[str, str]] = []
    if kind == "other-cluster-tag":
        tags = [{"Key": f"{tag_key}-other", "Value": "owned"}]
    elif kind == "name-tag-only":
        tags = [{"Key": "Name", "Value": "prometheus-data"}]
    elif kind == "exact-tag-other-region":
        other_region = next(candidate for candidate in _REGIONS if candidate != region)
        tags = [{"Key": tag_key, "Value": "owned"}]
        zone = f"{other_region}a"
    return {
        "VolumeId": volume_id,
        "AvailabilityZone": zone,
        "Size": 20,
        "State": "available",
        "Tags": tags,
        "Attachments": [],
    }


def _expected_pass(
    present: frozenset[str],
    volumes: dict[str, _TargetVolume],
    *,
    authorized_delete: bool,
) -> tuple[frozenset[str], dict[str, VolumeAction], VolumeCleanupStatus]:
    """Model one cleanup pass over the volumes still present in the reference set."""
    actions: dict[str, VolumeAction] = {}
    remaining = set(present)
    for volume_id in sorted(present):
        volume = volumes[volume_id]
        if not authorized_delete:
            actions[volume_id] = VolumeAction.RETAINED
            continue
        if not volume.eligible:
            actions[volume_id] = VolumeAction.SKIPPED
            continue
        remaining.discard(volume_id)
        actions[volume_id] = (
            VolumeAction.ALREADY_ABSENT if volume.phantom else VolumeAction.DELETE_REQUESTED
        )
    preserved = [
        volume_id
        for volume_id, action in actions.items()
        if action in {VolumeAction.RETAINED, VolumeAction.SKIPPED}
    ]
    if authorized_delete and any(volumes[volume_id].owned for volume_id in preserved):
        status = VolumeCleanupStatus.FAILED
    elif authorized_delete and preserved:
        status = VolumeCleanupStatus.COMPLETED_WITH_SAFETY_RETENTIONS
    else:
        status = VolumeCleanupStatus.COMPLETED
    return frozenset(remaining), actions, status


def _recorded_actions(outcome: TargetVolumeCleanupOutcome) -> dict[str, VolumeAction]:
    return {record.volume_id: record.action for record in outcome.volumes}


@settings(max_examples=150, deadline=None)
@given(
    region=st.sampled_from(_REGIONS),
    request=st.sampled_from(_REQUESTS),
    target_specs=st.lists(
        st.tuples(
            # Eligible volumes are weighted up so a bounded number of examples still
            # exercises real deletions, absence races, and their repeated passes.
            st.one_of(
                st.sampled_from(("eligible", "eligible-vanishing")),
                st.sampled_from(sorted(_VOLUME_KINDS)),
            ),
            st.integers(min_value=1, max_value=500),
            st.sampled_from(_ZONE_SUFFIXES),
        ),
        max_size=5,
    ),
    unrelated_kinds=st.lists(st.sampled_from(_UNRELATED_KINDS), max_size=4),
    page_size=st.integers(min_value=1, max_value=3),
)
def test_repeated_cleanup_is_scope_preserving_and_idempotent(
    region: str,
    request: VolumeCleanupRequest,
    target_specs: list[tuple[str, int, str]],
    unrelated_kinds: list[str],
    page_size: int,
) -> None:
    # Feature: cleanup-dynamic-ebs-volumes, Property 8: Repeated cleanup is scope-preserving
    # and idempotent
    # **Validates: Requirements 5.4**
    stack_name = f"{_PROJECT}-{region}"
    tag_key = f"kubernetes.io/cluster/{stack_name}"
    target = RegionalVolumeTarget(
        stack_name=stack_name,
        stack_id=None,
        region=region,
        cluster_name=stack_name,
        cluster_tag_key=tag_key,
    )
    absence = ClusterAbsenceProof(
        stack_name=stack_name,
        region=region,
        cluster_name=stack_name,
        verified_at="2026-01-01T00:00:00Z",
    )

    target_volumes = {
        _volume_id(index): _target_volume(index, kind, size_gib=size_gib, zone=zone)
        for index, (kind, size_gib, zone) in enumerate(target_specs)
    }
    unrelated_dtos = {
        _volume_id(len(target_specs) + index): _unrelated_dto(
            _volume_id(len(target_specs) + index),
            kind,
            region=region,
            tag_key=tag_key,
        )
        for index, kind in enumerate(unrelated_kinds)
    }

    stored: dict[str, dict[str, Any]] = {
        volume.volume_id: _target_dto(volume, region=region, tag_key=tag_key)
        for volume in target_volumes.values()
    }
    stored.update(unrelated_dtos)
    ec2 = _InMemoryEC2(
        stored,
        tag_key=tag_key,
        phantoms=frozenset(
            volume.volume_id for volume in target_volumes.values() if volume.phantom
        ),
        page_size=page_size,
    )
    factory = _RegionScopedFactory(ec2, region)
    service = VolumeCleanupService(factory)

    authorized_delete = request.policy is VolumePolicy.DELETE and request.deletion_authorized
    present = frozenset(target_volumes)
    outcomes: list[TargetVolumeCleanupOutcome] = []
    safe_actions: dict[str, VolumeAction] = {}

    for pass_number in range(1, _PASSES + 1):
        expected_present, expected_actions, expected_status = _expected_pass(
            present,
            target_volumes,
            authorized_delete=authorized_delete,
        )
        outcome = service.cleanup(target=target, absence=absence, request=request)
        outcomes.append(outcome)

        assert outcome.status is expected_status
        assert outcome.successful is (expected_status is not VolumeCleanupStatus.FAILED)
        assert _recorded_actions(outcome) == expected_actions
        assert outcome.counts.discovered == len(expected_actions)

        # Repeated passes only move exact target volumes toward retained, requested
        # deletion, or absence, and never recreate or reverse an earlier safe result.
        for volume_id, action in safe_actions.items():
            assert volume_id in ec2.volumes
            assert expected_actions[volume_id] is action
        safe_actions = {
            volume_id: action
            for volume_id, action in expected_actions.items()
            if action in {VolumeAction.RETAINED, VolumeAction.SKIPPED}
        }

        # No unrelated volume is discovered, described by ID, deleted, or mutated.
        assert set(unrelated_dtos).isdisjoint(_recorded_actions(outcome))
        assert set(unrelated_dtos).isdisjoint(ec2.described)
        assert set(unrelated_dtos).isdisjoint(ec2.delete_requests)
        for volume_id, dto in unrelated_dtos.items():
            assert ec2.volumes[volume_id] == dto

        assert set(ec2.volumes) == expected_present | set(unrelated_dtos)
        assert all(
            page_filters == [{"Name": "tag-key", "Values": [tag_key]}]
            for page_filters in ec2.filters
        )
        if not authorized_delete:
            assert ec2.described == []
            assert ec2.delete_requests == []
        event(
            f"pass={pass_number}, policy={request.policy.value}, "
            f"status={expected_status.value}, deleted={outcome.counts.deleted}, "
            f"already_absent={outcome.counts.already_absent}, "
            f"discovered={outcome.counts.discovered}"
        )
        present = expected_present

    # A prior delete-requested or already-absent record becomes current
    # zero-discovery evidence, and every later pass is fully idempotent.
    first_absent = {
        volume_id
        for volume_id, action in _recorded_actions(outcomes[0]).items()
        if action in {VolumeAction.DELETE_REQUESTED, VolumeAction.ALREADY_ABSENT}
    }
    assert first_absent.isdisjoint(_recorded_actions(outcomes[1]))
    assert first_absent.isdisjoint(ec2.volumes)
    assert outcomes[1].to_dict() == outcomes[2].to_dict()
