"""Floci emulator layer: the SageMaker HyperPod preflight and status over the wire.

``gco stacks hyperpod preflight`` (``cli.hyperpod_preflight.run_preflight``)
asks six services whether a Region can take the configured HyperPod block;
``gco stacks hyperpod status`` (``cli.hyperpod_status``) pairs the regional
stack's ``Hyperpod*`` outputs with the cluster SageMaker describes. Floci
models the EKS, EC2, Service Quotas and CloudFormation halves and none of
SageMaker's HyperPod or training-plan operations (docs/FLOCI_TESTING.md, gap
7); its SSM answers but carries none of AWS's public parameters, so the
HyperPod node-AMI tree reads empty. This module proves the checks that read
real responses — an EKS cluster in each access mode, the Region's zones, a
quota listing without the ``for cluster usage`` quotas, the empty AMI tree —
and that every SageMaker refusal lands on the documented path: a warning in
the preflight, a named error in the status.
"""

from __future__ import annotations

import json
from typing import Any

import boto3
import pytest

from gco.hyperpod_config import (
    HYPERPOD_OUTPUT_CLUSTER_ARN,
    HYPERPOD_OUTPUT_CLUSTER_NAME,
    validate_hyperpod_config,
)
from tests._floci import floci_test_markers, unique_name

pytestmark = floci_test_markers()

REGION = "us-east-1"
_FAST_WAIT = {"Delay": 1, "MaxAttempts": 120}
#: The EKS minor the preflight checks HyperPod's AMI tree for (cdk.json's, in a deployment).
_K8S = "1.36"


def _block(**overrides: Any) -> dict[str, Any]:
    """The release run's HyperPod block: one ``ml.g4dn.xlarge`` group, pinned to a zone."""
    raw: dict[str, Any] = {
        "enabled": True,
        "availability_zone": f"{REGION}a",
        "instance_groups": [
            {"name": "gco-hyperpod", "instance_type": "ml.g4dn.xlarge", "instance_count": 1}
        ],
    }
    raw.update(overrides)
    return validate_hyperpod_config(raw)


def _checks(report: Any) -> dict[str, list[tuple[str, str]]]:
    """``check name -> [(status, detail), ...]`` in report order."""
    grouped: dict[str, list[tuple[str, str]]] = {}
    for check in report.checks:
        grouped.setdefault(check.name, []).append((check.status, check.detail))
    return grouped


@pytest.fixture(scope="module")
def network(verified_floci_endpoint):
    """A VPC with two subnets and a security group for the EKS clusters."""
    ec2 = boto3.client("ec2", region_name=REGION)
    vpc_id = ec2.create_vpc(CidrBlock="10.78.0.0/16")["Vpc"]["VpcId"]
    subnets = [
        ec2.create_subnet(VpcId=vpc_id, CidrBlock=f"10.78.{index}.0/24", AvailabilityZone=zone)[
            "Subnet"
        ]["SubnetId"]
        for index, zone in enumerate((f"{REGION}a", f"{REGION}b"), start=1)
    ]
    security_group = ec2.create_security_group(
        GroupName=unique_name("gco-hyperpod-eks"), Description="floci", VpcId=vpc_id
    )["GroupId"]
    yield {"vpc_id": vpc_id, "subnets": subnets, "security_group": security_group}
    ec2.delete_security_group(GroupId=security_group)
    for subnet_id in subnets:
        ec2.delete_subnet(SubnetId=subnet_id)
    ec2.delete_vpc(VpcId=vpc_id)


def _create_eks_cluster(name: str, network: dict[str, Any], *, authentication_mode: str) -> None:
    eks = boto3.client("eks", region_name=REGION)
    eks.create_cluster(
        name=name,
        roleArn=f"arn:aws:iam::{boto3.client('sts').get_caller_identity()['Account']}:role/eks",
        resourcesVpcConfig={
            "subnetIds": network["subnets"],
            "securityGroupIds": [network["security_group"]],
        },
        accessConfig={
            "authenticationMode": authentication_mode,
            "bootstrapClusterCreatorAdminPermissions": True,
        },
    )
    eks.get_waiter("cluster_active").wait(name=name, WaiterConfig=_FAST_WAIT)


def _delete_eks_cluster(name: str) -> None:
    boto3.client("eks", region_name=REGION).delete_cluster(name=name)


