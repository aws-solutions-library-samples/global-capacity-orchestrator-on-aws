"""Focused tests for exact regional EBS cleanup target resolution."""

from dataclasses import FrozenInstanceError

import pytest

from cli.volume_cleanup import (
    RegionalVolumeTarget,
    TargetResolutionKind,
    resolve_regional_volume_target,
)

_PARTITIONS = {
    "us-east-1": "aws",
    "us-west-2": "aws",
    "us-gov-west-1": "aws-us-gov",
}
_STACK = "gco-us-east-1"
_STACK_ID = "arn:aws:cloudformation:us-east-1:123456789012:stack/gco-us-east-1/id"


def resolve(**overrides):
    inputs = {
        "project_name": "gco",
        "stack_name": _STACK,
        "configured_regions": ["us-east-1", "us-west-2"],
        "region_partitions": _PARTITIONS,
    }
    inputs.update(overrides)
    return resolve_regional_volume_target(**inputs)


def test_exact_ordinary_target_has_exact_cluster_and_tag_identity():
    resolution = resolve(stack_id=_STACK_ID)

    assert resolution.kind is TargetResolutionKind.TARGET
    assert resolution.target == RegionalVolumeTarget(
        stack_name=_STACK,
        stack_id=_STACK_ID,
        region="us-east-1",
        cluster_name=_STACK,
        cluster_tag_key=f"kubernetes.io/cluster/{_STACK}",
    )
    with pytest.raises(FrozenInstanceError):
        resolution.target.region = "us-west-2"


@pytest.mark.parametrize(
    ("overrides", "kind", "reason_code"),
    [
        ({"stack_name": "gco-global"}, TargetResolutionKind.NOT_REGIONAL, None),
        (
            {"stack_name": "gco-regional-api-us-east-1"},
            TargetResolutionKind.NOT_REGIONAL,
            None,
        ),
        ({"stack_name": "gco-us-east-1-extra"}, TargetResolutionKind.NOT_REGIONAL, None),
        ({"stack_name": "other-us-east-1"}, TargetResolutionKind.NOT_REGIONAL, None),
        (
            {"stack_name": "gco-us-west-2", "configured_regions": ["us-east-1"]},
            TargetResolutionKind.NOT_REGIONAL,
            None,
        ),
        (
            {
                "stack_name": "gco-ap-moon-1",
                "configured_regions": ["ap-moon-1"],
            },
            TargetResolutionKind.BLOCKED,
            "invalid-regional-configuration",
        ),
        (
            {"configured_regions": ["us-east-1", "us-gov-west-1"]},
            TargetResolutionKind.BLOCKED,
            "invalid-regional-configuration",
        ),
    ],
)
def test_non_regional_near_miss_unknown_and_cross_partition_targets_make_no_aws_calls(
    monkeypatch, overrides, kind, reason_code
):
    aws_client_calls = []

    def record_aws_client_call(*args, **kwargs):
        aws_client_calls.append((args, kwargs))
        raise AssertionError("target resolution must not create AWS clients")

    monkeypatch.setattr("boto3.client", record_aws_client_call)

    resolution = resolve(**overrides)

    assert resolution.kind is kind
    assert resolution.target is None
    if reason_code is not None:
        assert resolution.reason_code == reason_code
    assert aws_client_calls == []


def test_exact_strict_identity_resolves_to_the_same_target():
    resolution = resolve(
        strict=True,
        strict_resource={
            "stack_name": _STACK,
            "stack_id": _STACK_ID,
            "region": "us-east-1",
            "cluster_name": _STACK,
        },
    )

    assert resolution.kind is TargetResolutionKind.TARGET
    assert resolution.target is not None
    assert resolution.target.stack_id == _STACK_ID


@pytest.mark.parametrize(
    ("strict_resource", "reason_code"),
    [
        (None, "missing-strict-resource-identity"),
        (
            {"cluster_identity_error": "multiple EKS physical IDs"},
            "strict-cluster-identity-unresolved",
        ),
        (
            {"cluster_identity_error": "missing EKS physical ID"},
            "strict-cluster-identity-unresolved",
        ),
        (
            {
                "stack_name": "other-us-east-1",
                "stack_id": _STACK_ID,
                "region": "us-east-1",
                "cluster_name": _STACK,
            },
            "strict-stack-name-mismatch",
        ),
        (
            {
                "stack_name": _STACK,
                "stack_id": _STACK_ID,
                "region": "us-west-2",
                "cluster_name": _STACK,
            },
            "strict-region-mismatch",
        ),
        (
            {
                "stack_name": _STACK,
                "stack_id": _STACK_ID,
                "region": "us-east-1",
                "cluster_name": "other-cluster",
            },
            "strict-cluster-name-mismatch",
        ),
        (
            {
                "stack_name": _STACK,
                "region": "us-east-1",
                "cluster_name": _STACK,
            },
            "missing-strict-stack-arn",
        ),
        (
            {
                "stack_name": _STACK,
                "stack_id": "arn:aws-us-gov:cloudformation:us-east-1:123456789012:stack/gco-us-east-1/id",
                "region": "us-east-1",
                "cluster_name": _STACK,
            },
            "invalid-stack-arn",
        ),
    ],
)
def test_unresolved_changed_or_ambiguous_strict_identity_blocks_without_aws_calls(
    monkeypatch, strict_resource, reason_code
):
    aws_client_calls = []

    def record_aws_client_call(*args, **kwargs):
        aws_client_calls.append((args, kwargs))
        raise AssertionError("blocked strict resolution must not create AWS clients")

    monkeypatch.setattr("boto3.client", record_aws_client_call)

    resolution = resolve(strict=True, strict_resource=strict_resource)

    assert resolution.kind is TargetResolutionKind.BLOCKED
    assert resolution.reason_code == reason_code
    assert resolution.target is None
    assert aws_client_calls == []
