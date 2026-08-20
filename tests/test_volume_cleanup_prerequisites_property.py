"""Property test for fail-closed EBS cleanup prerequisites."""

from dataclasses import replace
from typing import Any

import pytest
from botocore.exceptions import ClientError
from hypothesis import given, settings
from hypothesis import strategies as st

from cli.volume_cleanup import (
    ClusterAbsenceProof,
    ClusterAbsenceStatus,
    ClusterAbsenceVerification,
    ClusterAbsenceVerifier,
    TargetResolutionKind,
    resolve_regional_volume_target,
)

_PROJECT = "gco"
_REGION = "us-east-1"
_STACK = f"{_PROJECT}-{_REGION}"
_STACK_ID = f"arn:aws:cloudformation:{_REGION}:123456789012:stack/{_STACK}/id"
_PARTITIONS = {_REGION: "aws", "us-west-2": "aws"}
_VOLUME_ID = "vol-property-13"


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "offline"}}, "DescribeCluster")


class _EksClient:
    def __init__(self, proof_state: str, malformed_response: object) -> None:
        self.proof_state = proof_state
        self.malformed_response = malformed_response

    def describe_cluster(self, *, name: str) -> object:
        assert name == _STACK
        if self.proof_state in {"verified-absent", "malformed", "ambiguous"}:
            raise _client_error("ResourceNotFoundException")
        if self.proof_state == "present":
            return {"cluster": {"name": name}}
        return self.malformed_response


class _Ec2Client:
    def __init__(self) -> None:
        self.volumes = {_VOLUME_ID}
        self.calls: list[str] = []

    def describe_volumes(self) -> dict[str, Any]:
        self.calls.append("describe")
        return {"Volumes": [{"VolumeId": volume_id} for volume_id in self.volumes]}

    def delete_volume(self, *, VolumeId: str) -> None:  # noqa: N803
        self.calls.append("delete")
        self.volumes.remove(VolumeId)


class _RecordingClientFactory:
    def __init__(self, proof_state: str, malformed_response: object) -> None:
        self.eks = _EksClient(proof_state, malformed_response)
        self.ec2 = _Ec2Client()
        self.calls: list[tuple[str, str]] = []

    def __call__(self, service_name: str, *, region_name: str) -> object:
        self.calls.append((service_name, region_name))
        if service_name == "eks":
            return self.eks
        if service_name == "ec2":
            return self.ec2
        raise AssertionError(f"unexpected service: {service_name}")


def _resolve_identity(identity_state: str, strict_valid: bool):
    strict_resource: dict[str, str] | None = None
    strict = identity_state != "valid" or strict_valid
    stack_id = _STACK_ID
    if strict:
        strict_resource = {
            "stack_name": _STACK,
            "stack_id": _STACK_ID,
            "region": _REGION,
            "cluster_name": _STACK,
        }
    if identity_state == "absent":
        strict_resource = None
    elif identity_state == "malformed":
        assert strict_resource is not None
        strict_resource["stack_id"] = "not-an-arn"
    elif identity_state == "ambiguous":
        assert strict_resource is not None
        strict_resource["cluster_identity_error"] = "multiple EKS physical IDs"

    return resolve_regional_volume_target(
        project_name=_PROJECT,
        stack_name=_STACK,
        configured_regions=[_REGION],
        stack_id=stack_id if not strict else None,
        strict=strict,
        strict_resource=strict_resource,
        region_partitions=_PARTITIONS,
    )


@settings(max_examples=150, deadline=None)
@given(
    identity_state=st.sampled_from(("valid", "absent", "malformed", "ambiguous")),
    proof_state=st.sampled_from(("verified-absent", "absent", "present", "malformed", "ambiguous")),
    strict_valid=st.booleans(),
    malformed_response=st.sampled_from((None, [], {}, {"cluster": []}, {"cluster": {"name": 0}})),
    example_nonce=st.integers(),
)
def test_prerequisite_failures_gate_all_ebs_activity(
    identity_state: str,
    proof_state: str,
    strict_valid: bool,
    malformed_response: object,
    example_nonce: int,
) -> None:
    # Feature: cleanup-dynamic-ebs-volumes, Property 13: Prerequisite failures gate all EBS activity
    # **Validates: Requirements 3.2, 5.5, 7.4, 7.6**
    del example_nonce
    resolution = _resolve_identity(identity_state, strict_valid)
    factory = _RecordingClientFactory(proof_state, malformed_response)
    initial_volumes = factory.ec2.volumes.copy()
    gate_open = False

    if resolution.kind is TargetResolutionKind.TARGET:
        target = resolution.target
        assert target is not None
        verification = ClusterAbsenceVerifier(factory).verify(
            target=target,
            stack_deleted=proof_state != "absent",
        )

        if verification.status is ClusterAbsenceStatus.VERIFIED_ABSENT:
            if proof_state == "malformed":
                with pytest.raises(ValueError, match="ISO-8601"):
                    ClusterAbsenceProof(
                        stack_name=target.stack_name,
                        region=target.region,
                        cluster_name=target.cluster_name,
                        verified_at="not-a-timestamp",
                    )
            else:
                if proof_state == "ambiguous":
                    assert verification.proof is not None
                    verification = ClusterAbsenceVerification(
                        status=ClusterAbsenceStatus.VERIFIED_ABSENT,
                        proof=replace(verification.proof, region="us-west-2"),
                    )
                try:
                    verification.proof_for(target)
                except ValueError:
                    pass
                else:
                    ec2 = factory("ec2", region_name=target.region)
                    assert isinstance(ec2, _Ec2Client)
                    ec2.describe_volumes()
                    ec2.delete_volume(VolumeId=_VOLUME_ID)
                    gate_open = True
    else:
        assert resolution.kind is TargetResolutionKind.BLOCKED

    expected_open = identity_state == "valid" and proof_state == "verified-absent"
    assert gate_open is expected_open
    if expected_open:
        assert factory.ec2.calls == ["describe", "delete"]
        assert factory.ec2.volumes == set()
    else:
        assert all(service != "ec2" for service, _region in factory.calls)
        assert factory.ec2.calls == []
        assert factory.ec2.volumes == initial_volumes