class TestPreflight:
    def test_a_region_ready_for_hyperpod_passes_every_check_the_emulator_can_answer(
        self, verified_floci_endpoint, network
    ):
        """The EKS access mode, the zone and the agent image pass; the quota is the verdict.

        Service Quotas answers a real page for ``sagemaker`` that carries no
        ``<type> for cluster usage`` quota, which is exactly what a Region
        without HyperPod capacity reports, and the preflight has to call that
        a failure naming the type rather than pass on an empty listing.
        """
        from cli.hyperpod_preflight import (
            FAIL,
            HYPERPOD_AMI_PARAMETER_ROOT,
            PASS,
            SKIP,
            WARN,
            run_preflight,
        )

        project = unique_name("gcohp").replace("-", "")[:12]
        _create_eks_cluster(f"{project}-{REGION}", network, authentication_mode="API")
        try:
            report = run_preflight(REGION, _block(), project=project, kubernetes_version=_K8S)
        finally:
            _delete_eks_cluster(f"{project}-{REGION}")

        checks = _checks(report)
        assert report.region == REGION and report.cluster == f"{project}-hyperpod"
        assert checks["agent-image"] == [
            (PASS, "health-monitoring agent image published for this Region")
        ]
        # The emulator's SSM carries none of AWS's public parameters, so the AMI
        # tree reads empty: a warning that names the tree, never a verdict.
        assert checks["kubernetes-version"] == [
            (
                WARN,
                "no HyperPod node AMI parameters under "
                f"{HYPERPOD_AMI_PARAMETER_ROOT} in {REGION}; cannot confirm HyperPod "
                f"supports EKS {_K8S}",
            )
        ]
        assert checks["instance-types"] == [
            (
                FAIL,
                "ml.g4dn.xlarge: no 'ml.g4dn.xlarge for cluster usage' quota in us-east-1; "
                "not a HyperPod instance type here",
            )
        ]
        assert checks["training-plans"] == [(SKIP, "no instance group names a training plan")]
        assert checks["availability-zone"] == [(PASS, "us-east-1a is available")]
        assert checks["eks-cluster"] == [(PASS, f"{project}-{REGION} ACTIVE, access entries API")]
        # SageMaker ListClusters is not modeled: the name check cannot be made
        # and says so, as a warning that never blocks the deploy.
        [(status, detail)] = checks["cluster-name"]
        assert status == WARN
        assert detail.startswith("could not check:")
        assert "ValidationException" in detail
        assert not report.ok and [check.name for check in report.failures] == ["instance-types"]

    def test_the_documented_listclusters_gap_shim_makes_the_name_check_pass(
        self, verified_floci_endpoint
    ):
        """Through the shim the harness subprocesses carry, the name reads as free."""
        from cli.hyperpod_preflight import PASS, SKIP, run_preflight
        from tests._floci import apply_known_floci_gap_shims

        session = boto3.session.Session()
        apply_known_floci_gap_shims(session.events)
        project = unique_name("gcohpfree").replace("-", "")[:12]
        report = run_preflight(
            REGION,
            _block(),
            project=project,
            kubernetes_version=_K8S,
            client_factory=session.client,
        )
        checks = _checks(report)
        assert checks["cluster-name"] == [(PASS, f"{project}-hyperpod is free")]
        assert checks["eks-cluster"] == [
            (
                SKIP,
                f"{project}-{REGION} is not deployed yet; the deploy creates it and HyperPod "
                "joins it",
            )
        ]

    def test_an_eks_cluster_without_access_entries_and_a_training_plan_are_named(
        self, verified_floci_endpoint, network
    ):
        from cli.hyperpod_preflight import FAIL, SKIP, WARN, run_preflight

        project = unique_name("gcohpcm").replace("-", "")[:12]
        _create_eks_cluster(f"{project}-{REGION}", network, authentication_mode="CONFIG_MAP")
        plan_arn = f"arn:aws:sagemaker:{REGION}:123456789012:training-plan/gco-floci-plan"
        block = _block(
            availability_zone=None,
            instance_groups=[
                {
                    "name": "gco-hyperpod",
                    "instance_type": "ml.g4dn.xlarge",
                    "instance_count": 1,
                    "training_plan_arn": plan_arn,
                }
            ],
        )
        try:
            report = run_preflight(REGION, block, project=project, kubernetes_version=_K8S)
        finally:
            _delete_eks_cluster(f"{project}-{REGION}")

        checks = _checks(report)
        assert checks["eks-cluster"] == [
            (
                FAIL,
                f"{project}-{REGION} authenticates with CONFIG_MAP; HyperPod nodes join through "
                "an access entry (API or API_AND_CONFIG_MAP)",
            )
        ]
        assert checks["availability-zone"] == [
            (SKIP, "none configured: the first EKS-supported subnet")
        ]
        # DescribeTrainingPlan is not modeled either: a warning, not a verdict
        # on a plan nobody could look at.
        [(status, detail)] = checks["training-plans"]
        assert status == WARN and detail.startswith("could not check:")
        assert sorted(check.name for check in report.failures) == ["eks-cluster", "instance-types"]


