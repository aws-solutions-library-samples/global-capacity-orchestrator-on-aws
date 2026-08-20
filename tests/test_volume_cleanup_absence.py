"""Focused tests for fail-closed EKS cluster-absence verification."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from cli.volume_cleanup import (
    ClusterAbsenceStatus,
    ClusterAbsenceVerifier,
    RegionalVolumeTarget,
)

_TARGET = RegionalVolumeTarget(
    stack_name="gco-us-east-1",
    stack_id="arn:aws:cloudformation:us-east-1:123456789012:stack/gco-us-east-1/id",
    region="us-east-1",
    cluster_name="gco-us-east-1",
    cluster_tag_key="kubernetes.io/cluster/gco-us-east-1",
)
_NOW = datetime(2026, 4, 17, 12, 30, tzinfo=UTC)


def aws_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "blocked"}}, "DescribeCluster")


def test_stack_deletion_must_be_definitive_before_eks_client_creation():
    factory = MagicMock()

    result = ClusterAbsenceVerifier(factory).verify(target=_TARGET, stack_deleted=False)

    assert result.status is ClusterAbsenceStatus.BLOCKED
    assert result.reason_code == "stack-deletion-unverified"
    factory.assert_not_called()


def test_exact_not_found_creates_region_and_identity_bound_proof():
    eks = MagicMock()
    eks.describe_cluster.side_effect = aws_error("ResourceNotFoundException")
    factory = MagicMock(return_value=eks)

    result = ClusterAbsenceVerifier(factory, clock=lambda: _NOW).verify(
        target=_TARGET, stack_deleted=True
    )

    factory.assert_called_once_with("eks", region_name="us-east-1")
    eks.describe_cluster.assert_called_once_with(name="gco-us-east-1")
    proof = result.proof_for(_TARGET)
    assert proof.verified_at == "2026-04-17T12:30:00Z"


@pytest.mark.parametrize(
    "other",
    [
        RegionalVolumeTarget(
            stack_name="other-us-east-1",
            stack_id=_TARGET.stack_id,
            region=_TARGET.region,
            cluster_name=_TARGET.cluster_name,
            cluster_tag_key=_TARGET.cluster_tag_key,
        ),
        RegionalVolumeTarget(
            stack_name=_TARGET.stack_name,
            stack_id=_TARGET.stack_id,
            region="us-west-2",
            cluster_name=_TARGET.cluster_name,
            cluster_tag_key=_TARGET.cluster_tag_key,
        ),
        RegionalVolumeTarget(
            stack_name=_TARGET.stack_name,
            stack_id=_TARGET.stack_id,
            region=_TARGET.region,
            cluster_name="changed-cluster",
            cluster_tag_key=_TARGET.cluster_tag_key,
        ),
    ],
)
def test_bound_proof_rejects_stack_region_and_cluster_mismatch(other):
    eks = MagicMock()
    eks.describe_cluster.side_effect = aws_error("ResourceNotFoundException")
    result = ClusterAbsenceVerifier(lambda *_args, **_kwargs: eks, clock=lambda: _NOW).verify(
        target=_TARGET, stack_deleted=True
    )

    with pytest.raises(ValueError, match="does not match"):
        result.proof_for(other)


@pytest.mark.parametrize(
    ("response", "reason_code"),
    [
        ({"cluster": {"name": _TARGET.cluster_name}}, "cluster-still-present"),
        (None, "cluster-response-malformed"),
        ([], "cluster-response-malformed"),
        ({}, "cluster-response-malformed"),
        ({"cluster": []}, "cluster-response-malformed"),
        ({"cluster": {}}, "cluster-response-malformed"),
        ({"cluster": {"name": 123}}, "cluster-response-malformed"),
        ({"cluster": {"name": "changed-cluster"}}, "cluster-identity-mismatch"),
    ],
)
def test_present_and_malformed_responses_block_without_ec2_activity(response, reason_code):
    eks = MagicMock()
    eks.describe_cluster.return_value = response
    factory = MagicMock(return_value=eks)

    result = ClusterAbsenceVerifier(factory).verify(target=_TARGET, stack_deleted=True)

    assert result.verified_absent is False
    assert result.reason_code == reason_code
    factory.assert_called_once_with("eks", region_name="us-east-1")
    assert all(call.args[0] != "ec2" for call in factory.call_args_list)
    with pytest.raises(ValueError):
        result.proof_for(_TARGET)


@pytest.mark.parametrize(
    ("error", "reason_code"),
    [
        (aws_error("AccessDeniedException"), "cluster-verification-unauthorized"),
        (aws_error("ThrottlingException"), "cluster-verification-throttled"),
        (aws_error("RequestLimitExceeded"), "cluster-verification-throttled"),
        (aws_error("ResourceNotFound"), "cluster-verification-error"),
        (aws_error("InternalServerException"), "cluster-verification-error"),
        (RuntimeError("transport failed"), "cluster-verification-error"),
    ],
)
def test_auth_exhausted_throttle_and_other_errors_block_without_ec2_activity(error, reason_code):
    eks = MagicMock()
    eks.describe_cluster.side_effect = error
    factory = MagicMock(return_value=eks)

    result = ClusterAbsenceVerifier(factory).verify(target=_TARGET, stack_deleted=True)

    assert result.status is ClusterAbsenceStatus.BLOCKED
    assert result.reason_code == reason_code
    assert result.proof is None
    factory.assert_called_once_with("eks", region_name="us-east-1")
    assert all(call.args[0] != "ec2" for call in factory.call_args_list)
