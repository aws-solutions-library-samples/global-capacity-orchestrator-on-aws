"""Property test proving regional targeting and EBS discovery scope are exact."""

from typing import Any

from hypothesis import event, given, settings
from hypothesis import strategies as st

from cli.volume_cleanup import (
    DeletionAuthorizationSource,
    RegionalVolumeTarget,
    TargetResolution,
    TargetResolutionKind,
    VolumeCleanupRequest,
    VolumePolicy,
    VolumeSnapshot,
    classify_volume,
    normalize_volume_snapshot,
    resolve_regional_volume_target,
)

_REGION_PARTITIONS = {
    "us-east-1": "aws",
    "us-west-2": "aws",
    "eu-west-1": "aws",
    "cn-north-1": "aws-cn",
    "us-gov-west-1": "aws-us-gov",
}
_UNKNOWN_REGION = "xx-nowhere-1"
_ALL_REGIONS = (*_REGION_PARTITIONS, _UNKNOWN_REGION)
_SINGLE_PARTITION_REGIONS = ("us-east-1", "us-west-2", "eu-west-1")
_ACCOUNT = "123456789012"

_PROJECTS = ("gco", "gco-dev", "global-capacity")

_STACK_KINDS = (
    "exact",
    "named",
    "api-bridge",
    "analytics",
    "trailing-hyphen",
    "uppercase-region",
)

_IDENTITY_KINDS = (
    "ordinary-no-arn",
    "ordinary-valid-arn",
    "ordinary-bad-arn",
    "strict-valid",
    "strict-missing-record",
    "strict-ambiguous-cluster",
    "strict-stack-mismatch",
    "strict-bad-arn",
)
_AUTHORIZED_IDENTITY_KINDS = frozenset({"ordinary-no-arn", "ordinary-valid-arn", "strict-valid"})

# Availability Zone suffixes paired with whether the zone belongs to the Region.
_ZONE_SUFFIXES = (
    ("a", True),
    ("b", True),
    ("-wl1-bos-wlz-1", True),
    ("", False),
    ("1", False),
    ("-", False),
    ("A", False),
)

_NON_EXACT_TAG_KEY_KINDS = (
    "exact-uppercase",
    "exact-trailing-space",
    "other-cluster",
    "prefix-only",
    "unrelated",
)
_TAG_VALUES = ("owned", "shared", "Owned", "OWNED", " owned", "owned ", "", "unowned")
_STATES = ("available", "in-use", "creating", "deleting")

_DELETE_REQUEST = VolumeCleanupRequest(
    policy=VolumePolicy.DELETE,
    deletion_authorized=True,
    authorization_source=DeletionAuthorizationSource.DESTROY_ALL_WITH_YES,
)


def _stack_name(project: str, region: str, stack_kind: str) -> str:
    if stack_kind == "exact":
        return f"{project}-{region}"
    if stack_kind == "named":
        return f"{project}-named-stack"
    if stack_kind == "api-bridge":
        return f"{project}-{region}-api-bridge"
    if stack_kind == "analytics":
        return f"{project}-analytics"
    if stack_kind == "trailing-hyphen":
        return f"{project}-{region}-"
    return f"{project}-{region.upper()}"


def _stack_arn(stack_name: str, region: str) -> str:
    partition = _REGION_PARTITIONS.get(region, "aws")
    return f"arn:{partition}:cloudformation:{region}:{_ACCOUNT}:stack/{stack_name}/token"


def _tag_key(kind: str, cluster_name: str) -> str:
    if kind == "exact":
        return f"kubernetes.io/cluster/{cluster_name}"
    if kind == "exact-uppercase":
        return f"kubernetes.io/Cluster/{cluster_name}"
    if kind == "exact-trailing-space":
        return f"kubernetes.io/cluster/{cluster_name} "
    if kind == "other-cluster":
        return f"kubernetes.io/cluster/{cluster_name}-other"
    if kind == "prefix-only":
        return "kubernetes.io/cluster/"
    return "Name"