class TestStatus:
    def _deploy_stack(self, stack_name: str, outputs: dict[str, str]) -> None:
        cloudformation = boto3.client("cloudformation", region_name=REGION)
        cloudformation.create_stack(
            StackName=stack_name,
            TemplateBody=json.dumps(
                {
                    "AWSTemplateFormatVersion": "2010-09-09",
                    "Resources": {
                        "Marker": {
                            "Type": "AWS::SSM::Parameter",
                            "Properties": {
                                "Name": f"/{stack_name}/marker",
                                "Type": "String",
                                "Value": "present",
                            },
                        }
                    },
                    "Outputs": {key: {"Value": value} for key, value in outputs.items()},
                }
            ),
        )
        cloudformation.get_waiter("stack_create_complete").wait(
            StackName=stack_name, WaiterConfig=_FAST_WAIT
        )

    def _delete_stack(self, stack_name: str) -> None:
        cloudformation = boto3.client("cloudformation", region_name=REGION)
        cloudformation.delete_stack(StackName=stack_name)
        cloudformation.get_waiter("stack_delete_complete").wait(
            StackName=stack_name, WaiterConfig=_FAST_WAIT
        )

    def test_outputs_are_read_with_their_prefix_stripped_and_an_absent_stack_is_empty(
        self, verified_floci_endpoint
    ):
        from cli.hyperpod_status import deployed_cluster, stack_outputs

        stack_name = unique_name("gcohpstatus").replace("-", "")[:20]
        assert stack_outputs(REGION, stack_name) == {}
        assert deployed_cluster(REGION, stack_name) is None

        # A regional stack with HyperPod off publishes none of the outputs.
        self._deploy_stack(stack_name, {"ApiEndpoint": "https://api.example.invalid"})
        try:
            assert stack_outputs(REGION, stack_name) == {}
            assert deployed_cluster(REGION, stack_name) is None
        finally:
            self._delete_stack(stack_name)

    def test_a_published_cluster_sagemaker_cannot_describe_is_a_named_error(
        self, verified_floci_endpoint
    ):
        """The stack says HyperPod is deployed; SageMaker refuses the describe.

        On real AWS that is a cluster deleted outside CloudFormation or a
        denied call; here it is the unmodeled operation. Either way the
        status must name the stack, the cluster and SageMaker's answer.
        """
        from cli.hyperpod_status import HyperpodStatusError, deployed_cluster, stack_outputs

        stack_name = unique_name("gcohpdesc").replace("-", "")[:20]
        account = boto3.client("sts").get_caller_identity()["Account"]
        arn = f"arn:aws:sagemaker:{REGION}:{account}:cluster/abc123def456"
        self._deploy_stack(
            stack_name,
            {
                HYPERPOD_OUTPUT_CLUSTER_NAME: "gco-hyperpod",
                HYPERPOD_OUTPUT_CLUSTER_ARN: arn,
                "ApiEndpoint": "https://api.example.invalid",
            },
        )
        try:
            assert stack_outputs(REGION, stack_name) == {
                "ClusterName": "gco-hyperpod",
                "ClusterArn": arn,
            }
            with pytest.raises(HyperpodStatusError) as excinfo:
                deployed_cluster(REGION, stack_name)
        finally:
            self._delete_stack(stack_name)
        message = str(excinfo.value)
        assert message.startswith(
            f"stack {stack_name} publishes HyperPod cluster gco-hyperpod but it cannot be described: "
        )
        assert "DescribeCluster" in message