class _RecordingClientFactory:
    """Client factory that records every service and Region it is asked for."""

    def __init__(self, volumes: list[dict[str, Any]]) -> None:
        self.volumes = volumes
        self.calls: list[tuple[str, str]] = []

    def __call__(self, service_name: str, *, region_name: str) -> Any:
        self.calls.append((service_name, region_name))
        if service_name != "ec2":
            raise AssertionError(f"unexpected service: {service_name}")
        return self

    def describe_volumes(self) -> dict[str, Any]:
        return {"Volumes": self.volumes}


def _discover(
    resolution: TargetResolution,
    factory: _RecordingClientFactory,
) -> list[VolumeSnapshot]:
    """Discover volumes only for an exact authorized regional target."""
    if resolution.kind is not TargetResolutionKind.TARGET:
        return []
    target = resolution.target
    assert target is not None
    ec2 = factory("ec2", region_name=target.region)
    snapshots: list[VolumeSnapshot] = []
    for dto in ec2.describe_volumes()["Volumes"]:
        snapshot = normalize_volume_snapshot(dto, target=target)
        if snapshot is not None:
            snapshots.append(snapshot)
    return snapshots


# Several inputs are deliberately weighted toward the in-scope combination so a
# bounded number of examples still exercises both sides of every "if and only if"
# clause instead of concentrating on trivially rejected inputs.
@settings(max_examples=300, deadline=None)
@given(
    project=st.sampled_from(_PROJECTS),
    configured_regions=st.one_of(
        st.lists(st.sampled_from(_SINGLE_PARTITION_REGIONS), min_size=1, max_size=3, unique=True),
        st.lists(st.sampled_from(_SINGLE_PARTITION_REGIONS), min_size=1, max_size=3, unique=True),
        st.lists(st.sampled_from(_ALL_REGIONS), min_size=1, max_size=3, unique=True),
    ),
    stack_region_configured=st.sampled_from((True, True, True, False)),
    region_index=st.integers(min_value=0, max_value=2),
    unconfigured_region=st.sampled_from(_ALL_REGIONS),
    stack_kind=st.one_of(st.just("exact"), st.just("exact"), st.sampled_from(_STACK_KINDS)),
    identity_kind=st.one_of(
        st.sampled_from(sorted(_AUTHORIZED_IDENTITY_KINDS)),
        st.sampled_from(_IDENTITY_KINDS),
    ),
    volume_id=st.from_regex(r"vol-[0-9a-f]{17}", fullmatch=True),
    volume_in_target_region=st.sampled_from((True, True, False)),
    other_volume_region=st.sampled_from(_ALL_REGIONS),
    zone=st.one_of(
        st.sampled_from(tuple(entry for entry in _ZONE_SUFFIXES if entry[1])),
        st.sampled_from(_ZONE_SUFFIXES),
    ),
    include_exact_tag=st.sampled_from((True, True, False)),
    other_tag_kinds=st.lists(st.sampled_from(_NON_EXACT_TAG_KEY_KINDS), max_size=3, unique=True),
    tag_value=st.sampled_from(_TAG_VALUES),
    size_gib=st.integers(min_value=0, max_value=16384),
    state=st.sampled_from(_STATES),
    attachment_ids=st.lists(
        st.from_regex(r"i-[0-9a-f]{8}", fullmatch=True), max_size=2, unique=True
    ),
)
def test_target_and_discovery_scope_are_exact(
    project: str,
    configured_regions: list[str],
    stack_region_configured: bool,
    region_index: int,
    unconfigured_region: str,
    stack_kind: str,
    identity_kind: str,
    volume_id: str,
    volume_in_target_region: bool,
    other_volume_region: str,
    zone: tuple[str, bool],
    include_exact_tag: bool,
    other_tag_kinds: list[str],
    tag_value: str,
    size_gib: int,
    state: str,
    attachment_ids: list[str],
) -> None:
    # Feature: cleanup-dynamic-ebs-volumes, Property 3: Target and discovery scope are exact
    # **Validates: Requirements 2.1, 2.2, 2.3, 7.2, 7.4**
    stack_region = (
        configured_regions[region_index % len(configured_regions)]
        if stack_region_configured
        else unconfigured_region
    )
    tag_kinds = (["exact"] if include_exact_tag else []) + other_tag_kinds
    stack_name = _stack_name(project, stack_region, stack_kind)
    matched_region = next(
        (region for region in configured_regions if stack_name == f"{project}-{region}"),
        None,
    )
    configuration_valid = (
        all(region in _REGION_PARTITIONS for region in configured_regions)
        and len({_REGION_PARTITIONS.get(region) for region in configured_regions}) == 1
    )
    identity_region = matched_region or stack_region

    strict = identity_kind.startswith("strict-")
    strict_resource: dict[str, str] | None = None
    stack_id: str | None = None
    if identity_kind == "ordinary-valid-arn":
        stack_id = _stack_arn(stack_name, identity_region)
    elif identity_kind == "ordinary-bad-arn":
        stack_id = "not-a-cloudformation-arn"
    elif identity_kind != "strict-missing-record" and strict:
        strict_resource = {
            "stack_name": stack_name,
            "stack_id": _stack_arn(stack_name, identity_region),
            "region": identity_region,
            "cluster_name": stack_name,
        }
        if identity_kind == "strict-ambiguous-cluster":
            strict_resource["cluster_identity_error"] = "multiple EKS physical IDs"
        elif identity_kind == "strict-stack-mismatch":
            strict_resource["stack_name"] = f"{stack_name}-other"
        elif identity_kind == "strict-bad-arn":
            strict_resource["stack_id"] = "not-a-cloudformation-arn"

    resolution = resolve_regional_volume_target(
        project_name=project,
        stack_name=stack_name,
        configured_regions=configured_regions,
        stack_id=stack_id,
        strict=strict,
        strict_resource=strict_resource,
        region_partitions=_REGION_PARTITIONS,
    )

    authorized = (
        matched_region is not None
        and configuration_valid
        and identity_kind in _AUTHORIZED_IDENTITY_KINDS
    )
    if matched_region is None:
        expected_kind = TargetResolutionKind.NOT_REGIONAL
    elif authorized:
        expected_kind = TargetResolutionKind.TARGET
    else:
        expected_kind = TargetResolutionKind.BLOCKED
    assert resolution.kind is expected_kind

    if expected_kind is TargetResolutionKind.TARGET:
        assert matched_region is not None
        assert resolution.target == RegionalVolumeTarget(
            stack_name=stack_name,
            stack_id=None
            if identity_kind == "ordinary-no-arn"
            else _stack_arn(stack_name, matched_region),
            region=matched_region,
            cluster_name=stack_name,
            cluster_tag_key=f"kubernetes.io/cluster/{stack_name}",
        )
    else:
        assert resolution.target is None
        assert resolution.reason_code and resolution.reason

    zone_suffix, zone_in_region = zone
    volume_region = (
        matched_region
        if volume_in_target_region and matched_region is not None
        else other_volume_region
    )
    tags = [
        {
            "Key": _tag_key(kind, stack_name),
            "Value": "prometheus-data" if kind == "unrelated" else tag_value,
        }
        for kind in tag_kinds
    ]
    dto: dict[str, Any] = {
        "VolumeId": volume_id,
        "AvailabilityZone": f"{volume_region}{zone_suffix}",
        "Size": size_gib,
        "State": state,
        "Tags": tags,
        "Attachments": [
            {"VolumeId": volume_id, "InstanceId": instance_id} for instance_id in attachment_ids
        ],
    }

    factory = _RecordingClientFactory([dto])
    snapshots = _discover(resolution, factory)

    in_scope = (
        expected_kind is TargetResolutionKind.TARGET
        and "exact" in tag_kinds
        and volume_region == matched_region
        and zone_in_region
    )
    event(
        f"resolution={expected_kind.value}, exact_tag={'exact' in tag_kinds}, "
        f"target_region={volume_region == matched_region}, zone_in_region={zone_in_region}"
    )
    assert (len(snapshots) == 1) is in_scope

    if not in_scope:
        if expected_kind is not TargetResolutionKind.TARGET:
            assert factory.calls == []
        assert snapshots == []
        return

    assert factory.calls == [("ec2", matched_region)]
    snapshot = snapshots[0]
    assert snapshot.volume_id == volume_id
    assert snapshot.region == matched_region
    assert snapshot.availability_zone == f"{matched_region}{zone_suffix}"
    assert snapshot.cluster_tag_value == tag_value

    classification = classify_volume(snapshot, _DELETE_REQUEST)
    assert classification.owned is (tag_value == "owned")
